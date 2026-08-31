/**
 * DagCanvas 三种布局算法（mode=dag / mode=lane / mode=timeline）
 *
 * 设计原则：
 * - mode=dag: BFS 分层布局（从 AuditReplayPage autoLayoutNodes 搬迁，零改动）
 * - mode=lane: 按 business_role 分泳道 + started_at 时间定位
 * - mode=timeline: 按 node_id 分行，节点条 = (started_at, duration)
 */
import type { Node, Edge } from 'reactflow';
import type { DagNodeData } from './DagNode';
import type { TimelineNodeData } from './TimelineNode';
import type { GraphNode, GraphEdge, HandoffInfo, LaneInfo, TimelineEntry } from '../../lib/types';
import { TIMELINE_CONSTANTS } from './TimelineNode';

// DAG 拓扑布局常量
const LEVEL_GAP = 280;
const INDEX_GAP = 120;

// 业务泳道布局常量
const LANE_HEIGHT = 100;
const LANE_LEFT_PAD = 156;

// 时间线布局常量
const TL_CANVAS_W = 1200;

// ===== mode=dag：BFS 分层布局 =====

export function buildDagLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  _handoffs: HandoffInfo[] | undefined,
): { rfNodes: Node<DagNodeData>[]; rfEdges: Edge[] } {
  // 1. BFS 分层
  const positions = bfsAutoLayout(nodes, edges);

  // 2. 构建 ReactFlow 节点
  const rfNodes: Node<DagNodeData>[] = nodes.map((n) => ({
    id: n.node_id,
    type: 'dag',
    position: positions.get(n.node_id) ?? { x: 80, y: 60 },
    data: {
      label: n.display_name ?? n.node_id,
      subtitle: `${n.agent_id} · ${n.harness}`,
      status: n.status,
      tokensIn: n.token_usage ? Math.floor(n.token_usage * 0.4) : undefined,
      tokensOut: n.token_usage ? Math.floor(n.token_usage * 0.6) : undefined,
      provider: n.model?.split('/')[0],
      errorType: n.error ?? undefined,
    },
  }));

  // 3. 构建 ReactFlow 边
  // 边 id 加 port 区分，避免同一对节点多条 port 共享 key（log-patrol report→notify 有 2 个 port）
  const rfEdges: Edge[] = edges.map((e) => ({
    id: `${e.from}-${e.to}-${e.port || 'default'}`,
    source: e.from,
    target: e.to,
    type: 'animated',
    data: { targetStatus: nodes.find((n) => n.node_id === e.to)?.status ?? 'pending' },
  }));

  return { rfNodes, rfEdges };
}

/** BFS 分层布局（从 AuditReplayPage.tsx:87-150 搬迁） */
function bfsAutoLayout(
  graphNodes: GraphNode[],
  graphEdges: GraphEdge[],
): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const ids = graphNodes.map((n) => n.node_id);
  if (ids.length === 0) return positions;

  // 构建邻接表（正向）
  const outAdj: Record<string, string[]> = {};
  for (const n of ids) outAdj[n] = [];
  for (const e of graphEdges) {
    if (outAdj[e.from]) outAdj[e.from].push(e.to);
  }

  // 找入度=0 的根节点
  const inDegree: Record<string, number> = {};
  for (const n of ids) inDegree[n] = 0;
  for (const e of graphEdges) {
    if (e.from in inDegree) inDegree[e.to] = (inDegree[e.to] ?? 0) + 1;
  }
  const roots = ids.filter((id) => (inDegree[id] ?? 0) === 0);

  // BFS 分层
  const levels: Record<string, number> = {};
  const queue: string[] = roots.length > 0 ? [...roots] : ids.slice(0, 1);
  for (const r of queue) levels[r] = 0;

  while (queue.length > 0) {
    const cur = queue.shift()!;
    const lvl = levels[cur] ?? 0;
    for (const next of outAdj[cur] || []) {
      if (levels[next] === undefined || levels[next] < lvl + 1) {
        levels[next] = lvl + 1;
        queue.push(next);
      }
    }
  }
  // 未访问节点兜底放第 0 层
  for (const id of ids) {
    if (levels[id] === undefined) levels[id] = 0;
  }

  // 按层分组
  const byLevel: Record<number, string[]> = {};
  for (const id of ids) {
    const lvl = levels[id] ?? 0;
    (byLevel[lvl] ??= []).push(id);
  }

  // 计算位置
  for (const lvlStr of Object.keys(byLevel)) {
    const lvl = Number(lvlStr);
    const list = byLevel[lvl];
    list.forEach((id, idx) => {
      positions.set(id, { x: 120 + lvl * LEVEL_GAP, y: 80 + idx * INDEX_GAP });
    });
  }

  return positions;
}

