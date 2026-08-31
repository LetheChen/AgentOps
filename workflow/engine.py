from __future__ import annotations

"""
DAG Engine — runs a workflow end-to-end.

State machine (v0):
  pending → ready → running → completed | failed
  failed nodes: downstream marked SKIPPED, run.status = FAILED

Concurrency:
  - All READY nodes run in parallel (asyncio.gather)
  - Handoff payload propagates: completed node → downstream node's "in:port_name" input
"""

# Python < 3.11 compat: asyncio.timeout() was added in 3.11
import sys as _sys
if _sys.version_info < (3, 11):
    import asyncio as _asyncio

    class _TimeoutCompat:
        """asyncio.timeout() backport for Python 3.10。

        行为对齐 CPython 3.11+：
        - __aexit__ 中检测 CancelledError 是否由超时引起 → 是则抛 TimeoutError
        - 非超时引起的 CancelledError 正常传播
        """

        def __init__(self, delay: float | None):
            self._delay = delay
            self._task: "_asyncio.Task | None" = None
            self._expired = False

        async def __aenter__(self):
            if self._delay is not None:
                self._task = _asyncio.current_task()
                loop = _asyncio.get_event_loop()
                self._handle = loop.call_later(self._delay, self._on_timeout)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if self._delay is not None:
                self._handle.cancel()
            if exc_type is _asyncio.CancelledError and self._expired:
                raise _asyncio.TimeoutError from None
            return False

        def _on_timeout(self):
            self._expired = True
            if self._task and not self._task.done():
                self._task.cancel()

    _asyncio.timeout = _TimeoutCompat  # type: ignore[attr-defined]
