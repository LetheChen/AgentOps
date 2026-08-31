/**
 * A2UI 节点渲染器（Vue → React 移植）。
 *
 * 职责：根据 A2uiComponentV1.component 字段分发到对应 React 元素。
 * 34 个组件分支：18 个 A2UI v1.0 标准组件 + 16 个 AgentOps(Ao*) Catalog 扩展。
 *
 * Vue → React 转换要点：
 * - Vue h(tag, attrs, children) → JSX <tag {...attrs}>{children}</tag>
 * - Vue ref({value: 0}) → React useState
 * - defineComponent + setup → 函数组件 + hooks
 * - runtime.dataModel.value（mutable ref）→ 直接读，写入后由父组件 forceUpdate 触发重渲染
 * - lucide-vue-next → lucide-react（图标组件 API 一致，仅 import 路径不同）
 * - isSafeGenerativeUiExternalUri / isSafeGenerativeUiPreviewUri 从协议层 artifact-uri.ts 导入
 * - postAppearanceToArtifactFrame 省略（上游 Vue 主题系统未移植）
 */
import { useState, type CSSProperties, type ReactNode } from 'react';
import MarkdownIt from 'markdown-it';
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Bell,
  BellOff,
  CalendarDays,
  Camera,
  Check,
  Circle,
  CircleAlert,
  CircleHelp,
  CreditCard,
  Download,
  Eye,
  EyeOff,
  FastForward,
  File,
  Folder,
  Heart,
  HeartOff,
  Home,
  Image as ImageIcon,
  Info,
  Lock,
  Mail,
  MapPin,
  Menu,
  MoreHorizontal,
  MoreVertical,
  Paperclip,
  Pause,
  Pencil,
  Phone,
  Play,
  Plus,
  Printer,
  RefreshCw,
  Rewind,
  Search,
  Send,
  Settings,
  Share2,
  ShoppingCart,
  SkipBack,
  SkipForward,
  Square,
  Star,
  StarHalf,
  Trash2,
  Unlock,
  Upload,
  User,
  UserCircle,
  Volume1,
  Volume2,
  VolumeX,
  X,
  type LucideIcon,
} from 'lucide-react';
import {
  isSafeGenerativeUiExternalUri,
  isSafeGenerativeUiPreviewUri,
} from '../../lib/a2ui/artifact-uri.js';
import {
  AGENTOPS_A2UI_MAX_DEPTH,
  AGENTOPS_A2UI_MAX_SOURCE_ITEMS,
  a2uiNumber,
  a2uiText,
  a2uiTone,
  evaluateA2uiValue,
  isA2uiRecord,
  isWritableA2uiBinding,
  readA2uiItemPointer,
  readA2uiPointer,
  writeA2uiBinding,
  type A2uiEvaluationScope,
  type A2uiRuntime,
} from '../../lib/a2ui/runtime.js';
import type { A2uiComponentV1 } from '../../lib/a2ui/a2ui.js';
import A2uiDag, { type A2uiDagItem } from './A2uiDag.js';
import AoLineChart from './AoLineChart.js';
import AoPieChart from './AoPieChart.js';

// 图标名 → lucide-react 组件映射
const icons: Record<string, LucideIcon> = {
  accountCircle: UserCircle,
  add: Plus,
  arrowBack: ArrowLeft,
  arrowForward: ArrowRight,
  attachFile: Paperclip,
  calendarToday: CalendarDays,
  call: Phone,
  camera: Camera,
  check: Check,
  close: X,
  delete: Trash2,
  download: Download,
  edit: Pencil,
  event: CalendarDays,
  error: CircleAlert,
  fastForward: FastForward,
  favorite: Heart,
  favoriteOff: HeartOff,
  folder: Folder,
  help: CircleHelp,
  home: Home,
  info: Info,
  locationOn: MapPin,
  lock: Lock,
  lockOpen: Unlock,
  mail: Mail,
  menu: Menu,
  moreVert: MoreVertical,
  moreHoriz: MoreHorizontal,
  notificationsOff: BellOff,
  notifications: Bell,
  pause: Pause,
  payment: CreditCard,
  person: User,
  phone: Phone,
  photo: ImageIcon,
  play: Play,
  print: Printer,
  refresh: RefreshCw,
  rewind: Rewind,
  search: Search,
  send: Send,
  settings: Settings,
  share: Share2,
  shoppingCart: ShoppingCart,
  skipNext: SkipForward,
  skipPrevious: SkipBack,
  star: Star,
  starHalf: StarHalf,
  starOff: Circle,
  stop: Square,
  upload: Upload,
  visibility: Eye,
  visibilityOff: EyeOff,
  volumeDown: Volume1,
  volumeMute: VolumeX,
  volumeOff: VolumeX,
  volumeUp: Volume2,
  warning: AlertTriangle,
};

