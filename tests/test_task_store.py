"""TaskStore 单测（4 表 CRUD + 乐观锁冲突）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.1
覆盖：
- 建项目 / 建任务 / 建阶段
- 乐观锁更新成功 / 冲突返回 None
- 状态推进 + 冲突重试
- global_revision 自增（触发器）
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from audit.store import SqliteEventStore
from task.store import TaskStore
from task.orchestrator import TaskOrchestrator
from task.status import TaskStatus


@pytest.fixture
def task_store():
    """临时 SQLite + TaskStore（隔离测试，不污染 audit.db）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store_conn = SqliteEventStore(tmp.name, task_v1_enabled=False)
    store = TaskStore(store_conn._conn, store_conn._db_lock)
    yield store
    store_conn._conn.close()
    os.unlink(tmp.name)


@pytest.fixture
def orchestrator(task_store):
    """TaskOrchestrator（P0 模式）。"""
    return TaskOrchestrator(task_store, p0_mode=True)


class TestProjectCRUD:

    @pytest.mark.asyncio
    async def test_create_and_get_project(self, task_store):
        """建项目 + 查项目。"""
        project = await task_store.create_project(
            project_id="proj_test_1", name="TestProject", type="code")
        assert project["name"] == "TestProject"
        assert project["project_id"] == "proj_test_1"
        assert project["version"] == 0

        fetched = await task_store.get_project("proj_test_1")
        assert fetched["name"] == "TestProject"

    @pytest.mark.asyncio
    async def test_list_projects(self, task_store):
        """列项目。"""
        await task_store.create_project(project_id="proj_a", name="A")
        await task_store.create_project(project_id="proj_b", name="B")
        projects = await task_store.list_projects()
        assert len(projects) == 2

    @pytest.mark.asyncio
    async def test_alloc_task_number(self, task_store):
        """分配任务序号 + identifier 生成。"""
        await task_store.create_project(project_id="proj_num", name="AgentOps")
        identifier1, num1 = await task_store.alloc_task_number("proj_num")
        identifier2, num2 = await task_store.alloc_task_number("proj_num")
        assert num1 == 1
        assert num2 == 2
        assert "AGE" in identifier1  # 项目名前 3 字符
        assert identifier1 != identifier2


class TestTaskCRUD:

    @pytest.mark.asyncio
    async def test_create_and_get_task(self, task_store):
        """建任务 + 查任务。"""
        await task_store.create_project(project_id="proj_t1", name="Test")
        task = await task_store.create_task(
            task_id="task_1", project_id="proj_t1", title="测试任务",
            thread_id="session_123")
        assert task["title"] == "测试任务"
        assert task["status"] == "idea"
        assert task["version"] == 0
        assert task["thread_id"] == "session_123"

        fetched = await task_store.get_task("task_1")
        assert fetched["title"] == "测试任务"

    @pytest.mark.asyncio
    async def test_list_tasks_by_project(self, task_store):
        """按项目列任务。"""
        await task_store.create_project(project_id="proj_l", name="L")
        await task_store.create_task(task_id="t1", project_id="proj_l", title="T1")
        await task_store.create_task(task_id="t2", project_id="proj_l", title="T2")
        tasks = await task_store.list_tasks(project_id="proj_l")
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_tasks_by_status(self, task_store):
        """按状态过滤任务。"""
        await task_store.create_project(project_id="proj_s", name="S")
        await task_store.create_task(task_id="t_idea", project_id="proj_s", title="I", status="idea")
        await task_store.create_task(task_id="t_backlog", project_id="proj_s", title="B", status="backlog")
        idea_tasks = await task_store.list_tasks(project_id="proj_s", status="idea")
        assert len(idea_tasks) == 1
        assert idea_tasks[0]["task_id"] == "t_idea"


class TestOptimisticLock:

    @pytest.mark.asyncio
    async def test_update_status_success(self, task_store):
        """乐观锁更新成功（version 匹配）。"""
        await task_store.create_project(project_id="proj_ol", name="OL")
        task = await task_store.create_task(
            task_id="task_ol", project_id="proj_ol", title="OL Task")
        updated = await task_store.update_task_status("task_ol", "backlog", if_version=0)
        assert updated is not None
        assert updated["status"] == "backlog"
        assert updated["version"] == 1

    @pytest.mark.asyncio
    async def test_update_status_conflict(self, task_store):
        """乐观锁冲突（version 不匹配）返回 None。"""
        await task_store.create_project(project_id="proj_c", name="C")
        await task_store.create_task(task_id="task_c", project_id="proj_c", title="C")
        # 用错误的 version
        result = await task_store.update_task_status("task_c", "backlog", if_version=99)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_fields_success(self, task_store):
        """更新字段成功。"""
        await task_store.create_project(project_id="proj_f", name="F")
        await task_store.create_task(task_id="task_f", project_id="proj_f", title="F")
        updated = await task_store.update_task_fields("task_f", 0, title="新标题", risk_level="high")
        assert updated is not None
        assert updated["title"] == "新标题"
        assert updated["risk_level"] == "high"
        assert updated["version"] == 1


