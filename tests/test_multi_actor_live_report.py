"""
P0.8 测试：v3 multi-actor-live-report workflow + actor profiles + A2UI surface emit。

v3 结构：
  1. workflow YAML：5 节点 = 3 个完全并行的维度 actor（research / synthesis /
     visual_story）+ join_surfaces + final_summary；全部 codex harness +
     docker_container（join/final 享受 correction 机制）
  2. 4 个 actor profile（research / synthesis / visual_story / integrator）
  3. 三次完整快照协议：started(5) → partial(55) → final(100)，
     每次携带全部字段（含 phase_text / summary）
  4. E2E：3 actor 各自 emit started→partial→final → digest pinning 生效
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
WORKFLOW_YAML = PROJECT_ROOT / "workflows" / "multi-actor-live-report.yaml"
ACTORS_DIR = PROJECT_ROOT / "config" / "actors"

EXPECTED_ACTORS = ("research", "synthesis", "visual_story")
EXPECTED_VIEW_IDS = {
    "research": "research-live",
    "synthesis": "analysis-live",
    "visual_story": "publication-live",
}

# 每个视图的完整快照必填字段（v3 完整快照协议）
FULL_SNAPSHOT_FIELDS = {
    "research-live": {
        "title", "phase_text", "summary", "progress",
        "verified_count", "source_count", "gap_count",
        "finding_one", "finding_two", "source_one", "source_two",
        "primary_tone",
    },
    "analysis-live": {
        "title", "phase_text", "summary", "progress",
        "confidence", "evidence_count", "risk_count",
        "conclusion", "evidence_one", "evidence_two", "caveat",
        "primary_tone",
    },
    "publication-live": {
        "title", "phase_text", "summary", "progress",
        "headline", "hook",
        "stat_one_label", "stat_one_value", "stat_two_label", "stat_two_value",
        "point_one", "point_two", "point_three",
        "post_text", "tags", "primary_tone",
    },
    "integrator-live": {
        "title", "phase_text", "summary", "progress",
        "actor_done", "insight_count", "ready_count",
        "headline_finding", "next_step", "primary_tone",
    },
}


# ── Fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def workflow_data() -> dict:
    """加载 multi-actor-live-report.yaml。"""
    assert WORKFLOW_YAML.exists(), f"缺工作流文件：{WORKFLOW_YAML}"
    return yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))


@pytest.fixture
def reset_tracker():
    """每个测试前重置 phase tracker / surface dedup。"""
    _reset_phase_tracker()
    yield
    _reset_phase_tracker()


# ── 测试 1：workflow 结构 ──────────────────────────────────────────────────


class TestWorkflowStructure:
    """multi-actor-live-report v3 结构正确性。"""

    def test_workflow_yaml_loads(self, workflow_data):
        assert workflow_data["workflow_id"] == "multi-actor-live-report"
        assert workflow_data["name"] == "多 actor 并行实时报告"
        assert workflow_data["version"] == 3  # v3：三维度并行 + codex join/final

    def test_workflow_has_five_nodes(self, workflow_data):
        """5 节点：3 并行 actor + join_surfaces + final_summary（auditor 已删除）。"""
        nodes = workflow_data["nodes"]
        assert len(nodes) == 5, f"期望 5 节点，实际 {len(nodes)}：{sorted(nodes.keys())}"
        expected = {
            "actor_research",
            "actor_synthesis",
            "actor_visual_story",
            "join_surfaces",
            "final_summary",
        }
        assert set(nodes.keys()) == expected

    def test_three_parallel_actors_fully_independent(self, workflow_data):
        """3 个维度 actor 完全并行（after 全空，无依赖边）。"""
        for node_id in ("actor_research", "actor_synthesis", "actor_visual_story"):
            node = workflow_data["nodes"][node_id]
            assert node["type"] == "agent"
            assert node["after"] == [], f"{node_id} 必须并行启动（after=[]）"

    def test_join_surfaces_waits_for_all_three_actors(self, workflow_data):
        """join_surfaces.after 必须包含全部 3 个并行 actor 节点（join 等齐）。"""
        join = workflow_data["nodes"]["join_surfaces"]
        assert sorted(join["after"]) == sorted(
            ["actor_research", "actor_synthesis", "actor_visual_story"]
        )

    def test_final_summary_after_join(self, workflow_data):
        """final_summary 必须等 join_surfaces 完成。"""
        final = workflow_data["nodes"]["final_summary"]
        assert final["after"] == ["join_surfaces"]
        outputs = final["outputs"]
        assert all(v.get("to") == "" for v in outputs.values()), (
            "final_summary 是终端节点，outputs.to 必须为空"
        )

    def test_join_receives_all_three_surfaces(self, workflow_data):
        """join_surfaces.inputs 必须接收 3 个 actor 的 surface 交付。"""
        join = workflow_data["nodes"]["join_surfaces"]
        assert sorted(join["inputs"]) == sorted(
            ["research_surface", "analysis_surface", "publication_surface"]
        )

    def test_workflow_has_no_deprecated_widgets(self, workflow_data):
        """widget 体系已废弃（统一 A2UI 渲染），workflow 不应声明 widgets。"""
        widgets = workflow_data.get("widgets", [])
        assert len(widgets) == 0, (
            f"workflow 不应声明 widgets（已废弃，统一 A2UI 渲染），实际：{widgets}"
        )


# ── 测试 2：actor profile 加载 + 白名单合法 ───────────────────────────────


class TestActorProfiles:
    """actor profile 存在 + view_id 白名单 + 完整快照字段约束合法。"""

    @pytest.mark.parametrize(
        "actor_id", ("research", "synthesis", "visual_story", "integrator")
    )
    def test_actor_profile_file_exists(self, actor_id):
        """config/actors/<actor_id>/actor_visual_profile.json 必须存在。"""
        profile_path = ACTORS_DIR / actor_id / "actor_visual_profile.json"
        assert profile_path.exists(), f"缺 actor profile：{profile_path}"

    @pytest.mark.parametrize(
        "actor_id", ("research", "synthesis", "visual_story", "integrator")
    )
    def test_actor_profile_loads(self, actor_id):
        """profile 加载成功 + actor_id 与目录名一致。"""
        profile = load_actor_visual_profile(actor_id)
        assert profile.actor_id == actor_id
        assert len(profile.allowed_surface_views) >= 1

    @pytest.mark.parametrize(
        "actor_id,expected_view",
        [
            ("research", "research-live"),
            ("synthesis", "analysis-live"),
            ("visual_story", "publication-live"),
            ("integrator", "integrator-live"),
        ],
    )
    def test_actor_profile_declares_expected_view(self, actor_id, expected_view):
        """每个 actor 必须声明规格指定的 view_id。"""
        profile = load_actor_visual_profile(actor_id)
        assert expected_view in profile.allowed_surface_views, (
            f"actor '{actor_id}' 缺 view_id='{expected_view}'"
        )
        view = profile.get_view(expected_view)
        assert view is not None
        assert view.output_contract == "ActorReport"

    @pytest.mark.parametrize(
        "actor_id,expected_view",
        [
            ("research", "research-live"),
            ("synthesis", "analysis-live"),
            ("visual_story", "publication-live"),
        ],
    )
    def test_actor_view_fields_are_full_snapshot(self, actor_id, expected_view):
        """v3 完整快照协议：字段全集必须与规格一致（含 phase_text/summary）。"""
        profile = load_actor_visual_profile(actor_id)
        view = profile.get_view(expected_view)
        expected_fields = FULL_SNAPSHOT_FIELDS[expected_view]
        assert set(view.fields.keys()) == expected_fields, (
            f"'{expected_view}' 字段集与规格不一致："
            f"缺 {expected_fields - set(view.fields.keys())}，"
            f"多 {set(view.fields.keys()) - expected_fields}"
        )
        # 完整快照：所有字段 required（每次 emit 都必须带全）
        optional = [
            f for f, c in view.fields.items() if f != "primary_tone" and not c.required
        ]
        assert not optional, f"'{expected_view}' 存在非必填字段：{optional}"

    def test_integrator_required_phases_started_final(self):
        """integrator（join 节点）只需 started → final 两阶段。"""
        profile = load_actor_visual_profile("integrator")
        view = profile.get_view("integrator-live")
        assert set(view.required_phases) == {"started", "final"}

    def test_resolve_actor_id_from_node_business_role_priority(self):
        """resolve_actor_id_from_node 优先级 1：node.business_role。"""
        node = SimpleNamespace(
            business_role="research", actor_id=None, agent=None, id="fallback"
        )
        assert resolve_actor_id_from_node(node) == "research"

    def test_resolve_actor_id_falls_back_to_agent(self):
        """回退：business_role 缺失时用 agent ID。"""
        node = SimpleNamespace(
            business_role=None, actor_id=None, agent="synthesis", id="fallback"
        )
        assert resolve_actor_id_from_node(node) == "synthesis"


# ── 测试 3：actor 节点 inline_agent 配置 ──────────────────────────────────


class TestActorNodeConfiguration:
    """actor 节点 inline_agent 必须含 report_surface_state + view_id + role_prompt。"""

    @pytest.mark.parametrize(
        "actor_node_id,expected_role",
        [
            ("actor_research", "research"),
            ("actor_synthesis", "synthesis"),
            ("actor_visual_story", "visual_story"),
        ],
    )
    def test_actor_node_has_correct_business_role(
        self, workflow_data, actor_node_id, expected_role
    ):
        """actor 节点 business_role 必须与 config/actors/<actor_id>/ 一致。"""
        node = workflow_data["nodes"][actor_node_id]
        assert node["business_role"] == expected_role

    @pytest.mark.parametrize(
        "actor_node_id",
        ["actor_research", "actor_synthesis", "actor_visual_story", "join_surfaces"],
    )
    def test_actor_node_inline_agent_has_report_surface_state_tool(
        self, workflow_data, actor_node_id
    ):
        """有可视化需求的节点 inline_agent.allowed_tools 必须含 report_surface_state。"""
        node = workflow_data["nodes"][actor_node_id]
        inline = node["inline_agent"]
        assert "report_surface_state" in inline["allowed_tools"], (
            f"{actor_node_id} 缺 report_surface_state 工具，"
            f"当前 allowed_tools: {inline['allowed_tools']}"
        )

    @pytest.mark.parametrize(
        "actor_node_id,expected_view",
        [
            ("actor_research", "research-live"),
            ("actor_synthesis", "analysis-live"),
            ("actor_visual_story", "publication-live"),
        ],
    )
    def test_actor_node_role_prompt_references_view_id(
        self, workflow_data, actor_node_id, expected_view
    ):
        """role_prompt 必须显式引用该 actor 的 view_id（防止 view_id 串用）。"""
        node = workflow_data["nodes"][actor_node_id]
        role_prompt = node["inline_agent"]["role_prompt"]
        assert expected_view in role_prompt, (
            f"{actor_node_id} role_prompt 未引用 view_id='{expected_view}'"
        )

    def test_all_nodes_use_codex_harness_with_container(self, workflow_data):
        """v3：全部 5 节点走 codex harness + docker_container（join/final 享受 correction）。"""
        for node_id, node in workflow_data["nodes"].items():
            inline = node["inline_agent"]
            assert inline["harness"] == "codex", (
                f"{node_id} 期望 codex harness（v3），实际 {inline['harness']}"
            )
            assert node.get("runtime_placement") == "docker_container", (
                f"{node_id} 期望 docker_container placement，实际 {node.get('runtime_placement')}"
            )

    def test_actor_role_prompt_pins_progress_rhythm(self, workflow_data):
        """role_prompt 必须锚定 5/55/100 三次快照节奏。"""
        for node_id in ("actor_research", "actor_synthesis", "actor_visual_story"):
            role_prompt = workflow_data["nodes"][node_id]["inline_agent"]["role_prompt"]
            assert "progress=5" in role_prompt.replace(" ", ""), (
                f"{node_id} role_prompt 未锚定 started progress=5"
            )
            assert "55" in role_prompt and "100" in role_prompt


# ── 测试 4：phase 单调推进校验（pure function） ──────────────────────────


class TestPhaseMonotonicityFor3Actors:
    """3 actor 各自的 view_id 都必须满足 phase 单调推进。"""

    @pytest.mark.parametrize("actor_id,view_id", list(EXPECTED_VIEW_IDS.items()))
    def test_started_to_partial_to_final_allowed(self, actor_id, view_id):
        """started → partial → final 单调推进合法。"""
        tracker: dict[str, str] = {}
        validate_phase_monotonic(view_id, "started", tracker)
        tracker[view_id] = "started"
        validate_phase_monotonic(view_id, "partial", tracker)
        tracker[view_id] = "partial"
        validate_phase_monotonic(view_id, "final", tracker)

    @pytest.mark.parametrize("actor_id,view_id", list(EXPECTED_VIEW_IDS.items()))
    def test_partial_back_to_started_rejected(self, actor_id, view_id):
        """partial 回退到 started 必须拒绝（单调推进是硬约束）。"""
        tracker = {view_id: "partial"}
        with pytest.raises(Exception, match="phase 回退"):
            validate_phase_monotonic(view_id, "started", tracker)

    @pytest.mark.parametrize("actor_id,view_id", list(EXPECTED_VIEW_IDS.items()))
    def test_superseded_allowed_from_any_phase(self, actor_id, view_id):
        """superseded 可从任意阶段进入（标记旧 surface 被新 surface 替代）。"""
        for current in ("started", "partial", "final"):
            tracker = {view_id: current}
            validate_phase_monotonic(view_id, "superseded", tracker)


# ── 测试 5：E2E 模拟 3 actor emit surface snapshot ─────────────────────────

# 各视图完整快照样例（全部 required 字段）
SNAPSHOT_SAMPLES = {
    "research-live": {
        "title": "Harness 引擎调研", "phase_text": "调研完成", "summary": "三方向证据收集完成",
        "progress": 100, "verified_count": 4, "source_count": 6, "gap_count": 1,
        "finding_one": "harness = 模型周围的运行时外壳", "finding_two": "主流工具链已分层解耦",
        "source_one": "官方文档·概念定义", "source_two": "社区实践综述", "primary_tone": "info",
    },
    "analysis-live": {
        "title": "Harness 独立分析", "phase_text": "分析完成", "summary": "权衡与风险已评估",
        "progress": 100, "confidence": 75, "evidence_count": 5, "risk_count": 2,
        "conclusion": "标准化外壳 + 插件化工具是主线",
        "evidence_one": "三大主流实现结构趋同", "evidence_two": "隔离边界普遍容器化",
        "caveat": "依据模型知识，未经外部检索验证", "primary_tone": "info",
    },
    "publication-live": {
        "title": "Harness 调研发布", "phase_text": "发布就绪", "summary": "标题/导语/亮点已定稿",
        "progress": 100, "headline": "Harness：AI Agent 的运行时外壳", "hook": "三维度并行拆解",
        "stat_one_label": "Actor 数量", "stat_one_value": "3",
        "stat_two_label": "覆盖维度", "stat_two_value": "证据/分析/发布",
        "point_one": "定义与概念", "point_two": "核心组件", "point_three": "框架对比",
        "post_text": "三个并行 actor 已完成收集、分析与排版", "tags": "AI Agent, Harness",
        "primary_tone": "info",
    },
}


class TestMultiActorSurfaceEmitE2E:
    """E2E：3 actor 各自 emit started → partial → final，digest pinning 防重复。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "actor_id,view_id", list(EXPECTED_VIEW_IDS.items())
    )
    async def test_actor_emits_started_partial_final_full_snapshot(
        self, reset_tracker, actor_id, view_id
    ):
        """每个 actor 按完整快照协议跑 started(5) → partial(55) → final(100)。"""
        base = dict(SNAPSHOT_SAMPLES[view_id])

        async def emit(phase: str, progress: int):
            dm = {**base, "phase": None, "progress": progress}
            dm.pop("phase")
            return await report_surface_state(
                {
                    "actor_id": actor_id,
                    "view_id": view_id,
                    "phase": phase,
                    "data_model": dm,
                }
            )

        r_started = await emit("started", 5)
        assert r_started["ok"] is True, f"started emit 失败：{r_started}"
        r_partial = await emit("partial", 55)
        assert r_partial["ok"] is True, f"partial emit 失败：{r_partial}"
        # 身份派生 surface_id：同一 (actor, view) 跨 phase 复用同一 surface_id
        # （同一张卡的多个 patch），patch_sequence 单调递增
        assert r_partial["surface_id"] == r_started["surface_id"]
        assert r_partial["patch_sequence"] > r_started["patch_sequence"]
        r_final = await emit("final", 100)
        assert r_final["ok"] is True, f"final emit 失败：{r_final}"
        assert r_final["surface_id"] == r_partial["surface_id"]
        assert r_final["patch_sequence"] > r_partial["patch_sequence"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "actor_id,view_id", list(EXPECTED_VIEW_IDS.items())
    )
    async def test_missing_required_field_rejected(
        self, reset_tracker, actor_id, view_id
    ):
        """完整快照缺任一 required 字段（如 phase_text）必须被拒绝。"""
        dm = dict(SNAPSHOT_SAMPLES[view_id])
        dm.pop("phase_text")
        r = await report_surface_state(
            {
                "actor_id": actor_id,
                "view_id": view_id,
                "phase": "started",
                "data_model": dm,
            }
        )
        assert r["ok"] is False
        assert r.get("error_code") == "data_model_invalid"

    @pytest.mark.asyncio
    async def test_digest_pinning_skips_duplicate_emit(self, reset_tracker):
        """相同 surface_id 重复 emit 被去重（返回 deduplicated=True）。"""
        dm = dict(SNAPSHOT_SAMPLES["research-live"])
        args = {
            "actor_id": "research",
            "view_id": "research-live",
            "phase": "final",
            "data_model": dm,
        }
        r1 = await report_surface_state(args)
        assert r1["ok"] is True, f"first emit 失败：{r1}"
        r2 = await report_surface_state(args)
        assert r2["ok"] is True, f"second emit 失败：{r2}"
        assert r2.get("deduplicated") is True
        assert r1["surface_id"] == r2["surface_id"]

    @pytest.mark.asyncio
    async def test_3_actors_parallel_surfaces_independent(self, reset_tracker):
        """3 actor 各自 emit 互不干扰（不同 view_id 独立 phase tracker）。"""
        surface_ids = set()
        for actor_id, view_id in EXPECTED_VIEW_IDS.items():
            dm = dict(SNAPSHOT_SAMPLES[view_id])
            r_started = await report_surface_state(
                {
                    "actor_id": actor_id,
                    "view_id": view_id,
                    "phase": "started",
                    "data_model": dm,
                }
            )
            assert r_started["ok"] is True, f"{actor_id} started emit 失败：{r_started}"
        for actor_id, view_id in EXPECTED_VIEW_IDS.items():
            dm = dict(SNAPSHOT_SAMPLES[view_id])
            r_partial = await report_surface_state(
                {
                    "actor_id": actor_id,
                    "view_id": view_id,
                    "phase": "partial",
                    "data_model": dm,
                }
            )
            assert r_partial["ok"] is True, f"{actor_id} partial emit 失败：{r_partial}"
            surface_ids.add(r_partial["surface_id"])
        assert len(surface_ids) == 3, (
            f"3 actor 应有 3 个独立 surface_id，实际 {len(surface_ids)}"
        )


