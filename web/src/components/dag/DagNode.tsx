import { memo } from 'react';
import { Handle, Position, type NodeProps } from 'reactflow';

/**
 * DagNode — 自定义 ReactFlow 节点组件。
 *
 * 特性：
 *   - 状态光晕：running 节点蓝色脉冲动画，failed 节点红色光晕
 *   - 徽章：token 用量 + 工具调用次数
 *   - 紧凑设计：节点名 + 副标题 + 状态指示
 */

export interface DagNodeData {
  label: string;
  status?: string;
  subtitle?: string;
  tokensIn?: number;
  tokensOut?: number;
  toolCalls?: number;
  provider?: string;
  errorType?: string;
}

const STATUS_STYLES: Record<string, { color: string; bg: string; glow: string }> = {
  pending: { color: '#64748B', bg: 'rgba(30, 41, 59, 0.8)', glow: 'none' },
  ready: { color: '#FBBF24', bg: 'rgba(251, 191, 36, 0.08)', glow: 'none' },
  waiting: { color: '#A78BFA', bg: 'rgba(167, 139, 250, 0.08)', glow: 'none' },
  running: { color: '#3B82F6', bg: 'rgba(59, 130, 246, 0.12)', glow: '0 0 16px rgba(59, 130, 246, 0.6)' },
  completed: { color: '#10B981', bg: 'rgba(16, 185, 129, 0.08)', glow: 'none' },
  failed: { color: '#EF4444', bg: 'rgba(239, 68, 68, 0.12)', glow: '0 0 16px rgba(239, 68, 68, 0.5)' },
  skipped: { color: '#6B7280', bg: 'rgba(30, 41, 59, 0.5)', glow: 'none' },
};

function DagNodeInner({ data }: NodeProps<DagNodeData>) {
  const status = data.status || 'pending';
  const style = STATUS_STYLES[status] || STATUS_STYLES.pending;
  const isRunning = status === 'running';
  const isFailed = status === 'failed';
  const totalTokens = (data.tokensIn || 0) + (data.tokensOut || 0);

  return (
    <div
      style={{
        background: style.bg,
        border: `${isRunning ? 2 : 1.5}px solid ${style.color}`,
        borderRadius: '10px',
        padding: '10px 14px',
        minWidth: '140px',
        maxWidth: '200px',
        boxShadow: style.glow !== 'none'
          ? style.glow
          : '0 4px 12px rgba(0, 0, 0, 0.3)',
        animation: isRunning ? 'dag-node-pulse 2s ease-in-out infinite' : undefined,
        transition: 'box-shadow 0.3s, border-color 0.3s',
      }}
    >
      {/* 输入连接点 */}
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: style.color, width: 8, height: 8, border: 'none' }}
      />

      {/* 节点标题 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
        <div
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: style.color,
            flexShrink: 0,
            animation: isRunning ? 'dag-dot-blink 1s ease-in-out infinite' : undefined,
          }}
        />
        <span style={{ fontSize: '13px', fontWeight: 600, color: '#E2E8F0', lineHeight: 1.2 }}>
          {data.label}
        </span>
      </div>

      {/* 副标题 */}
      {data.subtitle && (
        <div style={{ fontSize: '11px', color: '#94A3B8', marginBottom: '4px' }}>
          {data.subtitle}
        </div>
      )}

      {/* 徽章区：token + tool_calls + provider */}
      <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
        {totalTokens > 0 && (
          <span style={badgeStyle('#3B82F6')}>
            {totalTokens > 1000 ? `${(totalTokens / 1000).toFixed(1)}k` : totalTokens} tok
          </span>
        )}
        {(data.toolCalls || 0) > 0 && (
          <span style={badgeStyle('#8B5CF6')}>
            {data.toolCalls} tools
          </span>
        )}
        {data.provider && isFailed && (
          <span style={badgeStyle('#EF4444')} title={`Provider: ${data.provider}`}>
            {data.provider}
          </span>
        )}
      </div>

      {/* 失败时的错误类型 */}
      {isFailed && data.errorType && (
        <div style={{ fontSize: '10px', color: '#FCA5A5', marginTop: '4px', fontFamily: 'monospace' }}>
          {data.errorType}
        </div>
      )}

      {/* 输出连接点 */}
      <Handle
        type="source"
        position={Position.Right}
        style={{ background: style.color, width: 8, height: 8, border: 'none' }}
      />
    </div>
  );
}

function badgeStyle(color: string): React.CSSProperties {
  return {
    fontSize: '10px',
    fontWeight: 500,
    padding: '1px 6px',
    borderRadius: '4px',
    background: `${color}22`,
    color,
    border: `1px solid ${color}44`,
  };
}

export const DagNode = memo(DagNodeInner);
export default DagNode;
