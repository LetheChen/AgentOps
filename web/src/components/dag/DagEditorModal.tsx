import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  Background, Controls, MiniMap, ReactFlow,
  useNodesState, useEdgesState, MarkerType,
  type Node, type Edge, type Connection, type ReactFlowInstance,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { DagEditorNode, type DagEditorNodeData } from './DagEditorNode';
import { EditorFlowEdge, type EditorFlowEdgeData } from './EditorFlowEdge';
import { NodeConfigPanel } from './NodeConfigPanel';
import {
  apiToEditorState, serializeWorkflowYaml,
  type EditorNode, type EditorWorkflow, type HarnessType,
} from '../../lib/workflowYaml';
import { apiClient } from '../../lib/api';

/**
 * DagEditorModal — 可视化 DAG 工作流编辑器。
 *
 * 替代旧的 YAML textarea 模态框，提供：
 *   - ReactFlow 画布：可拖拽节点、可连线、可选择
 *   - 右侧配置面板：选中节点后编辑全部属性
 *   - 工具栏：添加节点、自动布局、模式切换
 *   - YAML 预览：实时查看序列化结果
 *   - 双模式：可视化编辑 / YAML 编辑
 */

const nodeTypes = { editorNode: DagEditorNode };
const edgeTypes = { editorFlow: EditorFlowEdge };

// 节点类型徽章配置（字母 + 类型色），用于 minimap 节点内徽章
const NODE_TYPE_LOG: Record<string, { glyph: string; color: string; label: string }> = {
  agent: { glyph: 'A', color: '#F59E0B', label: 'AGT' },
  command: { glyph: 'C', color: '#60A5FA', label: 'CMD' },
  await_command: { glyph: 'C', color: '#60A5FA', label: 'AWA' },
  while: { glyph: 'W', color: '#A78BFA', label: 'LOP' },
  gateway: { glyph: 'G', color: '#34D399', label: 'GW' },
  parallel_branch: { glyph: 'B', color: '#34D399', label: 'BRC' },
};

/**
 * MiniMapLogoNode — minimap 自定义节点组件。
 *
 * 与默认实心矩形不同，本组件：
 *   - 用暗色矩形作底（与画布背景融合）
 *   - 节点中心嵌入 LOG 徽章：类型图标 + 节点 ID 前缀
 *   - agent 节点额外带 harness 短标签（如 LL、CDX、OPN）
 *   - 选中态用蓝色边框高亮
 *
 * 为什么不用 reactflow 自带的 nodeLabel：
 *   v11 MiniMap 不支持 nodeLabel / nodeLabelStyle（v12 才有），用 nodeComponent 自渲染。
 */
interface MiniMapLogoNodeProps {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  borderRadius: number;
  className: string;
  color: string;
  shapeRendering: string;
  strokeColor: string;
  strokeWidth: number;
  selected?: boolean;
  style?: React.CSSProperties;
  onClick?: (event: React.MouseEvent, id: string) => void;
}

