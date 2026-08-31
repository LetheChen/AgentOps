import { useEffect, useState, useRef } from 'react';
import { apiClient } from '../../lib/api';
import type { CollaborationGraph, GraphNode, HandoffInfo } from '../../lib/types';
import { renderMarkdown, splitThinkBlocks } from '../../lib/markdown';
import { ThinkBlock } from '../task/TaskCommentThread';

interface RightSidebarProps {
  graphData: CollaborationGraph | null;
  selectedNode?: GraphNode;
  selectedHandoffs: HandoffInfo[];
  rightTab: 'delivery' | 'detail';
  onTabChange: (tab: 'delivery' | 'detail') => void;
  /** 重试回调：onlyNode=true=仅重试该节点（保留下游），false=重跑下游链 */
  onRerunNode: (nodeId: string, onlyNode: boolean) => void;
  /** 当前 run 的 workflow_id（resume 接口必填）；null 表示非 workflow run（对话 session） */
  workflowId: string | null;
}

/**
 * RightSidebar - 右侧栏
 *
 * 原型 swimlane-v2.html 右侧栏：产物瀑布 + 选中节点详情 + 仅重跑/重跑下游
 * 设计：固定双 tab 切换（产物瀑布 / 节点详情）
 */
export function RightSidebar({
  graphData, selectedNode, selectedHandoffs, rightTab, onTabChange,
  onRerunNode, workflowId,
}: RightSidebarProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {rightTab === 'delivery' ? (
        <DeliveryList graphData={graphData} />
      ) : (
        <NodeDetailPanel
          node={selectedNode}
          handoffs={selectedHandoffs}
          graphData={graphData}
          workflowId={workflowId}
          onRerunNode={onRerunNode}
        />
      )}
    </div>
  );
}

function RightTabBtn({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        flex: 1, padding: '6px 10px', fontSize: 11,
        background: active ? 'var(--accent, #3b82f6)' : 'transparent',
        color: active ? '#fff' : 'var(--text-dim, #8b97b0)',
        border: `1px solid ${active ? 'var(--accent, #3b82f6)' : 'var(--border, #243049)'}`,
        borderRadius: 6, cursor: 'pointer',
      }}
    >
      {children}
    </button>
  );
}

// ─── 产物瀑布 ───
function DeliveryList({ graphData }: { graphData: CollaborationGraph | null }) {
  if (!graphData || graphData.nodes.length === 0) {
    return (
      <div style={{ fontSize: 12, color: '#8b97b0', padding: '8px 0' }}>
        暂无产物。运行工作流后此处会显示各节点产出。
      </div>
    );
  }
  const nodes = graphData.nodes;
  const completedNodes = nodes.filter((n) => n.status === 'completed' || n.status === 'running');

  return (
    <>
      <h3 style={{
        fontSize: 11, textTransform: 'uppercase', color: '#8b97b0',
        margin: '8px 0 12px', letterSpacing: 0.5, fontWeight: 600,
      }}>
        📦 产物瀑布
      </h3>
      {completedNodes.map((n) => (
        <div
          key={n.node_id}
          style={{
            background: n.status === 'running' ? 'rgba(59,130,246,.06)' : 'rgba(255,255,255,.03)',
            border: '1px solid var(--border, #243049)',
            borderColor: n.status === 'running' ? '#3b82f6' : 'var(--border, #243049)',
            borderRadius: 6, padding: 10, marginBottom: 8, fontSize: 12,
          }}
        >
          <div style={{
            fontSize: 10, color: '#8b97b0', marginBottom: 6,
            display: 'flex', justifyContent: 'space-between',
          }}>
            <span>{n.business_role || n.node_id}</span>
            <span>
              {n.status === 'running'
                ? `⟳ ${n.duration_ms != null ? `${(n.duration_ms / 1000).toFixed(1)}s` : '运行中'}`
                : n.duration_ms != null ? `${(n.duration_ms / 1000).toFixed(1)}s` : '?'}
            </span>
          </div>
          <span style={{
            display: 'inline-block',
            background: 'rgba(59,130,246,.15)', color: 'var(--accent, #3b82f6)',
            padding: '2px 6px', borderRadius: 3, fontSize: 10, marginRight: 4,
          }}>
            {n.harness?.toUpperCase() || 'WIDGET'}
          </span>
          {n.display_name || n.node_id}
          {n.token_usage != null && (
            <span style={{ float: 'right', fontSize: 10, color: '#8b97b0' }}>
              🪙 {n.token_usage > 1000 ? `${(n.token_usage / 1000).toFixed(1)}k` : n.token_usage}
            </span>
          )}
        </div>
      ))}
      <div style={{ height: 1, background: 'var(--border, #243049)', margin: '16px 0 12px' }} />
    </>
  );
}

