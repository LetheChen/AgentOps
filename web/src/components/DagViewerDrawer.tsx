import { useState, useEffect, useRef, useCallback } from 'react';
import { ReactFlow, Background, Controls, MiniMap, type Node, type Edge } from 'reactflow';
import 'reactflow/dist/style.css';
import { DagNode, type DagNodeData } from './dag/DagNode';
import { AnimatedEdge, type AnimatedEdgeData } from './dag/AnimatedEdge';
import { apiClient } from '../lib/api';
import type { DagEvent, CollaborationGraph } from '../lib/types';
import DagCanvas, { type CanvasMode } from './DagCanvas';

/**
 * DagViewerDrawer — DAG 查看器抽屉。
 *
 * 功能：
 *   - 右侧滑出抽屉（60vw，最大 900px）
 *   - ReactFlow 显示 DAG 拓扑（复用 DagNode + AnimatedEdge）
 *   - 节点点击显示详情面板（状态/Provider/Token/耗时/error_type）
 *   - 底部"重跑此节点"和"重跑并级联下游"按钮
 *
 * 样式前缀：`dvd-`
 */

// 模块级常量（ReactFlow 要求 nodeTypes/edgeTypes 为模块级常量）
const nodeTypes = { dag: DagNode };
const edgeTypes = { animated: AnimatedEdge };

const LEVEL_GAP = 280;
const INDEX_GAP = 120;

const STATUS_COLORS: Record<string, string> = {
  completed: '#10B981',
  running: '#3B82F6',
  pending: '#475569',
  ready: '#FBBF24',
  waiting: '#A78BFA',
  failed: '#EF4444',
  skipped: '#6B7280',
};

const STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  ready: '就绪',
  waiting: '等待依赖',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
  idle: '空闲',
  active: '活跃',
  dormant: '休眠',
  cancelled: '已取消',
};

interface DagViewerDrawerProps {
  open: boolean;
  onClose: () => void;
  runId: string;
  /**
   * 父组件传入的实时事件回调注册器（可选）。
   *
   * 父组件（RunMonitorPage）已持有活动 run 的 EventSource（来自 openEventStream）。
   * 抽屉不要自己再开一个 SSE 连接——后端 `_event_streams[run_id]` 是单队列单消费者，
   * 两个 EventSource 会互相争抢事件导致双方都只收到部分。
   * 父组件在抽屉打开时注册回调（每收到一条 SSE 事件调用 `cb`），关闭时注销。
   * 抽屉内部按 sequence 去重保证幂等。
   */
  onLiveEventSubscribe?: (cb: (ev: DagEvent) => void) => () => void;
}

interface NodeInfo {
  id: string;
  label: string;
  x: number;
  y: number;
}

interface NodeDetail {
  provider?: string;
  errorType?: string;
  tokensIn?: number;
  tokensOut?: number;
  toolCalls?: number;
  startedAt?: string;
  finishedAt?: string;
  status?: string;
}

interface DagViewerDrawerState {
  workflowId: string;
  runInputs: Record<string, unknown>;
}

