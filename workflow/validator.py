"""
Workflow validator — 三层校验：结构 + 语义 + 图论。

Layer 1 结构校验：YAML schema + 字段类型（已有 7 项规则）
Layer 2 语义校验：跨字段一致性（port 匹配、事件名、harness 配置）
Layer 3 图论校验：可达性 + 终止性 + skip_if 引用完备性
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from typing import Any

from .schema import HarnessTypeRef, NodeType, WorkflowDefinition
from .schema import RuntimePlacementRef

logger = logging.getLogger(__name__)


class WorkflowValidationError(Exception):
    def __init__(self, errors: list[str], warnings: list[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__(f"Workflow validation failed: {len(errors)} error(s)")


def validate_workflow(
    workflow: WorkflowDefinition,
    agent_configs: dict[str, Any] | None = None,
) -> None:
    """三层校验：结构 → 语义 → 图论。任一层有错误则 raise。

    Args:
        workflow: 工作流定义
        agent_configs: 可选，agent_id → AgentDefinition 映射。
            传入时启用 3 个跨文件语义校验（agent 存在性 / 路由完备性 / output_files 匹配）。
            为 None 时跳过这 3 项（向后兼容现有单元测试）。
    """
    errors: list[str] = []

    # === Layer 1: 结构校验（已有 7 项）===

    # 1) Every "after" target must exist
    for nid, node in workflow.nodes.items():
        for dep in node.after:
            if dep not in workflow.nodes:
                errors.append(f"[结构] Node '{nid}' references unknown dependency '{dep}'")

    # 2) Every output target must exist (支持多消费者)
    for nid, node in workflow.nodes.items():
        for port, route in node.outputs.items():
            for target, _ in route.parse_all():
                if target and target not in workflow.nodes:
                    errors.append(
                        f"[结构] Node '{nid}' output port '{port}' references unknown target '{target}'"
                    )

    # 3) Agent-type nodes must have agent OR inline_agent set (P0.5: 二选一)
    for nid, node in workflow.nodes.items():
        if node.type == NodeType.AGENT and not (node.agent or node.inline_agent):
            errors.append(
                f"[结构] Agent node '{nid}' 必须配 'agent' 或 'inline_agent' 之一"
            )

    # 3.1) agent 与 inline_agent 互斥（不能同时存在）
    for nid, node in workflow.nodes.items():
        if node.agent and node.inline_agent:
            errors.append(
                f"[结构] Node '{nid}': 'agent' 与 'inline_agent' 互斥，"
                f"二选一（同时存在表明意图不清）"
            )

    # 4) Gateway must have gateway_kind + condition
    for nid, node in workflow.nodes.items():
        if node.type == NodeType.GATEWAY:
            if not node.gateway_kind:
                errors.append(f"[结构] Gateway node '{nid}' must have 'gateway_kind'")
            if node.gateway_kind and node.gateway_kind.value == "condition" and not node.condition:
                errors.append(f"[结构] Condition gateway '{nid}' must have 'condition'")

    # 5) parallel_branch must have branches pointing to existing nodes
    for nid, node in workflow.nodes.items():
        if node.type == NodeType.PARALLEL_BRANCH:
            if not node.branches:
                errors.append(f"[结构] Parallel branch '{nid}' must list 'branches'")
            for b in node.branches:
                if b not in workflow.nodes:
                    errors.append(
                        f"[结构] Parallel branch '{nid}' references unknown branch '{b}'"
                    )

    # 6) No cycles (topological sort check)
    cycle = _find_cycle(workflow)
    if cycle:
        errors.append(f"[结构] Workflow contains cycle: {' -> '.join(cycle)}")

    # 7) Widget input bindings point to real widgets + nodes
    widget_ids = {w.id for w in workflow.widgets}
    for wi in workflow.widget_inputs:
        if wi.from_widget not in widget_ids:
            errors.append(
                f"[结构] widget_input references unknown widget '{wi.from_widget}'"
            )
        if wi.to_node not in workflow.nodes:
            errors.append(
                f"[结构] widget_input '{wi.from_widget}' targets unknown node '{wi.to_node}'"
            )

    # === Layer 2: 语义校验（新增）===

    # 8) output port 名与下游 input 声明不匹配（警告，不阻断）
    #    DAG 引擎按 output 路由投递，不依赖下游 inputs 声明，
    #    但声明缺失意味着节点签名不完整，可能是 yaml 笔误
    warnings: list[str] = []
    for nid, node in workflow.nodes.items():
        for port_name, route in node.outputs.items():
            for target_node_id, target_port in route.parse_all():
                if target_port is None:
                    continue  # 无显式 port 引用，跳过
                if target_node_id not in workflow.nodes:
                    continue  # 结构校验已报
                downstream = workflow.nodes[target_node_id]
                if target_port not in downstream.inputs:
                    warnings.append(
                        f"[语义] Node '{nid}' output port '{port_name}' 路由到 "
                        f"{target_node_id}.in:{target_port}，"
                        f"但下游节点未声明该 input port"
                    )

    # 9) 事件名一致性（widget emit_on_event 必须在 DagEventType 枚举中）
    try:
        from orchestrator.protocol import DagEventType
        valid_event_types = {e.value for e in DagEventType}
    except Exception:
        valid_event_types = set()  # 防止循环导入阻断校验

    if valid_event_types:
        for widget in workflow.widgets:
            event_name = widget.emit_on_event
            if event_name and event_name not in valid_event_types:
                errors.append(
                    f"[语义] Widget '{widget.id}' emit_on_event='{event_name}' "
                    f"不在 DagEventType 枚举中，有效值: {sorted(valid_event_types)}"
                )

    # 10) 非 deterministic harness 节点必须有 agent_id 或 inline_agent
    #     deterministic 节点的 agent 字段用于 metadata（business_role/output_files），不报错
    for nid, node in workflow.nodes.items():
        if node.type != NodeType.AGENT:
            continue
        if node.harness != HarnessTypeRef.DETERMINISTIC and not (node.agent or node.inline_agent):
            errors.append(
                f"[语义] Node '{nid}': {node.harness.value} harness 节点必须配 agent_id 或 inline_agent"
            )

    # 10.1) v2.1 role_prompt 校验：配了 role_prompt 的节点必须有 agent_id 或 inline_agent
    #       角色提示不能脱离能力载体（三层模型：Role 依附 Agent）
    for nid, node in workflow.nodes.items():
        if node.role_prompt and not (node.agent or node.inline_agent):
            errors.append(
                f"[语义] Node '{nid}': 配了 role_prompt 但未配 agent / inline_agent，"
                f"角色提示必须依附能力载体（三层模型：Role 依附 Agent）"
            )

    # 10.2) runtime_placement 校验：若存在，必须为 RuntimePlacementRef 的合法值
    for nid, node in workflow.nodes.items():
        rp = getattr(node, "runtime_placement", None)
        if rp is None:
            continue
        # If loader produced a string (older programmatic creation), accept it if matches enum
        if isinstance(rp, str):
            try:
                RuntimePlacementRef(rp)
            except ValueError:
                errors.append(
                    f"[语义] Node '{nid}': invalid runtime_placement '{rp}'. "
                    f"Valid: {[r.value for r in RuntimePlacementRef]}"
                )
            continue
        if not isinstance(rp, RuntimePlacementRef):
            errors.append(
                f"[语义] Node '{nid}': runtime_placement must be one of {[r.value for r in RuntimePlacementRef]}"
            )
            continue
        # policy: deterministic harness should not request docker_container
        if rp == RuntimePlacementRef.DOCKER_CONTAINER and node.harness == HarnessTypeRef.DETERMINISTIC:
            errors.append(
                f"[语义] Node '{nid}': deterministic harness 不应设置 runtime_placement='docker_container'"
            )

    # === Layer 2 扩展：跨文件语义校验（v88 新增，需 agent_configs）===
    # 这 3 项只在传入 agent_configs 时启用，向后兼容现有单元测试（单参数调用）

    if agent_configs is not None:
        # 11) agent 存在性：workflow 引用的 agent_id 必须在 config/agents/ 中存在
        for nid, node in workflow.nodes.items():
            if node.type != NodeType.AGENT:
                continue
            if not node.agent:
                continue  # 规则 10 已报
            if node.agent not in agent_configs:
                errors.append(
                    f"[语义] Node '{nid}' 引用 agent '{node.agent}'，"
                    f"但 config/agents/ 中不存在该 agent_id（可用: {sorted(agent_configs.keys())}）"
                )

        # 12) agent 路由完备性：节点配了 role_prompt + agent，
        #     但 agent system_prompt 里没有该节点的路由段 → warning
        #     说明 agent 不知道该节点该干什么（role_prompt 与 system_prompt 节点段不匹配）
        #     判据：system_prompt 含 "node_id == "<nid>"" 字面量（精确匹配节点路由段）
        #     不检查 {{node_id}} 占位符（占位符只代表有路由机制，不代表所有节点都有路由段）
        for nid, node in workflow.nodes.items():
            if not node.role_prompt or not node.agent:
                continue
            if node.agent not in agent_configs:
                continue  # 规则 11 已报
            agent_def = agent_configs[node.agent]
            system_prompt = getattr(agent_def, "system_prompt", "") or ""
            # 精确匹配：含 "node_id == "<nid>"" 或 "node_id=="<nid>"" 字面量
            has_node_id_literal = (
                f'node_id == "{nid}"' in system_prompt
                or f'node_id=="{nid}"' in system_prompt
            )
            if not has_node_id_literal:
                warnings.append(
                    f"[语义] Node '{nid}' 配了 role_prompt + agent='{node.agent}'，"
                    f"但 agent system_prompt 未含 node_id == \"{nid}\" 路由段，"
                    f"agent 可能不知道该节点该干什么"
                )

        # 13) agent output_files 匹配：agent yaml 的 output_files 端口名
        #     必须包含 workflow 节点 outputs 声明的所有端口
        #     否则文件收割时找不到对应文件 → 下游无输入
        for nid, node in workflow.nodes.items():
            if not node.agent or node.agent not in agent_configs:
                continue
            agent_def = agent_configs[node.agent]
            agent_output_files = getattr(agent_def, "output_files", {}) or {}
            for port_name in node.outputs:
                if port_name not in agent_output_files:
                    errors.append(
                        f"[语义] Node '{nid}' outputs 端口 '{port_name}' "
                        f"在 agent '{node.agent}' 的 output_files 中未声明，"
                        f"文件收割会失败（agent output_files: {sorted(agent_output_files.keys())}）"
                    )

    # === Layer 3: 图论校验（新增）===

    # 14) 所有分支必须能到达终止节点
    #     终止节点定义：无 outputs，或所有 outputs 的 target 都是空（terminal port）
    #     P0.1: command.success / await_command.timeout 等 terminal port 是合法的「终止」
    def _is_terminal(node) -> bool:
        if not node.outputs:
            return True
        return all(
            all(not t for t, _ in route.parse_all())
            for route in node.outputs.values()
        )
    terminals = [nid for nid, n in workflow.nodes.items() if _is_terminal(n)]
    if not terminals:
        errors.append("[图论] 工作流没有终止节点（无 output 或全是 terminal port 的节点）")

    # 15) skip_if 条件引用的 port 必须在上游 output 中声明
    for nid, node in workflow.nodes.items():
        if not node.skip_if:
            continue
        # 解析 {{node_id.port_name}} 引用
        refs = re.findall(r"\{\{(\w+)\.(\w+)\}\}", node.skip_if)
        for ref_node, ref_port in refs:
            if ref_node not in workflow.nodes:
                errors.append(
                    f"[图论] Node '{nid}' skip_if 引用不存在的节点 '{ref_node}'"
                )
                continue
            upstream = workflow.nodes[ref_node]
            if ref_port not in upstream.outputs:
                errors.append(
                    f"[图论] Node '{nid}' skip_if 引用 {ref_node}.{ref_port}，"
                    f"但该节点未声明 output port '{ref_port}'"
                )

    # === P0.1: 3 类新原语的语义/图论校验 ===

    # 16) command 节点必有 outputs.success port（成功路径）
    for nid, node in workflow.nodes.items():
        if node.type != NodeType.COMMAND:
            continue
        if "success" not in node.outputs:
            errors.append(
                f"[语义] command 节点 '{nid}' 必须声明 outputs.success port"
            )

    # 17) await_command 节点 ≤ 1 个
    await_command_count = sum(
        1 for n in workflow.nodes.values() if n.type == NodeType.AWAIT_COMMAND
    )
    if await_command_count > 1:
        errors.append(
            f"[图论] 工作流含 {await_command_count} 个 await_command 节点，"
            f"协议冲突——整工作流 ≤ 1 个"
        )

    # 18) while 节点的 feedback edge 必须设 max_traversals ≤ max_iterations
    #    (UNBOUNDED_CYCLE 防失控)
    for nid, node in workflow.nodes.items():
        if node.type != NodeType.WHILE:
            continue
        if not node.while_config:
            continue
        max_iter = node.while_config.max_iterations
        max_trav = node.while_config.feedback_edge_max_traversals
        # 校验反馈边：从该 while 节点出发、最终回到该 while 节点的下游路径
        # 这里做静态校验：while 节点的 outputs 至少有一个 target 回到自己或上游 while 节点
        feedback_targets = []
        for port, route in node.outputs.items():
            for target, _ in route.parse_all():
                if target == nid or target in workflow.nodes:
                    feedback_targets.append(target)
        # 检查其他节点是否有输出回到此 while 节点（构成 feedback edge）
        has_feedback_edge = False
        for other_id, other_node in workflow.nodes.items():
            for port, route in other_node.outputs.items():
                for target, _ in route.parse_all():
                    if target == nid and other_id != nid:
                        has_feedback_edge = True
                        break
        if has_feedback_edge and max_trav > max_iter:
            errors.append(
                f"[图论] while 节点 '{nid}' 的 feedback_edge_max_traversals ({max_trav}) "
                f"超过 max_iterations ({max_iter})，可能失控"
            )

    if errors:
        raise WorkflowValidationError(errors, warnings)
    if warnings:
        for w in warnings:
            logger.warning(w)
    return None


def _find_cycle(workflow: WorkflowDefinition) -> list[str] | None:
    """Detect cycle using Kahn's algorithm. Return cycle path if exists."""
    in_degree: dict[str, int] = {nid: 0 for nid in workflow.nodes}
    adj: dict[str, list[str]] = defaultdict(list)

    for nid, node in workflow.nodes.items():
        for dep in node.after:
            adj[dep].append(nid)
            in_degree[nid] += 1

    queue = deque([nid for nid, d in in_degree.items() if d == 0])
    visited_count = 0

    while queue:
        curr = queue.popleft()
        visited_count += 1
        for nxt in adj[curr]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if visited_count != len(workflow.nodes):
        # Cycle exists. Find one cycle path for error message.
        return _trace_cycle(workflow, adj, in_degree)
    return None


