---
type: concept
domain: smart-query
created_at: 2026-08-29T00:00:00+00:00
tags: [glossary, enum, relation, smart-query]
---

# 业务术语 · 表关系 · 枚举取值

> 用途：route_intent 判意图 + plan_sql 理解业务口径时按需查。
> 字段级信息（字段名/类型/含义）请看 `describe_database` 返回的 `columns`（information_schema），不在这里。

## 一、业务术语 → 字段/取值映射

这些映射帮 agent 把「人话」翻译成「字段 + 取值」。**取值枚举以 information_schema 的 comment 为准**，下表是语义层面的速查。

| 业务说法 | 对应字段 | 判定 |
|---|---|---|
| 通过率 / 通过 | `audit_records.decision` | `= 'pass'`（不是 status） |
| 驳回 / 不通过 | `audit_records.decision` | `= 'reject'` |
| 人工复核 / 转人工 | `audit_records.decision` | `= 'manual'` |
| 审核单总数 | `audit_records` 行数 | `COUNT(*)` |
| 本周 / 本周业务 | `audit_records.receive_time` 或 `created_at` | 时间窗过滤（周一 00:00 到周日 24:00） |
| 审核耗时 | `audit_step_logs.duration_ms` 或 `receive_time`→`complete_time` 差值 | 见 patterns.md |
| 某类业务（如差旅/采购） | `audit_records.agent_id` / `agent_name` | 按 agent 过滤（取值见下） |

> ⚠️ 陷阱：`status`（pending/running/completed/failed）是**任务执行状态**，`decision`（pass/manual/reject）是**审核结论**。「通过率」用 `decision`，别用 `status`。这正是历史上 plan_sql 猜 `status='approved'` 猜错的根因。

## 二、表关系（1:N，选表 / JOIN 判断）

```
audit_records (主表，1)
  ├─ 1:N audit_step_logs      (request_id)   9 步管线步骤日志
  ├─ 1:N audit_item_results   (request_id)   审核项结果
  ├─ 1:N llm_call_logs        (request_id)   LLM 调用流水（弱关联）
  └─ 1:N access_logs          (request_id)   API 访问日志（弱关联）

llm_model_pricing (价目) 1:N llm_call_logs (pricing_version_id，弱关联)
```

- 单表聚合优先：绝大多数统计（状态分布/通过率/耗时）只查 `audit_records` 一张表即可。
- 只有需要「步骤级」「审核项级」「成本级」明细时才 JOIN 子表。
- MySQL 部署下**无数据库层外键**，删除主表不级联清理子表。

## 三、枚举取值

### audit_records.status（任务状态机）
`pending` → `running` → `completed` / `failed`

### audit_records.decision（审核结论，Step8 写入）
`pass` / `manual` / `reject`

### audit_item_results.decision（单项决策）
`pass` / `manual` / `reject` / `skip`

### 9 步管线（audit_step_logs.step_id）
| step_id | 含义 |
|---|---|
| step1_fetch_oa_data | 数据预处理 |
| step2_activate_items | 激活审核项 |
| step3_dispatch_tools | 调度工具 |
| step4_parse_attachments | 附件识别 |
| step5_prepare_and_assemble | 数据组装准备 |
| step6_batch_audit | 统一模型审核 |
| step7_split_results | 结果拆分 |
| step8_aggregate_decision | 汇总决策 |
| step9_report_and_callback | 生成 PDF 报告并回调 OA |

### llm_call_logs.call_purpose（调用目的）
`audit_pipeline` / `batch_audit` / `attachment_vision` / `attachment_text` / `item_audit`

### llm_call_logs.model_category / llm_model_pricing.model_category
`text` / `vision`

## 四、agent 取值（业务线过滤）

> ⚠️ 完整清单以 `audit_records.agent_id` 实际数据为准，下方是文档已知示例，未列全。

| agent_id 示例 | agent_name 示例 |
|---|---|
| `purchase_contract_agent` | 采购合同审核 |
| `expense_agent` | 费用报销审核 |

查询某类业务时，优先用 `agent_id`（稳定、目录名）而非 `agent_name`（中文、可能改名）。
