/**
 * A2UI 渲染验收页面。
 *
 * 访问方式：http://localhost:5173/a2ui-demo.html
 *
 * 展示 5 个核心场景：
 *   1. Text + Image（标准组件）
 *   2. AoGrid + AoMetric（AgentOps 扩展组件）
 *   3. AoTable（数据表，source path 引用 content 数据）
 *   4. 表单交互（Column + TextField + ChoicePicker + Button，点击触发 action）
 *   5. AoDag（DAG 图，source path 引用 nodes 数据）
 *
 * 全部通过 WidgetRenderer 走 case 'a2ui' 分支，验证：
 *   - normalizeWidgetProps 跳过不破坏 surface 结构
 *   - A2uiWidget wrapper 正确构造 IR node（含 content 数据模型）
 *   - A2uiRenderer + A2uiNode 渲染 34 个组件
 *   - onAction 回调绑定到 onWidgetInput
 */
import { StrictMode, useState } from 'react';
import ReactDOM from 'react-dom/client';
import { WidgetRenderer } from './components/WidgetRenderer';
import type { WidgetUpdate } from './lib/api';
import { AGENTOPS_A2UI_CATALOG_ID, type AgentopsA2uiSurfaceV1 } from './lib/a2ui/a2ui';
import './styles.css';
import './styles/a2ui.css';

// ── 测试 surface 数据 ──
// schema 关键约束：
//   - children 是 id 字符串数组（引用 components 顶层的子组件 id），不是嵌套对象
//   - source 是 {path: "/..."} 引用 node.content 数据，不是 {items: [...]} 内联
//   - 没有 Form 组件，用 Column 包裹表单元素
//   - Text 用 variant（caption/body）不是 weight
//   - Image 用 description 不是 alt，没有 width/height
//   - Button 的 child 是 identifier（引用另一个组件作为按钮内容）

const textImageSurface: AgentopsA2uiSurfaceV1 = {
  version: 'v1.0',
  catalogId: AGENTOPS_A2UI_CATALOG_ID,
  components: [
    { id: 'root', component: 'Column', children: ['t1', 't2', 't3'] } as never,
    { id: 't1', component: 'Text', text: 'A2UI 协议层验收', variant: 'caption' } as never,
    { id: 't2', component: 'Text', text: '本页面通过 WidgetRenderer case "a2ui" 渲染，验证 React 渲染器完整可用。', variant: 'body' } as never,
    { id: 't3', component: 'Image', url: 'https://placehold.co/600x200/1E293B/60A5FA?text=AgentOps+A2UI', description: '占位图' } as never,
  ],
};

const metricsSurface: AgentopsA2uiSurfaceV1 = {
  version: 'v1.0',
  catalogId: AGENTOPS_A2UI_CATALOG_ID,
  components: [
    {
      id: 'root',
      component: 'AoGrid',
      children: ['m1', 'm2', 'm3', 'm4', 'm5', 'm6'],
      columns: { default: 3, compact: 1 },
      gap: 'md',
      align: 'start',
    } as never,
    { id: 'm1', component: 'AoMetric', label: '总 token', value: '128420', unit: 'tok', tone: 'info' } as never,
    { id: 'm2', component: 'AoMetric', label: '成功率', value: '98.4', unit: '%', tone: 'positive' } as never,
    { id: 'm3', component: 'AoMetric', label: '失败 run', value: '3', tone: 'critical' } as never,
    { id: 'm4', component: 'AoMetric', label: '平均延迟', value: '4.2', unit: 's', tone: 'warning' } as never,
    { id: 'm5', component: 'AoMetric', label: '活跃 session', value: '17', tone: 'neutral' } as never,
    { id: 'm6', component: 'AoMetric', label: '今日 emit widget', value: '156', tone: 'info' } as never,
  ],
};

const tableSurface: AgentopsA2uiSurfaceV1 = {
  version: 'v1.0',
  catalogId: AGENTOPS_A2UI_CATALOG_ID,
  components: [
    {
      id: 'root',
      component: 'AoTable',
      source: { path: '/providers' },
      columns: [
        { id: 'provider', label: 'Provider', path: '/provider', format: 'text' },
        { id: 'model', label: 'Model', path: '/model', format: 'text' },
        { id: 'latency', label: '延迟 (ms)', path: '/latency', format: 'number' },
        { id: 'status', label: '状态', path: '/status', format: 'status' },
      ],
    } as never,
  ],
};

const tableContent = {
  providers: [
    { provider: 'minimax', model: 'MiniMax-M3', latency: 1200, status: 'ok' },
    { provider: 'deepseek', model: 'deepseek-v4-pro', latency: 850, status: 'ok' },
    { provider: 'vllm', model: 'qwen2.5-72b', latency: 320, status: 'ok' },
    { provider: 'openai', model: 'gpt-4o-mini', latency: 0, status: 'unconfigured' },
  ],
};

