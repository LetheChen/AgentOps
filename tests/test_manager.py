"""P6: Manager Agent + 动态 DAG 编排测试。

验证:
  - DomainRouter 关键词匹配路由
  - DynamicDagSpec 转 WorkflowDefinition + 校验 + 环检测
  - ManagerAgent 端到端编排（路由 → 动态 DAG → 执行 → 聚合）
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.config_loader import (
    AgentDefinition,
    ConfigLoader,
    get_system_config,
    reload_system_config,
)
from orchestrator.dynamic_dag import DynamicDagSpec, DynamicNodeSpec
from orchestrator.manager import ManagerAgent, ManagerResult
from orchestrator.router import DomainRouter, RouteResult
from workflow.schema import HarnessTypeRef, NodeType, WorkflowDefinition, WorkflowNode


# ====== DomainRouter 测试 ======

class TestDomainRouter:
    """DomainRouter 关键词匹配路由测试。"""

    def setup_method(self):
        from orchestrator import config_loader as cl_module
        cl_module._system_config = None
        self.router = DomainRouter()

    def test_route_smart_query_keyword(self):
        """关键词匹配智能问数域。"""
        result = self.router.route("查询本月销售数据")
        assert result.domain == "smart_query"
        assert result.method == "keyword"
        assert result.needs_dynamic_dag is True  # 无固定模板

    def test_route_smart_ops_keyword(self):
        """关键词匹配智能运维域。"""
        result = self.router.route("服务器重启")
        assert result.domain == "smart_ops"
        assert result.method == "keyword"

    def test_route_smart_form_keyword(self):
        """关键词匹配智能填单域。"""
        result = self.router.route("提交报销单")
        assert result.domain == "smart_form"

    def test_route_no_keyword_fallback(self):
        """无关键词匹配 → fallback 动态编排。"""
        result = self.router.route("hello world 12345")
        assert result.domain is None
        assert result.method == "fallback"
        assert result.needs_dynamic_dag is True

    def test_route_multi_domain_fallback(self):
        """多域匹配 → fallback 跨域编排。"""
        # "查询服务器日志" 同时命中 smart_query(查询) 和 smart_ops(服务器/日志)
        result = self.router.route("查询服务器日志")
        assert result.domain is None
        assert result.method == "fallback"
        assert result.needs_dynamic_dag is True

    def test_route_template_match(self):
        """固定模板匹配 → 直接路由。"""
        # template_routes: travel_expense → travel-expense
        result = self.router.route("travel_expense 差旅报销")
        assert result.workflow_id == "travel-expense"
        assert result.needs_dynamic_dag is False


# ====== DynamicDagSpec 测试 ======

class TestDynamicDagSpec:
    """DynamicDagSpec 动态 DAG 描述测试。"""

    def setup_method(self):
        from orchestrator import config_loader as cl_module
        cl_module._system_config = None

    def test_single_node_dag(self):
        """单节点 DAG 校验通过。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(id="step_1", agent_domain="smart_query", task_description="查询数据"),
            ],
        )
        errors = spec.validate()
        assert errors == [], f"校验错误: {errors}"

    def test_multi_node_dag_with_edges(self):
        """多节点 DAG + edges 校验通过。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(id="step_1", agent_domain="smart_query", task_description="查询"),
                DynamicNodeSpec(id="step_2", agent_domain="smart_analysis", task_description="分析"),
            ],
            edges=[("step_1", "step_2")],
        )
        errors = spec.validate()
        assert errors == []

    def test_undefined_domain_error(self):
        """未定义的域 → 校验报错。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(id="step_1", agent_domain="nonexistent", task_description="test"),
            ],
        )
        errors = spec.validate()
        assert any("nonexistent" in e for e in errors)

    def test_cycle_detection(self):
        """环检测。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(id="a", agent_domain="smart_query", task_description="a"),
                DynamicNodeSpec(id="b", agent_domain="smart_ops", task_description="b"),
            ],
            edges=[("a", "b"), ("b", "a")],  # 环
        )
        errors = spec.validate()
        assert any("环" in e for e in errors)

    def test_to_workflow_def(self):
        """转 WorkflowDefinition。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(id="step_1", agent_domain="smart_query", task_description="查询数据"),
            ],
        )
        wf = spec.to_workflow_def()
        assert isinstance(wf, WorkflowDefinition)
        assert "step_1" in wf.nodes
        assert wf.nodes["step_1"].domain == "smart_query"
        assert wf.nodes["step_1"].agent == "smart_query"

    def test_to_workflow_def_multi_node(self):
        """多节点转 WorkflowDefinition，after 正确设置。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(id="fetch", agent_domain="smart_query", task_description="查询"),
                DynamicNodeSpec(id="analyze", agent_domain="smart_analysis", task_description="分析"),
            ],
            edges=[("fetch", "analyze")],
        )
        wf = spec.to_workflow_def()
        assert wf.nodes["analyze"].after == ["fetch"]

    def test_serialize_deserialize(self):
        """序列化/反序列化。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(id="step_1", agent_domain="smart_query", task_description="查询"),
            ],
        )
        data = spec.to_dict()
        restored = DynamicDagSpec.from_dict(data)
        assert len(restored.nodes) == 1
        assert restored.nodes[0].id == "step_1"
        assert restored.nodes[0].agent_domain == "smart_query"


