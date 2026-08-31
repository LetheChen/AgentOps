import { useState, useEffect, useCallback, Fragment } from 'react';
import { apiClient } from '../lib/api';

interface RunHistoryPageProps {
  onManageAgents: () => void;
  onReplayRun: (runId: string) => void;
  onLoadRun: (runId: string) => void;
}

interface RunRecord {
  run_id: string;
  workflow_id: string | null;
  run_mode: string;
  agent_id: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
  total_tokens_input: number;
  total_tokens_output: number;
  total_cost_usd: number;
  error: string | null;
}

interface NodeDetail {
  run_id: string;
  node_id: string;
  events: Array<Record<string, unknown>>;
  raw_events: Array<Record<string, unknown>>;
  hil_events: Array<Record<string, unknown>>;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  input_payload: Record<string, unknown> | null;
  output_payload: Record<string, unknown> | null;
  error: string | null;
}

const STATUS_LABELS: Record<string, { label: string; cls: string }> = {
  pending: { label: '启动中', cls: 'status-pill-neutral' },
  running: { label: '运行中', cls: 'status-pill-info' },
  completed: { label: '成功', cls: 'status-pill-success' },
  failed: { label: '失败', cls: 'status-pill-error' },
  cancelled: { label: '已取消', cls: 'status-pill-warning' },
  paused: { label: '已暂停', cls: 'status-pill-warning' },
  // 对话模式专用
  active: { label: '对话中', cls: 'status-pill-info' },
  dormant: { label: '休眠', cls: 'status-pill-warning' },
  // 节点详情兜底（无任何 node.completed/node.failed 事件）
  unknown: { label: '未完成', cls: 'status-pill-warning' },
};

function formatDuration(start: string, end: string | null): string {
  if (!end) return '—';
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return `${m}m ${s}s`;
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return iso;
  }
}

