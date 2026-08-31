import { useMemo, useRef, useEffect, useState } from 'react';
import type { CollaborationGraph, GraphNode, TimelineEntry, LaneInfo } from '../../lib/types';
import {
  DagNodeCard,
  graphNodeToBadgeData,
  type GraphNodeLike,
} from './DagNodeShapeRegistry';
import { resolveDagNodeSemantic } from './DagNodeSemantics';

interface BusinessLaneViewProps {
  graphData: CollaborationGraph;
  timelineData: TimelineEntry[];
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
}

/** 柔和状态色（与 DeveloperDagView v4 共享调色板） */
const STATUS_COLORS: Record<string, { base: string; soft: string; border: string }> = {
  completed: { base: '#5ee2ad', soft: 'rgba(52, 211, 153, 0.12)', border: 'rgba(52, 211, 153, 0.32)' },
  running:   { base: '#4fd8e8', soft: 'rgba(79, 216, 232, 0.12)', border: 'rgba(79, 216, 232, 0.32)' },
  failed:    { base: '#fca5a5', soft: 'rgba(248, 113, 113, 0.12)', border: 'rgba(248, 113, 113, 0.34)' },
  skipped:   { base: '#fbc94b', soft: 'rgba(251, 191, 36, 0.12)', border: 'rgba(251, 191, 36, 0.34)' },
  pending:   { base: '#94b2d6', soft: 'rgba(148, 178, 214, 0.06)', border: 'rgba(148, 178, 214, 0.12)' },
  ready:     { base: '#93c5fd', soft: 'rgba(124, 179, 255, 0.12)', border: 'rgba(124, 179, 255, 0.3)' },
  waiting:   { base: '#a5b4fc', soft: 'rgba(129, 140, 248, 0.12)', border: 'rgba(129, 140, 248, 0.32)' },
  cancelled: { base: '#94b2d6', soft: 'rgba(148, 178, 214, 0.06)', border: 'rgba(148, 178, 214, 0.12)' },
};
function getStatusColor(status: string) {
  return STATUS_COLORS[status] || STATUS_COLORS.pending;
}

// 业务角色名 → emoji 映射（用于泳道头像）
const ROLE_EMOJI: Record<string, string> = {
  fanout: '🔄',
  research: '🔍',
  synthesis: '🧠',
  auditor: '🛡️',
  integrator: '🔗',
  reporter: '📝',
  需求分析师: '📋',
  数据采集员: '📊',
  异常分析员: '🧠',
  报告撰写员: '📝',
  告警通知员: '📢',
  Manager: '🎯',
  manager: '🎯',
};

/**
 * BusinessLaneView — 业务角色泳道（业务视角，v2 重设计）。
 *
 * 设计原则（视觉哲学）：
 *  - 24px 网格背景底纹
 *  - 柔和状态色（soft/border 两层语义）
 *  - 时间轴：刻度线 + NOW 指示器（柔和青色替代高饱和蓝）
 *  - 泳道卡片：半透明深底 + 左侧状态色条 + hover 发光
 *  - Handoff 连线：简洁虚线 + 箭头，柔和紫色
 *  - running 节点发光效果
 */
