"""task_commit_stage 工具：回写 stage_output + 可选推进状态机。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.5.4
- 调 store.create_stage + commit_stage
- 可选推进状态机（若传 target_status）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def task_commit_stage(args: dict) -> dict:
    """提交阶段产出，可选推进状态机。

    args:
        task_id (str, required): 任务 ID
        stage_type (str, required): 阶段类型（如 discussing/decomposing/coding/validating）
        stage_output (str, optional): 阶段产出内容
        target_status (str, optional): 若提供则推进状态机到此状态
        if_version (int, optional): 乐观锁版本号（推进状态机时必需）
        comment (str, optional): 转移备注（推进时透传）
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "提交失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "提交失败：缺少 task_id", "error": "missing_task_id"}

    stage_type = (args.get("stage_type") or "").strip()
    if not stage_type:
        return {"content": "提交失败：缺少 stage_type", "error": "missing_stage_type"}

    stage_output = args.get("stage_output") or ""
    target_status = (args.get("target_status") or "").strip() or None
    comment = args.get("comment") or ""

    if_version_raw = args.get("if_version")
    if_version = None
    if if_version_raw is not None:
        try:
            if_version = int(if_version_raw)
        except (ValueError, TypeError):
            return {"content": f"提交失败：if_version 非法（{if_version_raw}）", "error": "invalid_if_version"}

    if target_status is not None and if_version is None:
        return {"content": "提交失败：推进状态机需提供 if_version", "error": "missing_if_version"}

    # 1. 建 stage 记录
    stage_id = f"stage_{task_id}_{stage_type}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    stage = await orch.store.create_stage(
        stage_id=stage_id, task_id=task_id, stage_type=stage_type,
        stage_input=comment or "", stage_output=stage_output)

    # 2. 提交 stage 产出
    committed = await orch.store.commit_stage(stage_id, 0, stage_output)
    if committed is None:
        return {"content": f"提交失败：stage 乐观锁冲突（stage_id={stage_id}）",
                "error": "stage_conflict", "stage": stage}

    # 3. 可选推进状态机
    advanced_task = None
    if target_status is not None:
        adv_result = await orch.advance_stage(
            task_id=task_id, target_status=target_status,
            if_version=if_version, actor="agent",
            comment=comment, stage_output=stage_output)
        if not adv_result.get("ok"):
            return {"content": (f"stage 已提交但状态机推进失败："
                                f"{adv_result.get('message') or adv_result.get('error')}"),
                    "error": adv_result.get("error"),
                    "stage": committed,
                    "task": adv_result.get("task")}
        advanced_task = adv_result["task"]

    return {
        "content": (f"已提交阶段 {stage_type} 产出（stage_id={stage_id}）"
                    + (f"，并推进到 {advanced_task['status']}" if advanced_task else "")),
        "stage": committed,
        "task": advanced_task,
    }
