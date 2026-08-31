"""Claude Agent SDK Harness — 基于 claude-agent-sdk（PyPI，≥0.2.143 平台 wheel）。

Python 版Claude-SDK：
  - DAG 工具通过 create_sdk_mcp_server 注册为 in-process MCP server，
    Claude 用原生 tool_use 协议调用（替代 claude_code 的 system_prompt
    文本标记协议 —— 解决 manager 会话调不动项目工具/skill 的根因）
  - session 映射落盘 ~/.agentops/claude_sessions.json（替代内存 dict，
    服务重启后 resume 不断链）
  - PreToolUse hook（workspace_policy）拦截 Read/Grep/Glob/LS 工作区逃逸
  - system_prompt / cwd / setting_sources 原生传入（替代 temp settings
    JSON 的 claudeMd hack）

架构：
  Python (harness/claude_sdk.py)
    ├─ 每轮 run()：session store lookup_or_create → options
    ├─ ClaudeSDKClient（SDK 内部 spawn 捆绑的 claude.exe，stdin/stdout stream-json）
    ├─ in-process MCP server（agentops-dag-tools）→ ToolDefinition.handler
    ├─ PreToolUse hook → workspace_policy 校验
    └─ 结构化 Message 流（AssistantMessage/ResultMessage/...）→ AgentEvent

依赖：pip install --only-binary :all: claude-agent-sdk==0.2.143
（Windows 必须装平台 wheel，sdist 纯 Python 版不含捆绑 claude.exe，
 且 SDK 会拒绝执行 npm 安装的 claude.CMD —— cmd.exe 注入风险）
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool as sdk_tool,
)

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
from .workspace_policy import create_workspace_read_hook

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "agentops-dag-tools"

# 与 Claude 内置工具重名的 ToolDefinition 跳过注册（冲突过滤）
_CLAUDE_BUILTIN_TOOLS = frozenset({
    "Task", "Bash", "Glob", "Grep", "LS", "Read", "Edit", "MultiEdit",
    "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite", "BashOutput",
    "KillShell", "Skill", "ExitPlanMode", "Agent",
})


# ====== Session 持久化（替代 claude_code 的 _native_sessions 内存 dict）======

class ClaudeSessionStore:
    """agentops_session_id -> native claude session uuid 的落盘映射。

    格式（~/.agentops/claude_sessions.json）：
      { "<agentops_session_id>": {
          "native_session_id": "<uuid4>",
          "cwd": "<创建时 workspace>",
          "created_at": "<iso8601>" } }

    规则：
      - 已有映射且 cwd 一致 → resume（复用 Claude 会话历史）
      - 无映射 / cwd 变化   → 新 uuid + session_id（cwd 变化时旧会话
        在 ~/.claude/projects 下按路径归档，CLI 跨目录 resume 不可靠，故新开）
    """

    def __init__(self, path: Path | None = None):
        self._path = path or Path.home() / ".agentops" / "claude_sessions.json"
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self._path)  # 原子替换

    async def lookup_or_create(
        self, agentops_session_id: str, cwd: str
    ) -> tuple[str, bool]:
        """返回 (native_session_id, is_new)。"""
        async with self._lock:
            data = self._read()
            entry = data.get(agentops_session_id)
            if (
                entry
                and entry.get("native_session_id")
                and entry.get("cwd") == cwd
            ):
                return entry["native_session_id"], False
            if entry and entry.get("cwd") != cwd:
                logger.warning(
                    "session=%s cwd 变化 (%s -> %s)，新开 claude 会话",
                    agentops_session_id, entry.get("cwd"), cwd,
                )
            native = str(uuid4())
            data[agentops_session_id] = {
                "native_session_id": native,
                "cwd": cwd,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write(data)
            return native, True


_session_store = ClaudeSessionStore()


# ====== 工具注册（替代文本标记协议）======

def _wrap_tool_handler(
    tdef: ToolDefinition, context: AgentRunContext
) -> Any:
    """把 ToolDefinition.handler 包装为 SDK MCP 工具 handler。

    对齐 conversation_kit._try_execute_tool_call 的语义：
      - permission_check（tier fail-closed）先行，PermissionError → isError
      - handler 返回 dict → json.dumps 为文本（与文本协议回填格式一致）
      - 异常 → isError 结果（不中断主循环）
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if context.permission_check is not None:
                check = context.permission_check(tdef.name)
                if inspect.iscoroutine(check):
                    await check
            result = tdef.handler(args)  # type: ignore[misc]
            if inspect.iscoroutine(result):
                result = await result
            if isinstance(result, dict):
                text = json.dumps(result, ensure_ascii=False)
            else:
                text = str(result)
            return {"content": [{"type": "text", "text": text}]}
        except PermissionError as pe:
            return {
                "content": [{"type": "text", "text": f"[tool:{tdef.name}] 拒绝: {pe}"}],
                "is_error": True,
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"[tool:{tdef.name}] 错误: {e}"}],
                "is_error": True,
            }

    return handler


