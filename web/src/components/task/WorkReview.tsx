// web/src/components/task/WorkReview.tsx
// V1 工作回顾（活动时间线 + 交付物列表）
// 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.8 V12
//
// 两个区块：
// 1. 活动时间线：按 created_at 降序展示 task_activities（最新在上）
// 2. 交付物列表：展示 task_artifacts（type/path/hash/description）

import { useState, useEffect, useCallback } from 'react';
import {
  taskApi,
  type TaskActivity,
  type TaskArtifact,
} from '../../api/taskApi';
import { renderMarkdown } from '../../lib/markdown';

// 活动变更字段 → 中文标签
const FIELD_LABELS: Record<string, string> = {
  status: '状态',
  assignee_name: '负责人',
  assignee_id: '负责人ID',
  title: '标题',
  description: '描述',
  risk_level: '风险等级',
  style_id: '执行风格',
  terminal_session_id: '终端会话',
  parent_task_id: '父任务',
  source_idea_id: '来源灵感',
  thread_id: '对话线程',
  approved: '审批',
  sort_order: '排序',
};

// 交付物类型 → 图标 + 中文标签
const ARTIFACT_TYPE_LABELS: Record<string, { icon: string; label: string }> = {
  code: { icon: '{ }', label: '代码' },
  doc: { icon: '📄', label: '文档' },
  test: { icon: '✓', label: '测试' },
  config: { icon: '⚙', label: '配置' },
  report: { icon: '📊', label: '报告' },
  other: { icon: '📦', label: '其他' },
};

// 变更值渲染：短值纯文本；长文本/多行（agent 写的 markdown 描述等）→ markdown 渲染
function ChangeValue({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === '') {
    return <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>;
  }
  const str = typeof value === 'string' ? value : JSON.stringify(value);
  if (str.includes('\n') || str.length > 80) {
    return (
      <div
        className="md-content"
        style={{ width: '100%', fontSize: 12, color: 'var(--color-text-secondary)' }}
        dangerouslySetInnerHTML={{ __html: renderMarkdown(str) }}
      />
    );
  }
  return <span>{str}</span>;
}

