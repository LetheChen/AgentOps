"""S8 `/api/security/users` 端点测试。

沿用 S7 的做法：**不用 TestClient**（httpx 0.28 与 starlette 0.35 的
TestClient 不兼容），直接 await 路由 handler，依赖解析的结果手动构造。

重点覆盖方案里没写但实现时补上的三处护栏：
    1. 唯一活跃 owner 不能被禁用 / 软删（DB 触发器只拦硬 DELETE）
    2. 不能对自己做破坏性操作
    3. lock 只挡新登录，不动已有凭证
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from api.security import users as U
from api.security.deps import AuthContext
from api.security.users import (
    CreateUserRequest,
    PasswordRequest,
    RoleBindingRequest,
    UpdateUserRequest,
    bind_role,
    create_user,
    delete_user,
    list_users,
    lock_user,
    reset_password,
    revoke_all,
    unbind_role,
    unlock_user,
    update_user,
)
from audit.store import SqliteEventStore

OWNER = "role_owner"
ADMIN = "role_admin"
VIEWER = "role_viewer"


@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AGENTOPS_AUTH_DISABLED", raising=False)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "audit.security_schema.bootstrap_first_user", lambda conn: None
    )
    from api import server as server_mod

    s = SqliteEventStore(str(tmp_path / "audit.db"))
    monkeypatch.setattr(server_mod, "_event_store", s)
    return s


def run(coro):
    return asyncio.run(coro)


def ctx(user_id: str, scopes: tuple[str, ...] = ()) -> AuthContext:
    return AuthContext(
        user={"user_id": user_id, "username": user_id},
        scopes=frozenset(scopes),
        token_kind="session",
    )


def make(store, user_id="u1", username="alice", roles=(), pwd="pwd-alice-123"):
    """建用户（清掉 must_reset）+ 绑角色。返回构造好的 AuthContext。"""

    async def _go():
        await store.create_user(user_id, username, pwd)
        await store.set_user_password(user_id, pwd)   # 清掉 must_reset_password
        for r in roles:
            await store.bind_user_role(user_id, r)
        return await store.compute_user_scopes(user_id)

    scopes = run(_go())
    return ctx(user_id, tuple(scopes.split()))


# ============================================================
# 列表
# ============================================================

def test_list_users_includes_roles_and_flags(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))

    out = run(list_users(auth=ctx("u1", ("security.users.read",))))
    assert out["total"] == 2
    by_id = {u["user_id"]: u for u in out["users"]}

    assert by_id["u1"]["roles"] == [ADMIN]
    assert by_id["u1"]["disabled"] is False
    assert by_id["u1"]["locked"] is False
    # 脱敏：任何返回用户的接口都不能带 password_hash
    assert "password_hash" not in by_id["u1"]


def test_list_users_shows_disabled_and_locked(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.read",))

    run(store.set_user_disabled("u2", True, reason="test"))
    run(store.lock_user_login("bob", lock_sec=600))

    out = run(list_users(auth=admin))
    by_id = {u["user_id"]: u for u in out["users"]}
    # 禁用的也要列出来（include_disabled=True），否则运维看不到历史账号
    assert by_id["u2"]["disabled"] is True
    assert by_id["u2"]["locked"] is True


# ============================================================
# 创建
# ============================================================

def test_create_user_returns_201_shape_and_must_reset(store):
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    out = run(create_user(
        CreateUserRequest(username="carol", password="carol-pass-1",
                          display_name="Carol", role_ids=[VIEWER]),
        auth=admin,
    ))
    assert out["user_id"] == "user_carol"
    assert out["username"] == "carol"
    assert out["roles"] == [VIEWER]
    assert out["must_reset_password"] == 1, "新建账号必须强制首次改密"
    assert "password_hash" not in out


def test_create_user_rejects_duplicate_username(store):
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    run(create_user(CreateUserRequest(username="carol", password="carol-pass-1"),
                    auth=admin))
    with pytest.raises(HTTPException) as exc:
        run(create_user(CreateUserRequest(username="carol", password="carol-pass-1"),
                        auth=admin))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "username_taken"


def test_create_user_rejects_bad_username(store):
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(create_user(
            CreateUserRequest(username="bad name!", password="carol-pass-1"),
            auth=admin,
        ))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "invalid_username"


def test_create_user_cannot_grant_owner(store):
    """admin 缺 security.roles.write 走不了 /roles，但不能从建用户接口绕过去。"""
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(create_user(
            CreateUserRequest(username="carol", password="carol-pass-1",
                              role_ids=[OWNER]),
            auth=admin,
        ))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "owner_role_not_grantable"


def test_create_user_rejects_unknown_role(store):
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(create_user(
            CreateUserRequest(username="carol", password="carol-pass-1",
                              role_ids=["role_superuser"]),
            auth=admin,
        ))
    assert exc.value.detail["error"] == "unknown_role"


def test_create_user_enforces_min_password_length():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CreateUserRequest(username="carol", password="short")


# ============================================================
# 改资料 / 启停
# ============================================================

def test_update_user_profile(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    out = run(update_user(
        "u2", UpdateUserRequest(display_name="Bob L", email="bob@example.com"),
        auth=admin,
    ))
    assert out["display_name"] == "Bob L"
    assert out["email"] == "bob@example.com"


def test_disable_user_cascades_revocation(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    add_session(store, "u2")
    run(store.create_api_token("u2", "ci", scopes="runs.read"))
    assert len(run(store.list_auth_sessions("u2"))) == 1
    assert len(run(store.list_api_tokens("u2"))) == 1

    out = run(update_user("u2", UpdateUserRequest(disabled=True), auth=admin))
    assert out["disabled"] is True or out["disabled_at"] is not None

    assert run(store.list_auth_sessions("u2")) == [], "禁用必须吊销 session"
    assert run(store.list_api_tokens("u2")) == [], "禁用必须吊销 PAT"


def test_cannot_disable_self(store):
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(update_user("u1", UpdateUserRequest(disabled=True), auth=admin))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "cannot_modify_self"


def test_cannot_disable_last_active_owner(store):
    """护栏 #1：DB 触发器只拦硬 DELETE，软删/禁用必须在 API 层拦。

    不拦的话一条 PATCH 就能让整个安全模块无人可管。
    注意要用**别人**的身份操作——用 owner 自己会先撞上 cannot_modify_self。
    """
    make(store, "u1", "alice", (OWNER,))
    make(store, "u2", "bob", (ADMIN,))
    actor = ctx("u2", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(update_user("u1", UpdateUserRequest(disabled=True), auth=actor))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "last_owner_protected"

    # 再给 u2 也挂上 owner（绕过 API 护栏，直连数据层）之后就允许了
    run(store.bind_user_role("u2", OWNER, granted_by="u1"))  # granted_by 有 FK 指向 users
    out = run(update_user("u1", UpdateUserRequest(disabled=True), auth=actor))
    assert out["disabled_at"] is not None


def test_disable_already_disabled_conflicts(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    run(update_user("u2", UpdateUserRequest(disabled=True), auth=admin))
    with pytest.raises(HTTPException) as exc:
        run(update_user("u2", UpdateUserRequest(disabled=True), auth=admin))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "already_disabled"


def test_reenable_non_disabled_conflicts(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(update_user("u2", UpdateUserRequest(disabled=False), auth=admin))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "not_disabled"


# ============================================================
# 软删
# ============================================================

def add_session(store, user_id, ip="1.2.3.4"):
    async def _go():
        scopes = await store.compute_user_scopes(user_id)
        return await store.create_auth_session(user_id, scope=scopes, ip=ip)

    return run(_go())


def test_soft_delete_disables_and_revokes_but_keeps_row(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    out = run(delete_user("u2", auth=admin))
    assert out["deleted"] is True
    assert out["soft"] is True

    row = run(store.get_user("u2"))
    assert row is not None, "软删不能真删行——users 表被业务数据外键引用"
    assert row["disabled_at"] is not None


def test_cannot_soft_delete_self_or_last_owner(store):
    make(store, "u1", "alice", (OWNER,))
    make(store, "u2", "bob", (ADMIN,))

    with pytest.raises(HTTPException) as exc:
        run(delete_user("u2", auth=ctx("u2", ("security.users.write",))))
    assert exc.value.detail["error"] == "cannot_modify_self"

    with pytest.raises(HTTPException) as exc:
        run(delete_user("u1", auth=ctx("u2", ("security.users.write",))))
    assert exc.value.detail["error"] == "last_owner_protected"


# ============================================================
# 锁定 / 解锁
# ============================================================

def test_lock_blocks_new_login_but_keeps_existing_credentials(store):
    """护栏 #3 的语义：lock 只挡新登录，不吊销已有凭证。"""
    from api.security.rate_limit import check_login_rate_limit

    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    add_session(store, "u2")
    out = run(lock_user("u2", auth=admin))
    assert out["locked"] is True
    assert out["lock_sec"] == 30 * 60

    # 新登录被挡（走的是登录限流同一条路径）
    with pytest.raises(HTTPException) as exc:
        run(check_login_rate_limit(store, "9.9.9.9", "bob"))
    assert exc.value.status_code == 423

    # 已有 session 不受影响
    assert len(run(store.list_auth_sessions("u2"))) == 1


