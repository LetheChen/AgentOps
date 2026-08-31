"""
v0 smoke tests — validate the foundations work.

Run with: PYTHONIOENCODING=utf-8 python -m pytest tests/test_v0.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from harness import (
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    HarnessRegistry,
    HarnessType,
)
from orchestrator import (
    DagEvent,
    DagEventType,
    LocalSdkOrchestrator,
    NodeStatus,
    RunRequest,
    RunStatus,
)
from workflow import (
    HarnessTypeRef,
    NodeType,
    load_workflow_text,
    load_workflow_yaml,
    topological_order,
    validate_workflow,
    WorkflowValidationError,
)


# ====== Loader tests ======

def test_loader_hello_world_yaml():
    """Hello world DAG loads + validates."""
    wf = load_workflow_yaml(PROJECT_ROOT / "workflows" / "hello-world.yaml")
    assert wf.workflow_id == "hello-world"
    assert len(wf.nodes) == 3
    validate_workflow(wf)  # should not raise


def test_loader_v1_steps_yaml():
    """v1 9-step translation loads + validates (includes parallel_branch + gateway)."""
    wf = load_workflow_yaml(PROJECT_ROOT / "workflows" / "travel-expense.yaml")
    assert wf.workflow_id == "travel-expense"
    assert len(wf.nodes) == 12

    # 包含 parallel_branch 和 gateway 节点
    node_types = {n.type for n in wf.nodes.values()}
    assert NodeType.PARALLEL_BRANCH in node_types
    assert NodeType.GATEWAY in node_types

    # gateway 必须有 condition
    for nid, node in wf.nodes.items():
        if node.type == NodeType.GATEWAY:
            assert node.condition is not None, f"Gateway {nid} missing condition"
            assert node.gateway_kind is not None

    validate_workflow(wf)


def test_loader_invalid_yaml_raises():
    from workflow.loader import WorkflowLoadError
    bad = """
workflow_id: bad
nodes: {}
"""
    with pytest.raises(WorkflowLoadError) as exc_info:
        load_workflow_text(bad)
    assert "no nodes" in str(exc_info.value)


# ====== Validator tests ======

def test_validator_catches_unknown_dependency():
    wf_yaml = """
workflow_id: t
nodes:
  a:
    type: agent
    agent: x
    after: [nonexistent]
    outputs: {}
"""
    wf = load_workflow_text(wf_yaml)
    with pytest.raises(WorkflowValidationError) as exc_info:
        validate_workflow(wf)
    assert any("nonexistent" in e for e in exc_info.value.errors)


def test_validator_catches_cycle():
    wf_yaml = """
workflow_id: cycle
nodes:
  a:
    type: agent
    agent: x
    after: [b]
    outputs: {o: {to: "b"}}
  b:
    type: agent
    agent: y
    after: [a]
    outputs: {o: {to: "a"}}
"""
    wf = load_workflow_text(wf_yaml)
    with pytest.raises(WorkflowValidationError) as exc_info:
        validate_workflow(wf)
    assert any("cycle" in e.lower() for e in exc_info.value.errors)


def test_topological_order_simple():
    wf_yaml = """
workflow_id: order
nodes:
  c:
    type: agent
    agent: c
    after: [a, b]
    outputs: {}
  a:
    type: agent
    agent: a
    after: []
    outputs: {o: {to: "c"}}
  b:
    type: agent
    agent: b
    after: []
    outputs: {o: {to: "c"}}
"""
    wf = load_workflow_text(wf_yaml)
    order = topological_order(wf)
    assert order[-1] == "c"
    assert set(order[:2]) == {"a", "b"}


# ====== Harness tests ======

@pytest.mark.asyncio
async def test_deterministic_harness_emits_done():
    """Deterministic harness should yield DONE event with usage."""
    from harness.deterministic import DeterministicClient

    client = DeterministicClient()
    events: list[AgentEvent] = []
    async for ev in client.run(
        prompt="hello",
        tools=[],
        context=AgentRunContext(
            system_prompt="test",
            model="",
            api_key="",
            base_url="",
            workspace="/tmp",
            session_id="test",
        ),
    ):
        events.append(ev)

    # Must have at least one DONE
    done_events = [e for e in events if e.type == AgentEventType.DONE]
    assert len(done_events) == 1

    # Must have USAGE
    usage_events = [e for e in events if e.type == AgentEventType.USAGE]
    assert len(usage_events) >= 1

    # Must have TEXT
    text_events = [e for e in events if e.type == AgentEventType.TEXT]
    assert len(text_events) == 1


def test_harness_registry_has_deterministic():
    assert HarnessType.DETERMINISTIC in HarnessRegistry.available()


# ====== End-to-end orchestrator tests ======

@pytest.mark.asyncio
async def test_orchestrator_runs_hello_world_to_completion():
    """End-to-end: load + run + assert COMPLETED."""
    orch = LocalSdkOrchestrator()
    orch.load_workflow_file(str(PROJECT_ROOT / "workflows" / "hello-world.yaml"))

    handle = await orch.run(RunRequest(
        workflow_id="hello-world",
        inputs={"topic": "test"},
    ))

    # Wait for completion
    final = None
    async for event in orch.stream_events(handle.run_id):
        if event.type == DagEventType.RUN_COMPLETED:
            final = "COMPLETED"
        elif event.type == DagEventType.RUN_FAILED:
            final = "FAILED"
            pytest.fail(f"Run failed: {event.payload}")

    assert final == "COMPLETED"

    run_state = await orch.get_run(handle.run_id)
    assert run_state.status == RunStatus.COMPLETED
    assert run_state.total_tokens_input > 0
    assert run_state.total_tokens_output > 0

    # 3 nodes should be completed
    assert len(run_state.node_states) == 3
    for status in run_state.node_states.values():
        assert status == NodeStatus.COMPLETED


@pytest.mark.asyncio
async def test_orchestrator_runs_v1_steps_with_parallel_and_gateway():
    """v1 9 步: 12 节点 + parallel_branch + gateway 全部正确执行."""
    orch = LocalSdkOrchestrator()
    orch.load_workflow_file(str(PROJECT_ROOT / "workflows" / "travel-expense.yaml"))

    handle = await orch.run(RunRequest(
        workflow_id="travel-expense",
        inputs={"summary_id": "S001", "form_app_id": "F01", "node_token": "T001"},
    ))

    final = None
    async for event in orch.stream_events(handle.run_id):
        if event.type == DagEventType.RUN_COMPLETED:
            final = "COMPLETED"
        elif event.type == DagEventType.RUN_FAILED:
            final = "FAILED"
            pytest.fail(f"Run failed: {event.payload}")

    assert final == "COMPLETED"

    run_state = await orch.get_run(handle.run_id)
    assert run_state.status == RunStatus.COMPLETED
    # 12 nodes including virtual ones
    assert len(run_state.node_states) == 12


if __name__ == "__main__":
    # Allow running without pytest: python tests/test_v0.py
    pytest.main([__file__, "-v"])
