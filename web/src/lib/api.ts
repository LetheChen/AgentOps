import type { TimelineEntry as _TimelineEntry, CollaborationGraph as _CollaborationGraph, SessionInfo as _SessionInfo, SessionRunInfo as _SessionRunInfo, SessionMemoryInfo as _SessionMemoryInfo } from './types';

// dev 下走 vite 代理（同源，见 vite.config.ts proxy），使 EventSource/SSE 的
// cookie 鉴权生效；非 dev（直连后端静态部署）回退为后端直连地址。
export const API_BASE_URL = import.meta.env.DEV ? '' : 'http://127.0.0.1:1987';

export type WidgetType = 'a2ui';

export interface WidgetUpdate {
  run_id: string;
  widget_id: string;
  type: WidgetType;
  props: Record<string, unknown>;
  state?: Record<string, unknown>;
  version?: number;
}

export interface AgentRunResponse {
  run_id: string;
  stream_url: string;
}

export interface StartRunPayload {
  workflow_id?: string;
  inputs?: Record<string, unknown>;
  run_mode?: 'templated' | 'conversational' | 'task' | 'hybrid';
  agent_id?: string;
  initial_message?: string;
  workspace_id?: string | null;  // P0.18.7b: 指定授权 workspace（null=通用对话）
  // 指定关联 session_id：run 事件经 bridge_run_events 转发到该 session 的 SSE 流
  session_id?: string | null;
}

export interface WidgetInputPayload {
  widget_id: string;
  input: Record<string, unknown>;
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const message = await response.text();
    throw new Error(`Request failed ${response.status}: ${message || response.statusText}`);
  }

  return response.json() as Promise<T>;
}

export class ApiClient {
  constructor(private readonly baseUrl = API_BASE_URL) {}

