"""DagEngine cleanup tests."""
from __future__ import annotations

import pytest

from orchestrator import docker_runtime as docker_runtime_module
from workflow.engine import DagEngine, NodeExecutionState, _noop_sink
from workflow.schema import HarnessTypeRef, NodeType, OutputRoute, WorkflowDefinition, WorkflowNode


class DummyEventStore:
    def __init__(self) -> None:
        self.terminated: list[tuple[str, str]] = []

    async def terminate_subagent(self, subagent_id: str, cleanup_status: str = "released") -> None:
        self.terminated.append((subagent_id, cleanup_status))


@pytest.mark.asyncio
async def test_cleanup_provisioned_subagent_stops_and_removes_container(monkeypatch):
    wf = WorkflowDefinition(
        workflow_id="test_cleanup",
        name="Test Cleanup",
        nodes={
            "n1": WorkflowNode(
                id="n1",
                name="cleanup node",
                type=NodeType.AGENT,
                agent="echo_agent",
                harness=HarnessTypeRef.LOCAL_LLM,
                after=[],
                inputs=[],
                outputs={},
            ),
        },
    )
    store = DummyEventStore()
    engine = DagEngine(wf, event_sink=_noop_sink, llm_config={}, event_store=store)
    nstate = NodeExecutionState(node=wf.nodes["n1"])
    nstate.provisioned_subagent_id = "subagent-1"
    nstate.provisioned_container_id = "container-1"

    calls: list[tuple[str, str, object]] = []

    def fake_stop(container_id: str, timeout: int = 10) -> None:
        calls.append(("stop", container_id, timeout))

    def fake_remove(container_id: str, force: bool = False) -> None:
        calls.append(("remove", container_id, force))

    def fake_container_exists(container_id: str) -> bool:
        return False  # 容器已被清理

    monkeypatch.setattr(docker_runtime_module, "stop_container", fake_stop)
    monkeypatch.setattr(docker_runtime_module, "remove_container", fake_remove)
    monkeypatch.setattr(docker_runtime_module, "container_exists", fake_container_exists)

    await engine._cleanup_provisioned_subagent(nstate, cleanup_status="released")

    # stop + remove(force=True) 兜底强删
    assert calls == [("stop", "container-1", 10), ("remove", "container-1", True)]
    assert store.terminated == [("subagent-1", "released")]


@pytest.mark.asyncio
async def test_cleanup_provisioned_subagent_terminates_without_container(monkeypatch):
    wf = WorkflowDefinition(
        workflow_id="test_cleanup_no_container",
        name="Test Cleanup No Container",
        nodes={
            "n1": WorkflowNode(
                id="n1",
                name="cleanup node",
                type=NodeType.AGENT,
                agent="echo_agent",
                harness=HarnessTypeRef.LOCAL_LLM,
                after=[],
                inputs=[],
                outputs={},
            ),
        },
    )
    store = DummyEventStore()
    engine = DagEngine(wf, event_sink=_noop_sink, llm_config={}, event_store=store)
    nstate = NodeExecutionState(node=wf.nodes["n1"])
    nstate.provisioned_subagent_id = "subagent-2"
    nstate.provisioned_container_id = None

    def fake_stop(container_id: str, timeout: int = 10) -> None:
        raise AssertionError("stop_container should not be called")

    def fake_remove(container_id: str, force: bool = False) -> None:
        raise AssertionError("remove_container should not be called")

    monkeypatch.setattr(docker_runtime_module, "stop_container", fake_stop)
    monkeypatch.setattr(docker_runtime_module, "remove_container", fake_remove)

    await engine._cleanup_provisioned_subagent(nstate, cleanup_status="failed")

    assert store.terminated == [("subagent-2", "failed")]


@pytest.mark.asyncio
async def test_run_agent_node_fallback_cleans_up_previous_provisioned_subagent(monkeypatch):
    wf = WorkflowDefinition(
        workflow_id="test_fallback_cleanup",
        name="Test Fallback Cleanup",
        nodes={
            "n1": WorkflowNode(
                id="n1",
                name="fallback node",
                type=NodeType.AGENT,
                agent="echo_agent",
                harness=HarnessTypeRef.LOCAL_LLM,
                after=[],
                inputs=[],
                outputs={},
            ),
        },
    )
    store = DummyEventStore()
    engine = DagEngine(wf, event_sink=_noop_sink, llm_config={}, event_store=store)
    nstate = NodeExecutionState(node=wf.nodes["n1"])
    nstate.provisioned_subagent_id = "subagent-3"
    nstate.provisioned_worker_id = "container-3"
    nstate.provisioned_container_id = "container-3"

    cleanup_calls: list[tuple[str, str, str, str]] = []

    async def fake_cleanup(state: NodeExecutionState, cleanup_status: str = "failed") -> None:
        cleanup_calls.append((state.provisioned_subagent_id or "", state.provisioned_container_id or "", cleanup_status))

    monkeypatch.setattr(engine, "_cleanup_provisioned_subagent", fake_cleanup)
    monkeypatch.setattr(engine, "_try_resolve_fallback", lambda node, error_type, current_provider: {"provider": "fb", "model": "fb-model"} if error_type == "rate_limit" else None)
    monkeypatch.setattr(engine, "_resolve_model_for_node", lambda node: {"provider": "orig", "model": "orig-model"})

    execute_calls = {"count": 0}

    async def fake_execute_node(node, state, global_inputs, override_resolved_model=None):
        execute_calls["count"] += 1
        if execute_calls["count"] == 1:
            raise RuntimeError("429 rate limit exceeded")
        state.pending_handoffs["ok"] = {"content": "ok", "summary": "ok"}

    monkeypatch.setattr(engine, "_execute_node", fake_execute_node)
    engine.node_states["n1"] = nstate

    await engine._run_agent_node(wf.nodes["n1"], nstate, {})

    assert cleanup_calls == [("subagent-3", "container-3", "failed")]
    assert execute_calls["count"] == 2
    assert nstate.provisioned_subagent_id is None
    assert nstate.provisioned_worker_id is None
    assert nstate.provisioned_container_id is None
