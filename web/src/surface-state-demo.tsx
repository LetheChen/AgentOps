/**
 * surface-state-demo — v99.5 P0.2.5/6 + P0.12 浏览器验收页面。
 *
 * 访问：http://localhost:5173/surface-state-demo.html
 *
 * 验收目标（CLAUDE.md 规则：可视化改造必须实际打开浏览器确认效果）：
 *   1. SupervisionPanel 正确按 (actor_id, view_id) 聚合 report_surface_state 事件
 *   2. phase 单调推进（started → partial → final → superseded）正确触发 re-render
 *   3. AoGrid + AoMetric 渲染（3 列网格，6 个 metric）
 *   4. AoTable 渲染（数据表 source path 引用）
 *   5. AoProgress 渲染（带 tone 染色）
 *   6. AoStatusBadge 渲染（带 tone 染色）
 *   7. 旧 snapshot 收到 superseded 后整卡淡出（data-phase='superseded'）
 *   8. surface_id digest 重复推送被 dedup（同 digest 不再追加）
 *   9. phase 回退（partial → started）被丢弃
 *  10. v99.5 P0.10/P0.12：view_id 白名单（actor 未知 / view 未授权被 reducer 丢弃）
 *  11. v99.5 P0.12：weekly_reporter 三 view_id（collect-live/grade-live/archive-live）
 *      独立推进 AoProgress 0-33 / 33-66 / 66-100
 *
 * 此页面不连后端，直接调用 SupervisionPanel + applySurfaceStateEvent reducer，
 * 验证前端 reducer + UI 渲染链路。后端 report_surface_state 工具在 tests/ 已覆盖。
 */
import { StrictMode, useCallback, useMemo, useState } from 'react';
import ReactDOM from 'react-dom/client';
import {
  SupervisionPanel,
  applySurfaceStateEvent,
  type SupervisionSnapshot,
  type WhitelistedProfiles,
} from './components/supervision/SupervisionPanel';
import type { SurfaceState } from './lib/api';
import './styles.css';
import './styles/a2ui.css';

// ── SurfaceState factory ────────────────────────────────────────────────

/** 模拟后端 compute_surface_id：sha256(view_id + phase + canonical_json(data_model))。 */
async function computeSurfaceId(
  viewId: string,
  phase: string,
  dataModel: Record<string, unknown>,
): Promise<string> {
  const canonical = JSON.stringify(dataModel, Object.keys(dataModel).sort());
  const raw = `${viewId}|${phase}|${canonical}`;
  const buf = new TextEncoder().encode(raw);
  const hash = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('');
}

interface MakeArgs {
  actor_id: string;
  view_id: string;
  phase: 'started' | 'partial' | 'final' | 'superseded';
  components: Array<Record<string, unknown>>;
  data_model: Record<string, unknown>;
  output_contract?: string;
}

async function makeSurfaceState(args: MakeArgs): Promise<SurfaceState> {
  const surface_id = await computeSurfaceId(args.view_id, args.phase, args.data_model);
  return {
    surface_id,
    view_id: args.view_id,
    phase: args.phase,
    components: args.components,
    data_model: args.data_model,
    catalog_id: 'https://agentops.dev/a2ui/catalogs/core/v1',
    surface_properties: { agentDisplayName: args.actor_id },
    output_contract: args.output_contract || 'ActorReport',
    emitted_at: new Date().toISOString(),
  };
}

// ── 场景：AoGrid + AoMetric 调研实时面板 ──────────────────────────────────

