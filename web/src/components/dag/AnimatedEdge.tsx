import { memo } from 'react';
import { BaseEdge, getSmoothStepPath, type EdgeProps } from 'reactflow';

/**
 * AnimatedEdge — 自定义 ReactFlow 边组件。
 *
 * 特性：
 *   - running 状态：蓝色虚线流动动画（数据正在流动）
 *   - completed 状态：绿色淡入（数据已传输）
 *   - failed 状态：红色静态（传输中断）
 *   - pending 状态：灰色静态（等待传输）
 */

export interface AnimatedEdgeData {
  targetStatus?: string;
}

function AnimatedEdgeInner({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  style,
}: EdgeProps<AnimatedEdgeData>) {
  const status = data?.targetStatus || 'pending';

  const [edgePath] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 8,
  });

  let edgeStyle: React.CSSProperties = { strokeWidth: 1.5, ...style };
  let markerEnd: string | undefined;

  switch (status) {
    case 'running':
      edgeStyle = {
        ...edgeStyle,
        stroke: '#3B82F6',
        strokeWidth: 2.5,
        strokeDasharray: '6 3',
        animation: 'dag-edge-flow 0.8s linear infinite',
      };
      break;
    case 'completed':
      edgeStyle = {
        ...edgeStyle,
        stroke: '#10B981',
        strokeWidth: 1.5,
        opacity: 0.6,
      };
      break;
    case 'failed':
      edgeStyle = {
        ...edgeStyle,
        stroke: '#EF4444',
        strokeWidth: 1.5,
      };
      break;
    default:
      edgeStyle = {
        ...edgeStyle,
        stroke: '#334155',
        strokeWidth: 1.5,
      };
  }

  return <BaseEdge id={id} path={edgePath} style={edgeStyle} markerEnd={markerEnd} />;
}

export const AnimatedEdge = memo(AnimatedEdgeInner);
export default AnimatedEdge;
