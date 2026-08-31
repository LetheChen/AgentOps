/**
 * WorkspacePicker — 工作区选择按钮（精简版）
 *
 * 核心交互（v2）：
 *   - 按钮显示当前工作区名（无时显示「选择工作区」）
 *   - **点击主按钮直接调用 host 端原生文件夹选择对话框**（PowerShell+IFileOpenDialog /
 *     osascript / zenity），返回绝对路径后立即注册为 bind_mount workspace 并切换
 *   - 选完后按钮立即显示新文件夹名（无需刷新页面）
 *   - 已授权工作区 > 1 时，右下角小箭头按钮打开下拉列表用于切换
 *
 * 设计依据：参考 E:\GitHub\deepseek-harness\packages\host\directory-picker-native
 * 浏览器无法直接获取本地绝对路径，必须由 host 端弹出 OS-native 文件浏览器返回路径。
 *
 * 与 v1 的区别（响应用户反馈"操作太费劲"）：
 *   - v1：主按钮 → 下拉 → 「选择本地文件夹」→ 弹窗/降级浏览 → 选目录 → 确认
 *         （5 步，且 fallback 时用 in-app DirBrowser，路径导航繁琐）
 *   - v2：主按钮 → 弹原生文件浏览器 → 选目录 → 完成
 *         （2 步，直接走系统资源管理器，符合 Windows / macOS 用户习惯）
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { apiClient, MODE_LABELS } from '../lib/api';
import type {
  AuthorizedWorkspace,
  WorkspaceRuntimeBrief,
  WorkspaceMode,
  WorkspacePermissions,
} from '../lib/api';

interface WorkspacePickerProps {
  sessionId: string | null;
  /**
   * 保留以兼容父组件调用（v2 不再依赖此字段，agent tier 由父组件独立管理）
   */
  defaultAgentTier?: string;
  onSwitchWorkspace?: (selection: { workspaceId: string | null; mode: 'project' | 'general' }) => void;
}

