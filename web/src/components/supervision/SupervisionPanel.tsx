/**
 * SupervisionPanel — v99.5 P0.2.5/6 业务容器。
 *
 * 职责：
 *   - 接收 report_surface_state SSE 事件，按 surface_id（identity 派生）聚合为 snapshot
 *   - 按 phase 单调推进（started → partial → final → superseded）渲染最新 surface
 *   - 把 surface 渲染为 A2uiWidget（AoGrid/AoTable/AoProgress 等 30+ 组件）
 *
 * 设计要点（详见 docs/reconstruction/agentops-v99.5-a2ui-design.md §3.2.5）：
 *   - 业务容器 ≠ 渲染器：容器只负责状态聚合 + 路由，渲染复用 A2uiWidget/A2uiRenderer
 *   - phase 单调推进在容器层强制（旧 snapshot phase > 新 snapshot phase → 丢弃）
 *   - surface_id 由后端 Worker 身份派生：
 *       · report_surface_state / upsert_generated_view → per (actor, view) 稳定，一张卡演进
 *       · present_content → 每次调用新 surface_id（generation 序号），累积多张卡
 *   - 内容指纹 dedup 已在后端去重（重复 emit 相同内容不会改 snapshot）
 *   - superseded 后仍保留旧 snapshot 显示（让用户看到「已被 v2 取代」淡出样式）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  SurfacePhase,
  SurfaceState,
} from '../../lib/api';

// SupervisionSnapshot 是本组件的核心状态类型，re-export 出去供 SuperAgentPage / 测试使用。
export type SupervisionSnapshot = {
  view_id: string;
  actor_id: string;
  surface_id: string;
  phase: SurfacePhase;
  emitted_at: string;
  surface_state: SurfaceState;
};

import { A2uiWidget } from '../widgets/A2uiWidget';

// ── Phase 推进顺序（与后端 orchestrator/actor_visual_profile.py PHASE_ORDER 对齐）──

const PHASE_ORDER: Record<SurfacePhase, number> = {
  started: 0,
  partial: 1,
  final: 2,
  superseded: 3,
};

/** 是否允许替换旧 snapshot（new phase 必须 >= old phase，除非 new 是 superseded）。 */
function shouldReplace(oldPhase: SurfacePhase, newPhase: SurfacePhase): boolean {
  if (newPhase === 'superseded') return true;
  return PHASE_ORDER[newPhase] >= PHASE_ORDER[oldPhase];
}

// ── 空态粒子聚散动画（Canvas 2D，零依赖）──
// 约 70 个粒子按呼吸节律在「聚拢成团 ↔ 散开游走」间往复：
// 每个粒子有自己的轨道半径（聚拢目标）与散开半径，目标半径随全局
// 呼吸值插值，弹簧力 + 指数阻尼驱动（欠阻尼，落点带轻微过冲更自然）。
// 聚拢时近邻粒子间浮现连线形成星座网络，中心有呼吸光晕。

const PARTICLE_COUNT = 70;
const FIELD_SIZE = 168;
const BREATH_CYCLE = 7;   // 一次完整聚散呼吸（秒）
const LINE_DIST = 30;     // 近邻连线距离阈值（px）

type Particle = {
  x: number; y: number;   // 位置（CSS 像素）
  vx: number; vy: number; // 速度（px/s）
  size: number;           // 粒子半径（px）
  orbitR: number;         // 聚拢轨道半径（距中心）
  scatterR: number;       // 散开半径
  jit: number;            // 呼吸相位抖动（打散整齐感）
  base: number;           // 基础亮度 0.45~1
};

/** 个体呼吸值（0=完全散开，1=完全聚拢），按各自相位抖动错峰。 */
function breathOf(elapsed: number, jit: number): number {
  return 0.5 - 0.5 * Math.cos((elapsed / BREATH_CYCLE + jit) * Math.PI * 2);
}

