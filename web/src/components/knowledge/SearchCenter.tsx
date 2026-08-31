import { useState, useCallback, type KeyboardEvent, type ReactNode } from 'react';
import { apiClient } from '../../lib/api';

interface SearchCenterProps {
  params: Record<string, string>;
  onSwitchTab: (tab: 'dashboard' | 'vault' | 'search' | 'archive' | 'lint', params?: Record<string, string>) => void;
}

type SearchType = 'keyword' | 'tag';

/** 搜索结果单条匹配 */
interface SearchMatch {
  path: string;
  line?: number;
  context?: string;
  tags?: string[];
}

/** 转义正则特殊字符 */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 高亮 context 中匹配 query 的片段 */
function highlight(context: string, query: string): ReactNode[] {
  const q = query.trim();
  if (!q) return [context];
  const parts = context.split(new RegExp(`(${escapeRegExp(q)})`, 'gi'));
  return parts.map((part, i) => {
    if (part.toLowerCase() === q.toLowerCase()) {
      return (
        <mark key={i} className="kh-search-highlight">
          {part}
        </mark>
      );
    }
    return <span key={i}>{part}</span>;
  });
}

export function SearchCenter({ params, onSwitchTab }: SearchCenterProps) {
  const initialQuery = params.query || '';
  const [query, setQuery] = useState(initialQuery);
  const [searchType, setSearchType] = useState<SearchType>('keyword');
  const [results, setResults] = useState<SearchMatch[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 执行搜索
  const doSearch = useCallback(async (q: string, type: SearchType) => {
    if (!q.trim()) {
      setResults([]);
      setTotal(0);
      setSearched(false);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    setSearched(true);
    try {
      const res = await apiClient.searchVault(q, type, 100);
      setResults(res.matches);
      setTotal(res.total);
    } catch (e) {
      setError(`搜索失败: ${e instanceof Error ? e.message : String(e)}`);
      setResults([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleSearch = useCallback(() => {
    doSearch(query, searchType);
  }, [query, searchType, doSearch]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') handleSearch();
    },
    [handleSearch],
  );

  // 切换搜索类型时，若已有查询则自动重新搜索
  const handleSwitchType = useCallback(
    (type: SearchType) => {
      setSearchType(type);
      if (query.trim()) doSearch(query, type);
    },
    [query, doSearch],
  );

  return (
    <div className="kh-search">
      <div className="kh-search-bar">
        <div className="kh-search-type-toggle">
          <button
            type="button"
            className={searchType === 'keyword' ? 'active' : ''}
            onClick={() => handleSwitchType('keyword')}
          >
            关键词
          </button>
          <button
            type="button"
            className={searchType === 'tag' ? 'active' : ''}
            onClick={() => handleSwitchType('tag')}
          >
            标签
          </button>
        </div>
        <input
          className="kh-search-input"
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={searchType === 'keyword' ? '输入关键词搜索 Vault 内容…' : '输入标签名搜索…'}
          autoFocus
        />
        <button type="button" className="btn-primary" onClick={handleSearch} disabled={loading}>
          {loading ? '搜索中…' : '搜索'}
        </button>
      </div>

      {error && <div className="kh-error">{error}</div>}

      <div className="kh-search-results">
        {loading ? (
          <div className="kh-empty">搜索中…</div>
        ) : !searched ? (
          <div className="kh-empty">输入关键词或标签开始搜索</div>
        ) : results.length === 0 ? (
          <div className="kh-empty">未找到匹配结果</div>
        ) : (
          <>
            <div className="kh-search-summary">共 {total} 条结果</div>
            {results.map((m, i) => (
              <div key={`${m.path}-${m.line ?? ''}-${i}`} className="kh-search-result">
                <div className="kh-search-result-header">
                  <button
                    type="button"
                    className="kh-link font-mono"
                    onClick={() => onSwitchTab('vault', { path: m.path })}
                  >
                    {m.path}
                  </button>
                  {m.line !== undefined && (
                    <span className="kh-search-result-line">第 {m.line} 行</span>
                  )}
                </div>
                {searchType === 'tag' && m.tags && m.tags.length > 0 && (
                  <div className="kh-search-result-tags">
                    {m.tags.map((t) => (
                      <span key={t} className="kh-search-tag">
                        {t}
                      </span>
                    ))}
                  </div>
                )}
                {m.context && (
                  <div className="kh-search-result-context">{highlight(m.context, query)}</div>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
