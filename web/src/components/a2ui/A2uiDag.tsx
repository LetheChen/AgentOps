/**
 * A2UI DAG 图渲染组件。
 *
 * 职责：把 DAG 节点列表按依赖关系分层布局，渲染 SVG 连线 + 节点卡片。
 * 用途：A2uiNode.tsx 中 component === 'AoDag' 时调用本组件。
 *
 * Vue → React 转换要点：
 * - computed → useMemo（依赖 items 不变时复用布局）
 * - 模板 v-for → array.map
 * - :class / :style → className / style 对象
 */
import { useMemo, type CSSProperties } from 'react';
import { AlertTriangle, CheckCircle2, Clock3, LoaderCircle } from 'lucide-react';

/** DAG 节点数据结构（与 A2uiNode.ts 内 A2uiDagItem 一致）。 */
export interface A2uiDagItem {
  id: string;
  title: string;
  detail?: string;
  status?: string;
  /** 节点 tone，影响颜色（positive/info/warning/critical/neutral）。 */
  tone: string;
  /** 0-100 进度，渲染底部进度条。 */
  progress?: number;
  /** 依赖的其他节点 id 列表。 */
  dependsOn: string[];
}

interface A2uiDagProps {
  items: A2uiDagItem[];
}

// 节点尺寸常量（与原 Vue 组件保持一致）
const NODE_WIDTH = 154;
const NODE_HEIGHT = 64;
const COLUMN_GAP = 52;
const ROW_GAP = 14;

/** 拓扑分层布局算法：按 dependsOn 递归计算每个节点的层级。 */
function computeLayout(items: A2uiDagItem[]) {
  const ids = new Set(items.map(item => item.id));
  const levelById = new Map<string, number>();
  const visiting = new Set<string>();
  const byId = new Map(items.map(item => [item.id, item]));

  const level = (id: string): number => {
    if (levelById.has(id)) return levelById.get(id)!;
    if (visiting.has(id)) return 0; // 环依赖兜底
    visiting.add(id);
    const item = byId.get(id);
    const deps = (item?.dependsOn ?? []).filter(dep => ids.has(dep));
    const result = deps.length ? Math.max(...deps.map(dep => level(dep) + 1)) : 0;
    visiting.delete(id);
    levelById.set(id, result);
    return result;
  };
  items.forEach(item => level(item.id));

  // 按层级分组
  const groups = new Map<number, A2uiDagItem[]>();
  items.forEach(item => {
    const lvl = levelById.get(item.id) ?? 0;
    groups.set(lvl, [...(groups.get(lvl) ?? []), item]);
  });

  const maxRows = Math.max(1, ...[...groups.values()].map(g => g.length));
  const maxLevel = Math.max(0, ...levelById.values());
  const width = Math.max(360, (maxLevel + 1) * NODE_WIDTH + maxLevel * COLUMN_GAP + 28);
  const height = Math.max(190, maxRows * NODE_HEIGHT + Math.max(0, maxRows - 1) * ROW_GAP + 28);

  // 计算每个节点的坐标
  const positions = new Map<string, { x: number; y: number }>();
  for (const [lvl, group] of groups) {
    const groupHeight = group.length * NODE_HEIGHT + Math.max(0, group.length - 1) * ROW_GAP;
    const startY = (height - groupHeight) / 2;
    group.forEach((item, index) => positions.set(item.id, {
      x: 14 + lvl * (NODE_WIDTH + COLUMN_GAP),
      y: startY + index * (NODE_HEIGHT + ROW_GAP),
    }));
  }

  // 生成 SVG 边路径
  const edges = items.flatMap(item => item.dependsOn.flatMap(dep => {
    const from = positions.get(dep);
    const to = positions.get(item.id);
    if (!from || !to) return [];
    const startX = from.x + NODE_WIDTH;
    const startY = from.y + NODE_HEIGHT / 2;
    const endX = to.x;
    const endY = to.y + NODE_HEIGHT / 2;
    const control = Math.max(24, (endX - startX) / 2);
    return [{
      id: `${dep}:${item.id}`,
      path: `M ${startX} ${startY} C ${startX + control} ${startY}, ${endX - control} ${endY}, ${endX} ${endY}`,
      tone: item.tone,
    }];
  }));

  return { width, height, positions, edges };
}

/** 根据状态字符串选择 lucide 图标。 */
function statusIcon(status = '') {
  const normalized = status.toLowerCase();
  if (['passed', 'ready', 'resolved', 'verified', 'succeeded', 'completed'].includes(normalized)) return CheckCircle2;
  if (['failed', 'blocked', 'critical', 'error'].includes(normalized)) return AlertTriangle;
  if (['running', 'active', 'submitting'].includes(normalized)) return LoaderCircle;
  return Clock3;
}

export function A2uiDag({ items }: A2uiDagProps) {
  const layout = useMemo(() => computeLayout(items), [items]);

  const nodeStyle = (item: A2uiDagItem): CSSProperties => {
    const pos = layout.positions.get(item.id) ?? { x: 0, y: 0 };
    return {
      left: `${pos.x}px`,
      top: `${pos.y}px`,
      width: `${NODE_WIDTH}px`,
      height: `${NODE_HEIGHT}px`,
      // CSS 变量供进度条宽度使用
      ['--progress' as string]: `${Math.max(0, Math.min(100, item.progress ?? 0))}%`,
    };
  };

  return (
    <div className="ao-a2ui__dag-scroll">
      <div className="ao-a2ui__dag" style={{ width: `${layout.width}px`, height: `${layout.height}px` }}>
        <svg viewBox={`0 0 ${layout.width} ${layout.height}`} aria-hidden="true">
          <defs>
            <marker id="ao-a2ui-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
              <path d="M0,0 L7,3.5 L0,7 Z" />
            </marker>
          </defs>
          {layout.edges.map(edge => (
            <path
              key={edge.id}
              d={edge.path}
              data-tone={edge.tone}
              markerEnd="url(#ao-a2ui-arrow)"
            />
          ))}
        </svg>
        {items.map(item => {
          const Icon = statusIcon(item.status);
          return (
            <article
              key={item.id}
              className="ao-a2ui__dag-node"
              data-tone={item.tone}
              style={nodeStyle(item)}
            >
              <Icon size={14} aria-hidden="true" />
              <div>
                <strong>{item.title}</strong>
                <span>{item.detail || item.status}</span>
              </div>
              <i aria-hidden="true"><b /></i>
            </article>
          );
        })}
      </div>
    </div>
  );
}

export default A2uiDag;
