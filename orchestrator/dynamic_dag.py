"""P6: 动态 DAG 描述 — Manager Agent 运行时生成的 DAG。

DynamicDagSpec → WorkflowDefinition → DagEngine 执行。

与固定模板（workflows/*.yaml）的区别：
  - 固定模板：预定义拓扑，静态加载
  - 动态 DAG：Manager 根据用户请求运行时生成拓扑
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from orchestrator.config_loader import get_system_config
from workflow.schema import (
    HarnessTypeRef,
    NodeType,
    WorkflowDefinition,
    WorkflowNode,
)


@dataclass
class DynamicNodeSpec:
    """动态 DAG 节点描述。"""
    id: str                              # step_1
    agent_domain: str                    # smart_query / smart_ops / ...
    task_description: str                # "查本月报销超过5000的记录"
    inputs_from: list[str] = field(default_factory=list)  # 依赖的上游节点 ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_domain": self.agent_domain,
            "task_description": self.task_description,
            "inputs_from": self.inputs_from,
        }


@dataclass
class DynamicDagSpec:
    """Manager Agent 动态生成的 DAG 描述。"""
    nodes: list[DynamicNodeSpec]
    edges: list[tuple[str, str]] = field(default_factory=list)  # (from_node, to_node)
    generated_by: str = "manager_agent"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_workflow_def(self) -> WorkflowDefinition:
        """转换为 DagEngine 可执行的 WorkflowDefinition。"""
        config = get_system_config()
        wf_nodes: dict[str, WorkflowNode] = {}

        for n in self.nodes:
            # 按 agent_domain 查找对应的 AgentDefinition
            agent_def = self._find_agent_by_domain(config, n.agent_domain)
            if agent_def is None:
                raise ValueError(f"域 '{n.agent_domain}' 没有对应的 Agent 定义")

            # harness 类型从 AgentDefinition 获取
            try:
                harness_ref = HarnessTypeRef(agent_def.harness)
            except ValueError:
                harness_ref = HarnessTypeRef.LOCAL_LLM

            # after 从 edges 推导
            after = [e[0] for e in self.edges if e[1] == n.id] or n.inputs_from

            wf_nodes[n.id] = WorkflowNode(
                id=n.id,
                name=n.task_description[:50],
                type=NodeType.AGENT,
                agent=agent_def.agent_id,
                harness=harness_ref,
                after=after,
                config={"task_description": n.task_description},
                domain=n.agent_domain,
            )

        return WorkflowDefinition(
            workflow_id=f"dynamic_{int(time.time())}",
            name="Manager Generated DAG",
            nodes=wf_nodes,
        )

    def validate(self) -> list[str]:
        """校验 DAG 合法性，返回错误列表（空 = 通过）。"""
        errors: list[str] = []
        config = get_system_config()

        # 1. 检查域存在
        for n in self.nodes:
            if n.agent_domain not in config.domains and n.agent_domain != "manager":
                errors.append(f"节点 '{n.id}' 引用了未定义的域 '{n.agent_domain}'")

        # 2. 检查节点 ID 唯一
        node_ids = [n.id for n in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            errors.append("节点 ID 不唯一")

        # 3. 检查 edges 引用的节点存在
        id_set = set(node_ids)
        for src, dst in self.edges:
            if src not in id_set:
                errors.append(f"edge 引用了不存在的源节点 '{src}'")
            if dst not in id_set:
                errors.append(f"edge 引用了不存在的目标节点 '{dst}'")

        # 4. 检查无环
        if self._has_cycle():
            errors.append("DAG 包含环")

        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [list(e) for e in self.edges],
            "generated_by": self.generated_by,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DynamicDagSpec":
        """从字典反序列化。"""
        nodes = [
            DynamicNodeSpec(
                id=n["id"],
                agent_domain=n["agent_domain"],
                task_description=n.get("task_description", ""),
                inputs_from=n.get("inputs_from", []),
            )
            for n in data.get("nodes", [])
        ]
        edges = [tuple(e) for e in data.get("edges", [])]
        return cls(
            nodes=nodes,
            edges=edges,
            generated_by=data.get("generated_by", "manager_agent"),
            generated_at=data.get("generated_at", datetime.now(timezone.utc).isoformat()),
        )

    def _find_agent_by_domain(self, config, domain: str):
        """按域查找第一个 Agent。"""
        for agent in config.agents.values():
            if agent.domain == domain:
                return agent
        return None

    def _has_cycle(self) -> bool:
        """拓扑排序检测环。"""
        # 构建邻接表
        graph: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for src, dst in self.edges:
            graph[src].append(dst)

        # DFS 检测环
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n.id: WHITE for n in self.nodes}

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for neighbor in graph.get(node, []):
                if color.get(neighbor, WHITE) == GRAY:
                    return True
                if color.get(neighbor, WHITE) == WHITE and dfs(neighbor):
                    return True
            color[node] = BLACK
            return False

        for n in self.nodes:
            if color[n.id] == WHITE:
                if dfs(n.id):
                    return True
        return False
