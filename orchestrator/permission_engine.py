"""P5: 权限引擎 — 运行时权限校验。

三级优先级（deny > allow > default deny）:
  1. 域级 denied_tools（硬禁止，不可覆盖）
  2. Agent 级 denied_tools（显式禁止）
  3. Agent 级 allowed_tools（显式允许）
  4. 域级 allowed_tools（域默认允许）
  5. 默认拒绝（fail-closed）

知识库权限:
  read  → agent.domain in kb.read_domains
  write → agent.domain in kb.write_domains
"""
from __future__ import annotations

from dataclasses import dataclass

from orchestrator.config_loader import SystemConfig


@dataclass
class PermissionResult:
    """权限校验结果。"""
    allowed: bool = False
    denied: bool = False
    reason: str = ""

    @classmethod
    def allow(cls) -> "PermissionResult":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "PermissionResult":
        return cls(denied=True, reason=reason)


class PermissionEngine:
    """运行时权限校验。工具调用前必须过这里。"""

    def __init__(self, config: SystemConfig):
        self.config = config

    def check_tool_access(self, agent_id: str, tool_id: str) -> PermissionResult:
        """校验 agent 是否有权调用 tool。

        优先级链:
          域级 denied > Agent denied > Agent allowed > 域级 allowed > 默认拒绝
        """
        agent = self.config.agents.get(agent_id)
        if agent is None:
            return PermissionResult.deny(f"Agent '{agent_id}' 不存在")

        domain = self.config.domains.get(agent.domain)

        # 1. 域级硬禁止（不可覆盖）
        if domain and tool_id in domain.denied_tools:
            return PermissionResult.deny(
                f"域 '{agent.domain}' 硬禁止工具 '{tool_id}'（不可覆盖）"
            )

        # 2. Agent 级显式禁止
        if tool_id in agent.denied_tools:
            return PermissionResult.deny(f"Agent '{agent_id}' 禁止工具 '{tool_id}'")

        # 3. Agent 级允许
        if tool_id in agent.allowed_tools:
            return PermissionResult.allow()

        # 4. 域级允许
        if domain and tool_id in domain.allowed_tools:
            return PermissionResult.allow()

        # 5. 默认拒绝（fail-closed）
        return PermissionResult.deny(
            f"工具 '{tool_id}' 未授权给 Agent '{agent_id}'（默认拒绝）"
        )

    def check_knowledge_access(
        self, agent_id: str, kb_id: str, action: str
    ) -> PermissionResult:
        """校验 agent 是否有权访问知识库。

        action: "read" / "write" / "admin"
        """
        agent = self.config.agents.get(agent_id)
        if agent is None:
            return PermissionResult.deny(f"Agent '{agent_id}' 不存在")

        kb = self.config.knowledge.get(kb_id)
        if kb is None:
            return PermissionResult.deny(f"知识库 '{kb_id}' 不存在")

        if action == "read":
            if agent.domain in kb.read_domains:
                return PermissionResult.allow()
        elif action == "write":
            if agent.domain in kb.write_domains:
                return PermissionResult.allow()
        elif action == "admin":
            if agent.domain in kb.admin_domains:
                return PermissionResult.allow()
        else:
            return PermissionResult.deny(f"未知操作 '{action}'")

        return PermissionResult.deny(
            f"Agent '{agent_id}'（域 '{agent.domain}'）无权 {action} 知识库 '{kb_id}'"
        )

    def get_agent_permission_set(self, agent_id: str):
        """计算 Agent 的最终 PermissionSet（合并域级 + Agent 级）。

        返回 harness.PermissionSet，可直接注入 AgentRunContext。
        """
        from harness import PermissionSet

        agent = self.config.agents.get(agent_id)
        if agent is None:
            return PermissionSet()

        domain = self.config.domains.get(agent.domain)

        # 合并 allowed：域级 allowed + Agent allowed
        allowed = set(domain.allowed_tools) if domain else set()
        allowed |= set(agent.allowed_tools)

        # 合并 denied：域级 denied + Agent denied（deny 优先）
        denied = set(domain.denied_tools) if domain else set()
        denied |= set(agent.denied_tools)

        # denied 覆盖 allowed
        allowed -= denied

        return PermissionSet(allowed_tools=allowed, denied_tools=denied)
