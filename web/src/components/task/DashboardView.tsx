// web/src/components/task/DashboardView.tsx
// V3.1 仪表盘视图（参考 Taskboard 布局重构）
// - 完成度横幅：大号百分比 + X 已完成 · Y 尚未结束 + 进度条
// - 项目总结：问候语 + 数据摘要
// - 状态卡片行：进行中 / 等你确认 / 遇到阻碍 / 已关闭 / 待立项（数量 + 占比）
// - 项目分析：风险分布 / 今日动态 / 需要关注 / 角色贡献 / 贡献热力图 / 累计进度
// 点击条目 → 直达任务详情页

import { useState, useEffect, useCallback, useMemo } from 'react';
import { taskApi, type Task, type TaskDashboard } from '../../api/taskApi';

const RISK_COLORS: Record<string, string> = { high: '#e53935', medium: '#fb8c00', low: '#43a047' };

const CLOSED_SET = ['closed'];
const DONE_SET = ['closed'];
const STARTED_SET = ['in_progress', 'blocked', 'validating', 'reviewing', 'closing', 'discussing', 'decomposing', 'backlog'];
// v1.2 仪表盘分组与主线对齐：
// 待立项（idea）→ 处理中（discussing/decomposing/in_progress，agent 推进中）
// → 等你确认（reviewing 评审放行 + validating 验收 + closing，硬门禁）
// → 待执行（backlog 可执行任务池，dispatcher 自动派发）
const ACTIVE_SET = ['discussing', 'decomposing', 'in_progress'];      // 处理中
const CONFIRM_SET = ['reviewing', 'validating', 'closing'];           // 等你确认
const READY_SET = ['backlog'];                                        // 待执行池
const BACKLOG_SET = ['idea'];                                         // 待立项

// ---- 工具 ----
function dayKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function panelStyle(): React.CSSProperties {
  return {
    background: 'var(--color-bg-surface)',
    border: '1px solid var(--color-border-subtle)',
    borderRadius: 'var(--radius-md)',
    padding: 16,
  };
}

function sectionTitleStyle(): React.CSSProperties {
  return {
    fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)',
    letterSpacing: '0.05em', marginBottom: 12,
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
  };
}

// ---- 状态卡片 ----
interface StatusCardDef {
  key: string;
  label: string;
  color: string;
  icon: string;
  match: (s: string) => boolean;
}

const STATUS_CARDS: StatusCardDef[] = [
  { key: 'active', label: '处理中', color: '#60A5FA', icon: '◐', match: (s) => ACTIVE_SET.includes(s) },
  { key: 'ready', label: '待执行', color: '#94A3B8', icon: '▷', match: (s) => READY_SET.includes(s) },
  { key: 'confirm', label: '等你确认', color: '#F59E0B', icon: '◎', match: (s) => CONFIRM_SET.includes(s) },
  { key: 'blocked', label: '遇到阻碍', color: '#EF4444', icon: '⊘', match: (s) => s === 'blocked' },
  { key: 'closed', label: '已关闭', color: '#10B981', icon: '●', match: (s) => CLOSED_SET.includes(s) },
  { key: 'backlog', label: '待立项', color: '#6B7280', icon: '○', match: (s) => BACKLOG_SET.includes(s) },
];

