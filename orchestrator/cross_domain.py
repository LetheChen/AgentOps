"""P7: 跨域协调协议 — 子 Agent 跨域请求经 Manager 中转。

核心原则：禁止子 Agent 间直接调用，所有跨域请求必须经 Manager 中转。

流程:
  1. 子 Agent 调用 request_cross_domain 工具
  2. CrossDomainCoordinator.handle_request() 中转:
     a. 权限校验（caller 有 request_cross_domain 权限）
     b. 自调用 / manager 作为 target 拒绝
     c. 查找目标域 Agent
     d. 生成单节点 DynamicDagSpec → DagEngine 执行
     e. 审计事件留痕
  3. 结果回传给子 Agent

审计事件流（通过 event_sink emit DagEventType.CROSS_DOMAIN）:
  cross_domain.requested → manager.received → manager.dispatched
  → target.node_started → target.node_completed → manager.returned

失败路径额外事件: cross_domain.denied / cross_domain.failed
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from orchestrator.config_loader import get_system_config
from orchestrator.dynamic_dag import DynamicDagSpec, DynamicNodeSpec
from orchestrator.permission_engine import PermissionEngine
from orchestrator.protocol import DagEvent, DagEventType
from workflow.engine import DagEngine

logger = logging.getLogger(__name__)

EventSink = Callable[[Any], Awaitable[None]]


# ====== 数据结构 ======

@dataclass
class CrossDomainRequest:
    """跨域请求。"""
    request_id: str
    caller_agent: str               # smart_query
    caller_domain: str              # smart_query
    target_domain: str              # smart_ops
    request_message: str
    priority: str = "normal"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class CrossDomainResult:
    """跨域响应。"""
    request_id: str
    status: str                     # completed / failed / denied
    target_agent: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    result_text: str = ""
    error: str = ""


# ====== 跨域协调器 ======

class CrossDomainCoordinator:
    """跨域协调器 — Manager 持有，处理子 Agent 跨域请求中转。

    核心原则：禁止子 Agent 间直接调用，所有跨域请求必须经此中转。
    """

    def __init__(
        self,
        llm_config: dict[str, Any] | None = None,
        event_sink: EventSink | None = None,
        permission_engine: PermissionEngine | None = None,
    ):
        self.llm_config = llm_config or {}
        self.event_sink = event_sink
        self.permission_engine = permission_engine or PermissionEngine(
            get_system_config()
        )

    async def handle_request(
        self,
        caller_agent: str,
        target_domain: str,
        request_message: str,
        priority: str = "normal",
        parent_run_id: str = "",
    ) -> CrossDomainResult:
        """处理跨域请求 — 完整中转流程。

        Args:
            caller_agent: 发起方 Agent ID（如 smart_query）
            target_domain: 目标业务域（如 smart_ops）
            request_message: 跨域请求内容
            priority: 优先级 high/normal/low
            parent_run_id: 父 run_id（用于事件关联，可选）
        """
        # 1. 生成 request_id
        request_id = f"xd_{int(time.time())}_{uuid4().hex[:8]}"

        # 查找 caller 的 domain
        config = get_system_config()
        caller_def = config.agents.get(caller_agent)
        caller_domain = caller_def.domain if caller_def else "unknown"

        request = CrossDomainRequest(
            request_id=request_id,
            caller_agent=caller_agent,
            caller_domain=caller_domain,
            target_domain=target_domain,
            request_message=request_message,
            priority=priority,
        )

        # 2. 权限校验：caller 有 request_cross_domain 工具权限
        perm = self.permission_engine.check_tool_access(
            caller_agent, "request_cross_domain"
        )
        if not perm.allowed:
            await self._emit_event(
                "denied", request, parent_run_id, error=perm.reason
            )
            return CrossDomainResult(
                request_id=request_id,
                status="denied",
                error=f"权限拒绝: {perm.reason}",
            )

        # 3. 自调用拒绝（caller 域 == target 域）
        if caller_domain == target_domain:
            msg = f"自调用拒绝: caller 域 '{caller_domain}' == target 域 '{target_domain}'"
            await self._emit_event("denied", request, parent_run_id, error=msg)
            return CrossDomainResult(
                request_id=request_id, status="denied", error=msg
            )

        # 4. manager 域不能作为 target（Manager 不执行业务）
        if target_domain == "manager":
            msg = "manager 域不作为跨域目标（Manager 只做编排）"
            await self._emit_event("denied", request, parent_run_id, error=msg)
            return CrossDomainResult(
                request_id=request_id, status="denied", error=msg
            )

        # 5. emit cross_domain.requested
        await self._emit_event("requested", request, parent_run_id)

        # 6. emit manager.received
        await self._emit_event("received", request, parent_run_id)

        # 7. 查找目标域 Agent
        target_agent = self._find_agent_by_domain(target_domain)
        if target_agent is None:
            msg = f"目标域 '{target_domain}' 无可用 Agent"
            await self._emit_event(
                "failed", request, parent_run_id, error=msg
            )
            return CrossDomainResult(
                request_id=request_id, status="failed", error=msg
            )

        # 8. emit manager.dispatched
        await self._emit_event(
            "dispatched", request, parent_run_id, target_agent=target_agent
        )

        # 9. emit target.node_started
        await self._emit_event(
            "target_started", request, parent_run_id, target_agent=target_agent
        )

        # 10. 执行目标域 Agent
        try:
            run_result = await self._dispatch_to_target(
                target_domain, request_message
            )
        except Exception as e:
            logger.exception("Cross-domain dispatch failed")
            await self._emit_event(
                "failed", request, parent_run_id,
                target_agent=target_agent, error=str(e),
            )
            return CrossDomainResult(
                request_id=request_id,
                status="failed",
                target_agent=target_agent,
                error=str(e),
            )

        # 11. emit target.node_completed
        result_text = run_result.get("summary", str(run_result))
        await self._emit_event(
            "target_completed", request, parent_run_id,
            target_agent=target_agent, result_summary=result_text[:200],
        )

        # 12. emit manager.returned
        await self._emit_event(
            "returned", request, parent_run_id,
            target_agent=target_agent, result_summary=result_text[:200],
        )

        return CrossDomainResult(
            request_id=request_id,
            status="completed",
            target_agent=target_agent,
            result=run_result,
            result_text=result_text,
        )

    async def _dispatch_to_target(
        self, target_domain: str, request_message: str
    ) -> dict[str, Any]:
        """派发到目标域 Agent 执行 — 生成单节点 DAG → DagEngine。"""
        spec = DynamicDagSpec(
            nodes=[
                DynamicNodeSpec(
                    id="cross_domain_step",
                    agent_domain=target_domain,
                    task_description=f"[跨域请求] {request_message[:200]}",
                ),
            ],
        )

        # 校验
        errors = spec.validate()
        if errors:
            raise ValueError(f"动态 DAG 校验失败: {errors}")

        # 转 WorkflowDefinition + 执行
        wf_def = spec.to_workflow_def()
        engine = DagEngine(
            wf_def,
            event_sink=self.event_sink,
            llm_config=self.llm_config,
        )

        run_state = await engine.run(inputs={"user_message": request_message})

        # 提取输出
        outputs = run_state.node_outputs or {}
        node_output = outputs.get("cross_domain_step", {})

        return {
            "summary": node_output.get(
                "text", node_output.get("summary", str(node_output))
            ),
            "run_id": run_state.run_id,
            "status": run_state.status.value
            if hasattr(run_state.status, "value")
            else str(run_state.status),
            "node_outputs": outputs,
        }

    def _find_agent_by_domain(self, domain: str) -> str | None:
        """按域查找 Agent。优先返回与 domain 同名的主 Agent，否则返回第一个匹配。

        例如 smart_ops 域有 smart_ops + log_analyst 两个 agent，
        优先返回 smart_ops（与 domain 同名的主 agent）。
        """
        config = get_system_config()
        # 优先返回与 domain 同名的主 agent
        primary = config.agents.get(domain)
        if primary and primary.domain == domain:
            return primary.agent_id
        # 否则返回第一个属于该域的 agent
        for agent in config.agents.values():
            if agent.domain == domain:
                return agent.agent_id
        return None

    async def _emit_event(
        self,
        event_subtype: str,
        request: CrossDomainRequest,
        run_id: str = "",
        target_agent: str = "",
        error: str = "",
        result_summary: str = "",
    ):
        """emit 跨域审计事件。

        event_subtype:
          requested / received / dispatched / target_started /
          target_completed / returned / denied / failed
        """
        if self.event_sink is None:
            return

        payload: dict[str, Any] = {
            "event_subtype": event_subtype,
            "request_id": request.request_id,
            "caller_agent": request.caller_agent,
            "caller_domain": request.caller_domain,
            "target_domain": request.target_domain,
            "target_agent": target_agent,
            "request_message": request.request_message[:200],
            "priority": request.priority,
            "created_at": request.created_at,
        }
        if error:
            payload["error"] = error
        if result_summary:
            payload["result_summary"] = result_summary

        await self.event_sink(
            DagEvent(
                type=DagEventType.CROSS_DOMAIN,
                run_id=run_id,
                node_id=f"cross_domain:{request.request_id}:{event_subtype}",
                payload=payload,
                sequence=0,
            )
        )

    def make_tool_handler(self, caller_agent: str, parent_run_id: str = ""):
        """为指定 Agent 创建 request_cross_domain 工具 handler。

        返回符合 ConversationalEngine 工具签名的 (args) -> dict 闭包。
        """

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return await request_cross_domain_tool(
                args,
                caller_agent=caller_agent,
                coordinator=self,
                parent_run_id=parent_run_id,
            )

        return handler


# ====== 工具入口（子 Agent 调用） ======

async def request_cross_domain_tool(
    args: dict[str, Any],
    caller_agent: str = "",
    coordinator: CrossDomainCoordinator | None = None,
    parent_run_id: str = "",
) -> dict[str, Any]:
    """request_cross_domain 工具 handler — 子 Agent 跨域请求入口。

    Args:
        args: 工具参数 {target_domain, request, priority?}
        caller_agent: 调用方 Agent ID
        coordinator: 跨域协调器（由 Manager 注入）
        parent_run_id: 父 run_id（用于事件关联）

    Returns:
        {content, status, request_id, result?}
    """
    if coordinator is None:
        return {"content": "错误：跨域协调器未初始化", "status": "failed"}

    target_domain = args.get("target_domain", "")
    request_message = args.get("request", "")
    priority = args.get("priority", "normal")

    if not target_domain or not request_message:
        return {
            "content": "错误：target_domain 和 request 必填",
            "status": "failed",
        }

    result = await coordinator.handle_request(
        caller_agent=caller_agent,
        target_domain=target_domain,
        request_message=request_message,
        priority=priority,
        parent_run_id=parent_run_id,
    )

    if result.status == "completed":
        return {
            "content": result.result_text,
            "status": "completed",
            "request_id": result.request_id,
            "target_agent": result.target_agent,
        }
    return {
        "content": f"跨域请求失败: {result.error}",
        "status": result.status,
        "request_id": result.request_id,
        "error": result.error,
    }
