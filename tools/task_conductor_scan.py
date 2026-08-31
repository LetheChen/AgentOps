"""task_conductor_scan 工具：任务调度定时扫描（只读）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.10.3
- 判定逻辑 100% 确定性（无 LLM 参与），LLM（evaluate 节点）只能复核/剔除候选
- 五类输出：归档候选 / 解除阻塞候选 / 卡死任务 / 长期未处理 idea / 建议开工任务
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# 终态集合（依赖就绪判定用）
_TERMINAL_STATUSES = {"closed", "canceled", "abandoned"}


def _to_utc(ts: str | None) -> datetime | None:
    """ISO 8601 字符串 → aware datetime（无法解析返回 None）。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _summary(task: dict, idle_days: int) -> dict:
    """任务摘要（scan 输出瘦身，避免整行透传）。"""
    return {
        "task_id": task["task_id"],
        "identifier": task.get("identifier") or task["task_id"],
        "title": task.get("title", ""),
        "status": task["status"],
        "risk_level": task.get("risk_level", "medium"),
        "idle_days": idle_days,
        "last_activity_at": task.get("last_activity_at"),
    }


async def task_conductor_scan(args: dict) -> dict:
    """调度扫描（只读）：返回五类候选/提示清单。

    args:
        project_id (str, optional): 项目过滤（空 = 全项目）
        inactive_days (int, optional): 不活跃归档阈值，默认 30
        stuck_days (int, optional): 卡死/催熟判定阈值，默认 7
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "扫描失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    project_id = (args.get("project_id") or "").strip()
    try:
        inactive_days = int(args.get("inactive_days", 30))
        stuck_days = int(args.get("stuck_days", 7))
    except (ValueError, TypeError):
        return {"content": "扫描失败：inactive_days/stuck_days 必须是整数", "error": "invalid_threshold"}

    tasks = await orch.store.list_active_tasks_with_last_activity(project_id=project_id)
    now = datetime.now(timezone.utc)

    abandoned_candidates: list[dict] = []
    stuck_tasks: list[dict] = []
    stale_ideas: list[dict] = []

    for t in tasks:
        last = _to_utc(t.get("last_activity_at")) or _to_utc(t.get("updated_at"))
        if last is None:
            continue
        idle_days = (now - last).days
        if idle_days >= inactive_days:
            # 职责 1：不活跃归档候选（所有活跃态）
            abandoned_candidates.append(_summary(t, idle_days))
        elif idle_days >= stuck_days:
            if t["status"] in ("in_progress", "validating"):
                # 职责 3：卡死检测（只报告）
                stuck_tasks.append(_summary(t, idle_days))
            elif t["status"] == "idea":
                # 职责 4：idea 催熟提示（只报告，不自动立项）
                stale_ideas.append(_summary(t, idle_days))

    # 职责 2：依赖就绪解除候选（blocked 且 blocked_by 全部终态）
    unblock_candidates: list[dict] = []
    for t in tasks:
        if t["status"] != "blocked":
            continue
        deps = await orch.store.list_blocked_by(t["task_id"])
        if deps and all(d["status"] in _TERMINAL_STATUSES for d in deps):
            unblock_candidates.append({
                **_summary(t, 0),
                "deps": [{"task_id": d["task_id"], "identifier": d.get("identifier"),
                          "status": d["status"]} for d in deps],
            })

    # 职责 5：建议开工清单（backlog 任务，无未完成 blocked_by，按创建时间排序）
    ready_tasks: list[dict] = []
    for t in tasks:
        if t["status"] != "backlog":
            continue
        deps = await orch.store.list_blocked_by(t["task_id"])
        if not deps or all(d["status"] in _TERMINAL_STATUSES for d in deps):
            ready_tasks.append({
                "task_id": t["task_id"],
                "identifier": t.get("identifier") or t["task_id"],
                "title": t.get("title", ""),
                "risk_level": t.get("risk_level", "medium"),
                "created_at": t.get("created_at"),
                "blocked_by_pending": len([d for d in deps
                                           if d["status"] not in _TERMINAL_STATUSES]),
            })
    ready_tasks.sort(key=lambda x: (x["risk_level"] != "high", x.get("created_at") or ""))

    scan = {
        "abandoned_candidates": abandoned_candidates,
        "unblock_candidates": unblock_candidates,
        "stuck_tasks": stuck_tasks,
        "stale_ideas": stale_ideas,
        "ready_tasks": ready_tasks,
        "summary": {
            "active_tasks": len(tasks),
            "abandon": len(abandoned_candidates),
            "unblock": len(unblock_candidates),
            "stuck": len(stuck_tasks),
            "stale_ideas": len(stale_ideas),
            "ready": len(ready_tasks),
        },
        "scanned_at": now.isoformat(),
        "thresholds": {"inactive_days": inactive_days, "stuck_days": stuck_days},
    }
    logger.info("task_conductor_scan 完成: %s", scan["summary"])
    return {
        "content": (
            f"扫描完成：活跃 {scan['summary']['active_tasks']}，"
            f"归档候选 {scan['summary']['abandon']}，解除阻塞候选 {scan['summary']['unblock']}，"
            f"卡死 {scan['summary']['stuck']}，催熟 {scan['summary']['stale_ideas']}，"
            f"可开工 {scan['summary']['ready']}"
        ),
        "scan": scan,
    }
