/**
 * dag-visualization-demo — v99.5 P0.7 浏览器验收页面。
 *
 * 访问：http://localhost:5173/dag-visualization-demo.html
 *
 * 验收目标（CLAUDE.md 规则：可视化改造必须实际打开浏览器确认效果）：
 *   1. DagNodeSemantics.resolveDagNodeSemantic 11 类节点 → 8 shape 正确映射
 *   2. DagNodeShapeRegistry 8 种 shape 渲染正确（SVG path 看得见）
 *   3. 8 种 status 视觉叠加正确（边框色 + 动效）
 *   4. 5 种 metric 徽章触发正确
 *   5. DagLegend 列出 8 种 shape 图例 + 可折叠
 *   6. DeveloperDagView 渲染混合节点类型 DAG（layout + 边 SVG + legend）
 *
 * 此页面不连后端，直接调用 DagNodeSemantics / DagNodeShapeRegistry / DagLegend，
 * 验证前端组件链路。dag 真实业务事件由 DagEngine 通过 SSE 推送。
 */
import { StrictMode, useMemo, useState } from 'react';
import ReactDOM from 'react-dom/client';
import {
  resolveDagNodeSemantic,
  listDagLegendEntries,
  DAG_SHAPE_ORDER,
} from './components/collaboration/DagNodeSemantics';
import {
  DagNodeCard,
  type GraphNodeLike,
} from './components/collaboration/DagNodeShapeRegistry';
import { DagLegend } from './components/collaboration/DagLegend';
import { DeveloperDagView } from './components/collaboration/DeveloperDagView';

// Vite 5.4.21 dev server bug: __vite__updateStyle 对新 CSS 模块静默丢弃。
// 用 ?raw import 拿原始 CSS 字符串，手动创建 <style> 注入。
import dagV99Raw from './styles/dag-v99.css?raw';
if (typeof document !== 'undefined' && typeof dagV99Raw === 'string') {
  const style = document.createElement('style');
  style.setAttribute('data-source', 'dag-v99.css');
  style.textContent = dagV99Raw;
  document.head.appendChild(style);
}
import type {
  CollaborationGraph,
  GraphNode,
  GraphEdge,
  HandoffInfo,
  LaneInfo,
  TimelineEntry,
} from './lib/types';
import './styles.css';

// ── 场景 1：11 类节点类型矩阵 ──────────────────────────────────────────

const TYPE_MATRIX: Array<{
  label: string;
  node_type: string;
  gateway_kind?: string;
  terminal_kind?: string;
  description: string;
}> = [
  { label: 'agent', node_type: 'agent', description: 'LLM 推理节点' },
  { label: 'parallel_branch', node_type: 'parallel_branch', description: '并行分支' },
  { label: 'gateway (condition)', node_type: 'gateway', gateway_kind: 'condition', description: '条件判断' },
  { label: 'gateway (loop)', node_type: 'gateway', gateway_kind: 'loop', description: '循环网关' },
  { label: 'command', node_type: 'command', description: 'CLI 执行' },
  { label: 'await_command', node_type: 'await_command', description: '等待命令' },
  { label: 'while', node_type: 'while', description: '反馈循环' },
  { label: 'terminal (success)', node_type: 'terminal', terminal_kind: 'success', description: '终止（成功）' },
  { label: 'terminal (failure)', node_type: 'terminal', terminal_kind: 'failure', description: '终止（失败）' },
  { label: 'join', node_type: 'join', description: '多输入合并' },
  { label: 'quorum', node_type: 'quorum', description: '法定人数合并' },
  { label: 'foreach (预留)', node_type: 'foreach', description: '集合迭代（未来）' },
];

// ── 场景 2：8 种 status 视觉叠加 ──────────────────────────────────────

