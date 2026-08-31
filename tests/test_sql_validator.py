"""tools/sql_validator 单元测试。

覆盖三层校验 + 工具函数：
1. 合法 SELECT 通过（返回 tables/columns）
2. 危险词拦截（DELETE/UPDATE/INSERT/DROP/SLEEP/OUTFILE）
3. 多语句拒绝（分号分隔）
4. schema 白名单：表不在 allowed_tables 拒
5. schema 白名单：allowed_schemas 外拒
6. denied_columns 列级屏蔽（password_hash / *password*）
7. max_rows_for / max_query_seconds_for / row_filters_for 读默认值 + 配置值
8. ${ENV_VAR} 展开（db_whitelist.yaml 不再含生产 IP）
"""
from __future__ import annotations

import pytest

from tools.sql_validator import (
    _expand_dict,
    _expand_env,
    load_whitelist,
    max_query_seconds_for,
    max_rows_for,
    row_filters_for,
    validate,
)

# 与 config/db_whitelist.yaml 对齐的最小白名单（单测用，不依赖真实文件）
WHITELIST = {
    "allowed_schemas": ["audit_platform"],
    "allowed_tables": {
        "audit_platform": ["audit_records", "users", "llm_call_logs"],
    },
    "denied_columns": ["password_hash", ".*password.*", "node_token"],
    "operation_limits": {"max_rows": 5000, "max_query_seconds": 30},
    "row_filters": [],
}


def test_valid_select_passes():
    result = validate(
        "SELECT request_id, status FROM audit_platform.audit_records WHERE status='rejected'",
        "mysql:audit_reader",
        WHITELIST,
    )
    assert result["ok"] is True
    assert "audit_records" in result["tables"][0]
    assert "request_id" in result["columns"]


def test_valid_select_no_schema_prefix_passes():
    # 裸表名（无 schema 前缀）也能通过白名单
    result = validate(
        "SELECT COUNT(*) AS n FROM audit_records",
        "mysql:audit_reader",
        WHITELIST,
    )
    assert result["ok"] is True


@pytest.mark.parametrize("sql", [
    "DELETE FROM audit_records",
    "UPDATE audit_records SET status='x'",
    "INSERT INTO audit_records (id) VALUES (1)",
    "DROP TABLE audit_records",
    "SELECT SLEEP(10)",
    "TRUNCATE TABLE audit_records",
    "ALTER TABLE audit_records ADD COLUMN x int",
])
def test_dangerous_keywords_rejected(sql):
    result = validate(sql, "mysql:audit_reader", WHITELIST)
    assert result["ok"] is False
    assert "危险" in result["error"] or "只允许" in result["error"]


def test_into_outfile_rejected():
    """INTO OUTFILE 可能被语法层（sqlglot）或危险词层拦，只要 ok=False 即可。"""
    result = validate(
        "SELECT * FROM audit_records INTO OUTFILE '/tmp/x'",
        "mysql:audit_reader",
        WHITELIST,
    )
    assert result["ok"] is False


def test_multi_statement_rejected():
    result = validate(
        "SELECT 1; DROP TABLE audit_records",
        "mysql:audit_reader",
        WHITELIST,
    )
    assert result["ok"] is False
    assert "条语句" in result["error"]


def test_non_select_rejected():
    result = validate("SHOW TABLES", "mysql:audit_reader", WHITELIST)
    assert result["ok"] is False


def test_table_not_in_whitelist_rejected():
    result = validate(
        "SELECT * FROM audit_platform.secret_table",
        "mysql:audit_reader",
        WHITELIST,
    )
    assert result["ok"] is False
    assert "白名单" in result["error"] or "allowed_tables" in result["error"]


def test_schema_not_allowed_rejected():
    result = validate(
        "SELECT * FROM information_schema.tables",
        "mysql:audit_reader",
        WHITELIST,
    )
    assert result["ok"] is False


def test_denied_column_rejected():
    result = validate(
        "SELECT username, password_hash FROM audit_platform.users",
        "mysql:audit_reader",
        WHITELIST,
    )
    assert result["ok"] is False
    assert "password" in result["error"].lower()


def test_no_whitelist_only_l1l2():
    # whitelist 缺失时只做语法 + 危险词（表名不校验）
    result = validate("SELECT * FROM any_table", "mysql:nonexistent", None)
    assert result["ok"] is True


def test_max_rows_default_and_config():
    assert max_rows_for({}) == 5000
    assert max_rows_for({"operation_limits": {"max_rows": 100}}) == 100


