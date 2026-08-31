"""conversational ↔ cross_domain 集成测试 — 验证 P0-2 Coordinator 注入点。

覆盖：
- coordinator=None 时：request_cross_domain 走默认反射装载（bare 函数），调用 fast-fail
- coordinator=非空时：request_cross_domain 走闭包注入，调用真实中转，emit 6 个跨域事件
- ConversationalEngine 启动时不传 coordinator 不报错（向后兼容）
"""
from __future__ import annotations

import asyncio
from typing import Any

from orchestrator.config_loader import get_system_config
from orchestrator.conversation_kit import (
    ConversationState,
    make_conversational_tools,
)
from orchestrator.session_engine import SessionEngine
from orchestrator.cross_domain import CrossDomainCoordinator
from orchestrator.protocol import (
    DagEvent,
    RunMode,
)
from harness import HarnessType


# ====== 测试辅助 ======

def _reset_config_and_make_deterministic():
    """重置全局配置 + 把所有 agent harness 改为 deterministic（测试用）。"""
    from orchestrator import config_loader as cl_module
    cl_module._system_config = None
    config = get_system_config()
    for agent in config.agents.values():
        agent.harness = "deterministic"
    return config


def _make_event_collector():
    """创建事件收集器（event_sink 回调 + events 列表）。"""
    events: list[Any] = []

    async def sink(ev: DagEvent):
        events.append(ev)

    return sink, events


def _make_dummy_state(agent_id: str = "smart_query") -> ConversationState:
    """构造一个最小可用的 ConversationState（不连 event_sink）。"""
    return ConversationState(
        run_id="run_test_xcd",
        agent_id=agent_id,
    )


# ====== 核心注入点测试 ======

class TestMakeConversationalToolsInjection:
    """make_conversational_tools 的 coordinator 注入测试。"""

    def setup_method(self):
        _reset_config_and_make_deterministic()

    def test_no_coordinator_keeps_default_reflection(self):
        """coordinator=None：request_cross_domain 走默认反射（bare 函数），handler 调它会 fast-fail。"""
        async def sink(ev):
            pass

        state = _make_dummy_state("smart_query")
        tools = make_conversational_tools(state, sink, agent_id="smart_query")
        names = {t.name for t in tools}
        assert "request_cross_domain" in names, (
            f"smart_query allowed_tools 含 request_cross_domain，期望出现在工具列表里，got {names}"
        )
        # 找到该工具，调用其 handler 验证默认行为（bare 函数 → fast-fail）
        xcd_tool = next(t for t in tools if t.name == "request_cross_domain")
        result = asyncio.run(xcd_tool.handler({
            "target_domain": "smart_ops",
            "request": "test",
        }))
        assert result["status"] == "failed"
        assert "未初始化" in result["content"]

    def test_with_coordinator_uses_closure(self):
        """coordinator=非空：request_cross_domain handler 是闭包，能真实中转。"""
        async def sink(ev):
            pass

        sink_impl, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink_impl)

        state = _make_dummy_state("smart_query")
        tools = make_conversational_tools(
            state, sink_impl,
            agent_id="smart_query",
            coordinator=coord,
            parent_run_id="run_test_xcd",
        )
        names = {t.name for t in tools}
        assert "request_cross_domain" in names
        xcd_tool = next(t for t in tools if t.name == "request_cross_domain")

        # 调用 handler：期望成功（coordinator 中转到 target_domain=smart_ops）
        result = asyncio.run(xcd_tool.handler({
            "target_domain": "smart_ops",
            "request": "检查服务器状态",
        }))
        assert result["status"] == "completed", (
            f"coordinator 注入后应能中转，got {result}"
        )
        assert result["target_agent"] == "smart_ops"

        # 验证 emit 了 cross_domain 系列事件
        xd_subtypes = [
            ev.payload.get("event_subtype")
            for ev in events
            if isinstance(ev, DagEvent) and ev.payload.get("event_subtype")
        ]
        expected = {
            "requested", "received", "dispatched",
            "target_started", "target_completed", "returned",
        }
        missing = expected - set(xd_subtypes)
        assert not missing, f"缺少跨域事件: {missing}, got {xd_subtypes}"

    def test_coordinator_none_keeps_legacy_behavior(self):
        """coordinator=None 时不能 emit 任何 cross_domain 事件（与历史行为一致）。"""
        async def sink(ev):
            pass

        sink_impl, events = _make_event_collector()
        state = _make_dummy_state("smart_query")
        tools = make_conversational_tools(state, sink_impl, agent_id="smart_query")
        xcd_tool = next(t for t in tools if t.name == "request_cross_domain")

        asyncio.run(xcd_tool.handler({
            "target_domain": "smart_ops",
            "request": "test",
        }))

        # coordinator=None 时不应该 emit 任何 cross_domain 事件
        xd_events = [
            ev for ev in events
            if isinstance(ev, DagEvent) and ev.type.value == "cross_domain"
        ]
        assert len(xd_events) == 0, (
            f"coordinator=None 不应 emit cross_domain 事件，got {len(xd_events)} 条"
        )


# ====== SessionEngine 注入测试 ======

class TestSessionEngineInit:
    """SessionEngine.__init__ 接受 cross_domain_coordinator 参数（v2 迁移后替代 ConversationalEngine）。"""

    def setup_method(self):
        _reset_config_and_make_deterministic()

    def test_init_without_coordinator_still_works(self):
        """不传 coordinator 也能正常初始化（向后兼容）。"""
        async def sink(ev):
            pass

        engine = SessionEngine(
            session_id="run_test_no_coord",
            agent_id="smart_query",
            llm_config={},
            event_sink=sink,
            harness_type=HarnessType.DETERMINISTIC,
            system_prompt="test",
        )
        assert engine.coordinator is None

    def test_init_with_coordinator(self):
        """传入 coordinator 后保存到 self.coordinator。"""
        async def sink(ev):
            pass

        sink_impl, _ = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink_impl)

        engine = SessionEngine(
            session_id="run_test_with_coord",
            agent_id="smart_query",
            llm_config={},
            event_sink=sink,
            harness_type=HarnessType.DETERMINISTIC,
            system_prompt="test",
            cross_domain_coordinator=coord,
        )
        assert engine.coordinator is coord