"""task_detail_get 工具：任务详情只读查询（供分解/调度 agent 获取上下文）。

P2 配套：task_decomposer 的 estimate 节点需要任务标题/描述/风险来评估四因子。
只读，无任何写操作。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_detail_get(args: dict) -> dict:
    """查询任务详情（标题/描述/风险/状态/父子关系）。

    args:
        task_id (str, required): 任务 ID
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "查询失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "查询失败：缺少 task_id", "error": "missing_task_id"}

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "查询失败：任务不存在", "error": "task_not_found"}

    children = [t for t in await orch.store.list_tasks(project_id=task["project_id"], limit=500)
                if t.get("parent_task_id") == task_id]
    deps = await orch.store.list_blocked_by(task_id)

    return {
        "content": (f"任务 {task.get('identifier') or task_id}：{task.get('title', '')}"
                    f"（状态 {task['status']}，风险 {task.get('risk_level', 'medium')}，"
                    f"子任务 {len(children)} 个，前置依赖 {len(deps)} 个）"),
        "task": {
            "task_id": task["task_id"],
            "identifier": task.get("identifier") or task["task_id"],
            "title": task.get("title", ""),
            "description": task.get("description") or "",
            "status": task["status"],
            "risk_level": task.get("risk_level", "medium"),
            "parent_task_id": task.get("parent_task_id"),
            "children": [{"task_id": c["task_id"], "title": c.get("title", ""),
                          "status": c["status"], "risk_level": c.get("risk_level")}
                         for c in children],
            "blocked_by": [{"task_id": d["task_id"], "identifier": d.get("identifier"),
                            "status": d["status"]} for d in deps],
        },
    }