  async startRun(payload: StartRunPayload): Promise<AgentRunResponse> {
    const response = await fetch(`${this.baseUrl}/api/agent/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    return parseJsonResponse<AgentRunResponse>(response);
  }

  async resumeRun(runId: string, workflowId: string, inputs: Record<string, unknown> = {}, nodeId?: string, onlyNode = false): Promise<AgentRunResponse> {
    const response = await fetch(`${this.baseUrl}/api/agent/runs/${encodeURIComponent(runId)}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workflow_id: workflowId, inputs, node_id: nodeId ?? null, only_node: onlyNode }),
    });

    return parseJsonResponse<AgentRunResponse>(response);
  }

  async cancelRun(runId: string): Promise<{ status: string; run_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/agent/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    return parseJsonResponse(response);
  }

  // ── Sessions 会话 API（新建/继续/历史消息）──

  async createSession(agentId: string, message: string, runMode = 'conversational', workspaceId: string | null = null): Promise<{ run_id: string; stream_url: string }> {
    // P0.18.7b: workspaceId null 时不传字段（=通用对话），传时写 sessions.workspace_id
    const body: Record<string, unknown> = { agent_id: agentId, message, run_mode: runMode };
    if (workspaceId !== null) body.workspace_id = workspaceId;
    const response = await fetch(`${this.baseUrl}/api/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return parseJsonResponse(response);
  }

  async sendSessionMessage(runId: string, message: string): Promise<{ run_id: string; stream_url: string; resumed: boolean }> {
    const response = await fetch(`${this.baseUrl}/api/sessions/${encodeURIComponent(runId)}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    return parseJsonResponse(response);
  }

  async getSessionMessages(runId: string, limit = 1000): Promise<{ run_id: string; messages: Array<{ id: number; run_id: string; seq: number; role: string; content: unknown; created_at: string }> }> {
    const response = await fetch(`${this.baseUrl}/api/sessions/${encodeURIComponent(runId)}/messages?limit=${limit}`);
    return parseJsonResponse(response);
  }

  async listSessions(runMode?: string, status?: string, limit = 100, offset = 0, search?: string): Promise<{ sessions: Array<Record<string, unknown>>; count: number; total: number }> {
    const params = new URLSearchParams();
    if (runMode) params.set('run_mode', runMode);
    if (status) params.set('status', status);
    params.set('limit', String(limit));
    params.set('offset', String(offset));
    if (search) params.set('search', search);
    const response = await fetch(`${this.baseUrl}/api/sessions?${params}`);
    return parseJsonResponse(response);
  }

  async updateSessionTitle(runId: string, title: string): Promise<{ run_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/sessions/${encodeURIComponent(runId)}/title`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    return parseJsonResponse<{ run_id: string; status: string }>(response);
  }

  // ── 🆕 Phase 1: Session 全景 API（Session 与 Run 解耦后新增）──
  // 后端：api/server.py 的 GET /api/sessions/{id} + /runs + /memory

  /** 获取 Session 元数据 + 关联的子 Run 列表。 */
  async getSession(sessionId: string): Promise<{ session: _SessionInfo; runs: _SessionRunInfo[] }> {
    const response = await fetch(`${this.baseUrl}/api/sessions/${encodeURIComponent(sessionId)}`);
    return parseJsonResponse<{ session: _SessionInfo; runs: _SessionRunInfo[] }>(response);
  }

  /** 列出 Session 关联的所有子 Run（仅 runs，不含 session 元数据）。 */
  async getSessionRuns(sessionId: string): Promise<{ session_id: string; runs: _SessionRunInfo[]; total: number }> {
    const response = await fetch(`${this.baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/runs`);
    return parseJsonResponse<{ session_id: string; runs: _SessionRunInfo[]; total: number }>(response);
  }

  /** 查询 Session 的中期记忆（run_summary / topic_summary / user_preference）。 */
  async getSessionMemory(sessionId: string, limit = 20): Promise<{ session_id: string; memories: _SessionMemoryInfo[]; total: number }> {
    const response = await fetch(`${this.baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/memory?limit=${limit}`);
    return parseJsonResponse<{ session_id: string; memories: _SessionMemoryInfo[]; total: number }>(response);
  }

  openEventStream(
    runId: string,
    handlers: {
      onMessage: (event: MessageEvent<string>) => void;
      onError?: (event: Event) => void;
      /**
       * 连接成功（包括重连成功）时触发，前端用这个把 sseConnected 设回 true
       * 不传则不通知（向后兼容旧调用）
       */
      onOpen?: () => void;
      lastEventId?: number | string;
    },
  ): () => void {
    const query = handlers.lastEventId === undefined ? '' : `?last_event_id=${encodeURIComponent(String(handlers.lastEventId))}`;
    const source = new EventSource(`${this.baseUrl}/api/agent/runs/${encodeURIComponent(runId)}/events${query}`);

    source.onmessage = handlers.onMessage;
    source.onopen = () => handlers.onOpen?.();
    source.onerror = (event) => {
      handlers.onError?.(event);
    };

    return () => source.close();
  }

  // ── Thread 模式 v2 Session API ──

  async v2CreateSession(agentId: string = 'manager', workspaceId: string | null = null): Promise<{ session_id: string; stream_url: string }> {
    // P0.18.7: workspace_id 可为 null（通用对话）或具体授权 workspace UUID
    const body: Record<string, unknown> = { agent_id: agentId };
    if (workspaceId !== null) body.workspace_id = workspaceId;
    const response = await fetch(`${this.baseUrl}/api/v2/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return parseJsonResponse(response);
  }

  async v2SendTurn(sessionId: string, message: string): Promise<{ session_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/turns`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    return parseJsonResponse(response);
  }

  /**
   * 切换会话级权限级别（与 workspace 解耦，随时可切换、立即生效）。
   * 同一工作区下不同会话可有不同权限级别。
   */
  async v2UpdateSessionPermission(
    sessionId: string,
    permissionLevel: PermissionLevel,
  ): Promise<{ session_id: string; permission_level: string }> {
    const response = await fetch(
      `${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/permission`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ permission_level: permissionLevel }),
      },
    );
    return parseJsonResponse(response);
  }

  async v2CancelSession(sessionId: string): Promise<{ status: string; session_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    return parseJsonResponse(response);
  }

  /**
   * 审批决定（deepseek-harness 对齐的 allowed-once 语义）：
   * "allowed-once" 只放行被问的那一次工具调用，不改变会话权限级别；
   * "rejected" 拒绝本次执行。
   */
  async v2DecideApproval(
    requestId: string,
    outcome: 'allowed-once' | 'rejected',
  ): Promise<{ request_id: string; outcome: string }> {
    const response = await fetch(
      `${this.baseUrl}/api/v2/approvals/${encodeURIComponent(requestId)}/decide`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ outcome }),
      },
    );
    return parseJsonResponse(response);
  }

  async v2GetSessionMessages(sessionId: string, limit: number = 1000): Promise<{ session_id: string; messages: Array<Record<string, unknown>>; total: number }> {
    const response = await fetch(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`);
    return parseJsonResponse(response);
  }

  async v2GetSession(sessionId: string): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}`);
    return parseJsonResponse(response);
  }

  /** 列出 session 关联的所有子 Run（v2 Thread 模式）。 */
  async v2GetSessionRuns(sessionId: string): Promise<{ session_id: string; runs: _SessionRunInfo[]; total: number }> {
    const response = await fetch(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/runs`);
    return parseJsonResponse<{ session_id: string; runs: _SessionRunInfo[]; total: number }>(response);
  }

  /** 查询 session 中期记忆（v2 Thread 模式）。 */
  async v2GetSessionMemory(sessionId: string, limit: number = 20): Promise<{ session_id: string; memories: _SessionMemoryInfo[]; total: number }> {
    const response = await fetch(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/memory?limit=${limit}`);
    return parseJsonResponse<{ session_id: string; memories: _SessionMemoryInfo[]; total: number }>(response);
  }

  /** 提交 widget 交互输入（v2 Thread 模式，按 session_id 路由）。 */
  async v2SendWidgetInput(sessionId: string, payload: WidgetInputPayload): Promise<{ status: string; session_id: string; widget_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/widget-input`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const message = await response.text();
      throw new Error(`v2 widget input failed ${response.status}: ${message || response.statusText}`);
    }
    return parseJsonResponse(response);
  }

  async v2ListSessions(limit: number = 50, offset: number = 0, status?: string): Promise<{ sessions: Array<Record<string, unknown>>; count: number; total: number }> {
    const params = new URLSearchParams();
    params.set('limit', String(limit));
    params.set('offset', String(offset));
    if (status) params.set('status', status);
    const response = await fetch(`${this.baseUrl}/api/v2/sessions?${params}`);
    return parseJsonResponse(response);
  }

  v2OpenSessionEventStream(
    sessionId: string,
    handlers: {
      onMessage: (event: MessageEvent<string>) => void;
      onError?: (event: Event) => void;
      onOpen?: () => void;
    },
  ): () => void {
    const source = new EventSource(`${this.baseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/events`);
    source.onmessage = handlers.onMessage;
    source.onopen = () => handlers.onOpen?.();
    source.onerror = (event) => { handlers.onError?.(event); };
    return () => source.close();
  }

  async sendWidgetInput(runId: string, payload: WidgetInputPayload): Promise<void> {
    const response = await fetch(`${this.baseUrl}/api/agent/runs/${encodeURIComponent(runId)}/widget-input`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const message = await response.text();
      throw new Error(`Widget input failed ${response.status}: ${message || response.statusText}`);
    }
  }

  async getHarnesses(): Promise<{ harnesses: string[] }> {
    const response = await fetch(`${this.baseUrl}/api/agent/harnesses`);
    return parseJsonResponse<{ harnesses: string[] }>(response);
  }

  async getAgents(): Promise<{ agents: Array<Record<string, unknown>> }> {
    const response = await fetch(`${this.baseUrl}/api/agent/agents`);
    return parseJsonResponse<{ agents: Array<Record<string, unknown>> }>(response);
  }

  /** 单个 agent 详情（含运行统计 + 工作流绑定）。 */
  async getAgent(agentId: string): Promise<{ agent: Record<string, unknown> }> {
    const response = await fetch(`${this.baseUrl}/api/agent/agents/${encodeURIComponent(agentId)}`);
    return parseJsonResponse<{ agent: Record<string, unknown> }>(response);
  }

  /** 全部工具元数据（config/tools + 内置），供权限管理 UI 展示。 */
  async getTools(): Promise<{ tools: Array<Record<string, unknown>> }> {
    const response = await fetch(`${this.baseUrl}/api/agent/tools`);
    return parseJsonResponse<{ tools: Array<Record<string, unknown>> }>(response);
  }

  /** 单 agent 真实运行统计 + 工作流绑定。 */
  async getAgentStats(agentId: string): Promise<{ agent_id: string; stats: Record<string, unknown>; workflow_bindings: Array<Record<string, unknown>> }> {
    const response = await fetch(`${this.baseUrl}/api/agent/agents/${encodeURIComponent(agentId)}/stats`);
    return parseJsonResponse(response);
  }

  /**
   * v99.5 P0.11：列出所有 Actor Visual Profile（L1.5 Worker Profile 层）。
   * 返回每个 actor 的 actor_id + allowed_surface_views[]，
   * 供 SupervisionPanel 启用 view_id 白名单。
   *
   * 与 getAgents() 区别：
   *   - getAgents() → Agent 维度（runtime + config agents）
   *   - getActorProfiles() → Actor Profile 维度（view_id 白名单 + 字段约束）
   */
  async getActorProfiles(): Promise<{
    actors: Array<{
      actor_id: string;
      description: string;
      allowed_surface_views: Array<{
        view_id: string;
        output_contract: string | null;
        description: string;
        required_phases: string[];
        fields: Record<string, { type: string; required: boolean; max_length?: number; min?: number; max?: number; enum_values?: string[] }>;
      }>;
    }>;
  }> {
    const response = await fetch(`${this.baseUrl}/api/actors`);
    return parseJsonResponse(response);
  }

  /** 创建 agent（持久化到 config/agents/{id}.yaml）。 */
  async createAgent(agent: Record<string, unknown>): Promise<{ agent: Record<string, unknown> }> {
    const response = await fetch(`${this.baseUrl}/api/agent/agents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(agent),
    });
    return parseJsonResponse<{ agent: Record<string, unknown> }>(response);
  }

  /** 更新 agent（config agent 写回 YAML + 热重载）。 */
  async updateAgent(agentId: string, agent: Record<string, unknown>): Promise<{ agent: Record<string, unknown> }> {
    const response = await fetch(`${this.baseUrl}/api/agent/agents/${encodeURIComponent(agentId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(agent),
    });
    return parseJsonResponse<{ agent: Record<string, unknown> }>(response);
  }

  /** 删除 agent（runtime 删内存；config 删 YAML）。 */
  async deleteAgent(agentId: string): Promise<{ deleted: string }> {
    const response = await fetch(`${this.baseUrl}/api/agent/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' });
    return parseJsonResponse<{ deleted: string }>(response);
  }

  async getWorkflows(): Promise<{ workflows: Array<Record<string, unknown>> }> {
    const response = await fetch(`${this.baseUrl}/api/agent/workflows`);
    return parseJsonResponse<{ workflows: Array<Record<string, unknown>> }>(response);
  }

  async getWorkflowDetail(workflowId: string): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}/api/agent/workflows/${encodeURIComponent(workflowId)}`);
    return parseJsonResponse<Record<string, unknown>>(response);
  }

  async createWorkflow(yamlContent: string): Promise<{ workflow_id: string; name: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/agent/workflows`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ yaml_content: yamlContent }),
    });
    return parseJsonResponse<{ workflow_id: string; name: string; status: string }>(response);
  }

  async updateWorkflow(workflowId: string, yamlContent: string): Promise<{ workflow_id: string; name: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/agent/workflows/${encodeURIComponent(workflowId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ yaml_content: yamlContent }),
    });
    return parseJsonResponse<{ workflow_id: string; name: string; status: string }>(response);
  }

  async deleteWorkflow(workflowId: string): Promise<{ deleted: string }> {
    const response = await fetch(`${this.baseUrl}/api/agent/workflows/${encodeURIComponent(workflowId)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ deleted: string }>(response);
  }

  // ── 审计 API（P0）──
  async auditListRuns(filters: { workflow_id?: string; status?: string; limit?: number; offset?: number } = {}): Promise<{ runs: Array<Record<string, unknown>>; count: number; total: number }> {
    const params = new URLSearchParams();
    if (filters.workflow_id) params.set('workflow_id', filters.workflow_id);
    if (filters.status) params.set('status', filters.status);
    if (filters.limit != null) params.set('limit', String(filters.limit));
    if (filters.offset != null) params.set('offset', String(filters.offset));
    const qs = params.toString() ? `?${params}` : '';
    const response = await fetch(`${this.baseUrl}/api/audit/runs${qs}`);
    return parseJsonResponse(response);
  }

  async auditGetRunSummary(runId: string): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}/api/audit/runs/${encodeURIComponent(runId)}/summary`);
    return parseJsonResponse<Record<string, unknown>>(response);
  }

  async auditGetRunEvents(runId: string, since = 0, limit = 10000): Promise<{ run_id: string; count: number; events: Array<Record<string, unknown>> }> {
    const response = await fetch(`${this.baseUrl}/api/audit/runs/${encodeURIComponent(runId)}/events?since=${since}&limit=${limit}`);
    return parseJsonResponse<{ run_id: string; count: number; events: Array<Record<string, unknown>> }>(response);
  }

  async auditGetNodeDetail(runId: string, nodeId: string): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}/api/audit/runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}/detail`);
    return parseJsonResponse<Record<string, unknown>>(response);
  }

  async getCollaborationGraph(runId: string): Promise<_CollaborationGraph> {
    const response = await fetch(`${this.baseUrl}/api/audit/runs/${encodeURIComponent(runId)}/collaboration-graph`);
    return parseJsonResponse<_CollaborationGraph>(response);
  }

  async getCollaborationTimeline(
    runId: string,
    opts: { nodeId?: string; type?: string; sinceSeq?: number; sinceTime?: string } = {},
  ): Promise<{ run_id: string; count: number; timeline: _TimelineEntry[] }> {
    const params = new URLSearchParams();
    if (opts.nodeId) params.set('node_id', opts.nodeId);
    if (opts.type) params.set('type', opts.type);
    if (opts.sinceSeq != null) params.set('since_seq', String(opts.sinceSeq));
    if (opts.sinceTime) params.set('since_time', opts.sinceTime);
    const qs = params.toString();
    const url = `${this.baseUrl}/api/audit/runs/${encodeURIComponent(runId)}/collaboration-graph/timeline${qs ? `?${qs}` : ''}`;
    const response = await fetch(url);
    return parseJsonResponse<{ run_id: string; count: number; timeline: _TimelineEntry[] }>(response);
  }

  // ── 统一运行时 API ──
  async getRuntimeSummary(): Promise<RuntimeSummary> {
    const response = await fetch(`${this.baseUrl}/api/runtime/summary`);
    return parseJsonResponse<RuntimeSummary>(response);
  }

  async getRuntimeHealth(): Promise<{ providers: Record<string, ProviderHealthResult> }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/health`);
    return parseJsonResponse<{ providers: Record<string, ProviderHealthResult> }>(response);
  }

  // ── Docker runtime management ──
  async listDockerContainers(all = true): Promise<{ containers: DockerContainerInfo[] }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/docker/containers?all=${all ? 'true' : 'false'}`);
    return parseJsonResponse<{ containers: DockerContainerInfo[] }>(response);
  }

  async pullDockerImage(image: string): Promise<{ status: string; result: Record<string, unknown> }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/docker/images/pull`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image }),
    });
    return parseJsonResponse<{ status: string; result: Record<string, unknown> }>(response);
  }

  async createDockerContainer(payload: { image: string; name?: string; cmd?: string[]; env?: Record<string, string>; labels?: Record<string, string> }): Promise<{ status: string; container: { id: string; short_id: string; name: string } }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/docker/containers`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    return parseJsonResponse<{ status: string; container: { id: string; short_id: string; name: string } }>(response);
  }

  async stopDockerContainer(containerId: string): Promise<{ status: string; container_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/docker/containers/${encodeURIComponent(containerId)}/stop`, { method: 'POST' });
    return parseJsonResponse<{ status: string; container_id: string }>(response);
  }

  async removeDockerContainer(containerId: string): Promise<{ status: string; container_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/docker/containers/${encodeURIComponent(containerId)}`, { method: 'DELETE' });
    return parseJsonResponse<{ status: string; container_id: string }>(response);
  }

  async getDockerContainerLogs(containerId: string, tail = 200): Promise<{ container_id: string; logs: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/docker/containers/${encodeURIComponent(containerId)}/logs?tail=${tail}`);
    return parseJsonResponse<{ container_id: string; logs: string }>(response);
  }

  // ── P0.17 Runtime Environment ──
  async getRuntimeEnvironment(): Promise<RuntimeEnvironmentSnapshot> {
    const response = await fetch(`${this.baseUrl}/api/runtime/environment`);
    return parseJsonResponse<RuntimeEnvironmentSnapshot>(response);
  }

  async rebuildRuntimeEnvironment(force = false): Promise<{ build_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/environment/rebuild`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force }),
    });
    return parseJsonResponse<{ build_id: string; status: string }>(response);
  }

  streamBuildLog(buildId: string): EventSource {
    return new EventSource(`${this.baseUrl}/api/runtime/environment/build/${encodeURIComponent(buildId)}/stream`);
  }

  async listActiveWorkers(): Promise<{ workers: ConnectedWorker[]; count: number }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/environment/workers`);
    return parseJsonResponse<{ workers: ConnectedWorker[]; count: number }>(response);
  }

  async addModel(providerId: string, model: ModelInfo): Promise<{ status: string; provider_id: string; model_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/models`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_id: providerId, model }),
    });
    return parseJsonResponse<{ status: string; provider_id: string; model_id: string }>(response);
  }

  async deleteModel(providerId: string, modelId: string): Promise<{ status: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/models/${encodeURIComponent(providerId)}/${encodeURIComponent(modelId)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ status: string }>(response);
  }

  async deleteProvider(providerId: string): Promise<{ status: string; provider_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/providers`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_id: providerId }),
    });
    return parseJsonResponse<{ status: string; provider_id: string }>(response);
  }

  async updateRuntimeProvider(providerId: string, updates: { base_url?: string; protocol?: string; auth_type?: string }): Promise<{ status: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/providers/${encodeURIComponent(providerId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    return parseJsonResponse<{ status: string }>(response);
  }

  async setDefaultModel(providerId: string, modelId: string): Promise<{ status: string; provider_id: string; model_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/default-model`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
    });
    return parseJsonResponse<{ status: string; provider_id: string; model_id: string }>(response);
  }

  async setManagerModel(providerId: string, modelId: string): Promise<{ status: string; provider_id: string; model_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/manager-model`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
    });
    return parseJsonResponse<{ status: string; provider_id: string; model_id: string }>(response);
  }

  /** 批量写 fallback_chains。chains 为 {primary: [entry, ...]}，entry 可为 string（仅 provider）或 {provider, model?}。 */
  async updateFallbackChains(
    chains: Record<string, Array<string | { provider: string; model?: string | null }>>,
  ): Promise<{
    status: string;
    chains: Record<string, Array<{ provider: string; model: string | null }>>;
    raw_chains: Record<string, Array<string | { provider: string; model?: string | null }>>;
  }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/fallback-chains`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chains }),
    });
    return parseJsonResponse(response);
  }

  async createProvider(payload: { provider_id: string; base_url: string; protocol: string; auth_type: string; api_key_env?: string }): Promise<{ status: string; provider_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/providers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return parseJsonResponse<{ status: string; provider_id: string }>(response);
  }

  // ── Provider 管理 API（P2-2）──
  async listProviders(): Promise<{ providers: ProviderInfo[] }> {
    const response = await fetch(`${this.baseUrl}/api/providers`);
    return parseJsonResponse<{ providers: ProviderInfo[] }>(response);
  }

  async setProviderCredential(providerId: string, apiKey: string): Promise<{ provider_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/providers/${encodeURIComponent(providerId)}/credential`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey }),
    });
    return parseJsonResponse<{ provider_id: string; status: string }>(response);
  }

  async deleteProviderCredential(providerId: string): Promise<{ provider_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/providers/${encodeURIComponent(providerId)}/credential`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ provider_id: string; status: string }>(response);
  }

  // ── SSH 凭据 API（log-puller 日志拉取 / 将来 ssh_exec 复用）──
  async listSshCredentials(): Promise<{ credentials: SshCredentialInfo[] }> {
    const response = await fetch(`${this.baseUrl}/api/ssh-credentials`);
    return parseJsonResponse<{ credentials: SshCredentialInfo[] }>(response);
  }

  async setSshCredential(credentialId: string, secret: string): Promise<{ credential_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/ssh-credentials/${encodeURIComponent(credentialId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ secret }),
    });
    return parseJsonResponse<{ credential_id: string; status: string }>(response);
  }

  async deleteSshCredential(credentialId: string): Promise<{ credential_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/ssh-credentials/${encodeURIComponent(credentialId)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ credential_id: string; status: string }>(response);
  }

  // ── 数据库连接凭据 API（凭据管理 Tab「数据库连接」配套）──
  async setDbCredential(credentialId: string, secret: string): Promise<{ credential_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/db-credentials/${encodeURIComponent(credentialId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ secret }),
    });
    return parseJsonResponse<{ credential_id: string; status: string }>(response);
  }

  async deleteDbCredential(credentialId: string): Promise<{ credential_id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/db-credentials/${encodeURIComponent(credentialId)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ credential_id: string; status: string }>(response);
  }

  // ── 服务器连接管理 API（DESIGN_config_credential_refactor_v1 §8.1）──
  async listConnections(): Promise<{ connections: ConnectionInfo[] }> {
    const response = await fetch(`${this.baseUrl}/api/connections`);
    return parseJsonResponse<{ connections: ConnectionInfo[] }>(response);
  }

  async upsertConnection(connection: ConnectionInput): Promise<{ id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/connections`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(connection),
    });
    return parseJsonResponse<{ id: string; status: string }>(response);
  }

  async deleteConnection(connId: string): Promise<{ id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/connections/${encodeURIComponent(connId)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ id: string; status: string }>(response);
  }

  async testConnection(connId: string): Promise<ConnectionTestResult> {
    const response = await fetch(`${this.baseUrl}/api/connections/${encodeURIComponent(connId)}/test`, {
      method: 'POST',
    });
    return parseJsonResponse<ConnectionTestResult>(response);
  }

  // ── 统一定时计划 API（DESIGN_config_credential_refactor_v1 §8.2）──
  async listSchedules(): Promise<{ schedules: ScheduleInfo[] }> {
    const response = await fetch(`${this.baseUrl}/api/schedules`);
    return parseJsonResponse<{ schedules: ScheduleInfo[] }>(response);
  }

  async upsertSchedule(schedule: ScheduleInput): Promise<{ name: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/schedules`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(schedule),
    });
    return parseJsonResponse<{ name: string; status: string }>(response);
  }

  async deleteSchedule(name: string): Promise<{ name: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/schedules/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ name: string; status: string }>(response);
  }

  // ── 日志拉取任务 API（路径不变，行为改造：connection_id 引用连接对象）──
  async listLogPullSources(): Promise<{ sources: LogPullSourceInfo[]; log_source_ids: string[] }> {
    const response = await fetch(`${this.baseUrl}/api/log-pull/sources`);
    return parseJsonResponse<{ sources: LogPullSourceInfo[]; log_source_ids: string[] }>(response);
  }

  async upsertLogPullSource(source: LogPullSourceInput): Promise<{ id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/log-pull/sources`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(source),
    });
    return parseJsonResponse<{ id: string; status: string }>(response);
  }

  async deleteLogPullSource(sourceId: string): Promise<{ id: string; status: string; removed_schedules: number }> {
    const response = await fetch(`${this.baseUrl}/api/log-pull/sources/${encodeURIComponent(sourceId)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ id: string; status: string; removed_schedules: number }>(response);
  }

  // ── 本地日志目录 API（patrol.yaml log_sources 白名单 CRUD）──
  async listLogSourceDirs(): Promise<{ log_sources: LogSourceDirInfo[] }> {
    const response = await fetch(`${this.baseUrl}/api/log-sources`);
    return parseJsonResponse<{ log_sources: LogSourceDirInfo[] }>(response);
  }

  async upsertLogSourceDir(dir: LogSourceDirInput): Promise<{ id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/log-sources`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(dir),
    });
    return parseJsonResponse<{ id: string; status: string }>(response);
  }

  async deleteLogSourceDir(sourceId: string): Promise<{ id: string; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/log-sources/${encodeURIComponent(sourceId)}`, {
      method: 'DELETE',
    });
    return parseJsonResponse<{ id: string; status: string }>(response);
  }

  async testProvider(providerId: string, mode: 'api' | 'token' = 'api'): Promise<ProviderTestResult> {
    const response = await fetch(`${this.baseUrl}/api/providers/${encodeURIComponent(providerId)}/test?mode=${mode}`);
    return parseJsonResponse<ProviderTestResult>(response);
  }

  async fetchProviderModels(providerId: string): Promise<{ ok: boolean; models: Array<{ id: string }>; error: string | null }> {
    const response = await fetch(`${this.baseUrl}/api/providers/${encodeURIComponent(providerId)}/fetch-models`);
    return parseJsonResponse<{ ok: boolean; models: Array<{ id: string }>; error: string | null }>(response);
  }

  // ── 用量统计 API（P2-4）──
  // 后端返回 flat 数组：{days, provider_id, summary: [{day, provider_id, input_tokens, output_tokens, cost_usd, node_count}]}
  // 前端聚合为 {total_tokens, total_cost_usd, by_provider, by_date}
  async getUsageSummary(days = 30): Promise<UsageSummary> {
    const response = await fetch(`${this.baseUrl}/api/usage/summary?days=${days}`);
    const raw = await parseJsonResponse<{
      days: number;
      provider_id: string | null;
      summary: Array<{
        day: string;
        provider_id: string;
        input_tokens: number;
        output_tokens: number;
        cost_usd: number;
        node_count: number;
      }>;
    }>(response);

    let totalTokens = 0;
    let totalCost = 0;
    const byProviderMap = new Map<string, { tokens: number; cost_usd: number }>();
    const byDate: Array<{ date: string; provider_id: string; tokens: number; cost_usd: number }> = [];

    for (const row of raw.summary) {
      const tokens = (row.input_tokens ?? 0) + (row.output_tokens ?? 0);
      const cost = row.cost_usd ?? 0;
      totalTokens += tokens;
      totalCost += cost;

      const prev = byProviderMap.get(row.provider_id) ?? { tokens: 0, cost_usd: 0 };
      prev.tokens += tokens;
      prev.cost_usd += cost;
      byProviderMap.set(row.provider_id, prev);

      byDate.push({
        date: row.day,
        provider_id: row.provider_id,
        tokens,
        cost_usd: cost,
      });
    }

    const by_provider = Array.from(byProviderMap.entries()).map(([provider_id, v]) => ({
      provider_id,
      tokens: v.tokens,
      cost_usd: v.cost_usd,
    }));

    return {
      days: raw.days,
      total_tokens: totalTokens,
      total_cost_usd: totalCost,
      by_provider,
      by_date: byDate,
    };
  }

  // ── 用量多维度穿透（监控中心汇总卡片展开）──
  async getUsageBreakdown(days = 30): Promise<UsageBreakdown> {
    const response = await fetch(`${this.baseUrl}/api/usage/breakdown?days=${days}`);
    return parseJsonResponse<UsageBreakdown>(response);
  }

  // ── 监控中心 API ──

  /** 获取所有 Provider 的额度状态（用于额度面板） */
  async getQuotaStatus(): Promise<QuotaStatus> {
    const r = await fetch(`${this.baseUrl}/api/usage/quota-status`);
    return parseJsonResponse<QuotaStatus>(r);
  }

  /** 获取所有 Agent 的实时状态（用于 Agent 卡片网格） */
  async getAgentsStatus(): Promise<AgentsStatus> {
    const r = await fetch(`${this.baseUrl}/api/monitor/agents-status`);
    return parseJsonResponse<AgentsStatus>(r);
  }

  /** 拉取最近运行的 run 列表（用于运行中任务列表） */
  async listRuns(filters: { status?: string; limit?: number } = {}): Promise<{ runs: Array<Record<string, unknown>>; count: number; total: number }> {
    const params = new URLSearchParams();
    if (filters.status) params.set('status', filters.status);
    if (filters.limit != null) params.set('limit', String(filters.limit));
    const qs = params.toString() ? `?${params}` : '';
    const r = await fetch(`${this.baseUrl}/api/agent/runs${qs}`);
    return parseJsonResponse(r);
  }

  /** 拉取最近的 tips 列表（用于 TipsStream 初始化） */
  async listTips(limit = 20): Promise<{ tips: Tip[] }> {
    const r = await fetch(`${this.baseUrl}/api/monitor/tips?limit=${limit}`);
    return parseJsonResponse<{ tips: Tip[] }>(r);
  }

  // ======== /api/knowledge/* ========

  // --- Vault 浏览（5 个）---
  async listVaultFiles(path: string, extFilter?: string[]): Promise<{ entries: VaultEntry[]; total: number; truncated: boolean }> {
    const params = new URLSearchParams({ path });
    if (extFilter?.length) params.set('ext_filter', extFilter.join(','));
    const response = await fetch(`${this.baseUrl}/api/knowledge/vault/list?${params}`);
    return parseJsonResponse(response);
  }

  async readVaultFile(path: string): Promise<{ path: string; content: string; format: string; extracted_by: string; size: number; frontmatter?: Record<string, unknown> }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/vault/read?path=${encodeURIComponent(path)}`);
    return parseJsonResponse(response);
  }

  async validateVaultPath(path: string): Promise<{ path: string; read_allowed: boolean; write_allowed: boolean; reason: string }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/vault/validate?path=${encodeURIComponent(path)}`);
    return parseJsonResponse(response);
  }

  async searchVault(query: string, searchType: 'keyword' | 'tag' = 'keyword', maxResults = 100): Promise<{ matches: Array<{ path: string; line?: number; context?: string; tags?: string[] }>; total: number }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/vault/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, search_type: searchType, max_results: maxResults }),
    });
    return parseJsonResponse(response);
  }

  async getVaultStats(): Promise<{ total_dirs: number; total_files: number; by_ext: Record<string, number>; last_scan_at: string }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/vault/stats`);
    return parseJsonResponse(response);
  }

  // --- 知识库仪表盘（4 个）---
  async listKnowledgeDomains(): Promise<{ domains: DomainSummary[] }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/domains`);
    return parseJsonResponse(response);
  }

  async getKnowledgeDomain(domain: string): Promise<DomainDetail> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/domains/${encodeURIComponent(domain)}`);
    return parseJsonResponse(response);
  }

  async listDomainFiles(domain: string, category?: string): Promise<{ files: Array<{ path: string; size: number; mtime: string; frontmatter?: Record<string, unknown> }>; total: number }> {
    const params = category ? `?category=${encodeURIComponent(category)}` : '';
    const response = await fetch(`${this.baseUrl}/api/knowledge/domains/${encodeURIComponent(domain)}/files${params}`);
    return parseJsonResponse(response);
  }

  async readDomainFile(domain: string, path: string): Promise<{ path: string; content: string; frontmatter?: Record<string, unknown>; size: number }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/domains/${encodeURIComponent(domain)}/file?path=${encodeURIComponent(path)}`);
    return parseJsonResponse(response);
  }

  // --- Lint 处置（3 个）---
  async triggerLint(domain: string, payload: { check_types?: string[]; auto_fix?: boolean }): Promise<{ checked_at: string; issues: LintIssue[]; auto_fixed: number; needs_human_review: number; new_issues: number; updated_issues: number }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/domains/${encodeURIComponent(domain)}/lint`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return parseJsonResponse(response);
  }

  async listLintIssues(params: { domain: string; status?: string; type?: string; severity?: string; limit?: number; offset?: number }): Promise<{ issues: LintIssue[]; total: number }> {
    const qs = new URLSearchParams({ domain: params.domain });
    if (params.status) qs.set('status', params.status);
    if (params.type) qs.set('type', params.type);
    if (params.severity) qs.set('severity', params.severity);
    if (params.limit) qs.set('limit', String(params.limit));
    if (params.offset) qs.set('offset', String(params.offset));
    const response = await fetch(`${this.baseUrl}/api/knowledge/domains/${encodeURIComponent(params.domain)}/lint/issues?${qs}`);
    return parseJsonResponse(response);
  }

  async resolveLintIssue(issueId: string, payload: { action: 'resolve' | 'ignore' | 'fix' | 'reopen'; note?: string }): Promise<{ ok: boolean; issue: LintIssue }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/lint-issues/${encodeURIComponent(issueId)}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return parseJsonResponse(response);
  }

  // --- Agent 触发（2 个）---
  async scanDrafts(payload: { since?: string; draft_root?: string }): Promise<{ scanned_at: string; new_drafts: Array<{ path: string; size: number; mtime: string; title: string }>; total_new: number; already_processed: number }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/scan-drafts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return parseJsonResponse(response);
  }

  async curateContent(payload: { draft_paths?: string[]; since?: string }): Promise<{ run_id: string; workflow_id: string; status: string; stream_url: string }> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/curate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return parseJsonResponse(response);
  }

  // --- 智能问答（1 个）---
  async askKnowledge(question: string, domain?: string): Promise<AskResult> {
    const response = await fetch(`${this.baseUrl}/api/knowledge/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, domain: domain ?? null }),
    });
    return parseJsonResponse<AskResult>(response);
  }

  // ── P0.18.7 Workspace 授权 CRUD + tier 校验 ──

  /** 列出所有授权 workspace（enabled=1）。include_disabled=true 含已取消授权。 */
  async listWorkspaces(includeDisabled = false): Promise<{ workspaces: AuthorizedWorkspace[]; count: number }> {
    const response = await fetch(`${this.baseUrl}/api/workspaces?include_disabled=${includeDisabled ? 'true' : 'false'}`);
    return parseJsonResponse<{ workspaces: AuthorizedWorkspace[]; count: number }>(response);
  }

  /** 新增授权 workspace。 */
  async createWorkspace(payload: CreateWorkspacePayload): Promise<{ workspace: AuthorizedWorkspace; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/workspaces`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return parseJsonResponse<{ workspace: AuthorizedWorkspace; status: string }>(response);
  }

  /** 获取 onboarding 状态。 */
  async getOnboardingStatus(): Promise<{ onboarded: boolean; manager_default_workspace_id: string | null }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/onboarding`);
    return parseJsonResponse<{ onboarded: boolean; manager_default_workspace_id: string | null }>(response);
  }

  /** 首次引导：只传源路径，后端自动建子目录 + 授权 + 绑定 + 标记 onboarded。 */
  async createDefaultWorkspace(sourcePath: string): Promise<{
    status: string;
    workspace: AuthorizedWorkspace;
    manager_default_workspace_id: string;
    subdirs_created: string[];
  }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/onboarding/create-default`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_path: sourcePath }),
    });
    return parseJsonResponse(response);
  }

  /** 目录浏览器：列出指定路径下的子目录（供前端目录选择器使用）。 */
  async browseDirs(path?: string): Promise<{
    current: string;
    parent: string | null;
    entries: { name: string; path: string; is_dir: boolean }[];
    drives: string[];
  }> {
    const url = path
      ? `${this.baseUrl}/api/runtime/browse-dirs?path=${encodeURIComponent(path)}`
      : `${this.baseUrl}/api/runtime/browse-dirs`;
    const response = await fetch(url);
    return parseJsonResponse(response);
  }

  /**
   * 调用 host 端原生文件夹选择对话框，返回绝对路径。
   *
   * 对齐 deepseek harness 的 native backend 思路：浏览器无法直接获取本地
   * 绝对路径，必须由后端弹出 OS-native 对话框（PowerShell+IFileOpenDialog /
   * osascript / zenity）把绝对路径返回给前端。
   *
   * 适用场景：后端与用户在**同一台机器**上运行（v0.18+ 默认部署）。
   * 远程部署场景请改用 browseDirs() + DirBrowser 组件。
   */
  async pickFolder(initialDir?: string | null): Promise<{
    cancelled: boolean;
    path: string | null;
    error?: string;
  }> {
    const response = await fetch(`${this.baseUrl}/api/system/pick-folder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial_dir: initialDir ?? null }),
    });
    return parseJsonResponse(response);
  }

  /** 探测 host 平台是否支持原生文件夹选择对话框（前端用于按需显示按钮）。 */
  async isNativePickerSupported(): Promise<{ supported: boolean }> {
    const response = await fetch(`${this.baseUrl}/api/system/native-picker-supported`);
    return parseJsonResponse<{ supported: boolean }>(response);
  }

  /** 完成 onboarding：绑定 manager 默认工作区（旧路径，createDefaultWorkspace 已合并此步）。 */
  async completeOnboarding(workspaceId: string): Promise<{ status: string; manager_default_workspace_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/onboarding/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manager_default_workspace_id: workspaceId }),
    });
    return parseJsonResponse<{ status: string; manager_default_workspace_id: string }>(response);
  }

  /** 获取 manager 默认工作区详情。 */
  async getManagerWorkspace(): Promise<{ workspace: AuthorizedWorkspace | null }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/manager-workspace`);
    return parseJsonResponse<{ workspace: AuthorizedWorkspace | null }>(response);
  }

  /** 设置/更换 manager 默认工作区。 */
  async setManagerWorkspace(workspaceId: string): Promise<{ status: string; manager_default_workspace_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/manager-workspace`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manager_default_workspace_id: workspaceId }),
    });
    return parseJsonResponse<{ status: string; manager_default_workspace_id: string }>(response);
  }

  /** 获取单个 workspace 详情。 */
  async getWorkspace(workspaceId: string): Promise<{ workspace: AuthorizedWorkspace }> {
    const response = await fetch(`${this.baseUrl}/api/workspaces/${encodeURIComponent(workspaceId)}`);
    return parseJsonResponse<{ workspace: AuthorizedWorkspace }>(response);
  }

  /** 更新 workspace 字段（display_name / permissions / enabled）。 */
  async updateWorkspace(workspaceId: string, payload: UpdateWorkspacePayload): Promise<{ workspace: AuthorizedWorkspace; status: string }> {
    const response = await fetch(`${this.baseUrl}/api/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return parseJsonResponse<{ workspace: AuthorizedWorkspace; status: string }>(response);
  }

  /** soft delete：enabled=0 + deauthorized_at=now。 */
  async deleteWorkspace(workspaceId: string): Promise<{ status: string; workspace_id: string }> {
    const response = await fetch(`${this.baseUrl}/api/workspaces/${encodeURIComponent(workspaceId)}`, { method: 'DELETE' });
    return parseJsonResponse<{ status: string; workspace_id: string }>(response);
  }

  /** 测试 workspace 访问：返回 {exists, readable, writable, execuable}。 */
  async testWorkspaceAccess(workspaceId: string): Promise<WorkspaceAccessTestResult> {
    const response = await fetch(`${this.baseUrl}/api/workspaces/${encodeURIComponent(workspaceId)}/test`, { method: 'POST' });
    return parseJsonResponse<WorkspaceAccessTestResult>(response);
  }

  /** 为指定 run 准备 sandbox。 */
  async prepareRunWorkspace(runId: string, workspaceId: string): Promise<PrepareRunWorkspaceResult> {
    const response = await fetch(`${this.baseUrl}/api/runs/${encodeURIComponent(runId)}/workspace/prepare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workspace_id: workspaceId }),
    });
    return parseJsonResponse<PrepareRunWorkspaceResult>(response);
  }

  /** 获取当前 session 绑定的 workspace（前端 status bar 显示）。 */
  async getCurrentWorkspace(sessionId: string | null): Promise<{ workspace_id: string | null; workspace: AuthorizedWorkspace | null; permission_level: string | null }> {
    const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
    const response = await fetch(`${this.baseUrl}/api/runtime/workspace${qs}`);
    return parseJsonResponse<{ workspace_id: string | null; workspace: AuthorizedWorkspace | null; permission_level: string | null }>(response);
  }

  /** 列出所有 enabled=1 workspace 简要（前端 status bar dropdown 用）。 */
  async listRuntimeWorkspaces(): Promise<{ workspaces: WorkspaceRuntimeBrief[]; count: number }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/workspaces`);
    return parseJsonResponse<{ workspaces: WorkspaceRuntimeBrief[]; count: number }>(response);
  }

  /** P0.18.11: 手动触发 sandbox 延迟清理（Settings 立即清理入口）。 */
  async cleanupWorkspacesNow(): Promise<{ status: string; scanned: number; deleted: number; failed: number }> {
    const response = await fetch(`${this.baseUrl}/api/runtime/workspaces/cleanup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    return parseJsonResponse<{ status: string; scanned: number; deleted: number; failed: number }>(response);
  }
}

