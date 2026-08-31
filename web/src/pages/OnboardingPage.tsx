/**
 * OnboardingPage — 首次使用引导（全屏向导）
 *
 * 流程：
 *   步骤 1：欢迎 + 说明
 *   步骤 2：选择默认工作区文件夹（DirBrowser 目录浏览器，非 webkitdirectory）
 *           后端自动：末级目录名 → display_name + 系统描述 + bind_mount 模式
 *                    + 建 manager-agent/sessions/workspace 子目录 + AGENTS.md
 *   步骤 3：完成 → 显示创建的结构
 *
 * 用户必须完成才能进入主界面（App.tsx 顶层 early return 拦截）。
 */
import { useState, useCallback } from 'react';
import { apiClient } from '../lib/api';
import type { AuthorizedWorkspace } from '../lib/api';
import { DirBrowser } from '../components/DirBrowser';

type Step = 'welcome' | 'select' | 'done';

const SUBDIR_INFO: { name: string; desc: string; icon: string }[] = [
  { name: 'manager-agent', desc: 'Manager 个人设定（AGENTS.md）', icon: '👤' },
  { name: 'sessions', desc: '会话记录（对话 / 事件流）', icon: '💬' },
  { name: 'workspace', desc: '任务执行工作空间（脚本 / 测试 / 产出物）', icon: '📂' },
];

const dirBasename = (path: string): string => {
  if (!path) return '';
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean);
  return parts[parts.length - 1] || '';
};

