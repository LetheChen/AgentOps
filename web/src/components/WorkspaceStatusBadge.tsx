/**
 * WorkspaceStatusBadge — TopBar 显示当前 session 的 workspace + tier（P0.18.8）
 *
 * 设计原则：
 *   - 顶部常驻紧凑型 badge，不打断用户
 *   - hover/click 弹出 dropdown 切换 workspace
 *   - tier 用颜色区分（T0 灰 / T1 蓝 / T2 黄 / T3 红）
 *   - 通用对话模式显示 "💬 通用对话 · T0"
 */
import { useState, useEffect, useCallback } from 'react';
import {
  apiClient,
  MODE_LABELS,
  PERMISSIONS_LABELS,
  TIER_LABELS,
  workspacePermissionsToTier,
} from '../lib/api';
import type {
  AuthorizedWorkspace,
  WorkspaceRuntimeBrief,
  AgentTier,
} from '../lib/api';

interface WorkspaceStatusBadgeProps {
  /** 当前 session 的 session_id（null 表示尚未启动 session，badge 显示默认状态） */
  sessionId: string | null;
  /** 当前 session 默认 agent tier（来自 agent yaml） */
  defaultAgentTier: AgentTier;
  /** 切换 workspace 后的回调（父组件据此 update session） */
  onSwitchWorkspace?: (selection: { workspaceId: string | null; mode: 'project' | 'general' }) => void;
}

function tierColor(tier: AgentTier): string {
  switch (tier) {
    case 'T0': return '#6b7280';  // 灰
    case 'T1': return '#3b82f6';  // 蓝
    case 'T2': return '#fbbf24';  // 黄
    case 'T3': return '#ef4444';  // 红
    default: return '#94a3b8';
  }
}

export function WorkspaceStatusBadge({
  sessionId,
  defaultAgentTier,
  onSwitchWorkspace,
}: WorkspaceStatusBadgeProps) {
  const [open, setOpen] = useState(false);
  const [workspaces, setWorkspaces] = useState<WorkspaceRuntimeBrief[]>([]);
  const [currentWs, setCurrentWs] = useState<AuthorizedWorkspace | null>(null);
  const [loading, setLoading] = useState(false);

  // 加载当前 session workspace
  const loadCurrent = useCallback(async () => {
    if (!sessionId) {
      setCurrentWs(null);
      return;
    }
    try {
      const resp = await apiClient.getCurrentWorkspace(sessionId);
      setCurrentWs(resp.workspace);
    } catch {
      setCurrentWs(null);
    }
  }, [sessionId]);

  // 加载可选 workspace 列表
  const loadWorkspaces = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await apiClient.listRuntimeWorkspaces();
      setWorkspaces(resp.workspaces || []);
    } catch {
      setWorkspaces([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadCurrent(); }, [loadCurrent]);

  // 切换 dropdown 时按需加载
  useEffect(() => {
    if (open && workspaces.length === 0 && !loading) {
      loadWorkspaces();
    }
  }, [open, workspaces.length, loading, loadWorkspaces]);

  const handleSwitch = (selection: { workspaceId: string | null; mode: 'project' | 'general' }) => {
    setOpen(false);
    if (onSwitchWorkspace) {
      onSwitchWorkspace(selection);
    }
  };

  // 计算 badge 显示的 tier：有当前 workspace → workspace tier，否则 agent 默认 tier
  const badgeTier: AgentTier = currentWs
    ? workspacePermissionsToTier(currentWs.permissions)
    : defaultAgentTier;
  const color = tierColor(badgeTier);

  return (
    <div className="ws-status-badge-wrap">
      <button
        className="ws-status-badge"
        onClick={() => setOpen((v) => !v)}
        style={{ borderColor: color, color: color }}
        title={currentWs ? `${currentWs.display_name} · ${PERMISSIONS_LABELS[currentWs.permissions]}` : '通用对话'}
      >
        <span className="ws-status-icon">
          {currentWs ? '📁' : '💬'}
        </span>
        <span className="ws-status-name">
          {currentWs ? currentWs.display_name : '通用对话'}
        </span>
        <span className="ws-status-tier" style={{ backgroundColor: color }}>
          {badgeTier}
        </span>
      </button>

      {open && (
        <div className="ws-status-dropdown">
          <div className="ws-status-dropdown-header">
            <span>切换工作区</span>
            <button className="ws-status-close" onClick={() => setOpen(false)}>×</button>
          </div>
          <div className="ws-status-dropdown-body">
            <button
              className={`ws-status-item ${!currentWs ? 'active' : ''}`}
              onClick={() => handleSwitch({ workspaceId: null, mode: 'general' })}
            >
              <span className="ws-status-item-icon">💬</span>
              <span className="ws-status-item-body">
                <span className="ws-status-item-name">通用对话</span>
                <span className="ws-status-item-meta">{TIER_LABELS[defaultAgentTier]}</span>
              </span>
            </button>
            {loading ? (
              <div className="ws-status-empty">加载中…</div>
            ) : workspaces.length === 0 ? (
              <div className="ws-status-empty">暂无授权工作区</div>
            ) : (
              workspaces.map((w) => {
                const wt = workspacePermissionsToTier(w.permissions);
                return (
                  <button
                    key={w.workspace_id}
                    className={`ws-status-item ${currentWs?.workspace_id === w.workspace_id ? 'active' : ''}`}
                    onClick={() => handleSwitch({ workspaceId: w.workspace_id, mode: 'project' })}
                  >
                    <span className="ws-status-item-icon">
                      {w.mode === 'bind_mount' ? '🔗' : w.mode === 'local_copy' ? '📋' : w.mode === 'git_clone' ? '🌿' : '📦'}
                    </span>
                    <span className="ws-status-item-body">
                      <span className="ws-status-item-name">{w.display_name}</span>
                      <span className="ws-status-item-meta">
                        {MODE_LABELS[w.mode]} · {PERMISSIONS_LABELS[w.permissions]} · {TIER_LABELS[wt]}
                      </span>
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}