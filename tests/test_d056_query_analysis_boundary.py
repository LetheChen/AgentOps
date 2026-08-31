"""D-056 回归测试：smart_query vs smart_analysis 边界（消除「重叠」）。

背景：
- v88 前 smart_analysis.yaml 允许 sql_query、description 写「数据分析/趋势预测/报告生成」，
  system_prompt 也没声明「数据从哪来 / 不做什么」。Manager LLM 路由时把 DB 查询任务
  派给 smart_analysis 也是合法选项 → 用户感知「两个 agent 都能查 DB」→ 体验重叠。
- v88 按 DESIGN_db_connection_smart_query_v1.md 收紧：
  - smart_analysis.allowed_tools 移除 sql_query；system_prompt 显式声明数据来源 + 不适用项
  - smart_query.allowed_tools/denied_tools 补齐（之前 only [data_analysis]，等于啥也没限）
  - manager.yaml 路由规则明确 smart_query 走「统计/数量/单据状态/审批进度/个体维度」，
    smart_analysis 走「趋势/对比/归因/预测/解读/报告」

本测试断言 yaml 文本层边界（不实际跑 LLM），覆盖：
1. smart_query 的 sql_query 在 allowed、不在 denied
2. smart_query 的 sql_execute 在 denied（写操作永远禁）
3. smart_analysis 的 sql_query 在 denied（关键边界，绝不能直接查 DB）
4. smart_analysis 的 sql_query 不在 allowed（关键边界，绝不能直接查 DB）
5. smart_analysis 的 request_cross_domain 在 allowed（中转给 smart_query 的能力必须保留）
6. 两个 agent 的 harness 不串台（smart_query=codex, smart_analysis=local_llm）
7. 两个 agent 的 system_prompt 都显式声明对方是兜底/上游
8. manager.yaml 路由规则包含两个 agent 的清晰分工关键词
"""
from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent


def _load_yaml(filename: str) -> dict:
    """Load agent yaml via PyYAML (handles nested permissions/allowed_tools correctly)."""
    p = PROJECT_ROOT / "config" / "agents" / filename
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_yaml_text(filename: str) -> str:
    p = PROJECT_ROOT / "config" / "agents" / filename
    return p.read_text(encoding="utf-8")


def _allowed(yaml_dict: dict) -> list[str]:
    return list(yaml_dict.get("permissions", {}).get("allowed_tools") or [])


def _denied(yaml_dict: dict) -> list[str]:
    return list(yaml_dict.get("permissions", {}).get("denied_tools") or [])


# ───────────────────────────────────────────────────────────
# 1. smart_query：sql_query 必须在 allowed；sql_execute 必须在 denied
# ───────────────────────────────────────────────────────────

def test_smart_query_has_sql_query_allowed():
    cfg = _load_yaml("smart_query.yaml")
    allowed = _allowed(cfg)
    assert "sql_query" in allowed, (
        f"smart_query 必须允许 sql_query（直连 mysql:audit_reader 的核心能力），"
        f"实际 allowed_tools={allowed}"
    )


def test_smart_query_denies_sql_execute():
    cfg = _load_yaml("smart_query.yaml")
    denied = _denied(cfg)
    assert "sql_execute" in denied, (
        f"smart_query 是只读查询，sql_execute（写操作）必须 denied，"
        f"实际 denied_tools={denied}"
    )


def test_smart_query_denies_approval_and_ops():
    """防御性：审批/运维类工具永远不该给智能问数用。"""
    cfg = _load_yaml("smart_query.yaml")
    denied = _denied(cfg)
    for t in ("approval_flow", "submit_form", "ssh_exec", "server_restart", "db_migrate"):
        assert t in denied, f"smart_query 必须 deny {t}，实际 denied_tools={denied}"


# ───────────────────────────────────────────────────────────
# 2. smart_analysis：sql_query 必须在 denied 且不在 allowed（关键边界）
# ───────────────────────────────────────────────────────────

def test_smart_analysis_sql_query_not_allowed():
    """D-056 关键边界：smart_analysis 不直接查 DB。"""
    cfg = _load_yaml("smart_analysis.yaml")
    allowed = _allowed(cfg)
    assert "sql_query" not in allowed, (
        f"D-056 边界失效：smart_analysis.allowed_tools 含 sql_query，"
        f"会让 manager LLM 把 DB 查询派给分析 agent，破坏 v1 设计分工。"
        f"实际 allowed_tools={allowed}"
    )


