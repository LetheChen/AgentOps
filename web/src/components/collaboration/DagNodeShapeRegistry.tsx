/**
 * DagNodeShapeRegistry — v99.5 P0.7 DAG 编排可视化合并的渲染器层。
 *
 * 职责（与 DagNodeSemantics.ts 解耦）：
 *   - 把 `DagNodeSemantic`（shape/glyph/label/tone/status）渲染成可视节点卡片
 *   - 8 种 shape 各自一个 SVG path（circle / triangle / diamond / capsule / rounded_rect / octagon / hexagon / parallelogram）
 *   - 8 种 status 视觉叠加（pending/ready/running/waiting_for_command/completed/failed/cancelled/skipped）
 *   - 5 种 metric 徽章（tokens / tool_calls / tool_failures / duration / error_type）
 *
 * 设计要点：
 *   - 颜色全部走 CSS 变量（--dag-tone-{tone} / --dag-status-{status}），由 index.css 集中管理
 *   - 主入口 <DagNodeCard> 复用现有 GraphNode 字段（向后兼容），可选 badges / shapeOverride
 *   - 不依赖 React Flow（与 AoDag 独立），由 DeveloperDagView / BusinessLaneView 自己定位
 *   - 所有内嵌 SVG 用 viewBox + 100×100 坐标系，让外层容器自由 scale
 *
 * 不要在此文件中：
 *   - 引 hardcoded 颜色（必须走 CSS 变量）
 *   - 直接 setState（保持纯渲染组件，状态由父层 DAG 视图管理）
 *   - 重复 DagNodeSemantics 的映射逻辑（语义层只算一次，本文件只读）
 */
import type {
  DagNodeSemantic,
  DagShape,
  DagStatus,
  DagTone,
} from './DagNodeSemantics';

// ── 公共 prop 类型 ────────────────────────────────────────────────────────

export interface DagNodeBadgeData {
  /** 累计输入 tokens（与 token_usage 互补；优先用 token_usage）。 */
  tokens_in?: number | null;
  /** 累计输出 tokens。 */
  tokens_out?: number | null;
  /** 工具调用总次数。 */
  tool_calls?: number | null;
  /** 工具失败次数。 */
  tool_failures?: number | null;
  /** 执行毫秒数（优先用 node.duration_ms）。 */
  duration_ms?: number | null;
  /** 失败类型（rate_limit / timeout / auth_error / unknown 等）。 */
  error_type?: string | null;
}

export interface DagNodeCardProps {
  /** 语义层输出（resolveDagNodeSemantic 返回值）。 */
  semantic: DagNodeSemantic;
  /** 节点 ID（用于 data-attr 选中 + 测试）。 */
  node_id: string;
  /** 显示标题（fallback 用 node_id）。 */
  display_name?: string;
  /** 角色/agent 标签。 */
  agent_label?: string;
  /** 选中态（高亮 + zIndex 提升）。 */
  selected?: boolean;
  /** 点击回调。 */
  onClick?: () => void;
  /** metric 徽章数据（缺省则不显示徽章）。 */
  badges?: DagNodeBadgeData;
}

// ── Shape SVG 渲染器（每个 shape 一个函数） ──────────────────────────────

interface ShapeSvgProps {
  shape: DagShape;
  status: DagStatus;
  tone: DagTone;
  selected: boolean;
}

/** 圆（WORKER）。 */
function CircleShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-circle${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="circle" data-status={status}>
      <circle cx="50" cy="50" r="44" />
    </svg>
  );
}

/** 三角形（FAN-OUT）。 */
function TriangleShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-triangle${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="triangle" data-status={status}>
      <polygon points="50,6 94,90 6,90" />
    </svg>
  );
}

/** 菱形（GATE / gateway condition）。 */
function DiamondShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-diamond${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="diamond" data-status={status}>
      <polygon points="50,4 96,50 50,96 4,50" />
    </svg>
  );
}

/** 胶囊（LOOP / WAIT / WHILE）。 */
function CapsuleShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-capsule${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="capsule" data-status={status}>
      <rect x="4" y="28" width="92" height="44" rx="22" ry="22" />
    </svg>
  );
}

/** 圆角矩形（COMMAND）。 */
function RoundedRectShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-rounded-rect${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="rounded_rect" data-status={status}>
      <rect x="6" y="14" width="88" height="72" rx="10" ry="10" />
    </svg>
  );
}

