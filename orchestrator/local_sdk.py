"""
LocalSdkOrchestrator — runs DAG locally without external Orchestrator service.

This is the M0 candidate C / always-on fallback. Wraps DagEngine directly.
It implements the Orchestrator protocol by in-process delegation.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, TYPE_CHECKING

if TYPE_CHECKING:
    from audit.store import EventStore

from harness import (
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    HarnessRegistry,
    HarnessType,
    ToolDefinition,
)
from orchestrator import (
    DagEvent,
    DagEventType,
    Orchestrator,
    RawHarnessEvent,
    RunHandle,
    RunMode,
    RunRequest,
    RunState,
    RunStatus,
    SessionStatus,    # v3: Session 对话层状态机
)
from workflow import (
    NodeType,
    WorkflowDefinition,
    load_workflow_yaml,
    validate_workflow,
)
from workflow.engine import DagEngine
from orchestrator.session_engine import SessionEngine
from orchestrator.config_loader import get_system_config
from orchestrator.cross_domain import CrossDomainCoordinator
from orchestrator.permission_engine import PermissionEngine

logger = logging.getLogger(__name__)


# 字符串 → HarnessType 映射（agent config 里 harness 字段是字符串）
_HARNESS_NAME_MAP: dict[str, HarnessType] = {
    "opencode": HarnessType.OPENCODE,
    "claude_code": HarnessType.CLAUDE_CODE,
    "claude_sdk": HarnessType.CLAUDE_SDK,
    "codex": HarnessType.CODEX,
    "kimi": HarnessType.KIMI,
    "http": HarnessType.HTTP,
    "deterministic": HarnessType.DETERMINISTIC,
    "local_llm": HarnessType.LOCAL_LLM,
}


def _resolve_harness_type(agent_id: str) -> HarnessType:
    """从 config/agents/*.yaml 读 agent 的 harness 配置，找不到回退 DETERMINISTIC。"""
    try:
        cfg = get_system_config()
        agent = cfg.agents.get(agent_id)
        if agent and agent.harness in _HARNESS_NAME_MAP:
            return _HARNESS_NAME_MAP[agent.harness]
    except Exception:
        pass
    return HarnessType.DETERMINISTIC


class LocalSdkOrchestrator(Orchestrator):
    """In-process Orchestrator. No remote SDK, no MCP.

    Use cases:
      - M0 benchmark baseline
      - Local dev / demo
      - Fallback when external Orchestrator unavailable
    """

    def __init__(
        self,
        workflows: dict[str, WorkflowDefinition] | None = None,
        llm_config: dict[str, Any] | None = None,
        event_store: "EventStore | None" = None,
        container_provisioner: Any = None,  # P0.18.7f: 注入 ContainerProvisioner（None 时走旧路径）
    ):
        self.workflows = workflows or {}          # workflow_id -> definition
        self.llm_config = llm_config or {}
        self._event_store = event_store
        self._container_provisioner = container_provisioner  # P0.18.7f
        self._runs: dict[str, RunState] = {}
        self._event_history: dict[str, list[DagEvent | RawHarnessEvent]] = {}
        self._engines: dict[str, DagEngine] = {}
        self._conv_engines: dict[str, SessionEngine] = {}   # P1: 对话引擎（session_id → SessionEngine）
        # P0-2: 共享 PermissionEngine 实例（避免每次 _run_conversational 多触发一次 get_system_config）
        # 跨域 Coordinator 不在这里持有全局实例（每个 run 需要绑定自己的 event_sink），
        # 改在 _run_conversational / continue_conversation 里临时构造 run-local Coordinator
        self._permission_engine = PermissionEngine(get_system_config())

    def register_workflow(self, workflow: WorkflowDefinition) -> None:
        validate_workflow(workflow)
        self.workflows[workflow.workflow_id] = workflow

    def load_workflow_file(self, path: str) -> WorkflowDefinition:
        wf = load_workflow_yaml(path)
        self.register_workflow(wf)
        return wf

    async def run(self, req: RunRequest) -> RunHandle:
        # v3: run_ 前缀（v2 统一 session_ 前缀已废弃）
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

        # P1: 根据 run_mode 分流（task 模式已废弃，不再支持）
        if req.run_mode == RunMode.CONVERSATIONAL:
            return await self._run_conversational(req, run_id)
        if req.run_mode == RunMode.TASK:
            raise ValueError("task 模式已废弃，请使用 conversational 模式（v2 Thread）")
        return await self._run_templated(req, run_id)

    async def resume(
        self, run_id: str, workflow_id: str, inputs: dict[str, Any]
    ) -> RunHandle:
        """从已有 run_id 断点恢复执行（复用 run_id，跳过已完成节点）。

        利用 DagEngine.resume() 复用 run_id，workspace 下已有文件会被
        _try_restore_node 检测到自动跳过。
        """
        wf = self.workflows.get(workflow_id)
        if wf is None:
            raise ValueError(f"Workflow not found: {workflow_id}")

        run_state = RunState(
            run_id=run_id,
            workflow_id=workflow_id,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._runs[run_id] = run_state
        # 保留已有事件历史，新事件追加在后面
        if run_id not in self._event_history:
            self._event_history[run_id] = []

        engine = DagEngine(
            wf,
            event_sink=lambda ev: self._sink(run_id, ev),
            llm_config=self.llm_config,
            event_store=self._event_store,
            container_provisioner=self._container_provisioner,  # P0.18.7f
        )
        engine.run_state = run_state
        self._engines[run_id] = engine

        # 后台执行 resume（复用 run_id）
        asyncio.create_task(self._run_engine_resume(engine, inputs))

        return RunHandle(
            run_id=run_id,
            workflow_id=workflow_id,
            started_at=run_state.started_at,
            cancel_token=run_id,
        )

    async def _run_engine_resume(self, engine: DagEngine, inputs: dict[str, Any]) -> None:
        """后台执行 engine.resume()。"""
        try:
            await engine.resume(engine.run_state.run_id, inputs)
        except Exception as e:
            run_state = self._runs.get(engine.run_state.run_id)
            if run_state:
                run_state.status = RunStatus.FAILED
                run_state.error = str(e)
                run_state.finished_at = datetime.now(timezone.utc)
            await self._sink(engine.run_state.run_id, DagEvent(
                type=DagEventType.RUN_FAILED,
                run_id=engine.run_state.run_id,
                node_id=None,
                payload={"error": str(e)},
                sequence=0,
            ))

    async def _run_conversational(self, req: RunRequest, run_id: str) -> RunHandle:
        """conversational 模式：启动 SessionEngine（v2 Thread 模式）。"""
        if not req.agent_id:
            raise ValueError("conversational 模式需要 agent_id")

        run_state = RunState(
            run_id=run_id,
            workflow_id=f"conv:{req.agent_id}",
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._runs[run_id] = run_state
        self._event_history[run_id] = []

        # 从 config/agents/{agent_id}.yaml 读 harness 配置（v2: 不再硬编码）
        harness_type = _resolve_harness_type(req.agent_id)

        # 从 ConfigLoader 读 agent 的 system_prompt（req.system_prompt_override 优先）
        agent_system_prompt = req.system_prompt_override or ""
        if not agent_system_prompt:
            try:
                cfg = get_system_config()
                agent_def = cfg.agents.get(req.agent_id)
                if agent_def and agent_def.system_prompt:
                    agent_system_prompt = agent_def.system_prompt
            except Exception:
                pass

        engine = SessionEngine(
            session_id=run_id,
            agent_id=req.agent_id,
            llm_config=self.llm_config,
            event_sink=lambda ev: self._sink(run_id, ev),
            harness_type=harness_type,
            system_prompt=agent_system_prompt,
            event_store=self._event_store,
            # P0-2: 构造 run-local Coordinator，event_sink 绑定到本 run 的 _sink
            # permission_engine 共享 _permission_engine 实例（不重复构造）
            cross_domain_coordinator=CrossDomainCoordinator(
                llm_config=self.llm_config,
                event_sink=lambda ev: self._sink(run_id, ev),
                permission_engine=self._permission_engine,
            ),
        )
        self._conv_engines[run_id] = engine

        # v3 修复：engine 启动前先写 runs 表（避免 provision_subagent 的 FK 竞态）
        await self._pre_init_run(req, run_id)

        asyncio.create_task(self._run_conv_engine(engine, req.initial_message))

        return RunHandle(
            run_id=run_id,
            workflow_id=f"conv:{req.agent_id}",
            started_at=run_state.started_at,
            cancel_token=run_id,
        )

    async def _run_conv_engine(self, engine: SessionEngine, initial_message: str = "") -> None:
        """后台执行 SessionEngine 一轮对话（G5 崩溃兜底含 RUN_FAILED emit）。"""
        try:
            result = await engine.start_turn(initial_message)
            run_state = self._runs.get(engine.session_id)
            if run_state:
                if result and result.status == "completed":
                    run_state.status = RunStatus.COMPLETED
                else:
                    run_state.status = RunStatus.FAILED
                    run_state.error = result.summary if result else "unknown"
                run_state.finished_at = datetime.now(timezone.utc)
                if result:
                    run_state.total_tokens_input = result.total_tokens_input
                    run_state.total_tokens_output = result.total_tokens_output
        except Exception as e:
            logger.exception("SessionEngine %s 后台执行异常", engine.session_id)
            run_state = self._runs.get(engine.session_id)
            if run_state:
                run_state.status = RunStatus.FAILED
                run_state.error = str(e)
                run_state.finished_at = datetime.now(timezone.utc)
            # 引擎崩溃必须 emit RUN_FAILED，否则前端 SSE 静默断开无任何反馈
            try:
                await self._sink(engine.session_id, DagEvent(
                    type=DagEventType.RUN_FAILED,
                    run_id=engine.session_id,
                    node_id=f"conv:{engine.agent_id}",
                    payload={"error": str(e), "error_type": "engine_crash"},
                    sequence=0,
                ))
            except Exception:
                pass

    async def _run_templated(self, req: RunRequest, run_id: str) -> RunHandle:
        """templated / hybrid 模式：启动 DagEngine（现有逻辑）。"""
        wf = self.workflows.get(req.workflow_id)
        if wf is None:
            raise ValueError(f"Workflow not found: {req.workflow_id}")

        run_state = RunState(
            run_id=run_id,
            workflow_id=req.workflow_id,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._runs[run_id] = run_state
        self._event_history[run_id] = []

        # P0.18.7: 如果 req.workspace_id 指定，构造 workspace_context 注入 DagEngine
        # 让 DagEngine 的 _try_provision_via_provisioner 路径可被触发
        workspace_context = await self._resolve_workspace_context(req.workspace_id)

        # Build engine
        engine = DagEngine(
            wf,
            event_sink=lambda ev: self._sink(run_id, ev),
            llm_config=self.llm_config,
            event_store=self._event_store,
            container_provisioner=self._container_provisioner,  # P0.18.7f
            workspace_context=workspace_context,                # P0.18.7
        )
        engine.run_state = run_state
        self._engines[run_id] = engine

        # v3 修复：engine 启动前先写 runs 表，避免并行节点 provision_subagent 时
        # FK (subagents.run_id → runs.run_id) 因 runs 记录尚未写入而失败。
        await self._pre_init_run(req, run_id)

        # Kick off in background; caller can stream events via stream_events()
        asyncio.create_task(self._run_engine(engine, wf, req))

        return RunHandle(
            run_id=run_id,
            workflow_id=req.workflow_id,
            started_at=run_state.started_at,
            cancel_token=run_id,
        )

    async def _pre_init_run(self, req: RunRequest, run_id: str) -> None:
        """在 engine 启动（create_task）之前写入 sessions + runs 记录。

        engine 启动后并行节点会立即 provision_subagent（INSERT subagents，FK run_id → runs）。
        若 runs 记录尚未写入，SQLite 会报 FOREIGN KEY constraint failed，导致节点直接失败。
        因此在后台启动 engine 之前，先同步完成 session/run 落库（create_session/init_run
        均为幂等，调用方后续可再次 init_run 覆盖补全字段）。

        D-055 修复：同时为 run_id 落一行 session。
        child SessionEngine.session_id == run_id（_run_conversational line 216），且
        SessionEngine.start_turn 第 264-268 行调 append_session_message(self.session_id, ...)，
        会撞 session_messages.session_id → sessions.session_id FK。
        runs 行已 INSERT（含 run_id），但 sessions 表里没有 run_id 这一行 → FK 失败
        → 子 conversational run 启动后 3-20ms 即报 FOREIGN KEY constraint failed。
        修复：落库 sessions 行 session_id=run_id（INSERT OR IGNORE 幂等）。
        """
        if self._event_store is None or not req.session_id:
            return
        try:
            await self._event_store.create_session(
                session_id=req.session_id,
                agent_id=req.agent_id or "",
            )
            # D-055：把 run_id 也注册成 session（child SessionEngine.session_id=run_id）
            await self._event_store.create_session(
                session_id=run_id,
                agent_id=req.agent_id or "",
            )
            await self._event_store.init_run(
                run_id=run_id,
                session_id=req.session_id,
                workflow_id=req.workflow_id,
                run_mode=req.run_mode.value if hasattr(req.run_mode, "value") else str(req.run_mode),
                agent_id=req.agent_id,
                initial_message=req.initial_message or None,
                inputs=req.inputs,
            )
        except Exception as e:
            # 预落库失败不阻塞 run（兜底走旧路径，只是可能仍触发 FK 竞态）
            logger.warning("pre_init_run 失败（不阻塞 run）: %s", e)

    async def _resolve_workspace_context(self, workspace_id: str | None) -> dict[str, Any] | None:
        """P0.18.7: 根据 workspace_id 查 authorized_workspaces 表构造 workspace_context。

        None = 通用对话（无绑定项目工作区），返回 None，DagEngine 走旧路径。
        找不到 / store 不可用 → 返回 None + warning 日志（不阻塞 run）。
        """
        if not workspace_id:
            return None
        if self._event_store is None:
            logger.warning(
                "workspace_id=%s 但 event_store 未注入，无法解析 workspace_context", workspace_id
            )
            return None
        try:
            ws = await self._event_store.get_authorized_workspace(workspace_id)
        except Exception as e:
            logger.warning("get_authorized_workspace(%s) failed: %s", workspace_id, e)
            return None
        if not ws:
            logger.warning("workspace_id=%s 不存在或已删除", workspace_id)
            return None
        if not ws.get("enabled", 1):
            logger.warning("workspace_id=%s 已停用（enabled=0）", workspace_id)
            return None
        # 字段映射：authorized_workspaces 表字段 → DagEngine._workspace_context 字段
        return {
            "workspace_id": ws["workspace_id"],
            "display_name": ws.get("display_name", ""),
            "workspace_mode": ws.get("mode", "isolated"),  # 表 mode → ctx workspace_mode
            "permissions": ws.get("permissions", "read_write"),
            "source_path": ws.get("source_path"),
            "git_url": ws.get("git_url"),
            "git_branch": ws.get("git_branch"),
            # workspace_root 留空，由 prepare_workspace 在 provisioner 阶段决定
        }

    async def _run_engine(self, engine: DagEngine, wf: WorkflowDefinition, req: RunRequest) -> None:
        try:
            await engine.run(req.inputs)
        except Exception as e:
            run_state = self._runs[engine.run_state.run_id]
            run_state.status = RunStatus.FAILED
            run_state.error = str(e)
            run_state.finished_at = datetime.now(timezone.utc)
            # 确保异常时也 emit RUN_FAILED，让前端和 stream_events 能收到结束信号
            await self._sink(engine.run_state.run_id, DagEvent(
                type=DagEventType.RUN_FAILED,
                run_id=engine.run_state.run_id,
                node_id=None,
                payload={"error": str(e)},
                sequence=0,  # _sink 会分配
            ))

    async def _sink(self, run_id: str, event: DagEvent) -> None:
        # 分配递增 sequence（SessionEngine/DagEngine emit 时 sequence=0）
        # P-fix：强制 history 内 sequence 严格单调递增。原实现 sequence=0 时用
        # len(events)+1 分配，与 DagEngine._sequence 自增计数器是两套体系 ——
        # surface 事件（sink 分配）与 engine 事件（自增）交错后序号非单调，
        # stream_events 的 since 单调游标会跳过低序号事件（run_20260818_
        # 083310_325184 join_surfaces handoff/completed 及 final_summary
        # 全部事件丢失根因）。
        events = self._event_history.setdefault(run_id, [])
        last_seq = max(
            (e.sequence for e in events if isinstance(e, DagEvent)), default=0
        )
        if event.sequence <= last_seq:
            event.sequence = last_seq + 1
        events.append(event)
        # Also keep RunState in sync
        run_state = self._runs.get(run_id)
        if run_state:
            if event.type == DagEventType.NODE_COMPLETED:
                pass  # run_state already updated in engine
            elif event.type == DagEventType.RUN_COMPLETED:
                run_state.status = RunStatus.COMPLETED
                run_state.finished_at = datetime.now(timezone.utc)
            elif event.type == DagEventType.RUN_FAILED:
                run_state.status = RunStatus.FAILED
                run_state.finished_at = datetime.now(timezone.utc)
                if event.payload and "error" in event.payload:
                    run_state.error = event.payload["error"]

    async def stream_events(
        self, run_id: str, since: int = 0
    ) -> AsyncIterator[DagEvent | RawHarnessEvent]:
        """Yield events from history. since = sequence number to start from.

        修复：原实现先 yield 全部历史，然后 while 循环又从头扫描，
        导致历史事件重复投递。改为统一用 since 游标增量投递。
        """
        # 统一增量投递：无论是否在运行，都从 since 开始
        while True:
            events = self._event_history.get(run_id, [])
            # 投递 since 之后的新事件
            i = 0
            while i < len(events):
                ev = events[i]
                i += 1
                if isinstance(ev, DagEvent) and ev.sequence > since:
                    yield ev
                    since = ev.sequence
                elif isinstance(ev, RawHarnessEvent):
                    yield ev

            # 检查是否结束
            run_state = self._runs.get(run_id)
            if not run_state or run_state.status != RunStatus.RUNNING:
                break

            # 等待新事件
            await asyncio.sleep(0.1)

    async def submit_widget_input(self, run_id: str, widget_id: str, payload: dict) -> None:
        """P1: 转发 widget-input 到对应 SessionEngine。"""
        engine = self._conv_engines.get(run_id)
        if engine is None:
            # 也可能是 dag engine，先忽略
            return
        engine.submit_widget_input(widget_id, payload)

    async def get_messages(self, run_id: str) -> list[dict]:
        """获取会话消息历史。v2: get_messages → get_session_messages。"""
        if not self._event_store:
            return []
        return await self._event_store.get_session_messages(run_id)

    async def inject(self, run_id: str, node_id: str, instruction: str) -> None:
        engine = self._engines.get(run_id)
        if not engine:
            return
        # For v0, append instruction to the node's next prompt
        # (Real impl: use widget.input or push into inbox queue)

    async def abort(self, run_id: str, reason: str = "") -> None:
        # DagEngine 路径
        engine = self._engines.get(run_id)
        if engine:
            await engine.cancel()
            return
        # SessionEngine 路径（对话模式无 DAG 拓扑）
        conv = self._conv_engines.get(run_id)
        if conv:
            await conv.cancel(reason or "user_cancelled")

    async def get_run(self, run_id: str) -> RunState:
        return self._runs.get(run_id)  # type: ignore

    async def resume_node(self, run_id: str, node_id: str, instruction: str) -> None:
        engine = self._engines.get(run_id)
        if engine:
            # v0: re-run node with new instruction appended
            nstate = engine.node_states.get(node_id)
            if nstate:
                nstate.status = __import__("orchestrator.protocol", fromlist=["NodeStatus"]).NodeStatus.PENDING
