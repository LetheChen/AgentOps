// web/src/lib/markdown.ts
// 共享 markdown 渲染（配置与 SuperAgentPage 一致）
// - html: false / linkify: false（防 XSS：源文本中的 HTML 一律转义，不生成链接）
// - breaks: true（单换行也换行，符合聊天/评论输入习惯）
// - 启用 table + strikethrough（表格与删除线）
// 样式配套全局类 .md-content（styles.css），@mention 高亮由调用方后处理

import MarkdownIt from 'markdown-it';

const md = new MarkdownIt({
  html: false,
  linkify: false,
  breaks: true,
  typographer: false,
});

md.enable(['table', 'strikethrough']);

/** 将 markdown 文本渲染为安全 HTML（markdown-it 已禁 html/link，输出可直接注入） */
export function renderMarkdown(text: string): string {
  return md.render(text);
}

// 后端 task 域 @mention 白名单 agent（server.py _MENTION_RE + whitelist）
export const MENTION_AGENTS = [
  { id: 'coding_agent', label: 'coding_agent', desc: '编码执行' },
  { id: 'task_planner', label: 'task_planner', desc: '任务规划' },
  { id: 'quality_inspector', label: 'quality_inspector', desc: '质量检查' },
  { id: 'task_conductor', label: 'task_conductor', desc: '调度巡检' },
];

/** markdown 渲染 + @mention 高亮（评论正文等场景共用） */
export function renderMarkdownWithMentions(text: string): string {
  const re = new RegExp(`@(${MENTION_AGENTS.map((a) => a.id).join('|')})\\b`, 'g');
  return renderMarkdown(text).replace(re, '<span class="md-mention">@$1</span>');
}

/** 提取所有 `` 块，rest = 去掉所有 think 后的剩余文本。
 *  场景：活动流的 agent_text 是多次 progress 事件拼接而成，可能含多对 ``，
 *  旧版 parseThink 仅匹配首对，剩余字面量 `` 无法折叠。
 */
export function splitThinkBlocks(body: string): { thinks: string[]; rest: string } {
  const re = /<think>([\s\S]*?)<\/think>/g;
  const thinks: string[] = [];
  let rest = '';
  let lastIdx = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    rest += body.slice(lastIdx, m.index);
    thinks.push(m[1].trim());
    lastIdx = m.index + m[0].length;
  }
  rest += body.slice(lastIdx);
  return { thinks, rest: rest.trim() };
}