function MiniMapLogoNode(props: MiniMapLogoNodeProps) {
  const { x, y, width, height, strokeColor, strokeWidth, borderRadius, color, selected, onClick } = props;
  const nodeMeta = (window as unknown as { __miniMapNodesById?: Record<string, EditorNode> }).__miniMapNodesById?.[props.id];
  const nType = (nodeMeta?.type ?? '').toLowerCase();
  const log = NODE_TYPE_LOG[nType] ?? { glyph: '●', color: '#94A3B8', label: '???' };
  // agent 节点的 harness 短标签（如 LL / CDX / OPN / CLA）
  const harnessShort = nodeMeta?.harness?.replace(/_/g, '').slice(0, 3).toUpperCase() ?? '';

  const cx = width / 2;
  const cy = height / 2;
  const minDim = Math.min(width, height);

  // LOD：按 minimap 上实际像素尺寸决定渲染细节
  const showGlyph = minDim >= 12;   // ≥12px 才显示类型字母
  const showHarness = minDim >= 22 && harnessShort && nType === 'agent';
  const glyphSize = showGlyph ? Math.max(7, minDim * 0.55) : 0;

  return (
    <g
      transform={`translate(${x},${y})`}
      onClick={(e) => onClick?.(e as unknown as React.MouseEvent, props.id)}
      style={{ cursor: 'pointer' }}
    >
      {/* 底色矩形（边框用类型色提供高辨识度） */}
      <rect
        width={width}
        height={height}
        rx={borderRadius}
        ry={borderRadius}
        fill={color}
        stroke={selected ? '#FFFFFF' : strokeColor}
        strokeWidth={selected ? Math.max(1.5, strokeWidth + 0.5) : strokeWidth}
      />

      {/* 类型字母徽章：与协作可视化 DAG 节点 glyph 风格一致 */}
      {showGlyph && (
        <g pointerEvents="none">
          <text
            x={cx}
            y={cy - (showHarness ? glyphSize * 0.25 : 0)}
            textAnchor="middle"
            dominantBaseline="central"
            fill={log.color}
            fontSize={glyphSize}
            fontWeight={800}
            fontFamily='ui-monospace, "SF Mono", Menlo, monospace'
            style={{ userSelect: 'none' }}
          >
            {log.glyph}
          </text>

          {showHarness && (
            <text
              x={cx}
              y={cy + glyphSize * 0.65}
              textAnchor="middle"
              dominantBaseline="central"
              fill={log.color}
              fontSize={Math.max(5, glyphSize * 0.42)}
              fontWeight={700}
              fontFamily='ui-monospace, "SF Mono", Menlo, monospace'
              opacity={0.9}
              style={{ userSelect: 'none' }}
            >
              {harnessShort}
            </text>
          )}
        </g>
      )}
    </g>
  );
}

/** Harness 类型颜色映射（与 DagEditorNode 对齐） */
const HARNESS_COLORS: Record<string, string> = {
  opencode: '#3B82F6',
  local_llm: '#10B981',
  deterministic: '#6B7280',
  codex: '#F59E0B',
  claude_code: '#EC4899',
  kimi: '#06B6D4',
  http: '#8B5CF6',
};

/** 节点模板配置 */
const NODE_TEMPLATES: Array<{
  label: string;
  icon: string;
  color: string;
  type: EditorNode['type'];
  defaultName: string;
  defaultHarness: HarnessType;
}> = [
  { label: 'Agent', icon: '◆', color: '#3B82F6', type: 'agent', defaultName: '新 Agent', defaultHarness: 'opencode' },
  { label: 'Gateway', icon: '◇', color: '#F59E0B', type: 'gateway', defaultName: '条件网关', defaultHarness: 'deterministic' },
  { label: 'Branch', icon: '☰', color: '#06B6D4', type: 'parallel_branch', defaultName: '并行分支', defaultHarness: 'deterministic' },
];

interface DagEditorModalProps {
  editingId: string | null;
  initialYaml: string;
  onSave: (yaml: string) => Promise<void>;
  onClose: () => void;
  saving: boolean;
}

/** BFS 分层布局计算节点位置 */
function computeBfsLayout(nodes: EditorNode[]): Record<string, { x: number; y: number }> {
  const positions: Record<string, { x: number; y: number }> = {};
  if (nodes.length === 0) return positions;

  const ids = nodes.map(n => n.id);
  const afterMap: Record<string, string[]> = {};
  for (const n of nodes) afterMap[n.id] = n.after.filter(a => ids.includes(a));

  // 入度计算
  const inDegree: Record<string, number> = {};
  for (const id of ids) inDegree[id] = 0;
  for (const n of nodes) {
    for (const dep of afterMap[n.id]) {
      inDegree[n.id] = (inDegree[n.id] ?? 0) + 1;
    }
  }

  // BFS 分层
  const levels: Record<string, number> = {};
  const queue: string[] = ids.filter(id => (inDegree[id] ?? 0) === 0);
  for (const r of queue) levels[r] = 0;

  while (queue.length > 0) {
    const cur = queue.shift()!;
    const lvl = levels[cur] ?? 0;
    for (const n of nodes) {
      if (afterMap[n.id]?.includes(cur)) {
        const newLvl = lvl + 1;
        if (levels[n.id] === undefined || levels[n.id] < newLvl) {
          levels[n.id] = newLvl;
          queue.push(n.id);
        }
      }
    }
  }
  for (const id of ids) {
    if (levels[id] === undefined) levels[id] = 0;
  }

  // 按层分组
  const byLevel: Record<number, string[]> = {};
  for (const id of ids) {
    const lvl = levels[id] ?? 0;
    (byLevel[lvl] ??= []).push(id);
  }

  const LEVEL_GAP = 280;
  const INDEX_GAP = 120;
  for (const [lvlStr, list] of Object.entries(byLevel)) {
    const lvl = Number(lvlStr);
    list.forEach((id, idx) => {
      positions[id] = { x: 80 + lvl * LEVEL_GAP, y: 60 + idx * INDEX_GAP };
    });
  }

  return positions;
}

