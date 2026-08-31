"""log_pull_admin conn_type 支持单元测试。

覆盖：
1. normalize_credential_id(conn_type=mysql) → mysql:<id>；缺省 ssh 向后兼容
2. upsert_connection mysql 类型写 conn_type + database，auth_type 强制 password
3. 非法 conn_type 报 LogPullConfigError
4. ssh 缺省类型行为不变（向后兼容）
5. _test_mysql_connection 无凭据时报错
6. list_connections 返回 conn_type + database 字段
"""
from __future__ import annotations

import pytest

from orchestrator.log_pull_admin import (
    LogPullConfigError,
    _test_mysql_connection,
    list_connections,
    normalize_credential_id,
    upsert_connection,
)


def test_normalize_mysql_prefix():
    assert normalize_credential_id(None, "audit_reader", "mysql") == "mysql:audit_reader"
    assert normalize_credential_id("", "audit_reader", "mysql") == "mysql:audit_reader"
    assert normalize_credential_id("None", "audit_reader", "mysql") == "mysql:audit_reader"


def test_normalize_default_ssh_backward_compat():
    assert normalize_credential_id(None, "prod-seeyon") == "ssh:prod-seeyon"
    assert normalize_credential_id("None", "prod-seeyon", "ssh") == "ssh:prod-seeyon"


def test_normalize_preserves_explicit_id():
    assert normalize_credential_id("mysql:custom", "audit_reader", "mysql") == "mysql:custom"


def test_upsert_mysql_connection(tmp_path):
    """mysql 类型写 conn_type + database，auth_type 强制 password，凭据前缀 mysql:。"""
    p = tmp_path / "log-pull.yaml"
    upsert_connection({
        "id": "audit_reader",
        "name": "智能审批业务数据",
        "conn_type": "mysql",
        "host": "10.3.75.137",
        "port": 3306,
        "username": "audit_reader",
        "database": "seeyon_oa",
        "auth_type": "key",  # mysql 类型应被强制为 password
        "credential_id": None,
        "enabled": True,
    }, path=p)

    conns = list_connections(path=p)
    assert len(conns) == 1
    c = conns[0]
    assert c["conn_type"] == "mysql"
    assert c["database"] == "seeyon_oa"
    assert c["auth_type"] == "password"  # mysql 强制 password
    assert c["credential_id"] == "mysql:audit_reader"
    assert c["port"] == 3306


def test_upsert_invalid_conn_type(tmp_path):
    p = tmp_path / "log-pull.yaml"
    with pytest.raises(LogPullConfigError):
        upsert_connection({
            "id": "x", "host": "h", "username": "u", "conn_type": "oracle",
        }, path=p)


def test_upsert_ssh_backward_compat(tmp_path):
    """缺省 conn_type=ssh，行为与改造前一致。"""
    p = tmp_path / "log-pull.yaml"
    upsert_connection({
        "id": "prod-seeyon",
        "host": "192.168.1.100",
        "port": 22,
        "username": "logreader",
        "auth_type": "key",
        "credential_id": None,
        "enabled": True,
    }, path=p)

    conns = list_connections(path=p)
    c = conns[0]
    assert c["conn_type"] == "ssh"
    assert c["auth_type"] == "key"
    assert c["credential_id"] == "ssh:prod-seeyon"
    assert c["port"] == 22
    assert c["database"] == ""  # ssh 无 database 字段


def test_mysql_connection_missing_secret():
    """_test_mysql_connection：无凭据时报错，不抛异常。"""
    conn = {"host": "10.3.75.137", "port": 3306, "username": "audit_reader"}
    result = _test_mysql_connection(conn, None)
    assert result["ok"] is False
    assert "无凭据" in result["error"] or "credential" in result["error"].lower()
