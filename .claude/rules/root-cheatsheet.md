---
paths: ["**/*"]
priority: 90
---

# Root Cheatsheet — AgentOps

根文件 `CLAUDE.md` 的速查入口；本文件展开 CLAUDE.md 简版段，避免根文件膨胀。

## 项目一句话

多 Agent DAG 编排平台：Python 后端（FastAPI/uvicorn）+ React/TS 前端（Vite），多 harness（Claude Code / OpenCode / Kimi / 本地 LLM），配套 A2UI 生成式 UI 与可视化 DAG 编辑器。

## Stack

| 层 | 技术 | 路径 |
|---|---|---|
| 后端 | Python 3.11+ / FastAPI / uvicorn | `api/server.py` 入口 |
| 前端 | React 18 / TypeScript / Vite | `web/src/main.tsx` 入口 |
| 状态存储 | SQLite (audit.db) | `audit/` |
| 配置 | YAML + Pydantic | `config/` |
| Harness 抽象 | `harness/{claude_code,opencode,kimi,local_llm,...}.py` | 统一通过 `harness/register.py` |
| 工具注册 | `config/tools/*.yaml` + `tools/*.py` | `tool_registry` 反射 |
| 工作流引擎 | `workflow/` | DAG 调度 |
| 安全认证 | `api/security/` | argon2 + session token + 角色 RBAC |

## 关键目录约定

- `api/` — FastAPI 后端（路由、依赖、安全、中间件）
- `audit/` — 事件存储 + 安全 schema
- `config/` — YAML 配置（agents / tools / domains / knowledge / schedules / permissions）
- `harness/` — 多 LLM harness 抽象层
- `orchestrator/` — Manager Agent + 跨领域协调 + DAG 调度
- `task/` — 任务生命周期、状态机、存储、执行
- `tools/` — 工具实现（每个 yaml 配置一个 .py handler）
- `skills/` — Agent 可调用的 SKILL.md 提示词
- `web/` — React 前端（Vite，TS）
- `docs/` — 设计文档（按侧栏菜单归档，详见 CLAUDE.md「docs/ 目录约定」段）
- `bench/` — 基准测试 runner + orchestrators
- `docker/agentops-worker/` — Worker 容器化

## 端到端运行

```bash
# 启动 backend + frontend + opencode（后台，无窗口）
.\start.ps1

# 验证
curl http://127.0.0.1:1987/      # 后端 200
curl http://127.0.0.1:5173/      # 前端 200

# 停止
.\stop.ps1

# 看实时日志
.\start.ps1 -Watch
```

## 端口与默认账号

- 后端 `127.0.0.1:1987`，前端 `127.0.0.1:5173`，opencode `127.0.0.1:4096`
- 默认 admin 密码来自 `.env`：`AGENTOPS_BOOTSTRAP_USERNAME=admin` / `AGENTOPS_BOOTSTRAP_PASSWORD=admin123`
- 首次登录后强制改密（`must_reset_password=1`）

## docs/ 目录约定（速查）

完整规则见 CLAUDE.md 同名段；速查：

- `docs/00-platform/`：跨菜单平台级（architecture / archive / generative-ui / harness-analysis / harness-research / reviews）
- `docs/NN-<menu-name>/`：按侧栏菜单归档（01=监控中心, ..., 09=Coding 终端）
- 文件名前缀：`REQUIREMENTS-` / `DESIGN-` / `ANALYSIS-` / `REVIEW-` / `RESEARCH-` / `PLAN-` / `ARCHIVED-` / `DEPRECATED-`（连字符分隔）
- 总索引：`docs/INDEX.md` —— 新增 / 移动文档必须同步更新，否则视为不完整归档

## Things to avoid（速查）

完整版见 CLAUDE.md 同名段；速查：

- PowerShell `Write` 创建 `.ps1` 时含中文 + `[regex]::Replace` 会产生不可预测乱码 → 优先 `Read` 定位 + `SearchReplace` 工具逐处修复
- 目录 rename 后必查引用（波及 CLAUDE.md / TODO.md / skills/*.md / README.md 等非 docs/ 文件）
- grep 旧名时避免被新前缀（`DESIGN-` / `REQUIREMENTS-` 等）误命中 → 用 `\b` 或负向 lookbehind
- 临时 .ps1 / .py 脚本即用即删（跑完立即 `Remove-Item`）

## 子规则入口

| 关注点 | 文件 |
|---|---|
| 工作流 / DAG / 任务 | `workflow.md` |
| Harness / LLM 接入 | `harness.md` |
| 前端 / React 组件 | `frontend.md` |
| 知识库 / Vault / 域 | `knowledge.md` |
| 会话维护协议（自检） | `session-maintenance-protocol.md` |