"""task_decompose_apply 工具：拆分落库（创建子任务 + 父子关系）。

设计文档：docs/product-design/DESIGN_task_lifecycle_automation_v1.md §5.4/§5.5
- 只在 strategy=recursive（决策树判定拆分）时调用
- v1.2：子任务创建为 reviewing 态（跟随父任务等评审，父任务评审通过时级联入待办池）
- v1.2：高风险门禁统一在 reviewing→backlog 放行边（agent 不可自动放行）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 父任务允许拆分的活跃状态（decomposing 为主；讨论中允许提前拆但不常见）
_PARENT_STATUSES = {"discussing", "decomposing"}
_RISK_VALUES = {"low", "medium", "high"}


async def task_decompose_apply(args: dict) -> dict:
    """按拆分方案创建子任务（父子关系 + agent 创建者标记 + 高风险人审通知）。

    args:
        task_id (str, required): 父任务 ID
        subtasks (list, required): [{"title": "...", "description": "...",
                                     "risk_level": "low|medium|high"}]
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "拆分失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    subtasks = args.get("subtasks") or []
    if not task_id:
        return {"content": "拆分失败：缺少 task_id", "error": "missing_task_id"}
    if not isinstance(subtasks, list) or not subtasks:
        return {"content": "拆分失败：subtasks 必须是非空数组", "error": "missing_subtasks"}

    parent = await orch.store.get_task(task_id)
    if not parent:
        return {"content": "拆分失败：父任务不存在", "error": "task_not_found"}
    if parent["status"] not in _PARENT_STATUSES:
        return {"content": f"拆分失败：父任务状态 {parent['status']} 不允许拆分"
                           f"（需 discussing/decomposing）",
                "error": "illegal_parent_status"}

    created: list[dict] = []
    for i, sub in enumerate(subtasks):
        title = (sub.get("title") or "").strip()
        if not title:
            created.append({"index": i, "ok": False, "error": "missing_title"})
            continue
        risk = (sub.get("risk_level") or parent.get("risk_level") or "medium").strip()
        if risk not in _RISK_VALUES:
            risk = "medium"
        sub_id = f"{task_id}_sub{i+1}"
        row = await orch.store.create_task(
            task_id=sub_id, project_id=parent["project_id"], title=title,
            description=sub.get("description") or "", status="reviewing",
            task_type=parent.get("task_type", "code"), risk_level=risk,
            creator_type="agent", creator_id="task_decomposer",
            creator_name="任务分解师", thread_id=parent.get("thread_id") or "",
            sort_order=i + 1, parent_task_id=task_id)
        created.append({
            "index": i, "ok": True, "task_id": sub_id,
            "title": row.get("title"),
            "risk_level": row.get("risk_level"),
        })

    ok_count = sum(1 for c in created if c.get("ok"))

    # 记录父任务活动（拆分审计）
    await orch.store.add_activity(
        task_id=task_id, actor_type="agent", actor_id="task_decomposer",
        actor_name="任务分解师",
        changes={"action": "decompose", "created_subtasks": ok_count})

    # 高风险父任务：拆分方案人审门禁（评论区 @用户，决策 #7）
    notified = False
    if parent.get("risk_level") == "high" and ok_count:
        sub_list = "\n".join(
            f"- {c['title']}（风险 {c['risk_level']}）" for c in created if c.get("ok"))
        await orch.store.add_comment(
            task_id=task_id, author_type="agent", author_id="task_decomposer",
            author_name="任务分解师", comment_type="discussion",
            body=(f"⚠ 本任务为高风险，拆分方案已生成（{ok_count} 个子任务），"
                  f"请审阅确认后方可进入自动调度：\n{sub_list}\n\n"
                  f"如需调整请回复本评论；确认后调度器将按依赖关系派发执行。"),
            mentions=["user"])
        notified = True

    logger.info("task_decompose_apply: %s 创建 %d 个子任务（人审通知=%s）",
                task_id, ok_count, notified)
    return {
        "content": f"拆分落库完成：创建 {ok_count}/{len(subtasks)} 个子任务"
                   + ("（高风险方案已 @用户 审阅）" if notified else ""),
        "ok": ok_count == len(subtasks),
        "subtasks": created,
        "created": ok_count,
        "user_notified": notified,
    }
