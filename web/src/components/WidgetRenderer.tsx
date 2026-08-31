import * as React from 'react';
import type { WidgetUpdate } from '../lib/api';
import A2uiWidget from './widgets/A2uiWidget';
import { WidgetErrorBoundary } from './WidgetErrorBoundary';

export interface WidgetRendererProps {
  widget: WidgetUpdate;
  onWidgetInput?: (widgetId: string, input: Record<string, unknown>) => void | Promise<void>;
}

export function WidgetRenderer({ widget, onWidgetInput }: WidgetRendererProps) {
  const type = widget.type as string;
  // a2ui surface 是嵌套组件树（components 数组），直接传 raw props
  const props = widget.props as Record<string, unknown>;
  // 单 widget 错误隔离：即使某个 widget 渲染崩溃，ErrorBoundary 兜底显示占位卡
  const content = renderWidget(type, props, widget.widget_id, onWidgetInput);
  return <WidgetErrorBoundary widget={widget}>{content}</WidgetErrorBoundary>;
}

function renderWidget(
  type: string,
  props: Record<string, unknown>,
  widgetId: string,
  onWidgetInput?: (widgetId: string, input: Record<string, unknown>) => void | Promise<void>,
): React.ReactNode {
  switch (type) {
    case 'a2ui':
      return (
        <A2uiWidget
          {...props}
          widget_id={widgetId}
          onAction={(name: string) => onWidgetInput?.(widgetId, { action: name })}
        />
      );
    default:
      // 旧 widget type（memo/form/table/chart 等）已废弃，显示提示
      return (
        <article className="widget-card">
          <div className="widget-card-header">
            <span className="widget-card-title">此组件类型已废弃: {type}</span>
          </div>
          <div className="widget-card-body" style={{ padding: '12px', color: 'var(--color-text-tertiary)', fontSize: '13px' }}>
            该 widget 类型（{type}）已迁移至 A2UI 渲染。建议重新发起会话获取新版展示。
          </div>
        </article>
      );
  }
}

export default WidgetRenderer;
