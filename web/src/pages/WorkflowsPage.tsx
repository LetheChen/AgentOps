import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '../lib/api';
import { DagEditorModal } from '../components/dag/DagEditorModal';

interface WorkflowsPageProps {
  onRunWorkflow: (workflowId: string, inputs: Record<string, unknown>) => void;
}

interface Workflow {
  id: string;
  name: string;
  description: string;
  version: number;
  nodeCount: number;
  nodeIds: string[];
  edges: { source: string; target: string }[];
  widgets: number;
  status: 'verified' | 'draft';
}

interface DagNode { x: number; y: number }
interface DagEdge { fromIdx: number; toIdx: number }

const DEFAULT_TEMPLATE = `workflow_id: new-workflow
name: 新建工作流
version: 1.0
description: 描述这个工作流的用途

inputs:
  - name: topic
    type: string
    required: true
    default: ""

nodes:
  fetch:
    name: 数据采集
    type: agent
    agent: echo_agent
    harness: deterministic
    after: []
    inputs: [topic]
    outputs:
      fetched:
        to: "process.in:fetched"

  process:
    name: 数据处理
    type: agent
    agent: echo_agent
    harness: deterministic
    after: [fetch]
    outputs:
      processed:
        to: "output.in:processed"

  output:
    name: 结果输出
    type: agent
    agent: echo_agent
    harness: deterministic
    after: [process]
    outputs:
      done:
        to: ""

widgets:
  - id: w_progress
    type: progress_status
    title: 执行进度
    emit_on:
      node: output
      event: node.completed
    props: {}
`;

const defaultWorkflows: Workflow[] = [
  {
    id: 'travel-expense',
    name: '差旅报销审批',
    description: '自动审核差旅费用报销申请，包括票据识别、费用合规检查、审批流转和报告生成',
    version: 1.0,
    nodeCount: 7,
    nodeIds: ['step1', 'step2', 'step3a', 'step3b', 'step3c', 'step4', 'step5', 'step6', 'step7', 'step8', 'step9'],
    edges: [
      { source: 'step1', target: 'step2' },
      { source: 'step2', target: 'step3a' },
      { source: 'step2', target: 'step3b' },
      { source: 'step2', target: 'step3c' },
      { source: 'step3a', target: 'step4' },
      { source: 'step3b', target: 'step4' },
      { source: 'step3c', target: 'step4' },
      { source: 'step4', target: 'step5' },
      { source: 'step5', target: 'step6' },
      { source: 'step6', target: 'step7' },
      { source: 'step7', target: 'step8' },
      { source: 'step8', target: 'step9' },
    ],
    widgets: 2,
    status: 'verified',
  },
  {
    id: 'hello-world',
    name: 'Hello World 示例',
    description: '最小化工作流示例，用于测试平台基础功能和 Harness 适配器连通性',
    version: 1.0,
    nodeCount: 3,
    nodeIds: ['fetch', 'think', 'report'],
    edges: [
      { source: 'fetch', target: 'think' },
      { source: 'think', target: 'report' },
    ],
    widgets: 5,
    status: 'verified',
  },
];

/** Auto-layout DAG nodes by topological level for SVG preview */
function autoLayoutDAG(nodeIds: string[], edges: { source: string; target: string }[]): { nodes: DagNode[]; edges: DagEdge[] } {
  const adj: Record<string, string[]> = {};
  const inDegree: Record<string, number> = {};
  for (const id of nodeIds) { adj[id] = []; inDegree[id] = 0; }
  for (const e of edges) {
    if (adj[e.source] !== undefined && inDegree[e.target] !== undefined) {
      adj[e.source].push(e.target);
      inDegree[e.target]++;
    }
  }

  const levels: string[][] = [];
  const visited = new Set<string>();
  let current = nodeIds.filter(id => inDegree[id] === 0);
  while (current.length > 0) {
    levels.push(current);
    for (const id of current) visited.add(id);
    const next: string[] = [];
    for (const id of current) {
      for (const target of adj[id]) {
        inDegree[target]--;
        if (inDegree[target] === 0 && !visited.has(target)) next.push(target);
      }
    }
    current = next;
  }
  for (const id of nodeIds) if (!visited.has(id)) levels.push([id]);

  const totalLevels = levels.length;
  const positions: Record<string, DagNode> = {};
  levels.forEach((level, levelIdx) => {
    const x = totalLevels <= 1 ? 50 : 10 + (levelIdx / (totalLevels - 1)) * 80;
    level.forEach((id, idx) => {
      const y = level.length <= 1 ? 26 : 12 + (idx / (level.length - 1)) * 28;
      positions[id] = { x, y };
    });
  });

  const nodes = nodeIds.map(id => positions[id] ?? { x: 50, y: 26 });
  const edgeIndices = edges
    .map(e => ({ fromIdx: nodeIds.indexOf(e.source), toIdx: nodeIds.indexOf(e.target) }))
    .filter(e => e.fromIdx >= 0 && e.toIdx >= 0);

  return { nodes, edges: edgeIndices };
}

