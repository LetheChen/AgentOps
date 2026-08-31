# AgentOps Harness 子系统结构化分析

> 综合分析员产出 · 主题：AgentOps 内部 harness 模块（项目本地视角）
> 深度：medium · 生成日期：2026-08-16
> 配套文档：`docs/00-platform/harness-analysis/ANALYSIS-harness-three-contexts.md`（三语境概念综述，本文档是项目实现侧的深度对位）

---

## 一句话结论

**harness/ 子系统是 AgentOps DAG 引擎的「LLM 后端适配层」**——它把 7 种异构 Agent 运行时（CLI 子进程 / 本地 HTTP / 子进程 stdio-JSONRPC / OpenAI 兼容 Chat Completions / 进程内确定性回放）统一收敛到一份 `AgentClient` 抽象 + `AsyncIterator[AgentEvent]` 事件流，让 DAG 节点无需关心后端差异。**当前 13 个 agent 中：local_llm 占 6 席（任务管理类首选）、codex 占 4 席（视频/分析类）、claude_code 占 3 席（编码/质检），形成 "local_llm 简单任务 / codex 高复杂度 / claude_code 编码专属" 的三分天下**。

---

## 一、Harness 模块的 8 个文件全景

| 文件 | 字节数 | 角色 |
|------|--------|------|
| `protocol.py` | 8007 | 抽象层：HarnessType / AgentClient / AgentEvent / AgentRunContext / PermissionSet / ToolDefinition / HarnessRegistry |
| `register.py` | 2064 | 启动注册表，7 种 HarnessType → 工厂映射 |
| `__init__.py` | 553 | 入口：导出公共符号 + `register_builtin_harnesses()` 自动调用 |
| `deterministic.py` | 4375 | DETERMINISTIC：纯本地工具调用回放（无 LLM，golden fixture） |
| `opencode_harness.py` | 12687 | OPENCODE：HTTP + SSE 调 opencode server（**M0 默认后端**） |
| `claude_code.py` | 16935 | CLAUDE_CODE：subprocess 调 `claude --print --output-format stream-json` |
| `codex_appserver.py` | 35804 | CODEX：JSON-RPC over stdio 调 codex app-server，**最大、最复杂** |
| `codex_jsonrpc.py` | 14069 | CODEX 底层：JSON-RPC 客户端 + 凭证安全模式 |
| `kimi_harness.py` | 6203 | KIMI：subprocess 调 kimi CLI（**当前 0 个 agent 使用**，保留位） |
| `local_llm.py` | 15337 | LOCAL_LLM：httpx 直连 OpenAI 兼容 Chat Completions（max_rounds=8） |
| `http_harness.py` | 7834 | HTTP：调 v1 oa_audit HTTP 服务 + 进程内 fallback |
| `v1_oa_audit_adapter.py` | 4792 | v1 适配器：进程内 import `E:\Project\AI_Agent_Platform\...` 的 9 步管线 |
| `thread_lease.py` | 2548 | 进程内 lease：session 单例 turn 互斥 |

**总代码量 ~ 130KB（10 个 .py 实现）**，未含 28KB 测试（`tests/test_claude_harness.py` + `tests/test_harness_routing.py`）。

---

## 二、抽象契约：`AgentClient` 三大支柱

### 2.1 协议层（protocol.py）

```python
class AgentClient(ABC):
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
```

### 2.2 8 类事件类型（AgentEventType）

| 事件 | 必填字段 | 触发场景 |
|------|----------|----------|
| `TEXT` | text | LLM 流式输出文本片段 |
| `THINKING` | text | 推理/思考片段（reasoning 模型） |
| `TOOL_USE` | tool_use_id, tool_name, tool_input | LLM 决定调用工具 |
| `TOOL_RESULT` | tool_use_id, tool_result, tool_is_error | harness 执行完工具回调 |
| `USAGE` | usage: AgentUsage | 每个 turn 结束的成本统计 |
| `ERROR` | error_message | 任何失败（**不能吞掉，必须 emit**） |
| `TURN_COMPLETE` | turn_number | 一轮对话结束 |
| `DONE` | usage | 整个 run 结束（**必须发**） |

### 2.3 上下文注入（AgentRunContext）