/** 格式化时长（start → end 固定时长） */
function formatDuration(start: number, end: number): string {
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export function DagViewerDrawer({ open, onClose, runId, onLiveEventSubscribe }: DagViewerDrawerProps) {
  const [nodeStates, setNodeStates] = useState<Record<string, string>>({});
  const [nodeList, setNodeList] = useState<NodeInfo[]>([]);
  const [edges, setEdges] = useState<{ source: string; target: string; port?: string }[]>([]);
  const [nodeDetails, setNodeDetails] = useState<Record<string, NodeDetail>>({});
  const [events, setEvents] = useState<DagEvent[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reexecuting, setReexecuting] = useState(false);
  const [runMeta, setRunMeta] = useState<DagViewerDrawerState | null>(null);
  // run 是否已终止（completed/failed/cancelled）：决定是否继续轮询实时事件
  const [runTerminal, setRunTerminal] = useState(false);
  // ===== M2 新增：协作可视化 =====
  const [graphData, setGraphData] = useState<CollaborationGraph | null>(null);
  const [timelineData, setTimelineData] = useState<{ sequence: number; occurred_at?: string | null; type: string; node_id?: string | null; label: string; payload_size?: number | null }[]>([]);
  const [canvasMode, setCanvasMode] = useState<CanvasMode>('dag');
  // === /M2 新增 ===
  // StrictMode 双调用保护：记录本次挂载已处理的 key
  const initKeyRef = useRef<string | null>(null);
  // 事件去重：sequence-based
  const processedSeqRef = useRef<Set<string>>(new Set());

  /** 处理单条 DAG 事件（更新节点状态/列表/边/详情） */
  const handleDagEvent = useCallback((dagEvent: DagEvent) => {
    const seqKey =
      dagEvent.sequence != null
        ? `${dagEvent.type}:${dagEvent.node_id ?? ''}:${dagEvent.sequence}`
        : null;
    if (seqKey) {
      if (processedSeqRef.current.has(seqKey)) return;
      processedSeqRef.current.add(seqKey);
    }

    setEvents((prev) => [...prev, dagEvent]);

    // 检测 run 终止事件 → 停止实时轮询
    if (
      dagEvent.type === 'run.completed' ||
      dagEvent.type === 'run.failed' ||
      dagEvent.type === 'run.cancelled'
    ) {
      setRunTerminal(true);
    }

    if (dagEvent.node_id) {
      const nodeId = dagEvent.node_id;
      if (dagEvent.type === 'node.started' || dagEvent.type === 'node_started' || dagEvent.type === 'node.progress' || dagEvent.type === 'node_running') {
        setNodeStates((prev) => ({ ...prev, [nodeId]: 'running' }));
        setNodeDetails((prev) => ({ ...prev, [nodeId]: { ...prev[nodeId], startedAt: dagEvent.occurred_at, status: 'running' } }));
      } else if (dagEvent.type === 'node.completed' || dagEvent.type === 'node_completed' || dagEvent.type === 'node_done') {
        setNodeStates((prev) => ({ ...prev, [nodeId]: 'completed' }));
        setNodeDetails((prev) => ({ ...prev, [nodeId]: { ...prev[nodeId], finishedAt: dagEvent.occurred_at, status: 'completed' } }));
      } else if (dagEvent.type === 'node.failed' || dagEvent.type === 'node_failed' || dagEvent.type === 'node_error') {
        setNodeStates((prev) => ({ ...prev, [nodeId]: 'failed' }));
        setNodeDetails((prev) => ({
          ...prev,
          [nodeId]: {
            ...prev[nodeId],
            provider: dagEvent.payload?.provider_id || prev[nodeId]?.provider,
            errorType: dagEvent.payload?.error_type || prev[nodeId]?.errorType,
            status: 'failed',
            finishedAt: dagEvent.occurred_at,
          },
        }));
      } else if (dagEvent.type === 'node.ready' || dagEvent.type === 'node_ready') {
        setNodeStates((prev) => ({ ...prev, [nodeId]: 'ready' }));
      } else if (dagEvent.type === 'node.skipped' || dagEvent.type === 'node_skipped') {
        setNodeStates((prev) => ({ ...prev, [nodeId]: 'skipped' }));
      }

      setNodeList((prev) => {
        if (prev.find((n) => n.id === nodeId)) return prev;
        return [...prev, { id: nodeId, label: nodeId, x: 0, y: 0 }];
      });
    }

    if (dagEvent.payload?.edges) {
      setEdges(dagEvent.payload.edges as Array<{ source: string; target: string; port?: string }>);
    }

    if (dagEvent.payload?.layout?.nodes) {
      const layoutNodes = dagEvent.payload.layout.nodes as Array<{ id: string; level: number; index: number }>;
      setNodeList(layoutNodes.map((n) => ({ id: n.id, label: n.id, x: n.level * LEVEL_GAP, y: n.index * INDEX_GAP })));
    } else if (dagEvent.payload?.nodes) {
      setNodeList(
        (dagEvent.payload.nodes as Array<{ id: string; label?: string; x?: number; y?: number }>).map((n) => ({
          id: n.id,
          label: n.label || n.id,
          x: n.x || 0,
          y: n.y || 0,
        })),
      );
    }
  }, []);

  // 抽屉打开时加载 run 事件
  useEffect(() => {
    if (!open || !runId) return;
    const key = `dvd:${runId}`;
    // StrictMode 双调用保护：第二次直接跳过
    if (initKeyRef.current === key) return;
    initKeyRef.current = key;

    // 重置状态
    processedSeqRef.current = new Set();
    setNodeStates({});
    setNodeList([]);
    setEdges([]);
    setNodeDetails({});
    setEvents([]);
    setSelectedNodeId(null);
    setError(null);
    setRunTerminal(false);
    setGraphData(null);  // M2：重置协作图数据
    setTimelineData([]);  // M2：重置时间线
    setLoading(true);

    // M2：拉取协作图数据（业务角色 + handoff + lanes）
    apiClient.getCollaborationGraph(runId)
      .then((g) => setGraphData(g))
      .catch((err) => console.warn('Failed to fetch collaboration graph:', err));
    // M2：拉取时间线数据
    apiClient.getCollaborationTimeline(runId)
      .then((resp) => setTimelineData(resp.timeline || []))
      .catch((err) => console.warn('Failed to fetch timeline:', err));

    (async () => {
      // 1. 获取 run summary（恢复 workflow_id / inputs / 节点初始状态）
      try {
        const summary = await apiClient.auditGetRunSummary(runId);
        const wfId = String(summary.workflow_id || '');
        const inputs = (summary.inputs as Record<string, unknown>) || {};
        setRunMeta({ workflowId: wfId, runInputs: inputs });
        // 已终止的 run 不再轮询实时事件
        if (['completed', 'failed', 'cancelled'].includes(String(summary.status))) {
          setRunTerminal(true);
        }
        if (summary.node_states && typeof summary.node_states === 'object') {
          setNodeStates(summary.node_states as Record<string, string>);
        }
      } catch (err) {
        console.error('Failed to load run summary:', err);
      }

      // 2. 获取事件并回放
      try {
        const resp = await apiClient.auditGetRunEvents(runId);
        const historyEvents = resp.events as unknown as DagEvent[];
        for (const ev of historyEvents) {
          handleDagEvent(ev);
        }
      } catch (err) {
        console.error('Failed to load run events:', err);
        setError(err instanceof Error ? err.message : '加载事件失败');
      } finally {
        setLoading(false);
      }
    })();
  }, [open, runId, handleDagEvent]);

  // 选中节点时获取详细详情（补充事件回放未覆盖的字段）
  useEffect(() => {
    if (!selectedNodeId || !runId) return;
    let cancelled = false;
    apiClient
      .auditGetNodeDetail(runId, selectedNodeId)
      .then((detail) => {
        if (cancelled || !selectedNodeId) return;
        setNodeDetails((prev) => ({
          ...prev,
          [selectedNodeId]: {
            ...prev[selectedNodeId],
            startedAt: (detail.started_at as string) || prev[selectedNodeId]?.startedAt,
            finishedAt: (detail.finished_at as string) || prev[selectedNodeId]?.finishedAt,
            tokensIn: (detail.tokens_input as number) ?? prev[selectedNodeId]?.tokensIn,
            tokensOut: (detail.tokens_output as number) ?? prev[selectedNodeId]?.tokensOut,
            provider: (detail.provider_id as string) || prev[selectedNodeId]?.provider,
            errorType: (detail.error_type as string) || prev[selectedNodeId]?.errorType,
          },
        }));
      })
      .catch(() => {
        // 静默忽略：已有事件回放数据可用
      });
    return () => {
      cancelled = true;
    };
  }, [selectedNodeId, runId]);

  // 实时事件订阅：抽屉打开时通过父组件传入的回调注册器，把每条 SSE 事件喂给 handleDagEvent。
  // 不自己开 EventSource、不轮询 audit 端点（运行中 audit SQLite 写锁竞争会卡死）。
  // 父组件的 handleSSEMessage 已经处理了同一条事件，handleDagEvent 的 sequence 去重保证幂等。
  useEffect(() => {
    if (!open || !runId || !onLiveEventSubscribe) return;
    const unsubscribe = onLiveEventSubscribe((ev) => handleDagEvent(ev));
    return () => {
      try { unsubscribe(); } catch { /* 父组件可能已卸载 */ }
    };
  }, [open, runId, onLiveEventSubscribe, handleDagEvent]);

  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, onClose]);

  /** 重新执行节点 */
  const handleReexecute = useCallback(
    async (nodeId: string, cascade: boolean) => {
      if (!runMeta?.workflowId) {
        setError('无法重新执行：缺少 workflow_id（可能为对话模式会话）');
        return;
      }
      setReexecuting(true);
      // 重置节点状态为 pending
      if (cascade) {
        // BFS 找下游
        const downstream = new Set<string>();
        const queue = [nodeId];
        const adjacency = new Map<string, string[]>();
        for (const e of edges) {
          const list = adjacency.get(e.source) || [];
          list.push(e.target);
          adjacency.set(e.source, list);
        }
        while (queue.length > 0) {
          const n = queue.shift()!;
          for (const next of adjacency.get(n) || []) {
            if (!downstream.has(next)) {
              downstream.add(next);
              queue.push(next);
            }
          }
        }
        const allNodes = new Set([nodeId, ...downstream]);
        setNodeStates((prev) => {
          const updated = { ...prev };
          for (const n of allNodes) updated[n] = 'pending';
          return updated;
        });
      } else {
        setNodeStates((prev) => ({ ...prev, [nodeId]: 'pending' }));
      }

      try {
        // 调用 resumeRun（返回新 run_id，但事件流仍在原 run_id 下追加）
        await apiClient.resumeRun(runId, runMeta.workflowId, runMeta.runInputs, nodeId);
        // 刷新事件流以查看新状态
        const resp = await apiClient.auditGetRunEvents(runId);
        const fresh = resp.events as unknown as DagEvent[];
        // 重置去重集，重放所有事件
        processedSeqRef.current = new Set();
        setNodeStates({});
        setNodeList([]);
        setEdges([]);
        setNodeDetails({});
        setEvents([]);
        for (const ev of fresh) {
          handleDagEvent(ev);
        }
      } catch (err) {
        console.error('Failed to re-execute:', err);
        setError(err instanceof Error ? err.message : '重新执行失败');
      } finally {
        setReexecuting(false);
      }
    },
    [runId, runMeta, edges, handleDagEvent],
  );

  // 构建 ReactFlow 节点 / 边
  const rfNodes: Node<DagNodeData>[] = nodeList.map((n) => {
    const status = nodeStates[n.id] || 'pending';
    const detail = nodeDetails[n.id] || {};
    return {
      id: n.id,
      type: 'dag',
      position: { x: n.x, y: n.y },
      data: {
        label: n.label,
        status,
        subtitle: n.id !== n.label ? n.id : undefined,
        tokensIn: detail.tokensIn,
        tokensOut: detail.tokensOut,
        toolCalls: detail.toolCalls,
        provider: detail.provider,
        errorType: detail.errorType,
      },
    };
  });

  const rfEdges: Edge<AnimatedEdgeData>[] = edges.map((e) => ({
    // 边 id 加 port 区分，避免同一对节点多条 port 共享 key（log-patrol report→notify 有 2 个 port）
    id: `${e.source}-${e.target}-${e.port || 'default'}`,
    source: e.source,
    target: e.target,
    type: 'animated',
    data: { targetStatus: nodeStates[e.target] || 'pending' },
  }));

  const selectedDetail = selectedNodeId ? nodeDetails[selectedNodeId] : null;
  const selectedStatus = selectedNodeId ? nodeStates[selectedNodeId] || 'pending' : '';
  // M2：从 graphData 提取选中节点的业务角色 + harness + model
  const selectedGraphNode = graphData?.nodes.find((n) => n.node_id === selectedNodeId);
  const selectedBusinessRole = selectedGraphNode?.business_role;
  const selectedAgent = selectedGraphNode?.agent_id;
  const selectedHarness = selectedGraphNode?.harness;
  const selectedModel = selectedGraphNode?.model;
  // M2：从 graphData 提取该节点所有出方向 handoff
  const selectedHandoffs = graphData?.handoffs.filter((h) => h.from_node === selectedNodeId) || [];

  if (!open) return null;

  return (
    <div className="dvd-overlay" onClick={onClose}>
      <div className="dvd-drawer" onClick={(e) => e.stopPropagation()}>
        {/* ── 头部 ── */}
        <div className="dvd-header">
          <div className="dvd-title">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="3" width="6" height="6" rx="1" />
              <rect x="15" y="3" width="6" height="6" rx="1" />
              <rect x="9" y="15" width="6" height="6" rx="1" />
              <path d="M6 9v3a2 2 0 0 0 2 2h4" />
              <path d="M18 9v3a2 2 0 0 1-2 2h-4" />
            </svg>
            DAG 执行拓扑
            <span className="dvd-runid font-mono">{runId.slice(0, 16)}...</span>
          </div>
          {/* M2：3 视图切换 tab */}
          <div className="dvd-view-toggles" style={{ display: 'flex', gap: '4px' }}>
            <button
              className={`dvd-view-tab ${canvasMode === 'dag' ? 'active' : ''}`}
              onClick={() => setCanvasMode('dag')}
              title="DAG 拓扑视图（开发者视角）"
              style={{
                background: canvasMode === 'dag' ? 'rgba(59,130,246,.15)' : 'transparent',
                border: `1px solid ${canvasMode === 'dag' ? '#3b82f6' : 'var(--border)'}`,
                color: canvasMode === 'dag' ? '#3b82f6' : 'var(--color-text-secondary)',
                padding: '4px 10px', borderRadius: '4px', fontSize: '11px', cursor: 'pointer',
              }}
            >
              📐 DAG 拓扑
            </button>
            <button
              className={`dvd-view-tab ${canvasMode === 'lane' ? 'active' : ''}`}
              onClick={() => setCanvasMode('lane')}
              title="业务角色泳道视图（业务视角）"
              style={{
                background: canvasMode === 'lane' ? 'rgba(168,85,247,.15)' : 'transparent',
                border: `1px solid ${canvasMode === 'lane' ? '#a855f7' : 'var(--border)'}`,
                color: canvasMode === 'lane' ? '#a855f7' : 'var(--color-text-secondary)',
                padding: '4px 10px', borderRadius: '4px', fontSize: '11px', cursor: 'pointer',
              }}
            >
              🎯 业务泳道
            </button>
            <button
              className={`dvd-view-tab ${canvasMode === 'timeline' ? 'active' : ''}`}
              onClick={() => setCanvasMode('timeline')}
              title="时间线视图（开发者精细时序分析）"
              style={{
                background: canvasMode === 'timeline' ? 'rgba(16,185,129,.15)' : 'transparent',
                border: `1px solid ${canvasMode === 'timeline' ? '#10b981' : 'var(--border)'}`,
                color: canvasMode === 'timeline' ? '#10b981' : 'var(--color-text-secondary)',
                padding: '4px 10px', borderRadius: '4px', fontSize: '11px', cursor: 'pointer',
              }}
            >
              📊 时间线
            </button>
          </div>
          <button className="dvd-close" onClick={onClose} title="关闭 (Esc)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* ── 内容区：ReactFlow 拓扑 ── */}
        <div className="dvd-content">
          {loading && (
            <div className="dvd-empty">正在加载 DAG 事件...</div>
          )}
          {!loading && error && (
            <div className="dvd-empty dvd-empty-error">加载失败：{error}</div>
          )}
          {!loading && !error && rfNodes.length === 0 && (
            <div className="dvd-empty">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity="0.3" style={{ marginBottom: '8px' }}>
                <rect x="3" y="3" width="6" height="6" rx="1" />
                <rect x="15" y="3" width="6" height="6" rx="1" />
                <rect x="9" y="15" width="6" height="6" rx="1" />
                <path d="M6 9v3a2 2 0 0 0 2 2h4" />
                <path d="M18 9v3a2 2 0 0 1-2 2h-4" />
              </svg>
              <strong>该会话无 DAG 节点</strong>
              <span>对话模式会话通常不产生 DAG 拓扑</span>
            </div>
          )}
          {!loading && !error && (graphData || rfNodes.length > 0) && (
            <>
              {/* M2 优先级：有 graphData + 非简单 dag 模式 → 走新 DagCanvas（含业务角色 / 时间线） */}
              {graphData && canvasMode !== 'simple' ? (
                <DagCanvas
                  mode={canvasMode}
                  graphNodes={graphData.nodes}
                  graphEdges={graphData.edges}
                  handoffs={graphData.handoffs}
                  lanes={graphData.lanes}
                  timeline={timelineData}
                  runStartTime={graphData.started_at ? new Date(graphData.started_at).getTime() : undefined}
                  runDuration={graphData.started_at && graphData.finished_at
                    ? Math.max(60000, new Date(graphData.finished_at).getTime() - new Date(graphData.started_at).getTime())
                    : undefined}
                  onNodeClick={(id) => setSelectedNodeId(id)}
                />
              ) : (
                // fallback：无 graphData → 老 ReactFlow（向后兼容）
                <ReactFlow
                  nodes={rfNodes}
                  edges={rfEdges}
                  nodeTypes={nodeTypes}
                  edgeTypes={edgeTypes}
                  fitView
                  fitViewOptions={{ padding: 0.2 }}
                  proOptions={{ hideAttribution: true }}
                  nodesDraggable
                  onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                >
                  <Background color="#1E293B" gap={20} />
                  <Controls showInteractive={false} />
                  <MiniMap
                    nodeColor={(n) => STATUS_COLORS[nodeStates[n.id] || 'pending'] || '#475569'}
                    nodeStrokeColor={(n) => STATUS_COLORS[nodeStates[n.id] || 'pending'] || '#475569'}
                    nodeBorderRadius={4}
                    maskColor="rgba(11, 15, 20, 0.42)"
                    maskStrokeColor="rgba(99, 102, 241, 0.8)"
                    maskStrokeWidth={1.5}
                  />
                </ReactFlow>
              )}
            </>
          )}
        </div>

        {/* ── 节点详情面板（点击节点后展开） ── */}
        {selectedNodeId && (
          <div className="dvd-detail">
            <div className="dvd-detail-header">
              <span className="dvd-detail-title">
                节点详情: {selectedBusinessRole ? `${selectedBusinessRole} · ` : ''}{selectedNodeId}
              </span>
              <button
                className="dvd-detail-close"
                onClick={() => setSelectedNodeId(null)}
                title="关闭"
              >
                ×
              </button>
            </div>
            {/* M2：业务角色 + harness + model（来自 /collaboration-graph） */}
            {selectedGraphNode && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', padding: '0 12px 8px', borderBottom: '1px solid var(--border)', marginBottom: '8px' }}>
                {selectedBusinessRole && (
                  <span style={{ fontSize: '11px', padding: '2px 8px', borderRadius: '4px', background: 'rgba(168,85,247,.15)', color: '#a855f7', border: '1px solid rgba(168,85,247,.4)' }}>
                    🎭 {selectedBusinessRole}
                  </span>
                )}
                {selectedAgent && (
                  <span style={{ fontSize: '11px', padding: '2px 8px', borderRadius: '4px', background: 'rgba(59,130,246,.12)', color: '#3b82f6', border: '1px solid rgba(59,130,246,.4)', fontFamily: 'monospace' }}>
                    agent: {selectedAgent}
                  </span>
                )}
                {selectedHarness && (
                  <span style={{ fontSize: '11px', padding: '2px 8px', borderRadius: '4px', background: 'rgba(16,185,129,.12)', color: '#10b981', border: '1px solid rgba(16,185,129,.4)' }}>
                    ⚙️ {selectedHarness}
                  </span>
                )}
                {selectedModel && (
                  <span style={{ fontSize: '11px', padding: '2px 8px', borderRadius: '4px', background: 'rgba(251,191,36,.12)', color: '#fbbf24', border: '1px solid rgba(251,191,36,.4)', fontFamily: 'monospace' }}>
                    🤖 {selectedModel}
                  </span>
                )}
              </div>
            )}
            {/* M2：该节点的出方向 handoff 列表 */}
            {selectedHandoffs.length > 0 && (
              <div style={{ padding: '0 12px 8px' }}>
                <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginBottom: '6px' }}>
                  💬 Handoff（节点传递给下游）：
                </div>
                {selectedHandoffs.map((h, i) => (
                  <div
                    key={i}
                    style={{
                      background: 'linear-gradient(135deg, rgba(99,102,241,.10), rgba(168,85,247,.10))',
                      border: '1px solid rgba(168,85,247,.3)',
                      borderRadius: '6px',
                      padding: '6px 10px',
                      marginBottom: '4px',
                      fontSize: '11px',
                    }}
                  >
                    <div style={{ color: '#c4b5fd', fontSize: '10px', marginBottom: '2px' }}>
                      {h.from_role || '?'} → {h.to_role || '?'}
                    </div>
                    <div style={{ color: 'var(--color-text-primary)', lineHeight: 1.4 }}>
                      {h.summary || `${h.port || ''} (${(h.payload_size || 0)}B)`}
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div className="dvd-detail-grid">
              <div className="dvd-detail-cell">
                <span className="dvd-detail-label">状态</span>
                <span
                  className="dvd-detail-value"
                  style={{ color: STATUS_COLORS[selectedStatus] || 'var(--color-text-secondary)', fontWeight: 500 }}
                >
                  {STATUS_LABELS[selectedStatus] || selectedStatus}
                </span>
              </div>
              {selectedDetail?.provider && (
                <div className="dvd-detail-cell">
                  <span className="dvd-detail-label">Provider</span>
                  <span className="dvd-detail-value font-mono">{selectedDetail.provider}</span>
                </div>
              )}
              {selectedDetail?.tokensIn != null && (
                <div className="dvd-detail-cell">
                  <span className="dvd-detail-label">输入</span>
                  <span className="dvd-detail-value font-mono">{selectedDetail.tokensIn.toLocaleString()} tok</span>
                </div>
              )}
              {selectedDetail?.tokensOut != null && (
                <div className="dvd-detail-cell">
                  <span className="dvd-detail-label">输出</span>
                  <span className="dvd-detail-value font-mono">{selectedDetail.tokensOut.toLocaleString()} tok</span>
                </div>
              )}
              {selectedDetail?.startedAt && (
                <div className="dvd-detail-cell">
                  <span className="dvd-detail-label">开始</span>
                  <span className="dvd-detail-value font-mono">
                    {new Date(selectedDetail.startedAt).toLocaleTimeString('zh-CN', { hour12: false })}
                  </span>
                </div>
              )}
              {selectedDetail?.finishedAt && (
                <div className="dvd-detail-cell">
                  <span className="dvd-detail-label">结束</span>
                  <span className="dvd-detail-value font-mono">
                    {new Date(selectedDetail.finishedAt).toLocaleTimeString('zh-CN', { hour12: false })}
                  </span>
                </div>
              )}
              {selectedDetail?.startedAt && selectedDetail?.finishedAt && (
                <div className="dvd-detail-cell">
                  <span className="dvd-detail-label">耗时</span>
                  <span className="dvd-detail-value font-mono">
                    {formatDuration(new Date(selectedDetail.startedAt).getTime(), new Date(selectedDetail.finishedAt).getTime())}
                  </span>
                </div>
              )}
              {selectedDetail?.errorType && (
                <div className="dvd-detail-cell dvd-detail-cell-full">
                  <span className="dvd-detail-label">错误类型</span>
                  <span className="dvd-detail-value font-mono" style={{ color: 'var(--state-error)' }}>
                    {selectedDetail.errorType}
                  </span>
                </div>
              )}
            </div>
            <div className="dvd-detail-actions">
              <button
                className="btn-primary btn-sm"
                onClick={() => handleReexecute(selectedNodeId, false)}
                disabled={reexecuting || !runMeta?.workflowId}
                title={!runMeta?.workflowId ? '对话模式会话不支持节点重执行' : '仅重新执行此节点'}
              >
                {reexecuting ? '执行中...' : '重跑此节点'}
              </button>
              <button
                className="btn-secondary btn-sm"
                onClick={() => handleReexecute(selectedNodeId, true)}
                disabled={reexecuting || !runMeta?.workflowId}
                title={!runMeta?.workflowId ? '对话模式会话不支持节点重执行' : '重新执行此节点并级联所有下游节点'}
              >
                重跑并级联下游
              </button>
            </div>
            {!runMeta?.workflowId && (
              <div className="dvd-detail-warn">⚠️ 对话模式会话无 workflow_id，无法重执行节点</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
