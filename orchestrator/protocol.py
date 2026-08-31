"""
Orchestrator Protocol — v2.1 abstract interface.

Three implementations:
  - OpencodeOrchestrator (default, M0 winner candidate A)
  - AgentOpsOrchestrator (M0 candidate B)
  - LocalSdkOrchestrator (always-on fallback, M0 candidate C)

The Orchestrator owns:
  - Run lifecycle (create, status, cancel)
  - Workflow parsing + dispatch
  - Event emission (DagEvent + RawHarnessEvent dual channel)
  - Checkpoint + resume
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol
from uuid import uuid4


# ====== Enums ======

class SessionStatus(str, Enum):
    """v3: Session 对话层状态机（active / dormant / archived）。

    取代 v2 RunStatus.ACTIVE / DORMANT。Session 寿命 ⊇ Run 寿命，Session 可在
    多次 Run 之间保持 dormant 状态被唤醒。
    """
    ACTIVE = "active"
    DORMANT = "dormant"
    ARCHIVED = "archived"


class RunStatus(str, Enum):
    """v3: Run（DAG 执行实例）状态机。

    移除 v2 的 ACTIVE / DORMANT / PAUSED（这些是 Session 概念）；
    新增 WAITING（等 widget.input / 外部触发）。
    """
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubagentStatus(str, Enum):
    """v3: Subagent（一次性执行体）状态机。

    一次性原则：subagent launch → 终结（completed/failed），全程不可恢复。
    """
    PROVISIONING = "provisioning"     # 资源准备中（worker 调度 / thread 创建）
    RUNNING = "running"               # 正在执行 agent loop
    HANDOFF = "handoff"               # 已产出 handoff payload，等下游接收
    CLEANUP = "cleanup"               # 清理中（terminate harness / 释放资源）
    COMPLETED = "completed"           # 正常结束
    FAILED = "failed"                 # 异常终止


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"             # all upstream completed
    WAITING = "waiting"         # 等 widget.input, 没到就不启动
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"         # gateway chose different path
    CANCELLED = "cancelled"


class RunMode(str, Enum):
    """执行谱系：conversational → task → hybrid → templated。"""
    CONVERSATIONAL = "conversational"   # 单 Agent + ReAct，无拓扑
    TASK = "task"                       # 多步 but 无依赖，线性 todo
    HYBRID = "hybrid"                   # DAG + 节点内对话
    TEMPLATED = "templated"             # YAML 预定义拓扑（现有）


# ====== Core Data Structures ======

@dataclass
class RunRequest:
    """Caller submits to Orchestrator.run()"""
    workflow_id: str | None = None         # templated/hybrid 必填，conversational/task 为 None
    inputs: dict[str, Any] = field(default_factory=dict)
    user_id: str = ""
    tenant_id: str = ""
    # Optional override: which Orchestrator implementation to use
    backend: str | None = None
    # Optional override: which Harness to use (per-node usually)
    harness_overrides: dict[str, str] = field(default_factory=dict)
    # RunMode 路由（P1 新增）
    run_mode: RunMode = RunMode.TEMPLATED
    # conversational/task 模式专用
    agent_id: str | None = None            # 指定执行 Agent
    initial_message: str = ""              # 用户首条消息
    system_prompt_override: str | None = None
    # P0.18.7: 指定 authorized workspace_id，触发 DagEngine 的 provisioner 路径
    # None = 通用对话（无绑定项目工作区），provisioner 路径不会被触发（走旧 docker_runtime 路径）
    workspace_id: str | None = None
    # v3 修复：engine 启动前先写 runs 表，避免并行节点 provision_subagent 时
    # FK (subagents.run_id → runs.run_id) 因 runs 记录尚未写入而失败。
    # None = 由调用方在 orchestrator.run() 之后自行 init_run（旧路径，存在竞态）。
    session_id: str | None = None


@dataclass
class RunHandle:
    """Return value from Orchestrator.run() — caller uses run_id to query / stream"""
    run_id: str
    workflow_id: str
    started_at: datetime
    cancel_token: str  # pass to Orchestrator.abort() to cancel


# ====== Event Schema (DagEvent — business channel) ======

class DagEventType(str, Enum):
    # Session 生命周期（Thread 模式）
    SESSION_CREATED = "session.created"
    SESSION_DORMANT = "session.dormant"       # idle 超时（替代 run.completed idle_timeout）
    SESSION_CLOSED = "session.closed"
    # Turn 生命周期（替代 node.started/completed，对话模式）
    TURN_STARTED = "turn.started"
    TURN_PROGRESS = "turn.progress"           # agent 实时输出（文本模式）
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    # 语音事件
    VOICE_STARTED = "voice.started"
    VOICE_SDP = "voice.sdp"                   # SDP answer
    TRANSCRIPT_DELTA = "transcript.delta"     # 实时转录增量
    TRANSCRIPT_DONE = "transcript.done"       # 完整转录
    VOICE_STOPPED = "voice.stopped"
    # 向后兼容：templated DAG 模式保留
    RUN_CREATED = "run.created"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    NODE_READY = "node.ready"
    NODE_STARTED = "node.started"
    NODE_PROGRESS = "node.progress"   # agent 实时输出（TEXT/THINKING/TOOL_USE）
    NODE_HANDOFF = "node.handoff"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    NODE_SKIPPED = "node.skipped"     # skip_if 条件命中
    WIDGET_UPDATE = "widget.update"
    WIDGET_INPUT = "widget.input"
    USAGE = "usage"            # token / cost real-time
    CONVERSATION_TOOL_USE = "conversation.tool_use"   # v74: agent 调用工具（对话区显示，类 Claude Code 时间线）
    # P2（deepseek-harness 对齐）：审批闭环事件对（审计即落库，前端经 SSE 弹窗）
    APPROVAL_REQUESTED = "approval.requested"         # approval/asked：向用户发起权限审批请求
    APPROVAL_DECIDED = "approval.decided"             # approval/decided：审批结果（闭集 outcome）
    CROSS_DOMAIN = "cross_domain"  # P7: 跨域协调事件
    # v99.5 P0.2: 生成式 UI 监督式面板 — agent 主动 emit surface snapshot
    REPORT_SURFACE_STATE = "report_surface_state"


@dataclass
class SurfaceState:
    """Worker / Agent 主动 emit 的生成式 UI surface snapshot。

    对应 A2UI v1.0 surface envelope（详见 docs/reconstruction/agentops-v99.5-a2ui-design.md §2.1）。

    复用维度：
    - L0 协议层：A2UI v1.0 catalog（30+ 组件）
    - L1 项目层：AgentOps Ao* 扩展 catalog（18 节点类型，统一 Ao 前缀）
    - L1.5 Worker Profile 层：config/actors/<actor_id>/actor_visual_profile.json
    - L2 Surface 实例层：本 dataclass 即 L2 实例

    校验链（tools/report_surface_state.py 实现）：
    1. view_id 在 actor allowed_surface_views 白名单
    2. data_model 符合 fields 类型约束
    3. components 是有效 A2UI 组件树
    4. output_contract 与 view_id 声明一致
    5. phase 单调推进（per-view 维度）
    6. surface_id digest pinning 防覆盖
    """
    surface_id: str                                # identity-derived: sha256(run_id + actor_id + view_id + generation)，Worker 注入（模型不可覆盖）
    view_id: str                                   # 在 actor_visual_profile.json 中声明的 view id
    phase: str                                     # "started" | "partial" | "final" | "superseded"
    components: list[dict]                         # A2UI 组件树（30+ 组件之一）
    data_model: dict                               # JSON Pointer 数据源
    catalog_id: str = "https://agentops.dev/a2ui/catalogs/core/v1"
    surface_properties: dict | None = None         # {iconUrl, agentDisplayName}
    output_contract: str | None = None             # 绑定的 contract 类别（ActorReport / Mission / Failure / RoundGate）
    source: str = "agent"                          # OPT-1: "agent"（LLM 主动 emit）| "system"（DAG 事件确定性投影）
    emitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    patch_sequence: int = 0                        # 同 surface 内单调递增的 patch 序号（0=未启用/legacy）

    def to_payload(self) -> dict[str, Any]:
        """序列化为 DagEvent.payload dict（落库 + SSE 转发用）。"""
        return {
            "surface_id": self.surface_id,
            "view_id": self.view_id,
            "phase": self.phase,
            "catalog_id": self.catalog_id,
            "components": self.components,
            "data_model": self.data_model,
            "surface_properties": self.surface_properties,
            "output_contract": self.output_contract,
            "source": self.source,
            "emitted_at": self.emitted_at.isoformat(),
            "patch_sequence": self.patch_sequence,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SurfaceState":
        """从 DagEvent.payload dict 反序列化（resume / replay 用）。"""
        emitted_at_raw = payload.get("emitted_at")
        if isinstance(emitted_at_raw, str):
            emitted_at = datetime.fromisoformat(emitted_at_raw)
        elif isinstance(emitted_at_raw, datetime):
            emitted_at = emitted_at_raw
        else:
            emitted_at = datetime.now(timezone.utc)
        return cls(
            surface_id=payload["surface_id"],
            view_id=payload["view_id"],
            phase=payload["phase"],
            components=payload.get("components", []),
            data_model=payload.get("data_model", {}),
            catalog_id=payload.get("catalog_id", "https://agentops.dev/a2ui/catalogs/core/v1"),
            surface_properties=payload.get("surface_properties"),
            output_contract=payload.get("output_contract"),
            source=payload.get("source", "agent"),
            emitted_at=emitted_at,
            patch_sequence=int(payload.get("patch_sequence", 0) or 0),
        )


@dataclass
class DagEvent:
    type: DagEventType
    run_id: str
    node_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    surface_state: SurfaceState | None = None      # v99.5: 当 type=REPORT_SURFACE_STATE 时填充
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sequence: int = 0         # monotonic per run, for ordering + resume

    # v3 语义说明：DagEvent.run_id 直接对应 runs.run_id（不再映射到 session_id）。
    # 落库由 EventStore.append_run_event 写入 run_events 表，
    # run_events.run_id 列 ↔ DagEvent.run_id 字段一一对应（无错位）。

    def to_payload_with_surface(self) -> dict[str, Any]:
        """v99.5: 当 surface_state 非空时，序列化为含 surface 字段的 payload。

        旧 path（DagEvent.payload 单独使用）保持兼容，新 emit 的 REPORT_SURFACE_STATE 事件
        用此方法序列化，保证 surface_state 与 payload 一致。
        """
        if self.surface_state is None:
            return self.payload
        merged = dict(self.payload)
        merged["surface"] = self.surface_state.to_payload()
        return merged


@dataclass
class RawHarnessEvent:
    """Dual channel: harness-native events passed through, not translated.

    Used for: debugging, vendor-level issue triage, replay fidelity.
    """
    harness: str                # "opencode" / "claude_code" / "codex" / ...
    event_type: str             # vendor's native type name
    raw_payload: dict[str, Any]
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: str = ""
    node_id: str | None = None


# ====== Orchestrator Protocol ======

class Orchestrator(ABC):
    """Abstract Orchestrator. See module docstring for 3 implementations."""

    @abstractmethod
    async def run(self, req: RunRequest) -> RunHandle: ...

    @abstractmethod
    def stream_events(self, run_id: str, since: int = 0) -> AsyncIterator[DagEvent | RawHarnessEvent]:
        """Subscribe to events of a run. since: resume from sequence number.

        Yields BOTH DagEvent (business) and RawHarnessEvent (raw) events.
        Caller can filter by isinstance().
        """
        ...

    @abstractmethod
    async def inject(self, run_id: str, node_id: str, instruction: str) -> None:
        """Inject human instruction into a running node (HIL)."""

    @abstractmethod
    async def abort(self, run_id: str, reason: str = "") -> None: ...

    @abstractmethod
    async def get_run(self, run_id: str) -> "RunState": ...

    @abstractmethod
    async def resume_node(self, run_id: str, node_id: str, instruction: str) -> None:
        """Fork from checkpoint + rerun this node + downstream."""


# ====== RunState (read-only state machine view) ======

@dataclass
class RunState:
    run_id: str
    workflow_id: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    node_states: dict[str, NodeStatus] = field(default_factory=dict)
    node_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # node_id -> {port: payload}
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_cost_usd: float = 0.0
    error: str | None = None
