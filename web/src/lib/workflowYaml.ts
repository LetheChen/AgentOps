/**
 * 工作流 YAML 序列化器 + 编辑器类型定义。
 *
 * 不依赖 js-yaml，使用聚焦于 workflow YAML 结构的轻量序列化器。
 * 加载时从后端 API 的结构化数据初始化；保存时序列化为 YAML 字符串。
 */

// ── 类型定义 ──

export type HarnessType =
  | 'opencode' | 'claude_code' | 'codex' | 'kimi'
  | 'http' | 'deterministic' | 'local_llm';

export const HARNESS_OPTIONS: { value: HarnessType; label: string }[] = [
  { value: 'opencode', label: 'OpenCode' },
  { value: 'local_llm', label: 'Local LLM' },
  { value: 'deterministic', label: 'Deterministic' },
  { value: 'codex', label: 'Codex' },
  { value: 'claude_code', label: 'Claude Code' },
  { value: 'kimi', label: 'Kimi' },
  { value: 'http', label: 'HTTP' },
];

export const NODE_TYPE_OPTIONS = [
  { value: 'agent', label: 'Agent' },
  { value: 'command', label: 'Command (CLI)' },
  { value: 'await_command', label: 'Await Command' },
  { value: 'while', label: 'While (Loop)' },
  { value: 'parallel_branch', label: 'Parallel Branch' },
  { value: 'gateway', label: 'Gateway' },
] as const;

export interface EditorNode {
  id: string;
  name: string;
  type: 'agent' | 'command' | 'await_command' | 'while' | 'parallel_branch' | 'gateway';
  agent: string | null;
  harness: HarnessType;
  /** 模型配置：保留 provider/model 两个字段；空字符串表示未指定（序列化时省略）。 */
  model_provider: string;
  model_id: string;
  after: string[];
  inputs: string[];
  outputs: Record<string, string | string[]>;
  domain: string | null;
  business_role: string | null;
  role_prompt: string | null;
  skip_if: string | null;
  timeout_seconds: number | null;
  /** 编辑器未管理的节点字段（command_config / gateway_kind / inline_agent 等），
   *  保存时原样透传，避免可视化编辑丢字段导致后端 400。
   *  收集规则：apiToEditorState 把 ApiNode 里不在已知字段列表的项搬进来。 */
  rawFields: Record<string, unknown>;
}

export interface WorkflowInput {
  name: string;
  type: string;
  required: boolean;
  default?: unknown;
  description?: string;
}

export interface EditorWidget {
  id: string;
  type: string;
  title: string;
  emit_on_node: string;
  emit_on_event: string;
  props: Record<string, unknown>;
}

export interface EditorWorkflow {
  workflow_id: string;
  name: string;
  version: number;
  description: string;
  inputs: WorkflowInput[];
  nodes: EditorNode[];
  widgets: EditorWidget[];
  /** 编辑器未管理的顶层字段（workspace, permissions 等），保存时原样输出 */
  rawExtras: Record<string, unknown>;
}

// ── API 响应 → 编辑器状态 ──

interface ApiInlineAgent {
  harness?: string;
  model?: string | { provider?: string; id?: string } | null;
  domain?: string | null;
  role_prompt?: string;
  allowed_tools?: string[];
  denied_tools?: string[];
  timeout_seconds?: number | null;
}

interface ApiNode {
  id: string;
  name: string;
  type: string;
  agent: string | null;
  harness: string;
  inline_agent?: ApiInlineAgent | null;
  after: string[];
  inputs: string[];
  outputs: Record<string, string | string[]>;
  model: string | null;
  domain: string | null;
  business_role: string | null;
  role_prompt: string | null;
  skip_if: string | null;
  timeout_seconds: number | null;
}

interface ApiWidget {
  id: string;
  type: string;
  title: string;
  emit_on_node: string;
  emit_on_event: string;
  props: Record<string, unknown>;
}

interface ApiWorkflowDetail {
  workflow_id: string;
  name: string;
  description: string;
  version: number;
  inputs: WorkflowInput[];
  nodes: ApiNode[];
  widgets: ApiWidget[];
  raw?: Record<string, unknown>;
}

