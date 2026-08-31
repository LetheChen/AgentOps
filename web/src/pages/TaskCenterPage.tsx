// web/src/pages/TaskCenterPage.tsx
// V3 任务中心（§4.11）：六视图页签（仪表盘/看板/列表/甘特/网状图/Coding 终端）
// + 卡片点击直达全屏详情页 + modal 创建 + 拖拽锁定 + 2s 轮询
// 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.11

import { useState, useEffect, useCallback } from 'react';
import { taskApi, type Task, type TaskProject, type TaskTransition } from '../api/taskApi';
import TaskReportPage from './TaskReportPage';
import TaskDetailPage from './TaskDetailPage';
import DashboardView from '../components/task/DashboardView';
import TaskListView from '../components/task/TaskListView';
import GanttView from '../components/task/GanttView';
import ProjectGraphView from '../components/task/ProjectGraphView';
import CodingTerminalPage from '../components/task/CodingTerminalPage';

// V3 视图页签
type ViewKey = 'dashboard' | 'board' | 'list' | 'gantt' | 'graph' | 'coding';
const VIEWS: Array<{ key: ViewKey; label: string }> = [
  { key: 'dashboard', label: '📊 仪表盘' },
  { key: 'board', label: '🗂 看板' },
  { key: 'list', label: '📃 列表' },
  { key: 'gantt', label: '📅 甘特图' },
  { key: 'graph', label: '🕸 网状图' },
  { key: 'coding', label: '🖥 Coding 终端' },
];

// v1.2 主线：灵感→讨论→拆解→评审→待办池→执行→验收→关闭（backlog=可执行任务池）
const COLUMNS = ['idea', 'discussing', 'decomposing', 'reviewing', 'backlog', 'in_progress', 'validating', 'closed'];
const COLUMN_LABELS: Record<string, string> = {
  idea: '💡 灵感',
  discussing: '💬 讨论中',
  decomposing: '🔨 拆解中',
  reviewing: '🔍 评审中',
  backlog: '📋 待办池',
  in_progress: '🚀 进行中',
  validating: '✔️ 验证中',
  closed: '✅ 已关闭',
};

// V3.2：阶段卡片栏（状态筛选卡片 + 每卡尾部"+"快捷新增任务，默认创建到本阶段）
// v1.2 按新主线顺序排列
const STAGE_CARDS: Array<{ key: string; label: string; color: string }> = [
  { key: 'idea', label: '灵感', color: '#60A5FA' },
  { key: 'discussing', label: '讨论中', color: '#22D3EE' },
  { key: 'decomposing', label: '拆解中', color: '#F472B6' },
  { key: 'reviewing', label: '评审中', color: '#A78BFA' },
  { key: 'backlog', label: '待办池', color: '#94A3B8' },
  { key: 'in_progress', label: '进行中', color: '#F59E0B' },
  { key: 'validating', label: '验证中', color: '#34D399' },
  { key: 'closed', label: '已关闭', color: '#10B981' },
];

// 任务片按风险等级着色（仅左边框 + badge，背景统一白色与列容器协调）
const RISK_COLORS: Record<string, { border: string; badge: string }> = {
  high:   { border: '#e53935', badge: '#e53935' },
  medium: { border: '#fb8c00', badge: '#fb8c00' },
  low:    { border: '#43a047', badge: '#43a047' },
};
const RISK_LABELS: Record<string, string> = { high: '高', medium: '中', low: '低' };

