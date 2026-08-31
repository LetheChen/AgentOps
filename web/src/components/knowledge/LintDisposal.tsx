import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '../../lib/api';
import type { LintIssue, DomainSummary } from '../../lib/api';

interface LintDisposalProps {
  params: Record<string, string>;
  onSwitchTab: (tab: 'dashboard' | 'vault' | 'search' | 'archive' | 'lint', params?: Record<string, string>) => void;
}

type StatusFilter = 'pending' | 'resolved' | 'ignored' | 'all';
type SeverityFilter = 'critical' | 'warning' | 'info' | 'all';
type TypeFilter = 'all' | 'contradictions' | 'orphans' | 'missing_pages' | 'stale' | 'index_sync' | 'dead_links';

interface Filters {
  status: StatusFilter;
  severity: SeverityFilter;
  type: TypeFilter;
}

/** severity 中文标签 */
const SEVERITY_LABEL: Record<LintIssue['severity'], string> = {
  critical: '严重',
  warning: '警告',
  info: '提示',
};

/** severity 排序权重（critical > warning > info） */
const SEVERITY_ORDER: Record<LintIssue['severity'], number> = {
  critical: 0,
  warning: 1,
  info: 2,
};

/** severity 颜色 */
const SEVERITY_COLOR: Record<LintIssue['severity'], string> = {
  critical: '#ef4444',
  warning: '#f59e0b',
  info: '#3b82f6',
};

/** type 中文标签映射 */
const TYPE_LABEL: Record<LintIssue['type'], string> = {
  contradictions: '矛盾',
  orphans: '孤立页面',
  missing_pages: '缺失页面',
  stale: '过期信息',
  index_sync: '索引不同步',
  dead_links: '死链',
};

/** 状态中文标签 */
const STATUS_LABEL: Record<LintIssue['status'], string> = {
  pending: '待处理',
  resolved: '已解决',
  ignored: '已忽略',
};

/** 类型筛选下拉选项 */
const TYPE_OPTIONS: Array<{ value: TypeFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'contradictions', label: '矛盾' },
  { value: 'orphans', label: '孤立页面' },
  { value: 'missing_pages', label: '缺失页面' },
  { value: 'stale', label: '过期信息' },
  { value: 'index_sync', label: '索引不同步' },
  { value: 'dead_links', label: '死链' },
];

/** ISO 时间格式化为可读字符串 */
function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return iso;
  }
}

/** 处置动作类型 */
type ResolveAction = 'resolve' | 'ignore' | 'fix';

const ACTION_TITLE: Record<ResolveAction, string> = {
  resolve: '确认解决 Issue',
  ignore: '忽略 Issue',
  fix: '自动修复 Issue',
};

const ACTION_VERB: Record<ResolveAction, string> = {
  resolve: '解决',
  ignore: '忽略',
  fix: '修复',
};

