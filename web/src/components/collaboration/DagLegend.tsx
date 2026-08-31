/**
 * DagLegend — DAG 节点形状图例（内容组件，含折叠状态与 localStorage 持久化）。
 *
 * 折叠/定位由本组件控制（不依赖外层容器）：
 *  - 折叠状态写入 localStorage（key: agentops.dagLegend.collapsed）
 *  - 提供按钮切换折叠/展开
 *  - 默认展开（首次访问无 key 时）
 */
import { useEffect, useState } from 'react';
import { listDagLegendEntries } from './DagNodeSemantics';

const LS_KEY = 'agentops.dagLegend.collapsed';

function readCollapsed(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(LS_KEY) === '1';
  } catch {
    return false;
  }
}

export function DagLegend() {
  const entries = listDagLegendEntries();
  const [collapsed, setCollapsed] = useState<boolean>(readCollapsed);

  useEffect(() => {
    try {
      window.localStorage.setItem(LS_KEY, collapsed ? '1' : '0');
    } catch {
      // 忽略 localStorage 写入失败（隐私模式/容量满）
    }
  }, [collapsed]);

  return (
    <div
      className={`dag-legend-content ${collapsed ? 'collapsed' : ''}`}
      aria-label="DAG node shape legend"
    >
      <button
        type="button"
        className="dag-legend-collapsible-title"
        onClick={() => setCollapsed(c => !c)}
        aria-expanded={!collapsed}
        style={{
          background: 'none',
          border: 'none',
          color: 'inherit',
          cursor: 'pointer',
          width: '100%',
          textAlign: 'left',
          padding: 0,
          font: 'inherit',
        }}
      >
        节点类型 {collapsed ? '▸' : '▾'}
      </button>
      {!collapsed && (
        <ul className="dag-legend-list" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {entries.map((entry) => (
            <li
              key={entry.shape}
              className="dag-legend-item"
              data-shape={entry.shape}
              data-tone={entry.tone}
              style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, fontSize: 11 }}
            >
              <span className={`dag-legend-shape dag-tone-${entry.tone}`} aria-hidden style={{ fontSize: 14, width: 20, textAlign: 'center' }}>
                {shapeGlyph(entry.shape)}
              </span>
              <span className="dag-legend-text" style={{ display: 'flex', flexDirection: 'column' }}>
                <span className="dag-legend-label" style={{ color: '#e6ecf5', fontWeight: 600, fontSize: 11 }}>{entry.label}</span>
                <span className="dag-legend-concept" style={{ color: '#8b97b0', fontSize: 9 }}>{entry.concept}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 给图例项画一个迷你 svg（避免每个 legend 项都 mount 一个完整 DagNodeCard）。 */
function shapeGlyph(shape: string): string {
  switch (shape) {
    case 'circle': return '●';
    case 'triangle': return '▲';
    case 'diamond': return '◆';
    case 'capsule': return '⬭';
    case 'rounded_rect': return '▢';
    case 'octagon': return '⯃';
    case 'hexagon': return '⬡';
    case 'parallelogram': return '▱';
    default: return '○';
  }
}

export default DagLegend;
