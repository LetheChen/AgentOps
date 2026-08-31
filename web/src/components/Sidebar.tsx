import React from 'react';

export interface AgentInfo { id: string; name: string; harness: string; system_prompt?: string }
export interface WorkflowInfo { workflow_id: string; name: string; nodes: number }

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  agents: AgentInfo[];
  workflows: WorkflowInfo[];
  harnesses: string[];
  onSelectWorkflow: (id: string) => void;
  selectedWorkflow: string;
}

export default function Sidebar({ collapsed, onToggle, agents, workflows, harnesses, onSelectWorkflow, selectedWorkflow }: SidebarProps) {
  const [tab, setTab] = React.useState<'agents' | 'workflows' | 'harnesses'>('workflows');

  if (collapsed) {
    return (
      <div className="sidebar sidebar-collapsed">
        <button className="sidebar-toggle" onClick={onToggle} title="展开配置面板">☰</button>
      </div>
    );
  }

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <span>⚙ 配置</span>
        <button className="sidebar-toggle" onClick={onToggle}>✕</button>
      </div>
      <div className="sidebar-tabs">
        {(['workflows', 'agents', 'harnesses'] as const).map((t) => (
          <button key={t} className={`sidebar-tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
            {t === 'workflows' ? '📋 工作流' : t === 'agents' ? '🤖 智能体' : '🔧 Harness'}
          </button>
        ))}
      </div>
      <div className="sidebar-body">
        {tab === 'workflows' && (
          <ul className="sidebar-list">
            {workflows.map((w) => (
              <li key={w.workflow_id} className={w.workflow_id === selectedWorkflow ? 'active' : ''} onClick={() => onSelectWorkflow(w.workflow_id)}>
                <span className="sidebar-item-name">{w.name}</span>
                <span className="sidebar-item-meta">{w.nodes} 节点</span>
              </li>
            ))}
          </ul>
        )}
        {tab === 'agents' && (
          <ul className="sidebar-list">
            {agents.map((a) => (
              <li key={a.id}>
                <span className="sidebar-item-name">{a.name}</span>
                <span className="sidebar-item-meta">{a.harness}</span>
              </li>
            ))}
          </ul>
        )}
        {tab === 'harnesses' && (
          <ul className="sidebar-list">
            {harnesses.map((h) => (
              <li key={h}>
                <span className="sidebar-item-name">{h}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
