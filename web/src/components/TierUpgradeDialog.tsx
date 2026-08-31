/**
 * TierUpgradeDialog — 权限不足时弹窗升级 workspace 授权（P0.18.7d）
 *
 * 场景：用户在 WorkspaceSelectorDialog 选了 workspace，但 workspace.permissions
 *      < 当前 session 默认 agent tier。
 *
 * 升级路径：
 *   read_only (T1) → read_write (T2) → read_write_exec (T3)
 * 用户可一键升级到能跑当前 agent 的最小 tier。
 */
import { useState, useMemo } from 'react';
import {
  PERMISSIONS_LABELS,
  TIER_LABELS,
  workspacePermissionsToTier,
} from '../lib/api';
import type {
  AuthorizedWorkspace,
  WorkspacePermissions,
  AgentTier,
} from '../lib/api';

interface TierUpgradeDialogProps {
  workspace: AuthorizedWorkspace;
  requiredTier: AgentTier;
  onConfirm: (newPermissions: WorkspacePermissions) => void;
  onCancel: () => void;
}

const TIER_RANK: Record<AgentTier, number> = { T0: 0, T1: 1, T2: 2, T3: 3, T4: 4 };

/** 从当前权限向上到 requiredTier 的最小目标权限 */
function pickMinUpgradePerm(
  current: WorkspacePermissions,
  required: AgentTier,
): WorkspacePermissions {
  const requiredRank = TIER_RANK[required];
  if (requiredRank <= TIER_RANK[workspacePermissionsToTier(current)]) {
    return current; // 已满足，无需升级
  }
  // 升到能覆盖 required 的最小权限
  if (requiredRank >= TIER_RANK.T3) return 'read_write_exec';
  if (requiredRank >= TIER_RANK.T2) return 'read_write';
  return 'read_only';
}

export function TierUpgradeDialog({
  workspace,
  requiredTier,
  onConfirm,
  onCancel,
}: TierUpgradeDialogProps) {
  const currentTier = workspacePermissionsToTier(workspace.permissions);
  const recommendedPerm = useMemo(
    () => pickMinUpgradePerm(workspace.permissions, requiredTier),
    [workspace.permissions, requiredTier],
  );
  const [targetPerm, setTargetPerm] = useState<WorkspacePermissions>(recommendedPerm);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const targetTier = workspacePermissionsToTier(targetPerm);

  const handleConfirm = async () => {
    if (targetPerm === workspace.permissions) {
      // 用户选了原权限，相当于取消升级 → 走 cancel
      onCancel();
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      onConfirm(targetPerm);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  };

  return (
    <div className="ws-modal-overlay ws-modal-overlay-nested" onClick={onCancel}>
      <div className="ws-modal ws-modal-upgrade" onClick={(e) => e.stopPropagation()}>
        <div className="ws-modal-header">
          <div>
            <h2>权限升级</h2>
            <p className="ws-modal-subtitle">
              工作区「{workspace.display_name}」权限不足
            </p>
          </div>
          <button className="ws-modal-close" onClick={onCancel}>×</button>
        </div>

        <div className="ws-upgrade-body">
          <div className="ws-upgrade-row">
            <span className="ws-upgrade-label">当前权限</span>
            <span className="ws-upgrade-current">
              {PERMISSIONS_LABELS[workspace.permissions]} · {TIER_LABELS[currentTier]}
            </span>
          </div>
          <div className="ws-upgrade-arrow">→</div>
          <div className="ws-upgrade-row">
            <span className="ws-upgrade-label">所需权限</span>
            <span className="ws-upgrade-required">{TIER_LABELS[requiredTier]}</span>
          </div>

          <div className="ws-upgrade-divider" />

          <div className="ws-upgrade-pick">
            <label className="ws-form-label">升级到</label>
            <div className="ws-upgrade-options">
              {(['read_only', 'read_write', 'read_write_exec'] as WorkspacePermissions[]).map((p) => {
                const tier = workspacePermissionsToTier(p);
                const covers = TIER_RANK[tier] >= TIER_RANK[requiredTier];
                return (
                  <button
                    key={p}
                    type="button"
                    className={`ws-upgrade-opt ${targetPerm === p ? 'active' : ''} ${covers ? '' : 'ws-upgrade-opt-low'}`}
                    onClick={() => setTargetPerm(p)}
                  >
                    <div className="ws-upgrade-opt-name">{PERMISSIONS_LABELS[p]}</div>
                    <div className="ws-upgrade-opt-tier">{TIER_LABELS[tier]}</div>
                    {p === recommendedPerm && (
                      <div className="ws-upgrade-opt-recommend">推荐</div>
                    )}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="ws-upgrade-preview">
            <span>升级后：</span>
            <strong>{TIER_LABELS[targetTier]}</strong>
          </div>

          {error && (
            <div className="ws-error-banner">
              <span>⚠️ {error}</span>
            </div>
          )}
        </div>

        <div className="ws-form-actions">
          <button className="btn-secondary" onClick={onCancel} disabled={submitting}>
            取消
          </button>
          <button
            className="btn-primary"
            onClick={handleConfirm}
            disabled={submitting || targetPerm === workspace.permissions}
          >
            {submitting ? '升级中…' : '一键升级并继续'}
          </button>
        </div>
      </div>
    </div>
  );
}