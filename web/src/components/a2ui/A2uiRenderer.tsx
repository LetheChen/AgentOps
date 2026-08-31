/**
 * A2UI 渲染器入口（Vue → React 移植）。
 *
 * 职责：接收 GenerativeUiStoredNodeV1（IR 节点）+ surface（A2uiSurfaceV1），
 * 构造 A2uiRuntime 并调用 A2uiNode 渲染根组件。
 *
 * Vue → React 转换要点：
 * - computed → useMemo（依赖 node.id+revision）
 * - watch(node.id+revision) → useEffect（重置 dataModel）
 * - ref<unknown>(structuredClone(node.content)) → useState（mutable container + forceUpdate）
 * - vue-i18n useI18n() → props.locale（外部传入，AgentOps 无 vue-i18n 依赖）
 * - emit('request-action' / 'open-preview' / 'surface-actions') → props 回调
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  GenerativeUiCompositionItemV1,
  GenerativeUiStoredNodeV1,
  GenerativeUiSurfaceContextV1,
} from '../../lib/a2ui/types.js';
import type { AgentopsA2uiSurfaceV1 } from '../../lib/a2ui/a2ui.js';
import {
  a2uiActionNames,
  indexA2uiSurface,
  validateA2uiSurfaceForNode,
  type A2uiRuntime,
} from '../../lib/a2ui/runtime.js';
import type { A2uiPreviewRequestV1 } from '../../lib/a2ui/runtime.js';
import A2uiNode from './A2uiNode.js';

export interface A2uiRendererProps {
  /** IR 节点（包含 content 数据模型、a2ui surface、actions 等）。 */
  node: GenerativeUiStoredNodeV1;
  /** 放置信息（影响 summary/detail 视觉变体）。 */
  placement: GenerativeUiCompositionItemV1;
  /** 上下文（设备/输入/视口等）。 */
  context: GenerativeUiSurfaceContextV1;
  /** 可选 surface 覆盖（不传则用 node.a2ui）。 */
  surface?: unknown;
  /** 是否展开（影响 Artifact 等组件的展示尺寸）。 */
  expanded?: boolean;
  /** 当前 locale（默认 zh-CN）。 */
  locale?: string;
  /** 用户点击 Button 触发 action 回调。 */
  onRequestAction?: (name: string) => void;
  /** 用户点击 Artifact 触发预览。 */
  onOpenPreview?: (preview: A2uiPreviewRequestV1) => void;
  /** surface 可用 action 列表变化时通知父组件（用于补充 action 注册）。 */
  onSurfaceActions?: (names: string[]) => void;
}

export function A2uiRenderer({
  node,
  placement,
  context,
  surface,
  expanded = false,
  locale = 'zh-CN',
  onRequestAction,
  onOpenPreview,
  onSurfaceActions,
}: A2uiRendererProps) {
  // 校验 surface（合并 node + 显式 surface prop）
  // 失败时不抛异常（避免整个组件树卸载），改为显示错误卡片 + 降级用原样 surface
  const { resolvedSurface, validationError } = useMemo<{
    resolvedSurface: AgentopsA2uiSurfaceV1 | null;
    validationError: string | null;
  }>(() => {
    const source = surface ?? node.a2ui;
    try {
      const validated = validateA2uiSurfaceForNode(structuredClone(source), structuredClone(node));
      return { resolvedSurface: validated, validationError: null };
    } catch (e) {
      return { resolvedSurface: null, validationError: e instanceof Error ? e.message : String(e) };
    }
  }, [node.id, node.revision, surface, node]);

  // 组件索引：id → component（resolvedSurface 为 null 时返回空 Map，避免下游崩溃）
  const components = useMemo(
    () => (resolvedSurface ? indexA2uiSurface(resolvedSurface) : new Map<string, never>()),
    [resolvedSurface],
  );

  // 数据模型：mutable container，writeA2uiBinding 直接改内部字段
  // 用 useState 包 { value } 让 React 能感知重置（node 切换时重建容器）
  const [dataModel, setDataModel] = useState<{ value: unknown }>(() => ({
    value: structuredClone(node.content),
  }));

  // node.id 或 revision 变化时重置 dataModel（Vue watch 等价物）
  useEffect(() => {
    setDataModel({ value: structuredClone(node.content) });
  }, [node.id, node.revision, node.content]);

  // surface 变化时通知 action 列表（immediate: true 等价于初次也触发）
  useEffect(() => {
    if (onSurfaceActions && resolvedSurface) onSurfaceActions([...a2uiActionNames(resolvedSurface)]);
  }, [resolvedSurface, onSurfaceActions]);

  // requestAction / openPreview 包装：传给 runtime 供 Button/Artifact 调用
  const requestAction = useCallback((name: string) => {
    onRequestAction?.(name);
  }, [onRequestAction]);
  const openPreview = useCallback((preview: A2uiPreviewRequestV1) => {
    onOpenPreview?.(preview);
  }, [onOpenPreview]);

  // A2uiRuntime：组件索引 + 数据模型 + 上下文 + 回调
  const runtime = useMemo<A2uiRuntime>(() => ({
    components,
    dataModel,
    locale,
    compact: context.viewport === 'compact',
    expanded,
    requestAction,
    openPreview,
  }), [components, dataModel, locale, context.viewport, expanded, requestAction, openPreview]);

  // 根作用域：value = 整个 dataModel.value，key='root'
  const rootScope = useMemo(() => ({ value: dataModel.value, key: 'root' }), [dataModel]);

  // 校验失败时显示降级卡片（直接展示错误信息，便于诊断）
  // 注意：所有 hooks 必须在此 early return 之前调用，否则违反 Rules of Hooks
  if (validationError || !resolvedSurface) {
    return (
      <article className="widget-card" data-a2ui-error="true" style={{ borderColor: 'rgba(251, 191, 36, 0.2)' }}>
        <div className="widget-card-header">
          <span className="widget-card-title" style={{ color: 'rgba(251, 191, 36, 0.8)' }}>⚠️ A2UI 渲染降级</span>
        </div>
        <div style={{ padding: '8px 12px', fontSize: 12, color: 'rgba(148, 178, 214, 0.6)', lineHeight: 1.5 }}>
          Agent 生成的 UI 组件结构不完整，已降级为文本展示。
        </div>
        <pre style={{ fontSize: '10px', overflow: 'auto', whiteSpace: 'pre-wrap', margin: '0 12px 8px', padding: 4, fontFamily: 'var(--font-mono, monospace)', color: 'rgba(251, 191, 36, 0.5)' }}>
          {validationError ?? '未知错误'}
        </pre>
      </article>
    );
  }

  return (
    <section
      className="agentops-a2ui"
      data-variant={placement.variant}
      data-device={context.device}
      data-viewport={context.viewport}
      data-expanded={expanded ? 'true' : 'false'}
    >
      <A2uiNode
        key={node.id}
        componentId="root"
        runtime={runtime}
        scope={rootScope}
      />
    </section>
  );
}

export default A2uiRenderer;
