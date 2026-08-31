---
paths:
  - "api/server.py"
  - "orchestrator/**"
  - "workflow/**"
  - "task/**"
  - "config/dispatch.yaml"
  - "config/schedules.yaml"
  - "config/patrol.yaml"
priority: 50
---

# Workflow / DAG / Task 规则

本文件聚焦**工作流引擎、DAG 调度、任务生命周期**的修改约定。涉及本目录的改动前请通读。

## 架构三件套

| 层 | 路径 | 职责 |
|---|---|---|
| 工作流引擎 | `orchestrator/dynamic_dag.py` | DAG 节点解析、调度、状态转换 |
| 任务生命周期 | `task/orchestrator.py` / `task/store.py` / `task/status.py` | 任务 CRUD + 状态机 |
| Worker 执行 | `task/terminal_*.py` / `harness/*.py` | 在 worker container 里跑实际 LLM 调用 |

事件总线：`audit/`（SQLite）—— **所有跨模块事件必须走事件存储**，不要直接 import 互相调用。

## 改前必读

1. `orchestrator/manager.py` —— Manager Agent 主入口；任何 manager 行为变更先看这里
2. `task/status.py` —— 任务状态枚举（避免硬编码字符串）
3. `orchestrator/_registry.py` —— actor / tool / skill 注册中心
4. `config/agents/*.yaml` + `config/tools/*.yaml` —— 实际配置驱动行为，代码改动往往配 YAML 更合适

## 改时约束

- 状态机改动：`task/status.py` 加新状态 → `task/store.py` 同步转移规则 → 所有 transition caller 改完
- DAG 节点 schema 改动：`orchestrator/dynamic_dag.py` 节点定义 + 所有 node_type handler + 对应 test
- Manager 决策 prompt 改动：优先改 `config/agents/manager.yaml` 的 `system_prompt` 而非硬编码
- 加新工具：写 `tools/<name>.py` + `config/tools/<name>.yaml` + 注册到 `tools/__init__.py`，**不要在 server.py 里直接挂路由**

## 改后必验

- `pytest tests/test_dag_visualization.py tests/test_task_orchestrator_v1.py -x` —— DAG + 任务核心
- `pytest tests/test_session_engine.py -x` —— 会话引擎（如改了 manager 调度）
- 真跑一个端到端任务：`POST /api/agent/run`（参考 `bench/run_bench.sh`）

## 易踩坑

- `task/store.py` 的 SQLite 写入必须走事务；长事务会阻塞其他 reader → 用 `with store.connection() as conn:` 模式
- `orchestrator/dynamic_dag.py` 的 DAG 必须有 cycle 检测（`networkx` 已有）
- Manager Agent 用 Claude Code harness 时 prompt 缓存命中率敏感 → 避免在 system_prompt 里插入动态时间戳