// ── Provider 管理类型（P2-2）──
export interface ProviderInfo {
  id: string;
  name: string;
  base_url: string;
  protocol: string;       // openai_compatible | anthropic_compatible 等
  auth_type: string;      // bearer | x-api-key 等
  has_credential: boolean;
}

// ── SSH 凭据类型（log-puller / ssh_exec 复用）──
export interface SshCredentialInfo {
  provider_id: string;    // 形如 "ssh:prod-seeyon"
  kind: string;           // 'ssh'
  created_at: string;
  updated_at: string;
}

// ── 服务器连接类型（DESIGN_config_credential_refactor_v1 §8.1）──
export interface ConnectionInfo {
  id: string;
  name: string;
  conn_type: string;            // ssh（默认）| mysql
  host: string;                 // 全量返回（编辑表单需要）
  port: number;
  username: string;
  database: string;             // mysql 连接专用：默认 schema
  auth_type: string;            // key | password（mysql 固定 password）
  credential_id: string;        // 归一化后形如 ssh:<id> / mysql:<id>
  credential_present: boolean;  // credential_store 中是否有值（不含明文）
  private_key_path: string;
  enabled: boolean;
  referenced_by: string[];      // 引用该连接的拉取任务 id
}

export interface ConnectionInput {
  id: string;
  name: string;
  conn_type?: string;                // ssh（默认）| mysql
  host: string;
  port: number;
  username: string;
  database?: string | null;          // mysql 连接专用
  auth_type: string;
  credential_id?: string | null;      // 空/None 由后端归一化为 <conn_type>:<id>
  private_key_path?: string | null;
  enabled: boolean;
}

