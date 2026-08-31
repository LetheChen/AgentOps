# AGENTS.md — AgentOps

最后更新：2026-08-31（按 init-claude-md 重建）

## Session Maintenance Protocol

**开始时**：读 TODO.md，复述当前 🔴 和 🟡 段，确认本次会话要推进哪几项。

**结束前**（最后一条用户消息之后、停止前必做）：

1. **回看本次会话实际做了什么** —— 改动的文件、发现的问题、用户新增的约束
2. **更新 TODO.md**（决策表见 `.claude/rules/session-maintenance-protocol.md`）：
   - 完成了某项 → 移到 ✅ 段，加 (YYYY-MM-DD)
   - 新发现的问题 → 加到 🟡（P1/P2）或 🟢（P3+），附源链接
   - 放弃某项 → 移到 ✅ 段，备注 已放弃：<原因>
3. **更新本文件的规则** —— 如果本次会话暴露了新的项目级约定：
   - 用户反复纠正的行为 → 加到 Things to avoid 段（CLAUDE.md）
   - 反复跑错的命令 → 更新 Verification 段
   - 隐含约定 → 加到 Project conventions 段
4. **刷新日期戳** → 最后更新：YYYY-MM-DD（即使其他都没变）

**触发条件**：≥1 个源文件被改 · 用户新增了约束 · 会话 > 10 分钟

## Project

多 Agent DAG 编排平台：Python 后端（FastAPI/uvicorn）+ React/TS 前端（Vite），支持 Claude Code / OpenCode / Kimi / 本地 LLM 等 harness，配套 A2UI 生成式 UI 与可视化 DAG 编辑器。

## Language

回复语言：中文 · 注释语言：中文 · 代码标识符：英文 · commit 信息：中文

## Rules Index

完整规则集见 CLAUDE.md（含 Things to avoid、docs/ 目录约定等）；本文件作精简入口。

- 会话维护协议：`.claude/rules/session-maintenance-protocol.md`
- 后端 / harness / workflow / 前端 / 知识库 子规则：`.claude/rules/{workflow,harness,frontend,knowledge}.md`
- 速查：`docs/INDEX.md`（设计文档总索引）

## Karpathy 4 准则

1. **Think Before Coding** —— 改前先读；多文件改动先 grep 全局引用
2. **Simplicity First** —— 不过度抽象；3 行重复好过 1 个过早抽象
3. **Surgical Changes** —— 只动必要的；不要顺手重构无关代码
4. **Goal-Driven Execution** —— 每步都问"这离用户的目标更近了吗"

## Verification

```bash
# 一键启动（启动 backend + frontend + opencode，logs 写到 logs/）
.\start.ps1

# 验证后端
curl http://127.0.0.1:1987/
# 验证前端
curl http://127.0.0.1:5173/

# 停止
.\stop.ps1
```

## Things to avoid

详细版见 CLAUDE.md「Things to avoid」段。摘要：

- PowerShell 中文字符串陷阱：用 `Write` 工具创建 `.ps1` 时含中文 + `[regex]::Replace` 会乱码 → 优先 `Read` 定位 + `SearchReplace` 逐处修复
- 目录 rename 后必查引用：波及 CLAUDE.md / TODO.md / skills/*.md / README.md 等非 docs/ 文件
- grep 旧名时要避免被新前缀误命中：用 `\b` 或负向 lookbehind
- 临时 .ps1 / .py 脚本要即用即删：跑完立即 `Remove-Item`，不要在 docs/ / scripts/ 下残留

## Project conventions

- 设计文档按侧栏菜单项归档：`docs/00-platform/`（跨菜单）+ `docs/NN-<menu-name>/`（01-09 菜单）
- 文件名前缀：REQUIREMENTS-/DESIGN-/ANALYSIS-/REVIEW-/RESEARCH-/PLAN-/ARCHIVED-/DEPRECATED-（连字符分隔）
- 文档间互相引用时使用相对路径，rename 后必须同步更新所有引用方
- 前端验收：涉及前端关联能力的功能必须通过浏览器真实确认（Playwright）
- 涉及架构/流程图时**必须**用 Mermaid，禁止 ASCII / 纯文本拼凑