/** 从 EditorWorkflow 构建 ReactFlow 节点和边 */
function buildRfNodes(
  wf: EditorWorkflow,
  positions: Record<string, { x: number; y: number }>,
  selectedId: string | null,
): Node<DagEditorNodeData>[] {
  return wf.nodes.map(n => ({
    id: n.id,
    type: 'editorNode',
    position: positions[n.id] ?? { x: 100, y: 100 },
    data: { node: n, selected: n.id === selectedId },
    selected: n.id === selectedId,
  }));
}

function buildRfEdges(wf: EditorWorkflow): Edge<EditorFlowEdgeData>[] {
  const edges: Edge<EditorFlowEdgeData>[] = [];
  const nodeMap = new Map(wf.nodes.map(n => [n.id, n]));
  for (const node of wf.nodes) {
    for (const dep of node.after) {
      // dep → node (dep 完成后执行 node)
      const sourceNode = nodeMap.get(dep);
      const targetNode = node;
      const sourceColor = sourceNode ? (HARNESS_COLORS[sourceNode.harness] || '#475569') : '#475569';
      const targetColor = HARNESS_COLORS[targetNode.harness] || '#475569';

      // 查找端口名（从 source 的 outputs 中查找指向 target 的 port）
      let portName: string | undefined;
      if (sourceNode) {
        for (const [port, target] of Object.entries(sourceNode.outputs)) {
          const targetStr = Array.isArray(target) ? target.join(',') : target;
          if (targetStr.includes(node.id)) {
            portName = port;
            break;
          }
        }
      }

      edges.push({
        id: `${dep}->${node.id}`,
        source: dep,
        target: node.id,
        type: 'editorFlow',
        data: { sourceColor, targetColor, port: portName },
        markerEnd: { type: MarkerType.ArrowClosed, color: targetColor, width: 16, height: 16 },
      });
    }
  }
  return edges;
}

