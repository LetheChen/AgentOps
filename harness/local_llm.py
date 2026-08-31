"""
Local LLM Harness — 直接通过 httpx 调用 OpenAI 兼容 Chat Completions API。

定位：M0 候选 C 实现，无 subagent 树、无 MCP，纯 LLM 调用，用作 baseline / fallback。
被 log-patrol workflow 的 scan/analyze/report/notify 4 个节点使用（harness: local_llm）。

支持协议：OpenAI-compatible（适用于 DeepSeek / MiniMax / OpenAI 等大多数 provider）。

核心机制：
  - 多轮工具调用循环（max_rounds=8）：LLM 返回 tool_calls → 执行 handler →
    回灌 {role:"tool", tool_call_id, content} 到 messages → 再次调 LLM，
    直到 LLM 不再返回 tool_calls。
  - 累加每轮 usage（prompt_tokens / completion_tokens），最终 emit USAGE + DONE。

参考: config/agents/log_analyst.yaml, config/models.yaml
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

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


class LocalLlmClient(AgentClient):
    """通过 OpenAI 兼容 Chat Completions API 调用 LLM 的 AgentClient 实现。

    harness_type=LOCAL_LLM（H1: 独立槽位，不再偷占 OPENCODE）。
    构造参数优先于 AgentRunContext（支持无参构造 + 运行时注入）。
    """

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.LOCAL_LLM  # H1: 独立槽位，不再偷占 OPENCODE

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", timeout: float = 120.0):
        """初始化 local_llm 客户端。

        Args:
            base_url: provider 基础 URL（如 https://api.deepseek.com/v1）。空时用 context.base_url。
            api_key: API Key。空时用 context.api_key。
            model: 模型 ID（如 deepseek-v4-flash）。空时用 context.model。
            timeout: 单次 HTTP 请求超时秒数，默认 120。
                原 60s 在多轮工具调用场景下偏短（如 content_curator evaluate 节点
                多 draft 评估时单轮请求可能 60-90s），D-030 提到 120s。
        """
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """执行一次 LLM 对话（含多轮工具调用循环）。

        流程：
          1. 校验协议兼容性（LOCAL_LLM 要求 openai_compatible）
          2. 解析 base_url / api_key / model（构造参数优先，fallback 到 context）
          3. 构造 OpenAI tools schema + 初始 messages（system + user）
          4. 循环 max_rounds=8 轮：POST /chat/completions → 解析 → 执行 tool_calls → 回灌
          5. emit USAGE / TURN_COMPLETE / DONE

        Args:
            prompt: 用户 prompt 文本。
            tools: 可用工具列表（ToolDefinition，含 handler）。
            context: 运行时上下文（含 system_prompt / api_key / model / workspace 等）。

        Yields:
            AgentEvent: 按顺序 THINKING / TEXT / TOOL_USE / TOOL_RESULT / USAGE / TURN_COMPLETE / DONE。
            出错时 yield ERROR + DONE 提前结束。

        Raises:
            无（异常都通过 AgentEvent.ERROR 上报，不抛出，保证 caller 能正常收尾）。
        """
        assert_protocol_compatible(HarnessType.LOCAL_LLM, context.protocol or "openai_compatible")
        # H1: 优先用构造参数，fallback 到 AgentRunContext（支持无参构造 + 运行时注入）
        base_url = self.base_url or context.base_url
        api_key = self.api_key or context.api_key
        # model 兜底去 provider 前缀：部分调用方（如 server.py 早期版本）可能传
        # "minimax/MiniMax-M3" 格式，minimax chat/completions API 不接受带前缀的 model ID
        # 会返回 400 Bad Request（D-031 防御）。与 codex_appserver._normalize_model_id 同义
        raw_model = self.model or context.model
        model = raw_model.split("/", 1)[1] if "/" in raw_model else raw_model
        if not base_url:
            logger.error("LocalLlmClient.run base_url 未配置（session=%s）", context.session_id)
            yield AgentEvent(type=AgentEventType.ERROR, error_message="LocalLlmClient: base_url 未配置（既无构造参数也无 context.base_url）")
            yield AgentEvent(type=AgentEventType.DONE)
            return
        url = f"{base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        logger.info(
            "LocalLlm 调用开始 model=%s url=%s tools=%d session=%s",
            model, url, len(tools), context.session_id,
        )

        # Build OpenAI-format tool schemas
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

        # 多轮对话 messages（支持工具调用循环：LLM 返回 tool_calls → 执行 → 回灌 → 再调 LLM）
        messages: list[dict] = [
            {"role": "system", "content": context.system_prompt},
            {"role": "user", "content": prompt},
        ]

        total_input_tokens = 0
        total_output_tokens = 0
        max_rounds = 8  # 防无限循环

        for round_idx in range(max_rounds):
            body: dict = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if oai_tools:
                body["tools"] = oai_tools

            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as e:
                logger.exception("LocalLlm HTTP 调用失败 round=%d url=%s", round_idx, url)
                yield AgentEvent(type=AgentEventType.ERROR, error_message=f"LLM HTTP error: {e}")
                yield AgentEvent(type=AgentEventType.TURN_COMPLETE, turn_number=round_idx)
                yield AgentEvent(type=AgentEventType.DONE)
                return

            # Parse OpenAI non-streaming response
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            # 累加 usage
            usage_data = data.get("usage", {})
            total_input_tokens += usage_data.get("prompt_tokens", 0)
            total_output_tokens += usage_data.get("completion_tokens", 0)

            if content:
                logger.debug("LocalLlm round=%d 返回文本 %d 字符", round_idx, len(content))
                yield AgentEvent(type=AgentEventType.TEXT, text=content)

            # 无 tool_calls → 对话结束
            if not tool_calls:
                logger.info(
                    "LocalLlm 对话结束 round=%d tokens_in=%d tokens_out=%d session=%s",
                    round_idx, total_input_tokens, total_output_tokens, context.session_id,
                )
                break

            # 把 assistant message（含 tool_calls）加入 messages 供下一轮回灌
            # ⚠️ 必须清洗：MiniMax 等国产 LLM 响应含 reasoning_content 等非标准字段，
            # 原样回灌会导致下一轮 400 Bad Request。只保留 OpenAI 标准字段。
            clean_tool_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                clean_tool_calls.append({
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    },
                })
            messages.append({
                "role": "assistant",
                # MiniMax/OpenAI 规范：有 tool_calls 时 content 必须为 null（不能是 ""），
                # 否则下一轮请求 400 Bad Request
                "content": content if content else None,
                "tool_calls": clean_tool_calls,
            })
            logger.info(
                "LocalLlm round=%d 收到 %d 个 tool_calls: %s",
                round_idx, len(tool_calls), [tc.get("function", {}).get("name") for tc in tool_calls],
            )

            # 执行 tool_calls
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name")
                try:
                    tool_input = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    logger.warning("LocalLlm tool %s 参数 JSON 解析失败，回退空 dict", tool_name)
                    tool_input = {}

                yield AgentEvent(
                    type=AgentEventType.TOOL_USE,
                    tool_use_id=tc.get("id"),
                    tool_name=tool_name,
                    tool_input=tool_input,
                )

                # 执行 handler
                handler = next((t.handler for t in tools if t.name == tool_name), None)
                if handler:
                    try:
                        # 兼容 sync / async 两种 handler：
                        # sync handler 直接返回 dict（如 pull_logs/query_logs），await 会抛
                        # "object dict can't be used in 'await' expression"
                        raw_result = handler(tool_input)
                        result = await raw_result if asyncio.iscoroutine(raw_result) else raw_result
                        tool_result_str = json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result.get("content", ""))
                        logger.debug("LocalLlm tool=%s 执行成功 result_size=%d", tool_name, len(tool_result_str))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            tool_use_id=tc.get("id"),
                            tool_result=tool_result_str,
                        )
                        # 回灌 tool result 到 messages（供下一轮 LLM 调用）
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "content": tool_result_str,
                        })
                    except Exception as e:
                        logger.exception("LocalLlm tool=%s handler 执行异常", tool_name)
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            tool_use_id=tc.get("id"),
                            tool_result=f"error: {e}",
                            tool_is_error=True,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "content": f"error: {e}",
                        })
                else:
                    logger.warning("LocalLlm tool=%s 无注册 handler", tool_name)
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        tool_use_id=tc.get("id"),
                        tool_result=f"no handler for {tool_name}",
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": f"no handler for {tool_name}",
                    })
        else:
            # 达到 max_rounds 仍未结束 — 强制一回合，注入「必须现在回答」的指令
            # 很多 LLM 会把 read_file 用满 max_rounds，浪费最后一轮才得到答案。
            # 这里我们发一条 user 消息明确要求它立刻基于现有对话历史综合答案，
            # 不再追加新的 read_file 调用。这一轮再无 TEXT 输出才真正放弃。
            logger.warning(
                "LocalLlm 达到最大工具调用轮数 %d，注入收尾指令 session=%s",
                max_rounds, context.session_id,
            )
            messages.append({
                "role": "user",
                "content": (
                    "【系统指令】已经达到工具调用上限。立即停止 read_file 调用，"
                    "基于上面已经读到的所有文档内容直接生成最终答案（如未读到任何相关文档，"
                    "明确说明「知识库中未找到相关内容」）。不要再调用任何工具。"
                ),
            })
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    # 收尾轮用 tool_choice=none + tools=[] 双保险禁止 LLM 再调用工具
                    final_body = {**body, "messages": messages, "tools": [], "tool_choice": "none"}
                    resp = await client.post(
                        url, headers=headers, json=final_body,
                    )
                    resp.raise_for_status()
                    final = resp.json()
                    final_choice = final.get("choices", [{}])[0]
                    final_msg = final_choice.get("message", {})
                    final_content = final_msg.get("content") or ""
                    if final_content:
                        yield AgentEvent(type=AgentEventType.TEXT, text=final_content)
                    else:
                        yield AgentEvent(type=AgentEventType.TEXT, text=f"[local_llm] 达到最大工具调用轮数 {max_rounds}，强制结束")
                    final_usage = final.get("usage", {}) or {}
                    total_input_tokens += int(final_usage.get("prompt_tokens", 0) or 0)
                    total_output_tokens += int(final_usage.get("completion_tokens", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LocalLlm 收尾轮次失败: %s", exc)
                yield AgentEvent(type=AgentEventType.TEXT, text=f"[local_llm] 达到最大工具调用轮数 {max_rounds}，强制结束")

        yield AgentEvent(
            type=AgentEventType.USAGE,
            usage=AgentUsage(
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            ),
        )

        yield AgentEvent(type=AgentEventType.TURN_COMPLETE, turn_number=1)
        yield AgentEvent(
            type=AgentEventType.DONE,
            usage=AgentUsage(
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            ),
        )
