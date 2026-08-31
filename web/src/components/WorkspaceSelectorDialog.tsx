/**
 * WorkspaceSelectorDialog — 新对话时选择 workspace 的弹窗（P0.18.7c）
 *
 * 3 个选项：
 *   A. 项目工作区 — 从已授权列表选 1 个
 *   B. 通用对话 — 不绑定项目
 *   C. 历史对话恢复 — 最近 5 个 session
 *
 * 选项 A 触发 tier 校验：workspace.permissions ≥ session 默认 agent tier，否则弹 TierUpgradeDialog
 */
import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  apiClient,
  MODE_LABELS,
  PERMISSIONS_LABELS,
  TIER_LABELS,
  workspacePermissionsToTier,
  isTierCompatible,
} from '../lib/api';
import type {
  AuthorizedWorkspace,
  AgentTier,
} from '../lib/api';
import { TierUpgradeDialog } from './TierUpgradeDialog';

interface HistorySession {
  session_id: string;
  title?: string;
  last_activity_at?: string;
  agent_id?: string;
  workspace_id?: string | null;
}

interface WorkspaceSelectorDialogProps {
  open: boolean;
  onClose: () => void;
  /** 当前 session 的默认 agent tier（来自 agent yaml） */
  defaultAgentTier: AgentTier;
  /** 当前 session 默认 agent 名（展示用） */
  defaultAgentName?: string;
  /** 选择 workspace 后回调（workspace_id 可为 null 表示通用对话） */
  onSelect: (selection: { workspaceId: string | null; mode: 'project' | 'general' }) => void;
}

type Mode = 'project' | 'general' | 'history';

