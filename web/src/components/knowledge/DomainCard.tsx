import type { DomainSummary } from '../../lib/api';

// ── 类型定义 ──────────────────────────────────────────────────────

export interface DomainCardProps {
  domain: DomainSummary;
  onClick: () => void;
  onLintClick: () => void;
}

// ── 辅助函数 ──────────────────────────────────────────────────────

/** 将 ISO 时间格式化为 YYYY-MM-DD HH:mm；null 显示「从未」 */
export function formatTime(iso: string | null): string {
  if (!iso) return '从未';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '从未';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ── 组件 ──────────────────────────────────────────────────────────

export function DomainCard({ domain, onClick, onLintClick }: DomainCardProps) {
  return (
    <div
      className="kh-domain-card"
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick();
        }
      }}
    >
      {/* 顶部：图标 + 名称 + Lint 标签 */}
      <div className="kh-domain-card-header">
        <span className="kh-domain-card-icon">📁</span>
        <span className="kh-domain-card-name">{domain.display_name ?? domain.name}</span>
        <span className="kh-domain-card-id font-mono">{domain.id}</span>
        {domain.schema && (
          <span className="kh-domain-card-schema" title={`schema: ${domain.schema}`}>
            {domain.schema === 'llm_wiki' ? '📚' : '🎬'}
          </span>
        )}
        {domain.supports_lint ? (
          <span className="kh-lint-badge kh-lint-badge-on">支持 Lint</span>
        ) : (
          <span className="kh-lint-badge kh-lint-badge-off">无 Lint</span>
        )}
      </div>

      {/* 描述（来自 domains.yaml，可选） */}
      {domain.description && (
        <div className="kh-domain-card-description">{domain.description}</div>
      )}

      {/* Categories（来自 domains.yaml，可选） */}
      {domain.categories && domain.categories.length > 0 && (
        <div className="kh-domain-card-categories">
          {domain.categories.map((cat) => (
            <span key={cat} className="kh-category-tag font-mono">{cat}</span>
          ))}
        </div>
      )}

      {/* Bound Agents（来自 domains.yaml，可选） */}
      {domain.bound_agents && domain.bound_agents.length > 0 && (
        <div className="kh-domain-card-agents">
          <span className="kh-domain-card-agents-label">绑定 Agent：</span>
          {domain.bound_agents.map((a) => (
            <span key={a} className="kh-agent-tag font-mono">{a}</span>
          ))}
        </div>
      )}

      {/* 中部：页面数 + 最近 ingest 时间 */}
      <div className="kh-domain-card-stats">
        <div className="kh-domain-card-stat">
          <span className="kh-domain-card-stat-label">页面数</span>
          <span className="kh-domain-card-stat-value font-mono">{domain.page_count}</span>
        </div>
        <div className="kh-domain-card-stat">
          <span className="kh-domain-card-stat-label">最近 ingest</span>
          <span className="kh-domain-card-stat-value">{formatTime(domain.last_ingest_at)}</span>
        </div>
      </div>

      {/* 底部：lint 摘要（点击进入 lint 处置，阻止冒泡） */}
      <div
        className="kh-domain-card-lint"
        onClick={(e) => {
          e.stopPropagation();
          onLintClick();
        }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            e.stopPropagation();
            onLintClick();
          }
        }}
      >
        {domain.supports_lint ? (
          <>
            <span className="kh-lint-dot kh-lint-critical" />
            <span className="kh-lint-count">{domain.lint_summary.critical}</span>
            <span className="kh-lint-dot kh-lint-warning" />
            <span className="kh-lint-count">{domain.lint_summary.warning}</span>
            <span className="kh-lint-dot kh-lint-info" />
            <span className="kh-lint-count">{domain.lint_summary.info}</span>
            <span className="kh-lint-total">共 {domain.lint_summary.total} 条</span>
          </>
        ) : (
          <span className="kh-lint-na">不支持 Lint</span>
        )}
      </div>
    </div>
  );
}

export default DomainCard;
