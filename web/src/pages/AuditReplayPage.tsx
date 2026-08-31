import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { ReactFlow, Background, Controls, MiniMap, type Node, type Edge } from 'reactflow';
import 'reactflow/dist/style.css';
import { apiClient } from '../lib/api';
import type { DagEvent } from '../lib/types';

interface AuditReplayPageProps {
  initialRunId?: string | null;
  onBack: () => void;
}

interface RunRecord {
  run_id: string;
  workflow_id: string | null;
  run_mode: string;
  agent_id: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
}

interface NodeDetail {
  run_id: string;
  node_id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  input_payload: Record<string, unknown> | null;
  output_payload: Record<string, unknown> | null;
  error: string | null;
  events: Array<Record<string, unknown>>;
  hil_events: Array<Record<string, unknown>>;
}

// 状态颜色（复用 RunMonitorPage 配色）
const STATUS_COLORS: Record<string, string> = {
  completed: '#10B981',
  running: '#3B82F6',
  pending: '#475569',
  ready: '#FBBF24',
  waiting: '#A78BFA',
  failed: '#EF4444',
  skipped: '#6B7280',
};

const STATUS_BG: Record<string, string> = {
  completed: 'rgba(16, 185, 129, 0.08)',
  running: 'rgba(59, 130, 246, 0.12)',
  pending: 'rgba(30, 41, 59, 0.8)',
  ready: 'rgba(251, 191, 36, 0.08)',
  waiting: 'rgba(167, 139, 250, 0.08)',
  failed: 'rgba(239, 68, 68, 0.08)',
  skipped: 'rgba(30, 41, 59, 0.5)',
};

const EVENT_TYPE_LABELS: Record<string, string> = {
  'run.created': '运行创建',
  'run.completed': '运行完成',
  'run.failed': '运行失败',
  'run.cancelled': '运行取消',
  'node.ready': '节点就绪',
  'node.started': '节点启动',
  'node.handoff': '节点交接',
  'node.completed': '节点完成',
  'node.failed': '节点失败',
  'widget.update': '组件更新',
  'widget.input': '组件输入',
  'usage': '用量统计',
};

const EVENT_TYPE_COLORS: Record<string, string> = {
  'run.created': '#3B82F6',
  'run.completed': '#10B981',
  'run.failed': '#EF4444',
  'run.cancelled': '#F59E0B',
  'node.ready': '#FBBF24',
  'node.started': '#3B82F6',
  'node.handoff': '#8B5CF6',
  'node.completed': '#10B981',
  'node.failed': '#EF4444',
  'widget.update': '#06B6D4',
  'widget.input': '#EC4899',
  'usage': '#6B7280',
};