/** 粒子聚散画布：Canvas 2D 渲染，不可用时降级为 CSS 脉冲光点。 */
function ParticleField() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(FIELD_SIZE * dpr);
    canvas.height = Math.round(FIELD_SIZE * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) { setFallback(true); return; }
    ctx.scale(dpr, dpr);

    // 发光粒子贴图：离屏预渲染一次，运行时 drawImage（远快于逐粒子 shadowBlur）
    const sprite = document.createElement('canvas');
    sprite.width = 32;
    sprite.height = 32;
    const sctx = sprite.getContext('2d');
    if (!sctx) { setFallback(true); return; }
    const grad = sctx.createRadialGradient(16, 16, 0, 16, 16, 16);
    grad.addColorStop(0, 'rgba(191, 219, 254, 1)');
    grad.addColorStop(0.3, 'rgba(96, 165, 250, 0.7)');
    grad.addColorStop(1, 'rgba(96, 165, 250, 0)');
    sctx.fillStyle = grad;
    sctx.fillRect(0, 0, 32, 32);

    const cx = FIELD_SIZE / 2;
    const cy = FIELD_SIZE / 2;
    const particles: Particle[] = Array.from({ length: PARTICLE_COUNT }, () => {
      const ang = Math.random() * Math.PI * 2;
      const scatterR = 40 + Math.random() * 36;
      return {
        x: cx + Math.cos(ang) * scatterR,
        y: cy + Math.sin(ang) * scatterR,
        vx: 0,
        vy: 0,
        size: 1.4 + Math.random() * 2.4,
        orbitR: 6 + Math.random() * 28,
        scatterR,
        jit: (Math.random() - 0.5) * 0.08,
        base: 0.45 + Math.random() * 0.55,
      };
    });

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let raf = 0;
    let last = performance.now();
    let elapsed = 0;

    const frame = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      elapsed += dt;

      // ── 物理更新：目标半径呼吸 + 弹簧 + 阻尼 + 游走/旋涡 ──
      for (const p of particles) {
        const g = breathOf(elapsed, p.jit);
        const dx = cx - p.x;
        const dy = cy - p.y;
        const dist = Math.hypot(dx, dy) || 1;

        // 目标半径随呼吸插值：聚拢→各自轨道环，散开→各自外圈
        const targetR = p.orbitR + (p.scatterR - p.orbitR) * (1 - g);
        const err = dist - targetR;

        // 径向弹簧（沿指向中心方向，err>0 拉回、err<0 推出）
        const spring = err * 3.0;
        p.vx += (dx / dist) * spring * dt;
        p.vy += (dy / dist) * spring * dt;

        // 散开期随机游走 + 聚拢期缓慢旋涡（整体微转更有生命感）
        const wander = (1 - g) * 220;
        p.vx += (Math.random() - 0.5) * wander * dt;
        p.vy += (Math.random() - 0.5) * wander * dt;
        const swirl = g * 16;
        p.vx += (-dy / dist) * swirl * dt;
        p.vy += (dx / dist) * swirl * dt;

        // 指数阻尼（帧率无关）
        const damp = Math.exp(-2.2 * dt);
        p.vx *= damp;
        p.vy *= damp;
        p.x += p.vx * dt;
        p.y += p.vy * dt;

        // 边界回拉：不跑出画布
        if (dist > 80) {
          p.vx += (dx / dist) * 60 * dt;
          p.vy += (dy / dist) * 60 * dt;
        }
      }
      const gather = breathOf(elapsed, 0);

      // ── 绘制 ──
      ctx.clearRect(0, 0, FIELD_SIZE, FIELD_SIZE);

      // 星座连线：聚拢时密度与亮度同步上升
      const lineAlpha = 0.06 + 0.34 * gather;
      ctx.lineWidth = 0.6;
      for (let i = 0; i < particles.length; i++) {
        const a = particles[i];
        for (let j = i + 1; j < particles.length; j++) {
          const b = particles[j];
          const ddx = a.x - b.x;
          const ddy = a.y - b.y;
          const d2 = ddx * ddx + ddy * ddy;
          if (d2 < LINE_DIST * LINE_DIST) {
            const alpha = (1 - Math.sqrt(d2) / LINE_DIST) * lineAlpha;
            ctx.strokeStyle = `rgba(96, 165, 250, ${alpha.toFixed(3)})`;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
          }
        }
      }

      // 中心呼吸光晕
      const glowR = 12 + 20 * gather;
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, glowR);
      glow.addColorStop(0, `rgba(96, 165, 250, ${(0.10 + 0.16 * gather).toFixed(3)})`);
      glow.addColorStop(1, 'rgba(96, 165, 250, 0)');
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, glowR, 0, Math.PI * 2);
      ctx.fill();

      // 粒子（预渲染贴图 + 个体亮度）
      for (const p of particles) {
        const s = p.size * 2.6;
        ctx.globalAlpha = p.base * (0.6 + 0.4 * gather);
        ctx.drawImage(sprite, p.x - s, p.y - s, s * 2, s * 2);
      }
      ctx.globalAlpha = 1;

      if (!reduced) raf = requestAnimationFrame(frame);
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);

  if (fallback) {
    return (
      <div className="supervision-particle-fallback" aria-hidden="true">
        <span className="supervision-particle-fallback-core" />
      </div>
    );
  }
  return <canvas ref={canvasRef} className="supervision-particle-canvas" aria-hidden="true" />;
}

// ── 聚合 reducer ────────────────────────────────────────────────

