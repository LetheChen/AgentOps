/**
 * WorkspaceForm — 工作区创建/编辑表单（从 WorkspacesPage 抽取，供 Onboarding 复用）
 *
 * 字段：display_name / description / mode / permissions / source_path / git_url / git_branch
 */
import { useState } from 'react';
import type {
  WorkspaceMode,
  WorkspacePermissions,
} from '../lib/api';
import {
  MODE_LABELS,
  PERMISSIONS_LABELS,
  TIER_LABELS,
  workspacePermissionsToTier,
} from '../lib/api';

export interface WorkspaceFormState {
  display_name: string;
  description: string;
  mode: WorkspaceMode;
  permissions: WorkspacePermissions;
  source_path: string;
  git_url: string;
  git_branch: string;
}

export const EMPTY_FORM: WorkspaceFormState = {
  display_name: '',
  description: '',
  mode: 'bind_mount',
  permissions: 'read_write',
  source_path: '',
  git_url: '',
  git_branch: 'main',
};

export function WorkspaceForm({
  initial,
  onSubmit,
  onCancel,
  submitting,
}: {
  initial: WorkspaceFormState;
  onSubmit: (state: WorkspaceFormState) => void;
  onCancel: () => void;
  submitting: boolean;
}) {
  const [form, setForm] = useState<WorkspaceFormState>(initial);

  const update = <K extends keyof WorkspaceFormState>(k: K, v: WorkspaceFormState[K]) => {
    setForm((prev) => ({ ...prev, [k]: v }));
  };

  const canSubmit = (() => {
    if (!form.display_name.trim()) return false;
    if (form.mode === 'local_copy' || form.mode === 'bind_mount') {
      return !!form.source_path.trim();
    }
    if (form.mode === 'git_clone') {
      return !!form.git_url.trim();
    }
    return true;
  })();

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit || submitting) return;
    onSubmit(form);
  };

  return (
    <form className="ws-form" onSubmit={handleSubmit}>
      <div className="ws-form-row">
        <label className="ws-form-label">显示名称 *</label>
        <input
          className="ws-form-input"
          value={form.display_name}
          onChange={(e) => update('display_name', e.target.value)}
          placeholder="如：agentops-frontend"
          autoFocus
        />
      </div>

      <div className="ws-form-row">
        <label className="ws-form-label">描述</label>
        <input
          className="ws-form-input"
          value={form.description}
          onChange={(e) => update('description', e.target.value)}
          placeholder="可选：项目说明"
        />
      </div>

      <div className="ws-form-row">
        <label className="ws-form-label">挂载模式 *</label>
        <select
          className="ws-form-select"
          value={form.mode}
          onChange={(e) => update('mode', e.target.value as WorkspaceMode)}
        >
          {(Object.keys(MODE_LABELS) as WorkspaceMode[]).map((m) => (
            <option key={m} value={m}>{MODE_LABELS[m]} ({m})</option>
          ))}
        </select>
      </div>

      {(form.mode === 'local_copy' || form.mode === 'bind_mount') && (
        <div className="ws-form-row">
          <label className="ws-form-label">主机路径 * <span className="ws-form-hint">（绝对路径，如 E:/Project/agentops）</span></label>
          <input
            className="ws-form-input"
            value={form.source_path}
            onChange={(e) => update('source_path', e.target.value)}
            placeholder="E:/Project/your-project"
          />
        </div>
      )}

      {form.mode === 'git_clone' && (
        <>
          <div className="ws-form-row">
            <label className="ws-form-label">Git URL * <span className="ws-form-hint">（https://...）</span></label>
            <input
              className="ws-form-input"
              value={form.git_url}
              onChange={(e) => update('git_url', e.target.value)}
              placeholder="https://github.com/user/repo.git"
            />
          </div>
          <div className="ws-form-row">
            <label className="ws-form-label">分支</label>
            <input
              className="ws-form-input"
              value={form.git_branch}
              onChange={(e) => update('git_branch', e.target.value)}
              placeholder="main"
            />
          </div>
        </>
      )}

      <div className="ws-form-row">
        <label className="ws-form-label">权限 *</label>
        <select
          className="ws-form-select"
          value={form.permissions}
          onChange={(e) => update('permissions', e.target.value as WorkspacePermissions)}
        >
          {(Object.keys(PERMISSIONS_LABELS) as WorkspacePermissions[]).map((p) => {
            const tier = workspacePermissionsToTier(p);
            return (
              <option key={p} value={p}>
                {PERMISSIONS_LABELS[p]} ({p}) — {TIER_LABELS[tier]}
              </option>
            );
          })}
        </select>
      </div>

      <div className="ws-form-actions">
        <button type="button" className="btn-secondary" onClick={onCancel} disabled={submitting}>
          取消
        </button>
        <button type="submit" className="btn-primary" disabled={!canSubmit || submitting}>
          {submitting ? '保存中…' : '保存'}
        </button>
      </div>
    </form>
  );
}
