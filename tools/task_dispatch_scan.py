"""task_dispatch_scan 工具：调度派发扫描（只读，确定性）。

设计文档：docs/product-design/DESIGN_task_lifecycle_automation_v1.md §5.6
- v1.2：扫描 backlog 态任务（可执行任务池）：叶子（无活跃子任务）+ 依赖就绪 → 就绪清单
- 风险门禁已前移到 reviewing→backlog 放行边（agent 不可自动放行 high），
  本工具仍保留 high 复核（防御性双保险，不信任上游）
- 依赖检查：blocks 关系上游非 closed 则挂起（不动状态，下轮再扫）
- 并发占用：in_progress 态任务数（保守口径，占满则本轮不新增派发）
- 父任务收尾：backlog 态父任务等子任务全部终态 → finalize 清单（backlog→validating）
- 判定逻辑 100% 确定性（无 LLM 参与），LLM（gate 节点）只能复核/剔除候选
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"closed", "canceled", "abandoned"}

_DISPATCH_YAML = Path(__file__).resolve().parents[1] / "config" / "dispatch.yaml"


def _load_dispatch_config() -> dict:
    """读 config/dispatch.yaml（不存在时回退默认值）。"""
    defaults = {"concurrency": {"max_concurrent": 2}}
    try:
        with open(_DISPATCH_YAML, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {**defaults, **cfg}
    except Exception as e:
        logger.warning("dispatch.yaml 加载失败，使用默认值: %s", e)
        return defaults


def _summary(task: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "identifier": task.get("identifier") or task["task_id"],
        "title": task.get("title", ""),
        "risk_level": task.get("risk_level", "medium"),
    }


async def task_dispatch_scan(args: dict) -> dict:
    """调度派发扫描（只读）：就绪/待审/挂起清单 + 并发状态。

    args:
        project_id (str, optional): 项目过滤（空 = 全项目）
    """
    from orchestrator._registry import get_task_orchestrator
    from task.status import resolve_review_gate

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "扫描失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    project_id = (args.get("project_id") or "").strip()
    cfg = _load_dispatch_config()
    max_concurrent = int((cfg.get("concurrency") or {}).get("max_concurrent", 2))

    tasks = await orch.store.list_tasks(project_id=project_id, limit=500)

    # 子任务索引：parent_task_id → [子任务]
    children_map: dict[str, list[dict]] = {}
    for t in tasks:
        pid = t.get("parent_task_id")
        if pid:
            children_map.setdefault(pid, []).append(t)

    ready_tasks: list[dict] = []       # 可自动派发（backlog 叶子 + 依赖就绪 + low/medium）
    manual_review: list[dict] = []     # high 风险：强制人工确认（防御性复核）
    waiting_deps: list[dict] = []      # 依赖未就绪（上游非 closed）
    waiting_children: list[dict] = []  # 父任务等子任务
    parents_to_finalize: list[dict] = []  # 子任务全终态的父任务（backlog→validating 收尾）
    running = 0

    for t in tasks:
        status = t["status"]
        if status == "in_progress":
            running += 1  # 并发占用（保守口径）
            continue
        if status != "backlog":
            continue

        # 父任务：有子任务时不派发，等子任务全部终态
        children = children_map.get(t["task_id"]) or []
        if children:
            active = [c for c in children if c["status"] not in _TERMINAL_STATUSES]
            if active:
                waiting_children.append({
                    **_summary(t),
                    "active_children": len(active),
                    "total_children": len(children),
                })
            else:
                parents_to_finalize.append({
                    **_summary(t),
                    "finished_children": len(children),
                })
            continue

        # 叶子任务：依赖检查（blocks 上游全部 closed 才就绪）
        deps = await orch.store.list_blocked_by(t["task_id"])
        pending = [d for d in deps if d["status"] not in _TERMINAL_STATUSES]
        if pending:
            waiting_deps.append({
                **_summary(t),
                "pending_deps": [{"task_id": d["task_id"],
                                  "identifier": d.get("identifier") or d["task_id"],
                                  "status": d["status"]} for d in pending],
            })
            continue

        # 风险门禁：low/medium 自动；high 人工
        gate = resolve_review_gate(t.get("risk_level", "medium"))
        if gate == "manual":
            manual_review.append({**_summary(t), "gate": "manual"})
        else:
            ready_tasks.append({**_summary(t), "gate": gate})

    available = max(0, max_concurrent - running)
    # 并发余量为 0 时本轮不给出派发清单（execute 侧也会二次校验）
    dispatchable = ready_tasks[:available] if available > 0 else []
    scan = {
        "ready_tasks": dispatchable,
        "manual_review": manual_review,
        "waiting_deps": waiting_deps,
        "waiting_children": waiting_children,
        "parents_to_finalize": parents_to_finalize,
        "concurrency": {"running": running, "max": max_concurrent,
                        "available": available},
        "summary": {
            "ready": len(ready_tasks),
            "manual": len(manual_review),
            "waiting_deps": len(waiting_deps),
            "waiting_children": len(waiting_children),
            "parents_to_finalize": len(parents_to_finalize),
            "running": running,
        },
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("task_dispatch_scan 完成: %s", scan["summary"])
    return {
        "content": (
            f"扫描完成：可派发 {scan['summary']['ready']}（并发余量 {available}），"
            f"高风险待审 {scan['summary']['manual']}，等依赖 {scan['summary']['waiting_deps']}，"
            f"等子任务 {scan['summary']['waiting_children']}，"
            f"待收尾父任务 {scan['summary']['parents_to_finalize']}"
        ),
        "scan": scan,
    }