def test_max_query_seconds_default_and_config():
    assert max_query_seconds_for({}) == 30
    assert max_query_seconds_for({"operation_limits": {"max_query_seconds": 5}}) == 5


def test_row_filters():
    assert row_filters_for({}) == []
    assert row_filters_for({"row_filters": ["tenant_id = :t"]}) == ["tenant_id = :t"]


# ── ${ENV_VAR} 展开（db_whitelist.yaml 不再含生产 IP）─────────────────────
# 使用 RFC 5737 文档保留地址（192.0.2.x = TEST-NET-1），不可路由，绝不会与生产地址冲突


def test_expand_env_basic(monkeypatch):
    monkeypatch.setenv("MY_TEST_HOST", "192.0.2.10")
    assert _expand_env("host=${MY_TEST_HOST}:3306") == "host=192.0.2.10:3306"


def test_expand_env_unset_returns_empty(monkeypatch):
    monkeypatch.delenv("MY_TEST_HOST", raising=False)
    # 未设环境变量时返回空串（与 model_config._expand_env 语义一致）
    assert _expand_env("host=${MY_TEST_HOST}:3306") == "host=:3306"


def test_expand_env_with_default(monkeypatch):
    monkeypatch.delenv("MY_TEST_HOST", raising=False)
    # ${VAR:-default} 语法：未设时用 default（RFC 5737 文档保留地址）
    assert _expand_env("host=${MY_TEST_HOST:-192.0.2.10}:3306") == "host=192.0.2.10:3306"


def test_expand_env_no_var_unchanged():
    assert _expand_env("plain text") == "plain text"
    assert _expand_env("") == ""


def test_expand_dict_recursive(monkeypatch):
    monkeypatch.setenv("DB_AUDIT_HOST", "192.0.2.10")
    monkeypatch.setenv("DB_AUDIT_PORT", "3306")
    data = {
        "databases": {
            "mysql:audit_reader": {
                "note": "${DB_AUDIT_HOST}:${DB_AUDIT_PORT}",
                "nested": [{"host": "${DB_AUDIT_HOST}", "port": "${DB_AUDIT_PORT}"}],
            }
        }
    }
    out = _expand_dict(data)
    assert out["databases"]["mysql:audit_reader"]["note"] == "192.0.2.10:3306"
    assert out["databases"]["mysql:audit_reader"]["nested"][0]["host"] == "192.0.2.10"
    assert out["databases"]["mysql:audit_reader"]["nested"][0]["port"] == "3306"


def test_load_whitelist_expands_env(tmp_path, monkeypatch):
    """真实文件：yaml 含 ${ENV_VAR}，load_whitelist 应展开到真实值（RFC 5737 IP）。"""
    monkeypatch.setenv("DB_AUDIT_HOST", "192.0.2.10")
    monkeypatch.setenv("DB_AUDIT_PORT", "3306")

    yaml_content = """
databases:
  mysql:audit_reader:
    note: "audit_reader @ ${DB_AUDIT_HOST}:${DB_AUDIT_PORT}"
    allowed_schemas: [audit_platform]
    allowed_tables:
      audit_platform: [audit_records]
    denied_columns: ["password_hash"]
    row_filters: []
    operation_limits:
      max_rows: 5000
"""
    yaml_path = tmp_path / "db_whitelist.yaml"
    yaml_path.write_text(yaml_content, encoding="utf-8")

    wl = load_whitelist("mysql:audit_reader", path=yaml_path)
    assert "192.0.2.10" in wl["note"]
    assert "3306" in wl["note"]
    assert wl["operation_limits"]["max_rows"] == 5000  # int 未被占位符污染


def test_load_whitelist_uses_default_when_env_unset(tmp_path, monkeypatch):
    """环境变量未设时，${VAR:-default} 应回退到 default（RFC 5737 IP）。"""
    monkeypatch.delenv("DB_AUDIT_HOST", raising=False)
    monkeypatch.delenv("DB_AUDIT_PORT", raising=False)

    yaml_content = """
databases:
  mysql:audit_reader:
    note: "${DB_AUDIT_HOST:-192.0.2.10}:${DB_AUDIT_PORT:-3306}"
    allowed_schemas: [audit_platform]
"""
    yaml_path = tmp_path / "db_whitelist.yaml"
    yaml_path.write_text(yaml_content, encoding="utf-8")

    wl = load_whitelist("mysql:audit_reader", path=yaml_path)
    assert wl["note"] == "192.0.2.10:3306"