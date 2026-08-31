import { useState, useEffect, useCallback } from 'react';
import { AppSidebar } from './components/AppSidebar';
import { TopBar, ChatTopBar } from './components/TopBar';
import { AuthProvider, useAuth, LoginView, MustResetView } from './components/AuthGate';
import { MonitorCenter } from './pages/MonitorCenter';
import { WorkflowsPage } from './pages/WorkflowsPage';
import { SuperAgentPage } from './pages/SuperAgentPage';
import { AgentsPage } from './pages/AgentsPage';
import { RuntimeSettingsPage } from './pages/RuntimeSettingsPage';
import { ModelProvidersPage } from './pages/ModelProvidersPage';
import { SchedulesPage } from './pages/SchedulesPage';
import { ProviderSettings } from './pages/ProviderSettings';
import { SecurityPage } from './pages/SecurityPage';
import { KnowledgeHubPage } from './pages/KnowledgeHubPage';
import { CollaborationCenterPage } from './pages/CollaborationCenterPage';
import TaskCenterPage from './pages/TaskCenterPage';
import CodingTerminalPage from './components/task/CodingTerminalPage';
import { OnboardingPage } from './pages/OnboardingPage';
import { apiClient } from './lib/api';

// 超级智能体单 session 模型：SuperAgentPage 是唯一主视图（生成式UI大屏 + 对话抽屉）
// 工作区授权已并入「运行环境」页的 工作区 面板（不再作为一级菜单）
export type PageId = 'chat' | 'workflows' | 'monitor' | 'agents' | 'runtime-settings' | 'model-providers' | 'provider-settings' | 'schedules' | 'knowledge' | 'collaboration' | 'task-center' | 'coding' | 'security-users' | 'security-roles' | 'security-sessions' | 'security-tokens';

const PAGE_TITLES: Record<PageId, string> = {
  'chat': '工作台',
  'workflows': '工作流管理',
  'monitor': '监控中心',
  'agents': 'Agent 管理',
  'runtime-settings': '运行环境',
  'model-providers': '模型供应商',
  'provider-settings': '凭据管理',
  'schedules': '定时计划',
  'knowledge': '知识管理',
  'collaboration': '协作可视化',
  'task-center': '任务管理',
  'coding': 'Coding 终端',
  'security-users': '安全管理 · 用户管理',
  'security-roles': '安全管理 · 角色与权限',
  'security-sessions': '安全管理 · 登录会话',
  'security-tokens': '安全管理 · API Token',
};

