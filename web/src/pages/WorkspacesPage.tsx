/**
 * WorkspacesPage — 用户授权工作区管理页面（P0.18.7）
 *
 * 功能：
 *   - 列出所有 authorized_workspaces（含已取消授权）
 *   - 新增 / 编辑 / 取消授权（soft delete）/ 测试访问
 *   - 显示 mode / permissions / 上次使用 / 使用次数
 *   - 调用 workspace_paths 的 tier 映射（前端复制 PERMISSIONS_TO_TIER）
 *
 * 后端端点：
 *   GET    /api/workspaces
 *   POST   /api/workspaces
 *   PATCH  /api/workspaces/{id}
 *   DELETE /api/workspaces/{id}     (soft delete)
 *   POST   /api/workspaces/{id}/test
 */
import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '../lib/api';
import type {
  AuthorizedWorkspace,
  CreateWorkspacePayload,
  UpdateWorkspacePayload,
  WorkspaceAccessTestResult,
  WorkspaceMode,
  WorkspacePermissions,
  AgentTier,
} from '../lib/api';
import {
  MODE_LABELS,
  PERMISSIONS_LABELS,
  TIER_LABELS,
  workspacePermissionsToTier,
} from '../lib/api';
import { WorkspaceForm, EMPTY_FORM } from '../components/WorkspaceForm';
import type { WorkspaceFormState } from '../components/WorkspaceForm';

// ── 工具函数 ─────────────────────────────────────────────────────

function formatTime(ts: string | null | undefined): string {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', month: '2-digit', day: '2-digit' });
  } catch {
    return ts;
  }
}

function modeBadgeClass(mode: WorkspaceMode): string {
  return `ws-mode-badge ws-mode-${mode}`;
}

function permBadgeClass(perm: WorkspacePermissions): string {
  return `ws-perm-badge ws-perm-${perm}`;
}

// ── 创建/编辑表单组件（已抽取到 components/WorkspaceForm.tsx，供 Onboarding 复用） ──

// ── 主组件 ──────────────────────────────────────────────────────

