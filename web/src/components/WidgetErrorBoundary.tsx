import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * WidgetErrorBoundary — 单个 widget 崩溃时只卸载该 widget，
 * 不影响整个 widget-panel + chat panel 的渲染（避免"对话过程中黑屏"）。
 *
 * 用法：包裹 WidgetRenderer，如
 *   <WidgetErrorBoundary widget={w}>
 *     <WidgetRenderer widget={w} onWidgetInput={...} />
 *   </WidgetErrorBoundary>
 */
interface Props {
  widget: { widget_id: string; type: string };
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class WidgetErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error(
      `[WidgetErrorBoundary] widget ${this.props.widget.widget_id} (type=${this.props.widget.type}) crashed:`,
      error,
      info.componentStack,
    );
  }

  render() {
    if (this.state.hasError) {
      return (
        <article
          className="widget-card"
          style={{
            borderColor: 'var(--state-error)',
            background: 'rgba(239, 68, 68, 0.05)',
          }}
        >
          <div className="widget-card-header">
            <span
              className="widget-card-title"
              style={{ color: 'var(--state-error)' }}
            >
              ⚠️ 组件渲染失败（{this.props.widget.type}）
            </span>
          </div>
          <div
            style={{
              fontSize: '12px',
              color: 'var(--color-text-secondary)',
              fontFamily: 'var(--font-mono, monospace)',
              padding: '8px',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {this.state.error?.message || 'unknown error'}
          </div>
        </article>
      );
    }
    return this.props.children;
  }
}

export default WidgetErrorBoundary;