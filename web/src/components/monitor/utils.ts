// 监控中心共享格式化工具（从 DashboardPage/UsageDashboard 抽取，避免重复定义）

/** 格式化成本（¥，保留 4 位小数，避免小成本显示为 0） */
export function formatCost(cny: number): string {
  if (cny === 0) return '¥0.0000';
  if (cny < 0.01) return `¥${cny.toFixed(6)}`;
  return `¥${cny.toFixed(4)}`;
}

/** 紧凑版成本（>¥1000 自动 k 简写，<=1 保留 4 位小数）。监控中心用量卡 KPI 用 */
export function formatCostCompact(cny: number): string {
  if (cny === 0) return '¥0';
  if (cny < 1) return `¥${cny.toFixed(4)}`;
  if (cny >= 10_000) return `¥${(cny / 1000).toFixed(2)}k`;
  return `¥${cny.toFixed(2)}`;
}

/** 格式化 token 数量（K/M 简写） */
export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

/** 格式化数字（千分位） */
export function formatNumber(n: number): string {
  return n.toLocaleString('en-US');
}

/** 格式化耗时（基于 started_at 和 finished_at） */
export function formatDuration(startedAt: string, finishedAt: string | null): string {
  if (!finishedAt) return '运行中';
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return `${m}m ${s}s`;
}

/** 格式化已耗时（从 startedAt 到当前，每秒刷新场景使用）
 *  P0.18.13：startedAt 为空/null/无效时显示 "—"，避免 new Date(null) 算出 49 万小时；
 *  超过 24h 一律显示 "24h+"，避免历史遗留 run 的卡死时长污染监控。
 */
export function formatElapsed(startedAt: string | null | undefined, now: Date = new Date()): string {
  if (!startedAt) return '—';
  const start = new Date(startedAt).getTime();
  const nowMs = now.getTime();
  if (isNaN(start) || nowMs <= start) return '—';
  const ms = nowMs - start;
  // P0.18.13：超过 24h 显示 "24h+"，避免历史遗留卡死时长误导监控
  if (ms >= 24 * 3600 * 1000) return '24h+';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(0)}s`;
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const remainM = m % 60;
  return `${h}h ${remainM}m`;
}

/** 格式化倒计时秒数为 HH:MM:SS */
export function formatCountdown(seconds: number): string {
  if (seconds <= 0) return '00:00:00';
  const totalSec = Math.floor(seconds);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const pad = (v: number) => String(v).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

/** 格式化时间（月/日 时:分） */
export function formatTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** 截断 ID（保留前 12 位 + …） */
export function truncateId(id: string, max = 12): string {
  if (!id) return '—';
  return id.length > max ? `${id.slice(0, max)}…` : id;
}

/** 根据百分比返回告警级别（<80% normal / 80-95% yellow / >95% red） */
export function getAlertLevel(percentage: number): 'normal' | 'yellow' | 'red' {
  if (percentage >= 95) return 'red';
  if (percentage >= 80) return 'yellow';
  return 'normal';
}

/** 根据告警级别返回主色 CSS 变量 */
export function alertLevelColor(level: 'normal' | 'yellow' | 'red'): string {
  switch (level) {
    case 'red':
      return 'var(--state-error)';
    case 'yellow':
      return 'var(--state-warning)';
    default:
      return 'var(--state-success)';
  }
}

/** ISO 时间格式化为相对时间 */
export function formatRelative(iso: string | null): string {
  if (!iso) return '暂无';
  const date = new Date(iso);
  if (isNaN(date.getTime())) return '—';
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 0) return '刚刚';
  const diffMin = Math.floor(diffMs / 60000);
  const diffHour = Math.floor(diffMs / 3600000);
  const diffDay = Math.floor(diffMs / 86400000);
  if (diffMin < 1) return '刚刚';
  if (diffMin < 60) return `${diffMin} 分钟前`;
  if (diffHour < 24) return `${diffHour} 小时前`;
  if (diffDay < 30) return `${diffDay} 天前`;
  return date.toLocaleDateString('zh-CN');
}
