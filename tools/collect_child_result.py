"""collect_child_result 工具：Manager Agent 派发子任务后，阻塞收集子 run 结果。

常规业务流程：manager 通过 trigger_workflow 启动一个子 agent（templated/hybrid/conversational/task），
本工具 poll 该 child_run_id 的 RunState 直到终态（completed/failed/cancelled/dormant），
返回给 manager：
  - status: 子 run 终态
  - final_outputs.messages: conversational 模式下完整消息列表（user/assistant 交替）
  - final_outputs.summary: 运行总结
  - finished_at / duration_ms
  - error: 若失败/取消的错误信息

阻塞策略：每 2 秒 poll 一次，直到 status 是终态或超时（默认 600s）。
超时返回当前 status + 已收集的部分结果，让 manager 决定下一步（推进 / 取消 / 等）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 子 run 终态：这些状态出现后 collect_child_result 立即返回结果
_TERMINAL_STATUSES = {
    "completed", "failed", "cancelled",
    # conversational 模式 idle 5 分钟自动转 dormant，也算"等用户回来"的终态
    "active", "dormant",
}


async def collect_child_result(args: dict[str, Any]) -> dict[str, Any]:
    """阻塞等待子 run 完成（达到终态），返回 final_outputs/messages/summary 供 manager 整合。

    Args:
        run_id (str, required): 触发 trigger_workflow 返回的 child run_id
        timeout_seconds (int, optional): 最长阻塞秒数，默认 600（10 分钟）
        poll_interval_seconds (float, optional): poll 间隔秒数，默认 2.0

    Returns:
        dict: {
            child_run_id, status (final), final_outputs, messages (conversational),
            summary, started_at, finished_at, duration_ms, error, elapsed_seconds,
            timed_out (bool, 是否超时返回)
        }
    """
    from orchestrator._registry import get_event_store, get_orchestrator
    from orchestrator.protocol import RunStatus

    child_run_id = (args.get("run_id") or "").strip()
    if not child_run_id:
        return {"content": "collect_child_result 失败：缺少 run_id", "error": "missing_run_id"}

    timeout_seconds = int(args.get("timeout_seconds") or 600)
    poll_interval = float(args.get("poll_interval_seconds") or 2.0)

    orch = get_orchestrator()
    if orch is None:
        return {"content": "collect_child_result 失败：orchestrator 未初始化", "error": "orch_unavailable"}

    deadline = time.monotonic() + timeout_seconds
    final: dict[str, Any] = {}
    timed_out = False

    try:
        while time.monotonic() < deadline:
            state = await orch.get_run(child_run_id)
            if state is None:
                # 内存状态丢失（run 终结后从内存清掉）：查 audit.db 兜底
                store = get_event_store()
                if store is not None:
                    final = await _load_from_audit(child_run_id, store)
                    if final:
                        return _shape_response(child_run_id, final, elapsed=time.monotonic() - (deadline - timeout_seconds), timed_out=False)
                await asyncio.sleep(poll_interval)
                continue

            status_value = state.status.value if hasattr(state.status, "value") else str(state.status)
            if status_value in _TERMINAL_STATUSES:
                # 收集 final_outputs + 计算 duration
                messages = []
                node_outputs = state.node_outputs or {}
                # conversational run 的 messages 在 node id = "conv:<agent_id>" 的 outputs[messages] 字段
                for node_id, outputs in node_outputs.items():
                    if isinstance(outputs, dict) and isinstance(outputs.get("messages"), list):
                        messages = outputs["messages"]
                        break

                duration_ms = 0
                if state.started_at and state.finished_at:
                    duration_ms = int((state.finished_at - state.started_at).total_seconds() * 1000)

                final = {
                    "status": status_value,
                    "final_outputs": _extract_final_outputs(state.node_outputs),
                    "messages": messages,
                    "summary": _extract_summary(state.node_outputs),
                    "started_at": state.started_at.isoformat() if state.started_at else None,
                    "finished_at": state.finished_at.isoformat() if state.finished_at else None,
                    "duration_ms": duration_ms,
                    "error": state.error,
                }
                return _shape_response(child_run_id, final, elapsed=time.monotonic() - (deadline - timeout_seconds), timed_out=False)

            await asyncio.sleep(poll_interval)
        else:
            # 超时
            timed_out = True
            state = await orch.get_run(child_run_id)
            status_value = state.status.value if state and hasattr(state.status, "value") else "unknown"
            final = {
                "status": status_value,
                "final_outputs": _extract_final_outputs(state.node_outputs) if state else {},
                "messages": [],
                "summary": "",
                "started_at": state.started_at.isoformat() if state and state.started_at else None,
                "finished_at": state.finished_at.isoformat() if state and state.finished_at else None,
                "duration_ms": 0,
                "error": f"collect_child_result timed out after {timeout_seconds}s (still {status_value})",
            }
            return _shape_response(child_run_id, final, elapsed=timeout_seconds, timed_out=True)

    except Exception as e:
        logger.exception("collect_child_result unexpected error: %s", e)
        return {"content": f"collect_child_result 异常: {e}", "error": "unexpected_error"}


def _extract_final_outputs(node_outputs: dict[str, Any]) -> dict[str, Any]:
    """从 node_outputs 提取最终交付（兼容两种结构）。

    1. conversational / task 模式：某节点（如 ``conv:<agent_id>``）的 outputs 里
       直接带 ``final_outputs`` 键 → 原样返回。
    2. templated workflow（如 smart-query）：node_outputs 是
       ``{node_id: {port_name: payload}}``，按端口组织（如
       ``present_result.answer``），**不存在 final_outputs 键**
       （此前只处理情况 1，导致 templated 一律返回 {}，manager 永远读空）。
       此时整个 node_outputs 本身就是最终交付，直接返回。
    """
    if not isinstance(node_outputs, dict) or not node_outputs:
        return {}

    # 1. 优先显式 final_outputs（conversational / task）
    for outputs in node_outputs.values():
        if isinstance(outputs, dict) and isinstance(outputs.get("final_outputs"), dict):
            return outputs["final_outputs"]

    # 2. templated workflow：各节点的端口 payload 即最终交付
    return node_outputs


def _extract_summary(node_outputs: dict[str, Any]) -> str:
    """从 node_outputs 提取摘要（兼容两种结构）。

    1. 节点 outputs 顶层的 ``summary`` / ``final_summary``（conversational）
    2. templated：向下钻一层，取端口 payload 里的 ``summary``
       （如 ``present_result.answer.summary``）
    """
    if not isinstance(node_outputs, dict):
        return ""

    for outputs in node_outputs.values():
        if isinstance(outputs, dict):
            if isinstance(outputs.get("summary"), str):
                return outputs["summary"]
            if isinstance(outputs.get("final_summary"), str):
                return outputs["final_summary"]

    # templated：向下钻一层找端口 payload 里的 summary。
    # 反向遍历：templated DAG 的终点节点（如 present_result）才是最终结论，
    # 正序会先命中 route_intent 等上游节点，把"意图描述"当成最终摘要。
    for outputs in reversed(list(node_outputs.values())):
        if isinstance(outputs, dict):
            for payload in outputs.values():
                if isinstance(payload, dict) and isinstance(payload.get("summary"), str):
                    return payload["summary"]
    return ""


async def _load_from_audit(run_id: str, store: Any) -> dict[str, Any] | None:
    """子 run 已不在内存中（终结被清）→ audit.db 兜底，从 runs 表读 status/error/timestamps。

    v3 修复：v2 这函数查 `FROM runs` 但 sessions 表不存在 runs（v2 统一 sessions），
    导致永远返回 None。v3 runs 表独立，调用 public get_run_summary 接口即可。
    """
    try:
        summary = await store.get_run_summary(run_id)
        if not summary or summary.get("error"):
            return None
        return {
            "status": summary.get("status"),
            "final_outputs": summary.get("final_outputs") or {},
            "messages": [],
            "summary": summary.get("final_outputs", {}).get("summary", "") if isinstance(summary.get("final_outputs"), dict) else "",
            "started_at": summary.get("started_at"),
            "finished_at": summary.get("finished_at"),
            "duration_ms": 0,
            "error": summary.get("error"),
        }
    except Exception as e:
        logger.warning("_load_from_audit failed for %s: %s", run_id, e)
        return None


def _shape_response(run_id: str, result: dict[str, Any], elapsed: float, timed_out: bool) -> dict[str, Any]:
    """构造 LLM 易读的结果。返回 content 是 markdown 风格的文本摘要 + 数据字段。"""
    messages = result.get("messages") or []
    # 只保留 user/assistant 关键摘要，避免 LLM 上下文爆炸
    msg_preview = []
    for m in messages[-20:]:  # 最近 20 条
        if isinstance(m, dict):
            role = m.get("role", "?")
            content = m.get("content", "")
            if not content:
                continue
            text = str(content)[:1500]  # 单条截断
            msg_preview.append({"role": role, "content": text})
    content = (
        f"child run {run_id} status={result.get('status')}, "
        f"messages={len(messages)}, "
        f"elapsed={elapsed:.1f}s, "
        f"timed_out={timed_out}."
    )
    if result.get("error"):
        content += f" error={result['error']}"
    return {
        "content": content,
        "child_run_id": run_id,
        "status": result.get("status"),
        "final_outputs": result.get("final_outputs") or {},
        "messages": msg_preview,  # 截断 + 仅最近 20 条
        "summary": result.get("summary") or "",
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "duration_ms": result.get("duration_ms") or 0,
        "error": result.get("error"),
        "elapsed_seconds": round(elapsed, 1),
        "timed_out": timed_out,
    }
