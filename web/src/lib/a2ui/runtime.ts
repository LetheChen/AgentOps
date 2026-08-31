/**
 * A2UI 运行时（runtime）helper 函数集合。
 *
 * 与协议层 web/src/lib/a2ui/a2ui.ts（定义组件结构 / 校验）的区别：
 * - 协议层：纯数据结构 + JSON Schema 校验，无渲染依赖
 * - 运行时层（本文件）：执行期数据绑定解析、纯函数求值、tone 推断等
 *
 * Vue → React 转换要点：
 * - 原版 A2uiRuntime.dataModel 是 Vue Ref<unknown>（响应式）
 * - React 版改为 mutable ref 对象 { value: unknown }，配合父组件 forceUpdate 触发重渲染
 *   （writeA2uiBinding 写完值后由调用方触发 re-render，runtime 不主动通知）
 */
import {
  AGENTOPS_A2UI_MAX_DEPTH,
  AGENTOPS_A2UI_MAX_SOURCE_ITEMS,
  type A2uiComponentV1,
  type AgentopsA2uiSurfaceV1,
} from './a2ui.js';
import { validateAgentopsA2uiSurface } from './validation.js';
import type { GenerativeUiStoredNodeV1 } from './types.js';

export {
  AGENTOPS_A2UI_CATALOG_ID,
  AGENTOPS_A2UI_MAX_BYTES,
  AGENTOPS_A2UI_MAX_COMPONENTS,
  AGENTOPS_A2UI_MAX_DEPTH,
  AGENTOPS_A2UI_MAX_DIRECT_CHILDREN,
  AGENTOPS_A2UI_MAX_SOURCE_ITEMS,
  AGENTOPS_A2UI_VERSION,
} from './a2ui.js';
export type { A2uiComponentV1, AgentopsA2uiSurfaceV1 } from './a2ui.js';

/** 预览请求载荷（与 AgentOps GenerativeUiPreviewRequestV1 一致，独立定义避免循环依赖）。 */
export interface A2uiPreviewRequestV1 {
  title?: string;
  url: string;
  kind: 'image' | 'html';
  layout: 'fluid' | 'portrait';
}

/** 求值作用域：value = 当前数据上下文（List/Repeat 子项时切换为 item）。 */
export interface A2uiEvaluationScope {
  value: unknown;
  key: string;
  index?: number;
}

/**
 * 运行时上下文。dataModel 用 mutable ref 对象（去掉 Vue Ref）。
 * 父组件用 useRef<A2uiRuntimeData>({ value: ... }) 持有，写入时通过 forceUpdate 触发重渲染。
 */
export interface A2uiRuntime {
  components: ReadonlyMap<string, A2uiComponentV1>;
  /** 可变数据容器，writeA2uiBinding 直接修改 value 内部字段。 */
  dataModel: { value: unknown };
  locale: string;
  compact: boolean;
  expanded: boolean;
  requestAction: (name: string) => void;
  openPreview: (preview: A2uiPreviewRequestV1) => void;
}

