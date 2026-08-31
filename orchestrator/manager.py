"""P6: Manager Agent — 意图识别 + 任务分解 + 动态编排。

Manager 不执行具体业务任务，只做编排：
  1. DomainRouter 路由用户请求到业务域
  2. 有固定模板 → 路由到对应 workflow_id
  3. 无固定模板 → 生成 DynamicDagSpec → DagEngine 执行
  4. 聚合结果

动态 DAG 生成当前用规则方式（基于路由域），后续可接 LLM 生成复杂多节点 DAG。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from orchestrator.config_loader import get_system_config
from orchestrator.dynamic_dag import DynamicDagSpec, DynamicNodeSpec
from orchestrator.router import DomainRouter, RouteResult
from workflow.engine import DagEngine
from workflow.schema import WorkflowDefinition

logger = logging.getLogger(__name__)

EventSink = Callable[[Any], Awaitable[None]]


@dataclass
class ManagerResult:
    """Manager 编排结果。"""
    route: RouteResult                          # 路由结果
    run_id: str = ""                            # 执行的 run_id
    status: str = ""                            # completed / failed
    dynamic_dag_spec: DynamicDagSpec | None = None  # 动态 DAG（如果有）
    node_outputs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""                           # 聚合摘要
    error: str = ""


class ManagerAgent:
    """Manager Agent — 意图识别 + 任务分解 + 动态编排。

    不执行具体业务任务，只做编排。编排工具：
      - classify_intent: DomainRouter 路由
      - plan_tasks: 生成 DynamicDagSpec
      - dispatch: DagEngine 执行
      - aggregate: 结果聚合
    """

    def __init__(
        self,
        llm_config: dict[str, Any] | None = None,
        event_sink: EventSink | None = None,
        router: DomainRouter | None = None,
    ):
        self.llm_config = llm_config or {}
        self.event_sink = event_sink
        self.router = router or DomainRouter()

    async def handle(self, user_message: str) -> ManagerResult:
        """处理用户请求 — 完整编排流程。"""
        # 1. 意图识别（classify_intent）
        route = self.router.route(user_message)
        logger.info("Manager route: domain=%s method=%s reason=%s",
                     route.domain, route.method, route.reason)

        # 2. 有固定模板 → 直接路由
        if route.workflow_id:
            return await self._dispatch_template(route, user_message)

        # 3. 需要动态编排 → 生成 DAG + 执行
        if route.needs_dynamic_dag:
            return await self._dispatch_dynamic(route, user_message)

        # 4. 兜底
        return ManagerResult(
            route=route,
            status="failed",
            error=f"无法路由请求: {route.reason}",
        )

    async def _dispatch_template(self, route: RouteResult, user_message: str) -> ManagerResult:
        """固定模板路由 — 加载 workflow 执行。"""
        # 模板执行由调用方（LocalSdkOrchestrator）处理
        # Manager 只返回路由结果
        return ManagerResult(
            route=route,
            status="routed",
            summary=f"已路由到固定模板 {route.workflow_id}",
        )

    async def _dispatch_dynamic(self, route: RouteResult, user_message: str) -> ManagerResult:
        """动态编排 — plan_tasks + dispatch + aggregate。"""
        # 1. plan_tasks: 生成 DynamicDagSpec
        spec = self._generate_dag(route, user_message)
        errors = spec.validate()
        if errors:
            return ManagerResult(
                route=route,
                status="failed",
                error=f"动态 DAG 校验失败: {errors}",
            )

        # 2. dispatch: 转 WorkflowDefinition + DagEngine 执行
        try:
            wf_def = spec.to_workflow_def()
        except ValueError as e:
            return ManagerResult(
                route=route,
                status="failed",
                error=str(e),
                dynamic_dag_spec=spec,
            )

        engine = DagEngine(
            wf_def,
            event_sink=self.event_sink,
            llm_config=self.llm_config,
        )

        run_state = await engine.run(inputs={"user_message": user_message})

        # 3. aggregate: 聚合结果
        summary = self._aggregate(run_state.node_outputs)

        return ManagerResult(
            route=route,
            run_id=run_state.run_id,
            status=run_state.status.value if hasattr(run_state.status, 'value') else str(run_state.status),
            dynamic_dag_spec=spec,
            node_outputs=run_state.node_outputs,
            summary=summary,
            error=run_state.error or "",
        )

    def _generate_dag(self, route: RouteResult, user_message: str) -> DynamicDagSpec:
        """生成动态 DAG（plan_tasks）。

        当前用规则方式：
          - 单域 → 单节点 DAG
          - 无域/多域 → manager + 子节点 DAG
        后续可接 LLM 生成复杂多节点 DAG。
        """
        if route.domain:
            # 单域 → 单节点
            return DynamicDagSpec(
                nodes=[
                    DynamicNodeSpec(
                        id="step_1",
                        agent_domain=route.domain,
                        task_description=user_message[:200],
                    ),
                ],
            )
        else:
            # 无域/多域 → 需要用户澄清或 LLM 分解
            # 当前返回空 DAG（无法处理）
            return DynamicDagSpec(
                nodes=[
                    DynamicNodeSpec(
                        id="step_1",
                        agent_domain="smart_query",  # 兜底到问数域
                        task_description=user_message[:200],
                    ),
                ],
            )

    def _aggregate(self, node_outputs: dict[str, Any]) -> str:
        """聚合子 Agent 输出（aggregate）。

        当前简单拼接，后续可接 LLM 生成摘要。
        """
        if not node_outputs:
            return "无输出"
        parts = []
        for node_id, output in node_outputs.items():
            if isinstance(output, dict):
                text = output.get("text", output.get("summary", str(output)[:100]))
            else:
                text = str(output)[:100]
            parts.append(f"[{node_id}] {text}")
        return "\n".join(parts)
