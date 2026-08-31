import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import MarkdownIt from 'markdown-it';
import { WidgetRenderer } from '../components/WidgetRenderer';
import { apiClient, type WidgetUpdate, type WidgetType } from '../lib/api';
import type { DagEvent, SessionRunInfo, SessionMemoryInfo } from '../lib/types';
import type { PageId } from '../App';
import {
  applySurfaceStateEvent,
  SupervisionPanel,
  type SupervisionSnapshot,
  type WhitelistedProfiles,
} from '../components/supervision/SupervisionPanel';
import { WorkspaceSelectorDialog } from '../components/WorkspaceSelectorDialog';
import { WorkspacePicker } from '../components/WorkspacePicker';
import { PermissionLevelButton } from '../components/PermissionLevelButton';
import { ApprovalPrompt, type ApprovalRequestData } from '../components/ApprovalPrompt';
import type { AgentTier, PermissionLevel } from '../lib/api';
import '../styles/super-agent.css';

// 共享 markdown-it 实例（模块级单例）：禁用 html/link/image（防 XSS），启用表格+换行
// 用户消息和 assistant 回复都通过它渲染，支持表格/加粗/列表/代码块等格式
// 注：strong/emphasis/list/blockquote/code/fence/hr 默认启用，无需 enable；table 需显式 enable
const md = new MarkdownIt({
  html: false,
  linkify: false,
  breaks: true,
  typographer: true,
});
md.enable(['table', 'strikethrough']);

/** 将 markdown 文本渲染为 HTML（XSS 安全：禁用 html/link/image） */
function renderMarkdown(text: string): string {
  return md.render(text);
}

// ===== 类型 =====
interface ChatMsg {
  role: 'user' | 'assistant' | 'system' | 'widget' | 'tool_use';
  content: string;
  timestamp?: string;
  widget?: WidgetUpdate;
  node_id?: string;
  node_status?: { status: 'started' | 'completed' | 'failed' | 'skipped'; duration_ms?: number; tokens?: number };
  tool_use?: { tool_use_id?: string; tool_name: string; input?: unknown };
}

const STATUS_ICON: Record<string, string> = { completed: '✓', running: '▶', failed: '✗', waiting: '⏸', skipped: '⊘', pending: '·', active: '▶' };
const STATUS_COLOR: Record<string, string> = { completed: 'var(--green)', running: 'var(--accent)', failed: 'var(--red)', waiting: 'var(--st-waiting)', skipped: 'var(--st-skipped)', pending: 'var(--st-ready)', active: 'var(--accent)' };

