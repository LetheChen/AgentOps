// web/src/pages/TaskReportPage.tsx
// V1 任务报告页（博客评论模式：报告区 + 审批评论 + 退回决策 + 验收标准）
// 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.8.1
//
// 设计要点：
// - 报告区：显示最新一份 TaskReport（markdown 渲染，lib/markdown.ts + .md-content）
// - 审批评论区：按 created_at 升序展示所有 TaskComment，区分 comment_type（discussion/review/report）
// - 决策区：
//   · 「通过」→ addComment(decision=approve, comment_type=review) + close API
//   · 「退回」→ 弹出 rollback_target 选择（local/partial/global）→ rollback API + addComment(decision=reject)
//   · 普通评论输入框 → addComment(comment_type=discussion)
// - 验收标准区：列出 criteria + status badge
// - 深色主题 CSS 变量（与 TaskCenterPage 一致）

import { useState, useEffect, useCallback } from 'react';
import {
  taskApi,
  type Task,
  type TaskReport,
  type TaskTransition,
  type TaskComment,
  type AcceptanceCriteria,
  type TaskReportExport,
  type TaskExportVerify,
} from '../api/taskApi';
import { renderMarkdown, renderMarkdownWithMentions } from '../lib/markdown';
import { parseThink, ThinkBlock } from '../components/task/TaskCommentThread';

// comment_type → 中文标签 + 配色
const COMMENT_TYPE_LABELS: Record<string, { label: string; pillClass: string }> = {
  discussion: { label: '讨论', pillClass: 'status-pill-info' },
  review: { label: '评审', pillClass: 'status-pill-warning' },
  report: { label: '报告', pillClass: 'status-pill-neutral' },
  decision: { label: '决策', pillClass: 'status-pill-success' },
};

// decision → 中文标签
// 注：后端 CHECK 约束 decision IN ('approve','request_changes') OR decision IS NULL，
// 'reject' 是设计阶段废弃别名；这里保留映射兼容历史数据展示
const DECISION_LABELS: Record<string, string> = {
  approve: '✅ 通过',
  request_changes: '↩ 退回',
  reject: '↩ 退回',
};

// 目标阶段 → 中文标签 + 描述（用于 rollback 面板单选）
// alias 字段：旧三级别名 local/partial/global 的对应关系（向后兼容）
const TARGET_STAGE_META: Record<string, { label: string; desc: string; alias?: string }> = {
  in_progress:  { label: '回退到执行',     desc: '仅回退最近一步变更，保留主体产出',  alias: 'local' },
  decomposing:  { label: '回退到拆解',     desc: '回退部分阶段产出，重新拆解任务',    alias: 'partial' },
  discussing:   { label: '回退到讨论',     desc: '回到任务起点，重新讨论整个方案',    alias: 'global' },
  reviewing:    { label: '回退到评审',     desc: '重新评审拆分方案' },
  backlog:      { label: '回退到待办池',   desc: '退回讨论 / 灵感重新讨论' },
  idea:         { label: '回退到灵感',     desc: '退回灵感，重新讨论立项' },
  validating:   { label: '退回重验',       desc: '关闭前退回验收，重新校验交付物' },
};

// 三级别名 → 目标阶段（向后兼容旧 UI 与历史数据）
const ROLLBACK_ALIAS_MAP: Record<string, string> = {
  local: 'in_progress',
  partial: 'decomposing',
  global: 'discussing',
};

// 不属于"回退类"的目标状态（终态/异常/暂停/取消），不进入回退面板
const NON_ROLLBACK_TARGETS = new Set([
  'closed', 'canceled', 'abandoned', 'paused', 'blocked', 'failed',
]);

// 主线阶段顺序（v1.2：idea→discussing→decomposing→reviewing→backlog→in_progress→validating→closing→closed）
// 用于判断某条转移是"回退"（target 阶段更早）还是"正向推进"（target 阶段更后）
// 状态机 TRANSITIONS 中 validating→closing / closing→closed 都是正向推进，
// 不能误显示为回退选项
const STAGE_ORDER: Record<string, number> = {
  idea: 1, discussing: 2, decomposing: 3, reviewing: 4,
  backlog: 5, in_progress: 6, validating: 7, closing: 8, closed: 9,
};

