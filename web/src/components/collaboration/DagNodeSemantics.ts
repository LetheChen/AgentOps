/**
 * DagNodeSemantics — v99.5 P0.7 DAG 编排可视化合并的语义层。
 *
 * 职责（纯函数，无副作用、无 React 依赖）：
 *   - 把 `node_type` / `gateway_kind` / `terminal_kind` / `status` 映射为：
 *       shape (8 种)、glyph (符号)、label (短文本)、tone (语义色调)
 *   - 11 类节点类型 → 8 种 shape 的固定映射（基于 AgentOps resolveDagRuntimeNodeSemantic 原理）
 *   - 提供 fallback 推导：当 node_type 未知时，按 status + business_role 启发式给出最接近的 shape
 *
 * 设计要点（详见 docs/reconstruction/agentops-v99-4layer-a2ui-refactor.md §3.8）：
 *   - **节点类型 → 形状固定映射**：不是 per-workflow 自定义（§8 反模式 #2 明确禁止）
 *   - **不绑定 node name**：避免命名差异导致渲染不一致
 *   - **后端 NodeType 枚举当前只有 7 个值**（AGENT/PARALLEL_BRANCH/GATEWAY/COMMAND/AWAIT_COMMAND/WHILE），
 *     但 spec 预留 terminal/join/quorum/foreach 4 类未来节点；semantic 必须前向兼容
 *   - **纯函数**：便于 React 在 useMemo 中调用，且测试可直接覆盖
 *
 * 不要在此文件中：
 *   - import React 或 DOM API（保持纯 TS）
 *   - 引入硬编码颜色值（颜色由 DagNodeShapeRegistry 用 CSS 变量解析）
 *   - 改变 GraphNode 类型定义（type 字段向后兼容，新增字段可选）
 */

// ── 类型定义 ──────────────────────────────────────────────────────────────

/** 8 种可视形状（与 v99.5 §3.8 映射表一致；parallelogram 为未来预留）。 */
export type DagShape =
  | 'circle'         // WORKER（agent）
  | 'triangle'       // FAN-OUT（parallel_branch）
  | 'diamond'        // GATE（gateway condition）
  | 'capsule'        // LOOP / WAIT / WHILE（gateway loop / await_command / while）
  | 'rounded_rect'   // COMMAND（command）
  | 'octagon'        // END / FAIL（terminal success/failure）
  | 'hexagon'        // JOIN / QUORUM（join / quorum）
  | 'parallelogram'; // FOREACH（foreach，未来预留）

/** 12 种语义色调（与 11 类节点类型 + unknown fallback 一一对应）。 */
export type DagTone =
  | 'worker'
  | 'fanout'
  | 'gate'
  | 'loop'
  | 'command'
  | 'wait'
  | 'while'
  | 'end'
  | 'fail'
  | 'join'
  | 'quorum'
  | 'foreach'
  | 'unknown';

/** 8 种节点状态（与 §3.8 status 视觉叠加表一致）。 */
export type DagStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'waiting_for_command'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'skipped';

export type DagGatewayKind = 'condition' | 'loop';
export type DagTerminalKind = 'success' | 'failure';

/** resolveDagNodeSemantic 的输入契约。 */
export interface DagNodeSemanticInput {
  /** 后端 DagEngine 推送的 node_type（可选；缺省时按 business_role 启发式）。 */
  node_type?: string;
  /** 仅当 node_type === 'gateway' 时有意义（condition / loop）。 */
  gateway_kind?: string;
  /** 仅当 node_type === 'terminal' 时有意义（success / failure）。 */
  terminal_kind?: string;
  /** 节点当前状态（决定 status overlay 渲染）。 */
  status?: string;
  /** 业务角色名（fallback 启发式用，例如 'executer' / 'reviewer'）。 */
  business_role?: string;
}

/** resolveDagNodeSemantic 的输出契约。 */
export interface DagNodeSemantic {
  shape: DagShape;
  glyph: string;
  label: string;
  tone: DagTone;
  status: DagStatus;
  gateway_kind?: DagGatewayKind;
  terminal_kind?: DagTerminalKind;
  /** 原始 node_type（用于调试与 legend 关联）。 */
  node_type: string;
}

// ── 状态规范化 ────────────────────────────────────────────────────────────

