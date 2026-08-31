"""task_create_project 工具：建项目。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.9.3
签名：async def xxx(args: dict) -> dict（对齐 tools/trigger_workflow.py）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def task_create_project(args: dict) -> dict:
    """建项目。

    args:
        name (str, required): 项目名
        type (str, optional): code|knowledge|hybrid，默认 code
        local_path (str, optional): 本地路径
        workspace_id (str, optional): 工作区 ID
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "创建失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    name = (args.get("name") or "").strip()
    if not name:
        return {"content": "创建失败：缺少 name", "error": "missing_name"}

    project_type = (args.get("type") or "code").strip()
    local_path = (args.get("local_path") or "").strip()
    workspace_id = (args.get("workspace_id") or "").strip()

    project_id = f"proj_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    result = await orch.create_project(
        project_id=project_id, name=name, type=project_type,
        local_path=local_path, workspace_id=workspace_id)
    if not result.get("ok"):
        return {"content": f"创建失败：{result.get('error')}", "error": result.get("error")}

    project = result["project"]
    return {
        "content": f"已创建项目 {project['name']}（project_id={project['project_id']}）",
        "project": project,
        "project_id": project["project_id"],
    }
