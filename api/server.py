"""
FastAPI BFF for v2.1 — serves the frontend at http://localhost:1987.

Endpoints (match web/src/lib/api.ts ApiClient):
  POST /api/agent/run              start a workflow run, return run_id + stream_url
  GET  /api/agent/runs/{id}/events  SSE stream of DagEvents + RawHarnessEvents
  POST /api/agent/runs/{id}/widget-input   forward widget.input into a run
  GET  /api/agent/runs/{id}        final state
  GET  /api/agent/runs             list (debug)
  GET  /                          health check

The backend uses LocalSdkOrchestrator for execution (M1 ready).
OpencodeOrchestrator / AgentOpsOrchestrator are wired but deferred (run loop TODO).

Run:
    uvicorn api.server:app --host 0.0.0.0 --port 1987 --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

import yaml
from dotenv import load_dotenv

# 启动时加载 .env 文件（WECOM_WEBHOOK_URL / MINIMAX_API_KEY 等敏感配置）
load_dotenv()

# Windows 下必须用 ProactorEventLoop，否则 asyncio.create_subprocess_exec
# 在 uvicorn --reload 的 multiprocessing 子进程中会走 SelectorEventLoop → NotImplementedError
# 注意：必须由 `python -u -m uvicorn` 启动（去掉 --reload），因为 reload 子进程
# 不继承父进程的 set_event_loop_policy() → 仍走 SelectorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# 安全认证（S7/S8）。api.security.deps 内部对 api.server 的引用是函数级延迟导入，
# 所以这里顶层 import 不会形成循环。
from api.security.api_tokens import router as security_tokens_router
from api.security.auth import router as auth_router
from api.security.guard import auth_guard
from api.security.roles import perm_router as security_permissions_router
from api.security.roles import router as security_roles_router
from api.security.sessions import router as security_sessions_router
from api.security.users import router as security_users_router

from orchestrator import (
    DagEvent,
    DagEventType,
    LocalSdkOrchestrator,
    RawHarnessEvent,
    RunMode,
    RunRequest,
)
from workflow import (
    WorkflowLoadError,
    WorkflowValidationError,
    load_workflow_text,
    load_workflow_yaml,
    validate_workflow,
)
from audit import EventStore, SqliteEventStore
from orchestrator.patroller import Patroller
from orchestrator import docker_runtime

logger = logging.getLogger(__name__)


def setup_app_logging() -> None:
    """配置应用层 logger（harness / orchestrator / workflow / api / audit / tools）。

    uvicorn 自身的 --log-level 只配置 uvicorn.* logger，应用层 logger 默认 WARNING。
    本函数给应用包 logger 加独立 StreamHandler + INFO level，让 harness 的多轮工具
    调用、HTTP 请求、异常等关键路径 INFO 日志能输出到 stderr，便于排障。

    调用时机：lifespan 启动时调用一次。
    """
    app_logger_names = [
        "harness", "orchestrator", "workflow", "api", "audit", "tools", "config",
    ]
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    for name in app_logger_names:
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        # 避免重复加 handler（lifespan 重启时可能多次调用）
        if not lg.handlers:
            lg.addHandler(handler)
            lg.propagate = False  # 不向 root 传播，避免 uvicorn 重复输出

# Global orchestrator + event-bus registry + 事件存储
_orchestrator: LocalSdkOrchestrator | None = None
_event_streams: dict[str, asyncio.Queue] = {}
_event_store: EventStore | None = None
_patroller: Patroller | None = None
# Thread 模式：Session 引擎实例 + SSE 多消费者广播
_session_engines: dict[str, Any] = {}  # session_id -> SessionEngine
_session_event_streams: dict[str, set[asyncio.Queue]] = {}  # session_id -> set of SSE queues

# P2（deepseek-harness 对齐）：fail-closed 审批服务（惰性初始化）
_approval_service: Any = None


def _get_approval_service() -> Any:
    """惰性创建 ApprovalService（依赖 _session_event_sink，须在模块加载完成后调用）。"""
    global _approval_service
    if _approval_service is None:
        from orchestrator.approval import ApprovalService
        _approval_service = ApprovalService(
            event_sink=_session_event_sink,
            has_subscribers=lambda sid: bool(_session_event_streams.get(sid)),
        )
    return _approval_service
# 全局告警通道：patrol_alert 事件不绑定特定 run，用全局队列广播
_global_alerts: list[dict[str, Any]] = []  # 最近 100 条告警
_global_alert_queue: asyncio.Queue = asyncio.Queue(maxsize=500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭钩子。"""
    global _orchestrator, _event_store, _patroller
    # 配置应用层 logger（让 harness / orchestrator / workflow 的 INFO 日志输出到 stderr）
    setup_app_logging()
    # 从 config/models.yaml 读 manager_model（不再走 env 兜底）
    from orchestrator.model_config import get_model_config
    try:
        mc = get_model_config()
        mgr = mc.config.get("manager_model") or {}
        provider_name = mgr.get("provider", "")
        model_id = mgr.get("model", "")
        prov = mc.get_provider(provider_name) or {}
        logger.info("Manager model: provider=%s model=%s", provider_name, model_id)
        # api_key 优先级链：CredentialStore > models.yaml（${ENV} 展开后可能为空）
        # 与 HealthChecker.check_provider / ModelConfig._build_result 保持一致（D-028 修复）
        api_key = ""
        try:
            from orchestrator.credential_store import get_credential_store
            stored = get_credential_store().get(provider_name)
            if stored:
                api_key = stored
        except Exception as e:
            logger.warning("CredentialStore 查询失败 provider=%s: %s", provider_name, e)
        if not api_key:
            api_key = prov.get("api_key", "")
        llm_cfg = {
            "api_key": api_key,
            "base_url": prov.get("base_url", ""),
            "model": f"{provider_name}/{model_id}" if provider_name and model_id else "",
            "provider": provider_name,       # codex: modelProvider 参数需要
            "model_provider": provider_name,  # codex: modelProvider 别名
        }
    except Exception as e:
        logger.warning("Failed to load models.yaml, fallback to env: %s", e)
        llm_cfg = {
            "api_key": os.environ.get("OPENAI_API_KEY", ""),
            "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            "model": os.environ.get("OPENAI_MODEL", ""),
        }
    # V3 起任务管理模块为正式功能，V1 schema 默认启用（TASK_V1_ENABLED=0 可回退 P0）
    _task_v1_flag = os.getenv("TASK_V1_ENABLED", "1") == "1"
    _event_store = SqliteEventStore(str(PROJECT_ROOT / "audit.db"),
                                    task_v1_enabled=_task_v1_flag)
    logger.info("EventStore initialized at %s (task_v1=%s)",
                PROJECT_ROOT / "audit.db", _task_v1_flag)

    # 任务管理模块初始化（P0 + V1）：TaskStore + TaskOrchestrator + style_loader + terminal_manager
    _task_orchestrator = None
    try:
        from task.store import TaskStore
        from task.orchestrator import TaskOrchestrator
        from task.agent_style import AgentStyleLoader
        from task.terminal_session import TerminalSessionManager
        from orchestrator._registry import set_task_orchestrator
        _task_store = TaskStore(_event_store._conn, _event_store._db_lock)
        _task_v1 = _task_v1_flag
        _task_styles = AgentStyleLoader(PROJECT_ROOT / "config" / "agent_styles")
        _task_terminal = TerminalSessionManager()  # 自动检测后端（psmux/tmux/mock）
        _task_orchestrator = TaskOrchestrator(
            _task_store, p0_mode=not _task_v1,
            style_loader=_task_styles, terminal_manager=_task_terminal,
            llm_config=llm_cfg)
        set_task_orchestrator(_task_orchestrator)
        app.state.task_store = _task_store
        app.state.task_orchestrator = _task_orchestrator
        app.state.task_styles = _task_styles
        app.state.task_terminal = _task_terminal
        # 🆕 ReportExporter: 任务报告多格式导出（md/html/json）+ SHA-256 校验
        from task.exporter import ReportExporter
        _report_exporter = ReportExporter(
            conn=_task_store._conn, db_lock=_task_store._db_lock,
            workspace_root=PROJECT_ROOT / "workspace")
        _report_exporter.ensure_schema()
        app.state.report_exporter = _report_exporter
        logger.info("ReportExporter initialized (workspace=%s)", _report_exporter._exports_root)
        logger.info("TaskOrchestrator initialized (p0_mode=%s, styles=%d, terminal=%s)",
                    not _task_v1, len(_task_styles.list_styles()), _task_terminal.backend_name)
    except Exception as e:
        logger.warning("TaskOrchestrator 初始化失败（不阻塞 lifespan）: %s", e)

    # T3 断点收尾：后端重启后补偿收尾 terminal 模式残留任务
    # （in_progress 超 1h + 有 terminal_session_id，从 transcript 补 _finalize_execution，
    #  幂等：已收尾任务会转 validating 不再命中）
    try:
        from task.terminal_exec import TerminalExecDriver
        if _task_orchestrator is not None:
            _reconciled = await TerminalExecDriver.reconcile_stale(
                _task_orchestrator, max_age_s=3600.0, limit=10)
            if _reconciled:
                logger.info("terminal-exec 断点收尾 %d 个任务: %s",
                            len(_reconciled), _reconciled)
    except Exception as e:
        logger.debug("terminal-exec 断点收尾跳过（不阻塞启动）: %s", e)

    # P0.18.7f: 实例化 ContainerProvisioner（5 步启动 + 4 步销毁 + tier 资源限制 + WS 等待）
    # 若 docker 不可用，provisioner 内部降级到 no-op（不阻塞 lifespan）
    _container_provisioner = None
    try:
        from orchestrator.container_provisioner import ContainerProvisioner
        from orchestrator.worker_token import get_worker_registry
        _container_provisioner = ContainerProvisioner(
            event_store=_event_store,
            worker_registry=get_worker_registry(),
        )
        logger.info("ContainerProvisioner initialized")
    except Exception as e:
        logger.warning("ContainerProvisioner 初始化失败，回退到旧路径: %s", e)

    _orchestrator = LocalSdkOrchestrator(
        llm_config=llm_cfg,
        event_store=_event_store,
        container_provisioner=_container_provisioner,  # P0.18.7f
    )
    # 注册到全局 registry，让 tools/trigger_workflow.py 能在不直接 import orchestrator 的情况下调用
    from orchestrator._registry import (
        set_event_bridge, set_event_store, set_orchestrator,
        set_session_manager,  # 🆕 Phase 1
        set_memory_manager,    # 🆕 Phase 2
        set_container_provisioner,  # P0.18.7b
    )
    set_orchestrator(_orchestrator)
    set_event_store(_event_store)
    set_event_bridge(_event_bridge_for_trigger)
    set_container_provisioner(_container_provisioner)  # P0.18.7b: 注册到全局 registry

    # 🆕 Phase 1: 初始化 SessionManager
    from orchestrator.session_manager import SessionManager
    _session_manager = SessionManager(event_store=_event_store)
    set_session_manager(_session_manager)
    logger.info("SessionManager initialized")

    # 🆕 Phase 2: 初始化 MemoryManager
    from orchestrator.memory_manager import MemoryManager
    _memory_manager = MemoryManager(event_store=_event_store, llm_config=llm_cfg)
    set_memory_manager(_memory_manager)
    logger.info("MemoryManager initialized")
    # 从 CredentialStore 注入 provider API key 到 opencode.json
    # 消除两套凭证割裂：前端运行时配置页录入的 key 存在 CredentialStore（加密），
    # 但 opencode server 是独立进程，只认 opencode.json，不认 CredentialStore。
    # 本函数把 CredentialStore 中的真实 key 写入 opencode.json 的 apiKey 字段。
    _sync_opencode_credentials()
    # Pre-load default workflows
    for wf_path in (PROJECT_ROOT / "workflows").glob("*.yaml"):
        try:
            _orchestrator.load_workflow_file(str(wf_path))
            logger.info(f"Loaded workflow: {wf_path.name}")
        except Exception as e:
            logger.warning(f"Failed to load {wf_path.name}: {e}")

    # 🆕 WorkflowRegistry 扫描——启动时提取 workflow metadata 用于 system_prompt 动态注入
    from orchestrator.workflow_registry import WorkflowRegistry
    from orchestrator._registry import set_workflow_registry
    _wf_registry = WorkflowRegistry(str(PROJECT_ROOT / "workflows"))
    _wf_registry.scan()
    set_workflow_registry(_wf_registry)

    # 🆕 Phase 2: SkillRegistry 扫描——启动时解析 skills/*/SKILL.md frontmatter
    from orchestrator.skill_registry import SkillRegistry
    from orchestrator._registry import set_skill_registry
    _skill_registry = SkillRegistry(str(PROJECT_ROOT / "skills"))
    _skill_registry.scan()
    set_skill_registry(_skill_registry)

    # 一次性配置迁移：patrol.yaml 旧段 → private/log-pull.yaml + config/schedules.yaml
    # 必须在 Patroller 启动前执行（Patroller 读 schedules.yaml；失败不阻塞启动，报 ERROR 人工修复）
    try:
        from orchestrator import config_migrate
        _mig = config_migrate.run_once()
        if _mig.get("migrated"):
            logger.info(
                "Config migrated (patrol → private/schedules): private=%s schedules=%s patrol_cleaned=%s",
                _mig.get("private_written"), _mig.get("schedules_written"), _mig.get("patrol_cleaned"),
            )
    except Exception as e:
        logger.error("配置迁移失败（人工修复后重启）：%s", e)

    # 懒迁移：已存在的 schedules.yaml 条目若缺 id，按 name slug 一次性补齐（已有 id 不覆盖）
    try:
        from orchestrator import schedules_admin
        schedules_admin.ensure_ids()
        logger.info("schedules id 懒迁移完成（若有缺 id 条目已自动补齐）")
    except Exception as e:
        logger.error("schedules id 懒迁移失败：%s", e)

    # 启动 Patroller 巡检器（DAG run 巡检 + 日志巡检定时触发）
    async def _patrol_event_sink(event: dict[str, Any]) -> None:
        """Patroller 事件回调：存内存 list + 推全局告警队列。"""
        _global_alerts.append(event)
        if len(_global_alerts) > 100:
            _global_alerts.pop(0)
        try:
            _global_alert_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 队列满则丢弃（前端可轮询 _global_alerts 补偿）

    async def _workflow_trigger(workflow_id: str, inputs: dict[str, Any]) -> str | None:
        """触发工作流的回调（供 Patroller 调用）。"""
        if not _orchestrator or workflow_id not in _orchestrator.workflows:
            logger.warning("Patroller 触发失败：workflow %s 未加载", workflow_id)
            return None
        try:
            # v3 修复：提前生成 session_id，让 engine 启动前先写 runs 表（避免 FK 竞态）
            _sid = f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{_uuid4().hex[:6]}"
            handle = await _orchestrator.run(RunRequest(
                workflow_id=workflow_id,
                inputs=inputs,
                run_mode=RunMode.TEMPLATED,
                session_id=_sid,
            ))
            if _event_store:
                await _event_store.create_session(
                    session_id=_sid,
                    agent_id="",
                )
                await _event_store.init_run(
                    run_id=handle.run_id,
                    session_id=_sid,
                    workflow_id=workflow_id,
                    run_mode="templated",
                    inputs=inputs,
                )
            # 初始化 Event queue（和 start_run 一致）
            queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
            _event_streams[handle.run_id] = queue

            async def _bridge():
                try:
                    async for ev in _orchestrator.stream_events(handle.run_id):
                        if isinstance(ev, DagEvent) and _event_store:
                            await _event_store.append_run_event(run_id=ev.run_id or handle.run_id, event_type=ev.type.value if hasattr(ev.type, "value") else str(ev.type), payload=ev.payload or {}, node_id=ev.node_id)
                        elif isinstance(ev, RawHarnessEvent) and _event_store:
                            await _event_store.append_raw_event(ev)
                        await queue.put(ev)
                finally:
                    if _event_store and _orchestrator:
                        try:
                            state = await _orchestrator.get_run(handle.run_id)
                            if state:
                                await _event_store.finalize_run(
                                    run_id=handle.run_id,
                                    status=state.status.value,
                                    finished_at=state.finished_at,
                                    total_tokens_in=state.total_tokens_input,
                                    total_tokens_out=state.total_tokens_output,
                                    total_cost_usd=state.total_cost_usd,
                                    error=state.error,
                                    final_outputs=state.node_outputs,
                                )
                        except Exception as e:
                            logger.warning("finalize_run 失败: %s", e)
                    await queue.put(None)

            asyncio.create_task(_bridge())
            return handle.run_id
        except Exception as e:
            logger.error("Patroller 触发工作流 %s 失败: %s", workflow_id, e)
            return None

    _patroller = Patroller(
        event_store=_event_store,
        event_sink=_patrol_event_sink,
        workflow_trigger=_workflow_trigger,
        patrol_interval_seconds=60,        # DAG run 巡检每 60s（patrol.yaml 覆盖）
        stale_threshold_seconds=1800,      # 30 分钟无事件视为超时（patrol.yaml 覆盖）
        log_patrol_interval_seconds=3600,  # 兼容回退：无 patrol.yaml 时每小时触发一次
        config_path="config/patrol.yaml",  # 加载 cron + log_sources 白名单配置
    )
    _patroller.start()
    logger.info("Patroller 巡检器已启动（配置：config/patrol.yaml；DAG run 巡检 60s/次）")

    # lifespan 启动时扫描遗留的 active conversational run → 转 dormant
    # （服务重启后内存 queue 丢失，active 状态无意义）
    if _event_store:
        try:
            active_sessions = await _event_store.list_sessions(
                status="active", limit=100
            )
            for s in active_sessions:
                await _event_store.update_session_status(
                    s["session_id"], "dormant", last_activity=False
                )
            if active_sessions:
                logger.info("启动时扫描到 %d 个 active session，已转 dormant", len(active_sessions))
        except Exception as e:
            logger.warning("启动 dormant 扫描失败: %s", e)

    # 启动时清扫孤儿 worker 容器：进程重启后上一进程创建的 ao_* 容器
    # 全部是孤儿（强杀时 finally 清理没机会执行），stop + remove 兜底。
    # docker 不可用时静默跳过（不阻塞启动）。
    try:
        from orchestrator import docker_runtime as _docker_rt
        _orphans = await asyncio.to_thread(_docker_rt.cleanup_orphan_worker_containers)
        _ok = [c for c in _orphans if c.get("removed") == "true"]
        _fail = [c for c in _orphans if c.get("removed") != "true"]
        if _ok:
            logger.info("启动时清扫孤儿 worker 容器 %d 个: %s", len(_ok), [c["name"] for c in _ok])
        if _fail:
            logger.warning("启动时清扫失败的容器 %d 个: %s", len(_fail), _fail)
    except Exception as e:
        logger.info("启动时清扫孤儿容器跳过（docker 不可用或无孤儿）: %s", e)

    yield

    # 停止 Patroller
    if _patroller:
        await _patroller.stop()
        _patroller = None
    if _event_store:
        await _event_store.close()
    _orchestrator = None
    _event_store = None


# 全局路径级鉴权（S13，方案 v1.4 §3.6）：OPTIONS / 非 /api / /api/auth/* 放行，
# 其余按 guard 映射表解析 scope；未登记路径 fail-closed。WebSocket 不经过 HTTP 依赖。
app = FastAPI(
    title="AgentOps",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(auth_guard)],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:5174", "http://127.0.0.1:5174",
        "http://localhost:5175", "http://127.0.0.1:5175",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /api/auth/* 登录 / 登出 / 改密 / 当前身份（S7）
app.include_router(auth_router)

# /api/security/* 管理面（S8-S11）
app.include_router(security_users_router)
app.include_router(security_roles_router)
app.include_router(security_permissions_router)
app.include_router(security_tokens_router)
app.include_router(security_sessions_router)


class RunPayload(BaseModel):
    workflow_id: str | None = None
    inputs: dict[str, Any] = {}
    # P1: conversational/task 模式
    run_mode: str = "templated"          # templated / conversational / task / hybrid
    agent_id: str | None = None
    initial_message: str = ""
    # P0.18.7b: 指定授权 workspace（None=通用对话，走旧 docker_runtime 路径）
    workspace_id: str | None = None
    # 可选：指定关联 session_id（让 bridge_run_events 把 run 事件转发到正确的 session SSE）
    # 不传时自动生成新 session_id
    session_id: str | None = None


class ResumePayload(BaseModel):
    """断点恢复/节点重执行请求。"""
    workflow_id: str                     # 工作流 ID（从历史 run 的 summary 恢复）
    inputs: dict[str, Any] = {}           # 全局输入参数
    node_id: str | None = None           # 指定从某个节点重执行（None=自动跳过已完成）
    only_node: bool = False              # True=仅重试当前节点（保留下游文件；默认 False=连同下游一起重跑）


class RunResponse(BaseModel):
    run_id: str
    stream_url: str


class WidgetInputPayload(BaseModel):
    widget_id: str
    input: dict[str, Any]


class MessagePayload(BaseModel):
    """发消息到会话（新建和历史继续同一入口）。"""
    message: str
    agent_id: str | None = None        # 新建会话时指定
    run_mode: str = "conversational"   # 新建会话时指定


class SessionCreatePayload(BaseModel):
    """新建会话。"""
    agent_id: str = "manager"
    message: str
    run_mode: str = "conversational"
    # P0.18.7b: 指定授权 workspace（None=通用对话）
    workspace_id: str | None = None


class WorkflowPayload(BaseModel):
    yaml_content: str


class CredentialPayload(BaseModel):
    api_key: str


class SshCredentialPayload(BaseModel):
    """SSH 凭据录入（log-puller / ssh_exec 复用，存 CredentialStore，id 形如 ssh:<source_id>）。"""
    secret: str  # 密码或私钥口令（Fernet 加密存储，不回显）


class VaultSearchPayload(BaseModel):
    query: str
    search_type: str = "keyword"
    max_results: int = 100
    ext_filter: list[str] | None = None


class LintTriggerPayload(BaseModel):
    check_types: list[str] | None = None
    auto_fix: bool = False


class LintResolvePayload(BaseModel):
    action: str
    note: str = ""


class ScanDraftsPayload(BaseModel):
    since: str | None = None
    draft_root: str = "草稿仓库"


class CuratePayload(BaseModel):
    draft_paths: list[str] | None = None
    since: str | None = None


class KnowledgeAskPayload(BaseModel):
    question: str
    domain: str | None = None


@app.get("/")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# ── Agent CRUD (P5: 配置驱动 + 运行时 CRUD) ─────────────────────────

# 运行时 agent（POST/PUT/DELETE 创建的临时 agent，重启丢失）
_agents_store: dict[str, dict] = {}

def _agent_def_to_dict(aid: str, agent) -> dict:
    """AgentDefinition → API 返回格式（完整暴露身份/权责/权限/运行时配置）。"""
    return {
        "id": aid,
        "name": agent.display_name,
        "domain": agent.domain,
        "description": agent.description,
        "harness": agent.harness,
        "model": agent.model_config,
        "system_prompt": agent.system_prompt,
        "allowed_tools": agent.allowed_tools,
        "denied_tools": agent.denied_tools,
        "knowledge_bases": agent.knowledge_bases,
        "max_concurrent_runs": agent.max_concurrent_runs,
        "timeout_seconds": agent.timeout_seconds,
        "cost_limit_per_run": agent.cost_limit_per_run,
        "output_files": agent.output_files,
        "tier": getattr(agent, "tier", "T2") or "T2",  # P0.18.7g: 暴露 tier 字段供前端 WorkspaceSelectorDialog 校验
        "source": "config",                      # 来源：配置文件
    }


# 内置工具元数据（不在 config/tools/ 中定义，由引擎自身提供）
_BUILTIN_TOOLS: list[dict[str, Any]] = [
    {"tool_id": "finalize", "display_name": "完成会话", "description": "Agent 完成工具，标记当前回合结束", "allowed_domains": [], "requires_human_approval": False, "builtin": True},
    {"tool_id": "request_cross_domain", "display_name": "跨域请求", "description": "经 Manager 中转的跨业务域请求", "allowed_domains": [], "requires_human_approval": False, "builtin": True},
    {"tool_id": "classify_intent", "display_name": "意图识别", "description": "Manager 识别用户意图（编排 vs 直接对话）", "allowed_domains": ["manager"], "requires_human_approval": False, "builtin": True},
    {"tool_id": "plan_tasks", "display_name": "任务分解", "description": "Manager 将任务拆解为多步骤计划", "allowed_domains": ["manager"], "requires_human_approval": False, "builtin": True},
    {"tool_id": "dispatch", "display_name": "任务派发", "description": "Manager 将子任务派发给业务域 Agent", "allowed_domains": ["manager"], "requires_human_approval": False, "builtin": True},
    {"tool_id": "aggregate", "display_name": "结果聚合", "description": "Manager 聚合各子任务结果", "allowed_domains": ["manager"], "requires_human_approval": False, "builtin": True},
    {"tool_id": "read_file", "display_name": "读取文件", "description": "读取工作区文件内容", "allowed_domains": [], "requires_human_approval": False, "builtin": True},
    {"tool_id": "write_file", "display_name": "写入文件", "description": "写入工作区文件", "allowed_domains": [], "requires_human_approval": False, "builtin": True},
    {"tool_id": "bash", "display_name": "执行命令", "description": "执行 shell 命令", "allowed_domains": [], "requires_human_approval": False, "builtin": True},
]


def _compute_agent_workflow_bindings(agent_id: str) -> list[dict[str, Any]]:
    """计算某 agent 被哪些 workflow 的哪些节点引用（真实绑定关系）。"""
    if _orchestrator is None:
        return []
    bindings: list[dict[str, Any]] = []
    for wf_id, wf in _orchestrator.workflows.items():
        nodes_using = [
            {"node_id": nid, "node_name": node.name, "harness": node.harness.value}
            for nid, node in wf.nodes.items()
            if node.agent == agent_id
        ]
        if nodes_using:
            bindings.append({"workflow_id": wf_id, "workflow_name": wf.name, "nodes": nodes_using})
    return bindings


def _write_agent_yaml(agent_id: str, data: dict[str, Any]) -> None:
    """将 agent 配置写回 config/agents/{agent_id}.yaml（持久化）。"""
    # 构造与 config/agents/*.yaml 一致的字典结构
    model_val = data.get("model", "auto")
    if isinstance(model_val, dict):
        model_out = {"provider": model_val.get("provider", ""), "id": model_val.get("id", "")}
    else:
        model_out = model_val

    yaml_dict: dict[str, Any] = {
        "agent_id": agent_id,
        "domain": data.get("domain", ""),
        "display_name": data.get("name") or data.get("display_name", agent_id),
        "description": data.get("description", ""),
        "harness": data.get("harness", "local_llm"),
        "model": model_out,
        "system_prompt": data.get("system_prompt", ""),
    }
    if data.get("output_files"):
        yaml_dict["output_files"] = data["output_files"]
    if data.get("tools"):
        yaml_dict["tools"] = data["tools"]
    yaml_dict["permissions"] = {
        "allowed_tools": data.get("allowed_tools", []),
        "denied_tools": data.get("denied_tools", []),
    }
    if data.get("knowledge_bases"):
        yaml_dict["knowledge_bases"] = data["knowledge_bases"]
    yaml_dict["max_concurrent_runs"] = data.get("max_concurrent_runs", 1)
    yaml_dict["timeout_seconds"] = data.get("timeout_seconds", 3600)
    yaml_dict["cost_limit_per_run"] = data.get("cost_limit_per_run", 1.0)

    wf_path = PROJECT_ROOT / "config" / "agents" / f"{agent_id}.yaml"
    with open(wf_path, "w", encoding="utf-8") as f:
        f.write(f"# config/agents/{agent_id}.yaml\n")
        yaml.safe_dump(yaml_dict, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

def _get_all_agents() -> dict[str, dict]:
    """合并配置 agent + 运行时 agent。"""
    from orchestrator.config_loader import get_system_config
    config_agents = {
        aid: _agent_def_to_dict(aid, agent)
        for aid, agent in get_system_config().agents.items()
    }
    # 运行时 agent 覆盖同名配置 agent
    config_agents.update(_agents_store)
    return config_agents

@app.get("/api/agent/agents")
async def list_agents(domain: str | None = None):
    """列出所有 Agent，支持 ?domain=xxx 过滤。"""
    agents = _get_all_agents()
    if domain:
        agents = {k: v for k, v in agents.items() if v.get("domain") == domain}
    return {"agents": list(agents.values())}

@app.post("/api/agent/agents")
async def create_agent(agent: dict):
    """创建智能体并持久化到 config/agents/{id}.yaml。"""
    aid = agent.get("id") or (agent.get("name", "") or "").lower().replace(" ", "_")
    if not aid:
        raise HTTPException(400, "缺少 agent id 或 name")
    from orchestrator.config_loader import get_system_config, reload_system_config
    config = get_system_config()
    if aid in config.agents:
        raise HTTPException(409, f"Agent 已存在: {aid}")
    try:
        _write_agent_yaml(aid, {**agent, "id": aid})
        reload_system_config()
    except Exception as e:
        raise HTTPException(500, f"写入配置失败: {e}")
    new_agent = get_system_config().agents.get(aid)
    return {"agent": _agent_def_to_dict(aid, new_agent)} if new_agent else {"agent": {**agent, "id": aid}}

@app.get("/api/agent/agents/{agent_id}")
async def get_agent(agent_id: str):
    agents = _get_all_agents()
    if agent_id not in agents:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    agent_data = agents[agent_id]
    # 附带真实运行统计 + 工作流绑定
    wf_bindings = _compute_agent_workflow_bindings(agent_id)
    if _event_store:
        # 同时按 agent_id 和绑定的 workflow_id 聚合统计
        wf_ids = [wb["workflow_id"] for wb in wf_bindings]
        agent_data["stats"] = await _event_store.get_agent_stats(agent_id, workflow_ids=wf_ids)
    agent_data["workflow_bindings"] = wf_bindings
    return {"agent": agent_data}

@app.put("/api/agent/agents/{agent_id}")
async def update_agent(agent_id: str, agent: dict):
    """更新智能体配置并持久化到 YAML（config agent 写回文件，runtime agent 更新内存）。"""
    from orchestrator.config_loader import get_system_config, reload_system_config
    config = get_system_config()
    if agent_id in config.agents:
        # 配置 agent：写回 YAML 并热重载
        try:
            _write_agent_yaml(agent_id, {**agent, "id": agent_id})
            reload_system_config()
        except Exception as e:
            raise HTTPException(500, f"写入配置失败: {e}")
        updated = get_system_config().agents.get(agent_id)
        return {"agent": _agent_def_to_dict(agent_id, updated)} if updated else {"agent": {**agent, "id": agent_id}}
    elif agent_id in _agents_store:
        _agents_store[agent_id] = {**agent, "id": agent_id, "source": "runtime"}
        return {"agent": _agents_store[agent_id]}
    else:
        raise HTTPException(404, f"Agent not found: {agent_id}")

@app.delete("/api/agent/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """删除智能体（runtime agent 删内存；config agent 删 YAML 文件）。"""
    if agent_id in _agents_store:
        del _agents_store[agent_id]
        return {"deleted": agent_id}
    from orchestrator.config_loader import get_system_config, reload_system_config
    if agent_id in get_system_config().agents:
        wf_path = PROJECT_ROOT / "config" / "agents" / f"{agent_id}.yaml"
        if wf_path.exists():
            wf_path.unlink()
        reload_system_config()
        return {"deleted": agent_id}
    raise HTTPException(404, f"Agent not found: {agent_id}")


@app.get("/api/agent/tools")
async def list_tools():
    """列出全部工具元数据（config/tools/*.yaml + 内置工具），供权限管理 UI 展示可读名称。"""
    from orchestrator.config_loader import get_system_config
    config = get_system_config()
    tools = [
        {
            "tool_id": t.tool_id,
            "display_name": t.display_name,
            "description": t.description,
            "allowed_domains": t.allowed_domains,
            "requires_human_approval": t.requires_human_approval,
            "handler_module": t.handler_module,
            "handler_function": t.handler_function,
            "builtin": False,
        }
        for t in config.tools.values()
    ]
    return {"tools": tools + _BUILTIN_TOOLS}


@app.get("/api/agent/agents/{agent_id}/stats")
async def agent_stats(agent_id: str):
    """单 agent 真实运行统计（来自审计库 runs 表）+ 工作流绑定。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    wf_bindings = _compute_agent_workflow_bindings(agent_id)
    wf_ids = [wb["workflow_id"] for wb in wf_bindings]
    stats = await _event_store.get_agent_stats(agent_id, workflow_ids=wf_ids)
    return {
        "agent_id": agent_id,
        "stats": stats,
        "workflow_bindings": wf_bindings,
    }


# ── Actor Visual Profile（v99.5 P0.11）───────────────────────────────────
# 列出 config/actors/*/actor_visual_profile.json 全部 profile，
# 供前端 SupervisionPanel 启用 view_id 白名单（reject 未授权 snapshot）。
# 与 /api/agent/agents（agent 维度）并列，二者语义不同：
#   - /api/agent/agents      → Agent (runtime + config agents，含 harness/tools/model)
#   - /api/actors            → Actor Profile (L1.5 Worker Profile 层，含 view_id 白名单)

