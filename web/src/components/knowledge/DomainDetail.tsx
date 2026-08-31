import { useState } from 'react';
import type { DomainDetail as DomainDetailType } from '../../lib/api';
import { formatTime } from './DomainCard';
import { renderMarkdown } from '../../lib/markdown';

// ── 类型定义 ──────────────────────────────────────────────────────

export interface DomainDetailProps {
  detail: DomainDetailType;
  onBack: () => void;
  onSwitchTab: (
    tab: 'dashboard' | 'vault' | 'search' | 'archive' | 'lint',
    params?: Record<string, string>,
  ) => void;
}

// ── 图标 ──────────────────────────────────────────────────────────

const IconBack = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="19" y1="12" x2="5" y2="12" />
    <polyline points="12 19 5 12 12 5" />
  </svg>
);

const IconLint = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9 11l3 3L22 4" />
    <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
  </svg>
);

// ── 组件 ──────────────────────────────────────────────────────────

export function DomainDetail({ detail, onBack, onSwitchTab }: DomainDetailProps) {
  const [agentsMdOpen, setAgentsMdOpen] = useState(false);
  // index.md / AGENTS.md 渲染/源文本切换：默认渲染
  const [indexRender, setIndexRender] = useState(true);
  const [agentsRender, setAgentsRender] = useState(true);
  const indexMd = detail.index_md || '';
  const agentsMd = detail.agents_md || '';

  // 分类统计：计算总数用于进度条占比（至少 1 避免除零）
  const categoryTotal = Math.max(
    detail.by_category.raw + detail.by_category.entities + detail.by_category.concepts + detail.by_category.comparisons,
    1,
  );
  const categories: Array<{ key: string; label: string; value: number; color: string }> = [
    { key: 'raw', label: '原始片段', value: detail.by_category.raw, color: '#3b82f6' },
    { key: 'entities', label: '实体', value: detail.by_category.entities, color: '#10b981' },
    { key: 'concepts', label: '概念', value: detail.by_category.concepts, color: '#60a5fa' },
    { key: 'comparisons', label: '对比', value: detail.by_category.comparisons, color: '#f59e0b' },
  ];

  return (
    <div className="kh-domain-detail">
      {/* 顶部：返回按钮 + 名称 + 操作按钮 */}
      <div className="kh-domain-detail-header">
        <button className="kh-back-btn" onClick={onBack}>
          <IconBack />
          返回
        </button>
        <span className="kh-domain-detail-name">{detail.name}</span>
        <span className="kh-domain-detail-id font-mono">{detail.id}</span>
        <div className="kh-domain-detail-actions">
          <button className="btn-primary btn-sm" onClick={() => onSwitchTab('lint', { domain: detail.id })}>
            <IconLint />
            前往 Lint 处置
          </button>
        </div>
      </div>

      {/* 描述区 */}
      <div className="kh-domain-detail-section">
        <div className="kh-domain-section-title">描述</div>
        <p className="kh-domain-detail-desc">{detail.description || '暂无描述'}</p>
      </div>

      {/* 统计区：4 个统计卡片 */}
      <div className="kh-domain-stats-grid">
        <div className="kh-domain-stat-card">
          <div className="kh-domain-stat-label">总页面数</div>
          <div className="kh-domain-stat-value font-mono">{detail.page_count}</div>
        </div>
        <div className="kh-domain-stat-card">
          <div className="kh-domain-stat-label">原始片段 (raw)</div>
          <div className="kh-domain-stat-value font-mono">{detail.by_category.raw}</div>
        </div>
        <div className="kh-domain-stat-card">
          <div className="kh-domain-stat-label">实体 (entities)</div>
          <div className="kh-domain-stat-value font-mono">{detail.by_category.entities}</div>
        </div>
        <div className="kh-domain-stat-card">
          <div className="kh-domain-stat-label">概念 / 对比</div>
          <div className="kh-domain-stat-value font-mono">
            {detail.by_category.concepts + detail.by_category.comparisons}
          </div>
        </div>
      </div>

      {/* 分类统计：进度条 */}
      <div className="kh-domain-detail-section">
        <div className="kh-domain-section-title">分类统计</div>
        <div className="kh-domain-category-list">
          {categories.map((c) => (
            <div key={c.key} className="kh-domain-category-row">
              <span className="kh-domain-category-label">{c.label}</span>
              <div className="kh-domain-category-bar">
                <div
                  className="kh-domain-category-fill"
                  style={{ width: `${(c.value / categoryTotal) * 100}%`, background: c.color }}
                />
              </div>
              <span className="kh-domain-category-num font-mono">{c.value}</span>
            </div>
          ))}
        </div>
      </div>

      {/* index.md 预览 */}
      <div className="kh-domain-detail-section">
        <div className="kh-domain-section-title">index.md 预览</div>
        <MdPreview content={indexMd} render={indexRender} onToggle={setIndexRender} emptyText="（空）" />
      </div>

      {/* 最近 ingest 时间线 */}
      <div className="kh-domain-detail-section">
        <div className="kh-domain-section-title">最近 ingest 时间线</div>
        {detail.recent_ingests.length === 0 ? (
          <div className="kh-domain-empty">暂无 ingest 记录</div>
        ) : (
          <div className="kh-domain-timeline">
            {detail.recent_ingests.map((ingest, i) => (
              <div key={i} className="kh-domain-timeline-item">
                <span className="kh-domain-timeline-dot" />
                <span className="kh-domain-timeline-time font-mono">{formatTime(ingest.timestamp)}</span>
                <span className="kh-domain-timeline-action">{ingest.action}</span>
                <span className="kh-domain-timeline-page">{ingest.page}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* AGENTS.md 内容（可折叠） */}
      <div className="kh-domain-detail-section">
        <button className="kh-domain-collapse-btn" onClick={() => setAgentsMdOpen((v) => !v)}>
          <span className="kh-domain-collapse-arrow">{agentsMdOpen ? '▼' : '▶'}</span>
          AGENTS.md 内容
        </button>
        {agentsMdOpen && (
          <MdPreview content={agentsMd} render={agentsRender} onToggle={setAgentsRender} emptyText="（空）" className="kh-domain-md-agents" />
        )}
      </div>
    </div>
  );
}

export default DomainDetail;

// ── 局部子组件：markdown 预览（渲染/源文本 切换）─────────────────

interface MdPreviewProps {
  content: string;
  render: boolean;
  onToggle: (next: boolean) => void;
  emptyText?: string;
  className?: string;
}

/** 渲染 vs 源文本 切换的 markdown 预览。内容为空时直接显示占位。 */
function MdPreview({ content, render, onToggle, emptyText = '（空）', className }: MdPreviewProps) {
  const empty = !content;
  return (
    <>
      <div className="kh-preview-toolbar">
        <button
          type="button"
          className={`kh-preview-tool ${render ? 'active' : ''}`}
          onClick={() => onToggle(true)}
          aria-pressed={render}
          disabled={empty}
        >
          渲染
        </button>
        <button
          type="button"
          className={`kh-preview-tool ${!render ? 'active' : ''}`}
          onClick={() => onToggle(false)}
          aria-pressed={!render}
          disabled={empty}
        >
          源文本
        </button>
      </div>
      {empty ? (
        <pre className={`kh-domain-md-preview ${className ?? ''}`}>{emptyText}</pre>
      ) : render ? (
        <div
          className={`md-content kh-domain-md-rendered ${className ?? ''}`}
          // markdown-it 已配置 html:false/linkify:false,输出安全
          dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
        />
      ) : (
        <pre className={`kh-domain-md-preview ${className ?? ''}`}>{content}</pre>
      )}
    </>
  );
}