# ── 测试 6：与 v99.5 §4 反模式对齐 ────────────────────────────────────────


class TestV995AntiPatternCompliance:
    """确认 multi-actor-live-report 不踩 v99.5 §7 反模式。"""

    def test_no_visual_fields_in_yaml_nodes(self, workflow_data):
        """反模式 #2：workflow yaml 节点不声明视觉字段（a2ui_root / visual_template）。"""
        forbidden = ["a2ui_root", "visual_template", "a2ui_components", "ui_template"]
        for node_id, node in workflow_data["nodes"].items():
            for k in forbidden:
                assert k not in node, (
                    f"节点 {node_id} 含反模式字段 '{k}'（v99.5 §7 反模式 #2）"
                )

    def test_workflow_id_matches_filename(self):
        """workflow_id 必须与文件名一致（workflow-yaml.md 约定）。"""
        assert WORKFLOW_YAML.stem == "multi-actor-live-report"
        data = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
        assert data["workflow_id"] == WORKFLOW_YAML.stem

    def test_node_ids_are_lower_snake_case(self, workflow_data):
        """节点 ID 必须 lower_snake_case（workflow-yaml.md 反模式）。"""
        import re

        for nid in workflow_data["nodes"].keys():
            assert re.match(r"^[a-z][a-z0-9_]*$", nid), (
                f"节点 ID '{nid}' 不是 lower_snake_case"
            )
