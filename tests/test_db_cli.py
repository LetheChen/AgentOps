"""tests/test_db_cli.py — 验证 tools/db_cli.py 的 3 子命令契约。

避免后续 tools/sql_validator.py / tools/db_tools.py 接口改动时 db_cli 跟着坏。
3 个子命令都做断言：返回 JSON 结构 + exit code 语义。

不真连数据库（用 sqlite fixtures 跳过 audit_reader 真实连接）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_EXE = sys.executable  # 用测试运行环境本身的 python


def run_db_cli(*args: str) -> tuple[int, dict | None]:
    """调 db_cli.py 子命令，返回 (exit_code, parsed_json_or_None)。"""
    proc = subprocess.run(
        [PYTHON_EXE, str(PROJECT_ROOT / "tools" / "db_cli.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(PROJECT_ROOT),
    )
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, None


# ── resolve-schema ───────────────────────────────────────────

def test_resolve_schema_happy_path():
    rc, payload = run_db_cli("resolve-schema", "--database", "mysql:audit_reader")
    assert rc == 0
    assert payload["ok"] is True
    assert "audit_platform" in payload["schemas"]
    assert "audit_records" in payload["tables"]["audit_platform"]
    assert payload["max_rows"] > 0
    assert isinstance(payload["denied_columns"], list)
    # 字段级信息字段存在（无 DB 环境降级为 whitelist_only，columns 为空 dict）
    assert "columns" in payload
    assert isinstance(payload["columns"], dict)
    assert "schema_source" in payload
    assert payload["schema_source"] in ("information_schema", "whitelist_only")


def test_resolve_schema_missing_database():
    rc, payload = run_db_cli("resolve-schema", "--database", "")
    assert rc == 1
    assert payload["ok"] is False
    assert "database" in payload["error"]


def test_resolve_schema_unknown_connection():
    rc, payload = run_db_cli("resolve-schema", "--database", "mysql:nonexistent_xyz")
    assert rc == 1
    assert payload["ok"] is False
    assert "未在" in payload["error"] or "not" in payload["error"].lower()


# ── validate ────────────────────────────────────────────────

def test_validate_legitimate_select():
    rc, payload = run_db_cli(
        "validate",
        "--database", "mysql:audit_reader",
        "--sql", "SELECT COUNT(*) FROM audit_platform.audit_records",
    )
    assert rc == 0
    assert payload["ok"] is True
    assert "audit_platform.audit_records" in payload["tables"]


def test_validate_union_allowed():
    """UNION ALL 多段只读查询应被允许（多维度聚合合并）。"""
    rc, payload = run_db_cli(
        "validate",
        "--database", "mysql:audit_reader",
        "--sql",
        "SELECT 'a' AS s, COUNT(*) AS c FROM audit_platform.audit_records "
        "UNION ALL SELECT 'b', COUNT(*) FROM audit_platform.audit_records",
    )
    assert rc == 0
    assert payload["ok"] is True


def test_validate_rejects_drop():
    """DROP 必须被拒绝（exit_code=2，message 含中文铁律）。"""
    rc, payload = run_db_cli(
        "validate",
        "--database", "mysql:audit_reader",
        "--sql", "DROP TABLE audit_platform.audit_records",
    )
    assert rc == 2
    assert payload["ok"] is False
    assert "只允许" in payload["error"] or "SELECT" in payload["error"]


def test_validate_rejects_denied_column():
    """脱敏列（如 password_hash）必须被拒绝。"""
    rc, payload = run_db_cli(
        "validate",
        "--database", "mysql:audit_reader",
        "--sql", "SELECT username, password_hash FROM audit_platform.users",
    )
    assert rc == 2
    assert payload["ok"] is False
    assert "password_hash" in payload["error"]


def test_validate_empty_sql():
    rc, payload = run_db_cli("validate", "--database", "mysql:audit_reader", "--sql", "")
    assert rc == 1
    assert payload["ok"] is False


# ── query ────────────────────────────────────────────────────

def test_query_missing_args():
    rc, payload = run_db_cli("query", "--database", "mysql:audit_reader")
    assert rc == 1
    assert payload["ok"] is False


# ── 未知子命令 / 帮助 ──────────────────────────────────────────

def test_unknown_subcommand_returns_error():
    rc, payload = run_db_cli("foo", "--bar")
    assert rc == 1
    assert payload["ok"] is False
    assert "未知子命令" in payload["error"]


def test_no_args_shows_help():
    rc, payload = run_db_cli()
    assert rc == 0
    # 无参数输出 usage JSON（不是 yaml docstring，那是 stderr）
    assert payload is not None
    assert payload.get("ok") is True
    assert "usage" in payload