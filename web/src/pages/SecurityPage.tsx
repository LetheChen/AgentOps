import { useCallback, useEffect, useState } from 'react';
import * as secApi from '../lib/securityApi';
import { useAuth } from '../components/AuthGate';

export type SecuritySection = 'users' | 'roles' | 'sessions' | 'tokens';

const SECTION_META: Record<SecuritySection, { title: string; desc: string }> = {
  users: { title: '用户管理', desc: '账号生命周期：创建、启停、锁定、重置密码、角色绑定' },
  roles: { title: '角色与权限', desc: '4 个内置角色 × 46 个权限点的授权矩阵（只读，改配置走迁移）' },
  sessions: { title: '登录会话', desc: '当前账号的活跃登录会话，可远程下线' },
  tokens: { title: 'API Token', desc: '长期编程凭证（PAT），创建时明文只显示一次' },
};

// ── 通用小组件 ──

function Badge({ tone, children }: { tone: 'ok' | 'warn' | 'err' | 'muted'; children: React.ReactNode }) {
  const colors: Record<string, { bg: string; fg: string }> = {
    ok: { bg: 'var(--state-success-tint, rgba(34,197,94,.12))', fg: 'var(--state-success, #22c55e)' },
    warn: { bg: 'var(--state-warning-tint, rgba(234,179,8,.12))', fg: 'var(--state-warning, #eab308)' },
    err: { bg: 'var(--state-error-tint)', fg: 'var(--state-error)' },
    muted: { bg: 'var(--color-surface-tertiary, rgba(128,128,128,.15))', fg: 'var(--color-text-secondary)' },
  };
  const c = colors[tone];
  return (
    <span style={{ display: 'inline-block', padding: '2px 8px', borderRadius: 999, fontSize: 11, background: c.bg, color: c.fg }}>
      {children}
    </span>
  );
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div
      style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
      onClick={onClose}
    >
      <div className="card card-elevated" style={{ width: 460, maxHeight: '80vh', overflow: 'auto', padding: 20 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 14, color: 'var(--color-text-primary)' }}>{title}</div>
        {children}
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '7px 10px',
  borderRadius: 'var(--radius-md, 8px)',
  border: '1px solid var(--color-border, rgba(128,128,128,.3))',
  background: 'var(--color-surface-secondary, transparent)',
  color: 'var(--color-text-primary)',
  fontSize: 13,
  boxSizing: 'border-box',
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block', marginBottom: 10 }}>
      <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 4 }}>{label}</div>
      {children}
    </label>
  );
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleString('zh-CN', { hour12: false });
}

// ── 登录卡 ──

function LoginCard({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      const resp = await secApi.login(username, password);
      secApi.setStoredToken(resp.token);
      onLoggedIn();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 60 }}>
      <div className="card card-elevated" style={{ width: 380, padding: 24 }}>
        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 6, color: 'var(--color-text-primary)' }}>安全管理 · 登录</div>
        <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 16 }}>访问安全模块需要管理员身份（owner / admin 角色）</div>
        <Field label="用户名">
          <input style={inputStyle} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        </Field>
        <Field label="密码">
          <input style={inputStyle} type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !busy) submit(); }} />
        </Field>
        {err && <div style={{ fontSize: 12, color: 'var(--state-error)', marginBottom: 10 }}>{err}</div>}
        <button className="btn-primary" style={{ width: '100%' }} onClick={submit} disabled={busy || !username || !password}>
          {busy ? '登录中...' : '登录'}
        </button>
      </div>
    </div>
  );
}

// ── 强制改密卡 ──

