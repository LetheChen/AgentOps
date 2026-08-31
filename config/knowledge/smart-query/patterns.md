---
type: entity
domain: smart-query
created_at: 2026-08-29T00:00:00+00:00
tags: [metrics, sql-patterns, smart-query]
---

# 指标口径 + SQL 查询模式

> 用途：plan_sql 理解「通过率/驳回率/耗时」等指标怎么算、写标准 SQL。
> 字段名务必以 `describe_database` 返回的 `columns` 为准（information_schema），这里的字段名仅为口径说明。

## 一、指标口径

### 通过率
- 定义：`decision='pass'` 的记录数 / 有 decision 的记录总数（或全部记录数，看业务口径）
- 建议 SQL：`SUM(CASE WHEN decision='pass' THEN 1 ELSE 0 END) / COUNT(*)`

### 驳回率
- 定义：`decision='reject'` 占比
- 建议 SQL：`SUM(CASE WHEN decision='reject' THEN 1 ELSE 0 END) / COUNT(*)`

### 人工复核率
- 定义：`decision='manual'` 占比

### 状态分布
- `GROUP BY status`，各状态 COUNT

### 审核耗时（平均）
- 两种口径，按需选：
  - 管线整体耗时：`receive_time` → `complete_time` 差值
  - 步骤耗时：`audit_step_logs.duration_ms`（步骤级）
- 建议 SQL：`AVG(TIMESTAMPDIFF(SECOND, receive_time, complete_time))`

### 业务线对比
- `GROUP BY agent_id`（或 `agent_name`），各业务线的单量/通过率

## 二、SQL 编写铁律

1. **字段名来自 columns**，不猜（见 smart_query agent system_prompt 铁律）
2. **字符串字面值用单引号**，禁止双引号（会破坏 CLI 传递）
3. **末尾加 LIMIT**（不超过 max_rows）
4. **避开 denied_columns**（password_hash / node_token 等）
5. **只读 SELECT**，单表优先，非必要不 JOIN
6. **时间范围**：用户说「本周/今日」时，明确起止边界（周一起算还是近 7 天，拿不准就问）

## 三、常见查询模板

### 本周通过率
```sql
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN decision = 'pass' THEN 1 ELSE 0 END) AS pass_count,
  ROUND(SUM(CASE WHEN decision = 'pass' THEN 1 ELSE 0 END) / COUNT(*) * 100, 2) AS pass_rate
FROM audit_platform.audit_records
WHERE receive_time >= '2026-08-24 00:00:00'
  AND receive_time < '2026-08-31 00:00:00'
LIMIT 1000
```

### 状态分布（某业务线）
```sql
SELECT status, COUNT(*) AS cnt
FROM audit_platform.audit_records
WHERE agent_id = 'expense_agent'
GROUP BY status
LIMIT 1000
```

### 各业务线单量 + 通过率对比
```sql
SELECT
  agent_id,
  COUNT(*) AS total,
  SUM(CASE WHEN decision = 'pass' THEN 1 ELSE 0 END) AS pass_count,
  ROUND(SUM(CASE WHEN decision = 'pass' THEN 1 ELSE 0 END) / COUNT(*) * 100, 2) AS pass_rate
FROM audit_platform.audit_records
WHERE receive_time >= '2026-08-24 00:00:00'
GROUP BY agent_id
LIMIT 1000
```
