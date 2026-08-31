"""
SystemPromptBuilder — v2.1 三层模型 system_prompt 动态拼装。

替代 session_engine.py 中 system_prompt[:20000] 截断的硬编码逻辑，
按「角色层 → 能力层 → 编排层 → 动态层」顺序分段拼装。

三层模型（v2.1）：
- 角色层（可变）：node.role_prompt，来自 workflow yaml
- 能力层（固定）：agent.system_prompt + agent.allowed_tools
- 编排层（自动）：workflow_registry + skill_registry
- 动态层：session_context（记忆/DAG 上下文）

集成点：
- SessionEngine.__init__ 可构造 SystemPromptBuilder，传入 workflow_registry
- _execute_node 构造 RoleContext 传入 builder.build(node_role=...)
- 当前 session_engine.py 的拼接逻辑保持不变，本模块作为 Phase 2/3 集成入口

参考：docs/architecture/DESIGN_architecture_refactor_v2.md §六
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.workflow_registry import WorkflowRegistry

logger = logging.getLogger(__name__)


@dataclass
class RoleContext:
    """节点级角色上下文，来自 workflow yaml 的 node 配置。

    与 agent.system_prompt 分离——
    - agent.system_prompt 跨 workflow 固定（能力层）
    - RoleContext 跨 workflow 可变（角色层）

    三层模型核心：节点只能约束角色提示，不能扩展 agent 能力。
    """
    role_prompt: str | None = None      # 节点级角色提示，注入 system_prompt 开头
    business_role: str | None = None    # 角色名，用于泳道分组（已有字段，这里复用）


class SystemPromptBuilder:
    """分段拼装 system_prompt，替代 system_prompt[:N] 截断。

    v2.1 三层模型拼装顺序：
    1. 节点角色提示（可变层，来自 workflow node.role_prompt）
    2. 基础契约（固定层，来自 agent.yaml system_prompt）
    3. workflow 注册表（自动层，来自 WorkflowRegistry）
    4. skill 列表 metadata（自动层，来自 SkillRegistry，Phase 2 实现）
    5. 工具列表（固定层，来自 agent.allowed_tools，过渡期保留）
    6. 历史记忆（动态层，来自 session_context.memory）
    7. DAG 上下文（动态层，来自 session_context.dag_context）
    """

    def __init__(
        self,
        workflow_registry: "WorkflowRegistry | None" = None,
        skill_registry: Any = None,
    ):
        """
        Args:
            workflow_registry: WorkflowRegistry 实例（可选，Phase 1 已实现）
            skill_registry: SkillRegistry 实例（可选，Phase 2 实现）
        """
        self._workflows = workflow_registry
        self._skills = skill_registry

    def build(
        self,
        agent: Any,
        session_context: dict[str, Any] | None = None,
        node_role: RoleContext | None = None,
        tools_prompt: str | None = None,
    ) -> str:
        """分段拼装 system_prompt。

        Args:
            agent: 能力层配置（AgentDefinition），跨 workflow 固定。
                   需有 system_prompt / domain / allowed_tools 等字段。
            session_context: 会话动态上下文，支持键：
                - memory: 历史记忆文本
                - dag_context: 当前 DAG 上下文文本
            node_role: 角色层配置（可变，来自 workflow 节点）
            tools_prompt: 工具描述文本（过渡期保留，Phase 3 废弃）

        Returns:
            拼装后的完整 system_prompt 字符串
        """
        sections: list[str] = []

        # 1. 节点角色提示（v2.1 新增，可变层）
        #    来自 workflow yaml 的 node.role_prompt
        #    注入到最开头，让 LLM 先建立角色认知
        if node_role and node_role.role_prompt:
            sections.append(f"## 你的角色\n{node_role.role_prompt}")

        # 2. 基础契约（来自 agent.yaml 的 system_prompt）
        #    能力层，跨 workflow 固定
        base_prompt = getattr(agent, "system_prompt", "") or ""
        if base_prompt:
            sections.append(base_prompt)

        # 3. workflow 注册表（自动扫描）
        #    能力层补充：让 agent 知道有哪些 workflow 可触发
        if self._workflows:
            try:
                wf_section = self._workflows.build_prompt_section()
                if wf_section:
                    sections.append(wf_section)
            except Exception as e:
                logger.warning("注入 workflow registry 失败（不阻塞）: %s", e)

        # 4. skill 列表（metadata，不全量 inline）
        #    Phase 2 实现，当前跳过
        if self._skills:
            try:
                skill_section = self._build_skill_section(agent)
                if skill_section:
                    sections.append(skill_section)
            except Exception as e:
                logger.warning("注入 skill registry 失败（不阻塞）: %s", e)

        # 5. 工具列表（按 agent 过滤后的工具集）
        #    能力层，跨 workflow 固定（节点不能扩展）
        #    过渡期保留：opencode harness 不转发 tools 到 LLM，需注入 system_prompt
        #    Phase 3 切到 harness 原生 tool calling 后废弃
        if tools_prompt:
            sections.append(f"## 可用工具\n{tools_prompt}")

        # 6. 历史记忆（如有）
        if session_context and session_context.get("memory"):
            sections.append(f"## 历史记忆\n{session_context['memory']}")

        # 7. DAG 上下文（如有）
        if session_context and session_context.get("dag_context"):
            sections.append(f"## 当前 DAG 上下文\n{session_context['dag_context']}")

        return "\n\n".join(sections)

    def _build_skill_section(self, agent: Any) -> str:
        """构建 skill metadata 列表段（Phase 2 实现）。

        当前 SkillRegistry 未实现，返回空字符串。
        Phase 2 实现后，返回格式：
            ## 可用 Skill（需要详细操作指引时调 read_skill）
            - **dag-ops**: DAG 工作流操作指南 [_shared]
            - **workflow-author**: workflow 生成规范 [_shared]
        """
        if not self._skills:
            return ""
        agent_domain = getattr(agent, "domain", "")
        skills = self._skills.list_for_agent(agent_domain)
        if not skills:
            return "## 可用 Skill\n（无）"
        lines = [
            "## 可用 Skill（需要详细操作指引时调 read_skill）",
            "",
        ]
        for s in skills:
            lines.append(f"- **{s.id}**: {s.description} [{s.domain}]")
        return "\n".join(lines)
