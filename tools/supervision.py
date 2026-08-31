"""Supervision 工具集：让 Manager Agent 能跨 Run 监督和指挥子任务。

Manager Agent 工具设计，适配 AgentOps 的 DAG 引擎。

工具列表：
- get_run_supervision: 查看某个子 Run 的执行状态和节点进度
- list_session_runs: 列出当前 Session 关联的所有子 Run
- send_actor_command: 给指定 Actor 发送命令（用于 Multi-Round DAG 或 Live 干预）
- intervene_actor: 对 Actor 执行干预操作（retry / cancel / interrupt）
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def get_run_supervision(args: dict[str, Any]) -> dict[str, Any]:
    """查看某个子 Run 的执行状态和节点进度。

    args:
        run_id: 子 Run 的 ID（required）
    返回:
        Run 状态 + 各节点状态 + 关键里程碑
    """
    from orchestrator._registry import get_event_store

    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        return {"content": "缺少 run_id", "error": "missing_run_id"}

    event_store = get_event_store()
    if event_store is None:
        return {"content": "event_store 未初始化", "error": "store_unavailable"}

    # v3: run 的状态与 final_outputs 在 runs 层（get_run_summary），
    # 不是 sessions 层（get_session_summary 不含 workflow_id/final_outputs）
    summary = await event_store.get_run_summary(run_id)
    if not summary or summary.get("error"):
        return {"content": f"run {run_id} 不存在", "error": "run_not_found"}

    # 获取节点事件（v3: 方法名 get_run_events）
    events = await event_store.get_run_events(run_id)
    node_states: dict[str, dict[str, Any]] = {}
    milestones: list[str] = []
    for ev in events:
        if not ev.node_id:
            continue
        etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
        if etype == "node.started":
            node_states[ev.node_id] = {"status": "running"}
        elif etype == "node.completed":
            node_states[ev.node_id] = {
                "status": "completed",
                "summary": (ev.payload or {}).get("summary", "")[:200],
            }
            milestones.append(f"✓ {ev.node_id} 完成")
        elif etype == "node.failed":
            node_states[ev.node_id] = {"status": "failed"}
            milestones.append(f"✗ {ev.node_id} 失败")
        elif etype == "node.skipped":
            node_states[ev.node_id] = {"status": "skipped"}

    # 构造 supervision 摘要
    node_lines = [
        f"  {nid}: {ns['status']}" + (f" - {ns.get('summary', '')}" if ns.get("summary") else "")
        for nid, ns in node_states.items()
    ]

    content = (
        f"Run {run_id[:12]} 状态: {summary.get('status', 'unknown')}\n"
        f"工作流: {summary.get('workflow_id', 'unknown')}\n"
        f"节点进度:\n" + "\n".join(node_lines)
    )
    if milestones:
        content += f"\n里程碑:\n" + "\n".join(milestones)

    return {
        "content": content,
        "run_id": run_id,
        "status": summary.get("status"),
        "node_states": node_states,
        "milestones": milestones,
    }


async def list_session_runs(args: dict[str, Any]) -> dict[str, Any]:
    """列出当前 Session 关联的所有子 Run。

    args:
        session_id: Session ID（optional，默认用当前活跃 run_id）
    返回:
        Run 列表 + 每个 Run 的摘要
    """
    from orchestrator._registry import get_event_store, get_current_active_run_id

    session_id = (args.get("session_id") or "").strip()
    if not session_id:
        session_id = get_current_active_run_id() or ""
    if not session_id:
        return {"content": "缺少 session_id", "error": "missing_session_id"}

    event_store = get_event_store()
    if event_store is None:
        return {"content": "event_store 未初始化", "error": "store_unavailable"}

    # v3: list_child_runs_of_session 直接返回 runs JOIN parent_child_runs 的完整 run 字段
    # （含 workflow_id / status / agent_id），无需 N+1 调 get_session_summary
    children = await event_store.list_child_runs_of_session(session_id)

    runs = []
    for c in children[:20]:  # 限制最多 20 个，避免 N+1 爆炸
        child_id = c.get("run_id", "")
        if not child_id:
            continue
        runs.append({
            "run_id": child_id,  # 保留 run_id key 给 LLM（语义不变）
            "workflow_id": c.get("workflow_id", "?"),
            "status": c.get("status", "unknown"),
            "agent_id": c.get("agent_id"),
        })

    run_lines = []
    for r in runs:
        status_icon = {"completed": "✓", "running": "▶", "failed": "✗", "skipped": "⊘"}.get(
            r.get("status", ""), "?"
        )
        run_lines.append(
            f"  {status_icon} {r['run_id'][:12]} | {r.get('workflow_id', '?')} | {r.get('status', '?')}"
        )

    return {
        "content": f"Session {session_id[:12]} 关联的子任务（共 {len(runs)} 个）:\n" + "\n".join(run_lines),
        "runs": runs,
    }


async def collect_child_result(args: dict[str, Any]) -> dict[str, Any]:
    """查询子 Run 的最终输出结果。

    args:
        run_id: 子 Run 的 ID（required）
    返回:
        Run 的 final_outputs + 状态
    """
    from orchestrator._registry import get_event_store

    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        return {"content": "缺少 run_id", "error": "missing_run_id"}

    event_store = get_event_store()
    if event_store is None:
        return {"content": "event_store 未初始化", "error": "store_unavailable"}

    # v3: final_outputs 在 runs 层，用 get_run_summary（不是 get_session_summary）
    summary = await event_store.get_run_summary(run_id)
    if not summary or summary.get("error"):
        return {"content": f"run {run_id} 不存在", "error": "run_not_found"}

    status = summary.get("status", "unknown")
    final_outputs = summary.get("final_outputs")

    if status != "completed":
        return {
            "content": f"run {run_id[:12]} 状态为 {status}，尚未完成。可用 get_run_supervision 查看进度。",
            "run_id": run_id,
            "status": status,
        }

    if not final_outputs:
        return {
            "content": f"run {run_id[:12]} 已完成，但无 final_outputs 记录。",
            "run_id": run_id,
            "status": status,
        }

    return {
        "content": f"run {run_id[:12]} 已完成。输出:\n{final_outputs}",
        "run_id": run_id,
        "status": status,
        "final_outputs": final_outputs,
    }


# ⚠️ 死代码：不存在 config/tools/supervision.yaml，本 dict 当前无任何引用方。
# 真正的注册走 config/tools/*.yaml 的 handler 字段：
#   - get_run_supervision  → tools.supervision.get_run_supervision（本文件）
#   - list_session_runs    → tools.supervision.list_session_runs（本文件）
#   - collect_child_result → tools.collect_child_result.collect_child_result（独立文件！）
# 注意：下面 collect_child_result 条目是本文件的**重复副本**，不是 manager 实际调用的实现
# （2026-08-29 踩坑：改了这里没效果，真正在跑的是 tools/collect_child_result.py）。
# 修改 collect_child_result 行为前请先确认改的是哪个文件。
TOOL_DEFINITIONS = {
    "get_run_supervision": {
        "description": "查看某个子任务的执行状态和节点进度。Manager Agent 用来监督子 agent 执行情况。",
        "handler_module": "tools.supervision",
        "handler_function": "get_run_supervision",
    },
    "list_session_runs": {
        "description": "列出当前会话关联的所有子任务。Manager Agent 用来查看自己调度过的所有任务。",
        "handler_module": "tools.supervision",
        "handler_function": "list_session_runs",
    },
    "collect_child_result": {
        "description": "查询子任务的最终输出结果。子任务完成后用此工具获取结果。",
        "handler_module": "tools.supervision",
        "handler_function": "collect_child_result",
    },
}
