"""trigger_workflow 工具：让 Manager Agent 直接触发 templated/hybrid workflow。

背景：Manager Agent 的 system_prompt 之前写死「用 bash 工具调 curl 触发」，
但 codex harness 在某些会话/沙盒场景下未挂载 bash 工具，LLM 拿不到可执行能力，
只能 fallback 让用户手动 curl。本工具把「触发 workflow」从 bash+curl 路径
收回到统一的 in-process orchestrator.run() 路径，零 shell 依赖。

设计：
- handler 只依赖 orchestrator._registry，不 import 具体 orchestrator 类（防循环依赖）
- 走 orchestrator.run(RunRequest) 而非 httpx，与后端 /api/agent/run 端点等价
- 返回 run_id 给 LLM，LLM 可接着用 emit_widget 把 run_id 推给前端组件面板
- run 完成时 LLM 可再次调此工具查 status（workflow_id='' + run_id 透传）

注意：本项目暂时不做沙箱/docker，trigger_workflow 是 in-process 直调，
等价于 manager 自己手按 /api/agent/run 按钮。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# 透传到 event_sink 的桥接：异步读 _event_streams[run_id] 队列并 await queue.put(ev)
# 这里只做最简：handler 创建 run 后立即返回 run_id，事件流由 _event_bridge 异步转发


async def trigger_workflow(args: dict[str, Any]) -> dict[str, Any]:
    """触发一个 templated/hybrid workflow，返回 { run_id, status } 给 LLM。

    args:
        workflow_id (str, required): workflow 标识，如 "log-patrol"
        inputs (dict, optional): workflow 输入参数，如 {"log_source_id": "oa_approval", "time_range": "7d"}
        run_mode (str, optional): "templated" (默认) | "hybrid" | "conversational" | "task"
        agent_id (str, optional): conversational/task 模式必填
        initial_message (str, optional): conversational 模式必填
    """
    from orchestrator._registry import get_event_bridge, get_event_store, get_orchestrator
    from orchestrator.protocol import DagEvent, DagEventType, RunMode, RunRequest

    orch = get_orchestrator()
    if orch is None:
        return {
            "content": "触发失败：orchestrator 未初始化（后端服务未启动？）",
            "error": "orchestrator_unavailable",
        }

    workflow_id = (args.get("workflow_id") or "").strip()
    if not workflow_id:
        return {"content": "触发失败：缺少 workflow_id", "error": "missing_workflow_id"}

    run_mode_str = (args.get("run_mode") or "templated").lower()
    try:
        run_mode = RunMode(run_mode_str)
    except ValueError:
        return {
            "content": f"触发失败：run_mode '{run_mode_str}' 不合法（必须是 templated/hybrid/conversational/task）",
            "error": "invalid_run_mode",
        }

    # conversational/task 模式：要求 agent_id
    if run_mode in (RunMode.CONVERSATIONAL, RunMode.TASK) and not args.get("agent_id"):
        return {
            "content": f"触发失败：{run_mode_str} 模式需要 agent_id",
            "error": "missing_agent_id",
        }

    # templated/hybrid 模式：要求 workflow 存在
    if run_mode in (RunMode.TEMPLATED, RunMode.HYBRID):
        if workflow_id not in getattr(orch, "workflows", {}):
            return {
                "content": (
                    f"触发失败：workflow '{workflow_id}' 不存在。"
                    f"可用：{', '.join(list(getattr(orch, 'workflows', {}).keys())[:10])}"
                ),
                "error": "workflow_not_found",
                "available_workflows": list(getattr(orch, "workflows", {}).keys()),
            }

    inputs = args.get("inputs") or {}
    if isinstance(inputs, str):
        try:
            inputs = json.loads(inputs)
        except json.JSONDecodeError:
            return {"content": "触发失败：inputs JSON 解析失败", "error": "invalid_inputs_json"}

    # 🆕 Phase 1: parent_run_id 提前解析，作为 RunRequest.session_id，
    # 让 orchestrator.run() 在启动 engine 之前先写 runs 表（避免 provision_subagent 的 FK 竞态）。
    parent_run_id = (args.get("parent_run_id") or "").strip() or None
    if not parent_run_id:
        from orchestrator._registry import get_current_active_run_id
        parent_run_id = get_current_active_run_id()

    req = RunRequest(
        workflow_id=workflow_id,
        inputs=inputs,
        run_mode=run_mode,
        agent_id=args.get("agent_id"),
        initial_message=args.get("initial_message"),
        session_id=parent_run_id,
    )

    try:
        handle = await orch.run(req)
    except Exception as e:
        logger.exception("trigger_workflow failed: %s", e)
        return {"content": f"触发失败：{e}", "error": "orchestrator_run_failed"}

    # 落库 + 桥接事件流到 SSE 队列（与 api/server.py:start_run 内的 _bridge 同构）
    event_store = get_event_store()

    if event_store:
        try:
            # v3: session_id 来自 parent_run_id（manager session 复用），run_id 是新生成的 run_
            # ensure session 存在（首次创建时）
            await event_store.create_session(
                session_id=parent_run_id,
                agent_id=args.get("agent_id") or "manager",
                user_id="",
                title=f"session for {parent_run_id}",
            )
            # v3: init_run 写入 runs 表（替代 v2 init_session）
            await event_store.init_run(
                run_id=handle.run_id,
                session_id=parent_run_id,
                workflow_id=workflow_id if run_mode in (RunMode.TEMPLATED, RunMode.HYBRID) else None,
                run_mode=run_mode_str,
                agent_id=args.get("agent_id"),
                initial_message=args.get("initial_message"),
                inputs=inputs if run_mode in (RunMode.TEMPLATED, RunMode.HYBRID)
                else {"initial_message": args.get("initial_message")},
            )
        except Exception as e:
            logger.warning("init_run 失败: %s", e)

    bridge = get_event_bridge()
    if bridge is not None:
        try:
            # 异步桥接，不阻塞 handler 返回
            asyncio.create_task(bridge(handle.run_id))
        except Exception as e:
            logger.warning("event bridge 启动失败: %s", e)
    else:
        logger.warning("event_bridge 未注册，run %s 的事件不会推送到前端 SSE", handle.run_id)

    # ── 父子 run 映射：让 manager 可以通过 collect_child_result 反查子任务结果 ──
    # v3: parent_child_runs 表（替代 v2 parent_child_sessions）
    if parent_run_id and event_store is not None and parent_run_id != handle.run_id:
        try:
            await event_store.record_parent_child_run(
                parent_run_id=parent_run_id,         # 实际是 manager session_id（v2: parent_run_id 同 session）
                child_run_id=handle.run_id,
                parent_session_id=parent_run_id,
                child_session_id=parent_run_id,      # 子 run 默认挂到同一 session（manager 的）
                created_via="trigger_workflow",
            )
        except Exception as e:
            logger.warning("record_parent_child_run 失败: %s", e)

    # ── 🆕 Phase 1: 注册到 SessionManager ──
    from orchestrator._registry import get_session_manager
    session_mgr = get_session_manager()
    if session_mgr and parent_run_id:
        try:
            await session_mgr.attach_run(
                session_id=parent_run_id,
                run_id=handle.run_id,
                workflow_id=workflow_id,
            )
        except Exception as e:
            logger.warning("session attach_run 失败: %s", e)

    # ── 🆕 Phase 3: 注册 Run 完成后的摘要回灌回调 ──
    from orchestrator._registry import get_memory_manager
    mem_mgr = get_memory_manager()
    if mem_mgr and parent_run_id:
        asyncio.create_task(_summarize_run_on_completion(
            run_id=handle.run_id,
            session_id=parent_run_id,
            workflow_id=workflow_id,
            memory_manager=mem_mgr,
        ))

    return {
        "content": (
            f"workflow '{workflow_id}' 已启动。run_id={handle.run_id}。"
            f"可用 get_run_supervision 查看进度，用 collect_child_result 获取结果。"
        ),
        "run_id": handle.run_id,
        "workflow_id": workflow_id,
        "status": "started",
        "_parent_run_id": parent_run_id,  # 供 collect_child_result 等链下游使用
    }


async def _summarize_run_on_completion(
    run_id: str,
    session_id: str,
    workflow_id: str,
    memory_manager: Any,
) -> None:
    """轮询 Run 状态，完成后生成摘要回灌到 Session 记忆。

    不阻塞 trigger_workflow handler 返回，异步执行。
    """
    from orchestrator._registry import get_event_store

    event_store = get_event_store()
    if event_store is None:
        return

    # 简单轮询：每 5 秒查一次，最多等 10 分钟
    max_wait = 600
    waited = 0
    while waited < max_wait:
        try:
            # v3: run 状态在 runs 层（get_run_summary），不是 sessions 层
            summary = await event_store.get_run_summary(run_id)
        except Exception:
            summary = None
        if summary and summary.get("status") in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(5)
        waited += 5

    if not summary:
        logger.warning("摘要回灌超时: run %s 10 分钟内未完成", run_id[:12])
        return

    # 生成摘要
    try:
        events = await event_store.get_run_events(run_id)
        event_dicts = [
            {
                "type": ev.type.value if hasattr(ev.type, "value") else str(ev.type),
                "node_id": ev.node_id,
                "payload": ev.payload,
            }
            for ev in events
        ]
        await memory_manager.summarize_run(
            session_id=session_id,
            run_id=run_id,
            workflow_id=workflow_id,
            run_events=event_dicts,
        )
    except Exception as e:
        logger.warning("摘要回灌失败: %s", e)


# trigger_workflow 返回结果中携带的字段，供 harness 识别（如果需要）


async def get_workflow_status(args: dict[str, Any]) -> dict[str, Any]:
    """查询 run 当前状态。args: { run_id (required) }"""
    from orchestrator._registry import get_orchestrator

    orch = get_orchestrator()
    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        return {"content": "查询失败：缺少 run_id", "error": "missing_run_id"}
    if orch is None:
        return {"content": "查询失败：orchestrator 未初始化", "error": "orchestrator_unavailable"}

    state = await orch.get_run(run_id)
    if state is None:
        return {
            "content": f"run '{run_id}' 不存在或已结束（in-memory 状态丢失，可查 audit.db 历史）",
            "error": "run_not_found",
        }

    return {
        "content": (
            f"run {run_id} 状态={state.status.value}, "
            f"tokens={state.total_tokens_input + state.total_tokens_output}"
        ),
        "run_id": run_id,
        "status": state.status.value,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "finished_at": state.finished_at.isoformat() if state.finished_at else None,
        "total_tokens_input": state.total_tokens_input,
        "total_tokens_output": state.total_tokens_output,
        "error": state.error,
    }