const VALID_STATUSES: ReadonlySet<string> = new Set([
  'pending',
  'ready',
  'running',
  'waiting_for_command',
  'completed',
  'failed',
  'cancelled',
  'skipped',
]);

function normalizeStatus(raw: string | undefined): DagStatus {
  if (raw && VALID_STATUSES.has(raw)) return raw as DagStatus;
  // 后端偶发输出 'success'（兼容）；按 completed 处理
  if (raw === 'success') return 'completed';
  return 'pending';
}

function normalizeGatewayKind(raw: string | undefined): DagGatewayKind {
  return raw === 'loop' ? 'loop' : 'condition';
}

function normalizeTerminalKind(raw: string | undefined): DagTerminalKind {
  return raw === 'failure' ? 'failure' : 'success';
}

// ── 节点类型 → (shape, glyph, label, tone) 主映射 ─────────────────────────

interface ShapeMapping {
  shape: DagShape;
  glyph: string;
  label: string;
  tone: DagTone;
}

/**
 * 11 类节点类型 → 8 种 shape 的固定映射（v99.5 §3.8 表）。
 * 主入口是 SHAPE_MAP[node_type]；gateway / terminal 需进一步根据 kind 分支。
 */
const SHAPE_MAP: Readonly<Record<string, ShapeMapping>> = {
  agent: { shape: 'circle', glyph: 'A', label: 'WORKER', tone: 'worker' },
  parallel_branch: { shape: 'triangle', glyph: '↗', label: 'FAN-OUT', tone: 'fanout' },
  command: { shape: 'rounded_rect', glyph: '>_', label: 'COMMAND', tone: 'command' },
  await_command: { shape: 'capsule', glyph: '‖', label: 'WAIT', tone: 'wait' },
  while: { shape: 'capsule', glyph: '↻', label: 'WHILE', tone: 'while' },
  // gateway 是子类型，根据 kind 二分
  // terminal 是子类型，根据 terminal_kind 二分
  join: { shape: 'hexagon', glyph: '∩', label: 'JOIN', tone: 'join' },
  quorum: { shape: 'hexagon', glyph: 'n/m', label: 'QUORUM', tone: 'quorum' },
  foreach: { shape: 'parallelogram', glyph: '⋈', label: 'FOREACH', tone: 'foreach' },
  // terminal 子类型（success / failure 共享 shape=octagon，glyph/label/tone 不同）
  terminal_success: { shape: 'octagon', glyph: '✓', label: 'END', tone: 'end' },
  terminal_failure: { shape: 'octagon', glyph: '✗', label: 'FAIL', tone: 'fail' },
};

function mappingForTerminal(terminalKind: DagTerminalKind): ShapeMapping {
  return terminalKind === 'failure'
    ? SHAPE_MAP.terminal_failure!
    : SHAPE_MAP.terminal_success!;
}

function mappingForGateway(gatewayKind: DagGatewayKind): ShapeMapping {
  if (gatewayKind === 'loop') {
    return { shape: 'capsule', glyph: '↻', label: 'LOOP', tone: 'loop' };
  }
  return { shape: 'diamond', glyph: '?', label: 'GATE', tone: 'gate' };
}

// ── Fallback 启发式 ──────────────────────────────────────────────────────

/** 当 node_type 未知时，按 business_role 推断最接近的语义。 */
function fallbackByBusinessRole(
  businessRole: string | undefined,
): ShapeMapping {
  if (!businessRole) return SHAPE_MAP.agent!; // 默认 WORKER（最常见的 node_type）
  const role = businessRole.toLowerCase();
  if (role.includes('review') || role.includes('critic')) {
    return { shape: 'diamond', glyph: '?', label: 'GATE', tone: 'gate' };
  }
  if (role.includes('loop') || role.includes('iter')) {
    return { shape: 'capsule', glyph: '↻', label: 'LOOP', tone: 'loop' };
  }
  if (role.includes('fan') || role.includes('branch') || role.includes('parallel')) {
    return { shape: 'triangle', glyph: '↗', label: 'FAN-OUT', tone: 'fanout' };
  }
  if (role.includes('exec') || role.includes('cli') || role.includes('command')) {
    return { shape: 'rounded_rect', glyph: '>_', label: 'COMMAND', tone: 'command' };
  }
  return SHAPE_MAP.agent!;
}

// ── 主入口 ──────────────────────────────────────────────────────────────

