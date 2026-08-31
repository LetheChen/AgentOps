---
name: generative-ui
description: 生成式 UI 指南——通过 present_content（便捷模板）+ upsert_generated_view（自由 A2UI）向 Supervision 大屏展示可视化内容
domain: _shared
depends_on: []
---

# 生成式 UI 指南

> 本 skill 教你（Agent）如何把可视化内容渲染到右侧 Supervision 大屏。
> **首选 `present_content`**（12 种 content_type 模板，预校验不会降级）——你只需选 content_type + 准备 data，工具内部自动映射、校验、推送。
> 复杂布局用 `upsert_generated_view` 直接写 A2UI 组件树（escape hatch）。
> v2（2026-08-23）：对话流回归纯 markdown，所有 A2UI 展示统一渲染到 Supervision 大屏。`form` content_type 已废弃（HIL 走 `request_human_input` 文本问答）。两个工具都 emit `REPORT_SURFACE_STATE` 上大屏，按 view_id 聚合（同 id 替换）。

## 〇、先分清你在哪个通道

| 你的运行场景 | 用什么 | 渲染位置 |
|---|---|---|
| **对话场景-便捷模板** | `present_content`（12 种 content_type，form 已废弃） | Supervision 大屏 |
| **对话场景-自由 A2UI** | `upsert_generated_view`（escape hatch） | Supervision 大屏 |
| **DAG 工作流节点**（yaml 里的 agent 节点） | `report_surface_state`（在 role_prompt 里约定，见 workflow-author skill §2.5） | 生成式 UI 大屏（SupervisionPanel 瀑布流卡片） |

DAG 节点**不要**调 `present_content` 或 `upsert_generated_view`——大屏卡片走 surface 协议，按 (actor_id, view_id) 聚合、phase 原地替换。

### 何时用 upsert_generated_view（自由 A2UI）

当 `present_content` 的 12 种 content_type 无法满足复杂布局需求时：

- 需要自定义版式（如 2 列 + 底部时间线混合，12 种 content_type 覆盖不了）
- 需要混合组件（表格 + 进度 + 图表同屏）
- `present_content` 的预设模板无法表达的复杂布局

**工具签名**：

```python
upsert_generated_view(
    view_id: str = "manager-live",      # view 标识，同 view_id 替换
    title: str,                          # 面板标题
    components: list,                    # A2UI v1.0 组件树，root 组件必须存在
    data: dict = {},                     # 数据模型（组件 source.path 绑定）
    phase: str = "final",                # started/partial/final
    surface_properties: dict = None,     # 可选 surface 属性
) -> dict
```

**可用组件（被动展示）**：
Text/Image/Icon/Video/AudioPlayer/Row/Column/List/Card/Tabs/Divider +
AoGrid/AoTable/AoTimeline/AoMetric/AoStatusBadge/AoProgress/AoStep/AoList/AoBarChart/AoLineChart/AoPieChart/AoDag/AoDisclosure/AoLink/AoArtifact

**禁止**：Button/TextField/Modal/CheckBox（被动展示，交互走 actions）+ `action/html/onClick/script/srcdoc` 字段（防注入）

**选择原则**：
- 标准展示（表格/图表/指标/进度）→ `present_content`（便捷模板）
- 复杂布局/自定义版式 → `upsert_generated_view`（自由 A2UI）
- 两者都渲染到 Supervision 大屏，按 view_id 聚合（同 view_id 替换）

---

## 一、何时展示

| 场景 | 做法 |
|---|---|
| 简单文本回复（一句话能说清） | 直接输出对话消息，**不调工具** |
| 需要可视化展示（表格/图表/指标/进度/列表/对比/流程等） | 调用 `present_content`（渲染到大屏） |
| 需要多面板组合（如运维大屏） | 调用 `present_content`，content_type 用 `dashboard` |
| 复杂布局（12 种模板覆盖不了） | 调用 `upsert_generated_view` 直接写 A2UI 组件树（见 §〇） |
| 需要收集用户输入 | 调用 `request_human_input`（文本问答，不走 form） |

