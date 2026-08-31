"""/api/auth/* 端点（S7）。

方案见 ``docs/security-mvp-plan-2026-08-29.md`` §3.5。

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | 限流 + 恒定响应时间；成功后签发 session token |
| POST | `/api/auth/logout` | authed | 吊销当前 session |
| POST | `/api/auth/logout-all` | authed | 吊销**其他**所有 session（保留当前） |
| POST | `/api/auth/change-password` | authed | 改自己的密码；`must_reset_password` 用户也能访问 |
| GET | `/api/auth/me` | authed | 当前用户 + scopes + `current_session_id` |
| GET | `/api/auth/me/sessions` | authed | 我的活跃 session（标出当前） |

安全要点
    - 登录失败**一律返回同一个 `invalid_credentials`**，不区分"用户不存在"、
      "密码错"、"账号已禁用"，避免账号枚举
    - 登录与改密都过限流（改密是爆破旧密码的第二入口，常被漏掉）
    - 改密成功后吊销**其他**所有 session，保留当前（不让用户把自己踢下线）
    - 响应体里永不出现 `password_hash`（统一走 ``public_user``）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from api.security.deps import (
    SESSION_COOKIE_NAME,
    AuthContext,
    current_user,
    get_store,
    public_user,
)
from api.security.rate_limit import (
    check_login_rate_limit,
    client_ip,
    record_login_failure,
    reset_login_attempts,
    verify_password_constant_time,
)
from audit.security_store import SecurityStoreMixin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LEN = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 请求体
# ============================================================

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=256)


# ============================================================
# 登录
# ============================================================

@router.post("/login")
async def login(request: Request, response: Response, body: LoginRequest) -> dict:
    store = get_store()
    ip = client_ip(request)

    # 限流前置：被锁的话**密码正确也拒绝**（否则"猜中即绕过"）
    await check_login_rate_limit(store, ip, body.username)

    user = await store.get_user_by_username(body.username)

    # 用户不存在时也走一次 argon2（用户不存在 → hash=None → 诱饵 hash），
    # 保证"用户不存在"与"密码错误"耗时同量级
    password_ok = verify_password_constant_time(
        user["password_hash"] if user else None, body.password
    )

    # 三种失败（不存在 / 密码错 / 已禁用）合并成同一个响应，杜绝账号枚举
    if not user or not password_ok or user.get("disabled_at"):
        # 用户不存在时不记 username 维度，否则随机用户名能锁死合法账号
        await record_login_failure(store, ip, body.username if user else None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_credentials",
        )

    await reset_login_attempts(store, ip, body.username)

    scopes = await store.compute_user_scopes(user["user_id"])
    raw_token, sess = await store.create_auth_session(
        user["user_id"],
        scope=scopes,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )

    now_iso = _now_iso()
    await store.update_user(user["user_id"], last_login_at=now_iso, last_seen_at=now_iso)

    logger.info("[security] 登录成功 user=%s ip=%s", user["username"], ip)

    # session cookie（S13）：供 EventSource（SSE）等无法带 Authorization 头的
    # 客户端使用，浏览器自动携带，前端零改动。HttpOnly 防 XSS 读取；
    # SameSite=Lax 缓解跨站写（CSRF）。过期与滑动续期对齐（7d）。
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )

    return {
        "token": raw_token,                      # 明文只此一次
        "token_type": "bearer",
        "user": public_user(user),
        "scopes": sorted(scopes.split()),
        "expires_at": sess["sliding_expires_at"],
        "must_reset_password": bool(user.get("must_reset_password")),
        "current_session_id": sess["session_id"],
    }


# ============================================================
# 登出
# ============================================================

@router.post("/logout")
async def logout(response: Response, auth: AuthContext = Depends(current_user)) -> dict:
    if auth.token_kind != "session":
        # PAT 是长期凭证，没有"登出"语义，只能撤销（S9 的 /api/security/api-tokens）
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="pat_cannot_logout; revoke the token instead",
        )

    store = get_store()
    revoked = await store.revoke_auth_session(auth.session_id, reason="logout")
    # 同步清掉 session cookie（即使凭证本来就是 header 带的，清了无害）
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    logger.info("[security] 登出 user=%s session=%s", auth.user_id, auth.session_id)
    return {"revoked": bool(revoked), "session_id": auth.session_id}


@router.post("/logout-all")
async def logout_all(auth: AuthContext = Depends(current_user)) -> dict:
    """吊销**其他所有** session，保留当前这个（否则用户会把自己踢下线）。"""
    if auth.token_kind != "session":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="pat_cannot_logout; revoke the token instead",
        )

    store = get_store()
    n = await store.revoke_all_sessions(
        auth.user_id, except_session_id=auth.session_id, reason="logout-all"
    )
    logger.info("[security] 登出所有设备 user=%s revoked=%d", auth.user_id, n)
    return {"revoked": n, "kept_session_id": auth.session_id}


# ============================================================
# 改密
# ============================================================

@router.post("/change-password")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    auth: AuthContext = Depends(current_user),
) -> dict:
    store = get_store()
    ip = client_ip(request)
    username = auth.user.get("username") or ""

    # 改密是爆破旧密码的第二入口，同样要限流（方案 §3.5 没写，但漏了就是洞）
    await check_login_rate_limit(store, ip, username)

    if not verify_password_constant_time(
        auth.user.get("password_hash"), body.old_password
    ):
        await record_login_failure(store, ip, username)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="old_password_mismatch"
        )

    if body.old_password == body.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="new_password_must_differ"
        )

    await store.set_user_password(auth.user_id, body.new_password)
    await reset_login_attempts(store, ip, username)

    # 改密后吊销其他 session（凭证可能已泄露），保留当前不让用户掉线
    revoked = 0
    if auth.token_kind == "session":
        revoked = await store.revoke_all_sessions(
            auth.user_id, except_session_id=auth.session_id, reason="password_changed"
        )

    logger.info("[security] 改密成功 user=%s revoked_others=%d", auth.user_id, revoked)
    return {"ok": True, "revoked_other_sessions": revoked}


# ============================================================
# 当前身份
# ============================================================

@router.get("/me")
async def me(auth: AuthContext = Depends(current_user)) -> dict:
    """当前身份。**`must_reset_password` 用户也能访问**（前端靠它决定跳不跳改密页）。"""
    store = get_store()
    roles = await store.list_user_roles(auth.user_id)

    return {
        "user": public_user(auth.user),
        "roles": [r["role_id"] for r in roles],
        "scopes": sorted(auth.scopes),
        "must_reset_password": auth.must_reset_password,
        "current_session_id": auth.session_id,
        "token_kind": auth.token_kind,
    }


@router.get("/me/sessions")
async def my_sessions(auth: AuthContext = Depends(current_user)) -> dict:
    store = get_store()
    rows = await store.list_auth_sessions(auth.user_id)

    items = [
        {
            "session_id": r["session_id"],
            "ip": r.get("ip"),
            "user_agent": r.get("user_agent"),
            "created_at": r.get("created_at"),
            "last_used_at": r.get("last_used_at"),
            "expires_at": r.get("sliding_expires_at"),
            "is_current": r["session_id"] == auth.session_id,
            # 在线判定看的是这个 session 自己的最后使用时间，不是用户的 last_seen_at
            "is_online": SecurityStoreMixin.is_online(
                {"last_seen_at": r.get("last_used_at")}
            ),
        }
        for r in rows
    ]
    # 两次排序：先按最近使用倒序，再把当前 session 置顶。
    # Python 的 sort 是稳定的，第二次排序不会打乱第一次的相对顺序。
    items.sort(key=lambda x: x["last_used_at"] or "", reverse=True)
    items.sort(key=lambda x: not x["is_current"])
    return {"sessions": items, "total": len(items)}
