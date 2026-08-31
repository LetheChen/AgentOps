import { useState } from 'react';
import { ServerConnections } from '../components/ServerConnections';
import { LogPullSettings } from '../components/LogPullSettings';
import { DbConnectionsSettings } from '../components/DbConnectionsSettings';

/**
 * 凭据管理页 — 主机类资源的连接与凭据（DESIGN_config_credential_refactor_v1 §5）：
 *  Tab1 服务器连接：SSH 连接对象 + 绑定凭据（ssh:<connection_id>，Fernet 加密）
 *  Tab2 数据库连接：MySQL 等 DB 连接对象 + 绑定凭据（mysql:<connection_id>，Fernet 加密）
 *  Tab3 日志拉取任务：通过 connection_id 引用连接对象
 * Provider 管理已移至「模型供应商」页（运行时配置域）。
 */
export function ProviderSettings() {
  const [activeTab, setActiveTab] = useState<'connections' | 'db-connections' | 'log-pull'>('connections');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* 页面标题 */}
      <div>
        <div style={{ fontSize: '20px', fontWeight: 600, color: 'var(--color-text-primary)' }}>凭据管理</div>
        <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
          管理服务器连接、数据库连接与日志拉取任务；密码/口令加密存储，敏感配置写入用户目录（不入 Git）
        </div>
      </div>

      {/* Tab 切换 */}
      <div style={{ display: 'flex', gap: '8px' }}>
        <button
          className={`filter-chip ${activeTab === 'connections' ? 'active' : ''}`}
          onClick={() => setActiveTab('connections')}
        >
          服务器连接
        </button>
        <button
          className={`filter-chip ${activeTab === 'db-connections' ? 'active' : ''}`}
          onClick={() => setActiveTab('db-connections')}
        >
          数据库连接
        </button>
        <button
          className={`filter-chip ${activeTab === 'log-pull' ? 'active' : ''}`}
          onClick={() => setActiveTab('log-pull')}
        >
          日志拉取任务
        </button>
      </div>

      {/* Tab 内容（卸载重挂载，切 Tab 即刷新数据） */}
      {activeTab === 'connections' ? (
        <ServerConnections />
      ) : activeTab === 'db-connections' ? (
        <DbConnectionsSettings />
      ) : (
        <LogPullSettings />
      )}
    </div>
  );
}

export default ProviderSettings;
