import { useState, useCallback, useEffect, useMemo, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { apiClient, type AskResult, type DomainSummary } from '../../lib/api';
import { renderMarkdown } from '../../lib/markdown';

/** 知识管理 Tab key（与 KnowledgeHubPage 共享，此处局部声明避免循环依赖） */
type KnowledgeTab = 'dashboard' | 'vault' | 'search' | 'archive' | 'lint' | 'ask';

interface AskCenterProps {
  params: Record<string, string>;
  onSwitchTab: (tab: KnowledgeTab, params?: Record<string, string>) => void;
}

/** [📄 文档名] 标记的正则 */
const CITATION_RE = /\[📄\s*([^\]]+)\]/g;

/** 段落片段：纯文本段走 markdown 渲染，引用段渲染为可点击 button。 */
type AnswerSegment =
  | { type: 'text'; content: string; key: string }
  | { type: 'cite'; name: string; key: string };

/**
 * 答案正文：按 [📄 文档名] 切片成 segment 列表——
 * 纯文本段独立 markdown 渲染（避开 markdown-it 处理 NUL/占位符的问题），
 * 引用段直接 React 渲染为 button。稳定可控，无 DOM 后处理，无占位符污染。
 *
 * 跨段落的 markdown 块（极少，LLM 通常在引用前后换行）会断开，按可接受权衡处理。
 */
function AnswerBody({
  answer,
  onCiteClick,
}: {
  answer: string;
  onCiteClick: (name: string) => void;
}) {
  const segments = useMemo<AnswerSegment[]>(() => {
    CITATION_RE.lastIndex = 0;
    const segs: AnswerSegment[] = [];
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = CITATION_RE.exec(answer)) !== null) {
      if (m.index > last) {
        segs.push({ type: 'text', content: answer.slice(last, m.index), key: `t-${segs.length}` });
      }
      segs.push({ type: 'cite', name: m[1].trim(), key: `c-${segs.length}-${m[1].trim()}` });
      last = m.index + m[0].length;
    }
    if (last < answer.length) {
      segs.push({ type: 'text', content: answer.slice(last), key: `t-${segs.length}` });
    }
    return segs;
  }, [answer]);

  return (
    <div className="md-content kh-ask-answer-body">
      {segments.map((seg) =>
        seg.type === 'text' ? (
          <span
            key={seg.key}
            // 单段纯文本走 markdown-it 渲染：html:false / linkify:false 保证 XSS 安全
            dangerouslySetInnerHTML={{ __html: renderMarkdown(seg.content) }}
          />
        ) : (
          <button
            key={seg.key}
            type="button"
            className="kh-ask-cite-marker"
            title={seg.name}
            onClick={() => onCiteClick(seg.name)}
          >
            📄 {seg.name}
          </button>
        ),
      )}
    </div>
  );
}

export function AskCenter({ params, onSwitchTab }: AskCenterProps) {
  const initialQuery = params.query || '';
  const [question, setQuestion] = useState(initialQuery);
  const [domain, setDomain] = useState<string>('');
  const [domains, setDomains] = useState<DomainSummary[]>([]);
  const [result, setResult] = useState<AskResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const citationsRef = useRef<HTMLDivElement>(null);

  // 加载 domain 列表（供下拉选择）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiClient.listKnowledgeDomains();
        if (!cancelled) setDomains(res.domains);
      } catch {
        // 非致命：domain 下拉加载失败不影响查询
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // 执行问答
  const doAsk = useCallback(async (q: string, d: string) => {
    if (!q.trim()) {
      setResult(null);
      setSearched(false);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    setSearched(true);
    setResult(null);
    try {
      const res = await apiClient.askKnowledge(q, d || undefined);
      setResult(res);
    } catch (e) {
      setError(`查询失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleAsk = useCallback(() => {
    doAsk(question, domain);
  }, [question, domain, doAsk]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') handleAsk();
    },
    [handleAsk],
  );

  // 点击答案中的 [📄 文档名] 标记 → 滚动到对应引用
  const handleCiteClick = useCallback((name: string) => {
    // name 是 [📄 文档名] 中的文档名，匹配 citation 元素的 data-cite-name 属性
    const target = citationsRef.current?.querySelector(`[data-cite-name="${CSS.escape(name)}"]`);
    if (target instanceof HTMLElement) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.add('kh-ask-cite-flash');
      setTimeout(() => target.classList.remove('kh-ask-cite-flash'), 1500);
    }
  }, []);

  // 跳转到 Vault 浏览器查看原文
  const handleViewInVault = useCallback(
    (path: string) => {
      onSwitchTab('vault', { path });
    },
    [onSwitchTab],
  );

  const elapsedSec = result ? (result.elapsed_ms / 1000).toFixed(1) : '0';

  return (
    <div className="kh-ask">
      {/* 查询栏 */}
      <div className="kh-ask-bar">
        <input
          className="kh-ask-input"
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入问题，LLM 将从知识库中查找答案并标注出处…"
          autoFocus
          disabled={loading}
        />
        <select
          className="kh-ask-domain"
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
          disabled={loading}
          title="选择查询范围（留空查全部）"
        >
          <option value="">全部知识库</option>
          {domains.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn-primary" onClick={handleAsk} disabled={loading || !question.trim()}>
          {loading ? '查询中…' : '提问'}
        </button>
      </div>

      {error && <div className="kh-error">{error}</div>}

      {/* 结果区 */}
      <div className="kh-ask-results" ref={citationsRef}>
        {loading ? (
          <div className="kh-empty">
            <div className="kh-ask-loading">
              <span className="kh-ask-loading-icon">📚</span>
              <span>检索文档中…</span>
            </div>
          </div>
        ) : !searched ? (
          <div className="kh-empty">输入问题，LLM 将从知识库中查找答案并标注出处</div>
        ) : !result ? (
          <div className="kh-empty">未返回结果</div>
        ) : (
          <>
            {/* 答案区 */}
            <div className="kh-ask-answer-section">
              <div className="kh-ask-section-title">📖 答案</div>
              <div className="kh-ask-answer-body">
                {result.answer.includes('未找到相关内容') ? (
                  <span className="kh-ask-no-result">
                    知识库中未找到与「{question}」相关的内容
                  </span>
                ) : (
                  <AnswerBody answer={result.answer} onCiteClick={handleCiteClick} />
                )}
              </div>
            </div>

            {/* 引用区 */}
            {result.citations.length > 0 && (
              <div className="kh-ask-citations-section">
                <div className="kh-ask-section-title">📚 引用原文</div>
                <div className="kh-ask-citations-list">
                  {result.citations.map((cite, i) => (
                    <div key={`${cite.path}-${i}`} data-cite-name={cite.path.split('/').pop() ?? cite.path} className="kh-ask-citation">
                      <div className="kh-ask-citation-header">
                        <span className="kh-ask-citation-icon">📄</span>
                        <button
                          type="button"
                          className="kh-link font-mono kh-ask-citation-path"
                          onClick={() => handleViewInVault(cite.path)}
                          title="在 Vault 浏览器中查看"
                        >
                          {cite.path}
                        </button>
                        <button
                          type="button"
                          className="kh-ask-vault-btn"
                          onClick={() => handleViewInVault(cite.path)}
                        >
                          在 Vault 中查看
                        </button>
                      </div>
                      {cite.snippet && (
                        <div className="kh-ask-citation-snippet">{cite.snippet}…</div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 底部统计 */}
            <div className="kh-ask-stats">
              ℹ️ 命中 {result.matched_documents} 篇文档 · 耗时 {elapsedSec}s
            </div>
          </>
        )}
      </div>
    </div>
  );
}