// 判断 transition 是否属于"回退"语义：target 阶段严格早于 from
function isRollbackTransition(from: string, to: string): boolean {
  const fromOrder = STAGE_ORDER[from] ?? 0;
  const toOrder = STAGE_ORDER[to] ?? 99;
  return toOrder < fromOrder;
}

// status → status-pill class
function statusPillClass(status: string): string {
  const s = status.toLowerCase();
  if (['closed', 'done', 'approved', 'passed'].includes(s)) return 'status-pill-success';
  if (['reviewing', 'validating', 'closing', 'pending'].includes(s)) return 'status-pill-warning';
  if (['failed', 'blocked', 'rejected', 'canceled', 'abandoned'].includes(s)) return 'status-pill-error';
  return 'status-pill-info';
}

export default function TaskReportPage({ taskId, onBack }: { taskId: string; onBack: () => void }) {
  const [task, setTask] = useState<Task | null>(null);
  const [reports, setReports] = useState<TaskReport[]>([]);
  const [comments, setComments] = useState<TaskComment[]>([]);
  const [criteria, setCriteria] = useState<AcceptanceCriteria[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // 决策区局部状态
  const [commentInput, setCommentInput] = useState('');
  const [showRollback, setShowRollback] = useState(false);
  // 当前任务允许的「回退类」合法转移（按 to 字段过滤掉 closed/canceled/abandoned/paused/blocked/failed）
  // 从 GET /api/tasks/{id}/transitions 取，与后端 advance_stage 状态机一致
  const [allowedRollbackTargets, setAllowedRollbackTargets] = useState<TaskTransition[]>([]);
  // 用户选中的目标阶段（target_status），默认取 allowedRollbackTargets 第一项
  const [targetStatus, setTargetStatus] = useState('');
  const [rollbackReason, setRollbackReason] = useState('');
  const [acting, setActing] = useState(false);

  // ---- 加载所有数据 ----
  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [taskRes, reportsRes, commentsRes, criteriaRes, transRes] = await Promise.all([
        taskApi.getTask(taskId),
        taskApi.listReports(taskId),
        taskApi.listComments(taskId),
        taskApi.listCriteria(taskId),
        taskApi.getTransitions(taskId),
      ]);
      setTask(taskRes.task);
      setReports(reportsRes.reports || []);
      setComments(commentsRes.comments || []);
      setCriteria(criteriaRes.criteria || []);

      // 过滤合法转移：保留「回退类」目标
      // 1. to 不在终态/异常/暂停集合里
      // 2. requires_user=true（用户决策类转移：评审打回、验收回退、关闭前退回等）
      // 3. 主线阶段顺序：target 必须严格早于当前状态（防 closing/closed 等正向推进被误标为回退）
      const curStatus = taskRes.task?.status || '';
      const rollbackTargets = (transRes.transitions || []).filter(
        (t: TaskTransition) =>
          !NON_ROLLBACK_TARGETS.has(t.to)
          && t.requires_user
          && isRollbackTransition(curStatus, t.to),
      );
      setAllowedRollbackTargets(rollbackTargets);
      // 默认选中第一个回退目标
      setTargetStatus(prev => prev || rollbackTargets[0]?.to || '');
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 最新报告（按 submitted_at 降序取第一份）
  const latestReport: TaskReport | null =
    reports.length > 0
      ? [...reports].sort((a, b) => (a.submitted_at < b.submitted_at ? 1 : -1))[0]
      : null;

  // 评论按 created_at 升序展示（博客评论模式：从老到新）
  const sortedComments = [...comments].sort((a, b) =>
    a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0,
  );

  // ---- 决策：通过 ----
  const handleApprove = async () => {
    if (!task) return;
    setActing(true);
    setError('');
    try {
      // 先写一条 review 决策评论（decision=approve），再调 close
      await taskApi.addComment(taskId, {
        body: commentInput.trim() || '审批通过，关闭任务。',
        author_type: 'user',
        author_name: 'user',
        comment_type: 'review',
        decision: 'approve',
      });
      await taskApi.close(taskId, task.version);
      setCommentInput('');
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : '通过操作失败');
    } finally {
      setActing(false);
    }
  };

  // ---- 决策：退回 ----
  // 注：旧版 decision='reject' 违反后端 task_comments CHECK 约束
  // (decision IN ('approve','request_changes') OR decision IS NULL)
  // 已对齐设计文档 §4.5.3，统一规范化为 'request_changes'
  const handleRollback = async () => {
    if (!task) return;
    if (!targetStatus) {
      setError('请先选择目标阶段');
      return;
    }
    setActing(true);
    setError('');
    try {
      const stageMeta = TARGET_STAGE_META[targetStatus];
      const stageLabel = stageMeta?.label || targetStatus;
      // 旧三级别名（local/partial/global）：若目标阶段在别名映射里则保留，方便历史评论展示
      const rollbackAlias =
        Object.entries(ROLLBACK_ALIAS_MAP).find(([, v]) => v === targetStatus)?.[0] || '';
      // 先写一条 review 决策评论（decision=request_changes + rollback_target），再调 rollback
      await taskApi.addComment(taskId, {
        body: rollbackReason.trim() || `退回到：${stageLabel}`,
        author_type: 'user',
        author_name: 'user',
        comment_type: 'review',
        decision: 'request_changes',
        rollback_target: rollbackAlias,
      });
      // 调 rollback：传 target_status（新协议，优先级高于 rollback_target）
      await taskApi.rollback(taskId, task.version, rollbackAlias, rollbackReason, targetStatus);
      setRollbackReason('');
      setShowRollback(false);
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : '退回操作失败');
    } finally {
      setActing(false);
    }
  };

  // ---- 普通评论 ----
  const handleAddComment = async () => {
    if (!commentInput.trim()) return;
    setActing(true);
    setError('');
    try {
      await taskApi.addComment(taskId, {
        body: commentInput.trim(),
        author_type: 'user',
        author_name: 'user',
        comment_type: 'discussion',
      });
      setCommentInput('');
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : '评论失败');
    } finally {
      setActing(false);
    }
  };

  if (loading) {
    return (
      <div style={{ padding: 24, color: 'var(--color-text-secondary)' }}>
        加载中...
        <button onClick={onBack} style={{ ...linkBtnStyle, marginLeft: 16 }}>← 返回看板</button>
      </div>
    );
  }

  return (
    <div style={{
      padding: 16, height: '100%', boxSizing: 'border-box',
      display: 'flex', flexDirection: 'column', overflow: 'auto',
      color: 'var(--color-text-primary)',
    }}>
      {/* 顶部：返回 + 任务标题 + 状态 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <button onClick={onBack} style={linkBtnStyle}>← 返回看板</button>
        <h2 style={{ margin: 0, color: 'var(--color-text-primary)', fontSize: 18 }}>
          {task ? `${task.identifier || task.task_id.slice(-6)} · ${task.title}` : taskId}
        </h2>
        {task && (
          <span className={`status-pill ${statusPillClass(task.status)}`} style={pillStyle}>
            {task.status}
          </span>
        )}
        {task && (
          <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>v{task.version}</span>
        )}
      </div>

      {error && (
        <div style={{
          background: 'var(--state-error-tint)', color: 'var(--state-error)',
          padding: '8px 12px', borderRadius: 'var(--radius-sm)',
          marginBottom: 12, fontSize: 13, border: '1px solid var(--state-error)',
        }}>
          {error}
        </div>
      )}

      {/* 主体：左右分栏（左：报告+评论，右：决策+验收） */}
      <div style={{ display: 'flex', gap: 16, flex: 1, minHeight: 0 }}>
        {/* 左侧：报告 + 评论列表 */}
        <div style={{ flex: 2, display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
          {/* 报告区 */}
          <section style={sectionStyle}>
            <h3 style={sectionTitleStyle}>📄 最新报告 {latestReport && `（${new Date(latestReport.submitted_at).toLocaleString()}）`}</h3>
            {latestReport ? (
              <>
                <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)', marginBottom: 8 }}>
                  agent: {latestReport.agent_id}
                  {latestReport.terminal_session_id && ` · terminal: ${latestReport.terminal_session_id.slice(0, 8)}`}
                  {latestReport.artifact_ids.length > 0 && ` · artifacts: ${latestReport.artifact_ids.length}`}
                </div>
                {/* 报告正文为 agent 生成的 markdown，md-content 渲染 */}
                <div
                  className="md-content"
                  style={reportMdStyle}
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(latestReport.content) }}
                />
                {Object.keys(latestReport.acceptance_self_check || {}).length > 0 && (
                  <details style={{ marginTop: 8, fontSize: 12, color: 'var(--color-text-secondary)' }}>
                    <summary style={{ cursor: 'pointer', color: 'var(--color-text-secondary)' }}>
                      自检清单（{Object.keys(latestReport.acceptance_self_check).length} 项）
                    </summary>
                    <pre style={{ ...reportPreStyle, marginTop: 6, fontSize: 12 }}>
                      {JSON.stringify(latestReport.acceptance_self_check, null, 2)}
                    </pre>
                  </details>
                )}
                <ExportBar taskId={taskId} reportId={latestReport.report_id} />
              </>
            ) : (
              <div style={{ color: 'var(--color-text-tertiary)', fontSize: 13, padding: 12 }}>
                暂无报告。Agent 完成执行后会在此提交。
              </div>
            )}
            {reports.length > 1 && (
              <details style={{ marginTop: 8, fontSize: 12 }}>
                <summary style={{ cursor: 'pointer', color: 'var(--color-text-secondary)' }}>
                  查看历史报告（共 {reports.length} 份）
                </summary>
                {reports.slice().sort((a, b) => (a.submitted_at < b.submitted_at ? 1 : -1)).map((r, idx) => (
                  <div key={r.report_id} style={{ marginTop: 8, padding: 8, background: 'var(--color-bg-elevated)', borderRadius: 'var(--radius-sm)' }}>
                    <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>
                      #{idx + 1} · {new Date(r.submitted_at).toLocaleString()} · agent: {r.agent_id}
                    </div>
                    <div
                      className="md-content"
                      style={{ ...reportMdStyle, marginTop: 4, fontSize: 12, maxHeight: 300 }}
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(r.content) }}
                    />
                  </div>
                ))}
              </details>
            )}
          </section>

          {/* 评论列表 */}
          <section style={sectionStyle}>
            <h3 style={sectionTitleStyle}>💬 审批评论（{sortedComments.length}）</h3>
            {sortedComments.length === 0 ? (
              <div style={{ color: 'var(--color-text-tertiary)', fontSize: 13, padding: 12 }}>
                暂无评论。
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {sortedComments.map((c) => {
                  const meta = COMMENT_TYPE_LABELS[c.comment_type] || COMMENT_TYPE_LABELS.discussion;
                  return (
                    <div key={c.comment_id} style={commentItemStyle}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                        <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--color-text-primary)' }}>
                          {c.author_name || c.author_type}
                        </span>
                        <span className={`status-pill ${meta.pillClass}`} style={pillStyle}>
                          {meta.label}
                        </span>
                        {c.decision && (
                          <span className={`status-pill ${c.decision === 'approve' ? 'status-pill-success' : 'status-pill-error'}`} style={pillStyle}>
                            {DECISION_LABELS[c.decision] || c.decision}
                          </span>
                        )}
                        {c.rollback_target && (
                          <span className="status-pill status-pill-warning" style={pillStyle}>
                            退回 → {TARGET_STAGE_META[c.rollback_target]?.label
                                   || ROLLBACK_ALIAS_MAP[c.rollback_target]
                                   || c.rollback_target}
                          </span>
                        )}
                        <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginLeft: 'auto' }}>
                          {new Date(c.created_at).toLocaleString()}
                        </span>
                      </div>
                      <div style={{ fontSize: 13, color: 'var(--color-text-secondary)' }}>
                        {(() => {
                          const { think, rest } = parseThink(c.body);
                          return (
                            <>
                              {think !== null && <ThinkBlock content={think} />}
                              {rest && (
                                <div
                                  className="md-content"
                                  style={{ fontSize: 13, color: 'var(--color-text-secondary)' }}
                                  dangerouslySetInnerHTML={{ __html: renderMarkdownWithMentions(rest) }}
                                />
                              )}
                            </>
                          );
                        })()}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        </div>

        {/* 右侧：决策区 + 验收标准 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 16, minWidth: 280 }}>
          {/* 决策区 */}
          <section style={sectionStyle}>
            <h3 style={sectionTitleStyle}>⚖ 决策</h3>

            {/* 退回目标选择（条件展开）：动态列出当前状态合法的"回退类"目标阶段 */}
            {showRollback ? (
              <div style={{
                padding: 12, marginBottom: 8, borderRadius: 'var(--radius-sm)',
                background: 'var(--state-warning-tint)', border: '1px solid var(--state-warning)',
              }}>
                <div style={{ fontSize: 13, color: 'var(--state-warning)', marginBottom: 8, fontWeight: 600 }}>
                  选择退回到哪个阶段：
                </div>
                {allowedRollbackTargets.length === 0 ? (
                  <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)', padding: '8px 0' }}>
                    当前状态无可回退目标（任务可能已终态或处于异常态）。
                  </div>
                ) : (
                  allowedRollbackTargets.map((t) => {
                    const meta = TARGET_STAGE_META[t.to];
                    const label = meta?.label || t.to;
                    const desc = meta?.desc || t.action;
                    const aliasTag = meta?.alias ? `（${meta.alias}）` : '';
                    return (
                      <label key={t.to} style={{
                        display: 'block', padding: '6px 8px', marginBottom: 4,
                        cursor: 'pointer', fontSize: 13,
                        background: targetStatus === t.to ? 'var(--color-primary-tint)' : 'transparent',
                        borderRadius: 'var(--radius-sm)',
                        color: 'var(--color-text-primary)',
                      }}>
                        <input
                          type="radio"
                          name="target_status"
                          value={t.to}
                          checked={targetStatus === t.to}
                          onChange={(e) => setTargetStatus(e.target.value)}
                          style={{ marginRight: 8 }}
                        />
                        <strong>{label}</strong>
                        <span style={{ color: 'var(--color-text-tertiary)', marginLeft: 8, fontSize: 12 }}>
                          {desc}{aliasTag}
                        </span>
                      </label>
                    );
                  })
                )}
                <textarea
                  placeholder="退回原因（可选）"
                  value={rollbackReason}
                  onChange={(e) => setRollbackReason(e.target.value)}
                  style={{ ...inputStyle, minHeight: 60, marginTop: 8 }}
                />
                <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                  <button
                    onClick={handleRollback}
                    disabled={acting || !targetStatus}
                    style={dangerBtnStyle}
                  >
                    {acting ? '处理中...' : '确认退回'}
                  </button>
                  <button
                    onClick={() => { setShowRollback(false); setRollbackReason(''); }}
                    disabled={acting}
                    style={cancelBtnStyle}
                  >
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
                <button
                  onClick={handleApprove}
                  disabled={acting || !task || task.status === 'closed'}
                  style={successBtnStyle}
                  title={!task || task.status === 'closed' ? '任务已关闭或不可操作' : '通过并关闭任务'}
                >
                  ✅ 通过并关闭
                </button>
                <button
                  onClick={() => setShowRollback(true)}
                  disabled={acting || !task || task.status === 'closed'}
                  style={warningBtnStyle}
                >
                  ↩ 退回
                </button>
              </div>
            )}

            {/* 评论输入框 */}
            <textarea
              placeholder="发表评论...（普通讨论评论）"
              value={commentInput}
              onChange={(e) => setCommentInput(e.target.value)}
              style={{ ...inputStyle, minHeight: 80, resize: 'vertical' }}
            />
            <div style={{ display: 'flex', gap: 8, marginTop: 8, justifyContent: 'flex-end' }}>
              <button
                onClick={handleAddComment}
                disabled={acting || !commentInput.trim()}
                style={primaryBtnStyle}
              >
                发表评论
              </button>
            </div>
          </section>

          {/* 验收标准区 */}
          <section style={sectionStyle}>
            <h3 style={sectionTitleStyle}>✓ 验收标准（{criteria.length}）</h3>
            {criteria.length === 0 ? (
              <div style={{ color: 'var(--color-text-tertiary)', fontSize: 13, padding: 12 }}>
                暂无验收标准。
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {criteria.map((c) => {
                  const pass = c.status === 'passed' || c.status === 'pass' || c.status === 'done';
                  const fail = c.status === 'failed' || c.status === 'fail';
                  const pillCls = pass ? 'status-pill-success' : fail ? 'status-pill-error' : 'status-pill-warning';
                  return (
                    <div key={c.criteria_id} style={{
                      padding: 8, background: 'var(--color-bg-elevated)',
                      borderRadius: 'var(--radius-sm)', fontSize: 13,
                      border: '1px solid var(--color-border-subtle)',
                    }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                        <span className={`status-pill ${pillCls}`} style={pillStyle}>
                          {c.status}
                        </span>
                        <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>
                          {c.check_type} · v{c.version}
                        </span>
                      </div>
                      <div style={{ color: 'var(--color-text-secondary)' }}>{c.description}</div>
                      {c.checked_at && (
                        <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 4 }}>
                          校验于：{new Date(c.checked_at).toLocaleString()}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

// ---- 内联样式常量（与 TaskCenterPage 风格一致）----
const sectionStyle: React.CSSProperties = {
  background: 'var(--color-bg-surface)',
  borderRadius: 'var(--radius-md)',
  padding: 16,
  border: '1px solid var(--color-border-subtle)',
};
const sectionTitleStyle: React.CSSProperties = {
  margin: 0, marginBottom: 12, fontSize: 14, fontWeight: 600,
  color: 'var(--color-text-secondary)',
};

// ====== 报告导出工具条 ======
function ExportBar({ taskId, reportId }: { taskId: string; reportId: string }) {
  const [exporting, setExporting] = useState<null | string>(null);
  const [history, setHistory] = useState<TaskReportExport[]>([]);
  const [verifyMd, setVerifyMd] = useState<TaskExportVerify | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const items = await taskApi.listExports(taskId, reportId);
      setHistory(items);
    } catch (e: unknown) {
      // 静默失败：导出列表不影响主功能
    }
  }, [taskId, reportId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleExport(fmt: 'md' | 'html' | 'json') {
    setExporting(fmt);
    setError(null);
    try {
      await taskApi.exportReport(taskId, reportId, fmt);
      await refresh();
      // 浏览器原生下载（后端 GET 端点有兜底即时导出）
      window.open(taskApi.downloadExportUrl(taskId, reportId, fmt), '_blank');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(null);
    }
  }

  async function handleVerify() {
    setError(null);
    try {
      const v = await taskApi.verifyExport(taskId, reportId, 'md');
      setVerifyMd(v);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const btnStyle = (disabled: boolean): React.CSSProperties => ({
    padding: '4px 10px',
    fontSize: 12,
    border: '1px solid var(--color-border, #d0d7de)',
    borderRadius: 4,
    background: disabled ? 'var(--color-bg-disabled, #f6f8fa)' : 'var(--color-bg-elevated, #fff)',
    color: disabled ? 'var(--color-text-tertiary)' : 'var(--color-text-primary)',
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.6 : 1,
  });

  return (
    <div style={{ marginTop: 12, padding: 8, background: 'var(--color-bg-elevated)',
                  borderRadius: 'var(--radius-sm)', fontSize: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <strong style={{ color: 'var(--color-text-secondary)' }}>📤 导出报告：</strong>
        <button onClick={() => handleExport('md')}
                disabled={exporting !== null}
                style={btnStyle(exporting !== null)}>
          {exporting === 'md' ? '生成中…' : 'Markdown'}
        </button>
        <button onClick={() => handleExport('html')}
                disabled={exporting !== null}
                style={btnStyle(exporting !== null)}>
          {exporting === 'html' ? '生成中…' : 'HTML'}
        </button>
        <button onClick={() => handleExport('json')}
                disabled={exporting !== null}
                style={btnStyle(exporting !== null)}>
          {exporting === 'json' ? '生成中…' : 'JSON（含 metadata）'}
        </button>
        <button onClick={handleVerify}
                style={btnStyle(false)}>
          🔍 校验 MD
        </button>
        {verifyMd && (
          <span style={{
            color: verifyMd.verified ? 'var(--color-success, #1a7f37)'
                                     : 'var(--color-error, #cf222e)',
            fontSize: 11,
          }}>
            {verifyMd.verified ? '✓ hash 匹配' : '✗ hash 不匹配'}
            {verifyMd.expected_sha256 && ` (sha=${verifyMd.expected_sha256.slice(0, 8)}…)`}
          </span>
        )}
      </div>
      {error && (
        <div style={{ marginTop: 6, color: 'var(--color-error, #cf222e)', fontSize: 11 }}>
          ⚠ {error}
        </div>
      )}
      {history.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: 'pointer', color: 'var(--color-text-tertiary)', fontSize: 11 }}>
            导出历史（{history.length} 条）
          </summary>
          <ul style={{ margin: '4px 0 0 0', paddingLeft: 16, fontSize: 11,
                       color: 'var(--color-text-tertiary)' }}>
            {history.slice(0, 5).map(h => (
              <li key={h.export_id} style={{ fontFamily: 'monospace' }}>
                {h.exported_at} · {h.format} · sha={h.sha256.slice(0, 12)}… · {h.size_bytes}B
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}


const reportPreStyle: React.CSSProperties = {
  margin: 0, padding: 12, background: 'var(--color-bg-elevated)',
  borderRadius: 'var(--radius-sm)', border: '1px solid var(--color-border-subtle)',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
  fontSize: 13, lineHeight: 1.6, color: 'var(--color-text-primary)',
  whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflow: 'auto',
  maxHeight: 480,
};
// 报告正文 markdown 渲染容器（替代 reportPreStyle 的正文场景；自检清单 JSON 仍用 pre）
const reportMdStyle: React.CSSProperties = {
  margin: 0, padding: 12, background: 'var(--color-bg-elevated)',
  borderRadius: 'var(--radius-sm)', border: '1px solid var(--color-border-subtle)',
  fontSize: 13, lineHeight: 1.6, color: 'var(--color-text-primary)',
  overflow: 'auto', maxHeight: 480,
};
const commentItemStyle: React.CSSProperties = {
  padding: 10, background: 'var(--color-bg-elevated)',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--color-border-subtle)',
};
const pillStyle: React.CSSProperties = {
  fontSize: 11, padding: '2px 8px', borderRadius: 'var(--radius-full)',
  fontWeight: 600, display: 'inline-block',
};
const inputStyle: React.CSSProperties = {
  width: '100%', padding: '8px 12px',
  background: 'var(--color-bg-elevated)',
  border: '1px solid var(--color-border-default)',
  borderRadius: 'var(--radius-sm)', boxSizing: 'border-box',
  fontSize: 13, color: 'var(--color-text-primary)',
  fontFamily: 'inherit',
};
const primaryBtnStyle: React.CSSProperties = {
  padding: '8px 16px', background: 'var(--color-primary)', color: '#fff',
  border: 'none', borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 13,
};
const successBtnStyle: React.CSSProperties = {
  padding: '8px 16px', background: 'var(--state-success)', color: '#fff',
  border: 'none', borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 13,
  flex: 1,
};
const warningBtnStyle: React.CSSProperties = {
  padding: '8px 16px', background: 'var(--state-warning)', color: '#1a1a1a',
  border: 'none', borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 13,
  flex: 1,
};
const dangerBtnStyle: React.CSSProperties = {
  padding: '8px 16px', background: 'var(--state-error)', color: '#fff',
  border: 'none', borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 13,
  flex: 1,
};
const cancelBtnStyle: React.CSSProperties = {
  padding: '8px 16px', background: 'var(--color-bg-elevated)',
  color: 'var(--color-text-secondary)',
  border: '1px solid var(--color-border-default)',
  borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 13,
};
const linkBtnStyle: React.CSSProperties = {
  background: 'transparent', border: 'none', cursor: 'pointer',
  color: 'var(--color-primary-soft)', fontSize: 13, padding: '4px 8px',
};
