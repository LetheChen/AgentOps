import { useState, useCallback, type ReactNode } from 'react';
import type { PageId } from '../App';
import { VaultBrowser } from '../components/knowledge/VaultBrowser';
import { DomainDashboard } from '../components/knowledge/DomainDashboard';
import { SearchCenter } from '../components/knowledge/SearchCenter';
import { AgentArchive } from '../components/knowledge/AgentArchive';
import { LintDisposal } from '../components/knowledge/LintDisposal';
import { AskCenter } from '../components/knowledge/AskCenter';

// ── 类型定义 ──────────────────────────────────────────────────────

type KnowledgeTab = 'dashboard' | 'vault' | 'search' | 'archive' | 'lint' | 'ask';

/** 跨 Tab 切换函数：可携带参数（如从 dashboard 跳 lint 带 domain） */
export type SwitchTabFn = (tab: KnowledgeTab, params?: Record<string, string>) => void;

interface KnowledgeHubPageProps {
  onNavigate: (page: PageId, params?: Record<string, string>) => void;
}

interface TabDef {
  key: KnowledgeTab;
  label: string;
  icon: ReactNode;
}

// ── Tab 定义 ──────────────────────────────────────────────────────

// Tab 顺序：概览 → 高频主动使用 → 查找 → 浏览 → 低频维护
const TABS: TabDef[] = [
  {
    key: 'dashboard',
    label: '仪表盘',
    icon: (
      <svg viewBox="0 0 16 16" width="14" height="14">
        <rect x="2" y="2" width="5" height="5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <rect x="9" y="2" width="5" height="5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <rect x="2" y="9" width="5" height="5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <rect x="9" y="9" width="5" height="5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
      </svg>
    ),
  },
  {
    key: 'ask',
    label: '智能问答',
    icon: (
      <svg viewBox="0 0 16 16" width="14" height="14">
        <path
          d="M8 2.5a4.5 4.5 0 0 1 4.5 4.5c0 1.6-.8 3-2 3.8v2.2a.5.5 0 0 1-.5.5h-4a.5.5 0 0 1-.5-.5v-2.2c-1.2-.8-2-2.2-2-3.8A4.5 4.5 0 0 1 8 2.5z"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinejoin="round"
        />
        <path d="M6 14.5h4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    key: 'search',
    label: '搜索中心',
    icon: (
      <svg viewBox="0 0 16 16" width="14" height="14">
        <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M10.5 10.5l3 3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    key: 'vault',
    label: 'Vault 浏览',
    icon: (
      <svg viewBox="0 0 16 16" width="14" height="14">
        <ellipse cx="8" cy="3.5" rx="5.5" ry="1.8" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path
          d="M2.5 3.5v9c0 1 2.5 1.8 5.5 1.8s5.5-.8 5.5-1.8v-9M2.5 8c0 1 2.5 1.8 5.5 1.8s5.5-.8 5.5-1.8"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
        />
      </svg>
    ),
  },
  {
    key: 'archive',
    label: 'Agent 归档',
    icon: (
      <svg viewBox="0 0 16 16" width="14" height="14">
        <rect x="2" y="3" width="12" height="3" rx="0.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path
          d="M3 6v7.5a.5.5 0 0 0 .5.5h9a.5.5 0 0 0 .5-.5V6M6.5 9h3"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
  {
    key: 'lint',
    label: 'Lint 处置',
    icon: (
      <svg viewBox="0 0 16 16" width="14" height="14">
        <path
          d="M8 1.5l5.5 2v4.5c0 3.4-2.3 6-5.5 6.5C4.8 14 2.5 11.4 2.5 8V3.5l5.5-2z"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinejoin="round"
        />
        <path
          d="M5.8 8l1.6 1.6L10.4 6.6"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
];

// ── 辅助函数 ──────────────────────────────────────────────────────

/** 从 URL query 读取初始 tab，默认 dashboard */
function readTabFromUrl(): KnowledgeTab {
  if (typeof window === 'undefined') return 'dashboard';
  const search = new URLSearchParams(window.location.search);
  const tab = search.get('tab');
  if (tab && TABS.some((t) => t.key === tab)) return tab as KnowledgeTab;
  return 'dashboard';
}

// ── 主组件 ────────────────────────────────────────────────────────

export function KnowledgeHubPage({ onNavigate }: KnowledgeHubPageProps) {
  const [activeTab, setActiveTab] = useState<KnowledgeTab>(readTabFromUrl);
  const [tabParams, setTabParams] = useState<Record<string, string>>({});

  // ── Tab 切换：同步 URL + 携带跨 Tab 参数 ──
  const switchTab = useCallback<SwitchTabFn>((tab, params) => {
    setActiveTab(tab);
    setTabParams(params ?? {});
    try {
      const url = new URL(window.location.href);
      url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', url.toString());
    } catch {
      // 忽略 URL 同步失败（非浏览器环境兜底）
    }
  }, []);

  return (
    <div className="kh-page">
      <div className="kh-tabs">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            className={`kh-tab${activeTab === tab.key ? ' active' : ''}`}
            onClick={() => switchTab(tab.key)}
          >
            <span className="kh-tab-icon">{tab.icon}</span>
            <span className="kh-tab-label">{tab.label}</span>
          </button>
        ))}
      </div>
      <div className="kh-content">
        {activeTab === 'dashboard' && <DomainDashboard onSwitchTab={switchTab} />}
        {activeTab === 'vault' && <VaultBrowser params={tabParams} onNavigate={onNavigate} />}
        {activeTab === 'search' && <SearchCenter params={tabParams} onSwitchTab={switchTab} />}
        {activeTab === 'archive' && <AgentArchive params={tabParams} onNavigate={onNavigate} />}
        {activeTab === 'lint' && <LintDisposal params={tabParams} onSwitchTab={switchTab} />}
        {activeTab === 'ask' && <AskCenter params={tabParams} onSwitchTab={switchTab} />}
      </div>
    </div>
  );
}