const UNSAFE_POINTER_SEGMENTS = new Set(['__proto__', 'prototype', 'constructor']);
const TONES = new Set(['neutral', 'positive', 'info', 'warning', 'critical']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function pointerSegments(pointer: string, relative: boolean): string[] | undefined {
  if (pointer === '') return [];
  const source = relative ? pointer.replace(/^\//, '') : pointer.slice(1);
  if (!relative && !pointer.startsWith('/')) return undefined;
  const segments = source.split('/').map(segment => segment.replace(/~1/g, '/').replace(/~0/g, '~'));
  return segments.some(segment => UNSAFE_POINTER_SEGMENTS.has(segment)) ? undefined : segments;
}

function readSegments(root: unknown, segments: readonly string[]): unknown {
  let current = root;
  for (const segment of segments) {
    if (Array.isArray(current)) {
      if (!/^(0|[1-9][0-9]*)$/.test(segment)) return undefined;
      current = current[Number(segment)];
      continue;
    }
    if (!isRecord(current) || !Object.prototype.hasOwnProperty.call(current, segment)) return undefined;
    current = current[segment];
  }
  return current;
}

/** 解析标准绝对指针（/开头）和 A2UI 模板相对指针。 */
export function readA2uiPointer(
  pointer: string,
  dataModel: unknown,
  scope: A2uiEvaluationScope,
): unknown {
  const absolute = pointer.startsWith('/');
  const segments = pointerSegments(pointer, !absolute);
  return segments ? readSegments(absolute ? dataModel : scope.value, segments) : undefined;
}

/** 集合字段选择器，永远相对于单个 source item。 */
export function readA2uiItemPointer(item: unknown, pointer: unknown): unknown {
  if (typeof pointer !== 'string') return undefined;
  const segments = pointerSegments(pointer, true);
  return segments ? readSegments(item, segments) : undefined;
}

function writableChild(container: unknown, segment: string, create: boolean): unknown {
  if (Array.isArray(container)) {
    if (!/^(0|[1-9][0-9]*)$/.test(segment)) return undefined;
    const index = Number(segment);
    if (container[index] === undefined && create) container[index] = {};
    return container[index];
  }
  if (!isRecord(container)) return undefined;
  if (!Object.prototype.hasOwnProperty.call(container, segment) && create) container[segment] = {};
  return container[segment];
}

/** 写入 DataBinding.path 指向的字段（用于 TextField/CheckBox/ChoicePicker 等表单组件）。 */
export function writeA2uiBinding(
  binding: unknown,
  value: unknown,
  dataModel: unknown,
  scope: A2uiEvaluationScope,
): boolean {
  if (!isRecord(binding) || typeof binding.path !== 'string') return false;
  const absolute = binding.path.startsWith('/');
  const segments = pointerSegments(binding.path, !absolute);
  if (!segments?.length) return false;
  let target = absolute ? dataModel : scope.value;
  for (const segment of segments.slice(0, -1)) {
    target = writableChild(target, segment, true);
    if (target === undefined) return false;
  }
  const last = segments[segments.length - 1]!;
  if (Array.isArray(target)) {
    if (!/^(0|[1-9][0-9]*)$/.test(last)) return false;
    target[Number(last)] = value;
    return true;
  }
  if (!isRecord(target)) return false;
  target[last] = value;
  return true;
}

export function isWritableA2uiBinding(binding: unknown): boolean {
  if (!isRecord(binding) || typeof binding.path !== 'string') return false;
  const absolute = binding.path.startsWith('/');
  const segments = pointerSegments(binding.path, !absolute);
  return Boolean(segments?.length && segments.every(segment => segment.length > 0));
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function stringValue(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

/** 状态字符串 → tone 推断（pass/ready/succeeded → positive 等）。 */
function statusTone(value: unknown): string {
  const status = stringValue(value).trim().toLowerCase();
  if (['passed', 'ready', 'resolved', 'verified', 'succeeded', 'success', 'complete', 'completed', 'healthy'].includes(status)) {
    return 'positive';
  }
  if (['failed', 'blocked', 'critical', 'error', 'denied', 'cancelled', 'unhealthy'].includes(status)) {
    return 'critical';
  }
  if (['warning', 'warn', 'pending', 'paused', 'degraded', 'needs_grant'].includes(status)) return 'warning';
  if (['running', 'active', 'authorized', 'info', 'submitting'].includes(status)) return 'info';
  return 'neutral';
}

function formatDate(value: unknown, format: unknown, locale: string): string {
  const date = value instanceof Date ? value : new Date(value as string | number);
  if (Number.isNaN(date.getTime())) return '';
  const pattern = stringValue(format);
  if (!pattern) return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
  const options: Intl.DateTimeFormatOptions = {};
  if (/y/.test(pattern)) options.year = pattern.includes('yy') && !pattern.includes('yyyy') ? '2-digit' : 'numeric';
  if (/M/.test(pattern)) {
    options.month = pattern.includes('MMMM') ? 'long' : pattern.includes('MMM') ? 'short' : pattern.includes('MM') ? '2-digit' : 'numeric';
  }
  if (/d/.test(pattern)) options.day = pattern.includes('dd') ? '2-digit' : 'numeric';
  if (/E/.test(pattern)) options.weekday = pattern.includes('EEEE') ? 'long' : 'short';
  if (/[Hh]/.test(pattern)) {
    options.hour = pattern.includes('HH') || pattern.includes('hh') ? '2-digit' : 'numeric';
    options.hour12 = /h/.test(pattern);
  }
  if (/m/.test(pattern)) options.minute = '2-digit';
  if (/s/.test(pattern)) options.second = '2-digit';
  return new Intl.DateTimeFormat(locale, options).format(date);
}

function interpolateFormatString(
  template: string,
  runtime: A2uiRuntime,
  scope: A2uiEvaluationScope,
): string {
  return template.replace(/\\?\$\{([^{}]+)\}/g, (match, expression: string) => {
    if (match.startsWith('\\')) return match.slice(1);
    const pointer = expression.trim();
    if (!pointer || /[()'"`,]/.test(pointer)) return '';
    return stringValue(readA2uiPointer(pointer, runtime.dataModel.value, scope));
  });
}

/** 闭包纯函数求值器：formatString / formatNumber / formatCurrency / formatDate / pluralize / and|or|not / required / length / numeric / email / @index。 */
function evaluateFunction(
  call: string,
  rawArgs: unknown,
  runtime: A2uiRuntime,
  scope: A2uiEvaluationScope,
): unknown {
  const source = isRecord(rawArgs) ? rawArgs : {};
  const args = Object.fromEntries(Object.entries(source).map(([key, value]) => [
    key,
    evaluateA2uiValue(value, runtime, scope),
  ]));
  if (call === 'formatString') {
    return interpolateFormatString(stringValue(args.value), runtime, scope);
  }
  if (call === 'formatNumber') {
    const value = numberValue(args.value);
    if (value === undefined) return '';
    const requestedDecimals = numberValue(args.decimals);
    const decimals = requestedDecimals === undefined
      ? undefined
      : Math.max(0, Math.min(6, Math.round(requestedDecimals)));
    return new Intl.NumberFormat(runtime.locale, {
      useGrouping: args.grouping !== false,
      ...(decimals === undefined ? {} : {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      }),
    }).format(value);
  }
  if (call === 'formatCurrency') {
    const value = numberValue(args.value);
    const currency = stringValue(args.currency);
    if (value === undefined || !/^[A-Z]{3}$/.test(currency)) return '';
    const requestedDecimals = numberValue(args.decimals);
    const decimals = requestedDecimals === undefined
      ? undefined
      : Math.max(0, Math.min(6, Math.round(requestedDecimals)));
    return new Intl.NumberFormat(runtime.locale, {
      style: 'currency',
      currency,
      useGrouping: args.grouping !== false,
      ...(decimals === undefined ? {} : {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      }),
    }).format(value);
  }
  if (call === 'formatDate') return formatDate(args.value, args.format, runtime.locale);
  if (call === '@index') return (scope.index ?? 0) + (numberValue(args.offset) ?? 0);
  if (call === 'and') return Array.isArray(args.values) && args.values.every(Boolean);
  if (call === 'or') return Array.isArray(args.values) && args.values.some(Boolean);
  if (call === 'not') return !Boolean(args.value);
  if (call === 'required') {
    if (args.value === undefined || args.value === null) return false;
    if (typeof args.value === 'string' || Array.isArray(args.value)) return args.value.length > 0;
    return true;
  }
  if (call === 'length') {
    const length = stringValue(args.value).length;
    const min = numberValue(args.min);
    const max = numberValue(args.max);
    return (min === undefined || length >= min) && (max === undefined || length <= max);
  }
  if (call === 'numeric') {
    const value = numberValue(args.value);
    const min = numberValue(args.min);
    const max = numberValue(args.max);
    return value !== undefined && (min === undefined || value >= min) && (max === undefined || value <= max);
  }
  if (call === 'email') return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(stringValue(args.value));
  if (call === 'pluralize') {
    const value = numberValue(args.value);
    if (value === undefined) return stringValue(args.other);
    const category = new Intl.PluralRules(runtime.locale).select(value);
    return stringValue(args[category] ?? args.other);
  }
  return undefined;
}

/** 求值：DataBinding 读 path，FunctionCall 调 evaluateFunction，其他原样返回。 */
export function evaluateA2uiValue(
  value: unknown,
  runtime: A2uiRuntime,
  scope: A2uiEvaluationScope,
): unknown {
  if (Array.isArray(value)) return value.map(item => evaluateA2uiValue(item, runtime, scope));
  if (!isRecord(value)) return value;
  if (typeof value.path === 'string') return readA2uiPointer(value.path, runtime.dataModel.value, scope);
  if (typeof value.call === 'string') return evaluateFunction(value.call, value.args, runtime, scope);
  return undefined;
}

export function a2uiText(value: unknown): string {
  return stringValue(value);
}

export function a2uiNumber(value: unknown): number | undefined {
  return numberValue(value);
}

export function a2uiTone(value: unknown): string {
  const tone = stringValue(value);
  return TONES.has(tone) ? tone : statusTone(tone);
}

/** 校验 surface 并返回强类型结果，校验失败抛错。 */
// 各组件允许的字段白名单（对照 schemas.ts componentSchema，additionalProperties: false）
const COMPONENT_FIELDS_WHITELIST: Record<string, Set<string>> = {
  Text: new Set(['text', 'variant']),
  Image: new Set(['url', 'description', 'fit', 'variant']),
  Icon: new Set(['name']),
  Video: new Set(['url', 'description']),
  AudioPlayer: new Set(['url', 'description']),
  Row: new Set(['children', 'justify', 'align']),
  Column: new Set(['children', 'justify', 'align']),
  List: new Set(['children', 'direction', 'align']),
  Card: new Set(['child']),
  Tabs: new Set(['tabs']),
  Modal: new Set(['trigger', 'content']),
  Divider: new Set(['axis']),
  Button: new Set(['child', 'variant', 'action']),
  TextField: new Set(['label', 'value', 'placeholder', 'variant']),
  CheckBox: new Set(['label', 'value']),
  ChoicePicker: new Set(['label', 'variant', 'options', 'value', 'displayStyle', 'filterable']),
  Slider: new Set(['label', 'value', 'min', 'max', 'steps']),
  DateTimeInput: new Set(['label', 'value', 'enableDate', 'enableTime', 'min', 'max']),
  // Ao 前缀组件（AgentOps 自定义 catalog，与 schemas.ts componentSchema 定义对齐）
  AoGrid: new Set(['children', 'columns', 'gap', 'align']),
  AoGridItem: new Set(['child', 'span']),
  AoSection: new Set(['title', 'children', 'tone']),
  AoMetric: new Set(['label', 'value', 'unit', 'tone']),
  AoStatusBadge: new Set(['text', 'tone']),
  AoProgress: new Set(['label', 'value', 'tone']),
  AoStep: new Set(['index', 'label', 'detail', 'tone', 'child']),
  AoList: new Set(['source', 'maxItems', 'itemTitlePath', 'itemDetailPath', 'itemBadgePath', 'itemStatusPath']),
  AoTable: new Set(['source', 'maxItems', 'columns']),
  AoTimeline: new Set(['source', 'maxItems', 'itemTitlePath', 'itemDetailPath', 'itemTimePath', 'itemStatusPath']),
  AoBarChart: new Set(['source', 'maxItems', 'itemLabelPath', 'itemValuePath', 'itemTonePath']),
  AoLineChart: new Set(['source', 'maxItems', 'xAxis', 'seriesNamePath', 'seriesDataPath', 'unit']),
  AoPieChart: new Set(['source', 'maxItems', 'itemLabelPath', 'itemValuePath', 'unit']),
  AoDag: new Set(['source', 'maxItems', 'itemIdPath', 'itemLabelPath', 'itemDetailPath', 'itemStatusPath', 'itemProgressPath', 'itemDependsOnPath']),
  AoDisclosure: new Set(['title', 'children', 'open']),
  AoLink: new Set(['label', 'url', 'description']),
  AoArtifact: new Set(['kind', 'uri', 'title', 'description', 'alt', 'layout']),
  AoIf: new Set(['condition', 'children']),
};

const KNOWN_COMPONENT_NAMES = new Set(Object.keys(COMPONENT_FIELDS_WHITELIST));

function coerceNumeric(value: unknown): unknown {
  if (typeof value === 'string' && value !== '') {
    if (value.includes('.')) {
      const f = Number.parseFloat(value);
      if (!Number.isNaN(f)) return f;
    } else {
      const n = Number.parseInt(value, 10);
      if (!Number.isNaN(n)) return n;
    }
  }
  return value;
}

/**
 * 规范化 A2UI components，兼容 LLM 常见格式偏差。
 * 与后端 orchestrator/actor_visual_profile.py normalize_components 对齐。
 */
function _normalizeSingleComponent(
  comp: Record<string, unknown>,
  i: number,
  prefix: string,
): [Record<string, unknown>, Record<string, unknown>[]] {
  let compType = (comp.component as string) || (comp.type as string);
  // Legacy 前缀归一化：历史 session 数据中偶发的旧版组件名前缀 → Ao（当前命名）。
  // 避免 schema 校验失败。
  if (compType && compType.startsWith('Hr')) {
    const aoType = 'Ao' + compType.slice(2);
    if (KNOWN_COMPONENT_NAMES.has(aoType)) {
      compType = aoType;
    }
  }
  if (!compType || !KNOWN_COMPONENT_NAMES.has(compType)) {
    return [{}, []];
  }
  const props = (comp.props && typeof comp.props === 'object' ? comp.props : {}) as Record<string, unknown>;
  const merged: Record<string, unknown> = { ...props };
  for (const [k, v] of Object.entries(comp)) {
    if (k !== 'type' && k !== 'component' && k !== 'props' && k !== 'id') merged[k] = v;
  }
  const compId = (comp.id as string) || `${compType.toLowerCase()}-${prefix}${i}`;
  const normalized: Record<string, unknown> = { id: compId, component: compType };
  const allowed = COMPONENT_FIELDS_WHITELIST[compType] ?? new Set<string>();

  // 先处理 children：如果是对象数组（LLM 原始格式），递归规范化并展平
  const extraChildren: Record<string, unknown>[] = [];
  if (Array.isArray(merged.children)) {
    const rawChildren = merged.children;
    if (rawChildren.length > 0 && rawChildren.every(c => c && typeof c === 'object')) {
      // children 是对象数组 → 递归规范化，展平到顶层，用 ID 引用
      const childIds: string[] = [];
      rawChildren.forEach((child, ci) => {
        if (!child || typeof child !== 'object') return;
        const [childNorm, childGrand] = _normalizeSingleComponent(
          child as Record<string, unknown>,
          ci,
          `${prefix}${i}_`,
        );
        if (childNorm.id) {
          extraChildren.push(childNorm);
          extraChildren.push(...childGrand);
          childIds.push(childNorm.id as string);
        }
      });
      normalized.children = childIds;
    } else if (rawChildren.every(c => typeof c === 'string')) {
      normalized.children = rawChildren as string[];
    } else {
      normalized.children = (rawChildren as unknown[]).filter(c => typeof c === 'string');
    }
  }

  // 处理其他白名单字段
  for (const [k, v] of Object.entries(merged)) {
    if (k === 'children') continue; // 已单独处理
    if (allowed.has(k)) {
      if (k === 'value' || k === 'min' || k === 'max' || k === 'steps' || k === 'span') {
        normalized[k] = coerceNumeric(v);
      } else {
        normalized[k] = v;
      }
    }
  }

  // 组件特定补全：确保 required 字段存在（旧版前缀已在上方归一化为 Ao，此处只判断 Ao）
  if (compType === 'AoSection' && !('children' in normalized)) {
    normalized.children = [];
  } else if (compType === 'AoStatusBadge') {
    if (!('text' in normalized) && 'label' in merged) {
      normalized.text = merged.label;
    } else if (!('text' in normalized)) {
      normalized.text = '';
    }
  } else if (compType === 'Text') {
    if (!('text' in normalized)) normalized.text = '';
  } else if (compType === 'AoProgress') {
    if (!('value' in normalized)) normalized.value = 0;
  } else if (compType === 'AoMetric') {
    if (!('label' in normalized)) normalized.label = '';
    if (!('value' in normalized)) normalized.value = 0;
  } else if (compType === 'AoStep') {
    if (!('index' in normalized)) normalized.index = i;
    if (!('label' in normalized)) {
      normalized.label = (merged.title as string) || (merged.text as string) || '';
    }
    if (!('child' in normalized)) {
      const childId = `text-${prefix}${i}_child`;
      extraChildren.push({ id: childId, component: 'Text', text: normalized.label });
      normalized.child = childId;
    }
  } else if (['AoList', 'AoTable', 'AoTimeline', 'AoBarChart', 'AoLineChart', 'AoPieChart', 'AoDag'].includes(compType)) {
    if (!('source' in normalized)) {
      let itemsData = (merged.items as unknown) || (merged.data as unknown) || [];
      // 容错：LLM（如 MiniMax-M3）可能生成 {"item": [...]} 或嵌套 {"item":{"item":[...]}} 包裹格式
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
      if (itemsData && typeof itemsData === 'object' && !Array.isArray(itemsData)) {
        itemsData = extractArrayDeep(itemsData) ?? [];
      }
      if (Array.isArray(itemsData) && itemsData.length > 0) {
        normalized.source = { path: `/_inline_${compType}_${prefix}${i}` };
        if (!('itemTitlePath' in normalized)) normalized.itemTitlePath = '/';
        // 内联数据由调用方注入到 data_model（通过 _inline_data 临时字段）
        (normalized as Record<string, unknown>)._inline_data = itemsData;
      } else {
        // 无内联数据时注入空数组 dummy source，使 schema 校验通过（source 是必填字段）
        normalized.source = { path: `/_inline_${compType}_${prefix}${i}` };
        if (!('itemTitlePath' in normalized)) normalized.itemTitlePath = '/';
        (normalized as Record<string, unknown>)._inline_data = [];
      }
    }
  }

  return [normalized, extraChildren];
}

function normalizeA2uiComponents(components: unknown): unknown[] {
  if (!Array.isArray(components)) return [];
  const result: Record<string, unknown>[] = [];
  components.forEach((comp, i) => {
    if (!comp || typeof comp !== 'object') return;
    const [normalized, extraChildren] = _normalizeSingleComponent(
      comp as Record<string, unknown>,
      i,
      '',
    );
    if (normalized.id) {
      result.push(normalized);
      result.push(...extraChildren);
    }
  });
  // 语义校验要求：surface 必须有且仅有一个 id="root" 的组件作为渲染入口。
  // LLM 常生成扁平组件列表（无 root 容器），需自动补一个 Column 根容器包裹所有组件。
  if (result.length > 0 && !result.some(c => c.id === 'root')) {
    const childIds = result.map(c => c.id as string);
    const root: Record<string, unknown> = {
      id: 'root',
      component: 'Column',
      children: childIds,
    };
    return [root, ...result];
  }
  return result;
}

export function validateA2uiSurfaceForNode(
  value: unknown,
  node: GenerativeUiStoredNodeV1,
): AgentopsA2uiSurfaceV1 {
  // 容错：components 为空数组时注入占位组件（避免 minItems:1 校验失败）
  if (value && typeof value === 'object' && Array.isArray((value as Record<string, unknown>).components)) {
    const components = (value as Record<string, unknown>).components as unknown[];
    if (components.length === 0) {
      (value as Record<string, unknown>).components = [{
        id: 'placeholder-empty',
        component: 'Text',
        properties: { text: '暂无展示内容', variant: 'caption' },
      }];
    }
  }
  const validation = validateAgentopsA2uiSurface(value, {
    action_ids: new Set((node.actions ?? []).map(action => action.id)),
    data_model: node.content,
  });
  if (validation.valid && validation.value) {
    return validation.value;
  }
  // 诊断日志：记录第一次校验失败的具体错误
  console.warn('[A2UI] first validation failed, will try normalize. errors=', validation.errors,
    'components=', (value as Record<string, unknown>)?.components);
  // 校验失败时尝试 normalize（兼容 LLM 常见格式偏差：type→component、props 嵌套、缺 id）
  if (value && typeof value === 'object' && Array.isArray((value as Record<string, unknown>).components)) {
    const source = value as Record<string, unknown>;
    // 容错：components 为空数组时注入占位组件（避免 minItems:1 校验失败导致红色错误卡片）
    if ((source.components as unknown[]).length === 0) {
      source.components = [{
        id: 'placeholder-empty',
        component: 'Text',
        properties: { text: '暂无展示内容', variant: 'caption' },
      }];
    }
    // 重建干净的 surface 对象（只保留 schema 允许的字段：version/catalogId/components/surfaceProperties）
    // 避免 additionalProperties: false 拒绝多余的 data_model/surface_id/view_id/phase 等字段
    const normalized: Record<string, unknown> = {
      version: source.version ?? 'v1.0',
      catalogId: source.catalogId ?? source.catalog_id ?? 'https://agentops.dev/a2ui/catalogs/core/v1',
      components: normalizeA2uiComponents(source.components),
    };
    // 仅当 surfaceProperties 非空且为对象时才加入（null/undefined 跳过）
    const sp = source.surfaceProperties ?? source.surface_properties;
    if (sp && typeof sp === 'object' && !Array.isArray(sp)) {
      normalized.surfaceProperties = sp;
    }
    // 把 AoList/AoTable 等的内联数据注入到 data_model（合并 node.content + inline data）
    // 注意：data_model 不能放到 surface 对象里（schema additionalProperties: false）
    // 必须通过 options.data_model 传给校验器
    const mergedDataModel: Record<string, unknown> = {};
    if (node.content && typeof node.content === 'object') {
      Object.assign(mergedDataModel, node.content);
    }
    // 容错：历史 session 的 data_model 中 _inline_* 值可能是 dict（如 {"item":[...]} 或嵌套 {"item":{"item":[...]}}）而非数组
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
    for (const [dk, dv] of Object.entries(mergedDataModel)) {
      if (dk.startsWith('_inline_') && dv && typeof dv === 'object' && !Array.isArray(dv)) {
        const extracted = extractArrayDeep(dv);
        if (extracted) mergedDataModel[dk] = extracted;
      }
    }
    for (const comp of normalized.components as Record<string, unknown>[]) {
      const inlineData = comp._inline_data;
      if (inlineData !== undefined) {
        delete comp._inline_data;
        const compSource = comp.source as Record<string, unknown> | undefined;
        const path = (compSource?.path as string ?? '').replace(/^\//, '');
        if (path) mergedDataModel[path] = inlineData;
      }
    }
    const retry = validateAgentopsA2uiSurface(normalized, {
      action_ids: new Set((node.actions ?? []).map(action => action.id)),
      data_model: mergedDataModel,
    });
    if (retry.valid && retry.value) {
      const comps = (normalized as Record<string, unknown>).components;
      console.info('[A2UI] normalize retry succeeded, components=', Array.isArray(comps) ? comps.length : 'N/A');
      return retry.value;
    }
    // 诊断日志：retry 也失败
    console.warn('[A2UI] normalize retry also failed. errors=', retry.errors,
      'normalized components=', normalized.components);
    // retry 也失败：用 retry 的错误（更贴近 normalized 后的真实问题）
    throw new Error(`Generated A2UI surface is invalid: ${JSON.stringify(retry.errors ?? validation.errors)}`);
  }
  throw new Error(`Generated A2UI surface is invalid: ${JSON.stringify(validation.errors)}`);
}

/** 把 surface.components 数组转为 id → component 的 Map，便于 A2uiNode 查找。 */
export function indexA2uiSurface(surface: AgentopsA2uiSurfaceV1): ReadonlyMap<string, A2uiComponentV1> {
  return new Map(surface.components.map(component => [component.id, component]));
}

export function a2uiActionNames(surface: AgentopsA2uiSurfaceV1): ReadonlySet<string> {
  return scanA2uiActionNames(surface);
}

/** 预渲染扫描：收集所有 Button.action.event.name，避免补充 action 列表闪烁。 */
export function scanA2uiActionNames(value: unknown): ReadonlySet<string> {
  const names = new Set<string>();
  if (!isRecord(value) || !Array.isArray(value.components)) return names;
  for (const component of value.components) {
    if (!isRecord(component) || component.component !== 'Button' || !isRecord(component.action) || !isRecord(component.action.event)) continue;
    const name = component.action.event.name;
    if (typeof name === 'string' && name) names.add(name);
  }
  return names;
}

export function isA2uiRecord(value: unknown): value is Record<string, unknown> {
  return isRecord(value);
}
