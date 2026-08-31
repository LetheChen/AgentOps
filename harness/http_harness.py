"""
HTTP Harness — calls an external HTTP service (v1 oa_audit / MCP server / ...).

This is the integration point for the v1 AI_Agent_Platform oa_audit service:
v2 DAG nodes can call into v1 sub-services via HTTP, letting us migrate
v1 business logic into v2 workflows without rewriting v1 code.

Also supports in-process fallback: when v1 server is not running, fall back
to direct in-process import via v1_oa_audit_adapter.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

import httpx

from .protocol import (
    AgentClient,
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    AgentUsage,
    HarnessType,
    ToolDefinition,
    assert_protocol_compatible,
)
from .v1_oa_audit_adapter import (
    is_v1_oa_audit_available,
    call_v1_handle_audit_task,
    run_v1_full_pipeline,
)

logger = logging.getLogger(__name__)


class HttpHarness(AgentClient):
    """Calls a single HTTP endpoint and translates the response into AgentEvents.

    Request shape:
      POST {base_url}/{agent_name}
      Body: {
        "system_prompt": "...",
        "prompt": "...",
        "inputs": { ... },
        "tools": [{"name": "...", "description": "...", "input_schema": {...}}]
      }

    Expected response:
      {
        "text": "...",
        "tool_calls": [{"name": "...", "arguments": {...}, "result": "..."}],
        "usage": {"input_tokens": N, "output_tokens": M},
        "error": null | "..."
      }

    v0.5: Auto-falls-back to in-process v1 import if HTTP server unreachable.
    Set AGENTOPS_V1_INPROCESS_ONLY=1 to force in-process.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8099/api/v1/agents",
        timeout: float = 60.0,
        api_key: str = "",
        allow_inprocess_fallback: bool = True,
        inprocess_only: bool | None = None,  # None = read from env at runtime
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self.allow_inprocess_fallback = allow_inprocess_fallback
        # If explicitly given, use it; else defer to env (read at call time)
        self._inprocess_only_override = inprocess_only

    @property
    def _inprocess_only(self) -> bool:
        if self._inprocess_only_override is not None:
            return self._inprocess_only_override
        return os.environ.get("AGENTOPS_V1_INPROCESS_ONLY", "") == "1"

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.HTTP

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        assert_protocol_compatible(HarnessType.HTTP, context.protocol or "custom")
        agent_name = (context.extra or {}).get("agent", "")

        # Try HTTP first (unless forced in-process)
        if not self._inprocess_only:
            try:
                async for ev in self._run_http(agent_name, prompt, tools, context):
                    yield ev
                return
            except Exception as e:
                if not self.allow_inprocess_fallback:
                    yield AgentEvent(type=AgentEventType.ERROR, error_message=f"HTTP error: {e}")
                    yield AgentEvent(type=AgentEventType.DONE)
                    return
                logger.warning(f"HTTP unreachable ({e}); falling back to in-process v1")

        # In-process fallback: call_v1_handle_audit_task auto-mocks on import failure,
        # so we don't need to check is_v1_oa_audit_available() first.
        async for ev in self._run_inprocess(agent_name, prompt, context):
            yield ev

    async def _run_http(
        self,
        agent_name: str,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        url = f"{self.base_url}/{agent_name}" if agent_name else self.base_url
        body = {
            "system_prompt": context.system_prompt,
            "prompt": prompt,
            "inputs": {},
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Yield events
        if text := data.get("text"):
            yield AgentEvent(type=AgentEventType.TEXT, text=text)
        for tc in data.get("tool_calls", []) or []:
            yield AgentEvent(type=AgentEventType.TOOL_USE, tool_name=tc.get("name"), tool_input=tc.get("arguments", {}))
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_name=tc.get("name"), tool_result=tc.get("result", ""))
        usage = data.get("usage", {}) or {}
        yield AgentEvent(type=AgentEventType.USAGE, usage=AgentUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        ))
        if err := data.get("error"):
            yield AgentEvent(type=AgentEventType.ERROR, error_message=err)
        yield AgentEvent(type=AgentEventType.DONE, usage=AgentUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        ))

    async def _run_inprocess(
        self,
        agent_name: str,
        prompt: str,
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """Call v1's handle_audit_task directly."""
        # Extract identifiers from inputs (try multiple sources)
        inputs = (context.extra or {}).get("inputs", {}) or {}
        summary_id = (
            inputs.get("summary_id")
            or (context.extra or {}).get("summary_id")
            or "TEST_S001"
        )
        form_app_id = (
            inputs.get("form_app_id")
            or (context.extra or {}).get("form_app_id")
            or "TEST_F01"
        )
        node_token = (
            inputs.get("node_token")
            or (context.extra or {}).get("node_token")
            or "TEST_T001"
        )

        yield AgentEvent(type=AgentEventType.TEXT, text=f"[in-process v1] {agent_name} -> {summary_id}")

        try:
            # Use sync helper wrapped in thread
            result = await asyncio.to_thread(
                call_v1_handle_audit_task,
                summary_id=summary_id,
                form_app_id=form_app_id,
                node_token=node_token,
                agent=agent_name or "travel_agent",
            )
        except Exception as e:
            yield AgentEvent(type=AgentEventType.ERROR, error_message=f"v1 in-process error: {e}")
            yield AgentEvent(type=AgentEventType.DONE)
            return

        # Yield result as text + handoff
        text = json.dumps(result, ensure_ascii=False, default=str)[:2000]
        yield AgentEvent(type=AgentEventType.TEXT, text=text)
        # Synthetic handoff via tool
        yield AgentEvent(
            type=AgentEventType.TOOL_USE,
            tool_name="handoff",
            tool_input={"port": "v1_result", "content": result},
        )
        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            tool_name="handoff",
            tool_result="ok",
        )
        yield AgentEvent(type=AgentEventType.USAGE, usage=AgentUsage(input_tokens=50, output_tokens=100))
        yield AgentEvent(type=AgentEventType.DONE, usage=AgentUsage(input_tokens=50, output_tokens=100))


import asyncio  # used in _run_inprocess

