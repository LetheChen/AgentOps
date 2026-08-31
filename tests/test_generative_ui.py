"""
generative_ui IR 编译器测试。
"""
import pytest
from orchestrator.generative_ui import (
    A2UI_CATALOG_ID,
    A2UI_VERSION,
    build_a2ui_ir_node,
    build_a2ui_surface,
    build_legacy_widget_content,
    legacy_widget_to_a2ui_surface,
    validate_a2ui_surface,
)


class TestBuildA2uiSurface:
    def test_基本构造(self):
        components = [{"id": "root", "component": "Text", "text": "hello"}]
        surface = build_a2ui_surface(components)
        assert surface["version"] == A2UI_VERSION
        assert surface["catalogId"] == A2UI_CATALOG_ID
        assert surface["components"] == components

    def test_空组件列表也合法(self):
        surface = build_a2ui_surface([])
        assert isinstance(surface, dict)
        assert surface["components"] == []


class TestValidateA2uiSurface:
    def test_合法surface通过(self):
        surface = build_a2ui_surface([
            {"id": "root", "component": "Column", "children": ["t1"]},
            {"id": "t1", "component": "Text", "text": "hello", "variant": "body"},
        ])
        errors = validate_a2ui_surface(surface)
        assert errors == []

    def test_非对象失败(self):
        errors = validate_a2ui_surface("not a dict")
        assert len(errors) == 1
        assert "对象" in errors[0]

    def test_版本错误(self):
        surface = {"version": "v0.1", "catalogId": A2UI_CATALOG_ID, "components": []}
        errors = validate_a2ui_surface(surface)
        assert any("version" in e for e in errors)

    def test_catalogId错误(self):
        surface = {"version": A2UI_VERSION, "catalogId": "wrong", "components": []}
        errors = validate_a2ui_surface(surface)
        assert any("catalogId" in e for e in errors)

    def test_组件id重复(self):
        surface = build_a2ui_surface([
            {"id": "dup", "component": "Text", "text": "a"},
            {"id": "dup", "component": "Text", "text": "b"},
        ])
        errors = validate_a2ui_surface(surface)
        assert any("重复" in e for e in errors)

    def test_组件id为空(self):
        surface = build_a2ui_surface([
            {"id": "", "component": "Text", "text": "a"},
        ])
        errors = validate_a2ui_surface(surface)
        assert any("id" in e for e in errors)


class TestBuildA2uiIrNode:
    def test_基本构造(self):
        surface = build_a2ui_surface([{"id": "root", "component": "Text", "text": "hi"}])
        node = build_a2ui_ir_node(surface, content={"k": "v"}, node_id="test_node")
        assert node["ir_version"] == 1
        assert node["id"] == "test_node"
        assert node["kind"] == "com.agentops.core/a2ui"
        assert node["a2ui"] == surface
        assert node["content"] == {"k": "v"}
        assert node["revision"] == 1
        assert "updated_at" in node
        assert "fallback" in node

    def test_自动生成node_id(self):
        surface = build_a2ui_surface([{"id": "root", "component": "Text", "text": "hi"}])
        node = build_a2ui_ir_node(surface)
        assert node["id"].startswith("a2ui_")

    def test_自动收集button_action(self):
        """Button 的 action.event.name 必须在 node.actions 声明。"""
        surface = build_a2ui_surface([
            {"id": "root", "component": "Column", "children": ["b1", "b1_text"]},
            {"id": "b1", "component": "Button", "child": "b1_text", "action": {"event": {"name": "submit"}}},
            {"id": "b1_text", "component": "Text", "text": "提交"},
        ])
        node = build_a2ui_ir_node(surface)
        actions = node.get("actions", [])
        assert any(a["id"] == "submit" for a in actions)

    def test_外部actions优先(self):
        surface = build_a2ui_surface([
            {"id": "b1", "component": "Button", "child": "x", "action": {"event": {"name": "submit"}}},
        ])
        external = [{"id": "submit", "label": "提交表单", "intent": "submit", "style": "primary"}]
        node = build_a2ui_ir_node(surface, actions=external)
        actions = node["actions"]
        # 外部传入的应该保留
        ext = next(a for a in actions if a["id"] == "submit")
        assert ext["label"] == "提交表单"
        # 不应该有重复
        assert sum(1 for a in actions if a["id"] == "submit") == 1


class TestLegacyWidgetConversion:
    def test_table转换(self):
        props = {
            "columns": [
                {"name": "provider", "label": "Provider"},
                {"name": "latency", "label": "延迟"},
            ],
            "rows": [
                {"provider": "minimax", "latency": 1200},
                {"provider": "deepseek", "latency": 850},
            ],
        }
        surface = legacy_widget_to_a2ui_surface("table", props)
        assert surface is not None
        comp = surface["components"][0]
        assert comp["component"] == "AoTable"
        assert comp["source"] == {"path": "/rows"}
        assert len(comp["columns"]) == 2

    def test_chart柱状图转换(self):
        props = {
            "type": "bar",
            "series": [{"data": [10, 20, 30]}],
            "categories": ["a", "b", "c"],
        }
        surface = legacy_widget_to_a2ui_surface("chart", props)
        assert surface is not None
        comp = surface["components"][0]
        assert comp["component"] == "AoBarChart"

    def test_chart折线图不转换(self):
        """折线图 a2ui 没有对应组件，返回 None 用旧渲染。"""
        props = {"type": "line", "series": [{"data": [1, 2, 3]}]}
        surface = legacy_widget_to_a2ui_surface("chart", props)
        assert surface is None

    def test_memo转换(self):
        props = {"title": "标题", "content": "第一行\n第二行"}
        surface = legacy_widget_to_a2ui_surface("memo", props)
        assert surface is not None
        root = surface["components"][0]
        assert root["component"] == "Column"
        assert "memo_title" in root["children"]
        assert "memo_line_0" in root["children"]
        assert "memo_line_1" in root["children"]

    def test_checklist转换(self):
        props = {
            "items": [
                {"title": "任务1", "status": "done"},
                {"title": "任务2", "status": "pending"},
            ]
        }
        surface = legacy_widget_to_a2ui_surface("checklist", props)
        assert surface is not None
        comp = surface["components"][0]
        assert comp["component"] == "AoList"

    def test_不支持的类型返回None(self):
        surface = legacy_widget_to_a2ui_surface("form", {"fields": []})
        assert surface is None

    def test_table转换content数据(self):
        props = {
            "columns": [{"name": "x", "label": "X"}],
            "rows": [{"x": 1}, {"x": 2}],
        }
        content = build_legacy_widget_content("table", props)
        assert content == {"rows": [{"x": 1}, {"x": 2}]}

    def test_checklist转换content数据(self):
        props = {"items": [{"title": "a", "status": "done"}]}
        content = build_legacy_widget_content("checklist", props)
        assert content == {"items": [{"title": "a", "status": "done", "detail": ""}]}

    def test_memo空内容返回None(self):
        surface = legacy_widget_to_a2ui_surface("memo", {"title": "", "content": ""})
        assert surface is None

    def test_table空数据返回None(self):
        surface = legacy_widget_to_a2ui_surface("table", {"columns": [], "rows": []})
        assert surface is None
