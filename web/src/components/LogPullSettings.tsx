import { useState, useEffect, useCallback } from 'react';
import {
  apiClient,
  type ConnectionInfo,
  type LogPullSourceInfo,
  type LogSourceDirInfo,
} from '../lib/api';

/**
 * 日志拉取配置（凭据管理页 Tab2）：本地日志目录 + 拉取任务。
 * 本地日志目录 = log_sources 白名单（config/patrol.yaml），拉取任务落盘的目标位置；
 * 拉取任务 = "从哪台机器拉哪些目录落到本地哪个白名单目录"，存 ~/.agentops/private/log-pull.yaml；
 * 连接参数（host/port/用户名/认证）不在本组件配置 —— 通过 connection_id 引用「服务器连接」Tab 的连接对象；
 * 拉取计划已迁移至「定时计划」页（config/schedules.yaml 统一管理）。
 */

// 拉取任务编辑表单状态（空字符串 = 新增）
interface SourceForm {
  id: string;
  name: string;
  connection_id: string;        // 引用服务器连接对象
  remote_paths_text: string;    // textarea，一行一个
  local_log_source_id: string;
  local_max_days: string;
  enabled: boolean;
}

const EMPTY_SOURCE: SourceForm = {
  id: '', name: '', connection_id: '', remote_paths_text: '', local_log_source_id: '',
  local_max_days: '7', enabled: false,
};

// 本地日志目录编辑表单状态
interface DirForm {
  id: string;
  name: string;
  path: string;
  description: string;
  allow_read: boolean;
  allow_list: boolean;
}

const EMPTY_DIR: DirForm = {
  id: '', name: '', path: '', description: '', allow_read: true, allow_list: true,
};

