import type { Tip } from '../../lib/api';

const LIST_VISIBLE_COUNT = 6; // 底部滚动列表展示最近 6 条

interface TipsStreamProps {
  /** 从父级（MonitorCenter 顶层 SSE）传入的 tips 列表 */
  tips: Tip[];
  /** SSE 连接状态（用于展示实时流指示） */
  connected?: boolean;
  /** 点击 tip 项：跳转到运行记录 */
  onViewRun?: () => void;
}

/**
 * 动态 tips 滚动列表（底部）
 * - 自动弹出的气泡在全局右上角 TipsToasts（类 codex 宠物），本组件只保留底部滚动历史列表
 * - tips 数据由 MonitorCenter 顶层 SSE 统一分发，避免多连接
 */
export function TipsStream({ tips, connected, onViewRun }: TipsStreamProps) {
  const recentList = tips.slice(0, LIST_VISIBLE_COUNT);

  return (
    <div className="monitor-tips-stream card">
      <div className="section-header-row">
        <span className="section-header-title">动态提示</span>
        <div className="monitor-tips-meta">
          <span
            className={`status-dot ${connected ? 'status-dot-success' : 'status-dot-error'}`}
            style={connected ? { animation: 'monitor-pulse 2s ease-in-out infinite' } : undefined}
          />
          <span style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>
            {connected ? '实时流' : '未连接'}
          </span>
        </div>
      </div>
      {recentList.length === 0 ? (
        <div className="widget-empty-state" style={{ padding: '12px' }}>
          暂无提示事件
        </div>
      ) : (
        <ul className="monitor-tips-list">
          {recentList.map((tip) => (
            <li
              key={tip.id}
              className={`monitor-tip-item monitor-tip-${tip.severity}`}
              onClick={onViewRun}
              role={onViewRun ? 'button' : undefined}
              tabIndex={onViewRun ? 0 : undefined}
            >
              <span className={`monitor-tip-dot monitor-tip-dot-${tip.severity}`} />
              <div className="monitor-tip-content">
                <span className="monitor-tip-title">{tip.title}</span>
                <span className="monitor-tip-message">{tip.message}</span>
              </div>
              <span className="font-mono monitor-tip-time">
                {new Date(tip.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
