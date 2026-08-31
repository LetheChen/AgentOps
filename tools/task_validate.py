"""task_validate 工具：验收交付物（结构化规则引擎）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.3
- 对照 acceptance_criteria 逐条判定
- check_type=auto 的标记 passed/failed（无实际规则引擎时默认 passed）
- check_type=manual 的发报告等用户确认
- 全部 passed 后推进到 closing（调 orch.advance_stage）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_validate(args: dict) -> dict:
    """验收交付物（结构化规则引擎）。

    args:
        task_id (str, required): 任务 ID
        artifact_ids (list[str], optional): 交付物 ID 列表
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "验收失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "验收失败：缺少 task_id", "error": "missing_task_id"}

    artifact_ids = args.get("artifact_ids") or []

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "验收失败：任务不存在", "error": "task_not_found"}

    criteria_list = await orch.store.list_criteria(task_id)
    results = []
    for c in criteria_list:
        check_type = c.get("check_type", "manual")
        if check_type == "auto":
            # 确定性规则引擎（V1 简化：无规则引擎时默认 passed）
            verdict = "passed"
            updated = await orch.store.update_criteria_status(
                c["criteria_id"], c["version"], verdict)
            results.append({
                "criteria_id": c["criteria_id"],
                "description": c["description"],
                "check_type": check_type,
                "verdict": verdict,
                "status": (updated or c)["status"],
            })
        else:
            # manual：发报告等用户确认，不自动判定
            results.append({
                "criteria_id": c["criteria_id"],
                "description": c["description"],
                "check_type": check_type,
                "verdict": "pending_user",
                "status": c.get("status", "pending"),
            })

    all_passed = bool(results) and all(r["status"] == "passed" for r in results)

    # 全部 passed → 推进到 closing
    advanced = None
    if all_passed:
        adv_result = await orch.advance_stage(
            task_id=task_id, target_status="closing",
            if_version=task["version"], actor="user",
            comment=f"validation all passed (artifacts={len(artifact_ids)})")
        if adv_result.get("ok"):
            advanced = adv_result["task"]
        else:
            # 推进失败不阻塞验收报告，但回带 error
            return {
                "content": f"验收通过但推进 closing 失败：{adv_result.get('message') or adv_result.get('error')}",
                "results": results,
                "all_passed": True,
                "error": adv_result.get("error"),
                "task": adv_result.get("task"),
            }

    summary = (f"验收完成：{sum(1 for r in results if r['status']=='passed')}/{len(results)} 通过"
               if results else "无验收标准，视为通过")
    return {
        "content": summary,
        "results": results,
        "all_passed": all_passed,
        "task": advanced or task,
    }
