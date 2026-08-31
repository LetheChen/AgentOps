---
name: dag-ops
description: DAG 工作流操作指南——触发、监控、审查 DAG 运行
domain: _shared
depends_on: []
---

# DAG 工作流操作

> 本 skill 教你（Agent）如何触发、监控、审查 DAG 工作流运行。
> 可用 workflow 列表由 WorkflowRegistry 启动时自动注入 system_prompt，无需主动查询。

---

## 一、触发工作流

### 1.1 操作铁律（强制）

- **必须**用 `trigger_workflow` 工具触发，**禁止**用 bash+curl 调 HTTP API
- **禁止**让用户手动执行 curl 命令
- **禁止**直接调子 agent（如 video_creator / content_curator）——它们由 workflow DAG 节点自动调度
- 视频任务**必须**用 `trigger_workflow(workflow_id="video-pipeline")`，**不要**用 `request_cross_domain`——CrossDomainCoordinator 只生成动态单节点 DAG，不会加载 video-pipeline.yaml 的固定 6 步拓扑

### 1.2 调用方式

```python
trigger_workflow(
    workflow_id="weekly-report",       # 工作流 ID（中划线格式）
    inputs={                           # 工作流输入参数
        "period_start": "2026-07-29",
        "period_end": "2026-08-04",
        "extra_notes": "本周完成 v87 重构"
    },
    run_mode="templated"               # 默认 templated，不需要传 agent_id
)
```

**run_mode 选择**：
- `templated`：固定拓扑 DAG（默认，大部分场景）
- `conversational`：纯对话（无 DAG）
- `task`：单节点任务
- `hybrid`：DAG 内嵌对话子循环

### 1.3 触发后告知用户进度

调 `trigger_workflow` 后用**对话消息**告知用户"工作流已启动 · run_id=xxx"（展示统一走 present_content / report_surface_state 到 Supervision 大屏）。
进度展示是自动的：前端有 DAG 节点拓扑图；各 actor 节点按 role_prompt 推送 surface 卡片（started→partial→final 原地更新）。需要程序化查询时调 `get_workflow_status({run_id})`。

---

## 二、监控运行

### 2.1 实时事件流（SSE）

工作流启动后，前端通过 SSE 收到事件流：
- `run.created` / `run.completed` / `run.failed` / `run.cancelled`
- `node.ready` / `node.started` / `node.progress` / `node.handoff` / `node.completed` / `node.failed` / `node.skipped`
- `widget.update` / `widget.input`
- `usage` / `cross_domain`

### 2.2 历史回放

调用 `auditGetRunEvents` 取事件流，按 sequence 重放。前端用 `summary.status` 设置初始状态避免闪烁。

### 2.3 断点恢复

`DagEngine.resume(run_id, inputs)` 复用已有 run_id：
- 节点执行前检查 `output_files` 是否已存在
- 全部存在则从文件恢复 `pending_handoffs` 并跳过执行
- 利用文件驱动的天然检查点机制，无需额外持久化层

---

## 三、节点超时

- 节点级超时：`node.timeout_seconds`（workflow yaml）
- agent 级超时：`agent.timeout_seconds`（agent yaml）
- 默认：600s
- 优先级：node > agent > 默认

超时后 raise RuntimeError → emit `NODE_FAILED` → `RUN_FAILED`。

---

## 四、skip_if 条件跳过

```yaml
nodes:
  notify:
    skip_if: "{{not report.critical_summary}}"
```

支持格式：
- `{{not validate.passed}}`：取反
- `{{validate.passed}}`：直接取值

匹配 `true/1/yes/pass/passed` 为真。skip 触发后递归跳过下游节点。

---

## 五、Fallback 链

`models.yaml` 的 `fallback_chains` 字段配置 provider 切换：

```yaml
fallback_chains:
  minimax: [deepseek, vllm]
```

**仅在 error_type ∈ {rate_limit, timeout} 时触发**（provider 临时不可用）。
auth_error/protocol_mismatch/not_found 不触发（配置问题切换救不了）。

---

## 六、常见问题

| 问题 | 原因 | 解决 |
|---|---|---|
| `Model not found` | provider 被 `disabled_providers` 禁用 | 从 disabled_providers 移除该 provider |
| 429 频繁 | 短时间高频调用 | 降低频率或切 fallback |
| 节点状态卡 running | opencode server 崩溃 | 重启 opencode，Patroller 60s 内自动 sweep |
| session 残留堆积 | 每个 DAG 节点跑 1 个 opencode session | 定期清 `opencode.db` |