// 字符串首字母大写：用于 harness 显示（opencode → Opencode）
function capitalize(s: string): string { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

// Widget 类型 → 展示图标 + 中文标签（widget 面板里用）
const WIDGET_ICON: Record<string, string> = {
  form: '📝', task_draft: '✍️', checklist: '☑️', memo: '📌', progress_status: '📊',
  table: '📋', chart: '📈', compare: '🔄', file_preview: '📁', kanban: '🗂', diff: '🔀',
  artifact_ref: '📎', timeline: '⏱', html: '🌐', a2ui: '🎨',
};
const WIDGET_LABEL: Record<string, string> = {
  form: '表单输入', task_draft: '任务草稿确认', checklist: '检查清单', memo: '便笺', progress_status: '进度',
  table: '数据表', chart: '图表', compare: '对比', file_preview: '文件预览', kanban: '看板', diff: '差异',
  artifact_ref: '附件引用', timeline: '时间线', html: 'HTML 面板', a2ui: 'A2UI 组件',
};

// v81：widget.update 事件统一在对话区内联渲染（用户视线焦点在对话流，提交后上下文连贯）。
// 展示型大屏（旧 widgets 画布）已废弃：DAG surface 走 SupervisionPanel（唯一生成式 UI 出口）。

interface SuperAgentPageProps {
  onNavigate?: (page: PageId) => void;
  /** session 创建/切换时回调，将当前 sessionId 上提到 App 层供协作可视化页面跟随 */
  onLiveSessionChange?: (sessionId: string | null) => void;
  /** 对话记录抽屉展开状态（由 App 层 ChatTopBar 按钮控制） */
  chatDrawerOpen?: boolean;
  /** 对话记录抽屉收起/展开切换 */
  onToggleChatDrawer?: () => void;
  /** 工作流页「运行」入口：自动新建 session 并启动 workflow run（一次性，消费后由 App 层清空） */
  pendingWorkflow?: { workflowId: string; inputs: Record<string, unknown> } | null;
  /** pendingWorkflow 已消费（成功或失败）回调，App 层清空避免重复触发 */
  onWorkflowLaunched?: () => void;
}

/**
 * SuperAgentPage — AgentOps 工作台主视图（单 Session 三栏）。
 *
 * 布局：
 *   左栏(340px)：工作台对话区（发消息 + SSE + widget 内联 + 历史会话/新建按钮）
 *   中栏(300px)：任务卡片（RunCard + 快捷操作）
 *   右栏(1fr)  ：4 tab（交互面板/DAG 拓扑/Actor/数据流），交互面板为只读汇总
 *
 * 任务执行过程中的交互入口（form/task_draft/checklist 等）直接在对话消息流中
 * 内联渲染可交互 widget；右栏「🧩 交互面板」只做汇总展示。
 */
export function SuperAgentPage({ onNavigate, onLiveSessionChange, chatDrawerOpen = true, onToggleChatDrawer, pendingWorkflow, onWorkflowLaunched }: SuperAgentPageProps) {
  // === SSE 核心 state ===
  // 注：runId 实际存的是 session_id（Thread 模式 v2 已统一用 session_id 路由）。
  //     保留 runId 命名是为了减少 selectedRunId / sessionRuns 等邻近变量重命名风险。
  const [runId, setRunId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState('idle');
  // 会话级权限级别（与 workspace 解耦，随时可切换、立即生效）
  const [permissionLevel, setPermissionLevel] = useState<string | null>(null);
  const [sseConnected, setSseConnected] = useState(true);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [events, setEvents] = useState<DagEvent[]>([]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [tokenIn, setTokenIn] = useState(0);
  const [tokenOut, setTokenOut] = useState(0);
  // P2：当前待审批请求（SSE approval.requested 置入，decided/决定后清空）
  const [approvalRequest, setApprovalRequest] = useState<ApprovalRequestData | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  // v99.5 P0.2.5/6：report_surface_state 事件聚合为 (actor_id, view_id) → snapshot
  const [surfaceSnapshots, setSurfaceSnapshots] = useState<Record<string, SupervisionSnapshot>>({});
  // v99.5 P0.11：Actor Visual Profile 白名单（fetch on mount），
  // 传入 SupervisionPanel / applySurfaceStateEvent 启用 view_id 白名单拒绝未授权 snapshot
  const [actorProfiles, setActorProfiles] = useState<WhitelistedProfiles>({});

  // === Session 全景 state ===
  const [sessionRuns, setSessionRuns] = useState<SessionRunInfo[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [memories, setMemories] = useState<SessionMemoryInfo[]>([]);
  const [genuiFullscreen, setGenuiFullscreen] = useState(false);  // 生成式 UI 大屏全屏模式
  const [commandModal, setCommandModal] = useState<{ open: boolean; actorId: string }>({ open: false, actorId: '' });

  // === 当前 Agent 动态化（按 session 第一个 run 的 agent_id 拉 display_name + model，fallback 到 manager_model）===
  const [currentAgent, setCurrentAgent] = useState<{ id: string; displayName: string; model: string; harness: string; tier?: string } | null>(null);

  // === 历史会话下拉 ===
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historySessions, setHistorySessions] = useState<Array<{ session_id: string; title?: string; started_at?: string; last_activity_at?: string; status?: string; agent_id?: string }>>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // === refs ===
  const cleanupRef = useRef<(() => void) | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const initKeyRef = useRef<string | null>(null);
  // SSE 主动关闭标记：handleSend 切换 session 时主动 close 旧 SSE，浏览器可能在 close() 后异步触发 onError，
  // 用这个 ref 告诉 onError「这是主动关闭，别把 sseConnected 标 false」（CLAUDE.md「SSE 断连≠run 失败」原则）
  const sseManuallyClosedRef = useRef(false);
  // SSE 重试计数器（防止无限重试）+ 当前订阅的 runId
  const sseRetryCountRef = useRef(0);
  // P0.11：refs 让 SSE handler 闭包读到最新的 surfaceSnapshots / actorProfiles
  // （handleSSEMessage useCallback 依赖项不能包含 state，否则会断 SSE 重连）
  const surfaceSnapshotsRef = useRef<Record<string, SupervisionSnapshot>>({});
  const actorProfilesRef = useRef<WhitelistedProfiles>({});
  useEffect(() => { surfaceSnapshotsRef.current = surfaceSnapshots; }, [surfaceSnapshots]);
  useEffect(() => { actorProfilesRef.current = actorProfiles; }, [actorProfiles]);

  // === 滚动到底部 ===
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  // === SSE 事件处理（Thread 模式 v2 + 向后兼容旧事件）===
  const handleSSEMessage = useCallback((event: MessageEvent<string>) => {
    try {
      const data = JSON.parse(event.data);
      if (!data.type) return;

      const sid = data.session_id || data.run_id;
      if (sid) setEvents((prev) => [...prev, data as DagEvent]);

      // session.created
      if (data.type === 'session.created') {
        setRunStatus('active'); setIsGenerating(false);
      }
      // turn.started（不添加系统消息，用"正在思考..."指示器代替）
      else if (data.type === 'turn.started') {
        setIsGenerating(true); setRunStatus('active');
      }
      // turn.progress（agent 实时输出）
      else if (data.type === 'turn.progress') {
        const text = data.payload?.text || '';
        const isError = data.payload?.is_error;
        if (text) {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.role === 'assistant' && (last as any)._streaming) {
              return [...prev.slice(0, -1), { ...last, content: last.content + text } as ChatMsg];
            }
            return [...prev, { role: 'assistant', content: text, timestamp: new Date().toISOString(), _streaming: true } as ChatMsg];
          });
          if (isError) setIsGenerating(false);
        }
      }
      // turn.completed
      else if (data.type === 'turn.completed') {
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'assistant' && (last as any)._streaming) {
            return [...prev.slice(0, -1), { ...last, _streaming: false } as ChatMsg];
          }
          return prev;
        });
        setIsGenerating(false);
        setRunStatus('dormant');
        setMessages((prev) => [...prev, {
          role: 'system', content: '', timestamp: data.occurred_at || new Date().toISOString(),
          node_id: data.node_id || undefined,
          node_status: { status: 'completed' },
        }]);
        if (data.payload?.total_tokens_input) setTokenIn(data.payload.total_tokens_input);
        if (data.payload?.total_tokens_output) setTokenOut(data.payload.total_tokens_output);
      }
      // turn.failed
      else if (data.type === 'turn.failed') {
        setIsGenerating(false); setRunStatus('failed');
        setMessages((prev) => [...prev, {
          role: 'system', content: `\u26a0\ufe0f ${data.payload?.error || 'turn \u5931\u8d25'}`, timestamp: new Date().toISOString(),
          node_id: data.node_id || undefined,
          node_status: { status: 'failed' },
        }]);
      }
      // session.dormant
      else if (data.type === 'session.dormant') {
        setIsGenerating(false); setRunStatus('dormant');
      }
      // P2（deepseek-harness 对齐）：审批请求 → 弹窗等待用户决定（allowed-once / rejected）
      else if (data.type === 'approval.requested' && data.payload) {
        const p = data.payload;
        if (p.request_id && p.tool_name) {
          setApprovalRequest({
            request_id: String(p.request_id),
            tool_name: String(p.tool_name),
            reason: p.reason ? String(p.reason) : undefined,
          });
        }
      }
      // 审批已了结（含超时 unavailable / 取消 cancelled）→ 关闭弹窗
      else if (data.type === 'approval.decided' && data.payload) {
        const p = data.payload;
        setApprovalRequest((prev) =>
          prev && prev.request_id === p.request_id ? null : prev,
        );
      }
      // Widget 更新
      else if (data.type === 'widget.update' && data.payload) {
        const wp = data.payload;
        const wu: WidgetUpdate = { run_id: sid || '', widget_id: wp.widget_id || `w_${Date.now()}`, type: (wp.type as WidgetType) || 'memo', props: wp.props || {} };
        // v74：tool_use 类型 widget 不进对话区（由 conversation.tool_use 事件接管）
        if (wp.type === 'tool_use') return;
        // v81（清理后）：所有 widget 统一进 messages 数组，由 MessageRow 在对话区内联渲染。
        // 旧"展示型 → 右栏大屏画布"通道已删除（DAG surface 由 SupervisionPanel 渲染）。
        setMessages((prev) => {
          // 同 widget_id 更新（agent 重发同一表单的 progress 场景）→ 替换而非追加
          const idx = prev.findIndex((m) => m.role === 'widget' && m.widget?.widget_id === wu.widget_id);
          if (idx >= 0) {
            const updated = [...prev];
            updated[idx] = { role: 'widget', content: '', timestamp: new Date().toISOString(), widget: wu };
            return updated;
          }
          return [...prev, { role: 'widget', content: '', timestamp: new Date().toISOString(), widget: wu }];
        });
        // v2：流式文本走 turn.progress 分支（:171-185）累积到 assistant 消息，
        // widget.update 不再追加 memo 为 assistant（避免双重渲染）。
        // 历史 run 的 memo 事件仍进 messages 作为 widget 消息，由 WidgetRenderer 渲染。
      }
      // v74：agent 工具调用事件（类 Claude Code 时间线，只在对话区显示）
      else if (data.type === 'conversation.tool_use' && data.payload) {
        setMessages((prev) => [...prev, {
          role: 'tool_use',
          content: '',
          timestamp: data.occurred_at || new Date().toISOString(),
          tool_use: { tool_use_id: data.payload.tool_use_id, tool_name: data.payload.tool_name, input: data.payload.input },
        }]);
      }
      // v99.5 P0.2.5/6：report_surface_state 事件（agent 通过 report_surface_state 工具推送的
      // 生成式 UI surface snapshot）。payload 形状：
      //   {
      //     actor_id?: string,
      //     surface_state: SurfaceState,
      //     ...（后端 DagEvent.to_payload_with_surface 合并产物）
      //   }
      // 容器 SupervisionPanel 按 (actor_id, view_id) 聚合 + phase 单调推进。
      // P0.11：传入 actorProfiles 启用 view_id 白名单（未授权 snapshot 返回
      // {__dropped: true, reason}，被 reducer 丢弃）。
      else if (data.type === 'report_surface_state' && data.payload?.surface_state) {
        const result = applySurfaceStateEvent(
          { ...surfaceSnapshotsRef.current },
          data.payload,
          actorProfilesRef.current,
        );
        // reducer 在白名单外返回 dropped 标记；正常情况返回 byKey 快照
        if (result && typeof result === 'object' && '__dropped' in result) {
          console.warn(
            '[SupervisionPanel] snapshot dropped:',
            result.reason,
            'actor=',
            data.payload.actor_id,
            'view=',
            data.payload.surface_state?.view_id,
          );
          return;
        }
        setSurfaceSnapshots(result as Record<string, SupervisionSnapshot>);
      }
      // 向后兼容（DAG 工作流事件）
      else if (data.type === 'run.created') { setRunStatus('running'); setIsGenerating(true); }
      else if (data.type === 'run.completed') {
        setRunStatus('completed'); setIsGenerating(false);
        setMessages((prev) => [...prev, { role: 'system', content: '✅ 工作流已完成', timestamp: data.occurred_at || new Date().toISOString() }]);
      }
      else if (data.type === 'run.failed') {
        setRunStatus('failed'); setIsGenerating(false);
        setMessages((prev) => [...prev, { role: 'system', content: `❌ 工作流失败: ${data.payload?.error || ''}`, timestamp: data.occurred_at || new Date().toISOString() }]);
      }
      else if (data.type === 'node.started' || data.type === 'node.completed' || data.type === 'node.failed' || data.type === 'node.skipped') {
        const sm: Record<string, 'started' | 'completed' | 'failed' | 'skipped'> = { 'node.started': 'started', 'node.completed': 'completed', 'node.failed': 'failed', 'node.skipped': 'skipped' };
        setMessages((prev) => [...prev, { role: 'system', content: '', timestamp: data.occurred_at || new Date().toISOString(), node_id: data.node_id || undefined, node_status: { status: sm[data.type] } }]);
        if (data.type === 'node.completed' || data.type === 'node.failed') setIsGenerating(false);
      }
      // node.progress（agent 实时输出，追加到对话区让用户看到执行进度）
      else if (data.type === 'node.progress' && data.payload?.agent_text) {
        const text = String(data.payload.agent_text);
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'assistant' && (last as any)._streaming) {
            return [...prev.slice(0, -1), { ...last, content: last.content + text } as ChatMsg];
          }
          return [...prev, { role: 'assistant', content: text, timestamp: new Date().toISOString(), _streaming: true } as ChatMsg];
        });
      }
    } catch { /* non-JSON, ignore */ }
  }, []);

  // === Thread 模式：首次创建 session + 发送首条消息 ===
  // 注意：user 消息由 handleSend 在调用本函数前已添加，不要重复添加
  const startSessionAndSend = useCallback(async (message: string) => {
    if (!message.trim()) return;
    setLoading(true); setEvents([]); setTokenIn(0); setTokenOut(0); setRunStatus('pending');
    // P0.15 修复：新建会话时清空旧 surfaceSnapshots，避免上一个 session 的 final surface
    // 拒绝新 session 的 started/partial surface（phase 回退被 reducer 丢弃）
    setSurfaceSnapshots({});
    try {
      const resp = await apiClient.v2CreateSession('manager', pendingWorkspaceIdRef.current);
      setRunId(resp.session_id); setRunStatus('active'); setIsGenerating(true);
      onLiveSessionChange?.(resp.session_id);
      sseManuallyClosedRef.current = false; sseRetryCountRef.current = 0;
      if (cleanupRef.current) cleanupRef.current();
      const cleanup = apiClient.v2OpenSessionEventStream(resp.session_id, {
        onMessage: handleSSEMessage,
        onOpen: () => { console.log('[SSE] v2:', resp.session_id); setSseConnected(true); sseRetryCountRef.current = 0; },
        onError: () => { if (sseManuallyClosedRef.current) return; setSseConnected(false); },
      });
      setSseConnected(true); cleanupRef.current = cleanup;
      // 同步会话级权限级别（后端创建会话时从 workspace.permissions 初始化）
      try {
        const wsInfo = await apiClient.getCurrentWorkspace(resp.session_id);
        setPermissionLevel(wsInfo.permission_level);
      } catch { /* 忽略：权限加载失败不阻塞发消息 */ }
      await apiClient.v2SendTurn(resp.session_id, message);
    } catch (err) {
      setRunStatus('error');
      setMessages((prev) => [...prev, { role: 'system', content: `\u542f\u52a8\u5931\u8d25: ${err instanceof Error ? err.message : ''}`, timestamp: new Date().toISOString() }]);
    } finally { setLoading(false); }
  }, [handleSSEMessage]);

  // === 工作流页「运行」入口：自动新建 session → 打开 SSE → startRun（templated） ===
  // App 层 WorkflowsPage 点「运行」→ handleStartRun 设 runWorkflowId/runInputs → 跳转本页。
  // 历史断链：本页此前从未接收这两个值，run 请求根本不会发出。此处消费 pendingWorkflow，
  // 通过 session_id 把 run 事件桥接到本页已订阅的 session SSE 流。
  const wfLaunchKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!pendingWorkflow?.workflowId) return;
    const launchKey = `${pendingWorkflow.workflowId}:${JSON.stringify(pendingWorkflow.inputs)}`;
    if (wfLaunchKeyRef.current === launchKey) return; // 同一请求只触发一次
    wfLaunchKeyRef.current = launchKey;
    const { workflowId, inputs } = pendingWorkflow;
    const ts = new Date().toISOString();
    setMessages((prev) => [...prev, { role: 'user', content: `▶ 运行工作流 ${workflowId}`, timestamp: ts }]);
    setLoading(true); setEvents([]); setTokenIn(0); setTokenOut(0); setRunStatus('pending');
    setSurfaceSnapshots({}); setIsGenerating(true);
    (async () => {
      try {
        const resp = await apiClient.v2CreateSession('manager', pendingWorkspaceIdRef.current);
        setRunId(resp.session_id); setRunStatus('active');
        onLiveSessionChange?.(resp.session_id);
        sseManuallyClosedRef.current = false; sseRetryCountRef.current = 0;
        if (cleanupRef.current) cleanupRef.current();
        const cleanup = apiClient.v2OpenSessionEventStream(resp.session_id, {
          onMessage: handleSSEMessage,
          onOpen: () => { console.log('[SSE] v2:', resp.session_id); setSseConnected(true); sseRetryCountRef.current = 0; },
          onError: () => { if (sseManuallyClosedRef.current) return; setSseConnected(false); },
        });
        setSseConnected(true); cleanupRef.current = cleanup;
        try {
          const wsInfo = await apiClient.getCurrentWorkspace(resp.session_id);
          setPermissionLevel(wsInfo.permission_level);
        } catch { /* 权限加载失败不阻塞 run */ }
        const runResp = await apiClient.startRun({
          workflow_id: workflowId,
          inputs,
          run_mode: 'templated',
          session_id: resp.session_id,
        });
        setMessages((prev) => [...prev, { role: 'system', content: `工作流已启动（run: ${runResp.run_id}），DAG 执行中…`, timestamp: new Date().toISOString() }]);
      } catch (err) {
        setRunStatus('error');
        setMessages((prev) => [...prev, { role: 'system', content: `工作流启动失败: ${err instanceof Error ? err.message : ''}`, timestamp: new Date().toISOString() }]);
        setIsGenerating(false);
      } finally {
        setLoading(false);
        onWorkflowLaunched?.();
      }
    })();
  }, [pendingWorkflow, handleSSEMessage, onLiveSessionChange, onWorkflowLaunched]);

  // === 发送消息（Thread 模式 v2）===
  const handleSend = useCallback(() => {
    if (!inputText.trim()) return;
    const msg = inputText; setInputText('');
    if (inputRef.current) inputRef.current.style.height = 'auto';
    setMessages((prev) => [...prev, { role: 'user', content: msg, timestamp: new Date().toISOString() }]);
    setIsGenerating(true);
    if (runId) {
      apiClient.v2SendTurn(runId, msg).catch((err) => {
        setMessages((prev) => [...prev, { role: 'system', content: `\u53d1\u9001\u5931\u8d25: ${err instanceof Error ? err.message : ''}`, timestamp: new Date().toISOString() }]);
        setIsGenerating(false);
      });
    } else {
      startSessionAndSend(msg);
    }
  }, [inputText, runId, startSessionAndSend, handleSSEMessage]);

  // === 停止生成 ===
  const handleStop = useCallback(() => {
      if (!runId) return;
      apiClient.v2CancelSession(runId).catch(() => {});
    setIsGenerating(false);
  }, [runId]);

  // === 共享 widget 提交逻辑（对话区/右栏/弹窗三个调用点共用）===
  // widget 提交统一走 v2SendWidgetInput，由 SessionEngine 的 _pending_inputs 队列处理。
  // （旧 opencode question 拦截的 question_ 前缀特殊路径已随 widget A2UI 迁移清理）
  const handleWidgetInput = useCallback(async (widgetId: string, input: Record<string, unknown>) => {
    if (!runId) return;
    // v82：先打「已提交」系统消息（之前完全没反馈，用户以为按钮没反应）
    setMessages((prev) => [...prev, {
      role: 'system',
      content: `── 表单已提交 (widget: ${widgetId}) ──`,
      timestamp: new Date().toISOString(),
    }]);
    try {
      await apiClient.v2SendWidgetInput(runId, { widget_id: widgetId, input });
    } catch (e) {
      console.warn('[widget-input] failed', e);
      setMessages((prev) => [...prev, {
        role: 'system',
        content: `widget.input 提交失败: ${e instanceof Error ? e.message : '未知错误'}`,
        timestamp: new Date().toISOString(),
      }]);
    }
  }, [runId]);

  // === 加载历史 session 消息（v2 Thread 模式：从 session_messages 表查）===
  const loadSessionMessages = useCallback(async (sessionId: string) => {
    setLoading(true); setEvents([]); setTokenIn(0); setTokenOut(0); setRunStatus('idle');
    setSurfaceSnapshots({});
    try {
      const resp = await apiClient.v2GetSessionMessages(sessionId, 200);
      const loaded: ChatMsg[] = (resp.messages || []).map((m: Record<string, unknown>) => ({
        role: ((m.role === 'user' ? 'user' : m.role === 'assistant' ? 'assistant' : 'system') as 'user' | 'assistant' | 'system'),
        content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
        timestamp: (m.created_at as string) || '',
      }));
      console.log(`[SuperAgent] loadSessionMessages: ${loaded.length} 条 for ${sessionId}`);
      setMessages(loaded);
      setRunId(sessionId);
      onLiveSessionChange?.(sessionId);
      setRunStatus('completed');
      // 同步该历史会话的权限级别
      try {
        const wsInfo = await apiClient.getCurrentWorkspace(sessionId);
        setPermissionLevel(wsInfo.permission_level);
      } catch { /* 忽略 */ }

      // 回放历史 surface_state 事件（让生成式 UI 大屏显示历史 surface）
      // 修复：sequence 是 per-run 的，跨 run 排序无意义。改为按 run 分组回放
      // （每个 run 内部 phase monotonic 有效），合并时对相同 key 取 emitted_at 最新。
      try {
        const runsResp = await apiClient.v2GetSessionRuns(sessionId);
        const runIds = (runsResp.runs || []).map(r => r.run_id).filter(Boolean);
        if (runIds.length > 0) {
          const eventsResults = await Promise.all(
            runIds.map(rid => apiClient.auditGetRunEvents(rid).catch(() => null))
          );
          const profiles = actorProfilesRef.current;
          const hasProfiles = profiles && Object.keys(profiles).length > 0;
          // 每个 run 独立回放，得到 per-run snapshots
          const perRunSnapshots: Array<Record<string, SupervisionSnapshot>> = [];
          for (const result of eventsResults) {
            if (!result || !result.events) continue;
            const runEvents: Array<{ sequence: number; payload: Record<string, unknown> }> = [];
            for (const ev of result.events) {
              if (ev.type === 'report_surface_state' && (ev.payload as Record<string, unknown>)?.surface_state) {
                runEvents.push({
                  sequence: (ev.sequence as number) || 0,
                  payload: ev.payload as Record<string, unknown>,
                });
              }
            }
            runEvents.sort((a, b) => a.sequence - b.sequence);
            if (runEvents.length === 0) continue;
            let runSnapshots: Record<string, SupervisionSnapshot> = {};
            for (const ev of runEvents) {
              const r = applySurfaceStateEvent(
                { ...runSnapshots },
                ev.payload as { actor_id?: string; surface_state: import('../lib/api').SurfaceState },
                hasProfiles ? profiles : undefined,
              );
              if (r && typeof r === 'object' && !('__dropped' in r)) {
                runSnapshots = r as Record<string, SupervisionSnapshot>;
              }
            }
            if (Object.keys(runSnapshots).length > 0) {
              perRunSnapshots.push(runSnapshots);
            }
          }
          // 合并所有 run 的 snapshots：对相同 key，取 emitted_at 最新的
          const merged: Record<string, SupervisionSnapshot> = {};
          for (const runSnapshots of perRunSnapshots) {
            for (const [key, snap] of Object.entries(runSnapshots)) {
              const existing = merged[key];
              const snapTime = snap.emitted_at || '';
              const existTime = existing?.emitted_at || '';
              if (!existing || snapTime > existTime) {
                merged[key] = snap;
              }
            }
          }
          setSurfaceSnapshots(merged);
        }
      } catch (err) {
        console.warn('[SuperAgent] 回放历史 surface_state 失败:', err);
      }
    } catch (err) {
      console.warn('[SuperAgent] loadSessionMessages 失败:', err);
    } finally { setLoading(false); }
  }, []);

  // === 加载历史会话列表（v2ListSessions 返回 runs 表记录，按 session_id 去重取最新）===
  const loadHistorySessions = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const resp = await apiClient.v2ListSessions(50, 0);
      const seen = new Map<string, { session_id: string; title?: string; started_at?: string; last_activity_at?: string; status?: string; agent_id?: string }>();
      for (const r of resp.sessions || []) {
        const sid = (r.session_id as string) || (r.run_id as string);
        if (!sid) continue;
        const prev = seen.get(sid);
        const cur = {
          session_id: sid,
          title: (r.title as string) || '',
          started_at: (r.started_at as string) || '',
          last_activity_at: (r.last_activity_at as string) || (r.started_at as string) || '',
          status: (r.status as string) || '',
          agent_id: (r.agent_id as string) || '',
        };
        if (!prev || (cur.last_activity_at || '') > (prev.last_activity_at || '')) {
          seen.set(sid, cur);
        }
      }
      // 过滤：只展示有 title 的会话（title 为空 = 未发送消息的空会话，无展示价值）
      // 保留最近 10 条无 title 的会话，用 session_id 最后 12 位做 fallback（便于调试）
      const all = Array.from(seen.values()).sort((a, b) => (b.last_activity_at || '').localeCompare(a.last_activity_at || ''));
      const withTitle = all.filter(s => s.title && s.title.trim());
      const withoutTitle = all.filter(s => !s.title || !s.title.trim()).slice(0, 5); // 最多展示 5 个空会话
      const list = [...withTitle, ...withoutTitle];
      setHistorySessions(list);
    } catch (err) {
      console.warn('[SuperAgent] v2ListSessions 失败:', err);
    } finally { setHistoryLoading(false); }
  }, []);

  // === 切换到历史会话 ===
  const handleSelectHistory = useCallback((sessionId: string) => {
    setHistoryOpen(false);
    if (sessionId === runId) return;
    // 关闭旧 SSE
    sseManuallyClosedRef.current = true;
    if (cleanupRef.current) { cleanupRef.current(); cleanupRef.current = null; }
    setIsGenerating(false);
    loadSessionMessages(sessionId);
    // 重新订阅 SSE，便于接收后续 turn 事件
    sseManuallyClosedRef.current = false; sseRetryCountRef.current = 0;
    const cleanup = apiClient.v2OpenSessionEventStream(sessionId, {
      onMessage: handleSSEMessage,
      onOpen: () => { setSseConnected(true); sseRetryCountRef.current = 0; },
      onError: () => { if (sseManuallyClosedRef.current) return; setSseConnected(false); },
    });
    setSseConnected(true); cleanupRef.current = cleanup;
  }, [runId, loadSessionMessages, handleSSEMessage]);

  // === 新建会话：弹 WorkspaceSelectorDialog 让用户选择 workspace ===
  const [selectorOpen, setSelectorOpen] = useState(false);
  const handleNewSession = useCallback(() => {
    setHistoryOpen(false);
    setSelectorOpen(true);  // P0.18.7c: 弹工作区选择弹窗
  }, []);

  // 用户在 WorkspaceSelectorDialog 选定 workspace 后
  const handleSelectorSelect = useCallback((selection: { workspaceId: string | null; mode: 'project' | 'general' }) => {
    setSelectorOpen(false);
    // 清理旧 session 状态
    sseManuallyClosedRef.current = true;
    if (cleanupRef.current) { cleanupRef.current(); cleanupRef.current = null; }
    setRunId(null); setMessages([]); setEvents([]);
    setPermissionLevel(null);
    onLiveSessionChange?.(null);
    setTokenIn(0); setTokenOut(0); setRunStatus('idle'); setIsGenerating(false);
    setSessionRuns([]); setSelectedRunId(null); setMemories([]);
    setCurrentAgent(null);
    // 把选中的 workspace_id 暂存（下次发消息时 startSessionAndSend 自动带上）
    pendingWorkspaceIdRef.current = selection.workspaceId;
    sseManuallyClosedRef.current = false;
  }, []);

  const pendingWorkspaceIdRef = useRef<string | null>(null);

  // === 点击历史按钮：打开下拉并刷新列表 ===
  const handleToggleHistory = useCallback(() => {
    setHistoryOpen((v) => {
      const next = !v;
      if (next) loadHistorySessions();
      return next;
    });
  }, [loadHistorySessions]);

  // === useEffect: 自动恢复最近 session（v2 Thread 模式：从 sessions 表查）===
  useEffect(() => {
    if (initKeyRef.current) return;
    initKeyRef.current = 'auto-restore';
    console.log('[SuperAgent] 开始自动恢复最近 session…');
    apiClient.v2ListSessions(20, 0)
      .then((resp) => {
        const sessions = resp.sessions || [];
        console.log(`[SuperAgent] v2ListSessions 返回 ${sessions.length} 条 sessions`);
        if (sessions.length === 0) {
          console.warn('[SuperAgent] 没有 session，跳过自动恢复');
          return;
        }
        // 取最近活动的 session（last_activity_at 倒序第一）
        const sorted = sessions
          .map((s) => ({
            session_id: (s.session_id as string) || (s.run_id as string) || '',
            last_activity_at: (s.last_activity_at as string) || (s.started_at as string) || '',
          }))
          .filter((s) => s.session_id)
          .sort((a, b) => (b.last_activity_at || '').localeCompare(a.last_activity_at || ''));
        if (sorted[0]) {
          console.log(`[SuperAgent] 自动恢复 session: ${sorted[0].session_id}`);
          loadSessionMessages(sorted[0].session_id);
          // 同时订阅 SSE，便于接收后续 turn 事件
          sseManuallyClosedRef.current = false; sseRetryCountRef.current = 0;
          if (cleanupRef.current) cleanupRef.current();
          const cleanup = apiClient.v2OpenSessionEventStream(sorted[0].session_id, {
            onMessage: handleSSEMessage,
            onOpen: () => { setSseConnected(true); sseRetryCountRef.current = 0; },
            onError: () => { if (sseManuallyClosedRef.current) return; setSseConnected(false); },
          });
          setSseConnected(true); cleanupRef.current = cleanup;
        }
      })
      .catch((err) => console.warn('[SuperAgent] v2ListSessions 失败:', err));
  }, [loadSessionMessages, handleSSEMessage]);

  // === useEffect: v99.5 P0.11 — fetch actor profiles on mount for view_id whitelist ===
  // 把后端 /api/actors 返回的 profile 列表压缩为 SupervisionPanel 期望的
  // WhitelistedProfiles 格式（仅保留 actor_id + allowed_surface_views keys），
  // 传入 reducer 启用白名单拒绝未授权的 snapshot。
  useEffect(() => {
    let cancelled = false;
    apiClient.getActorProfiles()
      .then((resp) => {
        if (cancelled) return;
        const profiles: WhitelistedProfiles = {};
        for (const a of resp.actors || []) {
          profiles[a.actor_id] = {
            allowed_surface_views: Object.fromEntries(
              (a.allowed_surface_views || []).map((v) => [v.view_id, v]),
            ),
          };
        }
        setActorProfiles(profiles);
        console.log(`[SuperAgent] 加载 ${Object.keys(profiles).length} 个 actor profile:`,
          Object.keys(profiles));
      })
      .catch((err) => {
        console.warn('[SuperAgent] getActorProfiles 失败（白名单降级为不启用）:', err);
      });
    return () => { cancelled = true; };
  }, []);

  // === useEffect: runId(=session_id) 变化 → 加载 session runs + memory（v2 Thread 模式）===
  useEffect(() => {
    if (!runId) return;
    apiClient.v2GetSessionRuns(runId).then((resp) => {
      setSessionRuns(resp.runs);
      if (resp.runs.length > 0 && !selectedRunId) setSelectedRunId(resp.runs[0].run_id);
    }).catch(() => setSessionRuns([]));
    apiClient.v2GetSessionMemory(runId, 10).then((resp) => setMemories(resp.memories)).catch(() => {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  // === useEffect: session 第一个 run 的 agent_id → 拉 agent 元数据动态化头部
  // 数据源优先级：①getAgent(agent_id) → name + model ②getRuntimeSummary() → manager_provider/manager_model
  // 避免硬编码「Manager Agent · deepseek-v4-pro」（v59 之前是写死的，session 切换不更新）
  useEffect(() => {
    let cancelled = false;
    const firstAgentId = sessionRuns[0]?.agent_id;
    if (firstAgentId) {
      apiClient.getAgent(firstAgentId)
        .then((resp) => {
          if (cancelled) return;
          const a = resp.agent as Record<string, unknown>;
          // 后端 _agent_def_to_dict 返回字段：id / name (= display_name) / model (= {provider, id} 对象) / harness
          const displayName = (a.name as string) || (a.display_name as string) || firstAgentId;
          const modelObj = a.model as { provider?: string; id?: string } | string | undefined;
          const modelStr = typeof modelObj === 'string'
            ? modelObj
            : (modelObj?.provider && modelObj?.id ? `${modelObj.provider}/${modelObj.id}` : '');
          const harness = (a.harness as string) || '';
          const tier = (a.tier as string) || 'T2';
          setCurrentAgent({ id: firstAgentId, displayName, model: modelStr, harness, tier });
        })
        .catch(() => { if (!cancelled) fallbackToManagerModel(); });
    } else {
      fallbackToManagerModel();
    }
    function fallbackToManagerModel() {
      // 直接取 manager agent 的 display_name / harness / model（config/agents/manager.yaml），
      // 避免硬编码「超级智能助手」（v90 起；用户改 yaml 后 header 立即跟随）。
      apiClient.getAgent('manager')
        .then((resp) => {
          if (cancelled) return;
          const a = resp.agent as Record<string, unknown>;
          const displayName = (a.name as string) || (a.display_name as string) || 'Manager';
          const modelObj = a.model as { provider?: string; id?: string } | string | undefined;
          const modelStr = typeof modelObj === 'string'
            ? modelObj
            : (modelObj?.provider && modelObj?.id ? `${modelObj.provider}/${modelObj.id}` : '');
          const harness = (a.harness as string) || '';
          const tier = (a.tier as string) || 'T0';
          setCurrentAgent({ id: 'manager', displayName, model: modelStr, harness, tier });
        })
        .catch(() => { if (!cancelled) setCurrentAgent(null); });
    }
    return () => { cancelled = true; };
  }, [sessionRuns]);

  // === useEffect: 滚动到底部 ===
  useEffect(() => { scrollToBottom(); }, [messages, scrollToBottom]);

  // === useEffect: SSE cleanup ===
  useEffect(() => () => { if (cleanupRef.current) cleanupRef.current(); }, []);

  // === 派生 ===
  const totalTokens = tokenIn + tokenOut;
  const isRunning = runStatus === 'running' || runStatus === 'pending';

  // ===== render =====
  // surface 统计（标题行展示；SupervisionPanel 内部 summary 头已隐藏）
  const surfaceList = Object.values(surfaceSnapshots);
  const finalSurfaceCount = surfaceList.filter(s => s.phase === 'final').length;
  const partialSurfaceCount = surfaceList.filter(s => s.phase === 'partial').length;
  return (
    <div className="sa-root">
      {/* SSE 断连提示 */}
      {!sseConnected && isRunning && (
        <div style={{ background: 'rgba(251,191,36,.12)', borderBottom: '1px solid var(--amber)', color: 'var(--amber)', padding: '6px 16px', fontSize: 12, textAlign: 'center' }}>
          ⚠️ 事件流连接中断，正在自动重连（run 仍在后端执行）
        </div>
      )}

      {/* ── 两栏布局：左主区(生成式UI+授权+输入框) + 右对话抽屉（可收起）── */}
      <div className={`sa-session-layout ${genuiFullscreen ? 'genui-fullscreen' : ''} ${chatDrawerOpen ? '' : 'drawer-collapsed'}`}>
        {/* === 左主区：生成式 UI 大屏 + 授权选择 + 输入框 === */}
        <div className="sa-main-pane">
          {/* 主区顶部：生成式 UI 标题（含 surface 统计 + 全屏按钮） */}
          <div className="sa-detail-tabs">
            <div className="sa-detail-tab active">
              {/* 面板线性图标（替代 emoji） */}
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ marginRight: 6, flexShrink: 0 }}>
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <path d="M3 9h18" />
                <path d="M9 21V9" />
              </svg>
              生成式UI交互面板
              {/* surface 状态统计（从 SupervisionPanel 内部上提到标题行） */}
              <span className="sa-genui-summary">
                {surfaceList.length > 0
                  ? `${surfaceList.length} 个 active surface`
                    + (finalSurfaceCount > 0 ? ` · ${finalSurfaceCount} final` : '')
                    + (partialSurfaceCount > 0 ? ` · ${partialSurfaceCount} partial` : '')
                  : '等待 agent emit surface（report_surface_state）…'}
              </span>
            </div>
            {/* 全屏切换按钮 */}
            <button
              className="sa-genui-fullscreen-btn"
              onClick={() => setGenuiFullscreen(f => !f)}
              title={genuiFullscreen ? '退出全屏' : '全屏大屏'}
            >
              {genuiFullscreen ? (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M8 3v3a2 2 0 0 1-2 2H3" /><path d="M21 8h-3a2 2 0 0 1-2-2V3" /><path d="M3 16h3a2 2 0 0 1 2 2v3" /><path d="M16 21v-3a2 2 0 0 1 2-2h3" />
                </svg>
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 8V5a2 2 0 0 1 2-2h3" /><path d="M21 8V5a2 2 0 0 0-2-2h-3" /><path d="M3 16v3a2 2 0 0 0 2 2h3" /><path d="M16 21v-3a2 2 0 0 1 2-2h3" />
                </svg>
              )}
            </button>
          </div>

          {/* 生成式 UI 大屏内容区：SupervisionPanel 是唯一出口
              （DAG surface 按 (actor_id, view_id) 聚合，phase 原地替换不堆积） */}
          <div className="sa-detail-content sa-main-canvas">
            <div className={`sa-supervision-section ${genuiFullscreen ? 'fullscreen' : ''}`}>
              <SupervisionPanel
                snapshots={surfaceSnapshots}
                actorProfiles={actorProfiles}
                emptyText="等待 agent emit surface（report_surface_state）…"
                showSummary={false}
                onSurfaceAction={(view_id, action_name) => {
                  setMessages((prev) => [
                    ...prev,
                    {
                      role: 'system',
                      content: `surface action: view=${view_id} action=${action_name}`,
                      timestamp: new Date().toISOString(),
                    },
                  ]);
                }}
              />
            </div>
          </div>

          {/* 主区底部：工作区授权选择 + 输入框（Slack 风格） */}
          {!genuiFullscreen && (
            <div className="sa-main-footer">
              {/* 工作区授权选择条（复用 WorkspaceStatusBadge） */}
              <div className="sa-workspace-bar">
                <div className="sa-agent-info">
                  <div className="sa-avatar agent" style={{ width: 24, height: 24, fontSize: 11 }}><span className="sa-avatar-letter">M</span></div>
                  <span className="sa-agent-name">{currentAgent?.displayName ?? 'Manager Agent'}</span>
                  <span className="sa-agent-model">{currentAgent ? `${capitalize(currentAgent.harness)} · ${currentAgent.model}` : 'OpenCode · —'}</span>
                  {runStatus === 'running' && <span className="sa-agent-status">● 工作中</span>}
                  {runStatus !== 'running' && runId && <span className="sa-agent-status idle">● 空闲</span>}
                </div>
                <WorkspacePicker
                  sessionId={runId}
                  defaultAgentTier={(currentAgent?.tier as AgentTier | undefined) ?? 'T0'}
                  onSwitchWorkspace={handleSelectorSelect}
                />
                {/* 会话权限级别按钮（与工作区独立，每次发消息前可切换） */}
                <PermissionLevelButton
                  sessionId={runId}
                  currentLevel={permissionLevel}
                  onLevelChanged={(lvl: PermissionLevel) => setPermissionLevel(lvl)}
                />
                {/* P2：审批弹窗（tier 不足时 agent 工具调用请求 allowed-once 放行） */}
                <ApprovalPrompt
                  request={approvalRequest}
                  onSettled={() => setApprovalRequest(null)}
                />
                {/* 历史会话 + 新建会话按钮 */}
                <div className="sa-workspace-actions">
                  <button
                    className={`sa-header-btn ${historyOpen ? 'active' : ''}`}
                    onClick={handleToggleHistory}
                    title="历史会话"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M3 3v5h5" /><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8" /><path d="M12 7v5l4 2" />
                    </svg>
                  </button>
                  <button
                    className="sa-header-btn"
                    onClick={handleNewSession}
                    title="新建会话"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
                    </svg>
                  </button>
                </div>
              </div>

              {/* waiting bar（await_command 状态时显示） */}
              {runStatus === 'waiting' && (
                <div className="sa-waiting-bar">
                  <span className="wb-icon">⏸</span>
                  <div className="wb-text">
                    <div className="wb-title">任务等待你的确认</div>
                    <div className="wb-detail">await_command 节点 · 点击确认继续</div>
                  </div>
                  <div className="wb-actions">
                    <button className="wb-btn primary" onClick={() => { /* TODO: send_actor_command */ }}>✓ 确认</button>
                    <button className="wb-btn danger" onClick={handleStop}>✗ 取消</button>
                  </div>
                </div>
              )}

              {/* 输入框 */}
              <div className="sa-chat-input">
                <div className="sa-input-card">
                  <textarea
                    ref={inputRef as React.RefObject<HTMLTextAreaElement>}
                    className="sa-input-area"
                    placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
                    value={inputText}
                    rows={1}
                    onChange={(e) => {
                      setInputText(e.target.value);
                      const el = e.target;
                      el.style.height = 'auto';
                      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        if (isGenerating) handleStop();
                        else if (inputText.trim()) handleSend();
                      }
                    }}
                  />
                  <div className="sa-input-actions">
                    {isGenerating ? (
                      <button onClick={handleStop} className="sa-send-btn stop">⏹ 停止</button>
                    ) : (
                      <button onClick={handleSend} disabled={!inputText.trim()} className="sa-send-btn">发送</button>
                    )}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* === 右侧：对话记录抽屉（可收起） === */}
        <div className="sa-chat-drawer">
          <div className="sa-chat-header">
            <div className="sa-chat-header-text">
              <div className="title">{historySessions.find(s => s.session_id === runId)?.title || messages.find(m => m.role === 'user')?.content?.slice(0, 40) || ''}</div>
            </div>
            {memories.length > 0 && (
              <div className="memory-indicator" title={`记忆库 ${memories.length} 条`}>🧠 {memories.length}</div>
            )}
            <button
              className="sa-header-btn"
              onClick={onToggleChatDrawer}
              title="收起对话记录"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M18 6L6 18" /><path d="M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* 历史会话下拉面板 */}
          {historyOpen && (
            <div className="sa-history-panel">
              <div className="sa-history-header">
                <span className="title">历史会话</span>
                <span className="count">{historySessions.length}</span>
                <button className="sa-history-close" onClick={() => setHistoryOpen(false)}>✕</button>
              </div>
              <div className="sa-history-list">
                {historyLoading ? (
                  <div className="sa-history-empty">加载中…</div>
                ) : historySessions.length === 0 ? (
                  <div className="sa-history-empty">暂无历史会话</div>
                ) : (
                  historySessions.map((s) => (
                    <div
                      key={s.session_id}
                      className={`sa-history-item ${s.session_id === runId ? 'active' : ''} ${(!s.title || !s.title.trim()) ? 'empty' : ''}`}
                      onClick={() => handleSelectHistory(s.session_id)}
                    >
                      <div className="hi-row">
                        <span className="hi-title">
                          {s.title && s.title.trim() ? s.title : '(空会话)'}
                        </span>
                        <span className="hi-status">{s.status || ''}</span>
                      </div>
                      <div className="hi-meta">
                        <span>{s.agent_id || 'manager'}</span>
                        <span>·</span>
                        <span>{s.last_activity_at ? new Date(s.last_activity_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}</span>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}

          {/* 消息列表 */}
          <div className="sa-chat-messages">
            {messages.length === 0 && !loading && (
              <CapabilityGallery
                onPick={(prompt) => {
                  setInputText(prompt);
                  inputRef.current?.focus();
                }}
              />
            )}
            {loading && messages.length === 0 && (
              <div className="sa-thinking-indicator">
                <div className="dots"><div className="dot" /><div className="dot" /><div className="dot" /></div>
                <span className="text">正在加载会话...</span>
              </div>
            )}
            {messages.map((msg, i) => (
              <MessageRow key={i} msg={msg} runId={runId} agentName={currentAgent?.displayName}
                onWidgetInput={handleWidgetInput}
              />
            ))}
            {isGenerating && messages.length > 0 && messages[messages.length - 1].role === 'user' && (
              <div className="sa-thinking-indicator">
                <div className="dots"><div className="dot" /><div className="dot" /><div className="dot" /></div>
                <span className="text">正在思考...</span>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        </div>
      </div>

      {/* ── 命令弹窗 ── */}
      {commandModal.open && (
        <div className="sa-command-modal-overlay show" onClick={() => setCommandModal({ open: false, actorId: '' })}>
          <div className="sa-command-modal" onClick={(e) => e.stopPropagation()}>
            <div className="cm-header">
              <span style={{ fontSize: 18 }}>📨</span>
              <span className="cm-title">给 Actor 发送命令</span>
              <span className="cm-target">actor: {commandModal.actorId}</span>
            </div>
            <div className="cm-body">
              <div className="cm-label">命令内容</div>
              <textarea className="cm-textarea" placeholder="输入要发送给 Actor 的指令..." autoFocus />
            </div>
            <div className="cm-actions">
              <button className="sa-btn" onClick={() => setCommandModal({ open: false, actorId: '' })}>取消</button>
              <button className="sa-btn primary" onClick={() => { setCommandModal({ open: false, actorId: '' }); alert('命令已发送（待后端 Phase 6 实现 send_actor_command）'); }}>发送命令</button>
            </div>
          </div>
        </div>
      )}

      {/* ── P0.18.7c: 工作区选择弹窗 ── */}
      <WorkspaceSelectorDialog
        open={selectorOpen}
        onClose={() => setSelectorOpen(false)}
        defaultAgentTier={(currentAgent?.tier as AgentTier | undefined) ?? 'T2'}
        defaultAgentName={currentAgent?.displayName ?? 'manager'}
        onSelect={handleSelectorSelect}
      />
    </div>
  );
}

// ===== 子组件：消息行 =====
function MessageRow({ msg, runId, onWidgetInput, agentName }: {
  msg: ChatMsg; runId: string | null;
  onWidgetInput: (widgetId: string, input: Record<string, unknown>) => void;
  agentName?: string;
}) {
  if (msg.role === 'widget' && msg.widget) {
    // widget.update 事件统一在对话区内联渲染（清理后无展示型分流）
    const w = msg.widget;
    const icon = WIDGET_ICON[w.type] || '📋';
    const label = WIDGET_LABEL[w.type] || w.type;
    return (
      <div className="sa-msg widget-inline">
        <div className={`sa-avatar agent`}>
          <span className="sa-avatar-letter">M</span>
        </div>
        <div className="bubble-wrap" style={{ maxWidth: '100%', flex: 1 }}>
          <div className="sa-widget-inline-card">
            <div className="widget-header">
              <span className="widget-icon">{icon}</span>
              <span className="widget-title">{label}</span>
              <span className="widget-type">{w.type}</span>
              <span className="widget-badge">可交互</span>
            </div>
            <div className="widget-body">
              <WidgetRenderer
                widget={w}
                onWidgetInput={onWidgetInput}
              />
            </div>
          </div>
          {msg.timestamp && (
            <div className="bubble-meta">{new Date(msg.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</div>
          )}
        </div>
      </div>
    );
  }

  // v74：工具调用时间线（类 Claude Code 风格）
  if (msg.role === 'tool_use' && msg.tool_use) {
    const tu = msg.tool_use;
    const name = tu.tool_name || 'tool';
    const input = tu.input as Record<string, unknown> | undefined;
    const summary = input && typeof input === 'object'
      ? Object.entries(input).map(([k, v]) => {
          const val = typeof v === 'string' ? v : JSON.stringify(v);
          return `${k}: ${val.length > 80 ? val.slice(0, 80) + '…' : val}`;
        }).slice(0, 2).join('  ·  ')
      : '';
    return (
      <div className="sa-msg-tool-use">
        <div className="tu-dot" />
        <div className="tu-body">
          <span className="tu-name">{name}</span>
          {summary && <span className="tu-summary">{summary}</span>}
        </div>
      </div>
    );
  }

  if (msg.role === 'system' && msg.node_status) {
    const st = msg.node_status.status;
    const icon = st === 'completed' ? '✓' : st === 'started' ? '▶' : st === 'failed' ? '✗' : '⊘';
    return (
      <div className={`sa-msg-status ${st === 'completed' ? 'completed' : st === 'failed' ? 'failed' : ''}`}>
        <span className="icon" style={{ color: STATUS_COLOR[st] || 'var(--text-dim)' }}>{icon}</span>
        <span>节点「{msg.node_id}」{st === 'completed' ? '已完成' : st === 'started' ? '正在执行...' : st === 'failed' ? '失败' : '已跳过'}</span>
        <div className="meta">
          {msg.node_status.duration_ms != null && <span>{(msg.node_status.duration_ms / 1000).toFixed(1)}s</span>}
          {msg.node_status.tokens != null && <span>{msg.node_status.tokens.toLocaleString()} tk</span>}
        </div>
      </div>
    );
  }

  if (msg.role === 'system' && msg.content) {
    return (
      <div className="sa-msg system">
        <div className="bubble-wrap"><div className="bubble">{msg.content}</div></div>
      </div>
    );
  }

  // 头像字母：assistant 固定 M（manager agent 唯一），user 取「你」
  const avatarLetter = msg.role === 'user' ? '你' : 'M';
  const time = msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';

  // 思考内容可折叠判定：仅折叠 <think>...</think> 块，非 think 部分正常显示
  const hasThink = msg.role === 'assistant' && msg.content.includes('<think>');

  // markdown 渲染：assistant 回复 + user 消息都渲染（支持表格/加粗/列表/代码块）
  // 注意：用 dangerouslySetInnerHTML 注入 md.render 输出的 HTML（markdown-it 已禁 html/link/image，XSS 安全）
  const htmlContent = useMemo(() => renderMarkdown(msg.content), [msg.content]);

  return (
    <div className={`sa-msg ${msg.role === 'user' ? 'user' : 'assistant'}`}>
      <div className={`sa-avatar ${msg.role === 'user' ? 'user' : 'agent'}`}>
        <span className="sa-avatar-letter">{avatarLetter}</span>
      </div>
      <div className="bubble-wrap">
        {hasThink ? (
          <ThinkBubble content={msg.content} />
        ) : (
          <div
            className="bubble sa-md-bubble"
            dangerouslySetInnerHTML={{ __html: htmlContent }}
          />
        )}
        {time && <div className="bubble-meta">{time}</div>}
      </div>
    </div>
  );
}

// ===== 子组件：<think> 折叠气泡（仅折叠 think 块，按钮钉在 think 块顶部）=====
// 需求：只折叠 <think>...</think> 内容；收起/展开按钮放在 think 块起始位置，
// 让用户展开后不必滚到底部就能再次折叠。
function ThinkBubble({ content }: { content: string }) {
  // 按 <think>…</think> 分割，保留分隔符
  const segments = content.split(/(<think>[\s\S]*?<\/think>)/g);
  // 非 think 段走 markdown 渲染（支持表格/加粗/列表等）
  const renderedSegments = useMemo(() => segments.map((seg) => {
    if (seg.startsWith('<think>') && seg.endsWith('</think>')) {
      return { type: 'think' as const, inner: seg.slice(7, -8) };
    }
    return { type: 'md' as const, html: renderMarkdown(seg) };
  }), [content]);

  return (
    <div className="bubble sa-md-bubble">
      {renderedSegments.map((seg, i) => {
        if (seg.type === 'think') {
          return <CollapsibleThink key={i} content={seg.inner} />;
        }
        return <div key={i} dangerouslySetInnerHTML={{ __html: seg.html }} />;
      })}
    </div>
  );
}

function CollapsibleThink({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false);
  const preview = content.length > 120 ? content.slice(0, 120) : content;

  return (
    <div className="sa-think-block">
      {/* 按钮钉在 think 块顶部，展开后仍然可见 */}
      <div
        className="sa-think-toggle"
        onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
      >
        <span className="sa-think-icon">{expanded ? '▼' : '▶'}</span>
        <span>思考过程{expanded ? '（点击收起）' : '（点击展开）'}</span>
        {!expanded && <span className="sa-think-preview"> — {preview.slice(0, 80)}…</span>}
      </div>
      {expanded && (
        <div className="sa-think-body">{content}</div>
      )}
    </div>
  );
}

// ===== 子组件：空状态欢迎气泡（极简版，替代 v70 的 CapabilityGallery）=====
// 设计思路：对话框空状态应该是简洁入口，不放 hero 标题、不放菜单跳转卡片
//           （那些是侧边栏的职责）。只放一句欢迎语 + 几个示例 prompt 气泡，点击填入输入框。
const PROMPT_SUGGESTIONS: Array<{ icon: string; text: string }> = [
  { icon: '📝', text: '帮我整理本周的工作要点，并生成一份周报大纲' },
  { icon: '🔍', text: '什么是 harness 引擎？' },
  { icon: '📊', text: '触发一次日志巡检工作流' },
  { icon: '🎬', text: '帮我制作一段关于 AgentOps 项目介绍的 30 秒视频' },
];

function CapabilityGallery({
  onPick,
}: {
  onPick: (prompt: string) => void;
  onNavigate?: (page: PageId) => void; // 保留 prop 兼容（不再使用）
}) {
  return (
    <div className="sa-empty-prompts">
      <div className="sa-empty-greeting">
        👋 有什么可以帮你的？
      </div>
      <div className="sa-empty-hint">直接描述需求，或点击下方示例快速开始</div>
      <div className="sa-empty-bubbles">
        {PROMPT_SUGGESTIONS.map((s) => (
          <button
            key={s.text}
            className="sa-prompt-bubble"
            onClick={() => onPick(s.text)}
            type="button"
          >
            <span className="bubble-icon">{s.icon}</span>
            <span className="bubble-text">{s.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
