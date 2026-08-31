"""路径级鉴权 guard（S13，方案 v1.4 §3.6）。

设计来源：``docs/security-mvp-plan-2026-08-29.md`` v1.4 变更摘要 + §3.6。

为什么是 **FastAPI 全局依赖**（``Depends(auth_guard)``）而不是裸 Starlette
middleware（方案 v1.3 原设想）：

1. ``HTTPException`` 在 DI 上下文里能被 FastAPI 异常处理器正常转成 401/403
   JSON；裸 middleware 里抛会变 500。
2. app 级依赖只作用于 HTTP 路径操作，**天然不碰** ``@app.websocket``——
   WebSocket 豁免零成本。
3. 不包裹 ``StreamingResponse``，SSE 端点（runs events / patrol alerts /
   build stream）无流式干扰。

放行规则（按顺序短路）：

1. OPTIONS → 放行（CORS preflight 不带凭证，不豁免则跨域全挂）
2. 非 ``/api/*`` → 放行（``/``、``/docs``、``/openapi.json``、``/ws/*``）
3. ``/api/auth/*`` 与 ``/api/security/*`` → 放行。前者路由自带 ``current_user``
   鉴权（权限字典无 auth 域，只豁免 login 会让 must_reset 用户调 me/logout 被
   403 死锁——D-064 踩过的坑）；后者是 S8-S11 管理面，**每个端点已挂
   ``require_scope``**（security.users/roles/api_tokens/sessions 域），无需重复。
4. 其余按映射表解析 perm_id；**未登记路径 403 fail-closed**（由
   ``tests/test_security_guard.py`` 的路由覆盖测试保证不会误伤已注册路由）
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from api.security.deps import (
    SESSION_COOKIE_NAME,
    _path_allows_must_reset,
    resolve_auth,
)

logger = logging.getLogger(__name__)

__all__ = ["auth_guard", "is_exempt", "resolve_route_scope", "PERMISSION_IDS"]

# 权限字典全集（46 条，v91 26 + v93 20）。映射表产出的 perm_id 必须在此集合内，
# 否则视为配置错误 → fail-closed。与 DB 的 drift 由路由覆盖测试检测。
PERMISSION_IDS: frozenset[str] = frozenset({
    # v91（26）
    "sessions.read", "sessions.write", "sessions.cancel",
    "runs.read", "runs.write", "runs.cancel",
    "workflows.read", "workflows.write", "workflows.cancel",
    "agents.read", "agents.write", "agents.invoke",
    "credentials.read", "credentials.write",
    "knowledge.read", "knowledge.write",
    "security.users.read", "security.users.write",
    "security.roles.read", "security.roles.write",
    "security.api_tokens.read", "security.api_tokens.write",
    "security.sessions.read", "security.sessions.write",
    "system.read", "system.write",
    # v93（20）
    "tasks.read", "tasks.write",
    "runtime.read", "runtime.write",
    "workspaces.read", "workspaces.write",
    "providers.read", "providers.write",
    "connections.read", "connections.write",
    "schedules.read", "schedules.write",
    "logs.read", "logs.write",
    "patrol.read", "patrol.write",
    "monitor.read", "monitor.write",
    "usage.read", "audit.read",
})

# 路径前缀 → resource 域。**顺序即优先级**（首个 startswith 命中生效），
# 前缀互为包含的条目（/api/agent/*、/api/v2/*）必须排在兜底条目之前。
# 依据：api/server.py 全部 187 条 HTTP 路由实测清单（2026-08-29）。
_ROUTE_RESOURCES: tuple[tuple[str, str], ...] = (
    ("/api/agent/workflows", "workflows"),
    ("/api/agent/runs", "runs"),
    ("/api/agent/agents", "agents"),
    ("/api/agent", "agents"),          # 兜底：domains / harnesses / tools / run
    ("/api/actors", "agents"),         # Worker Visual Profile 只读元数据
    ("/api/v2/sessions", "sessions"),
    ("/api/v2", "sessions"),           # 兜底：approvals（审批属于会话流）
    ("/api/audit", "audit"),
    ("/api/connections", "connections"),
    ("/api/db-credentials", "credentials"),
    ("/api/ssh-credentials", "credentials"),
    ("/api/debug", "system"),          # asyncio-tasks 观测归系统只读
    ("/api/knowledge", "knowledge"),
    ("/api/log-pull", "logs"),
    ("/api/log-sources", "logs"),
    ("/api/monitor", "monitor"),
    ("/api/patrol", "patrol"),
    ("/api/providers", "providers"),
    ("/api/runtime", "runtime"),
    ("/api/schedules", "schedules"),
    ("/api/sessions", "sessions"),
    ("/api/system", "system"),
    ("/api/tasks", "tasks"),
    ("/api/usage", "usage"),
    ("/api/workspaces", "workspaces"),
    ("/api/runs", "runs"),
)

# 细粒度 action override：(路径后缀（精确或 endswith）, POST, resource 白名单, action)。
# cancel/invoke 是字典里已有的独立 action，一刀切 "写→write" 会让它们形同虚设。
_ACTION_OVERRIDES: tuple[tuple[str, frozenset[str], str], ...] = (
    ("/cancel", frozenset({"runs", "sessions", "workflows"}), "cancel"),
    ("/api/agent/run", frozenset({"agents"}), "invoke"),
)

_READ_METHODS = frozenset({"GET", "HEAD"})

# 自带鉴权的豁免前缀（is_exempt 规则 3）
_EXEMPT_PREFIXES = ("/api/auth/", "/api/security/")


def is_exempt(path: str, method: str) -> bool:
    """无需鉴权直接放行的请求（guard 规则 1-3）。"""
    if method.upper() == "OPTIONS":          # CORS preflight
        return True
    if not path.startswith("/api/"):         # 非 API：静态页 / docs / ws
        return True
    return path.startswith(_EXEMPT_PREFIXES)  # 自带鉴权的 auth + security 路由


def resolve_route_scope(path: str, method: str) -> str | None:
    """把 ``(路径, 方法)`` 解析成 perm_id；未登记或配置错误返回 None。

    纯函数（不碰 request/DB），供 guard 与路由覆盖测试共用。
    """
    resource = next(
        (r for prefix, r in _ROUTE_RESOURCES if path.startswith(prefix)), None
    )
    if resource is None:
        return None

    action = "read" if method.upper() in _READ_METHODS else "write"
    for suffix, resources, override in _ACTION_OVERRIDES:
        if (
            method.upper() == "POST"
            and (path == suffix or path.endswith(suffix))
            and resource in resources
        ):
            action = override
            break

    perm = f"{resource}.{action}"
    return perm if perm in PERMISSION_IDS else None


async def auth_guard(request: Request) -> None:
    """全局路径级鉴权依赖（注册在 ``FastAPI(dependencies=[...])``）。

    身份解析复用 ``deps.resolve_auth``：Bearer 头优先；无头时读
    ``agentops_session`` cookie（EventSource/SSE 无法自定义头，浏览器自动带
    cookie，前端零改动）。dev 绕过双条件开关在 resolve_auth 内原样生效。
    """
    path = request.url.path
    if is_exempt(path, request.method):
        return

    perm = resolve_route_scope(path, request.method)
    if perm is None:
        # fail-closed：未登记的 API 路径宁可误伤（覆盖测试兜底）也不裸奔
        logger.warning("[security] 未登记的 API 路径被拒绝: %s %s", request.method, path)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "unmapped_path", "path": path},
        )

    authz = request.headers.get("authorization")
    if not authz:
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_token:
            authz = f"Bearer {cookie_token}"

    # invalid_token / session_expired 等 HTTPException 直接向上抛（DI 上下文正常处理）
    auth = await resolve_auth(request, authz)

    if auth.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication_required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not auth.has(perm):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "missing_scope", "missing_scope": perm},
        )

    # 业务端点没挂 require_scope，强制改密门禁只能在这里拦
    if auth.must_reset_password and not _path_allows_must_reset(path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "must_reset_password",
                "message": "首次登录必须修改密码",
                "redirect": "/account/change-password",
            },
        )
