"""task_submit_report 工具：agent 提交任务报告 + 可选评论。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 调 store.submit_report + 可选 store.add_comment（comment_type=report）
- 博客评论模式：report 落库后用户/其他 agent 可在 task_comments 回复
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_submit_report(args: dict) -> dict:
    """提交任务报告 + 可选评论。

    args:
        task_id (str, required): 任务 ID
        agent_id (str, required): 提交 agent ID
        content (str, required): 报告正文
        artifact_ids (list[str], optional): 关联交付物 ID 列表
        self_check (dict, optional): 验收自检结果（acceptance_self_check）
        comment_body (str, optional): 若提供则同步写一条 comment_type=report 评论
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "提交失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "提交失败：缺少 task_id", "error": "missing_task_id"}

    agent_id = (args.get("agent_id") or "").strip()
    if not agent_id:
        return {"content": "提交失败：缺少 agent_id", "error": "missing_agent_id"}

    content = args.get("content") or ""
    if not content.strip():
        return {"content": "提交失败：缺少 content", "error": "missing_content"}

    artifact_ids = args.get("artifact_ids") or []
    self_check = args.get("self_check") or {}
    comment_body = args.get("comment_body") or ""

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "提交失败：任务不存在", "error": "task_not_found"}

    report = await orch.store.submit_report(
        task_id=task_id, agent_id=agent_id, content=content,
        artifact_ids=artifact_ids, self_check=self_check)

    comment = None
    if comment_body:
        comment = await orch.store.add_comment(
            task_id=task_id, body=comment_body,
            author_type="agent", author_id=agent_id,
            comment_type="report", report_id=report["report_id"])

    return {
        "content": (f"已提交任务报告（report_id={report['report_id']}, agent={agent_id}"
                    + (f", 已同步评论 comment_id={comment['comment_id']}" if comment else "")
                    + "）"),
        "report": report,
        "comment": comment,
    }