@app.get("/api/actors")
async def list_actors():
    """列出所有 Actor Visual Profile（v99.5 L1.5 Worker Profile 层）。

    返回每个 profile 的 actor_id + description + allowed_surface_views[]，
    前端 SupervisionPanel 据此启用 view_id 白名单（reject 未授权的 snapshot）。
    """
    from orchestrator.actor_visual_profile import list_actor_visual_profiles

    profiles = list_actor_visual_profiles()
    actors = []
    for p in profiles:
        actors.append({
            "actor_id": p.actor_id,
            "description": p.description,
            "allowed_surface_views": [
                {
                    "view_id": v.view_id,
                    "output_contract": v.output_contract,
                    "description": v.description,
                    "required_phases": list(v.required_phases),
                    "fields": {
                        fname: {
                            "type": fc.type,
                            "required": fc.required,
                            **({"max_length": fc.max_length} if fc.max_length is not None else {}),
                            **({"min": fc.min} if fc.min is not None else {}),
                            **({"max": fc.max} if fc.max is not None else {}),
                            **({"enum_values": list(fc.enum_values)} if fc.enum_values else {}),
                        }
                        for fname, fc in v.fields.items()
                    },
                }
                for v in p.allowed_surface_views.values()
            ],
        })
    return {"actors": actors}


# ── Domain list (P5 新增) ───────────────────────────────────────────

@app.get("/api/agent/domains")
async def list_domains():
    """列出所有业务域。"""
    from orchestrator.config_loader import get_system_config
    config = get_system_config()
    domains = [
        {
            "domain": d.domain,
            "display_name": d.display_name,
            "description": d.description,
            "default_harness": d.default_harness,
            "allowed_tools": d.allowed_tools,
            "denied_tools": d.denied_tools,
        }
        for d in config.domains.values()
    ]
    return {"domains": domains}

class ModelPayload(BaseModel):
    provider_id: str
    model: dict[str, Any]                     # {id, max_tokens, price_input_per_1k, price_output_per_1k}


class UpdateProviderPayload(BaseModel):
    base_url: str | None = None
    protocol: str | None = None
    auth_type: str | None = None


class SetModelPayload(BaseModel):
    provider_id: str
    model_id: str


class CreateProviderPayload(BaseModel):
    provider_id: str
    base_url: str
    protocol: str = "openai_compatible"
    auth_type: str = "bearer"
    api_key_env: str = ""


# ── 统一运行时配置 API ───────────────────────────────────────────────

@app.get("/api/runtime/summary")
async def runtime_summary():
    """统一运行时摘要：harness 列表 + provider 完整信息 + 模型列表 + fallback_chains。"""
    # 1. Harness 列表（含类型元数据）
    from harness import HarnessRegistry
    from harness.protocol import HarnessType
    # Harness 类型中文名映射
    HARNESS_LABELS: dict[str, str] = {
        "deterministic": "确定性脚本",
        "opencode": "OpenCode",
        "claude_code": "Claude Code",
        "codex": "Codex CLI",
        "kimi": "Kimi Code",
        "http": "HTTP 端点",
        "local_llm": "本地 LLM",
    }
    harnesses = [
        {
            "type": ht.value,
            "label": HARNESS_LABELS.get(ht.value, ht.value),
        }
        for ht in HarnessRegistry.available()
    ]

    # 2. Provider 列表（含模型、凭证状态）
    providers = _list_all_providers()

    # 3. Fallback chain 配置（v2 shape：list[{provider, model?}]，兼容旧 ["string"]）
    from orchestrator.provider_health import get_fallback_chain
    from orchestrator.model_config import get_model_config
    mc = get_model_config()
    fc = get_fallback_chain()
    fallback_chains: dict[str, list[dict[str, str | None]]] = {}
    # 只返回用户在 models.yaml 显式配置的 provider，不自动给每个 provider 补空链
    # （旧行为会引入大量 "" 空链，UI 难以区分"未配置" vs "配置为空"）
    for pid in (mc.config.get("fallback_chains", {}) or {}).keys():
        chain = fc.get_chain(pid)
        if chain:
            fallback_chains[pid] = chain

    # 4. 全局默认 + Manager 模型
    global_default = mc.config.get("default", {})
    manager_model = mc.config.get("manager_model", {})

    return {
        "harnesses": harnesses,
        "providers": providers,
        "fallback_chains": fallback_chains,
        "default_provider": global_default.get("provider", ""),
        "default_model": global_default.get("model", ""),
        "manager_provider": manager_model.get("provider", ""),
        "manager_model": manager_model.get("model", ""),
    }


@app.get("/api/runtime/diag-mmx")
async def diag_mmx():
    """临时诊断端点：在服务进程内测试 mmx CLI。"""
    import subprocess
    import os
    # 清除 MINIMAX_BASE_URL 后测试
    env_clean = os.environ.copy()
    env_clean.pop("MINIMAX_BASE_URL", None)
    r = subprocess.run(
        'mmx search query "test from server"',
        shell=True, capture_output=True, text=True,
        timeout=30, encoding="utf-8", errors="ignore",
        stdin=subprocess.DEVNULL,
        env=env_clean,
    )
    return {"rc": r.returncode, "stdout_len": len(r.stdout), "stderr": r.stderr[:200]}


@app.get("/api/runtime/health")
async def runtime_health():
    """获取所有 provider 健康状态（异步批量检查）。"""
    from orchestrator.provider_health import get_health_checker
    checker = get_health_checker()

    results = {}
    providers = _list_all_providers()
    for p in providers:
        pid = p["provider_id"]
        # 只检查有凭证的 provider
        if p["has_env_key"] or p["has_credential"]:
            result = await asyncio.to_thread(checker.check_provider, pid)
            results[pid] = result
        else:
            results[pid] = {"ok": False, "latency_ms": 0, "error": "无凭证"}
    return {"providers": results}


@app.get("/api/runtime/docker/containers")
async def list_docker_containers(all: bool = True):
    """列出本机 Docker 容器（轻量 wrapper）。"""
    try:
        lst = await asyncio.to_thread(docker_runtime.list_containers, all)
        return {"containers": lst}
    except Exception as e:
        raise HTTPException(500, f"Docker error: {e}")


class PullImagePayload(BaseModel):
    image: str


@app.post("/api/runtime/docker/images/pull")
async def pull_docker_image(payload: PullImagePayload):
    try:
        res = await asyncio.to_thread(docker_runtime.pull_image, payload.image)
        return {"status": "pulled", "result": res}
    except Exception as e:
        raise HTTPException(500, f"Docker pull error: {e}")


class CreateContainerPayload(BaseModel):
    image: str
    name: str | None = None
    cmd: list[str] | None = None
    env: dict[str, str] | None = None
    labels: dict[str, str] | None = None


@app.post("/api/runtime/docker/containers")
async def create_docker_container(payload: CreateContainerPayload):
    try:
        res = await asyncio.to_thread(
            docker_runtime.create_and_start_container,
            payload.image,
            payload.name,
            payload.cmd,
            payload.env,
            payload.labels,
        )
        return {"status": "started", "container": res}
    except Exception as e:
        raise HTTPException(500, f"Docker create error: {e}")


@app.post("/api/runtime/docker/containers/{container_id}/stop")
async def stop_docker_container(container_id: str):
    try:
        await asyncio.to_thread(docker_runtime.stop_container, container_id)
        return {"status": "stopped", "container_id": container_id}
    except Exception as e:
        raise HTTPException(500, f"Docker stop error: {e}")


@app.delete("/api/runtime/docker/containers/{container_id}")
async def remove_docker_container(container_id: str):
    try:
        await asyncio.to_thread(docker_runtime.remove_container, container_id, True)
        return {"status": "removed", "container_id": container_id}
    except Exception as e:
        raise HTTPException(500, f"Docker remove error: {e}")


@app.get("/api/runtime/docker/containers/{container_id}/logs")
async def docker_container_logs(container_id: str, tail: int = 200):
    try:
        logs = await asyncio.to_thread(docker_runtime.container_logs, container_id, tail)
        return {"container_id": container_id, "logs": logs}
    except Exception as e:
        raise HTTPException(500, f"Docker logs error: {e}")


# ═══════════════════════════════════════════════════════════════
# P0.17 Runtime Environment 面板
# ═══════════════════════════════════════════════════════════════
# 4 个端点对齐 docs/p017-runtime-environment-panel.md：
# GET  /api/runtime/environment              聚合 health
# POST /api/runtime/environment/rebuild     触发镜像重建
# GET  /api/runtime/environment/build/{id}/stream  SSE 实时日志
# GET  /api/runtime/environment/workers     活跃 subagent 列表

from orchestrator import runtime_environment as runtime_env


@app.get("/api/runtime/environment")
async def get_runtime_environment():
    """P0.17: 聚合 docker 状态 + agentops-worker 镜像 + 活跃 worker + 源码指纹。"""
    try:
        snapshot = await runtime_env.get_environment_snapshot(event_store=_event_store)
        return snapshot
    except Exception as e:
        raise HTTPException(500, f"runtime environment snapshot failed: {e}")


class RebuildPayload(BaseModel):
    force: bool = False


@app.post("/api/runtime/environment/rebuild")
async def rebuild_runtime_environment(payload: RebuildPayload):
    """P0.17: 触发 agentops-worker 镜像重建（异步）。返回 build_id 用于订阅 SSE 日志。"""
    build_id = await runtime_env.build_registry.create()
    if not payload.force:
        active = await runtime_env.build_registry.latest_active()
        if active and active["build_id"] != build_id:
            # 有别的 build 在跑
            raise HTTPException(
                409,
                f"已有 build 在运行: {active['build_id']}",
            )
    # 异步触发构建（不 await，避免阻塞响应）
    async def _run_build() -> None:
        try:
            await runtime_env.build_worker_image(build_id, force=payload.force)
        except Exception as e:
            logger.error("build_worker_image failed: %s", e)
            await runtime_env.build_registry.append_log(build_id, f"## BUILD ERROR: {e}")

    asyncio.create_task(_run_build())
    return {"build_id": build_id, "status": "queued"}


@app.get("/api/runtime/environment/build/{build_id}/stream")
async def stream_build_log(build_id: str, request: Request):
    """P0.17: SSE 实时推送 build 日志。

    推送格式（每行一个 data frame）：
      data: {"line": "...", "ts": "..."}
      data: {"event": "done", "exit_code": 0}
    """
    initial = await runtime_env.build_registry.get(build_id)
    if initial is None:
        raise HTTPException(404, f"build {build_id} not found")

    async def event_generator() -> AsyncIterator[str]:
        last_idx = 0
        # 1. 先把历史日志全部补推
        for entry in initial["logs"]:
            yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            last_idx += 1
        # 2. 等待新日志或 build 结束
        while True:
            if await request.is_disconnected():
                break
            current = await runtime_env.build_registry.get(build_id)
            if current is None:
                break
            new_logs = current["logs"][last_idx:]
            for entry in new_logs:
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                last_idx += 1
            if current["status"] in ("completed", "failed"):
                yield f"data: {json.dumps({'event': 'done', 'exit_code': current['exit_code']}, ensure_ascii=False)}\n\n"
                break
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/runtime/environment/workers")
async def list_active_workers():
    """P0.17: 列出当前活跃 subagent（来自 subagent_provisioned_workers JOIN subagents）。"""
    try:
        result = await runtime_env.get_connected_workers(_event_store)
        return result
    except Exception as e:
        raise HTTPException(500, f"list workers failed: {e}")


@app.post("/api/runtime/models")
async def add_model(payload: ModelPayload):
    """向 models.yaml 添加/更新模型。"""
    from orchestrator.model_config import get_model_config
    mc = get_model_config()
    mc.add_model(payload.provider_id, payload.model)
    return {"status": "saved", "provider_id": payload.provider_id, "model_id": payload.model.get("id")}


@app.delete("/api/runtime/models/{provider_id}/{model_id}")
async def delete_model(provider_id: str, model_id: str):
    """从 models.yaml 删除模型。"""
    from orchestrator.model_config import get_model_config
    mc = get_model_config()
    ok = mc.remove_model(provider_id, model_id)
    if not ok:
        raise HTTPException(404, f"Model not found: {provider_id}/{model_id}")
    return {"status": "deleted", "provider_id": provider_id, "model_id": model_id}


class DeleteProviderPayload(BaseModel):
    provider_id: str


@app.delete("/api/runtime/providers")
async def delete_runtime_provider(payload: DeleteProviderPayload):
    """删除供应商（含其全部模型、凭证、default/manager/fallback 引用）。

    用 body 传 provider_id 以支持含斜杠的 ID（如本地模型路径）。
    """
    from orchestrator.model_config import get_model_config
    from orchestrator.credential_store import get_credential_store

    provider_id = payload.provider_id
    mc = get_model_config()
    ok = mc.remove_provider(provider_id)
    if not ok:
        raise HTTPException(404, f"Provider not found: {provider_id}")
    # 同时删除凭证
    store = get_credential_store()
    store.delete(provider_id)
    logger.info("Provider '%s' fully removed (config + credential)", provider_id)
    return {"status": "deleted", "provider_id": provider_id}


@app.put("/api/runtime/providers/{provider_id}")
async def update_runtime_provider(provider_id: str, payload: UpdateProviderPayload):
    """更新 provider 的 runtime 配置（base_url, protocol, auth_type）。"""
    from orchestrator.model_config import get_model_config
    mc = get_model_config()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "至少需要提供一个字段")
    mc.update_provider(provider_id, updates)
    return {"status": "updated", "provider_id": provider_id}


@app.put("/api/runtime/default-model")
async def set_default_model(payload: SetModelPayload):
    """设置全局默认模型。"""
    from orchestrator.model_config import get_model_config
    mc = get_model_config()
    mc.set_default_model(payload.provider_id, payload.model_id)
    return {"status": "saved", "provider_id": payload.provider_id, "model_id": payload.model_id}


@app.put("/api/runtime/manager-model")
async def set_manager_model(payload: SetModelPayload):
    """设置 Manager 模型。"""
    from orchestrator.model_config import get_model_config
    mc = get_model_config()
    mc.set_manager_model(payload.provider_id, payload.model_id)
    return {"status": "saved", "provider_id": payload.provider_id, "model_id": payload.model_id}


class FallbackChainsPayload(BaseModel):
    """fallback_chains 批量更新 payload。

    chains: {primary_provider_id: [entry, ...]}
        entry 为 str 仅指定 provider，或 dict {provider, model} 同时指定 model。
        空 dict 表示清空全部 fallback 链（fail-loud：用户主动清空 = 显式声明）。
    """

    chains: dict[str, list[Any]]


@app.put("/api/runtime/fallback-chains")
async def set_fallback_chains(payload: FallbackChainsPayload):
    """批量更新 fallback_chains 字段，写回 config/models.yaml。

    失败模式（fail-loud）：
    - chains 非 dict → 400
    - 单条 entry 结构不合法 → 400（ModelConfig.set_fallback_chains 内 ValueError）
    - 写入 yaml 异常 → 500
    """
    from orchestrator.model_config import get_model_config

    if not isinstance(payload.chains, dict):
        raise HTTPException(400, "chains must be dict[provider_id, list[entry]]")

    mc = get_model_config()
    try:
        mc.set_fallback_chains(payload.chains)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("Failed to write fallback_chains")
        raise HTTPException(500, f"failed to save fallback_chains: {e}")

    # 主动失效 FallbackChain 单例，让下次 get_fallback_chain() 重新从 mc 加载
    # （与现有 _fallback_chain 模块级单例保持一致，下次访问自然重建）
    from orchestrator import provider_health as ph_module
    ph_module._fallback_chain = None

    # 回读最新值（用归一化后的 [{provider, model?}, ...] 形状给前端）
    from orchestrator.provider_health import get_fallback_chain
    fc = get_fallback_chain()
    normalized_chains: dict[str, list[dict[str, str | None]]] = {}
    for pid in mc.config.get("fallback_chains", {}).keys():
        normalized_chains[pid] = fc.get_chain(pid)

    return {
        "status": "saved",
        "chains": normalized_chains,
        "raw_chains": mc.config.get("fallback_chains", {}),
    }


@app.post("/api/runtime/providers")
async def create_provider(payload: CreateProviderPayload):
    """创建自定义供应商（如本地 Ollama）。"""
    from orchestrator.model_config import get_model_config
    mc = get_model_config()
    mc.add_provider(
        provider_id=payload.provider_id,
        base_url=payload.base_url,
        protocol=payload.protocol,
        auth_type=payload.auth_type,
        api_key_env=payload.api_key_env,
    )
    return {"status": "created", "provider_id": payload.provider_id}


# ── Onboarding + Manager 默认工作区（v2 新增） ─────────────────────
#
# 首次使用引导：用户必须完成默认工作区授权后才能开始对话。
# manager 默认工作区存 system_settings 表，对齐 manager-model 先例。

class OnboardingStatusPayload(BaseModel):
    """完成引导请求体。"""
    manager_default_workspace_id: str


class OnboardingCreateDefaultPayload(BaseModel):
    """首次引导创建默认工作区请求体（只传源路径，其余自动生成）。"""
    source_path: str


# 默认工作区子目录结构
_DEFAULT_WORKSPACE_SUBDIRS = ("manager-agent", "sessions", "workspace")
_AGENTS_MD_TEMPLATE = """# Manager 个人设定

> 此文件由 AgentOps Onboarding 自动生成，存放用户个人设定与偏好。
> Manager agent 在每次对话时会读取本文件作为个性化指令。

## 工作偏好

<!-- 例如：回答语言、代码风格、回复长度等 -->

## 常用项目

<!-- 例如：项目路径、技术栈、常用命令 -->

## 其他设定

<!-- 自由填写 -->
"""


@app.get("/api/runtime/onboarding")
async def get_onboarding_status():
    """获取引导状态：是否已完成 + manager 默认工作区。"""
    if not _event_store:
        return {"onboarded": False, "manager_default_workspace_id": None}
    onboarded = await _event_store.get_setting("onboarding_completed", "0")
    ws_id = await _event_store.get_setting("manager_default_workspace_id")
    return {
        "onboarded": onboarded == "1",
        "manager_default_workspace_id": ws_id,
    }


@app.post("/api/runtime/onboarding/create-default")
async def create_default_workspace(payload: OnboardingCreateDefaultPayload):
    """首次引导：只传源路径，自动生成 display_name/description/mode=bind_mount，
    在源路径下建立规范子目录结构（manager-agent/sessions/workspace）+ AGENTS.md，
    创建授权工作区记录并绑定为 manager 默认工作区。

    一次性完成"建目录 + 授权 + 绑定 + 标记 onboarded"，前端无需多次调用。
    """
    if not _event_store:
        raise HTTPException(500, "event store not initialized")
    from pathlib import Path
    src = Path(payload.source_path).resolve()
    if not src.exists():
        raise HTTPException(400, f"路径不存在: {src}")
    if not src.is_dir():
        raise HTTPException(400, f"路径不是目录: {src}")

    # 末级目录名作为 display_name（无需用户手填）
    display_name = src.name or str(src)
    description = "Manager 默认工作区 — 管理会话记录、任务产物与个人设定（系统引导自动创建）"

    # 在源路径下建规范子目录 + AGENTS.md
    for sub in _DEFAULT_WORKSPACE_SUBDIRS:
        (src / sub).mkdir(parents=True, exist_ok=True)
    agents_md = src / "manager-agent" / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(_AGENTS_MD_TEMPLATE, encoding="utf-8")

    workspace_id = str(uuid.uuid4())
    ws = await _event_store.create_authorized_workspace(
        workspace_id=workspace_id,
        display_name=display_name,
        description=description,
        mode="bind_mount",
        source_path=str(src),
        permissions="read_write_exec",
    )
    await _event_store.set_setting("manager_default_workspace_id", workspace_id)
    await _event_store.set_setting("onboarding_completed", "1")
    return {
        "status": "completed",
        "workspace": ws,
        "manager_default_workspace_id": workspace_id,
        "subdirs_created": list(_DEFAULT_WORKSPACE_SUBDIRS),
    }


@app.get("/api/runtime/browse-dirs")
async def browse_dirs(path: str | None = None):
    """目录浏览器：返回指定路径下的子目录列表（供前端目录选择器使用）。

    参数: path（查询参数，默认返回系统盘根/用户主目录）
    返回: {current, parent, entries: [{name, path, is_dir}]}
    - 仅返回目录（非文件），按名称排序
    - 父目录链接用于返回上一级
    - 异常路径返回 400
    """
    import os
    from pathlib import Path

    if path:
        target = Path(path).resolve()
    else:
        # 默认从用户主目录开始
        target = Path.home()

    if not target.exists():
        raise HTTPException(400, f"路径不存在: {target}")
    if not target.is_dir():
        raise HTTPException(400, f"路径不是目录: {target}")

    entries = []
    try:
        for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if item.is_dir():
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "is_dir": True,
                })
    except PermissionError:
        # 无权限列目录，返回空列表
        pass

    parent = str(target.parent) if target.parent != target else None

    # Windows 盘符列表（用于切换盘符）
    drives = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)

    return {
        "current": str(target),
        "parent": parent,
        "entries": entries,
        "drives": drives,
    }


# ── 原生文件夹选择对话框（v0.18+ 替代 webkitdirectory 的方案）────────────
#
# 为什么需要这个端点：
#   浏览器 <input webkitdirectory> 只能拿到所选目录的"相对路径片段"（如 AgentOps），
#   无法拿到 host 上的绝对路径（如 E:\Project\AgentOps），且弹出的是文件选择
#   对话框（带"上传"按钮），不是真正的文件夹选择器。
#   对应 deepseek-harness 的 `directory-picker-native` host capability
#   （其 windows 实现也是 IFileOpenDialog COM — 见 packages/host/directory-picker-native/src/win32-dialog.ts）。
#
# 工作流：
#   前端「选择文件夹」按钮 → POST /api/system/pick-folder → host 弹出原生对话框
#   → 用户选目录 → host 返回绝对路径 → 前端拿绝对路径调 createWorkspace
#
# 注意：对话框在 host 屏幕弹出（不是浏览器屏幕），需要后端运行在用户本地机器上。
# 远程部署场景应改用 /api/runtime/browse-dirs 的 in-app 目录浏览器。
class PickFolderPayload(BaseModel):
    """原生文件夹选择请求体。"""
    initial_dir: str | None = None   # 对话框初始打开的目录（可选）


@app.post("/api/system/pick-folder")
async def pick_folder_endpoint(payload: PickFolderPayload):
    """弹出 host 端原生文件夹选择对话框，返回绝对路径。

    返回:
        {cancelled: false, path: "C:\\Users\\..."}  成功
        {cancelled: true, path: null}                 用户取消
        {cancelled: false, path: null, error: "..."}  平台不支持 / 子进程失败 / 超时
    """
    from api.folder_picker import pick_folder_async, platform_supports_native_picker

    if not platform_supports_native_picker():
        return {
            "cancelled": False,
            "path": None,
            "error": "native folder picker not supported on this platform; "
                     "use the in-app directory browser instead",
        }

    result = await pick_folder_async(payload.initial_dir)
    return result.to_dict()


@app.get("/api/system/native-picker-supported")
async def native_picker_supported():
    """探测 host 平台是否支持原生文件夹选择对话框（前端用于按需显示按钮）。"""
    from api.folder_picker import platform_supports_native_picker
    return {"supported": platform_supports_native_picker()}


@app.post("/api/runtime/onboarding/complete")
async def complete_onboarding(payload: OnboardingStatusPayload):
    """完成引导：标记 onboarded + 绑定 manager 默认工作区。"""
    if not _event_store:
        raise HTTPException(500, "event store not initialized")
    ws = await _event_store.get_authorized_workspace(payload.manager_default_workspace_id)
    if not ws:
        raise HTTPException(404, f"workspace not found: {payload.manager_default_workspace_id}")
    if not ws.get("enabled"):
        raise HTTPException(400, "workspace is disabled")
    await _event_store.set_setting("manager_default_workspace_id", payload.manager_default_workspace_id)
    await _event_store.set_setting("onboarding_completed", "1")
    return {
        "status": "completed",
        "manager_default_workspace_id": payload.manager_default_workspace_id,
    }


@app.get("/api/runtime/manager-workspace")
async def get_manager_workspace():
    """获取 manager 默认工作区详情。"""
    if not _event_store:
        return {"workspace": None}
    ws_id = await _event_store.get_setting("manager_default_workspace_id")
    if not ws_id:
        return {"workspace": None}
    ws = await _event_store.get_authorized_workspace(ws_id)
    return {"workspace": ws}


@app.put("/api/runtime/manager-workspace")
async def set_manager_workspace(payload: OnboardingStatusPayload):
    """设置/更换 manager 默认工作区。"""
    if not _event_store:
        raise HTTPException(500, "event store not initialized")
    ws = await _event_store.get_authorized_workspace(payload.manager_default_workspace_id)
    if not ws:
        raise HTTPException(404, f"workspace not found: {payload.manager_default_workspace_id}")
    if not ws.get("enabled"):
        raise HTTPException(400, "workspace is disabled")
    await _event_store.set_setting("manager_default_workspace_id", payload.manager_default_workspace_id)
    return {
        "status": "saved",
        "manager_default_workspace_id": payload.manager_default_workspace_id,
    }


