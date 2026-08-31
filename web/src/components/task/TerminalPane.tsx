// web/src/components/task/TerminalPane.tsx
// V3 终端窗格（§4.13.2）：单个 psmux/tmux session 的展示与交互
// - 头部：类型标识 + 关联任务/会话名 + 状态 + 关闭按钮
// - 内容：2s 轮询 capturePane（<pre> 黑底渲染 + 自动滚底）
// - 输入框：sendKeys 发送命令（回车自动附加）
// 不依赖 xterm.js，原生 <pre> 轻量实现（对齐 TerminalPanel 模式）

import { useState, useEffect, useRef, useCallback } from 'react';
import { taskApi, type TerminalSession } from '../../api/taskApi';

const KIND_META: Record<string, { icon: string; label: string; color: string }> = {
  agent: { icon: '🤖', label: 'Agent', color: '#7c6ee6' },
  codex: { icon: '⚡', label: 'Codex', color: '#43a047' },
  claude: { icon: '🧠', label: 'Claude', color: '#fb8c00' },
  shell: { icon: '🐚', label: 'Shell', color: '#5b8def' },
};

const STATUS_META: Record<string, { label: string; color: string }> = {
  active: { label: '运行中', color: '#43a047' },
  done: { label: '已完成', color: '#5b8def' },
  dead: { label: '已关闭', color: '#888888' },
};

export default function TerminalPane({
  session,
  onClose,
}: {
  session: TerminalSession;
  onClose: (terminalSessionId: string) => void;
}) {
  const [content, setContent] = useState('');
  const [cmd, setCmd] = useState('');
  const [sending, setSending] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  const alive = session.status === 'active';
  const kind = KIND_META[session.kind] || KIND_META.shell;
  const status = STATUS_META[session.status] || STATUS_META.dead;

  const capture = useCallback(async () => {
    if (!alive) return;
    try {
      const res = await taskApi.captureTerminalPane(session.terminal_session_id);
      if (res.content !== undefined) {
        setContent(res.content);
      }
    } catch {
      // 后端不可用时静默（窗格显示上次内容）
    }
  }, [session.terminal_session_id, alive]);

  useEffect(() => {
    capture();
    if (!alive) return;
    const timer = setInterval(capture, 2000);
    return () => clearInterval(timer);
  }, [capture, alive]);

  // 自动滚底
  useEffect(() => {
    if (preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [content]);

  const send = async () => {
    const keys = cmd.trim();
    if (!keys || sending || !alive) return;
    setSending(true);
    try {
      await taskApi.sendTerminalKeys(session.terminal_session_id, keys);
      setCmd('');
      // 立即刷新一次内容（命令回显）
      setTimeout(capture, 400);
    } catch {
      // 忽略发送失败（下次轮询恢复）
    } finally {
      setSending(false);
    }
  };

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', minHeight: 260,
      background: 'var(--color-bg-surface)',
      border: `1px solid ${alive ? 'var(--color-border-subtle)' : 'var(--color-border-default)'}`,
      borderRadius: 'var(--radius-md)', overflow: 'hidden',
      opacity: alive ? 1 : 0.6,
    }}>
      {/* 头部 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 10px', borderBottom: '1px solid var(--color-border-subtle)',
        background: 'var(--color-bg-elevated)',
      }}>
        <span style={{ fontSize: 14 }}>{kind.icon}</span>
        <span style={{
          fontSize: 11, fontWeight: 700, color: kind.color,
          padding: '1px 8px', borderRadius: 'var(--radius-full)',
          background: `${kind.color}22`, whiteSpace: 'nowrap',
        }}>
          {kind.label}
        </span>
        <span style={{
          flex: 1, fontSize: 12, color: 'var(--color-text-secondary)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }} title={session.task_title || session.terminal_session_id}>
          {session.task_id
            ? `${session.task_title || session.task_id}`
            : session.terminal_session_id}
        </span>
        <span style={{ fontSize: 10, color: status.color, whiteSpace: 'nowrap' }}>
          ● {status.label}
        </span>
        <button
          onClick={() => onClose(session.terminal_session_id)}
          title={alive ? '关闭会话' : '移除窗格'}
          style={{
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'var(--color-text-tertiary)', fontSize: 14, padding: '0 4px',
          }}
        >
          ✕
        </button>
      </div>

      {/* 终端内容 */}
      <pre
        ref={preRef}
        style={{
          flex: 1, margin: 0, padding: 10, minHeight: 180, maxHeight: 320,
          overflow: 'auto', background: '#0d1117', color: '#c9d1d9',
          fontSize: 12, lineHeight: 1.45, fontFamily:
            'Consolas, "Courier New", monospace',
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}
      >
        {content || (alive ? '（等待输出…）' : '（会话已结束）')}
      </pre>

      {/* 命令输入 */}
      {alive && (
        <div style={{ display: 'flex', gap: 6, padding: 8, borderTop: '1px solid var(--color-border-subtle)' }}>
          <input
            value={cmd}
            onChange={(e) => setCmd(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                send();
              }
            }}
            placeholder="输入命令..."
            style={{
              flex: 1, padding: '5px 10px',
              background: 'var(--color-bg-elevated)',
              border: '1px solid var(--color-border-default)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 12, color: 'var(--color-text-primary)',
              fontFamily: 'Consolas, "Courier New", monospace',
            }}
          />
          <button
            onClick={send}
            disabled={sending || !cmd.trim()}
            style={{
              padding: '5px 12px', background: 'var(--color-primary)', color: '#fff',
              border: 'none', borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 12,
            }}
          >
            发送
          </button>
        </div>
      )}
    </div>
  );
}