function MustResetCard({ onDone }: { onDone: () => void }) {
  const [oldPw, setOldPw] = useState('');
  const [newPw, setNewPw] = useState('');
  const [confirm, setConfirm] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setErr(null);
    if (newPw !== confirm) { setErr('两次输入的新密码不一致'); return; }
    setBusy(true);
    try {
      await secApi.changePassword(oldPw, newPw);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 60 }}>
      <div className="card card-elevated" style={{ width: 380, padding: 24 }}>
        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 6, color: 'var(--color-text-primary)' }}>请先修改初始密码</div>
        <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 16 }}>安全策略要求：首次登录（或密码被重置）后必须改密才能继续使用</div>
        <Field label="当前密码">
          <input style={inputStyle} type="password" value={oldPw} onChange={(e) => setOldPw(e.target.value)} />
        </Field>
        <Field label="新密码">
          <input style={inputStyle} type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} />
        </Field>
        <Field label="确认新密码">
          <input style={inputStyle} type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        {err && <div style={{ fontSize: 12, color: 'var(--state-error)', marginBottom: 10 }}>{err}</div>}
        <button className="btn-primary" style={{ width: '100%' }} onClick={submit} disabled={busy || !oldPw || !newPw}>
          {busy ? '提交中...' : '修改密码'}
        </button>
      </div>
    </div>
  );
}

// ── 用户管理 ──

function UsersSection({ me, refreshMe }: { me: secApi.MeInfo; refreshMe: () => void }) {
  const [users, setUsers] = useState<secApi.SecurityUser[]>([]);
  const [roles, setRoles] = useState<secApi.SecurityRole[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const [u, r] = await Promise.all([secApi.listUsers(), secApi.listRoles().catch(() => ({ roles: [], total: 0 }))]);
      setUsers(u.users);
      setRoles(r.roles);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const act = async (userId: string, fn: () => Promise<unknown>) => {
    setErr(null);
    setBusyId(userId);
    try {
      await fn();
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const canWrite = me.scopes.includes('security.users.write') || me.scopes.includes('*');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button className="btn-secondary" onClick={load} disabled={loading}>{loading ? '加载中...' : '刷新'}</button>
        {canWrite && <button className="btn-primary" onClick={() => setShowCreate(true)}>+ 新建用户</button>}
      </div>
      {err && <div style={{ padding: '10px 12px', borderRadius: 8, fontSize: 13, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>{err}</div>}
      <div className="card" style={{ overflow: 'auto' }}>
        <table className="data-table" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th>用户</th><th>角色</th><th>状态</th><th>最近活跃</th><th>创建时间</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.user_id}>
                <td>
                  <div style={{ fontSize: 13, color: 'var(--color-text-primary)' }}>
                    {u.display_name || u.username}
                    {u.user_id === me.user.user_id && <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginLeft: 6 }}>(我)</span>}
                  </div>
                  <div className="font-mono" style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>{u.username}{u.email ? ` · ${u.email}` : ''}</div>
                </td>
                <td>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                    {u.roles.length === 0 && <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>无</span>}
                    {u.roles.map((r) => <Badge key={r} tone="muted">{r}</Badge>)}
                  </div>
                </td>
                <td>
                  <div style={{ display: 'flex', gap: 4 }}>
                    {u.disabled ? <Badge tone="err">已禁用</Badge> : u.locked ? <Badge tone="warn">已锁定</Badge> : <Badge tone="ok">正常</Badge>}
                    {u.is_online && <Badge tone="ok">在线</Badge>}
                  </div>
                </td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtTime(u.last_seen_at)}</td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtTime(u.created_at)}</td>
                <td>
                  {canWrite && (
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      <button className="btn-secondary btn-sm" disabled={busyId === u.user_id}
                        onClick={() => act(u.user_id, () => secApi.updateUser(u.user_id, { disabled: !u.disabled }))}>
                        {u.disabled ? '启用' : '禁用'}
                      </button>
                      {!u.disabled && (
                        <button className="btn-secondary btn-sm" disabled={busyId === u.user_id}
                          onClick={() => act(u.user_id, () => secApi.setUserLocked(u.user_id, !u.locked))}>
                          {u.locked ? '解锁' : '锁定'}
                        </button>
                      )}
                      <button className="btn-secondary btn-sm" disabled={busyId === u.user_id}
                        onClick={() => {
                          const pw = window.prompt(`为 ${u.username} 设置新密码（重置后该用户下次登录需强制改密）：`);
                          if (pw) act(u.user_id, () => secApi.resetUserPassword(u.user_id, pw));
                        }}>
                        重置密码
                      </button>
                      {u.user_id !== me.user.user_id && (
                        <button className="btn-secondary btn-sm" disabled={busyId === u.user_id}
                          onClick={() => {
                            if (window.confirm(`确认删除用户 ${u.username}？该操作不可恢复。`)) {
                              act(u.user_id, () => secApi.deleteUser(u.user_id));
                            }
                          }}>
                          删除
                        </button>
                      )}
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {users.length === 0 && !loading && (
              <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--color-text-tertiary)', padding: 24 }}>暂无用户</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {showCreate && (
        <CreateUserModal
          roles={roles.filter((r) => r.is_assignable)}
          onClose={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); load(); }}
        />
      )}
    </div>
  );
}

