// web/src/components/task/GanttView.tsx
// V3 甘特视图（§4.11.3）：时间轴 + 任务条（创建 → 关闭/当前）
// 数据基础：created_at（起点）→ closed_at | 现在（终点）；按天聚合刻度

import { useMemo } from 'react';
import type { Task } from '../../api/taskApi';

const STATUS_LABELS: Record<string, string> = {
  idea: '灵感', backlog: '待办池', discussing: '讨论中', decomposing: '拆解中',
  in_progress: '进行中', blocked: '被阻塞', validating: '验证中',
  reviewing: '评审中', closing: '关闭中', closed: '已关闭',
  canceled: '已取消', abandoned: '已废弃',
};
const BAR_COLORS: Record<string, string> = {
  high: '#e53935', medium: '#fb8c00', low: '#43a047',
};

const DAY_MS = 24 * 60 * 60 * 1000;

function fmtDate(ts: number): string {
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export default function GanttView({
  tasks,
  onOpenTask,
}: {
  tasks: Task[];
  onOpenTask: (taskId: string) => void;
}) {
  // 时间范围：最早创建 → 最晚结束（至少 7 天跨度）
  const { rows, minTs, days, dayWidth } = useMemo(() => {
    const open = tasks.filter((t) => t.created_at);
    if (open.length === 0) {
      return { rows: [], minTs: Date.now(), days: 7, dayWidth: 60 };
    }
    let min = Infinity;
    let max = -Infinity;
    const parsed = open.map((t) => {
      const start = new Date(t.created_at).getTime();
      const end = t.closed_at ? new Date(t.closed_at).getTime() : Date.now();
      return { task: t, start, end: Math.max(end, start + DAY_MS / 4) };
    });
    parsed.forEach((p) => {
      min = Math.min(min, p.start);
      max = Math.max(max, p.end);
    });
    // 范围前后各留 0.5 天空白
    min -= DAY_MS / 2;
    max += DAY_MS / 2;
    const spanDays = Math.max(Math.ceil((max - min) / DAY_MS), 7);
    // 按创建时间倒序（新任务在上）
    parsed.sort((a, b) => a.start - b.start);
    const rowH = 34;
    const width = Math.max(spanDays * 56, 600);
    return {
      rows: parsed,
      minTs: min,
      days: spanDays,
      dayWidth: Math.max(Math.floor(width / spanDays), 24),
    };
  }, [tasks]);

  // 今天竖线位置
  const todayPct = Math.min(
    Math.max(((Date.now() - minTs) / (days * DAY_MS)) * 100, 0), 100
  );

  if (rows.length === 0) {
    return (
      <div style={{
        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'var(--color-text-tertiary)',
        background: 'var(--color-bg-surface)',
        border: '1px solid var(--color-border-subtle)',
        borderRadius: 'var(--radius-md)',
      }}>
        暂无任务，无法生成甘特图
      </div>
    );
  }

  return (
    <div style={{
      flex: 1, overflow: 'auto',
      background: 'var(--color-bg-surface)',
      border: '1px solid var(--color-border-subtle)',
      borderRadius: 'var(--radius-md)',
      padding: 12,
    }}>
      <div style={{ display: 'flex' }}>
        {/* 左侧：任务名列 */}
        <div style={{ width: 220, flexShrink: 0 }}>
          <div style={{ height: 28, fontSize: 12, color: 'var(--color-text-tertiary)', padding: '6px 8px' }}>
            任务（{rows.length}）
          </div>
          {rows.map(({ task }) => (
            <div
              key={task.task_id}
              onClick={() => onOpenTask(task.task_id)}
              style={{
                height: 34, display: 'flex', alignItems: 'center',
                padding: '0 8px', cursor: 'pointer', fontSize: 12,
                color: 'var(--color-text-primary)',
                overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
                borderTop: '1px solid var(--color-border-subtle)',
              }}
              title={task.title}
            >
              <span style={{ fontWeight: 600, marginRight: 6 }}>
                {task.identifier || task.task_id.slice(-6)}
              </span>
              {task.title}
            </div>
          ))}
        </div>

        {/* 右侧：时间轴区 */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* 日期刻度头 */}
          <div style={{ height: 28, position: 'relative', borderBottom: '1px solid var(--color-border-default)' }}>
            {Array.from({ length: days }).map((_, i) => (
              <div
                key={i}
                style={{
                  position: 'absolute', left: `${(i / days) * 100}%`,
                  width: `${100 / days}%`, fontSize: 10,
                  color: 'var(--color-text-tertiary)', textAlign: 'left',
                  paddingLeft: 4, paddingTop: 6, whiteSpace: 'nowrap',
                }}
              >
                {fmtDate(minTs + i * DAY_MS)}
              </div>
            ))}
          </div>

          {/* 任务条区 */}
          <div style={{ position: 'relative' }}>
            {/* 今天竖线 */}
            <div style={{
              position: 'absolute', left: `${todayPct}%`, top: 0, bottom: 0,
              width: 1, background: '#5b8def', opacity: 0.6, zIndex: 1,
              pointerEvents: 'none',
            }} />
            {rows.map(({ task, start, end }) => {
              const leftPct = ((start - minTs) / (days * DAY_MS)) * 100;
              const widthPct = Math.max(((end - start) / (days * DAY_MS)) * 100, 1.5);
              const color = BAR_COLORS[task.risk_level] || '#5b8def';
              const isClosed = task.status === 'closed';
              return (
                <div
                  key={task.task_id}
                  onClick={() => onOpenTask(task.task_id)}
                  style={{
                    height: 34, position: 'relative', cursor: 'pointer',
                    borderTop: '1px solid var(--color-border-subtle)',
                  }}
                  title={`${task.title}：${new Date(start).toLocaleDateString('zh-CN')} → ${
                    isClosed && task.closed_at ? new Date(task.closed_at).toLocaleDateString('zh-CN') : '进行中'
                  }`}
                >
                  {/* 背景网格（每天一格参考线由 CSS 渐变模拟） */}
                  <div style={{
                    position: 'absolute', left: `${leftPct}%`, width: `${widthPct}%`,
                    top: 7, height: 20, borderRadius: 4,
                    background: color, opacity: isClosed ? 0.45 : 0.85,
                    boxShadow: task.status === 'in_progress' ? `0 0 0 1px ${color}` : 'none',
                  }} />
                  {/* 条上标签 */}
                  <div style={{
                    position: 'absolute', left: `calc(${leftPct}% + ${widthPct}% + 6px)`,
                    top: 8, fontSize: 10, color: 'var(--color-text-tertiary)', whiteSpace: 'nowrap',
                  }}>
                    {STATUS_LABELS[task.status] || task.status}
                    {task.status === 'blocked' ? ' ⛔' : ''}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* 图例 */}
      <div style={{
        display: 'flex', gap: 16, marginTop: 12, paddingTop: 10,
        borderTop: '1px solid var(--color-border-subtle)',
        fontSize: 11, color: 'var(--color-text-tertiary)',
      }}>
        <span>🟥 高风险</span>
        <span>🟧 中风险</span>
        <span>🟩 低风险</span>
        <span>条形透明 = 已关闭</span>
        <span>蓝竖线 = 今天</span>
        <span>起点 = 创建时间，终点 = 关闭时间/当前</span>
      </div>
    </div>
  );
}