const STATUS_MATRIX: Array<{
  label: string;
  status: string;
}> = [
  { label: 'pending', status: 'pending' },
  { label: 'ready', status: 'ready' },
  { label: 'running', status: 'running' },
  { label: 'waiting_for_command', status: 'waiting_for_command' },
  { label: 'completed', status: 'completed' },
  { label: 'failed', status: 'failed' },
  { label: 'cancelled', status: 'cancelled' },
  { label: 'skipped', status: 'skipped' },
];

// ── 场景 3：5 种徽章触发 ─────────────────────────────────────────────

interface BadgeCase {
  name: string;
  data: {
    tokens_in?: number | null;
    tokens_out?: number | null;
    tool_calls?: number | null;
    tool_failures?: number | null;
    duration_ms?: number | null;
    error_type?: string | null;
  };
}
const BADGE_CASES: BadgeCase[] = [
  { name: 'tokens 触发', data: { tokens_in: 1200, tokens_out: 800 } },
  { name: 'tool_calls 触发', data: { tool_calls: 5 } },
  { name: 'tool_failures 触发', data: { tool_failures: 2 } },
  { name: 'duration 触发', data: { duration_ms: 12340 } },
  { name: 'error_type 触发', data: { error_type: 'rate_limit', tool_failures: 1 } },
  { name: '无徽章', data: {} },
];

// ── 场景 4：完整 DAG（DeveloperDagView 输入） ─────────────────────────

const SAMPLE_GRAPH: CollaborationGraph = {
  run_id: 'demo-run-001',
  workflow_id: 'demo-workflow',
  status: 'running',
  nodes: [
    { node_id: 'fetch', agent_id: 'fetcher', business_role: '数据采集员', display_name: 'fetch', harness: 'codex', model: 'gpt-4', status: 'completed', node_type: 'command', tool_calls: 3, duration_ms: 4500, token_usage: 240 },
    { node_id: 'decide', agent_id: 'router', business_role: '需求分析师', display_name: 'decide', harness: 'claude_code', model: 'claude-sonnet', status: 'completed', node_type: 'gateway', gateway_kind: 'condition', tool_calls: 0, duration_ms: 1200, token_usage: 380 },
    { node_id: 'fanout', agent_id: 'router', business_role: '需求分析师', display_name: 'fanout', harness: 'auto', model: '', status: 'running', node_type: 'parallel_branch', tool_calls: 0, duration_ms: 600, token_usage: 80 },
    { node_id: 'research', agent_id: 'researcher', business_role: '数据采集员', display_name: 'research', harness: 'claude_code', model: 'claude-sonnet', status: 'running', node_type: 'agent', tool_calls: 4, duration_ms: 23000, token_usage: 1820 },
    { node_id: 'analyze', agent_id: 'analyzer', business_role: '异常分析员', display_name: 'analyze', harness: 'codex', model: 'gpt-4', status: 'ready', node_type: 'agent', token_usage: 0 },
    { node_id: 'wait_review', agent_id: 'reviewer', business_role: 'Manager', display_name: 'wait_review', harness: 'auto', model: '', status: 'waiting_for_command', node_type: 'await_command' },
    { node_id: 'writeup', agent_id: 'writer', business_role: '报告撰写员', display_name: 'writeup', harness: 'claude_code', model: 'claude-opus', status: 'pending', node_type: 'agent' },
    { node_id: 'end_ok', agent_id: 'system', business_role: 'Manager', display_name: 'end_ok', harness: 'deterministic', model: '', status: 'pending', node_type: 'terminal', terminal_kind: 'success' },
    { node_id: 'end_fail', agent_id: 'system', business_role: 'Manager', display_name: 'end_fail', harness: 'deterministic', model: '', status: 'failed', node_type: 'terminal', terminal_kind: 'failure', error_type: 'rate_limit', tool_failures: 3 },
  ] as unknown as GraphNode[],
  edges: [
    { from: 'fetch', to: 'decide', port: 'data' },
    { from: 'decide', to: 'fanout', port: 'matched' },
    { from: 'decide', to: 'end_fail', port: 'fallback' },
    { from: 'fanout', to: 'research', port: 'research_branch' },
    { from: 'fanout', to: 'analyze', port: 'analyze_branch' },
    { from: 'fanout', to: 'wait_review', port: 'human_branch' },
    { from: 'research', to: 'writeup', port: 'report' },
    { from: 'analyze', to: 'writeup', port: 'metrics' },
    { from: 'wait_review', to: 'writeup', port: 'human_feedback' },
    { from: 'writeup', to: 'end_ok', port: 'final' },
  ] as GraphEdge[],
  handoffs: [] as HandoffInfo[],
  lanes: [] as LaneInfo[],
  timeline: [] as TimelineEntry[],
};