// markdown-it 实例：禁用 link/image/autolink/html（防 XSS）
const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true, typographer: true });
markdown.inline.ruler.disable(['link', 'image', 'autolink', 'html_inline']);
markdown.block.ruler.disable(['html_block']);

const GAP_VALUES = new Set(['none', 'xs', 'sm', 'md', 'lg']);
const ALIGN_VALUES = new Set(['start', 'center', 'end', 'stretch']);
const JUSTIFY_VALUES = new Set(['start', 'center', 'end', 'spaceBetween', 'spaceAround', 'spaceEvenly', 'stretch']);

/** 组件名 → BEM 类名后缀（AoGrid → grid，AoGridItem → grid-item，Text → text）。 */
function className(component: string): string {
  return component
    .replace(/^Ao/, '')
    .replace(/^Hr/, '')
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .toLowerCase();
}

/** 构造节点公共属性（class + data-component + data-a2ui-id + weight style）。 */
function componentAttrs(component: A2uiComponentV1, extra: Record<string, unknown> = {}): Record<string, unknown> {
  const weight = a2uiNumber('weight' in component ? component.weight : undefined);
  return {
    className: ['ao-a2ui__node', `ao-a2ui__${className(component.component)}`].join(' '),
    'data-component': component.component,
    'data-a2ui-id': component.id,
    ...(weight !== undefined ? { style: { flexGrow: Math.max(0, weight) } as CSSProperties } : {}),
    ...extra,
  };
}

function boundedInteger(value: unknown, fallback: number, min: number, max: number): number {
  const number = a2uiNumber(value);
  return number === undefined ? fallback : Math.max(min, Math.min(max, Math.round(number)));
}

function evaluated(componentValue: unknown, runtime: A2uiRuntime, scope: A2uiEvaluationScope): unknown {
  return evaluateA2uiValue(componentValue, runtime, scope);
}

function text(componentValue: unknown, runtime: A2uiRuntime, scope: A2uiEvaluationScope): string {
  return a2uiText(evaluated(componentValue, runtime, scope));
}

function tone(componentValue: unknown, runtime: A2uiRuntime, scope: A2uiEvaluationScope): string {
  return a2uiTone(evaluated(componentValue, runtime, scope));
}

function itemText(item: unknown, path: unknown): string {
  return a2uiText(readA2uiItemPointer(item, path));
}

function itemNumber(item: unknown, path: unknown): number | undefined {
  return a2uiNumber(readA2uiItemPointer(item, path));
}

/** 从 source binding 读出数组，按 maxItems 截断。 */
function sourceItems(
  component: A2uiComponentV1,
  runtime: A2uiRuntime,
  scope: A2uiEvaluationScope,
): unknown[] {
  if (!('source' in component)) return [];
  const source = evaluated(component.source, runtime, scope);
  if (!Array.isArray(source)) return [];
  const maxItems = boundedInteger(
    'maxItems' in component ? component.maxItems : undefined,
    AGENTOPS_A2UI_MAX_SOURCE_ITEMS,
    1,
    AGENTOPS_A2UI_MAX_SOURCE_ITEMS,
  );
  return source.slice(0, maxItems);
}

