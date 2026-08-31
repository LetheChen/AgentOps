"""/api/security/users 端点（S8）。

方案见 ``docs/security-mvp-plan-2026-08-29.md`` §3.5。

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/security/users` | `security.users.read` |
| POST | `/api/security/users` | `security.users.write` |
| PATCH | `/api/security/users/{id}` | `security.users.write` |
| DELETE | `/api/security/users/{id}` | `security.users.write`（软删） |
| POST | `/api/security/users/{id}/lock` | `security.users.write` |
| POST | `/api/security/users/{id}/unlock` | `security.users.write` |
| POST | `/api/security/users/{id}/password` | `security.users.write` |
| POST | `/api/security/users/{id}/roles` | `security.roles.write` |
| DELETE | `/api/security/users/{id}/roles/{role_id}` | `security.roles.write` |
| POST | `/api/security/users/{id}/revoke-all` | `security.users.write` |

两处**方案没写但必须加**的护栏
    1. **owner 不能被禁用 / 软删**。DB 触发器 ``prevent_owner_user_delete`` 只拦
       ``DELETE FROM users``，而本模块的 DELETE 是**软删（UPDATE disabled_at）**，
       触发器管不着。不拦的话一条 PATCH 就能把唯一 owner 打昏，整个安全模块
       再也没人能管理 → 永久性锁死。
    2. **不能对自己做破坏性操作**（禁用 / 软删 / 锁定 / 解绑自己的角色）。
       不拦的话 admin 手滑一下就把自己踢出系统，还得进数据库手工修。

另一个方案没定的点：**lock 只挡新登录，不吊销已有凭证**。
    理由是"临时锁定"——30 分钟后用户自己就能重新登录，没必要把人家正在用的
    会话全打断。真要立刻踢人用 ``/revoke-all`` 或直接禁用，两个动作分开更可控。
    响应里带 ``note`` 明确提示这一点，避免运维误以为锁了就万事大吉。

注意：``users`` 表还留着 v2 时代的 ``role`` / ``is_active`` / ``metadata`` 三个
历史列，本模块**一律不使用**——角色一律走 ``security_user_roles``，启停一律走
``disabled_at``。两套并存会立刻不同步。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.security.deps import (
    AuthContext,
    get_store,
    public_user,
    require_scope,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/security/users", tags=["security"])

OWNER_ROLE = "role_owner"
MIN_PASSWORD_LEN = 8
LOCK_SEC = 30 * 60                       # 方案 §3.5：临时锁定 30 分钟
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


# ============================================================
# 请求体
# ============================================================

class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=256)
    display_name: str = ""
    email: str = ""
    role_ids: list[str] = Field(default_factory=list)


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    email: str | None = None
    disabled: bool | None = None


class PasswordRequest(BaseModel):
    new_password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=256)


class RoleBindingRequest(BaseModel):
    role_id: str = Field(min_length=1, max_length=64)


# ============================================================
# 内部护栏
# ============================================================

def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": code, "message": message},
    )


def _bad(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": code, "message": message},
    )


def _assert_not_self(auth: AuthContext, user_id: str, action: str) -> None:
    """禁止对自己做破坏性操作。

    只读和改自己资料不在此列——那些走 ``/api/auth/*``。
    """
    if auth.user_id == user_id:
        raise _bad(
            "cannot_modify_self",
            f"不能对自己执行「{action}」，请用另一个管理员账号操作",
        )


async def _active_owner_ids(store: Any) -> set[str]:
    """未被禁用的 owner 列表。"""
    return set(await store.list_role_members(OWNER_ROLE))


async def _assert_not_last_owner(store: Any, user_id: str, action: str) -> None:
    """禁止对**最后一个活跃 owner** 做会让其失去管理能力的操作。"""
    owners = await _active_owner_ids(store)
    if user_id in owners and len(owners) <= 1:
        raise _conflict(
            "last_owner_protected",
            f"不能对「{action}」唯一的活跃 owner：之后将无人能管理安全设置。"
            "请先把 role_owner 转给别的用户。",
        )


async def _load_user_or_404(store: Any, user_id: str) -> dict[str, Any]:
    user = await store.get_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found"
        )
    return user


async def _user_out(store: Any, user: dict[str, Any]) -> dict[str, Any]:
    """统一的用户响应形状：脱敏 + roles + disabled + locked。

    列表 / 创建 / 更新三条路径都走这里，避免"列表里有 disabled、更新响应里没有"
    这种让前端得写两套解析的不一致。
    """
    out = public_user(user)
    out["roles"] = [
        r["role_id"] for r in await store.list_user_roles(user["user_id"])
    ]
    out["disabled"] = bool(user.get("disabled_at"))
    out["locked"] = await store.is_login_locked(
        f"user:{user.get('username') or ''}"
    )
    return out


def _actor(auth: AuthContext) -> str | None:
    """审计字段里的操作者 id。

    ``granted_by`` / ``by_user_id`` / ``revoked_by_user_id`` 三列都有
    ``REFERENCES users(user_id)`` 外键，而 dev 绕过模式下的 ``user_dev_bypass``
    不是真实用户行，写进去直接 IntegrityError。这里统一降级成 NULL。
    """
    return None if auth.token_kind == "bypass" else auth.user_id


def _slug_user_id(username: str) -> str:
    """``user_<username>``（与 bootstrap 建的 ``user_admin`` 保持同一约定）。

    username 已过 ``_USERNAME_RE`` 校验，只含 ``[a-zA-Z0-9._-]``，直接拼安全。
    """
    return "user_" + username.lower()


# ============================================================
# 列表 / 创建
# ============================================================

@router.get("")
async def list_users(auth: AuthContext = Depends(require_scope("security.users.read"))) -> dict:
    store = get_store()
    rows = await store.list_users(include_disabled=True)
    items = [await _user_out(store, u) for u in rows]
    return {"users": items, "total": len(items)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    auth: AuthContext = Depends(require_scope("security.users.write")),
) -> dict:
    store = get_store()

    if not _USERNAME_RE.match(body.username):
        raise _bad(
            "invalid_username", "用户名只允许字母、数字、点、下划线、连字符"
        )
    if await store.get_user_by_username(body.username):
        raise _conflict("username_taken", f"用户名 {body.username!r} 已存在")

    # 不允许借建用户接口给自己/别人挂 owner：owner 必须由人工在数据库里转移，
    # 否则任何 admin 都能一步把自己变成 owner（admin 缺 security.roles.write，
    # 走不了 /roles 端点，但建用户接口只要求 security.users.write）。
    if OWNER_ROLE in body.role_ids:
        raise _bad(
            "owner_role_not_grantable",
            "不能通过创建用户接口授予 role_owner；请由现有 owner 在角色管理里转移",
        )

    valid = {r["role_id"] for r in await store.list_roles()}
    unknown = [r for r in body.role_ids if r not in valid]
    if unknown:
        raise _bad("unknown_role", f"未知角色: {unknown}")

    user_id = _slug_user_id(body.username)
    if await store.get_user(user_id):
        # 极端情况：同名不同大小写。直接报冲突，不静默改名
        raise _conflict("user_id_taken", f"user_id {user_id!r} 已存在")

    await store.create_user(
        user_id,
        body.username,
        body.password,
        display_name=body.display_name,
        email=body.email,
        must_reset_password=True,
    )
    # dev 绕过模式下的 user_id 不是真实用户，写进 granted_by 会撞外键
    granted_by = None if auth.token_kind == "bypass" else auth.user_id
    for role_id in body.role_ids:
        await store.bind_user_role(user_id, role_id, granted_by=granted_by)

    logger.info(
        "[security] 创建用户 %s by %s roles=%s", user_id, auth.user_id, body.role_ids
    )
    return await _user_out(store, await _load_user_or_404(store, user_id))


# ============================================================
# 改资料 / 启停 / 软删
# ============================================================

@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    auth: AuthContext = Depends(require_scope("security.users.write")),
) -> dict:
    store = get_store()
    user = await _load_user_or_404(store, user_id)

    fields: dict[str, Any] = {}
    if body.display_name is not None:
        fields["display_name"] = body.display_name
    if body.email is not None:
        fields["email"] = body.email
    if fields:
        await store.update_user(user_id, **fields)

    if body.disabled is not None:
        _assert_not_self(auth, user_id, "禁用" if body.disabled else "启用")
        if body.disabled:
            if user.get("disabled_at"):
                raise _conflict("already_disabled", "该用户已处于禁用状态")
            await _assert_not_last_owner(store, user_id, "禁用")
            await store.set_user_disabled(
                user_id, True, by_user_id=_actor(auth), reason=f"by {auth.user_id}"
            )
        else:
            if not user.get("disabled_at"):
                raise _conflict("not_disabled", "该用户当前不是禁用状态")
            await store.set_user_disabled(user_id, False, by_user_id=_actor(auth))

    logger.info("[security] 更新用户 %s by %s fields=%s", user_id, auth.user_id,
                sorted(fields) + (["disabled"] if body.disabled is not None else []))

    return await _user_out(store, await _load_user_or_404(store, user_id))


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    auth: AuthContext = Depends(require_scope("security.users.write")),
) -> dict:
    """软删：``disabled_at = now``，并级联吊销该用户全部 session + PAT。

    **不做物理删除**——``users`` 表被 runs / sessions 等业务数据外键引用，
    硬删会连带业务历史一起消失。
    """
    store = get_store()
    user = await _load_user_or_404(store, user_id)

    _assert_not_self(auth, user_id, "删除")
    await _assert_not_last_owner(store, user_id, "删除")

    if user.get("disabled_at"):
        raise _conflict("already_disabled", "该用户已处于禁用状态")

    await store.set_user_disabled(
        user_id, True, by_user_id=_actor(auth), reason="soft_delete"
    )
    logger.warning("[security] 软删用户 %s by %s", user_id, auth.user_id)
    return {
        "deleted": True,
        "user_id": user_id,
        "soft": True,
        "message": "已软删（disabled_at=now）并级联吊销全部 session/PAT；"
                   "业务历史数据保留。",
    }


# ============================================================
# 锁定 / 解锁
# ============================================================

@router.post("/{user_id}/lock")
async def lock_user(
    user_id: str,
    auth: AuthContext = Depends(require_scope("security.users.write")),
) -> dict:
    store = get_store()
    user = await _load_user_or_404(store, user_id)
    _assert_not_self(auth, user_id, "锁定")

    username = user.get("username")
    if not username:
        raise _bad("no_username", "该用户没有 username，无法通过登录限流表锁定")

    locked_until = await store.lock_user_login(username, lock_sec=LOCK_SEC)
    logger.warning("[security] 锁定用户 %s 至 %s by %s", user_id, locked_until,
                   auth.user_id)
    return {
        "locked": True,
        "user_id": user_id,
        "locked_until": locked_until,
        "lock_sec": LOCK_SEC,
        "note": "只阻止**新登录**；已签发的 session/PAT 仍可用。"
                "要立即失效请调 POST /api/security/users/{id}/revoke-all。",
    }


@router.post("/{user_id}/unlock")
async def unlock_user(
    user_id: str,
    auth: AuthContext = Depends(require_scope("security.users.write")),
) -> dict:
    """提前解除锁定（方案 §3.5 没有此端点，但没有它锁定就只能干等 30 分钟）。"""
    store = get_store()
    user = await _load_user_or_404(store, user_id)
    username = user.get("username")
    if not username:
        raise _bad("no_username", "该用户没有 username")

    ok = await store.unlock_user_login(username)
    logger.info("[security] 解锁用户 %s by %s (cleared=%s)", user_id, auth.user_id, ok)
    return {"unlocked": True, "user_id": user_id, "had_lock": ok}


# ============================================================
# 重置密码
# ============================================================

@router.post("/{user_id}/password")
async def reset_password(
    user_id: str,
    body: PasswordRequest,
    auth: AuthContext = Depends(require_scope("security.users.write")),
) -> dict:
    """admin 重置别人密码：强制 ``must_reset_password=1`` + 吊销全部凭证。"""
    store = get_store()
    await _load_user_or_404(store, user_id)

    await store.set_user_password(user_id, body.new_password)
    await store.update_user(user_id, must_reset_password=1)

    sessions = await store.revoke_all_sessions(
        user_id, revoked_by_user_id=_actor(auth), reason="password_reset"
    )
    tokens = await store.revoke_all_api_tokens(
        user_id, revoked_by_user_id=_actor(auth), reason="password_reset"
    )
    logger.warning(
        "[security] 重置密码 %s by %s sessions=%d tokens=%d",
        user_id, auth.user_id, sessions, tokens,
    )
    return {
        "ok": True,
        "user_id": user_id,
        "must_reset_password": True,
        "revoked_sessions": sessions,
        "revoked_api_tokens": tokens,
    }


# ============================================================
# 角色绑定
# ============================================================

@router.post("/{user_id}/roles")
async def bind_role(
    user_id: str,
    body: RoleBindingRequest,
    auth: AuthContext = Depends(require_scope("security.roles.write")),
) -> dict:
    """绑定角色。要求 ``security.roles.write``——**admin 没有这个权限**
    （角色表设计如此），避免管理员把自己提到 owner 等价权限。
    """
    store = get_store()
    await _load_user_or_404(store, user_id)

    if not await store.get_role(body.role_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="role_not_found"
        )
    if body.role_id == OWNER_ROLE:
        raise _bad(
            "owner_role_not_grantable",
            "不能通过接口授予 role_owner（会造成多个 owner 互相解绑的僵局）；"
            "owner 转移请在数据库里手工完成并留下审计记录",
        )

    await store.bind_user_role(user_id, body.role_id, granted_by=_actor(auth))
    logger.info("[security] 绑定角色 %s -> %s by %s", user_id, body.role_id,
                auth.user_id)
    roles = await store.list_user_roles(user_id)
    return {"user_id": user_id, "roles": [r["role_id"] for r in roles]}


@router.delete("/{user_id}/roles/{role_id}")
async def unbind_role(
    user_id: str,
    role_id: str,
    auth: AuthContext = Depends(require_scope("security.roles.write")),
) -> dict:
    """解绑角色。``role_owner`` 会被 DB 触发器 ``prevent_owner_unbind`` 拒绝。"""
    store = get_store()
    await _load_user_or_404(store, user_id)

    if role_id == OWNER_ROLE:
        # 触发器会拦，但触发器抛的是裸 sqlite3.IntegrityError（500），
        # 在这里先拦掉才能给出可读的 409
        raise _conflict(
            "owner_role_protected",
            "不能解绑 role_owner（DB 触发器 prevent_owner_unbind 也会拒绝）",
        )
    if role_id not in {r["role_id"] for r in await store.list_user_roles(user_id)}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="role_binding_not_found"
        )

    await store.unbind_user_role(user_id, role_id)
    logger.warning("[security] 解绑角色 %s -/-> %s by %s", user_id, role_id,
                   auth.user_id)

    # 降权不回收已签发凭证（D15）：明确提示运维还有这一步要做
    return {
        "user_id": user_id,
        "roles": [r["role_id"] for r in await store.list_user_roles(user_id)],
        "warning": "降权不会自动回收已签发的 token（旧 scope 仍嵌在 session/PAT 里）。"
                   "如需立即生效，请调 POST /api/security/users/{id}/revoke-all。",
    }


# ============================================================
# 一次性吊销全部凭证（B10）
# ============================================================

@router.post("/{user_id}/revoke-all")
async def revoke_all(
    user_id: str,
    auth: AuthContext = Depends(require_scope("security.users.write")),
) -> dict:
    """一次性吊销该用户的全部 session + PAT（D15 的运维兜底）。

    典型场景：人员离职、token 疑似泄露、刚做完降权需要让旧 scope 立刻失效。
    """
    store = get_store()
    await _load_user_or_404(store, user_id)

    sessions = await store.revoke_all_sessions(
        user_id, revoked_by_user_id=_actor(auth), reason="revoke_all"
    )
    tokens = await store.revoke_all_api_tokens(
        user_id, revoked_by_user_id=_actor(auth), reason="revoke_all"
    )
    logger.warning(
        "[security] 吊销全部凭证 user=%s by %s sessions=%d tokens=%d",
        user_id, auth.user_id, sessions, tokens,
    )
    return {
        "user_id": user_id,
        "revoked_sessions": sessions,
        "revoked_api_tokens": tokens,
        "message": "已吊销全部登录会话与 API 令牌，不可恢复。",
    }