/** 从后端 API 详情响应构建编辑器状态 */
export function apiToEditorState(detail: Record<string, unknown>): EditorWorkflow {
  const d = detail as unknown as ApiWorkflowDetail;
  const raw = d.raw ?? {};

  // 提取编辑器未管理的顶层字段
  const managedKeys = new Set(['workflow_id', 'name', 'version', 'description', 'inputs', 'nodes', 'widgets', 'widget_inputs', 'id']);
  const rawExtras: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (!managedKeys.has(k)) rawExtras[k] = v;
  }

  // 节点级已知字段（被编辑器管理的），其它字段（如 command_config / gateway_kind /
  // while_config / inline_agent / branches / condition / runtime_placement）进 rawFields 透传
  // 避免可视化编辑保存时丢字段导致后端 400
  const NODE_KNOWN_KEYS = new Set([
    'id', 'name', 'type', 'agent', 'harness', 'inline_agent',
    'after', 'inputs', 'outputs', 'model', 'domain',
    'business_role', 'role_prompt', 'skip_if', 'timeout_seconds',
  ]);

  const nodes: EditorNode[] = (d.nodes ?? []).map((n) => {
    // inline_agent 优先：harness / model / domain / role_prompt 均以 inline_agent 为准
    // （与 loader.py 语义一致：配了 inline_agent 则顶层同名字段被忽略）
    const ia = n.inline_agent ?? null;
    const harnessRaw = ia?.harness ?? n.harness;
    const modelRaw = (ia?.model as string | { provider?: string; id?: string } | null) ?? n.model;
    const domainRaw = ia?.domain ?? n.domain;
    const rolePromptRaw = ia?.role_prompt ?? n.role_prompt;

    // 把后端可能返回的字符串 "provider/model" 或对象 {provider,id} 拆成两个字段
    let modelProvider = '';
    let modelId = '';
    if (typeof modelRaw === 'string' && modelRaw.includes('/')) {
      const [p, ...rest] = modelRaw.split('/');
      modelProvider = p ?? '';
      modelId = rest.join('/');
    } else if (typeof modelRaw === 'object' && modelRaw) {
      modelProvider = (modelRaw as { provider?: string }).provider ?? '';
      modelId = (modelRaw as { id?: string }).id ?? '';
    }

    // 收集未管理字段进 rawFields（透传给后端保存）
    // 注意：n 里的 inline_agent 整体作为 rawFields 的一个 key 透传（apiToEditorState 不展开它）
    const rawFields: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(n as unknown as Record<string, unknown>)) {
      if (!NODE_KNOWN_KEYS.has(k)) rawFields[k] = v;
    }

    return {
      id: n.id,
      name: n.name,
      type: (n.type as EditorNode['type']) || 'agent',
      agent: n.agent ?? null,
      harness: (harnessRaw as HarnessType) || 'opencode',
      model_provider: modelProvider,
      model_id: modelId,
      after: n.after ?? [],
      inputs: n.inputs ?? [],
      outputs: n.outputs ?? {},
      domain: domainRaw ?? null,
      business_role: n.business_role ?? null,
      role_prompt: rolePromptRaw ?? null,
      skip_if: n.skip_if ?? null,
      timeout_seconds: n.timeout_seconds ?? null,
      rawFields,
    };
  });

  const widgets: EditorWidget[] = (d.widgets ?? []).map((w) => ({
    id: w.id,
    type: w.type,
    title: w.title,
    emit_on_node: w.emit_on_node ?? '',
    emit_on_event: w.emit_on_event ?? 'node.completed',
    props: w.props ?? {},
  }));

  return {
    workflow_id: d.workflow_id ?? '',
    name: d.name ?? '',
    version: d.version ?? 1.0,
    description: d.description ?? '',
    inputs: d.inputs ?? [],
    nodes,
    widgets,
    rawExtras,
  };
}

// ── YAML 序列化 ──