def test_lock_does_not_affect_other_users(store):
    from api.security.rate_limit import check_login_rate_limit

    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    run(lock_user("u2", auth=admin))
    run(check_login_rate_limit(store, "9.9.9.9", "alice"))   # 不抛即通过


def test_unlock_clears_lock(store):
    from api.security.rate_limit import check_login_rate_limit

    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    run(lock_user("u2", auth=admin))
    out = run(unlock_user("u2", auth=admin))
    assert out["had_lock"] is True

    run(check_login_rate_limit(store, "9.9.9.9", "bob"))     # 不抛即通过


def test_cannot_lock_self(store):
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(lock_user("u1", auth=admin))
    assert exc.value.detail["error"] == "cannot_modify_self"


# ============================================================
# 重置密码
# ============================================================

def test_reset_password_sets_must_reset_and_revokes_everything(store):
    from audit.security_schema import verify_password

    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    add_session(store, "u2")
    run(store.create_api_token("u2", "ci", scopes="runs.read"))

    out = run(reset_password("u2", PasswordRequest(new_password="brand-new-pwd"),
                             auth=admin))
    assert out["must_reset_password"] is True
    assert out["revoked_sessions"] == 1
    assert out["revoked_api_tokens"] == 1

    row = run(store.get_user("u2"))
    assert row["must_reset_password"] == 1
    assert verify_password(row["password_hash"], "brand-new-pwd")


