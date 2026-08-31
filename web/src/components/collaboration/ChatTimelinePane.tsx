import { useMemo } from 'react';
import type { CollaborationGraph, TimelineEntry } from '../../lib/types';

interface ChatTimelinePaneProps {
  graphData: CollaborationGraph | null;
  timelineData: TimelineEntry[];
  runId: string | null;
}

/**
 * ChatTimelinePane — 对话时间线（业务视角）
 *
 * 原型 §paneChat：5 种消息类型
 *  1. 系统分隔（"── 工作流「xxx」已启动 ──" / "✅ 工作流已完成"）
 *  2. 用户消息（avatar + bubble）
 *  3. Agent 回复（avatar + bubble）
 *  4. 节点状态卡片（started/completed/failed/skipped，含 business_role + 耗时 + token）
 *  5. Handoff 气泡（from → to + summary + port + size）
 *  6. Widget 产物内联（按 type 简化展示）
 */
export function ChatTimelinePane({ graphData, timelineData, runId: _runId }: ChatTimelinePaneProps) {
  // ─── 把 timeline 序列化为 ChatPanel 风格的消息列表 ───
  const messages = useMemo(() => buildChatMessages(graphData, timelineData), [graphData, timelineData]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: 'var(--panel, #131c2e)' }}>
      {/* header bar */}
      <div style={{
        padding: '10px 16px', borderBottom: '1px solid var(--border, #243049)',
        fontSize: 12, color: '#8b97b0', display: 'flex',
        justifyContent: 'space-between', alignItems: 'center',
      }}>
        <span>
          <span style={{ color: '#e6ecf5', fontWeight: 500 }}>💬 对话 · 协作时间线</span>
          {' '}· 5 种消息类型：用户 / Agent / 状态卡片 / Handoff 气泡 / 产物内联
        </span>
        <span>共 {messages.length} 条消息</span>
      </div>

      {/* messages */}
      <div style={{
        flex: 1, overflowY: 'auto', padding: 12,
        display: 'flex', flexDirection: 'column', gap: 6,
      }}>
        {messages.length === 0 && (
          <div style={{ padding: 40, textAlign: 'center', color: '#8b97b0', fontSize: 13 }}>
            该 run 暂无事件，请选择另一个 run 查看
          </div>
        )}
        {messages.map((m) => (
          <MessageRow key={m.key} msg={m} />
        ))}
      </div>

      {/* input bar (disabled demo) */}
      <div style={{
        padding: '10px 16px', borderTop: '1px solid var(--border, #243049)',
        display: 'flex', gap: 8,
      }}>
        <input
          placeholder="输入消息，Enter 发送..."
          disabled
          style={{
            flex: 1, padding: '8px 12px', fontSize: 12,
            background: 'var(--panel-2, #1a2440)', border: '1px solid var(--border, #243049)',
            borderRadius: 6, color: '#e6ecf5', opacity: 0.6,
          }}
        />
        <button
          disabled
          style={{
            padding: '8px 14px', fontSize: 12,
            background: 'var(--accent, #3b82f6)', color: '#fff',
            border: 0, borderRadius: 6, opacity: 0.5,
          }}
        >
          ▶
        </button>
      </div>
    </div>
  );
}

type ChatMsg =
  | { kind: 'system'; key: string; text: string; color?: string }
  | { kind: 'user'; key: string; text: string }
  | { kind: 'assistant'; key: string; text: string; agent: string }
  | { kind: 'status'; key: string; status: string; business_role: string; node_id: string; duration_ms?: number | null; token_usage?: number | null; running_sec?: number }
  | { kind: 'handoff'; key: string; from_role: string; to_role: string; summary: string; port: string; payload_size: number }
  | { kind: 'widget'; key: string; source_role: string; widget_type: string; title: string };

