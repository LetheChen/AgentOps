"""路径级鉴权 guard 测试（S13，方案 v1.4 §3.6）。

覆盖：
- 映射表单元用例（resource 解析 / method→action / cancel+invoke override）
- 豁免规则（OPTIONS / 非 /api / /api/auth/*）
- **路由覆盖测试**：遍历 ``app.routes``，断言每条 ``/api/*`` HTTP 路由都能被
  guard 解析出有效 perm_id 或在豁免名单——防止未来新增端点静默漏保护
- guard 行为：匿名 401 / 无效 token 401 / 缺 scope 403 / must_reset 403 /
  unmapped fail-closed 403 / cookie 认证回退 / bypass 放行
- ``PERMISSION_IDS`` 与 DB 权限字典 drift 检测（v93 迁移后两处必须一致）
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from api.security.guard import (
    PERMISSION_IDS,
    auth_guard,
    is_exempt,
    resolve_route_scope,
)
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


def _req(
    path="/api/tasks",
    method="GET",
    auth: str | None = None,
    cookie: str | None = None,
) -> Request:
    headers = []
    if auth:
        headers.append((b"authorization", auth.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("1.2.3.4", 54321),
            "server": ("testserver", 80),
        }
    )


def run(coro):
    import asyncio

    return asyncio.run(coro)


def make_user(store, user_id="u1", username="alice", pwd="pwd-alice-123",
              roles=(), *, keep_must_reset=False):
    async def _go():
        await store.create_user(user_id, username, pwd)
        if not keep_must_reset:
            await store.set_user_password(user_id, pwd)
        for r in roles:
            await store.bind_user_role(user_id, r)
        scopes = await store.compute_user_scopes(user_id)
        return await store.create_auth_session(user_id, scope=scopes, ip="1.2.3.4")

    return run(_go())


# ============================================================
# 映射表单元用例
# ============================================================

@pytest.mark.parametrize(
    "path,method,expected",
    [
        ("/api/tasks", "GET", "tasks.read"),
        ("/api/tasks", "POST", "tasks.write"),
        ("/api/tasks/terminal/layout", "PUT", "tasks.write"),
        ("/api/agent/runs/{run_id}/cancel", "POST", "runs.cancel"),
        ("/api/v2/sessions/{id}/cancel", "POST", "sessions.cancel"),
        ("/api/agent/run", "POST", "agents.invoke"),
        ("/api/agent/runs", "POST", "runs.write"),
        ("/api/agent/workflows", "GET", "workflows.read"),
        ("/api/agent/workflows/{id}", "PUT", "workflows.write"),
        ("/api/agent/domains", "GET", "agents.read"),
        ("/api/ssh-credentials", "GET", "credentials.read"),
        ("/api/db-credentials/{id}", "DELETE", "credentials.write"),
        ("/api/monitor/emit-alert", "POST", "monitor.write"),
        ("/api/patrol/log-patrol/trigger", "POST", "patrol.write"),
        ("/api/audit/runs/{id}/summary", "GET", "audit.read"),
        ("/api/usage/summary", "GET", "usage.read"),
        ("/api/v2/approvals/{id}/decide", "POST", "sessions.write"),
        ("/api/runtime/docker/containers/{c}/stop", "POST", "runtime.write"),
        ("/api/knowledge/vault/read", "GET", "knowledge.read"),
        ("/api/agent/runs/{run_id}/events", "GET", "runs.read"),
        ("/api/sessions/{session_id}", "GET", "sessions.read"),
    ],
)
def test_resolve_route_scope(path, method, expected):
    assert resolve_route_scope(path, method) == expected


def test_unmapped_path_fails_closed():
    """未登记前缀返回 None → guard 403，绝不放行。"""
    assert resolve_route_scope("/api/unknown-domain/x", "GET") is None


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/auth/login", "POST"),
        ("/api/auth/me", "GET"),
        ("/api/auth/logout", "POST"),
        ("/api/security/users", "GET"),  # S8-S11 端点自带 require_scope
        ("/api/security/api-tokens", "POST"),
        ("/", "GET"),
        ("/docs", "GET"),
        ("/openapi.json", "GET"),
        ("/api/tasks", "OPTIONS"),  # CORS preflight
    ],
)
def test_is_exempt(path, method):
    assert is_exempt(path, method)


def test_not_exempt():
    assert not is_exempt("/api/tasks", "GET")
    # 前缀边界：/api/authx 不属于 /api/auth/ 豁免
    assert not is_exempt("/api/authx/login", "POST")
    assert not is_exempt("/api/security2/users", "GET")


# ============================================================
# 路由覆盖测试（防新增端点漏保护的关键闸门）
# ============================================================

def test_route_coverage_all_api_routes_resolvable():
    """app.routes 里每条 /api/* HTTP 路由都必须可解析或在豁免名单。"""
    from api import server as server_mod

    checked = 0
    for route in server_mod.app.routes:
        if not isinstance(route, APIRoute):
            continue  # WebSocket 路由不经过 HTTP 依赖，天然豁免
        path = route.path
        if not path.startswith("/api/"):
            continue
        if is_exempt(path, "GET"):
            continue  # /api/auth/*
        perm = resolve_route_scope(path, "GET") or resolve_route_scope(path, "POST")
        assert perm is not None, f"路由未登记进 guard 映射表: {path}"
        assert perm in PERMISSION_IDS, f"路由解析出未定义的权限: {path} -> {perm}"
        checked += 1

    # 防御 app.routes 为空 / 导入失败导致的假通过
    assert checked > 150, f"覆盖路由数异常: {checked}"


def test_permission_ids_match_db(store):
    """guard 硬编码权限集与 DB 字典必须一致（v93 迁移 drift 检测）。"""
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    try:
        rows = conn.execute("SELECT perm_id FROM security_permissions").fetchall()
    finally:
        conn.close()
    db_perms = {r[0] for r in rows}
    assert db_perms == set(PERMISSION_IDS), (
        f"guard.PERMISSION_IDS 与 DB drift: 缺失={db_perms - set(PERMISSION_IDS)}, "
        f"多余={set(PERMISSION_IDS) - db_perms}"
    )


# ============================================================
# auth_guard 行为
# ============================================================

def test_guard_exempt_paths_need_no_store():
    """豁免路径不触 store（没有 Authorization 也不 401）。"""
    for req in (
        _req("/api/auth/login", "POST"),
        _req("/", "GET"),
        _req("/api/tasks", "OPTIONS"),
    ):
        assert run(auth_guard(req)) is None


def test_guard_unmapped_path_403():
    with pytest.raises(HTTPException) as ei:
        run(auth_guard(_req("/api/unknown-domain/x", "GET")))
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "unmapped_path"


def test_guard_anonymous_401():
    with pytest.raises(HTTPException) as ei:
        run(auth_guard(_req("/api/tasks", "GET")))
    assert ei.value.status_code == 401


def test_guard_invalid_token_401(store):
    with pytest.raises(HTTPException) as ei:
        run(auth_guard(_req("/api/tasks", "GET", auth="Bearer ses_nonexistent")))
    assert ei.value.status_code == 401


def test_guard_viewer_read_ok_write_403(store):
    token, _ = make_user(store, roles=("role_viewer",))
    ok = run(auth_guard(_req("/api/tasks", "GET", auth=f"Bearer {token}")))
    assert ok is None  # 放行
    with pytest.raises(HTTPException) as ei:
        run(auth_guard(_req("/api/tasks", "POST", auth=f"Bearer {token}")))
    assert ei.value.status_code == 403
    assert ei.value.detail["missing_scope"] == "tasks.write"


def test_guard_owner_write_ok(store):
    token, _ = make_user(store, roles=("role_owner",))
    assert run(auth_guard(_req("/api/tasks", "POST", auth=f"Bearer {token}"))) is None


def test_guard_owner_must_reset_403_on_business_path(store):
    """must_reset 用户除白名单外全部 403（业务端点没挂 require_scope，guard 兜底）。"""
    token, _ = make_user(store, roles=("role_owner",), keep_must_reset=True)
    with pytest.raises(HTTPException) as ei:
        run(auth_guard(_req("/api/tasks", "GET", auth=f"Bearer {token}")))
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "must_reset_password"


def test_guard_cookie_fallback(store):
    """无 Authorization 头时读 agentops_session cookie（SSE/EventSource 路径）。"""
    token, _ = make_user(store, roles=("role_viewer",))
    req = _req("/api/agent/runs/{run_id}/events", "GET",
               cookie=f"agentops_session={token}")
    assert run(auth_guard(req)) is None


def test_guard_cookie_only_used_without_header(store):
    """有 Authorization 头时 cookie 被忽略（头优先）。"""
    token, _ = make_user(store, roles=("role_viewer",))
    req = _req("/api/tasks", "GET", auth=f"Bearer {token}",
               cookie="agentops_session=ses_garbage")
    assert run(auth_guard(req)) is None


def test_guard_bypass_mode_passes_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """dev 绕过双条件：环境变量 + 标记文件同时满足 → 一切放行（无需凭证）。"""
    marker = tmp_path / ".auth-disabled"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_AUTH_DISABLED", "1")
    monkeypatch.setattr("api.security.deps._AUTH_BYPASS_MARKER", marker)
    assert run(auth_guard(_req("/api/tasks", "DELETE"))) is None
