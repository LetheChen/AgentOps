"""task_advance_stage 工具：推进任务状态。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.9.3
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_advance_stage(args: dict) -> dict:
    """推进任务状态（乐观锁 + 冲突重试一次）。

    args:
        task_id (str, required): 任务 ID
        target_status (str, required): 目标状态（idea/backlog/discussing/reviewing/closed）
        if_version (int, required): 乐观锁版本号
        thread_id (str, optional): 对话线程 ID（透传）
        comment (str, optional): 转移备注
        actor (str, optional): user|agent，默认 user
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "推进失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "推进失败：缺少 task_id", "error": "missing_task_id"}

    target_status = (args.get("target_status") or "").strip()
    if not target_status:
        return {"content": "推进失败：缺少 target_status", "error": "missing_target_status"}

    # if_version 可能被 LLM 序列化为字符串，自动转换
    if_version_raw = args.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        return {"content": f"推进失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    thread_id = (args.get("thread_id") or "").strip()
    comment = (args.get("comment") or "").strip()
    actor = (args.get("actor") or "user").strip()

    result = await orch.advance_stage(
        task_id=task_id, target_status=target_status,
        if_version=if_version, actor=actor,
        thread_id=thread_id, comment=comment)

    if not result.get("ok"):
        return {
            "content": f"推进失败：{result.get('message') or result.get('error')}",
            "error": result.get("error"),
            "task": result.get("task"),
        }

    task = result["task"]
    return {
        "content": f"任务 {task.get('identifier', task['task_id'])} 已推进到 {task['status']}",
        "task": task,
        "if_version": task["version"],
        "status": task["status"],
    }