def test_smart_analysis_sql_query_denied():
    cfg = _load_yaml("smart_analysis.yaml")
    denied = _denied(cfg)
    assert "sql_query" in denied, (
        f"D-056 边界失效：smart_analysis.denied_tools 缺 sql_query，"
        f"无法从 deny 端防御（白名单兜底）。实际 denied_tools={denied}"
    )


def test_smart_analysis_keeps_cross_domain():
    """smart_analysis 必须保留 request_cross_domain，否则跨域拿数据能力丧失。"""
    cfg = _load_yaml("smart_analysis.yaml")
    allowed = _allowed(cfg)
    assert "request_cross_domain" in allowed, (
        f"smart_analysis.allowed_tools 必须含 request_cross_domain（向 smart_query 拿数据），"
        f"实际 allowed_tools={allowed}"
    )


# ───────────────────────────────────────────────────────────
# 3. 两个 agent 的 harness / description 不串台
# ───────────────────────────────────────────────────────────

def test_smart_query_harness_is_codex():
    """v1 设计：smart_query 用 codex harness（独立进程）。"""
    cfg = _load_yaml("smart_query.yaml")
    assert cfg.get("harness") == "codex", (
        f"v1 设计要求 smart_query 用 codex harness，实际={cfg.get('harness')}"
    )


def test_smart_analysis_harness_is_local_llm():
    cfg = _load_yaml("smart_analysis.yaml")
    assert cfg.get("harness") == "local_llm", (
        f"smart_analysis 应保持 local_llm harness（轻量 + 共享 manager 进程），"
        f"实际={cfg.get('harness')}"
    )


# ───────────────────────────────────────────────────────────
# 4. system_prompt 显式声明对方是兜底/上游（双向引用）
# ───────────────────────────────────────────────────────────

def test_smart_query_system_prompt_references_analysis_as_fallback():
    cfg = _load_yaml("smart_query.yaml")
    prompt = cfg.get("system_prompt", "") or ""
    assert "smart_analysis" in prompt, (
        "smart_query.system_prompt 必须显式说『复杂报表/BI → smart_analysis』，"
        "给 LLM 明确兜底，避免 smart_query 越界做分析。"
    )


def test_smart_analysis_system_prompt_references_query_as_upstream():
    """D-056 关键：smart_analysis 必须显式说『DB 查询经 request_cross_domain 中转给 smart_query』。"""
    cfg = _load_yaml("smart_analysis.yaml")
    prompt = cfg.get("system_prompt", "") or ""
    assert "smart_query" in prompt, (
        "smart_analysis.system_prompt 必须显式引用 smart_query 是上游 DB 查询来源，"
        "否则 LLM 会以为分析 agent 自己就能查 DB。"
    )
    assert "request_cross_domain" in prompt, (
        "smart_analysis.system_prompt 必须说明跨域走 request_cross_domain。"
    )


# ───────────────────────────────────────────────────────────
# 5. manager.yaml 路由规则必须明确两个 agent 的分工
# ───────────────────────────────────────────────────────────

def test_manager_routing_distinguishes_query_vs_analysis():
    txt = _load_yaml_text("manager.yaml")
    # 路由段必须同时点名两个 agent 并说清差异
    assert "smart_query" in txt, "manager.yaml 必须提 smart_query 路由"
    assert "smart_analysis" in txt, "manager.yaml 必须提 smart_analysis 路由"
    # 关键词：smart_query 必须挂在「数据查询/统计」上下文附近
    # 关键词：smart_analysis 必须挂在「趋势/分析」上下文附近
    assert "趋势" in txt or "对比" in txt or "预测" in txt, (
        "manager 路由规则必须含分析类关键词（趋势/对比/预测），"
        "否则 LLM 没法判断何时派 smart_analysis。"
    )
    assert "统计" in txt or "数量" in txt or "单据" in txt or "审批" in txt, (
        "manager 路由规则必须含问数类关键词（统计/数量/单据/审批），"
        "否则 LLM 没法判断何时派 smart_query。"
    )