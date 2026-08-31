---
paths:
  - "harness/**"
  - "config/agents/*.yaml"
  - "config/provider_catalog.py"
  - "harness/register.py"
priority: 50
---

# Harness / LLM 接入规则

本文件聚焦**多 LLM harness 抽象层**的修改约定（Claude Code / OpenCode / Kimi / Codex / 本地 LLM）。

## 架构

所有 harness 实现 `harness/protocol.py` 定义的 `Harness` 协议，通过 `harness/register.py` 反射注册到全局表。改一个 harness 只需：

1. 实现新 `Harness` 子类（或编辑现有）
2. 在 `register.py` 注册
3. `config/agents/*.yaml` 里把 `harness: <name>` 指向它

## 改前必读

- `harness/protocol.py` —— 接口契约（`invoke / stream / cancel`）
- `harness/claude_code.py` + `harness/claude_sdk.py` —— 两个 Claude Code 实现（前者 CLI 包装，后者 SDK）
- `harness/opencode_harness.py` —— OpenCode server 客户端
- `harness/codex_appserver.py` / `harness/codex_jsonrpc.py` —— Codex 两种接入
- `config/provider_catalog.py` —— provider/model 索引
- `harness/workspace_policy.py` —— sandbox / ACL 策略

## 改时约束

- 新加 harness：**必须实现** `protocol.py` 的完整接口（包括 cancel / cleanup）
- 改现有 harness：**不要改协议签名**——兼容性优先；加新方法而非改旧方法
- Codex 改 `--reload` 行为不要碰：现有 `start.ps1` 已注释原因（避免在 SelectorEventLoop 上跑 `asyncio.create_subprocess_exec` 的 NotImplementedError）
- `claude_sdk.py` 与 `claude_code.py` 不要合并——它们对应不同部署形态（in-process SDK vs out-of-process CLI）

## 改后必验

- `pytest tests/test_claude_harness.py tests/test_claude_sdk_harness.py tests/test_codex_appserver.py -x`
- `pytest tests/test_harness_routing.py -x` —— 路由
- 至少跑通一个真实 session：`POST /api/agent/run` + `harness: claude_sdk`

## 易踩坑

- `codex_appserver` 启动后会持有长连接 → 测试必须用 fixture 显式关闭
- `local_llm` 在无 GPU 时 fallback 链路深 → 改 fallback 时不要让 happy path 走 fallback
- `kimi_harness` 与 `claude_sdk` 都用 SSE，需要小心 chunk 边界
- `harness/register.py` 不要在模块顶层做网络请求（会拖慢 import）