export function LintDisposal({ params, onSwitchTab }: LintDisposalProps) {
  const [domains, setDomains] = useState<DomainSummary[]>([]);
  const [selectedDomain, setSelectedDomain] = useState<string | null>(params.domain || null);
  const [issues, setIssues] = useState<LintIssue[]>([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState<Filters>({ status: 'pending', type: 'all', severity: 'all' });
  const [triggering, setTriggering] = useState(false);
  const [loadingDomains, setLoadingDomains] = useState(false);
  const [loadingIssues, setLoadingIssues] = useState(false);
  const [resolveModal, setResolveModal] = useState<{ issueId: string; action: ResolveAction } | null>(null);
  const [note, setNote] = useState('');
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // toast 自动消失
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3200);
    return () => clearTimeout(t);
  }, [toast]);

  // 加载 domain 列表
  const loadDomains = useCallback(async () => {
    setLoadingDomains(true);
    setError(null);
    try {
      const res = await apiClient.listKnowledgeDomains();
      setDomains(res.domains);
    } catch (e) {
      setError(`加载 domain 列表失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoadingDomains(false);
    }
  }, []);

  useEffect(() => {
    loadDomains();
  }, [loadDomains]);

  // 加载 issue 列表
  const loadIssues = useCallback(async (domain: string, f: Filters) => {
    setLoadingIssues(true);
    setError(null);
    try {
      const res = await apiClient.listLintIssues({
        domain,
        status: f.status === 'all' ? undefined : f.status,
        type: f.type === 'all' ? undefined : f.type,
        severity: f.severity === 'all' ? undefined : f.severity,
        limit: 200,
      });
      setIssues(res.issues);
      setTotal(res.total);
    } catch (e) {
      setError(`加载 issue 列表失败: ${e instanceof Error ? e.message : String(e)}`);
      setIssues([]);
      setTotal(0);
    } finally {
      setLoadingIssues(false);
    }
  }, []);

  // 选中 domain 或筛选器变化时加载 issues
  useEffect(() => {
    if (selectedDomain) {
      loadIssues(selectedDomain, filters);
    } else {
      setIssues([]);
      setTotal(0);
    }
  }, [selectedDomain, filters, loadIssues]);

  // 触发新 Lint
  const handleTrigger = useCallback(async () => {
    if (!selectedDomain) return;
    setTriggering(true);
    setError(null);
    try {
      const res = await apiClient.triggerLint(selectedDomain, {});
      setToast(`Lint 完成：新增 ${res.new_issues} 条，自动修复 ${res.auto_fixed} 条，待人工审核 ${res.needs_human_review} 条`);
      // 刷新 issues + domains（lint_summary 可能变化）
      await loadIssues(selectedDomain, filters);
      await loadDomains();
    } catch (e) {
      setToast(`触发 Lint 失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setTriggering(false);
    }
  }, [selectedDomain, filters, loadIssues, loadDomains]);

  // 打开处置模态
  const openResolveModal = useCallback((issueId: string, action: ResolveAction) => {
    setResolveModal({ issueId, action });
    setNote('');
  }, []);

  // 确认处置
  const handleConfirmResolve = useCallback(async () => {
    if (!resolveModal) return;
    const { issueId, action } = resolveModal;
    try {
      await apiClient.resolveLintIssue(issueId, { action, note: note.trim() || undefined });
      setToast(`已${ACTION_VERB[action]}该 issue`);
      setResolveModal(null);
      setNote('');
      if (selectedDomain) {
        await loadIssues(selectedDomain, filters);
        await loadDomains();
      }
    } catch (e) {
      setToast(`处置失败: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, [resolveModal, note, selectedDomain, filters, loadIssues, loadDomains]);

  // 按 severity 排序
  const sortedIssues = [...issues].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);

  // 只展示支持 lint 的 domain
  const lintDomains = domains.filter((d) => d.supports_lint);

  return (
    <div className="kh-lint">
      {/* 左栏 — domain 列表 */}
      <aside className="kh-lint-sidebar">
        <div className="kh-lint-sidebar-header">支持 Lint 的 Domain</div>
        {loadingDomains && <div className="kh-empty">加载中…</div>}
        {!loadingDomains && lintDomains.length === 0 && (
          <div className="kh-empty">暂无支持 Lint 的 domain</div>
        )}
        {lintDomains.map((d) => (
          <div
            key={d.id}
            className={`kh-lint-domain-item${selectedDomain === d.id ? ' active' : ''}`}
            onClick={() => setSelectedDomain(d.id)}
          >
            <div className="kh-lint-domain-name">{d.name}</div>
            <div className="kh-lint-domain-meta">
              <span className="kh-lint-domain-pending">{d.lint_summary.total} 待处理</span>
              {d.lint_summary.critical > 0 && (
                <span className="kh-lint-domain-critical">{d.lint_summary.critical} 严重</span>
              )}
            </div>
          </div>
        ))}
      </aside>

      {/* 右栏 — issue 列表 */}
      <section className="kh-lint-main">
        {!selectedDomain ? (
          <div className="kh-empty">请从左侧选择一个 domain</div>
        ) : (
          <>
            <div className="kh-lint-toolbar">
              <button className="btn-primary" onClick={handleTrigger} disabled={triggering}>
                {triggering ? '触发中…' : '触发新 Lint'}
              </button>
              <div className="kh-lint-filters">
                <label>
                  <span className="kh-filter-label">状态</span>
                  <select
                    value={filters.status}
                    onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value as StatusFilter }))}
                  >
                    <option value="pending">待处理</option>
                    <option value="resolved">已解决</option>
                    <option value="ignored">已忽略</option>
                    <option value="all">全部</option>
                  </select>
                </label>
                <label>
                  <span className="kh-filter-label">严重度</span>
                  <select
                    value={filters.severity}
                    onChange={(e) => setFilters((f) => ({ ...f, severity: e.target.value as SeverityFilter }))}
                  >
                    <option value="all">全部</option>
                    <option value="critical">严重</option>
                    <option value="warning">警告</option>
                    <option value="info">提示</option>
                  </select>
                </label>
                <label>
                  <span className="kh-filter-label">类型</span>
                  <select
                    value={filters.type}
                    onChange={(e) => setFilters((f) => ({ ...f, type: e.target.value as TypeFilter }))}
                  >
                    {TYPE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </label>
                <span className="kh-lint-total">共 {total} 条</span>
              </div>
            </div>

            {error && <div className="kh-error">{error}</div>}

            {loadingIssues ? (
              <div className="kh-empty">加载中…</div>
            ) : sortedIssues.length === 0 ? (
              <div className="kh-empty">暂无 issue</div>
            ) : (
              <div className="kh-lint-issues">
                {sortedIssues.map((issue) => (
                  <div key={issue.id} className="kh-lint-issue-card">
                    <div className="kh-lint-issue-top">
                      <span
                        className="kh-lint-issue-severity"
                        style={{ background: SEVERITY_COLOR[issue.severity] }}
                        title={SEVERITY_LABEL[issue.severity]}
                      />
                      <span className="kh-lint-issue-sev-label" style={{ color: SEVERITY_COLOR[issue.severity] }}>
                        {SEVERITY_LABEL[issue.severity]}
                      </span>
                      <span className="kh-lint-issue-type">{TYPE_LABEL[issue.type]}</span>
                      <span className={`kh-lint-issue-status kh-lint-issue-status-${issue.status}`}>
                        {STATUS_LABEL[issue.status]}
                      </span>
                      {issue.auto_fixable && <span className="kh-lint-issue-fixable">可自动修复</span>}
                    </div>
                    <div className="kh-lint-issue-desc">{issue.description}</div>
                    <div className="kh-lint-issue-meta">
                      {issue.page_a && (
                        <span className="kh-lint-issue-page">
                          <span className="kh-lint-issue-page-label">页面 A：</span>
                          <button
                            type="button"
                            className="kh-link font-mono"
                            onClick={() => onSwitchTab('vault', { path: issue.page_a! })}
                          >
                            {issue.page_a}
                          </button>
                        </span>
                      )}
                      {issue.page_b && (
                        <span className="kh-lint-issue-page">
                          <span className="kh-lint-issue-page-label">页面 B：</span>
                          <span className="font-mono">{issue.page_b}</span>
                        </span>
                      )}
                      <span className="kh-lint-issue-time">检测于 {formatTime(issue.detected_at)}</span>
                    </div>

                    {issue.status === 'pending' ? (
                      <div className="kh-lint-issue-actions">
                        <button className="btn-secondary" type="button" onClick={() => openResolveModal(issue.id, 'resolve')}>
                          确认解决
                        </button>
                        <button className="btn-secondary" type="button" onClick={() => openResolveModal(issue.id, 'ignore')}>
                          忽略
                        </button>
                        {issue.auto_fixable && (
                          <button className="btn-primary" type="button" onClick={() => openResolveModal(issue.id, 'fix')}>
                            自动修复
                          </button>
                        )}
                      </div>
                    ) : (
                      <div className="kh-lint-issue-resolved">
                        {issue.resolved_at && <span>处置于 {formatTime(issue.resolved_at)}</span>}
                        {issue.resolved_by && <span>处置人：{issue.resolved_by}</span>}
                        {issue.resolution_note && <span>备注：{issue.resolution_note}</span>}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </section>

      {/* 处置模态框 */}
      {resolveModal && (
        <div className="kh-modal-overlay" onClick={() => setResolveModal(null)}>
          <div className="kh-modal" onClick={(e) => e.stopPropagation()}>
            <div className="kh-modal-header">{ACTION_TITLE[resolveModal.action]}</div>
            <div className="kh-modal-body">
              <label className="kh-modal-label" htmlFor="kh-resolve-note">处置备注（可选）</label>
              <textarea
                id="kh-resolve-note"
                className="kh-modal-textarea"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="填写处置说明…"
                rows={4}
                autoFocus
              />
            </div>
            <div className="kh-modal-footer">
              <button className="btn-secondary" type="button" onClick={() => setResolveModal(null)}>
                取消
              </button>
              <button className="btn-primary" type="button" onClick={handleConfirmResolve}>
                确认
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toast 提示 */}
      {toast && <div className="kh-toast">{toast}</div>}
    </div>
  );
}