const RESEARCH_GRID_COMPONENTS: Array<Record<string, unknown>> = [
  { id: 'root', component: 'AoGrid', columns: { default: 3, compact: 1 }, gap: 'md', children: ['t1', 'm1', 'm2', 'm3', 'm4', 'm5', 'm6'] },
  { id: 't1', component: 'AoSection', title: '调研进度', children: ['m1', 'm2', 'm3', 'm4', 'm5', 'm6'] },
  { id: 'm1', component: 'AoMetric', label: { path: '/title' }, value: { path: '/progress' }, unit: '%', tone: 'info' },
  { id: 'm2', component: 'AoMetric', label: '已核实', value: { path: '/verified_count' }, tone: 'positive' },
  { id: 'm3', component: 'AoMetric', label: '信源数', value: { path: '/source_count' } },
  { id: 'm4', component: 'AoMetric', label: '信息缺口', value: { path: '/gap_count' }, tone: 'warning' },
  { id: 'm5', component: 'AoMetric', label: '进度', value: { path: '/progress' }, unit: '%' },
  { id: 'm6', component: 'AoStatusBadge', text: { path: '/status_badge' }, tone: { path: '/primary_tone' } },
];

// ── 场景：AoTable 引用源数据 ──────────────────────────────────────────────

const TABLE_COMPONENTS: Array<Record<string, unknown>> = [
  {
    id: 'root',
    component: 'AoTable',
    source: { path: '/sources' },
    columns: [
      { id: 'name', label: '信源', path: '/name', format: 'text' },
      { id: 'kind', label: '类型', path: '/kind', format: 'text' },
      { id: 'reliability', label: '可信度', path: '/reliability', format: 'percent' },
      { id: 'status', label: '状态', path: '/status', format: 'status' },
    ],
  },
];

// ── 场景：AoProgress 任务进度 ──────────────────────────────────────────────

const PROGRESS_COMPONENTS: Array<Record<string, unknown>> = [
  {
    id: 'root',
    component: 'Column',
    children: ['p1', 'p2', 'p3', 'p4'],
  },
  { id: 'p1', component: 'AoProgress', label: '扫描日志', value: { path: '/scan_pct' }, tone: 'info' },
  { id: 'p2', component: 'AoProgress', label: '分析告警', value: { path: '/analyze_pct' }, tone: 'warning' },
  { id: 'p3', component: 'AoProgress', label: '生成报告', value: { path: '/report_pct' }, tone: 'positive' },
  { id: 'p4', component: 'AoProgress', label: '通知用户', value: { path: '/notify_pct' } },
];

// ── Actor Profile 白名单（v99.5 P0.12 演示用） ───────────────────────────
// 与 config/actors/*/actor_visual_profile.json 完全一致：
//   - research → { research-live }
//   - synthesis → { analysis-live }
//   - auditor → { auditor-live }
//   - weekly_reporter → { collect-live, grade-live, archive-live }
// 真实 SuperAgentPage 通过 GET /api/actors 加载，demo 用静态 inline 避免依赖后端。

const DEMO_ACTOR_PROFILES: WhitelistedProfiles = {
  research: {
    allowed_surface_views: { 'research-live': { view_id: 'research-live' } },
  },
  synthesis: {
    allowed_surface_views: { 'analysis-live': { view_id: 'analysis-live' } },
  },
  auditor: {
    allowed_surface_views: { 'auditor-live': { view_id: 'auditor-live' } },
  },
  weekly_reporter: {
    allowed_surface_views: {
      'collect-live': { view_id: 'collect-live' },
      'grade-live': { view_id: 'grade-live' },
      'archive-live': { view_id: 'archive-live' },
    },
  },
};

// ── 周报 AoProgress 三段组件（P0.12 weekly_reporter 三 view 演示） ─────────

const WEEKLY_PROGRESS_COMPONENTS: Array<Record<string, unknown>> = [
  { id: 'root', component: 'Column', children: ['sec', 'progress'] },
  {
    id: 'sec',
    component: 'AoSection',
    title: { path: '/title' },
    children: ['progress', 'badge'],
  },
  {
    id: 'progress',
    component: 'AoProgress',
    label: { path: '/section' },
    value: { path: '/progress' },
    tone: { path: '/primary_tone' },
  },
  {
    id: 'badge',
    component: 'AoStatusBadge',
    text: { path: '/status_text' },
    tone: { path: '/primary_tone' },
  },
];

// ── 主页面 ───────────────────────────────────────────────────────────────

