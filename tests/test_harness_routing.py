"""H1: Harness 透明路由测试 — 验证 _create_harness 不再偷换。

关键验证点：
1. harness: opencode → 真正创建 OpencodeHarness（不是 LocalLlmClient）
2. harness: claude_code → 真正创建 ClaudeCodeClient（不是 LocalLlmClient）
3. harness: local_llm → 创建 LocalLlmClient（一等公民）
4. harness: deterministic → 创建 DeterministicClient
5. LocalLlmClient.harness_type == LOCAL_LLM（不再偷占 OPENCODE 槽位）
6. HarnessRegistry 注册了 7 种 harness（含 LOCAL_LLM）
"""
from __future__ import annotations

import pytest

from harness import (
    HarnessRegistry,
    HarnessType,
)
from harness.local_llm import LocalLlmClient
from harness.opencode_harness import OpencodeHarness
from harness.claude_code import ClaudeCodeClient
from harness.deterministic import DeterministicClient
# 从 orchestrator 入口 import 避免循环依赖（orchestrator → workflow.engine 链已打通）
from orchestrator import LocalSdkOrchestrator
from workflow.schema import (
    HarnessTypeRef,
    NodeType,
    WorkflowDefinition,
    WorkflowNode,
)


# ====== 测试用 workflow ======

def _make_workflow(harness_ref: HarnessTypeRef) -> WorkflowDefinition:
    """构造单节点 workflow，指定 harness 类型。"""
    return WorkflowDefinition(
        workflow_id="test_harness_routing",
        name="Test Harness Routing",
        nodes={
            "only": WorkflowNode(
                id="only",
                name="test",
                type=NodeType.AGENT,
                agent="echo_agent",
                harness=harness_ref,
                after=[],
                inputs=[],
                outputs={},
            ),
        },
    )


def _get_engine(harness_ref: HarnessTypeRef):
    """通过 orchestrator 构造 DagEngine（避免循环 import）。"""
    from workflow.engine import DagEngine  # 此时 orchestrator 已加载，循环已断开
    wf = _make_workflow(harness_ref)
    return DagEngine(wf, llm_config={})


# ====== 测试 ======

class TestHarnessRegistryCompleteness:
    """H1: Registry 注册完整性。"""

    def test_local_llm_registered(self):
        """LocalLlmClient 已注册为一等公民。"""
        assert HarnessType.LOCAL_LLM in HarnessRegistry._factories, \
            "LOCAL_LLM 未注册到 HarnessRegistry"

    def test_seven_harness_types_registered(self):
        """7 种 harness 全部注册（含 LOCAL_LLM）。"""
        available = HarnessRegistry.available()
        assert HarnessType.OPENCODE in available
        assert HarnessType.CLAUDE_CODE in available
        assert HarnessType.DETERMINISTIC in available
        assert HarnessType.LOCAL_LLM in available

    def test_local_llm_harness_type_is_not_opencode(self):
        """LocalLlmClient.harness_type 不再偷占 OPENCODE 槽位。"""
        client = LocalLlmClient()
        assert client.harness_type == HarnessType.LOCAL_LLM, \
            f"LocalLlmClient.harness_type 应为 LOCAL_LLM，实际为 {client.harness_type}"


class TestCreateHarnessTransparentRouting:
    """H1: _create_harness 透明路由 — 永远走 Registry，不偷换。"""

    def test_opencode_harness_not_replaced_by_local_llm(self):
        """harness: opencode → 创建 OpencodeHarness，不是 LocalLlmClient。"""
        engine = _get_engine(HarnessTypeRef.OPENCODE)
        harness = engine._create_harness(HarnessTypeRef.OPENCODE)
        assert isinstance(harness, OpencodeHarness), \
            f"opencode harness 被偷换为 {type(harness).__name__}"
        assert not isinstance(harness, LocalLlmClient), \
            "opencode harness 被偷换为 LocalLlmClient！"

    def test_claude_code_harness_not_replaced_by_local_llm(self):
        """harness: claude_code → 创建 ClaudeCodeClient，不是 LocalLlmClient。"""
        engine = _get_engine(HarnessTypeRef.CLAUDE_CODE)
        harness = engine._create_harness(HarnessTypeRef.CLAUDE_CODE)
        assert isinstance(harness, ClaudeCodeClient), \
            f"claude_code harness 被偷换为 {type(harness).__name__}"
        assert not isinstance(harness, LocalLlmClient), \
            "claude_code harness 被偷换为 LocalLlmClient！"

    def test_local_llm_harness_created_directly(self):
        """harness: local_llm → 创建 LocalLlmClient（一等公民）。"""
        engine = _get_engine(HarnessTypeRef.LOCAL_LLM)
        harness = engine._create_harness(HarnessTypeRef.LOCAL_LLM)
        assert isinstance(harness, LocalLlmClient), \
            f"local_llm harness 应为 LocalLlmClient，实际为 {type(harness).__name__}"

    def test_deterministic_harness_created_directly(self):
        """harness: deterministic → 创建 DeterministicClient。"""
        engine = _get_engine(HarnessTypeRef.DETERMINISTIC)
        harness = engine._create_harness(HarnessTypeRef.DETERMINISTIC)
        assert isinstance(harness, DeterministicClient), \
            f"deterministic harness 应为 DeterministicClient，实际为 {type(harness).__name__}"

    def test_local_llm_no_arg_construction(self):
        """LocalLlmClient 支持无参构造（Registry.create 无参调用）。"""
        # 这验证了 HarnessRegistry.create(LOCAL_LLM) 不会因缺少参数而失败
        client = HarnessRegistry.create(HarnessType.LOCAL_LLM)
        assert isinstance(client, LocalLlmClient)
        assert client.base_url == ""  # 无参构造时为空
        assert client.api_key == ""
        assert client.model == ""


class TestHarnessTypeRefLocalLlm:
    """H1: HarnessTypeRef.LOCAL_LLM 枚举值。"""

    def test_local_llm_ref_exists(self):
        """HarnessTypeRef 有 LOCAL_LLM 值。"""
        assert hasattr(HarnessTypeRef, "LOCAL_LLM")
        assert HarnessTypeRef.LOCAL_LLM.value == "local_llm"

    def test_local_llm_ref_converts_to_harness_type(self):
        """HarnessTypeRef.LOCAL_LLM → HarnessType.LOCAL_LLM 转换正确。"""
        ht = HarnessType(HarnessTypeRef.LOCAL_LLM.value)
        assert ht == HarnessType.LOCAL_LLM
