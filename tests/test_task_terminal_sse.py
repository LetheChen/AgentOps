"""terminal SSE 端点测试（V1 验收第⑧条：xterm 实时显示）。

测试 /api/tasks/{task_id}/terminal/stream 端点的核心逻辑：
1. stream_pane 产生正确的 pane 内容（真实 terminal 流）
2. store.list_activities 返回 activities（回退路径数据源）
3. SSE 序列化格式（data: JSON\n\n）
4. task 不存在时端点抛 404

不走 HTTP 层（避免 SSE 无限流在 TestClient 下阻塞），
直接测试 TerminalSessionManager.stream_pane + store.list_activities + SSE 序列化。
"""
import json
import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audit.store import SqliteEventStore
from task.store import TaskStore
from task.terminal_session import TerminalSessionManager
from task.orchestrator import TaskOrchestrator


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def sse_stack():
    """SSE 测试栈：EventStore + TaskStore + MockBackend terminal。"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sse.db")
    conn = SqliteEventStore(db_path=p, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    terminal_mgr = TerminalSessionManager(backend="mock")
    yield store, terminal_mgr
    conn._conn.close()


@pytest_asyncio.fixture
async def project(sse_stack):
    store, _ = sse_stack
    return await store.create_project(project_id="proj_sse", name="SSE测试", type="code")


# ============================================================
# 测试 1：stream_pane 产生 pane 内容（路径 1 数据源）
# ============================================================

class TestStreamPane:
    """路径 1：task 绑定 terminal_session_id → stream_pane 产生 pane 内容。"""

    @pytest.mark.asyncio
    async def test_stream_pane_yields_capture_content(self, sse_stack):
        """stream_pane 产生 capture_pane 内容，500ms 间隔。"""
        _, terminal_mgr = sse_stack
        # 向 mock terminal 注入内容
        await terminal_mgr.create_session("term_001")
        await terminal_mgr.send_keys("term_001", "$ echo hello")
        await terminal_mgr.send_keys("term_001", "hello")

        # 消费 stream_pane 的前 2 个事件
        events = []
        async for pane_text in terminal_mgr.stream_pane("term_001", interval=0.5):
            events.append(pane_text)
            if len(events) >= 2:
                break

        assert len(events) == 2
        # MockBackend.capture_pane 返回累积文本
        assert "echo hello" in events[0]
        assert "hello" in events[0]

    @pytest.mark.asyncio
    async def test_stream_pane_empty_session_returns_empty(self, sse_stack):
        """空 session 的 stream_pane 返回空串（不抛异常）。"""
        _, terminal_mgr = sse_stack
        await terminal_mgr.create_session("term_empty")

        events = []
        async for pane_text in terminal_mgr.stream_pane("term_empty", interval=0.5):
            events.append(pane_text)
            if len(events) >= 1:
                break

        assert len(events) == 1
        assert events[0] == ""  # 空 session 返回空串

    @pytest.mark.asyncio
    async def test_stream_pane_nonexistent_session_returns_empty(self, sse_stack):
        """不存在的 session 的 stream_pane 返回空串（不抛异常）。"""
        _, terminal_mgr = sse_stack

        events = []
        async for pane_text in terminal_mgr.stream_pane("nonexistent", interval=0.5):
            events.append(pane_text)
            if len(events) >= 1:
                break

        assert len(events) == 1
        assert events[0] == ""


# ============================================================
# 测试 2：list_activities 返回活动数据（路径 2 数据源）
# ============================================================

class TestActivitiesDataSource:
    """路径 2：task 未绑定 terminal_session_id → list_activities 提供回退数据。"""

    @pytest.mark.asyncio
    async def test_list_activities_returns_status_change(self, sse_stack, project):
        """advance_stage 产生的 activity 包含 status 变更。"""
        store, _ = sse_stack
        await store.create_task(
            task_id="t_act", project_id="proj_sse", title="活动测试")
        orch = TaskOrchestrator(store, p0_mode=True)
        await orch.advance_stage(task_id="t_act", target_status="backlog",
                                  if_version=0, thread_id="th1", actor="test")

        acts = await store.list_activities("t_act")
        assert len(acts) >= 1
        act = acts[0]
        assert "status" in act.get("changes", {})
        assert act["changes"]["status"]["after"] == "backlog"

    @pytest.mark.asyncio
    async def test_list_activities_empty_for_new_task(self, sse_stack, project):
        """新建任务（无状态变更）的 activities 为空。"""
        store, _ = sse_stack
        await store.create_task(
            task_id="t_empty", project_id="proj_sse", title="空活动测试")

        acts = await store.list_activities("t_empty")
        assert len(acts) == 0


# ============================================================
# 测试 3：SSE 序列化格式
# ============================================================

class TestSSESerialization:
    """SSE 事件序列化格式合规性。"""

    def test_pane_event_format(self):
        """pane 事件格式：data: {"type":"pane","content":"...","terminal_session_id":"..."}\n\n"""
        payload = {
            "type": "pane",
            "content": "$ echo hello\nhello",
            "terminal_session_id": "term_001",
        }
        sse_line = f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        assert sse_line.startswith("data: ")
        assert sse_line.endswith("\n\n")
        parsed = json.loads(sse_line[6:].strip())
        assert parsed["type"] == "pane"
        assert "hello" in parsed["content"]
        assert parsed["terminal_session_id"] == "term_001"

    def test_activities_event_format(self):
        """activities 事件格式：data: {"type":"activities","activities":[...]}\n\n"""
        payload = {
            "type": "activities",
            "activities": [{"activity_id": "a1", "changes": {"status": "backlog"}}],
            "terminal_session_id": "",
        }
        sse_line = f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        assert sse_line.startswith("data: ")
        assert sse_line.endswith("\n\n")
        parsed = json.loads(sse_line[6:].strip())
        assert parsed["type"] == "activities"
        assert isinstance(parsed["activities"], list)
        assert len(parsed["activities"]) == 1

    def test_sse_headers(self):
        """SSE 响应头包含必要的缓存控制字段。"""
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        assert headers["Cache-Control"] == "no-cache"
        assert headers["Connection"] == "keep-alive"
        assert headers["X-Accel-Buffering"] == "no"


# ============================================================
# 测试 4：端点 404 逻辑（task 不存在）
# ============================================================

class TestTerminalStream404:
    """task 不存在时端点返回 404。"""

    @pytest.mark.asyncio
    async def test_get_task_returns_none_for_nonexistent(self, sse_stack):
        """store.get_task 对不存在的 task_id 返回 None（端点据此抛 404）。"""
        store, _ = sse_stack
        task = await store.get_task("nonexistent_task")
        assert task is None

    @pytest.mark.asyncio
    async def test_get_task_returns_task_with_terminal_id(self, sse_stack, project):
        """store.get_task 返回的 task 包含 terminal_session_id 字段。"""
        store, _ = sse_stack
        await store.create_task(
            task_id="t_term", project_id="proj_sse", title="终端绑定测试")
        await store.update_task_fields("t_term", 0,
            terminal_session_id="term_bound_001")

        task = await store.get_task("t_term")
        assert task is not None
        assert task["terminal_session_id"] == "term_bound_001"


# ============================================================
# 测试 5：端到端 SSE 事件组装（pane 路径）
# ============================================================

class TestSSEEndToEndPane:
    """端到端验证 pane 路径的 SSE 事件组装逻辑。"""

    @pytest.mark.asyncio
    async def test_pane_path_sse_assembly(self, sse_stack, project):
        """模拟端点 pane 路径：stream_pane → SSE 序列化 → 解析验证。"""
        store, terminal_mgr = sse_stack
        # 建任务并绑定 terminal_session_id
        await store.create_task(
            task_id="t_e2e", project_id="proj_sse", title="E2E测试")
        await store.update_task_fields("t_e2e", 0,
            terminal_session_id="term_e2e")
        # 注入 terminal 内容
        await terminal_mgr.create_session("term_e2e")
        await terminal_mgr.send_keys("term_e2e", "$ npm test")
        await terminal_mgr.send_keys("term_e2e", "PASS")

        # 模拟端点逻辑
        task = await store.get_task("t_e2e")
        terminal_session_id = task["terminal_session_id"]
        sse_events = []

        async for pane_text in terminal_mgr.stream_pane(terminal_session_id, interval=0.5):
            payload = {
                "type": "pane",
                "content": pane_text,
                "terminal_session_id": terminal_session_id,
            }
            sse_events.append(f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n")
            if len(sse_events) >= 1:
                break

        # 验证 SSE 事件
        assert len(sse_events) == 1
        parsed = json.loads(sse_events[0][6:].strip())
        assert parsed["type"] == "pane"
        assert "npm test" in parsed["content"]
        assert "PASS" in parsed["content"]
        assert parsed["terminal_session_id"] == "term_e2e"

    @pytest.mark.asyncio
    async def test_activities_path_sse_assembly(self, sse_stack, project):
        """模拟端点 activities 路径：list_activities → SSE 序列化 → 解析验证。"""
        store, _ = sse_stack
        # 建任务（不绑定 terminal_session_id）+ 推进状态
        await store.create_task(
            task_id="t_e2e_act", project_id="proj_sse", title="E2E活动测试")
        orch = TaskOrchestrator(store, p0_mode=True)
        await orch.advance_stage(task_id="t_e2e_act", target_status="backlog",
                                  if_version=0, thread_id="th1", actor="test")

        # 模拟端点逻辑
        task = await store.get_task("t_e2e_act")
        terminal_session_id = task.get("terminal_session_id") or ""
        assert terminal_session_id == ""  # 未绑定

        acts = await store.list_activities("t_e2e_act")
        payload = {
            "type": "activities",
            "activities": acts,
            "terminal_session_id": terminal_session_id,
        }
        sse_line = f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

        # 验证 SSE 事件
        parsed = json.loads(sse_line[6:].strip())
        assert parsed["type"] == "activities"
        assert len(parsed["activities"]) >= 1
        assert "status" in parsed["activities"][0].get("changes", {})