export interface ConnectionTestResult {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
}

// ── 统一定时计划类型（DESIGN_config_credential_refactor_v1 §8.2）──
export interface ScheduleInfo {
  id: string;                    // 不可变主键，新建时由后端按 name slug 生成
  name: string;                  // 展示字段，可任意改
  workflow_id: string;
  cron: string;
  enabled: boolean;
  inputs: Record<string, unknown>;
  next_run: string | null;      // disabled 时为 null
}

export interface ScheduleInput {
  id?: string | null;            // 新建留空；编辑时必须带原 id（不可变）
  name: string;
  workflow_id: string;
  cron: string;
  enabled: boolean;
  inputs: Record<string, unknown>;
}

// ── 日志拉取任务类型（connection_id 引用连接对象，host 后端脱敏）──
export interface LogPullSourceInfo {
  id: string;
  name: string;
  connection_id: string;
  connection: { name: string; host_masked: string } | null;
  remote_paths: string[];
  local_log_source_id: string;  // log_sources 白名单 id
  local_max_days: number;
  enabled: boolean;
  schedules: Array<{ name: string; cron: string; enabled: boolean; next_run: string | null }>;
}

export interface LogPullSourceInput {
  id: string;
  name: string;
  connection_id: string;
  remote_paths: string[];
  local_log_source_id: string;
  local_max_days: number;
  enabled: boolean;
}