function CreateUserModal({ roles, onClose, onCreated }: { roles: secApi.SecurityRole[]; onClose: () => void; onCreated: () => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [roleIds, setRoleIds] = useState<string[]>(['role_viewer']);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const toggleRole = (id: string) => {
    setRoleIds((prev) => (prev.includes(id) ? prev.filter((r) => r !== id) : [...prev, id]));
  };

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      await secApi.createUser({ username, password, display_name: displayName, email, role_ids: roleIds });
      onCreated();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="新建用户" onClose={onClose}>
      <Field label="用户名 *"><input style={inputStyle} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus /></Field>
      <Field label="初始密码 *"><input style={inputStyle} type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
      <Field label="显示名"><input style={inputStyle} value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></Field>
      <Field label="邮箱"><input style={inputStyle} value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
      <Field label="角色">
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {roles.map((r) => (
            <label key={r.role_id} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, cursor: 'pointer', color: 'var(--color-text-primary)' }}>
              <input type="checkbox" checked={roleIds.includes(r.role_id)} onChange={() => toggleRole(r.role_id)} />
              {r.name}（{r.role_id}）
            </label>
          ))}
        </div>
      </Field>
      {err && <div style={{ fontSize: 12, color: 'var(--state-error)', marginBottom: 10 }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button className="btn-secondary" onClick={onClose}>取消</button>
        <button className="btn-primary" onClick={submit} disabled={busy || !username || !password}>{busy ? '创建中...' : '创建'}</button>
      </div>
    </Modal>
  );
}

// ── 角色与权限 ──

