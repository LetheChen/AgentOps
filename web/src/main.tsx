import { StrictMode } from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { installAuthFetch } from './lib/authFetch';
import './styles.css';
import './styles/a2ui.css';

// S16：全局 fetch 包装必须先于任何 API 调用安装（含 Onboarding 探测）
installAuthFetch();

// Vite 5.4.21 dev server bug: __vite__updateStyle 注入新 CSS 模块时静默丢弃。
// prod build 走 esbuild 打包 CSS 无此问题。
// 临时绕过：用 ?raw import 拿原始 CSS 字符串，手动创建 <style> 注入。
import dagV99Raw from './styles/dag-v99.css?raw';
if (typeof document !== 'undefined' && typeof dagV99Raw === 'string') {
  const style = document.createElement('style');
  style.setAttribute('data-source', 'dag-v99.css');
  style.textContent = dagV99Raw;
  document.head.appendChild(style);
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
