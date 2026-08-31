---
type: index
domain: weekly-report
created_at: 2026-07-17T00:00:00+00:00
updated_at: 2026-08-07T02:38:03+08:00
tags: [index]
---

# weekly-report 知识库索引

> LLM Wiki 双枢纽之一：本文件列出所有页面，是知识库的导航入口。
> 另一个枢纽是 [log.md](log.md)（时间线记录）。
> 指令文件：[AGENTS.md](AGENTS.md)。
> 模板文件：[template.md](template.md) — **v2（2026-08-07 优化）** 5 维度 MECE + S/A/B/C 分级 + Top 3 + 跨周闭环。

## Sources（原始素材）

- [Weekly\工作周报_2025-07-28_2025-08-03.md](raw/20260812_211054_9bf3184a.md) — ingested 2026-08-12（本周周期 2025-07-28 ~ 2025-08-03，2025-W31，信息技术中心 S=0/A=4/B=20/C=12 item=25）
- [20260718_034039_feb0c4ec.md](raw/20260718_034039_feb0c4ec.md) — ingested 2026-07-18
- [Weekly\工作周报_2026-08-03_08-09.md](raw/20260806_144735_8c173315.md) — ingested 2026-08-06
- [Weekly\工作周报_2026-07-27_2026-08-02.md](raw/20260807_023753_3d9a226f.md) — ingested 2026-08-06（本周周期 2026-07-27 ~ 2026-08-02，信息技术中心 S=0/A=2/B=5/C=26 item=33）

## Entities（实体）

- [patterns.md](patterns.md) — 典型问题处置模式集合（待 ingest 首次写入时创建）

## Concepts（概念）

- [importance-grading.md](concepts/importance-grading.md) — 重要程度分级规则（S/A/B/C 四级 + 量化锚点 + 合并铁律）
- [工作周报 2026-07-27 ~ 2026-08-02 归档经验](raw/20260812_151842_d41d8cd9.md) — ingested 2026-08-12
- [重要性分级 importance-grading 决策表](raw/20260812_151848_506aaf5d.md) — ingested 2026-08-12

## Comparisons（对比）

- [writing-samples.md](comparisons/writing-samples.md) — 去 AI 味写作样例（5 维度 + C 级合并失败案例）

---
- [5维 MECE 覆盖对照（本周）](raw/20260812_151848_88f6d95e.md) — ingested 2026-08-12

## 5 维度分类边界（MECE）

| 维度 | 判定锚点 | 典型动作 |
| --- | --- | --- |
| 一、系统功能优化及新增 | 代码/配置变更 → 产生新功能或修复既有功能 | 开发 / 修复 / 上线 |
| 二、流程、权限、账号管理 | 流程规范/审批节点/账号生命周期 | 调整 / 开通 / 回收 |
| 三、数据处理 | 数据采集/清洗/迁移/查询/报表 | 清洗 / 迁移 / 核对 |
| 四、系统运维支撑 | 服务器/中间件/数据库/网络/客户端 | 扩容 / 重启 / 告警处置 |
| 五、需求沟通 | 跨部门协调/需求评审/方案对齐 | 沟通 / 对齐 / 评审 |

---

## 维护规则

- 新素材 ingest 后自动追加到对应章节
- 条目格式：`- [标题](raw/文件名) — ingested YYYY-MM-DD`
- 删除条目前必须先确认 raw 文件已无用
- 模板 v2 关键变更（2026-08-07）：
  - 5 维度 MECE 边界表显式化
  - S/A/B/C 分级铁律 + 量化锚点
  - 新增「本周 Top 3 亮点」+「跨周闭环追踪」模块
  - C 级条目强制合并汇总（禁止逐条列）
- 详情见 [AGENTS.md](AGENTS.md)
