"""list_tasks 工具：查任务列表（只读便捷工具）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.9.4
不计入 §4.5 的 14 个业务工具，P0 供 agent 查看任务列表与版本号。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def list_tasks(args: dict) -> dict:
    """查任务列表。

    args:
        project_id (str, optional): 项目 ID 过滤
        status (str, optional): 状态过滤
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "查询失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    project_id = (args.get("project_id") or "").strip()
    status = (args.get("status") or "").strip()

    # 直接用 store 查（带过滤）
    tasks = await orch.store.list_tasks(project_id=project_id, status=status)

    if not tasks:
        return {"content": "无任务", "tasks": []}

    # 精简输出（供 LLM 阅读）
    lines = [f"- {t.get('identifier', t['task_id'][-8:])} | {t['status']} | v{t['version']} | {t['title']}"
             for t in tasks]
    return {
        "content": f"共 {len(tasks)} 个任务：\n" + "\n".join(lines),
        "tasks": tasks,
    }