// ─── 节点详情 + 活动流 ───

interface NodeEvent {
  sequence: number;
  event_type: string;
  node_id: string | null;
  payload: string | null;
  occurred_at: string | null;
}

interface NodeDetail {
  run_id: string;
  node_id: string;
  events: NodeEvent[];
  raw_events: Array<Record<string, unknown>>;
  handoffs: Array<Record<string, unknown>>;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  input_payload: Record<string, unknown> | null;
  output_payload: Record<string, unknown> | null;
  error: string | null;
}

function NodeDetailPanel({ node, handoffs, graphData, workflowId, onRerunNode }: {
  node?: GraphNode;
  handoffs: HandoffInfo[];
  graphData: CollaborationGraph | null;
  workflowId: string | null;
  onRerunNode: (nodeId: string, onlyNode: boolean) => void;
}) {
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [loading, setLoading] = useState(false);
  // 节点重试的局部 loading：仅在该节点点击按钮时变 true，提示用户正在提交
  const [rerunPending, setRerunPending] = useState<'only' | 'downstream' | null>(null);
  const feedEndRef = useRef<HTMLDivElement>(null);

  // 选中节点变化时拉取 detail；running 节点每 3s 轮询
  useEffect(() => {
    if (!node || !graphData) { setDetail(null); return; }
    let cancelled = false;
    const fetchDetail = async () => {
      setLoading(true);
      try {
        const d = await apiClient.auditGetNodeDetail(graphData.run_id, node.node_id);
        if (!cancelled) setDetail(d as unknown as NodeDetail);
      } catch { /* ignore */ }
      finally { if (!cancelled) setLoading(false); }
    };
    fetchDetail();
    // running 状态轮询
    if (node.status === 'running') {
      const id = setInterval(fetchDetail, 3000);
      return () => { cancelled = true; clearInterval(id); };
    }
    return () => { cancelled = true; };
  }, [node?.node_id, node?.status, graphData?.run_id]);

  // 新事件到达时自动滚到底部
  useEffect(() => {
    feedEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [detail?.events.length]);

  if (!node) {
    return (
      <div style={{ padding: '20px 8px', color: '#8b97b0', fontSize: 12, textAlign: 'center' }}>
        👆 点击画布上的节点查看详情
      </div>
    );
  }

  const statusColor =
    node.status === 'running' ? '#3b82f6' :
    node.status === 'completed' ? '#10b981' :
    node.status === 'failed' ? '#ef4444' : '#fbbf24';

  // 从 events 提取活动流
  const activityItems = extractActivity(detail?.events || []);

  // 统计 tool calls
  const toolCallCount = activityItems.filter(a => a.kind === 'tool').length;
  const textChunkCount = activityItems.filter(a => a.kind === 'text').length;

  return (
    <>
      <h3 style={{
        fontSize: 11, textTransform: 'uppercase', color: '#8b97b0',
        margin: '8px 0 12px', letterSpacing: 0.5, fontWeight: 600,
      }}>
        🔍 选中节点详情
      </h3>

      <DetailRow label="节点 ID" value={node.node_id} />
      <DetailRow label="业务角色" value={node.business_role || '?'} highlight />
      <DetailRow label="Harness" value={node.harness || 'auto'} />
      <DetailRow label="Model" value={shortModel(node.model)} />
      <DetailRow label="状态" value={
        <span style={{ color: statusColor, fontWeight: 600 }}>
          {node.status === 'running' ? '⟳ running' :
           node.status === 'completed' ? '✓ completed' :
           node.status === 'failed' ? '✗ failed' :
           node.status === 'skipped' ? '⊘ skipped' : '○ pending'}
        </span>
      } />
      {node.duration_ms != null && (
        <DetailRow label="耗时" value={`${(node.duration_ms / 1000).toFixed(2)}s`} />
      )}
      {node.token_usage != null && node.token_usage > 0 && (
        <DetailRow label="Token" value={node.token_usage.toLocaleString()} />
      )}
      {node.error && <DetailRow label="错误" value={node.error} error />}

      {/* ─── 节点重试操作 ─── */}
      {/* failed / completed 节点都允许重试；非 workflow run 或 running/pending 时不显示 */}
      {(node.status === 'failed' || node.status === 'completed') && workflowId && (
        <div style={{
          display: 'flex', gap: 6, marginTop: 10, marginBottom: 4,
          paddingTop: 8, borderTop: '1px dashed var(--border, #243049)',
        }}>
          <RerunButton
            label="仅重试该节点"
            icon="🔄"
            tone="blue"
            disabled={rerunPending !== null}
            loading={rerunPending === 'only'}
            onClick={() => {
              setRerunPending('only');
              onRerunNode(node.node_id, true);
              // 8s 后兜底清掉 loading（成功路径会通过轮询拿到新状态覆盖；超时则恢复）
              setTimeout(() => setRerunPending((cur) => (cur === 'only' ? null : cur)), 8000);
            }}
            title="仅清除当前节点输出文件后重跑；下游节点已有文件会被自动跳过"
          />
          <RerunButton
            label="重跑下游"
            icon="�"
            tone="amber"
            disabled={rerunPending !== null}
            loading={rerunPending === 'downstream'}
            onClick={() => {
              setRerunPending('downstream');
              onRerunNode(node.node_id, false);
              setTimeout(() => setRerunPending((cur) => (cur === 'downstream' ? null : cur)), 8000);
            }}
            title="清除当前节点及所有下游节点的输出文件，整条下游链强制重跑"
          />
        </div>
      )}

      {/* ─── 活动流 ─── */}
      {activityItems.length > 0 && (
        <>
          <div style={{ height: 1, background: 'var(--border, #243049)', margin: '12px 0 8px' }} />
          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            marginBottom: 8,
          }}>
            <h3 style={{
              fontSize: 11, textTransform: 'uppercase', color: '#8b97b0',
              margin: 0, letterSpacing: 0.5, fontWeight: 600,
            }}>
              📋 执行活动流
            </h3>
            <span style={{ fontSize: 10, color: '#64748b' }}>
              {toolCallCount > 0 && `${toolCallCount} 工具调用 · `}
              {textChunkCount > 0 && `${textChunkCount} 条输出`}
              {node.status === 'running' && ' · ⟳ 实时'}
            </span>
          </div>

          <div style={{
            maxHeight: 400, overflowY: 'auto',
            background: 'rgba(0,0,0,.15)', borderRadius: 6,
            padding: 8, fontSize: 11, lineHeight: 1.5,
            border: '1px solid var(--border, #243049)',
          }}>
            {activityItems.map((item, idx) => (
              <ActivityItem key={idx} item={item} />
            ))}
            {node.status === 'running' && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: 6,
                color: '#3b82f6', fontSize: 10, marginTop: 4,
              }}>
                <span style={{
                  width: 6, height: 6, borderRadius: '50%', background: '#3b82f6',
                  animation: 'pulse 1.5s infinite',
                }} />
                等待更多输出...
              </div>
            )}
            <div ref={feedEndRef} />
          </div>
        </>
      )}

      {/* Handoff 列表 */}
      {handoffs.length > 0 && (
        <>
          <div style={{ height: 1, background: 'var(--border, #243049)', margin: '12px 0 8px' }} />
          <h3 style={{
            fontSize: 11, textTransform: 'uppercase', color: '#8b97b0',
            margin: '0 0 8px', letterSpacing: 0.5, fontWeight: 600,
          }}>
            💬 Handoff（{handoffs.length} 个下游交接）
          </h3>
          {handoffs.map((h) => (
            <div
              key={h.id}
              style={{
                background: 'linear-gradient(135deg, rgba(99,102,241,.10), rgba(168,85,247,.10))',
                border: '1px solid rgba(168,85,247,.3)',
                borderRadius: 6, padding: '8px 10px', marginBottom: 6,
                fontSize: 11,
              }}
            >
              <div style={{ color: '#c4b5fd', fontSize: 10, marginBottom: 3 }}>
                {h.from_role} {'->'} {h.to_role}
              </div>
              <div style={{ color: '#e6ecf5', lineHeight: 1.4 }}>{h.summary || h.port}</div>
              <div style={{
                fontSize: 9, color: '#a5b4fc', marginTop: 3,
                fontFamily: '"SF Mono", Consolas, monospace',
                display: 'flex', justifyContent: 'space-between',
              }}>
                <span>port: {h.port}</span>
                <span>{h.payload_size}B</span>
              </div>
            </div>
          ))}
        </>
      )}

      {loading && !detail && (
        <div style={{ fontSize: 11, color: '#8b97b0', padding: 8 }}>加载中...</div>
      )}
    </>
  );
}