export function BusinessLaneView({ graphData, selectedNodeId, onSelectNode }: BusinessLaneViewProps) {
  const lanes = graphData.lanes || [];
  const nodes = graphData.nodes || [];
  const handoffs = graphData.handoffs || [];
  const timeline = graphData.timeline || [];

  // ─── 派生：run 时间窗口 ───
  const runStart = useMemo(() => {
    const ts: number[] = [];
    if (graphData.started_at) ts.push(new Date(graphData.started_at).getTime());
    timeline.forEach((e) => {
      if (e.occurred_at) ts.push(new Date(e.occurred_at).getTime());
    });
    return ts.length > 0 ? Math.min(...ts) : Date.now() - 60000;
  }, [graphData.started_at, timeline]);

  const runEnd = useMemo(() => {
    if (graphData.finished_at) return new Date(graphData.finished_at).getTime();
    const ts: number[] = [];
    timeline.forEach((e) => {
      if (e.occurred_at) ts.push(new Date(e.occurred_at).getTime());
    });
    return ts.length > 0 ? Math.max(...ts) : Date.now();
  }, [graphData.finished_at, timeline]);

  const runDurationMs = Math.max(runEnd - runStart, 30000);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (graphData.status !== 'running') return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [graphData.status]);
  const nowPct = Math.min(100, Math.max(0, ((now - runStart) / runDurationMs) * 100));

  // ─── 派生：每个节点的 start/end/pct ───
  const nodeMeta = useMemo(() => {
    const map: Record<string, { start?: number; end?: number; startPct?: number; widthPct?: number }> = {};
    timeline.forEach((e) => {
      if (!e.node_id) return;
      const t = e.occurred_at ? new Date(e.occurred_at).getTime() : undefined;
      if (t === undefined) return;
      if (e.type === 'node.started') {
        map[e.node_id] = { ...map[e.node_id], start: t };
      } else if (e.type === 'node.completed' || e.type === 'node.failed' || e.type === 'node.skipped') {
        const cur = map[e.node_id] || {};
        cur.end = t;
        map[e.node_id] = cur;
      }
    });
    Object.keys(map).forEach((id) => {
      const m = map[id];
      if (m.start !== undefined) {
        m.startPct = Math.min(100, Math.max(0, ((m.start - runStart) / runDurationMs) * 100));
      }
      if (m.start !== undefined && m.end !== undefined) {
        m.widthPct = Math.max(2, ((m.end - m.start) / runDurationMs) * 100);
      }
    });
    return map;
  }, [timeline, runStart, runDurationMs]);

  // ─── 派生：running 节点的 elapsed 秒数 ───
  const runningElapsed: Record<string, number> = useMemo(() => {
    const map: Record<string, number> = {};
    nodes.forEach((n) => {
      if (n.status === 'running' && nodeMeta[n.node_id]?.start) {
        map[n.node_id] = Math.floor((now - nodeMeta[n.node_id]!.start!) / 1000);
      }
    });
    return map;
  }, [nodes, nodeMeta, now]);

  if (lanes.length === 0) {
    return (
      <div className="business-lane-empty">
        <div className="business-lane-empty-icon">🎯</div>
        <div className="business-lane-empty-title">业务泳道</div>
        <div className="business-lane-empty-desc">该 run 无节点（可能是对话 session 或 workflow 已删除）</div>
      </div>
    );
  }

  return (
    <div className="business-lane-view">
      {/* ─── 时间轴 ─── */}
      <div className="business-lane-timeline" style={{ marginLeft: 160 }}>
        {[0, 25, 50, 75, 100].map((p) => {
          const totalSec = Math.floor((runDurationMs * p) / 100000);
          const mm = Math.floor(totalSec / 60).toString().padStart(2, '0');
          const ss = (totalSec % 60).toString().padStart(2, '0');
          return (
            <div key={p} className="business-lane-tick" style={{ left: `${p}%` }}>
              <div className="business-lane-tick-line" />
              <span className="business-lane-tick-label">{mm}:{ss}</span>
            </div>
          );
        })}
        {/* NOW 指示器 */}
        <div className="business-lane-now" style={{ left: `${nowPct}%` }}>
          <div className="business-lane-now-line" />
          <span className="business-lane-now-label">NOW</span>
        </div>
      </div>

      {/* ─── 泳道列表 ─── */}
      <div className="business-lane-list">
        {lanes.map((lane, idx) => (
          <LaneRow
            key={lane.business_role}
            lane={lane}
            nodes={nodes.filter((n) => lane.nodes.includes(n.node_id))}
            nodeMeta={nodeMeta}
            runningElapsed={runningElapsed}
            selectedNodeId={selectedNodeId}
            onSelectNode={onSelectNode}
            index={idx}
          />
        ))}
      </div>

      {/* ─── Handoff 气泡层 ─── */}
      <HandoffBubbleLayer
        handoffs={handoffs}
        lanes={lanes}
        nodes={nodes}
        nodeMeta={nodeMeta}
        runStart={runStart}
        runDurationMs={runDurationMs}
      />
    </div>
  );
}

