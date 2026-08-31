"""Session 会话改造测试 — v3 三层架构验收。

v3 改造（2026-08-09）：
  - sessions 表只装对话层（active/dormant/archived），不再含 workflow_id/run_mode/inputs/final_outputs
  - runs 表独立装 DAG 执行层
  - parent_child_runs 表（替代 v2 parent_child_sessions）
  - RunStatus 移除 ACTIVE/DORMANT/PAUSED，新增 WAITING
  - 新增 SessionStatus 枚举（ACTIVE/DORMANT/ARCHIVED）
  - 新增 SubagentStatus 枚举
  - EventStore.create_session / get_session / update_session_status / archive_session
  - EventStore.init_run / finalize_run / list_runs
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from audit.store import SqliteEventStore
from orchestrator.protocol import RunStatus, SessionStatus, SubagentStatus


@pytest_asyncio.fixture
async def store(tmp_path):
    """临时 SQLite EventStore。"""
    db_path = tmp_path / "test_session.db"
    s = SqliteEventStore(str(db_path))
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_session_messages_append_and_get(store):
    """session_messages 表能追加和查询消息。"""
    # v3: create_session（agent_id 必填）
    await store.create_session("sess_test_1", agent_id="manager")

    seq1 = await store.append_session_message("sess_test_1", "user", "你好")
    seq2 = await store.append_session_message("sess_test_1", "assistant", "你好，有什么可以帮你？")

    assert seq1 == 1
    assert seq2 == 2

    messages = await store.get_session_messages("sess_test_1")
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "你好"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["sequence"] == 2


@pytest.mark.asyncio
async def test_session_messages_content_json(store):
    """session_messages content 支持 JSON（tool_call 等复杂结构）。"""
    await store.create_session("sess_test_2", agent_id="manager")
    tool_call = {"tool": "mm_search", "args": {"query": "天气"}}
    await store.append_session_message("sess_test_2", "tool", tool_call)

    messages = await store.get_session_messages("sess_test_2")
    assert len(messages) == 1
    # content 被 JSON 解析回 dict
    assert isinstance(messages[0]["content"], dict)
    assert messages[0]["content"]["tool"] == "mm_search"


@pytest.mark.asyncio
async def test_update_session_status_v3(store):
    """v3: update_session_status 用 SessionStatus.ACTIVE / DORMANT。"""
    await store.create_session("sess_test_3", agent_id="manager", title="test session")

    await store.update_session_status("sess_test_3", SessionStatus.ACTIVE.value)
    summary = await store.get_session("sess_test_3")
    assert summary["status"] == "active"
    assert summary["last_activity_at"] is not None

    await store.update_session_status("sess_test_3", SessionStatus.DORMANT.value, last_activity=False)
    summary = await store.get_session("sess_test_3")
    assert summary["status"] == "dormant"


@pytest.mark.asyncio
async def test_list_sessions_filter_v3(store):
    """v3: list_sessions 支持 status/search 过滤（不含 workflow_id/run_mode）。"""
    await store.create_session("s1", agent_id="manager", title="会话 1")
    await store.create_session("s2", agent_id="manager", title="日志巡检")
    # s1 设为 dormant，s2 保持默认 active，过滤有意义
    await store.update_session_status("s1", SessionStatus.DORMANT.value)

    # 过滤 status=active
    active_sessions = await store.list_sessions(status="active")
    assert len(active_sessions) == 1
    assert active_sessions[0]["session_id"] == "s2"

    # 过滤 status=dormant
    dormant_sessions = await store.list_sessions(status="dormant")
    assert len(dormant_sessions) == 1
    assert dormant_sessions[0]["session_id"] == "s1"

    # 过滤 search=日志
    matched = await store.list_sessions(search="日志")
    assert len(matched) == 1
    assert matched[0]["session_id"] == "s2"

    # 无过滤：返回全部
    all_sessions = await store.list_sessions()
    assert len(all_sessions) == 2


@pytest.mark.asyncio
async def test_archive_session_v3(store):
    """v3: archive_session 能设 archived_at + status='archived'。"""
    await store.create_session("sess_archive", agent_id="manager")
    await store.update_session_status("sess_archive", SessionStatus.DORMANT.value, last_activity=False)

    await store.archive_session("sess_archive")
    summary = await store.get_session_summary("sess_archive")
    assert summary["archived_at"] is not None
    assert summary["status"] == "archived"


@pytest.mark.asyncio
async def test_sessions_table_fields_v3(store):
    """v3: sessions 表字段（active/dormant/archived 状态机 + dormant_at 新增）。"""
    await store.create_session("sess_mig", agent_id="manager")
    summary = await store.get_session_summary("sess_mig")
    # v3 sessions 表必含字段
    assert "last_activity_at" in summary
    assert "archived_at" in summary
    assert "dormant_at" in summary
    # v3 agent_id 必填
    assert summary["agent_id"] == "manager"
    # v3 sessions 不再含 workflow_id / run_mode / inputs / final_outputs
    assert "workflow_id" not in summary or summary.get("workflow_id") is None
    assert "run_mode" not in summary or summary.get("run_mode") is None


@pytest.mark.asyncio
async def test_parent_child_runs_relation_v3(store):
    """v3: parent_child_runs 表 + list_child_runs_of_session JOIN runs。"""
    # 父 session + 父 run + 子 run
    await store.create_session("parent_sess", agent_id="manager")
    await store.init_run("run_parent", session_id="parent_sess", workflow_id="manager-session", run_mode="conversational", agent_id="manager", initial_message="hi")
    await store.init_run("child_run", session_id="parent_sess", workflow_id="log-patrol", run_mode="templated")

    await store.record_parent_child_run(
        parent_run_id="run_parent",
        child_run_id="child_run",
        parent_session_id="parent_sess",
        child_session_id="parent_sess",
        created_via="trigger_workflow",
    )

    # list_child_runs_of: 返回 parent_child_runs 表字段
    children = await store.list_child_runs_of("run_parent")
    assert len(children) == 1
    assert children[0]["child_run_id"] == "child_run"

    # list_child_runs_of_session: JOIN parent_child_runs + runs，返回完整 run 字段
    child_runs = await store.list_child_runs_of_session("parent_sess")
    assert len(child_runs) == 1
    assert child_runs[0]["run_id"] == "child_run"

    # 递增 attached_run_count
    await store.increment_attached_run_count("parent_sess")
    parent_summary = await store.get_session_summary("parent_sess")
    assert parent_summary["attached_run_count"] == 1


@pytest.mark.asyncio
async def test_run_status_enum_v3():
    """v3: RunStatus 移除 ACTIVE/DORMANT/PAUSED，新增 WAITING。"""
    # Run 不再有 ACTIVE / DORMANT / PAUSED（这些是 Session 概念）
    assert not hasattr(RunStatus, "ACTIVE")
    assert not hasattr(RunStatus, "DORMANT")
    assert not hasattr(RunStatus, "PAUSED")
    # 新增 WAITING
    assert RunStatus.WAITING.value == "waiting"
    # 原有状态保留
    assert RunStatus.RUNNING.value == "running"
    assert RunStatus.COMPLETED.value == "completed"


@pytest.mark.asyncio
async def test_session_status_enum_v3():
    """v3: 新增 SessionStatus 枚举（ACTIVE / DORMANT / ARCHIVED）。"""
    assert SessionStatus.ACTIVE.value == "active"
    assert SessionStatus.DORMANT.value == "dormant"
    assert SessionStatus.ARCHIVED.value == "archived"


@pytest.mark.asyncio
async def test_subagent_status_enum_v3():
    """v3: 新增 SubagentStatus 枚举（一次性执行体状态机）。"""
    assert SubagentStatus.PROVISIONING.value == "provisioning"
    assert SubagentStatus.RUNNING.value == "running"
    assert SubagentStatus.HANDOFF.value == "handoff"
    assert SubagentStatus.CLEANUP.value == "cleanup"
    assert SubagentStatus.COMPLETED.value == "completed"
    assert SubagentStatus.FAILED.value == "failed"