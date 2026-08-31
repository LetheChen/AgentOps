"""tools/collect_child_result.py 单元测试。

覆盖 `_extract_final_outputs` / `_extract_summary` 对两种 node_outputs 结构的兼容：

- **templated workflow**（如 smart-query）：node_outputs = {node_id: {port: payload}}，
  按端口组织（present_result.answer），**不存在 final_outputs 键**。
- **conversational / task**：某节点 outputs 里直接带 final_outputs 键。

背景（2026-08-29 踩坑）：原实现只在节点 outputs 里找 `final_outputs` 键，
templated workflow 一律返回 {} → manager 用 collect_child_result 永远读空，
而 runs 表 final_outputs 其实一直有完整数据。本测试锁定两种结构的提取行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect_child_result import (  # noqa: E402  · 项目惯例 sys.path.insert 后 import
    _extract_final_outputs,
    _extract_summary,
    _shape_response,
)


# ====== 测试夹具 ======

TEMPLATED_NODE_OUTPUTS = {
    "route_intent": {"intent": {"content": "true", "summary": "本周智能审核业务查询"}},
    "resolve_schema": {"success": {"cli": "python tools/db_cli.py resolve-schema"}},
    "plan_sql": {"sql": {"content": "SELECT ...", "summary": "统计各状态单量"}},
    "validate_sql": {"success": {"cli": "python tools/db_cli.py validate"}},
    "execute_query": {"success": {"cli": "python tools/db_cli.py query"}},
    "present_result": {
        "answer": {
            "content": "近 14 天共受理 114 笔单据，通过率 48.2%",
            "summary": "近 14 天业务平稳",
        }
    },
}

CONVERSATIONAL_NODE_OUTPUTS = {
    "conv:manager": {
        "final_outputs": {"answer": "对话最终产出"},
        "messages": [{"role": "user", "content": "hi"}],
        "summary": "对话摘要",
    }
}


# ====== templated workflow（核心回归） ======

def test_templated_final_outputs_returns_all_node_ports():
    """templated 无 final_outputs 键时，整个 node_outputs 即最终交付。"""
    fo = _extract_final_outputs(TEMPLATED_NODE_OUTPUTS)
    assert fo, "templated workflow 的 final_outputs 不应为空（历史 bug：返回 {}）"
    assert set(fo.keys()) == set(TEMPLATED_NODE_OUTPUTS.keys())
    # 终点节点的答案必须能取到
    assert fo["present_result"]["answer"]["content"].startswith("近 14 天共受理")


def test_templated_summary_prefers_terminal_node():
    """summary 应取终点节点（present_result），不是上游 route_intent。"""
    assert _extract_summary(TEMPLATED_NODE_OUTPUTS) == "近 14 天业务平稳"


# ====== conversational / task（回归保护） ======

def test_conversational_final_outputs_still_priority():
    """显式 final_outputs 键优先，行为不变。"""
    fo = _extract_final_outputs(CONVERSATIONAL_NODE_OUTPUTS)
    assert fo == {"answer": "对话最终产出"}


def test_conversational_summary_top_level():
    assert _extract_summary(CONVERSATIONAL_NODE_OUTPUTS) == "对话摘要"


# ====== 边界 ======

def test_empty_and_none_return_empty():
    assert _extract_final_outputs({}) == {}
    assert _extract_final_outputs(None) == {}
    assert _extract_summary({}) == ""
    assert _extract_summary(None) == ""


def test_no_summary_anywhere_returns_empty():
    node_outputs = {"a": {"out": {"content": "无 summary 字段"}}}
    assert _extract_final_outputs(node_outputs) == node_outputs
    assert _extract_summary(node_outputs) == ""


# ====== 完整响应形状 ======

def test_shape_response_carries_final_outputs():
    """_shape_response 必须把 final_outputs 透传给 manager。"""
    final = {
        "status": "completed",
        "final_outputs": _extract_final_outputs(TEMPLATED_NODE_OUTPUTS),
        "messages": [],
        "summary": _extract_summary(TEMPLATED_NODE_OUTPUTS),
        "started_at": None,
        "finished_at": None,
        "duration_ms": 45000,
        "error": None,
    }
    resp = _shape_response("run_test_0001", final, elapsed=45.0, timed_out=False)
    assert resp["status"] == "completed"
    assert resp["final_outputs"], "manager 拿到的 final_outputs 不应为空"
    assert resp["summary"] == "近 14 天业务平稳"
    assert resp["duration_ms"] == 45000
    assert resp["timed_out"] is False
    assert "status=completed" in resp["content"]
