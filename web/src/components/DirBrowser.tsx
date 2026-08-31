/**
 * DirBrowser — 后端驱动的目录浏览器（解决浏览器无法获取完整本地路径的问题）
 *
 * 功能：
 *   - 列出指定路径下的子目录（双击进入，单击选中）
 *   - 返回上一级（parent 链接）
 *   - Windows 盘符切换栏（C:\ D:\ ...）
 *   - 当前路径输入框（可手动编辑）
 *   - 确认按钮（选中目录后可确认）
 *
 * 用法：
 *   <DirBrowser
 *     onSelect={(path, name) => { ... }}
 *     onConfirm={(path) => { ... }}
 *   />
 */
import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '../lib/api';

interface DirEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

interface BrowseResult {
  current: string;
  parent: string | null;
  entries: DirEntry[];
  drives: string[];
}

export function DirBrowser({
  initialPath,
  onSelect,
  onConfirm,
  confirmLabel = '确认选择此目录',
}: {
  initialPath?: string;
  /** 单击选中目录时回调 */
  onSelect?: (path: string, name: string) => void;
  /** 确认按钮回调 */
  onConfirm?: (path: string) => void;
  confirmLabel?: string;
}) {
  const [current, setCurrent] = useState<string>('');
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [drives, setDrives] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState<string>('');
  const [manualInput, setManualInput] = useState<string>('');

  const loadDir = useCallback(async (path?: string) => {
    setLoading(true);
    setError(null);
    setSelected(null);
    setSelectedName('');
    try {
      const resp: BrowseResult = await apiClient.browseDirs(path);
      setCurrent(resp.current);
      setParent(resp.parent);
      setEntries(resp.entries);
      setDrives(resp.drives || []);
      setManualInput(resp.current);
      onSelect?.(resp.current, resp.current.replace(/\\/g, '/').split('/').filter(Boolean).pop() || resp.current);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [onSelect]);

  useEffect(() => {
    loadDir(initialPath);
  }, [initialPath, loadDir]);

  const handleEntryClick = (entry: DirEntry) => {
    setSelected(entry.path);
    setSelectedName(entry.name);
    onSelect?.(entry.path, entry.name);
  };

  const handleEntryDoubleClick = (entry: DirEntry) => {
    loadDir(entry.path);
  };

  const handleParent = () => {
    if (parent) loadDir(parent);
  };

  const handleDrive = (drive: string) => {
    loadDir(drive);
  };

  const handleManualSubmit = () => {
    if (manualInput.trim()) {
      loadDir(manualInput.trim());
    }
  };

  const handleUseCurrent = () => {
    if (current) {
      setSelected(current);
      setSelectedName(current.replace(/\\/g, '/').split('/').filter(Boolean).pop() || current);
      onConfirm?.(current);
    }
  };

  const handleConfirm = () => {
    const target = selected || current;
    if (target) {
      onConfirm?.(target);
    }
  };

  return (
    <div className="dir-browser">
      {/* 盘符切换栏（Windows） */}
      {drives.length > 0 && (
        <div className="dir-browser-drives">
          {drives.map(d => (
            <button
              key={d}
              className={`dir-browser-drive ${current.toUpperCase().startsWith(d.toUpperCase().replace('\\', '')) ? 'active' : ''}`}
              onClick={() => handleDrive(d)}
              title={`切换到 ${d}`}
            >
              {d.replace('\\', '')}
            </button>
          ))}
        </div>
      )}

      {/* 路径栏：返回上级 + 当前路径输入框 */}
      <div className="dir-browser-pathbar">
        <button
          className="dir-browser-up"
          onClick={handleParent}
          disabled={!parent}
          title="返回上一级"
        >
          ↑
        </button>
        <input
          className="dir-browser-path-input"
          value={manualInput}
          onChange={(e) => setManualInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') handleManualSubmit(); }}
          placeholder="输入路径后回车跳转"
        />
        <button
          className="dir-browser-go"
          onClick={handleManualSubmit}
          title="跳转到输入路径"
        >
          跳转
        </button>
      </div>

      {/* 当前路径显示 */}
      <div className="dir-browser-current">
        <span className="dir-browser-current-label">当前目录</span>
        <span className="dir-browser-current-path">{current}</span>
      </div>

      {/* 目录列表 */}
      <div className="dir-browser-list">
        {loading ? (
          <div className="dir-browser-empty">加载中…</div>
        ) : error ? (
          <div className="dir-browser-error">{error}</div>
        ) : entries.length === 0 ? (
          <div className="dir-browser-empty">此目录下没有子目录</div>
        ) : (
          entries.map(entry => (
            <div
              key={entry.path}
              className={`dir-browser-entry ${selected === entry.path ? 'selected' : ''}`}
              onClick={() => handleEntryClick(entry)}
              onDoubleClick={() => handleEntryDoubleClick(entry)}
              title={`单击选中，双击进入：${entry.path}`}
            >
              <span className="dir-browser-entry-icon">📁</span>
              <span className="dir-browser-entry-name">{entry.name}</span>
              {selected === entry.path && (
                <span className="dir-browser-entry-check">✓</span>
              )}
            </div>
          ))
        )}
      </div>

      {/* 选中信息 + 确认按钮 */}
      <div className="dir-browser-footer">
        <div className="dir-browser-selected">
          {selected ? (
            <>
              <span className="dir-browser-selected-label">已选</span>
              <span className="dir-browser-selected-name">{selectedName}</span>
            </>
          ) : current ? (
            <>
              <span className="dir-browser-selected-label">当前</span>
              <span className="dir-browser-selected-name">
                {current.replace(/\\/g, '/').split('/').filter(Boolean).pop() || current}
              </span>
            </>
          ) : (
            <span className="dir-browser-selected-empty">未选择</span>
          )}
        </div>
        <div className="dir-browser-actions">
          <button
            className="btn-secondary dir-browser-use-current"
            onClick={handleUseCurrent}
            disabled={!current}
          >
            用当前目录
          </button>
          <button
            className="btn-primary dir-browser-confirm"
            onClick={handleConfirm}
            disabled={!selected && !current}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
