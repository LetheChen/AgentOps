# AgentOps 架构分析报告

> **范围**：Manager Agent / Subagent / DAG 节点 业务关系 + 执行规则 + 业务逻辑 + 数据库表关系。
> **参考**：`docs/ANALYSIS-manager-subagent-dag-event-flow.md` 与 `docs/persistence-layer-analysis.md`（与 外部参考方案 思路对齐，但 AgentOps 实现细节与存储选型自成一派）。
> **报告日期**：2026-08-09

---

## 0. 一句话总结

AgentOps 是一个 **三层架构**的 AI Agent 编排平台：

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: Manager Agent（长寿命对话 Agent）                  │
│  - 1 个 / 用户会话，Codex / Opencode 子进程                  │
│  - 意图识别 + 路由 + 调 trigger_workflow / 调 read_skill       │
└──────────────────────────┬──────────────────────────────────┘
                           │ trigger_workflow(workflow_id, inputs)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: DagEngine（In-process 状态机）                     │
│  - 解析 workflows/<id>.yaml → WorkflowDefinition             │
│  - 拓扑分层 → 同层并行 → 跨层 barrier                        │
│  - 事件总线：DagEvent (business) + RawHarnessEvent (raw)      │
└──────────────────────────┬──────────────────────────────────┘
                           │ harness.run(prompt, tools, ctx)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Harness Adapter（即派即用执行器）                  │