23 个字段的 dataclass，关键字段：
- **凭据**：`api_key` / `base_url` / `auth_type`（bearer / x-api-key）
- **模型路由**：`model` / `provider` / `service_tier` / `reasoning_effort`
- **协议**：`protocol`（openai_compatible / anthropic_compatible / dashscope_native / custom）
- **会话控制**：`session_id` / `persist_session` / `resumed_prompt`
- **执行边界**：`workspace` / `abort_signal`（asyncio.Event）
- **权限**：`permission: PermissionSet`（deny > allow > unset fail-closed）
- **工具**：`tools: list[ToolDefinition]`
- **codex 专属**：`container_id`（docker exec 模式）/ `skill_roots`（codex skills 根目录）

### 2.4 协议兼容性校验

```python
HARNESS_PROTOCOLS = {
    HarnessType.OPENCODE: {"openai_compatible", "anthropic_compatible", "dashscope_native"},
    HarnessType.CLAUDE_CODE: {"anthropic_compatible"},
    HarnessType.CODEX: {"openai_compatible"},   # wire_api=responses
    HarnessType.KIMI: {"openai_compatible"},
    HarnessType.LOCAL_LLM: {"openai_compatible", "anthropic_compatible"},
    HarnessType.HTTP: {"custom", "openai_compatible"},
    HarnessType.DETERMINISTIC: set(),
}
```

每个 harness 在 run() 入口调 `assert_protocol_compatible(...)`，不兼容直接 raise，**fail-loud 而非 fail-quiet**。

---

## 三、7 种 Harness 实现横向对比

### 3.1 进程模型对比

| Harness | 进程模型 | 通信协议 | 工具执行方 | Session 持久化 |
|---------|----------|----------|------------|----------------|
| `OPENCODE` | 独立 server 进程 | HTTP POST + 长连接 SSE（GET /event） | opencode server 内部执行 | `~/.local/share/opencode/opencode.db`（**无自动清理**，36 次 DAG ≈ 180 session 残留） |
| `CLAUDE_CODE` | subprocess（`cmd.exe /c claude`） | stream-json（stdout JSON Lines） | claude CLI 内部执行 + DAG 内 tool_call 文本协议 | 内存 `_native_sessions: dict[str, str]` |
| `CODEX` | subprocess（JSON-RPC stdio） | JSON-RPC 2.0 over stdio / **容器内 `docker exec`** | codex app-server 内部执行 + DAG 内 handler | codex 进程内 thread 管理 + 内存缓存 + 进程级 `_active_leases` |
| `KIMI` | subprocess | CLI stdout | kimi CLI 内部 | 无 |
| `LOCAL_LLM` | **无子进程** | httpx 直连 OpenAI 兼容 endpoint | **DAG 进程内 handler**（llm 返回 `tool_calls` → 本地 asyncio 执行） | 无 |
| `HTTP` | httpx 调外部 / 进程内 import | HTTP POST / 进程内函数调用 | v1 服务 / 进程内 mock | 无 |
| `DETERMINISTIC` | **无进程** | N/A | **DAG 进程内 handler**（`tools[0].handler`） | 无 |

### 3.2 工具执行模式二象限

- **「LLM 自己跑工具」阵营**（harness 内部能力）：OPENCODE（bash/read/edit/glob/grep）、CLAUDE_CODE（cli 内部）、CODEX（app-server 内部）、KIMI（kimi 内部）
- **「DAG 替 LLM 跑工具」阵营**（harness 只产事件）：LOCAL_LLM、HTTP、DETERMINISTIC

**这两种模式决定了 emit 时序差异**：
- 第一阵营：`TOOL_USE` 后 harness 等工具内部完成后才 emit `TOOL_RESULT`
- 第二阵营：`TOOL_USE` 后 DAG 引擎拿事件 → 查 `ToolDefinition.handler` → 异步执行 → 回写 `TOOL_RESULT` 事件

### 3.3 错误模型差异

| Harness | 异常路径 | Token 保护（D-022） |
|---------|----------|---------------------|
| OPENCODE | try/except → yield ERROR → yield DONE(usage) | ✅ |
| CLAUDE_CODE | subprocess returncode 非 0 → yield ERROR + DONE | ✅ |
| CODEX | `codex run` 异常 + aborted 路径都必须 emit USAGE + ERROR + DONE(**usage_total**) | ✅（D-022 专项修复） |
| KIMI | shutil.which 找不到 → 立即 ERROR + DONE | ✅ |
| LOCAL_LLM | httpx timeout/网络错误 → yield ERROR + DONE | ✅ |
| HTTP | HTTP 不可达 → fall back 到 v1_oa_audit_adapter → 真失败 yield ERROR + DONE | ✅ |
| DETERMINISTIC | 无外部依赖，几乎不出错 | N/A |

