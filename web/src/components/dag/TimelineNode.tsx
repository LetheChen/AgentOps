import { memo } from 'react';
import { Handle, Position, type NodeProps } from 'reactflow';

/**
 * TimelineNode — 时间线专用节点。
 *
 * 与 DagNode 不同：
 * - 横向色条（width = 持续时间 × 时间轴缩放）
 * - 左侧固定位置显示节点标签 + agent
 * - 不参与 ReactFlow 自动布局（节点位置由 buildTimelineLayout 预设）
 */

export interface TimelineNodeData {
  label: string;
  nodeId: string;
  status: string;
  width: number;
  durationMs?: number | null;
  agentId?: string;
}

const STATUS_COLORS: Record<string, string> = {
  completed: '#10B981',
  running: '#3B82F6',
  failed: '#EF4444',
  pending: '#475569',
  ready: '#FBBF24',
  skipped: '#6B7280',
};

// 时间线节点行布局常量
const ROW_HEIGHT = 90;
const LEFT_LABEL_WIDTH = 180;

function TimelineNodeInner({ data }: NodeProps<TimelineNodeData>) {
  const color = STATUS_COLORS[data.status] ?? STATUS_COLORS.pending;
  const isRunning = data.status === 'running';

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 0 }}>
      {/* 左侧：节点标签列（绝对定位到节点左侧） */}
      <div
        style={{
          position: 'absolute',
          left: -LEFT_LABEL_WIDTH,
          width: LEFT_LABEL_WIDTH - 20,
          padding: '6px 10px',
          fontSize: '12px',
          color: '#94A3B8',
          textAlign: 'right',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          pointerEvents: 'none',
        }}
      >
        <div style={{ fontWeight: 600, color: '#E2E8F0' }}>{data.label}</div>
        {data.agentId && (
          <div style={{ fontSize: '10px', fontFamily: 'monospace', color: '#94A3B8' }}>
            {data.agentId}
          </div>
        )}
      </div>

      {/* 节点条：横向色条 */}
      <div
        style={{
          width: Math.max(80, data.width),
          height: 32,
          borderRadius: '6px',
          background: `${color}22`,
          border: `2px solid ${color}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: '11px',
          color: '#E2E8F0',
          boxShadow: isRunning ? `0 0 12px ${color}66` : 'none',
          animation: isRunning ? 'dag-node-pulse 2s ease-in-out infinite' : undefined,
        }}
      >
        {data.durationMs != null ? `${(data.durationMs / 1000).toFixed(1)}s` : isRunning ? '⟳' : '...'}
      </div>

      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

export const TimelineNode = memo(TimelineNodeInner);
export default TimelineNode;

// 导出供布局函数使用
export const TIMELINE_CONSTANTS = { ROW_HEIGHT, LEFT_LABEL_WIDTH };