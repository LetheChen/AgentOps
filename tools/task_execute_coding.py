"""task_execute_coding 工具：派发 coding_agent 执行。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 调 orch.execute_coding
- 绑 terminal 会话 + workspace 沙箱 + 风格 overlay
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_execute_coding(args: dict) -> dict:
    """派发 coding_agent 执行任务。

    args:
        task_id (str, required): 任务 ID
        style_id (str, optional): 执行风格 ID，默认 default
        if_version (int, optional): 乐观锁版本号，默认 0
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "派发失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "派发失败：缺少 task_id", "error": "missing_task_id"}

    style_id = (args.get("style_id") or "default").strip()

    if_version_raw = args.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        return {"content": f"派发失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    result = await orch.execute_coding(task_id, style_id=style_id, if_version=if_version)
    if not result.get("ok"):
        return {"content": f"派发失败：{result.get('message') or result.get('error')}",
                "error": result.get("error"),
                "hint": result.get("hint"),
                "task": result.get("task")}

    return {
        "content": (f"已派发 coding_agent 执行任务 {result['task'].get('identifier', task_id)}"
                    f"（run_id={result.get('run_id')}, mock={result.get('mock')}）"),
        "run_id": result.get("run_id"),
        "session_id": result.get("session_id"),
        "terminal_session_id": result.get("terminal_session_id"),
        "style_id": result.get("style_id"),
        "workspace_id": result.get("workspace_id"),
        "mock": result.get("mock", False),
        "task": result.get("task"),
    }
