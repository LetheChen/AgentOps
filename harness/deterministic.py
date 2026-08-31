"""
Deterministic Harness — 回放预录制响应，不调用任何 LLM。

使用场景：
  - M0 benchmark baseline（无 LLM 成本）
  - 回归测试（golden fixtures）
  - 无 API Key 下的 Demo / 开发模式

工具执行：本地调 handler。若 handler 为 None，返回 "deterministic: no handler"，
保证 harness 不会意外调到外部 API。

参考: workflows/hello-world.yaml, tests/test_v0.py
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from .protocol import (
    AgentClient,
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    AgentUsage,
    HarnessType,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


class DeterministicClient(AgentClient):
    """回放预录制响应的 AgentClient（不调任何 LLM）。

    harness_type=DETERMINISTIC。
    流程：emit THINKING → 调第一个 tool 演示工具链路 → 调 finalize 让对话引擎结束 → emit TEXT/USAGE/DONE。
    """

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.DETERMINISTIC

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """执行一次 deterministic 响应（不调 LLM，仅本地工具演示）。

        Args:
            prompt: 用户 prompt 文本（仅用于回放展示，不发给任何 LLM）。
            tools: 可用工具列表。会调用 tools[0] 演示工具链路；若存在 finalize 工具则主动调用。
            context: 运行时上下文（仅取 workspace / session_id 用于输出展示）。

        Yields:
            AgentEvent: THINKING / TOOL_USE / TOOL_RESULT / TEXT / USAGE / TURN_COMPLETE / DONE。
        """
        logger.info(
            "Deterministic run session=%s tools=%d prompt_len=%d",
            context.session_id, len(tools), len(prompt),
        )
        # Emit "thinking" first
        yield AgentEvent(
            type=AgentEventType.THINKING,
            text=f"[deterministic] received prompt: {prompt[:100]}...",
        )

        # Simulate tool invocation — 调用第一个 tool 演示工具链路
        if tools:
            tool = tools[0]
            logger.debug("Deterministic 调用 tool=%s 演示工具链路", tool.name)
            yield AgentEvent(
                type=AgentEventType.TOOL_USE,
                tool_use_id="t1",
                tool_name=tool.name,
                tool_input={"prompt": prompt},
            )
            if tool.handler:
                result = await tool.handler({"prompt": prompt})
                content = result.get("content", "deterministic: no content")
            else:
                content = f"[deterministic] no handler for {tool.name}"
            yield AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                tool_use_id="t1",
                tool_result=content,
            )

        # 如果 tools 中存在 finalize，调用它让对话引擎正确结束
        finalize_tool = next((t for t in tools if t.name == "finalize"), None)
        if finalize_tool and finalize_tool.handler:
            logger.debug("Deterministic 主动调 finalize 结束对话引擎")
            yield AgentEvent(
                type=AgentEventType.TOOL_USE,
                tool_use_id="t_finalize",
                tool_name="finalize",
                tool_input={"summary": "deterministic finalize"},
            )
            await finalize_tool.handler({"summary": "deterministic finalize"})

        # Emit text (the "answer")
        answer = (
            f"[deterministic] processed '{prompt[:60]}' with "
            f"{len(tools)} tool(s), workspace={context.workspace}, "
            f"session={context.session_id}"
        )
        yield AgentEvent(type=AgentEventType.TEXT, text=answer)

        # Emit usage (zero cost deterministic)
        yield AgentEvent(
            type=AgentEventType.USAGE,
            usage=AgentUsage(input_tokens=len(prompt.split()), output_tokens=len(answer.split())),
        )

        yield AgentEvent(type=AgentEventType.TURN_COMPLETE, turn_number=1)
        yield AgentEvent(
            type=AgentEventType.DONE,
            usage=AgentUsage(input_tokens=len(prompt.split()), output_tokens=len(answer.split())),
        )
