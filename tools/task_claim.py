"""task_claim 工具：认领任务（claim 协议）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.1
- 调 orch.advance_stage 推进到 in_progress（从 decomposing）
- 校验 thread_id 绑定（已绑定其他对话则拒绝，不抢占）
- backlog 未立项不可认领
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_claim(args: dict) -> dict:
    """认领任务（乐观锁 + thread_id 绑定校验）。

    args:
        task_id (str, required): 任务 ID
        if_version (int, required): 乐观锁版本号
        thread_id (str, required): 当前对话线程 ID
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "认领失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "认领失败：缺少 task_id", "error": "missing_task_id"}

    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return {"content": "认领失败：缺少 thread_id", "error": "missing_thread_id"}

    if_version_raw = args.get("if_version")
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        return {"content": f"认领失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "认领失败：任务不存在", "error": "task_not_found"}

    # v1.2：idea 未立项不可认领（需先用户审核立项进 discussing）
    if task["status"] == "idea":
        return {"content": "拒绝认领：idea 未立项，需用户审核立项后才能认领",
                "error": "not_authorized", "task": task}

    if task["status"] == "in_progress" and task.get("thread_id") and task["thread_id"] != thread_id:
        return {"content": "拒绝认领：任务已被其他对话绑定，不抢占他人认领",
                "error": "claimed_by_other", "task": task}

    result = await orch.advance_stage(
        task_id=task_id, target_status="in_progress",
        if_version=if_version, actor="agent",
        thread_id=thread_id, comment=f"claimed by thread {thread_id}")

    if not result.get("ok"):
        err = result.get("error")
        if err in ("conflict_retry_failed", "update_failed"):
            return {"content": f"认领冲突，请重读任务（当前版本 {result.get('task', {}).get('version')})后重试一次",
                    "error": "version_conflict", "task": result.get("task")}
        return {"content": f"认领失败：{result.get('message') or err}",
                "error": err, "task": result.get("task")}

    task = result["task"]
    return {
        "content": f"已认领任务 {task.get('identifier', task['task_id'])}，开始执行",
        "task": task,
        "if_version": task["version"],
    }
