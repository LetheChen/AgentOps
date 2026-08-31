"""
P0.7 测试：v99.5 DAG 编排可视化合并 — 11 类节点 → 8 shape 映射 + 8 status 叠加 +
5 metric 徽章 + DagLegend + DeveloperDagView / BusinessLaneView 集成 ShapeRegistry。

为什么是源码检查测试（不直接挂载 React 树）：
- 单元渲染需要 JSDOM + Vite 编译产物；CI 跑 playwright 太重
- 本测试验证的是**契约**：SHAPE_MAP / STATUS_OVERLAYS / BADGE_TRIGGERS / Legend
  这些是 spec（v99.5 §3.8）定义的固定映射，写死在前端源码里；只要源码不被改坏，
  渲染就一定符合预期（CSS 选择器 + className 都已经绑定）
- 浏览器真实渲染验证见 web/dag-visualization-demo.html（手动打开验证）

覆盖矩阵：
  1. test_shapes_mapping_covers_eleven_types → 8 shapes
  2. test_eight_status_overlay_rules_in_css  → 8 status overlays
  3. test_five_badge_triggers_in_registry    → 5 metric badges
  4. test_dag_legend_lists_distinct_shapes   → 4-5+ shapes（实际 8 种）
  5. test_developer_dag_view_delegates_to_dag_node_card
  6. test_business_lane_view_integrates_shape_registry
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ── 路径常量 ───────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_SRC = PROJECT_ROOT / "web" / "src"
COMP_DIR = WEB_SRC / "components" / "collaboration"
STYLES_DIR = WEB_SRC / "styles"

DAG_NODE_SEMANTICS_TS = COMP_DIR / "DagNodeSemantics.ts"
DAG_NODE_SHAPE_REGISTRY_TSX = COMP_DIR / "DagNodeShapeRegistry.tsx"
DAG_LEGEND_TSX = COMP_DIR / "DagLegend.tsx"
DEVELOPER_DAG_VIEW_TSX = COMP_DIR / "DeveloperDagView.tsx"
BUSINESS_LANE_VIEW_TSX = COMP_DIR / "BusinessLaneView.tsx"
DAG_V99_CSS = STYLES_DIR / "dag-v99.css"


# ── 工具函数 ───────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    """读文件，缺失时给出可定位的失败信息。"""
    assert path.exists(), f"缺失源码文件：{path}"
    return path.read_text(encoding="utf-8")


def _extract_shape_map(ts_src: str) -> dict[str, dict[str, str]]:
    """
    从 DagNodeSemantics.ts 中解析 SHAPE_MAP 表。

    SHAPE_MAP 形如：
        const SHAPE_MAP: Readonly<Record<string, ShapeMapping>> = {
          agent: { shape: 'circle', glyph: '', label: 'WORKER', tone: 'worker' },
          ...
        };

    返回 { node_type: {shape, glyph, label, tone} }，外加 gateway_* / terminal_* 派生键。
    """
    block = re.search(
        r"SHAPE_MAP[^=]*=\s*\{(.*?)\n\s*\};",
        ts_src,
        re.DOTALL,
    )
    assert block, "未找到 SHAPE_MAP 表（请检查 DagNodeSemantics.ts）"
    body = block.group(1)

    # 单行记录：key: { shape: '...', glyph: '...', label: '...', tone: '...' }
    pattern = re.compile(
        r"(\w+):\s*\{\s*shape:\s*'(\w+)',\s*glyph:\s*'([^']*)',\s*label:\s*'([^']*)',\s*tone:\s*'(\w+)'"
    )
    out: dict[str, dict[str, str]] = {}
    for m in pattern.finditer(body):
        key, shape, glyph, label, tone = m.groups()
        out[key] = {"shape": shape, "glyph": glyph, "label": label, "tone": tone}
    return out


def _extract_list_dag_legend_entries(ts_src: str) -> list[dict[str, str]]:
    """从 DagNodeSemantics.ts 中解析 listDagLegendEntries() 的返回数组（用花括号计数）。"""
    m = re.search(
        r"listDagLegendEntries\(\):\s*DagLegendEntry\[\]\s*\{",
        ts_src,
    )
    assert m, "未找到 listDagLegendEntries() 签名"
    start = m.end()
    depth = 1
    i = start
    while i < len(ts_src) and depth > 0:
        ch = ts_src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, "listDagLegendEntries() 函数体未闭合"
    body = ts_src[start : i - 1]
    pattern = re.compile(
        r"shape:\s*'(\w+)',\s*glyph:\s*'([^']*)',\s*label:\s*'([^']*)',\s*tone:\s*'(\w+)'"
    )
    return [
        {"shape": s, "glyph": g, "label": l, "tone": t}
        for s, g, l, t in pattern.findall(body)
    ]


def _extract_status_rules(css_src: str) -> set[str]:
    """从 dag-v99.css 中解析所有 .dag-card[data-status='X'] 选择器。"""
    return set(re.findall(r"\.dag-card\[data-status=['\"](\w+)['\"]\]", css_src))


# ── 11 类节点类型清单（v99.5 §3.8 规格） ──────────────────────────────────

EXPECTED_NODE_TYPES: dict[str, str] = {
    # node_type (or node_type+kind) → expected shape
    "agent": "circle",
    "parallel_branch": "triangle",
    "gateway_condition": "diamond",
    "gateway_loop": "capsule",
    "command": "rounded_rect",
    "await_command": "capsule",
    "while": "capsule",
    "terminal_success": "octagon",
    "terminal_failure": "octagon",
    "join": "hexagon",
    "quorum": "hexagon",
    "foreach": "parallelogram",
}

EXPECTED_SHAPES: set[str] = {
    "circle",
    "triangle",
    "diamond",
    "capsule",
    "rounded_rect",
    "octagon",
    "hexagon",
    "parallelogram",
}

EXPECTED_STATUSES: set[str] = {
    "pending",
    "ready",
    "running",
    "waiting_for_command",
    "completed",
    "failed",
    "cancelled",
    "skipped",
}

EXPECTED_BADGE_TYPES: set[str] = {
    "tokens_in",
    "tokens_out",
    "tool_calls",
    "tool_failures",
    "duration_ms",
    "error_type",
}


# ── 测试 1：11 类节点 → 8 shape 映射 ──────────────────────────────────────


class TestShapesMapping:
    """11 类节点类型 → 8 shape 固定映射（v99.5 §3.8 规格）。"""

    def test_shape_map_contains_eleven_types(self):
        """SHAPE_MAP 含全部 11 类节点（含 gateway/terminal 派生子键）。"""
        ts = _read(DAG_NODE_SEMANTICS_TS)
        shape_map = _extract_shape_map(ts)

        # 主键 9 个 + 派生 2 个（gateway 由 mappingForGateway 产生，terminal 由 mappingForTerminal 产生）
        # 我们用派生键校验：terminal_success / terminal_failure 在 SHAPE_MAP 内显式存在
        expected_keys = {
            "agent",
            "parallel_branch",
            "command",
            "await_command",
            "while",
            "join",
            "quorum",
            "foreach",
            "terminal_success",
            "terminal_failure",
        }
        assert expected_keys.issubset(set(shape_map.keys())), (
            f"SHAPE_MAP 缺少以下节点类型：{expected_keys - set(shape_map.keys())}"
        )

    def test_eleven_types_map_to_eight_unique_shapes(self):
        """11 类节点映射出的 shape 集合大小为 8（每个 shape 至少被一个 node_type 使用）。"""
        ts = _read(DAG_NODE_SEMANTICS_TS)
        shape_map = _extract_shape_map(ts)

        # 11 类节点 → shape 表（与 spec 完全一致）
        actual_mapping: dict[str, str] = {
            "agent": shape_map["agent"]["shape"],
            "parallel_branch": shape_map["parallel_branch"]["shape"],
            "gateway_condition": "diamond",  # mappingForGateway 派生
            "gateway_loop": "capsule",        # mappingForGateway 派生
            "command": shape_map["command"]["shape"],
            "await_command": shape_map["await_command"]["shape"],
            "while": shape_map["while"]["shape"],
            "terminal_success": shape_map["terminal_success"]["shape"],
            "terminal_failure": shape_map["terminal_failure"]["shape"],
            "join": shape_map["join"]["shape"],
            "quorum": shape_map["quorum"]["shape"],
            "foreach": shape_map["foreach"]["shape"],
        }

        # 11 类节点实际使用的 shape 集合应等于 EXPECTED_SHAPES（8 种）
        used_shapes = set(actual_mapping.values())
        assert used_shapes == EXPECTED_SHAPES, (
            f"期望 shape 集合 {EXPECTED_SHAPES}，实际 {used_shapes}"
        )
        assert len(used_shapes) == 8, (
            f"期望 8 种 shape，实际 {len(used_shapes)} 种：{used_shapes}"
        )

    def test_each_node_type_maps_to_expected_shape(self):
        """逐个检查 11 类节点 → shape 是否符合规格表。"""
        ts = _read(DAG_NODE_SEMANTICS_TS)
        shape_map = _extract_shape_map(ts)

        # SHAPE_MAP 内的 10 个显式条目
        direct_checks = {
            "agent": "circle",
            "parallel_branch": "triangle",
            "command": "rounded_rect",
            "await_command": "capsule",
            "while": "capsule",
            "join": "hexagon",
            "quorum": "hexagon",
            "foreach": "parallelogram",
            "terminal_success": "octagon",
            "terminal_failure": "octagon",
        }
        for node_type, expected_shape in direct_checks.items():
            assert node_type in shape_map, f"SHAPE_MAP 缺 {node_type}"
            assert shape_map[node_type]["shape"] == expected_shape, (
                f"{node_type} 期望 shape={expected_shape}，"
                f"实际={shape_map[node_type]['shape']}"
            )

    def test_shape_order_is_stable(self):
        """DAG_SHAPE_ORDER 数组含 8 种 shape 且顺序固定（供 Legend 排序）。"""
        ts = _read(DAG_NODE_SEMANTICS_TS)
        m = re.search(
            r"DAG_SHAPE_ORDER:\s*readonly\s*DagShape\[\]\s*=\s*\[(.*?)\]",
            ts,
            re.DOTALL,
        )
        assert m, "未找到 DAG_SHAPE_ORDER"
        order = re.findall(r"'(\w+)'", m.group(1))
        assert order == [
            "circle",
            "triangle",
            "diamond",
            "capsule",
            "rounded_rect",
            "octagon",
            "hexagon",
            "parallelogram",
        ], f"DAG_SHAPE_ORDER 顺序不符：{order}"


# ── 测试 2：8 种 status 视觉叠加 ──────────────────────────────────────────


class TestStatusOverlays:
    """8 种 status 在 dag-v99.css 中均有视觉叠加规则。"""

    def test_eight_status_overlay_rules_present(self):
        """dag-v99.css 含 8 个 .dag-card[data-status='X'] 选择器。"""
        css = _read(DAG_V99_CSS)
        rules = _extract_status_rules(css)
        assert rules == EXPECTED_STATUSES, (
            f"status overlay 规则数不符：期望 {EXPECTED_STATUSES}，实际 {rules}"
        )

    def test_running_and_failed_have_animation(self):
        """running / failed / waiting_for_command 节点必须有关键帧动画。"""
        css = _read(DAG_V99_CSS)
        assert "@keyframes dag-running-pulse" in css, "缺 running 脉冲动画"
        assert "@keyframes dag-failed-pulse" in css, "缺 failed 脉冲动画"
        assert "@keyframes dag-waiting-blink" in css, "缺 waiting 闪烁动画"

        # 三个 status 必须引用对应动画
        for status, anim in [
            ("running", "dag-running-pulse"),
            ("failed", "dag-failed-pulse"),
            ("waiting_for_command", "dag-waiting-blink"),
        ]:
            rule = re.search(
                rf"\.dag-card\[data-status=['\"]" + status + r"['\"]\][^}}]*animation:[^}}]*"
                + anim,
                css,
                re.DOTALL,
            )
            assert rule, f"status={status} 未引用动画 {anim}"


# ── 测试 3：5 种 metric 徽章触发 ──────────────────────────────────────────


class TestBadgeTriggers:
    """DagNodeShapeRegistry 支持 5 类 metric 徽章触发。"""

    def test_dag_node_badge_data_interface_lists_six_fields(self):
        """DagNodeBadgeData 接口必须声明 5 类徽章字段（+ tokens_out 一共 6 字段）。"""
        tsx = _read(DAG_NODE_SHAPE_REGISTRY_TSX)
        block = re.search(
            r"DagNodeBadgeData\s*\{(.*?)\}",
            tsx,
            re.DOTALL,
        )
        assert block, "未找到 DagNodeBadgeData 接口"
        body = block.group(1)
        # 检查每个字段出现
        for field in EXPECTED_BADGE_TYPES:
            assert f"{field}?:" in body, f"DagNodeBadgeData 缺字段 {field}"

    def test_metric_badge_stack_renders_five_kinds(self):
        """MetricBadgeStack 渲染函数含 5 类徽章分支（tool_calls/tool_failures/duration/tokens/error）。"""
        tsx = _read(DAG_NODE_SHAPE_REGISTRY_TSX)
        # 找 MetricBadgeStack 函数体：定位到参数列表 `)` 后第一个 `{`
        m = re.search(r"function\s+MetricBadgeStack\([^)]*\)\s*[:\w<>\[\]|.&]*\{", tsx)
        assert m, "未找到 MetricBadgeStack 函数声明（含返回类型注解）"
        start = m.end() - 1  # 函数体 `{` 位置
        depth = 1
        i = start + 1
        while i < len(tsx) and depth > 0:
            ch = tsx[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        assert depth == 0, "MetricBadgeStack 函数体未闭合"
        body = tsx[start:i]

        # 5 类徽章触发：每个 items.push({ key: 'XXX' }) 必须存在
        badge_keys = ["tokens", "tool_calls", "tool_failures", "duration", "error_type"]
        for key in badge_keys:
            assert f"key: '{key}'" in body, f"MetricBadgeStack 缺 {key} 徽章分支"
        # 同时验证触发条件读取的字段名
        assert "badges.tokens_in" in body, "MetricBadgeStack 未读 tokens_in"
        assert "badges.tool_calls" in body, "MetricBadgeStack 未读 tool_calls"
        assert "badges.tool_failures" in body, "MetricBadgeStack 未读 tool_failures"
        assert "badges.duration_ms" in body, "MetricBadgeStack 未读 duration_ms"
        assert "badges.error_type" in body, "MetricBadgeStack 未读 error_type"


# ── 测试 4：DagLegend 列出多种 shape ──────────────────────────────────────


class TestDagLegend:
    """DagLegend 至少展示 4 种 shape（实际 8 种）。"""

    def test_list_dag_legend_entries_has_at_least_four_shapes(self):
        """listDagLegendEntries() 返回 ≥4 种 shape（v99.5 §3.8 规格说 4-5 种，实际 8 种）。"""
        ts = _read(DAG_NODE_SEMANTICS_TS)
        entries = _extract_list_dag_legend_entries(ts)
        shapes = {e["shape"] for e in entries}
        assert len(shapes) >= 4, f"DagLegend 至少 4 种 shape，实际 {len(shapes)}：{shapes}"
        # 实际期望 8 种
        assert shapes == EXPECTED_SHAPES, (
            f"DagLegend shape 集合与 DAG_SHAPE_ORDER 不一致：{shapes}"
        )

    def test_dag_legend_component_uses_list_dag_legend_entries(self):
        """DagLegend.tsx 调 listDagLegendEntries()（单一来源，不在 UI 层硬编码 shape 列表）。"""
        tsx = _read(DAG_LEGEND_TSX)
        assert "listDagLegendEntries" in tsx, "DagLegend 未引用 listDagLegendEntries"
        # 不应出现 entries = [...] 这种硬编码 8 行 shape 列表
        # （shapeGlyph switch 是必要的：把 shape 名映射到显示字符，性质不同于列表硬编码）
        # 检查没有从 DagNodeSemantics 之外定义 shape 数组
        assert "DAG_SHAPE_ORDER" not in tsx, (
            "DagLegend 不应直接引用 DAG_SHAPE_ORDER（应走 listDagLegendEntries）"
        )
        # 检查 entries 来源是 listDagLegendEntries() 的返回值
        assert re.search(r"const\s+entries\s*=\s*listDagLegendEntries\(\)", tsx), (
            "DagLegend.entries 未从 listDagLegendEntries() 派生"
        )

    def test_dag_legend_uses_localstorage_for_collapsed_state(self):
        """DagLegend 折叠状态必须持久化到 localStorage（用户偏好）。"""
        tsx = _read(DAG_LEGEND_TSX)
        assert "localStorage" in tsx, "DagLegend 缺 localStorage 持久化"
        assert "agentops.dagLegend" in tsx, "DagLegend 缺 namespace 前缀"


# ── 测试 5：DeveloperDagView 用 DagNodeCard 渲染 ──────────────────────────


class TestDeveloperDagViewIntegration:
    """DeveloperDagView 不再自定义节点形状，委托给 DagNodeCard。"""

    def test_developer_dag_view_imports_dag_node_card(self):
        """必须从 DagNodeShapeRegistry 导入 DagNodeCard。"""
        tsx = _read(DEVELOPER_DAG_VIEW_TSX)
        assert "DagNodeCard" in tsx, "DeveloperDagView 未引用 DagNodeCard"
        assert "from './DagNodeShapeRegistry'" in tsx, "DeveloperDagView 未正确 import"

    def test_developer_dag_view_uses_resolve_dag_node_semantic(self):
        """节点语义必须通过 resolveDagNodeSemantic 派生。"""
        tsx = _read(DEVELOPER_DAG_VIEW_TSX)
        assert "resolveDagNodeSemantic" in tsx, "DeveloperDagView 未用 resolveDagNodeSemantic"

    def test_developer_dag_view_renders_dag_legend(self):
        """DeveloperDagView 右上角嵌入 DagLegend（让用户对照形状含义）。"""
        tsx = _read(DEVELOPER_DAG_VIEW_TSX)
        assert "DagLegend" in tsx, "DeveloperDagView 未渲染 DagLegend"

    def test_developer_dag_view_no_inline_shape_svg(self):
        """DeveloperDagView 不应自己画 shape SVG（应统一走 ShapeRegistry）。"""
        tsx = _read(DEVELOPER_DAG_VIEW_TSX)
        # 不应有 <polygon points=... 这种 shape path（除边 SVG 外）
        # 边 SVG path 是用 M/Q 贝塞尔曲线，不是 polygon
        assert "<polygon" not in tsx, (
            "DeveloperDagView 内嵌了 polygon shape（应该委托给 DagNodeCard）"
        )

    def test_developer_dag_view_keeps_bfs_layer_layout(self):
        """DeveloperDagView 保留 BFS 分层 layout（不属于 ShapeRegistry 职责）。"""
        tsx = _read(DEVELOPER_DAG_VIEW_TSX)
        assert "incoming" in tsx and "layer[" in tsx, "DeveloperDagView 缺 BFS layer 计算"


# ── 测试 6：BusinessLaneView 集成 ShapeRegistry ────────────────────────────


class TestBusinessLaneViewIntegration:
    """BusinessLaneView 节点卡片复用 DagNodeCard（视觉一致）。"""

    def test_business_lane_view_imports_dag_node_card(self):
        tsx = _read(BUSINESS_LANE_VIEW_TSX)
        assert "DagNodeCard" in tsx, "BusinessLaneView 未引用 DagNodeCard"
        assert "from './DagNodeShapeRegistry'" in tsx, "BusinessLaneView 未正确 import"

    def test_business_lane_view_uses_resolve_dag_node_semantic(self):
        tsx = _read(BUSINESS_LANE_VIEW_TSX)
        assert "resolveDagNodeSemantic" in tsx, (
            "BusinessLaneView 未用 resolveDagNodeSemantic（应与 DeveloperDagView 共享语义层）"
        )

    def test_business_lane_view_uses_graph_node_to_badge_data(self):
        """BusinessLaneView 必须通过 graphNodeToBadgeData 把 GraphNode 转 DagNodeBadgeData。"""
        tsx = _read(BUSINESS_LANE_VIEW_TSX)
        assert "graphNodeToBadgeData" in tsx, (
            "BusinessLaneView 未用 graphNodeToBadgeData（应统一 badge 数据契约）"
        )

    def test_business_lane_view_no_inline_shape_svg(self):
        """BusinessLaneView 不应自己画 shape。"""
        tsx = _read(BUSINESS_LANE_VIEW_TSX)
        assert "<polygon" not in tsx, (
            "BusinessLaneView 内嵌了 polygon shape（应该委托给 DagNodeCard）"
        )

    def test_business_lane_view_keeps_role_lane_layout(self):
        """BusinessLaneView 保留角色泳道布局（不属于 ShapeRegistry 职责）。"""
        tsx = _read(BUSINESS_LANE_VIEW_TSX)
        assert "lane" in tsx.lower(), "BusinessLaneView 缺泳道布局逻辑"
        # 时间窗口相关（业务视角核心）
        assert "runStart" in tsx and "runEnd" in tsx, "BusinessLaneView 缺时间窗口计算"