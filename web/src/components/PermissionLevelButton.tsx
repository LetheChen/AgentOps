import { useState, useRef, useEffect, useCallback } from 'react';
import { apiClient } from '../lib/api';
import type { PermissionLevel } from '../lib/api';
import { PERMISSION_LEVEL_LABELS, permissionLevelToTier } from '../lib/api';

interface PermissionLevelButtonProps {
  sessionId: string | null;
  /** 当前权限级别（由父组件从 getCurrentWorkspace 同步） */
  currentLevel: string | null;
  /** 权限变更回调（父组件可用来更新本地状态） */
  onLevelChanged?: (level: PermissionLevel) => void;
}

const ALL_LEVELS: PermissionLevel[] = ['read_only', 'read_write', 'read_write_exec', 'full_access'];

/** 按钮线性 SVG 图标（替代 emoji；级别差异由 tier 配色体现，不另画图标）。 */
function ShieldIcon({ size = 13 }: { size?: number }) {
  return (
    <svg
      width={size} height={size} viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}
    >
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}

const LEVEL_DESC: Record<PermissionLevel, string> = {
  read_only: '只读：agent 只能读取工作区文件，不能修改/执行',
  read_write: '读写：agent 可读写工作区文件，不能执行命令',
  read_write_exec: '读写+执行：agent 可读写文件并执行命令（受限 tier 校验）',
  full_access: '完全访问：绕过所有 tier 校验与路径限制，慎用',
};

export function PermissionLevelButton({ sessionId, currentLevel, onLevelChanged }: PermissionLevelButtonProps) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [pendingFullAccess, setPendingFullAccess] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  const effective: PermissionLevel = (currentLevel as PermissionLevel) || 'read_write_exec';
  const tier = permissionLevelToTier(effective);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setPendingFullAccess(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  const handleSelect = useCallback(async (level: PermissionLevel) => {
    if (level === effective) {
      setOpen(false);
      return;
    }
    if (level === 'full_access' && !pendingFullAccess) {
      setPendingFullAccess(true);
      return;
    }
    if (!sessionId) {
      setError('无活跃会话，无法切换权限');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await apiClient.v2UpdateSessionPermission(sessionId, level);
      onLevelChanged?.(level);
      setOpen(false);
      setPendingFullAccess(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : '切换权限失败');
    } finally {
      setLoading(false);
    }
  }, [sessionId, effective, pendingFullAccess, onLevelChanged]);

  return (
    <div className="perm-level-btn-wrap" ref={ref}>
      <button
        type="button"
        className={`perm-level-btn tier-${tier.toLowerCase()}`}
        onClick={() => setOpen((v) => !v)}
        disabled={loading}
        title={`当前权限：${PERMISSION_LEVEL_LABELS[effective]}（${tier}）— 点击切换`}
      >
        <span className="perm-level-icon"><ShieldIcon /></span>
        <span className="perm-level-text">{PERMISSION_LEVEL_LABELS[effective]}</span>
        <span className="perm-level-caret">▾</span>
      </button>

      {open && (
        <div className="perm-level-dropdown">
          <div className="perm-level-dropdown-header">
            会话权限级别
            <span className="perm-level-dropdown-hint">切换后立即生效</span>
          </div>
          {ALL_LEVELS.map((lvl) => {
            const isCurrent = lvl === effective;
            const isPending = pendingFullAccess && lvl === 'full_access';
            return (
              <div
                key={lvl}
                data-permission={lvl}
                className={`perm-level-option ${isCurrent ? 'current' : ''} ${isPending ? 'pending' : ''}`}
                onClick={() => !loading && handleSelect(lvl)}
              >
                <div className="perm-level-option-body">
                  <span className="perm-level-option-name">
                    {PERMISSION_LEVEL_LABELS[lvl]}
                    <span className="perm-level-option-tier">{permissionLevelToTier(lvl)}</span>
                  </span>
                  <span className="perm-level-option-desc">{LEVEL_DESC[lvl]}</span>
                  {isPending && (
                    <span className="perm-level-option-warn">
                      Full Access 将绕过所有权限校验，确认请再次点击
                    </span>
                  )}
                </div>
                {isCurrent && <span className="perm-level-option-check">✓</span>}
              </div>
            );
          })}
          {error && <div className="perm-level-dropdown-error">{error}</div>}
        </div>
      )}
    </div>
  );
}
