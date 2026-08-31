import { useState, useEffect, useRef, useCallback } from 'react';
import { apiClient, type AgentStatus, type Tip } from '../../lib/api';
import { AgentCard } from './AgentCard';

const POLL_INTERVAL_MS = 5000; // 5s 轮询

interface AgentStatusGridProps {
  /** 每个 agent 最新的任务执行 tip（按 agent_id 索引，透传给对应 AgentCard 弹气泡） */
  lastTipByAgent?: Record<string, Tip>;
}

/** Agent 卡片网格：从 /api/monitor/agents-status 拉数据（轮询 5s） */
export function AgentStatusGrid({ lastTipByAgent }: AgentStatusGridProps) {
  const [agents, setAgents] = useState<AgentStatus[]>([]);
  const [runningCount, setRunningCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const initializedRef = useRef(false);

  const loadAgents = useCallback(async () => {
    try {
      const data = await apiClient.getAgentsStatus();
      setAgents(data.agents ?? []);
      setRunningCount(data.running_count ?? 0);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Agent 状态加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    loadAgents();
    const timer = window.setInterval(() => loadAgents(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadAgents]);

  return (
    <div className="monitor-agent-grid-wrap card">
      <div className="section-header-row">
        <span className="section-header-title">Agent 状态</span>
        <div className="monitor-agent-summary">
          <span className="status-dot status-dot-warning" />
          <span className="font-mono" style={{ fontSize: '12px' }}>{runningCount} 运行中 / {agents.length} 总计</span>
        </div>
      </div>

      {loading && agents.length === 0 ? (
        <div className="monitor-loading-inline">加载 Agent 状态...</div>
      ) : error && agents.length === 0 ? (
        <div className="monitor-quota-error">
          <span>Agent 状态加载失败：{error}</span>
        </div>
      ) : agents.length === 0 ? (
        <div className="widget-empty-state" style={{ padding: '24px' }}>
          暂无已注册 Agent
        </div>
      ) : (
        <div className="monitor-agent-grid">
          {agents.map((agent) => (
            <AgentCard
              key={agent.agent_id}
              agent={agent}
              lastTip={lastTipByAgent?.[agent.agent_id]}
            />
          ))}
        </div>
      )}
    </div>
  );
}