---

## 四、注册与路由

### 4.1 注册时机（register.py）

```python
def register_builtin_harnesses() -> None:
    HarnessRegistry.register(HarnessType.DETERMINISTIC, DeterministicClient)
    HarnessRegistry.register(HarnessType.OPENCODE,      OpencodeHarness)
    HarnessRegistry.register(HarnessType.CLAUDE_CODE,   ClaudeCodeClient)
    HarnessRegistry.register(HarnessType.CODEX,         CodexAppServerClient)
    HarnessRegistry.register(HarnessType.KIMI,          KimiHarness)
    HarnessRegistry.register(HarnessType.HTTP,          HttpHarness)
    HarnessRegistry.register(HarnessType.LOCAL_LLM,     LocalLlmClient)  # H1
```

`__init__.py` 末尾**导入即注册**——`from harness import ...` 触发 `register_builtin_harnesses()`，DAG 引擎启动时已经能拿到全部 7 个工厂。`HarnessRegistry.register` 是幂等的（dict 覆盖写），重复调用安全。

### 4.2 引擎集成路径（workflow/engine.py）

DAG 节点 → `_execute_node` → `_run_agent_node` → `effective_harness = node.inline_agent.harness if node.inline_agent else node.harness` → `harness = self._create_harness(effective_harness)` → `async for event in harness.run(prompt, tools, ctx)` → 事件透传到 `_translate_event_to_dag_event`。

**inline_agent.harness > node.harness**（P0.5）：允许 workflow 节点级 override agent 级 harness，**两套值用同一字段**，避免引入新 yaml 字段。

### 4.3 测试保护网（tests/test_harness_routing.py）

`TestCreateHarnessTransparentRouting` 4 个 case 锁死：
- `harness: opencode` 必须创建 `OpencodeHarness`（**不是** `LocalLlmClient`）
- `harness: claude_code` 必须创建 `ClaudeCodeClient`（**不是** `LocalLlmClient`）
- `harness: local_llm` 必须创建 `LocalLlmClient`（一等公民）
- `harness: deterministic` 必须创建 `DeterministicClient`

这套测试是 H1（**LocalLlmClient 不再偷占 OPENCODE 槽位**）的回归保护——一旦有人把 `LocalLlmClient` 注册到 `OPENCODE` 槽位，CI 立即红。

---

## 五、Agent → Harness 映射（13 个 agent 全景）

| Agent | Domain | Harness | Tier | 说明 |
|-------|--------|---------|------|------|
| `log_analyst` | log_patrol | **local_llm** | T1 | 日志扫描/分析（轻量） |
| `task_monitor` | task_patrol | **local_llm** | T1 | 任务监控（轻量） |
| `smart_form` | smart_form | **local_llm** | T2 | 智能表单（结构化） |
| `smart_approval` | smart_approval | **local_llm** | T2 | 智能审批（结构化） |
| `manager` | manager | **local_llm** | T3 | 总控（路由为主，少量推理） |
| `task_planner` | task_management | **local_llm** | T3 | 任务规划（结构化输出） |
| `coding_agent` | task_management | **claude_code** | T3 | 编码执行（**专属**，claude code 最强） |
| `proposal_planner` | proposal | **claude_code** | T3 | 方案规划（强推理） |
| `quality_inspector` | quality | **claude_code** | T3 | 质检（**v88 重组**：原 video_validator + deliverable_validator） |
| `smart_analysis` | smart_analysis | **codex** | T3 | 智能分析（**高复杂度**） |
| `smart_query` | smart_query | **codex** | T3 | 智能查询（**高复杂度**） |
| `smart_ops` | smart_ops | **codex** | T3 | 智能运维（**高复杂度**） |
| `video_creator` | video_producer | **codex** | T3 | 视频制作（**最强 codex** + minimax M3） |

**分布：local_llm 6 / claude_code 3 / codex 4**（共 13，KIMI/HTTP/DETERMINISTIC 0 个生产 agent 使用）

### 5.1 选择启发式（非硬性，由历史决策沉淀）

