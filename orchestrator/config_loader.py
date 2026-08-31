"""P5: 配置驱动架构 — 从 config/ 目录加载所有 YAML 配置。

数据模型 + ConfigLoader + 启动时校验。

目录结构:
  config/
  ├── agents/*.yaml       → AgentDefinition
  ├── domains/*.yaml      → DomainDefinition
  ├── tools/*.yaml        → ToolConfig
  ├── knowledge/*.yaml    → KnowledgeBase
  ├── routes.yaml         → 路由配置（P6 用）
  ├── models.yaml         → 模型配置（H2 已实现）
  └── permissions.yaml    → 全局权限策略
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ====== 数据模型 ======

@dataclass
class AgentDefinition:
    """子 Agent 定义 — 绑定业务域 + 工具白名单 + 权限 + 知识库。"""
    agent_id: str                          # smart_query
    domain: str                            # smart_query
    display_name: str                      # "智能问数"
    description: str = ""
    harness: str = "local_llm"             # opencode / claude_code / local_llm
    model_config: dict[str, Any] | str = "auto"  # {provider, id} 或 "auto"
    system_prompt: str = ""
    output_files: dict[str, str] = field(default_factory=dict)  # port → 文件路径模板
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    knowledge_bases: list[str] = field(default_factory=list)
    max_concurrent_runs: int = 1
    timeout_seconds: int = 3600
    cost_limit_per_run: float = 1.0
    # 业务角色名（agent 级默认值，可被 workflow node 级 business_role 覆盖）
    business_role: str = ""
    # P0.18.10: tier 字段 — agent 能力上限（静态默认）
    # 取值：T0（advisor 仅对话）/ T1（read-only）/ T2（read-write）/ T3（read-write-exec）
    # 实际有效 tier = min(agent tier, workspace tier)；默认 T2（保守：读写但不执行 shell）
    tier: str = "T2"


@dataclass
class DomainDefinition:
    """业务域定义 — 域级共享配置 + 硬禁止权限。"""
    domain: str
    display_name: str
    description: str = ""
    default_harness: str = "local_llm"
    default_model: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)   # 域级默认允许
    denied_tools: list[str] = field(default_factory=list)    # 域级硬禁止（不可覆盖）
    default_knowledge_bases: list[str] = field(default_factory=list)
    cost_limit_per_run: float = 1.0


@dataclass
class ToolConfig:
    """工具配置 — schema + handler 引用 + 域权限 + 审计。"""
    tool_id: str
    display_name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    handler_module: str = ""               # tools.db_tools
    handler_function: str = ""             # execute_sql_query
    handler_config: dict[str, Any] = field(default_factory=dict)
    allowed_domains: list[str] = field(default_factory=list)
    audit_log_args: bool = True
    audit_log_result: bool = True
    requires_human_approval: bool = False  # 高危工具
    approval_widget: dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeBase:
    """知识库定义 — 来源 + 域级读写权限。"""
    kb_id: str
    display_name: str
    description: str = ""
    source_type: str = ""                  # vector_store / file / api
    source_connection: str = ""
    source_collection: str = ""
    read_domains: list[str] = field(default_factory=list)
    write_domains: list[str] = field(default_factory=list)
    admin_domains: list[str] = field(default_factory=list)
    content_categories: list[str] = field(default_factory=list)
    retention_days: int = 365


@dataclass
class SystemConfig:
    """聚合配置 — 所有 YAML 加载后的统一对象。"""
    agents: dict[str, AgentDefinition] = field(default_factory=dict)
    domains: dict[str, DomainDefinition] = field(default_factory=dict)
    tools: dict[str, ToolConfig] = field(default_factory=dict)
    knowledge: dict[str, KnowledgeBase] = field(default_factory=dict)
    routes: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)


# ====== 配置加载器 ======

class ConfigLoader:
    """从 config/ 目录加载所有 YAML 配置，启动时校验。"""

    def __init__(self, config_dir: str | Path | None = None):
        if config_dir is None:
            project_root = Path(__file__).parent.parent
            config_dir = project_root / "config"
        self.config_dir = Path(config_dir)

    def load_all(self) -> SystemConfig:
        """加载全部配置，返回聚合对象。"""
        return SystemConfig(
            agents=self._load_agents(),
            domains=self._load_domains(),
            tools=self._load_tools(),
            knowledge=self._load_knowledge(),
            routes=self._load_yaml("routes.yaml"),
            models=self._load_yaml("models.yaml"),
            permissions=self._load_yaml("permissions.yaml"),
        )

    def _load_yaml(self, filename: str) -> dict[str, Any]:
        """加载单个 YAML 文件，不存在返回空 dict。"""
        path = self.config_dir / filename
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_dir(self, subdir: str) -> dict[str, Any]:
        """加载子目录下所有 .yaml 文件，返回 {filename_stem: content}。"""
        result: dict[str, Any] = {}
        dir_path = self.config_dir / subdir
        if not dir_path.exists():
            return result
        for yml_file in sorted(dir_path.glob("*.yaml")):
            with open(yml_file, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data:
                result[yml_file.stem] = data
        return result

    def _load_agents(self) -> dict[str, AgentDefinition]:
        """加载 config/agents/*.yaml → AgentDefinition。"""
        raw_agents = self._load_dir("agents")
        agents: dict[str, AgentDefinition] = {}
        for _stem, raw in raw_agents.items():
            agent = self._parse_agent(raw)
            agents[agent.agent_id] = agent
        return agents

    def _parse_agent(self, raw: dict[str, Any]) -> AgentDefinition:
        """解析单个 Agent YAML → AgentDefinition。"""
        perms = raw.get("permissions", {}) or {}
        model_cfg = raw.get("model", "auto")
        # model 可以是 dict {provider, id} 或字符串 "auto"
        # P0.18.10: tier 字段校验 + 默认值兜底
        tier = str(raw.get("tier", "T2")).upper()
        if tier not in ("T0", "T1", "T2", "T3"):
            tier = "T2"  # 非法值降级为默认 T2
        return AgentDefinition(
            agent_id=raw.get("agent_id", ""),
            domain=raw.get("domain", ""),
            display_name=raw.get("display_name", raw.get("agent_id", "")),
            description=raw.get("description", ""),
            harness=raw.get("harness", "local_llm"),
            model_config=model_cfg if model_cfg else "auto",
            system_prompt=raw.get("system_prompt", ""),
            output_files=raw.get("output_files", {}) or {},
            allowed_tools=perms.get("allowed_tools", []) or [],
            denied_tools=perms.get("denied_tools", []) or [],
            knowledge_bases=raw.get("knowledge_bases", []) or [],
            max_concurrent_runs=raw.get("max_concurrent_runs", 1),
            timeout_seconds=raw.get("timeout_seconds", 3600),
            cost_limit_per_run=raw.get("cost_limit_per_run", 1.0),
            business_role=raw.get("business_role", ""),
            tier=tier,
        )

    def _load_domains(self) -> dict[str, DomainDefinition]:
        """加载 config/domains/*.yaml → DomainDefinition。"""
        raw_domains = self._load_dir("domains")
        domains: dict[str, DomainDefinition] = {}
        for _stem, raw in raw_domains.items():
            domain = self._parse_domain(raw)
            domains[domain.domain] = domain
        return domains

    def _parse_domain(self, raw: dict[str, Any]) -> DomainDefinition:
        """解析单个 Domain YAML → DomainDefinition。"""
        perms = raw.get("default_permissions", {}) or {}
        return DomainDefinition(
            domain=raw.get("domain", ""),
            display_name=raw.get("display_name", raw.get("domain", "")),
            description=raw.get("description", ""),
            default_harness=raw.get("default_harness", "local_llm"),
            default_model=raw.get("default_model", {}) or {},
            allowed_tools=perms.get("allowed_tools", []) or [],
            denied_tools=perms.get("denied_tools", []) or [],
            default_knowledge_bases=raw.get("default_knowledge_bases", []) or [],
            cost_limit_per_run=raw.get("cost_limit_per_run", 1.0),
        )

    def _load_tools(self) -> dict[str, ToolConfig]:
        """加载 config/tools/*.yaml → ToolConfig。"""
        raw_tools = self._load_dir("tools")
        tools: dict[str, ToolConfig] = {}
        for _stem, raw in raw_tools.items():
            tool = self._parse_tool(raw)
            tools[tool.tool_id] = tool
        return tools

    def _parse_tool(self, raw: dict[str, Any]) -> ToolConfig:
        """解析单个 Tool YAML → ToolConfig。"""
        raw_handler = raw.get("handler", {})
        # handler 可以是 dict（{module, function, config}）或 string（cli/python）
        if isinstance(raw_handler, dict):
            handler_module = raw_handler.get("module", "")
            handler_function = raw_handler.get("function", "")
            handler_config = raw_handler.get("config", {}) or {}
        else:
            # string 类型（如 "cli"、"python"）→ 存到 handler_config
            handler_module = str(raw_handler) if raw_handler else ""
            handler_function = ""
            handler_config = {}
            # CLI 工具：从 yaml 顶层读 command / working_dir / timeout 塞进 handler_config
            if raw_handler == "cli":
                if raw.get("command"):
                    handler_config["command"] = raw["command"]
                if raw.get("working_dir"):
                    handler_config["working_dir"] = raw["working_dir"]
                if raw.get("timeout"):
                    handler_config["timeout"] = raw["timeout"]
        audit = raw.get("audit", {}) or {}
        return ToolConfig(
            tool_id=raw.get("tool_id") or raw.get("name", ""),
            display_name=raw.get("display_name", raw.get("tool_id") or raw.get("name", "")),
            description=raw.get("description", ""),
            input_schema=raw.get("input_schema", {}) or {},
            handler_module=handler_module,
            handler_function=handler_function,
            handler_config=handler_config,
            allowed_domains=raw.get("allowed_domains", []) or [],
            audit_log_args=audit.get("log_args", True),
            audit_log_result=audit.get("log_result", True),
            requires_human_approval=raw.get("requires_human_approval", False),
            approval_widget=raw.get("approval_widget", {}) or {},
        )

    def _load_knowledge(self) -> dict[str, KnowledgeBase]:
        """加载 config/knowledge/*.yaml → KnowledgeBase。

        过滤掉无 ``kb_id`` 的 yaml（如 domains.yaml 是域配置清单，
        不属于知识库，不应被当作 KnowledgeBase 加载）。
        """
        raw_kbs = self._load_dir("knowledge")
        kbs: dict[str, KnowledgeBase] = {}
        for _stem, raw in raw_kbs.items():
            if not raw.get("kb_id"):
                continue
            kb = self._parse_knowledge(raw)
            kbs[kb.kb_id] = kb
        return kbs

    def _parse_knowledge(self, raw: dict[str, Any]) -> KnowledgeBase:
        """解析单个 Knowledge YAML → KnowledgeBase。"""
        source = raw.get("source", {}) or {}
        perms = raw.get("permissions", {}) or {}
        return KnowledgeBase(
            kb_id=raw.get("kb_id", ""),
            display_name=raw.get("display_name", raw.get("kb_id", "")),
            description=raw.get("description", ""),
            source_type=source.get("type", ""),
            source_connection=source.get("connection", ""),
            source_collection=source.get("collection", ""),
            read_domains=perms.get("read", []) or [],
            write_domains=perms.get("write", []) or [],
            admin_domains=perms.get("admin", []) or [],
            content_categories=raw.get("content_categories", []) or [],
            retention_days=raw.get("retention_days", 365),
        )

    # 内置工具（不在 config/tools/ 中定义，由引擎自身提供）
    # 注意：bash/write_file/read_file 需 agent yaml 显式在 allowed_tools 中声明才可用（fail-closed）
    BUILTIN_TOOLS = frozenset({
        "finalize",              # Agent 完成工具（所有 Agent 内置）
        "classify_intent",       # Manager 意图识别（P6）
        "plan_tasks",            # Manager 任务分解（P6）
        "dispatch",              # Manager 任务派发（P6）
        "aggregate",             # Manager 结果聚合（P6）
        "trigger_workflow",      # Manager 触发预定义 workflow（替代 bash+curl 范式）
        "collect_child_result",  # Manager 阻塞等待子 run 终态（标准闭环）
        "get_run_supervision",   # Manager 拉取 run 监督面板
        "list_session_runs",     # Manager 列出 session 下属 run
        "request_cross_domain",  # Manager 跨域协调
        "present_content",       # Manager 高层语义展示（v99.5 P0.8 A2UI 内部映射）
        "read_skill",            # 加载 skill 文档
        # opencode 内置工具（agent 通过 opencode server 原生调用）
        "read_file",
        "write_file",
        "bash",
    })

    # ====== 校验 ======

    # P0.18.10: 工具 → 所需最低 tier 映射（用于 tier × allowed_tools 一致性校验）
    # T0=对话+知识查询 / T1=read_file / T2=write_file / T3=bash/ssh_exec
    TOOL_REQUIRED_TIER: dict[str, str] = {
        "read_file": "T1",
        "list_dir": "T1",
        "write_file": "T2",
        "edit_file": "T2",
        "bash": "T3",
        "run_command": "T3",
        "ssh_exec": "T3",
        "server_restart": "T3",
        "db_migrate": "T3",
    }

    # tier 数值映射（用于 min 比较）
    _TIER_RANK: dict[str, int] = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}

    def validate(self, config: SystemConfig) -> list[str]:
        """启动时校验配置合法性，返回错误列表（空 = 通过）。"""
        errors: list[str] = []

        # 1. 每个 agent 的 domain 必须在 domains 中有定义
        for aid, agent in config.agents.items():
            if agent.domain and agent.domain != "manager" and agent.domain not in config.domains:
                errors.append(f"Agent '{aid}' 引用了未定义的 domain '{agent.domain}'")

        # 2. 每个 agent 的 allowed_tools 必须在 tools 中有定义（内置工具除外）
            for tool_id in agent.allowed_tools:
                if tool_id not in config.tools and tool_id not in self.BUILTIN_TOOLS:
                    errors.append(f"Agent '{aid}' allowed_tools 引用了未定义的工具 '{tool_id}'")

        # 3. 每个 agent 的 knowledge_bases 必须在 knowledge 中有定义
            for kb_id in agent.knowledge_bases:
                if kb_id not in config.knowledge:
                    errors.append(f"Agent '{aid}' knowledge_bases 引用了未定义的知识库 '{kb_id}'")

        # 4. agent 的 denied_tools 不能和 allowed_tools 冲突
            conflict = set(agent.allowed_tools) & set(agent.denied_tools)
            if conflict:
                errors.append(f"Agent '{aid}' denied/allowed 冲突: {conflict}")

        # 5. 域级 denied_tools 不能被 agent 的 allowed_tools 覆盖
        for aid, agent in config.agents.items():
            domain = config.domains.get(agent.domain)
            if domain:
                override = set(agent.allowed_tools) & set(domain.denied_tools)
                if override:
                    errors.append(
                        f"Agent '{aid}' allowed_tools 覆盖了域 '{agent.domain}' 硬禁止: {override}"
                    )

        # 6. P0.18.10: tier × allowed_tools 一致性校验
        # agent.tier 必须 ≥ allowed_tools 中所有工具的 required tier
        # 例：tier=T1 的 agent 不允许在 allowed_tools 中声明 write_file（需 T2）
        # 豁免：manager agent 是动态 tier（T0 静态 → 运行时按工具调用动态升 tier），
        #       allowed_tools 中的 read_file/write_file/bash 走 opencode 内置工具通道，
        #       不经过 AgentOps tool_map 拦截，因此不参与静态 tier 校验
        DYNAMIC_TIER_AGENTS = {"manager"}  # 动态 tier agent 集合（可扩展）
        for aid, agent in config.agents.items():
            if aid in DYNAMIC_TIER_AGENTS:
                continue
            agent_rank = self._TIER_RANK.get(agent.tier, 2)
            for tool_id in agent.allowed_tools:
                required = self.TOOL_REQUIRED_TIER.get(tool_id)
                if not required:
                    continue
                required_rank = self._TIER_RANK.get(required, 0)
                if agent_rank < required_rank:
                    errors.append(
                        f"Agent '{aid}' tier={agent.tier} 但 allowed_tools 含 '{tool_id}' "
                        f"(需 {required})；请升级 tier 或移除该工具"
                    )

        return errors


# ====== 全局单例 ======

_system_config: SystemConfig | None = None


def get_system_config() -> SystemConfig:
    """获取全局 SystemConfig 单例（懒加载）。"""
    global _system_config
    if _system_config is None:
        loader = ConfigLoader()
        _system_config = loader.load_all()
    return _system_config


def reload_system_config() -> SystemConfig:
    """强制重新加载配置（热重载用）。"""
    global _system_config
    loader = ConfigLoader()
    _system_config = loader.load_all()
    return _system_config
