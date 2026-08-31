# AgentOps 设计文档索引（公开版）

> 最后更新：2026-08-31 · 本仓库为 AgentOps 开源版本。
> 详细设计文档（按侧栏菜单项分类的完整 70+ 份）位于 [`_private/`](./_private/) 子模块，**默认不公开**。
> 本目录仅公开**架构核心文档**，便于读者快速理解平台整体设计。

## 公开文档（架构核心）

### 平台架构（[00-platform/architecture/](./00-platform/architecture/)）

| 文档                                                                                                                                      | 说明                                        |
| --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| [REQUIREMENTS-agentops-prd-v1.6.md](./00-platform/architecture/REQUIREMENTS-agentops-prd-v1.6.md)                                       | AgentOps 多 Agent 平台 PRD v1.6（产品需求基线）      |
| [DESIGN-agentops-hld-v1.3.md](./00-platform/architecture/DESIGN-agentops-hld-v1.3.md)                                                   | 平台总体架构（HLD）v1.3                           |
| [DESIGN-architecture-refactor-v3.md](./00-platform/architecture/DESIGN-architecture-refactor-v3.md)                                     | v3 架构改造方案（sessions/runs/subagents 三层清晰拆分） |
| [DESIGN-agent-harness-layering.md](./00-platform/architecture/DESIGN-agent-harness-layering.md)                                         | 业务 Agent 与 Harness 原生子 Agent 分层架构         |
| [DESIGN-manager-subagent-dag-three-layers.md](./00-platform/architecture/DESIGN-manager-subagent-dag-three-layers.md)                   | Manager / Subagent / DAG 三层业务关系           |
| [DESIGN-node-containerization-and-event-relay-p016.md](./00-platform/architecture/DESIGN-node-containerization-and-event-relay-p016.md) | P0.16 节点容器化与事件回传                          |

### Harness 集成（[00-platform/harness-analysis/](./00-platform/harness-analysis/)）

| 文档                                                                                                      | 说明              |
| ------------------------------------------------------------------------------------------------------- | --------------- |
| [ANALYSIS-harness-three-contexts.md](./00-platform/harness-analysis/ANALYSIS-harness-three-contexts.md) | Harness 三语境综合分析 |

### 目录约定

```
docs/
├── INDEX.md                                   # 本文件
├── 00-platform/
│   ├── architecture/                          # 6 份架构核心（公开）
│   └── harness-analysis/                      # 1 份 harness 分析（公开）
├── 09-coding-terminal/
│   └── README.md                              # Coding 终端入口
├── gallery/                                   # 功能截图图廊（公开，README 配图）
└── _private/                                  # 完整设计文档（私有，不开源）
    ├── 01-monitor-center/                     # 监控中心（4 份）
    ├── 02-collaboration-visualization/        # 协作可视化（4 份）
    ├── 03-agent-management/                   # Agent 管理（1 份）
    ├── 05-task-management/                    # 任务管理（7 份）
    ├── 06-knowledge-management/               # 知识管理（8 份）
    ├── 07-security-management/                # 安全管理（6 份）
    ├── 08-system-settings/                    # 系统设置（1 份）
    └── 00-platform/
        ├── architecture/                      # 历史架构（10 份）
        ├── archive/                           # 归档（5 份）
        ├── generative-ui/                     # A2UI 调研（2 份）
        ├── harness-research/                  # Harness 调研（5 份）
        └── reviews/                           # 设计评审（3 份）
```

`_private/` 目录内的文档涉及具体业务（OA 集成、智能审核业务、致远审批、企业微信推送、日志采集等）和内部基础设施（内网 vLLM、内网 IP 等），不适宜公开发布。

## 阅读建议

新读者推荐按以下顺序阅读：

1. [项目简介（README.md）](../README.md) — 项目定位与快速开始
2. [PRD v1.6](./00-platform/architecture/REQUIREMENTS-agentops-prd-v1.6.md) — 产品需求全景
3. [HLD v1.3](./00-platform/architecture/DESIGN-agentops-hld-v1.3.md) — 平台整体架构
4. [v3 架构改造](./00-platform/architecture/DESIGN-architecture-refactor-v3.md) — 三层模型（sessions/runs/subagents）
5. [Agent/Harness 分层](./00-platform/architecture/DESIGN-agent-harness-layering.md) — 业务层与模型层解耦
6. [Manager/Subagent/DAG 三层业务关系](./00-platform/architecture/DESIGN-manager-subagent-dag-three-layers.md)
7. [P016 节点容器化](./00-platform/architecture/DESIGN-node-containerization-and-event-relay-p016.md)