function RolesSection() {
  const [roles, setRoles] = useState<secApi.SecurityRole[]>([]);
  const [perms, setPerms] = useState<secApi.SecurityPermission[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [r, p] = await Promise.all([secApi.listRoles(), secApi.listPermissions()]);
        setRoles(r.roles);
        setPerms(p.permissions);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // 权限按 resource 分组
  const byResource = new Map<string, secApi.SecurityPermission[]>();
  perms.forEach((p) => {
    const list = byResource.get(p.resource) ?? [];
    list.push(p);
    byResource.set(p.resource, list);
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {err && <div style={{ padding: '10px 12px', borderRadius: 8, fontSize: 13, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>{err}</div>}
      {loading && <div style={{ color: 'var(--color-text-tertiary)', fontSize: 13 }}>加载中...</div>}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 }}>
        {roles.map((r) => (
          <div key={r.role_id} className="card" style={{ padding: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--color-text-primary)' }}>{r.name}</div>
              {r.is_builtin && <Badge tone="muted">内置</Badge>}
            </div>
            <div className="font-mono" style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginBottom: 6 }}>{r.role_id}</div>
            <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 10, minHeight: 32 }}>{r.description}</div>
            <div style={{ fontSize: 12, color: 'var(--color-text-primary)' }}>
              权限 <strong>{r.permission_count}</strong> 项
              {!r.is_assignable && <span style={{ color: 'var(--color-text-tertiary)' }}>（不可绑定）</span>}
            </div>
            <details style={{ marginTop: 8 }}>
              <summary style={{ fontSize: 12, cursor: 'pointer', color: 'var(--color-text-secondary)' }}>查看权限明细</summary>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 8 }}>
                {r.permissions.map((p) => <Badge key={p} tone="muted">{p}</Badge>)}
              </div>
            </details>
          </div>
        ))}
      </div>

      <div className="card" style={{ overflow: 'auto' }}>
        <div style={{ padding: '12px 16px', fontSize: 14, fontWeight: 600, color: 'var(--color-text-primary)' }}>权限点 · 按资源域</div>
        <table className="data-table" style={{ width: '100%' }}>
          <thead>
            <tr><th style={{ width: 140 }}>资源域</th><th>权限点</th><th style={{ width: 260 }}>持有角色</th></tr>
          </thead>
          <tbody>
            {[...byResource.entries()].map(([resource, list]) => (
              <tr key={resource}>
                <td className="font-mono" style={{ fontSize: 12, color: 'var(--color-text-primary)' }}>{resource}</td>
                <td>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                    {list.map((p) => (
                      <span key={p.perm_id} title={p.description} className="font-mono" style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'var(--color-surface-tertiary, rgba(128,128,128,.12))', color: 'var(--color-text-secondary)' }}>
                        {p.perm_id}
                      </span>
                    ))}
                  </div>
                </td>
                <td>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                    {[...new Set(list.flatMap((p) => p.roles))].map((r) => <Badge key={r} tone="muted">{r}</Badge>)}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── 登录会话 ──

function SessionsSection({ me }: { me: secApi.MeInfo }) {
  const [sessions, setSessions] = useState<secApi.SecuritySession[]>([]);
  const [userLookup, setUserLookup] = useState<Record<string, secApi.SecurityUser>>({});
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [targetUser, setTargetUser] = useState('');

  const canReadAll = me.scopes.includes('security.sessions.read') || me.scopes.includes('*');

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      // 跨用户会话需要 users 列表做 user_id → 显示名映射（owner/admin 场景）
      // 只看自己时不拉 users，用 me 自身补一个 entry 即可
      const tasks: [Promise<{ sessions: secApi.SecuritySession[] }>] = [
        secApi.listSessions(targetUser.trim() || undefined),
      ];
      const tasksUsers: [Promise<{ users: secApi.SecurityUser[]; total: number }>?] = canReadAll
        ? [secApi.listUsers()]
        : [];
      const [sessionsResp, usersResp] = await Promise.all([...tasks, ...tasksUsers]);
      setSessions(sessionsResp.sessions);
      if (usersResp) {
        const map: Record<string, secApi.SecurityUser> = {};
        for (const u of usersResp.users) map[u.user_id] = u;
        setUserLookup(map);
      } else {
        setUserLookup({ [me.user.user_id]: me.user });
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [targetUser, canReadAll, me.user]);

  useEffect(() => { load(); }, [load]);

  const revoke = async (sessionId: string) => {
    setErr(null);
    try {
      await secApi.revokeSession(sessionId);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        {canReadAll && (
          <input style={{ ...inputStyle, width: 220 }} placeholder="按 user_id 过滤（可选）" value={targetUser} onChange={(e) => setTargetUser(e.target.value)} />
        )}
        <button className="btn-secondary" onClick={load} disabled={loading}>{loading ? '加载中...' : '刷新'}</button>
      </div>
      {err && <div style={{ padding: '10px 12px', borderRadius: 8, fontSize: 13, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>{err}</div>}
      <div className="card" style={{ overflow: 'auto' }}>
        <table className="data-table" style={{ width: '100%' }}>
          <thead>
            <tr><th>会话</th><th>用户</th><th>IP / UA</th><th>创建</th><th>最近使用</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.session_id} style={{ opacity: s.revoked ? 0.5 : 1 }}>
                <td className="font-mono" style={{ fontSize: 11, color: 'var(--color-text-secondary)' }} title={s.session_id}>
                  {s.session_id.slice(0, 16)}…
                </td>
                <td style={{ fontSize: 13 }} title={`user_id: ${s.user_id}`}>
                  {userLookup[s.user_id]?.display_name || userLookup[s.user_id]?.username || s.user_id}
                </td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.user_agent ?? ''}>
                  {s.ip ?? '—'}{s.user_agent ? ` · ${s.user_agent.slice(0, 40)}` : ''}
                </td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtTime(s.created_at)}</td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtTime(s.last_used_at)}</td>
                <td>
                  {s.revoked ? <Badge tone="err">已撤销</Badge> : s.is_current ? <Badge tone="ok">当前会话</Badge> : s.is_online ? <Badge tone="warn">在线</Badge> : <Badge tone="muted">离线</Badge>}
                </td>
                <td>
                  {!s.revoked && (
                    <button className="btn-secondary btn-sm" onClick={() => revoke(s.session_id)}>
                      {s.is_current ? '退出登录' : '下线'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {sessions.length === 0 && !loading && (
              <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--color-text-tertiary)', padding: 24 }}>暂无会话</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── API Token ──

function TokensSection({ me }: { me: secApi.MeInfo }) {
  const [tokens, setTokens] = useState<secApi.SecurityToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [plaintext, setPlaintext] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const resp = await secApi.listTokens();
      setTokens(resp.tokens);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const act = async (fn: () => Promise<secApi.CreateTokenResponse | void>) => {
    setErr(null);
    try {
      const resp = await fn();
      if (resp && 'token' in resp && resp.token) setPlaintext(resp.token);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const canWrite = me.scopes.includes('security.api_tokens.write') || me.scopes.includes('*');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button className="btn-secondary" onClick={load} disabled={loading}>{loading ? '加载中...' : '刷新'}</button>
        {canWrite && <button className="btn-primary" onClick={() => setShowCreate(true)}>+ 新建 Token</button>}
      </div>
      {err && <div style={{ padding: '10px 12px', borderRadius: 8, fontSize: 13, background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>{err}</div>}

      {plaintext && (
        <div className="card" style={{ padding: 14, border: '1px solid var(--state-warning, #eab308)' }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6, color: 'var(--color-text-primary)' }}>⚠️ Token 明文只显示这一次，请立即复制保存</div>
          <div className="font-mono" style={{ fontSize: 12, padding: '8px 10px', borderRadius: 6, background: 'var(--color-surface-tertiary, rgba(128,128,128,.12))', wordBreak: 'break-all', color: 'var(--color-text-primary)' }}>{plaintext}</div>
          <button className="btn-secondary btn-sm" style={{ marginTop: 8 }} onClick={() => setPlaintext(null)}>我已保存</button>
        </div>
      )}

      <div className="card" style={{ overflow: 'auto' }}>
        <table className="data-table" style={{ width: '100%' }}>
          <thead>
            <tr><th>名称</th><th>Scopes</th><th>创建</th><th>过期</th><th>最近使用</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            {tokens.map((t) => (
              <tr key={t.token_id} style={{ opacity: t.revoked ? 0.5 : 1 }}>
                <td>
                  <div style={{ fontSize: 13, color: 'var(--color-text-primary)' }}>{t.name}</div>
                  <div className="font-mono" style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>{t.token_id.slice(0, 12)}…</div>
                </td>
                <td>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, maxWidth: 300 }}>
                    {t.scopes.length === 0 ? <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>继承用户</span> : t.scopes.map((s) => <Badge key={s} tone="muted">{s}</Badge>)}
                  </div>
                </td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtTime(t.created_at)}</td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtTime(t.expires_at)}</td>
                <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtTime(t.last_used_at)}</td>
                <td>{t.revoked ? <Badge tone="err">已撤销</Badge> : <Badge tone="ok">有效</Badge>}</td>
                <td>
                  {!t.revoked && (
                    <div style={{ display: 'flex', gap: 6 }}>
                      {canWrite && (
                        <button className="btn-secondary btn-sm" onClick={() => act(() => secApi.rotateToken(t.token_id))}>轮换</button>
                      )}
                      <button className="btn-secondary btn-sm" onClick={() => {
                        if (window.confirm(`确认撤销 Token「${t.name}」？使用它的客户端会立即失效。`)) act(() => secApi.revokeToken(t.token_id));
                      }}>撤销</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {tokens.length === 0 && !loading && (
              <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--color-text-tertiary)', padding: 24 }}>暂无 Token</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {showCreate && (
        <CreateTokenModal
          scopes={me.scopes.filter((s) => s !== '*')}
          onClose={() => setShowCreate(false)}
          onCreated={(resp) => {
            setShowCreate(false);
            if (resp.token) setPlaintext(resp.token);
            load();
          }}
        />
      )}
    </div>
  );
}

function CreateTokenModal({ scopes, onClose, onCreated }: { scopes: string[]; onClose: () => void; onCreated: (r: secApi.CreateTokenResponse) => void }) {
  const [name, setName] = useState('');
  const [expiresInDays, setExpiresInDays] = useState<30 | 90 | 365>(30);
  const [selected, setSelected] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const toggle = (s: string) => setSelected((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      const resp = await secApi.createToken({ name, scopes: selected, expires_in_days: expiresInDays });
      onCreated(resp);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="新建 API Token" onClose={onClose}>
      <Field label="名称 *"><input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} autoFocus /></Field>
      <Field label="有效期">
        <select style={inputStyle} value={expiresInDays} onChange={(e) => setExpiresInDays(Number(e.target.value) as 30 | 90 | 365)}>
          <option value={30}>30 天</option>
          <option value={90}>90 天</option>
          <option value={365}>365 天</option>
        </select>
      </Field>
      <Field label="Scopes（不选 = 继承你的全部权限）">
        <div style={{ maxHeight: 180, overflow: 'auto', display: 'flex', flexWrap: 'wrap', gap: 6, padding: '8px', borderRadius: 6, border: '1px solid var(--color-border, rgba(128,128,128,.3))' }}>
          {scopes.map((s) => (
            <label key={s} className="font-mono" style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, cursor: 'pointer', color: 'var(--color-text-primary)' }}>
              <input type="checkbox" checked={selected.includes(s)} onChange={() => toggle(s)} />
              {s}
            </label>
          ))}
        </div>
      </Field>
      {err && <div style={{ fontSize: 12, color: 'var(--state-error)', marginBottom: 10 }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button className="btn-secondary" onClick={onClose}>取消</button>
        <button className="btn-primary" onClick={submit} disabled={busy || !name}>{busy ? '创建中...' : '创建'}</button>
      </div>
    </Modal>
  );
}

// ── 页面主组件 ──

export function SecurityPage({ section }: { section: SecuritySection }) {
  // S16 完整版：复用全局 AuthContext，不再维护本地的 authState/login card
  // ——避免切页面时重复触发 me 探测导致 LoginCard 闪现
  const { me, state, refresh, logout } = useAuth();

  const meta = SECTION_META[section];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* 页头 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 600, color: 'var(--color-text-primary)' }}>安全管理 · {meta.title}</div>
          <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', marginTop: 4 }}>{meta.desc}</div>
        </div>
        {me && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
              {me.user.display_name || me.user.username}
              {me.roles.length > 0 && `（${me.roles.join(', ')}）`}
            </span>
            <button
              className="btn-secondary btn-sm"
              onClick={() => logout().catch(() => { /* 全局 AuthContext 已处理 */ })}
            >
              退出登录
            </button>
          </div>
        )}
      </div>

      {state === 'checking' && <div style={{ color: 'var(--color-text-tertiary)', fontSize: 13 }}>正在检查登录状态...</div>}
      {state === 'anon' && <div style={{ color: 'var(--state-error)', fontSize: 13 }}>会话已过期，请重新登录后继续操作。</div>}
      {state === 'ok' && me && (
        <>
          {section === 'users' && <UsersSection me={me} refreshMe={refresh} />}
          {section === 'roles' && <RolesSection />}
          {section === 'sessions' && <SessionsSection me={me} />}
          {section === 'tokens' && <TokensSection me={me} />}
        </>
      )}
    </div>
  );
}