// ── 本地日志目录类型（patrol.yaml log_sources 白名单条目）──
export interface LogSourceDirInfo {
  id: string;
  name: string;
  path: string;                 // 本地存储目录
  description: string;
  allow_read: boolean;
  allow_list: boolean;
  referenced_by: string[];      // 引用该目录的拉取任务 id
}

export interface LogSourceDirInput {
  id: string;
  name: string;
  path: string;
  description?: string;
  allow_read?: boolean;
  allow_list?: boolean;
}

// ── 统一运行时类型 ──
export interface HarnessInfo {
  type: string;
  label: string;
}

export interface RuntimeProviderInfo {
  provider_id: string;
  base_url: string;
  protocol: string;
  auth_type: string;
  models: Array<ModelInfo>;
  has_env_key: boolean;
  has_credential: boolean;
  credential_updated_at: string | null;
}

export interface ModelInfo {
  id: string;
  max_tokens?: number;
  price_input_per_1k?: number;
  price_output_per_1k?: number;
}

export interface FallbackChainEntry {
  /** fallback 目标 provider_id。 */
  provider: string;
  /** 显式切换到的 model id；null = 使用该 provider 默认 model。 */
  model: string | null;
}

export interface RuntimeSummary {
  harnesses: HarnessInfo[];
  providers: RuntimeProviderInfo[];
  /** v2 shape：每条链是 [{provider, model?}, ...]，model 为 null 表示使用 provider 默认 model。 */
  fallback_chains: Record<string, FallbackChainEntry[]>;
  default_provider: string;
  default_model: string;
  manager_provider: string;
  manager_model: string;
}

