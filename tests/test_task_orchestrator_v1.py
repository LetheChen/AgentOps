"""V1 TaskOrchestrator 单测（14 态机 + execute_coding/rollback/close）。

验证：
- V1 advance_stage：合法/非法转移 + 风险门槛 + requires_user + 乐观锁冲突重试
- execute_coding：状态校验 + mock 派发 + task_run 弱关联 + activity 记录
- rollback_task：三级回退（local/partial/global）+ 评论记录
- close_task：硬约束检查（验收标准 + 文档提案）+ 两步推进
"""
import os
import tempfile
import sys
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audit.store import SqliteEventStore
from task.store import TaskStore
from task.orchestrator import TaskOrchestrator
from task.status import TaskStatus


@pytest.fixture
def v1_orch():
    """V1 启用的 Orchestrator（p0_mode=False，14 态机）。"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    conn = SqliteEventStore(db_path=p, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    orch = TaskOrchestrator(store, p0_mode=False)
    yield orch
    conn._conn.close()


@pytest_asyncio.fixture
async def project(v1_orch):
    return await v1_orch.store.create_project(
        project_id="proj_v1", name="V1项目", type="code")


async def _advance_to(v1_orch, task_id: str, target: str):
    """辅助：把任务推进到指定状态（按 v1.2 主线逐级推进：用户视角可过评审门禁）。"""
    task = await v1_orch.store.get_task(task_id)
    path = {
        "discussing": ["discussing"],
        "decomposing": ["discussing", "decomposing"],
        "reviewing": ["discussing", "decomposing", "reviewing"],
        "backlog": ["discussing", "decomposing", "reviewing", "backlog"],
        "in_progress": ["discussing", "decomposing", "reviewing", "backlog", "in_progress"],
        "validating": ["discussing", "decomposing", "reviewing", "backlog",
                       "in_progress", "validating"],
    }
    for target_status in path[target]:
        r = await v1_orch.advance_stage(
            task_id=task_id, target_status=target_status,
            if_version=task["version"], actor="user")
        assert r["ok"], f"推进失败 {task['status']}→{target_status}: {r}"
        task = r["task"]


# ============================================================
# V1 advance_stage
# ============================================================

@pytest.mark.asyncio
class TestV1AdvanceStage:
    async def test_legal_mainline(self, v1_orch, project):
        """低风险任务主线：idea→backlog→discussing→decomposing→in_progress→validating。"""
        await v1_orch.store.create_task(
            task_id="t_main", project_id="proj_v1", title="主线任务",
            risk_level="low")
        await _advance_to(v1_orch, "t_main", "validating")
        task = await v1_orch.store.get_task("t_main")
        assert task["status"] == "validating"

    async def test_high_risk_review_gate(self, v1_orch, project):
        """v1.2：高风险 reviewing→backlog 由 agent 触发应被拦截（必须用户放行）。"""
        await v1_orch.store.create_task(
            task_id="t_high", project_id="proj_v1", title="高风险任务",
            risk_level="high")
        # 推到 reviewing（新主线：idea→discussing→decomposing→reviewing）
        await _advance_to(v1_orch, "t_high", "reviewing")
        task = await v1_orch.store.get_task("t_high")
        # agent 触发 reviewing→backlog → 应被拒
        r = await v1_orch.advance_stage(
            task_id="t_high", target_status="backlog",
            if_version=task["version"], actor="agent")
        assert not r["ok"]
        assert r["error"] == "review_required_for_high_risk"

    async def test_high_risk_via_reviewing(self, v1_orch, project):
        """v1.2：高风险经用户确认 reviewing→backlog 放行合法。"""
        await v1_orch.store.create_task(
            task_id="t_high2", project_id="proj_v1", title="高风险任务2",
            risk_level="high")
        # idea → discussing → decomposing → reviewing
        await _advance_to(v1_orch, "t_high2", "reviewing")
        task = await v1_orch.store.get_task("t_high2")
        # reviewing → backlog（用户确认放行，入待办池）
        r = await v1_orch.advance_stage(
            task_id="t_high2", target_status="backlog",
            if_version=task["version"], actor="user")
        assert r["ok"]
        assert r["task"]["status"] == "backlog"

    async def test_illegal_transition(self, v1_orch, project):
        """非法转移被拒。"""
        await v1_orch.store.create_task(
            task_id="t_illegal", project_id="proj_v1", title="非法转移测试")
        task = await v1_orch.store.get_task("t_illegal")
        # idea → in_progress 跳级，非法
        r = await v1_orch.advance_stage(
            task_id="t_illegal", target_status="in_progress",
            if_version=task["version"], actor="user")
        assert not r["ok"]
        assert r["error"] == "illegal_transition"

    async def test_requires_user_blocks_agent(self, v1_orch, project):
        """agent 不能触发 requires_user=True 的转移（v1.2：idea→discussing 立项门禁）。"""
        await v1_orch.store.create_task(
            task_id="t_agent", project_id="proj_v1", title="agent校验",
            risk_level="low")
        task = await v1_orch.store.get_task("t_agent")
        # idea → discussing 是 requires_user=True
        r = await v1_orch.advance_stage(
            task_id="t_agent", target_status="discussing",
            if_version=task["version"], actor="agent")
        assert not r["ok"]
        assert r["error"] == "requires_user_approval"

    async def test_optimistic_lock_conflict_retry(self, v1_orch, project):
        """乐观锁冲突重试一次。"""
        await v1_orch.store.create_task(
            task_id="t_lock", project_id="proj_v1", title="锁冲突测试",
            risk_level="low")
        task = await v1_orch.store.get_task("t_lock")
        # 先用正确 version 推到 discussing（模拟并发：version 已变）
        await v1_orch.store.update_task_status("t_lock", "discussing", task["version"])
        # 用旧 version 推到 decomposing → 应触发冲突重试
        r = await v1_orch.advance_stage(
            task_id="t_lock", target_status="decomposing",
            if_version=task["version"], actor="user")
        assert r["ok"]
        assert r["task"]["status"] == "decomposing"

    async def test_terminal_rejected(self, v1_orch, project):
        """终态不可再转移。"""
        await v1_orch.store.create_task(
            task_id="t_term", project_id="proj_v1", title="终态测试",
            risk_level="low", status="closed")
        task = await v1_orch.store.get_task("t_term")
        r = await v1_orch.advance_stage(
            task_id="t_term", target_status="backlog",
            if_version=task["version"], actor="user")
        assert not r["ok"]
        assert r["error"] == "already_terminal"

    async def test_activity_recorded(self, v1_orch, project):
        """状态推进后写 task_activities。"""
        await v1_orch.store.create_task(
            task_id="t_act", project_id="proj_v1", title="activity测试",
            risk_level="low")
        task = await v1_orch.store.get_task("t_act")
        await v1_orch.advance_stage(
            task_id="t_act", target_status="discussing",
            if_version=task["version"], actor="user")
        acts = await v1_orch.store.list_activities("t_act")
        assert len(acts) == 1
        assert acts[0]["changes"]["status"]["after"] == "discussing"


# ============================================================
# execute_coding
# ============================================================

class MockTerminalManager:
    """terminal 会话 mock。"""
    def __init__(self):
        self.sessions = {}

    async def create_session(self, name: str, cwd: str = "") -> str:
        tid = f"term_{name}"
        self.sessions[tid] = []
        return tid

    async def send_keys(self, terminal_id: str, text: str):
        if terminal_id in self.sessions:
            self.sessions[terminal_id].append(text)

    async def append_output(self, terminal_id: str, text: str):
        if terminal_id in self.sessions:
            self.sessions[terminal_id].append(text)


class MockStyleLoader:
    """agent 风格 mock。"""
    async def get_overlay(self, style_id: str) -> str:
        return f"=== 风格覆盖 ===\n风格ID: {style_id}"


@pytest_asyncio.fixture
async def orch_with_deps():
    """带 terminal + style mock 的 V1 orchestrator。"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    conn = SqliteEventStore(db_path=p, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    terminal = MockTerminalManager()
    styles = MockStyleLoader()
    orch = TaskOrchestrator(store, p0_mode=False,
                            style_loader=styles, terminal_manager=terminal)
    # 预建项目（与 orch 共享同一数据库）
    await orch.store.create_project(
        project_id="proj_deps", name="依赖测试项目", type="code")
    yield orch, terminal
    conn._conn.close()


