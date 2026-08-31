import { useState, useEffect, useCallback, useRef } from 'react';
import { apiClient } from '../../lib/api';
import type { VaultEntry } from '../../lib/api';
import type { PageId } from '../../App';
import { VaultFileTree } from './VaultFileTree';
import type { VaultFileTreeNode } from './VaultFileTree';
import { VaultFilePreview } from './VaultFilePreview';

interface VaultBrowserProps {
  params: Record<string, string>;
  onNavigate: (page: PageId, params?: Record<string, string>) => void;
}

/** 文件预览状态 */
interface PreviewState {
  content: string;
  format: string;
  extractedBy?: string;
  frontmatter?: Record<string, unknown>;
  fileName?: string;
  /** 文件过大，不支持预览 */
  tooLarge?: boolean;
}

/** 超过 10MB 不预览 */
const TOO_LARGE_BYTES = 10 * 1024 * 1024;

// ── 辅助函数 ──────────────────────────────────────────────────────

/** 将 API 返回的 VaultEntry 转为树节点 */
function entryToNode(entry: VaultEntry): VaultFileTreeNode {
  return {
    name: entry.name,
    type: entry.type,
    path: entry.path,
    size: entry.size,
    mtime: entry.mtime,
    ext: entry.ext,
    children: entry.type === 'dir' ? [] : undefined,
    loaded: entry.type === 'dir' ? false : undefined,
  };
}

/** 规范化路径：去除首尾斜杠，用于比较 */
function normalizePath(p: string): string {
  return (p || '').replace(/^\/+|\/+$/g, '');
}

/** 在树中递归查找指定路径的节点 */
function findNode(nodes: VaultFileTreeNode[], path: string): VaultFileTreeNode | undefined {
  const target = normalizePath(path);
  for (const n of nodes) {
    if (normalizePath(n.path) === target) return n;
    if (n.children) {
      const found = findNode(n.children, path);
      if (found) return found;
    }
  }
  return undefined;
}

/** 不可变更新：对树中指定路径节点应用 patch */
function patchNode(
  nodes: VaultFileTreeNode[],
  path: string,
  patch: Partial<VaultFileTreeNode>,
): VaultFileTreeNode[] {
  const target = normalizePath(path);
  return nodes.map((n) => {
    if (normalizePath(n.path) === target) {
      return { ...n, ...patch };
    }
    if (n.children) {
      return { ...n, children: patchNode(n.children, path, patch) };
    }
    return n;
  });
}

// ── 主组件 ────────────────────────────────────────────────────────

