---
type: agents
domain: proposal-planning
created_at: 2026-07-17T00:00:00+00:00
tags: [agents, proposal-planning]
---

# proposal-planning 知识库 · AGENTS.md

> 本文件是 proposal-planning 知识库的指令文件，告诉 agent 这个知识库的范围、规则、维护方式。
> LLM Wiki 双枢纽：[index.md](index.md)（导航）+ [log.md](log.md)（时间线）。

## 知识库范围

本知识库收录**方案策划过程中的案例与经验**，包括：

- **历史方案案例**：用户与 agent 共同产出的方案（信息化建设 / 数字化 / 智能化建设）
- **方案模板**：不同类型方案的结构模板（如 OA 升级 / 预算管理 / 智能审批 / 智能问数）
- **补充纠正记录**：agent 对用户原思路的补充纠正点 + 用户确认结果
- **技术选型对比**：方案中涉及的技术选型决策记录（如 SSO vs MCP / 自研 vs 采购）

**不收录**：
- 方案最终归档（那是 obsidian_vault 的 `Reports/` 归档目录的事）
- 用户公司机密数据
- 与方案策划无关的内容

## 页面类型

> **目录结构对齐 query_kb.py 路由**：patterns 是单文件，cases/entities/concepts/comparisons 都是目录。
> 与 `config/knowledge/domains.yaml` 的 `category_layout` 字段保持一致。

| page_type | 用途 | 路径布局 | 示例 |
|---|---|---|---|
| `source` | 原始素材（某次方案的对话记录/背景资料） | `raw/` 目录 | `raw/20260717_180000_abc12345.md` |
| `entity` | 实体（方案类型/技术选型） | `cases/` 目录 | `cases/oa-upgrade.md`（OA 升级方案案例） |
| `concept` | 概念（方案设计原则） | `concepts/` 目录 | `concepts/design-principles.md` |
| `comparison` | 对比（技术选型决策） | `comparisons/` 目录 | `comparisons/tech-selection.md`（SSO vs MCP） |

## 维护规则

### Ingest（沉淀）
- 每次方案确认后，agent 调 `ingest_source` 沉淀本次方案
- `page_type=source`：存原始对话记录到 `raw/`
- `page_type=entity` + `target_pages=["cases/<主题>"]`：更新方案案例页
- `page_type=concept` + `target_pages=["concepts/design-principles"]`：更新方案设计原则
- `page_type=comparison` + `target_pages=["comparisons/tech-selection"]`：更新技术选型对比页

### Query（查询）
- 策划新方案前，agent 调 `query_knowledge` 查历史案例
- `category=cases`：查历史方案案例（目录）
- `category=entities`：查实体页（目录，目前为空，预留给未来扩展）
- `category=concepts`：查概念页（目录，含 design-principles.md 等）
- `category=comparisons`：查对比页（目录，含 tech-selection.md 等）

### Lint（检测）
- 方案案例 ≥5 个后，agent 可调 `lint_knowledge` 检测矛盾
- 例如：同一技术选型在不同方案中给出矛盾建议

## 相关文件

```
config/knowledge/proposal-planning/
├── AGENTS.md              # 本文件（指令）
├── index.md               # 导航枢纽
├── log.md                 # 时间线枢纽
├── raw/                   # 原始素材（不可变）
│   └── <timestamp>_<hash>.md
├── cases/                 # 方案案例（entity，目录）
│   ├── oa-upgrade.md
│   ├── budget-management.md
│   └── smart-approval.md
├── entities/              # 实体页目录（预留扩展，目前为空）
├── concepts/              # 概念页目录
│   └── design-principles.md   # 方案设计原则
└── comparisons/           # 对比页目录
    └── tech-selection.md      # 技术选型对比
```

## 调用方

- `proposal_planner_agent`：主要调用方，每次方案确认后 ingest，策划前 query
