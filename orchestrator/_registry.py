"""轻量全局注册器：让 trigger_workflow 工具不依赖具体 orchestrator 类。

设计动机：tools/trigger_workflow.py 在 Agent 进程内被调用，触发 workflow 时需要
访问 orchestrator 实例。但 tools/ 不应 import orchestrator（反方向依赖 + 循环）。
所以让 api/server.py 在 lifespan 启动时把 orchestrator 实例注册到这里，工具只
通过本模块的 getter 拿。tool 永远不 import orchestrator，server 反向 import tool。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from orchestrator.protocol import Orchestrator


_orchestrator: "Orchestrator | None" = None
# 桥接事件到 SSE 队列（api/server.py:start_run 的 _bridge 逻辑提到这里）
# 形参: (run_id, event) -> await
_event_bridge: "Any | None" = None
# EventStore 句柄（可选，用于 init_run 落库）
_event_store: "Any | None" = None
# 当前活跃 LLM 调用的 run_id（conversational engine 每轮 LLM.run 前设置，工具调用期间可读）。
# 让 trigger_workflow 等工具 handler 不需要把 parent_run_id 显式传过来就能定位父会话。
# 非线程安全（agent loop 是 asyncio 单协程），但足以支撑工具 handler 的同步调用。
_current_active_run_id: str | None = None
# 🆕 Phase 1: Session 管理器单例（管理长 Session 与子 Run 的关系）
_session_manager: "Any | None" = None
# 🆕 Phase 2: 记忆管理器单例（分层记忆：短期/中期/长期）
_memory_manager: "Any | None" = None
# 🆕 WorkflowRegistry 单例（启动时扫描 workflows/*.yaml，动态注入 system_prompt）
_workflow_registry: "Any | None" = None
# 🆕 Phase 2: SkillRegistry 单例（启动时扫描 skills/*/SKILL.md，动态注入 system_prompt）
_skill_registry: "Any | None" = None
# P0.18.7b: ContainerProvisioner 单例（lifespan 启动时实例化）
_container_provisioner: "Any | None" = None
# 任务管理模块 TaskOrchestrator 单例（P0，lifespan 启动时实例化）
_task_orchestrator: "Any | None" = None


def set_orchestrator(orch: "Orchestrator") -> None:
    global _orchestrator
    _orchestrator = orch
    logger.info("orchestrator registered: %s", type(orch).__name__)


def get_orchestrator() -> "Orchestrator | None":
    return _orchestrator


def set_event_bridge(bridge: "Any") -> None:
    """注册事件桥接（async (run_id, event) -> None）。

    trigger_workflow 创建 run 后会用这个把 DagEvent 推到 _event_streams[run_id] 队列，
    让前端 SSE 能正常收到事件流。如果没注册，run 仍会跑但前端看不到实时进度。
    """
    global _event_bridge
    _event_bridge = bridge


def get_event_bridge() -> "Any | None":
    return _event_bridge


def set_event_store(store: "Any") -> None:
    global _event_store
    _event_store = store


def get_event_store() -> "Any | None":
    return _event_store


def set_current_active_run_id(run_id: str | None) -> None:
    """设置当前活跃 run_id（conversational engine 每轮 LLM.run 上下文期间）。
    设为 None 表示清空。任何同步读 `get_current_active_run_id` 的工具/handler 都能拿到父 run_id。
    """
    global _current_active_run_id
    _current_active_run_id = run_id


def get_current_active_run_id() -> str | None:
    return _current_active_run_id


# 🆕 Phase 1: Session 管理器注册/获取
def set_session_manager(mgr: "Any") -> None:
    global _session_manager
    _session_manager = mgr
    logger.info("session_manager registered: %s", type(mgr).__name__)


def get_session_manager() -> "Any | None":
    return _session_manager


# 🆕 Phase 2: 记忆管理器注册/获取
def set_memory_manager(mgr: "Any") -> None:
    global _memory_manager
    _memory_manager = mgr
    logger.info("memory_manager registered: %s", type(mgr).__name__)


def get_memory_manager() -> "Any | None":
    return _memory_manager


# 🆕 WorkflowRegistry 注册/获取
def set_workflow_registry(reg: "Any") -> None:
    global _workflow_registry
    _workflow_registry = reg
    logger.info("workflow_registry registered: %s", type(reg).__name__)


def get_workflow_registry() -> "Any | None":
    return _workflow_registry


# 🆕 Phase 2: SkillRegistry 注册/获取
def set_skill_registry(reg: "Any") -> None:
    global _skill_registry
    _skill_registry = reg
    logger.info("skill_registry registered: %s", type(reg).__name__)


def get_skill_registry() -> "Any | None":
    return _skill_registry


# P0.18.7b: ContainerProvisioner 注册/获取
def set_container_provisioner(prov: "Any") -> None:
    global _container_provisioner
    _container_provisioner = prov
    if prov is not None:
        logger.info("container_provisioner registered: %s", type(prov).__name__)
    else:
        logger.info("container_provisioner cleared")


def get_container_provisioner() -> "Any | None":
    return _container_provisioner


# 任务管理模块 TaskOrchestrator 注册/获取（防循环依赖：tools/task_*.py 不 import task.orchestrator）
def set_task_orchestrator(orch: "Any") -> None:
    global _task_orchestrator
    _task_orchestrator = orch
    logger.info("task_orchestrator registered: %s", type(orch).__name__)


def get_task_orchestrator() -> "Any | None":
    return _task_orchestrator
