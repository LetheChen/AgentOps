"""emit_alert 工具 handler — 把告警推到 api/server.py 的全局告警队列。

设计动机：
  - api/server.py 的 `_global_alert_queue` 是 asyncio.Queue，且 emit_alert 可能在
    local_llm harness 子进程中被调用（与后端主进程隔离），不能直接 import 全局变量。
  - 因此走 HTTP：POST 到后端 `/api/monitor/emit-alert` 接口，由后端统一入队。
  - 后端接口在 api/server.py 中实现，把 tip 写入 `_global_alerts` 列表 +
    `_global_alert_queue` 队列，让 SSE 通道（`/api/patrol/alerts/stream`）实时推给前端。

函数签名遵循 deterministic / local_llm harness 约定：`async def(args, config) -> dict`。

参考：
  - tools/wecom_notify.py 第 19-25 行的 fail-safe load_dotenv 模式
  - tools/notify_tools.py 的 async handler 签名
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

# fail-safe：加载项目根目录的 .env（override=False 不覆盖已有环境变量）
# 确保工具在任意子进程执行时都能读到 BACKEND_URL 等环境变量
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=False)
    except Exception:
        pass  # dotenv 不可用时降级（环境变量可能已由父进程注入）

logger = logging.getLogger(__name__)

# 默认后端地址（与本机 api/server.py 一致）
_DEFAULT_BACKEND_URL = "http://localhost:1987"

# 允许的 severity 取值（与前端 tip 组件约定一致）
_VALID_SEVERITIES = {"info", "warning", "error", "success"}

# 允许的 tip_type 取值（与监控中心 tips 通道约定一致）
_VALID_TIP_TYPES = {
    "patrol_alert",
    "task_started",
    "task_completed",
    "validation_result",
    "quota_warning",
}


async def emit_alert(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """把告警/提示推送到监控中心全局告警队列（SSE 通道）。

    Args:
        args: {
            "severity": "info|warning|error|success",
            "title": "标题（短）",
            "message": "正文（可较长）",
            "agent_id": "task_monitor"（可选，用于追溯来源），
            "run_id": "run_xxx"（可选，关联 run），
            "tip_type": "patrol_alert|task_started|task_completed|validation_result|quota_warning"
        }
        config: {
            "backend_url": "http://localhost:1987"  # 后端地址
        }

    Returns:
        {"ok": True, "tip_id": "..."} 成功；
        {"ok": False, "error": "..."} 失败（参数校验/网络异常）。
    """
    cfg = config or {}
    backend_url = (cfg.get("backend_url") or _DEFAULT_BACKEND_URL).rstrip("/")
    endpoint = f"{backend_url}/api/monitor/emit-alert"

    severity = str(args.get("severity", "info")).lower()
    if severity not in _VALID_SEVERITIES:
        return {"ok": False, "error": f"severity 取值非法：{severity}，允许：{sorted(_VALID_SEVERITIES)}"}

    tip_type = str(args.get("tip_type", "patrol_alert")).lower()
    if tip_type not in _VALID_TIP_TYPES:
        return {"ok": False, "error": f"tip_type 取值非法：{tip_type}，允许：{sorted(_VALID_TIP_TYPES)}"}

    title = str(args.get("title", "")).strip()
    message = str(args.get("message", "")).strip()
    if not title and not message:
        return {"ok": False, "error": "title 和 message 至少需要一个非空"}

    # 本地生成 tip_id（便于日志关联；后端会原样透传或自行生成）
    tip_id = f"tip_{uuid.uuid4().hex[:12]}"

    payload = {
        "tip_id": tip_id,
        "severity": severity,
        "title": title,
        "message": message,
        "agent_id": args.get("agent_id"),
        "run_id": args.get("run_id"),
        "tip_type": tip_type,
    }

    # 局部 import httpx，避免模块加载时强依赖（pyproject 已声明）
    try:
        import httpx
    except ImportError as e:
        logger.error("emit_alert 失败：httpx 未安装 - %s", e)
        return {"ok": False, "error": f"httpx 未安装: {e}"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(endpoint, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            # 后端可能返回自己的 tip_id，优先用后端的
            returned_id = data.get("tip_id") or tip_id
            logger.info(
                "emit_alert 成功 tip_id=%s severity=%s title=%s",
                returned_id, severity, title[:50],
            )
            return {"ok": True, "tip_id": returned_id}
        logger.error(
            "emit_alert HTTP %s: %s (endpoint=%s)",
            resp.status_code, resp.text[:200], endpoint,
        )
        return {
            "ok": False,
            "error": f"HTTP {resp.status_code}",
            "status_code": resp.status_code,
            "response": resp.text[:500],
        }
    except httpx.HTTPError as e:
        logger.error("emit_alert 网络异常 endpoint=%s: %s", endpoint, e)
        return {"ok": False, "error": f"网络异常: {e}"}
    except Exception as e:
        logger.exception("emit_alert 未知异常: %s", e)
        return {"ok": False, "error": f"未知异常: {e}"}
