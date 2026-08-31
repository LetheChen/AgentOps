"""tools/db_cli.py — 数据库命令行工具（被 workflow 的 command 节点调用）。

提供 3 个子命令供 smart-query workflow 的确定性 command 节点调用：
- resolve-schema: 读白名单（安全边界）+ 连库查 information_schema 返回表结构（含字段 COMMENT）
- validate:      走 sql_validator 三层校验（parse / whitelist / cost）
- query:         调 sql_query 工具执行只读 SQL（包装 + 行集 trim）

设计原则：
- 全部为同步函数（workflow command 节点用 asyncio.create_subprocess_shell 调）
- stdout 写 JSON，stderr 写错误，exit code: 0=成功 / 1=参数错 / 2=校验失败 / 3=连接失败
- resolve-schema：白名单授权边界 + information_schema 字段来源；query：执行 sql_query（连库）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 让 `from tools.X import Y` 在 `python tools/db_cli.py` 直接调用时也能工作
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.db_tools import execute_sql_query, describe_database as _describe_database


def _emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    """统一 stdout 输出。"""
    print(json.dumps(payload, ensure_ascii=False, default=str))
    sys.exit(exit_code)


def _resolve_schema(database: str) -> None:
    """resolve-schema 子命令：返回某连接的表结构（白名单授权源）。

    Args:
        database: 形如 "mysql:audit_reader"
    """
    if not database:
        _emit({"ok": False, "error": "缺少 --database 参数"}, exit_code=1)

    result = asyncio.run(_describe_database({"database": database}, None))
    if not result.get("ok"):
        _emit(result, exit_code=1)
    _emit(result)


def _validate_sql(database: str, sql: str) -> None:
    """validate 子命令：跑 sql_validator 三层校验（不真查数据库）。

    Args:
        database: 连接标识（如 mysql:audit_reader）
        sql:      待校验 SQL
    """
    if not database or not sql:
        _emit({"ok": False, "error": "缺少 --database 或 --sql 参数"}, exit_code=1)

    from tools.sql_validator import validate as _validate

    result = _validate(sql=sql, conn_key=database)
    # validate 已返回 {ok, error?, ...}，按其 ok 字段决定 exit code
    _emit(result, exit_code=0 if result.get("ok") else 2)


def _query(database: str, sql: str) -> None:
    """query 子命令：执行只读 SQL（先 validate 再调 execute_sql_query）。

    Args:
        database: 连接标识
        sql:      已通过 validate 的 SQL
    """
    if not database or not sql:
        _emit({"ok": False, "error": "缺少 --database 或 --sql 参数"}, exit_code=1)

    async def _run() -> dict[str, Any]:
        return await execute_sql_query({"database": database, "sql": sql}, None)

    try:
        result = asyncio.run(_run())
    except Exception as e:
        _emit({"ok": False, "error": f"执行异常: {type(e).__name__}: {e}"}, exit_code=3)

    if not result.get("ok"):
        # 连接失败 / 校验拒绝 → exit_code 3
        _emit(result, exit_code=3)
    _emit(result)


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        # 输出纯 JSON 帮助（不要 print __doc__ 否则 stdout 非 JSON 会让 wrapper 解析失败）
        _emit(
            {
                "ok": True,
                "usage": "db_cli.py {resolve-schema|validate|query} [--key value]...",
                "commands": {
                    "resolve-schema": "读 config/db_whitelist.yaml 返回表结构（参数: --database）",
                    "validate":       "跑 sql_validator 三层校验（参数: --database --sql）",
                    "query":          "执行只读 SQL（参数: --database --sql，会先 validate）",
                },
            },
            exit_code=0,
        )

    cmd = argv[0]
    args = argv[1:]

    # 简易 --key value 解析
    kwargs: dict[str, str] = {}
    i = 0
    while i < len(args):
        key = args[i].lstrip("-")
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            kwargs[key] = args[i + 1]
            i += 2
        else:
            kwargs[key] = "true"
            i += 1

    try:
        if cmd == "resolve-schema":
            _resolve_schema(kwargs.get("database", ""))
        elif cmd == "validate":
            _validate_sql(kwargs.get("database", ""), kwargs.get("sql", ""))
        elif cmd == "query":
            _query(kwargs.get("database", ""), kwargs.get("sql", ""))
        else:
            _emit({"ok": False, "error": f"未知子命令: {cmd}（合法: resolve-schema/validate/query）"}, exit_code=1)
    except SystemExit:
        raise
    except Exception as e:
        _emit({"ok": False, "error": f"{type(e).__name__}: {e}"}, exit_code=1)


if __name__ == "__main__":
    main()