// ─── 活动流解析 ───

interface ActivityEntry {
  kind: 'started' | 'text' | 'tool' | 'progress' | 'completed' | 'failed' | 'handoff' | 'widget';
  sequence: number;
  time: string;
  text: string;
  meta?: string;
}

function extractActivity(events: NodeEvent[]): ActivityEntry[] {
  const items: ActivityEntry[] = [];
  // 合并连续的 node.progress agent_text 片段
  let textBuffer = '';
  let textSeq = 0;
  let textTime = '';

  const flushText = () => {
    if (textBuffer.trim()) {
      items.push({
        kind: 'text',
        sequence: textSeq,
        time: textTime,
        text: textBuffer.trim(),
      });
    }
    textBuffer = '';
  };

  for (const e of events) {
    const payload = e.payload ? safeParse(e.payload) : {};
    const time = e.occurred_at ? new Date(e.occurred_at).toLocaleTimeString('zh-CN', { hour12: false }) : '';

    switch (e.event_type) {
      case 'node.started':
        flushText();
        items.push({ kind: 'started', sequence: e.sequence, time, text: '节点启动' });
        break;
      case 'node.progress': {
        const agentText = payload.agent_text as string | undefined;
        if (agentText) {
          if (!textBuffer) { textSeq = e.sequence; textTime = time; }
          textBuffer += agentText;
        }
        // 也检查是否有 tool 相关信息
        const toolName = payload.tool_name as string | undefined;
        if (toolName) {
          flushText();
          items.push({
            kind: 'tool', sequence: e.sequence, time,
            text: `调用工具: ${toolName}`,
            meta: payload.tool_input ? truncateStr(JSON.stringify(payload.tool_input), 120) : undefined,
          });
        }
        break;
      }
      case 'node.handoff':
        flushText();
        items.push({
          kind: 'handoff', sequence: e.sequence, time,
          text: `交接: ${payload.from || '?'} -> ${payload.to || '?'}`,
          meta: `port=${payload.port || ''} ${payload.summary ? '· ' + payload.summary : ''}`,
        });
        break;
      case 'widget.update':
        flushText();
        items.push({
          kind: 'widget', sequence: e.sequence, time,
          text: `组件更新: ${payload.widget_id || payload.widget_type || ''}`,
          meta: payload.title as string | undefined,
        });
        break;
      case 'node.completed':
        flushText();
        items.push({
          kind: 'completed', sequence: e.sequence, time,
          text: '节点完成',
          meta: typeof payload.duration_ms === 'number' ? `耗时 ${(payload.duration_ms / 1000).toFixed(1)}s` : undefined,
        });
        break;
      case 'node.failed':
        flushText();
        items.push({
          kind: 'failed', sequence: e.sequence, time,
          text: '节点失败',
          meta: payload.error_type as string | undefined,
        });
        break;
      default:
        // 其他事件类型不显示
        break;
    }
  }
  flushText();
  return items;
}

