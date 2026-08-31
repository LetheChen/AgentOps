"""scan_drafts 工具：草稿仓库增量扫描（Layer B）。

背景：content_curator agent 入口 B（templated DAG）需要扫描 E:\\Document\\草稿仓库\\
下新增/未处理的文档，作为 evaluate 节点的输入。判断「已处理」的信号是 frontmatter.ingested_at
字段（obsidian_vault.write_file 自动注入）——无此字段视为未处理。

设计：
- 直接扫描 draft_root 子树（不调 obsidian_vault.scan_incremental，因为后者扫全 vault）
- 读 frontmatter 判断 ingested_at 是否存在
- since 非空时额外按 mtime 过滤（增量）
- since 为空时扫所有未处理文档
- 跳过 .obsidian/ 配置目录

参考配置：config/tools/scan_drafts.yaml
设计文档：docs/knowledge-base/DESIGN_content_curator_agent.md §3.2
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Vault 根目录（与 obsidian_vault 保持一致）
VAULT_ROOT = Path(r"E:\Document")

# 默认草稿仓库相对路径
DEFAULT_DRAFT_ROOT = "草稿仓库"


async def scan_drafts(args: dict[str, Any]) -> dict[str, Any]:
    """草稿仓库增量扫描工具。

    Args:
        args: dict
            - since (str): 起始时间（ISO 8601，不传则扫所有未处理）
            - draft_root (str): 草稿仓库相对路径（默认 "草稿仓库"）
            - max_results (int): 单次扫描上限（默认 50）
            - agent_id (str): 调用方 agent id

    Returns:
        dict，至少包含 content + scanned_at + new_drafts + total_new + already_processed。
    """
    since_str = (args.get("since") or "").strip()
    draft_root = (args.get("draft_root") or DEFAULT_DRAFT_ROOT).strip()
    max_results = int(args.get("max_results") or 50)
    agent_id = args.get("agent_id") or "unknown_agent"

    # 解析 draft_root 为绝对路径
    draft_dir = _resolve_draft_root(draft_root)
    if not draft_dir.exists():
        return {
            "content": f"扫描失败：草稿仓库不存在：{draft_root}（绝对路径：{draft_dir}）",
            "error": "draft_root_not_found",
            "draft_root": draft_root,
        }
    if not draft_dir.is_dir():
        return {
            "content": f"扫描失败：路径不是目录：{draft_root}",
            "error": "not_a_directory",
            "draft_root": draft_root,
        }

    # 解析 since
    since_dt = None
    if since_str:
        try:
            since_dt = datetime.fromisoformat(since_str)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError as e:
            return {
                "content": f"扫描失败：since 解析失败：{e}",
                "error": "invalid_since",
                "since": since_str,
            }

    # 扫描子树
    new_drafts = []
    already_processed = 0
    since_ts = since_dt.timestamp() if since_dt else 0

    for f in draft_dir.rglob("*"):
        if not f.is_file():
            continue
        # 跳过 .obsidian/ 配置目录
        if ".obsidian" in f.parts:
            continue
        # 只扫 md 文件（草稿主要是 markdown）
        if f.suffix.lower() != ".md":
            continue

        mtime = f.stat().st_mtime
        # since 非空时按 mtime 过滤
        if since_dt and mtime <= since_ts:
            continue

        # 读 frontmatter 判断 ingested_at
        fm = _parse_frontmatter(f)
        has_ingested_at = bool(fm.get("ingested_at"))

        if has_ingested_at:
            already_processed += 1
            continue

        # 未处理文档
        new_drafts.append({
            "path": str(f.relative_to(VAULT_ROOT)),
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "has_ingested_at": False,
            "title": fm.get("title") or f.stem,
        })

        if len(new_drafts) >= max_results:
            break

    return {
        "content": f"扫描完成：{len(new_drafts)} 个未处理 + {already_processed} 个已处理（since={since_str or '(无)'}）",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "new_drafts": new_drafts,
        "total_new": len(new_drafts),
        "already_processed": already_processed,
        "truncated": len(new_drafts) >= max_results,
        "draft_root": draft_root,
        "since": since_str or None,
        "agent_id": agent_id,
    }


# ==================== 辅助函数 ====================


def _resolve_draft_root(draft_root: str) -> Path:
    """把草稿仓库相对路径解析为绝对路径，防止路径遍历。"""
    normalized = draft_root.replace("/", "\\").lstrip(".\\")
    abs_path = (VAULT_ROOT / normalized).resolve()
    # 校验仍在 vault 内
    try:
        abs_path.relative_to(VAULT_ROOT.resolve())
    except ValueError:
        raise ValueError(f"路径越界：{draft_root} 不在 vault 内")
    return abs_path


def _parse_frontmatter(file_path: Path) -> dict[str, Any]:
    """解析 md 文件的 YAML frontmatter（与 obsidian_vault 实现一致）。"""
    if not file_path.exists() or file_path.suffix.lower() != ".md":
        return {}
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    # 找第二个 --- 作为 frontmatter 结束
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}
    fm_text = text[3:end_idx].strip()
    try:
        import yaml
        return yaml.safe_load(fm_text) or {}
    except (yaml.YAMLError, ImportError):
        return {}
