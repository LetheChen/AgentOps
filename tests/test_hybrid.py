"""P4 混合模式测试 — DAG 节点经 local_llm harness 执行。

验证：
- local_llm harness 节点能端到端跑通
- 节点事件（node.started / node.completed）正确写入 DAG 事件流
- 节点结束后 handoff payload 正确传递给下游节点
- 下游 deterministic 节点能接收 local_llm 节点的输出
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
import pytest_asyncio

from orchestrator import (
    DagEvent,
    DagEventType,
    LocalSdkOrchestrator,
    RunMode,
    RunRequest,
    RunStatus,
)


@pytest_asyncio.fixture
async def orchestrator():
    o = LocalSdkOrchestrator(llm_config={})
    wf_path = pathlib.Path(__file__).parent.parent / "workflows" / "hybrid-test.yaml"
    if not wf_path.exists():
        pytest.skip("hybrid-test.yaml not found")
    o.load_workflow_file(str(wf_path))
    yield o


@pytest.mark.asyncio
async def test_hybrid_mode_runs_to_completion(orchestrator):
    """混合模式 DAG 端到端跑通（含 local_llm 节点）。"""
    handle = await orchestrator.run(RunRequest(
        workflow_id="hybrid-test",
        run_mode=RunMode.TEMPLATED,
        inputs={"topic": "test hybrid"},
    ))

    for _ in range(100):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    state = await orchestrator.get_run(handle.run_id)
    assert state is not None
    assert state.status == RunStatus.COMPLETED, f"Expected COMPLETED, got {state.status}: {state.error}"


@pytest.mark.asyncio
async def test_hybrid_mode_local_llm_node_emits_node_events(orchestrator):
    """local_llm 节点（analyze）执行时产生 node.started / node.completed 事件，node_id 为节点 ID。"""
    handle = await orchestrator.run(RunRequest(
        workflow_id="hybrid-test",
        run_mode=RunMode.TEMPLATED,
        inputs={"topic": "widget test"},
    ))

    for _ in range(100):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    events = orchestrator._event_history.get(handle.run_id, [])
    dag_events = [e for e in events if isinstance(e, DagEvent)]

    # local_llm 节点（analyze）应产生 node.started / node.completed 事件
    node_events = [
        e for e in dag_events
        if e.type in (DagEventType.NODE_STARTED, DagEventType.NODE_COMPLETED)
    ]
    assert len(node_events) >= 1, "local_llm 节点应至少 emit 1 个 node.started/node.completed"

    # node_id 为 analyze（DAG 节点 ID）
    analyze_events = [e for e in node_events if e.node_id == "analyze"]
    assert len(analyze_events) >= 1, "应有 node_id=analyze 的 node 事件"


@pytest.mark.asyncio
async def test_hybrid_mode_handoff_propagates(orchestrator):
    """local_llm 节点的 handoff payload 传递给下游 report 节点。"""
    handle = await orchestrator.run(RunRequest(
        workflow_id="hybrid-test",
        run_mode=RunMode.TEMPLATED,
        inputs={"topic": "handoff test"},
    ))

    for _ in range(100):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    state = await orchestrator.get_run(handle.run_id)
    assert state is not None
    assert state.status == RunStatus.COMPLETED

    # report 节点应收到 analyze 的 handoff
    assert "report" in state.node_outputs
    # analyze 节点应有输出
    assert "analyze" in state.node_outputs


@pytest.mark.asyncio
async def test_hybrid_mode_all_three_nodes_complete(orchestrator):
    """三个节点（fetch/analyze/report）都应完成。"""
    handle = await orchestrator.run(RunRequest(
        workflow_id="hybrid-test",
        run_mode=RunMode.TEMPLATED,
        inputs={"topic": "three nodes"},
    ))

    for _ in range(100):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    state = await orchestrator.get_run(handle.run_id)
    assert state is not None
    assert state.status == RunStatus.COMPLETED
    # 三个节点都应完成
    assert state.node_states.get("fetch") == RunStatus.COMPLETED
    assert state.node_states.get("analyze") == RunStatus.COMPLETED
    assert state.node_states.get("report") == RunStatus.COMPLETED
