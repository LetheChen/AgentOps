"""tools/db_tools 单元测试。

覆盖：
1. _ensure_limit：没 LIMIT 自动加、已有 LIMIT 不动
2. _row_to_jsonable：bytes/Decimal/datetime 转 JSON 可序列化
3. execute_sql_query：校验失败返回 {ok: False, error} 不抛异常（mock list_connections 走不到 DB）
4. execute_sql_write：明确拒绝（不抛 NotImplementedError）
"""
from __future__ import annotations

import asyncio
import datetime
from decimal import Decimal

import pytest

from tools import db_tools
from tools.db_tools import _ensure_limit, _row_to_jsonable


def test_ensure_limit_appends_when_missing():
    sql = "SELECT * FROM audit_platform.audit_records WHERE status='rejected'"
    result = _ensure_limit(sql, 5000)
    assert result.endswith("LIMIT 5000")
    assert "LIMIT 5000" in result


def test_ensure_limit_preserves_existing():
    sql = "SELECT * FROM audit_platform.audit_records LIMIT 100"
    assert _ensure_limit(sql, 5000) == sql


def test_ensure_limit_preserves_semicolon_handling():
    sql = "SELECT * FROM audit_platform.audit_records;"
    result = _ensure_limit(sql, 5000)
    assert result.endswith("LIMIT 5000")
    assert result.count("LIMIT") == 1


def test_row_to_jsonable():
    row = (1, b"hello", Decimal("12.34"), datetime.datetime(2026, 8, 26, 10, 0))
    out = _row_to_jsonable(row)
    assert out[0] == 1
    assert out[1] == "hello"
    assert out[2] == 12.34
    assert isinstance(out[3], str)  # datetime → isoformat string


@pytest.mark.asyncio
async def test_execute_sql_query_validation_failure_returns_error():
    """校验失败（危险词）→ 返回 {ok: False, error}，不抛异常。"""
    result = await db_tools.execute_sql_query(
        {"database": "mysql:audit_reader", "sql": "DELETE FROM audit_records"}
    )
    assert result["ok"] is False
    assert "校验失败" in result["error"] or "危险" in result["error"]


@pytest.mark.asyncio
async def test_execute_sql_query_missing_args():
    result = await db_tools.execute_sql_query({"sql": "SELECT 1"})
    assert result["ok"] is False
    assert "database" in result["error"]

    result = await db_tools.execute_sql_query({"database": "mysql:x"})
    assert result["ok"] is False
    assert "sql" in result["error"]


@pytest.mark.asyncio
async def test_execute_sql_write_rejected():
    result = await db_tools.execute_sql_write({"sql": "DELETE FROM x"})
    assert result["ok"] is False
    assert "禁用" in result["error"] or "只读" in result["error"]
