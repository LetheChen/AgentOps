// web/src/components/task/TaskListView.tsx
// V3 列表视图（§4.11.3）：全字段表格 + 排序，行点击直达详情
// V3.2：状态筛选提升至 TaskCenterPage（阶段卡片栏），本组件受控显示

import { useState, useMemo } from 'react';
import type { Task } from '../../api/taskApi';

const STATUS_LABELS: Record<string, string> = {
  idea: '灵感', backlog: '待办池', discussing: '讨论中', decomposing: '拆解中',
  in_progress: '进行中', blocked: '被阻塞', validating: '验证中',
  reviewing: '评审中', closing: '关闭中', closed: '已关闭',
  canceled: '已取消', abandoned: '已废弃',
};
const RISK_COLORS: Record<string, string> = { high: '#e53935', medium: '#fb8c00', low: '#43a047' };
const RISK_LABELS: Record<string, string> = { high: '高', medium: '中', low: '低' };

function statusPillColor(status: string): string {
  if (['closed', 'done'].includes(status)) return '#43a047';
  if (['blocked', 'canceled', 'abandoned'].includes(status)) return '#e53935';
  if (['in_progress', 'validating', 'reviewing', 'closing', 'decomposing'].includes(status)) return '#fb8c00';
  return '#5b8def';
}

type SortKey = 'updated_at' | 'created_at' | 'risk_level' | 'status' | 'title';

export default function TaskListView({
  tasks,
  onOpenTask,
  statusFilter = '',
  onStatusFilterChange,
}: {
  tasks: Task[];
  onOpenTask: (taskId: string) => void;
  statusFilter?: string;
  onStatusFilterChange?: (status: string) => void;
}) {
  const [sortKey, setSortKey] = useState<SortKey>('updated_at');
  const [sortAsc, setSortAsc] = useState(false);

  const rows = useMemo(() => {
    let list = statusFilter ? tasks.filter((t) => t.status === statusFilter) : [...tasks];
    const riskOrder: Record<string, number> = { high: 0, medium: 1, low: 2 };
    list.sort((a, b) => {
      let cmp = 0;
      if (sortKey === 'risk_level') {
        cmp = (riskOrder[a.risk_level] ?? 3) - (riskOrder[b.risk_level] ?? 3);
      } else if (sortKey === 'title' || sortKey === 'status') {
        cmp = String(a[sortKey] || '').localeCompare(String(b[sortKey] || ''));
      } else {
        cmp = String(a[sortKey] || '').localeCompare(String(b[sortKey] || ''));
      }
      return sortAsc ? cmp : -cmp;
    });
    return list;
  }, [tasks, statusFilter, sortKey, sortAsc]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortAsc((v) => !v);
    } else {
      setSortKey(key);
      setSortAsc(false);
    }
  };

  const thStyle = (key?: SortKey): React.CSSProperties => ({
    textAlign: 'left', padding: '8px 10px', fontSize: 12,
    color: 'var(--color-text-secondary)', fontWeight: 600,
    cursor: key ? 'pointer' : 'default', userSelect: 'none',
    borderBottom: '1px solid var(--color-border-default)',
    whiteSpace: 'nowrap',
  });
  const tdStyle: React.CSSProperties = {
    padding: '8px 10px', fontSize: 13, color: 'var(--color-text-primary)',
    borderBottom: '1px solid var(--color-border-subtle)',
  };

  const sortMark = (key: SortKey) => (sortKey === key ? (sortAsc ? ' ↑' : ' ↓') : '');

  return (
    <div style={{
      flex: 1, overflow: 'auto',
      background: 'var(--color-bg-surface)',
      border: '1px solid var(--color-border-subtle)',
      borderRadius: 'var(--radius-md)',
    }}>
      {/* V3.2 工具行：筛选状态提示（筛选由上方阶段卡片栏驱动，此处可快速清除） */}
      {statusFilter && (
        <div style={{
          display: 'flex', gap: 8, alignItems: 'center',
          padding: '8px 12px', borderBottom: '1px solid var(--color-border-subtle)',
        }}>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
            已筛选阶段：{STATUS_LABELS[statusFilter] || statusFilter}（{rows.length} 条）
          </span>
          <button
            onClick={() => onStatusFilterChange?.('')}
            style={chipStyle}
          >
            ✕ 清除筛选
          </button>
        </div>
      )}

      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={thStyle()}>标识符</th>
            <th style={thStyle('title')} onClick={() => toggleSort('title')}>标题{sortMark('title')}</th>
            <th style={thStyle('status')} onClick={() => toggleSort('status')}>状态{sortMark('status')}</th>
            <th style={thStyle('risk_level')} onClick={() => toggleSort('risk_level')}>风险{sortMark('risk_level')}</th>
            <th style={thStyle('created_at')} onClick={() => toggleSort('created_at')}>创建时间{sortMark('created_at')}</th>
            <th style={thStyle('updated_at')} onClick={() => toggleSort('updated_at')}>更新时间{sortMark('updated_at')}</th>
            <th style={thStyle()}>负责人</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={7} style={{ ...tdStyle, textAlign: 'center', color: 'var(--color-text-tertiary)', padding: 32 }}>
                暂无任务
              </td>
            </tr>
          )}
          {rows.map((t) => (
            <tr
              key={t.task_id}
              onClick={() => onOpenTask(t.task_id)}
              style={{ cursor: 'pointer', background: 'transparent' }}
              onMouseEnter={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = 'var(--color-bg-elevated)'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = 'transparent'; }}
            >
              <td style={{ ...tdStyle, fontWeight: 600, whiteSpace: 'nowrap' }}>
                {t.identifier || t.task_id.slice(-6)}
              </td>
              <td style={{ ...tdStyle, maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {t.title}
              </td>
              <td style={tdStyle}>
                <span style={{
                  fontSize: 11, padding: '2px 8px', borderRadius: 'var(--radius-full)',
                  background: statusPillColor(t.status), color: '#fff', fontWeight: 600, whiteSpace: 'nowrap',
                }}>
                  {STATUS_LABELS[t.status] || t.status}
                </span>
              </td>
              <td style={tdStyle}>
                <span style={{
                  fontSize: 11, padding: '1px 6px', borderRadius: 'var(--radius-full)',
                  background: RISK_COLORS[t.risk_level] || '#666', color: '#fff', fontWeight: 600,
                }}>
                  {RISK_LABELS[t.risk_level] || t.risk_level}
                </span>
              </td>
              <td style={{ ...tdStyle, fontSize: 12, color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>
                {t.created_at ? new Date(t.created_at).toLocaleString('zh-CN', { hour12: false }) : '-'}
              </td>
              <td style={{ ...tdStyle, fontSize: 12, color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>
                {t.updated_at ? new Date(t.updated_at).toLocaleString('zh-CN', { hour12: false }) : '-'}
              </td>
              <td style={{ ...tdStyle, fontSize: 12, color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>
                {t.assignee_name || '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const chipStyle: React.CSSProperties = {
  padding: '3px 10px', fontSize: 12, cursor: 'pointer',
  background: 'var(--color-bg-elevated)',
  color: 'var(--color-text-secondary)',
  border: '1px solid var(--color-border-subtle)',
  borderRadius: 'var(--radius-full)',
};
