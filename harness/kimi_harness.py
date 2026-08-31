"""
Kimi Harness — 包装 kimi CLI（Moonshot AI）作为 Harness。

定位：可选 harness，无 fallback。kimi 二进制找不到时返回 ERROR。
docs/DESIGN_harness_and_multi_agent.md 明确标记 "KIMI 不可用"。

注意：当前 config/agents/ 中无 agent 使用 `harness: kimi`，仅注册保留。
      若长期不用可考虑删除（参见冗余分析报告）。

参考: harness/register.py, harness/protocol.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import AsyncIterator

from .protocol import (
    AgentClient,
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    AgentUsage,
    HarnessType,
    ToolDefinition,
    assert_protocol_compatible,
)

logger = logging.getLogger(__name__)


class KimiHarness(AgentClient):
    """包装 kimi CLI 的 AgentClient 实现。

    harness_type=KIMI。
    通过 subprocess 调用 kimi CLI，解析 JSON 输出（含 usage）。
    """

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.KIMI

    def __init__(self, kimi_bin: str = "kimi", timeout: float = 180.0):
        """初始化 kimi 客户端。

        Args:
            kimi_bin: kimi 二进制路径或可执行名。若不在 PATH，按候选目录查找。
            timeout: 子进程超时秒数，默认 180。
        """
        self.kimi_bin = kimi_bin
        self.timeout = timeout
        if not shutil.which(self.kimi_bin):
            for candidate in [
                "/d/Program Files/nodejs/node_global/kimi",
                "C:/Program Files/nodejs/node_global/kimi",
                os.path.expanduser("~/.npm-global/bin/kimi"),
            ]:
                if os.path.exists(candidate):
                    self.kimi_bin = candidate
                    break

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """调用 kimi CLI 执行一次对话。

        Args:
            prompt: 用户 prompt 文本。
            tools: 可用工具列表（kimi CLI 自行管理工具执行，本 harness 不直接调 handler）。
            context: 运行时上下文（取 model / api_key / base_url 写入环境变量）。

        Yields:
            AgentEvent: THINKING / TEXT / USAGE / DONE。
            二进制找不到 / 超时 / 返回码非 0 时 yield ERROR + DONE。
        """
        assert_protocol_compatible(HarnessType.KIMI, context.protocol or "openai_compatible")
        if not self.kimi_bin or not os.path.exists(self.kimi_bin):
            logger.error("kimi 二进制未找到: %s", self.kimi_bin)
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=f"kimi binary not found: {self.kimi_bin}",
            )
            yield AgentEvent(type=AgentEventType.DONE)
            return

        # kimi CLI: `kimi --print --output-format json "prompt"`
        cmd = [self.kimi_bin, "--print", "--output-format", "json", prompt]
        env = os.environ.copy()
        if context.api_key:
            env["KIMI_API_KEY"] = context.api_key
        if context.base_url:
            env["KIMI_MODEL_BASE_URL"] = context.base_url
        if context.model:
            env["KIMI_MODEL"] = context.model

        logger.info("Kimi 调用 bin=%s model=%s session=%s", self.kimi_bin, context.model, context.session_id)
        yield AgentEvent(type=AgentEventType.THINKING, text=f"[kimi] invoking: {self.kimi_bin}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                logger.warning("Kimi 子进程超时 timeout=%ss，kill", self.timeout)
                proc.kill()
                yield AgentEvent(type=AgentEventType.ERROR, error_message="kimi timeout")
                yield AgentEvent(type=AgentEventType.DONE)
                return

            if proc.returncode != 0:
                err_text = stderr.decode("utf-8", errors="replace")[:500]
                logger.error("Kimi 返回码非 0 rc=%s stderr=%s", proc.returncode, err_text)
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error_message=f"kimi failed (rc={proc.returncode}): {err_text}",
                )
                yield AgentEvent(type=AgentEventType.DONE)
                return

            raw = stdout.decode("utf-8", errors="replace").strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Kimi 输出非 JSON，按纯文本处理 size=%d", len(raw))
                data = {"text": raw}

            text = data.get("text") or data.get("result") or raw
            yield AgentEvent(type=AgentEventType.TEXT, text=text)

            usage = data.get("usage", {}) or {}
            logger.info(
                "Kimi 完成 tokens_in=%d tokens_out=%d session=%s",
                usage.get("input_tokens", 0), usage.get("output_tokens", 0), context.session_id,
            )
            yield AgentEvent(
                type=AgentEventType.USAGE,
                usage=AgentUsage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                ),
            )
            yield AgentEvent(
                type=AgentEventType.DONE,
                usage=AgentUsage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                ),
            )
        except Exception as e:
            logger.exception("Kimi harness 异常 session=%s", context.session_id)
            yield AgentEvent(type=AgentEventType.ERROR, error_message=f"kimi harness error: {e}")
            yield AgentEvent(type=AgentEventType.DONE)
