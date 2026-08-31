import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  apiClient,
  type UsageSummary,
  type UsageBreakdown,
  type UsageBreakdownRow,
} from '../../lib/api';
import { useQuotaPolling } from '../../hooks/useQuotaPolling';
import { formatTokens, formatCost, formatNumber, formatCountdown, alertLevelColor } from './utils';

interface UsageBreakdownModalProps {
  open: boolean;
  onClose: () => void;
  /** 打开时的初始范围（与汇总卡片当前范围一致） */
  initialDays: number;
}

const RANGE_OPTIONS = [
  { days: 7, label: '7天' },
  { days: 14, label: '14天' },
  { days: 30, label: '30天' },
  { days: 90, label: '90天' },
];

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'workflow', label: '按业务' },
  { key: 'agent', label: '按 Agent' },
  { key: 'provider', label: '服务商与模型' },
] as const;

type TabKey = (typeof TABS)[number]['key'];

/** Provider 配色（按区间用量降序分配） */
const PROVIDER_COLORS = [
  '#3B82F6', '#10B981', '#F59E0B', '#A855F7',
  '#06B6D4', '#F43F5E', '#84CC16', '#64748B',
];

/** 概览趋势图高度（px） */
const TREND_HEIGHT = 110;

const MODAL_STYLE = `
.ubm-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex; align-items: flex-start; justify-content: center;
  padding-top: 60px; z-index: 1000;
}
.ubm-modal {
  width: 860px; max-width: calc(100vw - 32px);
  max-height: calc(100vh - 120px);
  display: flex; flex-direction: column;
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-lg);
  box-shadow: 0 12px 40px rgba(0,0,0,0.4);
  animation: ubm-in 0.18s ease;
}
@keyframes ubm-in { from { opacity: 0; transform: translateY(8px) scale(0.98); } to { opacity: 1; transform: none; } }
.ubm-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px 10px; border-bottom: 1px solid var(--color-border-subtle);
}
.ubm-title { font-size: 15px; font-weight: 600; color: var(--color-text-primary); }
.ubm-close {
  background: none; border: none; cursor: pointer; color: var(--color-text-tertiary);
  padding: 4px; border-radius: var(--radius-sm); line-height: 0;
}
.ubm-close:hover { color: var(--color-text-primary); background: var(--color-bg-elevated); }
.ubm-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 18px 0; gap: 12px;
}
.ubm-tabs { display: flex; gap: 2px; }
.ubm-tab {
  padding: 5px 12px; font-size: 12px; cursor: pointer;
  background: none; border: none; border-bottom: 2px solid transparent;
  color: var(--color-text-secondary);
}
.ubm-tab:hover { color: var(--color-text-primary); }
.ubm-tab.active { color: var(--color-primary); border-bottom-color: var(--color-primary); font-weight: 600; }
.ubm-ranges { display: flex; gap: 3px; }
.ubm-range-btn {
  padding: 2px 8px; font-size: 11px; border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-sm); background: var(--color-bg-base);
  color: var(--color-text-secondary); cursor: pointer; transition: all 0.15s ease;
}
.ubm-range-btn:hover { border-color: var(--color-primary); color: var(--color-text-primary); }
.ubm-range-btn.active { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }
.ubm-body { padding: 12px 18px 18px; overflow-y: auto; flex: 1; }
.ubm-kpi-row { display: flex; flex-wrap: wrap; gap: 6px 18px; padding: 4px 0 10px; }
.ubm-kpi { display: flex; align-items: baseline; gap: 4px; }
.ubm-kpi-label { font-size: 11px; color: var(--color-text-tertiary); }
.ubm-kpi-value { font-size: 13px; font-weight: 600; color: var(--color-text-primary); }
.ubm-loading { padding: 32px; text-align: center; color: var(--color-text-tertiary); font-size: 12px; }
.ubm-empty { padding: 24px; text-align: center; color: var(--color-text-tertiary); font-size: 12px; }

/* 概览趋势图（按 provider 堆叠） */
.ubm-trend { display: flex; align-items: stretch; gap: 2px; height: 110px; padding: 6px 2px 0; border-bottom: 1px solid var(--color-border-subtle); }
.ubm-trend-day { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: flex-end; border-radius: 1px 1px 0 0; }
.ubm-trend-day:hover { background: var(--color-bg-base); }
.ubm-trend-seg { width: 100%; }
.ubm-trend-legend { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 8px 0 2px; }
.ubm-trend-legend-item { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--color-text-secondary); }
.ubm-trend-legend-dot { width: 8px; height: 8px; border-radius: 2px; }

/* 维度表格 */
.ubm-table { width: 100%; border-collapse: collapse; }
.ubm-table th {
  text-align: left; font-size: 10px; font-weight: 500; color: var(--color-text-tertiary);
  text-transform: uppercase; letter-spacing: 0.04em;
  padding: 6px 8px; border-bottom: 1px solid var(--color-border-subtle);
}
.ubm-table td { padding: 6px 8px; border-bottom: 1px solid var(--color-border-subtle); font-size: 12px; vertical-align: middle; }
.ubm-table tr:hover td { background: var(--color-bg-elevated); }
.ubm-table .num { text-align: right; font-family: var(--font-mono, monospace); color: var(--color-text-secondary); white-space: nowrap; }
.ubm-dim-cell { display: flex; align-items: center; gap: 6px; min-width: 0; }
.ubm-dim-text { font-family: var(--font-mono, monospace); font-size: 11px; color: var(--color-text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 260px; }
.ubm-dim-bar-track { height: 4px; min-width: 60px; background: var(--color-bg-base); border-radius: var(--radius-full); overflow: hidden; flex: 1; }
.ubm-dim-bar-fill { height: 100%; border-radius: var(--radius-full); background: var(--color-primary); }
.ubm-section-title { font-size: 12px; font-weight: 600; color: var(--color-text-secondary); letter-spacing: 0.05em; margin: 14px 0 6px; }
.ubm-section-title:first-child { margin-top: 0; }

/* 额度完整列表 */
.ubm-quota-row { display: grid; grid-template-columns: 150px 1fr 170px 60px 90px; gap: 10px; align-items: center; padding: 7px 10px; background: var(--color-bg-base); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-sm); margin-bottom: 6px; }
.ubm-quota-label { display: flex; flex-direction: column; gap: 1px; overflow: hidden; }
.ubm-quota-name { font-size: 12px; font-weight: 600; color: var(--color-text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ubm-quota-pid { font-size: 10px; color: var(--color-text-tertiary); }
.ubm-quota-track { height: 6px; background: var(--color-bg-elevated); border-radius: var(--radius-full); overflow: hidden; }
.ubm-quota-fill { height: 100%; border-radius: var(--radius-full); transition: width 0.3s ease; }
.ubm-quota-numbers { font-size: 10px; color: var(--color-text-secondary); white-space: nowrap; text-align: right; font-family: var(--font-mono, monospace); }
.ubm-quota-pct { font-size: 12px; font-weight: 600; text-align: right; font-family: var(--font-mono, monospace); }
.ubm-quota-reset { font-size: 10px; color: var(--color-text-tertiary); text-align: right; white-space: nowrap; font-family: var(--font-mono, monospace); }
`;

