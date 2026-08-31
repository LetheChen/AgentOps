/**
 * 安全模块 API 客户端（S16 前端切片）。
 *
 * - session token 存 localStorage（key: agentops_token），所有请求带 Authorization 头
 * - 401 时抛 AuthError，页面据此显示登录卡
 * - 全局 AuthContext / 路由守卫属 S16 完整范围，这里先做模块内自洽的登录态
 */

import { API_BASE_URL } from './api';

const TOKEN_KEY = 'agentops_token';

export class AuthError extends Error {
  constructor(message = '未登录或会话已过期') {
    super(message);
    this.name = 'AuthError';
  }
}

export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setStoredToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  const token = getStoredToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  if (response.status === 401) throw new AuthError();
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      // FastAPI HTTPException detail 可能是 string 或 {error, message}
      detail = typeof body.detail === 'string' ? body.detail : body.detail?.message ?? JSON.stringify(body.detail);
    } catch { /* 保留 statusText */ }
    throw new Error(detail || `请求失败 ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

// ── 类型 ──

export interface SecurityUser {
  user_id: string;
  username: string;
  display_name: string;
  email: string;
  roles: string[];
  disabled: boolean;
  locked: boolean;
  is_online: boolean;
  last_seen_at: string | null;
  created_at: string;
  must_reset_password?: boolean;
}

export interface SecurityRole {
  role_id: string;
  name: string;
  description: string;
  is_builtin: boolean;
  is_assignable: boolean;
  permissions: string[];
  permission_count: number;
}

export interface SecurityPermission {
  perm_id: string;
  resource: string;
  action: string;
  description: string;
  roles: string[];
}

export interface SecuritySession {
  session_id: string;
  user_id: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  ip: string | null;
  user_agent: string | null;
  revoked_at: string | null;
  is_current: boolean;
  is_online: boolean;
  revoked: boolean;
}

export interface SecurityToken {
  token_id: string;
  user_id: string;
  name: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  revoked: boolean;
}

export interface MeInfo {
  user: SecurityUser;
  roles: string[];
  scopes: string[];
  must_reset_password: boolean;
  current_session_id: string | null;
  token_kind: string;
}

export interface LoginResponse {
  token: string;
  user: SecurityUser;
  scopes: string[];
  must_reset_password: boolean;
  current_session_id: string;
}

// ── auth ──

export async function login(username: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
}

export async function getMe(): Promise<MeInfo> {
  return request<MeInfo>('/api/auth/me');
}

export async function changePassword(oldPassword: string, newPassword: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/api/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
}

export async function logout(): Promise<{ revoked: boolean }> {
  return request<{ revoked: boolean }>('/api/auth/logout', { method: 'POST' });
}

// ── users ──

export async function listUsers(): Promise<{ users: SecurityUser[]; total: number }> {
  return request('/api/security/users');
}

export interface CreateUserPayload {
  username: string;
  password: string;
  display_name?: string;
  email?: string;
  role_ids?: string[];
}

export async function createUser(payload: CreateUserPayload): Promise<SecurityUser> {
  return request('/api/security/users', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateUser(userId: string, patch: { display_name?: string; email?: string; disabled?: boolean }): Promise<SecurityUser> {
  return request(`/api/security/users/${encodeURIComponent(userId)}`, { method: 'PATCH', body: JSON.stringify(patch) });
}

export async function deleteUser(userId: string): Promise<void> {
  return request(`/api/security/users/${encodeURIComponent(userId)}`, { method: 'DELETE' });
}

export async function setUserLocked(userId: string, locked: boolean): Promise<void> {
  return request(`/api/security/users/${encodeURIComponent(userId)}/${locked ? 'lock' : 'unlock'}`, { method: 'POST' });
}

export async function resetUserPassword(userId: string, newPassword: string): Promise<void> {
  return request(`/api/security/users/${encodeURIComponent(userId)}/password`, {
    method: 'POST',
    body: JSON.stringify({ new_password: newPassword }),
  });
}

export async function bindUserRole(userId: string, roleId: string): Promise<void> {
  return request(`/api/security/users/${encodeURIComponent(userId)}/roles`, { method: 'POST', body: JSON.stringify({ role_id: roleId }) });
}

export async function unbindUserRole(userId: string, roleId: string): Promise<void> {
  return request(`/api/security/users/${encodeURIComponent(userId)}/roles/${encodeURIComponent(roleId)}`, { method: 'DELETE' });
}

// ── roles / permissions ──

export async function listRoles(): Promise<{ roles: SecurityRole[]; total: number }> {
  return request('/api/security/roles');
}

export async function listPermissions(): Promise<{ permissions: SecurityPermission[]; total: number }> {
  return request('/api/security/permissions');
}

// ── sessions ──

export async function listSessions(userId?: string): Promise<{ sessions: SecuritySession[]; total: number }> {
  const qs = userId ? `?user_id=${encodeURIComponent(userId)}` : '';
  return request(`/api/security/sessions${qs}`);
}

export async function revokeSession(sessionId: string): Promise<void> {
  return request(`/api/security/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
}

// ── api tokens ──

export async function listTokens(userId?: string): Promise<{ tokens: SecurityToken[]; total: number }> {
  const qs = userId ? `?user_id=${encodeURIComponent(userId)}` : '';
  return request(`/api/security/api-tokens${qs}`);
}

export interface CreateTokenPayload {
  name: string;
  scopes?: string[];
  expires_in_days: 30 | 90 | 365;
  user_id?: string;
}

export interface CreateTokenResponse extends SecurityToken {
  /** 明文 token 只在创建/轮换响应里出现一次 */
  token?: string;
}

export async function createToken(payload: CreateTokenPayload): Promise<CreateTokenResponse> {
  return request('/api/security/api-tokens', { method: 'POST', body: JSON.stringify(payload) });
}

export async function revokeToken(tokenId: string): Promise<void> {
  return request(`/api/security/api-tokens/${encodeURIComponent(tokenId)}`, { method: 'DELETE' });
}

export async function rotateToken(tokenId: string, name?: string): Promise<CreateTokenResponse> {
  return request(`/api/security/api-tokens/${encodeURIComponent(tokenId)}/rotate`, {
    method: 'POST',
    body: JSON.stringify({ name: name ?? null }),
  });
}