// 自动布局：左到右分层
function autoLayoutNodes(nodeIds: string[], edges: { source: string; target: string }[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  if (nodeIds.length === 0) return positions;

  // 计算入度
  const inDegree = new Map<string, number>();
  nodeIds.forEach((id) => inDegree.set(id, 0));
  edges.forEach((e) => {
    if (inDegree.has(e.target)) inDegree.set(e.target, (inDegree.get(e.target) || 0) + 1);
  });

  // BFS 分层
  const levels: string[][] = [];
  let current = nodeIds.filter((id) => (inDegree.get(id) || 0) === 0);
  const visited = new Set<string>();

  while (current.length > 0) {
    levels.push(current);
    current.forEach((id) => visited.add(id));
    const next: string[] = [];
    for (const id of current) {
      for (const e of edges) {
        if (e.source === id && !visited.has(e.target)) {
          inDegree.set(e.target, (inDegree.get(e.target) || 0) - 1);
          if ((inDegree.get(e.target) || 0) <= 0) {
            next.push(e.target);
            visited.add(e.target);
          }
        }
      }
    }
    // 防死循环：把未访问的节点加入下一层
    if (next.length === 0) {
      const remaining = nodeIds.filter((id) => !visited.has(id));
      if (remaining.length > 0) {
        levels.push(remaining);
        remaining.forEach((id) => visited.add(id));
      }
      break;
    }
    current = Array.from(new Set(next));
  }

  // 分配坐标
  const colWidth = 200;
  const rowHeight = 100;
  levels.forEach((level, col) => {
    level.forEach((id, row) => {
      positions.set(id, {
        x: 80 + col * colWidth,
        y: 60 + row * rowHeight + (col % 2) * 30, // 交错布局
      });
    });
  });

  // 兜底：未分配的节点
  nodeIds.forEach((id) => {
    if (!positions.has(id)) {
      positions.set(id, { x: 80, y: 60 + positions.size * rowHeight });
    }
  });

  return positions;
}

// 从工作流定义中提取边
function edgesFromWorkflow(wf: Record<string, unknown>): { source: string; target: string }[] {
  const nodes = wf.nodes as Record<string, { after?: string[] }> | undefined;
  if (!nodes) return [];
  const edges: { source: string; target: string }[] = [];
  for (const [nid, node] of Object.entries(nodes)) {
    if (node.after) {
      for (const dep of node.after) {
        edges.push({ source: dep, target: nid });
      }
    }
  }
  return edges;
}

export function AuditReplayPage({ initialRunId, onBack }: AuditReplayPageProps) {
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>(initialRunId || '');
  const [events, setEvents] = useState<DagEvent[]>([]);
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [workflowDef, setWorkflowDef] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);

  // 回放状态
  const [cursor, setCursor] = useState(0); // 当前事件索引
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1); // 倍速
  const [nodeDetail, setNodeDetail] = useState<NodeDetail | null>(null);
  const [nodeDetailNodeId, setNodeDetailNodeId] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const playTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const eventListRef = useRef<HTMLDivElement>(null);

  // 加载 run 列表
  const loadRuns = useCallback(async () => {
    try {
      const resp = await apiClient.auditListRuns({ limit: 100 });
      setRuns(resp.runs as unknown as RunRecord[]);
    } catch (err) {
      console.error('Failed to load runs:', err);
      setRuns([]);
    }
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  // 加载指定 run 的事件 + 概要
  const loadRunData = useCallback(async (runId: string) => {
    if (!runId) return;
    setLoading(true);
    setCursor(0);
    setPlaying(false);
    setNodeDetail(null);
    setNodeDetailNodeId(null);
    setWorkflowDef(null);

    try {
      const [eventsResp, summaryResp] = await Promise.all([
        apiClient.auditGetRunEvents(runId),
        apiClient.auditGetRunSummary(runId),
      ]);
      setEvents(eventsResp.events as unknown as DagEvent[]);
      setSummary(summaryResp);

      // 如果有 workflow_id，尝试加载工作流定义以获取 DAG 拓扑
      const wfId = summaryResp.workflow_id as string | undefined;
      if (wfId) {
        try {
          const wf = await apiClient.getWorkflowDetail(wfId);
          setWorkflowDef(wf);
        } catch {
          // 工作流可能已删除，忽略
        }
      }
    } catch (err) {
      console.error('Failed to load run data:', err);
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (initialRunId) {
      setSelectedRunId(initialRunId);
      loadRunData(initialRunId);
    }
  }, [initialRunId, loadRunData]);

  // 提取所有节点 ID
  const nodeIds = useMemo(() => {
    const ids = new Set<string>();
    events.forEach((e) => {
      if (e.node_id) ids.add(e.node_id);
    });
    // 也从工作流定义中提取
    if (workflowDef?.nodes) {
      Object.keys(workflowDef.nodes as object).forEach((id) => ids.add(id));
    }
    return Array.from(ids);
  }, [events, workflowDef]);

  // 提取边
  const edges = useMemo(() => {
    if (workflowDef) {
      return edgesFromWorkflow(workflowDef);
    }
    // 无工作流定义时，从事件时序推断：node.completed 的 payload.outputs 可能含目标
    // 简单回退：按 node.started 时序连线
    const startedOrder: string[] = [];
    events.forEach((e) => {
      if (e.type === 'node.started' && e.node_id && !startedOrder.includes(e.node_id)) {
        startedOrder.push(e.node_id);
      }
    });
    const inferred: { source: string; target: string }[] = [];
    for (let i = 0; i < startedOrder.length - 1; i++) {
      inferred.push({ source: startedOrder[i], target: startedOrder[i + 1] });
    }
    return inferred;
  }, [events, workflowDef]);

  // 节点位置
  const nodePositions = useMemo(() => autoLayoutNodes(nodeIds, edges), [nodeIds, edges]);

  // 核心回放逻辑：根据 cursor 计算各节点状态
  const replayNodeStates = useMemo(() => {
    const states: Record<string, string> = {};
    nodeIds.forEach((id) => { states[id] = 'pending'; });

    // 重放 0..cursor 的事件
    for (let i = 0; i <= cursor && i < events.length; i++) {
      const e = events[i];
      if (!e.node_id) continue;
      switch (e.type) {
        case 'node.ready':
          states[e.node_id] = 'ready';
          break;
        case 'node.started':
          states[e.node_id] = 'running';
          break;
        case 'node.completed':
          states[e.node_id] = 'completed';
          break;
        case 'node.failed':
          states[e.node_id] = 'failed';
          break;
      }
    }
    return states;
  }, [events, cursor, nodeIds]);

  // 协作可视化 v2：replayedHandoffs — cursor 推进到 handoff sequence 时显示气泡
  // cursor 回退时气泡消失（符合时间线回放语义）
  const replayedHandoffs = useMemo(() => {
    const result: Array<{
      sequence: number;
      from_node: string;
      from_role: string;
      to_node: string;
      to_role: string;
      port: string;
      payload_size: number;
      summary: string;
    }> = [];
    for (let i = 0; i <= cursor && i < events.length; i++) {
      const e = events[i];
      if (e.type !== 'node.handoff' || !e.payload) continue;
      const p = e.payload;
      result.push({
        sequence: e.sequence ?? i,
        from_node: (p.from as string) || '',
        from_role: (p.from_role as string) || '',
        to_node: (p.to as string) || '',
        to_role: (p.to_role as string) || '',
        port: (p.port as string) || '',
        payload_size: (p.payload_size as number) || 0,
        summary: (p.summary as string) || '',
      });
    }
    return result;
  }, [events, cursor]);

  // 当前事件
  const currentEvent = events[cursor] || null;

  // 播放控制
  useEffect(() => {
    if (!playing || events.length === 0) return;
    if (cursor >= events.length - 1) {
      setPlaying(false);
      return;
    }
    const delay = Math.max(100, 800 / speed);
    playTimerRef.current = setTimeout(() => {
      setCursor((prev) => Math.min(prev + 1, events.length - 1));
    }, delay);
    return () => {
      if (playTimerRef.current) clearTimeout(playTimerRef.current);
    };
  }, [playing, cursor, events.length, speed]);

  // 滚动事件列表到当前事件
  useEffect(() => {
    if (eventListRef.current) {
      const el = eventListRef.current.querySelector(`[data-event-idx="${cursor}"]`);
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [cursor]);

  // 构建 ReactFlow 节点
  const rfNodes: Node[] = useMemo(() => {
    return nodeIds.map((id) => {
      const status = replayNodeStates[id] || 'pending';
      const color = STATUS_COLORS[status] || STATUS_COLORS.pending;
      const bg = STATUS_BG[status] || STATUS_BG.pending;
      const pos = nodePositions.get(id) || { x: 80, y: 60 };
      const isCurrent = currentEvent?.node_id === id;
      return {
        id,
        position: pos,
        data: { label: id },
        style: {
          background: bg,
          color: status === 'pending' ? '#64748B' : '#E2E8F0',
          border: `${isCurrent ? 3 : status === 'running' ? 2 : 1.5}px solid ${color}`,
          borderRadius: '8px',
          padding: '8px 16px',
          fontSize: '13px',
          fontWeight: isCurrent ? 700 : 500,
          boxShadow: isCurrent ? `0 0 0 4px ${color}33` : undefined,
        },
      };
    });
  }, [nodeIds, replayNodeStates, nodePositions, currentEvent]);

  const rfEdges: Edge[] = useMemo(() => {
    return edges.map((e) => {
      const targetStatus = replayNodeStates[e.target] || 'pending';
      let style: React.CSSProperties = { stroke: '#334155', strokeWidth: 1.5 };
      if (targetStatus === 'completed') style = { stroke: '#10B981', strokeWidth: 1.5, opacity: 0.6 };
      else if (targetStatus === 'running') style = { stroke: '#3B82F6', strokeWidth: 2 };
      else if (targetStatus === 'failed') style = { stroke: '#EF4444', strokeWidth: 1.5 };
      return {
        id: `${e.source}-${e.target}`,
        source: e.source,
        target: e.target,
        style,
        animated: targetStatus === 'running',
      };
    });
  }, [edges, replayNodeStates]);

  // 加载节点详情
  const handleNodeClick = useCallback(async (runId: string, nodeId: string) => {
    setNodeDetailNodeId(nodeId);
    setNodeDetail(null);
    setDetailLoading(true);
    try {
      const detail = await apiClient.auditGetNodeDetail(runId, nodeId);
      setNodeDetail(detail as unknown as NodeDetail);
    } catch (err) {
      console.error('Failed to load node detail:', err);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const handleLoadRun = useCallback(() => {
    if (selectedRunId) loadRunData(selectedRunId);
  }, [selectedRunId, loadRunData]);

  const handleStep = useCallback((dir: 1 | -1) => {
    setCursor((prev) => Math.max(0, Math.min(prev + dir, events.length - 1)));
  }, [events.length]);

  const handleReset = useCallback(() => {
    setPlaying(false);
    setCursor(0);
  }, []);

  const handleJumpTo = useCallback((idx: number) => {
    setPlaying(false);
    setCursor(Math.max(0, Math.min(idx, events.length - 1)));
  }, [events.length]);

  const isConversational = summary?.run_mode === 'conversational' || summary?.run_mode === 'task';
  const hasGraph = nodeIds.length > 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', height: '100%' }}>
      {/* 顶部控制栏 */}
      <div className="card" style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <button className="btn-secondary btn-sm" onClick={onBack} style={{ height: '30px' }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5" /><path d="M12 19l-7-7 7-7" /></svg>
          返回
        </button>
        <div className="topbar-separator" />
        <span style={{ fontSize: '13px', color: 'var(--color-text-tertiary)' }}>回放运行：</span>
        <select
          className="input-base"
          style={{ height: '30px', fontSize: '12px', minWidth: '280px' }}
          value={selectedRunId}
          onChange={(e) => setSelectedRunId(e.target.value)}
          disabled={loading}
        >
          <option value="">— 选择运行记录 —</option>
          {runs.map((r) => (
            <option key={r.run_id} value={r.run_id}>
              {r.run_id.slice(0, 20)}... · {r.workflow_id || r.agent_id || 'unknown'} · {r.status}
            </option>
          ))}
        </select>
        <button className="btn-primary btn-sm" onClick={handleLoadRun} disabled={loading || !selectedRunId} style={{ height: '30px' }}>
          {loading ? '加载中...' : '加载'}
        </button>

        {events.length > 0 && (
          <>
            <div className="topbar-separator" />
            {/* 回放控制 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
              <button className="btn-secondary btn-sm" onClick={handleReset} title="重置到开头" style={{ height: '30px', padding: '0 8px' }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="19 20 9 12 19 4 19 20" /><line x1="5" y1="19" x2="5" y2="5" /></svg>
              </button>
              <button className="btn-secondary btn-sm" onClick={() => handleStep(-1)} disabled={cursor <= 0} title="上一步" style={{ height: '30px', padding: '0 8px' }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
              </button>
              <button
                className={`btn-sm ${playing ? 'btn-secondary' : 'btn-primary'}`}
                onClick={() => {
                  if (cursor >= events.length - 1) setCursor(0);
                  setPlaying(!playing);
                }}
                disabled={events.length === 0}
                style={{ height: '30px', padding: '0 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
              >
                {playing ? (
                  <><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" /><rect x="14" y="4" width="4" height="16" /></svg>暂停</>
                ) : (
                  <><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3" /></svg>播放</>
                )}
              </button>
              <button className="btn-secondary btn-sm" onClick={() => handleStep(1)} disabled={cursor >= events.length - 1} title="下一步" style={{ height: '30px', padding: '0 8px' }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
              </button>
              <button className="btn-secondary btn-sm" onClick={() => setCursor(events.length - 1)} disabled={cursor >= events.length - 1} title="跳到末尾" style={{ height: '30px', padding: '0 8px' }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 4 15 12 5 20 5 4" /><line x1="19" y1="5" x2="19" y2="19" /></svg>
              </button>
            </div>
            {/* 速度 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '12px' }}>
              <span style={{ color: 'var(--color-text-tertiary)' }}>速度</span>
              {[0.5, 1, 2, 4].map((s) => (
                <button
                  key={s}
                  className={`btn-sm ${speed === s ? 'btn-primary' : 'btn-secondary'}`}
                  onClick={() => setSpeed(s)}
                  style={{ height: '24px', padding: '0 6px', fontSize: '11px', minWidth: '32px' }}
                >
                  {s}x
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      {events.length === 0 ? (
        <div className="card" style={{ padding: '60px', textAlign: 'center', color: 'var(--color-text-tertiary)' }}>
          {loading ? '加载事件中...' : '选择一个运行记录开始回放。回放页面支持时间轴滑块、节点状态高亮、事件逐步重放。'}
        </div>
      ) : (
        <>
          {/* 主区域：DAG 画布 + 事件列表 */}
          <div style={{ display: 'flex', gap: '12px', flex: 1, minHeight: '400px' }}>
            {/* 左：DAG 画布 */}
            <div className="card" style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: 0 }}>
              <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span style={{ fontWeight: 600, fontSize: '13px' }}>DAG 回放画布</span>
                <span className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>
                  {nodeIds.length} 节点 · {edges.length} 边
                </span>
              </div>
              <div style={{ flex: 1, position: 'relative', minHeight: '350px' }}>
                {hasGraph ? (
                  <ReactFlow
                    nodes={rfNodes}
                    edges={rfEdges}
                    fitView
                    fitViewOptions={{ padding: 0.2 }}
                    proOptions={{ hideAttribution: true }}
                    onNodeClick={(_, node) => selectedRunId && handleNodeClick(selectedRunId, node.id)}
                  >
                    <Background color="#1E293B" gap={20} />
                    <Controls showInteractive={false} />
                    <MiniMap
                      nodeColor={(n) => STATUS_COLORS[replayNodeStates[n.id] || 'pending'] || '#475569'}
                      maskColor="rgba(11, 15, 20, 0.7)"
                    />
                  </ReactFlow>
                ) : (
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--color-text-tertiary)', fontSize: '13px' }}>
                    {isConversational ? '对话模式无 DAG 拓扑，请在右侧查看事件时间轴' : '暂无节点数据'}
                  </div>
                )}
                {/* 图例 */}
                <div style={{ position: 'absolute', bottom: '8px', left: '8px', display: 'flex', gap: '8px', background: 'rgba(15, 23, 42, 0.85)', padding: '6px 10px', borderRadius: '6px', fontSize: '11px' }}>
                  {[
                    { color: '#10B981', label: '已完成' },
                    { color: '#3B82F6', label: '运行中' },
                    { color: '#FBBF24', label: '就绪' },
                    { color: '#475569', label: '等待' },
                    { color: '#EF4444', label: '失败' },
                  ].map((item) => (
                    <div key={item.label} style={{ display: 'flex', alignItems: 'center', gap: '3px' }}>
                      <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: item.color }} />
                      <span style={{ color: '#94A3B8' }}>{item.label}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* 右：事件列表 + 节点详情 */}
            <div style={{ width: '380px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {/* 当前事件信息 */}
              <div className="card" style={{ padding: '12px' }}>
                <div style={{ fontSize: '11px', color: 'var(--color-text-tertiary)', marginBottom: '6px' }}>当前事件</div>
                {currentEvent ? (
                  <>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                      <span style={{
                        fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
                        background: `${EVENT_TYPE_COLORS[currentEvent.type] || '#6B7280'}22`,
                        color: EVENT_TYPE_COLORS[currentEvent.type] || '#6B7280',
                        fontWeight: 600,
                      }}>
                        {EVENT_TYPE_LABELS[currentEvent.type] || currentEvent.type}
                      </span>
                      {currentEvent.node_id && (
                        <span className="font-mono" style={{ fontSize: '12px', color: 'var(--color-primary-soft)' }}>
                          {currentEvent.node_id}
                        </span>
                      )}
                    </div>
                    {currentEvent.occurred_at && (
                      <div className="font-mono" style={{ fontSize: '11px', color: 'var(--color-text-tertiary)' }}>
                        {new Date(currentEvent.occurred_at).toLocaleString('zh-CN', { hour12: false })}
                      </div>
                    )}
                    {currentEvent.payload && Object.keys(currentEvent.payload).length > 0 && (
                      <pre className="font-mono" style={{ fontSize: '11px', marginTop: '8px', maxHeight: '120px', overflow: 'auto', margin: '8px 0 0 0', padding: '6px', background: 'var(--color-bg-secondary)', borderRadius: '4px' }}>
                        {JSON.stringify(currentEvent.payload, null, 2)}
                      </pre>
                    )}
                  </>
                ) : (
                  <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>—</div>
                )}
              </div>

              {/* 节点详情（点击 DAG 节点后显示） */}
              {nodeDetailNodeId && (
                <div className="card" style={{ padding: '12px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                    <span style={{ fontWeight: 600, fontSize: '13px' }}>节点: {nodeDetailNodeId}</span>
                    <button onClick={() => { setNodeDetail(null); setNodeDetailNodeId(null); }} style={{ border: 'none', background: 'none', cursor: 'pointer', color: 'var(--color-text-tertiary)', fontSize: '14px' }}>×</button>
                  </div>
                  {detailLoading ? (
                    <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>加载中...</div>
                  ) : nodeDetail ? (
                    <div style={{ fontSize: '12px' }}>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', marginBottom: '8px' }}>
                        <div>
                          <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '2px' }}>状态</div>
                          <span className={`status-pill ${nodeDetail.status === 'completed' ? 'status-pill-success' : nodeDetail.status === 'failed' ? 'status-pill-error' : 'status-pill-info'}`}>
                            {nodeDetail.status}
                          </span>
                        </div>
                        <div>
                          <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '2px' }}>耗时</div>
                          <span className="font-mono">
                            {nodeDetail.started_at && nodeDetail.finished_at
                              ? `${Math.round(new Date(nodeDetail.finished_at).getTime() - new Date(nodeDetail.started_at).getTime())}ms`
                              : '—'}
                          </span>
                        </div>
                      </div>
                      {nodeDetail.input_payload && (
                        <div style={{ marginBottom: '6px' }}>
                          <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '2px' }}>输入</div>
                          <pre className="font-mono" style={{ fontSize: '10px', maxHeight: '80px', overflow: 'auto', margin: 0, padding: '4px', background: 'var(--color-bg-secondary)', borderRadius: '4px' }}>
                            {JSON.stringify(nodeDetail.input_payload, null, 2)}
                          </pre>
                        </div>
                      )}
                      {nodeDetail.output_payload && (
                        <div style={{ marginBottom: '6px' }}>
                          <div style={{ color: 'var(--color-text-tertiary)', marginBottom: '2px' }}>输出</div>
                          <pre className="font-mono" style={{ fontSize: '10px', maxHeight: '80px', overflow: 'auto', margin: 0, padding: '4px', background: 'var(--color-bg-secondary)', borderRadius: '4px' }}>
                            {JSON.stringify(nodeDetail.output_payload, null, 2)}
                          </pre>
                        </div>
                      )}
                      {nodeDetail.error && (
                        <div style={{ color: 'var(--state-error)' }}>
                          <div style={{ marginBottom: '2px' }}>错误</div>
                          <pre className="font-mono" style={{ fontSize: '10px', margin: 0 }}>{nodeDetail.error}</pre>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>无详情数据</div>
                  )}
                </div>
              )}

              {/* 事件列表 */}
              <div className="card" style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: 0 }}>
                <div style={{ padding: '10px 12px', borderBottom: '1px solid var(--color-border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ fontWeight: 600, fontSize: '13px' }}>事件流</span>
                  <span className="font-mono" style={{ fontSize: '11px', color: 'var(--color-text-tertiary)' }}>
                    {cursor + 1} / {events.length}
                  </span>
                </div>
                <div ref={eventListRef} style={{ flex: 1, overflow: 'auto', padding: '4px' }}>
                  {events.map((e, i) => {
                    const isCurrent = i === cursor;
                    const isPast = i < cursor;
                    const color = EVENT_TYPE_COLORS[e.type] || '#6B7280';
                    return (
                      <div
                        key={i}
                        data-event-idx={i}
                        onClick={() => handleJumpTo(i)}
                        style={{
                          padding: '6px 10px',
                          margin: '2px 0',
                          borderRadius: '4px',
                          cursor: 'pointer',
                          fontSize: '11px',
                          borderLeft: `3px solid ${color}`,
                          background: isCurrent ? `${color}22` : isPast ? 'var(--color-bg-secondary)' : 'transparent',
                          opacity: isPast ? 0.6 : 1,
                          display: 'flex',
                          alignItems: 'center',
                          gap: '6px',
                        }}
                      >
                        <span className="font-mono" style={{ color: 'var(--color-text-tertiary)', minWidth: '28px' }}>
                          {e.sequence ?? i}
                        </span>
                        <span style={{ fontWeight: isCurrent ? 600 : 400, color: isCurrent ? color : 'var(--color-text-primary)', minWidth: '70px' }}>
                          {EVENT_TYPE_LABELS[e.type] || e.type}
                        </span>
                        {e.node_id && (
                          <span className="font-mono" style={{ color: 'var(--color-primary-soft)', fontSize: '10px' }}>
                            {e.node_id}
                          </span>
                        )}
                        {e.occurred_at && (
                          <span className="font-mono" style={{ color: 'var(--color-text-tertiary)', fontSize: '10px', marginLeft: 'auto' }}>
                            {new Date(e.occurred_at).toLocaleTimeString('zh-CN', { hour12: false })}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>

          {/* 底部：时间轴滑块 */}
          <div className="card" style={{ padding: '12px 16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
              <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--color-text-primary)' }}>时间轴回放</span>
              <span className="font-mono" style={{ fontSize: '11px', color: 'var(--color-text-tertiary)' }}>
                事件 #{currentEvent?.sequence ?? 0} / #{events[events.length - 1]?.sequence ?? 0}
              </span>
              {currentEvent?.occurred_at && (
                <span className="font-mono" style={{ fontSize: '11px', color: 'var(--color-text-tertiary)' }}>
                  · {new Date(currentEvent.occurred_at).toLocaleString('zh-CN', { hour12: false })}
                </span>
              )}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span className="font-mono" style={{ fontSize: '10px', color: 'var(--color-text-tertiary)', minWidth: '40px' }}>开始</span>
              <input
                type="range"
                min={0}
                max={events.length - 1}
                value={cursor}
                onChange={(e) => handleJumpTo(Number(e.target.value))}
                style={{ flex: 1, accentColor: 'var(--color-primary)' }}
              />
              <span className="font-mono" style={{ fontSize: '10px', color: 'var(--color-text-tertiary)', minWidth: '40px', textAlign: 'right' }}>结束</span>
            </div>
            {/* 时间轴上的事件类型标记 */}
            <div style={{ display: 'flex', marginTop: '6px', height: '20px', position: 'relative' }}>
              {events.map((e, i) => {
                const color = EVENT_TYPE_COLORS[e.type] || '#6B7280';
                const pct = events.length > 1 ? (i / (events.length - 1)) * 100 : 0;
                return (
                  <div
                    key={i}
                    onClick={() => handleJumpTo(i)}
                    title={`#${e.sequence ?? i} ${EVENT_TYPE_LABELS[e.type] || e.type}${e.node_id ? ' · ' + e.node_id : ''}`}
                    style={{
                      position: 'absolute',
                      left: `${pct}%`,
                      width: '3px',
                      height: i === cursor ? '20px' : '12px',
                      background: color,
                      borderRadius: '2px',
                      cursor: 'pointer',
                      transform: 'translateX(-50%)',
                      transition: 'height 0.15s',
                      top: i === cursor ? 0 : '4px',
                    }}
                  />
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
