"""S9/S10/S11：`/api/security/roles` + `/api/security/api-tokens` + `/api/security/sessions` 测试。

同样不用 TestClient（httpx 0.28 与 starlette 0.35 不兼容），直接 await handler。

重点验证三处安全约束：
    1. PAT 的 scopes 不能超出签发者自身权限（否则 viewer 能签出 runs.write）
    2. PAT 强制过期档位（30/90/365），不接受永不过期
    3. token_hash / session token_hash 永不出网
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from api.security.api_tokens import (
    ALLOWED_EXPIRY_DAYS,
    CreateTokenRequest,
    RotateTokenRequest,
    create_token,
    list_tokens,
    revoke_token,
    rotate_token,
)
from api.security.deps import AuthContext
from api.security.roles import list_permissions, list_roles
from api.security.sessions import list_sessions, revoke_session
from audit.store import SqliteEventStore

OWNER = "role_owner"
ADMIN = "role_admin"
DEVELOPER = "role_developer"
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


def make(store, user_id="u1", username="alice", roles=(), pwd="pwd-alice-123"):
    async def _go():
        await store.create_user(user_id, username, pwd)
        await store.set_user_password(user_id, pwd)
        for r in roles:
            await store.bind_user_role(user_id, r)
        return await store.compute_user_scopes(user_id)

    scopes = run(_go())
    return AuthContext(
        user={"user_id": user_id, "username": username},
        scopes=frozenset(scopes.split()),
        token_kind="session",
    )


def add_session(store, user_id, ip="1.2.3.4"):
    async def _go():
        scopes = await store.compute_user_scopes(user_id)
        raw, sess = await store.create_auth_session(user_id, scope=scopes, ip=ip)
        return raw, sess

    return run(_go())


# ============================================================
# S9 · 角色
# ============================================================

def test_list_roles_returns_four_builtin_roles(store):
    auth = make(store, "u1", "alice", (VIEWER,))
    out = run(list_roles(auth=auth))
    assert out["total"] == 4
    assert {r["role_id"] for r in out["roles"]} == {
        OWNER, ADMIN, DEVELOPER, VIEWER
    }


def test_list_roles_permission_counts(store):
    """与种子数据对齐：owner 46 / admin 45 / developer 35 / viewer 22（v93 后）。"""
    auth = make(store, "u1", "alice", (VIEWER,))
    out = run(list_roles(auth=auth))
    counts = {r["role_id"]: r["permission_count"] for r in out["roles"]}

    assert counts[OWNER] == 46
    assert counts[ADMIN] == 45, "admin 只缺 security.roles.write"
    assert counts[DEVELOPER] == 35
    assert counts[VIEWER] == 22, "viewer = 全部 action=read 的权限"


def test_owner_role_not_assignable(store):
    """owner 不能被 UI 列进可绑定下拉，否则会出现多个 owner 互相解绑的僵局。"""
    auth = make(store, "u1", "alice", (VIEWER,))
    out = run(list_roles(auth=auth))
    flags = {r["role_id"]: r["is_assignable"] for r in out["roles"]}
    assert flags[OWNER] is False
    assert all(flags[r] for r in (ADMIN, DEVELOPER, VIEWER))


def test_list_permissions_maps_roles_back(store):
    auth = make(store, "u1", "alice", (VIEWER,))
    out = run(list_permissions(auth=auth))
    assert out["total"] == 46

    by_id = {p["perm_id"]: p for p in out["permissions"]}
    assert OWNER in by_id["security.roles.write"]["roles"]
    assert ADMIN not in by_id["security.roles.write"]["roles"]
    assert VIEWER in by_id["runs.read"]["roles"]
    assert VIEWER not in by_id["runs.write"]["roles"]


def test_roles_require_read_scope(store):
    from api.security.deps import require_scope

    checker = require_scope("security.roles.read")
    # 无角色用户：scopes 为空
    no_roles = AuthContext(
        user={"user_id": "u1", "username": "alice"},
        scopes=frozenset(), token_kind="session",
    )

    async def _go():
        await checker(request=None, auth=no_roles)

    with pytest.raises(HTTPException) as exc:
        run(_go())
    assert exc.value.status_code == 403
    assert exc.value.detail["missing_scope"] == "security.roles.read"


# ============================================================
# S10 · API 令牌
# ============================================================

def test_create_token_returns_plaintext_once(store):
    auth = make(store, "u1", "alice", (DEVELOPER,))
    out = run(create_token(CreateTokenRequest(name="ci"), auth=auth))

    raw = out["token"]
    assert raw.startswith("pat_")
    assert len(raw) == 4 + 12 + 22          # pat_ + prefix(12) + secret(22)
    assert "warning" in out

    # 明文能验通过，且库里只有 hash
    assert run(store.verify_api_token(raw)) is not None
    row = out["token_row"]
    assert "token_hash" not in row
    assert row["last4"] == raw[-4:]


def test_create_token_inherits_own_scopes_by_default(store):
    auth = make(store, "u1", "alice", (DEVELOPER,))
    out = run(create_token(CreateTokenRequest(name="ci"), auth=auth))
    assert set(out["token_row"]["scopes"]) == set(auth.scopes)


def test_create_token_rejects_scope_escalation(store):
    """核心约束：不能签出超出自身权限的 token。"""
    auth = make(store, "u1", "alice", (VIEWER,))
    assert "runs.write" not in auth.scopes

    with pytest.raises(HTTPException) as exc:
        run(create_token(
            CreateTokenRequest(name="evil", scopes=["runs.write"]), auth=auth
        ))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "scope_not_granted"
    assert "runs.write" in exc.value.detail["not_granted"]


def test_create_token_allows_scope_subset(store):
    auth = make(store, "u1", "alice", (DEVELOPER,))
    out = run(create_token(
        CreateTokenRequest(name="readonly", scopes=["runs.read"]), auth=auth
    ))
    assert out["token_row"]["scopes"] == ["runs.read"]

    # 签出来的 token 真的只有这个 scope
    row = run(store.verify_api_token(out["token"]))
    assert row["scopes"] == "runs.read"


def test_create_token_rejects_arbitrary_expiry(store):
    auth = make(store, "u1", "alice", (DEVELOPER,))
    for bad in (0, 1, 7, 9999):
        with pytest.raises(HTTPException) as exc:
            run(create_token(
                CreateTokenRequest(name="ci", expires_in_days=bad), auth=auth
            ))
        assert exc.value.detail["error"] == "invalid_expiry"
    assert ALLOWED_EXPIRY_DAYS == (30, 90, 365)


def test_create_token_accepts_each_allowed_expiry(store):
    auth = make(store, "u1", "alice", (DEVELOPER,))
    for days in ALLOWED_EXPIRY_DAYS:
        out = run(create_token(
            CreateTokenRequest(name=f"ci-{days}", expires_in_days=days), auth=auth
        ))
        expires = datetime.fromisoformat(out["token_row"]["expires_at"])
        delta = expires - datetime.now(timezone.utc)
        assert timedelta(days=days - 1) < delta <= timedelta(days=days + 1)


def test_create_token_for_other_user_requires_write_scope(store):
    dev = make(store, "u1", "alice", (DEVELOPER,))
    make(store, "u2", "bob", (VIEWER,))

    # developer 有 security.api_tokens.write → 可以给 bob 建
    out = run(create_token(
        CreateTokenRequest(name="for-bob", user_id="u2", scopes=["runs.read"]),
        auth=dev,
    ))
    assert out["token_row"]["user_id"] == "u2"

    # 给别人建必须显式给 scopes，不能静默继承
    with pytest.raises(HTTPException) as exc:
        run(create_token(
            CreateTokenRequest(name="for-bob", user_id="u2", scopes=[]), auth=dev
        ))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "scopes_required"

    # viewer 没有 write scope → 403
    viewer = make(store, "u3", "carol", (VIEWER,))
    with pytest.raises(HTTPException) as exc:
        run(create_token(
            CreateTokenRequest(name="for-bob", user_id="u2", scopes=["runs.read"]),
            auth=viewer,
        ))
    assert exc.value.status_code == 403


def test_create_token_for_other_cannot_exceed_target_scopes(store):
    """给别人签的 PAT 不能超出**目标用户**自己的权限。

    只校验调用方是不够的：admin 持有除 security.roles.write 外的全部权限，
    可以给 viewer 签一个 runs.write 的 PAT，等于绕过「admin 不能改别人角色」
    这条防提权设计。
    """
    admin = make(store, "u1", "alice", (ADMIN,))
    make(store, "u2", "bob", (VIEWER,))
    assert "runs.write" in admin.scopes
    assert "runs.write" not in frozenset(
        run(store.compute_user_scopes("u2")).split()
    )

    with pytest.raises(HTTPException) as exc:
        run(create_token(
            CreateTokenRequest(name="escalate", user_id="u2",
                               scopes=["runs.write"]),
            auth=admin,
        ))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "scope_not_granted"

    # 目标用户自己也有的权限就可以签
    out = run(create_token(
        CreateTokenRequest(name="ok", user_id="u2", scopes=["runs.read"]),
        auth=admin,
    ))
    assert out["token_row"]["scopes"] == ["runs.read"]


def test_list_own_tokens_without_read_scope(store):
    """developer 有 .write 但没有 .read，仍能列自己的（自服务路径）。"""
    dev = make(store, "u1", "alice", (DEVELOPER,))
    assert "security.api_tokens.read" not in dev.scopes

    run(create_token(CreateTokenRequest(name="t1"), auth=dev))
    run(create_token(CreateTokenRequest(name="t2"), auth=dev))

    out = run(list_tokens(auth=dev))
    assert out["total"] == 2
    assert all("token_hash" not in t for t in out["tokens"])


def test_list_other_tokens_requires_read_scope(store):
    make(store, "u1", "alice", (DEVELOPER,))
    make(store, "u2", "bob", (VIEWER,))
    dev = AuthContext(
        user={"user_id": "u1", "username": "alice"},
        scopes=frozenset(run(store.compute_user_scopes("u1")).split()),
        token_kind="session",
    )
    with pytest.raises(HTTPException) as exc:
        run(list_tokens(user_id="u2", auth=dev))
    assert exc.value.status_code == 403


def test_revoke_own_token(store):
    dev = make(store, "u1", "alice", (DEVELOPER,))
    raw, row = run(store.create_api_token("u1", "ci", scopes="runs.read"))
    tid = row["token_id"]      # 撤销后 verify 会返回 None，id 要提前留下

    out = run(revoke_token(tid, auth=dev))
    assert out["revoked"] is True
    assert run(store.verify_api_token(raw)) is None, "撤销后明文立刻失效"

    with pytest.raises(HTTPException) as exc:
        run(revoke_token(tid, auth=dev))
    assert exc.value.status_code == 409


def test_cannot_revoke_others_token_without_write_scope(store):
    make(store, "u1", "alice", (DEVELOPER,))
    make(store, "u2", "bob", (VIEWER,))
    raw, row = run(store.create_api_token("u1", "ci", scopes="runs.read"))

    viewer = make(store, "u3", "carol", (VIEWER,))
    with pytest.raises(HTTPException) as exc:
        run(revoke_token(row["token_id"], auth=viewer))
    assert exc.value.status_code == 403


def test_rotate_keeps_scopes_and_remaining_lifetime(store):
    dev = make(store, "u1", "alice", (DEVELOPER,))
    out1 = run(create_token(
        CreateTokenRequest(name="ci", scopes=["runs.read"], expires_in_days=90),
        auth=dev,
    ))
    old_id = out1["token_row"]["token_id"]
    old_exp = datetime.fromisoformat(out1["token_row"]["expires_at"])

    out2 = run(rotate_token(old_id, RotateTokenRequest(), auth=dev))
    assert out2["rotated_from"] == old_id
    assert out2["token_row"]["scopes"] == ["runs.read"], "scopes 必须沿用"

    new_exp = datetime.fromisoformat(out2["token_row"]["expires_at"])
    # 按剩余时长续，不能因为轮换就重置成完整的 90 天
    assert new_exp <= old_exp + timedelta(minutes=1)
    assert new_exp > old_exp - timedelta(days=1)

    # 新明文可用，旧明文立即失效
    assert run(store.verify_api_token(out2["token"])) is not None
    assert run(store.verify_api_token(out1["token"])) is None


def test_rotate_revoked_token_conflicts(store):
    dev = make(store, "u1", "alice", (DEVELOPER,))
    out = run(create_token(CreateTokenRequest(name="ci"), auth=dev))
    tid = out["token_row"]["token_id"]
    run(revoke_token(tid, auth=dev))

    with pytest.raises(HTTPException) as exc:
        run(rotate_token(tid, RotateTokenRequest(), auth=dev))
    assert exc.value.status_code == 409
    assert exc.value.detail == "cannot_rotate_revoked"


def test_rotate_can_rename(store):
    dev = make(store, "u1", "alice", (DEVELOPER,))
    out = run(create_token(CreateTokenRequest(name="old-name"), auth=dev))
    tid = out["token_row"]["token_id"]

    out2 = run(rotate_token(tid, RotateTokenRequest(name="new-name"), auth=dev))
    assert out2["token_row"]["name"] == "new-name"


def test_revoke_missing_token_404(store):
    dev = make(store, "u1", "alice", (DEVELOPER,))
    with pytest.raises(HTTPException) as exc:
        run(revoke_token("tok_nope", auth=dev))
    assert exc.value.status_code == 404


# ============================================================
# S11 · 登录会话
# ============================================================

def test_list_own_sessions_without_scope(store):
    make(store, "u1", "alice", (VIEWER,))
    _, s1 = add_session(store, "u1")
    add_session(store, "u1", ip="5.5.5.5")

    auth = AuthContext(
        user={"user_id": "u1", "username": "alice"},
        scopes=frozenset(run(store.compute_user_scopes("u1")).split()),
        token_kind="session",
        session_id=s1["session_id"],
    )
    out = run(list_sessions(auth=auth))
    assert out["total"] == 2
    assert all("token_hash" not in s for s in out["sessions"])

    current = [s for s in out["sessions"] if s["is_current"]]
    assert len(current) == 1
    assert current[0]["session_id"] == s1["session_id"]
    assert out["sessions"][0]["is_current"] is True, "当前 session 必须置顶"


def test_list_other_sessions_requires_read_scope(store):
    make(store, "u2", "bob", (VIEWER,))
    # 注意：viewer 有 security.sessions.read（viewer = 全部 read 权限），
    # 无权方要用 developer——它只有 security.api_tokens.write，没有 sessions.read
    dev = make(store, "u1", "alice", (DEVELOPER,))
    assert "security.sessions.read" not in dev.scopes

    with pytest.raises(HTTPException) as exc:
        run(list_sessions(user_id="u2", auth=dev))
    assert exc.value.status_code == 403

    # 带 scope 的 admin 可以
    admin = make(store, "u4", "dave", (ADMIN,))
    assert "security.sessions.read" in admin.scopes
    out = run(list_sessions(user_id="u2", auth=admin))
    assert all(s["user_id"] == "u2" for s in out["sessions"])


def test_revoke_own_session(store):
    make(store, "u1", "alice", (VIEWER,))
    _, s1 = add_session(store, "u1")
    _, s2 = add_session(store, "u1", ip="5.5.5.5")

    auth = AuthContext(
        user={"user_id": "u1", "username": "alice"},
        scopes=frozenset(run(store.compute_user_scopes("u1")).split()),
        token_kind="session",
        session_id=s1["session_id"],
    )
    out = run(revoke_session(s2["session_id"], auth=auth))
    assert out["revoked"] is True
    assert len(run(store.list_auth_sessions("u1"))) == 1

    with pytest.raises(HTTPException) as exc:
        run(revoke_session(s2["session_id"], auth=auth))
    assert exc.value.status_code == 409


def test_cannot_revoke_others_session(store):
    make(store, "u1", "alice", (VIEWER,))
    _, s1 = add_session(store, "u1")
    admin = make(store, "u2", "bob", (ADMIN,))

    # 即便 admin 有 security.sessions.read，MVP 的写只放行 self
    with pytest.raises(HTTPException) as exc:
        run(revoke_session(s1["session_id"], auth=admin))
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "not_your_session"


def test_revoke_missing_session_404(store):
    auth = make(store, "u1", "alice", (VIEWER,))
    with pytest.raises(HTTPException) as exc:
        run(revoke_session("nonexistent", auth=auth))
    assert exc.value.status_code == 404
