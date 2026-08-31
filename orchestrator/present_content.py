"""present_content 高层语义展示工具。

Agent 不直接接触 A2UI 协议，只输出 content_type + data，
由本工具内部映射为 A2UI surface 组件并校验推送。

设计文档：docs/concepts/present-content-tool-design.md（v4）
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from orchestrator.present_content_schemas import CONTENT_TYPES, CONTENT_TYPE_SCHEMAS, TONE_ENUM

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

A2UI_VERSION = "v1.0"
A2UI_CATALOG_ID = "https://agentops.dev/a2ui/catalogs/core/v1"

MAX_COMPONENTS = 128
MAX_CHILDREN = 24
MAX_SOURCE_ITEMS = 50
MAX_DEPTH = 8
MAX_BYTES = 64 * 1024
MAX_DASHBOARD_PANELS = 12

# 防注入检测规则
_INJECTION_PATTERNS = [
    re.compile(r"<script", re.IGNORECASE),
    re.compile(r"<iframe", re.IGNORECASE),
    re.compile(r"onerror\s*=", re.IGNORECASE),
    re.compile(r"onload\s*=", re.IGNORECASE),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"eval\s*\(", re.IGNORECASE),
    re.compile(r"Function\s*\(", re.IGNORECASE),
]
_PROTOTYPE_PATTERNS = re.compile(r"__proto__|prototype|constructor", re.IGNORECASE)
_POINTER_BAD = re.compile(r"\.\.|//")
_ALLOWED_URL_SCHEMES = ("http://", "https://", "data:")

# present_content 可覆盖的 root 组件类型
_COVERED_ROOTS = {
    "AoGrid", "AoTable", "AoTimeline", "Column", "Row",
    "AoDag", "AoBarChart", "AoLineChart", "AoPieChart",
}
# present_content 生成的 ID 模式
_AO_ID_PATTERN = re.compile(
    r"^(root|(sec\d+_)?(m|t|tl|p|s|d|c|md|f|fld|btn|btn_text|btn_row|lc|rc|lt|rt|li|ri|sec)\d+(_\w+)?)$"
)


# ── 13 种 content_type 映射函数 ──────────────────────
# 每个函数返回 (components: list[dict], content: dict)


def _map_metric_group(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    metrics = data["metrics"]
    n = len(metrics)
    cols = {"default": min(n, 3), "compact": 1}
    child_ids = [f"{prefix}m{i+1}" for i in range(n)]
    components = [
        {"id": f"{prefix}root", "component": "AoGrid", "children": child_ids, "columns": cols},
    ]
    for i, m in enumerate(metrics):
        comp = {"id": f"{prefix}m{i+1}", "component": "AoMetric",
                "label": m["label"], "value": str(m["value"])}
        if "unit" in m:
            comp["unit"] = m["unit"]
        comp["tone"] = m.get("tone", "neutral")
        components.append(comp)
    return components, {}


def _pick(d: Any, keys: list[str], default: str = "") -> str:
    """从 dict 中按优先顺序取第一个存在的字符串值（LLM 常用字段名容错）。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v is not None:
            return str(v)
    return default


