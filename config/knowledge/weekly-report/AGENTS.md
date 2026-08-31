---
type: agents
domain: weekly-report
created_at: 2026-07-17T00:00:00+00:00
tags: [agents, weekly-report]
---

# weekly-report 知识库 · AGENTS.md

> 本文件是 weekly-report 知识库的指令文件，告诉 agent 这个知识库的范围、规则、维护方式。
> LLM Wiki 双枢纽：[index.md](index.md)（导航）+ [log.md](log.md)（时间线）。

## 知识库范围

本知识库收录**周报生成过程中的模式与经验**，包括：

- **典型问题处置模式**：用户周报中反复出现的问题类型 + 标准处置流程
- **重要程度分级案例**：S/A/B/C 四级分级的典型样例（什么算"重要"、什么算"常规运维"）
- **5 维度分类规则**：系统功能优化 / 流程权限管理 / 数据处理 / 系统运维支撑 / 需求沟通 的边界判定
- **去 AI 味写作样例**：好的周报条目 vs 差的周报条目对比

**不收录**：
- 原始周报内容（那是 obsidian_vault 的 `Weekly/` 归档目录的事）
- 用户个人隐私信息
- 与周报生成无关的内容

## 页面类型

> **目录结构对齐 query_kb.py 路由**：patterns 是单文件，entities/concepts/comparisons 都是目录。
> 与 `config/knowledge/domains.yaml` 的 `category_layout` 字段保持一致。

| page_type | 用途 | 路径布局 | 示例 |
|---|---|---|---|
| `source` | 原始素材（某次周报的提炼结果） | `raw/` 目录 | `raw/20260717_180000_abc12345.md` |
| `entity` | 实体（反复出现的关键概念） | `patterns.md` 单文件 | `patterns.md`（典型问题处置模式集合） |
| `concept` | 概念（重要程度分级规则） | `concepts/` 目录 | `concepts/importance-grading.md` |
| `comparison` | 对比（好/差写作样例） | `comparisons/` 目录 | `comparisons/writing-samples.md` |

## 维护规则

### Ingest（沉淀）
- 每次生成周报后，agent 调 `ingest_source` 沉淀本次周报的「模式」
- `page_type=source`：存原始提炼结果到 `raw/`
- `page_type=entity` + `target_pages=["patterns"]`：更新 patterns.md（典型问题处置模式）
- `page_type=concept` + `target_pages=["concepts/importance-grading"]`：更新重要程度分级规则
- `page_type=comparison` + `target_pages=["comparisons/writing-samples"]`：更新去 AI 味写作样例

### Query（查询）
- 生成新周报前，agent 调 `query_knowledge` 查历史模式
- `category=patterns`：查典型问题处置模式（单文件）
- `category=entities`：查实体页（目录，目前为空，预留给未来扩展）
- `category=concepts`：查概念页（目录，含 importance-grading.md 等）
- `category=comparisons`：查对比页（目录，含 writing-samples.md 等）

### Lint（检测）
- 周报模式积累 ≥10 条后，agent 可调 `lint_knowledge` 检测矛盾
- 例如：同一类问题在不同周报中被分到不同维度

## 相关文件

```
config/knowledge/weekly-report/
├── AGENTS.md          # 本文件（指令）
├── index.md           # 导航枢纽（所有页面索引）
├── log.md             # 时间线枢纽（所有操作记录）
├── raw/               # 原始素材（不可变）
│   └── <timestamp>_<hash>.md
├── patterns.md        # 典型问题处置模式（entity，单文件）
├── entities/          # 实体页目录（预留扩展，目前为空）
├── concepts/          # 概念页目录
│   └── importance-grading.md  # 重要程度分级规则
└── comparisons/       # 对比页目录
    └── writing-samples.md     # 去 AI 味写作样例
```

## 调用方

- `weekly_report_agent`：主要调用方，每次生成周报后 ingest，生成前 query
