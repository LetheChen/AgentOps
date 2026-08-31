// web/src/pages/TaskDetailPage.tsx
// V3.1 任务详情页（参考 Taskboard issue 详情布局重构）
// - 顶部：返回 + identifier + 标题 + 状态/风险 badge + 执行编码 / 查看报告
// - 左栏（内容流）：描述 → 子议题（进度 + 列表）→ 活动与评论时间线
// - 右栏（属性面板）：状态/优先级（可改）、负责人、关系（阻塞于/阻塞/父子）、时间戳、终端
// - @agent 评论（TaskCommentThread）：@coding_agent 触发后台 agent 回复
// 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.11/§4.12

import { useState, useEffect, useCallback } from 'react';
import {
  taskApi,
  type Task,
  type TaskRelation,
  type TaskTransition,
} from '../api/taskApi';
import TaskCommentThread from '../components/task/TaskCommentThread';
import TerminalPanel from '../components/task/TerminalPanel';
import WorkReview from '../components/task/WorkReview';
import { renderMarkdown } from '../lib/markdown';

// 状态 → pill class
function statusPillClass(status: string): string {
  const s = status.toLowerCase();
  if (['closed', 'done', 'approved', 'passed'].includes(s)) return 'status-pill-success';
  if (['reviewing', 'validating', 'closing', 'pending', 'in_progress', 'decomposing', 'discussing'].includes(s)) return 'status-pill-warning';
  if (['failed', 'blocked', 'rejected', 'canceled', 'abandoned'].includes(s)) return 'status-pill-error';
  return 'status-pill-info';
}

const STATUS_LABELS: Record<string, string> = {
  idea: '灵感', backlog: '待办池', discussing: '讨论中', decomposing: '拆解中',
  in_progress: '进行中', blocked: '被阻塞', validating: '验证中',
  reviewing: '评审中', closing: '关闭中', closed: '已关闭',
  canceled: '已取消', abandoned: '已废弃',
};
const RISK_LABELS: Record<string, string> = { high: '高', medium: '中', low: '低' };
const RISK_COLORS: Record<string, string> = { high: '#e53935', medium: '#fb8c00', low: '#43a047' };

function statusDot(status: string): string {
  if (['closed', 'done'].includes(status)) return '#10B981';
  if (['blocked', 'canceled', 'abandoned'].includes(status)) return '#EF4444';
  if (['in_progress', 'validating', 'reviewing', 'closing', 'decomposing', 'discussing'].includes(status)) return '#F59E0B';
  return '#60A5FA';
}

function fmtDateTime(iso?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { hour12: false });
}

// ---- 属性面板行 ----
function PropRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginBottom: 4 }}>{label}</div>
      {children}
    </div>
  );
}

const propSelectStyle: React.CSSProperties = {
  width: '100%', padding: '6px 8px',
  background: 'var(--color-bg-elevated)',
  border: '1px solid var(--color-border-default)',
  borderRadius: 'var(--radius-sm)', fontSize: 12, cursor: 'pointer',
  color: 'var(--color-text-primary)',
};

