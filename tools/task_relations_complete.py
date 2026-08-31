"""task_relations_complete 工具：关系与风险补全。

设计文档：docs/product-design/DESIGN_task_lifecycle_automation_v1.md §5.5
- 分解 agent 评估补全：上下游（父子关系由 decompose_apply 建）、阻断关系、风险级别
- 落库复用现有能力：blocks relations（store.add_relation，自带环检测）
- 风险级别变更必须带理由（升级/降级可解释）
- 补全结果以 diff 形式发评论区：中低风险静默落库，高风险必须 @用户 确认
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_RISK_VALUES = {"low", "medium", "high"}


async def task_relations_complete(args: dict) -> dict:
    """补全任务阻断关系与风险级别（幂等，支持批量）。

    args:
        blocks (list, optional): [{"source": task_id, "target": task_id}]
                                 source 阻塞 target（source 完成前 target 不派发）
        risk_updates (list, optional): [{"task_id": "...", "risk_level": "low|medium|high",
                                         "reason": "升级/降级理由"}]
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "补全失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    blocks = args.get("blocks") or []
    risk_updates = args.get("risk_updates") or []
    if not blocks and not risk_updates:
        return {"content": "补全失败：blocks 与 risk_updates 至少提供一项", "error": "empty_input"}

    results: list[dict] = []
    notified_tasks: set[str] = set()

    # 1. 阻断关系（blocks）
    for b in blocks:
        source = (b.get("source") or "").strip()
        target = (b.get("target") or "").strip()
        if not source or not target:
            results.append({"kind": "block", "ok": False, "error": "missing_task_id"})
            continue
        for tid in (source, target):
            if not await orch.store.get_task(tid):
                results.append({"kind": "block", "ok": False,
                                "error": "task_not_found", "task_id": tid})
                break
        else:
            if source == target:
                results.append({"kind": "block", "ok": False,
                                "error": "self_block", "task_id": source})
                continue
            r = await orch.store.add_relation(source, target, "blocks")
            results.append({
                "kind": "block", "ok": bool(r.get("ok")),
                "source": source, "target": target,
                "error": r.get("error")})
            if not r.get("ok") and r.get("error") == "relation_cycle":
                logger.warning("blocks 关系形成环，已拒绝: %s → %s", source, target)

    # 2. 风险级别更新（必须带理由）
    risk_changed: list[dict] = []
    for ru in risk_updates:
        tid = (ru.get("task_id") or "").strip()
        risk = (ru.get("risk_level") or "").strip()
        reason = (ru.get("reason") or "").strip()
        if not tid or risk not in _RISK_VALUES:
            results.append({"kind": "risk", "ok": False, "task_id": tid,
                            "error": "invalid_args"})
            continue
        task = await orch.store.get_task(tid)
        if not task:
            results.append({"kind": "risk", "ok": False, "task_id": tid,
                            "error": "task_not_found"})
            continue
        if task.get("risk_level") == risk:
            continue  # 无变化，跳过（幂等）
        if not reason:
            results.append({"kind": "risk", "ok": False, "task_id": tid,
                            "error": "missing_reason",
                            "message": "风险级别变更必须带理由"})
            continue
        updated = await orch.store.update_task_fields(
            tid, task["version"], risk_level=risk)
        if updated:
            direction = "升级" if _RISK_VALUES and \
                list(_RISK_VALUES).index(risk) > list(_RISK_VALUES).index(task["risk_level"]) \
                else "降级"
            risk_changed.append({
                "task_id": tid, "identifier": updated.get("identifier") or tid,
                "from": task["risk_level"], "to": risk,
                "direction": direction, "reason": reason})
            results.append({"kind": "risk", "ok": True, "task_id": tid,
                            "from": task["risk_level"], "to": risk})
            # 风险升级到 high 的任务：@用户 确认（调度侧亦不自动派发）
            if risk == "high":
                notified_tasks.add(tid)
        else:
            results.append({"kind": "risk", "ok": False, "task_id": tid,
                            "error": "version_conflict"})

    # 3. diff 通知：中低风险静默落库，高风险 @用户（§5.5）
    for tid in notified_tasks:
        t = await orch.store.get_task(tid)
        if t:
            await orch.store.add_comment(
                task_id=tid, author_type="agent", author_id="task_decomposer",
                author_name="任务分解师", comment_type="discussion",
                body=(f"⚠ 风险级别已补全为 **high**（原 {t['risk_level']}）。"
                      f"该任务将不进入自动调度，需您确认派发方式。"),
                mentions=["user"])

    ok_count = sum(1 for r in results if r.get("ok"))
    logger.info("task_relations_complete: %d/%d 成功（风险变更 %d，@用户 %d）",
                ok_count, len(results), len(risk_changed), len(notified_tasks))

    # v1.2：分解 workflow 最后一步完成后，涉及的 decomposing 态任务自动提交评审
    #    （拆分+关系补全 = 拆解工作完成，reviewing 评审拆分方案）
    submitted: list[dict] = []
    if ok_count:
        candidate_ids = {b.get("source") for b in blocks} | {b.get("target") for b in blocks}
        candidate_ids.discard(None)
        for tid in sorted(candidate_ids):
            t = await orch.store.get_task(tid)
            if t and t["status"] == "decomposing":
                r = await orch.advance_stage(
                    task_id=tid, target_status="reviewing",
                    if_version=t["version"], actor="agent",
                    comment="task_relations_complete 拆解完成，自动提交评审")
                submitted.append({"task_id": tid,
                                  "ok": bool(r.get("ok")),
                                  "error": r.get("error")})

    return {
        "content": f"关系与风险补全完成：{ok_count}/{len(results)} 成功"
                   f"（blocks {len(blocks)} 项，风险变更 {len(risk_changed)} 项，"
                   f"提交评审 {sum(1 for s in submitted if s.get('ok'))} 项）",
        "ok": ok_count == len(results),
        "results": results,
        "risk_changed": risk_changed,
        "user_notified": sorted(notified_tasks),
        "submitted_to_review": submitted,
    }