function buildChatMessages(g: CollaborationGraph | null, timeline: TimelineEntry[]): ChatMsg[] {
  if (!g) return [];
  const msgs: ChatMsg[] = [];

  // 1. 系统分隔：工作流已启动
  msgs.push({ kind: 'system', key: 'sys-start', text: `── 工作流「${g.workflow_id || '会话'}」已启动 ──` });

  // 2. 用户消息（mock：基于 started_at + initial_inputs 推断）
  msgs.push({ kind: 'user', key: 'user-1', text: '执行工作流' });

  // 3. Manager 回复
  const nodes = g.nodes || [];
  const runningCount = nodes.filter((n) => n.status === 'running').length;
  const completedCount = nodes.filter((n) => n.status === 'completed').length;
  msgs.push({
    kind: 'assistant',
    key: 'agent-mgr',
    agent: 'manager',
    text: `好的，已触发工作流（${g.run_id.slice(-12)}），下面是协作过程：`,
  });

  // 4. 按 timeline 顺序生成节点状态卡片 + handoff
  const nodeMap = new Map(nodes.map((n) => [n.node_id, n]));
  for (const ev of [...timeline].sort((a, b) => a.sequence - b.sequence)) {
    const t = (ev.type || '').toLowerCase();
    if (t === 'node.started') {
      const node = ev.node_id ? nodeMap.get(ev.node_id) : null;
      msgs.push({
        kind: 'status',
        key: `status-${ev.sequence}`,
        status: 'started',
        business_role: node?.business_role || '?',
        node_id: ev.node_id || '',
      });
    } else if (t === 'node.completed' || t === 'node.failed' || t === 'node.skipped') {
      const node = ev.node_id ? nodeMap.get(ev.node_id) : null;
      const status = t === 'node.completed' ? 'completed' : t === 'node.failed' ? 'failed' : 'skipped';
      msgs.push({
        kind: 'status',
        key: `status-${ev.sequence}`,
        status,
        business_role: node?.business_role || '?',
        node_id: ev.node_id || '',
        duration_ms: node?.duration_ms,
        token_usage: node?.token_usage,
      });
    } else if (t === 'node.handoff') {
      const h = (g.handoffs || []).find((x) => x.sequence === ev.sequence);
      if (h) {
        msgs.push({
          kind: 'handoff',
          key: `handoff-${ev.sequence}`,
          from_role: h.from_role || '?',
          to_role: h.to_role || '?',
          summary: h.summary || h.port,
          port: h.port,
          payload_size: h.payload_size,
        });
      }
    } else if (t === 'widget.update') {
      // 简化：widget 类型不可见，记录 source_role 即可
      const node = ev.node_id ? nodeMap.get(ev.node_id) : null;
      msgs.push({
        kind: 'widget',
        key: `widget-${ev.sequence}`,
        source_role: node?.business_role || '?',
        widget_type: 'Widget',
        title: `${node?.display_name || ev.node_id || '?'} 产出更新`,
      });
    }
  }

  // 5. 系统分隔：工作流已完成
  const isRunning = g.status === 'running';
  msgs.push({
    kind: 'system',
    key: 'sys-end',
    text: isRunning
      ? `⟳ 工作流进行中（${completedCount}/${nodes.length} 节点已完成）`
      : `✅ 工作流已完成（${completedCount}/${nodes.length} 节点成功）`,
    color: isRunning ? '#3b82f6' : '#10b981',
  });
  // touch runningCount to avoid lint
  void runningCount;
  return msgs;
}

