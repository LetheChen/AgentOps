"""
Phase 1.5 三层模型测试 — role_prompt 字段 + SystemPromptBuilder + validator 校验。

覆盖设计文档 §12 三层模型（Agent/Role/Workflow）：
- SystemPromptBuilder.build 支持 node_role 参数（角色层注入到 prompt 开头）
- validator 拒绝 role_prompt 无 agent 的节点（Role 必须依附 Agent）
- DagEngine._build_system_prompt 注入 role_prompt 到 system_prompt 开头
- workflow loader 正确解析 role_prompt 字段

Run with: PYTHONIOENCODING=utf-8 python -m pytest tests/test_phase1_5_role_prompt.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from orchestrator.prompt_builder import RoleContext, SystemPromptBuilder
from workflow import (
    load_workflow_text,
    validate_workflow,
    WorkflowValidationError,
)


# ====== SystemPromptBuilder.build + node_role 单元测试 ======

class TestSystemPromptBuilderNodeRole:
    """验证 SystemPromptBuilder.build 的 node_role 参数行为。"""

    def _make_agent(self, system_prompt: str = "你是基础能力体。") -> SimpleNamespace:
        """构造一个最小的 agent 桩对象。"""
        return SimpleNamespace(
            system_prompt=system_prompt,
            domain="video_production",
            allowed_tools=["mm_image"],
        )

    def test_role_prompt_injected_at_head(self):
        """node_role.role_prompt 必须注入到 system_prompt 开头。"""
        builder = SystemPromptBuilder()
        agent = self._make_agent()
        role = RoleContext(role_prompt="你是素材调研员，负责搜索素材。")

        prompt = builder.build(agent=agent, node_role=role)

        # 角色段在最前
        assert prompt.startswith("## 你的角色\n你是素材调研员")
        # 基础契约在后面
        assert "你是基础能力体。" in prompt
        # 顺序：role 在 base 之前
        assert prompt.index("你是素材调研员") < prompt.index("你是基础能力体。")

    def test_role_prompt_none_skips_role_section(self):
        """node_role=None 时不应注入「## 你的角色」段。"""
        builder = SystemPromptBuilder()
        agent = self._make_agent()

        prompt = builder.build(agent=agent, node_role=None)

        assert "## 你的角色" not in prompt
        assert "你是基础能力体。" in prompt

    def test_role_prompt_empty_string_skips_role_section(self):
        """role_prompt 为空字符串时也应跳过角色段（falsy）。"""
        builder = SystemPromptBuilder()
        agent = self._make_agent()
        role = RoleContext(role_prompt="")

        prompt = builder.build(agent=agent, node_role=role)

        assert "## 你的角色" not in prompt

    def test_business_role_does_not_inject_role_section(self):
        """business_role 字段不应触发角色段注入（只 role_prompt 触发）。"""
        builder = SystemPromptBuilder()
        agent = self._make_agent()
        role = RoleContext(business_role="素材调研员", role_prompt=None)

        prompt = builder.build(agent=agent, node_role=role)

        assert "## 你的角色" not in prompt

    def test_session_context_memory_appended(self):
        """session_context.memory 应追加到 prompt 末尾。"""
        builder = SystemPromptBuilder()
        agent = self._make_agent()

        prompt = builder.build(
            agent=agent,
            session_context={"memory": "上次会话用户喜欢短视频。"},
        )

        assert "## 历史记忆" in prompt
        assert "上次会话用户喜欢短视频。" in prompt

    def test_tools_prompt_appended(self):
        """tools_prompt 应作为「## 可用工具」段追加。"""
        builder = SystemPromptBuilder()
        agent = self._make_agent()

        prompt = builder.build(
            agent=agent,
            tools_prompt="- mm_image: 生成图像",
        )

        assert "## 可用工具" in prompt
        assert "- mm_image: 生成图像" in prompt

    def test_full_assembly_order(self):
        """完整拼装顺序：role → base → tools → memory。"""
        builder = SystemPromptBuilder()
        # 用明确的 marker 便于断言顺序
        agent = SimpleNamespace(
            system_prompt="BASE_CONTRACT_MARKER",
            domain="video_production",
            allowed_tools=[],
        )
        role = RoleContext(role_prompt="ROLE_PROMPT_MARKER")

        prompt = builder.build(
            agent=agent,
            node_role=role,
            tools_prompt="TOOLS_PROMPT_MARKER",
            session_context={"memory": "MEMORY_MARKER"},
        )

        # 顺序校验
        idx_role = prompt.index("ROLE_PROMPT_MARKER")
        idx_base = prompt.index("BASE_CONTRACT_MARKER")
        idx_tools = prompt.index("TOOLS_PROMPT_MARKER")
        idx_memory = prompt.index("MEMORY_MARKER")
        assert idx_role < idx_base < idx_tools < idx_memory