export function WorkspacePicker({ sessionId, onSwitchWorkspace }: WorkspacePickerProps) {
  const [open, setOpen] = useState(false);
  const [workspaces, setWorkspaces] = useState<WorkspaceRuntimeBrief[]>([]);
  const [currentWs, setCurrentWs] = useState<AuthorizedWorkspace | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  // 原生对话框打开期间（HTTP 请求挂着直到用户关闭对话框），按钮显示"选择中"
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 平台能力探测：null=未探测/探测中，true=支持原生对话框，false=降级
  const [nativeSupported, setNativeSupported] = useState<boolean | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  // 本地选中覆盖：选完目录后 session 绑定要等下次发消息才生效（父组件
  // 把 workspace_id 存进 pending，startSession 时才带上），期间服务端
  // getCurrentWorkspace 返回的还是旧绑定 — 会把按钮覆盖回旧名字。
  // 用 locallySelectedWs 在"选中 → 新 session 建立"窗口期直接显示新选目录。
  const [locallySelectedWs, setLocallySelectedWs] = useState<{
    workspace_id: string;
    display_name: string;
    source_path?: string;
    mode?: string;
  } | null>(null);

  // session 建立/切换（sessionId 变为非空）→ 服务端绑定已生效，清除本地覆盖
  useEffect(() => {
    if (sessionId) setLocallySelectedWs(null);
  }, [sessionId]);

  // 显示优先级：本地刚选的 > 服务端 session 绑定
  const displayWs = locallySelectedWs ?? currentWs;

  // 加载当前 session workspace；无 session 时后端回退到 manager 默认工作区
  const loadCurrent = useCallback(async () => {
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

  // 探测 host 平台是否支持原生文件夹选择对话框（首次 mount 一次即可）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await apiClient.isNativePickerSupported();
        if (!cancelled) setNativeSupported(resp.supported);
      } catch {
        if (!cancelled) setNativeSupported(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => { loadCurrent(); }, [loadCurrent]);

  useEffect(() => {
    if (open && workspaces.length === 0 && !loading) {
      loadWorkspaces();
    }
  }, [open, workspaces.length, loading, loadWorkspaces]);

  // 点击外部关闭下拉
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const handleSwitch = (selection: { workspaceId: string | null; mode: 'project' | 'general' }) => {
    setOpen(false);
    onSwitchWorkspace?.(selection);
  };

  // 从绝对路径提取末级目录名（同时处理 Windows 反斜杠和 Unix 正斜杠）
  const pathBasename = (absPath: string): string => {
    if (!absPath) return '选择的文件夹';
    const parts = absPath.replace(/\\/g, '/').split('/').filter(Boolean);
    return parts[parts.length - 1] || absPath || '选择的文件夹';
  };

  // 把绝对路径注册为 bind_mount workspace 并切换
  const registerAndSwitch = useCallback(async (absPath: string) => {
    setCreating(true);
    setError(null);
    try {
      const dirName = pathBasename(absPath);
      const resp = await apiClient.createWorkspace({
        display_name: dirName,
        mode: 'bind_mount' as WorkspaceMode,
        permissions: 'read_write_exec' as WorkspacePermissions,
        source_path: absPath,
        description: '通过原生文件夹选择器添加的工作区',
      });
      // 立即显示新选目录（session 绑定要等下次发消息，loadCurrent 会拿到
      // 旧绑定，不能用它刷新按钮 — 用本地覆盖）
      setLocallySelectedWs({
        workspace_id: resp.workspace.workspace_id,
        display_name: resp.workspace.display_name || dirName,
        source_path: resp.workspace.source_path || absPath,
        mode: 'bind_mount',
      });
      handleSwitch({ workspaceId: resp.workspace.workspace_id, mode: 'project' });
      await loadWorkspaces();
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建工作区失败');
    } finally {
      setCreating(false);
    }
  }, [loadWorkspaces]);

  // **核心交互**：主按钮点击 → 直接调原生文件夹选择对话框
  const handleMainClick = useCallback(async () => {
    setError(null);
    if (nativeSupported === false) {
      // 平台不支持原生对话框：打开下拉让用户从已有 workspace 选
      // （避免弹出 in-app DirBrowser，因为用户反馈体验差）
      setOpen(true);
      return;
    }
    setPicking(true);
    try {
      const initial = displayWs?.source_path || undefined;
      const result = await apiClient.pickFolder(initial);
      if (result.cancelled) return;       // 取消：静默
      if (result.error) {
        setError(result.error);
        return;
      }
      if (result.path) {
        await registerAndSwitch(result.path);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '调用原生文件夹选择器失败');
    } finally {
      setPicking(false);
    }
  }, [nativeSupported, displayWs, registerAndSwitch]);

  // 按钮显示文本（本地刚选的优先，其次 session 绑定）
  const displayName = displayWs
    ? pathBasename(displayWs.source_path || displayWs.display_name)
    : '选择工作区';
  const fullPath = displayWs?.source_path || '';
  const hasMultipleWorkspaces = workspaces.length > 1;

  return (
    <div className="ws-picker-v2" ref={wrapRef}>
      <div className={`ws-picker-v2-main ${creating || picking ? 'creating' : ''} ${error ? 'error' : ''}`}>
        <button
          className="ws-picker-v2-btn"
          onClick={handleMainClick}
          disabled={creating || picking || nativeSupported === null}
          title={fullPath || '点击选择工作区目录（弹出系统原生文件夹选择器）'}
        >
          {/* 左：文件夹图标（与 mode 关联，线性 SVG） */}
          <span className="ws-picker-v2-icon">
            <ModeIcon mode={displayWs?.mode} />
          </span>

          {/* 中：工作区名（点击即弹原生对话框） */}
          <span className="ws-picker-v2-name" title={fullPath}>
            {picking ? '选择中…' : creating ? '正在添加…' : displayName}
          </span>

          {/* 右：原生对话框指示箭头（提示用户点击会弹原生选择器） */}
          <svg
            className="ws-picker-v2-arrow"
            width="12" height="12" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" strokeWidth="2"
            strokeLinecap="round" strokeLinejoin="round"
          >
            <path d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        </button>

        {/* 已有 workspace > 1 个时显示小箭头按钮用于切换（避免主按钮承担两种语义） */}
        {hasMultipleWorkspaces && (
          <button
            className="ws-picker-v2-toggle"
            onClick={() => setOpen(v => !v)}
            title="切换其他已授权工作区"
            aria-label="切换工作区"
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
        )}
      </div>

      {/* 错误提示（紧凑气泡样式） */}
      {error && (
        <div className="ws-picker-v2-err" role="alert">
          <span>{error}</span>
          <button onClick={() => setError(null)} aria-label="关闭">×</button>
        </div>
      )}

      {/* 下拉：仅在 hasMultipleWorkspaces 时使用，用于切换已有 workspace */}
      {open && hasMultipleWorkspaces && (
        <div className="ws-picker-v2-dropdown">
          {loading ? (
            <div className="ws-picker-v2-empty">加载中…</div>
          ) : (
            <>
              <div className="ws-picker-v2-list-hint">切换到已授权工作区：</div>
              {workspaces.map(w => {
                const isActive = displayWs?.workspace_id === w.workspace_id;
                const itemPath = (w as WorkspaceRuntimeBrief & { source_path?: string }).source_path;
                const itemBasename = pathBasename(itemPath || w.display_name);
                return (
                  <button
                    key={w.workspace_id}
                    className={`ws-picker-v2-item ${isActive ? 'active' : ''}`}
                    onClick={() => {
                      // 下拉切换也立即显示（session 绑定等下次发消息生效）
                      setLocallySelectedWs({
                        workspace_id: w.workspace_id,
                        display_name: w.display_name,
                        source_path: itemPath,
                        mode: w.mode,
                      });
                      handleSwitch({ workspaceId: w.workspace_id, mode: 'project' });
                    }}
                    title={itemPath || w.display_name}
                  >
                    <span className="ws-picker-v2-item-icon">
                      <ModeIcon mode={w.mode} size={13} />
                    </span>
                    <span className="ws-picker-v2-item-body">
                      <span className="ws-picker-v2-item-name">{itemBasename}</span>
                      <span className="ws-picker-v2-item-meta">{MODE_LABELS[w.mode]}</span>
                    </span>
                    {isActive && (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, color: 'var(--accent)' }}>
                        <polyline points="20 6 9 17 4 12" />
                      </svg>
                    )}
                  </button>
                );
              })}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// mode → 线性 SVG 图标（替代 emoji，与全局 stroke 图标风格统一）
function ModeIcon({ mode, size = 14 }: { mode: string | undefined; size?: number }) {
  const common = {
    width: size, height: size, viewBox: '0 0 24 24',
    fill: 'none', stroke: 'currentColor', strokeWidth: 2,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
    style: { flexShrink: 0 as const },
  };
  switch (mode) {
    case 'bind_mount': // 链接（挂载）
      return (
        <svg {...common}>
          <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
          <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
        </svg>
      );
    case 'local_copy': // 剪贴板/复制
      return (
        <svg {...common}>
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      );
    case 'git_clone': // git 分支
      return (
        <svg {...common}>
          <line x1="6" y1="3" x2="6" y2="15" />
          <circle cx="18" cy="6" r="3" />
          <circle cx="6" cy="18" r="3" />
          <path d="M18 9a9 9 0 0 1-9 9" />
        </svg>
      );
    case 'isolated': // 隔离箱
      return (
        <svg {...common}>
          <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
          <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
          <line x1="12" y1="22.08" x2="12" y2="12" />
        </svg>
      );
    default: // 文件夹
      return (
        <svg {...common}>
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        </svg>
      );
  }
}