export function VaultBrowser({ params }: VaultBrowserProps) {
  const [rootNodes, setRootNodes] = useState<VaultFileTreeNode[]>([]);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | undefined>();
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [loadingTree, setLoadingTree] = useState(false);
  const [loadingFile, setLoadingFile] = useState(false);
  const [rootLoaded, setRootLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 记录已处理过的自动展开路径，避免 StrictMode 双调用重复处理 */
  const lastHandledPath = useRef<string | null>(null);

  // ── 加载根目录 ──
  const loadRoot = useCallback(async () => {
    setLoadingTree(true);
    setError(null);
    try {
      const resp = await apiClient.listVaultFiles('');
      setRootNodes(resp.entries.map(entryToNode));
      setRootLoaded(true);
    } catch (e) {
      setError(`加载根目录失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoadingTree(false);
    }
  }, []);

  useEffect(() => {
    loadRoot();
  }, [loadRoot]);

  // ── 懒加载子目录 ──
  const loadChildren = useCallback(async (node: VaultFileTreeNode) => {
    setRootNodes((prev) => patchNode(prev, node.path, { loading: true }));
    try {
      const resp = await apiClient.listVaultFiles(node.path);
      const children = resp.entries.map(entryToNode);
      setRootNodes((prev) => patchNode(prev, node.path, { children, loaded: true, loading: false }));
    } catch (e) {
      setRootNodes((prev) => patchNode(prev, node.path, { loading: false }));
      setError(`加载目录 ${node.name} 失败: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, []);

  // ── 读取文件内容 ──
  const loadFile = useCallback(async (path: string) => {
    setLoadingFile(true);
    setError(null);
    try {
      const resp = await apiClient.readVaultFile(path);
      if (resp.size > TOO_LARGE_BYTES) {
        setPreview({
          content: '',
          format: resp.format,
          extractedBy: resp.extracted_by,
          fileName: resp.path,
          tooLarge: true,
        });
      } else {
        setPreview({
          content: resp.content,
          format: resp.format,
          extractedBy: resp.extracted_by,
          frontmatter: resp.frontmatter,
          fileName: resp.path,
        });
      }
    } catch (e) {
      setError(`读取文件失败: ${e instanceof Error ? e.message : String(e)}`);
      setPreview(null);
    } finally {
      setLoadingFile(false);
    }
  }, []);

  // ── 点击目录：展开/折叠切换 ──
  const handleToggleExpand = useCallback(
    (node: VaultFileTreeNode) => {
      setExpandedPaths((prev) => {
        const next = new Set(prev);
        if (next.has(node.path)) {
          next.delete(node.path);
        } else {
          next.add(node.path);
        }
        return next;
      });
      // 首次展开且未加载子目录时懒加载
      if (!node.loaded && !node.loading && node.type === 'dir' && !expandedPaths.has(node.path)) {
        void loadChildren(node);
      }
    },
    [loadChildren, expandedPaths],
  );

  // ── 点击文件：选中并加载内容 ──
  const handleSelect = useCallback(
    (node: VaultFileTreeNode) => {
      if (node.type !== 'file') return;
      setSelectedPath(node.path);
      void loadFile(node.path);
    },
    [loadFile],
  );

  // ── params.path 存在时自动展开到该文件并选中 ──
  useEffect(() => {
    if (!rootLoaded || !params.path) return;
    if (lastHandledPath.current === params.path) return;
    lastHandledPath.current = params.path;

    const target = normalizePath(params.path);
    if (!target) return;

    setSelectedPath(target);
    const segments = target.split('/').filter(Boolean);

    // 根级文件：直接加载
    if (segments.length <= 1) {
      void loadFile(target);
      return;
    }

    // 逐级展开父目录
    let current = '';
    const expandSequentially = async () => {
      for (let i = 0; i < segments.length - 1; i++) {
        current = current ? `${current}/${segments[i]}` : segments[i];
        const dirPath = current;

        // 检查该目录是否已加载，未加载则触发加载
        let needLoad = false;
        setRootNodes((prev) => {
          const n = findNode(prev, dirPath);
          if (n && n.type === 'dir' && !n.loaded && !n.loading) {
            needLoad = true;
            return patchNode(prev, dirPath, { loading: true });
          }
          return prev;
        });

        if (needLoad) {
          try {
            const resp = await apiClient.listVaultFiles(dirPath);
            const children = resp.entries.map(entryToNode);
            setRootNodes((prev) => patchNode(prev, dirPath, { children, loaded: true, loading: false }));
          } catch (e) {
            setRootNodes((prev) => patchNode(prev, dirPath, { loading: false }));
            setError(`展开目录 ${dirPath} 失败: ${e instanceof Error ? e.message : String(e)}`);
            return;
          }
        }

        setExpandedPaths((prev) => {
          const next = new Set(prev);
          next.add(dirPath);
          return next;
        });
      }
      // 全部展开后加载目标文件
      void loadFile(target);
    };
    void expandSequentially();
  }, [rootLoaded, params.path, loadFile]);

  // ── 刷新 ──
  const handleRefresh = useCallback(() => {
    setExpandedPaths(new Set());
    setSelectedPath(undefined);
    setPreview(null);
    lastHandledPath.current = null;
    void loadRoot();
  }, [loadRoot]);

  return (
    <div className="kh-vault">
      <div className="kh-vault-tree">
        <div className="kh-vault-toolbar">
          <span className="kh-vault-toolbar-title">Vault 文件树</span>
          <button
            type="button"
            className="kh-vault-refresh"
            onClick={handleRefresh}
            disabled={loadingTree}
          >
            <svg viewBox="0 0 16 16" width="13" height="13">
              <path
                d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 3v3h-3"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <span>刷新</span>
          </button>
        </div>
        <div className="kh-vault-tree-body">
          {loadingTree && rootNodes.length === 0 && <div className="kh-loading">加载中...</div>}
          {error && !loadingTree && (
            <div className="kh-empty kh-empty-error" style={{ flexDirection: 'column' }}>
              {error}
            </div>
          )}
          {!loadingTree && !error && rootNodes.length === 0 && (
            <div className="kh-empty">Vault 为空</div>
          )}
          {rootNodes.length > 0 && (
            <VaultFileTree
              nodes={rootNodes}
              selectedPath={selectedPath}
              expandedPaths={expandedPaths}
              onExpand={handleToggleExpand}
              onSelect={handleSelect}
            />
          )}
        </div>
      </div>
      <div className="kh-vault-preview">
        {loadingFile && <div className="kh-loading">加载文件中...</div>}
        {!loadingFile && !preview && <div className="kh-empty">选择左侧文件查看内容</div>}
        {!loadingFile && preview && preview.tooLarge && (
          <div className="kh-empty">文件过大（&gt; 10MB），不支持预览</div>
        )}
        {!loadingFile && preview && !preview.tooLarge && (
          <VaultFilePreview
            content={preview.content}
            format={preview.format}
            extractedBy={preview.extractedBy}
            frontmatter={preview.frontmatter}
            fileName={preview.fileName}
          />
        )}
      </div>
    </div>
  );
}
