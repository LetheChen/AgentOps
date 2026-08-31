import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { useMemo } from 'react';
import { DagNode, type DagNodeData } from './dag/DagNode';
import { AnimatedEdge, type AnimatedEdgeData } from './dag/AnimatedEdge';
import { TimelineNode } from './dag/TimelineNode';
import { buildDagLayout, buildLaneLayout, buildTimelineLayout } from './dag/layouts';
import type { GraphNode, GraphEdge, HandoffInfo, LaneInfo, TimelineEntry } from '../lib/types';

export type DagNodeStatus =
  | 'pending'
  | 'ready'
  | 'waiting'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped';

export const STATUS_COLORS: Record<DagNodeStatus, string> = {
  pending: '#94a3b8',
  ready: '#fbbf24',
  waiting: '#a78bfa',
  running: '#3b82f6',
  completed: '#10b981',
  failed: '#ef4444',
  skipped: '#6b7280',
};

/**
 * 节点类型：dag（默认拓扑）/ timeline（时间线行）
 * 模块级常量（ReactFlow 要求，避免每次渲染重建）
 */
const nodeTypes = {
  dag: DagNode,
  timeline: TimelineNode,
};
const edgeTypes = {
  animated: AnimatedEdge,
};

/**
 * 旧版 simple 模式节点类型（DagCanvas 内部用）
 */
type DagNodeSimpleData = {
  label: string;
  status?: DagNodeStatus;
  subtitle?: string;
};

export type CanvasMode = 'simple' | 'dag' | 'lane' | 'timeline';

export interface DagCanvasProps {
  // ===== 旧版 simple 模式（DagCanvas 内部默认） =====
  nodes?: Node<DagNodeSimpleData>[];
  edges?: Edge[];
  // ===== 新版 v2 三模式（mode !== 'simple' 时启用） =====
  mode?: CanvasMode;
  graphNodes?: GraphNode[];
  graphEdges?: GraphEdge[];
  handoffs?: HandoffInfo[];
  lanes?: LaneInfo[];
  timeline?: TimelineEntry[];
  runStartTime?: number;
  runDuration?: number;
  onNodeClick?: (nodeId: string) => void;
}

function withStatusStyle(node: Node<DagNodeSimpleData>): Node<DagNodeSimpleData> {
  const status = node.data.status ?? 'pending';
  const color = STATUS_COLORS[status];

  return {
    ...node,
    style: {
      border: `2px solid ${color}`,
      borderRadius: 12,
      color: '#0f172a',
      fontWeight: 700,
      padding: 12,
      width: 160,
      boxShadow: `0 8px 24px ${color}24`,
      ...(node.style ?? {}),
    },
    data: {
      ...node.data,
      label: node.data.subtitle ? `${node.data.label}\n${node.data.subtitle}` : node.data.label,
    },
  };
}

export default function DagCanvas(props: DagCanvasProps) {
  const {
    nodes = [],
    edges = [],
    mode = 'simple',
    graphNodes = [],
    graphEdges = [],
    handoffs,
    lanes = [],
    timeline,
    runStartTime,
    runDuration,
    onNodeClick,
  } = props;

  // === 旧版 simple 模式（保持向后兼容） ===
  if (mode === 'simple') {
    const styledNodes = nodes.map(withStatusStyle);
    const hasGraph = styledNodes.length > 0 || edges.length > 0;

    return (
      <section className="dag-card" aria-label="DAG 画布">
        <div className="section-header">
          <div>
            <p className="eyebrow">Workflow DAG</p>
            <h2>工作流拓扑</h2>
          </div>
          <span className="pill">React Flow</span>
        </div>

        <div className="dag-canvas-shell">
          {!hasGraph && (
            <div className="empty-dag-hint">
              <strong>暂无 DAG 数据</strong>
              <span>触发 Orchestrator 即可看到节点拓扑</span>
            </div>
          )}
          <ReactFlow nodes={styledNodes} edges={edges} fitView nodesDraggable={false} nodesConnectable={false}>
            <Background gap={18} size={1.4} color="#cbd5e1" />
            <MiniMap
              nodeColor={(node: { data?: { status?: DagNodeStatus } }) => STATUS_COLORS[node.data?.status ?? 'pending']}
              nodeStrokeColor={(node: { data?: { status?: DagNodeStatus } }) => STATUS_COLORS[node.data?.status ?? 'pending']}
              nodeBorderRadius={4}
              maskColor="rgba(11, 15, 20, 0.42)"
              maskStrokeColor="rgba(99, 102, 241, 0.8)"
              maskStrokeWidth={1.5}
              pannable
              zoomable
            />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        <div className="status-legend" aria-label="状态图例">
          {Object.entries(STATUS_COLORS).map(([status, color]) => (
            <span key={status} className="legend-item">
              <span className="legend-dot" style={{ backgroundColor: color }} />
              {status}
            </span>
          ))}
        </div>
      </section>
    );
  }

  // === 新版 v2 三模式（dag / lane / timeline） ===
  const { rfNodes, rfEdges } = useMemo(() => {
    switch (mode) {
      case 'lane':
        return buildLaneLayout(graphNodes, graphEdges, handoffs, lanes);
      case 'timeline':
        return buildTimelineLayout(graphNodes, timeline, runStartTime, runDuration);
      case 'dag':
      default:
        return buildDagLayout(graphNodes, graphEdges, handoffs);
    }
  }, [mode, graphNodes, graphEdges, handoffs, lanes, timeline, runStartTime, runDuration]);

  const titleByMode: Record<string, { eyebrow: string; title: string }> = {
    dag: { eyebrow: 'Workflow DAG · 开发者视角', title: '工作流拓扑' },
    lane: { eyebrow: 'Workflow DAG · 业务视角', title: '业务角色泳道' },
    timeline: { eyebrow: 'Workflow DAG · 时间线', title: '节点时序视图' },
  };
  const meta = titleByMode[mode] ?? titleByMode.dag;

  return (
    <section className="dag-card" aria-label="DAG 画布">
      <div className="section-header">
        <div>
          <p className="eyebrow">{meta.eyebrow}</p>
          <h2>{meta.title}</h2>
        </div>
        <span className="pill">React Flow · {mode}</span>
      </div>

      <div className="dag-canvas-shell">
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          nodesDraggable={false}
          nodesConnectable={false}
          onNodeClick={(_e, n) => onNodeClick?.(n.id)}
        >
          <Background gap={18} size={1.4} color="#cbd5e1" />
          <MiniMap
            nodeColor={(node: { data?: { status?: DagNodeStatus } }) => STATUS_COLORS[node.data?.status ?? 'pending']}
            nodeStrokeColor={(node: { data?: { status?: DagNodeStatus } }) => STATUS_COLORS[node.data?.status ?? 'pending']}
            nodeBorderRadius={4}
            maskColor="rgba(11, 15, 20, 0.42)"
            maskStrokeColor="rgba(99, 102, 241, 0.8)"
            maskStrokeWidth={1.5}
            pannable
            zoomable
          />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>

      <div className="status-legend" aria-label="状态图例">
        {Object.entries(STATUS_COLORS).map(([status, color]) => (
          <span key={status} className="legend-item">
            <span className="legend-dot" style={{ backgroundColor: color }} />
            {status}
          </span>
        ))}
      </div>
    </section>
  );
}

// 抑制 unused 警告（AnimatedEdgeData 当前由 buildDagLayout 通过 type 类型使用，但 explicit import 防止 tree-shake）
export type { AnimatedEdgeData };