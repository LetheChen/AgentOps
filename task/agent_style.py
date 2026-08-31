"""AgentStyleLoader — agent 风格加载器（从 config/agent_styles/ 扫描 yaml）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.6.3
- 启动时扫描 config/agent_styles/*.yaml，构建 style_id → config 映射
- get_overlay(style_id) 返回 system_prompt_overlay 字符串（供 orchestrator 装配 prompt）
- get_style(style_id) 返回完整风格配置 dict
- list_styles() 返回所有风格列表
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class AgentStyleLoader:
    """agent 风格加载器。

    Args:
        styles_dir: config/agent_styles/ 目录路径
    """

    def __init__(self, styles_dir: Path | str):
        self._dir = Path(styles_dir)
        self._styles: dict[str, dict] = {}
        self._load_all()

    def _load_all(self) -> None:
        """启动时扫描目录，加载所有 *.yaml 风格配置。"""
        if not self._dir.exists():
            logger.warning("agent_styles 目录不存在: %s", self._dir)
            return
        for yaml_file in self._dir.glob("*.yaml"):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                if cfg and "style_id" in cfg:
                    self._styles[cfg["style_id"]] = cfg
                    logger.debug("加载风格 %s: %s", cfg["style_id"], cfg.get("name"))
            except Exception as e:
                logger.warning("加载风格文件失败 %s: %s", yaml_file, e)
        logger.info("AgentStyleLoader 加载完成：%d 个风格", len(self._styles))

    async def get_overlay(self, style_id: str) -> str:
        """返回风格的 system_prompt_overlay（供 orchestrator 装配 prompt）。

        Args:
            style_id: 风格 ID（如 critical/conservative/aggressive）

        Returns:
            overlay 字符串；style_id 不存在或为 "default" 时返回空串
        """
        if not style_id or style_id == "default":
            return ""
        style = self._styles.get(style_id)
        if not style:
            logger.warning("风格 %s 未找到，使用默认（无 overlay）", style_id)
            return ""
        return style.get("system_prompt_overlay", "") or ""

    def get_style(self, style_id: str) -> dict | None:
        """返回完整风格配置 dict。"""
        return self._styles.get(style_id)

    def list_styles(self) -> list[dict]:
        """返回所有风格列表（按 name 排序）。"""
        return sorted(self._styles.values(), key=lambda s: s.get("name", ""))
