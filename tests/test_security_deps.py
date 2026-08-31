"""S6 鉴权依赖链测试。

覆盖方案 §8 验收标准：
  - anon / session / PAT 三条路径
  - 过期 session 401、已撤销 PAT 401
  - scope 不足 403
  - must_reset_password 强制改密（含白名单豁免）
  - dev 绕过必须环境变量 + 标记文件双条件

**不用 TestClient**：本项目 httpx 0.28.1 + starlette 0.35.1 组合下
`TestClient(app=...)` 会抛 `Client.__init__() got an unexpected keyword argument 'app'`
（httpx 0.28 移除了 `app` 参数，starlette 需 ≥0.37.2）。直接调用依赖函数更轻，
也避免单测去拉起 api.server 的 lifespan（那个会起 Patroller / 扫 docker）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException, Request

from api.security import deps
from api.security.deps import ANONYMOUS, AuthContext, require_scope, resolve_auth
from audit.store import SqliteEventStore


# ============================================================
# fixtures / helpers
# ============================================================

@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch: pytest.MonkeyPatch):
    """默认关掉 dev 绕过，避免用例互相污染。"""
    monkeypatch.delenv("AGENTOPS_AUTH_DISABLED", raising=False)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "audit.security_schema.bootstrap_first_user", lambda conn: None
    )
    from api import server as server_mod

    s = SqliteEventStore(str(tmp_path / "audit.db"))
    # deps.get_store() 从 api.server 模块属性上取，这里手动注入
    monkeypatch.setattr(server_mod, "_event_store", s)
    return s


def _req(
    path: str = "/api/runs/start",
    headers: dict[str, str] | None = None,
    host: str = "1.2.3.4",
) -> Request:
    """构造最小 Request：依赖里只用 request.headers / request.client / request.url.path。"""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": (host, 54321),
            "server": ("testserver", 80),
        }
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def run(coro):
    return asyncio.run(coro)


def resolve(path="/api/runs/start", headers=None) -> AuthContext:
    return run(resolve_auth(_req(path, headers), (headers or {}).get("Authorization")))


def check_scope(scope: str, path: str = "/api/runs/start", headers=None) -> AuthContext:
    """走完整的 current_user + require_scope 链路（匿名 → 401 在这里抛）。"""
    checker = require_scope(scope)

    async def _go():
        auth = await deps.current_user(
            await resolve_auth(_req(path, headers), (headers or {}).get("Authorization"))
        )
        return await checker(_req(path, headers), auth)

    return run(_go())


def make_user(store, user_id="u1", username="alice", roles=(), *, keep_must_reset=False):
    """建用户 + 绑角色 + 开一个 session。

    ``create_user`` 默认 ``must_reset_password=1``（新建账号首次登录强制改密），
    所以除专门测该行为的用例外，这里都先改一次密码把标记清掉。
    """
    async def _go():
        await store.create_user(user_id, username, f"pwd-{username}-123")
        if not keep_must_reset:
            await store.set_user_password(user_id, "already-changed-123")
        for r in roles:
            await store.bind_user_role(user_id, r)
        scopes = await store.compute_user_scopes(user_id)
        return await store.create_auth_session(user_id, scope=scopes)

    return run(_go())


# ============================================================
# 匿名路径
# ============================================================

def test_anonymous_when_no_header(store):
    ctx = resolve(headers=None)
    assert ctx.is_anonymous
    assert ctx.user_id == ""
    assert ctx.token_kind == "anon"


def test_anonymous_when_header_not_bearer(store):
    assert resolve(headers={"Authorization": "Basic abc"}).is_anonymous


def test_anonymous_when_bearer_empty(store):
    assert resolve(headers=bearer("")).is_anonymous


def test_current_user_rejects_anonymous_with_401(store):
    async def _go():
        return await deps.current_user(await resolve_auth(_req(), None))

    with pytest.raises(HTTPException) as exc:
        run(_go())
    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"] == "Bearer"


def test_unsupported_token_format(store):
    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer("xxx_unknown_prefix"))
    assert exc.value.status_code == 401
    assert exc.value.detail == "unsupported_token_format"


# ============================================================
# Session 路径
# ============================================================

def test_session_ok_and_kind(store):
    raw, sess = make_user(store, roles=("role_developer",))
    assert "runs.write" in sess["scope"], "developer 角色应有 runs.write"

    ctx = resolve(headers=bearer(raw))
    assert ctx.token_kind == "session"
    assert ctx.user_id == "u1"
    assert ctx.session_id == sess["session_id"]
    assert "runs.write" in ctx.scopes


def test_session_revoked_401(store):
    raw, sess = make_user(store)
    run(store.revoke_auth_session(sess["session_id"], reason="logout"))

    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer(raw))
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid_or_revoked_session"


def test_session_sliding_expired_401(store):
    """滑动过期到点 → 401（即便绝对过期还早）。"""
    raw, sess = make_user(store)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE security_auth_sessions SET sliding_expires_at = ? WHERE session_id = ?",
        (past, sess["session_id"]),
    )
    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer(raw))
    assert exc.value.detail == "session_expired"


def test_session_absolute_expired_401(store):
    raw, sess = make_user(store)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE security_auth_sessions SET absolute_expires_at = ? WHERE session_id = ?",
        (past, sess["session_id"]),
    )
    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer(raw))
    assert exc.value.detail == "session_expired"


def test_session_sliding_auto_renew(store):
    """剩余不足 1 天时自动续期到 +7 天。"""
    raw, sess = make_user(store)
    soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    store._conn.execute(
        "UPDATE security_auth_sessions SET sliding_expires_at = ? WHERE session_id = ?",
        (soon, sess["session_id"]),
    )

    assert resolve(headers=bearer(raw)).user_id == "u1"

    row = run(store.get_auth_session(sess["session_id"]))
    remaining = (
        datetime.fromisoformat(row["sliding_expires_at"]) - datetime.now(timezone.utc)
    ).total_seconds() / 86400
    assert remaining > 6, f"续期后应约 7 天，实际 {remaining:.2f} 天"


def test_session_touch_is_throttled_not_every_request(store):
    """每请求都写 last_used_at 会给 SQLite 造成写压力，30s 节流。"""
    raw, sess = make_user(store)
    resolve(headers=bearer(raw))       # 首次写
    row = run(store.get_auth_session(sess["session_id"]))
    first_touch = row["last_used_at"]

    resolve(headers=bearer(raw))       # 30s 内 → 节流，不写
    row = run(store.get_auth_session(sess["session_id"]))
    assert row["last_used_at"] == first_touch


def test_disabled_user_session_already_revoked_so_401(store):
    """禁用会级联吊销 session（方案 §4.4），所以真实结果先撞上 401 而不是 403。"""
    raw, _ = make_user(store)
    run(store.set_user_disabled("u1", True, reason="offboard"))

    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer(raw))
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid_or_revoked_session"


def test_disabled_user_403_when_credential_survives(store):
    """403 user_disabled 是纵深防御：覆盖"凭证尚有效但用户已被禁"的竞态。

    正常路径下禁用会级联吊销，走不到这里；直接改 users 表模拟
    "session 还没被吊销 / 吊销未生效" 的窗口。
    """
    raw, _ = make_user(store)
    now_iso = datetime.now(timezone.utc).isoformat()
    store._conn.execute(
        "UPDATE users SET disabled_at = ?, updated_at = ? WHERE user_id = ?",
        (now_iso, now_iso, "u1"),
    )

    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer(raw))
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "user_disabled"


# ============================================================
# PAT 路径
# ============================================================

def test_pat_ok(store):
    make_user(store)
    raw, tok = run(store.create_api_token("u1", "ci", scopes="runs.write"))

    ctx = resolve(headers=bearer(raw))
    assert ctx.token_kind == "pat"
    assert ctx.token_id == tok["token_id"]
    assert "runs.write" in ctx.scopes


def test_pat_expired_401(store):
    make_user(store)
    raw, tok = run(store.create_api_token("u1", "ci", expires_in_days=30))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE security_api_tokens SET expires_at = ? WHERE token_id = ?",
        (past, tok["token_id"]),
    )
    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer(raw))
    assert exc.value.detail == "token_expired"


def test_pat_revoked_401(store):
    make_user(store)
    raw, tok = run(store.create_api_token("u1", "ci", expires_in_days=30))
    run(store.revoke_api_token(tok["token_id"], reason="leaked"))

    with pytest.raises(HTTPException) as exc:
        resolve(headers=bearer(raw))
    assert exc.value.detail == "invalid_token"


def test_pat_scope_bound_to_token_not_user(store):
    """PAT 权限以 token 自带 scopes 为准，不继承用户角色（最小权限）。"""
    make_user(store, roles=("role_owner",))          # 用户权限很大
    raw, _ = run(store.create_api_token("u1", "ci", scopes="runs.read"))  # 只签发读

    with pytest.raises(HTTPException) as exc:
        check_scope("runs.write", headers=bearer(raw))
    assert exc.value.status_code == 403
    assert exc.value.detail["missing_scope"] == "runs.write"


# ============================================================
# require_scope
# ============================================================

def test_require_scope_401_for_anon(store):
    with pytest.raises(HTTPException) as exc:
        check_scope("runs.write")          # 无 token
    assert exc.value.status_code == 401, "没登录应先 401 而不是 403"


def test_require_scope_403_when_missing(store):
    raw, _ = make_user(store, roles=("role_viewer",))   # viewer 没有 runs.write
    with pytest.raises(HTTPException) as exc:
        check_scope("runs.write", headers=bearer(raw))
    assert exc.value.status_code == 403
    assert exc.value.detail["missing_scope"] == "runs.write"


def test_require_scope_ok(store):
    raw, _ = make_user(store, roles=("role_developer",))
    assert check_scope("runs.write", headers=bearer(raw)).user_id == "u1"


# ============================================================
# must_reset_password 强制改密
# ============================================================

def test_must_reset_blocks_business_api(store):
    raw, _ = make_user(store, roles=("role_owner",), keep_must_reset=True)
    with pytest.raises(HTTPException) as exc:
        check_scope("runs.write", headers=bearer(raw))
    detail = exc.value.detail
    assert detail["error"] == "must_reset_password"
    assert detail["redirect"] == "/account/change-password"


def test_must_reset_allows_whitelisted_paths(store):
    """改密 + /api/auth/me 必须放行，否则前端拿不到改密入口 → 死锁。"""
    raw, _ = make_user(store, roles=("role_owner",), keep_must_reset=True)
    headers = bearer(raw)
    for path in ("/api/auth/change-password", "/api/auth/logout", "/api/auth/me"):
        assert check_scope("security.users.read", path=path, headers=headers)


def test_must_reset_still_blocks_other_paths(store):
    raw, _ = make_user(store, roles=("role_owner",), keep_must_reset=True)
    with pytest.raises(HTTPException) as exc:
        check_scope("security.users.read", path="/api/auth/me/sessions", headers=bearer(raw))
    assert exc.value.detail["error"] == "must_reset_password"


def test_must_reset_cleared_after_password_change(store):
    raw, _ = make_user(store, roles=("role_owner",), keep_must_reset=True)
    run(store.set_user_password("u1", "brand-new-pass-9"))
    assert check_scope("runs.write", headers=bearer(raw)).user_id == "u1"


def test_path_allows_must_reset_prefix_logic():
    assert deps._path_allows_must_reset("/api/auth/me")
    assert deps._path_allows_must_reset("/api/auth/me/")
    assert deps._path_allows_must_reset("/api/auth/logout")
    assert deps._path_allows_must_reset("/api/auth/logout-all")
    assert deps._path_allows_must_reset("/api/auth/change-password")
    assert not deps._path_allows_must_reset("/api/runs/start")
    assert not deps._path_allows_must_reset("/api/auth/me/sessions")


# ============================================================
# dev 绕过（双条件）
# ============================================================

def test_bypass_needs_both_env_and_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(deps, "_AUTH_BYPASS_MARKER", tmp_path / "nope")

    monkeypatch.setenv("AGENTOPS_AUTH_DISABLED", "1")
    assert deps.auth_bypass_enabled() is False, "只有环境变量，缺标记文件 → 不开"

    marker = tmp_path / ".auth-disabled"
    marker.write_text("dev", encoding="utf-8")
    monkeypatch.setattr(deps, "_AUTH_BYPASS_MARKER", marker)
    assert deps.auth_bypass_enabled() is True

    monkeypatch.setenv("AGENTOPS_AUTH_DISABLED", "0")
    assert deps.auth_bypass_enabled() is False, "环境变量不是 1 → 不开"


def test_bypass_grants_all_scopes_without_token(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    marker = tmp_path / ".auth-disabled"
    marker.write_text("dev", encoding="utf-8")
    monkeypatch.setattr(deps, "_AUTH_BYPASS_MARKER", marker)
    monkeypatch.setenv("AGENTOPS_AUTH_DISABLED", "1")

    ctx = resolve(headers=None)
    assert ctx.token_kind == "bypass"
    assert ctx.has("anything.at.all")
    assert check_scope("runs.write") is not None


# ============================================================
# AuthContext
# ============================================================

def test_auth_context_has_and_require():
    ctx = AuthContext(
        user={"user_id": "u1"}, scopes=frozenset({"runs.read"}), token_kind="session"
    )
    assert ctx.has("runs.read")
    assert not ctx.has("runs.write")
    assert ctx.user_id == "u1"
    assert not ctx.is_anonymous
    assert not ctx.must_reset_password

    with pytest.raises(HTTPException) as exc:
        ctx.require("runs.write")
    assert exc.value.status_code == 403


def test_auth_context_wildcard_scope():
    ctx = AuthContext(user={"user_id": "dev"}, scopes=frozenset({deps.SCOPE_ALL}))
    assert ctx.has("anything.at.all")


def test_anon_context():
    assert ANONYMOUS.is_anonymous
    assert ANONYMOUS.user_id == ""
    assert not ANONYMOUS.has("runs.read")
