/**
 * Handoff summary 兜底生成器（前端）
 *
 * 设计原则（v2 采纳 PRD A9）：
 * 1. M1 阶段：agent system_prompt 未输出 handoff_summary，前端从 payload 派生
 * 2. M2 阶段：engine 已 enrich payload.summary（agent emit 优先），前端只做 fallback
 * 3. 兜底优先级：payload.summary > payload.content 截断 > port 名
 *
 * 注意：summary 在 engine 层也会兜底一次（[workflow/engine.py](file:///e:/Project/AgentOps/workflow/engine.py)），
 *       前端做第二层兜底，应对历史 run（engine 未 enrich 老数据）+ SSE 丢包场景。
 */

export interface HandoffSummaryInput {
  from_role?: string;
  to_role?: string;
  port?: string;
  payload_size?: number;
  payload?: unknown;
  /** engine 已 enrich 的 summary（优先使用） */
  summary?: string;
}

export function genHandoffSummary(input: HandoffSummaryInput): string {
  // 1. 优先使用 engine / agent 提供的 summary
  if (input.summary && input.summary.trim()) {
    return input.summary.slice(0, 200);
  }

  const from = input.from_role || '上游';
  const to = input.to_role || '下游';
  const port = input.port || '';

  // 2. payload 是 dict 且有 summary 字段
  if (input.payload && typeof input.payload === 'object') {
    const p = input.payload as Record<string, unknown>;
    if (typeof p.summary === 'string' && p.summary.trim()) {
      return p.summary.slice(0, 200);
    }
    // 提取关键字段名作为 hint
    const keys = Object.keys(p).filter((k) => k !== 'content' && k !== 'raw');
    if (keys.length > 0) {
      return `${from} 传递 ${port}（含 ${keys.slice(0, 3).join('、')}）给 ${to}`;
    }
  }

  // 3. payload.content 截断
  if (input.payload && typeof input.payload === 'object') {
    const p = input.payload as Record<string, unknown>;
    const content = typeof p.content === 'string' ? p.content : '';
    if (content) {
      return content.slice(0, 120);
    }
  }

  // 4. 兜底：基于 from_role + port
  return `${from} 传递 ${port} 给 ${to}`;
}