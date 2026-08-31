/**
 * DeveloperDagView — 开发者视角 DAG 拓扑（v4 重设计）。
 *
 * 设计原则（视觉哲学）：
 *  - 网格背景底纹（工程感）
 *  - 节点按 layer 分层 → 横向排列，dagre 风格自动间距
 *  - 边线：简洁贝塞尔曲线 + 条件着色（✓ success / ✗ failure dashed / always muted）
 *  - running 边自动虚线流动动画
 *  - 节点卡片用 DagNodeCard（共享 ShapeRegistry）
 *  - 柔和色调：状态色用 soft/border 两层语义
 *  - 点击节点 → 抽屉联动
 *
 * v4 重设计要点：
 *  - 网格背景 24px
 *  - 边线简化：去掉 3 层光晕 + 3 粒子，改为单层条件色 + 虚线流动
 *  - 边条件标签（✓/✗）
 *  - 柔和状态色（success=#5ee2ad, running=#4fd8e8, failed=#fca5a5）
 *  - 节点 running 发光效果
 *  - ResizeObserver 性能优化
 *  - 缩放控制
 */
import { useEffect, useRef, useState, useMemo, useCallback } from 'react';
import type { CollaborationGraph, GraphNode } from '../../lib/types';
import {
  DagNodeCard,
  graphNodeToBadgeData,
  type GraphNodeLike,
} from './DagNodeShapeRegistry';
import { resolveDagNodeSemantic } from './DagNodeSemantics';
import { DagLegend } from './DagLegend';

interface DeveloperDagViewProps {
  graphData: CollaborationGraph;
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
}

/** 柔和状态色 */
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

const DEFAULT_COLOR = STATUS_COLORS.pending;

function getStatusColor(status: string) {
  return STATUS_COLORS[status] || DEFAULT_COLOR;
}