// ---- modal 创建项目表单 ----
function CreateProjectModal({ onClose, onCreated }: {
  onClose: () => void;
  onCreated: (projectId: string) => void;
}) {
  const [form, setForm] = useState({
    name: '',
    type: 'code',
    local_path: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    if (!form.name.trim()) {
      setError('项目名称不能为空');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const proj = await taskApi.createProject({
        name: form.name,
        type: form.type,
        local_path: form.local_path,
      });
      onCreated(proj.project_id);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : '创建失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={overlayStyle}>
      <div style={modalStyle}>
        <h3 style={{ marginTop: 0, color: 'var(--color-text-primary)' }}>新建项目</h3>
        <input
          placeholder="项目名称（必填）"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          style={inputStyle}
          autoFocus
        />
        <div style={{ display: 'flex', gap: 12, marginTop: 8, color: 'var(--color-text-secondary)' }}>
          <label style={{ flex: 1 }}>
            类型：
            <select
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
              style={selectStyle}
            >
              <option value="code">代码</option>
              <option value="doc">文档</option>
              <option value="research">调研</option>
            </select>
          </label>
        </div>
        <input
          placeholder="本地路径（可选，如 e:/project/xxx）"
          value={form.local_path}
          onChange={(e) => setForm({ ...form, local_path: e.target.value })}
          style={inputStyle}
        />
        {error && <div style={{ color: '#fca5a5', marginTop: 8, fontSize: 13 }}>{error}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 16 }}>
          <button onClick={onClose} style={cancelBtnStyle}>取消</button>
          <button
            onClick={submit}
            disabled={submitting || !form.name.trim()}
            style={submitBtnStyle}
          >
            {submitting ? '创建中...' : '创建'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---- modal 创建任务表单（评审缺点 4 修复：替代 prompt()） ----
// V3.2：支持 defaultStatus（从阶段卡片"+"进入，默认创建到该阶段）
function CreateTaskModal({ projectId, defaultStatus = 'idea', onClose, onCreated }: {
  projectId: string;
  defaultStatus?: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [form, setForm] = useState({
    title: '',
    description: '',
    type: 'code',
    risk_level: 'medium',
    status: defaultStatus,
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    if (!form.title.trim()) {
      setError('标题不能为空');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await taskApi.createTask({
        project_id: projectId,
        title: form.title,
        description: form.description,
        risk_level: form.risk_level,
        status: form.status,
      });
      onCreated();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : '创建失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={overlayStyle}>
      <div style={modalStyle}>
        <h3 style={{ marginTop: 0, color: 'var(--color-text-primary)' }}>新建任务</h3>
        <input
          placeholder="任务标题（必填）"
          value={form.title}
          onChange={(e) => setForm({ ...form, title: e.target.value })}
          style={inputStyle}
          autoFocus
        />
        <textarea
          placeholder="任务描述（可选）"
          value={form.description}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
          style={{ ...inputStyle, minHeight: 80, resize: 'vertical' }}
        />
        <div style={{ display: 'flex', gap: 12, marginTop: 8, color: 'var(--color-text-secondary)' }}>
          <label style={{ flex: 1 }}>
            类型：
            <select
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
              style={selectStyle}
            >
              <option value="code">代码</option>
              <option value="doc">文档</option>
              <option value="research">调研</option>
            </select>
          </label>
          <label style={{ flex: 1 }}>
            风险：
            <select
              value={form.risk_level}
              onChange={(e) => setForm({ ...form, risk_level: e.target.value })}
              style={selectStyle}
            >
              <option value="low">低</option>
              <option value="medium">中</option>
              <option value="high">高</option>
            </select>
          </label>
          <label style={{ flex: 1 }}>
            阶段：
            <select
              value={form.status}
              onChange={(e) => setForm({ ...form, status: e.target.value })}
              style={selectStyle}
            >
              {STAGE_CARDS.map((s) => (
                <option key={s.key} value={s.key}>{s.label}</option>
              ))}
            </select>
          </label>
        </div>
        {error && <div style={{ color: '#fca5a5', marginTop: 8, fontSize: 13 }}>{error}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 16 }}>
          <button onClick={onClose} style={cancelBtnStyle}>取消</button>
          <button
            onClick={submit}
            disabled={submitting || !form.title.trim()}
            style={submitBtnStyle}
          >
            {submitting ? '创建中...' : '创建'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function TaskCenterPage() {
  const [projects, setProjects] = useState<TaskProject[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [transitionsMap, setTransitionsMap] = useState<Record<string, TaskTransition[]>>({});
  const [revision, setRevision] = useState(0);
  const [showModal, setShowModal] = useState(false);
  const [showProjectModal, setShowProjectModal] = useState(false);
  // V3.2：状态筛选（提升，由阶段卡片栏驱动）+ 新建任务默认阶段
  const [statusFilter, setStatusFilter] = useState('');
  const [createTaskStatus, setCreateTaskStatus] = useState('idea');
  const [isDragging, setIsDragging] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  // V1：board/report 页面切换 + 当前选中的任务
  const [currentPage, setCurrentPage] = useState<'views' | 'detail' | 'report'>('views');
  const [selectedTaskId, setSelectedTaskId] = useState('');
  // V3.1：当前视图页签（默认仪表盘）
  const [activeView, setActiveView] = useState<ViewKey>('dashboard');
  // V2-W4：任务搜索
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<Task[] | null>(null);
  const [searching, setSearching] = useState(false);

  // 拉项目列表
  const loadProjects = useCallback(async (selectId?: string) => {
    try {
      const data = await taskApi.listProjects();
      setProjects(data.projects);
      const target = selectId || (data.projects.length > 0 ? data.projects[0].project_id : '');
      setSelectedProjectId(target);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载项目失败');
    }
  }, []);

  // 拉任务列表
  const loadTasks = useCallback(async () => {
    if (!selectedProjectId) return;
    setLoading(true);
    try {
      const data = await taskApi.listTasks(selectedProjectId);
      setTasks(data.tasks);
      setRevision(data.revision);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载任务失败');
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId]);

  // V2-W4：任务搜索（防抖 300ms）
  useEffect(() => {
    const q = searchQuery.trim();
    if (!q) {
      setSearchResults(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    const timer = setTimeout(async () => {
      try {
        const data = await taskApi.search(q, selectedProjectId || undefined);
        setSearchResults(data.tasks);
        setError('');
      } catch (e) {
        setError(e instanceof Error ? e.message : '搜索失败');
        setSearchResults([]);
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery, selectedProjectId]);

  // 初始加载项目
  useEffect(() => {
    loadProjects();
  }, [loadProjects]);

  // 项目切换时加载任务
  useEffect(() => {
    if (selectedProjectId) loadTasks();
  }, [selectedProjectId, loadTasks]);

  // 拉每个任务的合法转移（用于禁用非法列）
  useEffect(() => {
    tasks.forEach(async (t) => {
      if (transitionsMap[t.task_id]) return;
      try {
        const data = await taskApi.getTransitions(t.task_id);
        setTransitionsMap((m) => ({ ...m, [t.task_id]: data.transitions }));
      } catch {
        // 忽略单个任务拉取失败
      }
    });
  }, [tasks]); // eslint-disable-line react-hooks/exhaustive-deps

  // 2s 轮询 revision（拖拽时暂停，防覆盖）
  useEffect(() => {
    if (isDragging || !selectedProjectId) return;
    const timer = setInterval(async () => {
      try {
        const data = await taskApi.getRevision();
        if (data.revision > revision) {
          loadTasks(); // revision 变了才刷新
        }
      } catch {
        // 忽略轮询失败
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [selectedProjectId, revision, isDragging, loadTasks]);

  // 拖拽处理
  const handleDragStart = () => setIsDragging(true);

  const handleDragEnd = async (taskId: string, targetStatus: string) => {
    setIsDragging(false);
    const task = tasks.find((t) => t.task_id === taskId);
    if (!task || task.status === targetStatus) return;

    // 前端按 transitions 禁用非法列（评审吸收点 ②）
    const allowed = transitionsMap[taskId] || [];
    const isAllowed = allowed.some((t) => t.to === targetStatus && t.requires_user);
    // 自动推进态（requires_user=false）不允许前端拖拽，由 agent 触发
    if (!isAllowed) {
      const allowedStr = allowed.filter((t) => t.requires_user)
        .map((t) => COLUMN_LABELS[t.to] || t.to).join('、');
      setError(`非法转移：${COLUMN_LABELS[task.status]}→${COLUMN_LABELS[targetStatus]}。` +
        `允许拖到：${allowedStr || '无（当前状态需 agent 推进）'}`);
      return;
    }

    setError('');
    try {
      const updated = await taskApi.advance(taskId, targetStatus, task.version, {
        actor: 'user',
        comment: '前端拖拽',
      });
      // 更新本地状态
      setTasks((prev) => prev.map((t) => (t.task_id === taskId ? updated : t)));
      // 重新拉 transitions
      const newTrans = await taskApi.getTransitions(taskId);
      setTransitionsMap((m) => ({ ...m, [taskId]: newTrans.transitions }));
    } catch (e) {
      setError(e instanceof Error ? e.message : '推进失败');
      loadTasks(); // 恢复真实状态
    }
  };

  // V3：卡片/行/节点点击直达详情页（统一入口）
  const openTask = useCallback((taskId: string) => {
    setSelectedTaskId(taskId);
    setCurrentPage('detail');
  }, []);

  // V1→V3：详情页（信息聚合：关系 + 回顾 + 评论@agent + 报告入口）
  if (currentPage === 'detail' && selectedTaskId) {
    return (
      <TaskDetailPage
        taskId={selectedTaskId}
        projectTasks={tasks}
        onBack={() => setCurrentPage('views')}
        onOpenTask={openTask}
        onGoReport={(tid) => {
          setSelectedTaskId(tid);
          setCurrentPage('report');
        }}
      />
    );
  }

  // V1：报告页（博客评论模式）—— 选中任务后切换到此视图
  if (currentPage === 'report' && selectedTaskId) {
    return (
      <TaskReportPage
        taskId={selectedTaskId}
        onBack={() => setCurrentPage('views')}
      />
    );
  }

  return (
    <div style={{
      padding: 16, height: '100%', boxSizing: 'border-box',
      display: 'flex', flexDirection: 'column',
      color: 'var(--color-text-primary)',
    }}>
      {/* 顶部栏：项目选择（含"新建项目"选项）+ 搜索（看板视图） */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 12, alignItems: 'center' }}>
        <h2 style={{
          margin: 0, height: 38, display: 'flex', alignItems: 'center',
          fontSize: 20, color: 'var(--color-text-primary)',
        }}>
          任务中心
        </h2>
        <select
          value={selectedProjectId}
          onChange={(e) => {
            if (e.target.value === '__create_project__') {
              // V3.2：下拉内"新建项目"选项 → 弹 modal（选择保持原项目）
              setShowProjectModal(true);
              return;
            }
            setSelectedProjectId(e.target.value);
            setStatusFilter('');
          }}
          style={selectStyle}
        >
          {projects.length === 0 && <option value="">无项目</option>}
          {projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>{p.name}</option>
          ))}
          <option value="__create_project__">＋ 新建项目…</option>
        </select>
        {/* V2-W4：任务搜索框（看板视图显示） */}
        {activeView === 'board' && (
        <div style={{ position: 'relative', flex: 1, maxWidth: 320 }}>
          <input
            type="text"
            placeholder="搜索任务标题/描述/标识符..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              ...inputStyle,
              width: '100%',
              paddingLeft: 32,
              // 与标题/下拉框等高对齐：抵消 inputStyle 的 8px 上下 margin，
              // 避免看板视图搜索框把顶部栏撑高、标题错位 8px
              margin: 0,
              height: 38,
            }}
          />
          <span style={{
            position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)',
            color: 'var(--color-text-tertiary)', fontSize: 14, pointerEvents: 'none',
          }}>
            {searching ? '⋯' : '🔍'}
          </span>
          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              style={{
                position: 'absolute', right: 6, top: '50%', transform: 'translateY(-50%)',
                background: 'none', border: 'none', cursor: 'pointer',
                color: 'var(--color-text-tertiary)', fontSize: 16, padding: '0 4px',
              }}
              title="清除搜索"
            >
              ✕
            </button>
          )}
        </div>
        )}
        {loading && <span style={{ color: 'var(--color-text-tertiary)', fontSize: 13 }}>加载中...</span>}
      </div>

      {/* V3 视图页签栏 */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 12, borderBottom: '1px solid var(--color-border-subtle)', paddingBottom: 0 }}>
        {VIEWS.map((v) => (
          <button
            key={v.key}
            onClick={() => setActiveView(v.key)}
            style={{
              padding: '8px 16px', cursor: 'pointer', fontSize: 13,
              background: activeView === v.key ? 'var(--color-primary)' : 'transparent',
              color: activeView === v.key ? '#fff' : 'var(--color-text-secondary)',
              border: 'none',
              borderBottom: `2px solid ${activeView === v.key ? 'var(--color-primary)' : 'transparent'}`,
              borderRadius: 'var(--radius-sm) var(--radius-sm) 0 0',
              fontWeight: activeView === v.key ? 600 : 400,
              marginBottom: -1,
            }}
          >
            {v.label}
          </button>
        ))}
      </div>

      {/* V3.2 阶段卡片栏：点卡片筛选（切到列表视图）；卡片尾部"+"新建任务（默认到本阶段） */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
        {/* 全部卡片（无"+"，只筛选） */}
        <div
          onClick={() => {
            setStatusFilter('');
            if (activeView !== 'list') setActiveView('list');
          }}
          style={{
            ...stageCardStyle,
            cursor: 'pointer',
            borderColor: statusFilter === '' ? 'var(--color-primary)' : 'var(--color-border-subtle)',
            background: statusFilter === '' ? 'rgba(59,130,246,0.12)' : 'var(--color-bg-surface)',
          }}
          title="查看全部任务"
        >
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 600 }}>全部</span>
          <span style={{ fontSize: 18, fontWeight: 700, color: 'var(--color-text-primary)' }}>{tasks.length}</span>
        </div>
        {STAGE_CARDS.map((s) => {
          const count = tasks.filter((t) => t.status === s.key).length;
          const active = statusFilter === s.key;
          return (
            <div
              key={s.key}
              onClick={() => {
                setStatusFilter(active ? '' : s.key);
                if (activeView !== 'list') setActiveView('list');
              }}
              style={{
                ...stageCardStyle,
                cursor: 'pointer',
                borderColor: active ? s.color : 'var(--color-border-subtle)',
                background: active ? `${s.color}1a` : 'var(--color-bg-surface)',
              }}
              title={`筛选「${s.label}」阶段任务`}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: s.color, flexShrink: 0 }} />
                <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 600, whiteSpace: 'nowrap' }}>{s.label}</span>
              </span>
              <span style={{ fontSize: 18, fontWeight: 700, color: 'var(--color-text-primary)' }}>{count}</span>
              {/* 尾部"+"：新建任务，默认创建到本阶段 */}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  if (!selectedProjectId) return;
                  setCreateTaskStatus(s.key);
                  setShowModal(true);
                }}
                disabled={!selectedProjectId}
                title={`在「${s.label}」阶段新建任务`}
                style={{
                  width: 22, height: 22, flexShrink: 0,
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  borderRadius: 'var(--radius-sm)', cursor: selectedProjectId ? 'pointer' : 'not-allowed',
                  background: `${s.color}22`, color: s.color,
                  border: `1px solid ${s.color}55`,
                  fontSize: 14, fontWeight: 700, lineHeight: 1, padding: 0,
                  opacity: selectedProjectId ? 1 : 0.4,
                }}
              >
                ＋
              </button>
            </div>
          );
        })}
      </div>

      {error && (
        <div style={{
          background: 'rgba(239, 68, 68, 0.12)', color: '#fca5a5',
          padding: '8px 12px', borderRadius: 'var(--radius-sm)',
          marginBottom: 12, fontSize: 13,
          border: '1px solid rgba(239, 68, 68, 0.3)',
        }}>
          {error}
        </div>
      )}

      {/* ====== V3 视图内容（六视图切换） ====== */}
      {activeView === 'dashboard' && (
        <DashboardView projectId={selectedProjectId} tasks={tasks} onOpenTask={openTask} />
      )}

      {activeView === 'coding' && <CodingTerminalPage compact />}

      {activeView === 'list' && (
        <TaskListView
          tasks={tasks}
          onOpenTask={openTask}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
        />
      )}

      {activeView === 'gantt' && (
        <GanttView tasks={tasks} onOpenTask={openTask} />
      )}

      {activeView === 'graph' && (
        <ProjectGraphView projectId={selectedProjectId} onOpenTask={openTask} />
      )}

      {/* 看板视图：V2-W4 搜索结果列表 / 5 列看板（互斥切换） */}
      {activeView === 'board' && (searchResults ? (
        <div style={{
          flex: 1, overflow: 'auto',
          background: 'var(--color-bg-surface)',
          borderRadius: 'var(--radius-md)',
          padding: 16,
          border: '1px solid var(--color-border-subtle)',
        }}>
          <div style={{
            marginBottom: 12, color: 'var(--color-text-secondary)', fontSize: 14,
          }}>
            搜索「{searchQuery}」匹配 {searchResults.length} 条结果
            {searching && '（搜索中...）'}
          </div>
          {searchResults.length === 0 && !searching && (
            <div style={{ color: 'var(--color-text-tertiary)', padding: 24, textAlign: 'center' }}>
              无匹配任务
            </div>
          )}
          {searchResults.map((task) => (
            <div
              key={task.task_id}
              onClick={() => {
                setSelectedTaskId(task.task_id);
                setCurrentPage('detail');
              }}
              style={{
                padding: '8px 12px', marginBottom: 6, cursor: 'pointer',
                background: 'var(--color-bg-elevated)',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--color-border-subtle)',
                borderLeft: `3px solid ${RISK_COLORS[task.risk_level]?.border || '#666'}`,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontWeight: 500, color: 'var(--color-text-primary)' }}>
                  {task.title}
                </span>
                <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>
                  {COLUMN_LABELS[task.status] || task.status}
                </span>
              </div>
              {task.description && (
                <div style={{
                  fontSize: 12, color: 'var(--color-text-tertiary)',
                  marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {task.description}
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 8, flex: 1, overflow: 'hidden' }}>
          {COLUMNS.map((col) => {
            const colTasks = tasks.filter((t) => t.status === col);
            return (
              <div
                key={col}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  const taskId = e.dataTransfer.getData('text/plain');
                  handleDragEnd(taskId, col);
                }}
                style={{
                  flex: 1, minWidth: 200,
                  background: 'var(--color-bg-surface)',
                  borderRadius: 'var(--radius-md)',
                  padding: 8, display: 'flex', flexDirection: 'column',
                  overflow: 'auto',
                  border: '1px solid var(--color-border-subtle)',
              }}
            >
              <h4 style={{
                marginTop: 0, marginBottom: 8, fontSize: 14,
                color: 'var(--color-text-secondary)',
              }}>
                {COLUMN_LABELS[col]} ({colTasks.length})
              </h4>
              {colTasks.map((task) => {
                const allowed = transitionsMap[task.task_id] || [];
                const droppable = allowed.some((t) => t.requires_user);
                const risk = RISK_COLORS[task.risk_level] || RISK_COLORS.medium;
                return (
                  <div
                    key={task.task_id}
                    draggable={droppable}
                    onClick={() => openTask(task.task_id)}
                    onDragStart={(e) => {
                      e.dataTransfer.setData('text/plain', task.task_id);
                      handleDragStart();
                    }}
                    style={{
                      background: 'var(--color-bg-elevated)', padding: 8,
                      borderRadius: 'var(--radius-sm)',
                      marginBottom: 6,
                      border: '1px solid var(--color-border-subtle)',
                      borderLeft: `3px solid ${risk.border}`,
                      cursor: droppable ? 'grab' : 'default',
                      opacity: droppable ? 1 : 0.75,
                      fontSize: 13,
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                      <span style={{ fontWeight: 600, color: 'var(--color-text-primary)' }}>
                        {task.identifier || task.task_id.slice(-6)}
                      </span>
                      <span style={{
                        fontSize: 10, padding: '1px 6px', borderRadius: 'var(--radius-full)',
                        background: risk.badge, color: '#fff', fontWeight: 600,
                      }}>
                        {RISK_LABELS[task.risk_level] || task.risk_level}
                      </span>
                    </div>
                    <div style={{ color: 'var(--color-text-secondary)' }}>{task.title}</div>
                    <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 4, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span>v{task.version}</span>
                      {/* V1：详情按钮 → 跳转 TaskDetailPage（依赖图/终端/工作回顾） */}
                      <button
                        draggable={false}
                        onMouseDown={(e) => e.stopPropagation()}
                        onClick={(e) => {
                          e.stopPropagation();
                          setSelectedTaskId(task.task_id);
                          setCurrentPage('detail');
                        }}
                        style={detailBtnStyle}
                      >
                        详情 →
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
      ))}

      {/* 新建任务 modal（V3.2：从阶段卡片"+"进入时默认创建到该阶段） */}
      {showModal && selectedProjectId && (
        <CreateTaskModal
          projectId={selectedProjectId}
          defaultStatus={createTaskStatus}
          onClose={() => setShowModal(false)}
          onCreated={() => {
            loadTasks();
            // 创建后切到该阶段筛选，方便看到新任务
            setStatusFilter(createTaskStatus);
          }}
        />
      )}

      {/* 新建项目 modal */}
      {showProjectModal && (
        <CreateProjectModal
          onClose={() => setShowProjectModal(false)}
          onCreated={(pid) => loadProjects(pid)}
        />
      )}
    </div>
  );
}

// ---- 内联样式常量（P0 快速验证；V1 抽离至 TaskCenterPage.module.css） ----
// 统一使用深色主题 CSS 变量，与全局 styles.css 协调
const overlayStyle: React.CSSProperties = {
  position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
  background: 'rgba(0,0,0,0.6)', display: 'flex',
  alignItems: 'center', justifyContent: 'center', zIndex: 1000,
};
const modalStyle: React.CSSProperties = {
  background: 'var(--color-bg-surface)', borderRadius: 'var(--radius-md)',
  padding: 24, minWidth: 420, maxWidth: 560,
  border: '1px solid var(--color-border-default)',
  color: 'var(--color-text-primary)',
};
const inputStyle: React.CSSProperties = {
  width: '100%', padding: '8px 12px', margin: '8px 0',
  background: 'var(--color-bg-elevated)',
  border: '1px solid var(--color-border-default)',
  borderRadius: 'var(--radius-sm)', boxSizing: 'border-box',
  fontSize: 14, color: 'var(--color-text-primary)',
};
const selectStyle: React.CSSProperties = {
  padding: '6px 8px',
  height: 38, boxSizing: 'border-box',
  background: 'var(--color-bg-elevated)',
  border: '1px solid var(--color-border-default)',
  borderRadius: 'var(--radius-sm)',
  fontSize: 14, cursor: 'pointer',
  color: 'var(--color-text-primary)',
};
const submitBtnStyle: React.CSSProperties = {
  padding: '8px 16px', background: 'var(--color-primary)', color: '#fff',
  border: 'none', borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 14,
};
// V3.2：阶段卡片栏卡片样式（图标点 + 名称 + 数量 + 尾部"+"）
const stageCardStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 10,
  padding: '8px 12px', minWidth: 96,
  background: 'var(--color-bg-surface)',
  border: '1px solid var(--color-border-subtle)',
  borderRadius: 'var(--radius-md)',
  userSelect: 'none',
};
const cancelBtnStyle: React.CSSProperties = {
  padding: '8px 16px', background: 'var(--color-bg-elevated)',
  color: 'var(--color-text-secondary)',
  border: '1px solid var(--color-border-default)',
  borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 14,
};
// V1：任务卡片「详情」按钮（小巧链接风格，不干扰拖拽）
const detailBtnStyle: React.CSSProperties = {
  padding: '2px 8px', background: 'transparent',
  color: 'var(--color-primary-soft)',
  border: '1px solid var(--color-primary)',
  borderRadius: 'var(--radius-sm)', cursor: 'pointer', fontSize: 11,
  lineHeight: 1.4,
};
