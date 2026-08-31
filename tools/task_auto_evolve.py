"""task_auto_evolve 工具：task_conductor 确定性演进执行（批量）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.10.4
- 每个动作 = advance_stage（状态机单一事实源），单条失败不影响其余
- 工具端二次校验（不信任 LLM decisions）：abandon 只允许活跃态、unblock 只允许 blocked
- requires_user=True 的转移由 advance_stage 既有守卫兜底（actor=agent 被拒）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 动作 → 目标状态映射（白名单，LLM 不能发明新动作）
_ACTION_TARGET = {"abandon": "abandoned", "unblock": "in_progress"}


async def task_auto_evolve(args: dict) -> dict:
    """批量执行确定性演进动作。

    args:
        actions (list, required): [{"task_id": "...", "action": "abandon"|"unblock"}]
        thread_id (str, optional): 调度 run 的 session_id（审计用）
    """
    from orchestrator._registry import get_task_orchestrator
    from task.status import can_transition

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "执行失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    actions = args.get("actions") or []
    if not isinstance(actions, list) or not actions:
        return {"content": "执行失败：actions 必须是非空数组", "error": "missing_actions"}

    thread_id = (args.get("thread_id") or "").strip()
    results: list[dict] = []

    for act in actions:
        task_id = (act.get("task_id") or "").strip()
        action = (act.get("action") or "").strip()

        if not task_id:
            results.append({"task_id": "", "ok": False, "error": "missing_task_id"})
            continue
        if action not in _ACTION_TARGET:
            results.append({"task_id": task_id, "ok": False,
                            "error": "unknown_action", "action": action})
            continue

        target = _ACTION_TARGET[action]
        task = await orch.store.get_task(task_id)
        if not task:
            results.append({"task_id": task_id, "ok": False, "error": "task_not_found"})
            continue

        # 工具端二次校验：只放行 scan 候选的合法形态（防 LLM 臆造）
        if not can_transition(task["status"], target):
            results.append({
                "task_id": task_id, "ok": False, "error": "illegal_transition",
                "message": f"{task['status']} → {target} 不是合法转移",
            })
            continue

        comment = ("task_conductor 自动归档（不活跃）" if action == "abandon"
                   else "task_conductor 自动解除阻塞（依赖就绪）")
        r = await orch.advance_stage(
            task_id=task_id, target_status=target,
            if_version=task["version"], actor="agent",
            thread_id=thread_id, comment=comment)

        if r.get("ok"):
            t = r["task"]
            results.append({
                "task_id": task_id, "ok": True, "action": action,
                "status": t["status"], "if_version": t["version"],
                "identifier": t.get("identifier") or task_id,
            })
        else:
            results.append({
                "task_id": task_id, "ok": False, "action": action,
                "error": r.get("error"), "message": r.get("message"),
            })

    ok_count = sum(1 for x in results if x["ok"])
    all_ok = ok_count == len(results)
    logger.info("task_auto_evolve 完成: %d/%d 成功", ok_count, len(results))
    return {
        "content": f"演进执行完成：{ok_count}/{len(results)} 成功",
        "ok": all_ok,
        "actions": results,
        "executed": ok_count,
    }
