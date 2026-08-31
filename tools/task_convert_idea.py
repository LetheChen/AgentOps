"""task_convert_idea 工具：灵感转任务。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 调 store.convert_idea_to_task
- idea → backlog，回写 idea.converted_task_id
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_convert_idea(args: dict) -> dict:
    """灵感转任务（idea → backlog）。

    args:
        idea_id (str, required): 灵感 ID
        task_id (str, required): 新任务 ID
        title (str, optional): 任务标题（默认取 idea.content 前 80 字）
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "转换失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    idea_id = (args.get("idea_id") or "").strip()
    if not idea_id:
        return {"content": "转换失败：缺少 idea_id", "error": "missing_idea_id"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "转换失败：缺少 task_id", "error": "missing_task_id"}

    title = (args.get("title") or "").strip() or None

    try:
        task = await orch.store.convert_idea_to_task(idea_id, task_id, title)
    except ValueError as e:
        return {"content": f"转换失败：{e}", "error": "idea_not_found"}
    except Exception as e:
        return {"content": f"转换失败：{e}", "error": "convert_failed"}

    return {
        "content": f"已将灵感 {idea_id} 转为任务 {task.get('identifier', task['task_id'])}（status=backlog）",
        "task": task,
        "task_id": task["task_id"],
        "if_version": task["version"],
    }
