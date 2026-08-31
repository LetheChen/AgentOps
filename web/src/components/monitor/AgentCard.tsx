import { useState, useEffect, useMemo, useRef } from 'react';
import type { AgentStatus, Tip } from '../../lib/api';
import { formatElapsed, truncateId, formatRelative } from './utils';

/** harness 徽章颜色映射 */
const HARNESS_BADGE_CLASS: Record<string, string> = {
  codex: 'monitor-harness-codex',
  opencode: 'monitor-harness-opencode',
  claude_code: 'monitor-harness-claude-code',
  local_llm: 'monitor-harness-local-llm',
  deterministic: 'monitor-harness-deterministic',
  kimi: 'monitor-harness-kimi',
  http: 'monitor-harness-http',
};

function harnessBadgeClass(harness: string): string {
  return HARNESS_BADGE_CLASS[harness] ?? 'monitor-harness-default';
}

/** 状态灯颜色 */
const STATUS_DOT_CLASS: Record<AgentStatus['status'], string> = {
  idle: 'status-dot status-dot-success',
  running: 'status-dot status-dot-warning',
  error: 'status-dot status-dot-error',
};

const STATUS_PILL_CLASS: Record<AgentStatus['status'], string> = {
  idle: 'status-pill status-pill-success',
  running: 'status-pill status-pill-warning',
  error: 'status-pill status-pill-error',
};

const STATUS_LABEL: Record<AgentStatus['status'], string> = {
  idle: '空闲',
  running: '运行中',
  error: '异常',
};

/** 气泡 severity 样式 */
const BUBBLE_CLASS: Record<Tip['severity'], string> = {
  info: 'monitor-card-bubble-info',
  warning: 'monitor-card-bubble-warning',
  error: 'monitor-card-bubble-error',
  success: 'monitor-card-bubble-success',
};

/** 气泡图标 */
const BUBBLE_ICON: Record<Tip['type'], string> = {
  task_started: '🚀',
  task_progress: '💭',
  task_completed: '✅',
  task_failed: '❌',
  patrol_alert: '⚠️',
  validation_result: '🎬',
  quota_warning: '📊',
};

interface AgentCardProps {
  agent: AgentStatus;
  /** 该 agent 最新的任务执行 tip（变化时卡片右上角弹气泡） */
  lastTip?: Tip | null;
}

/** 单个 Agent 卡片（含右上角任务执行气泡） */
export function AgentCard({ agent, lastTip }: AgentCardProps) {
  const [now, setNow] = useState(() => Date.now());
  // 当前显示的气泡（单条，新的覆盖旧的）
  const [bubble, setBubble] = useState<Tip | null>(null);
  // StrictMode 双调用守护
  const processedIdsRef = useRef<Set<string>>(new Set());

  // running 状态每秒刷新 elapsed
  useEffect(() => {
    if (agent.status !== 'running' || !agent.current_task) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [agent.status, agent.current_task]);

  // 监听 lastTip：匹配 agent_id 时弹气泡
  useEffect(() => {
    if (!lastTip) return;
    if (lastTip.agent_id !== agent.agent_id) return;
    if (processedIdsRef.current.has(lastTip.id)) return;
    processedIdsRef.current.add(lastTip.id);

    setBubble(lastTip);

    // 自动消失：error 5s，其余 3.5s
    const delay = lastTip.severity === 'error' ? 5000 : 3500;
    const timer = window.setTimeout(() => {
      setBubble((cur) => (cur?.id === lastTip.id ? null : cur));
    }, delay);
    return () => window.clearTimeout(timer);
  }, [lastTip, agent.agent_id]);

  // 清理 processedIds
  useEffect(() => {
    if (processedIdsRef.current.size > 80) {
      processedIdsRef.current = new Set(Array.from(processedIdsRef.current).slice(-40));
    }
  }, [bubble]);

  const task = agent.current_task;
  const elapsed = useMemo(() => {
    if (!task) return '';
    return formatElapsed(task.started_at, new Date(now));
  }, [task, now]);

  return (
    <div className="monitor-agent-card-wrap">
      {/* 卡片右上角气泡（任务执行状态：开始/思考/完成/失败） */}
      {bubble && (
        <div className={`monitor-card-bubble ${BUBBLE_CLASS[bubble.severity]}`}>
          <span className="monitor-card-bubble-icon">{BUBBLE_ICON[bubble.type] ?? '💬'}</span>
          <div className="monitor-card-bubble-body">
            <span className="monitor-card-bubble-title">{bubble.title}</span>
            {bubble.message && <span className="monitor-card-bubble-msg">{bubble.message}</span>}
          </div>
        </div>
      )}

      <div className={`monitor-agent-card ${agent.status}`}>
        {/* 顶部：状态灯 + 名称 + harness 徽章 */}
        <div className="monitor-agent-top">
          <div className="monitor-agent-idblock">
            <span className={STATUS_DOT_CLASS[agent.status]} />
            <span className="monitor-agent-name">{agent.display_name || agent.agent_id}</span>
          </div>
          <div className="monitor-agent-badges">
            <span className={`monitor-harness-badge ${harnessBadgeClass(agent.harness)}`}>{agent.harness}</span>
          </div>
        </div>
        <div className="monitor-agent-id-row">
          <span className="font-mono monitor-agent-id">{agent.agent_id}</span>
          <span className={`status-pill ${STATUS_PILL_CLASS[agent.status]}`} style={{ fontSize: '10px' }}>
            {STATUS_LABEL[agent.status]}
          </span>
        </div>

        {/* 中部：当前任务块（仅 running 时展示） */}
        {agent.status === 'running' && task ? (
          <div className="monitor-agent-task">
            <div className="monitor-agent-task-row">
              <span className="monitor-task-label">RUN</span>
              <span className="font-mono monitor-task-runid">{truncateId(task.run_id)}</span>
            </div>
            <div className="monitor-agent-task-row">
              <span className="monitor-task-label">FLOW</span>
              <span className="font-mono monitor-task-flow">{task.workflow_id}</span>
            </div>
            <div className="monitor-agent-task-row">
              <span className="monitor-task-label">NODE</span>
              <span className="monitor-task-node">
                <span className="monitor-task-node-dot" />
                <span className="font-mono">{task.current_node}</span>
              </span>
            </div>
            <div className="monitor-agent-task-row">
              <span className="monitor-task-label">已耗时</span>
              <span className="font-mono monitor-task-elapsed">{elapsed}</span>
            </div>
          </div>
        ) : (
          <div className="monitor-agent-idle">
            <span style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>
              {agent.status === 'error' ? '最近一次运行异常' : '空闲中'}
            </span>
          </div>
        )}

        {/* 底部：累计 stats */}
        <div className="monitor-agent-stats">
          <div className="monitor-stat-item">
            <span className="monitor-stat-label">总运行</span>
            <span className="font-mono monitor-stat-value">{agent.stats.total_runs}</span>
          </div>
          <div className="monitor-stat-item">
            <span className="monitor-stat-label" style={{ color: 'var(--state-success)' }}>完成</span>
            <span className="font-mono monitor-stat-value">{agent.stats.completed}</span>
          </div>
          <div className="monitor-stat-item">
            <span className="monitor-stat-label" style={{ color: 'var(--state-error)' }}>失败</span>
            <span className="font-mono monitor-stat-value">{agent.stats.failed}</span>
          </div>
          <div className="monitor-stat-item monitor-stat-time">
            <span className="monitor-stat-label">最近</span>
            <span className="monitor-stat-value">{formatRelative(agent.stats.last_run_at)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
