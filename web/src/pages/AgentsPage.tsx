import { useState, useEffect, useCallback } from 'react';
import { apiClient, API_BASE_URL } from '../lib/api';

// ── 类型定义 ──────────────────────────────────────────────────────

interface AgentModel {
  provider: string;
  id: string;
}

interface Agent {
  id: string;
  name: string;
  domain: string;
  description: string;
  harness: string;
  model: AgentModel | 'auto';
  system_prompt: string;
  allowed_tools: string[];
  denied_tools: string[];
  knowledge_bases: string[];
  max_concurrent_runs: number;
  timeout_seconds: number;
  cost_limit_per_run: number;
  output_files: Record<string, string>;
  source: 'config' | 'runtime';
}

interface AgentStats {
  total_runs: number;
  status_counts: Record<string, number>;
  running: number;
  completed: number;
  failed: number;
  last_run_at: string | null;
  last_run_status: string | null;
  last_run_finished_at: string | null;
  total_tokens_input: number;
  total_tokens_output: number;
  total_cost_usd: number;
}

interface WorkflowBindingNode {
  node_id: string;
  node_name: string;
  harness: string;
}

interface WorkflowBinding {
  workflow_id: string;
  workflow_name: string;
  nodes: WorkflowBindingNode[];
}

interface AgentDetail extends Agent {
  stats?: AgentStats;
  workflow_bindings?: WorkflowBinding[];
}

interface Tool {
  tool_id: string;
  display_name: string;
  description: string;
  allowed_domains: string[];
  requires_human_approval: boolean;
  handler_module: string;
  handler_function: string;
  builtin: boolean;
}

interface Domain {
  domain: string;
  display_name: string;
  description: string;
}

/** 运行时供应商信息（用于模型级联下拉） */
interface RuntimeProviderInfo {
  provider_id: string;
  models: Array<{ id: string }>;
}

interface AgentsPageProps {
  onConfigureHarness?: () => void;
}

type TabKey = 'identity' | 'responsibility' | 'permissions' | 'knowledge' | 'runtime';

// ── 辅助函数 ──────────────────────────────────────────────────────

/** 将 ISO 时间格式化为相对时间 */
function formatRelative(iso: string | null): string {
  if (!iso) return '暂无运行记录';
  const date = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 0) return '刚刚';
  const diffMin = Math.floor(diffMs / 60000);
  const diffHour = Math.floor(diffMs / 3600000);
  const diffDay = Math.floor(diffMs / 86400000);
  if (diffMin < 1) return '刚刚';
  if (diffMin < 60) return `${diffMin} 分钟前`;
  if (diffHour < 24) return `${diffHour} 小时前`;
  if (diffDay < 30) return `${diffDay} 天前`;
  return date.toLocaleDateString('zh-CN');
}

/** 格式化 token 数量 */
function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

/** 格式化成本 */
function formatCost(n: number): string {
  if (n >= 1) return `$${n.toFixed(2)}`;
  if (n > 0) return `$${n.toFixed(4)}`;
  return '$0.00';
}

/** 格式化成本上限 */
function formatCostLimit(n: number): string {
  if (n <= 0) return '无限制';
  return `$${n}`;
}

/** 模型展示 */
function modelDisplay(model: AgentModel | 'auto'): string {
  if (model === 'auto') return '自动';
  if (model.provider && model.id) return `${model.provider}/${model.id}`;
  if (model.id) return model.id;
  return '自动';
}

/** harness 徽章 CSS 类 */
const HARNESS_BADGE_CLASS: Record<string, string> = {
  codex: 'ag-harness-codex',
  opencode: 'ag-harness-opencode',
  claude_code: 'ag-harness-claude-code',
  local_llm: 'ag-harness-local-llm',
  deterministic: 'ag-harness-deterministic',
  kimi: 'ag-harness-kimi',
  http: 'ag-harness-http',
};

function harnessBadgeClass(harness: string): string {
  return HARNESS_BADGE_CLASS[harness] ?? 'ag-harness-default';
}

function safeNum(val: unknown, defaultVal: number): number {
  const n = Number(val);
  return Number.isNaN(n) ? defaultVal : n;
}

function parseModel(raw: unknown): AgentModel | 'auto' {
  if (raw === 'auto' || raw === null || raw === undefined) return 'auto';
  if (typeof raw === 'object' && raw !== null) {
    const obj = raw as Record<string, unknown>;
    return { provider: String(obj.provider ?? ''), id: String(obj.id ?? '') };
  }
  return 'auto';
}

function parseAgent(raw: Record<string, unknown>): Agent {
  return {
    id: String(raw.id ?? ''),
    name: String(raw.name ?? ''),
    domain: String(raw.domain ?? ''),
    description: String(raw.description ?? ''),
    harness: String(raw.harness ?? 'opencode'),
    model: parseModel(raw.model),
    system_prompt: String(raw.system_prompt ?? ''),
    allowed_tools: Array.isArray(raw.allowed_tools) ? (raw.allowed_tools as string[]) : [],
    denied_tools: Array.isArray(raw.denied_tools) ? (raw.denied_tools as string[]) : [],
    knowledge_bases: Array.isArray(raw.knowledge_bases) ? (raw.knowledge_bases as string[]) : [],
    max_concurrent_runs: safeNum(raw.max_concurrent_runs, 1),
    timeout_seconds: safeNum(raw.timeout_seconds, 600),
    cost_limit_per_run: safeNum(raw.cost_limit_per_run, 0),
    output_files:
      raw.output_files && typeof raw.output_files === 'object'
        ? (raw.output_files as Record<string, string>)
        : {},
    source: raw.source === 'runtime' ? 'runtime' : 'config',
  };
}

