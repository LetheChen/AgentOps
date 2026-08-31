// web/src/api/taskApi.ts
// P0 任务管理模块前端 API
// 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.9.6

import { API_BASE_URL } from '../lib/api';

export interface TaskProject {
  project_id: string;
  name: string;
  type: string;
  local_path?: string;
  workspace_id?: string;
  next_task_number: number;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface Task {
  task_id: string;
  project_id: string;
  identifier?: string;
  title: string;
  description?: string;
  status: string;
  task_type: string;
  risk_level: string;
  creator_type: string;
  thread_id?: string;
  sort_order: number;
  version: number;
  created_at: string;
  updated_at: string;
  closed_at?: string;
  // V1 新增字段
  parent_task_id?: string;
  source_idea_id?: string;
  assignee_type?: string;
  assignee_id?: string;
  assignee_name?: string;
  style_id?: string;
  terminal_session_id?: string;
  approved?: number | null;
  archived_at?: string;
}

export interface TaskTransition {
  to: string;
  action: string;
  requires_user: boolean;
}

// ====== V1 新增接口类型 ======
export interface TaskReport {
  report_id: string;
  task_id: string;
  agent_id: string;
  session_id?: string;
  terminal_session_id?: string;
  content: string;
  artifact_ids: string[];
  acceptance_self_check: Record<string, unknown>;
  submitted_at: string;
}

export interface TaskReportExport {
  export_id: string;
  path: string;
  sha256: string;
  size_bytes: number;
  format: 'md' | 'html' | 'json';
  exported_at: string;
  content_type: string;
}

export interface TaskExportVerify {
  verified: boolean;
  expected_sha256: string | null;
  actual_sha256: string | null;
  path: string;
  reason?: string;
}

export interface TaskComment {
  comment_id: string;
  task_id: string;
  report_id?: string;
  author_type: string;
  author_id?: string;
  author_name?: string;
  body: string;
  comment_type: string;
  decision?: string;
  rollback_target?: string;
  thread_id?: string;
  mentions?: string[];
  created_at: string;
  updated_at: string;
}

export interface TaskIdea {
  idea_id: string;
  project_id: string;
  source: string;
  source_ref?: string;
  content: string;
  tags: string[];
  status: string;
  confidence: string;
  priority: string;
  converted_task_id?: string;
  version: number;
  created_at: string;
}

export interface TaskRelation {
  relation_id: string;
  relation_type: string;
  source_task_id: string;
  target_task_id: string;
  created_at: string;
}

export interface AcceptanceCriteria {
  criteria_id: string;
  task_id: string;
  description: string;
  check_type: string;
  status: string;
  checked_at?: string;
  version: number;
}

export interface DocChangeProposal {
  proposal_id: string;
  doc_id: string;
  task_id: string;
  change_type: string;
  section_path?: string;
  old_content_hash?: string;
  new_content: string;
  rationale?: string;
  status: string;
  applied_at?: string;
  version: number;
  created_at: string;
}

export interface TaskActivity {
  activity_id: string;
  task_id: string;
  actor_type: string;
  actor_id?: string;
  actor_name?: string;
  changes: Record<string, unknown>;
  created_at: string;
}

export interface TaskArtifact {
  artifact_id: string;
  task_id: string;
  type: string;
  path?: string;
  content_hash?: string;
  description?: string;
  version: number;
  created_at: string;
}

// ====== V3 新增接口类型（§4.11/§4.12/§4.13） ======

export interface TaskDashboard {
  total: number;
  status_distribution: Record<string, number>;
  stage_funnel: Array<{ stage: string; count: number }>;
  blocked_summary: {
    count: number;
    tasks: Array<{
      task_id: string;
      identifier: string;
      title: string;
      pending_blockers: Array<{ task_id: string; identifier: string; status: string }>;
    }>;
  };
  ready_to_unblock: number;
  risk_exposure: Array<{
    task_id: string;
    identifier: string;
    title: string;
    risk_level: string;
    status: string;
  }>;
  today_digest: {
    created: number;
    closed: number;
    advanced: number;
    conductor_actions: number;
  };
}

export interface TerminalSession {
  terminal_session_id: string;
  task_id?: string;
  kind: 'agent' | 'codex' | 'claude' | 'shell';
  status: 'active' | 'done' | 'dead';
  created_at: string;
  ended_at?: string;
  // list 接口附带（agent 窗格显示任务摘要）
  task_title?: string;
  task_status?: string;
}

export interface TerminalLayoutPane {
  terminal_session_id: string;
  // 网格位置（列/行，自动排列时后端只存顺序）
  x?: number;
  y?: number;
  w?: number;
  h?: number;
}

export interface TerminalLayout {
  layout_id?: string;
  user_id?: string;
  panes: TerminalLayoutPane[];
  updated_at?: string;
}

async function _fetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const msg = await res.text();
    throw new Error(`Task API ${path} failed ${res.status}: ${msg}`);
  }
  return res.json();
}

