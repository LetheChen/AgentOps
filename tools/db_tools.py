"""tools/db_tools.py — sql_query 工具真实现（只读 SQL 查询）。

流程（execute_sql_query）：
1. args.database 为「凭据管理-数据库连接」中的连接 id（如 audit_reader）
   - 兼容旧格式 "mysql:audit_reader"（provider_id 形式，自动 split 取 id 部分）
2. 从 log_pull_admin.list_connections() 找连接对象，直接取 host/port/username/database/conn_type
3. 从 credential_store 读密码（Fernet 解密），key 用连接对象的 credential_id 字段（不再魔法拼接）
4. tools/sql_validator.validate() 三层校验（语法 + 危险词 + schema 白名单）
5. pymysql 连接（asyncio.to_thread 跑同步，避免阻塞事件循环）
6. 自动加 LIMIT（LLM 没写时，取 db_whitelist.yaml max_rows）
7. 返回 {ok, columns, rows, row_count, elapsed_ms, truncated, validation}

校验失败：返回 {ok: False, error}（不抛异常，让 LLM 看到错误重写 SQL）。
原 stub 是 raise NotImplementedError → 静默成功（LLM 编答案），本实现根治该问题。

重构说明（2026-08-28）：
- db_type 直接从连接对象的 conn_type 字段读取（不再用 database 参数前缀推断）
- credential key 直接用连接对象的 credential_id 字段（不再魔法拼接）
- database 参数支持纯 id（audit_reader）和 provider_id（mysql:audit_reader）两种格式
- 根因：原实现在连接查找用 split[-1] 但密码查找用完整 key，导致 audit_reader 找不到密码
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


async def execute_sql_query(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行只读 SQL 查询（sql_query 工具 handler）。

    args:
        database (str, required): 连接 id（如 audit_reader）或 provider_id（如 mysql:audit_reader）
        sql (str, required): 只读 SQL（SELECT/WITH）

    Returns:
        {"ok": True, "columns": [...], "rows": [[...]], "row_count": N,
         "elapsed_ms": N, "truncated": bool, "validation": {...}}
        或 {"ok": False, "error": "..."}（校验失败 / 连接失败，不抛异常）
    """
    database = str(args.get("database", "")).strip()
    sql = str(args.get("sql", "")).strip()
    if not database:
        return {"ok": False, "error": "缺少 database 参数（连接 id，如 audit_reader）"}
    if not sql:
        return {"ok": False, "error": "缺少 sql 参数"}

    # 兼容两种入参格式：纯 id（audit_reader）或 provider_id（mysql:audit_reader）
    # 统一抽取连接 id 用于查找连接对象
    conn_id = database.split(":", 1)[-1] if ":" in database else database

    # 1. 找连接对象（host/port/username/database/conn_type/credential_id 全在里面）
    try:
        from orchestrator.log_pull_admin import list_connections
        conn_cfg = next(
            (c for c in list_connections() if c["id"] == conn_id),
            None,
        )
        if conn_cfg is None:
            return {"ok": False, "error": f"连接 '{conn_id}' 未在凭据管理-数据库连接中配置"}
        # db_type 直接从连接对象读，不再用 database 参数前缀推断
        db_type = conn_cfg.get("conn_type", "ssh")
        if db_type != "mysql":
            return {"ok": False, "error": f"连接 '{conn_id}' 不是 mysql 类型（当前 {db_type}）"}
    except Exception as e:
        return {"ok": False, "error": f"读取连接配置失败: {e}"}

    # 2. 读密码（Fernet 解密）—— 直接用连接对象的 credential_id 字段，不再魔法拼接
    cred_key = conn_cfg.get("credential_id") or f"{db_type}:{conn_id}"
    try:
        from orchestrator.credential_store import get_credential_store
        secret = get_credential_store().get(cred_key)
    except Exception as e:
        return {"ok": False, "error": f"读取凭据失败: {e}"}
    if not secret:
        return {"ok": False, "error": f"凭据 '{cred_key}' 未录入密码，请先在「凭据管理-数据库连接」录入"}

    # 3. 三层校验（语法 + 危险词 + schema 白名单）
    # 白名单 yaml 的 key 历史用 provider_id 格式（mysql:audit_reader），保持兼容
    from tools.sql_validator import (
        max_query_seconds_for,
        max_rows_for,
        validate,
    )
    whitelist_key = cred_key  # 与 db_whitelist.yaml 的 databases 键对齐
    validation = validate(sql, whitelist_key)
    if not validation["ok"]:
        return {
            "ok": False,
            "error": f"SQL 校验失败: {validation['error']}",
            "validation": validation,
        }

    # 4. 自动加 LIMIT（LLM 没写时）
    from tools.sql_validator import load_whitelist
    whitelist = load_whitelist(whitelist_key)
    max_rows = max_rows_for(whitelist)
    effective_sql = _ensure_limit(sql, max_rows)

    # 5. pymysql 执行（同步包走 asyncio.to_thread）
    query_timeout = max_query_seconds_for(whitelist)
    result = await asyncio.to_thread(
        _run_query,
        conn_cfg, secret, effective_sql, max_rows, query_timeout,
    )
    result["validation"] = validation
    return result