export function RunHistoryPage({ onManageAgents, onReplayRun, onLoadRun }: RunHistoryPageProps) {
  const PAGE_SIZE = 100;
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<string>('all');
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [runEvents, setRunEvents] = useState<Array<Record<string, unknown>> | null>(null);
  const [nodeDetail, setNodeDetail] = useState<NodeDetail | null>(null);
  const [nodeDetailNodeId, setNodeDetailNodeId] = useState<string | null>(null);
  const [eventsLoading, setEventsLoading] = useState(false);

  const loadRuns = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await apiClient.auditListRuns({ limit: PAGE_SIZE, offset: 0 });
      setRuns(resp.runs as unknown as RunRecord[]);
      setTotal(resp.total ?? resp.count);
      setHasMore((resp.runs?.length ?? 0) + 0 < (resp.total ?? 0));
    } catch (err) {
      console.error('Failed to load runs:', err);
      setRuns([]);
      setTotal(0);
      setHasMore(false);
    } finally {
      setLoading(false);
    }
  }, []);

  // 加载更多：append 到现有列表
  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    try {
      const resp = await apiClient.auditListRuns({ limit: PAGE_SIZE, offset: runs.length });
      const batch = resp.runs as unknown as RunRecord[];
      setRuns((prev) => [...prev, ...batch]);
      setHasMore(runs.length + (batch?.length ?? 0) < (resp.total ?? 0));
    } catch (err) {
      console.error('Failed to load more runs:', err);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, hasMore, runs.length]);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  const handleExpand = useCallback(async (runId: string) => {
    if (expandedRun === runId) {
      setExpandedRun(null);
      setRunEvents(null);
      return;
    }
    setExpandedRun(runId);
    setRunEvents(null);
    setNodeDetail(null);
    setEventsLoading(true);
    try {
      const resp = await apiClient.auditGetRunEvents(runId);
      setRunEvents(resp.events);
    } catch (err) {
      console.error('Failed to load events:', err);
      setRunEvents([]);
    } finally {
      setEventsLoading(false);
    }
  }, [expandedRun]);

  const handleNodeClick = useCallback(async (runId: string, nodeId: string) => {
    setNodeDetailNodeId(nodeId);
    setNodeDetail(null);
    try {
      const detail = await apiClient.auditGetNodeDetail(runId, nodeId);
      setNodeDetail(detail as unknown as NodeDetail);
    } catch (err) {
      console.error('Failed to load node detail:', err);
    }
  }, []);

  const filtered = runs.filter((r) => filter === 'all' || r.status === filter);

  const chips = [
    { id: 'all', label: '全部' },
    { id: 'completed', label: '成功' },
    { id: 'failed', label: '失败' },
    { id: 'running', label: '运行中' },
  ];

  // 从事件流中提取节点列表
  const nodeIds = runEvents
    ? Array.from(new Set(runEvents.map((e) => e.node_id).filter(Boolean))) as string[]
    : [];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* Filter Bar */}
      <div className="card" style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: '6px' }}>
          {chips.map((chip) => (
            <button key={chip.id} className={`filter-chip ${filter === chip.id ? 'active' : ''}`} onClick={() => setFilter(chip.id)}>
              {chip.label}
            </button>
          ))}
        </div>
        <div className="topbar-separator" />
        <button className="btn-secondary btn-sm" onClick={loadRuns} disabled={loading}>
          {loading ? '加载中...' : '刷新'}
        </button>
        <div style={{ marginLeft: 'auto' }}>
          <button className="btn-secondary btn-sm" onClick={onManageAgents}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="10" rx="2" /><circle cx="12" cy="5" r="2" /><path d="M12 7v4" /></svg>
            管理智能体
          </button>
        </div>
      </div>

      {/* Runs Table */}
      <div className="card" style={{ overflow: 'hidden' }}>
        {runs.length === 0 && !loading ? (
          <div className="widget-empty-state" style={{ padding: '40px' }}>
            暂无运行记录。启动一个工作流或对话后，记录会自动出现在这里。
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>运行ID</th>
                <th>工作流/Agent</th>
                <th>模式</th>
                <th>状态</th>
                <th>开始时间</th>
                <th style={{ textAlign: 'right' }}>耗时</th>
                <th style={{ textAlign: 'right' }}>Tokens (入/出)</th>
                <th>操作</th>
                <th>查看</th>
                <th>回放</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((run) => {
                const isExpanded = expandedRun === run.run_id;
                const wfLabel = run.workflow_id || run.agent_id || '-';
                const statusInfo = STATUS_LABELS[run.status] || { label: run.status, cls: 'status-pill-warning' };
                return (
                  <Fragment key={run.run_id}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => handleExpand(run.run_id)}>
                      <td className="font-mono" style={{ color: 'var(--color-primary-soft)', fontSize: '12px', maxWidth: '280px' }}>
                        <span
                          title={run.run_id}
                          style={{ display: 'inline-block', maxWidth: '230px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'middle' }}
                        >
                          {run.run_id}
                        </span>
                        <button
                          type="button"
                          aria-label="复制完整 run_id"
                          title="复制完整 run_id"
                          onClick={(e) => { e.stopPropagation; e.preventDefault(); navigator.clipboard?.writeText(run.run_id); }}
                          style={{
                            marginLeft: '6px', padding: '2px 6px', fontSize: '10px',
                            border: '1px solid var(--color-border-subtle)',
                            background: 'transparent', color: 'var(--color-text-secondary)',
                            borderRadius: 'var(--radius-sm)', cursor: 'pointer',
                            verticalAlign: 'middle',
                          }}
                        >
                          复制
                        </button>
                      </td>
                      <td>{wfLabel}</td>
                      <td><span style={{ fontSize: '11px', padding: '2px 6px', background: 'var(--color-bg-secondary)', borderRadius: 'var(--radius-sm)' }}>{run.run_mode}</span></td>
                      <td><span className={`status-pill ${statusInfo.cls}`}>{statusInfo.label}</span></td>
                      <td className="font-mono" style={{ color: 'var(--color-text-secondary)', fontSize: '12px' }}>{formatTime(run.started_at)}</td>
                      <td className="font-mono" style={{ color: 'var(--color-text-secondary)', textAlign: 'right' }}>{formatDuration(run.started_at, run.finished_at)}</td>
                      <td className="font-mono" style={{ color: 'var(--color-text-secondary)', textAlign: 'right', fontSize: '12px' }}>
                        {run.total_tokens_input} / {run.total_tokens_output}
                      </td>
                      <td>
                        <span style={{ fontSize: '13px', color: isExpanded ? 'var(--color-primary)' : 'var(--color-text-secondary)' }}>
                          {isExpanded ? '收起' : '详情'}
                        </span>
                      </td>
                      <td>
                        <button
                          className="btn-secondary btn-sm"
                          style={{ height: '24px', padding: '0 8px', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '3px' }}
                          onClick={(e) => { e.stopPropagation(); onLoadRun(run.run_id); }}
                          title="在对话页中查看此会话"
                        >
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
                          对话
                        </button>
                      </td>
                      <td>
                        <button
                          className="btn-secondary btn-sm"
                          style={{ height: '24px', padding: '0 8px', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '3px' }}
                          onClick={(e) => { e.stopPropagation(); onReplayRun(run.run_id); }}
                          title="在审计回放页中回放此运行"
                        >
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                          回放
                        </button>
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr>
                        <td colSpan={10} style={{ padding: 0 }}>
                          <div className="expanded-detail">
                            {run.error && (
                              <div style={{ marginBottom: '12px', padding: '8px 12px', background: 'rgba(239, 68, 68, 0.08)', borderRadius: 'var(--radius-sm)', color: 'var(--state-error)', fontSize: '12px' }}>
                                错误: {run.error}
                              </div>
                            )}
                            <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: '8px' }}>
                              事件流 ({runEvents?.length || 0} 条)
                            </div>
                            {eventsLoading ? (
                              <div style={{ color: 'var(--color-text-tertiary)', fontSize: '12px' }}>加载中...</div>
                            ) : nodeIds.length > 0 ? (
                              <>
                                <div className="node-timeline" style={{ flexWrap: 'wrap' }}>
                                  {nodeIds.map((nid, i) => (
                                    <div key={nid} style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                      <button
                                        className="node-timeline-item"
                                        style={{
                                          cursor: 'pointer', border: 'none', background: 'transparent',
                                          color: nodeDetailNodeId === nid ? 'var(--color-primary)' : 'inherit',
                                          fontWeight: nodeDetailNodeId === nid ? 600 : 400,
                                        }}
                                        onClick={(e) => { e.stopPropagation(); handleNodeClick(run.run_id, nid); }}
                                      >
                                        {nid}
                                      </button>
                                      {i < nodeIds.length - 1 && <span className="node-timeline-arrow">→</span>}
                                    </div>
                                  ))}
                                </div>
                                {nodeDetail && nodeDetailNodeId && (
                                  <div style={{ marginTop: '12px', padding: '12px', background: 'var(--color-bg-secondary)', borderRadius: 'var(--radius-sm)', fontSize: '12px' }}>
                                    <div style={{ fontWeight: 600, marginBottom: '8px' }}>节点详情: {nodeDetailNodeId}</div>
                                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                                      <div>
                                        <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>状态</div>
                                        <span className={`status-pill ${nodeDetail.status === 'completed' ? 'status-pill-success' : nodeDetail.status === 'failed' ? 'status-pill-error' : 'status-pill-info'}`}>
                                          {nodeDetail.status}
                                        </span>
                                      </div>
                                      <div>
                                        <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>时间</div>
                                        <span className="font-mono">
                                          {nodeDetail.started_at ? formatTime(nodeDetail.started_at) : '-'}
                                          {' → '}
                                          {nodeDetail.finished_at ? formatTime(nodeDetail.finished_at) : '-'}
                                        </span>
                                      </div>
                                    </div>
                                    {nodeDetail.input_payload && (
                                      <div style={{ marginTop: '8px' }}>
                                        <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>输入</div>
                                        <pre className="font-mono" style={{ fontSize: '11px', overflow: 'auto', margin: 0 }}>
                                          {JSON.stringify(nodeDetail.input_payload, null, 2)}
                                        </pre>
                                      </div>
                                    )}
                                    {nodeDetail.output_payload && (
                                      <div style={{ marginTop: '8px' }}>
                                        <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>输出</div>
                                        <pre className="font-mono" style={{ fontSize: '11px', overflow: 'auto', margin: 0 }}>
                                          {JSON.stringify(nodeDetail.output_payload, null, 2)}
                                        </pre>
                                      </div>
                                    )}
                                    {nodeDetail.error && (
                                      <div style={{ marginTop: '8px', color: 'var(--state-error)' }}>
                                        <div style={{ marginBottom: '4px' }}>错误</div>
                                        <pre className="font-mono" style={{ fontSize: '11px', margin: 0 }}>{nodeDetail.error}</pre>
                                      </div>
                                    )}
                                    {nodeDetail.hil_events.length > 0 && (
                                      <div style={{ marginTop: '8px' }}>
                                        <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>HIL 介入 ({nodeDetail.hil_events.length})</div>
                                        {nodeDetail.hil_events.map((h, i) => (
                                          <pre key={i} className="font-mono" style={{ fontSize: '11px', margin: '0 0 4px 0' }}>
                                            {h.input_payload ? JSON.stringify(h.input_payload) : ''}
                                          </pre>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                )}
                              </>
                            ) : (
                              <div style={{ color: 'var(--color-text-tertiary)', fontSize: '12px' }}>无事件</div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
        {/* 分页：加载更多 + 总数提示 */}
        {!loading && runs.length > 0 && (
          <div className="rh-pagination">
            <span className="rh-pagination-info">
              已显示 {runs.length} / {total} 条
            </span>
            {hasMore && (
              <button
                className="btn-secondary btn-sm"
                onClick={loadMore}
                disabled={loadingMore}
              >
                {loadingMore ? '加载中...' : '加载更多'}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
