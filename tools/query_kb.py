"""query_kb 工具：通用知识库查询（Layer B，多 domain 支持）。

背景：两个个人助理 agent（content_curator / proposal_planner）在
执行过程中需要查询历史笔记、实体/概念页、对比页等。视频生产域已有专用版
query_knowledge.py（4 个固定 category），但通用版需要按 domain 动态路由。

设计：
- 按 domain 路由到 config/knowledge/<domain>/
- category 指定时返回该类别下的所有页面（patterns/cases/entities/concepts/comparisons）
- query 指定时按关键词在 domain 目录下搜索
- 同时返回 index.md 导航 + log.md 最近 10 条操作
- 与视频生产专用版 query_knowledge.py 解耦（向后兼容，不动视频生产域）

参考配置：config/tools/query_kb.yaml
设计文档：docs/knowledge-base/DESIGN_content_curator_agent.md §3.4
"""
from __future__ import annotations

import logging
import re
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

# 支持的 category（LLM Wiki 四类页面 + 通用 patterns/cases）
CATEGORIES = ["patterns", "cases", "entities", "concepts", "comparisons"]

# 默认返回上限
DEFAULT_MAX_RESULTS = 10


async def query_kb(args: dict[str, Any]) -> dict[str, Any]:
    """通用知识库查询工具。

    Args:
        args: dict
            - domain (str, required): weekly-report | proposal-planning | content-curation
            - category (str): 限定类别（patterns/cases/entities/concepts/comparisons）
            - query (str): 关键词查询（全文搜索）
            - max_results (int): 返回上限（默认 10）
            - agent_id (str): 调用方 agent id

    Returns:
        dict，至少包含 content + domain + index_summary + recent_log + 查询结果。
    """
    domain = (args.get("domain") or "").strip()
    if domain not in DOMAIN_MAP:
        return {
            "content": f"调用失败：未知 domain '{domain}'",
            "error": "unknown_domain",
            "available_domains": list(DOMAIN_MAP.keys()),
        }

    category = (args.get("category") or "").strip().lower()
    if category and category not in CATEGORIES:
        return {
            "content": f"调用失败：未知 category '{category}'",
            "error": "unknown_category",
            "available_categories": CATEGORIES,
        }

    query = (args.get("query") or "").strip()
    max_results = int(args.get("max_results") or DEFAULT_MAX_RESULTS)
    agent_id = args.get("agent_id") or "unknown_agent"

    domain_dir = KB_ROOT / DOMAIN_MAP[domain]
    if not domain_dir.exists():
        return {
            "content": f"调用失败：domain 目录不存在：{domain_dir}",
            "error": "domain_dir_not_found",
        }

    # 路由到对应查询模式
    if query:
        # 关键词查询（优先级高于 category）
        results = _search_by_keyword(domain_dir, query, max_results)
        mode = "keyword_search"
    elif category:
        # 按 category 列举
        results = _list_by_category(domain_dir, category, max_results)
        mode = "category_list"
    else:
        # 默认：返回 index.md 导航
        results = []
        mode = "index_only"

    # 读 index.md 摘要
    index_summary = _read_index_summary(domain_dir)
    # 读 log.md 最近 10 条
    recent_log = _read_recent_log(domain_dir, limit=10)

    return {
        "content": (
            f"查询完成：domain={domain} mode={mode} "
            f"results={len(results)} query='{query}' category='{category}'"
        ),
        "domain": domain,
        "mode": mode,
        "category": category or None,
        "query": query or None,
        "results": results,
        "total": len(results),
        "truncated": len(results) >= max_results,
        "index_summary": index_summary,
        "recent_log": recent_log,
        "agent_id": agent_id,
    }


# ==================== category 列举 ====================


def _list_by_category(domain_dir: Path, category: str, max_results: int) -> list[dict[str, Any]]:
    """按 category 列举页面。"""
    results: list[dict[str, Any]] = []

    if category == "patterns":
        # patterns.md 是单文件
        patterns_path = domain_dir / "patterns.md"
        if patterns_path.exists():
            results.append(_build_page_entry(patterns_path, domain_dir))
    elif category == "cases":
        # cases/ 是目录
        cases_dir = domain_dir / "cases"
        if cases_dir.exists():
            for md_file in sorted(cases_dir.glob("*.md"))[:max_results]:
                results.append(_build_page_entry(md_file, domain_dir))
    elif category == "entities":
        # entities/ 是目录
        entities_dir = domain_dir / "entities"
        if entities_dir.exists():
            for md_file in sorted(entities_dir.glob("*.md"))[:max_results]:
                results.append(_build_page_entry(md_file, domain_dir))
    elif category == "concepts":
        # concepts/ 是目录
        concepts_dir = domain_dir / "concepts"
        if concepts_dir.exists():
            for md_file in sorted(concepts_dir.glob("*.md"))[:max_results]:
                results.append(_build_page_entry(md_file, domain_dir))
    elif category == "comparisons":
        # comparisons/ 是目录
        comparisons_dir = domain_dir / "comparisons"
        if comparisons_dir.exists():
            for md_file in sorted(comparisons_dir.glob("*.md"))[:max_results]:
                results.append(_build_page_entry(md_file, domain_dir))

    return results


