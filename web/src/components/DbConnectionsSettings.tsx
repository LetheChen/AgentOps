import { useState, useEffect, useCallback } from 'react';
import { apiClient, type ConnectionInfo } from '../lib/api';

/**
 * 数据库连接管理（凭据管理页 Tab2）。
 * 连接对象 = "怎么连上一个数据库"（host/port/用户名/数据库），存 ~/.agentops/private/log-pull.yaml；
 * 凭据（密码）与连接绑定，id 形如 mysql:<connection_id>，经 /api/db-credentials 写入
 * credential_store（Fernet 加密）。与 ServerConnections 同构，仅字段按 MySQL 语义。
 */

// 连接编辑表单（空 id = 新增）
interface DbForm {
  id: string;
  name: string;
  host: string;
  port: string;
  username: string;
  database: string;
  enabled: boolean;
  // 凭据录入（可选：编辑已有连接时可留空 = 不改凭据）
  secret: string;
}

const EMPTY_FORM: DbForm = {
  id: '', name: '', host: '', port: '3306', username: '', database: '',
  enabled: true, secret: '',
};

interface TestState {
  state: 'testing' | 'success' | 'failed';
  message: string;
  latency?: number;
}

export function DbConnectionsSettings() {
  const [connections, setConnections] = useState<ConnectionInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [modal, setModal] = useState<'new' | string | null>(null);
  const [form, setForm] = useState<DbForm>(EMPTY_FORM);
  const [modalError, setModalError] = useState('');
  const [saving, setSaving] = useState(false);

  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  const [testStates, setTestStates] = useState<Record<string, TestState>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await apiClient.listConnections();
      // 本 Tab 只显示数据库（mysql）连接
      setConnections((data.connections || []).filter((c) => c.conn_type === 'mysql'));
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(`加载数据库连接失败：${msg}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const openModal = useCallback((conn?: ConnectionInfo) => {
    if (conn) {
      setForm({
        id: conn.id, name: conn.name, host: conn.host, port: String(conn.port),
        username: conn.username, database: conn.database || '', enabled: conn.enabled, secret: '',
      });
      setModal(conn.id);
    } else {
      setForm(EMPTY_FORM);
      setModal('new');
    }
    setModalError('');
  }, []);

  const handleSave = useCallback(async () => {
    const f = form;
    const isNew = modal === 'new';
    if (isNew && !f.id.trim()) { setModalError('请输入连接 ID'); return; }
    if (!f.host.trim()) { setModalError('请输入数据库地址'); return; }
    if (!f.username.trim()) { setModalError('请输入用户名'); return; }
    const port = parseInt(f.port, 10);
    if (!port || port < 1 || port > 65535) { setModalError('端口必须在 1-65535'); return; }

    setSaving(true);
    setModalError('');
    try {
      await apiClient.upsertConnection({
        id: f.id.trim(), name: f.name.trim() || f.id.trim(), conn_type: 'mysql',
        host: f.host.trim(), port,
        username: f.username.trim(), database: f.database.trim() || null,
        auth_type: 'password',
        credential_id: null, // 后端归一化为 mysql:<id>
        enabled: f.enabled,
      });
      // 凭据录入（非空才写：编辑时留空 = 不改）
      if (f.secret.trim()) {
        await apiClient.setDbCredential(`mysql:${f.id.trim()}`, f.secret.trim());
      }
      await load();
      setModal(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setModalError(`保存失败：${msg}`);
    } finally {
      setSaving(false);
    }
  }, [form, modal, load]);

  const handleDelete = useCallback(async () => {
    if (!deleteId) return;
    setDeleting(true);
    setDeleteError('');
    try {
      await apiClient.deleteConnection(deleteId);
      await load();
      setDeleteId(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setDeleteError(msg);
    } finally {
      setDeleting(false);
    }
  }, [deleteId, load]);

  const handleTest = useCallback(async (connId: string) => {
    setTestStates((prev) => ({ ...prev, [connId]: { state: 'testing', message: '正在测试连接...' } }));
    try {
      const result = await apiClient.testConnection(connId);
      setTestStates((prev) => ({
        ...prev,
        [connId]: result.ok
          ? { state: 'success', message: '连接成功', latency: result.latency_ms ?? undefined }
          : { state: 'failed', message: result.error ?? '未知错误' },
      }));
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setTestStates((prev) => ({ ...prev, [connId]: { state: 'failed', message: msg } }));
    }
  }, []);

  const inputStyle = { width: '100%' };
  const labelStyle = { display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' } as const;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* 区块头 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: '18px', fontWeight: 600, color: 'var(--color-text-primary)' }}>数据库连接</div>
          <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
            数据库的连接对象（地址/端口/用户名/数据库），供智能问数等工具引用；密码加密存储
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button className="btn-secondary" onClick={load} disabled={loading}>
            {loading ? '加载中...' : '刷新'}
          </button>
          <button className="btn-primary" onClick={() => openModal()}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ marginRight: '4px' }}>
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            新增连接
          </button>
        </div>
      </div>

      {error && (
        <div style={{ padding: '10px 12px', borderRadius: 'var(--radius-md)', fontSize: '13px', background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>
          {error}
        </div>
      )}

      {/* 连接表格 */}
      <div className="card" style={{ overflow: 'hidden' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>名称 / ID</th>
              <th>数据库</th>
              <th>用户名</th>
              <th>默认库</th>
              <th>凭据状态</th>
              <th>测试结果</th>
              <th>被引用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} style={{ textAlign: 'center', padding: '32px', color: 'var(--color-text-tertiary)' }}>正在加载...</td></tr>
            ) : connections.length === 0 ? (
              <tr><td colSpan={8} style={{ textAlign: 'center', padding: '32px', color: 'var(--color-text-tertiary)' }}>暂无数据库连接，点击右上角"新增连接"添加</td></tr>
            ) : (
              connections.map((c) => {
                const ts = testStates[c.id];
                return (
                  <tr key={c.id}>
                    <td>
                      <div style={{ fontSize: '13px', color: 'var(--color-text-primary)' }}>{c.name}</div>
                      <div className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>{c.id}</div>
                    </td>
                    <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{c.host}:{c.port}</td>
                    <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{c.username}</td>
                    <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{c.database || '—'}</td>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <div className={`status-dot status-dot-${c.credential_present ? 'success' : 'neutral'}`} />
                        <span style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>
                          {c.credential_present ? '已录入' : '未录入'}
                        </span>
                      </div>
                      <div className="font-mono" style={{ fontSize: '11px', color: 'var(--color-text-tertiary)', marginTop: '2px' }}>{c.credential_id}</div>
                    </td>
                    <td>
                      {ts?.state === 'testing' ? (
                        <span className="status-pill status-pill-info">测试中</span>
                      ) : ts?.state === 'success' ? (
                        <span className="status-pill status-pill-success">OK · {ts.latency ?? '?'}ms</span>
                      ) : ts?.state === 'failed' ? (
                        <span className="status-pill status-pill-error" title={ts.message}>失败</span>
                      ) : (
                        <span style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>—</span>
                      )}
                    </td>
                    <td style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                      {c.referenced_by.length === 0 ? '—' : `${c.referenced_by.length} 个任务`}
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: '4px' }}>
                        <button className="btn-secondary btn-sm" onClick={() => handleTest(c.id)} disabled={ts?.state === 'testing'} title="测试连接">
                          测试
                        </button>
                        <button className="btn-secondary btn-sm" onClick={() => openModal(c)}>编辑</button>
                        <button
                          onClick={() => { setDeleteId(c.id); setDeleteError(''); }}
                          style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-secondary)', padding: '4px', borderRadius: 'var(--radius-sm)', display: 'flex', alignItems: 'center' }}
                          title={c.referenced_by.length > 0 ? '被引用，无法删除' : '删除连接'}
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                          </svg>
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div style={{ fontSize: '13px', color: 'var(--color-text-tertiary)' }}>
        共 {connections.length} 个连接 · 凭据与连接绑定（id 形如 <span className="font-mono">mysql:&lt;连接ID&gt;</span>），Fernet 加密存储，不落明文。
      </div>

      {/* ── 连接编辑弹窗 ── */}
      {modal !== null && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setModal(null); }}
        >
          <div className="card-elevated" style={{ width: '560px', maxHeight: '86vh', overflowY: 'auto', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 20px', borderBottom: '1px solid var(--color-border-subtle)', position: 'sticky', top: 0, background: 'var(--color-bg-primary)' }}>
              <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--color-text-primary)' }}>
                {modal === 'new' ? '新增数据库连接' : `编辑连接：${modal}`}
              </span>
              <button onClick={() => setModal(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-tertiary)', padding: '4px', display: 'flex' }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>

            <div style={{ padding: '20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
              <div>
                <label style={labelStyle}>连接 ID（唯一，编辑后不可改）</label>
                <input className="input-base font-mono" style={inputStyle} value={form.id}
                  onChange={(e) => setForm({ ...form, id: e.target.value })}
                  disabled={modal !== 'new'} placeholder="audit_reader" autoFocus={modal === 'new'} />
              </div>
              <div>
                <label style={labelStyle}>名称</label>
                <input className="input-base" style={inputStyle} value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="智能审批业务数据" />
              </div>
              <div>
                <label style={labelStyle}>数据库地址</label>
                <input className="input-base font-mono" style={inputStyle} value={form.host}
                  onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="10.3.75.137" />
              </div>
              <div>
                <label style={labelStyle}>端口</label>
                <input className="input-base font-mono" style={inputStyle} type="number" value={form.port}
                  onChange={(e) => setForm({ ...form, port: e.target.value })} />
              </div>
              <div>
                <label style={labelStyle}>用户名（建议专用只读账号）</label>
                <input className="input-base font-mono" style={inputStyle} value={form.username}
                  onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="audit_reader" />
              </div>
              <div>
                <label style={labelStyle}>默认库（可选）</label>
                <input className="input-base font-mono" style={inputStyle} value={form.database}
                  onChange={(e) => setForm({ ...form, database: e.target.value })} placeholder="seeyon_oa" />
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={labelStyle}>
                  {modal === 'new' ? '数据库密码' : '更新数据库密码（留空 = 不修改）'}
                </label>
                <input className="input-base font-mono" style={inputStyle} type="password" value={form.secret}
                  onChange={(e) => setForm({ ...form, secret: e.target.value })}
                  placeholder="MySQL 登录密码" />
                <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)', marginTop: '6px', lineHeight: 1.5 }}>
                  密码 Fernet 加密存储；连接测试走 MySQL 握手（非 SSH）。
                </div>
              </div>
              <div style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <input id="db-conn-enabled" type="checkbox" checked={form.enabled}
                  onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                <label htmlFor="db-conn-enabled" style={{ fontSize: '13px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
                  启用该连接
                </label>
              </div>

              {modalError && (
                <div style={{ gridColumn: '1 / -1', fontSize: '13px', color: 'var(--state-error)', padding: '8px 12px', background: 'var(--state-error-tint)', borderRadius: 'var(--radius-md)' }}>
                  {modalError}
                </div>
              )}
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '12px 20px', borderTop: '1px solid var(--color-border-subtle)', position: 'sticky', bottom: 0, background: 'var(--color-bg-primary)' }}>
              <button className="btn-secondary btn-sm" onClick={() => setModal(null)}>取消</button>
              <button className="btn-primary btn-sm" onClick={handleSave} disabled={saving}>
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 删除确认 ── */}
      {deleteId && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setDeleteId(null); }}>
          <div className="card-elevated" style={{ width: '420px', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ padding: '20px' }}>
              <div style={{ fontSize: '15px', fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: '8px' }}>删除数据库连接</div>
              <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
                确定删除 <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>{deleteId}</span> 吗？
                <br />被引用时会拒绝删除；已录入的凭据保留。
              </div>
              {deleteError && (
                <div style={{ marginTop: '10px', fontSize: '13px', color: 'var(--state-error)', padding: '8px 12px', background: 'var(--state-error-tint)', borderRadius: 'var(--radius-md)' }}>
                  {deleteError}
                </div>
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '0 20px 20px' }}>
              <button className="btn-secondary btn-sm" onClick={() => setDeleteId(null)} disabled={deleting}>取消</button>
              <button className="btn-sm" style={{ background: 'var(--state-error)', color: '#fff', border: 'none', borderRadius: 'var(--radius-md)', fontWeight: 600, fontSize: '13px', padding: '0 16px', height: '32px', cursor: 'pointer' }}
                onClick={handleDelete} disabled={deleting}>
                {deleting ? '删除中...' : '删除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default DbConnectionsSettings;
