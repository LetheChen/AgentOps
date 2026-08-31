"""
Claude Harness — 直接调用 Claude CLI 子进程。

通过 `claude --print --output-format stream-json --verbose` 实现流式 Agent 对话。
系统提示词通过临时 settings JSON 文件传递（claudeMd 字段），
避免命令行参数长度限制和 Windows 编码问题。

DAG 工具通过 system_prompt 注入（<tool_call> 文本标记），由 SessionEngine 的
`_extract_and_run_tool_calls` 统一解析和执行。

架构：
  Python (harness/claude_code.py)
    ├─ 写 temp settings JSON（含 system_prompt 作为 claudeMd）
    ├─ Spawn: cmd.exe /c claude --print --settings <tmp.json> --permission-mode bypassPermissions "用户消息"
    ├─ 解析 stdout JSON lines → AgentEvent
    └─ 清理子进程 + 临时文件
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

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

# 内存缓存：session_id -> native session UUID
_native_sessions: dict[str, str] = {}

# credential redaction
_SECRET_KEYS = frozenset({
    "apiKey", "api_key", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "Authorization", "auth_token", "secret", "token",
})


def _resolve_claude_bin() -> str:
    """解析 claude 二进制路径。Windows 上返回 basename，通过 cmd.exe PATH 查找。"""
    for env_var in ("CLAUDE_BIN",):
        path = os.environ.get(env_var, "")
        if path and os.path.exists(path):
            lower = path.lower()
            if lower.endswith(".exe"):
                return path
            return os.path.basename(path)
    found = shutil.which("claude")
    if found:
        return os.path.basename(found)
    return "claude.CMD"


def _needs_cmd_wrapper(path: str) -> bool:
    """Windows 上需要 cmd.exe 解释。"""
    if os.name != "nt":
        return False
    lower = path.lower()
    return lower.endswith(".cmd") or lower.endswith(".bat") or "." not in os.path.basename(path)


def _redact(obj: dict[str, Any]) -> dict[str, Any]:
    """抹除敏感字段。"""
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if any(sk.lower() in k.lower() for sk in _SECRET_KEYS):
            out[k] = "[REDACTED]"
        elif isinstance(v, str):
            out[k] = _redact_string(v)
        else:
            out[k] = v
    return out


def _redact_string(s: str) -> str:
    """抹除字符串中的 bearer token / API key 模式。"""
    s = re.sub(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED]", s)
    return s


class ClaudeCodeClient(AgentClient):
    """Claude harness：CLI 子进程 + stream-json 输出 + temp settings 文件。

    harness_type=CLAUDE_CODE。
    每次 run() 对应一次 claude --print 调用：
      1. 写 temp settings JSON（claudeMd = system_prompt）
      2. 构建命令行：cmd.exe /c claude --settings <tmp.json> ... "用户消息"
      3. 解析 stdout JSON lines → AgentEvent
      4. 清理
    """

    def __init__(self, timeout: float = 300.0):
        self.timeout = timeout

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.CLAUDE_CODE

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """执行一次 Agent 对话（真流式）。

        Yields:
            AgentEvent: TEXT / THINKING / TOOL_USE / TOOL_RESULT / USAGE / ERROR / DONE。

        流程：
          1. 写 temp settings JSON（系统提示词作为 claudeMd）
          2. asyncio.create_subprocess_exec 启动 claude 子进程（不用 shell=True）
          3. 后台协程 drain stderr（capped 12KB）
          4. 主循环 await proc.stdout.readline() 逐行解析 → 逐 event yield
          5. EOF 后 proc.wait() 检查 returncode，yield USAGE/DONE

        安全：抛弃 shell=True + list2cmdline，改为显式 cmd.exe argv，避免 shell 注入。
        超时：单行 idle 30s（继续等）；累计 self.timeout 强制 kill。
        """
        assert_protocol_compatible(
            HarnessType.CLAUDE_CODE, context.protocol or "anthropic_compatible"
        )

        # cwd fail-fast：workspace 必须显式（配置缺失应暴露，不默默跑错目录）
        workspace = (context.workspace or "").strip()
        if not workspace:
            logger.error(
                "claude_code harness requires explicit context.workspace "
                "(拒绝静默回退到进程 cwd) session=%s", context.session_id,
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=(
                    "claude_code harness requires explicit context.workspace "
                    "(拒绝静默回退到进程 cwd —— 会话会跑错目录)"
                ),
            )
            yield AgentEvent(type=AgentEventType.DONE)
            return

        session_id = context.session_id
        claude_bin = _resolve_claude_bin()
        settings_path = ""
        usage_total = AgentUsage()
        final_text = ""
        proc: asyncio.subprocess.Process | None = None

        try:
            # 1. 写 temp settings JSON
            if context.system_prompt:
                settings = {"claudeMd": context.system_prompt}
                fd, settings_path = tempfile.mkstemp(suffix=".json", prefix="agentops_claude_")
                os.close(fd)
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(settings, f, ensure_ascii=False)

            # 2. 构建 argv
            cmd = [claude_bin, "--print", "--output-format", "stream-json", "--verbose"]
            if settings_path:
                cmd.extend(["--settings", settings_path])
            cmd.extend(["--permission-mode", "bypassPermissions"])

            # Session 持久化
            native_sid = _native_sessions.get(session_id, "")
            if native_sid and context.persist_session:
                cmd.extend(["--resume", native_sid])
            elif context.persist_session:
                native_sid = str(uuid4())
                _native_sessions[session_id] = native_sid
                cmd.extend(["--session-id", native_sid])

            # 模型：只在明确是 Anthropic/Claude 模型时覆盖
            raw_model = context.model or ""
            model = ""
            if "/" in raw_model:
                provider, bare = raw_model.split("/", 1)
                if provider.lower() in ("anthropic", "claude"):
                    model = bare
            elif raw_model:
                if any(x in raw_model.lower() for x in ("claude", "fable", "opus", "sonnet", "haiku")):
                    model = raw_model
            if model:
                cmd.extend(["--model", model])

            # 用户消息（argv 元素，Python 自动处理 Windows 引号转义）
            cmd.append(prompt)

            # 3. Windows .CMD 文件需要 cmd.exe 解释，但**不用 shell=True**
            #    把 cmd.exe 作为 program、/c 作为 argv，Python 会自动转义后续参数
            if _needs_cmd_wrapper(claude_bin):
                argv = ["cmd.exe", "/c", *cmd]
            else:
                argv = cmd

            logger.info(
                "Claude harness starting: bin=%s model=%s session=%s prompt_len=%d system_prompt_len=%d",
                claude_bin, model or "default", session_id, len(prompt),
                len(context.system_prompt or ""),
            )
            logger.debug("Claude argv[0..7]: %s", argv[:8])

            # 4. 异步启动子进程
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workspace,
                    env=os.environ.copy(),
                    limit=1024 * 1024,  # 单行最大 1MB
                )
            except FileNotFoundError as e:
                logger.error("claude 二进制未找到: %s", e)
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error_message=f"claude binary not found: {argv[0]}",
                )
                yield AgentEvent(type=AgentEventType.DONE)
                return

            # 5. 后台 drain stderr（capped 12KB）
            stderr_chunks: list[str] = []

            async def _drain_stderr() -> None:
                try:
                    while True:
                        line = await proc.stderr.readline()
                        if not line:
                            return
                        stderr_chunks.append(line.decode("utf-8", errors="replace"))
                        if sum(len(s) for s in stderr_chunks) > 12_000:
                            joined = "".join(stderr_chunks)
                            stderr_chunks.clear()
                            stderr_chunks.append(joined[-12_000:])
                except (asyncio.CancelledError, Exception):
                    pass

            stderr_task = asyncio.create_task(_drain_stderr())

            # 6. 流式读 stdout，逐行解析，逐 event yield
            #    - 单行 idle 30s 不算 hang（LLM 可能慢）—— 继续等
            #    - 累计 self.timeout 强制 kill + 报错
            start_ts = time.monotonic()
            IDLE_TIMEOUT = 30.0

            try:
                while True:
                    elapsed = time.monotonic() - start_ts
                    if elapsed > self.timeout:
                        proc.kill()
                        await proc.wait()
                        stderr_tail = "".join(stderr_chunks)[-1000:]
                        logger.error("Claude CLI 总超时 (%ds)", self.timeout)
                        yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
                        yield AgentEvent(
                            type=AgentEventType.ERROR,
                            error_message=f"Claude CLI timeout ({self.timeout}s): {stderr_tail or '(no stderr)'}",
                        )
                        yield AgentEvent(type=AgentEventType.DONE, usage=usage_total)
                        return

                    line_timeout = min(IDLE_TIMEOUT, max(0.5, self.timeout - elapsed))
                    try:
                        raw = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=line_timeout
                        )
                    except asyncio.TimeoutError:
                        logger.debug(
                            "Claude stdout idle for %.1fs (still waiting)", IDLE_TIMEOUT
                        )
                        continue

                    if not raw:
                        break  # EOF
                    text = raw.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        logger.debug("claude stdout non-JSON: %s", text[:200])
                        continue

                    async for ev in self._map_cli_message(msg):
                        yield ev
                        if ev.type == AgentEventType.TEXT and ev.text:
                            final_text += ev.text
                        if ev.type == AgentEventType.USAGE and ev.usage:
                            usage_total.input_tokens = max(
                                usage_total.input_tokens, ev.usage.input_tokens
                            )
                            usage_total.output_tokens = max(
                                usage_total.output_tokens, ev.usage.output_tokens
                            )
            finally:
                stderr_task.cancel()
                try:
                    await stderr_task
                except (asyncio.CancelledError, Exception):
                    pass

            # 7. 等子进程退出（stdout EOF 后通常已 exit，但保险起见 wait 一下）
            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
                returncode = proc.returncode if proc.returncode is not None else -1

            # 8. 检查 returncode — 即使 stderr 为空也必须报告错误
            stderr_tail = "".join(stderr_chunks)[-1000:]
            if returncode != 0:
                logger.error(
                    "Claude CLI failed: returncode=%d stderr=%s",
                    returncode, stderr_tail,
                )
                yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error_message=(
                        f"Claude CLI exit code {returncode}: "
                        f"{stderr_tail or '(no stderr)'}"
                    ),
                )
                yield AgentEvent(type=AgentEventType.DONE, usage=usage_total)
                return

            logger.info(
                "Claude harness done: tokens_in=%d tokens_out=%d text_len=%d returncode=%d",
                usage_total.input_tokens, usage_total.output_tokens,
                len(final_text), returncode,
            )
            yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
            yield AgentEvent(
                type=AgentEventType.DONE,
                text=final_text or None,
                usage=usage_total,
            )

        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.exception("Claude harness 异常 session=%s", session_id)
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=f"Claude harness error: {e}",
                usage=usage_total,
            )
            yield AgentEvent(type=AgentEventType.DONE, usage=usage_total)
        finally:
            if settings_path:
                try:
                    os.unlink(settings_path)
                except Exception:
                    pass

    async def _map_cli_message(self, msg: dict[str, Any]) -> AsyncIterator[AgentEvent]:
        """将 Claude CLI stream-json 消息映射为 AgentEvent。"""
        msg_type = msg.get("type", "")

        if msg_type == "assistant":
            message = msg.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            yield AgentEvent(type=AgentEventType.TEXT, text=text)
                    elif block_type == "thinking":
                        thinking = block.get("thinking", "")
                        if thinking:
                            yield AgentEvent(type=AgentEventType.THINKING, text=thinking)
                    elif block_type == "tool_use":
                        yield AgentEvent(
                            type=AgentEventType.TOOL_USE,
                            tool_use_id=block.get("id", ""),
                            tool_name=block.get("name", ""),
                            tool_input=block.get("input", {}),
                        )

        elif msg_type == "result":
            usage_data = msg.get("usage", {})
            if isinstance(usage_data, dict):
                yield AgentEvent(
                    type=AgentEventType.USAGE,
                    usage=AgentUsage(
                        input_tokens=usage_data.get("input_tokens", 0),
                        output_tokens=usage_data.get("output_tokens", 0),
                    ),
                )

        elif msg_type == "system":
            subtype = msg.get("subtype", "")
            if subtype == "init":
                logger.debug(
                    "claude init: model=%s session=%s tools=%d",
                    msg.get("model", ""), msg.get("session_id", ""),
                    len(msg.get("tools", [])),
                )