import type { PageId } from '../App';
import { UsagePanel } from '../components/monitor/UsagePanel';
import { AgentStatusGrid } from '../components/monitor/AgentStatusGrid';
import { TipsStream } from '../components/monitor/TipsStream';
import { useMonitorSSE } from '../hooks/useMonitorSSE';

interface MonitorCenterProps {
  onNavigate: (page: PageId) => void;
}

// 监控中心整体样式
const MONITOR_STYLE = `
.monitor-center {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 100%;
}

.section-header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.section-header-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary);
}

/* 顶部：用量区域（左汇总卡按内容紧凑 + 右热力图吃剩余宽度，两卡高度对齐） */
.monitor-usage-row {
  display: grid;
  grid-template-columns: minmax(360px, 1fr) auto;
  gap: 12px;
  align-items: stretch;
}
.monitor-usage-summary,
.monitor-usage-heat {
  padding: 12px 14px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: border-color 0.15s ease;
  outline: none;
  min-width: 0;
  min-height: 170px;
}
.monitor-usage-summary {
  cursor: pointer;
}
.monitor-usage-summary:hover { border-color: var(--color-primary); }
.monitor-usage-summary:focus-visible { border-color: var(--color-primary); }
.monitor-loading-inline { padding: 16px; text-align: center; color: var(--color-text-tertiary); font-size: 12px; }
.monitor-quota-error-inline { font-size: 12px; color: var(--state-error); }

.monitor-usage-summary-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.monitor-usage-kpi-label-title { font-size: 13px; font-weight: 600; color: var(--color-text-primary); white-space: nowrap; }

/* KPI 三联（紧凑版：大数字 + 副标，竖分割线分组） */
.monitor-usage-kpi-row {
  display: flex;
  align-items: stretch;
  gap: 10px;
  padding: 4px 0 2px;
}
.monitor-usage-kpi-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.monitor-usage-kpi-huge {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text-primary);
  line-height: 1.15;
  letter-spacing: -0.01em;
}
.monitor-usage-kpi-sub { font-size: 10px; color: var(--color-text-tertiary); white-space: nowrap; }
.monitor-usage-kpi-divider {
  width: 1px;
  align-self: stretch;
  background: var(--color-border-subtle);
}

/* 二级 KPI（活跃天数 / 日均 / 峰值当日） */
.monitor-usage-sub-kpi {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--color-text-secondary);
  padding: 2px 0;
}
.monitor-usage-sub-kpi b { font-weight: 600; color: var(--color-text-primary); }
.monitor-usage-sub-kpi .dot {
  display: inline-block;
  width: 2px; height: 2px;
  border-radius: 50%;
  background: var(--color-text-tertiary);
  opacity: 0.6;
}

/* 日趋势 sparkline（横向条形，container 宽度自适应） */
.monitor-usage-spark {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}
.monitor-usage-spark-label {
  font-size: 10px;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
  white-space: nowrap;
}
.monitor-usage-spark-svg {
  flex: 1;
  width: 100%;
  height: 22px;
  display: block;
}

.monitor-usage-heat-sub { font-size: 11px; color: var(--color-text-tertiary); }
.monitor-usage-ranges { display: flex; gap: 3px; }
.monitor-usage-range-btn { padding: 2px 7px; font-size: 11px; border: 1px solid var(--color-border-subtle); border-radius: var(--radius-sm); background: var(--color-bg-base); color: var(--color-text-secondary); cursor: pointer; transition: all 0.15s ease; }
.monitor-usage-range-btn:hover { border-color: var(--color-primary); color: var(--color-text-primary); }
.monitor-usage-range-btn.active { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }
.monitor-usage-loading { font-size: 11px; color: var(--color-text-tertiary); }

/* 热力图面板（内容自适应宽度，格子固定小尺寸控制高度） */
.monitor-usage-heat-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
/* paddingTop 给月份标签留出垂直空间（月份 text 渲染在 y=-4 上方），避免撞到 head */
.monitor-usage-heat-body { display: flex; flex-direction: column; align-items: stretch; gap: 6px; padding-top: 14px; flex: 1; }
.monitor-usage-heat-svg-wrap { display: flex; gap: 5px; align-items: flex-start; justify-content: flex-end; }
.monitor-usage-heat-weekdays { display: grid; flex-shrink: 0; }
.monitor-usage-heat-weekday { font-size: 10px; color: var(--color-text-tertiary); text-align: center; width: 10px; }

/* 热力图底部紧凑汇总：3 个统计数字一行 */
.monitor-usage-heat-stats {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--color-text-secondary);
  padding-top: 4px;
  border-top: 1px solid var(--color-border-subtle);
  margin-top: 2px;
  flex-wrap: wrap;
}
.monitor-usage-heat-stats b { font-weight: 700; color: var(--color-text-primary); font-family: var(--font-mono, monospace); }
.monitor-usage-heat-stats i { font-style: normal; color: var(--color-text-tertiary); margin-left: 2px; }
.monitor-usage-heat-stats .sep {
  display: inline-block;
  width: 1px; height: 10px;
  background: var(--color-border-subtle);
  margin: 0 4px;
}
.monitor-usage-heat-legend { display: flex; align-items: center; justify-content: flex-end; gap: 4px; font-size: 10px; color: var(--color-text-tertiary); }

/* 额度列表（全部 provider，纵向排列，每行：名 + 进度 + 百分比） */
.monitor-usage-quota-list { display: flex; flex-direction: column; gap: 4px; padding-top: 6px; border-top: 1px solid var(--color-border-subtle); margin-top: auto; }
.monitor-usage-quota-row { display: grid; grid-template-columns: minmax(72px, 1fr) 1fr 52px; align-items: center; gap: 8px; }
.monitor-usage-quota-name { font-size: 11px; color: var(--color-text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.monitor-usage-quota-track { height: 4px; background: var(--color-bg-base); border-radius: var(--radius-full); overflow: hidden; }
.monitor-usage-quota-fill { display: block; height: 100%; border-radius: var(--radius-full); transition: width 0.3s ease; }
.monitor-usage-quota-pct { font-size: 11px; font-weight: 600; text-align: right; }
.monitor-usage-quota-reset { display: none; }

/* 右侧：热力图容器（vertical flex 让内部元素都能精确占位） */
.monitor-usage-heat { display: flex; flex-direction: column; gap: 10px; }
.monitor-usage-heat-section { display: flex; flex-direction: column; gap: 6px; flex: 1; }
.monitor-usage-agents-section { display: flex; flex-direction: column; gap: 6px; flex: 1; min-height: 0; }
.monitor-usage-agent-table { display: none; }

@media (max-width: 800px) {
  .monitor-usage-row { grid-template-columns: 1fr; }
}

/* Agent 卡片网格 */
.monitor-agent-grid-wrap { padding: 14px 18px; flex: 1; }
.monitor-agent-summary { display: flex; align-items: center; gap: 6px; }
.monitor-agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin-top: 12px; }

/* Agent 卡片旁气泡（右上角，任务执行状态） */
.monitor-agent-card-wrap { position: relative; }
.monitor-card-bubble {
  position: absolute;
  top: -8px;
  right: -8px;
  z-index: 50;
  display: grid;
  grid-template-columns: 18px 1fr;
  gap: 6px;
  align-items: start;
  padding: 8px 10px;
  min-width: 180px;
  max-width: 260px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-subtle);
  border-left: 3px solid;
  border-radius: var(--radius-md);
  box-shadow: 0 4px 14px rgba(0,0,0,0.25);
  animation: monitor-bubble-in 0.2s ease;
  pointer-events: none;
}
.monitor-card-bubble-icon { font-size: 13px; line-height: 1.3; }
.monitor-card-bubble-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.monitor-card-bubble-title { font-size: 11px; font-weight: 600; color: var(--color-text-primary); }
.monitor-card-bubble-msg { font-size: 10px; color: var(--color-text-secondary); line-height: 1.3; word-break: break-word; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.monitor-card-bubble-info { border-left-color: var(--color-primary); }
.monitor-card-bubble-warning { border-left-color: var(--state-warning); }
.monitor-card-bubble-error { border-left-color: var(--state-error); }
.monitor-card-bubble-success { border-left-color: var(--state-success); }

@keyframes monitor-bubble-in {
  from { opacity: 0; transform: translateY(6px) scale(0.95); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

/* Agent 卡片本体 */
.monitor-agent-card {
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  padding: 12px;
  display: flex; flex-direction: column; gap: 10px;
  transition: border-color 0.2s ease;
}
.monitor-agent-card.running { border-color: var(--state-warning); }
.monitor-agent-card.error { border-color: var(--state-error); }
.monitor-agent-top { display: flex; justify-content: space-between; align-items: center; }
.monitor-agent-idblock { display: flex; align-items: center; gap: 6px; }
.monitor-agent-name { font-size: 13px; font-weight: 600; color: var(--color-text-primary); }
.monitor-agent-badges { display: flex; gap: 4px; }
.monitor-harness-badge { font-size: 10px; padding: 2px 6px; border-radius: var(--radius-sm); font-family: var(--font-mono, monospace); border: 1px solid var(--color-border-subtle); background: var(--color-bg-elevated); color: var(--color-text-secondary); }
.monitor-harness-codex { background: rgba(59,130,246,0.12); color: var(--color-primary); border-color: rgba(59,130,246,0.3); }
.monitor-harness-opencode { background: rgba(16,185,129,0.12); color: var(--state-success); border-color: rgba(16,185,129,0.3); }
.monitor-harness-claude-code { background: rgba(251,191,36,0.12); color: var(--state-warning); border-color: rgba(251,191,36,0.3); }
.monitor-harness-local-llm { background: rgba(96,165,250,0.12); color: var(--color-primary-soft); border-color: rgba(96,165,250,0.3); }
.monitor-harness-deterministic { background: rgba(148,163,184,0.12); color: var(--color-text-tertiary); border-color: rgba(148,163,184,0.3); }
.monitor-harness-kimi { background: rgba(168,85,247,0.12); color: #A855F7; border-color: rgba(168,85,247,0.3); }
.monitor-harness-http { background: rgba(59,130,246,0.12); color: var(--color-primary); border-color: rgba(59,130,246,0.3); }
.monitor-harness-default { background: var(--color-bg-elevated); color: var(--color-text-secondary); }
.monitor-agent-id-row { display: flex; justify-content: space-between; align-items: center; margin-top: -4px; }
.monitor-agent-id { font-size: 11px; color: var(--color-text-tertiary); }
.monitor-agent-task { display: flex; flex-direction: column; gap: 4px; padding: 8px; background: var(--color-bg-elevated); border-radius: var(--radius-sm); font-size: 12px; }
.monitor-agent-task-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.monitor-task-label { font-size: 10px; color: var(--color-text-tertiary); text-transform: uppercase; letter-spacing: 0.04em; }
.monitor-task-runid, .monitor-task-flow, .monitor-task-elapsed { font-size: 11px; color: var(--color-text-primary); }
.monitor-task-node { display: flex; align-items: center; gap: 4px; }
.monitor-task-node-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--state-warning); animation: monitor-pulse 1.5s ease-in-out infinite; }
.monitor-agent-idle { padding: 10px 8px; text-align: center; }
.monitor-agent-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; padding-top: 8px; border-top: 1px solid var(--color-border-subtle); }
.monitor-stat-item { display: flex; flex-direction: column; gap: 2px; align-items: flex-start; }
.monitor-stat-label { font-size: 10px; color: var(--color-text-tertiary); }
.monitor-stat-value { font-size: 12px; color: var(--color-text-primary); font-weight: 500; }
.monitor-stat-time .monitor-stat-value { font-size: 11px; color: var(--color-text-secondary); font-family: var(--font-mono, monospace); }

/* Tips Stream（底部告警滚动栏） */
.monitor-tips-stream { padding: 10px 14px; min-height: 100px; max-height: 150px; overflow: hidden; display: flex; flex-direction: column; }
.monitor-tips-meta { display: flex; align-items: center; gap: 6px; }
.monitor-tips-list { list-style: none; padding: 0; margin: 6px 0 0 0; display: flex; flex-direction: column; gap: 4px; overflow-y: auto; flex: 1; }
.monitor-tip-item { display: grid; grid-template-columns: 8px 1fr auto; gap: 8px; align-items: center; padding: 3px 0; font-size: 12px; }
.monitor-tip-dot { width: 8px; height: 8px; border-radius: 50%; }
.monitor-tip-dot-info { background: var(--color-primary); }
.monitor-tip-dot-warning { background: var(--state-warning); }
.monitor-tip-dot-error { background: var(--state-error); }
.monitor-tip-dot-success { background: var(--state-success); }
.monitor-tip-content { display: flex; flex-direction: column; gap: 1px; overflow: hidden; }
.monitor-tip-title { font-size: 12px; font-weight: 500; color: var(--color-text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.monitor-tip-message { font-size: 11px; color: var(--color-text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.monitor-tip-time { font-size: 11px; color: var(--color-text-tertiary); white-space: nowrap; }

@keyframes monitor-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(251,191,36,0.4); }
  50% { box-shadow: 0 0 0 4px rgba(251,191,36,0); }
}
`;

export function MonitorCenter({ onNavigate }: MonitorCenterProps) {
  // 顶层单一 SSE：lastTipByAgent 路由到对应 AgentCard（卡片旁气泡），alertTips 归底部滚动栏
  const { alertTips, lastTipByAgent, connected } = useMonitorSSE();

  const handleViewRun = () => {
    onNavigate('chat');
  };

  return (
    <div className="monitor-center">
      <style>{MONITOR_STYLE}</style>

      {/* 顶部：用量与额度汇总卡片（固定高度，点击穿透多维度明细） */}
      <UsagePanel />

      {/* 主区域：Agent 状态卡片（独占中部，每个卡片右上角弹任务执行气泡） */}
      <AgentStatusGrid lastTipByAgent={lastTipByAgent} />

      {/* 底部：告警类 tips 滚动列表（patrol_alert / quota_warning / validation_result） */}
      <TipsStream tips={alertTips} connected={connected} onViewRun={handleViewRun} />
    </div>
  );
}

export default MonitorCenter;
