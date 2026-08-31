"""S7 `/api/auth/*` 端点测试。

**不用 TestClient**：`httpx 0.28.1` + `starlette 0.35.1` 下 `TestClient(app=...)`
直接报错（详见 docs §13.10）。这里直接 await 路由 handler，
把依赖解析的结果（``AuthContext``）作为关键字参数传进去。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException, Request, Response
from pydantic import ValidationError

from api.security.auth import (
    ChangePasswordRequest,
    LoginRequest,
    change_password,
    login,
    logout,
    logout_all,
    me,
    my_sessions,
)
from api.security.deps import AuthContext
from audit.store import SqliteEventStore


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


def _resp() -> Response:
    """S13 起 login/logout 直接函数调用需要传 Response（挂/清 session cookie）。"""
    return Response()


def _req(path="/api/auth/login", host="1.2.3.4", ua="pytest-agent") -> Request:
    headers = [(b"user-agent", ua.encode())] if ua else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": (host, 54321),
            "server": ("testserver", 80),
        }
    )


def run(coro):
    import asyncio

    return asyncio.run(coro)


def make_user(store, user_id="u1", username="alice", pwd="pwd-alice-123", roles=(),
              *, keep_must_reset=False):
    async def _go():
        await store.create_user(user_id, username, pwd)
        if not keep_must_reset:
            await store.set_user_password(user_id, pwd)
        for r in roles:
            await store.bind_user_role(user_id, r)
        scopes = await store.compute_user_scopes(user_id)
        return await store.create_auth_session(user_id, scope=scopes, ip="1.2.3.4")

    return run(_go())


def add_session(store, user_id="u1", ip="1.2.3.4"):
    """给同一用户再开一个 session（make_user 会重建用户，撞 username UNIQUE）。"""
    async def _go():
        scopes = await store.compute_user_scopes(user_id)
        return await store.create_auth_session(user_id, scope=scopes, ip=ip)

    return run(_go())


def ctx_for(store, raw: str) -> AuthContext:
    from api.security import deps

    return run(deps.resolve_auth(_req(), f"Bearer {raw}"))


# ============================================================
# 请求体校验
# ============================================================

def test_login_request_rejects_empty_username():
    with pytest.raises(ValidationError):
        LoginRequest(username="", password="x")


def test_change_password_request_enforces_min_length():
    with pytest.raises(ValidationError):
        ChangePasswordRequest(old_password="old-pass-1", new_password="short")
    assert ChangePasswordRequest(old_password="old-pass-1", new_password="long-enough")


# ============================================================
# login
# ============================================================

def test_login_success(store):
    make_user(store, roles=("role_developer",))
    resp = run(login(_req(), _resp(), LoginRequest(username="alice", password="pwd-alice-123")))

    assert resp["token"].startswith("ses_")
    assert resp["token_type"] == "bearer"
    assert resp["user"]["username"] == "alice"
    assert "password_hash" not in resp["user"], "绝不能把 hash 吐出去"
    assert "runs.write" in resp["scopes"]
    assert resp["current_session_id"]
    assert resp["expires_at"]

    # 明文 token 能反查出 session
    from api.security import deps

    ctx = run(deps.resolve_auth(_req(), f"Bearer {resp['token']}"))
    assert ctx.user_id == "u1"


def test_login_records_last_login_and_ip(store):
    make_user(store)
    run(login(_req(host="9.9.9.9"), _resp(), LoginRequest(username="alice", password="pwd-alice-123")))

    user = run(store.get_user("u1"))
    assert user["last_login_at"]
    assert user["last_seen_at"]
    sessions = run(store.list_auth_sessions("u1"))
    assert sessions[0]["ip"] == "9.9.9.9", "要记真实客户端 IP（限流与审计都依赖）"


def test_login_wrong_password_401(store):
    make_user(store)
    with pytest.raises(HTTPException) as exc:
        run(login(_req(), _resp(), LoginRequest(username="alice", password="totally-wrong")))
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid_credentials"


def test_login_unknown_user_401_and_no_username_counter(store):
    """用户不存在：同样 401，且**不**建 user: 维度的计数（防随机用户名锁死账号）。"""
    with pytest.raises(HTTPException) as exc:
        run(login(_req(), _resp(), LoginRequest(username="ghost", password="whatever")))
    assert exc.value.detail == "invalid_credentials"

    assert run(store.get_login_attempt("ip:1.2.3.4")) is not None
    assert run(store.get_login_attempt("user:ghost")) is None


def test_login_disabled_user_401(store):
    """禁用账号的失败原因与"密码错"完全一致，不泄露账号状态。"""
    make_user(store)
    run(store.set_user_disabled("u1", True, reason="offboard"))

    with pytest.raises(HTTPException) as exc:
        run(login(_req(), _resp(), LoginRequest(username="alice", password="pwd-alice-123")))
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid_credentials"


def test_login_failure_counter_resets_on_success(store):
    make_user(store)
    for bad in ("bad-1", "bad-2"):
        with pytest.raises(HTTPException):
            run(login(_req(host="4.4.4.4"), _resp(),
                      LoginRequest(username="alice", password=bad)))
    assert run(store.get_login_attempt("ip:4.4.4.4"))["failures"] == 2

    run(login(_req(host="4.4.4.4"), _resp(), LoginRequest(username="alice", password="pwd-alice-123")))
    assert run(store.get_login_attempt("ip:4.4.4.4")) is None, "登录成功要清计数"


def test_login_locked_after_too_many_failures(store):
    """先撞到的是**用户名**维度的阈值（5 次），比 IP 的 10 次更早。"""
    from api.security.rate_limit import LOGIN_MAX_FAILURES_USER

    make_user(store)
    for _ in range(LOGIN_MAX_FAILURES_USER):
        with pytest.raises(HTTPException) as exc:
            run(login(_req(host="6.6.6.6"), _resp(),
                      LoginRequest(username="alice", password="wrong")))
        assert exc.value.detail == "invalid_credentials"

    with pytest.raises(HTTPException) as exc:
        run(login(_req(host="6.6.6.6"), _resp(),
                  LoginRequest(username="alice", password="pwd-alice-123")))
    assert exc.value.status_code == 423, "锁上之后密码正确也拒绝"


def test_login_must_reset_flag_surfaced(store):
    make_user(store, keep_must_reset=True)
    resp = run(login(_req(), _resp(), LoginRequest(username="alice", password="pwd-alice-123")))
    assert resp["must_reset_password"] is True


# ============================================================
# logout / logout-all
# ============================================================

def test_logout_revokes_current_session(store):
    raw, sess = make_user(store)
    ctx = ctx_for(store, raw)

    assert run(logout(_resp(), auth=ctx))["revoked"] is True
    with pytest.raises(HTTPException) as exc:
        ctx_for(store, raw)
    assert exc.value.detail == "invalid_or_revoked_session"


def test_logout_all_keeps_current(store):
    raw_a, _ = make_user(store)
    _, s_b = add_session(store)
    _, s_c = add_session(store)
    ctx = ctx_for(store, raw_a)

    out = run(logout_all(auth=ctx))
    assert out["revoked"] == 2
    assert out["kept_session_id"] == ctx.session_id

    remaining = {s["session_id"] for s in run(store.list_auth_sessions("u1"))}
    assert remaining == {ctx.session_id}


def test_pat_cannot_logout(store):
    make_user(store)
    raw, _ = run(store.create_api_token("u1", "ci", scopes="runs.read"))
    ctx = ctx_for(store, raw)
    assert ctx.token_kind == "pat"

    with pytest.raises(HTTPException) as exc:
        run(logout(_resp(), auth=ctx))
    assert exc.value.status_code == 400
    assert "pat_cannot_logout" in exc.value.detail


# ============================================================
# change-password
# ============================================================

def test_change_password_success_and_revokes_others(store):
    raw, _ = make_user(store)
    add_session(store)                                # 第二个 session
    ctx = ctx_for(store, raw)

    out = run(
        change_password(
            _req(),
            ChangePasswordRequest(old_password="pwd-alice-123", new_password="new-pass-456"),
            auth=ctx,
        )
    )
    assert out["ok"] is True
    assert out["revoked_other_sessions"] == 1, "改密要踢掉其他设备，但保留当前"

    assert run(store.get_user("u1"))["must_reset_password"] == 0
    # 新密码能登录
    resp = run(login(_req(host="7.7.7.7"), _resp(),
                     LoginRequest(username="alice", password="new-pass-456")))
    assert resp["must_reset_password"] is False


def test_change_password_wrong_old_password(store):
    raw, _ = make_user(store)
    ctx = ctx_for(store, raw)

    with pytest.raises(HTTPException) as exc:
        run(
            change_password(
                _req(host="8.8.8.8"),
                ChangePasswordRequest(old_password="not-my-password",
                                      new_password="new-pass-456"),
                auth=ctx,
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "old_password_mismatch"
    # 改密失败也要进限流计数（爆破旧密码的第二入口）
    assert run(store.get_login_attempt("ip:8.8.8.8")) is not None


def test_change_password_rejects_same_password(store):
    raw, _ = make_user(store)
    ctx = ctx_for(store, raw)

    with pytest.raises(HTTPException) as exc:
        run(
            change_password(
                _req(),
                ChangePasswordRequest(old_password="pwd-alice-123",
                                      new_password="pwd-alice-123"),
                auth=ctx,
            )
        )
    assert exc.value.detail == "new_password_must_differ"


def test_change_password_rate_limited(store):
    from api.security.rate_limit import LOGIN_MAX_FAILURES_IP

    raw, _ = make_user(store)
    ctx = ctx_for(store, raw)
    body = ChangePasswordRequest(old_password="wrong", new_password="new-pass-456")

    for _ in range(LOGIN_MAX_FAILURES_IP):
        with pytest.raises(HTTPException):
            run(change_password(_req(host="5.5.5.5"), body, auth=ctx))

    with pytest.raises(HTTPException) as exc:
        run(change_password(_req(host="5.5.5.5"), body, auth=ctx))
    assert exc.value.status_code == 423, "改密同样要限流，否则是爆破旧密码的口子"


def test_change_password_accessible_for_must_reset_user(store):
    """must_reset 用户必须能改密——这是唯一能解除该标记的路径。"""
    raw, _ = make_user(store, keep_must_reset=True)
    ctx = ctx_for(store, raw)
    assert ctx.must_reset_password

    out = run(
        change_password(
            _req(),
            ChangePasswordRequest(old_password="pwd-alice-123", new_password="new-pass-456"),
            auth=ctx,
        )
    )
    assert out["ok"] is True


# ============================================================
# me / me/sessions
# ============================================================

def test_me_returns_user_scopes_and_session(store):
    raw, sess = make_user(store, roles=("role_developer",))
    ctx = ctx_for(store, raw)

    out = run(me(auth=ctx))
    assert out["user"]["username"] == "alice"
    assert "password_hash" not in out["user"]
    assert "role_developer" in out["roles"]
    assert "runs.write" in out["scopes"]
    assert out["current_session_id"] == sess["session_id"]
    assert out["token_kind"] == "session"
    assert "is_online" in out["user"]


def test_me_marks_online(store):
    raw, _ = make_user(store)
    ctx = ctx_for(store, raw)
    assert run(me(auth=ctx))["user"]["is_online"] is True


def test_my_sessions_current_first_and_online(store):
    raw_a, s_a = make_user(store)
    _, s_b = add_session(store)
    _, s_c = add_session(store)
    ctx = ctx_for(store, raw_a)

    out = run(my_sessions(auth=ctx))
    assert out["total"] == 3
    assert {s_b["session_id"], s_c["session_id"]} <= {
        s["session_id"] for s in out["sessions"]
    }
    assert out["sessions"][0]["is_current"] is True
    assert out["sessions"][0]["session_id"] == s_a["session_id"]
    assert all(s["ip"] == "1.2.3.4" for s in out["sessions"])
    assert all(s["is_online"] for s in out["sessions"]), "刚建的 session 都是在线"


def test_my_sessions_excludes_revoked(store):
    raw_a, _ = make_user(store)
    _, s_b = add_session(store)
    run(store.revoke_auth_session(s_b["session_id"], reason="test"))
    ctx = ctx_for(store, raw_a)

    out = run(my_sessions(auth=ctx))
    assert out["total"] == 1
    assert out["sessions"][0]["is_current"] is True
