import { useState, useEffect, useCallback, useMemo } from 'react';
import { apiClient, type UsageSummary } from '../../lib/api';
import { useQuotaPolling } from '../../hooks/useQuotaPolling';
import { formatTokens, formatNumber, formatCostCompact, alertLevelColor } from './utils';
import { UsageBreakdownModal } from './UsageBreakdownModal';

interface UsagePanelProps {
  /** 自定义 className */
  className?: string;
}

const RANGE_OPTIONS = [
  { days: 7, label: '7天' },
  { days: 14, label: '14天' },
  { days: 30, label: '30天' },
  { days: 90, label: '90天' },
];

/** 热力图固定展示近 26 周（与任务中心一致），独立于 KPI 范围切换 */
const HEATMAP_WEEKS = 26;
const HEATMAP_DAYS = HEATMAP_WEEKS * 7;
/** 固定小格子（控制面板高度，不随宽度拉伸） */
const CELL = 11;
const CELL_GAP = 3;
/** 月份标签行高 */
const MONTH_LABEL_H = 12;
/** 中文星期标签（周一在上，依次到周日） */
const WEEKDAY_LABELS = ['一', '二', '三', '四', '五', '六', '日'];

/** 本地日期 → UTC YYYY-MM-DD key（与后端 date(created_at) 口径对齐） */
function dayKeyUtc(d: Date): string {
  return new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())).toISOString().slice(0, 10);
}

/** 按日 token → 热力图 5 档颜色（与任务中心热力图同款蓝色系，匹配深色主题） */
function heatLevelColor(tokens: number, max: number): string {
  if (tokens <= 0) return 'rgba(148, 163, 184, 0.10)';
  const r = tokens / max;
  if (r <= 0.2) return 'rgba(59,130,246,0.28)';
  if (r <= 0.4) return 'rgba(59,130,246,0.48)';
  if (r <= 0.65) return 'rgba(59,130,246,0.68)';
  if (r <= 0.85) return 'rgba(59,130,246,0.88)';
  return '#3B82F6';
}

/** 计算最近 N 天每日 token 总量（缺日补 0），返回数组长度 = N */
function dailyTokensLastN(byDate: Array<{ date: string; tokens: number }>, n: number): number[] {
  const map = new Map<string, number>();
  for (const r of byDate) map.set(r.date, (map.get(r.date) ?? 0) + r.tokens);
  const out: number[] = new Array(n).fill(0);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  for (let i = 0; i < n; i++) {
    const d = new Date(today);
    d.setDate(today.getDate() - (n - 1 - i));
    const k = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())).toISOString().slice(0, 10);
    out[i] = map.get(k) ?? 0;
  }
  return out;
}

