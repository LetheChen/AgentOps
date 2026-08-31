"""
Harness Adapter Protocol — v2.1.

Inspired by agentOps's `AgentClient` pattern. Unifies 6 backends:
  - opencode       (default, M0 candidate A)
  - claude_code    (claude-agent-sdk)
  - codex          (codex app-server via JSON-RPC)
  - kimi           (kimi-code SDK)
  - http           (call v1 sub-services / MCP servers)
  - deterministic  (replay / regression test)

A Harness takes a prompt + tool list + context, returns AgentEvent stream.
The Orchestrator wraps this in a Node execution and translates to DagEvent.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Literal


# ====== Enums ======

class HarnessType(str, Enum):
    OPENCODE = "opencode"
    CLAUDE_CODE = "claude_code"
    CLAUDE_SDK = "claude_sdk"          # Claude Agent SDK（in-process MCP + 结构化消息）
    CODEX = "codex"  # 已实现：codex_appserver.py 直连 minimax /v1/responses
    KIMI = "kimi"
    HTTP = "http"
    DETERMINISTIC = "deterministic"
    LOCAL_LLM = "local_llm"           # H1: 纯 API 调用一等公民（不再偷占 OPENCODE 槽位）


class AgentEventType(str, Enum):
    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    ERROR = "error"
    TURN_COMPLETE = "turn_complete"
    DONE = "done"


# ====== Tool Definition ======

@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]   # JSON Schema
    handler: "Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None" = None
    # For built-in (DAG) tools (handoff / send_message / graph_context), handler is in-process.
    # For harness-internal tools (Bash/Read), handler is None — harness executes natively.


# ====== Permission ======

@dataclass
class PermissionSet:
    """Computed from Agent's role assignments (sys.* + cap.*).

    Resolution rule: deny > allow > unset.
    """
    allowed_tools: set[str] = field(default_factory=set)
    denied_tools: set[str] = field(default_factory=set)

    def is_allowed(self, tool: str) -> bool:
        if tool in self.denied_tools:
            return False
        if tool in self.allowed_tools:
            return True
        return False   # unset → default deny (fail-closed)

    @classmethod
    def merge(cls, perms: list["PermissionSet"]) -> "PermissionSet":
        """Merge multiple role permissions. deny > allow semantics."""
        allowed: set[str] = set()
        denied: set[str] = set()
        for p in perms:
            allowed |= p.allowed_tools
            denied |= p.denied_tools
        return cls(allowed_tools=allowed - denied, denied_tools=denied)


# ====== Agent Run Context ======

@dataclass
class AgentRunContext:
    system_prompt: str
    model: str
    api_key: str
    base_url: str
    workspace: str
    session_id: str
    protocol: str = ""                  # openai_compatible / anthropic_compatible / custom
    auth_type: str = ""                   # bearer / x-api-key
    abort_signal: Any = None              # asyncio.Event
    permission: PermissionSet = field(default_factory=PermissionSet)
    extra: dict[str, Any] = field(default_factory=dict)  # harness-specific (e.g., codex sandbox)
    tools: list[ToolDefinition] = field(default_factory=list)  # opencode server: {tool_name: True}
    # Thread 模式：是否持久化 session（thread/start ephemeral=false）
    persist_session: bool = True
    # Thread 模式：resume 后的 prompt（不含历史，codex 服务端已有）
    resumed_prompt: str | None = None
    # Thread 模式：provider / service_tier / reasoning_effort（codex app-server 参数）
    provider: str | None = None
    service_tier: str | None = None
    reasoning_effort: str | None = None
    # Thread 模式：skill 根目录（codex skills/extraRoots/set）
    skill_roots: list[str] = field(default_factory=list)
    # 方案A：容器内执行 codex subagent — docker exec 模式
    # 当 container_id 非空时，CodexJsonRpcClient 通过 `docker exec -i <container_id> codex app-server`
    # 在容器内启动 codex，而非 host 本地 spawn。容器只跑 codex，不启动 agentops 服务。
    container_id: str | None = None
    # 会话权限级别推导的沙箱模式（deepseek-harness 对齐：read-only / workspace-write / danger-full-access）。
    # None = 未指定，harness 回退到部署级默认（环境变量）。
    sandbox_mode: str | None = None
    # 工具执行前动态权限校验回调（fail-closed）：抛 PermissionError 表示拒绝。
    # orchestrator 注入（闭包捕获 session tier / workspace 状态 / 审批服务），
    # harness 层不依赖 orchestrator —— 分层边界与 deepseek 的 approval seam 等价。
    permission_check: Any = None


# ====== Agent Event (normalized across all harnesses) ======

@dataclass
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class AgentEvent:
    type: AgentEventType
    text: str | None = None              # for text/thinking
    tool_use_id: str | None = None       # for tool_use/tool_result
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: str | None = None
    tool_is_error: bool = False
    usage: AgentUsage | None = None      # for usage
    error_message: str | None = None     # for error
    turn_number: int | None = None       # for turn_complete
    raw: dict[str, Any] = field(default_factory=dict)   # original vendor payload


# ====== Agent Client Protocol ======

class AgentClient(ABC):
    """Single agent run. Each Orchestrator node spawns one AgentClient.run() per node."""

    @property
    @abstractmethod
    def harness_type(self) -> HarnessType: ...

    @abstractmethod
    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """Run agent loop, yield AgentEvent until done or aborted.

        Must:
          - yield at least one DONE event
          - honor context.abort_signal
          - record token usage on USAGE event
          - NEVER drop events silently (errors → ERROR event)
        """
        ...

    async def resume(self, session_id: str, instruction: str) -> AsyncIterator[AgentEvent]:
        """Optional: fork from a previous session checkpoint."""
        raise NotImplementedError(f"{self.harness_type} does not support resume")
        yield  # for type checker


# ====== Harness Registry ======

class HarnessRegistry:
    """Maps HarnessType → AgentClient factory."""

    _factories: dict[HarnessType, type[AgentClient]] = {}

    @classmethod
    def register(cls, harness_type: HarnessType, factory: type[AgentClient]) -> None:
        cls._factories[harness_type] = factory

    @classmethod
    def create(cls, harness_type: HarnessType, **kwargs) -> AgentClient:
        if harness_type not in cls._factories:
            raise ValueError(f"No harness registered for {harness_type}. Registered: {list(cls._factories)}")
        return cls._factories[harness_type](**kwargs)

    @classmethod
    def available(cls) -> list[HarnessType]:
        return list(cls._factories.keys())


# ====== Protocol 兼容校验（harness 自描述，不含未实现的 CODEX）======

HARNESS_PROTOCOLS: dict[HarnessType, set[str]] = {
    HarnessType.OPENCODE: {"openai_compatible", "anthropic_compatible", "dashscope_native"},
    HarnessType.CLAUDE_CODE: {"anthropic_compatible"},
    HarnessType.CLAUDE_SDK: {"anthropic_compatible"},   # claude-agent-sdk（官方 Python SDK）
    HarnessType.CODEX: {"openai_compatible"},   # codex 走 OpenAI Responses API（wire_api=responses）
    HarnessType.KIMI: {"openai_compatible"},
    HarnessType.LOCAL_LLM: {"openai_compatible", "anthropic_compatible"},
    HarnessType.HTTP: {"custom", "openai_compatible"},
    HarnessType.DETERMINISTIC: set(),
}


def assert_protocol_compatible(harness: HarnessType, protocol: str) -> None:
    """harness 自我校验 protocol 兼容性，不兼容直接 raise。"""
    allowed = HARNESS_PROTOCOLS.get(harness, set())
    if not allowed:
        return
    if not protocol:
        return
    if protocol not in allowed:
        raise ValueError(
            f"Harness '{harness.value}' 不兼容 protocol '{protocol}'。支持: {sorted(allowed)}"
        )
