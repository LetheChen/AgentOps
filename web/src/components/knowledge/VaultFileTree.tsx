import { useMemo } from 'react';

// ── 类型定义 ──────────────────────────────────────────────────────

/** Vault 文件树节点（递归结构） */
export interface VaultFileTreeNode {
  name: string;
  type: 'file' | 'dir';
  path: string;
  size?: number;
  mtime?: string;
  ext?: string;
  children?: VaultFileTreeNode[];
  loaded?: boolean;
  loading?: boolean;
}

interface VaultFileTreeProps {
  nodes: VaultFileTreeNode[];
  selectedPath?: string;
  /** 受控的展开路径集合，支持父组件程序化展开 */
  expandedPaths: Set<string>;
  /** 用户点击目录节点时触发（展开/折叠均触发，由父组件决定行为） */
  onExpand: (node: VaultFileTreeNode) => void;
  /** 用户点击文件节点时触发 */
  onSelect: (node: VaultFileTreeNode) => void;
  /** 递归深度，用于缩进（内部使用） */
  depth?: number;
}

// ── 辅助函数 ──────────────────────────────────────────────────────

/** 排序：目录在前，文件在后，各自按名称字母序 */
function sortNodes(nodes: VaultFileTreeNode[]): VaultFileTreeNode[] {
  return [...nodes].sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

/** 根据扩展名返回文件类型分类 */
function fileKind(ext?: string): 'md' | 'image' | 'file' {
  if (!ext) return 'file';
  const e = ext.toLowerCase();
  if (e === 'md' || e === 'markdown') return 'md';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(e)) return 'image';
  return 'file';
}

// ── 图标组件 ──────────────────────────────────────────────────────

function ChevronIcon({ expanded, loading }: { expanded: boolean; loading: boolean }) {
  if (loading) {
    return <span className="kh-tree-spinner" aria-label="加载中" />;
  }
  return (
    <svg
      className="kh-tree-chevron"
      viewBox="0 0 16 16"
      width="12"
      height="12"
      style={{ transform: expanded ? 'rotate(90deg)' : 'none' }}
    >
      <path
        d="M6 4l4 4-4 4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" className="kh-tree-icon kh-tree-icon-folder">
      {open ? (
        <path
          d="M1.5 4h4l1.2 1.2h7.3a.5.5 0 0 1 .5.5V6H2l-.5-2z"
          fill="currentColor"
          opacity="0.55"
        />
      ) : null}
      <path
        d="M1.5 4.2a.5.5 0 0 1 .5-.5h3.8l1.2 1.2h7a.5.5 0 0 1 .5.5v7.6a.5.5 0 0 1-.5.5H2a.5.5 0 0 1-.5-.5V4.2z"
        fill={open ? 'currentColor' : 'none'}
        fillOpacity={open ? '0.18' : '0'}
        stroke="currentColor"
        strokeWidth="1"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FileKindIcon({ kind }: { kind: 'md' | 'image' | 'file' }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" className={`kh-tree-icon kh-tree-icon-${kind}`}>
      <path
        d="M3 1.6a.5.5 0 0 1 .5-.5H9l4 4v9.4a.5.5 0 0 1-.5.5H3.5a.5.5 0 0 1-.5-.5V1.6z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1"
        strokeLinejoin="round"
      />
      <path d="M9 1.1v4h4" fill="none" stroke="currentColor" strokeWidth="1" strokeLinejoin="round" />
    </svg>
  );
}

// ── 主组件 ────────────────────────────────────────────────────────

export function VaultFileTree({
  nodes,
  selectedPath,
  expandedPaths,
  onExpand,
  onSelect,
  depth = 0,
}: VaultFileTreeProps) {
  const sorted = useMemo(() => sortNodes(nodes), [nodes]);
  if (sorted.length === 0) return null;

  return (
    <ul className="kh-tree-list" role="tree">
      {sorted.map((node) => {
        const isSelected = selectedPath === node.path;
        const isExpanded = expandedPaths.has(node.path);
        const indentStyle = { paddingLeft: `${depth * 14 + 8}px` };

        if (node.type === 'dir') {
          return (
            <li key={node.path} className="kh-tree-node" role="treeitem" aria-expanded={isExpanded}>
              <div
                className={`kh-tree-node-row${isSelected ? ' kh-tree-node-selected' : ''}`}
                style={indentStyle}
                onClick={() => onExpand(node)}
              >
                <ChevronIcon expanded={isExpanded} loading={!!node.loading} />
                <FolderIcon open={isExpanded} />
                <span className="kh-tree-node-name" title={node.path}>
                  {node.name}
                </span>
              </div>
              {isExpanded && node.children && node.children.length > 0 && (
                <VaultFileTree
                  nodes={node.children}
                  selectedPath={selectedPath}
                  expandedPaths={expandedPaths}
                  onExpand={onExpand}
                  onSelect={onSelect}
                  depth={depth + 1}
                />
              )}
              {isExpanded && node.children && node.children.length === 0 && !node.loading && (
                <div className="kh-tree-empty" style={{ paddingLeft: `${(depth + 1) * 14 + 24}px` }}>
                  空目录
                </div>
              )}
            </li>
          );
        }

        return (
          <li key={node.path} className="kh-tree-node" role="treeitem">
            <div
              className={`kh-tree-node-row${isSelected ? ' kh-tree-node-selected' : ''}`}
              style={indentStyle}
              onClick={() => onSelect(node)}
            >
              <span className="kh-tree-chevron-spacer" />
              <FileKindIcon kind={fileKind(node.ext)} />
              <span className="kh-tree-node-name" title={node.path}>
                {node.name}
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