export function WorkspacesPage() {
  const [workspaces, setWorkspaces] = useState<AuthorizedWorkspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [includeDisabled, setIncludeDisabled] = useState(false);

  // 弹窗状态
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<AuthorizedWorkspace | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 测试访问结果
  const [testing, setTesting] = useState<string | null>(null); // workspace_id
  const [testResults, setTestResults] = useState<Record<string, WorkspaceAccessTestResult>>({});

  // 取消授权确认
  const [deleting, setDeleting] = useState<string | null>(null);

  // P0.18.11b: 手动清理 sandbox
  const [cleanupLoading, setCleanupLoading] = useState(false);
  const [cleanupResult, setCleanupResult] = useState<{ scanned: number; deleted: number; failed: number } | null>(null);

  const loadWorkspaces = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiClient.listWorkspaces(includeDisabled);
      setWorkspaces(resp.workspaces || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [includeDisabled]);

  useEffect(() => {
    loadWorkspaces();
  }, [loadWorkspaces]);

  const handleCreate = useCallback(async (form: WorkspaceFormState) => {
    setSubmitting(true);
    try {
      const payload: CreateWorkspacePayload = {
        display_name: form.display_name,
        mode: form.mode,
        permissions: form.permissions,
        description: form.description || undefined,
        source_path: form.source_path || undefined,
        git_url: form.git_url || undefined,
        git_branch: form.git_branch || undefined,
      };
      await apiClient.createWorkspace(payload);
      setCreateOpen(false);
      await loadWorkspaces();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }, [loadWorkspaces]);

  const handleUpdate = useCallback(async (form: WorkspaceFormState) => {
    if (!editing) return;
    setSubmitting(true);
    try {
      const payload: UpdateWorkspacePayload = {
        display_name: form.display_name,
        description: form.description,
        permissions: form.permissions,
      };
      await apiClient.updateWorkspace(editing.workspace_id, payload);
      setEditing(null);
      await loadWorkspaces();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }, [editing, loadWorkspaces]);

  const handleDelete = useCallback(async (workspaceId: string) => {
    setSubmitting(true);
    try {
      await apiClient.deleteWorkspace(workspaceId);
      setDeleting(null);
      await loadWorkspaces();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }, [loadWorkspaces]);

  const handleTest = useCallback(async (workspaceId: string) => {
    setTesting(workspaceId);
    try {
      const result = await apiClient.testWorkspaceAccess(workspaceId);
      setTestResults((prev) => ({ ...prev, [workspaceId]: result }));
    } catch (e) {
      setTestResults((prev) => ({
        ...prev,
        [workspaceId]: {
          exists: false, readable: false, writable: false, execuable: false,
          skipped: false, reason: e instanceof Error ? e.message : String(e),
        },
      }));
    } finally {
      setTesting(null);
    }
  }, []);

  const handleCleanup = useCallback(async () => {
    setCleanupLoading(true);
    setCleanupResult(null);
    try {
      const r = await apiClient.cleanupWorkspacesNow();
      setCleanupResult({ scanned: r.scanned, deleted: r.deleted, failed: r.failed });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCleanupLoading(false);
    }
  }, []);

  return (
    <div className="ws-page">
      {/* 页头 */}
      <div className="ws-header">
        <div className="ws-header-info">
          <h1 className="ws-header-title">工作区授权</h1>
          <p className="ws-header-subtitle">
            管理用户授权的项目目录 · 容器化运行的 mount 范围 · tier 权限矩阵
          </p>
        </div>
        <div className="ws-header-actions">
          <label className="ws-toggle">
            <input
              type="checkbox"
              checked={includeDisabled}
              onChange={(e) => setIncludeDisabled(e.target.checked)}
            />
            <span>显示已取消授权</span>
          </label>
          <button
            className="btn-secondary"
            onClick={handleCleanup}
            disabled={cleanupLoading}
            title="立即清理过期 sandbox（默认每日 03:30 UTC 自动清理）"
          >
            {cleanupLoading ? '清理中…' : '🧹 立即清理 sandbox'}
          </button>
          <button className="btn-primary" onClick={() => setCreateOpen(true)}>
            + 添加工作区
          </button>
        </div>
      </div>

      {/* P0.18.11b: 清理结果提示 */}
      {cleanupResult && (
        <div className="ws-cleanup-banner">
          <span>
            🧹 清理完成：扫描 <strong>{cleanupResult.scanned}</strong> 个 sandbox，
            已删除 <strong>{cleanupResult.deleted}</strong>，失败 <strong>{cleanupResult.failed}</strong>
          </span>
          <button onClick={() => setCleanupResult(null)}>×</button>
        </div>
      )}

      {/* 错误提示 */}
      {error && (
        <div className="ws-error-banner">
          <span>⚠️ {error}</span>
          <button onClick={() => setError(null)}>×</button>
        </div>
      )}

      {/* 列表 */}
      {loading ? (
        <div className="ws-loading">加载中…</div>
      ) : workspaces.length === 0 ? (
        <div className="ws-empty">
          <p>暂无授权工作区</p>
          <p className="ws-empty-hint">点击「添加工作区」授权一个项目目录，启动对话时即可绑定</p>
        </div>
      ) : (
        <div className="ws-list">
          {workspaces.map((ws) => {
            const tier = workspacePermissionsToTier(ws.permissions);
            const testResult = testResults[ws.workspace_id];
            return (
              <div
                key={ws.workspace_id}
                className={`ws-card ${ws.enabled ? '' : 'ws-card-disabled'}`}
              >
                {/* 顶部：名称 + 模式 + 权限 */}
                <div className="ws-card-top">
                  <div className="ws-card-idblock">
                    <span className="ws-card-name">📁 {ws.display_name}</span>
                    <span className="ws-card-id font-mono">{ws.workspace_id.slice(0, 8)}</span>
                  </div>
                  <div className="ws-card-badges">
                    <span className={modeBadgeClass(ws.mode)}>{MODE_LABELS[ws.mode]}</span>
                    <span className={permBadgeClass(ws.permissions)}>
                      {PERMISSIONS_LABELS[ws.permissions]} · {TIER_LABELS[tier]}
                    </span>
                    {!ws.enabled && (
                      <span className="ws-disabled-badge">已取消授权</span>
                    )}
                  </div>
                </div>

                {/* 路径信息 */}
                <div className="ws-card-meta">
                  {ws.source_path && (
                    <div className="ws-meta-row">
                      <span className="ws-meta-label">主机路径:</span>
                      <code className="ws-meta-value">{ws.source_path}</code>
                    </div>
                  )}
                  {ws.git_url && (
                    <>
                      <div className="ws-meta-row">
                        <span className="ws-meta-label">Git URL:</span>
                        <code className="ws-meta-value">{ws.git_url}</code>
                      </div>
                      {ws.git_branch && (
                        <div className="ws-meta-row">
                          <span className="ws-meta-label">分支:</span>
                          <span className="ws-meta-value">{ws.git_branch}</span>
                        </div>
                      )}
                    </>
                  )}
                  {ws.description && (
                    <div className="ws-meta-row">
                      <span className="ws-meta-label">描述:</span>
                      <span className="ws-meta-value">{ws.description}</span>
                    </div>
                  )}
                  <div className="ws-meta-row">
                    <span className="ws-meta-label">使用次数:</span>
                    <span className="ws-meta-value">{ws.usage_count}</span>
                    <span className="ws-meta-label ws-meta-label-2">上次使用:</span>
                    <span className="ws-meta-value">{formatTime(ws.last_used_at)}</span>
                    <span className="ws-meta-label ws-meta-label-2">授权时间:</span>
                    <span className="ws-meta-value">{formatTime(ws.authorized_at)}</span>
                  </div>
                </div>

                {/* 测试访问结果 */}
                {testResult && (
                  <div className={`ws-test-result ${testResult.skipped ? 'ws-test-skipped' : (testResult.exists && testResult.readable ? 'ws-test-ok' : 'ws-test-fail')}`}>
                    {testResult.skipped ? (
                      <span>⏭️ 跳过测试：{testResult.reason}</span>
                    ) : (
                      <span>
                        {testResult.exists ? '✓ 路径存在' : '✗ 路径不存在'}
                        {testResult.exists && ` · 读取:${testResult.readable ? '✓' : '✗'} · 写入:${testResult.writable ? '✓' : '✗'} · 执行:${testResult.execuable ? '✓' : '✗'}`}
                        {testResult.reason && ` · ${testResult.reason}`}
                      </span>
                    )}
                  </div>
                )}

                {/* 操作按钮 */}
                <div className="ws-card-actions">
                  <button
                    className="btn-secondary btn-sm"
                    onClick={() => handleTest(ws.workspace_id)}
                    disabled={testing === ws.workspace_id}
                  >
                    {testing === ws.workspace_id ? '测试中…' : '测试访问'}
                  </button>
                  <button
                    className="btn-secondary btn-sm"
                    onClick={() => setEditing(ws)}
                  >
                    编辑
                  </button>
                  {ws.enabled ? (
                    <button
                      className="btn-danger btn-sm"
                      onClick={() => setDeleting(ws.workspace_id)}
                      disabled={submitting}
                    >
                      取消授权
                    </button>
                  ) : (
                    <button
                      className="btn-primary btn-sm"
                      onClick={async () => {
                        await apiClient.updateWorkspace(ws.workspace_id, { enabled: 1 });
                        await loadWorkspaces();
                      }}
                      disabled={submitting}
                    >
                      重新启用
                    </button>
                  )}
                </div>

                {/* 取消授权确认 */}
                {deleting === ws.workspace_id && (
                  <div className="ws-confirm-overlay">
                    <div className="ws-confirm-dialog">
                      <p>确定取消授权「{ws.display_name}」吗？</p>
                      <p className="ws-confirm-hint">
                        取消后新对话不能选此工作区；旧 run 仍可读已 prepared 的 sandbox（软删除）。
                      </p>
                      <div className="ws-confirm-actions">
                        <button className="btn-secondary" onClick={() => setDeleting(null)}>取消</button>
                        <button
                          className="btn-danger"
                          onClick={() => handleDelete(ws.workspace_id)}
                          disabled={submitting}
                        >
                          {submitting ? '处理中…' : '确认取消授权'}
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 新建弹窗 */}
      {createOpen && (
        <div className="ws-modal-overlay" onClick={() => setCreateOpen(false)}>
          <div className="ws-modal" onClick={(e) => e.stopPropagation()}>
            <div className="ws-modal-header">
              <h2>添加授权工作区</h2>
              <button className="ws-modal-close" onClick={() => setCreateOpen(false)}>×</button>
            </div>
            <WorkspaceForm
              initial={EMPTY_FORM}
              onSubmit={handleCreate}
              onCancel={() => setCreateOpen(false)}
              submitting={submitting}
            />
          </div>
        </div>
      )}

      {/* 编辑弹窗 */}
      {editing && (
        <div className="ws-modal-overlay" onClick={() => setEditing(null)}>
          <div className="ws-modal" onClick={(e) => e.stopPropagation()}>
            <div className="ws-modal-header">
              <h2>编辑工作区：{editing.display_name}</h2>
              <button className="ws-modal-close" onClick={() => setEditing(null)}>×</button>
            </div>
            <WorkspaceForm
              initial={{
                display_name: editing.display_name,
                description: editing.description ?? '',
                mode: editing.mode,
                permissions: editing.permissions,
                source_path: editing.source_path ?? '',
                git_url: editing.git_url ?? '',
                git_branch: editing.git_branch ?? 'main',
              }}
              onSubmit={handleUpdate}
              onCancel={() => setEditing(null)}
              submitting={submitting}
            />
          </div>
        </div>
      )}
    </div>
  );
}
