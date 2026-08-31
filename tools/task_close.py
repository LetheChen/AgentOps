"""task_close 工具：关闭任务（硬约束检查）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 调 orch.close_task
- 硬约束：验收标准全 passed + 文档提案无 pending
- 两步推进：validating → closing → closed
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_close(args: dict) -> dict:
    """关闭任务（硬约束检查 + 两步推进）。

    args:
        task_id (str, required): 任务 ID
        if_version (int, required): 乐观锁版本号
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "关闭失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "关闭失败：缺少 task_id", "error": "missing_task_id"}

    if_version_raw = args.get("if_version")
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        return {"content": f"关闭失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    result = await orch.close_task(task_id=task_id, if_version=if_version)
    if not result.get("ok"):
        return {"content": f"关闭失败：{result.get('message') or result.get('error')}",
                "error": result.get("error"),
                "detail": result.get("detail"),
                "task": result.get("task")}

    task = result["task"]
    return {
        "content": f"已关闭任务 {task.get('identifier', task_id)}（status=closed, closed_at={task.get('closed_at')}）",
        "task": task,
        "if_version": task["version"],
    }
