---
name: data-query
description: 数据查询规范——多轮对话直查数据库的表结构获取、SQL 写法、安全约束与示例
domain: _shared
depends_on: []
---

# 数据查询规范

> 本 skill 教你（Agent）在多轮对话场景下如何查询业务数据库。
> 适用：manager 直接响应用户的数据查询/统计/分析问题（不触发 workflow 时）。

---

## 一、操作铁律（强制）

- **只读**：只能用 `sql_query` 工具查询，**禁止** `sql_execute`（写操作已禁用）
- **先看表结构**：不确定有哪些表/字段时，先调 `describe_database(database="mysql:audit_reader")`，不要猜表名字段名
- **禁止编数据**：sql_query 返回失败/空时，如实告知用户错误，不要靠自身知识编造数字
- **单条 SELECT**：一次只提交一条 SELECT，自动有 LIMIT 5000 兜底，大结果集优先聚合

---

## 二、可用数据源

| 连接标识 | 库 | 说明 |
|---|---|---|
| `mysql:audit_reader` | `audit_platform` | 智能审批业务数据（只读账号） |

表结构以 `describe_database` 返回为准：
- `tables` 表清单 + 敏感列屏蔽（denied_columns）来自 `config/db_whitelist.yaml`（安全授权源）
- `columns` 字段名/类型/中文含义来自 `information_schema`（字段字典，COMMENT 已维护）

核心表速览（审计平台）：

| 表 | 用途 | 关键字段 |
|---|---|---|
| `audit_records` | 审批记录 | request_id / agent_id / agent_name / doc_number / status / decision / applicant_id / receive_time / complete_time |
| `audit_item_results` | 审计条目结果 | request_id / item_id / item_name / status / decision / opinion / reason |
| `audit_step_logs` | 审批步骤日志 | request_id / step_id / step_name / action / status / input_data / output_data |
| `access_logs` | 访问日志 | request_id / method / path / status_code / client_ip / username / duration_ms |
| `llm_call_logs` | LLM 调用日志 | agent_id / model_name / provider / status / prompt_tokens / completion_tokens / cost_rmb |
| `llm_model_pricing` | 模型定价 | model_name / model_category / input_price_per_1k / output_price_per_1k |
| `users` | 用户 | username / role / user_type / is_active |

> 注意：`users.password_hash`、`audit_records.node_token` 等敏感列已被白名单屏蔽，禁止尝试查询。

---

## 三、SQL 写法规范

1. **表名全限定**：`audit_platform.audit_records`，避免歧义
2. **时间过滤**：用 `created_at` / `receive_time` / `complete_time`（datetime 类型），例如 `WHERE receive_time >= DATE_SUB(NOW(), INTERVAL 30 DAY)`
3. **聚合优先**：统计用 `COUNT` / `GROUP BY` / `AVG` / `SUM`，不要全量拉数据后自己数
4. **LIMIT 兜底**：单查询最多 5000 行（工具自动加），明细查询自己加 `LIMIT 20` 控制返回

---

## 四、典型示例

### 4.1 审批通过率
```sql
SELECT
  status,
  COUNT(*) AS n
FROM audit_platform.audit_records
WHERE receive_time >= DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY status;
```

### 4.2 状态分布（含驳回）
```sql
SELECT decision, COUNT(*) AS n
FROM audit_platform.audit_records
WHERE decision IS NOT NULL
GROUP BY decision;
```

### 4.3 LLM 成本统计
```sql
SELECT model_name, SUM(cost_rmb) AS total_cost, SUM(total_tokens) AS total_tokens
FROM audit_platform.llm_call_logs
WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY model_name;
```

---

## 五、错误处理

| 错误 | 处理 |
|---|---|
| 表/列不在白名单 | 换合法的表/列（用 `describe_database` 确认），或告知用户该数据不在授权范围 |
| 校验拒绝（危险词） | 检查是否误用写操作关键字，改写成纯 SELECT |
| 连接失败 | 告知用户数据库连接异常，建议到「凭据管理」检查连接 |