// ---- 贡献热力图（近 26 周） ----
function ContributionHeatmap({ tasks }: { tasks: Task[] }) {
  const weeks = 26;
  const cell = 11;
  const gap = 3;
  // 每日计数：创建 +1、关闭 +1
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    tasks.forEach((t) => {
      if (t.created_at) {
        const k = dayKey(new Date(t.created_at));
        m.set(k, (m.get(k) || 0) + 1);
      }
      if (t.closed_at) {
        const k = dayKey(new Date(t.closed_at));
        m.set(k, (m.get(k) || 0) + 1);
      }
    });
    return m;
  }, [tasks]);

  // 从本周周一开始往前推 25 周
  const today = new Date();
  const dow = (today.getDay() + 6) % 7; // 周一=0
  const start = new Date(today);
  start.setDate(today.getDate() - dow - (weeks - 1) * 7);

  const levelColor = (n: number): string => {
    if (n <= 0) return 'var(--color-bg-elevated)';
    if (n <= 1) return 'rgba(59,130,246,0.25)';
    if (n <= 2) return 'rgba(59,130,246,0.45)';
    if (n <= 4) return 'rgba(59,130,246,0.65)';
    if (n <= 6) return 'rgba(59,130,246,0.85)';
    return '#3B82F6';
  };

  const cells: React.ReactNode[] = [];
  for (let w = 0; w < weeks; w++) {
    for (let d = 0; d < 7; d++) {
      const date = new Date(start);
      date.setDate(start.getDate() + w * 7 + d);
      const after = date > today;
      const n = counts.get(dayKey(date)) || 0;
      cells.push(
        <rect
          key={`${w}-${d}`}
          x={w * (cell + gap)}
          y={d * (cell + gap)}
          width={cell}
          height={cell}
          rx={2}
          fill={after ? 'transparent' : levelColor(n)}
          stroke={after ? 'none' : 'var(--color-border-subtle)'}
          strokeWidth={0.5}
        >
          <title>{`${dayKey(date)}：${n} 次任务动态`}</title>
        </rect>
      );
    }
  }

  const monthLabels: React.ReactNode[] = [];
  let lastMonth = -1;
  for (let w = 0; w < weeks; w++) {
    const date = new Date(start);
    date.setDate(start.getDate() + w * 7);
    if (date.getMonth() !== lastMonth) {
      lastMonth = date.getMonth();
      monthLabels.push(
        <text
          key={`m-${w}`}
          x={w * (cell + gap)}
          y={-6}
          fontSize={9}
          fill="var(--color-text-tertiary)"
        >
          {`${lastMonth + 1}月`}
        </text>
      );
    }
  }

  return (
    <div>
      <svg
        width={weeks * (cell + gap)}
        height={7 * (cell + gap) + 14}
        style={{ display: 'block', overflow: 'visible' }}
      >
        <g transform="translate(0,14)">{cells}</g>
        {monthLabels}
      </svg>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginTop: 8, fontSize: 10, color: 'var(--color-text-tertiary)' }}>
        <span>少</span>
        {[0, 1, 2, 3, 4, 5].map((l) => (
          <span
            key={l}
            style={{
              width: 10, height: 10, borderRadius: 2,
              background: l === 0 ? 'var(--color-bg-elevated)' : `rgba(59,130,246,${0.25 + l * 0.15})`,
              border: '1px solid var(--color-border-subtle)',
            }}
          />
        ))}
        <span>多</span>
      </div>
    </div>
  );
}