class TestOrchestratorAdvance:

    @pytest.mark.asyncio
    async def test_advance_forward_path(self, orchestrator, task_store):
        """正向推进：idea→backlog→discussing→reviewing。"""
        await task_store.create_project(project_id="proj_adv", name="Adv")
        result = await orchestrator.submit_idea(
            task_id="task_adv", project_id="proj_adv", title="推进测试")
        assert result["ok"]
        task_id = result["task"]["task_id"]
        if_version = result["if_version"]

        # idea→backlog
        r = await orchestrator.advance_stage(
            task_id=task_id, target_status="backlog", if_version=if_version, actor="agent")
        assert r["ok"]
        assert r["task"]["status"] == "backlog"
        if_version = r["if_version"]

        # backlog→discussing
        r = await orchestrator.advance_stage(
            task_id=task_id, target_status="discussing", if_version=if_version, actor="agent")
        assert r["ok"]
        if_version = r["if_version"]

        # discussing→reviewing
        r = await orchestrator.advance_stage(
            task_id=task_id, target_status="reviewing", if_version=if_version, actor="agent")
        assert r["ok"]
        assert r["task"]["status"] == "reviewing"

    @pytest.mark.asyncio
    async def test_agent_cannot_close_reviewing(self, orchestrator, task_store):
        """agent 不能触发 reviewing→closed（requires_user=True）。"""
        await task_store.create_project(project_id="proj_rc", name="RC")
        result = await orchestrator.submit_idea(
            task_id="task_rc", project_id="proj_rc", title="RC")
        task_id = result["task"]["task_id"]
        v = result["if_version"]

        # 推到 reviewing
        for target in ["backlog", "discussing", "reviewing"]:
            r = await orchestrator.advance_stage(
                task_id=task_id, target_status=target, if_version=v, actor="agent")
            assert r["ok"], f"推进到 {target} 失败: {r}"
            v = r["if_version"]

        # agent 尝试 closed → 被拒绝
        r = await orchestrator.advance_stage(
            task_id=task_id, target_status="closed", if_version=v, actor="agent")
        assert not r["ok"]
        assert r["error"] == "requires_user_approval"

    @pytest.mark.asyncio
    async def test_user_can_close_reviewing(self, orchestrator, task_store):
        """user 可以触发 reviewing→closed。"""
        await task_store.create_project(project_id="proj_uc", name="UC")
        result = await orchestrator.submit_idea(
            task_id="task_uc", project_id="proj_uc", title="UC")
        task_id = result["task"]["task_id"]
        v = result["if_version"]

        for target in ["backlog", "discussing", "reviewing"]:
            r = await orchestrator.advance_stage(
                task_id=task_id, target_status=target, if_version=v, actor="agent")
            v = r["if_version"]

        # user 关闭
        r = await orchestrator.advance_stage(
            task_id=task_id, target_status="closed", if_version=v, actor="user")
        assert r["ok"]
        assert r["task"]["status"] == "closed"

    @pytest.mark.asyncio
    async def test_illegal_transition_rejected(self, orchestrator, task_store):
        """非法转移被拒绝。"""
        await task_store.create_project(project_id="proj_il", name="IL")
        result = await orchestrator.submit_idea(
            task_id="task_il", project_id="proj_il", title="IL")
        task_id = result["task"]["task_id"]
        v = result["if_version"]

        # idea→discussing（非法，必须经 backlog）
        r = await orchestrator.advance_stage(
            task_id=task_id, target_status="discussing", if_version=v, actor="agent")
        assert not r["ok"]
        assert r["error"] == "illegal_transition"

    @pytest.mark.asyncio
    async def test_conflict_retry_once(self, orchestrator, task_store):
        """乐观锁冲突重试一次成功。"""
        await task_store.create_project(project_id="proj_cr", name="CR")
        result = await orchestrator.submit_idea(
            task_id="task_cr", project_id="proj_cr", title="CR")
        task_id = result["task"]["task_id"]
        v = result["if_version"]

        # 先用正确 version 推到 backlog（模拟另一个 agent 先改了）
        await task_store.update_task_status(task_id, "backlog", if_version=v)
        # 此时 version=1，但调用方还以为 version=0

        # 用旧 version 推进到 discussing → 冲突 → 重试 → 成功
        r = await orchestrator.advance_stage(
            task_id=task_id, target_status="discussing", if_version=v, actor="agent")
        assert r["ok"]
        assert r["task"]["status"] == "discussing"


class TestGlobalRevision:

    @pytest.mark.asyncio
    async def test_revision_increments_on_insert(self, task_store):
        """建任务后 revision 自增（触发器）。"""
        initial = await task_store.get_revision()
        await task_store.create_project(project_id="proj_rev", name="Rev")
        await task_store.create_task(task_id="task_rev", project_id="proj_rev", title="R")
        after = await task_store.get_revision()
        assert after > initial  # 至少 +2（project + task 各触发一次）