│  - opencode / claude_code / codex / kimi / local_llm /       │
│    http / deterministic / conversational                    │
│  - 注册到 HarnessRegistry.create(HarnessType.X)              │
└─────────────────────────────────────────────────────────────┘
```

**关键边界**：
- **Manager Agent ≠ DAG 内的 subagent**。Manager 是会话级的长寿命 LLM 循环，subagent 是节点级的短寿命 LLM 循环（且大多数情况下 subagent 是 `local_llm` 直接 API 调用，没有独立进程）。
- **DAG 节点 ≠ subagent**。节点是拓扑位置，subagent 是执行该节点的 LLM 实例。一对一映射只在 `type: agent` 节点成立，virtual 节点（`parallel_branch` / `gateway`）不是 subagent。
- **会话与会话**通过 `parent_child_sessions` 表连接，事件流按 `session_id`（v2 统一 ID，原来叫 `run_id`）做时间序。

---

## 1. 角色业务定位

### 1.1 三层角色矩阵

| 角色 | 业务定位 | 数量级 | 进程边界 | LLM 上下文 |
|---|---|---|---|---|
| **Manager Agent** | 高层指挥，用户主对话入口 | 1 个 / 用户会话 | 1 个 Codex / Opencode 子进程（线程内 session_engine 调度） | 跨多轮对话 |
| **Subagent**（DAG 节点执行体）| 实际执行某道工序的 LLM Worker | N 个 / DAG run（N = `type: agent` 节点数） | 多数在 in-process Harness 子循环；少数走 Docker / 子进程（取决于 harness 类型） | 一次性，每节点一次调用 |
| **DAG 节点**（拓扑位置）| 调度图上的位置，描述"做什么" | 节点总数 | 0 进程（Manager 端状态机） | 0 |
| **Harness Adapter** | 执行节点的具体引擎实现 | 1 个 / DAG run 内同种 harness 共享 | 由 `HarnessRegistry.create()` 工厂化 | — |

### 1.2 一句话边界（来自代码）

> **Manager Agent = 指挥**。1 个长寿命 Codex / Opencode 子进程，通过 `trigger_workflow` 工具调用 `LocalSdkOrchestrator`。
> **DAG Engine = 调度图**。Manager 进程内的状态机，决定何时让哪个 node 进入 RUNNING。
> **Subagent = 执行**。图里 `kind: agent` 节点派发出来的 Harness Adapter，每个节点一个独立 LLM 调用。
> **DAG 非 agent 节点（parallel_branch / gateway）= 纯网关**。0 LLM 调用，是 DAG 状态机的内部节点。

### 1.3 长寿命 vs 短寿命 Agent 判定准则

| 场景 | 是否需要显式 Agent | Agent 类型 |
|---|---|---|
| DAG 中一个工序节点（采集/分类/汇总/校验） | ❌ 不需要 | 节点角色（由 YAML 内联） |
| 多轮 IDE 风格 coding 会话（持续编辑 + 用户中途插话） | ✅ 必须 | 长寿命 Agent |
| 独立业务入口（"直接查 vault" / "直接发邮件"，绕过 Manager） | ✅ 必须 | 长寿命 Agent |
| 跨 DAG 共享的"长记忆"角色（如 QA Reviewer 累积历史批注） | ✅ 必须 | 长寿命 Agent |
| 单纯工具调用型 worker（如 ssh 运维、单步 DB 查询） | ❌ 不需要 | 注册成 `tools/` 下的工具 |

按当前项目实际需要的"长寿命 Agent"清单：
- `manager`（已有，[config/agents/manager.yaml](config/agents/manager.yaml)）
- 业务域 Agent：`smart_query` / `smart_ops` / `smart_form` / `smart_analysis` / `smart_approval` / `video_creator` / `content_curator` / `log_analyst` / `weekly_reporter` / `quality_inspector` / `task_monitor` / `proposal_planner`
  - 大多数作为 DAG 节点 agent（短寿命），少部分支持直接独立会话（conversational 模式）
- `coding_agent`（规划中，多轮 IDE 对话）
- `knowledge_agent`（规划中，独立 vault 入口）

---

## 2. 进程 / LLM 上下文边界

| 角色 | 进程 | LLM 上下文 | 触发者 | 终止条件 |
|---|---|---|---|---|
| Manager Agent | 1 个 Codex / Opencode 子进程 | 1 个 SessionEngine 多轮对话 | 用户 / UI | 用户离开 / 服务重启 / idle 超时（120s 转 dormant） |
| Subagent（type: agent 节点） | 由 harness 决定（多数 in-process） | 1 个 Harness.run() 调用 | DagEngine 在节点 RUNNING 时 | `handoff` 成功 / 异常退出 / 节点 timeout |
| virtual 节点（parallel_branch / gateway） | 无（纯内存） | 0 | `run_node()` 内分支 | 完成聚合 / 完成路由 |
| HIL（widget input） | 无 | 0 | 用户在 UI 上提交表单 | 用户提交 / widget timeout |

**关键代码定位**：
- Manager Agent 入口：[orchestrator/manager.py:41](orchestrator/manager.py#L41)（`ManagerAgent`）
- Session Engine（Thread 模式）：[orchestrator/session_engine.py:50](orchestrator/session_engine.py#L50)（`SessionEngine`）
- Orchestrator 协议：[orchestrator/protocol.py:153](orchestrator/protocol.py#L153)（`Orchestrator` ABC）
- DagEngine：[workflow/engine.py:185](workflow/engine.py#L185)
- HarnessRegistry：[harness/](harness/)（各 adapter 注册到工厂）

---

## 3. 跨边界通信通道

| 起点 | 终点 | 通道 | 协议 |
|---|---|---|---|
| UI / 用户 | Manager Agent | HTTPS (SSE) | [api/server.py](api/server.py) `/api/v2/sessions/...` |
| Manager Agent | LocalSdkOrchestrator | In-process Python | `trigger_workflow` 工具 → `orch.run(RunRequest)` |
| LocalSdkOrchestrator | DagEngine | In-process | `engine.run(inputs)` / `engine.resume(run_id, inputs)` |
| DagEngine | Harness Adapter | In-process | `harness.run(prompt, tools, ctx)` 返回 `AsyncIterator[AgentEvent]` |
| DagEngine → UI | SSE | [events/bus.py](api/server.py) SSE endpoint | `DagEvent`（business）+ `RawHarnessEvent`（raw 双通道） |
| Manager → Subagent（跨域） | In-process | `request_cross_domain` 工具 + [CrossDomainCoordinator](orchestrator/cross_domain.py) | 12 步跨域事件流 |

---

## 4. 身份 / 寿命映射

| 标识 | 寿命 | 跨 run 复用？ | 含义 |
|---|---|---|---|
| `session_id`（v2 统一 ID）| 整个 session | 不变（除非 idle 转 dormant 后重新唤醒） | 1 次业务会话（conversational）或 1 次 DAG run（templated） |
| `run_id`（v1 旧名，已废弃）| = session_id | — | 见 [audit/store.py:11-13](audit/store.py#L11-L13) — `DagEvent.run_id` 字段名保留，值是 session_id |
| `workflow_id` | 整个生命周期 | 不变 | YAML 文件名（如 `weekly-report.yaml` → `workflow_id: weekly-report`） |
| `node_id` | 整个 DAG run | 不变 | YAML 节点名（如 `classify` / `validate`） |
| `agent_id` | 注册期永久 | 不变 | `config/agents/*.yaml` 文件名（如 `weekly_reporter`） |
| `turn_id` | 1 次 LLM 调用 | 每轮新生成 | Thread 模式：`turn_{uuid4().hex[:8]}` |
| `actor_id` | 无显式 actor | — | AgentOps 没有 外部参考方案 那种 `actor_id` 概念；actor = node（节点寿命 = DAG run 寿命） |

**注**：AgentOps 的 actor 身份通过 `node_id`（在 DAG 内）+ `agent_id`（跨 run 复用同一 agent yaml）联合表达。外部参考方案 的 `actor_id` 概念不适用，因为 AgentOps 没有 Docker Worker 抽象。

---

## 5. DAG 执行规则（核心）

### 5.1 状态机

节点状态（[orchestrator/protocol.py:41-50](orchestrator/protocol.py#L41-L50)）：

```
PENDING ──入度=0──▶ READY ──run_node()──▶ RUNNING ──完成──▶ COMPLETED
                            │                  │
                            │                  ├── 异常 ──▶ FAILED
                            │                  │
                            │                  └── skip_if=true ──▶ SKIPPED
                            │
                            └─ 等 widget.input ──▶ WAITING ──▶ READY
```

Run 状态（[orchestrator/protocol.py:29-39](orchestrator/protocol.py#L29-L39)）：

```
PENDING → RUNNING → COMPLETED
                  → FAILED
                  → CANCELLED
   ACTIVE（conversational 进行中）→ DORMANT（idle 120s）
```

### 5.2 节点生命周期（[workflow/engine.py:425-458](workflow/engine.py#L425-L458)）

每节点一次 `_run_node(node_id, global_inputs)`：

```
1. status in (SKIPPED/FAILED/COMPLETED)? → 早退
2. node.skip_if 评估 → true → SKIPPED + 标记下游 SKIPPED
3. _try_restore_node() 检查 output_files 是否都在 → 在 → 收割 + 标记 COMPLETED（断点恢复）
4. node.type 分发：
   ├─ PARALLEL_BRANCH → _run_parallel_branch_node（virtual 聚合）
   ├─ GATEWAY        → _run_gateway_node（virtual 路由）
   └─ default        → _run_agent_node（真 LLM 调用）
5. _finalize_completed_node：
   ├─ status = COMPLETED
   ├─ 累计 tokens / cost
   ├─ 触发 widgets emit
   ├─ handoff 路由到下游节点
   ├─ BLOCKED 检测（所有 output port 都 BLOCKED → FAILED）
   └─ emit NODE_COMPLETED
6. 用量落库（usage_records）
```

### 5.3 派发流程（[_run_agent_node](workflow/engine.py#L647-L739)）

```
_run_agent_node:
  status = RUNNING
  emit NODE_STARTED + widget.update (node.started event)
  
  # 收集输入
  for dep in node.after:                    # 上游 output 作为 downstream input
    nstate.upstream_outputs[dep] → nstate.current_inputs
  for input_name in node.inputs:            # 全局 input 名
    if in global_inputs: nstate.current_inputs[input_name] = ...
  
  try:
    _execute_node(node, nstate, global_inputs)
  except Exception as e:
    error_type = _classify_error_type(e)
    
    # D-029：rate_limit / timeout → 尝试 fallback provider
    fallback_resolved = _try_resolve_fallback(node, error_type, current_provider)
    if fallback_resolved:
      重置 nstate 状态 → 用 fallback provider 重试
      return  # 重试成功直接返回
    
    # 否则：emit NODE_FAILED + raise
    status = FAILED
    emit NODE_FAILED { error, agent, provider, model, error_type }
    raise
```

### 5.4 handoff 路由（[_finalize_completed_node](workflow/engine.py#L511-L587)）

每个节点声明 `outputs: { <port>: { to: "<next>.in:<port>" | [...] } }`。完成时引擎：

```
for port, route in node.outputs.items():
  payload = nstate.pending_handoffs[port]
  for target, target_port in route.parse_all():        # 支持多消费者广播
    if target_node exists:
      target_state.upstream_outputs[source][incoming_port] = payload
      
      emit NODE_HANDOFF:
        from, to, port, incoming_port, payload_size,
        from_role, to_role, summary                     # M3 协作可视化
```

**端口命名约定**（[orchestrator/protocol.py](orchestrator/protocol.py) + [workflow/yaml 约定](.claude/rules/workflow-yaml.md)）：
- 单目标：`to: "next_node_id"` 或 `to: "next_node_id.in:port_name"`
- 多目标：`to: ["a.in:p1", "b.in:p2"]`（一个 port 广播到多个下游）
- 无 port：默认 port 名为 `default`

### 5.5 skip_if 条件评估（[_eval_skip_if](workflow/engine.py#L370-L423)）

```yaml
# YAML
notify:
  after: [report]
  skip_if: "{{not report.critical_summary}}"
```

支持格式：
- `{{not node.port}}` → 取 `node.port` 的 handoff content 取反
- `{{node.port}}` → 取值
- 值类型分派：
  - bool：直接返回
  - 字符串：先匹配显式关键字（true/1/yes/pass/passed/false/0/no/skip/none/null/""），未命中按"非空即 True"（适用于 markdown 摘要）
  - 其他：`bool(content)`

### 5.6 BLOCKED 检测（[_is_blocked_payload / _all_ports_blocked](workflow/engine.py#L460-L509)）

每个节点的 `nstate.pending_handoffs` 中所有 output port 都以 BLOCKED 信号收尾时：
- 节点标 FAILED（替代 COMPLETED）
- 终止 BFS 循环停止下游传播
- 避免"agent 报告 BLOCKED 但 run 仍 COMPLETED"的乐观失败

BLOCKED 检测两种模式：
1. JSON 结构化信号：`{"status": "BLOCKED"/"ERROR"/"FAILED"}`
2. 文本强声明：含 `— BLOCKED` / `hard-BLOCKED` / `cannot proceed` / `不能继续`

### 5.7 文件收割（[_harvest_file_outputs](workflow/engine.py#L1517-L1563)）

**适用场景**：opencode / codex / local_llm harness 通过 `Write` / `write_file` 工具把产物写到 `workspace/<workflow_id>/<run_id>/` 下。

`agent yaml` 声明 `output_files: { <port>: "<file_path_template>" }`（如 [config/agents/weekly_reporter.yaml:275-293](config/agents/weekly_reporter.yaml#L275-L293)）。harness.run() 完成后：

```
for port, pattern in agent.output_files.items():
  if port not in node.outputs: continue
  file_path = pattern.replace("{{workspace.root}}", ws_root)
                     .replace("{{run_id}}", run_id)
  if path.is_dir():
    pending_handoffs[port] = {"content": str(path), "summary": "directory: ..."}
  elif path.exists():
    content = path.read_text(...)
    pending_handoffs[port] = {"content": content, "summary": content[:200], "source_file": str(path)}
  else:
    logger.warning("expected file not found, port %s skipped")
```

**断点恢复**（[_try_restore_node](workflow/engine.py#L589-L632)）：如果所有 `output_files` 都已存在 → 直接从文件恢复 `pending_handoffs` → 节点标记 COMPLETED → 不实际调用 LLM。配合 `engine.resume(run_id, inputs)` 可实现"复用 run_id，跳过已完成节点"。

### 5.8 Fallback provider 切换（D-029，[workflow/engine.py:741-793](workflow/engine.py#L741-L793)）

节点失败时若错误类型属于 fallback 触发条件：
- `rate_limit` / `timeout`（provider 临时不可用）
- `FallbackChain` 配置了当前 provider 的 fallback

则切换 fallback provider 重试一次。**不触发**：`auth_error` / `protocol_mismatch` / `not_found` / `unknown`（这些是配置问题，切 provider 救不了）。

### 5.9 BLOCKED + 用量落库 + 终止

```
if _all_ports_blocked(nstate):                              # BLOCKED 检测
  status = FAILED
  emit NODE_FAILED { error: "所有输出端口均返回 BLOCKED/ERROR 信号，无实际产出",
                    error_type: "execution_blocked" }

# 用量落库
if self._event_store and nstate.resolved_model:
  await _record_node_usage(node_id, nstate)                # → usage_records 表
```

---

## 6. Workflow YAML 三层校验（[workflow/validator.py](workflow/validator.py)）

### Layer 1: 结构校验（7 项规则）

1. 每个 `after` 目标必须存在
2. 每个 output 目标必须存在（支持多消费者）
3. `type: agent` 节点必须有 `agent` 字段
4. `type: gateway` 必须有 `gateway_kind` + `condition`
5. `type: parallel_branch` 必须声明 `branches` 列表，分支必须存在
6. 无环（Kahn 算法拓扑排序）
7. `widget_inputs` 的 `from_widget` / `to_node` 必须存在

### Layer 2: 语义校验（5 项规则）

8. output port 名与下游 input 声明不匹配 → warning
9. widget `emit_on_event` 必须在 [DagEventType](orchestrator/protocol.py) 枚举中
10. 非 deterministic harness 节点必须有 `agent_id`
10.1. v2.1 三层模型：`role_prompt` 必须依附 `agent`（角色不能脱离能力载体）
11. **跨文件语义**（需 `agent_configs` 参数）：agent 存在性
12. **跨文件语义**：agent system_prompt 含 `node_id == "<nid>"` 字面量 → 否则 warning（agent 不知道该节点该干什么）
13. **跨文件语义**：agent `output_files` 必须包含 node outputs 所有 port

### Layer 3: 图论校验

14. 所有分支必须能到达终止节点（无 output 的节点）
15. `skip_if` 引用的 `{{node.port}}` 必须在上游 output 中声明

---

## 7. RunMode 谱系（[orchestrator/protocol.py:52-58](orchestrator/protocol.py#L52-L58)）

```
CONVERSATIONAL → TASK → HYBRID → TEMPLATED
   单 Agent        线性    DAG+内嵌   YAML 预定义
   + ReAct         todo    对话子循环   拓扑
   无拓扑
```

| RunMode | 入口 | 引擎 | 适用场景 |
|---|---|---|---|
| `conversational` | `LocalSdkOrchestrator._run_conversational()` | `SessionEngine` / `ConversationalEngine` | 单 Agent 多轮对话（如直接和 manager 聊天） |
| `task` | 同上 | 同上（+ todo 列表） | 多步骤但无依赖的线性 todo |
| `hybrid` | `DagEngine._run_node` 识别 `harness == CONVERSATIONAL` | DAG 主循环 + 内嵌对话子循环 | DAG 节点内需要多轮对话 |
| `templated` | `DagEngine.run()` | `DagEngine` | 静态 YAML 工作流（最常见） |

**判断逻辑**（[orchestrator/local_sdk.py:113-116](orchestrator/local_sdk.py#L113-L116)）：

```python
if req.run_mode in (RunMode.CONVERSATIONAL, RunMode.TASK):
    return await self._run_conversational(req, run_id)
else:
    return await self._run_templated(req, run_id)
```

---

## 8. 业务逻辑：Manager → Subagent 派发链路

### 8.1 用户消息 → DAG run 完整路径

```
1. 用户消息 → API endpoint /api/v2/sessions/.../turns
   ↓
2. SessionEngine.start_turn(message)
   ├─ 持久化 user message → session_messages
   ├─ emit TURN_STARTED
   ├─ 创建 harness client（opencode/codex/local_llm）
   ├─ 构造 tools（make_conversational_tools + agent allowed_tools）
   ├─ 构造 context（system_prompt + workflow registry + skill registry + tools_prompt + memory）
   └─ 调 harness.run(message, tools, context)
   ↓
3. Manager LLM 决定调 trigger_workflow 工具
   ├─ args: { workflow_id, inputs, run_mode, agent_id?, initial_message? }
   ├─ 走 [tools/trigger_workflow.py:trigger_workflow](tools/trigger_workflow.py#L32)
   │   ├─ validate workflow_id 存在
   │   ├─ 构造 RunRequest
   │   ├─ orch.run(req) → RunHandle{run_id, workflow_id, started_at, cancel_token}
   │   ├─ event_store.init_session(session_id=run_id, workflow_id, run_mode, agent_id, inputs)
   │   ├─ event_bridge.start(run_id) → 异步转发事件到 SSE 队列
   │   ├─ event_store.record_parent_child(parent_session_id, child_session_id=run_id, ...)
   │   ├─ session_mgr.attach_run(parent_session_id, run_id, workflow_id)
   │   └─ memory_manager.summarize_run（轮询完成 → 摘要回灌）
   └─ 返回 { run_id, workflow_id, status: "started", _parent_run_id }
   ↓
4. Manager 立即用 emit_widget 把 run_id 推给前端组件面板
5. Manager 调 collect_child_result({run_id}) 阻塞等待子任务终态
6. 拿到 messages + summary + final_outputs → 整合告诉用户
```

### 8.2 反向链路：用户中断 → Manager → DAG 节点干预

```
用户 HIL 表单提交 → POST /api/v2/sessions/.../widget-input
  ↓
event_store.append_widget_input(session_id, widget_id, payload)
  ↓
若 widget_input 配置了 to_node + to_input：
  - 注入到对应节点的 waiting queue
  - 节点 status 从 WAITING → READY → RUNNING
```

---

## 9. 工具链与权限

### 9.1 DAG 内置工具（[workflow/engine.py:104-151](workflow/engine.py#L104-L151)）

每个 agent 节点自动获得：

| 工具 | 作用 | input_schema |
|---|---|---|
| `handoff` | 发送产物到下游节点的指定 port | `{port: string, content: any, summary?: string}` |
| `graph_context` | 查看上游节点 outputs + 当前节点 inputs | `{}` |

### 9.2 agent 允许的工具（[orchestrator/config_loader.py](orchestrator/config_loader.py)）

agent yaml `permissions.allowed_tools` 声明工具白名单。`LocalSdkOrchestrator` 通过 `make_conversational_tools` / `_load_agent_extra_tools` 装载：

- **内置工具**（`make_conversational_tools`）：`emit_widget` / `todo` / `finalize` / `request_human_input`
- **配置工具**（`config/tools/*.yaml`）：`log_query` / `wecom_notify` / `trigger_workflow` / `request_cross_domain` / `obsidian_vault` / `ingest_source` / `query_knowledge` 等
- **Harness 内置工具**（由 opencode / codex 子进程自身实现）：`read_file` / `write_file` / `bash`（在 local_llm harness 下由 engine 注入 Python 实现，详见 [workflow/engine.py:1303-1400](workflow/engine.py#L1303-L1400)）

**最小权限原则**：`manager` 看到 `trigger_workflow`，普通子 agent 看不到（通过 `agent_id` 过滤）。

### 9.3 PermissionEngine（[orchestrator/permission_engine.py](orchestrator/permission_engine.py)）

- 纯配置对象无 IO，缓存到 `dag._permission_engine`（dag 级共享）
- PermissionEngine 在 DAG 派发前根据 `node.agent` 的 `permissions.allowed_tools` / `denied_tools` 校验工具调用

### 9.4 CrossDomainCoordinator（[orchestrator/cross_domain.py](orchestrator/cross_domain.py)）

跨域请求（如 `request_cross_domain(target_domain="video_production")`）走 12 步事件流：
1. agent 调 `request_cross_domain`
2. CrossDomainCoordinator 闭包注入 `caller_agent` + `coordinator` + `parent_run_id`
3. 跨域子任务作为独立 run 启动
4. 事件流带 `cross_domain` 标记
5. 结果回灌到 caller

**DynamicDagSpec**（[orchestrator/dynamic_dag.py](orchestrator/dynamic_dag.py)）：单域无固定模板时 Manager 动态生成 1 节点 DAG；多域无固定模板时 fallback 到 manager 域。

---

## 10. Skill 激活机制（v2.1 三层知识分离）

### 10.1 三层模型

| 层 | 回答 | 存储 | 加载时机 |
|---|---|---|---|
| **Skill** | "怎么操作"（通用流程文档） | `skills/<id>/SKILL.md`（frontmatter + body） | 启动时 `SkillRegistry.scan()` 构建 metadata 索引，运行时 `read_skill(skill_id)` 按需读 body |
| **Knowledge Base** | "具体怎么做"（领域知识 + 提示词模板） | `config/knowledge/<domain>/` | 节点内 `query_knowledge` 工具按需查 |
| **Workflow YAML** | "执行什么"（编排实例） | `workflows/*.yaml` | Manager `trigger_workflow(workflow_id)` 按需 dispatch |

### 10.2 SkillRegistry（[orchestrator/skill_registry.py](orchestrator/skill_registry.py)）

```python
@dataclass
class SkillMeta:
    id: str                # "dag-ops"（来自目录名）
    name: str              # frontmatter name
    description: str       # 一句话说明
    domain: str            # "_shared" / "video_production" / ...
    depends_on: list[str]  # 依赖的其他 skill
    content: str           # 完整 body（启动时驻留内存）
```

**核心约定**：
- skill body **不全量 inline** 到 system_prompt（节省 token）
- system_prompt 只注入 metadata 列表（`id + description + domain`）
- LLM 按需调 `read_skill(skill_id)` 加载完整 body
- `_shared` 域的 skill 所有 agent 可见；其他域只对匹配域的 agent 可见

**注入位置**（[orchestrator/session_engine.py:155-174](orchestrator/session_engine.py#L155-L174)）：`SessionEngine.start_turn()` 在构造 system_prompt 时通过 `_registry.get_skill_registry().build_prompt_section(agent_domain)` 注入。

---

## 11. 数据库表关系（[audit/store.py](audit/store.py)）

### 11.1 表 ER 全景

```
sessions (核心表，吸收原 runs 表功能)
  ├─ dag_events (1:N)                    # 业务事件流
  ├─ raw_harness_events (1:N)            # 原始 harness 事件（双通道）
  ├─ widget_inputs (1:N)                 # HIL 介入点
  ├─ usage_records (1:N)                 # 用量明细（按节点）
  ├─ session_messages (1:N)              # Thread 模式消息持久化
  ├─ session_events (1:N)                # Thread 模式事件流
  ├─ session_memory (1:N)                # 会话期记忆
  └─ parent_child_sessions (1:N via parent_session_id / child_session_id)

lint_issues (独立表，知识库 lint 检查)
```

### 11.2 核心表 schema

#### sessions（吸收原 runs 表，v2 统一架构）

```sql
CREATE TABLE sessions (
    session_id          TEXT PRIMARY KEY,
    user_id             TEXT,
    agent_id            TEXT,                    -- NULL（templated DAG 是 workflow 级别）
    status              TEXT NOT NULL DEFAULT 'active',  -- active/dormant/running/completed/failed/cancelled/archived
    title               TEXT,
    workflow_id         TEXT,                    -- templated/hybrid 模式
    run_mode            TEXT NOT NULL DEFAULT 'conversational',  -- conversational/templated/hybrid/task
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    last_activity_at    TEXT NOT NULL,
    archived_at         TEXT,
    total_tokens_input  INTEGER DEFAULT 0,
    total_tokens_output INTEGER DEFAULT 0,
    total_cost_usd      REAL DEFAULT 0.0,
    error               TEXT,
    inputs              JSON,
    final_outputs       JSON,
    message_count       INTEGER DEFAULT 0,
    attached_run_count  INTEGER DEFAULT 0,       -- trigger_workflow 派发子任务计数
    thread_id           TEXT,                    -- Thread 模式（opencode/codex thread 复用）
    thread_name         TEXT,
    thread_tool_digest  TEXT,
    voice_active        INTEGER DEFAULT 0,
    metadata            JSON
);
```

**索引**：
- `(user_id)` / `(status)` / `(workflow_id)` / `(run_mode)`

**v1 → v2 迁移**（[audit/store.py:5-14](audit/store.py#L5-L14)）：
- 删除 `runs` / `messages` / `parent_child_runs` 旧表
- `sessions` 表扩展吸收 `runs` 表功能（workflow_id / run_mode / inputs / final_outputs）
- `run_id` 列统一改为 `session_id`
- `parent_child_runs` → `parent_child_sessions`
- `session_memory.source_run_id` → `session_memory.source_session_id`
- 方法名：`init_run` → `init_session`、`finalize_run` → `finalize_session`、`get_run_summary` → `get_session_summary`
- 删除 `append_message` / `get_messages` / `delete_run` / `list_runs_by_session`

#### dag_events（业务事件流）

```sql
CREATE TABLE dag_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    event_type  TEXT NOT NULL,                    -- run.created/node.started/...
    node_id     TEXT,
    payload     JSON NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);
```

**索引**：
- `(session_id)` / `(session_id, node_id)`

**写路径**：[workflow/engine.py:_emit](workflow/engine.py#L214-L223) → `event_sink(ev)` → [audit/store.py:append_dag_event](audit/store.py#L538-L550) → `INSERT OR IGNORE`。

**幂等**：`UNIQUE(session_id, sequence)` + `INSERT OR IGNORE` 保证重放不重复。

#### raw_harness_events（双通道原始事件）

```sql
CREATE TABLE raw_harness_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    node_id      TEXT,
    harness      TEXT NOT NULL,                    -- "opencode" / "claude_code" / "codex" / ...
    event_type   TEXT NOT NULL,                    -- vendor native event type
    raw_payload  JSON NOT NULL,                    -- 脱敏后
    received_at  TEXT NOT NULL
);
```

**写路径**：[workflow/engine.py](workflow/engine.py) → `RawHarnessEvent` → `event_store.append_raw_event(ev)`。
**脱敏**：[audit/store.py:_redact_value](audit/store.py#L213-L237) 递归遮蔽 `api_key` / `authorization` / `bearer` / `secret` / `password` / `token` 等字段。

#### widget_inputs（HIL 介入点）

```sql
CREATE TABLE widget_inputs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    widget_id     TEXT NOT NULL,
    input_payload JSON NOT NULL,
    user_id       TEXT,
    submitted_at  TEXT NOT NULL
);
```

#### usage_records（节点级用量明细）

```sql
CREATE TABLE usage_records (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT NOT NULL,
    node_id               TEXT NOT NULL,
    provider_id           TEXT NOT NULL,
    model                 TEXT NOT NULL,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    duration_ms           INTEGER DEFAULT 0,
    cost_usd              REAL DEFAULT 0.0,
    created_at            TEXT NOT NULL
);
```

**写路径**：[workflow/engine.py:_record_node_usage](workflow/engine.py#L1103-L1125) → `event_store.record_usage(session_id, node_id, provider, model, ...)`。
**定价**：[orchestrator/model_config.py:get_price](orchestrator/model_config.py) 返回 `(input_price_per_1k, output_price_per_1k)`，cost = `tokens_in / 1000 * in_price + tokens_out / 1000 * out_price`。

#### parent_child_sessions（父子会话关系）

```sql
CREATE TABLE parent_child_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_session_id   TEXT NOT NULL,
    child_session_id    TEXT NOT NULL,
    agent_id            TEXT,
    workflow_id         TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE(parent_session_id, child_session_id)
);
```

**写路径**：[tools/trigger_workflow.py:139-148](tools/trigger_workflow.py#L139-L148) → `event_store.record_parent_child(parent_session_id, child_session_id, agent_id, workflow_id)`。
**幂等**：`UNIQUE` 冲突时 `ON CONFLICT DO UPDATE SET agent_id=excluded.agent_id, workflow_id=excluded.workflow_id, created_at=excluded.created_at`。

#### session_memory（会话期记忆）

```sql
CREATE TABLE session_memory (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL,
    memory_type         TEXT NOT NULL,           -- run_summary / topic_summary / user_preference
    source_session_id   TEXT,                    -- 来源 session（run_summary 类型时必填）
    content             TEXT NOT NULL,
    tokens              INTEGER DEFAULT 0,
    importance          REAL DEFAULT 0.5,
    created_at          TEXT NOT NULL,
    expires_at          TEXT,
    UNIQUE(session_id, memory_type, source_session_id)
);
```

**索引**：
- `(session_id)` / `(session_id, importance DESC)`

**写路径**：[tools/trigger_workflow.py:_summarize_run_on_completion](tools/trigger_workflow.py#L186-L238) → 轮询 run 完成 → 生成摘要 → `memory_manager.summarize_run(session_id, run_id, workflow_id, run_events)` → `add_session_memory`。

#### session_messages（Thread 模式消息持久化）

```sql
CREATE TABLE session_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    role            TEXT NOT NULL,                 -- user / assistant / system
    content         TEXT NOT NULL,
    turn_id         TEXT,                          -- 所属 turn（user+assistant 成对）
    message_type    TEXT DEFAULT 'text',           -- text / transcript / tool_result / error
    metadata        JSON,
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);
```

**索引**：
- `(session_id)` / `(session_id, turn_id)`

**写路径**：[orchestrator/session_engine.py:108-110](orchestrator/session_engine.py#L108-L110) → `event_store.append_session_message(session_id, "user", message, turn_id=turn_id)`。

**content 序列化**：[audit/store.py:1219-1242](audit/store.py#L1219-L1242) — `dict` / `list` 自动 JSON 序列化；读回时 [audit/store.py:1255-1260](audit/store.py#L1255-L1260) 解析 JSON 还原。

#### session_events（Thread 模式事件流）

```sql
CREATE TABLE session_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    node_id         TEXT,
    payload         JSON NOT NULL,
    occurred_at     TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);
```

**与 `dag_events` 的关系**：两者都按 `(session_id, sequence)` 持久化，但事件类型不同：
- `dag_events` 偏向 DAG 模式（`run.created` / `node.started` / `node.handoff` 等）
- `session_events` 偏向 Thread 模式（`turn.started` / `turn.progress` / `turn.completed`）

实际写入路径上，Thread 模式走 `session_events`，DAG 模式走 `dag_events`，两者并存。

#### lint_issues（知识库 lint）

```sql
CREATE TABLE lint_issues (
    id              TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    type            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    page_a          TEXT,
    page_b          TEXT,
    description     TEXT NOT NULL,
    auto_fixable    INTEGER DEFAULT 0,
    detected_at     TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',        -- pending/resolved/ignored
    resolved_at     TEXT,
    resolved_by     TEXT,
    resolution_note TEXT,
    UNIQUE(domain, type, COALESCE(page_a, ''), COALESCE(page_b, ''))
);
```

**写路径**：`append_lint_issue` 幂等：UNIQUE 冲突时仅更新 `detected_at` + `status='pending'`（重新打开 issue）。

### 11.3 SQLite 配置

```python
# [audit/store.py:506-510](audit/store.py#L506-L510)
self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
self._conn.row_factory = sqlite3.Row
self._conn.execute("PRAGMA journal_mode=WAL")           # WAL 模式（并发读 + 单写）
self._conn.execute("PRAGMA synchronous=NORMAL")          # 性能 vs 安全折中
```

**线程安全**：sqlite3 连接虽设 `check_same_thread=False` 允许跨线程使用，但连接本身不是线程安全的。Patroller 后台 task 与 lifespan 并发访问同一连接会触发 `bad parameter or other API misuse`。用 `threading.Lock`（`self._db_lock`）保护所有 execute 调用（在 `asyncio.to_thread` 的 worker 线程中生效）。

### 11.4 数量级断言（实跑数据，2026-08-09）

| 维度 | 当前值 |
|---|---|
| 活跃 session 数（`status IN ('running', 'active')`） | `list_active_sessions()` 返回 ≤ 200（按 started_at DESC） |
| 30 天用量 | `get_usage_summary(days=30)` 按日 + provider 聚合 |
| skill 数量 | `SkillRegistry.scan()` 扫描 `skills/<id>/SKILL.md` |
| agent 数量 | `config/agents/*.yaml` 共 12 个（manager + 11 个业务域） |
| workflow 数量 | `workflows/*.yaml` 共 8 个（hello-world / travel-expense / log-patrol / task-patrol / weekly-report / video-pipeline / content-curation / example-greet / hybrid-test） |

---

## 12. 关键路径串联

### 12.1 Manager 启动一次 DAG（[tools/trigger_workflow.py](tools/trigger_workflow.py)）

```
dag_events(session_id=run_id, sequence=N, event_type="run.created", node_id=null, payload={workflow_id, inputs, layout})
dag_events(session_id=run_id, sequence=N+1, event_type="node.started", node_id="scan", payload={agent, harness})
...
dag_events(session_id=run_id, sequence=M, event_type="node.completed", node_id="scan", payload={outputs, tokens})
dag_events(session_id=run_id, sequence=M+1, event_type="run.completed", node_id=null, payload={duration_ms, tokens})

usage_records(session_id=run_id, node_id="scan", provider_id="minimax", model="MiniMax-M3", input_tokens=…, output_tokens=…, duration_ms=…, cost_usd=…)

sessions(session_id=run_id, status="running", ...) → finalize → status="completed", final_outputs={...}

parent_child_sessions(parent_session_id=<manager_session>, child_session_id=run_id, workflow_id="log-patrol", agent_id="log_analyst")

session_memory(session_id=<manager_session>, memory_type="run_summary", source_session_id=run_id, content="扫描到 24h 日志 N 条，重大问题 M 条...", importance=0.8)  ← 摘要回灌
```

### 12.2 Subagent 上报一次活动

```
DagEngine agent.run() → harness emits AgentEvent
↓
engine._emit(DagEventType.NODE_PROGRESS, node_id, {agent_text: ...})  → event_sink
↓
[api/server.py] SSE 推给前端
↓
audit/store.py:append_dag_event(INSERT OR IGNORE into dag_events)
```

### 12.3 HIL 介入

```
用户在 UI 上提交 form widget
↓
POST /api/v2/sessions/<sid>/widget-input
↓
widget_inputs(session_id=sid, widget_id="hil_3", input_payload={...}, user_id="user1", submitted_at=...)
↓
SessionEngine 把 widget_input 投递到对应节点 waiting queue
↓
节点 status WAITING → READY → RUNNING
↓
emit TURN_PROGRESS / NODE_PROGRESS（带 widget_input 反馈）
```

---

## 13. 设计特征

1. **统一 session 表 + 业务子表**：[sessions](audit/store.py#L40) 是核心表，`dag_events` / `session_events` / `widget_inputs` / `usage_records` / `session_messages` / `session_memory` 全部 FK 到 `sessions.session_id`。`status` 字段覆盖 `active/dormant/running/completed/failed/cancelled/archived` 全谱系。
2. **三层知识分离**（v2.1）：Skill（操作文档）/ Knowledge Base（领域知识）/ Workflow YAML（编排实例）。Skill 不全量 inline 到 system_prompt，LLM 按需调 `read_skill(skill_id)`。
3. **三段式 system_prompt**（v2.1）：`role_prompt`（节点级角色，可变）→ `agent system_prompt`（能力层，跨 workflow 固定）→ 工具/记忆注入（动态）。
4. **强校验 DAG YAML**：[workflow/validator.py](workflow/validator.py) 三层校验（结构 + 语义 + 图论），15 项规则。
5. **断点恢复**：[workflow/engine.py:_try_restore_node](workflow/engine.py#L589-L632) — 文件驱动的天然检查点（output_files 已存在 → 跳过 LLM 调用）。
6. **Fallback provider**（D-029）：节点失败时若错误属于 `rate_limit`/`timeout`，自动切换 FallbackChain 配置的 provider 重试一次。
7. **BLOCKED 检测**：[workflow/engine.py:_all_ports_blocked](workflow/engine.py#L496-L509) — 所有 output port 都以 BLOCKED 信号收尾 → 节点 FAILED → 停止下游传播。
8. **事件总线 dedup**：[audit/store.py:_SCHEMA](audit/store.py#L37) — `UNIQUE(session_id, sequence) + INSERT OR IGNORE` 保证重放不重复。
9. **WAL + 双连接锁**：SQLite WAL 模式 + `asyncio.Lock` + `threading.Lock` 双重保护，避免 Patroller 与 request handler 并发写冲突。
10. **线程池并发**：[workflow/engine.py:_topological_levels](workflow/engine.py#L342-L361) + `asyncio.gather` — 同层节点并行执行，跨层 barrier。
11. **idempotent insert**：`parent_child_sessions` / `lint_issues` / `session_memory` 都用 `ON CONFLICT … DO UPDATE` 保证重放幂等。
12. **冷启动恢复**（todo）：DB 在 → 重启后从 sessions 表 + usage_records 重建运行队列。

---

## 14. 关键代码定位

| 关注点 | 文件 / 行 |
|---|---|
| Manager Agent 入口 | [orchestrator/manager.py:41](orchestrator/manager.py#L41)（ManagerAgent） |
| Orchestrator 协议 | [orchestrator/protocol.py:153](orchestrator/protocol.py#L153)（Orchestrator ABC） |
| LocalSdkOrchestrator 实现 | [orchestrator/local_sdk.py:72](orchestrator/local_sdk.py#L72) |
| SessionEngine（Thread 模式） | [orchestrator/session_engine.py:50](orchestrator/session_engine.py#L50) |
| DagEngine | [workflow/engine.py:185](workflow/engine.py#L185) |
| 节点状态机 | [workflow/engine.py:425-458](workflow/engine.py#L425-L458)（`_run_node`） |
| handoff 路由 | [workflow/engine.py:511-587](workflow/engine.py#L511-L587)（`_finalize_completed_node`） |
| skip_if 评估 | [workflow/engine.py:370-423](workflow/engine.py#L370-L423) |
| BLOCKED 检测 | [workflow/engine.py:460-509](workflow/engine.py#L460-L509) |
| Fallback provider | [workflow/engine.py:741-793](workflow/engine.py#L741-L793) |
| 文件收割 | [workflow/engine.py:1517-1563](workflow/engine.py#L1517-L1563) |
| 断点恢复 | [workflow/engine.py:589-632](workflow/engine.py#L589-L632) |
| Workflow YAML 加载 | [workflow/loader.py](workflow/loader.py) |
| Workflow YAML 校验 | [workflow/validator.py](workflow/validator.py) |
| Workflow schema 定义 | [workflow/schema.py](workflow/schema.py) |
| M3 业务角色解析 | [workflow/collaboration.py:15](workflow/collaboration.py#L15)（resolve_business_role） |
| Skill Registry | [orchestrator/skill_registry.py:50](orchestrator/skill_registry.py#L50) |
| DomainRouter | [orchestrator/router.py:27](orchestrator/router.py#L27) |
| DynamicDagSpec | [orchestrator/dynamic_dag.py](orchestrator/dynamic_dag.py) |
| CrossDomainCoordinator | [orchestrator/cross_domain.py](orchestrator/cross_domain.py) |
| PermissionEngine | [orchestrator/permission_engine.py](orchestrator/permission_engine.py) |
| EventStore ABC + SQLite 实现 | [audit/store.py](audit/store.py) |
| trigger_workflow 工具 | [tools/trigger_workflow.py:32](tools/trigger_workflow.py#L32) |
| collect_child_result 工具 | [tools/collect_child_result.py](tools/collect_child_result.py) |
| Manager Agent yaml | [config/agents/manager.yaml](config/agents/manager.yaml) |
| 业务域 Agent yamls | [config/agents/](config/agents/)（12 个 yaml） |
| Workflow 模板 | [workflows/](workflows/)（8 个 yaml） |

---

## 15. 一句话总结

> **Manager Agent = 指挥**（长寿命 Codex/Opencode 会话，通过 `trigger_workflow` 工具调度 DAG）。
> **DagEngine = 调度图**（拓扑分层、同层并行、跨层 barrier、状态机驱动）。
> **Subagent = 执行**（每个 `type: agent` 节点派发一个 Harness Adapter，handoff 交接产物）。
> **DAG 非 agent 节点 = 纯网关**（parallel_branch 聚合 / gateway 路由，0 LLM 调用）。
> **数据库 = sessions 表核心 + 7 张业务子表**（dag_events / raw_harness_events / widget_inputs / usage_records / session_messages / session_events / session_memory / parent_child_sessions），全部 FK 到 `sessions.session_id`，v2 统一 ID 架构。

---

## 附录 A：典型 workflow YAML 结构（[workflows/weekly-report.yaml](workflows/weekly-report.yaml)）

```yaml
workflow_id: weekly-report
version: 2.0

inputs:
  - name: weekly_submissions
    type: array
    required: true
  - name: period_start
    type: string
    required: true
  - name: period_end
    type: string
    required: true

nodes:
  start_collect:                 # parallel_branch virtual
    type: parallel_branch
    branches: [query_kb, collect]
  query_kb:                      # agent node
    type: agent
    agent: weekly_reporter       # → config/agents/weekly_reporter.yaml
    business_role: 知识库查询员
    role_prompt: |
      你是知识库查询员 ...
    harness: codex
    model: minimax/MiniMax-M3
    after: []
    timeout_seconds: 60
    inputs: [period_start, period_end]
    outputs:
      patterns: "classify.in:patterns"
  classify:
    type: agent
    agent: weekly_reporter
    business_role: 分类专员
    role_prompt: |
      你是分类专员，按 5 维度 MECE 分类 ...
    harness: codex
    after: [start_collect]
    timeout_seconds: 300
    inputs: [collected_items, patterns]
    outputs:
      classified_items: "prioritize.in:classified_items"
  # ... 8 个 agent 节点 + 1 parallel_branch + 1 gateway
  gate:
    type: gateway
    gateway_kind: condition
    condition: "validate.passed == true ? 'pass' : 'fail'"
    after: [validate]
    inputs: [validated_md, passed, failed_items]
    outputs:
      pass: "archive.in:validated_md"
      fail: "archive.in:validated_md"

widgets:
  - id: w_weekly_progress
    type: progress_status
    title: 周报生成进度
    emit_on: { node: start_collect, event: node.started }
    props:
      steps:
        - { id: query_kb, title: 查询历史模式, node: query_kb }
        - { id: classify, title: 5 维度分类, node: classify }
        # ... 与节点一一对应
```

**关键约定**（来自 [.claude/rules/workflow-yaml.md](.claude/rules/workflow-yaml.md)）：
- `workflow_id` 必须与文件名一致
- 节点 `after` 只能引用已声明节点 ID，禁止前向引用
- `outputs.to` 格式：`"next_node.in:port_name"` 或 `"next_node"`（默认 port）
- `parallel_branch` 节点必须声明 `branches` 列表
- `gateway` 节点必须声明 `gateway_kind` + `condition`
- `widgets.emit_on.node` 必须是 `nodes` 中已声明节点 ID
- 修改后必须跑 `python cli.py validate <file>` 验证

---

## 附录 B：典型 agent yaml 结构（[config/agents/weekly_reporter.yaml](config/agents/weekly_reporter.yaml)）

```yaml
agent_id: weekly_reporter
domain: personal_assistant
display_name: Weekly Reporter Agent
harness: codex
model:
  provider: minimax
  id: MiniMax-M3

system_prompt: |
  你是 AgentOps 的周报助手 agent，按 {{node_id}} 路由执行对应职责（v2 8 节点复用同一 agent）。

  === 全局规则（所有节点都必须遵守）===
  ...

  === node_id == "query_kb" ===
  你的职责：并行注入历史模式 ...
  执行步骤：
  1. 读取 handoff 字段 period_start / period_end
  2. 调用 query_knowledge 查 3 类：patterns / concepts / comparisons
  ...

  === node_id == "classify" ===
  你的职责：把工作项按 v2 5 维度严格 MECE 分类 ...
  ...

output_files:                              # 文件收割（harvest_file_outputs）
  patterns: '{{workspace.root}}data/kb-patterns.json'
  classified_items: '{{workspace.root}}data/classified-items.json'
  # ... 与节点 outputs 一一对应

permissions:
  allowed_tools:
  - obsidian_vault
  - ingest_source
  - query_knowledge
  - read_file
  - write_file
  - finalize
  - emit_widget
  denied_tools:
  - bash
  - wecom_notify
  - log_query
  - trigger_workflow
  - ssh_exec
  - db_migrate

max_concurrent_runs: 1
timeout_seconds: 1200
cost_limit_per_run: 2
```

**关键约定**：
- `agent_id` 必须与文件名一致
- `harness` 字符串通过 `_HARNESS_NAME_MAP` 映射到 `HarnessType` 枚举
- `system_prompt` 用 `{{node_id}}` / `{{node_name}}` 占位符由 engine 渲染
- `system_prompt` 含 `node_id == "<nid>"` 字面量作为多节点共享 agent 的路由段（[workflow/validator.py:184-187](workflow/validator.py#L184-L187) 校验）
- `output_files` 端口名必须包含 workflow 节点 outputs 声明的所有端口（[workflow/validator.py:198-209](workflow/validator.py#L198-L209) 校验）
- `permissions.allowed_tools` 走最小权限原则，manager 才有 `trigger_workflow`
- `timeout_seconds` 是 agent 级默认；workflow 节点可覆盖（[workflow/engine.py:_get_node_timeout](workflow/engine.py#L1451-L1467)）

---

## 附录 C：典型触发路径日志（单次 weekly-report run）

```
# Manager 决定派发
manager_turn_started  → session=<manager_session> turn_id=turn_abc123
trigger_workflow(workflow_id="weekly-report", inputs={...})  → run_id=session_20260809_xxx

# 桥接到 DAG engine
init_session(session_id=session_20260809_xxx, workflow_id="weekly-report", run_mode="templated", inputs={...})
event_bridge.start(session_20260809_xxx)

# DAG 启动
run.created            seq=1   payload={workflow_id, inputs, layout: {nodes: [...], edges: [...]}}
node.started           seq=2   node=query_kb        payload={agent, harness}
node.started           seq=3   node=collect         payload={agent, harness}      # parallel
node.progress          seq=4   node=query_kb        payload={agent_text: "..."}
node.handoff           seq=5   node=query_kb        payload={from, to, port, summary}
node.completed         seq=6   node=query_kb        payload={outputs, tokens_in, tokens_out}
node.handoff           seq=7   node=collect         payload={from, to, port, summary}
node.completed         seq=8   node=collect         payload={outputs, tokens}
node.started           seq=9   node=classify        payload={agent, harness}
... (后续节点)
node.completed         seq=42  node=archive         payload={outputs, tokens}
run.completed          seq=43  payload={duration_ms, total_tokens_in, total_tokens_out}

# 用量落库
usage_records(session_id, node_id, provider_id="minimax", model="MiniMax-M3", ...)  × 8 条（每节点 1 条）

# Run 终止
finalize_session(session_id, status="completed", finished_at, total_tokens_*, final_outputs={archive_path, kb_ingest_result})

# 摘要回灌（异步）
_summary_run_on_completion:
  轮询 sessions.status == completed
  events = get_events(run_id)
  memory_manager.summarize_run(session_id=parent, run_id=run_id, workflow_id="weekly-report", run_events)
  → session_memory(parent_session_id, memory_type="run_summary", source_session_id=run_id, content="...", importance=0.8)
```

---

**报告完成。** 对比 外部参考方案 文档，本报告聚焦于 AgentOps 的三层架构（Manager → DagEngine → Harness）、v2 统一 session_id 架构、以及 SQLite + WAL 的轻量持久化方案。外部参考方案 的 Docker Worker 抽象 / `actor_id` / `lease_generation` 概念在 AgentOps 中被 in-process harness + `node_id` 替代，更轻量但缺少跨进程的 actor 复用能力。