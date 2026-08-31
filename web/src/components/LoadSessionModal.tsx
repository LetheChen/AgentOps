import { useState, useEffect, useRef, useCallback } from 'react';
import { apiClient } from '../lib/api';

/**
 * LoadSessionModal — 载入历史会话弹窗。
 *
 * 功能：
 *   - 顶部搜索框（按 title 模糊匹配）
 *   - 会话列表：显示 title / agent_id / last_activity_at 相对时间
 *   - 点击某条会话触发 onLoad(runId)
 *
 * 样式前缀：`lsm-`
 */
interface LoadSessionModalProps {
  open: boolean;
  onClose: () => void;
  onLoad: (runId: string) => void;
}

interface SessionItem {
  run_id: string;
  title?: string | null;
  agent_id?: string | null;
  run_mode?: string | null;
  status?: string | null;
  started_at?: string | null;
  last_activity_at?: string | null;
  message_count?: number;
}

/** 格式化相对时间（"3 分钟前" / "2 小时前" / "1 天前"） */
function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '—';
  const diff = Date.now() - t;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(t).toLocaleDateString('zh-CN');
}

export function LoadSessionModal({ open, onClose, onLoad }: LoadSessionModalProps) {
  const PAGE_SIZE = 100;
  const [search, setSearch] = useState('');
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // 请求去重 ref：避免 StrictMode 双调用触发两次 fetch
  const lastReqIdRef = useRef<number>(0);

  const fetchSessions = useCallback(async (keyword: string, offset = 0) => {
    const reqId = ++lastReqIdRef.current;
    if (offset === 0) {
      setLoading(true);
      setError(null);
    } else {
      setLoadingMore(true);
    }
    try {
      const resp = await apiClient.listSessions(undefined, undefined, PAGE_SIZE, offset, keyword || undefined);
      // 仅保留最新一次请求的结果
      if (reqId !== lastReqIdRef.current) return;
      const batch = (resp.sessions as unknown as SessionItem[]) || [];
      if (offset === 0) {
        setSessions(batch);
      } else {
        setSessions((prev) => [...prev, ...batch]);
      }
      setTotal(resp.total ?? batch.length);
      setHasMore(offset + batch.length < (resp.total ?? 0));
    } catch (err) {
      if (reqId !== lastReqIdRef.current) return;
      console.error('Failed to load sessions:', err);
      setError(err instanceof Error ? err.message : '加载失败');
      if (offset === 0) setSessions([]);
    } finally {
      if (reqId === lastReqIdRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  // 加载更多
  const loadMore = useCallback(() => {
    if (loadingMore || !hasMore) return;
    fetchSessions(search, sessions.length);
  }, [loadingMore, hasMore, search, sessions.length, fetchSessions]);

  // 弹窗打开时首次加载；搜索词变化时重置到第一页
  useEffect(() => {
    if (!open) return;
    fetchSessions(search, 0);
  }, [open, search, fetchSessions]);

  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, onClose]);

  if (!open) return null;

  const handleItemClick = (runId: string) => {
    onLoad(runId);
    onClose();
  };

  return (
    <div className="lsm-overlay" onClick={onClose}>
      <div className="lsm-modal" onClick={(e) => e.stopPropagation()}>
        {/* ── 顶部：标题 + 搜索框 ── */}
        <div className="lsm-header">
          <div className="lsm-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
            </svg>
            载入历史会话
          </div>
          <button className="lsm-close" onClick={onClose} title="关闭 (Esc)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="lsm-search">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            type="text"
            className="lsm-search-input"
            placeholder="按标题搜索会话..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            autoFocus
          />
          {search && (
            <button className="lsm-search-clear" onClick={() => setSearch('')} title="清除">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          )}
        </div>

        {/* ── 会话列表 ── */}
        <div className="lsm-list">
          {loading && sessions.length === 0 && (
            <div className="lsm-empty">正在加载会话列表...</div>
          )}
          {!loading && error && (
            <div className="lsm-empty lsm-empty-error">加载失败：{error}</div>
          )}
          {!loading && !error && sessions.length === 0 && (
            <div className="lsm-empty">
              {search ? `未找到匹配「${search}」的会话` : '暂无历史会话'}
            </div>
          )}
          {sessions.map((s) => (
            <button
              key={s.run_id}
              className="lsm-item"
              onClick={() => handleItemClick(s.run_id)}
              title="点击载入此会话"
            >
              <div className="lsm-item-main">
                <div className="lsm-item-title">
                  {s.title || s.run_id.slice(0, 16)}
                </div>
                <div className="lsm-item-meta">
                  {s.agent_id && <span className="lsm-item-agent">{s.agent_id}</span>}
                  {s.run_mode && <span className="lsm-item-mode">{s.run_mode}</span>}
                  {(s.message_count != null && s.message_count > 0) && (
                    <span className="lsm-item-count">{s.message_count} 条消息</span>
                  )}
                </div>
              </div>
              <div className="lsm-item-time">
                {formatRelative(s.last_activity_at || s.started_at)}
              </div>
            </button>
          ))}
          {/* 加载更多按钮（弹窗内联渲染，避免滚动条失效） */}
          {!loading && sessions.length > 0 && hasMore && (
            <button
              className="btn-secondary btn-sm"
              style={{ margin: '8px 12px', alignSelf: 'center' }}
              onClick={loadMore}
              disabled={loadingMore}
            >
              {loadingMore ? '加载中...' : '加载更多'}
            </button>
          )}
        </div>

        {/* ── 底部 ── */}
        <div className="lsm-footer">
          <span className="lsm-footer-count">
            {sessions.length > 0 ? `已显示 ${sessions.length} / ${total} 个会话` : ''}
          </span>
          <button className="btn-secondary btn-sm" onClick={onClose}>取消</button>
        </div>
      </div>
    </div>
  );
}
