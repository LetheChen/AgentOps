"""M3 协作可视化辅助函数 + business_role 字段测试。

覆盖 M3 §3.1（business_role 解析链）+ M3 §4.3（handoff summary 兜底生成）：
- WorkflowNode.business_role 字段（默认值 + 可设置）
- resolve_business_role 解析链 4 级优先级
- gen_handoff_summary 兜底生成 3 级优先级
- log-patrol.yaml 端到端加载验证

设计依据：[PRD §9.1 M1 详细拆解](file:///e:/Project/AgentOps/docs/product-design/PRD_collaboration_visualization_M3.md#91-m1-详细拆解)
"""
from __future__ import annotations

from pathlib import Path

from workflow.collaboration import gen_handoff_summary, resolve_business_role
from workflow.loader import load_workflow_yaml
from workflow.schema import NodeType, WorkflowNode


# ============================================================
# 1. WorkflowNode.business_role 字段
# ============================================================

def test_workflow_node_business_role_default_none():
    """WorkflowNode.business_role 字段默认值是 None（向后兼容老 yaml）。"""
    node = WorkflowNode(id="x", name="X", type=NodeType.AGENT, agent="some_agent")
    assert node.business_role is None


def test_workflow_node_business_role_settable():
    """WorkflowNode.business_role 字段可显式设置。"""
    node = WorkflowNode(
        id="scan",
        name="扫描",
        type=NodeType.AGENT,
        agent="log_analyst",
        business_role="数据采集员",
    )
    assert node.business_role == "数据采集员"


# ============================================================
# 2. resolve_business_role 解析链 4 级优先级
# ============================================================

def test_resolve_business_role_prefers_node_field():
    """优先级 1：node.business_role 显式声明（最高优先）。"""
    node = WorkflowNode(
        id="scan", name="扫描", type=NodeType.AGENT,
        agent="log_analyst", business_role="数据采集员",
    )
    assert resolve_business_role(node, fallback_id="scan") == "数据采集员"


def test_resolve_business_role_fallback_to_agent():
    """优先级 2：node.business_role 缺省时回落到 node.agent。"""
    node = WorkflowNode(
        id="scan", name="扫描", type=NodeType.AGENT,
        agent="log_analyst", business_role=None,
    )
    assert resolve_business_role(node, fallback_id="scan") == "log_analyst"


def test_resolve_business_role_fallback_to_node_id():
    """优先级 3：node.business_role 和 node.agent 都缺省时回落到 fallback_id（node_id）。"""
    node = WorkflowNode(
        id="scan", name="扫描", type=NodeType.AGENT,
        agent=None, business_role=None,
    )
    assert resolve_business_role(node, fallback_id="scan") == "scan"


def test_resolve_business_role_unknown_when_all_empty():
    """优先级 4：所有 fallback 都缺省时返回 "unknown"（前端气泡头部必填字段）。"""
    node = WorkflowNode(
        id="x", name="X", type=NodeType.AGENT,
        agent="", business_role=None,
    )
    assert resolve_business_role(node, fallback_id="") == "unknown"


def test_resolve_business_role_none_node():
    """None 节点也能解析（用于 target_node 不存在的场景）。"""
    assert resolve_business_role(None, fallback_id="missing_target") == "missing_target"
    assert resolve_business_role(None, fallback_id="") == "unknown"


# ============================================================
# 3. gen_handoff_summary 兜底生成 3 级优先级
# ============================================================

def test_gen_summary_priority_1_agent_summary():
    """优先级 1：payload.summary（agent emit 时填的，M2 升级路径）。"""
    summary = gen_handoff_summary(
        "scan_result",
        {"summary": "扫描 ERROR 级日志，窗口 24h"},
    )
    assert summary == "扫描 ERROR 级日志，窗口 24h"


def test_gen_summary_priority_2_content():
    """优先级 2：payload.summary 缺省时回落到 payload.content 截断 120 字。"""
    long_content = "x" * 200
    summary = gen_handoff_summary("scan_result", {"content": long_content})
    assert summary == "x" * 120


def test_gen_summary_priority_3_port_target():
    """优先级 3：summary 和 content 都缺省时，机械兜底用 port + target。"""
    summary = gen_handoff_summary("scan_result", {}, target="analyze")
    assert summary == "scan_result → analyze"


def test_gen_summary_priority_3_port_only():
    """优先级 3 退化：没有 target 时只用 port。"""
    summary = gen_handoff_summary("scan_result", {})
    assert summary == "scan_result"


def test_gen_summary_truncates_long_agent_summary():
    """agent 填的 summary 超过 200 字自动截断（防止气泡溢出）。"""
    long_summary = "a" * 500
    summary = gen_handoff_summary("p", {"summary": long_summary})
    assert len(summary) == 200
    assert summary == "a" * 200


def test_gen_summary_non_dict_payload():
    """非 dict payload（如 str/int）走 port 兜底，不报错。"""
    assert gen_handoff_summary("port_a", "string payload") == "port_a"
    assert gen_handoff_summary("port_a", 42, target="x") == "port_a → x"


def test_gen_summary_empty_dict_empty_content():
    """空 dict + 空 content 走 port 兜底。"""
    summary = gen_handoff_summary("scan_result", {"summary": "", "content": ""})
    assert summary == "scan_result"


# ============================================================
# 4. log-patrol.yaml 端到端加载验证
# ============================================================

def test_log_patrol_yaml_has_business_role():
    """log-patrol.yaml 4 个 node 全部声明 business_role（手动 trigger 验证）。"""
    yaml_path = Path(__file__).parent.parent / "workflows" / "log-patrol.yaml"
    wf = load_workflow_yaml(yaml_path)

    assert wf.workflow_id == "log-patrol"
    assert len(wf.nodes) == 4

    # 4 个 node 各自的业务角色（来自 log-patrol.yaml 当前值）
    expected_roles = {
        "scan": "数据采集员",
        "analyze": "异常分析员",
        "report": "报告撰写员",
        "notify": "告警通知员",
    }
    for node_id, expected_role in expected_roles.items():
        assert wf.nodes[node_id].business_role == expected_role, (
            f"node {node_id} business_role 应为 '{expected_role}'"
            f"，实际 '{wf.nodes[node_id].business_role}'"
        )


def test_resolve_business_role_real_workflow():
    """用真实 log-patrol workflow 验证解析链：业务角色名直接来自 yaml。"""
    yaml_path = Path(__file__).parent.parent / "workflows" / "log-patrol.yaml"
    wf = load_workflow_yaml(yaml_path)

    # 业务角色名必须等于 yaml 中的声明（不回落 agent_id）
    for node_id, node in wf.nodes.items():
        role = resolve_business_role(node, fallback_id=node_id)
        # log-patrol.yaml 中每个 node 都显式声明了 business_role
        # 所以 role 应等于 node.business_role（不回落）
        assert role == node.business_role, (
            f"{node_id}: resolve 应返回 '{node.business_role}'，实 '{role}'"
        )