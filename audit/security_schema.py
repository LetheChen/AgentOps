"""安全认证访问模块 — 数据库 schema 迁移 + 首次启动 bootstrap（S1 / S2 / S3）。

设计来源：``docs/security-mvp-plan-2026-08-29.md``（v1.2 事实核查版）

落地位置
--------
``audit/store.py`` 的 ``SqliteEventStore.__init__`` 在 ``_migrate_v2_to_v3()`` 之后调用::

    migrate_security_schema(self._conn)
    bootstrap_first_user(self._conn)

版本控制
--------
用 SQLite 原生 ``PRAGMA user_version``（= 91）。

.. note::
   项目里**不存在** ``schema_version`` 表（grep ``audit/store.py`` 0 命中）。
   方案文档 v1.1 曾假设有这张表，照抄会直接
   ``sqlite3.OperationalError: no such table: schema_version``。

时间戳
------
统一走 :func:`_now_iso`（``datetime.now(timezone.utc)``），
遵守 AGENTS.md 强制约定「不要用已弃用的 ``datetime.utcnow()``」。
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 写入 PRAGMA user_version。现有 audit.db 的 user_version 实测为 0（从未设置）。
#
# 版本步进：
#   91 → security MVP 初始 schema（users 扩展 + 7 张新表 + 种子数据）
#   92 → 补 users 删除保护触发器（见 _migrate_v92 的 SQLite 行为陷阱说明）
#   93 → S13 路径级鉴权：补齐业务域权限（26 → 45 条）+ 角色重新绑定
SECURITY_SCHEMA_VERSION = 93


# ============================================================
# 工具函数
# ============================================================

def _now_iso() -> str:
    """UTC 时间戳（带时区）。AGENTS.md 强制约定：不用 datetime.utcnow()。"""
    return datetime.now(timezone.utc).isoformat()


_HASHER: Any = None


def _get_hasher() -> Any:
    """惰性构造 argon2 PasswordHasher（进程内单例）。

    参数取 OWASP 推荐档：m=64MiB, t=3, p=4。
    单次 hash/verify 约 50-100ms，登录场景可接受；PAT 每请求校验的开销由
    D20 的 60s 进程内缓存兜底（S4 实现）。
    """
    global _HASHER
    if _HASHER is None:
        try:
            from argon2 import PasswordHasher
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError(
                "缺少 argon2-cffi 依赖，无法进行密码哈希。"
                "安装：pip install 'argon2-cffi>=23.1'"
            ) from exc
        _HASHER = PasswordHasher(
            time_cost=3,
            memory_cost=65536,   # 64 MiB
            parallelism=4,
            hash_len=32,
            salt_len=16,
        )
    return _HASHER


def hash_password(plain: str) -> str:
    """明文密码 → argon2 hash。绝不明文落库。"""
    return _get_hasher().hash(plain)


def verify_password(hashed: str, plain: str) -> bool:
    """校验密码。hash 为空串 / 格式非法 / 不匹配一律返回 False。"""
    if not hashed:
        return False
    try:
        return _get_hasher().verify(hashed, plain)
    except Exception:
        # VerifyMismatchError / VerificationError / InvalidHash 统一吞掉
        return False


# ============================================================
# S1 · users 表扩展
# ============================================================

# SQLite 没有 ADD COLUMN IF NOT EXISTS，逐列用 PRAGMA table_info 检查后执行（幂等）
_USERS_ALTERS: tuple[str, ...] = (
    # 登录名。可空 → 存量行回填后再靠部分唯一索引约束
    "ALTER TABLE users ADD COLUMN username TEXT",
    # argon2 hash。NOT NULL DEFAULT '' 是迁移占位：空串永远 verify 不过
    "ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN must_reset_password INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN last_login_at TEXT",
    "ALTER TABLE users ADD COLUMN last_seen_at TEXT",
    # 软删时间戳；NULL = 有效，非空 = 已禁用
    "ALTER TABLE users ADD COLUMN disabled_at TEXT",
)

# users.role 是 v3 遗留的单值字段，MVP 不再使用（全部走 security_user_roles 多对多）。
# 这里不删它，避免破坏其他既有查询；代码层只读新表。


# ============================================================
# S1 · 7 张新表
# ============================================================

_SECURITY_DDL = """
-- 角色定义
CREATE TABLE IF NOT EXISTS security_roles (
    role_id           TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT,
    is_system         INTEGER NOT NULL DEFAULT 0,
    is_assignable     INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- 用户 <-> 角色 多对多（权限取并集）
CREATE TABLE IF NOT EXISTS security_user_roles (
    user_id     TEXT NOT NULL,
    role_id     TEXT NOT NULL,
    granted_by  TEXT REFERENCES users(user_id),
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, role_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (role_id) REFERENCES security_roles(role_id) ON DELETE CASCADE
);

-- 权限字典（资源 x 操作）
CREATE TABLE IF NOT EXISTS security_permissions (
    perm_id     TEXT PRIMARY KEY,
    resource    TEXT NOT NULL,
    action      TEXT NOT NULL,
    UNIQUE (resource, action)
);

-- 角色 <-> 权限 多对多（UI 的「权限矩阵」即本表）
CREATE TABLE IF NOT EXISTS security_role_permissions (
    role_id     TEXT NOT NULL,
    perm_id     TEXT NOT NULL,
    PRIMARY KEY (role_id, perm_id),
    FOREIGN KEY (role_id) REFERENCES security_roles(role_id) ON DELETE CASCADE,
    FOREIGN KEY (perm_id) REFERENCES security_permissions(perm_id) ON DELETE CASCADE
);

-- 登录会话（与「对话 session」是完全不同的东西，别混）
CREATE TABLE IF NOT EXISTS security_auth_sessions (
    session_id           TEXT PRIMARY KEY,      -- 32B hex，每次登录重新生成（防 session fixation）
    user_id              TEXT NOT NULL,
    token_hash           TEXT NOT NULL UNIQUE,  -- SHA-256(token)，仅作查询指纹；安全性来自 token 熵
    user_agent           TEXT,
    ip                   TEXT,                  -- 真实客户端 IP（容器部署见方案 §10）
    scope                TEXT NOT NULL DEFAULT '',   -- 登录时的权限快照
    created_at           TEXT NOT NULL,
    last_used_at         TEXT NOT NULL,
    absolute_expires_at  TEXT NOT NULL,         -- 绝对最长 30 天
    sliding_expires_at   TEXT NOT NULL,         -- 滑动：7 天无活动即失效
    revoked_at           TEXT,
    revoked_by_user_id   TEXT REFERENCES users(user_id),
    revoked_reason       TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- API 令牌（PAT：用户/服务调 AgentOps 自身 API 的 inbound 凭证）
-- 注意与「凭据管理」区分：后者是 AgentOps 调第三方 LLM 的 outbound 凭证
CREATE TABLE IF NOT EXISTS security_api_tokens (
    token_id            TEXT PRIMARY KEY,       -- 'pat_xxxxxxxx'
    user_id             TEXT NOT NULL,
    name                TEXT NOT NULL,
    token_hash          TEXT NOT NULL UNIQUE,   -- argon2 hash
    prefix              TEXT NOT NULL,          -- 12 位 base62，提升碰撞难度
    last4               TEXT NOT NULL,          -- UI 识别用
    scopes              TEXT NOT NULL DEFAULT '',   -- 创建时的权限子集快照
    expires_at          TEXT NOT NULL,          -- MVP 强制有过期时间，不允许 NULL
    last_used_at        TEXT,
    last_used_ip        TEXT,
    created_at          TEXT NOT NULL,
    revoked_at          TEXT,
    revoked_by_user_id  TEXT REFERENCES users(user_id),
    revoked_reason      TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- 登录失败计数（限流用）
CREATE TABLE IF NOT EXISTS security_login_attempts (
    key           TEXT PRIMARY KEY,   -- 'ip:1.2.3.4' 或 'user:username'
    failures      INTEGER NOT NULL DEFAULT 0,
    first_fail_at TEXT NOT NULL,
    locked_until  TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user    ON security_auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_active  ON security_auth_sessions(user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_api_tokens_user       ON security_api_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_active     ON security_api_tokens(user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_api_tokens_prefix     ON security_api_tokens(prefix);
CREATE INDEX IF NOT EXISTS idx_user_roles_role       ON security_user_roles(role_id);
CREATE INDEX IF NOT EXISTS idx_role_perms_perm       ON security_role_permissions(perm_id);
CREATE INDEX IF NOT EXISTS idx_login_attempts_locked ON security_login_attempts(locked_until);
"""

# §2.4 owner 不可解绑的 DB 级保证（UI 挡不住，就靠触发器）
_SECURITY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS prevent_owner_unbind
BEFORE DELETE ON security_user_roles
WHEN OLD.role_id = 'role_owner'
BEGIN
    SELECT RAISE(ABORT, 'cannot unbind role_owner; bootstrap-only operation');
END;
"""

# v92：owner 用户不可删除。
#
# ⚠️ SQLite 行为陷阱（实测确认，2026-08-29）：
#   外键的 ON DELETE CASCADE 级联删除**不会激活**目标表上的 DELETE 触发器。
#   即 `DELETE FROM users` 会级联清掉 security_user_roles 里的 owner 行，
#   但 prevent_owner_unbind 根本不触发 —— owner 保护被整个绕过。
#   （SQLite 官方：ON DELETE CASCADE 属于 foreign key action，非 DELETE 语句，不 fire trigger）
#
# 所以必须在 users 表上再加一道 BEFORE DELETE 触发器。
_SECURITY_TRIGGERS_V92 = """
CREATE TRIGGER IF NOT EXISTS prevent_owner_user_delete
BEFORE DELETE ON users
WHEN EXISTS (
    SELECT 1 FROM security_user_roles
    WHERE user_id = OLD.user_id AND role_id = 'role_owner'
)
BEGIN
    SELECT RAISE(ABORT, 'cannot delete owner user; transfer ownership first');
END;
"""


# ============================================================
# S2 · 内置种子数据
# ============================================================

# 权限字典（26 条）
_SEED_PERMISSIONS = """
INSERT OR IGNORE INTO security_permissions(perm_id, resource, action) VALUES
    ('sessions.read',            'sessions',            'read'),
    ('sessions.write',           'sessions',            'write'),
    ('sessions.cancel',          'sessions',            'cancel'),
    ('runs.read',                'runs',                'read'),
    ('runs.write',               'runs',                'write'),
    ('runs.cancel',              'runs',                'cancel'),
    ('workflows.read',           'workflows',           'read'),
    ('workflows.write',          'workflows',           'write'),
    ('workflows.cancel',         'workflows',           'cancel'),
    ('agents.read',              'agents',              'read'),
    ('agents.write',             'agents',              'write'),
    ('agents.invoke',            'agents',              'invoke'),
    ('credentials.read',         'credentials',         'read'),
    ('credentials.write',        'credentials',         'write'),
    ('knowledge.read',           'knowledge',           'read'),
    ('knowledge.write',          'knowledge',           'write'),
    ('security.users.read',      'security.users',      'read'),
    ('security.users.write',     'security.users',      'write'),
    ('security.roles.read',      'security.roles',      'read'),
    ('security.roles.write',     'security.roles',      'write'),
    ('security.api_tokens.read', 'security.api_tokens', 'read'),
    ('security.api_tokens.write','security.api_tokens', 'write'),
    ('security.sessions.read',   'security.sessions',   'read'),
    ('security.sessions.write',  'security.sessions',   'write'),
    ('system.read',              'system',              'read'),
    ('system.write',             'system',              'write');
"""

# 4 个内置角色。is_assignable=0 仅 owner —— UI 不把 owner 列进可绑定列表。
# 用 Python 元组而非 SQL 字符串，因为 created_at/updated_at 是 NOT NULL，
# 需要运行时时间戳参数化插入（SQL 里拼时间戳字符串不可接受）。
_BUILTIN_ROLES: tuple[tuple[str, str, str, int, int], ...] = (
    ("role_owner", "Owner", "超级管理员，拥有全部权限", 1, 0),
    ("role_admin", "Admin", "管理员，除 security.roles.write 外全部权限", 1, 1),
    ("role_developer", "Developer", "开发者，可读写 workflows/runs/agents", 1, 1),
    ("role_viewer", "Viewer", "只读所有资源", 1, 1),
)

# 角色 <-> 权限 绑定
_SEED_ROLE_PERMISSIONS = """
-- owner：全部
INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id)
SELECT 'role_owner', perm_id FROM security_permissions;

-- admin：全部，除 security.roles.write（防止管理员自我提权到 owner 等价）
INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id)
SELECT 'role_admin', perm_id FROM security_permissions
WHERE perm_id NOT IN ('security.roles.write');

-- developer：业务读写 + 自管 PAT + 只读系统配置
INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id) VALUES
    ('role_developer', 'sessions.read'),   ('role_developer', 'sessions.write'),
    ('role_developer', 'runs.read'),       ('role_developer', 'runs.write'),
    ('role_developer', 'runs.cancel'),
    ('role_developer', 'workflows.read'),  ('role_developer', 'workflows.write'),
    ('role_developer', 'workflows.cancel'),
    ('role_developer', 'agents.read'),     ('role_developer', 'agents.write'),
    ('role_developer', 'agents.invoke'),
    ('role_developer', 'credentials.read'),('role_developer', 'credentials.write'),
    ('role_developer', 'knowledge.read'),  ('role_developer', 'knowledge.write'),
    ('role_developer', 'security.api_tokens.write'),
    ('role_developer', 'system.read');

-- viewer：所有 read
INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id)
SELECT 'role_viewer', perm_id FROM security_permissions WHERE action = 'read';
"""


# ============================================================
# S1/S2 · 迁移入口
# ============================================================

def _migrate_v91(conn: sqlite3.Connection) -> None:
    """v91：users 扩展 + 7 张新表 + 索引 + 触发器 + 种子数据。"""
    cursor = conn.cursor()

    # 1) users 表扩展（逐列幂等；SQLite 无 ADD COLUMN IF NOT EXISTS）
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    for alter_sql in _USERS_ALTERS:
        col_name = alter_sql.split("ADD COLUMN ")[1].split()[0]
        if col_name not in existing_cols:
            cursor.execute(alter_sql)

    # 2) 部分唯一索引：SQLite 不支持 ADD CONSTRAINT UNIQUE，必须用索引实现
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
        "ON users(username) WHERE username IS NOT NULL"
    )

    # 3) 存量行回填登录名：优先 email，无则退回 user_id
    cursor.execute(
        "UPDATE users SET username = COALESCE(NULLIF(email, ''), user_id) "
        "WHERE username IS NULL"
    )

    # 4) 新表 + 索引 + 触发器
    cursor.executescript(_SECURITY_DDL)
    cursor.executescript(_SECURITY_TRIGGERS)

    # 5) 种子数据（INSERT OR IGNORE，重复启动不报错也不覆盖）
    now_iso = _now_iso()
    cursor.executescript(_SEED_PERMISSIONS)

    # 角色种子需要 created_at/updated_at（NOT NULL），走参数化逐条 upsert
    for role_id, name, desc, is_sys, is_assign in _BUILTIN_ROLES:
        cursor.execute(
            "INSERT OR IGNORE INTO security_roles"
            "(role_id, name, description, is_system, is_assignable, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (role_id, name, desc, is_sys, is_assign, now_iso, now_iso),
        )

    cursor.executescript(_SEED_ROLE_PERMISSIONS)


def _migrate_v92(conn: sqlite3.Connection) -> None:
    """v92：补 users 删除保护触发器（FK CASCADE 不 fire trigger 的绕过防护）。"""
    conn.executescript(_SECURITY_TRIGGERS_V92)


# ============================================================
# S13 · v93 业务域权限补齐
# ============================================================

# 原 26 条权限只覆盖 9 个 resource 域，而实际业务路由前缀有 22 个。
# v93 补 20 条：tasks/runtime/workspaces/providers/connections/schedules/logs/
# patrol/monitor 的 read+write（18 条），加 usage.read / audit.read（均只读）。
# INSERT OR IGNORE 幂等，老库重跑不报错。
_SEED_PERMISSIONS_V93 = """
INSERT OR IGNORE INTO security_permissions(perm_id, resource, action) VALUES
    ('tasks.read',       'tasks',       'read'),
    ('tasks.write',      'tasks',       'write'),
    ('runtime.read',     'runtime',     'read'),
    ('runtime.write',    'runtime',     'write'),
    ('workspaces.read',  'workspaces',  'read'),
    ('workspaces.write', 'workspaces',  'write'),
    ('providers.read',   'providers',   'read'),
    ('providers.write',  'providers',   'write'),
    ('connections.read', 'connections', 'read'),
    ('connections.write','connections', 'write'),
    ('schedules.read',   'schedules',   'read'),
    ('schedules.write',  'schedules',   'write'),
    ('logs.read',        'logs',        'read'),
    ('logs.write',       'logs',        'write'),
    ('patrol.read',      'patrol',      'read'),
    ('patrol.write',     'patrol',      'write'),
    ('monitor.read',     'monitor',     'read'),
    ('monitor.write',    'monitor',     'write'),
    ('usage.read',       'usage',       'read'),
    ('audit.read',       'audit',       'read');
"""

# 角色重新绑定（INSERT OR IGNORE：v91 已绑定的行不动，只 pickup 新权限）。
# owner/admin/viewer 用与 v91 相同的通用语句即可自动覆盖新权限；
# developer 是显式白名单，需逐条追加。
# 边界设计：patrol.write / monitor.write 仅 owner+admin（运维动作，developer 不给）。
_SEED_ROLE_PERMISSIONS_V93 = """
INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id)
SELECT 'role_owner', perm_id FROM security_permissions;

INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id)
SELECT 'role_admin', perm_id FROM security_permissions
WHERE perm_id NOT IN ('security.roles.write');

INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id)
SELECT 'role_viewer', perm_id FROM security_permissions WHERE action = 'read';

INSERT OR IGNORE INTO security_role_permissions(role_id, perm_id) VALUES
    ('role_developer', 'tasks.read'),      ('role_developer', 'tasks.write'),
    ('role_developer', 'runtime.read'),    ('role_developer', 'runtime.write'),
    ('role_developer', 'workspaces.read'), ('role_developer', 'workspaces.write'),
    ('role_developer', 'providers.read'),  ('role_developer', 'providers.write'),
    ('role_developer', 'connections.read'),('role_developer', 'connections.write'),
    ('role_developer', 'schedules.read'),  ('role_developer', 'schedules.write'),
    ('role_developer', 'logs.read'),       ('role_developer', 'logs.write'),
    ('role_developer', 'patrol.read'),
    ('role_developer', 'monitor.read'),
    ('role_developer', 'usage.read'),
    ('role_developer', 'audit.read');
"""


def _migrate_v93(conn: sqlite3.Connection) -> None:
    """v93：S13 路径级鉴权前置——补齐业务域权限并重新绑定内置角色。"""
    cursor = conn.cursor()
    cursor.executescript(_SEED_PERMISSIONS_V93)
    cursor.executescript(_SEED_ROLE_PERMISSIONS_V93)


# 版本步进迁移表。新增迁移时：往末尾追加 (新版本号, 迁移函数)，
# 并把 SECURITY_SCHEMA_VERSION 提到该版本号。
_MIGRATIONS: tuple[tuple[int, Any], ...] = (
    (91, _migrate_v91),
    (92, _migrate_v92),
    (93, _migrate_v93),
)


def migrate_security_schema(conn: sqlite3.Connection) -> bool:
    """执行 security schema 迁移，返回是否真的做了迁移。

    幂等：``PRAGMA user_version`` 已达最新时直接返回 False。
    增量：只跑 ``current`` 之后的版本，老库升级不会重跑已经做过的步骤。
    """
    cursor = conn.cursor()
    current = cursor.execute("PRAGMA user_version").fetchone()[0]
    if current >= SECURITY_SCHEMA_VERSION:
        return False

    for version, migrate_fn in _MIGRATIONS:
        if current >= version:
            continue
        migrate_fn(conn)
        # PRAGMA 不支持参数化，只能拼整数常量（version 来自本模块的常量表，非外部输入）
        cursor.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        logger.info("[security] schema migrated: %s -> %s", current, version)
        current = version

    return True


# ============================================================
# S3 · 首次启动 bootstrap（admin 账号）
# ============================================================

def bootstrap_first_user(conn: sqlite3.Connection) -> str | None:
    """users 表为空时创建初始 admin，返回明文密码；已有用户返回 None。

    密码来源（两条路径都设 ``must_reset_password=1``，首次登录强制改密）：

    1. 环境变量 ``AGENTOPS_BOOTSTRAP_PASSWORD`` 有值 → 用它（**仅本地 dev**）
    2. 否则 → ``secrets.token_urlsafe(24)`` 随机生成（生产默认，零配置即安全）

    明文只在 stderr 和 ``logs/bootstrap-password.txt`` 各出现一次，库里只有 argon2 hash。
    """
    cursor = conn.cursor()
    existing = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing > 0:
        return None

    username = os.environ.get("AGENTOPS_BOOTSTRAP_USERNAME", "admin")

    if os.environ.get("AGENTOPS_BOOTSTRAP_PASSWORD"):
        password = os.environ["AGENTOPS_BOOTSTRAP_PASSWORD"]
        static_pwd = True
    else:
        password = secrets.token_urlsafe(24)
        static_pwd = False

    pwd_hash = hash_password(password)
    now_iso = _now_iso()
    user_id = "user_admin"

    cursor.execute(
        "INSERT INTO users(user_id, username, display_name, password_hash, "
        "                  must_reset_password, created_at, updated_at) "
        "VALUES (?, ?, 'Administrator', ?, 1, ?, ?)",
        (user_id, username, pwd_hash, now_iso, now_iso),
    )
    cursor.execute(
        "INSERT INTO security_user_roles(user_id, role_id, granted_at) "
        "VALUES (?, 'role_owner', ?)",
        (user_id, now_iso),
    )
    conn.commit()

    if static_pwd:
        logger.warning(
            "[bootstrap] using STATIC env password — DEV ONLY. "
            "must_reset_password=1 is set, but change it immediately."
        )

    banner = (
        f"\n{'=' * 60}\n"
        f"[bootstrap] Initial admin created (SAVE NOW):\n"
        f"  username: {username}\n"
        f"  password: {password}\n"
        f"[bootstrap] must_reset_password=1 -> forced change on first login\n"
        f"{'=' * 60}\n"
    )

    # stderr：只在终端出现，不进 backend.log
    print(banner, file=sys.stderr)

    # 文件兜底：容器/后台启动时看不到终端。含明文，首次登录后应删除。
    #
    # pytest 下不写：测试库每个用例都是全新的，bootstrap 必然触发，会不断用随机
    # 密码覆盖运维手上那份真实的 bootstrap-password.txt（2026-08-29 实测踩到，
    # admin123 被冲掉）。只在真正启动服务时才落文件。
    # 需要验证本段行为的用例请先 monkeypatch.delenv("PYTEST_CURRENT_TEST")。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return password

    try:
        logs_dir = Path("logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        pwd_file = logs_dir / "bootstrap-password.txt"
        pwd_file.write_text(
            banner + "\n[!] 此文件含明文密码，首次登录改密后请立即删除。\n",
            encoding="utf-8",
        )
        logger.warning("[bootstrap] 初始密码已写入 %s（改密后请删除）", pwd_file)
    except OSError as exc:
        logger.error("[bootstrap] 无法写入初始密码文件：%s", exc)

    return password
