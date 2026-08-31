"""
P0.10 测试：v99.5 SurfaceAggregator 业务级 surface 状态聚合 + view_id 白名单。

覆盖：
  1. SurfaceAggregator 基础 reducer 语义（与前端 SupervisionPanel.applySurfaceStateEvent 对齐）
     - 单 actor 单 view：started → partial → final 单调推进
     - supersede 总是替换
     - phase 回退（partial → started）被丢弃
     - missing_view_id / missing_phase / invalid_phase 拒绝
  2. view_id 白名单（per-actor）
     - 未知 actor → unknown_actor
     - 已知 actor + 未声明 view_id → view_id_not_in_whitelist
     - 已知 actor + 声明内 view_id → 接受
  3. apply_batch 批量处理（每条独立返回 ApplyResult）
  4. Phase 4 Day 1 集成测试（v99.5 plan）：
     - multi-actor-live-report（3 actor：research / synthesis / auditor）端到端
     - weekly-report（1 actor 3 view_id：collect-live / grade-live / archive-live）端到端
     - 两个工作流同时跑 → 6 个 snapshot 互不冲突
  5. 与 orchestrator/report_surface_state.py report_surface_state 工具联合校验
     - 后端工具拒绝 view_id → SurfaceAggregator 也拒绝同一 view_id（语义对齐）
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.actor_visual_profile import (
    load_actor_visual_profile,
)
from orchestrator.surface_aggregator import (
    PHASE_ORDER,
    AggregationEvent,
    ApplyResult,
    SupervisionSnapshot,
    SurfaceAggregator,
    SurfaceAggregatorError,
    should_replace,
)
from tools.report_surface_state import (
    _reset_phase_tracker,
    report_surface_state,
)


# ── 路径常量 ───────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MULTI_ACTOR_YAML = PROJECT_ROOT / "workflows" / "multi-actor-live-report.yaml"
WEEKLY_REPORT_YAML = PROJECT_ROOT / "workflows" / "weekly-report.yaml"


# ── Fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def reset_phase_tracker():
    _reset_phase_tracker()
    yield
    _reset_phase_tracker()


@pytest.fixture
def aggregator_4_actors() -> SurfaceAggregator:
    """加载 v99.5 P0.10 全部 4 个 actor profile（Phase 4 Day 1 集成测试用）。"""
    return SurfaceAggregator.with_known_actors(
        ["research", "synthesis", "auditor", "weekly_reporter"],
        load_profiles=True,
    )


def _make_surface_state(
    view_id: str,
    phase: str,
    data_model: dict,
    surface_id: str | None = None,
    actor_display: str | None = None,
) -> dict:
    """构造一个最小合法 SurfaceState dict（与 SurfaceState.to_payload() 一致）。

    默认 surface_id = "surf-test"（所有事件共用同一 key，模拟「同 surface 跨 phase 多次 emit」）。
    若需要「不同 view_id 各自独立聚合」，传 surface_id=f"surf-{view_id}" 或其它唯一值。
    """
    sid = surface_id if surface_id is not None else "surf-test"
    state: dict = {
        "surface_id": sid,
        "view_id": view_id,
        "phase": phase,
        "components": [{"type": "AoProgress", "value": 0}],
        "data_model": data_model,
        "catalog_id": "https://agentops.dev/a2ui/catalogs/core/v1",
        "emitted_at": datetime.now(timezone.utc).isoformat(),
    }
    if actor_display:
        state["surface_properties"] = {"agentDisplayName": actor_display}
    return state


# ── 测试 1：reducer 基础语义 ───────────────────────────────────────────────


class TestReducerBasics:
    """SurfaceAggregator 单 actor 单 view_id 推进语义。"""

    def test_should_replace_phase_order(self):
        """PHASE_ORDER 顺序必须 started(0) < partial(1) < final(2) < superseded(3)。"""
        assert PHASE_ORDER["started"] < PHASE_ORDER["partial"]
        assert PHASE_ORDER["partial"] < PHASE_ORDER["final"]
        assert PHASE_ORDER["final"] < PHASE_ORDER["superseded"]

    def test_should_replace_forward_allowed(self):
        """new phase 向前推进 → 允许替换。"""
        assert should_replace("started", "partial") is True
        assert should_replace("partial", "final") is True
        assert should_replace("started", "final") is True

    def test_should_replace_backward_rejected(self):
        """new phase 回退 → 拒绝。"""
        assert should_replace("partial", "started") is False
        assert should_replace("final", "partial") is False
        assert should_replace("final", "started") is False

    def test_should_replace_superseded_always_wins(self):
        """superseded 从任意阶段都可替换。"""
        for old in ("started", "partial", "final", "superseded"):
            assert should_replace(old, "superseded") is True, (
                f"superseded 应能从 '{old}' 替换，但应拒绝"
            )

    def test_first_event_started_accepted(self, aggregator_4_actors):
        """actor 第一次 emit started → 接受。"""
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live",
                    "started",
                    {"title": "调研启动", "progress": 0, "primary_tone": "info"},
                ),
            )
        )
        assert result.accepted is True
        assert result.dropped_reason is None
        # identity 派生 surface_id：聚合 key = surface_id（per-actor/view 稳定）
        assert "surf-test" in result.snapshots

    def test_started_partial_final_chain(self, aggregator_4_actors):
        """started → partial → final 链式推进全部接受。"""
        for phase in ("started", "partial", "final"):
            result = aggregator_4_actors.apply(
                AggregationEvent(
                    actor_id="research",
                    surface_state=_make_surface_state(
                        "research-live",
                        phase,
                        {"title": "调研", "progress": 50, "primary_tone": "info"},
                    ),
                )
            )
            assert result.accepted is True, (
                f"phase='{phase}' 应被接受，但 result={result}"
            )

    def test_phase_backward_rejected(self, aggregator_4_actors):
        """先 emit final，再 emit started → 第二条被丢弃（phase 回退）。"""
        aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live", "final", {"title": "t", "progress": 100, "primary_tone": "info"}
                ),
            )
        )
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live", "started", {"title": "t", "progress": 0, "primary_tone": "info"}
                ),
            )
        )
        assert result.accepted is False
        assert result.dropped_reason == "phase_not_monotonic"
        # 旧 snapshot 保持 final 状态（identity 派生后 key=surface_id="surf-test"）
        snap = result.snapshots["surf-test"]
        assert snap.phase == "final"

    def test_superseded_replaces_final(self, aggregator_4_actors):
        """final → superseded 允许（superseded 总是替换）。"""
        aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live", "final", {"title": "t", "progress": 100, "primary_tone": "info"}
                ),
            )
        )
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live", "superseded", {"title": "t", "progress": 100, "primary_tone": "info"}
                ),
            )
        )
        assert result.accepted is True
        assert result.snapshots["surf-test"].phase == "superseded"


# ── 测试 2：白名单语义 ─────────────────────────────────────────────────────


class TestWhitelistEnforcement:
    """view_id 白名单（per-actor allowed_surface_views）拒绝未授权。"""

    def test_unknown_actor_rejected(self, aggregator_4_actors):
        """actor_id 不在白名单 → unknown_actor。"""
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="rogue_actor",
                surface_state=_make_surface_state(
                    "rogue-live", "started", {"title": "x", "progress": 0, "primary_tone": "info"}
                ),
            )
        )
        assert result.accepted is False
        assert result.dropped_reason == "unknown_actor"
        assert "rogue_actor::rogue-live" not in result.snapshots

    def test_view_id_not_in_whitelist_rejected(self, aggregator_4_actors):
        """actor_id 在白名单，但 view_id 未声明 → view_id_not_in_whitelist。"""
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "synthesis-live",  # research 没声明这个 view
                    "started",
                    {"title": "x", "progress": 0, "primary_tone": "info"},
                ),
            )
        )
        assert result.accepted is False
        assert result.dropped_reason == "view_id_not_in_whitelist"

    def test_known_actor_and_view_accepted(self, aggregator_4_actors):
        """actor_id + view_id 都在白名单 → 接受。"""
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live",
                    "started",
                    {"title": "调研", "progress": 0, "primary_tone": "info"},
                ),
            )
        )
        assert result.accepted is True

    def test_weekly_reporter_has_three_views(self, aggregator_4_actors):
        """weekly_reporter 的 3 view_id 都在白名单内。"""
        for view_id in ("collect-live", "grade-live", "archive-live"):
            assert aggregator_4_actors.is_view_allowed("weekly_reporter", view_id) is True, (
                f"weekly_reporter.{view_id} 必须在白名单"
            )

    def test_no_profile_load_returns_unknown_actor(self):
        """未注册任何 profile 的 aggregator → 任何 actor 都 unknown_actor。"""
        agg = SurfaceAggregator()  # profiles 空
        result = agg.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live",
                    "started",
                    {"title": "x", "progress": 0, "primary_tone": "info"},
                ),
            )
        )
        assert result.accepted is False
        assert result.dropped_reason == "unknown_actor"

    def test_register_profile_runtime(self):
        """register_profile 动态加载 + 白名单生效。"""
        agg = SurfaceAggregator()
        agg.register_profile(load_actor_visual_profile("research"))
        assert agg.is_view_allowed("research", "research-live") is True
        assert agg.is_view_allowed("research", "synthesis-live") is False


# ── 测试 3：字段缺失 / 非法值 ──────────────────────────────────────────────


class TestFieldValidation:
    """AggregationEvent 字段合法性校验。"""

    def test_missing_surface_state_rejected(self, aggregator_4_actors):
        result = aggregator_4_actors.apply(
            AggregationEvent(actor_id="research", surface_state={})
        )
        assert result.accepted is False
        assert result.dropped_reason == "missing_surface_state"

    def test_missing_view_id_rejected(self, aggregator_4_actors):
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state={"phase": "started"},
            )
        )
        assert result.accepted is False
        assert result.dropped_reason == "missing_view_id"

    def test_missing_phase_rejected(self, aggregator_4_actors):
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state={"view_id": "research-live"},
            )
        )
        assert result.accepted is False
        assert result.dropped_reason == "missing_phase"

    def test_invalid_phase_rejected(self, aggregator_4_actors):
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state={
                    "view_id": "research-live",
                    "phase": "halfway",  # 非法
                    "surface_id": "x",
                },
            )
        )
        assert result.accepted is False
        assert result.dropped_reason == "invalid_phase"

    def test_actor_id_falls_back_to_surface_properties(self, aggregator_4_actors):
        """event.actor_id 为空时，从 surface_properties.agentDisplayName 推导。"""
        result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id=None,
                surface_state=_make_surface_state(
                    "research-live",
                    "started",
                    {"title": "x", "progress": 0, "primary_tone": "info"},
                    actor_display="research",
                ),
            )
        )
        assert result.accepted is True
        assert "surf-test" in result.snapshots


# ── 测试 4：批量处理 + snapshot 内容 ────────────────────────────────────────


class TestBatchAndSnapshotContent:
    """apply_batch 批量 + SupervisionSnapshot 内容完整性。"""

    def test_apply_batch_returns_independent_results(self, aggregator_4_actors):
        events = [
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live", "started", {"title": "t", "progress": 0, "primary_tone": "info"}
                ),
            ),
            AggregationEvent(
                actor_id="rogue",
                surface_state=_make_surface_state(
                    "x-live", "started", {"title": "t", "progress": 0, "primary_tone": "info"}
                ),
            ),
            AggregationEvent(
                actor_id="synthesis",
                surface_state=_make_surface_state(
                    "analysis-live", "started", {"title": "t", "progress": 0, "primary_tone": "info"}
                ),
            ),
        ]
        results = aggregator_4_actors.apply_batch(events)
        assert len(results) == 3
        assert results[0].accepted is True
        assert results[1].accepted is False
        assert results[1].dropped_reason == "unknown_actor"
        assert results[2].accepted is True

    def test_supervision_snapshot_to_dict_round_trip(self, aggregator_4_actors):
        snap = SupervisionSnapshot(
            view_id="research-live",
            actor_id="research",
            surface_id="abc123",
            phase="final",
            emitted_at="2026-08-09T00:00:00Z",
            surface_state={"view_id": "research-live", "phase": "final"},
        )
        d = snap.to_dict()
        assert d["view_id"] == "research-live"
        assert d["phase"] == "final"
        assert d["surface_id"] == "abc123"

    def test_reset_clears_snapshots_but_keeps_profiles(self, aggregator_4_actors):
        aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live", "started", {"title": "t", "progress": 0, "primary_tone": "info"}
                ),
            )
        )
        assert len(aggregator_4_actors.snapshots) == 1
        aggregator_4_actors.reset()
        assert len(aggregator_4_actors.snapshots) == 0
        # profile 仍保留
        assert aggregator_4_actors.is_view_allowed("research", "research-live") is True


# ── 测试 5：Phase 4 Day 1 集成测试 — multi-actor-live-report E2E ──────────


class TestPhase4Day1MultiActorLiveReport:
    """v99.5 Phase 4 Day 1：multi-actor-live-report（6 节点，3 actor 并行）端到端。"""

    @pytest.mark.asyncio
    async def test_three_actors_emit_started_partial_final_independently(
        self, aggregator_4_actors, reset_phase_tracker
    ):
        """3 个并行 actor（research / synthesis / auditor）各自完整推进 3 阶段。"""
        actor_data = [
            ("research", "research-live", "research", "verified_count"),
            ("synthesis", "analysis-live", "synthesis", "inputs_synthesized"),
            ("auditor", "auditor-live", "auditor", "checks_run"),
        ]

        for phase_idx, phase in enumerate(("started", "partial", "final")):
            for actor_id, view_id, actor_disp, required_field in actor_data:
                data = {
                    "title": f"{actor_id} {phase}",
                    "progress": phase_idx * 50,
                    "primary_tone": "info",
                }
                data[required_field] = phase_idx * 3
                result = aggregator_4_actors.apply(
                    AggregationEvent(
                        actor_id=actor_id,
                        # 不同 view_id 各自独立聚合：surface_id 按 view_id 派生
                        surface_state=_make_surface_state(
                            view_id, phase, data,
                            surface_id=f"surf-{view_id}",
                        ),
                    )
                )
                assert result.accepted is True, (
                    f"actor={actor_id} view={view_id} phase={phase} 应接受，got {result}"
                )

        # 3 actor 各自 latest snapshot 都在 final
        # （identity 派生后 key=surface_id="surf-{view_id}"，3 个 view 互不干扰）
        assert aggregator_4_actors.snapshots["surf-research-live"].phase == "final"
        assert aggregator_4_actors.snapshots["surf-analysis-live"].phase == "final"
        assert aggregator_4_actors.snapshots["surf-auditor-live"].phase == "final"

    @pytest.mark.asyncio
    async def test_multi_actor_workflow_yaml_validates(self):
        """workflows/multi-actor-live-report.yaml 通过 CLI 校验。"""
        import subprocess

        result = subprocess.run(
            ["python", "cli.py", "validate", str(MULTI_ACTOR_YAML)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",  # 强制 UTF-8 解码，避免 Windows GBK 崩溃
            timeout=30,
        )
        assert result.returncode == 0, f"validate 失败: {result.stdout} {result.stderr}"
        assert "OK" in result.stdout


# ── 测试 6：Phase 4 Day 1 集成测试 — weekly-report E2E ──────────────────


class TestPhase4Day1WeeklyReport:
    """v99.5 Phase 4 Day 1：weekly-report（3 节点 × 3 view_id）端到端。"""

    @pytest.mark.asyncio
    async def test_weekly_reporter_three_views_aggregate_independently(
        self, aggregator_4_actors, reset_phase_tracker
    ):
        """weekly_reporter 单 actor 3 view_id 独立聚合。"""
        view_data = [
            ("collect-live", {"items_collected": 5}, {"classified_count": 5}),
            ("grade-live", {"graded_count": 10}, {"s_count": 2, "a_count": 3, "b_count": 4, "c_count": 1}),
            ("archive-live", {"archive_path": "/x.md"}, {"ingest_count": 3}),
        ]

        for view_id, started_extra, final_extra in view_data:
            # started
            result = aggregator_4_actors.apply(
                AggregationEvent(
                    actor_id="weekly_reporter",
                    surface_state=_make_surface_state(
                        view_id, "started",
                        {"title": f"{view_id} 启动", "progress": 0, "primary_tone": "info", **started_extra},
                        surface_id=f"surf-{view_id}",
                    ),
                )
            )
            assert result.accepted is True
            # final
            result = aggregator_4_actors.apply(
                AggregationEvent(
                    actor_id="weekly_reporter",
                    surface_state=_make_surface_state(
                        view_id, "final",
                        {"title": f"{view_id} 完成", "progress": 100, "primary_tone": "info", **started_extra, **final_extra},
                        surface_id=f"surf-{view_id}",
                    ),
                )
            )
            assert result.accepted is True

        # 3 view_id 各自有 final snapshot
        for view_id in ("collect-live", "grade-live", "archive-live"):
            key = f"surf-{view_id}"
            assert key in aggregator_4_actors.snapshots
            assert aggregator_4_actors.snapshots[key].phase == "final"

    @pytest.mark.asyncio
    async def test_weekly_report_workflow_yaml_validates(self):
        """workflows/weekly-report.yaml 通过 CLI 校验。"""
        import subprocess

        result = subprocess.run(
            ["python", "cli.py", "validate", str(WEEKLY_REPORT_YAML)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",  # 强制 UTF-8 解码，避免 Windows GBK 崩溃
            timeout=30,
        )
        assert result.returncode == 0, f"validate 失败: {result.stdout} {result.stderr}"
        assert "OK" in result.stdout


# ── 测试 7：双工作流并行 + 6 snapshot 互不冲突 ────────────────────────────


class TestPhase4Day1CombinedRun:
    """v99.5 Phase 4 Day 1：multi-actor-live-report + weekly-report 同时跑。"""

    @pytest.mark.asyncio
    async def test_six_snapshots_coexist_no_collision(
        self, aggregator_4_actors, reset_phase_tracker
    ):
        """6 snapshot 一起跑：3 actor (research/synthesis/auditor) + 3 view_id (collect/grade/archive) = 6 key 不冲突。"""
        events = [
            # multi-actor-live-report 3 actor
            AggregationEvent(
                actor_id="research",
                surface_state=_make_surface_state(
                    "research-live", "final",
                    {"title": "r", "progress": 100, "verified_count": 5, "primary_tone": "info"},
                    surface_id="surf-research-live",
                ),
            ),
            AggregationEvent(
                actor_id="synthesis",
                surface_state=_make_surface_state(
                    "analysis-live", "final",
                    {"title": "s", "progress": 100, "inputs_synthesized": 5, "primary_tone": "info"},
                    surface_id="surf-analysis-live",
                ),
            ),
            AggregationEvent(
                actor_id="auditor",
                surface_state=_make_surface_state(
                    "auditor-live", "final",
                    {"title": "a", "progress": 100, "checks_run": 5, "primary_tone": "info"},
                    surface_id="surf-auditor-live",
                ),
            ),
            # weekly-report 3 view_id
            AggregationEvent(
                actor_id="weekly_reporter",
                surface_state=_make_surface_state(
                    "collect-live", "final",
                    {"title": "c", "progress": 33, "items_collected": 5, "primary_tone": "info"},
                    surface_id="surf-collect-live",
                ),
            ),
            AggregationEvent(
                actor_id="weekly_reporter",
                surface_state=_make_surface_state(
                    "grade-live", "final",
                    {"title": "g", "progress": 66, "graded_count": 5, "primary_tone": "info"},
                    surface_id="surf-grade-live",
                ),
            ),
            AggregationEvent(
                actor_id="weekly_reporter",
                surface_state=_make_surface_state(
                    "archive-live", "final",
                    {"title": "a2", "progress": 100, "archive_path": "/x.md", "primary_tone": "info"},
                    surface_id="surf-archive-live",
                ),
            ),
        ]
        results = aggregator_4_actors.apply_batch(events)
        for i, r in enumerate(results):
            assert r.accepted is True, f"events[{i}] 应被接受，got dropped_reason={r.dropped_reason}"

        assert len(aggregator_4_actors.snapshots) == 6
        # 6 个 key 全部唯一（identity 派生后 key=surface_id="surf-{view_id}"）
        keys = set(aggregator_4_actors.snapshots.keys())
        assert keys == {
            "surf-research-live",
            "surf-analysis-live",
            "surf-auditor-live",
            "surf-collect-live",
            "surf-grade-live",
            "surf-archive-live",
        }


# ── 测试 8：与 report_surface_state 后端工具联合校验 ──────────────────────


class TestAlignmentWithReportSurfaceState:
    """SurfaceAggregator 与 report_surface_state.py 后端工具的语义对齐。"""

    @pytest.mark.asyncio
    async def test_backend_and_aggregator_agree_on_whitelist(
        self, aggregator_4_actors, reset_phase_tracker
    ):
        """后端工具拒绝的 view_id（白名单外）→ SurfaceAggregator 也拒绝。"""
        # 后端：weekly_reporter 试图 emit 'research-live'（不在白名单）
        backend_result = await report_surface_state(
            {
                "actor_id": "weekly_reporter",
                "view_id": "research-live",
                "phase": "started",
                "components": [{"type": "AoProgress", "value": 0}],
                "data_model": {"title": "t", "progress": 0, "primary_tone": "info"},
            }
        )
        assert backend_result["ok"] is False
        assert backend_result["error_code"] == "view_id_not_in_whitelist"

        # 聚合器：同一事件
        agg_result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="weekly_reporter",
                surface_state=_make_surface_state(
                    "research-live", "started", {"title": "t", "progress": 0, "primary_tone": "info"}
                ),
            )
        )
        assert agg_result.accepted is False
        assert agg_result.dropped_reason == "view_id_not_in_whitelist"

    @pytest.mark.asyncio
    async def test_backend_and_aggregator_agree_on_unknown_actor(
        self, aggregator_4_actors, reset_phase_tracker
    ):
        """后端工具拒绝的 actor_id（profile 缺失）→ 聚合器也拒绝。"""
        backend_result = await report_surface_state(
            {
                "actor_id": "rogue_actor",
                "view_id": "rogue-live",
                "phase": "started",
                "components": [{"type": "AoProgress", "value": 0}],
                "data_model": {"title": "t", "progress": 0, "primary_tone": "info"},
            }
        )
        assert backend_result["ok"] is False
        assert backend_result["error_code"] == "view_id_not_in_whitelist"

        agg_result = aggregator_4_actors.apply(
            AggregationEvent(
                actor_id="rogue_actor",
                surface_state=_make_surface_state(
                    "rogue-live", "started", {"title": "t", "progress": 0, "primary_tone": "info"}
                ),
            )
        )
        assert agg_result.accepted is False
        assert agg_result.dropped_reason == "unknown_actor"


# ── 测试 9：与前端 SupervisionPanel.applySurfaceStateEvent 的 key 格式对齐 ─


class TestFrontendParity:
    """Python SurfaceAggregator key 格式必须与前端 SupervisionPanel.applySurfaceStateEvent 一致。"""

    def test_key_format_matches_frontend(self):
        """_key 仍是 legacy fallback 格式 ``f"{actor_id}::{view_id}"``；主聚合 key
        现在优先用 surface_id（identity 派生），由 ``_surface_key`` 派生。"""
        from orchestrator.surface_aggregator import _key, _surface_key

        assert _key("research", "research-live") == "research::research-live"
        assert _key("weekly_reporter", "collect-live") == "weekly_reporter::collect-live"
        # surface_id 优先（与前端 SupervisionPanel.tsx 一致）：
        assert _surface_key(
            {"surface_id": "abc", "view_id": "research-live"},
            "research", "research-live",
        ) == "abc"
        # 无 surface_id 时回退 legacy 格式（兼容旧事件）：
        assert _surface_key({}, "research", "research-live") == "research::research-live"

    def test_phase_order_matches_frontend(self):
        """PHASE_ORDER 与 SupervisionPanel.tsx:35-40 完全一致。"""
        assert PHASE_ORDER == {
            "started": 0,
            "partial": 1,
            "final": 2,
            "superseded": 3,
        }

    def test_should_replace_supersede_rule_matches_frontend(self):
        """superseded 总是替换，与 SupervisionPanel.tsx:44 一致。"""
        assert should_replace("started", "superseded") is True
        assert should_replace("partial", "superseded") is True
        assert should_replace("final", "superseded") is True
        assert should_replace("superseded", "superseded") is True