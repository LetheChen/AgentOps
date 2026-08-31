"""Tavily Search 工具 —— 包装 Tavily Search API 为 AgentOps 平台级联网研究工具。

对应配置: config/tools/tavily_search.yaml
Skill 文档: skills/tavily-search/SKILL.md
设计动机: docs/reconstruction/DAG Workflow 规范与实现与skills业务关系.md §7.1

设计要点:
- 用 stdlib urllib 调用 https://api.tavily.com/search（零额外依赖，与 wecom_notify 一致）
- API key 从环境变量 TAVILY_API_KEY 读取（不引入 CredentialStore 复杂度——Tavily 不是 LLM provider）
- 返回结构与项目现有工具对齐：{"ok": bool, ...} + 失败时 {"error": <code>, "detail": <msg>}
- 错误分类（error code）：missing_api_key | network_error | http_error | rate_limited | parse_error
- 不做 LLM 提炼（那是 agent 的职责），工具只做"网络调用 + 结构化返回"
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Tavily REST API endpoint（官方文档 https://docs.tavily.com/docs/rest-api/api-reference）
_TAVILY_ENDPOINT = "https://api.tavily.com/search"

# HTTP 错误码 → 项目内 error code 映射
_RATE_LIMIT_CODES = {429}
_SERVER_ERROR_CODES = {500, 502, 503, 504}


async def tavily_search(
    args: dict[str, Any], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """调用 Tavily Search API 并返回结构化结果（async handler 接口）。

    Args:
        args: dict
            - query (str, required): 搜索关键词，自然语言
            - max_results (int, optional, default 5): 返回结果数 1-20
            - search_depth (str, optional, default "basic"): "basic" | "advanced"
            - topic (str, optional, default "general"): "general" | "news"
            - days (int, optional): 仅 topic="news" 生效，限定最近 N 天
            - include_answer (bool, optional, default false): 是否返回 Tavily 自生成汇总
            - include_domains (list[str], optional): 白名单域名
            - exclude_domains (list[str], optional): 黑名单域名
        config: handler 配置（来自 yaml handler.config；当前未使用，保留扩展位）

    Returns:
        成功: {"ok": True, "query", "answer"?, "results": [{"title", "url", "content", "score"}], "result_count"}
        失败: {"ok": False, "error": <code>, "detail": <msg>}
    """
    query = (args.get("query") or "").strip()
    if not query:
        return {
            "ok": False,
            "error": "invalid_input",
            "detail": "query 不能为空",
        }

    # 1. 读取 API key
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return {
            "ok": False,
            "error": "missing_api_key",
            "detail": "环境变量 TAVILY_API_KEY 未设置。请到 https://tavily.com 申请并配置（见 SKILL.md §四）。",
        }

    # 2. 组装 request body（仅透传 Tavily 支持的字段，不做二次解释）
    payload: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "max_results": int(args.get("max_results") or 5),
        "search_depth": args.get("search_depth") or "basic",
        "topic": args.get("topic") or "general",
        "include_answer": bool(args.get("include_answer", False)),
    }

    # 可选字段（仅在显式传入时附带，避免空 list 触发 Tavily 默认行为差异）
    if args.get("days") is not None:
        payload["days"] = int(args["days"])
    if args.get("include_domains"):
        payload["include_domains"] = list(args["include_domains"])
    if args.get("exclude_domains"):
        payload["exclude_domains"] = list(args["exclude_domains"])

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _TAVILY_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # 3. 发起请求（超时 30s，与 yaml handler.config.timeout_seconds 对齐）
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # HTTP 4xx/5xx：分类为 rate_limited / http_error
        code = e.code
        detail_body = ""
        try:
            detail_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001 — body 读取失败不影响错误分类
            pass
        if code in _RATE_LIMIT_CODES:
            return {
                "ok": False,
                "error": "rate_limited",
                "detail": f"Tavily 返回 429（触发频率限制）。{detail_body}",
            }
        if code in _SERVER_ERROR_CODES:
            return {
                "ok": False,
                "error": "http_error",
                "detail": f"Tavily 返回 {code}（服务端错误）。{detail_body}",
            }
        return {
            "ok": False,
            "error": "http_error",
            "detail": f"Tavily 返回 {code}。{detail_body}",
        }
    except urllib.error.URLError as e:
        # 网络层失败（DNS / 连接拒绝 / 超时）
        return {
            "ok": False,
            "error": "network_error",
            "detail": f"无法连接 Tavily API（{e.reason}）。检查网络/代理。",
        }
    except TimeoutError:
        return {
            "ok": False,
            "error": "network_error",
            "detail": "Tavily API 调用超时（>30s）。如果是 advanced 模式抓正文，考虑改 basic 或减少 max_results。",
        }
    except Exception as e:  # noqa: BLE001 — 最后兜底，避免工具崩溃影响上游节点
        logger.exception("tavily_search 未知异常")
        return {
            "ok": False,
            "error": "network_error",
            "detail": f"未预期异常: {type(e).__name__}: {e}",
        }

    # 4. 解析响应
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "error": "parse_error",
            "detail": f"Tavily 返回非 JSON（HTTP {status}）：{raw[:300]} ({e})",
        }

    results_raw = data.get("results") or []
    results = [
        {
            "title": (r.get("title") or "").strip(),
            "url": (r.get("url") or "").strip(),
            "content": (r.get("content") or "").strip(),
            "score": r.get("score"),
        }
        for r in results_raw
        if isinstance(r, dict)
    ]

    return {
        "ok": True,
        "query": query,
        "answer": (data.get("answer") or "").strip() or None,
        "results": results,
        "result_count": len(results),
    }


# 同步入口（CLI 调试用：python -m tools.tavily_search "query text"）
def _sync_entry(query: str, **kwargs: Any) -> dict[str, Any]:
    import asyncio

    return asyncio.run(tavily_search({"query": query, **kwargs}))


if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m tools.tavily_search <query> [--max-results N] [--advanced]")
        sys.exit(1)
    q = sys.argv[1]
    extra: dict[str, Any] = {}
    if "--max-results" in sys.argv:
        idx = sys.argv.index("--max-results")
        extra["max_results"] = int(sys.argv[idx + 1])
    if "--advanced" in sys.argv:
        extra["search_depth"] = "advanced"
    if "--answer" in sys.argv:
        extra["include_answer"] = True
    out = _sync_entry(q, **extra)
    print(json.dumps(out, ensure_ascii=False, indent=2))
