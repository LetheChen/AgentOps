"""SessionEngine 测试 — 验证 v2 Thread 模式对话引擎。

覆盖：
- start_turn 返回 TurnResult（G3），调用方能 await 拿 summary/tokens
- 事件命名符合 v2 约定（turn.started / turn.progress / turn.completed）
"""
from __future__ import annotations

import pytest

from harness import HarnessType
from orchestrator.protocol import DagEvent, DagEventType
from orchestrator.session_engine import SessionEngine, TurnResult


@pytest.mark.asyncio
async def test_start_turn_returns_turn_result():
    """G3：start_turn 返回 TurnResult，P4 hybrid 等调用方能 await 拿产物。"""
    events: list[DagEvent] = []

    async def sink(ev: DagEvent):
        events.append(ev)

    engine = SessionEngine(
        session_id="sess_test_g3",
        agent_id="echo_agent",
        llm_config={},
        event_sink=sink,
        harness_type=HarnessType.DETERMINISTIC,
        system_prompt="test",
    )

    result = await engine.start_turn("hello")

    assert result is not None
    assert isinstance(result, TurnResult)
    assert result.status == "completed"
    # deterministic harness 调 finalize({"summary": "deterministic finalize"})
    assert "deterministic finalize" in result.summary
    # 有 assistant 流式文本
    assert result.assistant_text


@pytest.mark.asyncio
async def test_start_turn_emits_v2_events():
    """start_turn 产生 turn.started / turn.progress / turn.completed 事件。"""
    events: list[DagEvent] = []

    async def sink(ev: DagEvent):
        events.append(ev)

    engine = SessionEngine(
        session_id="sess_test_events",
        agent_id="echo_agent",
        llm_config={},
        event_sink=sink,
        harness_type=HarnessType.DETERMINISTIC,
        system_prompt="test",
    )

    await engine.start_turn("hello")

    types = [e.type for e in events]
    assert DagEventType.TURN_STARTED in types
    assert DagEventType.TURN_PROGRESS in types
    assert DagEventType.TURN_COMPLETED in types
    # v2 约定：不应再 emit widget.update type=memo
    memo_events = [
        e for e in events
        if e.type == DagEventType.WIDGET_UPDATE and e.payload.get("type") == "memo"
    ]
    assert len(memo_events) == 0