export default function TaskDetailPage({
  taskId,
  projectTasks,
  onBack,
  onGoReport,
  onOpenTask,
}: {
  taskId: string;
  projectTasks: Task[];
  onBack: () => void;
  onGoReport: (taskId: string) => void;
  onOpenTask?: (taskId: string) => void;
}) {
  const [task, setTask] = useState<Task | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [executing, setExecuting] = useState(false);
  // 执行编码的 harness 选择（claude_code / codex）
  const [executeHarness, setExecuteHarness] = useState('claude_code');
  const [showTerminal, setShowTerminal] = useState(false);
  // 属性面板
  const [transitions, setTransitions] = useState<TaskTransition[]>([]);
  const [relations, setRelations] = useState<TaskRelation[]>([]);
  const [blockers, setBlockers] = useState<Task[]>([]);
  const [saving, setSaving] = useState(false);
  // 父任务编辑（V3.3：详情页可选父任务，含环检测由后端保证）
  const [editingParent, setEditingParent] = useState(false);
  const [savingParent, setSavingParent] = useState(false);

  // 加载任务详情
  const loadTask = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const taskRes = await taskApi.getTask(taskId);
      setTask(taskRes.task);
      // 已绑定终端会话时默认展开终端面板
      if (taskRes.task?.terminal_session_id) {
        setShowTerminal(true);
      }
      // 并行拉 transitions + relations（失败不阻塞）
      taskApi.getTransitions(taskId)
        .then((t) => setTransitions(t.transitions || []))
        .catch(() => setTransitions([]));
      taskApi.getRelations(taskId)
        .then((r) => {
          setRelations(r.relations || []);
          setBlockers(r.blockers || []);
        })
        .catch(() => {
          setRelations([]);
          setBlockers([]);
        });
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    setShowTerminal(false);
    setTransitions([]);
    setRelations([]);
    setBlockers([]);
    loadTask();
  }, [loadTask]);

  // 执行编码
  const handleExecute = async () => {
    if (!task) return;
    setExecuting(true);
    setError('');
    try {
      await taskApi.executeCoding(taskId, task.style_id || 'default', task.version, executeHarness);
      await loadTask();
      setShowTerminal(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : '执行编码失败');
    } finally {
      setExecuting(false);
    }
  };

  // 状态推进（走合法转移）
  const handleStatusChange = async (target: string) => {
    if (!task || target === task.status) return;
    setSaving(true);
    setError('');
    try {
      const updated = await taskApi.advance(taskId, target, task.version, {
        actor: 'user',
        comment: '详情页属性面板操作',
      });
      setTask(updated);
      const t = await taskApi.getTransitions(taskId);
      setTransitions(t.transitions || []);
      // 活动时间线刷新（TaskCommentThread 内部不感知，重新挂载）
      setRelations((r) => [...r]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '状态推进失败');
    } finally {
      setSaving(false);
    }
  };

  // 优先级（risk_level）修改：白名单字段直接 PATCH
  const handleRiskChange = async (risk: string) => {
    if (!task || risk === task.risk_level) return;
    setSaving(true);
    setError('');
    try {
      const updated = await taskApi.updateTask(taskId, task.version, { risk_level: risk });
      setTask(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : '更新优先级失败');
    } finally {
      setSaving(false);
    }
  };

  // 父任务变更（parent_task_id；后端环检测 + relations 同步，值非法时 400）
  // 409 版本冲突时拉最新任务重试一次（页面数据可能落后于其他端修改）
  const handleParentChange = async (parentId: string) => {
    if (!task) return;
    setSavingParent(true);
    setError('');
    const doUpdate = async (ifVersion: number) =>
      taskApi.updateTask(taskId, ifVersion, { parent_task_id: parentId || null });
    try {
      let updated: Task | null = null;
      try {
        updated = await doUpdate(task.version);
      } catch {
        // 乐观锁冲突：拉最新版本重试一次
        const fresh = await taskApi.getTask(taskId);
        if (fresh.task) {
          setTask(fresh.task);
          updated = await doUpdate(fresh.task.version);
        }
      }
      if (updated) {
        setTask(updated);
        setEditingParent(false);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '更新父任务失败');
    } finally {
      setSavingParent(false);
    }
  };

  // 复制 ID
  const [copied, setCopied] = useState(false);
  const copyId = async () => {
    if (!task) return;
    try {
      await navigator.clipboard.writeText(task.identifier || task.task_id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板不可用时忽略
    }
  };

  if (loading) {
    return (
      <div style={{ padding: 24, color: 'var(--color-text-secondary)' }}>
        加载中...
        <button onClick={onBack} style={{ ...linkBtnStyle, marginLeft: 16 }}>
          ← 返回
        </button>
      </div>
    );
  }

  // 子任务：projectTasks 过滤 parent_task_id（relation 数据作补充）
  const childIdsFromRel = relations
    .filter((r) => r.relation_type === 'parent' && r.source_task_id === taskId)
    .map((r) => r.target_task_id);
  const childIds = Array.from(new Set([
    ...projectTasks.filter((t) => t.parent_task_id === taskId).map((t) => t.task_id),
    ...childIdsFromRel,
  ]));
  const childTasks = childIds
    .map((id) => projectTasks.find((t) => t.task_id === id))
    .filter((t): t is Task => Boolean(t));
  const childDone = childTasks.filter((t) => t.status === 'closed').length;

  // 关系：父任务 / 阻塞于 / 阻塞
  const parentIdFromRel = relations.find(
    (r) => r.relation_type === 'parent' && r.target_task_id === taskId,
  )?.source_task_id;
  const parentId = parentIdFromRel || task?.parent_task_id || '';
  const parentTask = projectTasks.find((t) => t.task_id === parentId);
  const blockingIds = relations
    .filter((r) => r.relation_type === 'blocks' && r.source_task_id === taskId)
    .map((r) => r.target_task_id);
  const findTaskById = (id: string): Task | undefined =>
    projectTasks.find((t) => t.task_id === id) ||
    blockers.find((b) => b.task_id === id);

  // 可选状态：所有合法转移目标（含自动推进型如立项，requires_user 型如关闭）
  // 注：actor='user' 时后端对两类转移均放行；requires_user 仅限制 agent
  const statusOptions = transitions;

  const relLinkStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 8, width: '100%',
    padding: '6px 8px', marginBottom: 4,
    background: 'var(--color-bg-elevated)',
    border: '1px solid var(--color-border-subtle)',
    borderRadius: 'var(--radius-sm)', cursor: 'pointer',
    fontSize: 12, color: 'var(--color-text-primary)', textAlign: 'left',
  };

  return (
    <div
      style={{
        height: '100%',
        boxSizing: 'border-box',
        display: 'flex',
        flexDirection: 'column',
        color: 'var(--color-text-primary)',
      }}
    >
      {/* ===== 顶部栏 ===== */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '12px 20px',
          borderBottom: '1px solid var(--color-border-subtle)',
          flexWrap: 'wrap',
        }}
      >
        <button onClick={onBack} style={linkBtnStyle}>
          ← 返回
        </button>
        {task && (
          <span style={{
            fontSize: 12, fontWeight: 600, color: 'var(--color-text-tertiary)',
            background: 'var(--color-bg-elevated)', padding: '2px 8px',
            borderRadius: 'var(--radius-sm)',
          }}>
            {task.identifier || task.task_id.slice(-6)}
          </span>
        )}
        <h2 style={{
          margin: 0, fontSize: 17, color: 'var(--color-text-primary)',
          flex: 1, minWidth: 120,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {task ? task.title : taskId}
        </h2>
        {task && (
          <span className={`status-pill ${statusPillClass(task.status)}`} style={pillStyle}>
            {STATUS_LABELS[task.status] || task.status}
          </span>
        )}
        {task && (
          <span
            style={{
              fontSize: 11,
              padding: '2px 8px',
              borderRadius: 'var(--radius-full)',
              background: RISK_COLORS[task.risk_level] || '#666',
              color: '#fff',
              fontWeight: 600,
            }}
          >
            {RISK_LABELS[task.risk_level] || task.risk_level}风险
          </span>
        )}
        {task && (
          <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>v{task.version}</span>
        )}
        {/* 右侧操作按钮 */}
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <select
            value={executeHarness}
            onChange={(e) => setExecuteHarness(e.target.value)}
            disabled={executing}
            title="选择执行编码的 agent（claude CLI / codex）"
            style={propSelectStyle}
          >
            <option value="claude_code">Claude</option>
            <option value="codex">Codex</option>
          </select>
          <button
            onClick={handleExecute}
            disabled={executing || !task || task.status === 'closed'}
            style={primaryBtnStyle}
            title="触发 agent 执行编码（Coding 终端页可观测）"
          >
            {executing ? '执行中...' : '▶ 执行编码'}
          </button>
          <button onClick={() => onGoReport(taskId)} style={secondaryBtnStyle}>
            📄 查看报告
          </button>
        </div>
      </div>

      {/* 错误提示 */}
      {error && (
        <div
          style={{
            background: 'rgba(239, 68, 68, 0.12)',
            color: '#fca5a5',
            padding: '8px 20px',
            fontSize: 13,
            borderBottom: '1px solid rgba(239, 68, 68, 0.3)',
          }}
        >
          {error}
        </div>
      )}

      {/* ===== 左右双栏 ===== */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        {/* ---- 左栏：内容流 ---- */}
        <div style={{
          flex: 1, minWidth: 0, overflow: 'auto', padding: 20,
          display: 'flex', flexDirection: 'column', gap: 16,
        }}>
          {/* 描述（markdown 渲染） */}
          <div>
            <div style={leftSectionTitleStyle}>描述</div>
            {task?.description ? (
              <div
                className="md-content"
                style={{
                  fontSize: 13, color: 'var(--color-text-secondary)',
                  lineHeight: 1.7,
                  background: 'var(--color-bg-surface)',
                  border: '1px solid var(--color-border-subtle)',
                  borderRadius: 'var(--radius-md)',
                  padding: 14,
                }}
                dangerouslySetInnerHTML={{ __html: renderMarkdown(task.description) }}
              />
            ) : (
              <div style={{
                fontSize: 13, color: 'var(--color-text-tertiary)',
                lineHeight: 1.7,
                background: 'var(--color-bg-surface)',
                border: '1px solid var(--color-border-subtle)',
                borderRadius: 'var(--radius-md)',
                padding: 14,
              }}>
                暂无描述
              </div>
            )}
          </div>

          {/* 子议题 */}
          <div>
            <div style={{ ...leftSectionTitleStyle, display: 'flex', alignItems: 'center', gap: 8 }}>
              子议题
              {childTasks.length > 0 && (
                <span style={{
                  fontSize: 11, fontWeight: 400, color: 'var(--color-text-tertiary)',
                  background: 'var(--color-bg-elevated)', padding: '1px 8px',
                  borderRadius: 'var(--radius-full)',
                }}>
                  {childDone} / {childTasks.length}
                </span>
              )}
            </div>
            {childTasks.length === 0 ? (
              <div style={{
                fontSize: 12, color: 'var(--color-text-tertiary)',
                padding: '12px 14px',
                background: 'var(--color-bg-surface)',
                border: '1px dashed var(--color-border-subtle)',
                borderRadius: 'var(--radius-md)',
              }}>
                暂无子议题
              </div>
            ) : (
              <>
                {/* 子任务完成度进度条 */}
                <div style={{
                  height: 5, borderRadius: 3, background: 'var(--color-bg-elevated)',
                  marginBottom: 8,
                }}>
                  <div style={{
                    height: 5, borderRadius: 3, background: '#10B981',
                    width: `${Math.max(Math.round((childDone / childTasks.length) * 100), 2)}%`,
                    transition: 'width .3s ease',
                  }} />
                </div>
                {childTasks.map((ct) => {
                  const done = ct.status === 'closed';
                  return (
                    <div
                      key={ct.task_id}
                      onClick={() => onOpenTask?.(ct.task_id)}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 10,
                        padding: '8px 12px', marginBottom: 6, cursor: 'pointer',
                        background: 'var(--color-bg-surface)',
                        border: '1px solid var(--color-border-subtle)',
                        borderRadius: 'var(--radius-sm)',
                      }}
                    >
                      <span style={{
                        width: 16, height: 16, borderRadius: 4, flexShrink: 0,
                        border: `1.5px solid ${done ? '#10B981' : 'var(--color-border-default)'}`,
                        background: done ? '#10B981' : 'transparent',
                        color: '#fff', fontSize: 10, fontWeight: 700,
                        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                      }}>
                        {done ? '✓' : ''}
                      </span>
                      <span style={{
                        fontSize: 11, fontWeight: 600, color: 'var(--color-text-tertiary)',
                        whiteSpace: 'nowrap',
                      }}>
                        {ct.identifier || ct.task_id.slice(-6)}
                      </span>
                      <span style={{
                        flex: 1, fontSize: 13, color: done ? 'var(--color-text-tertiary)' : 'var(--color-text-primary)',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        textDecoration: done ? 'line-through' : 'none',
                      }}>
                        {ct.title}
                      </span>
                      <span style={{ width: 8, height: 8, borderRadius: '50%', background: statusDot(ct.status), flexShrink: 0 }} />
                      <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)', whiteSpace: 'nowrap' }}>
                        {STATUS_LABELS[ct.status] || ct.status}
                      </span>
                    </div>
                  );
                })}
              </>
            )}
          </div>

          {/* 活动与评论时间线 */}
          <TaskCommentThread taskId={taskId} />

          {/* 工作回顾 */}
          <WorkReview taskId={taskId} />
        </div>

        {/* ---- 右栏：属性面板 ---- */}
        <div style={{
          width: 300, flexShrink: 0, overflow: 'auto', padding: 16,
          borderLeft: '1px solid var(--color-border-subtle)',
          background: 'var(--color-bg-base)',
        }}>
          {/* 快捷操作 */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
            <button
              onClick={copyId}
              style={quickBtnStyle}
              title="复制任务 ID"
            >
              {copied ? '✓ 已复制' : '⧉ 复制 ID'}
            </button>
            {task?.terminal_session_id && (
              <button
                onClick={() => setShowTerminal((v) => !v)}
                style={quickBtnStyle}
                title="展开/收起终端面板"
              >
                🖥 终端
              </button>
            )}
          </div>

          {/* 属性 */}
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)', marginBottom: 10 }}>
            属性
          </div>

          <PropRow label="状态">
            {statusOptions.length > 0 ? (
              <select
                value={task?.status || ''}
                disabled={saving}
                onChange={(e) => handleStatusChange(e.target.value)}
                style={propSelectStyle}
              >
                <option value={task?.status}>{STATUS_LABELS[task?.status || ''] || task?.status}（当前）</option>
                {statusOptions
                  .filter((t) => t.to !== task?.status)
                  .map((t) => (
                    <option key={t.to} value={t.to}>
                      {STATUS_LABELS[t.to] || t.to} · {t.action}
                    </option>
                  ))}
              </select>
            ) : (
              <div style={propValueStyle}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: statusDot(task?.status || ''), display: 'inline-block', marginRight: 6 }} />
                {STATUS_LABELS[task?.status || ''] || task?.status}
                <span style={{ color: 'var(--color-text-tertiary)', fontSize: 11 }}>（无可推进状态）</span>
              </div>
            )}
          </PropRow>

          <PropRow label="优先级（风险）">
            <select
              value={task?.risk_level || ''}
              disabled={saving}
              onChange={(e) => handleRiskChange(e.target.value)}
              style={propSelectStyle}
            >
              <option value="high">高</option>
              <option value="medium">中</option>
              <option value="low">低</option>
            </select>
          </PropRow>

          <PropRow label="负责人">
            <div style={propValueStyle}>
              {(task?.assignee_name || '未分配')}
              {task?.assignee_type === 'agent' && (
                <span style={{ fontSize: 11, color: 'var(--color-primary-soft)', marginLeft: 6 }}>(agent)</span>
              )}
            </div>
          </PropRow>

          <PropRow label="任务类型">
            <div style={propValueStyle}>{task?.task_type || '—'}</div>
          </PropRow>

          {/* 关系 */}
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)', margin: '16px 0 10px' }}>
            关系
          </div>

          <PropRow label="父任务">
            {editingParent ? (
              <select
                value={parentId || ''}
                disabled={savingParent}
                onChange={(e) => handleParentChange(e.target.value)}
                style={propSelectStyle}
                autoFocus
              >
                <option value="">（无父任务）</option>
                {projectTasks
                  .filter((t) => t.task_id !== taskId)
                  .map((t) => (
                    <option key={t.task_id} value={t.task_id}>
                      {t.identifier || t.task_id.slice(-6)} · {t.title}
                    </option>
                  ))}
              </select>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%' }}>
                {parentId ? (
                  <button
                    onClick={() => onOpenTask?.(parentId)}
                    style={{ ...relLinkStyle, flex: 1, minWidth: 0 }}
                  >
                    <span style={{ fontWeight: 600, color: 'var(--color-text-tertiary)' }}>
                      {parentTask?.identifier || parentId.slice(-6)}
                    </span>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {parentTask?.title || '（不在当前项目）'}
                    </span>
                  </button>
                ) : (
                  <div style={{ ...propValueStyle, color: 'var(--color-text-tertiary)', flex: 1 }}>无</div>
                )}
                <button
                  onClick={() => setEditingParent(true)}
                  disabled={savingParent}
                  title="修改父任务"
                  style={{
                    padding: '2px 8px', fontSize: 11, cursor: 'pointer', flexShrink: 0,
                    background: 'var(--color-bg-elevated)',
                    color: 'var(--color-text-secondary)',
                    border: '1px solid var(--color-border-default)',
                    borderRadius: 'var(--radius-sm)',
                  }}
                >
                  修改
                </button>
              </div>
            )}
          </PropRow>

          <PropRow label={`阻塞于（${blockers.length}）`}>
            {blockers.length === 0 ? (
              <div style={{ ...propValueStyle, color: 'var(--color-text-tertiary)' }}>无</div>
            ) : (
              blockers.map((b) => (
                <button
                  key={b.task_id}
                  onClick={() => onOpenTask?.(b.task_id)}
                  style={relLinkStyle}
                >
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: statusDot(b.status), flexShrink: 0 }} />
                  <span style={{ fontWeight: 600, color: 'var(--color-text-tertiary)' }}>
                    {b.identifier || b.task_id.slice(-6)}
                  </span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {b.title}
                  </span>
                </button>
              ))
            )}
          </PropRow>

          <PropRow label={`阻塞（${blockingIds.length}）`}>
            {blockingIds.length === 0 ? (
              <div style={{ ...propValueStyle, color: 'var(--color-text-tertiary)' }}>无</div>
            ) : (
              blockingIds.map((id) => {
                const t = findTaskById(id);
                return (
                  <button
                    key={id}
                    onClick={() => onOpenTask?.(id)}
                    style={relLinkStyle}
                  >
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: statusDot(t?.status || ''), flexShrink: 0 }} />
                    <span style={{ fontWeight: 600, color: 'var(--color-text-tertiary)' }}>
                      {t?.identifier || id.slice(-6)}
                    </span>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {t?.title || '（不在当前项目）'}
                    </span>
                  </button>
                );
              })
            )}
          </PropRow>

          {/* 时间戳 */}
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)', margin: '16px 0 10px' }}>
            时间
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', lineHeight: 1.8 }}>
            <div>创建于 {fmtDateTime(task?.created_at)}</div>
            <div>更新于 {fmtDateTime(task?.updated_at)}</div>
            {task?.closed_at && <div>关闭于 {fmtDateTime(task.closed_at)}</div>}
          </div>

          {/* 终端面板（折叠区） */}
          <div style={{
            marginTop: 16,
            background: 'var(--color-bg-surface)',
            border: '1px solid var(--color-border-subtle)',
            borderRadius: 'var(--radius-md)',
          }}>
            <button
              onClick={() => setShowTerminal((v) => !v)}
              style={{
                width: '100%', padding: '10px 14px', cursor: 'pointer',
                background: 'transparent', border: 'none',
                color: 'var(--color-text-primary)', fontSize: 12, fontWeight: 600,
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              }}
            >
              <span>🖥 终端 {task?.terminal_session_id ? `（${task.terminal_session_id.slice(-8)}）` : '（未绑定会话）'}</span>
              <span style={{ color: 'var(--color-text-tertiary)' }}>{showTerminal ? '收起 ▴' : '展开 ▾'}</span>
            </button>
            {showTerminal && (
              <div style={{ padding: '0 10px 10px' }}>
                <TerminalPanel
                  taskId={taskId}
                  terminalSessionId={task?.terminal_session_id}
                  height={320}
                />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---- 内联样式 ----
const pillStyle: React.CSSProperties = {
  fontSize: 11,
  padding: '2px 8px',
  borderRadius: 'var(--radius-full)',
  fontWeight: 600,
  display: 'inline-block',
};
const linkBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  color: 'var(--color-primary-soft)',
  fontSize: 13,
  padding: '4px 8px',
};
const primaryBtnStyle: React.CSSProperties = {
  padding: '10px 24px',
  minWidth: 128,
  background: 'var(--color-primary)',
  color: '#fff',
  border: 'none',
  borderRadius: 'var(--radius-md)',
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 600,
  whiteSpace: 'nowrap',
};
const secondaryBtnStyle: React.CSSProperties = {
  padding: '10px 24px',
  minWidth: 128,
  background: 'transparent',
  color: 'var(--color-primary-soft)',
  border: '1px solid var(--color-primary)',
  borderRadius: 'var(--radius-md)',
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 600,
  whiteSpace: 'nowrap',
};
const leftSectionTitleStyle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: 'var(--color-text-secondary)',
  letterSpacing: '0.05em',
  marginBottom: 8,
};
const propValueStyle: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--color-text-primary)',
  padding: '6px 8px',
  background: 'var(--color-bg-surface)',
  border: '1px solid var(--color-border-subtle)',
  borderRadius: 'var(--radius-sm)',
};
const quickBtnStyle: React.CSSProperties = {
  flex: 1, padding: '6px 8px',
  background: 'var(--color-bg-surface)',
  color: 'var(--color-text-secondary)',
  border: '1px solid var(--color-border-subtle)',
  borderRadius: 'var(--radius-sm)',
  cursor: 'pointer', fontSize: 11,
};