def build_mcp_server(
    tools: list[ToolDefinition], context: AgentRunContext
) -> Any | None:
    """把带 in-process handler 的 ToolDefinition 注册为 in-process MCP server。

    - handler=None 的（harness 内置工具，如 Bash/Read）不注册，由 SDK 原生提供
    - 与 Claude 内置工具重名的跳过并告警（避免覆盖内置语义）
    返回 None 表示无可注册工具。
    """
    sdk_tools = []
    for t in tools:
        if t.handler is None:
            continue
        if t.name in _CLAUDE_BUILTIN_TOOLS:
            logger.warning(
                "跳过与 Claude 内置工具重名的 ToolDefinition: %s", t.name
            )
            continue
        sdk_tools.append(
            sdk_tool(t.name, t.description, t.input_schema)(
                _wrap_tool_handler(t, context)
            )
        )
    if not sdk_tools:
        return None
    server = create_sdk_mcp_server(
        name=MCP_SERVER_NAME, version="0.1.0", tools=sdk_tools
    )
    logger.info(
        "claude_sdk MCP server registered: %d tools (%s)",
        len(sdk_tools), ", ".join(t.name for t in sdk_tools[:10]),
    )
    return server


# ====== 模型名解析（复用 claude_code.py 的规则）======

def _resolve_model_name(context: AgentRunContext) -> str:
    """provider/model 格式 → bare 模型名；仅 Anthropic/Claude 系才覆盖。"""
    raw = context.model or ""
    if "/" in raw:
        provider, bare = raw.split("/", 1)
        if provider.lower() in ("anthropic", "claude"):
            return bare
        return ""
    if raw and any(
        x in raw.lower() for x in ("claude", "opus", "sonnet", "haiku")
    ):
        return raw
    return ""


def _auth_env(context: AgentRunContext) -> dict[str, str]:
    """认证环境变量：~/.claude/settings.json env 节 + context 补缺。

    背景：claude_code harness 时代 CLI 自行加载 settings.json（用户本机
    配置为 MiniMax Anthropic 兼容代理：ANTHROPIC_AUTH_TOKEN +
    ANTHROPIC_BASE_URL + ANTHROPIC_DEFAULT_*_MODEL 映射）；SDK 隔离模式
    （setting_sources=[]）跳过 settings.json 加载，认证会丢失（表现为
    "Not logged in"），故此处显式读取 env 节透传给 CLI 子进程。

    优先级（对齐 claude_code 行为：settings.json 是事实上的认证来源）：
      1. settings.json env 节（本机 CLI 登录态/代理/模型映射）
      2. context.api_key/base_url（AgentOps provider 配置）仅补缺——
         注：provider 为 minimax 时 context 凭证是 OpenAI 协议的，
         直接覆盖会破坏 Anthropic 兼容代理，故不覆盖已有键。
    """
    env: dict[str, str] = {}
    # 1. settings.json env 节（用户本机 CLI 认证/代理配置）
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        section = data.get("env")
        if isinstance(section, dict):
            for key, value in section.items():
                if isinstance(key, str) and isinstance(value, str):
                    env[key] = value
            if env:
                logger.debug(
                    "claude_sdk auth env: 透传 settings.json env 节 %d 键",
                    len(env),
                )
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("读取 ~/.claude/settings.json 失败（跳过）: %s", e)

    # 2. AgentOps provider 配置仅补缺（不覆盖 settings.json 已有认证）
    has_auth = any(
        k in env for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    )
    if context.api_key and not has_auth:
        env["ANTHROPIC_API_KEY"] = context.api_key
    if context.base_url and "ANTHROPIC_BASE_URL" not in env:
        env["ANTHROPIC_BASE_URL"] = context.base_url
    return env


