import type { SessionRunInfo } from '../../lib/types';

interface RunCardProps {
  run: SessionRunInfo;
  selected: boolean;
  onClick: () => void;
}

// 状态 → 图标 + 颜色（与 CollaborationCenterPage 的 NODE_CARD_STYLES 对齐）
const STATUS_ICON: Record<string, string> = {
  completed: '✓',
  running: '▶',
  failed: '✗',
  waiting: '⏸',
  skipped: '⊘',
  pending: '·',
  ready: '○',
  cancelled: '✕',
  dormant: '💤',
  active: '▶',
};

const STATUS_COLOR: Record<string, string> = {
  completed: '#10b981',
  running: '#3b82f6',
  failed: '#ef4444',
  waiting: '#a78bfa',
  skipped: '#6b7280',
  pending: '#94a3b8',
  ready: '#fbbf24',
  cancelled: '#6b7280',
  dormant: '#8b97b0',
  active: '#3b82f6',
};

/**
 * RunCard — Session 视图中的子任务缩略卡片。
 *
 * 展示：状态图标 + workflow_id + run_id 短码 + title + started_at。
 * 点击后通知父组件选中（用于三栏视图的中栏 → 右栏联动）。
 */
export function RunCard({ run, selected, onClick }: RunCardProps) {
  const icon = STATUS_ICON[run.status] ?? '?';
  const color = STATUS_COLOR[run.status] ?? '#94a3b8';
  const startedAt = run.started_at ? new Date(run.started_at) : null;
  const startedStr = startedAt && !Number.isNaN(startedAt.getTime())
    ? startedAt.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
    : '';

  return (
    <div
      onClick={onClick}
      style={{
        padding: '10px 12px',
        borderRadius: 8,
        border: `1px solid ${selected ? color : 'var(--border, #243049)'}`,
        background: selected ? `linear-gradient(90deg, ${color}1a, transparent)` : 'var(--panel, #131c2e)',
        cursor: 'pointer',
        transition: 'border-color .15s, background .15s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        <span style={{ color, fontSize: 14, fontWeight: 600, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
          {icon} {run.workflow_id || run.run_mode}
        </span>
        <span style={{ color: '#8b97b0', fontSize: 11, fontFamily: 'ui-monospace, monospace' }}>
          {run.run_id.slice(0, 12)}
        </span>
      </div>
      {run.title && (
        <div style={{ color: '#e6ecf5', fontSize: 12, marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {run.title}
        </div>
      )}
      <div style={{ color: '#8b97b0', fontSize: 11, marginTop: 6, display: 'flex', gap: 12 }}>
        {startedStr && <span>{startedStr}</span>}
        <span style={{ textTransform: 'uppercase', letterSpacing: 0.5 }}>{run.status}</span>
      </div>
    </div>
  );
}
