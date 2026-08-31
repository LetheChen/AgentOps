import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    // 开发代理：/api 全部转发到后端 1987，前端请求变同源。
    // 目的：SSE（EventSource）无法带 Authorization 头，靠 login Set-Cookie
    // 的 agentops_session cookie 回退鉴权；跨源（5173→1987）时浏览器既不存
    // 该 cookie（fetch 默认 same-origin 凭证）也不随 EventSource 发送 → 401
    // → 会话回复无法实时推送（chat 卡"正在思考"、DAG/tips 流无内容）。
    // 代理后全部同源，cookie 天然生效。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:1987',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      input: {
        main: 'index.html',
        a2uiDemo: 'a2ui-demo.html',
        surfaceStateDemo: 'surface-state-demo.html',
        dagVizDemo: 'dag-visualization-demo.html',
      },
    },
  },
});
