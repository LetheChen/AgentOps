"""report_surface_state 工具 handler — Agent 主动 emit 生成式 UI surface snapshot。

设计动机（详见 docs/reconstruction/agentops-v99.5-a2ui-design.md §3.2）：
  - Agent 在执行期间任意次调 report_surface_state 推送 partial surface snapshot
  - 前端 SupervisionPanel 按 view_id 路由到对应 A2UI 组件，实时展示
  - 与 NODE_COMPLETED 不同：report_surface_state 是 agent 主动 emit，可推多次
    （started → partial → final 单调推进）；NODE_COMPLETED 是节点终态一次性 emit

校验链（纯函数思路，每个阶段独立可单测）：
  1. view_id 在 actor allowed_surface_views 白名单
  2. data_model 符合 fields 类型约束（required/max_length/min/max/enum）
  3. components 是有效 A2UI 组件树（30+ 组件白名单）
  4. output_contract 与 view_id 声明一致
  5. phase 单调推进（per-view 维度，superseded 可从任意阶段进入）
  6. surface_id = sha256(run_id + actor_id + view_id)（identity 派生，Worker 注入）
     内容指纹 dedup：相同 (view, phase, data_model) 不重复 emit（幂等重试保护）

DESIGN surface identity 锁定范式：
  - surface_id 由调用方 Worker 身份派生，模型不可指定（args 带 surface_id → identity_spoof 拒绝）
  - 同一 (actor, view) 的多次 emit = 同一 surface 的多个 patch，patch_sequence 单调递增
  - 前端 Composer 按 surface_id 聚合，同 surface 显示最新 patch（一张卡演进）

函数签名：async def(args, config) -> dict（与 tools/emit_alert.py 一致）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from orchestrator.actor_visual_profile import (
    ActorVisualProfileError,
    FieldConstraint,
    ViewDeclaration,
    compute_components_digest,
    compute_surface_id,
    compute_surface_id_identity,
    load_actor_visual_profile,
    normalize_components,
    render_view_template,
    validate_components,
    validate_phase_monotonic,
)
from orchestrator.protocol import SurfaceState

logger = logging.getLogger(__name__)


# ── 模块级状态：per-view phase tracker ──────────────────────────
# 维护 {actor_id: {view_id: last_phase}} 用于单调推进校验。
# 生产环境应替换为 redis 等共享存储；当前按 in-memory 兜底。
_PHASE_TRACKER: dict[str, dict[str, str]] = {}
# 内容指纹 dedup（防止相同 (view, phase, data_model) 重复 emit，幂等重试保护）
# key = compute_surface_id(view_id, phase, data_model)（内容 digest，非 surface 身份）
_SURFACE_DEDUP: dict[str, str] = {}  # content_digest → actor_id/view_id
# patch_sequence tracker：{actor_id/view_id: last_seq}，同一 surface 每次 emit 递增
_PATCH_SEQ: dict[str, int] = {}
# OPT-1：最后一次成功 emit 的 data_model（actor/view 维度），
# 供 surface_projector final 兜底投影合成「系统 final 卡」用
_LAST_DATA_MODEL: dict[str, dict[str, Any]] = {}


def _reset_phase_tracker(actor_id: str | None = None) -> None:
    """测试辅助：清空 phase tracker / dedup / patch seq。"""
    global _PHASE_TRACKER, _SURFACE_DEDUP, _LAST_DATA_MODEL
    if actor_id is None:
        _PHASE_TRACKER.clear()
        _SURFACE_DEDUP.clear()
        _LAST_DATA_MODEL.clear()
        _PATCH_SEQ.clear()
    else:
        _PHASE_TRACKER.pop(actor_id, None)
        for key in [k for k in _LAST_DATA_MODEL if k.startswith(f"{actor_id}/")]:
            del _LAST_DATA_MODEL[key]
        for key in [k for k in _PATCH_SEQ if k.startswith(f"{actor_id}/")]:
            del _PATCH_SEQ[key]
        # 不清 _SURFACE_DEDUP（其他 actor 的不能误删）


def get_last_data_model(actor_id: str, view_id: str) -> dict[str, Any] | None:
    """OPT-1：取该 actor/view 最后一次成功 emit 的 data_model（无则 None）。"""
    dm = _LAST_DATA_MODEL.get(f"{actor_id}/{view_id}")
    return dict(dm) if isinstance(dm, dict) else None


def reset_run_surface_state(actor_ids: list[str]) -> None:
    """OPT-1：run 启动时清理相关 actor 的 surface 状态（phase tracker + dedup）。

    修复跨 run 残留 bug：同一进程内第二次 run 同一 actor 时，
    残留的 final phase 会拒绝新的 started（phase_not_monotonic），
    残留的 surface_id dedup 会吞掉事件（前端收不到卡片）。
    DagEngine 在 run 启动时调用本函数。
    """
    for actor_id in actor_ids:
        _PHASE_TRACKER.pop(actor_id, None)
        for key in [k for k in _LAST_DATA_MODEL if k.startswith(f"{actor_id}/")]:
            del _LAST_DATA_MODEL[key]
        for key in [k for k in _PATCH_SEQ if k.startswith(f"{actor_id}/")]:
            del _PATCH_SEQ[key]
    for sid in [
        sid for sid, owner in _SURFACE_DEDUP.items()
        if owner.split("/", 1)[0] in set(actor_ids)
    ]:
        del _SURFACE_DEDUP[sid]


def _validate_reachable(components: list[dict]) -> list[str]:
    """校验所有组件从 root 可达。

    防止 dangling children bug（如 _map_progress 曾出现 s1 未加入 root.children
    导致前端 A2uiRenderer 抛 "component is unreachable from root" 降级卡片）。

    后端校验只查 schema structural 不查 reachable，导致 bug 在"后端通过 → 前端降级"
    的缝隙里漏出。本函数在后端补齐 reachable 检查。

    root 查找逻辑：root = 不被任何组件 children 引用的组件（恰好一个）。
    这比硬编码 id == "root" 更健壮，支持 prefix 模式（如 sec1_root）。
    """
    if not components:
        return ["empty components"]

    # 收集所有被引用的 id（任何组件的 children 或 child 中出现的 id）
    referenced: set[str] = set()
    for c in components:
        children = c.get("children")
        if isinstance(children, list):
            for cid in children:
                if isinstance(cid, str):
                    referenced.add(cid)
        # A2UI Button 组件用 child（单数）引用文本子组件
        child_single = c.get("child")
        if isinstance(child_single, str):
            referenced.add(child_single)

    # root = 不被任何组件引用的组件（应该恰好一个）
    roots = [c for c in components if c.get("id") and c.get("id") not in referenced]
    if len(roots) == 0:
        return ["no root component (all components are referenced by others)"]

    # 第一个无父引用组件作为 root，其余检查是否从 root 可达
    root_id = roots[0].get("id")

    reachable: set[str] = set()

    def mark_reachable(comp_id: str) -> None:
        if comp_id in reachable:
            return
        reachable.add(comp_id)
        comp = next((c for c in components if c.get("id") == comp_id), None)
        if comp:
            # children 数组（容器组件：Column/Row/AoGrid 等）
            if isinstance(comp.get("children"), list):
                for child_id in comp["children"]:
                    if isinstance(child_id, str):
                        mark_reachable(child_id)
            # child 单个引用（Button 用 child 引用文本子组件）
            child_single = comp.get("child")
            if isinstance(child_single, str):
                mark_reachable(child_single)

    mark_reachable(root_id)

    # 所有不在 reachable 集合里的组件都是 dangling（从 root 不可达）
    issues: list[str] = []
    for c in components:
        cid = c.get("id")
        if cid and cid not in reachable:
            issues.append(f"component is unreachable from root: {cid}")
    return issues


async def report_surface_state(
    args: dict[str, Any], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Agent 主动 emit 生成式 UI surface snapshot。

    Args:
        args: {
            "actor_id": "research",                # 必填
            "view_id": "research-live",            # 必填，必须在 actor allowed_surface_views 白名单
            "phase": "started|partial|final|superseded",  # 必填，单调推进
            "components": [...],                   # 必填，A2UI 组件树（30+ 组件）
            "data_model": {...},                   # 必填，符合 fields 约束
            "surface_properties": {...} (可选),
            "output_contract": "ActorReport" (可选，建议填)
        }
        config: 工具级配置（暂未使用，保留扩展位）

    Returns:
        成功：{
            "ok": True,
            "surface_id": "sha256...",        # 身份派生（actor+view 稳定，跨 phase 不变）
            "patch_sequence": 3,               # 同 surface 内单调递增
            "components_digest": "sha256...",
            "view_id": "research-live",
            "phase": "partial",
            "emitted_at": "2026-08-09T...",
        }
        失败：{"ok": False, "error": "...", "error_code": "view_id_not_in_whitelist|identity_spoof|..."}
    """
    # ── 0. identity_spoof 检查（surface 身份由 Worker 派生，模型不可指定） ──
    if args.get("surface_id"):
        return _err(
            "identity_spoof",
            "surface_id 由 Worker 按调用身份派生，不可通过 args 指定（拒绝注入）",
        )

    # ── 1. 解析必填参数 ──
    actor_id = (args.get("actor_id") or "").strip()
    view_id = (args.get("view_id") or "").strip()
    phase = (args.get("phase") or "").strip()
    components = args.get("components")
    data_model = args.get("data_model")
    # OPT-1 source 字段：system 投影（surface_projector）可信，跳过模板渲染循环
    source = args.get("source") or "agent"

    if not actor_id:
        return _err("missing_actor_id", "actor_id 必填")
    if not view_id:
        return _err("missing_view_id", "view_id 必填")
    if not phase:
        return _err("missing_phase", "phase 必填（started/partial/final/superseded）")
    if data_model is None:
        return _err("missing_data_model", "data_model 必填")

    # ── 2. 加载 profile + view 声明 ──
    try:
        profile = load_actor_visual_profile(actor_id)
    except ActorVisualProfileError as e:
        return _err("profile_load_failed", f"加载 actor profile 失败: {e}")

    view = profile.get_view(view_id)
    if view is None:
        return _err(
            "view_id_not_in_whitelist",
            f"view_id='{view_id}' 不在 actor '{actor_id}' 的 allowed_surface_views 白名单",
        )

    # ── 3. output_contract 一致性校验 ──
    args_contract = args.get("output_contract")
    if args_contract is not None and view.output_contract is not None:
        if args_contract != view.output_contract:
            return _err(
                "output_contract_mismatch",
                f"view '{view_id}' 声明 output_contract='{view.output_contract}'，"
                f"args 给 '{args_contract}'，不一致",
            )

    # ── 4. data_model fields 约束校验 ──
    try:
        view.validate_data_model(data_model)
    except ActorVisualProfileError as e:
        return _err("data_model_invalid", str(e))

    # ── 5. components 解析（OPT-1 fields-only 模式） ──
    # 优先级：
    #   a. agent 显式传 components（向后兼容，present_content_surface 走此路径）
    #   b. view 声明 template → render_view_template 确定性渲染（fields-only 推荐路径）
    #   c. 都没有 → 报错
    if components is None:
        if view.template is not None:
            try:
                components = render_view_template(view, data_model)
                logger.info(
                    "report_surface_state fields-only 渲染: actor=%s view=%s phase=%s "
                    "template_components=%d",
                    actor_id, view_id, phase, len(components),
                )
            except ActorVisualProfileError as e:
                return _err("template_render_failed", str(e))
        else:
            return _err(
                "missing_components",
                "components 必填（A2UI 组件树），或在该 view 的 "
                "actor_visual_profile.json 中声明 template 启用 fields-only 模式",
            )

    # normalize + AoList/AoTable 内联数据注入（LLM 常见格式偏差容错）
    try:
        components = normalize_components(components)
        # 把 AoList/AoTable 等的内联数据注入到 data_model
        for comp in components:
            inline_data = comp.pop("_inline_data", None)
            if inline_data is not None:
                src_ref = comp.get("source", {})
                path = src_ref.get("path", "") if isinstance(src_ref, dict) else ""
                if path:
                    # 把数据放到 data_model 的指定路径
                    data_model[path.lstrip("/")] = inline_data
        args["components"] = components  # 回写，让后续 SurfaceState 用规范数据
        validate_components(components)
    except ActorVisualProfileError as e:
        return _err("components_invalid", str(e))

    # ── 5b. reachable 校验（防止 dangling children 导致前端降级） ──
    reachable_issues = _validate_reachable(components)
    if reachable_issues:
        return _err(
            "components_unreachable",
            f"A2UI 组件树存在不可达组件（前端会降级）: {'; '.join(reachable_issues)}",
        )

    # ── 6. phase 单调推进校验 ──
    tracker = _PHASE_TRACKER.setdefault(actor_id, {})
    try:
        validate_phase_monotonic(view_id, phase, tracker)
    except ActorVisualProfileError as e:
        return _err("phase_not_monotonic", str(e))

    # ── 7. surface_id 身份派生 + 内容指纹 dedup + patch_sequence ──
    # 身份派生 surface_id：
    #   surface_id = sha256(run_id + actor_id + view_id)，跨 phase 稳定。
    #   同一 (actor, view) 的 started/partial/final 是同一 surface 的多个 patch，
    #   前端 Composer 按 surface_id 聚合，一张卡演进（不再按内容 hash 拆成多张卡）。
    surface_id = compute_surface_id_identity("", actor_id, view_id)
    components_digest = compute_components_digest(components)

    # 内容指纹 dedup（幂等重试保护）：相同 (view, phase, data_model) 不重复 emit。
    # compute_surface_id 在这里只作为内容 digest 使用（不再作为 surface 身份）。
    content_digest = compute_surface_id(view_id, phase, data_model)
    if content_digest in _SURFACE_DEDUP:
        existing = _SURFACE_DEDUP[content_digest]
        if existing != f"{actor_id}/{view_id}":
            # digest 冲突（不同 actor/view 算出相同 hash），按异常处理
            return _err(
                "surface_id_collision",
                f"content_digest='{content_digest[:12]}...' 已被 '{existing}' 占用，digest 冲突",
            )
        logger.info(
            "report_surface_state 跳过重复 emit: actor=%s view=%s phase=%s digest=%s",
            actor_id,
            view_id,
            phase,
            content_digest[:12],
        )
        return {
            "ok": True,
            "surface_id": surface_id,
            "patch_sequence": _PATCH_SEQ.get(f"{actor_id}/{view_id}", 0),
            "components_digest": components_digest,
            "view_id": view_id,
            "phase": phase,
            "emitted_at": datetime.now().isoformat(),
            "deduplicated": True,
        }

    # patch_sequence：同一 surface (actor/view) 内单调递增
    seq_key = f"{actor_id}/{view_id}"
    patch_sequence = _PATCH_SEQ.get(seq_key, 0) + 1

    # OPT-1：记录最后一次成功 emit 的 data_model（final 兜底投影数据源）
    if isinstance(data_model, dict):
        _LAST_DATA_MODEL[seq_key] = dict(data_model)

    # ── 8. 构造 SurfaceState dataclass ──
    surface = SurfaceState(
        surface_id=surface_id,
        view_id=view_id,
        phase=phase,
        components=components,
        data_model=data_model,
        surface_properties=args.get("surface_properties"),
        output_contract=args_contract or view.output_contract,
        source=source,
        emitted_at=datetime.now(),
        patch_sequence=patch_sequence,
    )

    # ── 9. 更新 tracker / dedup / patch seq 索引 ──
    tracker[view_id] = phase
    _SURFACE_DEDUP[content_digest] = f"{actor_id}/{view_id}"
    _PATCH_SEQ[seq_key] = patch_sequence

    # ── 10. 通知 event_store（如可用），由 harness/DagEngine 进一步 emit DagEvent ──
    # 注意：本工具的职责是校验 + 构造 SurfaceState。
    # 真正的 DagEvent (REPORT_SURFACE_STATE) 发射由调用方（DagEngine / local_llm harness）
    # 拿到本工具的返回值后负责 emit（保持工具无副作用）。
    logger.info(
        "report_surface_state 通过校验: actor=%s view=%s phase=%s surface_id=%s patch_seq=%d",
        actor_id,
        view_id,
        phase,
        surface_id[:12],
        patch_sequence,
    )

    return {
        "ok": True,
        "surface_id": surface_id,
        "patch_sequence": patch_sequence,
        "components_digest": components_digest,
        "view_id": view_id,
        "phase": phase,
        "output_contract": surface.output_contract,
        "emitted_at": surface.emitted_at.isoformat(),
        "surface": surface.to_payload(),
    }


def _err(error_code: str, message: str) -> dict[str, Any]:
    """构造统一错误返回格式。"""
    return {
        "ok": False,
        "error": message,
        "error_code": error_code,
    }