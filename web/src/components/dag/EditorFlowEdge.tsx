import { memo } from 'react';
import { BaseEdge, getSmoothStepPath, EdgeLabelRenderer, type EdgeProps } from 'reactflow';

/**
 * EditorFlowEdge — DAG 编辑器专用边（v2 简洁设计）。
 *
 * 边线设计：
 *   - 单层贝塞尔曲线，无底层光晕
 *   - 渐变色描边（source → target harness 色，柔和半透明）
 *   - 虚线流动动画（数据流向指示）
 *   - 端口名标签
 *   - 箭头标记
 */

export interface EditorFlowEdgeData {
  sourceColor?: string;
  targetColor?: string;
  port?: string;
}

function EditorFlowEdgeInner({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  markerEnd,
}: EdgeProps<EditorFlowEdgeData>) {
  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 12,
  });

  const sourceColor = data?.sourceColor || 'rgba(148, 178, 214, 0.4)';
  const targetColor = data?.targetColor || 'rgba(148, 178, 214, 0.4)';
  const gradientId = `edge-grad-${id}`;

  return (
    <>
      <defs>
        <linearGradient id={gradientId} x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stopColor={sourceColor} stopOpacity={0.6} />
          <stop offset="100%" stopColor={targetColor} stopOpacity={0.6} />
        </linearGradient>
      </defs>
      {/* 单层边线（简洁风格） */}
      <BaseEdge
        id={id}
        path={edgePath}
        style={{
          stroke: `url(#${gradientId})`,
          strokeWidth: 1.8,
          strokeDasharray: '6 4',
          animation: 'editor-edge-flow 1.2s linear infinite',
        }}
        markerEnd={markerEnd}
      />
      {data?.port && (
        <EdgeLabelRenderer>
          <div
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              background: 'rgba(10, 15, 24, 0.92)',
              border: '1px solid rgba(148, 178, 214, 0.12)',
              borderRadius: '4px',
              padding: '1px 6px',
              fontSize: '9px',
              fontFamily: 'var(--font-mono, monospace)',
              color: 'rgba(148, 178, 214, 0.56)',
              pointerEvents: 'none',
              whiteSpace: 'nowrap',
            }}
          >
            {data.port}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export const EditorFlowEdge = memo(EditorFlowEdgeInner);
export default EditorFlowEdge;
