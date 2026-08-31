"""Claude Agent SDK Harness 单测 — workspace_policy 防逃逸 + claude_sdk 核心逻辑。

覆盖（对应 DESIGN_claude_sdk_harness_v1.md 6.1）：
  1. workspace_policy.create_workspace_read_hook：Read/Grep/Glob/LS 路径校验
     （allow 相对路径 / deny 绝对路径逃逸 / deny .. 穿越 / deny Glob pattern 穿越 /
      deny 非法输入 / 非 READ 工具放行 / Windows 大小写不敏感）
  2. ClaudeSessionStore：新建 / 复用 / cwd 变化新开
  3. _resolve_model_name：anthropic 系解析 / 非 anthropic 返回空
  4. _map_message：AssistantMessage(Text/Thinking/ToolUse) / UserMessage(ToolResult) /
     ResultMessage(usage+done)
  5. build_mcp_server：handler=None 跳过 / 内置重名跳过 / 正常注册
  6. _wrap_tool_handler：dict→json / 异常→is_error / PermissionError→is_error
  7. run() cwd fail-fast：空 workspace → ERROR+DONE
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

# ---------- workspace_policy ----------


@pytest.fixture()
def read_hook(tmp_path: Path):
    from harness.workspace_policy import create_workspace_read_hook
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.txt").write_text("x", encoding="utf-8")
    return create_workspace_read_hook(str(tmp_path)), tmp_path


async def _run_hook(hook: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """统一调用 hook（兼容 sync/async 返回）。"""
    result = hook(payload)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def _decision(result: dict[str, Any]) -> str | None:
    hso = result.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision")


class TestWorkspaceReadHook:
    @pytest.mark.asyncio
    async def test_allow_relative_inside(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Read",
            "tool_input": {"file_path": "sub/file.txt"},
        })
        assert _decision(res) == "allow"

    @pytest.mark.asyncio
    async def test_allow_root_itself(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "LS", "tool_input": {"path": "."},
        })
        assert _decision(res) == "allow"

    @pytest.mark.asyncio
    async def test_allow_case_insensitive_root(self, read_hook):
        """Windows 大小写不敏感：不同大小写的根内路径应放行。"""
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Read",
            "tool_input": {"file_path": str(root).upper()},
        })
        assert _decision(res) == "allow"

    @pytest.mark.asyncio
    async def test_deny_absolute_outside(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Read",
            "tool_input": {"file_path": "C:\\Windows\\system32\\config.sys"},
        })
        assert _decision(res) == "deny"

    @pytest.mark.asyncio
    async def test_deny_traversal(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Read",
            "tool_input": {"file_path": "../outside.txt"},
        })
        assert _decision(res) == "deny"

    @pytest.mark.asyncio
    async def test_deny_glob_pattern_traversal(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Glob",
            "tool_input": {"path": ".", "pattern": "../**/*.env"},
        })
        assert _decision(res) == "deny"

    @pytest.mark.asyncio
    async def test_deny_non_dict_tool_input(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Read", "tool_input": "not-a-dict",
        })
        assert _decision(res) == "deny"

    @pytest.mark.asyncio
    async def test_deny_missing_path(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Grep", "tool_input": {"pattern": "x"},
        })
        assert _decision(res) == "deny"

    @pytest.mark.asyncio
    async def test_deny_nul_byte(self, read_hook):
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Read",
            "tool_input": {"file_path": "sub\x00/../.."},
        })
        assert _decision(res) == "deny"

    @pytest.mark.asyncio
    async def test_passthrough_non_read_tool(self, read_hook):
        """非 READ_TOOL_PATH_FIELDS 工具直接放行（权限兜底在别处）。"""
        hook, root = read_hook
        res = await _run_hook(hook, {
            "tool_name": "Bash", "tool_input": {"command": "ls"},
        })
        assert res == {"continue": True}


# ---------- ClaudeSessionStore ----------


class TestSessionStore:
    @pytest.mark.asyncio
    async def test_create_then_lookup(self, tmp_path: Path):
        from harness.claude_sdk import ClaudeSessionStore
        store = ClaudeSessionStore(path=tmp_path / "sessions.json")
        sid1, is_new = await store.lookup_or_create("agentops-1", "E:\\ws")
        assert is_new and sid1
        # 复用（同 cwd）
        sid2, is_new2 = await store.lookup_or_create("agentops-1", "E:\\ws")
        assert not is_new2 and sid2 == sid1

    @pytest.mark.asyncio
    async def test_cwd_change_creates_new(self, tmp_path: Path):
        from harness.claude_sdk import ClaudeSessionStore
        store = ClaudeSessionStore(path=tmp_path / "sessions.json")
        sid1, _ = await store.lookup_or_create("s", "E:\\ws1")
        sid2, is_new2 = await store.lookup_or_create("s", "E:\\ws2")
        assert is_new2 and sid2 != sid1

    @pytest.mark.asyncio
    async def test_persistence_across_instances(self, tmp_path: Path):
        from harness.claude_sdk import ClaudeSessionStore
        p = tmp_path / "sessions.json"
        s1, _ = await ClaudeSessionStore(path=p).lookup_or_create("s", "E:\\ws")
        # 新实例（模拟服务重启）应能读到同一映射
        s2, is_new = await ClaudeSessionStore(path=p).lookup_or_create("s", "E:\\ws")
        assert not is_new and s2 == s1


# ---------- _resolve_model_name ----------


class TestResolveModelName:
    def test_anthropic_prefixed(self):
        from harness.claude_sdk import _resolve_model_name

        class _Ctx:
            model = "anthropic/claude-sonnet-4-5"

        assert _resolve_model_name(_Ctx()) == "claude-sonnet-4-5"

    def test_non_anthropic_returns_empty(self):
        from harness.claude_sdk import _resolve_model_name

        class _Ctx:
            model = "minimax/MiniMax-M3"

        assert _resolve_model_name(_Ctx()) == ""

    def test_bare_claude_name(self):
        from harness.claude_sdk import _resolve_model_name

        class _Ctx:
            model = "claude-sonnet-4-20250514"

        assert _resolve_model_name(_Ctx()) == "claude-sonnet-4-20250514"


# ---------- _map_message ----------


def _mk_assistant(blocks: list[Any]) -> Any:
    from claude_agent_sdk import AssistantMessage
    return AssistantMessage(content=blocks, model="test")


class TestMapMessage:
    def _client(self):
        from harness.claude_sdk import ClaudeSdkAgentClient
        return ClaudeSdkAgentClient()

    def test_text_block(self):
        from claude_agent_sdk import TextBlock
        from harness.protocol import AgentEventType

        evs = self._client()._map_message(
            _mk_assistant([TextBlock(text="你好")])
        )
        assert len(evs) == 1
        assert evs[0].type == AgentEventType.TEXT and evs[0].text == "你好"

    def test_thinking_block(self):
        from claude_agent_sdk import ThinkingBlock
        from harness.protocol import AgentEventType

        evs = self._client()._map_message(
            _mk_assistant([ThinkingBlock(thinking="思考中", signature="sig")])
        )
        assert evs[0].type == AgentEventType.THINKING and evs[0].text == "思考中"

    def test_tool_use_block(self):
        from claude_agent_sdk import ToolUseBlock
        from harness.protocol import AgentEventType

        evs = self._client()._map_message(
            _mk_assistant([ToolUseBlock(
                id="tu_1", name="present_content",
                input={"title": "t"},
            )])
        )
        assert evs[0].type == AgentEventType.TOOL_USE
        assert evs[0].tool_name == "present_content"
        assert evs[0].tool_use_id == "tu_1"

    def test_user_message_tool_result(self):
        from claude_agent_sdk import ToolResultBlock, UserMessage
        from harness.protocol import AgentEventType

        msg = UserMessage(content=[ToolResultBlock(
            tool_use_id="tu_1",
            content=[{"type": "text", "text": "ok"}], is_error=False,
        )])
        evs = self._client()._map_message(msg)
        assert evs[0].type == AgentEventType.TOOL_RESULT
        assert evs[0].tool_result == "ok" and not evs[0].tool_is_error

    def test_user_message_tool_result_error(self):
        from claude_agent_sdk import ToolResultBlock, UserMessage
        from harness.protocol import AgentEventType

        msg = UserMessage(content=[ToolResultBlock(
            tool_use_id="tu_2",
            content="boom", is_error=True,
        )])
        evs = self._client()._map_message(msg)
        assert evs[0].type == AgentEventType.TOOL_RESULT and evs[0].tool_is_error

    def test_result_message_yields_usage_and_done(self):
        from claude_agent_sdk import ResultMessage
        from harness.protocol import AgentEventType

        msg = ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="sess-1",
            usage={"input_tokens": 10, "output_tokens": 5}, result="done-text",
        )
        evs = self._client()._map_message(msg)
        types = [e.type for e in evs]
        assert types == [AgentEventType.USAGE, AgentEventType.DONE]
        assert evs[0].usage.input_tokens == 10 and evs[0].usage.output_tokens == 5
        assert evs[1].text == "done-text"


# ---------- build_mcp_server / _wrap_tool_handler ----------


class TestMcpTools:
    def _tool(self, name: str, handler=None):
        from harness.protocol import ToolDefinition
        return ToolDefinition(
            name=name, description=f"{name} tool",
            input_schema={"type": "object", "properties": {}},
            handler=handler,
        )

    def _ctx(self):
        from harness.protocol import AgentRunContext
        return AgentRunContext(
            system_prompt="", model="", api_key="", base_url="",
            session_id="s", workspace="E:\\ws",
        )

    def test_skips_handler_none_and_builtin_conflict(self):
        from harness.claude_sdk import build_mcp_server

        tools = [
            self._tool("Bash"),           # handler=None → 跳过
            self._tool("Read", handler=lambda a: a),  # 内置重名 → 跳过
            self._tool("present_content", handler=lambda a: {"ok": True}),
        ]
        server = build_mcp_server(tools, self._ctx())
        assert server is not None

    def test_returns_none_when_no_tools(self):
        from harness.claude_sdk import build_mcp_server
        assert build_mcp_server([self._tool("Bash")], self._ctx()) is None

    @pytest.mark.asyncio
    async def test_wrap_handler_dict_result(self):
        from harness.claude_sdk import _wrap_tool_handler

        tdef = self._tool("present_content", handler=lambda a: {"ok": 1})
        wrapped = _wrap_tool_handler(tdef, self._ctx())
        res = await wrapped({})
        text = res["content"][0]["text"]
        assert json.loads(text) == {"ok": 1}
        assert not res.get("is_error")

    @pytest.mark.asyncio
    async def test_wrap_handler_exception(self):
        from harness.claude_sdk import _wrap_tool_handler

        def boom(_):
            raise RuntimeError("炸了")

        tdef = self._tool("boom", handler=boom)
        res = await _wrap_tool_handler(tdef, self._ctx())({})
        assert res.get("is_error") and "炸了" in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_wrap_handler_permission_error(self):
        from harness.claude_sdk import _wrap_tool_handler

        def denied(_):
            raise PermissionError("tier 不足")

        tdef = self._tool("sql_query", handler=denied)
        res = await _wrap_tool_handler(tdef, self._ctx())({})
        assert res.get("is_error") and "拒绝" in res["content"][0]["text"]


# ---------- run() cwd fail-fast ----------


class TestCwdFailFast:
    @pytest.mark.asyncio
    async def test_empty_workspace_yields_error(self):
        from harness.protocol import AgentEventType, AgentRunContext
        from harness.claude_sdk import ClaudeSdkAgentClient

        ctx = AgentRunContext(
            system_prompt="", model="", api_key="", base_url="",
            session_id="s", workspace="   ",
        )
        events = []
        async for ev in ClaudeSdkAgentClient().run("hi", [], ctx):
            events.append(ev)
        assert events[0].type == AgentEventType.ERROR
        assert "workspace" in events[0].error_message
        assert events[-1].type == AgentEventType.DONE
