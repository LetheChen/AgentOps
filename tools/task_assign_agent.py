"""task_assign_agent 工具：指派 agent + 风格。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 更新 tasks.assignee_* + style_id（调 store.update_task_fields）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_assign_agent(args: dict) -> dict:
    """指派 agent 与执行风格到任务。

    args:
        task_id (str, required): 任务 ID
        if_version (int, required): 乐观锁版本号
        assignee_type (str, optional): 指派对象类型（agent/role/user）
        assignee_id (str, optional): 指派对象 ID
        assignee_name (str, optional): 指派对象名称
        style_id (str, optional): 执行风格 ID
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "指派失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "指派失败：缺少 task_id", "error": "missing_task_id"}

    if_version_raw = args.get("if_version")
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        return {"content": f"指派失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    fields = {}
    for key in ("assignee_type", "assignee_id", "assignee_name", "style_id"):
        val = args.get(key)
        if val is not None and str(val).strip() != "":
            fields[key] = str(val).strip()

    if not fields:
        return {"content": "指派失败：至少需提供一个 assignee_* 或 style_id 字段",
                "error": "missing_assignee_fields"}

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "指派失败：任务不存在", "error": "task_not_found"}

    updated = await orch.store.update_task_fields(task_id, if_version, **fields)
    if updated is None:
        latest = await orch.store.get_task(task_id)
        return {"content": f"指派失败：乐观锁冲突（当前版本 {latest['version'] if latest else '?'}）",
                "error": "version_conflict", "task": latest}

    try:
        await orch.store.add_activity(
            task_id=task_id, actor_type="user", actor_name="manager",
            changes={"assign": {"before": {
                        k: task.get(k) for k in fields},
                    "after": fields}})
    except Exception as e:
        logger.debug("add_activity 失败（不阻塞主流程）: %s", e)

    return {
        "content": f"已指派任务 {updated.get('identifier', task_id)}（assignee={updated.get('assignee_name') or updated.get('assignee_id') or '-'}, style={updated.get('style_id') or '-'}）",
        "task": updated,
        "if_version": updated["version"],
    }
