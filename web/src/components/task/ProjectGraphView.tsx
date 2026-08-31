// web/src/components/task/ProjectGraphView.tsx
// V3 项目级网状图（§4.11.4 X9）：Obsidian 风格全项目任务关系网
// - ReactFlow 力导向式分层布局（parent 定层级，blocks 叠加交叉边）
// - 节点 = 任务（状态着色），边 = parent（灰）/ blocks（红）
// - 点击节点 → 直达任务详情

import { useState, useEffect, useMemo, useCallback } from 'react';
import ReactFlow, {
  type Node, type Edge, type NodeMouseHandler,
  Background, Controls, MiniMap,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { taskApi, type Task, type TaskRelation } from '../../api/taskApi';

const STATUS_LABELS: Record<string, string> = {
  idea: '灵感', backlog: '待办池', discussing: '讨论中', decomposing: '拆解中',
  in_progress: '进行中', blocked: '被阻塞', validating: '验证中',
  reviewing: '评审中', closing: '关闭中', closed: '已关闭',
  canceled: '已取消', abandoned: '已废弃',
};
const STATUS_COLORS: Record<string, { bg: string; border: string }> = {
  closed: { bg: '#1b3a25', border: '#43a047' },
  blocked: { bg: '#3a1b1b', border: '#e53935' },
  in_progress: { bg: '#3a2e1b', border: '#fb8c00' },
  reviewing: { bg: '#3a2e1b', border: '#fb8c00' },
  validating: { bg: '#3a2e1b', border: '#fb8c00' },
};
const DEFAULT_COLOR = { bg: '#1e2430', border: '#4a5568' };

// 分层布局：parent 定层级（根=0），同层横向排开；无关系任务单独一行
function layoutNodes(tasks: Task[], relations: TaskRelation[]): Node[] {
  const parentOf = new Map<string, string>();
  relations.forEach((r) => {
    if (r.relation_type === 'parent') {
      parentOf.set(r.target_task_id, r.source_task_id);
    }
  });

  // 计算层级（防环：最多遍历深度 = 任务数）
  const depth = (id: string, seen = new Set<string>()): number => {
    if (seen.has(id)) return 0; // 环保护
    seen.add(id);
    const p = parentOf.get(id);
    if (!p) return 0;
    return depth(p, seen) + 1;
  };

  const byDepth = new Map<number, Task[]>();
  tasks.forEach((t) => {
    const d = depth(t.task_id);
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d)!.push(t);
  });

  const nodes: Node[] = [];
  const ROW_H = 110;
  byDepth.forEach((rowTasks, d) => {
    rowTasks.forEach((t, i) => {
      const c = STATUS_COLORS[t.status] || DEFAULT_COLOR;
      nodes.push({
        id: t.task_id,
        data: { label: `${t.identifier || t.task_id.slice(-6)}\n${t.title}\n[${STATUS_LABELS[t.status] || t.status}]` },
        position: {
          x: (i - (rowTasks.length - 1) / 2) * 200 + 400,
          y: 40 + d * ROW_H,
        },
        style: {
          background: c.bg,
          border: `1.5px solid ${c.border}`,
          color: '#e0e0e0',
          width: 170, height: 58, fontSize: 11,
          borderRadius: '8px', textAlign: 'center',
          lineHeight: 1.35, whiteSpace: 'pre-line',
        },
      });
    });
  });
  return nodes;
}

export default function ProjectGraphView({
  projectId,
  onOpenTask,
}: {
  projectId: string;
  onOpenTask: (taskId: string) => void;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [relations, setRelations] = useState<TaskRelation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!projectId) return;
    try {
      const data = await taskApi.graph(projectId);
      setTasks(data.tasks || []);
      setRelations(data.relations || []);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载网状图失败');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  const nodes = useMemo(() => layoutNodes(tasks, relations), [tasks, relations]);
  const edges = useMemo<Edge[]>(() =>
    relations.map((r) => {
      const isBlocks = r.relation_type === 'blocks';
      return {
        id: `e-${r.relation_id}`,
        source: r.source_task_id,
        target: r.target_task_id,
        label: isBlocks ? '阻断' : '',
        animated: isBlocks,
        style: { stroke: isBlocks ? '#e53935' : '#4a5568', strokeWidth: isBlocks ? 2 : 1.2 },
        labelStyle: { fontSize: 10, fill: '#e53935' },
        labelBgStyle: { fill: '#1a1a2e' },
      };
    }), [relations]);

  const handleNodeClick = useCallback< NodeMouseHandler>((_, node: Node) => {
    onOpenTask(node.id);
  }, [onOpenTask]);

  if (loading) {
    return <div style={{ padding: 24, color: 'var(--color-text-tertiary)' }}>网状图加载中...</div>;
  }
  if (error) {
    return <div style={{ padding: 24, color: '#fca5a5' }}>{error}</div>;
  }
  if (tasks.length === 0) {
    return (
      <div style={{
        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'var(--color-text-tertiary)',
        background: 'var(--color-bg-surface)',
        border: '1px solid var(--color-border-subtle)',
        borderRadius: 'var(--radius-md)',
      }}>
        暂无任务，无法生成网状图
      </div>
    );
  }

  return (
    <div style={{
      flex: 1, minHeight: 400, position: 'relative',
      background: 'var(--color-bg-surface)',
      border: '1px solid var(--color-border-subtle)',
      borderRadius: 'var(--radius-md)', overflow: 'hidden',
    }}>
      {/* 图例 */}
      <div style={{
        position: 'absolute', top: 10, left: 10, zIndex: 5,
        background: 'rgba(13, 17, 23, 0.85)', borderRadius: 8, padding: '8px 12px',
        fontSize: 11, color: '#c9d1d9', lineHeight: 1.8, pointerEvents: 'none',
      }}>
        <div>父子关系（上下层级）：灰线</div>
        <div style={{ color: '#e53935' }}>阻断关系（animated）：红线</div>
        <div>边框色：绿=已关闭 橙=进行/评审 红=阻塞 灰=其他</div>
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={handleNodeClick}
        fitView
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1a2028" gap={20} />
        <Controls />
        <MiniMap
          nodeColor={(n) => (STATUS_COLORS[(n as unknown as { status?: string }).status || '']?.border) || '#4a5568'}
          style={{ background: '#0d1117' }}
        />
      </ReactFlow>
    </div>
  );
}