# ── Authorized Workspaces CRUD（v2 P0.18.1 新增） ────────────────────
#
# 用户在 Settings → Workspaces 添加"已授权目录"，每个授权包含 path/mode/permissions。
# manager agent 启动对话时按 session.workspace_id 关联工作区，run 启动时按 mode 落地 sandbox。

class CreateWorkspacePayload(BaseModel):
    """新增授权工作区请求体。"""
    display_name: str
    mode: str                                    # local_copy / bind_mount / git_clone / isolated
    permissions: str                             # read_only / read_write / read_write_exec
    description: str | None = None
    source_path: str | None = None               # local_copy/bind_mount 必填
    git_url: str | None = None                   # git_clone 必填
    git_branch: str | None = None
    extra: dict | None = None


class UpdateWorkspacePayload(BaseModel):
    """更新授权工作区字段（全部可选）。"""
    display_name: str | None = None
    description: str | None = None
    permissions: str | None = None
    enabled: bool | None = None


def _validate_workspace_payload(payload: CreateWorkspacePayload) -> None:
    """校验 mode 与路径字段一致性（DB CHECK 约束的预校验，给前端更友好的错误）。"""
    valid_modes = {"local_copy", "bind_mount", "git_clone", "isolated"}
    valid_perms = {"read_only", "read_write", "read_write_exec"}
    if payload.mode not in valid_modes:
        raise HTTPException(400, f"invalid mode: {payload.mode}, must be one of {valid_modes}")
    if payload.permissions not in valid_perms:
        raise HTTPException(400, f"invalid permissions: {payload.permissions}, must be one of {valid_perms}")
    if payload.mode in ("local_copy", "bind_mount") and not payload.source_path:
        raise HTTPException(400, f"mode={payload.mode} requires source_path")
    if payload.mode == "git_clone" and not payload.git_url:
        raise HTTPException(400, "mode=git_clone requires git_url")


@app.get("/api/workspaces")
async def list_workspaces(include_disabled: bool = False):
    """列出所有授权工作区。include_disabled=true 含已取消授权的（管理视图）。"""
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    rows = await _event_store.list_authorized_workspaces(include_disabled=include_disabled)
    return {"workspaces": rows, "count": len(rows)}


@app.post("/api/workspaces")
async def create_workspace_endpoint(payload: CreateWorkspacePayload):
    """新增授权工作区。返回新建记录。

    P3（deepseek-harness 对齐）：source_path 以 canonical 形式做幂等去重——
    realpath（resolve 解析 symlink/../尾部斜杠）+ 大小写归一（Windows 大小写
    不敏感盘）后与现有注册比对，同一路径的不同拼写幂等返回已有记录，
    不重复注册（deepseek WorkspaceRegistry.create 语义）。
    """
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    _validate_workspace_payload(payload)
    # source_path 规范化（resolve symlink + .. + .）
    source_path = None
    if payload.source_path:
        from pathlib import Path
        source_path = str(Path(payload.source_path).resolve())

    # canonical 幂等去重：resolve 已解析 symlink，normcase 统一大小写（Windows）
    if source_path:
        import os as _os
        canonical = _os.path.normcase(source_path)
        for existing in await _event_store.list_authorized_workspaces():
            existing_path = existing.get("source_path")
            if existing_path and _os.path.normcase(str(existing_path)) == canonical:
                return {
                    "workspace": existing,
                    "status": "exists",
                    "note": f"该目录已注册为工作区（canonical path 幂等去重）",
                }

    workspace_id = str(uuid.uuid4())
    ws = await _event_store.create_authorized_workspace(
        workspace_id=workspace_id,
        display_name=payload.display_name,
        mode=payload.mode,
        permissions=payload.permissions,
        description=payload.description,
        source_path=source_path,
        git_url=payload.git_url,
        git_branch=payload.git_branch,
        extra=payload.extra,
    )
    return {"workspace": ws, "status": "created"}


