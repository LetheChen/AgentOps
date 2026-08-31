import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import * as secApi from '../lib/securityApi';
import { AuthError } from '../lib/securityApi';
import { UNAUTHORIZED_EVENT } from '../lib/authFetch';

/**
 * 全局认证上下文（S16 完整版）。
 *
 * - App 级门禁：checking → anon(登录页) / must-reset(强制改密页) / ok(应用)
 * - 监听 authFetch 的 401 全局事件，会话过期自动回登录页
 * - dev 绕过模式（后端 .auth-disabled）：/api/auth/me 匿名可访问（token_kind=bypass），
 *   无 token 也视为已认证，开发环境零打扰
 */

export type AuthState = 'checking' | 'anon' | 'must-reset' | 'ok';

interface AuthContextValue {
  state: AuthState;
  me: secApi.MeInfo | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  changePassword: (oldPw: string, newPw: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return ctx;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>('checking');
  const [me, setMe] = useState<secApi.MeInfo | null>(null);

  const refresh = useCallback(async () => {
    try {
      const info = await secApi.getMe();
      setMe(info);
      setState(info.must_reset_password ? 'must-reset' : 'ok');
    } catch (e) {
      if (e instanceof AuthError) {
        // 401：token 真的失效（带 token 还 401），清掉并跳登录页
        secApi.setStoredToken(null);
        setMe(null);
        setState('anon');
      }
      // 其它错误（403、网络抖动、5xx、后端不可达）：保持当前态不变。
      // 原因：这些通常不需要用户重新登录；如果确实不可用，AppShell 内的 fetch
      // 会带 token 返回 401，由 authFetch 派发 UNAUTHORIZED_EVENT 走上面的 401 分支。
      // 避免"切个页面就跳登录卡"的反馈循环。
    }
  }, []);

  useEffect(() => {
    refresh();
    const onUnauthorized = () => {
      secApi.setStoredToken(null);
      setMe(null);
      setState('anon');
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    const resp = await secApi.login(username, password);
    secApi.setStoredToken(resp.token);
    await refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    try { await secApi.logout(); } catch { /* 已过期/网络失败都继续本地清理 */ }
    secApi.setStoredToken(null);
    setMe(null);
    setState('anon');
  }, []);

  const changePassword = useCallback(async (oldPw: string, newPw: string) => {
    await secApi.changePassword(oldPw, newPw);
    await refresh();
  }, [refresh]);

  return (
    <AuthContext.Provider value={{ state, me, login, logout, refresh, changePassword }}>
      {children}
    </AuthContext.Provider>
  );
}

// ── 全屏登录页 ──

export function LoginView() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      await login(username, password);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div className="card card-elevated" style={{ width: 400, padding: 28 }}>
        <div style={{ fontSize: 22, fontWeight: 600, marginBottom: 6, color: 'var(--color-text-primary)' }}>AgentOps</div>
        <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', marginBottom: 20 }}>请登录以继续</div>
        <label style={{ display: 'block', marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 4 }}>用户名</div>
          <input
            className="font-mono"
            style={{ width: '100%', padding: '8px 10px', borderRadius: 8, border: '1px solid var(--color-border, rgba(128,128,128,.3))', background: 'var(--color-surface-secondary, transparent)', color: 'var(--color-text-primary)', fontSize: 13, boxSizing: 'border-box' }}
            value={username} onChange={(e) => setUsername(e.target.value)} autoFocus
          />
        </label>
        <label style={{ display: 'block', marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 4 }}>密码</div>
          <input
            type="password"
            style={{ width: '100%', padding: '8px 10px', borderRadius: 8, border: '1px solid var(--color-border, rgba(128,128,128,.3))', background: 'var(--color-surface-secondary, transparent)', color: 'var(--color-text-primary)', fontSize: 13, boxSizing: 'border-box' }}
            value={password} onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !busy) submit(); }}
          />
        </label>
        {err && <div style={{ fontSize: 12, color: 'var(--state-error)', marginBottom: 10 }}>{err}</div>}
        <button className="btn-primary" style={{ width: '100%' }} onClick={submit} disabled={busy || !username || !password}>
          {busy ? '登录中...' : '登录'}
        </button>
      </div>
    </div>
  );
}

// ── 全屏强制改密页 ──

export function MustResetView() {
  const { changePassword, logout } = useAuth();
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
      await changePassword(oldPw, newPw);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const inputStyle: React.CSSProperties = {
    width: '100%', padding: '8px 10px', borderRadius: 8,
    border: '1px solid var(--color-border, rgba(128,128,128,.3))',
    background: 'var(--color-surface-secondary, transparent)',
    color: 'var(--color-text-primary)', fontSize: 13, boxSizing: 'border-box',
  };
  const field = (label: string, value: string, set: (v: string) => void) => (
    <label style={{ display: 'block', marginBottom: 12 }}>
      <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 4 }}>{label}</div>
      <input type="password" style={inputStyle} value={value} onChange={(e) => set(e.target.value)} />
    </label>
  );

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div className="card card-elevated" style={{ width: 400, padding: 28 }}>
        <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 6, color: 'var(--color-text-primary)' }}>请先修改初始密码</div>
        <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', marginBottom: 18 }}>
          安全策略要求：首次登录（或密码被重置）后必须改密才能继续使用
        </div>
        {field('当前密码', oldPw, setOldPw)}
        {field('新密码', newPw, setNewPw)}
        {field('确认新密码', confirm, setConfirm)}
        {err && <div style={{ fontSize: 12, color: 'var(--state-error)', marginBottom: 10 }}>{err}</div>}
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn-primary" style={{ flex: 1 }} onClick={submit} disabled={busy || !oldPw || !newPw}>
            {busy ? '提交中...' : '修改密码'}
          </button>
          <button className="btn-secondary" onClick={logout}>退出</button>
        </div>
      </div>
    </div>
  );
}