@pytest.mark.asyncio
class TestExecuteCoding:
    async def test_not_in_progress_rejected(self, v1_orch, project):
        """非 in_progress 状态拒绝派发。"""
        await v1_orch.store.create_task(
            task_id="t_exec1", project_id="proj_v1", title="派发测试1",
            risk_level="low")
        r = await v1_orch.execute_coding("t_exec1")
        assert not r["ok"]
        assert r["error"] == "task_not_in_progress"

    async def test_mock_dispatch(self, orch_with_deps):
        """in_progress 状态 mock 派发，返回 run_id + 弱关联 task_run。"""
        orch, terminal = orch_with_deps
        await orch.store.create_task(
            task_id="t_exec2", project_id="proj_deps", title="派发测试2",
            risk_level="low")
        await _advance_to(orch, "t_exec2", "in_progress")

        r = await orch.execute_coding("t_exec2", style_id="cautious")
        assert r["ok"]
        assert r["mock"] is True
        assert r["run_id"].startswith("mock_run_")
        assert r["terminal_session_id"].startswith("term_")
        assert r["style_id"] == "cautious"

        # terminal_session_id 已回写
        task = await orch.store.get_task("t_exec2")
        assert task["terminal_session_id"] == r["terminal_session_id"]

        # task_run 弱关联已建（mock 模式下 run_id 为空，terminal_session_id 仍关联）
        runs = await orch.store.list_task_runs("t_exec2")
        assert len(runs) == 1
        assert runs[0]["terminal_session_id"] == r["terminal_session_id"]

        # terminal 写了启动横幅（含 harness 名）
        assert len(terminal.sessions[r["terminal_session_id"]]) == 1
        assert "coding_agent(claude_code) dispatched" in \
            terminal.sessions[r["terminal_session_id"]][0]

        # activity 记录了 dispatch
        acts = await orch.store.list_activities("t_exec2")
        dispatch_acts = [a for a in acts if "dispatch" in a["changes"]]
        assert len(dispatch_acts) == 1
        assert dispatch_acts[0]["changes"]["dispatch"]["after"]["mock"] is True

    async def test_execute_from_backlog_auto_advances(self, orch_with_deps):
        """友好 UX：backlog 态点「执行编码」自动推进到 in_progress 后派发。"""
        orch, terminal = orch_with_deps
        await orch.store.create_task(
            task_id="t_exec_backlog", project_id="proj_deps",
            title="backlog直执行", risk_level="low")
        # 推到 backlog（不推到 in_progress）
        await _advance_to(orch, "t_exec_backlog", "backlog")
        task = await orch.store.get_task("t_exec_backlog")
        assert task["status"] == "backlog"

        r = await orch.execute_coding("t_exec_backlog")
        assert r["ok"]
        # 派发后任务已转 in_progress（自动推进 + dispatch）
        fresh = await orch.store.get_task("t_exec_backlog")
        assert fresh["status"] == "in_progress"
        # activity 记录了「in_progress」状态推进 + dispatch
        acts = await orch.store.list_activities("t_exec_backlog")
        status_acts = [a for a in acts if a.get("changes", {}).get("status")]
        assert any(a["changes"]["status"]["after"] == "in_progress"
                   for a in status_acts)
        dispatch_acts = [a for a in acts if "dispatch" in a["changes"]]
        assert len(dispatch_acts) == 1

    async def test_execute_from_other_state_rejected(self, orch_with_deps):
        """非 backlog/in_progress 状态（讨论中/评审中）拒绝派发。"""
        orch, _ = orch_with_deps
        await orch.store.create_task(
            task_id="t_exec_review", project_id="proj_deps",
            title="评审中拒绝", risk_level="low")
        await _advance_to(orch, "t_exec_review", "reviewing")
        r = await orch.execute_coding("t_exec_review")
        assert not r["ok"]
        assert r["error"] == "task_not_in_progress"
        assert "reviewing" in r.get("hint", "")

    async def test_prompt_assembly(self, orch_with_deps):
        """prompt 装配包含任务上下文 + 验收标准 + 风格 overlay。"""
        orch, _ = orch_with_deps
        await orch.store.create_task(
            task_id="t_exec3", project_id="proj_deps", title="prompt装配",
            description="实现缓存模块", risk_level="low")
        await _advance_to(orch, "t_exec3", "in_progress")
        await orch.store.add_criteria(
            task_id="t_exec3", description="单测覆盖率≥80%", check_type="auto")

        task = await orch.store.get_task("t_exec3")
        proj = await orch.store.get_project("proj_deps")
        prompt = await orch._build_coding_prompt(task, proj, "=== 风格覆盖 ===\n谨慎型")
        assert "prompt装配" in prompt
        assert "实现缓存模块" in prompt
        assert "单测覆盖率≥80%" in prompt
        assert "=== 风格覆盖 ===" in prompt


