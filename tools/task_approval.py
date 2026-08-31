"""task_approval 工具：发起任务评审（风险分级）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.2
- v1.2 适配：reviewing 已变主线必经节点（拆解后评审拆分方案），
  高风险门禁统一在 reviewing→backlog 放行边（agent 不可自动放行），
  本工具不再把高风险任务单独拐进 reviewing
- low → 自动通过标记 approved，进 decomposing
- medium → 通知用户，可放行
- high → 正常进 decomposing，拆解完成后在 reviewing 由用户确认放行
"""
from __future__ import annotations

import logging

from task.status import resolve_review_gate

logger = logging.getLogger(__name__)


async def task_approval(args: dict) -> dict:
    """发起任务评审（轻量风险分级 + 用户确认，非 OA 审批流）。

    args:
        task_id (str, required): 任务 ID
        risk_level (str, required): low|medium|high
        if_version (int, required): 乐观锁版本号
        proposal_summary (str, optional): 方案摘要
        impact_analysis (str, optional): 影响面分析
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "评审失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "评审失败：缺少 task_id", "error": "missing_task_id"}

    risk_level = (args.get("risk_level") or "").strip()
    if risk_level not in ("low", "medium", "high"):
        return {"content": "评审失败：risk_level 必须为 low/medium/high", "error": "invalid_risk_level"}

    if_version_raw = args.get("if_version")
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        return {"content": f"评审失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    proposal_summary = (args.get("proposal_summary") or "").strip()
    impact_analysis = (args.get("impact_analysis") or "").strip()

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "评审失败：任务不存在", "error": "task_not_found"}

    gate = resolve_review_gate(risk_level)

    if gate == "auto":
        # low：自动通过，标记 approved + 推进到 decomposing
        updated = await orch.store.update_task_fields(
            task_id, if_version, risk_level=risk_level, approved=1)
        if updated is None:
            latest = await orch.store.get_task(task_id)
            return {"content": f"评审失败：乐观锁冲突（当前版本 {latest['version'] if latest else '?'}）",
                    "error": "version_conflict", "task": latest}
        result = await orch.advance_stage(
            task_id=task_id, target_status="decomposing",
            if_version=updated["version"], actor="user",
            comment=f"approval auto-passed (low risk): {proposal_summary}")
        if not result.get("ok"):
            return {"content": f"评审通过但推进失败：{result.get('message') or result.get('error')}",
                    "error": result.get("error"), "gate": gate, "approved": True,
                    "task": result.get("task")}
        task = result["task"]
        return {
            "content": f"低风险自动通过，已推进到 decomposing（任务 {task.get('identifier', task_id)}）",
            "gate": gate,
            "approved": True,
            "task": task,
        }

    if gate == "manual":
        # v1.2 high：正常进 decomposing（拆解照常），拆解完成后 reviewing 由用户放行
        updated = await orch.store.update_task_fields(
            task_id, if_version, risk_level=risk_level)
        if updated is None:
            latest = await orch.store.get_task(task_id)
            return {"content": f"评审失败：乐观锁冲突（当前版本 {latest['version'] if latest else '?'}）",
                    "error": "version_conflict", "task": latest}
        result = await orch.advance_stage(
            task_id=task_id, target_status="decomposing",
            if_version=updated["version"], actor="user",
            comment=f"approval: 高风险，正常拆解，评审放行需用户确认（{proposal_summary}）")
        if not result.get("ok"):
            return {"content": f"推进 decomposing 失败：{result.get('message') or result.get('error')}",
                    "error": result.get("error"), "gate": gate, "approved": False,
                    "task": result.get("task")}
        task = result["task"]
        return {
            "content": (f"高风险任务进入拆解（任务 {task.get('identifier', task_id)}），"
                        "拆解完成后评审阶段需用户确认放行入待办池"),
            "gate": gate,
            "approved": False,
            "task": task,
        }

    # medium：通知用户，可放行（不自动推进）
    await orch.store.update_task_fields(task_id, if_version, risk_level=risk_level)
    latest = await orch.store.get_task(task_id)
    return {
        "content": (f"中风险任务已通知用户（任务 {latest.get('identifier', task_id)}），"
                    "等待用户放行；方案摘要=" + proposal_summary if proposal_summary
                    else f"中风险任务已通知用户（任务 {latest.get('identifier', task_id)}），等待用户放行"),
        "gate": gate,
        "approved": False,
        "task": latest,
    }
