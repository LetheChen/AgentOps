"""task_link_run 工具：弱关联 task_run（run/session/terminal）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 调 store.link_task_run
- 弱关联：run_id/session_id 不强制 FK，便于跨存储/跨进程关联
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def task_link_run(args: dict) -> dict:
    """弱关联 task_run（run/session/terminal）。

    args:
        task_id (str, required): 任务 ID
        run_id (str, optional): Run ID
        session_id (str, optional): Session ID
        terminal_session_id (str, optional): Terminal 会话 ID
        role (str, optional): 关联角色（默认 main_execution）
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "关联失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "关联失败：缺少 task_id", "error": "missing_task_id"}

    run_id = (args.get("run_id") or "").strip()
    session_id = (args.get("session_id") or "").strip()
    terminal_session_id = (args.get("terminal_session_id") or "").strip()
    role = (args.get("role") or "main_execution").strip()

    if not (run_id or session_id or terminal_session_id):
        return {"content": "关联失败：至少需提供 run_id/session_id/terminal_session_id 之一",
                "error": "missing_link_target"}

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "关联失败：任务不存在", "error": "task_not_found"}

    link = await orch.store.link_task_run(
        task_id=task_id, role=role,
        run_id=run_id, session_id=session_id,
        terminal_session_id=terminal_session_id)

    return {
        "content": (f"已关联 task_run（link_id={link['link_id']}, role={role}"
                    + (f", run_id={run_id}" if run_id else "")
                    + (f", session_id={session_id}" if session_id else "")
                    + (f", terminal_session_id={terminal_session_id}" if terminal_session_id else "")
                    + "）"),
        "link": link,
    }