export const taskApi = {
  // ====== 项目 ======
  listProjects: () => _fetch<{ projects: TaskProject[] }>('/api/tasks/projects'),

  createProject: (data: { name: string; type?: string; local_path?: string; workspace_id?: string }) =>
    _fetch<TaskProject>('/api/tasks/projects', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  // ====== 任务 ======
  listTasks: (projectId = '', status = '') => {
    const params = new URLSearchParams();
    if (projectId) params.set('project_id', projectId);
    if (status) params.set('status', status);
    const qs = params.toString();
    return _fetch<{ tasks: Task[]; revision: number }>(`/api/tasks${qs ? '?' + qs : ''}`);
  },

  createTask: (data: {
    project_id: string;
    title: string;
    description?: string;
    thread_id?: string;
    creator_id?: string;
    creator_name?: string;
    risk_level?: string;
    status?: string;
  }) => _fetch<Task>('/api/tasks', { method: 'POST', body: JSON.stringify(data) }),

  getTask: (taskId: string) =>
    _fetch<{ task: Task; stages: unknown[] }>(`/api/tasks/${taskId}`),

  updateTask: (taskId: string, ifVersion: number, fields: Record<string, unknown>) =>
    _fetch<Task>(`/api/tasks/${taskId}`, {
      method: 'PATCH',
      body: JSON.stringify({ if_version: ifVersion, ...fields }),
    }),

  advance: (taskId: string, targetStatus: string, ifVersion: number, extra?: {
    thread_id?: string; comment?: string; actor?: string;
  }) => _fetch<Task>(`/api/tasks/${taskId}/advance`, {
    method: 'POST',
    body: JSON.stringify({ target_status: targetStatus, if_version: ifVersion, ...extra }),
  }),

  getTransitions: (taskId: string) =>
    _fetch<{ task_id: string; status: string; transitions: TaskTransition[] }>(
      `/api/tasks/${taskId}/transitions`
    ),

  // ====== 版本号轮询 ======
  getRevision: () => _fetch<{ revision: number }>('/api/tasks/revision'),

  // ====== V2-W4：任务搜索 ======
  search: (q: string, projectId?: string, status?: string, limit?: number) => {
    const params = new URLSearchParams();
    params.set('q', q);
    if (projectId) params.set('project_id', projectId);
    if (status) params.set('status', status);
    if (limit) params.set('limit', String(limit));
    return _fetch<{ tasks: Task[]; query: string; count: number }>(
      `/api/tasks/search?${params.toString()}`
    );
  },

  // ====== V1：认领/回退/关闭 ======
  claim: (taskId: string, ifVersion: number, threadId: string) =>
    _fetch<Task>(`/api/tasks/${taskId}/claim`, {
      method: 'POST',
      body: JSON.stringify({ if_version: ifVersion, thread_id: threadId }),
    }),

  rollback: (taskId: string, ifVersion: number, rollbackTarget: string, comment?: string, targetStatus?: string) =>
    _fetch<Task>(`/api/tasks/${taskId}/rollback`, {
      method: 'POST',
      body: JSON.stringify({
        if_version: ifVersion,
        rollback_target: rollbackTarget,
        comment: comment || '',
        // 新增：直接指定目标阶段（优先于 rollback_target）；后端 advance_stage 校验状态机合法性
        target_status: targetStatus || '',
      }),
    }),

  close: (taskId: string, ifVersion: number) =>
    _fetch<Task>(`/api/tasks/${taskId}/close`, {
      method: 'POST',
      body: JSON.stringify({ if_version: ifVersion }),
    }),

  // ====== V1：报告 ======
  listReports: (taskId: string) =>
    _fetch<{ reports: TaskReport[] }>(`/api/tasks/${taskId}/reports`),

  submitReport: (taskId: string, data: {
    agent_id: string; content: string; session_id?: string;
    terminal_session_id?: string; artifact_ids?: string[];
    self_check?: Record<string, unknown>;
  }) => _fetch<TaskReport>(`/api/tasks/${taskId}/reports`, {
    method: 'POST', body: JSON.stringify(data),
  }),

  // ====== V1：报告导出（md/html/json） ======
  // 后端走 ReportExporter：格式转换 + SHA-256 + 历史表 task_report_exports
  exportReport: (taskId: string, reportId: string,
                 format: 'md' | 'html' | 'json' = 'md', verifyOnly = false) =>
    _fetch<TaskReportExport>(`/api/tasks/${taskId}/reports/${reportId}/export`, {
      method: 'POST', body: JSON.stringify({ format, verify_only: verifyOnly }),
    }),

  listExports: (taskId: string, reportId: string) =>
    _fetch<TaskReportExport[]>(`/api/tasks/${taskId}/reports/${reportId}/exports`),

  verifyExport: (taskId: string, reportId: string,
                 format: 'md' | 'html' | 'json' = 'md') =>
    _fetch<TaskExportVerify>(`/api/tasks/${taskId}/reports/${reportId}/export`, {
      method: 'POST', body: JSON.stringify({ format, verify_only: true }),
    }),

  /**
   * 浏览器原生下载导出文件（不走 _fetch，直接拼接 URL 触发 backend FileResponse）。
   * 后端 GET 端点会兜底即时导出（文件不存在则生成）。
   */
  downloadExportUrl: (taskId: string, reportId: string,
                      format: 'md' | 'html' | 'json' = 'md') =>
    `${API_BASE_URL}/api/tasks/${taskId}/reports/${reportId}/export?format=${format}`,

  // ====== V1：评论 ======
  listComments: (taskId: string, commentType?: string) => {
    const qs = commentType ? `?comment_type=${commentType}` : '';
    return _fetch<{ comments: TaskComment[] }>(`/api/tasks/${taskId}/comments${qs}`);
  },

  addComment: (taskId: string, data: {
    body: string; author_type?: string; author_id?: string; author_name?: string;
    comment_type?: string; report_id?: string; decision?: string;
    rollback_target?: string; thread_id?: string;
  }) => _fetch<TaskComment>(`/api/tasks/${taskId}/comments`, {
    method: 'POST', body: JSON.stringify(data),
  }),

  // ====== V1：验收标准 ======
  listCriteria: (taskId: string) =>
    _fetch<{ criteria: AcceptanceCriteria[] }>(`/api/tasks/${taskId}/criteria`),

  addCriteria: (taskId: string, data: { description: string; check_type?: string }) =>
    _fetch<AcceptanceCriteria>(`/api/tasks/${taskId}/criteria`, {
      method: 'POST', body: JSON.stringify(data),
    }),

  // ====== V1：依赖关系 ======
  getRelations: (taskId: string) =>
    _fetch<{ relations: TaskRelation[]; blockers: Task[] }>(`/api/tasks/${taskId}/relations`),

  addRelation: (data: { source_task_id: string; target_task_id: string; relation_type: string }) =>
    _fetch<{ ok: boolean; relation_id: string }>('/api/tasks/relations', {
      method: 'POST', body: JSON.stringify(data),
    }),

  // ====== V1：灵感池 ======
  listIdeas: (projectId?: string, status?: string) => {
    const params = new URLSearchParams();
    if (projectId) params.set('project_id', projectId);
    if (status) params.set('status', status);
    const qs = params.toString();
    return _fetch<{ ideas: TaskIdea[] }>(`/api/tasks/ideas${qs ? '?' + qs : ''}`);
  },

  submitIdea: (data: {
    project_id: string; content: string; source?: string; source_ref?: string;
    tags?: string[]; auto_draft?: boolean;
  }) => _fetch<TaskIdea>('/api/tasks/ideas', { method: 'POST', body: JSON.stringify(data) }),

  confirmIdea: (ideaId: string, ifVersion: number) =>
    _fetch<TaskIdea>(`/api/tasks/ideas/${ideaId}/confirm`, {
      method: 'POST', body: JSON.stringify({ if_version: ifVersion }),
    }),

  convertIdea: (ideaId: string, taskId?: string, title?: string) =>
    _fetch<Task>(`/api/tasks/ideas/${ideaId}/convert`, {
      method: 'POST', body: JSON.stringify({ task_id: taskId || '', title: title || '' }),
    }),

  // ====== V1：文档提案 ======
  listProposals: (taskId?: string, status?: string) => {
    const params = new URLSearchParams();
    if (taskId) params.set('task_id', taskId);
    if (status) params.set('status', status);
    const qs = params.toString();
    return _fetch<{ proposals: DocChangeProposal[] }>(`/api/tasks/proposals${qs ? '?' + qs : ''}`);
  },

  applyProposal: (proposalId: string, ifVersion: number, newHash: string) =>
    _fetch<{ ok: boolean }>(`/api/tasks/proposals/${proposalId}/apply`, {
      method: 'POST', body: JSON.stringify({ if_version: ifVersion, new_hash: newHash }),
    }),

  // ====== V1：活动/交付物 ======
  listActivities: (taskId: string) =>
    _fetch<{ activities: TaskActivity[] }>(`/api/tasks/${taskId}/activities`),

  listArtifacts: (taskId: string) =>
    _fetch<{ artifacts: TaskArtifact[] }>(`/api/tasks/${taskId}/artifacts`),

  // ====== V1：执行编码（harness 可选 claude_code / codex） ======
  executeCoding: (taskId: string, styleId?: string, ifVersion?: number, harness?: string) =>
    _fetch<{ ok: boolean; run_id: string; terminal_session_id: string; mock: boolean; harness?: string }>(
      `/api/tasks/${taskId}/execute`, {
        method: 'POST',
        body: JSON.stringify({
          style_id: styleId || 'default', if_version: ifVersion || 0,
          harness: harness || 'claude_code',
        }),
      }),

  // ====== V3：仪表盘聚合（§4.11.2） ======
  dashboard: (projectId = '') => {
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return _fetch<TaskDashboard>(`/api/tasks/dashboard${qs}`);
  },

  // ====== V3：项目级网状图数据（§4.11.4 X9） ======
  graph: (projectId = '') => {
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return _fetch<{ tasks: Task[]; relations: TaskRelation[] }>(`/api/tasks/graph${qs}`);
  },

  // ====== V3：终端会话注册表（§4.13） ======
  listTerminalSessions: () =>
    _fetch<{ sessions: TerminalSession[] }>('/api/tasks/terminal/sessions'),

  createTerminalSession: (kind: 'codex' | 'claude' | 'shell') =>
    _fetch<TerminalSession>('/api/tasks/terminal/sessions', {
      method: 'POST',
      body: JSON.stringify({ kind }),
    }),

  closeTerminalSession: (terminalSessionId: string) =>
    _fetch<TerminalSession>(`/api/tasks/terminal/sessions/${terminalSessionId}`, {
      method: 'DELETE',
    }),

  captureTerminalPane: (terminalSessionId: string) =>
    _fetch<{ content: string; terminal_session_id: string; error?: string }>(
      `/api/tasks/terminal/sessions/${terminalSessionId}/pane`
    ),

  sendTerminalKeys: (terminalSessionId: string, keys: string) =>
    _fetch<{ ok: boolean }>(`/api/tasks/terminal/sessions/${terminalSessionId}/keys`, {
      method: 'POST',
      body: JSON.stringify({ keys }),
    }),

  getTerminalLayout: () =>
    _fetch<TerminalLayout>('/api/tasks/terminal/layout'),

  saveTerminalLayout: (panes: TerminalLayoutPane[]) =>
    _fetch<TerminalLayout>('/api/tasks/terminal/layout', {
      method: 'PUT',
      body: JSON.stringify({ panes }),
    }),
};

// V3：Coding 终端页 SSE 地址（EventSource 用，需全 URL）
export const terminalPageStreamUrl = `${API_BASE_URL}/api/tasks/terminal/stream`;