- **local_llm（首选 46%）**：轻量、结构化、token 便宜；适合**扫日志、查任务、填表单、简单审批**这类"已知 schema → LLM 套模板"的任务。模型多为 `deepseek-v4-flash` / `MiniMax-M2.7`
- **claude_code（23%）**：**编码专属**——AGENTS.md 明确"任务管理模块核心执行体"，只有 3 个 agent 用；适合需要 agent loop 长会话 + terminal 交互的场景
- **codex（31%）**：**高复杂度专属**——codex app-server 的 thread 模型 + JSON-RPC 通知流适合"复杂多轮决策 + 大量工具调用"。4 个 agent 都是 T3 高 tier

**KIMI/HTTP/DETERMINISTIC 都是"保留位"**——注册了但 0 生产使用。KIMI 是显式标注"长期不用可考虑删除"（见 register.py 注释）。

---

## 六、跨 Harness 共享机制

### 6.1 ToolDefinition 通用契约

```python
@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]   # JSON Schema
    handler: Callable | None       # None 表示 harness 内部执行
```

**handler 在场 vs 不在场**就是区分"DAG 进程内执行"和"harness 内部执行"的唯一标志。

### 6.2 PermissionSet fail-closed 语义

```python
def is_allowed(self, tool: str) -> bool:
    if tool in self.denied_tools: return False   # deny 最强
    if tool in self.allowed_tools: return True   # allow 次之
    return False   # unset → 默认拒绝（fail-closed）
```

`merge(perms)` 多个 role 时执行 `allowed - denied`，deny 永远赢。

### 6.3 Thread Lease（仅 CODEX 涉及）

`_active_leases: dict[str, str]` + asyncio.Lock：同一 session 同一时刻只能有一个活跃 turn，**防止用户快速连发两条消息导致 thread 状态错乱**。`_clear_all_leases()` 给测试用。

### 6.4 Credential Redaction（CLAUDE_CODE / CODEX 共享）

两个 harness 都有 `_SECRET_KEYS = {"apiKey", "OPENAI_API_KEY", "Authorization", "auth_token", ...}`，在记录 raw event 前 redact 敏感字段；CODEX 还做 `_SECRET_ENV_PATTERNS` 检查，敏感 env 用 `-e KEY` 形式由 docker daemon 注入而不是写在命令行（避免 `ps aux` 可见）。

### 6.5 关键修复与设计意图

| ID | 影响范围 | 修复内容 |
|----|----------|----------|
| **H1** | LOCAL_LLM 注册 | LocalLlmClient 不再偷占 OPENCODE 槽位，独立 HarnessType.LOCAL_LLM 一等公民 |
| **D-022** | CODEX | aborted 路径和 except 路径都必须 emit USAGE + ERROR + DONE(usage_total)，避免已累加 token 丢失 |
| **D-031** | LOCAL_LLM | 去掉 `<provider>/` 前缀（`minimax/MiniMax-M3` → `MiniMax-M3`），minimax chat/completions API 不接受带前缀的 model ID |
| **D-030** | LOCAL_LLM | timeout 从 60s 提到 120s，content_curator 多 draft 评估场景单轮 60-90s 不够 |

---

## 七、与上游/下游的边界

### 7.1 上游：DAG 引擎（workflow/engine.py）

- 节点 yaml 的 `harness:` 字段 → `HarnessTypeRef` 枚举 → `_create_harness` → `AgentClient` 实例
- `AgentRunContext` 由引擎从 yaml + run_inputs + 模型配置 + workspace + abort_signal 组装后传入
- harness 不感知 DAG 拓扑、节点拓扑、上下游——只关心 prompt + tools + context

### 7.2 下游：audit/event 持久化

- harness 产出的 `AgentEvent` 流 → 引擎 `_translate_event_to_dag_event` → `DagEventType` 枚举（node.progress / node.tool_call / usage 等）→ 写 `run_events` 表
- `AgentUsage` 字段（input/output/cache_read/cache_creation）映射到 `DagEvent.usage`，最终汇总到 `usage_records` 表

### 7.3 凭证来源（关键集成点）

