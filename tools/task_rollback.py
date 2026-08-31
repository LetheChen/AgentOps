"""task_rollback 工具：任务验收回退（三级别名 / 直接指定目标阶段）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 调 orch.rollback_task
- 两种入参（target_status 优先）：
  1. rollback_target: local|partial|global（旧别名）
     local → in_progress / partial → decomposing / global → discussing
  2. target_status: 直接指定目标阶段（in_progress/decomposing/discussing 等），
     走 advance_stage 状态机合法性校验
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_rollback(args: dict) -> dict:
    """任务验收回退（三级别名 / 直接指定目标阶段）。

    args:
        task_id (str, required): 任务 ID
        rollback_target (str, optional): local|partial|global（旧别名）
        target_status (str, optional): 直接指定目标阶段，优先于 rollback_target
        if_version (int, required): 乐观锁版本号
        comment (str, optional): 退回备注
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "回退失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "回退失败：缺少 task_id", "error": "missing_task_id"}

    rollback_target = (args.get("rollback_target") or "").strip()
    target_status = (args.get("target_status") or "").strip()

    # 二选一校验：target_status 优先；都没有则报错
    if not target_status and rollback_target not in ("local", "partial", "global"):
        return {"content": "回退失败：rollback_target 必须为 local/partial/global，"
                           "或直接提供 target_status",
                "error": "invalid_rollback_target"}

    if_version_raw = args.get("if_version")
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        return {"content": f"回退失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    comment = args.get("comment") or ""

    result = await orch.rollback_task(
        task_id=task_id, rollback_target=rollback_target,
        if_version=if_version, comment=comment,
        target_status=target_status)

    if not result.get("ok"):
        return {"content": f"回退失败：{result.get('message') or result.get('error')}",
                "error": result.get("error"),
                "hint": result.get("hint"),
                "task": result.get("task")}

    task = result["task"]
    label = target_status or rollback_target
    return {
        "content": f"已回退任务 {task.get('identifier', task_id)}（{label} → {task['status']}）",
        "task": task,
        "if_version": task["version"],
        "rollback_target": rollback_target,
        "target_status": target_status,
    }
