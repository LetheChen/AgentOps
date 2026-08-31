"""注册内置 harness 实现。

在 api/server.py lifespan 启动时调用 register_builtin_harnesses()，
把 7 种 HarnessType 注册到全局 HarnessRegistry，供 orchestrator 按 agent yaml 的
`harness:` 字段路由到对应实现。

注册清单：
  - DETERMINISTIC → DeterministicClient（回放预录制响应，无 LLM）
  - OPENCODE      → OpencodeHarness（调 opencode server）
  - CLAUDE_CODE   → ClaudeCodeClient（调 claude CLI）
  - CODEX         → CodexAppServerClient（直连 minimax /v1/responses）
  - KIMI          → KimiHarness（调 kimi CLI，backup）
  - HTTP          → HttpHarness（调 HTTP harness，backup，当前 config 无引用）
  - LOCAL_LLM     → LocalLlmClient（OpenAI 兼容 API，log-patrol 用）

参考: harness/protocol.py HarnessRegistry, api/server.py lifespan
"""
from __future__ import annotations

from .protocol import HarnessRegistry, HarnessType
from .deterministic import DeterministicClient
from .opencode_harness import OpencodeHarness
from .claude_code import ClaudeCodeClient
from .claude_sdk import ClaudeSdkAgentClient
from .kimi_harness import KimiHarness
from .http_harness import HttpHarness
from .local_llm import LocalLlmClient  # H1: 提升为一等公民
from .codex_appserver import CodexAppServerClient  # codex app-server via WebSocket JSON-RPC


def register_builtin_harnesses() -> None:
    """注册全部内置 harness 到 HarnessRegistry。

    调用时机：api/server.py lifespan 启动时调用一次。
    幂等：HarnessRegistry.register 是覆盖式，重复调用不会报错。
    """
    HarnessRegistry.register(HarnessType.DETERMINISTIC, DeterministicClient)
    HarnessRegistry.register(HarnessType.OPENCODE, OpencodeHarness)
    HarnessRegistry.register(HarnessType.CLAUDE_CODE, ClaudeCodeClient)
    HarnessRegistry.register(HarnessType.CLAUDE_SDK, ClaudeSdkAgentClient)
    HarnessRegistry.register(HarnessType.CODEX, CodexAppServerClient)
    HarnessRegistry.register(HarnessType.KIMI, KimiHarness)
    HarnessRegistry.register(HarnessType.HTTP, HttpHarness)
    HarnessRegistry.register(HarnessType.LOCAL_LLM, LocalLlmClient)  # H1: 一等公民注册
