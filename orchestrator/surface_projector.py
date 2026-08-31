"""SurfaceProjector — OPT-1 系统投影层（source=system）。

设计原则：「A2UI 卡片是 DAG 状态机的确定性投影」：
  - 骨架卡片由系统在节点启动时自动投影（roster 预建骨架等价实现），
    即使 agent 全程不调 report_surface_state，前端也能看到 started 骨架卡
  - agent 后续 emit 的业务卡片（source=agent）按 phase 单调推进自然覆盖骨架
  - 同一 (actor_id, view_id) 聚合 key 下，骨架 started(0) → agent partial(1)
    → final(2)，无需额外合并逻辑

与 tools/report_surface_state.py 的关系：
  - agent 路径：LLM 调工具 → 校验链 → emit DagEvent（source=agent）
  - system 路径：本模块在节点启动时直接构造骨架 SurfaceState → 复用
    report_surface_state 校验（phase tracker / dedup / data_model 约束）
    → emit DagEvent（source=system）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from orchestrator.actor_visual_profile import (
    ActorVisualProfileError,
    load_actor_visual_profile,
    make_skeleton_data_model,
    resolve_actor_id_from_node,
)

logger = logging.getLogger(__name__)


async def project_node_started_skeleton(
    node: Any,
    run_id: str,
    event_sink: "Callable[[Any], Awaitable[None]] | None",
) -> bool:
    """节点启动时投影骨架 surface（phase=started, source=system）。

    条件（全部满足才投影）：
      - event_sink 可用（无 SSE 通道则无意义）
      - actor 有 visual profile 且恰好 1 个 view（与 handoff 铁律约束一致）
      - 该 view 声明了 template（无模板无法确定性渲染骨架）

    Returns:
        True 表示骨架已投影；False 表示跳过（条件不满足 / 投影失败已降级日志）
    """
    if event_sink is None:
        return False

    actor_id = resolve_actor_id_from_node(node)
    if not actor_id:
        return False

    try:
        profile = load_actor_visual_profile(actor_id)
    except ActorVisualProfileError as e:
        logger.debug("surface_projector 加载 profile 失败 actor=%s: %s", actor_id, e)
        return False

    # 恰好 1 个 view 才投影（与 _check_surface_final_violation 约束对齐）
    if len(profile.allowed_surface_views) != 1:
        return False
    view = next(iter(profile.allowed_surface_views.values()))
    if view.template is None:
        return False

    # 延迟 import 避免循环依赖
    from orchestrator.protocol import DagEvent, DagEventType
    from tools.report_surface_state import report_surface_state

    skeleton_data_model = make_skeleton_data_model(view)
    # 骨架注入 run 信息，保证跨 run 的 surface_id 不同（dedup 不会吞第二次 run 的骨架）
    skeleton_data_model["_run_id"] = run_id

    result = await report_surface_state({
        "actor_id": actor_id,
        "view_id": view.view_id,
        "phase": "started",
        "data_model": skeleton_data_model,
        "source": "system",
        "surface_properties": {
            "agentDisplayName": actor_id,
        },
    })

    if not isinstance(result, dict) or not result.get("ok"):
        logger.warning(
            "surface_projector 骨架投影被拒绝: actor=%s view=%s err=%s",
            actor_id, view.view_id, result.get("error") if isinstance(result, dict) else result,
        )
        return False

    if result.get("deduplicated"):
        # 同 run 内重复启动（如 retry）→ 骨架已存在，视为成功
        return True

    surface_payload = result.get("surface")
    if not isinstance(surface_payload, dict):
        return False

    try:
        from orchestrator.protocol import SurfaceState
        surface_state = SurfaceState.from_payload(surface_payload)
        await event_sink(
            DagEvent(
                type=DagEventType.REPORT_SURFACE_STATE,
                run_id=run_id,
                node_id=getattr(node, "id", None),
                payload={
                    "surface_state": surface_state.to_payload(),
                    "actor_id": actor_id,
                    "view_id": surface_state.view_id,
                    "phase": surface_state.phase,
                    "source": "system",
                },
                surface_state=surface_state,
            )
        )
        logger.info(
            "surface_projector 骨架已投影: actor=%s view=%s node=%s run=%s "
            "surface_id=%s",
            actor_id, view.view_id, getattr(node, "id", "?"), run_id,
            surface_state.surface_id[:12],
        )
        return True
    except Exception as e:
        # 投影失败不阻断节点执行（骨架只是增强，不是必需）
        logger.warning("surface_projector emit DagEvent 失败（不影响节点执行）: %s", e)
        return False


async def project_node_final_fallback(
    node: Any,
    run_id: str,
    event_sink: "Callable[[Any], Awaitable[None]] | None",
) -> bool:
    """节点完成时 final 兜底投影（phase=final, source=system）。

    稳定输出措施：A2UI 卡片的 final 状态由系统兜底保证，
    agent emit 只是增强 —— UI 完整性永远不依赖 agent 自觉。

    行为：
      - actor 的 view 已到 final（agent 已 emit）→ 不做任何事（返回 True）
      - 未到 final（agent 忘调/只调了 partial）→ 从「最后 agent data_model
        （若有）+ 骨架默认值 + progress=100」合成系统 final 卡投影，
        保证 L0 铁律校验（DAG_HANDOFF_SURFACE_INCOMPLETE）自然通过

    条件与骨架投影一致：event_sink 可用 + 恰好 1 个 view + view 声明 template。

    Returns:
        True 表示 view 已处于/已达 final；False 表示跳过或投影失败（已降级日志）。
    """
    if event_sink is None:
        return False

    actor_id = resolve_actor_id_from_node(node)
    if not actor_id:
        return False

    try:
        profile = load_actor_visual_profile(actor_id)
    except ActorVisualProfileError as e:
        logger.debug("final 兜底投影加载 profile 失败 actor=%s: %s", actor_id, e)
        return False

    if len(profile.allowed_surface_views) != 1:
        return False
    view = next(iter(profile.allowed_surface_views.values()))
    if view.template is None:
        return False

    from tools.report_surface_state import (
        _PHASE_TRACKER,
        get_last_data_model,
        report_surface_state,
    )

    last_phase = _PHASE_TRACKER.get(actor_id, {}).get(view.view_id)
    if last_phase == "final":
        return True  # agent 已 emit final，无需兜底

    # 合成兜底 data_model：骨架默认值 ← 最后 agent data_model（业务数据保留）
    data_model = make_skeleton_data_model(view)
    last_dm = get_last_data_model(actor_id, view.view_id)
    if last_dm:
        data_model.update({k: v for k, v in last_dm.items() if not k.startswith("_")})
    # progress 类字段强制满格（final 语义）
    if "progress" in data_model:
        data_model["progress"] = 100
    data_model["_run_id"] = run_id  # 跨 run surface_id 去重

    result = await report_surface_state({
        "actor_id": actor_id,
        "view_id": view.view_id,
        "phase": "final",
        "data_model": data_model,
        "source": "system",
        "surface_properties": {
            "agentDisplayName": actor_id,
        },
    })

    if not isinstance(result, dict) or not result.get("ok"):
        logger.warning(
            "final 兜底投影被拒绝: actor=%s view=%s last_phase=%s err=%s",
            actor_id, view.view_id, last_phase,
            result.get("error") if isinstance(result, dict) else result,
        )
        return False

    if result.get("deduplicated"):
        return True

    surface_payload = result.get("surface")
    if not isinstance(surface_payload, dict):
        return False

    try:
        from orchestrator.protocol import DagEvent, DagEventType, SurfaceState
        surface_state = SurfaceState.from_payload(surface_payload)
        await event_sink(
            DagEvent(
                type=DagEventType.REPORT_SURFACE_STATE,
                run_id=run_id,
                node_id=getattr(node, "id", None),
                payload={
                    "surface_state": surface_state.to_payload(),
                    "actor_id": actor_id,
                    "view_id": surface_state.view_id,
                    "phase": surface_state.phase,
                    "source": "system",
                },
                surface_state=surface_state,
            )
        )
        logger.info(
            "surface_projector final 兜底已投影: actor=%s view=%s node=%s run=%s "
            "last_phase=%s surface_id=%s",
            actor_id, view.view_id, getattr(node, "id", "?"), run_id,
            last_phase, surface_state.surface_id[:12],
        )
        return True
    except Exception as e:
        logger.warning("surface_projector final 兜底 emit DagEvent 失败: %s", e)
        return False


def collect_workflow_actor_ids(workflow: Any) -> list[str]:
    """收集 workflow 全部节点的 actor_id（run 启动时重置 surface 状态用）。"""
    actor_ids: set[str] = set()
    nodes = getattr(workflow, "nodes", None)
    if nodes is None:
        return []
    node_list = nodes.values() if isinstance(nodes, dict) else nodes
    for node in node_list:
        actor_id = resolve_actor_id_from_node(node)
        if actor_id:
            actor_ids.add(actor_id)
    return sorted(actor_ids)


def _now_iso() -> str:
    return datetime.now().isoformat()