function formatRelativeTime(ts: string | null | undefined): string {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    const diff = Date.now() - d.getTime();
    const min = Math.floor(diff / 60000);
    if (min < 1) return '刚刚';
    if (min < 60) return `${min} 分钟前`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr} 小时前`;
    const day = Math.floor(hr / 24);
    if (day < 30) return `${day} 天前`;
    return d.toLocaleDateString('zh-CN');
  } catch {
    return ts;
  }
}

function modeIcon(mode: AuthorizedWorkspace['mode']): string {
  switch (mode) {
    case 'bind_mount': return '🔗';
    case 'local_copy': return '📋';
    case 'git_clone': return '🌿';
    case 'isolated': return '📦';
  }
}

export function WorkspaceSelectorDialog({
  open,
  onClose,
  defaultAgentTier,
  defaultAgentName,
  onSelect,
}: WorkspaceSelectorDialogProps) {
  const [mode, setMode] = useState<Mode>('project');

  // 项目工作区列表
  const [workspaces, setWorkspaces] = useState<AuthorizedWorkspace[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 历史 session 列表
  const [history, setHistory] = useState<HistorySession[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // tier 升级弹窗状态
  const [upgradeTarget, setUpgradeTarget] = useState<AuthorizedWorkspace | null>(null);

  // 加载项目工作区
  useEffect(() => {
    if (!open) return;
    if (mode !== 'project') return;
    setLoading(true);
    setError(null);
    apiClient
      .listWorkspaces(false)
      .then((resp) => setWorkspaces(resp.workspaces || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [open, mode]);

  // 加载历史 session
  useEffect(() => {
    if (!open) return;
    if (mode !== 'history') return;
    setHistoryLoading(true);
    apiClient
      .listSessions(undefined, undefined, 5, 0)
      .then((resp) => {
        const items = (resp.sessions || []).map((s) => ({
          session_id: String(s.session_id || ''),
          title: (s.title as string) || undefined,
          last_activity_at: (s.last_activity_at as string) || undefined,
          agent_id: (s.agent_id as string) || undefined,
          workspace_id: (s.workspace_id as string | null) ?? null,
        }));
        setHistory(items);
      })
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false));
  }, [open, mode]);

  // 项目工作区历史映射（用于显示历史 session 的 workspace 名）
  const workspaceById = useMemo(() => {
    const map: Record<string, AuthorizedWorkspace> = {};
    workspaces.forEach((w) => { map[w.workspace_id] = w; });
    return map;
  }, [workspaces]);

  const handleSelectWorkspace = useCallback((ws: AuthorizedWorkspace) => {
    const wsTier = workspacePermissionsToTier(ws.permissions);
    if (!isTierCompatible(wsTier, defaultAgentTier)) {
      setUpgradeTarget(ws);
      return;
    }
    onSelect({ workspaceId: ws.workspace_id, mode: 'project' });
  }, [defaultAgentTier, onSelect]);

  const handleUpgradeConfirmed = useCallback(async (newPermissions: AuthorizedWorkspace['permissions']) => {
    if (!upgradeTarget) return;
    try {
      await apiClient.updateWorkspace(upgradeTarget.workspace_id, {
        permissions: newPermissions,
      });
      onSelect({ workspaceId: upgradeTarget.workspace_id, mode: 'project' });
      setUpgradeTarget(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setUpgradeTarget(null);
    }
  }, [upgradeTarget, onSelect]);

  const handleSelectGeneral = useCallback(() => {
    onSelect({ workspaceId: null, mode: 'general' });
  }, [onSelect]);

  if (!open) return null;

  return (
    <div className="ws-modal-overlay" onClick={onClose}>
      <div className="ws-modal ws-modal-selector" onClick={(e) => e.stopPropagation()}>
        <div className="ws-modal-header">
          <div>
            <h2>开始新对话</h2>
            <p className="ws-modal-subtitle">
              选择工作区模式 · 默认 Agent：<strong>{defaultAgentName || 'manager'}</strong> · {TIER_LABELS[defaultAgentTier]}
            </p>
          </div>
          <button className="ws-modal-close" onClick={onClose}>×</button>
        </div>

        {/* 三选项 tab */}
        <div className="ws-selector-tabs">
          <button
            className={`ws-selector-tab ${mode === 'project' ? 'active' : ''}`}
            onClick={() => setMode('project')}
          >
            📁 项目工作区
          </button>
          <button
            className={`ws-selector-tab ${mode === 'general' ? 'active' : ''}`}
            onClick={() => setMode('general')}
          >
            💬 通用对话
          </button>
          <button
            className={`ws-selector-tab ${mode === 'history' ? 'active' : ''}`}
            onClick={() => setMode('history')}
          >
            🕘 历史对话
          </button>
        </div>

        {error && (
          <div className="ws-error-banner">
            <span>⚠️ {error}</span>
            <button onClick={() => setError(null)}>×</button>
          </div>
        )}

        <div className="ws-selector-body">
          {mode === 'project' && (
            <>
              {loading ? (
                <div className="ws-loading">加载工作区…</div>
              ) : workspaces.length === 0 ? (
                <div className="ws-empty">
                  <p>暂无授权工作区</p>
                  <p className="ws-empty-hint">
                    请前往「运行时配置 → 工作区授权」页面添加；通用对话可正常进行（部分工具不可用）
                  </p>
                  <button className="btn-secondary" onClick={handleSelectGeneral}>
                    使用通用对话
                  </button>
                </div>
              ) : (
                <div className="ws-selector-list">
                  {workspaces.map((ws) => {
                    const wsTier = workspacePermissionsToTier(ws.permissions);
                    const compatible = isTierCompatible(wsTier, defaultAgentTier);
                    return (
                      <button
                        key={ws.workspace_id}
                        className={`ws-selector-card ${compatible ? '' : 'ws-selector-card-warn'}`}
                        onClick={() => handleSelectWorkspace(ws)}
                        title={ws.source_path || ws.git_url || ''}
                      >
                        <div className="ws-selector-card-icon">{modeIcon(ws.mode)}</div>
                        <div className="ws-selector-card-body">
                          <div className="ws-selector-card-name">
                            {ws.display_name}
                            {!compatible && <span className="ws-warn-badge">权限不足</span>}
                          </div>
                          <div className="ws-selector-card-meta">
                            <span>{MODE_LABELS[ws.mode]}</span>
                            <span>·</span>
                            <span>{PERMISSIONS_LABELS[ws.permissions]}</span>
                            <span>·</span>
                            <span>{TIER_LABELS[wsTier]}</span>
                          </div>
                          {ws.source_path && (
                            <code className="ws-selector-card-path">{ws.source_path}</code>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </>
          )}

          {mode === 'general' && (
            <div className="ws-general-card">
              <div className="ws-general-icon">💬</div>
              <h3>通用对话模式</h3>
              <p>不绑定任何项目工作区，可正常对话与知识问答。</p>
              <div className="ws-general-restrictions">
                <p>限制：</p>
                <ul>
                  <li>无法使用 <code>write_file</code> / <code>edit_file</code> / <code>bash</code> / <code>run_command</code></li>
                  <li>无法触发工作流（<code>trigger_workflow</code>）</li>
                  <li>只能访问 <code>~/.agentops/workspaces/_general/{'{session_id}'}/</code> 下的文件</li>
                </ul>
              </div>
              <button className="btn-primary" onClick={handleSelectGeneral}>
                使用通用对话
              </button>
            </div>
          )}

          {mode === 'history' && (
            <>
              {historyLoading ? (
                <div className="ws-loading">加载历史对话…</div>
              ) : history.length === 0 ? (
                <div className="ws-empty">
                  <p>暂无历史对话</p>
                </div>
              ) : (
                <div className="ws-selector-list">
                  {history.map((s) => {
                    const ws = s.workspace_id ? workspaceById[s.workspace_id] : null;
                    const wsDisabled = !!s.workspace_id && !ws; // workspace 已取消授权
                    return (
                      <button
                        key={s.session_id}
                        className={`ws-selector-card ${wsDisabled ? 'ws-selector-card-disabled' : ''}`}
                        onClick={() => onSelect({ workspaceId: s.workspace_id || null, mode: 'project' })}
                        title={wsDisabled ? '此工作区已取消授权' : ''}
                      >
                        <div className="ws-selector-card-icon">🕘</div>
                        <div className="ws-selector-card-body">
                          <div className="ws-selector-card-name">
                            {s.title || s.session_id.slice(-12)}
                            {wsDisabled && <span className="ws-warn-badge">工作区已失效</span>}
                          </div>
                          <div className="ws-selector-card-meta">
                            <span>{s.agent_id || 'manager'}</span>
                            <span>·</span>
                            <span>{formatRelativeTime(s.last_activity_at)}</span>
                            {ws && (
                              <>
                                <span>·</span>
                                <span>📁 {ws.display_name}</span>
                              </>
                            )}
                            {!wsDisabled && !ws && s.workspace_id === null && (
                              <>
                                <span>·</span>
                                <span>通用对话</span>
                              </>
                            )}
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Tier 升级弹窗（嵌套） */}
      {upgradeTarget && (
        <TierUpgradeDialog
          workspace={upgradeTarget}
          requiredTier={defaultAgentTier}
          onConfirm={handleUpgradeConfirmed}
          onCancel={() => setUpgradeTarget(null)}
        />
      )}
    </div>
  );
}