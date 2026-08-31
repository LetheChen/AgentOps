import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { API_BASE_URL, type Tip } from '../lib/api';

const MAX_RETRIES = 5;          // 最多重连次数
const RECONNECT_DELAY_MS = 3000; // 断线 3s 后重连
const MAX_TIPS = 50;             // 队列上限

/** 任务执行类 tip（归 Agent 卡片旁气泡） */
const AGENT_TIP_TYPES = new Set<Tip['type']>(['task_started', 'task_progress', 'task_completed', 'task_failed']);

interface UseMonitorSSEOptions {
  /** 是否启用 SSE（默认 true） */
  enabled?: boolean;
}

interface UseMonitorSSEReturn {
  /** 全部 tips（按时间倒序） */
  tips: Tip[];
  /** 告警类 tips（patrol_alert/quota_warning/validation_result，归底部滚动栏） */
  alertTips: Tip[];
  /** 每个 agent 最新的任务执行 tip（按 agent_id 索引，归卡片旁气泡） */
  lastTipByAgent: Record<string, Tip>;
  /** 最新一条 tip（兼容旧用法） */
  lastEvent: Tip | null;
  connected: boolean;
  error: string | null;
}

/**
 * 监控中心 SSE 订阅 hook
 * - 连接 /api/monitor/tips-stream
 * - 自动重连（断线 3s 后重连，最多 5 次）
 * - tips 分两路：任务执行类（task_*）按 agent_id 路由到卡片气泡；告警类（patrol_alert/quota_warning）归底部滚动栏
 */
export function useMonitorSSE(options: UseMonitorSSEOptions = {}): UseMonitorSSEReturn {
  const { enabled = true } = options;

  const [tips, setTips] = useState<Tip[]>([]);
  const [lastEvent, setLastEvent] = useState<Tip | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sourceRef = useRef<EventSource | null>(null);
  const retryCountRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const closedByUnmountRef = useRef(false);

  const cleanup = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }
    setConnected(false);
  }, []);

  const connect = useCallback(() => {
    if (closedByUnmountRef.current) return;
    if (sourceRef.current) return;

    const source = new EventSource(`${API_BASE_URL}/api/monitor/tips-stream`);
    sourceRef.current = source;

    source.onopen = () => {
      retryCountRef.current = 0;
      setConnected(true);
      setError(null);
    };

    // 后端用命名事件 `event: tip` 推送，必须用 addEventListener('tip') 接收
    source.addEventListener('tip', (ev: MessageEvent<string>) => {
      try {
        const raw = JSON.parse(ev.data) as Record<string, unknown>;
        const tip: Tip = {
          id: String(raw.id ?? raw.tip_id ?? ''),
          type: (String(raw.tip_type ?? raw.type ?? 'task_started') as Tip['type']),
          severity: (String(raw.severity ?? 'info') as Tip['severity']),
          agent_id: raw.agent_id ? String(raw.agent_id) : undefined,
          run_id: raw.run_id ? String(raw.run_id) : undefined,
          title: String(raw.title ?? ''),
          message: String(raw.message ?? ''),
          timestamp: String(raw.timestamp ?? raw.emitted_at ?? new Date().toISOString()),
        };
        if (!tip.id) return;
        setLastEvent(tip);
        setTips((prev) => {
          if (prev.some((t) => t.id === tip.id)) return prev;
          const next = [tip, ...prev];
          return next.length > MAX_TIPS ? next.slice(0, MAX_TIPS) : next;
        });
      } catch {
        // 忽略解析失败
      }
    });

    source.onerror = () => {
      setConnected(false);
      source.close();
      sourceRef.current = null;
      if (closedByUnmountRef.current) return;
      if (retryCountRef.current >= MAX_RETRIES) {
        setError(`SSE 重连失败：已达最大重试次数 ${MAX_RETRIES}`);
        return;
      }
      retryCountRef.current += 1;
      reconnectTimerRef.current = window.setTimeout(() => {
        if (!closedByUnmountRef.current) connect();
      }, RECONNECT_DELAY_MS);
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      cleanup();
      return;
    }
    closedByUnmountRef.current = false;
    connect();
    return () => {
      closedByUnmountRef.current = true;
      cleanup();
    };
  }, [enabled, connect, cleanup]);

  // 派生：告警类 tips（底部滚动栏）
  const alertTips = useMemo(
    () => tips.filter((t) => !AGENT_TIP_TYPES.has(t.type)),
    [tips],
  );

  // 派生：每个 agent 最新的任务执行 tip（卡片旁气泡）
  const lastTipByAgent = useMemo(() => {
    const map: Record<string, Tip> = {};
    for (const t of tips) {
      if (!AGENT_TIP_TYPES.has(t.type)) continue;
      if (!t.agent_id) continue;
      // tips 按时间倒序，第一条就是最新的
      if (!map[t.agent_id]) map[t.agent_id] = t;
    }
    return map;
  }, [tips]);

  return { tips, alertTips, lastTipByAgent, lastEvent, connected, error };
}
