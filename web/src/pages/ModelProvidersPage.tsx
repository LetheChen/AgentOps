import { useState, useEffect, useCallback, useMemo } from 'react';
import { apiClient } from '../lib/api';
import type {
  RuntimeSummary,
  RuntimeProviderInfo,
  ModelInfo,
  ProviderTestResult,
} from '../lib/api';

// ═══════════════════════════════════════════════════════════════
//  模型供应商页（LLM 资源域）— 由 RuntimeSettingsPage 拆出：
//  供应商列表/详情/凭证/测试、模型配置、Fallback 链
// ═══════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════
//  Provider Catalog — 内置供应商预设（Layer 1，只读）
// ═══════════════════════════════════════════════════════════════
interface CatalogProvider {
  id: string;
  name: string;
  icon: string;
  base_url: string;
  protocol: string;
  auth_type: string;
  description: string;
  preset_models: ModelInfo[];
}

const PROVIDER_CATALOG: CatalogProvider[] = [
  {
    id: 'minimax',
    name: 'MiniMax',
    icon: 'M',
    base_url: 'https://api.minimaxi.com/v1',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
    description: 'MiniMax AI 大模型平台',
    preset_models: [
      { id: 'MiniMax-M3', max_tokens: 512000, price_input_per_1k: 0.0021, price_output_per_1k: 0.0084 },
      { id: 'MiniMax-M2.7', max_tokens: 8192, price_input_per_1k: 0.0021, price_output_per_1k: 0.0084 },
      { id: 'MiniMax-M2.7-highspeed', max_tokens: 8192, price_input_per_1k: 0.0042, price_output_per_1k: 0.0168 },
    ],
  },
  {
    id: 'openai',
    name: 'OpenAI',
    icon: 'O',
    base_url: 'https://api.openai.com/v1',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
    description: 'OpenAI GPT 系列模型',
    preset_models: [
      { id: 'gpt-4o', max_tokens: 4096, price_input_per_1k: 0.018, price_output_per_1k: 0.072 },
      { id: 'gpt-4o-mini', max_tokens: 16384, price_input_per_1k: 0.00108, price_output_per_1k: 0.00432 },
    ],
  },
  {
    id: 'anthropic',
    name: 'Anthropic',
    icon: 'A',
    base_url: 'https://api.anthropic.com/v1',
    protocol: 'anthropic_compatible',
    auth_type: 'x-api-key',
    description: 'Claude 系列模型',
    preset_models: [
      { id: 'claude-sonnet-4-20250514', max_tokens: 8192, price_input_per_1k: 0.0216, price_output_per_1k: 0.108 },
    ],
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    icon: 'D',
    base_url: 'https://api.deepseek.com/v1',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
    description: 'DeepSeek 开源模型',
    preset_models: [
      { id: 'deepseek-v4-flash', max_tokens: 384000, price_input_per_1k: 0.001, price_output_per_1k: 0.002 },
      { id: 'deepseek-v4-pro', max_tokens: 384000, price_input_per_1k: 0.003, price_output_per_1k: 0.006 },
      { id: 'deepseek-coder', max_tokens: 8192, price_input_per_1k: 0.001, price_output_per_1k: 0.002 },
    ],
  },
  {
    id: 'kimi',
    name: 'Kimi (月之暗面)',
    icon: 'K',
    base_url: 'https://api.moonshot.cn/v1',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
    description: 'Kimi 大模型平台',
    preset_models: [],
  },
  {
    id: 'glm',
    name: 'GLM (智谱)',
    icon: 'G',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
    description: '智谱 GLM 系列模型',
    preset_models: [],
  },
  {
    id: 'vllm',
    name: 'vLLM (本地)',
    icon: 'V',
    base_url: 'http://localhost:8000/v1',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
    description: '本地 vLLM 部署，OpenAI 兼容端点',
    preset_models: [],
  },
  {
    id: '火山方舟',
    name: '火山方舟',
    icon: '火',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
    description: '字节跳动火山方舟大模型服务平台（Agent Plan / Coding Plan）。模型 ID 为控制台创建的推理接入点 ID。',
    preset_models: [
      { id: 'doubao-seed-1-6-251015', max_tokens: 256000, price_input_per_1k: 0.0008, price_output_per_1k: 0.008 },
      { id: 'doubao-seed-1-6-flash', max_tokens: 256000, price_input_per_1k: 0.00015, price_output_per_1k: 0.0015 },
      { id: 'deepseek-r1', max_tokens: 128000, price_input_per_1k: 0.004, price_output_per_1k: 0.012 },
    ],
  },
];

// ═══════════════════════════════════════════════════════════════
//  常量映射
// ═══════════════════════════════════════════════════════════════
const PROTOCOL_LABELS: Record<string, string> = {
  openai_compatible: 'Chat Completions',
  anthropic_compatible: 'Anthropic Messages',
};

const AUTH_LABELS: Record<string, string> = {
  bearer: 'Bearer Token',
  'x-api-key': 'X-API-Key',
};

// ═══════════════════════════════════════════════════════════════
//  侧边栏选中类型
// ═══════════════════════════════════════════════════════════════
type Selection =
  | { type: 'provider'; id: string }
  | { type: 'models' }
  | { type: 'fallback' };

// ═══════════════════════════════════════════════════════════════
//  辅助函数
// ═══════════════════════════════════════════════════════════════

/** 判断 provider 是否为内置预设（catalog 中存在） */
function isBuiltinProvider(providerId: string): boolean {
  return PROVIDER_CATALOG.some((c) => c.id === providerId);
}

/** 从 catalog 获取 provider 的展示名称 */
function getProviderDisplayName(providerId: string): string {
  const cat = PROVIDER_CATALOG.find((c) => c.id === providerId);
  return cat ? cat.name : providerId;
}

/** 从 catalog 获取 provider 的 icon */
function getProviderIcon(providerId: string): string {
  const cat = PROVIDER_CATALOG.find((c) => c.id === providerId);
  return cat ? cat.icon : providerId.charAt(0).toUpperCase();
}

/** 凭证状态类型 */
type CredStatus = 'ok' | 'env' | 'none';

/** 获取凭证状态 */
function getCredStatus(p: RuntimeProviderInfo): CredStatus {
  if (p.has_credential) return 'ok';
  if (p.has_env_key) return 'env';
  return 'none';
}

/** 格式化时间戳 */
function formatTimestamp(ts: string | null): string {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    return d.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return ts;
  }
}

