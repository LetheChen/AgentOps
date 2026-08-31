"""M3 协作可视化辅助函数（纯函数，无外部依赖，独立可测试）。

设计依据：[PRD §9.1 M1 详细拆解](file:///e:/Project/AgentOps/docs/product-design/PRD_collaboration_visualization_M3.md#91-m1-详细拆解)

本模块故意保持极简——不 import orchestrator / audit / harness，避免循环依赖。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow.schema import WorkflowNode


def resolve_business_role(node: "WorkflowNode | None", fallback_id: str = "") -> str:
    """M3 §3.1 业务角色解析链：node.business_role → node.agent → fallback_id。

    用于 NODE_HANDOFF payload 的 from_role/to_role 字段。
    兜底保证永不返回空字符串（前端气泡头部必填）。

    优先级：
      1. node.business_role（node yaml 显式声明，最优先）
      2. node.agent（agent ID，业务上代表"谁在执行"）
      3. fallback_id（通常是 node_id）
      4. "unknown"（极端兜底）

    >>> resolve_business_role(WorkflowNode(id="scan", agent="log_analyst",
    ...     business_role="数据采集员"))
    '数据采集员'
    >>> resolve_business_role(WorkflowNode(id="scan", agent="log_analyst"))
    'log_analyst'
    >>> resolve_business_role(None, fallback_id="scan")
    'scan'
    """
    if node is None:
        return fallback_id or "unknown"
    return node.business_role or node.agent or fallback_id or "unknown"


def gen_handoff_summary(port: str, payload: Any, target: str = "") -> str:
    """M3 §4.3 handoff summary 兜底生成（3 级优先级）。

    1. payload["summary"]（agent emit 时填，M2 升级路径）
    2. payload["content"] 截断 120 字（兜底有人话时）
    3. port + target（机械兜底，保证永不空白）

    >>> gen_handoff_summary("scan_result", {"summary": "扫了 24h 日志"})
    '扫了 24h 日志'
    >>> gen_handoff_summary("scan_result", {"content": "找到 3 个 P0 异常..."})
    '找到 3 个 P0 异常...'
    >>> gen_handoff_summary("scan_result", {}, target="analyze")
    'scan_result → analyze'
    """
    payload_summary = ""
    if isinstance(payload, dict):
        payload_summary = str(payload.get("summary", ""))[:200]
    if not payload_summary and isinstance(payload, dict):
        content = str(payload.get("content", ""))[:120]
        if content:
            payload_summary = content
    if not payload_summary:
        payload_summary = f"{port} → {target}" if target else port
    return payload_summary