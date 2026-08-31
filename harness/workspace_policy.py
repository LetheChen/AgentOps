"""Workspace read policy — PreToolUse hook 防工作区逃逸。

移植自 patch_worker/src/agent/workspace-read-policy.ts（只读类工具拦截）。

核心事实（patch 注释原文）：
  The SDK's cwd and `workspace_access` snapshot are not a read sandbox
  on their own: absolute paths and symlinks must be rejected before
  the tool executes.

拦截范围：Claude 内置 Read / Grep / Glob / LS 四个工具的路径参数。
校验规则：
  - resolve() 展开 `..` 与符号链接后，目标必须位于 workspace 根内
  - Windows 绝对路径（E:\\x / C:/x）会天然落点在根外 → deny
  - Glob 的 pattern 含 `..` 段 → deny
  - 大小写：Windows 文件系统大小写不敏感，比较前 normcase
边界（诚实说明）：Bash 不在拦截范围（patch），Bash 越权由
AgentRunContext.permission_check 回调与审批流兜底。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# 工具名 → 路径参数字段名（patch）
READ_TOOL_PATH_FIELDS: dict[str, str] = {
    "Read": "file_path",
    "Grep": "path",
    "Glob": "path",
    "LS": "path",
}


def is_within(root: Path, target: Path) -> bool:
    """target 是否位于 root 内（含 root 本身）。

    Windows 文件系统大小写不敏感，normcase 后再比较，
    避免 `E:\\Project\\AgentOps` vs `e:\\project\\agentops` 误判。
    """
    r = os.path.normcase(str(root))
    t = os.path.normcase(str(target))
    return t == r or t.startswith(r + os.sep)


def _deny(reason: str) -> dict[str, Any]:
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def _allow() -> dict[str, Any]:
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        },
    }


def create_workspace_read_hook(workspace_root: str) -> Any:
    """构造 PreToolUse hook 回调（Claude Agent SDK HookCallback）。

    只拦 READ_TOOL_PATH_FIELDS 中的工具；其余工具直接放行
    （权限兜底由 permission_check / 审批流负责）。
    """
    root = Path(workspace_root).resolve()

    async def workspace_read_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        field = READ_TOOL_PATH_FIELDS.get(tool_name)
        if not field:
            return {"continue": True}

        tool_input = input_data.get("tool_input")
        if not isinstance(tool_input, dict):
            return _deny(f"{tool_name} requires a path inside the workspace root")

        raw_path = tool_input.get(field)
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            return _deny(f"{tool_name}.{field} must name a path inside the workspace root")

        # Glob 的 pattern 也可能带 .. 逃逸
        if tool_name == "Glob":
            pattern = tool_input.get("pattern")
            if isinstance(pattern, str) and any(
                seg == ".." for seg in pattern.replace("\\", "/").split("/")
            ):
                return _deny("Glob.pattern must be relative and traversal-free")

        try:
            # 绝对路径天然落点在根外；resolve 展开 .. 与符号链接/junction
            target = (root / raw_path).resolve()
        except (OSError, ValueError):
            return _deny(f"{tool_name} target could not be resolved inside the workspace root")

        if not is_within(root, target):
            return _deny(
                f"{tool_name} target is outside the workspace root: "
                f"{raw_path} (allowed root: {root})"
            )
        return _allow()

    return workspace_read_hook