// ═══════════════════════════════════════════════════════════════
//  主组件
// ═══════════════════════════════════════════════════════════════
export function ModelProvidersPage() {
  // ── 数据状态 ──
  const [summary, setSummary] = useState<RuntimeSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection>({ type: 'models' });

  // ── 测试连接状态 ──
  const [testingProvider, setTestingProvider] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { state: 'testing' | 'success' | 'failed'; message: string; latency?: number }>>({});

  // ── Provider 编辑状态 ──
  const [editingProvider, setEditingProvider] = useState(false);
  const [editForm, setEditForm] = useState({ base_url: '', protocol: '', auth_type: '' });
  const [savingProvider, setSavingProvider] = useState(false);

  // ── 凭证管理状态 ──
  const [credModalProvider, setCredModalProvider] = useState<string | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [credSaving, setCredSaving] = useState(false);
  const [credError, setCredError] = useState('');
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  // ── 模型表单状态 ──
  const [modelFormOpen, setModelFormOpen] = useState(false);
  const [modelForm, setModelForm] = useState<ModelInfo>({
    id: '',
    max_tokens: 4096,
    price_input_per_1k: 0,
    price_output_per_1k: 0,
  });

  // ── Catalog 弹窗 ──
  const [catalogOpen, setCatalogOpen] = useState(false);

  // ── 模型选择下拉 ──
  const [defaultModelProvider, setDefaultModelProvider] = useState<string>('');
  const [defaultModelId, setDefaultModelId] = useState<string>('');
  const [managerModelProvider, setManagerModelProvider] = useState<string>('');
  const [managerModelId, setManagerModelId] = useState<string>('');
  const [savingModel, setSavingModel] = useState<'default' | 'manager' | null>(null);

  // ── 拉取模型 ──
  const [fetchingModels, setFetchingModels] = useState<string | null>(null);
  const [fetchedModels, setFetchedModels] = useState<Record<string, Array<{ id: string }>>>({});
  const [fetchError, setFetchError] = useState<string | null>(null);

  // ── 自定义供应商表单 ──
  const [catalogTab, setCatalogTab] = useState<'presets' | 'custom'>('presets');
  const [customForm, setCustomForm] = useState({
    provider_id: '',
    base_url: '',
    protocol: 'openai_compatible',
    auth_type: 'bearer',
  });
  const [creatingProvider, setCreatingProvider] = useState(false);

  // ── Fallback 链编辑器状态 ──
  // null → modal 关闭；非空 → 编辑中（primary 已存在 = 编辑；primary 为空 / isNew=true = 新建）
  const [fallbackModal, setFallbackModal] = useState<{
    primary: string;
    entries: Array<{ provider: string; model: string | null }>;
    isNew: boolean;
  } | null>(null);
  const [savingFallback, setSavingFallback] = useState(false);
  const [fallbackError, setFallbackError] = useState<string | null>(null);

  // ════════════════════════════════════════════════════════════
  //  数据加载
  // ════════════════════════════════════════════════════════════
  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await apiClient.getRuntimeSummary();
      setSummary(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // ── 当前选中的 provider 信息 ──
  const selectedProvider = useMemo(() => {
    if (selection.type !== 'provider' || !summary) return null;
    return summary.providers.find((p) => p.provider_id === selection.id) ?? null;
  }, [selection, summary]);

  // ════════════════════════════════════════════════════════════
  //  测试连接
  // ════════════════════════════════════════════════════════════
  const handleTestProvider = useCallback(async (providerId: string, mode: 'api' | 'token' = 'api') => {
    setTestingProvider(`${providerId}:${mode}`);
    setTestResults((prev) => ({
      ...prev,
      [`${providerId}:${mode}`]: { state: 'testing', message: mode === 'token' ? '正在校验凭证...' : '正在检测连接...' },
    }));
    try {
      const result: ProviderTestResult = await apiClient.testProvider(providerId, mode);
      const isOk = result.status === 'ok' || (result as unknown as Record<string, unknown>).ok === true;
      setTestResults((prev) => ({
        ...prev,
        [`${providerId}:${mode}`]: {
          state: isOk ? 'success' : 'failed',
          message: isOk
            ? mode === 'token'
              ? result.detail || '凭证校验通过'
              : `连接成功 (${result.latency_ms ?? 0}ms)`
            : `失败: ${result.error || '未知错误'}`,
          latency: result.latency_ms,
        },
      }));
    } catch (e) {
      setTestResults((prev) => ({
        ...prev,
        [`${providerId}:${mode}`]: {
          state: 'failed',
          message: e instanceof Error ? e.message : String(e),
        },
      }));
    } finally {
      setTestingProvider(null);
    }
  }, []);

  // ════════════════════════════════════════════════════════════
  //  Provider 编辑
  // ════════════════════════════════════════════════════════════
  const handleStartEdit = useCallback((p: RuntimeProviderInfo) => {
    setEditingProvider(true);
    setEditForm({ base_url: p.base_url, protocol: p.protocol, auth_type: p.auth_type });
  }, []);

  const handleSaveProvider = useCallback(
    async (providerId: string) => {
      setSavingProvider(true);
      try {
        await apiClient.updateRuntimeProvider(providerId, {
          base_url: editForm.base_url,
          protocol: editForm.protocol,
          auth_type: editForm.auth_type,
        });
        setEditingProvider(false);
        await loadData();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSavingProvider(false);
      }
    },
    [editForm, loadData],
  );

  // ════════════════════════════════════════════════════════════
  //  凭证管理
  // ════════════════════════════════════════════════════════════
  const handleOpenCredModal = useCallback((providerId: string) => {
    setCredModalProvider(providerId);
    setApiKeyInput('');
    setCredError('');
  }, []);

  const handleSaveCredential = useCallback(async () => {
    if (!credModalProvider || !apiKeyInput.trim()) return;
    setCredSaving(true);
    setCredError('');
    try {
      await apiClient.setProviderCredential(credModalProvider, apiKeyInput.trim());
      setCredModalProvider(null);
      setApiKeyInput('');
      await loadData();
    } catch (e) {
      setCredError(e instanceof Error ? e.message : String(e));
    } finally {
      setCredSaving(false);
    }
  }, [credModalProvider, apiKeyInput, loadData]);

  const handleDeleteCredential = useCallback(async () => {
    if (!deleteConfirmId) return;
    try {
      await apiClient.deleteProviderCredential(deleteConfirmId);
      setDeleteConfirmId(null);
      await loadData();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [deleteConfirmId, loadData]);

  // ════════════════════════════════════════════════════════════
  //  模型 CRUD
  // ════════════════════════════════════════════════════════════
  const handleOpenModelForm = useCallback(() => {
    setModelForm({ id: '', max_tokens: 4096, price_input_per_1k: 0, price_output_per_1k: 0 });
    setModelFormOpen(true);
  }, []);

  const handleSaveModel = useCallback(
    async (providerId: string) => {
      if (!modelForm.id.trim()) return;
      try {
        await apiClient.addModel(providerId, {
          ...modelForm,
          id: modelForm.id.trim(),
        });
        setModelFormOpen(false);
        await loadData();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [modelForm, loadData],
  );

  const handleDeleteModel = useCallback(
    async (providerId: string, modelId: string) => {
      try {
        await apiClient.deleteModel(providerId, modelId);
        await loadData();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [loadData],
  );

  // ════════════════════════════════════════════════════════════
  //  从 catalog 添加预设模型
  // ════════════════════════════════════════════════════════════
  const handleAddPresetModel = useCallback(
    async (providerId: string, model: ModelInfo) => {
      try {
        await apiClient.addModel(providerId, model);
        await loadData();
      } catch (e) {
        // 模型可能已存在，忽略错误
        const msg = e instanceof Error ? e.message : String(e);
        if (!msg.includes('already') && !msg.includes('exist')) {
          setError(msg);
        }
      }
    },
    [loadData],
  );

  // ════════════════════════════════════════════════════════════
  //  保存默认模型 / Manager 模型
  // ════════════════════════════════════════════════════════════
  const handleSetDefaultModel = useCallback(
    async (providerId: string, modelId: string) => {
      setSavingModel('default');
      try {
        await apiClient.setDefaultModel(providerId, modelId);
        await loadData();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSavingModel(null);
      }
    },
    [loadData],
  );

  const handleSetManagerModel = useCallback(
    async (providerId: string, modelId: string) => {
      setSavingModel('manager');
      try {
        await apiClient.setManagerModel(providerId, modelId);
        await loadData();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSavingModel(null);
      }
    },
    [loadData],
  );

  // ════════════════════════════════════════════════════════════
  //  创建自定义供应商
  // ════════════════════════════════════════════════════════════
  const handleCreateCustomProvider = useCallback(async () => {
    if (!customForm.provider_id.trim() || !customForm.base_url.trim()) return;
    setCreatingProvider(true);
    try {
      await apiClient.createProvider({
        provider_id: customForm.provider_id.trim(),
        base_url: customForm.base_url.trim(),
        protocol: customForm.protocol,
        auth_type: customForm.auth_type,
      });
      setCatalogOpen(false);
      setCustomForm({ provider_id: '', base_url: '', protocol: 'openai_compatible', auth_type: 'bearer' });
      setCatalogTab('presets');
      await loadData();
      // 自动选中新建的 provider
      setSelection({ type: 'provider', id: customForm.provider_id.trim() });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreatingProvider(false);
    }
  }, [customForm, loadData]);

  // ════════════════════════════════════════════════════════════
  //  从供应商 API 拉取可用模型
  // ════════════════════════════════════════════════════════════
  const handleFetchModels = useCallback(async (providerId: string) => {
    setFetchingModels(providerId);
    setFetchError(null);
    try {
      const result = await apiClient.fetchProviderModels(providerId);
      if (result.ok && result.models.length > 0) {
        setFetchedModels((prev) => ({ ...prev, [providerId]: result.models }));
      } else if (!result.ok) {
        setFetchError(result.error || '拉取失败');
      }
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : String(e));
    } finally {
      setFetchingModels(null);
    }
  }, []);

  // ════════════════════════════════════════════════════════════
  //  删除供应商（含模型、凭证、引用）
  // ════════════════════════════════════════════════════════════
  const [deletingProvider, setDeletingProvider] = useState(false);
  const handleDeleteProvider = useCallback(async (providerId: string) => {
    setDeletingProvider(true);
    try {
      await apiClient.deleteProvider(providerId);
      setSelection({ type: 'models' });
      await loadData();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingProvider(false);
    }
  }, [loadData]);

  // ════════════════════════════════════════════════════════════
  //  Fallback 链编辑
  // ════════════════════════════════════════════════════════════

  /** 打开「新建链」编辑器。primary 留空，让用户填。 */
  const handleNewFallbackChain = useCallback(() => {
    const available = summary?.providers ?? [];
    const firstWithConfig = available.find((p) => p.base_url) ?? available[0];
    setFallbackError(null);
    setFallbackModal({
      primary: firstWithConfig?.provider_id ?? '',
      entries: firstWithConfig
        ? [{ provider: available.find((p) => p.provider_id !== firstWithConfig.provider_id)?.provider_id ?? '', model: null }]
        : [],
      isNew: true,
    });
  }, [summary]);

  /** 打开「编辑链」编辑器，已有 entries 一字不差灌进去。 */
  const handleEditFallbackChain = useCallback((primary: string) => {
    const existing = summary?.fallback_chains[primary] ?? [];
    setFallbackError(null);
    setFallbackModal({
      primary,
      entries: existing.map((e) => ({ provider: e.provider, model: e.model })),
      isNew: false,
    });
  }, [summary]);

  /** 关闭编辑器。 */
  const handleCloseFallbackModal = useCallback(() => {
    setFallbackModal(null);
    setSavingFallback(false);
    setFallbackError(null);
  }, []);

  /**
   * 保存整个 fallback_chains 字典。PUT 全字段语义（参考 /api/runtime/manager-model），
   * 前端不需要 delta merge，直接从当前 summary 出发构造下一版本。
   */
  const handleSaveFallbackChains = useCallback(async () => {
    if (!fallbackModal || !summary) return;
    setFallbackError(null);
    // 校验
    if (!fallbackModal.primary) {
      setFallbackError('请选择主 provider');
      return;
    }
    const trimmed = fallbackModal.entries
      .map((e) => ({ provider: e.provider.trim(), model: e.model }))
      .filter((e) => e.provider);
    if (trimmed.length === 0) {
      setFallbackError('至少添加 1 个 fallback 目标');
      return;
    }
    if (trimmed.some((e) => e.provider === fallbackModal.primary)) {
      setFallbackError('不能把自己设为 fallback');
      return;
    }
    setSavingFallback(true);
    try {
      // 1) 构造下一版本字典：保留现有链（跳过正在编辑的那条），再灌入本次编辑结果
      const next: Record<string, Array<string | { provider: string; model: string | null }>> = {};
      for (const [pid, chain] of Object.entries(summary.fallback_chains)) {
        if (pid === fallbackModal.primary) continue;
        next[pid] = chain.map((entry) =>
          entry.model
            ? { provider: entry.provider, model: entry.model }
            : entry.provider,
        );
      }
      // 2) 加入本次编辑结果。model === null 时只写 provider 字符串（YAML 更紧凑）
      next[fallbackModal.primary] = trimmed.map((e) =>
        e.model ? { provider: e.provider, model: e.model } : e.provider,
      );
      const resp = await apiClient.updateFallbackChains(next);
      // 3) 用响应回写 summary.fallback_chains（避免再次 GET）
      if (summary) {
        const normalized: Record<string, Array<{ provider: string; model: string | null }>> = {};
        for (const [pid, entries] of Object.entries(resp.chains)) {
          normalized[pid] = entries.map((e) => ({ provider: e.provider, model: e.model }));
        }
        // 但必须在原对象上 mutate，触发 React re-render。
        // setSummary 写法更安全。构造一个新 summary 对象。
        setSummary({ ...summary, fallback_chains: normalized });
      }
      setFallbackModal(null);
      setSavingFallback(false);
    } catch (e) {
      setSavingFallback(false);
      setFallbackError(e instanceof Error ? e.message : String(e));
    }
  }, [fallbackModal, summary]);

  /**
   * 删除某条链。直接 PUT 一份「去掉这条链」的字典。
   */
  const handleDeleteFallbackChain = useCallback(async (primary: string) => {
    if (!summary) return;
    if (!window.confirm(`确认删除主 provider「${primary}」的整条 fallback 链？`)) return;
    setSavingFallback(true);
    try {
      const next: Record<string, Array<string | { provider: string; model: string | null }>> = {};
      for (const [pid, chain] of Object.entries(summary.fallback_chains)) {
        if (pid === primary) continue;
        next[pid] = chain.map((entry) =>
          entry.model
            ? { provider: entry.provider, model: entry.model }
            : entry.provider,
        );
      }
      const resp = await apiClient.updateFallbackChains(next);
      const normalized: Record<string, Array<{ provider: string; model: string | null }>> = {};
      for (const [pid, entries] of Object.entries(resp.chains)) {
        normalized[pid] = entries.map((e) => ({ provider: e.provider, model: e.model }));
      }
      setSummary({ ...summary, fallback_chains: normalized });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingFallback(false);
    }
  }, [summary]);

  // ════════════════════════════════════════════════════════════
  //  渲染：加载中
  // ════════════════════════════════════════════════════════════
  if (loading && !summary) {
    return (
      <div className="rs-loading">
        <div className="rs-spinner" />
        <span>正在加载模型供应商...</span>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：错误
  // ════════════════════════════════════════════════════════════
  if (error && !summary) {
    return (
      <div className="rs-error-state">
        <div className="rs-error-icon">!</div>
        <div className="rs-error-text">加载失败: {error}</div>
        <button className="btn-secondary btn-sm" onClick={loadData}>
          重试
        </button>
      </div>
    );
  }

  if (!summary) return null;

  // 非 null 常量，供嵌套渲染函数安全引用（TypeScript 对 const 保留类型收窄）
  const s = summary;

  // ════════════════════════════════════════════════════════════
  //  渲染：侧边栏
  // ════════════════════════════════════════════════════════════
  function renderSidebar() {
    return (
      <aside className="rs-sidebar">
        {/* 供应商 */}
        <div className="rs-sidebar-section">
          <div className="rs-sidebar-section-header">
            <span className="rs-sidebar-section-title">供应商</span>
            <button
              className="rs-sidebar-add-btn"
              onClick={() => setCatalogOpen(true)}
              title="添加供应商"
            >
              +
            </button>
          </div>
          <div className="rs-sidebar-provider-list">
            {s.providers.length === 0 ? (
              <div className="rs-sidebar-empty">暂无供应商</div>
            ) : (
              s.providers.map((p) => {
                const credStatus = getCredStatus(p);
                const isActive = selection.type === 'provider' && selection.id === p.provider_id;
                return (
                  <div
                    key={p.provider_id}
                    className={`rs-sidebar-item rs-sidebar-provider-item ${isActive ? 'active' : ''}`}
                    onClick={() => setSelection({ type: 'provider', id: p.provider_id })}
                  >
                    <span className={`rs-cred-dot rs-cred-dot-${credStatus}`} />
                    <span className="rs-provider-icon-sm">{getProviderIcon(p.provider_id)}</span>
                    <span className="rs-sidebar-provider-name">{getProviderDisplayName(p.provider_id)}</span>
                    <span className="rs-sidebar-badge">{p.models.length}</span>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* 模型配置 & Fallback */}
        <div className="rs-sidebar-section">
          <div
            className={`rs-sidebar-item ${selection.type === 'models' ? 'active' : ''}`}
            onClick={() => setSelection({ type: 'models' })}
          >
            <SidebarIcon type="model" />
            <span>模型配置</span>
          </div>
          <div
            className={`rs-sidebar-item ${selection.type === 'fallback' ? 'active' : ''}`}
            onClick={() => setSelection({ type: 'fallback' })}
          >
            <SidebarIcon type="fallback" />
            <span>Fallback 链</span>
            <span className="rs-sidebar-badge">
              {Object.keys(s.fallback_chains).length}
            </span>
          </div>
        </div>
      </aside>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：供应商详情面板
  // ════════════════════════════════════════════════════════════
  function renderProviderPanel(p: RuntimeProviderInfo) {
    const credStatus = getCredStatus(p);
    const testStateToken = testResults[`${p.provider_id}:token`];
    const testStateApi = testResults[`${p.provider_id}:api`];
    const isBuiltin = isBuiltinProvider(p.provider_id);
    const catalogEntry = PROVIDER_CATALOG.find((c) => c.id === p.provider_id);

    // 找出 catalog 中有但 provider 尚未配置的预设模型
    const existingModelIds = new Set(p.models.map((m) => m.id));
    const availablePresets = catalogEntry?.preset_models.filter((m) => !existingModelIds.has(m.id)) ?? [];

    return (
      <div className="rs-panel">
        {/* 面板头部 */}
        <div className="rs-panel-header">
          <div className="rs-panel-header-left">
            <div className="rs-provider-icon-lg">{getProviderIcon(p.provider_id)}</div>
            <div>
              <h2 className="rs-panel-title">{getProviderDisplayName(p.provider_id)}</h2>
              <p className="rs-panel-subtitle font-mono">{p.provider_id}</p>
            </div>
          </div>
          <div className="rs-test-btn-group">
            <button
              className="btn-secondary btn-sm"
              onClick={() => handleTestProvider(p.provider_id, 'token')}
              disabled={testingProvider === `${p.provider_id}:token`}
              title="仅校验凭证存在性和格式，不发网络请求"
            >
              {testingProvider === `${p.provider_id}:token` ? (
                <>
                  <span className="rs-btn-spinner" />
                  校验中...
                </>
              ) : (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                  凭证校验
                </>
              )}
            </button>
            <button
              className="btn-secondary btn-sm"
              onClick={() => handleTestProvider(p.provider_id, 'api')}
              disabled={testingProvider === `${p.provider_id}:api`}
              title="直接调用 GET /models 检查实际连通性"
            >
              {testingProvider === `${p.provider_id}:api` ? (
                <>
                  <span className="rs-btn-spinner" />
                  检测中...
                </>
              ) : (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
                  </svg>
                  API 连接
                </>
              )}
            </button>
            <button
              className="btn-danger-outline btn-sm"
              onClick={() => {
                if (window.confirm(`确认删除供应商「${getProviderDisplayName(p.provider_id)}」？\n\n将同时删除：\n• 该供应商的全部模型配置\n• 已录入的 API Key 凭证\n• default / manager_model / fallback 链中的引用\n\n此操作不可撤销。`)) {
                  handleDeleteProvider(p.provider_id);
                }
              }}
              disabled={deletingProvider}
              title="删除此供应商（含模型、凭证、引用）"
            >
              {deletingProvider ? (
                <>
                  <span className="rs-btn-spinner" />
                  删除中...
                </>
              ) : (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /><line x1="10" y1="11" x2="10" y2="17" /><line x1="14" y1="11" x2="14" y2="17" />
                  </svg>
                  删除
                </>
              )}
            </button>
          </div>
        </div>

        {/* 错误提示 */}
        {error && (
          <div className="rs-error-banner" onClick={() => setError(null)}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            {error}
          </div>
        )}

        <div className="rs-panel-body">
          {/* ── 基本配置 ── */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">基本配置</span>
              {isBuiltin ? (
                <span className="status-pill status-pill-neutral">内置预设</span>
              ) : (
                !editingProvider && (
                  <button className="btn-secondary btn-sm" onClick={() => handleStartEdit(p)}>
                    编辑
                  </button>
                )
              )}
            </div>
            <div className="rs-config-grid">
              {editingProvider && !isBuiltin ? (
                <>
                  <div className="rs-config-row rs-config-row-edit">
                    <label className="rs-config-label">Base URL</label>
                    <input
                      className="input-base"
                      value={editForm.base_url}
                      onChange={(e) => setEditForm((f) => ({ ...f, base_url: e.target.value }))}
                      style={{ flex: 1 }}
                    />
                  </div>
                  <div className="rs-config-row rs-config-row-edit">
                    <label className="rs-config-label">Protocol</label>
                    <select
                      className="input-base"
                      value={editForm.protocol}
                      onChange={(e) => setEditForm((f) => ({ ...f, protocol: e.target.value }))}
                      style={{ flex: 1 }}
                    >
                      <option value="openai_compatible">openai_compatible</option>
                      <option value="anthropic_compatible">anthropic_compatible</option>
                    </select>
                  </div>
                  <div className="rs-config-row rs-config-row-edit">
                    <label className="rs-config-label">Auth Type</label>
                    <select
                      className="input-base"
                      value={editForm.auth_type}
                      onChange={(e) => setEditForm((f) => ({ ...f, auth_type: e.target.value }))}
                      style={{ flex: 1 }}
                    >
                      <option value="bearer">bearer</option>
                      <option value="x-api-key">x-api-key</option>
                    </select>
                  </div>
                  <div className="rs-config-actions">
                    <button
                      className="btn-primary btn-sm"
                      onClick={() => handleSaveProvider(p.provider_id)}
                      disabled={savingProvider}
                    >
                      {savingProvider ? '保存中...' : '保存'}
                    </button>
                    <button className="btn-secondary btn-sm" onClick={() => setEditingProvider(false)}>
                      取消
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div className="rs-config-row">
                    <span className="rs-config-label">Base URL</span>
                    <span className="rs-config-value font-mono">{p.base_url}</span>
                  </div>
                  <div className="rs-config-row">
                    <span className="rs-config-label">Protocol</span>
                    <span className="rs-config-value">
                      <span className="status-pill status-pill-neutral font-mono">
                        {PROTOCOL_LABELS[p.protocol] || p.protocol}
                      </span>
                    </span>
                  </div>
                  <div className="rs-config-row">
                    <span className="rs-config-label">Auth Type</span>
                    <span className="rs-config-value">
                      <span className="status-pill status-pill-neutral font-mono">
                        {AUTH_LABELS[p.auth_type] || p.auth_type}
                      </span>
                    </span>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* ── 凭证管理 ── */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">凭证管理</span>
            </div>
            <div className="rs-cred-status-row">
              <div className="rs-cred-status-info">
                <span className={`rs-cred-dot rs-cred-dot-${credStatus}`} style={{ width: 10, height: 10 }} />
                <span className="rs-cred-status-text">
                  {credStatus === 'ok' && '已配置'}
                  {credStatus === 'env' && 'ENV 变量已配置'}
                  {credStatus === 'none' && '未配置'}
                </span>
                {p.credential_updated_at && (
                  <span className="rs-cred-status-time font-mono">
                    {formatTimestamp(p.credential_updated_at)}
                  </span>
                )}
              </div>
              <div className="rs-cred-actions">
                <button
                  className="btn-primary btn-sm"
                  onClick={() => handleOpenCredModal(p.provider_id)}
                >
                  {p.has_credential ? '更新 API Key' : '录入 API Key'}
                </button>
                {p.has_credential && (
                  <button
                    className="btn-secondary btn-sm rs-btn-danger"
                    onClick={() => setDeleteConfirmId(p.provider_id)}
                  >
                    删除凭证
                  </button>
                )}
              </div>
            </div>
            <div className="rs-cred-hint">
              API Key 通过 Fernet 加密存储到本地，不会以明文落盘或上传第三方。
            </div>
          </div>

          {/* ── 模型列表 ── */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">
                模型列表 <span className="rs-count-badge">{p.models.length}</span>
              </span>
              <div className="rs-model-list-actions">
                <button
                  className="btn-secondary btn-sm"
                  onClick={() => handleFetchModels(p.provider_id)}
                  disabled={fetchingModels === p.provider_id}
                  title="从服务商 API 拉取可用模型列表"
                >
                  {fetchingModels === p.provider_id ? (
                    <><span className="rs-btn-spinner" /> 拉取中...</>
                  ) : (
                    <><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" /><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" /></svg> 拉取模型</>
                  )}
                </button>
                <button className="btn-primary btn-sm" onClick={handleOpenModelForm}>
                  + 添加模型
                </button>
              </div>
            </div>
            {fetchError && p.provider_id === fetchingModels && (
              <div className="rs-error-banner" style={{ margin: '8px 16px 0' }} onClick={() => setFetchError(null)}>
                {fetchError}
              </div>
            )}
            {/* 拉取到的新模型 — 未添加的显示为可点击标签 */}
            {(() => {
              const remoteModels = fetchedModels[p.provider_id] || [];
              const existingIds = new Set(p.models.map((m) => m.id));
              const newRemoteModels = remoteModels.filter((m) => !existingIds.has(m.id));
              if (newRemoteModels.length === 0) return null;
              return (
                <div className="rs-fetched-models">
                  <span className="rs-fetched-label">从服务商拉取到 {newRemoteModels.length} 个新模型，点击添加：</span>
                  <div className="rs-fetched-tags">
                    {newRemoteModels.map((m) => (
                      <span
                        key={m.id}
                        className="rs-model-tag rs-tag-new"
                        title="点击添加此模型"
                        onClick={() => {
                          apiClient.addModel(p.provider_id, { id: m.id }).then(() => loadData());
                        }}
                      >
                        + {m.id}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })()}
            {p.models.length === 0 ? (
              <div className="widget-empty-state">
                尚未配置模型，点击"添加模型"或"拉取模型"从服务商获取
              </div>
            ) : (
              <div className="rs-model-list">
                {p.models.map((m) => {
                  const isDefault = s.default_provider === p.provider_id && s.default_model === m.id;
                  const isManager = s.manager_provider === p.provider_id && s.manager_model === m.id;
                  return (
                    <div key={m.id} className="rs-model-row">
                      <div className="rs-model-row-main">
                        <span className="rs-model-id font-mono">{m.id}</span>
                        <div className="rs-model-tags">
                          {isDefault && <span className="status-pill status-pill-success">默认</span>}
                          {isManager && <span className="status-pill status-pill-info">Manager</span>}
                        </div>
                      </div>
                      <div className="rs-model-row-meta">
                        <span className="rs-model-meta-item">
                          max: <strong className="font-mono">{m.max_tokens?.toLocaleString() || '-'}</strong>
                        </span>
                        <span className="rs-model-meta-item">
                          <strong className="font-mono">¥{m.price_input_per_1k ?? '-'}</strong>
                          {' / '}
                          <strong className="font-mono">¥{m.price_output_per_1k ?? '-'}</strong>
                          {' /1K'}
                        </span>
                      </div>
                      <button
                        className="rs-model-delete-btn"
                        onClick={() => handleDeleteModel(p.provider_id, m.id)}
                        title="删除模型"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
            {/* 预设模型快捷添加 */}
            {availablePresets.length > 0 && (
              <div className="rs-preset-models">
                <div className="rs-preset-models-title">可用预设模型</div>
                <div className="rs-preset-models-list">
                  {availablePresets.map((m) => (
                    <button
                      key={m.id}
                      className="rs-preset-model-chip"
                      onClick={() => handleAddPresetModel(p.provider_id, m)}
                    >
                      + {m.id}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* ── 健康状态 ── */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">健康状态</span>
            </div>
            <div className="rs-health-results">
              {/* 凭证校验结果 */}
              <div className="rs-health-row">
                <span className="rs-health-row-label">凭证校验</span>
                {testStateToken ? (
                  <div className={`rs-health-result rs-health-${testStateToken.state}`}>
                    <span className={`rs-cred-dot rs-cred-dot-${testStateToken.state === 'success' ? 'ok' : testStateToken.state === 'failed' ? 'none' : 'env'}`} style={{ width: 10, height: 10 }} />
                    <span className="rs-health-text">{testStateToken.message}</span>
                  </div>
                ) : (
                  <span className="rs-health-idle-inline">未检测</span>
                )}
              </div>
              {/* API 连接结果 */}
              <div className="rs-health-row">
                <span className="rs-health-row-label">API 连接</span>
                {testStateApi ? (
                  <div className={`rs-health-result rs-health-${testStateApi.state}`}>
                    <span className={`rs-cred-dot rs-cred-dot-${testStateApi.state === 'success' ? 'ok' : testStateApi.state === 'failed' ? 'none' : 'env'}`} style={{ width: 10, height: 10 }} />
                    <span className="rs-health-text">{testStateApi.message}</span>
                  </div>
                ) : (
                  <span className="rs-health-idle-inline">未检测</span>
                )}
              </div>
              {!testStateToken && !testStateApi && (
                <div className="rs-health-idle">
                  点击上方"凭证校验"或"API 连接"检测该供应商的可用性
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：全局模型配置面板
  // ════════════════════════════════════════════════════════════
  function renderModelsPanel() {
    return (
      <div className="rs-panel">
        <div className="rs-panel-header">
          <div>
            <h2 className="rs-panel-title">全局模型配置</h2>
            <p className="rs-panel-subtitle">默认模型与 Manager 模型配置（可在线编辑，保存到 models.yaml）</p>
          </div>
          <span className="status-pill status-pill-success">可编辑</span>
        </div>
        <div className="rs-panel-body">
          {/* 默认模型 — 级联下拉 */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">默认模型 (default)</span>
              {savingModel === 'default' && <span className="status-pill status-pill-info">保存中...</span>}
            </div>
            <div className="rs-cascade-row">
              <div className="rs-cascade-field">
                <label className="rs-cascade-label">供应商</label>
                <select
                  className="rs-cascade-select font-mono"
                  value={defaultModelProvider || s.default_provider || ''}
                  onChange={(e) => {
                    setDefaultModelProvider(e.target.value);
                    setDefaultModelId('');
                  }}
                >
                  <option value="">选择供应商</option>
                  {s.providers.map((p) => (
                    <option key={p.provider_id} value={p.provider_id}>{getProviderDisplayName(p.provider_id)}</option>
                  ))}
                </select>
              </div>
              <div className="rs-cascade-arrow">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </div>
              <div className="rs-cascade-field">
                <label className="rs-cascade-label">模型</label>
                <select
                  className="rs-cascade-select font-mono"
                  value={defaultModelId || s.default_model || ''}
                  onChange={(e) => {
                    const providerId = defaultModelProvider || s.default_provider || '';
                    if (providerId && e.target.value) {
                      handleSetDefaultModel(providerId, e.target.value);
                    }
                  }}
                >
                  <option value="">选择模型</option>
                  {(s.providers.find((p) => p.provider_id === (defaultModelProvider || s.default_provider))?.models ?? []).map((m) => (
                    <option key={m.id} value={m.id}>{m.id}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="rs-config-hint" style={{ marginTop: '6px' }}>
              当前: <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>{s.default_provider || '—'} / {s.default_model || '—'}</span>，选择后自动保存到 models.yaml。
            </div>
          </div>

          {/* Manager 模型 — 级联下拉 */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">Manager 模型</span>
              {savingModel === 'manager' && <span className="status-pill status-pill-info">保存中...</span>}
            </div>
            <div className="rs-cascade-row">
              <div className="rs-cascade-field">
                <label className="rs-cascade-label">供应商</label>
                <select
                  className="rs-cascade-select font-mono"
                  value={managerModelProvider || s.manager_provider || ''}
                  onChange={(e) => {
                    setManagerModelProvider(e.target.value);
                    setManagerModelId('');
                  }}
                >
                  <option value="">选择供应商</option>
                  {s.providers.map((p) => (
                    <option key={p.provider_id} value={p.provider_id}>{getProviderDisplayName(p.provider_id)}</option>
                  ))}
                </select>
              </div>
              <div className="rs-cascade-arrow">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </div>
              <div className="rs-cascade-field">
                <label className="rs-cascade-label">模型</label>
                <select
                  className="rs-cascade-select font-mono"
                  value={managerModelId || s.manager_model || ''}
                  onChange={(e) => {
                    const providerId = managerModelProvider || s.manager_provider || '';
                    if (providerId && e.target.value) {
                      handleSetManagerModel(providerId, e.target.value);
                    }
                  }}
                >
                  <option value="">选择模型</option>
                  {(s.providers.find((p) => p.provider_id === (managerModelProvider || s.manager_provider))?.models ?? []).map((m) => (
                    <option key={m.id} value={m.id}>{m.id}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="rs-config-hint" style={{ marginTop: '6px' }}>
              当前: <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>{s.manager_provider || '—'} / {s.manager_model || '—'}</span>，Manager Agent 用于任务路由和智能体调度。
            </div>
          </div>

          {/* 可用模型 — 卡片式布局，仅显示已配置凭证的供应商 */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">可用模型</span>
              <span className="rs-config-label" style={{ textTransform: 'none', letterSpacing: '0' }}>
                已配置 {s.providers.filter((p) => getCredStatus(p) !== 'none').length} / {s.providers.length} 个供应商
              </span>
            </div>
            {fetchError && (
              <div className="rs-error-banner" style={{ margin: '12px 16px 0' }} onClick={() => setFetchError(null)}>
                {fetchError}
              </div>
            )}
            <div className="rs-model-cards">
              {/* 已配置凭证的供应商 — 卡片展示 */}
              {s.providers.filter((p) => getCredStatus(p) !== 'none').map((p) => {
                const isCustom = !isBuiltinProvider(p.provider_id);
                const remoteModels = fetchedModels[p.provider_id] || [];
                const existingIds = new Set(p.models.map((m) => m.id));
                const newRemoteModels = remoteModels.filter((m) => !existingIds.has(m.id));
                return (
                  <div key={p.provider_id} className="rs-model-card">
                    <div className="rs-model-card-header">
                      <div className="rs-model-card-provider">
                        <span className="rs-provider-icon-sm">{getProviderIcon(p.provider_id)}</span>
                        <span className="rs-model-card-name">{getProviderDisplayName(p.provider_id)}</span>
                        {isCustom && <span className="status-pill status-pill-info" style={{ fontSize: '10px', padding: '1px 6px' }}>自定义</span>}
                        <span className="rs-model-card-count">{p.models.length} 个模型</span>
                      </div>
                      <button
                        className="rs-model-card-fetch"
                        onClick={() => handleFetchModels(p.provider_id)}
                        disabled={fetchingModels === p.provider_id}
                        title="从服务商 API 拉取可用模型"
                      >
                        {fetchingModels === p.provider_id ? (
                          <span className="rs-btn-spinner" />
                        ) : (
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" /><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" /></svg>
                        )}
                      </button>
                    </div>
                    <div className="rs-model-card-body">
                      {p.models.length === 0 && newRemoteModels.length === 0 ? (
                        <span className="rs-model-card-empty">无模型，点击右上角按钮从服务商拉取</span>
                      ) : (
                        <div className="rs-model-card-tags">
                          {p.models.map((m) => {
                            const isDefault = s.default_provider === p.provider_id && s.default_model === m.id;
                            const isManager = s.manager_provider === p.provider_id && s.manager_model === m.id;
                            return (
                              <span key={m.id} className={`rs-model-tag ${isDefault ? 'rs-tag-default' : ''} ${isManager ? 'rs-tag-manager' : ''}`}>
                                {isDefault && <span className="rs-tag-dot rs-tag-dot-default" />}
                                {isManager && <span className="rs-tag-dot rs-tag-dot-manager" />}
                                {m.id}
                              </span>
                            );
                          })}
                          {newRemoteModels.map((m) => (
                            <span
                              key={m.id}
                              className="rs-model-tag rs-tag-new"
                              title="点击添加此模型"
                              onClick={() => {
                                apiClient.addModel(p.provider_id, { id: m.id }).then(() => loadData());
                              }}
                            >
                              + {m.id}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
              {/* 未配置凭证的供应商 — 折叠提示 */}
              {(() => {
                const unconfigured = s.providers.filter((p) => getCredStatus(p) === 'none');
                if (unconfigured.length === 0) return null;
                return (
                  <div className="rs-model-card rs-model-card-muted">
                    <span className="rs-model-card-empty">
                      {unconfigured.map((p) => getProviderDisplayName(p.provider_id)).join('、')} 未配置凭证，配置 API Key 后可在此拉取模型
                    </span>
                  </div>
                );
              })()}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：Fallback 链面板（可编辑：新建/编辑/删除）
  // ════════════════════════════════════════════════════════════
  function renderFallbackPanel() {
    const chains = s.fallback_chains;
    const chainEntries = Object.entries(chains);

    return (
      <div className="rs-panel">
        <div className="rs-panel-header">
          <div>
            <h2 className="rs-panel-title">Fallback 链配置</h2>
            <p className="rs-panel-subtitle">
              当主 provider 调用失败（rate_limit / timeout）时，按顺序尝试 fallback
            </p>
          </div>
          <button
            className="rs-btn rs-btn-primary"
            onClick={handleNewFallbackChain}
            disabled={savingFallback || (s.providers?.length ?? 0) < 2}
            title={
              (s.providers?.length ?? 0) < 2
                ? '至少需要 2 个已配置 provider 才能建链'
                : '新建一条 fallback 链'
            }
          >
            + 新建链
          </button>
        </div>
        <div className="rs-panel-body">
          {chainEntries.length === 0 ? (
            <div className="rs-fallback-empty-card">
              <div className="rs-fallback-empty-icon">⚡</div>
              <div className="rs-fallback-empty-title">还没有 fallback 链</div>
              <div className="rs-fallback-empty-desc">
                主 provider 在 rate_limit 或 timeout 时，会按链顺序自动切换到 fallback。
                <br />
                点击右上「+ 新建链」开始配置。
              </div>
              <button
                className="rs-btn rs-btn-primary rs-btn-lg"
                onClick={handleNewFallbackChain}
                disabled={savingFallback || (s.providers?.length ?? 0) < 2}
              >
                + 新建第一条 fallback 链
              </button>
            </div>
          ) : (
            <div className="rs-fallback-list">
              {chainEntries.map(([providerId, fallbacks]) => (
                <div key={providerId} className="rs-fallback-row">
                  <div className="rs-fallback-primary">
                    <span className="rs-provider-icon-sm">{getProviderIcon(providerId)}</span>
                    <span className="font-mono">{providerId}</span>
                  </div>
                  <div className="rs-fallback-arrow">→</div>
                  <div className="rs-fallback-chain">
                    {fallbacks.length === 0 ? (
                      <span className="rs-fallback-empty">无 fallback</span>
                    ) : (
                      fallbacks.map((fb, idx) => (
                        <span
                          key={`${fb.provider}-${idx}`}
                          className="rs-fallback-chip"
                          title={fb.model ? `显式切到 ${fb.provider} / ${fb.model}` : `用 ${fb.provider} 的默认 model`}
                        >
                          {idx + 1}. {fb.provider}
                          {fb.model && <span className="rs-fallback-chip-model">#{fb.model}</span>}
                        </span>
                      ))
                    )}
                  </div>
                  <div className="rs-fallback-actions">
                    <button
                      className="rs-btn rs-btn-ghost rs-btn-sm"
                      onClick={() => handleEditFallbackChain(providerId)}
                      disabled={savingFallback}
                    >
                      编辑
                    </button>
                    <button
                      className="rs-btn rs-btn-ghost rs-btn-sm rs-btn-danger"
                      onClick={() => handleDeleteFallbackChain(providerId)}
                      disabled={savingFallback}
                    >
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="rs-config-hint">
            <strong>fail-loud 原则</strong>：仅当用户显式配置{' '}
            <code className="font-mono">fallback_chains</code> 时才切换；未配置时主 provider 报错会原样冒泡，
            不会静默切换让你错过故障信号。
            <br />
            <strong>触发条件</strong>：DagEngine 在 <code className="font-mono">error_type ∈ {'{'}rate_limit, timeout{'}'}</code>{' '}
            时触发 fallback；auth_error / protocol_mismatch / not_found 不切（配置问题切了也救不了）。
            <br />
            <strong>model 可省</strong>：不指定 model 则用 fallback provider 的第一个注册 model；指定则精确切换。
          </div>
        </div>
      </div>
    );
  }

  /**
   * Fallback 链编辑弹窗：主 provider 选择（isNew 可改 / 编辑锁定）+ 动态 fallback 数组
   * 每条 fb 可选 provider + 可选 model + 上下移 + 删除 + 添加。
   */
  function renderFallbackEditorModal() {
    if (!fallbackModal) return null;
    const isNew = fallbackModal.isNew;
    const primary = fallbackModal.primary;
    const entries = fallbackModal.entries;
    const providerOptions = (s?.providers ?? []).map((p) => ({
      id: p.provider_id,
      label: getProviderDisplayName(p.provider_id),
      icon: getProviderIcon(p.provider_id),
      models: p.models ?? [],
    }));

    // 主 provider 锁定时不能再选它为 fb
    const fbProviderOptions = providerOptions.filter((opt) => opt.id !== primary);

    return (
      <div
        className="rs-modal-overlay"
        onClick={(e) => {
          if (e.target === e.currentTarget && !savingFallback) handleCloseFallbackModal();
        }}
      >
        <div className="rs-modal" style={{ maxWidth: '720px' }}>
          <div className="rs-modal-header">
            <span>{isNew ? '新建 fallback 链' : `编辑 fallback 链：${primary}`}</span>
            <button
              className="rs-modal-close"
              onClick={handleCloseFallbackModal}
              disabled={savingFallback}
            >
              ×
            </button>
          </div>
          <div className="rs-modal-body">
            {/* 主 provider */}
            <div className="rs-form-row">
              <label className="rs-form-label">主 provider</label>
              {isNew ? (
                <select
                  className="rs-form-input"
                  value={primary}
                  onChange={(e) => {
                    // 主 provider 改了，清理 entries 里同名的 fb
                    const newPrimary = e.target.value;
                    setFallbackModal({
                      ...fallbackModal,
                      primary: newPrimary,
                      entries: fallbackModal.entries.filter(
                        (entry) => entry.provider && entry.provider !== newPrimary,
                      ),
                    });
                  }}
                  disabled={savingFallback}
                >
                  <option value="">-- 选择主 provider --</option>
                  {providerOptions.map((opt) => (
                    <option key={opt.id} value={opt.id}>
                      {opt.icon} {opt.label} ({opt.id})
                    </option>
                  ))}
                </select>
              ) : (
                <div className="rs-form-static">
                  <span className="rs-provider-icon-sm">{getProviderIcon(primary)}</span>
                  <span>{getProviderDisplayName(primary)}</span>
                  <span className="font-mono rs-text-muted">({primary})</span>
                  <span className="rs-text-muted rs-text-xs">编辑时主 provider 不可改（删除重建）</span>
                </div>
              )}
            </div>

            {/* Fallback 数组 */}
            <div className="rs-form-row">
              <label className="rs-form-label">Fallback 顺序</label>
              {entries.length === 0 ? (
                <div className="rs-form-hint">
                  还没有任何 fallback，点击下方「+ 添加」开始。
                </div>
              ) : (
                <div className="rs-fallback-edit-list">
                  {entries.map((entry, idx) => {
                    // 当前 fb 选项的可用 model 列表（联动）
                    const optForEntry = providerOptions.find((o) => o.id === entry.provider);
                    const availableModels = optForEntry?.models ?? [];
                    return (
                      <div key={idx} className="rs-fallback-edit-row">
                        <span className="rs-fallback-edit-idx">{idx + 1}</span>
                        <select
                          className="rs-form-input rs-fallback-edit-provider"
                          value={entry.provider}
                          onChange={(e) => {
                            const next = [...entries];
                            const newProvider = e.target.value;
                            // 切换 provider 时若选了主 provider 或重复，重置 model
                            const newModel = newProvider === primary
                              ? null
                              : next[idx].model && availableModels.find((m) => m.id === next[idx].model)
                                ? next[idx].model
                                : null;
                            next[idx] = { provider: newProvider, model: newModel };
                            // 移除重复（保留首次出现）
                            const seen = new Set<string>();
                            const deduped = next.filter((e2) => {
                              if (!e2.provider || seen.has(e2.provider)) return false;
                              seen.add(e2.provider);
                              return true;
                            });
                            setFallbackModal({ ...fallbackModal, entries: deduped });
                          }}
                          disabled={savingFallback}
                        >
                          <option value="">-- 选 provider --</option>
                          {fbProviderOptions.map((opt) => (
                            <option key={opt.id} value={opt.id}>
                              {opt.icon} {opt.label} ({opt.id})
                            </option>
                          ))}
                        </select>
                        <select
                          className="rs-form-input rs-fallback-edit-model"
                          value={entry.model ?? ''}
                          onChange={(e) => {
                            const next = [...entries];
                            next[idx] = { ...next[idx], model: e.target.value || null };
                            setFallbackModal({ ...fallbackModal, entries: next });
                          }}
                          disabled={savingFallback || !entry.provider || availableModels.length === 0}
                          title={
                            !entry.provider
                              ? '先选 provider'
                              : availableModels.length === 0
                                ? '该 provider 未注册 model，将用默认 model'
                                : '显式指定 model；留空用 provider 默认 model'
                          }
                        >
                          <option value="">默认 model</option>
                          {availableModels.map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.id}
                            </option>
                          ))}
                        </select>
                        <button
                          className="rs-btn rs-btn-ghost rs-btn-sm"
                          onClick={() => {
                            const next = [...entries];
                            [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
                            setFallbackModal({ ...fallbackModal, entries: next });
                          }}
                          disabled={idx === 0 || savingFallback}
                          title="上移"
                        >
                          ↑
                        </button>
                        <button
                          className="rs-btn rs-btn-ghost rs-btn-sm"
                          onClick={() => {
                            const next = [...entries];
                            [next[idx + 1], next[idx]] = [next[idx], next[idx + 1]];
                            setFallbackModal({ ...fallbackModal, entries: next });
                          }}
                          disabled={idx === entries.length - 1 || savingFallback}
                          title="下移"
                        >
                          ↓
                        </button>
                        <button
                          className="rs-btn rs-btn-ghost rs-btn-sm rs-btn-danger"
                          onClick={() => {
                            const next = entries.filter((_, i) => i !== idx);
                            setFallbackModal({ ...fallbackModal, entries: next });
                          }}
                          disabled={savingFallback}
                          title="删除"
                        >
                          ×
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
              <button
                className="rs-btn rs-btn-ghost rs-btn-sm rs-btn-add-fb"
                onClick={() => {
                  setFallbackModal({
                    ...fallbackModal,
                    entries: [...entries, { provider: '', model: null }],
                  });
                }}
                disabled={savingFallback || entries.length >= fbProviderOptions.length}
                title={
                  entries.length >= fbProviderOptions.length
                    ? '已用完所有非主 provider'
                    : '添加一条 fallback'
                }
              >
                + 添加 fallback
              </button>
            </div>

            {fallbackError && (
              <div className="rs-form-error" role="alert">
                {fallbackError}
              </div>
            )}
          </div>
          <div className="rs-modal-footer">
            <button
              className="rs-btn rs-btn-ghost"
              onClick={handleCloseFallbackModal}
              disabled={savingFallback}
            >
              取消
            </button>
            <button
              className="rs-btn rs-btn-primary"
              onClick={handleSaveFallbackChains}
              disabled={savingFallback}
            >
              {savingFallback ? '保存中...' : isNew ? '创建' : '保存'}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：Catalog 弹窗
  // ════════════════════════════════════════════════════════════
  function renderCatalogModal() {
    if (!catalogOpen) return null;
    return (
      <div
        className="rs-modal-overlay"
        onClick={(e) => { if (e.target === e.currentTarget) setCatalogOpen(false); }}
      >
        <div className="rs-modal" style={{ maxWidth: '880px' }}>
          <div className="rs-modal-header">
            <span>供应商目录 — 内置预设</span>
            <button className="rs-modal-close" onClick={() => setCatalogOpen(false)}>×</button>
          </div>
          <div className="rs-modal-body">
            {/* 标签页切换 */}
            <div className="rs-catalog-tabs">
              <button
                className={`rs-catalog-tab ${catalogTab === 'presets' ? 'active' : ''}`}
                onClick={() => setCatalogTab('presets')}
              >
                内置预设
              </button>
              <button
                className={`rs-catalog-tab ${catalogTab === 'custom' ? 'active' : ''}`}
                onClick={() => setCatalogTab('custom')}
              >
                自定义供应商
              </button>
            </div>

            {catalogTab === 'presets' ? (
              <div className="rs-catalog-grid">
                {PROVIDER_CATALOG.map((cat) => {
                  const existing = s.providers.find((p) => p.provider_id === cat.id);
                  return (
                    <div key={cat.id} className="rs-catalog-card">
                      <div className="rs-catalog-card-header">
                        <div className="rs-provider-icon-lg" style={{ width: 32, height: 32, fontSize: 14 }}>
                          {cat.icon}
                        </div>
                        <div>
                          <div className="rs-catalog-card-name">{cat.name}</div>
                          <div className="rs-catalog-card-id font-mono">{cat.id}</div>
                        </div>
                        {existing ? (
                          <span className="status-pill status-pill-success">已配置</span>
                        ) : (
                          <span className="status-pill status-pill-neutral">未配置</span>
                        )}
                      </div>
                      <div className="rs-catalog-card-desc">{cat.description}</div>
                      <div className="rs-catalog-card-config">
                        <div className="rs-config-row">
                          <span className="rs-config-label">Base URL</span>
                          <span className="rs-config-value font-mono">{cat.base_url}</span>
                        </div>
                        <div className="rs-config-row">
                          <span className="rs-config-label">Protocol</span>
                          <span className="rs-config-value font-mono">{cat.protocol}</span>
                        </div>
                        <div className="rs-config-row">
                          <span className="rs-config-label">Auth</span>
                          <span className="rs-config-value font-mono">{cat.auth_type}</span>
                        </div>
                      </div>
                      {cat.preset_models.length > 0 && (
                        <div className="rs-catalog-card-models">
                          <span className="rs-catalog-models-label">预设模型 ({cat.preset_models.length})</span>
                          <div className="rs-catalog-models-list">
                            {cat.preset_models.map((m) => (
                              <span key={m.id} className="rs-catalog-model-chip font-mono">{m.id}</span>
                            ))}
                          </div>
                        </div>
                      )}
                      {existing ? (
                        <button
                          className="btn-secondary btn-sm"
                          style={{ width: '100%' }}
                          onClick={() => {
                            setSelection({ type: 'provider', id: cat.id });
                            setCatalogOpen(false);
                          }}
                        >
                          查看配置
                        </button>
                      ) : (
                        <button
                          className="btn-primary btn-sm"
                          style={{ width: '100%' }}
                          onClick={() => {
                            // 从 catalog 创建 provider
                            apiClient.createProvider({
                              provider_id: cat.id,
                              base_url: cat.base_url,
                              protocol: cat.protocol,
                              auth_type: cat.auth_type,
                            }).then(() => {
                              // 添加预设模型
                              cat.preset_models.forEach((m) => {
                                apiClient.addModel(cat.id, m);
                              });
                              setCatalogOpen(false);
                              loadData();
                              setSelection({ type: 'provider', id: cat.id });
                            }).catch((e) => {
                              setError(e instanceof Error ? e.message : String(e));
                            });
                          }}
                        >
                          添加供应商
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              /* 自定义供应商表单 */
              <div className="rs-custom-provider-form">
                <div className="rs-model-form-field">
                  <label className="rs-form-label">供应商 ID *</label>
                  <input
                    className="input-base font-mono"
                    value={customForm.provider_id}
                    onChange={(e) => setCustomForm((f) => ({ ...f, provider_id: e.target.value }))}
                    placeholder="例如：ollama"
                    style={{ width: '100%' }}
                    autoFocus
                  />
                  <div className="rs-form-hint">唯一标识，小写字母+连字符，用于配置文件引用</div>
                </div>
                <div className="rs-model-form-field">
                  <label className="rs-form-label">Base URL *</label>
                  <input
                    className="input-base font-mono"
                    value={customForm.base_url}
                    onChange={(e) => setCustomForm((f) => ({ ...f, base_url: e.target.value }))}
                    placeholder="http://localhost:11434/v1"
                    style={{ width: '100%' }}
                  />
                  <div className="rs-form-hint">API 基础地址，本地模型通常为 http://localhost:端口/v1</div>
                </div>
                <div className="rs-model-form-row">
                  <div className="rs-model-form-field">
                    <label className="rs-form-label">API 协议</label>
                    <select
                      className="input-base"
                      value={customForm.protocol}
                      onChange={(e) => setCustomForm((f) => ({ ...f, protocol: e.target.value }))}
                      style={{ width: '100%' }}
                    >
                      <option value="openai_compatible">OpenAI 兼容</option>
                      <option value="anthropic_compatible">Anthropic 兼容</option>
                    </select>
                  </div>
                  <div className="rs-model-form-field">
                    <label className="rs-form-label">认证方式</label>
                    <select
                      className="input-base"
                      value={customForm.auth_type}
                      onChange={(e) => setCustomForm((f) => ({ ...f, auth_type: e.target.value }))}
                      style={{ width: '100%' }}
                    >
                      <option value="bearer">Bearer Token</option>
                      <option value="x-api-key">X-API-Key</option>
                    </select>
                  </div>
                </div>
                <div className="rs-custom-provider-hint">
                  添加自定义供应商后，可在供应商详情中为其添加模型，并设置为默认模型或 Manager 模型。本地模型（如 Ollama）认证方式选 Bearer Token，API Key 可留空。
                </div>
                <button
                  className="btn-primary"
                  style={{ width: '100%' }}
                  onClick={handleCreateCustomProvider}
                  disabled={!customForm.provider_id.trim() || !customForm.base_url.trim() || creatingProvider}
                >
                  {creatingProvider ? '创建中...' : '确认添加'}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：凭证录入弹窗
  // ════════════════════════════════════════════════════════════
  function renderCredentialModal() {
    if (!credModalProvider) return null;
    const p = s.providers.find((rp) => rp.provider_id === credModalProvider);
    if (!p) return null;

    return (
      <div
        className="rs-modal-overlay"
        onClick={(e) => { if (e.target === e.currentTarget) { setCredModalProvider(null); setCredError(''); } }}
      >
        <div className="rs-modal" style={{ maxWidth: '480px' }}>
          <div className="rs-modal-header">
            <span>{p.has_credential ? '更新 API Key' : '录入 API Key'}</span>
            <button className="rs-modal-close" onClick={() => { setCredModalProvider(null); setCredError(''); }}>×</button>
          </div>
          <div className="rs-modal-body">
            <div className="rs-cred-modal-info">
              <div className="rs-config-row">
                <span className="rs-config-label">Provider</span>
                <span className="rs-config-value font-mono">{p.provider_id}</span>
              </div>
              <div className="rs-config-row">
                <span className="rs-config-label">Base URL</span>
                <span className="rs-config-value font-mono">{p.base_url}</span>
              </div>
              <div className="rs-config-row">
                <span className="rs-config-label">Auth</span>
                <span className="rs-config-value font-mono">{AUTH_LABELS[p.auth_type] || p.auth_type}</span>
              </div>
            </div>
            <div className="rs-cred-modal-field">
              <label className="rs-cred-modal-label">API Key</label>
              <input
                className="input-base font-mono"
                type="password"
                value={apiKeyInput}
                onChange={(e) => { setApiKeyInput(e.target.value); setCredError(''); }}
                placeholder={p.auth_type === 'bearer' ? 'sk-...' : '输入 API Key'}
                style={{ width: '100%' }}
                autoFocus
              />
              <div className="rs-cred-modal-hint">
                凭证将通过 Fernet 加密存储，不会以明文落盘或上传第三方。
              </div>
            </div>
            {credError && (
              <div className="rs-cred-modal-error">{credError}</div>
            )}
          </div>
          <div className="rs-modal-footer">
            <button className="btn-secondary btn-sm" onClick={() => { setCredModalProvider(null); setCredError(''); }}>
              取消
            </button>
            <button
              className="btn-primary btn-sm"
              onClick={handleSaveCredential}
              disabled={credSaving || !apiKeyInput.trim()}
            >
              {credSaving ? '保存中...' : '保存'}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：删除凭证确认弹窗
  // ════════════════════════════════════════════════════════════
  function renderDeleteConfirmModal() {
    if (!deleteConfirmId) return null;
    return (
      <div
        className="rs-modal-overlay"
        onClick={(e) => { if (e.target === e.currentTarget) setDeleteConfirmId(null); }}
      >
        <div className="rs-modal" style={{ maxWidth: '400px' }}>
          <div className="rs-modal-header">
            <span>确认删除</span>
            <button className="rs-modal-close" onClick={() => setDeleteConfirmId(null)}>×</button>
          </div>
          <div className="rs-modal-body">
            <div className="rs-delete-confirm-text">
              确定要删除 <strong className="font-mono">{deleteConfirmId}</strong> 的 API Key 凭证吗？
              <br />删除后该供应商将无法调用，直至重新录入凭证。
            </div>
          </div>
          <div className="rs-modal-footer">
            <button className="btn-secondary btn-sm" onClick={() => setDeleteConfirmId(null)}>
              取消
            </button>
            <button
              className="btn-sm rs-btn-danger-confirm"
              onClick={handleDeleteCredential}
            >
              确认删除
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：添加模型弹窗
  // ════════════════════════════════════════════════════════════
  function renderModelFormModal() {
    if (!modelFormOpen || !selectedProvider) return null;
    return (
      <div
        className="rs-modal-overlay"
        onClick={(e) => { if (e.target === e.currentTarget) setModelFormOpen(false); }}
      >
        <div className="rs-modal" style={{ maxWidth: '480px' }}>
          <div className="rs-modal-header">
            <span>添加模型 — {selectedProvider.provider_id}</span>
            <button className="rs-modal-close" onClick={() => setModelFormOpen(false)}>×</button>
          </div>
          <div className="rs-modal-body">
            <div className="rs-model-form-field">
              <label className="rs-form-label">模型 ID *</label>
              <input
                className="input-base font-mono"
                value={modelForm.id}
                onChange={(e) => setModelForm((f) => ({ ...f, id: e.target.value }))}
                placeholder="如 gpt-4o, claude-sonnet-4"
                style={{ width: '100%' }}
                autoFocus
              />
            </div>
            <div className="rs-model-form-field">
              <label className="rs-form-label">最大 Token</label>
              <input
                className="input-base font-mono"
                type="number"
                value={modelForm.max_tokens || ''}
                onChange={(e) => setModelForm((f) => ({ ...f, max_tokens: Number(e.target.value) || undefined }))}
                placeholder="4096"
                style={{ width: '100%' }}
              />
            </div>
            <div className="rs-model-form-row">
              <div className="rs-model-form-field">
                <label className="rs-form-label">输入价格 / 1K tokens (¥)</label>
                <input
                  className="input-base font-mono"
                  type="number"
                  step="0.0001"
                  value={modelForm.price_input_per_1k || ''}
                  onChange={(e) => setModelForm((f) => ({ ...f, price_input_per_1k: Number(e.target.value) || undefined }))}
                  placeholder="0.0025"
                  style={{ width: '100%' }}
                />
              </div>
              <div className="rs-model-form-field">
                <label className="rs-form-label">输出价格 / 1K tokens (¥)</label>
                <input
                  className="input-base font-mono"
                  type="number"
                  step="0.0001"
                  value={modelForm.price_output_per_1k || ''}
                  onChange={(e) => setModelForm((f) => ({ ...f, price_output_per_1k: Number(e.target.value) || undefined }))}
                  placeholder="0.01"
                  style={{ width: '100%' }}
                />
              </div>
            </div>
          </div>
          <div className="rs-modal-footer">
            <button className="btn-secondary btn-sm" onClick={() => setModelFormOpen(false)}>
              取消
            </button>
            <button
              className="btn-primary btn-sm"
              onClick={() => handleSaveModel(selectedProvider.provider_id)}
              disabled={!modelForm.id.trim()}
            >
              添加
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  主渲染
  // ════════════════════════════════════════════════════════════
  return (
    <>
      <style>{RUNTIME_SETTINGS_STYLES}</style>
      <div className="rs-layout">
        {renderSidebar()}
        <main className="rs-content">
          {selection.type === 'provider' && selectedProvider && renderProviderPanel(selectedProvider)}
          {selection.type === 'provider' && !selectedProvider && (
            <div className="rs-content-empty">
              <div className="rs-content-empty-icon">!</div>
              <div>未找到该供应商</div>
            </div>
          )}
          {selection.type === 'models' && renderModelsPanel()}
          {selection.type === 'fallback' && renderFallbackPanel()}
        </main>
      </div>

      {/* 弹窗 */}
      {renderCatalogModal()}
      {renderCredentialModal()}
      {renderDeleteConfirmModal()}
      {renderModelFormModal()}
      {renderFallbackEditorModal()}
    </>
  );
}

// ═══════════════════════════════════════════════════════════════
//  侧边栏图标组件
// ═══════════════════════════════════════════════════════════════
function SidebarIcon({ type }: { type: 'model' | 'fallback' }) {
  if (type === 'model') {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="3" />
        <path d="M12 1v6m0 6v6m11-7h-6m-6 0H1m15.5-7.5l-4.2 4.2m-4.6 4.6l-4.2 4.2m12.8 0l-4.2-4.2m-4.6-4.6L4.5 4.5" />
      </svg>
    );
  }
  // fallback
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="1 4 1 10 7 10" />
      <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
    </svg>
  );
}

// ═══════════════════════════════════════════════════════════════
//  样式（CSS-in-JS，使用项目 CSS 变量）
// ═══════════════════════════════════════════════════════════════
const RUNTIME_SETTINGS_STYLES = `
.rs-layout {
  display: flex;
  height: 100%;
  min-height: 0;
  gap: 0;
}

/* ── 侧边栏 ── */
.rs-sidebar {
  width: 240px;
  flex-shrink: 0;
  background: var(--color-bg-surface);
  border-right: 1px solid var(--color-border-subtle);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  padding: 12px 8px;
  gap: 4px;
}

.rs-sidebar-section {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.rs-sidebar-section + .rs-sidebar-section {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--color-border-subtle);
}

.rs-sidebar-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 12px;
  margin-bottom: 2px;
}

.rs-sidebar-section-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-text-tertiary);
}

.rs-sidebar-add-btn {
  width: 20px;
  height: 20px;
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  line-height: 1;
  transition: border-color 0.15s, color 0.15s;
}

.rs-sidebar-add-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary-soft);
}

.rs-sidebar-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-secondary);
  cursor: pointer;
  position: relative;
  transition: background-color 0.15s, color 0.15s;
  border: none;
  background: none;
  text-align: left;
  width: 100%;
}

.rs-sidebar-item:hover {
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
}

.rs-sidebar-item.active {
  background: var(--color-primary-tint);
  color: var(--color-primary-soft);
}

.rs-sidebar-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 2px;
  height: 16px;
  background: var(--color-primary);
  border-radius: 0 2px 2px 0;
}

.rs-sidebar-item svg {
  flex-shrink: 0;
  opacity: 0.7;
}

.rs-sidebar-badge {
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-tertiary);
  background: var(--color-bg-elevated);
  padding: 1px 7px;
  border-radius: var(--radius-full);
  min-width: 18px;
  text-align: center;
}

.rs-sidebar-item.active .rs-sidebar-badge {
  background: rgba(59, 130, 246, 0.2);
  color: var(--color-primary-soft);
}

.rs-sidebar-provider-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.rs-sidebar-provider-item {
  padding: 6px 12px;
  font-size: 12px;
}

.rs-sidebar-provider-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-sidebar-empty {
  padding: 8px 12px;
  font-size: 12px;
  color: var(--color-text-tertiary);
  font-style: italic;
}

/* ── 凭证状态圆点 ── */
.rs-cred-dot {
  width: 7px;
  height: 7px;
  border-radius: var(--radius-full);
  flex-shrink: 0;
}

.rs-cred-dot-ok {
  background: var(--state-success);
  box-shadow: 0 0 4px rgba(16, 185, 129, 0.4);
}

.rs-cred-dot-env {
  background: var(--state-warning);
  box-shadow: 0 0 4px rgba(251, 191, 36, 0.3);
}

.rs-cred-dot-none {
  background: var(--state-error);
  opacity: 0.6;
}

/* ── Provider 图标 ── */
.rs-provider-icon-sm {
  width: 20px;
  height: 20px;
  border-radius: var(--radius-sm);
  background: var(--color-bg-elevated);
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
}

.rs-provider-icon-lg {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-md);
  background: var(--color-primary-tint);
  color: var(--color-primary-soft);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 700;
  flex-shrink: 0;
}

/* ── 右侧内容区 ── */
.rs-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 28px 32px;
  background: var(--color-bg-base);
}

.rs-content-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 12px;
  color: var(--color-text-tertiary);
}

.rs-content-empty-icon {
  width: 48px;
  height: 48px;
  border-radius: var(--radius-full);
  background: var(--color-bg-elevated);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 700;
}

/* ── 面板 ── */
.rs-panel {
  max-width: 860px;
  margin: 0 auto;
}

.rs-panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 20px;
  padding: 0 4px;
}

.rs-panel-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.rs-panel-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.rs-panel-subtitle {
  font-size: 13px;
  color: var(--color-text-tertiary);
  margin: 2px 0 0 0;
}

.rs-panel-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ── 详情区块 ── */
.rs-detail-section {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  /* overflow: visible — 允许下拉菜单溢出 section 边界 */
  overflow: visible;
}

.rs-detail-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.rs-detail-section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.rs-count-badge {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-tertiary);
  background: var(--color-bg-elevated);
  padding: 1px 7px;
  border-radius: var(--radius-full);
}

/* ── 配置行 ── */
.rs-config-grid {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rs-config-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  padding: 6px 16px;
}

.rs-config-row-edit {
  align-items: center;
}

.rs-config-label {
  font-size: 12px;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  font-weight: 500;
  width: 90px;
  flex-shrink: 0;
}

.rs-config-value {
  color: var(--color-text-primary);
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-config-actions {
  display: flex;
  gap: 8px;
  padding-left: 102px;
}

/* ── 凭证管理 ── */
.rs-cred-status-row {
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.rs-cred-status-info {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rs-cred-status-text {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-primary);
}

.rs-cred-status-time {
  font-size: 12px;
  color: var(--color-text-tertiary);
}

.rs-cred-actions {
  display: flex;
  gap: 8px;
}

.rs-cred-hint {
  padding: 0 16px 12px;
  font-size: 12px;
  color: var(--color-text-tertiary);
}

.rs-btn-danger {
  color: var(--state-error);
}

.rs-btn-danger-confirm {
  background: var(--state-error);
  color: #fff;
  border: none;
  border-radius: var(--radius-md);
  font-weight: 600;
  font-size: 13px;
  padding: 0 16px;
  height: 32px;
  cursor: pointer;
}

.rs-btn-danger-confirm:hover { opacity: 0.9; }

/* ── 模型列表 ── */
.rs-model-list {
  padding: 8px 0;
}

.rs-model-list-actions {
  display: flex;
  gap: 8px;
}

.rs-fetched-models {
  padding: 10px 16px;
  border-bottom: 1px solid var(--color-border-subtle);
}
.rs-fetched-label {
  display: block;
  font-size: 12px;
  color: var(--color-text-tertiary);
  margin-bottom: 6px;
}
.rs-fetched-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.rs-model-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--color-border-subtle);
  transition: background-color 0.15s;
}

.rs-model-row:last-child {
  border-bottom: none;
}

.rs-model-row:hover {
  background: rgba(30, 41, 59, 0.4);
}

.rs-model-row-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.rs-model-id {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-primary);
}

.rs-model-tags {
  display: flex;
  gap: 4px;
}

.rs-model-row-meta {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.rs-model-meta-item strong {
  color: var(--color-text-primary);
}

.rs-model-delete-btn {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background-color 0.15s, color 0.15s;
}

.rs-model-delete-btn:hover {
  background: var(--state-error-tint);
  color: var(--state-error);
}

/* ── 预设模型快捷添加 ── */
.rs-preset-models {
  padding: 8px 16px 12px;
  border-top: 1px solid var(--color-border-subtle);
}

.rs-preset-models-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  font-weight: 500;
  color: var(--color-text-tertiary);
  margin-bottom: 6px;
}

.rs-preset-models-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.rs-preset-model-chip {
  font-size: 12px;
  font-family: var(--font-mono);
  padding: 3px 10px;
  border-radius: var(--radius-full);
  border: 1px dashed var(--color-border-default);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}

.rs-preset-model-chip:hover {
  border-color: var(--color-primary);
  color: var(--color-primary-soft);
  border-style: solid;
}

/* ── 健康状态 ── */
.rs-health-result {
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.rs-health-text {
  font-size: 13px;
}

.rs-health-success .rs-health-text { color: var(--state-success); }
.rs-health-failed .rs-health-text { color: var(--state-error); }
.rs-health-testing .rs-health-text { color: var(--color-text-tertiary); }

.rs-health-idle {
  padding: 12px 16px;
  font-size: 13px;
  color: var(--color-text-tertiary);
  font-style: italic;
}
.rs-health-results {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
}
.rs-health-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 0;
}
.rs-health-row-label {
  font-size: 13px;
  color: var(--color-text-tertiary);
  width: 72px;
  flex-shrink: 0;
}
.rs-health-idle-inline {
  font-size: 13px;
  color: var(--color-text-tertiary);
  font-style: italic;
}
.rs-test-btn-group {
  display: flex;
  gap: 8px;
}

/* ── 模型概览 ── */
/* ── 可用模型卡片 ── */
.rs-model-cards {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.rs-model-card {
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  overflow: hidden;
  transition: border-color 0.15s;
}
.rs-model-card:hover {
  border-color: var(--color-border-default);
}
.rs-model-card-muted {
  padding: 10px 14px;
  border-style: dashed;
  border-color: var(--color-border-subtle);
}
.rs-model-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--color-border-subtle);
}
.rs-model-card-provider {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rs-model-card-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-primary);
}
.rs-model-card-count {
  font-size: 11px;
  color: var(--color-text-tertiary);
}
.rs-model-card-nokey {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: rgba(251,191,36,0.1);
  color: var(--state-warning);
}
.rs-model-card-fetch {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  background: transparent;
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-sm);
  color: var(--color-text-tertiary);
  cursor: pointer;
  transition: all 0.15s;
  flex-shrink: 0;
}
.rs-model-card-fetch:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: rgba(59,130,246,0.06);
}
.rs-model-card-fetch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.rs-model-card-body {
  padding: 10px 12px;
}
.rs-model-card-empty {
  font-size: 12px;
  color: var(--color-text-tertiary);
}
.rs-model-card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.rs-model-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-family: var(--font-mono);
  padding: 3px 10px;
  border-radius: var(--radius-full);
  background: var(--color-bg-base);
  color: var(--color-text-secondary);
  border: 1px solid transparent;
  line-height: 1.6;
}
.rs-tag-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.rs-tag-default {
  background: rgba(16,185,129,0.1);
  color: var(--state-success);
  border-color: rgba(16,185,129,0.25);
}
.rs-tag-dot-default {
  background: var(--state-success);
}
.rs-tag-manager {
  background: rgba(59,130,246,0.1);
  color: var(--color-primary);
  border-color: rgba(59,130,246,0.25);
}
.rs-tag-dot-manager {
  background: var(--color-primary);
}
.rs-tag-new {
  border: 1px dashed var(--color-primary) !important;
  color: var(--color-primary) !important;
  cursor: pointer;
  background: rgba(59,130,246,0.06) !important;
}
.rs-tag-new:hover {
  background: rgba(59,130,246,0.14) !important;
}
  color: var(--state-success);
}

/* ── Fallback 链 ── */
.rs-fallback-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rs-fallback-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
}

.rs-fallback-primary {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
  flex-shrink: 0;
}

.rs-fallback-arrow {
  font-size: 16px;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.rs-fallback-chain {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  flex: 1;
}

.rs-fallback-chip {
  font-size: 12px;
  font-family: var(--font-mono);
  padding: 3px 10px;
  border-radius: var(--radius-full);
  background: var(--color-bg-elevated);
  color: var(--color-text-secondary);
}

.rs-fallback-empty {
  font-size: 12px;
  color: var(--color-text-tertiary);
  font-style: italic;
}

/* ── 提示文本 ── */
.rs-config-hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
  padding: 8px 16px;
  line-height: 1.6;
}

.rs-config-hint code {
  background: var(--color-bg-elevated);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: 11px;
}

/* ── 弹窗 ── */
.rs-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  backdrop-filter: blur(2px);
}

.rs-modal {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-floating);
  width: 90vw;
  max-height: min(85vh, calc(100vh - 48px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.rs-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border-subtle);
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.rs-modal-close {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--color-text-tertiary);
  font-size: 20px;
  line-height: 1;
  padding: 4px;
}

.rs-modal-close:hover {
  color: var(--color-text-primary);
}

.rs-modal-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.rs-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border-subtle);
}

/* ── 凭证弹窗 ── */
.rs-cred-modal-info {
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rs-cred-modal-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rs-cred-modal-label {
  font-size: 12px;
  color: var(--color-text-tertiary);
  font-weight: 500;
}

.rs-cred-modal-hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
  line-height: 1.5;
}

.rs-cred-modal-error {
  font-size: 13px;
  color: var(--state-error);
  padding: 8px 12px;
  background: var(--state-error-tint);
  border-radius: var(--radius-md);
}

/* ── 删除确认 ── */
.rs-delete-confirm-text {
  font-size: 14px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

/* ── 模型表单 ── */
.rs-model-form-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rs-model-form-row {
  display: flex;
  gap: 12px;
}

.rs-model-form-row .rs-model-form-field {
  flex: 1;
}

.rs-form-label {
  font-size: 12px;
  color: var(--color-text-tertiary);
  font-weight: 500;
}

/* ── Catalog 弹窗 ── */
.rs-catalog-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 10px;
}

.rs-catalog-card {
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rs-catalog-card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.rs-catalog-card-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.rs-catalog-card-id {
  font-size: 11px;
  color: var(--color-text-tertiary);
}

.rs-catalog-card-header .status-pill {
  margin-left: auto;
}

.rs-catalog-card-desc {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.4;
}

.rs-catalog-card-config {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  background: var(--color-bg-surface);
  border-radius: var(--radius-sm);
}

.rs-catalog-card-config .rs-config-row {
  font-size: 11px;
  padding: 2px 0;
  gap: 8px;
}

.rs-catalog-card-config .rs-config-label {
  width: 56px;
  font-size: 10px;
}

.rs-catalog-card-config .rs-config-value {
  font-size: 11px;
}

.rs-catalog-card-models {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.rs-catalog-models-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  font-weight: 500;
  color: var(--color-text-tertiary);
}

.rs-catalog-models-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.rs-catalog-model-chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--color-bg-elevated);
  color: var(--color-text-secondary);
}

.rs-catalog-not-configured {
  font-size: 11px;
  color: var(--color-text-tertiary);
  text-align: center;
  padding: 6px;
  font-style: italic;
}

/* ── 加载/错误状态 ── */
.rs-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 12px;
  color: var(--color-text-tertiary);
  font-size: 14px;
}

.rs-spinner {
  width: 24px;
  height: 24px;
  border: 2px solid var(--color-border-default);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: rs-spin 0.8s linear infinite;
}

.rs-btn-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 1.5px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: rs-spin 0.8s linear infinite;
  margin-right: 4px;
}

@keyframes rs-spin {
  to { transform: rotate(360deg); }
}

.rs-error-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 12px;
}

.rs-error-icon {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--state-error-tint);
  color: var(--state-error);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 700;
}

.rs-error-text {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.rs-error-banner {
  margin: 0 0 16px 0;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  background: var(--state-error-tint);
  color: var(--state-error);
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

/* ── 响应式 ── */
@media (max-width: 768px) {
  .rs-sidebar {
    width: 200px;
  }
  .rs-catalog-grid {
    grid-template-columns: 1fr;
  }
}

/* ── 模型选择下拉 ── */
.rs-model-select-row {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 4px;
}
.rs-model-select-row .rs-config-label {
  width: 80px;
  flex-shrink: 0;
  padding-top: 9px;
}
.rs-model-select-wrapper {
  flex: 1;
  position: relative;
}
.rs-model-select-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 10px 14px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  color: var(--color-text-primary);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.rs-model-select-btn:hover {
  border-color: var(--color-primary);
}
.rs-model-select-provider {
  color: var(--color-primary);
  font-size: 12px;
}
.rs-model-select-sep {
  color: var(--color-text-tertiary);
}
.rs-model-select-id {
  color: var(--color-text-primary);
}
.rs-model-select-chevron {
  margin-left: auto;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}
.rs-model-select-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  max-height: 280px;
  overflow-y: auto;
  z-index: 100;
  box-shadow: 0 8px 24px rgba(0,0,0,0.32);
}
.rs-model-select-option {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  cursor: pointer;
  font-size: 13px;
  transition: background-color 0.1s;
}
.rs-model-select-option:hover {
  background: var(--color-bg-hover, rgba(255,255,255,0.04));
}
.rs-model-select-option.active {
  background: var(--color-primary-tint, rgba(59,130,246,0.08));
}
.rs-model-opt-provider {
  color: var(--color-primary);
  font-size: 12px;
}
.rs-model-opt-sep {
  color: var(--color-text-tertiary);
}
.rs-model-opt-id {
  color: var(--color-text-primary);
}
.rs-model-select-empty {
  padding: 16px;
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: 13px;
}

/* ── Catalog 标签页 ── */
.rs-catalog-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--color-border-subtle);
}
.rs-catalog-tab {
  padding: 8px 16px;
  font-size: 13px;
  color: var(--color-text-secondary);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.rs-catalog-tab:hover {
  color: var(--color-text-primary);
}
.rs-catalog-tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
  font-weight: 500;
}

/* ── 自定义供应商表单 ── */
.rs-custom-provider-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.rs-custom-provider-hint {
  padding: 10px 12px;
  background: var(--color-primary-tint, rgba(59,130,246,0.08));
  border: 1px solid rgba(59,130,246,0.2);
  border-radius: var(--radius-md);
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.rs-form-hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
  margin-top: 4px;
}

/* ── 级联下拉（供应商→模型） ── */
.rs-cascade-row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  padding: 12px 16px;
}
.rs-cascade-field {
  flex: 1;
  min-width: 0;
}
.rs-cascade-label {
  display: block;
  font-size: 12px;
  color: var(--color-text-tertiary);
  margin-bottom: 6px;
}
.rs-cascade-select {
  width: 100%;
  height: 38px;
  padding: 8px 12px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  color: var(--color-text-primary);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s;
  appearance: none;
  -webkit-appearance: none;
  background-image: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 12px center;
  padding-right: 36px;
}
.rs-cascade-select:hover {
  border-color: var(--color-primary);
}
.rs-cascade-select:focus {
  border-color: var(--color-primary);
  outline: none;
}
.rs-cascade-arrow {
  display: flex;
  align-items: center;
  padding-bottom: 10px;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

/* ── 错误提示 banner ── */
.rs-error-banner {
  padding: 10px 14px;
  background: rgba(239,68,68,0.08);
  border: 1px solid rgba(239,68,68,0.25);
  border-radius: var(--radius-md);
  font-size: 12px;
  color: var(--state-error);
  cursor: pointer;
}

/* ── Fallback 链编辑（empty + 列表 + 编辑列表） ── */
.rs-fallback-empty-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 24px;
  background: var(--color-bg-base);
  border: 1px dashed var(--color-border-default);
  border-radius: var(--radius-lg);
  text-align: center;
}
.rs-fallback-empty-icon {
  font-size: 40px;
  opacity: 0.5;
}
.rs-fallback-empty-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-primary);
}
.rs-fallback-empty-desc {
  font-size: 13px;
  color: var(--color-text-tertiary);
  line-height: 1.6;
  max-width: 460px;
}

.rs-fallback-chip-model {
  margin-left: 6px;
  color: var(--color-text-tertiary);
  font-size: 11px;
}
.rs-fallback-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

/* ── Fallback 链编辑列表（弹窗内） ── */
.rs-fallback-edit-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 8px;
}
.rs-fallback-edit-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
}
.rs-fallback-edit-idx {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--color-bg-elevated);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.rs-fallback-edit-provider {
  flex: 1;
  min-width: 0;
}
.rs-fallback-edit-model {
  flex: 1;
  min-width: 0;
}

/* ── 按钮（缺失的 rs-btn 系列，过去只定义了 rs-btn-danger） ── */
.rs-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 500;
  font-family: inherit;
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  background: var(--color-bg-surface);
  color: var(--color-text-primary);
  cursor: pointer;
  transition: opacity 0.15s, background 0.15s;
  white-space: nowrap;
}
.rs-btn:hover:not(:disabled) {
  opacity: 0.85;
}
.rs-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.rs-btn-primary {
  background: var(--color-accent-primary, #4f46e5);
  color: white;
  border-color: var(--color-accent-primary, #4f46e5);
}
.rs-btn-ghost {
  background: transparent;
  border-color: transparent;
  color: var(--color-text-secondary);
}
.rs-btn-ghost:hover:not(:disabled) {
  background: var(--color-bg-elevated);
  opacity: 1;
}
.rs-btn-sm {
  padding: 4px 10px;
  font-size: 12px;
}
.rs-btn-lg {
  padding: 10px 22px;
  font-size: 14px;
}
.rs-btn-add-fb {
  align-self: flex-start;
  margin-top: 4px;
}

/* ── 表单（缺失的 rs-form-* 系列） ── */
.rs-form-row {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}
.rs-form-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  letter-spacing: 0.02em;
}
.rs-form-input {
  padding: 7px 10px;
  font-size: 13px;
  font-family: inherit;
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  background: var(--color-bg-base);
  color: var(--color-text-primary);
  outline: none;
}
.rs-form-input:focus {
  border-color: var(--color-accent-primary, #4f46e5);
}
.rs-form-input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.rs-form-static {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  font-size: 13px;
  background: var(--color-bg-elevated);
  border-radius: var(--radius-md);
}
.rs-form-hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
  padding: 12px;
  background: var(--color-bg-base);
  border: 1px dashed var(--color-border-default);
  border-radius: var(--radius-md);
  text-align: center;
}
.rs-form-error {
  margin-top: 12px;
  padding: 8px 12px;
  font-size: 12px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: var(--radius-md);
  color: var(--state-error);
}

.rs-text-muted {
  color: var(--color-text-tertiary);
}
.rs-text-xs {
  font-size: 11px;
}

/* ── 弹窗底部（缺失的 rs-modal-footer） ── */
.rs-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border-subtle);
}
`;