function ActivityItem({ item }: { item: ActivityEntry }) {
  const config: Record<ActivityEntry['kind'], { icon: string; color: string }> = {
    started:   { icon: '🟢', color: '#10b981' },
    text:      { icon: '💬', color: '#8b97b0' },
    tool:      { icon: '🔧', color: '#a5b4fc' },
    progress:  { icon: '📊', color: '#3b82f6' },
    completed: { icon: '✅', color: '#10b981' },
    failed:    { icon: '❌', color: '#ef4444' },
    handoff:   { icon: '💬', color: '#c4b5fd' },
    widget:    { icon: '📦', color: '#06b6d4' },
  };
  const c = config[item.kind];

  return (
    <div style={{
      display: 'flex', gap: 6, marginBottom: 4,
      padding: '3px 0', borderBottom: '1px solid rgba(255,255,255,.03)',
    }}>
      <span style={{ fontSize: 9, color: '#475569', flexShrink: 0, fontFamily: 'ui-monospace, monospace', width: 56 }}>
        {item.time}
      </span>
      <span style={{ flexShrink: 0 }}>{c.icon}</span>
      <div style={{ minWidth: 0, flex: 1 }}>
        {/* agent 输出（text）：markdown 渲染 + 多对 `` 全部折叠；其余 kind 为本地短文案 */}
        {item.kind === 'text' ? (() => {
          const { thinks, rest } = splitThinkBlocks(item.text);
          return (
            <>
              {thinks.map((t, i) => <ThinkBlock key={i} content={t} />)}
              {rest && (
                <div
                  className="md-content"
                  style={{ color: '#b8c2d6', fontSize: 11, lineHeight: 1.4, wordBreak: 'break-word' }}
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(rest) }}
                />
              )}
            </>
          );
        })() : (
          <div style={{
            color: c.color,
            fontSize: 11, lineHeight: 1.4,
            wordBreak: 'break-word',
          }}>
            {item.text}
          </div>
        )}
        {item.meta && (
          <div style={{
            fontSize: 9, color: '#64748b', marginTop: 2,
            fontFamily: 'ui-monospace, monospace',
          }}>
            {item.meta}
          </div>
        )}
      </div>
    </div>
  );
}