// ---- 累计进度图（范围 / 已开始 / 已完成） ----
function ScopeChart({ tasks }: { tasks: Task[] }) {
  const data = useMemo(() => {
    if (tasks.length === 0) return null;
    const created: string[] = [];
    const closed: string[] = [];
    tasks.forEach((t) => {
      if (t.created_at) created.push(t.created_at);
      if (t.closed_at) closed.push(t.closed_at);
    });
    if (created.length === 0) return null;
    const first = new Date(Math.min(...created.map((s) => new Date(s).getTime())));
    first.setHours(0, 0, 0, 0);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days: Array<{ date: string; scope: number; started: number; done: number }> = [];
    let scope = 0;
    let started = 0;
    let done = 0;
    // 已开始 = 状态推进过或已创建即视作范围内；直接按当前状态口径统计累计
    const createdPerDay = new Map<string, number>();
    const closedPerDay = new Map<string, number>();
    const startedPerDay = new Map<string, number>();
    tasks.forEach((t) => {
      const ck = dayKey(new Date(t.created_at));
      createdPerDay.set(ck, (createdPerDay.get(ck) || 0) + 1);
      if (STARTED_SET.includes(t.status) || DONE_SET.includes(t.status)) {
        // 用 updated_at 近似开始时间（无专门 started_at 字段）
        const sk = dayKey(new Date(t.updated_at || t.created_at));
        startedPerDay.set(sk, (startedPerDay.get(sk) || 0) + 1);
      }
      if (t.closed_at) {
        const dk = dayKey(new Date(t.closed_at));
        closedPerDay.set(dk, (closedPerDay.get(dk) || 0) + 1);
      }
    });
    for (let d = new Date(first); d <= today; d.setDate(d.getDate() + 1)) {
      const k = dayKey(d);
      scope += createdPerDay.get(k) || 0;
      started += startedPerDay.get(k) || 0;
      done += closedPerDay.get(k) || 0;
      if (started > scope) started = scope;
      if (done > started) done = started;
      days.push({ date: k, scope, started, done });
    }
    return days;
  }, [tasks]);

  if (!data || data.length === 0) {
    return <div style={{ color: 'var(--color-text-tertiary)', fontSize: 12 }}>暂无进度数据</div>;
  }

  const W = 640;
  const H = 110;
  const n = data.length;
  const barW = Math.max(W / n - 1, 1.5);
  const maxScope = Math.max(...data.map((d) => d.scope), 1);

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg width={W} height={H} style={{ display: 'block' }}>
        {data.map((d, i) => {
          const x = (i * W) / n;
          const hScope = (d.scope / maxScope) * (H - 8);
          const hDone = (d.done / maxScope) * (H - 8);
          const hStarted = ((d.started - d.done) / maxScope) * (H - 8);
          return (
            <g key={d.date}>
              <rect x={x} y={H - hScope} width={barW} height={hScope} fill="#334155" rx={1}>
                <title>{`${d.date}：范围 ${d.scope}，已开始 ${d.started}，已完成 ${d.done}`}</title>
              </rect>
              <rect x={x} y={H - hDone - hStarted} width={barW} height={hStarted} fill="#F59E0B" rx={1}>
                <title>{`${d.date}：范围 ${d.scope}，已开始 ${d.started}，已完成 ${d.done}`}</title>
              </rect>
              <rect x={x} y={H - hDone} width={barW} height={hDone} fill="#10B981" rx={1}>
                <title>{`${d.date}：范围 ${d.scope}，已开始 ${d.started}，已完成 ${d.done}`}</title>
              </rect>
            </g>
          );
        })}
      </svg>
      <div style={{ display: 'flex', gap: 12, marginTop: 6, fontSize: 10, color: 'var(--color-text-tertiary)' }}>
        <span>■ <span style={{ color: '#334155' }}>范围 {data[data.length - 1].scope}</span></span>
        <span>■ <span style={{ color: '#F59E0B' }}>已开始 {data[data.length - 1].started}</span></span>
        <span>■ <span style={{ color: '#10B981' }}>已完成 {data[data.length - 1].done}</span></span>
      </div>
    </div>
  );
}