/** 八边形（END / FAIL / terminal）。 */
function OctagonShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-octagon${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="octagon" data-status={status}>
      <polygon points="30,4 70,4 96,30 96,70 70,96 30,96 4,70 4,30" />
    </svg>
  );
}

/** 六边形（JOIN / QUORUM）。 */
function HexagonShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-hexagon${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="hexagon" data-status={status}>
      <polygon points="50,4 94,28 94,72 50,96 6,72 6,28" />
    </svg>
  );
}

/** 平行四边形（FOREACH，未来预留）。 */
function ParallelogramShape({ status, selected }: { status: DagStatus; selected: boolean }) {
  const cls = `dag-shape dag-shape-parallelogram${selected ? ' dag-shape-selected' : ''}`;
  return (
    <svg viewBox="0 0 100 100" className={cls} data-shape="parallelogram" data-status={status}>
      <polygon points="22,12 96,12 78,88 4,88" />
    </svg>
  );
}

/** 8 种 shape 的统一路由（按 spec §3.8 映射表）。 */
function ShapeSvg({ shape, status, selected }: Pick<ShapeSvgProps, 'shape' | 'status' | 'selected'>) {
  switch (shape) {
    case 'circle': return <CircleShape status={status} selected={selected} />;
    case 'triangle': return <TriangleShape status={status} selected={selected} />;
    case 'diamond': return <DiamondShape status={status} selected={selected} />;
    case 'capsule': return <CapsuleShape status={status} selected={selected} />;
    case 'rounded_rect': return <RoundedRectShape status={status} selected={selected} />;
    case 'octagon': return <OctagonShape status={status} selected={selected} />;
    case 'hexagon': return <HexagonShape status={status} selected={selected} />;
    case 'parallelogram': return <ParallelogramShape status={status} selected={selected} />;
    default: {
      // exhaustive：TypeScript strict 应在缺 case 时报错；这里只兜底
      const _exhaustive: never = shape;
      void _exhaustive;
      return <CircleShape status={status} selected={selected} />;
    }
  }
}

// ── Status overlay（运行/失败/取消等视觉叠加） ───────────────────────────

interface StatusOverlayProps {
  status: DagStatus;
  terminalKind?: 'success' | 'failure';
  tone: DagTone;
}

/** status → 顶部右侧的徽章图标。completed 用 ✓ / failed 用 ✗ / cancelled 用 ⚠。 */
function StatusBadge({ status }: { status: DagStatus }) {
  if (status === 'completed') {
    return <span className="dag-status-badge dag-status-badge-completed" aria-label="completed">✓</span>;
  }
  if (status === 'failed') {
    return <span className="dag-status-badge dag-status-badge-failed" aria-label="failed">✗</span>;
  }
  if (status === 'cancelled') {
    return <span className="dag-status-badge dag-status-badge-cancelled" aria-label="cancelled">⚠</span>;
  }
  if (status === 'waiting_for_command') {
    return <span className="dag-status-badge dag-status-badge-waiting" aria-label="waiting">…</span>;
  }
  return null;
}

function StatusOverlay({ status, tone }: StatusOverlayProps) {
  return (
    <div className="dag-status-overlay" data-status={status} data-tone={tone}>
      {/* 状态徽章只对终态/等待态有意义；运行时由 .dag-card[data-status="running"] 的 box-shadow 接管动画 */}
      <StatusBadge status={status} />
    </div>
  );
}

// ── Metric 徽章（5 类，按 §3.8 表触发） ──────────────────────────────────

