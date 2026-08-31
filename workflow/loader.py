"""
YAML workflow loader — parse v2.1 YAML into WorkflowDefinition.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import (
    AwaitCommandNodeConfig,
    CommandNodeConfig,
    GatewayKind,
    HarnessTypeRef,
    InlineAgentConfig,
    NodeType,
    OutputRoute,
    WhileNodeConfig,
    WidgetDeclaration,
    WidgetInputBinding,
    WorkflowDefinition,
    WorkflowNode,
)


class WorkflowLoadError(Exception):
    pass


def load_workflow_yaml(path: str | Path) -> WorkflowDefinition:
    """Load a workflow from a YAML file."""
    p = Path(path)
    if not p.exists():
        raise WorkflowLoadError(f"Workflow file not found: {p}")
    text = p.read_text(encoding="utf-8")
    return load_workflow_text(text, source_path=str(p))


def load_workflow_text(text: str, source_path: str = "<inline>") -> WorkflowDefinition:
    """Load a workflow from a YAML string."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkflowLoadError(f"Invalid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise WorkflowLoadError(f"Workflow root must be an object, got {type(raw).__name__}")

    return _parse_workflow(raw, source_path)


def _parse_workflow(raw: dict[str, Any], source_path: str) -> WorkflowDefinition:
    workflow_id = raw.get("workflow_id") or raw.get("id")
    if not workflow_id:
        raise WorkflowLoadError(f"Workflow must have 'workflow_id' (source: {source_path})")

    name = raw.get("name", workflow_id)
    version = float(raw.get("version", 1.0))
    description = raw.get("description", "") or ""
    source_policy = raw.get("source_policy")
    schema_version = str(raw.get("schema_version", "2.1"))

    inputs = raw.get("inputs", []) or []
    permissions = raw.get("permissions", {}) or {}

    # Parse nodes
    nodes_raw = raw.get("nodes", {}) or {}
    if not nodes_raw:
        raise WorkflowLoadError(f"Workflow '{workflow_id}' has no nodes")

    nodes: dict[str, WorkflowNode] = {}
    for nid, nraw in nodes_raw.items():
        if not isinstance(nraw, dict):
            raise WorkflowLoadError(f"Node '{nid}' must be an object")
        nodes[nid] = _parse_node(nid, nraw, workflow_id)

    # Parse widgets
    widgets: list[WidgetDeclaration] = []
    for w in raw.get("widgets", []) or []:
        widgets.append(_parse_widget(w, workflow_id))

    widget_inputs: list[WidgetInputBinding] = []
    for wi in raw.get("widget_inputs", []) or []:
        widget_inputs.append(_parse_widget_input(wi, workflow_id))

    return WorkflowDefinition(
        workflow_id=workflow_id,
        name=name,
        version=version,
        description=description,
        source_policy=source_policy,
        inputs=inputs,
        permissions=permissions,
        nodes=nodes,
        widgets=widgets,
        widget_inputs=widget_inputs,
        schema_version=schema_version,
    )


