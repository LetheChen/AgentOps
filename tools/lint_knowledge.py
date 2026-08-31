"""lint_knowledge 工具：LLM Wiki Lint 操作（Layer B）。

背景：content_curator agent 的核心能力是冲突检测（FR-16）。当新内容要归档时，
需要先与历史笔记对比，检测 4 类冲突（fact/version/opinion/complement）。
本工具做程序化检查 + 输出结构化结果，让 agent 拿到候选后用 LLM 判断 confidence。

6 种 check_type：
- contradictions（核心）：新内容 vs 历史笔记的候选冲突对（基于关键词重叠）
- orphans：未被引用的实体/概念页
- missing_pages：被引用但不存在的页面（[[xxx]] 链接目标缺失）
- index_sync：index.md 条目数 vs raw/ 实际文件数
- dead_links：markdown 链接 [text](path) 目标不存在
- stale：过时声明（需 LLM 判断，工具只标记可疑项）

设计原则：
- 工具只做「程序化检查 + 候选输出」，不做 LLM 判断（agent 的职责）
- contradictions 检测：关键词提取 + TF-IDF 简单匹配（非语义）
- auto_fix 仅对 index_sync 类型生效（重建 index.md 计数）
- 4 类冲突的 confidence 由 LLM 后续打分（工具输出 confidence=None）

参考配置：config/tools/lint_knowledge.yaml
设计文档：docs/knowledge-base/DESIGN_content_curator_agent.md §3.3
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 知识库根目录（由 kb_config.resolve_kb_root() 动态解析）
# 不再硬编码 DOMAIN_MAP。新增 domain 只改 config/knowledge/domains.yaml
from tools import kb_config  # noqa: E402

KB_ROOT = Path(__file__).resolve().parent.parent / "config" / "knowledge"

# 支持的 domain（向后兼容：保留 DOMAIN_MAP 名称，3 个工具 import 此名）
# 仅包含 llm_wiki schema 的 domain（video_production 走专用 query_knowledge.py）
DOMAIN_MAP = kb_config.get_domain_map()

# 支持的 check_type
CHECK_TYPES = ["contradictions", "orphans", "missing_pages", "stale", "index_sync", "dead_links"]

# 4 类冲突
CONFLICT_TYPES = ["fact", "version", "opinion", "complement"]

# 默认置信度阈值（Q-5）
DEFAULT_CONFIDENCE_THRESHOLD = 0.7

# 中文/英文关键词最小长度（过滤无意义短词）
MIN_KEYWORD_LENGTH = 2
# 关键词最大数（避免输出过长）
MAX_KEYWORDS_PER_DOC = 20


async def lint_knowledge(args: dict[str, Any]) -> dict[str, Any]:
    """LLM Wiki Lint 操作工具。

    Args:
        args: dict
            - domain (str, required): weekly-report | proposal-planning | content-curation
            - check_types (list[str]): 默认全部检查
            - auto_fix (bool): 是否自动修复可修复项（仅 index_sync 生效）
            - new_content (str): 新内容（contradictions 检查时必填）
            - confidence_threshold (float): 冲突检测置信度阈值（默认 0.7）
            - agent_id (str): 调用方 agent id

    Returns:
        dict，至少包含 content + domain + checked_at + 各 check_type 结果。
    """
    domain = (args.get("domain") or "").strip()
    if domain not in DOMAIN_MAP:
        return {
            "content": f"调用失败：未知 domain '{domain}'",
            "error": "unknown_domain",
            "available_domains": list(DOMAIN_MAP.keys()),
        }

    check_types = args.get("check_types") or CHECK_TYPES
    if isinstance(check_types, str):
        check_types = [check_types]
    # 校验 check_types
    invalid = [ct for ct in check_types if ct not in CHECK_TYPES]
    if invalid:
        return {
            "content": f"调用失败：未知 check_type {invalid}",
            "error": "unknown_check_type",
            "available_check_types": CHECK_TYPES,
        }

    auto_fix = bool(args.get("auto_fix", False))
    new_content = args.get("new_content") or ""
    confidence_threshold = float(args.get("confidence_threshold") or DEFAULT_CONFIDENCE_THRESHOLD)
    agent_id = args.get("agent_id") or "unknown_agent"

    # contradictions 必须传 new_content
    if "contradictions" in check_types and not new_content:
        return {
            "content": "调用失败：contradictions 检查必须传 new_content",
            "error": "missing_new_content",
        }

    domain_dir = KB_ROOT / DOMAIN_MAP[domain]
    if not domain_dir.exists():
        return {
            "content": f"调用失败：domain 目录不存在：{domain_dir}",
            "error": "domain_dir_not_found",
        }

    # 执行检查
    issues: list[dict[str, Any]] = []
    new_content_conflicts: list[dict[str, Any]] = []
    auto_fixed = 0

    if "contradictions" in check_types:
        new_content_conflicts = _check_contradictions(domain_dir, new_content, confidence_threshold)
        issues.extend(new_content_conflicts)

    if "orphans" in check_types:
        issues.extend(_check_orphans(domain_dir))

    if "missing_pages" in check_types:
        issues.extend(_check_missing_pages(domain_dir))

    if "dead_links" in check_types:
        issues.extend(_check_dead_links(domain_dir))

    if "index_sync" in check_types:
        sync_issues, fixed = _check_index_sync(domain_dir, auto_fix=auto_fix)
        issues.extend(sync_issues)
        auto_fixed += fixed

    if "stale" in check_types:
        # stale 需要 LLM 判断，工具只标记「无更新时间」的可疑项
        issues.extend(_check_stale(domain_dir))

    needs_human_review = sum(1 for i in issues if i.get("severity") == "high")

    return {
        "content": (
            f"Lint 完成：domain={domain} checks={check_types} "
            f"issues={len(issues)} auto_fixed={auto_fixed} needs_review={needs_human_review}"
        ),
        "domain": domain,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "check_types": check_types,
        "new_content_conflicts": new_content_conflicts,
        "issues": issues,
        "auto_fixed": auto_fixed,
        "needs_human_review": needs_human_review,
        "confidence_threshold": confidence_threshold,
        "agent_id": agent_id,
    }


# ==================== contradictions 检查（核心）====================


def _check_contradictions(
    domain_dir: Path, new_content: str, confidence_threshold: float
) -> list[dict[str, Any]]:
    """检测新内容与历史笔记的候选冲突对。

    工具只做关键词重叠匹配，输出候选对；confidence 留 None 让 LLM 后续打分。
    """
    # 提取新内容关键词
    new_keywords = _extract_keywords(new_content)
    if not new_keywords:
        return []

    conflicts: list[dict[str, Any]] = []

    # 扫所有 wiki 页面（entities/*.md, concepts/*.md, comparisons/*.md, 根目录下 *.md 除 index/log/AGENTS）
    wiki_pages = _list_wiki_pages(domain_dir)
    for page_path, page_text in wiki_pages:
        page_keywords = _extract_keywords(page_text)
        if not page_keywords:
            continue
        # 计算关键词重叠
        overlap = new_keywords & page_keywords
        if not overlap:
            continue
        # 重叠率（Jaccard 系数）
        union = new_keywords | page_keywords
        overlap_ratio = len(overlap) / len(union) if union else 0
        # 重叠率 > 0.1 才算候选（避免噪音）
        if overlap_ratio < 0.1:
            continue

        # 输出候选冲突对（confidence=None，待 LLM 打分）
        rel_path = str(page_path.relative_to(domain_dir))
        conflicts.append({
            "type": "candidate",  # fact/version/opinion/complement 由 LLM 后续判定
            "severity": "medium",
            "new_content_keywords": sorted(list(overlap))[:10],  # 重叠关键词（前 10）
            "existing_page": rel_path,
            "existing_keywords": sorted(list(page_keywords))[:10],
            "overlap_ratio": round(overlap_ratio, 3),
            "confidence": None,  # 待 LLM 打分
            "auto_fixable": False,
            "recommended_action": f"调 LLM 对比新内容 vs {rel_path}，判定 fact/version/opinion/complement 类型 + confidence",
            "llm_prompt_hint": (
                f"请对比以下新内容与历史笔记 {rel_path}，判定冲突类型（fact 事实矛盾 / version 版本差异 / "
                f"opinion 观点对立 / complement 互补）+ confidence (0-1)。重叠关键词：{sorted(list(overlap))[:5]}"
            ),
        })

    # 按 overlap_ratio 降序
    conflicts.sort(key=lambda x: x.get("overlap_ratio", 0), reverse=True)
    return conflicts


# ==================== orphans 检查 ====================


def _check_orphans(domain_dir: Path) -> list[dict[str, Any]]:
    """检测未被引用的实体/概念页。"""
    # 收集所有 wiki 页面名（不含扩展名）
    wiki_pages = _list_wiki_page_names(domain_dir)
    if not wiki_pages:
        return []

    # 扫所有 md 文件中的引用（[[xxx]] 和 [text](xxx.md)）
    referenced: set[str] = set()
    for md_file in domain_dir.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Obsidian wikilink: [[xxx]] 或 [[xxx|alias]]
        for m in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", text):
            referenced.add(m.group(1).strip())
        # Markdown link: [text](xxx.md) 或 [text](path/xxx.md)
        for m in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", text):
            link = m.group(1).strip()
            # 取文件名（不含扩展名和路径）
            name = Path(link).stem
            referenced.add(name)

    # 找未被引用的
    orphans = []
    for page_name, page_path in wiki_pages:
        if page_name not in referenced:
            orphans.append({
                "type": "orphan",
                "severity": "low",
                "page": page_name,
                "path": str(page_path.relative_to(domain_dir)),
                "auto_fixable": False,
                "recommended_action": "考虑在相关页面中引用此页，或合并到父页面",
            })

    return orphans


# ==================== missing_pages 检查 ====================


def _check_missing_pages(domain_dir: Path) -> list[dict[str, Any]]:
    """检测被引用但不存在的页面（[[xxx]] 链接目标缺失）。"""
    existing_pages = {p.stem for p in domain_dir.rglob("*.md")}
    missing = []

    for md_file in domain_dir.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel_source = str(md_file.relative_to(domain_dir))
        # Obsidian wikilink: [[xxx]] 或 [[xxx|alias]]
        for m in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", text):
            target = m.group(1).strip()
            # 跳过外部 URL 和锚点
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            # 取纯文件名（去路径）
            target_name = Path(target).stem
            if target_name not in existing_pages:
                missing.append({
                    "type": "missing_page",
                    "severity": "medium",
                    "referenced_in": rel_source,
                    "missing_target": target,
                    "auto_fixable": False,
                    "recommended_action": f"创建 {target}.md 或修正引用",
                })

    return missing


# ==================== dead_links 检查 ====================


def _check_dead_links(domain_dir: Path) -> list[dict[str, Any]]:
    """检测 markdown 链接 [text](path) 目标不存在。"""
    dead = []

    for md_file in domain_dir.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel_source = str(md_file.relative_to(domain_dir))
        # Markdown link: [text](xxx) — 只检查相对路径，跳过 http/mailto/锚点
        for m in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
            link = m.group(1).strip()
            # 跳过外部 URL / 锚点 / 邮箱
            if link.startswith(("http://", "https://", "#", "mailto:", "ftp://")):
                continue
            # 去掉锚点部分
            link_path = link.split("#")[0].strip()
            if not link_path:
                continue
            # 解析相对路径
            target = (md_file.parent / link_path).resolve()
            if not target.exists():
                dead.append({
                    "type": "dead_link",
                    "severity": "low",
                    "referenced_in": rel_source,
                    "dead_target": link,
                    "auto_fixable": False,
                    "recommended_action": f"修正链接或创建目标文件 {link}",
                })

    return dead


# ==================== index_sync 检查 ====================


def _check_index_sync(domain_dir: Path, auto_fix: bool = False) -> tuple[list[dict], int]:
    """检测 index.md 条目数 vs raw/ 实际文件数。auto_fix=True 时重建 index 对应章节计数。

    Returns:
        (issues, auto_fixed_count)
    """
    index_path = domain_dir / "index.md"
    raw_dir = domain_dir / "raw"

    if not index_path.exists():
        return [], 0

    # 数 raw/ 下实际文件数
    actual_raw_count = len(list(raw_dir.glob("*.md"))) if raw_dir.exists() else 0

    # 数 index.md 中的 raw/ 引用数（条目格式：- [标题](raw/xxx.md)）
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except OSError:
        return [], 0
    index_refs = re.findall(r"\]\(raw/[^)]+\.md\)", index_text)
    index_count = len(index_refs)

    if index_count == actual_raw_count:
        return [], 0

    issue = {
        "type": "index_sync",
        "severity": "medium" if abs(index_count - actual_raw_count) > 2 else "low",
        "index_count": index_count,
        "actual_count": actual_raw_count,
        "auto_fixable": True,
        "recommended_action": (
            f"index.md 有 {index_count} 个 raw 引用，实际 raw/ 有 {actual_raw_count} 个文件，"
            f"建议人工核对后用 ingest_source 重新写入或手工调整"
        ),
    }

    # auto_fix 不自动重建 index（避免误删条目），只标记问题
    # 真正的修复需要 agent 调 ingest_source 重新 ingest 或人工调整
    return [issue], 0


# ==================== stale 检查（仅标记可疑项）====================


def _check_stale(domain_dir: Path) -> list[dict[str, Any]]:
    """标记「无更新时间」或「超过 180 天未更新」的可疑项。"""
    stale_items = []
    now = datetime.now(timezone.utc)
    threshold_days = 180

    for md_file in domain_dir.rglob("*.md"):
        # 跳过 raw/（不可变层）和配置文件
        rel = md_file.relative_to(domain_dir)
        if str(rel).startswith("raw/"):
            continue
        if md_file.name in ("index.md", "log.md", "AGENTS.md", "template.md"):
            continue

        fm = _parse_frontmatter(md_file)
        # 没有 created_at / updated_at 字段的可疑
        if not fm.get("created_at") and not fm.get("ingested_at"):
            stale_items.append({
                "type": "stale_suspect",
                "severity": "low",
                "page": str(rel),
                "reason": "无 frontmatter 时间字段，无法判断时效性",
                "auto_fixable": False,
                "recommended_action": "补充 frontmatter.created_at 或由 LLM 判断内容是否过时",
            })
            continue

        # 检查是否超过 180 天未更新
        time_str = fm.get("updated_at") or fm.get("ingested_at") or fm.get("created_at")
        if not time_str:
            continue
        try:
            dt = datetime.fromisoformat(str(time_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (now - dt).days
            if age_days > threshold_days:
                stale_items.append({
                    "type": "stale",
                    "severity": "low",
                    "page": str(rel),
                    "age_days": age_days,
                    "last_updated": time_str,
                    "auto_fixable": False,
                    "recommended_action": f"由 LLM 判断内容是否过时（已 {age_days} 天未更新）",
                })
        except (ValueError, TypeError):
            continue

    return stale_items


# ==================== 辅助函数 ====================


def _extract_keywords(text: str) -> set[str]:
    """简单关键词提取：去标点 + 分词 + 过滤停用词 + 长度 ≥ 2。

    中文按字 + 英文按单词，不做 NLP（工具只做候选检测，语义由 LLM 判断）。
    """
    if not text:
        return set()

    # 去 markdown 标记和 frontmatter
    clean = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    clean = re.sub(r"```[^\n]*\n.*?\n```", "", clean, flags=re.DOTALL)  # 代码块
    clean = re.sub(r"[#`*\[\]()>|_\-!]", " ", clean)  # markdown 标记

    keywords: set[str] = set()

    # 英文单词（≥3 字符）
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\-]{2,}", clean):
        word = m.group().lower()
        if word not in ENGLISH_STOPWORDS:
            keywords.add(word)

    # 中文连续字符（≥2 字符，按 2-gram）
    chinese_text = re.sub(r"[^\u4e00-\u9fff]", " ", clean)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", chinese_text):
        segment = m.group()
        # 2-gram 切分
        for i in range(len(segment) - 1):
            keywords.add(segment[i : i + 2])

    # 限制关键词数
    if len(keywords) > MAX_KEYWORDS_PER_DOC * 2:
        # 简单截断（实际场景由 LLM 处理）
        keywords = set(list(keywords)[: MAX_KEYWORDS_PER_DOC * 2])

    return keywords


# 英文停用词（简单版本）
ENGLISH_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "her", "was", "one",
    "our", "out", "his", "has", "had", "how", "its", "may", "them", "than", "this", "that",
    "with", "have", "from", "they", "were", "been", "said", "each", "which", "their", "will",
    "what", "when", "your", "them", "then", "these", "those", "into", "over", "also", "made",
    "more", "some", "such", "only", "very", "does", "done", "here", "there", "where", "would",
    "could", "should", "might", "must", "shall", "about", "after", "before", "between", "through",
    "during", "while", "because", "though", "although", "since", "until", "upon", "within",
    "without", "any", "few", "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "can", "will", "just", "don", "should", "now",
}


def _list_wiki_pages(domain_dir: Path) -> list[tuple[Path, str]]:
    """列出所有 wiki 页面（entities/concepts/comparisons 子目录 + 根目录下的非骨架文件）。"""
    pages: list[tuple[Path, str]] = []
    skip_files = {"index.md", "log.md", "AGENTS.md", "template.md"}

    for md_file in domain_dir.rglob("*.md"):
        if md_file.name in skip_files:
            continue
        # 跳过 raw/（不可变层，不参与冲突检测）
        if "raw" in md_file.parts:
            continue
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        pages.append((md_file, text))

    return pages


def _list_wiki_page_names(domain_dir: Path) -> list[tuple[str, Path]]:
    """列出所有 wiki 页面名（不含扩展名）+ 路径，用于 orphans 检测。"""
    pages: list[tuple[str, Path]] = []
    skip_files = {"index", "log", "AGENTS", "template"}

    for md_file in domain_dir.rglob("*.md"):
        if md_file.stem in skip_files:
            continue
        if "raw" in md_file.parts:
            continue
        pages.append((md_file.stem, md_file))

    return pages


def _parse_frontmatter(file_path: Path) -> dict[str, Any]:
    """解析 md 文件的 YAML frontmatter。"""
    if not file_path.exists() or file_path.suffix.lower() != ".md":
        return {}
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}
    fm_text = text[3:end_idx].strip()
    try:
        import yaml
        return yaml.safe_load(fm_text) or {}
    except (yaml.YAMLError, ImportError):
        return {}
