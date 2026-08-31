"""Conversational 模式测试 — 验证 SessionEngine 驱动的对话 run。

覆盖 P1 验收标准（v2 迁移后）：
- 不选 workflow 可发起对话 run
- 对话 run 产生 v2 事件（turn.started / turn.progress / turn.completed）
- 流式文本走 turn.progress（不再 emit widget.update memo）
- task 模式已废弃，明确拒绝
- templated 模式不受影响（回归）
"""
from __future__ import annotations

import asyncio

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
    """无 workflow 的 orchestrator（纯对话模式）。"""
    o = LocalSdkOrchestrator(llm_config={})
    yield o


@pytest.mark.asyncio
async def test_conversational_run_basic(orchestrator):
    """conversational 模式能端到端跑通。"""
    handle = await orchestrator.run(RunRequest(
        run_mode=RunMode.CONVERSATIONAL,
        agent_id="echo_agent",
        initial_message="hello world",
    ))
    for _ in range(50):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    state = await orchestrator.get_run(handle.run_id)
    assert state is not None
    assert state.status == RunStatus.COMPLETED
    assert state.workflow_id == "conv:echo_agent"


@pytest.mark.asyncio
async def test_conversational_emits_events(orchestrator):
    """对话 run 产生 v2 事件（turn.started / turn.progress / turn.completed）。"""
    handle = await orchestrator.run(RunRequest(
        run_mode=RunMode.CONVERSATIONAL,
        agent_id="echo_agent",
        initial_message="test message",
    ))
    for _ in range(50):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    events = orchestrator._event_history.get(handle.run_id, [])
    event_types = [e.type for e in events if isinstance(e, DagEvent)]
    assert DagEventType.TURN_STARTED in event_types
    assert DagEventType.TURN_PROGRESS in event_types
    assert DagEventType.TURN_COMPLETED in event_types


@pytest.mark.asyncio
async def test_conversational_emits_widgets(orchestrator):
    """v2：conversational 流式文本走 turn.progress（不再 emit widget.update memo）。"""
    handle = await orchestrator.run(RunRequest(
        run_mode=RunMode.CONVERSATIONAL,
        agent_id="echo_agent",
        initial_message="show me a widget",
    ))
    for _ in range(50):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    events = orchestrator._event_history.get(handle.run_id, [])
    turn_progress_events = [
        e for e in events
        if isinstance(e, DagEvent) and e.type == DagEventType.TURN_PROGRESS
    ]
    assert len(turn_progress_events) >= 1
    # v2：不再 emit widget.update type=memo
    memo_events = [
        e for e in events
        if isinstance(e, DagEvent)
        and e.type == DagEventType.WIDGET_UPDATE
        and e.payload.get("type") == "memo"
    ]
    assert len(memo_events) == 0


@pytest.mark.asyncio
async def test_conversational_requires_agent_id(orchestrator):
    """conversational 模式缺 agent_id 抛异常。"""
    with pytest.raises(ValueError, match="agent_id"):
        await orchestrator.run(RunRequest(
            run_mode=RunMode.CONVERSATIONAL,
            initial_message="no agent",
        ))


@pytest.mark.asyncio
async def test_task_mode_rejected(orchestrator):
    """task 模式已废弃，明确拒绝。"""
    with pytest.raises(ValueError, match="task 模式已废弃"):
        await orchestrator.run(RunRequest(
            run_mode=RunMode.TASK,
            agent_id="echo_agent",
            initial_message="do 3 things",
        ))


@pytest.mark.asyncio
async def test_templated_mode_still_works(orchestrator):
    """templated 模式不受影响（回归测试）。"""
    from workflow import load_workflow_yaml
    import pathlib
    wf_path = pathlib.Path(__file__).parent.parent / "workflows" / "hello-world.yaml"
    if not wf_path.exists():
        pytest.skip("hello-world.yaml not found")
    orchestrator.load_workflow_file(str(wf_path))

    handle = await orchestrator.run(RunRequest(
        workflow_id="hello-world",
        run_mode=RunMode.TEMPLATED,
    ))
    for _ in range(100):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    state = await orchestrator.get_run(handle.run_id)
    assert state is not None
    assert state.status == RunStatus.COMPLETED
