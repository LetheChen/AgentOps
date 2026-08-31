// web/src/components/task/CodingTerminalPage.tsx
// V3 Coding 终端多窗格页（§4.13）：可观测智能体开发
// - SSE 订阅 /api/tasks/terminal/stream：agent 会话注册即自动上屏（新窗格）
// - 手动新建 codex / claude / shell 窗口（psmux/tmux session）
// - 网格自动排列（1/2/3 列切换），窗格顺序持久化（terminal_layouts）
// - 复用 TerminalPane 渲染每个会话（轮询 pane 内容 + 命令输入）

import { useState, useEffect, useRef, useCallback } from 'react';
import { taskApi, terminalPageStreamUrl, type TerminalSession } from '../../api/taskApi';
import TerminalPane from './TerminalPane';

type NewKind = 'codex' | 'claude' | 'shell';

export default function CodingTerminalPage({
  compact = false,
}: {
  // 任务中心页签内嵌时 compact=true（隐藏大标题）
  compact?: boolean;
}) {
  const [sessions, setSessions] = useState<TerminalSession[]>([]);
  const [cols, setCols] = useState(2);
  const [creating, setCreating] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const esRef = useRef<EventSource | null>(null);
  // 已保存布局的会话顺序（首次加载时用于排序）
  const layoutOrderRef = useRef<string[]>([]);

  // 按布局顺序排序（布局中的在前，新增的按 created_at 追加）
  const sortSessions = useCallback((list: TerminalSession[]): TerminalSession[] => {
    const order = layoutOrderRef.current;
    if (order.length === 0) return list;
    const idx = (id: string) => {
      const i = order.indexOf(id);
      return i === -1 ? order.length : i;
    };
    return [...list].sort((a, b) => idx(a.terminal_session_id) - idx(b.terminal_session_id));
  }, []);

  const applySessions = useCallback((list: TerminalSession[]) => {
    setSessions(sortSessions(list));
  }, [sortSessions]);

  // 初始加载：布局 + 会话列表
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [layoutRes, sessRes] = await Promise.all([
          taskApi.getTerminalLayout(),
          taskApi.listTerminalSessions(),
        ]);
        if (cancelled) return;
        layoutOrderRef.current = (layoutRes.panes || []).map((p) => p.terminal_session_id);
        applySessions(sessRes.sessions || []);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : '加载终端会话失败');
      }
    })();
    return () => { cancelled = true; };
  }, [applySessions]);

  // SSE：会话注册表快照变更推送（新 agent 会话自动上屏）
  useEffect(() => {
    let es: EventSource | null = null;
    try {
      es = new EventSource(terminalPageStreamUrl);
      esRef.current = es;
      es.onopen = () => setConnected(true);
      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as { type: string; sessions?: TerminalSession[] };
          if (data.type === 'sessions' && Array.isArray(data.sessions)) {
            applySessions(data.sessions);
          }
        } catch {
          // 忽略解析失败
        }
      };
      es.onerror = () => setConnected(false);
    } catch {
      setConnected(false);
    }
    return () => {
      es?.close();
      esRef.current = null;
    };
  }, [applySessions]);

  // 手动新建窗口
  const createSession = async (kind: NewKind) => {
    setCreating(true);
    setError('');
    try {
      const s = await taskApi.createTerminalSession(kind);
      // 立即上屏（不等 SSE 推送）
      setSessions((prev) =>
        prev.find((x) => x.terminal_session_id === s.terminal_session_id) ? prev : [...prev, s]
      );
      // 追加到布局顺序并保存
      layoutOrderRef.current = [
        ...layoutOrderRef.current.filter((id) => id !== s.terminal_session_id),
        s.terminal_session_id,
      ];
      taskApi.saveTerminalLayout(
        layoutOrderRef.current.map((id) => ({ terminal_session_id: id }))
      ).catch(() => { /* 布局保存失败不阻塞 */ });
    } catch (e) {
      setError(e instanceof Error ? e.message : '新建窗口失败');
    } finally {
      setCreating(false);
    }
  };

  // 关闭会话
  const closeSession = async (terminalSessionId: string) => {
    // 本地立即移除（后端 kill + 状态 dead）
    setSessions((prev) => prev.filter((s) => s.terminal_session_id !== terminalSessionId));
    layoutOrderRef.current = layoutOrderRef.current.filter((id) => id !== terminalSessionId);
    try {
      await taskApi.closeTerminalSession(terminalSessionId);
    } catch (e) {
      setError(e instanceof Error ? e.message : '关闭会话失败');
    }
    taskApi.saveTerminalLayout(
      layoutOrderRef.current.map((id) => ({ terminal_session_id: id }))
    ).catch(() => { /* 忽略 */ });
  };

  const activeCount = sessions.filter((s) => s.status === 'active').length;

  return (
    <div style={{
      height: '100%', display: 'flex', flexDirection: 'column',
      color: 'var(--color-text-primary)',
    }}>
      {/* 工具栏 */}
      <div style={{
        display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap',
      }}>
        {!compact && (
          <h2 style={{ margin: 0, fontSize: 18, color: 'var(--color-text-primary)' }}>
            Coding 终端
          </h2>
        )}
        <span style={{
          fontSize: 11, color: connected ? '#43a047' : 'var(--color-text-tertiary)',
        }}>
          ● {connected ? '实时连接' : '离线（重连中）'}
        </span>
        <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>
          会话 {activeCount} 活跃 / {sessions.length} 总计 · agent 窗格随任务派发自动上屏
        </span>

        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {/* 列数切换 */}
          <div style={{ display: 'flex', gap: 2 }}>
            {[1, 2, 3].map((n) => (
              <button
                key={n}
                onClick={() => setCols(n)}
                title={`${n} 列布局`}
                style={{
                  padding: '4px 10px', fontSize: 12, cursor: 'pointer',
                  background: cols === n ? 'var(--color-primary)' : 'var(--color-bg-elevated)',
                  color: cols === n ? '#fff' : 'var(--color-text-secondary)',
                  border: '1px solid var(--color-border-subtle)',
                }}
              >
                {n} 列
              </button>
            ))}
          </div>
          {/* 手动新建 */}
          <button
            onClick={() => createSession('codex')}
            disabled={creating}
            style={{ ...newBtnStyle, borderColor: '#43a047', color: '#43a047' }}
            title="新建 Codex 开发窗口（psmux/tmux session，自动启动 codex CLI）"
          >
            ⚡ Codex
          </button>
          <button
            onClick={() => createSession('claude')}
            disabled={creating}
            style={{ ...newBtnStyle, borderColor: '#fb8c00', color: '#fb8c00' }}
            title="新建 Claude 开发窗口（自动启动 claude CLI）"
          >
            🧠 Claude
          </button>
          <button
            onClick={() => createSession('shell')}
            disabled={creating}
            style={newBtnStyle}
            title="新建 Shell 窗口"
          >
            🐚 Shell
          </button>
        </div>
      </div>

      {error && (
        <div style={{
          background: 'rgba(239, 68, 68, 0.12)', color: '#fca5a5',
          padding: '6px 12px', borderRadius: 'var(--radius-sm)',
          marginBottom: 10, fontSize: 12, border: '1px solid rgba(239, 68, 68, 0.3)',
        }}>
          {error}
        </div>
      )}

      {/* 窗格网格 */}
      {sessions.length === 0 ? (
        <div style={{
          flex: 1, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 10,
          background: 'var(--color-bg-surface)',
          border: '1px dashed var(--color-border-default)',
          borderRadius: 'var(--radius-md)', color: 'var(--color-text-tertiary)',
        }}>
          <div style={{ fontSize: 15 }}>暂无终端会话</div>
          <div style={{ fontSize: 12, lineHeight: 1.8, textAlign: 'center' }}>
            · 任务详情页点「▶ 执行编码」→ agent 窗格自动上屏<br />
            · 或上方手动新建 Codex / Claude / Shell 开发窗口
          </div>
        </div>
      ) : (
        <div style={{
          flex: 1, overflow: 'auto',
          display: 'grid', gap: 12,
          gridTemplateColumns: `repeat(${cols}, minmax(280px, 1fr))`,
          alignItems: 'start',
        }}>
          {sessions.map((s) => (
            <TerminalPane
              key={s.terminal_session_id}
              session={s}
              onClose={closeSession}
            />
          ))}
        </div>
      )}
    </div>
  );
}

const newBtnStyle: React.CSSProperties = {
  padding: '5px 14px', background: 'transparent',
  border: '1px solid var(--color-border-default)',
  borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 12,
};
