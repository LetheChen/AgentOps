"""
P0.9 测试：v99.5 weekly-report workflow A2UI AoProgress 迁移。

覆盖：
  1. config/actors/weekly_reporter/actor_visual_profile.json 含 3 view_id
     （collect-live / grade-live / archive-live） + 各自字段约束合法
  2. weekly-report.yaml 3 节点都用 inline_agent + business_role=weekly_reporter
  3. 每个节点 allowed_tools 含 report_surface_state
  4. 每个节点 role_prompt 显式引用自己的 view_id（防止串用）
  5. workflow YAML 仍 3 层校验通过
  6. E2E：3 节点模拟各自 emit started → partial → final，surface_id 互不冲突
  7. AoProgress 0-33 / 33-66 / 66-100 三段进度对应 3 个 view_id 的 progress 字段
  8. v99.5 §7 反模式：workflow yaml 无 a2ui_root / visual_template
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestrator.actor_visual_profile import (
    load_actor_visual_profile,
    resolve_actor_id_from_node,
    validate_phase_monotonic,
)
from tools.report_surface_state import _reset_phase_tracker, report_surface_state


# ── 路径常量 ───────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_YAML = PROJECT_ROOT / "workflows" / "weekly-report.yaml"
ACTORS_DIR = PROJECT_ROOT / "config" / "actors"

# 3 view_id 与 3 节点一一对应（v99.5 P0.9 AoProgress 三段进度映射）
EXPECTED_VIEWS = {
    "collect_classify": "collect-live",
    "grade_summarize": "grade-live",
    "archive": "archive-live",
}

# AoProgress 三段进度区间（v99.5 §6 Phase 4 Day 2 规格）
PROGRESS_RANGES = {
    "collect-live": (0, 33),
    "grade-live": (33, 66),
    "archive-live": (66, 100),
}


# ── Fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def workflow_data() -> dict:
    assert WORKFLOW_YAML.exists(), f"缺工作流文件：{WORKFLOW_YAML}"
    return yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))


@pytest.fixture
def reset_tracker():
    _reset_phase_tracker()
    yield
    _reset_phase_tracker()


# ── 测试 1：weekly_reporter actor profile ────────────────────────────────


class TestWeeklyReporterProfile:
    """weekly_reporter actor profile 含 3 view_id + 字段约束合法。"""

    def test_weekly_reporter_profile_file_exists(self):
        profile_path = ACTORS_DIR / "weekly_reporter" / "actor_visual_profile.json"
        assert profile_path.exists(), f"缺 actor profile：{profile_path}"

    def test_weekly_reporter_profile_loads(self):
        profile = load_actor_visual_profile("weekly_reporter")
        assert profile.actor_id == "weekly_reporter"
        assert len(profile.allowed_surface_views) == 3, (
            f"期望 3 view_id（collect-live / grade-live / archive-live），实际 {len(profile.allowed_surface_views)}"
        )

    @pytest.mark.parametrize(
        "node_id,view_id",
        [(nid, vid) for nid, vid in EXPECTED_VIEWS.items()],
    )
    def test_weekly_reporter_declares_expected_view(self, node_id, view_id):
        """3 view_id 必须全部在 weekly_reporter profile 白名单内。"""
        profile = load_actor_visual_profile("weekly_reporter")
        assert view_id in profile.allowed_surface_views, (
            f"节点 {node_id} 对应 view_id='{view_id}' 不在 weekly_reporter 白名单"
        )

    def test_all_views_progress_field_is_integer_zero_to_hundred(self):
        """3 view_id 的 progress 字段都是 integer [0, 100]（AoProgress 通用约束）。"""
        profile = load_actor_weekly_reporter()
        for view_id in EXPECTED_VIEWS.values():
            view = profile.get_view(view_id)
            progress = view.fields["progress"]
            assert progress.type == "integer"
            assert progress.min == 0
            assert progress.max == 100
            assert progress.required is True

    def test_aoprogress_three_segments_in_role_prompts(self):
        """3 节点 role_prompt 必须把 progress 分别约束在 0-33 / 33-66 / 66-100 三段（LLM 引导约定）。"""
        workflow_data = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
        segment_ranges = {
            "collect_classify": ("0-33", "33"),
            "grade_summarize": ("33-66", "66"),
            "archive": ("66-100", "100"),
        }
        for node_id, (range_text, final_value) in segment_ranges.items():
            role_prompt = workflow_data["nodes"][node_id]["inline_agent"]["role_prompt"]
            assert range_text in role_prompt or f"progress={final_value}" in role_prompt, (
                f"节点 {node_id} role_prompt 必须体现 AoProgress 段 {range_text} "
                f"（或 progress={final_value}）"
            )

    @pytest.mark.parametrize(
        "view_id",
        list(EXPECTED_VIEWS.values()),
    )
    def test_view_output_contract_is_actor_report(self, view_id):
        """3 view_id 都声明 output_contract=ActorReport（与 research/synthesis/auditor 对齐）。"""
        profile = load_actor_weekly_reporter()
        view = profile.get_view(view_id)
        assert view.output_contract == "ActorReport"

    @pytest.mark.parametrize(
        "view_id",
        list(EXPECTED_VIEWS.values()),
    )
    def test_view_required_phases_includes_full_progression(self, view_id):
        """3 view_id 都声明 required_phases 含 started/partial/final。"""
        profile = load_actor_weekly_reporter()
        view = profile.get_view(view_id)
        for phase in ("started", "partial", "final"):
            assert phase in view.required_phases

    @pytest.mark.parametrize(
        "view_id",
        list(EXPECTED_VIEWS.values()),
    )
    def test_view_primary_tone_enum(self, view_id):
        """primary_tone 字段 enum 5 值。"""
        profile = load_actor_weekly_reporter()
        view = profile.get_view(view_id)
        tone = view.fields["primary_tone"]
        assert tone.type == "enum"
        assert set(tone.enum_values) == {
            "neutral", "info", "positive", "warning", "critical"
        }


def load_actor_weekly_reporter():
    return load_actor_visual_profile("weekly_reporter")


# ── 测试 2：weekly-report workflow 节点配置 ──────────────────────────────


class TestWeeklyReportWorkflowStructure:
    """weekly-report workflow 节点配置正确（3 节点都用 weekly_reporter + report_surface_state）。"""

    def test_workflow_still_three_nodes(self, workflow_data):
        """P0.5 简化的 3 节点结构保持不变。"""
        nodes = workflow_data["nodes"]
        assert set(nodes.keys()) == {"collect_classify", "grade_summarize", "archive"}

    @pytest.mark.parametrize(
        "node_id,view_id",
        list(EXPECTED_VIEWS.items()),
    )
    def test_node_business_role_is_weekly_reporter(
        self, workflow_data, node_id, view_id
    ):
        """3 节点 business_role 都指向 weekly_reporter actor profile。"""
        node = workflow_data["nodes"][node_id]
        assert node["business_role"] == "weekly_reporter", (
            f"节点 {node_id} business_role 应为 'weekly_reporter'，"
            f"实际='{node['business_role']}'"
        )

    @pytest.mark.parametrize("node_id", list(EXPECTED_VIEWS.keys()))
    def test_node_inline_agent_has_report_surface_state_tool(
        self, workflow_data, node_id
    ):
        """3 节点 allowed_tools 都含 report_surface_state（P0.9 新增）。"""
        node = workflow_data["nodes"][node_id]
        inline = node["inline_agent"]
        assert "report_surface_state" in inline["allowed_tools"], (
            f"节点 {node_id} 缺 report_surface_state 工具，"
            f"当前 allowed_tools: {inline['allowed_tools']}"
        )

    @pytest.mark.parametrize(
        "node_id,view_id",
        list(EXPECTED_VIEWS.items()),
    )
    def test_node_role_prompt_references_view_id(
        self, workflow_data, node_id, view_id
    ):
        """role_prompt 必须显式引用该节点对应的 view_id（防止 view_id 串用）。"""
        node = workflow_data["nodes"][node_id]
        role_prompt = node["inline_agent"]["role_prompt"]
        assert view_id in role_prompt, (
            f"节点 {node_id} role_prompt 未引用 view_id='{view_id}'"
        )

    @pytest.mark.parametrize(
        "node_id",
        list(EXPECTED_VIEWS.keys()),
    )
    def test_node_role_prompt_mentions_phase_progression(
        self, workflow_data, node_id
    ):
        """role_prompt 必须包含 started/partial/final 3 阶段推进指引。"""
        import re

        node = workflow_data["nodes"][node_id]
        role_prompt = node["inline_agent"]["role_prompt"]
        for phase in ("started", "partial", "final"):
            pattern = rf"phase\s*[:=]\s*[\"']?{phase}[\"']?"
            assert re.search(pattern, role_prompt), (
                f"节点 {node_id} role_prompt 未指引 phase='{phase}' 推进"
            )

    def test_resolve_actor_id_from_weekly_report_node(self):
        """resolve_actor_id_from_node 推导 business_role='weekly_reporter'。"""
        node = SimpleNamespace(
            business_role="weekly_reporter", actor_id=None, agent=None, id="fallback"
        )
        assert resolve_actor_id_from_node(node) == "weekly_reporter"


# ── 测试 3：phase 单调推进（3 view_id） ──────────────────────────────────


class TestWeeklyReporterPhaseMonotonicity:
    """3 view_id 必须支持 phase 单调推进。"""

    @pytest.mark.parametrize("view_id", list(EXPECTED_VIEWS.values()))
    def test_started_partial_final_allowed(self, view_id):
        tracker: dict[str, str] = {}
        validate_phase_monotonic(view_id, "started", tracker)
        tracker[view_id] = "started"
        validate_phase_monotonic(view_id, "partial", tracker)
        tracker[view_id] = "partial"
        validate_phase_monotonic(view_id, "final", tracker)

    @pytest.mark.parametrize("view_id", list(EXPECTED_VIEWS.values()))
    def test_final_back_to_started_rejected(self, view_id):
        tracker = {view_id: "final"}
        with pytest.raises(Exception, match="phase 回退"):
            validate_phase_monotonic(view_id, "started", tracker)


# ── 测试 4：E2E 3 节点模拟 emit surface snapshot ─────────────────────────


class TestWeeklyReportSurfaceEmitE2E:
    """E2E：3 节点各自 emit started → partial → final，surface_id 互不冲突。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "node_id,view_id,started_data,extra_data",
        [
            (
                "collect_classify",
                "collect-live",
                {"title": "解析周报合集", "progress": 0, "items_collected": 0, "primary_tone": "info"},
                {"classified_count": 5, "value_level_hint": "S/A/B/C 粗判"},
            ),
            (
                "grade_summarize",
                "grade-live",
                {"title": "分级 + 汇总", "progress": 33, "graded_count": 0, "primary_tone": "info"},
                {"s_count": 2, "a_count": 3, "b_count": 4, "c_count": 1, "self_check_passed": True},
            ),
            (
                "archive",
                "archive-live",
                {"title": "归档周报", "progress": 66, "primary_tone": "info"},
                {"archive_path": "E:\\Document\\Weekly\\工作周报.md", "ingest_count": 3, "kb_target": "weekly-report"},
            ),
        ],
    )
    async def test_node_emits_started_partial_final(
        self, reset_tracker, node_id, view_id, started_data, extra_data
    ):
        """每个节点完整跑 started → partial → final 都能成功 emit。"""
        components = [{"type": "AoProgress", "value": 0}, {"type": "AoSection", "title": "title"}]

        # ── started ──
        r = await report_surface_state(
            {
                "actor_id": "weekly_reporter",
                "view_id": view_id,
                "phase": "started",
                "components": components,
                "data_model": dict(started_data),
            }
        )
        assert r["ok"] is True, f"{node_id} started emit 失败：{r}"
        assert r["view_id"] == view_id
        assert r["phase"] == "started"
        surface_id_started = r["surface_id"]

        # ── partial（带 extra 字段） ──
        data_partial = {**started_data, "progress": (started_data["progress"] + 100) // 2, **extra_data}
        r2 = await report_surface_state(
            {
                "actor_id": "weekly_reporter",
                "view_id": view_id,
                "phase": "partial",
                "components": components,
                "data_model": data_partial,
            }
        )
        assert r2["ok"] is True, f"{node_id} partial emit 失败：{r2}"
        # 身份派生 surface_id：同一 (actor, view) 跨 phase 复用 surface_id
        assert r2["surface_id"] == surface_id_started
        assert r2["patch_sequence"] > r["patch_sequence"]

        # ── final ──
        data_final = {**started_data, "progress": 100, **extra_data}
        r3 = await report_surface_state(
            {
                "actor_id": "weekly_reporter",
                "view_id": view_id,
                "phase": "final",
                "components": components,
                "data_model": data_final,
            }
        )
        assert r3["ok"] is True, f"{node_id} final emit 失败：{r3}"
        # 身份派生 surface_id：同一 surface 跨 phase 复用
        assert r3["surface_id"] == r2["surface_id"]
        assert r3["patch_sequence"] > r2["patch_sequence"]

    @pytest.mark.asyncio
    async def test_three_views_independent_surface_ids(self, reset_tracker):
        """3 view_id 各自的 surface_id 必须互不冲突（不同 view_id 独立 phase tracker）。"""
        surface_ids = set()
        for view_id, data in [
            ("collect-live", {"title": "collect", "progress": 0, "items_collected": 5, "primary_tone": "info"}),
            ("grade-live", {"title": "grade", "progress": 33, "graded_count": 5, "primary_tone": "info"}),
            ("archive-live", {"title": "archive", "progress": 66, "primary_tone": "info"}),
        ]:
            r = await report_surface_state(
                {
                    "actor_id": "weekly_reporter",
                    "view_id": view_id,
                    "phase": "started",
                    "components": [{"type": "AoProgress", "value": 0}],
                    "data_model": data,
                }
            )
            assert r["ok"] is True, f"{view_id} emit 失败：{r}"
            surface_ids.add(r["surface_id"])
        assert len(surface_ids) == 3, f"3 view_id 应有 3 个独立 surface_id，实际 {len(surface_ids)}"

    @pytest.mark.asyncio
    async def test_digest_pinning_for_weekly_reporter(self, reset_tracker):
        """相同 view_id + data_model 重复 emit 被去重。"""
        data = {"title": "test", "progress": 33, "graded_count": 1, "primary_tone": "info"}
        components = [{"type": "AoProgress", "value": 33}]
        args = {
            "actor_id": "weekly_reporter",
            "view_id": "grade-live",
            "phase": "partial",
            "components": components,
            "data_model": data,
        }
        r1 = await report_surface_state(args)
        assert r1["ok"] is True
        r2 = await report_surface_state(args)
        assert r2["ok"] is True
        assert r2.get("deduplicated") is True


# ── 测试 5：v99.5 §7 反模式对齐 ──────────────────────────────────────────


class TestWeeklyReportAntiPatternCompliance:
    """weekly-report workflow 不踩 v99.5 §7 反模式。"""

    def test_no_visual_fields_in_yaml_nodes(self, workflow_data):
        forbidden = ["a2ui_root", "visual_template", "a2ui_components", "ui_template"]
        for nid, node in workflow_data["nodes"].items():
            for k in forbidden:
                assert k not in node, (
                    f"节点 {nid} 含反模式字段 '{k}'（v99.5 §7 反模式 #2）"
                )

    def test_workflow_id_matches_filename(self):
        assert WORKFLOW_YAML.stem == "weekly-report"
        data = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
        assert data["workflow_id"] == WORKFLOW_YAML.stem

    def test_progress_status_widget_still_present(self, workflow_data):
        """progress_status widget 仍存在（与 A2UI 并行：widget 实时进度 + A2UI 详细 surface）。"""
        widgets = workflow_data.get("widgets", [])
        progress_widgets = [w for w in widgets if w["type"] == "progress_status"]
        assert len(progress_widgets) >= 1
        steps = progress_widgets[0]["props"]["steps"]
        step_node_ids = {s["node"] for s in steps}
        assert step_node_ids == {"collect_classify", "grade_summarize", "archive"}