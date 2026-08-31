"""P6: 域路由器 — 关键词匹配 + LLM 兜底分类。

路由流程:
  1. 关键词匹配 → 命中单个域 → 检查固定模板
  2. 0 或 ≥2 匹配 → LLM 兜底分类（当前返回 manager 动态编排）
  3. 有固定模板 → 路由到 workflow_id
  4. 无固定模板 → Manager 动态编排
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from orchestrator.config_loader import get_system_config


@dataclass
class RouteResult:
    """路由结果。"""
    domain: str | None                      # 命中的业务域（None = 未知）
    method: Literal["keyword", "llm", "fallback"]  # 路由方法
    workflow_id: str | None = None          # 固定模板（有则直接路由）
    needs_dynamic_dag: bool = False         # 是否需要 Manager 动态编排
    reason: str = ""


class DomainRouter:
    """域路由器 — 关键词匹配优先，LLM 兜底。"""

    def __init__(self, routes_config: dict[str, Any] | None = None):
        if routes_config is None:
            routes_config = get_system_config().routes
        # keyword_routes 格式: {domain: {keywords: [...]}} 或 {domain: [...]}
        raw_keyword_routes = routes_config.get("keyword_routes", {}) or {}
        self.keyword_routes: dict[str, list[str]] = {}
        for domain, val in raw_keyword_routes.items():
            if isinstance(val, dict):
                self.keyword_routes[domain] = val.get("keywords", []) or []
            elif isinstance(val, list):
                self.keyword_routes[domain] = val
            else:
                self.keyword_routes[domain] = []
        self.template_routes: dict[str, str] = routes_config.get("template_routes", {}) or {}
        self.llm_classify_prompt: str = routes_config.get("llm_classify_prompt", "")

    def route(self, user_message: str) -> RouteResult:
        """路由用户请求。

        P0.18.13：template_routes 优先级提升——只要消息命中固定模板，
        即使同时匹配多个 keyword 域也走模板（避免 smart_query 抢占「差旅报销」等模板入口）。
        """
        # 0. 固定模板优先（即使 keyword 多域命中也走模板）
        template_workflow = self._match_template(user_message)
        matched_domains = self._match_keywords(user_message)

        if template_workflow and not matched_domains:
            return RouteResult(
                domain=None,
                method="template",
                workflow_id=template_workflow,
                needs_dynamic_dag=False,
                reason=f"固定模板匹配 {template_workflow}",
            )
        if template_workflow and len(matched_domains) >= 1:
            return RouteResult(
                domain=matched_domains[0] if len(matched_domains) == 1 else None,
                method="template",
                workflow_id=template_workflow,
                needs_dynamic_dag=False,
                reason=f"固定模板 {template_workflow} 优先于关键词 {matched_domains}",
            )

        if len(matched_domains) == 1:
            domain = matched_domains[0]
            # 2. 检查固定模板
            workflow_id = self._match_template(user_message, domain)
            if workflow_id:
                return RouteResult(
                    domain=domain,
                    method="keyword",
                    workflow_id=workflow_id,
                    needs_dynamic_dag=False,
                    reason=f"关键词匹配域 {domain}，命中固定模板 {workflow_id}",
                )
            # 3. 无固定模板 → Manager 动态编排
            return RouteResult(
                domain=domain,
                method="keyword",
                needs_dynamic_dag=True,
                reason=f"关键词匹配域 {domain}，无固定模板，需动态编排",
            )

        if len(matched_domains) == 0:
            # 4. 无关键词匹配 → LLM 兜底（当前返回 manager）
            return RouteResult(
                domain=None,
                method="fallback",
                needs_dynamic_dag=True,
                reason="无关键词匹配，需 Manager 动态编排",
            )

        # 5. 多域匹配 → Manager 跨域编排
        return RouteResult(
            domain=None,
            method="fallback",
            needs_dynamic_dag=True,
            reason=f"多域匹配 {matched_domains}，需 Manager 跨域编排",
        )

    def _match_keywords(self, message: str) -> list[str]:
        """关键词匹配，返回命中的域列表。"""
        matched: list[str] = []
        for domain, keywords in self.keyword_routes.items():
            for kw in keywords:
                if kw in message:
                    matched.append(domain)
                    break  # 每个域只匹配一次
        return matched

    def _match_template(self, message: str, domain: str | None = None) -> str | None:
        """检查是否有固定模板匹配。domain 参数保留向后兼容（不影响匹配逻辑）。"""
        for pattern, workflow_id in self.template_routes.items():
            # pattern 可能是 "travel_expense" 这样的 ID
            # 检查消息中是否包含相关关键词
            if pattern in message or pattern.replace("_", "") in message:
                return workflow_id
        return None