/**
 * 把节点元数据映射为视觉语义。纯函数，输入相同 → 输出相同。
 *
 * @param input 节点的 node_type / gateway_kind / terminal_kind / status / business_role
 * @returns DagNodeSemantic（shape/glyph/label/tone/status）
 */
export function resolveDagNodeSemantic(input: DagNodeSemanticInput): DagNodeSemantic {
  const status = normalizeStatus(input.status);
  const nodeType = input.node_type?.trim() || '';

  // 1. 显式 node_type 优先（11 类已知类型）
  if (nodeType === 'gateway') {
    const kind = normalizeGatewayKind(input.gateway_kind);
    const m = mappingForGateway(kind);
    return {
      shape: m.shape,
      glyph: m.glyph,
      label: m.label,
      tone: m.tone,
      status,
      gateway_kind: kind,
      node_type: 'gateway',
    };
  }
  if (nodeType === 'terminal') {
    const kind = normalizeTerminalKind(input.terminal_kind);
    const m = mappingForTerminal(kind);
    return {
      shape: m.shape,
      glyph: m.glyph,
      label: m.label,
      tone: m.tone,
      status,
      terminal_kind: kind,
      node_type: 'terminal',
    };
  }
  if (nodeType in SHAPE_MAP) {
    const m = SHAPE_MAP[nodeType]!;
    return {
      shape: m.shape,
      glyph: m.glyph,
      label: m.label,
      tone: m.tone,
      status,
      node_type: nodeType,
    };
  }

  // 2. fallback：按 business_role 启发式
  const m = fallbackByBusinessRole(input.business_role);
  return {
    shape: m.shape,
    glyph: m.glyph,
    label: m.label,
    tone: m.tone,
    status,
    node_type: nodeType || 'unknown',
  };
}

// ── 辅助导出：图例 + 测试用 ──────────────────────────────────────────────

/** 图例条目（供 DagLegend 渲染 + 测试断言用）。 */
export interface DagLegendEntry {
  shape: DagShape;
  glyph: string;
  label: string;
  tone: DagTone;
  /** 此条目代表的概念（多对一映射时聚合）。 */
  concept: string;
}

/** 返回 DagLegend 应展示的 8 种 shape 图例（每 shape 一行）。 */
export function listDagLegendEntries(): DagLegendEntry[] {
  return [
    { shape: 'circle', glyph: '', label: 'WORKER', tone: 'worker', concept: 'LLM 推理节点' },
    { shape: 'triangle', glyph: '↗', label: 'FAN-OUT', tone: 'fanout', concept: '并行分支' },
    { shape: 'diamond', glyph: '?', label: 'GATE', tone: 'gate', concept: '条件判断' },
    { shape: 'capsule', glyph: '↻/‖', label: 'LOOP/WAIT', tone: 'loop', concept: '循环 / 等待' },
    { shape: 'rounded_rect', glyph: '>_', label: 'COMMAND', tone: 'command', concept: 'CLI / 二进制' },
    { shape: 'octagon', glyph: '✓/✗', label: 'END/FAIL', tone: 'end', concept: '终止节点' },
    { shape: 'hexagon', glyph: '∩/n/m', label: 'JOIN/QUORUM', tone: 'join', concept: '多输入合并' },
    { shape: 'parallelogram', glyph: '⋈', label: 'FOREACH', tone: 'foreach', concept: '集合迭代（预留）' },
  ];
}

/** 把 DagTone 映射为 CSS 变量名（让 DagNodeShapeRegistry 不用再次枚举 tone）。 */
export function toneCssVar(tone: DagTone): string {
  return `--dag-tone-${tone}`;
}

/** 所有 8 种 shape 的稳定顺序（用于测试断言 + 图例排序）。 */
export const DAG_SHAPE_ORDER: readonly DagShape[] = [
  'circle',
  'triangle',
  'diamond',
  'capsule',
  'rounded_rect',
  'octagon',
  'hexagon',
  'parallelogram',
] as const;

/** 所有 13 种 tone 的稳定顺序（tone='unknown' 排末尾）。 */
export const DAG_TONE_ORDER: readonly DagTone[] = [
  'worker',
  'fanout',
  'gate',
  'loop',
  'command',
  'wait',
  'while',
  'end',
  'fail',
  'join',
  'quorum',
  'foreach',
  'unknown',
] as const;