**决策优先级**：
1. 用户明确要求展示形式 → 按用户要求选 content_type
2. workflow 预设了展示方式 → 按 workflow 配置
3. 通用规范 → 按下文 §三 的"内容类型→content_type"映射表选择

---

## 二、present_content 调用方法

### 2.1 工具签名

```python
present_content(
    title: str,                    # 展示标题（1-200 字符）
    content_type: str,             # 内容类型枚举（12 种，见 §三；form 已废弃）
    data: dict,                     # 数据，按 content_type 对应的 schema
    tone: str = "neutral",         # 可选整体 tone：neutral/info/positive/warning/critical
    widget_id: str = None,         # 可选，同 id 替换大屏同卡片、不同 id 累积（见 §五）
    actions: list = None,           # v2 已弃用（form 废弃 + emit REPORT_SURFACE_STATE 不再带 actions）；传了会被忽略
) -> dict
```

### 2.2 返回值

```python
{
    "widget_id": "w_xxx",          # 推送的 widget id
    "status": "committed" | "rejected" | "partial",
    "error": None | str,           # 失败时的错误信息
    "failed_panels": [] | [...]    # 仅 partial 时，列出失败的子面板 index
}
```

| status | 含义 | 你应做的 |
|---|---|---|
| `committed` | 全部推送成功 | 继续后续任务 |
| `rejected` | 校验失败，未推送 | 按 §2.3 错误处理修正后重试（最多 3 次） |
| `partial` | dashboard 部分子面板失败 | 查 `failed_panels` 修正失败子面板，用相同 widget_id 重发整个 dashboard |

### 2.3 错误处理

| error 示例 | 原因 | 应对 |
|---|---|---|
| `"metrics[0]: missing required field 'label'"` | schema 校验失败 | 检查 data 结构，按 §四 schema 补全字段后重试 |
| `"field 'title' contains forbidden pattern: <script>"` | 防注入校验失败 | 移除危险内容（HTML/JS/原型链污染）后重试 |
| `"component count 135 exceeds limit 128"` | 预算超限 | 减少 data 条目数（如 rows ≤ 50）或拆分为多次调用 |
| `"content_type 'bar_chart' does not support actions"` | content_type 不支持 actions | 移除 actions 参数 |
| 重试 3 次仍失败 | — | 放弃展示，用对话消息告诉用户"展示失败，原因：{error}" |

---

## 三、content_type 选择指南（按内容类型）

| 你的数据 | content_type | 典型示例 |
|---|---|---|
| 多个数值指标 | `metric_group` | CPU/内存/磁盘利用率 |
| 同构对象数组 | `table` | 实例列表、配置项 |
| 时间排序事件 | `timeline` | 执行日志、变更记录 |
| 多步骤进度 | `progress` | 巡检进展、部署步骤 |
| 两个方案对比 | `comparison` | A vs B 决策 |
| 流程依赖图 | `dag_flow` | DAG 工作流节点 |
| 可折叠详情列表 | `disclosure_list` | 错误详情、调试信息 |
| 分类数值对比 | `bar_chart` | 各服务 QPS 对比 |
| 时间趋势 | `line_chart` | 温度趋势、流量趋势 |
| 占比分布 | `pie_chart` | 流量来源、资源占比 |
| 图片/视频/音频 | `media` | 生成结果预览、截图、旁白音频 |
| ~~用户输入表单~~ | ~~`form`~~ | v2 已废弃，HIL 走 `request_human_input` |
| 多面板组合 | `dashboard` | 运维大屏、综合报告 |

**选择原则**：根据你要展示的**内容性质**选 content_type，不要根据用户提问的措辞猜。例如用户问"列出实例"，内容是同构对象数组 → `table`；用户问"服务健康概览"，内容是多个指标 → `metric_group`。

---

## 四、各 content_type 的 data 格式

