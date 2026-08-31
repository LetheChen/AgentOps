"""task_dispatch_execute 工具：执行派发（写操作，含父任务收尾推进）。

设计文档：docs/product-design/DESIGN_task_lifecycle_automation_v1.md §5.6
- 执行者选择：ε-greedy（20% 均匀随机探索）+ 声望加权轮询（时间衰减有效声望 + 保底权重）
  反马太效应：衰减打断「吃老本」（时间维），ε 打断「饥饿」（机会维）
- v1.2 派发链路：advance_stage(backlog→in_progress) → execute_coding(harness)
- medium 风险：派发后评论区 @用户（知情可召回，通知不是等待确认）
- v1.2 父任务收尾：子任务全部终态 → backlog→validating（一步直进验收链路）
- 工具端二次校验（不信任 LLM decisions）：状态/依赖/风险/并发全量复核
"""
from __future__ import annotations

import logging
import random
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"closed", "canceled", "abandoned"}
_DISPATCH_YAML = Path(__file__).resolve().parents[1] / "config" / "dispatch.yaml"


def _load_dispatch_config() -> dict:
    """读 config/dispatch.yaml（不存在时回退默认值）。"""
    defaults = {
        "concurrency": {"max_concurrent": 2},
        "reputation": {"half_life_days": 30, "epsilon": 0.2, "base_weight": 10.0},
        "executors": [{"agent_id": "coding_agent", "harness": "codex",
                       "enabled": True}],
    }
    try:
        with open(_DISPATCH_YAML, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {**defaults, **cfg}
    except Exception as e:
        logger.warning("dispatch.yaml 加载失败，使用默认值: %s", e)
        return defaults


async def _select_executor(orch, rep_cfg: dict, executors: list[dict]) -> dict:
    """ε-greedy + 声望加权轮询选择执行者（反马太效应，DESIGN §5.2/§5.6）。

    - ε 概率：均匀随机（探索，防新 agent 饥饿）
    - 1-ε 概率：加权随机（利用，权重 = 时间衰减有效声望 + 保底权重）
    """
    half_life = float(rep_cfg.get("half_life_days", 30))
    epsilon = float(rep_cfg.get("epsilon", 0.2))
    base_weight = float(rep_cfg.get("base_weight", 10.0))

    # 声望查询（账本空的新 agent 有效声望为 0，由 base_weight 保底）
    candidates: list[dict] = []
    for ex in executors:
        agent_id = ex.get("agent_id") or ""
        if not agent_id or not ex.get("enabled", True):
            continue
        rep = await orch.store.effective_reputation(agent_id, half_life_days=half_life)
        candidates.append({
            "agent_id": agent_id,
            "harness": ex.get("harness", "codex"),
            "reputation": rep,
            "weight": rep + base_weight,
        })
    if not candidates:
        return {"agent_id": "coding_agent", "harness": "codex",
                "reputation": 0.0, "weight": 0.0, "explore": False}

    explored = random.random() < epsilon
    if explored or len(candidates) == 1:
        chosen = random.choice(candidates)
        return {**chosen, "explore": explored}

    weights = [c["weight"] for c in candidates]
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    return {**chosen, "explore": False}


async def task_dispatch_execute(args: dict) -> dict:
    """按就绪清单派发执行（每任务：门禁复核 → 选 executor → 推进 → 派发）。

    args:
        task_ids (list, required): 派发任务 ID 清单（来自 scan 的 ready_tasks）
        parent_finalizes (list, optional): 子任务全完成的父任务 ID（推进验收链路）
        thread_id (str, optional): 调度 run 的 session_id（审计用）
    """
    from orchestrator._registry import get_task_orchestrator
    from task.status import resolve_review_gate

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "派发失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_ids = args.get("task_ids") or []
    parent_finalizes = args.get("parent_finalizes") or []
    if not isinstance(task_ids, list) or not task_ids:
        return {"content": "派发失败：task_ids 必须是非空数组", "error": "missing_task_ids"}

    thread_id = (args.get("thread_id") or "").strip()
    cfg = _load_dispatch_config()
    rep_cfg = cfg.get("reputation") or {}
    executors = cfg.get("executors") or []
    max_concurrent = int((cfg.get("concurrency") or {}).get("max_concurrent", 2))

    # 并发基线：当前 in_progress 任务数（保守口径）
    all_tasks = await orch.store.list_tasks(limit=500)
    running = sum(1 for t in all_tasks if t["status"] == "in_progress")

    results: list[dict] = []
    for tid in task_ids:
        task = await orch.store.get_task((tid or "").strip())
        if not task:
            results.append({"task_id": tid, "ok": False, "error": "task_not_found"})
            continue

        # 工具端二次校验 1：状态（只有 backlog 叶子可派发，v1.2 待办池语义）
        if task["status"] != "backlog":
            results.append({"task_id": tid, "ok": False, "error": "illegal_status",
                            "message": f"当前状态 {task['status']}，需 backlog（待办池）"})
            continue

        # 二次校验 2：依赖（blocks 上游全 closed）
        deps = await orch.store.list_blocked_by(task["task_id"])
        pending = [d for d in deps if d["status"] not in _TERMINAL_STATUSES]
        if pending:
            results.append({"task_id": tid, "ok": False, "error": "deps_not_ready",
                            "pending": [d.get("identifier") or d["task_id"] for d in pending]})
            continue

        # 二次校验 3：风险门禁（high 不自动派发）
        gate = resolve_review_gate(task.get("risk_level", "medium"))
        if gate == "manual":
            results.append({"task_id": tid, "ok": False, "error": "manual_review_required",
                            "message": "high 风险任务需人工确认派发"})
            continue

        # 二次校验 4：并发上限
        if running >= max_concurrent:
            results.append({"task_id": tid, "ok": False, "error": "concurrency_limit",
                            "message": f"并发已满（{running}/{max_concurrent}）"})
            continue

        # 执行者选择：ε-greedy + 声望加权
        executor = await _select_executor(orch, rep_cfg, executors)

        # 推进 backlog → in_progress（"调度 agent 执行"，requires_user=False）
        r = await orch.advance_stage(
            task_id=task["task_id"], target_status="in_progress",
            if_version=task["version"], actor="agent", thread_id=thread_id,
            comment=f"task_dispatcher 自动派发（executor={executor['agent_id']}"
                    f"{'，ε探索' if executor.get('explore') else '，声望加权'}）")
        if not r.get("ok"):
            results.append({"task_id": tid, "ok": False, "error": r.get("error"),
                            "message": r.get("message")})
            continue
        advanced = r["task"]

        # 派发 coding agent（真实 harness：codex/claude_code；无 harness 工厂时 mock）
        d = await orch.execute_coding(
            task["task_id"], if_version=advanced["version"],
            harness=executor["harness"])
        if not d.get("ok"):
            # 派发失败回滚语义：留在 in_progress 由 conductor 卡死监控处置（不反复重试）
            results.append({"task_id": tid, "ok": False,
                            "error": d.get("error"), "message": d.get("message"),
                            "executor": executor["agent_id"]})
            continue

        running += 1
        results.append({
            "task_id": task["task_id"],
            "identifier": advanced.get("identifier") or task["task_id"],
            "ok": True,
            "executor": executor["agent_id"],
            "executor_harness": executor["harness"],
            "explored": bool(executor.get("explore")),
            "run_id": d.get("run_id"),
            "mock": bool(d.get("mock")),
        })

        # medium 风险：派发知情通知（可召回，不等待确认——决策 #1）
        if task.get("risk_level") == "medium":
            await orch.store.add_comment(
                task_id=task["task_id"], author_type="agent",
                author_id="task_dispatcher", author_name="任务调度管家",
                comment_type="discussion",
                body=(f"📢 medium 风险任务已自动派发执行（executor={executor['agent_id']}，"
                      f"run={d.get('run_id')}）。如需召回请暂停任务（转 paused）。"),
                mentions=["user"])

    # v1.2 父任务收尾：backlog 态父任务 + 子任务全终态 → backlog→validating（一步直进验收）
    finalize_results: list[dict] = []
    for pid in parent_finalizes:
        parent = await orch.store.get_task((pid or "").strip())
        if not parent or parent["status"] != "backlog":
            continue
        children = [t for t in all_tasks
                    if t.get("parent_task_id") == parent["task_id"]]
        if not children or any(c["status"] not in _TERMINAL_STATUSES for c in children):
            continue  # 二次校验：仍有活跃子任务
        r = await orch.advance_stage(
            task_id=parent["task_id"], target_status="validating",
            if_version=parent["version"], actor="agent", thread_id=thread_id,
            comment="task_dispatcher 子任务全部完成，父任务自动收尾进验收")
        finalize_results.append({
            "task_id": parent["task_id"], "ok": bool(r.get("ok")),
            "error": r.get("error"),
            "status": r.get("task", {}).get("status", parent["status"])})

    ok_count = sum(1 for x in results if x.get("ok"))
    logger.info("task_dispatch_execute: %d/%d 派发成功，父任务收尾 %d",
                ok_count, len(results), len(finalize_results))
    return {
        "content": f"派发执行完成：{ok_count}/{len(results)} 成功"
                   f"（父任务收尾 {len(finalize_results)}）",
        "ok": ok_count == len(results),
        "dispatches": results,
        "finalizes": finalize_results,
        "executed": ok_count,
        "concurrency": {"running": running, "max": max_concurrent},
    }