function App() {
  const [snapshots, setSnapshots] = useState<Record<string, SupervisionSnapshot>>({});
  const [log, setLog] = useState<string[]>([]);
  // P0.12：白名单开关（默认开启，让用户能看到拒绝路径；关闭后是旧行为）
  const [whitelistEnabled, setWhitelistEnabled] = useState(true);

  const emit = useCallback(
    async (
      actor: string,
      viewId: string,
      phase: 'started' | 'partial' | 'final' | 'superseded',
      components: Array<Record<string, unknown>>,
      dataModel: Record<string, unknown>,
    ) => {
      const surface = await makeSurfaceState({
        actor_id: actor,
        view_id: viewId,
        phase,
        components,
        data_model: dataModel,
      });
      setSnapshots((prev) => {
        const result = applySurfaceStateEvent(
          prev,
          { actor_id: actor, surface_state: surface },
          whitelistEnabled ? DEMO_ACTOR_PROFILES : undefined,
        );

        // P0.12：reducer 返回 dropped 标记（白名单未通过）
        if (result && typeof result === 'object' && '__dropped' in result) {
          const dropReason = result.reason;
          setLog((prevLog) => [
            `[${new Date().toLocaleTimeString()}] ${actor}/${viewId} ${phase} → ✗ (whitelist:${dropReason})`,
            ...prevLog,
          ].slice(0, 30));
          return prev;
        }

        const next = result as Record<string, SupervisionSnapshot>;
        const key = `${actor}::${viewId}`;
        const accepted = next[key]?.surface_id === surface.surface_id && next[key]?.phase === phase;
        const wasPresent = prev[key] !== undefined;
        const rejectReason = !accepted
          ? prev[key] && prev[key].phase === phase && prev[key].surface_id === surface.surface_id
            ? 'digest-pinned'
            : prev[key] && ['partial', 'final', 'superseded'].includes(prev[key].phase) && phase === 'started'
              ? 'phase-backward'
              : 'unknown'
          : wasPresent
            ? 'phase-update'
            : 'first-emit';
        setLog((prevLog) => [
          `[${new Date().toLocaleTimeString()}] ${actor}/${viewId} ${phase} → ${accepted ? '✓' : '✗'} (${rejectReason})`,
          ...prevLog,
        ].slice(0, 30));
        return next;
      });
    },
    [whitelistEnabled],
  );

  const reset = useCallback(() => {
    setSnapshots({});
    setLog((prev) => ['reset', ...prev]);
  }, []);

  const stats = useMemo(() => {
    const all = Object.values(snapshots);
    return {
      total: all.length,
      started: all.filter(s => s.phase === 'started').length,
      partial: all.filter(s => s.phase === 'partial').length,
      final: all.filter(s => s.phase === 'final').length,
      superseded: all.filter(s => s.phase === 'superseded').length,
    };
  }, [snapshots]);

  return (
    <div style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '20px', background: 'var(--color-bg-canvas)', minHeight: '100vh' }}>
      <header>
        <h1 style={{ fontSize: '24px', color: 'var(--color-text-primary)', margin: 0 }}>
          Surface State Demo · v99.5 P0.2.5/6 + P0.12
        </h1>
        <p style={{ color: 'var(--color-text-secondary)', marginTop: '6px', fontSize: '13px' }}>
          验收 SupervisionPanel 容器 + AoGrid/AoMetric/AoTable/AoProgress/AoStatusBadge 渲染 +
          phase 单调推进 + digest dedup + view_id 白名单（P0.10/P0.12）+ weekly_reporter 三段
          AoProgress 0-33/33-66/66-100（P0.9）。访问路径：
          <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-primary-soft)', marginLeft: '6px' }}>
            http://localhost:5173/surface-state-demo.html
          </code>
        </p>
        <div style={{ marginTop: '10px', display: 'flex', gap: '14px', fontSize: '12px', color: 'var(--color-text-secondary)' }}>
          <span>total: <strong style={{ color: 'var(--color-text-primary)' }}>{stats.total}</strong></span>
          <span>started: <strong style={{ color: '#93c5fd' }}>{stats.started}</strong></span>
          <span>partial: <strong style={{ color: '#fcd34d' }}>{stats.partial}</strong></span>
          <span>final: <strong style={{ color: '#6ee7b7' }}>{stats.final}</strong></span>
          <span>superseded: <strong style={{ color: '#cbd5e1' }}>{stats.superseded}</strong></span>
        </div>
      </header>

      {/* ── 控制面板 ── */}
      <section style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-subtle)', borderRadius: '8px', padding: '14px' }}>
        <h2 style={{ fontSize: '15px', color: 'var(--color-text-primary)', margin: '0 0 10px' }}>控制面板（模拟 agent emit）</h2>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
          <div>
            <h3 style={{ fontSize: '13px', color: 'var(--color-text-primary)', margin: '0 0 8px' }}>1. research actor → AoGrid + AoMetric</h3>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              <button data-testid="btn-research-started" onClick={() => emit('research', 'research-live', 'started', RESEARCH_GRID_COMPONENTS, { title: '调研启动', progress: 0, verified_count: 0, source_count: 0, gap_count: 5, primary_tone: 'info', status_badge: '开始调研' })} style={btn}>emit started</button>
              <button data-testid="btn-research-partial" onClick={() => emit('research', 'research-live', 'partial', RESEARCH_GRID_COMPONENTS, { title: '调研进行中', progress: 42, verified_count: 7, source_count: 12, gap_count: 3, primary_tone: 'info', status_badge: '已核实 7 条' })} style={btn}>emit partial</button>
              <button data-testid="btn-research-final" onClick={() => emit('research', 'research-live', 'final', RESEARCH_GRID_COMPONENTS, { title: '调研完成', progress: 100, verified_count: 14, source_count: 18, gap_count: 0, primary_tone: 'positive', status_badge: '完成' })} style={btn}>emit final</button>
              <button data-testid="btn-research-revert" onClick={() => emit('research', 'research-live', 'started', RESEARCH_GRID_COMPONENTS, { title: '回退测试', progress: 0, verified_count: 0, source_count: 0, gap_count: 5, primary_tone: 'info', status_badge: '开始调研' })} style={{ ...btn, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>回退 started (应被丢弃)</button>
            </div>
          </div>
          <div>
            <h3 style={{ fontSize: '13px', color: 'var(--color-text-primary)', margin: '0 0 8px' }}>2. synthesis actor → AoTable</h3>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              <button data-testid="btn-synthesis-started" onClick={() => emit('synthesis', 'analysis-live', 'started', TABLE_COMPONENTS, { sources: [] })} style={btn}>emit started (空)</button>
              <button data-testid="btn-synthesis-partial" onClick={() => emit('synthesis', 'analysis-live', 'partial', TABLE_COMPONENTS, {
                sources: [
                  { name: 'kb:weekly-report', kind: '内部', reliability: 0.92, status: 'verified' },
                  { name: 'github:AgentOps', kind: '外部', reliability: 0.78, status: 'pending' },
                  { name: 'kb:design', kind: '内部', reliability: 0.95, status: 'verified' },
                ],
              })} style={btn}>emit partial (3 行)</button>
              <button data-testid="btn-synthesis-final" onClick={() => emit('synthesis', 'analysis-live', 'final', TABLE_COMPONENTS, {
                sources: [
                  { name: 'kb:weekly-report', kind: '内部', reliability: 0.92, status: 'verified' },
                  { name: 'github:AgentOps', kind: '外部', reliability: 0.84, status: 'verified' },
                  { name: 'kb:design', kind: '内部', reliability: 0.95, status: 'verified' },
                  { name: 'rfc:md-v3', kind: '规范', reliability: 0.88, status: 'verified' },
                  { name: 'kb:patterns', kind: '内部', reliability: 0.91, status: 'verified' },
                ],
              })} style={btn}>emit final (5 行)</button>
            </div>
          </div>
          <div>
            <h3 style={{ fontSize: '13px', color: 'var(--color-text-primary)', margin: '0 0 8px' }}>3. execution actor → AoProgress 多步骤</h3>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              <button data-testid="btn-exec-started" onClick={() => emit('execution', 'progress-live', 'started', PROGRESS_COMPONENTS, { scan_pct: 0, analyze_pct: 0, report_pct: 0, notify_pct: 0 })} style={btn}>emit started</button>
              <button data-testid="btn-exec-partial" onClick={() => emit('execution', 'progress-live', 'partial', PROGRESS_COMPONENTS, { scan_pct: 100, analyze_pct: 60, report_pct: 20, notify_pct: 0 })} style={btn}>emit partial</button>
              <button data-testid="btn-exec-superseded" onClick={() => emit('execution', 'progress-live', 'superseded', PROGRESS_COMPONENTS, { scan_pct: 100, analyze_pct: 60, report_pct: 20, notify_pct: 0 })} style={{ ...btn, background: 'var(--color-bg-elevated)' }}>emit superseded</button>
              <button data-testid="btn-exec-v2" onClick={() => emit('execution', 'progress-live-v2', 'started', PROGRESS_COMPONENTS, { scan_pct: 100, analyze_pct: 100, report_pct: 100, notify_pct: 50 })} style={btn}>emit v2 view (started)</button>
            </div>
          </div>
          <div>
            <h3 style={{ fontSize: '13px', color: 'var(--color-text-primary)', margin: '0 0 8px' }}>4. 调试</h3>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              <button data-testid="btn-reset" onClick={reset} style={{ ...btn, background: 'var(--color-bg-elevated)' }}>重置</button>
            </div>
          </div>

          {/* ── P0.12：view_id 白名单场景（5） ── */}
          <div style={{ gridColumn: '1 / -1', borderTop: '1px dashed var(--color-border-subtle)', paddingTop: '14px', marginTop: '4px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
              <h3 style={{ fontSize: '13px', color: 'var(--color-text-primary)', margin: 0 }}>
                5. v99.5 P0.12 — view_id 白名单（actor profile 4 个：research / synthesis / auditor / weekly_reporter）
              </h3>
              <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  data-testid="toggle-whitelist"
                  checked={whitelistEnabled}
                  onChange={(e) => setWhitelistEnabled(e.target.checked)}
                />
                启用白名单（关闭 = 旧行为：所有 snapshot 都通过）
              </label>
            </div>

            {/* weekly_reporter 三段 AoProgress 演示 */}
            <div style={{ marginBottom: '10px' }}>
              <h4 style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '0 0 6px' }}>
                weekly_reporter 流水线（3 view_id 独立聚合 → AoProgress 0-33 / 33-66 / 66-100）
              </h4>
              <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                <button data-testid="btn-weekly-collect" onClick={() => emit('weekly_reporter', 'collect-live', 'partial', WEEKLY_PROGRESS_COMPONENTS, { title: '解析周报合集', section: 'collect-live', progress: 20, status_text: '进行中', primary_tone: 'info' })} style={btn}>
                  collect-live partial (progress=20)
                </button>
                <button data-testid="btn-weekly-collect-final" onClick={() => emit('weekly_reporter', 'collect-live', 'final', WEEKLY_PROGRESS_COMPONENTS, { title: '解析完成', section: 'collect-live', progress: 33, status_text: '完成', primary_tone: 'positive' })} style={btn}>
                  collect-live final (progress=33)
                </button>
                <button data-testid="btn-weekly-grade" onClick={() => emit('weekly_reporter', 'grade-live', 'partial', WEEKLY_PROGRESS_COMPONENTS, { title: '分级汇总', section: 'grade-live', progress: 50, status_text: '分级中', primary_tone: 'info' })} style={btn}>
                  grade-live partial (progress=50)
                </button>
                <button data-testid="btn-weekly-grade-final" onClick={() => emit('weekly_reporter', 'grade-live', 'final', WEEKLY_PROGRESS_COMPONENTS, { title: '分级完成', section: 'grade-live', progress: 66, status_text: '完成', primary_tone: 'positive' })} style={btn}>
                  grade-live final (progress=66)
                </button>
                <button data-testid="btn-weekly-archive" onClick={() => emit('weekly_reporter', 'archive-live', 'partial', WEEKLY_PROGRESS_COMPONENTS, { title: '归档中', section: 'archive-live', progress: 85, status_text: '写入中', primary_tone: 'info' })} style={btn}>
                  archive-live partial (progress=85)
                </button>
                <button data-testid="btn-weekly-archive-final" onClick={() => emit('weekly_reporter', 'archive-live', 'final', WEEKLY_PROGRESS_COMPONENTS, { title: '归档完成', section: 'archive-live', progress: 100, status_text: '完成', primary_tone: 'positive' })} style={btn}>
                  archive-live final (progress=100)
                </button>
              </div>
            </div>

            {/* 白名单拒绝路径演示 */}
            <div>
              <h4 style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '0 0 6px' }}>
                白名单拒绝路径（reducer 返回 dropped 标记，不进入 snapshot map）
              </h4>
              <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                <button
                  data-testid="btn-whitelist-valid"
                  onClick={() => emit('research', 'research-live', 'started', RESEARCH_GRID_COMPONENTS, { title: '调研启动', progress: 0, verified_count: 0, primary_tone: 'info', status_badge: '开始' })}
                  style={btn}
                  title="已知 actor + 白名单 view → 应接受"
                >
                  ✓ valid: research → research-live (应接受)
                </button>
                <button
                  data-testid="btn-whitelist-unknown-actor"
                  onClick={() => emit('rogue_actor', 'rogue-live', 'started', RESEARCH_GRID_COMPONENTS, { title: 'rogue', progress: 0, verified_count: 0, primary_tone: 'info', status_badge: 'rogue' })}
                  style={{ ...btn, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}
                  title="actor_id 不在 profiles → unknown_actor"
                >
                  ✗ unknown actor: rogue_actor → rogue-live
                </button>
                <button
                  data-testid="btn-whitelist-unauthorized-view"
                  onClick={() => emit('research', 'synthesis-live', 'started', RESEARCH_GRID_COMPONENTS, { title: '串用', progress: 0, verified_count: 0, primary_tone: 'info', status_badge: 'cross' })}
                  style={{ ...btn, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}
                  title="research 没声明 synthesis-live → view_id_not_in_whitelist"
                >
                  ✗ unauthorized view: research → synthesis-live
                </button>
                <button
                  data-testid="btn-whitelist-wrong-actor-weekly"
                  onClick={() => emit('research', 'collect-live', 'started', WEEKLY_PROGRESS_COMPONENTS, { title: 'wrong actor', section: 'collect-live', progress: 0, status_text: 'wrong', primary_tone: 'info' })}
                  style={{ ...btn, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}
                  title="research 不能 emit weekly_reporter 的 collect-live → view_id_not_in_whitelist"
                >
                  ✗ wrong actor: research → collect-live (weekly_reporter 才允许)
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── 主面板 ── */}
      <SupervisionPanel snapshots={snapshots} onSurfaceAction={(view_id, action_name) => setLog((prev) => [`[${new Date().toLocaleTimeString()}] action: ${view_id} → ${action_name}`, ...prev].slice(0, 30))} />

      {/* ── 事件日志 ── */}
      <section style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-subtle)', borderRadius: '8px', padding: '14px' }}>
        <h2 style={{ fontSize: '15px', color: 'var(--color-text-primary)', margin: '0 0 10px' }}>
          事件日志（最近 30 条）
        </h2>
        <pre data-testid="event-log" style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--color-text-secondary)', margin: 0, maxHeight: '200px', overflow: 'auto', whiteSpace: 'pre-wrap' }}>
          {log.length === 0 ? '（暂无事件，点上方按钮模拟 emit）' : log.join('\n')}
        </pre>
      </section>
    </div>
  );
}

const btn: React.CSSProperties = {
  padding: '5px 10px',
  background: 'var(--color-primary)',
  color: '#fff',
  border: 'none',
  borderRadius: '5px',
  fontSize: '11px',
  cursor: 'pointer',
  fontWeight: 500,
};

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);