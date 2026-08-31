"""tools/sql_validator.py — SQL 只读三层校验（sql_query 工具的安全闸门）。

设计来源：
- 骨架借鉴 data_agent 的 sql_validator.py（E:/Project/data_agent/app/core/sql_validator.py）
- 本版补强（data_agent 没有的）：
  1. dialect 改为 mysql（原为 postgres）
  2. 多 statement 拒绝（sqlglot.parse 返回多个节点 → 拒）
  3. schema 白名单：sqlglot AST 提取 Table/Column 节点，比对 config/db_whitelist.yaml
  4. denied_columns 列级屏蔽（正则匹配列名，如 "*password*"）
  5. 白名单缺失时只做 L1+L2（语法 + 危险词），不阻塞（配置优先）

三层：
- L1 语法层：sqlglot.parse(sql, dialect="mysql") 解析无异常 + 单 Select 节点
- L2 危险词黑名单：正则匹配（INTO OUTFILE / LOAD DATA / SLEEP / BENCHMARK / DML/DDL）
- L3 schema 白名单：Table 节点 ⊂ allowed_tables[db]；列名 ⊄ denied_columns 正则

校验失败抛 SqlValidationError（LLM 可据此重写 SQL，不静默成功）。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 项目根（config/db_whitelist.yaml 定位）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_WHITELIST_YAML = _PROJECT_ROOT / "config" / "db_whitelist.yaml"

# 危险关键词（\b 词边界，避免误伤子串；宁可误伤不可放过）
DANGEROUS_KEYWORDS = (
    r"\bDROP\b", r"\bDELETE\b", r"\bUPDATE\b", r"\bINSERT\b",
    r"\bALTER\b", r"\bCREATE\b", r"\bTRUNCATE\b", r"\bGRANT\b",
    r"\bREVOKE\b", r"\bCALL\b", r"\bEXEC(?:UTE)?\b", r"\bSET\b",
    r"\bLOAD\s+DATA\b", r"\bINTO\s+OUTFILE\b", r"\bDUMPFILE\b",
    r"\bSLEEP\s*\(", r"\bBENCHMARK\s*\(",
)

# 默认操作限制（whitelist 未声明时兜底）
DEFAULT_MAX_ROWS = 5000
DEFAULT_MAX_QUERY_SECONDS = 30


def _expand_env(value: str) -> str:
    """展开 ${VAR} 和 ${VAR:-default} 语法（与 orchestrator/model_config.py:_expand_env 同语义）。

    用途：db_whitelist.yaml 中的 IP/端口等占位符（如 ${DB_AUDIT_HOST}:${DB_AUDIT_PORT}）
    在加载时从环境变量注入真实值，避免把生产 IP 写进仓库。
    """
    def replacer(match):
        var_name = match.group(1)
        default_val = match.group(2)
        env_val = os.environ.get(var_name, "")
        return env_val if env_val else (default_val or "")
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.+?))?\}", replacer, value)


def _expand_dict(d: Any) -> Any:
    """递归展开 dict/list/str 中的 ${VAR} 引用。"""
    if isinstance(d, str):
        return _expand_env(d)
    if isinstance(d, dict):
        return {k: _expand_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_expand_dict(v) for v in d]
    return d


class SqlValidationError(Exception):
    """SQL 校验失败（工具层转 {ok: False, error} 给 LLM 重写，不静默）。"""


def load_whitelist(conn_key: str, path: Path | None = None) -> dict[str, Any]:
    """读取 config/db_whitelist.yaml 中某连接的策略；无配置返回空 dict（仅 L1+L2）。

    支持 ${ENV_VAR} 展开：所有字符串值（包含注释外的 IP/端口占位符）在 yaml.safe_load
    后递归展开，避免生产 IP 入仓库。

    兼容两种 conn_key 格式：
    - provider_id 形式（mysql:audit_reader）：直接匹配 yaml 键
    - 纯 id 形式（audit_reader）：先精确匹配，未命中则按后缀匹配（mysql:audit_reader）
    """
    p = path or DB_WHITELIST_YAML
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = _expand_dict(data)
        databases = data.get("databases") or {}
        # 1. 精确匹配（覆盖 provider_id 形式）
        if conn_key in databases:
            return databases[conn_key] or {}
        # 2. 纯 id 兜底：按 ":<id>" 后缀匹配（覆盖 audit_reader → mysql:audit_reader）
        if ":" not in conn_key:
            suffix = f":{conn_key}"
            for k in databases:
                if k.endswith(suffix):
                    return databases[k] or {}
        return {}
    except Exception as e:
        logger.warning("读取 db_whitelist.yaml 失败（%s）: %s", p, e)
        return {}


def _extract_tables(parsed: Any) -> list[tuple[str | None, str]]:
    """从 sqlglot AST 提取 (schema, table) 元组列表（FROM/JOIN 子查询除外由 find_all 全量取）。"""
    import sqlglot.expressions as exp

    result: list[tuple[str | None, str]] = []
    for node in parsed.find_all(exp.Table):
        name = node.name
        if not name:
            continue
        db = node.db or None
        result.append((db, name))
    return result


def _extract_columns(parsed: Any) -> list[str]:
    """从 sqlglot AST 提取显式列名（SELECT * 无列名，跳过）。"""
    import sqlglot.expressions as exp

    cols: list[str] = []
    for node in parsed.find_all(exp.Column):
        cname = node.name
        if cname and cname != "*" and cname not in cols:
            cols.append(cname)
    return cols


def validate(
    sql: str,
    conn_key: str,
    whitelist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """三层校验入口。

    Args:
        sql: LLM 生成的 SQL
        conn_key: 形如 "mysql:audit_reader"（db_whitelist.yaml 的 databases 键）
        whitelist: 该连接的白名单 dict（不传则从 config/db_whitelist.yaml 读）

    Returns:
        {"ok": True, "tables": [...], "columns": [...], "error": None}
        或 {"ok": False, "error": "..."}
    """
    if not sql or not sql.strip():
        return {"ok": False, "error": "SQL 为空"}
    if whitelist is None:
        whitelist = load_whitelist(conn_key)

    # ── L1 语法层 ──
    try:
        import sqlglot
        parsed = sqlglot.parse(sql, dialect="mysql")
    except ImportError:
        return {"ok": False, "error": "缺少依赖 sqlglot（pip install sqlglot）"}
    except Exception as e:
        return {"ok": False, "error": f"SQL 语法错误: {e}"}

    if not parsed:
        return {"ok": False, "error": "sqlglot 解析结果为空"}

    # 多语句拒绝：parse 返回多个节点 = 有 ; 分隔的多语句
    if len(parsed) > 1:
        return {"ok": False, "error": f"只允许单条 SELECT，检测到 {len(parsed)} 条语句"}

    from sqlglot.expressions import Select, Union
    root = parsed[0]
    # 允许单条 SELECT 或 UNION（多维度聚合常用 UNION ALL 合并多个分组结果）。
    # 安全保证：UNION 语法下每个子查询都只能是 SELECT（sqlglot 解析保证），
    # 危险词 L2 + 表白名单 L3 用 find_all 递归覆盖 UNION 的所有子查询。
    if not isinstance(root, (Select, Union)):
        return {
            "ok": False,
            "error": f"只允许 SELECT/UNION 查询，当前是 {type(root).__name__.upper()}",
        }

    # ── L2 危险词黑名单 ──
    # 从 whitelist 合并可配置的 forbidden_keywords，默认用内置清单
    forbidden = list(DANGEROUS_KEYWORDS)
    extra_kw = (whitelist.get("operation_limits") or {}).get("forbidden_keywords") or []
    if isinstance(extra_kw, list):
        forbidden.extend(extra_kw)
    sql_upper = sql.upper()
    for pattern in forbidden:
        if re.search(pattern, sql_upper, flags=re.IGNORECASE):
            return {"ok": False, "error": f"SQL 包含危险关键词/函数: {pattern}"}

    # ── L3 schema 白名单 ──
    tables = _extract_tables(root)
    columns = _extract_columns(root)

    allowed_schemas = whitelist.get("allowed_schemas") or []
    allowed_tables_map = whitelist.get("allowed_tables") or {}  # {db: [table, ...]}
    denied_column_patterns = whitelist.get("denied_columns") or []

    if whitelist:
        # 4.1 表名白名单
        for db, table in tables:
            in_any = False
            if db:
                # 显式 db.table
                allowed = allowed_tables_map.get(db, [])
                if table in allowed:
                    in_any = True
                elif db not in allowed_schemas:
                    return {
                        "ok": False,
                        "error": f"表 {db}.{table} 不在允许的 schema 白名单（allowed_schemas={allowed_schemas}）",
                    }
            else:
                # 裸表名：检查任意允许的 schema 里是否有该表
                for db_name, allowed in allowed_tables_map.items():
                    if db_name in allowed_schemas and table in allowed:
                        in_any = True
                        break
            if not in_any:
                return {
                    "ok": False,
                    "error": f"表 {db + '.' if db else ''}{table} 不在白名单（allowed_tables）中",
                }

        # 4.2 列级屏蔽（denied_columns 正则）
        for col in columns:
            for pat in denied_column_patterns:
                try:
                    if re.search(pat, col, flags=re.IGNORECASE):
                        return {
                            "ok": False,
                            "error": f"列 {col} 命中禁止访问规则: {pat}",
                        }
                except re.error:
                    logger.warning("db_whitelist denied_columns 非法正则: %s", pat)

    return {
        "ok": True,
        "tables": [f"{db}.{t}" if db else t for db, t in tables],
        "columns": columns,
        "error": None,
    }


def max_rows_for(whitelist: dict[str, Any]) -> int:
    """该连接的最大返回行数（whitelist.operation_limits.max_rows 或默认 5000）。"""
    limits = whitelist.get("operation_limits") or {}
    try:
        return int(limits.get("max_rows", DEFAULT_MAX_ROWS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROWS


def max_query_seconds_for(whitelist: dict[str, Any]) -> int:
    """该连接的单查询超时秒数（whitelist.operation_limits.max_query_seconds 或默认 30）。"""
    limits = whitelist.get("operation_limits") or {}
    try:
        return int(limits.get("max_query_seconds", DEFAULT_MAX_QUERY_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_QUERY_SECONDS


def row_filters_for(whitelist: dict[str, Any]) -> list[str]:
    """该连接需要强制注入的 WHERE 条件模板（如 ["tenant_id = :tenant_id"]）。"""
    return list(whitelist.get("row_filters") or [])