// S16：AppShell 是认证通过后的主应用；App 是认证门禁外壳
function AppShell() {
  // Onboarding 探测：首次访问（无 onboarding 记录）时拦截到引导页
  const [onboardingChecked, setOnboardingChecked] = useState(false);
  const [needOnboarding, setNeedOnboarding] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiClient.getOnboardingStatus()
      .then((status) => {
        if (cancelled) return;
        setNeedOnboarding(!status.onboarded);
      })
      .catch(() => {
        if (cancelled) return;
        setNeedOnboarding(false);
      })
      .finally(() => {
        if (!cancelled) setOnboardingChecked(true);
      });
    return () => { cancelled = true; };
  }, []);

  const [currentPage, setCurrentPage] = useState<PageId>('chat');
  // 左侧导航抽屉：默认展开，工作台页面可隐藏
  const [sidebarOpen, setSidebarOpen] = useState(true);
  // 工作流模式：点运行后传 workflowId + inputs
  const [runWorkflowId, setRunWorkflowId] = useState<string | null>(null);
  const [runInputs, setRunInputs] = useState<Record<string, unknown>>({});
  // 对话模式：默认走 Manager Agent
  const [chatAgentId, setChatAgentId] = useState<string>('manager');
  const [chatInitialMessage, setChatInitialMessage] = useState<string>('');
  // 载入历史会话：从 RunHistory/LoadSessionModal 传入 run_id，统一在 chat 页面加载
  const [loadRunId, setLoadRunId] = useState<string | null>(null);
  // 协作可视化：从 RunHistory 点「回放」传入 run_id，复用 collaboration 页面
  const [collabRunId, setCollabRunId] = useState<string | null>(null);
  // 工作台实时 sessionId：SuperAgentPage 创建/切换 session 时上提到 App，
  // 供协作可视化页面自动跟随当前活跃 session（实时过程可视化）
  const [liveSessionId, setLiveSessionId] = useState<string | null>(null);
  // 对话记录抽屉：上提到 App，供 ChatTopBar 按钮控制
  const [chatDrawerOpen, setChatDrawerOpen] = useState(true);

  const handleNavigate = useCallback((page: PageId) => {
    setCurrentPage(page);
  }, []);

  // 工作流管理 → 点运行 → 传 workflowId + inputs，跳对话页
  const handleStartRun = useCallback((workflowId: string, inputs: Record<string, unknown>) => {
    setRunWorkflowId(workflowId);
    setRunInputs(inputs);
    setChatInitialMessage('');
    setLoadRunId(null);
    setCurrentPage('chat');
  }, []);

  // SuperAgentPage 消费完 pendingWorkflow（run 已提交或启动失败）后清空，避免重复触发
  const handleWorkflowLaunched = useCallback(() => {
    setRunWorkflowId(null);
    setRunInputs({});
  }, []);

  // 对话页直接发消息
  const handleStartChat = useCallback((agentId: string, message: string) => {
    setChatAgentId(agentId);
    setChatInitialMessage(message);
    setRunWorkflowId(null);
    setRunInputs({});
    setLoadRunId(null);
    setCurrentPage('chat');
  }, []);

  // 载入历史会话：统一走 chat 页面（不再跳独立页面）
  // 设置 loadRunId 后，RunMonitorPage 的 useEffect 检测到变化即加载该 session 的消息
  const handleLoadRun = useCallback((runId: string) => {
    setLoadRunId(runId);
    setRunWorkflowId(null);
    setRunInputs({});
    setChatInitialMessage('');
    setCurrentPage('chat');
  }, []);

  const handleViewHistory = useCallback(() => {
    setCurrentPage('chat');
  }, []);

  // RunHistory 点「回放」→ 跳协作可视化页面，预选该 run
  const handleReplayRun = useCallback((runId: string) => {
    setCollabRunId(runId);
    setCurrentPage('collaboration');
  }, []);

  const handleManageAgents = useCallback(() => {
    setCurrentPage('agents');
  }, []);

  const handleConfigureHarness = useCallback(() => {
    setCurrentPage('runtime-settings');
  }, []);

  const handleViewWorkflows = useCallback(() => {
    setCurrentPage('workflows');
  }, []);

  const renderPage = () => {
    switch (currentPage) {
      case 'monitor':
        return <MonitorCenter onNavigate={handleNavigate} />;
      case 'chat':
        // 工作台：生成式 UI 大屏 + 对话抽屉（产出物结果可视化交互展示）
        return <SuperAgentPage onNavigate={handleNavigate} onLiveSessionChange={setLiveSessionId} chatDrawerOpen={chatDrawerOpen} onToggleChatDrawer={() => setChatDrawerOpen(v => !v)} pendingWorkflow={runWorkflowId ? { workflowId: runWorkflowId, inputs: runInputs } : null} onWorkflowLaunched={handleWorkflowLaunched} />;
      case 'workflows':
        return <WorkflowsPage onRunWorkflow={handleStartRun} />;
      case 'agents':
        return <AgentsPage onConfigureHarness={handleConfigureHarness} />;
      case 'runtime-settings':
        return <RuntimeSettingsPage />;
      case 'model-providers':
        return <ModelProvidersPage />;
      case 'provider-settings':
        return <ProviderSettings />;
      case 'schedules':
        return <SchedulesPage />;
      case 'knowledge':
        return <KnowledgeHubPage onNavigate={handleNavigate} />;
      case 'collaboration':
        // 协作可视化：实时 DAG workflow 过程可视化
        // liveSessionId 让协作页自动跟随工作台当前活跃 session；collabRunId 用于回放入口
        return <CollaborationCenterPage onNavigate={handleNavigate} initialRunId={collabRunId} liveSessionId={liveSessionId} />;
      case 'task-center':
        return <TaskCenterPage />;
      case 'coding':
        return <CodingTerminalPage />;
      case 'security-users':
        return <SecurityPage section="users" />;
      case 'security-roles':
        return <SecurityPage section="roles" />;
      case 'security-sessions':
        return <SecurityPage section="sessions" />;
      case 'security-tokens':
        return <SecurityPage section="tokens" />;
      default:
        return <SuperAgentPage onNavigate={handleNavigate} onLiveSessionChange={setLiveSessionId} chatDrawerOpen={chatDrawerOpen} onToggleChatDrawer={() => setChatDrawerOpen(v => !v)} pendingWorkflow={runWorkflowId ? { workflowId: runWorkflowId, inputs: runInputs } : null} onWorkflowLaunched={handleWorkflowLaunched} />;
    }
  };

  // 全屏布局页面（工作台、协作可视化）
  const isFullPage = currentPage === 'chat' || currentPage === 'collaboration';
  const isChatLike = currentPage === 'chat' || currentPage === 'collaboration';

  // Onboarding 拦截：未完成引导时短路整个 app-shell
  if (!onboardingChecked) {
    return (
      <div className="app-boot-loading">
        <div className="app-boot-loading-text">正在加载…</div>
      </div>
    );
  }
  if (needOnboarding) {
    return <OnboardingPage onDone={() => setNeedOnboarding(false)} />;
  }

  return (
    <div className={`app-shell ${sidebarOpen ? '' : 'sidebar-collapsed'}`}>
      {/* 全宽顶部栏（logo + 标题 + 状态） */}
      {isChatLike ? (
        <ChatTopBar
          title={PAGE_TITLES[currentPage]}
          onToggleSidebar={() => setSidebarOpen(!sidebarOpen)}
          sidebarOpen={sidebarOpen}
          onToggleChatDrawer={() => setChatDrawerOpen(v => !v)}
          chatDrawerOpen={chatDrawerOpen}
        />
      ) : (
        <TopBar
          title={PAGE_TITLES[currentPage]}
          onToggleSidebar={() => setSidebarOpen(!sidebarOpen)}
          sidebarOpen={sidebarOpen}
        />
      )}
      {/* 下方：sidebar + content 并排 */}
      <div className="app-body">
        <AppSidebar
          currentPage={currentPage}
          onNavigate={handleNavigate}
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
        />
        <div className="app-main">
          {isFullPage ? (
            <div className="page-content-full">
              {renderPage()}
            </div>
          ) : (
            <div className="page-content">
              {renderPage()}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// S16：门禁壳，按 AuthContext 状态分支
function Gate() {
  const { state } = useAuth();
  if (state === 'checking') {
    return (
      <div className="app-boot-loading">
        <div className="app-boot-loading-text">正在校验登录状态…</div>
      </div>
    );
  }
  if (state === 'anon') return <LoginView />;
  if (state === 'must-reset') return <MustResetView />;
  return <AppShell />;
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}