// ─── 单条泳道 ───
function LaneRow({ lane, nodes, nodeMeta, runningElapsed, selectedNodeId, onSelectNode }: {
  lane: LaneInfo;
  nodes: GraphNode[];
  nodeMeta: Record<string, { start?: number; end?: number; startPct?: number; widthPct?: number }>;
  runningElapsed: Record<string, number>;
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
  index: number;
}) {
  const emoji = ROLE_EMOJI[lane.business_role] || '👤';
  const runningCount = nodes.filter(n => n.status === 'running').length;
  const completedCount = nodes.filter(n => n.status === 'completed').length;

  return (
    <div className="business-lane-row">
      {/* lane-label */}
      <div className="business-lane-label" style={{ borderLeftColor: lane.color }}>
        <div className="business-lane-avatar" style={{ background: `${lane.color}1a`, borderColor: `${lane.color}40` }}>
          <span style={{ fontSize: 18 }}>{emoji}</span>
        </div>
        <div className="business-lane-label-info">
          <div className="business-lane-label-name">{lane.business_role}</div>
          <div className="business-lane-label-meta">
            {nodes[0]?.harness || 'n/a'} · {nodes[0]?.model ? nodes[0].model.split('/').pop() : 'auto'}
          </div>
          <div className="business-lane-label-stats">
            {completedCount > 0 && <span className="lane-stat completed">✓{completedCount}</span>}
            {runningCount > 0 && <span className="lane-stat running">●{runningCount}</span>}
          </div>
        </div>
      </div>

      {/* lane-track */}
      <div className="business-lane-track">
        {nodes.length === 0 && (
          <span className="business-lane-empty-track">暂无节点</span>
        )}
        {nodes.map((n) => (
          <NodeCard
            key={n.node_id}
            node={n}
            startPct={nodeMeta[n.node_id]?.startPct}
            widthPct={nodeMeta[n.node_id]?.widthPct}
            elapsedSec={runningElapsed[n.node_id]}
            selected={n.node_id === selectedNodeId}
            onClick={() => onSelectNode(n.node_id)}
          />
        ))}
      </div>
    </div>
  );
}

// ─── 节点卡片 ───
function NodeCard({ node, startPct, widthPct, elapsedSec, selected, onClick }: {
  node: GraphNode;
  startPct?: number;
  widthPct?: number;
  elapsedSec?: number;
  selected: boolean;
  onClick: () => void;
}) {
  const left = startPct !== undefined ? `${startPct}%` : '10%';
  const minWidth = widthPct !== undefined ? `${Math.max(widthPct, 8)}%` : 180;

  const semantic = resolveDagNodeSemantic({
    node_type: node.node_type,
    gateway_kind: node.gateway_kind,
    terminal_kind: node.terminal_kind,
    status: node.status,
    business_role: node.business_role,
  });
  const badgeData = graphNodeToBadgeData(node as GraphNodeLike);
  const statusColor = getStatusColor(node.status);
  const isRunning = node.status === 'running';

  const badgeOverride = (() => {
    if (node.status === 'running' && elapsedSec != null) {
      return { ...badgeData, duration_ms: elapsedSec * 1000 };
    }
    return badgeData;
  })();

  return (
    <div
      className={`business-lane-node-card ${selected ? 'selected' : ''}`}
      style={{
        left, minWidth, maxWidth: 220,
        zIndex: selected ? 12 : 2,
        // running 节点发光
        filter: isRunning ? `drop-shadow(0 0 12px ${statusColor.soft})` : selected ? `drop-shadow(0 0 8px ${statusColor.border})` : 'none',
      }}
      onClick={onClick}
    >
      <DagNodeCard
        semantic={semantic}
        node_id={node.node_id}
        display_name={node.display_name}
        agent_label={node.agent_id ? `${ROLE_EMOJI[node.business_role] || '⚙️'} ${node.agent_id}` : undefined}
        selected={selected}
        onClick={onClick}
        badges={badgeOverride}
      />
    </div>
  );
}