- **前端运行时配置**：CredentialStore（`~/.agentops/credentials.db`，Fernet 加密）存 provider API key
- **opencode 进程独立**：`~/.config/opencode/opencode.json` 是 opencode server 单独读的配置
- **自动同步机制**：`api/server.py` lifespan 启动时 `_sync_opencode_credentials()` 从 CredentialStore 拉 key → 替换 `opencode.json` 的 `${VAR}` 占位符 → opencode server 下次启动自动用新 key
- **一次配置两端生效**：前端录入 1 次，AgentOps 后端 + opencode server 都用

---

## 八、关键风险与技术债

### 8.1 死代码（KIMI harness）

`kimi_harness.py` 注册但**0 个 agent 使用**。register.py 注释："长期不用可考虑删除"。**建议**：要么绑定 1 个 agent 做 A/B 对比，要么从 register 中移除。

### 8.2 HTTP harness 也基本是死代码

`http_harness.py` + `v1_oa_audit_adapter.py` 注册但 0 个 agent 使用。设计意图是"v2 DAG 调 v1 子服务做渐进迁移"，但当前 v2 已自给自足，迁移未发生。**建议**：评估是否保留（如果未来 v1 → v2 还有任务）还是直接删除。

### 8.3 opencode session 残留

每次 DAG 节点跑 1 个 opencode session，36 节点 DAG ≈ 180 个 session 残留在 `~/.local/share/opencode/opencode.db`。**不影响功能，占磁盘 + SQLite 变慢**。清理：`python -c "import sqlite3; c=sqlite3.connect(r'<db_path>'); c.execute('DELETE FROM session'); c.commit()"` 后重启 opencode。`restart_opencode.ps1 -CleanSessions` 已脚本化。

### 8.4 LOCAL_LLM 抽象不对称

LOCAL_LLM 是**唯一**自己跑工具循环（max_rounds=8）的 harness——其他 harness 都让 LLM 自己决定何时停止。**这导致**：
- LOCAL_LLM 必须自己处理 max_rounds 强制终止（注入 user 消息"停止 read_file，直接给答案"）
- LOCAL_LLM 必须自己处理 provider/model 校验（如 D-031）
- LOCAL_LLM 必须自己处理 credential 优先级（构造参数 > context）

**长期**：要不要把"tool call loop"也抽象成 `LLMToolLoop` 协议？让 OPENCODE/CLAUDE_CODE/CODEX 也可选走 in-process loop（性能 + 可观测性更好）？

### 8.5 Thread Lease 进程内 + 单实例

`thread_lease.py` 的 `_active_leases` 是**进程内 dict**，**不跨进程**。如果未来 AgentOps 多 worker 部署（uvicorn --workers 4），同一 session 的 turn 可能跨进程并发，lease 失效。**建议**：换 `Redis SETNX` 或 `asyncio.Lock + IPC`。

### 8.6 Codex 占 31% 但模型强依赖 minimax

video_creator / smart_analysis / smart_query / smart_ops 4 个 agent 都用 codex harness + minimax provider。**单点依赖**：minimax 短时间高频触发 429，opencode 会把 model 标记为 `ModelUnavailable` 持续到重启（D-022 周边问题）。**建议**：在 `models.yaml` 给 codex harness 配置 fallback_chains（minimax 429 → deepseek-v4-flash）。

### 8.7 协议兼容性矩阵未对外暴露

`HARNESS_PROTOCOLS` 是 harness 内部白名单，但 `model_config.yaml` 里没有"这个 provider 的 wire_api 是什么"的强校验——只在 harness run() 入口校验。**风险**：models.yaml 配错（如 wire_api=dashscope_native 但 harness=claude_code）报错时机晚。**建议**：在 `model_config.py` 加载时校验 provider.protocol × agent.harness 兼容性。

---

## 九、关键里程碑与决策归档

- **v84（Phase 1）**：harness 模块独立化、`AgentClient` 抽象稳定、register.py 注册表模式
- **v85（Phase 1.5）**：三层模型 Agent/Role/Workflow，harness 仍是 Agent 层概念
- **v88（H1）**：LocalLlmClient 独立槽位，不再借用 OPENCODE；新增 `HarnessType.LOCAL_LLM` + `HarnessTypeRef.LOCAL_LLM`；`models.yaml` 配置 `openai_compatible` provider 走 local_llm
- **D-022（v90+）**：codex harness 全路径 token 保护
- **D-030**：local_llm timeout 120s
- **D-031**：local_llm model ID 去前缀

---

## 十、给决策者的可执行项

### 高优先级（建议本周）

