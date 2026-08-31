"""测试 codex app-server harness（v2 Thread 模式）。

验证：
  1. codex 二进制可解析
  2. tool_digest / thread_name 计算
  3. notification 映射（item/agentMessage/delta, item/reasoning, item/started, item/completed, turn/completed）
  4. 环境变量构建（OPENAI_API_KEY 而非 ANTHROPIC_API_KEY）
  5. credential redaction
  6. 集成：simple prompt（需 codex 可用）
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.codex_appserver import (
    CodexAppServerClient,
    _resolve_codex_bin,
    _compute_tool_digest,
    _thread_name,
    _redact,
    _AgentMessageState,
)
from harness.protocol import (
    AgentEventType,
    AgentRunContext,
    ToolDefinition,
)

CODEX_AVAILABLE = shutil.which("codex") is not None


# ====== 基础工具函数测试 ======

def test_resolve_codex_bin() -> None:
    """codex 二进制应能在 PATH 或常见路径中找到。"""
    bin_path = _resolve_codex_bin()
    assert bin_path, "codex bin 不应为空"
    assert os.path.exists(bin_path) or bin_path == "codex", f"非兜底路径应存在: {bin_path}"


def test_compute_tool_digest() -> None:
    """tools schema 相同 -> digest 相同；tools 不同 -> digest 不同。"""
    t1 = [ToolDefinition(name="a", description="desc a", input_schema={})]
    t2 = [ToolDefinition(name="a", description="desc a", input_schema={})]
    t3 = [ToolDefinition(name="b", description="desc b", input_schema={"type": "object"})]
    assert _compute_tool_digest(t1) == _compute_tool_digest(t2)
    assert _compute_tool_digest(t1) != _compute_tool_digest(t3)


def test_thread_name() -> None:
    """thread name 格式：agentops-{sessionHash}-{toolDigest[:16]}"""
    digest = "abc123def456" * 4  # 48 chars
    name = _thread_name("test-session-001", digest)
    assert name.startswith("agentops-")
    parts = name.split("-")
    assert len(parts) == 3  # agentops + session_hash + tool_digest


# ====== credential redaction 测试 ======

def test_redact_hides_api_keys() -> None:
    """_redact 应抹除敏感字段。"""
    obj = {
        "OPENAI_API_KEY": "sk-abc123",
        "api_key": "secret",
        "model": "MiniMax-M3",
        "base_url": "https://api.minimaxi.com/v1",
    }
    redacted = _redact(obj)
    assert redacted["OPENAI_API_KEY"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["model"] == "MiniMax-M3"
    assert redacted["base_url"] == "https://api.minimaxi.com/v1"


def test_redact_string_hides_bearer_tokens() -> None:
    """_redact_string 应抹除 Bearer token。"""
    from harness.codex_appserver import _redact_string
    s = "Authorization: Bearer abc123def456"
    assert "abc123def456" not in _redact_string(s)
    assert "[REDACTED]" in _redact_string(s)


# ====== 环境变量构建测试 ======

def test_build_env_uses_openai_keys() -> None:
    """_build_env 应使用 OPENAI_API_KEY 而非 ANTHROPIC_API_KEY。"""
    client = CodexAppServerClient()
    ctx = AgentRunContext(
        system_prompt="test",
        model="minimax/M3",
        api_key="test-key-123",
        base_url="https://api.test.com/v1",
        workspace=".",
        session_id="test",
    )
    env = client._build_env(ctx)
    assert env.get("OPENAI_API_KEY") == "test-key-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.test.com/v1"
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_falls_back_to_os_environ() -> None:
    """_build_env 应从 os.environ 回退。"""
    os.environ["OPENAI_API_KEY"] = "env-key"
    try:
        client = CodexAppServerClient()
        ctx = AgentRunContext(
            system_prompt="test",
            model="",
            api_key="",
            base_url="",
            workspace=".",
            session_id="test",
        )
        env = client._build_env(ctx)
        assert env.get("OPENAI_API_KEY") == "env-key"
    finally:
        del os.environ["OPENAI_API_KEY"]


# ====== notification 映射测试 ======

class TestNotificationMapping:
    """测试 _map_notification 方法。"""

    def setup_method(self) -> None:
        self.client = CodexAppServerClient()
        self.client._agent_messages.clear()

    def test_agent_message_delta_yields_text(self) -> None:
        """item/agentMessage/delta 应产出流式 TEXT 事件。"""
        events = self.client._map_notification("item/agentMessage/delta", {
            "delta": "Hello",
            "itemId": "msg-1",
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.TEXT
        assert events[0].text == "Hello"

    def test_agent_message_delta_accumulates(self) -> None:
        """多个 delta 应累积到同一个 _AgentMessageState。"""
        self.client._map_notification("item/agentMessage/delta", {"delta": "Hi", "itemId": "msg-1"})
        self.client._map_notification("item/agentMessage/delta", {"delta": " there", "itemId": "msg-1"})
        state = self.client._agent_messages.get("msg-1")
        assert state is not None
        assert "".join(state.deltas) == "Hi there"
        assert state.deltas_yielded is True

    def test_reasoning_delta_yields_thinking(self) -> None:
        """item/reasoning/textDelta 应产出 THINKING 事件。"""
        events = self.client._map_notification("item/reasoning/textDelta", {
            "delta": "Let me think...",
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.THINKING
        assert events[0].text == "Let me think..."

    def test_reasoning_summary_yields_thinking(self) -> None:
        """item/reasoning/summaryTextDelta 应产出 THINKING 事件。"""
        events = self.client._map_notification("item/reasoning/summaryTextDelta", {
            "text": "Summary of reasoning",
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.THINKING

    def test_item_started_agent_message_tracks_phase(self) -> None:
        """item/started agentMessage 应记录 phase。"""
        self.client._map_notification("item/started", {
            "item": {"root": {"type": "agentMessage", "id": "msg-1", "phase": "final_answer"}},
        })
        state = self.client._agent_messages.get("msg-1")
        assert state is not None
        assert state.phase == "final_answer"

    def test_item_started_command_execution_yields_tool_use(self) -> None:
        """item/started commandExecution 应产出 TOOL_USE 事件。"""
        events = self.client._map_notification("item/started", {
            "item": {"root": {"type": "commandExecution", "id": "cmd-1", "command": "ls -la"}},
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.TOOL_USE
        assert events[0].tool_name == "bash"
        assert events[0].tool_input == {"command": "ls -la"}

    def test_item_completed_agent_message_no_delta(self) -> None:
        """item/completed agentMessage 在无 delta 时补发完整文本。"""
        # 先 item/started 建立 state
        self.client._map_notification("item/started", {
            "item": {"root": {"type": "agentMessage", "id": "msg-1", "phase": "final_answer"}},
        })
        # item/completed 带完整文本，但没有 delta 流式过
        events = self.client._map_notification("item/completed", {
            "item": {"root": {"type": "agentMessage", "id": "msg-1", "text": "完整回复"}},
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.TEXT
        assert events[0].text == "完整回复"

    def test_item_completed_agent_message_with_delta_no_duplicate(self) -> None:
        """item/completed agentMessage 在 delta 已流式过时不重复发。"""
        # 先 delta 流式
        self.client._map_notification("item/agentMessage/delta", {"delta": "流式", "itemId": "msg-1"})
        # item/completed 不应再发 TEXT
        events = self.client._map_notification("item/completed", {
            "item": {"root": {"type": "agentMessage", "id": "msg-1", "text": "流式"}},
        })
        assert len(events) == 0

    def test_item_completed_command_execution_yields_tool_result(self) -> None:
        """item/completed commandExecution 应产出 TOOL_RESULT 事件。"""
        events = self.client._map_notification("item/completed", {
            "item": {"root": {
                "type": "commandExecution",
                "id": "cmd-1",
                "exitCode": 0,
                "aggregatedOutput": "file1\nfile2",
            }},
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.TOOL_RESULT
        assert events[0].tool_result == "file1\nfile2"
        assert events[0].tool_is_error is False

    def test_item_completed_command_execution_error(self) -> None:
        """item/completed commandExecution exitCode!=0 应标记 is_error。"""
        events = self.client._map_notification("item/completed", {
            "item": {"root": {
                "type": "commandExecution",
                "id": "cmd-1",
                "exitCode": 1,
                "aggregatedOutput": "error msg",
            }},
        })
        assert events[0].tool_is_error is True

    def test_item_completed_mcp_tool_call(self) -> None:
        """item/completed mcpToolCall 应产出 TOOL_RESULT 事件。"""
        events = self.client._map_notification("item/completed", {
            "item": {"root": {
                "type": "mcpToolCall",
                "id": "mcp-1",
                "tool": "my_tool",
                "result": {"content": [{"type": "text", "text": "result text"}]},
            }},
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.TOOL_RESULT
        assert events[0].tool_result == "result text"
        assert events[0].tool_is_error is False

    def test_turn_completed_flushes_pending_deltas(self) -> None:
        """turn/completed 应 flush 未流式输出的 delta。"""
        # 建立一个有 delta 但没流式过的 state（模拟直接 set）
        self.client._agent_messages["msg-1"] = _AgentMessageState(
            deltas=["pending text"], deltas_yielded=False,
        )
        events = self.client._map_notification("turn/completed", {})
        assert len(events) == 1
        assert events[0].type == AgentEventType.TEXT
        assert events[0].text == "pending text"
        # 清理后 state 应被移除
        assert "msg-1" not in self.client._agent_messages

    def test_realtime_transcript_delta(self) -> None:
        """thread/realtime/transcript/delta 应产出 TEXT 事件。"""
        events = self.client._map_notification("thread/realtime/transcript/delta", {
            "delta": "语音文本",
        })
        assert len(events) == 1
        assert events[0].type == AgentEventType.TEXT
        assert events[0].text == "语音文本"

    def test_unknown_notification_returns_empty(self) -> None:
        """未知 notification 应返回空列表。"""
        events = self.client._map_notification("some/unknown/method", {"foo": "bar"})
        assert len(events) == 0

    def test_empty_params_returns_empty(self) -> None:
        """空 params 应返回空列表。"""
        events = self.client._map_notification("item/started", {})
        assert len(events) == 0


# ====== 集成测试（需 codex 二进制）======

@pytest.mark.asyncio
@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex binary not available")
async def test_run_simple_prompt() -> None:
    """无工具的简单 prompt，应能拿到 TEXT + USAGE + DONE。"""
    client = CodexAppServerClient(timeout=120.0)
    ctx = AgentRunContext(
        system_prompt="你是 helpful assistant。用一句话回答问题。用中文回答。",
        model="",
        api_key="",
        base_url="",
        workspace=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        session_id="test_simple",
        protocol="openai_compatible",
        persist_session=False,
    )
    events = []
    async for ev in client.run("1+1=?", [], ctx):
        events.append(ev)

    types = [e.type for e in events]
    assert AgentEventType.TEXT in types, f"缺少 TEXT 事件：{types}"
    assert AgentEventType.DONE in types, f"缺少 DONE 事件：{types}"
    text_events = [e for e in events if e.type == AgentEventType.TEXT and e.text]
    assert any(e.text for e in text_events), f"TEXT 事件无内容: {text_events}"