export default function WorkReview({ taskId }: { taskId: string }) {
  const [activities, setActivities] = useState<TaskActivity[]>([]);
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadAll = useCallback(async () => {
    try {
      const [actRes, artRes] = await Promise.all([
        taskApi.listActivities(taskId),
        taskApi.listArtifacts(taskId),
      ]);
      setActivities(actRes.activities || []);
      setArtifacts(artRes.artifacts || []);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 活动按 created_at 降序（最新在上）
  const sortedActivities = [...activities].sort((a, b) =>
    a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0,
  );

  // 交付物按 created_at 降序
  const sortedArtifacts = [...artifacts].sort((a, b) =>
    a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0,
  );

  if (loading) {
    return (
      <div style={{ padding: 16, color: 'var(--color-text-tertiary)', fontSize: 13 }}>
        加载工作回顾...
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {error && (
        <div
          style={{
            background: 'rgba(239, 68, 68, 0.12)',
            color: '#fca5a5',
            padding: '8px 12px',
            borderRadius: 'var(--radius-sm)',
            fontSize: 13,
            border: '1px solid rgba(239, 68, 68, 0.3)',
          }}
        >
          {error}
        </div>
      )}

      {/* 活动时间线 */}
      <section
        style={{
          background: 'var(--color-bg-surface)',
          borderRadius: 'var(--radius-md)',
          padding: 16,
          border: '1px solid var(--color-border-subtle)',
        }}
      >
        <h4
          style={{
            margin: 0,
            marginBottom: 12,
            fontSize: 14,
            fontWeight: 600,
            color: 'var(--color-text-secondary)',
          }}
        >
          📋 活动时间线（{sortedActivities.length}）
        </h4>
        {sortedActivities.length === 0 ? (
          <div style={{ color: 'var(--color-text-tertiary)', fontSize: 13, padding: 8 }}>
            暂无活动记录。
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {sortedActivities.map((act) => {
              const changes = act.changes || {};
              const entries = Object.entries(changes);
              return (
                <div
                  key={act.activity_id}
                  style={{
                    padding: 8,
                    background: 'var(--color-bg-elevated)',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--color-border-subtle)',
                    fontSize: 13,
                    borderLeft: '3px solid var(--color-primary)',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      marginBottom: 4,
                    }}
                  >
                    <span style={{ fontWeight: 600, color: 'var(--color-text-primary)' }}>
                      {act.actor_name || act.actor_id || act.actor_type}
                    </span>
                    <span
                      style={{
                        fontSize: 11,
                        padding: '1px 6px',
                        borderRadius: 'var(--radius-full)',
                        background: 'var(--color-bg-surface)',
                        color: 'var(--color-text-tertiary)',
                      }}
                    >
                      {act.actor_type}
                    </span>
                    <span
                      style={{
                        fontSize: 11,
                        color: 'var(--color-text-tertiary)',
                        marginLeft: 'auto',
                      }}
                    >
                      {new Date(act.created_at).toLocaleString('zh-CN', { hour12: false })}
                    </span>
                  </div>
                  <div
                    style={{
                      display: 'flex',
                      flexWrap: 'wrap',
                      gap: '4px 12px',
                      color: 'var(--color-text-secondary)',
                      fontSize: 12,
                    }}
                  >
                    {entries.length === 0 ? (
                      <span style={{ color: 'var(--color-text-tertiary)' }}>(无字段变更)</span>
                    ) : (
                      entries.map(([k, v]) => {
                        const val = v as { before?: unknown; after?: unknown } | null;
                        const isDiff = val !== null && typeof val === 'object' &&
                          ('before' in val || 'after' in val);
                        return (
                          <div key={k} style={{ minWidth: 0 }}>
                            <strong style={{ color: 'var(--color-text-primary)' }}>
                              {FIELD_LABELS[k] || k}
                            </strong>
                            ：{isDiff ? (
                              <>
                                <ChangeValue value={val!.before} />
                                <span style={{ color: 'var(--color-text-tertiary)' }}> → </span>
                                <ChangeValue value={val!.after} />
                              </>
                            ) : (
                              <ChangeValue value={v} />
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* 交付物列表 */}
      <section
        style={{
          background: 'var(--color-bg-surface)',
          borderRadius: 'var(--radius-md)',
          padding: 16,
          border: '1px solid var(--color-border-subtle)',
        }}
      >
        <h4
          style={{
            margin: 0,
            marginBottom: 12,
            fontSize: 14,
            fontWeight: 600,
            color: 'var(--color-text-secondary)',
          }}
        >
          📦 交付物（{sortedArtifacts.length}）
        </h4>
        {sortedArtifacts.length === 0 ? (
          <div style={{ color: 'var(--color-text-tertiary)', fontSize: 13, padding: 8 }}>
            暂无交付物。
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {sortedArtifacts.map((art) => {
              const meta =
                ARTIFACT_TYPE_LABELS[art.type] || ARTIFACT_TYPE_LABELS.other;
              return (
                <div
                  key={art.artifact_id}
                  style={{
                    padding: 10,
                    background: 'var(--color-bg-elevated)',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--color-border-subtle)',
                    fontSize: 13,
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 10,
                  }}
                >
                  <span
                    style={{
                      fontFamily: 'ui-monospace, monospace',
                      fontSize: 16,
                      color: 'var(--color-primary-soft)',
                      minWidth: 24,
                      textAlign: 'center',
                    }}
                  >
                    {meta.icon}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        marginBottom: 4,
                      }}
                    >
                      <span
                        style={{
                          fontSize: 11,
                          padding: '1px 6px',
                          borderRadius: 'var(--radius-full)',
                          background: 'var(--color-primary-tint)',
                          color: 'var(--color-primary-soft)',
                          fontWeight: 600,
                        }}
                      >
                        {meta.label}
                      </span>
                      <span
                        style={{
                          fontSize: 11,
                          color: 'var(--color-text-tertiary)',
                        }}
                      >
                        v{art.version}
                      </span>
                      <span
                        style={{
                          fontSize: 11,
                          color: 'var(--color-text-tertiary)',
                          marginLeft: 'auto',
                        }}
                      >
                        {new Date(art.created_at).toLocaleString('zh-CN', { hour12: false })}
                      </span>
                    </div>
                    {art.description && (
                      <div
                        className="md-content"
                        style={{ color: 'var(--color-text-secondary)', marginBottom: 4 }}
                        dangerouslySetInnerHTML={{ __html: renderMarkdown(art.description) }}
                      />
                    )}
                    {art.path && (
                      <div
                        style={{
                          fontFamily: 'ui-monospace, monospace',
                          fontSize: 12,
                          color: 'var(--color-text-tertiary)',
                          wordBreak: 'break-all',
                        }}
                      >
                        {art.path}
                      </div>
                    )}
                    {art.content_hash && (
                      <div
                        style={{
                          fontFamily: 'ui-monospace, monospace',
                          fontSize: 11,
                          color: 'var(--color-text-tertiary)',
                          marginTop: 2,
                        }}
                      >
                        hash: {art.content_hash.slice(0, 16)}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