export function OnboardingPage({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<Step>('welcome');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdWs, setCreatedWs] = useState<AuthorizedWorkspace | null>(null);
  const [subdirs, setSubdirs] = useState<string[]>([]);
  const [selectedPath, setSelectedPath] = useState<string>('');
  const [selectedName, setSelectedName] = useState<string>('');

  const handleDirSelect = useCallback((path: string, name: string) => {
    setSelectedPath(path);
    setSelectedName(name || dirBasename(path));
  }, []);

  const handleDirConfirm = useCallback((path: string) => {
    setSelectedPath(path);
    setSelectedName(dirBasename(path));
  }, []);

  const handleCreate = useCallback(async () => {
    if (!selectedPath.trim()) {
      setError('请先选择一个文件夹目录');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const resp = await apiClient.createDefaultWorkspace(selectedPath.trim());
      setCreatedWs(resp.workspace);
      setSubdirs(resp.subdirs_created || []);
      setStep('done');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }, [selectedPath]);

  const handleFinish = useCallback(() => {
    onDone();
  }, [onDone]);

  return (
    <div className="ob-overlay">
      <div className="ob-container">
        <div className="ob-header">
          <div className="ob-logo">AgentOps</div>
          <div className="ob-step-indicator">
            <span className={step === 'welcome' ? 'ob-step active' : 'ob-step'}>1</span>
            <span className="ob-step-sep" />
            <span className={step === 'select' ? 'ob-step active' : 'ob-step'}>2</span>
            <span className="ob-step-sep" />
            <span className={step === 'done' ? 'ob-step active' : 'ob-step'}>3</span>
          </div>
        </div>

        {error && (
          <div className="ob-error">
            {error}
            <button className="ob-error-close" onClick={() => setError(null)}>×</button>
          </div>
        )}

        {step === 'welcome' && (
          <div className="ob-body">
            <h1 className="ob-title">欢迎使用 AgentOps</h1>
            <p className="ob-desc">
              AgentOps 是 AI 智能体运维平台。开始对话前，需要先设定 Manager 默认工作区目录。
              <br />
              <strong>这是系统强制设定</strong>，未设定工作区的 Manager 会话将无法启动。
            </p>
            <div className="ob-info-card">
              <div className="ob-info-item">
                <span className="ob-info-icon">📁</span>
                <div>
                  <div className="ob-info-label">默认工作区</div>
                  <div className="ob-info-text">
                    Manager agent 的工作根目录。会话记录、任务产物、个人设定都规整存放在此目录下的子目录中。
                  </div>
                </div>
              </div>
              <div className="ob-info-item">
                <span className="ob-info-icon">📂</span>
                <div>
                  <div className="ob-info-label">自动建立子目录</div>
                  <div className="ob-info-text">
                    选定目录后系统会自动创建 manager-agent / sessions / workspace 三个子目录，
                    脚本、测试、产物不再堆在根目录。
                  </div>
                </div>
              </div>
              <div className="ob-info-item">
                <span className="ob-info-icon">🔒</span>
                <div>
                  <div className="ob-info-label">安全隔离</div>
                  <div className="ob-info-text">
                    agent 只能在授权目录内读写。权限级别（Read Only / Workspace Write / Full Access）
                    由独立按钮控制，不在引导里设置。
                  </div>
                </div>
              </div>
            </div>
            <div className="ob-actions">
              <button className="btn-primary ob-btn-large" onClick={() => setStep('select')}>
                选择工作区目录
              </button>
            </div>
          </div>
        )}

        {step === 'select' && (
          <div className="ob-body">
            <h2 className="ob-title">选择 Manager 默认工作区目录</h2>
            <p className="ob-desc">
              浏览并选中一个文件夹作为 Manager 默认工作区。系统会以末级目录名作为工作区名称，
              并在此目录下自动建立规范子目录结构。
            </p>

            <DirBrowser
              onSelect={handleDirSelect}
              onConfirm={handleDirConfirm}
              confirmLabel="选定此目录"
            />

            {selectedPath && (
              <div className="ob-folder-preview">
                <div className="ob-folder-preview-row">
                  <span className="ob-folder-preview-label">已选目录</span>
                  <span className="ob-folder-preview-value">{selectedPath}</span>
                </div>
                <div className="ob-folder-preview-row">
                  <span className="ob-folder-preview-label">工作区名称（末级目录名）</span>
                  <span className="ob-folder-preview-value">{selectedName}</span>
                </div>
                <div className="ob-folder-preview-row">
                  <span className="ob-folder-preview-label">将创建的子目录</span>
                  <span className="ob-folder-preview-value">
                    {selectedName}/manager-agent/ · {selectedName}/sessions/ · {selectedName}/workspace/
                  </span>
                </div>
                <div className="ob-folder-preview-row">
                  <span className="ob-folder-preview-label">将创建的文件</span>
                  <span className="ob-folder-preview-value">
                    {selectedName}/manager-agent/AGENTS.md
                  </span>
                </div>
                <div className="ob-folder-preview-row">
                  <span className="ob-folder-preview-label">系统描述</span>
                  <span className="ob-folder-preview-value ob-folder-preview-desc">
                    Manager 默认工作区 — 管理会话记录、任务产物与个人设定（系统引导自动创建）
                  </span>
                </div>
              </div>
            )}

            <div className="ob-actions">
              <button className="btn-secondary" onClick={() => setStep('welcome')} disabled={submitting}>
                返回
              </button>
              <button
                className="btn-primary ob-btn-large"
                onClick={handleCreate}
                disabled={submitting || !selectedPath.trim()}
              >
                {submitting ? '创建中…' : '确认并创建工作区'}
              </button>
            </div>
          </div>
        )}

        {step === 'done' && (
          <div className="ob-body">
            <h1 className="ob-title">✅ 设置完成</h1>
            <p className="ob-desc">
              默认工作区已绑定到 <strong>{createdWs?.display_name}</strong>。
              系统已自动创建以下目录结构：
            </p>
            <div className="ob-done-card">
              <div className="ob-done-row">
                <span className="ob-done-label">工作区路径</span>
                <span className="ob-done-value">{createdWs?.source_path}</span>
              </div>
              <div className="ob-done-subdirs">
                {SUBDIR_INFO.map(s => (
                  <div key={s.name} className="ob-done-subdir">
                    <span className="ob-done-subdir-icon">{s.icon}</span>
                    <div>
                      <div className="ob-done-subdir-name">{s.name}/</div>
                      <div className="ob-done-subdir-desc">{s.desc}</div>
                    </div>
                  </div>
                ))}
              </div>
              {subdirs.includes('manager-agent') && (
                <div className="ob-done-row">
                  <span className="ob-done-label">个人设定</span>
                  <span className="ob-done-value ob-done-value-mono">
                    {createdWs?.source_path}/manager-agent/AGENTS.md
                  </span>
                </div>
              )}
            </div>
            <div className="ob-actions">
              <button className="btn-primary ob-btn-large" onClick={handleFinish}>
                开始使用
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
