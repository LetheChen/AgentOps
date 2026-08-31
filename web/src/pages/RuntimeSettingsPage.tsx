import { useState, useEffect, useCallback, useRef } from 'react';
import { apiClient } from '../lib/api';
import type {
  RuntimeSummary,
  RuntimeEnvironmentSnapshot,
  OverallKind,
} from '../lib/api';
import { WorkspacesPage } from './WorkspacesPage';

// ═══════════════════════════════════════════════════════════════
//  运行环境页 — 由原「运行时配置」瘦身而来：
//  适配器 / Docker 运行环境 / 工作区授权（原一级菜单并入，LLM 资源域已拆至 ModelProvidersPage）
// ═══════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════
//  常量映射
// ═══════════════════════════════════════════════════════════════
const HARNESS_DESCRIPTIONS: Record<string, string> = {
  deterministic: '用于确定性流程，不调用 LLM，适合脚本化节点',
  opencode: '外部 agent CLI 集成，支持多 provider 路由',
  local_llm: '本地 LLM 调用，直连 provider API',
  codex: 'Codex 兼容 agent，通过 Responses API 交互',
};

// ═══════════════════════════════════════════════════════════════
//  侧边栏选中类型
// ═══════════════════════════════════════════════════════════════
type Selection =
  | { type: 'harnesses' }
  | { type: 'docker' }
  | { type: 'workspaces' };  // P0.18.9: 工作区授权区块

