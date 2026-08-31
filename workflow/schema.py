"""
Workflow Engine — v2.1.

Parses YAML DAG, validates, dispatches to Harness adapters.

Key state machine (initial, codex will refine):
  - DAG 加载 → 验证（拓扑/无环/ID 存在性）
  - 找入度=0 节点 → READY
  - READY 节点并行启动 → RUNNING
  - RUNNING 完成 → 输出 handoff → 找下游入度=0 节点 → READY
  - 全部 COMPLETED → run 完成
  - 任一 FAILED → run FAILED (后续 SKIPPED)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class NodeType(str, Enum):
    AGENT = "agent"
    PARALLEL_BRANCH = "parallel_branch"
    GATEWAY = "gateway"
    # P0.1 新增：3 类缺失的节点原语
    COMMAND = "command"           # CLI / 二进制确定性执行（如 git diff / ffprobe / pytest）
    AWAIT_COMMAND = "await_command"  # 保持 Actor 可达，多轮对话注入
    WHILE = "while"               # 反馈循环（带 max_iterations 防失控）


class GatewayKind(str, Enum):
    CONDITION = "condition"
    LOOP = "loop"


class HarnessTypeRef(str, Enum):
    OPENCODE = "opencode"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    KIMI = "kimi"
    HTTP = "http"
    DETERMINISTIC = "deterministic"
    LOCAL_LLM = "local_llm"             # H1: 纯 API 调用（一等公民）


class RuntimePlacementRef(str, Enum):
    IN_PROCESS = "in_process"
    DOCKER_CONTAINER = "docker_container"
    SUBPROCESS = "subprocess"


@dataclass
class OutputRoute:
    """Edge from one node to another via a port.

    支持单目标（str）和多目标（list[str]）两种格式：
      - 单目标：to="next_node_id" 或 "next_node_id.in:port_name"
      - 多目标：to=["a.in:p1", "b.in:p2"]  （一个端口广播到多个下游）
    向后兼容：parse() 返回首个目标，parse_all() 返回全部。
    """
    to: str | list[str]   # 单目标或 多目标

    def parse(self) -> tuple[str, str | None]:
        """Returns (target_node_id, target_port_or_None) — 首个目标。"""
        all_targets = self.parse_all()
        return all_targets[0] if all_targets else ("", None)

    def parse_all(self) -> list[tuple[str, str | None]]:
        """返回所有 (target_node_id, target_port) 列表。"""
        targets = self.to if isinstance(self.to, list) else [self.to]
        result: list[tuple[str, str | None]] = []
        for t in targets:
            if not isinstance(t, str) or not t:
                continue
            if ".in:" in t:
                node, port = t.split(".in:", 1)
                result.append((node, port))
            else:
                result.append((t, None))
        return result


@dataclass
class InlineAgentConfig:
    """Inline agent definition (P0.5 Node Agent 内联).

    节点自包含的 agent 定义，无需 config/agents/*.yaml 全局文件。
    与 `WorkflowNode.agent`（全局 agent ID）互斥——必须二选一。

    设计动机：避免为单个 workflow 维护一个仅被该 workflow 使用的全局 agent。
    每个节点自带 agent 定义。
    """
    role_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    harness: HarnessTypeRef = HarnessTypeRef.OPENCODE
    model: str | dict[str, Any] | None = None
    timeout_seconds: int | None = None
    domain: str | None = None


@dataclass
class CommandNodeConfig:
    """P0.1 `command` 节点配置：CLI / 二进制确定性执行。

    与 agent 节点的关键区别：不调 LLM，直接 asyncio.create_subprocess_shell。
    模板中的 `{input_name}` 占位符由 dag inputs 替换，禁止硬编码凭据。

    Example:
        cli_template: "ffprobe -v error -show_entries format=duration {audio_path}"
        parse_stdout: "int(float(stdout.strip()) * 1000)"  # 表达式，结果写入 outputs.success
    """
    cli_template: str
    timeout_seconds: int = 30
    success_exit_code: int = 0
    parse_stdout: str | None = None    # Python 表达式，输入 stdout 字符串
    parse_stderr: str | None = None    # Python 表达式，输入 stderr 字符串（失败时）
    allowed_env_keys: list[str] = field(default_factory=list)
    cwd: str | None = None              # 工作目录（None 则用 workspace root）


@dataclass
class AwaitCommandNodeConfig:
    """P0.1 `await_command` 节点配置：保持 Actor 可达 + 多轮命令注入。

    整工作流 ≤ 1 个 await_command（避免协议冲突）。
    主循环监听 chat_input_queue，收到 command 后喂回 actor 触发新一轮 handoff，
    直到 max_commands 或 expiry_seconds。

    Example:
        target_actors: [research, synthesis, visual_story]
        command_port: "command"          # 注入用的 output port 名
        expiry_seconds: 86400            # 24h 不发命令则自动 timeout
        max_commands: 10
    """
    target_actors: list[str] = field(default_factory=list)
    command_port: str = "command"
    expiry_seconds: int = 86400        # 24h
    max_commands: int = 10


@dataclass
class WhileNodeConfig:
    """P0.1 `while` 节点配置：带 max_iterations 的反馈循环。

    强制要求 feedback edge 设 max_traversals，否则 validator 拒绝（UNBOUNDED_CYCLE）。

    Example:
        continue_if: "{{loop_source.passed == false}}"
        max_iterations: 3
        backoff_seconds: [0, 2, 5]      # 第 N 次循环前等待秒数
    """
    continue_if: str
    max_iterations: int = 3
    backoff_seconds: list[int] = field(default_factory=list)
    feedback_edge_max_traversals: int = 3  # 反馈边 max_traversals 硬上限


@dataclass
class WorkflowNode:
    id: str
    name: str
    type: NodeType
    agent: str | None = None           # agent ID (when type=agent)
    inline_agent: InlineAgentConfig | None = None  # P0.5: 节点内联 agent，与 agent 互斥
    harness: HarnessTypeRef = HarnessTypeRef.OPENCODE
    after: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)    # input names
    outputs: dict[str, OutputRoute] = field(default_factory=dict)
    branches: list[str] = field(default_factory=list)   # for parallel_branch
    # parallel_branch 配置 (来自 codex 补)
    join_strategy: str = "all"       # all | first | any
    cancel_on_first_fail: bool = True
    gateway_kind: GatewayKind | None = None             # for gateway
    condition: str | None = None                        # for condition gateway
    skip_if: str | None = None                          # 表达式如 "{{not validate.passed}}"，为 true 则跳过
    config: dict[str, Any] = field(default_factory=dict)
    # H2: 模型配置（per-node 覆盖）。值: "auto" / {provider, id} / None
    model: str | dict[str, Any] | None = None
    # P5: 业务域（用于域级默认模型 + 权限隔离）
    domain: str | None = None
    # 业务角色名（node 级覆盖 agent 级默认值，用于协作可视化泳道分组）
    business_role: str | None = None
    # v2.1 三层模型：节点级角色提示（角色层，可变）
    # 来自 workflow yaml，注入到 system_prompt 开头
    # 与 agent.base_prompt 分离——base_prompt 跨 workflow 固定（能力层），role_prompt 跨 workflow 可变
    # 若配了 inline_agent，则此字段被忽略（inline_agent.role_prompt 优先）
    role_prompt: str | None = None
    # runtime placement 指定该节点的执行载体偏好：
    # - in_process: 在 orchestrator 进程内直接执行（轻量、无隔离）
    # - docker_container: 在 Docker 容器中运行（需 provision subagent）
    # - subprocess: 使用 subprocess/create_subprocess_exec 启动本地进程
    runtime_placement: RuntimePlacementRef | None = None
    # P0.1: 3 类新原语配置子结构（仅对应 type 生效；其他 type 应为 None）
    command_config: CommandNodeConfig | None = None
    await_command_config: AwaitCommandNodeConfig | None = None
    while_config: WhileNodeConfig | None = None


@dataclass
class WidgetDeclaration:
    """YAML widget emit spec."""
    id: str
    type: str                # memo / task_draft / progress_status / checklist / artifact_ref / timeline
    title: str
    emit_on_node: str
    emit_on_event: str       # node.started / node.completed / node.handoff
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class WidgetInputBinding:
    """YAML widget input → DAG node input mapping."""
    from_widget: str
    to_node: str
    to_input: str
    required: bool = False
    trigger: str = "human_review"   # always human
    # codex 补的并发控制字段
    abortable: bool = False         # 节点已 running 时, input 到达是否 abort 后重跑
    timeout_seconds: int = 3600     # 等 input 多久后节点 failed (避免永久 waiting)


@dataclass
class WorkflowDefinition:
    workflow_id: str
    name: str
    version: float = 1.0
    description: str = ""
    source_policy: str | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    permissions: dict[str, list[str]] = field(default_factory=dict)   # allowed_tools, denied_tools
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)
    widgets: list[WidgetDeclaration] = field(default_factory=list)
    widget_inputs: list[WidgetInputBinding] = field(default_factory=list)
    schema_version: str = "2.1"
