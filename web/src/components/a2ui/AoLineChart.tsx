/**
 * A2UI 折线图渲染组件。
 *
 * 职责：把多系列数值数据渲染为纯 SVG 折线图（polyline + circle + title tooltip）。
 * 用途：A2uiNode.tsx 中需要折线图渲染时调用本组件。
 *
 * 参考：ChartWidget.tsx 的 line 模式渲染逻辑，适配为独立组件（接收 props 而非从 widget props 解构）。
 */

export interface AoLineChartProps {
  series: Array<{ name: string; data: number[] }>;
  xAxis: string[];
  unit?: string;
}

const DEFAULT_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899'];

export function AoLineChart({ series, xAxis, unit = '' }: AoLineChartProps) {
  // 归一化系列：补默认颜色，过滤空系列
  const lineSeries = series
    .map((s, i) => ({
      name: s.name,
      data: s.data,
      color: DEFAULT_COLORS[i % DEFAULT_COLORS.length],
    }))
    .filter((s) => s.data.length > 0);

  // 空数据兜底
  if (lineSeries.length === 0) {
    return <div className="ao-a2ui__chart-empty">暂无数据</div>;
  }

  const pointCount = Math.max(
    ...lineSeries.map((s) => s.data.length),
    xAxis.length,
    1,
  );
  const xLabelsPadded = Array.from(
    { length: pointCount },
    (_, i) => xAxis[i] || `第${i + 1}项`,
  );
  const allNumbers = lineSeries.flatMap((s) => s.data).filter((v) => Number.isFinite(v));
  const maxValue = allNumbers.length > 0 ? Math.max(...allNumbers, 1) : 1;
  const minValue = allNumbers.length > 0 ? Math.min(...allNumbers, 0) : 0;

  // 自适应宽度：每个点 60px（最小 320px）
  const step = 60;
  const chartWidth = Math.max(pointCount * step, 320);
  const chartHeight = 180;
  const padding = { left: 40, right: 12, top: 20, bottom: 30 };
  const innerWidth = chartWidth - padding.left - padding.right;
  const innerHeight = chartHeight - padding.top - padding.bottom;
  const xOf = (i: number) =>
    padding.left + (pointCount <= 1 ? innerWidth / 2 : (i * innerWidth) / (pointCount - 1));
  const yOf = (v: number) =>
    padding.top + innerHeight - ((v - minValue) / (maxValue - minValue || 1)) * innerHeight;

  return (
    <div className="ao-a2ui__chart-scroll" style={{ overflowX: 'auto' }}>
      {lineSeries.length > 1 && (
        <div
          className="ao-a2ui__chart-legend"
          style={{ display: 'flex', gap: '10px', fontSize: '11px', color: 'var(--color-text-secondary)' }}
        >
          {lineSeries.map((s, i) => (
            <span
              key={i}
              className="ao-a2ui__chart-legend-item"
              style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}
            >
              <span
                className="ao-a2ui__chart-legend-swatch"
                style={{ width: '10px', height: '2px', background: s.color, borderRadius: '1px' }}
              />
              {s.name}
            </span>
          ))}
        </div>
      )}
      <svg width={chartWidth} height={chartHeight + 24} viewBox={`0 0 ${chartWidth} ${chartHeight + 24}`}>
        {/* y 轴基线（min~max 范围） */}
        <line
          x1={padding.left}
          y1={padding.top}
          x2={padding.left}
          y2={padding.top + innerHeight}
          stroke="var(--color-border-subtle)"
          strokeWidth="1"
        />
        <line
          x1={padding.left}
          y1={padding.top + innerHeight}
          x2={padding.left + innerWidth}
          y2={padding.top + innerHeight}
          stroke="var(--color-border-subtle)"
          strokeWidth="1"
        />
        {/* y 轴标签：min/max */}
        <text x={padding.left - 6} y={padding.top + 4} textAnchor="end" fontSize="10" fill="var(--color-text-tertiary)">
          {Math.round(maxValue)}{unit}
        </text>
        <text x={padding.left - 6} y={padding.top + innerHeight} textAnchor="end" fontSize="10" fill="var(--color-text-tertiary)">
          {Math.round(minValue)}{unit}
        </text>
        {/* 每个 series 一条折线 + 数据点 */}
        {lineSeries.map((s, si) => {
          const points = s.data
            .map((v, i) => ({ v, i }))
            .filter(({ v }) => Number.isFinite(v))
            .map(({ v, i }) => `${xOf(i)},${yOf(v)}`)
            .join(' ');
          return (
            <g key={si}>
              <polyline points={points} fill="none" stroke={s.color} strokeWidth="2" />
              {s.data.map((v, i) => {
                if (!Number.isFinite(v)) return null;
                return (
                  <g key={i}>
                    <circle cx={xOf(i)} cy={yOf(v)} r="3" fill={s.color}>
                      <title>{s.name} · {xLabelsPadded[i]}: {v}{unit}</title>
                    </circle>
                    {lineSeries.length === 1 && (
                      <text
                        x={xOf(i)}
                        y={yOf(v) - 8}
                        textAnchor="middle"
                        fontSize="10"
                        fill="var(--color-text-secondary)"
                        className="font-mono"
                      >
                        {v}{unit}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          );
        })}
        {/* x 轴标签 */}
        {xLabelsPadded.map((lbl, i) => (
          <text
            key={i}
            x={xOf(i)}
            y={chartHeight - 8}
            textAnchor="middle"
            fontSize="10"
            fill="var(--color-text-tertiary)"
          >
            {lbl.length > 6 ? lbl.slice(0, 6) + '…' : lbl}
          </text>
        ))}
      </svg>
    </div>
  );
}

export default AoLineChart;
