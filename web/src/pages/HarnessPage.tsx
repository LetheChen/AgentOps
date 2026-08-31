import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '../lib/api';

interface HarnessPageProps {}

interface HarnessAdapter {
  type: string;
  name: string;
  online: boolean;
  description: string;
  config: Record<string, string>;
}

interface OpenCodeConfig {
  host: string;
  port: string;
  timeout: string;
  waitTimeout: string;
  directory: string;
  agent: string;
  model: string;
}

const DEFAULT_OC_CONFIG: OpenCodeConfig = {
  host: '127.0.0.1',
  port: '4096',
  timeout: '300',
  waitTimeout: '240',
  directory: '',
  agent: '',
  model: '',
};

const otherAdapters: HarnessAdapter[] = [
  {
    type: 'deterministic',
    name: '确定性适配器',
    online: true,
    description: '基于预定义脚本执行任务，无外部依赖，适用于测试和确定性流程',
    config: { mode: 'script', timeout: '30', retry: '2' },
  },
  {
    type: 'claude_code',
    name: 'Claude Code 适配器',
    online: true,
    description: '通过 Claude Code CLI 运行 Anthropic Claude 智能体，支持复杂推理和代码任务',
    config: { binary: 'claude', model: 'claude-sonnet-4', max_tokens: '8192' },
  },
  {
    type: 'kimi',
    name: 'Kimi 适配器',
    online: false,
    description: '通过 Moonshot API 运行 Kimi 智能体，适用于中文场景和长文本处理',
    config: { endpoint: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-128k', timeout: '60' },
  },
  {
    type: 'http',
    name: 'HTTP 适配器',
    online: true,
    description: '通过 HTTP API 调用外部智能体服务，支持自定义请求头和响应解析',
    config: { endpoint: 'http://localhost:8080/api/agent', method: 'POST', timeout: '60' },
  },
];

type TestState = 'idle' | 'testing' | 'success' | 'failed';

export function HarnessPage({}: HarnessPageProps) {
  const [ocConfig, setOcConfig] = useState<OpenCodeConfig>(DEFAULT_OC_CONFIG);
  const [testState, setTestState] = useState<TestState>('idle');
  const [testMessage, setTestMessage] = useState('');
  const [availableHarnesses, setAvailableHarnesses] = useState<string[]>([]);
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Fetch available harnesses from backend
  useEffect(() => {
    apiClient.getHarnesses().then((data) => {
      setAvailableHarnesses(data.harnesses || []);
    }).catch(() => {
      // Backend may not be running
    });
  }, []);

  const baseUrl = `http://${ocConfig.host}:${ocConfig.port}`;

  const handleTest = useCallback(async () => {
    setTestState('testing');
    setTestMessage('');
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);

      const response = await fetch(baseUrl, {
        method: 'GET',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (response.ok) {
        const data = await response.json().catch(() => ({}));
        setTestState('success');
        setTestMessage(`连接成功 — 服务状态: ${data.status || 'ok'}${data.version ? `, 版本: ${data.version}` : ''}`);
      } else {
        setTestState('failed');
        setTestMessage(`连接失败 — HTTP ${response.status} ${response.statusText}`);
      }
    } catch (err) {
      setTestState('failed');
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes('abort') || msg.includes('timeout')) {
        setTestMessage(`连接超时 — 请确认 opencode 服务已启动: opencode serve --hostname=${ocConfig.host} --port=${ocConfig.port}`);
      } else if (msg.includes('fetch') || msg.includes('ECONNREFUSED')) {
        setTestMessage(`无法连接到 ${baseUrl} — 请确认 opencode 服务已启动`);
      } else {
        setTestMessage(`连接错误: ${msg}`);
      }
    }
  }, [baseUrl, ocConfig.host, ocConfig.port]);

  const handleSave = useCallback(() => {
    // Persist to localStorage (the backend reads env vars; this is for UI persistence)
    localStorage.setItem('opencode_harness_config', JSON.stringify(ocConfig));
    setTestMessage('配置已保存到本地。后端通过 OPENCODE_HOST 和 OPENCODE_PORT 环境变量读取连接参数。');
    setTestState('success');
  }, [ocConfig]);

  // Load saved config on mount
  useEffect(() => {
    const saved = localStorage.getItem('opencode_harness_config');
    if (saved) {
      try {
        setOcConfig({ ...DEFAULT_OC_CONFIG, ...JSON.parse(saved) });
      } catch { /* ignore */ }
    }
  }, []);

  const isOcAvailable = availableHarnesses.includes('opencode');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* Page Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: '20px', fontWeight: 600, color: 'var(--color-text-primary)' }}>适配器管理</div>
          <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
            管理 AI 智能体运行时适配器，配置连接参数和执行策略
          </div>
        </div>
        <button className="btn-primary">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
          添加适配器
        </button>
      </div>

      {/* OpenCode Configuration Panel */}
      <div className="card" style={{ overflow: 'hidden' }}>
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', padding: '20px 20px 0' }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--color-text-primary)' }}>OpenCode 适配器</span>
              <span className={`status-pill ${testState === 'success' || isOcAvailable ? 'status-pill-success' : testState === 'testing' ? 'status-pill-info' : testState === 'failed' ? 'status-pill-error' : 'status-pill-neutral'}`}>
                {testState === 'testing' ? '检测中' : testState === 'success' ? '在线' : testState === 'failed' ? '离线' : '未检测'}
              </span>
            </div>
            <div className="font-mono" style={{ fontSize: '11px', color: 'var(--color-text-tertiary)', marginTop: '4px' }}>
              type: opencode · {baseUrl}
            </div>
          </div>
          <div className="harness-status">
            <div className={`status-dot status-dot-${testState === 'success' || isOcAvailable ? 'success' : testState === 'failed' ? 'error' : 'neutral'}`} />
            <span>{testState === 'success' || isOcAvailable ? '可用' : testState === 'failed' ? '不可用' : '未检测'}</span>
          </div>
        </div>

        {/* Description */}
        <div style={{ padding: '12px 20px 0', fontSize: '13px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
          通过 OpenCode headless server (SDK v2 HTTP API) 运行智能体。opencode 内置 agent loop 处理工具调用 (Bash/Read/Edit 等)，
          harness 采用 fire-and-collect 模式：创建 session → 发送 prompt → 等待 idle → 获取消息。
        </div>

        {/* Connection Configuration */}
        <div style={{ padding: '16px 20px' }}>
          <div className="harness-config-label" style={{ marginBottom: '12px' }}>连接配置</div>

          {/* Host & Port Row */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '12px' }}>
            <div>
              <label style={{ display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>
                主机地址 (OPENCODE_HOST)
              </label>
              <input
                className="input-base"
                style={{ width: '100%' }}
                value={ocConfig.host}
                onChange={(e) => { setOcConfig({ ...ocConfig, host: e.target.value }); setTestState('idle'); }}
                placeholder="127.0.0.1"
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>
                端口 (OPENCODE_PORT)
              </label>
              <input
                className="input-base"
                style={{ width: '100%' }}
                value={ocConfig.port}
                onChange={(e) => { setOcConfig({ ...ocConfig, port: e.target.value }); setTestState('idle'); }}
                placeholder="4096"
              />
            </div>
          </div>

          {/* Advanced Settings Toggle */}
          <button
            onClick={() => setShowAdvanced(!showAdvanced)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--color-primary-soft)', fontSize: '13px',
              display: 'flex', alignItems: 'center', gap: '4px',
              padding: '4px 0', marginBottom: showAdvanced ? '12px' : '0',
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              style={{ transform: showAdvanced ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }}>
              <polyline points="9 18 15 12 9 6" />
            </svg>
            高级设置
          </button>

          {/* Advanced Settings */}
          {showAdvanced && (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '12px' }}>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>
                  请求超时 (秒)
                </label>
                <input
                  className="input-base font-mono"
                  style={{ width: '100%' }}
                  value={ocConfig.timeout}
                  onChange={(e) => setOcConfig({ ...ocConfig, timeout: e.target.value })}
                  placeholder="300"
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>
                  等待超时 (秒)
                </label>
                <input
                  className="input-base font-mono"
                  style={{ width: '100%' }}
                  value={ocConfig.waitTimeout}
                  onChange={(e) => setOcConfig({ ...ocConfig, waitTimeout: e.target.value })}
                  placeholder="240"
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>
                  工作目录 (x-opencode-directory)
                </label>
                <input
                  className="input-base font-mono"
                  style={{ width: '100%' }}
                  value={ocConfig.directory}
                  onChange={(e) => setOcConfig({ ...ocConfig, directory: e.target.value })}
                  placeholder="/path/to/workspace"
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>
                  默认 Agent 名称
                </label>
                <input
                  className="input-base"
                  style={{ width: '100%' }}
                  value={ocConfig.agent}
                  onChange={(e) => setOcConfig({ ...ocConfig, agent: e.target.value })}
                  placeholder="(可选)"
                />
              </div>
              <div style={{ gridColumn: 'span 2' }}>
                <label style={{ display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' }}>
                  默认模型 (provider/model 格式)
                </label>
                <input
                  className="input-base font-mono"
                  style={{ width: '100%' }}
                  value={ocConfig.model}
                  onChange={(e) => setOcConfig({ ...ocConfig, model: e.target.value })}
                  placeholder="anthropic/claude-sonnet-4"
                />
              </div>
            </div>
          )}

          {/* Test Result Message */}
          {testMessage && (
            <div style={{
              padding: '10px 12px',
              marginBottom: '12px',
              borderRadius: 'var(--radius-md)',
              fontSize: '13px',
              background: testState === 'success' ? 'var(--state-success-tint)' : testState === 'failed' ? 'var(--state-error-tint)' : 'var(--state-info-tint)',
              color: testState === 'success' ? 'var(--state-success)' : testState === 'failed' ? 'var(--state-error)' : 'var(--color-primary-soft)',
              display: 'flex',
              alignItems: 'flex-start',
              gap: '8px',
            }}>
              {testState === 'success' ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>
              ) : testState === 'failed' ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
              ) : (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}><circle cx="12" cy="12" r="10" /><path d="M12 16v-4" /><path d="M12 8h.01" /></svg>
              )}
              <span>{testMessage}</span>
            </div>
          )}

          {/* Action Buttons */}
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              className="btn-secondary btn-sm"
              onClick={handleTest}
              disabled={testState === 'testing'}
            >
              {testState === 'testing' ? (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ animation: 'spin 1s linear infinite' }}><path d="M21 12a9 9 0 1 1-6.219-8.56" /></svg>
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
              )}
              {testState === 'testing' ? '检测中...' : '测试连接'}
            </button>
            <button className="btn-primary btn-sm" onClick={handleSave}>
              保存配置
            </button>
          </div>
        </div>

        {/* SDK API Reference */}
        <div style={{ padding: '0 20px 20px' }}>
          <div className="harness-config-label" style={{ marginBottom: '8px' }}>SDK v2 API 端点</div>
          <div className="harness-config-block" style={{ fontSize: '11px' }}>
            <div><span className="harness-config-key">POST</span> <span className="harness-config-val">{baseUrl}/api/session</span> <span style={{ color: 'var(--color-text-tertiary)' }}>— 创建会话</span></div>
            <div><span className="harness-config-key">POST</span> <span className="harness-config-val">{baseUrl}/api/session/{'{id}'}/prompt</span> <span style={{ color: 'var(--color-text-tertiary)' }}>— 发送 prompt</span></div>
            <div><span className="harness-config-key">POST</span> <span className="harness-config-val">{baseUrl}/api/session/{'{id}'}/wait</span> <span style={{ color: 'var(--color-text-tertiary)' }}>— 等待 idle</span></div>
            <div><span className="harness-config-key">GET </span> <span className="harness-config-val">{baseUrl}/api/session/{'{id}'}/message</span> <span style={{ color: 'var(--color-text-tertiary)' }}>— 获取消息</span></div>
          </div>
        </div>
      </div>

      {/* Other Adapters Grid */}
      <div className="harness-config-label" style={{ marginTop: '4px' }}>其他适配器</div>
      <div className="harness-grid">
        {otherAdapters.map((adapter) => (
          <div key={adapter.type} className="harness-card">
            <div className="harness-card-header">
              <div>
                <div className="harness-card-name">{adapter.name}</div>
                <div className="harness-type-badge">{adapter.type}</div>
              </div>
              <div className="harness-status">
                <div className={`status-dot status-dot-${adapter.online ? 'success' : 'warning'}`} />
                <span>{adapter.online ? '在线' : '离线'}</span>
              </div>
            </div>
            <div className="harness-desc">{adapter.description}</div>
            <div>
              <div className="harness-config-label" style={{ marginBottom: '6px' }}>配置参数</div>
              <div className="harness-config-block">
                {Object.entries(adapter.config).map(([key, val]) => (
                  <div key={key}>
                    <span className="harness-config-key">{key}</span>: <span className="harness-config-val">"{val}"</span>
                  </div>
                ))}
              </div>
            </div>
            <div className="harness-card-footer">
              <button className="btn-secondary btn-sm" disabled={!adapter.online}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                测试连接
              </button>
              <button className="btn-secondary btn-sm">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
                编辑
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Inline keyframe for spinner */}
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
