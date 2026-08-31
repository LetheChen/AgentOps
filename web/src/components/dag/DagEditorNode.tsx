import { memo } from 'react';
import { Handle, Position, type NodeProps } from 'reactflow';
import type { EditorNode, HarnessType } from '../../lib/workflowYaml';

/**
 * DagEditorNode — 可视化编辑器专用 ReactFlow 节点。
 *
 * 与运行时 DagNode 区别：
 *   - 可拖拽、可选择、可连线
 *   - 显示编辑属性（agent / harness / model）
 *   - harness 类型用颜色标签区分
 *   - 选中态高亮蓝边框
 *
 * v2 增强：
 *   - 顶部 harness 色彩渐变条（CSS ::before 驱动）
 *   - 状态指示点（右上角发光圆点）
 *   - 端口指示器增强（彩色 chip + 数量徽章）
 *   - 依赖关系可视化（after 数量提示）
 */

export interface DagEditorNodeData {
  node: EditorNode;
  selected?: boolean;
}

const HARNESS_COLORS: Record<string, string> = {
  opencode: '#3B82F6',
  local_llm: '#10B981',
  deterministic: '#6B7280',
  codex: '#F59E0B',
  claude_code: '#EC4899',
  kimi: '#06B6D4',
  http: '#8B5CF6',
};

const NODE_TYPE_LABELS: Record<string, string> = {
  agent: 'Agent',
  parallel_branch: 'Branch',
  gateway: 'Gateway',
};

const NODE_TYPE_ICONS: Record<string, string> = {
  agent: '◆',
  parallel_branch: '☰',
  gateway: '◇',
};

function DagEditorNodeInner({ data, selected }: NodeProps<DagEditorNodeData>) {
  const { node } = data;
  const harnessColor = HARNESS_COLORS[node.harness] || '#6B7280';
  const isSelected = selected || data.selected;
  const outputPortCount = Object.keys(node.outputs).length;
  const depCount = node.after.length;

  return (
    <div
      className="dag-editor-node"
      style={{
        '--node-color': harnessColor,
        borderColor: isSelected ? '#3B82F6' : `${harnessColor}66`,
        boxShadow: isSelected
          ? '0 0 0 2px rgba(59, 130, 246, 0.4), 0 4px 16px rgba(59, 130, 246, 0.2)'
          : '0 2px 8px rgba(0,0,0,0.3)',
      } as React.CSSProperties}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="dag-editor-handle"
        style={{ background: harnessColor }}
      />

      {/* 状态指示点 */}
      <span className="dag-editor-node-status-dot" />

      {/* 节点头部：类型图标 + 名称 + 类型标签 */}
      <div className="dag-editor-node-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '5px', minWidth: 0 }}>
          <span style={{ color: harnessColor, fontSize: '12px', flexShrink: 0 }}>
            {NODE_TYPE_ICONS[node.type] || '◆'}
          </span>
          <span className="dag-editor-node-name">{node.name}</span>
        </div>
        <span className="dag-editor-node-type">{NODE_TYPE_LABELS[node.type] || node.type}</span>
      </div>

      {/* 节点 ID */}
      <div className="dag-editor-node-id">{node.id}</div>

      {/* 节点详情 */}
      <div className="dag-editor-node-details">
        {node.agent && (
          <div className="dag-editor-node-row">
            <span className="dag-editor-node-label">agent</span>
            <span className="dag-editor-node-value">{node.agent}</span>
          </div>
        )}
        <div className="dag-editor-node-row">
          <span className="dag-editor-node-label">harness</span>
          <span
            className="dag-editor-node-badge"
            style={{ background: `${harnessColor}22`, color: harnessColor, borderColor: `${harnessColor}44` }}
          >
            {node.harness}
          </span>
        </div>
        {(node.model_provider || node.model_id) && (
          <div className="dag-editor-node-row">
            <span className="dag-editor-node-label">model</span>
            <span className="dag-editor-node-value dag-editor-node-mono">
              {node.model_provider && node.model_id ? `${node.model_provider}/${node.model_id}` : node.model_id || node.model_provider}
            </span>
          </div>
        )}
        {node.business_role && (
          <div className="dag-editor-node-row">
            <span className="dag-editor-node-label">role</span>
            <span className="dag-editor-node-value">{node.business_role}</span>
          </div>
        )}
      </div>

      {/* 底部指标栏：依赖数 + 端口数 */}
      <div className="dag-editor-node-footer">
        {depCount > 0 && (
          <span className="dag-editor-node-meta">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 3H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h2" />
              <path d="M15 3h4a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-2" />
              <line x1="12" y1="7" x2="12" y2="13" />
              <polyline points="9 10 12 13 15 10" />
            </svg>
            {depCount} dep{depCount > 1 ? 's' : ''}
          </span>
        )}
        {outputPortCount > 0 && (
          <span className="dag-editor-node-meta">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="6" cy="12" r="2" />
              <circle cx="18" cy="6" r="2" />
              <circle cx="18" cy="18" r="2" />
              <line x1="8" y1="11" x2="16" y2="7" />
              <line x1="8" y1="13" x2="16" y2="17" />
            </svg>
            {outputPortCount} port{outputPortCount > 1 ? 's' : ''}
          </span>
        )}
        {node.skip_if && (
          <span className="dag-editor-node-meta dag-editor-node-meta-skip" title={node.skip_if}>
            ⏭ skip
          </span>
        )}
      </div>

      {/* 端口指示器 */}
      {outputPortCount > 0 && (
        <div className="dag-editor-node-ports">
          {Object.keys(node.outputs).map(port => (
            <span key={port} className="dag-editor-port-chip">{port}</span>
          ))}
        </div>
      )}

      <Handle
        type="source"
        position={Position.Right}
        className="dag-editor-handle"
        style={{ background: harnessColor }}
      />
    </div>
  );
}

export const DagEditorNode = memo(DagEditorNodeInner);
export default DagEditorNode;
