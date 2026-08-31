"""
opencode Harness — calls the opencode server via HTTP API.

连接 opencode HTTP server（默认 http://127.0.0.1:4096），通过
POST /session 创建 session，POST /session/{id}/prompt_async
异步发送 prompt，GET /event 订阅全局 SSE 事件流，按 sessionID 过滤
事件并转换为 AgentEvent。

注意：路由路径不带 /api 前缀（v2 API 路径为 /session/...，而非 /api/session/...）。
带 /api 前缀的路径是 v1 兼容路由，只有部分端点支持，会被 SPA fallback 拦截。

Dependencies:
    - opencode server running (opencode serve, default http://127.0.0.1:4096)
    - httpx

Protocol:
    AgentClient (see harness/protocol.py).

SSE 事件格式（标准 text/event-stream）:
    data: {"type": "message.part.updated", "properties": {"part": {...}}, "id": "..."}
    data: {"type": "session.status", "properties": {"sessionID": "...", "status": {"type": "idle"}}}
    data: {"type": "session.error", "properties": {"sessionID": "...", "error": {...}}}

关键事件类型:
    - message.part.updated  — part 更新（text/reasoning/tool/step-start/step-finish）
    - session.status        — session 状态变化（idle 表示完成）
    - session.error         — 错误
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator, Optional
from urllib.parse import quote

import httpx

from .protocol import (
    AgentClient,
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    AgentUsage,
    HarnessRegistry,
    HarnessType,
    ToolDefinition,
    assert_protocol_compatible,
)

logger = logging.getLogger(__name__)


def _default_base_url() -> str:
    host = os.environ.get("OPENCODE_HOST", "127.0.0.1")
    port = os.environ.get("OPENCODE_PORT", "4096")
    return f"http://{host}:{port}"


class OpencodeHarness(AgentClient):
    """Drives the opencode server for one agent run.

    通过 HTTP API 连接 opencode server，使用 prompt_async 异步发送
    prompt，通过 SSE 事件流接收 agent 回复。opencode 内部 agent loop
    原生处理工具调用 (Bash/Read/Edit/...)，harness 只负责事件转发。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 300.0,
        wait_timeout: float = 240.0,
        directory: Optional[str] = None,
    ):
        self.base_url = (base_url or _default_base_url()).rstrip("/")
        self.timeout = timeout
        self.wait_timeout = wait_timeout
        self.directory = directory

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.OPENCODE

    # ---------- AgentClient.run ----------

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        assert_protocol_compatible(HarnessType.OPENCODE, context.protocol or "openai_compatible")
        model = context.model or None
        directory = self.directory or context.workspace or None
        system_prompt = context.system_prompt or ""

        # 构建 model 参数（PromptPayload 的 ModelRef 用 {providerID, modelID}）
        model_ref = None
        if model and "/" in model:
            provider_id, model_id = model.split("/", 1)
            model_ref = {"providerID": provider_id, "modelID": model_id}

        headers = {"Content-Type": "application/json"}
        if directory:
            # SDK 用 encodeURIComponent 编码 directory header
            headers["x-opencode-directory"] = quote(directory, safe="")

        # SSE 是长连接，read 不设超时
        timeout = httpx.Timeout(None, connect=10.0)

        usage = AgentUsage()
        session_id: str | None = None

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=timeout, headers=headers
            ) as client:
                # 1. 创建 session
                resp = await client.post("/session", json={})
                resp.raise_for_status()
                session_data = resp.json()
                session_id = (
                    session_data.get("data", {}).get("id")
                    or session_data.get("id")
                )
                if not session_id:
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        error_message=f"Failed to create session: {session_data}",
                    )
                    yield AgentEvent(type=AgentEventType.DONE, usage=usage)
                    return

                # 2. 构建 prompt body
                prompt_body: dict = {
                    "parts": [{"type": "text", "text": prompt}],
                }
                if model_ref:
                    prompt_body["model"] = model_ref
                if system_prompt:
                    prompt_body["system"] = system_prompt

                # 3. 订阅 SSE + 发送 prompt_async
                try:
                    async with client.stream("GET", "/event") as sse:
                        # SSE 连接已建立，发送 prompt_async（httpx 连接池支持并发请求）
                        resp = await client.post(
                            f"/session/{session_id}/prompt_async",
                            json=prompt_body,
                        )
                        resp.raise_for_status()

                        # 4. 读取 SSE 事件
                        async for line in sse.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if not data_str.strip():
                                continue
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            etype = event.get("type")
                            props = event.get("properties", {})

                            # session.idle → 完成，退出 SSE 循环
                            if etype == "session.status":
                                sid = props.get("sessionID")
                                if sid and sid != session_id:
                                    continue
                                status = props.get("status", {})
                                if status.get("type") == "idle":
                                    break
                                continue

                            # 其他事件转换为 AgentEvent
                            async for ae in self._translate_event(
                                event, session_id
                            ):
                                yield ae
                                if (
                                    ae.type == AgentEventType.USAGE
                                    and ae.usage
                                ):
                                    usage = ae.usage

                except httpx.HTTPStatusError as e:
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        error_message=f"HTTP {e.response.status_code}: {e.response.text[:300]}",
                    )

        except httpx.ConnectError as e:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=f"无法连接 opencode server ({self.base_url}): {e}",
            )
        except asyncio.CancelledError:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message="opencode harness cancelled",
            )
            yield AgentEvent(type=AgentEventType.DONE, usage=usage)
            raise
        except Exception as e:
            logger.exception("opencode harness failed")
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=f"opencode harness error: {type(e).__name__}: {e}",
            )

        yield AgentEvent(type=AgentEventType.DONE, usage=usage)

    async def _translate_event(
        self, event: dict, session_id: str
    ) -> AsyncIterator[AgentEvent]:
        """将 opencode SSE 事件转换为 AgentEvent，过滤非当前 session。

        SSE 事件结构: {type: "...", properties: {...}, id: "..."}
        sessionID 可能出现在 properties.sessionID 或 properties.part.sessionID。
        """
        etype = event.get("type")
        props = event.get("properties", {})

        # 过滤非当前 session 的事件
        sid = props.get("sessionID") or props.get("part", {}).get("sessionID")
        if sid and sid != session_id:
            return

        if etype == "message.part.updated":
            part = props.get("part", {})
            part_type = part.get("type")

            if part_type == "text" and part.get("time", {}).get("end"):
                txt = part.get("text", "")
                if txt:
                    yield AgentEvent(
                        type=AgentEventType.TEXT, text=txt, raw=part
                    )

            elif part_type == "reasoning" and part.get("time", {}).get("end"):
                txt = part.get("text", "")
                if txt:
                    yield AgentEvent(
                        type=AgentEventType.THINKING, text=txt, raw=part
                    )

            elif part_type == "tool":
                state = part.get("state", {})
                status = state.get("status")
                tool_name = part.get("tool", "")
                call_id = part.get("callID", part.get("id", ""))
                tool_input = state.get("input", {})
                yield AgentEvent(
                    type=AgentEventType.TOOL_USE,
                    tool_use_id=call_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    raw=part,
                )
                if status == "completed":
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        tool_use_id=call_id,
                        tool_name=tool_name,
                        tool_result=state.get("output", ""),
                        raw=part,
                    )
                elif status == "error":
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        tool_use_id=call_id,
                        tool_name=tool_name,
                        tool_result=state.get("error", ""),
                        tool_is_error=True,
                        raw=part,
                    )

            elif part_type == "step-finish":
                tokens = part.get("tokens", {})
                if isinstance(tokens, dict):
                    turn_usage = AgentUsage(
                        input_tokens=int(tokens.get("input", 0)),
                        output_tokens=int(tokens.get("output", 0)),
                    )
                    cache = tokens.get("cache", {})
                    if isinstance(cache, dict):
                        turn_usage.cache_read_tokens = int(
                            cache.get("read", 0)
                        )
                        turn_usage.cache_creation_tokens = int(
                            cache.get("write", 0)
                        )
                    if (
                        turn_usage.input_tokens
                        or turn_usage.output_tokens
                    ):
                        yield AgentEvent(
                            type=AgentEventType.USAGE,
                            usage=turn_usage,
                            raw=part,
                        )
                yield AgentEvent(
                    type=AgentEventType.TURN_COMPLETE,
                    turn_number=1,
                    raw=part,
                )

        elif etype == "session.error":
            error = props.get("error", {})
            if isinstance(error, dict):
                data = error.get("data", {})
                if isinstance(data, dict) and data.get("message"):
                    error_msg = data["message"]
                else:
                    error_msg = error.get("message", str(error))
            else:
                error_msg = str(error)
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=error_msg,
                raw=props,
            )


# ---------- auto-register on import ----------

HarnessRegistry.register(HarnessType.OPENCODE, OpencodeHarness)
