import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '../../lib/api';
import type { DomainSummary, DomainDetail as DomainDetailType } from '../../lib/api';
import { DomainCard } from './DomainCard';
import { DomainDetail } from './DomainDetail';

// ── 类型定义 ──────────────────────────────────────────────────────

export interface DomainDashboardProps {
  onSwitchTab: (
    tab: 'dashboard' | 'vault' | 'search' | 'archive' | 'lint',
    params?: Record<string, string>,
  ) => void;
}

// ── 主组件 ────────────────────────────────────────────────────────

export function DomainDashboard({ onSwitchTab }: DomainDashboardProps) {
  const [domains, setDomains] = useState<DomainSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedDomain, setSelectedDomain] = useState<string | null>(null);
  const [domainDetail, setDomainDetail] = useState<DomainDetailType | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // ── 加载 domain 列表 ──
  const loadDomains = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiClient.listKnowledgeDomains();
      setDomains(resp.domains);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载域列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDomains();
  }, [loadDomains]);

  // ── 进入 domain 详情 ──
  const openDomain = useCallback(async (id: string) => {
    setSelectedDomain(id);
    setDomainDetail(null);
    setError(null);
    setLoadingDetail(true);
    try {
      const detail = await apiClient.getKnowledgeDomain(id);
      setDomainDetail(detail);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载域详情失败');
      setSelectedDomain(null);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  // ── 返回卡片网格 ──
  const backToList = useCallback(() => {
    setSelectedDomain(null);
    setDomainDetail(null);
    setError(null);
  }, []);

  // ── 渲染：详情视图 ──
  if (selectedDomain) {
    if (loadingDetail) {
      return (
        <div className="kh-domain-dashboard">
          <div className="kh-loading">
            <div className="ag-spinner" />
            <span>加载域详情...</span>
          </div>
        </div>
      );
    }
    if (domainDetail) {
      return (
        <div className="kh-domain-dashboard">
          <DomainDetail detail={domainDetail} onBack={backToList} onSwitchTab={onSwitchTab} />
        </div>
      );
    }
    // 详情加载失败：回退到列表视图并展示错误
  }

  // ── 渲染：卡片网格 ──
  return (
    <div className="kh-domain-dashboard">
      <div className="kh-domain-dashboard-header">
        <h2 className="kh-domain-dashboard-title">知识域仪表盘</h2>
        <p className="kh-domain-dashboard-subtitle">
          共 {domains.length} 个知识域 · 点击卡片查看详情
        </p>
      </div>

      {loading ? (
        <div className="kh-loading">
          <div className="ag-spinner" />
          <span>加载知识域列表...</span>
        </div>
      ) : error ? (
        <div className="kh-error">
          <p>{error}</p>
          <button className="btn-secondary btn-sm" onClick={loadDomains}>
            重试
          </button>
        </div>
      ) : domains.length === 0 ? (
        <div className="kh-empty">
          <p>暂无知识域</p>
        </div>
      ) : (
        <div className="kh-domains-grid">
          {domains.map((d) => (
            <DomainCard
              key={d.id}
              domain={d}
              onClick={() => openDomain(d.id)}
              onLintClick={() => onSwitchTab('lint', { domain: d.id })}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default DomainDashboard;
