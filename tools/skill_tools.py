"""read_skill 工具：让 LLM 按需加载 skill 完整 body。

Phase 2 Skill 体系的核心工具。skill metadata（id + description）已注入
system_prompt 的「## 可用 Skill」段，但完整 body 不全量 inline（节省 token）。
LLM 看到需要的 skill 时调本工具加载完整内容。

设计：
- handler 只依赖 orchestrator._registry.get_skill_registry()，不直接 import SkillRegistry 类
- 走 in-process 调用，不读磁盘（scan 时 body 已驻留内存）
- 返回 {content: skill_body} 给 LLM，LLM 在下一轮回复中应用学到的规范
- skill 不存在时返回友好错误（含可用 skill id 列表）

参考：docs/architecture/DESIGN_architecture_refactor_v2.md §三 Skill 体系
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def read_skill(args: dict[str, Any]) -> dict[str, Any]:
    """读取一个 skill 的完整 body。

    args:
        skill_id (str, required): skill ID，如 "dag-ops" / "workflow-author"

    Returns:
        {"content": skill_body} 或 {"error": ..., "available": [...]}（skill 不存在时）
    """
    from orchestrator._registry import get_skill_registry

    skill_id = args.get("skill_id", "").strip()
    if not skill_id:
        return {
            "content": "❌ 缺少 skill_id 参数",
            "error": "missing_skill_id",
        }

    registry = get_skill_registry()
    if registry is None:
        return {
            "content": "❌ SkillRegistry 未初始化（后端服务未启动或 skills/ 目录为空）",
            "error": "registry_unavailable",
        }

    body = registry.get_skill_body(skill_id)
    if body is None:
        # skill 不存在，返回可用列表帮 LLM 自纠
        available = sorted(registry.skills.keys())
        return {
            "content": (
                f"❌ Skill '{skill_id}' 不存在。"
                f"可用 skill: {', '.join(available) or '（无）'}"
            ),
            "error": "skill_not_found",
            "available": available,
        }

    # 加前缀让 LLM 清楚这是 skill 内容
    meta = registry.get(skill_id)
    header = f"# Skill: {meta.name if meta else skill_id}\n"
    if meta and meta.depends_on:
        header += f"> 依赖: {', '.join(meta.depends_on)}\n"
    header += "\n"

    logger.info("read_skill: %s (%d chars)", skill_id, len(body))
    return {
        "content": header + body,
        "skill_id": skill_id,
        "length": len(body),
    }
