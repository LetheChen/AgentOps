// 解包工具：minimax Responses API / 部分后端会把 array 字段序列化为 {item: [...]} 包装对象。
// 前端在所有需要 .map / .length / Array methods 的地方先过一遍 unwrapArray，
// 避免 React 渲染时 "items.map is not a function" 崩溃。

export function unwrapArray<T = unknown>(v: unknown): T[] {
  if (Array.isArray(v)) return v as T[];
  if (v && typeof v === 'object' && Array.isArray((v as { item?: unknown }).item)) {
    return (v as { item: T[] }).item;
  }
  return [];
}

/**
 * 把任意值规整化为 React 安全的字符串。
 *
 * minimax Responses API 序列化 array 时包装为 {item: [...]}，
 * 但对于"本应是 string 的字段"也会包装为对象，例如：
 *   - task_draft.task = {label, description, status}  (checklist item 结构)
 *   - task_draft.actions[].label = "..."
 *   - form.prompt = {label, description}
 *   - memo.title = {label, description, status}
 *
 * 如果直接渲染非 string/non-number 值，React 会抛
 * "Objects are not valid as a React child"，整个 widget-panel 树卸载黑屏。
 *
 * 该函数在 WidgetRenderer 入口递归处理 props，
 * 保证 widget 子组件拿到的字段都是 string 或已知结构（array/object）。
 */
export function normalizeToString(v: unknown): string {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'bigint') {
    return String(v);
  }
  if (Array.isArray(v)) {
    return v.map(normalizeToString).filter(Boolean).join('\n');
  }
  if (typeof v === 'object') {
    const obj = v as Record<string, unknown>;
    // minimax 包装：先 unwrap {item: [...]} / {items: [...]} / {value: ...} / {content: ...}
    const inner = obj.item ?? obj.items ?? obj.value ?? obj.content;
    if (inner !== undefined && inner !== v) {
      return normalizeToString(inner);
    }
    // 优先字段（LLM 常见输出 schema）
    const text = obj.description ?? obj.label ?? obj.text ?? obj.title
      ?? obj.name ?? obj.summary ?? obj.body;
    if (typeof text === 'string') return text;
    // 兜底：JSON 序列化
    try {
      return JSON.stringify(obj);
    } catch {
      return '';
    }
  }
  return String(v);
}

/**
 * 递归规范化 widget props：
 * - array 字段：unwrap 为真实 array
 * - string 字段：如果是 object（如 minimax 包装的 {label, description, status}），
 *   通过 normalizeToString 提取可读文本
 *
 * 注意：不会修改已知结构化字段（如 widget_id / type），仅按 string/array 区分。
 * 数字/布尔/null 保持原样。
 */
export function normalizeWidgetProps(props: Record<string, unknown> | undefined): Record<string, unknown> {
  if (!props || typeof props !== 'object') return props ?? {};
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(props)) {
    if (v == null) {
      out[k] = v;
    } else if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      out[k] = v;
    } else if (Array.isArray(v)) {
      // 已经是 array，直接保留（widget 子组件用 unwrapArray 再处理 minimax {item:[...]} 包装）
      out[k] = v;
    } else if (typeof v === 'object') {
      const obj = v as Record<string, unknown>;
      // minimax {item: [...]} 包装：直接 unwrap 为 array
      if (Array.isArray(obj.item)) {
        out[k] = obj.item;
      } else {
        // 其他 object（如 task = {label, description, status}）：归一化为 string
        out[k] = normalizeToString(v);
      }
    } else {
      out[k] = v;
    }
  }
  return out;
}
