"""Phase 0 冒烟：验证 claude-agent-sdk 在 AgentOps 环境（Windows + asyncio）可用。

用法:
  python scripts/smoke_claude_sdk.py          # Level 1: 离线构造校验（不需要登录/网络）
  python scripts/smoke_claude_sdk.py --live   # Level 2: 真实调用一次 ClaudeSDKClient

Level 1 验证点:
  - starlette 0.35.1 降级后 mcp/claude_agent_sdk 可正常 import
  - create_sdk_mcp_server + tool() 注册 in-process MCP 工具
  - ClaudeAgentOptions 构造（setting_sources / hooks / mcp_servers / cwd）
  - PreToolUse hook 的输入输出格式

Level 2 验证点（依赖本机 claude 登录态或 ANTHROPIC_API_KEY）:
  - asyncio 事件循环内 ClaudeSDKClient 真实可用（anyio 兼容性）
  - 结构化消息（AssistantMessage/TextBlock/ResultMessage）流式返回
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def level1() -> None:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
        create_sdk_mcp_server,
        tool,
    )

    # in-process MCP 工具（JSON Schema dict 形式，对齐 ToolDefinition.input_schema）
    @tool(
        "echo",
        "Echo back the input text",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    async def echo(args):
        return {"content": [{"type": "text", "text": args["text"]}]}

    mcp = create_sdk_mcp_server(name="smoke-tools", version="0.0.1", tools=[echo])

    # PreToolUse hook（对齐 workspace_policy 的输出格式）
    async def allow_hook(input_data, tool_use_id, context):
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            },
        }

    options = ClaudeAgentOptions(
        cwd=str(Path.cwd()),
        system_prompt="smoke test",
        setting_sources=[],               # SDK 隔离模式：不加载用户/项目记忆文件
        permission_mode="bypassPermissions",
        strict_mcp_config=True,
        mcp_servers={"smoke-tools": mcp},
        hooks={"PreToolUse": [HookMatcher(matcher="Read", hooks=[allow_hook])]},
        max_turns=1,
    )
    client = ClaudeSDKClient(options=options)   # 只构造，不 connect
    assert client is not None
    print("[L1] ok: import / mcp server / options / hook / ClaudeSDKClient 构造")


async def level2() -> None:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )
    from harness.claude_sdk import _resolve_cli_path, _auth_env

    class _Ctx:
        api_key = ""
        base_url = ""

    cli_path = _resolve_cli_path()
    auth_env = _auth_env(_Ctx())
    print(f"[L2] cli_path: {cli_path or '(SDK default)'}")
    print(f"[L2] auth_env keys: {sorted(auth_env.keys())}")
    options = ClaudeAgentOptions(
        cwd=str(Path.cwd()),
        setting_sources=[],
        max_turns=1,
        permission_mode="bypassPermissions",
        cli_path=cli_path,
        env=auth_env,  # settings.json env 节（MiniMax 代理认证）
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query("只回复一个字：好")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"[L2] text: {block.text[:80]}")
            elif isinstance(msg, ResultMessage):
                print(
                    f"[L2] result: {str(msg.result)[:80]!r} "
                    f"turns={msg.num_turns} err={msg.is_error} session={msg.session_id[:8]}..."
                )
                assert not msg.is_error, "ResultMessage.is_error=True"


if __name__ == "__main__":
    asyncio.run(level1())
    if "--live" in sys.argv:
        asyncio.run(level2())
    print("SMOKE OK")