export function WorkflowsPage({ onRunWorkflow }: WorkflowsPageProps) {
  const [workflows, setWorkflows] = useState<Workflow[]>(defaultWorkflows);
  const [searchQuery, setSearchQuery] = useState('');
  const [filter, setFilter] = useState<'all' | 'verified' | 'draft'>('all');
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [yamlContent, setYamlContent] = useState(DEFAULT_TEMPLATE);
  const [saving, setSaving] = useState(false);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  // 运行 modal：点运行后弹出 inputs 表单
  const [runModalWf, setRunModalWf] = useState<Workflow | null>(null);
  const [runInputs, setRunInputs] = useState<Record<string, string>>({});
  const [runWfInputsDef, setRunWfInputsDef] = useState<Array<{ name: string; type: string; required: boolean; default?: unknown; description?: string }>>([]);
  const [runFormError, setRunFormError] = useState<string | null>(null);

  const handleOpenRunModal = useCallback(async (wf: Workflow) => {
    setRunModalWf(wf);
    setRunInputs({});
    setRunWfInputsDef([]);
    setRunFormError(null);
    // 从后端获取 workflow inputs 定义
    try {
      const detail = await apiClient.getWorkflowDetail(wf.id);
      if (detail.inputs && Array.isArray(detail.inputs)) {
        const inputsDef = detail.inputs.map((inp: Record<string, unknown>) => ({
          name: String(inp.name || ''),
          type: String(inp.type || 'string'),
          required: Boolean(inp.required),
          default: inp.default,
          description: String(inp.description || ''),
        }));
        setRunWfInputsDef(inputsDef);
        // 预填默认值
        const defaults: Record<string, string> = {};
        for (const inp of inputsDef) {
          if (inp.default !== undefined && inp.default !== null) {
            defaults[inp.name] = String(inp.default);
          }
        }
        setRunInputs(defaults);
      }
    } catch {
      // 后端不可用时无 inputs 定义，直接运行
    }
  }, []);

  const handleConfirmRun = useCallback(() => {
    if (!runModalWf) return;
    const inputs: Record<string, unknown> = {};
    for (const inp of runWfInputsDef) {
      const val = runInputs[inp.name];
      if (val !== undefined && val !== '') {
        // 按类型转换
        if (inp.type === 'integer') {
          inputs[inp.name] = parseInt(val, 10);
        } else if (inp.type === 'boolean') {
          inputs[inp.name] = val === 'true' || val === '1';
        } else {
          inputs[inp.name] = val;
        }
      } else if (inp.default !== undefined) {
        inputs[inp.name] = inp.default;
      } else if (inp.required) {
        setRunFormError(`参数「${inp.name}」为必填项，请填写后再运行`);
        return;
      }
    }
    setRunFormError(null);
    onRunWorkflow(runModalWf.id, inputs);
    setRunModalWf(null);
  }, [runModalWf, runInputs, runWfInputsDef, onRunWorkflow]);

  // Fetch workflows from backend
  const loadWorkflows = useCallback(() => {
    apiClient.getWorkflows().then((data) => {
      if (data.workflows && data.workflows.length > 0) {
        const mapped: Workflow[] = data.workflows.map((w: Record<string, unknown>) => ({
          id: String(w.workflow_id || ''),
          name: String(w.name || w.workflow_id || ''),
          description: String(w.description || ''),
          version: Number(w.version || 1.0),
          nodeCount: Number(w.nodes || 0),
          nodeIds: (w.node_ids as string[]) || [],
          edges: (w.edges as { source: string; target: string }[]) || [],
          widgets: Number(w.widgets || 0),
          status: 'verified' as const,
        }));
        setWorkflows(mapped);
      }
    }).catch(() => {
      // Backend not available, use default data
    });
  }, []);

  useEffect(() => { loadWorkflows(); }, [loadWorkflows]);

  const filtered = workflows.filter((w) => {
    if (filter === 'verified' && w.status !== 'verified') return false;
    if (filter === 'draft' && w.status !== 'draft') return false;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      return w.name.toLowerCase().includes(q) || w.id.toLowerCase().includes(q) || w.description.toLowerCase().includes(q);
    }
    return true;
  });

  const chips: { id: typeof filter; label: string }[] = [
    { id: 'all', label: '全部' },
    { id: 'verified', label: '已验证' },
    { id: 'draft', label: '草稿' },
  ];

  const handleOpenCreate = useCallback(() => {
    setEditingId(null);
    setYamlContent(DEFAULT_TEMPLATE);
    setModalOpen(true);
  }, []);

  const handleOpenEdit = useCallback(async (wf: Workflow) => {
    setEditingId(wf.id);
    setModalOpen(true);
    setLoadingDetail(true);
    try {
      const detail = await apiClient.getWorkflowDetail(wf.id);
      const source = String(detail.yaml_source || '');
      setYamlContent(source || DEFAULT_TEMPLATE);
    } catch {
      setYamlContent(DEFAULT_TEMPLATE);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  const handleSave = useCallback(async (yaml: string) => {
    if (!yaml.trim()) return;
    setSaving(true);
    try {
      if (editingId) {
        await apiClient.updateWorkflow(editingId, yaml);
      } else {
        await apiClient.createWorkflow(yaml);
      }
      setModalOpen(false);
      loadWorkflows();
    } catch (err) {
      throw err;
    } finally {
      setSaving(false);
    }
  }, [editingId, loadWorkflows]);

  const handleDelete = useCallback(async (id: string) => {
    try {
      await apiClient.deleteWorkflow(id);
      setWorkflows(prev => prev.filter(w => w.id !== id));
    } catch {
      // If backend fails, still remove from local state
      setWorkflows(prev => prev.filter(w => w.id !== id));
    }
    setDeleteConfirmId(null);
  }, []);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* Toolbar */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <input
            className="input-base"
            style={{ width: '280px' }}
            placeholder="搜索工作流..."
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
          />
          {chips.map(chip => (
            <button
              key={chip.id}
              className={`filter-chip ${filter === chip.id ? 'active' : ''}`}
              onClick={() => setFilter(chip.id)}
            >
              {chip.label}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button className="btn-secondary" onClick={loadWorkflows} title="刷新列表">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" /></svg>
            刷新
          </button>
          <button className="btn-primary" onClick={handleOpenCreate}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
            创建工作流
          </button>
        </div>
      </div>

      {/* Workflow Cards */}
      {filtered.length === 0 ? (
        <div className="card" style={{ padding: '48px', textAlign: 'center', color: 'var(--color-text-tertiary)' }}>
          {searchQuery ? '未找到匹配的工作流' : '暂无工作流，点击「创建工作流」添加'}
        </div>
      ) : (
        <div className="workflow-grid">
          {filtered.map(wf => {
            const layout = autoLayoutDAG(wf.nodeIds, wf.edges);
            return (
              <div key={wf.id} className="workflow-card">
                <div className="workflow-card-header">
                  <div>
                    <div className="workflow-card-name">{wf.name}</div>
                    <div className="workflow-card-version">v{wf.version}</div>
                  </div>
                  <span className={`status-pill ${wf.status === 'verified' ? 'status-pill-success' : 'status-pill-warning'}`}>
                    {wf.status === 'verified' ? '已验证' : '草稿'}
                  </span>
                </div>
                <div className="workflow-card-desc">{wf.description}</div>
                {/* Mini DAG Preview */}
                <div className="mini-dag">
                  <svg width="100%" height="100%" viewBox="0 0 100 52" preserveAspectRatio="xMidYMid meet">
                    {/* 连接线 */}
                    {layout.edges.map((edge, i) => {
                      const from = layout.nodes[edge.fromIdx];
                      const to = layout.nodes[edge.toIdx];
                      if (!from || !to) return null;
                      const dy = to.y - from.y;
                      const isCurved = Math.abs(dy) > 5;
                      if (isCurved) {
                        const mx = (from.x + to.x) / 2;
                        const path = `M ${from.x} ${from.y} Q ${mx} ${(from.y + to.y) / 2} ${to.x} ${to.y}`;
                        return <path key={i} d={path} fill="none" stroke="var(--color-primary)" strokeWidth="0.4" strokeOpacity="0.35" />;
                      }
                      return <line key={i} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke="var(--color-primary)" strokeWidth="0.4" strokeOpacity="0.35" />;
                    })}
                    {/* 节点 */}
                    {layout.nodes.map((node, i) => {
                      // 根据连接数判断节点类型：入口(绿) / 出口(蓝) / 中间(灰)
                      const isInlet = layout.edges.every(e => layout.nodes[e.toIdx] !== node);
                      const isOutlet = layout.edges.every(e => layout.nodes[e.fromIdx] !== node);
                      const fill = isInlet ? '#10B981' : isOutlet ? '#3B82F6' : '#64748B';
                      return (
                        <g key={i}>
                          <circle cx={node.x} cy={node.y} r="2.2" fill={fill} fillOpacity="0.2" />
                          <circle cx={node.x} cy={node.y} r="1.3" fill={fill} />
                        </g>
                      );
                    })}
                  </svg>
                </div>
                <div className="workflow-card-footer">
                  <span className="workflow-card-nodes">{wf.nodeCount} 节点 · {wf.widgets} Widget</span>
                  <div style={{ display: 'flex', gap: '4px' }}>
                    <button
                      onClick={() => handleOpenEdit(wf)}
                      style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-secondary)', padding: '4px', borderRadius: 'var(--radius-sm)', display: 'flex', alignItems: 'center' }}
                      title="编辑"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
                    </button>
                    <button
                      onClick={() => setDeleteConfirmId(wf.id)}
                      style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-secondary)', padding: '4px', borderRadius: 'var(--radius-sm)', display: 'flex', alignItems: 'center' }}
                      title="删除"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
                    </button>
                    <button className="btn-primary btn-sm" onClick={() => handleOpenRunModal(wf)}>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                      运行
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div style={{ fontSize: '13px', color: 'var(--color-text-tertiary)' }}>共 {filtered.length} 个工作流</div>

      {/* 可视化 DAG 编辑器 Modal */}
      {modalOpen && (
        <DagEditorModal
          editingId={editingId}
          initialYaml={yamlContent}
          onSave={handleSave}
          onClose={() => setModalOpen(false)}
          saving={saving}
        />
      )}

      {/* Delete Confirmation Modal */}
      {deleteConfirmId && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(0, 0, 0, 0.6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
          onClick={e => { if (e.target === e.currentTarget) setDeleteConfirmId(null); }}
        >
          <div
            className="card-elevated"
            style={{ width: '360px', boxShadow: 'var(--shadow-floating)' }}
            onClick={e => e.stopPropagation()}
          >
            <div style={{ padding: '20px' }}>
              <div style={{ fontSize: '15px', fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: '8px' }}>
                删除工作流
              </div>
              <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>
                确定要删除工作流「{workflows.find(w => w.id === deleteConfirmId)?.name || deleteConfirmId}」吗？对应的 YAML 文件也将被删除，此操作不可撤销。
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '0 20px 20px' }}>
              <button className="btn-secondary btn-sm" onClick={() => setDeleteConfirmId(null)}>取消</button>
              <button
                className="btn-sm"
                style={{ background: 'var(--state-error)', color: '#fff', border: 'none', borderRadius: 'var(--radius-md)', fontWeight: 600, fontSize: '13px', padding: '0 16px', height: '32px', cursor: 'pointer' }}
                onClick={() => handleDelete(deleteConfirmId)}
              >
                删除
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 运行 inputs 表单 Modal */}
      {runModalWf && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(0, 0, 0, 0.6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
          onClick={e => { if (e.target === e.currentTarget) setRunModalWf(null); }}
        >
          <div
            className="card-elevated"
            style={{ width: '520px', maxHeight: '90vh', overflowY: 'auto', boxShadow: 'var(--shadow-floating)' }}
            onClick={e => e.stopPropagation()}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 20px', borderBottom: '1px solid var(--color-border-subtle)' }}>
              <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--color-text-primary)' }}>
                运行「{runModalWf.name}」
              </span>
              <button
                onClick={() => setRunModalWf(null)}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-tertiary)', padding: '4px', display: 'flex' }}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
              </button>
            </div>
            <div style={{ padding: '20px' }}>
              {runWfInputsDef.length > 0 ? (
                <>
                  <p style={{ fontSize: '13px', color: 'var(--color-text-tertiary)', marginBottom: '16px' }}>
                    请填写工作流输入参数：
                  </p>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                    {runWfInputsDef.map((inp) => (
                      <div key={inp.name}>
                        <label style={{ fontSize: '13px', fontWeight: 500, color: 'var(--color-text-primary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                          {inp.name}
                          <span style={{ fontSize: '11px', color: 'var(--color-text-tertiary)' }}>({inp.type})</span>
                          {inp.required && <span style={{ color: 'var(--state-error)', fontSize: '11px' }}>*必填</span>}
                        </label>
                        {inp.description && (
                          <p style={{ fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>{inp.description}</p>
                        )}
                        <input
                          className="input-base"
                          style={{ width: '100%', height: '36px', marginTop: '4px' }}
                          placeholder={`输入 ${inp.name}...`}
                          value={runInputs[inp.name] || ''}
                          onChange={(e) => setRunInputs(prev => ({ ...prev, [inp.name]: e.target.value }))}
                        />
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p style={{ fontSize: '14px', color: 'var(--color-text-tertiary)', textAlign: 'center', padding: '24px 0' }}>
                  此工作流无需输入参数，点击「开始运行」即可。
                </p>
              )}
              {runFormError && (
                <p style={{ fontSize: '13px', color: 'var(--state-error)', marginTop: '12px' }}>
                  {runFormError}
                </p>
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '12px 20px', borderTop: '1px solid var(--color-border-subtle)' }}>
              <button className="btn-secondary" onClick={() => setRunModalWf(null)}>取消</button>
              <button className="btn-primary" onClick={handleConfirmRun}>
                开始运行
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