1. **删除 KIMI/HTTP harness 或绑定真实负载**——要么从 `register.py` 移除（减 11KB 死代码），要么绑定 1 个 agent（验证未实际用过）
2. **codex harness 加 fallback_chains**——`models.yaml` 给 4 个 codex agent 配置 minimax 429 → deepseek-v4-flash fallback
3. **修复 LOCAL_LLM 的 provider/protocol 强校验**——在 models.yaml 加载时校验 wire_api × harness 兼容性

### 中优先级（下个迭代）

4. **抽象 Tool Loop 协议**——把 LOCAL_LLM 的 max_rounds=8 循环抽到 `LLMToolLoop` 基类，让其他 harness 可选启用
5. **Thread Lease 升级到 Redis**——为多 worker 部署铺路（即使现在不上，未来 API server 横向扩展是必然）
6. **opencode session 自动清理 job**——`audit.patroller` 每 N 分钟扫 `~/.local/share/opencode/opencode.db`，删除 >7 天未用的 session

### 低优先级（按需）

7. **harness 性能基准**——为 7 种 harness 写一组 micro-benchmark（首 token latency / 全程 latency / tokens/$），帮助未来选型决策
8. **harness 健康检查 endpoint**——`GET /api/harness/health` 返回 7 种 harness 的可达性 + 最近一次成功时间（运维仪表盘用）

---

## 附录 A：调用链示意

```
DAG engine._run_agent_node(node)
  ├─ effective_harness = node.inline_agent.harness if node.inline_agent else node.harness
  ├─ harness = self._create_harness(effective_harness)
  │     └─ HarnessRegistry.create(effective_harness) → 工厂映射 → AgentClient 实例
  ├─ tools = _load_tools_for_node(node)            # 内置工具 + agent 声明的工具
  ├─ context = AgentRunContext(
  │     system_prompt, model, api_key, base_url,
  │     workspace, session_id, permission, abort_signal, ...
  │  )
  ├─ prompt = _resolve_template(node.prompt, inputs, global_inputs)
  └─ async for event in harness.run(prompt, tools, context):
        ├─ event.type == TEXT          → emit NODE_PROGRESS
        ├─ event.type == THINKING      → emit NODE_PROGRESS (kind=thinking)
        ├─ event.type == TOOL_USE      → emit NODE_TOOL_CALL
        ├─ event.type == TOOL_RESULT   → emit NODE_TOOL_RESULT
        ├─ event.type == USAGE         → accumulate usage
        ├─ event.type == ERROR         → 视情况 NODE_FAILED / RUN_FAILED
        └─ event.type == DONE          → finalize run_usage, emit NODE_COMPLETED
```

## 附录 B：13 个 agent × 7 种 harness 决策矩阵

```
              │ opencode │ claude_code │ codex │ kimi │ local_llm │ http │ determin │
──────────────┼──────────┼─────────────┼───────┼──────┼───────────┼──────┼──────────┤
log_analyst   │          │             │       │      │     ●     │      │          │
task_monitor  │          │             │       │      │     ●     │      │          │
smart_form    │          │             │       │      │     ●     │      │          │
smart_approval│          │             │       │      │     ●     │      │          │
manager       │          │             │       │      │     ●     │      │          │
task_planner  │          │             │       │      │     ●     │      │          │
──────────────┼──────────┼─────────────┼───────┼──────┼───────────┼──────┼──────────┤
coding_agent  │          │      ●      │       │      │           │      │          │
proposal_plan │          │      ●      │       │      │           │      │          │
quality_insp  │          │      ●      │       │      │           │      │          │
──────────────┼──────────┼─────────────┼───────┼──────┼───────────┼──────┼──────────┤
smart_analysis│          │             │   ●   │      │           │      │          │
smart_query   │          │             │   ●   │      │           │      │          │
smart_ops     │          │             │   ●   │      │           │      │          │
video_creator │          │             │   ●   │      │           │      │          │
──────────────┼──────────┼─────────────┼───────┼──────┼───────────┼──────┼──────────┤
合计          │    0     │      3      │   4   │  0   │     6     │  0   │    0     │
```

**注**：DETERMINISTIC 仅 workflow 测试用（`workflows/hello-world.yaml`），不算生产 agent。

---

> 本文档由综合分析员产出，配合 `docs/00-platform/harness-analysis/ANALYSIS-harness-three-contexts.md`（概念综述）使用
