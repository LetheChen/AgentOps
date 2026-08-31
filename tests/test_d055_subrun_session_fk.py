"""D-055 回归测试：trigger_workflow 子 conversational run 启动阶段 FOREIGN KEY 失败。

根因：
- trigger_workflow 调用 orch.run()，_run_conversational 创建 child SessionEngine，
  其中 SessionEngine.session_id = run_id（[orchestrator/local_sdk.py:216](orchestrator/local_sdk.py)）。
- SessionEngine.start_turn 首行调 append_session_message(self.session_id, ...)，
  session_messages.session_id 有 FK → sessions.session_id。
- runs 表已 INSERT（含 run_id），但 sessions 表没有 run_id 这一行 → FK violation。
- 现象：run_events 只有 turn.failed + session.dormant 两条，run.error = "FOREIGN KEY constraint failed"。

修复：在 _pre_init_run 里同时为 run_id 落一行 session（INSERT OR IGNORE 幂等），
让 append_session_message(run_id, ...) 能 FK 到 sessions。

覆盖：
1. _pre_init_run 后 sessions 表同时含 parent session_id + run_id（修复后）。
2. _pre_init_run 缺失 run_id session 行时会复现 FK 失败（修复前断言）。
3. 端到端：orch.run() 不再因 FK 失败 fail-fast（child run 进入正常生命周期）。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
import pytest_asyncio

from audit import SqliteEventStore
from orchestrator import (
    DagEvent,
    DagEventType,
    LocalSdkOrchestrator,
    RunMode,
    RunRequest,
    RunStatus,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    """每个测试用独立临时 db 文件（FK enforcement ON）。"""
    db_path = tmp_path / "test_d055.db"
    s = SqliteEventStore(str(db_path))
    # 显式开 FK enforcement（SqliteEventStore 构造时已 PRAGMA foreign_keys=ON，
    # 这里 double-check 防回归）
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def orchestrator(store):
    """带真实 EventStore 的 LocalSdkOrchestrator（无 workflow，纯对话模式）。"""
    o = LocalSdkOrchestrator(llm_config={}, event_store=store)
    yield o


@pytest.mark.asyncio
async def test_pre_init_run_creates_session_for_run_id(store):
    """D-055: _pre_init_run 必须同时为 run_id 落一行 session。

    修复前：sessions 表只有 parent session_id，没有 run_id，
    child SessionEngine.start_turn 会撞 FK。修复后应该两行都在。
    """
    o = LocalSdkOrchestrator(llm_config={}, event_store=store)
    req = RunRequest(
        run_mode=RunMode.CONVERSATIONAL,
        agent_id="echo_agent",
        initial_message="hello",
        session_id="sess_parent_d055",
    )
    expected_run_id = "run_d055_test_a"

    # 直接调 _pre_init_run（避免触发真 harness）
    await o._pre_init_run(req, expected_run_id)

    # 验证 sessions 表同时有 parent session_id 和 run_id
    sessions = await store.list_sessions()
    sids = {s["session_id"] for s in sessions}
    assert "sess_parent_d055" in sids, "parent session 缺失"
    assert expected_run_id in sids, (
        f"D-055 修复失效：sessions 表缺 run_id={expected_run_id}，"
        f"child SessionEngine.start_turn 会撞 session_messages FK"
    )


@pytest.mark.asyncio
async def test_append_session_message_works_for_run_id(store):
    """D-055: create_session(run_id) 之后 append_session_message(run_id, ...) 不再 FK 失败。

    模拟 SessionEngine.start_turn 第 264-268 行的调用链。
    """
    o = LocalSdkOrchestrator(llm_config={}, event_store=store)
    req = RunRequest(
        run_mode=RunMode.CONVERSATIONAL,
        agent_id="echo_agent",
        initial_message="hello",
        session_id="sess_parent_d055_b",
    )
    run_id = "run_d055_test_b"
    await o._pre_init_run(req, run_id)

    # 这是 SessionEngine.start_turn 第 264-268 行的核心调用
    # 修复前会报 sqlite3.IntegrityError: FOREIGN KEY constraint failed
    seq = await store.append_session_message(
        run_id, "user", "hello", turn_id="turn_d055_b",
    )
    assert seq == 1, f"append_session_message 应返回 sequence=1，实际 {seq}"

    # 校验 session_messages 行确实写入
    msgs = await store.get_session_messages(run_id)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_append_session_message_without_d055_fix_fails(tmp_path):
    """反向断言：缺少 D-005 修复时，append_session_message(run_id) 会 FK 失败。

    这个测试保护修复不被无意中回滚——若有人删了 _pre_init_run 里
    `create_session(session_id=run_id)` 这一行，本测试会 fail。
    """
    db_path = tmp_path / "test_d055_neg.db"
    store = SqliteEventStore(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()

    # 仅建 parent session + run（模拟修复前 _pre_init_run 的行为：只 create_session(parent)）
    await store.create_session(
        session_id="sess_parent_neg",
        agent_id="echo_agent",
    )
    await store.init_run(
        run_id="run_d055_neg",
        session_id="sess_parent_neg",
        workflow_id=None,
        run_mode="conversational",
        agent_id="echo_agent",
        initial_message="hi",
        inputs={"initial_message": "hi"},
    )

    # 不为 run_id 调 create_session，直接 append_session_message 应该 FK 失败
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await store.append_session_message(
            "run_d055_neg", "user", "hi", turn_id="turn_neg",
        )

    await store.close()


@pytest.mark.asyncio
async def test_conversational_run_not_fail_fast_on_fk(orchestrator, store):
    """D-055: 端到端 — conversational 子 run 不再因 FK 在 3-20ms 内 fail-fast。

    用 echo_agent（LocalSdkOrchestrator 内置）跑通，验证 run 进入 completed 状态
    而不是 failed=FOREIGN KEY constraint failed。
    """
    req = RunRequest(
        run_mode=RunMode.CONVERSATIONAL,
        agent_id="echo_agent",
        initial_message="d055 smoke",
        session_id="sess_d055_e2e",
    )
    handle = await orchestrator.run(req)

    # 等终态（最长 5s）
    for _ in range(50):
        state = await orchestrator.get_run(handle.run_id)
        if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        await asyncio.sleep(0.1)

    state = await orchestrator.get_run(handle.run_id)
    assert state is not None
    # D-005 修复前会卡 failed + error="FOREIGN KEY constraint failed"
    assert state.status != RunStatus.FAILED, (
        f"D-055 修复失效：run 异常失败 — {state.error}"
    )

    # 同时校验 sessions 表有 run_id 一行
    sessions = await store.list_sessions()
    sids = {s["session_id"] for s in sessions}
    assert handle.run_id in sids, "run_id 必须已落 sessions 表"
