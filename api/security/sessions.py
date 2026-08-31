"""/api/security/sessions 端点（S11）。

方案见 ``docs/security-mvp-plan-2026-08-29.md`` §3.5。

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/security/sessions` | `security.sessions.read`，**或看自己的** |
| DELETE | `/api/security/sessions/{id}` | 仅自己的（v1.1 不开他人的） |

与 ``/api/auth/me/sessions``（S7）的区别
    那个是"我的会话"快捷入口，永远只返回自己，不要求任何 scope——登录后的
    UI 要用它做"注销其他设备"。这个模块是管理面：带 scope 的人可以看别人的，
    但 MVP 阶段 **DELETE 只放行自己的**，管理他人会话推迟到 v1.1
    （方案 §3.5 明确：v1.1 才允许 admin 看他人、且 v1.1 的写也只到 self）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from api.security.deps import AuthContext, current_user, get_store
from audit.security_store import SecurityStoreMixin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/security/sessions", tags=["security"])

READ_SCOPE = "security.sessions.read"

# 永不出网的列
_SESSION_SECRET_FIELDS = ("token_hash",)


def _public_session(row: dict[str, Any], current_session_id: str | None) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in _SESSION_SECRET_FIELDS}
    out["is_current"] = row["session_id"] == current_session_id
    # 在线看这个 session 自己的最后使用时间，不是用户的 last_seen_at
    out["is_online"] = SecurityStoreMixin.is_online(
        {"last_seen_at": row.get("last_used_at")}
    )
    out["revoked"] = bool(row.get("revoked_at"))
    return out


# ============================================================
# 列表 / 撤销
# ============================================================

@router.get("")
async def list_sessions(
    user_id: str | None = None,
    auth: AuthContext = Depends(current_user),
) -> dict:
    """会话列表。默认自己；``?user_id=`` 看他人需要 ``security.sessions.read``。"""
    store = get_store()
    if user_id and user_id != auth.user_id and not auth.has(READ_SCOPE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "not_your_session",
                "message": f"查看他人的登录会话需要 {READ_SCOPE}",
            },
        )
    target = user_id or auth.user_id

    rows = await store.list_auth_sessions(target)
    items = [_public_session(r, auth.session_id) for r in rows]
    # 当前 session 置顶，其余按最近使用倒序（稳定排序，两次 sort 不冲突）
    items.sort(key=lambda x: x.get("last_used_at") or "", reverse=True)
    items.sort(key=lambda x: not x["is_current"])
    return {"sessions": items, "total": len(items)}


@router.delete("/{session_id}")
async def revoke_session(
    session_id: str,
    auth: AuthContext = Depends(current_user),
) -> dict:
    """撤销会话。**MVP 只放行自己的**——注销他人设备是管理动作，v1.1 再开。"""
    store = get_store()
    row = await store.get_auth_session(session_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found"
        )
    if row["user_id"] != auth.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "not_your_session",
                "message": "只能注销自己的会话；注销他人会话将在 v1.1 提供",
            },
        )
    if row.get("revoked_at"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already_revoked"
        )

    ok = await store.revoke_auth_session(
        session_id,
        revoked_by_user_id=None if auth.token_kind == "bypass" else auth.user_id,
        reason="revoked_by_user",
    )
    logger.info("[security] 注销会话 %s by %s", session_id, auth.user_id)
    return {"revoked": ok, "session_id": session_id}
