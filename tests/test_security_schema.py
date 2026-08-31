"""安全认证模块 schema 迁移 + bootstrap 测试（S1 / S2 / S3）。

设计来源：``docs/security-mvp-plan-2026-08-29.md``（v1.2）

覆盖：
- S1 users 表扩展 6 列 + 7 张新表 + 索引 + 触发器
- S2 26 条权限 + 4 个内置角色 + 角色权限绑定
- S3 首次启动 bootstrap admin（argon2 hash + owner 角色 + must_reset_password）
- 幂等性（重复启动不重建、不重复建用户）
- owner 触发器防解绑
- username 部分唯一索引
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from audit.security_schema import (
    SECURITY_SCHEMA_VERSION,
    hash_password,
    migrate_security_schema,
    verify_password,
)
from audit.store import SqliteEventStore

NEW_USER_COLUMNS = (
    "username",
    "password_hash",
    "must_reset_password",
    "last_login_at",
    "last_seen_at",
    "disabled_at",
)

SECURITY_TABLES = (
    "security_roles",
    "security_user_roles",
    "security_permissions",
    "security_role_permissions",
    "security_auth_sessions",
    "security_api_tokens",
    "security_login_attempts",
)


@pytest.fixture
def db_path(tmp_path: Path):
    """临时 audit.db（每个用例独立，互不污染）。"""
    return str(tmp_path / "audit.db")


@pytest.fixture
def store(db_path: str):
    """已跑完 security 迁移的 store。"""
    return SqliteEventStore(db_path)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


# ============================================================
# S1 · schema 迁移
# ============================================================

def test_migration_sets_user_version(store: SqliteEventStore):
    uv = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert uv >= SECURITY_SCHEMA_VERSION


def test_users_table_extended(store: SqliteEventStore):
    cols = _cols(store._conn, "users")
    for col in NEW_USER_COLUMNS:
        assert col in cols, f"users 表缺少列 {col}"


def test_security_tables_created(store: SqliteEventStore):
    tables = _tables(store._conn)
    for t in SECURITY_TABLES:
        assert t in tables, f"缺少表 {t}"


def test_migration_is_idempotent(db_path: str, store: SqliteEventStore):
    """重复 migrate 返回 False，且不会抛（ALTER 逐列检查 + CREATE IF NOT EXISTS）。"""
    assert migrate_security_schema(store._conn) is False
    # 再起一次 store（模拟服务重启）也不应报错
    SqliteEventStore(db_path)


# ============================================================
# S2 · 种子数据
# ============================================================

def test_permissions_seeded(store: SqliteEventStore):
    n = store._conn.execute("SELECT COUNT(*) FROM security_permissions").fetchone()[0]
    assert n == 46, f"权限字典应为 46 条（v91 26 + v93 20），实际 {n}"


def test_builtin_roles_seeded(store: SqliteEventStore):
    rows = store._conn.execute(
        "SELECT role_id, is_system, is_assignable FROM security_roles ORDER BY role_id"
    ).fetchall()
    assert len(rows) == 4
    by_id = {r["role_id"]: r for r in rows}
    assert by_id["role_owner"]["is_assignable"] == 0, "owner 不可被 UI 绑定"
    for r in rows:
        assert r["is_system"] == 1


def test_role_permission_matrix(store: SqliteEventStore):
    def count(role_id: str) -> int:
        return store._conn.execute(
            "SELECT COUNT(*) FROM security_role_permissions WHERE role_id = ?", (role_id,)
        ).fetchone()[0]

    assert count("role_owner") == 46, "owner 拥有全部权限"
    assert count("role_admin") == 45, "admin 除 security.roles.write 外全部"
    assert "security.roles.write" not in {
        r["perm_id"]
        for r in store._conn.execute(
            "SELECT perm_id FROM security_role_permissions WHERE role_id='role_admin'"
        ).fetchall()
    }
    # viewer = 所有 action='read' 的权限
    read_n = store._conn.execute(
        "SELECT COUNT(*) FROM security_permissions WHERE action='read'"
    ).fetchone()[0]
    assert count("role_viewer") == read_n
    # developer 35 条：v91 17 条 + v93 补 18 条业务域（patrol/monitor 只读）
    assert count("role_developer") == 35


# ============================================================
# S2 · owner 不可解绑（DB 触发器）
# ============================================================

def test_owner_unbind_blocked_by_trigger(db_path: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_PASSWORD", "bootstrap-pwd-12345")
    monkeypatch.chdir(Path(db_path).parent)
    store = SqliteEventStore(db_path)

    with pytest.raises(sqlite3.IntegrityError) as exc:
        store._conn.execute(
            "DELETE FROM security_user_roles WHERE user_id='user_admin' AND role_id='role_owner'"
        )
    assert "role_owner" in str(exc.value)


def test_owner_user_delete_blocked(db_path: str, monkeypatch: pytest.MonkeyPatch):
    """回归：FK 的 ON DELETE CASCADE **不会** fire 目标表的 DELETE 触发器。

    实测（2026-08-29）：只靠 prevent_owner_unbind 时，``DELETE FROM users``
    会级联清掉 security_user_roles 的 owner 行而触发器毫无反应，owner 保护被绕过。
    v92 因此在 users 表上补了 prevent_owner_user_delete。
    """
    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_PASSWORD", "bootstrap-pwd-12345")
    monkeypatch.chdir(Path(db_path).parent)
    store = SqliteEventStore(db_path)

    with pytest.raises(sqlite3.IntegrityError) as exc:
        store._conn.execute("DELETE FROM users WHERE user_id='user_admin'")
    assert "owner" in str(exc.value).lower()

    n = store._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert n == 1, "owner 用户不得被删除"


def test_non_owner_user_delete_allowed(store: SqliteEventStore):
    conn = store._conn
    conn.execute(
        "INSERT INTO users(user_id, username, created_at, updated_at) VALUES (?,?,?,?)",
        ("u_del", "bob", "2026-08-29T00:00:00+00:00", "2026-08-29T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO security_user_roles(user_id, role_id, granted_at) VALUES (?,?,?)",
        ("u_del", "role_viewer", "2026-08-29T00:00:00+00:00"),
    )
    conn.execute("DELETE FROM users WHERE user_id='u_del'")
    # 级联：角色绑定随之清掉
    assert conn.execute(
        "SELECT COUNT(*) FROM security_user_roles WHERE user_id='u_del'"
    ).fetchone()[0] == 0


def test_incremental_migration_v91_to_v92(db_path: str, monkeypatch: pytest.MonkeyPatch):
    """老库停在 v91 时，只需补跑 v92，不重跑 v91 的 DDL。"""
    monkeypatch.chdir(Path(db_path).parent)
    store = SqliteEventStore(db_path)
    conn = store._conn

    # 手动把版本退回 v91，模拟"已跑过 v91 但没跑 v92"的存量库
    conn.execute("PRAGMA user_version = 91")
    conn.execute("DROP TRIGGER IF EXISTS prevent_owner_user_delete")

    assert migrate_security_schema(conn) is True
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SECURITY_SCHEMA_VERSION
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='trigger' AND name='prevent_owner_user_delete'"
    ).fetchone()[0] == 1


def test_non_owner_unbind_allowed(db_path: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_PASSWORD", "bootstrap-pwd-12345")
    monkeypatch.chdir(Path(db_path).parent)
    store = SqliteEventStore(db_path)
    conn = store._conn

    conn.execute(
        "INSERT INTO security_user_roles(user_id, role_id, granted_at) VALUES (?,?,?)",
        ("user_admin", "role_viewer", "2026-08-29T00:00:00+00:00"),
    )
    conn.execute(
        "DELETE FROM security_user_roles WHERE user_id='user_admin' AND role_id='role_viewer'"
    )
    remaining = {
        r["role_id"]
        for r in conn.execute(
            "SELECT role_id FROM security_user_roles WHERE user_id='user_admin'"
        ).fetchall()
    }
    assert remaining == {"role_owner"}


# ============================================================
# S3 · bootstrap admin
# ============================================================

def test_bootstrap_creates_admin_with_env_password(
    db_path: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_PASSWORD", "admin123")
    monkeypatch.chdir(Path(db_path).parent)
    store = SqliteEventStore(db_path)
    conn = store._conn

    row = conn.execute(
        "SELECT user_id, username, password_hash, must_reset_password FROM users"
    ).fetchone()
    assert row is not None
    assert row["username"] == "admin"
    assert row["must_reset_password"] == 1
    # 明文绝不能落库
    assert "admin123" not in (row["password_hash"] or "")
    assert verify_password(row["password_hash"], "admin123")
    assert not verify_password(row["password_hash"], "wrong-password")

    roles = {
        r["role_id"]
        for r in conn.execute(
            "SELECT role_id FROM security_user_roles WHERE user_id=?", (row["user_id"],)
        ).fetchall()
    }
    assert roles == {"role_owner"}


def test_bootstrap_generates_random_password_without_env(
    db_path: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("AGENTOPS_BOOTSTRAP_PASSWORD", raising=False)
    # bootstrap 默认会在 pytest 下跳过写文件（防止测试库用随机密码覆盖运维手上的
    # logs/bootstrap-password.txt）。本用例就是要验证文件兜底，显式解除该守卫。
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.chdir(Path(db_path).parent)
    store = SqliteEventStore(db_path)

    row = store._conn.execute("SELECT password_hash FROM users").fetchone()
    assert row["password_hash"]
    # 随机密码不应是任何常见弱口令
    assert not verify_password(row["password_hash"], "admin")
    assert not verify_password(row["password_hash"], "admin123")

    # 明文应写入 logs/bootstrap-password.txt 供首次登录取用
    pwd_file = Path(db_path).parent / "logs" / "bootstrap-password.txt"
    assert pwd_file.exists(), "随机密码必须有文件兜底，否则用户无法登录"


def test_bootstrap_skipped_when_users_exist(db_path: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_PASSWORD", "first-pwd-12345")
    monkeypatch.chdir(Path(db_path).parent)
    SqliteEventStore(db_path)

    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_PASSWORD", "second-pwd-12345")
    SqliteEventStore(db_path)  # 重启

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert n == 1, "已有用户时不应重复 bootstrap"
    row = conn.execute("SELECT password_hash FROM users").fetchone()
    assert verify_password(row["password_hash"], "first-pwd-12345")
    conn.close()


def test_bootstrap_respects_custom_username(db_path: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_USERNAME", "root")
    monkeypatch.setenv("AGENTOPS_BOOTSTRAP_PASSWORD", "root-pwd-12345")
    monkeypatch.chdir(Path(db_path).parent)
    store = SqliteEventStore(db_path)
    row = store._conn.execute("SELECT username FROM users").fetchone()
    assert row["username"] == "root"


# ============================================================
# 约束与密码哈希
# ============================================================

def test_username_unique_index_enforced(store: SqliteEventStore):
    conn = store._conn
    conn.execute(
        "INSERT INTO users(user_id, username, created_at, updated_at) VALUES (?,?,?,?)",
        ("u1", "alice", "2026-08-29T00:00:00+00:00", "2026-08-29T00:00:00+00:00"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users(user_id, username, created_at, updated_at) VALUES (?,?,?,?)",
            ("u2", "alice", "2026-08-29T00:00:00+00:00", "2026-08-29T00:00:00+00:00"),
        )


def test_username_null_allowed_for_multiple_rows(store: SqliteEventStore):
    """部分唯一索引：NULL 不受唯一约束（存量未回填行不会互相打架）。"""
    conn = store._conn
    for uid in ("n1", "n2"):
        conn.execute(
            "INSERT INTO users(user_id, created_at, updated_at) VALUES (?,?,?)",
            (uid, "2026-08-29T00:00:00+00:00", "2026-08-29T00:00:00+00:00"),
        )


def test_password_hash_roundtrip():
    h = hash_password("S3cret-pass")
    assert h.startswith("$argon2")
    assert verify_password(h, "S3cret-pass")
    assert not verify_password(h, "S3cret-pas")
    assert not verify_password("", "anything"), "空 hash 必须验不过"


def test_empty_password_hash_never_matches():
    """迁移占位的 DEFAULT '' 不能成为后门。"""
    assert not verify_password("", "")