def _resolve_cli_path() -> str | None:
    """解析原生 claude CLI 路径（Windows 专项处理）。

    SDK 自身查找顺序（subprocess_cli._find_cli）会拒绝 npm 的 claude.CMD
    shim，且本机 wheel 未捆绑 claude.exe、~/.local/bin 也无原生安装，
    故在 harness 侧先行解析：

      1. CLAUDE_BIN 环境变量（与 claude_code._resolve_claude_bin 同约定，
         但要求指向 .exe —— SDK 拒绝 .bat/.cmd）
      2. npm 全局包内捆绑的原生 exe：
         which("claude") 命中 .CMD shim → 同目录 node_modules 下
         @anthropic-ai/claude-code/bin/claude.exe（npm 新版包内置）
      3. ~/.local/bin/claude.exe（官方原生安装器默认位置）
      4. None → 交由 SDK 默认解析（会给出明确报错指引）
    """
    # 1. 环境变量显式覆盖
    env_bin = os.environ.get("CLAUDE_BIN", "").strip()
    if env_bin:
        if env_bin.lower().endswith((".exe", ".com")):
            return env_bin
        logger.warning(
            "CLAUDE_BIN=%s 不是原生可执行文件（.exe/.com），忽略", env_bin
        )

    # 2. npm 全局包内的原生 exe
    try:
        which_hit = shutil.which("claude")
    except OSError:
        which_hit = None
    if which_hit:
        npm_native = (
            Path(which_hit).parent
            / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
            / "claude.exe"
        )
        if npm_native.is_file():
            return str(npm_native)

    # 3. 官方原生安装器默认位置
    native_install = Path.home() / ".local" / "bin" / "claude.exe"
    if native_install.is_file():
        return str(native_install)

    return None


# ====== Harness 主体 ======

