---
type: agents
domain: smart-query
created_at: 2026-08-29T00:00:00+00:00
tags: [agents, smart-query]
---

# smart-query 知识库 · AGENTS.md

> 本文件是 smart-query 知识库的指令文件，告诉 agent 本知识库的范围、规则。
> LLM Wiki 双枢纽：[index.md](index.md)（导航）+ 维护规则见下。

## 知识库范围

本知识库收录**智能问数（smart-query）过程中，information_schema 字段字典覆盖不了的业务语义**：

- **业务术语 → 字段/取值映射**：意图识别（route_intent）和 SQL 生成（plan_sql）用，如「通过率」对应 `audit_records.decision='pass'`
- **表关系**：7 张表的 1:N 关联（选表 / 是否 JOIN 判断）
- **枚举取值**：状态机、决策、9 步管线、call_purpose 的合法取值
- **指标口径**：通过率 / 驳回率 / 平均耗时等指标的标准计算方式 + SQL 模板

**不收录**：
- 字段名/类型/中文含义 —— 那走 `information_schema.COLUMNS`（`describe_database` 返回的 `columns`）
- 敏感列 —— 那是 `config/db_whitelist.yaml` 的 `denied_columns`
- 真实业务数据 —— 那是 SQL 查询结果

## 与 information_schema 的分工（关键）

| 信息 | 来源 |
|---|---|
| 字段名 / 类型 / 中文含义 | `information_schema.COLUMNS` + `COLUMN_COMMENT`（describe_database 返回 `columns`） |
| 表级白名单 / 敏感列 | `config/db_whitelist.yaml` |
| **业务术语 / 表关系 / 枚举 / 指标口径** | **本知识库**（query_kb 按需查） |

## 页面类型

| page_type | 用途 | 路径 |
|---|---|---|
| `patterns` | 指标口径 + SQL 查询模式（单文件） | `patterns.md` |
| `concept` | 业务术语 / 表关系 / 枚举（目录） | `concepts/business-glossary.md` |
| `reference` | 上游数据字典快照（只读参考，query_kb 关键词兜底可命中） | `references/audit_platform_database_dictionary.md` |

## 维护规则

- 字段级信息**不要**写进本知识库（那是 information_schema 的事，写这里会双源漂移）
- 指标口径变更时更新 `patterns.md`
- 新业务术语 / 新枚举值更新 `concepts/business-glossary.md`
- 数据来源基准：`references/audit_platform_database_dictionary.md`（上游 `AI_Agent_Platform/docs/audit_platform_database_dictionary.md` 的快照，2026-08-31 复制入库；上游更新时需手动重新同步）

## 调用方

- `smart_query` agent：route_intent 节点判意图 + plan_sql 节点理解口径时，通过 `query_kb` 按需查
