"""SurfaceAggregator — v99.5 P0.10 业务级 surface 状态聚合器。

设计动机（详见 docs/reconstruction/agentops-v99.5-a2ui-design.md §3.2.5）：
  - 后端 ``report_surface_state`` 工具只校验单次 emit 合法性，不维护 (actor_id, view_id)
    维度的全局聚合状态
  - 前端 SupervisionPanel 的 ``applySurfaceStateEvent`` reducer 负责按 key 聚合
    + phase 单调推进，但只在浏览器内存里
  - P0.10 抽出一份 Python 等价实现，让后端可以独立测试「6 个 snapshot 同时跑」
    场景（multi-actor-live-report 3 个 actor + weekly-report 3 个节点）

与 ``tools/report_surface_state.py`` 的关系：
  - ``report_surface_state`` 是 emit-side 校验（单次）
  - ``SurfaceAggregator`` 是 aggregate-side 校验（多次，含去重 + 推进 + 白名单）

与前端 ``SupervisionPanel.applySurfaceStateEvent`` 的关系：
  - 两者逻辑必须等价（PHASE_ORDER、shouldReplace、key 格式完全一致）
  - 改一侧必须同步另一侧（保持 Python ↔ TS parity）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from orchestrator.actor_visual_profile import (
    ActorVisualProfile,
    load_actor_visual_profile,
)

logger = logging.getLogger(__name__)


# 与 orchestrator/actor_visual_profile.py PHASE_ORDER 完全对齐
# 前端 SupervisionPanel.tsx:35-40 也是同一份，必须保持同步
PHASE_ORDER: dict[str, int] = {
    "started": 0,
    "partial": 1,
    "final": 2,
    "superseded": 3,
}
VALID_PHASES = set(PHASE_ORDER.keys())


class SurfaceAggregatorError(Exception):
    """surface 聚合错误（白名单 / phase 回退 / 字段缺失）。"""


@dataclass
class SupervisionSnapshot:
    """单个 (actor_id, view_id) 当前 surface snapshot。

    与前端 ``SupervisionPanel.tsx:22-29`` 的 ``SupervisionSnapshot`` 字段对齐。
    """
    view_id: str
    actor_id: str
    surface_id: str
    phase: str
    emitted_at: str
    surface_state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "actor_id": self.actor_id,
            "surface_id": self.surface_id,
            "phase": self.phase,
            "emitted_at": self.emitted_at,
            "surface_state": self.surface_state,
        }


@dataclass
class AggregationEvent:
    """单次 surface emit 事件（与 DagEvent.payload.surface_state 配合）。

    字段与前端 ``applySurfaceStateEvent`` 接受的 event 形状对齐：
    ``{actor_id?: string, surface_state: SurfaceState}``。
    """
    actor_id: str | None
    surface_state: dict[str, Any]


@dataclass
class ApplyResult:
    """apply() 调用的结果（不只是 by_key，还含是否被丢弃）。"""
    snapshots: dict[str, SupervisionSnapshot] = field(default_factory=dict)
    accepted: bool = False
    dropped_reason: str | None = None
    """被丢弃时的原因，取值：
      - "unknown_actor": actor_id 不在 profiles 白名单
      - "view_id_not_in_whitelist": view_id 不在该 actor 的 allowed_surface_views
      - "phase_not_monotonic": 新 phase < 旧 phase（且不是 superseded）
      - "missing_surface_state": surface_state 字段缺失
      - "missing_view_id" / "missing_phase": 字段缺失
      - "invalid_phase": phase 不在 VALID_PHASES
    """


def _key(actor_id: str, view_id: str) -> str:
    """聚合 key 格式（legacy fallback）：``f"{actor_id}::{view_id}"``。"""
    return f"{actor_id}::{view_id}"


def _surface_key(surface_state: dict[str, Any], actor_id: str, view_id: str) -> str:
    """聚合 key：优先 surface_id（identity 派生），
    无 surface_id 时回退 ``f"{actor_id}::{view_id}"``（legacy 事件兼容）。

    与前端 ``SupervisionPanel.applySurfaceStateEvent`` 的 key 计算完全对齐：
    - report_surface_state / upsert_generated_view：surface_id 稳定 per (actor, view)
      → 一张卡演进（与旧 actor::view 行为等价）
    - present_content：每次调用（未传 widget_id）身份派生新 surface_id
      → 每次调用一张新卡片（累积展示，修复「多次调用合并为一张卡」）
    """
    sid = (surface_state.get("surface_id") or "").strip() if isinstance(surface_state, dict) else ""
    return sid or _key(actor_id, view_id)


def should_replace(old_phase: str, new_phase: str) -> bool:
    """是否允许替换旧 snapshot（与 SupervisionPanel.tsx:43-46 一致）。

    规则：
      - 新 phase = superseded → 总是允许
      - 否则新 phase order >= 旧 phase order → 允许
    """
    if new_phase == "superseded":
        return True
    if old_phase not in PHASE_ORDER or new_phase not in PHASE_ORDER:
        return False
    return PHASE_ORDER[new_phase] >= PHASE_ORDER[old_phase]


@dataclass
class SurfaceAggregator:
    """维护 ``{surface_id: SupervisionSnapshot}`` 聚合状态 + view_id 白名单。

    聚合 key 优先用 surface_state.surface_id（identity 派生，Worker 注入），
    legacy 事件（无 surface_id）回退 ``f"{actor_id}::{view_id}"``。

    用法::

        agg = SurfaceAggregator.with_known_actors(["research", "synthesis",
                                                     "auditor", "weekly_reporter"])
        result = agg.apply(AggregationEvent(actor_id="research",
                                            surface_state={...}))
        if result.accepted:
            for snap in result.snapshots.values():
                render(snap)

    白名单加载：
      - ``load_profiles`` 为 True → 自动调 ``load_actor_visual_profile(actor_id)``
      - 否则只信任 actor_id 字符串（不校验 view_id 是否在白名单）

    测试时可显式构造 ``profiles: dict[str, ActorVisualProfile]`` 注入。
    """
    profiles: dict[str, ActorVisualProfile] = field(default_factory=dict)
    snapshots: dict[str, SupervisionSnapshot] = field(default_factory=dict)

    @classmethod
    def with_known_actors(
        cls,
        actor_ids: list[str],
        load_profiles: bool = True,
    ) -> "SurfaceAggregator":
        """构造时预加载一组 actor 的 profile。

        Args:
            actor_ids: actor_id 列表
            load_profiles: True → 自动 ``load_actor_visual_profile(actor_id)``；
                           False → 只信任 actor_id（视所有 view_id 都允许，
                           适合单元测试 reducer 本身）

        Returns:
            配置好的 ``SurfaceAggregator``
        """
        profiles: dict[str, ActorVisualProfile] = {}
        for aid in actor_ids:
            if load_profiles:
                profiles[aid] = load_actor_visual_profile(aid)
            else:
                profiles[aid] = ActorVisualProfile(actor_id=aid)
        return cls(profiles=profiles)

    def register_profile(self, profile: ActorVisualProfile) -> None:
        """注册单个 actor profile（用于动态加载 / 测试注入）。"""
        self.profiles[profile.actor_id] = profile

    def known_actors(self) -> list[str]:
        return list(self.profiles.keys())

    def is_view_allowed(self, actor_id: str, view_id: str) -> bool:
        """检查 (actor_id, view_id) 是否在白名单。

        - actor_id 未知 → False
        - view_id 不在该 actor 的 allowed_surface_views → False
        - profiles 加载失败 / profile 为空 → False（保守拒绝）

        注意：``ActorVisualProfile.has_view`` 对空 profile 永远返回 False，
        这正是我们要的「未知 actor / 未声明白名单 → 拒绝」语义。
        """
        profile = self.profiles.get(actor_id)
        if profile is None:
            return False
        return profile.has_view(view_id)

    def apply(self, event: AggregationEvent) -> ApplyResult:
        """处理一条 surface emit 事件，返回新状态 + 是否被接受。

        行为：
          - accepted=True → ``self.snapshots`` 被原地更新（key→snapshot），
            同时 ``ApplyResult.snapshots`` 也指向同一份新状态
          - accepted=False → ``self.snapshots`` 保持不变，``dropped_reason`` 解释原因

        调用方可以检查 ``result.accepted`` 决定是否要 warn / log。
        """
        # ── 1. 字段提取 + 基础校验 ──
        surface_state = event.surface_state
        if not surface_state or not isinstance(surface_state, dict):
            return ApplyResult(
                snapshots=self.snapshots,
                accepted=False,
                dropped_reason="missing_surface_state",
            )

        view_id = surface_state.get("view_id")
        phase = surface_state.get("phase")
        surface_id = surface_state.get("surface_id", "")

        if not view_id:
            return ApplyResult(
                snapshots=self.snapshots,
                accepted=False,
                dropped_reason="missing_view_id",
            )
        if not phase:
            return ApplyResult(
                snapshots=self.snapshots,
                accepted=False,
                dropped_reason="missing_phase",
            )
        if phase not in VALID_PHASES:
            return ApplyResult(
                snapshots=self.snapshots,
                accepted=False,
                dropped_reason="invalid_phase",
            )

        actor_id = event.actor_id or surface_state.get("surface_properties", {}).get(
            "agentDisplayName"
        ) or "unknown"

        # ── 2. 白名单校验 ──
        if actor_id not in self.profiles:
            return ApplyResult(
                snapshots=self.snapshots,
                accepted=False,
                dropped_reason="unknown_actor",
            )
        if not self.is_view_allowed(actor_id, view_id):
            return ApplyResult(
                snapshots=self.snapshots,
                accepted=False,
                dropped_reason="view_id_not_in_whitelist",
            )

        # ── 3. phase 单调推进（per-surface；surface_id 主键，actor::view 回退） ──
        key = _surface_key(surface_state, actor_id, view_id)
        old = self.snapshots.get(key)
        if old is not None and not should_replace(old.phase, phase):
            return ApplyResult(
                snapshots=self.snapshots,
                accepted=False,
                dropped_reason="phase_not_monotonic",
            )

        # ── 4. 写入新 snapshot（原地更新 + 返回同一份引用） ──
        emitted_at = surface_state.get("emitted_at") or datetime.now(timezone.utc).isoformat()
        snap = SupervisionSnapshot(
            view_id=view_id,
            actor_id=actor_id,
            surface_id=surface_id,
            phase=phase,
            emitted_at=emitted_at,
            surface_state=surface_state,
        )
        self.snapshots[key] = snap
        return ApplyResult(
            snapshots=self.snapshots,
            accepted=True,
            dropped_reason=None,
        )

    def apply_batch(self, events: list[AggregationEvent]) -> list[ApplyResult]:
        """批量处理事件，每条独立返回 ApplyResult（用于 E2E 测试）。"""
        return [self.apply(ev) for ev in events]

    def reset(self) -> None:
        """清空所有 snapshot（保留 profiles）。测试辅助。"""
        self.snapshots.clear()