"""ingest_source 工具：LLM Wiki 的 Ingest 操作（Layer B）。

背景：三个个人助理 agent 在执行过程中会产出有价值的内容（周报模式、方案案例、
内容评估结果），需要沉淀到后端知识库供后续 query_knowledge 查询。本工具实现
LLM Wiki 的 Ingest 操作：把素材写入 raw/（不可变层）+ 更新 index.md（导航枢纽）
+ 追加 log.md（时间线枢纽）。

设计：
- 不做 LLM 提炼（那是 agent 的职责），工具只做「记账」
- raw 文件不可变：每次 ingest 都写新文件，不覆盖
- index.md 追加条目（按 page_type 分类）
- log.md 追加 action 记录（时间线）
- 与 obsidian_vault 解耦：本工具操作 config/knowledge/，不碰 E:\\Document

参考配置：config/tools/ingest_source.yaml
设计文档：docs/knowledge-base/DESIGN_knowledge_base_module.md §4.2
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 知识库根目录（config/knowledge/）— 由 kb_config.resolve_kb_root() 动态解析，
# 不再硬编码 DOMAIN_MAP。新增 domain 只改 config/knowledge/domains.yaml
from tools import kb_config  # noqa: E402

KB_ROOT = Path(__file__).resolve().parent.parent / "config" / "knowledge"

# 支持的 domain → 子目录名（向后兼容：保留 DOMAIN_MAP 名称，3 个工具 import 此名）
# 仅包含 llm_wiki schema 的 domain（video_production 走专用 query_knowledge.py）
DOMAIN_MAP = kb_config.get_domain_map()

# 支持的 page_type（LLM Wiki 四类页面）
PAGE_TYPES = ["source", "entity", "concept", "comparison"]

# page_type → index.md 中的章节标题
PAGE_TYPE_SECTION = {
    "source": "## Sources（原始素材）",
    "entity": "## Entities（实体）",
    "concept": "## Concepts（概念）",
    "comparison": "## Comparisons（对比）",
}


async def ingest_source(args: dict[str, Any]) -> dict[str, Any]:
    """LLM Wiki Ingest 操作：把素材写入知识库 + 更新 index/log。

    Args:
        args: dict
            - domain (str, required): weekly-report | proposal-planning | content-curation
            - source_path (str): 原始素材路径（vault 内相对路径，与 source_content 二选一）
            - source_content (str): 直接传内容（与 source_path 二选一）
            - source_meta (dict): 元数据 {submitter, url, published, title, ...}
            - page_type (str): source | entity | concept | comparison（默认 source）
            - target_pages (list[str]): 指定要更新的 wiki 页面（不传则只写 raw + index/log）
            - page_content (str): 要写入 target_pages 的内容（target_pages 非空时必填）
            - agent_id (str): 调用方 agent id

    Returns:
        dict，至少包含 content + ingested + raw_path + index_updated + log_appended。
    """
    domain = (args.get("domain") or "").strip()
    if domain not in DOMAIN_MAP:
        return {
            "content": f"调用失败：未知 domain '{domain}'",
            "error": "unknown_domain",
            "available_domains": list(DOMAIN_MAP.keys()),
        }

    source_path = (args.get("source_path") or "").strip()
    source_content = args.get("source_content") or ""
    source_meta = args.get("source_meta") or {}
    page_type = (args.get("page_type") or "source").strip()
    target_pages = args.get("target_pages") or []
    page_content = args.get("page_content") or ""
    agent_id = args.get("agent_id") or "unknown_agent"

    if page_type not in PAGE_TYPES:
        return {
            "content": f"调用失败：未知 page_type '{page_type}'",
            "error": "unknown_page_type",
            "available_page_types": PAGE_TYPES,
        }

    if not source_path and not source_content:
        return {
            "content": "调用失败：source_path 和 source_content 至少传一个",
            "error": "missing_source",
        }

    if target_pages and not page_content:
        return {
            "content": "调用失败：target_pages 非空时必须传 page_content",
            "error": "missing_page_content",
        }

    domain_dir = KB_ROOT / DOMAIN_MAP[domain]
    raw_dir = domain_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # 1. 写 raw 文件（不可变层）
    timestamp = datetime.now(timezone.utc)
    timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")
    # 用内容 hash 防止重复 ingest
    content_hash = hashlib.md5(source_content.encode("utf-8")).hexdigest()[:8]
    raw_filename = f"{timestamp_str}_{content_hash}.md"
    raw_path = raw_dir / raw_filename

    # 组装 raw 文件内容（含 frontmatter）
    raw_frontmatter = {
        "ingested_at": timestamp.isoformat(),
        "ingested_by": agent_id,
        "domain": domain,
        "page_type": page_type,
        "source_path": source_path or "(inline content)",
        **source_meta,
    }
    raw_text = _serialize_with_frontmatter(raw_frontmatter, source_content)
    raw_path.write_text(raw_text, encoding="utf-8")
    logger.info(
        "ingest_source raw written domain=%s path=%s size=%d",
        domain, raw_path.name, len(raw_text),
    )

    # 2. 更新 index.md（追加条目）
    index_updated = _update_index(
        domain_dir / "index.md",
        domain=domain,
        page_type=page_type,
        raw_filename=raw_filename,
        source_meta=source_meta,
        source_path=source_path,
    )

    # 3. 追加 log.md（action 记录）
    log_appended = _append_log(
        domain_dir / "log.md",
        domain=domain,
        action="ingest",
        page_type=page_type,
        raw_filename=raw_filename,
        agent_id=agent_id,
        target_pages=target_pages,
    )

    # 4. 可选：更新 target_pages（wiki 页面）
    pages_updated = []
    if target_pages:
        for page_name in target_pages:
            page_path = domain_dir / f"{page_name}.md"
            updated = _update_wiki_page(page_path, page_name, page_type, page_content, source_meta, raw_filename)
            pages_updated.append({"page": page_name, "path": str(page_path.relative_to(KB_ROOT)), "updated": updated})

    return {
        "content": (
            f"Ingest 完成：domain={domain} page_type={page_type} raw={raw_filename} "
            f"index_updated={index_updated} log_appended={log_appended} pages_updated={len(pages_updated)}"
        ),
        "ingested": True,
        "raw_path": str(raw_path.relative_to(KB_ROOT)),
        "raw_filename": raw_filename,
        "index_updated": index_updated,
        "log_appended": log_appended,
        "pages_updated": pages_updated,
        "domain": domain,
        "page_type": page_type,
    }


# ==================== 辅助函数 ====================


def _update_index(
    index_path: Path,
    domain: str,
    page_type: str,
    raw_filename: str,
    source_meta: dict[str, Any],
    source_path: str,
) -> bool:
    """更新 index.md，追加新条目到对应 page_type 章节。"""
    section_header = PAGE_TYPE_SECTION.get(page_type, "## Sources（原始素材）")

    # 如果 index.md 不存在，创建骨架
    if not index_path.exists():
        skeleton = _build_index_skeleton(domain)
        index_path.write_text(skeleton, encoding="utf-8")

    # 读现有内容
    text = index_path.read_text(encoding="utf-8")

    # 构造新条目（raw 文件在 raw/ 子目录下，链接需带前缀）
    title = source_meta.get("title") or source_path or raw_filename
    entry = f"- [{title}](raw/{raw_filename}) — ingested {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    # 找到对应章节并追加
    lines = text.split("\n")
    section_idx = None
    next_section_idx = None
    for i, line in enumerate(lines):
        if line.strip() == section_header:
            section_idx = i
        elif section_idx is not None and line.startswith("## ") and i > section_idx:
            next_section_idx = i
            break

    if section_idx is None:
        # 章节不存在，追加到末尾
        lines.append("")
        lines.append(section_header)
        lines.append("")
        lines.append(entry)
    else:
        # 在章节末尾（下一个 ## 之前）追加
        insert_at = next_section_idx if next_section_idx is not None else len(lines)
        # 跳过章节末尾的空行
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        # 扫描骨架占位符「（暂无）」：首次 ingest 时直接替换，避免重复保留占位符
        placeholder_idx = None
        for i in range(section_idx + 1, insert_at):
            stripped = lines[i].strip()
            if stripped and stripped in {"（暂无）", "(暂无)", "(none)", "—"}:
                placeholder_idx = i
                break
        if placeholder_idx is not None:
            # 直接替换占位符行为新条目（最简单且不会破坏结构）
            lines[placeholder_idx] = entry
        else:
            lines.insert(insert_at, entry)

    index_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _append_log(
    log_path: Path,
    domain: str,
    action: str,
    page_type: str,
    raw_filename: str,
    agent_id: str,
    target_pages: list[str] | None = None,
) -> bool:
    """追加 action 记录到 log.md。"""
    # 如果 log.md 不存在，创建骨架
    if not log_path.exists():
        skeleton = _build_log_skeleton(domain)
        log_path.write_text(skeleton, encoding="utf-8")

    timestamp = datetime.now(timezone.utc).isoformat()
    target_pages_str = ",".join(target_pages) if target_pages else "-"
    entry = f"| {timestamp} | {action} | {page_type} | {raw_filename} | {agent_id} | {target_pages_str} |"

    # 追加到表格末尾
    text = log_path.read_text(encoding="utf-8")
    # 找到表格最后一行（| 开头）
    lines = text.split("\n")
    last_table_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("|"):
            last_table_idx = i

    if last_table_idx >= 0:
        lines.insert(last_table_idx + 1, entry)
    else:
        # 无表格，追加到末尾
        lines.append("")
        lines.append(entry)

    log_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _update_wiki_page(
    page_path: Path,
    page_name: str,
    page_type: str,
    page_content: str,
    source_meta: dict[str, Any],
    raw_filename: str,
) -> bool:
    """更新 wiki 页面（entity/concept/comparison 类）。

    如果页面不存在，创建新页面；如果存在，追加新章节。
    """
    # 构造新章节
    section_title = source_meta.get("title") or raw_filename
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_section = f"\n\n---\n\n### {section_title}（{timestamp}）\n\n来源：[raw/{raw_filename}](raw/{raw_filename})\n\n{page_content}\n"

    if not page_path.exists():
        # 创建新页面
        frontmatter = {
            "page_type": page_type,
            "page_name": page_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tags": ["wiki", page_type],
        }
        skeleton = _serialize_with_frontmatter(frontmatter, f"# {page_name}\n\n本页面由 ingest_source 工具自动维护。{new_section}")
        page_path.write_text(skeleton, encoding="utf-8")
    else:
        # 追加新章节
        text = page_path.read_text(encoding="utf-8")
        page_path.write_text(text + new_section, encoding="utf-8")

    return True


def _build_index_skeleton(domain: str) -> str:
    """构建 index.md 骨架。"""
    frontmatter = {
        "type": "index",
        "domain": domain,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tags": ["index"],
    }
    body = f"""# {domain} 知识库索引

