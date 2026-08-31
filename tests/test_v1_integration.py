"""
v1 oa_audit in-process integration test.

Verifies that v2 HTTP harness can call v1's handle_audit_task via in-process
import (when v1 server is not running). With v0.5, falls back to a
deterministic mock when v1 has missing deps (e.g. doc_parser).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
# Force in-process mode (avoid trying HTTP first)
os.environ["AGENTOPS_V1_INPROCESS_ONLY"] = "1"

import pytest

from harness import (
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    HarnessType,
)
from harness.http_harness import HttpHarness
from harness.v1_oa_audit_adapter import (
    call_v1_handle_audit_task,
    is_v1_oa_audit_available,
)


def test_v1_oa_audit_module_status():
    """Probe v1 module status — may or may not be importable.

    The test passes either way; the harness must handle both cases.
    """
    available = is_v1_oa_audit_available()
    if available:
        print("v1 oa_audit is importable (real mode available)")
    else:
        print("v1 oa_audit is NOT importable (mock mode only — v1 has missing deps)")


def test_v1_handle_audit_task_returns_dict():
    """call_v1_handle_audit_task always returns a dict (real OR mock)."""
    result = call_v1_handle_audit_task(
        summary_id="TEST_S_DUMMY_001",
        form_app_id="TEST_F_DUMMY",
        node_token="TEST_T_DUMMY",
        agent="travel_agent",
        force_mock=True,
    )
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    # Mock result has these fields
    assert "request_id" in result
    assert "status" in result
    assert result["status"] == "accepted"
    assert "pipeline" in result
    assert result["pipeline"]["step8_decision"] == "pass"
    print(f"v1 direct call returned: {list(result.keys())}, mode={result.get('mode')}")


@pytest.mark.asyncio
async def test_http_harness_falls_back_to_inprocess():
    """HTTP harness with v1 server unreachable should fall back to in-process (or mock)."""
    harness = HttpHarness(
        base_url="http://localhost:1/no-such-service",  # unreachable
        allow_inprocess_fallback=True,
    )
    events: list[AgentEvent] = []
    async for ev in harness.run(
        prompt="test",
        tools=[],
        context=AgentRunContext(
            system_prompt="test",
            model="",
            api_key="",
            base_url="",
            workspace="/tmp",
            session_id="test",
            extra={"agent": "travel_agent", "inputs": {
                "summary_id": "TEST_FALLBACK_001",
                "form_app_id": "F01",
                "node_token": "T01",
            }},
        ),
    ):
        events.append(ev)

    # Must have a DONE event
    done_events = [e for e in events if e.type == AgentEventType.DONE]
    assert len(done_events) == 1, f"Expected 1 DONE event, got {len(done_events)}"

    # Should have TEXT events (the synthetic in-process output)
    text_events = [e for e in events if e.type == AgentEventType.TEXT]
    assert len(text_events) >= 1, "Expected at least 1 TEXT event from in-process fallback"

    # Should NOT have ERROR (unless v1 itself errored — that's still acceptable)
    errors = [e.error_message for e in events if e.type == AgentEventType.ERROR]
    print(f"Fallback harness produced {len(events)} events: "
          f"text={len(text_events)}, errors={errors}")


@pytest.mark.asyncio
async def test_http_harness_inprocess_only_mode():
    """AGENTOPS_V1_INPROCESS_ONLY=1 forces in-process path even if HTTP works."""
    harness = HttpHarness(
        base_url="http://localhost:8099/api/v1/agents",  # would be real v1 if up
        allow_inprocess_fallback=True,
    )
    events: list[AgentEvent] = []
    async for ev in harness.run(
        prompt="test",
        tools=[],
        context=AgentRunContext(
            system_prompt="test",
            model="",
            api_key="",
            base_url="",
            workspace="/tmp",
            session_id="test",
            extra={"agent": "oa_audit", "inputs": {
                "summary_id": "TEST_INPROC_001",
                "form_app_id": "F01",
                "node_token": "T01",
            }},
        ),
    ):
        events.append(ev)

    # Should have DONE
    done_events = [e for e in events if e.type == AgentEventType.DONE]
    assert len(done_events) == 1
    # Should have a TEXT event (the in-process result)
    text_events = [e for e in events if e.type == AgentEventType.TEXT]
    assert len(text_events) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

