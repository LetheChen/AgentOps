import { useState } from 'react';
import { renderMarkdown } from '../../lib/markdown';

interface VaultFilePreviewProps {
  content: string;
  format: string;
  extractedBy?: string;
  frontmatter?: Record<string, unknown>;
  fileName?: string;
}

/** 将 frontmatter 对象渲染为可读的 key: value 文本 */
function renderFrontmatter(fm: Record<string, unknown>): string {
  return Object.entries(fm)
    .map(([k, v]) => {
      const value = typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v);
      return `${k}: ${value}`;
    })
    .join('\n');
}

/** 判断是否为 Markdown 格式 */
function isMarkdown(format: string): boolean {
  const f = (format || '').toLowerCase();
  return f === 'markdown' || f === 'md';
}

export function VaultFilePreview({ content, format, extractedBy, frontmatter, fileName }: VaultFilePreviewProps) {
  const [showFm, setShowFm] = useState(true);
  // md 源文/渲染切换：默认渲染
  const [renderMd, setRenderMd] = useState(true);
  const md = isMarkdown(format);
  const hasFm = !!frontmatter && Object.keys(frontmatter).length > 0;

  return (
    <div className="kh-vault-preview-body">
      {fileName && (
        <div className="kh-vault-breadcrumb">
          <span className="kh-vault-breadcrumb-label">路径</span>
          <span className="kh-vault-breadcrumb-sep">/</span>
          <span className="kh-vault-breadcrumb-path">{fileName}</span>
        </div>
      )}

      {hasFm && (
        <div className="kh-frontmatter">
          <button
            type="button"
            className="kh-frontmatter-toggle"
            onClick={() => setShowFm(!showFm)}
            aria-expanded={showFm}
          >
            <svg
              viewBox="0 0 16 16"
              width="10"
              height="10"
              style={{ transform: showFm ? 'rotate(90deg)' : 'none' }}
            >
              <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.5" />
            </svg>
            <span>Frontmatter 元数据</span>
          </button>
          {showFm && <pre className="kh-frontmatter-pre">{renderFrontmatter(frontmatter!)}</pre>}
        </div>
      )}

      {!md && (
        <div className="kh-format-info">
          <span>
            格式: <code>{format || '未知'}</code>
          </span>
          {extractedBy && (
            <span>
              抽取方式: <code>{extractedBy}</code>
            </span>
          )}
        </div>
      )}

      {md && (
        <div className="kh-preview-toolbar">
          <button
            type="button"
            className={`kh-preview-tool ${renderMd ? 'active' : ''}`}
            onClick={() => setRenderMd(true)}
            aria-pressed={renderMd}
          >
            渲染
          </button>
          <button
            type="button"
            className={`kh-preview-tool ${!renderMd ? 'active' : ''}`}
            onClick={() => setRenderMd(false)}
            aria-pressed={!renderMd}
          >
            源文本
          </button>
        </div>
      )}

      {md && renderMd ? (
        <div
          className="md-content kh-preview-md-rendered"
          // markdown-it 已配置 html:false/linkify:false,输出安全;此处注入来自知识库 md 源文件
          dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
        />
      ) : (
        <pre className="kh-preview-md">{content}</pre>
      )}
    </div>
  );
}
