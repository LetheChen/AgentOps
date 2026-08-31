"""P7: 跨域协调协议测试。

验证:
  - CrossDomainCoordinator 正常跨域请求中转
  - 权限拒绝（无 request_cross_domain 权限 / Agent 不存在）
  - 自调用拒绝 / manager 作为 target 拒绝
  - 目标域不存在
  - 审计事件流（6 个子事件 + denied/failed）
  - make_tool_handler 闭包注入 caller_agent
  - request_cross_domain_tool 工具入口
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.config_loader import get_system_config
from orchestrator.cross_domain import (
    CrossDomainCoordinator,
    CrossDomainRequest,
    CrossDomainResult,
    request_cross_domain_tool,
)
from orchestrator.protocol import DagEvent, DagEventType


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

    async def sink(ev):
        events.append(ev)

    return sink, events


def _filter_cross_domain_events(events: list[Any]) -> list[Any]:
    """过滤出跨域事件。"""
    return [e for e in events if e.type == DagEventType.CROSS_DOMAIN]


# ====== CrossDomainCoordinator 测试 ======

class TestCrossDomainCoordinator:
    """CrossDomainCoordinator 跨域中转测试。"""

    def setup_method(self):
        _reset_config_and_make_deterministic()

    @pytest.mark.asyncio
    async def test_normal_cross_domain_request(self):
        """正常跨域请求：smart_query → smart_ops。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_ops",
            request_message="检查数据库连接状态",
        )

        assert result.status == "completed"
        assert result.target_agent == "smart_ops"
        assert result.request_id.startswith("xd_")
        assert result.result_text != ""

    @pytest.mark.asyncio
    async def test_cross_domain_to_smart_analysis(self):
        """跨域请求到智能分析域。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_analysis",
            request_message="分析本月销售趋势",
        )

        assert result.status == "completed"
        assert result.target_agent == "smart_analysis"

    @pytest.mark.asyncio
    async def test_permission_denied_agent_not_exist(self):
        """权限拒绝：不存在的 Agent。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await coord.handle_request(
            caller_agent="nonexistent_agent",
            target_domain="smart_ops",
            request_message="test",
        )

        assert result.status == "denied"
        assert "不存在" in result.error or "未授权" in result.error

        # 应该 emit denied 事件
        xd_events = _filter_cross_domain_events(events)
        assert len(xd_events) == 1
        assert xd_events[0].payload["event_subtype"] == "denied"

    @pytest.mark.asyncio
    async def test_permission_denied_no_tool_access(self):
        """权限拒绝：Agent 存在但没有 request_cross_domain 工具权限。"""
        config = get_system_config()
        # 临时移除 smart_query 的 request_cross_domain 权限
        agent = config.agents["smart_query"]
        original_allowed = agent.allowed_tools.copy()
        agent.allowed_tools = [t for t in agent.allowed_tools if t != "request_cross_domain"]

        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        try:
            result = await coord.handle_request(
                caller_agent="smart_query",
                target_domain="smart_ops",
                request_message="test",
            )
            assert result.status == "denied"
            assert "request_cross_domain" in result.error
        finally:
            agent.allowed_tools = original_allowed

    @pytest.mark.asyncio
    async def test_self_call_denied(self):
        """自调用拒绝：caller 域 == target 域。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_query",
            request_message="test",
        )

        assert result.status == "denied"
        assert "自调用" in result.error

        xd_events = _filter_cross_domain_events(events)
        assert len(xd_events) == 1
        assert xd_events[0].payload["event_subtype"] == "denied"

    @pytest.mark.asyncio
    async def test_manager_as_target_denied(self):
        """manager 域不能作为跨域目标。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await coord.handle_request(
            caller_agent="smart_query",
            target_domain="manager",
            request_message="test",
        )

        assert result.status == "denied"
        assert "manager" in result.error

    @pytest.mark.asyncio
    async def test_target_domain_not_exist(self):
        """目标域不存在 → failed。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await coord.handle_request(
            caller_agent="smart_query",
            target_domain="nonexistent_domain",
            request_message="test",
        )

        assert result.status == "failed"
        assert "无可用 Agent" in result.error

        # 审计事件：requested + received + failed
        xd_events = _filter_cross_domain_events(events)
        subtypes = [e.payload["event_subtype"] for e in xd_events]
        assert "requested" in subtypes
        assert "received" in subtypes
        assert "failed" in subtypes

    @pytest.mark.asyncio
    async def test_audit_event_flow_normal(self):
        """正常跨域请求的完整审计事件流（6 个子事件）。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_ops",
            request_message="检查服务器状态",
        )

        xd_events = _filter_cross_domain_events(events)
        subtypes = [e.payload["event_subtype"] for e in xd_events]

        # 完整事件流
        assert "requested" in subtypes
        assert "received" in subtypes
        assert "dispatched" in subtypes
        assert "target_started" in subtypes
        assert "target_completed" in subtypes
        assert "returned" in subtypes

        # 事件顺序
        expected_order = [
            "requested", "received", "dispatched",
            "target_started", "target_completed", "returned",
        ]
        actual_order = [s for s in subtypes if s in expected_order]
        assert actual_order == expected_order, f"事件顺序错误: {actual_order}"

    @pytest.mark.asyncio
    async def test_audit_event_payload(self):
        """审计事件 payload 包含完整跨域元数据。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_ops",
            request_message="检查DB状态",
            priority="high",
        )

        xd_events = _filter_cross_domain_events(events)
        # 检查第一个事件（requested）的 payload
        requested_ev = next(
            e for e in xd_events
            if e.payload["event_subtype"] == "requested"
        )
        p = requested_ev.payload
        assert p["caller_agent"] == "smart_query"
        assert p["caller_domain"] == "smart_query"
        assert p["target_domain"] == "smart_ops"
        assert p["priority"] == "high"
        assert "检查DB状态" in p["request_message"]
        assert p["request_id"].startswith("xd_")
        assert p["created_at"] != ""

    @pytest.mark.asyncio
    async def test_node_id_format(self):
        """跨域事件 node_id 格式: cross_domain:{request_id}:{subtype}。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_ops",
            request_message="test",
        )

        xd_events = _filter_cross_domain_events(events)
        for ev in xd_events:
            assert ev.node_id.startswith(f"cross_domain:{result.request_id}:")

    @pytest.mark.asyncio
    async def test_parent_run_id_propagation(self):
        """parent_run_id 传播到事件。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_ops",
            request_message="test",
            parent_run_id="parent_run_123",
        )

        xd_events = _filter_cross_domain_events(events)
        for ev in xd_events:
            assert ev.run_id == "parent_run_123"

    @pytest.mark.asyncio
    async def test_no_event_sink_still_works(self):
        """无 event_sink 时跨域请求仍正常执行（只缺审计）。"""
        coord = CrossDomainCoordinator(llm_config={}, event_sink=None)

        result = await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_ops",
            request_message="test",
        )

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_priority_in_payload(self):
        """priority 字段出现在 payload 中。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        await coord.handle_request(
            caller_agent="smart_query",
            target_domain="smart_ops",
            request_message="test",
            priority="low",
        )

        xd_events = _filter_cross_domain_events(events)
        for ev in xd_events:
            assert ev.payload["priority"] == "low"