# ====== validator role_prompt 校验单元测试 ======

class TestValidatorRolePrompt:
    """验证 validator 拒绝 role_prompt 无 agent 的节点。"""

    def test_role_prompt_without_agent_rejected(self):
        """配了 role_prompt 但未配 agent 的节点必须报错。"""
        wf_yaml = """
workflow_id: test-role-no-agent
name: 测试 role_prompt 无 agent
nodes:
  n1:
    name: 节点1
    type: agent
    harness: opencode
    role_prompt: |
      你是素材调研员。
    after: []
    outputs: {}
"""
        wf = load_workflow_text(wf_yaml)
        with pytest.raises(WorkflowValidationError) as exc_info:
            validate_workflow(wf)
        # 错误信息必须提到 role_prompt 必须依附能力载体
        assert any("role_prompt" in e and "agent" in e for e in exc_info.value.errors)

    def test_role_prompt_with_agent_accepted(self):
        """配了 role_prompt 且配了 agent 的节点应通过校验。"""
        wf_yaml = """
workflow_id: test-role-with-agent
name: 测试 role_prompt 有 agent
nodes:
  n1:
    name: 节点1
    type: agent
    agent: video_creator
    harness: opencode
    role_prompt: |
      你是素材调研员。
    after: []
    outputs: {}
"""
        wf = load_workflow_text(wf_yaml)
        # 不应抛异常
        validate_workflow(wf)

    def test_no_role_prompt_no_agent_still_rejected_by_existing_rule(self):
        """未配 role_prompt 且未配 agent 的节点走原规则 10（非 deterministic 必须有 agent）。"""
        wf_yaml = """
workflow_id: test-no-role-no-agent
name: 测试无 role_prompt 无 agent
nodes:
  n1:
    name: 节点1
    type: agent
    harness: opencode
    after: []
    outputs: {}
"""
        wf = load_workflow_text(wf_yaml)
        with pytest.raises(WorkflowValidationError) as exc_info:
            validate_workflow(wf)
        # 走原规则 10
        assert any("harness 节点必须配 agent_id" in e for e in exc_info.value.errors)

    def test_deterministic_node_without_agent_with_role_prompt_still_rejected(self):
        """deterministic 节点虽豁免规则 10，但配了 role_prompt 仍必须有 agent（三层模型铁律）。"""
        wf_yaml = """
workflow_id: test-det-role-no-agent
name: 测试 deterministic+role_prompt 无 agent
nodes:
  n1:
    name: 节点1
    type: agent
    harness: deterministic
    role_prompt: |
      你是数据采集员。
    after: []
    outputs: {}
"""
        wf = load_workflow_text(wf_yaml)
        with pytest.raises(WorkflowValidationError) as exc_info:
            validate_workflow(wf)
        assert any("role_prompt" in e for e in exc_info.value.errors)


# ====== workflow loader 解析 role_prompt 字段 ======

