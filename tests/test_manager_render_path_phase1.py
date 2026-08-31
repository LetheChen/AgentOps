"""阶段 1（P0）测试 — manager 渲染路径改造。

覆盖：
1. _map_progress dangling bug 修复验证
2. _validate_reachable 校验函数
3. manager_visual_templates 模板预验证

upsert_generated_view 的集成测试需 mock event_sink + profile，留到 test_surface_state.py 扩展。
"""
from __future__ import annotations

import pytest

from orchestrator.present_content import _map_progress
from orchestrator.manager_visual_templates import (
    PRE_VALIDATION_SAMPLES,
    load_template,
    pre_validate_all_templates,
)
from tools.report_surface_state import _validate_reachable


# ── 1. _map_progress dangling bug 修复验证 ──────────────


class TestMapProgressFix:
    """验证 _map_progress 的 s1 dangling bug 已修复。"""

    def test_progress_with_steps_has_s1_in_children(self):
        """steps 非空时 root.children 必须包含 s1（修复前 s1 dangling）。"""
        data = {"percent": 75, "steps": [{"title": "扫描", "detail": "完成", "status": "done"}]}
        components, content = _map_progress(data, tone="info")

        root = next(c for c in components if c["id"] == "root")
        assert "s1" in root["children"], (
            f"root.children should include 's1' when steps non-empty, got {root['children']}"
        )

    def test_progress_without_steps_no_s1(self):
        """steps 为空时 root.children 不含 s1。"""
        data = {"percent": 50}
        components, content = _map_progress(data, tone=None)

        root = next(c for c in components if c["id"] == "root")
        assert "s1" not in root["children"], (
            f"root.children should not include 's1' when steps empty, got {root['children']}"
        )
        assert root["children"] == ["p1"]

    def test_progress_with_steps_reachable(self):
        """_map_progress 输出（含 steps）通过 reachable 校验。"""
        data = {
            "percent": 75,
            "steps": [
                {"title": "扫描", "detail": "完成", "status": "done"},
                {"title": "报告", "detail": "进行中", "status": "active"},
            ],
        }
        components, content = _map_progress(data, tone="info")
        issues = _validate_reachable(components)
        assert issues == [], f"_map_progress output has unreachable components: {issues}"

    def test_progress_with_prefix_reachable(self):
        """带 prefix 的 _map_progress 输出也通过 reachable 校验。"""
        data = {"percent": 100, "steps": [{"title": "done", "detail": "all"}]}
        components, content = _map_progress(data, tone=None, prefix="sec1_")
        issues = _validate_reachable(components)
        assert issues == [], f"prefixed _map_progress output unreachable: {issues}"

    def test_progress_with_steps_has_s1_component(self):
        """steps 非空时 components 数组含 s1（AoList）组件。"""
        data = {"percent": 50, "steps": [{"title": "x"}]}
        components, _ = _map_progress(data, tone=None)
        s1 = next((c for c in components if c.get("id") == "s1"), None)
        assert s1 is not None, "s1 component should exist when steps non-empty"
        assert s1["component"] == "AoList"


# ── 2. _validate_reachable 校验函数 ─────────────────────


class TestValidateReachable:
    """验证 reachable 校验函数。"""

    def test_valid_tree_passes(self):
        """合法树（root → p1 → s1）通过。"""
        components = [
            {"id": "root", "component": "Column", "children": ["p1", "s1"]},
            {"id": "p1", "component": "AoProgress", "value": 50},
            {"id": "s1", "component": "AoList", "source": {"path": "/steps"}},
        ]
        issues = _validate_reachable(components)
        assert issues == []

    def test_dangling_component_detected(self):
        """dangling 组件（s1 未在 root.children）被检测。"""
        components = [
            {"id": "root", "component": "Column", "children": ["p1"]},  # 缺 s1
            {"id": "p1", "component": "AoProgress", "value": 50},
            {"id": "s1", "component": "AoList", "source": {"path": "/steps"}},  # dangling
        ]
        issues = _validate_reachable(components)
        assert len(issues) == 1
        assert "s1" in issues[0]
        assert "unreachable" in issues[0]

    def test_single_component_is_root(self):
        """单个组件（无 children 引用）本身就是合法 root（新 root 查找逻辑）。"""
        components = [
            {"id": "p1", "component": "AoProgress", "value": 50},
        ]
        issues = _validate_reachable(components)
        assert issues == []  # p1 不被引用 → 它就是 root，合法

    def test_empty_components_detected(self):
        """空组件列表被检测。"""
        issues = _validate_reachable([])
        assert len(issues) == 1
        assert "empty" in issues[0]

    def test_nested_children_reachable(self):
        """嵌套子组件可达。"""
        components = [
            {"id": "root", "component": "Column", "children": ["col1"]},
            {"id": "col1", "component": "Column", "children": ["t1", "t2"]},
            {"id": "t1", "component": "Text", "text": "a"},
            {"id": "t2", "component": "Text", "text": "b"},
        ]
        issues = _validate_reachable(components)
        assert issues == []

    def test_circular_reference_detected(self):
        """循环引用被检测为 no root（所有组件互相引用，无真正 root）。"""
        components = [
            {"id": "root", "component": "Column", "children": ["a"]},
            {"id": "a", "component": "Column", "children": ["root"]},  # 循环
        ]
        issues = _validate_reachable(components)
        assert len(issues) == 1
        assert "no root" in issues[0]

    def test_multiple_dangling_detected(self):
        """多个 dangling 组件都被检测。"""
        components = [
            {"id": "root", "component": "Column", "children": ["p1"]},
            {"id": "p1", "component": "AoProgress", "value": 50},
            {"id": "s1", "component": "AoList"},  # dangling
            {"id": "s2", "component": "AoList"},  # dangling
        ]
        issues = _validate_reachable(components)
        assert len(issues) == 2