function DetailRow({ label, value, highlight, error }: {
  label: string;
  value: React.ReactNode;
  highlight?: boolean;
  error?: boolean;
}) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between',
      padding: '5px 0', fontSize: 12, borderBottom: '1px solid var(--border, #243049)',
    }}>
      <span style={{ color: '#8b97b0' }}>{label}</span>
      <span style={{
        fontFamily: '"SF Mono", Consolas, monospace',
        fontSize: 11,
        color: error ? '#ef4444' : highlight ? '#e6ecf5' : '#e6ecf5',
        fontWeight: highlight ? 600 : 400,
      }}>
        {value}
      </span>
    </div>
  );
}

function shortModel(m: string): string {
  if (!m) return '?';
  const parts = m.split('/');
  return parts[parts.length - 1] || m;
}

/**
 * RerunButton — 抽屉里的「节点重试」操作按钮。
 * tone=blue：仅重试该节点（保留下游）；tone=amber：重跑下游链。
 * loading=true 时显示 ⟳ 旋转文字，禁用点击。
 */
function RerunButton({ label, icon, tone, disabled, loading, onClick, title }: {
  label: string;
  icon: string;
  tone: 'blue' | 'amber';
  disabled: boolean;
  loading: boolean;
  onClick: () => void;
  title?: string;
}) {
  const palette = tone === 'blue'
    ? { bg: 'rgba(59,130,246,.12)', border: 'rgba(59,130,246,.5)', color: '#3b82f6', hover: 'rgba(59,130,246,.22)' }
    : { bg: 'rgba(251,191,36,.10)', border: 'rgba(251,191,36,.5)', color: '#fbbf24', hover: 'rgba(251,191,36,.20)' };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        flex: 1, padding: '7px 10px', fontSize: 11,
        background: palette.bg, color: palette.color,
        border: `1px solid ${palette.border}`,
        borderRadius: 6, cursor: disabled ? 'not-allowed' : 'pointer',
        fontWeight: 500, opacity: disabled ? 0.5 : 1,
        display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 5,
        transition: 'background .15s',
      }}
      onMouseEnter={(e) => {
        if (disabled) return;
        (e.currentTarget as HTMLElement).style.background = palette.hover;
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLElement).style.background = palette.bg;
      }}
    >
      <span style={{ display: 'inline-block', minWidth: 12, textAlign: 'center' }}>
        {loading ? '⟳' : icon}
      </span>
      <span>{loading ? '提交中...' : label}</span>
    </button>
  );
}

function safeParse(s: string): Record<string, unknown> {
  try { return JSON.parse(s); } catch { return {}; }
}

function truncateStr(s: string, max: number): string {
  return s.length <= max ? s : s.slice(0, max - 1) + '…';
}
