/**
 * 全局 fetch 包装（S16）：单点给所有 API 请求注入 Authorization 头，
 * 并在 401 时派发全局事件供 AuthContext 感知登出。
 *
 * 设计约束：
 * - 124+ 个 fetch 调用点（lib/api.ts / taskApi.ts / 各组件）零改动
 * - EventSource（SSE）不带 header，靠后端 cookie 回退（login Set-Cookie agentops_session）
 * - /api/auth/login 的 401 是"密码错误"，不能触发全局登出，豁免
 */

import { API_BASE_URL } from './api';
import { getStoredToken } from './securityApi';

export const UNAUTHORIZED_EVENT = 'agentops:unauthorized';

// 登录接口的 401 = 凭证错误，不是会话过期
const AUTH_EXEMPT_PATHS = ['/api/auth/login'];

let installed = false;

export function installAuthFetch(): void {
  if (installed || typeof window === 'undefined') return;
  installed = true;

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const isApiCall = url.startsWith(API_BASE_URL) || url.startsWith('/api/');

    let nextInit = init;
    if (isApiCall) {
      const path = url.startsWith(API_BASE_URL) ? url.slice(API_BASE_URL.length) : url;
      const exempt = AUTH_EXEMPT_PATHS.some((p) => path.startsWith(p));
      const token = getStoredToken();
      if (!exempt && token) {
        const headers = new Headers(init?.headers);
        if (!headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);
        nextInit = { ...init, headers };
      }
    }

    const response = await originalFetch(input, nextInit);

    if (isApiCall && response.status === 401) {
      const path = url.startsWith(API_BASE_URL) ? url.slice(API_BASE_URL.length) : url;
      // 只在"带了 token 还 401"时触发登出（token 真的过期/失效）；
      // 无 token 的探测（如 AuthProvider 启动时 me）401 属于合法探测，不应触发反馈循环。
      const sentAuth = !!(nextInit?.headers && new Headers(nextInit.headers).has('Authorization'));
      if (sentAuth && !AUTH_EXEMPT_PATHS.some((p) => path.startsWith(p))) {
        window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
      }
    }
    return response;
  };
}
