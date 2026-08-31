"""安全认证模块数据访问层测试（S4）。

覆盖方案 §5 的 S4 验收标准：**单元测试覆盖 CRUD + 节流验证**。
另覆盖 PAT 校验缓存（D20）、级联吊销、限流数据层、在线状态。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from audit.security_store import (
    LOGIN_MAX_FAILURES_USER,
    _last_seen_cache,
    _pat_cache,
    _touch_cache,
    invalidate_pat_cache,
)
from audit.store import SqliteEventStore


@pytest.fixture(autouse=True)
def _clean_caches():
    """模块级节流/缓存单例会在用例间串味，每个用例前后清空。"""
    _last_seen_cache.clear()
    _touch_cache.clear()
    _pat_cache.clear()
    yield
    _last_seen_cache.clear()
    _touch_cache.clear()
    _pat_cache.clear()


@pytest.fixture
def db_path(tmp_path: Path):
    return str(tmp_path / "audit.db")


@pytest.fixture
def store(db_path: str, monkeypatch: pytest.MonkeyPatch):
    # 关掉 bootstrap：每个用例都是全新空库，bootstrap 必然建出 admin，
    # 会让「list_users 只有 alice/bob」这类断言多出一个 admin。
    # bootstrap_first_user 本身在 tests/test_security_schema.py 里单独测。
    monkeypatch.setattr(
        "audit.security_schema.bootstrap_first_user", lambda conn: None
    )
    # 同步 fixture：SqliteEventStore 构造是同步的，各 async 方法自带 to_thread 包装。
    # 用 async fixture 需要 pytest-asyncio 的 asyncio_mode=auto，本项目没开，会拿到 coroutine。
    return SqliteEventStore(db_path)


# ============================================================
# 用户 CRUD
# ============================================================

@pytest.mark.asyncio
async def test_create_and_get_user(store: SqliteEventStore):
    await store.create_user("u1", "alice", "alice-pwd-123", display_name="Alice")
    user = await store.get_user("u1")
    assert user["username"] == "alice"
    assert user["display_name"] == "Alice"
    assert user["must_reset_password"] == 1
    assert "alice-pwd-123" not in user["password_hash"], "明文绝不能落库"
    assert user["password_hash"].startswith("$argon2")


@pytest.mark.asyncio
async def test_get_by_username_and_list(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    await store.create_user("u2", "bob", "pwd-bob-456")

    assert (await store.get_user_by_username("alice"))["user_id"] == "u1"
    assert await store.get_user_by_username("nobody") is None
    assert {u["username"] for u in await store.list_users()} == {"alice", "bob"}


@pytest.mark.asyncio
async def test_list_users_excludes_disabled_by_default(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    await store.create_user("u2", "bob", "pwd-bob-456")
    await store.set_user_disabled("u2", True, reason="test")

    assert [u["user_id"] for u in await store.list_users()] == ["u1"]
    assert len(await store.list_users(include_disabled=True)) == 2


@pytest.mark.asyncio
async def test_update_user_whitelist_enforced(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    await store.update_user("u1", display_name="Alice Z", email="a@example.com")
    assert (await store.get_user("u1"))["display_name"] == "Alice Z"

    # password_hash 故意在白名单内（set_user_password 走的就是这条路），
    # 真正要挡的是 user_id / username / created_at 这类不该被改动的字段。
    with pytest.raises(ValueError, match="不允许更新"):
        await store.update_user("u1", username="hacked")


@pytest.mark.asyncio
async def test_set_user_password_clears_must_reset(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    assert (await store.get_user("u1"))["must_reset_password"] == 1
    await store.set_user_password("u1", "new-pass-456")
    user = await store.get_user("u1")
    assert user["must_reset_password"] == 0
    assert "new-pass-456" not in user["password_hash"]


# ============================================================
# 角色 / 权限
# ============================================================

@pytest.mark.asyncio
async def test_bind_unbind_and_scopes_union(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    # 注意 granted_by 是 FK → users.user_id，bootstrap 被关掉后没有 user_admin，
    # 传了会直接 IntegrityError。这里留空。
    await store.bind_user_role("u1", "role_developer")
    await store.bind_user_role("u1", "role_viewer")

    roles = await store.list_user_roles("u1")
    assert {r["role_id"] for r in roles} == {"role_developer", "role_viewer"}

    scopes = (await store.compute_user_scopes("u1")).split()
    dev = set(await store.list_role_permissions("role_developer"))
    viewer = set(await store.list_role_permissions("role_viewer"))
    assert set(scopes) == dev | viewer, "多角色权限取并集"

    await store.unbind_user_role("u1", "role_viewer")
    assert set((await store.compute_user_scopes("u1")).split()) == dev


@pytest.mark.asyncio
async def test_scopes_empty_for_disabled_user(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    await store.bind_user_role("u1", "role_developer")
    assert await store.compute_user_scopes("u1")

    await store.set_user_disabled("u1", True, reason="test")
    assert await store.compute_user_scopes("u1") == "", "禁用用户不该有任何权限"


@pytest.mark.asyncio
async def test_scopes_empty_for_user_without_roles(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    assert await store.compute_user_scopes("u1") == ""


@pytest.mark.asyncio
async def test_permission_matrix(store: SqliteEventStore):
    matrix = await store.get_permission_matrix()
    assert set(matrix) == {"role_owner", "role_admin", "role_developer", "role_viewer"}
    assert len(matrix["role_owner"]) == 46
    assert "security.roles.write" not in matrix["role_admin"]


@pytest.mark.asyncio
async def test_owner_unbind_raises(store: SqliteEventStore, db_path: str):
    """owner 解绑被 DB 触发器拒绝（S4 层能正确向上传播异常）。"""
    import sqlite3

    await store.create_user("u1", "alice", "pwd-alice-123")
    await store.bind_user_role("u1", "role_owner")
    with pytest.raises(sqlite3.IntegrityError):
        await store.unbind_user_role("u1", "role_owner")


# ============================================================
# 认证 Session
# ============================================================

@pytest.mark.asyncio
async def test_create_and_resolve_auth_session(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    raw, sess = await store.create_auth_session(
        "u1", scope="runs.read runs.write", ip="1.2.3.4", user_agent="pytest"
    )

    assert raw.startswith("ses_")
    assert "ses_" not in sess["token_hash"], "存的是 hash 不是明文"
    assert len(sess["token_hash"]) == 64, "SHA-256 hex"
    assert sess["scope"] == "runs.read runs.write"

    found = await store.get_auth_session_by_token(raw)
    assert found["session_id"] == sess["session_id"]
    assert await store.get_auth_session_by_token("ses_bogus") is None


@pytest.mark.asyncio
async def test_sliding_and_absolute_expiry_set(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    _, sess = await store.create_auth_session("u1", sliding_days=7, absolute_days=30)
    now = datetime.now(timezone.utc)
    sliding = datetime.fromisoformat(sess["sliding_expires_at"])
    absolute = datetime.fromisoformat(sess["absolute_expires_at"])
    # timedelta.days 是向下取整（6.99 天 → 6），必须换算成浮点天数再比
    sliding_days = (sliding - now).total_seconds() / 86400
    absolute_days = (absolute - now).total_seconds() / 86400
    assert 6.9 < sliding_days <= 7
    assert 29.9 < absolute_days <= 30
    assert sliding < absolute


@pytest.mark.asyncio
async def test_extend_sliding_expiry(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    _, sess = await store.create_auth_session("u1", sliding_days=7)
    old = datetime.fromisoformat(sess["sliding_expires_at"])
    new = await store.extend_sliding_expiry(sess["session_id"], sliding_days=7)
    assert datetime.fromisoformat(new) > old


@pytest.mark.asyncio
async def test_revoke_session_and_revoke_all(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    _, s1 = await store.create_auth_session("u1", ip="1.1.1.1")
    _, s2 = await store.create_auth_session("u1", ip="2.2.2.2")
    _, s3 = await store.create_auth_session("u1", ip="3.3.3.3")

    assert await store.revoke_auth_session(s2["session_id"], reason="logout")
    assert not await store.revoke_auth_session(s2["session_id"]), "重复撤销返回 False"
    assert len(await store.list_auth_sessions("u1")) == 2

    # 注销其他设备：保留 s1，吊销其余
    n = await store.revoke_all_sessions("u1", except_session_id=s1["session_id"], reason="logout-all")
    assert n == 1
    remaining = await store.list_auth_sessions("u1")
    assert [r["session_id"] for r in remaining] == [s1["session_id"]]


# ============================================================
# API 令牌（PAT）
# ============================================================

@pytest.mark.asyncio
async def test_create_and_verify_api_token(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    raw, tok = await store.create_api_token(
        "u1", "ci-token", scopes="runs.read", expires_in_days=90
    )

    assert raw.startswith("pat_")
    assert tok["prefix"] == raw[4:16], "prefix 必须是 token 明文的固定前段（用于索引查询）"
    assert tok["last4"] == raw[-4:]
    assert tok["token_hash"].startswith("$argon2")
    assert raw not in tok["token_hash"]

    verified = await store.verify_api_token(raw)
    assert verified["token_id"] == tok["token_id"]
    assert verified["scopes"] == "runs.read"


@pytest.mark.asyncio
async def test_verify_api_token_rejects_bad_input(store: SqliteEventStore):
    assert await store.verify_api_token("") is None
    assert await store.verify_api_token("pat_short") is None
    assert await store.verify_api_token("ses_notapat") is None
    assert await store.verify_api_token("pat_" + "A" * 34) is None, "不存在的 token"


@pytest.mark.asyncio
async def test_revoke_api_token_invalidates_cache(store: SqliteEventStore):
    """D20 的关键正确性：撤销后 60s 缓存必须立刻失效，否则已撤销 token 还能用。"""
    await store.create_user("u1", "alice", "pwd-alice-123")
    raw, tok = await store.create_api_token("u1", "tok", expires_in_days=30)

    assert await store.verify_api_token(raw) is not None      # 首次：走 argon2，写入缓存
    assert len(_pat_cache) == 1
    assert await store.verify_api_token(raw) is not None      # 二次：命中缓存

    assert await store.revoke_api_token(tok["token_id"], reason="leaked")
    assert await store.verify_api_token(raw) is None, "撤销后缓存必须失效"


@pytest.mark.asyncio
async def test_invalidate_pat_cache_manual(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    raw, _ = await store.create_api_token("u1", "tok", expires_in_days=30)
    await store.verify_api_token(raw)
    assert len(_pat_cache) == 1

    import hashlib
    invalidate_pat_cache(hashlib.sha256(raw.encode()).hexdigest())
    assert len(_pat_cache) == 0


@pytest.mark.asyncio
async def test_list_and_revoke_all_api_tokens(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    await store.create_user("u2", "bob", "pwd-bob-456")
    _, t1 = await store.create_api_token("u1", "a", expires_in_days=30)
    _, t2 = await store.create_api_token("u1", "b", expires_in_days=30)
    _, t3 = await store.create_api_token("u2", "c", expires_in_days=30)

    assert {t["token_id"] for t in await store.list_api_tokens("u1")} == {
        t1["token_id"], t2["token_id"]
    }
    assert await store.revoke_api_token(t1["token_id"])
    assert [t["token_id"] for t in await store.list_api_tokens("u1")] == [t2["token_id"]]

    n = await store.revoke_all_api_tokens("u1", reason="user_disabled")
    assert n == 1
    assert await store.list_api_tokens("u1") == []
    assert len(await store.list_api_tokens("u2")) == 1, "不该影响其他用户"


# ============================================================
# 禁用级联吊销（方案 §4.4）
# ============================================================

@pytest.mark.asyncio
async def test_disable_user_cascades_revocation(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    _, sess = await store.create_auth_session("u1")
    raw, tok = await store.create_api_token("u1", "tok", expires_in_days=30)
    await store.verify_api_token(raw)      # 确保进了缓存

    await store.set_user_disabled("u1", True, reason="offboard")

    assert (await store.get_user("u1"))["disabled_at"]
    assert await store.list_auth_sessions("u1") == []
    assert await store.list_api_tokens("u1") == []
    assert await store.verify_api_token(raw) is None, "禁用后 PAT 必须立刻失效"
    assert len(_pat_cache) == 0


@pytest.mark.asyncio
async def test_enable_user_does_not_resurrect_credentials(store: SqliteEventStore):
    """重新启用用户不该复活已吊销的凭证——安全语义上必须重新签发。"""
    await store.create_user("u1", "alice", "pwd-alice-123")
    _, sess = await store.create_auth_session("u1")
    await store.set_user_disabled("u1", True, reason="test")
    await store.set_user_disabled("u1", False)

    assert (await store.get_user("u1"))["disabled_at"] is None
    assert await store.list_auth_sessions("u1") == [], "已吊销 session 不该复活"


# ============================================================
# 节流（D20）
# ============================================================

@pytest.mark.asyncio
async def test_touch_last_seen_throttled(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")

    assert await store.touch_last_seen("u1") is True, "首次落库"
    assert (await store.get_user("u1"))["last_seen_at"]
    assert await store.touch_last_seen("u1") is False, "30s 内被节流"
    assert await store.touch_last_seen("u1", throttle=False) is True, "可强制落库"


@pytest.mark.asyncio
async def test_touch_auth_session_and_token_throttled(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    _, sess = await store.create_auth_session("u1")
    raw, tok = await store.create_api_token("u1", "tok", expires_in_days=30)

    assert await store.touch_auth_session(sess["session_id"]) is True
    assert await store.touch_auth_session(sess["session_id"]) is False

    assert await store.touch_api_token(tok["token_id"], ip="9.9.9.9") is True
    assert await store.touch_api_token(tok["token_id"], ip="9.9.9.9") is False
    assert (await store.get_api_token(tok["token_id"]))["last_used_ip"] == "9.9.9.9"


# ============================================================
# 登录限流（数据层）
# ============================================================

@pytest.mark.asyncio
async def test_login_failure_counting_and_lock(store: SqliteEventStore):
    key = "user:alice"
    for _ in range(LOGIN_MAX_FAILURES_USER - 1):
        assert await store.record_login_failure(key, max_failures=LOGIN_MAX_FAILURES_USER) is False

    assert await store.record_login_failure(key, max_failures=LOGIN_MAX_FAILURES_USER) is True
    assert await store.is_login_locked(key)

    await store.reset_login_attempts(key)
    assert not await store.is_login_locked(key)
    assert await store.get_login_attempt(key) is None


@pytest.mark.asyncio
async def test_login_window_expiry_resets_count(store: SqliteEventStore):
    """超过统计窗口的旧失败不该继续累加。"""
    key = "ip:1.2.3.4"
    await store.record_login_failure(key, max_failures=100, window_sec=300)
    row = await store.get_login_attempt(key)
    assert row["failures"] == 1

    # 把 first_fail_at 改到 1 小时前 → 下次失败应重置为 1 而不是累加成 2
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE security_login_attempts SET first_fail_at = ? WHERE key = ?", (old, key)
    )
    await store.record_login_failure(key, max_failures=100, window_sec=300)
    assert (await store.get_login_attempt(key))["failures"] == 1


@pytest.mark.asyncio
async def test_cleanup_login_attempts(store: SqliteEventStore):
    await store.record_login_failure("ip:1.1.1.1", max_failures=100)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    store._conn.execute(
        "UPDATE security_login_attempts SET first_fail_at = ?", (old,)
    )
    n = await store.cleanup_login_attempts(older_than_sec=86400)
    assert n == 1
    assert await store.get_login_attempt("ip:1.1.1.1") is None


@pytest.mark.asyncio
async def test_cleanup_login_attempts_removes_expired_locks(store: SqliteEventStore):
    """锁定过期的行也要清掉，否则被锁过的 IP 会永久留在表里。"""
    key = "ip:8.8.8.8"
    for _ in range(LOGIN_MAX_FAILURES_USER):
        await store.record_login_failure(key, max_failures=LOGIN_MAX_FAILURES_USER)
    assert await store.is_login_locked(key)

    # 把整行挪到 2 天前（含 locked_until），锁早就该过期了
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    store._conn.execute(
        "UPDATE security_login_attempts SET first_fail_at = ?, locked_until = ? "
        "WHERE key = ?", (old, old, key),
    )
    assert await store.cleanup_login_attempts(older_than_sec=86400) == 1
    assert await store.get_login_attempt(key) is None


# ============================================================
# 在线状态
# ============================================================

@pytest.mark.asyncio
async def test_is_online(store: SqliteEventStore):
    await store.create_user("u1", "alice", "pwd-alice-123")
    user = await store.get_user("u1")
    assert SqliteEventStore.is_online(user) is False, "从未活跃 = 离线"

    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert SqliteEventStore.is_online({"last_seen_at": recent}) is True
    assert SqliteEventStore.is_online({"last_seen_at": stale}) is False
    assert SqliteEventStore.is_online({"last_seen_at": "not-a-date"}) is False