// ═══════════════════════════════════════════════════════════════
//  主组件
// ═══════════════════════════════════════════════════════════════
export function RuntimeSettingsPage() {
  // ── 数据状态 ──
  const [summary, setSummary] = useState<RuntimeSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection>({ type: 'harnesses' });

  // ── Docker 管理状态 ──
  const [dockerContainers, setDockerContainers] = useState<Array<{
    id: string; short_id: string; name: string; image: string; status: string; labels: Record<string, string> | null;
  }>>([]);
  const [dockerLoading, setDockerLoading] = useState(false);
  const [dockerError, setDockerError] = useState<string | null>(null);
  const [dockerLogsModal, setDockerLogsModal] = useState<{ containerId: string; logs: string } | null>(null);
  const [pullImageInput, setPullImageInput] = useState('');

  // ── P0.17 Runtime Environment 状态 ──
  const [envSnapshot, setEnvSnapshot] = useState<RuntimeEnvironmentSnapshot | null>(null);
  const [envLoading, setEnvLoading] = useState(false);
  const [envError, setEnvError] = useState<string | null>(null);
  const [rebuildBusy, setRebuildBusy] = useState(false);
  const [rebuildError, setRebuildError] = useState<string | null>(null);
  const [buildLogLines, setBuildLogLines] = useState<Array<{ line: string; ts: string }>>([]);
  const [buildLogOpen, setBuildLogOpen] = useState(false);
  const buildEventSourceRef = useRef<EventSource | null>(null);
  const pollingRef = useRef<number | null>(null);
  // 跟踪上一次构建活动状态，用来检测"新活动开始"以自动展开
  // 活动 = 构建运行中 / 有日志 / 刚完成（包括 failed）
  const buildActivityRef = useRef<{ isBuilding: boolean; hasLogs: boolean; finished: boolean }>({
    isBuilding: false,
    hasLogs: false,
    finished: false,
  });

  const loadEnvironment = useCallback(async () => {
    setEnvLoading(true);
    setEnvError(null);
    try {
      const snap = await apiClient.getRuntimeEnvironment();
      setEnvSnapshot(snap);
    } catch (e) {
      setEnvError(e instanceof Error ? e.message : String(e));
    } finally {
      setEnvLoading(false);
    }
  }, []);

  // 选 docker tab 时启动 polling
  const ensureEnvironmentPolling = useCallback(() => {
    if (pollingRef.current !== null) return;
    loadEnvironment();
    pollingRef.current = window.setInterval(() => {
      loadEnvironment();
    }, 3000);
  }, [loadEnvironment]);

  const stopEnvironmentPolling = useCallback(() => {
    if (pollingRef.current !== null) {
      window.clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  // tab 切换时启动/停止 polling
  useEffect(() => {
    if (selection.type === 'docker') {
      ensureEnvironmentPolling();
    } else {
      stopEnvironmentPolling();
    }
    return () => stopEnvironmentPolling();
  }, [selection, ensureEnvironmentPolling, stopEnvironmentPolling]);

  // 构建活动变化时自动展开日志卡片（仅在"新活动开始"那一刻展开，
  // 用户手动收起后不会被旧状态触发再次展开）
  useEffect(() => {
    const status = envSnapshot?.build.status;
    const isBuilding = status === 'running' || status === 'queued';
    const hasLogs = buildLogLines.length > 0;
    const finished = status === 'completed' || status === 'failed';
    const prev = buildActivityRef.current;
    const newlyBuilding = isBuilding && !prev.isBuilding;
    const newlyFinished = finished && !prev.finished;
    const newlyHasLogs = hasLogs && !prev.hasLogs;
    if (newlyBuilding || newlyFinished || newlyHasLogs) {
      setBuildLogOpen(true);
    }
    buildActivityRef.current = { isBuilding, hasLogs, finished };
  }, [envSnapshot?.build.status, buildLogLines.length]);

  const handleRebuild = useCallback(async () => {
    setRebuildBusy(true);
    setRebuildError(null);
    setBuildLogLines([]);
    try {
      const res = await apiClient.rebuildRuntimeEnvironment(false);
      // 订阅 SSE
      if (buildEventSourceRef.current) {
        buildEventSourceRef.current.close();
      }
      const es = apiClient.streamBuildLog(res.build_id);
      buildEventSourceRef.current = es;
      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as { line?: string; ts?: string; event?: string; exit_code?: number };
          if (data.event === 'done') {
            es.close();
            buildEventSourceRef.current = null;
            // 重新拉一次状态
            loadEnvironment();
          } else if (typeof data.line === 'string') {
            setBuildLogLines((prev) => [...prev, { line: data.line as string, ts: data.ts ?? new Date().toISOString() }]);
          }
        } catch {
          // ignore parse errors
        }
      };
      es.onerror = () => {
        es.close();
        buildEventSourceRef.current = null;
        loadEnvironment();
      };
    } catch (e) {
      setRebuildError(e instanceof Error ? e.message : String(e));
    } finally {
      setRebuildBusy(false);
    }
  }, [loadEnvironment]);

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

  // Load Docker containers
  const loadDockerContainers = useCallback(async () => {
    setDockerLoading(true);
    setDockerError(null);
    try {
      const res = await apiClient.listDockerContainers(true);
      setDockerContainers(res.containers || []);
    } catch (e) {
      setDockerError(e instanceof Error ? e.message : String(e));
    } finally {
      setDockerLoading(false);
    }
  }, []);

  const handlePullImage = useCallback(async () => {
    if (!pullImageInput.trim()) return;
    setDockerError(null);
    try {
      await apiClient.pullDockerImage(pullImageInput.trim());
      await loadDockerContainers();
      setPullImageInput('');
    } catch (e) {
      setDockerError(e instanceof Error ? e.message : String(e));
    }
  }, [pullImageInput, loadDockerContainers]);

  const handleGetLogs = useCallback(async (containerId: string) => {
    try {
      const res = await apiClient.getDockerContainerLogs(containerId, 400);
      setDockerLogsModal({ containerId: res.container_id, logs: res.logs });
    } catch (e) {
      setDockerError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const handleStopContainer = useCallback(async (containerId: string) => {
    try {
      await apiClient.stopDockerContainer(containerId);
      await loadDockerContainers();
    } catch (e) {
      setDockerError(e instanceof Error ? e.message : String(e));
    }
  }, [loadDockerContainers]);

  const handleRemoveContainer = useCallback(async (containerId: string) => {
    try {
      await apiClient.removeDockerContainer(containerId);
      await loadDockerContainers();
    } catch (e) {
      setDockerError(e instanceof Error ? e.message : String(e));
    }
  }, [loadDockerContainers]);

  // ════════════════════════════════════════════════════════════
  //  渲染：加载中
  // ════════════════════════════════════════════════════════════
  if (loading && !summary) {
    return (
      <div className="rs-loading">
        <div className="rs-spinner" />
        <span>正在加载运行环境...</span>
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
        {/* 适配器（基础设施，放底部） */}
        <div className="rs-sidebar-section">
          <div
            className={`rs-sidebar-item ${selection.type === 'harnesses' ? 'active' : ''}`}
            onClick={() => setSelection({ type: 'harnesses' })}
          >
            <SidebarIcon type="harness" />
            <span>适配器</span>
            <span className="rs-sidebar-badge">{s.harnesses.length}</span>
          </div>
          <div
            className={`rs-sidebar-item ${selection.type === 'docker' ? 'active' : ''}`}
            onClick={() => setSelection({ type: 'docker' })}
          >
            <SidebarIcon type="harness" />
            <span>运行环境</span>
            <span className="rs-sidebar-badge">容器</span>
          </div>
          {/* P0.18.9: 工作区授权区块 — 跳转 WorkspacesPage 或本面板渲染 */}
          <div
            className={`rs-sidebar-item ${selection.type === 'workspaces' ? 'active' : ''}`}
            onClick={() => setSelection({ type: 'workspaces' })}
          >
            <SidebarIcon type="harness" />
            <span>工作区</span>
            <span className="rs-sidebar-badge">授权</span>
          </div>
        </div>
      </aside>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：适配器面板
  // ════════════════════════════════════════════════════════════
  function renderHarnessPanel() {
    return (
      <div className="rs-panel">
        <div className="rs-panel-header">
          <div>
            <h2 className="rs-panel-title">适配器</h2>
            <p className="rs-panel-subtitle">已注册的运行时适配器类型</p>
          </div>
          <span className="status-pill status-pill-neutral">
            {s.harnesses.length} 个适配器
          </span>
        </div>
        <div className="rs-harness-grid">
          {s.harnesses.map((h) => (
            <div key={h.type} className="rs-harness-card">
              <div className="rs-harness-card-top">
                <div className="rs-harness-card-icon">
                  {h.type === 'deterministic' ? 'D' : h.type === 'opencode' ? 'O' : h.type === 'local_llm' ? 'L' : h.type.charAt(0).toUpperCase()}
                </div>
                <div className="rs-harness-card-info">
                  <div className="rs-harness-card-name">{h.label}</div>
                  <div className="rs-harness-card-type font-mono">{h.type}</div>
                </div>
                <span className="status-pill status-pill-success">
                  <span className="status-dot status-dot-success" style={{ width: 6, height: 6 }} />
                  在线
                </span>
              </div>
              <div className="rs-harness-card-desc">
                {HARNESS_DESCRIPTIONS[h.type] || '运行时适配器'}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：Docker 管理面板（基础）
  // ════════════════════════════════════════════════════════════
  // ── P0.17: 4 个区块的 Runtime Environment 面板 ──
  function overallLabel(overall: OverallKind): string {
    const map: Record<OverallKind, string> = {
      ready: 'DAG 运行环境已就绪',
      checking: '正在检查...',
      stale: '镜像与源码不一致',
      missing: '镜像不存在',
      incompatible: '镜像协议不兼容',
      build_failed: '构建失败',
      docker_error: 'Docker 不可用',
      building: '正在构建镜像',
    };
    return map[overall];
  }

  function overallColor(overall: OverallKind): string {
    if (overall === 'ready') return 'var(--color-success, #16a34a)';
    if (overall === 'building' || overall === 'checking') return 'var(--color-info, #2563eb)';
    return 'var(--color-danger, #dc2626)';
  }

  function renderEnvironmentHealthCard() {
    const snap = envSnapshot;
    const overall: OverallKind = snap?.overall ?? 'checking';
    const borderColor = overallColor(overall);
    const d = snap?.docker;
    const w = snap?.worker_image;
    return (
      <div className="rs-detail-section" style={{ borderTop: `3px solid ${borderColor}` }}>
        <div className="rs-detail-section-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ color: borderColor, fontSize: 18 }}>●</span>
            <span className="rs-detail-section-title">DAG 运行环境</span>
            <span style={{ color: borderColor, fontWeight: 600 }}>{snap ? overallLabel(snap.overall) : '加载中…'}</span>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn-secondary btn-sm" onClick={loadEnvironment} disabled={envLoading}>
              {envLoading ? '检查中...' : '重新检查'}
            </button>
            <button
              className="btn-primary btn-sm"
              onClick={handleRebuild}
              disabled={
                rebuildBusy ||
                !snap ||
                snap.build.status === 'running' ||
                snap.build.status === 'queued'
              }
            >
              {rebuildBusy ||
              (snap && (snap.build.status === 'running' || snap.build.status === 'queued'))
                ? '构建中...'
                : '重新构建'}
            </button>
          </div>
        </div>
        {envError && <div className="rs-config-hint" style={{ color: 'var(--color-danger, #dc2626)' }}>错误：{envError}</div>}
        {rebuildError && <div className="rs-config-hint" style={{ color: 'var(--color-danger, #dc2626)' }}>rebuild 错误：{rebuildError}</div>}
        {!snap || !d || !w ? (
          <SkeletonBlock lines={3} />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, padding: '12px 16px' }}>
            <div>
              <div className="rs-config-hint">Docker Engine</div>
              <div className="font-mono" style={{ fontWeight: 600 }}>
                {d.status === 'ready' ? d.version : '—'}
              </div>
              <div className="rs-config-hint font-mono">{d.platform || '—'}</div>
              {d.reason && <div className="rs-config-hint" style={{ color: 'var(--color-danger)' }}>{d.reason}</div>}
            </div>
            <div>
              <div className="rs-config-hint">当前 Worker 镜像</div>
              <div className="font-mono" style={{ fontWeight: 600 }}>{w.tag || '—'}</div>
              <div className="rs-config-hint font-mono">{w.image_id ? w.image_id.slice(7, 19) : '—'}</div>
            </div>
            <div>
              <div className="rs-config-hint">当前源码指纹</div>
              <div className="font-mono" style={{ fontWeight: 600 }}>
                {snap.source.fingerprint ? snap.source.fingerprint.slice(0, 16) : '—'}
              </div>
              <div className="rs-config-hint font-mono">{snap.source.git_status}</div>
            </div>
            <div>
              <div className="rs-config-hint">协议版本</div>
              <div className="font-mono" style={{ fontWeight: 600 }}>{w.protocol_version || '—'}</div>
              <div className="rs-config-hint">{w.compatibility === 'current' ? '✓ 兼容' : '⚠ ' + w.compatibility}</div>
            </div>
            <div>
              <div className="rs-config-hint">已连接 Worker</div>
              <div className="font-mono" style={{ fontWeight: 600, fontSize: 18 }}>{snap.connected_workers}</div>
              <div className="rs-config-hint">活跃 subagent</div>
            </div>
          </div>
        )}
      </div>
    );
  }

  function renderWorkerImageList() {
    const snap = envSnapshot;
    const images = snap?.images ?? [];
    const isEmpty = snap !== null && images.length === 0;
    return (
      <div className="rs-detail-section">
        <div className="rs-detail-section-header">
          <span className="rs-detail-section-title">📦 Worker 镜像</span>
          <span className="rs-config-hint">
            {snap === null ? '加载中…' : `共 ${images.length} 个`}
          </span>
        </div>
        {snap === null ? (
          <SkeletonBlock lines={2} />
        ) : isEmpty ? (
          <div className="rs-config-hint">未找到 agentops-worker 镜像，点击「重新构建」生成。</div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 8, padding: '12px 16px' }}>
            {images.map((img) => {
              // 优先用 built_at（每次 docker build 都会通过 --label 更新），
              // fallback 1：img.labels["agentops.built_at"]（旧后端响应没顶层 built_at 字段时从 labels 取）
              // fallback 2：created_at（镜像层时间，CACHED 时不准）
              const builtAt = img.built_at || img.labels?.['agentops.built_at'] || img.created_at || '';
              const builtAtDisplay = builtAt
                ? new Date(builtAt).toLocaleString('zh-CN', {
                    month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                  })
                : '—';
              const isFresh = (() => {
                if (!builtAt) return false;
                const t = new Date(builtAt).getTime();
                return Number.isFinite(t) && Date.now() - t < 60_000;
              })();
              return (
                <div key={img.id} className="rs-docker-row" style={{ padding: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <div style={{ fontWeight: 600 }}>
                        {img.tags[0] || img.short_id}
                        {img.selected && (
                          <span className="status-pill status-pill-info" style={{ marginLeft: 8 }}>当前使用</span>
                        )}
                        {isFresh && (
                          <span className="status-pill status-pill-success" style={{ marginLeft: 8 }}>
                            ✓ 刚构建
                          </span>
                        )}
                      </div>
                      <div className="rs-config-hint font-mono">
                        {img.short_id} · {(img.size_bytes / 1024 / 1024).toFixed(1)} MB · 构建 {builtAtDisplay}
                      </div>
                      <div className="rs-config-hint font-mono">
                        协议 {img.protocol_version} · 指纹 {img.source_fingerprint.slice(0, 16)}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  function renderConnectedWorkers() {
    const snap = envSnapshot;
    const workers = snap?.workers ?? [];
    const isEmpty = snap !== null && workers.length === 0;
    return (
      <div className="rs-detail-section">
        <div className="rs-detail-section-header">
          <span className="rs-detail-section-title">🖥️ 正在运行的 Worker</span>
          <span className="rs-config-hint">
            {snap === null ? '加载中…' : `共 ${snap.connected_workers} 个`}
          </span>
        </div>
        {snap === null ? (
          <SkeletonBlock lines={3} />
        ) : isEmpty ? (
          <div className="rs-config-hint">当前没有已连接的 Worker；新 DAG 任务会按需启动。</div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 6, padding: '12px 16px' }}>
            {workers.map((w) => (
              <div key={w.subagent_id + ':' + w.lease_generation} className="rs-docker-row" style={{ padding: 8 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <div>
                    <span className="font-mono" style={{ fontWeight: 600 }}>{w.actor_id}</span>
                    <span className="rs-config-hint" style={{ marginLeft: 8 }}>{w.runtime_placement}</span>
                  </div>
                  <span className="rs-config-hint font-mono">{w.started_at}</span>
                </div>
                {w.container_id && (
                  <div className="rs-config-hint font-mono">
                    container: {w.container_id.slice(0, 19)}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  function renderBuildLog() {
    const snap = envSnapshot;
    const status = snap?.build.status;
    const isBuilding = status === 'running' || status === 'queued';
    const hasLogs = buildLogLines.length > 0;
    const finished = status === 'completed' || status === 'failed';
    const open = buildLogOpen;
    return (
      <div className="rs-detail-section">
        <div
          className="rs-detail-section-header rs-collapsible-header"
          onClick={() => setBuildLogOpen((v) => !v)}
          style={{ cursor: 'pointer', userSelect: 'none' }}
        >
          <span className="rs-detail-section-title">
            <span
              style={{
                display: 'inline-block',
                transition: 'transform 0.15s',
                transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
                fontSize: 10,
                marginRight: 4,
              }}
            >
              ▶
            </span>
            📜 构建日志
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {status && (
              <span className="status-pill status-pill-info">{status}</span>
            )}
            {!open && hasLogs && (
              <span className="rs-config-hint">{buildLogLines.length} 行</span>
            )}
          </span>
        </div>
        {open && (
          <div
            style={{
              background: 'var(--color-bg-code, #0a0a0a)',
              color: 'var(--color-text-code, #d4d4d4)',
              padding: 12,
              margin: '0 16px 12px 16px',
              borderRadius: 6,
              fontFamily: 'var(--font-mono, monospace)',
              fontSize: 12,
              maxHeight: 320,
              overflowY: 'auto',
              whiteSpace: 'pre-wrap',
              lineHeight: 1.5,
            }}
          >
            {hasLogs ? (
              buildLogLines.map((e, i) => (
                <div key={i} className="font-mono">{e.line}</div>
              ))
            ) : (
              <span style={{ opacity: 0.6 }}>
                {isBuilding ? '等待日志…' : '暂无构建活动；点击「重新构建」开始一次构建。'}
              </span>
            )}
          </div>
        )}
      </div>
    );
  }

  function renderDockerPanel() {
    return (
      <div className="rs-panel">
        <div className="rs-panel-header">
          <div>
            <h2 className="rs-panel-title">运行环境</h2>
            <p className="rs-panel-subtitle">检查 Docker、管理 Worker 镜像，并查看正在运行的 Worker</p>
          </div>
          <div>
            <button className="btn-secondary btn-sm" onClick={async () => { await loadDockerContainers(); }}>
              刷新容器列表
            </button>
          </div>
        </div>
        <div className="rs-panel-body">
          {renderEnvironmentHealthCard()}
          {renderWorkerImageList()}
          {renderConnectedWorkers()}
          {renderBuildLog()}

          {/* 兼容旧版：拉取镜像 + 容器列表（保留 Copilot 之前的 CRUD 入口） */}
          <div className="rs-detail-section">
            <div className="rs-detail-section-header">
              <span className="rs-detail-section-title">容器列表（本地 Docker）</span>
            </div>
            <div style={{ marginBottom: 8 }}>
              <input
                className="input-base"
                placeholder="镜像名（如 alpine:3.18）"
                value={pullImageInput}
                onChange={(e) => setPullImageInput(e.target.value)}
                style={{ width: 320, marginRight: 8 }}
              />
              <button className="btn-primary btn-sm" onClick={async () => { await handlePullImage(); }}>拉取镜像</button>
            </div>
            {dockerError && <div style={{ color: 'var(--color-danger, #dc2626)', marginBottom: 8 }}>{dockerError}</div>}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 8 }}>
              {dockerContainers.map((c) => (
                <div key={c.id} className="rs-docker-row">
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <div style={{ fontWeight: 600 }}>{c.name} <span className="font-mono" style={{ marginLeft: 8 }}>{c.short_id}</span></div>
                      <div style={{ color: 'var(--color-text-secondary, #666)' }}>{c.image} · {c.status}</div>
                    </div>
                    <div>
                      <button className="btn-secondary btn-sm" onClick={async () => { await handleGetLogs(c.id); }}>日志</button>
                      <button className="btn-secondary btn-sm" onClick={async () => { await handleStopContainer(c.id); }}>停止</button>
                      <button className="btn-danger-outline btn-sm" onClick={async () => { if (confirm('确认删除容器？')) await handleRemoveContainer(c.id); }}>删除</button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ════════════════════════════════════════════════════════════
  //  渲染：工作区授权面板（原一级菜单「工作区授权」整页并入）
  // ════════════════════════════════════════════════════════════
  function renderWorkspacesPanel() {
    return <WorkspacesPage />;
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
          {selection.type === 'harnesses' && renderHarnessPanel()}
          {selection.type === 'docker' && renderDockerPanel()}
          {selection.type === 'workspaces' && renderWorkspacesPanel()}
        </main>
      </div>

      {/* 弹窗 */}
      {dockerLogsModal && (
        <div className="rs-modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) setDockerLogsModal(null); }}>
          <div className="rs-modal" style={{ maxWidth: '760px', width: '90%' }}>
            <div className="rs-modal-header">
              <span>容器日志 — {dockerLogsModal.containerId}</span>
              <button className="rs-modal-close" onClick={() => setDockerLogsModal(null)}>×</button>
            </div>
            <div className="rs-modal-body">
              <pre style={{ whiteSpace: 'pre-wrap', maxHeight: '60vh', overflow: 'auto' }}>{dockerLogsModal.logs}</pre>
            </div>
            <div className="rs-modal-footer">
              <button className="btn-secondary btn-sm" onClick={() => setDockerLogsModal(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// ═══════════════════════════════════════════════════════════════
//  骨架占位组件
// ═══════════════════════════════════════════════════════════════
function SkeletonBlock({ lines = 3 }: { lines?: number }) {
  return (
    <div style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
      {Array.from({ length: lines }).map((_, i) => (
        <div key={i} className="rs-skeleton-bar" style={{ width: `${85 - i * 8}%` }} />
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
//  侧边栏图标组件
// ═══════════════════════════════════════════════════════════════
function SidebarIcon({ type }: { type: 'harness' }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
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

/* ── 右侧内容区 ── */
.rs-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 28px 32px;
  background: var(--color-bg-base);
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

/* ── 适配器网格 ── */
.rs-harness-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}

.rs-harness-card {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  transition: border-color 0.15s;
}

.rs-harness-card:hover {
  border-color: var(--color-border-default);
}

.rs-harness-card-top {
  display: flex;
  align-items: center;
  gap: 10px;
}

.rs-harness-card-icon {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-md);
  background: var(--color-primary-tint);
  color: var(--color-primary-soft);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 700;
  flex-shrink: 0;
}

.rs-harness-card-info {
  flex: 1;
  min-width: 0;
}

.rs-harness-card-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.rs-harness-card-type {
  font-size: 11px;
  color: var(--color-text-tertiary);
  margin-top: 2px;
}

.rs-harness-card-desc {
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.5;
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

/* ── 骨架占位 ── */
.rs-skeleton-bar {
  height: 12px;
  border-radius: 4px;
  background: linear-gradient(
    90deg,
    var(--color-bg-elevated, rgba(255, 255, 255, 0.04)) 0%,
    var(--color-border-default, rgba(255, 255, 255, 0.10)) 50%,
    var(--color-bg-elevated, rgba(255, 255, 255, 0.04)) 100%
  );
  background-size: 200% 100%;
  animation: rs-skel-shimmer 1.4s ease-in-out infinite;
}

@keyframes rs-skel-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* ── 可折叠标题 ── */
.rs-collapsible-header {
  transition: background-color 0.15s;
}
.rs-collapsible-header:hover {
  background: var(--color-bg-elevated, rgba(255, 255, 255, 0.04));
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

/* ── 响应式 ── */
@media (max-width: 768px) {
  .rs-sidebar {
    width: 200px;
  }
}

`;