def test_reset_password_on_missing_user_404(store):
    make(store, "u1", "alice", (ADMIN,))
    admin = ctx("u1", ("security.users.write",))

    with pytest.raises(HTTPException) as exc:
        run(reset_password("nope", PasswordRequest(new_password="brand-new-pwd"),
                           auth=admin))
    assert exc.value.status_code == 404


# ============================================================
# 角色绑定
# ============================================================

def test_bind_and_unbind_role(store):
    make(store, "u1", "alice", (OWNER,))
    make(store, "u2", "bob", (ADMIN,))
    owner = ctx("u1", ("security.roles.write",))

    out = run(bind_role("u2", RoleBindingRequest(role_id=VIEWER), auth=owner))
    assert set(out["roles"]) == {ADMIN, VIEWER}

    out = run(unbind_role("u2", VIEWER, auth=owner))
    assert out["roles"] == [ADMIN]
    assert "warning" in out, "降权要提示运维还有 revoke-all 这一步"


def test_bind_unknown_role_404(store):
    make(store, "u1", "alice", (OWNER,))
    owner = ctx("u1", ("security.roles.write",))

    with pytest.raises(HTTPException) as exc:
        run(bind_role("u1", RoleBindingRequest(role_id="role_nope"), auth=owner))
    assert exc.value.status_code == 404
    assert exc.value.detail == "role_not_found"


def test_bind_owner_rejected_with_readable_409(store):
    """DB 触发器也能拦，但抛的是裸 IntegrityError → 500。这里先拦掉给 409。"""
    make(store, "u1", "alice", (OWNER,))
    make(store, "u2", "bob", (ADMIN,))
    owner = ctx("u1", ("security.roles.write",))

    with pytest.raises(HTTPException) as exc:
        run(bind_role("u2", RoleBindingRequest(role_id=OWNER), auth=owner))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "owner_role_not_grantable"


def test_unbind_owner_rejected_before_trigger(store):
    make(store, "u1", "alice", (OWNER,))
    owner = ctx("u1", ("security.roles.write",))

    with pytest.raises(HTTPException) as exc:
        run(unbind_role("u1", OWNER, auth=owner))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "owner_role_protected"


def test_unbind_unbound_role_404(store):
    make(store, "u1", "alice", (OWNER,))
    make(store, "u2", "bob", (ADMIN,))
    owner = ctx("u1", ("security.roles.write",))

    with pytest.raises(HTTPException) as exc:
        run(unbind_role("u2", VIEWER, auth=owner))
    assert exc.value.status_code == 404


# ============================================================
# revoke-all（B10）
# ============================================================

def test_revoke_all_kills_sessions_and_tokens(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    add_session(store, "u2")
    add_session(store, "u2", ip="5.5.5.5")
    run(store.create_api_token("u2", "ci-1", scopes="runs.read"))
    run(store.create_api_token("u2", "ci-2", scopes="runs.read"))

    out = run(revoke_all("u2", auth=admin))
    assert out["revoked_sessions"] == 2
    assert out["revoked_api_tokens"] == 2
    assert run(store.list_auth_sessions("u2")) == []
    assert run(store.list_api_tokens("u2")) == []


def test_revoke_all_is_idempotent(store):
    make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    admin = ctx("u1", ("security.users.write",))

    assert run(revoke_all("u2", auth=admin))["revoked_sessions"] == 0
    assert run(revoke_all("u2", auth=admin))["revoked_sessions"] == 0


# ============================================================
# 权限
# ============================================================

def test_user_without_read_scope_is_rejected_by_dependency(store):
    """scope 校验发生在依赖层，handler 本身不检查——这里验证工厂确实会拒。"""
    from api.security.deps import require_scope

    checker = require_scope("security.users.read")

    async def _go():
        return await checker(
            request=None,
            auth=ctx("u1", ("some.other.scope",)),
        )

    with pytest.raises(HTTPException) as exc:
        run(_go())
    assert exc.value.status_code == 403
    assert exc.value.detail["missing_scope"] == "security.users.read"


def test_constant_owners_set_used_for_guard(store):
    """_active_owner_ids 只算**未禁用**的 owner，否则护栏会失效。"""
    make(store, "u1", "alice", (OWNER,))
    make(store, "u2", "bob", (OWNER,))
    run(store.set_user_disabled("u2", True, reason="test"))

    assert run(U._active_owner_ids(store)) == {"u1"}
