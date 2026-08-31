"""
OpencodeOrchestrator — talks to a running opencode headless server (default :4096).

Architecture (v2.1 §3.1 + opencode API surface):
  - opencode exposes a **session-based** API (no DAG concept natively).
  - We map our DAG into a single opencode session:
      * system prompt: DAG 节点说明
      * tools: handoff / send_message / graph_context (DAG 工具)
      * user prompt: "Run DAG workflow_id=xxx, inputs=yyy, follow handoff protocol"
  - opencode's Manager Agent 自由调度 (subagent tree) 跑节点
  - We collect opencode 原生 events + parse handoff 调用 → 拼成我们的 DagEvent 流

Status: run loop implemented (session-based). DAG 节点识别靠 handoff 工具调用的 port 名.

需要:
  - opencode headless 服务在跑 (default :4096)
  - 一个 workflow 已 sync 进 opencode DB
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
import yaml

from orchestrator import (
    DagEvent,
    DagEventType,
    NodeStatus,
    Orchestrator,
    RawHarnessEvent,
    RunHandle,
    RunRequest,
    RunState,
    RunStatus,
)
from workflow import (
    WorkflowDefinition,
    load_workflow_yaml,
    validate_workflow,
    WorkflowNode,
    NodeType,
)

logger = logging.getLogger(__name__)


# opencode session-based endpoints (from opencode/packages/opencode/src/server/routes/...)
SESSION_LIST = "/api/agent/sessions"
SESSION_BY_ID = "/api/agent/sessions/{session_id}"
SESSION_PROMPT = "/api/agent/sessions/{session_id}/prompt"
SESSION_PROMPT_ASYNC = "/api/agent/sessions/{session_id}/prompt_async"
SESSION_MESSAGES = "/api/agent/sessions/{session_id}/message"
EVENTS_LISTEN = "/api/agent/events"
DAG_SYNC = "/api/dag/workflows/sync"
DAG_LIST = "/api/dag/workflows"
RUNS_CREATE = "/api/runs"


class OpencodeOrchestrator(Orchestrator):
    """Orchestrator that delegates run lifecycle to a running opencode server.

    Differs from LocalSdkOrchestrator:
      - run loop is HTTP-based (not in-process)
      - DAG 节点识别: parses opencode tool_use calls (handoff tool → node events)
      - 事件: opencode's /event SSE stream → our DagEvent

    Caveats (v0.5):
      - Requires opencode server running on :4096
      - If opencode 不可用, run() raises RuntimeError immediately
      - DAG 节点顺序依赖 opencode Manager Agent 正确调度 (不保证 determinism)
    """

    def __init__(self, base_url: str = "http://127.0.0.1:4096", timeout: float = 240.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.workflows: dict[str, WorkflowDefinition] = {}
        self._runs: dict[str, RunState] = {}
        self._events: dict[str, list[DagEvent | RawHarnessEvent]] = {}
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self):
        await self._client.aclose()

    def register_workflow(self, workflow: WorkflowDefinition) -> None:
        validate_workflow(workflow)
        self.workflows[workflow.workflow_id] = workflow

    def load_workflow_file(self, path: str) -> WorkflowDefinition:
        wf = load_workflow_yaml(path)
        self.register_workflow(wf)
        return wf

    async def _health(self) -> None:
        """Probe opencode server. Raises if unreachable."""
        try:
            r = await self._client.get(self.base_url + "/", timeout=3.0)
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"opencode unreachable at {self.base_url}: {e}")

    async def _sync_workflow(self, workflow: WorkflowDefinition) -> None:
        """Push workflow YAML to opencode's DAG DB via /api/dag/workflows/sync."""
        yaml_text = yaml.safe_dump(workflow.__dict__ if hasattr(workflow, '__dict__') else {}, default_flow_style=False)
        # Re-serialize using the workflow's loader
        from workflow.loader import _parse_workflow
        raw = _parse_workflow(_workflow_to_raw(workflow), source_path="<inline>")
        yaml_text = yaml.safe_dump(raw, allow_unicode=True, default_flow_style=False)

        r = await self._client.post(
            self.base_url + DAG_SYNC,
            json={"yaml_text": yaml_text, "source_path": "<inline>"},
        )
        r.raise_for_status()
        logger.info(f"Synced workflow {workflow.workflow_id} to opencode")

    async def run(self, req: RunRequest) -> RunHandle:
        await self._health()

        # Resolve workflow
        wf = self.workflows.get(req.workflow_id)
        if wf is None:
            raise ValueError(f"Workflow not found: {req.workflow_id}")

        # Sync workflow to opencode
        await self._sync_workflow(wf)

        # Create run record
        run_id = f"oc_run_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        run_state = RunState(
            run_id=run_id,
            workflow_id=req.workflow_id,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._runs[run_id] = run_state
        self._events[run_id] = []

        # Build the master prompt for opencode Manager Agent
        prompt = self._build_master_prompt(wf, req.inputs)

        # Create session in opencode
        r = await self._client.post(self.base_url + SESSION_LIST, json={})
        r.raise_for_status()
        session = r.json()
        session_id = session.get("id") or session.get("session_id")
        if not session_id:
            raise RuntimeError(f"opencode session create failed: {r.text[:200]}")

        # Dispatch to opencode
        await self._dispatch_master_prompt(wf, session_id, prompt, req, run_id)

        return RunHandle(
            run_id=run_id,
            workflow_id=req.workflow_id,
            started_at=run_state.started_at,
            cancel_token=run_id,
        )

    def _build_master_prompt(self, wf: WorkflowDefinition, inputs: dict) -> str:
        lines = [
            f"# Run DAG: {wf.workflow_id}",
            f"Name: {wf.name}",
            f"Description: {wf.description}",
            "",
            f"## Inputs",
            "```json",
            json.dumps(inputs, ensure_ascii=False, indent=2),
            "```",
            "",
            f"## DAG Nodes ({len(wf.nodes)} 个)",
            "按以下顺序执行 (parallel_branch 的分支可并行):",
            "",
        ]
        for nid, node in wf.nodes.items():
            lines.append(f"- **{nid}** ({node.type.value}): {node.name}")
            if node.agent:
                lines.append(f"  - agent: {node.agent}, harness: {node.harness.value}")
            for port, route in (node.outputs or {}).items():
                target, _ = route.parse()
                if target:
                    lines.append(f"  - handoff {port} -> {target}")
            lines.append("")
        lines += [
            "## 工具说明",
            "- `handoff(port, content, summary)`: 把本节点的产出送到下游, **每轮只能调用一次**",
            "- `graph_context()`: 查上游节点产出",
            "- `finish()`: 所有节点完成后调用",
            "",
            "## 执行规则",
            "1. 按 `after` 关系确定执行顺序 (上游全部 completed 才能开始)",
            "2. 准备好本节点的 inputs 后, 用 handoff 把结果送出",
            "3. parallel_branch 节点的 branches 可以并行, 等所有分支都 handoff 后聚合",
            "4. 所有节点完成后, 调 finish()",
        ]
        return "\n".join(lines)

    async def _dispatch_master_prompt(
        self, wf: WorkflowDefinition, session_id: str, prompt: str,
        req: RunRequest, run_id: str,
    ) -> None:
        """Send master prompt to opencode, start background SSE listener."""
        # Register tools on session
        # (v0.5: rely on opencode's default tools, handoff is just text-based)

        # Send prompt (async — we don't block on completion)
        r = await self._client.post(
            self.base_url + SESSION_PROMPT_ASYNC.format(session_id=session_id),
            json={
                "parts": [{"type": "text", "text": prompt}],
                "model": {"providerID": "minimax", "modelID": "MiniMax-M3"},
            },
        )
        r.raise_for_status()
        # Don't read response body; we listen to events instead

        # Start event listener in background
        asyncio.create_task(self._event_loop(wf, session_id, run_id))

    async def _event_loop(self, wf: WorkflowDefinition, session_id: str, run_id: str) -> None:
        """Subscribe to opencode events, convert to our DagEvent.

        Strategy:
          - opencode sends {type: "message", sessionID, message: {parts: [...]}} events
          - Look for tool_use parts where name == "handoff" or "graph_context"
          - Map port name to node_id, emit node.started / node.completed
        """
        url = self.base_url + EVENTS_LISTEN
        # v0.5: use polling for simplicity (SSE requires proper reconnect logic)
        last_seq = 0
        node_started_emitted: set[str] = set()

        try:
            while True:
                # Poll for events (opencode doesn't expose a clean event history yet)
                # Use session messages endpoint as proxy
                try:
                    r = await self._client.get(
                        self.base_url + SESSION_MESSAGES.format(session_id=session_id),
                        timeout=10.0,
                    )
                    if r.status_code != 200:
                        await asyncio.sleep(2.0)
                        continue
                    msgs = r.json() or []
                except Exception as e:
                    logger.warning(f"event poll error: {e}")
                    await asyncio.sleep(2.0)
                    continue

                # Process new messages
                if len(msgs) > last_seq:
                    new_msgs = msgs[last_seq:]
                    for msg in new_msgs:
                        await self._process_opencode_message(
                            wf, run_id, session_id, msg, node_started_emitted
                        )
                    last_seq = len(msgs)

                # Check if session is still running
                # (v0.5: assume done if we see stop_reason or after N seconds of no new msgs)
                await asyncio.sleep(1.0)
                # Heuristic: if no new messages for 30 seconds AND we emitted all expected nodes, mark done
                # (v0.5: simpler — check if any node is still in running state)
                run_state = self._runs[run_id]
                if run_state.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                    break

                # Mark done if all expected nodes completed
                if all(nid in run_state.node_outputs for nid in wf.nodes):
                    if run_state.status == RunStatus.RUNNING:
                        run_state.status = RunStatus.COMPLETED
                        run_state.finished_at = datetime.now(timezone.utc)
                        await self._emit_to(run_id, DagEvent(
                            type=DagEventType.RUN_COMPLETED,
                            run_id=run_id,
                            node_id=None,
                            payload={"duration_ms": int((run_state.finished_at - run_state.started_at).total_seconds() * 1000)},
                        ))
                    break
        except asyncio.CancelledError:
            pass

    async def _process_opencode_message(
        self, wf: WorkflowDefinition, run_id: str, session_id: str,
        msg: dict, node_started_emitted: set,
    ) -> None:
        """Translate one opencode message into DagEvent(s)."""
        msg_id = msg.get("id") or msg.get("messageID")
        parts = msg.get("parts", []) or []

        for part in parts:
            ptype = part.get("type")
            if ptype == "tool_use":
                tool_name = part.get("name")
                tool_input = part.get("input", {})
                tool_id = part.get("id")

                if tool_name == "handoff":
                    port = tool_input.get("port", "")
                    content = tool_input.get("content", "")

                    # Try to identify which node this corresponds to
                    # Heuristic: handoff port name matches a node's outputs port
                    target_node_id = self._find_node_by_port(wf, port)
                    if target_node_id and target_node_id not in node_started_emitted:
                        node_started_emitted.add(target_node_id)
                        await self._emit_to(run_id, DagEvent(
                            type=DagEventType.NODE_STARTED,
                            run_id=run_id,
                            node_id=target_node_id,
                            payload={"agent": wf.nodes[target_node_id].agent},
                        ))

                    if target_node_id:
                        # Update node_outputs and mark completed
                        run_state = self._runs[run_id]
                        run_state.node_outputs.setdefault(target_node_id, {})[port] = content
                        run_state.node_states[target_node_id] = NodeStatus.COMPLETED
                        await self._emit_to(run_id, DagEvent(
                            type=DagEventType.NODE_COMPLETED,
                            run_id=run_id,
                            node_id=target_node_id,
                            payload={"port": port, "content_size": len(str(content))},
                        ))

                elif tool_name == "graph_context":
                    # Don't emit a special event; just log
                    pass
                else:
                    # Generic tool call
                    await self._emit_to(run_id, RawHarnessEvent(
                        harness="opencode",
                        event_type="tool_use",
                        raw_payload={"tool": tool_name, "input": tool_input, "id": tool_id},
                        run_id=run_id,
                    ))

            elif ptype == "tool_result":
                content = part.get("content", "")
                tool_id = part.get("id")
                await self._emit_to(run_id, RawHarnessEvent(
                    harness="opencode",
                    event_type="tool_result",
                    raw_payload={"tool_use_id": tool_id, "content": content},
                    run_id=run_id,
                ))

            elif ptype == "text":
                text = part.get("text", "")
                if text:
                    await self._emit_to(run_id, RawHarnessEvent(
                        harness="opencode",
                        event_type="text",
                        raw_payload={"text": text[:500]},
                        run_id=run_id,
                    ))

    def _find_node_by_port(self, wf: WorkflowDefinition, port: str) -> str | None:
        """Find a node that has this port in its outputs."""
        for nid, node in wf.nodes.items():
            if port in (node.outputs or {}):
                return nid
        return None

    async def _emit_to(self, run_id: str, event: DagEvent | RawHarnessEvent) -> None:
        self._events.setdefault(run_id, []).append(event)
        # Notify via callback if anyone listening (FastAPI BFF uses _event_streams)
        try:
            from api.server import _event_streams
            queue = _event_streams.get(run_id)
            if queue is not None:
                await queue.put(event)
        except ImportError:
            pass

    async def stream_events(
        self, run_id: str, since: int = 0
    ) -> AsyncIterator[DagEvent | RawHarnessEvent]:
        history = self._events.get(run_id, [])
        for ev in history:
            if isinstance(ev, DagEvent) and ev.sequence > since:
                yield ev
        # Wait for completion
        while True:
            run_state = self._runs.get(run_id)
            if run_state is None or run_state.status in (
                RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
            ):
                break
            await asyncio.sleep(0.5)

    async def inject(self, run_id: str, node_id: str, instruction: str) -> None:
        # v0.5: not implemented for opencode orchestrator
        pass

    async def abort(self, run_id: str, reason: str = "") -> None:
        run_state = self._runs.get(run_id)
        if run_state:
            run_state.status = RunStatus.CANCELLED
            run_state.finished_at = datetime.now(timezone.utc)

    async def get_run(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    async def resume_node(self, run_id: str, node_id: str, instruction: str) -> None:
        pass


def _workflow_to_raw(wf: WorkflowDefinition) -> dict:
    """Serialize WorkflowDefinition back to raw dict for re-parsing."""
    from workflow.schema import (
        NodeType as NT, GatewayKind as GK, HarnessTypeRef as HT
    )
    nodes = {}
    for nid, node in wf.nodes.items():
        nodes[nid] = {
            "type": node.type.value,
            "name": node.name,
            "agent": node.agent,
            "harness": node.harness.value,
            "after": node.after,
            "inputs": node.inputs,
            "branches": node.branches,
            "join_strategy": node.join_strategy,
            "cancel_on_first_fail": node.cancel_on_first_fail,
            "gateway_kind": node.gateway_kind.value if node.gateway_kind else None,
            "condition": node.condition,
            "outputs": {p: r.to if hasattr(r, "to") else r for p, r in (node.outputs or {}).items()},
            "config": node.config,
        }
    widgets = [
        {
            "id": w.id, "type": w.type, "title": w.title,
            "emit_on": {"node": w.emit_on_node, "event": w.emit_on_event},
            "props": w.props,
        }
        for w in wf.widgets
    ]
    widget_inputs = [
        {
            "from_widget": wi.from_widget, "to_node": wi.to_node, "to_input": wi.to_input,
            "required": wi.required, "abortable": wi.abortable, "timeout_seconds": wi.timeout_seconds,
        }
        for wi in wf.widget_inputs
    ]
    return {
        "workflow_id": wf.workflow_id,
        "name": wf.name,
        "version": wf.version,
        "description": wf.description,
        "source_policy": wf.source_policy,
        "inputs": wf.inputs,
        "permissions": wf.permissions,
        "nodes": nodes,
        "widgets": widgets,
        "widget_inputs": widget_inputs,
        "schema_version": wf.schema_version,
    }