export function DeveloperDagView({ graphData, selectedNodeId, onSelectNode }: DeveloperDagViewProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [legendOpen, setLegendOpen] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const dragStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
  const nodes = graphData.nodes || [];
  const edges = graphData.edges || [];
  const handoffs = graphData.handoffs || [];

  // ─── 1. 计算节点布局：layer（最长路径 BFS）→ x，node_id → y ───
  const layout = useMemo(() => {
    if (nodes.length === 0) return { positions: {} as Record<string, { x: number; y: number; layer: number }>, layers: 0 };

    const incoming: Record<string, string[]> = {};
    edges.forEach((e) => {
      if (!incoming[e.to]) incoming[e.to] = [];
      incoming[e.to].push(e.from);
    });
    const layer: Record<string, number> = {};
    const computeLayer = (id: string, visited: Set<string>): number => {
      if (layer[id] !== undefined) return layer[id];
      if (visited.has(id)) return 0;
      visited.add(id);
      const ins = incoming[id] || [];
      const l = ins.length === 0 ? 0 : Math.max(...ins.map((p) => computeLayer(p, visited))) + 1;
      layer[id] = l;
      return l;
    };
    nodes.forEach((n) => computeLayer(n.node_id, new Set()));

    // y 分桶
    const layerBuckets: Record<number, Record<string, number>> = {};
    nodes.forEach((n) => {
      if (!layerBuckets[layer[n.node_id]]) layerBuckets[layer[n.node_id]] = {};
      if (layerBuckets[layer[n.node_id]][n.node_id] === undefined) {
        layerBuckets[layer[n.node_id]][n.node_id] = Object.keys(layerBuckets[layer[n.node_id]]).length;
      }
    });
    const totalLayers = Math.max(...Object.values(layer)) + 1;

    const colWidth = 240;
    const rowHeight = 120;
    const positions: Record<string, { x: number; y: number; layer: number }> = {};
    nodes.forEach((n) => {
      const col = layer[n.node_id];
      const row = layerBuckets[col][n.node_id];
      positions[n.node_id] = {
        x: 40 + col * colWidth,
        y: 40 + row * rowHeight,
        layer: col,
      };
    });
    return { positions, layers: totalLayers };
  }, [nodes, edges]);

  // ─── 2. 计算 SVG path 坐标 ───
  const [svgPaths, setSvgPaths] = useState<Array<{
    id: string; d: string; label: string; port: string;
    fromStatus: string; toStatus: string; midX: number; midY: number;
  }>>([]);

  const recalcPaths = useCallback(() => {
    if (!wrapRef.current || !contentRef.current) return;
    const contentRect = contentRef.current.getBoundingClientRect();

    const paths: Array<{
      id: string; d: string; label: string; port: string;
      fromStatus: string; toStatus: string; midX: number; midY: number;
    }> = [];
    edges.forEach((e, i) => {
      const fromEl = wrapRef.current!.querySelector(`[data-dag-node="${e.from}"]`) as HTMLElement;
      const toEl = wrapRef.current!.querySelector(`[data-dag-node="${e.to}"]`) as HTMLElement;
      if (!fromEl || !toEl) return;
      const fromRect = fromEl.getBoundingClientRect();
      const toRect = toEl.getBoundingClientRect();
      const x1 = (fromRect.right - contentRect.left) / zoom;
      const y1 = (fromRect.top + fromRect.height / 2 - contentRect.top) / zoom;
      const x2 = (toRect.left - contentRect.left) / zoom;
      const y2 = (toRect.top + toRect.height / 2 - contentRect.top) / zoom;
      // 贝塞尔曲线
      const dx = x2 - x1;
      const cp1x = x1 + dx * 0.5;
      const cp2x = x2 - dx * 0.5;
      const d = `M ${x1} ${y1} C ${cp1x} ${y1} ${cp2x} ${y2} ${x2} ${y2}`;
      const midX = (x1 + x2) / 2;
      const midY = (y1 + y2) / 2;

      const fromStatus = nodes.find((n) => n.node_id === e.from)?.status || 'pending';
      const toStatus = nodes.find((n) => n.node_id === e.to)?.status || 'pending';
      paths.push({ id: `${e.from}-${e.to}-${i}`, d, label: e.port, port: e.port, fromStatus, toStatus, midX, midY });
    });
    setSvgPaths(paths);
  }, [edges, nodes, zoom]);

  // ─── 3. 边路径重算：ResizeObserver ───
  useEffect(() => {
    recalcPaths();
  }, [recalcPaths, layout, selectedNodeId, zoom]);

  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver(() => recalcPaths());
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, [recalcPaths]);

  // ─── 4. 缩放控制 ───
  const handleZoomIn = useCallback(() => setZoom(z => Math.min(z + 0.15, 2)), []);
  const handleZoomOut = useCallback(() => setZoom(z => Math.max(z - 0.15, 0.3)), []);
  const handleZoomReset = useCallback(() => { setZoom(1); setPan({ x: 0, y: 0 }); }, []);

  // ─── 4b. 拖拽平移 ───
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    // 点击节点时不拖拽（节点自身有 onClick）
    if ((e.target as HTMLElement).closest('[data-dag-node]')) return;
    setIsDragging(true);
    dragStart.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
  }, [pan]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isDragging) return;
    const dx = e.clientX - dragStart.current.x;
    const dy = e.clientY - dragStart.current.y;
    setPan({ x: dragStart.current.panX + dx, y: dragStart.current.panY + dy });
  }, [isDragging]);

  const handleMouseUp = useCallback(() => setIsDragging(false), []);

  // ─── 4c. 滚轮缩放（以鼠标位置为中心） ───
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    setZoom(z => Math.max(0.3, Math.min(2, z + delta)));
  }, []);

  // ─── 5. 计算内容尺寸 ───
  const contentBounds = useMemo(() => {
    if (nodes.length === 0) return { width: 800, height: 400 };
    const maxX = Math.max(...Object.values(layout.positions).map(p => p.x)) + 240;
    const maxY = Math.max(...Object.values(layout.positions).map(p => p.y)) + 120;
    return { width: Math.max(maxX, 800), height: Math.max(maxY, 400) };
  }, [layout, nodes]);

  // ─── 4d. 自动 fit-to-view：首次加载时根据画布尺寸自动缩放 ───
  const hasAutoFit = useRef(false);
  useEffect(() => {
    if (hasAutoFit.current || !wrapRef.current || nodes.length === 0) return;
    hasAutoFit.current = true;
    const wrapRect = wrapRef.current.getBoundingClientRect();
    const padding = 60;
    const scaleX = (wrapRect.width - padding * 2) / contentBounds.width;
    const scaleY = (wrapRect.height - padding * 2) / contentBounds.height;
    const fitZoom = Math.min(scaleX, scaleY, 1);
    if (fitZoom < 1) {
      setZoom(fitZoom);
      // 居中
      setPan({
        x: (wrapRect.width - contentBounds.width * fitZoom) / 2,
        y: (wrapRect.height - contentBounds.height * fitZoom) / 2,
      });
    }
  }, [contentBounds, nodes]);

  if (nodes.length === 0) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: '#8b97b0', fontSize: 13 }}>
        📐 DAG 拓扑：该 run 无节点
      </div>
    );
  }

  return (
    <div
      ref={wrapRef}
      className="developer-dag-view"
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
      onWheel={handleWheel}
      style={{
        position: 'relative', flex: 1, overflow: 'hidden', padding: 0, minHeight: 600,
        cursor: isDragging ? 'grabbing' : 'grab',
        // 24px 网格背景
        backgroundImage: `
          linear-gradient(rgba(148, 178, 214, 0.04) 1px, transparent 1px),
          linear-gradient(90deg, rgba(148, 178, 214, 0.04) 1px, transparent 1px)
        `,
        backgroundSize: '24px 24px',
      }}
    >
      {/* ─── 可缩放内容区 ─── */}
      <div
        ref={contentRef}
        style={{
          position: 'relative',
          width: contentBounds.width,
          height: contentBounds.height,
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: '0 0',
          transition: isDragging ? 'none' : 'transform 0.2s ease',
        }}
      >
        {/* ─── SVG 边层 ─── */}
        <svg
          className="developer-dag-svg"
          style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', pointerEvents: 'none', zIndex: 1, overflow: 'visible' }}
        >
          <defs>
            {/* 箭头标记（柔和色） */}
            <marker id="dag-arrow-default" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
              <path d="M0,0 L0,6 L7,3 z" fill="rgba(148, 178, 214, 0.4)" />
            </marker>
            <marker id="dag-arrow-success" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
              <path d="M0,0 L0,6 L7,3 z" fill="rgba(94, 226, 173, 0.6)" />
            </marker>
            <marker id="dag-arrow-failed" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
              <path d="M0,0 L0,6 L7,3 z" fill="rgba(252, 165, 165, 0.6)" />
            </marker>
            {/* 流光粒子路径 */}
            {svgPaths.map((p) => (
              <path key={`pp-${p.id}`} id={`pp-${p.id}`} d={p.d} fill="none" stroke="none" />
            ))}
          </defs>

          {/* 边线渲染（简洁风格） */}
          {svgPaths.map((p) => {
            const isRunning = p.toStatus === 'running';
            const isFailed = p.fromStatus === 'failed' || p.toStatus === 'failed';
            const isCompleted = p.fromStatus === 'completed' && p.toStatus === 'completed';
            const isSkipped = p.fromStatus === 'skipped' || p.toStatus === 'skipped';

            // 条件着色（agentops 风格）
            const edgeColor = isFailed ? '#fca5a5' : isRunning ? '#4fd8e8' : isCompleted ? '#5ee2ad' : isSkipped ? '#fbc94b' : 'rgba(148, 178, 214, 0.3)';
            const arrowId = isFailed ? 'dag-arrow-failed' : isCompleted ? 'dag-arrow-success' : 'dag-arrow-default';
            const isDashed = isFailed || isSkipped;
            // 条件标签
            const conditionLabel = isFailed ? '✗' : isCompleted ? '✓' : '';

            return (
              <g key={p.id}>
                {/* 主边线（单层，简洁） */}
                <path
                  d={p.d}
                  fill="none"
                  stroke={edgeColor}
                  strokeWidth={1.8}
                  strokeDasharray={isDashed ? '5 5' : isRunning ? '6 4' : '0'}
                  style={{
                    animation: isRunning ? 'dag-edge-flow-v4 1.2s linear infinite' : isDashed ? 'dag-edge-flow-v4 2s linear infinite' : undefined,
                  }}
                  markerEnd={`url(#${arrowId})`}
                />
                {/* running 状态：单个流光粒子（不再 3 粒子） */}
                {isRunning && (
                  <circle r={3} fill="#4fd8e8" style={{ filter: 'drop-shadow(0 0 4px rgba(79, 216, 232, 0.6))' }}>
                    <animateMotion dur="1.8s" repeatCount="indefinite">
                      <mpath href={`#pp-${p.id}`} />
                    </animateMotion>
                  </circle>
                )}
                {/* 条件标签（✓/✗） */}
                {conditionLabel && (
                  <text
                    x={p.midX}
                    y={p.midY - 6}
                    fontSize={12}
                    fill={edgeColor}
                    textAnchor="middle"
                    fontWeight="bold"
                  >
                    {conditionLabel}
                  </text>
                )}
                {/* 端口标签 */}
                {p.port && !conditionLabel && (
                  <g>
                    <rect
                      x={p.midX - 28}
                      y={p.midY - 8}
                      width={56}
                      height={16}
                      rx={4}
                      fill="rgba(10, 15, 24, 0.92)"
                      stroke="rgba(148, 178, 214, 0.12)"
                    />
                    <text
                      x={p.midX}
                      y={p.midY + 3}
                      fontSize={9}
                      fill="rgba(148, 178, 214, 0.56)"
                      textAnchor="middle"
                      fontFamily='"SF Mono", Consolas, monospace'
                    >
                      {p.port}
                    </text>
                  </g>
                )}
              </g>
            );
          })}
        </svg>

        {/* ─── 节点卡片 ─── */}
        {nodes.map((n) => {
          const semantic = resolveDagNodeSemantic({
            node_type: n.node_type,
            gateway_kind: n.gateway_kind,
            terminal_kind: n.terminal_kind,
            status: n.status,
            business_role: n.business_role,
          });
          const badgeData = graphNodeToBadgeData(n as GraphNodeLike);
          const statusColor = getStatusColor(n.status);
          const isSelected = n.node_id === selectedNodeId;
          const isRunning = n.status === 'running';

          return (
            <div
              key={n.node_id}
              data-dag-node={n.node_id}
              style={{
                position: 'absolute',
                left: layout.positions[n.node_id]?.x ?? 40,
                top: layout.positions[n.node_id]?.y ?? 40,
                zIndex: isSelected ? 12 : 2,
                // running 节点发光效果
                filter: isRunning ? `drop-shadow(0 0 14px ${statusColor.soft})` : isSelected ? `drop-shadow(0 0 10px ${statusColor.border})` : 'none',
                transition: 'filter 0.3s ease',
              }}
            >
              <DagNodeCard
                semantic={semantic}
                node_id={n.node_id}
                display_name={n.display_name}
                agent_label={n.agent_id}
                selected={isSelected}
                onClick={() => onSelectNode(n.node_id)}
                badges={badgeData}
              />
            </div>
          );
        })}
      </div>

      {/* ─── 可折叠图例（右上角） ─── */}
      <button
        className={`dag-legend-toggle ${legendOpen ? 'active' : ''}`}
        onClick={() => setLegendOpen(o => !o)}
        title="节点类型图例"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
      </button>
      <div className={`dag-legend-collapsible ${legendOpen ? 'open' : ''}`}>
        <DagLegend />
        <div style={{ height: 1, background: 'rgba(148, 178, 214, 0.1)', margin: '8px 0' }} />
        <div style={{ height: 1, background: 'rgba(148, 178, 214, 0.1)', margin: '8px 0' }} />
        <div className="dag-legend-collapsible-title" style={{ marginBottom: 6 }}>状态色</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {[
            { color: '#5ee2ad', label: '已完成' },
            { color: '#4fd8e8', label: '运行中' },
            { color: '#fca5a5', label: '失败' },
            { color: '#fbc94b', label: '跳过' },
            { color: '#94b2d6', label: '等待' },
          ].map(item => (
            <div key={item.label} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: item.color }} />
              <span style={{ color: '#8b97b0' }}>{item.label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* ─── 缩放控制（左下角） ─── */}
      <div className="dag-zoom-controls">
        <button className="dag-zoom-btn" onClick={handleZoomIn} title="放大">+</button>
        <button className="dag-zoom-btn" onClick={handleZoomOut} title="缩小">−</button>
        <button className="dag-zoom-btn" onClick={handleZoomReset} title="重置视图" style={{ fontSize: '11px' }}>⊕</button>
        <div className="dag-zoom-level">{Math.round(zoom * 100)}%</div>
      </div>

      {/* ─── 拖拽提示（底部居中，渐隐） ─── */}
      <div className="dag-pan-hint">拖拽画布平移 · 滚轮缩放</div>

      {/* ─── Handoff 面板（右下角，简化设计） ─── */}
      {handoffs.length > 0 && (
        <div className="dag-handoff-panel">
          <div className="dag-handoff-panel-header">
            <span className="dag-handoff-panel-title">Handoff</span>
            <span className="dag-handoff-panel-count">{handoffs.length}</span>
          </div>
          <div className="dag-handoff-list">
            {handoffs.map((h) => (
              <div key={h.id} className="dag-handoff-item">
                <span className="dag-handoff-item-from" title={h.from_node}>{h.from_node}</span>
                <span className="dag-handoff-item-arrow">→</span>
                <span className="dag-handoff-item-to" title={h.to_node}>{h.to_node}</span>
                <span className="dag-handoff-item-port">{h.port}</span>
                <span className="dag-handoff-item-size">
                  {h.payload_size > 1024 ? `${(h.payload_size / 1024).toFixed(1)}KB` : `${h.payload_size}B`}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 边线流动动画 */}
      <style>{`
        @keyframes dag-edge-flow-v4 {
          to { stroke-dashoffset: -20; }
        }
      `}</style>
    </div>
  );
}

export type { GraphNode };
