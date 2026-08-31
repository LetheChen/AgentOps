---
name: _shared
description: AgentOps 跨域共享规则——端口表、Provider 边界、反模式、验证阶梯
domain: _shared
depends_on: []
---

# AgentOps 共享规则

> 所有 agent 都必须遵守的跨域规则。本 skill 永远在 skill 列表顶部，不需要主动 read_skill 也能看到摘要。

---

## 一、端口表（agent 间数据契约）

| 端口类型 | 字段约定 | 示例 |
|---|---|---|
| 文本内容 | `{content: str, summary: str}` | `{content: "完整文本...", summary: "前 200 字符..."}` |
| 文件路径 | `{path: str, format: str}` | `{path: "workspace/wf/run/file.md", format: "markdown"}` |
| 结构化数据 | `{data: dict, schema: str}` | `{data: {...}, schema: "issues_list_v1"}` |
| 多项列表 | `{items: list, count: int}` | `{items: [...], count: 3}` |

**铁律**：output port 必须在 workflow yaml 声明，下游 input port 名称必须匹配（validator 规则 8 会校验）。

---

## 二、Provider 边界

| Provider | 适用场景 | 禁止场景 |
|---|---|---|
| MiniMax-M3 | 通用对话 / 工具调用 / Manager 路由 | 长文档生成（>2000 字符易截断） |
| MiniMax-M2.7-highspeed | 高频短任务（巡检/扫描） | 复杂推理 / 多轮工具调用 |
| DeepSeek-v4-pro | 长文档生成 / 报告撰写 | 实时对话（延迟较高） |
| DeepSeek-v4-flash | 快速判定 / 分类 | 长文本生成（质量不足） |

**模型覆盖优先级**：workflow yaml `node.model` > agent yaml `model` > 全局 `default_model`。

---

## 三、反模式（禁止）

1. **节点内嵌完整业务逻辑**：节点应只做"调工具 + 组装输出"，业务规则放 knowledge_base
2. **跨节点共享可变状态**：节点间只能通过 output port 传递数据，不能读写全局变量
3. **agent 调用其他 agent**：agent 间不能直接调用，必须通过 workflow DAG 拓扑
4. **节点内打开新会话**：节点执行是同步的，不能在节点内 `start_session` 创建子会话
5. **硬编码 workflow 路由表**：新增 workflow 只需放 yaml 到 `workflows/`，WorkflowRegistry 自动发现
6. **节点级工具白名单**：工具权限在 agent yaml 配置，节点不能扩展或收窄

---

## 四、验证阶梯

修改 workflow yaml 后必须跑 `python cli.py validate <file>`，三层校验全过：

| 层 | 校验内容 | 失败示例 |
|---|---|---|
| 结构 | 字段类型 / 枚举 / 必填 | `harness: foo`（无效枚举） |
| 语义 | agent 存在 / port 匹配 / 事件名一致 / role_prompt 依附 agent | `agent: nonexistent` |
| 图论 | 无环 / 可达终止 / skip_if 引用完备 | `A → B → A`（循环） |

---

## 五、Workspace 产物约定

- 运行时产物存 `workspace/{workflow_id}/{run_id}/` 下
- `workflows/` 只放 yaml 定义，不放二进制产物
- agent 通过 `output_files` 字段声明产物路径，引擎自动收割
- 目录路径以 `/` 结尾（如 `reports/`）