_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _slugify(raw: str, index: int) -> str:
    """把任意列名转为合法 A2UI identifier（前端 identifier pattern: ^[A-Za-z0-9][A-Za-z0-9._:-]*$）。

    LLM 常用中文/含空格的列名（"显示名称"、"harness 类型"），直接作 id 会触发
    AJV oneOf 校验失败导致整个 surface 降级。这里做 ASCII 安全化：
    - 保留已有合法 ASCII 段
    - 非法字符替换为下划线
    - 不以字母/数字开头时加 'c' 前缀
    - 空串或纯非 ASCII 时用 col{index} 兜底
    """
    s = str(raw).strip()
    if not s:
        return f"col{index}"
    cleaned = re.sub(r"[^A-Za-z0-9._:-]", "_", s)
    if not cleaned or cleaned[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789":
        cleaned = f"c{cleaned}" if cleaned else f"col{index}"
    if not _IDENT_RE.match(cleaned):
        cleaned = f"col{index}"
    return cleaned


def _map_table(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    """构造 AoTable component + 规范化 rows。

    关键约束：
    - column.id 必须符合 A2UI identifier pattern（^[A-Za-z0-9][A-Za-z0-9._:-]*$），
      否则前端 oneOf 校验失败导致整个 surface 降级。用 _slugify 保证。
    - column.path 是 JSON Pointer，前端 readA2uiItemPointer 用它从 row 取值：
        * row 是 dict → path 段必须是 dict 的 key
        * row 是 list → path 段必须是数字索引
      所以 path 必须和 row 的实际结构对齐，不能盲目用 slug 化的列名。
    - LLM 可能传：columns=字符串数组（中文列名），rows=数组行 or 对象行，
      任意组合都要正确映射。
    """
    columns: list[dict] = []
    col_labels: list[str] = []
    for idx, col in enumerate(data["columns"]):
        if isinstance(col, str):
            col_label = col
        elif isinstance(col, dict):
            col_label = _pick(col, ["label", "title", "header", "text", "name", "id", "key", "field", "data"])
            if not col_label:
                col_label = f"col{idx}"
        else:
            raise ValueError(f"column must be dict or str, got {type(col).__name__}")
        col_id = _slugify(col_label, idx)
        if col_id in [c["id"] for c in columns]:
            col_id = f"{col_id}_{idx}"
        c: dict = {"id": col_id, "label": col_label}
        if isinstance(col, dict):
            fmt = _pick(col, ["format", "type", "fmt"])
            if fmt:
                c["format"] = fmt
        columns.append(c)
        col_labels.append(col_id)

    raw_rows = data["rows"]
    object_rows: list[dict] = []
    # 判断行结构：对象行用 row key 作 path，数组行用 column slug id 作 key
    sample = raw_rows[0] if raw_rows else None
    if isinstance(sample, dict):
        row_keys = list(sample.keys())
        # column.path 用对象行实际 key（按列顺序对齐）
        for i, c in enumerate(columns):
            key = row_keys[i] if i < len(row_keys) else col_labels[i]
            c["path"] = f"/{key}"
        for row in raw_rows:
            if isinstance(row, dict):
                object_rows.append(row)
            elif isinstance(row, list):
                object_rows.append({
                    col_labels[i]: row[i] if i < len(row) else None
                    for i in range(len(col_labels))
                })
            else:
                object_rows.append({col_labels[0]: row} if col_labels else {"value": row})
    else:
        # 数组行：path 用 slug id，并把数组行转成对象行
        for c, slug in zip(columns, col_labels):
            c["path"] = f"/{slug}"
        for row in raw_rows:
            if isinstance(row, list):
                object_rows.append({
                    col_labels[i]: row[i] if i < len(row) else None
                    for i in range(len(col_labels))
                })
            elif isinstance(row, dict):
                object_rows.append(row)
            else:
                object_rows.append({col_labels[0]: row} if col_labels else {"value": row})

    components = [{
        "id": f"{prefix}root", "component": "AoTable",
        "source": {"path": f"/{prefix}rows"}, "columns": columns,
    }]
    return components, {f"{prefix}rows": object_rows}


def _map_timeline(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    components = [{
        "id": f"{prefix}root", "component": "AoTimeline",
        "source": {"path": f"/{prefix}events"},
        "itemTimePath": "/time", "itemTitlePath": "/title", "itemDetailPath": "/detail",
    }]
    return components, {f"{prefix}events": data["events"]}


def _map_progress(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    steps = data.get("steps", [])
    children = [f"{prefix}p1"] + ([f"{prefix}s1"] if steps else [])
    components = [{"id": f"{prefix}root", "component": "Column", "children": children}]
    p1 = {"id": f"{prefix}p1", "component": "AoProgress",
          "label": "总进度", "value": data["percent"]}
    if tone:
        p1["tone"] = tone
    components.append(p1)
    if steps:
        components.append({
            "id": f"{prefix}s1", "component": "AoList",
            "source": {"path": f"/{prefix}steps"},
            "itemTitlePath": "/title", "itemDetailPath": "/detail",
        })
        return components, {f"{prefix}steps": steps}
    return components, {}


def _map_comparison(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    left = data["left"]
    right = data["right"]
    cols = [
        {"id": "attr", "label": "属性", "path": "/label", "format": "text"},
        {"id": "val", "label": "值", "path": "/value", "format": "text"},
    ]
    components = [
        {"id": f"{prefix}root", "component": "Row", "children": [f"{prefix}lc", f"{prefix}rc"]},
        {"id": f"{prefix}lc", "component": "Column", "children": [f"{prefix}lt", f"{prefix}lt1"]},
        {"id": f"{prefix}lt", "component": "Text", "text": left["title"], "variant": "caption"},
        {"id": f"{prefix}lt1", "component": "AoTable",
         "source": {"path": f"/{prefix}left_items"}, "columns": cols},
        {"id": f"{prefix}rc", "component": "Column", "children": [f"{prefix}rt", f"{prefix}rt1"]},
        {"id": f"{prefix}rt", "component": "Text", "text": right["title"], "variant": "caption"},
        {"id": f"{prefix}rt1", "component": "AoTable",
         "source": {"path": f"/{prefix}right_items"}, "columns": cols},
    ]
    return components, {f"{prefix}left_items": left["items"], f"{prefix}right_items": right["items"]}


def _map_dag_flow(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    components = [{
        "id": f"{prefix}root", "component": "AoDag",
        "source": {"path": f"/{prefix}nodes"},
        "itemIdPath": "/id", "itemTitlePath": "/title",
        "itemStatusPath": "/status", "itemDependsOnPath": "/depends_on",
    }]
    return components, {f"{prefix}nodes": data["nodes"]}


def _map_disclosure_list(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    items = data["items"]
    # 容错：LLM 可能传单个对象而非数组（normalize 未提取出数组时）
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ValueError(f"disclosure_list items must be array or object, got {type(items).__name__}")
    child_ids = []
    components = [{"id": f"{prefix}root", "component": "Column", "children": []}]
    for i, item in enumerate(items):
        did = f"{prefix}d{i+1}"
        did_detail = f"{prefix}d{i+1}_detail"
        child_ids.append(did)
        title = item.get("title", f"项目 {i+1}") if isinstance(item, dict) else str(item)
        detail = item.get("detail", item.get("description", item.get("content", ""))) if isinstance(item, dict) else ""
        comp = {"id": did, "component": "AoDisclosure", "title": title, "children": [did_detail]}
        if "tone" in item if isinstance(item, dict) else False:
            comp["tone"] = item["tone"]
        components.append(comp)
        components.append({"id": did_detail, "component": "Text", "text": detail})
    components[0]["children"] = child_ids
    return components, {}


def _map_bar_chart(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    comp = {
        "id": f"{prefix}root", "component": "AoBarChart",
        "source": {"path": f"/{prefix}items"},
        "itemLabelPath": "/label", "itemValuePath": "/value",
    }
    if "unit" in data:
        comp["unit"] = data["unit"]
    components = [comp]
    return components, {f"{prefix}items": data["items"]}


def _map_line_chart(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    comp = {
        "id": f"{prefix}root", "component": "AoLineChart",
        "source": {"path": f"/{prefix}series"},
        "xAxis": {"path": f"/{prefix}x_axis"},
        "seriesNamePath": "/name", "seriesDataPath": "/data",
    }
    if "unit" in data:
        comp["unit"] = data["unit"]
    components = [comp]
    return components, {f"{prefix}series": data["series"], f"{prefix}x_axis": data["x_axis"]}


def _map_pie_chart(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    comp = {
        "id": f"{prefix}root", "component": "AoPieChart",
        "source": {"path": f"/{prefix}items"},
        "itemLabelPath": "/label", "itemValuePath": "/value",
    }
    if "unit" in data:
        comp["unit"] = data["unit"]
    components = [comp]
    return components, {f"{prefix}items": data["items"]}


def _map_media(data: dict, tone: str | None, prefix: str = "") -> tuple[list, dict]:
    mtype = data["type"]
    url = data["url"]
    comp_map = {"image": "Image", "video": "Video", "audio": "AudioPlayer"}
    media_comp = {"id": f"{prefix}md1", "component": comp_map[mtype], "url": url}
    if mtype == "image":
        if "fit" in data:
            media_comp["fit"] = data["fit"]
        if "variant" in data:
            media_comp["variant"] = data["variant"]
    caption = data.get("caption")
    if caption:
        components = [
            {"id": f"{prefix}root", "component": "Column", "children": [f"{prefix}md1", f"{prefix}md1_cap"]},
            media_comp,
            {"id": f"{prefix}md1_cap", "component": "Text", "text": caption, "variant": "caption"},
        ]
    else:
        components = [
            {"id": f"{prefix}root", "component": "Column", "children": [f"{prefix}md1"]},
            media_comp,
        ]
    return components, {}


_FORM_TYPE_MAP = {
    "text": ("TextField", {}),
    "textarea": ("TextField", {"variant": "longText"}),
    "number": ("TextField", {"variant": "number"}),
    "select": ("ChoicePicker", {}),
    "checkbox": ("CheckBox", {}),
    "date": ("DateTimeInput", {"enableDate": True}),
}


def _map_form(data: dict, tone: str | None, prefix: str = "",
              actions: list | None = None) -> tuple[list, dict]:
    fields = data["fields"]
    field_ids = [f"{prefix}fld{i+1}" for i in range(len(fields))]
    # 按钮区
    if actions:
        btn_ids = [f"{prefix}btn{i+1}" for i in range(len(actions))]
        btn_text_ids = [f"{prefix}btn{i+1}_text" for i in range(len(actions))]
    else:
        btn_ids = [f"{prefix}btn1"]
        btn_text_ids = [f"{prefix}btn1_text"]
        label = data.get("submit_label", "提交")
        actions = [{"name": "submit", "label": label, "tone": "neutral"}]

    children = field_ids + [f"{prefix}btn_row"]
    components = [{"id": f"{prefix}root", "component": "Column", "children": children}]

    # form content 数据
    form_content: dict[str, Any] = {}
    for i, f in enumerate(fields):
        comp_name, extra = _FORM_TYPE_MAP[f["type"]]
        comp: dict[str, Any] = {
            "id": f"{prefix}fld{i+1}", "component": comp_name,
            "label": f["label"], "value": {"path": f"/{prefix}form/{f['name']}"},
        }
        comp.update(extra)
        if f["type"] == "select" and "options" in f:
            comp["options"] = f["options"]
        if "min" in f and f["type"] == "number":
            comp["component"] = "Slider"
            comp.pop("variant", None)
            comp["min"] = f["min"]
            if "max" in f:
                comp["max"] = f["max"]
            if "step" in f:
                comp["step"] = f["step"]
        components.append(comp)
        form_content[f["name"]] = f.get("default", "" if f["type"] in ("text", "textarea") else 0 if f["type"] == "number" else False)

    # 按钮行
    components.append({"id": f"{prefix}btn_row", "component": "Row", "children": btn_ids})
    for i, action in enumerate(actions):
        btn: dict[str, Any] = {
            "id": btn_ids[i], "component": "Button",
            "child": btn_text_ids[i], "action": {"event": {"name": action["name"]}},
        }
        atone = action.get("tone", "neutral")
        if atone != "neutral":
            btn["tone"] = atone
        components.append(btn)
        components.append({"id": btn_text_ids[i], "component": "Text", "text": action["label"]})

    return components, {f"{prefix}form": form_content}


_MAP_FUNCTIONS = {
    "metric_group": _map_metric_group,
    "table": _map_table,
    "timeline": _map_timeline,
    "progress": _map_progress,
    "comparison": _map_comparison,
    "dag_flow": _map_dag_flow,
    "disclosure_list": _map_disclosure_list,
    "bar_chart": _map_bar_chart,
    "line_chart": _map_line_chart,
    "pie_chart": _map_pie_chart,
    "media": _map_media,
    "form": _map_form,
}


def _map_dashboard(data: dict, tone: str | None, prefix: str = "",
                   actions: list | None = None) -> tuple[list, dict]:
    panels = data["panels"]
    section_ids = [f"{prefix}sec{i+1}" for i in range(len(panels))]
    components = [{"id": f"{prefix}root", "component": "Column", "children": section_ids}]
    content: dict[str, Any] = {}

    for i, panel in enumerate(panels):
        sec_prefix = f"{prefix}sec{i+1}_"
        ptype = panel["content_type"]
        pdata = panel["data"]
        ptone = panel.get("tone", tone)

        # 递归映射子面板（不允许嵌套 dashboard）
        if ptype == "dashboard":
            raise ValueError("dashboard 不允许嵌套 dashboard")

        map_fn = _MAP_FUNCTIONS[ptype]
        if ptype == "form":
            sub_components, sub_content = map_fn(pdata, ptone, sec_prefix, panel.get("actions"))
        else:
            sub_components, sub_content = map_fn(pdata, ptone, sec_prefix)

        # AoSection 包裹子面板
        sec_comp: dict[str, Any] = {
            "id": f"{prefix}sec{i+1}", "component": "AoSection",
            "title": panel["title"], "children": [f"{sec_prefix}root"],
        }
        if ptone:
            sec_comp["tone"] = ptone
        components.append(sec_comp)
        components.extend(sub_components)
        content.update(sub_content)

    return components, content


# ── 三层校验 ──────────────────────────────────────────


def _validate_schema(data: dict, content_type: str) -> str | None:
    """第 1 层：schema 校验（简化版，检查关键字段）。"""
    schema = CONTENT_TYPE_SCHEMAS.get(content_type)
    if schema is None:
        return f"unknown content_type: {content_type}"

    props = schema.get("properties", {})
    required = schema.get("required", [])

    # 检查必填字段
    for field in required:
        if field not in data:
            return f"missing required field: {field}"

    # 检查数组长度
    for field, field_schema in props.items():
        if field not in data:
            continue
        val = data[field]
        if field_schema.get("type") == "array":
            if not isinstance(val, list):
                return f"field {field} must be array"
            min_items = field_schema.get("minItems", 0)
            max_items = field_schema.get("maxItems", 9999)
            if len(val) < min_items:
                return f"field {field} needs at least {min_items} items"
            if len(val) > max_items:
                return f"field {field} exceeds max {max_items} items"

    # dashboard 子面板不允许嵌套 dashboard
    if content_type == "dashboard":
        for i, panel in enumerate(data.get("panels", [])):
            if panel.get("content_type") == "dashboard":
                return f"dashboard panel {i+1} cannot nest dashboard"

    return None


def _validate_injection(data: Any, content_type: str = "") -> str | None:
    """第 2 层：防注入校验（递归检查所有字符串值）。"""
    if isinstance(data, str):
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(data):
                return f"injection detected: {pattern.pattern}"
        if _PROTOTYPE_PATTERNS.search(data):
            return "prototype pollution detected"
        if _POINTER_BAD.search(data):
            # 排除合法 URL 中的 //（如 https://）
            if not data.startswith(_ALLOWED_URL_SCHEMES):
                return "invalid pointer path"
        # media URL scheme 校验
        if content_type == "media" and data.startswith(("http", "ftp", "file", "data")):
            if not data.startswith(_ALLOWED_URL_SCHEMES):
                return f"disallowed URL scheme in media url"
    elif isinstance(data, dict):
        # 检查字段名
        for key in data:
            if _PROTOTYPE_PATTERNS.search(str(key)):
                return f"prototype pollution in field name: {key}"
            err = _validate_injection(data[key], content_type)
            if err:
                return err
    elif isinstance(data, list):
        for item in data:
            err = _validate_injection(item, content_type)
            if err:
                return err
    return None


def _validate_budget(components: list, content: dict) -> str | None:
    """第 3 层：预算约束校验。"""
    if len(components) > MAX_COMPONENTS:
        return f"too many components: {len(components)} > {MAX_COMPONENTS}"
    root = next((c for c in components if c.get("id") == "root"), None)
    if root and "children" in root:
        if len(root["children"]) > MAX_CHILDREN:
            return f"too many root children: {len(root['children'])} > {MAX_CHILDREN}"
    serialized = json.dumps({"components": components, "content": content}, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > MAX_BYTES:
        return f"serialized too large: {len(serialized.encode('utf-8'))} > {MAX_BYTES}"
    return None


# ── 防绕过检查（已废弃，emit_widget 工具整体移除后无需检查）──────────────────────


def is_covered_by_present_content(surface: dict) -> bool:
    """检查 surface 是否可由 present_content 生成（特征匹配）。

    历史用途：emit_widget 防绕过——如果 agent 直接调 emit_widget(type=a2ui) 且
    surface 符合 present_content 特征，则拒绝（应走 present_content）。

    v2 后 emit_widget 工具整体移除，本函数已无调用方，仅保留供历史代码兼容。
    新代码不应再依赖此检查。
    """
    components = surface.get("components", [])
    if not components:
        return False

    # 特征 1：catalogId 是 AgentOps catalog
    if surface.get("catalogId") != A2UI_CATALOG_ID:
        return False

    # 特征 2：root 组件是 present_content 可生成的类型
    root = next((c for c in components if c.get("id") == "root"), None)
    if not root:
        return False
    if root.get("component") not in _COVERED_ROOTS:
        return False

    # 特征 3：所有组件 ID 符合 present_content 命名规则
    for comp in components:
        if not _AO_ID_PATTERN.match(comp.get("id", "")):
            return False

    return True


# ── 主函数 ────────────────────────────────────────────


def build_surface(content_type: str, data: dict, tone: str | None = None,
                  actions: list | None = None) -> tuple[dict, dict]:
    """把高层语义 data 映射为 A2UI surface + content。

    返回 (surface, props_content)：
    - surface: A2UI surface dict（含 version/catalogId/components）
    - props_content: 传给前端 A2uiWidget 的 content 字段
    """
    # dashboard 单独处理（递归）
    if content_type == "dashboard":
        components, content = _map_dashboard(data, tone)
    elif content_type == "form":
        # v2：form content_type 已废弃，HIL 走 request_human_input 文本问答
        raise ValueError("form content_type 已废弃（v2），HIL 走 request_human_input 文本问答")
    else:
        map_fn = _MAP_FUNCTIONS[content_type]
        components, content = map_fn(data, tone)

    surface = {
        "version": A2UI_VERSION,
        "catalogId": A2UI_CATALOG_ID,
        "components": components,
    }
    return surface, content


def _normalize_data(data: dict, content_type: str) -> dict:
    """预处理 LLM 常见格式偏差，返回修正后的 data。

    已知偏差：
    - 数组字段被包装在 {"item": [...]} 中（MiniMax 常见）
    - 字符串值被包装在 {"text": "..."} 中
    - dashboard 用 groups/sections 代替 panels，panel 内用 metrics 代替 data
    """
    schema = CONTENT_TYPE_SCHEMAS.get(content_type, {})
    props = schema.get("properties", {})
    normalized = dict(data)

    # dashboard 专用容错：LLM 常用 groups/sections 代替 panels
    if content_type == "dashboard" and "panels" not in normalized:
        for alias in ("groups", "sections", "items"):
            if alias in normalized and isinstance(normalized[alias], list):
                normalized["panels"] = normalized.pop(alias)
                logger.debug("normalize: dashboard %s → panels (%d items)", alias, len(normalized["panels"]))
                break

    for field, field_schema in props.items():
        if field not in normalized:
            continue
        val = normalized[field]
        # 期望数组但收到 dict → 尝试从 item/items/list/data/metrics 等键提取
        if field_schema.get("type") == "array" and isinstance(val, dict) and not isinstance(val, list):
            for key in ("item", "items", "list", "data", "metrics", "rows", "columns", "panels", "events", "steps", "checklist"):
                if key in val and isinstance(val[key], list):
                    normalized[field] = val[key]
                    logger.debug("normalize: extracted %s[%s] → array (%d items)", field, key, len(val[key]))
                    break

    # dashboard panel 容错：panel 内用 metrics/items 代替 data，缺 content_type 时推断
    if content_type == "dashboard" and isinstance(normalized.get("panels"), list):
        for panel in normalized["panels"]:
            if not isinstance(panel, dict):
                continue
            # metrics/items → data（包装为 {alias: [...]} 格式，匹配子 content_type 的 schema）
            if "data" not in panel:
                for alias in ("metrics", "items", "rows", "events", "steps", "columns", "nodes"):
                    if alias in panel:
                        val = panel.pop(alias)
                        # 数组 → 包装为 {alias: [...]}；dict → 直接用
                        panel["data"] = {alias: val} if isinstance(val, list) else val
                        # 推断 content_type
                        if "content_type" not in panel:
                            panel["content_type"] = "metric_group" if alias == "metrics" else _infer_content_type(panel["data"])
                        break
            # 缺 content_type 但有 data → 推断
            if "content_type" not in panel and "data" in panel:
                panel["content_type"] = _infer_content_type(panel["data"])

    return normalized


def _infer_content_type(data: Any) -> str:
    """根据 data 的结构特征推断 content_type。"""
    if not isinstance(data, dict):
        return "metric_group"
    if "metrics" in data:
        return "metric_group"
    if "columns" in data and "rows" in data:
        return "table"
    if "events" in data:
        return "timeline"
    if "percent" in data or "steps" in data:
        return "progress"
    if "left" in data and "right" in data:
        return "comparison"
    if "nodes" in data:
        return "dag_flow"
    if "items" in data:
        return "disclosure_list"
    return "metric_group"


def validate(content_type: str, data: dict) -> str | None:
    """执行三层校验，返回 None 表示通过，否则返回错误信息。"""
    # 预处理：修正 LLM 常见格式偏差
    data = _normalize_data(data, content_type)
    # 第 1 层：schema
    err = _validate_schema(data, content_type)
    if err:
        return err
    # 第 2 层：防注入
    err = _validate_injection(data, content_type)
    if err:
        return err
    # 第 3 层：预算约束（需要先映射才能检查组件数）
    try:
        surface, content = build_surface(content_type, data)
        err = _validate_budget(surface["components"], content)
        if err:
            return err
    except Exception as e:
        logger.warning(
            "validate mapping failed: content_type=%s data=%s",
            content_type, json.dumps(data, ensure_ascii=False, default=str)[:1000],
        )
        return f"mapping error: {e}"
    return None


def make_present_content_tool(state, event_sink):
    """创建 present_content 工具的 ToolDefinition。

    在 make_conversational_tools 中调用，注入 state 和 event_sink 闭包。
    """
    from harness.protocol import ToolDefinition

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        """高层语义展示工具：Agent 只传 content_type + data，工具内部映射为 A2UI。"""
        # identity_spoof：surface 身份由 Worker 按调用身份派生，模型不可指定
        if args.get("surface_id"):
            logger.warning("present_content rejected: identity_spoof (args.surface_id)")
            return {"content": "rejected: surface_id 由系统派生，不可通过参数指定", "status": "rejected"}

        title = args.get("title", "")
        content_type = args.get("content_type", "")
        data = args.get("data", {})
        tone = args.get("tone")
        widget_id = args.get("widget_id") or f"w_{state.turn_count}_{len(state.emitted_widgets)}"
        actions = args.get("actions")

        logger.info(
            "present_content called: title=%r content_type=%r data=%s",
            title[:50], content_type,
            json.dumps(data, ensure_ascii=False, default=str)[:800] if isinstance(data, dict) else repr(data)[:800],
        )

        # 标题长度校验
        if not title or len(title) < 1 or len(title) > 200:
            logger.warning("present_content rejected: title invalid (%d chars)", len(title))
            return {"content": "rejected: title must be 1-200 chars", "status": "rejected"}

        # content_type 枚举校验
        if content_type not in CONTENT_TYPES:
            logger.warning("present_content rejected: unknown content_type %r", content_type)
            return {"content": f"rejected: unknown content_type '{content_type}'", "status": "rejected"}

        # v2：form content_type 废弃（HIL 走 request_human_input 文本问答）
        if content_type == "form":
            logger.warning("present_content rejected: form content_type deprecated in v2")
            return {
                "content": "rejected: form content_type 已废弃（v2），HIL 请用 request_human_input 文本问答",
                "status": "rejected",
            }

        # tone 校验
        if tone and tone not in TONE_ENUM:
            logger.warning("present_content rejected: invalid tone %r", tone)
            return {"content": f"rejected: invalid tone '{tone}'", "status": "rejected"}

        # 三层校验（validate 内部会先 normalize data）
        err = validate(content_type, data)
        if err:
            logger.warning("present_content rejected: validation failed: %s", err)
            return {"content": f"rejected: {err}", "status": "rejected"}

        # 用 normalize 后的 data 映射为 A2UI surface（经 load_template 走 reachable 校验，
        # 与 present_content_surface 共用单一入口，确保内联路径也享受 dangling 防护）
        try:
            data = _normalize_data(data, content_type)
            from orchestrator.manager_visual_templates import load_template
            components, content = load_template(content_type, data, tone, actions)
            surface = {
                "version": A2UI_VERSION,
                "catalogId": A2UI_CATALOG_ID,
                "components": components,
            }
        except Exception as e:
            logger.exception("present_content mapping error: %s", e)
            return {"content": f"rejected: mapping error: {e}", "status": "rejected"}

        # v2：展示型 content_type 上大屏（emit REPORT_SURFACE_STATE）
        # form 已在校验阶段拒绝；actions 随 form 废弃（展示型 _map_* 不生成交互组件）
        from orchestrator.protocol import DagEvent, DagEventType, SurfaceState
        from orchestrator.actor_visual_profile import (
            compute_surface_id_identity,
            compute_components_digest,
        )
        from datetime import datetime

        view_id = "manager-live"  # manager profile 唯一声明 view
        phase = "final"

        # 身份派生 surface_id，修复
        # 「多次调用被合并为一张卡」：surface 标识由调用身份决定，不由内容 hash 决定）：
        #   - 模型传了 widget_id → 命名 surface："同 id 替换"（同一张卡原地更新）
        #   - 未传 → 序号 surface：每次调用一张新卡片（累积展示）
        named_widget_id = (args.get("widget_id") or "").strip()
        if named_widget_id:
            generation: int | str = f"w:{named_widget_id}"
        else:
            generation = len(state.emitted_widgets) + 1
        surface_id = compute_surface_id_identity(state.run_id, state.agent_id, view_id, generation)
        components_digest = compute_components_digest(components)

        # patch_sequence：同一 surface 的第 N 次更新（新序号 surface 恒为 1）
        patch_sequence = state.surface_patch_seq.get(surface_id, 0) + 1
        state.surface_patch_seq[surface_id] = patch_sequence

        data_model = dict(content)  # _map_* 返回的内联数据（前端 A2uiWidget content）
        data_model.setdefault("title", title)

        surface_state = SurfaceState(
            surface_id=surface_id,
            view_id=view_id,
            phase=phase,
            components=components,
            data_model=data_model,
            # surfaceProperties schema 只允许 iconUrl / agentDisplayName（additionalProperties: false）
            # title 已放入 data_model，surface_properties 只放合规字段避免前端 ajv 校验降级
            surface_properties={"agentDisplayName": state.agent_id},
            output_contract=None,
            source="agent",
            emitted_at=datetime.now(),
            patch_sequence=patch_sequence,
        )

        state.emitted_widgets.append(widget_id)
        await event_sink(DagEvent(
            type=DagEventType.REPORT_SURFACE_STATE,
            run_id=state.run_id,
            node_id=f"conv:{state.agent_id}",
            payload={
                "surface_state": surface_state.to_payload(),
                "actor_id": state.agent_id,
                "view_id": view_id,
                "phase": phase,
                "source": "agent",
            },
            surface_state=surface_state,
            sequence=0,
        ))
        n_comps = len(components)
        logger.info(
            "present_content OK (v2 大屏): widget_id=%s view=%s content_type=%s components=%d "
            "surface_id=%s patch_seq=%d",
            widget_id, view_id, content_type, n_comps, surface_id[:12], patch_sequence,
        )
        return {
            "content": f"presented {content_type} as {widget_id} on supervision panel",
            "widget_id": widget_id,
            "surface_id": surface_id,
            "patch_sequence": patch_sequence,
        }

    return ToolDefinition(
        name="present_content",
        description=(
            "向用户展示可视化内容（高层语义，不接触 A2UI 协议）。"
            "content_type 枚举：metric_group/table/timeline/progress/comparison/"
            "dag_flow/disclosure_list/bar_chart/line_chart/pie_chart/media/form/dashboard。"
            "根据返回内容类型选 content_type，工具内部映射为 A2UI 组件并校验推送。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 200,
                          "description": "展示标题"},
                "content_type": {"enum": CONTENT_TYPES,
                                 "description": "内容类型，决定展示形式"},
                "data": {"type": "object",
                         "description": "数据，按 content_type 对应的 schema"},
                "tone": {"enum": TONE_ENUM,
                         "description": "可选整体 tone"},
                "widget_id": {"type": "string",
                              "description": "可选命名卡 ID：同 id 复用同一张卡原地更新，不同 id 或不传则每次调用生成新卡片（累积展示）"},
                "actions": {"type": "array",
                            "description": "可选交互按钮（仅 form/table/disclosure_list/dashboard）",
                            "items": {"type": "object",
                                      "properties": {
                                          "name": {"type": "string"},
                                          "label": {"type": "string"},
                                          "tone": {"enum": TONE_ENUM},
                                      },
                                      "required": ["name", "label"]}},
            },
            "required": ["title", "content_type", "data"],
        },
        handler=handler,
    )


