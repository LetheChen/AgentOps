---
name: tavily-search
description: 联网研究 skill——把 Tavily Search API 包装为 AgentOps 平台级工具，供任意需要「实时联网调研」的工作流复用（行业研究、竞品分析、新闻监控、技术调研、事实核查等）。
version: 1.0
domain: _shared
depends_on: []
triggers:
  - 联网搜索
  - 实时调研
  - 行业研究
  - 竞品分析
  - 新闻监控
  - 技术调研
  - 事实核查
  - web search
  - tavily
---

# Tavily Search Skill

> 本 skill 把 Tavily Search API 包装成 AgentOps 的 `tavily_search` 工具，供任意需要「实时联网调研」的工作流节点复用。
> 设计动机见 [docs/reconstruction/DAG Workflow 规范与实现与skills业务关系.md §7.1](file:///e:/Project/AgentOps/docs/reconstruction/DAG%20Workflow%20%E8%A7%84%E8%8C%83%E4%B8%8E%E5%AE%9E%E7%8E%B0%E4%B8%8Eskills%E4%B8%9A%E5%8A%A1%E5%85%B3%E7%B3%BB.md) —— 把通用工具调用从单个 workflow 抽出为平台级 skill。

---

## 一、何时使用本 skill

满足以下任一条件时，在节点 role_prompt 中调用 `tavily_search` 工具：

1. **实时性要求**：知识截止日之后的事实（如「2026 年最新发布的 LLM」「昨天的新闻」）
2. **公网信息检索**：行业研究、竞品分析、市场数据、政策法规、技术文档
3. **新闻/事件追踪**：topic="news" + days=N 限定窗口
4. **事实核查**：LLM 内部知识不确定时，回到源头核对

**不要使用本 skill 的场景**：

- ❌ 用户问的是项目内部知识（用 `query_knowledge` / `obsidian_vault`）
- ❌ 私域文档/代码（用 `obsidian_vault` / `read_file`）
- ❌ 单纯查询 SQL/数据库（用 `sql_query`）
- ❌ 单步问答且无实时性要求（直接对话回答）

---

## 二、工具签名

调用工具：`tavily_search`

### 输入参数

| 参数 | 必填 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | ✅ | string | — | 搜索关键词，中英文皆可。**支持自然语言问句**（如"2026 年 Anthropic 发布的最新模型有哪些"） |
| `max_results` | ❌ | integer | 5 | 返回结果数（1-20），按相关度排序 |
| `search_depth` | ❌ | enum | `"basic"` | `"basic"`（快，标题+摘要）/`"advanced"`（慢，含正文,token 计费翻倍） |
| `topic` | ❌ | enum | `"general"` | `"general"`（通用网页）/`"news"`（新闻源，含 days 窗口） |
| `days` | ❌ | integer | — | 仅 `topic="news"` 生效，限定最近 N 天 |
| `include_answer` | ❌ | boolean | `false` | `true` 时 Tavily 直接给一句话汇总答案（节约下游 LLM 整理成本） |
| `include_domains` | ❌ | array | — | 白名单域名（精确匹配，如 `["anthropic.com"]`） |
| `exclude_domains` | ❌ | array | — | 黑名单域名（如 `["reddit.com"]` 排除社区噪音） |

### 输出结构

```json
{
  "ok": true,
  "query": "原始 query",
  "answer": "Tavily 直接生成的汇总（include_answer=true 时才有）",
  "results": [
    {
      "title": "页面标题",
      "url": "https://...",
      "content": "页面摘要（~300-500 字）",
      "score": 0.95
    }
  ],
  "result_count": 5
}
```

失败时：

```json
{
  "ok": false,
  "error": "missing_api_key | network_error | http_error | rate_limited | parse_error",
  "detail": "人可读的错误描述"
}
```

---

## 三、调用纪律（role_prompt 必须写明）

### 3.1 单次调用足够

绝大多数研究问题 1 次调用即可拿到答案。**禁止**对同一 query 重复调用。

例外：第一次返回 `ok=false` 时，可换不同 query 重试 1 次。

### 3.2 advanced 模式慎用

`search_depth="advanced"` token 计费翻倍（同样 max_results 下抓正文）。仅当：

- `include_answer=true` 且 basic 答案不够用
- 下游需要引用正文做引述（如写研究报告）

否则默认 `basic`。

### 3.3 下游消费契约

调用 `tavily_search` 后，节点必须将搜索结果**显式交付**到 output port（参考 `skills/_shared` SKILL.md §一端口表约定）。常见模式：

- 节点 outputs 声明 `research_findings` port → 把 results 列表交给下游写作/汇报节点
- 节点 outputs 声明 `answer` port → 仅传 `answer` 字符串（如 Tavily 已生成）

### 3.4 错误处理

返回 `ok=false` 时**禁止以纯文本结束回合**，必须：

1. 在 role_prompt 写明「如果搜索失败，尝试改写 query 重试 1 次」
2. 重试仍失败 → 在 handoff content 中标注 `search_failed: true` + `error` 字段，让下游知情
3. **禁止 LLM 编造**搜索结果

---

## 四、API Key 配置

工具读取环境变量 `TAVILY_API_KEY`。**没有 key 时返回 `missing_api_key` 错误，不会崩溃**。

### 配置方式（任选其一）

#### 方式 A：项目根目录 `.env`（推荐）

```bash
# E:\Project\AgentOps\.env
TAVILY_API_KEY=tvly-xxxxxxxxxxxx
```

启动 backend 时 `python-dotenv`（已通过其他 .env 路径间接支持）会自动加载。如果未生效，手动 `export TAVILY_API_KEY=...` 后启动。

#### 方式 B：CredentialStore（暂不推荐）

Tavily 不是 LLM provider，`CredentialStore` 主要管理 OpenAI/DeepSeek/MiniMax 这类 provider key。如果你坚持用 CredentialStore 存 tavily key，需要扩展 CredentialStore 的 provider 注册（不在本 skill 范围内）。

#### 方式 C：环境变量

```bash
# Linux/macOS
export TAVILY_API_KEY=tvly-xxx

# Windows PowerShell
$env:TAVILY_API_KEY = "tvly-xxx"

# Windows cmd
set TAVILY_API_KEY=tvly-xxx
```

### 申请 Key

访问 https://tavily.com 注册免费账号，免费档每月 1000 次搜索额度（足够内部研究 workflow 使用）。

---

## 五、最佳实践（10 条）

1. **query 用自然语言**："2026 年 Anthropic 发布的最新模型" 优于 "Anthropic 2026 model"
2. **max_results 默认 5**，只有需要深度调研才开到 10-20
3. **news topic 必须配 days**：`topic="news", days=7` 限定一周内，避免历史新闻噪音
4. **领域白名单**：调研竞品时 `include_domains: ["competitor.com", "competitor.com/blog"]` 提升精度
5. **避免社交媒体噪音**：`exclude_domains: ["reddit.com", "quora.com", "twitter.com"]`
6. **answer 字段用于总结**：如果下游只是写"研究背景"段，开 `include_answer=true` 省 token
7. **多语言友好**：query 支持中英混合，结果语言取决于源网页
8. **审计 log**：query 会被审计日志记录（`audit.log_args=true`），避免在 query 里塞用户隐私
9. **不要做"搜索引擎替代品"**：本 skill 是给 agent 研究用的，不是给前端用户做实时搜索 UI
10. **不要无限重试**：失败最多 2 次（原始 + 改写），仍失败就走 `search_failed` 兜底

---

## 六、完整示例：行业研究节点 role_prompt

```yaml
# workflows/industry-research.yaml 中的 research 节点
nodes:
  research:
    name: 联网研究
    type: agent
    agent: smart_query
    business_role: 研究员
    harness: codex
    inputs: [research_topic]
    role_prompt: |
      对 {{research_topic}} 做联网研究。

      调用 tavily_search 工具 1 次：
        query: "{{research_topic}} 2026 最新进展"
        max_results: 8
        search_depth: basic
        include_answer: true

      如果 ok=false，改写 query 重试 1 次（去掉年份或换关键词）。
      重试仍失败 → 在 handoff content 写 search_failed=true + error。

      完成后必须调用 handoff 工具恰好一次：
        port: research_findings
        content: {
          "summary": "一段话总结（含 Tavily answer 字段）",
          "sources": [
            {"title": "...", "url": "..."},
            ...最多 5 条最权威来源...
          ],
          "search_failed": false
        }
        summary: "X 条研究结果"

      禁止以纯文本结束回合，必须以 handoff 工具调用结束。
    outputs:
      research_findings:
        to: "synthesis.in:findings"
```

---

## 七、反模式（禁止）

1. **❌ 在 workflow 节点 yaml 里硬编码 query**：query 应该是 workflow input 或上游节点产物
2. **❌ 对同一 query 连调 3+ 次**：浪费 token + 触发 Tavily rate limit
3. **❌ 用 tavily_search 查私域内容**：数据库 / 项目代码 / 内部文档用 `sql_query` / `obsidian_vault`
4. **❌ 不传递 search 失败状态**：失败时 handoff 必须带 `search_failed: true`，下游必须知情
5. **❌ 在 audit 日志里塞完整 query**：query 会被记录，避免 PII（个人信息）

---

## 八、相关文档

- 工具实现：`tools/tavily_search.py`（urllib 调用 Tavily REST API）
- 工具注册：`config/tools/tavily_search.yaml`
- 设计规划：`docs/reconstruction/DAG Workflow 规范与实现与skills业务关系.md §7.1` L1 平台级 skill 段落
- Tavily API 官方文档：https://docs.tavily.com/docs/rest-api/api-reference
- 共享端口契约：`skills/_shared/SKILL.md §一`
- 交付协议（handoff 铁律）：`skills/workflow-author/SKILL.md §2.6`