# ====== make_tool_handler 测试 ======

class TestMakeToolHandler:
    """make_tool_handler 闭包注入测试。"""

    def setup_method(self):
        _reset_config_and_make_deterministic()

    @pytest.mark.asyncio
    async def test_tool_handler_closure(self):
        """make_tool_handler 创建的闭包正确注入 caller_agent。"""
        sink, events = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        handler = coord.make_tool_handler("smart_query")

        result = await handler({
            "target_domain": "smart_ops",
            "request": "检查服务器状态",
        })

        assert result["status"] == "completed"
        assert result["target_agent"] == "smart_ops"
        assert "request_id" in result

    @pytest.mark.asyncio
    async def test_tool_handler_missing_args(self):
        """工具参数缺失 → failed。"""
        coord = CrossDomainCoordinator(llm_config={})
        handler = coord.make_tool_handler("smart_query")

        result = await handler({"target_domain": "smart_ops"})  # 缺 request
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_tool_handler_denied(self):
        """工具 handler 权限拒绝时返回 denied。"""
        coord = CrossDomainCoordinator(llm_config={})
        handler = coord.make_tool_handler("nonexistent_agent")

        result = await handler({
            "target_domain": "smart_ops",
            "request": "test",
        })
        assert result["status"] == "denied"


# ====== request_cross_domain_tool 函数入口测试 ======