### 4.1 metric_group（指标卡组）

```python
present_content(
    title="服务器概览",
    content_type="metric_group",
    data={"metrics": [
        {"label": "总实例", "value": "12", "tone": "neutral"},
        {"label": "健康", "value": "10", "tone": "positive"},
        {"label": "告警", "value": "2", "tone": "critical"}
    ]}
)
```
- `metrics`：1-12 个，每项 `{label(必填), value(必填, string|number), unit?, tone?}`

### 4.2 table（数据表）

```python
present_content(
    title="实例列表",
    content_type="table",
    data={
        "columns": [
            {"id": "name", "label": "名称", "format": "text"},
            {"id": "cpu", "label": "CPU%", "format": "number"},
            {"id": "status", "label": "状态", "format": "status"}
        ],
        "rows": [
            {"name": "web-01", "cpu": 45, "status": "ok"},
            {"name": "web-02", "cpu": 78, "status": "warning"}
        ]
    }
)
```
- `columns`：1-24 个，每项 `{id(必填), label(必填), format?}`，format 可选 text/number/status
- `rows`：≤50 行，每行是 `{column_id: value}` 对象
- **注意**：columns 只需写 `id`，不需要写 `path`（工具内部自动生成）

### 4.3 timeline（时间线）

```python
present_content(
    title="执行日志",
    content_type="timeline",
    data={"events": [
        {"time": "10:00", "title": "启动", "detail": "agent 初始化完成", "tone": "info"},
        {"time": "10:05", "title": "扫描", "detail": "发现 3 个问题", "tone": "warning"},
        {"time": "10:10", "title": "完成", "detail": "报告已生成", "tone": "positive"}
    ]}
)
```
- `events`：≤50 个，每项 `{time(必填), title(必填), detail?, tone?}`

### 4.4 progress（进度+步骤）

```python
present_content(
    title="巡检进展",
    content_type="progress",
    tone="info",
    data={
        "percent": 75,
        "steps": [
            {"title": "扫描日志", "detail": "完成，扫描 1280 行", "status": "done"},
            {"title": "分析异常", "detail": "完成，发现 5 条 critical", "status": "done"},
            {"title": "生成报告", "detail": "进行中...", "status": "active"},
            {"title": "推送告警", "detail": "等待中", "status": "pending"}
        ]
    }
)
```
- `percent`：0-100 整数
- `steps`：每项 `{title(必填), detail?, status?}`，status 可选 done/active/pending

### 4.5 comparison（A vs B 对比）

```python
present_content(
    title="方案对比",
    content_type="comparison",
    data={
        "left": {"title": "方案 A", "items": [{"label": "成本", "value": "低"}, {"label": "性能", "value": "中"}]},
        "right": {"title": "方案 B", "items": [{"label": "成本", "value": "高"}, {"label": "性能", "value": "高"}]}
    }
)
```
- `left`/`right`：各 `{title(必填), items: [{label, value}]}`

### 4.6 dag_flow（流程图）

```python
present_content(
    title="工作流节点",
    content_type="dag_flow",
    data={"nodes": [
        {"id": "scan", "title": "扫描", "status": "done"},
        {"id": "analyze", "title": "分析", "status": "active", "depends_on": ["scan"]},
        {"id": "report", "title": "报告", "status": "pending", "depends_on": ["analyze"]}
    ]}
)
```
- `nodes`：≤50 个，每项 `{id(必填), title(必填), status?, depends_on?[]}`，status 可选 done/active/pending

### 4.7 disclosure_list（可展开列表）

```python
present_content(
    title="错误详情",
    content_type="disclosure_list",
    data={"items": [
        {"title": "ERROR 1: 数据库连接失败", "detail": "Connection refused at 10.0.0.1:5432", "tone": "critical"},
        {"title": "WARN 2: 内存使用率 85%", "detail": "建议扩容或检查内存泄漏", "tone": "warning"}
    ]}
)
```
- `items`：每项 `{title(必填), detail(必填), tone?}`