class ClaudeSdkAgentClient(AgentClient):
    """Claude Agent SDK harness：ClaudeSDKClient + in-process MCP + 结构化消息。

    harness_type=CLAUDE_SDK。每轮 run()：
      1. session store lookup_or_create（落盘持久化）
      2. 注册 DAG 工具为 in-process MCP server
      3. 构造 ClaudeAgentOptions（cwd/system_prompt/session/hooks/env）
      4. ClaudeSDKClient 一轮对话：query → receive_response → AgentEvent
    """

    def __init__(self, timeout: float = 300.0):
        self.timeout = timeout

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.CLAUDE_SDK

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """执行一轮 Agent 对话（结构化流）。

        Yields:
            AgentEvent: TEXT / THINKING / TOOL_USE / TOOL_RESULT / USAGE / ERROR / DONE
        """
        assert_protocol_compatible(
            HarnessType.CLAUDE_SDK, context.protocol or "anthropic_compatible"
        )

        # cwd fail-fast：workspace 必须显式（不再静默回退 os.getcwd()）
        workspace = (context.workspace or "").strip()
        if not workspace:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=(
                    "claude_sdk harness requires explicit context.workspace "
                    "(拒绝静默回退到进程 cwd —— 会话会跑错目录)"
                ),
            )
            yield AgentEvent(type=AgentEventType.DONE)
            return

        usage_total = AgentUsage()

        # 1. session 解析（persist_session=False → ephemeral，不落盘）
        session_id_option: str | None = None
        resume_option: str | None = None
        if context.persist_session:
            native_sid, is_new = await _session_store.lookup_or_create(
                context.session_id, workspace
            )
            if is_new:
                session_id_option = native_sid
            else:
                resume_option = native_sid

        # 2. in-process MCP server
        mcp_server = build_mcp_server(tools, context)

        # 3. options
        model = _resolve_model_name(context)
        cli_path = _resolve_cli_path()
        if cli_path:
            logger.info("claude_sdk cli_path resolved: %s", cli_path)
        options = ClaudeAgentOptions(
            cwd=workspace,
            system_prompt=context.system_prompt or None,
            setting_sources=[],               # SDK 隔离模式：上下文由 AgentOps 全权控制
            permission_mode="bypassPermissions",
            strict_mcp_config=True,            # 只用我们注册的 MCP，不加载项目 .mcp.json
            cli_path=cli_path,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Read|Grep|Glob|LS",
                        hooks=[create_workspace_read_hook(workspace)],
                    )
                ]
            },
            env=_auth_env(context),
            stderr=lambda data: logger.debug("claude stderr: %s", data[-500:]),
        )
        if mcp_server is not None:
            options.mcp_servers = {MCP_SERVER_NAME: mcp_server}
        if model:
            options.model = model
        if session_id_option:
            options.session_id = session_id_option
        if resume_option:
            options.resume = resume_option

        logger.info(
            "Claude SDK harness starting: model=%s session=%s resume=%s "
            "cwd=%s mcp_tools=%s prompt_len=%d",
            model or "default", context.session_id, bool(resume_option),
            workspace,
            "yes" if mcp_server is not None else "no",
            len(prompt),
        )

        # 4. 一轮对话（超时保护 + abort watcher）
        client: ClaudeSDKClient | None = None
        abort_task: asyncio.Task | None = None
        try:
            async with asyncio.timeout(self.timeout):
                client = ClaudeSDKClient(options=options)
                async with client:
                    abort_task = self._spawn_abort_watcher(context, client)
                    await client.query(prompt)
                    async for msg in client.receive_response():
                        for ev in self._map_message(msg):
                            yield ev
                            if ev.type == AgentEventType.USAGE and ev.usage:
                                usage_total = ev.usage
                            if ev.type == AgentEventType.DONE:
                                return
        except TimeoutError:
            logger.error("Claude SDK 总超时 (%ss)", self.timeout)
            yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=f"Claude SDK timeout ({self.timeout}s)",
            )
            yield AgentEvent(type=AgentEventType.DONE, usage=usage_total)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(
                "Claude SDK harness 异常 session=%s", context.session_id
            )
            yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message=f"Claude SDK harness error: {e}",
            )
            yield AgentEvent(type=AgentEventType.DONE, usage=usage_total)
        finally:
            if abort_task is not None:
                abort_task.cancel()

    @staticmethod
    def _spawn_abort_watcher(
        context: AgentRunContext, client: ClaudeSDKClient
    ) -> asyncio.Task | None:
        """abort_signal（asyncio.Event）置位 → interrupt 当前回合。"""
        signal = getattr(context, "abort_signal", None)
        if signal is None:
            return None

        async def _watch() -> None:
            try:
                await signal.wait()
                await client.interrupt()
            except (asyncio.CancelledError, Exception):
                pass

        return asyncio.create_task(_watch())

    def _map_message(self, msg: Any) -> list[AgentEvent]:
        """SDK 结构化 Message → AgentEvent 列表（纯函数，便于单测）。"""
        events: list[AgentEvent] = []

        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    events.append(
                        AgentEvent(type=AgentEventType.TEXT, text=block.text)
                    )
                elif isinstance(block, ThinkingBlock) and block.thinking:
                    events.append(
                        AgentEvent(type=AgentEventType.THINKING, text=block.thinking)
                    )
                elif isinstance(block, ToolUseBlock):
                    events.append(
                        AgentEvent(
                            type=AgentEventType.TOOL_USE,
                            tool_use_id=block.id,
                            tool_name=block.name,
                            tool_input=block.input,
                        )
                    )

        elif isinstance(msg, UserMessage):
            # 主线程 tool result 以 UserMessage 形式回流（content 为 block 列表时）
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        content = block.content
                        if isinstance(content, list):
                            text = "\n".join(
                                str(item.get("text", ""))
                                for item in content
                                if isinstance(item, dict)
                            )
                        else:
                            text = content or ""
                        events.append(
                            AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                tool_use_id=block.tool_use_id,
                                tool_result=text,
                                tool_is_error=bool(block.is_error),
                            )
                        )

        elif isinstance(msg, ResultMessage):
            usage = msg.usage or {}
            usage_event = AgentEvent(
                type=AgentEventType.USAGE,
                usage=AgentUsage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                ),
            )
            events.append(usage_event)
            events.append(
                AgentEvent(
                    type=AgentEventType.DONE,
                    text=msg.result,
                    usage=usage_event.usage,
                )
            )

        return events
