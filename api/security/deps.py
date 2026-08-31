"""FastAPI 鉴权依赖链（S6）。

设计见 ``docs/security-mvp-plan-2026-08-29.md`` §3.1 / §3.4。

职责：把 ``Authorization: Bearer <token>`` 解析成 ``AuthContext``，并提供
``require_scope(...)`` 依赖工厂给业务端点挂载。

三种身份载体（§3.1）
    1. ``ses_*`` 浏览器/UI 会话 token —— 存 SHA-256 指纹，支持滑动续期
    2. ``pat_*`` 个人访问令牌（CLI/CI）—— 存 argon2，明文只在创建时出现一次
    3. 无 token → 匿名上下文（``ANONYMOUS``），``require_scope`` 会拒

与方案 §3.4 的三处偏差
    1. **返回 ``AuthContext`` 而不是 ``(user, scope_str)`` 元组**。
       需要额外携带 ``token_kind``（PAT 不能"登出"，登出端点要区分）和
       ``session_id``（``/api/auth/me`` 要高亮当前 session）。scopes 内部存
       ``frozenset``，``has()`` 是 O(1) 而不是每次 ``str.split()``。
    2. **dev 绕过用"环境变量 + 标记文件"双重条件**，而不是方案说的"DB 中的
       dev-marker 行"。理由：标记行会污染 ``users`` 表（出现在用户列表里），
       且数据库经常从备份/迁移恢复，标记可能被无意带过去。标记文件在部署目录下，
       不会被数据迁移复制，也不会进用户列表。
    3. **``must_reset_password`` 的豁免路径收窄**。原方案用
       ``path.endswith("/auth/change-password")`` 判断，``/api/auth/logout``
       和 ``/api/auth/me`` 之外的一切都被挡——包括前端加载用户信息，会死锁。
       改为显式白名单：改密、登出、读自己信息三条路径放行。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import Depends, Header, HTTPException, Request, status

from api.security.rate_limit import client_ip, parse_iso

logger = logging.getLogger(__name__)

__all__ = [
    "AuthContext",
    "ANONYMOUS",
    "SCOPE_ALL",
    "get_store",
    "current_user",
    "optional_user",
    "require_scope",
    "auth_bypass_enabled",
]

# 通配 scope：只有 dev 绕过模式下的上下文才有
SCOPE_ALL = "*"

# session cookie 名（S13）：login 时 Set-Cookie，供 EventSource（SSE）等
# 无法带 Authorization 头的客户端使用。guard 在无 Bearer 头时读它构造凭证。
SESSION_COOKIE_NAME = "agentops_session"

# 会话滑动续期阈值：剩余不足 1 天就续到 +7 天（方案 §3.4）
SLIDING_RENEW_THRESHOLD = timedelta(days=1)
SLIDING_RENEW_DAYS = 7

# 必须允许 must_reset_password 用户访问的路径（否则前端拿不到改密入口，直接死锁）
_MUST_RESET_ALLOWED_SUFFIXES = (
    "/api/auth/change-password",
    "/api/auth/logout",
    "/api/auth/logout-all",
    "/api/auth/me",
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_AUTH_BYPASS_MARKER = _PROJECT_ROOT / ".auth-disabled"


# ============================================================
# store 访问
# ============================================================

def get_store() -> Any:
    """取全局 ``SqliteEventStore``。

    延迟导入 + 运行时取属性（不能 ``from api.server import _event_store``，
    那样拿到的是 import 时刻的 ``None``；也不能在模块顶层 import api.server，
    会形成 ``api.server → api.security → api.server`` 循环导入）。
    """
    from api import server

    store = getattr(server, "_event_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="event store 尚未初始化",
        )
    return store


def auth_bypass_enabled() -> bool:
    """本地 dev 免鉴权开关：环境变量 + 标记文件**同时**满足才生效。

    双条件的目的是防生产误开：只设环境变量不够，还必须手动在部署目录创建
    ``.auth-disabled`` 文件。数据备份/迁移不会带上这个文件。
    """
    if os.environ.get("AGENTOPS_AUTH_DISABLED", "").strip() != "1":
        return False
    return _AUTH_BYPASS_MARKER.exists()


# ============================================================
# 鉴权上下文
# ============================================================

@dataclass(frozen=True)
class AuthContext:
    """一次请求的鉴权结果。

    - ``user``：``users`` 表一行（dict）；匿名时是 ``_ANON_USER``
    - ``scopes``：perm_id 集合；含 ``*`` 表示通配（仅 dev 绕过模式）
    - ``token_kind``：``session`` / ``pat`` / ``anon`` / ``bypass``
    - ``session_id`` / ``token_id``：凭证标识，登出与审计用
    """

    user: dict[str, Any]
    scopes: frozenset[str] = field(default_factory=frozenset)
    token_kind: str = "anon"
    session_id: str | None = None
    token_id: str | None = None

    @property
    def user_id(self) -> str:
        return self.user.get("user_id") or ""

    @property
    def is_anonymous(self) -> bool:
        return self.token_kind == "anon"

    @property
    def must_reset_password(self) -> bool:
        return bool(self.user.get("must_reset_password"))

    def has(self, scope: str) -> bool:
        """是否持有某个 perm_id。``*`` 通配。"""
        return SCOPE_ALL in self.scopes or scope in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "missing_scope", "missing_scope": scope},
            )


_ANON_USER: dict[str, Any] = {
    "user_id": "",
    "username": None,
    "display_name": "Anonymous",
    "disabled_at": None,
    "must_reset_password": 0,
}

ANONYMOUS = AuthContext(user=_ANON_USER, scopes=frozenset(), token_kind="anon")

_BYPASS_USER: dict[str, Any] = {
    "user_id": "user_dev_bypass",
    "username": "dev-bypass",
    "display_name": "Dev Bypass",
    "disabled_at": None,
    "must_reset_password": 0,
}

_BYPASS_CONTEXT = AuthContext(
    user=_BYPASS_USER,
    scopes=frozenset({SCOPE_ALL}),
    token_kind="bypass",
)


def _now() -> datetime:
    """当前 UTC 时间。AGENTS.md 约定：不用 ``datetime.utcnow()``（已弃用）。"""
    return datetime.now(timezone.utc)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


# ============================================================
# token 解析
# ============================================================

async def _resolve_pat(store: Any, raw: str, ip: str) -> AuthContext:
    """PAT 路径：argon2 校验 → 过期/撤销/禁用检查 → 节流 touch。"""
    row = await store.verify_api_token(raw)
    if not row:
        raise _unauthorized("invalid_token")

    expires_at = parse_iso(row.get("expires_at"))
    if expires_at and expires_at <= _now():
        raise _unauthorized("token_expired")

    user = await store.get_user(row["user_id"])
    if not user or user.get("disabled_at"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "user_disabled"},
        )

    await store.touch_last_seen(row["user_id"], throttle=True)
    await store.touch_api_token(row["token_id"], ip=ip, throttle=True)

    # 同 session 路径：回填刚 touch 的 last_seen_at，否则 is_online 永远 False
    user = dict(user, last_seen_at=_now().isoformat())

    scopes = frozenset((row.get("scopes") or "").split())
    return AuthContext(
        user=user, scopes=scopes, token_kind="pat", token_id=row["token_id"]
    )


async def _resolve_session(store: Any, raw: str, ip: str) -> AuthContext:
    """Session 路径：SHA-256 指纹查表 → 撤销/绝对过期/滑动过期 → 按需续期。"""
    sess = await store.get_auth_session_by_token(raw)
    if not sess or sess.get("revoked_at"):
        raise _unauthorized("invalid_or_revoked_session")

    now = _now()
    absolute = parse_iso(sess.get("absolute_expires_at"))
    sliding = parse_iso(sess.get("sliding_expires_at"))
    if (absolute and absolute <= now) or (sliding and sliding <= now):
        raise _unauthorized("session_expired")

    # 滑动续期：剩余不足 1 天就再续 7 天（绝不超过绝对过期时间，store 层已保证）
    if sliding and (sliding - now) < SLIDING_RENEW_THRESHOLD:
        await store.extend_sliding_expiry(
            sess["session_id"], sliding_days=SLIDING_RENEW_DAYS
        )

    user = await store.get_user(sess["user_id"])
    if not user or user.get("disabled_at"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "user_disabled"},
        )

    await store.touch_last_seen(sess["user_id"], throttle=True)
    await store.touch_auth_session(sess["session_id"], throttle=True)

    # touch 刚写过 users.last_seen_at，但上面拿到的 user 是 touch **之前**的行，
    # 直接返回会让 /api/auth/me 的 is_online 永远是 False。这里把新值回填进内存副本
    # （不回查一次 DB，省一次写后读）。节流未落库时写 now 同样成立：
    # 30s 内活跃过，本来就该算在线。
    user = dict(user, last_seen_at=_now().isoformat())

    scopes = frozenset((sess.get("scope") or "").split())
    return AuthContext(
        user=user, scopes=scopes, token_kind="session", session_id=sess["session_id"]
    )


# ============================================================
# 依赖
# ============================================================

async def resolve_auth(
    request: Request, authorization: str | None
) -> AuthContext:
    """解析请求身份的纯逻辑（不依赖 FastAPI 的参数注入，方便单测直接调用）。

    被 ``optional_user`` 包一层；分开是因为 ``Header(default=None)`` 的默认值在
    直接函数调用时是 ``Header`` 哨兵对象而不是 ``None``。
    """
    if auth_bypass_enabled():
        return _BYPASS_CONTEXT

    if not authorization or not authorization.lower().startswith("bearer "):
        return ANONYMOUS

    raw = authorization.split(" ", 1)[1].strip()
    if not raw:
        return ANONYMOUS

    ip = client_ip(request)

    try:
        if raw.startswith("pat_"):
            return await _resolve_pat(get_store(), raw, ip)
        if raw.startswith("ses_"):
            return await _resolve_session(get_store(), raw, ip)
    except HTTPException:
        raise
    except Exception as exc:
        # DB 抖动不该变成 500 堆栈泄漏给客户端，统一当鉴权失败处理
        logger.warning("[security] 解析凭证失败：%s", exc, exc_info=True)
        raise _unauthorized("credential_resolution_failed") from exc

    raise _unauthorized("unsupported_token_format")


async def optional_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthContext:
    """FastAPI 依赖版 ``resolve_auth``。

    与 ``current_user`` 的分工：本函数对匿名/非法 token 一律返回匿名上下文（不抛），
    ``current_user`` 才把匿名转成 401。分开是因为 MVP 期间部分端点要允许匿名读，
    但又希望在带 token 时给出更多内容。
    """
    return await resolve_auth(request, authorization)


async def current_user(
    auth: AuthContext = Depends(optional_user),
) -> AuthContext:
    """必须已登录。匿名 → 401。"""
    if auth.is_anonymous:
        raise _unauthorized("authentication_required")
    return auth


def require_scope(scope: str):
    """依赖工厂：要求当前身份持有指定 perm_id。

    检查顺序（**顺序有讲究**）：

    1. 匿名 → 401（先区分"没登录"和"权限不够"，前端要靠这个决定跳登录页还是显示无权限）
    2. scope 缺失 → 403
    3. ``must_reset_password`` → 403 + redirect（除白名单路径外）
       放在 scope 之后，避免"该改密"先于"根本没这个权限"返回，
       否则攻击者能靠错误码枚举自己有哪些权限。
    """

    async def _checker(
        request: Request,
        auth: AuthContext = Depends(current_user),
    ) -> AuthContext:
        if not auth.has(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "missing_scope", "missing_scope": scope},
            )

        if auth.must_reset_password and not _path_allows_must_reset(request.url.path):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "must_reset_password",
                    "message": "首次登录必须修改密码",
                    "redirect": "/account/change-password",
                },
            )
        return auth

    return _checker


def _path_allows_must_reset(path: str) -> bool:
    """``must_reset_password`` 状态下仍可访问的路径白名单。

    必须放行 ``/api/auth/me``——前端启动时要先拿到用户信息才知道要跳改密页，
    把它挡掉会让用户卡在登录后的空白页上（方案原文只豁免了 change-password，
    这是个死锁 bug）。
    """
    normalized = path.rstrip("/")
    return any(
        normalized.endswith(suffix.rstrip("/")) for suffix in _MUST_RESET_ALLOWED_SUFFIXES
    )


def scopes_of(scopes: Iterable[str]) -> frozenset[str]:
    """把 store 返回的空间分隔 scope 串转成集合。"""
    return frozenset(s for s in scopes if s)


# 永不出网的字段。**任何**返回用户的接口都必须过 ``public_user``，
# 否则 password_hash 会被顺手 serialize 出去。
_USER_SECRET_FIELDS = ("password_hash",)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """脱敏用户行：剔除 password_hash，补上派生的 ``is_online`` / ``roles``。"""
    from audit.security_store import SecurityStoreMixin

    out = {k: v for k, v in user.items() if k not in _USER_SECRET_FIELDS}
    out["is_online"] = SecurityStoreMixin.is_online(user)
    return out