@app.get("/api/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str):
    """获取单个授权工作区详情。"""
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    ws = await _event_store.get_authorized_workspace(workspace_id)
    if not ws:
        raise HTTPException(404, f"workspace {workspace_id} not found")
    return {"workspace": ws}


@app.patch("/api/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, payload: UpdateWorkspacePayload):
    """更新授权工作区字段。enabled=false 时填 deauthorized_at（soft delete）。"""
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    # 校验 permissions 取值（若提供）
    if payload.permissions is not None:
        valid_perms = {"read_only", "read_write", "read_write_exec"}
        if payload.permissions not in valid_perms:
            raise HTTPException(400, f"invalid permissions: {payload.permissions}")
    updated = await _event_store.update_authorized_workspace(
        workspace_id,
        display_name=payload.display_name,
        description=payload.description,
        permissions=payload.permissions,
        enabled=payload.enabled,
    )
    if not updated:
        raise HTTPException(404, f"workspace {workspace_id} not found")
    return {"workspace": updated, "status": "updated"}


@app.delete("/api/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str):
    """soft delete：enabled=0 + deauthorized_at=now。非 RESTful，但前端调用更直观。"""
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    found = await _event_store.delete_authorized_workspace(workspace_id)
    if not found:
        raise HTTPException(404, f"workspace {workspace_id} not found")
    return {"status": "deauthorized", "workspace_id": workspace_id}


@app.post("/api/workspaces/{workspace_id}/test")
async def test_workspace_access(workspace_id: str):
    """测试 workspace 访问：返回 {exists, readable, writable, execuable}。

    仅对 local_copy / bind_mount 模式有意义（git_clone/isolated 返回 skipped=true）。
    """
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    ws = await _event_store.get_authorized_workspace(workspace_id)
    if not ws:
        raise HTTPException(404, f"workspace {workspace_id} not found")
    if ws["mode"] not in ("local_copy", "bind_mount") or not ws.get("source_path"):
        return {"exists": False, "readable": False, "writable": False,
                "execuable": False, "skipped": True,
                "reason": f"mode={ws['mode']} does not require host path access test"}
    from pathlib import Path
    p = Path(ws["source_path"])
    return {
        "exists": p.exists(),
        "readable": os.access(str(p), os.R_OK) if p.exists() else False,
        "writable": os.access(str(p), os.W_OK) if p.exists() else False,
        "execuable": os.access(str(p), os.X_OK) if p.exists() else False,
        "skipped": False,
        "source_path": ws["source_path"],
    }


# ── Run Workspace prepare（v2 P0.18.1 新增） ─────────────────────────
#
# 为指定 run 准备 sandbox。本地 mode（local_copy/git_clone/isolated）创建实际目录；
# bind_mount 仅校验 + 返回 source_path。实际 cp / git clone 由 P0.18.2 workspace_paths.py 实现。
# 本期（P0.18.1）只实现 mkdir + record_run_workspace_meta，cp/git clone 留 P0.18.2。

class PrepareWorkspacePayload(BaseModel):
    workspace_id: str


@app.post("/api/runs/{run_id}/workspace/prepare")
async def prepare_run_workspace(run_id: str, payload: PrepareWorkspacePayload):
    """为指定 run 准备 sandbox。

    返回: {workspace_root, workspace_mode, authorized_workspace_id}
    本期（P0.18.1）：仅 mkdir sandbox + 写 run_workspace_meta，cp/git clone 留 P0.18.2。
    """
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    ws = await _event_store.get_authorized_workspace(payload.workspace_id)
    if not ws:
        raise HTTPException(404, f"workspace {payload.workspace_id} not found")
    if not ws["enabled"]:
        raise HTTPException(403, f"workspace {payload.workspace_id} is disabled (deauthorized)")

    # sandbox 路径：${AGENTOPS_HOME}/workspaces/${ws_id}/${run_id}/
    # ${AGENTOPS_HOME} 默认 ~/.agentops
    agentops_home = os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops"))
    sandbox_root = os.path.join(agentops_home, "workspaces", ws["workspace_id"], run_id)
    os.makedirs(sandbox_root, exist_ok=True)

    # v2 P0.18.1: 写 run_workspace_meta（含 authorized_workspace_id 关联）
    # 本期 cleanup_at=None（不立即标记清理，留 P0.18.5 patroller 集成时填）
    await _event_store.record_run_workspace_meta(
        run_id=run_id,
        workflow_id="",  # 由 engine.py 在 run init 时更新
        workspace_root=sandbox_root,
        absolute_root=os.path.abspath(sandbox_root),
        mode=0o755 if ws["permissions"] != "read_only" else 0o555,
        authorized_workspace_id=ws["workspace_id"],
        cleanup_at=None,
    )

    # touch workspace（更新 last_used_at + usage_count）
    await _event_store.touch_authorized_workspace(payload.workspace_id)

    return {
        "workspace_root": sandbox_root,
        "workspace_mode": ws["mode"],
        "authorized_workspace_id": ws["workspace_id"],
        "permissions": ws["permissions"],
    }


# ── Runtime workspace 查询（v2 P0.18.1 新增） ────────────────────────

@app.get("/api/runtime/workspace")
async def get_current_workspace(session_id: str | None = None):
    """获取当前 session 的 workspace（前端 status bar 显示）。

    参数: session_id（查询参数）
    返回: {workspace_id, workspace} 或 {workspace_id: null}（通用对话）

    无 session_id 时回退到 manager 默认工作区（引导完成后首次进入工作台，
    WorkspacePicker 也能显示默认工作区的末级目录名，而不是"选择工作区"）。
    """
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    if not session_id:
        default_ws_id = await _event_store.get_setting("manager_default_workspace_id")
        if default_ws_id:
            default_ws = await _event_store.get_authorized_workspace(default_ws_id)
            if default_ws and default_ws.get("enabled"):
                return {
                    "workspace_id": default_ws_id, "workspace": default_ws,
                    "is_default": True,
                    "permission_level": default_ws.get("permissions"),
                }
        return {"workspace_id": None, "workspace": None, "permission_level": None}
    session = await _event_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    # 会话级权限级别（与 workspace 解耦，优先取 session 记录）
    sess_perm_level = session.get("permission_level")
    workspace_id = session.get("workspace_id")
    if not workspace_id:
        # session 未绑定工作区 → 回退到 manager 默认工作区（引导完成后
        # 历史会话也能在 WorkspacePicker 上显示默认工作区末级目录名）
        default_ws_id = await _event_store.get_setting("manager_default_workspace_id")
        if default_ws_id:
            default_ws = await _event_store.get_authorized_workspace(default_ws_id)
            if default_ws and default_ws.get("enabled"):
                return {
                    "workspace_id": default_ws_id, "workspace": default_ws,
                    "is_default": True,
                    "permission_level": sess_perm_level or default_ws.get("permissions"),
                }
        return {"workspace_id": None, "workspace": None, "permission_level": sess_perm_level}
    ws = await _event_store.get_authorized_workspace(workspace_id)
    return {
        "workspace_id": workspace_id, "workspace": ws,
        "permission_level": sess_perm_level or (ws.get("permissions") if ws else None),
    }


@app.get("/api/runtime/workspaces")
async def list_runtime_workspaces():
    """列出所有 enabled=1 workspace 简要（前端 status bar dropdown 用）。

    与 /api/workspaces 区别：此端点不含 disabled + 只返回简要字段（不含 extra/description）。
    """
    if _event_store is None:
        raise HTTPException(500, "event store not initialized")
    rows = await _event_store.list_authorized_workspaces(include_disabled=False)
    simplified = [
        {
            "workspace_id": r["workspace_id"],
            "display_name": r["display_name"],
            "mode": r["mode"],
            "permissions": r["permissions"],
            "source_path": r.get("source_path"),
            "last_used_at": r.get("last_used_at"),
        }
        for r in rows
    ]
    return {"workspaces": simplified, "count": len(simplified)}


@app.post("/api/runtime/workspaces/cleanup")
async def cleanup_workspaces_now():
    """P0.18.11: 手动触发 sandbox 延迟清理（供前端 Settings → 立即清理入口调用）。

    等价于 Patroller.cleanup_sandboxes_once() — 扫 audit.db 的 run_workspace_meta 表，
    删除 cleanup_at < now() 的 sandbox 物理目录（仅 local_copy/git_clone 模式），
    bind_mount/isolated 仅标记 cleanup_status='deleted' 不删文件。

    Returns:
        {"scanned": N, "deleted": M, "failed": K}
    """
    if _patroller is None:
        raise HTTPException(503, "Patroller not initialized")
    result = await _patroller.cleanup_sandboxes_once()
    return {"status": "ok", **result}


# ── Worker WS endpoint（v2 P0.18.6 新增） ─────────────────────────────
#
# worker → manager WS 端点。worker 容器启动后 connect 此端点注册。
# provisioner 通过 WorkerRegistry.wait_registered 等 worker 上线。

@app.websocket("/ws/projects/{project_id}/workers/{worker_id}")
async def worker_ws_endpoint(websocket: WebSocket, project_id: str, worker_id: str):
    """worker → manager WS 端点。

    握手时校验 worker_token（JWT，5min TTL）。
    注册到 WorkerRegistry，触发 provisioner 的 wait_registered。
    双向消息：task / task_progress / task_complete / shutdown / credential。
    """
    from orchestrator.worker_token import verify_worker_token, get_worker_registry

    token = websocket.query_params.get("token", "")
    if not verify_worker_token(worker_id, token):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    registry = get_worker_registry()
    await registry.register(worker_id, websocket)

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type", "")
            if msg_type == "task_progress":
                # 转发到 SSE broadcast（P0.18.5 engine 集成时实现）
                logger.debug("worker %s progress: %s", worker_id, msg.get("payload", {}))
            elif msg_type == "task_complete":
                logger.info("worker %s task complete: %s", worker_id, msg.get("payload", {}))
                break
            elif msg_type == "heartbeat":
                # worker 心跳，可记录到 audit
                pass
            else:
                logger.debug("worker %s message: %s", worker_id, msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("worker %s WS error: %s", worker_id, e)
    finally:
        await registry.unregister(worker_id)


@app.get("/api/runtime/workers")
async def list_runtime_workers():
    """列出已注册的 worker（v2 P0.18.6 新增）。"""
    from orchestrator.worker_token import get_worker_registry
    registry = get_worker_registry()
    worker_ids = registry.list_active_workers()
    return {"workers": worker_ids, "count": len(worker_ids)}


# ── Harness list ─────────────────────────────────────────────────────

@app.get("/api/agent/harnesses")
async def list_harnesses():
    from harness import HarnessRegistry
    return {"harnesses": [h.value for h in HarnessRegistry.available()]}

# ── Workflow list ────────────────────────────────────────────────────

@app.get("/api/agent/workflows")
async def list_workflows():
    if _orchestrator is None:
        return {"workflows": []}
    return {
        "workflows": [
            {
                "workflow_id": wf_id,
                "name": wf.name,
                "description": wf.description,
                "version": wf.version,
                "nodes": len(wf.nodes),
                "node_ids": list(wf.nodes.keys()),
                "edges": [
                    {"source": dep, "target": nid}
                    for nid, node in wf.nodes.items()
                    for dep in node.after
                ],
                "widgets": len(wf.widgets),
            }
            for wf_id, wf in _orchestrator.workflows.items()
        ]
    }


@app.get("/api/agent/workflows/{workflow_id}")
async def get_workflow_detail(workflow_id: str):
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    wf = _orchestrator.workflows.get(workflow_id)
    if wf is None:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")

    wf_path = PROJECT_ROOT / "workflows" / f"{workflow_id}.yaml"
    yaml_source = wf_path.read_text(encoding="utf-8") if wf_path.exists() else ""

    # 完整节点详情（供可视化编辑器使用）
    nodes = [
        {
            "id": nid,
            "name": node.name,
            "type": node.type.value,
            "agent": node.agent,
            # inline_agent.harness 优先于顶层 harness（与 loader 语义一致）
            "harness": (node.inline_agent.harness.value if node.inline_agent else node.harness.value),
            "inline_agent": (
                {
                    "harness": node.inline_agent.harness.value,
                    "model": node.inline_agent.model,
                    "domain": node.inline_agent.domain,
                    "role_prompt": node.inline_agent.role_prompt,
                    "allowed_tools": node.inline_agent.allowed_tools,
                    "denied_tools": node.inline_agent.denied_tools,
                    "timeout_seconds": node.inline_agent.timeout_seconds,
                }
                if node.inline_agent else None
            ),
            "after": node.after,
            "inputs": node.inputs,
            "outputs": {
                port: (route.to if isinstance(route.to, list) else route.to)
                for port, route in node.outputs.items()
            },
            "model": node.model,
            "domain": node.domain,
            "business_role": node.business_role,
            "role_prompt": node.role_prompt,
            "skip_if": node.skip_if,
            "timeout_seconds": node.config.get("timeout_seconds") if node.config else None,
            # 修复（D-XXX）：编辑器保存工作流报"type=command 节点必须有 command_config"
            # 根因：get_workflow_detail 没把 command_config / await_command_config /
            # while_config 返回给前端，前端 rawFields 收集不到，序列化丢失。
            # 修复：用 dataclasses.asdict 转为 dict 返回（None 时也返回，前端按需透传）
            "command_config": asdict(node.command_config) if node.command_config else None,
            "await_command_config": asdict(node.await_command_config) if node.await_command_config else None,
            "while_config": asdict(node.while_config) if node.while_config else None,
        }
        for nid, node in wf.nodes.items()
    ]
    edges = [
        {"source": dep, "target": nid}
        for nid, node in wf.nodes.items()
        for dep in node.after
    ]
    # 完整 widget 详情（含 emit_on + props）
    widgets = [
        {
            "id": w.id,
            "type": w.type,
            "title": w.title,
            "emit_on_node": w.emit_on_node,
            "emit_on_event": w.emit_on_event,
            "props": w.props,
        }
        for w in wf.widgets
    ]

    # 原始解析 YAML dict（保留 workspace/permissions 等编辑器未管理字段）
    raw_parsed: dict = {}
    if yaml_source:
        try:
            raw_parsed = yaml.safe_load(yaml_source) or {}
        except Exception:
            raw_parsed = {}

    return {
        "workflow_id": wf.workflow_id,
        "name": wf.name,
        "description": wf.description,
        "version": wf.version,
        "inputs": wf.inputs,
        "nodes": nodes,
        "edges": edges,
        "widgets": widgets,
        "yaml_source": yaml_source,
        "raw": raw_parsed,
    }


@app.post("/api/agent/workflows")
async def create_workflow(payload: WorkflowPayload):
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")

    try:
        wf = load_workflow_text(payload.yaml_content)
        validate_workflow(wf)
    except WorkflowValidationError as e:
        raise HTTPException(400, f"Validation failed: {'; '.join(e.errors)}")
    except WorkflowLoadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Parse error: {e}")

    if wf.workflow_id in _orchestrator.workflows:
        raise HTTPException(409, f"Workflow already exists: {wf.workflow_id}")

    wf_path = PROJECT_ROOT / "workflows" / f"{wf.workflow_id}.yaml"
    wf_path.write_text(payload.yaml_content, encoding="utf-8")
    _orchestrator.register_workflow(wf)

    return {"workflow_id": wf.workflow_id, "name": wf.name, "status": "created"}


@app.put("/api/agent/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, payload: WorkflowPayload):
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    if workflow_id not in _orchestrator.workflows:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")

    try:
        wf = load_workflow_text(payload.yaml_content)
        validate_workflow(wf)
    except WorkflowValidationError as e:
        raise HTTPException(400, f"Validation failed: {'; '.join(e.errors)}")
    except WorkflowLoadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Parse error: {e}")

    # If workflow_id changed, remove old file
    if wf.workflow_id != workflow_id:
        old_path = PROJECT_ROOT / "workflows" / f"{workflow_id}.yaml"
        if old_path.exists():
            old_path.unlink()
        del _orchestrator.workflows[workflow_id]

    wf_path = PROJECT_ROOT / "workflows" / f"{wf.workflow_id}.yaml"
    wf_path.write_text(payload.yaml_content, encoding="utf-8")
    _orchestrator.register_workflow(wf)

    return {"workflow_id": wf.workflow_id, "name": wf.name, "status": "updated"}


@app.delete("/api/agent/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str):
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    if workflow_id not in _orchestrator.workflows:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")

    wf_path = PROJECT_ROOT / "workflows" / f"{workflow_id}.yaml"
    if wf_path.exists():
        wf_path.unlink()

    del _orchestrator.workflows[workflow_id]
    return {"deleted": workflow_id}


@app.post("/api/agent/run", response_model=RunResponse)
async def start_run(payload: RunPayload):
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")

    # P1: 根据 run_mode 构造 RunRequest
    try:
        run_mode = RunMode(payload.run_mode)
    except ValueError:
        raise HTTPException(400, f"Invalid run_mode: {payload.run_mode}")

    # templated/hybrid 模式校验 workflow_id
    if run_mode in (RunMode.TEMPLATED, RunMode.HYBRID):
        if not payload.workflow_id or payload.workflow_id not in _orchestrator.workflows:
            raise HTTPException(404, f"Workflow not found: {payload.workflow_id}")
    # conversational/task 模式校验 agent_id
    if run_mode in (RunMode.CONVERSATIONAL, RunMode.TASK):
        if not payload.agent_id:
            raise HTTPException(400, "conversational/task 模式需要 agent_id")

    # P0.18.10: tier 兼容性校验（conversational/task 模式有单一 agent_id + workspace_id）
    if (
        run_mode in (RunMode.CONVERSATIONAL, RunMode.TASK)
        and payload.workspace_id
        and _event_store
        and payload.agent_id
    ):
        try:
            from orchestrator.config_loader import get_system_config
            _cfg = get_system_config()
            _agent_def = _cfg.agents.get(payload.agent_id)
        except Exception:
            _agent_def = None
        if _agent_def:
            _ws_row = await _event_store.get_authorized_workspace(payload.workspace_id)
            if _ws_row:
                from orchestrator.workspace_paths import WorkspaceInfo, tier_compatible
                _ws_info = WorkspaceInfo.from_row(_ws_row)
                _agent_tier = getattr(_agent_def, "tier", "T2")
                if not tier_compatible(_ws_info.tier, _agent_tier):
                    _PERMS_TIER = {"read_only": "T1", "read_write": "T2", "read_write_exec": "T3"}
                    _needed = next(
                        (p for p, t in _PERMS_TIER.items() if t == _agent_tier),
                        "read_write_exec",
                    )
                    raise HTTPException(409, (
                        f"Agent '{payload.agent_id}' tier={_agent_tier} 超过 workspace "
                        f"'{_ws_info.display_name}' permissions tier={_ws_info.tier}（{_ws_info.permissions}）；"
                        f"请将该 workspace 权限升级到 {_needed}"
                    ))

    # v3 修复：提前生成 session_id，传给 RunRequest.session_id，
    # 让 orchestrator.run() 在启动 engine 前先写 runs 表（避免 provision_subagent 的 FK 竞态）。
    # 如果调用方传了 session_id（如前端已有 session 想关联 run），优先使用传入的，
    # 这样 bridge_run_events 能把 run 事件转发到正确的 session SSE 通道。
    session_id = payload.session_id or f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{_uuid4().hex[:6]}"

    handle = await _orchestrator.run(RunRequest(
        workflow_id=payload.workflow_id,
        inputs=payload.inputs,
        run_mode=run_mode,
        agent_id=payload.agent_id,
        initial_message=payload.initial_message,
        workspace_id=payload.workspace_id,  # P0.18.7b: 传 workspace_id 触发 provisioner 路径
        session_id=session_id,
    ))

    try:
        # 初始化 EventStore 记录（v3: session + run 两层）
        if _event_store:
            await _event_store.create_session(
                session_id=session_id,
                agent_id=payload.agent_id or "",
                workspace_id=payload.workspace_id,  # P0.18.7b: session-workspace 关联
            )
            await _event_store.init_run(
                run_id=handle.run_id,
                session_id=session_id,
                workflow_id=payload.workflow_id,
                run_mode=payload.run_mode,
                agent_id=payload.agent_id,
                initial_message=payload.initial_message or None,
                inputs=payload.inputs if run_mode in (RunMode.TEMPLATED, RunMode.HYBRID)
                       else {"initial_message": payload.initial_message},
            )

        # Set up event queue for this run
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        _event_streams[handle.run_id] = queue

        # Track emitted sequence numbers to dedupe (engine + sink may both write)
        last_seq: dict[str, int] = {}

        # Bridge: forward events from orchestrator's stream to this queue (deduped)
        async def _bridge():
            await bridge_run_events(handle.run_id, queue, last_seq)

        asyncio.create_task(_bridge())

        return RunResponse(
            run_id=handle.run_id,
            stream_url=f"/api/agent/runs/{handle.run_id}/events",
        )
    except Exception:
        try:
            if _event_store:
                await _event_store.delete_session(session_id)
        except Exception as cleanup_err:
            logger.exception("start_run cleanup delete_session 失败: %s", cleanup_err)
        _event_streams.pop(handle.run_id, None)
        raise


def _dag_event_to_tip(ev: "DagEvent") -> dict[str, Any] | None:
    """把关键 DAG 事件转成监控 tip（供 Agent 卡片旁气泡展示执行状态）。

    只桥接任务执行类事件（NODE_STARTED/NODE_PROGRESS/NODE_COMPLETED/NODE_FAILED），
    告警类（patrol_alert/quota_warning）由 emit_alert 工具单独推送，不在这里生成。

    Returns:
        tip dict（含 tip_id/tip_type/severity/title/message/agent_id/run_id/emitted_at）或 None
    """
    from datetime import datetime, timezone

    node_id = ev.node_id or ""
    payload = ev.payload or {}
    agent_id = str(payload.get("agent", "") or "")
    tip_id = f"dag_{ev.run_id}_{ev.sequence}"

    if ev.type == DagEventType.NODE_STARTED:
        return {
            "tip_id": tip_id,
            "tip_type": "task_started",
            "severity": "info",
            "title": f"开始执行 · {node_id}",
            "message": f"{agent_id} 开始处理节点 {node_id}" if agent_id else f"节点 {node_id} 开始",
            "agent_id": agent_id or None,
            "run_id": ev.run_id,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
    if ev.type == DagEventType.NODE_PROGRESS:
        text = str(payload.get("agent_text", "") or "").strip()
        if not text:
            return None  # 空文本不弹
        # 截断长文本，气泡只显示摘要
        snippet = text if len(text) <= 120 else text[:117] + "..."
        return {
            "tip_id": tip_id,
            "tip_type": "task_progress",
            "severity": "info",
            "title": f"思考中 · {node_id}",
            "message": snippet,
            "agent_id": agent_id or None,
            "run_id": ev.run_id,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
    if ev.type == DagEventType.NODE_COMPLETED:
        return {
            "tip_id": tip_id,
            "tip_type": "task_completed",
            "severity": "success",
            "title": f"节点完成 · {node_id}",
            "message": f"{agent_id} 完成 {node_id}" if agent_id else f"节点 {node_id} 完成",
            "agent_id": agent_id or None,
            "run_id": ev.run_id,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
    if ev.type == DagEventType.NODE_FAILED:
        err = str(payload.get("error", "") or "节点失败")[:160]
        return {
            "tip_id": tip_id,
            "tip_type": "task_failed",
            "severity": "error",
            "title": f"节点失败 · {node_id}",
            "message": err,
            "agent_id": agent_id or None,
            "run_id": ev.run_id,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
    return None


def _push_tip_to_global(tip: dict[str, Any]) -> None:
    """把 tip 写入 _global_alerts + _global_alert_queue + 广播到所有 tips-stream SSE 订阅者。"""
    logger.info("monitor tip 推送 tip_id=%s type=%s agent=%s title=%s", tip.get("tip_id"), tip.get("tip_type"), tip.get("agent_id"), tip.get("title"))
    _global_alerts.append(tip)
    if len(_global_alerts) > 100:
        _global_alerts.pop(0)
    try:
        _global_alert_queue.put_nowait(tip)
    except asyncio.QueueFull:
        logger.warning("monitor tip 队列已满，tip_id=%s 被丢弃", tip.get("tip_id"))
    # 广播到所有 tips-stream SSE 订阅者（与 emit-alert 端点一致）
    for sub in list(_tip_subscribers):
        try:
            sub.put_nowait(tip)
        except asyncio.QueueFull:
            logger.warning("monitor tips 订阅者队列已满，丢弃 tip_id=%s", tip.get("tip_id"))


async def bridge_run_events(run_id: str, queue: "asyncio.Queue", last_seq: dict[str, int]) -> None:
    """从 orchestrator.stream_events 拉事件，落到 audit.db + SSE 队列。

    模块级函数，start_run 和 trigger_workflow 工具共享同一份桥接逻辑。
    run 结束后调 finalize_run 落库最终状态 + 推 sentinel 让前端 SSE 关闭。
    同时把关键 DAG 事件（节点开始/进度/完成/失败）转成 tip，推到全局告警队列，
    让监控中心 Agent 卡片旁气泡能实时展示各智能体执行状态。

    P0.18 后修复：同时将 run 级事件转发到父 session 的 SSE 通道，
    让前端 SuperAgentPage 订阅 session SSE 时也能收到 widget.update / node.progress /
    report_surface_state 等事件。
    """
    # 查找 run 对应的 session_id，用于转发事件到 session SSE
    _bridge_session_id: str | None = None
    if _event_store:
        try:
            run_row = await _event_store.get_run(run_id)
            if run_row:
                _bridge_session_id = run_row.get("session_id")
        except Exception as e:
            logger.warning("bridge_run_events[%s] get_run 失败: %s", run_id[:12], e)
    _dbg_subscribed = list(_session_event_streams.keys()) if _bridge_session_id else []
    logger.info(
        "bridge_run_events[%s] start: session_id=%s, subscribers=%s",
        run_id[:12], _bridge_session_id, [s[:12] for s in _dbg_subscribed],
    )

    try:
        async for ev in _orchestrator.stream_events(run_id):
            if isinstance(ev, DagEvent):
                if _event_store:
                    await _event_store.append_run_event(run_id=ev.run_id or run_id, event_type=ev.type.value if hasattr(ev.type, "value") else str(ev.type), payload=ev.payload or {}, node_id=ev.node_id)
                seq = ev.sequence
                if seq <= last_seq.get(run_id, 0):
                    continue
                last_seq[run_id] = seq
                # 桥接 DAG 事件 → 监控 tip（Agent 卡片旁气泡）
                tip = _dag_event_to_tip(ev)
                if tip:
                    _push_tip_to_global(tip)
                # 转发到 session SSE 通道（让前端 SuperAgentPage 收到 run 级事件）
                if _bridge_session_id and _bridge_session_id in _session_event_streams:
                    sse_payload = {
                        "type": ev.type.value if hasattr(ev.type, "value") else str(ev.type),
                        "session_id": _bridge_session_id,
                        "run_id": ev.run_id or run_id,
                        "node_id": ev.node_id,
                        "payload": ev.payload,
                        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                        "sequence": ev.sequence,
                    }
                    if hasattr(ev, "surface_state") and ev.surface_state:
                        sse_payload["surface_state"] = ev.surface_state.to_payload()
                    for sq in list(_session_event_streams.get(_bridge_session_id, set())):
                        try:
                            sq.put_nowait(sse_payload)
                        except asyncio.QueueFull:
                            pass
            elif isinstance(ev, RawHarnessEvent):
                if _event_store:
                    await _event_store.append_raw_event(ev)
            await queue.put(ev)
    finally:
        if _event_store and _orchestrator:
            try:
                state = await _orchestrator.get_run(run_id)
                if state:
                    await _event_store.finalize_run(
                        run_id=run_id,
                        status=state.status.value,
                        finished_at=state.finished_at,
                        total_tokens_in=state.total_tokens_input,
                        total_tokens_out=state.total_tokens_output,
                        total_cost_usd=state.total_cost_usd,
                        error=state.error,
                        final_outputs=state.node_outputs,
                    )
            except Exception as e:
                logger.warning("finalize_run 失败: %s", e)
        await queue.put(None)


async def _event_bridge_for_trigger(run_id: str) -> None:
    """trigger_workflow 工具的 event bridge：分配独立 queue + 桥接 + 落库。

    与 start_run 的 _bridge 等价，但不需要返回 SSE 端点（工具调用方是 LLM，
    LLM 通过后续的 get_workflow_status 拿结果，前端 SSE 由用户自己看组件面板）。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _event_streams[run_id] = queue
    last_seq: dict[str, int] = {}
    await bridge_run_events(run_id, queue, last_seq)


@app.post("/api/agent/runs/{run_id}/resume", response_model=RunResponse)
async def resume_run(run_id: str, payload: ResumePayload):
    """从已有 run_id 断点恢复执行，支持指定节点重执行。

    利用 DagEngine.resume() 复用 run_id，workspace 下已有文件会被
    _try_restore_node 检测到自动跳过。指定 node_id 时会清除该节点及
    下游节点的已完成文件，强制重新执行。
    """
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")

    if payload.workflow_id not in _orchestrator.workflows:
        raise HTTPException(404, f"Workflow not found: {payload.workflow_id}")

    wf = _orchestrator.workflows[payload.workflow_id]

    # 如果指定了 node_id，需要清除指定节点的已完成文件
    # 默认连带清下游；only_node=True 时仅清当前节点（下游由 _try_restore_node 自动跳过）
    if payload.node_id:
        ws_root = getattr(wf, 'workspace_root', None)
        if ws_root:
            node_dir = Path(ws_root) / run_id
            if node_dir.exists():
                if payload.only_node:
                    target_nodes = [payload.node_id]
                else:
                    target_nodes = [payload.node_id] + _get_downstream_nodes(wf, payload.node_id)
                for nid in target_nodes:
                    for pattern in [f"{nid}*", f"*/{nid}*"]:
                        for f in node_dir.rglob(pattern):
                            f.unlink(missing_ok=True)
                            logger.info(
                                "清除节点 %s 输出文件: %s（only_node=%s）",
                                nid, f, payload.only_node,
                            )

    # 通过 orchestrator.resume() 恢复执行
    handle = await _orchestrator.resume(
        run_id=run_id,
        workflow_id=payload.workflow_id,
        inputs=payload.inputs,
    )

    # 初始化事件队列（复用 run_id）
    if run_id not in _event_streams:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        _event_streams[run_id] = queue
    else:
        # 已有队列则清空旧事件
        while not _event_streams[run_id].empty():
            _event_streams[run_id].get_nowait()

    last_seq: dict[str, int] = {"seq": 0}

    async def _resume_bridge():
        try:
            async for ev in _orchestrator.stream_events(run_id):
                if isinstance(ev, DagEvent):
                    if _event_store:
                        await _event_store.append_run_event(run_id=ev.run_id or run_id, event_type=ev.type.value if hasattr(ev.type, "value") else str(ev.type), payload=ev.payload or {}, node_id=ev.node_id)
                    seq = ev.sequence
                    if seq <= last_seq["seq"]:
                        continue
                    last_seq["seq"] = seq
                elif isinstance(ev, RawHarnessEvent):
                    if _event_store:
                        await _event_store.append_raw_event(ev)
                await _event_streams[run_id].put(ev)
        finally:
            if _event_store and _orchestrator:
                try:
                    state = await _orchestrator.get_run(run_id)
                    if state:
                        await _event_store.finalize_run(
                            run_id=run_id,
                            status=state.status.value,
                            finished_at=state.finished_at,
                            total_tokens_in=state.total_tokens_input,
                            total_tokens_out=state.total_tokens_output,
                            total_cost_usd=state.total_cost_usd,
                            error=state.error,
                            final_outputs=state.node_outputs,
                        )
                except Exception as e:
                    logger.warning("finalize_run 失败: %s", e)
            await _event_streams[run_id].put(None)

    asyncio.create_task(_resume_bridge())

    return RunResponse(
        run_id=run_id,
        stream_url=f"/api/agent/runs/{run_id}/events",
    )


def _get_downstream_nodes(wf, node_id: str) -> list[str]:
    """递归获取指定节点的所有下游节点 ID。"""
    downstream: list[str] = []
    visited: set[str] = set()

    def _walk(nid: str):
        for node in wf.nodes.values():
            if nid in node.after and node.id not in visited:
                visited.add(node.id)
                downstream.append(node.id)
                _walk(node.id)

    _walk(node_id)
    return downstream


@app.get("/api/agent/runs")
async def list_runs():
    if _orchestrator is None:
        return {"runs": []}
    runs = []
    for run_id, state in _orchestrator._runs.items():
        runs.append({
            "run_id": run_id,
            "workflow_id": state.workflow_id,
            "status": state.status.value,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "finished_at": state.finished_at.isoformat() if state.finished_at else None,
            "total_tokens_input": state.total_tokens_input,
            "total_tokens_output": state.total_tokens_output,
        })
    return {"runs": runs}


@app.get("/api/agent/runs/{run_id}")
async def get_run(run_id: str):
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    state = await _orchestrator.get_run(run_id)
    if state is None:
        raise HTTPException(404, f"Run not found: {run_id}")
    return {
        "run_id": state.run_id,
        "workflow_id": state.workflow_id,
        "status": state.status.value,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "finished_at": state.finished_at.isoformat() if state.finished_at else None,
        "total_tokens_input": state.total_tokens_input,
        "total_tokens_output": state.total_tokens_output,
        "node_states": {k: v.value for k, v in state.node_states.items()},
    }


@app.get("/api/agent/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request):
    """SSE endpoint: streams DagEvent + RawHarnessEvent as JSON lines."""
    queue = _event_streams.get(run_id)
    if queue is None:
        # Reconstruct queue from run (if not in memory, fail)
        raise HTTPException(404, f"Stream not found for run: {run_id}")

    async def event_generator() -> AsyncIterator[str]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # heartbeat
                    yield ": heartbeat\n\n"
                    continue
                if ev is None:  # sentinel
                    break
                # Serialize DagEvent or RawHarnessEvent
                if isinstance(ev, DagEvent):
                    payload = {
                        "type": ev.type.value,
                        "run_id": ev.run_id,
                        "node_id": ev.node_id,
                        "payload": ev.payload,
                        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                        "sequence": ev.sequence,
                    }
                    # surface_state 字段单独序列化（report_surface_state 事件）
                    if hasattr(ev, "surface_state") and ev.surface_state:
                        payload["surface_state"] = ev.surface_state.to_payload()
                else:
                    payload = {
                        "type": "raw." + getattr(ev, "event_type", "unknown"),
                        "harness": ev.harness,
                        "raw": ev.raw_payload,
                    }
                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        finally:
            # ⚠️ 不能在这里 pop queue！浏览器 SSE 断连（idle/网络/HMR）会触发重连，
            # pop 后下一次连接会 404「Stream not found」（用户报告的「接收不到」Bug 根因）。
            # queue 应该在 run 真正结束时由 DagEngine 清理（event_sink finalize）。
            pass  # 删了会导致每次 SSE 重连都 404

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/agent/runs/{run_id}/widget-input")
async def widget_input(run_id: str, payload: WidgetInputPayload):
    """Forward widget.input from frontend into a run.

    v0.5: stored in a per-run queue; the engine picks it up on next iteration.
    """
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    # 落库 HIL 介入点（v93 统一 session 架构：store 层参数已从 run_id 改为 session_id）
    if _event_store:
        await _event_store.append_widget_input(
            run_id=run_id,
            widget_id=payload.widget_id,
            payload=payload.input,
            user_id="frontend",
        )
    # P1: 转到 SessionEngine，让 Agent 真正收到消息
    try:
        await _orchestrator.submit_widget_input(run_id, payload.widget_id, payload.input)
    except Exception as e:
        logger.warning("submit_widget_input failed: %s", e)
    # store in event stream as widget.input event (让前端 UI 立即显示)
    queue = _event_streams.get(run_id)
    if queue is not None:
        synthetic = DagEvent(
            type=DagEventType.WIDGET_INPUT,
            run_id=run_id,
            node_id=None,
            payload={"widget_id": payload.widget_id, "input": payload.input, "user_id": "frontend"},
        )
        await queue.put(synthetic)
    return {"status": "accepted", "run_id": run_id, "widget_id": payload.widget_id}


@app.post("/api/agent/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """用户主动取消运行（对话模式 / DAG 模式都支持）。

    - 对话模式：触发 SessionEngine.cancel，emit session.dormant
    - DAG 模式：触发 DagEngine.cancel，emit RUN_CANCELLED
    """
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    try:
        await _orchestrator.abort(run_id, reason="user_cancelled")
    except Exception as e:
        logger.warning("cancel_run %s failed: %s", run_id, e)
    return {"status": "cancel_requested", "run_id": run_id}


# ── 审计 API（P0）──────────────────────────────────────────────────

@app.get("/api/audit/runs")
async def audit_list_runs(workflow_id: str | None = None, status: str | None = None,
                          limit: int = 100, offset: int = 0):
    """run 列表筛选（从 EventStore 查，含历史 run）。支持分页（limit + offset）。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    runs = await _event_store.list_runs(workflow_id=workflow_id, status=status,
                                       limit=limit, offset=offset)
    total = await _event_store.count_runs(workflow_id=workflow_id, status=status)
    return {"runs": runs, "count": len(runs), "total": total}


@app.get("/api/audit/runs/{run_id}/summary")
async def audit_get_run_summary(run_id: str):
    """单次 run 概要。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    return await _event_store.get_run_summary(run_id)


@app.get("/api/audit/runs/{run_id}/events")
async def audit_get_events(run_id: str, since: int = 0, limit: int = 10000):
    """分页查历史 DagEvent（按 sequence 排序）。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    events = await _event_store.get_run_events(run_id, since=since, limit=limit)
    return {
        "run_id": run_id,
        "count": len(events),
        "events": [
            {
                "type": e.type.value,
                "run_id": e.run_id,
                "node_id": e.node_id,
                "payload": e.payload,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                "sequence": e.sequence,
            }
            for e in events
        ],
    }


@app.get("/api/audit/runs/{run_id}/nodes/{node_id}/detail")
async def audit_get_node_detail(run_id: str, node_id: str):
    """节点详情聚合：输入/输出/tokens/工具调用/错误/HIL 介入。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    return await _event_store.get_node_detail(run_id, node_id)


# ── 协程栈诊断（卡死排查工具）──────────────────────────────────
@app.get("/api/debug/asyncio-tasks")
async def debug_asyncio_tasks(filter_substr: str = ""):
    """dump 当前事件循环所有协程的调用栈。

    定位节点协程永久挂起（asyncio.timeout 失效类 bug）的诊断工具：
    GET /api/debug/asyncio-tasks?filter_substr=actor_research
    """
    import asyncio as _aio

    tasks = [t for t in _aio.all_tasks() if not t.done()]
    out = []
    for t in tasks:
        coro = t.get_coro()
        frames = []
        f = getattr(coro, "cr_frame", None)
        while f is not None and len(frames) < 15:
            frames.append({
                "func": f.f_code.co_name,
                "file": f.f_code.co_filename,
                "line": f.f_lineno,
            })
            f = f.f_back
        name = repr(coro)
        if filter_substr and filter_substr not in name:
            continue
        out.append({"task": t.get_name(), "coro": name[:300], "stack": frames})
    return {"count": len(out), "tasks": out}


# ── 协作可视化 API ──────────────────────────────────────────────

# 泳道颜色板
_LANE_COLORS = ["#3b82f6", "#06b6d4", "#8b5cf6", "#f59e0b", "#ec4899", "#10b981"]

# 事件类型 → 中文标签
_EVENT_LABELS = {
    "run.created": "运行创建", "run.completed": "运行完成",
    "run.failed": "运行失败", "run.cancelled": "运行取消",
    "node.ready": "节点就绪", "node.started": "节点启动",
    "node.progress": "节点进度", "node.handoff": "节点交接",
    "node.completed": "节点完成", "node.failed": "节点失败",
    "node.skipped": "节点跳过", "widget.update": "组件更新",
    "widget.input": "组件输入", "usage": "用量统计",
    "cross_domain": "跨域调度",
}


def _resolve_business_role(node_id: str, wf, agents: dict) -> str:
    """解析链: node.business_role → agent.business_role → agent.display_name → agent_id"""
    node = wf.nodes.get(node_id)
    if node and node.business_role:
        return node.business_role
    if node and node.agent:
        ag = agents.get(node.agent)
        if ag:
            return ag.business_role or ag.display_name or node.agent
        return node.agent
    return node_id or "unknown"


# ====== 🆕 Phase 1: Session API ======

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """获取 Session 元数据 + 关联的子 Run 列表。

    向后兼容：旧 run 的 session_id = run_id（Phase 1 回填），但 sessions 表没有对应记录。
    此时从 runs 表反推构造临时 session 元数据，而不是返回 404。
    """
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    session = await _event_store.get_session(session_id)
    runs = await _event_store.list_child_runs_of_session(session_id)
    if not session:
        if not runs:
            raise HTTPException(404, f"Session {session_id} not found")
        # 旧 run 回填的 session_id：从 runs 表反推构造临时 session
        first_run = runs[0]
        session = {
            "session_id": session_id,
            "user_id": "",
            "agent_id": first_run.get("agent_id") or "manager",
            "status": "active",
            "title": "",
            "started_at": first_run.get("started_at", ""),
            "last_activity_at": first_run.get("last_activity_at") or first_run.get("finished_at") or first_run.get("started_at", ""),
            "message_count": 0,
            "attached_run_count": len(runs),
            "total_tokens_input": 0,
            "total_tokens_output": 0,
            "metadata": None,
        }
    return {"session": session, "runs": runs}


@app.get("/api/sessions/{session_id}/runs")
async def list_session_runs(session_id: str):
    """列出 Session 关联的所有子 Run。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    runs = await _event_store.list_child_runs_of_session(session_id)
    return {"session_id": session_id, "runs": runs, "total": len(runs)}


@app.get("/api/sessions/{session_id}/memory")
async def list_session_memory(session_id: str, limit: int = 20):
    """查询 Session 记忆（中期记忆）。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    memories = await _event_store.list_session_memory(session_id, limit=limit)
    return {"session_id": session_id, "memories": memories, "total": len(memories)}


@app.get("/api/audit/runs/{run_id}/collaboration-graph")
async def get_collaboration_graph(run_id: str):
    """聚合 workflow 定义 + audit 事件 → 协作全景数据（lanes/nodes/edges/handoffs/timeline）。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")

    # 1. run 元数据
    summary = await _event_store.get_run_summary(run_id)
    if not summary:
        raise HTTPException(404, f"Run {run_id} not found")

    # 2. 加载 workflow 定义（summary 是 dict[str, Any]，必须用 key 访问）
    workflow_id_str = summary.get("workflow_id") or ""
    wf = None
    if workflow_id_str:
        wf_path = PROJECT_ROOT / "workflows" / f"{workflow_id_str}.yaml"
        try:
            wf = load_workflow_yaml(wf_path)
        except Exception:
            wf = None  # 对话 session 或 workflow 文件已删除，降级返回仅元数据

    # 3. agent 配置（用于 business_role 解析链）
    from orchestrator.config_loader import get_system_config
    agents = get_system_config().agents

    # 4. audit 事件
    events = await _event_store.get_run_events(run_id)

    # 对话 session（无 workflow）：返回最小骨架，前端仍能看到 handoff/timeline
    if wf is None:
        handoffs_list = []
        for ev in events:
            etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
            if etype == "node.handoff" and ev.payload:
                handoffs_list.append({
                    "id": f"h_{ev.sequence}",
                    "from_node": ev.payload.get("from", ""),
                    "from_role": ev.payload.get("from_role", ""),
                    "to_node": ev.payload.get("to", ""),
                    "to_role": ev.payload.get("to_role", ""),
                    "port": ev.payload.get("port", ""),
                    "payload_size": ev.payload.get("payload_size", 0),
                    "summary": ev.payload.get("summary", ""),
                    "sequence": ev.sequence,
                    "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                })
        timeline_list = []
        for ev in sorted(events, key=lambda e: e.sequence):
            etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
            timeline_list.append({
                "sequence": ev.sequence,
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                "type": etype,
                "node_id": ev.node_id,
                "label": _EVENT_LABELS.get(etype, etype),
                "payload_size": ev.payload.get("payload_size") if ev.payload else None,
            })
        return {
            "run_id": run_id,
            "workflow_id": workflow_id_str,
            "status": summary.get("status") or "unknown",
            "started_at": summary.get("started_at"),
            "finished_at": summary.get("finished_at"),
            "lanes": [],
            "nodes": [],
            "edges": [],
            "handoffs": handoffs_list,
            "timeline": timeline_list,
            "note": "会话无关联 workflow（DAG 节点来自 conversation），仅展示 handoff/timeline",
        }

    # 3. agent 配置（用于 business_role 解析链）
    from orchestrator.config_loader import get_system_config
    agents = get_system_config().agents

    # 4. audit 事件
    events = await _event_store.get_run_events(run_id)

    # 5. 构建 lanes（按 business_role 分组）
    role_nodes: dict[str, list[str]] = {}
    for node_id in wf.nodes:
        role = _resolve_business_role(node_id, wf, agents)
        role_nodes.setdefault(role, []).append(node_id)
    lanes = [
        {"business_role": role, "color": _LANE_COLORS[i % len(_LANE_COLORS)], "nodes": ids}
        for i, (role, ids) in enumerate(role_nodes.items())
    ]

    # 6. 构建 nodes（合并 workflow 定义 + 事件状态）
    node_status: dict[str, str] = {}
    node_details: dict[str, dict] = {}
    for ev in events:
        if not ev.node_id:
            continue
        etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
        if etype == "node.started":
            node_status[ev.node_id] = "running"
        elif etype == "node.completed":
            node_status[ev.node_id] = "completed"
            if ev.payload:
                node_details[ev.node_id] = {
                    "duration_ms": ev.payload.get("duration_ms"),
                    "tokens": (ev.payload.get("tokens_in", 0) or 0) + (ev.payload.get("tokens_out", 0) or 0),
                }
        elif etype == "node.failed":
            node_status[ev.node_id] = "failed"
            if ev.payload:
                node_details[ev.node_id] = {"error_type": ev.payload.get("error_type")}
        elif etype == "node.skipped":
            node_status[ev.node_id] = "skipped"

    nodes_list = []
    for node_id, node in wf.nodes.items():
        agent_cfg = agents.get(node.agent) if node.agent else None
        # P0.5: inline_agent 优先于 node 顶层 harness/model
        effective_harness = (
            node.inline_agent.harness if node.inline_agent else node.harness
        )
        effective_model = (
            node.inline_agent.model if node.inline_agent else node.model
        )
        nodes_list.append({
            "node_id": node_id,
            "agent_id": node.agent or "",
            "business_role": _resolve_business_role(node_id, wf, agents),
            "display_name": node.name,
            "harness": effective_harness.value if hasattr(effective_harness, "value") else str(effective_harness),
            "model": str(effective_model) if effective_model else (str(agent_cfg.model_config) if agent_cfg else "auto"),
            "node_type": node.type.value if hasattr(node.type, "value") else str(node.type),
            "status": node_status.get(node_id, "pending"),
            "duration_ms": node_details.get(node_id, {}).get("duration_ms"),
            "token_usage": node_details.get(node_id, {}).get("tokens"),
            "error": node_details.get(node_id, {}).get("error_type"),
        })

    # 7. 构建 edges（从 workflow outputs）
    edges_list = []
    for node_id, node in wf.nodes.items():
        for port, route in node.outputs.items():
            for target, _ in route.parse_all():
                if target:
                    edges_list.append({"from": node_id, "to": target, "port": port})

    # 8. 构建 handoffs（从 NODE_HANDOFF 事件，已含 from_role/to_role/summary）
    handoffs_list = []
    for ev in events:
        etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
        if etype == "node.handoff" and ev.payload:
            handoffs_list.append({
                "id": f"h_{ev.sequence}",
                "from_node": ev.payload.get("from", ""),
                "from_role": ev.payload.get("from_role", ""),
                "to_node": ev.payload.get("to", ""),
                "to_role": ev.payload.get("to_role", ""),
                "port": ev.payload.get("port", ""),
                "payload_size": ev.payload.get("payload_size", 0),
                "summary": ev.payload.get("summary", ""),
                "sequence": ev.sequence,
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
            })

    # 9. 构建 timeline（全部事件按 sequence 排序）
    timeline_list = []
    for ev in sorted(events, key=lambda e: e.sequence):
        etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
        timeline_list.append({
            "sequence": ev.sequence,
            "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
            "type": etype,
            "node_id": ev.node_id,
            "label": _EVENT_LABELS.get(etype, etype),
            "payload_size": ev.payload.get("payload_size") if ev.payload else None,
        })

    return {
        "run_id": run_id,
        "workflow_id": workflow_id_str,
        "status": summary.get("status") or "unknown",
        "started_at": summary.get("started_at"),
        "finished_at": summary.get("finished_at"),
        "lanes": lanes,
        "nodes": nodes_list,
        "edges": edges_list,
        "handoffs": handoffs_list,
        "timeline": timeline_list,
    }


@app.get("/api/audit/runs/{run_id}/collaboration-graph/timeline")
async def get_collaboration_timeline(
    run_id: str,
    node_id: str | None = None,
    type: str | None = None,
    since_seq: int = 0,
    since_time: str | None = None,
):
    """独立 timeline 端点（按需加载，避免主接口 750KB payload）。

    Query 参数：
    - node_id: 过滤单节点（如 ?node_id=scan）
    - type: 过滤事件类型（如 ?type=node.handoff）
    - since_seq: 增量加载（自该 sequence 之后）
    - since_time: 时间范围（ISO 8601）
    """
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")

    events = await _event_store.get_run_events(run_id, since=since_seq)

    # 应用过滤
    def match(ev) -> bool:
        etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
        if node_id and ev.node_id != node_id:
            return False
        if type and etype != type:
            return False
        if since_time and ev.occurred_at:
            try:
                from datetime import datetime
                cutoff = datetime.fromisoformat(since_time.replace("Z", "+00:00"))
                if ev.occurred_at < cutoff:
                    return False
            except (ValueError, AttributeError):
                pass
        return True

    filtered = [ev for ev in events if match(ev)]

    timeline_list = []
    for ev in sorted(filtered, key=lambda e: e.sequence):
        etype = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
        timeline_list.append({
            "sequence": ev.sequence,
            "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
            "type": etype,
            "node_id": ev.node_id,
            "label": _EVENT_LABELS.get(etype, etype),
            "payload_size": ev.payload.get("payload_size") if ev.payload else None,
        })

    return {
        "run_id": run_id,
        "count": len(timeline_list),
        "timeline": timeline_list,
    }


# ── Provider 管理 API（P2-2）─────────────────────────────────────────

def _list_all_providers() -> list[dict[str, Any]]:
    """合并 PROVIDER_DEFAULTS + models.yaml + CredentialStore 的 provider 信息。

    三个数据源：
    - config/provider_catalog.py 的 PROVIDER_DEFAULTS：系统已知的 provider 全集（含默认 base_url/protocol/auth_type）
    - config/models.yaml 的 providers：用户配置（含 models 列表 + ${ENV} api_key）
    - CredentialStore：前端表单录入的加密凭证状态
    """
    from orchestrator.credential_store import get_credential_store
    from orchestrator.model_config import get_model_config
    from config.provider_catalog import PROVIDER_DEFAULTS

    mc = get_model_config()
    yaml_providers = mc.config.get("providers", {})

    # 合并所有 provider_id（defaults 全集 + yaml 中可能新增的）
    all_ids = sorted(set(PROVIDER_DEFAULTS.keys()) | set(yaml_providers.keys()))

    # CredentialStore 凭证状态（不含明文 key）
    store = get_credential_store()
    cred_map = {c["provider_id"]: c for c in store.list_providers()}

    result = []
    for pid in all_ids:
        defaults = PROVIDER_DEFAULTS.get(pid, {})
        yaml_cfg = yaml_providers.get(pid, {})
        # defaults 兜底，yaml 覆盖
        base_url = yaml_cfg.get("base_url") or defaults.get("base_url", "")
        protocol = yaml_cfg.get("protocol") or defaults.get("protocol", "openai_compatible")
        auth_type = yaml_cfg.get("auth_type") or defaults.get("auth_type", "bearer")

        # models 列表（来自 models.yaml）
        models = yaml_cfg.get("models", [])

        # models.yaml 中 ${ENV} 已展开，非空说明环境变量已配置
        yaml_api_key = yaml_cfg.get("api_key", "")
        has_env_key = bool(yaml_api_key)

        # CredentialStore 凭证状态（前端表单录入）
        cred_info = cred_map.get(pid)
        has_credential = cred_info is not None

        result.append({
            "provider_id": pid,
            "base_url": base_url,
            "protocol": protocol,
            "auth_type": auth_type,
            "models": models,
            "has_env_key": has_env_key,            # models.yaml 中 ${ENV} 是否已配置
            "has_credential": has_credential,      # CredentialStore 中是否有表单录入的凭证
            "credential_updated_at": cred_info["updated_at"] if cred_info else None,
        })
    return result


@app.get("/api/providers")
async def list_providers():
    """列出所有 provider（合并 defaults + models.yaml + 凭证状态）。"""
    return {"providers": _list_all_providers()}


@app.post("/api/providers/{provider_id}/credential")
async def set_provider_credential(provider_id: str, payload: CredentialPayload):
    """存储或更新 provider 的 api_key（加密存 CredentialStore）。"""
    from orchestrator.credential_store import get_credential_store
    store = get_credential_store()
    store.store(provider_id, payload.api_key)
    logger.info("Credential stored for provider '%s'", provider_id)
    return {"provider_id": provider_id, "status": "stored"}


@app.delete("/api/providers/{provider_id}/credential")
async def delete_provider_credential(provider_id: str):
    """删除 provider 的凭证。"""
    from orchestrator.credential_store import get_credential_store
    store = get_credential_store()
    deleted = store.delete(provider_id)
    if not deleted:
        raise HTTPException(404, f"No credential found for provider: {provider_id}")
    logger.info("Credential deleted for provider '%s'", provider_id)
    return {"provider_id": provider_id, "status": "deleted"}


# ── SSH 凭据 API（log-puller 日志拉取 / 将来 ssh_exec 复用）────────
# 凭据 id 形如 "ssh:<source_id>"，与模型 provider api_key 同存 CredentialStore（Fernet 加密）

@app.get("/api/ssh-credentials")
async def list_ssh_credentials():
    """列出已存储的 SSH 凭据（仅 id 与时间戳，不含明文）。"""
    from orchestrator.credential_store import get_credential_store
    items = [
        p for p in get_credential_store().list_providers()
        if p.get("kind") == "ssh"
    ]
    return {"credentials": items}


@app.post("/api/ssh-credentials/{credential_id}")
async def set_ssh_credential(credential_id: str, payload: SshCredentialPayload):
    """存储或更新 SSH 凭据（密码 / 私钥口令，加密存储）。"""
    if not credential_id.startswith("ssh:"):
        raise HTTPException(400, "credential_id 必须以 ssh: 开头（如 ssh:prod-seeyon）")
    if not payload.secret.strip():
        raise HTTPException(400, "secret 不能为空")
    from orchestrator.credential_store import get_credential_store
    get_credential_store().store(credential_id, payload.secret)
    logger.info("SSH credential stored for '%s'", credential_id)
    return {"credential_id": credential_id, "status": "stored"}


@app.delete("/api/ssh-credentials/{credential_id}")
async def delete_ssh_credential(credential_id: str):
    """删除 SSH 凭据。"""
    from orchestrator.credential_store import get_credential_store
    deleted = get_credential_store().delete(credential_id)
    if not deleted:
        raise HTTPException(404, f"No SSH credential found: {credential_id}")
    logger.info("SSH credential deleted for '%s'", credential_id)
    return {"credential_id": credential_id, "status": "deleted"}


# ── 数据库连接凭据 API（凭据管理 Tab「数据库连接」配套）────────
# 凭据 id 形如 "mysql:<connection_id>"，与 SSH 凭据同存 CredentialStore（Fernet 加密）
# 与 /api/ssh-credentials 同构，仅前缀放开为 mysql:/pg:/mssql:

_DB_CREDENTIAL_PREFIXES = ("mysql:", "pg:", "mssql:")


@app.post("/api/db-credentials/{credential_id}")
async def set_db_credential(credential_id: str, payload: SshCredentialPayload):
    """存储或更新数据库连接凭据（密码，Fernet 加密存储）。"""
    if not credential_id.startswith(_DB_CREDENTIAL_PREFIXES):
        raise HTTPException(
            400, f"credential_id 必须以 {' / '.join(_DB_CREDENTIAL_PREFIXES)} 开头（如 mysql:audit_reader）"
        )
    if not payload.secret.strip():
        raise HTTPException(400, "secret 不能为空")
    from orchestrator.credential_store import get_credential_store
    get_credential_store().store(credential_id, payload.secret)
    logger.info("DB credential stored for '%s'", credential_id)
    return {"credential_id": credential_id, "status": "stored"}


@app.delete("/api/db-credentials/{credential_id}")
async def delete_db_credential(credential_id: str):
    """删除数据库连接凭据。"""
    from orchestrator.credential_store import get_credential_store
    deleted = get_credential_store().delete(credential_id)
    if not deleted:
        raise HTTPException(404, f"No DB credential found: {credential_id}")
    logger.info("DB credential deleted for '%s'", credential_id)
    return {"credential_id": credential_id, "status": "deleted"}


# ── 配置与凭据管理 API（DESIGN_config_credential_refactor_v1.md §8）──
# 服务器连接 + 统一定时计划 + 日志拉取任务，分别读写：
#   ~/.agentops/private/log-pull.yaml（connections + pull_sources，敏感不进 git）
#   config/schedules.yaml（统一 schedules，patroller 消费）
#   config/patrol.yaml（仅 log_sources 白名单，供校验）
# 既有策略不做热加载，回写后需重启后端生效

class ConnectionPayload(BaseModel):
    id: str
    name: str = ""
    conn_type: str = "ssh"           # ssh（默认，向后兼容）| mysql
    host: str
    port: int = 22
    username: str
    database: str | None = None      # mysql 连接专用：默认 schema
    auth_type: str = "key"          # key | password（mysql 强制 password）
    credential_id: str | None = None  # 空/None/"None" → 归一化为 <conn_type>:<id>
    private_key_path: str | None = None
    enabled: bool = True


class SchedulePayload(BaseModel):
    id: str | None = None      # 新建留空（后端按 name slug 生成）；编辑必须带上原 id
    name: str
    workflow_id: str
    cron: str
    enabled: bool = True
    inputs: dict[str, Any] = {}


class LogPullSourcePayload(BaseModel):
    id: str
    name: str = ""
    connection_id: str              # 引用连接对象（host/port 等由其提供）
    remote_paths: list[str]
    local_log_source_id: str        # 必须在 patrol.yaml log_sources 白名单
    local_max_days: int = 7
    enabled: bool = False


class LogSourceDirPayload(BaseModel):
    """本地日志目录（patrol.yaml log_sources 白名单条目）。"""
    id: str
    name: str = ""
    path: str
    description: str = ""
    allow_read: bool = True
    allow_list: bool = True


# ── 本地日志目录（log_sources 白名单）──────────────────────

@app.get("/api/log-sources")
async def list_log_source_dirs():
    """本地日志目录列表（含被哪些拉取任务引用）。"""
    from orchestrator import log_pull_admin
    try:
        return {"log_sources": log_pull_admin.list_log_sources_detail()}
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"读取 config/patrol.yaml 失败：{e}")


@app.post("/api/log-sources")
async def upsert_log_source_dir(payload: LogSourceDirPayload):
    """新增/更新本地日志目录（按 id upsert，回写 patrol.yaml 白名单）。"""
    from orchestrator import log_pull_admin
    try:
        result = log_pull_admin.upsert_log_source(payload.model_dump())
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(400, str(e))
    logger.info("log source dir upserted: %s", result["id"])
    return result


@app.delete("/api/log-sources/{source_id}")
async def delete_log_source_dir(source_id: str):
    """删除本地日志目录；被拉取任务引用时 409。"""
    from orchestrator import log_pull_admin
    try:
        result = log_pull_admin.delete_log_source(source_id)
    except log_pull_admin.ReferencedConnectionError as e:
        raise HTTPException(409, str(e))
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(404, str(e))
    logger.info("log source dir deleted: %s", source_id)
    return result


# ── 服务器连接 ─────────────────────────────────────────────

@app.get("/api/connections")
async def list_connections():
    """连接对象列表（host 全量返回：编辑表单需要；凭据只报状态不报明文）。"""
    from orchestrator import log_pull_admin
    try:
        return {"connections": log_pull_admin.list_connections()}
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"读取 private/log-pull.yaml 失败：{e}")


@app.post("/api/connections")
async def upsert_connection(payload: ConnectionPayload):
    """新增/更新连接对象（按 id upsert；凭据录入走 /api/ssh-credentials/{id}）。"""
    from orchestrator import log_pull_admin
    try:
        result = log_pull_admin.upsert_connection(payload.model_dump())
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(400, str(e))
    logger.info("connection upserted: %s", result["id"])
    return result


@app.delete("/api/connections/{conn_id}")
async def delete_connection(conn_id: str):
    """删除连接对象；被拉取任务引用时 409（响应体列出引用方）。"""
    from orchestrator import log_pull_admin
    try:
        result = log_pull_admin.delete_connection(conn_id)
    except log_pull_admin.ReferencedConnectionError as e:
        raise HTTPException(409, str(e))
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(404, str(e))
    logger.info("connection deleted: %s", conn_id)
    return result


@app.post("/api/connections/{conn_id}/test")
async def test_connection(conn_id: str):
    """paramiko 建连测试（同步网络 IO，放线程池避免阻塞事件循环）。"""
    from orchestrator import log_pull_admin
    try:
        result = await asyncio.to_thread(log_pull_admin.test_connection, conn_id)
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(404, str(e))
    return result


# ── 统一定时计划 ───────────────────────────────────────────

@app.get("/api/schedules")
async def list_schedules():
    """统一计划列表（含下次触发时间）。"""
    from orchestrator import schedules_admin
    try:
        return {"schedules": schedules_admin.list_schedules()}
    except schedules_admin.SchedulesConfigError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"读取 config/schedules.yaml 失败：{e}")


@app.post("/api/schedules")
async def upsert_schedule(payload: SchedulePayload):
    """新增/更新计划（按 id upsert，校验 cron 与 workflow_id）。"""
    from orchestrator import schedules_admin
    try:
        result = schedules_admin.upsert_schedule(payload.model_dump())
    except schedules_admin.SchedulesConfigError as e:
        raise HTTPException(400, str(e))
    logger.info("schedule upserted: id=%s name=%s", result["id"], result["name"])
    return result


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    """删除计划（按 id）。"""
    from orchestrator import schedules_admin
    try:
        result = schedules_admin.delete_schedule(schedule_id)
    except schedules_admin.SchedulesConfigError as e:
        raise HTTPException(404, str(e))
    logger.info("schedule deleted: id=%s", result["id"])
    return result


# ── 日志拉取任务（路径不变，行为改造：connection_id 引用连接对象）──

@app.get("/api/log-pull/sources")
async def list_log_pull_sources():
    """拉取任务列表 + log_sources 白名单 id（前端下拉框）。"""
    from orchestrator import log_pull_admin
    try:
        sources = log_pull_admin.list_pull_sources()
        whitelist = log_pull_admin.list_log_source_ids()
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"读取配置失败：{e}")
    return {"sources": sources, "log_source_ids": whitelist}


@app.post("/api/log-pull/sources")
async def upsert_log_pull_source(payload: LogPullSourcePayload):
    """新增/更新拉取任务（按 id upsert；连接参数由 connection_id 引用）。"""
    from orchestrator import log_pull_admin
    try:
        result = log_pull_admin.upsert_source(payload.model_dump())
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(400, str(e))
    logger.info("log-pull source upserted: %s", result["id"])
    return result


@app.delete("/api/log-pull/sources/{source_id}")
async def delete_log_pull_source(source_id: str):
    """删除拉取任务（级联删除引用它的统一计划）。"""
    from orchestrator import log_pull_admin
    try:
        result = log_pull_admin.delete_source(source_id)
    except log_pull_admin.LogPullConfigError as e:
        raise HTTPException(404, str(e))
    logger.info("log-pull source deleted: %s (removed %d schedules)", source_id, result.get("removed_schedules", 0))
    return result


@app.get("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, mode: str = "api"):
    """测试 provider 连接。

    支持两种模式：
    - mode=api（默认）：直接调用 GET {base_url}/models，检查实际连通性
    - mode=token：仅校验凭证存在性和格式，不发网络请求（适用于本地模型或内网环境）
    """
    from orchestrator.provider_health import get_health_checker
    checker = get_health_checker()
    result = await asyncio.to_thread(checker.check_provider, provider_id, mode)
    # 兼容前端 ProviderTestResult.status（'ok'/'error'）+ ok（bool）
    return {"provider_id": provider_id, "status": "ok" if result.get("ok") else "error", **result}


@app.get("/api/providers/{provider_id}/fetch-models")
async def fetch_provider_models(provider_id: str):
    """从供应商 API 拉取可用模型列表（GET {base_url}/models）。

    需要 API Key 已配置。返回 { ok, models: [{id, ...}], error }。
    用于前端"拉取模型"按钮，让用户从服务商直接获取可用模型。
    """
    import httpx
    from orchestrator.credential_store import get_credential_store
    from orchestrator.model_config import get_model_config

    mc = get_model_config()
    provider = mc.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, f"未知 provider: {provider_id}")

    base_url = provider.get("base_url", "")
    if not base_url:
        raise HTTPException(400, "缺少 base_url")

    store = get_credential_store()
    api_key = store.get(provider_id) or provider.get("api_key", "")
    auth_type = provider.get("auth_type", "bearer")

    headers: dict[str, str] = {}
    if api_key:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "x-api-key":
            headers["x-api-key"] = api_key

    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = await asyncio.to_thread(
            lambda: httpx.Client(timeout=15).get(url, headers=headers)
        )
        if resp.status_code >= 400:
            return {"ok": False, "models": [], "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        # OpenAI 兼容格式: { data: [{ id: "model-name", ... }] }
        models = []
        if isinstance(data, dict) and "data" in data:
            for item in data["data"]:
                model_id = item.get("id", "") if isinstance(item, dict) else str(item)
                if model_id:
                    models.append({"id": model_id, "raw": item})
        elif isinstance(data, list):
            for item in data:
                model_id = item.get("id", "") if isinstance(item, dict) else str(item)
                if model_id:
                    models.append({"id": model_id, "raw": item})
        return {"ok": True, "models": models, "error": None}
    except Exception as e:
        return {"ok": False, "models": [], "error": str(e)}


# ── Usage 统计 API（P2-4）────────────────────────────────────────────

@app.get("/api/usage/summary")
async def usage_summary(days: int = 30, provider_id: str | None = None):
    """按日聚合用量统计（token + 成本），供用量面板展示。

    查询参数：
    - days: 统计最近 N 天（默认 30）
    - provider_id: 可选，按 provider 过滤
    """
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    rows = await _event_store.get_usage_summary(days=days, provider_id=provider_id)
    return {"days": days, "provider_id": provider_id, "summary": rows}


@app.get("/api/usage/breakdown")
async def usage_breakdown(days: int = 30):
    """多维度用量穿透（监控中心汇总卡片点击展开）：按业务/Agent/服务商/模型聚合。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    data = await _event_store.get_usage_breakdown(days=days)
    return {"days": days, **data}


# ── Sessions 会话 API（新建/继续/历史消息）─────────────────────────

@app.post("/api/sessions")
async def create_session(payload: SessionCreatePayload):
    """新建对话会话。"""
    if not _orchestrator:
        raise HTTPException(500, "orchestrator not initialized")
    try:
        from orchestrator.protocol import RunMode
        run_mode_enum = RunMode(payload.run_mode)
    except ValueError:
        raise HTTPException(400, f"无效的 run_mode: {payload.run_mode}")

    handle = await _orchestrator.run(RunRequest(
        run_mode=run_mode_enum,
        agent_id=payload.agent_id,
        initial_message=payload.message,
        workspace_id=payload.workspace_id,  # P0.18.7b: 传 workspace_id 触发 provisioner 路径
    ))
    # 异步生成会话标题（先兜底截取，后台 LLM 生成更新）
    if _event_store and _orchestrator:
        import asyncio as _asyncio
        from orchestrator.title_generator import generate_and_update_title
        _asyncio.create_task(
            generate_and_update_title(
                handle.run_id, payload.message, _event_store, _orchestrator.llm_config
            )
        )
    return {"run_id": handle.run_id, "stream_url": f"/api/agent/runs/{handle.run_id}/events"}


# DEPRECATED: v1 /api/sessions/{run_id}/messages 已由 /api/v2/sessions/{session_id}/turns 替代（2026-08-25 迁移）
@app.post("/api/sessions/{run_id}/messages")
async def send_session_message(run_id: str, payload: MessagePayload):
    """DEPRECATED: use /api/v2/sessions/{session_id}/turns instead（v2 迁移后返回 410）。"""
    raise HTTPException(410, "v1 会话消息接口已废弃，请改用 POST /api/v2/sessions/{session_id}/turns")


@app.get("/api/sessions/{run_id}/messages")
async def get_session_messages(run_id: str, limit: int = 1000):
    """DEPRECATED: use /api/v2/sessions/{session_id}/messages instead（v2 迁移后返回 410）。"""
    raise HTTPException(410, "v1 会话消息接口已废弃，请改用 GET /api/v2/sessions/{session_id}/messages")


@app.get("/api/sessions")
async def list_sessions(
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """会话列表（支持 status/search 过滤 + 分页）。v2 重构：会话统一为 conversational，移除 run_mode 过滤。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    sessions = await _event_store.list_sessions(
        status=status, search=search, limit=limit, offset=offset
    )
    total_sessions = await _event_store.count_sessions(
        status=status, search=search
    )
    return {"sessions": sessions, "count": len(sessions), "total": total_sessions}


@app.patch("/api/sessions/{run_id}/title")
async def update_session_title(run_id: str, title: str = ""):
    """手动更新会话标题。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    if not title.strip():
        raise HTTPException(400, "title 不能为空")
    await _event_store.update_session_title(run_id, title.strip())
    return {"run_id": run_id, "title": title.strip()}


# ── 知识管理中心 API（/api/knowledge/*）─────────────────────────────

# 知识库根目录（config/knowledge/）
_KB_ROOT = PROJECT_ROOT / "config" / "knowledge"

# 不支持 lint 的 domain（无标准 wiki 结构）
_LINT_UNSUPPORTED_DOMAINS = {"video-production"}

# lint_knowledge 工具的 severity → lint_issues 表 severity 归一化映射
_SEVERITY_MAP = {
    "high": "critical",
    "medium": "warning",
    "low": "info",
    "critical": "critical",
    "warning": "warning",
    "info": "info",
}


def _parse_last_ingest(log_path: Path) -> dict[str, Any] | None:
    """解析 domain 的 log.md，返回最后一条 ingest 记录。

    log.md 是 markdown 表格，格式：
    | timestamp | action | page_type | raw_filename | agent_id | target_pages |
    |---|---|---|---|---|---|
    | 2026-07-18T03:40:39+00:00 | ingest | source | xxx.md | content_curator | patterns |

    Returns:
        最后一条数据行的 dict，或 None（无数据行）
    """
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # 找表格数据行（以 | 开头，跳过表头和分隔行）
    data_rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        # 跳过分隔行（全是 ---）
        if all(set(c) <= {"-", ":"} for c in cells):
            continue
        # 跳过表头行（含 timestamp 字样）
        if cells and "timestamp" in cells[0].lower():
            continue
        data_rows.append(cells)
    if not data_rows:
        return None
    last = data_rows[-1]
    # 容错：列数不足时补空
    while len(last) < 6:
        last.append("")
    return {
        "timestamp": last[0],
        "action": last[1],
        "page_type": last[2],
        "raw_filename": last[3],
        "agent_id": last[4],
        "target_pages": last[5],
    }


def _count_md_files(directory: Path) -> int:
    """统计目录下 md 文件数（递归）。"""
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*.md"))


def _by_category_stats(domain_dir: Path) -> dict[str, int]:
    """按 category 统计 md 文件数：raw/entities/concepts/comparisons/other。"""
    cats = {"raw": 0, "entities": 0, "concepts": 0, "comparisons": 0, "other": 0}
    if not domain_dir.exists():
        return cats
    for md in domain_dir.rglob("*.md"):
        rel_parts = md.relative_to(domain_dir).parts
        if len(rel_parts) > 1:
            top = rel_parts[0].lower()
            if top in cats:
                cats[top] += 1
                continue
        cats["other"] += 1
    return cats


# --- Vault 浏览（5 个）---

@app.get("/api/knowledge/vault/list")
async def vault_list(path: str = "", ext_filter: str | None = None,
                     max_results: int = 500, tag_filter: str | None = None):
    """列举 vault 内指定路径下的文件和目录（单层，非递归）。

    返回 { entries: [{ name, type, path, size?, mtime?, ext? }], total, truncated }。
    """
    from tools.obsidian_vault import VAULT_ROOT, _resolve_vault_path

    target_dir = _resolve_vault_path(path)
    if not target_dir.exists():
        raise HTTPException(400, f"路径不存在：{path}")
    if not target_dir.is_dir():
        raise HTTPException(400, f"不是目录：{path}")

    ext_set = {e.strip().lower() for e in ext_filter.split(",")} if ext_filter else None
    entries: list[dict[str, Any]] = []
    for item in sorted(target_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if item.name.startswith("."):
            continue
        rel_path = str(item.relative_to(VAULT_ROOT)).replace("\\", "/")
        if item.is_dir():
            entries.append({
                "name": item.name,
                "type": "dir",
                "path": rel_path,
            })
        elif item.is_file():
            ext = item.suffix.lstrip(".").lower()
            if ext_set and ext not in ext_set:
                continue
            entries.append({
                "name": item.name,
                "type": "file",
                "path": rel_path,
                "size": item.stat().st_size,
                "mtime": datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc).isoformat(),
                "ext": ext,
            })
        if len(entries) >= max_results:
            break

    return {
        "entries": entries,
        "total": len(entries),
        "truncated": len(entries) >= max_results,
    }


@app.get("/api/knowledge/vault/read")
async def vault_read(path: str):
    """读取 vault 文件内容（先校验权限；>10MB 返回 413）。"""
    from tools.obsidian_vault import obsidian_vault, VAULT_ROOT
    if not path:
        raise HTTPException(400, "path 参数不能为空")
    # 先校验路径权限
    validation = await obsidian_vault({
        "action": "validate_path", "agent_id": "api", "path": path,
    })
    if "error" in validation:
        raise HTTPException(400, validation["error"])
    if not validation.get("read_allowed", False):
        raise HTTPException(403, "路径不可读")
    # 检查文件大小（>10MB 返回 413）
    normalized = path.replace("/", "\\").lstrip(".\\")
    abs_path = (VAULT_ROOT / normalized).resolve()
    try:
        abs_path.relative_to(VAULT_ROOT.resolve())
    except ValueError:
        raise HTTPException(403, "路径越界：不在 vault 内")
    if abs_path.is_file() and abs_path.stat().st_size > 10 * 1024 * 1024:
        raise HTTPException(413, "文件过大（>10MB），拒绝读取")
    result = await obsidian_vault({
        "action": "read_file", "agent_id": "api", "path": path,
    })
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/knowledge/vault/validate")
async def vault_validate(path: str):
    """校验 vault 路径的读/写权限。"""
    from tools.obsidian_vault import obsidian_vault
    if not path:
        raise HTTPException(400, "path 参数不能为空")
    result = await obsidian_vault({
        "action": "validate_path", "agent_id": "api", "path": path,
    })
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/knowledge/vault/search")
async def vault_search(payload: VaultSearchPayload):
    """搜索 vault（按关键字或 tag）。search_type: keyword | tag。"""
    from tools.obsidian_vault import obsidian_vault
    if not payload.query:
        raise HTTPException(400, "query 不能为空")
    action = "search_by_keyword" if payload.search_type != "tag" else "search_by_tag"
    args: dict[str, Any] = {
        "action": action,
        "agent_id": "api",
        "max_results": payload.max_results,
    }
    if action == "search_by_keyword":
        args["query"] = payload.query
    else:
        args["tag_filter"] = [payload.query] if payload.query else []
    result = await obsidian_vault(args)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/knowledge/vault/stats")
async def vault_stats():
    """统计 vault 概况（总文件数/总大小/按扩展名分布/最近修改）。"""
    from tools.obsidian_vault import obsidian_vault
    # 通过 list_files 扫根目录聚合（stats action 未在工具实现，用 list_files 兜底）
    result = await obsidian_vault({
        "action": "list_files",
        "agent_id": "api",
        "path": "",
        "max_results": 100000,
    })
    if "error" in result:
        raise HTTPException(400, result["error"])
    files = result.get("files", [])
    by_ext: dict[str, int] = {}
    total_size = 0
    latest_mtime = ""
    for f in files:
        ext = Path(f.get("path", "")).suffix.lstrip(".").lower() or "(no-ext)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
        total_size += int(f.get("size", 0))
        mt = f.get("mtime", "")
        if mt > latest_mtime:
            latest_mtime = mt
    return {
        "content": f"vault 共 {len(files)} 个文件，总大小 {total_size} 字节",
        "total_files": len(files),
        "total_size": total_size,
        "by_extension": by_ext,
        "latest_mtime": latest_mtime or None,
        "truncated": result.get("truncated", False),
    }


# --- 知识库仪表盘（4 个）---

@app.get("/api/knowledge/domains")
async def knowledge_domains():
    """列出所有知识库 domain 概况。

    元数据（display_name / description / schema / categories / bound_agents 等）
    从 config/knowledge/domains.yaml 读取（kb_config 加载器）；stats
    （page_count / last_ingest_at / lint_summary）从文件系统 + audit.db 实时算。
    新增 domain 只改 yaml 不改代码（kb_config.get_domain_map()）。
    """
    from tools import kb_config

    cfg = kb_config.load_domains()
    domains: list[dict[str, Any]] = []
    for domain_id, meta in cfg.domains.items():
        kb_path = PROJECT_ROOT / meta.kb_root
        # lint 概况（仅 supports_lint=true 的 domain 查）
        lint_summary: dict[str, Any] = {"total": 0, "critical": 0, "warning": 0, "info": 0}
        if meta.supports_lint and _event_store:
            try:
                summary = await _event_store.get_lint_summary(domain_id)
                lint_summary = {
                    "total": summary.get("total", 0),
                    "critical": summary.get("critical", 0),
                    "warning": summary.get("warning", 0),
                    "info": summary.get("info", 0),
                }
            except Exception as e:
                logger.warning("get_lint_summary(%s) 失败: %s", domain_id, e)
        # stats 实时计算（仅 kb_path 存在时算）
        page_count = _count_md_files(kb_path) if kb_path.exists() else 0
        last_ingest_at = _parse_last_ingest(kb_path / "log.md") if kb_path.exists() else None
        entry: dict[str, Any] = {
            "id": domain_id,
            "domain_id": domain_id,                            # 新字段 · 与前端 KnowledgeDomain 对齐
            "display_name": meta.display_name,                  # 新字段 · 从 yaml 读
            "description": meta.description,                    # 新字段 · 从 yaml 读
            "name": meta.display_name,                          # 向后兼容 · 原 name 字段
            "kb_root": meta.kb_root,                            # 新字段 · 相对项目根
            "vault_write_dir": meta.vault_write_dir,            # 新字段
            "schema": meta.schema,                              # 新字段 · llm_wiki | video_production
            "categories": meta.categories,                      # 新字段
            "category_layout": meta.category_layout,            # 新字段
            "supports_lint": meta.supports_lint,
            "bound_agents": meta.bound_agents,                  # 新字段
            "note": meta.note,                                  # 新字段
            "page_count": page_count,
            "last_ingest_at": last_ingest_at,
            "lint_summary": lint_summary,
            "exists": kb_path.exists(),                         # 新字段 · kb_root 是否实际创建
        }
        domains.append(entry)
    return {
        "domains": domains,
        "total": len(domains),
        "vault_root": cfg.vault_root,                          # 新增 · 前端顶部信息条用
        "write_whitelist": cfg.write_whitelist,                # 新增
        "config_source": cfg.source_path,                      # 新增 · 调试用
    }


@app.get("/api/knowledge/domains/{domain}")
async def knowledge_domain_detail(domain: str):
    """返回 domain 详情：index/log/AGENTS 内容 + by_category + recent_ingests。"""
    domain_dir = _KB_ROOT / domain
    if not domain_dir.exists() or not domain_dir.is_dir():
        raise HTTPException(404, f"domain 不存在：{domain}")

    def _read_text(p: Path) -> str | None:
        try:
            return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None
        except OSError:
            return None

    detail: dict[str, Any] = {
        "domain": domain,
        "index_md": _read_text(domain_dir / "index.md"),
        "log_md": _read_text(domain_dir / "log.md"),
        "agents_md": _read_text(domain_dir / "AGENTS.md"),
        "by_category": _by_category_stats(domain_dir),
        "supports_lint": domain not in _LINT_UNSUPPORTED_DOMAINS,
    }
    # 解析 recent_ingests（log.md 最近 10 条）
    log_path = domain_dir / "log.md"
    if log_path.exists():
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ":"} for c in cells):
                continue
            if cells and "timestamp" in cells[0].lower():
                continue
            while len(cells) < 6:
                cells.append("")
            rows.append({
                "timestamp": cells[0], "action": cells[1], "page_type": cells[2],
                "raw_filename": cells[3], "agent_id": cells[4], "target_pages": cells[5],
            })
        detail["recent_ingests"] = rows[-10:]
    else:
        detail["recent_ingests"] = []
    return detail


@app.get("/api/knowledge/domains/{domain}/files")
async def knowledge_domain_files(domain: str, category: str | None = None):
    """列出 domain 下所有 md 文件，支持 category 过滤。"""
    domain_dir = _KB_ROOT / domain
    if not domain_dir.exists() or not domain_dir.is_dir():
        raise HTTPException(404, f"domain 不存在：{domain}")
    valid_cats = {"raw", "entities", "concepts", "comparisons"}
    if category and category not in valid_cats:
        raise HTTPException(400, f"无效 category：{category}（可选：{sorted(valid_cats)}）")
    files: list[dict[str, Any]] = []
    for md in sorted(domain_dir.rglob("*.md")):
        rel = md.relative_to(domain_dir)
        rel_str = str(rel).replace("\\", "/")
        top = rel.parts[0].lower() if len(rel.parts) > 1 else "other"
        if category and top != category:
            continue
        files.append({
            "path": rel_str,
            "category": top if top in valid_cats else "other",
            "size": md.stat().st_size,
            "mtime": datetime.fromtimestamp(md.stat().st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"domain": domain, "files": files, "total": len(files), "category": category}


@app.get("/api/knowledge/domains/{domain}/file")
async def knowledge_domain_file_read(domain: str, filename: str):
    """读取 domain 下指定 md 文件内容（防路径遍历）。"""
    domain_dir = _KB_ROOT / domain
    if not domain_dir.exists() or not domain_dir.is_dir():
        raise HTTPException(404, f"domain 不存在：{domain}")
    if not filename:
        raise HTTPException(400, "filename 参数不能为空")
    # 防路径遍历：filename 不能含 .. 或绝对路径
    target = (domain_dir / filename).resolve()
    try:
        target.relative_to(domain_dir.resolve())
    except ValueError:
        raise HTTPException(403, "路径越界：不在 domain 目录内")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"文件不存在：{filename}")
    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        raise HTTPException(500, f"读取失败：{e}")
    return {
        "domain": domain,
        "filename": filename,
        "content": content,
        "size": target.stat().st_size,
    }


# --- Lint 处理（3 个）---

@app.post("/api/knowledge/domains/{domain}/lint")
async def knowledge_lint_trigger(domain: str, payload: LintTriggerPayload):
    """触发 domain 的 lint 检测，结果写入 lint_issues 表。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    if domain in _LINT_UNSUPPORTED_DOMAINS:
        raise HTTPException(400, f"domain {domain} 不支持 lint（无标准 wiki 结构）")
    from tools.lint_knowledge import lint_knowledge, CHECK_TYPES
    # 默认检查类型排除 contradictions（需 new_content，周期性 lint 无此参数）
    if payload.check_types:
        check_types = payload.check_types
    else:
        check_types = [ct for ct in CHECK_TYPES if ct != "contradictions"]
    result = await lint_knowledge({
        "domain": domain,
        "agent_id": "api",
        "auto_fix": payload.auto_fix,
        "check_types": check_types,
    })
    if "error" in result:
        raise HTTPException(400, result["error"])
    # 把 issues 逐条写入 lint_issues 表
    issues = result.get("issues", [])
    written: list[dict[str, Any]] = []
    for issue in issues:
        severity = _SEVERITY_MAP.get(issue.get("severity", "low"), "info")
        # 提取 page_a / page_b（不同 issue 类型字段不同）
        page_a = (
            issue.get("existing_page")
            or issue.get("page")
            or issue.get("referenced_in")
        )
        page_b = (
            issue.get("missing_target")
            or issue.get("dead_target")
            or None
        )
        description = (
            issue.get("recommended_action")
            or issue.get("llm_prompt_hint")
            or issue.get("reason")
            or json.dumps(issue, ensure_ascii=False, default=str)
        )
        auto_fixable = bool(issue.get("auto_fixable", False))
        issue_id = await _event_store.append_lint_issue(
            domain=domain,
            type_=issue.get("type", "unknown"),
            severity=severity,
            description=description,
            page_a=page_a,
            page_b=page_b,
            auto_fixable=auto_fixable,
        )
        written.append({"issue_id": issue_id, "type": issue.get("type"), "severity": severity})
    return {
        "domain": domain,
        "checked_at": result.get("checked_at"),
        "check_types": result.get("check_types"),
        "auto_fixed": result.get("auto_fixed", 0),
        "needs_human_review": result.get("needs_human_review", 0),
        "issues_detected": len(issues),
        "issues_written": len(written),
        "written": written,
    }


@app.get("/api/knowledge/domains/{domain}/lint/issues")
async def knowledge_lint_issues(domain: str, status: str | None = None,
                                type: str | None = None, severity: str | None = None,
                                limit: int = 100, offset: int = 0):
    """查询 domain 的 lint issues 列表。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    issues, total = await _event_store.list_lint_issues(
        domain=domain, status=status, type_=type, severity=severity,
        limit=limit, offset=offset,
    )
    return {"domain": domain, "issues": issues, "total": total, "limit": limit, "offset": offset}


@app.post("/api/knowledge/lint-issues/{issue_id}/resolve")
async def knowledge_lint_resolve(issue_id: str, payload: LintResolvePayload):
    """处理 lint issue。action: resolve | ignore | fix | reopen。"""
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    action_map = {
        "resolve": "resolved",
        "ignore": "ignored",
        "fix": "resolved",
        "reopen": "pending",
    }
    if payload.action not in action_map:
        raise HTTPException(
            400, f"无效 action：{payload.action}（可选：{sorted(action_map.keys())}）"
        )
    status = action_map[payload.action]
    note = payload.note
    if payload.action == "fix":
        note = f"[auto-fix] {note}" if note else "[auto-fix]"
    ok = await _event_store.update_lint_issue_status(
        issue_id=issue_id, status=status,
        resolved_by="api", resolution_note=note,
    )
    if not ok:
        raise HTTPException(404, f"issue 不存在：{issue_id}")
    return {"issue_id": issue_id, "status": status, "action": payload.action}


# --- Agent 触发（2 个）---

@app.post("/api/knowledge/scan-drafts")
async def knowledge_scan_drafts(payload: ScanDraftsPayload):
    """扫描草稿仓库增量文档。"""
    from tools.scan_drafts import scan_drafts
    args: dict[str, Any] = {
        "agent_id": "api",
        "draft_root": payload.draft_root,
    }
    if payload.since:
        args["since"] = payload.since
    result = await scan_drafts(args)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/knowledge/curate")
async def knowledge_curate(payload: CuratePayload):
    """触发 content-curation workflow（scan_drafts → evaluate → archive）。"""
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    if "content-curation" not in _orchestrator.workflows:
        raise HTTPException(404, "workflow not found: content-curation")
    # 构造 inputs：draft_path 取首个或默认，since 透传
    draft_path = (
        payload.draft_paths[0] if payload.draft_paths else "草稿仓库/"
    )
    inputs: dict[str, Any] = {"draft_path": draft_path}
    if payload.since:
        inputs["since"] = payload.since
    # v3 修复：提前生成 session_id，让 engine 启动前先写 runs 表（避免 FK 竞态）
    _sid2 = f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{_uuid4().hex[:6]}"
    handle = await _orchestrator.run(RunRequest(
        workflow_id="content-curation",
        inputs=inputs,
        run_mode=RunMode.TEMPLATED,
        session_id=_sid2,
    ))
    if _event_store:
        await _event_store.create_session(
            session_id=_sid2,
            agent_id="",
        )
        await _event_store.init_run(
            run_id=handle.run_id,
            session_id=_sid2,
            workflow_id="content-curation",
            run_mode="templated",
            inputs=inputs,
        )
    # 初始化 event queue + 桥接（与 start_run 一致）
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _event_streams[handle.run_id] = queue
    last_seq: dict[str, int] = {}

    async def _bridge():
        await bridge_run_events(handle.run_id, queue, last_seq)

    asyncio.create_task(_bridge())
    return {
        "run_id": handle.run_id,
        "stream_url": f"/api/agent/runs/{handle.run_id}/events",
        "workflow_id": "content-curation",
        "inputs": inputs,
    }


# --- 智能问答（Karpathy LLM Wiki Query 流程）--

# 知识问答索引扫描的文件类型（与 tools/obsidian_vault.py 的 lazy extractor 保持一致）
_KB_INDEX_EXTS = {".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx", ".txt"}
# 顶层目录清单里每个目录最多展示的文件数（防止 prompt 超长）
_LIST_VAULT_MAX_FILES_PER_DIR = 30
# 顶层目录清单总文件数上限（防止个别超大目录把整个索引撑爆）
_LIST_VAULT_MAX_TOTAL_FILES = 200


def _list_vault_dir_files(rel_dir: str) -> str:
    """列出 vault 目录下的可索引文件清单（退化索引，domain 无 index.md 时兜底）。

    与 obsidian_vault 支持的 extractor 类型对齐：md / html / pdf / docx / pptx / xlsx。
    修复前只看 *.md，导致 HTML/PDF 等报告（Reports/Harness_Engineering_深度解析.html 等）
    永远不进知识问答索引，LLM 找不到相关文档直接回「未找到」。
    """
    from tools.obsidian_vault import VAULT_ROOT, _resolve_vault_path
    try:
        target = _resolve_vault_path(rel_dir)
    except ValueError:
        return f"（目录不存在：{rel_dir}）"
    if not target.exists() or not target.is_dir():
        return f"（目录不存在：{rel_dir}）"
    files: list[str] = []
    for f in target.rglob("*"):
        if not f.is_file() or ".obsidian" in f.parts:
            continue
        if f.suffix.lower() not in _KB_INDEX_EXTS:
            continue
        rel = str(f.relative_to(VAULT_ROOT)).replace("\\", "/")
        files.append(f"- {rel}")
        if len(files) >= 200:
            break
    return "\n".join(files) if files else "（无可索引文件）"


def _list_vault_top_dirs() -> str:
    """列出 vault 顶层目录及其可索引文件清单（作为全局退化索引补充）。

    每个目录除总数外还列出最多 N 个文件名（含相对路径），让 LLM 看得到具体文档名
    （如 Reports/Harness_Engineering_深度解析.html），
    否则即使目录被列出，没有具体文件名 LLM 也不会调 read_file。
    """
    from tools.obsidian_vault import VAULT_ROOT
    lines: list[str] = []
    total_shown = 0
    truncated = False
    for d in sorted(VAULT_ROOT.iterdir()):
        if d.name.startswith(".") or not d.is_dir():
            continue
        files = [
            f for f in d.rglob("*")
            if f.is_file() and ".obsidian" not in f.parts and f.suffix.lower() in _KB_INDEX_EXTS
        ]
        if not files:
            continue
        rels = [str(f.relative_to(VAULT_ROOT)).replace("\\", "/") for f in files]
        rels.sort()
        remaining_budget = _LIST_VAULT_MAX_TOTAL_FILES - total_shown
        if remaining_budget <= 0:
            truncated = True
            break
        head = rels[: min(_LIST_VAULT_MAX_FILES_PER_DIR, remaining_budget)]
        total_shown += len(head)
        more = ""
        if len(rels) > len(head):
            more = f"\n  - ...（还有 {len(rels) - len(head)} 个）"
        bullet = "\n".join(f"  - {r}" for r in head)
        lines.append(f"- {d.name}/（共 {len(rels)} 个可索引文件）\n{bullet}{more}")
    if truncated:
        lines.append(f"\n（注：vault 文件数过多，已截断到前 {_LIST_VAULT_MAX_TOTAL_FILES} 个；如需指定文档请用 read_file 直接传完整路径）")
    return "\n".join(lines)


def _build_ask_index(domain: str | None) -> str:
    """构建查询索引：优先用 domain 的 index.md，无则用文件清单兜底。

    遵循 Karpathy LLM Wiki Query 流程：LLM 先读 index 定位相关页面，再深入阅读。
    """
    sections: list[str] = []
    if domain:
        # 指定 domain：优先读 config/knowledge/{domain}/index.md
        index_path = _KB_ROOT / domain / "index.md"
        if index_path.exists():
            sections.append(
                f"### Domain: {domain}\n\n"
                + index_path.read_text(encoding="utf-8", errors="ignore")
            )
        else:
            # 退化：列出 vault 中同名目录的文件清单
            file_list = _list_vault_dir_files(domain)
            sections.append(f"### Domain: {domain}（文件清单兜底）\n\n{file_list}")
    else:
        # 未指定 domain：拼接所有 config/knowledge/*/index.md
        for d in sorted(_KB_ROOT.iterdir()):
            if d.is_dir():
                idx = d / "index.md"
                if idx.exists():
                    sections.append(
                        f"### Domain: {d.name}\n\n"
                        + idx.read_text(encoding="utf-8", errors="ignore")
                    )
        # 补充 vault 顶层目录清单（覆盖无 wiki 结构的目录如草稿仓库）
        vault_listing = _list_vault_top_dirs()
        if vault_listing:
            sections.append(f"### Vault 顶层目录\n\n{vault_listing}")
    return "\n\n---\n\n".join(sections) if sections else "（无可用索引）"


@app.post("/api/knowledge/ask")
async def knowledge_ask(payload: KnowledgeAskPayload):
    """知识库智能问答（Karpathy LLM Wiki Query 流程）。

    流程：读 index.md 定位 → LLM 通过 read_file function calling 读全文 → 生成带 [📄 文档名] 出处的答案。
    不使用向量数据库切片检索，保证 LLM 读到完整文档上下文。
    """
    import re
    from harness.local_llm import LocalLlmClient
    from harness.protocol import AgentRunContext, AgentEventType, ToolDefinition

    if not payload.question.strip():
        raise HTTPException(400, "question 不能为空")
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")

    llm_cfg = _orchestrator.llm_config
    if not llm_cfg.get("base_url") or not llm_cfg.get("api_key"):
        raise HTTPException(503, "LLM 未配置（base_url/api_key 为空）")

    start_ms = time.time() * 1000

    # 1. 构建索引（复用 domain index.md 或文件清单兜底）
    index_content = _build_ask_index(payload.domain)

    # 2. 跟踪 LLM 读取的文件（path -> 完整内容，用于引用 snippet 提取）
    read_files: dict[str, str] = {}

    # 3. read_file 工具 handler（封装 obsidian_vault.read_file）
    async def _read_file_handler(args: dict[str, Any]) -> dict[str, Any]:
        path = (args.get("path") or "").strip()
        if not path:
            return {"error": "missing path"}
        from tools.obsidian_vault import obsidian_vault
        result = await obsidian_vault({
            "action": "read_file", "agent_id": "ask_api", "path": path,
        })
        if "error" in result:
            return {"error": result["error"]}
        content = result.get("full_content", result.get("content", ""))
        read_files[path] = content  # 记录读取的文件内容
        logger.info("knowledge_ask read_file path=%s size=%d", path, len(content))
        # 超长文档截断到 8000 字符（防 prompt 超长，风险表应对）
        truncated = content[:8000] + ("\n...(截断)" if len(content) > 8000 else "")
        return {"path": path, "content": truncated}

    read_file_tool = ToolDefinition(
        name="read_file",
        description="读取知识库中指定路径文档的完整内容。path 为 vault 内相对路径。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "vault 内相对路径，如 '草稿仓库/到底什么是Agent Harness.md'",
                }
            },
            "required": ["path"],
        },
        handler=_read_file_handler,
    )

    # 4. 构造 system prompt + user prompt
    system_prompt = (
        "你是知识库查询助手。你的任务是基于知识库内容回答用户问题。\n\n"
        "工作流程：\n"
        "1. 分析下方「知识库索引」，判断哪些文档与问题相关\n"
        "2. 最多调用 read_file 读取 2 个文档后就必须生成答案（不要再继续 read_file）\n"
        "3. 如果索引里没有任何看起来相关的文档，直接回答「知识库中未找到相关内容」，不调用 read_file\n"
        "4. 基于读到的完整内容综合生成答案\n\n"
        "输出格式（前端会做 markdown 渲染，请充分利用）：\n"
        "- 使用 # ## ### 标题分层；多个并列要点用 - 或 1.2.3. 列表\n"
        "- 关键术语用 **加粗**；代码/路径/配置项用 `代码`；多行代码用 ```代码块```\n"
        "- 文档结构、字段说明等可用 | 列1 | 列2 | 表格呈现\n"
        "- 每个观点后用 [📄 文档名] 标注出处（文档名用实际文件名，不含路径）\n\n"
        "规则：\n"
        "- 如果读到的内容与问题无关或读不到，明确说明「知识库中未找到相关内容」\n"
        "- 不要编造，只基于实际读到的内容回答\n"
        "- 用中文回答"
    )
    user_prompt = (
        f"知识库索引：\n\n{index_content}\n\n---\n\n"
        f"问题：{payload.question}\n\n"
        f"最多调用 read_file 读取 2 个文档后必须基于内容生成带 [📄 文档名] 出处的答案，不要再继续 read_file。"
    )

    # 5. 调用 LocalLlmClient（function calling 多轮循环由 harness 内部处理）
    client = LocalLlmClient(
        base_url=llm_cfg.get("base_url", ""),
        api_key=llm_cfg.get("api_key", ""),
        model=llm_cfg.get("model", ""),
        timeout=60.0,
    )
    context = AgentRunContext(
        system_prompt=system_prompt,
        model=llm_cfg.get("model", ""),
        api_key=llm_cfg.get("api_key", ""),
        base_url=llm_cfg.get("base_url", ""),
        workspace="",
        session_id=f"ask-{uuid.uuid4().hex[:8]}",
        protocol="openai_compatible",
    )

    answer_parts: list[str] = []
    error_msg: str | None = None
    try:
        async for event in client.run(user_prompt, [read_file_tool], context):
            if event.type == AgentEventType.TEXT and event.text:
                answer_parts.append(event.text)
            elif event.type == AgentEventType.ERROR and event.error_message:
                error_msg = event.error_message
    except Exception as e:
        logger.exception("knowledge_ask LLM 调用失败: %s", e)
        raise HTTPException(500, f"LLM 调用失败：{e}")

    if error_msg and not answer_parts:
        raise HTTPException(500, f"LLM 调用失败：{error_msg}")

    answer = "".join(answer_parts).strip()
    # 过滤 LLM 输出的 <think>...</think> 推理标签（MiniMax 等模型会输出思考过程）
    answer = re.sub(r"<think>.*?</think>\s*", "", answer, flags=re.DOTALL).strip()
    # 过滤未闭合的 <think> 标签（LLM 截断时可能发生）
    answer = re.sub(r"<think>.*$", "", answer, flags=re.DOTALL).strip()
    if not answer:
        answer = "（LLM 未返回有效答案，请重试）"

    # 6. 解析引用 [📄 文档名]，匹配到实际读取的文件
    citation_pattern = re.compile(r"\[📄\s*([^\]]+)\]")
    cited_names: list[str] = []
    for m in citation_pattern.finditer(answer):
        name = m.group(1).strip()
        if name and name not in cited_names:
            cited_names.append(name)

    citations: list[dict[str, Any]] = []
    for name in cited_names:
        matched_path = None
        snippet = ""
        # 在 read_files 中按文件名匹配
        for fpath, fcontent in read_files.items():
            fname = Path(fpath).name
            if name == fname or name == fpath or name in fpath:
                matched_path = fpath
                snippet = fcontent[:200].replace("\n", " ").strip()
                break
        if not matched_path:
            matched_path = name
        citations.append({"path": matched_path, "snippet": snippet})

    elapsed_ms = int(time.time() * 1000 - start_ms)
    logger.info(
        "knowledge_ask question=%r domain=%s read_files=%d citations=%d elapsed=%dms",
        payload.question[:50], payload.domain, len(read_files), len(citations), elapsed_ms,
    )

    return {
        "answer": answer,
        "citations": citations,
        "matched_documents": len(read_files),
        "elapsed_ms": elapsed_ms,
    }


# ── Patroller 巡检 API ──────────────────────────────────────────────

@app.get("/api/patrol/alerts")
async def patrol_alerts(limit: int = 50):
    """查询最近的巡检告警（patrol_alert + log_patrol_triggered 事件）。"""
    # 返回最近 N 条告警（倒序）
    return {"alerts": list(reversed(_global_alerts[-limit:]))}


@app.get("/api/patrol/alerts/stream")
async def patrol_alerts_stream(request: Request):
    """SSE 流式订阅巡检告警（实时推送）。"""
    import json as _json

    async def event_generator() -> AsyncIterator[str]:
        # 先投递历史告警
        for alert in _global_alerts[-20:]:
            yield f"data: {_json.dumps(alert, ensure_ascii=False)}\n\n"
        # 然后实时推送新告警
        while True:
            if await request.is_disconnected():
                break
            try:
                alert = await asyncio.wait_for(_global_alert_queue.get(), timeout=15)
                yield f"data: {_json.dumps(alert, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/patrol/log-patrol/trigger")
async def trigger_log_patrol(payload: dict[str, Any] | None = None):
    """手动触发日志巡检工作流。

    Body（可选）:
        {"log_dir": "...", "time_range": "24h", "level": "ERROR"}
    """
    if _patroller is None:
        raise HTTPException(503, "Patroller not initialized")
    inputs = payload or {}
    run_id = await _patroller.trigger_log_patrol_now(inputs=inputs or None)
    if run_id is None:
        raise HTTPException(500, "触发失败：workflow 未加载或触发器未配置")
    return {"run_id": run_id, "stream_url": f"/api/agent/runs/{run_id}/events"}


# ── 监控中心告警推送 API（emit_alert 工具 HTTP 入口）─────────────────
# 由 tools/emit_alert.py 通过 httpx POST 调用（子进程不能直接 import 全局变量）
# 把 tip 写入 _global_alerts + _global_alert_queue，让 SSE 通道实时推给前端

# 允许的 severity / tip_type 取值（与 tools/emit_alert.py 一致）
_MONITOR_SEVERITIES = {"info", "warning", "error", "success"}
_MONITOR_TIP_TYPES = {
    "patrol_alert", "task_started", "task_completed",
    "validation_result", "quota_warning",
}
# tips-stream SSE 订阅者集合（广播模式，emit-alert 时遍历 put）
_tip_subscribers: set[asyncio.Queue] = set()


@app.post("/api/monitor/emit-alert")
async def monitor_emit_alert(payload: dict[str, Any] | None = None):
    """接收 emit_alert 工具推送的告警/提示，写入全局告警队列。

    Body:
        {
            "tip_id": "tip_xxx"（可选，不传则后端生成）,
            "severity": "info|warning|error|success",
            "title": "标题",
            "message": "正文",
            "agent_id": "task_monitor"（可选）,
            "run_id": "run_xxx"（可选）,
            "tip_type": "patrol_alert|task_started|task_completed|..."
        }

    Returns:
        {"ok": True, "tip_id": "..."}
    """
    if payload is None:
        raise HTTPException(400, "body 不能为空")

    severity = str(payload.get("severity", "info")).lower()
    if severity not in _MONITOR_SEVERITIES:
        raise HTTPException(400, f"severity 取值非法：{severity}")

    tip_type = str(payload.get("tip_type", "patrol_alert")).lower()
    if tip_type not in _MONITOR_TIP_TYPES:
        raise HTTPException(400, f"tip_type 取值非法：{tip_type}")

    title = str(payload.get("title", "")).strip()
    message = str(payload.get("message", "")).strip()
    if not title and not message:
        raise HTTPException(400, "title 和 message 至少需要一个非空")

    # tip_id：客户端可传，不传则后端生成
    tip_id = str(payload.get("tip_id") or f"tip_{uuid.uuid4().hex[:12]}")

    event = {
        "type": "monitor_tip",
        "tip_id": tip_id,
        "severity": severity,
        "title": title,
        "message": message,
        "agent_id": payload.get("agent_id"),
        "run_id": payload.get("run_id"),
        "tip_type": tip_type,
        "emitted_at": datetime.now(timezone.utc).isoformat(),
    }
    _global_alerts.append(event)
    if len(_global_alerts) > 100:
        _global_alerts.pop(0)
    try:
        _global_alert_queue.put_nowait(event)
    except asyncio.QueueFull:
        # 队列满则丢弃（前端可轮询 _global_alerts 补偿）
        logger.warning("monitor_emit_alert: 全局告警队列已满，tip_id=%s 被丢弃", tip_id)
    # 广播到所有 tips-stream SSE 订阅者
    for sub in list(_tip_subscribers):
        try:
            sub.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("monitor tips 订阅者队列已满，丢弃 tip_id=%s", tip_id)
    logger.info(
        "monitor_emit_alert tip_id=%s severity=%s tip_type=%s title=%s",
        tip_id, severity, tip_type, title[:50],
    )
    return {"ok": True, "tip_id": tip_id}


# ── 监控中心只读 API（额度 / Agent 状态 / Tips）──────────────────────


def _load_quota_config() -> dict[str, Any]:
    """加载 config/quota.yaml（每次请求读盘，避免热更新时漏掉）。

    失败时返回空 dict（providers 空 → get_quota_status 返回空列表）。
    """
    quota_path = PROJECT_ROOT / "config" / "quota.yaml"
    if not quota_path.exists():
        logger.warning("quota.yaml 不存在: %s", quota_path)
        return {}
    try:
        with open(quota_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("加载 quota.yaml 失败: %s", e)
        return {}


def _normalize_tip(raw: dict[str, Any]) -> dict[str, Any]:
    """把 _global_alerts 内部格式转成前端 Tip 类型。

    内部格式：{tip_id, tip_type, severity, title, message, agent_id, run_id, emitted_at}
    前端 Tip：{id, type, severity, agent_id, run_id, title, message, timestamp}
    """
    return {
        "id": raw.get("tip_id") or raw.get("id") or "",
        "type": raw.get("tip_type") or raw.get("type") or "patrol_alert",
        "severity": raw.get("severity", "info"),
        "agent_id": raw.get("agent_id"),
        "run_id": raw.get("run_id"),
        "title": raw.get("title", ""),
        "message": raw.get("message", ""),
        "timestamp": raw.get("emitted_at") or raw.get("timestamp") or "",
    }


@app.get("/api/usage/quota-status")
async def get_quota_status():
    """查询各 provider 滑动窗口配额状态（供监控中心 QuotaPanel）。

    Returns:
        {
            "providers": [QuotaProvider],   # 见 audit.store.get_quota_status 返回
            "alert_thresholds": {"yellow": 80, "red": 95}
        }
    """
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")
    quota_cfg = _load_quota_config()
    thresholds = quota_cfg.get("alert_thresholds") or {"yellow": 80, "red": 95}
    providers = await _event_store.get_quota_status(quota_cfg)
    return {"providers": providers, "alert_thresholds": thresholds}


@app.get("/api/monitor/agents-status")
async def get_agents_status():
    """聚合所有 agent 的实时状态（供监控中心 AgentStatusGrid）。

    数据来源：
      - config agents（_get_all_agents）→ 基本身份信息
      - audit.db active_runs → 实时 running/active 任务
      - audit.db get_agent_stats → 历史运行统计

    Returns:
        {
            "agents": [AgentStatus],
            "running_count": int
        }
    """
    if _event_store is None:
        raise HTTPException(503, "EventStore not initialized")

    agents_map = _get_all_agents()
    active_runs = await _event_store.list_active_runs()
    # 按 agent_id 分组 active run（一个 agent 可能多个并发 run）
    runs_by_agent: dict[str, list[dict[str, Any]]] = {}
    for r in active_runs:
        aid = r.get("agent_id") or ""
        # templated/hybrid 模式下 agent_id 可能为 NULL，从 workflow_id 反查绑定
        if not aid:
            wf_id = r.get("workflow_id") or ""
            for aid_guess, agent_obj in agents_map.items():
                wf_bindings = _compute_agent_workflow_bindings(aid_guess)
                if any(wb["workflow_id"] == wf_id for wb in wf_bindings):
                    aid = aid_guess
                    break
        runs_by_agent.setdefault(aid, []).append(r)

    agents_out: list[dict[str, Any]] = []
    running_total = 0
    for aid, agent_data in agents_map.items():
        active = runs_by_agent.get(aid, [])
        is_running = len(active) > 0
        running_total += len(active)

        # 当前任务（取最早开始的一个，若有多个则展示第一个）
        current_task: dict[str, Any] | None = None
        if active:
            first = active[0]
            current_node = await _event_store.get_current_node(first["run_id"]) if first.get("run_id") else None
            current_task = {
                "run_id": first.get("run_id"),
                "workflow_id": first.get("workflow_id"),
                "started_at": first.get("started_at"),
                "current_node": current_node,
            }

        # agent 历史统计（按 agent_id + workflow 绑定聚合）
        wf_bindings = _compute_agent_workflow_bindings(aid)
        wf_ids = [wb["workflow_id"] for wb in wf_bindings]
        stats = await _event_store.get_agent_stats(aid, workflow_ids=wf_ids)
        last_status = stats.get("last_run_status") or ""
        # error 判定：上次运行失败且当前无运行
        is_error = (not is_running) and last_status in ("failed", "cancelled")

        status_str = "running" if is_running else ("error" if is_error else "idle")

        # model 字段从 agent_data.model 字典取
        model_cfg = agent_data.get("model") or {}
        model_str = ""
        if isinstance(model_cfg, dict):
            model_str = model_cfg.get("id") or model_cfg.get("model") or ""

        agents_out.append({
            "agent_id": aid,
            "display_name": agent_data.get("name") or aid,
            "domain": agent_data.get("domain") or "",
            "harness": agent_data.get("harness") or "",
            "model": model_str,
            "status": status_str,
            "running_tasks": len(active),
            "current_task": current_task,
            "stats": {
                "total_runs": stats.get("total_runs", 0),
                "completed": stats.get("completed", 0),
                "failed": stats.get("failed", 0),
                "last_run_at": stats.get("last_run_at"),
                "last_run_status": stats.get("last_run_status"),
            },
        })

    return {"agents": agents_out, "running_count": running_total}


@app.get("/api/monitor/tips")
async def list_tips(limit: int = 20):
    """查询最近的 tips 列表（从 _global_alerts 取最近 N 条）。

    Args:
        limit: 返回条数上限（默认 20，最大 100）

    Returns:
        {"tips": [Tip]}   # 按 emitted_at 降序
    """
    limit = max(1, min(limit, 100))
    # _global_alerts 末尾追加 → 倒序取最近 N 条
    recent = list(reversed(_global_alerts))[:limit]
    return {"tips": [_normalize_tip(t) for t in recent]}


@app.get("/api/monitor/tips-stream")
async def tips_stream(request: Request):
    """聚合 tips SSE 流：实时推送 patrol_alert / emit_alert 工具推送的 tip。

    订阅机制：
      - 创建 asyncio.Queue 加入 _tip_subscribers
      - 先推送历史最近 20 条 tip（让前端初始化有内容）
      - 循环 await queue.get()，15s 超时发心跳
      - 客户端断开时从 _tip_subscribers 移除队列
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _tip_subscribers.add(queue)

    async def event_generator() -> AsyncIterator[str]:
        try:
            # 先发历史最近 20 条（让前端首次连接即能看到历史告警）
            recent = list(reversed(_global_alerts))[:20]
            for tip_raw in reversed(recent):
                tip = _normalize_tip(tip_raw)
                yield f"event: tip\ndata: {json.dumps(tip, ensure_ascii=False, default=str)}\n\n"
            # 实时循环
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if ev is None:
                    break
                tip = _normalize_tip(ev)
                yield f"event: tip\ndata: {json.dumps(tip, ensure_ascii=False, default=str)}\n\n"
        finally:
            _tip_subscribers.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 手动验收 API（视频质检）──────────────────────────────────────────


class ValidateRunPayload(BaseModel):
    """手动验收请求。"""
    mp4_path: str | None = None         # 可选：覆盖默认 output.mp4 路径
    target_duration: int | None = None  # 可选：覆盖目标时长
    topic: str = ""                      # 可选：主题


@app.post("/api/agent/runs/{run_id}/validate")
async def validate_run(run_id: str, payload: ValidateRunPayload | None = None):
    """手动触发视频质检（重新跑 quality_inspector agent）。

    机制：
      - 复用原 run_id 的 workspace（workspace/{wf_id}/{run_id}/output.mp4）
      - 清除 video_validate 节点输出文件，强制重跑（参考 resume_run 的 node_id 机制）
      - 通过 orchestrator.resume() 触发断点恢复执行
      - 若原 run_id 不存在或 workflow 不是 video-pipeline，返回 404

    Args:
        run_id: 原 video-pipeline run_id
        payload: 可选覆盖参数（mp4_path/target_duration/topic）

    Returns:
        {"run_id": "run_xxx", "stream_url": "/api/agent/runs/run_xxx/events"}
    """
    if _orchestrator is None or _event_store is None:
        raise HTTPException(503, "Orchestrator/EventStore 未初始化")
    # 查原 run 的 workflow_id
    summary = await _event_store.get_run_summary(run_id)
    if not summary:
        raise HTTPException(404, f"Run 不存在: {run_id}")
    workflow_id = summary.get("workflow_id") or ""
    if workflow_id != "video-pipeline":
        raise HTTPException(400, f"Run 不是 video-pipeline: workflow_id={workflow_id}")
    if workflow_id not in _orchestrator.workflows:
        raise HTTPException(404, f"Workflow 未加载: {workflow_id}")

    wf = _orchestrator.workflows[workflow_id]

    # 构造 resume inputs（覆盖 mp4 路径 / target_duration / topic）
    inputs: dict[str, Any] = {}
    if payload and payload.target_duration is not None:
        inputs["target_duration"] = payload.target_duration
    if payload and payload.topic:
        inputs["topic"] = payload.topic

    # 清除 video_validate 节点输出文件，强制重跑（参考 resume_run 的 node_id 机制）
    node_id = "video_validate"
    ws_root = getattr(wf, "workspace_root", None)
    if ws_root:
        node_dir = Path(ws_root) / run_id
        if node_dir.exists():
            downstream = _get_downstream_nodes(wf, node_id)
            for nid in [node_id] + downstream:
                for pattern in [f"{nid}*", f"*/{nid}*"]:
                    for f in node_dir.rglob(pattern):
                        f.unlink(missing_ok=True)
                        logger.info("清除节点 %s 输出文件: %s", nid, f)

    # 通过 orchestrator.resume() 触发断点恢复执行
    try:
        handle = await _orchestrator.resume(
            run_id=run_id,
            workflow_id=workflow_id,
            inputs=inputs,
        )
    except Exception as e:
        logger.error("手动验收触发失败 run_id=%s: %s", run_id, e)
        raise HTTPException(500, f"触发验收失败: {e}")

    # 初始化事件流队列（复用 run_id）
    if run_id not in _event_streams:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        _event_streams[run_id] = queue
    else:
        while not _event_streams[run_id].empty():
            _event_streams[run_id].get_nowait()

    last_seq: dict[str, int] = {"seq": 0}

    async def _validate_bridge():
        try:
            async for ev in _orchestrator.stream_events(run_id):
                if isinstance(ev, DagEvent):
                    if _event_store:
                        await _event_store.append_run_event(run_id=ev.run_id or run_id, event_type=ev.type.value if hasattr(ev.type, "value") else str(ev.type), payload=ev.payload or {}, node_id=ev.node_id)
                    seq = ev.sequence
                    if seq <= last_seq["seq"]:
                        continue
                    last_seq["seq"] = seq
                elif isinstance(ev, RawHarnessEvent):
                    if _event_store:
                        await _event_store.append_raw_event(ev)
                await _event_streams[run_id].put(ev)
        finally:
            if _event_store and _orchestrator:
                try:
                    state = await _orchestrator.get_run(run_id)
                    if state:
                        await _event_store.finalize_run(
                            run_id=run_id,
                            status=state.status.value,
                            finished_at=state.finished_at,
                            total_tokens_in=state.total_tokens_input,
                            total_tokens_out=state.total_tokens_output,
                            total_cost_usd=state.total_cost_usd,
                            error=state.error,
                            final_outputs=state.node_outputs,
                        )
                except Exception as e:
                    logger.warning("finalize_run 失败: %s", e)
            await _event_streams[run_id].put(None)

    asyncio.create_task(_validate_bridge())

    return {
        "run_id": handle.run_id,
        "stream_url": f"/api/agent/runs/{handle.run_id}/events",
    }


def _sync_opencode_credentials() -> None:
    """从 CredentialStore 注入 provider API key 到 opencode.json。

    opencode server 是独立进程，只读 ~/.config/opencode/opencode.json，
    不认 AgentOps 的 CredentialStore（~/.agentops/credentials.db）。
    本函数在 lifespan 启动时调用，把 CredentialStore 中前端配置的 provider key
    写入 opencode.json 的 apiKey 字段，消除两套凭证体系割裂。

    不覆盖同名环境变量已生效的值（${VAR} 占位符保留），
    只填充 CredentialStore 有、但 opencode.json 中仍是占位符的字段。
    """
    import re
    from orchestrator.credential_store import get_credential_store

    opencode_config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    if not opencode_config_path.exists():
        logger.info("opencode.json not found at %s, skip credential sync", opencode_config_path)
        return

    try:
        store = get_credential_store()
    except Exception as e:
        logger.warning("CredentialStore 初始化失败，跳过 opencode 凭证同步: %s", e)
        return

    try:
        with open(opencode_config_path, "r", encoding="utf-8") as f:
            oc = json.load(f)
    except Exception as e:
        logger.warning("读取 opencode.json 失败: %s", e)
        return

    providers = oc.get("provider", {})
    updated = False

    for pid, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            continue
        options = pcfg.get("options", {}) or {}
        api_key = options.get("apiKey", "")
        if not api_key or not isinstance(api_key, str):
            continue
        # 如果已经是真实 key（不以 ${ 开头），说明已手动填写，跳过
        if not re.match(r'^\$\{', api_key.strip()):
            continue
        # 从 CredentialStore 读
        stored_key = store.get(pid)
        if stored_key:
            options["apiKey"] = stored_key
            pcfg["options"] = options
            updated = True
            logger.info("opencode credential synced: provider=%s from CredentialStore", pid)

    if updated:
        try:
            tmp = str(opencode_config_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(oc, f, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp)
            tmp_path.replace(opencode_config_path)
            logger.info("opencode.json updated with %d provider credentials", sum(1 for _ in providers if True))
        except Exception as e:
            logger.warning("写入 opencode.json 失败: %s", e)
    else:
        logger.debug("opencode.json credential sync: no updates needed")


# ====== Thread 模式 Session API（v2 新增）======
# 参考：docs/architecture/DESIGN_thread_session_refactor.md

from orchestrator.session_engine import SessionEngine
from orchestrator.local_sdk import _resolve_harness_type
from uuid import uuid4 as _uuid4

# Thread 模式 SSE 多消费者广播
def _session_subscribe(session_id: str) -> asyncio.Queue:
    """订阅 session 事件流（每个 SSE 连接一个独立 Queue）。"""
    if session_id not in _session_event_streams:
        _session_event_streams[session_id] = set()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _session_event_streams[session_id].add(queue)
    return queue

def _session_unsubscribe(session_id: str, queue: asyncio.Queue) -> None:
    """取消订阅。"""
    if session_id in _session_event_streams:
        _session_event_streams[session_id].discard(queue)
        if not _session_event_streams[session_id]:
            del _session_event_streams[session_id]

async def _session_broadcast(session_id: str, event: dict) -> None:
    """广播事件到所有订阅者。"""
    subscribers = _session_event_streams.get(session_id, set())
    for queue in list(subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("session SSE queue full, dropping event for %s", session_id)

async def _session_event_sink(session_id: str, ev: "DagEvent") -> None:
    """SessionEngine 的 event_sink：落库 + SSE 广播。"""
    # 落库到 session_events
    if _event_store:
        try:
            # 注入 surface_state 到 payload（让 DB 也存全字段，回放能拿到 patch_sequence 等）
            payload_for_db = dict(ev.payload or {})
            if hasattr(ev, "surface_state") and ev.surface_state and "surface_state" not in payload_for_db:
                payload_for_db["surface_state"] = ev.surface_state.to_payload()
            await _event_store.append_session_event(
                session_id, ev.type.value, payload_for_db,
                node_id=ev.node_id,
            )
        except Exception as e:
            logger.warning("session_events 落库失败: %s", e)

    # 广播到 SSE
    payload = {
        "type": ev.type.value,
        "session_id": session_id,
        "node_id": ev.node_id,
        "payload": ev.payload,
        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
        "sequence": ev.sequence,
    }
    await _session_broadcast(session_id, payload)

    # 同步 usage 到 sessions 表
    if ev.type == DagEventType.TURN_COMPLETED and _event_store:
        p = ev.payload or {}
        try:
            await _event_store.update_session_status(
                session_id, "active", last_activity=False,
            )
        except Exception:
            pass


class CreateSessionPayload(BaseModel):
    agent_id: str = "manager"
    title: str | None = None
    # P0.18.7b: 指定授权 workspace（None=通用对话）
    workspace_id: str | None = None


class TurnPayload(BaseModel):
    message: str


@app.post("/api/v2/sessions")
async def v2_create_session(payload: CreateSessionPayload):
    """创建新 Session（Thread 模式）。"""
    if not _orchestrator:
        raise HTTPException(500, "orchestrator not initialized")

    session_id = f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{_uuid4().hex[:6]}"

    # 从配置读 agent 信息
    try:
        from orchestrator.config_loader import get_system_config
        cfg = get_system_config()
        agent_def = cfg.agents.get(payload.agent_id)
        if not agent_def:
            raise HTTPException(404, f"Agent not found: {payload.agent_id}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"配置加载失败: {e}")

    # 落库（P0.18.7b: session-workspace 关联）
    # manager 默认工作区 fallback：payload 未指定时读 system_settings
    effective_workspace_id = payload.workspace_id
    if effective_workspace_id is None and payload.agent_id == "manager":
        if _event_store:
            default_ws_id = await _event_store.get_setting("manager_default_workspace_id")
            if default_ws_id:
                default_ws = await _event_store.get_authorized_workspace(default_ws_id)
                if default_ws and default_ws.get("enabled"):
                    effective_workspace_id = default_ws_id

    # P0.18.10: tier 兼容性校验（agent tier ≤ workspace tier）
    # 防止 T3 agent 绑定 read_only(T1) workspace 造成权限语义不一致
    # 注意：此处校验的是 workspace 注册时的默认权限，会话级权限可独立切换
    effective_permission_level: str | None = None
    if effective_workspace_id and _event_store:
        ws_row = await _event_store.get_authorized_workspace(effective_workspace_id)
        if ws_row:
            from orchestrator.workspace_paths import WorkspaceInfo, tier_compatible
            ws_info = WorkspaceInfo.from_row(ws_row)
            agent_tier = getattr(agent_def, "tier", "T2")
            # 初始化会话权限级别 = workspace 注册权限（之后可独立切换）
            effective_permission_level = ws_info.permissions
            if not tier_compatible(ws_info.tier, agent_tier):
                _PERMS_TO_TIER = {"read_only": "T1", "read_write": "T2", "read_write_exec": "T3", "full_access": "T4"}
                needed = next(
                    (p for p, t in _PERMS_TO_TIER.items() if t == agent_tier),
                    "read_write_exec",
                )
                raise HTTPException(409, (
                    f"Agent '{payload.agent_id}' tier={agent_tier} 超过 workspace "
                    f"'{ws_info.display_name}' permissions tier={ws_info.tier}（{ws_info.permissions}）；"
                    f"请将该 workspace 权限升级到 {needed}"
                ))

    if _event_store:
        await _event_store.create_session(
            session_id=session_id,
            agent_id=payload.agent_id,
            title=payload.title or "",
            workspace_id=effective_workspace_id,
            permission_level=effective_permission_level,
        )

    # 创建 SSE queue
    _session_event_streams[session_id] = set()

    # emit session.created（P0.18.7b: payload 带 workspace_id 供前端 workspace 状态条渲染）
    await _session_event_sink(session_id, DagEvent(
        type=DagEventType.SESSION_CREATED,
        run_id=session_id,
        payload={"agent_id": payload.agent_id, "title": payload.title, "workspace_id": effective_workspace_id},
        sequence=0,
    ))

    return {"session_id": session_id, "stream_url": f"/api/v2/sessions/{session_id}/events"}


class UpdatePermissionLevelPayload(BaseModel):
    """切换会话级权限级别（与 workspace 解耦，随时可切换、立即生效）。"""
    permission_level: str


@app.patch("/api/v2/sessions/{session_id}/permission")
async def v2_update_session_permission(session_id: str, payload: UpdatePermissionLevelPayload):
    """切换会话权限级别（Read Only / Workspace Write / Full Access）。

    权限与工作区独立：同一工作区下不同会话可有不同权限级别。
    切换后立即生效——当前 turn 的后续工具调用实时读取新权限。
    """
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    from orchestrator.workspace_paths import VALID_PERMISSION_LEVELS
    if payload.permission_level not in VALID_PERMISSION_LEVELS:
        raise HTTPException(400, (
            f"invalid permission_level: {payload.permission_level}, "
            f"must be one of {VALID_PERMISSION_LEVELS}"
        ))
    session = await _event_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session not found: {session_id}")
    await _event_store.update_session_permission_level(session_id, payload.permission_level)
    # 通过 SSE 通知前端权限已变更
    await _session_event_sink(session_id, DagEvent(
        type=DagEventType.SESSION_CREATED,
        run_id=session_id,
        payload={"permission_level": payload.permission_level, "action": "permission_changed"},
        sequence=0,
    ))
    return {"session_id": session_id, "permission_level": payload.permission_level}


class ApprovalDecisionPayload(BaseModel):
    """用户对审批请求的决定（deepseek 闭集：只有授权/拒绝可由用户主动给出）。"""
    outcome: str  # "allowed-once" | "rejected"


@app.post("/api/v2/approvals/{request_id}/decide")
async def v2_decide_approval(request_id: str, payload: ApprovalDecisionPayload):
    """审批决定端点：前端弹窗「允许本次 / 拒绝」调用。

    allowed-once 语义：只放行被问的那一次工具调用，不改变会话权限级别。
    未知/已完结的 request_id 返回 404（迟到决定被丢弃，重复点击无害）。
    """
    from orchestrator.approval import DECIDABLE_OUTCOMES
    if payload.outcome not in DECIDABLE_OUTCOMES:
        raise HTTPException(400, (
            f"invalid outcome: {payload.outcome}, must be one of {DECIDABLE_OUTCOMES}"
        ))
    service = _get_approval_service()
    ok = service.decide(request_id, payload.outcome)
    if not ok:
        raise HTTPException(404, f"Approval request not found or already settled: {request_id}")
    return {"request_id": request_id, "outcome": payload.outcome}


@app.post("/api/v2/sessions/{session_id}/turns")
async def v2_send_turn(session_id: str, payload: TurnPayload):
    """发送一轮对话消息（Thread 模式）。"""
    if not _orchestrator:
        raise HTTPException(500, "orchestrator not initialized")
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")

    # 检查 session 是否存在
    session = await _event_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session not found: {session_id}")

    agent_id = session.get("agent_id") or "manager"

    # 获取或创建 SessionEngine
    engine = _session_engines.get(session_id)
    if engine is None:
        harness_type = _resolve_harness_type(agent_id)

        # 读 agent system_prompt + tier
        system_prompt = ""
        agent_tier = "T2"
        try:
            from orchestrator.config_loader import get_system_config
            cfg = get_system_config()
            agent_def = cfg.agents.get(agent_id)
            if agent_def and agent_def.system_prompt:
                system_prompt = agent_def.system_prompt
            if agent_def and getattr(agent_def, "tier", None):
                agent_tier = agent_def.tier
        except Exception:
            pass

        # 构造 llm_config
        llm_cfg = _orchestrator.llm_config

        # 构造 CrossDomainCoordinator
        from orchestrator.cross_domain import CrossDomainCoordinator
        coordinator = CrossDomainCoordinator(
            llm_config=llm_cfg,
            event_sink=lambda ev: _session_event_sink(session_id, ev),
            permission_engine=_orchestrator._permission_engine,
        )

        # P0.18.10: 传入 workspace_id + agent_tier（tier 拦截链路接线）
        session_workspace_id = session.get("workspace_id")

        engine = SessionEngine(
            session_id=session_id,
            agent_id=agent_id,
            llm_config=llm_cfg,
            event_sink=lambda ev: _session_event_sink(session_id, ev),
            harness_type=harness_type,
            system_prompt=system_prompt,
            event_store=_event_store,
            cross_domain_coordinator=coordinator,
            workspace_id=session_workspace_id,
            agent_tier=agent_tier,
            # P2：fail-closed 审批服务（tier 不足时向用户请求 allowed-once 放行）
            approval_service=_get_approval_service(),
        )
        _session_engines[session_id] = engine

    # 启动 turn（后台 task）
    asyncio.create_task(engine.start_turn(payload.message))

    # 若 session 没有 title，用首条消息的前 50 字作为摘要标题
    if session.get("title") == "" and payload.message:
        snippet = (payload.message or "").strip().replace("\n", " ")
        if len(snippet) > 50:
            snippet = snippet[:47] + "..."
        asyncio.ensure_future(_event_store.update_session_title(session_id, snippet))

    return {"session_id": session_id, "status": "turn_started"}


@app.get("/api/v2/sessions/{session_id}/events")
async def v2_stream_session_events(session_id: str, request: Request):
    """SSE 事件流（Thread 模式，多消费者）。

    先重放历史事件（session_events 表），再推送实时事件。
    避免 SSE 连接晚于事件产生导致丢失。
    """
    queue = _session_subscribe(session_id)

    async def event_generator():
        try:
            # 1. 重放历史事件（统一格式：type/run_id/node_id/payload/sequence）
            if _event_store:
                try:
                    history = await _event_store.get_session_events(session_id)
                    for row in history:
                        raw_payload = row.get("payload")
                        if isinstance(raw_payload, str):
                            try:
                                raw_payload = json.loads(raw_payload)
                            except Exception:
                                pass
                        sse_ev = {
                            "type": row.get("event_type", ""),
                            "run_id": row.get("session_id", session_id),
                            "session_id": row.get("session_id", session_id),
                            "node_id": row.get("node_id"),
                            "payload": raw_payload,
                            "occurred_at": row.get("occurred_at"),
                            "sequence": row.get("sequence", 0),
                        }
                        yield f"data: {json.dumps(sse_ev, ensure_ascii=False, default=str)}\n\n"
                except Exception as e:
                    logger.warning("重放 session events 失败: %s", e)

            # 2. 实时推送
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if ev is None:  # sentinel
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        finally:
            _session_unsubscribe(session_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/v2/sessions/{session_id}/cancel")
async def v2_cancel_session(session_id: str):
    """取消当前 turn。"""
    engine = _session_engines.get(session_id)
    if engine:
        await engine.cancel("user_cancelled")
    return {"status": "cancel_requested", "session_id": session_id}


@app.get("/api/v2/sessions/{session_id}/messages")
async def v2_get_session_messages(session_id: str, limit: int = 1000):
    """获取 session 消息历史。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    messages = await _event_store.get_session_messages(session_id, limit=limit)
    return {"session_id": session_id, "messages": messages, "total": len(messages)}


@app.get("/api/v2/sessions/{session_id}")
async def v2_get_session(session_id: str):
    """获取 session 元数据。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    session = await _event_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session not found: {session_id}")
    return session


@app.get("/api/v2/sessions/{session_id}/runs")
async def v2_list_session_runs(session_id: str):
    """列出 session 关联的所有子 Run（Thread 模式 v2）。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    runs = await _event_store.list_child_runs_of_session(session_id)
    return {"session_id": session_id, "runs": runs, "total": len(runs)}


@app.get("/api/v2/sessions/{session_id}/memory")
async def v2_list_session_memory(session_id: str, limit: int = 20):
    """查询 session 中期记忆（Thread 模式 v2）。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    memories = await _event_store.list_session_memory(session_id, limit=limit)
    return {"session_id": session_id, "memories": memories, "total": len(memories)}


@app.post("/api/v2/sessions/{session_id}/widget-input")
async def v2_widget_input(session_id: str, payload: WidgetInputPayload):
    """提交 widget 交互输入（Thread 模式 v2）。

    复用 _orchestrator.submit_widget_input，但用 session_id 路由：
    SessionEngine 主循环阻塞在 chat_input/hil_queue，submit_widget_input
    会把输入送到对应 run 的队列（session 内当前活跃 run）。
    """
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    # 落库 HIL 介入点（v2 模式：session_id 同时作为 run_id；FK 失败不阻塞核心转发）
    if _event_store:
        try:
            await _event_store.append_widget_input(
                run_id=session_id,
                session_id=session_id,
                widget_id=payload.widget_id,
                payload=payload.input,
                user_id="frontend",
            )
        except Exception as e:
            logger.warning("[v2] append_widget_input 落库失败（不阻塞）: %s", e)
    # v2 Thread 模式：直接走 _session_engines，不走 _orchestrator（那是 v1 路径）
    engine = _session_engines.get(session_id)
    if engine is not None:
        try:
            engine.submit_widget_input(payload.widget_id, payload.input)
            logger.info("[v2] widget_input 转发到 SessionEngine: session=%s widget=%s",
                        session_id, payload.widget_id)
        except Exception as e:
            logger.warning("[v2] SessionEngine.submit_widget_input failed: %s", e)
    else:
        # fallback：v1 路径（DAG run 或 v1 会话 SessionEngine）
        try:
            await _orchestrator.submit_widget_input(session_id, payload.widget_id, payload.input)
        except Exception as e:
            logger.warning("[v2] fallback submit_widget_input failed: %s", e)
    # 广播 widget.input 事件到所有 SSE 订阅者（让前端 UI 立即响应）
    synthetic = {
        "type": DagEventType.WIDGET_INPUT.value,
        "run_id": session_id,
        "session_id": session_id,
        "node_id": None,
        "payload": {"widget_id": payload.widget_id, "input": payload.input, "user_id": "frontend"},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "sequence": 0,
    }
    await _session_broadcast(session_id, synthetic)
    return {"status": "accepted", "session_id": session_id, "widget_id": payload.widget_id}


@app.get("/api/v2/sessions")
async def v2_list_sessions(limit: int = 50, offset: int = 0, status: str | None = None):
    """列出所有 session。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    sessions = await _event_store.list_sessions(
        status=status, limit=limit, offset=offset,
    )
    total = await _event_store.count_sessions(status=status)
    return {"sessions": sessions, "count": len(sessions), "total": total}


@app.get("/api/v2/sessions/{session_id}/events/audit")
async def v2_get_session_events_audit(session_id: str, since: int = 0, limit: int = 10000):
    """获取 session 历史事件（从 audit.db 查）。"""
    if not _event_store:
        raise HTTPException(500, "event_store not initialized")
    events = await _event_store.get_session_events(session_id, since=since, limit=limit)
    return {"session_id": session_id, "events": events, "total": len(events)}


# ====== 任务管理模块 API（P0） ======
# 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.7

def _get_task_orchestrator():
    """获取 TaskOrchestrator（从 app.state 或 _registry）。"""
    from orchestrator._registry import get_task_orchestrator
    orch = get_task_orchestrator()
    if orch is None:
        raise HTTPException(500, "task_orchestrator not initialized")
    return orch


@app.get("/api/tasks/projects")
async def task_list_projects():
    """项目列表。"""
    orch = _get_task_orchestrator()
    projects = await orch.store.list_projects()
    return {"projects": projects}


@app.post("/api/tasks/projects")
async def task_create_project_endpoint(payload: dict):
    """创建项目。"""
    orch = _get_task_orchestrator()
    from datetime import datetime, timezone
    project_id = f"proj_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    result = await orch.create_project(
        project_id=project_id,
        name=payload.get("name", ""),
        type=payload.get("type", "code"),
        local_path=payload.get("local_path", ""),
        workspace_id=payload.get("workspace_id", ""),
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "create_failed"))
    return result["project"]


@app.get("/api/tasks/ideas")
async def task_list_ideas(project_id: str = "", status: str = ""):
    """idea 列表。"""
    orch = _get_task_orchestrator()
    ideas = await orch.store.list_ideas(project_id=project_id, status=status)
    return {"ideas": ideas}


@app.post("/api/tasks/ideas")
async def task_submit_idea(payload: dict):
    """提交 idea（手动录入 status=open，自动接入 status=draft）。"""
    orch = _get_task_orchestrator()
    idea = await orch.store.submit_idea(
        project_id=payload.get("project_id", ""),
        content=payload.get("content", ""),
        source=payload.get("source", "manual"),
        source_ref=payload.get("source_ref", ""),
        tags=payload.get("tags"),
        auto_draft=payload.get("auto_draft", False),
    )
    return idea


@app.get("/api/tasks/relations")
async def task_list_relations(task_id: str = ""):
    """依赖关系列表（供依赖图）。"""
    orch = _get_task_orchestrator()
    rels = await orch.store.list_relations(task_id=task_id)
    return {"relations": rels}


@app.post("/api/tasks/relations")
async def task_add_relation(payload: dict):
    """添加依赖关系（环检测）。"""
    orch = _get_task_orchestrator()
    r = await orch.store.add_relation(
        source_task_id=payload.get("source_task_id", ""),
        target_task_id=payload.get("target_task_id", ""),
        relation_type=payload.get("relation_type", "blocks"),
    )
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "add_failed"))
    return r


@app.get("/api/tasks/proposals")
async def task_list_proposals(task_id: str = "", status: str = ""):
    """文档变更提案列表。"""
    orch = _get_task_orchestrator()
    proposals = await orch.store.list_doc_proposals(task_id=task_id, status=status)
    return {"proposals": proposals}


@app.get("/api/tasks/revision")
async def task_get_revision():
    """全局版本号（前端轮询判断刷新）。

    注意：必须注册在 /api/tasks/{task_id} 之前，否则 "revision" 会被当作 task_id 匹配。
    """
    orch = _get_task_orchestrator()
    revision = await orch.store.get_revision()
    return {"revision": revision}


@app.get("/api/tasks")
async def task_list(project_id: str = "", status: str = "", assignee_id: str = ""):
    """任务列表（project_id/status/assignee 过滤）。"""
    orch = _get_task_orchestrator()
    tasks = await orch.store.list_tasks(project_id=project_id, status=status,
                                        assignee_id=assignee_id)
    revision = await orch.store.get_revision()
    return {"tasks": tasks, "revision": revision}


@app.get("/api/tasks/search")
async def task_search(q: str, project_id: str = "", status: str = "",
                      limit: int = 50):
    """V2-W4：任务全文检索（FTS5）。

    必须注册在 /api/tasks/{task_id} 之前，否则 "search" 会被当作 task_id 匹配。
    """
    orch = _get_task_orchestrator()
    tasks = await orch.store.search_tasks(q, project_id=project_id,
                                           status=status, limit=limit)
    return {"tasks": tasks, "query": q, "count": len(tasks)}


# ============================================================
# V3 终端注册表 / 布局（Coding 终端页，设计文档 §4.13）
# 静态路径必须注册在 /api/tasks/{task_id} 之前，避免被当作 task_id 匹配
# ============================================================

@app.get("/api/tasks/terminal/sessions")
async def task_terminal_list_sessions():
    """列出终端会话（Coding 终端页窗格数据源，§4.13.4）。

    agent 窗格附带任务摘要（标题/状态），供窗格头部展示。
    """
    orch = _get_task_orchestrator()
    sessions = await orch.store.list_terminal_sessions()
    enriched: list[dict] = []
    for s in sessions:
        item = dict(s)
        if s.get("task_id"):
            t = await orch.store.get_task(s["task_id"])
            if t:
                item["task_title"] = t.get("title", "")
                item["task_status"] = t.get("status", "")
        enriched.append(item)
    return {"sessions": enriched}


@app.post("/api/tasks/terminal/sessions")
async def task_terminal_create_session(payload: dict):
    """手动新建终端窗口（codex/claude/shell，§4.13.3）。

    创建 psmux/tmux 会话 + 注册 terminal_sessions 表；
    codex/claude 类型自动发送启动命令。
    """
    orch = _get_task_orchestrator()
    kind = payload.get("kind", "shell")
    if kind not in ("codex", "claude", "shell"):
        raise HTTPException(400, f"kind must be one of codex/claude/shell, got {kind}")

    terminal_session_id = f"manual_{uuid.uuid4().hex[:8]}"
    terminal_mgr = getattr(app.state, "task_terminal", None)
    if terminal_mgr is not None:
        try:
            backend_name = getattr(terminal_mgr, "backend_name", "")
            if kind in ("codex", "claude") and backend_name == "conpty_host":
                # ConPTY：直接在业务内终端窗格里跑 claude/codex TUI
                # （会话由独立 host 进程持有，后端重启不丢，可交互）
                await terminal_mgr.create_session(
                    terminal_session_id, cwd=os.getcwd(),
                    command=["cmd.exe", "/q", "/d", "/c", kind])
            else:
                # 其他后端：先进 shell，再敲 kind 启动（psmux/tmux 伪 TTY 可跑 TUI）
                await terminal_mgr.create_session(terminal_session_id,
                                                  cwd=os.getcwd())
                if kind in ("codex", "claude"):
                    if backend_name == "subprocess":
                        # 管道无 TTY：TUI 起不来，弹独立窗口降级（有真实 TTY）
                        ps = (
                            "powershell -NoProfile -Command \""
                            f"Start-Process cmd -ArgumentList '/k','{kind}' "
                            f"-WorkingDirectory '{os.getcwd()}'\""
                        )
                        await terminal_mgr.send_keys(terminal_session_id, ps)
                    else:
                        await terminal_mgr.send_keys(terminal_session_id, kind)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"terminal backend error: {e}")

    session = await orch.store.register_terminal_session(
        terminal_session_id=terminal_session_id, kind=kind)
    return session


@app.delete("/api/tasks/terminal/sessions/{terminal_session_id}")
async def task_terminal_close_session(terminal_session_id: str):
    """关闭终端会话/移除窗格：kill 进程 + 物理删除记录。

    删除记录（而非仅置 dead）的原因：SSE 每 1.5s 推送 DB 全量会话列表，
    死记录若保留会被前端重新上屏，导致「移除窗格」永远不生效。
    """
    orch = _get_task_orchestrator()
    terminal_mgr = getattr(app.state, "task_terminal", None)
    if terminal_mgr is not None:
        try:
            await terminal_mgr.destroy_session(terminal_session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("destroy_session failed (%s): %s", terminal_session_id, e)
    session = await orch.store.delete_terminal_session(terminal_session_id)
    if not session:
        raise HTTPException(404, f"terminal session {terminal_session_id} not found")
    return session


@app.get("/api/tasks/terminal/sessions/{terminal_session_id}/pane")
async def task_terminal_capture_pane(terminal_session_id: str):
    """拉取窗格当前内容（前端轮询渲染，手动窗口无 task_id 也可用）。"""
    terminal_mgr = getattr(app.state, "task_terminal", None)
    if terminal_mgr is None:
        return {"content": "", "terminal_session_id": terminal_session_id}
    try:
        content = await terminal_mgr.capture_pane(terminal_session_id)
    except Exception as e:  # noqa: BLE001
        return {"content": "", "error": str(e), "terminal_session_id": terminal_session_id}
    return {"content": content, "terminal_session_id": terminal_session_id}


@app.post("/api/tasks/terminal/sessions/{terminal_session_id}/keys")
async def task_terminal_send_keys(terminal_session_id: str, payload: dict):
    """向窗格发送命令（回车自动附加）。"""
    terminal_mgr = getattr(app.state, "task_terminal", None)
    if terminal_mgr is None:
        raise HTTPException(503, "terminal manager not available")
    keys = str(payload.get("keys", ""))
    if not keys.strip():
        raise HTTPException(400, "keys required")
    try:
        await terminal_mgr.send_keys(terminal_session_id, keys)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"send_keys failed: {e}")
    return {"ok": True}


@app.get("/api/tasks/terminal/layout")
async def task_terminal_get_layout():
    """读取当前用户窗格布局。"""
    orch = _get_task_orchestrator()
    layout = await orch.store.get_terminal_layout("local")
    return layout or {"user_id": "local", "panes": []}


@app.put("/api/tasks/terminal/layout")
async def task_terminal_save_layout(payload: dict):
    """保存当前用户窗格布局。"""
    orch = _get_task_orchestrator()
    panes = payload.get("panes", [])
    if not isinstance(panes, list):
        raise HTTPException(400, "panes must be a list")
    return await orch.store.save_terminal_layout(user_id="local", panes=panes)


@app.get("/api/tasks/terminal/stream")
async def task_terminal_page_stream(request: Request):
    """Coding 终端页 SSE：会话注册表快照变更推送（新窗格自动上屏，§4.13.4）。

    事件格式：
        data: {"type":"sessions","sessions":[...]}\n\n
    前端收到新会话即新增窗格；会话状态变化（active→done/dead）即更新窗格。
    """
    orch = _get_task_orchestrator()

    async def event_generator():
        last_snapshot = ""
        try:
            while True:
                if await request.is_disconnected():
                    break
                sessions = await orch.store.list_terminal_sessions()
                snapshot = json.dumps(sessions, sort_keys=True, default=str)
                if snapshot != last_snapshot:
                    payload = {"type": "sessions", "sessions": sessions}
                    yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    last_snapshot = snapshot
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("terminal page stream ended: %s", e)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/tasks/graph")
async def task_graph(project_id: str = ""):
    """V3 项目级网状图数据（设计文档 §4.11.4 X9）：项目全部任务 + 关系。

    必须注册在 /api/tasks/{task_id} 之前，否则 "graph" 会被当作 task_id 匹配。
    """
    orch = _get_task_orchestrator()
    tasks = await orch.store.list_tasks(project_id=project_id)
    task_ids = {t["task_id"] for t in tasks}
    all_relations = await orch.store.list_relations()
    # 只保留两端都在本项目内的关系（跨项目关系不渲染）
    relations = [r for r in all_relations
                 if r.get("source_task_id") in task_ids
                 and r.get("target_task_id") in task_ids]
    return {"tasks": tasks, "relations": relations}


@app.get("/api/tasks/dashboard")
async def task_dashboard(project_id: str = ""):
    """V3 仪表盘聚合查询（设计文档 §4.11.2），单接口避免前端 N+1。

    返回：status_distribution / stage_funnel / blocked_summary /
          risk_exposure(top5) / today_digest / ready_to_unblock

    必须注册在 /api/tasks/{task_id} 之前，否则 "dashboard" 会被当作 task_id 匹配。
    """
    orch = _get_task_orchestrator()
    tasks = await orch.store.list_tasks(project_id=project_id)
    relations = await orch.store.list_relations()

    # 状态分布
    status_distribution: dict[str, int] = {}
    for t in tasks:
        status_distribution[t["status"]] = status_distribution.get(t["status"], 0) + 1

    # 阶段漏斗（v1.2 主链路顺序：立项→讨论→拆解→评审→待办→执行→验收→关闭）
    funnel_order = ["idea", "discussing", "decomposing", "reviewing", "backlog",
                    "in_progress", "validating", "closing", "closed"]
    stage_funnel = [{"stage": s, "count": status_distribution.get(s, 0)} for s in funnel_order]
    other = {s: c for s, c in status_distribution.items() if s not in funnel_order}
    for s, c in sorted(other.items()):
        stage_funnel.append({"stage": s, "count": c})

    # blocked_by 映射（source → target：source blocked_by target）
    blocked_by_map: dict[str, list[dict]] = {}
    task_map = {t["task_id"]: t for t in tasks}
    for r in relations:
        if r.get("relation_type") == "blocks":
            # source blocks target → target 的上游是 source
            blocked_by_map.setdefault(r["target_task_id"], []).append(
                task_map.get(r["source_task_id"]) or {"task_id": r["source_task_id"],
                                                      "status": "unknown"})

    # 阻塞摘要：blocked 状态任务 + 其未完成上游
    blocked_tasks = []
    ready_to_unblock = 0
    for t in tasks:
        if t["status"] != "blocked":
            continue
        upstream = blocked_by_map.get(t["task_id"], [])
        pending = [u for u in upstream if u.get("status") not in ("closed", "done")]
        blocked_tasks.append({
            "task_id": t["task_id"], "identifier": t.get("identifier", ""),
            "title": t["title"],
            "pending_blockers": [{"task_id": u["task_id"],
                                  "identifier": u.get("identifier", ""),
                                  "status": u.get("status", "?")} for u in pending],
        })
        if upstream and not pending:
            ready_to_unblock += 1

    # 风险敞口 top5（高风险优先，其次 medium）
    risk_order = {"high": 0, "medium": 1, "low": 2}
    open_tasks = [t for t in tasks if t["status"] not in ("closed", "canceled", "abandoned")]
    risk_exposure = sorted(
        open_tasks, key=lambda t: (risk_order.get(t.get("risk_level", "low"), 3),
                                   t.get("updated_at", "")), reverse=True)[:5]
    risk_exposure = [
        {"task_id": t["task_id"], "identifier": t.get("identifier", ""),
         "title": t["title"], "risk_level": t.get("risk_level", ""),
         "status": t["status"]} for t in risk_exposure]

    # 今日摘要
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_digest = {
        "created": sum(1 for t in tasks if (t.get("created_at") or "").startswith(today)),
        "closed": sum(1 for t in tasks if (t.get("closed_at") or "").startswith(today)),
        "advanced": 0, "conductor_actions": 0,
    }
    try:
        for t in tasks:
            acts = await orch.store.list_activities(t["task_id"])
            today_digest["advanced"] += sum(
                1 for a in acts
                if (a.get("created_at") or "").startswith(today)
                and "status" in (a.get("changes") or {}))
            today_digest["conductor_actions"] += sum(
                1 for a in acts
                if (a.get("created_at") or "").startswith(today)
                and a.get("actor_name") == "task_conductor")
    except Exception as e:  # noqa: BLE001
        logger.debug("today_digest 活动统计失败（降级为 0）: %s", e)

    return {
        "total": len(tasks),
        "status_distribution": status_distribution,
        "stage_funnel": stage_funnel,
        "blocked_summary": {"count": len(blocked_tasks), "tasks": blocked_tasks},
        "ready_to_unblock": ready_to_unblock,
        "risk_exposure": risk_exposure,
        "today_digest": today_digest,
    }


@app.post("/api/tasks")
async def task_create_endpoint(payload: dict):
    """创建任务（默认 idea；可指定合法初始状态）。"""
    orch = _get_task_orchestrator()
    from datetime import datetime, timezone
    task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    result = await orch.submit_idea(
        task_id=task_id,
        project_id=payload.get("project_id", ""),
        title=payload.get("title", ""),
        description=payload.get("description", ""),
        thread_id=payload.get("thread_id", ""),
        creator_id=payload.get("creator_id", ""),
        creator_name=payload.get("creator_name", ""),
        risk_level=payload.get("risk_level", "medium"),
        status=payload.get("status", ""),
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "create_failed"))
    return result["task"]


@app.get("/api/tasks/{task_id}")
async def task_get(task_id: str):
    """任务详情 + stages。"""
    orch = _get_task_orchestrator()
    task = await orch.store.get_task(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    stages = await orch.store.list_stages(task_id)
    return {"task": task, "stages": stages}


@app.patch("/api/tasks/{task_id}")
async def task_update(task_id: str, payload: dict):
    """更新任务字段（乐观锁 if_version）。"""
    orch = _get_task_orchestrator()
    if_version = payload.pop("if_version", 0)
    try:
        if_version = int(if_version)
    except (ValueError, TypeError):
        raise HTTPException(400, "if_version must be integer")
    updated = None
    try:
        updated = await orch.store.update_task_fields(task_id, if_version, **payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if updated is None:
        latest = await orch.store.get_task(task_id)
        raise HTTPException(409, {"error": "version_conflict", "latest": latest})
    return updated


@app.post("/api/tasks/{task_id}/advance")
async def task_advance(task_id: str, payload: dict):
    """推进任务状态（乐观锁 + 冲突重试一次）。

    payload: { target_status, if_version, thread_id?, comment?, actor? }
    """
    orch = _get_task_orchestrator()
    target_status = payload.get("target_status", "")
    if_version_raw = payload.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "if_version must be integer")
    result = await orch.advance_stage(
        task_id=task_id,
        target_status=target_status,
        if_version=if_version,
        actor=payload.get("actor", "user"),
        thread_id=payload.get("thread_id", ""),
        comment=payload.get("comment", ""),
    )
    if not result.get("ok"):
        error = result.get("error", "advance_failed")
        # 非法转移返回 400，冲突返回 409
        code = 409 if "conflict" in error else 400
        raise HTTPException(code, {"error": error, "message": result.get("message", ""),
                                    "task": result.get("task")})
    return result["task"]


@app.get("/api/tasks/{task_id}/transitions")
async def task_get_transitions(task_id: str):
    """查合法转移目标（供前端禁用非法列）。"""
    orch = _get_task_orchestrator()
    task = await orch.store.get_task(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    transitions = await orch.get_transitions(task["status"])
    return {"task_id": task_id, "status": task["status"], "transitions": transitions}


# ====== 任务管理模块 API（V1） ======
# V1 新增端点：claim/rollback/close 协议 + reports/comments/relations/criteria/
# activities/artifacts/proposals + idea→task 转化 + execute_coding 派发


@app.post("/api/tasks/{task_id}/claim")
async def task_claim(task_id: str, payload: dict):
    """认领任务（claim 协议，乐观锁）。"""
    orch = _get_task_orchestrator()
    if_version_raw = payload.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "if_version must be integer")
    thread_id = payload.get("thread_id", "")
    task = await orch.store.get_task(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    # thread_id 绑定校验
    if task.get("thread_id") and task["thread_id"] != thread_id:
        raise HTTPException(409, {"error": "claimed_by_other",
                                   "message": "任务已被其他对话绑定，不抢占"})
    # 推进到 in_progress（从 backlog 或 decomposing）
    target = "in_progress"
    result = await orch.advance_stage(
        task_id=task_id, target_status=target,
        if_version=if_version, actor="agent", thread_id=thread_id,
        comment="agent 认领任务")
    if not result.get("ok"):
        code = 409 if "conflict" in result.get("error", "") else 400
        raise HTTPException(code, {"error": result.get("error"),
                                    "message": result.get("message", ""),
                                    "task": result.get("task")})
    return result["task"]


@app.post("/api/tasks/{task_id}/rollback")
async def task_rollback(task_id: str, payload: dict):
    """回退（三级 local/partial/global 别名 或 直接指定 target_status）。

    payload 二选一：
    - rollback_target: local|partial|global（旧别名）
    - target_status: 直接指定目标阶段（in_progress/decomposing/discussing/reviewing/backlog/idea 等）
      target_status 优先于 rollback_target；advance_stage 会校验状态机合法性
    """
    orch = _get_task_orchestrator()
    if_version_raw = payload.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "if_version must be integer")
    rollback_target = payload.get("rollback_target", "")
    target_status = payload.get("target_status", "")
    comment = payload.get("comment", "")
    if not rollback_target and not target_status:
        raise HTTPException(400, {"error": "missing_target",
                                   "message": "rollback_target 或 target_status 至少提供一个"})
    result = await orch.rollback_task(
        task_id=task_id, rollback_target=rollback_target,
        if_version=if_version, comment=comment,
        target_status=target_status)
    if not result.get("ok"):
        code = 409 if "conflict" in result.get("error", "") else 400
        raise HTTPException(code, {"error": result.get("error"),
                                    "message": result.get("message", ""),
                                    "task": result.get("task")})
    return result["task"]


@app.post("/api/tasks/{task_id}/close")
async def task_close(task_id: str, payload: dict):
    """关闭（硬约束检查：验收标准 + 文档提案）。"""
    orch = _get_task_orchestrator()
    if_version_raw = payload.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "if_version must be integer")
    result = await orch.close_task(task_id=task_id, if_version=if_version)
    if not result.get("ok"):
        code = 409 if "conflict" in result.get("error", "") else 400
        raise HTTPException(code, {"error": result.get("error"),
                                    "message": result.get("message", ""),
                                    "detail": result.get("detail")})
    return result["task"]


@app.get("/api/tasks/{task_id}/reports")
async def task_list_reports(task_id: str):
    """任务报告列表。"""
    orch = _get_task_orchestrator()
    reports = await orch.store.list_reports(task_id)
    return {"reports": reports}


@app.post("/api/tasks/{task_id}/reports")
async def task_submit_report(task_id: str, payload: dict):
    """发布报告。"""
    orch = _get_task_orchestrator()
    report = await orch.store.submit_report(
        task_id=task_id,
        agent_id=payload.get("agent_id", ""),
        content=payload.get("content", ""),
        session_id=payload.get("session_id", ""),
        terminal_session_id=payload.get("terminal_session_id", ""),
        artifact_ids=payload.get("artifact_ids"),
        self_check=payload.get("self_check"),
    )
    return report


# ============ 报告导出（ReportExporter）============

@app.post("/api/tasks/{task_id}/reports/{report_id}/export")
async def task_export_report(task_id: str, report_id: str, payload: dict = Body(default={})):
    """触发报告导出（同步生成文件 + 落历史表）。

    payload 字段：format（md|html|json，默认 md）、verify_only（bool，默认 False）
    返回：{export_id, path, sha256, size_bytes, format, exported_at, content_type}
    """
    fmt = (payload.get("format") or "md").lower()
    verify_only = bool(payload.get("verify_only"))
    exporter = getattr(app.state, "report_exporter", None)
    if exporter is None:
        raise HTTPException(503, "report_exporter not initialized")
    orch = _get_task_orchestrator()
    report = await orch.store.get_report(report_id)
    if not report:
        raise HTTPException(404, "report not found")
    if report["task_id"] != task_id:
        raise HTTPException(400, "report_id does not belong to task_id")
    if verify_only:
        result = await asyncio.to_thread(exporter.verify_export, task_id=task_id, report_id=report_id, fmt=fmt)
        return result
    try:
        result = await asyncio.to_thread(exporter.export, report, fmt=fmt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from task.exporter import get_content_type
    result["content_type"] = get_content_type(fmt)
    return result


@app.get("/api/tasks/{task_id}/reports/{report_id}/export")
async def task_download_export(task_id: str, report_id: str, format: str = "md"):
    """下载已导出的文件（流式返回二进制）。

    若文件不存在则触发一次即时导出（保证下载总能成功）。
    """
    fmt = (format or "md").lower()
    exporter = getattr(app.state, "report_exporter", None)
    if exporter is None:
        raise HTTPException(503, "report_exporter not initialized")
    try:
        target = await asyncio.to_thread(exporter._resolve_path, task_id=task_id, report_id=report_id, fmt=fmt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not target.exists():
        # 兜底：即时导出
        orch = _get_task_orchestrator()
        report = await orch.store.get_report(report_id)
        if not report or report["task_id"] != task_id:
            raise HTTPException(404, "report not found or not yet exported")
        try:
            await asyncio.to_thread(exporter.export, report, fmt=fmt)
        except ValueError as e:
            raise HTTPException(400, str(e))
    from task.exporter import get_content_type
    from fastapi.responses import FileResponse
    return FileResponse(path=target, media_type=get_content_type(fmt),
                        filename=target.name)


@app.get("/api/tasks/{task_id}/reports/{report_id}/exports")
async def task_list_exports(task_id: str, report_id: str):
    """导出历史列表（按时间倒序）。"""
    exporter = getattr(app.state, "report_exporter", None)
    if exporter is None:
        raise HTTPException(503, "report_exporter not initialized")
    return await asyncio.to_thread(exporter.list_exports, task_id=task_id, report_id=report_id)



async def task_list_design_notes(task_id: str, status: str = ""):
    """设计笔记列表（v1.1 四态：proposed/implemented/rejected/archived）。"""
    orch = _get_task_orchestrator()
    notes = await orch.store.list_design_notes(task_id=task_id, status=status)
    return {"notes": notes}


@app.post("/api/tasks/{task_id}/design-notes/{note_id}/status")
async def task_update_design_note(task_id: str, note_id: str, payload: dict):
    """笔记状态流转（验收晋升 implemented / 否决 rejected / 归档 archived）。

    rejected 必须带 reject_reason；implemented 可带 supersedes（旧笔记自动归档）。
    """
    orch = _get_task_orchestrator()
    try:
        note = await orch.store.update_design_note_status(
            note_id, payload.get("status", ""),
            reject_reason=payload.get("reject_reason", ""),
            supersedes=payload.get("supersedes", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not note:
        raise HTTPException(404, "design note not found")
    return note


@app.get("/api/tasks/{task_id}/comments")
async def task_list_comments(task_id: str, comment_type: str = ""):
    """评论列表。"""
    orch = _get_task_orchestrator()
    comments = await orch.store.list_comments(task_id, comment_type=comment_type)
    return {"comments": comments}


@app.post("/api/tasks/{task_id}/comments")
async def task_add_comment(task_id: str, payload: dict):
    """发表评论（审批/讨论）。

    V3 @agent 评论交互（设计文档 §4.12）：body 中 `@coding_agent` 等白名单 agent
    即触发后台 agent 回复（不阻塞响应，回复落 task_comments 后前端轮询/SSE 到达）。
    """
    orch = _get_task_orchestrator()
    body = payload.get("body", "")

    # V3：解析 @mentions（白名单 = task 域 agent）
    _MENTION_RE = re.compile(r"@([a-z_][a-z0-9_]*)", re.IGNORECASE)
    whitelist = {"coding_agent", "task_planner", "quality_inspector", "task_conductor"}
    mentions = [m for m in dict.fromkeys(_MENTION_RE.findall(body)) if m in whitelist]

    comment = await orch.store.add_comment(
        task_id=task_id,
        body=body,
        author_type=payload.get("author_type", "user"),
        author_id=payload.get("author_id", ""),
        author_name=payload.get("author_name", ""),
        comment_type=payload.get("comment_type", "discussion"),
        report_id=payload.get("report_id", ""),
        decision=payload.get("decision"),
        rollback_target=payload.get("rollback_target"),
        thread_id=payload.get("thread_id", ""),
        mentions=mentions,
    )

    # V3：后台派发 agent 回复（不阻塞响应；回复写 task_comments actor=agent）
    if mentions and payload.get("author_type", "user") == "user":
        async def _dispatch_mention_replies(question: str, agent_ids: list[str]) -> None:
            for aid in agent_ids:
                try:
                    result = await orch.respond_to_mention(task_id, aid, question)
                    if not result.get("ok"):
                        logger.warning("mention reply 失败 (%s): %s", aid, result.get("error"))
                except Exception as e:  # noqa: BLE001
                    logger.warning("mention reply 异常 (%s): %s", aid, e)

        asyncio.create_task(_dispatch_mention_replies(body, mentions))

    return comment


@app.get("/api/tasks/{task_id}/relations")
async def task_get_relations(task_id: str):
    """任务的依赖关系。"""
    orch = _get_task_orchestrator()
    rels = await orch.store.list_relations(task_id)
    blockers = await orch.store.list_blocked_by(task_id)
    return {"relations": rels, "blockers": blockers}


@app.post("/api/tasks/{task_id}/execute")
async def task_execute_coding(task_id: str, payload: dict):
    """派发 coding_agent 执行（绑 terminal + workspace；harness=claude_code/codex）。

    exec_mode（可选，默认 terminal）：
    - terminal：ConPTY/psmux/tmux 直跑原生 claude/codex TUI（完整终端窗口 +
      人工介入；后端不支持时自动降级 tee）
    - tee：后台子进程 stream-json + 事件流 tee（旧行为）
    """
    orch = _get_task_orchestrator()
    style_id = payload.get("style_id", "default")
    harness = payload.get("harness", "claude_code")
    if harness not in ("claude_code", "codex"):
        raise HTTPException(400, f"harness must be claude_code/codex, got {harness}")
    exec_mode = payload.get("exec_mode", "terminal")
    if exec_mode not in ("terminal", "tee"):
        raise HTTPException(400, f"exec_mode must be terminal/tee, got {exec_mode}")
    if_version_raw = payload.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        if_version = 0
    result = await orch.execute_coding(
        task_id, style_id=style_id, if_version=if_version, harness=harness,
        exec_mode=exec_mode)
    if not result.get("ok"):
        raise HTTPException(400, {"error": result.get("error"),
                                   "message": result.get("hint", "")})
    return result


@app.get("/api/tasks/{task_id}/criteria")
async def task_list_criteria(task_id: str):
    """验收标准列表。"""
    orch = _get_task_orchestrator()
    criteria = await orch.store.list_criteria(task_id)
    return {"criteria": criteria}


@app.post("/api/tasks/{task_id}/criteria")
async def task_add_criteria(task_id: str, payload: dict):
    """添加验收标准。"""
    orch = _get_task_orchestrator()
    criteria = await orch.store.add_criteria(
        task_id=task_id,
        description=payload.get("description", ""),
        check_type=payload.get("check_type", "manual"),
    )
    return criteria


@app.post("/api/tasks/{task_id}/ideas/{idea_id}/confirm")
async def task_confirm_idea(idea_id: str, payload: dict):
    """draft→open（用户确认）。"""
    orch = _get_task_orchestrator()
    if_version_raw = payload.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "if_version must be integer")
    r = await orch.store.confirm_idea(idea_id, if_version)
    if not r.get("ok"):
        raise HTTPException(409, {"error": "version_conflict", "idea": r.get("idea")})
    return r["idea"]


@app.post("/api/tasks/{task_id}/ideas/{idea_id}/convert")
async def task_convert_idea(idea_id: str, payload: dict):
    """idea→task（进入 backlog）。"""
    orch = _get_task_orchestrator()
    task_id_new = payload.get("task_id", "")
    if not task_id_new:
        from datetime import datetime, timezone
        task_id_new = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    title = payload.get("title", "")
    task = await orch.store.convert_idea_to_task(idea_id, task_id_new, title=title or None)
    return task


@app.post("/api/tasks/proposals/{proposal_id}/apply")
async def task_apply_proposal(proposal_id: str, payload: dict):
    """应用文档变更提案。"""
    orch = _get_task_orchestrator()
    if_version_raw = payload.get("if_version", 0)
    try:
        if_version = int(if_version_raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "if_version must be integer")
    new_hash = payload.get("new_hash", "")
    r = await orch.store.apply_doc_proposal(proposal_id, if_version, new_hash)
    if not r.get("ok"):
        raise HTTPException(409, {"error": "version_conflict"})
    return r


@app.get("/api/tasks/{task_id}/activities")
async def task_list_activities(task_id: str):
    """任务活动记录（字段级变更）。"""
    orch = _get_task_orchestrator()
    acts = await orch.store.list_activities(task_id)
    return {"activities": acts}


@app.get("/api/tasks/{task_id}/artifacts")
async def task_list_artifacts(task_id: str):
    """任务交付物列表。"""
    orch = _get_task_orchestrator()
    arts = await orch.store.list_artifacts(task_id)
    return {"artifacts": arts}


@app.get("/api/tasks/{task_id}/terminal/stream")
async def task_terminal_stream(task_id: str, request: Request):
    """终端会话 SSE 流（V1 验收第⑧条：xterm 实时显示 coding 过程）。

    策略（双路径）：
    1. task 绑定了 terminal_session_id → stream_pane() 实时推送 capture-pane 内容（500ms）
    2. 未绑定 terminal_session_id → 回退推送 activities（2s 间隔），前端兼容显示

    SSE 事件格式：
        data: {"type":"pane","content":"...","terminal_session_id":"..."}\n\n
        data: {"type":"activities","activities":[...]}\\n\\n
        : heartbeat\\n\\n   (30s 无数据时保活)
    """
    import asyncio as _asyncio

    orch = _get_task_orchestrator()
    task = await orch.store.get_task(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} not found")
    terminal_session_id = task.get("terminal_session_id") or ""
    terminal_mgr = getattr(request.app.state, "task_terminal", None)

    async def event_generator():
        try:
            if terminal_session_id and terminal_mgr:
                # 路径 1：真实 terminal 流（stream_pane 内部 0.5s 间隔）
                async for pane_text in terminal_mgr.stream_pane(terminal_session_id, interval=0.5):
                    if await request.is_disconnected():
                        break
                    payload = {
                        "type": "pane",
                        "content": pane_text,
                        "terminal_session_id": terminal_session_id,
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            else:
                # 路径 2：activities 回退（2s 间隔）
                last_count = -1
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        acts = await orch.store.list_activities(task_id)
                        cur_count = len(acts)
                        if cur_count != last_count:
                            payload = {
                                "type": "activities",
                                "activities": acts,
                                "terminal_session_id": terminal_session_id,
                            }
                            yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                            last_count = cur_count
                    except Exception as e:
                        logger.warning("terminal_stream activities fetch failed: %s", e)
                    await _asyncio.sleep(2.0)
        except _asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("terminal_stream ended: %s", e)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