// ── 主页面 ────────────────────────────────────────────────────────────

function App() {
  const [selected, setSelected] = useState<string | null>(null);
  const legendEntries = useMemo(() => listDagLegendEntries(), []);
  const shapeOrder = useMemo(() => [...DAG_SHAPE_ORDER], []);

  return (
    <div style={{ padding: '24px', background: 'var(--color-bg-base)', minHeight: '100vh', color: 'var(--color-text-primary)' }}>
      <header>
        <h1 style={{ fontSize: 22, margin: 0 }}>DAG Visualization Demo · v99.5 P0.7</h1>
        <p style={{ color: 'var(--color-text-secondary)', fontSize: 12, marginTop: 6 }}>
          验收 DagNodeSemantics（11→8 shape 映射）+ DagNodeShapeRegistry（8 shape 渲染 + 8 status 叠加 + 5 badge）
          + DagLegend（可折叠图例）+ DeveloperDagView（layout + SVG 边）。
        </p>
        <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-primary-soft)', fontSize: 11 }}>
          http://localhost:5173/dag-visualization-demo.html
        </code>
      </header>

      {/* ── 1. 11 类节点类型矩阵 ── */}
      <section style={sectionStyle}>
        <h2 style={h2Style}>1. 11 类节点类型 → 8 shape 映射</h2>
        <p style={descStyle}>
          点击卡片可高亮。每行展示 node_type / gateway_kind / terminal_kind →
          shape / glyph / label / tone（resolveDagNodeSemantic 纯函数输出）。
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14, marginTop: 12 }}>
          {TYPE_MATRIX.map((row) => {
            const semantic = resolveDagNodeSemantic({
              node_type: row.node_type,
              gateway_kind: row.gateway_kind,
              terminal_kind: row.terminal_kind,
              status: 'ready',
            });
            // key 必须含 kind：gateway 包含 condition/loop 两条，terminal 包含 success/failure 两条
            const rowKey = `${row.node_type}-${row.gateway_kind ?? row.terminal_kind ?? 'default'}`;
            const card = (
              <DagNodeCard
                semantic={semantic}
                node_id={rowKey}
                display_name={row.description}
                agent_label={`type=${row.node_type}${row.gateway_kind ? ` / ${row.gateway_kind}` : ''}${row.terminal_kind ? ` / ${row.terminal_kind}` : ''}`}
                selected={selected === rowKey}
                onClick={() => setSelected(rowKey)}
                badges={{ tool_calls: 2, duration_ms: 1234, tokens_in: 600, tokens_out: 400 }}
              />
            );
            return (
              <div key={rowKey} style={cardWrap}>
                {card}
                <pre style={metaPre}>
                  {`shape=${semantic.shape}\nglyph=${JSON.stringify(semantic.glyph)}\nlabel=${semantic.label}\ntone=${semantic.tone}`}
                </pre>
              </div>
            );
          })}
        </div>
        <details style={{ marginTop: 14 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--color-text-secondary)' }}>
            shape 顺序（DAG_SHAPE_ORDER，单一来源）
          </summary>
          <pre style={metaPre}>{shapeOrder.join('\n')}</pre>
        </details>
      </section>

      {/* ── 2. 8 种 status 视觉叠加 ── */}
      <section style={sectionStyle}>
        <h2 style={h2Style}>2. 8 种 status 视觉叠加</h2>
        <p style={descStyle}>
          同一 shape (circle/agent) 在 8 种 status 下的边框色 + 动效对比。running/failed/waiting_for_command 有动画。
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14, marginTop: 12 }}>
          {STATUS_MATRIX.map((row) => {
            const semantic = resolveDagNodeSemantic({
              node_type: 'agent',
              status: row.status,
            });
            return (
              <div key={row.status} style={cardWrap}>
                <DagNodeCard
                  semantic={semantic}
                  node_id={row.status}
                  display_name={row.label}
                  agent_label={`status=${row.status}`}
                  badges={{ tool_calls: 3, duration_ms: 5000 }}
                />
              </div>
            );
          })}
        </div>
      </section>

      {/* ── 3. 5 种徽章触发 ── */}
      <section style={sectionStyle}>
        <h2 style={h2Style}>3. 5 种 metric 徽章触发</h2>
        <p style={descStyle}>
          DagNodeCard 接 DagNodeBadgeData；徽章按 presence + value &gt; 0 触发。
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14, marginTop: 12 }}>
          {BADGE_CASES.map((row) => {
            const semantic = resolveDagNodeSemantic({
              node_type: 'agent',
              status: 'completed',
            });
            return (
              <div key={row.name} style={cardWrap}>
                <DagNodeCard
                  semantic={semantic}
                  node_id={row.name}
                  display_name={row.name}
                  agent_label="badge test"
                  badges={row.data}
                />
              </div>
            );
          })}
        </div>
      </section>

      {/* ── 4. Legend ── */}
      <section style={sectionStyle}>
        <h2 style={h2Style}>4. DagLegend（图例 + 可折叠）</h2>
        <p style={descStyle}>点击右上「折叠/展开」按钮验证 localStorage 持久化（不依赖后端）。</p>
        <div style={{ marginTop: 12 }}>
          <DagLegend />
        </div>
        <details style={{ marginTop: 14 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--color-text-secondary)' }}>
            legend 条目（listDagLegendEntries，单一来源）
          </summary>
          <pre style={metaPre}>
            {legendEntries.map((e) => `${e.shape} | ${e.label} | ${e.concept}`).join('\n')}
          </pre>
        </details>
      </section>

      {/* ── 5. DeveloperDagView 完整渲染 ── */}
      <section style={{ ...sectionStyle, minHeight: 700 }}>
        <h2 style={h2Style}>5. DeveloperDagView（layout + SVG 边 + legend）</h2>
        <p style={descStyle}>
          9 节点混合类型 DAG；BFS 分层 + 边 SVG path（贝塞尔曲线）+ 右上 legend。点击节点验证选中态。
        </p>
        <div style={{ marginTop: 12, border: '1px solid var(--color-border-subtle)', borderRadius: 8, overflow: 'hidden', minHeight: 620 }}>
          <DeveloperDagView
            graphData={SAMPLE_GRAPH}
            selectedNodeId={selected}
            onSelectNode={setSelected}
          />
        </div>
      </section>

      <footer style={{ marginTop: 24, color: 'var(--color-text-tertiary)', fontSize: 11 }}>
        selected: <code>{selected ?? '(none)'}</code> · color-mix() requires modern browsers (Chrome 111+ / Safari 16.4+)
      </footer>
    </div>
  );
}

const sectionStyle: React.CSSProperties = {
  marginTop: 24,
  padding: 16,
  background: 'var(--color-bg-surface)',
  border: '1px solid var(--color-border-subtle)',
  borderRadius: 10,
};

const h2Style: React.CSSProperties = {
  fontSize: 15,
  margin: 0,
  color: 'var(--color-text-primary)',
};

const descStyle: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--color-text-secondary)',
  marginTop: 6,
};

const cardWrap: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
};

const metaPre: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  color: 'var(--color-text-secondary)',
  margin: 0,
  padding: 8,
  background: 'var(--color-bg-elevated)',
  borderRadius: 6,
  whiteSpace: 'pre-wrap',
};

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);