/** 用量区域：左汇总卡（点击穿透）+ 右调用热力图（仅 hover tooltip） */
export function UsagePanel({ className }: UsagePanelProps) {
  const [days, setDays] = useState(30);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  /** 热力图数据：UTC 日期 → 当日 token（固定 26 周，一次加载） */
  const [heatData, setHeatData] = useState<Map<string, number> | null>(null);

  const { quotaData } = useQuotaPolling();

  const loadUsage = useCallback(async (rangeDays: number) => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiClient.getUsageSummary(rangeDays);
      setUsage(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : '用量数据加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadHeat = useCallback(async () => {
    try {
      const data = await apiClient.getUsageSummary(HEATMAP_DAYS);
      const m = new Map<string, number>();
      for (const row of data.by_date) {
        m.set(row.date, (m.get(row.date) ?? 0) + row.tokens);
      }
      setHeatData(m);
    } catch {
      setHeatData(new Map());
    }
  }, []);

  useEffect(() => {
    loadUsage(days);
  }, [days, loadUsage]);

  useEffect(() => {
    loadHeat();
  }, [loadHeat]);

  // 汇总 KPI：累计 / 单日峰值 / 累计成本 / 活跃天数 / 日均
  const summary = useMemo(() => {
    if (!usage) return null;
    const byDateMap = new Map<string, number>();
    const costByDate = new Map<string, number>();
    for (const row of usage.by_date) {
      byDateMap.set(row.date, (byDateMap.get(row.date) ?? 0) + row.tokens);
      costByDate.set(row.date, (costByDate.get(row.date) ?? 0) + row.cost_usd);
    }
    const dailyTotals = Array.from(byDateMap.values());
    const dailyCosts = Array.from(costByDate.values());
    const peak = dailyTotals.length ? Math.max(...dailyTotals) : 0;
    const activeDays = dailyTotals.filter((v) => v > 0).length;
    const spanDays = usage.days;
    return {
      total: usage.total_tokens,
      totalCost: usage.total_cost_usd,
      peak,
      activeDays,
      spanDays,
      dailyAvg: spanDays > 0 ? usage.total_tokens / spanDays : 0,
      // 近 N 天每日 token（用于 sparkline）
      sparkline: dailyTokensLastN(usage.by_date, spanDays),
      // 当日峰值当日成本（KPI 行附加显示，纯展示，可空）
      peakCost: dailyCosts.length ? Math.max(...dailyCosts) : 0,
    };
  }, [usage]);

  // 额度列表：按告警级别 + 使用率排序，全部展示
  const quotaProviders = quotaData?.providers ?? [];
  const levelRank: Record<string, number> = { red: 0, yellow: 1, normal: 2 };
  const sortedQuota = useMemo(
    () =>
      [...quotaProviders].sort(
        (a, b) =>
          (levelRank[a.alert_level] ?? 3) - (levelRank[b.alert_level] ?? 3) ||
          b.percentage - a.percentage,
      ),
    [quotaProviders],
  );

  // 热力图：从本周周一开始往前推 25 周
  const heat = useMemo(() => {
    const today = new Date();
    const dow = (today.getDay() + 6) % 7; // 周一=0
    const start = new Date(today);
    start.setDate(today.getDate() - dow - (HEATMAP_WEEKS - 1) * 7);
    const max = heatData ? Math.max(...heatData.values(), 1) : 1;
    return { today, start, max };
  }, [heatData]);

  const heatCells = useMemo(() => {
    if (!heatData) return null;
    const cells: React.ReactNode[] = [];
    for (let w = 0; w < HEATMAP_WEEKS; w++) {
      for (let d = 0; d < 7; d++) {
        const date = new Date(heat.start);
        date.setDate(heat.start.getDate() + w * 7 + d);
        const after = date > heat.today;
        const key = dayKeyUtc(date);
        const tokens = heatData.get(key) ?? 0;
        cells.push(
          <rect
            key={`${w}-${d}`}
            x={w * (CELL + CELL_GAP)}
            y={d * (CELL + CELL_GAP)}
            width={CELL}
            height={CELL}
            rx={2}
            fill={after ? 'transparent' : heatLevelColor(tokens, heat.max)}
            stroke={after ? 'none' : 'rgba(148, 163, 184, 0.25)'}
            strokeWidth={1}
          >
            <title>{`${key}：${formatTokens(tokens)} Token`}</title>
          </rect>,
        );
      }
    }
    return cells;
  }, [heatData, heat]);

  // 月份标签（每列首日所在月变化时标注）
  const monthLabels = useMemo(() => {
    if (!heatData) return null;
    const labels: React.ReactNode[] = [];
    let lastMonth = -1;
    for (let w = 0; w < HEATMAP_WEEKS; w++) {
      const date = new Date(heat.start);
      date.setDate(heat.start.getDate() + w * 7);
      if (date.getMonth() !== lastMonth) {
        lastMonth = date.getMonth();
        labels.push(
          <text
            key={`m-${w}`}
            x={w * (CELL + CELL_GAP)}
            y={-4}
            fontSize={9}
            fill="var(--color-text-tertiary)"
          >
            {`${lastMonth + 1}月`}
          </text>,
        );
      }
    }
    return labels;
  }, [heatData, heat]);

  // 热力图区紧凑汇总：总调用 / 活跃日 / 单日峰值 token
  const heatStats = useMemo(() => {
    if (!heatData) return null;
    let active = 0;
    let peak = 0;
    for (const v of heatData.values()) {
      if (v > 0) active++;
      if (v > peak) peak = v;
    }
    return { active, peak, totalDays: HEATMAP_DAYS };
  }, [heatData]);

  return (
    <div className={`monitor-usage-row ${className ?? ''}`}>
      {/* 左：汇总卡（KPI + 趋势 + 额度摘要，点击穿透多维度明细） */}
      <div
        className="monitor-usage-summary"
        onClick={() => setModalOpen(true)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && setModalOpen(true)}
      >
        <div className="monitor-usage-summary-head">
          <span className="monitor-usage-kpi-label-title">用量与成本</span>
          <div className="monitor-usage-ranges" onClick={(e) => e.stopPropagation()}>
            {RANGE_OPTIONS.map((opt) => (
              <button
                key={opt.days}
                className={`monitor-usage-range-btn ${days === opt.days ? 'active' : ''}`}
                onClick={() => setDays(opt.days)}
                type="button"
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {loading && !usage ? (
          <span className="monitor-usage-loading">加载中...</span>
        ) : error ? (
          <span className="monitor-quota-error-inline">用量加载失败</span>
        ) : usage && summary ? (
          <>
            {/* KPI 三联：累计 Token · 累计成本 · 单日峰值 */}
            <div className="monitor-usage-kpi-row">
              <div className="monitor-usage-kpi-cell">
                <span className="monitor-usage-kpi-huge font-mono">{formatNumber(summary.total)}</span>
                <span className="monitor-usage-kpi-sub">累计 Token</span>
              </div>
              <span className="monitor-usage-kpi-divider" aria-hidden />
              <div className="monitor-usage-kpi-cell">
                <span className="monitor-usage-kpi-huge font-mono">{formatCostCompact(summary.totalCost)}</span>
                <span className="monitor-usage-kpi-sub">累计成本</span>
              </div>
              <span className="monitor-usage-kpi-divider" aria-hidden />
              <div className="monitor-usage-kpi-cell">
                <span className="monitor-usage-kpi-huge font-mono">{formatNumber(summary.peak)}</span>
                <span className="monitor-usage-kpi-sub">单日峰值</span>
              </div>
            </div>

            {/* 二级 KPI：活跃天数 / 日均 / 窗口期（一行紧凑展示） */}
            <div className="monitor-usage-sub-kpi">
              <span><b>{summary.activeDays}</b>/{summary.spanDays} 活跃</span>
              <span className="dot" />
              <span>日均 <b>{formatNumber(Math.round(summary.dailyAvg))}</b></span>
              <span className="dot" />
              <span>峰值当日 <b className="font-mono">{formatCostCompact(summary.peakCost)}</b></span>
            </div>

            {/* 日趋势 sparkline（横向条形） */}
            <Sparkline data={summary.sparkline} />

            {/* 全部 provider 额度列表（按告警级别+使用率排序，最多前 4） */}
            {sortedQuota.length > 0 && (
              <div className="monitor-usage-quota-list" onClick={(e) => e.stopPropagation()}>
                {sortedQuota.slice(0, 4).map((q) => (
                  <div key={q.provider_id} className="monitor-usage-quota-row">
                    <span className="monitor-usage-quota-name">{q.display_name || q.provider_id}</span>
                    <span className="monitor-usage-quota-track">
                      <span
                        className="monitor-usage-quota-fill"
                        style={{
                          width: `${Math.min(100, q.percentage)}%`,
                          background: alertLevelColor(q.alert_level),
                        }}
                      />
                    </span>
                    <span
                      className="font-mono monitor-usage-quota-pct"
                      style={{ color: alertLevelColor(q.alert_level) }}
                    >
                      {q.percentage.toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : null}
      </div>

      {/* 右：调用热力图（近 26 周，hover 看 tooltip，不穿透） */}
      <div className="monitor-usage-heat">
        <div className="monitor-usage-heat-section">
          <div className="monitor-usage-heat-head">
            <span className="monitor-usage-kpi-label-title">调用热力图</span>
            <span className="monitor-usage-heat-sub">近 {HEATMAP_WEEKS} 周</span>
          </div>
          <div className="monitor-usage-heat-body">
            {heatCells ? (
              <>
                <div className="monitor-usage-heat-svg-wrap">
                  <div
                    className="monitor-usage-heat-weekdays"
                    style={{
                      gridTemplateRows: `repeat(7, ${CELL}px)`,
                      gap: `${CELL_GAP}px`,
                      paddingTop: MONTH_LABEL_H,
                    }}
                  >
                    {WEEKDAY_LABELS.map((wd) => (
                      <span key={wd} className="monitor-usage-heat-weekday" style={{ lineHeight: `${CELL}px` }}>{wd}</span>
                    ))}
                  </div>
                  <svg
                    width={HEATMAP_WEEKS * (CELL + CELL_GAP)}
                    height={7 * (CELL + CELL_GAP) + MONTH_LABEL_H}
                    style={{ display: 'block', overflow: 'visible' }}
                  >
                    <g transform={`translate(0,${MONTH_LABEL_H})`}>{heatCells}</g>
                    {monthLabels}
                  </svg>
                </div>
                {/* 紧凑汇总：3 个关键数字一行 */}
                <div className="monitor-usage-heat-stats">
                  <span>
                    <b>{formatNumber(heatStats?.active ?? 0)}</b>
                    <i> 活跃日</i>
                  </span>
                  <span className="sep" />
                  <span>
                    <b>{heatStats ? Math.round((heatStats.active / heatStats.totalDays) * 100) : 0}%</b>
                    <i> 活跃率</i>
                  </span>
                  <span className="sep" />
                  <span>
                    <b>{formatTokens(heatStats?.peak ?? 0)}</b>
                    <i> 单日峰值 Token</i>
                  </span>
                </div>
                <div className="monitor-usage-heat-legend">
                  <span>少</span>
                  {[0, 1, 2, 3, 4, 5].map((l) => (
                    <span
                      key={l}
                      style={{
                        width: 10,
                        height: 10,
                        borderRadius: 2,
                        background: heatLevelColorSample(l),
                        border: '1px solid var(--color-border-subtle)',
                      }}
                    />
                  ))}
                  <span>多</span>
                </div>
              </>
            ) : (
              <span className="monitor-usage-loading">加载中...</span>
            )}
          </div>
        </div>
      </div>

      <UsageBreakdownModal open={modalOpen} onClose={() => setModalOpen(false)} initialDays={days} />
    </div>
  );
}

/**
 * 横向 sparkline（日 token 趋势条形图，last N 天为一组）
 * 用 SVG <rect> 替代渲染，每个 bar 宽按父容器比例；用作"近 N 天用量趋势"。
 */
function Sparkline({ data }: { data: number[] }) {
  // data 长度可能很大（90），按容器宽度自适应 cell 大小（最窄 3px，最宽 6px）
  // 这里只取 data 的最后段渲染（前端默认取 N=days，已足够）
  if (!data.length) return null;
  const max = Math.max(...data, 1);
  const H = 22;
  return (
    <div className="monitor-usage-spark">
      <span className="monitor-usage-spark-label">日趋势</span>
      <svg
        className="monitor-usage-spark-svg"
        viewBox={`0 0 ${data.length} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label="近 N 天每日 Token 趋势"
      >
        {data.map((v, i) => {
          const h = (v / max) * (H - 2);
          return (
            <rect
              key={i}
              x={i}
              y={H - h}
              width={0.85}
              height={h}
              fill={v === 0 ? 'rgba(148,163,184,0.18)' : 'rgba(59,130,246,0.75)'}
            >
              <title>{`Day ${i + 1}: ${formatTokens(v)} Token`}</title>
            </rect>
          );
        })}
      </svg>
    </div>
  );
}

/** 图例颜色采样（与 heatLevelColor 对齐） */
function heatLevelColorSample(level: number): string {
  if (level === 0) return 'rgba(148, 163, 184, 0.10)';
  const stops = ['rgba(59,130,246,0.28)', 'rgba(59,130,246,0.48)', 'rgba(59,130,246,0.68)', 'rgba(59,130,246,0.88)', '#3B82F6'];
  return stops[Math.min(level - 1, stops.length - 1)];
}