# ==================== 关键词搜索 ====================


def _search_by_keyword(domain_dir: Path, query: str, max_results: int) -> list[dict[str, Any]]:
    """按关键词在 domain 目录下搜索 md 文件（跳过 raw/）。"""
    results: list[dict[str, Any]] = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for md_file in domain_dir.rglob("*.md"):
        # 跳过 raw/（不可变层）和骨架文件
        if "raw" in md_file.parts:
            continue
        if md_file.name in ("index.md", "log.md", "AGENTS.md", "template.md"):
            continue

        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        matches: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.split("\n"), start=1):
            if pattern.search(line):
                idx = line.lower().find(query.lower())
                start = max(0, idx - 30)
                end = min(len(line), idx + len(query) + 30)
                context = (
                    ("..." if start > 0 else "")
                    + line[start:end]
                    + ("..." if end < len(line) else "")
                )
                matches.append({"line": line_no, "context": context})
                if len(matches) >= 3:  # 每个文件最多 3 处上下文
                    break

        if matches:
            results.append({
                "path": str(md_file.relative_to(domain_dir)),
                "title": _extract_title(text, md_file.stem),
                "matches": matches,
                "match_count": len(matches),
            })
            if len(results) >= max_results:
                break

    # 按匹配数降序
    results.sort(key=lambda x: x.get("match_count", 0), reverse=True)
    return results


# ==================== index.md / log.md 读取 ====================


def _read_index_summary(domain_dir: Path) -> dict[str, Any]:
    """读 index.md 摘要（章节标题 + 条目数）。"""
    index_path = domain_dir / "index.md"
    if not index_path.exists():
        return {"exists": False}

    try:
        text = index_path.read_text(encoding="utf-8")
    except OSError:
        return {"exists": False}

    # 提取章节 + 条目数
    sections: list[dict[str, Any]] = []
    current_section = None
    entry_count = 0

    for line in text.split("\n"):
        if line.startswith("## "):
            if current_section:
                sections.append({**current_section, "entry_count": entry_count})
            current_section = {"section": line[3:].strip(), "line": 0}
            entry_count = 0
        elif current_section and line.strip().startswith("- ["):
            entry_count += 1

    if current_section:
        sections.append({**current_section, "entry_count": entry_count})

    return {
        "exists": True,
        "path": "index.md",
        "sections": sections,
        "total_entries": sum(s.get("entry_count", 0) for s in sections),
    }


def _read_recent_log(domain_dir: Path, limit: int = 10) -> dict[str, Any]:
    """读 log.md 最近 N 条操作。"""
    log_path = domain_dir / "log.md"
    if not log_path.exists():
        return {"exists": False}

    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return {"exists": False}

    # 解析表格行（| timestamp | action | ...）
    rows: list[dict[str, str]] = []
    lines = text.split("\n")
    headers: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.split("|")[1:-1]]  # 去掉首尾空 cell
        if not headers:
            # 第一行是表头
            if cells and all(c and c != "---" and not all(ch == "-" for ch in c) for c in cells):
                headers = cells
            continue
        # 跳过分隔行 |---|---|...|
        if all(not c or all(ch == "-" for ch in c) for c in cells):
            continue
        if headers and len(cells) == len(headers):
            row = {headers[i]: cells[i] for i in range(len(headers))}
            rows.append(row)

    # 取最后 N 条（倒序）
    recent = rows[-limit:][::-1]

    return {
        "exists": True,
        "path": "log.md",
        "total_actions": len(rows),
        "recent": recent,
    }


# ==================== 辅助函数 ====================


def _build_page_entry(file_path: Path, domain_dir: Path) -> dict[str, Any]:
    """构造单个页面条目。"""
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""

    fm = _parse_frontmatter(file_path)
    return {
        "path": str(file_path.relative_to(domain_dir)),
        "title": _extract_title(text, file_path.stem),
        "frontmatter": fm,
        "size": file_path.stat().st_size,
        "preview": text[:500] + ("..." if len(text) > 500 else ""),
    }


def _extract_title(text: str, fallback: str) -> str:
    """从 markdown 提取标题（第一个 # 标题），失败返回 fallback。"""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


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
