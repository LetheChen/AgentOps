"""
生成式 UI IR 编译器（Python 端）。

职责：
1. 构造合法 A2UI surface（agent 用这个避免手写出错）
2. 基本结构校验（完整 JSON Schema 校验由前端 Ajv 负责）
3. 旧 13 类 widget → a2ui surface 转换（让旧 widget 也能用 A2uiRenderer 渲染）
4. 构造 IR node 结构

设计原则（Karpathy）：
- 最小代码解决问题，不写投机抽象
- 完整 schema 校验交给前端 Ajv（已有），Python 端只做基本结构校验
- 旧 widget 转换只做最常用的 4 类（table/chart/memo/checklist），其他按需补

参考：
- AgentOps agentops_manager/src/generative-ui/legacy-widget-compiler.ts
- AgentOps web/src/lib/a2ui/a2ui.ts（协议层）
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# A2UI 协议常量（与前端 web/src/lib/a2ui/a2ui.ts 保持一致）
A2UI_CATALOG_ID = "https://agentops.dev/a2ui/catalogs/core/v1"
A2UI_VERSION = "v1.0"
GENERATIVE_UI_IR_VERSION = 1


def build_a2ui_surface(components: list[dict[str, Any]]) -> dict[str, Any]:
    """
    构造合法 A2UI surface。

    Args:
        components: 组件列表，每个组件必须有 id 和 component 字段

    Returns:
        合法的 AgentopsA2uiSurfaceV1 字典
    """
    return {
        "version": A2UI_VERSION,
        "catalogId": A2UI_CATALOG_ID,
        "components": components,
    }


def validate_a2ui_surface(surface: Any) -> list[str]:
    """
    基本结构校验（完整 JSON Schema 校验由前端 Ajv 负责）。

    Args:
        surface: 待校验的 surface 对象

    Returns:
        错误信息列表，空列表表示通过
    """
    errors: list[str] = []
    if not isinstance(surface, dict):
        return ["surface 必须是对象"]

    if surface.get("version") != A2UI_VERSION:
        errors.append(f"version 必须是 {A2UI_VERSION}，实际为 {surface.get('version')}")

    if surface.get("catalogId") != A2UI_CATALOG_ID:
        errors.append(f"catalogId 必须是 {A2UI_CATALOG_ID}，实际为 {surface.get('catalogId')}")

    components = surface.get("components")
    if not isinstance(components, list):
        errors.append("components 必须是数组")
        return errors

    if len(components) == 0:
        errors.append("components 不能为空")
        return errors

    # 检查每个 component 的基本结构
    seen_ids: set[str] = set()
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            errors.append(f"components[{i}] 必须是对象")
            continue
        comp_id = comp.get("id")
        if not isinstance(comp_id, str) or not comp_id:
            errors.append(f"components[{i}].id 必须是非空字符串")
            continue
        if comp_id in seen_ids:
            errors.append(f"components[{i}].id 重复：{comp_id}")
            continue
        seen_ids.add(comp_id)

        comp_type = comp.get("component")
        if not isinstance(comp_type, str) or not comp_type:
            errors.append(f"components[{i}].component 必须是非空字符串")

    return errors


def build_a2ui_ir_node(
    surface: dict[str, Any],
    content: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    node_id: str | None = None,
    kind: str = "com.agentops.core/a2ui",
    title: str = "A2UI Surface",
    summary: str | None = None,
) -> dict[str, Any]:
    """
    构造 GenerativeUiStoredNodeV1 IR 节点。

    Args:
        surface: A2UI surface 对象
        content: 数据模型（source path 引用的数据源）
        actions: action 声明列表（Button 引用的 event.name 必须在此声明）
        node_id: 节点 ID（默认自动生成）
        kind: 语义类型（默认 com.agentops.core/a2ui）
        title: fallback 标题
        summary: fallback 摘要

    Returns:
        GenerativeUiStoredNodeV1 字典
    """
    # 自动扫描 surface 收集 Button.action.event.name 合成 actions 声明
    # （与前端 A2uiWidget.tsx 逻辑一致，避免 schema 校验失败）
    collected_actions: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for comp in surface.get("components", []):
        if not isinstance(comp, dict):
            continue
        if comp.get("component") == "Button" and isinstance(comp.get("action"), dict):
            action = comp["action"]
            event = action.get("event")
            if isinstance(event, dict) and isinstance(event.get("name"), str):
                name = event["name"]
                if name not in seen_names:
                    seen_names.add(name)
                    collected_actions.append({
                        "id": name,
                        "label": name,
                        "intent": name,
                        "style": "primary",
                    })

    # 合并外部传入的 actions（覆盖同名 collected）
    external_actions = actions or []
    external_names = {a.get("id") for a in external_actions if isinstance(a, dict)}
    merged_actions = [
        *external_actions,
        *filter(lambda a: a["id"] not in external_names, collected_actions),
    ]

    node_id = node_id or f"a2ui_{uuid.uuid4().hex[:12]}"
    return {
        "ir_version": GENERATIVE_UI_IR_VERSION,
        "id": node_id,
        "kind": kind,
        "kind_version": 1,
        "owner": {"id": "com.agentops.core", "version": "1.0.0"},
        "surface": "result",
        "importance": "primary",
        "content": content or {},
        "a2ui": surface,
        "fallback": {
            "title": title,
            **({"summary": summary} if summary else {}),
            "items": [],
        },
        **({"actions": merged_actions} if merged_actions else {}),
        "revision": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 旧 widget → a2ui surface 转换 ──
# 只做最常用的 4 类，其他按需补


def legacy_widget_to_a2ui_surface(
    widget_type: str,
    props: dict[str, Any],
) -> dict[str, Any] | None:
    """
    把旧 13 类 widget props 转换为 a2ui surface。

    当前支持：table / chart / memo / checklist
    其他类型返回 None（前端继续用旧 WidgetRenderer 渲染）。

    Args:
        widget_type: widget 类型（table/chart/memo/checklist/...）
        props: widget props

    Returns:
        a2ui surface 字典，或不支持时返回 None
    """
    converter = _LEGACY_CONVERTERS.get(widget_type)
    if converter is None:
        return None
    try:
        components = converter(props)
        if not components:
            return None
        return build_a2ui_surface(components)
    except Exception as e:
        logger.warning("legacy_widget_to_a2ui_surface 转换失败 %s: %s", widget_type, e)
        return None


def _convert_table(props: dict[str, Any]) -> list[dict[str, Any]]:
    """table widget → AoTable surface."""
    columns_raw = props.get("columns") or []
    rows_raw = props.get("rows") or props.get("data") or []

    # 兼容 columns 是 [{name/label, key/dataIndex}] 两种格式
    columns: list[dict[str, Any]] = []
    for i, col in enumerate(columns_raw):
        if isinstance(col, str):
            columns.append({
                "id": f"col_{i}",
                "label": col,
                "path": f"/{col}",
                "format": "text",
            })
        elif isinstance(col, dict):
            col_id = col.get("id") or col.get("key") or col.get("name") or f"col_{i}"
            label = col.get("label") or col.get("title") or col.get("name") or col_id
            key = col.get("dataIndex") or col.get("key") or col.get("name") or col_id
            columns.append({
                "id": str(col_id),
                "label": str(label),
                "path": f"/{key}",
                "format": "text",
            })

    if not columns or not rows_raw:
        return []

    return [{
        "id": "root",
        "component": "AoTable",
        "source": {"path": "/rows"},
        "columns": columns,
    }]


def _convert_chart(props: dict[str, Any]) -> list[dict[str, Any]]:
    """chart widget → AoBarChart surface（折线图/饼图暂不转换，返回 None 用旧渲染）。"""
    chart_type = props.get("type") or props.get("chart_type")
    # 只转换柱状图，折线图/饼图保留旧渲染（a2ui 没有对应组件）
    if chart_type not in ("bar", "column"):
        return []

    series = props.get("series") or []
    if not series:
        return []

    # 取第一个 series 的数据作为 items
    first_series = series[0] if isinstance(series[0], dict) else {"data": series}
    data = first_series.get("data") or []
    if not data:
        return []

    # 构造 items：[{label, value}]
    items: list[dict[str, Any]] = []
    categories = props.get("categories") or props.get("labels") or []
    for i, value in enumerate(data):
        label = categories[i] if i < len(categories) else f"项{i + 1}"
        items.append({"label": str(label), "value": value})

    return [{
        "id": "root",
        "component": "AoBarChart",
        "source": {"path": "/items"},
        "itemLabelPath": "/label",
        "itemValuePath": "/value",
    }]


def _convert_memo(props: dict[str, Any]) -> list[dict[str, Any]]:
    """memo widget → Column + Text surface."""
    title = props.get("title")
    content = props.get("content") or props.get("text") or ""

    components: list[dict[str, Any]] = []
    children: list[str] = []

    if title:
        components.append({
            "id": "memo_title",
            "component": "Text",
            "text": str(title),
            "variant": "caption",
        })
        children.append("memo_title")

    # content 可能是多段文字
    if isinstance(content, str):
        content_lines = content.split("\n")
    elif isinstance(content, list):
        content_lines = [str(c) for c in content]
    else:
        content_lines = [str(content)]

    for i, line in enumerate(content_lines):
        if not line.strip():
            continue
        comp_id = f"memo_line_{i}"
        components.append({
            "id": comp_id,
            "component": "Text",
            "text": line,
            "variant": "body",
        })
        children.append(comp_id)

    if not children:
        return []

    components.insert(0, {
        "id": "root",
        "component": "Column",
        "children": children,
    })
    return components


def _convert_checklist(props: dict[str, Any]) -> list[dict[str, Any]]:
    """checklist widget → AoList surface。"""
    items_raw = props.get("items") or []
    if not items_raw:
        return []

    items: list[dict[str, Any]] = []
    for i, item in enumerate(items_raw):
        if isinstance(item, dict):
            title = item.get("title") or item.get("label") or item.get("text") or f"项{i + 1}"
            status = item.get("status") or item.get("checked") and "done" or "pending"
            detail = item.get("detail") or item.get("description") or ""
        else:
            title = str(item)
            status = "pending"
            detail = ""
        items.append({
            "title": str(title),
            "status": str(status),
            "detail": str(detail),
        })

    return [{
        "id": "root",
        "component": "AoList",
        "source": {"path": "/items"},
        "itemTitlePath": "/title",
        "itemDetailPath": "/detail",
        "itemStatusPath": "/status",
    }]


# 转换器注册表
_LEGACY_CONVERTERS = {
    "table": _convert_table,
    "chart": _convert_chart,
    "memo": _convert_memo,
    "checklist": _convert_checklist,
}


def build_legacy_widget_content(
    widget_type: str,
    props: dict[str, Any],
) -> dict[str, Any]:
    """
    为旧 widget 转换的 a2ui surface 构造 content 数据模型。

    AoTable/AoList/AoBarChart 的 source.path 引用的数据放在 content 里。
    """
    if widget_type == "table":
        rows = props.get("rows") or props.get("data") or []
        return {"rows": rows}
    if widget_type == "chart":
        series = props.get("series") or []
        first_series = series[0] if series else {}
        data = first_series.get("data") if isinstance(first_series, dict) else first_series
        categories = props.get("categories") or props.get("labels") or []
        items = []
        if isinstance(data, list):
            for i, value in enumerate(data):
                label = categories[i] if i < len(categories) else f"项{i + 1}"
                items.append({"label": str(label), "value": value})
        return {"items": items}
    if widget_type == "checklist":
        items_raw = props.get("items") or []
        items = []
        for i, item in enumerate(items_raw):
            if isinstance(item, dict):
                title = item.get("title") or item.get("label") or item.get("text") or f"项{i + 1}"
                status = item.get("status") or ("done" if item.get("checked") else "pending")
                detail = item.get("detail") or item.get("description") or ""
            else:
                title = str(item)
                status = "pending"
                detail = ""
            items.append({"title": str(title), "status": str(status), "detail": str(detail)})
        return {"items": items}
    return {}
