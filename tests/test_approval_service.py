"""ApprovalService 单元测试 — fail-closed 闭集语义（对齐 deepseek-harness dsh-user-approval）。

验证点：
  1. 无 SSE 订阅者 → unavailable（fail closed，不发出请求）
  2. 用户决定 allowed-once / rejected 正确传递
  3. 迟到/重复决定被丢弃（decide 返回 False）
  4. 等待超时 → unavailable
  5. cancel_pending → cancelled
  6. 审计事件对：每次 request 恰好产生 approval.requested + approval.decided 两条
  7. 权限级别 → sandbox 模式映射（P1）
"""
from __future__ import annotations

import asyncio
import pytest

from orchestrator.approval import (
    APPROVAL_OUTCOMES,
    DECIDABLE_OUTCOMES,
    ApprovalService,
)
from orchestrator.protocol import DagEventType
from orchestrator.workspace_paths import permission_level_to_sandbox_mode


class FakeSink:
    """收集 event_sink 收到的 DagEvent（模拟落库 + SSE 广播）。"""

    def __init__(self, *, fail: bool = False):
        self.events: list[tuple[str, dict]] = []
        self.fail = fail

    async def __call__(self, session_id: str, ev) -> None:
        if self.fail:
            raise RuntimeError("sink down")
        self.events.append((ev.type.value, dict(ev.payload or {})))


def make_service(subscribed: bool = True, sink: FakeSink | None = None):
    sink = sink or FakeSink()
    svc = ApprovalService(
        event_sink=sink,
        has_subscribers=lambda sid: subscribed,
    )
    return svc, sink


async def test_no_subscribers_fails_closed():
    """无 SSE 订阅者：不发请求，直接 unavailable（deepseek missing answerer 语义）。"""
    svc, sink = make_service(subscribed=False)
    outcome = await svc.request("s1", "bash", "test reason", timeout=1)
    assert outcome == "unavailable"
    assert sink.events == []  # 连审计对都不产生（请求从未发出）


async def test_allowed_once_decision():
    """用户允许 → allowed-once（唯一授权形态）。"""
    svc, sink = make_service()
    task = asyncio.create_task(svc.request("s1", "write_file", "tier 不足", timeout=5))
    await asyncio.sleep(0.05)  # 等 request 注册 pending
    assert svc.pending_count == 1
    assert svc.decide(list(svc._pending)[0], "allowed-once") is True
    outcome = await asyncio.wait_for(task, timeout=2)
    assert outcome == "allowed-once"
    # 审计事件对
    types = [t for t, _ in sink.events]
    assert types == ["approval.requested", "approval.decided"]
    assert sink.events[0][1]["tool_name"] == "write_file"
    assert sink.events[1][1]["outcome"] == "allowed-once"


async def test_rejected_decision():
    """用户拒绝 → rejected，调用方 fail closed。"""
    svc, _ = make_service()
    task = asyncio.create_task(svc.request("s1", "bash", "高危命令", timeout=5))
    await asyncio.sleep(0.05)
    rid = list(svc._pending)[0]
    assert svc.decide(rid, "rejected") is True
    assert await asyncio.wait_for(task, timeout=2) == "rejected"


async def test_late_decision_discarded():
    """迟到决定（超时后）被丢弃：decide 返回 False，不影响已完结结果。"""
    svc, _ = make_service()
    outcome = await svc.request("s1", "bash", "x", timeout=0.1)
    assert outcome == "unavailable"  # 超时
    assert svc.decide("nonexistent", "allowed-once") is False


async def test_duplicate_decide():
    """重复点击决定端点：第二次返回 False（幂等无害）。"""
    svc, _ = make_service()
    task = asyncio.create_task(svc.request("s1", "bash", "x", timeout=5))
    await asyncio.sleep(0.05)
    rid = list(svc._pending)[0]
    assert svc.decide(rid, "rejected") is True
    assert await asyncio.wait_for(task, timeout=2) == "rejected"
    assert svc.decide(rid, "rejected") is False  # 已完结


async def test_invalid_outcome_rejected_by_decide():
    """decide 只接受闭集子集（allowed-once/rejected），cancelled/unavailable 不可由用户给出。"""
    svc, _ = make_service()
    task = asyncio.create_task(svc.request("s1", "bash", "x", timeout=5))
    await asyncio.sleep(0.05)
    rid = list(svc._pending)[0]
    for bad in ("cancelled", "unavailable", "always", ""):
        assert svc.decide(rid, bad) is False
    assert svc.decide(rid, "allowed-once") is True  # 仍可正常决定
    assert await asyncio.wait_for(task, timeout=2) == "allowed-once"


async def test_timeout_fails_closed():
    """等待超时 → unavailable（fail closed）。"""
    svc, sink = make_service()
    outcome = await svc.request("s1", "bash", "x", timeout=0.15)
    assert outcome == "unavailable"
    # 超时也要补齐审计对（decided=unavailable）
    types = [t for t, _ in sink.events]
    assert types == ["approval.requested", "approval.decided"]
    assert sink.events[1][1]["outcome"] == "unavailable"


async def test_sink_failure_fails_closed():
    """审计事件发送失败 → unavailable（不允许无审计的放行）。"""
    svc, _ = make_service(sink=FakeSink(fail=True))
    outcome = await svc.request("s1", "bash", "x", timeout=1)
    assert outcome == "unavailable"


async def test_cancel_pending():
    """turn 结束清理：挂起请求落 cancelled。"""
    svc, sink = make_service()
    task = asyncio.create_task(svc.request("s1", "bash", "x", timeout=5))
    await asyncio.sleep(0.05)
    assert svc.cancel_pending("s1") == 1
    assert await asyncio.wait_for(task, timeout=2) == "cancelled"
    assert svc.cancel_pending("s1") == 0  # 幂等
    # cancelled 也有审计对
    assert sink.events[1][1]["outcome"] == "cancelled"


def test_outcome_vocabulary_closed():
    """闭集校验：四态穷尽，用户可决定的只有两态。"""
    assert APPROVAL_OUTCOMES == ("allowed-once", "rejected", "cancelled", "unavailable")
    assert DECIDABLE_OUTCOMES == ("allowed-once", "rejected")


def test_permission_level_to_sandbox_mode():
    """P1：权限级别 → codex sandbox 模式映射。"""
    assert permission_level_to_sandbox_mode("read_only") == "read-only"
    assert permission_level_to_sandbox_mode("read_write") == "workspace-write"
    assert permission_level_to_sandbox_mode("read_write_exec") == "workspace-write"
    assert permission_level_to_sandbox_mode("full_access") == "danger-full-access"
    # None = 未设置：harness 回退部署默认
    assert permission_level_to_sandbox_mode(None) is None
    assert permission_level_to_sandbox_mode("") is None
    # 未知值 fail-closed：宁可只读也不放大
    assert permission_level_to_sandbox_mode("bogus") == "read-only"
