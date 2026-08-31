"""upsert_generated_view 工具 — Manager Agent 自由 A2UI Block 写入。

与 present_content（便捷语法糖，选 content_type + data）互补：本工具让 agent
直接写 A2UI 组件树，提供 escape hatch 能力。走独立校验链（被动组件白名单 +
reachable + 防注入 + 预算），**不复用 report_surface_state 的 view 白名单 +
phase 单调 + digest dedup 校验链**——manager 自由 A2UI 不应受 DAG 约束。

emit 路径：构造 SurfaceState → emit DagEvent(REPORT_SURFACE_STATE) → 前端
SupervisionPanel reducer 按 (actor_id, view_id) 聚合渲染。

Manager Agent 自由 A2UI Block 写入工具（agent 直接构造组件树，非 content_type 选模板）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from orchestrator.actor_visual_profile import (
    compute_components_digest,
    compute_surface_id_identity,
)
from orchestrator.protocol import DagEvent, DagEventType, SurfaceState
from tools.report_surface_state import _validate_reachable

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

A2UI_VERSION = "v1.0"
A2UI_CATALOG_ID = "https://agentops.dev/a2ui/catalogs/core/v1"

MAX_COMPONENTS = 128
MAX_DEPTH = 8
MAX_CHILDREN = 24
MAX_BYTES = 64 * 1024

# 被动 A2UI 组件白名单（manager 自由 A2UI 禁用可交互组件）
# 禁止 Button/TextField/CheckBox/ChoicePicker/Slider/DateTimeInput/Modal 等可交互组件
PASSIVE_A2UI_COMPONENTS = {
    # 基础展示
    "Text", "Image", "Icon", "Video", "AudioPlayer",
    # 容器布局
    "Row", "Column", "List", "Card", "Tabs", "Divider",
    # AgentOps Catalog（Ao* 前缀）
    "AoGrid", "AoGridItem", "AoTable", "AoTimeline",
    "AoMetric", "AoStatusBadge", "AoProgress", "AoStep",
    "AoList", "AoBarChart", "AoLineChart", "AoPieChart",
    "AoDag", "AoDisclosure", "AoLink", "AoArtifact", "AoIf",
    "AoSection",
}

# 禁止的可交互组件（安全边界：manager 生成被动展示，交互走 actions）
EXECUTABLE_COMPONENTS = {
    "Button", "TextField", "CheckBox", "ChoicePicker",
    "Slider", "DateTimeInput", "Modal",
}

# 禁止的可执行字段（防注入 + 安全边界）
EXECUTABLE_FIELD_NAMES = {
    "action", "actions", "functionCall", "html",
    "onClick", "onSubmit", "script", "srcdoc",
}

# 防注入检测规则（与 present_content.py 一致）
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
_ALLOWED_URL_SCHEMES = ("http://", "https://", "data:")

# 默认 view_id（manager profile 已声明的 view）
DEFAULT_VIEW_ID = "manager-live"
DEFAULT_PHASE = "final"  # 单次展示用 final（支持同 view 多次更新，shouldReplace("final","final")=true）


# ── 校验函数 ──────────────────────────────────────────


# 组件必填字段表（对齐 web/src/lib/a2ui/schemas.ts:531-771 componentSchema required 参数）
# 防止 LLM 自由写 A2UI 时漏字段（如 AoDisclosure 漏 title/children）→ 后端放行 → 前端 ajv 拒 → 降级
_COMPONENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    # 基础展示
    "Text": ("text",),
    "Image": ("url",),
    "Icon": ("name",),
    "Video": ("url",),
    "AudioPlayer": ("url",),
    # 容器布局
    "Row": ("children",),
    "Column": ("children",),
    "List": ("children",),
    "Card": ("child",),
    "Tabs": ("tabs",),
    "Divider": (),
    # AgentOps Catalog（Ao* 前缀）—— 对齐 schemas.ts:669-770
    "AoGrid": ("children",),
    "AoGridItem": (),
    "AoSection": ("title", "children"),
    "AoMetric": ("label", "value"),
    "AoStatusBadge": ("text",),
    "AoProgress": ("value",),
    "AoStep": (),
    "AoList": ("source",),
    "AoTable": ("source", "columns"),
    "AoTimeline": ("source",),
    "AoBarChart": ("source",),
    "AoLineChart": ("source",),
    "AoPieChart": ("source",),
    "AoDag": ("source",),
    "AoDisclosure": ("title", "children"),  # schemas.ts:749-753
    "AoLink": ("label", "url"),
    "AoArtifact": ("kind", "uri"),
    "AoIf": ("condition", "children"),
}


def _validate_required_fields(components: list[dict]) -> list[str]:
    """校验每个组件的必填字段齐全（对齐前端 ajv schema required 列表）。

    防止 LLM 自由写 A2UI 漏字段（如 AoDisclosure 漏 title 或 children）→
    后端 reachable 校验放行 → 前端 ajv 拒绝 → A2uiRenderer 降级卡片。
    本函数把降级挡在后端，让 agent 收到明确错误可修正。
    """
    issues: list[str] = []
    for c in components:
        comp_type = c.get("component")
        if not comp_type:
            continue  # _validate_passive_components 已报
        required = _COMPONENT_REQUIRED_FIELDS.get(comp_type)
        if required is None:
            continue  # 未列入表的组件不强制（保守，避免误拒合法新组件）
        missing = [f for f in required if f not in c]
        if missing:
            issues.append(
                f"component id='{c.get('id', '?')}' type='{comp_type}' missing required fields: {missing}"
            )
    return issues


def _validate_passive_components(components: list[dict]) -> list[str]:
    """校验所有组件在被动白名单内（禁止可交互组件）。"""
    issues: list[str] = []
    for c in components:
        comp_type = c.get("component")
        if not comp_type:
            issues.append(f"component id='{c.get('id')}' missing 'component' field")
            continue
        if comp_type in EXECUTABLE_COMPONENTS:
            issues.append(
                f"component id='{c.get('id')}' type='{comp_type}' is executable "
                f"(forbidden in passive surface)"
            )
        elif comp_type not in PASSIVE_A2UI_COMPONENTS:
            issues.append(
                f"component id='{c.get('id')}' type='{comp_type}' not in passive whitelist"
            )
    return issues


def _validate_no_executable_fields(components: list[dict]) -> list[str]:
    """校验组件树无禁止的可执行字段（防注入 + 安全边界）。"""
    issues: list[str] = []

    def _scan(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in EXECUTABLE_FIELD_NAMES:
                    issues.append(f"forbidden field '{k}' at {path}")
                _scan(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _scan(item, f"{path}[{i}]")

    _scan(components, "components")
    return issues


def _validate_injection(text: str, field_name: str) -> str | None:
    """检测文本是否含注入模式。返回 issue 字符串或 None。"""
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return f"field '{field_name}' contains forbidden pattern: {pat.pattern}"
    if _PROTOTYPE_PATTERNS.search(text):
        return f"field '{field_name}' contains prototype pollution pattern"
    return None


def _validate_urls(components: list[dict]) -> list[str]:
    """校验所有 url 字段 scheme 安全。"""
    issues: list[str] = []

    def _scan(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "url" and isinstance(v, str):
                    if not v.startswith(_ALLOWED_URL_SCHEMES):
                        issues.append(f"url at {path}.{k} has forbidden scheme")
                _scan(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _scan(item, f"{path}[{i}]")

    _scan(components, "components")
    return issues


def _validate_artifact_uris(components: list[dict]) -> list[str]:
    """校验所有 uri 字段（AoArtifact 等用 uri 而非 url）安全。

    对齐 web/src/lib/a2ui/artifact-uri.ts 的 isSafeGenerativeUiArtifactUri：
    允许 HTTP(S) / artifact: scheme / Windows drive / 无冒号本地路径；
    拒绝 //、\\\\、其他 scheme、控制字符、协议相对 URL。
    防 LLM 注入恶意 uri（file: / javascript: / 路径穿越）。
    """
    import re as _re
    control = _re.compile(r'[\x00-\x1f\x7f]')
    http_uri = _re.compile(r'^https?://[^\s\\]+$', _re.IGNORECASE)
    artifact_uri = _re.compile(r'^artifact:[A-Za-z0-9][A-Za-z0-9._~/%-]*$', _re.IGNORECASE)
    win_drive = _re.compile(r'^[A-Za-z]:[\\/](?![\\/])')

    def _is_safe(v: str) -> bool:
        if not isinstance(v, str) or len(v) < 1 or len(v) > 2048:
            return False
        if v != v.strip() or control.search(v):
            return False
        # 纵深防御：uri 字段拒 HTML 标记（合法 http/artifact/path 都不含 < > "）
        if '<' in v or '>' in v or '"' in v:
            return False
        if _re.match(r'^[\\/]{2}', v) or v.startswith('\\'):
            return False
        if http_uri.match(v):
            from urllib.parse import urlparse
            try:
                p = urlparse(v)
                return (p.scheme in ('http', 'https') and bool(p.hostname)
                        and not p.username and not p.password)
            except Exception:
                return False
        if _re.match(r'^https?:', v, _re.IGNORECASE):
            return False
        if artifact_uri.match(v):
            return True
        if _re.match(r'^artifact:', v, _re.IGNORECASE):
            return False
        if win_drive.match(v):
            return True
        return ':' not in v

    issues: list[str] = []

    def _scan(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "uri" and isinstance(v, str) and not _is_safe(v):
                    issues.append(f"unsafe artifact uri at {path}.{k}")
                _scan(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _scan(item, f"{path}[{i}]")

    _scan(components, "components")
    return issues


def _validate_depth(components: list[dict]) -> list[str]:
    """校验组件树深度不超过 MAX_DEPTH。"""
    issues: list[str] = []
    by_id = {c.get("id"): c for c in components if c.get("id")}

    def _depth(comp_id: str, visited: set[str] | None = None) -> int:
        if visited is None:
            visited = set()
        if comp_id in visited:
            return 0  # 循环引用，已校验 reachable 会单独报错
        visited.add(comp_id)
        comp = by_id.get(comp_id)
        if not comp:
            return 1
        children = comp.get("children", [])
        if not isinstance(children, list) or not children:
            return 1
        return 1 + max(
            (_depth(cid, visited.copy()) for cid in children if isinstance(cid, str)),
            default=0,
        )

    root_depth = _depth("root")
    if root_depth > MAX_DEPTH:
        issues.append(f"component tree depth {root_depth} exceeds limit {MAX_DEPTH}")
    return issues


def _validate_budget(components: list[dict]) -> list[str]:
    """校验组件数量 + 字节数预算。"""
    issues: list[str] = []
    if len(components) > MAX_COMPONENTS:
        issues.append(f"component count {len(components)} exceeds limit {MAX_COMPONENTS}")

    # 检查每个 root 直接子节点数
    root = next((c for c in components if c.get("id") == "root"), None)
    if root and isinstance(root.get("children"), list):
        if len(root["children"]) > MAX_CHILDREN:
            issues.append(
                f"root children count {len(root['children'])} exceeds limit {MAX_CHILDREN}"
            )

    import json
    try:
        payload_bytes = len(json.dumps(components, ensure_ascii=False).encode("utf-8"))
        if payload_bytes > MAX_BYTES:
            issues.append(f"payload size {payload_bytes} exceeds limit {MAX_BYTES}")
    except (TypeError, ValueError) as e:
        issues.append(f"payload serialization failed: {e}")

    return issues


# ── 工具工厂 ──────────────────────────────────────────


def make_upsert_generated_view_tool(
    actor_id: str,
    run_id: str | None = None,
    event_sink=None,
    node_id: str | None = None,
):
    """构造 upsert_generated_view ToolDefinition。

    Manager Agent 专用自由 A2UI 工具。agent 直接写 A2UI 组件树，
    工具内部校验（被动白名单 + reachable + 防注入 + 预算）后 emit
    DagEvent(REPORT_SURFACE_STATE) 到前端 SupervisionPanel。

    与 make_present_content_surface_tool 的区别：
    - 后者：接收 content_type + data → _map_* 生成 components → 调 report_surface_state 校验链
    - 本工具：接收 a2ui components → 自己校验 → 直接构造 SurfaceState → emit

    Args:
        actor_id: 烘焙进 handler 的 actor 身份（agent 不必传）
        run_id: run/session id（emit DagEvent 用）
        event_sink: DagEvent emit 目标
        node_id: 节点 id（emit DagEvent 用）
    """
    from harness.protocol import ToolDefinition  # 延迟 import 避免循环

    # 闭包级 patch 计数：同一 surface 每次 upsert 递增
    _patch_seq: dict[str, int] = {}

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        # ── 0. identity_spoof（surface 身份由 Worker 派生，模型不可指定） ──
        if args.get("surface_id"):
            return _err(
                "identity_spoof",
                "surface_id 由系统按调用身份派生，不可通过参数指定（拒绝注入）",
            )

        # ── 1. 解析参数 ──
        view_id = (args.get("view_id") or DEFAULT_VIEW_ID).strip()
        phase = (args.get("phase") or DEFAULT_PHASE).strip()
        components = args.get("components") or args.get("a2ui", {}).get("components")
        data_model = args.get("data") or args.get("data_model") or {}
        title = (args.get("title") or "").strip()
        surface_properties = args.get("surface_properties")

        if not components:
            return _err(
                "missing_components",
                "components 必填（A2UI 组件树，root 组件必须存在）",
            )
        if not isinstance(components, list):
            return _err(
                "components_not_list",
                f"components 必须是数组，got {type(components).__name__}",
            )

        # ── 2. 组件树校验 ──
        # 2a. 被动组件白名单
        issues = _validate_passive_components(components)
        if issues:
            return _err("executable_component", "; ".join(issues))

        # 2a-bis. 组件必填字段齐全（对齐前端 ajv schema，防止漏字段导致前端降级）
        issues = _validate_required_fields(components)
        if issues:
            return _err("missing_required_fields", "; ".join(issues))

        # 2b. 无可执行字段（防注入）
        issues = _validate_no_executable_fields(components)
        if issues:
            return _err("executable_field", "; ".join(issues))

        # 2c. URL scheme 安全
        issues = _validate_urls(components)
        if issues:
            return _err("unsafe_url", "; ".join(issues))

        # 2c-bis. AoArtifact uri 字段安全（对齐 web/src/lib/a2ui/artifact-uri.ts）
        issues = _validate_artifact_uris(components)
        if issues:
            return _err("unsafe_artifact_uri", "; ".join(issues))

        # 2d. 文本字段防注入扫描
        for c in components:
            for k, v in c.items():
                if isinstance(v, str) and k in ("text", "label", "title", "caption", "summary"):
                    inj = _validate_injection(v, f"{c.get('id', '?')}.{k}")
                    if inj:
                        return _err("injection_detected", inj)

        # 2e. reachable（防止 dangling children）
        issues = _validate_reachable(components)
        if issues:
            return _err("components_unreachable", "; ".join(issues))

        # 2f. 深度
        issues = _validate_depth(components)
        if issues:
            return _err("depth_exceeded", "; ".join(issues))

        # 2g. 预算（数量 + 字节数）
        issues = _validate_budget(components)
        if issues:
            return _err("budget_exceeded", "; ".join(issues))

        # ── 3. 构造 SurfaceState（不走 report_surface_state 校验链） ──
        # 身份派生 surface_id：
        # 同一 (run, actor, view) 稳定复用 → upsert 语义（同一张卡原地更新），
        # 不再按内容 hash 拆卡（前端按 surface_id 聚合）。
        surface_id = compute_surface_id_identity(run_id or "", actor_id, view_id)
        components_digest = compute_components_digest(components)
        patch_sequence = _patch_seq.get(surface_id, 0) + 1
        _patch_seq[surface_id] = patch_sequence

        # surfaceProperties schema 只允许 iconUrl / agentDisplayName（additionalProperties: false）
        # title 放 data_model，surface_properties 只放合规字段避免前端 ajv 校验降级
        if surface_properties is None:
            surface_properties = {}
        surface_properties.setdefault("agentDisplayName", actor_id)
        if "title" in surface_properties:
            # title 不在 surfaceProperties schema，转存到 data_model
            data_model.setdefault("title", surface_properties.pop("title"))

        surface = SurfaceState(
            surface_id=surface_id,
            view_id=view_id,
            phase=phase,
            components=components,
            data_model=data_model,
            surface_properties=surface_properties or None,
            output_contract=None,  # 自由 A2UI 不绑定 output_contract
            source="agent",
            emitted_at=datetime.now(),
            patch_sequence=patch_sequence,
        )

        logger.info(
            "upsert_generated_view 通过校验: actor=%s view=%s phase=%s surface_id=%s components=%d patch_seq=%d",
            actor_id, view_id, phase, surface_id[:12], len(components), patch_sequence,
        )

        # ── 4. emit DagEvent(REPORT_SURFACE_STATE) ──
        # 复用前端 SupervisionPanel 渲染路径（applySurfaceStateEvent reducer）
        if event_sink is not None:
            try:
                await event_sink(
                    DagEvent(
                        type=DagEventType.REPORT_SURFACE_STATE,
                        run_id=run_id or "",
                        node_id=node_id,
                        payload={
                            "surface_state": surface.to_payload(),
                            "actor_id": actor_id,
                            "view_id": surface.view_id,
                            "phase": surface.phase,
                            "source": surface.source,
                        },
                        surface_state=surface,
                    )
                )
            except Exception as e:
                # emit 失败不阻断工具返回（与现有工具一致）
                logger.warning(
                    "upsert_generated_view emit DagEvent 失败（不影响工具返回）: %s", e
                )

        return {
            "ok": True,
            "widget_id": surface_id,  # 前端按 (actor_id, view_id) 聚合，widget_id 供 agent 引用
            "surface_id": surface_id,
            "components_digest": components_digest,
            "view_id": view_id,
            "phase": phase,
            "emitted_at": surface.emitted_at.isoformat(),
        }

    return ToolDefinition(
        name="upsert_generated_view",
        description=(
            "Create or replace an A2UI Block on the Supervision panel. "
            "Agent writes A2UI component tree directly (escape hatch for "
            "complex layouts that present_content's 13 content_types cannot cover). "
            "Passive components only (no Button/TextField/Modal). "
            "Reuse view_id to update an existing panel."
        ),
        handler=handler,
        input_schema={
            "type": "object",
            "properties": {
                "view_id": {
                    "type": "string",
                    "description": "View identifier. Reuse same view_id to update panel.",
                    "default": DEFAULT_VIEW_ID,
                },
                "title": {
                    "type": "string",
                    "description": "Panel title (shown in surface_properties)",
                    "maxLength": 200,
                },
                "components": {
                    "type": "array",
                    "description": "A2UI v1.0 component tree. Must contain a 'root' component.",
                    "items": {"type": "object"},
                },
                "data": {
                    "type": "object",
                    "description": "Data model for component bindings (JSON Pointer sources).",
                },
                "phase": {
                    "type": "string",
                    "enum": ["started", "partial", "final"],
                    "default": DEFAULT_PHASE,
                    "description": "Snapshot phase. Use 'final' for single-shot, "
                    "'partial' for progressive updates.",
                },
                "surface_properties": {
                    "type": "object",
                    "description": "Optional surface properties (iconUrl, agentDisplayName, etc.)",
                },
            },
            "required": ["components"],
        },
    )


def _err(error_code: str, message: str) -> dict[str, Any]:
    """构造统一错误返回格式。"""
    return {
        "ok": False,
        "error": message,
        "error_code": error_code,
    }
