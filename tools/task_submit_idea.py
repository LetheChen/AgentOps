"""task_submit_idea 工具：建灵感任务（status=idea）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.9.3
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def task_submit_idea(args: dict) -> dict:
    """建灵感任务。

    args:
        project_id (str, required): 项目 ID
        title (str, required): 任务标题
        description (str, optional): 任务描述
        thread_id (str, optional): 对话线程 ID（V1 claim 协议依赖）
        creator_id (str, optional): 创建者 ID
        creator_name (str, optional): 创建者名称
        risk_level (str, optional): low|medium|high，默认 medium
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "创建失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    project_id = (args.get("project_id") or "").strip()
    if not project_id:
        return {"content": "创建失败：缺少 project_id", "error": "missing_project_id"}

    title = (args.get("title") or "").strip()
    if not title:
        return {"content": "创建失败：缺少 title", "error": "missing_title"}

    description = (args.get("description") or "").strip()
    thread_id = (args.get("thread_id") or "").strip()
    creator_id = (args.get("creator_id") or "").strip()
    creator_name = (args.get("creator_name") or "").strip()
    risk_level = (args.get("risk_level") or "medium").strip()

    task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    result = await orch.submit_idea(
        task_id=task_id, project_id=project_id, title=title,
        description=description, thread_id=thread_id,
        creator_id=creator_id, creator_name=creator_name,
        risk_level=risk_level)
    if not result.get("ok"):
        return {"content": f"创建失败：{result.get('error')}", "error": result.get("error")}

    task = result["task"]
    return {
        "content": f"已创建任务 {task.get('identifier', task['task_id'])}（status=idea）",
        "task": task,
        "task_id": task["task_id"],
        "if_version": task["version"],
    }