# ============================================================
# rollback_task
# ============================================================

@pytest.mark.asyncio
class TestRollbackTask:
    async def test_invalid_target(self, v1_orch, project):
        """无效回退目标。"""
        await v1_orch.store.create_task(
            task_id="t_rb1", project_id="proj_v1", title="回退测试1",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb1", "validating")
        task = await v1_orch.store.get_task("t_rb1")
        r = await v1_orch.rollback_task("t_rb1", "invalid", task["version"])
        assert not r["ok"]
        assert r["error"] == "invalid_rollback_target"

    async def test_local_rollback(self, v1_orch, project):
        """local 回退到 in_progress。"""
        await v1_orch.store.create_task(
            task_id="t_rb2", project_id="proj_v1", title="local回退",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb2", "validating")
        task = await v1_orch.store.get_task("t_rb2")

        r = await v1_orch.rollback_task(
            "t_rb2", "local", task["version"], comment="执行偏差")
        assert r["ok"]
        assert r["task"]["status"] == "in_progress"

        # 退回评论已记录
        cmts = await v1_orch.store.list_comments("t_rb2", comment_type="review")
        assert len(cmts) == 1
        assert cmts[0]["decision"] == "request_changes"
        assert cmts[0]["rollback_target"] == "local"
        assert cmts[0]["body"] == "执行偏差"

    async def test_partial_rollback(self, v1_orch, project):
        """partial 回退到 decomposing。"""
        await v1_orch.store.create_task(
            task_id="t_rb3", project_id="proj_v1", title="partial回退",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb3", "validating")
        task = await v1_orch.store.get_task("t_rb3")

        r = await v1_orch.rollback_task("t_rb3", "partial", task["version"])
        assert r["ok"]
        assert r["task"]["status"] == "decomposing"

    async def test_global_rollback(self, v1_orch, project):
        """global 回退到 discussing。"""
        await v1_orch.store.create_task(
            task_id="t_rb4", project_id="proj_v1", title="global回退",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb4", "validating")
        task = await v1_orch.store.get_task("t_rb4")

        r = await v1_orch.rollback_task("t_rb4", "global", task["version"])
        assert r["ok"]
        assert r["task"]["status"] == "discussing"

    async def test_target_status_priority(self, v1_orch, project):
        """target_status 优先于 rollback_target；且自动归一化 rollback_target 别名。"""
        await v1_orch.store.create_task(
            task_id="t_rb5", project_id="proj_v1", title="精确阶段回退",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb5", "validating")
        task = await v1_orch.store.get_task("t_rb5")

        # 不传 rollback_target，直接给 target_status=in_progress
        r = await v1_orch.rollback_task(
            "t_rb5", if_version=task["version"], target_status="in_progress")
        assert r["ok"]
        assert r["task"]["status"] == "in_progress"
        # 评论归一化：rollback_target 应为 'local'（in_progress 的别名）
        cmts = await v1_orch.store.list_comments("t_rb5", comment_type="review")
        assert len(cmts) == 1
        assert cmts[0]["decision"] == "request_changes"
        assert cmts[0]["rollback_target"] == "local"

    async def test_target_status_closing(self, v1_orch, project):
        """closing 状态下用 target_status 退回到任意前置阶段。"""
        await v1_orch.store.create_task(
            task_id="t_rb6", project_id="proj_v1", title="关闭前退回",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb6", "validating")
        task = await v1_orch.store.get_task("t_rb6")
        # validating → closing
        r1 = await v1_orch.advance_stage(
            task_id="t_rb6", target_status="closing",
            if_version=task["version"], actor="user")
        assert r1["ok"]
        task2 = await v1_orch.store.get_task("t_rb6")
        # closing → discussing（关闭前退回方案），走状态机合法转移校验
        r2 = await v1_orch.rollback_task(
            "t_rb6", if_version=task2["version"], target_status="discussing")
        assert r2["ok"]
        assert r2["task"]["status"] == "discussing"

    async def test_target_status_illegal(self, v1_orch, project):
        """target_status 违反状态机转移规则 → 返回 illegal_transition。"""
        await v1_orch.store.create_task(
            task_id="t_rb7", project_id="proj_v1", title="非法转移",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb7", "validating")
        task = await v1_orch.store.get_task("t_rb7")
        # validating → backlog（无直接转移边）
        r = await v1_orch.rollback_task(
            "t_rb7", if_version=task["version"], target_status="backlog")
        assert not r["ok"]
        assert r["error"] == "illegal_transition"

    async def test_missing_both_targets(self, v1_orch, project):
        """既不传 rollback_target 也不传 target_status → invalid_rollback_target。"""
        await v1_orch.store.create_task(
            task_id="t_rb8", project_id="proj_v1", title="缺失目标",
            risk_level="low")
        await _advance_to(v1_orch, "t_rb8", "validating")
        task = await v1_orch.store.get_task("t_rb8")
        r = await v1_orch.rollback_task("t_rb8", if_version=task["version"])
        assert not r["ok"]
        assert r["error"] == "invalid_rollback_target"


# ============================================================
# close_task
# ============================================================

@pytest.mark.asyncio
class TestCloseTask:
    async def test_acceptance_not_passed(self, v1_orch, project):
        """验收标准未全通过 → 阻止关闭。"""
        await v1_orch.store.create_task(
            task_id="t_close1", project_id="proj_v1", title="关闭测试1",
            risk_level="low")
        await _advance_to(v1_orch, "t_close1", "validating")
        c = await v1_orch.store.add_criteria(
            task_id="t_close1", description="验收条件1")
        # criteria 默认 pending，未通过
        task = await v1_orch.store.get_task("t_close1")
        r = await v1_orch.close_task("t_close1", task["version"])
        assert not r["ok"]
        assert r["error"] == "acceptance_not_passed"

    async def test_doc_proposals_pending(self, v1_orch, project):
        """有 pending 文档提案 → 阻止关闭。"""
        await v1_orch.store.create_task(
            task_id="t_close2", project_id="proj_v1", title="关闭测试2",
            risk_level="low")
        await _advance_to(v1_orch, "t_close2", "validating")
        # 验收标准全通过
        c = await v1_orch.store.add_criteria(
            task_id="t_close2", description="验收条件2")
        await v1_orch.store.update_criteria_status(c["criteria_id"], c["version"], "passed")
        # 建 pending 文档提案
        await v1_orch.store.create_doc(
            doc_id="doc_close2", project_id="proj_v1", title="D", path="/d.md")
        await v1_orch.store.create_doc_proposal(
            doc_id="doc_close2", task_id="t_close2", change_type="modify", new_content="x")

        task = await v1_orch.store.get_task("t_close2")
        r = await v1_orch.close_task("t_close2", task["version"])
        assert not r["ok"]
        assert r["error"] == "doc_proposals_pending"

    async def test_close_success(self, v1_orch, project):
        """全部约束满足 → validating→closing→closed 两步推进。"""
        await v1_orch.store.create_task(
            task_id="t_close3", project_id="proj_v1", title="关闭成功",
            risk_level="low")
        await _advance_to(v1_orch, "t_close3", "validating")
        # 验收标准全通过
        c = await v1_orch.store.add_criteria(
            task_id="t_close3", description="验收条件3")
        await v1_orch.store.update_criteria_status(c["criteria_id"], c["version"], "passed")
        # 无 pending 文档提案

        task = await v1_orch.store.get_task("t_close3")
        r = await v1_orch.close_task("t_close3", task["version"])
        assert r["ok"]
        assert r["task"]["status"] == "closed"
        assert r["task"]["closed_at"] is not None

    async def test_close_no_criteria_passes(self, v1_orch, project):
        """无验收标准时视为通过（允许关闭）。"""
        await v1_orch.store.create_task(
            task_id="t_close4", project_id="proj_v1", title="无标准关闭",
            risk_level="low")
        await _advance_to(v1_orch, "t_close4", "validating")
        task = await v1_orch.store.get_task("t_close4")
        r = await v1_orch.close_task("t_close4", task["version"])
        assert r["ok"]
        assert r["task"]["status"] == "closed"

    async def test_close_from_closing_direct(self, v1_orch, project):
        """已在 closing 状态时直接推进到 closed。"""
        await v1_orch.store.create_task(
            task_id="t_close5", project_id="proj_v1", title="closing直推",
            risk_level="low")
        await _advance_to(v1_orch, "t_close5", "validating")
        task = await v1_orch.store.get_task("t_close5")
        # 先推到 closing
        r = await v1_orch.advance_stage(
            task_id="t_close5", target_status="closing",
            if_version=task["version"], actor="user")
        assert r["ok"]
        # 从 closing 直接 close
        r2 = await v1_orch.close_task("t_close5", r["task"]["version"])
        assert r2["ok"]
        assert r2["task"]["status"] == "closed"