# ── 3. manager_visual_templates 模板预验证 ──────────────


class TestManagerVisualTemplates:
    """验证模板生成器 + 预验证机制。"""

    def test_load_template_progress(self):
        """load_template('progress', ...) 成功生成 + reachable 通过。"""
        data = {"percent": 75, "steps": [{"title": "x", "detail": "y"}]}
        components, content = load_template("progress", data, tone="info")
        assert isinstance(components, list)
        assert len(components) >= 2  # root + p1 + s1
        # reachable 已在 load_template 内校验，到这里说明通过

    def test_load_template_table(self):
        """load_template('table', ...) 成功。"""
        data = {
            "columns": [{"id": "name", "label": "名称"}],
            "rows": [{"name": "web-01"}],
        }
        components, content = load_template("table", data)
        assert len(components) >= 1

    def test_load_template_metric_group(self):
        """load_template('metric_group', ...) 成功。"""
        data = {"metrics": [{"label": "CPU", "value": "45%"}]}
        components, content = load_template("metric_group", data)
        assert len(components) >= 2  # root + m1

    def test_load_template_unknown_rejected(self):
        """未知 content_type 报错。"""
        with pytest.raises(ValueError, match="unknown content_type"):
            load_template("nonexistent", {})

    def test_load_template_reachable_guaranteed(self):
        """所有 content_type 的 load_template 输出都通过 reachable。"""
        for content_type, sample_data in PRE_VALIDATION_SAMPLES.items():
            components, _ = load_template(content_type, sample_data, tone="info")
            # load_template 内部已校验 reachable，到这里说明通过
            issues = _validate_reachable(components)
            assert issues == [], (
                f"content_type '{content_type}' generated unreachable: {issues}"
            )

    def test_pre_validate_all_templates(self):
        """pre_validate_all_templates 全部通过（启动预验证）。"""
        results = pre_validate_all_templates()
        assert len(results) == 13, f"expected 13 content_types, got {len(results)}"

        failed = {k: v for k, v in results.items() if v.startswith("FAIL")}
        assert not failed, f"templates failed pre-validation: {failed}"

        for content_type, result in results.items():
            assert result.startswith("ok"), (
                f"{content_type}: {result}"
            )

    def test_pre_validate_covers_all_content_types(self):
        """预验证覆盖 13 种 content_type。"""
        results = pre_validate_all_templates()
        expected = {
            "metric_group", "table", "timeline", "progress", "comparison",
            "dag_flow", "disclosure_list", "bar_chart", "line_chart", "pie_chart",
            "media", "form", "dashboard",
        }
        assert set(results.keys()) == expected


# ── 4. _map_progress 回归测试（防退化） ─────────────────


class TestMapProgressRegression:
    """防止 _map_progress bug 回归。"""

    def test_regression_s1_dangling_never_happens(self):
        """回归测试：_map_progress steps 非空时 s1 必须在 root.children。

        这是 2026-08-23 修复的 dangling children bug 的回归保护。
        bug 原因：line 200 `children = [f"{prefix}p1"]` 写死，s1 加入 components
        但未加入 root.children，导致前端 A2uiRenderer 抛
        "component is unreachable from root: s1" 降级卡片。
        """
        # 多种 steps 数量都验证
        for steps_count in [1, 2, 5]:
            data = {
                "percent": 50,
                "steps": [{"title": f"step {i}"} for i in range(steps_count)],
            }
            components, _ = _map_progress(data, tone=None)

            root = next(c for c in components if c["id"] == "root")
            assert "s1" in root["children"], (
                f"regression: s1 missing from root.children with {steps_count} steps"
            )

            # reachable 校验
            issues = _validate_reachable(components)
            assert issues == [], (
                f"regression: unreachable with {steps_count} steps: {issues}"
            )