def _parse_node(node_id: str, raw: dict[str, Any], workflow_id: str) -> WorkflowNode:
    node_type_str = raw.get("type", "agent")
    try:
        node_type = NodeType(node_type_str)
    except ValueError:
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}' has invalid type '{node_type_str}'"
        )

    harness_str = raw.get("harness", "opencode")
    try:
        harness = HarnessTypeRef(harness_str)
    except ValueError:
        raise WorkflowLoadError(
            f"Node '{node_id}' has invalid harness '{harness_str}'. "
            f"Valid: {[h.value for h in HarnessTypeRef]}"
        )

    after = raw.get("after", []) or []
    inputs = raw.get("inputs", []) or []

    outputs: dict[str, OutputRoute] = {}
    for port, route in (raw.get("outputs", {}) or {}).items():
        if isinstance(route, dict):
            to = route.get("to", "")
        elif route is None:
            to = ""  # P0.1: 显式 None 表示 terminal port（保留 outputs[port]）
        else:
            to = str(route)
        # P0.1: 保留空 to 作为 terminal port（如 command.success）
        # 向后兼容：旧 yaml 没有 to 字段时跳过（视为无 port 声明）
        if "to" not in (route if isinstance(route, dict) else {"to": ""}) and route is not None:
            continue
        outputs[port] = OutputRoute(to=to)

    branches = raw.get("branches", []) or []

    gateway_kind = None
    if "gateway_kind" in raw:
        try:
            gateway_kind = GatewayKind(raw["gateway_kind"])
        except ValueError:
            raise WorkflowLoadError(
                f"Node '{node_id}' has invalid gateway_kind '{raw['gateway_kind']}'"
            )

    # 把 node 级 timeout_seconds 塞进 config（_get_node_timeout 优先读它）
    config = raw.get("config", {}) or {}
    if "timeout_seconds" in raw:
        config["timeout_seconds"] = raw["timeout_seconds"]

    # P0.5: 解析 inline_agent 子结构（与 agent 全局引用互斥）
    inline_agent = _parse_inline_agent(node_id, raw.get("inline_agent"), workflow_id)

    # P0.1: 解析 3 类新原语的 config 子结构（按 type 分别处理）
    command_config = _parse_command_config(node_id, node_type, raw.get("command_config"), workflow_id)
    await_command_config = _parse_await_command_config(node_id, node_type, raw.get("await_command_config"), workflow_id)
    while_config = _parse_while_config(node_id, node_type, raw.get("while_config"), workflow_id)

    return WorkflowNode(
        id=node_id,
        name=raw.get("name", node_id),
        type=node_type,
        agent=raw.get("agent"),
        inline_agent=inline_agent,      # P0.5: 节点内联 agent
        harness=harness,
        after=after,
        inputs=inputs,
        outputs=outputs,
        branches=branches,
        gateway_kind=gateway_kind,
        condition=raw.get("condition"),
        skip_if=raw.get("skip_if"),
        config=config,
        model=raw.get("model"),          # H2: per-node 模型覆盖
        domain=raw.get("domain"),        # P5: 业务域
        business_role=raw.get("business_role"),  # 协作可视化：node 级业务角色
        role_prompt=raw.get("role_prompt"),       # v2.1 三层模型：节点级角色提示
        command_config=command_config,             # P0.1: command 原语
        await_command_config=await_command_config, # P0.1: await_command 原语
        while_config=while_config,                 # P0.1: while 原语
            runtime_placement=_parse_runtime_placement(node_id, raw.get("runtime_placement"), workflow_id),
    )


