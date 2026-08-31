"""User Approval — fail-closed 审批闭环（对齐 deepseek-harness dsh-user-approval）。

核心语义（照搬 deepseek 的闭集设计）：
  ApprovalOutcome = allowed-once | rejected | cancelled | unavailable
  - allowed-once 是唯一授权形态：只覆盖被问的那一次动作，不放大会话常驻权限
  - 无 SSE 订阅者 / 等待超时 / 校验器故障 → unavailable，调用方 fail closed
  - 审计事件对 approval.requested + approval.decided 经 event_sink 落库（session_events 表）
    并广播 SSE —— 前端弹窗与审计轨迹同一条通道，不重复建设

与 deepseek 的差异（架构映射）：
  deepseek: ctx.approval.request() → answerer waterfall → session log 审计对
  AgentOps: ApprovalService.request() → SSE 前端 answerer → session_events 审计对
  审批策略（ask/never）由会话权限级别隐式承担：设置了权限级别 = ask，
  full_access = never（tier 校验直接放行，不产生审批请求）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from orchestrator.protocol import DagEvent, DagEventType

logger = logging.getLogger(__name__)

# 闭集：allowed-once 是唯一授权
APPROVAL_OUTCOMES: tuple[str, ...] = (
    "allowed-once", "rejected", "cancelled", "unavailable",
)

# 决定端点只接受这两个值（cancelled/unavailable 由服务侧产生）
DECIDABLE_OUTCOMES: tuple[str, ...] = ("allowed-once", "rejected")

# 默认等待用户决定的时长（秒）。超时 = unavailable（fail closed）。
# 可被 request(timeout=...) 覆盖。
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120


class ApprovalService:
    """会话级审批服务：request 阻塞等待用户决定，decide 由 API 端点驱动。

    线程安全：单事件循环内使用（FastAPI + SessionEngine 同循环）。
    """

    def __init__(
        self,
        event_sink: Callable[[str, DagEvent], Awaitable[None]],
        has_subscribers: Callable[[str], bool],
    ):
        """注入 server 侧能力（分层：本模块不 import api）。

        event_sink: (session_id, DagEvent) -> None，负责落库 + SSE 广播
        has_subscribers: session 是否有活跃 SSE 连接（无连接直接 unavailable）
        """
        self._event_sink = event_sink
        self._has_subscribers = has_subscribers
        # request_id -> {future, session_id, tool_name}
        self._pending: dict[str, dict[str, Any]] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def request(
        self,
        session_id: str,
        tool_name: str,
        reason: str = "",
        *,
        call_id: str | None = None,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> str:
        """发起一次审批请求并等待决定。返回 ApprovalOutcome（闭集）。

        fail-closed 路径全部返回 unavailable：
        - 无 SSE 订阅者（前端没连着，问了也白问）
        - 等待超时
        - event_sink 落库/广播异常（审计对残缺即拒绝，与 deepseek 语义一致）
        """
        # 无订阅者：不发请求直接拒绝（deepseek "missing answerer → unavailable"）
        if not self._has_subscribers(session_id):
            logger.info(
                "approval request session=%s tool=%s 无 SSE 订阅者，fail closed",
                session_id, tool_name,
            )
            return "unavailable"

        request_id = uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = {
            "future": future,
            "session_id": session_id,
            "tool_name": tool_name,
        }

        # 审计事件 1/2：approval.requested（落库 + SSE 弹窗）
        try:
            await self._event_sink(session_id, DagEvent(
                type=DagEventType.APPROVAL_REQUESTED,
                run_id=session_id,
                payload={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "reason": reason,
                    "call_id": call_id,
                },
                sequence=0,
            ))
        except Exception as e:
            # 审计对的第一条都发不出去 → 不允许在没有审计的情况下放行
            self._pending.pop(request_id, None)
            logger.warning("approval.requested 发送失败 session=%s: %s", session_id, e)
            return "unavailable"

        # 等待决定
        outcome = "unavailable"
        try:
            outcome = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            outcome = "unavailable"
            logger.info(
                "approval request %s session=%s tool=%s 超时 %ss，fail closed",
                request_id, session_id, tool_name, timeout,
            )
        except Exception as e:
            outcome = "unavailable"
            logger.warning("approval request %s 等待异常: %s", request_id, e)
        finally:
            self._pending.pop(request_id, None)

        # 审计事件 2/2：approval.decided（无论结果如何都补齐审计对）
        try:
            await self._event_sink(session_id, DagEvent(
                type=DagEventType.APPROVAL_DECIDED,
                run_id=session_id,
                payload={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "outcome": outcome,
                },
                sequence=0,
            ))
        except Exception as e:
            # 审计对不完整 → 已拿到的决定也不能用（deepseek "returning an
            # unlogged decision would violate the pair"）
            logger.warning("approval.decided 发送失败 %s: %s", request_id, e)
            return "unavailable"

        return outcome

    def decide(self, request_id: str, outcome: str) -> bool:
        """API 端点驱动：用户点了「允许本次」或「拒绝」。

        返回 False = 未知/已完结的请求（重复点击、超时后的迟到决定）。
        迟到决定被丢弃（future 已清理），与 deepseek "late answer discarded" 一致。
        """
        if outcome not in DECIDABLE_OUTCOMES:
            return False
        entry = self._pending.get(request_id)
        if not entry:
            return False
        future: asyncio.Future = entry["future"]
        if not future.done():
            future.set_result(outcome)
            logger.info(
                "approval %s decided=%s tool=%s session=%s",
                request_id, outcome, entry["tool_name"], entry["session_id"],
            )
        return True

    def cancel_pending(self, session_id: str) -> int:
        """session 关闭/取消时：所有挂起请求落 cancelled。返回清理数量。"""
        cancelled = 0
        for request_id, entry in list(self._pending.items()):
            if entry["session_id"] != session_id:
                continue
            future: asyncio.Future = entry["future"]
            if not future.done():
                future.set_result("cancelled")
            del self._pending[request_id]
            cancelled += 1
        if cancelled:
            logger.info("session=%s 取消 %s 个挂起审批", session_id, cancelled)
        return cancelled
