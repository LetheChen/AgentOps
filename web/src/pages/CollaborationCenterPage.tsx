import { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { apiClient } from '../lib/api';
import type { CollaborationGraph, GraphNode, HandoffInfo, TimelineEntry } from '../lib/types';
import type { PageId } from '../App';
import { BusinessLaneView } from '../components/collaboration/BusinessLaneView';
import { DeveloperDagView } from '../components/collaboration/DeveloperDagView';
import { ChatTimelinePane } from '../components/collaboration/ChatTimelinePane';
import { LeftSidebar } from '../components/collaboration/LeftSidebar';
import { RightSidebar } from '../components/collaboration/RightSidebar';
import { SkillCatalog } from '../components/collaboration/SkillCatalog';

interface CollaborationCenterPageProps {
  onNavigate?: (page: PageId) => void;
  /** 回放入口：从 RunHistory 点「回放」传来的 run_id，预选该 run */
  initialRunId?: string | null;
  /** 工作台实时 sessionId：让协作页自动跟随当前活跃 session（实时过程可视化） */
  liveSessionId?: string | null;
}

interface RunSummary {
  run_id: string;
  workflow_id?: string;
  status?: string;
  started_at?: string;
  finished_at?: string | null;
  /** 所属 Session ID（旧 run 回填为 run_id）。 */
  session_id?: string;
}

/** 实时轮询间隔（ms），仅当 run status=running 时启用 */
const POLL_INTERVAL = 3000;

/**
 * CollaborationCenterPage — 协作可视化中心（过程可视化）
 *
 * 职责：实时 DAG workflow 过程可视化
 *   - liveSessionId：自动跟随工作台当前活跃 session
 *   - 实时轮询：run status=running 时每 3s 刷新 collaboration graph
 *   - 回放：选历史 run 即回放（fetch-once）
 *
 * 数据源：/api/audit/runs/{id}/collaboration-graph
 */
export function CollaborationCenterPage({ onNavigate, initialRunId, liveSessionId }: CollaborationCenterPageProps) {
  // ─── 全局状态 ───
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [graphData, setGraphData] = useState<CollaborationGraph | null>(null);
  const [timelineData, setTimelineData] = useState<TimelineEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // ─── UI 状态 ───
  const [view, setView] = useState<'business' | 'developer'>('business');
  const [mainTab, setMainTab] = useState<'graph' | 'chat' | 'actors'>('graph');
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [rightTab, setRightTab] = useState<'delivery' | 'detail'>('detail');
  const [drawerOpen, setDrawerOpen] = useState(false);

  // ─── Run 列表分页状态（按需加载）──
  // 替代原 limit=50 一次性拉：首次 50，浮层滚动触底 IntersectionObserver 触发追加
  const [runDropdownOpen, setRunDropdownOpen] = useState(false);
  const [runsHasMore, setRunsHasMore] = useState(false);
  const [runsLoadingMore, setRunsLoadingMore] = useState(false);
  const [runsTotal, setRunsTotal] = useState(0);
  const dropdownPanelRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);

  // 实时轮询：用 ref 控制 cleanup
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 选中节点时自动打开抽屉
  const handleSelectNode = useCallback((id: string) => {
    setSelectedNodeId(id);
    setDrawerOpen(true);
  }, []);

  // ─── 1. 加载 run 列表（首次 50 条，剩余按需加载）──
  // 抽出 fetchRunsPage 让初次加载和 loadMore 共用同一套去重 / 翻页逻辑
  const fetchRunsPage = useCallback(async (offset: number, append: boolean) => {
    if (append) setRunsLoadingMore(true);
    try {
      const resp = await apiClient.auditListRuns({ limit: 50, offset });
      const items = ((resp.runs || []) as unknown as RunSummary[])
        .filter((r) => r.workflow_id);
      setRunsTotal(resp.total || 0);
      setRuns((prev) => {
        // 初次加载或 selectedRunId 失效时全量替换；追加时去重
        if (!append) return items;
        const seen = new Set(prev.map((r) => r.run_id));
        return [...prev, ...items.filter((r) => !seen.has(r.run_id))];
      });
      // 是否还有更多：依赖后端返回 total 与已加载数比较
      const loaded = offset + items.length;
      setRunsHasMore(loaded < (resp.total || 0));
    } catch (err) {
      console.warn('Failed to list runs:', err);
    } finally {
      if (append) setRunsLoadingMore(false);
    }
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlRunId = params.get('runId') || params.get('run_id');
    fetchRunsPage(0, false).then(() => {
      // 优先级：initialRunId > URL ?runId= > 第一个 run
      setRuns((cur) => {
        if (cur.length === 0 || selectedRunId) return cur;
        const fromInitial = initialRunId ? cur.find((r) => r.run_id === initialRunId) : null;
        const fromUrl = urlRunId ? cur.find((r) => r.run_id === urlRunId) : null;
        setSelectedRunId((fromInitial ?? fromUrl ?? cur[0]).run_id);
        return cur;
      });
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ─── 1c. 浮层滚动触底 → loadMore（IntersectionObserver 监听 sentinel）──
  const loadMoreRuns = useCallback(() => {
    if (runsLoadingMore || !runsHasMore) return;
    fetchRunsPage(runs.length, true);
  }, [fetchRunsPage, runs.length, runsHasMore, runsLoadingMore]);

  useEffect(() => {
    if (!runDropdownOpen || !sentinelRef.current || !runsHasMore) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) loadMoreRuns();
      },
      { root: dropdownPanelRef.current ?? null, rootMargin: '80px' },
    );
    obs.observe(sentinelRef.current);
    return () => obs.disconnect();
  }, [runDropdownOpen, runsHasMore, loadMoreRuns]);

  // ─── 1d. 点击浮层外部关闭下拉 ───
  useEffect(() => {
    if (!runDropdownOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (dropdownPanelRef.current?.contains(e.target as Node)) return;
      // 触发按钮本身也排除（按钮 onClick 处理开/关）
      const target = e.target as HTMLElement;
      if (target.closest('[data-run-dropdown-trigger]')) return;
      setRunDropdownOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [runDropdownOpen]);

  // ─── 1b. liveSessionId 变化 → 自动选中对应 run ───
  // 工作台创建/切换 session 时，协作页自动跟随
  useEffect(() => {
    if (!liveSessionId) return;
    // 在 run 列表中找 session_id 匹配的 run
    const matched = runs.find(r => r.session_id === liveSessionId || r.run_id === liveSessionId);
    if (matched && matched.run_id !== selectedRunId) {
      setSelectedRunId(matched.run_id);
    } else if (!matched && runs.length === 0) {
      // run 列表还没加载，先直接选中该 run_id（后端可能按 session_id 查询）
      setSelectedRunId(liveSessionId);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveSessionId, runs]);

  // ─── 2. run 切换时拉协作图 + timeline（一次性加载）───
  const fetchGraph = useCallback((runId: string) => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setGraphData(null);
    setTimelineData([]);
    setSelectedNodeId(null);
    apiClient.getCollaborationGraph(runId)
      .then((g) => { if (!cancelled) setGraphData(g); })
      .catch((err) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    apiClient.getCollaborationTimeline(runId)
      .then((resp) => { if (!cancelled) setTimelineData(resp.timeline || []); })
      .catch((err) => console.warn('Failed to fetch timeline:', err));
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!selectedRunId) return;
    const cleanup = fetchGraph(selectedRunId);
    return cleanup;
  }, [selectedRunId, fetchGraph]);

  // ─── 2b. 实时轮询：run status=running 时每 POLL_INTERVAL 刷新 graph ───
  // 不重新 loading（避免闪烁），静默更新 graphData
  useEffect(() => {
    if (!selectedRunId || !graphData) return;
    const isRunning = graphData.status === 'running';
    if (!isRunning) {
      // 非运行状态，清理已有定时器
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    // 启动轮询
    pollRef.current = setInterval(() => {
      apiClient.getCollaborationGraph(selectedRunId)
        .then((g) => setGraphData(g))
        .catch(() => {/* 静默失败，下次重试 */});
      apiClient.getCollaborationTimeline(selectedRunId)
        .then((resp) => setTimelineData(resp.timeline || []))
        .catch(() => {});
    }, POLL_INTERVAL);
    return () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    };
  }, [selectedRunId, graphData?.status]);

  // ─── 派生 ───
  const selectedGraphNode: GraphNode | undefined = useMemo(
    () => graphData?.nodes.find((n) => n.node_id === selectedNodeId),
    [graphData, selectedNodeId]
  );
  const selectedHandoffs: HandoffInfo[] = useMemo(
    () => graphData?.handoffs.filter((h) => h.from_node === selectedNodeId) || [],
    [graphData, selectedNodeId]
  );
  const runStatus = graphData?.status || 'unknown';
  const isRunning = runStatus === 'running';
  const hasWorkflow = (graphData?.nodes?.length ?? 0) > 0;

  // 重跑节点：调 /api/agent/runs/{run_id}/resume（后端会清节点文件后复用 run_id 重新跑）
  const handleRerunNode = useCallback(async (nodeId: string, onlyNode: boolean) => {
    if (!graphData) return;
    const verb = onlyNode ? '仅重试该节点' : '重跑下游链';
    console.log(`[CollaborationCenter] 请求${verb}: ${nodeId}`);
    try {
      const resp = await apiClient.resumeRun(
        graphData.run_id,
        graphData.workflow_id,
        {},
        nodeId,
        onlyNode,
      );
      console.log(`[CollaborationCenter] ${verb}已提交:`, resp);
      // 立刻拉一次图，等几秒让后端先把节点置为 running；3s 轮询会自动续上
      try {
        const g = await apiClient.getCollaborationGraph(graphData.run_id);
        setGraphData(g);
      } catch {/* 静默失败，轮询兜底 */}
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`[CollaborationCenter] ${verb}失败:`, err);
      alert(`❌ ${verb}失败：${msg}\n\n可能原因：\n· run 已结束且 workspace 文件被清理\n· 节点不属于当前 workflow\n· 后端服务异常`);
    }
  }, [graphData]);

  // 切换视角联动
  const switchView = useCallback((v: 'business' | 'developer') => {
    setView(v);
  }, []);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: 'var(--bg)' }}>
      {/* ─────── 顶部栏 ─────── */}
      <div style={{
        height: 56, display: 'flex', alignItems: 'center', gap: 16,
        padding: '0 20px', borderBottom: '1px solid var(--border)',
        background: 'var(--panel)', flexShrink: 0,
      }}>
        <h1 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
          📊 协作可视化
        </h1>

        {/* run 切换器（自定义下拉 + 按需加载） */}
        <div style={{ position: 'relative' }}>
          <button
            data-run-dropdown-trigger
            onClick={() => setRunDropdownOpen((v) => !v)}
            title="切换 run（向下滚动加载更多）"
            style={{
              height: 28, padding: '0 28px 0 10px', fontSize: 11,
              background: 'var(--panel-2, #1a2440)', color: 'var(--text, #e6ecf5)',
              border: '1px solid var(--border)', borderRadius: 6,
              minWidth: 420, maxWidth: 560, cursor: 'pointer',
              fontFamily: 'ui-monospace, monospace',
              textAlign: 'left', position: 'relative',
            }}
          >
            {(() => {
              const cur = runs.find((r) => r.run_id === selectedRunId);
              if (!cur) return runs.length === 0 ? '（加载中...）' : '（无 run）';
              return `${cur.workflow_id || '(对话)'} · ${cur.run_id} · ${cur.status || '?'}`;
            })()}
            <span style={{
              position: 'absolute', right: 8, top: '50%',
              transform: `translateY(-50%) rotate(${runDropdownOpen ? 180 : 0}deg)`,
              transition: 'transform .15s', fontSize: 9, opacity: 0.7,
            }}>▾</span>
          </button>

          {runDropdownOpen && (
            <div
              ref={dropdownPanelRef}
              style={{
                position: 'absolute', top: 32, left: 0, zIndex: 1000,
                minWidth: 480, maxWidth: 640, maxHeight: 420,
                background: 'var(--panel, #131c2e)', border: '1px solid var(--border)',
                borderRadius: 6, boxShadow: '0 8px 24px rgba(0,0,0,.45)',
                display: 'flex', flexDirection: 'column', overflow: 'hidden',
              }}
            >
              {/* 顶部计数条 */}
              <div style={{
                padding: '6px 10px', fontSize: 10, color: 'var(--text-dim, #8b97b0)',
                borderBottom: '1px solid var(--border)',
                display: 'flex', justifyContent: 'space-between',
                background: 'var(--panel-2, #1a2440)',
              }}>
                <span>📂 已加载 {runs.length} / {runsTotal || '?'}</span>
                <span>{runsHasMore ? '⬇ 滚动加载更多' : '✓ 已加载全部'}</span>
              </div>

              {/* 列表 */}
              <div style={{ flex: 1, overflowY: 'auto' }}>
                {runs.length === 0 && (
                  <div style={{ padding: 14, fontSize: 11, color: '#8b97b0', textAlign: 'center' }}>
                    暂无 run
                  </div>
                )}
                {runs.map((r) => {
                  const selected = r.run_id === selectedRunId;
                  return (
                    <div
                      key={r.run_id}
                      onClick={() => { setSelectedRunId(r.run_id); setRunDropdownOpen(false); }}
                      style={{
                        padding: '6px 10px', fontSize: 11,
                        fontFamily: 'ui-monospace, monospace',
                        cursor: 'pointer',
                        background: selected ? 'rgba(59,130,246,.18)' : 'transparent',
                        borderLeft: `3px solid ${selected ? 'var(--accent, #3b82f6)' : 'transparent'}`,
                        color: 'var(--text, #e6ecf5)',
                      }}
                      onMouseEnter={(e) => {
                        if (!selected) (e.currentTarget as HTMLElement).style.background = 'rgba(59,130,246,.08)';
                      }}
                      onMouseLeave={(e) => {
                        if (!selected) (e.currentTarget as HTMLElement).style.background = 'transparent';
                      }}
                    >
                      {`${r.workflow_id || '(对话)'} · ${r.run_id} · ${r.status || '?'}`}
                    </div>
                  );
                })}

                {/* 触底 sentinel：IntersectionObserver 观察此 div 是否进入视口 */}
                <div ref={sentinelRef} style={{ height: 1 }} />

                {runsLoadingMore && (
                  <div style={{ padding: '8px 10px', fontSize: 10, color: '#8b97b0', textAlign: 'center' }}>
                    ⟳ 加载中...
                  </div>
                )}
                {!runsHasMore && runs.length > 0 && (
                  <div style={{ padding: '8px 10px', fontSize: 10, color: '#475569', textAlign: 'center' }}>
                    — 到底了 —
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* 双视角 toggle */}
        <div style={{
          display: 'flex', background: 'var(--panel-2, #1a2440)',
          borderRadius: 8, padding: 2, gap: 2,
        }}>
          <button
            onClick={() => switchView('business')}
            style={{
              padding: '5px 14px', border: 0,
              background: view === 'business' ? 'var(--accent)' : 'transparent',
              color: view === 'business' ? '#fff' : 'var(--text-dim, #8b97b0)',
              fontSize: 12, borderRadius: 6, cursor: 'pointer',
            }}
          >
            🎯 业务视角
          </button>
          <button
            onClick={() => switchView('developer')}
            style={{
              padding: '5px 14px', border: 0,
              background: view === 'developer' ? 'var(--accent)' : 'transparent',
              color: view === 'developer' ? '#fff' : 'var(--text-dim, #8b97b0)',
              fontSize: 12, borderRadius: 6, cursor: 'pointer',
            }}
          >
            📐 开发者视角
          </button>
        </div>

        <div style={{ marginLeft: 'auto', display: 'flex', gap: 14, alignItems: 'center', fontSize: 12, color: 'var(--text-dim, #8b97b0)' }}>
          {isRunning && (
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{
                width: 8, height: 8, borderRadius: '50%',
                background: '#3b82f6', boxShadow: '0 0 8px #3b82f6',
                animation: 'pulse 1.5s infinite',
              }} />
              实时运行中
            </span>
          )}
          <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            节点 <strong style={{ color: 'var(--text, #e6ecf5)', fontWeight: 600 }}>
              {graphData ? `${graphData.nodes.filter((n) => n.status === 'completed').length}/${graphData.nodes.length}` : '0/0'}
            </strong>
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            tokens <strong style={{ color: 'var(--text, #e6ecf5)', fontWeight: 600 }}>
              {graphData?.nodes.reduce((sum, n) => sum + (n.token_usage || 0), 0).toLocaleString() || '0'}
            </strong>
          </span>
        </div>
      </div>

      {/* ─────── 主体 2 栏（左 + 中） ─────── */}
      <div style={{
        flex: 1, display: 'grid',
        gridTemplateColumns: '240px 1fr',
        minHeight: 0, overflow: 'hidden',
      }}>
        {/* 左侧栏 */}
        <LeftSidebar
          graphData={graphData}
          selectedNodeId={selectedNodeId}
          onSelectNode={handleSelectNode}
        />

        {/* 中部 Canvas */}
        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, background: 'var(--bg, #0b1220)' }}>
          {/* Canvas Header */}
          <div style={{
            padding: '10px 16px', borderBottom: '1px solid var(--border)',
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            background: 'var(--panel, #131c2e)',
          }}>
            {/* 状态图例 */}
            <div style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--text-dim, #8b97b0)' }}>
              <LegendItem color="#10b981" label="已完成" />
              <LegendItem color="#3b82f6" label="运行中" />
              <LegendItem color="#ef4444" label="失败" />
              <LegendItem color="#6b7280" label="跳过" />
              <LegendItem color="#fbbf24" label="待开始" />
            </div>
            {/* 视角提示 */}
            <span style={{ fontSize: 11, color: 'var(--text-dim, #8b97b0)' }}>
              {view === 'business'
                ? '🎯 业务视角 · 业务角色名 + Handoff 气泡'
                : '📐 开发者视角 · agent ID / harness / payload JSON'}
            </span>
          </div>

          {/* 主区 tab */}
          <div style={{
            display: 'flex', gap: 4, padding: '8px 16px 0',
            borderBottom: '1px solid var(--border)',
            background: 'var(--panel, #131c2e)',
          }}>
            <MainTab id="graph" active={mainTab === 'graph'} onClick={() => setMainTab('graph')}>
              🗺️ 协作全景
            </MainTab>
            <MainTab id="chat" active={mainTab === 'chat'} onClick={() => setMainTab('chat')}>
              💬 对话时间线
            </MainTab>
            <MainTab id="actors" active={mainTab === 'actors'} onClick={() => setMainTab('actors')}>
              👥 Actor 汇总
            </MainTab>
          </div>

          {/* tab pane */}
          {error && (
            <div style={{ padding: 16, color: 'var(--state-error, #EF4444)', fontSize: 13 }}>
              ⚠️ 加载协作图失败：{error}
            </div>
          )}
          {mainTab === 'graph' && (
            <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
              {!graphData && loading && (
                <div className="widget-empty-state" style={{ padding: 40 }}>加载协作图中...</div>
              )}
              {!graphData && !loading && (
                <div className="widget-empty-state" style={{ padding: 40 }}>暂无协作图数据</div>
              )}
              {graphData && (
                <>
                  {!hasWorkflow && (
                    <div style={{
                      padding: '8px 16px', fontSize: 12, color: '#fbbf24',
                      background: 'rgba(251,191,36,.08)', borderBottom: '1px solid rgba(251,191,36,.3)',
                    }}>
                      ⚠️ 该 run 是对话 session（无关联 workflow），以下仅展示 handoff/timeline。
                      请选择一个 workflow run 查看完整 DAG。
                    </div>
                  )}
                  {view === 'business' ? (
                    <BusinessLaneView
                      graphData={graphData}
                      timelineData={timelineData}
                      selectedNodeId={selectedNodeId}
                      onSelectNode={handleSelectNode}
                    />
                  ) : (
                    <DeveloperDagView
                      graphData={graphData}
                      selectedNodeId={selectedNodeId}
                      onSelectNode={handleSelectNode}
                    />
                  )}
                </>
              )}
            </div>
          )}
          {mainTab === 'chat' && (
            <ChatTimelinePane
              graphData={graphData}
              timelineData={timelineData}
              runId={selectedRunId}
            />
          )}
          {mainTab === 'actors' && (
            <ActorSummaryTab graphData={graphData} loading={loading} onSelectNode={handleSelectNode} />
          )}
        </div>

      </div>

      {/* ─────── 详情抽屉 ─────── */}
      {!drawerOpen && selectedGraphNode && (
        <button
          className="dag-drawer-trigger"
          onClick={() => setDrawerOpen(true)}
          title="打开节点详情"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 6h18M3 12h18M3 18h18" />
          </svg>
          {selectedHandoffs.length > 0 && (
            <span className="dag-drawer-trigger-badge">{selectedHandoffs.length}</span>
          )}
        </button>
      )}
      <div
        className={`dag-detail-drawer-overlay ${drawerOpen ? 'open' : ''}`}
        onClick={() => setDrawerOpen(false)}
      />
      <div className={`dag-detail-drawer ${drawerOpen ? 'open' : ''}`}>
        <div className="dag-detail-drawer-header">
          <div className="dag-detail-drawer-title">
            {rightTab === 'detail' ? '🔍 节点详情' : '📦 产物瀑布'}
            {selectedGraphNode && (
              <span style={{ fontSize: 11, color: 'var(--text-dim, #8b97b0)', fontWeight: 400 }}>
                · {selectedGraphNode.node_id}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <button
              onClick={() => setRightTab('delivery')}
              style={{
                padding: '3px 8px', fontSize: 10,
                background: rightTab === 'delivery' ? 'var(--accent, #3b82f6)' : 'transparent',
                color: rightTab === 'delivery' ? '#fff' : 'var(--text-dim, #8b97b0)',
                border: `1px solid ${rightTab === 'delivery' ? 'var(--accent, #3b82f6)' : 'var(--border, #243049)'}`,
                borderRadius: 5, cursor: 'pointer',
              }}
            >
              产物
            </button>
            <button
              onClick={() => setRightTab('detail')}
              style={{
                padding: '3px 8px', fontSize: 10,
                background: rightTab === 'detail' ? 'var(--accent, #3b82f6)' : 'transparent',
                color: rightTab === 'detail' ? '#fff' : 'var(--text-dim, #8b97b0)',
                border: `1px solid ${rightTab === 'detail' ? 'var(--accent, #3b82f6)' : 'var(--border, #243049)'}`,
                borderRadius: 5, cursor: 'pointer',
              }}
            >
              详情
            </button>
            <button
              className="dag-detail-drawer-close"
              onClick={() => setDrawerOpen(false)}
              title="关闭"
            >
              ✕
            </button>
          </div>
        </div>
        <div className="dag-detail-drawer-body">
          <RightSidebar
            graphData={graphData}
            selectedNode={selectedGraphNode}
            selectedHandoffs={selectedHandoffs}
            rightTab={rightTab}
            onTabChange={setRightTab}
            onRerunNode={handleRerunNode}
            workflowId={graphData?.workflow_id ?? null}
          />
        </div>
      </div>
    </div>
  );
}

// ===== 子组件：Actor 汇总列表 =====
function ActorSummaryTab({ graphData, loading, onSelectNode }: {
  graphData: CollaborationGraph | null;
  loading: boolean;
  onSelectNode: (id: string) => void;
}) {
  if (!graphData && loading) {
    return <div className="widget-empty-state" style={{ padding: 40 }}>加载 Actor 数据中...</div>;
  }
  if (!graphData || graphData.nodes.length === 0) {
    return <div className="widget-empty-state" style={{ padding: 40 }}>暂无 Actor 数据</div>;
  }

  const nodes = graphData.nodes;
  const totalTokens = nodes.reduce((s, n) => s + (n.token_usage || 0), 0);
  const totalDuration = nodes.reduce((s, n) => s + (n.duration_ms || 0), 0);
  const statusCount = nodes.reduce((acc, n) => {
    acc[n.status] = (acc[n.status] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);

  const statusColor = (s: string) => {
    if (s === 'completed') return '#10b981';
    if (s === 'running') return '#3b82f6';
    if (s === 'failed') return '#ef4444';
    if (s === 'skipped') return '#6b7280';
    return '#fbbf24';
  };

  return (
    <div style={{ flex: 1, overflow: 'auto', padding: 16 }}>
      {/* 汇总卡片 */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
        <div style={{
          flex: 1, padding: '10px 14px', borderRadius: 8,
          background: 'rgba(59,130,246,.08)', border: '1px solid var(--border, #243049)',
        }}>
          <div style={{ fontSize: 10, color: 'var(--text-dim, #8b97b0)', textTransform: 'uppercase' }}>Actor 总数</div>
          <div style={{ fontSize: 20, fontWeight: 600, color: 'var(--text, #e6ecf5)' }}>{nodes.length}</div>
        </div>
        <div style={{
          flex: 1, padding: '10px 14px', borderRadius: 8,
          background: 'rgba(16,185,129,.08)', border: '1px solid var(--border, #243049)',
        }}>
          <div style={{ fontSize: 10, color: 'var(--text-dim, #8b97b0)', textTransform: 'uppercase' }}>总 Tokens</div>
          <div style={{ fontSize: 20, fontWeight: 600, color: 'var(--text, #e6ecf5)' }}>{totalTokens.toLocaleString()}</div>
        </div>
        <div style={{
          flex: 1, padding: '10px 14px', borderRadius: 8,
          background: 'rgba(168,85,247,.08)', border: '1px solid var(--border, #243049)',
        }}>
          <div style={{ fontSize: 10, color: 'var(--text-dim, #8b97b0)', textTransform: 'uppercase' }}>总耗时</div>
          <div style={{ fontSize: 20, fontWeight: 600, color: 'var(--text, #e6ecf5)' }}>{(totalDuration / 1000).toFixed(1)}s</div>
        </div>
        <div style={{
          flex: 1, padding: '10px 14px', borderRadius: 8,
          background: 'rgba(251,191,36,.08)', border: '1px solid var(--border, #243049)',
        }}>
          <div style={{ fontSize: 10, color: 'var(--text-dim, #8b97b0)', textTransform: 'uppercase' }}>状态分布</div>
          <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--text, #e6ecf5)', display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 2 }}>
            {Object.entries(statusCount).map(([s, c]) => (
              <span key={s} style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: statusColor(s) }} />
                {s} {c}
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Actor 表格 */}
      <table style={{
        width: '100%', borderCollapse: 'collapse', fontSize: 12,
        background: 'var(--panel, #131c2e)', borderRadius: 8, overflow: 'hidden',
      }}>
        <thead>
          <tr style={{ background: 'var(--panel-2, #1a2440)', textAlign: 'left' }}>
            <th style={thStyle}>节点 ID</th>
            <th style={thStyle}>Agent</th>
            <th style={thStyle}>业务角色</th>
            <th style={thStyle}>Model</th>
            <th style={thStyle}>Harness</th>
            <th style={thStyle}>状态</th>
            <th style={thStyle}>耗时</th>
            <th style={thStyle}>Tokens</th>
            <th style={thStyle}>错误</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => (
            <tr
              key={n.node_id}
              onClick={() => onSelectNode(n.node_id)}
              style={{
                cursor: 'pointer', borderBottom: '1px solid var(--border, #243049)',
                transition: 'background .15s',
              }}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'rgba(59,130,246,.06)'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent'; }}
            >
              <td style={tdStyle}>{n.node_id}</td>
              <td style={tdStyle}>{n.agent_id}</td>
              <td style={tdStyle}>{n.business_role || '-'}</td>
              <td style={{ ...tdStyle, fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>{n.model || '-'}</td>
              <td style={{ ...tdStyle, fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>{n.harness || '-'}</td>
              <td style={tdStyle}>
                <span style={{
                  display: 'inline-flex', alignItems: 'center', gap: 4,
                  padding: '2px 8px', borderRadius: 4,
                  background: `${statusColor(n.status)}20`, color: statusColor(n.status),
                  fontSize: 11, fontWeight: 500,
                }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: statusColor(n.status) }} />
                  {n.status}
                </span>
              </td>
              <td style={{ ...tdStyle, fontFamily: 'ui-monospace, monospace' }}>
                {n.duration_ms != null ? `${(n.duration_ms / 1000).toFixed(1)}s` : '-'}
              </td>
              <td style={{ ...tdStyle, fontFamily: 'ui-monospace, monospace' }}>
                {n.token_usage != null ? n.token_usage.toLocaleString() : '-'}
              </td>
              <td style={{ ...tdStyle, color: '#ef4444', fontSize: 11, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {n.error || '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  padding: '8px 10px', fontSize: 10, textTransform: 'uppercase' as const,
  color: 'var(--text-dim, #8b97b0)', fontWeight: 600, letterSpacing: 0.3,
};
const tdStyle: React.CSSProperties = {
  padding: '8px 10px', fontSize: 12, color: 'var(--text, #e6ecf5)',
};

function LegendItem({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <span style={{ width: 10, height: 10, borderRadius: 3, background: color }} />
      {label}
    </span>
  );
}

function MainTab({ id, active, onClick, children }: {
  id: string; active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 14px', background: 'transparent', border: 0,
        color: active ? 'var(--accent, #3b82f6)' : 'var(--text-dim, #8b97b0)',
        fontSize: 12, cursor: 'pointer',
        borderBottom: `2px solid ${active ? 'var(--accent, #3b82f6)' : 'transparent'}`,
      }}
    >
      {children}
    </button>
  );
}

export default CollaborationCenterPage;

// ─── 静默未使用引用 ───
void SkillCatalog;