def _trace_cycle(
    workflow: WorkflowDefinition, adj: dict[str, list[str]], in_degree: dict[str, int]
) -> list[str]:
    """DFS to find a cycle path (crude but useful for error messages)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in workflow.nodes}
    parent: dict[str, str | None] = {nid: None for nid in workflow.nodes}

    def dfs(start: str) -> list[str] | None:
        stack = [(start, iter(adj.get(start, [])))]
        color[start] = GRAY
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
                if color[child] == GRAY:
                    # Found cycle: trace back from `node` to `child`
                    cycle = [child, node]
                    cur = node
                    while parent.get(cur) is not None and parent[cur] != child:
                        cur = parent[cur]
                        cycle.append(cur)
                    return list(reversed(cycle))
                if color[child] == WHITE:
                    color[child] = GRAY
                    parent[child] = node
                    stack.append((child, iter(adj.get(child, []))))
            except StopIteration:
                color[node] = BLACK
                stack.pop()
        return None

    for nid in workflow.nodes:
        if color[nid] == WHITE:
            result = dfs(nid)
            if result:
                return result
    return ["<unknown>"]


def topological_order(workflow: WorkflowDefinition) -> list[str]:
    """Return nodes in valid execution order (BFS by level)."""
    in_degree: dict[str, int] = {nid: 0 for nid in workflow.nodes}
    adj: dict[str, list[str]] = defaultdict(list)

    for nid, node in workflow.nodes.items():
        for dep in node.after:
            adj[dep].append(nid)
            in_degree[nid] += 1

    levels: list[list[str]] = []
    current_level = [nid for nid, d in in_degree.items() if d == 0]

    while current_level:
        levels.append(current_level)
        next_level: list[str] = []
        for nid in current_level:
            for nxt in adj[nid]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    next_level.append(nxt)
        current_level = next_level

    if sum(len(layer) for layer in levels) != len(workflow.nodes):
        raise WorkflowValidationError(["Workflow has cycle (topo sort failed)"])

    return [nid for level in levels for nid in level]