function parseTool(raw: Record<string, unknown>): Tool {
  return {
    tool_id: String(raw.tool_id ?? ''),
    display_name: String(raw.display_name ?? ''),
    description: String(raw.description ?? ''),
    allowed_domains: Array.isArray(raw.allowed_domains) ? (raw.allowed_domains as string[]) : [],
    requires_human_approval: Boolean(raw.requires_human_approval),
    handler_module: String(raw.handler_module ?? ''),
    handler_function: String(raw.handler_function ?? ''),
    builtin: Boolean(raw.builtin),
  };
}

function parseDomain(raw: Record<string, unknown>): Domain {
  return {
    domain: String(raw.domain ?? ''),
    display_name: String(raw.display_name ?? ''),
    description: String(raw.description ?? ''),
  };
}

function parseAgentDetail(raw: Record<string, unknown>): AgentDetail {
  const agent = parseAgent(raw);
  const statsRaw = raw.stats as Record<string, unknown> | undefined;
  const bindingsRaw = raw.workflow_bindings as Array<Record<string, unknown>> | undefined;

  const stats: AgentStats | undefined = statsRaw
    ? {
        total_runs: safeNum(statsRaw.total_runs, 0),
        status_counts:
          statsRaw.status_counts && typeof statsRaw.status_counts === 'object'
            ? (statsRaw.status_counts as Record<string, number>)
            : {},
        running: safeNum(statsRaw.running, 0),
        completed: safeNum(statsRaw.completed, 0),
        failed: safeNum(statsRaw.failed, 0),
        last_run_at: (statsRaw.last_run_at as string | null) ?? null,
        last_run_status: (statsRaw.last_run_status as string | null) ?? null,
        last_run_finished_at: (statsRaw.last_run_finished_at as string | null) ?? null,
        total_tokens_input: safeNum(statsRaw.total_tokens_input, 0),
        total_tokens_output: safeNum(statsRaw.total_tokens_output, 0),
        total_cost_usd: safeNum(statsRaw.total_cost_usd, 0),
      }
    : undefined;

  const workflow_bindings: WorkflowBinding[] | undefined = bindingsRaw
    ? bindingsRaw.map((wb) => ({
        workflow_id: String(wb.workflow_id ?? ''),
        workflow_name: String(wb.workflow_name ?? ''),
        nodes: Array.isArray(wb.nodes)
          ? (wb.nodes as Array<Record<string, unknown>>).map((n) => ({
              node_id: String(n.node_id ?? ''),
              node_name: String(n.node_name ?? ''),
              harness: String(n.harness ?? ''),
            }))
          : [],
      }))
    : undefined;

  return { ...agent, stats, workflow_bindings };
}

/** 获取工具在 agent 上的三态 */
function getToolState(toolId: string, agent: Agent | null): 'allowed' | 'denied' | 'default' {
  if (!agent) return 'default';
  if (agent.allowed_tools.includes(toolId)) return 'allowed';
  if (agent.denied_tools.includes(toolId)) return 'denied';
  return 'default';
}

/** 设置工具三态，返回新的 agent */
function setToolState(agent: Agent, toolId: string, state: 'allowed' | 'denied' | 'default'): Agent {
  const allowed = agent.allowed_tools.filter((t) => t !== toolId);
  const denied = agent.denied_tools.filter((t) => t !== toolId);
  if (state === 'allowed') allowed.push(toolId);
  if (state === 'denied') denied.push(toolId);
  return { ...agent, allowed_tools: allowed, denied_tools: denied };
}

/** 从 Agent 构造 draft（深拷贝数组/对象） */
function agentToDraft(a: Agent): Agent {
  return {
    ...a,
    allowed_tools: [...a.allowed_tools],
    denied_tools: [...a.denied_tools],
    knowledge_bases: [...a.knowledge_bases],
    output_files: { ...a.output_files },
  };
}

/** 构造发送给后端的 payload */
function agentToPayload(a: Agent): Record<string, unknown> {
  return {
    id: a.id,
    name: a.name,
    domain: a.domain,
    description: a.description,
    harness: a.harness,
    model: a.model,
    system_prompt: a.system_prompt,
    allowed_tools: a.allowed_tools,
    denied_tools: a.denied_tools,
    knowledge_bases: a.knowledge_bases,
    max_concurrent_runs: a.max_concurrent_runs,
    timeout_seconds: a.timeout_seconds,
    cost_limit_per_run: a.cost_limit_per_run,
    output_files: a.output_files,
    source: a.source,
  };
}

// ── 图标组件 ──────────────────────────────────────────────────────

const IconSearch = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
);

const IconPlus = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
  </svg>
);

const IconClose = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

const IconTrash = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
  </svg>
);

const IconUser = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" />
  </svg>
);

const IconDoc = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" />
  </svg>
);

const IconShield = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
  </svg>
);

const IconBook = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
  </svg>
);

const IconActivity = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
  </svg>
);

// ── Tab 配置 ──────────────────────────────────────────────────────

const TABS: Array<{ key: TabKey; label: string; icon: () => JSX.Element }> = [
  { key: 'identity', label: '身份定义', icon: IconUser },
  { key: 'responsibility', label: '权责信息', icon: IconDoc },
  { key: 'permissions', label: '工具权限', icon: IconShield },
  { key: 'knowledge', label: '知识库', icon: IconBook },
  { key: 'runtime', label: '运行情况', icon: IconActivity },
];

// ── CreateForm 类型 ───────────────────────────────────────────────

interface CreateFormState {
  id: string;
  name: string;
  domain: string;
  harness: string;
  modelAuto: boolean;
  modelProvider: string;
  modelId: string;
  description: string;
  system_prompt: string;
  allowed_tools: string[];
}

