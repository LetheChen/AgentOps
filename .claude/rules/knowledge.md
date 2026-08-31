---
paths:
  - "config/knowledge/**"
  - "tools/lint_knowledge.py"
  - "tools/extract_content.py"
  - "tools/query_kb.py"
  - "tools/obsidian_vault.py"
  - "web/src/components/knowledge/**"
  - "web/src/lib/markdown.ts"
priority: 50
---

# 知识库 / Vault / 域 规则

本文件聚焦**知识管理子系统**（知识域、Vault 文件、Lint、问答、md 渲染）的修改约定。

## 架构

- **域（Domain）**：业务域，如 `weekly-report` / `proposal-planning` / `video-production` / `smart-query` / `content-curation`；每个域是一个独立目录 + AGENTS.md + index.md
- **分类**：`raw`（原始片段）/ `entities`（实体）/ `concepts`（概念）/ `comparisons`（对比）
- **Vault**：本地 Obsidian vault 路径，由 `tools/obsidian_vault.py` 提供浏览 + 预览接口
- **渲染策略**：md 视图统一走 `web/src/lib/markdown.ts` 的 `renderMarkdown`（已配置 `html:false` 安全内联）

## 改前必读

- `config/knowledge/domains.yaml` —— 域定义
- `config/knowledge/<domain>/AGENTS.md` —— 域内的 agent 约束
- `config/knowledge/shared_kb.yaml` —— 跨域共享知识
- `tools/lint_knowledge.py` —— 知识质量扫描（lint 工具）
- `tools/query_kb.py` + `config/tools/query_knowledge.yaml` —— 知识库查询
- `tools/extract_content.py` —— 文档抽取（article / pdf / docx / whisper）
- `web/src/components/knowledge/` —— 前端 6 个子组件（仪表盘 / 域详情 / Vault / 搜索 / 归档 / Lint / 问答）

## 改时约束

### 配置改动

- 新增域：`config/knowledge/domains.yaml` 加条目 + 新建 `<domain>/` 子目录（含 AGENTS.md / index.md）
- 改域结构：先改 yaml 再改 `_catalog` 等缓存；CLI `python cli.py kb rebuild` 重建
- vault 路径改：用绝对路径写 `.env` 的 `LOG_PATROL_DIR` 风格变量（不要硬编码）

### 代码改动

- 新增知识工具：写 `tools/<name>.py` handler + `config/tools/<name>.yaml` schema + 在 `tools/__init__.py` 注册
- 改 lint 规则：`tools/lint_knowledge.py` 的规则函数保持纯函数，方便测试
- 改前端 md 视图：**统一复用** `web/src/components/knowledge/` 已有的"渲染/源文本"切换模式（如 `VaultFilePreview.tsx` / `DomainDetail.tsx`），不要重新发明

### markdown 渲染相关

- md 视图默认渲染（markdown-it），通过工具栏可切回源文本
- frontmatter（如有）：保持折叠区独立显示，不要混入正文
- 渲染容器统一用 `.md-content` 全局样式 + `.kh-preview-md-rendered` / `.kh-domain-md-rendered` 容器样式

## 改后必验

- 后端测试：`pytest tests/test_kb_config.py tests/test_lint_knowledge.py -x`
- 前端测试：浏览器实跑"知识管理 → Vault 浏览 → 选 .md → 看渲染 + 切源文本"
- Lint 自身：`python cli.py kb lint --domain <domain>`

## 易踩坑

- `tools/extract_content.py` 的可选依赖（trafilatura / pdfplumber / python-docx / whisper）按需装 → 缺依赖时返回 `missing_dependency`，不要让它 throw
- vault 是只读视图，不要把"vault 浏览"当成"vault 写入"路径
- Lint 规则不能误报：markdown frontmatter 的 `type:` 行不要被当成无序列表触发误报