// 没有原生 Form 组件，用 Column 包裹 TextField + ChoicePicker + Button
// Button 的 child 是 identifier，引用一个 Text 组件作为按钮内容
const formSurface: AgentopsA2uiSurfaceV1 = {
  version: 'v1.0',
  catalogId: AGENTOPS_A2UI_CATALOG_ID,
  components: [
    { id: 'root', component: 'Column', children: ['form_title', 'f1', 'f2', 'f3', 'b1'] } as never,
    { id: 'form_title', component: 'Text', text: '触发日志巡检参数', variant: 'caption' } as never,
    { id: 'f1', component: 'TextField', label: '日志源 ID', value: 'seeyon', variant: 'shortText' } as never,
    { id: 'f2', component: 'TextField', label: '时间窗口（小时）', value: '24', variant: 'number' } as never,
    {
      id: 'f3',
      component: 'ChoicePicker',
      label: '严重级别',
      variant: 'multipleSelection',
      options: [
        { label: 'info', value: 'info' },
        { label: 'warning', value: 'warning' },
        { label: 'error', value: 'error' },
        { label: 'critical', value: 'critical' },
      ],
      value: ['warning', 'error'],
      displayStyle: 'chips',
    } as never,
    {
      id: 'b1',
      component: 'Button',
      child: 'b1_text',
      variant: 'primary',
      action: { event: { name: 'trigger_patrol' } },
    } as never,
    { id: 'b1_text', component: 'Text', text: '发起巡检', variant: 'body' } as never,
  ],
};

const dagSurface: AgentopsA2uiSurfaceV1 = {
  version: 'v1.0',
  catalogId: AGENTOPS_A2UI_CATALOG_ID,
  components: [
    {
      id: 'root',
      component: 'AoDag',
      source: { path: '/nodes' },
      itemIdPath: '/id',
      itemLabelPath: '/title',
      itemDetailPath: '/detail',
      itemStatusPath: '/status',
      itemDependsOnPath: '/depends_on',
    } as never,
  ],
};

const dagContent = {
  nodes: [
    { id: 'scan', title: 'scan', status: 'succeeded', detail: '扫描完成 1280 行', depends_on: [] },
    { id: 'analyze', title: 'analyze', status: 'succeeded', detail: '5 条 critical', depends_on: ['scan'] },
    { id: 'report', title: 'report', status: 'running', detail: '生成报告中', depends_on: ['analyze'] },
    { id: 'notify', title: 'notify', status: 'pending', detail: '等待 report', depends_on: ['report'] },
  ],
};

// ── 渲染 ──

function makeWidget(
  widget_id: string,
  surface: AgentopsA2uiSurfaceV1,
  content?: Record<string, unknown>,
): WidgetUpdate {
  return {
    run_id: 'demo',
    widget_id,
    type: 'a2ui',
    props: { surface, content: content ?? {}, node_id: widget_id, title: 'A2UI Surface' },
  };
}

function App() {
  const [actions, setActions] = useState<string[]>([]);

  return (
    <div style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <header>
        <h1 style={{ fontSize: '24px', color: 'var(--color-text-primary)' }}>A2UI 渲染验收</h1>
        <p style={{ color: 'var(--color-text-secondary)', marginTop: '6px' }}>
          访问路径：<code style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-primary-soft)' }}>http://localhost:5173/a2ui-demo.html</code>
          {' · '}
          通过 <code style={{ fontFamily: 'var(--font-mono)' }}>WidgetRenderer case 'a2ui'</code> 渲染 5 类场景
        </p>
        {actions.length > 0 && (
          <div style={{ marginTop: '8px', padding: '6px 10px', background: 'var(--state-success-tint)', color: 'var(--state-success)', borderRadius: '6px', fontSize: '12px' }}>
            ✓ 收到 action 回调：<code>{actions[actions.length - 1]}</code>（累计 {actions.length} 次）
          </div>
        )}
      </header>

      <section>
        <h2 style={{ fontSize: '16px', color: 'var(--color-text-primary)', marginBottom: '12px' }}>1. Text + Image（标准组件）</h2>
        <div style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-subtle)', borderRadius: '8px', padding: '16px' }}>
          <WidgetRenderer
            widget={makeWidget('text_img', textImageSurface)}
            onWidgetInput={(_id, input) => setActions(prev => [...prev, JSON.stringify(input)])}
          />
        </div>
      </section>

      <section>
        <h2 style={{ fontSize: '16px', color: 'var(--color-text-primary)', marginBottom: '12px' }}>2. AoGrid + AoMetric（扩展组件 · 3 列网格）</h2>
        <div style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-subtle)', borderRadius: '8px', padding: '16px' }}>
          <WidgetRenderer widget={makeWidget('metrics', metricsSurface)} />
        </div>
      </section>

      <section>
        <h2 style={{ fontSize: '16px', color: 'var(--color-text-primary)', marginBottom: '12px' }}>3. AoTable（数据表 · source path 引用 content）</h2>
        <div style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-subtle)', borderRadius: '8px', padding: '16px' }}>
          <WidgetRenderer widget={makeWidget('table', tableSurface, tableContent)} />
        </div>
      </section>

      <section>
        <h2 style={{ fontSize: '16px', color: 'var(--color-text-primary)', marginBottom: '12px' }}>4. 表单交互（Column + TextField + ChoicePicker + Button · 点击触发 action）</h2>
        <div style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-subtle)', borderRadius: '8px', padding: '16px' }}>
          <WidgetRenderer
            widget={makeWidget('form', formSurface)}
            onWidgetInput={(_id, input) => setActions(prev => [...prev, JSON.stringify(input)])}
          />
        </div>
      </section>

      <section>
        <h2 style={{ fontSize: '16px', color: 'var(--color-text-primary)', marginBottom: '12px' }}>5. AoDag（DAG 图 · source path 引用 nodes · 状态着色 + 依赖箭头）</h2>
        <div style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-subtle)', borderRadius: '8px', padding: '16px' }}>
          <WidgetRenderer widget={makeWidget('dag', dagSurface, dagContent)} />
        </div>
      </section>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
