/**
 * A2UI 饼图渲染组件。
 *
 * 职责：把 {label, value} 列表渲染为纯 SVG 饼图（path 扇形 + 右侧图例）。
 * 用途：A2uiNode.tsx 中需要饼图渲染时调用本组件。
 *
 * 参考：ChartWidget.tsx 的 pie 模式渲染逻辑，适配为独立组件（接收 props 而非从 widget props 解构）。
 */

export interface AoPieChartProps {
  items: Array<{ label: string; value: number }>;
  unit?: string;
}

const DEFAULT_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899'];

export function AoPieChart({ items, unit = '' }: AoPieChartProps) {
  // 过滤 NaN 值
  const pieData = items
    .map((item, i) => ({
      label: item.label,
      value: item.value,
      color: DEFAULT_COLORS[i % DEFAULT_COLORS.length],
    }))
    .filter((item) => Number.isFinite(item.value));

  // 空数据兜底
  if (pieData.length === 0) {
    return <div className="ao-a2ui__chart-empty">暂无数据</div>;
  }

  const total = pieData.reduce((sum, s) => sum + s.value, 0) || 1;
  let cumulative = 0;
  const radius = 60;
  const cx = 80;
  const cy = 80;

  return (
    <div
      className="ao-a2ui__pie-chart"
      style={{ display: 'flex', gap: '16px', alignItems: 'center' }}
    >
      <svg width="160" height="160" viewBox="0 0 160 160">
        {pieData.map((s, i) => {
          const startAngle = (cumulative / total) * 2 * Math.PI - Math.PI / 2;
          cumulative += s.value;
          const endAngle = (cumulative / total) * 2 * Math.PI - Math.PI / 2;
          const x1 = cx + radius * Math.cos(startAngle);
          const y1 = cy + radius * Math.sin(startAngle);
          const x2 = cx + radius * Math.cos(endAngle);
          const y2 = cy + radius * Math.sin(endAngle);
          const largeArc = endAngle - startAngle > Math.PI ? 1 : 0;
          return (
            <path
              key={i}
              d={`M ${cx} ${cy} L ${x1} ${y1} A ${radius} ${radius} 0 ${largeArc} 1 ${x2} ${y2} Z`}
              fill={s.color}
              stroke="white"
              strokeWidth="1"
            />
          );
        })}
      </svg>
      <div
        className="ao-a2ui__pie-legend"
        style={{ display: 'flex', flexDirection: 'column', gap: '4px', fontSize: '12px' }}
      >
        {pieData.map((s, i) => (
          <div
            key={i}
            className="ao-a2ui__pie-legend-item"
            style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
          >
            <span
              className="ao-a2ui__pie-legend-swatch"
              style={{ width: '10px', height: '10px', borderRadius: '2px', background: s.color }}
            />
            <span>{s.label}</span>
            <span className="font-mono" style={{ color: 'var(--color-text-tertiary)' }}>
              {s.value}{unit}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default AoPieChart;