class TestRequestCrossDomainTool:
    """request_cross_domain_tool 工具入口测试。"""

    def setup_method(self):
        _reset_config_and_make_deterministic()

    @pytest.mark.asyncio
    async def test_tool_no_coordinator(self):
        """coordinator 未初始化 → failed。"""
        result = await request_cross_domain_tool(
            {"target_domain": "smart_ops", "request": "test"},
            caller_agent="smart_query",
            coordinator=None,
        )
        assert result["status"] == "failed"
        assert "未初始化" in result["content"]

    @pytest.mark.asyncio
    async def test_tool_missing_target_domain(self):
        """缺 target_domain → failed。"""
        coord = CrossDomainCoordinator(llm_config={})
        result = await request_cross_domain_tool(
            {"request": "test"},
            caller_agent="smart_query",
            coordinator=coord,
        )
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_tool_missing_request(self):
        """缺 request → failed。"""
        coord = CrossDomainCoordinator(llm_config={})
        result = await request_cross_domain_tool(
            {"target_domain": "smart_ops"},
            caller_agent="smart_query",
            coordinator=coord,
        )
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_tool_success(self):
        """工具入口正常调用。"""
        sink, _ = _make_event_collector()
        coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

        result = await request_cross_domain_tool(
            {"target_domain": "smart_ops", "request": "检查状态", "priority": "high"},
            caller_agent="smart_query",
            coordinator=coord,
        )

        assert result["status"] == "completed"
        assert result["target_agent"] == "smart_ops"
        assert result["content"] != ""

    @pytest.mark.asyncio
    async def test_tool_denied_returns_error(self):
        """工具入口权限拒绝时返回 error 字段。"""
        coord = CrossDomainCoordinator(llm_config={})
        result = await request_cross_domain_tool(
            {"target_domain": "smart_ops", "request": "test"},
            caller_agent="nonexistent",
            coordinator=coord,
        )
        assert result["status"] == "denied"
        assert "error" in result


# ====== e2e: SessionEngine ↔ Coordinator 注入链路测试（P0-2） ======

