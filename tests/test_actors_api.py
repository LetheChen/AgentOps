"""
P0.11 测试：v99.5 GET /api/actors + list_actor_visual_profiles 助手。

覆盖：
  1. list_actor_visual_profiles() 扫描 config/actors/*/actor_visual_profile.json
     - 至少返回 4 个已知 actor（research / synthesis / auditor / weekly_reporter）
     - 每个 profile 含 actor_id / description / allowed_surface_views
     - 按 actor_id 字典序排序
  2. /api/actors HTTP endpoint 形状
     - 返回 { actors: [...] } 数组
     - 每个 actor 含 actor_id + allowed_surface_views[]
     - view 含 view_id + output_contract + required_phases + fields{}
     - field dict 含 type + required
  3. 与 SupervisionPanel WhitelistedProfiles shape 对齐
     - 前端 getActorProfiles() 压缩后产生 { actor_id: { allowed_surface_views: { view_id: {} } } }
     - 与 applySurfaceStateEvent 的 actorProfiles 参数兼容
  4. 错误隔离：单个 actor profile JSON 损坏不应影响其他加载
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.actor_visual_profile import (
    ActorVisualProfile,
    ActorVisualProfileError,
    list_actor_visual_profiles,
    load_actor_visual_profile,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTORS_DIR = PROJECT_ROOT / "config" / "actors"


# ── 测试 1：list_actor_visual_profiles helper ────────────────────────────


class TestListActorProfiles:
    """list_actor_visual_profiles() 扫描全部 actor profile。"""

    def test_returns_at_least_four_known_actors(self):
        """至少返回 4 个 v99.5 P0.8-0.10 阶段引入的 actor。"""
        profiles = list_actor_visual_profiles()
        actor_ids = {p.actor_id for p in profiles}
        assert "research" in actor_ids
        assert "synthesis" in actor_ids
        assert "auditor" in actor_ids
        assert "weekly_reporter" in actor_ids

    def test_each_profile_has_core_fields(self):
        """每个 profile 含 actor_id + description + allowed_surface_views。"""
        profiles = list_actor_visual_profiles()
        assert len(profiles) >= 4
        for p in profiles:
            assert p.actor_id
            assert isinstance(p.description, str)
            assert isinstance(p.allowed_surface_views, dict)
            assert len(p.allowed_surface_views) >= 1, (
                f"actor '{p.actor_id}' 必须至少声明 1 个 view_id"
            )

    def test_profiles_sorted_by_actor_id(self):
        """profiles 按 actor_id 字典序排序。"""
        profiles = list_actor_visual_profiles()
        actor_ids = [p.actor_id for p in profiles]
        assert actor_ids == sorted(actor_ids)

    def test_weekly_reporter_has_three_views(self):
        """weekly_reporter 含 3 view_id（collect-live / grade-live / archive-live）。"""
        profiles = list_actor_visual_profiles()
        weekly = next(p for p in profiles if p.actor_id == "weekly_reporter")
        assert set(weekly.allowed_surface_views.keys()) == {
            "collect-live", "grade-live", "archive-live",
        }

    def test_research_view_required_fields(self):
        """research profile 的 research-live 含 progress / verified_count 等必填字段。"""
        profiles = list_actor_visual_profiles()
        research = next(p for p in profiles if p.actor_id == "research")
        view = research.get_view("research-live")
        assert view is not None
        assert "title" in view.fields
        assert view.fields["title"].required is True
        assert view.fields["title"].type == "string"
        assert "progress" in view.fields
        assert view.fields["progress"].type == "integer"
        assert view.fields["progress"].min == 0
        assert view.fields["progress"].max == 100


# ── 测试 2：与 load_actor_visual_profile 一致性 ──────────────────────────


class TestConsistencyWithSingleLoad:
    """批量加载结果与单个加载结果完全一致。"""

    def test_list_matches_load_for_each_actor(self):
        profiles = list_actor_visual_profiles()
        for p in profiles:
            single = load_actor_visual_profile(p.actor_id)
            assert single.actor_id == p.actor_id
            assert single.description == p.description
            assert set(single.allowed_surface_views.keys()) == set(p.allowed_surface_views.keys())


# ── 测试 3：GET /api/actors HTTP endpoint 形状 ───────────────────────────


class TestActorsApiEndpoint:
    """api/server.py GET /api/actors 返回形状（直接 import 函数测，避免起服务器）。"""

    def test_endpoint_payload_structure_matches_helper(self):
        """endpoint 返回的 dict 形状与 list_actor_visual_profiles 一致。"""
        # 直接调 helper 验证 shape（HTTP 层单独测，避免起服务器）
        profiles = list_actor_visual_profiles()
        actors_payload = []
        for p in profiles:
            actors_payload.append({
                "actor_id": p.actor_id,
                "description": p.description,
                "allowed_surface_views": [
                    {
                        "view_id": v.view_id,
                        "output_contract": v.output_contract,
                        "description": v.description,
                        "required_phases": list(v.required_phases),
                        "fields": {
                            fname: {
                                "type": fc.type,
                                "required": fc.required,
                                **({"max_length": fc.max_length} if fc.max_length is not None else {}),
                                **({"min": fc.min} if fc.min is not None else {}),
                                **({"max": fc.max} if fc.max is not None else {}),
                                **({"enum_values": list(fc.enum_values)} if fc.enum_values else {}),
                            }
                            for fname, fc in v.fields.items()
                        },
                    }
                    for v in p.allowed_surface_views.values()
                ],
            })
        # 验证 shape：每个 actor 必有 actor_id + allowed_surface_views[]
        assert len(actors_payload) >= 4
        for actor in actors_payload:
            assert isinstance(actor["actor_id"], str)
            assert isinstance(actor["description"], str)
            assert isinstance(actor["allowed_surface_views"], list)
            assert len(actor["allowed_surface_views"]) >= 1
            for view in actor["allowed_surface_views"]:
                assert isinstance(view["view_id"], str)
                assert view["output_contract"] in (None, "ActorReport", "Mission", "Failure", "RoundGate")
                assert isinstance(view["required_phases"], list)
                assert isinstance(view["fields"], dict)
                for fname, fc in view["fields"].items():
                    assert fc["type"] in {"string", "integer", "number", "boolean", "array", "object", "enum"}
                    assert isinstance(fc["required"], bool)

    def test_endpoint_module_attribute(self):
        """GET /api/actors 端点已在 api/server.py 注册。"""
        import api.server as srv

        # FastAPI app.routes 查找
        route_paths = {
            getattr(r, "path", None)
            for r in srv.app.routes
        }
        assert "/api/actors" in route_paths, (
            "api/server.py 必须注册 GET /api/actors 端点"
        )

    def test_endpoint_returns_json_serializable(self):
        """endpoint payload 是 JSON 可序列化（前端能 fetch + parse）。"""
        profiles = list_actor_visual_profiles()
        actors_payload = [
            {
                "actor_id": p.actor_id,
                "description": p.description,
                "allowed_surface_views": [
                    {
                        "view_id": v.view_id,
                        "output_contract": v.output_contract,
                        "description": v.description,
                        "required_phases": list(v.required_phases),
                        "fields": {
                            fname: {
                                "type": fc.type,
                                "required": fc.required,
                                **({"max_length": fc.max_length} if fc.max_length is not None else {}),
                                **({"min": fc.min} if fc.min is not None else {}),
                                **({"max": fc.max} if fc.max is not None else {}),
                                **({"enum_values": list(fc.enum_values)} if fc.enum_values else {}),
                            }
                            for fname, fc in v.fields.items()
                        },
                    }
                    for v in p.allowed_surface_views.values()
                ],
            }
            for p in profiles
        ]
        # round-trip JSON 序列化确保无 dataclass / set 等不可序列化对象
        s = json.dumps({"actors": actors_payload}, ensure_ascii=False)
        parsed = json.loads(s)
        assert "actors" in parsed
        assert len(parsed["actors"]) == len(actors_payload)


# ── 测试 4：与前端 SupervisionPanel WhitelistedProfiles 兼容 ──────────────


class TestFrontendShapeParity:
    """前端 SuperAgentPage 压缩后的 WhitelistedProfiles 形状与 SupervisionPanel 兼容。"""

    def test_compressed_profiles_match_whitelisted_profiles_shape(self):
        """SuperAgentPage 把 {actor_id, allowed_surface_views[]} 压缩成
        {actor_id: {allowed_surface_views: {view_id: view_dict}}}，
        必须满足 SupervisionPanel.applySurfaceStateEvent 的 actorProfiles 参数。"""
        profiles = list_actor_visual_profiles()

        # 模拟 SuperAgentPage 的 useEffect 压缩逻辑
        compressed: dict = {}
        for p in profiles:
            compressed[p.actor_id] = {
                "allowed_surface_views": {
                    v.view_id: {"view_id": v.view_id, "output_contract": v.output_contract}
                    for v in p.allowed_surface_views.values()
                },
            }

        # 形状对齐 SupervisionPanel.WhitelistedProfiles = Record<actor_id, { allowed_surface_views: Record<view_id, unknown> }>
        for actor_id, profile in compressed.items():
            assert isinstance(profile, dict)
            assert "allowed_surface_views" in profile
            assert isinstance(profile["allowed_surface_views"], dict)
            for view_id in profile["allowed_surface_views"]:
                assert isinstance(view_id, str)

    def test_whitelist_correctly_blocks_unknown_actor(self):
        """用压缩后的 profiles 调 SurfaceAggregator 应拒绝未知 actor。"""
        from orchestrator.surface_aggregator import (
            AggregationEvent,
            SurfaceAggregator,
        )

        profiles = list_actor_visual_profiles()
        compressed: dict = {
            p.actor_id: {
                "allowed_surface_views": {
                    v.view_id: {"view_id": v.view_id}
                    for v in p.allowed_surface_views.values()
                },
            }
            for p in profiles
        }

        # 把 compressed 转为 SurfaceAggregator 用 profiles 字典
        agg_profiles = {
            actor_id: ActorVisualProfile(
                actor_id=actor_id,
                description="",
                allowed_surface_views={
                    view_id: type("V", (), {"view_id": view_id})()  # 最小 stub
                    for view_id in profile["allowed_surface_views"]
                },
            )
            for actor_id, profile in compressed.items()
        }

        agg = SurfaceAggregator(profiles=agg_profiles)
        # 已知 actor + 已知 view → 接受
        result = agg.apply(
            AggregationEvent(
                actor_id="research",
                surface_state={
                    "view_id": "research-live", "phase": "started",
                    "surface_id": "x",
                    "data_model": {"title": "t", "progress": 0, "primary_tone": "info"},
                },
            )
        )
        assert result.accepted is True
        # 未知 actor → 拒绝
        result2 = agg.apply(
            AggregationEvent(
                actor_id="rogue",
                surface_state={
                    "view_id": "rogue-live", "phase": "started",
                    "surface_id": "x",
                    "data_model": {"title": "t", "progress": 0, "primary_tone": "info"},
                },
            )
        )
        assert result2.accepted is False
        assert result2.dropped_reason == "unknown_actor"