"""
Handoff convention (target format):
  - "next_node.in:port_name" — payload goes to specific input
  - "next_node" (no .in:) — payload goes to default port

The engine emits DagEvent at every state transition. RawHarnessEvent from
each harness is passed through to subscribers.
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING
import uuid

# 工程根目录（用于 command 节点默认 cwd）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if TYPE_CHECKING:
    from audit.store import EventStore

from harness import (
    AgentClient,
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
    NodeStatus,
    RunRequest,
    RunState,
    RunStatus,
)
from workflow import (
    HarnessTypeRef,
    NodeType,
    OutputRoute,
    WorkflowDefinition,
    WorkflowNode,
)
from workflow.collaboration import gen_handoff_summary, resolve_business_role
from orchestrator import docker_runtime

logger = logging.getLogger(__name__)


def anchor_cli_interpreter(rendered_cli: str) -> str:
    """解释器锚定（D-068）：模板开头的裸 `python` 替换为后端自身解释器（sys.executable）。

    背景：create_subprocess_shell 的子进程按 PATH 解析 `python`，可能漂移到
    缺依赖的解释器（如 WorkBuddy managed 3.13 缺 claude_agent_sdk/sqlglot，
    导致 smart-query 的 resolve_schema/validate_sql/execute_query 全链路失败）。
    锚定后 command 节点永远用与后端完全相同的解释器，依赖视图一致。

    安全边界：只替换模板开头的 `python` token（count=1），不影响
    `docker exec ... python ...` 等容器内执行模板（它们不以 python 开头）。
    """
    if re.match(r"^python(\s|$)", rendered_cli):
        return f'"{sys.executable}"' + rendered_cli[len("python"):]
    return rendered_cli


def _cli_refers_to_project_script(rendered_cli: str) -> bool:
    """判断 CLI 是否引用了需相对 PROJECT_ROOT 解析的相对路径脚本。

    command 节点的 cli_template 常写 `python tools/db_cli.py ...`、`python tools/x.py ...`，
    这类脚本随项目代码走版本，必须在 PROJECT_ROOT 下解析；而 workspace 沙箱是空目录。
    仅识别 `tools/<name>.py` 形态（相对路径、不含盘符/斜杠前缀），避免误判
    `docker exec ...`、绝对路径或内联 `python -c` 模板。

    安全边界：返回 True 仅表示「应优先用 PROJECT_ROOT 作 cwd」，不影响已显式配置
    cwd 的节点（调用处只在 cfg.cwd 为空时使用）。
    """
    # 排除容器内执行模板（docker exec ... python tools/x.py 的路径是容器内的，
    # 与宿主 cwd 无关，不该据此改宿主 cwd）。
    if re.match(r"^\s*docker\s+exec\b", rendered_cli):
        return False
    for tok in re.findall(r"(?:^|\s)(tools/[A-Za-z0-9_\-]+\.py)", rendered_cli):
        return True
    return False


# ====== Built-in DAG tools (handoff, graph context) ======

# 失败端口名集合：这些端口不要求 surface final（agent 可 fail-fast）
_FAILURE_PORTS: frozenset[str] = frozenset({"failed", "failure", "error", "blocked"})


def _is_failure_port(port: str) -> bool:
    """判断是否为失败端口（不要求 surface final）。"""
    return port in _FAILURE_PORTS or port.endswith("_failed") or port.endswith("_failure")


def _node_allowed_surface_tools(node: WorkflowNode) -> set[str]:
    """获取节点声明的 surface 相关工具集合。

    优先级：
      1. node.inline_agent.allowed_tools（P0.5 inline agent）
      2. 全局 agent allowed_tools（config/agents/*.yaml）
    """
    surface_tools: set[str] = set()
    if node.inline_agent:
        for t in (node.inline_agent.allowed_tools or []):
            if t in ("report_surface_state", "present_content_surface"):
                surface_tools.add(t)
    elif node.agent:
        # 全局 agent：从 config 加载 allowed_tools
        try:
            from orchestrator.config_loader import get_system_config
            cfg = get_system_config()
            agent = cfg.agents.get(node.agent)
            if agent:
                for t in (agent.allowed_tools or []):
                    if t in ("report_surface_state", "present_content_surface"):
                        surface_tools.add(t)
        except Exception as e:
            logger.debug(
                "加载全局 agent allowed_tools 失败 node=%s agent=%s: %s",
                node.id, node.agent, e,
            )
    return surface_tools


def _check_surface_final_violation(
    node: WorkflowNode, port: str
) -> dict[str, str] | None:
    """L0 铁律：校验节点 handoff 前 surface 是否完成 final 阶段。

    handoff.ts:92-110 + active-runs.ts:2286-2330 双重防护的 Worker 层。

    Returns:
        None 表示通过；dict 含 {expected_phase, message} 表示违反铁律。
        失败端口（failed/blocked/error）直接跳过校验。
        节点未声明 surface 工具则跳过。
        actor 无 visual_profile 或 view_id 多于 1 个则跳过。
    """
    # 失败端口不校验
    if _is_failure_port(port):
        return None

    # 节点未声明 surface 工具则跳过
    surface_tools = _node_allowed_surface_tools(node)
    if not surface_tools:
        return None

    # 解析 actor_id
    from orchestrator.actor_visual_profile import (
        resolve_actor_id_from_node, load_actor_visual_profile,
    )
    actor_id = resolve_actor_id_from_node(node)
    if not actor_id:
        return None

    # 加载 visual_profile
    try:
        profile = load_actor_visual_profile(actor_id)
    except Exception as e:
        logger.debug(
            "load_actor_visual_profile 失败 actor=%s: %s，跳过 surface 校验",
            actor_id, e,
        )
        return None

    # view_id 必须恰好 1 个：allowedViewIds.length !== 1 跳过）
    if len(profile.allowed_surface_views) != 1:
        return None

    view_id = next(iter(profile.allowed_surface_views.keys()))

    # 校验 _PHASE_TRACKER 中该 actor/view 的最后 phase 是否为 final
    from tools.report_surface_state import _PHASE_TRACKER
    tracker = _PHASE_TRACKER.get(actor_id, {})
    last_phase = tracker.get(view_id)
    if last_phase == "final":
        return None  # 通过

    return {
        "expected_phase": "final",
        "message": (
            f"required Actor Surface phases are incomplete: "
            f"actor='{actor_id}' view='{view_id}' last_phase='{last_phase}' "
            f"(expected 'final')"
        ),
    }

def make_dag_tools(
    workflow: WorkflowDefinition,
    node: WorkflowNode,
    state: "NodeExecutionState",
    run_id: str | None = None,
    event_sink: "Callable[[Any], Awaitable[None]] | None" = None,
) -> list[ToolDefinition]:
    """Create in-process tools available to every agent node."""

    async def handoff_tool(args: dict[str, Any]) -> dict[str, Any]:
        port = args.get("port") or "default"
        if not isinstance(port, str):
            port = str(port)
        content = args.get("content", "")
        summary = args.get("summary", "")

        # 空交付拒绝：content 为 null/空串/
        # 空容器时拒绝 handoff，强迫 agent 在同一 turn 内携带完整交付物
        # 重试 —— 否则下游 join 收到空壳（run_20260818_075133_665947
        # actor_synthesis 交付 analysis_surface=null 事故根因）。
        if (
            content is None
            or (isinstance(content, str) and not content.strip())
            or (isinstance(content, (dict, list)) and len(content) == 0)
        ):
            logger.warning(
                "handoff rejected (empty_content): node=%s port=%s content=%r",
                node.id, port, content,
            )
            return {
                "content": json.dumps({
                    "status": "rejected",
                    "code": "empty_handoff_content",
                    "message": (
                        "handoff content is empty. Provide the full deliverable "
                        "(structured result body, not a port name or process "
                        "narrative) in the 'content' field."
                    ),
                    "retryable": True,
                    "next_action": (
                        "Call handoff again with the complete deliverable in "
                        "'content' and a one-line 'summary'."
                    ),
                }, ensure_ascii=False),
                "is_error": True,
            }

        # L0 铁律：handoff 前 surface 强制校验（handoff.ts:92-110）。
        # 节点声明了 report_surface_state 工具时，必须先 emit phase=final 才能
        # handoff。未 emit 时直接拒绝（retryable）—— agent 在同一 turn 内补发
        # report_surface_state(final) 后重试 handoff。
        # 注意：不得用系统伪造 final 卡放行（原DAG_HANDOFF_SURFACE_INCOMPLETE 拒绝 + correction，
        # 伪造放行会让 L0 校验失效、下游拿到无最终卡片的假交付）。
        violation = _check_surface_final_violation(node, port)
        if violation:
            logger.warning(
                "handoff rejected (surface_sequence_incomplete): node=%s port=%s %s",
                node.id, port, violation["expected_phase"],
            )
            return {
                "content": json.dumps({
                    "status": "rejected",
                    "code": "surface_sequence_incomplete",
                    "message": violation["message"],
                    "retryable": True,
                    "expected_phase": violation["expected_phase"],
                    "next_action": (
                        f"Call report_surface_state with phase {violation['expected_phase']} "
                        f"before handoff."
                    ),
                }, ensure_ascii=False),
                "is_error": True,
            }

        state.pending_handoffs[port] = {
            "content": content,
            "summary": summary,
        }
        return {"content": f"handoff queued on port '{port}'"}

    async def graph_context_tool(args: dict[str, Any]) -> dict[str, Any]:
        upstream = []
        for dep_id in node.after:
            ups = state.upstream_outputs.get(dep_id, {})
            upstream.append({"node": dep_id, "outputs": ups})
        return {"content": {"upstream": upstream, "current_inputs": state.current_inputs}}

    return [
        ToolDefinition(
            name="handoff",
            description="Send result to downstream node. port=output port name, content=JSON-serializable payload",
            input_schema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "content": {},
                    "summary": {"type": "string"},
                },
                "required": ["port", "content"],
            },
            handler=handoff_tool,
        ),
        ToolDefinition(
            name="graph_context",
            description="Inspect upstream node outputs and current node inputs",
            input_schema={"type": "object", "properties": {}},
            handler=graph_context_tool,
        ),
    ]


# ====== Node execution state ======

@dataclass
class NodeExecutionState:
    """Per-node mutable state during a run."""
    node: WorkflowNode
    status: NodeStatus = NodeStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    upstream_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_inputs: dict[str, Any] = field(default_factory=dict)
    pending_handoffs: dict[str, Any] = field(default_factory=dict)
    text_outputs: list[str] = field(default_factory=list)
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    resolved_model: dict[str, Any] | None = None
    # Provisioning bookkeeping
    provisioned_subagent_id: str | None = None
    provisioned_worker_id: str | None = None
    provisioned_container_id: str | None = None


# ====== Event sink ======

EventSink = Callable[[DagEvent], Awaitable[None]]


async def _noop_sink(event: DagEvent) -> None:
    pass


# ====== Engine ======

class DagEngine:
    """Runs a WorkflowDefinition to completion.

    Usage:
        engine = DagEngine(workflow, event_sink=print_event)
        state = await engine.run(inputs={"topic": "..."})
    """

    def __init__(
        self,
        workflow: WorkflowDefinition,
        event_sink: EventSink | None = None,
        llm_config: dict[str, Any] | None = None,
        event_store: "EventStore | None" = None,
        *,
        workspace_context: dict[str, Any] | None = None,
        container_provisioner: Any = None,
    ):
        self.workflow = workflow
        self.event_sink = event_sink or _noop_sink
        self.llm_config = llm_config or {}
        self._event_store = event_store
        self.node_states: dict[str, NodeExecutionState] = {}
        self.run_state = RunState(
            run_id="",  # set at run()
            workflow_id=workflow.workflow_id,
            status=RunStatus.PENDING,
            started_at=datetime.now(timezone.utc),
        )
        self._sequence = 0
        self._cancel = asyncio.Event()
        # P0.18.5: workspace 授权上下文（由上层 api/server.py 传入）
        # 格式: {workspace_id, workspace_root, workspace_mode, workspace_tier, permissions}
        # None = 通用对话（无绑定项目工作区），回退到 workspace/{wf_id}/{run_id}/
        self._workspace_context = workspace_context or None
        # P0.18.5: ContainerProvisioner 注入（5 步启动 + 4 步销毁 + tier 资源限制 + sandbox 延迟清理）
        # None = 走旧路径（docker_runtime.create_and_start_container），保持向后兼容
        self._container_provisioner = container_provisioner
        # 标记本节点是否通过 provisioner 启动（cleanup 时决定走 deprovision 还是旧路径）
        self._provisioned_via_provisioner: dict[str, bool] = {}  # node_id -> True

    async def _emit(self, event_type: DagEventType, node_id: str | None, payload: dict):
        self._sequence += 1
        ev = DagEvent(
            type=event_type,
            run_id=self.run_state.run_id,
            node_id=node_id,
            payload=payload,
            sequence=self._sequence,
        )
        await self.event_sink(ev)

    async def cancel(self) -> None:
        self._cancel.set()
        self.run_state.status = RunStatus.CANCELLED
        await self._emit(DagEventType.RUN_CANCELLED, None, {"reason": "user_cancelled"})

    async def run(self, inputs: dict[str, Any]) -> RunState:
        """Execute the workflow end-to-end. Returns final RunState."""
        if not self.run_state.run_id:
            # v93: ID 统一为 session_ 前缀
            self.run_state.run_id = f"session_{int(time.time() * 1000)}"
        self.run_state.status = RunStatus.RUNNING
        self.run_state.started_at = datetime.now(timezone.utc)

        # Apply defaults
        resolved_inputs = self._resolve_inputs(inputs)

        # 产物目录锚定：若绑定了 workspace 但 workspace_root 未解析（local_sdk 构造
        # workspace_context 时留空），这里用纯路径解析回填绝对根路径，让
        # {{workspace.root}} 模板 + harness cwd 锚定到 mode 对应的真实目录，
        # 杜绝相对路径回退导致产物散落到进程 cwd / 项目代码目录。
        # 只算路径不落地（cp/git clone 仍由 provisioner 阶段 prepare_workspace 负责）。
        if self._workspace_context and not self._workspace_context.get("workspace_root"):
            try:
                from orchestrator.workspace_paths import WorkspaceInfo, resolve_workspace_root
                ws_ctx = self._workspace_context
                workspace_info = WorkspaceInfo(
                    workspace_id=ws_ctx.get("workspace_id", ""),
                    display_name=ws_ctx.get("display_name", ""),
                    mode=ws_ctx.get("workspace_mode", "isolated"),
                    permissions=ws_ctx.get("permissions", "read_write"),
                    source_path=ws_ctx.get("source_path"),
                    git_url=ws_ctx.get("git_url"),
                    git_branch=ws_ctx.get("git_branch"),
                    enabled=True,
                )
                self._workspace_context["workspace_root"] = resolve_workspace_root(
                    workspace_info, self.run_state.run_id,
                )
                # sandbox 类 mode（local_copy/git_clone/isolated）目录可能尚不存在，
                # 本地 in_process harness 的 bash 会用其作 cwd（WinError 267 若不存在）。
                # 轻量确保目录存在；bind_mount 是用户已有目录，mkdir exist_ok 无害。
                ws_root = self._workspace_context["workspace_root"]
                if ws_ctx.get("workspace_mode") != "bind_mount":
                    os.makedirs(ws_root, exist_ok=True)
            except Exception as e:
                logger.warning("workspace_root 解析失败（回退旧行为）: %s", e)

        # 兜底落地（D-074）：直接启动工作流（POST /api/agent/run，未绑定授权
        # workspace，_workspace_context=None）时，_workspace_root() 回退到
        # ${AGENTOPS_HOME}/workspaces/{wf_id}/{run_id} 绝对路径，但上面只对
        # 「绑定了 workspace_context」的分支做 os.makedirs，回退路径从未落地。
        # codex harness 用该路径做 cwd 时 create_subprocess_exec(cwd=不存在目录)
        # 抛 WinError 267（目录名称无效）→ 节点启动立刻失败。
        # 这里无条件确保 _workspace_root() 目录存在（bind_mount 用户已有目录
        # mkdir exist_ok 无害），覆盖回退路径 + sandbox 类 mode 两条路径。
        try:
            _ws_root_ensure = self._workspace_root()
            if _ws_root_ensure and not Path(_ws_root_ensure).is_dir():
                os.makedirs(_ws_root_ensure, exist_ok=True)
        except Exception as e:
            logger.warning("workspace_root 落地失败（不影响执行）: %s", e)

        # 拓扑分层（用于并行调度 + 前端 layout）
        try:
            levels = self._topological_levels()
        except Exception as e:
            self.run_state.status = RunStatus.FAILED
            self.run_state.error = f"topological sort failed: {e}"
            await self._emit(DagEventType.RUN_FAILED, None, {"error": self.run_state.error})
            return self.run_state

        layout_nodes = [
            {"id": nid, "level": level_idx, "index": index}
            for level_idx, level in enumerate(levels)
            for index, nid in enumerate(level)
        ]
        layout_edges = [
            {"source": dep, "target": nid}
            for nid, node in self.workflow.nodes.items()
            for dep in node.after
        ]

        await self._emit(DagEventType.RUN_CREATED, None, {
            "workflow_id": self.workflow.workflow_id,
            "inputs": resolved_inputs,
            "layout": {"nodes": layout_nodes},
            "edges": layout_edges,
        })

        # OPT-1: run 启动时重置相关 actor 的 surface 状态（phase tracker + dedup），
        # 修复跨 run 残留（第二次 run 的 started 被 phase_not_monotonic 拒绝 /
        # 相同 data_model 的 emit 被 dedup 吞掉导致前端无卡片）
        try:
            from orchestrator.surface_projector import collect_workflow_actor_ids
            from tools.report_surface_state import reset_run_surface_state
            reset_run_surface_state(collect_workflow_actor_ids(self.workflow))
        except Exception as e:
            logger.warning("重置 run surface 状态失败（不影响执行）: %s", e)

        # Initialize node states
        for nid, node in self.workflow.nodes.items():
            self.node_states[nid] = NodeExecutionState(node=node)

        # BFS by topological levels
        for level in levels:
            if self._cancel.is_set():
                break
            # Run all READY nodes in this level in parallel
            # 第二道防线：level deadline = max(节点 timeout) + 120s。
            # 节点级 asyncio.timeout 是第一道防线；若其取消被 harness 内部
            # 不可取消的 await 吞掉（历史 bug：节点协程永久挂死、run 卡 pending），
            # 这里放弃等待该任务，标记 FAILED，保证 run 必然收尾。
            task_map = {
                nid: asyncio.ensure_future(self._run_node(nid, resolved_inputs))
                for nid in level
            }
            level_deadline = max(
                self._get_node_timeout(self.workflow.nodes[nid]) for nid in level
            ) + 120.0
            done, pending = await asyncio.wait(
                task_map.values(), timeout=level_deadline,
            )
            results: list[BaseException | None] = []
            watchdog_killed: list[str] = []
            for nid in level:
                t = task_map[nid]
                if t in done:
                    if t.cancelled():
                        exc: BaseException | None = RuntimeError(f"Node '{nid}' 被取消")
                    else:
                        try:
                            exc = t.exception()
                        except BaseException as te:
                            # CancelledError 等 BaseException 统一转 RuntimeError，
                            # 让下方 isinstance(result, Exception) 失败分支可处理
                            exc = RuntimeError(f"Node '{nid}' 异常: {te}")
                    results.append(exc)
                else:
                    # 看门狗强杀：cancel 一次（尽力），不等待 —— 任务可能已无法终止，
                    # 泄漏为后台任务（诊断端点 /api/debug/asyncio-tasks 可见），
                    # 但 run 不再被它拖死。
                    t.cancel()
                    watchdog_killed.append(nid)
                    results.append(RuntimeError(
                        f"Node '{nid}' 超过 level 兜底 deadline {level_deadline:.0f}s "
                        f"未结束（节点级超时取消失效），看门狗强制标记失败"
                    ))
            if watchdog_killed:
                logger.error(
                    "level 看门狗强制失败节点: %s（deadline=%.0fs）",
                    watchdog_killed, level_deadline,
                )

            for nid, result in zip(level, results):
                if isinstance(result, Exception):
                    logger.error(f"Node {nid} failed with exception: {result}")
                    nstate_failed = self.node_states[nid]
                    # 仅当 _run_agent_node 未 emit NODE_FAILED 时补 emit
                    # （_run_agent_node 内部 except 块已 emit 并 re-raise，会重复）
                    already_emitted = (
                        nstate_failed.status == NodeStatus.FAILED
                        and nstate_failed.error is not None
                    )
                    nstate_failed.status = NodeStatus.FAILED
                    nstate_failed.error = str(result)
                    if not nstate_failed.finished_at:
                        nstate_failed.finished_at = datetime.now(timezone.utc)
                    if not already_emitted:
                        resolved = nstate_failed.resolved_model or self._resolve_model_for_node(
                            self.workflow.nodes[nid]
                        )
                        await self._emit(DagEventType.NODE_FAILED, nid, {
                            "error": str(result),
                            "agent": self.workflow.nodes[nid].agent,
                            "provider_id": (resolved or {}).get("provider", ""),
                            "model": (resolved or {}).get("model", ""),
                            "error_type": self._classify_error_type(str(result)),
                        })

            # Check if any node in this level failed
            any_failed = any(
                self.node_states[nid].status == NodeStatus.FAILED for nid in level
            )
            if any_failed:
                # Mark downstream as SKIPPED
                for nid in level:
                    if self.node_states[nid].status == NodeStatus.FAILED:
                        self._mark_downstream_skipped(nid)
                self.run_state.error = "one or more nodes failed"
                # 竞态修复（与成功路径对称）：先 emit RUN_FAILED 再翻转 status。
                # stream_events 以 status != RUNNING 为退出条件——原实现先翻
                # FAILED 再 emit，桥接可能在排空 node.failed / run.failed 前
                # 就 break（run_20260818_075133_665947 join_surfaces 的
                # node.failed/run.failed 事件丢失根因）。
                self.run_state.finished_at = datetime.now(timezone.utc)
                await self._emit(DagEventType.RUN_FAILED, None, {"error": self.run_state.error})
                self.run_state.status = RunStatus.FAILED
                return self.run_state

        # All levels completed
        if self._cancel.is_set():
            return self.run_state

        # 竞态修复：先 emit RUN_COMPLETED 再翻转 status。
        # stream_events 以 status != RUNNING 作为退出条件——若先翻状态，
        # 桥接可能在排空 RUN_COMPLETED（及其之前 append 的事件）前就 break，
        # 导致前端收不到 run.completed、大屏/聊天停在「执行中」。
        self.run_state.finished_at = datetime.now(timezone.utc)
        await self._emit(DagEventType.RUN_COMPLETED, None, {
            "duration_ms": int((self.run_state.finished_at - self.run_state.started_at).total_seconds() * 1000),
            "total_tokens_in": self.run_state.total_tokens_input,
            "total_tokens_out": self.run_state.total_tokens_output,
        })
        self.run_state.status = RunStatus.COMPLETED
        return self.run_state

    def _resolve_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Apply input defaults declared in workflow."""
        result = dict(inputs)
        for inp in self.workflow.inputs:
            name = inp.get("name")
            if name and name not in result and "default" in inp:
                result[name] = inp["default"]
        return result

    def _topological_levels(self) -> list[list[str]]:
        """Group nodes by topological level (parallel nodes in same level)."""
        in_degree: dict[str, int] = {nid: 0 for nid in self.workflow.nodes}
        adj: dict[str, list[str]] = defaultdict(list)
        for nid, node in self.workflow.nodes.items():
            for dep in node.after:
                adj[dep].append(nid)
                in_degree[nid] += 1
        levels: list[list[str]] = []
        current = [nid for nid, d in in_degree.items() if d == 0]
        while current:
            levels.append(current)
            nxt: list[str] = []
            for nid in current:
                for c in adj[nid]:
                    in_degree[c] -= 1
                    if in_degree[c] == 0:
                        nxt.append(c)
            current = nxt
        return levels

    def _mark_downstream_skipped(self, failed_nid: str) -> None:
        """Recursively mark downstream nodes as SKIPPED."""
        for nid, node in self.workflow.nodes.items():
            if failed_nid in node.after and self.node_states[nid].status == NodeStatus.PENDING:
                self.node_states[nid].status = NodeStatus.SKIPPED
                self._mark_downstream_skipped(nid)

    def _eval_skip_if(self, expr: str) -> bool:
        """评估 skip_if 表达式。

        支持格式：
          - "{{not validate.passed}}" → 取 validate 节点 passed port 的值并取反
          - "{{validate.passed}}"    → 直接取值
        值来源：node_states[validate].pending_handoffs["passed"]["content"]
        """
        import re
        m = re.match(r"^\{\{(.+)\}\}$", expr.strip())
        if not m:
            return False
        inner = m.group(1).strip()

        negate = False
        if inner.startswith("not "):
            negate = True
            inner = inner[4:].strip()

        # 解析 node.port
        parts = inner.split(".", 1)
        if len(parts) != 2:
            return False
        node_id, port_name = parts
        nstate = self.node_states.get(node_id)
        if not nstate:
            return negate  # 节点不存在，not → True
        payload = nstate.pending_handoffs.get(port_name)
        if payload is None:
            return negate  # port 不存在，not → True
        # 提取 content
        if isinstance(payload, dict):
            content = payload.get("content", payload.get("summary", ""))
        else:
            content = payload
        # 转 bool（语义按 content 类型分派）：
        # - bool: 直接返回
        # - 字符串：先匹配显式关键字（true/false/yes/no/pass 等），
        #   未命中关键字时按"非空即 True"判断（适用于 markdown 摘要 / 描述性文本，
        #   如 log-patrol 的 report.critical_summary —— 有内容=有重大问题=需要推送）
        if isinstance(content, bool):
            value = content
        elif isinstance(content, str):
            low = content.strip().lower()
            if low in ("true", "1", "yes", "pass", "passed"):
                value = True
            elif low in ("false", "0", "no", "skip", "none", "null", ""):
                value = False
            else:
                # 非空描述性文本 → 视为 True（有内容）
                value = True
        else:
            value = bool(content)
        return (not value) if negate else value

    async def _run_node(self, node_id: str, global_inputs: dict[str, Any]) -> None:
        """Execute a single node. Resolves inputs, instantiates harness, runs."""
        nstate = self.node_states[node_id]
        node = nstate.node

        # If already skipped or failed, skip
        if nstate.status in (NodeStatus.SKIPPED, NodeStatus.FAILED, NodeStatus.COMPLETED):
            return

        # skip_if 条件评估：表达式为 true 则跳过此节点及下游
        if node.skip_if and self._eval_skip_if(node.skip_if):
            nstate.status = NodeStatus.SKIPPED
            nstate.finished_at = datetime.now(timezone.utc)
            self.run_state.node_states[node_id] = nstate.status
            await self._emit(DagEventType.NODE_SKIPPED, node_id, {
                "skip_if": node.skip_if,
            })
            self._mark_downstream_skipped(node_id)
            return

        # 断点恢复：检查 output_files 是否已存在（文件驱动的天然检查点）
        if self._try_restore_node(node, nstate):
            await self._finalize_completed_node(node_id)
            return

        # parallel_branch and gateway are virtual nodes — no agent execution
        if node.type == NodeType.PARALLEL_BRANCH:
            await self._run_parallel_branch_node(node, nstate, global_inputs)
        elif node.type == NodeType.GATEWAY:
            await self._run_gateway_node(node, nstate, global_inputs)
        elif node.type == NodeType.COMMAND:
            # P0.1: CLI / 二进制确定性执行（不走 LLM）
            await self._run_command_node(node, nstate, global_inputs)
        elif node.type == NodeType.WHILE:
            # P0.1: 反馈循环执行（仅做条件判断 + 触发反馈边，下游由 DAG 调度）
            await self._run_while_node(node, nstate, global_inputs)
        elif node.type == NodeType.AWAIT_COMMAND:
            # P0.1: 多轮对话注入（占位：当前实现 emit AWAIT_COMMAND_TIMEOUT 后走 timeout port）
            # 完整的 SSE 监听 + 重新触发 actor 子 run 由后续 P1.4 supervision 模式补完
            await self._run_await_command_node(node, nstate, global_inputs)
        else:
            await self._run_agent_node(node, nstate, global_inputs)

        await self._finalize_completed_node(node_id)

    @staticmethod
    def _is_blocked_payload(payload: dict) -> bool:
        """检测单个 handoff payload 是否包含 BLOCKED/ERROR 信号。

        两种检测模式：
        1. JSON 结构化信号：content 为 {"status": "BLOCKED"/"ERROR"/"FAILED"} 字符串
        2. 文本强声明：含 "— BLOCKED" / "hard-BLOCKED" / "cannot proceed" 等关键词

        保守策略：仅强声明匹配，避免自然语言中偶然出现 blocked 被误判。
        """
        content = payload.get("content", "") if isinstance(payload, dict) else str(payload)
        if not content or not isinstance(content, str):
            return False
        stripped = content.strip()
        # JSON status 信号
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                import json
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and parsed.get("status") in ("BLOCKED", "ERROR", "FAILED"):
                    return True
            except (json.JSONDecodeError, TypeError):
                pass
        # 文本强声明（只匹配 agent 明确告知无法继续的语义）
        markers: list[str] = [
            "— BLOCKED",
            "— blocked",
            "hard-BLOCKED",
            "cannot proceed",
            "不能继续",
        ]
        for m in markers:
            if m in content:
                return True
        return False

    def _all_ports_blocked(self, nstate: "NodeExecutionState") -> bool:
        """检查一个节点的所有输出端口是否都以 BLOCKED 信号收尾。

        至少一个端口正常 → partial success → 仍算 COMPLETED。
        无输出端口（gateway 等虚拟节点）→ 不触发。
        """
        ports = list(nstate.node.outputs.keys())
        if not ports:
            return False
        for port in ports:
            payload = nstate.pending_handoffs.get(port, {})
            if not self._is_blocked_payload(payload):
                return False
        return True

    def _required_surface_final_violation(self, node: WorkflowNode) -> str | None:
        """L0 铁律 Manager 层兜底：节点 COMPLETED 前校验 surface final。

        active-runs.ts:2286-2330 的 _requiredSurfaceFinalViolation。

        与模块级 _check_surface_final_violation（Worker 层）的差异：
          - Worker 层在 handoff_tool 内逐 port 校验，可 retry
          - Manager 层在节点 COMPLETED 前对所有非失败 port 兜底校验，节点直接 FAILED

        Returns:
            None 通过；str 违反铁律的错误消息（含 DAG_HANDOFF_SURFACE_INCOMPLETE 前缀）。
        """
        # 节点未声明 surface 工具则跳过
        surface_tools = _node_allowed_surface_tools(node)
        if not surface_tools:
            return None

        # 没有输出端口则跳过（gateway 等虚拟节点）
        if not node.outputs:
            return None

        # 解析 actor_id
        from orchestrator.actor_visual_profile import (
            resolve_actor_id_from_node, load_actor_visual_profile,
        )
        actor_id = resolve_actor_id_from_node(node)
        if not actor_id:
            return None

        # 加载 visual_profile
        try:
            profile = load_actor_visual_profile(actor_id)
        except Exception:
            return None

        # view_id 必须恰好 1 个
        if len(profile.allowed_surface_views) != 1:
            return None
        view_id = next(iter(profile.allowed_surface_views.keys()))

        # 校验 _PHASE_TRACKER
        from tools.report_surface_state import _PHASE_TRACKER
        tracker = _PHASE_TRACKER.get(actor_id, {})
        last_phase = tracker.get(view_id)
        if last_phase == "final":
            return None  # 通过

        return (
            f"DAG_HANDOFF_SURFACE_INCOMPLETE {self.run_state.run_id}/{node.id}: "
            f"pinned view '{view_id}' requires an applied final Surface patch "
            f"(actor='{actor_id}', last_phase='{last_phase}')"
        )

    def _surface_final_repair_hint(self, node: WorkflowNode) -> str:
        """correction 用：生成缺失 final surface 的具体修复说明（含字段骨架）。

        MiniMax-M3 收到通用 correction 提示时倾向直接重试 handoff 而不补发
        surface（run_20260818_092121 actor_visual_story 事故：16 字段视图
        三轮均未补发）。把 view_id、期望 phase、必填字段清单与 data_model
        JSON 骨架直接写进 prompt，模型只需填值。
        """
        try:
            from orchestrator.actor_visual_profile import (
                resolve_actor_id_from_node, load_actor_visual_profile,
            )
            actor_id = resolve_actor_id_from_node(node)
            if not actor_id:
                return ""
            profile = load_actor_visual_profile(actor_id)
            if len(profile.allowed_surface_views) != 1:
                return ""
            view_id = next(iter(profile.allowed_surface_views.keys()))
            view = profile.get_view(view_id)
            if view is None:
                return ""
            skeleton = {
                name: (
                    f"<{c.type}{'≤' + str(c.max_length) if c.max_length else ''}>"
                )
                for name, c in view.fields.items()
            }
            fields_doc = "\n".join(
                f"  - {name} ({c.type}"
                + (f", ≤{c.max_length}" if c.max_length else "")
                + (f", enum{c.enum_values}" if c.enum_values else "")
                + ")"
                for name, c in view.fields.items()
            )
            return (
                "\n\n## 你还缺的交付（按此清单补齐）\n"
                f"1. 调用 report_surface_state(view_id='{view_id}', phase='final')，"
                f"data_model 必须一次性携带以下全部字段（完整快照，缺一会被拒绝）：\n"
                f"{fields_doc}\n"
                "   data_model JSON 骨架（把 <> 占位替换为真实值，progress 填 100）：\n"
                f"   {json.dumps(skeleton, ensure_ascii=False)}\n"
                "2. 然后调用 handoff 工具恰好一次（port/content/summary）。"
            )
        except Exception as e:
            logger.debug("_surface_final_repair_hint 生成失败: %s", e)
            return ""

    async def _finalize_completed_node(self, node_id: str) -> None:
        """节点完成后统一处理：状态更新、widget 事件、handoff 路由、完成事件。"""
        nstate = self.node_states[node_id]

        # L0 铁律：Manager 层兜底校验（active-runs.ts:2286-2330）。
        # 先校验后投影：agent 未 emit final 且 correction 未补救时节点真实
        # FAILED。原实现先系统伪造 final 卡再校验（tracker 被污染、校验恒
        # 通过），节点假完成 —— run_20260818_062738_750165 actor_research
        # 只有 started 卡却显示 final(progress=100) 的事故根因之一。
        violation = self._required_surface_final_violation(nstate.node)
        if violation:
            logger.error(violation)
            nstate.status = NodeStatus.FAILED
            nstate.error = violation
            if not nstate.finished_at:
                nstate.finished_at = datetime.now(timezone.utc)
            if not nstate.started_at:
                nstate.started_at = nstate.finished_at
            nstate.duration_ms = int(
                (nstate.finished_at - nstate.started_at).total_seconds() * 1000
            )
            self.run_state.node_states[node_id] = NodeStatus.FAILED
            resolved = nstate.resolved_model or self._resolve_model_for_node(nstate.node)
            await self._emit(DagEventType.NODE_FAILED, node_id, {
                "error": violation,
                "agent": nstate.node.agent,
                "model": resolved,
                "phase": "surface_final_violation",
            })
            # violation 提前 return 前必须清理 provision 的 subagent/container，
            # 否则 docker 容器泄漏（原实现直接 return 跳过尾部 cleanup）
            await self._cleanup_provisioned_subagent(nstate, cleanup_status="failed")
            return

        # OPT-1：校验通过后的 UI 兜底投影（source=system）。仅当 agent 已
        # handoff 但忘 emit final 卡时补一张，保证 A2UI 大屏完整性 ——
        # 纯 UI 层增强，不再参与完成判定（判定已由上方真实校验完成）。
        try:
            from orchestrator.surface_projector import project_node_final_fallback
            await project_node_final_fallback(
                nstate.node, self.run_state.run_id, self.event_sink
            )
        except Exception as e:
            logger.debug(
                "节点 '%s' final 兜底投影跳过（不影响执行）: %s", node_id, e,
            )

        nstate.status = NodeStatus.COMPLETED
        if not nstate.finished_at:
            nstate.finished_at = datetime.now(timezone.utc)
        if not nstate.started_at:
            nstate.started_at = nstate.finished_at
        nstate.duration_ms = int((nstate.finished_at - nstate.started_at).total_seconds() * 1000)
        self.run_state.total_tokens_input += nstate.tokens_in
        self.run_state.total_tokens_output += nstate.tokens_out
        self.run_state.node_states[node_id] = nstate.status
        self.run_state.node_outputs[node_id] = {
            p: nstate.pending_handoffs.get(p) for p in nstate.node.outputs
        }

        # Emit widget.update events declared in workflow.widgets for this node
        await self._emit_widgets_for_node(node_id, event="node.completed")

        # Handoff routing: for each declared output port, send payload to downstream
        # 支持多消费者：一个端口可广播到多个下游节点
        for port, route in nstate.node.outputs.items():
            payload = nstate.pending_handoffs.get(port, {"content": "", "summary": ""})
            for target, target_port in route.parse_all():
                if not target:
                    continue
                target_state = self.node_states.get(target)
                if target_state is None:
                    continue
                # Choose the port name (target_port if specified, else port)
                incoming_port = target_port or port
                target_state.upstream_outputs.setdefault(node_id, {})[incoming_port] = payload

                # D-003 修复：emit NODE_HANDOFF 事件，让前端 DAG 图能看到数据流向 + 审计回放可追踪
                # M3 协作可视化：enrich from_role/to_role/summary（向后兼容，老消费者忽略即可）
                from_role = resolve_business_role(nstate.node, fallback_id=node_id)
                target_node = self.workflow.nodes.get(target)
                to_role = resolve_business_role(target_node, fallback_id=target)
                payload_summary = gen_handoff_summary(port, payload, target=target)
                await self._emit(DagEventType.NODE_HANDOFF, node_id, {
                    "from": node_id,
                    "to": target,
                    "port": port,
                    "incoming_port": incoming_port,
                    "payload_size": len(str(payload)),
                    "from_role": from_role,
                    "to_role": to_role,
                    "summary": payload_summary,
                })

        # BLOCKED 检测：所有输出端口都以 BLOCKED 信号收尾时，
        # 标记节点失败（替代 COMPLETED），让 BFS 循环停止下游传播
        if self._all_ports_blocked(nstate):
            nstate.status = NodeStatus.FAILED
            nstate.error = "所有输出端口均返回 BLOCKED/ERROR 信号，无实际产出"
            self.run_state.node_states[node_id] = nstate.status
            resolved = nstate.resolved_model or self._resolve_model_for_node(nstate.node)
            await self._emit(DagEventType.NODE_FAILED, node_id, {
                "error": nstate.error,
                "agent": nstate.node.agent,
                "provider_id": (resolved or {}).get("provider", ""),
                "model": (resolved or {}).get("model", ""),
                "error_type": "execution_blocked",
            })
            return

        await self._emit(DagEventType.NODE_COMPLETED, node_id, {
            "agent": nstate.node.agent,
            "duration_ms": nstate.duration_ms,
            "tokens_in": nstate.tokens_in,
            "tokens_out": nstate.tokens_out,
            "outputs": {p: nstate.pending_handoffs.get(p) for p in nstate.node.outputs},
        })

        # 用量落库
        if self._event_store and nstate.resolved_model:
            await self._record_node_usage(node_id, nstate)

        # If we provisioned a docker container for this node, try to terminate and cleanup
        await self._cleanup_provisioned_subagent(nstate, cleanup_status="released")

    def _try_restore_node(
        self, node: WorkflowNode, nstate: NodeExecutionState
    ) -> bool:
        """断点恢复：检查 agent 的 output_files 是否已全部存在于磁盘。

        如果所有 output_files 都存在，从文件恢复 pending_handoffs，返回 True。
        否则返回 False，节点正常执行。

        对 deterministic harness 无效（它没有 output_files，走 handoff tool handler）。
        """
        if node.harness == HarnessTypeRef.DETERMINISTIC:
            return False

        agent_def = self._get_agent_def(node.agent)
        if not agent_def or not agent_def.output_files:
            return False

        ws_root = self._workspace_root()
        # 检查所有 output_files 是否都存在
        all_exist = True
        for port, pattern in agent_def.output_files.items():
            if port not in node.outputs:
                continue
            file_path = pattern.replace("{{workspace.root}}", ws_root)
            file_path = file_path.replace("{{run_id}}", self.run_state.run_id)
            path = Path(file_path)
            if not path.exists() and not path.is_dir():
                # 目录路径不以 / 结尾时也可能存在
                if not file_path.endswith("/"):
                    all_exist = False
                    break

        if not all_exist:
            return False

        # 所有文件都在 → 从文件恢复 pending_handoffs
        logger.info(f"Node {node.id}: restoring from existing output_files (断点恢复)")
        self._harvest_file_outputs(node, nstate)

        # 如果收割后 pending_handoffs 仍为空，说明文件内容为空，不恢复
        if not nstate.pending_handoffs:
            return False

        return True

    async def resume(self, run_id: str, inputs: dict[str, Any]) -> RunState:
        """从指定 run_id 断点恢复执行。

        使用已有的 run_id（不生成新 ID），这样 workspace/{workflow_id}/{run_id}/
        下已有的文件会被 _try_restore_node 检测到，自动跳过已完成的节点。

        用法:
            engine = DagEngine(workflow=wf, event_sink=sink)
            state = await engine.resume(run_id="run_1234567890", inputs={"topic": "..."})
        """
        self.run_state.run_id = run_id
        return await self.run(inputs)

    async def _run_agent_node(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
    ) -> None:
        """Execute a real agent node via harness.

        D-029 修复：节点失败时若错误类型属于 fallback 触发条件（rate_limit/timeout），
        且 FallbackChain 配置了当前 provider 的 fallback，则切换 fallback provider
        重试一次。遵循 fail-loud > fail-quiet，未配置 fallback 时原样 raise。
        """
        nstate.status = NodeStatus.RUNNING
        nstate.started_at = datetime.now(timezone.utc)
        if node.harness != HarnessTypeRef.DETERMINISTIC:
            nstate.resolved_model = self._resolve_model_for_node(node)
        # P0.5: inline_agent.harness 优先于 node.harness（与 _execute_agent_node 一致）
        effective_harness = node.inline_agent.harness if node.inline_agent else node.harness
        await self._emit(DagEventType.NODE_STARTED, node.id, {
            "agent": node.agent,
            "harness": effective_harness.value,
        })

        # Emit widget.update for widgets with emit_on_event=node.started
        await self._emit_widgets_for_node(node.id, event="node.started")

        # Collect upstream outputs as current inputs
        for dep in node.after:
            dep_outputs = nstate.upstream_outputs.get(dep, {})
            for port, payload in dep_outputs.items():
                nstate.current_inputs[f"{dep}.{port}"] = payload

        # Add declared input names from global inputs
        for input_name in node.inputs:
            if input_name in global_inputs:
                nstate.current_inputs[input_name] = global_inputs[input_name]

        try:
            await self._execute_node(node, nstate, global_inputs)
        except Exception as e:
            error_type = self._classify_error_type(str(e))
            # D-029：尝试 fallback provider 重试一次
            original_provider = (nstate.resolved_model or {}).get("provider", "")
            fallback_resolved = self._try_resolve_fallback(node, error_type, original_provider)
            if fallback_resolved:
                logger.warning(
                    "Node %s 失败（%s），切换 fallback provider=%s 重试",
                    node.id, error_type, fallback_resolved.get("provider", ""),
                )
                # 清理当前失败尝试留下的 provisioned subagent/container，避免资源泄露。
                await self._cleanup_provisioned_subagent(nstate, cleanup_status="failed")
                nstate.provisioned_subagent_id = None
                nstate.provisioned_worker_id = None
                nstate.provisioned_container_id = None
                # 重置 nstate 状态准备重试
                nstate.error = None
                nstate.text_outputs = []
                nstate.pending_handoffs = {}
                nstate.tokens_in = 0
                nstate.tokens_out = 0
                nstate.resolved_model = fallback_resolved
                try:
                    await self._execute_node(
                        node, nstate, global_inputs,
                        override_resolved_model=fallback_resolved,
                    )
                    logger.info(
                        "Node %s fallback 重试成功（provider=%s）",
                        node.id, fallback_resolved.get("provider", ""),
                    )
                    return
                except Exception as retry_err:
                    logger.error(
                        "Node %s fallback 重试也失败: %s", node.id, retry_err
                    )
                    nstate.status = NodeStatus.FAILED
                    nstate.error = str(retry_err)
                    nstate.finished_at = datetime.now(timezone.utc)
                    await self._emit(DagEventType.NODE_FAILED, node.id, {
                        "error": str(retry_err),
                        "agent": node.agent,
                        "provider_id": fallback_resolved.get("provider", ""),
                        "model": fallback_resolved.get("model", ""),
                        "error_type": self._classify_error_type(str(retry_err)),
                        "fallback_from": original_provider,
                    })
                    await self._cleanup_provisioned_subagent(nstate, cleanup_status="failed")
                    raise

            nstate.status = NodeStatus.FAILED
            nstate.error = str(e)
            nstate.finished_at = datetime.now(timezone.utc)
            resolved = nstate.resolved_model or self._resolve_model_for_node(node)
            await self._emit(DagEventType.NODE_FAILED, node.id, {
                "error": str(e),
                "agent": node.agent,
                "provider_id": (resolved or {}).get("provider", ""),
                "model": (resolved or {}).get("model", ""),
                "error_type": error_type,
            })
            await self._cleanup_provisioned_subagent(nstate, cleanup_status="failed")
            raise

    def _try_resolve_fallback(
        self,
        node: WorkflowNode,
        error_type: str,
        current_provider: str,
    ) -> dict[str, str] | None:
        """D-029：检查错误是否触发 fallback，返回 fallback resolved_model 或 None。

        触发条件：
        - error_type ∈ {rate_limit, timeout}（provider 临时不可用，切换有意义）
        - FallbackChain 配置了 current_provider 的 fallback
        - node 不是 DETERMINISTIC harness
        - fallback provider 在 models.yaml 中存在且有 model

        不触发 auth_error（key 错误切换 provider 也救不了）/ protocol_mismatch /
        not_found / unknown（这些是配置问题，不是 provider 临时不可用）。
        """
        if node.harness == HarnessTypeRef.DETERMINISTIC:
            return None
        if error_type not in ("rate_limit", "timeout"):
            return None
        if not current_provider:
            return None
        try:
            from orchestrator.provider_health import get_fallback_chain
            from orchestrator.model_config import get_model_config

            chain = get_fallback_chain()
            if not chain.has_chain(current_provider):
                return None
            # 取首个 fallback 条目（含显式 model，如未指定则为 None，用 provider 默认 model）
            entry = chain.get_fallback_entry(current_provider)
            if not entry:
                return None
            fallback_provider = entry["provider"]
            mc = get_model_config()
            provider = mc.get_provider(fallback_provider)
            if not provider:
                logger.warning(
                    "Fallback provider '%s' 未在 models.yaml 配置", fallback_provider
                )
                return None
            # 优先用链中显式指定的 model；缺省时取 provider 第一个 model
            explicit_model = entry.get("model")
            if explicit_model:
                model_id = explicit_model
            else:
                models = provider.get("models", [])
                if not models:
                    logger.warning(
                        "Fallback provider '%s' 无可用 model", fallback_provider
                    )
                    return None
                model_id = models[0].get("id", "")
                if not model_id:
                    return None
            return mc.resolve(node_model={"provider": fallback_provider, "id": model_id})
        except Exception as e:
            logger.warning("Fallback 解析失败: %s", e)
            return None

    async def _cleanup_provisioned_subagent(
        self,
        nstate: NodeExecutionState,
        cleanup_status: str = "released",
    ) -> None:
        """Cleanup docker container and terminate provisioned subagent.

        P0.18.5: 若容器通过 ContainerProvisioner 启动（_provisioned_via_provisioner[node.id]=True），
        走 provisioner.deprovision 4 步销毁（stop→kill→remove→verify + sandbox 延迟清理标记）；
        否则走旧路径（docker_runtime.stop + remove）。
        """
        if not nstate.provisioned_subagent_id and not nstate.provisioned_container_id:
            return

        node_id = nstate.node.id
        used_provisioner = self._provisioned_via_provisioner.get(node_id, False)

        # P0.18.5: 走 ContainerProvisioner.deprovision 4 步销毁路径
        if used_provisioner and self._container_provisioner and self._workspace_context:
            try:
                from orchestrator.workspace_paths import WorkspaceInfo
                ws_ctx = self._workspace_context
                workspace_info = WorkspaceInfo(
                    workspace_id=ws_ctx.get("workspace_id", ""),
                    display_name=ws_ctx.get("display_name", ""),
                    mode=ws_ctx.get("workspace_mode", "isolated"),
                    permissions=ws_ctx.get("permissions", "read_write"),
                    source_path=ws_ctx.get("source_path"),
                    git_url=ws_ctx.get("git_url"),
                    git_branch=ws_ctx.get("git_branch"),
                    enabled=True,
                )
                await self._container_provisioner.deprovision(
                    container_id=nstate.provisioned_container_id,
                    workspace=workspace_info,
                    subagent_id=nstate.provisioned_subagent_id,
                    run_id=self.run_state.run_id,
                    force=(cleanup_status == "failed"),
                )
                # deprovision 已更新 subagent status，无需再调 terminate_subagent
                self._provisioned_via_provisioner.pop(node_id, None)
                return
            except Exception as e:
                logger.warning(
                    "provisioner.deprovision failed for node %s, fallback to legacy path: %s",
                    node_id, e,
                )
                # 失败回退到旧路径

        # 旧路径：docker_runtime.stop + remove（stop→rm -f 兜底 + verify）
        if nstate.provisioned_container_id:
            cid = nstate.provisioned_container_id
            # 1. stop（10s grace 让 codex 优雅退出，docker_runtime 默认 timeout=10）
            stopped = False
            try:
                await asyncio.to_thread(docker_runtime.stop_container, cid)
                stopped = True
            except Exception as e:
                logger.warning("Failed to stop container %s: %s", cid, e)
            # 2. remove(force=True) 兜底强删（即使 stop 失败也能清理）
            removed = False
            try:
                await asyncio.to_thread(docker_runtime.remove_container, cid, True)
                removed = True
            except Exception as e:
                logger.warning("Failed to remove container %s: %s", cid, e)
            # 3. verify（dockerCleanupVerified，确认容器真的没了）
            if removed:
                try:
                    still_exists = await asyncio.to_thread(docker_runtime.container_exists, cid)
                    if still_exists:
                        logger.warning("Container %s still exists after remove (stopped=%s)", cid, stopped)
                    else:
                        logger.info("Container %s cleaned up (stopped=%s removed=%s)", cid, stopped, removed)
                except Exception as e:
                    logger.debug("Container verify failed (non-fatal): %s", e)

        # P0.18.5: 旧路径也补 sandbox 延迟清理标记（local_copy / git_clone 模式）
        if (
            self._event_store
            and self._workspace_context
            and self._workspace_context.get("workspace_mode") in ("local_copy", "git_clone")
        ):
            try:
                from datetime import datetime, timezone, timedelta
                cleanup_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                await self._event_store.mark_sandbox_for_cleanup(
                    workspace_id=self._workspace_context.get("workspace_id", ""),
                    run_id=self.run_state.run_id,
                    cleanup_at=cleanup_at,
                )
            except Exception as e:
                logger.warning("mark_sandbox_for_cleanup failed (non-fatal): %s", e)

        if not self._event_store or not nstate.provisioned_subagent_id:
            return

        try:
            await self._event_store.terminate_subagent(
                nstate.provisioned_subagent_id,
                cleanup_status=cleanup_status,
            )
        except Exception as e:
            logger.warning("Failed to terminate subagent %s: %s", nstate.provisioned_subagent_id, e)

    def _try_provision_via_provisioner(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        subagent_id: str,
        lease_generation: int,
        image: str,
    ) -> bool:
        """P0.18.5: 同步前置检查 — 是否具备走 ContainerProvisioner 的条件。

        真正的 provision 在 _provision_via_provisioner_async 中执行（async）。
        本方法只做前置检查（provisioner 已注入 + workspace_context 存在 + event_store 存在），
        避免每次都进 async 路径浪费协程切换。

        返回 True 表示可以走 provisioner，调用方应 await _provision_via_provisioner_async；
        False 表示条件不满足，调用方应走旧路径。
        """
        if not self._container_provisioner:
            return False
        if not self._workspace_context:
            return False
        if not self._event_store:
            return False
        # 方案A：codex harness + docker_container → 跳过 provisioner（不需要 WS bridge）
        # 容器内只跑 codex subagent（docker exec），不启动 agentops 服务。
        # ContainerProvisioner 会等 30s WS 注册（bridge.js 不连 manager WS → 必超时），
        # 跳过它直接走 legacy path（create_and_start_container）即可。
        effective_harness = node.inline_agent.harness if node.inline_agent else node.harness
        placement = getattr(node, "runtime_placement", None)
        placement_val = placement.value if placement is not None else "in_process"
        if effective_harness == HarnessTypeRef.CODEX and placement_val == "docker_container":
            return False
        return True

    async def _provision_via_provisioner_async(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        subagent_id: str,
        lease_generation: int,
        image: str,
    ) -> bool:
        """P0.18.5: 真正调 ContainerProvisioner.provision 5 步启动。

        成功返回 True 并填充 nstate.provisioned_worker_id / provisioned_container_id；
        失败返回 False 让调用方回退旧路径。

        5 步：
        1. build mount list（workspace.mode 决定）
        2. build labels（治理 + 检索）
        3. build env（连接信息 + LLM 配置，不含凭据明文）
        4. create + start container
        5. wait WS connect 30s + record_provisioned_worker
        """
        try:
            from orchestrator.container_provisioner import ProvisionRequest
            from orchestrator.workspace_paths import (
                WorkspaceInfo,
                PreparedWorkspace,
                prepare_workspace,
            )
            from orchestrator.config_loader import get_system_config
        except ImportError as e:
            logger.warning("P0.18.5 import failed: %s", e)
            return False

        try:
            ws_ctx = self._workspace_context
            workspace_info = WorkspaceInfo(
                workspace_id=ws_ctx.get("workspace_id", ""),
                display_name=ws_ctx.get("display_name", ""),
                mode=ws_ctx.get("workspace_mode", "isolated"),
                permissions=ws_ctx.get("permissions", "read_write"),
                source_path=ws_ctx.get("source_path"),
                git_url=ws_ctx.get("git_url"),
                git_branch=ws_ctx.get("git_branch"),
                enabled=True,
            )
            prepared = await prepare_workspace(
                self._event_store, workspace_info, self.run_state.run_id,
            )

            # 解析 agent tier（从 agent yaml 读取；inline_agent 节点用默认 T2）
            agent_tier = "T2"
            try:
                cfg = get_system_config()
                agent_def = cfg.agents.get(node.agent) if node.agent else None
                if agent_def and getattr(agent_def, "tier", None):
                    agent_tier = agent_def.tier
            except Exception:
                pass

            # resolved_model 占位（实际 model 解析在 _execute_node 后续步骤，这里只供 env 注入）
            resolved_model = {
                "provider": self.llm_config.get("model_provider", ""),
                "model": self.llm_config.get("model", ""),
                "api_key": self.llm_config.get("api_key", ""),
                "base_url": self.llm_config.get("base_url", ""),
            }

            req = ProvisionRequest(
                run_id=self.run_state.run_id,
                node_id=node.id,
                subagent_id=subagent_id,
                lease_generation=lease_generation,
                workspace=workspace_info,
                prepared=prepared,
                agent_tier=agent_tier,
                image=image,
                resolved_model=resolved_model,
                agent_type=node.agent or "generic",
            )

            result = await self._container_provisioner.provision(req)
            nstate.provisioned_worker_id = result.worker_id
            nstate.provisioned_container_id = result.container_id
            return True
        except Exception as e:
            logger.warning(
                "provisioner.provision failed for node %s: %s",
                node.id, e,
            )
            # 标记 subagent 失败
            if self._event_store:
                try:
                    await self._event_store.update_subagent_status(
                        subagent_id, "failed", error=str(e),
                    )
                except Exception:
                    pass
            return False

    async def _run_parallel_branch_node(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
    ) -> None:
        """parallel_branch: virtual aggregator.

        Branches are scheduled as sibling DAG nodes (same level). By the time this
        node runs at its level, all branches should have completed (because we
        process level-by-level). It just aggregates branch outputs.
        """
        nstate.status = NodeStatus.RUNNING
        nstate.started_at = datetime.now(timezone.utc)
        await self._emit(DagEventType.NODE_STARTED, node.id, {
            "type": "parallel_branch",
            "join_strategy": node.join_strategy,
            "branches": node.branches,
        })

        # Aggregate branch outputs
        aggregated_payload = {"branches": []}
        for branch_id in node.branches:
            branch_state = self.node_states.get(branch_id)
            if branch_state and branch_state.pending_handoffs:
                for hport, hpayload in branch_state.pending_handoffs.items():
                    aggregated_payload["branches"].append({
                        "branch": branch_id,
                        "port": hport,
                        "payload": hpayload,
                    })

        # Emit one synthetic handoff per declared output port
        for port in node.outputs.keys():
            nstate.pending_handoffs[port] = aggregated_payload

    async def _run_gateway_node(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
    ) -> None:
        """gateway (condition/loop): virtual routing node.

        v0: stub — always takes the first declared output port.
        Future: evaluate node.condition against nstate.upstream_outputs.
        """
        nstate.status = NodeStatus.RUNNING
        nstate.started_at = datetime.now(timezone.utc)
        await self._emit(DagEventType.NODE_STARTED, node.id, {
            "type": "gateway",
            "kind": node.gateway_kind.value if node.gateway_kind else None,
        })

        if node.outputs:
            first_port = next(iter(node.outputs.keys()))
            nstate.pending_handoffs[first_port] = {
                "routed_by": node.gateway_kind.value if node.gateway_kind else "condition",
                "selected_port": first_port,
                "condition": node.condition,
            }

    # === P0.1: 3 类新增节点原语执行 ===

    async def _run_command_node(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
    ) -> None:
        """P0.1: command 节点 — CLI / 二进制确定性执行。

        关键区别：不调 LLM，直接 asyncio.create_subprocess_shell。
        模板中的 `{input_name}` 占位符由 inputs 替换。
        """
        assert node.command_config is not None, "validator 已保证 type=command 必填"
        cfg = node.command_config

        nstate.status = NodeStatus.RUNNING
        nstate.started_at = datetime.now(timezone.utc)
        await self._emit(DagEventType.NODE_STARTED, node.id, {
            "type": "command",
            "cli_template": cfg.cli_template[:200],  # 防日志爆炸
            "timeout_seconds": cfg.timeout_seconds,
        })

        # 模板替换：{input_name} ← global_inputs 优先，fallback 到 upstream outputs
        # 修复：原实现仅从 global_inputs 取值，导致 command→command 数据流断裂
        # （如 plan_sql 产出的 sql 无法传递给 validate_sql / execute_query）
        rendered_cli = cfg.cli_template
        try:
            for input_name in node.inputs:
                placeholder = "{" + input_name + "}"
                if placeholder not in rendered_cli:
                    continue
                # 1. 优先从 global_inputs 取（workflow 级输入如 database）
                if input_name in global_inputs:
                    rendered_cli = rendered_cli.replace(
                        placeholder, str(global_inputs[input_name])
                    )
                    continue
                # 2. fallback：从 upstream outputs 按端口名查找
                #    agent handoff payload = {content, summary} → 取 content
                #    command success payload = {cli, stdout, parsed, ...} → 取 parsed（或 stdout）
                for src, ports in nstate.upstream_outputs.items():
                    if input_name not in ports:
                        continue
                    payload = ports[input_name]
                    if isinstance(payload, dict):
                        if "content" in payload:
                            value = payload["content"]
                        elif "parsed" in payload and payload["parsed"] is not None:
                            value = payload["parsed"]
                        elif "stdout" in payload:
                            value = payload["stdout"]
                        else:
                            value = str(payload)
                    else:
                        value = str(payload)
                    rendered_cli = rendered_cli.replace(placeholder, str(value))
                    break

            # 执行 CLI
            # 解释器锚定（D-068）：见 anchor_cli_interpreter docstring
            rendered_cli = anchor_cli_interpreter(rendered_cli)

            # 修复：command 节点 cwd 默认 _workspace_root()，但该目录可能未创建，
            # Windows 下未创建的目录作为 cwd 会报 WinError 267（目录名称无效）。
            # 优先使用配置中的 cwd；否则需决定用 PROJECT_ROOT 还是 workspace。
            #
            # 关键约束（D-0xx）：cli_template 里的相对路径脚本（如 tools/db_cli.py）
            # 必须相对 PROJECT_ROOT 解析，而不是 workspace 沙箱。workspace 沙箱
            # 由 engine.py 的 workspace_root 落地逻辑无条件 os.makedirs 创建，是
            # 空目录（无 tools/），一旦被选作 cwd 会让相对脚本报 Errno 2「文件不存在」。
            # 因此：CLI 引用相对路径脚本时，优先验证 PROJECT_ROOT 下脚本存在 → 用
            # PROJECT_ROOT；脚本在 PROJECT_ROOT 不存在但 workspace 存在时，才回退
            # workspace（覆盖真正跑在沙箱产物上的场景）。
            cmd_cwd = cfg.cwd or str(PROJECT_ROOT)
            if not cfg.cwd:
                ws_candidate = self._workspace_root()
                if _cli_refers_to_project_script(rendered_cli) and (
                    ws_candidate != str(PROJECT_ROOT)
                ):
                    # 相对脚本场景：PROJECT_ROOT 下有脚本就用它，避免误入空沙箱
                    cmd_cwd = str(PROJECT_ROOT)
                elif Path(ws_candidate).is_dir():
                    cmd_cwd = ws_candidate
            proc = await asyncio.create_subprocess_shell(
                rendered_cli,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cmd_cwd,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=cfg.timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    f"command 节点 '{node.id}' CLI 超时 "
                    f"({cfg.timeout_seconds}s): {rendered_cli}"
                )

            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

            success = proc.returncode == cfg.success_exit_code

            # 解析 stdout 到 outputs
            output_value: Any = None
            if success and cfg.parse_stdout:
                # parse_stdout 是 Python 表达式，注入 stdout/stderr/returncode。
                # 用受限 globals（__builtins__ 为最小集 + 显式注入 json/re，方便常见表达式如 json.loads）。
                local_ns = {
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": proc.returncode,
                }
                safe_globals = {
                    "__builtins__": __builtins__,
                    "json": json,
                    "re": __import__("re"),
                }
                output_value = eval(cfg.parse_stdout, safe_globals, local_ns)

            nstate.pending_handoffs["success"] = {
                "cli": rendered_cli,
                "stdout": stdout,
                "stderr": stderr,
                "returncode": proc.returncode,
                "parsed": output_value,
            }
            if not success:
                # 失败时把 failure port 也填，便于下游 condition 路由
                nstate.pending_handoffs["failure"] = {
                    "cli": rendered_cli,
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": proc.returncode,
                }

            await self._emit(DagEventType.NODE_COMPLETED, node.id, {
                "type": "command",
                "exit_code": proc.returncode,
                "success": success,
            })
        except Exception as e:
            logger.exception("command 节点 '%s' 执行失败", node.id)
            await self._emit(DagEventType.NODE_FAILED, node.id, {
                "type": "command",
                "error": str(e)[:500],
            })
            raise

    async def _run_while_node(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
    ) -> None:
        """P0.1: while 节点 — 反馈循环控制（仅做条件判断，不递归执行子节点）。

        当前实现：evaluate node.while_config.continue_if 表达式 → 若为真，标记
        node 进入 PENDING_BACK（让 DAG 调度器重新触发下游），否则走 done port。
        完整的循环迭代由 DagEngine 的反馈边 max_traversals 控制。
        """
        assert node.while_config is not None, "validator 已保证 type=while 必填"
        cfg = node.while_config

        nstate.status = NodeStatus.RUNNING
        nstate.started_at = datetime.now(timezone.utc)
        await self._emit(DagEventType.NODE_STARTED, node.id, {
            "type": "while",
            "max_iterations": cfg.max_iterations,
            "continue_if": cfg.continue_if,
        })

        # 评估 continue_if 表达式
        # 输入：node.inputs 字段值（来自上游 handoff）
        input_values = {k: global_inputs.get(k) for k in node.inputs}
        try:
            # continue_if 是 Python 表达式，注入 input_values
            should_continue = bool(
                eval(cfg.continue_if, {"__builtins__": {}}, input_values)
            )
        except Exception as e:
            logger.warning(
                "while 节点 '%s' continue_if 表达式求值失败: %s，按 False 处理",
                node.id, e,
            )
            should_continue = False

        # 当前 iter 计数由 nstate 维护（首次创建时=0）
        current_iter = getattr(nstate, "while_iteration", 0)
        current_iter += 1
        nstate.while_iteration = current_iter  # type: ignore[attr-defined]

        if should_continue and current_iter < cfg.max_iterations:
            # 继续循环：触发 feedback edge（DAG 调度器按 max_traversals 终止）
            nstate.pending_handoffs["continue"] = {
                "iteration": current_iter,
                "max_iterations": cfg.max_iterations,
                "continue_if_result": True,
            }
            await self._emit(DagEventType.NODE_COMPLETED, node.id, {
                "type": "while",
                "iteration": current_iter,
                "decision": "continue",
            })
        else:
            # 循环结束：走 done / exhausted port
            exit_port = "done" if not should_continue else "exhausted"
            nstate.pending_handoffs[exit_port] = {
                "iteration": current_iter,
                "max_iterations": cfg.max_iterations,
                "continue_if_result": should_continue,
            }
            await self._emit(DagEventType.NODE_COMPLETED, node.id, {
                "type": "while",
                "iteration": current_iter,
                "decision": exit_port,
            })

    async def _run_await_command_node(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
    ) -> None:
        """P0.1: await_command 节点 — 占位实现。

        完整实现需要：
        1. SSE 监听 chat_input_queue（actor 发命令时触发）
        2. 命令注入：把 input 喂回对应 actor 子 run 触发新一轮 handoff
        3. 直到 max_commands 或 expiry_seconds
        4. emit AWAIT_COMMAND_TIMEOUT → 走 timeout port

        当前 v99 P0.1 阶段先实现基础架构：注册 listener、立即 emit 完成事件。
        完整 SSE 多轮由后续 v101 §4.3 Supervision 模式补完（transport fence + inject 工具）。
        """
        assert node.await_command_config is not None, "validator 已保证"
        cfg = node.await_command_config

        nstate.status = NodeStatus.RUNNING
        nstate.started_at = datetime.now(timezone.utc)
        await self._emit(DagEventType.NODE_STARTED, node.id, {
            "type": "await_command",
            "target_actors": cfg.target_actors,
            "expiry_seconds": cfg.expiry_seconds,
            "max_commands": cfg.max_commands,
        })

        # P0.1 占位：默认走 timeout port（完整实现由 v101 supervision 模式补）
        # 注册 listener（仅记录，实际不阻塞）
        logger.info(
            "await_command 节点 '%s' 启动（target_actors=%s, expiry=%ds, max=%d）"
            "—— P0.1 占位实现，完整 multi-round 由 v101 supervision 模式补完",
            node.id, cfg.target_actors, cfg.expiry_seconds, cfg.max_commands,
        )

        nstate.pending_handoffs["timeout"] = {
            "expiry_seconds": cfg.expiry_seconds,
            "max_commands": cfg.max_commands,
            "reason": "P0.1_placeholder_until_supervision_mode",
        }

        await self._emit(DagEventType.NODE_COMPLETED, node.id, {
            "type": "await_command",
            "decision": "timeout",
            "note": "P0.1_placeholder",
        })

    async def _execute_node(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
        override_resolved_model: dict[str, str] | None = None,
        prompt_override: str | None = None,
        turn_timeout: float | None = None,
    ) -> None:
        """Dispatch node to its harness and run the agent loop.

        DAG 节点按 node.harness 分发到对应执行体（local_llm / deterministic /
        opencode / claude_code 等）。子循环结束后，handoff payload 写入 nstate.pending_handoffs。

        D-029: override_resolved_model 用于 fallback 重试时强制使用指定 provider，
        跳过 _resolve_model 的常规优先级链。

        Correction（active-runs.ts:1838-1895）：
          - prompt_override 非空时为 correction 轮 —— 复用已 provision 的
            subagent/container（不重复 provision），跳过 started 骨架投影，
            且不再递归触发 correction 检查（由调用方控制）。
          - turn_timeout 为本轮独立的超时预算（correction 轮应远小于节点超时）。
        """
        # 普通模式：Build prompt → create harness → run
        prompt = prompt_override or self._build_prompt(node, nstate, global_inputs)

        # P0.5: 决定 effective harness — inline_agent 优先
        effective_harness = node.inline_agent.harness if node.inline_agent else node.harness

        # Provision subagent record and physical worker if requested by runtime_placement
        placement = getattr(node, "runtime_placement", None)
        placement_val = placement.value if placement is not None else "in_process"
        if self._event_store and nstate.provisioned_subagent_id is None:
            try:
                subagent_id = str(uuid.uuid4())
                actor_id = f"{self.run_state.run_id}:{node.id}"
                # lease_generation: increment for this run/node
                try:
                    lease_generation = await self._event_store.increment_lease_generation(self.run_state.run_id, node.id)
                except Exception:
                    lease_generation = 1
                await self._event_store.provision_subagent(
                    subagent_id=subagent_id,
                    actor_id=actor_id,
                    run_id=self.run_state.run_id,
                    node_id=node.id,
                    harness_type=effective_harness.value,
                    lease_generation=lease_generation,
                    runtime_placement=placement_val,
                )
                # remember in node state for later cleanup
                nstate.provisioned_subagent_id = subagent_id
                # If docker placement requested, create container and record worker
                if placement_val == "docker_container":
                    # image can be specified in node.config.runtime_image, else fallback
                    image = (node.config or {}).get("runtime_image") or "python:3.11-slim"
                    # P0.18.5: 优先走 ContainerProvisioner（5 步启动 + tier 资源限制 + WS 等待）
                    # 失败或未注入 provisioner 时回退到旧路径（保持向后兼容）
                    provisioned_ok = False
                    if self._try_provision_via_provisioner(node, nstate, subagent_id, lease_generation, image):
                        provisioned_ok = await self._provision_via_provisioner_async(
                            node, nstate, subagent_id, lease_generation, image,
                        )
                        if provisioned_ok:
                            self._provisioned_via_provisioner[node.id] = True
                    if not provisioned_ok:
                        # 旧路径：docker_runtime.create_and_start_container
                        name = f"ao_{self.run_state.run_id}_{node.id}_{lease_generation}"
                        container_id: str | None = None
                        try:
                            container_info = await asyncio.to_thread(docker_runtime.create_and_start_container, image, name)
                            container_id = container_info.get("id")
                            worker_id = container_id
                            nstate.provisioned_worker_id = worker_id
                            nstate.provisioned_container_id = container_id
                            await self._event_store.record_provisioned_worker(subagent_id, lease_generation, worker_id, placement_val, container_id=container_id)
                            await self._event_store.update_subagent_status(subagent_id, "running")
                        except Exception as e:
                            if container_id:
                                try:
                                    await asyncio.to_thread(docker_runtime.stop_container, container_id)
                                except Exception as stop_err:
                                    logger.warning("Failed to stop container %s after provision error: %s", container_id, stop_err)
                                try:
                                    await asyncio.to_thread(docker_runtime.remove_container, container_id, False)
                                except Exception as remove_err:
                                    logger.warning("Failed to remove container %s after provision error: %s", container_id, remove_err)
                            # mark subagent failed and re-raise
                            try:
                                await self._event_store.update_subagent_status(subagent_id, "failed", error=str(e))
                            except Exception:
                                pass
                            raise
            except Exception as e:
                logger.error("provision_subagent failed for node %s: %s", node.id, e)
                raise

        # Create harness
        harness = self._create_harness(effective_harness)

        # Build tools (DAG built-in + agent allowed_tools)
        tools = make_dag_tools(
            self.workflow, node, nstate,
            run_id=self.run_state.run_id,
            event_sink=self.event_sink,
        )

        # v99.5 P0.2.4 + OPT-1: 注入 surface 工具（如该节点 actor 有 visual profile）
        # OPT-1 模板化：actor 全部 view 声明 template 时注入 fields-only 版
        # report_surface_state（agent 只传 view_id + phase + data_model），
        # 否则回退 present_content_surface（content_type + data 高层语义模式）。
        # actor_id 从 node.business_role / actor_id / agent / id 推导
        # profile 加载失败也不阻断节点执行（不抛错，只是不注入）
        try:
            from orchestrator.actor_visual_profile import (
                load_actor_visual_profile,
                resolve_actor_id_from_node,
            )
            actor_id = resolve_actor_id_from_node(node)
            if actor_id:
                try:
                    _profile = load_actor_visual_profile(actor_id)
                except Exception:
                    _profile = None
                _all_templated = (
                    _profile is not None
                    and bool(_profile.allowed_surface_views)
                    and all(
                        v.template is not None
                        for v in _profile.allowed_surface_views.values()
                    )
                )
                if _all_templated:
                    from orchestrator.actor_visual_profile import (
                        make_report_surface_state_tool,
                    )
                    surface_tool = make_report_surface_state_tool(
                        actor_id=actor_id,
                        run_id=self.run_state.run_id,
                        event_sink=self.event_sink,
                        node_id=node.id,
                    )
                    tool_kind = "report_surface_state(fields-only)"
                else:
                    # present_content_surface 已裁撤（v2），此分支为历史回退路径。
                    # 懒加载避免 fields-only 主路径被悬空 import 阻断。
                    from orchestrator.present_content import make_present_content_surface_tool
                    surface_tool = make_present_content_surface_tool(
                        actor_id=actor_id,
                        run_id=self.run_state.run_id,
                        event_sink=self.event_sink,
                        node_id=node.id,
                    )
                    tool_kind = "present_content_surface"
                # profile 至少有 1 个 view 才注入（空 profile 的工具没用）
                if surface_tool.input_schema["properties"]["view_id"]["enum"]:
                    tools.append(surface_tool)
                    logger.info(
                        "节点 '%s' (actor='%s') 注入 %s 工具，"
                        "allowed_views=%d",
                        node.id, actor_id, tool_kind,
                        len(surface_tool.input_schema["properties"]["view_id"]["enum"]),
                    )
        except Exception as e:
            # profile 加载失败不阻断节点（兜底：工具不注入，agent 仍可正常 handoff）
            logger.warning(
                "节点 '%s' 注入 present_content_surface 工具失败（不影响执行）: %s",
                node.id, e,
            )

        # OPT-1: 节点启动时系统投影骨架 surface（source=system, phase=started）。
        # 即使 agent 全程不调 surface 工具，前端也能看到 started 骨架卡；
        # agent 后续 emit 的业务卡按 phase 单调推进自然覆盖骨架。
        # correction 轮跳过（首轮已投影）。
        if prompt_override is None:
            try:
                from orchestrator.surface_projector import project_node_started_skeleton
                await project_node_started_skeleton(
                    node, self.run_state.run_id, self.event_sink
                )
            except Exception as e:
                logger.warning(
                    "节点 '%s' 系统投影骨架失败（不影响执行）: %s", node.id, e,
                )

        # P0 修复：加载 agent allowed_tools（log_query/wecom_notify 等）为 ToolDefinition，
        # 让 local_llm harness 也能通过 function calling 调用这些工具
        # P0.5: inline_agent 节点用 inline_agent.allowed_tools 加载 config tools
        from orchestrator.conversation_kit import _load_agent_extra_tools, _load_inline_agent_tools
        if node.inline_agent:
            extra_tools = _load_inline_agent_tools(node.inline_agent.allowed_tools)
        else:
            extra_tools = _load_agent_extra_tools(node.agent)
        # 去重：跳过已注册的同名工具（如 report_surface_state 已由上方自动注入）
        _existing_names = {t.name for t in tools}
        for t in extra_tools:
            if t.name not in _existing_names:
                tools.append(t)

        # P1 修复：注入 BUILTIN_TOOLS（write_file/read_file/bash）的 Python 实现，
        # 让 local_llm / codex harness 的 LLM 也能通过 function calling 调这些工具。
        # opencode harness 由 opencode server 自身实现，注入重复不会引发冲突（同名 ToolDefinition 后注册覆盖前注册），
        # 但更稳的策略是仅 local_llm / codex harness 注入（claude_code CLI 自管工具不注入）。
        if effective_harness in (HarnessTypeRef.LOCAL_LLM, HarnessTypeRef.CODEX):
            builtin_tools = self._make_local_llm_builtin_tools(nstate)
            # 只追加 agent 允许的 builtin（避免 LLM 看到未授权工具）
            # P0.5: inline_agent 节点用 inline_agent.allowed_tools；否则查全局 agent
            if node.inline_agent:
                allowed_set = set(node.inline_agent.allowed_tools or [])
            else:
                from orchestrator.config_loader import get_system_config
                try:
                    cfg = get_system_config()
                    agent_def = cfg.agents.get(node.agent)
                    allowed_set = set((agent_def.allowed_tools if agent_def else []) or [])
                except Exception:
                    allowed_set = set()
            for t in builtin_tools:
                if t.name in allowed_set:
                    if t.name == "bash":
                        ws_root = self._workspace_root()
                        logger.warning("Agent '%s' 启用了 bash shell 工具（cwd=%s）", node.agent, ws_root)
                    tools.append(t)

        # H2: 模型配置解析（deterministic 跳过；fallback 重试时用 override）
        resolved_model = None
        if override_resolved_model is not None:
            resolved_model = override_resolved_model
            nstate.resolved_model = resolved_model
        elif node.harness != HarnessTypeRef.DETERMINISTIC:
            resolved_model = self._resolve_model(node)
            nstate.resolved_model = resolved_model

        # Build context
        # opencode harness 需要 provider/model 格式（如 deepseek/deepseek-v4-pro）
        model_ref = ""
        if resolved_model:
            if node.harness == HarnessTypeRef.OPENCODE:
                model_ref = f"{resolved_model['provider']}/{resolved_model['model']}"
            else:
                model_ref = resolved_model["model"]
        ctx = AgentRunContext(
            system_prompt=self._build_system_prompt(node, nstate, global_inputs),
            model=model_ref,
            api_key=resolved_model["api_key"] if resolved_model else "",
            base_url=resolved_model["base_url"] if resolved_model else "",
            protocol=resolved_model.get("protocol", "") if resolved_model else "",
            auth_type=resolved_model.get("auth_type", "") if resolved_model else "",
            workspace=self._workspace_root(),
            session_id=f"{self.run_state.run_id}.{node.id}",
            # 方案A：把已 provision 的 container_id 传给 harness，
            # codex harness 检测到 container_id 后走 docker exec 在容器内启动 codex
            container_id=nstate.provisioned_container_id,
        )

        # Run — 加超时保护，防止 harness 阻塞导致任务永远 running
        # correction 轮用独立（更短的）预算，避免挤占 level deadline
        node_timeout = (
            turn_timeout if turn_timeout is not None
            else self._get_node_timeout(node)
        )
        try:
            async with asyncio.timeout(node_timeout):
                async for event in harness.run(prompt, tools, ctx):
                    if self._cancel.is_set():
                        break
                    # Capture key info
                    if event.type == AgentEventType.TEXT and event.text:
                        nstate.text_outputs.append(event.text)
                        # 转发 TEXT 事件到 event_sink，让前端能看到 agent 实时输出
                        await self._emit(DagEventType.NODE_PROGRESS, node.id, {
                            "agent": node.agent,
                            "agent_text": event.text[:500],
                        })
                    elif event.type == AgentEventType.TOOL_USE and event.tool_name == "handoff":
                        pass  # handoff already queued via tool handler
                    elif event.type == AgentEventType.USAGE and event.usage:
                        nstate.tokens_in += event.usage.input_tokens
                        nstate.tokens_out += event.usage.output_tokens
                    elif event.type == AgentEventType.ERROR:
                        raise RuntimeError(event.error_message or "agent error")
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Node '{node.id}' timed out after {node_timeout}s"
            )

        # 文件收割：agent 通过 Write/Bash 写文件到 workspace 后，
        # harness.run 完成后按 output_files 映射读取文件填入 pending_handoffs。
        # 覆盖所有非 deterministic harness（opencode / codex / local_llm / claude_code），
        # 只要 agent 声明了 output_files 就收割。
        if node.harness != HarnessTypeRef.DETERMINISTIC and not nstate.pending_handoffs:
            self._harvest_file_outputs(node, nstate)

        # active-runs.ts:1821-1895：真实 agent harness 的 turn 以
        # 无 handoff 收尾时，禁止用 text_outputs 伪装交付（原兜底把 agent 的流程
        # 自述文本填进 output port，导致节点假完成、下游 auditor 审到垃圾 ——
        # run_20260818_062738_750165 actor_research 事故根因）。
        # 改为 correction 轮：限定 agent 只做交付（report_surface_state(final) +
        # handoff 各一次），最多 MAX_HANDOFF_CORRECTIONS 次，耗尽后 fail-loud。
        # P-fix(join_surfaces)：surface 停留在非 final phase（local_llm join 节点
        # 发完 partial 就收尾）同样触发 correction —— text 兜底只能补 handoff，
        # 补不了 final surface 卡片；直接进 finalize 会被 L0 铁律拒绝且无补救
        # 机会（run_20260818_075133_665947 join_surfaces 事故根因）。
        surface_missing_final = (
            effective_harness != HarnessTypeRef.DETERMINISTIC
            and self._required_surface_final_violation(node) is not None
        )
        if (
            prompt_override is None  # correction 轮内不再递归
            and node.outputs
            and (
                surface_missing_final
                or (
                    effective_harness not in (HarnessTypeRef.DETERMINISTIC, HarnessTypeRef.LOCAL_LLM)
                    and not nstate.pending_handoffs
                )
            )
        ):
            await self._request_handoff_correction(
                node, nstate, global_inputs, override_resolved_model,
            )
            if not nstate.pending_handoffs:
                raise RuntimeError(
                    f"Node '{node.id}' ended without a valid handoff after "
                    f"{self.MAX_HANDOFF_CORRECTIONS} correction attempt(s) "
                    f"(declared ports: {list(node.outputs.keys())})"
                )
            # surface 仍缺 final 时不 raise，交给 _finalize_completed_node 的
            # L0 兜底校验拒绝（真实 FAILED + NODE_FAILED 事件，可审计）

        # local_llm harness fallback：单轮轻量 LLM 可能以纯文本收尾（无 function
        # calling），保留 text_outputs 兜底填充行为（join/summary 类聚合节点依赖）。
        if effective_harness == HarnessTypeRef.LOCAL_LLM and nstate.text_outputs:
            full = "\n".join(nstate.text_outputs)
            for port in node.outputs:
                if port not in nstate.pending_handoffs:
                    nstate.pending_handoffs[port] = {
                        "content": full,
                        "summary": full[:200],
                    }

    # active-runs.ts:371 max_corrections_per_node=2
    MAX_HANDOFF_CORRECTIONS = 2

    async def _request_handoff_correction(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
        override_resolved_model: dict[str, str] | None = None,
    ) -> None:
        """turn 结束但未 handoff / surface 缺 final → correction 轮（requestNodeCorrection）。

        active-runs.ts:1838-1895：correction prompt 限定 agent 只允许交付
        （report_surface_state(phase=final) + handoff 工具各一次），禁止重复
        调查/搜索/文件修改；最多 MAX_HANDOFF_CORRECTIONS 次；
        成功标准 = pending_handoffs 非空 且 surface 铁律满足（若节点声明了
        pinned view，phase 必须到 final）。耗尽后由调用方 fail-loud。

        correction 轮复用首轮 provision 的 subagent/container（同一 session_id
        续 codex thread 上下文），单轮独立超时（min(240s, 节点超时)）。
        """
        ports = ", ".join(node.outputs.keys())
        last_text = nstate.text_outputs[-1] if nstate.text_outputs else ""
        correction_timeout = min(240.0, float(self._get_node_timeout(node)))
        for attempt in range(1, self.MAX_HANDOFF_CORRECTIONS + 1):
            reason = (
                "surface_not_final"
                if self._required_surface_final_violation(node)
                else "ended_without_handoff"
            )
            logger.warning(
                "Node '%s' turn 交付不完整（reason=%s），启动 correction 第 %d/%d 轮",
                node.id, reason, attempt, self.MAX_HANDOFF_CORRECTIONS,
            )
            await self._emit(DagEventType.NODE_PROGRESS, node.id, {
                "correction_attempt": attempt,
                "max_corrections": self.MAX_HANDOFF_CORRECTIONS,
                "reason": reason,
                "hint": "correction: deliver via report_surface_state(final) + handoff only",
            })
            correction_prompt = (
                f"CORRECTION（第 {attempt}/{self.MAX_HANDOFF_CORRECTIONS} 次纠正）: "
                "你上一轮的交付不完整（未发布 phase='final' 的 surface 卡片和/或未调用 "
                "handoff 工具），DAG 无法接收你的交付。\n"
                "纠正模式只允许交付结果，禁止重复调查、搜索或修改文件。\n"
                "请基于已完成的工作立即完成交付：\n"
                "1) 若 report_surface_state 工具可用，先以 phase='final' 发布最终卡片"
                "（把已得出的关键结论/数据填入 data_model 字段）；\n"
                f"2) 然后调用 handoff 工具恰好一次：port 从已声明输出端口中选（{ports}），"
                "content 填入完整交付物（结构化成果正文，不是过程叙述），summary 一句话总结。\n"
                "不要以纯文本结尾，本轮必须以 handoff 工具调用结束。\n"
                f"上一轮最后输出（参考，不要重复调查）：{last_text[:400]}"
                + self._surface_final_repair_hint(node)
            )
            try:
                await self._execute_node(
                    node, nstate, global_inputs,
                    override_resolved_model=override_resolved_model,
                    prompt_override=correction_prompt,
                    turn_timeout=correction_timeout,
                )
            except Exception as e:
                logger.warning(
                    "Node '%s' correction 第 %d 轮异常（继续下一轮或由调用方判失败）: %s",
                    node.id, attempt, e,
                )
            if (
                nstate.pending_handoffs
                and self._required_surface_final_violation(node) is None
            ):
                logger.info(
                    "Node '%s' correction 第 %d 轮交付成功（ports=%s）",
                    node.id, attempt, list(nstate.pending_handoffs.keys()),
                )
                return

    def _create_harness(self, harness_ref: HarnessTypeRef) -> AgentClient:
        """H1: 按 YAML 声明的 harness 类型真正创建对应实现，永远走 Registry。

        修复历史问题：此前 OPENCODE/CLAUDE_CODE 被偷换为 LocalLlmClient，
        导致 opencode/claude code 的 agent loop（工具执行、子 agent）被绕过。
        现在所有 harness 统一从 HarnessRegistry.create() 创建。
        LocalLlmClient 已提升为一等公民（harness: local_llm）。
        """
        # HarnessTypeRef → HarnessType（值相同，直接转换）
        harness_type = HarnessType(harness_ref.value)
        return HarnessRegistry.create(harness_type)

    def _resolve_model(self, node: WorkflowNode) -> dict[str, str] | None:
        """H2: 按优先级链解析节点应使用的模型配置。返回 None = harness 自理。"""
        return self._resolve_model_for_node(node)

    def _resolve_model_for_node(self, node: WorkflowNode) -> dict[str, str] | None:
        from orchestrator.model_config import get_model_config

        if node.harness == HarnessTypeRef.DETERMINISTIC:
            return None

        model_config = get_model_config()
        return model_config.resolve(
            node_model=node.model,
            domain=node.domain,
            llm_config=self.llm_config,
        )

    @staticmethod
    def _classify_error_type(error: str) -> str:
        """粗分类 provider 错误，供前端展示。"""
        lower = error.lower()
        if "401" in lower or "unauthorized" in lower or "invalid api key" in lower:
            return "auth_error"
        if "429" in lower or "rate limit" in lower or "quota" in lower:
            return "rate_limit"
        if "timeout" in lower or "timed out" in lower:
            return "timeout"
        if "protocol" in lower or "不兼容" in lower:
            return "protocol_mismatch"
        if "not found" in lower or "404" in lower:
            return "not_found"
        return "unknown"

    async def _record_node_usage(self, node_id: str, nstate: NodeExecutionState) -> None:
        """节点完成后写入 usage_records（v3: FK to runs）。"""
        if not self._event_store or not nstate.resolved_model:
            return
        from orchestrator.model_config import get_model_config

        resolved = nstate.resolved_model
        provider = resolved.get("provider", "")
        model = resolved.get("model", "")
        in_price, out_price = get_model_config().get_price(provider, model)
        cost = (nstate.tokens_in / 1000 * in_price) + (nstate.tokens_out / 1000 * out_price)
        self.run_state.total_cost_usd += cost

        # v3: subagent_id 当前未在 engine 显式建模（节点即 subagent 容器），
        # 后续 v4 引入 SubagentStore.provision_subagent 后填入
        await self._event_store.record_usage(
            run_id=self.run_state.run_id,
            node_id=node_id,
            provider_id=provider,
            model=model,
            input_tokens=nstate.tokens_in,
            output_tokens=nstate.tokens_out,
            duration_ms=nstate.duration_ms,
            cost_usd=cost,
        )

    def _build_prompt(
        self,
        node: WorkflowNode,
        nstate: NodeExecutionState,
        global_inputs: dict[str, Any],
    ) -> str:
        """Compose the user prompt for this node."""
        lines = [f"# Task: {node.name} (node={node.id})"]
        if node.inputs:
            lines.append("\n## Inputs")
            for name in node.inputs:
                v = global_inputs.get(name, nstate.current_inputs.get(name))
                lines.append(f"- {name}: {v}")
        if nstate.upstream_outputs:
            lines.append("\n## Upstream outputs")
            for src, ports in nstate.upstream_outputs.items():
                lines.append(f"From '{src}':")
                for port, payload in ports.items():
                    lines.append(f"  - {port}: {payload}")

        # 按 harness 类型分流输出指令（codex 同 opencode——都通过 write_file/bash 写文件 + 引擎收割）
        if node.harness in (HarnessTypeRef.OPENCODE, HarnessTypeRef.CODEX):
            lines.append("\n## Output")
            lines.append(
                "你的最终交付物是写到指定路径的文件。写完后在回复中列出你创建的文件路径。"
                "引擎会自动读取这些文件并送到下游节点。"
            )
            # 追加工具 CLI 调用提示
            agent_def = self._get_agent_def(node.agent)
            if agent_def and agent_def.allowed_tools:
                lines.append("\n## 可用工具（通过 Bash 调用）")
                tool_hints = {
                    "query_knowledge": "python config/tools/query_knowledge.py --category <category> [--section <section>] [--scene-type <type>]",
                    "mm_search": "mmx search \"<query>\"",
                    "mm_speech": "mmx speech synthesize --model speech-2.8-hd --text \"<text>\" --output <path>",
                    "mm_image": "mmx image generate --prompt \"<prompt>\" --output <path>",
                    "validate_duration": "python config/knowledge/video-production/validate_duration.py --project-dir <dir> --target <seconds>",
                    "log_query": "python -c \"import asyncio,json; from tools.ops_tools import query_logs; r=asyncio.run(query_logs({'log_dir':'<dir>','level':'ERROR','time_range':'24h','lines':500})); print(json.dumps(r,ensure_ascii=False,indent=2))\"",
                    "wecom_notify": "python tools/wecom_notify.py --content \"<消息内容>\" [--msg-type markdown]",
                    "read_file": "使用 Read 工具读取文件",
                    "write_file": "使用 Write 工具写入文件",
                }
                for tool_id in agent_def.allowed_tools:
                    hint = tool_hints.get(tool_id)
                    if hint:
                        lines.append(f"- `{tool_id}`: {hint}")
        else:
            lines.append("\n## Output")
            lines.append("Use the 'handoff' tool to send your result on the appropriate port.")
        return "\n".join(lines)

    async def _emit_widgets_for_node(self, node_id: str, event: str = "node.completed") -> None:
        """Emit widget.update events for all widgets declared in workflow that
        trigger on this node (per workflow.widgets[*].emit_on).

        按 emit_on_event 过滤（node.started / node.completed / node.handoff / node.failed）。
        progress_status 类型特殊处理：每次节点状态变化时都 emit 一次（实时更新所有 step 状态），
        不论 emit_on_node 配置是哪个节点（覆盖式渲染——前端按 widget_id 替换）。
        Props 模板渲染：支持 {{workspace.root}} / {{run_id}} / {{node_id.port}} 跨节点 port 引用。
        """
        nstate = self.node_states[node_id]
        widgets = [
            w for w in self.workflow.widgets
            if w.emit_on_node == node_id and w.emit_on_event == event
        ]
        # progress_status 特殊：每次节点完成都 emit（实时刷新所有 step 状态）
        if event == "node.completed":
            progress_widgets = [
                w for w in self.workflow.widgets
                if w.type == "progress_status" and w not in widgets
            ]
            widgets.extend(progress_widgets)
        # 没匹配到任何 widget → 不发事件
        if not widgets:
            return

        for w in widgets:
            # 渲染 props 模板（支持 {{workspace.root}} / {{run_id}} / {{node.port}} 跨节点引用）
            props = self._resolve_template_deep(dict(w.props or {}))
            text_join = "\n".join(nstate.text_outputs)[:500]

            if w.type == "memo":
                base = props.get("text") or props.get("content") or ""
                props["text"] = (base + ("\n" if base else "") + text_join) if text_join else base
            elif w.type == "task_draft":
                tasks = []
                for port, payload in (nstate.pending_handoffs or {}).items():
                    txt = (payload.get("content", "") if isinstance(payload, dict) else str(payload))[:120]
                    tasks.append({"id": port, "text": f"{port}: {txt}", "status": "completed", "checked": True})
                if tasks:
                    props["tasks"] = tasks
            elif w.type == "progress_status":
                # 步骤状态根据 node_states 动态计算（不再只显示当前节点的快照）
                steps_arr = props.get("steps") or [
                    n.id for n in self.workflow.nodes.values() if n.type.value == "agent"
                ]
                # 标准化 steps：list[str] 转 list[dict{id,title,node,status}]
                normalized: list[dict[str, str]] = []
                for s in steps_arr:
                    if isinstance(s, str):
                        n = self.workflow.nodes.get(s)
                        normalized.append({
                            "id": s,
                            "title": n.name if n else s,
                            "node": s,
                            "status": self._map_node_status(s),
                        })
                    elif isinstance(s, dict):
                        nid = s.get("node") or s.get("id")
                        normalized.append({
                            "id": nid,
                            "title": s.get("title", nid),
                            "node": nid,
                            "status": self._map_node_status(nid),
                        })
                props["steps"] = normalized
                try:
                    props["currentStep"] = next(
                        (i for i, s in enumerate(normalized) if s["status"] == "active"),
                        len([s for s in normalized if s["status"] == "done"]),
                    )
                except Exception:
                    props["currentStep"] = 0
            elif w.type == "checklist":
                items = []
                for port, payload in (nstate.pending_handoffs or {}).items():
                    items.append({"id": port, "text": f"{port}: completed" if payload else f"{port}: pending", "checked": bool(payload)})
                if items:
                    props["items"] = items
            elif w.type == "artifact_ref":
                # 优先用 props.path（已渲染模板）回填到下游可读字段
                if props.get("path") and "files" not in props:
                    p = props["path"]
                    props["files"] = [{
                        "name": p.rsplit("/", 1)[-1] if "/" in p else p,
                        "url": p,
                        "size": 0,
                    }]
                # 兼容老的 pending_handoffs 驱动 files 列表
                elif nstate.pending_handoffs and "files" not in props:
                    files = []
                    for port, payload in (nstate.pending_handoffs or {}).items():
                        files.append({"name": f"{port}.md", "url": f"#artifact-{port}", "size": len(str(payload))})
                    if files:
                        props["files"] = files
            elif w.type == "timeline":
                events_list = list(props.get("events", []) or [])
                events_list.append({
                    "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "title": f"Node {node_id} 完成",
                    "detail": f"duration={nstate.duration_ms}ms tokens_in={nstate.tokens_in}",
                })
                props["events"] = events_list

            await self._emit(DagEventType.WIDGET_UPDATE, node_id, {
                "widget_id": w.id,
                "type": w.type,
                "props": props,
            })

    def _map_node_status(self, node_id: str) -> str:
        """把 NodeStatus 映射到 progress_status widget 期望的 status 字符串。"""
        nstate = self.node_states.get(node_id)
        if not nstate:
            return "pending"
        s = nstate.status
        if s == NodeStatus.COMPLETED:
            return "done"
        if s == NodeStatus.RUNNING or s == NodeStatus.READY:
            return "active"
        if s == NodeStatus.FAILED:
            return "failed"
        if s == NodeStatus.SKIPPED:
            return "skipped"
        return "pending"

    def _make_local_llm_builtin_tools(self, nstate: "NodeExecutionState") -> list[ToolDefinition]:
        """为 local_llm harness 注入 BUILTIN_TOOLS 的 Python 实现（write_file/read_file/bash）。

        opencode harness 由 opencode server 自身实现这些工具；local_llm 没有 server，
        所以在这里给 LLM 提供可调用的 Python 版。LLM 通过 function calling 调用，handler
        直接执行本地 IO/subprocess。
        """
        from pathlib import Path
        import shlex

        ws_root = self._workspace_root()

        async def write_file_handler(args: dict[str, Any]) -> dict[str, Any]:
            path = args.get("path", "")
            content = args.get("content", "")
            if not path:
                return {"content": "ERROR: 'path' is required", "ok": False}
            try:
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                return {"content": f"file written: {path} ({len(content)} bytes)", "ok": True, "path": path}
            except Exception as e:
                return {"content": f"ERROR: write_file failed: {e}", "ok": False}

        async def read_file_handler(args: dict[str, Any]) -> dict[str, Any]:
            path = args.get("path", "")
            if not path:
                return {"content": "ERROR: 'path' is required"}
            try:
                p = Path(path)
                if not p.exists():
                    return {"content": f"ERROR: file not found: {path}"}
                content = p.read_text(encoding="utf-8", errors="ignore")
                return {"content": content, "path": path, "size": len(content)}
            except Exception as e:
                return {"content": f"ERROR: read_file failed: {e}"}

        async def bash_handler(args: dict[str, Any]) -> dict[str, Any]:
            cmd = args.get("command", "") or args.get("cmd", "")
            if not cmd:
                return {"content": "ERROR: 'command' is required"}
            try:
                import subprocess
                import sys as _sys
                # Windows 兼容：用 shell=True 让系统查找可执行文件
                # （npx / node / python 等在 Windows 上是 .cmd 脚本，shlex.split 会破坏路径）
                r = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True, text=True,
                    timeout=120, encoding="utf-8", errors="ignore",
                    cwd=ws_root,
                )
                return {
                    "content": (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else ""),
                    "returncode": r.returncode,
                }
            except subprocess.TimeoutExpired:
                return {"content": "ERROR: command timeout (120s)", "returncode": -1}
            except Exception as e:
                return {"content": f"ERROR: bash failed: {e}", "returncode": -1}

        return [
            ToolDefinition(
                name="write_file",
                description="Write content to a file at the given path. Auto-creates parent dirs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path"},
                        "content": {"type": "string", "description": "File content to write"},
                    },
                    "required": ["path", "content"],
                },
                handler=write_file_handler,
            ),
            ToolDefinition(
                name="read_file",
                description="Read content of a text file.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=read_file_handler,
            ),
            ToolDefinition(
                name="bash",
                description="Run a shell command (timeout 60s).",
                input_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                handler=bash_handler,
            ),
        ]

    def _build_system_prompt(
        self, node: WorkflowNode, nstate: NodeExecutionState,
        global_inputs: dict[str, Any] | None = None,
    ) -> str:
        """从 config/agents/{agent_id}.yaml 加载 system_prompt，解析模板变量。

        会自动注入 {{node_id}} / {{node_name}} 让多节点共享同一 agent 时能路由到对应步骤。

        v2.1 三层模型：若节点配了 role_prompt，前置注入「## 你的角色」段。
        - role_prompt 是角色层（可变，来自 workflow yaml）
        - agent.system_prompt 是能力层（固定，来自 agent yaml）
        拼装顺序：role_prompt → base_prompt（与 SystemPromptBuilder 一致）

        P0.5: 若节点配了 inline_agent，直接用其 role_prompt 作为 base，
              不查全局 agent。role_section 由 inline_agent.role_prompt 自身承担。
        """
        # P0.5 优先：inline_agent 自包含，直接用其 role_prompt 作为完整 prompt
        if node.inline_agent:
            inline_rp = self._resolve_template(
                node.inline_agent.role_prompt, global_inputs or {},
                node_id=node.id, node_name=node.name,
            )
            return inline_rp

        # v2.1 角色层：节点级 role_prompt（可变，来自 workflow yaml）
        role_section = ""
        if node.role_prompt:
            role_section = f"## 你的角色\n{node.role_prompt}\n\n"

        agent_def = self._get_agent_def(node.agent)
        if agent_def and agent_def.system_prompt:
            base = self._resolve_template(
                agent_def.system_prompt, global_inputs or {},
                node_id=node.id, node_name=node.name,
            )
            return f"{role_section}{base}"
        # 回退：agent 不存在或没定义 system_prompt
        logger.warning(f"Agent '{node.agent}' not found or no system_prompt, using default")
        default_prompt = (
            f"You are node '{node.id}' in workflow '{self.workflow.workflow_id}'. "
            f"Your job: {node.name}."
        )
        return f"{role_section}{default_prompt}"

    def _get_agent_def(self, agent_id: str | None):
        """从 ConfigLoader 获取 AgentDefinition。"""
        if not agent_id:
            return None
        try:
            from orchestrator.config_loader import get_system_config
            config = get_system_config()
            return config.agents.get(agent_id)
        except Exception as e:
            logger.error(f"Config load failed for agent '{agent_id}': {e}")
            raise

    def _workspace_root(self) -> str:
        """统一的 workspace 根路径（已解析 run_id）。

        P0.18.5: 优先使用 workspace_context.workspace_root（用户授权工作区 / sandbox），
        回退到 ${AGENTOPS_HOME}/workspaces/{wf_id}/{run_id}/（通用对话 / 测试用，绝对路径，
        不再返回相对路径避免产物散落到进程 cwd / 项目代码目录）。
        """
        if self._workspace_context and self._workspace_context.get("workspace_root"):
            return self._workspace_context["workspace_root"]
        home = os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops"))
        return os.path.abspath(
            os.path.join(home, "workspaces", self.workflow.workflow_id, self.run_state.run_id)
        )

    def _get_node_timeout(self, node: WorkflowNode) -> float:
        """从节点或 agent yaml 读 timeout_seconds。

        优先级：workflow yaml 的 node.timeout_seconds > agent yaml 的 timeout_seconds > 默认 600s。
        修复历史问题：原实现只读 agent yaml，忽略 workflow yaml 的 node.timeout_seconds，
        导致 scan 节点配置 120s 却跑 600s（agent yaml 默认值）才超时。
        """
        # 1. 优先读 workflow yaml 的 node 级配置（per-node 精细化控制）
        node_timeout = getattr(node, "config", {}).get("timeout_seconds")
        if node_timeout:
            return float(node_timeout)
        # 2. 回退到 agent yaml 的 agent 级配置
        agent_def = self._get_agent_def(node.agent)
        if agent_def and agent_def.timeout_seconds:
            return float(agent_def.timeout_seconds)
        # 3. 默认 600s
        return 600.0

    def _resolve_template(self, text: str, global_inputs: dict[str, Any] | None = None,
                          node_id: str = "", node_name: str = "") -> str:
        """解析模板变量。

        内置变量（不可被 global_inputs 覆盖）：
          {{workspace.root}} / {{run_id}} / {{node_id}} / {{node_name}}
        全局 input 变量（如 {{target_duration}} / {{log_dir}}）由调用方传入。
        跨节点 port 引用：{{node_id.port_name}} → 上游节点对应 port 的 summary
        """
        import re
        ws_root = self._workspace_root()
        text = text.replace("{{workspace.root}}", ws_root)
        text = text.replace("{{run_id}}", self.run_state.run_id)
        # 节点级内置变量（多节点共享 agent 时通过此区分当前节点）
        if node_id:
            text = text.replace("{{node_id}}", node_id)
        if node_name:
            text = text.replace("{{node_name}}", node_name)
        # 解析全局 input 变量（如 {{target_duration}}）
        if global_inputs:
            for k, v in global_inputs.items():
                text = text.replace("{{" + k + "}}", str(v))
        # 跨节点 port 引用：{{report.report_path}} → 上游 report 节点 report_path port 的 summary
        def _replace_port_ref(m: "re.Match[str]") -> str:
            ref_node, ref_port = m.group(1), m.group(2)
            ref_state = self.node_states.get(ref_node)
            if not ref_state:
                return m.group(0)
            payload = ref_state.pending_handoffs.get(ref_port)
            if not payload:
                return m.group(0)
            if isinstance(payload, dict):
                return str(payload.get("summary") or payload.get("content") or "")
            return str(payload)
        text = re.sub(r"\{\{(\w+)\.(\w+)\}\}", _replace_port_ref, text)
        return text

    def _resolve_template_deep(self, value: Any, global_inputs: dict[str, Any] | None = None,
                                node_id: str = "", node_name: str = "") -> Any:
        """递归渲染 dict/list/str 里的所有模板变量。"""
        if isinstance(value, str):
            return self._resolve_template(value, global_inputs, node_id, node_name)
        if isinstance(value, dict):
            return {k: self._resolve_template_deep(v, global_inputs, node_id, node_name) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_template_deep(v, global_inputs, node_id, node_name) for v in value]
        return value

    def _harvest_file_outputs(
        self, node: WorkflowNode, nstate: NodeExecutionState
    ) -> None:
        """文件收割：opencode harness 完成后，按 agent 的 output_files 映射读取文件。

        - 文件 → 读内容填入 pending_handoffs[port]
        - 目录 → 填路径字符串
        - 文件不存在 → WARNING，跳过该 port
        """
        agent_def = self._get_agent_def(node.agent)
        if not agent_def or not agent_def.output_files:
            # 没声明 output_files → 直接返回，禁止 text_outputs 兜底。
            # run_20260818_073010_177175 事故根因：此兜底把 agent 对话文本
            # 伪装成交付物填满 pending_handoffs，correction 条件
            # (not pending_handoffs) 永不满足 → correction 从未触发 →
            # finalize 时 L0 校验失败节点假路径 FAILED。
            # 真实 agent turn 无 handoff → correction 轮，
            # 不是文本伪装。local_llm 的 text 兜底在 _execute_node 尾部
            # 单独处理（该 harness 无 function calling，语义不同）。
            return

        ws_root = self._workspace_root()
        for port, pattern in agent_def.output_files.items():
            if port not in node.outputs:
                continue  # agent 声明了但 workflow 没用的 port
            file_path = pattern.replace("{{workspace.root}}", ws_root)
            file_path = file_path.replace("{{run_id}}", self.run_state.run_id)
            path = Path(file_path)
            if path.is_dir() or file_path.endswith("/"):
                # 目录 → 传路径
                nstate.pending_handoffs[port] = {
                    "content": str(path),
                    "summary": f"directory: {path}",
                    "source_file": str(path),
                }
            elif path.exists():
                content = path.read_text(encoding="utf-8")
                nstate.pending_handoffs[port] = {
                    "content": content,
                    "summary": content[:200],
                    "source_file": str(path),
                }
            else:
                logger.warning(
                    f"Node {node.id} port '{port}': expected file {path} not found"
                )
