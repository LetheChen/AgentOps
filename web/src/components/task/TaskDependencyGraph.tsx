// web/src/components/task/TaskDependencyGraph.tsx
// V1 任务依赖图（reactflow + 内置 LR 分层布局，无 dagre 依赖）
// 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.8.2
//
// 约束遵循 CLAUDE.md：
// - edge id 全局唯一含 source/target/relation_type
// - wrap 显式 minHeight
// - colWidth 按 label 字符数自适应

import { useMemo, useRef, useEffect, useState } from 'react';
import { ReactFlow, Background, Controls, MiniMap, type Node, type Edge } from 'reactflow';
import 'reactflow/dist/style.css';
import type { Task, TaskRelation } from '../../api/taskApi';

// 状态 → 背景色（与看板配色协调，深色主题友好）
const STATUS_BG: Record<string, string> = {
  idea: '#3a3a4a',
  backlog: '#2a3550',
  discussing: '#2d4a3a',
  reviewing: '#4a3a2a',
  closed: '#1a3a2a',
  decomposing: '#2a3a4a',
  in_progress: '#1e3a5f',
  validating: '#3a2a4a',
  closing: '#4a4a2a',
  paused: '#3a3a3a',
  blocked: '#4a2a2a',
  failed: '#5a1a1a',
  canceled: '#2a2a2a',
  abandoned: '#1a1a1a',
};

const STATUS_BORDER: Record<string, string> = {
  closed: '#43a047',
  in_progress: '#1e88e5',
  reviewing: '#fb8c00',
  blocked: '#e53935',
  failed: '#e53935',
};

// 节点尺寸：按 label 字符数估算（中文按 14px/字，最小 120 最大 280）
function nodeSize(label: string): { width: number; height: number } {
  const w = Math.min(280, Math.max(120, label.length * 14 + 24));
  return { width: w, height: 56 };
}

// 简单 LR 分层布局（替代 dagre）：
// 1. 计算每个节点的 depth（最长路径 from root）
// 2. 按 depth 分层
// 3. x = depth * colWidth, y = level 内序号 * rowHeight
function layoutLR(
  tasks: Task[],
  relations: TaskRelation[],
): Node[] {
  const taskIds = new Set(tasks.map((t) => t.task_id));
  const children = new Map<string, string[]>();
  const inDegree = new Map<string, number>();
  tasks.forEach((t) => {
    children.set(t.task_id, []);
    inDegree.set(t.task_id, 0);
  });
  relations.forEach((r) => {
    if (!taskIds.has(r.source_task_id) || !taskIds.has(r.target_task_id)) return;
    children.get(r.source_task_id)!.push(r.target_task_id);
    inDegree.set(r.target_task_id, (inDegree.get(r.target_task_id) || 0) + 1);
  });

  // BFS 计算每个节点的 depth（从入度为 0 的节点开始）
  const depth = new Map<string, number>();
  const queue: string[] = [];
  tasks.forEach((t) => {
    if ((inDegree.get(t.task_id) || 0) === 0) {
      depth.set(t.task_id, 0);
      queue.push(t.task_id);
    }
  });
  // 孤立节点 depth=0
  tasks.forEach((t) => {
    if (!depth.has(t.task_id)) depth.set(t.task_id, 0);
  });

  // 拓扑遍历计算 depth（取最长路径）
  const processed = new Set<string>();
  while (queue.length > 0) {
    const cur = queue.shift()!;
    if (processed.has(cur)) continue;
    processed.add(cur);
    const curDepth = depth.get(cur) || 0;
    for (const child of children.get(cur) || []) {
      const childDepth = depth.get(child) || 0;
      depth.set(child, Math.max(childDepth, curDepth + 1));
      // 重新入队以确保子节点被处理
      if (!processed.has(child)) queue.push(child);
    }
  }

  // 按 depth 分层
  const levels = new Map<number, string[]>();
  tasks.forEach((t) => {
    const d = depth.get(t.task_id) || 0;
    if (!levels.has(d)) levels.set(d, []);
    levels.get(d)!.push(t.task_id);
  });

  // 排序层级
  const sortedDepths = Array.from(levels.keys()).sort((a, b) => a - b);
  const colWidth = 280;
  const rowHeight = 80;
  const nodes: Node[] = [];

  sortedDepths.forEach((d) => {
    const levelTasks = levels.get(d)!;
    levelTasks.forEach((tid, idx) => {
      const t = tasks.find((x) => x.task_id === tid)!;
      const label = `${t.identifier || tid.slice(-6)} · ${t.title}`;
      const { width, height } = nodeSize(label);
      nodes.push({
        id: tid,
        data: { label },
        position: { x: d * colWidth + 20, y: idx * rowHeight + 20 },
        style: {
          background: STATUS_BG[t.status] || '#2a2a2a',
          border: `1px solid ${STATUS_BORDER[t.status] || 'var(--color-border-default)'}`,
          color: '#e0e0e0',
          width,
          height,
          fontSize: 12,
          borderRadius: '6px',
        },
      });
    });
  });

  return nodes;
}

export default function TaskDependencyGraph({
  tasks,
  relations,
}: {
  tasks: Task[];
  relations: TaskRelation[];
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [wrapWidth, setWrapWidth] = useState(800);

  // 实测容器宽度（resize 自适应）
  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver((ents) => {
      if (ents[0]) setWrapWidth(ents[0].contentRect.width);
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  const edges = useMemo<Edge[]>(
    () =>
      relations.map((r) => ({
        // edge id 含 source/target/relation_type，全局唯一
        id: `e-${r.source_task_id}__${r.target_task_id}__${r.relation_type}`,
        source: r.source_task_id,
        target: r.target_task_id,
        label: r.relation_type,
        animated: r.relation_type === 'blocks',
        style: { stroke: r.relation_type === 'blocks' ? '#e53935' : '#666', strokeWidth: 1.5 },
        labelStyle: { fontSize: 10, fill: '#999' },
      })),
    [relations],
  );

  const nodes = useMemo(
    () => layoutLR(tasks, relations),
    [tasks, relations],
  );

  // 空图提示
  if (tasks.length === 0) {
    return (
      <div
        ref={wrapRef}
        style={{
          minHeight: 200,
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--color-text-tertiary)',
          fontSize: 13,
          background: 'var(--color-bg-surface)',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-border-subtle)',
        }}
      >
        暂无任务，无法渲染依赖图。
      </div>
    );
  }

  // wrap 显式 minHeight，避免空图塌缩
  return (
    <div ref={wrapRef} style={{ minHeight: 400, width: wrapWidth, height: 400 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#2a2a2a" gap={16} />
        <Controls showInteractive={false} />
        <MiniMap
          nodeColor={(n) => (n.style?.background as string) || '#2a2a2a'}
          maskColor="rgba(0,0,0,0.7)"
          style={{ background: '#1a1a1a' }}
        />
      </ReactFlow>
    </div>
  );
}
