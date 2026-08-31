---
type: agents
domain: content-curation
created_at: 2026-07-17T00:00:00+00:00
tags: [agents, content-curation]
---

# content-curation 知识库 · AGENTS.md

> 本文件是 content-curation 知识库的指令文件，告诉 agent 这个知识库的范围、规则、维护方式。
> LLM Wiki 双枢纽：[index.md](index.md)（导航）+ [log.md](log.md)（时间线）。

## 知识库范围

本知识库收录**内容策展过程中的来源页与冲突记录**，包括：

- **来源页（Sources）**：用户提供的博客/技术文档/音视频转写稿的提炼摘要
- **实体页（Entities）**：从多个来源提炼的关键技术实体（如 Redis / RAG / Agent Harness）
- **概念页（Concepts）**：跨来源的技术概念（如「向量化检索」「工具调用循环」）
- **对比页（Comparisons）**：来源间的矛盾/版本差异/观点对立记录

**不收录**：
- 原始素材全文（那是 obsidian_vault 的 `Articles/` 归档目录的事）
- 与技术无关的内容（如寓言故事音视频）
- 已被判定为低价值/错误的素材

## 页面类型

| page_type | 用途 | 示例 |
|---|---|---|
| `source` | 来源页（单篇素材的提炼摘要） | `raw/20260717_180000_abc12345.md` |
| `entity` | 实体页（关键技术实体） | `entities/redis.md` / `entities/agent-harness.md` |
| `concept` | 概念页（跨来源技术概念） | `concepts/rag.md` / `concepts/tool-loop.md` |
| `comparison` | 对比页（来源间矛盾） | `comparisons/redis-version.md` |

## 维护规则

### Ingest（沉淀）
- 每篇内容评估后，agent 调 `ingest_source` 沉淀
- `page_type=source`：存来源摘要到 `raw/`
- `page_type=entity` + `target_pages=["entities/<name>"]`：更新实体页
- `page_type=concept` + `target_pages=["concepts/<name>"]`：更新概念页
- `page_type=comparison` + `target_pages=["comparisons/<name>"]`：记录矛盾

### Query（查询）
- 评估新内容前，agent 调 `query_knowledge` 查相关实体/概念
- `category=entities`：查相关实体页（判断是否与历史笔记冲突）
- `category=concepts`：查相关概念页
- `category=comparisons`：查已有矛盾记录

### Lint（检测）
- 来源页 ≥10 个后，agent 调 `lint_knowledge` 检测：
  - `contradictions`：实体页 vs 来源页的矛盾
  - `orphans`：无人引用的实体/概念页
  - `stale`：过时的技术声明（如已废弃的 API）
  - `dead_links`：指向已删除 raw 文件的链接

## 冲突检测四类型

| 类型 | 说明 | 处置 |
|---|---|---|
| `fact` | 事实矛盾（如 Redis 版本号冲突） | 自动归档 + 标记 conflict |
| `version` | 版本差异（如 v4 vs v5 API 变化） | 自动归档 + 标记 version_diff |
| `opinion` | 观点对立（如是否该用 RAG） | 人在环确认后再归档 |
| `complement` | 互补（新来源补充旧来源） | 自动合并到实体页 |

## 相关文件

```
config/knowledge/content-curation/
├── AGENTS.md              # 本文件（指令）
├── index.md               # 导航枢纽
├── log.md                 # 时间线枢纽
├── raw/                   # 来源页（不可变）
│   └── <timestamp>_<hash>.md
├── entities/              # 实体页（关键技术实体）
│   ├── redis.md
│   └── agent-harness.md
├── concepts/              # 概念页（跨来源技术概念）
│   ├── rag.md
│   └── tool-loop.md
└── comparisons/           # 对比页（来源间矛盾）
    └── redis-version.md
```

## 调用方

- `content_curator_agent`：主要调用方，每篇内容评估后 ingest，评估前 query + lint
