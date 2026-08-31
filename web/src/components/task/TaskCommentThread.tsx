// web/src/components/task/TaskCommentThread.tsx
// V3.1 活动 + 评论混合时间线（参考 Taskboard issue 详情布局重构）
// - 单一时间线：task_activities（字段级变更事件）+ task_comments 按时间合并
// - 事件行：图标 + 谁做了什么 + 相对时间
// - 评论条目：头像 + 作者 + 时间 + 正文（agent 回复高亮）
// - 底部评论输入框：@mention 快捷按钮 + Ctrl+Enter 发送
// - @agent 评论 → 后台派发 conversational run → 轮询 agent 回复落库

import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { taskApi, type TaskComment, type TaskActivity } from '../../api/taskApi';
import { MENTION_AGENTS, renderMarkdownWithMentions } from '../../lib/markdown';

// 解析 <think>...</think> 思考块（MiniMax 等模型输出）：折叠展示，可展开
export function parseThink(body: string): { think: string | null; rest: string } {
  const m = body.match(/<think>([\s\S]*?)<\/think>\s*/);
  if (!m) return { think: null, rest: body };
  return { think: m[1].trim(), rest: body.replace(m[0], '').trim() };
}

export function ThinkBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginBottom: 8 }}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="点击展开/收起思考过程"
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '3px 10px 3px 8px', fontSize: 11, cursor: 'pointer',
          fontWeight: 500,
          background: 'var(--color-bg-base)',
          color: 'var(--color-text-secondary)',
          border: '1px solid var(--color-border-default)',
          borderLeft: '3px solid var(--color-primary-soft)',
          borderRadius: 'var(--radius-sm)',
          transition: 'background 0.12s, color 0.12s',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = 'var(--color-bg-elevated)';
          e.currentTarget.style.color = 'var(--color-text-primary)';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = 'var(--color-bg-base)';
          e.currentTarget.style.color = 'var(--color-text-secondary)';
        }}
      >
        <span style={{ fontSize: 9, color: 'var(--color-primary-soft)', transition: 'transform 0.15s', transform: open ? 'rotate(90deg)' : 'none' }}>▶</span>
        思考过程
        <span style={{ color: 'var(--color-text-tertiary)', fontWeight: 400 }}>· {open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div style={{
          marginTop: 6, padding: '8px 12px',
          fontSize: 12, lineHeight: 1.6, whiteSpace: 'pre-wrap',
          color: 'var(--color-text-secondary)',
          background: 'var(--color-bg-base)',
          border: '1px solid var(--color-border-subtle)',
          borderLeft: '3px solid var(--color-primary-soft)',
          borderRadius: 'var(--radius-sm)',
          maxHeight: 300, overflow: 'auto',
        }}>
          {content}
        </div>
      )}
    </div>
  );
}

// 白名单 = 后端 task 域 agent（lib/markdown.ts MENTION_AGENTS）
const STATUS_LABELS: Record<string, string> = {
  idea: '灵感', backlog: '待办池', discussing: '讨论中', decomposing: '拆解中',
  in_progress: '进行中', blocked: '被阻塞', validating: '验证中',
  reviewing: '评审中', closing: '关闭中', closed: '已关闭',
  canceled: '已取消', abandoned: '已废弃',
};

function mentionsIn(body: string): string[] {
  const found: string[] = [];
  MENTION_AGENTS.forEach((a) => {
    if (body.includes(`@${a.id}`)) found.push(a.id);
  });
  return found;
}

function relTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const diff = Date.now() - t;
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} 天前`;
  return new Date(iso).toLocaleDateString('zh-CN');
}

// 活动事件 → 可读描述
function describeActivity(a: TaskActivity): { icon: string; text: string } {
  const changes = a.changes || {};
  const who = a.actor_name || (a.actor_type === 'agent' ? 'Agent' : '用户');
  const parts: string[] = [];
  Object.entries(changes).forEach(([field, v]) => {
    const val = v as Record<string, unknown> | undefined;
    if (field === 'status' && val?.after) {
      parts.push(`状态改为「${STATUS_LABELS[String(val.after)] || val.after}」`);
    } else if (field === 'dispatch') {
      parts.push('派发了编码执行');
    } else if (field === 'assignee_name' && val?.after) {
      parts.push(`指派给 ${val.after}`);
    } else if (field === 'title') {
      parts.push('更新了标题');
    } else if (field === 'description') {
      parts.push('更新了描述');
    } else if (field === 'risk_level' && val?.after) {
      parts.push(`风险调整为 ${val.after}`);
    } else {
      parts.push(`更新了 ${field}`);
    }
  });
  if (parts.length === 0) parts.push('更新了任务');
  return { icon: '⚙', text: `${who} ${parts.join('、')}` };
}

type FeedItem =
  | { kind: 'activity'; time: string; data: TaskActivity }
  | { kind: 'comment'; time: string; data: TaskComment };

export default function TaskCommentThread({
  taskId,
}: {
  taskId: string;
}) {
  const [comments, setComments] = useState<TaskComment[]>([]);
  const [activities, setActivities] = useState<TaskActivity[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [waitingAgent, setWaitingAgent] = useState(false);
  const [error, setError] = useState('');
  const listRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const [c, a] = await Promise.all([
        taskApi.listComments(taskId),
        taskApi.listActivities(taskId).catch(() => ({ activities: [] as TaskActivity[] })),
      ]);
      setComments(c.comments || []);
      setActivities(a.activities || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载评论失败');
    }
  }, [taskId]);

  useEffect(() => {
    setComments([]);
    setActivities([]);
    setWaitingAgent(false);
    load();
  }, [load]);

  // 底部自动滚动
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [comments.length, activities.length]);

  // 等待 agent 回复期间 3s 轮询（最长 90s）
  useEffect(() => {
    if (!waitingAgent) {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    const started = Date.now();
    pollRef.current = setInterval(async () => {
      await load();
      if (Date.now() - started > 90000) {
        setWaitingAgent(false);
      }
    }, 3000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [waitingAgent, load]);

  // agent 回复到达 → 停止等待
  useEffect(() => {
    if (waitingAgent && comments.some((c) => c.author_type === 'agent')) {
      const lastAgent = comments.filter((c) => c.author_type === 'agent').pop();
      const lastUser = comments.filter((c) => c.author_type === 'user').pop();
      if (lastAgent && lastUser && lastAgent.created_at >= lastUser.created_at) {
        setWaitingAgent(false);
      }
    }
  }, [comments, waitingAgent]);

  const send = async () => {
    const body = input.trim();
    if (!body || sending) return;
    setSending(true);
    setError('');
    try {
      const c = await taskApi.addComment(taskId, {
        body,
        author_type: 'user',
        author_name: '我',
      });
      setComments((prev) => [...prev, c]);
      setInput('');
      // 含 @mention → 后台派发 agent 回复，进入轮询
      if (mentionsIn(body).length > 0) {
        setWaitingAgent(true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '发送失败');
    } finally {
      setSending(false);
    }
  };

  const insertMention = (agentId: string) => {
    setInput((prev) => {
      if (prev.includes(`@${agentId}`)) return prev;
      return prev ? `${prev} @${agentId} ` : `@${agentId} `;
    });
  };

  // 合并时间线
  const feed = useMemo<FeedItem[]>(() => {
    const items: FeedItem[] = [
      ...activities.map((a) => ({ kind: 'activity' as const, time: a.created_at, data: a })),
      ...comments.map((c) => ({ kind: 'comment' as const, time: c.created_at, data: c })),
    ];
    return items.sort((x, y) => new Date(x.time).getTime() - new Date(y.time).getTime());
  }, [activities, comments]);

  return (
    <div style={{
      background: 'var(--color-bg-surface)',
      border: '1px solid var(--color-border-subtle)',
      borderRadius: 'var(--radius-md)',
      padding: 16,
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        fontSize: 13, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 12,
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        活动与评论
        <span style={{
          fontSize: 11, fontWeight: 400, color: 'var(--color-text-tertiary)',
          background: 'var(--color-bg-elevated)', padding: '1px 8px', borderRadius: 'var(--radius-full)',
        }}>
          {feed.length}
        </span>
      </div>

      {/* 时间线 */}
      <div ref={listRef} style={{
        flex: 1, overflow: 'auto', maxHeight: 480, minHeight: 100,
        padding: '2px 2px 8px',
      }}>
        {feed.length === 0 && !waitingAgent && (
          <div style={{
            color: 'var(--color-text-tertiary)', fontSize: 12, textAlign: 'center',
            padding: '24px 0',
          }}>
            暂无动态。@coding_agent 试试让它分析任务并给出执行建议。
          </div>
        )}
        {feed.map((item, idx) => {
          if (item.kind === 'activity') {
            const { icon, text } = describeActivity(item.data);
            return (
              <div
                key={`act-${item.data.activity_id}`}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  padding: '6px 4px',
                  position: 'relative',
                }}
              >
                {/* 时间线节点 */}
                <span style={{
                  width: 18, height: 18, borderRadius: '50%', flexShrink: 0,
                  background: 'var(--color-bg-elevated)', color: 'var(--color-text-tertiary)',
                  border: '1px solid var(--color-border-subtle)',
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 9, zIndex: 1,
                }}>
                  {icon}
                </span>
                {idx < feed.length - 1 && (
                  <span style={{
                    position: 'absolute', left: 13, top: 24, bottom: -6,
                    width: 1, background: 'var(--color-border-subtle)',
                  }} />
                )}
                <span style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{text}</span>
                <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginLeft: 'auto', whiteSpace: 'nowrap' }}>
                  {relTime(item.time)}
                </span>
              </div>
            );
          }
          const c = item.data;
          const isAgent = c.author_type === 'agent';
          return (
            <div
              key={`cmt-${c.comment_id}`}
              style={{ display: 'flex', gap: 10, padding: '8px 4px', position: 'relative' }}
            >
              {idx < feed.length - 1 && (
                <span style={{
                  position: 'absolute', left: 13, top: 36, bottom: -8,
                  width: 1, background: 'var(--color-border-subtle)',
                }} />
              )}
              <span style={{
                width: 26, height: 26, borderRadius: '50%', flexShrink: 0, zIndex: 1,
                background: isAgent ? 'rgba(124,110,230,0.18)' : 'var(--color-bg-elevated)',
                color: isAgent ? 'var(--color-primary-soft)' : 'var(--color-text-secondary)',
                border: `1px solid ${isAgent ? 'rgba(124,110,230,0.4)' : 'var(--color-border-subtle)'}`,
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 12,
              }}>
                {isAgent ? '🤖' : (c.author_name || '我').slice(0, 1)}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 3 }}>
                  <span style={{
                    fontSize: 12, fontWeight: 600,
                    color: isAgent ? 'var(--color-primary-soft)' : 'var(--color-text-primary)',
                  }}>
                    {c.author_name || (isAgent ? 'agent' : '用户')}
                  </span>
                  <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>
                    {relTime(c.created_at)}
                  </span>
                </div>
                <div style={{
                  fontSize: 13, color: 'var(--color-text-primary)',
                  lineHeight: 1.6,
                  background: isAgent ? 'rgba(124,110,230,0.07)' : 'var(--color-bg-elevated)',
                  border: `1px solid ${isAgent ? 'rgba(124,110,230,0.25)' : 'var(--color-border-subtle)'}`,
                  borderRadius: 'var(--radius-sm)',
                  padding: '8px 12px',
                }}>
                  {(() => {
                    const { think, rest } = parseThink(c.body);
                    return (
                      <>
                        {think !== null && <ThinkBlock content={think} />}
                        {rest && (
                          <div
                            className="md-content"
                            style={{ fontSize: 13, lineHeight: 1.6 }}
                            dangerouslySetInnerHTML={{ __html: renderMarkdownWithMentions(rest) }}
                          />
                        )}
                      </>
                    );
                  })()}
                </div>
              </div>
            </div>
          );
        })}
        {waitingAgent && (
          <div style={{ display: 'flex', gap: 10, padding: '8px 4px' }}>
            <span style={{
              width: 26, height: 26, borderRadius: '50%', flexShrink: 0,
              background: 'rgba(124,110,230,0.18)', border: '1px dashed rgba(124,110,230,0.4)',
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 12,
            }}>
              🤖
            </span>
            <div style={{
              padding: '8px 12px', borderRadius: 'var(--radius-sm)',
              background: 'rgba(124,110,230,0.06)',
              border: '1px dashed rgba(124,110,230,0.35)',
              fontSize: 12, color: 'var(--color-primary-soft)', alignSelf: 'center',
            }}>
              Agent 正在分析任务上下文（任务信息 + 关系 + 关联文档 + 评论），稍候…
            </div>
          </div>
        )}
      </div>

      {error && (
        <div style={{ color: '#fca5a5', fontSize: 12, marginBottom: 6 }}>{error}</div>
      )}

      {/* @mention 快捷插入 */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
        {MENTION_AGENTS.map((a) => (
          <button
            key={a.id}
            onClick={() => insertMention(a.id)}
            title={`@${a.id}（${a.desc}）`}
            style={{
              padding: '2px 10px', fontSize: 11, cursor: 'pointer',
              background: 'var(--color-bg-elevated)',
              color: 'var(--color-primary-soft)',
              border: '1px solid var(--color-border-subtle)',
              borderRadius: 'var(--radius-full)',
            }}
          >
            @{a.label}
          </button>
        ))}
      </div>

      {/* 输入区 */}
      <div style={{
        border: '1px solid var(--color-border-default)',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--color-bg-elevated)',
        padding: 8,
      }}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              send();
            }
          }}
          placeholder="留下评论…（@coding_agent 召唤 agent，Ctrl+Enter 发送）"
          style={{
            width: '100%', padding: '6px 8px', minHeight: 40, resize: 'vertical',
            background: 'transparent',
            border: 'none', outline: 'none', boxSizing: 'border-box',
            fontSize: 13, color: 'var(--color-text-primary)',
            fontFamily: 'inherit',
          }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 6 }}>
          <button
            onClick={send}
            disabled={sending || !input.trim()}
            style={{
              padding: '5px 16px', background: 'var(--color-primary)', color: '#fff',
              border: 'none', borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 12,
              opacity: sending || !input.trim() ? 0.5 : 1,
            }}
          >
            {sending ? '发送中...' : '评论'}
          </button>
        </div>
      </div>
    </div>
  );
}
