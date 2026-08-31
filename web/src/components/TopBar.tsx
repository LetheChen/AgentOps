import { useState, useEffect } from 'react';

interface TopBarProps {
  title: string;
  /** 侧边栏收起/展开回调（非工作台/协作页也能手动折叠） */
  onToggleSidebar?: () => void;
  sidebarOpen?: boolean;
}

export function TopBar({ title, onToggleSidebar, sidebarOpen }: TopBarProps) {
  const [clock, setClock] = useState('');

  useEffect(() => {
    const updateClock = () => {
      const now = new Date();
      const h = String(now.getHours()).padStart(2, '0');
      const m = String(now.getMinutes()).padStart(2, '0');
      const s = String(now.getSeconds()).padStart(2, '0');
      setClock(`${h}:${m}:${s}`);
    };
    updateClock();
    const interval = setInterval(updateClock, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <header className="app-topbar chat-topbar">
      {/* Logo 打通顶部栏（与 ChatTopBar 一致，避免非工作台页 Logo 消失） */}
      <div className="nav-logo">
        <div className="nav-logo-icon" />
        <span className="nav-logo-text">AgentOps</span>
      </div>
      {onToggleSidebar && (
        <button
          onClick={onToggleSidebar}
          title={sidebarOpen ? '收起菜单' : '展开菜单'}
          className="nav-drawer-toggle-btn"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {sidebarOpen ? <polyline points="15 18 9 12 15 6" /> : <line x1="3" y1="12" x2="21" y2="12" />}
            {sidebarOpen ? null : <><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="18" x2="21" y2="18" /></>}
          </svg>
        </button>
      )}
      <span className="app-topbar-title">{title}</span>
      <div className="app-topbar-right">
        <div className="topbar-status">
          <div className="topbar-status-dot" />
          <span className="topbar-status-text">系统正常</span>
        </div>
        <div className="topbar-separator" />
        <span className="topbar-clock">{clock}</span>
      </div>
    </header>
  );
}

/**
 * ChatTopBar — 全宽顶部栏，Logo 打通，含 sidebar toggle + 对话记录收起按钮
 */
export function ChatTopBar({ title, onToggleSidebar, sidebarOpen, onToggleChatDrawer, chatDrawerOpen }: {
  title: string;
  onToggleSidebar?: () => void;
  sidebarOpen?: boolean;
  onToggleChatDrawer?: () => void;
  chatDrawerOpen?: boolean;
}) {
  const [clock, setClock] = useState('');

  useEffect(() => {
    const updateClock = () => {
      const now = new Date();
      const h = String(now.getHours()).padStart(2, '0');
      const m = String(now.getMinutes()).padStart(2, '0');
      const s = String(now.getSeconds()).padStart(2, '0');
      setClock(`${h}:${m}:${s}`);
    };
    updateClock();
    const interval = setInterval(updateClock, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <header className="app-topbar chat-topbar">
      {/* Logo 打通顶部栏 */}
      <div className="nav-logo">
        <div className="nav-logo-icon" />
        <span className="nav-logo-text">AgentOps</span>
      </div>
      {/* 侧边栏收起/展开按钮 */}
      <button
        onClick={onToggleSidebar}
        title={sidebarOpen ? '收起菜单' : '展开菜单'}
        className="nav-drawer-toggle-btn"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {sidebarOpen ? <polyline points="15 18 9 12 15 6" /> : <line x1="3" y1="12" x2="21" y2="12" />}
          {sidebarOpen ? null : <><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="18" x2="21" y2="18" /></>}
        </svg>
      </button>
      <span className="app-topbar-title">{title}</span>
      <div className="app-topbar-right">
        {/* 对话记录收起/展开按钮 */}
        <button
          onClick={onToggleChatDrawer}
          title={chatDrawerOpen ? '收起对话记录' : '展开对话记录'}
          className="nav-drawer-toggle-btn"
        >
          {chatDrawerOpen ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18" /><path d="M6 6l12 12" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
            </svg>
          )}
        </button>
        <div className="topbar-separator" />
        <div className="topbar-status">
          <div className="topbar-status-dot" />
          <span className="topbar-status-text">系统正常</span>
        </div>
        <div className="topbar-separator" />
        <span className="topbar-clock">{clock}</span>
      </div>
    </header>
  );
}