### 4.8 bar_chart（条形图）

```python
present_content(
    title="各服务 QPS",
    content_type="bar_chart",
    data={"items": [{"label": "API", "value": 1200}, {"label": "Web", "value": 800}], "unit": "QPS"}
)
```
- `items`：每项 `{label(必填), value(必填, number), tone?}`
- `unit`：可选，数值单位

### 4.9 line_chart（折线图）

```python
present_content(
    title="温度趋势",
    content_type="line_chart",
    data={
        "x_axis": ["08-13", "08-14", "08-15", "08-16", "08-17"],
        "series": [
            {"name": "最高温", "data": [32, 31, 30, 29, 28]},
            {"name": "最低温", "data": [24, 23, 22, 21, 22]}
        ],
        "unit": "°C"
    }
)
```
- `x_axis`：x 轴标签数组（字符串）
- `series`：每项 `{name, data: [number]}`，data 长度需与 x_axis 一致
- `unit`：可选

### 4.10 pie_chart（饼图）

```python
present_content(
    title="流量来源",
    content_type="pie_chart",
    data={"items": [
        {"label": "搜索", "value": 45},
        {"label": "直接", "value": 30},
        {"label": "推荐", "value": 25}
    ], "unit": "%"}
)
```
- `items`：每项 `{label(必填), value(必填, number), tone?}`
- `unit`：可选

### 4.11 media（媒体展示）

```python
# 图片
present_content(
    title="生成结果预览",
    content_type="media",
    data={
        "type": "image",
        "url": "https://example.com/output.png",
        "caption": "视频管线生成的缩略图",
        "fit": "contain",
        "variant": "largeFeature"
    }
)

# 视频
present_content(
    title="生成视频",
    content_type="media",
    data={"type": "video", "url": "https://example.com/output.mp4"}
)

# 音频
present_content(
    title="旁白音频",
    content_type="media",
    data={"type": "audio", "url": "https://example.com/voiceover.mp3", "caption": "TTS 生成的旁白"}
)
```
- `type`：必填，`image`/`video`/`audio`
- `url`：必填，scheme 必须是 `http`/`https`/`data`（拒绝 `javascript:`/`file:`）
- `caption`：可选，媒体下方说明
- `fit`（仅 image）：可选，contain/cover/fill/none/scale-down，默认 contain
- `variant`（仅 image）：可选，icon/avatar/smallFeature/mediumFeature/largeFeature/header，默认 mediumFeature

### 4.12 form（表单输入）—— v2 已废弃

v2 改造后 `form` content_type **已废弃**，调用会被工具拒绝（`rejected: form content_type 已废弃`）。

**需要收集用户输入时**：调用 `request_human_input(prompt="...", fields=[...])` 走文本问答（用户在对话流文字回复，不走 A2UI 表单）。

> 历史背景：v1 时 form 走对话内联 A2UI 表单 + HIL 队列。v2 大屏纯展示 + 对话流纯文本后，form 的交互组件（TextField/Button）与 upsert 被动白名单冲突，且 codex harness 不支持 HIL 等待，故废弃。

### 4.13 dashboard（多面板组合）

```python
present_content(
    title="运维大屏",
    content_type="dashboard",
    data={"panels": [
        {"title": "概览", "content_type": "metric_group", "data": {"metrics": [{"label": "CPU", "value": "45%"}]}},
        {"title": "明细", "content_type": "table", "data": {"columns": [...], "rows": [...]}},
        {"title": "趋势", "content_type": "line_chart", "data": {"x_axis": [...], "series": [...]}}
    ]}
)
```
- `panels`：1-12 个，每项 `{title(必填), content_type(必填), data(必填), tone?}`
- 每个 panel 的 content_type/data 格式与单面板一致
- **dashboard 不允许嵌套 dashboard**（会校验失败）
- 子面板数超过 12 返回 rejected

---

## 五、widget_id 规则（v2：大屏卡片替换/累积）

