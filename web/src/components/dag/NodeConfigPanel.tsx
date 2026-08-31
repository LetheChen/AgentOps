import { useState, useEffect } from 'react';
import type { EditorNode, HarnessType } from '../../lib/workflowYaml';
import { HARNESS_OPTIONS, NODE_TYPE_OPTIONS } from '../../lib/workflowYaml';
import { apiClient, type RuntimeProviderInfo, type ModelInfo } from '../../lib/api';

/**
 * NodeConfigPanel — 节点配置面板（右侧栏）。
 *
 * 选中节点时显示完整属性表单，支持实时编辑：
 *   - 基础信息：id / name / type
 *   - Agent 配置：agent / harness / model / domain
 *   - 角色提示：business_role / role_prompt
 *   - 输入输出：inputs 列表 / outputs 端口路由
 *   - 高级：skip_if / timeout_seconds
 */

interface NodeConfigPanelProps {
  node: EditorNode;
  allNodeIds: string[];
  onChange: (updated: EditorNode) => void;
  onDelete: (nodeId: string) => void;
}

/** 输出端口路由条目 */
interface OutputEntry {
  port: string;
  target: string;
}

export function NodeConfigPanel({ node, allNodeIds, onChange, onDelete }: NodeConfigPanelProps) {
  // 用本地 state 缓冲文本输入，避免每次按键触发父级 re-render 导致光标跳
  const [localInputs, setLocalInputs] = useState(node.inputs.join(', '));
  const [localRolePrompt, setLocalRolePrompt] = useState(node.role_prompt ?? '');
  const [providers, setProviders] = useState<RuntimeProviderInfo[]>([]);

  useEffect(() => {
    setLocalInputs(node.inputs.join(', '));
    setLocalRolePrompt(node.role_prompt ?? '');
  }, [node.id, node.inputs, node.role_prompt]);

  // 加载运行时供应商/模型列表，供模型下拉选择
  useEffect(() => {
    let cancelled = false;
    apiClient.getRuntimeSummary().then((summary) => {
      if (!cancelled) setProviders(summary.providers ?? []);
    }).catch(() => {
      // 非关键数据，静默失败
    });
    return () => { cancelled = true; };
  }, []);

  const selectedProvider = providers.find(p => p.provider_id === node.model_provider) ?? null;
  const availableModels: ModelInfo[] = selectedProvider?.models ?? [];

  // 输出端口列表
  const outputEntries: OutputEntry[] = Object.entries(node.outputs).map(([port, target]) => ({
    port,
    target: Array.isArray(target) ? target.join(', ') : target,
  }));

  const updateField = <K extends keyof EditorNode>(key: K, value: EditorNode[K]) => {
    onChange({ ...node, [key]: value });
  };

  const handleInputsChange = (val: string) => {
    setLocalInputs(val);
    const inputs = val.split(',').map(s => s.trim()).filter(Boolean);
    updateField('inputs', inputs);
  };

  const handleRolePromptChange = (val: string) => {
    setLocalRolePrompt(val);
    updateField('role_prompt', val || null);
  };

  const handleOutputChange = (index: number, field: 'port' | 'target', value: string) => {
    const entries = [...outputEntries];
    entries[index] = { ...entries[index], [field]: value };
    const outputs: Record<string, string | string[]> = {};
    for (const e of entries) {
      if (!e.port) continue;
      // 多目标（逗号分隔）
      if (e.target.includes(',')) {
        outputs[e.port] = e.target.split(',').map(s => s.trim()).filter(Boolean);
      } else {
        outputs[e.port] = e.target;
      }
    }
    updateField('outputs', outputs);
  };

  const handleAddOutput = () => {
    const portName = `port_${Object.keys(node.outputs).length + 1}`;
    updateField('outputs', { ...node.outputs, [portName]: '' });
  };

  const handleRemoveOutput = (port: string) => {
    const outputs = { ...node.outputs };
    delete outputs[port];
    updateField('outputs', outputs);
  };

  // 可选的依赖节点（排除自身）
  const availableDeps = allNodeIds.filter(id => id !== node.id);
  const toggleDep = (depId: string) => {
    const has = node.after.includes(depId);
    updateField('after', has ? node.after.filter(d => d !== depId) : [...node.after, depId]);
  };

  // 校验：agent 类型节点必须有 agent_id（全局引用）或 role_prompt（内联 agent）二选一
  const isAgentNode = node.type === 'agent';
  const isCommandNode = node.type === 'command' || node.type === 'await_command' || node.type === 'while';
  const missingAgentRef = isAgentNode && !node.agent && !node.role_prompt;
  // command 节点必须通过 rawFields 透传 command_config；前端编辑器不展开它
  const hasCommandConfig = isCommandNode && Boolean((node.rawFields as Record<string, unknown>)?.command_config);

  return (
    <div className="node-config-panel">
      {/* 面板标题 */}
      <div className="ncp-header">
        <span className="ncp-title">节点配置</span>
        <button
          className="ncp-delete-btn"
          onClick={() => onDelete(node.id)}
          title="删除此节点"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
        </button>
      </div>

      <div className="ncp-body">
        {/* ── 基础信息 ── */}
        <section className="ncp-section">
          <h4 className="ncp-section-title">基础信息</h4>
          <div className="ncp-field">
            <label>节点 ID</label>
            <input
              className="input-base ncp-input"
              value={node.id}
              onChange={e => updateField('id', e.target.value)}
              placeholder="node_id"
            />
          </div>
          <div className="ncp-field">
            <label>显示名称</label>
            <input
              className="input-base ncp-input"
              value={node.name}
              onChange={e => updateField('name', e.target.value)}
              placeholder="节点名称"
            />
          </div>
          <div className="ncp-field">
            <label>节点类型</label>
            <select
              className="input-base ncp-input"
              value={node.type}
              onChange={e => updateField('type', e.target.value as EditorNode['type'])}
            >
              {NODE_TYPE_OPTIONS.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        </section>

        {/* ── Command 节点提示（command / await_command / while） ── */}
        {isCommandNode && (
          <section className="ncp-section">
            <h4 className="ncp-section-title">Command 节点</h4>
            {hasCommandConfig ? (
              <div className="ncp-field-info">
                ✓ command_config 已通过 rawFields 透传（{Object.keys((node.rawFields as Record<string, unknown>).command_config as object).length} 个字段）。
                如需修改 cli_template / timeout_seconds / parse_stdout 等，请切到「YAML」模式编辑。
              </div>
            ) : (
              <div className="ncp-field-error">
                ⚠ Command 节点必须有 command_config（cli_template 等）。
                请切到「YAML」模式填入，或从已有的 command 节点复制。
              </div>
            )}
          </section>
        )}

        {/* ── Agent 配置 ── */}
        <section className="ncp-section">
          <h4 className="ncp-section-title">Agent 配置</h4>
          {missingAgentRef && (
            <div className="ncp-field-error">
              Agent 节点必须填写 Agent ID（全局引用）或角色提示词（内联 agent），二者至少填一项。
            </div>
          )}
          <div className="ncp-field">
            <label>Agent ID</label>
            <input
              className={`input-base ncp-input ${missingAgentRef ? 'ncp-input-invalid' : ''}`}
              value={node.agent ?? ''}
              onChange={e => updateField('agent', e.target.value || null)}
              placeholder="如 log_analyst"
            />
          </div>
          <div className="ncp-field">
            <label>Harness 类型</label>
            <select
              className="input-base ncp-input"
              value={node.harness}
              onChange={e => updateField('harness', e.target.value as HarnessType)}
            >
              {HARNESS_OPTIONS.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
          <div className="ncp-field">
            <label>模型</label>
            <div className="ncp-model-selects">
              <select
                className="input-base ncp-input"
                value={node.model_provider}
                onChange={e => onChange({ ...node, model_provider: e.target.value, model_id: '' })}
                title="供应商"
              >
                <option value="">自动 / 不指定</option>
                {providers.map(p => (
                  <option key={p.provider_id} value={p.provider_id}>{p.provider_id}</option>
                ))}
              </select>
              <select
                className="input-base ncp-input ncp-mono"
                value={node.model_id}
                onChange={e => onChange({ ...node, model_id: e.target.value })}
                disabled={!availableModels.length}
                title="模型"
              >
                <option value="">{availableModels.length ? '选择模型' : '请先选择供应商'}</option>
                {availableModels.map(m => (
                  <option key={m.id} value={m.id}>{m.id}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="ncp-field">
            <label>业务域</label>
            <input
              className="input-base ncp-input"
              value={node.domain ?? ''}
              onChange={e => updateField('domain', e.target.value || null)}
              placeholder="可选"
            />
          </div>
        </section>

        {/* ── 角色提示 ── */}
        <section className="ncp-section">
          <h4 className="ncp-section-title">角色提示</h4>
          <div className="ncp-field">
            <label>业务角色</label>
            <input
              className="input-base ncp-input"
              value={node.business_role ?? ''}
              onChange={e => updateField('business_role', e.target.value || null)}
              placeholder="如 数据采集员"
            />
          </div>
          <div className="ncp-field">
            <label>角色提示词 (role_prompt)</label>
            <textarea
              className={`input-base ncp-textarea ${missingAgentRef ? 'ncp-input-invalid' : ''}`}
              value={localRolePrompt}
              onChange={e => handleRolePromptChange(e.target.value)}
              placeholder="你是数据采集员，负责..."
              rows={4}
            />
          </div>
        </section>

        {/* ── 输入输出 ── */}
        <section className="ncp-section">
          <h4 className="ncp-section-title">输入输出</h4>
          <div className="ncp-field">
            <label>输入参数 (逗号分隔)</label>
            <input
              className="input-base ncp-input ncp-mono"
              value={localInputs}
              onChange={e => handleInputsChange(e.target.value)}
              placeholder="topic, log_source_id"
            />
          </div>

          {/* 依赖节点（after） */}
          <div className="ncp-field">
            <label>依赖节点 (after)</label>
            <div className="ncp-chip-group">
              {availableDeps.length === 0 ? (
                <span className="ncp-hint">无其他节点可选</span>
              ) : (
                availableDeps.map(depId => (
                  <button
                    key={depId}
                    className={`ncp-chip ${node.after.includes(depId) ? 'ncp-chip-active' : ''}`}
                    onClick={() => toggleDep(depId)}
                  >
                    {depId}
                  </button>
                ))
              )}
            </div>
          </div>

          {/* 输出端口路由 */}
          <div className="ncp-field">
            <div className="ncp-field-header">
              <label>输出端口路由</label>
              <button className="ncp-add-btn" onClick={handleAddOutput}>+ 添加端口</button>
            </div>
            {outputEntries.length === 0 ? (
              <span className="ncp-hint">无输出端口</span>
            ) : (
              <div className="ncp-outputs">
                {outputEntries.map((entry, idx) => (
                  <div key={idx} className="ncp-output-row">
                    <input
                      className="input-base ncp-output-port"
                      value={entry.port}
                      onChange={e => handleOutputChange(idx, 'port', e.target.value)}
                      placeholder="port_name"
                    />
                    <span className="ncp-output-arrow">→</span>
                    <input
                      className="input-base ncp-output-target"
                      value={entry.target}
                      onChange={e => handleOutputChange(idx, 'target', e.target.value)}
                      placeholder="target_node.in:port"
                    />
                    <button
                      className="ncp-remove-btn"
                      onClick={() => handleRemoveOutput(entry.port)}
                      title="删除端口"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        {/* ── 高级 ── */}
        <section className="ncp-section">
          <h4 className="ncp-section-title">高级配置</h4>
          <div className="ncp-field">
            <label>跳过条件 (skip_if)</label>
            <input
              className="input-base ncp-input ncp-mono"
              value={node.skip_if ?? ''}
              onChange={e => updateField('skip_if', e.target.value || null)}
              placeholder='如 {{not report.critical_summary}}'
            />
          </div>
          <div className="ncp-field">
            <label>超时时间 (秒)</label>
            <input
              className="input-base ncp-input"
              type="number"
              value={node.timeout_seconds ?? ''}
              onChange={e => updateField('timeout_seconds', e.target.value ? parseInt(e.target.value, 10) : null)}
              placeholder="600"
            />
          </div>
        </section>
      </div>
    </div>
  );
}