/** 单元格格式化（number/percent/duration/datetime/text）。 */
function formattedCell(value: unknown, format: unknown, runtime: A2uiRuntime, scope: A2uiEvaluationScope): string {
  if (format === 'number') return a2uiText(evaluateA2uiValue({ call: 'formatNumber', args: { value } }, runtime, scope));
  if (format === 'percent') {
    const number = a2uiNumber(value);
    if (number === undefined) return '';
    return new Intl.NumberFormat(runtime.locale, { style: 'percent', maximumFractionDigits: 1 }).format(
      Math.abs(number) > 1 ? number / 100 : number,
    );
  }
  if (format === 'duration') {
    const milliseconds = a2uiNumber(value);
    if (milliseconds === undefined) return '';
    const seconds = Math.max(0, Math.round(milliseconds / 1_000));
    const hours = Math.floor(seconds / 3_600);
    const minutes = Math.floor((seconds % 3_600) / 60);
    const remainder = seconds % 60;
    return [hours ? `${hours}h` : '', minutes ? `${minutes}m` : '', remainder || (!hours && !minutes) ? `${remainder}s` : '']
      .filter(Boolean)
      .slice(0, 2)
      .join(' ');
  }
  if (format === 'datetime') {
    const date = new Date(value as string | number);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(runtime.locale, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(date);
  }
  return a2uiText(value);
}

/** 校验消息：checks 数组中 condition === false 的项 message 收集。 */
function validationMessages(
  component: A2uiComponentV1,
  runtime: A2uiRuntime,
  scope: A2uiEvaluationScope,
): string[] {
  if (!('checks' in component) || !Array.isArray(component.checks)) return [];
  return component.checks.flatMap(check => {
    if (!isA2uiRecord(check) || evaluated(check.condition, runtime, scope) !== false) return [];
    return [a2uiText(check.message) || 'Invalid value'];
  });
}

/** 渲染校验消息列表（role="alert" 小字）。 */
function validationFeedback(messages: readonly string[]): ReactNode[] {
  return messages.map((message, index) => (
    <small key={`msg-${index}`} className="ao-a2ui__validation" role="alert">{message}</small>
  ));
}

interface RenderNodeContext {
  runtime: A2uiRuntime;
  scope: A2uiEvaluationScope;
  ancestors: string[];
  selectedTab: number;
  setSelectedTab: (v: number) => void;
  modalOpen: boolean;
  setModalOpen: (v: boolean) => void;
  choiceFilter: string;
  setChoiceFilter: (v: string) => void;
}

/** 渲染单个 A2UI 节点（递归调用子节点通过 <A2uiNode />）。 */
function renderNode(component: A2uiComponentV1 & Record<string, unknown>, ctx: RenderNodeContext): ReactNode {
  const { runtime, scope, ancestors } = ctx;
  // Legacy 前缀归一化：历史 session 数据 → Ao（当前命名）。
  const componentName: string = component.component.replace(/^Hr/, 'Ao');

  // 环依赖 / 深度保护
  if (ancestors.includes(component.id) || ancestors.length >= AGENTOPS_A2UI_MAX_DEPTH) {
    throw new Error(`Generated A2UI component graph is cyclic or too deep at ${component.id}`);
  }
  const nextAncestors = [...ancestors, component.id];

  const accessibilityLabel = text(component.accessibility?.label, runtime, scope);
  const accessibilityDescription = text(component.accessibility?.description, runtime, scope);

  const nodeAttrs = (extra: Record<string, unknown> = {}): Record<string, unknown> => componentAttrs(component, {
    ...(accessibilityLabel ? { 'aria-label': accessibilityLabel } : {}),
    ...(accessibilityDescription ? { 'aria-description': accessibilityDescription } : {}),
    ...extra,
  });

  // 渲染子组件：根据 id 从 runtime.components 查找并递归
  const child = (id: unknown, childScope = scope, key = a2uiText(id)): ReactNode => {
    if (typeof id !== 'string' || !runtime.components.has(id)) {
      throw new Error(`Generated A2UI component reference is missing: ${String(id)}`);
    }
    return (
      <A2uiNode
        key={`${key}:${childScope.key}`}
        runtime={runtime}
        componentId={id}
        scope={childScope}
        ancestors={nextAncestors}
      />
    );
  };

  // 渲染 children 列表（array of id 或 ChildListV1 binding）
  const children = (value: unknown): ReactNode[] => {
    if (Array.isArray(value)) return value.map((id, index) => child(id, scope, `${String(id)}:${index}`));
    if (!isA2uiRecord(value) || typeof value.path !== 'string' || typeof value.componentId !== 'string') return [];
    const collection = readA2uiPointer(value.path, runtime.dataModel.value, scope);
    if (!Array.isArray(collection)) return [];
    return collection.slice(0, AGENTOPS_A2UI_MAX_SOURCE_ITEMS).map((item, index) => child(
      value.componentId,
      { value: item, key: `${scope.key}:${value.path}:${index}`, index },
      `${value.componentId}:${index}`,
    ));
  };

  // === 18 个 A2UI v1.0 标准组件 ===

  if (component.component === 'Text') {
    const value = text(component.text, runtime, scope);
    const variant = typeof component.variant === 'string' ? component.variant : 'body';
    return (
      <div
        {...nodeAttrs({ 'data-variant': variant })}
        dangerouslySetInnerHTML={{ __html: markdown.render(value) }}
      />
    );
  }

  if (component.component === 'Image') {
    const url = text(component.url, runtime, scope);
    const description = text(component.description, runtime, scope);
    if (!isSafeGenerativeUiPreviewUri(url)) {
      return <div {...nodeAttrs({ 'data-unavailable': 'true' })} role="img" aria-label={accessibilityLabel || description} />;
    }
    return (
      <img
        {...nodeAttrs()}
        src={url}
        alt={description}
        loading="lazy"
        referrerPolicy="no-referrer"
        data-fit={typeof component.fit === 'string' ? component.fit : 'fill'}
        data-variant={typeof component.variant === 'string' ? component.variant : 'mediumFeature'}
      />
    );
  }

  if (component.component === 'Icon') {
    const name = text(component.name, runtime, scope);
    const Icon = icons[name] ?? Info;
    return (
      <i {...nodeAttrs()}>
        <Icon size={18} aria-hidden />
      </i>
    );
  }

  if (component.component === 'Video' || component.component === 'AudioPlayer') {
    const url = text(component.url, runtime, scope);
    if (!isSafeGenerativeUiPreviewUri(url)) {
      return <div {...nodeAttrs({ 'data-unavailable': 'true' })} />;
    }
    if (component.component === 'Video') {
      const poster = text(component.posterUrl, runtime, scope);
      return (
        <video
          {...nodeAttrs()}
          src={url}
          {...(isSafeGenerativeUiPreviewUri(poster) ? { poster } : {})}
          controls
          preload="metadata"
        />
      );
    }
    const description = text(component.description, runtime, scope);
    return (
      <figure {...nodeAttrs()}>
        {description ? <figcaption>{description}</figcaption> : null}
        <audio src={url} controls preload="metadata" />
      </figure>
    );
  }

  if (component.component === 'Row' || component.component === 'Column' || component.component === 'List') {
    const direction = component.component === 'Row'
      ? 'row'
      : component.component === 'List' && component.direction === 'horizontal' ? 'row' : 'column';
    const directHtmlArtifacts = component.component === 'Column'
      && ancestors.length === 0
      && Array.isArray(component.children)
      ? component.children.filter(id => {
          const candidate = typeof id === 'string' ? runtime.components.get(id) : undefined;
          return candidate?.component === 'AoArtifact'
            && candidate.kind === 'html'
            && isSafeGenerativeUiPreviewUri(text(candidate.uri, runtime, scope));
        })
      : [];
    return (
      <div
        {...nodeAttrs({
          'data-direction': direction,
          'data-justify': 'justify' in component && JUSTIFY_VALUES.has(String(component.justify)) ? component.justify : 'start',
          'data-align': ALIGN_VALUES.has(String(component.align)) ? component.align : 'stretch',
          ...(directHtmlArtifacts.length === 1 ? { 'data-fill-html-artifact': directHtmlArtifacts[0] } : {}),
        })}
      >
        {children(component.children)}
      </div>
    );
  }

  if (component.component === 'Card') {
    return <article {...nodeAttrs()}>{child(component.child)}</article>;
  }

  if (component.component === 'Tabs') {
    const tabs = Array.isArray(component.tabs) ? component.tabs.filter(isA2uiRecord) : [];
    const active = Math.min(ctx.selectedTab, Math.max(0, tabs.length - 1));
    const activeTab = tabs[active];
    return (
      <section {...nodeAttrs()}>
        <div className="ao-a2ui__tab-list" role="tablist">
          {tabs.map((tab, index) => (
            <button
              key={`tab:${index}`}
              type="button"
              role="tab"
              aria-selected={index === active}
              onClick={() => ctx.setSelectedTab(index)}
            >
              {text(tab.title, runtime, scope)}
            </button>
          ))}
        </div>
        {activeTab ? (
          <div className="ao-a2ui__tab-panel" role="tabpanel">{child(activeTab.child)}</div>
        ) : null}
      </section>
    );
  }

  if (component.component === 'Modal') {
    return (
      <div {...nodeAttrs()}>
        <div
          className="ao-a2ui__modal-trigger"
          onClickCapture={event => {
            event.stopPropagation();
            ctx.setModalOpen(true);
          }}
        >
          {child(component.trigger)}
        </div>
        {ctx.modalOpen ? (
          <div className="ao-a2ui__modal-backdrop" role="presentation">
            <section className="ao-a2ui__modal-panel" role="dialog" aria-modal="true">
              <button
                className="ao-a2ui__modal-close"
                type="button"
                title="Close"
                aria-label="Close"
                onClick={() => ctx.setModalOpen(false)}
              >
                <X size={18} aria-hidden />
              </button>
              {child(component.content)}
            </section>
          </div>
        ) : null}
      </div>
    );
  }

  if (component.component === 'Divider') {
    return <hr {...nodeAttrs({ 'data-axis': component.axis === 'vertical' ? 'vertical' : 'horizontal' })} />;
  }

  if (component.component === 'Button') {
    const action = 'event' in component.action && isA2uiRecord(component.action.event) ? component.action.event : undefined;
    const name = typeof action?.name === 'string' ? action.name : '';
    const messages = validationMessages(component, runtime, scope);
    return (
      <div {...nodeAttrs({ 'data-variant': typeof component.variant === 'string' ? component.variant : 'default' })}>
        <button
          type="button"
          disabled={!name || messages.length > 0}
          onClick={event => {
            event.stopPropagation();
            if (name && messages.length === 0) runtime.requestAction(name);
          }}
        >
          {child(component.child)}
        </button>
        {validationFeedback(messages)}
      </div>
    );
  }

  if (component.component === 'TextField') {
    const messages = validationMessages(component, runtime, scope);
    const current = text(component.value, runtime, scope);
    const writable = isWritableA2uiBinding(component.value);
    const placeholder = text(component.placeholder, runtime, scope);
    const label = text(component.label, runtime, scope);
    const onInput = (event: React.FormEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      writeA2uiBinding(component.value, event.currentTarget.value, runtime.dataModel.value, scope);
    };
    return (
      <label {...nodeAttrs()}>
        <span>{label}</span>
        {component.variant === 'longText' ? (
          <textarea
            value={current}
            placeholder={placeholder}
            rows={3}
            disabled={!writable}
            aria-invalid={messages.length > 0 ? 'true' : undefined}
            onInput={onInput}
            onChange={onInput}
          />
        ) : (
          <input
            value={current}
            placeholder={placeholder}
            type={component.variant === 'obscured' ? 'password' : component.variant === 'number' ? 'number' : 'text'}
            disabled={!writable}
            aria-invalid={messages.length > 0 ? 'true' : undefined}
            onInput={onInput}
            onChange={onInput}
          />
        )}
        {validationFeedback(messages)}
      </label>
    );
  }

  if (component.component === 'CheckBox') {
    const messages = validationMessages(component, runtime, scope);
    const writable = isWritableA2uiBinding(component.value);
    const label = text(component.label, runtime, scope);
    const checked = Boolean(evaluated(component.value, runtime, scope));
    return (
      <label {...nodeAttrs()}>
        <input
          type="checkbox"
          checked={checked}
          disabled={!writable}
          aria-invalid={messages.length > 0 ? 'true' : undefined}
          onChange={event => writeA2uiBinding(component.value, event.target.checked, runtime.dataModel.value, scope)}
        />
        <span>{label}</span>
        {validationFeedback(messages)}
      </label>
    );
  }

  if (component.component === 'ChoicePicker') {
    const options = Array.isArray(component.options) ? component.options.filter(isA2uiRecord) : [];
    const selected = evaluated(component.value, runtime, scope);
    const selectedValues = Array.isArray(selected) ? selected.map(a2uiText) : [];
    const messages = validationMessages(component, runtime, scope);
    const writable = isWritableA2uiBinding(component.value);
    const query = ctx.choiceFilter.trim().toLocaleLowerCase(runtime.locale);
    const visibleOptions = query
      ? options.filter(option => text(option.label, runtime, scope).toLocaleLowerCase(runtime.locale).includes(query))
      : options;
    const multiple = component.variant === 'multipleSelection';
    const displayStyle = component.displayStyle === 'chips' ? 'chips' : 'checkbox';
    const label = text(component.label, runtime, scope);
    const setSelection = (value: string, checked: boolean): void => {
      const next = multiple
        ? checked
          ? [...new Set([...selectedValues, value])]
          : selectedValues.filter(candidate => candidate !== value)
        : checked ? [value] : [];
      writeA2uiBinding(component.value, next, runtime.dataModel.value, scope);
    };
    return (
      <fieldset
        {...nodeAttrs({
          'data-display-style': displayStyle,
          'data-variant': multiple ? 'multipleSelection' : 'mutuallyExclusive',
        })}
      >
        {label ? <legend>{label}</legend> : null}
        {component.filterable ? (
          <input
            className="ao-a2ui__choice-filter"
            type="search"
            value={ctx.choiceFilter}
            aria-label={label}
            onInput={event => ctx.setChoiceFilter((event.target as HTMLInputElement).value)}
            onChange={event => ctx.setChoiceFilter(event.target.value)}
          />
        ) : null}
        <div className="ao-a2ui__choice-options">
          {visibleOptions.map(option => {
            const value = a2uiText(option.value);
            return (
              <label key={value} data-selected={selectedValues.includes(value) ? 'true' : 'false'}>
                <input
                  type={multiple ? 'checkbox' : 'radio'}
                  name={multiple ? undefined : `choice:${component.id}:${scope.key}`}
                  value={value}
                  checked={selectedValues.includes(value)}
                  disabled={!writable}
                  aria-invalid={messages.length > 0 ? 'true' : undefined}
                  onChange={event => setSelection(value, event.target.checked)}
                />
                <span>{text(option.label, runtime, scope)}</span>
              </label>
            );
          })}
        </div>
        {validationFeedback(messages)}
      </fieldset>
    );
  }

  if (component.component === 'Slider') {
    const current = a2uiNumber(evaluated(component.value, runtime, scope)) ?? 0;
    const messages = validationMessages(component, runtime, scope);
    const writable = isWritableA2uiBinding(component.value);
    const min = a2uiNumber(component.min) ?? 0;
    const max = a2uiNumber(component.max) ?? 100;
    const divisions = a2uiNumber(component.steps);
    const step = divisions === undefined ? 'any' : Math.max(Number.EPSILON, (max - min) / divisions);
    const label = text(component.label, runtime, scope);
    return (
      <label {...nodeAttrs()}>
        {label ? <span>{label}</span> : null}
        <input
          type="range"
          value={current}
          min={min}
          max={max}
          step={step}
          disabled={!writable}
          aria-invalid={messages.length > 0 ? 'true' : undefined}
          onInput={event => writeA2uiBinding(component.value, Number((event.target as HTMLInputElement).value), runtime.dataModel.value, scope)}
        />
        <output>{String(current)}</output>
        {validationFeedback(messages)}
      </label>
    );
  }

  if (component.component === 'DateTimeInput') {
    const type = component.enableDate && component.enableTime ? 'datetime-local' : component.enableTime ? 'time' : 'date';
    const messages = validationMessages(component, runtime, scope);
    const writable = isWritableA2uiBinding(component.value);
    const label = text(component.label, runtime, scope);
    const current = text(component.value, runtime, scope);
    const min = text(component.min, runtime, scope);
    const max = text(component.max, runtime, scope);
    return (
      <label {...nodeAttrs()}>
        {label ? <span>{label}</span> : null}
        <input
          type={type}
          value={current}
          min={min || undefined}
          max={max || undefined}
          disabled={!writable}
          aria-invalid={messages.length > 0 ? 'true' : undefined}
          onInput={event => writeA2uiBinding(component.value, event.currentTarget.value, runtime.dataModel.value, scope)}
          onChange={event => writeA2uiBinding(component.value, event.currentTarget.value, runtime.dataModel.value, scope)}
        />
        {validationFeedback(messages)}
      </label>
    );
  }

  // === 16 个 AgentOps(Ao*) Catalog 扩展组件 ===

  if (componentName === 'AoGrid') {
    const columns: Record<string, unknown> = isA2uiRecord(component.columns) ? component.columns : {};
    const compactColumns = boundedInteger(columns.compact, 1, 1, 3);
    return (
      <div
        {...nodeAttrs({
          'data-gap': GAP_VALUES.has(String(component.gap)) ? component.gap : 'md',
          'data-align': ALIGN_VALUES.has(String(component.align)) ? component.align : 'stretch',
          'data-compact-columns': compactColumns,
          style: {
            '--columns': boundedInteger(columns.default, 1, 1, 3),
            '--compact-columns': compactColumns,
          } as CSSProperties,
        })}
      >
        {children(component.children)}
      </div>
    );
  }

  if (componentName === 'AoGridItem') {
    const span = boundedInteger(component.span, 1, 1, 3);
    return (
      <div {...nodeAttrs({ style: { gridColumn: `span ${span}` }, 'data-span': span })}>
        {child(component.child)}
      </div>
    );
  }

  if (componentName === 'AoSection') {
    return (
      <section {...nodeAttrs({ 'data-tone': tone(component.tone, runtime, scope) })}>
        {component.title === undefined ? null : <header>{text(component.title, runtime, scope)}</header>}
        {children(component.children)}
      </section>
    );
  }

  if (componentName === 'AoMetric') {
    return (
      <article {...nodeAttrs({ 'data-tone': tone(component.tone, runtime, scope) })}>
        <span>{text(component.label, runtime, scope)}</span>
        <strong>
          {text(component.value, runtime, scope)}
          {component.unit === undefined ? null : <small>{text(component.unit, runtime, scope)}</small>}
        </strong>
      </article>
    );
  }

  if (componentName === 'AoStatusBadge') {
    return (
      <span {...nodeAttrs({ 'data-tone': tone(component.tone, runtime, scope) })}>
        {text(component.text, runtime, scope)}
      </span>
    );
  }

  if (componentName === 'AoProgress') {
    const value = Math.max(0, Math.min(100, a2uiNumber(evaluated(component.value, runtime, scope)) ?? 0));
    const label = component.label === undefined ? null : (
      <header>
        <span>{text(component.label, runtime, scope)}</span>
        <strong>{value}%</strong>
      </header>
    );
    return (
      <div {...nodeAttrs({ 'data-tone': tone(component.tone, runtime, scope) })}>
        {label}
        <div
          className="ao-a2ui__progress-track"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={value}
        >
          <i style={{ width: `${value}%` }} />
        </div>
      </div>
    );
  }

  if (componentName === 'AoStep') {
    const index = text(component.index, runtime, scope) || String((scope.index ?? 0) + 1);
    const detail = text(component.detail, runtime, scope);
    return (
      <article {...nodeAttrs({ 'data-tone': tone(component.tone, runtime, scope) })}>
        <div className="ao-a2ui__step-rail" aria-hidden>
          <span>{index}</span>
        </div>
        <div className="ao-a2ui__step-content">
          <header>
            <strong>{text(component.label, runtime, scope)}</strong>
            {detail ? <small>{detail}</small> : null}
          </header>
          {child(component.child)}
        </div>
      </article>
    );
  }

  if (componentName === 'AoList') {
    return (
      <ul {...nodeAttrs()}>
        {sourceItems(component, runtime, scope).map((item, index) => {
          const itemTone = a2uiTone(readA2uiItemPointer(item, component.itemStatusPath));
          return (
            <li key={`item:${index}`} data-tone={itemTone}>
              <i />
              <div>
                <strong>{itemText(item, component.itemTitlePath)}</strong>
                {component.itemDetailPath === undefined ? null : <p>{itemText(item, component.itemDetailPath)}</p>}
              </div>
              {component.itemBadgePath === undefined ? null : <span>{itemText(item, component.itemBadgePath)}</span>}
            </li>
          );
        })}
      </ul>
    );
  }

  if (componentName === 'AoTable') {
    const columns = Array.isArray(component.columns) ? component.columns.filter(isA2uiRecord) : [];
    return (
      <div {...nodeAttrs()}>
        <table className="ao-a2ui__table">
          <thead>
            <tr>
              {columns.map(column => (
                <th key={a2uiText(column.id)}>{text(column.label, runtime, scope)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sourceItems(component, runtime, scope).map((item, rowIndex) => (
              <tr key={`row:${rowIndex}`}>
                {columns.map(column => {
                  const value = readA2uiItemPointer(item, column.path);
                  return (
                    <td key={a2uiText(column.id)} data-format={column.format}>
                      {formattedCell(value, column.format, runtime, scope)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (componentName === 'AoTimeline') {
    return (
      <ol {...nodeAttrs()}>
        {sourceItems(component, runtime, scope).map((item, index) => {
          const status = itemText(item, component.itemStatusPath);
          return (
            <li key={`timeline:${index}`} data-tone={a2uiTone(status)}>
              <i />
              {component.itemTimePath === undefined ? null : <time>{itemText(item, component.itemTimePath)}</time>}
              <div>
                <strong>{itemText(item, component.itemTitlePath)}</strong>
                {component.itemDetailPath === undefined ? null : <p>{itemText(item, component.itemDetailPath)}</p>}
              </div>
            </li>
          );
        })}
      </ol>
    );
  }

  if (componentName === 'AoBarChart') {
    const items = sourceItems(component, runtime, scope);
    const values = items.map(item => itemNumber(item, component.itemValuePath) ?? 0);
    const max = Math.max(1, ...values);
    return (
      <div {...nodeAttrs()}>
        {items.map((item, index) => {
          const itemTone = a2uiTone(readA2uiItemPointer(item, component.itemTonePath));
          const value = values[index] ?? 0;
          return (
            <div key={`bar:${index}`} data-tone={itemTone}>
              <span>{itemText(item, component.itemLabelPath)}</span>
              <i>
                <b style={{ width: `${Math.max(2, (value / max) * 100)}%` }} />
              </i>
              <strong>{formattedCell(value, 'number', runtime, scope)}</strong>
            </div>
          );
        })}
      </div>
    );
  }

  if (componentName === 'AoLineChart') {
    const seriesRaw = sourceItems(component, runtime, scope);
    const xAxisBinding = (component as Record<string, unknown>).xAxis;
    const xAxisRaw = Array.isArray(xAxisBinding) ? xAxisBinding : evaluated(xAxisBinding, runtime, scope);
    const xAxis = (Array.isArray(xAxisRaw) ? xAxisRaw : []).map(a2uiText);
    const seriesNamePath = (component as Record<string, unknown>).seriesNamePath as string | undefined;
    const seriesDataPath = (component as Record<string, unknown>).seriesDataPath as string | undefined;
    const unit = a2uiText((component as Record<string, unknown>).unit);
    const series = seriesRaw.map((item) => ({
      name: a2uiText(readA2uiItemPointer(item, seriesNamePath)) || '',
      data: (Array.isArray(readA2uiItemPointer(item, seriesDataPath))
        ? (readA2uiItemPointer(item, seriesDataPath) as unknown[]).map((v) => Number(v))
        : []),
    }));
    return <AoLineChart {...nodeAttrs()} series={series} xAxis={xAxis} unit={unit || undefined} />;
  }

  if (componentName === 'AoPieChart') {
    const items = sourceItems(component, runtime, scope);
    const unit = a2uiText((component as Record<string, unknown>).unit);
    const pieData = items.map((item) => ({
      label: itemText(item, component.itemLabelPath) || '',
      value: itemNumber(item, component.itemValuePath) ?? 0,
    }));
    return <AoPieChart {...nodeAttrs()} items={pieData} unit={unit || undefined} />;
  }

  if (componentName === 'AoDag') {
    const items: A2uiDagItem[] = sourceItems(component, runtime, scope).map((item, index) => {
      const status = itemText(item, component.itemStatusPath);
      const dependencies = readA2uiItemPointer(item, component.itemDependsOnPath);
      return {
        id: itemText(item, component.itemIdPath) || `node-${index}`,
        title: itemText(item, component.itemLabelPath),
        detail: itemText(item, component.itemDetailPath),
        status,
        tone: a2uiTone(status),
        progress: itemNumber(item, component.itemProgressPath),
        dependsOn: Array.isArray(dependencies) ? dependencies.map(a2uiText).filter(Boolean) : [],
      };
    });
    return <A2uiDag {...nodeAttrs()} items={items} />;
  }

  if (componentName === 'AoDisclosure') {
    return (
      <details {...nodeAttrs()} open={runtime.expanded || Boolean(evaluated(component.open, runtime, scope))}>
        <summary>{text(component.title, runtime, scope)}</summary>
        <div>{children(component.children)}</div>
      </details>
    );
  }

  if (componentName === 'AoLink') {
    const url = text(component.url, runtime, scope);
    const label = text(component.label, runtime, scope);
    const description = text(component.description, runtime, scope);
    if (!isSafeGenerativeUiExternalUri(url)) {
      return <span {...nodeAttrs({ 'data-unavailable': 'true' })}>{label || 'Link unavailable'}</span>;
    }
    return (
      <a
        {...nodeAttrs()}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        referrerPolicy="no-referrer"
      >
        <span>{label}</span>
        {description ? <small>{description}</small> : null}
        <ArrowRight size={15} aria-hidden />
      </a>
    );
  }

  if (componentName === 'AoArtifact') {
    const uri = text(component.uri, runtime, scope);
    const title = text(component.title, runtime, scope);
    const description = text(component.description, runtime, scope);
    const kind: 'image' | 'html' | 'file' = component.kind === 'image' || component.kind === 'html' ? component.kind : 'file';
    if (!isSafeGenerativeUiPreviewUri(uri)) {
      return (
        <div {...nodeAttrs({ 'data-artifact-kind': kind })} data-unavailable="true">
          <File size={18} />
          <span>{title || 'Artifact unavailable'}</span>
        </div>
      );
    }
    const preview = () => runtime.openPreview({
      title: title || undefined,
      url: uri,
      kind: kind === 'image' ? 'image' : 'html',
      layout: component.layout === 'portrait' ? 'portrait' : 'fluid',
    });
    if (kind === 'image') {
      return (
        <button {...nodeAttrs({ 'data-artifact-kind': kind })} type="button" onClick={preview}>
          <img
            src={uri}
            alt={text(component.alt, runtime, scope) || title}
            loading="lazy"
            referrerPolicy="no-referrer"
          />
          <span>
            <strong>{title}</strong>
            {description ? <small>{description}</small> : null}
          </span>
        </button>
      );
    }
    if (kind === 'html') {
      // 省略 postAppearanceToArtifactFrame（上游 Vue 主题系统未移植）
      return (
        <div {...nodeAttrs({ 'data-artifact-kind': kind })}>
          <iframe
            src={uri}
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            allow=""
            tabIndex={-1}
            title={title || 'HTML artifact preview'}
            data-agentops-artifact-frame=""
          />
        </div>
      );
    }
    return (
      <button {...nodeAttrs({ 'data-artifact-kind': kind })} type="button" onClick={preview}>
        <File size={18} aria-hidden />
        <span>
          <strong>{title || 'Artifact'}</strong>
          {description ? <small>{description}</small> : null}
        </span>
      </button>
    );
  }

  if (componentName === 'AoIf') {
    return (
      <div {...nodeAttrs()}>
        {Boolean(evaluated(component.condition, runtime, scope)) ? children(component.children) : null}
      </div>
    );
  }

  throw new Error(`Generated A2UI component is not in the AgentOps Catalog: ${componentName}`);
}

interface A2uiNodeProps {
  runtime: A2uiRuntime;
  componentId: string;
  scope: A2uiEvaluationScope;
  ancestors?: string[];
}

/** A2uiNode 函数组件：每个节点持自己的 UI state（tab/modal/choiceFilter）。 */
export function A2uiNode({ runtime, componentId, scope, ancestors = [] }: A2uiNodeProps) {
  const [selectedTab, setSelectedTab] = useState(0);
  const [modalOpen, setModalOpen] = useState(false);
  const [choiceFilter, setChoiceFilter] = useState('');

  const component = runtime.components.get(componentId);
  if (!component) throw new Error(`Generated A2UI component is missing: ${componentId}`);

  return (
    <>
      {renderNode(component as A2uiComponentV1 & Record<string, unknown>, {
        runtime,
        scope,
        ancestors,
        selectedTab,
        setSelectedTab,
        modalOpen,
        setModalOpen,
        choiceFilter,
        setChoiceFilter,
      })}
    </>
  );
}

export default A2uiNode;
