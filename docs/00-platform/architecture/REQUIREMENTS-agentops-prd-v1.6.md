# AgentOps 多Agent协作管理平台 · 项目需求文档（PRD）

> **文档版本**：v1.6（2026-07-30 修订：整合 `docs/research/agent-tool-kb-permission-design.zh-CN.md` 专题——明确工具白名单 deny-by-default 合并语义 + KB ACL tag 格式 + 权限装配 Defense in Depth 链路）
> **创建时间**：2026-07-29
> **项目代号**：AgentOps（暂定名）
> **项目形态**：**全新 Python 工程（绿地重写，不在任何前代代码上重构）**；工程根目录 `E:\Project\Agent_Ops`，文档统一存放 `docs/`；需求与经验来源：`E:\Project\AgentOps` 前代工程、AI_Agent_Platform（智能审批）、龙门客栈（协作范式）
> **文档性质**：项目需求文档——定义"做什么、为什么、做到什么程度"，技术方案仅在与需求强绑定时给出方向性约束
> **参考文档**：见附录 A

---

## 目录

1. [背景与项目概述](#1-背景与项目概述)
2. [核心概念与术语表](#2-核心概念与术语表)
3. [关键设计决策（含开放问题结论）](#3-关键设计决策含开放问题结论)
4. [总体架构](#4-总体架构)
5. [功能需求](#5-功能需求)
6. [非功能需求](#6-非功能需求)
7. [核心数据实体概览](#7-核心数据实体概览)
8. [分期规划与里程碑](#8-分期规划与里程碑)
9. [风险与缓解](#9-风险与缓解)
10. [遗留开放问题](#10-遗留开放问题)
11. [附录](#11-附录)

---

## 1. 背景与项目概述

### 1.1 背景

本项目为**全新 Python 工程（绿地重写）**——不在任何前代代码上重构，以三份需求与经验来源为输入：

| 来源 | 位置 | 角色 | 继承内容 |
|---|---|---|---|
| **前代工程 AgentOps** | `E:\Project\AgentOps` | 需求验证 + 教训 | 已验证的需求场景（DAG 编排、生成式 UI、跨域调度、知识库）；工程教训清单；**不迁移任何资产，全部重新开始（2026-07-29 用户决策）** |
| **AI_Agent_Platform** | `E:\Project\AI_Agent_Platform` | 业务场景 | OA 智能审批 9 步管线、程序化检查器体系、声明式审核项 |
| **龙门客栈（longmen-inn）** | `github.com/LetheChen/openclaw-longmen-inn`（设计文档：`E:\Document\06-平台设计\longmen-inn设计文档`） | 协作范式 | 角色分工与禁止事项、任务看板、凭据归档、记忆流水账 |

#### 来源一：前代工程 AgentOps（`E:\Project\AgentOps`）

前代工程（Python 3.11 + FastAPI + React）已跑通本平台的核心概念验证，**证明了需求的真实性**：

| 已验证能力 | 说明 | 新项目的态度 |
|---|---|---|
| DAG 引擎（四 RunMode） | templated / conversational / task / hybrid 四种运行模式、skip_if 条件、断点文件恢复、节点超时 | 保留概念，重新设计（协议化、状态机纯化） |
| 生成式 UI Widget 体系 | 声明式 6 类 + 生成式 7 类共 13 类 widget，WidgetRenderer 统一路由 + ErrorBoundary | 保留概念，升级为受限协议渲染（FR-07） |
| 跨域协调器 | 7 个业务域（smart_* / personal_assistant / video_production）跨域调度 | 演进为 Manager Agent 的任务分解与 DAG 路由 |
| 多 harness 适配实战 | opencode / codex / local_llm / deterministic 四个适配器的接入与踩坑 | 沉淀为 harness 抽象层；**实战教训直接转化为选型决策（§3.5）** |
| 凭证库 | Fernet 加密 CredentialStore + models.yaml + fallback chain | 继承设计，纳入统一凭据管理（FR-10-5） |
| 审计事件库 | SQLite WAL 4 表、SSE 事件流、stale run sweep | 继承设计，升级为统一 trace_id + 结构化事件（FR-14） |
| Obsidian 知识库工具链 | obsidian_vault 读写白名单、ingest/query/lint、LLM Wiki 双枢纽 | 作为 FR-15 的直接需求输入 |
| 工作流模板 | video-pipeline（搜索→分镜→语音→图像→合成→渲染）、log-patrol | 作为场景需求参考（S3 等），不迁移资产 |

**不在其上重构的原因（工程教训清单，新项目必须规避）**：

| 教训 | 新项目对策 |
|---|---|
| 源码编码事故（GBK↔UTF-8 错误转换致 SyntaxError，数百行受损） | 全仓 UTF-8 无 BOM + CI 编码校验 + lint 门禁（NFR-E6） |
| SSE 单队列单消费者，多页面争抢事件 | 事件总线多订阅者设计（FR-14 事件流） |
| harness 与厂商耦合踩坑（codex 子进程把 model id 小写化致 MiniMax 拒绝，被迫绕开直连） | harness 抽象层 + 厂商直连兜底双轨（§3.5、FR-10） |
| 第三方 harness 会话残留无法清理（opencode 无 session 删除 API，残留堆积） | 平台自持会话生命周期，不依赖 harness 侧清理能力（FR-11-6） |
| 前端防御性补丁层层堆积（unwrap/normalize 全局兜底） | 渲染协议 schema 校验前置，坏数据在入口拒绝（FR-07-2） |
| 单文件巨型化（api/server.py 3000+ 行） | 模块行数红线 + 分层包结构（§4.3） |

#### 来源二：AI_Agent_Platform（智能审批业务）

生产中的 OA 智能审批系统（嵌入致远 OA 审批流，`docs/oa-audit-config-generator/presentation.html`）：员工提交报销单据后自动触发**脚本固化的 9 步审核管线**——拉取数据 → 激活审核项 → 调度工具 → VLM 识别附件 → 拼装提示词 → **单次 LLM 批量审核** → 拆分结果+程序化检查 → 汇总决策（reject/manual/pass）→ PDF 报告+回调 OA。全程仅需 1~2 次 LLM 调用，成本与延迟极低。

**继承的设计智慧**：

| 资产 | 说明 | 在 AgentOps 中的演进 |
|---|---|---|
| **确定性优先** | "规则能用代码表达就别让 LLM 猜"：程序化检查器（checks）先于 LLM 执行，程序化 reject 优先于 LLM 结论 | 沉淀为设计原则 P11；检查器演进为**脚本节点（Script Node）**（FR-03-10） |
| **声明式零代码扩展** | 新增审核项 = YAML + Markdown，热加载生效 | 演进为 DAG 节点/审核项的声明式配置 + 能力注册中心 |
| **三级决策模型** | reject > manual > pass，manual 转人工复核 | 演进为置信度门禁 + WAITING_FOR_APPROVAL 人工介入节点 |
| **激活条件表达式** | `activate_when` 字段级条件激活审核项 | 演进为 DAG condition 节点与节点启用条件 |
| **单次批量审核** | 前置数据处理（提取/整理/组装）后一次性提交 LLM | 演进为 DAG 中的数据准备脚本节点链 + 审核 agent 节点 |

**暴露的痛点（本项目要解决）**：管线中个别节点存在**产出低置信、结论判断错误**的情况，但脚本固化无法感知与自愈——无法按产物质量重试单个环节、无法让模型复核自己、无法动态路由。用户判断：**脚本固化有确定性、低成本、可预测的优势，但终将被 Agent + DAG 流程编排取代**。AgentOps 的迁移路径不是推倒重来，而是"**管线 DAG 化**"：9 步管线成为首批 DAG 模板（场景 S7），确定性环节保留为脚本节点，需要判断的环节升级为 agent 节点 + 置信度门禁——脚本节点与 agent 节点长期共存，按"确定性优先"原则逐环节演进。

#### 来源三：龙门客栈 longmen-inn（多 Agent 协作范式）

早期的多 Agent 协作原型（基于 OpenClaw）：以客栈文化定义角色分工（老板娘=总控、大掌柜=战略、店小二=调度、厨子=开发、画师=设计、账房先生=质控、说书先生=文档），通过 `INN_RULES.md` 约束角色边界与禁止事项，`LEDGER.md` 任务看板做中央协调，`deliverables/` 目录做凭据归档，`记忆流水账` 做经验沉淀。

**继承的协作范式**：

| 资产 | 在 AgentOps 中的演进 |
|---|---|
| 角色职责边界 + **禁止事项**（负向约束） | Agent Card 增加 `constraints`/`forbidden` 字段（FR-06-1） |
| LEDGER 任务看板（中央协调、状态流转可视） | Agent 管理视图 + 任务看板（FR-08） |
| deliverables/ 凭据归档（产物留证） | 产物库 + 归档保留策略（FR-17） |
| 记忆流水账（跨任务经验沉淀） | 持久化记忆 + 记忆总结（FR-12） |
| 角色间协作纪律 | DAG 编排 + 节点间 handoff 契约 |

#### 各来源共同暴露、本项目必须补齐的能力缺口

- 无跨 Session 长期记忆、无记忆总结、无用户画像
- Agent 无自我描述（role/capability/description），仅是"DAG 节点 + System Prompt"
- 无 Sub-agent 一等抽象（仅有单薄的 advisor 咨询）
- 无节点产出质量评估机制（仅有 quality_gate 雏形；审批管线靠人工发现错误结论）
- 无 Agent 全局监控视图（仅有 ad-hoc CLI / 事件流 / 静态看板）
- 无工作空间分级与产物保留治理（仅有 run workspace 7 天清理策略雏形）
- 无移动端访问能力（仅桌面浏览器）
- 无知识库体系（Obsidian + VLM 为全新需求）
- 智能模型路由未实现（用户明确：**本期不做，预留接口**）

### 1.2 产品定位

**AgentOps 是一个"Manager Agent 超级个人助理 + 可视化作战室"的多 Agent 协作管理平台。**

用户通过 **语音 + 生成式 UI 面板** 与一个 Manager Agent 对话；Manager Agent 负责理解复杂意图、分解任务、规划调度，按需 **直接派遣 Subagent** 或 **调度预置 DAG 工作流** 完成工作；全流程的状态、思考过程、产物、置信度都通过可视化看板实时呈现，且可观测、可审计、可干预。

一句话：**"我说一句话，一个团队的 Agent 替我干活，我在看板上看着他们干，干得不行的打回去重干。"**

### 1.3 目标用户与核心场景

**目标用户**：个人超级用户（单租户、本地优先部署），未来可扩展至多用户/小团队。

**核心场景**（用于校验需求完整性，非穷举）：

| # | 场景 | 涉及核心模块 |
|---|---|---|
| S1 | 研发项目协作：方案设计 agent 出设计文档 → coding agent 写代码 → 验收 agent 检查 → 审计 agent 做代码审计并 git 提交 | DAG 编排、节点能力控制、置信度门禁 |
| S2 | 每日早报：定时/语音触发，搜索汇总后按"用户画像"针对性推送，以卡片/曲线图呈现在面板 | 用户画像、记忆系统、生成式 UI、推送 |
| S3 | 6 步管线视频生成：脚本 → 素材 → 配音 → 字幕 → 封面 → 合成，各节点产物（图片/视频）直接显示在面板 | DAG 编排、生成式 UI（媒体展示）、产物审计 |
| S4 | 文章发布流水线：选题 → 撰写 → 校对 → 排版 → 发布，校对节点置信度不足自动重写，最终产物不满意手动重跑该节点 | 置信度门禁、手动重执行 |
| S5 | 临时任务："帮我查一下 X 并整理成表格"——Manager 直接派遣单个 subagent，结果以表格卡片呈现 | 直接派遣、生成式 UI |
| S6 | 作战室监控：用户打开 Agent 管理视图，看到 5 个 agent 气泡，2 个在干活、2 个干完、1 个报错；点开报错的气泡看思想过程和错误详情，点击重试 | Agent 管理视图、可观测、人工干预 |
| S7 | 智能审批（9 步管线 DAG 化）：OA 单据触发 → 数据准备脚本节点链（提取/整理/组装，确定性）→ VLM 识别 → 批量审核 agent → 程序化检查脚本节点 → 三级决策；低置信环节自动重审，manual 转人工审批卡片；手机端完成审批 | 脚本节点、置信度门禁、人工审批、产物归档、移动端 |
| S8 | Coding 场景：coding agent 在用户信任的项目目录下创建子目录、产出代码文件，审计 agent 审查后 git 提交（需审批）；产物归档，临时文件到期自动清理 | 项目级信任工作区、产物治理、人工审批 |

### 1.4 核心价值主张

1. **意图到执行的最短路径**：语音/自然语言 → Manager 意图识别 → 直接派遣 or DAG 编排，无需手工编排每个细节
2. **能力复用而非流程复制**：Subagent 一次定义、多 DAG 节点胜任复用（1 对多）
3. **质量内建于流程**：节点产出带置信度，低于门禁自动重执行，不满意可手动重执行
4. **一切可见、一切可查**：执行过程、思考过程、产物、决策理由全量可观测、可审计
5. **框架自由切换**：Skill/Plugin/Agent/DAG 全部平台自持，Harness 基座（codex / Claude Code / pi 可选）与模型厂商（MiniMax / DeepSeek / Kimi / OpenCode / 智谱 等）可替换而不影响业务资产

### 1.5 设计原则

继承历史项目已验证原则，并补充本项目新增原则：

| # | 原则 | 说明 | 来源 |
|---|---|---|---|
| P1 | **三层分离** | 能力定义（Agent）≠ 拓扑（DAG）≠ 运行时绑定（RuntimeProfile） | 继承 |
| P2 | **Provider/Harness 无关** | DAG 定义、Agent 定义不含 endpoint/secret/model；模型在运行时注入 | 继承 |
| P3 | **Adapt-not-abstract** | Harness 只做 SDK 事件流适配，不强行统一各家 plan 模式/prompt 格式/工具 schema | 继承 |
| P4 | **能力注册中心统筹** | 所有 Skill/Plugin/Tool/Workflow 由平台按自定义规则统一管理，再投影到当前 Harness | 继承并强化 |
| P5 | **Gateway 确定性** | DAG 控制类节点（条件/汇聚/循环）不调用模型，Manager 本地确定性执行 | 继承 |
| P6 | **质量门禁内置** | 每个 agent 节点可配置置信度门禁，质量是流程的一等公民 | **新增** |
| P7 | **记忆分层与可治理** | 记忆分层（缓存/会话/持久/画像），用户可见、可编、可删、可审计 | **新增** |
| P8 | **UI 即协议** | 生成式 UI 走受限协议（A2UI 子集），宿主负责布局，Agent 只表达意图；fallback 强制 | 继承 |
| P9 | **安全默认收紧** | 凭据隔离、工具白名单、传输防重放、输出静态拦截，默认最小权限 | 继承 |
| P10 | **可观测先行** | 统一 trace_id、结构化日志、全事件落盘，先能看清再谈智能 | 继承并强化 |
| P11 | **确定性优先** | 规则能用代码表达就不用模型猜：确定性逻辑固化为脚本节点，先于/替代 agent 节点执行；模型只处理真正需要判断的环节，脚本结论可作为质量门禁输入 | 继承（AI_Agent_Platform） |
| P12 | **产物与工作区分离治理** | 工作区是过程、产物是结果：成果文件显式登记归档、分级保留；临时产物按策略周期清理，磁盘不无限增长 | 继承并强化（龙门客栈凭据归档 + 产物与工作区分离治理原则） |

---

## 2. 核心概念与术语表

| 概念 | 一句话定义 | 备注 |
|---|---|---|
| **Manager Agent** | 顶层协调者 Agent，用户唯一对话入口，负责意图识别、任务分解、调度规划 | 拥有跨 Session 读取权限（受审计） |
| **Subagent** | 被 Manager 派遣执行具体任务的 Agent，可直接调用或作为 DAG 节点执行体 | 物理上运行在 Worker 容器中 |
| **Agent（能力配置）** | 可复用的逻辑角色定义：角色 + 工具权限 + 知识库权限 + 技能 + 描述 | **不绑定任何 DAG 拓扑**，详见 §3.1 |
| **Agent 卡片（Agent Card）** | Agent 能力配置的可视化呈现 + 运行时状态载体 | 气泡弹框的展开形态 |
| **DAG 流程** | 声明式工作流编排蓝图，nodes + edges 组成的有向无环图 | 不含 endpoint/secret/model |
| **Node（节点）** | DAG 中单一可调度单元；agent 类节点通过引用"胜任"它的 Agent 来执行 | 类型：agent / condition / join / foreach / while / terminal |
| **置信度（Confidence）** | 节点产出结果的质量量化评分，含自评分量与核验分量 | 命名决策见 §3.2 |
| **质量门禁（Quality Gate）** | 基于置信度的节点放行机制：低于阈值触发自动重执行 | 见 §3.2 |
| **Harness** | Agent 背后的模型适配框架（Claude Agent SDK / Codex AppServer / Kimi Code / PI） | 平台不自研 harness |
| **能力注册中心（Capability Registry）** | 平台对 Skill / Plugin / Tool / Workflow 的统一管理平面 | 切换 harness 无感的关键 |
| **RuntimeProfile** | 运行时绑定配置：把 Agent/DAG 绑定到具体 provider/model | 运行时注入 |
| **DispatchEnvelope（派发信封）** | Manager → Subagent 的完整传递物（配置+技能+输入+凭据投影+活动元数据） | 继承历史设计 |
| **Handoff（交接）** | Subagent 执行完成后回传结果的动作与数据载体 | 带 Transport Fence 防重放 |
| **Surface（面板）** | 生成式 UI 的分区：task / execution / result / ambient | 继承 A2UI 体系 |
| **气泡弹框（Bubble）** | Agent 管理视图中每个活跃 Agent 的实时状态悬浮入口 | 类"桌面宠物"交互 |
| **用户画像（User Profile）** | 用户身份、偏好、订阅设置的显式持久化存储 | 记忆治理的一部分 |
| **知识库（KB）** | Obsidian Vault + VLM 索引的本地知识资源，节点级 ACL 控制访问 | 后期实现 |
| **脚本节点（Script Node）** | 不调模型、执行确定性逻辑（注册脚本/检查器）的 DAG 节点 | 继承智能审批"程序化检查器"，原则 P11 |
| **工作空间（Workspace）** | Agent 执行期的文件工作区，分三级：项目级信任区 / Run 级共享区 / 节点暂存区 | 决策见 §3.6 |
| **产物（Artifact）** | 从工作区显式登记提升的成果文件，入产物库独立管理与归档 | 产物 ≠ 工作区文件 |
| **保留策略（Retention Policy）** | 工作区与产物的保留/清理规则：分级保留 + 周期清理 + 手动清理 | 参数化配置 |
| **配对设备（Paired Device）** | 经扫码/token 配对授权访问平台的移动端设备 | 移动端接入安全单元 |
| **DAG Actor（持久执行身份）** | DAG 节点在运行期的持久身份（actor_id）：跨 Worker 容器销毁/重建存活，经可便携 checkpoint 恢复上下文 | 与 Trae/Codex"每 turn 独立容器"的根本差异 |
| **声明式产物（Declared Artifact）** | 节点/工作流预先声明的产物：handoff 产物（经 JSON 契约校验）或 workspace 产物（run 终结自动打包归档） | 区别于"事后手动登记" |
| **Runtime Placement** | Agent 运行位置：`host`（宿主机直连 daemon）/ `host_shell`（宿主机子进程）/ `container`（Docker 容器） | 决定隔离强度与启动速度 |
| **控制平面 / 数据平面** | 启用容器隔离时的两条隔离通道与 token 域：控制平面管容器生命周期，数据平面管任务派发与回传 | 容器化场景的信任分离 |

---

## 3. 关键设计决策（含开放问题结论）

本章回答需求描述中提出的 4 个开放问题。**这些决策是后续所有设计的基线。**

### 3.1 决策一：Subagent 采用"胜任"模式，而非"绑定"模式

**问题**：子 Agent 与 DAG 流程节点的关系，是"绑定节点"还是"胜任节点的工作能力"？

**结论**：**胜任模式（Competency-based Reference）**——Agent 是能力配置，Node 是拓扑节点，Node 通过逻辑引用声明"由哪个 Agent 胜任"，同一 Agent 可被多个 Node、多个 DAG 复用（1 对多）。

```text
  agents（能力层，可复用）          nodes（拓扑层，引用）
  ┌──────────────┐           ┌──────────────────────┐
  │ designer      │ ←──────── │ design   (kind:agent) │
  │ role: planner │           └──────────────────────┘
  │ tools: [搜索,  │ ←──────── │ refine   (kind:agent) │  ← 同一 Agent 胜任多个节点
  │        编辑,写入]│          └──────────────────────┘
  │ kb: [设计规范库] │
  └──────────────┘
  ┌──────────────┐           ┌──────────────────────┐
  │ coder         │ ←──────── │ coding   (kind:agent) │
  └──────────────┘           └──────────────────────┘
```

**理由**（历史项目已验证 + 用户直觉正确）：

| 维度 | 绑定模式 | 胜任模式（采用） |
|---|---|---|
| 复用性 | 一个 agent 只能服务一个节点，配置大量复制 | 一次定义，多节点/多 DAG 引用 |
| 模型绑定 | 拓扑与模型耦合，换模型要改 DAG | RuntimeProfile 运行时注入，同一 DAG 可在 Claude/Codex/Kimi 间切换 |
| 智能派遣 | 无匹配依据 | Manager 可按 agent 的 role/capability/tags 做"胜任度匹配"（为后续智能派遣/模型路由留接口） |
| 用户场景契合 | — | 完全契合："做方案规划的 agent 具备了搜索/编辑/写入等工具权限，可胜任具备这些权限要求的 DAG 节点" |

**补充设计**：胜任模式下，Node 侧可声明"胜任要求"（required capabilities / 最低工具集合），调度时校验 Agent 是否满足；不满足则拒绝派发并给出诊断。这为后续"Manager 自动挑选最胜任的 agent"提供规则基础。

### 3.2 决策二：节点产出质量量化命名为"置信度（Confidence）"，机制为"质量门禁（Quality Gate）"

**问题**：节点输出结果的置信度概念叫什么好？

**结论**：正式命名为 **置信度（Confidence Score）**，完整表述为"节点产出置信度"；配套强制机制命名为 **质量门禁（Quality Gate）**。

候选名对比：

| 候选名 | 评价 |
|---|---|
| **置信度 Confidence** ✅ | 业界通用（ML/统计直觉一致），"低于阈值重试"语义自然，中英文都顺口 |
| 把握度 | 口语化，但缺乏机制感 |
| 可靠度 Reliability | 易与系统可靠性（reliability）混淆 |
| 验收分 Acceptance Score | 偏"人工验收"语义，不适合自动门禁 |
| 质量分 Quality Score | 语义过宽，无法区分"产出自评"与"外部核验" |

**双分量设计**（区别于单一自评）：

| 分量 | 来源 | 说明 |
|---|---|---|
| **自评置信度** `self_confidence` | 执行节点自身输出 | Agent 在 handoff 时按契约自评 0~1 分并给出理由 |
| **核验置信度** `verified_confidence` | 下游 verifier 节点 / 规则引擎 / 人工 | 对上游产物的独立核验评分（可选配置） |

**质量门禁规则**（per-node 可配）：

```text
节点产出 → 置信度评估
   ├─ ≥ 门禁阈值        → 放行，handoff 到下游
   ├─ < 门禁阈值        → 自动重执行（带重试策略，见 FR-05）
   │    ├─ 重试次数内达标 → 放行
   │    └─ 超过重试上限  → 挂起 + 通知用户，等待人工决策
   └─ 人工随时可干预     → 手动重执行 / 修改输入后重执行 / 强制放行 / 跳过（全部留审计）
```

### 3.3 决策三：生成式 UI 对表格/图表/HTML/PPT/图片/视频的支持边界

**问题**：面板能展示表格、视图、曲线、HTML、PPT、图片、视频吗？

**结论**：

| 内容形态 | 支持策略 | 实现方式 | 分期 |
|---|---|---|---|
| 表格 / 列表 / 指标 / 进度 | ✅ 原生支持 | A2UI 组件（Table/List/Metric/Progress） | M1 |
| 曲线 / 柱状图 / 时间线 | ✅ 原生支持 | 图表类组件（bar_chart 等视图 + DAG 视图） | M1-M2 |
| 图片 | ✅ 原生支持 | Image 组件（URI 白名单校验） | M1 |
| 视频 / 音频 | ✅ 原生支持 | Video / AudioPlayer 组件 | M2 |
| HTML | ⚠️ 受控支持 | 优先用声明式渲染器表达；确需自定义 HTML 走**沙箱 iframe + CSP 白名单**，禁脚本或受限脚本 | M2 |
| PPT | ❌ 不原生渲染 .pptx | 替代方案：Agent 产出"幻灯片视图"（卡片序列/Slides 式组件）在面板原生呈现；导出物提供 PDF/HTML 格式；原始 .pptx 作为可下载产物 | M2-M3 |

**原则**：面板展示走受限协议（安全、跨设备一致），重格式文件作为"产物（Artifact）"管理——可预览（转图片/PDF）、可下载、可审计，但不在面板内执行其原生格式。

### 3.4 决策四：Manager Agent 应"认识"用户，但画像必须显式、可治理、受权限约束

**问题**：要不要让 Manager Agent 知道我是谁？要不要给它跨 Session 权限和分层记忆？

**结论**：**要**。这是个性化推送（早报、内容生成针对性）的前提。设计如下：

1. **用户画像（User Profile）显式化**：身份、角色、偏好、订阅项（如"每日早报""视频管线风格"）作为一等数据实体存储，**用户可查看、可编辑、可删除、可导出**（记忆治理）。
2. **跨 Session 权限**：Manager Agent 拥有 `cross_session:read` 特权 scope，可在调度时检索所有历史会话与持久记忆；**每次跨域检索写入审计日志**（谁、何时、查了什么、用于哪个任务）。
3. **记忆分层**：临时缓存记忆（turn 级）→ 会话上下文（session 级，窗口+压缩）→ 持久化记忆（跨 session，事件+事实）→ 用户画像/知识库（长期显式）。详见 FR-12。
4. **记忆总结（Memory Summarization）**：要做。Session/Run 结束时由小模型自动摘要写回持久记忆；Manager 新会话按需检索 top-k 注入，避免上下文膨胀。
5. **节点级隔离**：Subagent 默认**看不到**完整用户画像与跨 session 记忆，只能拿到 DispatchEnvelope 中显式注入的最小上下文切片——防止画像信息泄漏到不可控节点。

### 3.5 决策五：Harness 多产品可选（首批 codex / Claude Code / pi），统一适配规范

**结论**（2026-07-29 用户拍板，同日由"codex 单一地基"修订为多产品策略）：

- **Harness 基座 = 多产品可选，首批三个全部一等公民**：

  | Harness | 接入形态 | 特点 | 首批场景 |
  |---|---|---|---|
  | **codex** | `codex app-server`（JSON-RPC over stdio） | thread/turn 会话模型、原生审批/沙箱、Responses 生态 | 默认地基：Manager 与多数节点 |
  | **Claude Code** | CLI 子进程 `claude -p --output-format stream-json`（session 可 resume）；后续可升级 SDK sidecar | live-steer 成熟、thinking 流、Anthropic 系生态 | 需要 steering/思考流的节点 |
  | **pi**（pi-mono） | Node sidecar（pi-agent-core 嵌入式 library + stdio JSON-RPC 桥） | **原生多 provider（minimax/deepseek/kimi/anthropic 内置）**、steer/compaction/tool hooks 一等公民 | 多厂商混排、成本敏感节点 |

  选择哪个 harness 执行某个 agent/节点，由 AgentCard / RuntimeProfile 显式配置，Manager 调度可见（能力矩阵驱动降级）。

- **统一适配规范不动摇**：`run()/resume()/interrupt()` + 统一 AgentEvent 流 + 契约测试金标——任一 harness 接入必须重放同一 fixture 并语义等价。**harness 是执行框架，模型厂商是模型来源，两个概念独立配置**。
- **Python 后端的接入约束**：三个 harness 均为外部进程（codex 为 Rust 二进制、Claude Code 为 Node CLI、pi 为 TS library），统一走"受管子进程 + stdio JSON-RPC"接入。pi 因其 pi-server 的 Unix socket **不支持 Windows（P0）**，采用 sidecar 嵌入模式（依据 `docs/report/` 两份 pi 调研）。
- **厂商接入策略**：不设独立的厂商直连兜底适配器；厂商接入在各 harness 内部解决（codex 走其 provider 配置；pi 原生内置 minimax/deepseek/kimi 等——与预设厂商列表高度重合，是 pi 进首批的加分项；Claude Code 走 anthropic 兼容端点）。已知兼容问题（前代实测 codex model id 归一化被厂商拒绝）按"遇一例修一例"沉淀到厂商兼容矩阵。
- **模型厂商层**：Provider → Endpoint → Model 三级配置，**预设 MiniMax、DeepSeek、Kimi、OpenCode、智谱（GLM）等厂商模板**，凭据加密存储（Fernet 对称加密 + 优先级链注入）。
- **切换无感的机制**：所有 Skill/Plugin/Agent/DAG 定义由**能力注册中心**平台自持，运行时以 as-native 方式投影到当前 harness；切换 harness 只是换一个投影目标，业务资产零改动。
- **智能模型路由**（按任务难度/角色自动选模型/选 harness）：**本期不实现**，但预留 `ModelResolution` 接口与 `model_decision` 审计事件位，Agent 的 role 字段即为将来路由依据。

### 3.6 决策六：三级工作空间模型——"项目级信任区 + Run 级共享区 + 节点暂存区"

**问题**：工作空间按每个 Agent 建、还是按 DAG 工作流建？Coding Agent 是否需要 Trae/Codex 式的项目级信任目录？衍生产物哪些保留、哪些清除、如何清除？

**结论**：**不按 Agent 也不按 DAG 单选——按"用途与生命周期"分三级**。成果物交接走 Run 级共享区，代码产出走项目级信任区，临时产物走节点暂存区。

| 级别 | 名称 | 生命周期 | 用途 | 类比 |
|---|---|---|---|---|
| **L1 项目级信任工作区**（Project Workspace） | 用户显式选择并信任的目录（如代码仓库根目录） | **跨 Run 持久**，不受 run 清理影响 | Coding Agent 创建子目录、产出代码文件；多个 run 可复用同一项目区 | Trae/Codex 的"信任此文件夹" |
| **L2 Run 级共享工作区**（Run Workspace） | 每次 DAG run 自动创建 `workspace/<run_id>/` | run 结束进入保留倒计时 | **节点间成果物交接的主通道**：上游产物落盘、下游读取；调试证据 | run workspace（已验证） |
| **L3 节点暂存区**（Node Scratch） | 节点执行期的私有临时目录 | **run 结束即清理**（可配为节点结束即清） | 中间临时文件、下载缓存、不进入交接的过程数据 | /tmp 语义 |

**关键规则**：

1. **交接语义**：DAG 节点间传递成果物，默认经 L2 共享区落盘 + handoff `artifact_refs` 引用，禁止直接读写他节点的 L3 暂存区
2. **产物提升（Promote）**：工作区文件 ≠ 产物。节点将有价值的成果文件**显式登记**到产物库（复制/引用 + 元数据 + 来源溯源），产物库有独立于工作区的保留策略
3. **项目区信任模型**：L1 目录需用户**显式添加并确认信任**（授权留审计）；Agent 在项目区内的写权限按节点 workspace policy 收敛（可写子目录白名单）；git 提交类操作默认需审批
4. **保留与清理**（继承已验证的 retention 安全模型并扩展）：
   - **保留分级**：`ephemeral`（随 run/暂存区清理）/ `standard`（按天数保留，成功与失败分开配置）/ `pinned`（手动钉住，永不自动清理）
   - **周期清理**：Manager 侧 janitor 定时扫描（默认 6 小时间隔可配），仅清理 terminal 状态的 run；active / pinned / 未知孤儿目录一律跳过；符号链接防护（不跟随、拒绝逃逸）
   - **手动清理**：清理 API 默认 dry-run 预览，UI 确认后执行；策略更新 / 清理 / pin 均走变更授权 + 审计
   - **参数化**：保留策略为平台配置项（启用开关、成功/失败天数、清理间隔），UI 可编辑、原子写入
5. **磁盘防护**：存储用量看板（按级别/按 run 统计），超阈值告警

---

## 4. 总体架构

### 4.1 分层架构

```text
┌─────────────────────────────── 交互层 ───────────────────────────────┐
│  语音通道(ASR/TTS/旁白) │ 生成式UI看板(A2UI) │ Agent管理视图(气泡) │ 移动端(PWA) │ CLI │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
┌─────────────────────────────── 协调层 ───────────────────────────────┐
│  Manager Agent（意图识别/任务分解/调度规划/直接派遣/DAG触发/人工介入入口） │
│  ├─ 用户画像服务   ├─ 记忆系统(分层+总结+检索)   ├─ Session管理(三层)     │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
┌─────────────────────────────── 编排层 ───────────────────────────────┐
│  DAG Engine（纯状态机） │ Dispatcher │ Response Bridge(Fence) │ Supervisor │
│  质量门禁(置信度评估/自动重试/人工挂起) │ 模板库(Pattern) │ 可视化DAG编辑器  │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │  DispatchEnvelope (WS)
┌─────────────────────────────── 执行层 ───────────────────────────────┐
│  Worker/Node 容器：Subagent 运行时（Harness适配器 + DAG工具 + 白名单工具） │
│  Harness 基座(首批): codex │ Claude Code │ pi (+deterministic) │ 扩展位: opencode │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
┌─────────────────────────────── 能力层 ───────────────────────────────┐
│  能力注册中心：Skill │ Plugin │ Tool │ Workflow │ Renderer │ Kind      │
│  （平台自定义规则管理：trust tier / 权限 / 版本 / digest，as-native 投影） │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
┌─────────────────────────────── 资源层 ───────────────────────────────┐
│  模型厂商(预设 MiniMax/DeepSeek/Kimi/OpenCode…) │ 凭据库(加密)          │
│  知识库(Obsidian+VLM, 节点级ACL) │ 三级工作空间(项目/Run/暂存) │ 产物库(归档+保留策略) │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
┌─────────────────────────────── 观测层（横切） ────────────────────────┐
│  统一trace_id │ 结构化日志 │ 事件流(OTel) │ 审计存储 │ 指标(Prometheus) │
└──────────────────────────────────────────────────────────────────────┘
```

### 4.2 运行时拓扑（继承历史已验证模型）

```text
用户 → 语音/文字 → Manager Agent
                       │ 意图识别/分解/规划
                       ├─→ 直接派遣：DispatchEnvelope → Worker(Subagent) → 结果回传
                       └─→ DAG 触发：createRun → DAG Engine 循环取 READY 节点
                              ├─ gateway 节点 → Manager 本地确定性执行
                              └─ agent 节点 → 装配 Envelope(角色+技能+工具白名单
                                   +KB权限+凭据投影+输入) → Dispatcher(WS)
                                   → Worker(Subagent) 执行 → Handoff 回传
                                   → Fence 校验 → Mailbox 投递 → 下游 READY
                                   → …循环至 Terminal
                       │
                       ├─ 全程：Supervisor 聚合状态 → Agent管理视图(气泡)
                       ├─ 全程：事件/产物/transcript 落盘 → 审计与复盘
                       └─ 全程：质量门禁评估置信度 → 自动重试/人工介入
```

### 4.3 代码结构（全新 Python 工程 + React 前端）

工程根目录 = `E:\Project\Agent_Ops`；**全部文档**（PRD、架构设计、调研资料、研究专题）统一存放 `docs/`，代码与文档同仓但分层清晰。

| 顶层目录 | 职责 |
|---|---|
| `docs/` | 全部项目文档：PRD、架构设计、调研资料（research/）、决策记录 |
| `agentops/core/` | 协议与契约单一真相源：事件类型、DAG spec、Envelope、置信度、错误码、术语表 |
| `agentops/orchestrator/` | DAG 引擎（纯状态机）、调度器、质量门禁、脚本节点运行时 |
| `agentops/manager/` | Manager Agent 宿主：意图识别、任务分解、调度规划、直接派遣 |
| `agentops/harness/` | Harness 适配层：`codex`（地基）/ `deterministic`（测试）/ 扩展位（claude/pi/opencode/local_llm） |
| `agentops/capability/` | 能力注册中心：skill/tool/workflow/action/renderer/kind 统一管理 |
| `agentops/memory/` | 四层记忆、记忆总结、检索注入、用户画像 |
| `agentops/workspace/` | 三级工作空间、产物库、retention janitor、git 集成 |
| `agentops/audit/` | 审计事件、trace_id、复盘存储（SQLite WAL） |
| `agentops/server/` | FastAPI 入口：REST + SSE/WS、移动端配对认证 |
| `agentops/cli.py` | 命令行入口 |
| `web/` | React 18 + Vite 前端：生成式 UI 看板、气泡 Fleet 视图、复盘中心、DAG 编辑器 |
| `workflows/` | DAG 模板 YAML（不含 endpoint/secret/model） |
| `config/` | agents / models / knowledge 配置 |
| `tests/` | pytest：契约测试、金标对照、单元与集成测试 |

工程红线：单文件 ≤ 500 行（超出强制拆分）；全仓 UTF-8 无 BOM；`ruff` + `pytest` + 前端 typecheck 为 CI 门禁。

### 4.4 部署架构（右尺寸信任分离，按需启用容器隔离）

```text
┌──────────────────────────── Host OS（PC 宿主机）────────────────────────┐
│  agentops_manager（Python/FastAPI 进程，不容器化）                        │
│    · 信任根：持有全部 secrets / SQLite / DAG Engine / 调度决策            │
│    · Manager Agent 经 codex harness 运行（app-server JSON-RPC 长连接）     │
│      ├─ 默认：harness 以受管子进程/本机 daemon 形态运行（低延迟、低运维）   │
│      └─ 按需：不受信负载（第三方脚本/plugin runtime/coding 沙箱）          │
│           进入 Docker 容器隔离（采用安全隔离姿态，见下表）              │
└──────────────────────────────────────────────────────────────────────────┘
```

| 要点 | 规则 |
|---|---|
| **Manager 不容器化** | secrets 不进 volume、SQLite 用宿主文件系统、OS 信号优雅关闭 |
| **harness 进程隔离** | codex app-server 作为受管子进程：崩溃可感知可重启；token/配置由 Manager 运行时注入，不落子进程环境明文 |
| **容器沙箱按需启用** | 仅当执行不受信负载时：digest-pinned 镜像、readOnlyRootfs、非 root、网络隔离（内部网络或 none）、env 白名单（禁敏感前缀）；plugin runtime 增加启动 attestation + 运行期漂移检测（容器安全沙箱方案） |
| **DAG Actor 持久身份** | actor_id 跨 harness 进程重启存活，checkpoint 恢复——进程是消耗品，Actor 不是 |
| **部署模式** | ①单机开发（默认，全进程本机）②单机 + 容器沙箱（启用不受信负载隔离）③多机分布（远程执行端，远期，Q13） |

---

## 5. 功能需求

> 优先级约定：**P0** = MVP 必须；**P1** = V1 必须；**P2** = V2 规划；**P3** = 远期/按需。
> 每条需求标注验收要点（AC）。

### FR-01 Manager Agent（超级个人助理）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-01-1 | 支持语音 + 文字双通道输入，生成式 UI + 语音双通道输出，不以长篇文字为主要交互 | P0 | 语音提问→面板卡片呈现核心结论，AC: 核心结果以结构化卡片而非纯文本呈现 |
| FR-01-2 | 复杂工作意图识别：将用户请求分类为任务类别（task_class），模糊意图主动澄清 | P0 | AC: 意图分类写审计事件；模糊请求时产出澄清卡片而非盲目执行 |
| FR-01-3 | 任务分解：将复杂请求拆解为子任务序列 | P0 | AC: 分解结果以可视化任务树呈现并经用户确认（可配置为免确认） |
| FR-01-4 | 调度规划：按任务特征选择执行路径——①直接派遣单 Subagent ②匹配预置 DAG 模板 ③分解后动态编排 DAG | P0 | AC: 三种路径均有决策理由记录（decision audit） |
| FR-01-5 | 运行中干预：支持插话（live-steer）、暂停、急停、改派 | P1 | AC: 干预指令带幂等键；harness 不支持 live-steer 时降级为"节点间干预"并提示 |
| FR-01-6 | Manager 持有跨 Session 读取特权，检索受审计（见 §3.4） | P1 | AC: 跨域检索行为 100% 落审计日志 |
| FR-01-7 | Manager 调度时感知用户画像，产出针对性结果（如早报的侧重点） | P1 | AC: 画像注入有 scope 控制；画像版本变化可追溯 |

### FR-02 多 Agent 协作管理（直接派遣）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-02-1 | Manager 可不经过 DAG，直接派遣一个 Subagent 执行临时任务并回收结果 | P0 | AC: 派遣-执行-回传全链路 trace_id 贯通 |
| FR-02-2 | 直接派遣同样装配完整 Envelope（角色/技能/工具白名单/KB 权限/凭据投影），与 DAG 派遣同安全等级 | P0 | AC: 直接派遣不存在权限旁路 |
| FR-02-3 | 支持同步（等待结果）与异步（派发后继续对话，结果回推到面板）两种派遣模式 | P1 | AC: 异步结果以卡片形式推送到 result surface |
| FR-02-4 | 并发派遣控制：同时多个 Subagent 工作时，有并发上限、心跳、超时、熔断 | P1 | AC: 心跳超时→优雅中断→强制中断→标记失败，状态可观测 |
| FR-02-5 | Subagent 间咨询（advisor 机制）：执行中 Agent 可咨询另一个顾问 Agent，有调用次数/token/超时预算 | P2 | AC: advisor 调用独立计数并落审计 |

### FR-03 DAG 流程编排

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-03-1 | 声明式 DAG 定义（YAML），nodes + edges；定义中**禁止**出现 endpoint/secret/model | P0 | AC: schema 校验拒绝含凭据字段的 DAG 文件 |
| FR-03-2 | 节点类型：**agent / script** / condition / join(n_of_m,all,any) / foreach / while(静态上限) / terminal；控制类节点（condition/join/foreach/while/terminal）本地确定性执行不调模型 | P0 | AC: gateway 节点零模型调用（可审计断言） |
| FR-03-3 | 节点状态机：PENDING→READY→RUNNING→COMPLETED/FAILED/CANCELLED，支持 WAITING_FOR_APPROVAL / WAITING_FOR_COMMAND | P0 | AC: 状态转移全量落事件流，可回放 |
| FR-03-4 | 数据边隐含完成依赖；失败 port 传播跳过下游（除显式 on_failure/always） | P0 | AC: 引擎语义以 core 协议为唯一真相源，状态转移有完整契约测试 |
| FR-03-5 | DAG 模板库（Pattern）：预置可参数化实例化的流程模板（如 PR 审查、视频管线、早报、文章发布） | P1 | AC: 模板带参数 schema，实例化时校验 |
| FR-03-6 | DAG 版本管理与灰度：模板版本化，实例化的 run 锁定版本 | P1 | AC: 同一模板不同版本的 run 可并存且可溯源 |
| FR-03-7 | 可视化 DAG 编辑器：拖拽建节点、连线、配参数，与 YAML 双向同步 | P2 | AC: 编辑器产物可通过 schema 校验 |
| FR-03-8 | DAG 录制-回放：任一历史 run 可完整回放（重放事件流）用于复盘与调试 | P2 | AC: 回放不产生副作用（side_effect_free） |
| FR-03-9 | 人工介入节点：WAITING_FOR_APPROVAL 状态下，面板呈现审批卡片，批准后继续 | P1 | AC: 审批操作带签名与审计 |
| FR-03-10 | **脚本节点（Script Node）**：执行确定性逻辑的注册脚本/检查器（如金额阈值、日期区间、字段匹配、发票查重），不调模型；先于/替代 agent 节点执行，其结论可作为质量门禁输入（原则 P11）；脚本经能力注册中心登记 + digest 校验 | P1 | AC: 脚本节点独立审计、超时控制；未登记脚本拒绝执行 |

### FR-04 节点能力控制（角色 / 工具 / 知识库权限）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-04-1 | 每个 DAG 节点独立配置：节点角色定义（引用 Agent + 节点级 system prompt 补充）、输入输出契约 | P0 | AC: 契约校验失败的 handoff 被拒绝并进入校正轮 |
| FR-04-2 | 工具权限三维控制：①harness 内置工具白名单+调用预算 ②DAG 工具白名单 ③Plugin 工具白名单 | P0 | AC: 白名单外工具调用被硬拒绝并告警；**节点级 ⊄ AgentCard 白名单 → 启动期报 AO_E_TOOL_ESCALATION 拒绝加载（deny-by-default，详见 §3.2 合并语义）** |
| FR-04-3 | 知识库访问权限：节点级 KB ACL（ACL tag 格式，如 `kb:read:projects`）；KB 检索作为 `kb_search` tool 接入，ACL 间接受效（工具白名单机制复用） | P2 | AC: 越权检索被拦截并落审计；默认无 KB 访问权；M3 启动前补 ACL tag→路径解析 ADR |
| FR-04-4 | Workspace 读写策略：路径级只读/可写白名单 | P0 | AC: 越权写入被 hook 拦截 |
| FR-04-5 | 凭据投影：manager_broker（引用不接触明文）/ env（受限前缀注入）两种模式；禁止敏感前缀泄漏 | P0 | AC: 输出含凭据值时被拦截（containsCredentialValue） |
| FR-04-6 | 危险输出静态拦截：节点可配置 forbidden/需审批 的危险命令模式（如 `rm -rf /`、`git push --force`） | P1 | AC: 命中即拦截/转审批，事件落审计 |

### FR-05 置信度与质量门禁（核心机制，命名见 §3.2）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-05-1 | 每个 agent 节点产出必须携带自评置信度（0~1 + 理由），作为 handoff 契约的一部分 | P1 | AC: 缺置信度的 handoff 契约校验失败 |
| FR-05-2 | 可选核验置信度：由下游 verifier 节点或规则引擎独立评分 | P1 | AC: 双分量分别存储、分别可查 |
| FR-05-3 | 节点可配质量门禁阈值；低于阈值自动触发重新执行 | P1 | AC: 重试触发事件含原置信度、理由、重试策略 |
| FR-05-4 | 自动重试策略可配：①同配置重试 ②携带失败反馈的校正轮（锁定仅允许 handoff 工具）③升级更强模型重试（预留） | P1 | AC: 每种策略有重试次数上限 |
| FR-05-5 | 超过重试上限 → 节点挂起 + 面板告警卡片，等待人工决策 | P1 | AC: 挂起状态在 Agent 管理视图可见 |
| FR-05-6 | **手动重执行**：用户对任一已完成/失败节点产物不满意，可手动触发该节点重执行（可选修改输入） | P1 | AC: 手动重执行与自动重试走同一审计与 Fence 机制；下游受影响节点按策略重算 |
| FR-05-7 | 人工决策选项：重执行 / 强制放行 / 跳过节点（标记） / 终止 run | P1 | AC: 全部决策落审计（谁、何时、理由） |
| FR-05-8 | 置信度历史统计：按节点/Agent/模型维度聚合，作为后续智能路由反馈信号 | P2 | AC: 复盘中心可查询置信度分布 |

### FR-06 Subagent 注册与胜任管理（模式见 §3.1）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-06-1 | Agent 注册中心：Agent 作为能力配置被创建/编辑/版本化/停用，字段含 role、capabilities、skills、工具权限、KB 权限、描述、版本、**constraints/forbidden（职责边界与禁止事项，继承龙门客栈角色规则）** | P0 | AC: Agent 定义不含 provider/model/secret；forbidden 条目注入 system prompt 并在工具层硬约束 |
| FR-06-2 | 胜任引用：DAG 节点以逻辑名引用 Agent；同一 Agent 可被多节点、多 DAG 引用（1 对多） | P0 | AC: 引用解析与复用在调度层验证 |
| FR-06-3 | 胜任校验：节点可声明胜任要求（必需 capability/工具），调度时校验不满足即拒派并诊断 | P1 | AC: 拒派事件含缺失项清单 |
| FR-06-4 | Agent 自我描述可被 Manager 检索：Manager 派遣时可按 role/capability/tag 搜索"谁胜任" | P1 | AC: 检索结果支撑调度决策并留痕 |
| FR-06-5 | Agent 版本升级：新版本不影响进行中的 run（run 锁定版本） | P1 | AC: 版本切换有审计 |

### FR-07 生成式 UI 可视化看板（边界见 §3.3）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-07-1 | 四 Surface 布局：task / execution / result / ambient；宿主负责布局，Agent 只表达意图 | P1 | AC: 节点 payload 无法注入坐标/可执行代码 |
| FR-07-2 | 受限渲染协议（A2UI 子集）：组件白名单 + 纯函数白名单 + 大小/深度硬限制 | P1 | AC: 超限节点被拒并渲染 fallback |
| FR-07-3 | 内容形态：表格、列表、指标、进度、曲线/柱状图、时间线、DAG 视图、图片 | P1 | AC: 各形态有对应组件与示例 |
| FR-07-4 | 媒体：视频/音频播放器组件；HTML 走沙箱；PPT 走"幻灯片视图+导出物"方案 | P2 | AC: 沙箱 CSP 白名单生效；外部 URI 白名单校验 |
| FR-07-5 | 每节点强制 fallback（title/summary/items），渲染失败也有可读兜底 | P1 | AC: schema 校验无 fallback 即拒绝 |
| FR-07-6 | 节点事务化更新（put/patch/remove，原子+幂等+冲突检测+可重放） | P1 | AC: 事务语义有完整单测覆盖（含冲突/重放/指纹去重） |
| FR-07-7 | 节点可携带操作按钮（Action）：用户点击触发显式意图（如"重新生成封面"），走幂等+确认挑战管线 | P1 | AC: 危险操作强制二次确认 |
| FR-07-8 | 设备自适应：device/viewport/attention 上下文影响排序、容量与密度 | P2 | AC: compact 视口自动降级密度 |
| FR-07-9 | 节点 provenance：每个节点可溯源到 actor/plugin/skill/run | P1 | AC: UI 可查看"这张卡片是谁生成的" |

### FR-08 Agent 管理视图（气泡弹框监控）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-08-1 | Fleet 视图：面板中陈列所有 Agent；活跃 Agent 以"气泡"悬浮呈现（类桌面宠物交互） | P1 | AC: 气泡实时反映状态，延迟 ≤ 2s |
| FR-08-2 | 气泡状态：idle / working / done / error / waiting_approval，颜色与动效区分 | P1 | AC: 状态机与后端 Supervisor 数据一致 |
| FR-08-3 | 点击气泡展开 **Agent 卡片**：思想过程流（thinking/commentary）、当前执行节点、任务执行情况、进度、产物预览、置信度 | P1 | AC: 卡片数据来自实时事件流，可下钻到 transcript |
| FR-08-4 | 全局监控统计：运行中/已完成/失败/等待审批的 Agent 计数与列表 | P1 | AC: 与审计存储一致 |
| FR-08-5 | 卡片内干预操作：暂停/继续/急停/重试/查看产物/查看审计 | P1 | AC: 操作走签名+幂等+审计 |
| FR-08-6 | 系统错误呈现：Worker 掉线、harness 崩溃、凭据失效等系统级错误与业务失败区分呈现 | P1 | AC: 错误分类（系统错误/业务失败/质量不达标）在视图可过滤 |

### FR-09 Skill / Plugin 统筹管理（能力注册中心）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-09-1 | 统一 Catalog：Skill / Tool / Workflow / Action / Renderer / Kind 六类对象统一登记，含跨对象引用校验 | P1 | AC: 引用校验覆盖六类对象互引矩阵，缺引用即拒绝 |
| FR-09-2 | 平台自定义管理规则：trust tier（data_only/sandboxed_runtime/trusted_builtin）、权限声明与授权、版本、digest 锚定 | P1 | AC: 未授权权限的工具不可被调用 |
| FR-09-3 | as-native 投影：Skill/工具集在运行时投影为当前 harness 的原生形态；**切换 harness 业务资产零改动** | P1 | AC: 同一 skill 在 Claude/Codex 两种 harness 下行为等价（有对照测试） |
| FR-09-4 | 生命周期：安装（验签）/启用/禁用/升级/回滚/卸载 | P1 | AC: 全程审计；回滚可用 |
| FR-09-5 | Skill 信任分级：verified/audited/community/draft；低信任 skill 不得进入高权限节点 | P2 | AC: 节点声明最低信任级，运行时校验 |
| FR-09-6 | 管理 UI：浏览/启停/权限授予/版本查看 | P1 | AC: 全部操作有审计与二次确认（危险项） |

### FR-10 Harness 与模型厂商管理

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-10-1 | Harness 基座（首批三个，全部一等公民）：**codex**（app-server）/ **Claude Code**（CLI stream-json 子进程）/ **pi**（Node sidecar 嵌入接入）+ deterministic 测试适配器；统一 `run()/resume()/interrupt()` + 事件流接口 | P0 | AC: 三个适配器全部通过同一套契约测试（金标 fixture 语义等价）；中断/异常路径 token 统计不丢失 |
| FR-10-2 | Harness 扩展位（按需启用）：opencode / local_llm 等——实现同一适配器规范即可插拔；厂商接入在各 harness 内部解决（§3.5），不设直连兜底 | P2 | AC: 通过同一套契约测试 |
| FR-10-3 | Harness 能力矩阵显式声明（live-steer/commentary/thinking/并行工具/会话恢复/原生 shell），Manager 调度可感知并降级 | P1 | AC: 能力缺失时降级路径有测试（如 commentary 缺失→走 text） |
| FR-10-4 | 模型厂商三级配置（Provider→Endpoint→Model），**与 harness 底座解耦——harness 是执行框架，模型厂商是模型来源，两个概念独立配置**；预设 **MiniMax、DeepSeek、Kimi、OpenCode、智谱（GLM）** 等厂商模板，可自定义扩展 | P0 | AC: 新厂商接入仅需配置，不改代码；同一 harness 可切换不同厂商模型 |
| FR-10-5 | 凭据管理：加密存储、优先级链注入（context > 厂商环境变量 > 通用回退）、禁止落日志 | P0 | AC: 日志/审计中无明文凭据（redaction 测试） |
| FR-10-6 | Provider Policy v2：黑名单 + 白名单 + 配额（日/周/run 级 token 与美元预算）+ 超限告警 | P1 | AC: 超限自动熔断对应 provider |
| FR-10-7 | 【预留，本期不做】智能模型路由：按 role/任务难度/成本自动选模型，输出 ModelResolution + model_decision 审计事件 | P3 | AC: 接口与审计事件位已预留（FR-06 的 role 字段为路由依据） |

### FR-11 Session 与上下文管理

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-11-1 | 三层 Session：①ChatSession（Manager 对话）②节点 Session（磁盘 session.json + transcript.jsonl，可中断恢复）③harness 原生 session（透传） | P0 | AC: 中断后重放恢复有测试 |
| FR-11-2 | 对话历史窗口管理：滑动窗口 + 超限压缩策略（summarize/compact/truncate 可配） | P1 | AC: 压缩前后关键信息保留（评估集验证） |
| FR-11-3 | 上下文预算：turn 级 token 软/硬上限，触顶触发压缩而非爆错 | P1 | AC: 预算事件落审计 |
| FR-11-4 | 节点 checkpoint：可便携 checkpoint（目标/已确认结论/未决项/产物引用/技能摘要），SHA-256 自检 | P1 | AC: checkpoint 可跨轮次恢复 |
| FR-11-5 | Resume 计划：支持从 session 起点/指定工具调用后/checkpoint 重放 | P2 | AC: 重放与首次执行结果一致（确定性 fixture） |
| FR-11-6 | **Session 自动归档**：closed session 到期（默认 30 天可配）自动归档（压缩消息体 → archived 表/冷存储），主表不无限增长；归档可检索、可还原 | P1 | AC: 历史项目"SQLite 无界增长"缺口闭环；归档任务本身留审计 |
| FR-11-7 | **Checkpoint 版本化清理**：每个 actor 按 run 保留最近 N 个 checkpoint 版本（N 可配），旧版本自动清理 | P1 | AC: checkpoint 表有界增长；清理不影响进行中 run |

### FR-12 记忆系统（分层 + 总结 + 治理，决策见 §3.4）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-12-1 | 四层记忆：L0 工作缓存（turn 级，run 结束清理）→ L1 会话上下文（session 级）→ L2 持久化记忆（跨 session：事件 episodic + 事实 semantic）→ L3 用户画像/知识库 | P1 | AC: 各层读写边界清晰，L0 不泄漏到 L2 |
| FR-12-2 | **记忆总结**：Session/Run 结束自动摘要（小模型），写回 L2；摘要含来源引用 | P1 | AC: 摘要可溯源到原始会话；注入新会话时显著降低 token 占用 |
| FR-12-3 | 检索注入：Manager 新 turn 按需检索 L2/L3 top-k 注入上下文，检索行为落审计 | P1 | AC: 注入内容在 transcript 中可见 |
| FR-12-4 | 记忆治理 UI：查看/编辑/删除/导出记忆条目与画像字段 | P1 | AC: 删除即生效（含派生摘要失效） |
| FR-12-5 | Subagent 记忆隔离：Subagent 默认仅获得 Envelope 显式注入的最小上下文切片 | P1 | AC: 节点无法直接访问记忆库（除非 KB ACL 显式授予） |
| FR-12-6 | 记忆存储抽象：MemoryStore 接口（首版 SQLite + FTS，预留向量库） | P1 | AC: 替换存储实现不改调用方 |

### FR-13 用户画像与个性化推送

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-13-1 | 用户画像实体：身份、角色、偏好、订阅项（如每日早报/内容风格/推送时间），显式可编辑 | P1 | AC: 画像字段变更全部留版本与审计 |
| FR-13-2 | 画像注入 scope：字段级控制"哪些画像信息可被 Manager/哪类节点使用" | P1 | AC: 越 scope 使用被拦截 |
| FR-13-3 | 个性化场景：每日早报、6 步管线视频、文章发布等内容生成/推送类任务，产出按画像定制 | P1 | AC: 场景 S2/S3/S4 端到端走通 |
| FR-13-4 | 定时/触发推送：cron 风格定时 DAG + 事件触发，结果推送到面板（ambient/result surface）并可语音播报 | P2 | AC: 推送免打扰时段可配 |

### FR-14 可观测性与审计

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-14-1 | 统一 trace_id（W3C Trace Context）：贯穿 Manager/Worker/Plugin/模型调用 | P0 | AC: 任一产物可经 trace_id 反查全链路事件 |
| FR-14-2 | 全事件落盘：transcript / tool events / handoff / 决策 / 置信度评估 / 人工干预 | P0 | AC: 事件含 actor、时间、输入摘要、输出摘要 |
| FR-14-3 | Supervisor 聚合视图：跨所有 actor 的 DAG 运行状态聚合，供 Manager 与 UI 消费 | P1 | AC: Manager 可在不轮询 Worker 的情况下获取全局视图 |
| FR-14-4 | **复盘中心**：按时间/状态/流程/Agent 检索历史 run，时间轴回放，自然语言搜索 | P1 | AC: 场景 S6 及"上周那个任务结果如何"类查询可走通 |
| FR-14-5 | 产物（Artifact）管理：所有产物登记入库（类型/来源/版本/存储引用），可预览/下载/审计 | P1 | AC: 产物与节点、置信度、run 关联可查 |
| FR-14-6 | 结构化日志 + 指标（Prometheus）：run 计数、节点时长、token 用量、熔断状态 | P1 | AC: 指标可对接 Grafana |
| FR-14-7 | 审计导出：run 打包导出（transcript+handoff+checkpoint+产物清单）用于复盘/演练 | P2 | AC: 导出包可重新导入回放 |

### FR-15 知识库（Obsidian + VLM，后期实现）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-15-1 | Obsidian Vault 挂载：以只读为主挂载本地 Vault，增量索引 | P2 | AC: 索引不修改原 Vault 文件 |
| FR-15-2 | VLM 图文理解：对 Vault 中图片/图文混排笔记做视觉语言模型索引 | P2 | AC: 图片内容可被检索命中 |
| FR-15-3 | 检索工具：语义+全文混合检索，作为可被节点白名单授予的工具 | P2 | AC: 检索走 FR-04-3 的节点级 KB ACL |
| FR-15-4 | 知识回写（可选）：经审批后把 run 产物沉淀为 Vault 笔记 | P3 | AC: 回写需人工确认并落审计 |

### FR-16 语音交互

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-16-1 | ASR 语音输入 + TTS 语音播报 | P1 | AC: 中文优先；与面板输出协同（说要点、看详情） |
| FR-16-2 | 执行过程语音旁白（commentary，依赖 harness 能力） | P2 | AC: harness 不支持时优雅降级 |
| FR-16-3 | 语音指令干预："停一下""重做第三步"等口语指令映射到干预操作 | P2 | AC: 危险操作语音指令需二次确认 |

### FR-17 工作空间与产物治理（模型见 §3.6）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-17-1 | 三级工作空间：项目级信任区（跨 run 持久）/ Run 级共享区（节点交接主通道）/ 节点暂存区（临时，run 结束即清） | P0 | AC: 三级目录隔离；节点默认仅见本节点暂存区 + 被授权路径 |
| FR-17-2 | 项目级信任目录管理：用户显式添加/移除/确认信任（Trae/Codex 式），授权留审计；节点在项目区写权限按路径白名单收敛；git 提交类操作默认需审批 | P0 | AC: 未信任目录 agent 不可写；越权路径写入被 hook 拦截 |
| FR-17-3 | 成果物交接：节点间经 Run 共享区落盘 + handoff `artifact_refs` 引用交接；禁止直接读写他节点暂存区 | P0 | AC: handoff 引用的产物路径做存在性校验，缺失即契约失败 |
| FR-17-4 | 产物登记与归档：节点将成果文件显式登记到产物库（类型/来源/版本/溯源/保留级别），产物库独立于工作区保留 | P1 | AC: 产物可从工作区提升；产物与 run/节点/置信度关联可查 |
| FR-17-5 | 保留分级：ephemeral（随 run/暂存区清理）/ standard（按天数，成功与失败分开配置）/ pinned（钉住永不清） | P1 | AC: 三级策略在清理器行为可逐项验证 |
| FR-17-6 | 周期清理：janitor 定时扫描（默认 6h 可配），仅清理 terminal run；active / pinned / 未知孤儿目录一律跳过；符号链接不跟随、防逃逸 | P1 | AC: 与 retention 安全规则逐条对照通过 |
| FR-17-7 | 手动清理：清理 API 默认 dry-run 预览，UI 确认后执行；策略/清理/pin 操作走变更授权 + 审计 | P1 | AC: dry-run 输出与实删清单一致 |
| FR-17-8 | 保留策略参数化：平台配置项（启用开关、成功/失败天数、清理间隔），UI 可编辑、原子写入、立即生效 | P1 | AC: 配置变更留审计 |
| FR-17-9 | 存储用量看板：按级别/按 run 统计磁盘占用，超阈值告警 | P2 | AC: 看板数据与实际磁盘一致 |
| FR-17-10 | **声明式产物（Declared Artifact）**：节点/工作流可预声明产物——handoff 产物（经 JSON 契约校验）与 workspace 产物（run 终结时自动 tar.gz 打包回传归档）；支持 publish 策略（success/failure/always） | P1 | AC: run 终结（含失败/中止）自动触发收集；契约不符拒绝归档并告警 |
| FR-17-11 | 产物安全封顶与路径安全：schema 级上限（max_files / max_bytes / timeout）；产物路径仅允许相对 POSIX 路径（拒绝绝对路径/`..`/NUL） | P1 | AC: 超限/越径产物被拒绝；与历史安全规则对照通过 |
| FR-17-12 | **项目区 Git 集成**：项目级信任目录支持绑定 git 仓库（clone 到项目区）；coding agent 完成后自动 commit（可配为需审批）；默认分支保护（不直接污染 main/master） | P1 | AC: commit 作者/信息可溯源到 run 与 agent；分支策略可配 |
| FR-17-13 | 【可选】产物外部归档同步：产物库可扩展同步到外部存储（NAS/S3/OSS） | P3 | AC: 同步失败不影响本地产物可用性 |

### FR-18 移动端访问（手机连接 PC 端）

| # | 需求 | 优先级 | 验收要点 |
|---|---|---|---|
| FR-18-1 | 移动 Web 端：响应式布局适配手机，PWA 形态（可加主屏、离线壳），访问 PC 端 Manager 服务 | P1 | AC: 手机浏览器局域网访问可用；核心页面无横向滚动 |
| FR-18-2 | 移动端核心功能优先级：Agent 气泡监控、审批卡片操作、结果/产物查看、通知接收、语音输入（可选） | P1 | AC: 审批/干预操作移动端可用，同样走签名 + 审计 |
| FR-18-3 | 设备接入认证：手机首次接入需配对（扫码/token），设备列表可管理、可吊销 | P1 | AC: 未配对设备无法建立会话；吊销即时生效 |
| FR-18-4 | 网络边界：默认仅局域网监听；远程访问为显式可配项（隧道/中继），默认关闭并附醒目安全提示 | P1 | AC: 默认配置不暴露公网 |
| FR-18-5 | 移动端精简上下文：device=phone + viewport=compact 时 UI 协议自动降级密度与容量 | P2 | AC: 与 FR-07-8 设备自适应规则一致 |

---

## 6. 非功能需求

### 6.1 安全（继承历史基线并强化）

| # | 需求 | 优先级 |
|---|---|---|
| NFR-S1 | Manager→Worker 通信 Ed25519 签名 + 防重放缓存；Worker→Manager 敏感操作同等签名 | P0 |
| NFR-S2 | Transport Fence：handoff 回传校验 round/actor/generation/command，防重放防过期 | P0 |
| NFR-S3 | 凭据隔离：Subagent 不接触 Manager 敏感配置；broker 引用模式优先 | P0 |
| NFR-S4 | Skill digest 钉定 + 白名单严格匹配，防 Worker 侧替换 | P0 |
| NFR-S5 | UI 协议安全：组件/纯函数白名单、大小深度硬限、URI 白名单、禁 openUrl/regex 类危险函数 | P1 |
| NFR-S6 | 凭据/敏感值 redaction：日志、审计、UI 渲染三层脱敏 | P0 |
| NFR-S7 | 人工审批令牌与变更令牌分离；敏感操作二次确认挑战 | P1 |
| NFR-S8 | 单租户假设显式化；多用户/多租户为显式排除项（远期再议），但数据结构不留死结 | — |
| NFR-S9 | 部署信任分离（见 §4.4）：Manager/Secrets 不容器化；harness 受管子进程化；启用容器隔离时控制/数据平面分离 + 独立 token | P0 |
| NFR-S10 | Worker 容器安全姿态：digest-pinned 镜像、readOnlyRootfs、非 root、网络隔离、env 白名单（禁 `TOKEN/SECRET/*_KEY` 等前缀） | P0 |
| NFR-S11 | Plugin Runtime 沙箱：启动 attestation 全量测量 + 运行期漂移检测，漂移即拒绝服务 | P1 |

### 6.2 可靠性与韧性

| # | 需求 | 优先级 |
|---|---|---|
| NFR-R1 | Circuit Breaker：per (provider, model) / per harness 熔断，5xx 比例与延迟阈值可配 | P0 |
| NFR-R2 | Worker 心跳 + 失联分级处置（优雅中断→强制中断→标记失败→按重试策略） | P1 |
| NFR-R3 | 派发离线退避重试（指数退避），目标变化重置 | P1 |
| NFR-R4 | 毒消息隔离：连续失败节点进入隔离区，触发诊断流程而非反复烧 token | P2 |
| NFR-R5 | Manager DB 每日快照 + 7 天轮转 + 一键备份/恢复 | P1 |

### 6.3 性能

| # | 需求 | 优先级 |
|---|---|---|
| NFR-P1 | UI patch 合并窗口（如 75ms coalesce）防渲染抖动 | P1 |
| NFR-P2 | HTTP/WS 入口早期 body-size 拒绝（Content-Length 阶段 O(1) 决策） | P1 |
| NFR-P3 | 模型调用 keep-alive 连接池 | P2 |
| NFR-P4 | 大 transcript 分卷存储，冷热分层 | P2 |

### 6.4 工程化

| # | 需求 | 优先级 |
|---|---|---|
| NFR-E1 | 协议包为单一真相源；跨包变更先改协议 | P0 |
| NFR-E2 | 统一错误码体系：`HR_E_*` + userMessage + remediation 三元组 | P1 |
| NFR-E3 | 契约测试：harness 适配器、handoff 契约、A2UI 协议、DAG 引擎语义迁移对照 | P0 |
| NFR-E4 | deterministic 后端为一等测试公民（单测/金标对比/离线录播） | P1 |
| NFR-E5 | 术语表（glossary）随协议维护，杜绝多义词（session/agent/checkpoint 等历史教训） | P0 |
| NFR-E6 | CI：协议 schema 校验、契约测试、A2UI catalog 漂移检查 | P1 |

### 6.5 产品化

| # | 需求 | 优先级 |
|---|---|---|
| NFR-PR1 | i18n 框架：中文优先，英文一等支持（UI + 语音 + 生成内容） | P1 |
| NFR-PR2 | 主题：暗色/亮色 | P2 |
| NFR-PR3 | 本地优先部署：一键启动（docker-compose / 桌面安装包），数据全本地 | P0 |
| NFR-PR4 | 升级与迁移：DB schema 版本化迁移，可回滚 | P1 |
| NFR-PR5 | 免确认/确认模式可全局与按场景配置 | P1 |

---

## 7. 核心数据实体概览

| 实体 | 关键字段 | 说明 |
|---|---|---|
| `AgentCard` | id, role, capabilities[], skills[], tool_grants, kb_grants, description, version, status | Subagent 能力配置（FR-06） |
| `WorkflowTemplate` | id, version, nodes[], edges[], params_schema | DAG 模板（FR-03） |
| `Run` | id, template_ref, status, profile, started_at, outcome, trace_id | 一次 DAG 执行 |
| `NodeRun` | run_id, node_id, agent_ref, state, attempts, confidence_history[] | 节点执行实例 |
| `Handoff` | from_node, port, content, self_confidence, verified_confidence, fence_meta | 节点产出交接 |
| `ConfidenceRecord` | node_run_id, self_score, self_reason, verified_score, verifier, gate_decision | 置信度评估记录（FR-05） |
| `ChatSession` / `NodeSession` | messages, window_state, checkpoint_refs | 三层 session（FR-11） |
| `MemoryItem` | layer, kind(episodic/semantic), content, source_refs, created_at, ttl | 分层记忆（FR-12） |
| `UserProfile` | identity, preferences, subscriptions[], field_scopes | 用户画像（FR-13） |
| `CapabilityPackage` | type(skill/tool/workflow/action/renderer/kind), manifest, digest, trust_tier, permissions, version | 能力注册中心条目（FR-09） |
| `ProviderConfig` | provider, endpoint, model, credential_ref, policy_ref | 模型厂商配置（FR-10） |
| `Artifact` | id, type, source_node_run, workspace_ref, retention_class, uri, version, preview_ref | 产物登记与归档（FR-14-5、FR-17-4） |
| `AuditEvent` | trace_id, actor, action, target, input_digest, output_digest, ts | 审计事件（FR-14） |
| `SurfaceDocument` | scope, revision, nodes[], purpose | 生成式 UI 文档（FR-07） |
| `WorkspaceBinding` | scope(project/run/scratch), path, trust_state, policy_ref, run_id? | 三级工作空间绑定（FR-17） |
| `RetentionPolicy` | enabled, success_days, failure_days, interval_ms, pinned_refs[] | 保留策略配置（FR-17） |
| `PairedDevice` | device_id, name, paired_at, token_ref, last_seen, revoked | 移动端配对设备（FR-18-3） |

---

## 8. 分期规划与里程碑

> 原则：**先能看清（观测/审计/安全底座）→ 再能干活（调度/DAG/置信度）→ 再好用（UI/记忆/画像）→ 再聪明（路由/知识库）**。

### M0 · 奠基（第 1~3 周）

- 协议包骨架 + 术语表（NFR-E1/E5）
- 统一 trace_id + 结构化日志 + 审计存储（FR-14-1/2，NFR-S6）
- Harness 基座首个适配：codex（app-server）+ deterministic + 统一事件流契约测试（FR-10-1）
- codex 接入验证 + 厂商兼容矩阵验证（直连兜底策略）（FR-10-1/4）
- 凭据加密存储 + Provider 三级配置 + 预设厂商模板（MiniMax/DeepSeek/Kimi/OpenCode/智谱）（FR-10-4/5）
- Circuit Breaker v1（NFR-R1）

**出口标准**：一个确定性 DAG（deterministic 后端）可端到端跑通且全程可审计。

### M1 · MVP（第 4~8 周）

- Manager Agent：意图识别/分解/调度规划 v1（FR-01-1~4）
- 直接派遣 Subagent（同步）（FR-02-1/2）
- DAG 引擎 + 节点类型 + 状态机 + Handoff/Fence（FR-03-1~4）
- Agent 注册中心 + 胜任引用（FR-06-1/2）
- 节点能力控制：工具白名单/Workspace 策略/凭据投影（FR-04-1/2/4/5）
- 生成式 UI v1：四 Surface + 表格/指标/进度/图片/DAG 视图 + fallback（FR-07-1~3/5/6）
- Agent 管理视图 v1：气泡 + 卡片（状态/节点/进度/思考流）（FR-08-1~4）
- 三层 Session（FR-11-1）
- 三级工作空间 + 项目级信任目录 + 成果物交接（FR-17-1~3）
- Harness 适配：Claude Code 接入（CLI stream-json + session resume）（FR-10-1）

**出口标准**：场景 S1（研发协作 DAG）与 S5（临时派遣）端到端走通，可观测可审计。

### M2 · V1（第 9~16 周）

- 置信度与质量门禁全量（FR-05 全项）
- 手动重执行 + 人工审批节点（FR-05-6/7、FR-03-9）
- 记忆系统：四层 + 记忆总结 + 检索注入 + 治理 UI（FR-12）
- 用户画像 + 个性化场景（早报/视频管线/文章发布）（FR-13-1~3）
- 能力注册中心 + as-native 投影 + 管理 UI（FR-09-1~4/6）
- Harness 首批补齐：pi（Node sidecar）接入 + 能力矩阵（FR-10-1/3）
- Provider Policy v2（FR-10-6）
- 生成式 UI v2：媒体组件/Action/沙箱 HTML/幻灯片视图（FR-07-4/7）
- 语音 ASR/TTS（FR-16-1）
- 复盘中心 v1（FR-14-4）+ 产物库（FR-14-5）+ Prometheus（FR-14-6）
- DAG 模板库首批（**智能审批 9 步管线 DAG 化** / PR 审查 / 早报 / 视频管线 / 文章发布）（FR-03-5、FR-03-10）
- 产物归档与保留治理（FR-17-4~8）+ 声明式产物与 run 终结自动归档（FR-17-10/11）
- Session 自动归档 + checkpoint 版本化清理（FR-11-6/7）
- 项目区 Git 集成（clone / commit / 分支保护）（FR-17-12）
- 移动端 PWA + 设备配对 + 局域网访问（FR-18-1~4）
- 可靠性：心跳/退避/备份（NFR-R2/R3/R5）

**出口标准**：场景 S1~S8 全部走通；三大 harness（codex / Claude Code / pi）全场景通过并按节点热切换演示；早报每日自动产出并按画像定制；手机端可完成审批操作；工作区按保留策略自动清理。

### M3 · V2（第 17~26 周）

- 知识库：Obsidian 挂载 + VLM 索引 + 节点级 KB ACL（FR-15-1~3）
- 可视化 DAG 编辑器（FR-03-7）+ 录制回放（FR-03-8）
- 异步派遣/并发控制（FR-02-3/4）、advisor 咨询（FR-02-5）
- 胜任校验 + Manager "谁胜任"检索（FR-06-3/4）
- 上下文预算与压缩（FR-11-2/3）
- 定时/触发推送（FR-13-4）
- 语音旁白与语音干预（FR-16-2/3）
- 置信度统计反哺（FR-05-8）
- 移动端体验优化（FR-18-5）、存储用量看板（FR-17-9）
- i18n 英文一等（NFR-PR1）

### M4 · V3（远期，按需启动）

- 智能模型路由（FR-10-7，依赖 role/置信度/成本历史数据积累）
- 扩展 harness：opencode 等按需接入（FR-10-2）
- Skill 信任分级强化、Plugin 生态（签名链/市场）（FR-09-5+）
- 多设备终端、跨设备接力
- 知识回写（FR-15-4）、记忆图谱 UI
- Manager HA

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Harness 能力差异导致体验不一致（如 live-steer/commentary 缺失） | 中 | 能力矩阵显式声明 + 降级路径契约测试（FR-10-3） |
| 记忆/画像引入隐私与上下文膨胀 | 中 | 字段级 scope、检索审计、总结压缩、用户可删可导出（FR-12/13） |
| 置信度自评失真（模型自评普遍偏高） | 中 | 双分量设计（核验分量）、校正轮锁定工具、历史统计校准（FR-05） |
| 单文件/单模块巨型化（前代工程教训：单文件数千行） | 中 | M0 立规：模块行数红线 + 分层包结构（§4.3）+ 拆分式 code review（NFR-E 系列） |
| 多 harness 行为漂移 | 高 | 抽象层最小适配 + 统一事件流契约测试 + deterministic 金标对照 |
| codex 厂商兼容风险（model id 归一化等已知坑） | 中 | 兼容矩阵沉淀 + 遇一例修一例（§3.5、FR-10）；前代已实测 |
| pi 接入的已知 P0 问题（pi-server Unix socket 不支持 Windows、session 双持久化冲突、steer 语义差异） | 高 | 采用 sidecar 嵌入模式 + 平台 session 为唯一审计真相源；M2 前置 spike 验证（依据 `docs/report/pi-harness-open-issues-research-2026-07-29.md`） |
| 凭据泄漏（日志/产物/handoff） | 高 | 三层 redaction + containsCredentialValue 拦截 + broker 引用优先（NFR-S3/S6） |
| 范围蔓延（PPT 原生渲染、多租户、SaaS 化等诱惑） | 中 | §3.3 边界 + 遗留开放问题清单管理；多租户/SaaS 显式排除 |
| 前代工程重写回归风险 | 中 | 以 PRD 场景 S1~S8 为验收锚点；全部重新开始、不迁移前代资产，需求以 PRD 为唯一真相源 |
| 移动端接入引入内网暴露面 | 中 | 默认仅 LAN 监听 + 配对认证 + 吊销机制；远程访问默认关闭（FR-18-3/4） |
| 工作区清理误删用户数据 | 高 | dry-run 默认 + 分级保留 + pin 机制 + 符号链接防护 + 孤儿目录不扫描（FR-17-6/7） |
| 智能审批双轨期过长（新旧系统并存成本） | 中 | 按业务逐个迁移（先一个审核 Agent 试点），保留旧系统只读回放；迁移节奏见 Q10 |

---

## 10. 遗留开放问题

| # | 问题 | 建议决策时点 |
|---|---|---|
| Q1 | 智能模型路由的具体策略（规则表 → 启发式 → LLM 辅助） | M3 启动前，届时已有 role/置信度/成本数据 |
| Q2 | i18n 第二语言确认（英文优先？） | M2 中 |
| Q3 | 脚本节点（Script Node）的脚本语言与沙箱运行时选型（Python / Node / 受限 shell） | M1 内确定，影响 FR-03-10 实现 |
| Q4 | 知识库 VLM 选型（本地小模型 vs 网关多模态模型） | M3 启动前 |
| Q5 | 多用户/多租户是否进入 roadmap（当前显式排除） | 商业化讨论时 |
| Q6 | 记忆存储是否升级向量库（首版 SQLite FTS 是否够用） | M3 按检索质量评估 |
| Q7 | PPT 导出物格式（PDF 优先 or HTML 幻灯片优先） | M2 做幻灯片视图时 |
| Q8 | 移动端远期形态：PWA 是否够用，还是需原生 App（推送通知能力是分水岭） | M3 末评估 |
| Q9 | 远程访问方案：仅局域网 vs 自建隧道 vs 中继服务 | 有远程需求时 |
| Q10 | 智能审批业务迁移节奏：AI_Agent_Platform 与 AgentOps 双轨并行多久、按审核 Agent 逐个迁移还是整体切换 | M2 模板就绪后 |
| Q11 | pi sidecar 的 session 冲突与 steer 语义方案（P0 已知问题）是否经 spike 验证通过 | M2 接入前 1 周完成 spike |
| Q12 | 产物外部归档存储（NAS/S3/OSS）是否需要、何时需要（FR-17-13） | 磁盘压力或异地容灾需求出现时 |
| Q13 | 多机分布式部署（远程 Node/Worker，§4.4 模式③）是否进入 roadmap | 单机性能瓶颈或团队化时 |

---

## 11. 附录

### 附录 A · 需求与方案依据索引

| 资料 | 被本 PRD 继承/引用的内容 |
|---|---|
| 前代工程 `E:\Project\AgentOps`（AGENTS.md / CLAUDE.md / TODO.md / docs/） | 已验证需求场景（DAG 四模式、Widget 体系、跨域协调、凭证库、知识库工具链）；工程教训清单（§1.1 来源一） |
| `E:\Project\AI_Agent_Platform`（含 `docs/oa-audit-config-generator/presentation.html`） | 9 步审核管线、程序化检查器、三级决策、声明式审核项、激活条件表达式 |
| `github.com/LetheChen/openclaw-longmen-inn`（设计文档：`E:\Document\06-平台设计\longmen-inn设计文档\docs\index.html`） | 角色分工与禁止事项、LEDGER 任务看板、deliverables 凭据归档、记忆流水账 |
| `workspace-session-artifact-management.md`（本目录） | 工作区/Session/产物管理全景：统一 sessions 表、声明式产物体系、retention 安全模型、Trae/Codex 对比与缺口清单 |
| `agent-deployment-architecture.md`（本目录） | 部署信任分离模型、容器安全约束、attestation 与漂移检测方案 |
| `report/pi-harness-adapter-poc-2026-07-29.md`（docs/report/） | pi 架构对齐分析、Event 映射、sidecar 嵌入模式依据（§3.5、架构 §4.3） |
| `report/pi-harness-open-issues-research-2026-07-29.md`（docs/report/） | pi 接入 P0/P1 未决问题（Windows socket、session 双持久化、steer 语义）与决策依据 |

### 附录 B · 端到端示例：研发项目协作（场景 S1）

```text
用户(语音): "帮我把 XX 功能做了，老规矩走研发流程"
  ↓
Manager Agent: 意图识别(task_class=feature_dev) → 检索画像("老规矩"= 预置研发 DAG 模板)
  → 检索记忆(上周类似任务的经验) → 任务分解确认卡片(可免确认)
  ↓
实例化 DAG: design → coding → review → audit_commit
  ├─ design 节点: 引用 designer Agent(工具:搜索/编辑/写入; KB:设计规范库)
  │     → 产出设计文档 + 自评置信度 0.9 → 门禁(≥0.7)放行
  ├─ coding 节点: 引用 coder Agent(工具:读/写/Bash; Workspace:可写 src/)
  │     → 产出代码 + 自评 0.62 → 门禁(≥0.7)不通过 → 校正轮重试(锁定 handoff)
  │     → 第二次 0.81 → 放行
  ├─ review 节点: 引用 reviewer Agent(只读工具 + diff)
  │     → 核验置信度 0.85 + 问题清单 → 放行
  └─ audit_commit 节点: 引用 auditor Agent(工具:git只读/提交需审批)
        → 触发 WAITING_FOR_APPROVAL → 面板审批卡片 → 用户语音"提交吧"
        → git 提交完成 → run COMPLETED
  ↓
全程: 气泡视图实时显示 4 个 Agent 状态; 产物(设计文档/代码diff/审查报告)入产物库;
      记忆总结写回("XX 功能用了 ZZ 方案"); 早报可引用本次进展
```

### 附录 B-2 · 端到端示例：智能审批 9 步管线 DAG 化（场景 S7）

```text
OA 系统回调 / 定时轮询触发: "差旅报销单 #TR-20260729-018 待审"
  ↓
实例化 DAG「智能审批 9 步管线」(脚本节点与 agent 节点混合编排):
  ├─ fetch_data      [script] 拉取 OA 表单+商旅数据 → 落 Run 共享区
  ├─ activate_items  [script] activate_when 表达式求值 → 激活 6 个审核项
  ├─ dispatch_tools  [script] 收集/去重/下载附件 → 登记 artifact_refs
  ├─ vlm_recognize   [agent]  VLM 识别附件(发票/行程单) → 自评置信度 0.88 → 放行
  ├─ assemble_prompt [script] 拼装批量审核提示词(确定性,零模型)
  ├─ batch_audit     [agent]  单次 LLM 批量审核 → 自评 0.74; 其中"住宿水单"项 0.52
  │     → 门禁(≥0.7): 整单放行,但低置信审核项标记 → 触发该项校正轮重审 → 0.79 → 放行
  ├─ program_checks  [script] 程序化检查器(金额上限/日期区间/发票查重)
  │     → 命中一条硬性 reject(原则 P11: 脚本结论优先于模型结论)
  ├─ aggregate       [script] 三级决策聚合(reject>manual>pass) → manual
  └─ decide_report   [agent]  生成审核报告 → 登记产物库(standard 保留)
        → WAITING_FOR_APPROVAL: 用户手机端收到审批卡片,查看 reject 依据,点"同意驳回"
        → 回调 OA 审批节点 → run COMPLETED
  ↓
全程: 每步脚本/agent 节点输出均带置信度与审计; 附件/报告归档产物库;
      Run 共享区 7 天后自动清理(失败保留 14 天), 产物库独立长期保留;
      出差途中用户用手机完成了审批(FR-18)
```

---

> **文档维护说明**：本文档为需求基线（当前 v1.4）。后续设计文档（架构设计、协议设计、UI 设计）应逐条回引本 PRD 的需求编号（FR-xx / NFR-xx），需求变更需更新本文档并升版本号。
