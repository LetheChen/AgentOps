---
paths:
  - "web/src/**"
  - "web/package.json"
  - "web/vite.config.ts"
priority: 50
---

# Frontend / React 规则

本文件聚焦**前端开发约定**。涉及前端改动前请通读。

## 技术栈

- React 18 + TypeScript 5 + Vite 5
- `markdown-it` 已配 `html:false / linkify:false / breaks:true`（防 XSS）→ 共享渲染走 `web/src/lib/markdown.ts`
- `dompurify` 已装但默认不挂；如需富文本输入清洗再启用
- `reactflow` 11 用于 DAG 编辑/可视化
- 端口：`5173`（dev），vite proxy `/api → 127.0.0.1:1987`（同源才能让 EventSource 带 cookie）

## 改前必读

- `web/src/lib/api.ts` —— 所有后端调用的统一入口；禁止直接 `fetch('http://127.0.0.1:1987/...')`
- `web/src/lib/authFetch.ts` —— 全局 fetch 包装；401 派发 `agentops:unauthorized` 事件，触发 AuthContext 退出登录
- `web/src/lib/markdown.ts` —— markdown 渲染共享（已有 `renderMarkdown` / `renderMarkdownWithMentions` / `splitThinkBlocks`）
- `web/src/lib/securityApi.ts` —— 登录/token 存 localStorage（key=`agentops_token`）
- `web/src/styles.css` —— 全部样式 token 定义（颜色 / 间距 / 字号），**新增样式必须用 token，不写死值**
- 全局排版容器 `.md-content`（markdown 渲染配套样式）—— md 视图直接套即可

## 改时约束

- **Markdown 渲染走 `renderMarkdown`，不要自己 new MarkdownIt()**（避免 XSS 风险）
- 涉及后端调用的页面：用 `apiClient` / `authFetch`，不要绕过
- 涉及安全敏感页（用户/角色/凭证）：先看 `web/src/components/AuthGate.tsx` 的守卫范围
- 样式：用 `var(--color-*)` / `var(--radius-*)` / `var(--font-*)` token，不用 `#60A5FA` 这种字面量
- 涉及 markdown 视图（`VaultFilePreview` / `DomainDetail` / 后续新组件）：优先复用 `lib/markdown.ts` 的 `renderMarkdown` + `.md-content` 全局样式

## 改后必验（强制）

**涉及前端关联能力的功能验收交付，必须通过浏览器真实确认。**

- 起后端 + 前端：`.\start.ps1`
- 浏览器自动化：`scripts/` 下写 Playwright 脚本（参考 `scripts/_verify_md_render.py`）
- 登录态注入：API `POST /api/auth/login` 拿 token → `ctx.add_init_script` 写到 `localStorage.agentops_token`
- 截图存 `logs/_<scenario>/`，最后清理临时脚本（详见 CLAUDE.md Things to avoid）

## 易踩坑

- Vite HMR 缓存：改 `.tsx` 后浏览器仍加载旧版本 → `page.reload(wait_until="networkidle")` 强刷
- PowerShell 控制台默认 GBK → 含中文/emoji 的 `print` 会抛 `UnicodeEncodeError` → 脚本开头 `sys.stdout.reconfigure(encoding="utf-8")`
- `get_by_role("button", name="知识管理")` 因侧栏按钮含 SVG 图标可能 strict 匹配失败 → 退化为 `get_by_text("知识管理", exact=False)`
- React `<pre>` 直接显示 markdown 源 → 永远是 `<pre>` 视觉；想真正渲染必须 `dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}`
- 测 markdown 渲染后想验证 DOM 结构：`document.querySelector('.md-content').querySelectorAll('h1').length` 比看 innerText 更准