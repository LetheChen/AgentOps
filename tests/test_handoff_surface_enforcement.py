"""L0 handoff-surface 强制校验测试。

Worker 层 handoff.ts:92-110 + Manager 层 active-runs.ts:2286-2330 双重防护机制。

测试 6 个核心场景：
1. agent 未 emit final surface → handoff 被拒绝（Worker 层）
2. agent emit 了 final surface → handoff 通过（Worker 层）
3. Manager 层兜底校验，节点 FAILED
4. failed 端口不校验（agent 可 fail-fast）
5. 节点 allowed_tools 不含 report_surface_state 则跳过
6. 被拒绝后 agent 补 emit final → handoff 成功
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

# 确保项目根在 sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _make_inline_agent_node(
    node_id: str = "actor_research",
    agent_id: str = "research",
    allowed_tools: list[str] | None = None,
    outputs: dict | None = None,
):
    """构造一个 inline_agent 节点用于测试。"""
    from workflow.schema import WorkflowNode, InlineAgentConfig, NodeType, HarnessTypeRef, OutputRoute

    if allowed_tools is None:
        allowed_tools = ["report_surface_state", "handoff"]

    return WorkflowNode(
        id=node_id,
        name=node_id,
        type=NodeType.AGENT,
        agent=None,
        inline_agent=InlineAgentConfig(
            role_prompt="test role",
            allowed_tools=allowed_tools,
            harness=HarnessTypeRef.LOCAL_LLM,
        ),
        outputs=outputs or {"report": OutputRoute(to=""), "failed": OutputRoute(to="")},
        business_role=agent_id,
    )


def _seed_phase_tracker(actor_id: str, view_id: str, phase: str | None):
    """向 _PHASE_TRACKER 注入数据模拟 agent 已 emit 某个 phase。"""
    from tools.report_surface_state import _PHASE_TRACKER
    if phase is None:
        _PHASE_TRACKER.pop(actor_id, None)
    else:
        _PHASE_TRACKER.setdefault(actor_id, {})[view_id] = phase


@pytest.fixture(autouse=True)
def _reset_trackers():
    """每个测试前后清空 phase tracker 避免污染。"""
    from tools.report_surface_state import _reset_phase_tracker
    _reset_phase_tracker()
    yield
    _reset_phase_tracker()


@pytest.fixture
def research_profile(tmp_path, monkeypatch):
    """构造一个 actor_visual_profile.json 让 load_actor_visual_profile 能加载。"""
    profile_data = {
        "actor_id": "research",
        "description": "test research actor",
        "allowed_surface_views": [
            {"view_id": "research-live", "description": "live view"},
        ],
    }
    profile_path = tmp_path / "actor_research" / "actor_visual_profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(json.dumps(profile_data), encoding="utf-8")

    # patch _profile_path 让 load_actor_visual_profile 找到我们的临时文件
    from orchestrator import actor_visual_profile as avp
    original_profile_path = avp._profile_path

    def _mock_profile_path(actor_id: str) -> Path:
        if actor_id == "research":
            return profile_path
        return original_profile_path(actor_id)

    monkeypatch.setattr(avp, "_profile_path", _mock_profile_path)
    return profile_data


class TestHandoffSurfaceEnforcement:
    """L0 双重防护测试。"""

    @pytest.mark.asyncio
    async def test_worker_rejects_handoff_without_final_surface(self, research_profile):
        """agent 未 emit final surface → handoff 被拒绝，retryable=True。

        场景：agent 只 emit 了 partial，没 emit final 就尝试 handoff。
        对应 Worker 层 handoff.ts:92-110 surface_sequence_incomplete。
        """
        from workflow.engine import make_dag_tools, _check_surface_final_violation
        from workflow.schema import WorkflowDefinition

        node = _make_inline_agent_node()
        workflow = MagicMock(spec=WorkflowDefinition)
        state = MagicMock()
        state.pending_handoffs = {}

        tools = make_dag_tools(workflow, node, state)
        handoff_tool = next(t for t in tools if t.name == "handoff")

        # 模拟 agent 只 emit 了 partial
        _seed_phase_tracker("research", "research-live", "partial")

        # 尝试 handoff
        result = await handoff_tool.handler({"port": "report", "content": "test"})

        # 应被拒绝
        assert result.get("is_error") is True, f"应被拒绝，实际: {result}"
        payload = json.loads(result["content"])
        assert payload["status"] == "rejected"
        assert payload["code"] == "surface_sequence_incomplete"
        assert payload["retryable"] is True
        assert payload["expected_phase"] == "final"
        assert "report_surface_state" in payload["next_action"]

        # pending_handoffs 不应被写入
        assert "report" not in state.pending_handoffs

    @pytest.mark.asyncio
    async def test_worker_accepts_handoff_with_final_surface(self, research_profile):
        """agent emit 了 final surface → handoff 通过。"""
        from workflow.engine import make_dag_tools
        from workflow.schema import WorkflowDefinition

        node = _make_inline_agent_node()
        workflow = MagicMock(spec=WorkflowDefinition)
        state = MagicMock()
        state.pending_handoffs = {}

        tools = make_dag_tools(workflow, node, state)
        handoff_tool = next(t for t in tools if t.name == "handoff")

        # 模拟 agent 已 emit final
        _seed_phase_tracker("research", "research-live", "final")

        result = await handoff_tool.handler({"port": "report", "content": "test"})

        # 应通过
        assert result.get("is_error") is not True, f"应通过，实际: {result}"
        assert "report" in state.pending_handoffs
        assert state.pending_handoffs["report"]["content"] == "test"

    @pytest.mark.asyncio
    async def test_failure_port_bypasses_check(self, research_profile):
        """failed 端口不校验（agent 可 fail-fast）。

        对应 _requiredSurfaceFinalViolation:2296-2298 — isFailurePort 直接 return。
        """
        from workflow.engine import make_dag_tools
        from workflow.schema import WorkflowDefinition

        node = _make_inline_agent_node()
        workflow = MagicMock(spec=WorkflowDefinition)
        state = MagicMock()
        state.pending_handoffs = {}

        tools = make_dag_tools(workflow, node, state)
        handoff_tool = next(t for t in tools if t.name == "handoff")

        # 不 emit 任何 surface
        _seed_phase_tracker("research", "research-live", None)

        # 用 failed 端口 handoff
        result = await handoff_tool.handler({"port": "failed", "content": "error"})

        # 应通过（失败端口不校验）
        assert result.get("is_error") is not True, f"失败端口应跳过校验: {result}"
        assert "failed" in state.pending_handoffs

    @pytest.mark.asyncio
    async def test_node_without_surface_tools_bypasses_check(self, research_profile):
        """节点 allowed_tools 不含 report_surface_state 则跳过。

        场景：普通 agent 节点没有声明 surface 工具，handoff 不应被拦截。
        """
        from workflow.engine import make_dag_tools
        from workflow.schema import WorkflowDefinition

        # 节点不含 surface 工具
        node = _make_inline_agent_node(allowed_tools=["handoff", "graph_context"])
        workflow = MagicMock(spec=WorkflowDefinition)
        state = MagicMock()
        state.pending_handoffs = {}

        tools = make_dag_tools(workflow, node, state)
        handoff_tool = next(t for t in tools if t.name == "handoff")

        # 不 emit 任何 surface
        _seed_phase_tracker("research", "research-live", None)

        result = await handoff_tool.handler({"port": "report", "content": "test"})

        # 应通过（节点不声明 surface 工具则跳过）
        assert result.get("is_error") is not True, f"无 surface 工具应跳过: {result}"
        assert "report" in state.pending_handoffs

    @pytest.mark.asyncio
    async def test_retry_after_surface_emit_succeeds(self, research_profile):
        """被拒绝后 agent 补 emit final → handoff 成功。

        场景：第一次 handoff 被拒（只 emit 了 partial），agent 补 emit final 后重试 handoff 通过。
        """
        from workflow.engine import make_dag_tools
        from workflow.schema import WorkflowDefinition

        node = _make_inline_agent_node()
        workflow = MagicMock(spec=WorkflowDefinition)
        state = MagicMock()
        state.pending_handoffs = {}

        tools = make_dag_tools(workflow, node, state)
        handoff_tool = next(t for t in tools if t.name == "handoff")

        # 第一次：只 emit partial，应被拒
        _seed_phase_tracker("research", "research-live", "partial")
        result1 = await handoff_tool.handler({"port": "report", "content": "test"})
        assert result1.get("is_error") is True

        # 第二次：补 emit final，应通过
        _seed_phase_tracker("research", "research-live", "final")
        result2 = await handoff_tool.handler({"port": "report", "content": "test"})
        assert result2.get("is_error") is not True
        assert "report" in state.pending_handoffs

    @pytest.mark.asyncio
    async def test_manager_blocks_node_without_final_surface(self, research_profile):
        """Manager 层兜底校验：节点未 emit final surface → 节点 FAILED（非 COMPLETED）。

        场景：agent 直接退出没调 handoff_tool，_finalize_completed_node 触发 Manager 层校验，
        节点应被标记为 FAILED 并 emit NODE_FAILED 事件。
        对应 active-runs.ts:2286-2330 _requiredSurfaceFinalViolation。
        """
        from workflow.engine import DagEngine
        from workflow.schema import WorkflowDefinition
        from orchestrator.protocol import NodeStatus

        node = _make_inline_agent_node()
        workflow = MagicMock(spec=WorkflowDefinition)
        engine = DagEngine.__new__(DagEngine)  # 不调 __init__ 跳过依赖
        engine.run_state = MagicMock()
        engine.run_state.run_id = "run_test_001"
        engine.node_states = {node.id: MagicMock()}
        engine.node_states[node.id].node = node
        engine.node_states[node.id].status = NodeStatus.RUNNING
        engine.node_states[node.id].finished_at = None
        engine.node_states[node.id].started_at = None
        engine.node_states[node.id].tokens_in = 0
        engine.node_states[node.id].tokens_out = 0
        engine.node_states[node.id].resolved_model = None
        engine.node_states[node.id].error = None

        emit_calls = []
        async def _mock_emit(event_type, node_id, payload):
            emit_calls.append((event_type, node_id, payload))
        engine._emit = _mock_emit
        engine._resolve_model_for_node = lambda n: "test-model"
        engine._cleanup_provisioned_subagent = AsyncMock()

        # 不 emit 任何 surface
        _seed_phase_tracker("research", "research-live", None)

        # 触发 _finalize_completed_node
        await engine._finalize_completed_node(node.id)

        # 节点应 FAILED（非 COMPLETED）
        assert engine.node_states[node.id].status == NodeStatus.FAILED
        assert "DAG_HANDOFF_SURFACE_INCOMPLETE" in engine.node_states[node.id].error

        # 应 emit NODE_FAILED 事件
        assert len(emit_calls) == 1
        event_type, event_node_id, event_payload = emit_calls[0]
        from orchestrator.protocol import DagEventType
        assert event_type == DagEventType.NODE_FAILED
        assert event_node_id == node.id
        assert event_payload["phase"] == "surface_final_violation"
        assert "DAG_HANDOFF_SURFACE_INCOMPLETE" in event_payload["error"]

    @pytest.mark.asyncio
    async def test_manager_accepts_node_with_final_surface(self, research_profile):
        """Manager 层校验：节点已 emit final surface → 节点 COMPLETED。

        对照测试：与 test_manager_blocks_node_without_final_surface 配对，确保
        emit final 后 Manager 层不拦截。
        """
        from workflow.engine import DagEngine
        from workflow.schema import WorkflowDefinition
        from orchestrator.protocol import NodeStatus

        node = _make_inline_agent_node()
        workflow = MagicMock(spec=WorkflowDefinition)
        engine = DagEngine.__new__(DagEngine)
        engine.workflow = workflow
        engine.run_state = MagicMock()
        engine.run_state.run_id = "run_test_002"
        engine.run_state.total_tokens_input = 0
        engine.run_state.total_tokens_output = 0
        engine.run_state.node_states = {}
        engine.run_state.node_outputs = {}
        engine.node_states = {node.id: MagicMock()}
        engine.node_states[node.id].node = node
        engine.node_states[node.id].status = NodeStatus.RUNNING
        engine.node_states[node.id].finished_at = None
        engine.node_states[node.id].started_at = None
        engine.node_states[node.id].tokens_in = 0
        engine.node_states[node.id].tokens_out = 0
        engine.node_states[node.id].resolved_model = None
        engine.node_states[node.id].pending_handoffs = {"report": {"content": "ok", "summary": ""}}
        engine.node_states[node.id].node = node

        emit_calls = []
        async def _mock_emit(event_type, node_id, payload):
            emit_calls.append((event_type, node_id, payload))
        engine._emit = _mock_emit
        engine._resolve_model_for_node = lambda n: "test-model"
        engine._emit_widgets_for_node = AsyncMock()
        engine._all_ports_blocked = lambda nstate: False
        engine._event_store = None
        engine._cleanup_provisioned_subagent = AsyncMock()
        engine._record_node_usage = AsyncMock()

        # emit final surface
        _seed_phase_tracker("research", "research-live", "final")

        await engine._finalize_completed_node(node.id)

        # 节点应 COMPLETED
        assert engine.node_states[node.id].status == NodeStatus.COMPLETED
        # 不应 emit NODE_FAILED
        from orchestrator.protocol import DagEventType
        failed_events = [e for e in emit_calls if e[0] == DagEventType.NODE_FAILED]
        assert len(failed_events) == 0, f"emit final 后不应有 NODE_FAILED: {failed_events}"
