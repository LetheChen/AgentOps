import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient, type QuotaStatus } from '../lib/api';

const POLL_INTERVAL_MS = 30000; // 30s 轮询一次

interface UseQuotaPollingReturn {
  quotaData: QuotaStatus | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

/**
 * 模型额度轮询 hook
 * - 每 30s 调 GET /api/usage/quota-status
 * - 组件卸载时清理定时器
 * - 首次立即加载，后续按间隔刷新
 */
export function useQuotaPolling(): UseQuotaPollingReturn {
  const [quotaData, setQuotaData] = useState<QuotaStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // StrictMode 双调用守护
  const initializedRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const data = await apiClient.getQuotaStatus();
      setQuotaData(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : '额度状态加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;

    // 立即拉一次
    refresh();

    // 定时轮询
    const timer = window.setInterval(() => {
      refresh();
    }, POLL_INTERVAL_MS);

    // 卸载时清理（独立 effect cleanup）
    return () => {
      window.clearInterval(timer);
    };
  }, [refresh]);

  return { quotaData, loading, error, refresh };
}