// ── Docker runtime types ──
export interface DockerContainerInfo {
  id: string;
  short_id: string;
  name: string;
  image: string;
  status: string;
  labels: Record<string, string> | null;
}

// ── P0.17 Runtime Environment types ──
export type DockerStatusKind = 'ready' | 'checking' | 'error' | 'unknown';
export type DockerReasonCode =
  | null
  | 'docker_cli_missing'
  | 'docker_daemon_unavailable'
  | 'docker_permission_denied'
  | 'docker_linux_engine_required'
  | 'docker_version_too_old'
  | 'docker_error';

export interface DockerStatus {
  status: DockerStatusKind;
  version: string | null;
  platform: string | null;
  reason_code: DockerReasonCode;
  reason: string | null;
}

export type WorkerImageStatusKind =
  | 'ready'
  | 'stale'
  | 'missing'
  | 'incompatible'
  | 'build_failed'
  | 'building'
  | 'checking';
export type WorkerImageReasonCode =
  | null
  | 'worker_image_missing'
  | 'worker_image_stale'
  | 'worker_image_incompatible'
  | 'worker_image_build_failed'
  | 'docker_error';

export interface WorkerImageStatus {
  status: WorkerImageStatusKind;
  image_id: string | null;
  tag: string | null;
  version: string | null;
  protocol_version: string | null;
  source_fingerprint: string;
  compatibility: 'current' | 'stale' | 'incompatible' | 'unknown';
  reason_code: WorkerImageReasonCode;
  reason: string | null;
}

