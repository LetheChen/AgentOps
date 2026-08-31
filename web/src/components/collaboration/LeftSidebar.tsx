import type { CollaborationGraph } from '../../lib/types';

interface LeftSidebarProps {
  graphData: CollaborationGraph | null;
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
}

/**
 * LeftSidebar — 左侧栏
 *
 * 原型 swimlane-v2.html 左侧栏：
 *  - 📚 Skill 模板（占位：当前 run 的 workflow）
 *  - 🎯 业务角色（按 lane 列出，可点击跳到节点）
 *  - 🛡️ 隔离（agent / 知识库 / 工具 / harness 汇总）
 */
export function LeftSidebar({ graphData, selectedNodeId, onSelectNode }: LeftSidebarProps) {
  const lanes = graphData?.lanes || [];
  const nodes = graphData?.nodes || [];

  // 统计：去重的 agent / harness
  const agents = Array.from(new Set(nodes.map((n) => n.agent_id).filter(Boolean)));
  const harnesses = Array.from(new Set(nodes.map((n) => n.harness).filter(Boolean)));

  return (
    <div style={{
      background: 'var(--panel, #131c2e)',
      borderRight: '1px solid var(--border, #243049)',
      padding: 16, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 12,
    }}>
      {/* Skill 模板 */}
      <SectionTitle>📚 当前工作流</SectionTitle>
      {graphData && graphData.workflow_id ? (
        <div style={{
          padding: '10px 12px',
          background: 'rgba(59,130,246,.15)', border: '1px solid var(--accent, #3b82f6)',
          borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>
          <div>📋 {graphData.workflow_id}</div>
          <div style={{ fontSize: 10, color: '#8b97b0', marginTop: 3 }}>
            {nodes.length} 节点 · {lanes.length} 业务角色 · 状态: {graphData.status}
          </div>
        </div>
      ) : (
        <div style={{
          padding: '10px 12px', background: 'rgba(255,255,255,.03)',
          border: '1px solid var(--border, #243049)', borderRadius: 6, fontSize: 12,
          color: '#8b97b0',
        }}>
          对话 session（无 workflow）
        </div>
      )}

      {/* 业务角色 */}
      <SectionTitle style={{ marginTop: 24 }}>🎯 业务角色</SectionTitle>
      {lanes.length === 0 ? (
        <div style={{ fontSize: 11, color: '#8b97b0' }}>暂无业务角色</div>
      ) : (
        lanes.map((lane) => {
          const isActive = lane.nodes.includes(selectedNodeId || '');
          return (
            <div
              key={lane.business_role}
              onClick={() => lane.nodes[0] && onSelectNode(lane.nodes[0])}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '8px 10px', borderRadius: 6, fontSize: 12,
                cursor: 'pointer',
                background: isActive ? 'rgba(59,130,246,.10)' : 'transparent',
                borderLeft: `3px solid ${lane.color}`,
                transition: 'all .15s',
              }}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,.03)'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = isActive ? 'rgba(59,130,246,.10)' : 'transparent'; }}
            >
              <span style={{
                width: 10, height: 10, borderRadius: '50%', background: lane.color,
              }} />
              <span style={{ flex: 1 }}>{lane.business_role}</span>
              <span style={{ fontSize: 10, color: '#8b97b0' }}>
                {lane.nodes.length} 节点
              </span>
            </div>
          );
        })
      )}

      {/* 隔离 */}
      <SectionTitle style={{ marginTop: 24 }}>🛡️ 隔离配置</SectionTitle>
      <div style={{ fontSize: 11, color: '#8b97b0', lineHeight: 1.7 }}>
        {agents.length > 0 && (
          <div>Agent：<span style={{ color: '#e6ecf5' }}>{agents.join(', ')}</span></div>
        )}
        {harnesses.length > 0 && (
          <div>Harness：<span style={{ color: '#e6ecf5' }}>{harnesses.join(', ')}</span></div>
        )}
        <div style={{ marginTop: 6, color: '#10b981' }}>
          ✓ business_role 在 node 级声明
        </div>
        <div style={{ color: '#10b981' }}>✓ agent/harness/model 按节点隔离</div>
      </div>
    </div>
  );
}

function SectionTitle({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <h3 style={{
      fontSize: 11, textTransform: 'uppercase', color: '#8b97b0',
      margin: 0, marginBottom: 12, letterSpacing: 0.5, fontWeight: 600,
      ...style,
    }}>
      {children}
    </h3>
  );
}