export function DagEditorModal({ editingId, initialYaml, onSave, onClose, saving }: DagEditorModalProps) {
  const [mode, setMode] = useState<'visual' | 'yaml'>('visual');
  const [editorState, setEditorState] = useState<EditorWorkflow | null>(null);
  const [yamlText, setYamlText] = useState(initialYaml);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [loading, setLoading] = useState(!editingId ? false : true);
  const [error, setError] = useState('');
  const [showYamlPreview, setShowYamlPreview] = useState(false);
  const positionsRef = useRef(positions);
  positionsRef.current = positions;
  const rfInstanceRef = useRef<ReactFlowInstance | null>(null);

  const onFlowInit = useCallback((instance: ReactFlowInstance) => {
    rfInstanceRef.current = instance;
  }, []);

  const handleFitView = useCallback(() => {
    rfInstanceRef.current?.fitView({ padding: 0.2, duration: 300 });
  }, []);

  const handleZoomIn = useCallback(() => {
    rfInstanceRef.current?.zoomIn({ duration: 300 });
  }, []);

  const handleZoomOut = useCallback(() => {
    rfInstanceRef.current?.zoomOut({ duration: 300 });
  }, []);

  // 从 API 加载工作流详情
  useEffect(() => {
    if (!editingId) {
      // 新建工作流：用默认 YAML 初始化
      // 尝试从后端获取解析后的结构，失败则用空状态
      setEditorState({
        workflow_id: 'new-workflow',
        name: '新建工作流',
        version: 1.0,
        description: '',
        inputs: [],
        nodes: [],
        widgets: [],
        rawExtras: {},
      });
      setYamlText(initialYaml);
      setLoading(false);
      return;
    }

    setLoading(true);
    apiClient.getWorkflowDetail(editingId).then((detail) => {
      const state = apiToEditorState(detail);
      setEditorState(state);
      // 加载节点位置：优先用 yaml 里持久化的 layout.nodes，缺失的节点 fallback 到 bfs 自动布局
      // v2026-07-15 D-061：手动拖拽的节点位置持久化到 workflow.yaml 顶层 layout 字段
      const rawLayout = (detail.raw as Record<string, unknown> | undefined)?.layout;
      const savedPositions = (rawLayout && typeof rawLayout === 'object'
        ? (rawLayout as Record<string, unknown>).nodes
        : null) as Record<string, { x: number; y: number }> | null;
      const fallbackPos = computeBfsLayout(state.nodes);
      const mergedPos: Record<string, { x: number; y: number }> = {};
      for (const node of state.nodes) {
        const saved = savedPositions?.[node.id];
        mergedPos[node.id] = saved && typeof saved.x === 'number' && typeof saved.y === 'number'
          ? { x: saved.x, y: saved.y }
          : fallbackPos[node.id] || { x: 0, y: 0 };
      }
      setPositions(mergedPos);
      setYamlText(initialYaml);
      setLoading(false);
    }).catch(() => {
      setError('无法从后端加载工作流详情');
      setLoading(false);
    });
  }, [editingId, initialYaml]);

  // ReactFlow nodes/edges
  const rfNodes = useMemo(
    () => editorState ? buildRfNodes(editorState, positions, selectedNodeId) : [],
    [editorState, positions, selectedNodeId],
  );
  const rfEdges = useMemo(
    () => editorState ? buildRfEdges(editorState) : [],
    [editorState],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(rfNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(rfEdges);

  // 同步外部 nodes/edges 到 ReactFlow 内部 state
  useEffect(() => { setNodes(rfNodes); }, [rfNodes, setNodes]);
  useEffect(() => { setEdges(rfEdges); }, [rfEdges, setEdges]);
  // 同步节点元数据到 window，供 MiniMapLogoNode 自定义渲染用（v11 MiniMap nodeComponent 无 data 入参）
  useEffect(() => {
    const w = window as unknown as { __miniMapNodesById?: Record<string, EditorNode> };
    if (editorState) {
      w.__miniMapNodesById = Object.fromEntries(editorState.nodes.map(n => [n.id, n]));
    }
    return () => { w.__miniMapNodesById = undefined; };
  }, [editorState]);

  // 节点拖拽 → 更新位置
  const onNodeDragStop = useCallback((_: React.MouseEvent, node: Node) => {
    setPositions(prev => ({ ...prev, [node.id]: node.position }));
  }, []);

  // 节点点击 → 选中
  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    setSelectedNodeId(node.id);
  }, []);

  // 画布空白点击 → 取消选中
  const onPaneClick = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  // 连线 → 添加依赖关系（source 完成后执行 target）
  const onConnect = useCallback((conn: Connection) => {
    if (!conn.source || !conn.target || !editorState) return;
    if (conn.source === conn.target) return;

    setEditorState(prev => {
      if (!prev) return prev;
      // 把 source 添加到 target 的 after 数组（如果不存在）
      return {
        ...prev,
        nodes: prev.nodes.map(n => {
          if (n.id === conn.target && !n.after.includes(conn.source!)) {
            return { ...n, after: [...n.after, conn.source!] };
          }
          return n;
        }),
      };
    });
  }, [editorState]);

  // 添加新节点（支持模板）
  const handleAddNode = useCallback((template?: typeof NODE_TEMPLATES[number]) => {
    if (!editorState) return;
    const id = `node_${Date.now().toString(36).slice(-6)}`;
    const newNode: EditorNode = {
      id,
      name: template?.defaultName || '新节点',
      type: template?.type || 'agent',
      agent: template?.type === 'agent' ? null : null,
      harness: template?.defaultHarness || 'opencode',
      model_provider: '',
      model_id: '',
      after: [],
      inputs: [],
      outputs: {},
      domain: null,
      business_role: null,
      role_prompt: null,
      skip_if: null,
      timeout_seconds: null,
      rawFields: {},    // v2026-08-28 D-060：透传 command_config 等未管理字段
    };
    setEditorState(prev => prev ? { ...prev, nodes: [...prev.nodes, newNode] } : prev);
    // 放在画布右侧
    const maxX = Math.max(0, ...editorState.nodes.map(n => positionsRef.current[n.id]?.x ?? 0));
    setPositions(prev => ({ ...prev, [id]: { x: maxX + 280, y: 80 } }));
    setSelectedNodeId(id);
  }, [editorState]);

  // 删除节点
  const handleDeleteNode = useCallback((nodeId: string) => {
    if (!editorState) return;
    setEditorState(prev => {
      if (!prev) return prev;
      // 删除节点 + 清理其他节点的 after 引用
      return {
        ...prev,
        nodes: prev.nodes
          .filter(n => n.id !== nodeId)
          .map(n => ({ ...n, after: n.after.filter(a => a !== nodeId) })),
      };
    });
    setPositions(prev => {
      const next = { ...prev };
      delete next[nodeId];
      return next;
    });
    if (selectedNodeId === nodeId) setSelectedNodeId(null);
  }, [editorState, selectedNodeId]);

  // 更新节点配置
  const handleNodeChange = useCallback((updated: EditorNode) => {
    setEditorState(prev => {
      if (!prev) return prev;
      return {
        ...prev,
        nodes: prev.nodes.map(n => n.id === updated.id ? updated : n),
      };
    });
  }, []);

  // 自动布局
  const handleAutoLayout = useCallback(() => {
    if (!editorState) return;
    setPositions(computeBfsLayout(editorState.nodes));
  }, [editorState]);

  // 切换模式
  const handleModeSwitch = useCallback((newMode: 'visual' | 'yaml') => {
    // visual → yaml: 保留 yamlText（原始 yaml_source，含 inline_agent 等字段）
    // 不再用 serializeWorkflowYaml 覆盖，避免丢失 inline_agent/branches/config 等字段
    setMode(newMode);
  }, []);

  // 保存
  const handleSave = useCallback(async () => {
    let yaml = yamlText;
    if (mode === 'visual' && editorState) {
      // 保存前校验：agent 节点必须有 agent_id 或 role_prompt
      const invalidNodes = editorState.nodes.filter(
        n => n.type === 'agent' && !n.agent && !n.role_prompt
      );
      if (invalidNodes.length > 0) {
        const names = invalidNodes.map(n => n.id).join(', ');
        setError(`以下节点缺少 Agent ID 或角色提示词，无法保存：${names}`);
        setSelectedNodeId(invalidNodes[0].id);
        return;
      }
      yaml = serializeWorkflowYaml({
        ...editorState,
        // v2026-07-15 D-061：手动拖拽的节点位置持久化到 workflow.yaml 顶层 layout 字段
        rawExtras: {
          ...editorState.rawExtras,
          layout: { nodes: positions },
        },
      });
    }
    if (!yaml.trim()) {
      setError('YAML 内容不能为空');
      return;
    }
    try {
      await onSave(yaml);
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败');
    }
  }, [mode, editorState, yamlText, onSave]);

  // 序列化预览：优先使用 yamlText（保留 yaml_source 原始内容，含 inline_agent 等字段）
  // 仅当用户在可视化模式编辑后 yamlText 未同步时，才 fallback 到序列化结果
  const previewYaml = useMemo(() => {
    return yamlText || (editorState ? serializeWorkflowYaml(editorState) : '');
  }, [yamlText, editorState]);

  const selectedNode = editorState?.nodes.find(n => n.id === selectedNodeId) ?? null;

  if (loading) {
    return (
      <div className="dag-editor-overlay">
        <div className="dag-editor-loading">正在加载工作流...</div>
      </div>
    );
  }

  return (
    <div className="dag-editor-overlay" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dag-editor-modal">
        {/* ── 头部栏 ── */}
        <div className="dag-editor-header">
          <div className="dag-editor-header-left">
            <span className="dag-editor-title">
              {editingId ? '编辑工作流' : '创建工作流'}
            </span>
            {editorState && (
              <span className="dag-editor-wf-name">{editorState.name || editorState.workflow_id}</span>
            )}
          </div>
          <div className="dag-editor-header-right">
            {/* 模式切换 */}
            <div className="dag-editor-mode-toggle">
              <button
                className={mode === 'visual' ? 'dag-editor-mode-btn active' : 'dag-editor-mode-btn'}
                onClick={() => handleModeSwitch('visual')}
              >
                可视化
              </button>
              <button
                className={mode === 'yaml' ? 'dag-editor-mode-btn active' : 'dag-editor-mode-btn'}
                onClick={() => handleModeSwitch('yaml')}
              >
                YAML
              </button>
            </div>
            {mode === 'visual' && (
              <button
                className="btn-secondary btn-sm"
                onClick={() => setShowYamlPreview(!showYamlPreview)}
                title="展开/收起 YAML 预览"
              >
                {showYamlPreview ? '隐藏预览' : '预览 YAML'}
              </button>
            )}
            <button className="btn-secondary btn-sm" onClick={onClose}>取消</button>
            <button className="btn-primary btn-sm" onClick={handleSave} disabled={saving}>
              {saving ? '保存中...' : editingId ? '保存修改' : '创建'}
            </button>
          </div>
        </div>

        {error && (
          <div className="dag-editor-error">{error}</div>
        )}

        {/* ── 主体 ── */}
        {mode === 'visual' && editorState ? (
          <div className="dag-editor-body">
            {/* 画布区 */}
            <div className="dag-editor-canvas-area">
              {/* 画布工具栏 */}
              <div className="dag-editor-toolbar">
                {/* 节点模板组 */}
                <div className="dag-editor-template-group">
                  {NODE_TEMPLATES.map(tpl => (
                    <button
                      key={tpl.label}
                      className="dag-editor-template-btn"
                      onClick={() => handleAddNode(tpl)}
                      title={`添加 ${tpl.label} 节点`}
                    >
                      <span className="dag-editor-template-dot" style={{ background: tpl.color }} />
                      {tpl.icon} {tpl.label}
                    </button>
                  ))}
                </div>
                <div className="dag-editor-toolbar-separator" />
                <button className="dag-editor-tool-btn" onClick={handleAutoLayout} title="自动布局">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>
                  自动布局
                </button>
                <div className="dag-editor-toolbar-separator" />
                {/* 视图控制 */}
                <button className="dag-editor-tool-btn" onClick={handleZoomIn} title="放大">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /><line x1="11" y1="8" x2="11" y2="14" /><line x1="8" y1="11" x2="14" y2="11" /></svg>
                </button>
                <button className="dag-editor-tool-btn" onClick={handleZoomOut} title="缩小">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /><line x1="8" y1="11" x2="14" y2="11" /></svg>
                </button>
                <button className="dag-editor-tool-btn" onClick={handleFitView} title="适应视图">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2" /><path d="M17 3h2a2 2 0 0 1 2 2v2" /><path d="M21 17v2a2 2 0 0 1-2 2h-2" /><path d="M7 21H5a2 2 0 0 1-2-2v-2" /></svg>
                </button>
                <span className="dag-editor-node-count">
                  {editorState.nodes.length} 节点 · {rfEdges.length} 连接
                </span>
              </div>

              {/* ReactFlow 画布 */}
              <div className="dag-editor-canvas">
                {editorState.nodes.length === 0 && (
                  <div className="dag-editor-empty-state">
                    <div className="dag-editor-empty-icon">
                      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>
                    </div>
                    <div className="dag-editor-empty-title">画布为空</div>
                    <div className="dag-editor-empty-desc">
                      点击上方工具栏的模板按钮添加节点，然后拖拽节点间的连接点建立依赖关系。
                    </div>
                  </div>
                )}
                <ReactFlow
                  nodes={nodes}
                  edges={edges}
                  nodeTypes={nodeTypes}
                  edgeTypes={edgeTypes}
                  onNodesChange={onNodesChange}
                  onEdgesChange={onEdgesChange}
                  onConnect={onConnect}
                  onNodeClick={onNodeClick}
                  onNodeDragStop={onNodeDragStop}
                  onPaneClick={onPaneClick}
                  onInit={onFlowInit}
                  fitView
                  nodesDraggable
                  nodesConnectable
                  elementsSelectable
                  deleteKeyCode={null}
                >
                  <Background gap={16} size={1.2} color="#1E293B" />
                  <MiniMap
                    nodeColor={(n) => {
                      // 用类型色半透明填充，像协作可视化节点徽章
                      const data = n.data as DagEditorNodeData;
                      const nType = (data?.node?.type ?? '').toLowerCase();
                      const typeBase: Record<string, string> = {
                        agent: 'rgba(245, 158, 11, 0.22)',
                        command: 'rgba(96, 165, 250, 0.18)',
                        await_command: 'rgba(96, 165, 250, 0.18)',
                        while: 'rgba(167, 139, 250, 0.18)',
                        gateway: 'rgba(52, 211, 153, 0.18)',
                        parallel_branch: 'rgba(52, 211, 153, 0.18)',
                      };
                      return typeBase[nType] ?? '#1E293B';
                    }}
                    nodeStrokeColor={(n) => {
                      const data = n.data as DagEditorNodeData;
                      const nType = (data?.node?.type ?? '').toLowerCase();
                      const stroke: Record<string, string> = {
                        agent: '#F59E0B',
                        command: '#60A5FA',
                        await_command: '#60A5FA',
                        while: '#A78BFA',
                        gateway: '#34D399',
                        parallel_branch: '#34D399',
                      };
                      return data?.selected ? '#FFFFFF' : (stroke[nType] ?? '#475569');
                    }}
                    nodeStrokeWidth={1.5}
                    nodeBorderRadius={4}
                    maskColor="rgba(11, 15, 20, 0.42)"
                    maskStrokeColor="rgba(99, 102, 241, 0.8)"
                    maskStrokeWidth={1.5}
                    nodeComponent={MiniMapLogoNode}
                    pannable
                    zoomable
                    style={{
                      background: 'linear-gradient(135deg, #0B0F14 0%, #0F172A 100%)',
                      border: '1px solid rgba(99, 102, 241, 0.3)',
                      borderRadius: 8,
                      boxShadow: '0 4px 16px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255,255,255,0.05)',
                    }}
                  />
                  <Controls showInteractive={true} />
                </ReactFlow>
              </div>

              {/* YAML 预览（可折叠） */}
              {showYamlPreview && (
                <div className="dag-editor-yaml-preview">
                  <div className="dag-editor-yaml-preview-header">
                    <span>YAML 预览（只读）</span>
                    <button
                      className="dag-editor-copy-btn"
                      onClick={() => navigator.clipboard.writeText(previewYaml)}
                    >
                      复制
                    </button>
                  </div>
                  <pre className="dag-editor-yaml-code">{previewYaml}</pre>
                </div>
              )}
            </div>

            {/* 右侧配置面板 */}
            <div className="dag-editor-panel-area">
              {selectedNode ? (
                <NodeConfigPanel
                  node={selectedNode}
                  allNodeIds={editorState.nodes.map(n => n.id)}
                  onChange={handleNodeChange}
                  onDelete={handleDeleteNode}
                />
              ) : (
                <WorkflowOverviewPanel
                  state={editorState}
                  onChange={setEditorState}
                />
              )}
            </div>
          </div>
        ) : (
          /* YAML 编辑模式 */
          <div className="dag-editor-yaml-mode">
            <div className="dag-editor-yaml-hint">
              编辑 YAML 工作流定义。保存时后端会自动验证拓扑结构和依赖完整性。
              切换回「可视化」模式后，改动需要重新加载才能同步。
            </div>
            <textarea
              className="input-base dag-editor-yaml-textarea"
              value={yamlText}
              onChange={e => { setYamlText(e.target.value); setError(''); }}
              placeholder="在此输入 YAML 工作流定义..."
              spellCheck={false}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * WorkflowOverviewPanel — 未选中节点时显示工作流概览。
 * 可编辑工作流级别的元数据（id, name, version, description, inputs）。
 */
function WorkflowOverviewPanel({
  state,
  onChange,
}: {
  state: EditorWorkflow;
  onChange: (state: EditorWorkflow) => void;
}) {
  const [localInputs, setLocalInputs] = useState('');

  useEffect(() => {
    setLocalInputs(state.inputs.map(i => i.name).join(', '));
  }, [state.inputs]);

  return (
    <div className="node-config-panel">
      <div className="ncp-header">
        <span className="ncp-title">工作流概览</span>
      </div>
      <div className="ncp-body">
        <section className="ncp-section">
          <h4 className="ncp-section-title">基本信息</h4>
          <div className="ncp-field">
            <label>工作流 ID</label>
            <input
              className="input-base ncp-input"
              value={state.workflow_id}
              onChange={e => onChange({ ...state, workflow_id: e.target.value })}
            />
          </div>
          <div className="ncp-field">
            <label>名称</label>
            <input
              className="input-base ncp-input"
              value={state.name}
              onChange={e => onChange({ ...state, name: e.target.value })}
            />
          </div>
          <div className="ncp-field">
            <label>版本</label>
            <input
              className="input-base ncp-input"
              type="number"
              step="0.1"
              value={state.version}
              onChange={e => onChange({ ...state, version: parseFloat(e.target.value) || 1.0 })}
            />
          </div>
          <div className="ncp-field">
            <label>描述</label>
            <textarea
              className="input-base ncp-textarea"
              value={state.description}
              onChange={e => onChange({ ...state, description: e.target.value })}
              rows={3}
            />
          </div>
        </section>

        <section className="ncp-section">
          <h4 className="ncp-section-title">输入参数</h4>
          <div className="ncp-field">
            <label>参数名 (逗号分隔)</label>
            <input
              className="input-base ncp-input ncp-mono"
              value={localInputs}
              onChange={e => {
                setLocalInputs(e.target.value);
                const names = e.target.value.split(',').map(s => s.trim()).filter(Boolean);
                onChange({
                  ...state,
                  inputs: names.map(name => {
                    const existing = state.inputs.find(i => i.name === name);
                    return existing ?? { name, type: 'string', required: false };
                  }),
                });
              }}
              placeholder="topic, log_source_id"
            />
          </div>
        </section>

        <section className="ncp-section">
          <h4 className="ncp-section-title">统计</h4>
          <div className="ncp-stats">
            <div className="ncp-stat">
              <span className="ncp-stat-value">{state.nodes.length}</span>
              <span className="ncp-stat-label">节点</span>
            </div>
            <div className="ncp-stat">
              <span className="ncp-stat-value">{state.widgets.length}</span>
              <span className="ncp-stat-label">Widget</span>
            </div>
            <div className="ncp-stat">
              <span className="ncp-stat-value">{state.inputs.length}</span>
              <span className="ncp-stat-label">输入参数</span>
            </div>
          </div>
        </section>

        {state.widgets.length > 0 && (
          <section className="ncp-section">
            <h4 className="ncp-section-title">Widget 列表</h4>
            <div className="ncp-widget-list">
              {state.widgets.map(w => (
                <div key={w.id} className="ncp-widget-item">
                  <span className="ncp-widget-type">{w.type}</span>
                  <span className="ncp-widget-title">{w.title}</span>
                  <span className="ncp-widget-node">@{w.emit_on_node}</span>
                </div>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
