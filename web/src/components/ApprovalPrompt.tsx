import { useState, useCallback } from 'react';
import { apiClient } from '../lib/api';

/**
 * 审批请求弹窗（deepseek-harness 对齐：allowed-once 语义）。
 *
 * SSE approval.requested 事件触发显示；用户只能给出两个决定：
 * - 允许本次（allowed-once）：只放行被问的那一次工具调用，不改变会话权限级别
 * - 拒绝（rejected）：本次不执行
 * 服务端超时/无订阅者自动 unavailable（fail closed），弹窗随 approval.decided 消失。
 */
export interface ApprovalRequestData {
  request_id: string;
  tool_name: string;
  reason?: string;
}

interface ApprovalPromptProps {
  request: ApprovalRequestData | null;
  /** 决定后回调（父组件清空弹窗状态） */
  onSettled?: (requestId: string, outcome: string) => void;
}

// 高危工具的醒目标识（T3 级命令执行类）
const DANGEROUS_TOOLS = new Set(['bash', 'run_command', 'ssh_exec', 'server_restart', 'db_migrate']);

export function ApprovalPrompt({ request, onSettled }: ApprovalPromptProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDecide = useCallback(async (outcome: 'allowed-once' | 'rejected') => {
    if (!request || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiClient.v2DecideApproval(request.request_id, outcome);
      onSettled?.(request.request_id, outcome);
    } catch (e) {
      // 404 = 请求已超时/已完结（迟到决定被服务端丢弃），视为已了结
      const msg = e instanceof Error ? e.message : '提交失败';
      if (msg.includes('not found') || msg.includes('404')) {
        onSettled?.(request.request_id, 'unavailable');
      } else {
        setError(msg);
      }
    } finally {
      setSubmitting(false);
    }
  }, [request, submitting, onSettled]);

  if (!request) return null;

  const isDangerous = DANGEROUS_TOOLS.has(request.tool_name);

  return (
    <div className="approval-prompt-overlay">
      <div className={`approval-prompt ${isDangerous ? 'dangerous' : ''}`}>
        <div className="approval-prompt-header">
          <span className="approval-prompt-icon">{isDangerous ? '🛡️' : '🔐'}</span>
          <span className="approval-prompt-title">权限审批请求</span>
        </div>
        <div className="approval-prompt-body">
          <p className="approval-prompt-tool">
            agent 请求执行工具 <code>{request.tool_name}</code>
            {isDangerous && <span className="approval-prompt-danger-tag">高危操作</span>}
          </p>
          {request.reason && <p className="approval-prompt-reason">{request.reason}</p>}
          <p className="approval-prompt-hint">
            「允许本次」只放行这一次调用，不改变会话权限级别；
            不确定请选择「拒绝」。
          </p>
        </div>
        {error && <div className="approval-prompt-error">{error}</div>}
        <div className="approval-prompt-actions">
          <button
            type="button"
            className="approval-prompt-btn reject"
            disabled={submitting}
            onClick={() => handleDecide('rejected')}
          >
            拒绝
          </button>
          <button
            type="button"
            className="approval-prompt-btn allow"
            disabled={submitting}
            onClick={() => handleDecide('allowed-once')}
          >
            {submitting ? '提交中…' : '允许本次'}
          </button>
        </div>
      </div>
    </div>
  );
}