/** reducer：接受旧 map + 新 event，返回新 map（不可变更新）。

P0.10 新增 actorProfiles 参数（可选）：传入后 reducer 会校验
(actor_id, view_id) 是否在白名单内，未授权的 snapshot 直接丢弃。
校验失败时返回 ``{ __dropped: true, reason }`` 标记供上层 warn / 埋点。

白名单语义与后端 orchestrator/surface_aggregator.py SurfaceAggregator 完全对齐：
  - actor_id 不在 profiles → dropped_reason='unknown_actor'
  - view_id 不在该 actor 的 allowed_surface_views → dropped_reason='view_id_not_in_whitelist'
  - phase 回退（非 superseded）→ dropped_reason='phase_not_monotonic'
*/
export type WhitelistedProfiles = Record<
  string,
  { allowed_surface_views: Record<string, unknown> }
>;

export type ApplyResult =
  | { byKey: Record<string, SupervisionSnapshot>; accepted: true }
  | { byKey: Record<string, SupervisionSnapshot>; accepted: false; dropped_reason: string };

export function applySurfaceStateEvent(
  byKey: Record<string, SupervisionSnapshot>,
  event: { actor_id?: string; surface_state: SurfaceState },
  actorProfiles?: WhitelistedProfiles,
): Record<string, SupervisionSnapshot> | { __dropped: true; reason: string } {
  const { actor_id, surface_state } = event;
  if (!surface_state || !surface_state.view_id || !surface_state.phase) return byKey;
  const actor = actor_id || surface_state.surface_properties?.agentDisplayName || 'unknown';

  // ── 白名单校验（P0.10，可选） ──
  if (actorProfiles) {
    const profile = actorProfiles[actor];
    if (!profile) {
      return { __dropped: true, reason: 'unknown_actor' };
    }
    if (!profile.allowed_surface_views[surface_state.view_id]) {
      return { __dropped: true, reason: 'view_id_not_in_whitelist' };
    }
  }

  // ── 聚合 key：优先 surface_id（identity 派生）──
  // - report_surface_state / upsert_generated_view：surface_id 稳定 per (actor, view)
  //   → 一张卡演进（与旧 actor::view keying 等价）
  // - present_content：每次调用新 surface_id（generation 序号派生）
  //   → 累积多张卡（修复「多次调用合并为一张卡」）
  // - legacy 事件（无 surface_id）：回退 actor::view
  const key = surface_state.surface_id || `${actor}::${surface_state.view_id}`;
  const old = byKey[key];
  if (old && !shouldReplace(old.phase, surface_state.phase)) {
    // phase 回退：丢弃（与后端 actor_visual_profile.validate_phase_monotonic 一致）
    return byKey;
  }
  return {
    ...byKey,
    [key]: {
      view_id: surface_state.view_id,
      actor_id: actor,
      surface_id: surface_state.surface_id,
      phase: surface_state.phase,
      emitted_at: surface_state.emitted_at || new Date().toISOString(),
      surface_state,
    },
  };
}

// ── 容器组件 ───────────────────────────────────────────────────

export interface SupervisionPanelProps {
  /** 当前 (actor_id, view_id) → snapshot 映射（来自父组件 SSE 状态）。 */
  snapshots: Record<string, SupervisionSnapshot>;
  /** 用户点击 surface action 回调（Button 触发）。 */
  onSurfaceAction?: (view_id: string, action_name: string) => void;
  /** 空状态文案。 */
  emptyText?: string;
  /** 是否渲染面板内部 summary 头（统计行）。默认 true；外层标题行已承载统计时传 false。 */
  showSummary?: boolean;
  /** P0.10：可选 view_id 白名单（actor_id → { allowed_surface_views }）。传入后未授权的 snapshot 会被 reducer 丢弃。 */
  actorProfiles?: WhitelistedProfiles;
}

/** 把 SurfaceState 转换为 A2uiWidget props.surface（A2uiSurfaceV1）。 */
function surfaceStateToWidgetProps(snapshot: SupervisionSnapshot): {
  surface: unknown;
  content: Record<string, unknown>;
  title: string;
} {
  // A2uiSurfaceV1 期望 { version, catalogId, components, surfaceProperties? }
  // 后端 SurfaceState.components 已经是 component dict 数组（A2uiComponentV1 shape）
  const surface = {
    version: 'v1.0' as const,
    catalogId:
      snapshot.surface_state.catalog_id ||
      'https://agentops.dev/a2ui/catalogs/core/v1',
    components: snapshot.surface_state.components,
    ...(snapshot.surface_state.surface_properties
      ? { surfaceProperties: snapshot.surface_state.surface_properties }
      : {}),
  };
  return {
    surface,
    content: snapshot.surface_state.data_model,
    title: `${snapshot.actor_id} · ${snapshot.view_id}`,
  };
}