function MessageRow({ msg }: { msg: ChatMsg }) {
  switch (msg.kind) {
    case 'system':
      return (
        <div style={{
          alignSelf: 'center',
          padding: '6px 14px', borderRadius: 12,
          background: 'rgba(255,255,255,.03)',
          border: `1px solid ${msg.color || 'var(--border, #243049)'}`,
          color: msg.color || '#8b97b0', fontSize: 11, margin: '4px 0',
        }}>
          {msg.text}
        </div>
      );
    case 'user':
      return (
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <div style={{
            width: 28, height: 28, borderRadius: '50%',
            background: 'rgba(59,130,246,.2)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            flexShrink: 0, fontSize: 13,
          }}>👤</div>
          <div style={{
            background: 'rgba(59,130,246,.12)',
            border: '1px solid rgba(59,130,246,.4)',
            borderRadius: 12, padding: '8px 12px',
            maxWidth: '70%', fontSize: 12,
          }}>
            {msg.text}
          </div>
        </div>
      );
    case 'assistant':
      return (
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <div style={{
            width: 28, height: 28, borderRadius: '50%',
            background: 'rgba(139,92,246,.2)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            flexShrink: 0, fontSize: 13,
          }}>🤖</div>
          <div style={{
            background: 'var(--panel-2, #1a2440)',
            border: '1px solid var(--border, #243049)',
            borderRadius: 12, padding: '8px 12px',
            maxWidth: '70%', fontSize: 12,
          }}>
            {msg.text}
          </div>
        </div>
      );
    case 'status': {
      const s = msg.status;
      const cls = ['started', 'completed', 'failed', 'skipped'].includes(s) ? s : 'started';
      const colors: Record<string, { bg: string; border: string; icon: string; iconColor: string }> = {
        started:   { bg: 'rgba(59,130,246,.10)', border: '#3b82f6', icon: '▶', iconColor: '#3b82f6' },
        completed: { bg: 'rgba(16,185,129,.10)', border: '#10b981', icon: '✓', iconColor: '#10b981' },
        failed:    { bg: 'rgba(239,68,68,.12)',  border: '#ef4444', icon: '✗', iconColor: '#ef4444' },
        skipped:   { bg: 'rgba(245,158,11,.10)', border: '#f59e0b', icon: '⊘', iconColor: '#f59e0b' },
      };
      const c = colors[cls];
      return (
        <div style={{
          background: c.bg, border: `1px solid ${c.border}`,
          borderRadius: 8, padding: '8px 12px', fontSize: 12,
          display: 'flex', alignItems: 'center', gap: 8, margin: '2px 0',
        }}>
          <span style={{ fontWeight: 'bold', color: c.iconColor }}>{c.icon}</span>
          <span style={{ color: '#e6ecf5' }}>{msg.business_role} · {msg.node_id}</span>
          <div style={{
            marginLeft: 'auto', display: 'flex', gap: 8, fontSize: 11, color: '#8b97b0',
          }}>
            {msg.running_sec !== undefined ? (
              <span className="duration">⟳ {msg.running_sec}s</span>
            ) : msg.duration_ms != null ? (
              <span className="duration">{(msg.duration_ms / 1000).toFixed(1)}s</span>
            ) : (
              <span style={{ color: '#64748b' }}>...</span>
            )}
            {msg.token_usage != null && (
              <span className="tokens">
                {msg.token_usage > 1000 ? `${(msg.token_usage / 1000).toFixed(1)}k` : msg.token_usage} tok
              </span>
            )}
          </div>
        </div>
      );
    }
    case 'handoff':
      return (
        <div style={{
          background: 'linear-gradient(135deg, rgba(99,102,241,.12), rgba(168,85,247,.12))',
          border: '1px solid rgba(168,85,247,.4)', borderRadius: 14,
          padding: '8px 12px 8px 24px', margin: '6px 0',
          fontSize: 12, position: 'relative',
        }}>
          <span style={{
            position: 'absolute', left: 6, top: '50%', transform: 'translateY(-50%)',
          }}>💬</span>
          <div style={{ color: '#c4b5fd', fontSize: 10, marginBottom: 4 }}>
            {msg.from_role} → {msg.to_role}
          </div>
          <div style={{ color: '#e6ecf5', lineHeight: 1.4 }}>{msg.summary}</div>
          <div style={{
            fontSize: 9, color: '#a5b4fc', marginTop: 4,
            fontFamily: '"SF Mono", Consolas, monospace',
            display: 'flex', justifyContent: 'space-between',
          }}>
            <span>port: {msg.port}</span>
            <span>{msg.payload_size > 1024 ? `${(msg.payload_size / 1024).toFixed(1)}KB` : `${msg.payload_size}B`}</span>
          </div>
        </div>
      );
    case 'widget':
      return (
        <div style={{
          background: 'rgba(139,92,246,.08)',
          border: '1px solid rgba(139,92,246,.4)',
          borderRadius: 8, padding: '8px 12px', fontSize: 12, margin: '6px 0',
        }}>
          <div style={{ fontSize: 10, color: '#8b97b0', marginBottom: 4 }}>
            📦 {msg.source_role} 产出
          </div>
          <div style={{ color: '#e6ecf5', fontWeight: 500 }}>
            {msg.widget_type} · {msg.title}
          </div>
        </div>
      );
  }
}