function MetricBadgeStack({ badges }: { badges?: DagNodeBadgeData }) {
  if (!badges) return null;
  const items: Array<{ key: string; icon: string; text: string; tone: string }> = [];

  const totalTokens = (badges.tokens_in ?? 0) + (badges.tokens_out ?? 0);
  if (totalTokens > 0 || badges.tokens_in != null || badges.tokens_out != null) {
    items.push({
      key: 'tokens',
      icon: '🔢',
      text: formatCount(totalTokens),
      tone: 'info',
    });
  }
  if (typeof badges.tool_calls === 'number' && badges.tool_calls > 0) {
    items.push({
      key: 'tool_calls',
      icon: '🔧',
      text: String(badges.tool_calls),
      tone: 'neutral',
    });
  }
  if (typeof badges.tool_failures === 'number' && badges.tool_failures > 0) {
    items.push({
      key: 'tool_failures',
      icon: '❌',
      text: String(badges.tool_failures),
      tone: 'critical',
    });
  }
  if (typeof badges.duration_ms === 'number' && badges.duration_ms > 0) {
    items.push({
      key: 'duration',
      icon: '⏱',
      text: `${(badges.duration_ms / 1000).toFixed(1)}s`,
      tone: 'neutral',
    });
  }
  if (badges.error_type && badges.error_type.length > 0) {
    items.push({
      key: 'error_type',
      icon: '⚠',
      text: badges.error_type,
      tone: 'critical',
    });
  }

  if (items.length === 0) return null;

  return (
    <ul className="dag-badge-stack" aria-label="node metrics">
      {items.map((item) => (
        <li key={item.key} className="dag-badge" data-tone={item.tone}>
          <span className="dag-badge-icon" aria-hidden>{item.icon}</span>
          <span className="dag-badge-text">{item.text}</span>
        </li>
      ))}
    </ul>
  );
}

function formatCount(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

// ── 主入口：DagNodeCard ──────────────────────────────────────────────────

/**
 * 统一的 DAG 节点渲染器。
 * - 卡片宽 200 / 高 100（与 DeveloperDagView 既有布局对齐）
 * - 左 64×64 是 shape svg，右 136 是 label/glyph/badges
 * - selected 时加 4px outline + zIndex 12
 */
export function DagNodeCard({
  semantic,
  node_id,
  display_name,
  agent_label,
  selected = false,
  onClick,
  badges,
}: DagNodeCardProps) {
  const title = display_name || node_id;
  const toneClass = `dag-tone-${semantic.tone}`;
  const status = semantic.status;

  return (
    <div
      className={`dag-card ${toneClass}${selected ? ' dag-card-selected' : ''}`}
      data-dag-node={node_id}
      data-shape={semantic.shape}
      data-tone={semantic.tone}
      data-status={status}
      data-node-type={semantic.node_type}
      data-gateway-kind={semantic.gateway_kind}
      data-terminal-kind={semantic.terminal_kind}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (onClick && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault();
          onClick();
        }
      }}
    >
      {/* 左：shape svg */}
      <div className="dag-card-shape-wrap">
        <ShapeSvg shape={semantic.shape} status={status} selected={selected} />
        <span className="dag-card-glyph" aria-hidden>{semantic.glyph}</span>
      </div>

      {/* 右：label / role / 徽章 */}
      <div className="dag-card-body">
        <div className="dag-card-label-row">
          <span className="dag-card-label">{semantic.label}</span>
          <span className="dag-card-node-id" title={node_id}>#{truncate(node_id, 12)}</span>
        </div>
        <div className="dag-card-title" title={title}>{title}</div>
        {agent_label && (
          <div className="dag-card-agent" title={agent_label}>
            {truncate(agent_label, 22)}
          </div>
        )}
        <MetricBadgeStack badges={badges} />
      </div>

      <StatusOverlay status={status} terminalKind={semantic.terminal_kind} tone={semantic.tone} />
    </div>
  );
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + '…';
}

// ── 辅助：从 GraphNode 直接构造（让现有 dag 视图无脑接入） ──────────────

export interface GraphNodeLike {
  node_id: string;
  agent_id?: string;
  business_role?: string;
  display_name?: string;
  harness?: string;
  model?: string;
  status?: string;
  duration_ms?: number | null;
  token_usage?: number | null;
  // 新字段（v99.5 后端 DagEngine 推送）
  node_type?: string;
  gateway_kind?: string;
  terminal_kind?: string;
  tool_calls?: number | null;
  tool_failures?: number | null;
  error_type?: string | null;
  // 进/出 tokens 拆分（缺省时只用 token_usage）
  tokens_in?: number | null;
  tokens_out?: number | null;
}

/**
 * 把 GraphNode 转换为 DagNodeCard 需要的 props。
 * 不调用 resolveDagNodeSemantic（让调用方决定缓存策略）。
 */
export function graphNodeToBadgeData(node: GraphNodeLike): DagNodeBadgeData {
  return {
    tokens_in: node.tokens_in ?? null,
    tokens_out: node.tokens_out ?? null,
    duration_ms: node.duration_ms ?? null,
    tool_calls: node.tool_calls ?? null,
    tool_failures: node.tool_failures ?? null,
    error_type: node.error_type ?? null,
  };
}