/** 单个 snapshot 卡片：phase badge + A2uiWidget 渲染。 */
function SnapshotCard({
  snapshot,
  onAction,
}: {
  snapshot: SupervisionSnapshot;
  onAction?: (view_id: string, action_name: string) => void;
}) {
  const widgetProps = useMemo(() => surfaceStateToWidgetProps(snapshot), [snapshot]);
  const handleAction = useCallback(
    (actionName: string) => onAction?.(snapshot.view_id, actionName),
    [onAction, snapshot.view_id],
  );

  // phase → tone 映射（与后端 VALID_TONES 对齐）
  const phaseTone = ((): string => {
    switch (snapshot.phase) {
      case 'started':
        return 'info';
      case 'partial':
        return 'warning';
      case 'final':
        return 'positive';
      case 'superseded':
        return 'neutral';
      default:
        return 'neutral';
    }
  })();

  // OPT-1: source 徽标（agent=LLM 主动推送 / system=系统投影骨架）
  const source = snapshot.surface_state.source || 'agent';
  const isSystem = source === 'system';

  return (
    <article
      className="supervision-card"
      data-phase={snapshot.phase}
      data-view={snapshot.view_id}
      data-actor={snapshot.actor_id}
      data-source={source}
    >
      <header className="supervision-card-header">
        <div className="supervision-card-meta">
          <span className="supervision-card-actor">{snapshot.actor_id}</span>
          <span className="supervision-card-sep">·</span>
          <span className="supervision-card-view">{snapshot.view_id}</span>
          <span
            className="supervision-card-source-badge"
            data-source={source}
            title={
              isSystem
                ? '系统投影骨架（DAG 事件确定性生成，等待 agent 推送业务数据）'
                : 'agent 主动推送（report_surface_state 工具）'
            }
          >
            {isSystem ? 'system' : 'agent'}
          </span>
        </div>
        <div className="supervision-card-phase-row">
          <span className="supervision-card-phase-badge" data-tone={phaseTone}>
            {snapshot.phase}
          </span>
          <span className="supervision-card-phase-time">
            {new Date(snapshot.emitted_at).toLocaleTimeString()}
          </span>
          <span
            className="supervision-card-surface-id"
            title={snapshot.surface_id}
          >
            #{snapshot.surface_id.slice(0, 8)}
          </span>
          {snapshot.surface_state.patch_sequence ? (
            <span
              className="supervision-card-surface-id"
              title={`patch #${snapshot.surface_state.patch_sequence}（同 surface 第 ${snapshot.surface_state.patch_sequence} 次更新）`}
            >
              patch #{snapshot.surface_state.patch_sequence}
            </span>
          ) : null}
        </div>
      </header>
      <div className="supervision-card-body">
        <A2uiWidget
          surface={widgetProps.surface as never}
          content={widgetProps.content}
          node_id={`supervision_${snapshot.surface_id}`}
          title={widgetProps.title}
          onAction={handleAction}
        />
      </div>
    </article>
  );
}

export function SupervisionPanel({
  snapshots,
  onSurfaceAction,
  emptyText = '等待 agent emit surface（report_surface_state）…',
  actorProfiles,
  showSummary = true,
}: SupervisionPanelProps) {
  const ordered = useMemo(() => {
    return Object.values(snapshots).sort((a, b) => {
      // 最新优先（按 emitted_at 倒序）
      return new Date(b.emitted_at).getTime() - new Date(a.emitted_at).getTime();
    });
  }, [snapshots]);

  if (ordered.length === 0) {
    return (
      <section className="supervision-panel" data-empty="true">
        {showSummary && (
          <header className="supervision-panel-summary">
            <span className="supervision-panel-count">0 个 active surface</span>
            <span className="supervision-panel-waiting">
              <span className="supervision-panel-waiting-bar" />
              {emptyText}
            </span>
          </header>
        )}
        <div className="supervision-panel-empty">
          {/* 中央粒子聚散：粒子呼吸聚散 + 星座连线（Canvas 2D 渲染） */}
          <ParticleField />
          <p className="supervision-panel-empty-text">
            发起工作流运行后，各 actor 通过
            {' '}<code>report_surface_state</code>{' '}
            推送的实时进度卡会在此铺开
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="supervision-panel" data-count={ordered.length}>
      {showSummary && (
        <header className="supervision-panel-summary">
          <span>
            {ordered.length} 个 active surface
            {ordered.filter(s => s.phase === 'final').length > 0 &&
              ` · ${ordered.filter(s => s.phase === 'final').length} final`}
            {ordered.filter(s => s.phase === 'partial').length > 0 &&
              ` · ${ordered.filter(s => s.phase === 'partial').length} partial`}
          </span>
        </header>
      )}
      <div className="supervision-panel-list">
        {ordered.map(snapshot => (
          <SnapshotCard
            key={snapshot.surface_id || `${snapshot.actor_id}::${snapshot.view_id}`}
            snapshot={snapshot}
            onAction={onSurfaceAction}
          />
        ))}
      </div>
    </section>
  );
}

export default SupervisionPanel;