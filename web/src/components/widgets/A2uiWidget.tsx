/**
 * A2uiWidget：把 widget.props.surface 适配为 A2uiRenderer 期望的 IR node 结构。
 *
 * widget.props 字段：
 *   - surface: A2uiSurfaceV1（必填，包含 components 数组）
 *   - content?: 数据模型（默认 {}）
 *   - actions?: GenerativeUiActionV1[]
 *   - node_id?: 节点 ID（默认 widget_id）
 *   - kind?: 语义类型（默认 com.agentops.core/a2ui）
 *   - title?: fallback 标题（默认 'A2UI Surface'）
 *
 * 注：widget.props 直接传给本组件，**不经 normalizeWidgetProps 处理**
 * （A2UI surface 是嵌套组件树，normalize 会破坏结构），
 * 由 WidgetRenderer 在 type==='a2ui' 时跳过 normalize。
 */
import { useMemo } from 'react';
import { A2uiRenderer } from '../a2ui/A2uiRenderer';
import type {
  AgentopsA2uiSurfaceV1,
} from '../../lib/a2ui/a2ui.js';
import type {
  GenerativeUiActionV1,
  GenerativeUiCompositionItemV1,
  GenerativeUiFallbackV1,
  GenerativeUiImportance,
  GenerativeUiNodeV1,
  GenerativeUiPluginRef,
  GenerativeUiStatusV1,
  GenerativeUiStoredNodeV1,
  GenerativeUiSurface,
  GenerativeUiSurfaceContextV1,
} from '../../lib/a2ui/types.js';

export interface A2uiWidgetProps {
  surface?: AgentopsA2uiSurfaceV1;
  content?: Record<string, unknown>;
  actions?: GenerativeUiActionV1[];
  node_id?: string;
  kind?: string;
  kind_version?: number;
  title?: string;
  summary?: string;
  status?: GenerativeUiStatusV1;
  widget_id?: string;
  onAction?: (name: string) => void;
}

const DEFAULT_OWNER: GenerativeUiPluginRef = {
  id: 'com.agentops.core',
  version: '1.0.0',
};

const DEFAULT_PLACEMENT: GenerativeUiCompositionItemV1 = {
  node_id: '',
  node_revision: 1,
  surface: 'result' as GenerativeUiSurface,
  variant: 'detail',
  rank: 1,
  placement: 'primary',
  pinned: false,
  visibility: 'visible',
};

const DEFAULT_CONTEXT: GenerativeUiSurfaceContextV1 = {
  device: 'desktop',
  input: 'mouse',
  viewport: 'wide',
  attention: 'focused',
};

export function A2uiWidget(props: A2uiWidgetProps) {
  const {
    surface,
    content,
    actions,
    node_id,
    kind = 'com.agentops.core/a2ui',
    kind_version = 1,
    title = 'A2UI Surface',
    summary,
    status,
    widget_id,
    onAction,
  } = props;

  // 调试日志：诊断 surface 数据完整性
  console.log('[A2uiWidget] surface=', surface, 'content=', content, 'widget_id=', widget_id,
    'components_count=', surface && typeof surface === 'object' && Array.isArray(surface.components) ? surface.components.length : 'N/A');

  const nodeId = node_id || widget_id || `a2ui_${Date.now()}`;

  // surface 有效性标记（所有 hooks 必须在 early return 之前调用，否则违反 Rules of Hooks）
  const surfaceValid = !!(surface && typeof surface === 'object' && Array.isArray(surface.components));

  // 构造 IR node（GenerativeUiStoredNodeV1 = GenerativeUiNodeV1 + revision + updated_at）
  // 自动扫描 surface 收集所有 Button.action.event.name 合成 node.actions 声明，
  // 避免 schema 校验失败「event does not map to a node action」
  // （A2UI 规范要求 Button 引用的 action event.name 必须在 node.actions 数组中先声明）
  const node = useMemo<GenerativeUiStoredNodeV1>(() => {
    const fallback: GenerativeUiFallbackV1 = {
      title,
      summary,
      items: [],
    };
    // 扫描 surface 中所有 Button 组件的 action.event.name（surface 可能为 null，需容错）
    const collectedActions: GenerativeUiActionV1[] = [];
    const seenNames = new Set<string>();
    for (const comp of surface?.components ?? []) {
      const c = comp as unknown as Record<string, unknown>;
      if (c.component === 'Button' && c.action && typeof c.action === 'object') {
        const action = c.action as { event?: { name?: string } };
        const name = action.event?.name;
        if (name && !seenNames.has(name)) {
          seenNames.add(name);
          collectedActions.push({
            id: name,
            label: name,
            intent: name,
            style: 'primary',
          });
        }
      }
    }
    // 合并外部传入的 actions（覆盖同名 collected）
    const externalActions = actions ?? [];
    const externalNames = new Set(externalActions.map(a => a.id));
    const mergedActions = [
      ...externalActions,
      ...collectedActions.filter(a => !externalNames.has(a.id)),
    ];
    // 容错：历史 session 的 data_model 中 _inline_* 值可能是 dict（如 {"item":[...]} 或嵌套）而非数组
    // 前端 source path 要求解析为数组，需递归提取内部数组
    const extractArrayDeep = (value: unknown): unknown[] | undefined => {
      if (Array.isArray(value)) return value;
      if (value && typeof value === 'object') {
        for (const v of Object.values(value as Record<string, unknown>)) {
          const found = extractArrayDeep(v);
          if (found) return found;
        }
      }
      return undefined;
    };
    const sanitizedContent: Record<string, unknown> = {};
    if (content && typeof content === 'object') {
      for (const [k, v] of Object.entries(content)) {
        if (k.startsWith('_inline_') && v && typeof v === 'object' && !Array.isArray(v)) {
          const extracted = extractArrayDeep(v);
          sanitizedContent[k] = extracted ?? v;
        } else {
          sanitizedContent[k] = v;
        }
      }
    }
    const base: GenerativeUiNodeV1 = {
      ir_version: 1,
      id: nodeId,
      kind,
      kind_version,
      owner: DEFAULT_OWNER,
      surface: 'result',
      importance: 'primary' as GenerativeUiImportance,
      content: sanitizedContent,
      a2ui: surface,
      fallback,
      ...(mergedActions.length > 0 ? { actions: mergedActions } : {}),
      ...(status ? { status } : {}),
    };
    return {
      ...base,
      revision: 1,
      updated_at: new Date().toISOString(),
    };
  }, [nodeId, kind, kind_version, content, surface, actions, status, title, summary]);

  // placement 需要 node_id 同步
  const placement = useMemo<GenerativeUiCompositionItemV1>(
    () => ({ ...DEFAULT_PLACEMENT, node_id: nodeId }),
    [nodeId],
  );

  // 校验 surface 必填（所有 hooks 已在上文调用，此处 early return 安全）
  if (!surfaceValid) {
    return (
      <article className="widget-card">
        <div className="widget-card-header">
          <span className="widget-card-title">A2UI Surface 缺失或无效</span>
        </div>
        <pre className="font-mono" style={{ fontSize: '11px', color: 'var(--color-text-tertiary)', overflow: 'auto' }}>
          {JSON.stringify({ has_surface: !!surface, widget_id }, null, 2)}
        </pre>
      </article>
    );
  }

  return (
    <A2uiRenderer
      node={node}
      placement={placement}
      context={DEFAULT_CONTEXT}
      expanded={false}
      locale="zh-CN"
      onRequestAction={onAction ?? (() => undefined)}
    />
  );
}

export default A2uiWidget;