const EMPTY_CREATE_FORM: CreateFormState = {
  id: '',
  name: '',
  domain: '',
  harness: 'opencode',
  modelAuto: true,
  modelProvider: '',
  modelId: '',
  description: '',
  system_prompt: '',
  allowed_tools: [],
};

// ── 主组件 ────────────────────────────────────────────────────────

export function AgentsPage(_props: AgentsPageProps = {}) {
  // 列表状态
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 筛选状态
  const [searchQuery, setSearchQuery] = useState('');
  const [harnessFilter, setHarnessFilter] = useState('all');
  const [domainFilter, setDomainFilter] = useState('all');

  // 参考数据
  const [harnesses, setHarnesses] = useState<string[]>([]);
  const [domains, setDomains] = useState<Domain[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [runtimeProviders, setRuntimeProviders] = useState<RuntimeProviderInfo[]>([]);

  // 抽屉状态
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerAgent, setDrawerAgent] = useState<Agent | null>(null);
  const [draft, setDraft] = useState<Agent | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>('identity');
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  // 工具筛选
  const [toolSearch, setToolSearch] = useState('');
  const [onlyConfigured, setOnlyConfigured] = useState(false);

  // 知识库输入
  const [newKb, setNewKb] = useState('');

  // 新建模态
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CreateFormState>(EMPTY_CREATE_FORM);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');

  // 删除确认
  const [deleteId, setDeleteId] = useState<string | null>(null);

  // Toast
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  // ── 数据加载 ──

  const loadAgents = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiClient.getAgents();
      setAgents(data.agents.map((a) => parseAgent(a)));
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载智能体列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    const loadRef = async () => {
      try {
        const [hData, tData] = await Promise.all([
          apiClient.getHarnesses(),
          apiClient.getTools(),
        ]);
        setHarnesses(hData.harnesses);
        setTools(tData.tools.map(parseTool));
      } catch {
        // 非关键数据，静默
      }
      try {
        const dRes = await fetch(`${API_BASE_URL}/api/agent/domains`);
        if (dRes.ok) {
          const dData = await dRes.json();
          setDomains((dData.domains as Array<Record<string, unknown>>).map(parseDomain));
        }
      } catch {
        // 非关键数据，静默
      }
      // 加载运行时供应商（用于模型级联下拉）
      try {
        const rtRes = await fetch(`${API_BASE_URL}/api/runtime/summary`);
        if (rtRes.ok) {
          const rtData = await rtRes.json();
          setRuntimeProviders(
            (rtData.providers as Array<Record<string, unknown>>).map((p) => ({
              provider_id: String(p.provider_id ?? ''),
              models: Array.isArray(p.models)
                ? (p.models as Array<Record<string, unknown>>).map((m) => ({ id: String(m.id ?? '') }))
                : [],
            })),
          );
        }
      } catch {
        // 非关键数据，静默
      }
    };
    loadRef();
  }, []);

  // Toast 自动消失
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 3500);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  const showToast = useCallback((type: 'success' | 'error', text: string) => {
    setToast({ type, text });
  }, []);

  // ── 筛选 ──

  const filteredAgents = agents.filter((a) => {
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      if (
        !a.name.toLowerCase().includes(q) &&
        !a.id.toLowerCase().includes(q) &&
        !a.domain.toLowerCase().includes(q)
      ) {
        return false;
      }
    }
    if (harnessFilter !== 'all' && a.harness !== harnessFilter) return false;
    if (domainFilter !== 'all' && a.domain !== domainFilter) return false;
    return true;
  });

  // ── 抽屉操作 ──

  const openDrawer = useCallback(async (agent: Agent) => {
    setDrawerOpen(true);
    setDrawerAgent(agent);
    setDraft(agentToDraft(agent));
    setDetail(null);
    setDetailLoading(true);
    setActiveTab('identity');
    setSaveMessage(null);
    setToolSearch('');
    setOnlyConfigured(false);
    setNewKb('');

    try {
      const data = await apiClient.getAgent(agent.id);
      const parsed = parseAgentDetail(data.agent);
      setDetail(parsed);
      setDraft(agentToDraft(parsed));
    } catch {
      // draft 已有列表数据，忽略
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    setDrawerAgent(null);
    setDraft(null);
    setDetail(null);
    setSaveMessage(null);
    setToolSearch('');
    setOnlyConfigured(false);
    setNewKb('');
    setActiveTab('identity');
  }, []);

  // ── 保存 ──

  const handleSave = useCallback(async () => {
    if (!draft) return;
    setSaving(true);
    setSaveMessage(null);
    try {
      await apiClient.updateAgent(draft.id, agentToPayload(draft));
      const msg =
        draft.source === 'config'
          ? `已保存并写回 config/agents/${draft.id}.yaml`
          : '已保存';
      // 成功反馈仅由 toast 承担，避免与内嵌 saveMessage 重复显示
      setSaveMessage(null);
      showToast('success', msg);
      // 刷新列表
      const listData = await apiClient.getAgents();
      setAgents(listData.agents.map(parseAgent));
      // 刷新详情
      const detailData = await apiClient.getAgent(draft.id);
      const parsed = parseAgentDetail(detailData.agent);
      setDetail(parsed);
      setDrawerAgent(parsed);
      setDraft(agentToDraft(parsed));
    } catch (e) {
      const msg = e instanceof Error ? e.message : '保存失败';
      setSaveMessage({ type: 'error', text: msg });
      showToast('error', msg);
    } finally {
      setSaving(false);
    }
  }, [draft, showToast]);

  // ── 删除 ──

  const handleDelete = useCallback(async () => {
    if (!deleteId) return;
    try {
      await apiClient.deleteAgent(deleteId);
      showToast('success', `智能体 ${deleteId} 已删除`);
      setDeleteId(null);
      if (drawerAgent?.id === deleteId) {
        closeDrawer();
      }
      const data = await apiClient.getAgents();
      setAgents(data.agents.map(parseAgent));
    } catch (e) {
      showToast('error', e instanceof Error ? e.message : '删除失败');
      setDeleteId(null);
    }
  }, [deleteId, drawerAgent, closeDrawer, showToast]);

  // ── 新建 ──

  const handleOpenCreate = useCallback(() => {
    setCreateForm({
      ...EMPTY_CREATE_FORM,
      domain: domains[0]?.domain ?? '',
    });
    setCreateError('');
    setCreateOpen(true);
  }, [domains]);

  const handleCreate = useCallback(async () => {
    if (!createForm.id.trim() || !createForm.name.trim()) {
      setCreateError('请填写 ID 和显示名');
      return;
    }
    setCreating(true);
    setCreateError('');
    try {
      const model: AgentModel | 'auto' = createForm.modelAuto
        ? 'auto'
        : { provider: createForm.modelProvider, id: createForm.modelId };
      const payload: Record<string, unknown> = {
        id: createForm.id.trim(),
        name: createForm.name.trim(),
        domain: createForm.domain || 'general',
        description: createForm.description,
        harness: createForm.harness,
        model,
        system_prompt: createForm.system_prompt,
        allowed_tools: createForm.allowed_tools,
        denied_tools: [],
        knowledge_bases: [],
        max_concurrent_runs: 1,
        timeout_seconds: 600,
        cost_limit_per_run: 0,
        output_files: {},
        source: 'config',
      };
      const data = await apiClient.createAgent(payload);
      const newAgent = parseAgent(data.agent);
      setCreateOpen(false);
      showToast('success', `智能体 ${newAgent.name} 已创建`);
      const listData = await apiClient.getAgents();
      setAgents(listData.agents.map(parseAgent));
      openDrawer(newAgent);
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : '创建失败');
    } finally {
      setCreating(false);
    }
  }, [createForm, openDrawer, showToast]);

  // ── 工具权限筛选 ──

  const filteredTools = tools.filter((t) => {
    if (toolSearch) {
      const q = toolSearch.toLowerCase();
      if (
        !t.display_name.toLowerCase().includes(q) &&
        !t.tool_id.toLowerCase().includes(q) &&
        !t.description.toLowerCase().includes(q)
      ) {
        return false;
      }
    }
    if (onlyConfigured && draft) {
      if (!draft.allowed_tools.includes(t.tool_id) && !draft.denied_tools.includes(t.tool_id)) {
        return false;
      }
    }
    return true;
  });

  // ── 渲染 ──

  return (
    <div className="ag-page">
      {/* 页头 */}
      <div className="ag-header">
        <div className="ag-header-info">
          <h1 className="ag-header-title">Agent 管理</h1>
          <p className="ag-header-subtitle">身份定义 · 权责信息 · 工具权限 · 运行状态</p>
        </div>
        <div className="ag-header-actions">
          <button className="btn-primary" onClick={handleOpenCreate}>
            <IconPlus />
            新建智能体
          </button>
        </div>
      </div>

      {/* 筛选栏 */}
      <div className="ag-filters">
        <div className="ag-search-wrap">
          <IconSearch />
          <input
            className="ag-search-input"
            placeholder="搜索名称 / ID / 域..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
        <select
          className="ag-filter-select"
          value={harnessFilter}
          onChange={(e) => setHarnessFilter(e.target.value)}
        >
          <option value="all">全部 Harness</option>
          {harnesses.map((h) => (
            <option key={h} value={h}>{h}</option>
          ))}
        </select>
        <select
          className="ag-filter-select"
          value={domainFilter}
          onChange={(e) => setDomainFilter(e.target.value)}
        >
          <option value="all">全部域</option>
          {domains.map((d) => (
            <option key={d.domain} value={d.domain}>{d.display_name}</option>
          ))}
        </select>
      </div>

      {/* 卡片网格 */}
      {loading ? (
        <div className="ag-loading">
          <div className="ag-spinner" />
          <span>加载智能体列表...</span>
        </div>
      ) : error ? (
        <div className="ag-error">
          <p>{error}</p>
          <button className="btn-secondary btn-sm" onClick={loadAgents}>重试</button>
        </div>
      ) : filteredAgents.length === 0 ? (
        <div className="ag-empty">
          <p>{searchQuery || harnessFilter !== 'all' || domainFilter !== 'all' ? '未找到匹配的智能体' : '暂无智能体，点击「新建智能体」添加'}</p>
        </div>
      ) : (
        <div className="agent-grid">
          {filteredAgents.map((agent) => (
            <div key={agent.id} className="agent-card" onClick={() => openDrawer(agent)}>
              {/* 顶部：名称 + 徽章 */}
              <div className="agent-card-top">
                <div className="agent-card-idblock">
                  <span className="agent-card-name">{agent.name}</span>
                  <span className="agent-card-id font-mono">{agent.id}</span>
                </div>
                <div className="agent-card-badges">
                  <span className={`ag-harness-badge ${harnessBadgeClass(agent.harness)}`}>{agent.harness}</span>
                  <span className={`ag-source-badge ag-source-${agent.source}`}>{agent.source}</span>
                </div>
              </div>

              {/* 标签行 */}
              <div className="agent-card-tags">
                <span className="agent-card-domain">
                  <span className="agent-card-domain-dot" />
                  {agent.domain}
                </span>
                <span className="agent-card-model font-mono">{modelDisplay(agent.model)}</span>
              </div>

              {/* 描述 */}
              <p className="agent-card-desc">{agent.description || '暂无描述'}</p>

              {/* 权限摘要 */}
              <div className="agent-card-perms">
                <div className="ag-perm-block ag-perm-allow">
                  <span className="ag-perm-dot" />
                  <span className="ag-perm-num">{agent.allowed_tools.length}</span>
                  <span className="ag-perm-label">已授权</span>
                </div>
                <div className="ag-perm-block ag-perm-deny">
                  <span className="ag-perm-dot" />
                  <span className="ag-perm-num">{agent.denied_tools.length}</span>
                  <span className="ag-perm-label">已禁止</span>
                </div>
                <div className="ag-perm-block ag-perm-kb">
                  <span className="ag-perm-dot" />
                  <span className="ag-perm-num">{agent.knowledge_bases.length}</span>
                  <span className="ag-perm-label">知识库</span>
                </div>
              </div>

              {/* 运行配置 */}
              <div className="agent-card-config">
                <div className="ag-config-item">
                  <span className="ag-config-label">超时</span>
                  <span className="ag-config-value font-mono">{agent.timeout_seconds}s</span>
                </div>
                <div className="ag-config-item">
                  <span className="ag-config-label">并发</span>
                  <span className="ag-config-value font-mono">{agent.max_concurrent_runs}</span>
                </div>
                <div className="ag-config-item">
                  <span className="ag-config-label">成本上限</span>
                  <span className="ag-config-value font-mono">{formatCostLimit(agent.cost_limit_per_run)}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="ag-count">共 {filteredAgents.length} 个智能体</div>

      {/* ── 详情抽屉 ── */}
      {drawerOpen && draft && (
        <>
          <div className="ag-drawer-overlay" onClick={closeDrawer} />
          <div className="ag-drawer">
            {/* 抽屉头部 */}
            <div className="ag-drawer-header">
              <div className="ag-drawer-header-left">
                <span className="ag-drawer-title">{drawerAgent?.name}</span>
                <span className="ag-drawer-id font-mono">{drawerAgent?.id}</span>
                <span className={`ag-source-badge ag-source-${drawerAgent?.source}`}>{drawerAgent?.source}</span>
              </div>
              <div className="ag-drawer-header-right">
                <button className="ag-btn-danger-sm" onClick={() => setDeleteId(draft.id)} disabled={saving}>
                  <IconTrash />
                </button>
                <button className="btn-primary btn-sm" onClick={handleSave} disabled={saving}>
                  {saving ? '保存中...' : '保存'}
                </button>
                <button className="ag-drawer-close" onClick={closeDrawer}>
                  <IconClose />
                </button>
              </div>
            </div>

            {/* 保存消息 */}
            {saveMessage && (
              <div className={`ag-save-msg ag-save-msg-${saveMessage.type}`}>{saveMessage.text}</div>
            )}

            {/* Tab 栏 */}
            <div className="ag-drawer-tabs">
              {TABS.map((tab) => {
                const Icon = tab.icon;
                return (
                  <button
                    key={tab.key}
                    className={`ag-drawer-tab ${activeTab === tab.key ? 'active' : ''}`}
                    onClick={() => setActiveTab(tab.key)}
                  >
                    <Icon />
                    <span>{tab.label}</span>
                  </button>
                );
              })}
            </div>

            {/* Tab 内容 */}
            <div className="ag-drawer-body">
              {/* ── 身份定义 ── */}
              {activeTab === 'identity' && (
                <div className="ag-tab-content">
                  <div className="ag-form-group">
                    <label className="ag-form-label">智能体 ID</label>
                    <input className="ag-form-input ag-form-readonly font-mono" value={draft.id} readOnly />
                  </div>
                  <div className="ag-form-group">
                    <label className="ag-form-label">显示名</label>
                    <input
                      className="ag-form-input"
                      value={draft.name}
                      onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                    />
                  </div>
                  <div className="ag-form-row">
                    <div className="ag-form-group">
                      <label className="ag-form-label">域 (Domain)</label>
                      <select
                        className="ag-form-select"
                        value={draft.domain}
                        onChange={(e) => setDraft({ ...draft, domain: e.target.value })}
                      >
                        <option value="">未指定</option>
                        {domains.map((d) => (
                          <option key={d.domain} value={d.domain}>{d.display_name} ({d.domain})</option>
                        ))}
                      </select>
                    </div>
                    <div className="ag-form-group">
                      <label className="ag-form-label">Harness</label>
                      <select
                        className="ag-form-select"
                        value={draft.harness}
                        onChange={(e) => setDraft({ ...draft, harness: e.target.value })}
                      >
                        {harnesses.map((h) => (
                          <option key={h} value={h}>{h}</option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <div className="ag-form-group">
                    <label className="ag-form-label">模型 (Model)</label>
                    <div className="ag-model-edit">
                      <label className="ag-checkbox-label">
                        <input
                          type="checkbox"
                          checked={draft.model === 'auto'}
                          onChange={(e) => setDraft({ ...draft, model: e.target.checked ? 'auto' : { provider: runtimeProviders[0]?.provider_id ?? '', id: '' } })}
                        />
                        自动选择 (auto)
                      </label>
                      {draft.model !== 'auto' && (
                        <div className="ag-model-inputs">
                          <select
                            className="ag-form-select font-mono"
                            value={(draft.model as AgentModel).provider}
                            onChange={(e) => setDraft({ ...draft, model: { ...(draft.model as AgentModel), provider: e.target.value, id: '' } })}
                          >
                            <option value="">选择供应商</option>
                            {runtimeProviders.map((p) => (
                              <option key={p.provider_id} value={p.provider_id}>{p.provider_id}</option>
                            ))}
                          </select>
                          <select
                            className="ag-form-select font-mono"
                            value={(draft.model as AgentModel).id}
                            onChange={(e) => setDraft({ ...draft, model: { ...(draft.model as AgentModel), id: e.target.value } })}
                            disabled={!runtimeProviders.find((p) => p.provider_id === (draft.model as AgentModel).provider)?.models.length}
                          >
                            <option value="">选择模型</option>
                            {runtimeProviders.find((p) => p.provider_id === (draft.model as AgentModel).provider)?.models.map((m) => (
                              <option key={m.id} value={m.id}>{m.id}</option>
                            ))}
                          </select>
                        </div>
                      )}
                    </div>
                  </div>
                  <div className="ag-form-group">
                    <label className="ag-form-label">描述</label>
                    <textarea
                      className="ag-form-textarea"
                      value={draft.description}
                      onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                      rows={3}
                    />
                  </div>
                  <div className="ag-form-row">
                    <div className="ag-form-group">
                      <label className="ag-form-label">超时 (秒)</label>
                      <input
                        className="ag-form-input font-mono"
                        type="number"
                        value={draft.timeout_seconds}
                        onChange={(e) => setDraft({ ...draft, timeout_seconds: Number(e.target.value) })}
                      />
                    </div>
                    <div className="ag-form-group">
                      <label className="ag-form-label">并发上限</label>
                      <input
                        className="ag-form-input font-mono"
                        type="number"
                        value={draft.max_concurrent_runs}
                        onChange={(e) => setDraft({ ...draft, max_concurrent_runs: Number(e.target.value) })}
                      />
                    </div>
                    <div className="ag-form-group">
                      <label className="ag-form-label">成本上限 ($)</label>
                      <input
                        className="ag-form-input font-mono"
                        type="number"
                        step="0.01"
                        value={draft.cost_limit_per_run}
                        onChange={(e) => setDraft({ ...draft, cost_limit_per_run: Number(e.target.value) })}
                      />
                    </div>
                  </div>
                </div>
              )}

              {/* ── 权责信息 ── */}
              {activeTab === 'responsibility' && (
                <div className="ag-tab-content">
                  <div className="ag-form-group">
                    <div className="ag-form-label-row">
                      <label className="ag-form-label">System Prompt</label>
                      <span className="ag-char-count">{draft.system_prompt.length} 字符</span>
                    </div>
                    <textarea
                      className="ag-form-textarea ag-form-textarea-mono"
                      value={draft.system_prompt}
                      onChange={(e) => setDraft({ ...draft, system_prompt: e.target.value })}
                    />
                  </div>
                </div>
              )}

              {/* ── 工具权限 ── */}
              {activeTab === 'permissions' && (
                <div className="ag-tab-content">
                  <div className="ag-perms-note">
                    勾选 = 授权 (allowed_tools)，禁止 = 显式拒绝 (denied_tools)，未选 = 不配置
                  </div>
                  <div className="ag-perms-filters">
                    <div className="ag-search-wrap ag-perms-search">
                      <IconSearch />
                      <input
                        className="ag-search-input"
                        placeholder="搜索工具..."
                        value={toolSearch}
                        onChange={(e) => setToolSearch(e.target.value)}
                      />
                    </div>
                    <label className="ag-checkbox-label">
                      <input
                        type="checkbox"
                        checked={onlyConfigured}
                        onChange={(e) => setOnlyConfigured(e.target.checked)}
                      />
                      仅看已配置
                    </label>
                  </div>
                  <div className="ag-tool-list">
                    {filteredTools.length === 0 ? (
                      <div className="ag-empty-text">无匹配工具</div>
                    ) : (
                      filteredTools.map((tool) => {
                        const state = getToolState(tool.tool_id, draft);
                        return (
                          <div key={tool.tool_id} className="ag-tool-row">
                            <div className="ag-tool-info">
                              <div className="ag-tool-name-row">
                                <span className="ag-tool-name">{tool.display_name}</span>
                                <span className="ag-tool-id font-mono">{tool.tool_id}</span>
                              </div>
                              <div className="ag-tool-desc">{tool.description}</div>
                              <div className="ag-tool-badges">
                                {tool.builtin && <span className="ag-tool-tag ag-tool-tag-builtin">内置</span>}
                                {tool.requires_human_approval && (
                                  <span className="ag-tool-tag ag-tool-tag-approval">需人工审批</span>
                                )}
                              </div>
                            </div>
                            <div className="ag-tool-toggle">
                              <button
                                className={`ag-tool-toggle-btn ${state === 'allowed' ? 'active-allow' : ''}`}
                                onClick={() => setDraft(setToolState(draft, tool.tool_id, 'allowed'))}
                              >
                                授权
                              </button>
                              <button
                                className={`ag-tool-toggle-btn ${state === 'denied' ? 'active-deny' : ''}`}
                                onClick={() => setDraft(setToolState(draft, tool.tool_id, 'denied'))}
                              >
                                禁止
                              </button>
                              <button
                                className={`ag-tool-toggle-btn ${state === 'default' ? 'active-default' : ''}`}
                                onClick={() => setDraft(setToolState(draft, tool.tool_id, 'default'))}
                              >
                                默认
                              </button>
                            </div>
                          </div>
                        );
                      })
                    )}
                  </div>
                </div>
              )}

              {/* ── 知识库 ── */}
              {activeTab === 'knowledge' && (
                <div className="ag-tab-content">
                  <div className="ag-kb-list">
                    {draft.knowledge_bases.length === 0 ? (
                      <div className="ag-empty-text">暂无知识库</div>
                    ) : (
                      draft.knowledge_bases.map((kb, i) => (
                        <span key={i} className="ag-kb-tag">
                          <span className="ag-kb-tag-name">{kb}</span>
                          <button
                            className="ag-kb-tag-remove"
                            onClick={() =>
                              setDraft({
                                ...draft,
                                knowledge_bases: draft.knowledge_bases.filter((_, j) => j !== i),
                              })
                            }
                          >
                            ×
                          </button>
                        </span>
                      ))
                    )}
                  </div>
                  <div className="ag-kb-add">
                    <input
                      className="ag-form-input"
                      placeholder="输入知识库名称后回车..."
                      value={newKb}
                      onChange={(e) => setNewKb(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && newKb.trim()) {
                          setDraft({ ...draft, knowledge_bases: [...draft.knowledge_bases, newKb.trim()] });
                          setNewKb('');
                        }
                      }}
                    />
                    <button
                      className="btn-secondary btn-sm"
                      onClick={() => {
                        if (newKb.trim()) {
                          setDraft({ ...draft, knowledge_bases: [...draft.knowledge_bases, newKb.trim()] });
                          setNewKb('');
                        }
                      }}
                    >
                      添加
                    </button>
                  </div>
                </div>
              )}

              {/* ── 运行情况 ── */}
              {activeTab === 'runtime' && (
                <div className="ag-tab-content">
                  {detailLoading ? (
                    <div className="ag-loading">
                      <div className="ag-spinner" />
                      <span>加载运行数据...</span>
                    </div>
                  ) : detail?.stats ? (
                    <>
                      {/* 统计卡片 */}
                      <div className="ag-stat-grid">
                        <div className="ag-stat-card">
                          <div className="ag-stat-label">总运行数</div>
                          <div className="ag-stat-value font-mono">{detail.stats.total_runs}</div>
                        </div>
                        <div className="ag-stat-card">
                          <div className="ag-stat-label">
                            <span className="status-dot status-dot-info" /> 运行中
                          </div>
                          <div className="ag-stat-value font-mono">{detail.stats.running}</div>
                        </div>
                        <div className="ag-stat-card">
                          <div className="ag-stat-label">
                            <span className="status-dot status-dot-success" /> 已完成
                          </div>
                          <div className="ag-stat-value font-mono">{detail.stats.completed}</div>
                        </div>
                        <div className="ag-stat-card">
                          <div className="ag-stat-label">
                            <span className="status-dot status-dot-error" /> 失败
                          </div>
                          <div className="ag-stat-value font-mono">{detail.stats.failed}</div>
                        </div>
                      </div>

                      {/* 额外统计 */}
                      <div className="ag-stat-extra">
                        <div className="ag-stat-extra-row">
                          <span className="ag-stat-extra-label">最近运行</span>
                          <span className="ag-stat-extra-value">{formatRelative(detail.stats.last_run_at)}</span>
                        </div>
                        <div className="ag-stat-extra-row">
                          <span className="ag-stat-extra-label">最近状态</span>
                          <span className="ag-stat-extra-value">{detail.stats.last_run_status ?? '—'}</span>
                        </div>
                        <div className="ag-stat-extra-row">
                          <span className="ag-stat-extra-label">累计 Input Tokens</span>
                          <span className="ag-stat-extra-value font-mono">{formatTokens(detail.stats.total_tokens_input)}</span>
                        </div>
                        <div className="ag-stat-extra-row">
                          <span className="ag-stat-extra-label">累计 Output Tokens</span>
                          <span className="ag-stat-extra-value font-mono">{formatTokens(detail.stats.total_tokens_output)}</span>
                        </div>
                        <div className="ag-stat-extra-row">
                          <span className="ag-stat-extra-label">累计成本</span>
                          <span className="ag-stat-extra-value font-mono">{formatCost(detail.stats.total_cost_usd)}</span>
                        </div>
                      </div>

                      {/* 工作流绑定 */}
                      <div className="ag-section-title">工作流绑定</div>
                      {detail.workflow_bindings && detail.workflow_bindings.length > 0 ? (
                        detail.workflow_bindings.map((wb, i) => (
                          <div key={i} className="ag-wf-binding">
                            <div className="ag-wf-name">{wb.workflow_name}</div>
                            <div className="ag-wf-nodes">
                              {wb.nodes.map((n, j) => (
                                <span key={j} className="ag-wf-node">
                                  <span className="ag-wf-node-name">{n.node_name}</span>
                                  <span className={`ag-harness-badge ag-harness-sm ${harnessBadgeClass(n.harness)}`}>{n.harness}</span>
                                </span>
                              ))}
                            </div>
                          </div>
                        ))
                      ) : (
                        <div className="ag-empty-text">未绑定任何工作流</div>
                      )}

                      {/* 输出文件映射 */}
                      {Object.keys(detail.output_files).length > 0 && (
                        <>
                          <div className="ag-section-title">输出文件映射</div>
                          <div className="ag-output-files">
                            {Object.entries(detail.output_files).map(([port, path]) => (
                              <div key={port} className="ag-output-row">
                                <span className="ag-output-port">{port}</span>
                                <span className="ag-output-path font-mono">{path}</span>
                              </div>
                            ))}
                          </div>
                        </>
                      )}
                    </>
                  ) : (
                    <div className="ag-empty-text">暂无运行数据</div>
                  )}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* ── 新建智能体模态 ── */}
      {createOpen && (
        <div className="ag-modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) setCreateOpen(false); }}>
          <div className="ag-modal">
            <div className="ag-modal-header">
              <span className="ag-modal-title">新建智能体</span>
              <button className="ag-drawer-close" onClick={() => setCreateOpen(false)}>
                <IconClose />
              </button>
            </div>
            <div className="ag-modal-body">
              <div className="ag-form-group">
                <label className="ag-form-label">智能体 ID</label>
                <input
                  className="ag-form-input font-mono"
                  placeholder="如：cost_auditor"
                  value={createForm.id}
                  onChange={(e) => { setCreateForm({ ...createForm, id: e.target.value }); setCreateError(''); }}
                />
              </div>
              <div className="ag-form-group">
                <label className="ag-form-label">显示名</label>
                <input
                  className="ag-form-input"
                  placeholder="如：费用审核员"
                  value={createForm.name}
                  onChange={(e) => { setCreateForm({ ...createForm, name: e.target.value }); setCreateError(''); }}
                />
              </div>
              <div className="ag-form-row">
                <div className="ag-form-group">
                  <label className="ag-form-label">域</label>
                  <select
                    className="ag-form-select"
                    value={createForm.domain}
                    onChange={(e) => setCreateForm({ ...createForm, domain: e.target.value })}
                  >
                    <option value="">未指定</option>
                    {domains.map((d) => (
                      <option key={d.domain} value={d.domain}>{d.display_name}</option>
                    ))}
                  </select>
                </div>
                <div className="ag-form-group">
                  <label className="ag-form-label">Harness</label>
                  <select
                    className="ag-form-select"
                    value={createForm.harness}
                    onChange={(e) => setCreateForm({ ...createForm, harness: e.target.value })}
                  >
                    {harnesses.map((h) => (
                      <option key={h} value={h}>{h}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="ag-form-group">
                <label className="ag-form-label">模型</label>
                <div className="ag-model-edit">
                  <label className="ag-checkbox-label">
                    <input
                      type="checkbox"
                      checked={createForm.modelAuto}
                      onChange={(e) => setCreateForm({ ...createForm, modelAuto: e.target.checked })}
                    />
                    自动选择 (auto)
                  </label>
                  {!createForm.modelAuto && (
                    <div className="ag-model-inputs">
                      <select
                        className="ag-form-select font-mono"
                        value={createForm.modelProvider}
                        onChange={(e) => setCreateForm({ ...createForm, modelProvider: e.target.value, modelId: '' })}
                      >
                        <option value="">选择供应商</option>
                        {runtimeProviders.map((p) => (
                          <option key={p.provider_id} value={p.provider_id}>{p.provider_id}</option>
                        ))}
                      </select>
                      <select
                        className="ag-form-select font-mono"
                        value={createForm.modelId}
                        onChange={(e) => setCreateForm({ ...createForm, modelId: e.target.value })}
                        disabled={!runtimeProviders.find((p) => p.provider_id === createForm.modelProvider)?.models.length}
                      >
                        <option value="">选择模型</option>
                        {runtimeProviders.find((p) => p.provider_id === createForm.modelProvider)?.models.map((m) => (
                          <option key={m.id} value={m.id}>{m.id}</option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>
              </div>
              <div className="ag-form-group">
                <label className="ag-form-label">描述</label>
                <textarea
                  className="ag-form-textarea"
                  placeholder="描述智能体的角色和职责..."
                  value={createForm.description}
                  onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
                  rows={2}
                />
              </div>
              <div className="ag-form-group">
                <label className="ag-form-label">System Prompt</label>
                <textarea
                  className="ag-form-textarea ag-form-textarea-mono"
                  placeholder="描述智能体的行为规则..."
                  value={createForm.system_prompt}
                  onChange={(e) => setCreateForm({ ...createForm, system_prompt: e.target.value })}
                />
              </div>
              <div className="ag-form-group">
                <label className="ag-form-label">授权工具</label>
                <div className="ag-tool-checklist">
                  {tools.map((tool) => (
                    <label key={tool.tool_id} className="ag-tool-check-item">
                      <input
                        type="checkbox"
                        checked={createForm.allowed_tools.includes(tool.tool_id)}
                        onChange={(e) => {
                          if (e.target.checked) {
                            setCreateForm({ ...createForm, allowed_tools: [...createForm.allowed_tools, tool.tool_id] });
                          } else {
                            setCreateForm({ ...createForm, allowed_tools: createForm.allowed_tools.filter((t) => t !== tool.tool_id) });
                          }
                        }}
                      />
                      <span className="ag-tool-check-name">{tool.display_name}</span>
                      <span className="ag-tool-id font-mono">{tool.tool_id}</span>
                    </label>
                  ))}
                </div>
              </div>
              {createError && (
                <div className="ag-save-msg ag-save-msg-error">{createError}</div>
              )}
            </div>
            <div className="ag-modal-footer">
              <button className="btn-secondary btn-sm" onClick={() => setCreateOpen(false)}>取消</button>
              <button className="btn-primary btn-sm" onClick={handleCreate} disabled={creating}>
                {creating ? '创建中...' : '创建'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 删除确认 ── */}
      {deleteId && (
        <div className="ag-modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) setDeleteId(null); }}>
          <div className="ag-modal ag-modal-sm">
            <div className="ag-modal-body">
              <div className="ag-delete-title">删除智能体</div>
              <div className="ag-delete-desc">
                确定要删除智能体 <span className="font-mono">{deleteId}</span> 吗？此操作不可撤销。
              </div>
            </div>
            <div className="ag-modal-footer">
              <button className="btn-secondary btn-sm" onClick={() => setDeleteId(null)}>取消</button>
              <button className="ag-btn-danger-sm ag-btn-danger-full" onClick={handleDelete}>删除</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Toast ── */}
      {toast && (
        <div className={`ag-toast ag-toast-${toast.type}`}>{toast.text}</div>
      )}
    </div>
  );
}

export default AgentsPage;
