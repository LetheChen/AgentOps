#!/usr/bin/env python3
"""AgentOps 首次启动初始化脚本（容器 init 服务内调用）。

职责：
  1. 触发 SqliteEventStore 自动建表（33 张表）
  2. 若 admin 不存在，自动创建 admin 账号
     - 若 AGENTOPS_BOOTSTRAP_PASSWORD 环境变量存在 → 用该密码
     - 否则 → 生成 16 位随机密码，写入 /app/data/bootstrap-password.txt

使用：
  docker compose --profile init run --rm init python /docker-init/init_admin.py
  # 或本地：
  python docker/agentops-api/init_admin.py
"""
from __future__ import annotations

import asyncio
import os
import secrets
import string
import sys
from pathlib import Path


async def main() -> int:
    # 路径设置（容器内 /app 是工作目录，业务代码已经在 /app 下）
    db_path = os.environ.get("AUDIT_DB_PATH", "/app/data/audit.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # 触发 SqliteEventStore 自动建表（__init__ 里会调用 ensure_schema）
    from audit import SqliteEventStore
    store = SqliteEventStore(db_path)
    print(f"[init] EventStore initialized at {db_path}")

    # 创建 admin 账号（async）
    sec = store  # SqliteEventStore 已经 mixin 了 SecurityStoreMixin
    admin_name = os.environ.get("AGENTOPS_BOOTSTRAP_USERNAME", "admin")
    existing = await sec.get_user_by_username(admin_name)
    if existing:
        print(f"[init] user '{admin_name}' already exists, skip creation")
        return 0

    password = os.environ.get("AGENTOPS_BOOTSTRAP_PASSWORD", "").strip()
    random_generated = False
    if not password:
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(16))
        random_generated = True

    user_id = f"usr_{secrets.token_hex(8)}"
    await sec.create_user(
        user_id=user_id,
        username=admin_name,
        password=password,
        must_reset_password=1,
    )

    # 给 admin 绑定 owner 角色（若 role_owner 存在）
    try:
        role = await sec.get_role_by_name("owner")
        if role:
            await sec.add_role_member(role["role_id"], user_id)
            print(f"[init] admin '{admin_name}' bound to owner role")
    except Exception as e:
        # 角色表未就绪或 add_role_member 签名不符，不阻塞主流程
        print(f"[init] WARN: could not bind owner role: {e}")

    if random_generated:
        bootstrap_file = "/app/data/bootstrap-password.txt"
        with open(bootstrap_file, "w", encoding="utf-8") as f:
            f.write(
                "============================================================\n"
                f"[bootstrap] Initial admin created (SAVE NOW):\n"
                f"  username: {admin_name}\n"
                f"  password: {password}\n"
                f"[bootstrap] must_reset_password=1 -> forced change on first login\n"
                "============================================================\n"
            )
        try:
            os.chmod(bootstrap_file, 0o600)
        except Exception:
            pass
        print(f"[init] random password written to {bootstrap_file}")
        print(f"[init] admin password: {password}")
    else:
        print(f"[init] admin '{admin_name}' created with env-provided password")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
