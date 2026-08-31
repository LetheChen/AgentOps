import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '../../lib/api';
import type { VaultEntry } from '../../lib/api';
import type { PageId } from '../../App';

interface AgentArchiveProps {
  params: Record<string, string>;
  onNavigate: (page: PageId, params?: Record<string, string>) => void;
}

/** 归档根目录（OpenClaw 生成的策展产物，5 个一级目录：Articles/Reports/Notes/Weekly/Images） */
const ROOT = '';

/** 文件预览内容 */
interface Preview {
  path: string;
  content: string;
  format: string;
}

/** 递归文件树子组件 */
interface ArchiveTreeProps {
  dirPath: string;
  depth: number;
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
  defaultExpanded?: boolean;
}

function ArchiveTree({ dirPath, depth, selectedPath, onSelectFile, defaultExpanded = false }: ArchiveTreeProps) {
  const [entries, setEntries] = useState<VaultEntry[]>([]);
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRoot = dirPath === ROOT;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiClient.listVaultFiles(dirPath);
      // 目录在前，再按名称排序
      const sorted = [...res.entries].sort((a, b) => {
        if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      setEntries(sorted);
      setLoaded(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [dirPath]);

  // 根节点默认展开自动加载
  useEffect(() => {
    if (defaultExpanded && !loaded) {
      void load();
    }
  }, [defaultExpanded, loaded, load]);

  const handleDirClick = useCallback(() => {
    if (!loaded) void load();
    setExpanded((v) => !v);
  }, [loaded, load]);

  const indent = depth * 14 + 8;

  return (
    <div className="kh-archive-tree">
      {/* 根节点不渲染自身目录行，只渲染子项 */}
      {!isRoot && (
        <div
          className={`kh-archive-tree-row kh-archive-tree-dir${expanded ? ' expanded' : ''}`}
          style={{ paddingLeft: indent }}
          onClick={handleDirClick}
        >
          <span className="kh-archive-arrow">{expanded ? '▾' : '▸'}</span>
          <span className="kh-archive-tree-name">{dirPath.split('/').pop() ?? dirPath}</span>
        </div>
      )}
      {expanded && loading && (
        <div className="kh-archive-tree-loading" style={{ paddingLeft: indent + 14 }}>
          加载中…
        </div>
      )}
      {expanded && error && (
        <div className="kh-error" style={{ marginLeft: indent + 14 }}>
          {error}
        </div>
      )}
      {expanded &&
        loaded &&
        entries.map((entry) => {
          if (entry.type === 'dir') {
            return (
              <ArchiveTree
                key={entry.path}
                dirPath={entry.path}
                depth={depth + 1}
                selectedPath={selectedPath}
                onSelectFile={onSelectFile}
              />
            );
          }
          return (
            <div
              key={entry.path}
              className={`kh-archive-tree-row kh-archive-tree-file${selectedPath === entry.path ? ' selected' : ''}`}
              style={{ paddingLeft: (depth + 1) * 14 + 8 }}
              onClick={() => onSelectFile(entry.path)}
            >
              <span className="kh-archive-tree-name">{entry.name}</span>
            </div>
          );
        })}
    </div>
  );
}

export function AgentArchive({ params, onNavigate }: AgentArchiveProps) {
  const [selectedPath, setSelectedPath] = useState<string | null>(params.path || null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [curating, setCurating] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [curateRunId, setCurateRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // toast 自动消失
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  // 选中文件并预览
  const handleSelectFile = useCallback(async (path: string) => {
    setSelectedPath(path);
    setLoadingPreview(true);
    setError(null);
    try {
      const res = await apiClient.readVaultFile(path);
      setPreview({ path: res.path, content: res.content, format: res.format });
    } catch (e) {
      setError(`读取文件失败: ${e instanceof Error ? e.message : String(e)}`);
      setPreview(null);
    } finally {
      setLoadingPreview(false);
    }
  }, []);

  // 初始 params.path 存在时自动加载预览
  useEffect(() => {
    if (params.path) void handleSelectFile(params.path);
  }, [params.path, handleSelectFile]);

  // 触发内容策展
  const handleCurate = useCallback(async () => {
    setCurating(true);
    setError(null);
    try {
      const res = await apiClient.curateContent({});
      setCurateRunId(res.run_id);
      setToast(`策展已启动（run_id: ${res.run_id}）`);
    } catch (e) {
      setToast(`策展启动失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCurating(false);
    }
  }, []);

  return (
    <div className="kh-archive">
      <div className="kh-archive-toolbar">
        <div className="kh-archive-title">Agent 内容归档</div>
        <div className="kh-archive-actions">
          <button type="button" className="btn-primary" onClick={handleCurate} disabled={curating}>
            {curating ? '启动中…' : '触发内容策展'}
          </button>
          {curateRunId && (
            <button type="button" className="btn-secondary" onClick={() => onNavigate('chat')}>
              查看对话
            </button>
          )}
        </div>
      </div>

      <div className="kh-archive-body">
        <aside className="kh-archive-sidebar">
          <div className="kh-archive-sidebar-header">{ROOT}/</div>
          <ArchiveTree
            dirPath={ROOT}
            depth={0}
            selectedPath={selectedPath}
            onSelectFile={handleSelectFile}
            defaultExpanded
          />
        </aside>

        <section className="kh-archive-preview">
          {error && <div className="kh-error">{error}</div>}
          {!selectedPath && !error && <div className="kh-empty">从左侧选择归档文件预览</div>}
          {selectedPath && loadingPreview && <div className="kh-empty">加载中…</div>}
          {preview && !loadingPreview && (
            <>
              <div className="kh-archive-preview-header">
                <span className="font-mono">{preview.path}</span>
                <span className="kh-archive-preview-format">{preview.format}</span>
              </div>
              <pre className="kh-archive-preview-content">{preview.content}</pre>
            </>
          )}
        </section>
      </div>

      {toast && <div className="kh-toast">{toast}</div>}
    </div>
  );
}