function fmtNextRun(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function LogPullSettings() {
  const [sources, setSources] = useState<LogPullSourceInfo[]>([]);
  const [connections, setConnections] = useState<ConnectionInfo[]>([]);
  const [logSourceDirs, setLogSourceDirs] = useState<LogSourceDirInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // 任务弹窗（null 关闭；'new' 新增；其余 = 编辑该 id）
  const [sourceModal, setSourceModal] = useState<'new' | string | null>(null);
  const [sourceForm, setSourceForm] = useState<SourceForm>(EMPTY_SOURCE);
  const [sourceModalError, setSourceModalError] = useState('');
  const [sourceSaving, setSourceSaving] = useState(false);
  const [sourceDeleteId, setSourceDeleteId] = useState<string | null>(null);
  const [sourceDeleting, setSourceDeleting] = useState(false);

  // 本地日志目录弹窗
  const [dirModal, setDirModal] = useState<'new' | string | null>(null);
  const [dirForm, setDirForm] = useState<DirForm>(EMPTY_DIR);
  const [dirModalError, setDirModalError] = useState('');
  const [dirSaving, setDirSaving] = useState(false);
  const [dirDeleteId, setDirDeleteId] = useState<string | null>(null);
  const [dirDeleting, setDirDeleting] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [src, conn, dirs] = await Promise.all([
        apiClient.listLogPullSources(),
        apiClient.listConnections(),
        apiClient.listLogSourceDirs(),
      ]);
      setSources(src.sources || []);
      setConnections(conn.connections || []);
      setLogSourceDirs(dirs.log_sources || []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(`加载日志拉取配置失败：${msg}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // ── 本地日志目录 ──
  const openDirModal = useCallback((dir?: LogSourceDirInfo) => {
    if (dir) {
      setDirForm({
        id: dir.id, name: dir.name, path: dir.path, description: dir.description,
        allow_read: dir.allow_read, allow_list: dir.allow_list,
      });
      setDirModal(dir.id);
    } else {
      setDirForm(EMPTY_DIR);
      setDirModal('new');
    }
    setDirModalError('');
  }, []);

  const handleSaveDir = useCallback(async () => {
    const f = dirForm;
    if (dirModal === 'new' && !f.id.trim()) { setDirModalError('请输入目录 ID'); return; }
    if (!f.path.trim()) { setDirModalError('请输入本地存储路径'); return; }
    setDirSaving(true);
    setDirModalError('');
    try {
      await apiClient.upsertLogSourceDir({
        id: f.id.trim(), name: f.name.trim() || f.id.trim(), path: f.path.trim(),
        description: f.description.trim(), allow_read: f.allow_read, allow_list: f.allow_list,
      });
      await loadAll();
      setDirModal(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setDirModalError(`保存失败：${msg}`);
    } finally {
      setDirSaving(false);
    }
  }, [dirForm, dirModal, loadAll]);

  const handleDeleteDir = useCallback(async () => {
    if (!dirDeleteId) return;
    setDirDeleting(true);
    try {
      await apiClient.deleteLogSourceDir(dirDeleteId);
      await loadAll();
      setDirDeleteId(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(`删除本地日志目录失败：${msg}`);
      setDirDeleteId(null);
    } finally {
      setDirDeleting(false);
    }
  }, [dirDeleteId, loadAll]);

  // ── 拉取任务 ──
  const openSourceModal = useCallback((src?: LogPullSourceInfo) => {
    if (src) {
      setSourceForm({
        id: src.id, name: src.name, connection_id: src.connection_id,
        remote_paths_text: src.remote_paths.join('\n'),
        local_log_source_id: src.local_log_source_id,
        local_max_days: String(src.local_max_days), enabled: src.enabled,
      });
      setSourceModal(src.id);
    } else {
      setSourceForm({ ...EMPTY_SOURCE, connection_id: connections[0]?.id || '' });
      setSourceModal('new');
    }
    setSourceModalError('');
  }, [connections]);

  const handleSaveSource = useCallback(async () => {
    const f = sourceForm;
    const isNew = sourceModal === 'new';
    if (isNew && !f.id.trim()) { setSourceModalError('请输入任务 ID'); return; }
    if (!f.connection_id) { setSourceModalError('请选择服务器连接（在「服务器连接」Tab 配置）'); return; }
    const paths = f.remote_paths_text.split('\n').map((s) => s.trim()).filter(Boolean);
    if (paths.length === 0) { setSourceModalError('远程抽取目录至少填一个（支持 * 通配）'); return; }
    if (!f.local_log_source_id) { setSourceModalError('请选择本地存放目录（log_sources 白名单）'); return; }
    const maxDays = parseInt(f.local_max_days, 10) || 7;
    setSourceSaving(true);
    setSourceModalError('');
    try {
      await apiClient.upsertLogPullSource({
        id: f.id.trim(), name: f.name.trim() || f.id.trim(),
        connection_id: f.connection_id,
        remote_paths: paths, local_log_source_id: f.local_log_source_id,
        local_max_days: maxDays, enabled: f.enabled,
      });
      await loadAll();
      setSourceModal(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setSourceModalError(`保存失败：${msg}`);
    } finally {
      setSourceSaving(false);
    }
  }, [sourceForm, sourceModal, loadAll]);

  const handleDeleteSource = useCallback(async () => {
    if (!sourceDeleteId) return;
    setSourceDeleting(true);
    try {
      await apiClient.deleteLogPullSource(sourceDeleteId);
      await loadAll();
      setSourceDeleteId(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(`删除拉取任务失败：${msg}`);
      setSourceDeleteId(null);
    } finally {
      setSourceDeleting(false);
    }
  }, [sourceDeleteId, loadAll]);

  const inputStyle = { width: '100%' };
  const labelStyle = { display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' } as const;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* 区块头 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: '18px', fontWeight: 600, color: 'var(--color-text-primary)' }}>日志抽取配置</div>
          <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
            本地日志目录（落盘目标）+ 拉取任务（远程抽取目录）；服务器连接参数通过 connection_id 引用「服务器连接」Tab 的连接对象
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button className="btn-secondary" onClick={loadAll} disabled={loading}>
            {loading ? '加载中...' : '刷新'}
          </button>
          <button className="btn-primary" onClick={() => openSourceModal()} disabled={connections.length === 0} title={connections.length === 0 ? '请先在「服务器连接」Tab 配置连接' : undefined}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ marginRight: '4px' }}>
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            新增任务
          </button>
        </div>
      </div>

      {error && (
        <div style={{ padding: '10px 12px', borderRadius: 'var(--radius-md)', fontSize: '13px', background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>
          {error}
        </div>
      )}

      {/* ── 本地日志目录（log_sources 白名单，拉取任务的落盘目标）── */}
      <div className="card" style={{ overflow: 'hidden' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '14px 16px', borderBottom: '1px solid var(--color-border-subtle)' }}>
          <div>
            <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--color-text-primary)' }}>本地日志目录</div>
            <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)', marginTop: '2px' }}>
              拉取任务日志落盘到本地的存储目录（config/patrol.yaml log_sources 白名单），被任务引用的目录不可删除
            </div>
          </div>
          <button className="btn-secondary btn-sm" onClick={() => openDirModal()}>新增目录</button>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>目录 ID / 名称</th>
              <th>本地存储路径</th>
              <th>权限</th>
              <th>被引用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={5} style={{ textAlign: 'center', padding: '24px', color: 'var(--color-text-tertiary)' }}>正在加载...</td></tr>
            ) : logSourceDirs.length === 0 ? (
              <tr><td colSpan={5} style={{ textAlign: 'center', padding: '24px', color: 'var(--color-text-tertiary)' }}>暂无本地日志目录，点击"新增目录"添加</td></tr>
            ) : (
              logSourceDirs.map((d) => (
                <tr key={d.id}>
                  <td>
                    <div className="font-mono" style={{ fontSize: '13px', color: 'var(--color-text-primary)' }}>{d.id}</div>
                    <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>{d.name}</div>
                  </td>
                  <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{d.path}</td>
                  <td style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                    {d.allow_read ? '读' : '—读'} / {d.allow_list ? '列目录' : '—列目录'}
                  </td>
                  <td style={{ fontSize: '12px' }}>
                    {d.referenced_by.length === 0 ? (
                      <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>
                    ) : (
                      <span className="font-mono" style={{ color: 'var(--color-text-secondary)' }}>{d.referenced_by.join(', ')}</span>
                    )}
                  </td>
                  <td>
                    <div style={{ display: 'flex', gap: '4px' }}>
                      <button className="btn-secondary btn-sm" onClick={() => openDirModal(d)}>编辑</button>
                      <button
                        onClick={() => setDirDeleteId(d.id)}
                        style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-secondary)', padding: '4px', borderRadius: 'var(--radius-sm)', display: 'flex', alignItems: 'center' }}
                        title={d.referenced_by.length > 0 ? `被任务引用：${d.referenced_by.join(', ')}` : '删除目录'}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 任务表格 */}
      <div className="card" style={{ overflow: 'hidden' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '14px 16px', borderBottom: '1px solid var(--color-border-subtle)' }}>
          <div>
            <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--color-text-primary)' }}>拉取任务</div>
            <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)', marginTop: '2px' }}>
              从目标服务器的远程目录抽取日志，落到上方本地日志目录
            </div>
          </div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>任务 ID / 名称</th>
              <th>服务器连接</th>
              <th>远程抽取目录</th>
              <th>本地目录</th>
              <th>拉取计划</th>
              <th>启用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} style={{ textAlign: 'center', padding: '32px', color: 'var(--color-text-tertiary)' }}>正在加载...</td></tr>
            ) : sources.length === 0 ? (
              <tr><td colSpan={7} style={{ textAlign: 'center', padding: '32px', color: 'var(--color-text-tertiary)' }}>暂无拉取任务，点击右上角"新增任务"添加</td></tr>
            ) : (
              sources.map((s) => (
                <tr key={s.id}>
                  <td>
                    <div className="font-mono" style={{ fontSize: '13px', color: 'var(--color-text-primary)' }}>{s.id}</div>
                    <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>{s.name}</div>
                  </td>
                  <td>
                    {s.connection ? (
                      <>
                        <div style={{ fontSize: '13px', color: 'var(--color-text-primary)' }}>{s.connection.name}</div>
                        <div className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>
                          {s.connection.host_masked}
                        </div>
                      </>
                    ) : (
                      <span style={{ fontSize: '12px', color: 'var(--state-error)' }}>连接缺失</span>
                    )}
                  </td>
                  <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)', maxWidth: '220px' }}>
                    {s.remote_paths[0] || '—'}{s.remote_paths.length > 1 && <span style={{ color: 'var(--color-text-tertiary)' }}> +{s.remote_paths.length - 1}</span>}
                  </td>
                  <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{s.local_log_source_id}</td>
                  <td style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                    {s.schedules.length === 0 ? '—' : (
                      <div>
                        {s.schedules.map((sc) => (
                          <div key={sc.name} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span>{sc.name}</span>
                            <span className="font-mono" style={{ color: 'var(--color-text-tertiary)' }}>{sc.cron}</span>
                            <span style={{ color: sc.enabled ? 'var(--state-success)' : 'var(--color-text-tertiary)' }}>
                              {sc.enabled ? `→ ${fmtNextRun(sc.next_run)}` : '（停用）'}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <div className={`status-dot status-dot-${s.enabled ? 'success' : 'neutral'}`} />
                      <span style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>{s.enabled ? '已启用' : '停用'}</span>
                    </div>
                  </td>
                  <td>
                    <div style={{ display: 'flex', gap: '4px' }}>
                      <button className="btn-secondary btn-sm" onClick={() => openSourceModal(s)}>编辑</button>
                      <button
                        onClick={() => setSourceDeleteId(s.id)}
                        style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-secondary)', padding: '4px', borderRadius: 'var(--radius-sm)', display: 'flex', alignItems: 'center' }}
                        title="删除任务（级联删除其计划）"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 拉取计划迁移提示 */}
      <div style={{
        padding: '12px 16px', borderRadius: 'var(--radius-md)', fontSize: '13px',
        background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)',
        display: 'flex', alignItems: 'center', gap: '8px',
      }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, color: 'var(--state-info)' }}>
          <circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
        <span>拉取计划已迁移至侧边栏「定时计划」页统一管理（cron 触发配置见该页）。</span>
      </div>

      <div style={{ fontSize: '13px', color: 'var(--color-text-tertiary)' }}>
        配置改动回写 ~/.agentops/private/log-pull.yaml，重启后端后生效。
      </div>

      {/* ── 任务编辑弹窗 ── */}
      {sourceModal !== null && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setSourceModal(null); }}
        >
          <div className="card-elevated" style={{ width: '600px', maxHeight: '86vh', overflowY: 'auto', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 20px', borderBottom: '1px solid var(--color-border-subtle)', position: 'sticky', top: 0, background: 'var(--color-bg-primary)' }}>
              <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--color-text-primary)' }}>
                {sourceModal === 'new' ? '新增拉取任务' : `编辑拉取任务：${sourceModal}`}
              </span>
              <button onClick={() => setSourceModal(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-tertiary)', padding: '4px', display: 'flex' }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>

            <div style={{ padding: '20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
              <div>
                <label style={labelStyle}>任务 ID（唯一，编辑后不可改）</label>
                <input className="input-base font-mono" style={inputStyle} value={sourceForm.id}
                  onChange={(e) => setSourceForm({ ...sourceForm, id: e.target.value })}
                  disabled={sourceModal !== 'new'} placeholder="prod-seeyon" autoFocus={sourceModal === 'new'} />
              </div>
              <div>
                <label style={labelStyle}>名称</label>
                <input className="input-base" style={inputStyle} value={sourceForm.name}
                  onChange={(e) => setSourceForm({ ...sourceForm, name: e.target.value })} placeholder="生产-致远OA" />
              </div>
              <div>
                <label style={labelStyle}>服务器连接（在「服务器连接」Tab 配置）</label>
                <select className="input-base font-mono" style={inputStyle} value={sourceForm.connection_id}
                  onChange={(e) => setSourceForm({ ...sourceForm, connection_id: e.target.value })}>
                  <option value="">（请选择）</option>
                  {connections.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.id} · {c.name}（{c.host}:{c.port}）
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label style={labelStyle}>本地存放目录（在上方「本地日志目录」区块配置）</label>
                <select className="input-base font-mono" style={inputStyle} value={sourceForm.local_log_source_id}
                  onChange={(e) => setSourceForm({ ...sourceForm, local_log_source_id: e.target.value })}>
                  <option value="">（请选择）</option>
                  {logSourceDirs.map((d) => (
                    <option key={d.id} value={d.id}>{d.id} · {d.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label style={labelStyle}>本地保留天数（超期清理）</label>
                <input className="input-base font-mono" style={inputStyle} type="number" value={sourceForm.local_max_days}
                  onChange={(e) => setSourceForm({ ...sourceForm, local_max_days: e.target.value })} />
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={labelStyle}>远程抽取目录（一行一个，支持 * 通配）</label>
                <textarea className="input-base font-mono" style={{ ...inputStyle, minHeight: '72px', resize: 'vertical' }}
                  value={sourceForm.remote_paths_text}
                  onChange={(e) => setSourceForm({ ...sourceForm, remote_paths_text: e.target.value })}
                  placeholder={'/data/seeyon/logs/*.log\n/data/seeyon/logs/catalina.out'} />
              </div>
              <div style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <input id="src-enabled" type="checkbox" checked={sourceForm.enabled}
                  onChange={(e) => setSourceForm({ ...sourceForm, enabled: e.target.checked })} />
                <label htmlFor="src-enabled" style={{ fontSize: '13px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
                  启用该拉取任务（凭据/私钥/网络就绪后再开）
                </label>
              </div>

              {sourceModalError && (
                <div style={{ gridColumn: '1 / -1', fontSize: '13px', color: 'var(--state-error)', padding: '8px 12px', background: 'var(--state-error-tint)', borderRadius: 'var(--radius-md)' }}>
                  {sourceModalError}
                </div>
              )}
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '12px 20px', borderTop: '1px solid var(--color-border-subtle)', position: 'sticky', bottom: 0, background: 'var(--color-bg-primary)' }}>
              <button className="btn-secondary btn-sm" onClick={() => setSourceModal(null)}>取消</button>
              <button className="btn-primary btn-sm" onClick={handleSaveSource} disabled={sourceSaving}>
                {sourceSaving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 任务删除确认 ── */}
      {sourceDeleteId && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setSourceDeleteId(null); }}>
          <div className="card-elevated" style={{ width: '400px', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ padding: '20px' }}>
              <div style={{ fontSize: '15px', fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: '8px' }}>删除拉取任务</div>
              <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
                确定删除 <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>{sourceDeleteId}</span> 吗？
                <br />引用它的拉取计划会一并删除，服务器连接与凭据保留。
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '0 20px 20px' }}>
              <button className="btn-secondary btn-sm" onClick={() => setSourceDeleteId(null)} disabled={sourceDeleting}>取消</button>
              <button className="btn-sm" style={{ background: 'var(--state-error)', color: '#fff', border: 'none', borderRadius: 'var(--radius-md)', fontWeight: 600, fontSize: '13px', padding: '0 16px', height: '32px', cursor: 'pointer' }}
                onClick={handleDeleteSource} disabled={sourceDeleting}>
                {sourceDeleting ? '删除中...' : '删除'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 本地日志目录编辑弹窗 ── */}
      {dirModal !== null && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setDirModal(null); }}
        >
          <div className="card-elevated" style={{ width: '520px', maxHeight: '86vh', overflowY: 'auto', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 20px', borderBottom: '1px solid var(--color-border-subtle)', position: 'sticky', top: 0, background: 'var(--color-bg-primary)' }}>
              <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--color-text-primary)' }}>
                {dirModal === 'new' ? '新增本地日志目录' : `编辑本地日志目录：${dirModal}`}
              </span>
              <button onClick={() => setDirModal(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-tertiary)', padding: '4px', display: 'flex' }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>

            <div style={{ padding: '20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
              <div>
                <label style={labelStyle}>目录 ID（唯一，创建后不可改）</label>
                <input className="input-base font-mono" style={inputStyle} value={dirForm.id}
                  onChange={(e) => setDirForm({ ...dirForm, id: e.target.value })}
                  disabled={dirModal !== 'new'} placeholder="seeyon" autoFocus={dirModal === 'new'} />
              </div>
              <div>
                <label style={labelStyle}>名称</label>
                <input className="input-base" style={inputStyle} value={dirForm.name}
                  onChange={(e) => setDirForm({ ...dirForm, name: e.target.value })} placeholder="致远 OA 日志" />
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={labelStyle}>本地存储路径（拉取任务日志落盘位置）</label>
                <input className="input-base font-mono" style={inputStyle} value={dirForm.path}
                  onChange={(e) => setDirForm({ ...dirForm, path: e.target.value })}
                  placeholder="./logs/seeyon 或 /var/agentops/logs/seeyon" />
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={labelStyle}>描述（可选）</label>
                <input className="input-base" style={inputStyle} value={dirForm.description}
                  onChange={(e) => setDirForm({ ...dirForm, description: e.target.value })} />
              </div>
              <div style={{ gridColumn: '1 / -1', display: 'flex', gap: '20px' }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
                  <input type="checkbox" checked={dirForm.allow_read}
                    onChange={(e) => setDirForm({ ...dirForm, allow_read: e.target.checked })} />
                  允许读取（allow_read）
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
                  <input type="checkbox" checked={dirForm.allow_list}
                    onChange={(e) => setDirForm({ ...dirForm, allow_list: e.target.checked })} />
                  允许列目录（allow_list）
                </label>
              </div>

              {dirModalError && (
                <div style={{ gridColumn: '1 / -1', fontSize: '13px', color: 'var(--state-error)', padding: '8px 12px', background: 'var(--state-error-tint)', borderRadius: 'var(--radius-md)' }}>
                  {dirModalError}
                </div>
              )}
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '12px 20px', borderTop: '1px solid var(--color-border-subtle)', position: 'sticky', bottom: 0, background: 'var(--color-bg-primary)' }}>
              <button className="btn-secondary btn-sm" onClick={() => setDirModal(null)}>取消</button>
              <button className="btn-primary btn-sm" onClick={handleSaveDir} disabled={dirSaving}>
                {dirSaving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 目录删除确认 ── */}
      {dirDeleteId && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setDirDeleteId(null); }}>
          <div className="card-elevated" style={{ width: '400px', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ padding: '20px' }}>
              <div style={{ fontSize: '15px', fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: '8px' }}>删除本地日志目录</div>
              <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
                确定删除 <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>{dirDeleteId}</span> 吗？
                <br />仅移除白名单条目，不删除磁盘上的日志文件；被拉取任务引用时无法删除。
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '0 20px 20px' }}>
              <button className="btn-secondary btn-sm" onClick={() => setDirDeleteId(null)} disabled={dirDeleting}>取消</button>
              <button className="btn-sm" style={{ background: 'var(--state-error)', color: '#fff', border: 'none', borderRadius: 'var(--radius-md)', fontWeight: 600, fontSize: '13px', padding: '0 16px', height: '32px', cursor: 'pointer' }}
                onClick={handleDeleteDir} disabled={dirDeleting}>
                {dirDeleting ? '删除中...' : '删除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default LogPullSettings;
