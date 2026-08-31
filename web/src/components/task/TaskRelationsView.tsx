// web/src/components/task/TaskRelationsView.tsx
// V3 任务关系视图（§4.11.4）：详情页内嵌一层关系（父子 + 阻断），不渲染大图
// - 上游：父任务 + 阻断我的任务（blockers）
// - 下游：子任务 + 我阻断的任务
// 点击条目 → 跳转对应任务详情

import { useState, useEffect, useCallback } from 'react';
import { taskApi, type Task, type TaskRelation } from '../../api/taskApi';

const STATUS_LABELS: Record<string, string> = {
  idea: '灵感', backlog: '待办池', discussing: '讨论中', decomposing: '拆解中',
  in_progress: '进行中', blocked: '被阻塞', validating: '验证中',
  reviewing: '评审中', closing: '关闭中', closed: '已关闭',
  canceled: '已取消', abandoned: '已废弃',
};

function statusDot(status: string): string {
  if (['closed', 'done'].includes(status)) return '#43a047';
  if (['blocked', 'canceled', 'abandoned'].includes(status)) return '#e53935';
  if (['in_progress', 'validating', 'reviewing', 'closing', 'decomposing'].includes(status)) return '#fb8c00';
  return '#5b8def';
}

function RelItem({
  task,
  fallbackId,
  onOpenTask,
  kindTag,
}: {
  task?: Task;
  fallbackId: string;
  onOpenTask: (taskId: string) => void;
  kindTag?: string;
}) {
  const id = task?.task_id || fallbackId;
  const title = task?.title || '（任务不在当前项目或已删除）';
  const status = task?.status || 'unknown';
  return (
    <div
      onClick={() => task && onOpenTask(id)}
      style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '7px 10px', marginBottom: 6,
        background: 'var(--color-bg-elevated)',
        border: '1px solid var(--color-border-subtle)',
        borderRadius: 'var(--radius-sm)',
        cursor: task ? 'pointer' : 'default',
        opacity: task ? 1 : 0.55,
      }}
    >
      {kindTag && (
        <span style={{
          fontSize: 10, padding: '1px 6px', borderRadius: 'var(--radius-full)',
          background: 'var(--color-bg-surface)', color: 'var(--color-text-tertiary)',
          border: '1px solid var(--color-border-subtle)', whiteSpace: 'nowrap',
        }}>
          {kindTag}
        </span>
      )}
      <span style={{
        width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
        background: statusDot(status),
      }} />
      <span style={{ fontWeight: 600, fontSize: 12, color: 'var(--color-text-primary)', whiteSpace: 'nowrap' }}>
        {task?.identifier || fallbackId.slice(-6)}
      </span>
      <span style={{
        flex: 1, fontSize: 12, color: 'var(--color-text-secondary)',
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}>
        {title}
      </span>
      <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)', whiteSpace: 'nowrap' }}>
        {STATUS_LABELS[status] || status}
      </span>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)', margin: '8px 0 6px' }}>
      {children}
    </div>
  );
}

export default function TaskRelationsView({
  taskId,
  task,
  projectTasks,
  onOpenTask,
}: {
  taskId: string;
  task?: Task | null;
  projectTasks: Task[];
  onOpenTask: (taskId: string) => void;
}) {
  const [relations, setRelations] = useState<TaskRelation[]>([]);
  const [blockers, setBlockers] = useState<Task[]>([]);

  const load = useCallback(async () => {
    try {
      const res = await taskApi.getRelations(taskId);
      setRelations(res.relations || []);
      setBlockers(res.blockers || []);
    } catch {
      // 关系加载失败不阻塞详情页
    }
  }, [taskId]);

  useEffect(() => {
    load();
  }, [load]);

  // 任务查找：优先项目任务列表（带标题），找不到用 task 自身
  const findTask = (id: string): Task | undefined =>
    projectTasks.find((t) => t.task_id === id) ||
    (task && task.task_id === id ? task : undefined);

  // 父任务：task_relations 权威来源（type=parent 且 target=当前 → source 是父），
  // tasks.parent_task_id 冗余字段作补充（relation 接口失败时兜底）
  const parentFromRel = relations.find(
    (r) => r.relation_type === 'parent' && r.target_task_id === taskId,
  )?.source_task_id;
  const parentId = parentFromRel || task?.parent_task_id || '';
  // 子任务：relations 中 source=当前 且 type=parent（source 是父 → target 是子）
  const children = relations
    .filter((r) => r.relation_type === 'parent' && r.source_task_id === taskId)
    .map((r) => r.target_task_id);
  // 我阻断的（下游）：relations 中 source=当前 且 type=blocks
  const blocking = relations
    .filter((r) => r.relation_type === 'blocks' && r.source_task_id === taskId)
    .map((r) => r.target_task_id);
  // 阻断我的（上游）：blockers 接口
  const blockedBy = blockers.map((b) => b.task_id);

  const upstreamCount = (parentId ? 1 : 0) + blockedBy.length;
  const downstreamCount = children.length + blocking.length;
  const empty = upstreamCount === 0 && downstreamCount === 0;

  return (
    <div style={{
      background: 'var(--color-bg-surface)',
      border: '1px solid var(--color-border-subtle)',
      borderRadius: 'var(--radius-md)', padding: 14,
    }}>
      <div style={{
        fontSize: 13, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 4,
      }}>
        任务关系
      </div>
      <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginBottom: 10 }}>
        仅展示一层依赖（父子 + 阻断）；项目全局网状图见「网状图」视图
      </div>

      {empty && (
        <div style={{
          color: 'var(--color-text-tertiary)', fontSize: 12, textAlign: 'center', padding: 16,
        }}>
          该任务暂无父子/阻断关系
        </div>
      )}

      {!empty && (
        <div style={{ display: 'flex', gap: 16 }}>
          {/* 上游 */}
          <div style={{ flex: 1 }}>
            <SectionTitle>⬆ 上游（{upstreamCount}）</SectionTitle>
            {parentId && (
              <RelItem task={findTask(parentId)} fallbackId={parentId} onOpenTask={onOpenTask} kindTag="父任务" />
            )}
            {blockedBy.map((id) => {
              const t = blockers.find((b) => b.task_id === id);
              return (
                <RelItem
                  key={id}
                  task={t || findTask(id)}
                  fallbackId={id}
                  onOpenTask={onOpenTask}
                  kindTag="阻断我"
                />
              );
            })}
          </div>

          {/* 下游 */}
          <div style={{ flex: 1 }}>
            <SectionTitle>⬇ 下游（{downstreamCount}）</SectionTitle>
            {children.map((id) => (
              <RelItem key={id} task={findTask(id)} fallbackId={id} onOpenTask={onOpenTask} kindTag="子任务" />
            ))}
            {blocking.map((id) => (
              <RelItem key={id} task={findTask(id)} fallbackId={id} onOpenTask={onOpenTask} kindTag="我阻断" />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
