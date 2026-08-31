"""/api/security/roles + /api/security/permissions 只读接口（S9）。

方案见 ``docs/security-mvp-plan-2026-08-29.md`` §3.5：**v1.1 只做读**，角色的
增删改推迟到 v1.2——内置 4 角色是种子数据，改它等于改权限模型，需要配套的
迁移与审计，不值得在 MVP 里塞进来。

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/security/roles` | `security.roles.read` |
| GET | `/api/security/permissions` | `security.roles.read` |

一次返回角色 + 权限 + 归属关系，前端权限矩阵（S16）不用发三次请求。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from api.security.deps import AuthContext, get_store, require_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/security/roles", tags=["security"])
perm_router = APIRouter(prefix="/api/security/permissions", tags=["security"])


@router.get("")
async def list_roles(
    auth: AuthContext = Depends(require_scope("security.roles.read")),
) -> dict:
    """角色列表，每项带它持有的 perm_id 列表。"""
    store = get_store()
    matrix = await store.get_permission_matrix()
    roles = await store.list_roles()

    items = [
        {
            "role_id": r["role_id"],
            "name": r["name"],
            "description": r["description"],
            "is_builtin": bool(r.get("is_builtin")),
            # is_assignable=0 → UI 不把它列进可绑定下拉（owner 只能有一个）
            "is_assignable": bool(r.get("is_assignable")),
            "permissions": matrix.get(r["role_id"], []),
            "permission_count": len(matrix.get(r["role_id"], [])),
        }
        for r in roles
    ]
    return {"roles": items, "total": len(items)}


@perm_router.get("")
async def list_permissions(
    auth: AuthContext = Depends(require_scope("security.roles.read")),
) -> dict:
    """全部权限点 + 角色归属，前端画矩阵用。"""
    store = get_store()
    perms = await store.list_permissions()
    matrix = await store.get_permission_matrix()

    by_perm: dict[str, list[str]] = {p["perm_id"]: [] for p in perms}
    for role_id, perm_ids in matrix.items():
        for pid in perm_ids:
            by_perm.setdefault(pid, []).append(role_id)

    items = [
        {
            "perm_id": p["perm_id"],
            "resource": p.get("resource"),
            "action": p.get("action"),
            "description": p.get("description"),
            "roles": sorted(by_perm.get(p["perm_id"], [])),
        }
        for p in perms
    ]
    return {"permissions": items, "total": len(items)}
