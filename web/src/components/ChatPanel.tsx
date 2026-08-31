import React from 'react';
import type { WidgetUpdate } from '../lib/api';
import { NODE_CARD_STYLES } from '../lib/types';

// 协作时间线：5 种消息类型
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'handoff' | 'widget';
  text: string;
  time: string;
  // action button on system messages (e.g. "▶ Run")
  action?: { label: string; onClick: () => void };
  // inline widget card
  widget?: WidgetUpdate;
  // === 协作可视化扩展字段 ===
  // 消息归属节点
  node_id?: string;
  // 业务角色名（消息头部展示）
  business_role?: string;
  // handoff 对话气泡
  handoff?: {
    from_role: string;
    to_role: string;
    port: string;
    payload_size: number;
    summary: string;
  };
  // 节点状态卡片（status='system' + 有 node_status 时使用）
  node_status?: {
    status: 'started' | 'completed' | 'failed' | 'skipped';
    duration_ms?: number;
    tokens?: number;
    error_type?: string;
  };
}

interface ChatPanelProps {
  messages: ChatMessage[];
  running: boolean;
  onSend: (text: string) => void;
  status: string;
}

function formatBytes(b: number): string {
  if (b < 1024) return `${b}B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)}KB`;
  return `${(b / 1024 / 1024).toFixed(1)}MB`;
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + '...' : s;
}

export default function ChatPanel({ messages, running, onSend, status }: ChatPanelProps) {
  const [input, setInput] = React.useState('');
  const listRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages]);

  function handleSend() {
    const text = input.trim();
    if (!text || running) return;
    setInput('');
    onSend(text);
  }

  return (
    <div className="chat-panel">
      <div className="chat-header">
        <span>💬 对话 · 协作时间线</span>
        <span className="chat-status">{status}</span>
      </div>
      <div className="chat-messages" ref={listRef}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <strong>Agent Platform v2.1</strong>
            <p>输入自然语言，主 Agent 解析意图后自动执行 DAG 工作流</p>
            <p className="chat-hint">试试: "帮我审一张差旅报销单" 或 "跑一次 hello-world"</p>
          </div>
        )}
        {messages.map((msg) => <ChatMessageItem key={msg.id} msg={msg} />)}
      </div>
      <div className="chat-input-bar">
        <input
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
          placeholder={running ? '执行中...' : '输入消息, Enter 发送...'}
          disabled={running}
        />
        <button className="chat-send-btn" onClick={handleSend} disabled={running || !input.trim()}>
          ▶
        </button>
      </div>
    </div>
  );
}

// 消息分发渲染
function ChatMessageItem({ msg }: { msg: ChatMessage }) {
  switch (msg.role) {
    case 'handoff':
      return <HandoffBubble msg={msg} />;
    case 'widget':
      return <InlineWidgetBubble msg={msg} />;
    case 'system':
      // 节点状态卡片（带 node_status）或一般系统消息
      return msg.node_status ? <NodeStatusCard msg={msg} /> : <SystemBubble msg={msg} />;
    default:
      return <GenericBubble msg={msg} />;
  }
}

// 用户/Agent 通用气泡
function GenericBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === 'user';
  return (
    <div className={`chat-msg chat-msg-${msg.role}`}>
      <div className="chat-msg-meta">
        <span className="chat-msg-role">{isUser ? '👤' : msg.role === 'assistant' ? '🤖' : '⚡'}</span>
        <span className="chat-msg-time">{msg.time}</span>
      </div>
      <div className="chat-msg-body">{msg.text}</div>
      {msg.action && (
        <button className="chat-msg-action" onClick={msg.action.onClick}>{msg.action.label}</button>
      )}
    </div>
  );
}

