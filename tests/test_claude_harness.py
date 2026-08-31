#!/usr/bin/env python
"""手动测试 Claude CLI Harness — 验证流式输出和工具调用。

用法：
  python tests/test_claude_harness.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.asyncio
async def test_simple_prompt() -> None:
    """简单对话测试。"""
    from harness.claude_code import ClaudeCodeClient
    from harness.protocol import AgentRunContext, AgentEventType

    client = ClaudeCodeClient(timeout=120.0)
    ctx = AgentRunContext(
        system_prompt="你是一个 helpful assistant。用一句话回答。用中文。",
        model="",
        api_key="",
        base_url="",
        workspace=os.getcwd(),
        session_id="test_claude_simple",
        protocol="anthropic_compatible",
        persist_session=False,
    )

    print("=== 简单对话测试 ===")
    events = []
    async for ev in client.run("1+1=?", [], ctx):
        events.append(ev)
        if ev.type == AgentEventType.TEXT and ev.text:
            print(f"[TEXT] {ev.text}", end="", flush=True)
        elif ev.type == AgentEventType.THINKING and ev.text:
            print(f"[THINK] {ev.text[:100]}...")
        elif ev.type == AgentEventType.ERROR:
            print(f"[ERROR] {ev.error_message}")
        elif ev.type == AgentEventType.USAGE and ev.usage:
            print(f"\n[USAGE] in={ev.usage.input_tokens} out={ev.usage.output_tokens}")
        elif ev.type == AgentEventType.DONE:
            print(f"\n[DONE] text_len={len(ev.text or '')}")

    types = [e.type for e in events]
    assert AgentEventType.TEXT in types, f"缺少 TEXT: {types}"
    assert AgentEventType.DONE in types, f"缺少 DONE: {types}"
    print("✅ 简单对话测试通过！")


@pytest.mark.asyncio
async def test_streaming_output() -> None:
    """流式输出测试 — 验证增量文本。"""
    from harness.claude_code import ClaudeCodeClient
    from harness.protocol import AgentRunContext, AgentEventType

    client = ClaudeCodeClient(timeout=120.0)
    ctx = AgentRunContext(
        system_prompt="你是一个 helpful assistant。请用中文回答。",
        model="",
        api_key="",
        base_url="",
        workspace=os.getcwd(),
        session_id="test_claude_stream",
        protocol="anthropic_compatible",
        persist_session=False,
    )

    print("\n=== 流式输出测试 ===")
    text_chunks = 0
    async for ev in client.run("列出 3 个 Python 最佳实践，用编号列表。", [], ctx):
        if ev.type == AgentEventType.TEXT and ev.text:
            text_chunks += 1
            print(ev.text, end="", flush=True)
        elif ev.type == AgentEventType.DONE:
            print(f"\n[DONE] chunks={text_chunks}")

    assert text_chunks > 0, "应有至少一个文本块"
    print(f"✅ 流式输出测试通过（{text_chunks} 个文本块）！")


@pytest.mark.asyncio
async def test_session_persistence() -> None:
    """Session 持久化测试 — 多轮对话。"""
    from harness.claude_code import ClaudeCodeClient
    from harness.protocol import AgentRunContext, AgentEventType

    client = ClaudeCodeClient(timeout=120.0)
    session_id = "test_claude_persist"

    ctx = AgentRunContext(
        system_prompt="你是一个 helpful assistant。记住用户说的话。用中文回答。",
        model="",
        api_key="",
        base_url="",
        workspace=os.getcwd(),
        session_id=session_id,
        protocol="anthropic_compatible",
        persist_session=True,
    )

    print("\n=== Session 持久化测试 ===")

    # 第一轮
    print("Round 1: 我叫张三")
    async for ev in client.run("我的名字叫张三，请记住。", [], ctx):
        if ev.type == AgentEventType.TEXT and ev.text:
            print(ev.text, end="", flush=True)
        elif ev.type == AgentEventType.DONE:
            print()

    # 第二轮
    print("\nRound 2: 我叫什么？")
    async for ev in client.run("我叫什么名字？", [], ctx):
        if ev.type == AgentEventType.TEXT and ev.text:
            print(ev.text, end="", flush=True)
        elif ev.type == AgentEventType.DONE:
            print()

    print("✅ Session 持久化测试完成！")


if __name__ == "__main__":
    # Windows 需要 ProactorEventLoop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(test_simple_prompt())
    asyncio.run(test_streaming_output())
    asyncio.run(test_session_persistence())