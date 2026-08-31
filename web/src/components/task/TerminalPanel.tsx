// web/src/components/task/TerminalPanel.tsx
// V1 终端面板（SSE 流式 + activities 回退）
// 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.8.3
//
// 策略：优先连接 /api/tasks/{taskId}/terminal/stream SSE 流
//   - task 绑定 terminal_session_id → 接收 pane 内容（真实 terminal 输出）
//   - 未绑定 → 接收 activities 事件（活动日志回退）
//   SSE 连接失败时回退到 2s 轮询 activities（兼容旧路径）
// 不依赖 xterm.js，使用原生 <pre> + 自动滚动，轻量高效。

import { useState, useEffect, useRef, useCallback } from 'react';
import { API_BASE_URL } from '../../lib/api';
import { taskApi, type TaskActivity } from '../../api/taskApi';

// 活动变更 → 终端行格式化
function formatActivityLine(act: TaskActivity): string {
  const ts = new Date(act.created_at).toLocaleTimeString('zh-CN', { hour12: false });
  const actor = act.actor_name || act.actor_id || act.actor_type;
  const changes = act.changes || {};
  const entries = Object.entries(changes);
  if (entries.length === 0) {
    return `[${ts}] ${actor}: (no changes)`;
  }
  const parts = entries.map(([k, v]) => {
    if (k === 'status') return `status → ${String(v)}`;
    if (k === 'assignee_name') return `assignee → ${String(v)}`;
    return `${k} = ${JSON.stringify(v)}`;
  });
  return `[${ts}] ${actor}: ${parts.join(' | ')}`;
}

export default function TerminalPanel({
  taskId,
  terminalSessionId,
  height = 320,
}: {
  taskId: string;
  terminalSessionId?: string;
  height?: number;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const [mode, setMode] = useState<'sse' | 'poll'>('sse');
  const preRef = useRef<HTMLPreElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  // 回退：加载活动日志（轮询模式）
  const loadActivities = useCallback(async () => {
    try {
      const data = await taskApi.listActivities(taskId);
      const acts = data.activities || [];
      const formatted = acts.map(formatActivityLine);
      setLines(formatted);
      setConnected(true);
    } catch {
      setConnected(false);
    }
  }, [taskId]);

  useEffect(() => {
    let cancelled = false;

    // 优先尝试 SSE 流
    const sseUrl = `${API_BASE_URL}/api/tasks/${taskId}/terminal/stream`;
    let es: EventSource | null = null;
    try {
      es = new EventSource(sseUrl);
      eventSourceRef.current = es;

      es.onopen = () => {
        if (!cancelled) {
          setConnected(true);
          setMode('sse');
        }
      };

      es.onmessage = (ev) => {
        if (cancelled) return;
        try {
          const data = JSON.parse(ev.data);
          if (data.type === 'pane') {
            // 真实 terminal 输出：按行分割追加
            const paneLines = (data.content || '').split('\n').filter((l: string) => l.length > 0);
            if (paneLines.length > 0) {
              setLines((prev) => [...prev, ...paneLines].slice(-500));
            }
          } else if (data.type === 'activities') {
            // activities 回退（SSE 通道）
            const acts: TaskActivity[] = data.activities || [];
            setLines(acts.map(formatActivityLine));
          }
        } catch {
          // 忽略解析错误
        }
      };

      es.onerror = () => {
        if (cancelled) return;
        // SSE 失败 → 回退到轮询
        setConnected(false);
        setMode('poll');
        if (es) {
          es.close();
          es = null;
          eventSourceRef.current = null;
        }
        loadActivities();
        pollRef.current = setInterval(loadActivities, 2000);
      };
    } catch {
      // EventSource 构造失败 → 直接轮询
      setMode('poll');
      loadActivities();
      pollRef.current = setInterval(loadActivities, 2000);
    }

    return () => {
      cancelled = true;
      if (es) {
        es.close();
        es = null;
        eventSourceRef.current = null;
      }
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [taskId, loadActivities]);

  // 自动滚动到底部
  useEffect(() => {
    if (preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [lines]);

  return (
    <div
      style={{
        background: '#1e1e1e',
        borderRadius: 'var(--radius-sm)',
        border: '1px solid #333',
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* 终端标题栏 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '4px 12px',
          background: '#252526',
          borderBottom: '1px solid #333',
          fontSize: 12,
          color: '#cccccc',
          fontFamily: 'ui-monospace, monospace',
        }}
      >
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: connected ? '#43a047' : '#e53935',
            display: 'inline-block',
          }}
        />
        <span>terminal</span>
        {terminalSessionId && (
          <span style={{ color: '#888' }}>· session: {terminalSessionId.slice(0, 12)}</span>
        )}
        <span style={{ marginLeft: 'auto', color: '#666', fontSize: 11 }}>
          {lines.length} lines · {mode === 'sse' ? 'SSE' : '2s poll'}
        </span>
      </div>

      {/* 终端输出区 */}
      <pre
        ref={preRef}
        style={{
          margin: 0,
          padding: 12,
          height,
          overflow: 'auto',
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
          fontSize: 12,
          lineHeight: 1.5,
          color: '#d4d4d4',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {lines.length === 0 ? (
          <span style={{ color: '#666' }}>
            等待终端输出...{'\n'}
            （{mode === 'sse' ? 'SSE 流连接中' : 'activities 轮询中'}）
          </span>
        ) : (
          lines.join('\n')
        )}
        {/* 光标闪烁 */}
        <span style={{ animation: 'blink 1s step-end infinite', color: '#43a047' }}>▊</span>
      </pre>

      {/* 闪烁光标动画 */}
      <style>{`
        @keyframes blink {
          0%, 50% { opacity: 1; }
          51%, 100% { opacity: 0; }
        }
      `}</style>
    </div>
  );
}