// 节点状态卡片（业务角色 + 节点名 + 状态色 + 耗时 + token + error_type）
function NodeStatusCard({ msg }: { msg: ChatMessage }) {
  const s = msg.node_status!;
  const style = NODE_CARD_STYLES[s.status] || NODE_CARD_STYLES.started;
  const header = msg.business_role
    ? `${msg.business_role} · ${msg.node_id || ''}`
    : `节点「${msg.node_id || ''}」`;
  return (
    <div className="chat-msg chat-msg-system">
      <div
        style={{
          background: style.bg,
          border: `1px solid ${style.border}`,
          borderRadius: 8,
          padding: '8px 12px',
          margin: '4px 0',
          fontSize: 12,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <span style={{ color: style.border, fontWeight: 'bold' }}>{style.icon}</span>
        <span style={{ color: '#E2E8F0' }}>{header}</span>
        {s.duration_ms != null && (
          <span style={{ color: '#94A3B8', marginLeft: 'auto' }}>
            {(s.duration_ms / 1000).toFixed(1)}s
          </span>
        )}
        {s.tokens != null && s.tokens > 0 && (
          <span style={{ color: '#3B82F6' }}>
            {s.tokens > 1000 ? `${(s.tokens / 1000).toFixed(1)}k` : s.tokens} tok
          </span>
        )}
        {s.error_type && (
          <span style={{ color: '#EF4444', fontFamily: 'monospace', fontSize: 10 }}>
            {s.error_type}
          </span>
        )}
      </div>
    </div>
  );
}

// Handoff 对话气泡（跨业务角色协作的语义摘要）
function HandoffBubble({ msg }: { msg: ChatMessage }) {
  const h = msg.handoff!;
  const [expanded, setExpanded] = React.useState(false);
  return (
    <div className="chat-msg chat-msg-handoff">
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          background: 'linear-gradient(135deg, rgba(99,102,241,.12), rgba(168,85,247,.12))',
          border: '1px solid rgba(168,85,247,.4)',
          borderRadius: 14,
          padding: '8px 12px 8px 28px',
          margin: '6px 0',
          fontSize: 12,
          position: 'relative',
          cursor: 'pointer',
        }}
      >
        <span style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)' }}>💬</span>
        <div style={{ color: '#C4B5FD', fontSize: 10, marginBottom: 4 }}>
          {h.from_role} → {h.to_role}
        </div>
        <div style={{ color: '#E2E8F0', lineHeight: 1.4 }}>
          {expanded ? h.summary : truncate(h.summary, 50)}
        </div>
        <div
          style={{
            fontSize: 9,
            color: '#A5B4FC',
            marginTop: 4,
            fontFamily: 'monospace',
            display: 'flex',
            justifyContent: 'space-between',
          }}
        >
          <span>port: {h.port}</span>
          <span>{formatBytes(h.payload_size)}</span>
        </div>
      </div>
    </div>
  );
}

// 内联产物卡片
function InlineWidgetBubble({ msg }: { msg: ChatMessage }) {
  const source = msg.business_role
    ? `📦 ${msg.business_role} 产出`
    : '📦 产出';
  return (
    <div className="chat-msg chat-msg-widget">
      <div style={{ fontSize: 10, color: '#94A3B8', marginBottom: 4 }}>{source}</div>
      <WidgetCard widget={msg.widget!} />
    </div>
  );
}

// 普通系统消息（fallback，兼容现有 '── 工作流已启动 ──' 等）
function SystemBubble({ msg }: { msg: ChatMessage }) {
  return (
    <div className="chat-msg chat-msg-system">
      <div className="chat-msg-meta">
        <span className="chat-msg-role">⚡</span>
        <span className="chat-msg-time">{msg.time}</span>
      </div>
      <div className="chat-msg-body">{msg.text}</div>
      {msg.action && (
        <button className="chat-msg-action" onClick={msg.action.onClick}>{msg.action.label}</button>
      )}
    </div>
  );
}

// Inline widget card rendered inside chat messages
function WidgetCard({ widget }: { widget: WidgetUpdate }) {
  const p: Record<string, any> = (widget.props ?? {}) as Record<string, any>;
  switch (widget.type as string) {
    case 'progress_status': {
      const steps: string[] = Array.isArray(p.steps) ? p.steps : [];
      const cur: number = typeof p.currentStep === 'number' ? p.currentStep : typeof p.current === 'number' ? p.current : 0;
      return (
        <div className="widget-inline">
          <strong>{String(p.title ?? 'Progress')}</strong>
          <div className="widget-inline-steps">
            {steps.map((s: string, i: number) => (
              <span key={s} className={i < cur ? 'done' : i === cur ? 'active' : ''}>{s}</span>
            ))}
          </div>
        </div>
      );
    }
    case 'task_draft': {
      const tasks: Array<{ text: string; status?: string }> = Array.isArray(p.tasks) ? p.tasks : [];
      return (
        <div className="widget-inline">
          <strong>{String(p.title ?? 'Tasks')}</strong>
          {tasks.slice(0, 5).map((t: any, i: number) => (
            <div key={i} className="widget-inline-item">{t.status === 'completed' ? '✓' : '○'} {t.text}</div>
          ))}
        </div>
      );
    }
    case 'memo':
      return <div className="widget-inline"><strong>{String(p.title ?? 'Memo')}</strong><p>{String(p.text ?? p.content ?? '')}</p></div>;
    default:
      return <div className="widget-inline"><strong>{widget.type}</strong></div>;
  }
}