// ===== mode=lane：业务角色泳道布局 =====

export function buildLaneLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  handoffs: HandoffInfo[] | undefined,
  lanes: LaneInfo[],
): { rfNodes: Node<DagNodeData>[]; rfEdges: Edge[] } {
  // lane 模式复用 dag 节点渲染（业务视角）
  const { rfNodes: dagNodes, rfEdges: dagEdges } = buildDagLayout(nodes, edges, handoffs);

  // 调整节点位置：按 (lane_index, started_at 时间偏移)
  const nodeIndex = new Map(nodes.map((n) => [n.node_id, n]));
  const laneIndexByNode = new Map<string, number>();
  lanes.forEach((lane, idx) => {
    for (const nodeId of lane.nodes) laneIndexByNode.set(nodeId, idx);
  });

  // 计算时间偏移（基于最早 started_at）
  const startTimes = nodes
    .map((n) => (n as any).started_at as string | undefined)
    .filter((s): s is string => !!s)
    .map((s) => new Date(s).getTime());
  const runStart = startTimes.length > 0 ? Math.min(...startTimes) : Date.now();
  const runEnd = startTimes.length > 0 ? Math.max(...startTimes) : runStart + 60000;

  const laneNodes: Node<DagNodeData>[] = dagNodes.map((n) => {
    const gn = nodeIndex.get(n.id);
    const laneIdx = laneIndexByNode.get(n.id) ?? 0;
    const startedAt = gn ? (gn as any).started_at : undefined;
    const completedAt = gn ? (gn as any).completed_at : undefined;
    const xRatio = startedAt
      ? Math.max(0, Math.min(1, (new Date(startedAt).getTime() - runStart) / (runEnd - runStart || 1)))
      : 0.1;
    const widthRatio = completedAt
      ? Math.max(0.05, Math.min(1 - xRatio, (new Date(completedAt).getTime() - new Date(startedAt).getTime()) / (runEnd - runStart || 1)))
      : 0.1;
    return {
      ...n,
      position: {
        x: LANE_LEFT_PAD + xRatio * TL_CANVAS_W,
        y: 40 + laneIdx * LANE_HEIGHT,
      },
      data: { ...n.data, subtitle: gn?.business_role },
    };
  });

  return { rfNodes: laneNodes, rfEdges: dagEdges };
}

// ===== mode=timeline：按 node_id 分行 =====

export function buildTimelineLayout(
  nodes: GraphNode[],
  timeline: TimelineEntry[] | undefined,
  runStart?: number,
  runDur?: number,
): { rfNodes: Node<TimelineNodeData>[]; rfEdges: Edge[] } {
  const start = runStart ?? Date.now() - 60000;
  const dur = runDur ?? 60000;

  const rfNodes: Node<TimelineNodeData>[] = nodes.map((n, idx) => {
    const nodeStart = (n as any).started_at ? new Date((n as any).started_at as string).getTime() : start;
    const nodeEnd = (n as any).completed_at
      ? new Date((n as any).completed_at as string).getTime()
      : start + dur;
    const xRatio = Math.max(0, Math.min(1, (nodeStart - start) / dur));
    const wRatio = Math.max(0.02, Math.min(1 - xRatio, (nodeEnd - nodeStart) / dur));

    return {
      id: n.node_id,
      type: 'timeline',
      position: {
        x: TIMELINE_CONSTANTS.LEFT_LABEL_WIDTH + xRatio * TL_CANVAS_W,
        y: idx * TIMELINE_CONSTANTS.ROW_HEIGHT,
      },
      data: {
        label: n.display_name ?? n.node_id,
        nodeId: n.node_id,
        status: n.status,
        width: Math.max(80, wRatio * TL_CANVAS_W),
        durationMs: n.duration_ms,
        agentId: n.agent_id,
      },
      draggable: false,
    };
  });

  // 时间线视图不需要边（节点条本身就是连接关系）
  return { rfNodes, rfEdges: [] };
}