/** 转义字符串为 YAML 标量值 */
function yamlScalar(val: string): string {
  if (val === '') return '""';
  // 含特殊字符 → 双引号
  if (/[:\[\]\{\},&*#?|<>=!%@`"'\\\n]/.test(val) || /^[\s\-?]/.test(val) || /\s$/.test(val)) {
    return `"${val.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`;
  }
  // 布尔/数字/null 关键字 → 双引号
  if (/^(true|false|null|yes|no|on|off|~)$/i.test(val)) return `"${val}"`;
  if (/^-?\d+(\.\d+)?$/.test(val)) return `"${val}"`;
  return val;
}

/** 递归序列化任意值为 YAML 行（返回多行字符串，不含末尾换行） */
function yamlDump(val: unknown, indent: number): string {
  const pad = ' '.repeat(indent);
  if (val === null || val === undefined) return 'null';
  if (typeof val === 'boolean') return val ? 'true' : 'false';
  if (typeof val === 'number') return String(val);
  if (typeof val === 'string') {
    // 多行字符串 → block scalar
    if (val.includes('\n')) {
      const lines = val.split('\n');
      return '|\n' + lines.map(l => `${pad}${l}`).join('\n');
    }
    return yamlScalar(val);
  }
  if (Array.isArray(val)) {
    if (val.length === 0) return '[]';
    return val.map(v => {
      const dumped = yamlDump(v, indent + 2);
      // 嵌套对象/多行 → 换行缩进
      if (dumped.includes('\n') && !dumped.startsWith('|')) {
        return `${pad}- ${dumped.replace(/\n/g, `\n${pad}  `)}`;
      }
      if (dumped.startsWith('|')) {
        return `${pad}- ${dumped}`;
      }
      return `${pad}- ${dumped}`;
    }).join('\n');
  }
  if (typeof val === 'object') {
    const entries = Object.entries(val as Record<string, unknown>);
    if (entries.length === 0) return '{}';
    return entries.map(([k, v]) => {
      const dumped = yamlDump(v, indent + 2);
      // block scalar → `key: |` 头部内联（内容行已带缩进）
      if (dumped.startsWith('|')) {
        return `${pad}${k}: ${dumped}`;
      }
      // 非空对象/数组 → 必须块形式（key 单独一行）。
      // 否则单行子对象会被内联成 `key: subkey: value` 的非法 YAML
      const isNonEmptyContainer =
        v !== null && typeof v === 'object' &&
        (Array.isArray(v) ? v.length > 0 : Object.keys(v as object).length > 0);
      if (isNonEmptyContainer || dumped.includes('\n')) {
        return `${pad}${k}:\n${dumped}`;
      }
      return `${pad}${k}: ${dumped}`;
    }).join('\n');
  }
  return String(val);
}

/** 序列化单个节点的 YAML 输出 */
function serializeNode(node: EditorNode, indent: number): string {
  const pad = ' '.repeat(indent);
  const lines: string[] = [];
  lines.push(`${pad}${node.id}:`);
  const f = indent + 2;
  const fp = ' '.repeat(f);

  lines.push(`${fp}name: ${yamlScalar(node.name)}`);
  lines.push(`${fp}type: ${node.type}`);
  if (node.agent) lines.push(`${fp}agent: ${node.agent}`);
  if (node.business_role) lines.push(`${fp}business_role: ${yamlScalar(node.business_role)}`);

  // role_prompt（block scalar）
  if (node.role_prompt) {
    if (node.role_prompt.includes('\n')) {
      lines.push(`${fp}role_prompt: |`);
      for (const line of node.role_prompt.split('\n')) {
        lines.push(`${fp}  ${line}`);
      }
    } else {
      lines.push(`${fp}role_prompt: ${yamlScalar(node.role_prompt)}`);
    }
  }

  lines.push(`${fp}harness: ${node.harness}`);
  if (node.model_provider && node.model_id) {
    lines.push(`${fp}model: ${node.model_provider}/${node.model_id}`);
  }

  // after
  if (node.after.length === 0) {
    lines.push(`${fp}after: []`);
  } else {
    lines.push(`${fp}after: [${node.after.join(', ')}]`);
  }

  // timeout_seconds
  if (node.timeout_seconds !== null && node.timeout_seconds !== undefined) {
    lines.push(`${fp}timeout_seconds: ${node.timeout_seconds}`);
  }

  // inputs
  if (node.inputs.length === 0) {
    lines.push(`${fp}inputs: []`);
  } else {
    lines.push(`${fp}inputs:`);
    for (const inp of node.inputs) {
      lines.push(`${fp}  - ${inp}`);
    }
  }

  // outputs
  if (Object.keys(node.outputs).length > 0) {
    lines.push(`${fp}outputs:`);
    for (const [port, target] of Object.entries(node.outputs)) {
      if (Array.isArray(target)) {
        lines.push(`${fp}  ${port}:`);
        lines.push(`${fp}    to:`);
        for (const t of target) {
          lines.push(`${fp}      - "${t}"`);
        }
      } else {
        lines.push(`${fp}  ${port}:`);
        lines.push(`${fp}    to: "${target}"`);
      }
    }
  }

  // skip_if
  if (node.skip_if) {
    lines.push(`${fp}skip_if: "${node.skip_if}"`);
  }

  // domain
  if (node.domain) {
    lines.push(`${fp}domain: ${node.domain}`);
  }

  // rawFields 透传（command_config / gateway_kind / while_config / inline_agent / 等
  // 编辑器未管理的节点字段）。v2026-08-28 D-060：避免可视化编辑保存丢字段导致后端 400。
  // 序列化策略：scalar 内联 / object 块形式，缩进 fp（node 子字段级别）
  if (node.rawFields && Object.keys(node.rawFields).length > 0) {
    for (const [key, value] of Object.entries(node.rawFields)) {
      if (value === null || value === undefined) {
        lines.push(`${fp}${key}: null`);
      } else if (typeof value === 'number' || typeof value === 'boolean') {
        lines.push(`${fp}${key}: ${value}`);
      } else if (typeof value === 'string') {
        lines.push(`${fp}${key}: ${yamlScalar(value)}`);
      } else if (Array.isArray(value) && value.length === 0) {
        lines.push(`${fp}${key}: []`);
      } else if (typeof value === 'object') {
        lines.push(`${fp}${key}:`);
        // ⚠️ yamlDump 内部 object entry 用 `pad` 渲染 key，对 rawExtras 调用方
        // （push `${key}:` 在 0 缩进，yamlDump 在 2 缩进渲染 value）OK，但对
        // rawFields 调用方（push `${fp}${key}:` 在 fp 缩进）需要 +2 才能让
        // 子 key 比父 key 多缩进 2。v2026-08-28 D-060 实测发现。
        lines.push(yamlDump(value, fp.length + 2));
      } else {
        lines.push(`${fp}${key}: ${yamlScalar(String(value))}`);
      }
    }
  }

  return lines.join('\n');
}

/** 序列化编辑器状态为完整 workflow YAML 字符串 */
export function serializeWorkflowYaml(wf: EditorWorkflow): string {
  const lines: string[] = [];

  // 顶层基础字段
  lines.push(`workflow_id: ${wf.workflow_id}`);
  lines.push(`name: ${yamlScalar(wf.name)}`);
  lines.push(`version: ${wf.version}`);

  // description（block scalar 如果多行）
  if (wf.description) {
    if (wf.description.includes('\n')) {
      lines.push('description: |');
      for (const line of wf.description.split('\n')) {
        lines.push(`  ${line}`);
      }
    } else {
      lines.push(`description: ${yamlScalar(wf.description)}`);
    }
  } else {
    lines.push('description: ""');
  }

  // 保留编辑器未管理的顶层字段（workspace, permissions, timeout_seconds 等）
  // ⚠️ 关键：scalar（number / boolean / string）走内联，complex（object / array）走块形式
  //    否则 yamlDump 返回的 bare scalar（无 pad）会被 YAML parser 当成顶级 key
  //    → 报 "could not find expected ':'"（v2026-08-28 D-058 真实踩坑）
  for (const [key, value] of Object.entries(wf.rawExtras)) {
    if (value === null || value === undefined) {
      lines.push(`${key}: null`);
    } else if (typeof value === 'number' || typeof value === 'boolean') {
      lines.push(`${key}: ${value}`);
    } else if (typeof value === 'string') {
      // 单行字符串 → 内联（多行才走块形式）
      lines.push(`${key}: ${yamlScalar(value)}`);
    } else if (Array.isArray(value) && value.length === 0) {
      lines.push(`${key}: []`);
    } else if (typeof value === 'object') {
      // object / 非空 array → 块形式（yamlDump 内部正确加缩进）
      lines.push(`${key}:`);
      lines.push(yamlDump(value, 2));
    } else {
      // 兜底：toString 后内联
      lines.push(`${key}: ${yamlScalar(String(value))}`);
    }
  }

  // inputs
  if (wf.inputs.length > 0) {
    lines.push('inputs:');
    for (const inp of wf.inputs) {
      lines.push(`  - name: ${inp.name}`);
      lines.push(`    type: ${inp.type}`);
      lines.push(`    required: ${inp.required}`);
      if (inp.default !== undefined && inp.default !== null && inp.default !== '') {
        lines.push(`    default: ${yamlScalar(String(inp.default))}`);
      }
      if (inp.description) {
        lines.push(`    description: ${yamlScalar(inp.description)}`);
      }
    }
  }

  // nodes
  lines.push('nodes:');
  for (const node of wf.nodes) {
    lines.push(serializeNode(node, 2));
  }

  // widgets
  if (wf.widgets.length > 0) {
    lines.push('widgets:');
    for (const w of wf.widgets) {
      lines.push(`  - id: ${w.id}`);
      lines.push(`    type: ${w.type}`);
      lines.push(`    title: ${yamlScalar(w.title)}`);
      lines.push('    emit_on:');
      lines.push(`      node: ${w.emit_on_node}`);
      lines.push(`      event: ${w.emit_on_event}`);
      if (w.props && Object.keys(w.props).length > 0) {
        lines.push('    props:');
        lines.push(yamlDump(w.props, 6));
      } else {
        lines.push('    props: {}');
      }
    }
  }

  return lines.join('\n') + '\n';
}