| 场景 | widget_id 处理 | 效果 |
|---|---|---|
| 追加新卡片（展示不同内容） | 不传，或传新 id | 大屏累积新卡片 |
| 更新已有卡片（如进度推进） | 传之前返回的 widget_id | 替换原卡片内容 |
| 多卡片并列展示 | 多次调用，每次不同 id | 大屏多卡片并列 |

**默认**：不传 widget_id（工具自动生成）。仅当需要"更新"已有卡片时才传之前返回的 widget_id。

**v2 注意**：present_content 内部用 `view_id=manager-live`、`phase=final` emit 到大屏。多个 widget_id 的卡片在 SupervisionPanel 按 `(actor_id, view_id)` 聚合——同 view_id + 同 phase=final 会**替换**（`shouldReplace(final, final)=true`）。所以"累积多卡片"实际靠不同 widget_id 生成不同 surface_id（digest 不同）实现。

**示例**：先展示进度 50%，推进到 75% 时用相同 widget_id 重发：
```python
# 第一次
r1 = present_content(title="巡检进展", content_type="progress", data={"percent": 50, "steps": [...]})
# r1.widget_id = "w_abc"

# 第二次（更新）
present_content(title="巡检进展", content_type="progress", widget_id="w_abc", data={"percent": 75, "steps": [...]})
```

---

## 六、交互按钮（actions）—— v2 已弃用

v2 改造后 `present_content` emit `REPORT_SURFACE_STATE` 不再带 actions，参数传了会被忽略。
历史支持的 content_type（form/table/disclosure_list/dashboard）的 actions 也随 form 废弃 + upsert 被动白名单禁 `action/actions` 字段而失效。

**需要交互时**：
- 用户输入 → `request_human_input`（文本问答）
- 按钮回调 → 大屏 A2UI Button 组件被 upsert 被动白名单禁止，目前无 A2UI 按钮通道；如确需可走 `request_human_input` 让用户选择

> v1 历史参数：`actions=[{"name": "approve", "label": "批准", "tone": "positive"}]`，已不可用。

---

## 七、tone 参数说明

`tone` 表示语义色调，用于强调内容性质：

| tone | 含义 | 典型用途 |
|---|---|---|
| `neutral` | 中性（默认） | 普通信息 |
| `info` | 提示 | 进度、状态 |
| `positive` | 正向 | 成功、健康 |
| `warning` | 警告 | 异常但非严重 |
| `critical` | 严重 | 错误、告警 |

`tone` 是"默认 tone"，仅在组件支持 tone 字段时应用；`data` 中字段级 tone（如单个 metric 的 tone）优先于参数 tone。

---

## 八、你不需要关心的事

以下由工具内部处理，你**不需要也不应该**直接写：
- A2UI 协议字段（`surface.version` / `surface.catalogId` / `components` 数组）
- 组件 id 命名和邻接关系
- `source.path` / JSON Pointer 语法
- `children: [id字符串]` 引用
- 任何 `Ao*` 组件名（AoGrid/AoTable/...）
- 防注入校验、预算约束校验、reachable 校验

**需要 present_content 12 种模板覆盖不了的复杂布局时**：
- **对话场景**：调用 `upsert_generated_view` 直接写 A2UI 组件树（见 §〇「何时用 upsert_generated_view」）。注意每个组件有必填字段（如 AoDisclosure=title+children / AoTable=source+columns / AoArtifact=kind+uri），漏字段会被后端拒绝
- **DAG 场景**：定制 `actor_visual_profile.json` 的 `template` 组件树（见 workflow-author skill §2.5），模板支持完整 Ao* 组件编排

---

## 九、minimax Responses API 序列化坑

minimax 会把 array 字段序列化为 `{item: [...]}` 包装对象，直接 `.map` 会崩溃。
所有 widget 入口已统一调 `unwrapArray` 还原为 array，你只需按本 skill 的 data schema 传数组即可，无需额外处理。