> LLM Wiki 双枢纽之一：本文件列出所有页面，是知识库的导航入口。
> 另一个枢纽是 [log.md](log.md)（时间线记录）。

{PAGE_TYPE_SECTION["source"]}

（暂无）

{PAGE_TYPE_SECTION["entity"]}

（暂无）

{PAGE_TYPE_SECTION["concept"]}

（暂无）

{PAGE_TYPE_SECTION["comparison"]}

（暂无）

---

## 维护规则

- 新素材 ingest 后自动追加到对应章节
- 条目格式：`- [标题](raw/文件名) — ingested YYYY-MM-DD`
- 删除条目前必须先确认 raw 文件已无用
"""
    return _serialize_with_frontmatter(frontmatter, body)


def _build_log_skeleton(domain: str) -> str:
    """构建 log.md 骨架。"""
    frontmatter = {
        "type": "log",
        "domain": domain,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tags": ["log"],
    }
    body = f"""# {domain} 知识库时间线

> LLM Wiki 双枢纽之一：本文件按时间线记录所有 ingest/query/lint 操作。
> 另一个枢纽是 [index.md](index.md)（导航索引）。

| timestamp | action | page_type | raw_filename | agent_id | target_pages |
|---|---|---|---|---|---|
"""
    return _serialize_with_frontmatter(frontmatter, body)


def _serialize_with_frontmatter(frontmatter: dict[str, Any], content: str) -> str:
    """组装 frontmatter + 正文。"""
    try:
        import yaml
        if "tags" in frontmatter and isinstance(frontmatter["tags"], str):
            frontmatter["tags"] = [frontmatter["tags"]]
        fm_yaml = yaml.safe_dump(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
        return f"---\n{fm_yaml}\n---\n\n{content}"
    except (ImportError, Exception):
        fm_lines = []
        for k, v in frontmatter.items():
            if isinstance(v, list):
                fm_lines.append(f"{k}:")
                for item in v:
                    fm_lines.append(f"  - {item}")
            else:
                fm_lines.append(f"{k}: {v}")
        return f"---\n{chr(10).join(fm_lines)}\n---\n\n{content}"
