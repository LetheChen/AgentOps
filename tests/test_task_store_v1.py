"""V1 TaskStore 增量方法单测（13 张新表 CRUD）。

仅当 task_v1_enabled=True 时运行。验证：
- ideas：submit/get/list/confirm/convert
- task_relations：add/list/blocked_by/环检测
- task_reports + task_comments：submit/get/list + add/list
- acceptance_criteria：add/list/update_status
- design_docs + doc_change_proposals + design_doc_changes：create/propose/apply 链路
- task_runs：link/list
- task_activities：add/list
- task_artifacts：add/list
- task_events：add/list
- agent_styles：create/get/list
- global_revision 触发器联动
"""
import os
import tempfile
import asyncio
import sqlite3
import sys
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audit.store import SqliteEventStore
from task.store import TaskStore


@pytest.fixture
def v1_store():
    """V1 启用的 TaskStore（临时数据库）。"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    conn = SqliteEventStore(db_path=p, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    yield store
    conn._conn.close()


@pytest_asyncio.fixture
async def project(v1_store):
    """预建项目。"""
    return await v1_store.create_project(project_id="proj_v1", name="V1测试项目", type="code")


@pytest_asyncio.fixture
async def task(v1_store, project):
    """预建任务。"""
    return await v1_store.create_task(
        task_id="task_v1_1", project_id="proj_v1", title="V1测试任务")


@pytest.mark.asyncio
class TestIdeas:
    async def test_submit_and_get(self, v1_store, project):
        idea = await v1_store.submit_idea(
            project_id="proj_v1", content="需要一个缓存模块",
            source="conversation", source_ref="sess_123",
            tags=["cache", "perf"])
        assert idea["idea_id"].startswith("idea_")
        assert idea["status"] == "open"
        assert idea["tags"] == ["cache", "perf"]

        got = await v1_store.get_idea(idea["idea_id"])
        assert got["content"] == "需要一个缓存模块"

    async def test_submit_auto_draft(self, v1_store, project):
        idea = await v1_store.submit_idea(
            project_id="proj_v1", content="自动接入的灵感", auto_draft=True)
        assert idea["status"] == "draft"

    async def test_list_ideas(self, v1_store, project):
        await v1_store.submit_idea(project_id="proj_v1", content="灵感1")
        await v1_store.submit_idea(project_id="proj_v1", content="灵感2")
        ideas = await v1_store.list_ideas(project_id="proj_v1")
        assert len(ideas) == 2

    async def test_confirm_idea(self, v1_store, project):
        idea = await v1_store.submit_idea(
            project_id="proj_v1", content="待确认", auto_draft=True)
        r = await v1_store.confirm_idea(idea["idea_id"], idea["version"])
        assert r["ok"]
        assert r["idea"]["status"] == "open"

    async def test_confirm_idea_conflict(self, v1_store, project):
        idea = await v1_store.submit_idea(
            project_id="proj_v1", content="冲突测试", auto_draft=True)
        # 先用正确 version 确认
        await v1_store.confirm_idea(idea["idea_id"], idea["version"])
        # 再用旧 version 确认 → 冲突
        r = await v1_store.confirm_idea(idea["idea_id"], idea["version"])
        assert not r["ok"]
        assert r["conflict"]

    async def test_convert_idea_to_task(self, v1_store, project):
        idea = await v1_store.submit_idea(
            project_id="proj_v1", content="要转成任务的灵感")
        task = await v1_store.convert_idea_to_task(idea["idea_id"], "task_from_idea")
        assert task["task_id"] == "task_from_idea"
        assert task["status"] == "backlog"
        assert task["source_idea_id"] == idea["idea_id"]
        # idea 已 converted
        converted = await v1_store.get_idea(idea["idea_id"])
        assert converted["status"] == "converted"
        assert converted["converted_task_id"] == "task_from_idea"


@pytest.mark.asyncio
class TestRelations:
    async def test_add_and_list_relation(self, v1_store, project):
        await v1_store.create_task(task_id="t_a", project_id="proj_v1", title="A")
        await v1_store.create_task(task_id="t_b", project_id="proj_v1", title="B")
        r = await v1_store.add_relation("t_a", "t_b", "blocks")
        assert r["ok"]
        rels = await v1_store.list_relations("t_a")
        assert len(rels) == 1
        assert rels[0]["relation_type"] == "blocks"

    async def test_list_blocked_by(self, v1_store, project):
        await v1_store.create_task(task_id="t_blocker", project_id="proj_v1", title="阻塞者")
        await v1_store.create_task(task_id="t_blocked", project_id="proj_v1", title="被阻塞")
        await v1_store.add_relation("t_blocker", "t_blocked", "blocks")
        blockers = await v1_store.list_blocked_by("t_blocked")
        assert len(blockers) == 1
        assert blockers[0]["task_id"] == "t_blocker"

    async def test_cycle_detection(self, v1_store, project):
        """parent 关系成环应被触发器拦截。"""
        await v1_store.create_task(task_id="t_p1", project_id="proj_v1", title="P1")
        await v1_store.create_task(task_id="t_p2", project_id="proj_v1", title="P2")
        # P1 parent→ P2
        r1 = await v1_store.add_relation("t_p1", "t_p2", "parent")
        assert r1["ok"]
        # P2 parent→ P1 → 成环，应被拒
        r2 = await v1_store.add_relation("t_p2", "t_p1", "parent")
        assert not r2["ok"]
        assert r2["error"] == "relation_cycle"


@pytest.mark.asyncio
class TestReportsComments:
    async def test_submit_and_get_report(self, v1_store, task):
        report = await v1_store.submit_report(
            task_id="task_v1_1", agent_id="agent_coder",
            content="## 完成报告\n已实现缓存模块",
            artifact_ids=["art_1", "art_2"],
            self_check={"tests_pass": True})
        assert report["report_id"].startswith("report_")
        assert report["artifact_ids"] == ["art_1", "art_2"]
        assert report["acceptance_self_check"]["tests_pass"] is True

        got = await v1_store.get_report(report["report_id"])
        assert got["content"].startswith("## 完成报告")

    async def test_list_reports(self, v1_store, task):
        await v1_store.submit_report(task_id="task_v1_1", agent_id="a1", content="R1")
        await v1_store.submit_report(task_id="task_v1_1", agent_id="a2", content="R2")
        reports = await v1_store.list_reports("task_v1_1")
        assert len(reports) == 2

    async def test_add_and_list_comment(self, v1_store, task):
        cmt = await v1_store.add_comment(
            task_id="task_v1_1", body="同意，可以关闭",
            author_type="user", author_id="u1", author_name="张三",
            comment_type="review", decision="approve")
        assert cmt["comment_id"].startswith("cmt_")
        assert cmt["decision"] == "approve"

        cmts = await v1_store.list_comments("task_v1_1")
        assert len(cmts) == 1
        assert cmts[0]["body"] == "同意，可以关闭"

    async def test_list_comments_by_type(self, v1_store, task):
        await v1_store.add_comment(task_id="task_v1_1", body="讨论1", author_type="user", comment_type="discussion")
        await v1_store.add_comment(task_id="task_v1_1", body="评审1", author_type="user", comment_type="review")
        discussions = await v1_store.list_comments("task_v1_1", comment_type="discussion")
        assert len(discussions) == 1
        assert discussions[0]["comment_type"] == "discussion"


@pytest.mark.asyncio
class TestAcceptanceCriteria:
    async def test_add_and_list(self, v1_store, task):
        c1 = await v1_store.add_criteria(task_id="task_v1_1", description="单测覆盖率≥80%", check_type="auto")
        c2 = await v1_store.add_criteria(task_id="task_v1_1", description="人工验收交互", check_type="manual")
        assert c1["criteria_id"].startswith("crit_")
        criteria = await v1_store.list_criteria("task_v1_1")
        assert len(criteria) == 2

    async def test_update_status(self, v1_store, task):
        c = await v1_store.add_criteria(task_id="task_v1_1", description="测试条件")
        updated = await v1_store.update_criteria_status(c["criteria_id"], c["version"], "passed")
        assert updated["status"] == "passed"
        assert updated["checked_at"] is not None

    async def test_update_conflict(self, v1_store, task):
        c = await v1_store.add_criteria(task_id="task_v1_1", description="冲突测试")
        # 先用正确 version 更新
        await v1_store.update_criteria_status(c["criteria_id"], c["version"], "passed")
        # 再用旧 version → 冲突
        r = await v1_store.update_criteria_status(c["criteria_id"], c["version"], "failed")
        assert r is None


@pytest.mark.asyncio
class TestDesignDocs:
    async def test_create_and_get_doc(self, v1_store, project):
        doc = await v1_store.create_doc(
            doc_id="doc_1", project_id="proj_v1",
            title="架构设计文档", path="/docs/arch.md",
            content_hash="abc123")
        assert doc["doc_id"] == "doc_1"
        got = await v1_store.get_doc("doc_1")
        assert got["title"] == "架构设计文档"

    async def test_list_docs(self, v1_store, project):
        await v1_store.create_doc(doc_id="d1", project_id="proj_v1", title="D1", path="/d1.md")
        await v1_store.create_doc(doc_id="d2", project_id="proj_v1", title="D2", path="/d2.md")
        docs = await v1_store.list_docs("proj_v1")
        assert len(docs) == 2

    async def test_proposal_apply_flow(self, v1_store, project, task):
        """提案 → 手动 approved → apply → 变更历史 + 文档 hash 更新。"""
        doc = await v1_store.create_doc(doc_id="doc_flow", project_id="proj_v1",
                                         title="流测试", path="/flow.md", content_hash="old_hash")
        proposal = await v1_store.create_doc_proposal(
            doc_id="doc_flow", task_id="task_v1_1",
            change_type="modify", new_content="新内容",
            rationale="需求变更", section_path="## 章节1",
            old_content_hash="old_hash")
        assert proposal["status"] == "pending"

        # 手动置为 approved（绕过 approve 方法，直接 SQL）
        v1_store._exec(
            "UPDATE doc_change_proposals SET status = 'approved' WHERE proposal_id = ?",
            (proposal["proposal_id"],))

        # apply
        r = await v1_store.apply_doc_proposal(proposal["proposal_id"], proposal["version"], "new_hash")
        assert r["ok"]

        # 验证提案状态
        applied = await v1_store.get_doc_proposal(proposal["proposal_id"])
        assert applied["status"] == "applied"
        assert applied["applied_at"] is not None

        # 验证文档 hash 更新
        doc_after = await v1_store.get_doc("doc_flow")
        assert doc_after["content_hash"] == "new_hash"
        assert doc_after["last_updated_by_task"] == "task_v1_1"

    async def test_apply_conflict(self, v1_store, project, task):
        doc = await v1_store.create_doc(doc_id="doc_c", project_id="proj_v1",
                                         title="C", path="/c.md")
        proposal = await v1_store.create_doc_proposal(
            doc_id="doc_c", task_id="task_v1_1", change_type="add", new_content="x")
        # 不 approved 直接 apply → 冲突
        r = await v1_store.apply_doc_proposal(proposal["proposal_id"], proposal["version"], "h")
        assert not r["ok"]
        assert r["conflict"]


@pytest.mark.asyncio
class TestTaskRuns:
    async def test_link_and_list(self, v1_store, task):
        # task_runs.run_id/session_id 有 FK 到 runs/sessions，先建记录
        ts = "2026-01-01T00:00:00+00:00"
        v1_store._exec(
            "INSERT INTO sessions (session_id, user_id, agent_id, last_activity_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("sess_456", "u1", "coding_agent", ts, ts, ts))
        v1_store._exec(
            "INSERT INTO runs (run_id, session_id, run_mode, agent_id, initial_message, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run_123", "sess_456", "conversational", "coding_agent", "hi", ts, ts))
        link = await v1_store.link_task_run(
            task_id="task_v1_1", role="main_execution",
            run_id="run_123", session_id="sess_456")
        assert link["link_id"].startswith("link_")
        assert link["run_id"] == "run_123"

        links = await v1_store.list_task_runs("task_v1_1")
        assert len(links) == 1


@pytest.mark.asyncio
class TestActivities:
    async def test_add_and_list(self, v1_store, task):
        act = await v1_store.add_activity(
            task_id="task_v1_1", actor_type="agent", actor_id="a1",
            actor_name="coder",
            changes={"status": {"before": "in_progress", "after": "validating"}})
        assert act["activity_id"].startswith("act_")
        assert act["changes"]["status"]["after"] == "validating"

        acts = await v1_store.list_activities("task_v1_1")
        assert len(acts) == 1


@pytest.mark.asyncio
class TestArtifacts:
    async def test_add_and_list(self, v1_store, task):
        art = await v1_store.add_artifact(
            task_id="task_v1_1", type="code", path="/src/cache.py",
            content_hash="sha_abc", description="缓存模块实现")
        assert art["artifact_id"].startswith("art_")
        arts = await v1_store.list_artifacts("task_v1_1")
        assert len(arts) == 1
        assert arts[0]["type"] == "code"


@pytest.mark.asyncio
class TestEvents:
    async def test_add_and_list(self, v1_store, task):
        evt = await v1_store.add_event(
            task_id="task_v1_1", event_type="stage_started",
            actor="agent_coder", stage_id="stage_1",
            payload={"node": "execute_coding"})
        assert evt["event_id"].startswith("evt_")
        assert evt["payload"]["node"] == "execute_coding"

        evts = await v1_store.list_events("task_v1_1")
        assert len(evts) == 1


@pytest.mark.asyncio
class TestAgentStyles:
    async def test_create_and_get(self, v1_store):
        style = await v1_store.create_style(
            style_id="style_cautious", name="谨慎型",
            description="低风险偏好",
            system_prompt_overlay="优先选择保守方案",
            permissions_overlay={"denied_tools_add": ["bash"]},
            model_overlay={"id": "gpt-4", "provider": "openai"})
        assert style["style_id"] == "style_cautious"
        assert style["permissions_overlay"]["denied_tools_add"] == ["bash"]
        assert style["model_overlay"]["id"] == "gpt-4"

        got = await v1_store.get_style("style_cautious")
        assert got["name"] == "谨慎型"

    async def test_list_styles(self, v1_store):
        await v1_store.create_style(style_id="s1", name="B风格")
        await v1_store.create_style(style_id="s2", name="A风格")
        styles = await v1_store.list_styles()
        assert len(styles) == 2
        # 按 name 排序
        assert styles[0]["name"] == "A风格"


@pytest.mark.asyncio
class TestGlobalRevisionTrigger:
    """V1 新表写入应触发 global_revision 递增。"""

    async def test_revision_increments_on_idea(self, v1_store, project):
        rev0 = await v1_store.get_revision()
        await v1_store.submit_idea(project_id="proj_v1", content="新灵感")
        rev1 = await v1_store.get_revision()
        assert rev1 > rev0

    async def test_revision_increments_on_comment(self, v1_store, task):
        rev0 = await v1_store.get_revision()
        await v1_store.add_comment(task_id="task_v1_1", body="评论", author_type="user")
        rev1 = await v1_store.get_revision()
        assert rev1 > rev0

    async def test_revision_increments_on_style(self, v1_store):
        rev0 = await v1_store.get_revision()
        await v1_store.create_style(style_id="s_rev", name="revision测试")
        rev1 = await v1_store.get_revision()
        assert rev1 > rev0
