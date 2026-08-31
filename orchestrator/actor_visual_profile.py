"""
Actor Visual Profile — v99.5 P0.2 L1.5 Worker Profile 层。

每个 actor 在 config/actors/<actor_id>/actor_visual_profile.json 声明：
  - allowed_surface_views[]: view_id 白名单
  - 每个 view 的 output_contract（ActorReport / Mission / Failure / RoundGate）
  - 每个 view 的 fields 类型约束（required / type / max_length / min / max / enum）

Profile 跟 actor 走（不跟 agent 走），跨 workflow 共享同一 actor → 同一组 view_id。

设计哲学（详见 docs/reconstruction/agentops-v99.5-a2ui-design.md §2.2）：
  - Actor 命名松散（research / 调研员 / critic）→ Actor 维度
  - Contract 是普适的（ActorReport 跨 workflow 复用）→ Contract 维度
  - view_id 绑定到 contract 的字段子集 → View 维度
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    # 仅类型检查时 import（运行时函数体内已延迟 import，避免循环依赖）
    from orchestrator.protocol import DagEvent


# 5 种 tone 词汇（A2UI v1.0 + AgentOps View Spec toneFrom）
VALID_TONES = {"neutral", "info", "positive", "warning", "critical"}

# 4 种 phase 单调推进顺序（per view 维度）
PHASE_ORDER = {"started": 0, "partial": 1, "final": 2, "superseded": 3}
VALID_PHASES = set(PHASE_ORDER.keys())

# 字段类型白名单（对照 AgentOps Ao* 扩展 18 节点类型 + A2UI v1.0 catalog）
VALID_FIELD_TYPES = {
    "string",
    "integer",
    "number",
    "boolean",
    "array",
    "object",
    "enum",
}

# A2UI v1.0 catalog 已知的组件名（30+ 组件，前端 A2uiRenderer 据此路由）
KNOWN_A2UI_COMPONENTS = {
    # L0 标准组件（13 个）
    "Text", "Image", "Icon", "Video", "AudioPlayer",
    "Row", "Column", "List", "Card", "Tabs",
    "Modal", "Divider", "Button", "TextField", "CheckBox",
    "ChoicePicker", "Slider", "DateTimeInput",
    # AgentOps Ao* 扩展（18 个，统一前缀）
    "AoGrid", "AoGridItem", "AoSection", "AoMetric", "AoStatusBadge",
    "AoProgress", "AoStep", "AoList", "AoTable", "AoTimeline",
    "AoBarChart", "AoLineChart", "AoPieChart", "AoDag", "AoDisclosure",
    "AoIf", "AoForm", "AoField",
}


class ActorVisualProfileError(Exception):
    """加载或校验 ActorVisualProfile 失败。"""


@dataclass
class FieldConstraint:
    """单个字段的类型约束（对应 profile JSON 中 fields.<name> 对象）。"""
    type: str                              # string / integer / number / boolean / array / object / enum
    required: bool = False
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    enum_values: list[str] | None = None   # 仅 type=enum 时使用
    items: dict | None = None              # 仅 type=array 时使用（item 类型）

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "FieldConstraint":
        if not isinstance(raw, dict):
            raise ActorVisualProfileError(
                f"field '{name}' constraint 必须是 object, got {type(raw).__name__}"
            )
        type_str = raw.get("type")
        if not type_str or type_str not in VALID_FIELD_TYPES:
            raise ActorVisualProfileError(
                f"field '{name}' type='{type_str}' 非法，有效值: {sorted(VALID_FIELD_TYPES)}"
            )
        return cls(
            type=type_str,
            required=bool(raw.get("required", False)),
            max_length=raw.get("max_length"),
            min=raw.get("min"),
            max=raw.get("max"),
            enum_values=raw.get("values") if type_str == "enum" else None,
            items=raw.get("items") if type_str == "array" else None,
        )

    def validate_value(self, name: str, value: Any) -> None:
        """校验单个值是否符合约束。

        容错策略：部分 LLM 提供商（如 MiniMax-M3）在 tool_call 参数序列化时
        会将所有值转为字符串（如 progress: "0" 而非 0）。此处对 integer/number/boolean
        类型做自动强制转换，仅当转换失败时才报错。
        """
        if value is None:
            if self.required:
                raise ActorVisualProfileError(f"field '{name}' 必填但为 null")
            return
        if self.type == "string":
            if not isinstance(value, str):
                raise ActorVisualProfileError(f"field '{name}' 应为 string, got {type(value).__name__}")
            if self.max_length is not None and len(value) > self.max_length:
                raise ActorVisualProfileError(
                    f"field '{name}' 长度 {len(value)} 超过 max_length {self.max_length}"
                )
        elif self.type in ("integer", "number"):
            # 容错：LLM 可能传字符串数字（如 "0" 而非 0）
            if isinstance(value, str) and not isinstance(value, bool):
                try:
                    value = int(value) if self.type == "integer" else float(value)
                except (ValueError, TypeError):
                    raise ActorVisualProfileError(
                        f"field '{name}' 应为 {self.type}, got str '{value}' 且无法转换"
                    )
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ActorVisualProfileError(
                    f"field '{name}' 应为 {'integer' if self.type == 'integer' else 'number'}, "
                    f"got {type(value).__name__}"
                )
            if self.min is not None and value < self.min:
                raise ActorVisualProfileError(f"field '{name}'={value} < min {self.min}")
            if self.max is not None and value > self.max:
                raise ActorVisualProfileError(f"field '{name}'={value} > max {self.max}")
        elif self.type == "boolean":
            # 容错：LLM 可能传 "true"/"false" 字符串
            if isinstance(value, str) and not isinstance(value, bool):
                if value.lower() in ("true", "1"):
                    value = True
                elif value.lower() in ("false", "0"):
                    value = False
                else:
                    raise ActorVisualProfileError(
                        f"field '{name}' 应为 boolean, got str '{value}'"
                    )
            if not isinstance(value, bool):
                raise ActorVisualProfileError(f"field '{name}' 应为 boolean, got {type(value).__name__}")
        elif self.type == "enum":
            if not isinstance(value, str):
                raise ActorVisualProfileError(f"field '{name}' 应为 enum string, got {type(value).__name__}")
            if self.enum_values and value not in self.enum_values:
                raise ActorVisualProfileError(
                    f"field '{name}'='{value}' 不在 enum {self.enum_values}"
                )
        elif self.type == "array":
            if not isinstance(value, list):
                raise ActorVisualProfileError(f"field '{name}' 应为 array, got {type(value).__name__}")
            if self.max_length is not None and len(value) > self.max_length:
                raise ActorVisualProfileError(
                    f"field '{name}' 数组长度 {len(value)} 超过 max_length {self.max_length}"
                )
            # 暂不递归校验 items（保持简单，array element 校验留给组件树层）
        elif self.type == "object":
            if not isinstance(value, dict):
                raise ActorVisualProfileError(f"field '{name}' 应为 object, got {type(value).__name__}")


@dataclass
class ViewDeclaration:
    """单个 view_id 的完整声明（白名单 + output_contract + fields + required_phases + template）。"""
    view_id: str
    output_contract: str | None
    fields: dict[str, FieldConstraint] = field(default_factory=dict)
    required_phases: list[str] = field(default_factory=list)
    description: str = ""
    template: list[dict[str, Any]] | None = None
    """OPT-1 view 模板：固定 A2UI 组件结构，属性值支持 ``$field`` 占位符。

    声明 template 后 agent 调 report_surface_state 只需传 view_id + phase +
    data_model（fields-only），组件树由 :func:`render_view_template` 确定性渲染，
    彻底消除 LLM 生成组件树的不稳定性。
    """

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ViewDeclaration":
        view_id = raw.get("view_id")
        if not view_id or not isinstance(view_id, str):
            raise ActorVisualProfileError(
                f"view 声明必须有 view_id (string)，got {raw.get('view_id')!r}"
            )
        if not re.match(r"^[a-z0-9][a-z0-9_-]{1,63}$", view_id):
            raise ActorVisualProfileError(
                f"view_id='{view_id}' 非法（必须 kebab-case，1-64 字符）"
            )
        output_contract = raw.get("output_contract")
        fields_raw = raw.get("fields", {}) or {}
        if not isinstance(fields_raw, dict):
            raise ActorVisualProfileError(
                f"view '{view_id}' fields 必须是 object, got {type(fields_raw).__name__}"
            )
        fields = {
            fname: FieldConstraint.from_dict(fname, fraw)
            for fname, fraw in fields_raw.items()
        }
        required_phases = raw.get("required_phases", []) or []
        if not isinstance(required_phases, list):
            raise ActorVisualProfileError(
                f"view '{view_id}' required_phases 必须是 array"
            )
        for p in required_phases:
            if p not in VALID_PHASES:
                raise ActorVisualProfileError(
                    f"view '{view_id}' required_phase='{p}' 非法，有效值: {sorted(VALID_PHASES)}"
                )
        template = cls._parse_template(view_id, raw.get("template"))
        return cls(
            view_id=view_id,
            output_contract=output_contract,
            fields=fields,
            required_phases=required_phases,
            description=raw.get("description", "") or "",
            template=template,
        )

    @staticmethod
    def _parse_template(
        view_id: str, template_raw: Any
    ) -> list[dict[str, Any]] | None:
        """解析并校验 view template（组件数组，属性值支持 $field 占位符）。"""
        if template_raw is None:
            return None
        if not isinstance(template_raw, list) or not template_raw:
            raise ActorVisualProfileError(
                f"view '{view_id}' template 必须是非空 array（A2UI 组件数组）"
            )
        # 此处仅做结构校验；$field 引用合法性放到 render 时校验
        for i, comp in enumerate(template_raw):
            if not isinstance(comp, dict):
                raise ActorVisualProfileError(
                    f"view '{view_id}' template[{i}] 必须是 object"
                )
            comp_type = comp.get("component")
            if not comp_type or comp_type not in KNOWN_A2UI_COMPONENTS:
                raise ActorVisualProfileError(
                    f"view '{view_id}' template[{i}].component='{comp_type}' "
                    f"不在 A2UI v1.0 catalog"
                )
            if not comp.get("id"):
                raise ActorVisualProfileError(
                    f"view '{view_id}' template[{i}] 缺 id 字段"
                )
        return template_raw

    def validate_data_model(self, data_model: dict[str, Any]) -> None:
        """校验 data_model 符合本 view 的字段约束，并就地修正类型（容错 LLM 传字符串数字）。"""
        if not isinstance(data_model, dict):
            raise ActorVisualProfileError(
                f"data_model 应为 dict, got {type(data_model).__name__}"
            )
        for fname, constraint in self.fields.items():
            if fname not in data_model:
                if constraint.required:
                    raise ActorVisualProfileError(
                        f"view '{self.view_id}' 必填字段 '{fname}' 缺失"
                    )
                continue
            original = data_model[fname]
            constraint.validate_value(fname, original)
            # 就地修正：integer/number/boolean 字符串 → 正确类型
            if isinstance(original, str) and not isinstance(original, bool):
                if constraint.type == "integer":
                    try:
                        data_model[fname] = int(original)
                    except (ValueError, TypeError):
                        pass
                elif constraint.type == "number":
                    try:
                        data_model[fname] = float(original)
                    except (ValueError, TypeError):
                        pass
                elif constraint.type == "boolean":
                    low = original.lower()
                    if low in ("true", "1"):
                        data_model[fname] = True
                    elif low in ("false", "0"):
                        data_model[fname] = False
        # 多余字段：允许，但 warning（不阻断）
        extra = set(data_model.keys()) - set(self.fields.keys())
        if extra:
            logger.warning(
                "view '%s' data_model 含未声明字段: %s（允许但不规范）",
                self.view_id,
                sorted(extra),
            )


@dataclass
class ActorVisualProfile:
    """L1.5 Worker Profile 层：单个 actor 的视觉配置。"""
    actor_id: str
    description: str = ""
    allowed_surface_views: dict[str, ViewDeclaration] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ActorVisualProfile":
        actor_id = raw.get("actor_id")
        if not actor_id or not isinstance(actor_id, str):
            raise ActorVisualProfileError(
                f"profile 必须有 actor_id (string)，got {raw.get('actor_id')!r}"
            )
        views_raw = raw.get("allowed_surface_views", []) or []
        if not isinstance(views_raw, list):
            raise ActorVisualProfileError(
                f"profile '{actor_id}' allowed_surface_views 必须是 array"
            )
        views: dict[str, ViewDeclaration] = {}
        for vraw in views_raw:
            view = ViewDeclaration.from_dict(vraw)
            if view.view_id in views:
                raise ActorVisualProfileError(
                    f"profile '{actor_id}' 含重复 view_id='{view.view_id}'"
                )
            views[view.view_id] = view
        return cls(
            actor_id=actor_id,
            description=raw.get("description", "") or "",
            allowed_surface_views=views,
        )

    def get_view(self, view_id: str) -> ViewDeclaration | None:
        return self.allowed_surface_views.get(view_id)

    def has_view(self, view_id: str) -> bool:
        return view_id in self.allowed_surface_views


# ── Profile 加载器 ─────────────────────────────────────────────


def _profile_path(actor_id: str) -> Path:
    """config/actors/<actor_id>/actor_visual_profile.json"""
    return Path(f"config/actors/{actor_id}/actor_visual_profile.json")


# ── 批量加载（GET /api/actors 用） ───────────────────────────────


def _actors_root() -> Path:
    """config/actors/ 目录路径。"""
    return Path("config/actors")


def list_actor_visual_profiles() -> list[ActorVisualProfile]:
    """扫描 config/actors/*/actor_visual_profile.json，返回全部 actor profile。

    与 ``load_actor_visual_profile`` 单个加载不同，本函数做目录扫描 + 错误隔离：
    单个 profile 损坏不影响其他加载。专供 GET /api/actors 用。

    Returns:
        按 actor_id 字典序排序的 profile 列表
    """
    root = _actors_root()
    if not root.exists():
        logger.debug("actors 目录不存在：%s，返回空列表", root)
        return []

    profiles: list[ActorVisualProfile] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        profile_file = entry / "actor_visual_profile.json"
        if not profile_file.exists():
            logger.debug("actor 目录 %s 无 actor_visual_profile.json，跳过", entry)
            continue
        try:
            profile = load_actor_visual_profile(entry.name)
        except ActorVisualProfileError as e:
            logger.warning(
                "加载 actor '%s' profile 失败：%s（跳过但不影响其他）",
                entry.name,
                e,
            )
            continue
        profiles.append(profile)
    return profiles


def load_actor_visual_profile(actor_id: str) -> ActorVisualProfile:
    """加载指定 actor 的视觉 profile。

    文件不存在时返回空 profile（actor 没声明 view_id 白名单 → 所有 view 都被拒绝，
    强制显式声明 → 防止误用）。

    加载失败抛 ActorVisualProfileError。
    """
    path = _profile_path(actor_id)
    if not path.exists():
        logger.debug(
            "actor '%s' 无 actor_visual_profile.json（路径=%s），返回空 profile",
            actor_id,
            path,
        )
        return ActorVisualProfile(actor_id=actor_id)
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ActorVisualProfileError(
            f"加载 {path} 失败: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise ActorVisualProfileError(
            f"{path} 必须是 YAML object, got {type(raw).__name__}"
        )
    profile = ActorVisualProfile.from_dict(raw)
    if profile.actor_id != actor_id:
        raise ActorVisualProfileError(
            f"{path} 声明 actor_id='{profile.actor_id}' 与目录 '{actor_id}' 不一致"
        )
    return profile


# ── Digest 计算（surface_id content-addressed） ─────────────────


def compute_surface_id(view_id: str, phase: str, data_model: dict[str, Any]) -> str:
    """计算 surface_id = sha256(view_id + phase + canonical_json(data_model))。

    canonical_json：sort_keys + ensure_ascii=False，保证相同内容产生相同 hash。
    """
    canonical = json.dumps(data_model, sort_keys=True, ensure_ascii=False)
    raw = f"{view_id}|{phase}|{canonical}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_surface_id_identity(
    run_id: str,
    actor_id: str,
    view_id: str,
    generation: int | str | None = None,
) -> str:
    """身份派生 surface_id（Worker identity 注入）。

    与 content-addressed 的 :func:`compute_surface_id` 不同，本函数不 hash 数据
    内容，而是 hash surface 身份（run / actor / view / generation）：

    - ``generation=None`` → 稳定 surface：同一 (run, actor, view) 复用同一
      surface_id，phase 单调推进靠 phase 字段区分（同一 surface 多次更新，前端一张卡推进）。
    - ``generation=int`` → 独立 surface 实例：present_content 每次调用用
      ``len(state.emitted_widgets)`` 作为递增序号，每次生成新卡片。
    - ``generation=str`` → 命名 surface 实例：present_content 带 widget_id
      调用时（"同 id 替换"语义），同名的调用复用同一 surface（一张卡原地更新）。

    这是解决「多次展示相同/不同数据被合并为一张卡」的关键：surface 标识不再
    由内容决定，而由调用身份决定。
    """
    gen = "" if generation is None else str(generation)
    raw = f"{run_id}|{actor_id}|{view_id}|{gen}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_components_digest(components: list[dict]) -> str:
    """组件树 digest（用于校验 components 是否变化，前端据次决定是否重渲染）。"""
    canonical = json.dumps(components, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Components 校验（A2UI v1.0 catalog 已知组件名） ───────────────


# 各组件允许的字段白名单（对照 web/src/lib/a2ui/schemas.ts componentSchema 定义）
# additionalProperties: false → 多余字段必须移除，否则前端 schema 校验报错
_COMPONENT_FIELDS_WHITELIST: dict[str, set[str]] = {
    "Text": {"text", "variant"},
    "Image": {"url", "description", "fit", "variant"},
    "Icon": {"name"},
    "Video": {"url", "description"},
    "AudioPlayer": {"url", "description"},
    "Row": {"children", "gap", "align"},
    "Column": {"children", "gap", "align"},
    "List": {"children"},
    "Card": {"child"},
    "Tabs": {"tabs"},
    "Modal": {"trigger", "content"},
    "Divider": {"axis"},
    "Button": {"child", "variant", "action"},
    "TextField": {"label", "value", "placeholder", "variant"},
    "CheckBox": {"label", "value"},
    "ChoicePicker": {"label", "variant", "options", "value", "displayStyle"},
    "Slider": {"label", "value", "min", "max", "step"},
    "DateTimeInput": {"label", "value", "variant"},
    # Ao 前缀组件（与前端 schemas.ts componentSchema 定义对齐）
    "AoGrid": {"children", "columns", "gap", "align"},
    "AoGridItem": {"child", "span"},
    "AoSection": {"title", "children", "tone"},
    "AoMetric": {"label", "value", "unit", "tone"},
    "AoStatusBadge": {"text", "tone"},
    "AoProgress": {"label", "value", "tone"},
    "AoStep": {"index", "label", "detail", "tone", "child"},
    "AoList": {"source", "itemTitlePath", "itemDetailPath", "itemBadgePath", "itemStatusPath"},
    "AoTable": {"source", "columns"},
    "AoTimeline": {"source", "itemTitlePath", "itemDetailPath", "itemTimePath", "itemStatusPath"},
    "AoBarChart": {"source", "itemLabelPath", "itemValuePath", "itemTonePath"},
    "AoDag": {"source", "itemIdPath"},
    # OPT-1 修正：前端 schemas.ts AoDisclosure = {title, children, open}（required title+children）。
    # 旧白名单 {summary, child} 与前端不一致 → normalize 剥离 title 后前端校验降级。
    # 保留 summary/child 兼容历史 payload，但新模板一律用 title/children。
    "AoDisclosure": {"title", "children", "open", "summary", "child"},
    "AoLink": {"url", "label"},
    "AoArtifact": {"artifactId", "title"},
    "AoIf": {"condition", "child"},
}


def _coerce_numeric(value: Any) -> Any:
    """尝试将字符串数字转为 int/float（LLM 常见问题：value='50' 而非 50）。"""
    if isinstance(value, str) and not isinstance(value, bool):
        try:
            if "." in value:
                return float(value)
            return int(value)
        except (ValueError, TypeError):
            pass
    return value


def _normalize_single_component(comp: dict, i: int, prefix: str = "") -> tuple[dict, list[dict]]:
    """规范化单个 component，返回 (normalized_component, child_components)。

    child_components 是递归规范化后的子组件列表（已展平），调用方需追加到顶层。
    """
    # 提取 component type（兼容 type / component 两种字段名）
    comp_type = comp.get("component") or comp.get("type")
    if not comp_type:
        raise ActorVisualProfileError(f"components[{i}] 缺 type/component 字段")
    # Legacy 前缀归一化：历史 session 数据中偶发的旧版组件名前缀 → Ao（当前命名）。
    # 避免白名单校验失败。
    if isinstance(comp_type, str) and comp_type.startswith("Hr"):
        ao_type = "Ao" + comp_type[2:]
        if ao_type in KNOWN_A2UI_COMPONENTS:
            comp_type = ao_type
    if comp_type not in KNOWN_A2UI_COMPONENTS:
        raise ActorVisualProfileError(
            f"components[{i}].component='{comp_type}' 不在 A2UI v1.0 catalog "
            f"（已知: {sorted(KNOWN_A2UI_COMPONENTS)}）"
        )

    # 展开 props 嵌套（LLM 常用 {"type":"AoSection","props":{"title":"..."}}）
    props = comp.get("props", {})
    if not isinstance(props, dict):
        props = {}

    # 合并：顶层字段优先（排除保留字段），然后 props 覆盖
    merged: dict[str, Any] = {}
    for k, v in props.items():
        merged[k] = v
    for k, v in comp.items():
        if k not in ("type", "component", "props", "id"):
            merged[k] = v  # 顶层字段覆盖 props（更显式）

    # 生成 id
    comp_id = comp.get("id") or f"{str(comp_type).lower()}-{prefix}{i}"

    # 构建规范 component，只保留白名单字段
    result: dict[str, Any] = {"id": comp_id, "component": comp_type}
    allowed = _COMPONENT_FIELDS_WHITELIST.get(comp_type, set())

    # 先处理 children：如果是对象数组（LLM 原始格式），递归规范化并展平
    extra_children: list[dict] = []
    if "children" in merged and isinstance(merged["children"], list):
        raw_children = merged["children"]
        if raw_children and all(isinstance(c, dict) for c in raw_children):
            # children 是对象数组 → 递归规范化，展平到顶层，用 ID 引用
            child_ids: list[str] = []
            for ci, child in enumerate(raw_children):
                if not isinstance(child, dict):
                    continue
                child_norm, child_grandchildren = _normalize_single_component(
                    child, ci, prefix=f"{prefix}{i}_"
                )
                extra_children.append(child_norm)
                extra_children.extend(child_grandchildren)
                child_ids.append(child_norm["id"])
            result["children"] = child_ids
        elif all(isinstance(c, str) for c in raw_children):
            # children 已经是 ID 字符串数组
            result["children"] = raw_children
        else:
            # 混合或空数组
            result["children"] = [c for c in raw_children if isinstance(c, str)]

    # 处理其他白名单字段
    for k, v in merged.items():
        if k == "children":
            continue  # 已单独处理
        if k in allowed:
            # 数值字段做类型转换
            if k in ("value", "min", "max", "step", "span"):
                v = _coerce_numeric(v)
            result[k] = v

    # 组件特定补全：确保 required 字段存在（旧版前缀已在上方归一化为 Ao）
    if comp_type == "AoGrid":
        # 前端 gridColumnsSchema 要求 columns 必须同时含 default + compact
        # （缺 compact 会 AJV 校验失败导致整卡降级）。模板/LLM 常只写 default。
        cols = result.get("columns")
        if not isinstance(cols, dict):
            cols = {}
        cols.setdefault(
            "default", min(max(len(result.get("children") or []), 1), 3)
        )
        cols.setdefault("compact", 1)
        result["columns"] = cols
    elif comp_type == "AoSection" and "children" not in result:
        result["children"] = []
    elif comp_type == "AoStatusBadge":
        # LLM 常用 label 而非 text
        if "text" not in result and "label" in merged:
            result["text"] = merged["label"]
        elif "text" not in result:
            result["text"] = ""
    elif comp_type == "Text":
        if "text" not in result:
            result["text"] = ""
    elif comp_type == "AoProgress":
        if "value" not in result:
            result["value"] = 0
    elif comp_type == "AoMetric":
        if "label" not in result:
            result["label"] = ""
        if "value" not in result:
            result["value"] = 0
    elif comp_type == "AoStep":
        # AoStep requires index, label, child
        if "index" not in result:
            result["index"] = i
        if "label" not in result:
            result["label"] = merged.get("title", "") or merged.get("text", "")
        if "child" not in result:
            # Create a placeholder Text child component
            child_id = f"text-{prefix}{i}_child"
            child_comp = {"id": child_id, "component": "Text", "text": result.get("label", "")}
            extra_children.append(child_comp)
            result["child"] = child_id
    elif comp_type in ("AoList", "AoTable", "AoTimeline", "AoBarChart", "AoDag"):
        # LLM 常用 items/columns 直接放数据，需转成 source 数据绑定
        if "source" not in result:
            items_data = merged.get("items") or merged.get("data") or []
            # 容错：LLM（如 MiniMax-M3）可能生成 {"item": [...]} 或嵌套 {"item":{"item":[...]}} 包裹格式
            # 前端 source path 要求解析为数组，需递归提取内部数组
            if isinstance(items_data, dict):
                def _extract_array_deep(val: Any) -> list | None:
                    if isinstance(val, list):
                        return val
                    if isinstance(val, dict):
                        for _v in val.values():
                            _found = _extract_array_deep(_v)
                            if _found is not None:
                                return _found
                    return None
                items_data = _extract_array_deep(items_data) or []
            if isinstance(items_data, list) and items_data:
                # 把 items 放到 data_model 中（由调用方注入）
                result["_inline_data"] = items_data  # 临时字段，调用方会处理
                result["source"] = {"path": f"/_inline_{comp_type}_{prefix}{i}"}
                if "itemTitlePath" not in result:
                    result["itemTitlePath"] = "/"
                if comp_type == "AoTable" and "columns" not in result:
                    # 生成默认 columns（同时补全 id/label 字段满足 schema）
                    if items_data and isinstance(items_data[0], dict):
                        result["columns"] = [
                            {"id": k, "label": k, "path": f"/{k}"}
                            for k in items_data[0].keys()
                        ]

    return result, extra_children


def normalize_components(components: list[dict]) -> list[dict]:
    """规范化 A2UI components，兼容 LLM 常见格式偏差。

    LLM（特别是 MiniMax-M3）生成的 components 常见问题：
    1. 用 `type` 而非 `component` 字段名
    2. 嵌套 `props` 对象而非扁平字段（如 {"type":"AoProgress","props":{"value":"50"}}）
    3. 缺少 `id` 字段
    4. 数值字段传成字符串（如 "50" 而非 50）
    5. 多余字段（前端 schema additionalProperties: false 会报错）
    6. children 嵌套原始组件对象而非 ID 引用（需递归展平）

    本函数做以下转换：
    - type → component（重命名）
    - 展开 props 嵌套到顶层
    - 自动生成 id（如 "aoprogress-0"）
    - 数值字段字符串 → int/float
    - 移除不在白名单中的多余字段
    - children 中的对象递归规范化并展平到顶层，用 ID 引用
    """
    if not isinstance(components, list):
        return components

    normalized: list[dict] = []
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            continue
        result, extra_children = _normalize_single_component(comp, i)
        normalized.append(result)
        normalized.extend(extra_children)

    return normalized


def validate_components(components: list[dict]) -> None:
    """校验 components 是合法 A2UI 组件树。

    兼容 type / component 两种字段名（normalize 后用 component，原始 LLM 输出用 type）。
    禁止空数组——LLM 必须提供至少 1 个 A2UI 组件，否则前端无组件可渲染会降级为文本。
    """
    if not isinstance(components, list):
        raise ActorVisualProfileError(
            f"components 应为 array, got {type(components).__name__}"
        )
    if len(components) == 0:
        raise ActorVisualProfileError(
            "components 不能为空数组——必须提供至少 1 个 A2UI 组件"
            "（如 AoProgress / AoSection / AoList / AoStatusBadge 等），"
            "否则前端无法渲染交互信息"
        )
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            raise ActorVisualProfileError(
                f"components[{i}] 应为 object, got {type(comp).__name__}"
            )
        comp_type = comp.get("component") or comp.get("type")
        if not comp_type:
            raise ActorVisualProfileError(f"components[{i}] 缺 type/component 字段")
        if comp_type not in KNOWN_A2UI_COMPONENTS:
            raise ActorVisualProfileError(
                f"components[{i}].component='{comp_type}' 不在 A2UI v1.0 catalog "
                f"（已知: {sorted(KNOWN_A2UI_COMPONENTS)}）"
            )


# ── Phase 单调推进校验 ─────────────────────────────────────────


def validate_phase_monotonic(
    view_id: str,
    new_phase: str,
    last_phase_by_view: dict[str, str],
) -> None:
    """校验 phase 单调推进（per-view 维度）。

    推进顺序：started (0) → partial (1) → final (2) → superseded (3)
    superseded 可以从任意阶段进入（标记旧 surface 被新 surface 替代）
    """
    if new_phase not in VALID_PHASES:
        raise ActorVisualProfileError(
            f"phase='{new_phase}' 非法，有效值: {sorted(VALID_PHASES)}"
        )
    last_phase = last_phase_by_view.get(view_id)
    if last_phase is None:
        return  # 首次 emit，不需要校验
    last_rank = PHASE_ORDER.get(last_phase, -1)
    new_rank = PHASE_ORDER.get(new_phase, -1)
    # superseded 允许从任意阶段进入
    if new_phase == "superseded":
        return
    if new_rank < last_rank:
        raise ActorVisualProfileError(
            f"view '{view_id}' phase 回退: {last_phase} ({last_rank}) → "
            f"{new_phase} ({new_rank})，必须单调推进"
        )


# ── OPT-1 view 模板渲染（fields-only 模式核心） ───────────────────


def field_default_value(constraint: FieldConstraint) -> Any:
    """按字段约束生成类型默认值（模板 $field 占位符缺失时的兜底）。"""
    if constraint.type == "string":
        return ""
    if constraint.type in ("integer", "number"):
        return 0
    if constraint.type == "boolean":
        return False
    if constraint.type == "enum":
        return constraint.enum_values[0] if constraint.enum_values else "neutral"
    if constraint.type == "array":
        return []
    if constraint.type == "object":
        return {}
    return None


def make_skeleton_data_model(view: ViewDeclaration) -> dict[str, Any]:
    """生成骨架 data_model：全部字段填类型默认值（系统投影 source=system 用）。

    title 类 string 字段默认用 view_id（卡片标题可读），其余 string 默认空串。
    """
    skeleton: dict[str, Any] = {}
    for fname, constraint in view.fields.items():
        if fname == "title" and constraint.type == "string":
            skeleton[fname] = f"{view.view_id} · 等待 actor 数据"
        else:
            skeleton[fname] = field_default_value(constraint)
    return skeleton


def render_view_template(
    view: ViewDeclaration, data_model: dict[str, Any]
) -> list[dict]:
    """把 view.template 中的 ``$field`` 占位符替换为 data_model 实际值。

    占位符语义：组件属性值为 ``"$fieldname"``（字符串，$ 开头）→ 取
    ``data_model[fieldname]``；字段缺失时按 view.fields 约束填类型默认值
    （未声明的字段名 → 报错，防止模板与 fields 脱节静默渲染空值）。

    Returns:
        normalize + validate 后的规范组件树（可直接作为 SurfaceState.components）

    Raises:
        ActorVisualProfileError: 模板为空 / $field 引用未声明字段 / 组件校验失败
    """
    if not view.template:
        raise ActorVisualProfileError(
            f"view '{view.view_id}' 未声明 template，无法 fields-only 渲染"
        )

    # 预解析默认值表（$field 缺失时兜底）
    defaults = {
        fname: field_default_value(constraint)
        for fname, constraint in view.fields.items()
    }

    rendered = copy.deepcopy(view.template)
    for comp in rendered:
        for key, val in list(comp.items()):
            if not isinstance(val, str) or not val.startswith("$"):
                continue
            fname = val[1:]
            if fname not in view.fields:
                raise ActorVisualProfileError(
                    f"view '{view.view_id}' template 引用未声明字段 '${fname}'"
                    f"（component id='{comp.get('id')}' 属性 '{key}'）"
                )
            comp[key] = data_model.get(fname, defaults[fname])

    rendered = normalize_components(rendered)
    validate_components(rendered)
    return rendered


# ── ToolDefinition 工厂（DagEngine 集成用） ─────────────────────────


def make_report_surface_state_tool(
    actor_id: str,
    run_id: str | None = None,
    event_sink: "Callable[[DagEvent], Awaitable[None]] | None" = None,
    node_id: str | None = None,
):
    """为指定 actor 构造 report_surface_state ToolDefinition。

    actor_id 烘焙进 handler（agent 调工具时不必再传 actor_id），
    input_schema 的 view_id 用 enum 限定白名单，让 LLM 在 function calling
    阶段就看到合法值。

    run_id 通过闭包注入到返回值的 surface_state.run_id 字段（不是
    SurfaceState 的字段，而是 payload metadata，让前端关联到具体 run）。

    v99.5 P0.15 — Phase 5 starter：
    如果 event_sink 不为 None，工具通过校验后（ok=True 且非 deduplicated）
    会 emit 一个 DagEvent(type=REPORT_SURFACE_STATE, surface_state=...) 到
    event_sink，让 SSE 通道把 snapshot 推到前端 SupervisionPanel。
    校验失败 / 重复 emit 不发事件（保持 reducer 单调性 + 避免污染 SSE）。

    Args:
        actor_id: actor ID（与 config/actors/<actor_id>/ 目录一致）
        run_id: 当前 run ID（注入到返回 payload，方便前端路由）
        event_sink: DagEngine.event_sink（可选；提供则发 DagEvent）
        node_id: 当前 DAG 节点 ID（注入到 DagEvent.node_id，让前端按节点路由）

    Returns:
        ToolDefinition 实例，可直接 append 到 agent 的 tools 列表
    """
    # 导入放这里避免循环依赖（tools/report_surface_state 也会 import 此模块；
    # protocol.py 也会被 harness 反向引用 → 延迟 import）。
    from harness.protocol import ToolDefinition
    from orchestrator.protocol import DagEvent, DagEventType, SurfaceState
    from tools.report_surface_state import report_surface_state

    # 加载 profile 拿 view_id 白名单（用于 schema enum）
    profile = load_actor_visual_profile(actor_id)
    allowed_views = sorted(profile.allowed_surface_views.keys())

    # OPT-1 fields-only 模式：全部 view 声明 template 时，schema 不暴露 components，
    # LLM 只传 view_id + phase + data_model，组件树由模板确定性渲染
    all_templated = bool(profile.allowed_surface_views) and all(
        v.template is not None for v in profile.allowed_surface_views.values()
    )

    view_list_str = ", ".join(allowed_views) if allowed_views else "(none declared)"

    # fields 提示（fields-only 模式下 LLM 需要知道每个 view 的字段约束）
    fields_hint = ""
    if all_templated:
        parts: list[str] = []
        for vid in allowed_views:
            vdecl = profile.allowed_surface_views[vid]
            flines = []
            for fname, fc in vdecl.fields.items():
                req_mark = "必填" if fc.required else "可选"
                type_desc = fc.type if fc.type != "enum" else f"enum{fc.enum_values}"
                extra = ""
                if fc.max_length is not None:
                    extra += f", ≤{fc.max_length}字符"
                if fc.min is not None:
                    extra += f", ≥{fc.min}"
                if fc.max is not None:
                    extra += f", ≤{fc.max}"
                flines.append(f"    {fname}: {type_desc} ({req_mark}{extra})")
            parts.append(f"  view_id='{vid}':\n" + "\n".join(flines))
        fields_hint = "\n各 view 字段约束（data_model 只传这些字段值）：\n" + "\n".join(parts)

    if all_templated:
        description = (
            f"Emit surface snapshot for actor '{actor_id}'（OPT-1 fields-only 模式）。"
            f"只需传 view_id + phase + data_model，组件树由 view 模板自动渲染，"
            f"不要传 components。"
            f"Allowed view_ids: [{view_list_str}]。"
            f"phase 单调推进（started → partial → final）。"
            f"{fields_hint}"
        )
        input_schema_props = {
            "view_id": {
                "type": "string",
                "enum": allowed_views,
                "description": f"view ID（白名单: {view_list_str}）",
            },
            "phase": {
                "type": "string",
                "enum": ["started", "partial", "final", "superseded"],
                "description": "阶段（单调推进）",
            },
            "data_model": {
                "type": "object",
                "description": "业务字段值（符合 view fields 约束，见上方字段表）",
            },
        }
        input_schema_required = ["view_id", "phase", "data_model"]
    else:
        description = (
            f"Emit partial surface snapshot for actor '{actor_id}'. "
            f"Allowed view_ids: [{view_list_str}]. "
            f"view_id 在 actor allowed_surface_views 白名单外 → 拒绝。"
            f"phase 必须单调推进（started → partial → final）。"
            f"相同 surface_id 不重复 emit（digest pinning）。"
            f"\n\ncomponents 是 dict 对象数组（不是 string 数组！），每个元素结构："
            f'  {{"id": "唯一ID", "component": "组件名", ...字段}}。'
            f"\n常用组件与必填字段："
            f'\n  AoProgress: {{"id":"p1","component":"AoProgress","label":"进度","value":50}}'
            f'\n  AoSection: {{"id":"s1","component":"AoSection","title":"标题","children":[]}}'
            f'\n  AoList: {{"id":"l1","component":"AoList","source":{{"path":"/items"}},"itemTitlePath":"/title"}}'
            f'\n  AoStatusBadge: {{"id":"b1","component":"AoStatusBadge","text":"运行中","tone":"info"}}'
            f'\n  AoMetric: {{"id":"m1","component":"AoMetric","label":"指标","value":42,"unit":"个"}}'
            f'\n  Text: {{"id":"t1","component":"Text","text":"说明文字"}}'
        )
        input_schema_props = {
            "view_id": {
                "type": "string",
                "enum": allowed_views,
                "description": f"view ID（白名单: {view_list_str}）",
            },
            "phase": {
                "type": "string",
                "enum": ["started", "partial", "final", "superseded"],
                "description": "阶段（单调推进）",
            },
            "components": {
                "type": "array",
                "description": "A2UI 组件树（dict 对象数组，每个元素至少含 id + component 字段）。"
                    "示例: [{\"id\":\"p1\",\"component\":\"AoProgress\",\"label\":\"进度\",\"value\":50},"
                    " {\"id\":\"s1\",\"component\":\"AoSection\",\"title\":\"标题\",\"children\":[]}]。"
                    "可用组件: AoProgress/AoSection/AoList/AoStatusBadge/AoMetric/AoStep/AoTable/AoTimeline/AoDag/Text 等。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "组件唯一 ID"},
                        "component": {
                            "type": "string",
                            "description": "组件类型（如 AoProgress/AoSection/AoList）",
                        },
                    },
                    "required": ["id", "component"],
                },
            },
            "data_model": {
                "type": "object",
                "description": "数据模型（符合 view fields 约束）",
            },
            "surface_properties": {
                "type": "object",
                "description": "可选展示属性 {iconUrl, agentDisplayName}",
            },
            "output_contract": {
                "type": "string",
                "description": "可选，contract 类别（ActorReport / Mission / Failure / RoundGate）",
            },
        }
        input_schema_required = ["view_id", "phase", "components", "data_model"]

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        # 注入 actor_id（agent 不必传；防止 agent 错传其他 actor）
        args_with_actor = dict(args)
        args_with_actor.setdefault("actor_id", actor_id)
        result = await report_surface_state(args_with_actor)
        # 注入 run_id 到返回（前端据次路由到具体 run）
        if run_id and isinstance(result, dict):
            result["run_id"] = run_id

        # v99.5 P0.15：仅当校验通过 + 非 dedup 时 emit DagEvent。
        # 校验失败 → result["ok"]=False；dedup → result["deduplicated"]=True
        if (
            event_sink is not None
            and isinstance(result, dict)
            and result.get("ok") is True
            and not result.get("deduplicated", False)
            and isinstance(result.get("surface"), dict)
        ):
            try:
                surface_state = SurfaceState.from_payload(result["surface"])
                await event_sink(
                    DagEvent(
                        type=DagEventType.REPORT_SURFACE_STATE,
                        run_id=run_id or "",
                        node_id=node_id,
                        payload={
                            "surface_state": surface_state.to_payload(),
                            "actor_id": actor_id,
                            "view_id": surface_state.view_id,
                            "phase": surface_state.phase,
                            "source": surface_state.source,
                        },
                        surface_state=surface_state,
                    )
                )
            except Exception as e:
                # emit 失败不应阻断工具返回（agent 仍然看到 ok=True）
                logger.warning(
                    "report_surface_state emit DagEvent 失败（不影响 agent 工具返回）: %s",
                    e,
                )
        return result

    return ToolDefinition(
        name="report_surface_state",
        description=description,
        input_schema={
            "type": "object",
            "properties": input_schema_props,
            "required": input_schema_required,
        },
        handler=handler,
    )


def resolve_actor_id_from_node(node: Any) -> str | None:
    """从 WorkflowNode 推导 actor_id（多优先级回退）。

    优先级：
      1. node.business_role（最直接，actor 命名松散即可）
      2. node.actor_id（如未来扩展）
      3. node.agent（向后兼容：现有 agent_id 通常与 actor 命名一致）
      4. node.id（最后回退）

    返回 None 表示无法推导（profile 加载将返回空 → 工具不注入）。
    """
    # 优先级 1：business_role（v2.1 已存在的字段）
    role = getattr(node, "business_role", None)
    if role and isinstance(role, str) and role.strip():
        return role.strip()
    # 优先级 2：actor_id 字段（如未来添加）
    actor_id = getattr(node, "actor_id", None)
    if actor_id and isinstance(actor_id, str) and actor_id.strip():
        return actor_id.strip()
    # 优先级 3：agent_id（向后兼容）
    agent = getattr(node, "agent", None)
    if agent and isinstance(agent, str) and agent.strip():
        return agent.strip()
    # 优先级 4：node.id
    nid = getattr(node, "id", None)
    if nid and isinstance(nid, str) and nid.strip():
        return nid.strip()
    return None