export interface WorkerImageInfo {
  id: string;
  short_id: string;
  tags: string[];
  source_fingerprint: string;
  protocol_version: string;
  version: string;
  size_bytes: number;
  /** 每次 docker build 都会通过 `--label agentops.built_at` 更新，是真实的"构建时间"
   *  后端新字段；旧后端响应没有此字段，应 fallback 到 labels["agentops.built_at"] */
  built_at?: string;
  /** docker 镜像层创建时间（FROM 基础镜像的时间，CACHED 时不变，不可作为"构建时间"依据） */
  created_at: string;
  /** docker 镜像 labels（包含 agentops.built_at 等元信息，旧后端就有） */
  labels?: Record<string, string>;
  selected: boolean;
}

export interface SourceStatus {
  available: boolean;
  fingerprint: string;
  git_status: 'clean' | 'dirty';
}

export type BuildStatusKind = 'idle' | 'queued' | 'running' | 'completed' | 'failed';
export interface BuildStatus {
  build_id: string | null;
  status: BuildStatusKind;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
}

export interface ConnectedWorker {
  subagent_id: string;
  worker_id: string;
  runtime_placement: string;
  container_id: string | null;
  status: string;
  started_at: string;
  lease_generation: number;
  run_id: string;
  node_id: string;
  actor_id: string;
}

export type OverallKind =
  | 'ready'
  | 'checking'
  | 'stale'
  | 'missing'
  | 'incompatible'
  | 'build_failed'
  | 'docker_error'
  | 'building';

export interface RuntimeEnvironmentSnapshot {
  docker: DockerStatus;
  worker_image: WorkerImageStatus;
  images: WorkerImageInfo[];
  source: SourceStatus;
  build: BuildStatus;
  connected_workers: number;
  workers: ConnectedWorker[];
  overall: OverallKind;
}

