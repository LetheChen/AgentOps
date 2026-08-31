"""EventStore 测试 — 验证 v3 三层架构（sessions + runs + subagents）。

v3 改造（2026-08-09）：
  - sessions 只装对话层（active/dormant/archived）
  - runs 表独立（DAG 执行实例，含 workflow_id/run_mode/inputs/final_outputs/status/tokens 等）
  - subagents 表独立（一次性执行体，含 actor_id/run_id/node_id/lease_generation/harness_type/status）
  - dag_events → run_events（FK to runs）
  - parent_child_sessions → parent_child_runs
  - session_memory.source_session_id → source_run_id
  - 方法名：init_session/create_session, init_run/finalize_run, append_run_event/get_run_events,
    record_parent_child_run/list_child_runs_of_session

覆盖 P0 验收标准：
- audit.db 自动创建，核心表存在（sessions/runs/run_events/raw_harness_events/widget_inputs）
- Session 创建 + Run 创建 + 事件落库
- 服务重启后数据不丢失
- 节点详情聚合正确
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from audit import EventStore, SqliteEventStore
from orchestrator.protocol import DagEventType


@pytest_asyncio.fixture
async def store(tmp_path):
    """每个测试用独立临时 db 文件。"""
    db_path = tmp_path / "test_audit.db"
    s = SqliteEventStore(str(db_path))
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_store_creates_tables(store):
    """audit.db 自动创建，核心 v3 表存在。"""
    import sqlite3
    conn = sqlite3.connect(str(store.db_path))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    # v3 核心表：sessions（对话层）+ runs（DAG 执行层）+ subagents（执行体）
    assert {"sessions", "runs", "run_events", "subagents", "raw_harness_events", "widget_inputs"} <= tables
    assert "parent_child_runs" in tables
    assert "run_memory" in tables
    assert "session_memory" in tables
    assert "session_messages" in tables
    assert "session_events" in tables
    assert "handoffs" in tables
    assert "node_executions" in tables


@pytest.mark.asyncio
async def test_create_session_and_init_run(store):
    """v3: session 创建（对话层）+ run 创建（DAG 执行层）双层写入。"""
    # 1. 先创建 session（对话层）
    await store.create_session(
        session_id="sess_test_1",
        agent_id="manager",
        user_id="alice",
        title="test session",
    )
    sessions = await store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess_test_1"
    assert sessions[0]["agent_id"] == "manager"
    assert sessions[0]["status"] == "active"

    # 2. 在该 session 下创建 run（DAG 执行层）
    await store.init_run(
        run_id="run_test_1",
        session_id="sess_test_1",
        workflow_id="hello-world",
        run_mode="templated",
        inputs={"x": 1},
    )
    runs = await store.list_runs(session_id="sess_test_1")
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run_test_1"
    assert runs[0]["session_id"] == "sess_test_1"
    assert runs[0]["workflow_id"] == "hello-world"
    assert runs[0]["status"] == "pending"

    # 3. finalize_run 标 completed
    await store.finalize_run(
        "run_test_1", "completed",
        total_tokens_in=100, total_tokens_out=200,
        final_outputs={"node_a": {"result": "ok"}},
    )
    summary = await store.get_run_summary("run_test_1")
    assert summary["status"] == "completed"
    assert summary["total_tokens_in"] == 100
    assert summary["total_tokens_out"] == 200
    assert summary["final_outputs"]["node_a"]["result"] == "ok"


@pytest.mark.asyncio
async def test_append_and_query_run_events(store):
    """v3: DagEvent 写入 run_events 表 + 按 sequence 查询。"""
    await store.create_session("sess_e1", agent_id="manager")
    await store.init_run("run_e1", session_id="sess_e1", workflow_id="wf", run_mode="templated")
    for i in range(1, 6):
        await store.append_run_event(
            run_id="run_e1",
            event_type=DagEventType.NODE_STARTED.value if i % 2 == 0 else DagEventType.NODE_COMPLETED.value,
            node_id=f"node_{i}",
            payload={"step": i},
        )

    events = await store.get_run_events("run_e1")
    assert len(events) == 5
    assert events[0].sequence == 1
    assert events[-1].sequence == 5
    assert events[0].node_id == "node_1"

    # since 过滤
    events_since_3 = await store.get_run_events("run_e1", since=3)
    assert len(events_since_3) == 2
    assert events_since_3[0].sequence == 4


@pytest.mark.asyncio
async def test_append_raw_harness_event_v3(store):
    """v3: RawHarnessEvent 落库需要 run_id + subagent_id（FK 必填）。"""
    await store.create_session("sess_r1", agent_id="manager")
    await store.init_run("run_r1", session_id="sess_r1", workflow_id="wf", run_mode="templated")
    # v3: 节点执行前先 provision subagent
    await store.provision_subagent(
        subagent_id="sub_r1_a",
        actor_id="run_r1:node_a",
        run_id="run_r1",
        node_id="node_a",
        harness_type="opencode",
    )
    # v3: append_raw_event 签名需要 run_id + subagent_id + node_id
    await store.append_raw_event(
        run_id="run_r1",
        subagent_id="sub_r1_a",
        node_id="node_a",
        harness="opencode",
        event_type="tool.call",
        raw_payload={"tool": "search", "args": {"q": "test"}},
    )
    # 通过 node_detail 验证 raw 事件被查到
    detail = await store.get_node_detail("run_r1", "node_a")
    assert len(detail["raw_events"]) == 1
    assert detail["raw_events"][0]["harness"] == "opencode"


@pytest.mark.asyncio
async def test_widget_input_persisted_v3(store):
    """v3: widget_inputs 表 FK to runs + sessions。"""
    await store.create_session("sess_w1", agent_id="manager")
    await store.init_run("run_w1", session_id="sess_w1", workflow_id="wf", run_mode="templated")
    await store.append_widget_input(
        run_id="run_w1",
        widget_id="form_1",
        payload={"value": "approved"},
        session_id="sess_w1",
        user_id="alice",
    )
    runs = await store.list_runs()
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_data_survives_reopen(tmp_path):
    """服务重启后数据不丢失：用新 store 实例指向同一文件。"""
    db_path = str(tmp_path / "persist.db")
    s1 = SqliteEventStore(db_path)
    await s1.create_session("sess_p1", agent_id="manager")
    await s1.init_run("run_p1", session_id="sess_p1", workflow_id="wf", run_mode="templated", inputs={"k": "v"})
    await s1.append_run_event(
        run_id="run_p1",
        event_type=DagEventType.RUN_CREATED.value,
        payload={"init": True},
    )
    await s1.close()

    # 新实例，模拟重启
    s2 = SqliteEventStore(db_path)
    sessions = await s2.list_sessions()
    runs = await s2.list_runs()
    events = await s2.get_run_events("run_p1")
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess_p1"
    assert len(runs) == 1
    assert len(events) == 1
    assert events[0].payload["init"] is True
    await s2.close()


@pytest.mark.asyncio
async def test_node_detail_aggregation_v3(store):
    """v3: 节点详情聚合（run_events + raw_harness_events + handoffs + widget_inputs）。"""
    await store.create_session("sess_d1", agent_id="manager")
    await store.init_run("run_d1", session_id="sess_d1", workflow_id="wf", run_mode="templated")
    await store.provision_subagent(
        subagent_id="sub_d1_n1",
        actor_id="run_d1:n1",
        run_id="run_d1",
        node_id="n1",
        harness_type="deterministic",
    )
    # 节点生命周期事件
    await store.append_run_event(
        run_id="run_d1", event_type=DagEventType.NODE_STARTED.value,
        node_id="n1", payload={"input": "data"},
    )
    await store.append_run_event(
        run_id="run_d1", event_type=DagEventType.NODE_COMPLETED.value,
        node_id="n1", payload={"output": "result"},
    )
    await store.append_raw_event(
        run_id="run_d1", subagent_id="sub_d1_n1", node_id="n1",
        harness="deterministic", event_type="tool.call",
        raw_payload={"tool": "calc"},
    )
    await store.append_widget_input(
        run_id="run_d1", widget_id="n1_form",
        payload={"approved": True}, session_id="sess_d1",
    )

    detail = await store.get_node_detail("run_d1", "n1")
    assert detail["status"] == "completed"
    assert detail["input_payload"]["input"] == "data"
    assert detail["output_payload"]["output"] == "result"
    assert len(detail["raw_events"]) == 1
    assert len(detail["events"]) == 2
    assert len(detail["hil_events"]) == 1


@pytest.mark.asyncio
async def test_list_runs_filter(store):
    """v3: list_runs 按 session_id / workflow_id / status 过滤。"""
    await store.create_session("sess_f1", agent_id="manager")
    await store.init_run("run_f1", session_id="sess_f1", workflow_id="wf_a", run_mode="templated")
    await store.init_run("run_f2", session_id="sess_f1", workflow_id="wf_b", run_mode="templated")
    await store.finalize_run("run_f2", "completed")

    # 按 workflow_id 筛选
    wf_a_runs = await store.list_runs(workflow_id="wf_a")
    assert len(wf_a_runs) == 1
    assert wf_a_runs[0]["run_id"] == "run_f1"

    # 按 status 筛选
    completed = await store.list_runs(status="completed")
    assert len(completed) == 1
    assert completed[0]["run_id"] == "run_f2"

    # 按 session_id 筛选
    f1_runs = await store.list_runs(session_id="sess_f1")
    assert len(f1_runs) == 2


@pytest.mark.asyncio
async def test_parent_child_runs_v3(store):
    """v3: parent_child_runs 表 + record_parent_child_run + list_child_runs_of_session。"""
    # 父 session + 父 run + 两个子 run（FK 约束：parent_run_id 必须存在）
    await store.create_session("sess_parent_1", agent_id="manager")
    await store.init_run("run_parent_x", session_id="sess_parent_1", workflow_id="manager-session", run_mode="templated")
    await store.init_run("run_child_1", session_id="sess_parent_1", workflow_id="log-patrol", run_mode="templated")
    await store.init_run("run_child_2", session_id="sess_parent_1", workflow_id="task-patrol", run_mode="templated")

    # 记录父子关系
    await store.record_parent_child_run(
        parent_run_id="run_parent_x",
        child_run_id="run_child_1",
        parent_session_id="sess_parent_1",
        child_session_id="sess_parent_1",
        created_via="trigger_workflow",
    )
    await store.record_parent_child_run(
        parent_run_id="run_parent_x",
        child_run_id="run_child_2",
        parent_session_id="sess_parent_1",
        child_session_id="sess_parent_1",
        created_via="trigger_workflow",
    )

    # list_child_runs_of：返回 parent_child_runs 表字段
    children = await store.list_child_runs_of("run_parent_x")
    assert len(children) == 2
    assert all("child_run_id" in c for c in children)

    # list_child_runs_of_session：JOIN parent_child_runs + runs
    child_runs = await store.list_child_runs_of_session("sess_parent_1")
    assert len(child_runs) == 2
    assert all("run_id" in c for c in child_runs)


@pytest.mark.asyncio
async def test_subagent_provision_v3(store):
    """v3: subagents 表 — actor_id = run_id:node_id + lease_generation。"""
    await store.create_session("sess_s1", agent_id="manager")
    await store.init_run("run_s1", session_id="sess_s1", workflow_id="wf", run_mode="templated")
    # provision 第 1 代 subagent
    await store.provision_subagent(
        subagent_id="sub_s1_n1_gen1",
        actor_id="run_s1:n1",
        run_id="run_s1",
        node_id="n1",
        harness_type="opencode",
        lease_generation=1,
    )
    # 第 1 代完成后 terminated（status='completed' 退出 partial UNIQUE）
    await store.terminate_subagent("sub_s1_n1_gen1")
    # provision 第 2 代（纠错重派）
    new_lease = await store.increment_lease_generation("run_s1", "n1")
    assert new_lease == 2
    await store.provision_subagent(
        subagent_id="sub_s1_n1_gen2",
        actor_id="run_s1:n1",
        run_id="run_s1",
        node_id="n1",
        harness_type="opencode",
        lease_generation=2,
    )
    subagents = await store.list_subagents_for_run("run_s1")
    assert len(subagents) == 2
    # active subagent 当前是 lease_generation=2 的那个
    active = await store.get_active_subagent("run_s1", "n1")
    assert active is not None
    assert active["lease_generation"] == 2


@pytest.mark.asyncio
async def test_session_status_v3(store):
    """v3: session.status 仅 active / dormant / archived（无 running/completed/failed）。"""
    await store.create_session("sess_status_1", agent_id="manager")
    await store.update_session_status("sess_status_1", "dormant")
    s = await store.get_session("sess_status_1")
    assert s["status"] == "dormant"
    # 归档
    await store.archive_session("sess_status_1")
    s = await store.get_session("sess_status_1")
    assert s["status"] == "archived"
    assert s.get("archived_at") is not None


# ── P0.18.13：update_run_status 自动回填 started_at/finished_at ─────────


@pytest.mark.asyncio
async def test_update_run_status_auto_backfills_started_at(store):
    """P0.18.13：转 running 时若 started_at 为 NULL 自动回填，
    避免前端 formatElapsed(NULL) 算出 49 万小时。"""
    await store.create_session("sess_b1", agent_id="manager")
    await store.init_run("run_b1", session_id="sess_b1", workflow_id="wf", run_mode="templated")
    # 初始 started_at 是 NULL
    summary = await store.get_run_summary("run_b1")
    assert summary["started_at"] is None

    # 转 running：应自动回填 started_at
    await store.update_run_status("run_b1", "running")
    summary = await store.get_run_summary("run_b1")
    assert summary["status"] == "running"
    assert summary["started_at"] is not None, "转 running 必须自动回填 started_at"

    # 再次转 running：started_at 不应被覆盖（幂等）
    first_started = summary["started_at"]
    await store.update_run_status("run_b1", "running")
    summary = await store.get_run_summary("run_b1")
    assert summary["started_at"] == first_started, "started_at 必须保持不变（幂等）"


@pytest.mark.asyncio
async def test_update_run_status_explicit_started_at_wins(store):
    """P0.18.13：调用方显式传 started_at 时，外部值优先。"""
    await store.create_session("sess_b2", agent_id="manager")
    await store.init_run("run_b2", session_id="sess_b2", workflow_id="wf", run_mode="templated")

    custom_iso = "2026-01-01T00:00:00+00:00"
    await store.update_run_status("run_b2", "running", started_at=custom_iso)
    summary = await store.get_run_summary("run_b2")
    assert summary["started_at"] == custom_iso, "显式传值必须生效"


@pytest.mark.asyncio
async def test_update_run_status_auto_backfills_finished_at(store):
    """P0.18.13：转 completed/failed/cancelled 时自动回填 finished_at。"""
    await store.create_session("sess_b3", agent_id="manager")
    await store.init_run("run_b3", session_id="sess_b3", workflow_id="wf", run_mode="templated")

    for terminal in ("completed", "failed", "cancelled"):
        await store.init_run(
            f"run_b3_{terminal}", session_id="sess_b3", workflow_id="wf", run_mode="templated",
        )
        await store.update_run_status(f"run_b3_{terminal}", terminal)
        summary = await store.get_run_summary(f"run_b3_{terminal}")
        assert summary["status"] == terminal
        assert summary["finished_at"] is not None, f"转 {terminal} 必须自动回填 finished_at"


@pytest.mark.asyncio
async def test_list_stale_runs_filters_by_created_at(store):
    """P0.18.13：list_stale_runs 按 created_at 阈值过滤未终止 run。"""
    await store.create_session("sess_st1", agent_id="manager")
    # 三个 run，状态分别是 pending / running / completed
    await store.init_run("run_st_pending", session_id="sess_st1", workflow_id="wf", run_mode="templated")
    await store.update_run_status("run_st_pending", "running")  # 自动回填 started_at
    await store.update_run_status("run_st_pending", "cancelled")  # 终止
    # 重新创建一个 pending 用于测 stale
    await store.init_run("run_st_active", session_id="sess_st1", workflow_id="wf", run_mode="templated")
    # 注意：init_run 不写入 started_at（保持 NULL），status 默认 pending

    # 阈值设到现在（所有现有 created_at 都早于 now），应至少返回 1 个
    import datetime as _dt
    future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=10)).isoformat()
    stale = await store.list_stale_runs(threshold_iso=future, limit=100)
    active_ids = {r["run_id"] for r in stale}
    assert "run_st_active" in active_ids
    assert "run_st_pending" not in active_ids, "已终止的 run 不应被列出来"