class TestSessionEngineCrossDomainIntegration:
    """P0-2: SessionEngine 注入 Coordinator 后，跨域请求走完整 12 步事件流。

    验证：SessionEngine.start_turn() 内调 request_cross_domain 工具
    → coordinator 中转 → 子 dag 跑完 → 6 个 cross_domain.* 事件全部 emit
    → content 返回非空（包含子 agent 摘要）。
    """

    def setup_method(self):
        _reset_config_and_make_deterministic()

    @pytest.mark.asyncio
    async def test_session_engine_with_coordinator_dispatches(self):
        """SessionEngine 注入 Coordinator 后，跨域请求能中转到 target agent。"""
        from harness import HarnessRegistry, HarnessType
        from harness.deterministic import DeterministicClient
        from harness.protocol import AgentEvent, AgentEventType
        from orchestrator.session_engine import SessionEngine

        turn = {"n": 0}
        captured_args: list[dict] = []

        class FakeCrossDomainHarness:
            """单轮：调 request_cross_domain(target_domain="smart_ops") + 立刻 finalize。"""

            @property
            def harness_type(self):
                return HarnessType.DETERMINISTIC

            async def run(self, prompt, tools, context):
                # 调跨域工具
                xcd_tool = next(t for t in tools if t.name == "request_cross_domain")
                result = await xcd_tool.handler({
                    "target_domain": "smart_ops",
                    "request": "查询服务器健康",
                })
                captured_args.append(result)
                # 立即 finalize 结束 turn
                fin_tool = next(t for t in tools if t.name == "finalize")
                await fin_tool.handler({"summary": "done"})
                yield AgentEvent(type=AgentEventType.TEXT, text=f"跨域调用结果: {result.get('status', '?')}")
                yield AgentEvent(type=AgentEventType.DONE)

        HarnessRegistry.register(HarnessType.DETERMINISTIC, FakeCrossDomainHarness)
        try:
            sink, events = _make_event_collector()
            coord = CrossDomainCoordinator(llm_config={}, event_sink=sink)

            engine = SessionEngine(
                session_id="run_xcd_e2e",
                agent_id="smart_query",
                llm_config={},
                event_sink=sink,
                harness_type=HarnessType.DETERMINISTIC,
                system_prompt="test",
                cross_domain_coordinator=coord,
            )

            result = await asyncio.wait_for(engine.start_turn("开始"), timeout=5.0)
            assert result is not None

            # 验证 1：跨域工具返回成功
            assert len(captured_args) == 1
            assert captured_args[0]["status"] == "completed"
            assert captured_args[0]["target_agent"] == "smart_ops"

            # 验证 2：emit 了 6 个 cross_domain.* 事件
            xd_subtypes = sorted([
                ev.payload.get("event_subtype")
                for ev in events
                if isinstance(ev, DagEvent) and ev.payload.get("event_subtype")
            ])
            expected = [
                "dispatched", "received", "requested",
                "returned", "target_completed", "target_started",
            ]
            assert xd_subtypes == expected, (
                f"期望 6 个跨域事件，实际: {xd_subtypes}"
            )

            # 验证 3：cross_domain 事件归属到本 run
            xd_events_for_our_run = [
                ev for ev in events
                if isinstance(ev, DagEvent) and ev.payload.get("event_subtype")
            ]
            assert all(ev.run_id == "run_xcd_e2e" for ev in xd_events_for_our_run)
        finally:
            HarnessRegistry.register(HarnessType.DETERMINISTIC, DeterministicClient)

    @pytest.mark.asyncio
    async def test_session_engine_without_coordinator_fast_fails(self):
        """SessionEngine 不注入 Coordinator 时，跨域请求 fast-fail（向后兼容）。

        回归测试：保证 DAG 节点路径（不传 coordinator）仍能跑通，只是 request_cross_domain 失败。
        """
        from harness import HarnessRegistry, HarnessType
        from harness.deterministic import DeterministicClient
        from harness.protocol import AgentEvent, AgentEventType
        from orchestrator.session_engine import SessionEngine

        turn = {"n": 0}
        captured_args: list[dict] = []

        class FakeHarness:
            """单轮：调 request_cross_domain（coordinator=None 会 fast-fail）+ 立即 finalize。"""

            @property
            def harness_type(self):
                return HarnessType.DETERMINISTIC

            async def run(self, prompt, tools, context):
                xcd_tool = next(t for t in tools if t.name == "request_cross_domain")
                result = await xcd_tool.handler({
                    "target_domain": "smart_ops",
                    "request": "test",
                })
                captured_args.append(result)
                fin_tool = next(t for t in tools if t.name == "finalize")
                await fin_tool.handler({"summary": "done"})
                yield AgentEvent(type=AgentEventType.TEXT, text=f"跨域调用结果: {result['content']}")
                yield AgentEvent(type=AgentEventType.DONE)

        HarnessRegistry.register(HarnessType.DETERMINISTIC, FakeHarness)
        try:
            sink, events = _make_event_collector()

            engine = SessionEngine(
                session_id="run_xcd_no_coord",
                agent_id="smart_query",
                llm_config={},
                event_sink=sink,
                harness_type=HarnessType.DETERMINISTIC,
                system_prompt="test",
                # cross_domain_coordinator=None（默认）
            )

            result = await asyncio.wait_for(engine.start_turn("开始"), timeout=5.0)
            assert result is not None

            # 验证 1：fast-fail 返回
            assert len(captured_args) == 1
            assert captured_args[0]["status"] == "failed"
            assert "未初始化" in captured_args[0]["content"]

            # 验证 2：未 emit 任何 cross_domain 事件
            xd_events = [
                ev for ev in events
                if isinstance(ev, DagEvent) and ev.payload.get("event_subtype")
            ]
            assert len(xd_events) == 0
        finally:
            HarnessRegistry.register(HarnessType.DETERMINISTIC, DeterministicClient)


# ====== 数据结构测试 ======

class TestCrossDomainDataStructures:
    """CrossDomainRequest / CrossDomainResult 数据结构测试。"""

    def test_request_defaults(self):
        """CrossDomainRequest 默认值。"""
        req = CrossDomainRequest(
            request_id="xd_test",
            caller_agent="smart_query",
            caller_domain="smart_query",
            target_domain="smart_ops",
            request_message="test",
        )
        assert req.priority == "normal"
        assert req.created_at != ""

    def test_result_defaults(self):
        """CrossDomainResult 默认值。"""
        res = CrossDomainResult(request_id="xd_test", status="completed")
        assert res.status == "completed"
        assert res.target_agent == ""
        assert res.result == {}
        assert res.result_text == ""
        assert res.error == ""