export interface ProviderHealthResult {
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

export interface ProviderTestResult {
  provider_id: string;
  status: 'ok' | 'error';
  latency_ms?: number;
  error?: string;
  mode?: string;
  detail?: string;
}

// ── 用量统计类型（P2-4）──
export interface UsageSummary {
  days: number;
  total_tokens: number;
  total_cost_usd: number;
  by_provider: Array<{
    provider_id: string;
    tokens: number;
    cost_usd: number;
  }>;
  by_date: Array<{
    date: string;             // YYYY-MM-DD
    provider_id: string;
    tokens: number;
    cost_usd: number;
  }>;
}

/** 多维度用量穿透（单个维度行） */
export interface UsageBreakdownRow {
  dim: string;
  tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  cost_usd: number;
  run_count: number;
  provider_id?: string;       // by_model 维度额外携带
}

export interface UsageBreakdown {
  days: number;
  by_workflow: UsageBreakdownRow[];
  by_agent: UsageBreakdownRow[];
  by_provider: UsageBreakdownRow[];
  by_model: UsageBreakdownRow[];
}

// ── 监控中心类型 ──

/** 单个 Provider 的额度状态 */
export interface QuotaProvider {
  provider_id: string;
  display_name: string;
  window_hours: number;
  total_tokens: number;
  used_tokens: number;
  percentage: number;
  earliest_record_at: string;  // ISO
  reset_at: string;             // ISO
  reset_in_seconds: number;
  models: string[];
  alert_level: 'normal' | 'yellow' | 'red';
}

/** 额度状态汇总 */
export interface QuotaStatus {
  providers: QuotaProvider[];
  alert_thresholds: { yellow: number; red: number };
}

/** 单个 Agent 的实时状态 */
export interface AgentStatus {
  agent_id: string;
  display_name: string;
  domain: string;
  harness: string;
  model: string;
  status: 'idle' | 'running' | 'error';
  running_tasks: number;
  current_task: {
    run_id: string;
    workflow_id: string;
    started_at: string;
    current_node: string;
  } | null;
  stats: {
    total_runs: number;
    completed: number;
    failed: number;
    last_run_at: string | null;
    last_run_status: string | null;
  };
}

/** Agent 状态汇总 */
export interface AgentsStatus {
  agents: AgentStatus[];
  running_count: number;
}

/** 动态 Tip（监控中心通知） */
export interface Tip {
  id: string;
  type: 'task_started' | 'task_progress' | 'task_completed' | 'task_failed' | 'patrol_alert' | 'validation_result' | 'quota_warning';
  severity: 'info' | 'warning' | 'error' | 'success';
  agent_id?: string;
  run_id?: string;
  title: string;
  message: string;
  timestamp: string;
}

// ======== /api/knowledge/* 类型 ========
// DomainSummary 是仪表盘列表项的最小类型。
// 后端从 config/knowledge/domains.yaml 读取的元数据通过可选项暴露，
// 旧字段（id/name/page_count/last_ingest_at/lint_summary/supports_lint）保留用于向后兼容。
export interface DomainSummary {
  id: string;
  // 旧字段 · 后端仍返回（与 display_name 同值或 fallback 到 id）
  name: string;
  // 核心 stats · 实时计算
  page_count: number;
  last_ingest_at: string | null;
  lint_summary: {
    total: number;
    critical: number;
    warning: number;
    info: number;
  };
  supports_lint: boolean;
  // 新字段 · 从 config/knowledge/domains.yaml 读取（2026-07-18 kb_config 可配置化）
  domain_id?: string;           // 等同 id；与后端新返回字段对齐
  display_name?: string;        // 中文友好名称（如「周报助手知识库」）
  description?: string;         // 知识域描述
  kb_root?: string;             // 后端 KB 目录（相对项目根）
  vault_write_dir?: string | null;  // vault 写入目录（相对 vault root，null = 不写）
  schema?: 'llm_wiki' | 'video_production';
  categories?: string[];        // 支持的 category 列表
  category_layout?: Record<string, 'single_file' | 'directory'>;
  bound_agents?: string[];      // 绑定的 agent id 列表
  note?: string;                // 备注（仅异构 domain 有）
  exists?: boolean;             // kb_root 目录是否实际存在
}

export interface DomainDetail extends DomainSummary {
  description: string;
  agents_md: string;
  index_md: string;
  log_md: string;
  by_category: {
    raw: number;
    entities: number;
    concepts: number;
    comparisons: number;
  };
  recent_ingests: Array<{
    timestamp: string;
    action: string;
    page: string;
  }>;
}

export interface LintIssue {
  id: string;
  domain: string;
  type: 'contradictions' | 'orphans' | 'missing_pages' | 'stale' | 'index_sync' | 'dead_links';
  severity: 'critical' | 'warning' | 'info';
  page_a: string | null;
  page_b: string | null;
  description: string;
  auto_fixable: boolean;
  detected_at: string;
  status: 'pending' | 'resolved' | 'ignored';
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_note: string | null;
}

export interface VaultEntry {
  name: string;
  type: 'file' | 'dir';
  path: string;
  size?: number;
  mtime?: string;
  ext?: string;
}

// ── 智能问答类型 ──
export interface AskCitation {
  path: string;
  snippet: string;
}

export interface AskResult {
  answer: string;
  citations: AskCitation[];
  matched_documents: number;
  elapsed_ms: number;
}

export const apiClient = new ApiClient();

// Re-export DagEvent for convenience
export type { DagEvent } from "./types";
export type { CollaborationGraph, LaneInfo, GraphNode, GraphEdge, HandoffInfo, TimelineEntry } from "./types";
export type { SessionInfo, SessionRunInfo, SessionMemoryInfo } from "./types";

// ── v99.5 P0.2 生成式 UI Surface State（与后端 SurfaceState 一一对应）──
// 后端：orchestrator/protocol.py SurfaceState（to_payload/from_payload round-trip）
// 工具：tools/report_surface_state.py（DagEngine 注入到 agent tools）
// 事件：DagEventType.REPORT_SURFACE_STATE = "report_surface_state"

export type SurfacePhase = "started" | "partial" | "final" | "superseded";

/** 4 种 contract 类别（与 ActorVisualProfile.output_contract 对齐）。 */
export type OutputContract = "ActorReport" | "Mission" | "Failure" | "RoundGate";

/** 与后端 SurfaceState dataclass 字段一一对应。 */
export interface SurfaceState {
  /** 身份派生 sha256(run_id + actor_id + view_id + generation)，Worker 注入（模型不可覆盖）。
   * report_surface_state / upsert_generated_view：per (actor, view) 稳定 → 一张卡演进；
   * present_content：每次调用（未传 widget_id）新 surface_id → 累积多张卡。 */
  surface_id: string;
  /** view_id（actor 的 allowed_surface_views 白名单内）。 */
  view_id: string;
  /** phase（单调推进：started → partial → final → superseded）。 */
  phase: SurfacePhase;
  /** A2UI 组件树（A2uiComponentV1[] 的 plain JSON 形态）。 */
  components: Array<Record<string, unknown>>;
  /** 字段数据（符合 view 字段约束）。 */
  data_model: Record<string, unknown>;
  /** catalog ID（与 orchestrator/protocol.py SurfaceState 默认一致）：https://agentops.dev/a2ui/catalogs/core/v1 */
  catalog_id?: string;
  /** 可选展示属性 {iconUrl, agentDisplayName}。 */
  surface_properties?: { iconUrl?: string; agentDisplayName?: string };
  /** output_contract（与 view.output_contract 一致）。 */
  output_contract?: OutputContract | string;
  /** OPT-1: surface 来源（agent=LLM 主动 emit / system=DAG 事件确定性投影骨架）。 */
  source?: "agent" | "system";
  /** ISO 时间戳。 */
  emitted_at?: string;
  /** 同 surface 内单调递增 patch 序号。
   * 序号由后端 Worker 派生（无注入向量），reducer 不做拒绝——排序守卫仍由 phase 单调承担。 */
  patch_sequence?: number;
}

/** DagEvent payload 中的 surface_state 字段形状。 */
export interface ReportSurfaceStateEvent {
  /** session_id 或 run_id（前端据此路由到具体会话的 SupervisionPanel）。 */
  session_id?: string;
  run_id?: string;
  /** 当前节点的 actor_id（用于视图归属）。 */
  actor_id?: string;
  /** 节点 ID（用于关联到 DAG 节点）。 */
  node_id?: string;
  /** 推送的 SurfaceState。 */
  surface_state: SurfaceState;
  occurred_at?: string;
}

/** SupervisionPanel 内部状态：按 view_id 聚合的最新 snapshot。 */
export interface SupervisionSnapshot {
  view_id: string;
  actor_id: string;
  surface_id: string;
  phase: SurfacePhase;
  emitted_at: string;
  surface_state: SurfaceState;
}

// ═══════════════════════════════════════════════════════════════
//  P0.18.7 Workspace 授权模型类型
// ═══════════════════════════════════════════════════════════════

/** workspace mode：local_copy / bind_mount / git_clone / isolated */
export type WorkspaceMode = 'local_copy' | 'bind_mount' | 'git_clone' | 'isolated';

/** workspace permissions：read_only (T1) / read_write (T2) / read_write_exec (T3) */
export type WorkspacePermissions = 'read_only' | 'read_write' | 'read_write_exec';

/**
 * 会话级权限级别（与 workspace 解耦）：
 * read_only (T1 只读) / read_write (T2 读写) / read_write_exec (T3 读写+执行) / full_access (T4 完全访问)
 * 创建会话时从 workspace.permissions 初始化，之后可随时独立切换。
 */
export type PermissionLevel = 'read_only' | 'read_write' | 'read_write_exec' | 'full_access';

/** Tier 等级：T0 通用对话 / T1 只读 / T2 读写 / T3 读写+执行 / T4 完全访问 */
export type AgentTier = 'T0' | 'T1' | 'T2' | 'T3' | 'T4';

/** authorized_workspaces 表记录。 */
export interface AuthorizedWorkspace {
  workspace_id: string;
  display_name: string;
  description?: string | null;
  mode: WorkspaceMode;
  source_path?: string | null;
  git_url?: string | null;
  git_branch?: string | null;
  permissions: WorkspacePermissions;
  authorized_at: string;
  last_used_at?: string | null;
  usage_count: number;
  enabled: number;        // 0/1（SQLite bool）
  deauthorized_at?: string | null;
  extra?: string | null;  // JSON string
}

/** 创建 workspace 的 payload。 */
export interface CreateWorkspacePayload {
  display_name: string;
  mode: WorkspaceMode;
  permissions: WorkspacePermissions;
  description?: string;
  source_path?: string;       // local_copy / bind_mount 必填
  git_url?: string;           // git_clone 必填
  git_branch?: string;
  extra?: string;
}

/** 更新 workspace 的 payload（所有字段可选）。 */
export interface UpdateWorkspacePayload {
  display_name?: string;
  description?: string;
  permissions?: WorkspacePermissions;
  enabled?: 0 | 1;
}

/** workspace 访问测试结果。 */
export interface WorkspaceAccessTestResult {
  exists: boolean;
  readable: boolean;
  writable: boolean;
  execuable: boolean;
  skipped?: boolean;
  reason?: string;
  source_path?: string;
}

/** prepare run workspace 返回结果。 */
export interface PrepareRunWorkspaceResult {
  workspace_root: string;
  workspace_mode: WorkspaceMode;
  authorized_workspace_id: string;
  permissions: WorkspacePermissions;
}

/** status bar dropdown 简要 workspace。 */
export interface WorkspaceRuntimeBrief {
  workspace_id: string;
  display_name: string;
  mode: WorkspaceMode;
  permissions: WorkspacePermissions;
  source_path?: string | null;
  last_used_at?: string | null;
}

/**
 * workspace permissions → tier 上限映射（与后端 workspace_paths.py WorkspaceConfig.tier 一致）。
 * read_only → T1, read_write → T2, read_write_exec → T3，其余 → T0
 */
const PERMISSIONS_TO_TIER: Record<string, AgentTier> = {
  read_only: 'T1',
  read_write: 'T2',
  read_write_exec: 'T3',
  full_access: 'T4',
};

const TIER_RANK: Record<AgentTier, number> = { T0: 0, T1: 1, T2: 2, T3: 3, T4: 4 };

/** workspace permissions 映射到 tier 上限。 */
export function workspacePermissionsToTier(permissions: WorkspacePermissions | string): AgentTier {
  return PERMISSIONS_TO_TIER[permissions] ?? 'T0';
}

/**
 * 校验 workspace tier 是否兼容 agent tier。
 * 规则：实际有效 tier = min(agent tier, workspace tier)。
 * 若 agent 要求的 tier > workspace 提供的 tier，则不兼容。
 */
export function isTierCompatible(workspaceTier: AgentTier, agentTier: AgentTier): boolean {
  return TIER_RANK[workspaceTier] >= TIER_RANK[agentTier];
}

/** 计算实际有效 tier = min(workspace tier, agent tier)。 */
export function effectiveTier(workspaceTier: AgentTier, agentTier: AgentTier): AgentTier {
  const wsRank = TIER_RANK[workspaceTier];
  const agentRank = TIER_RANK[agentTier];
  const rank = Math.min(wsRank, agentRank);
  return (Object.entries(TIER_RANK).find(([, r]) => r === rank)?.[0] ?? 'T0') as AgentTier;
}

/** tier 中文标签。 */
export const TIER_LABELS: Record<AgentTier, string> = {
  T0: 'T0 通用对话',
  T1: 'T1 只读',
  T2: 'T2 读写',
  T3: 'T3 读写+执行',
  T4: 'T4 完全访问',
};

/** mode 中文标签。 */
export const MODE_LABELS: Record<WorkspaceMode, string> = {
  local_copy: '复制到 sandbox',
  bind_mount: '绑定挂载',
  git_clone: 'Git 克隆',
  isolated: '隔离空目录',
};

/** permissions 中文标签。 */
export const PERMISSIONS_LABELS: Record<WorkspacePermissions, string> = {
  read_only: '只读',
  read_write: '读写',
  read_write_exec: '读写+执行',
};

/**
 * 会话级权限级别中文标签（用户面向）。
 * 与 workspace 的 PERMISSIONS_LABELS 区分：这里是"请求对话时选的权限"，
 * 用 Read Only / Workspace Write / Full Access 三档命名。
 */
export const PERMISSION_LEVEL_LABELS: Record<PermissionLevel, string> = {
  read_only: 'Read Only',
  read_write: 'Workspace Write',
  read_write_exec: 'Workspace Write+Exec',
  full_access: 'Full Access',
};

/** 会话级权限级别 → tier 映射（full_access → T4 绕过所有校验）。 */
export function permissionLevelToTier(level: PermissionLevel | string | null | undefined): AgentTier {
  if (!level) return 'T0';
  return PERMISSIONS_TO_TIER[level] ?? 'T0';
}