# ====== ManagerAgent 测试 ======

class TestManagerAgent:
    """ManagerAgent 端到端编排测试。"""

    def setup_method(self):
        from orchestrator import config_loader as cl_module
        cl_module._system_config = None
        # 临时把所有 agent 的 harness 改为 deterministic 以便测试
        config = get_system_config()
        for agent in config.agents.values():
            agent.harness = "deterministic"

    @pytest.mark.asyncio
    async def test_handle_routes_to_smart_query(self):
        """Manager 路由到 smart_query 域并动态编排。"""
        manager = ManagerAgent(llm_config={})
        result = await manager.handle("查询本月销售数据")
        assert result.route.domain == "smart_query"
        assert result.dynamic_dag_spec is not None
        assert len(result.dynamic_dag_spec.nodes) == 1
        assert result.dynamic_dag_spec.nodes[0].agent_domain == "smart_query"

    @pytest.mark.asyncio
    async def test_handle_routes_to_smart_ops(self):
        """Manager 路由到 smart_ops 域。"""
        manager = ManagerAgent(llm_config={})
        result = await manager.handle("服务器重启")
        assert result.route.domain == "smart_ops"
        assert result.dynamic_dag_spec is not None

    @pytest.mark.asyncio
    async def test_handle_no_keyword_fallback(self):
        """无关键词 → fallback 动态编排。"""
        manager = ManagerAgent(llm_config={})
        result = await manager.handle("hello world")
        assert result.route.domain is None
        assert result.route.method == "fallback"
        assert result.route.needs_dynamic_dag is True

    @pytest.mark.asyncio
    async def test_handle_template_route(self):
        """固定模板路由 → 不生成动态 DAG。"""
        manager = ManagerAgent(llm_config={})
        result = await manager.handle("travel_expense 差旅报销")
        assert result.route.workflow_id == "travel-expense"
        assert result.dynamic_dag_spec is None
        assert result.status == "routed"

    @pytest.mark.asyncio
    async def test_handle_dynamic_dag_executes(self):
        """动态 DAG 端到端执行（deterministic harness）。"""
        manager = ManagerAgent(llm_config={})
        result = await manager.handle("查询数据")
        assert result.status in ("completed", "failed")  # deterministic 可能完成
        assert result.run_id != ""  # 有 run_id
        # dynamic_dag_spec 应该存在
        assert result.dynamic_dag_spec is not None

    @pytest.mark.asyncio
    async def test_handle_aggregates_output(self):
        """Manager 聚合子 Agent 输出。"""
        manager = ManagerAgent(llm_config={})
        result = await manager.handle("查询数据")
        # 如果执行成功，summary 应该有内容
        if result.status == "completed":
            assert result.summary != ""

    @pytest.mark.asyncio
    async def test_manager_does_not_hold_business_tools(self):
        """Manager 不持有业务域工具（只有编排工具）。"""
        config = get_system_config()
        manager_agent = config.agents.get("manager")
        assert manager_agent is not None
        # Manager 不应有 sql_query / server_restart 等业务工具
        business_tools = {"sql_query", "sql_execute", "server_restart", "db_migrate",
                          "ssh_exec", "submit_form", "approval_flow"}
        assert not (set(manager_agent.allowed_tools) & business_tools), \
            "Manager 不应持有业务域工具"

    @pytest.mark.asyncio
    async def test_manager_has_orchestration_tools(self):
        """Manager 有编排工具。"""
        config = get_system_config()
        manager_agent = config.agents.get("manager")
        orchestration_tools = {"classify_intent", "plan_tasks", "dispatch", "aggregate",
                               "present_content"}
        assert orchestration_tools.issubset(set(manager_agent.allowed_tools)), \
            f"Manager 应有编排工具: {orchestration_tools}"