class TestLoaderRolePrompt:
    """验证 workflow loader 正确解析 role_prompt 字段。"""

    def test_role_prompt_parsed_from_yaml(self):
        """loader 应把 yaml 的 role_prompt 字段填入 WorkflowNode.role_prompt。"""
        wf_yaml = """
workflow_id: test-loader-role
name: 测试 loader role_prompt
nodes:
  n1:
    name: 节点1
    type: agent
    agent: video_creator
    harness: opencode
    business_role: 素材调研员
    role_prompt: |
      你是素材调研员，负责搜索素材。
    after: []
    outputs: {}
"""
        wf = load_workflow_text(wf_yaml)
        node = wf.nodes["n1"]
        assert node.role_prompt is not None
        assert "你是素材调研员" in node.role_prompt
        # business_role 字段也应正确解析
        assert node.business_role == "素材调研员"

    def test_role_prompt_absent_defaults_none(self):
        """未配 role_prompt 时 WorkflowNode.role_prompt 应为 None。"""
        wf_yaml = """
workflow_id: test-loader-no-role
name: 测试 loader 无 role_prompt
nodes:
  n1:
    name: 节点1
    type: agent
    agent: video_creator
    harness: opencode
    after: []
    outputs: {}
"""
        wf = load_workflow_text(wf_yaml)
        node = wf.nodes["n1"]
        assert node.role_prompt is None


# ====== DagEngine._build_system_prompt 注入 role_prompt（轻量级集成测试）======

class TestDagEngineBuildSystemPromptWithRole:
    """验证 DagEngine._build_system_prompt 把 role_prompt 注入到 system_prompt 开头。

    不启动完整 DAG run，只测 _build_system_prompt 方法本身。
    用最小桩对象构造 DagEngine，避免依赖完整 config 加载。
    """

    def _make_minimal_dag_engine(self, agent_system_prompt: str = "BASE_PROMPT"):
        """构造一个最小可用的 DagEngine 桩，仅用于 _build_system_prompt 测试。"""
        from workflow.engine import DagEngine
        from workflow.schema import WorkflowDefinition

        # 最小 workflow 定义
        wf = WorkflowDefinition(
            workflow_id="test-dag-engine-role",
            name="测试 DagEngine role_prompt",
            nodes={},
        )

        # 用 object.__new__ 绕过 __init__ 的复杂依赖
        engine = object.__new__(DagEngine)
        engine.workflow = wf
        engine.run_state = SimpleNamespace(run_id="test-run-001")

        # 桩 _get_agent_def：返回带 system_prompt 的 agent
        agent_def = SimpleNamespace(system_prompt=agent_system_prompt)
        engine._get_agent_def = lambda agent_id: agent_def

        # 桩 _resolve_template：直接返回原字符串
        engine._resolve_template = lambda tpl, inputs, **kw: tpl

        return engine

    def test_role_prompt_prepended_to_system_prompt(self):
        """DagEngine._build_system_prompt 应把 role_prompt 注入到 base_prompt 之前。"""
        from workflow.schema import WorkflowNode, NodeType, HarnessTypeRef

        engine = self._make_minimal_dag_engine(agent_system_prompt="BASE_PROMPT")
        node = WorkflowNode(
            id="n1",
            name="节点1",
            type=NodeType.AGENT,
            agent="video_creator",
            harness=HarnessTypeRef.OPENCODE,
            role_prompt="你是素材调研员。",
        )

        # 用最简 nstate 桩
        nstate = SimpleNamespace()
        prompt = engine._build_system_prompt(node, nstate)

        assert prompt.startswith("## 你的角色\n你是素材调研员。")
        assert "BASE_PROMPT" in prompt
        # 顺序
        assert prompt.index("你是素材调研员。") < prompt.index("BASE_PROMPT")

    def test_no_role_prompt_returns_base_only(self):
        """未配 role_prompt 时 _build_system_prompt 应只返回 base_prompt。"""
        from workflow.schema import WorkflowNode, NodeType, HarnessTypeRef

        engine = self._make_minimal_dag_engine(agent_system_prompt="BASE_PROMPT")
        node = WorkflowNode(
            id="n1",
            name="节点1",
            type=NodeType.AGENT,
            agent="video_creator",
            harness=HarnessTypeRef.OPENCODE,
            role_prompt=None,
        )

        nstate = SimpleNamespace()
        prompt = engine._build_system_prompt(node, nstate)

        assert "## 你的角色" not in prompt
        assert prompt == "BASE_PROMPT"