// ─── Handoff 气泡层 ───
function HandoffBubbleLayer({ handoffs, lanes, nodes, nodeMeta, runStart, runDurationMs }: {
  handoffs: CollaborationGraph['handoffs'];
  lanes: LaneInfo[];
  nodes: GraphNode[];
  nodeMeta: Record<string, { start?: number; end?: number; startPct?: number; widthPct?: number }>;
  runStart: number;
  runDurationMs: number;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [bubbleLayouts, setBubbleLayouts] = useState<
    Array<{
      id: string;
      topPx: number;
      leftPct: number;
      handoff: typeof handoffs[0];
      fromTop?: number;
      toTop?: number;
    }>
  >([]);

  useEffect(() => {
    if (!wrapRef.current || handoffs.length === 0) return;
    const laneHeight = 106;
    const layouts = handoffs.map((h, i) => {
      const fromIdx = lanes.findIndex((l) => l.nodes.includes(h.from_node));
      const toIdx = lanes.findIndex((l) => l.nodes.includes(h.to_node));
      const t = h.occurred_at ? new Date(h.occurred_at).getTime() : undefined;
      const leftPct = t ? Math.min(95, Math.max(5, ((t - runStart) / runDurationMs) * 100)) : 30 + i * 15;
      const fromY = fromIdx >= 0 ? fromIdx * laneHeight + 50 : 50;
      const toY = toIdx >= 0 ? toIdx * laneHeight + 50 : 50;
      const topPx = (fromY + toY) / 2;
      return { id: h.id, topPx, leftPct, handoff: h, fromTop: fromY, toTop: toY };
    });
    setBubbleLayouts(layouts);
  }, [handoffs, lanes, runStart, runDurationMs]);

  if (handoffs.length === 0) return null;

  return (
    <div ref={wrapRef} className="handoff-bubble-layer">
      <svg className="handoff-bubble-svg">
        <defs>
          <marker id="handoff-arrow-v2" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L0,6 L7,3 z" fill="rgba(168, 85, 247, 0.5)" />
          </marker>
        </defs>
        {bubbleLayouts.map((b) => {
          if (b.fromTop === undefined || b.toTop === undefined) return null;
          const wrapWidth = wrapRef.current?.clientWidth ?? 0;
          const x = ((b.leftPct ?? 0) / 100) * wrapWidth;
          // 贝塞尔曲线（对齐 DeveloperDagView 风格）
          const midY = (b.fromTop + b.toTop) / 2;
          const path = `M ${x} ${b.fromTop} C ${x + 20} ${b.fromTop} ${x + 20} ${b.toTop} ${x} ${b.toTop}`;
          return (
            <path
              key={`path-${b.id}`}
              d={path}
              fill="none"
              stroke="rgba(168, 85, 247, 0.4)"
              strokeWidth={1.5}
              strokeDasharray="5 4"
              markerEnd="url(#handoff-arrow-v2)"
              className="handoff-bubble-path"
            />
          );
        })}
      </svg>

      {bubbleLayouts.map((b) => (
        <HandoffBubble key={b.id} topPx={b.topPx} leftPct={b.leftPct} handoff={b.handoff} />
      ))}
    </div>
  );
}

function HandoffBubble({ topPx, leftPct, handoff }: {
  topPx: number; leftPct: number; handoff: CollaborationGraph['handoffs'][0];
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      className={`handoff-bubble ${expanded ? 'expanded' : ''}`}
      style={{ top: topPx, left: `${leftPct}%` }}
      onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
    >
      <div className="handoff-bubble-header">
        <span className="handoff-bubble-arrow">→</span>
        <span className="handoff-bubble-roles">{handoff.from_role || '?'} → {handoff.to_role || '?'}</span>
      </div>
      <div className="handoff-bubble-summary">{handoff.summary || handoff.port}</div>
      <div className="handoff-bubble-meta">
        <span className="handoff-bubble-port">{handoff.port || '?'}</span>
        <span className="handoff-bubble-size">
          {handoff.payload_size > 1024 ? `${(handoff.payload_size / 1024).toFixed(1)}KB` : `${handoff.payload_size}B`}
        </span>
      </div>
    </div>
  );
}