def _ensure_limit(sql: str, max_rows: int) -> str:
    """SQL 没写 LIMIT 时自动追加（上限 max_rows），已写则不动。"""
    upper = sql.upper().rstrip(";")
    if re_search_limit(upper):
        return sql
    return f"{sql.rstrip(';')} LIMIT {max_rows}"


def re_search_limit(sql_upper: str) -> bool:
    """粗略检测是否已有 LIMIT 子句（避免重复追加）。"""
    import re
    # 简单检测：LIMIT 出现在末尾且后面跟数字（或 ? 占位）
    return bool(re.search(r"\bLIMIT\s+(\d+|\?|:limit)\s*$", sql_upper))


def _run_query(
    conn_cfg: dict[str, Any],
    secret: str,
    sql: str,
    max_rows: int,
    timeout_sec: int,
) -> dict[str, Any]:
    """同步执行查询（pymysql）。返回 {ok, columns, rows, row_count, elapsed_ms, truncated, error}。"""
    try:
        import pymysql
    except ImportError:
        return {
            "ok": False, "error": "缺少依赖 pymysql（pip install pymysql）",
            "columns": [], "rows": [], "row_count": 0, "elapsed_ms": None, "truncated": False,
        }

    started = time.monotonic()
    try:
        conn = pymysql.connect(
            host=conn_cfg["host"],
            port=int(conn_cfg["port"]),
            user=conn_cfg["username"],
            password=secret,
            database=conn_cfg.get("database") or None,
            connect_timeout=5,
            read_timeout=timeout_sec,
        )
    except Exception as e:
        return {
            "ok": False, "error": f"连接失败: {e}",
            "columns": [], "rows": [], "row_count": 0,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "truncated": False,
        }

    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            truncated = False
            if len(rows) > max_rows:
                rows = rows[:max_rows]
                truncated = True
            # rows 里可能有 bytes，转 str 保证 JSON 可序列化
            rows = [_row_to_jsonable(r) for r in rows]
        return {
            "ok": True,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "truncated": truncated,
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False, "error": f"查询执行失败: {e}",
            "columns": [], "rows": [], "row_count": 0,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "truncated": False,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _row_to_jsonable(row: tuple) -> list[Any]:
    """pymysql 返回的元组可能含 bytes / Decimal / datetime，转 JSON 可序列化类型。"""
    import datetime
    from decimal import Decimal

    out: list[Any] = []
    for v in row:
        if isinstance(v, bytes):
            out.append(v.decode("utf-8", errors="replace"))
        elif isinstance(v, Decimal):
            out.append(float(v))
        elif isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
            out.append(v.isoformat())
        else:
            out.append(v)
    return out


async def execute_sql_write(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行写入 SQL（sql_execute 工具）。当前**禁止**（安全策略：sql_query 只读，写操作走审批流）。

    保留签名供 config/tools/sql_execute.yaml 引用；明确拒绝而不是 stub 抛异常，
    避免 LLM 拿到 NotImplementedError 后静默编答案。
    """
    return {
        "ok": False,
        "error": "sql_execute 写操作已禁用：本项目只允许只读查询（写操作请走审批流 smart_approval）",
    }


async def describe_database(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回某数据库连接的表结构（白名单 = 安全边界，information_schema = 字段来源）。

    两层信息源：
    - 白名单（config/db_whitelist.yaml）：哪些 schema/表允许查、哪些列屏蔽（安全边界，不连库）
    - information_schema.COLUMNS：字段名 + 类型 + COLUMN_COMMENT（连库拿，需 COMMENT 已维护）
      连库失败时降级为「仅表名」（schema_source="whitelist_only"），不阻塞调用方。

    args:
        database (str, required): 形如 "mysql:audit_reader"

    Returns:
        {"ok": True, "schemas": [...], "tables": {schema: [table, ...]},
         "denied_columns": [...], "max_rows": N,
         "columns": {schema: {table: [{name, type, nullable, default, comment}, ...]}},
         "schema_source": "information_schema" | "whitelist_only"}
        或 {"ok": False, "error": "..."}
    """
    database = str(args.get("database", "")).strip()
    if not database:
        return {"ok": False, "error": "缺少 database 参数（如 mysql:audit_reader）"}

    from tools.sql_validator import (
        load_whitelist,
        max_rows_for,
    )

    whitelist = load_whitelist(database)
    if not whitelist:
        return {
            "ok": False,
            "error": f"连接 '{database}' 未在 config/db_whitelist.yaml 配置白名单",
        }

    schemas = whitelist.get("allowed_schemas") or []
    tables = whitelist.get("allowed_tables") or {}
    denied = whitelist.get("denied_columns") or []

    result = {
        "ok": True,
        "schemas": schemas,
        "tables": tables,
        "denied_columns": denied,
        "max_rows": max_rows_for(whitelist),
        "columns": {},  # 字段级（information_schema，连库成功才有）
        "schema_source": "whitelist_only",
    }

    # 尝试连库查 information_schema.COLUMNS（字段名 + 类型 + COMMENT）
    conn_id = database.split(":", 1)[-1] if ":" in database else database
    try:
        from orchestrator.log_pull_admin import list_connections

        conn_cfg = next(
            (c for c in list_connections() if c["id"] == conn_id),
            None,
        )
        if conn_cfg is None:
            logger.warning("describe_database: 连接 '%s' 未配置，降级白名单表名", conn_id)
            return result
        if conn_cfg.get("conn_type", "ssh") != "mysql":
            logger.warning(
                "describe_database: 连接 '%s' 非 mysql（%s），降级白名单表名",
                conn_id, conn_cfg.get("conn_type"),
            )
            return result
        cred_key = conn_cfg.get("credential_id") or f"mysql:{conn_id}"
        from orchestrator.credential_store import get_credential_store

        secret = get_credential_store().get(cred_key)
        if not secret:
            logger.warning("describe_database: 凭据 '%s' 未录入，降级白名单表名", cred_key)
            return result

        columns = await asyncio.to_thread(
            _query_information_schema_columns, conn_cfg, secret, schemas, tables, denied,
        )
        result["columns"] = columns
        result["schema_source"] = "information_schema"
    except Exception as e:
        logger.warning("describe_database: 连库查 information_schema 失败，降级白名单表名: %s", e)

    return result


def _query_information_schema_columns(
    conn_cfg: dict[str, Any],
    secret: str,
    schemas: list[str],
    tables_map: dict[str, list[str]],
    denied: list[str],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """同步查 information_schema.COLUMNS，返回 {schema: {table: [字段dict]}}。

    仅查白名单允许的表；过滤 denied_columns 正则命中的列；带 charset=utf8mb4 保证 COMMENT 返回 str。
    """
    import pymysql

    pairs: list[tuple[str, str]] = []
    for schema, tlist in tables_map.items():
        if schema not in schemas:
            continue
        for t in tlist:
            pairs.append((schema, t))
    if not pairs:
        return {}

    conn = pymysql.connect(
        host=conn_cfg["host"],
        port=int(conn_cfg["port"]),
        user=conn_cfg["username"],
        password=secret,
        database=conn_cfg.get("database") or None,
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=15,
    )
    try:
        placeholders = ", ".join(["(%s, %s)"] * len(pairs))
        params = [x for pair in pairs for x in pair]
        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, "
            "IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            f"WHERE (TABLE_SCHEMA, TABLE_NAME) IN ({placeholders}) "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        columns: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for schema, table, col_name, col_type, nullable, default, comment in rows:
            if _is_denied_column(col_name, denied):
                continue
            columns.setdefault(schema, {}).setdefault(table, []).append({
                "name": col_name,
                "type": col_type,
                "nullable": nullable == "YES",
                "default": _jsonable_default(default),
                "comment": comment or "",
            })
        return columns
    finally:
        conn.close()


def _is_denied_column(col_name: str, denied_patterns: list[str]) -> bool:
    """列名是否命中 denied_columns 正则（忽略大小写）。"""
    import re

    for pat in denied_patterns:
        try:
            if re.search(pat, col_name, flags=re.IGNORECASE):
                return True
        except re.error:
            logger.warning("db_whitelist denied_columns 非法正则: %s", pat)
    return False


def _jsonable_default(value: Any) -> Any:
    """把 pymysql 返回的 COLUMN_DEFAULT 转 JSON 可序列化（bytes→str，其余原样）。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