def _parse_inline_agent(
    node_id: str, raw: Any, workflow_id: str
) -> InlineAgentConfig | None:
    """解析 inline_agent 子结构（P0.5 节点内联 agent）。

    inline_agent 必填 role_prompt（即使 agent.system_prompt 为空）。
    harness 默认 opencode（向后兼容）。
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}': "
            f"inline_agent must be an object, got {type(raw).__name__}"
        )

    role_prompt = raw.get("role_prompt")
    if not role_prompt or not isinstance(role_prompt, str):
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}': "
            f"inline_agent 必须有 role_prompt (string)"
        )

    harness_str = raw.get("harness", "opencode")
    try:
        harness = HarnessTypeRef(harness_str)
    except ValueError:
        raise WorkflowLoadError(
            f"Node '{node_id}' inline_agent.harness='{harness_str}' 非法，"
            f"有效值: {[h.value for h in HarnessTypeRef]}"
        )

    return InlineAgentConfig(
        role_prompt=role_prompt,
        allowed_tools=raw.get("allowed_tools", []) or [],
        denied_tools=raw.get("denied_tools", []) or [],
        harness=harness,
        model=raw.get("model"),
        timeout_seconds=raw.get("timeout_seconds"),
        domain=raw.get("domain"),
    )


def _parse_command_config(
    node_id: str, node_type: NodeType, raw: Any, workflow_id: str
) -> CommandNodeConfig | None:
    """P0.1: 解析 `command` 原语 config（仅 type=command 节点生效）。

    cli_template 必填。timeout_seconds 默认 30s。parse_stdout 是 Python 表达式，
    输入 stdout 字符串，结果写入 outputs.success port。
    """
    if node_type != NodeType.COMMAND:
        if raw is not None:
            raise WorkflowLoadError(
                f"Node '{node_id}' in workflow '{workflow_id}': "
                f"type={node_type.value} 不应有 command_config（仅 type=command 可配）"
            )
        return None
    if not raw:
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}': "
            f"type=command 节点必须有 command_config"
        )
    if not isinstance(raw, dict):
        raise WorkflowLoadError(
            f"Node '{node_id}' command_config 必须是 object, got {type(raw).__name__}"
        )

    cli_template = raw.get("cli_template")
    if not cli_template or not isinstance(cli_template, str):
        raise WorkflowLoadError(
            f"Node '{node_id}' command_config 必须有 cli_template (string)"
        )

    return CommandNodeConfig(
        cli_template=cli_template,
        timeout_seconds=int(raw.get("timeout_seconds", 30)),
        success_exit_code=int(raw.get("success_exit_code", 0)),
        parse_stdout=raw.get("parse_stdout"),
        parse_stderr=raw.get("parse_stderr"),
        allowed_env_keys=raw.get("allowed_env_keys", []) or [],
        cwd=raw.get("cwd"),
    )


def _parse_await_command_config(
    node_id: str, node_type: NodeType, raw: Any, workflow_id: str
) -> AwaitCommandNodeConfig | None:
    """P0.1: 解析 `await_command` 原语 config（仅 type=await_command 节点生效）。"""
    if node_type != NodeType.AWAIT_COMMAND:
        if raw is not None:
            raise WorkflowLoadError(
                f"Node '{node_id}' in workflow '{workflow_id}': "
                f"type={node_type.value} 不应有 await_command_config"
            )
        return None
    if not raw:
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}': "
            f"type=await_command 节点必须有 await_command_config"
        )
    if not isinstance(raw, dict):
        raise WorkflowLoadError(
            f"Node '{node_id}' await_command_config 必须是 object, got {type(raw).__name__}"
        )

    return AwaitCommandNodeConfig(
        target_actors=raw.get("target_actors", []) or [],
        command_port=raw.get("command_port", "command"),
        expiry_seconds=int(raw.get("expiry_seconds", 86400)),
        max_commands=int(raw.get("max_commands", 10)),
    )


def _parse_while_config(
    node_id: str, node_type: NodeType, raw: Any, workflow_id: str
) -> WhileNodeConfig | None:
    """P0.1: 解析 `while` 原语 config（仅 type=while 节点生效）。

    continue_if 必填（Python 表达式，输入 inputs 字段），max_iterations 默认 3。
    反馈边 max_traversals 必填（否则 validator 报 UNBOUNDED_CYCLE）。
    """
    if node_type != NodeType.WHILE:
        if raw is not None:
            raise WorkflowLoadError(
                f"Node '{node_id}' in workflow '{workflow_id}': "
                f"type={node_type.value} 不应有 while_config"
            )
        return None
    if not raw:
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}': "
            f"type=while 节点必须有 while_config"
        )
    if not isinstance(raw, dict):
        raise WorkflowLoadError(
            f"Node '{node_id}' while_config 必须是 object, got {type(raw).__name__}"
        )

    continue_if = raw.get("continue_if")
    if not continue_if or not isinstance(continue_if, str):
        raise WorkflowLoadError(
            f"Node '{node_id}' while_config 必须有 continue_if (string 表达式)"
        )

    max_iterations = int(raw.get("max_iterations", 3))
    feedback_max = int(raw.get("feedback_edge_max_traversals", max_iterations))

    if feedback_max > max_iterations:
        raise WorkflowLoadError(
            f"Node '{node_id}' while_config.feedback_edge_max_traversals ({feedback_max}) "
            f"不能大于 max_iterations ({max_iterations})"
        )

    return WhileNodeConfig(
        continue_if=continue_if,
        max_iterations=max_iterations,
        backoff_seconds=raw.get("backoff_seconds", []) or [],
        feedback_edge_max_traversals=feedback_max,
    )


def _parse_widget(raw: dict[str, Any], workflow_id: str) -> WidgetDeclaration:
    wid = raw.get("id")
    if not wid:
        raise WorkflowLoadError(f"Widget in '{workflow_id}' missing 'id'")
    emit_on = raw.get("emit_on", {}) or {}
    return WidgetDeclaration(
        id=wid,
        type=raw.get("type"),
        title=raw.get("title", ""),
        emit_on_node=emit_on.get("node", ""),
        emit_on_event=emit_on.get("event", "node.completed"),
        props=raw.get("props", {}) or {},
    )


def _parse_widget_input(raw: dict[str, Any], workflow_id: str) -> WidgetInputBinding:
    if "from_widget" not in raw or "to_node" not in raw or "to_input" not in raw:
        raise WorkflowLoadError(
            f"widget_input in '{workflow_id}' must have from_widget / to_node / to_input"
        )
    return WidgetInputBinding(
        from_widget=raw["from_widget"],
        to_node=raw["to_node"],
        to_input=raw["to_input"],
        required=raw.get("required", False),
        trigger=raw.get("trigger", "human_review"),
    )


def _parse_runtime_placement(node_id: str, raw: Any, workflow_id: str):
    """解析 runtime_placement 字段，保证为 RuntimePlacementRef 中的值。"""
    if raw is None:
        return None
    from .schema import RuntimePlacementRef
    if not isinstance(raw, str):
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}': runtime_placement must be a string"
        )
    try:
        return RuntimePlacementRef(raw)
    except ValueError:
        raise WorkflowLoadError(
            f"Node '{node_id}' in workflow '{workflow_id}': invalid runtime_placement '{raw}'. "
            f"Valid: {[r.value for r in RuntimePlacementRef]}"
        )
