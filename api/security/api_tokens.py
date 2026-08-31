"""/api/security/api-tokens 端点（S10）。

方案见 ``docs/security-mvp-plan-2026-08-29.md`` §3.5。

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/security/api-tokens` | `security.api_tokens.read`，**或看自己的** |
| POST | `/api/security/api-tokens` | 给别人建要 `security.api_tokens.write`；给自己建只要求已登录 |
| DELETE | `/api/security/api-tokens/{id}` | `security.api_tokens.write`，**或删自己的** |
| POST | `/api/security/api-tokens/{id}/rotate` | 同上 |

三处方案没写但必须补的约束
    1. **scopes 必须是创建者自身 scopes 的子集**。不校验的话一个只读 viewer
       可以给自己签一个带 ``runs.write`` 的 PAT，直接绕过角色体系。
    2. **强制过期**：``expires_in_days`` 只能取 30 / 90 / 365，不接任意值。
       永不过期的凭证一旦泄露就是永久后门。
    3. **响应永不出现 token_hash**。``prefix`` / ``last4`` 是给 UI 识别用的，
       hash 出去了等于把整个库交给对方去离线爆破。

自服务的边界
    "给自己建/删/轮换"只要求是**已登录用户**，不要求 ``security.api_tokens.*``
    权限——因为 PAT 的 scopes 被限制在自己的子集里，签出来也不会有超出自身
    的能力，不构成提权。这也顺带解决了种子数据里 developer 只有
    ``security.api_tokens.write`` 没有 ``.read`` 的尴尬：他能建，也能列自己的。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.security.deps import AuthContext, current_user, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/security/api-tokens", tags=["security"])

READ_SCOPE = "security.api_tokens.read"
WRITE_SCOPE = "security.api_tokens.write"

# 强制过期档位。不接受任意值：永不过期的 token 泄露后无法自愈
ALLOWED_EXPIRY_DAYS = (30, 90, 365)

# 永不出网的列
_TOKEN_SECRET_FIELDS = ("token_hash",)


class CreateTokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int = 30
    # 不传 = 给自己建；传了别人 = 需要 security.api_tokens.write
    user_id: str | None = None


class RotateTokenRequest(BaseModel):
    """轮换只能改名字，scopes 与过期策略沿用原 token（方案 §3.5）。"""

    name: str | None = None


# ============================================================
# 工具
# ============================================================

def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": code, "message": message},
    )


def _public_token(row: dict[str, Any]) -> dict[str, Any]:
    """脱敏 token 行。``token_hash`` 永不出网。"""
    out = {k: v for k, v in row.items() if k not in _TOKEN_SECRET_FIELDS}
    out["scopes"] = sorted((row.get("scopes") or "").split())
    out["revoked"] = bool(row.get("revoked_at"))
    return out


def _target_user_id(auth: AuthContext, requested: str | None) -> str:
    """决定操作对象：不给就是自己；给别人要求对应 scope。"""
    if not requested or requested == auth.user_id:
        return auth.user_id
    if not auth.has(WRITE_SCOPE):
        raise _forbidden(
            "not_your_token",
            f"查看/管理他人的 API 令牌需要 {WRITE_SCOPE}",
        )
    return requested


async def _load_token_or_404(store: Any, token_id: str) -> dict[str, Any]:
    row = await store.get_api_token(token_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="token_not_found"
        )
    return row


def _assert_can_touch(auth: AuthContext, row: dict[str, Any]) -> None:
    """自己的 token 随便动；别人的要有 write scope。"""
    if row["user_id"] == auth.user_id:
        return
    if not auth.has(WRITE_SCOPE):
        raise _forbidden(
            "not_your_token", f"操作他人的 API 令牌需要 {WRITE_SCOPE}"
        )


def _validate_scopes(requested: list[str], allowed: frozenset[str]) -> str:
    """scopes 必须是 ``allowed`` 的子集，防止签出超能力 token。"""
    if "*" in allowed:                       # dev 绕过模式：不校验
        return " ".join(sorted(set(requested)))
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "scope_not_granted",
                "message": "不能签发超出被授权范围的令牌",
                "not_granted": unknown,
            },
        )
    return " ".join(sorted(set(requested)))


# ============================================================
# 列表 / 创建
# ============================================================

@router.get("")
async def list_tokens(
    user_id: str | None = None,
    auth: AuthContext = Depends(current_user),
) -> dict:
    """自己的 token 列表。带 ``?user_id=`` 看别人的需要 ``security.api_tokens.read``。"""
    store = get_store()
    if user_id and user_id != auth.user_id and not auth.has(READ_SCOPE):
        raise _forbidden(
            "not_your_token", f"查看他人的 API 令牌需要 {READ_SCOPE}"
        )
    target = user_id or auth.user_id

    rows = await store.list_api_tokens(target)
    return {"tokens": [_public_token(r) for r in rows], "total": len(rows)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_token(
    body: CreateTokenRequest,
    auth: AuthContext = Depends(current_user),
) -> dict:
    store = get_store()
    target = _target_user_id(auth, body.user_id)

    if body.expires_in_days not in ALLOWED_EXPIRY_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_expiry",
                "message": "过期天数必须是 30 / 90 / 365，不支持永不过期",
                "allowed": list(ALLOWED_EXPIRY_DAYS),
            },
        )

    if target == auth.user_id:
        # 给自己建：不给 scopes 就默认全量继承自己的；给了就校验子集
        scope_str = (
            " ".join(sorted(auth.scopes))
            if not body.scopes
            else _validate_scopes(body.scopes, auth.scopes)
        )
    else:
        # 给别人建：scopes 必须**同时**是调用方和目标用户权限的子集。
        #
        # 只校验调用方是不够的——admin 持有几乎全部权限，可以给一个 viewer
        # 签出 runs.write 的 PAT，等于绕过了「admin 没有 security.roles.write、
        # 不能改别人角色」这条防提权设计。所以两侧都要卡。
        #
        # 同时要求显式给 scopes：不给就静默授予目标用户的全部权限，太意外。
        if not body.scopes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "scopes_required",
                    "message": "给别人创建令牌时必须显式指定 scopes",
                },
            )
        target_scopes = frozenset(
            (await store.compute_user_scopes(target)).split()
        )
        scope_str = _validate_scopes(body.scopes, auth.scopes & target_scopes)

    raw, row = await store.create_api_token(
        target, body.name, scopes=scope_str,
        expires_in_days=body.expires_in_days,
    )
    logger.info(
        "[security] 创建 PAT %s for %s by %s scopes=%d",
        row.get("token_id"), target, auth.user_id, len(scope_str.split()),
    )
    return {
        # 明文只在这一次响应里出现，之后再无任何途径取回
        "token": raw,
        "token_row": _public_token(row),
        "warning": "明文令牌只显示这一次，请立即保存。",
    }


# ============================================================
# 撤销 / 轮换
# ============================================================

@router.delete("/{token_id}")
async def revoke_token(
    token_id: str,
    auth: AuthContext = Depends(current_user),
) -> dict:
    store = get_store()
    row = await _load_token_or_404(store, token_id)
    _assert_can_touch(auth, row)

    if row.get("revoked_at"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already_revoked"
        )

    ok = await store.revoke_api_token(
        token_id, revoked_by_user_id=_actor(auth), reason="revoked_by_user"
    )
    logger.warning("[security] 撤销 PAT %s by %s", token_id, auth.user_id)
    return {
        "revoked": ok,
        "token_id": token_id,
        "message": "已撤销，不可恢复。",
    }


@router.post("/{token_id}/rotate")
async def rotate_token(
    token_id: str,
    body: RotateTokenRequest | None = None,
    auth: AuthContext = Depends(current_user),
) -> dict:
    """轮换：保留 scopes 与过期**时长**，换一个新明文。

    原 token 立即撤销，新旧之间无重叠窗口。
    """
    store = get_store()
    old = await _load_token_or_404(store, token_id)
    _assert_can_touch(auth, old)

    if old.get("revoked_at"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="cannot_rotate_revoked"
        )

    from api.security.rate_limit import parse_iso
    from datetime import datetime, timezone

    expires_at = parse_iso(old.get("expires_at"))
    if not expires_at:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="token_expires_at_missing",
        )
    # 按**剩余时长**续，不是从今天重新算——否则反复轮换可以无限续期，
    # 让"强制过期"形同虚设
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    days = max(1, int(remaining // 86400))

    name = (body.name if body and body.name else old["name"])
    raw, new_row = await store.create_api_token(
        old["user_id"], name, scopes=old.get("scopes") or "", expires_in_days=days
    )
    await store.revoke_api_token(
        token_id, revoked_by_user_id=_actor(auth), reason=f"rotated_to_{new_row['token_id']}"
    )
    logger.warning(
        "[security] 轮换 PAT %s -> %s by %s", token_id, new_row["token_id"],
        auth.user_id,
    )
    return {
        "token": raw,
        "token_row": _public_token(new_row),
        "rotated_from": token_id,
        "warning": "明文令牌只显示这一次；旧令牌已立即失效。",
    }


def _actor(auth: AuthContext) -> str | None:
    """审计字段的操作者 id。dev 绕过模式下 ``user_dev_bypass`` 不是真实用户行，
    写进 ``revoked_by_user_id`` 会撞外键，降级成 NULL。
    """
    return None if auth.token_kind == "bypass" else auth.user_id
