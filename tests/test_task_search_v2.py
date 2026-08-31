"""V2-W4 任务搜索单测（LIKE 模糊匹配，支持中英文）。

验证：
1. search_tasks 基本检索（title/description/identifier 命中）
2. 中文搜索（LIKE 完美支持中文）
3. 项目/状态过滤
4. 空查询返回空列表
5. UPDATE/DELETE 后搜索结果同步更新
6. 多关键词子串匹配
"""
import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audit.store import SqliteEventStore
from task.store import TaskStore


@pytest.fixture
def search_stack():
    """搜索测试栈：V1 启用。"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "search.db")
    conn = SqliteEventStore(db_path=p, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    yield store, conn
    conn._conn.close()


@pytest_asyncio.fixture
async def project(search_stack):
    store, _ = search_stack
    return await store.create_project(project_id="proj_s", name="搜索测试项目", type="code")


# ============================================================
# 测试 1：基本检索（title/description/identifier 命中）
# ============================================================

class TestBasicSearch:
    """基本模糊匹配：title/description/identifier 字段命中。"""

    @pytest.mark.asyncio
    async def test_search_by_title(self, search_stack, project):
        """按标题搜索命中。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_title", project_id="proj_s",
            title="修复登录页面bug")
        await store.create_task(
            task_id="t_other", project_id="proj_s",
            title="优化性能")

        results = await store.search_tasks("登录")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_title"

    @pytest.mark.asyncio
    async def test_search_by_description(self, search_stack, project):
        """按描述搜索命中。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_desc", project_id="proj_s",
            title="用户模块",
            description="实现用户注册和登录认证逻辑")
        await store.create_task(
            task_id="t_other", project_id="proj_s",
            title="其他模块",
            description="数据库优化")

        results = await store.search_tasks("认证")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_desc"

    @pytest.mark.asyncio
    async def test_search_by_identifier(self, search_stack, project):
        """按标识符搜索命中。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_id", project_id="proj_s",
            title="任务A",
            identifier="AGENTOPS-42")
        await store.create_task(
            task_id="t_other", project_id="proj_s",
            title="任务B")

        results = await store.search_tasks("AGENTOPS")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_id"

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, search_stack, project):
        """LIKE 搜索默认大小写不敏感（SQLite 默认）。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_ci", project_id="proj_s",
            title="Login Bug Fix")

        results = await store.search_tasks("login")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_ci"

        results = await store.search_tasks("BUG")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_ci"


# ============================================================
# 测试 2：空查询返回空列表
# ============================================================

class TestEmptyQuery:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, search_stack, project):
        """空查询返回空列表（不搜索）。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t1", project_id="proj_s", title="测试任务")

        results = await store.search_tasks("")
        assert results == []

        results = await store.search_tasks("   ")
        assert results == []


# ============================================================
# 测试 3：项目/状态过滤
# ============================================================

class TestSearchFilter:
    @pytest.mark.asyncio
    async def test_filter_by_project(self, search_stack):
        """按项目过滤搜索结果。"""
        store, _ = search_stack
        await store.create_project(project_id="proj_a", name="项目A", type="code")
        await store.create_project(project_id="proj_b", name="项目B", type="code")
        await store.create_task(
            task_id="t_a", project_id="proj_a", title="登录功能")
        await store.create_task(
            task_id="t_b", project_id="proj_b", title="登录页面")

        # 只搜项目 A
        results_a = await store.search_tasks("登录", project_id="proj_a")
        assert len(results_a) == 1
        assert results_a[0]["task_id"] == "t_a"

        # 只搜项目 B
        results_b = await store.search_tasks("登录", project_id="proj_b")
        assert len(results_b) == 1
        assert results_b[0]["task_id"] == "t_b"

        # 不过滤项目，两个都命中
        results_all = await store.search_tasks("登录")
        assert len(results_all) == 2

    @pytest.mark.asyncio
    async def test_filter_by_status(self, search_stack, project):
        """按状态过滤搜索结果。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_idea", project_id="proj_s", title="登录功能")
        await store.create_task(
            task_id="t_backlog", project_id="proj_s", title="登录优化")
        await store.update_task_status("t_backlog", "backlog",
                                       if_version=0)

        # 搜 idea 状态
        results = await store.search_tasks("登录", status="idea")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_idea"

        # 搜 backlog 状态
        results = await store.search_tasks("登录", status="backlog")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_backlog"


# ============================================================
# 测试 4：UPDATE/DELETE 后搜索结果同步更新
# ============================================================

class TestSearchSync:
    """搜索结果随 tasks 表变更自动更新（直接查 tasks 表，无需同步触发器）。"""

    @pytest.mark.asyncio
    async def test_update_title_syncs_results(self, search_stack, project):
        """更新 title 后，旧关键词不再命中，新关键词命中。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_upd", project_id="proj_s",
            title="原始标题登录功能")

        # 原始关键词命中
        results = await store.search_tasks("登录")
        assert len(results) == 1

        # 更新 title（去掉"登录"，加"注册"）
        await store.update_task_fields("t_upd", 0, title="注册功能")

        # "登录" 不再命中
        results = await store.search_tasks("登录")
        assert len(results) == 0

        # "注册" 命中
        results = await store.search_tasks("注册")
        assert len(results) == 1
        assert results[0]["task_id"] == "t_upd"

    @pytest.mark.asyncio
    async def test_delete_task_syncs_results(self, search_stack, project):
        """删除 task 后，搜索不再命中。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_del", project_id="proj_s",
            title="待删除的登录任务")

        # 删除前命中
        results = await store.search_tasks("登录")
        assert len(results) == 1

        # 删除 task
        store._exec("DELETE FROM tasks WHERE task_id = ?", ("t_del",))

        # 删除后不再命中
        results = await store.search_tasks("登录")
        assert len(results) == 0


# ============================================================
# 测试 5：子串匹配
# ============================================================

class TestSubstringMatch:
    @pytest.mark.asyncio
    async def test_partial_match(self, search_stack, project):
        """LIKE 支持子串匹配（部分关键词命中）。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_sub", project_id="proj_s",
            title="用户认证模块重构",
            description="包含JWT token验证")

        # 搜 "认证" 命中 title
        results = await store.search_tasks("认证")
        assert len(results) == 1

        # 搜 "JWT" 命中 description
        results = await store.search_tasks("JWT")
        assert len(results) == 1

        # 搜 "重构" 命中 title
        results = await store.search_tasks("重构")
        assert len(results) == 1

        # 搜 "token" 命中 description（大小写不敏感）
        results = await store.search_tasks("token")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, search_stack, project):
        """无匹配时返回空列表。"""
        store, _ = search_stack
        await store.create_task(
            task_id="t_nm", project_id="proj_s",
            title="登录功能")

        results = await store.search_tasks("不存在的关键词xyz")
        assert results == []


# ============================================================
# 测试 6：limit 参数
# ============================================================

class TestSearchLimit:
    @pytest.mark.asyncio
    async def test_limit_caps_results(self, search_stack, project):
        """limit 参数限制返回条数。"""
        store, _ = search_stack
        for i in range(5):
            await store.create_task(
                task_id=f"t_l{i}", project_id="proj_s",
                title=f"登录功能版本{i}")

        # limit=2
        results = await store.search_tasks("登录", limit=2)
        assert len(results) == 2

        # limit=10（大于匹配数）
        results = await store.search_tasks("登录", limit=10)
        assert len(results) == 5
