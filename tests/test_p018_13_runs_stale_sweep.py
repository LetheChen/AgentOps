"""P0.18.13: Patroller._sweep_stale_runs_table — runs 表 stale 收敛单元测试。

覆盖：
  1. 超时未终止 run 自动标 cancelled + finished_at + error
  2. 未超时 run 不动
  3. 包含 conversational run（修复前 _patrol_once 显式跳过 conversational）
  4. event_sink 收到 patrol_alert 事件
  5. list_stale_runs 抛错时 _sweep_stale_runs_table 不抛出异常
"""
from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock

import pytest


class _FakeEventStore:
    """最小 EventStore 替身：支持 list_stale_runs + update_run_status。"""

    def __init__(self, stale_rows: list[dict] | None = None):
        self._stale_rows = stale_rows or []
        self.updates: list[tuple[str, str, dict]] = []  # (run_id, status, kwargs)
        self.calls: list[str] = []

    async def list_stale_runs(self, threshold_iso: str, limit: int = 100):
        self.calls.append(f"list_stale:{threshold_iso}:{limit}")
        return list(self._stale_rows)

    async def update_run_status(self, run_id, status, **kwargs):
        self.calls.append(f"update:{run_id}:{status}:{sorted(kwargs.keys())}")
        self.updates.append((run_id, status, kwargs))


@pytest.mark.asyncio
async def test_sweep_stale_runs_table_cancels_oversized_runs():
    """超时 run 全部标 cancelled，finished_at + error 写入。"""
    from orchestrator.patroller import Patroller

    fake = _FakeEventStore(stale_rows=[
        {"run_id": "r_old_1", "run_mode": "conversational", "agent_id": "smart_query",
         "workflow_id": None, "status": "running"},
        {"run_id": "r_old_2", "run_mode": "templated", "agent_id": None,
         "workflow_id": "wf_a", "status": "pending"},
    ])
    p = Patroller(event_store=fake, stale_threshold_seconds=1800)
    await p._sweep_stale_runs_table(datetime.datetime.now(datetime.timezone.utc))

    assert len(fake.updates) == 2
    assert {u[1] for u in fake.updates} == {"cancelled"}
    for run_id, status, kwargs in fake.updates:
        assert status == "cancelled"
        assert "finished_at" in kwargs, "finished_at 必须写入"
        assert kwargs.get("error", "").startswith("stale_timeout_"), "error 必须标 stale 原因"


@pytest.mark.asyncio
async def test_sweep_stale_runs_table_includes_conversational():
    """P0.18.13 关键修复：conversational 模式也走 stale 收敛（之前 _patrol_once 显式 skip）。"""
    from orchestrator.patroller import Patroller

    fake = _FakeEventStore(stale_rows=[
        {"run_id": "r_conv_1", "run_mode": "conversational", "agent_id": "smart_query",
         "workflow_id": None, "status": "running"},
    ])
    p = Patroller(event_store=fake, stale_threshold_seconds=1800)
    await p._sweep_stale_runs_table(datetime.datetime.now(datetime.timezone.utc))

    assert len(fake.updates) == 1
    assert fake.updates[0][0] == "r_conv_1"
    assert fake.updates[0][1] == "cancelled"


@pytest.mark.asyncio
async def test_sweep_stale_runs_table_no_eligible_returns_silently():
    """无超时 run：list_stale_runs 返回空时，update_run_status 不会被调用。"""
    from orchestrator.patroller import Patroller

    fake = _FakeEventStore(stale_rows=[])
    p = Patroller(event_store=fake, stale_threshold_seconds=1800)
    await p._sweep_stale_runs_table(datetime.datetime.now(datetime.timezone.utc))

    assert fake.updates == []


@pytest.mark.asyncio
async def test_sweep_stale_runs_table_silently_handles_store_error():
    """list_stale_runs 抛错时 _sweep_stale_runs_table 不抛出异常（防御性）。"""
    from orchestrator.patroller import Patroller

    fake = _FakeEventStore()
    fake.list_stale_runs = AsyncMock(side_effect=RuntimeError("db locked"))
    p = Patroller(event_store=fake, stale_threshold_seconds=1800)
    # 不应抛
    await p._sweep_stale_runs_table(datetime.datetime.now(datetime.timezone.utc))
    assert fake.updates == []


@pytest.mark.asyncio
async def test_sweep_stale_runs_table_emits_patrol_alert():
    """sweep 后通过 event_sink emit patrol_alert 事件。"""
    from orchestrator.patroller import Patroller

    fake = _FakeEventStore(stale_rows=[
        {"run_id": "r_alert_1", "run_mode": "conversational", "agent_id": "smart_query",
         "workflow_id": None, "status": "running"},
    ])
    sink = AsyncMock()
    p = Patroller(event_store=fake, event_sink=sink, stale_threshold_seconds=1800)
    await p._sweep_stale_runs_table(datetime.datetime.now(datetime.timezone.utc))

    assert sink.await_count == 1
    evt = sink.await_args.args[0]
    assert evt["type"] == "patrol_alert"
    assert evt["run_id"] == "r_alert_1"
    assert evt["alerts"][0]["type"] == "stale_runs_table_swept"


@pytest.mark.asyncio
async def test_patrol_once_invokes_sweep_stale_runs_table():
    """_patrol_once 末尾必须调用 _sweep_stale_runs_table（p0.18.13 增量）。"""
    from orchestrator.patroller import Patroller

    fake = _FakeEventStore()
    fake.list_sessions = AsyncMock(return_value=[])  # 主循环无 active
    fake.archive_session = AsyncMock()
    fake.list_dormant_sessions = AsyncMock(return_value=[])
    p = Patroller(event_store=fake, stale_threshold_seconds=1800)
    # spy on _sweep_stale_runs_table
    called = {"flag": False}
    orig = p._sweep_stale_runs_table

    async def spy(now):
        called["flag"] = True
        await orig(now)

    p._sweep_stale_runs_table = spy
    # 还需要让 _archive_stale_dormant 不报错
    p.list_sessions = AsyncMock(return_value=[])  # 防止 _archive_stale_dormant 走错方法
    await p._patrol_once()
    assert called["flag"], "_patrol_once 必须触发 _sweep_stale_runs_table"