/** 维度表格（业务/Agent/服务商/模型共用） */
function DimensionTable({ rows, totalTokens }: { rows: UsageBreakdownRow[]; totalTokens: number }) {
  if (rows.length === 0) return <div className="ubm-empty">区间内暂无数据</div>;
  return (
    <table className="ubm-table">
      <thead>
        <tr>
          <th style={{ width: '38%' }}>名称</th>
          <th style={{ width: '14%' }}>占比</th>
          <th className="num">Token</th>
          <th className="num">输入/输出/缓存</th>
          <th className="num">成本</th>
          <th className="num">运行次数</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const pct = totalTokens > 0 ? (r.tokens / totalTokens) * 100 : 0;
          return (
            <tr key={r.dim}>
              <td>
                <div className="ubm-dim-cell">
                  <span className="ubm-dim-text" title={r.dim}>{r.dim}</span>
                </div>
              </td>
              <td>
                <div className="ubm-dim-cell">
                  <span className="ubm-dim-bar-track">
                    <span className="ubm-dim-bar-fill" style={{ width: `${pct}%` }} />
                  </span>
                  <span className="num" style={{ fontSize: 11 }}>{pct.toFixed(1)}%</span>
                </div>
              </td>
              <td className="num">{formatTokens(r.tokens)}</td>
              <td className="num" style={{ fontSize: 11 }}>
                {formatTokens(r.input_tokens)} / {formatTokens(r.output_tokens)} / {formatTokens(r.cache_tokens)}
              </td>
              <td className="num">{formatCost(r.cost_usd)}</td>
              <td className="num">{formatNumber(r.run_count)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** 用量多维度穿透弹窗：概览趋势 + 业务/Agent/服务商模型维度分析 + 完整额度 */
export function UsageBreakdownModal({ open, onClose, initialDays }: UsageBreakdownModalProps) {
  const [days, setDays] = useState(initialDays);
  const [tab, setTab] = useState<TabKey>('overview');
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [breakdown, setBreakdown] = useState<UsageBreakdown | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { quotaData } = useQuotaPolling();
  const [now, setNow] = useState(() => Date.now());

  // 范围跟随打开时的卡片范围
  useEffect(() => {
    if (open) setDays(initialDays);
  }, [open, initialDays]);

  // 打开/切范围时加载数据
  const load = useCallback(async (rangeDays: number, isOpen: boolean) => {
    if (!isOpen) return;
    setLoading(true);
    setError(null);
    try {
      const [s, b] = await Promise.all([
        apiClient.getUsageSummary(rangeDays),
        apiClient.getUsageBreakdown(rangeDays),
      ]);
      setSummary(s);
      setBreakdown(b);
    } catch (e) {
      setError(e instanceof Error ? e.message : '明细数据加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(days, open);
  }, [open, days, load]);

  // Esc 关闭 + 额度倒计时每秒刷新
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.clearInterval(timer);
    };
  }, [open, onClose]);

  // 概览趋势：按日 + provider 堆叠
  const providerOrder = useMemo(
    () => [...(summary?.by_provider ?? [])].sort((a, b) => b.tokens - a.tokens).map((p) => p.provider_id),
    [summary],
  );
  const providerColor = useCallback((pid: string) => {
    const idx = providerOrder.indexOf(pid);
    return PROVIDER_COLORS[idx >= 0 ? idx % PROVIDER_COLORS.length : PROVIDER_COLORS.length - 1];
  }, [providerOrder]);

  const dailyStacked = useMemo(() => {
    if (!summary) return [];
    const byDate = new Map<string, Array<{ providerId: string; tokens: number }>>();
    for (const row of summary.by_date) {
      let arr = byDate.get(row.date);
      if (!arr) { arr = []; byDate.set(row.date, arr); }
      arr.push({ providerId: row.provider_id, tokens: row.tokens });
    }
    const result: Array<{ date: string; tokens: number; cost: number; stack: Array<{ providerId: string; tokens: number }> }> = [];
    const nowMs = Date.now();
    for (let i = Math.max(1, summary.days) - 1; i >= 0; i--) {
      const key = new Date(nowMs - i * 86400000).toISOString().slice(0, 10);
      const stack = (byDate.get(key) ?? [])
        .map((s) => ({ providerId: s.providerId, tokens: s.tokens }))
        .sort((a, b) => providerOrder.indexOf(a.providerId) - providerOrder.indexOf(b.providerId));
      const tokens = stack.reduce((n, s) => n + s.tokens, 0);
      const cost = (summary.by_date.filter((r) => r.date === key)).reduce((n, r) => n + r.cost_usd, 0);
      result.push({ date: key, tokens, cost, stack });
    }
    return result;
  }, [summary, providerOrder]);

  const maxDaily = dailyStacked.length ? Math.max(...dailyStacked.map((d) => d.tokens), 1) : 1;

  if (!open) return null;

  const totalTokens = summary?.total_tokens ?? 0;
  const quotaProviders = quotaData?.providers ?? [];

  return (
    <div className="ubm-overlay" onClick={onClose}>
      <style>{MODAL_STYLE}</style>
      <div className="ubm-modal" onClick={(e) => e.stopPropagation()}>
        <div className="ubm-header">
          <span className="ubm-title">用量分析明细</span>
          <button className="ubm-close" onClick={onClose} title="关闭 (Esc)" type="button">✕</button>
        </div>

        <div className="ubm-toolbar">
          <div className="ubm-tabs">
            {TABS.map((t) => (
              <button
                key={t.key}
                className={`ubm-tab ${tab === t.key ? 'active' : ''}`}
                onClick={() => setTab(t.key)}
                type="button"
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="ubm-ranges">
            {RANGE_OPTIONS.map((opt) => (
              <button
                key={opt.days}
                className={`ubm-range-btn ${days === opt.days ? 'active' : ''}`}
                onClick={() => setDays(opt.days)}
                type="button"
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        <div className="ubm-body">
          {loading && !breakdown ? (
            <div className="ubm-loading">正在加载明细数据...</div>
          ) : error ? (
            <div className="ubm-empty">加载失败：{error}</div>
          ) : !summary || !breakdown ? (
            <div className="ubm-empty">暂无数据</div>
          ) : tab === 'overview' ? (
            <>
              <div className="ubm-kpi-row">
                <span className="ubm-kpi">
                  <span className="ubm-kpi-label">Token</span>
                  <span className="ubm-kpi-value font-mono">{formatNumber(summary.total_tokens)}</span>
                </span>
                <span className="ubm-kpi">
                  <span className="ubm-kpi-label">成本</span>
                  <span className="ubm-kpi-value font-mono">{formatCost(summary.total_cost_usd)}</span>
                </span>
                <span className="ubm-kpi">
                  <span className="ubm-kpi-label">日均</span>
                  <span className="ubm-kpi-value font-mono">{formatNumber(Math.round(summary.total_tokens / Math.max(1, summary.days)))}</span>
                </span>
              </div>
              {summary.total_tokens === 0 ? (
                <div className="ubm-empty">区间内暂无用量</div>
              ) : (
                <>
                  <div className="ubm-trend">
                    {dailyStacked.map((d) => (
                      <div
                        key={d.date}
                        className="ubm-trend-day"
                        title={[
                          d.date,
                          `Token ${formatTokens(d.tokens)} · ${formatCost(d.cost)}`,
                          ...d.stack.map((s) => `${s.providerId} ${formatTokens(s.tokens)}`),
                        ].join('\n')}
                      >
                        {d.stack.map((seg) => (
                          <div
                            key={seg.providerId}
                            className="ubm-trend-seg"
                            style={{
                              height: `${Math.max((seg.tokens / maxDaily) * TREND_HEIGHT, 2)}px`,
                              background: providerColor(seg.providerId),
                            }}
                          />
                        ))}
                      </div>
                    ))}
                  </div>
                  <div className="ubm-trend-legend">
                    {providerOrder.map((pid) => (
                      <span key={pid} className="ubm-trend-legend-item">
                        <span className="ubm-trend-legend-dot" style={{ background: providerColor(pid) }} />
                        {pid}
                      </span>
                    ))}
                  </div>
                </>
              )}
            </>
          ) : tab === 'workflow' ? (
            <DimensionTable rows={breakdown.by_workflow} totalTokens={totalTokens} />
          ) : tab === 'agent' ? (
            <DimensionTable rows={breakdown.by_agent} totalTokens={totalTokens} />
          ) : (
            <>
              {quotaProviders.length > 0 && (
                <>
                  <div className="ubm-section-title">额度状态</div>
                  {quotaProviders.map((p) => {
                    const color = alertLevelColor(p.alert_level);
                    const resetMs = Date.parse(p.reset_at);
                    const resetSec = Number.isFinite(resetMs)
                      ? Math.max(0, Math.floor((resetMs - now) / 1000))
                      : Math.max(0, p.reset_in_seconds);
                    return (
                      <div key={p.provider_id} className="ubm-quota-row">
                        <div className="ubm-quota-label">
                          <span className="ubm-quota-name">{p.display_name || p.provider_id}</span>
                          <span className="ubm-quota-pid font-mono">{p.provider_id}</span>
                        </div>
                        <div className="ubm-quota-track">
                          <div className="ubm-quota-fill" style={{ width: `${Math.min(100, p.percentage)}%`, background: color }} />
                        </div>
                        <span className="ubm-quota-numbers">
                          {formatNumber(p.used_tokens)} / {formatNumber(p.total_tokens)}
                        </span>
                        <span className="ubm-quota-pct" style={{ color }}>{p.percentage.toFixed(1)}%</span>
                        <span className="ubm-quota-reset">{formatCountdown(resetSec)} 重置</span>
                      </div>
                    );
                  })}
                </>
              )}
              <div className="ubm-section-title">按服务商</div>
              <DimensionTable rows={breakdown.by_provider} totalTokens={totalTokens} />
              <div className="ubm-section-title">按模型</div>
              <DimensionTable rows={breakdown.by_model} totalTokens={totalTokens} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