// ---- 角色贡献 ----
function ContributorStats({ tasks }: { tasks: Task[] }) {
  const rows = useMemo(() => {
    const m = new Map<string, { total: number; done: number }>();
    tasks.forEach((t) => {
      const name = t.assignee_name || t.creator_type || '未分配';
      const cur = m.get(name) || { total: 0, done: 0 };
      cur.total += 1;
      if (DONE_SET.includes(t.status)) cur.done += 1;
      m.set(name, cur);
    });
    return Array.from(m.entries())
      .map(([name, v]) => ({ name, ...v, pct: v.total > 0 ? Math.round((v.done / v.total) * 100) : 0 }))
      .sort((a, b) => b.total - a.total)
      .slice(0, 6);
  }, [tasks]);

  if (rows.length === 0) {
    return <div style={{ color: 'var(--color-text-tertiary)', fontSize: 12 }}>暂无数据</div>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {rows.map((r) => (
        <div key={r.name} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{
            width: 26, height: 26, borderRadius: '50%', flexShrink: 0,
            background: 'var(--color-primary-tint)', color: 'var(--color-primary-soft)',
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 12, fontWeight: 600,
          }}>
            {r.name.slice(0, 1)}
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 3 }}>
              <span style={{ color: 'var(--color-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.name}
              </span>
              <span style={{ color: 'var(--color-text-tertiary)' }}>
                {r.done} 个已完成 · {r.pct}%
              </span>
            </div>
            <div style={{ height: 5, borderRadius: 3, background: 'var(--color-bg-elevated)' }}>
              <div style={{
                height: 5, borderRadius: 3, background: '#10B981',
                width: `${Math.max(r.pct, 2)}%`, transition: 'width .3s ease',
              }} />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ---- 主组件 ----
export default function DashboardView({
  projectId,
  tasks = [],
  onOpenTask,
}: {
  projectId: string;
  tasks?: Task[];
  onOpenTask: (taskId: string) => void;
}) {
  const [data, setData] = useState<TaskDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!projectId) return;
    try {
      const d = await taskApi.dashboard(projectId);
      setData(d);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载仪表盘失败');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    load();
    const timer = setInterval(load, 30000);
    return () => clearInterval(timer);
  }, [load]);

  if (loading && !data) {
    return <div style={{ padding: 24, color: 'var(--color-text-tertiary)' }}>仪表盘加载中...</div>;
  }
  if (error && !data) {
    return <div style={{ padding: 24, color: '#fca5a5' }}>{error}</div>;
  }
  if (!data) return null;

  // 完成度口径：closed / total；尚未结束 = 未关闭且未取消/废弃
  // tasks 非空时统一以任务列表为准（dashboard 聚合 30s 刷新，避免与列表不同步出现 >100% 占比）
  const useList = tasks.length > 0;
  const totalTasks = useList ? tasks.length : data.total;
  const closed = useList
    ? tasks.filter((t) => CLOSED_SET.includes(t.status)).length
    : (data.status_distribution.closed || 0);
  const canceled = useList
    ? tasks.filter((t) => t.status === 'canceled' || t.status === 'abandoned').length
    : (data.status_distribution.canceled || 0) + (data.status_distribution.abandoned || 0);
  const open = totalTasks - closed - canceled;
  const pct = totalTasks > 0 ? Math.round((closed / totalTasks) * 100) : 0;

  // 状态卡片计数（以任务列表为准，回退 dashboard 聚合）
  const countOf = (def: StatusCardDef): number =>
    tasks.length > 0 ? tasks.filter((t) => def.match(t.status)).length
      : Object.entries(data.status_distribution)
          .filter(([s]) => def.match(s))
          .reduce((acc, [, c]) => acc + c, 0);

  // 风险分布
  const riskRows = [
    { label: '高风险', key: 'high', color: RISK_COLORS.high },
    { label: '中风险', key: 'medium', color: RISK_COLORS.medium },
    { label: '低风险', key: 'low', color: RISK_COLORS.low },
    { label: '未设置', key: 'none', color: '#64748B' },
  ].map((r) => ({
    ...r,
    count: tasks.length > 0
      ? tasks.filter((t) => (t.risk_level ? t.risk_level === r.key : r.key === 'none')).length
      : 0,
  }));
  const riskTotal = riskRows.reduce((a, r) => a + r.count, 0);

  // 问候语
  const hour = new Date().getHours();
  const greet = hour < 6 ? '凌晨好' : hour < 12 ? '上午好' : hour < 18 ? '下午好' : '晚上好';
  const digestText = `${greet}。当前共 ${totalTasks} 个任务：${countOf(STATUS_CARDS[0])} 个处理中、` +
    `${countOf(STATUS_CARDS[3])} 个被阻塞；今日新增 ${data.today_digest.created} 个、关闭 ${data.today_digest.closed} 个、` +
    `推进 ${data.today_digest.advanced} 次${data.today_digest.conductor_actions > 0 ? `，调度器执行 ${data.today_digest.conductor_actions} 次` : ''}。`;

  return (
    <div style={{
      flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 16,
      paddingBottom: 24,
    }}>
      {error && (
        <div style={{ ...panelStyle(), color: '#fca5a5', fontSize: 13 }}>{error}（展示上次数据）</div>
      )}

      {/* ===== 完成度横幅 ===== */}
      <div style={{
        ...panelStyle(),
        background: 'linear-gradient(135deg, rgba(59,130,246,0.14) 0%, rgba(124,110,230,0.08) 55%, var(--color-bg-surface) 100%)',
        border: '1px solid rgba(59,130,246,0.25)',
        display: 'flex', alignItems: 'center', gap: 24, flexWrap: 'wrap',
      }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-text-secondary)', marginBottom: 4 }}>
            项目完成度
          </div>
          <div style={{ fontSize: 44, fontWeight: 700, color: 'var(--color-text-primary)', lineHeight: 1.1 }}>
            {pct}
            <span style={{ fontSize: 20, color: 'var(--color-text-secondary)', marginLeft: 2 }}>%</span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)', marginTop: 4 }}>
            {closed} 个已完成 · {open} 个尚未结束
          </div>
        </div>
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{
            height: 10, borderRadius: 5, background: 'var(--color-bg-elevated)',
            border: '1px solid var(--color-border-subtle)', overflow: 'hidden',
          }}>
            <div style={{
              height: '100%', width: `${Math.max(pct, 1)}%`, borderRadius: 5,
              background: 'linear-gradient(90deg, #3B82F6, #10B981)',
              transition: 'width .5s ease',
            }} />
          </div>
          <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginTop: 8, lineHeight: 1.6 }}>
            {digestText}
          </div>
        </div>
      </div>

      {/* ===== 状态卡片行 ===== */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {STATUS_CARDS.map((def) => {
          const count = countOf(def);
          const cardPct = totalTasks > 0 ? Math.round((count / totalTasks) * 100) : 0;
          return (
            <div
              key={def.key}
              style={{
                flex: 1, minWidth: 140,
                background: 'var(--color-bg-surface)',
                border: '1px solid var(--color-border-subtle)',
                borderRadius: 'var(--radius-md)',
                padding: '14px 16px',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{
                  width: 22, height: 22, borderRadius: 6, flexShrink: 0,
                  background: `${def.color}22`, color: def.color,
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 13, fontWeight: 700,
                }}>
                  {def.icon}
                </span>
                <span style={{ fontSize: 13, color: 'var(--color-text-secondary)' }}>{def.label}</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                <span style={{ fontSize: 26, fontWeight: 700, color: 'var(--color-text-primary)' }}>{count}</span>
                <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>{cardPct}%</span>
              </div>
              <div style={{ height: 4, borderRadius: 2, background: 'var(--color-bg-elevated)', marginTop: 8 }}>
                <div style={{
                  height: 4, borderRadius: 2, background: def.color,
                  width: `${Math.max(cardPct, 2)}%`, transition: 'width .3s ease',
                }} />
              </div>
            </div>
          );
        })}
      </div>

      {/* ===== 项目分析 ===== */}
      <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--color-text-primary)', marginTop: 4 }}>
        项目分析
      </div>

      {/* 第一行：风险分布 + 今日动态 */}
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ ...panelStyle(), flex: '1 1 300px' }}>
          <div style={sectionTitleStyle()}><span>风险分布</span><span>{riskTotal} 个任务</span></div>
          {riskRows.map((r) => {
            const p = riskTotal > 0 ? Math.round((r.count / riskTotal) * 100) : 0;
            return (
              <div key={r.key} style={{ marginBottom: 10 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 4 }}>
                  <span>{r.label}</span>
                  <span style={{ color: 'var(--color-text-primary)', fontWeight: 600 }}>{r.count}</span>
                </div>
                <div style={{ background: 'var(--color-bg-elevated)', borderRadius: 4 }}>
                  <div style={{
                    height: 7, borderRadius: 4, background: r.color,
                    width: `${Math.max(p, 2)}%`, transition: 'width .3s ease',
                  }} />
                </div>
              </div>
            );
          })}
          {riskTotal === 0 && <div style={{ color: 'var(--color-text-tertiary)', fontSize: 12 }}>暂无任务</div>}
        </div>

        <div style={{ ...panelStyle(), flex: '1 1 300px' }}>
          <div style={sectionTitleStyle()}><span>今日动态</span></div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            {[
              { label: '新增任务', value: data.today_digest.created, color: '#60A5FA' },
              { label: '关闭任务', value: data.today_digest.closed, color: '#10B981' },
              { label: '状态推进', value: data.today_digest.advanced, color: '#F59E0B' },
              { label: '调度执行', value: data.today_digest.conductor_actions, color: '#7C6EE6' },
            ].map((m) => (
              <div
                key={m.label}
                style={{
                  background: 'var(--color-bg-elevated)', borderRadius: 'var(--radius-sm)',
                  padding: '10px 12px', border: '1px solid var(--color-border-subtle)',
                }}
              >
                <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginBottom: 4 }}>{m.label}</div>
                <div style={{ fontSize: 22, fontWeight: 700, color: m.color }}>{m.value}</div>
              </div>
            ))}
          </div>
          {data.ready_to_unblock > 0 && (
            <div style={{
              marginTop: 10, padding: '8px 12px', borderRadius: 'var(--radius-sm)',
              background: 'rgba(251,140,0,0.10)', border: '1px solid rgba(251,140,0,0.35)',
              fontSize: 12, color: '#fbbf24',
            }}>
              ⚡ {data.ready_to_unblock} 个阻塞任务的上游已全部完成，可解锁推进
            </div>
          )}
        </div>
      </div>

      {/* 第二行：需要关注 + 角色贡献 */}
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ ...panelStyle(), flex: '1.2 1 320px' }}>
          <div style={sectionTitleStyle()}>
            <span>需要关注（阻塞 / 风险）</span>
            <span>{data.blocked_summary.count}</span>
          </div>
          {data.blocked_summary.tasks.length === 0 && (
            <div style={{ color: 'var(--color-text-tertiary)', fontSize: 12, padding: '12px 0' }}>无阻塞任务</div>
          )}
          {data.blocked_summary.tasks.slice(0, 6).map((t) => (
            <div
              key={t.task_id}
              onClick={() => onOpenTask(t.task_id)}
              style={{
                padding: '9px 12px', marginBottom: 6, cursor: 'pointer',
                background: 'var(--color-bg-elevated)',
                border: '1px solid var(--color-border-subtle)',
                borderLeft: '3px solid #EF4444', borderRadius: 'var(--radius-sm)',
              }}
            >
              <div style={{ fontSize: 13, color: 'var(--color-text-primary)', fontWeight: 500 }}>
                {t.identifier || t.task_id.slice(-6)} · {t.title}
              </div>
              <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)', marginTop: 3 }}>
                {t.pending_blockers.length === 0
                  ? '✅ 上游已全部完成，可解锁'
                  : `等待 ${t.pending_blockers.length} 个上游：${t.pending_blockers
                      .map((b) => `${b.identifier || b.task_id.slice(-6)}`)
                      .join('、')}`}
              </div>
            </div>
          ))}
        </div>

        <div style={{ ...panelStyle(), flex: '1 1 280px' }}>
          <div style={sectionTitleStyle()}><span>角色贡献</span></div>
          <ContributorStats tasks={tasks} />
        </div>
      </div>

      {/* 第三行：贡献热力图 */}
      <div style={{ ...panelStyle() }}>
        <div style={sectionTitleStyle()}>
          <span>任务动态热力图（近 26 周）</span>
          <span style={{ color: 'var(--color-text-tertiary)', fontWeight: 400 }}>创建 + 关闭</span>
        </div>
        <div style={{ overflowX: 'auto' }}>
          <ContributionHeatmap tasks={tasks} />
        </div>
      </div>

      {/* 第四行：累计进度 */}
      <div style={{ ...panelStyle() }}>
        <div style={sectionTitleStyle()}>
          <span>项目累计进度</span>
          <span style={{ color: 'var(--color-text-tertiary)', fontWeight: 400 }}>范围 · 已开始 · 已完成</span>
        </div>
        <ScopeChart tasks={tasks} />
      </div>
    </div>
  );
}
