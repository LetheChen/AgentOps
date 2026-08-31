// 6 类 widget 协议 (v2.1 §4.5 + §8)

export type WidgetType =
  | "memo"
  | "task_draft"
  | "progress_status"
  | "checklist"
  | "artifact_ref"
  | "timeline";

export interface WidgetItem {
  text: string;
  done?: boolean;
  status?: "pending" | "running" | "done" | "failed";
}

export interface WidgetStep {
  id: string;
  title: string;
  status: "pending" | "active" | "done" | "failed";
}

export interface WidgetArtifact {
  name: string;
  url?: string;
  size?: number;
}

export interface WidgetEvent {
  time: string;
  text: string;
}

export interface WidgetUpdate {
  run_id: string;
  widget_id: string;
  type: WidgetType;
  props: {
    title?: string;
    body?: string;
    items?: WidgetItem[];
    steps?: WidgetStep[];
    active_step?: number;
    artifacts?: WidgetArtifact[];
    events?: WidgetEvent[];
  };
  state?: Record<string, any>;
  version?: number;
}

export interface DagEvent {
  type: string;
  run_id: string;
  node_id?: string | null;
  payload?: Record<string, any>;
  sequence?: number;
  occurred_at?: string;
}

export interface RunState {
  run_id: string;
  workflow_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | "paused";
  started_at: string;
  finished_at?: string;
  total_tokens_input: number;
  total_tokens_output: number;
  node_states: Record<string, string>;
}

export const STATUS_COLORS: Record<string, string> = {
  pending: "#94a3b8",
  ready: "#fbbf24",
  waiting: "#a78bfa",
  running: "#3b82f6",
  completed: "#10b981",
  failed: "#ef4444",
  skipped: "#6b7280",
};

// 协作可视化：业务角色名 → 泳道颜色（与后端 _LANE_COLORS 对齐）
export const LANE_COLORS = ["#3b82f6", "#06b6d4", "#8b5cf6", "#f59e0b", "#ec4899", "#10b981"];

// 协作可视化：节点状态 → 卡片色（与 STATUS_COLORS 对齐）
export const NODE_CARD_STYLES: Record<string, { bg: string; border: string; icon: string }> = {
  started:   { bg: "rgba(59,130,246,.10)",  border: "#3b82f6", icon: "▶" },
  completed: { bg: "rgba(16,185,129,.10)",  border: "#10b981", icon: "✓" },
  failed:    { bg: "rgba(239,68,68,.12)",   border: "#ef4444", icon: "✗" },
  skipped:   { bg: "rgba(245,158,11,.10)",  border: "#f59e0b", icon: "⊘" },
};

// 协作可视化：聚合数据 schema
export interface LaneInfo {
  business_role: string;
  color: string;
  nodes: string[];
}

export interface GraphNode {
  node_id: string;
  agent_id: string;
  business_role: string;
  display_name: string;
  harness: string;
  model: string;
  status: string;
  duration_ms?: number | null;
  token_usage?: number | null;
  error?: string | null;
  // v99.5 P0.7：节点类型字段（后端 DagEngine 推送，可选 → 缺省按 fallback 启发式）
  node_type?: string;
  gateway_kind?: string;
  terminal_kind?: string;
  // v99.5 P0.7：metric 徽章触发字段
  tokens_in?: number | null;
  tokens_out?: number | null;
  tool_calls?: number | null;
  tool_failures?: number | null;
  error_type?: string | null;
}

export interface GraphEdge {
  from: string;
  to: string;
  port: string;
}

export interface HandoffInfo {
  id: string;
  from_node: string;
  from_role: string;
  to_node: string;
  to_role: string;
  port: string;
  payload_size: number;
  summary: string;
  sequence: number;
  occurred_at?: string | null;
}

export interface TimelineEntry {
  sequence: number;
  occurred_at?: string | null;
  type: string;
  node_id?: string | null;
  label: string;
  payload_size?: number | null;
}

export interface CollaborationGraph {
  run_id: string;
  workflow_id: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  lanes: LaneInfo[];
  nodes: GraphNode[];
  edges: GraphEdge[];
  handoffs: HandoffInfo[];
  timeline: TimelineEntry[];
}

// 🆕 Phase 1: Session 与 Run 解耦后的 Session 全景数据
// 后端来源：audit/store.py 的 sessions / session_memory 表 + list_runs_by_session

/** Session 元数据（sessions 表）。 */
export interface SessionInfo {
  session_id: string;
  user_id?: string;
  agent_id: string;
  status: 'active' | 'dormant' | 'archived' | string;
  title?: string;
  started_at: string;
  last_activity_at: string;
  message_count: number;
  attached_run_count: number;
  total_tokens_input: number;
  total_tokens_output: number;
  metadata?: Record<string, unknown> | null;
}

/** Session 关联的子 Run 摘要（runs 表子集）。 */
export interface SessionRunInfo {
  run_id: string;
  session_id: string;
  workflow_id: string | null;
  run_mode: string;
  agent_id: string | null;
  status: string;
  title: string | null;
  started_at: string;
  finished_at: string | null;
}

/** Session 中期记忆条目（session_memory 表）。 */
export interface SessionMemoryInfo {
  id: number;
  session_id: string;
  memory_type: 'run_summary' | 'topic_summary' | 'user_preference' | string;
  source_run_id: string | null;
  content: string;
  tokens: number;
  importance: number;
  created_at: string;
  expires_at: string | null;
}
