"""V1 任务管理工具层单测（11 个 tool handler）。

验证：
- 每个工具 handler 在 orchestrator 未初始化时返回 orchestrator_unavailable 错误
- 核心工具（task_rollback、task_close、task_execute_coding）在 orchestrator 已初始化时返回正确结果
- 额外覆盖 task_claim / task_approval / task_validate / task_commit_stage /
  task_link_run / task_submit_report / task_convert_idea / task_assign_agent 的成功路径
- 使用与 tests/test_task_orchestrator_v1.py 相同的 fixture 模式
  （tempfile + SqliteEventStore + TaskStore + TaskOrchestrator + set_task_orchestrator）
- 用 try/finally 确保 set_task_orchestrator 在用例结束后清理
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
from orchestrator._registry import set_task_orchestrator

from tools.task_claim import task_claim
from tools.task_approval import task_approval
from tools.task_validate import task_validate
from tools.task_convert_idea import task_convert_idea
from tools.task_assign_agent import task_assign_agent
from tools.task_execute_coding import task_execute_coding
from tools.task_commit_stage import task_commit_stage
from tools.task_rollback import task_rollback
from tools.task_close import task_close
from tools.task_link_run import task_link_run
from tools.task_submit_report import task_submit_report


# ============================================================
# 辅助
# ============================================================

async def _advance_to(orch: TaskOrchestrator, task_id: str, target: str):
    """把任务推进到指定状态（按 v1.2 主线逐级推进，用户视角可过评审门禁）。"""
    task = await orch.store.get_task(task_id)
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
        r = await orch.advance_stage(
            task_id=task_id, target_status=target_status,
            if_version=task["version"], actor="user")
        assert r["ok"], f"推进失败 {task['status']}→{target_status}: {r}"
        task = r["task"]


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def clean_registry():
    """确保 _registry 中无 task_orchestrator（用于 unavailable 测试）。

    用 try/finally 双向清理：进入时清空，退出时再清空，
    防止其他用例残留影响。
    """
    set_task_orchestrator(None)
    try:
        yield
    finally:
        set_task_orchestrator(None)


@pytest_asyncio.fixture
async def orch_registered():
    """注册到 _registry 的 V1 orchestrator + 预建项目，用例结束清理。

    使用 try/finally 确保 set_task_orchestrator(None) 一定执行，
    避免全局单例污染后续用例。
    """
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    conn = SqliteEventStore(db_path=p, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    orch = TaskOrchestrator(store, p0_mode=False)
    set_task_orchestrator(orch)
    await orch.store.create_project(
        project_id="proj_tools", name="工具测试项目", type="code")
    try:
        yield orch
    finally:
        set_task_orchestrator(None)
        conn._conn.close()


@pytest_asyncio.fixture
async def orch_with_terminal():
    """带 terminal + style mock 的注册 orchestrator（execute_coding 用）。"""
    from tests.test_task_orchestrator_v1 import MockTerminalManager, MockStyleLoader
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    conn = SqliteEventStore(db_path=p, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    terminal = MockTerminalManager()
    styles = MockStyleLoader()
    orch = TaskOrchestrator(store, p0_mode=False,
                            style_loader=styles, terminal_manager=terminal)
    set_task_orchestrator(orch)
    await orch.store.create_project(
        project_id="proj_tools", name="工具测试项目", type="code")
    try:
        yield orch, terminal
    finally:
        set_task_orchestrator(None)
        conn._conn.close()


# ============================================================
# 1. orchestrator 未初始化 → 所有工具返回 orchestrator_unavailable
# ============================================================

# 11 个工具 + 最小入参（orch 检查在参数校验之前，所以入参仅为占位）
ALL_TOOLS_WITH_ARGS = [
    ("task_claim", task_claim,
     {"task_id": "t1", "if_version": 0, "thread_id": "th1"}),
    ("task_approval", task_approval,
     {"task_id": "t1", "risk_level": "low", "if_version": 0}),
    ("task_validate", task_validate, {"task_id": "t1"}),
    ("task_convert_idea", task_convert_idea,
     {"idea_id": "i1", "task_id": "t1"}),
    ("task_assign_agent", task_assign_agent,
     {"task_id": "t1", "if_version": 0, "assignee_id": "a1"}),
    ("task_execute_coding", task_execute_coding, {"task_id": "t1"}),
    ("task_commit_stage", task_commit_stage,
     {"task_id": "t1", "stage_type": "coding"}),
    ("task_rollback", task_rollback,
     {"task_id": "t1", "rollback_target": "local", "if_version": 0}),
    ("task_close", task_close, {"task_id": "t1", "if_version": 0}),
    ("task_link_run", task_link_run, {"task_id": "t1", "run_id": "r1"}),
    ("task_submit_report", task_submit_report,
     {"task_id": "t1", "agent_id": "a1", "content": "x"}),
]


@pytest.mark.asyncio
class TestOrchestratorUnavailable:
    @pytest.mark.parametrize("tool_name,tool_fn,args", ALL_TOOLS_WITH_ARGS,
                             ids=[t[0] for t in ALL_TOOLS_WITH_ARGS])
    async def test_unavailable(self, clean_registry, tool_name, tool_fn, args):
        """orchestrator 未初始化时，每个工具都返回 orchestrator_unavailable。"""
        result = await tool_fn(args)
        assert result.get("error") == "orchestrator_unavailable", (
            f"{tool_name} 未返回 orchestrator_unavailable，实际: {result}")
        assert "content" in result, f"{tool_name} 返回缺少 content 字段"


# ============================================================
# 2. 核心工具成功路径（task_rollback、task_close、task_execute_coding）
# ============================================================

@pytest.mark.asyncio
class TestTaskRollback:
    async def test_local_rollback_success(self, orch_registered):
        """task_rollback local 回退到 in_progress。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_rb", project_id="proj_tools", title="回退测试",
            risk_level="low")
        await _advance_to(orch, "t_rb", "validating")
        task = await orch.store.get_task("t_rb")

        result = await task_rollback({
            "task_id": "t_rb",
            "rollback_target": "local",
            "if_version": task["version"],
            "comment": "执行偏差",
        })
        assert result.get("error") is None, f"回退失败: {result}"
        assert result["task"]["status"] == "in_progress"
        assert result["if_version"] == result["task"]["version"]
        assert result["rollback_target"] == "local"

        # 退回评论已记录
        cmts = await orch.store.list_comments("t_rb", comment_type="review")
        assert len(cmts) == 1
        assert cmts[0]["decision"] == "request_changes"
        assert cmts[0]["rollback_target"] == "local"

    async def test_partial_rollback_success(self, orch_registered):
        """task_rollback partial 回退到 decomposing。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_rb2", project_id="proj_tools", title="partial回退",
            risk_level="low")
        await _advance_to(orch, "t_rb2", "validating")
        task = await orch.store.get_task("t_rb2")

        result = await task_rollback({
            "task_id": "t_rb2",
            "rollback_target": "partial",
            "if_version": task["version"],
        })
        assert result.get("error") is None, f"回退失败: {result}"
        assert result["task"]["status"] == "decomposing"

    async def test_global_rollback_success(self, orch_registered):
        """task_rollback global 回退到 discussing。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_rb3", project_id="proj_tools", title="global回退",
            risk_level="low")
        await _advance_to(orch, "t_rb3", "validating")
        task = await orch.store.get_task("t_rb3")

        result = await task_rollback({
            "task_id": "t_rb3",
            "rollback_target": "global",
            "if_version": task["version"],
        })
        assert result.get("error") is None, f"回退失败: {result}"
        assert result["task"]["status"] == "discussing"

    async def test_invalid_rollback_target(self, orch_registered):
        """无效 rollback_target 返回 invalid_rollback_target。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_rb4", project_id="proj_tools", title="无效目标",
            risk_level="low")
        await _advance_to(orch, "t_rb4", "validating")
        task = await orch.store.get_task("t_rb4")

        result = await task_rollback({
            "task_id": "t_rb4",
            "rollback_target": "invalid",
            "if_version": task["version"],
        })
        assert result["error"] == "invalid_rollback_target"

    async def test_if_version_string(self, orch_registered):
        """if_version 被序列化为字符串时自动 int() 转换。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_rb5", project_id="proj_tools", title="字符串版本",
            risk_level="low")
        await _advance_to(orch, "t_rb5", "validating")
        task = await orch.store.get_task("t_rb5")

        result = await task_rollback({
            "task_id": "t_rb5",
            "rollback_target": "local",
            "if_version": str(task["version"]),  # 字符串
        })
        assert result.get("error") is None, f"字符串 if_version 转换失败: {result}"
        assert result["task"]["status"] == "in_progress"


@pytest.mark.asyncio
class TestTaskClose:
    async def test_close_no_criteria(self, orch_registered):
        """task_close 无验收标准时视为通过，直接关闭。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_close", project_id="proj_tools", title="关闭测试",
            risk_level="low")
        await _advance_to(orch, "t_close", "validating")
        task = await orch.store.get_task("t_close")

        result = await task_close({
            "task_id": "t_close",
            "if_version": task["version"],
        })
        assert result.get("error") is None, f"关闭失败: {result}"
        assert result["task"]["status"] == "closed"
        assert result["task"]["closed_at"] is not None
        assert result["if_version"] == result["task"]["version"]

    async def test_close_with_passed_criteria(self, orch_registered):
        """task_close 验收标准全通过 → 关闭。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_close2", project_id="proj_tools", title="带标准关闭",
            risk_level="low")
        await _advance_to(orch, "t_close2", "validating")
        c = await orch.store.add_criteria(
            task_id="t_close2", description="单测通过", check_type="auto")
        await orch.store.update_criteria_status(
            c["criteria_id"], c["version"], "passed")

        task = await orch.store.get_task("t_close2")
        result = await task_close({
            "task_id": "t_close2",
            "if_version": task["version"],
        })
        assert result.get("error") is None, f"关闭失败: {result}"
        assert result["task"]["status"] == "closed"

    async def test_close_blocked_by_criteria(self, orch_registered):
        """task_close 验收标准未通过 → 阻止关闭。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_close3", project_id="proj_tools", title="阻止关闭",
            risk_level="low")
        await _advance_to(orch, "t_close3", "validating")
        await orch.store.add_criteria(
            task_id="t_close3", description="未通过标准")
        task = await orch.store.get_task("t_close3")

        result = await task_close({
            "task_id": "t_close3",
            "if_version": task["version"],
        })
        assert result["error"] == "acceptance_not_passed"


@pytest.mark.asyncio
class TestTaskExecuteCoding:
    async def test_mock_dispatch_success(self, orch_with_terminal):
        """task_execute_coding in_progress 状态 mock 派发，返回 run_id + terminal_session_id。"""
        orch, terminal = orch_with_terminal
        await orch.store.create_task(
            task_id="t_exec", project_id="proj_tools", title="派发测试",
            risk_level="low")
        await _advance_to(orch, "t_exec", "in_progress")

        result = await task_execute_coding({
            "task_id": "t_exec",
            "style_id": "cautious",
        })
        assert result.get("error") is None, f"派发失败: {result}"
        assert result["mock"] is True
        assert result["run_id"].startswith("mock_run_")
        assert result["terminal_session_id"].startswith("term_")
        assert result["style_id"] == "cautious"

        # terminal_session_id 已回写
        task = await orch.store.get_task("t_exec")
        assert task["terminal_session_id"] == result["terminal_session_id"]

        # task_run 弱关联已建
        runs = await orch.store.list_task_runs("t_exec")
        assert len(runs) == 1
        assert runs[0]["terminal_session_id"] == result["terminal_session_id"]

    async def test_not_in_progress_rejected(self, orch_registered):
        """task_execute_coding 非 in_progress 状态拒绝派发。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_exec2", project_id="proj_tools", title="非执行态",
            risk_level="low")
        # 任务在 idea 状态（未推进）
        result = await task_execute_coding({"task_id": "t_exec2"})
        assert result["error"] == "task_not_in_progress"

    async def test_default_style_id(self, orch_with_terminal):
        """task_execute_coding 默认 style_id=default。"""
        orch, _ = orch_with_terminal
        await orch.store.create_task(
            task_id="t_exec3", project_id="proj_tools", title="默认风格",
            risk_level="low")
        await _advance_to(orch, "t_exec3", "in_progress")

        result = await task_execute_coding({"task_id": "t_exec3"})
        assert result.get("error") is None, f"派发失败: {result}"
        assert result["style_id"] == "default"


# ============================================================
# 3. 其他工具成功路径（补充覆盖）
# ============================================================

@pytest.mark.asyncio
class TestTaskClaim:
    async def test_claim_from_decomposing(self, orch_registered):
        """v1.2：task_claim 从 backlog（待办池）推进到 in_progress + 绑定 thread_id。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_claim", project_id="proj_tools", title="认领测试",
            risk_level="low")
        await _advance_to(orch, "t_claim", "backlog")
        task = await orch.store.get_task("t_claim")

        result = await task_claim({
            "task_id": "t_claim",
            "if_version": task["version"],
            "thread_id": "thread_001",
        })
        assert result.get("error") is None, f"认领失败: {result}"
        assert result["task"]["status"] == "in_progress"
        assert result["task"]["thread_id"] == "thread_001"
        assert result["if_version"] == result["task"]["version"]

    async def test_claim_backlog_rejected(self, orch_registered):
        """v1.2：task_claim idea 状态拒绝认领（未立项）。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_claim2b", project_id="proj_tools", title="idea拒绝测试",
            risk_level="low", status="idea")
        result = await task_claim({
            "task_id": "t_claim2b",
            "if_version": 0,
            "thread_id": "thread_002",
        })
        assert result["error"] == "not_authorized"

    async def test_claim_already_bound_by_other(self, orch_registered):
        """task_claim 已被其他对话绑定 → 拒绝抢占。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_claim3", project_id="proj_tools", title="抢占测试",
            risk_level="low")
        await _advance_to(orch, "t_claim3", "in_progress")
        # 绑定到 thread_A
        task = await orch.store.get_task("t_claim3")
        await orch.store.update_task_fields(
            "t_claim3", task["version"], thread_id="thread_A")

        # 用 thread_B 认领 → 拒绝
        task = await orch.store.get_task("t_claim3")
        result = await task_claim({
            "task_id": "t_claim3",
            "if_version": task["version"],
            "thread_id": "thread_B",
        })
        assert result["error"] == "claimed_by_other"


@pytest.mark.asyncio
class TestTaskApproval:
    async def test_low_risk_auto_pass(self, orch_registered):
        """task_approval low 风险 → 自动通过 + 推进到 decomposing。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_appr", project_id="proj_tools", title="低风险评审",
            risk_level="low")
        await _advance_to(orch, "t_appr", "discussing")
        task = await orch.store.get_task("t_appr")

        result = await task_approval({
            "task_id": "t_appr",
            "risk_level": "low",
            "if_version": task["version"],
            "proposal_summary": "方案A",
        })
        assert result.get("error") is None, f"评审失败: {result}"
        assert result["gate"] == "auto"
        assert result["approved"] is True
        assert result["task"]["status"] == "decomposing"
        # approved 标记已写
        assert result["task"]["approved"] in (1, True)

    async def test_high_risk_manual_review(self, orch_registered):
        """task_approval high 风险 → 推进到 reviewing。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_appr2", project_id="proj_tools", title="高风险评审",
            risk_level="high")
        await _advance_to(orch, "t_appr2", "discussing")
        task = await orch.store.get_task("t_appr2")

        result = await task_approval({
            "task_id": "t_appr2",
            "risk_level": "high",
            "if_version": task["version"],
        })
        assert result.get("error") is None, f"评审失败: {result}"
        assert result["gate"] == "manual"
        assert result["approved"] is False
        # v1.2：高风险正常进拆解，评审放行（reviewing→backlog）由用户确认
        assert result["task"]["status"] == "decomposing"

    async def test_medium_risk_notify(self, orch_registered):
        """task_approval medium 风险 → 通知用户，不自动推进。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_appr3", project_id="proj_tools", title="中风险评审",
            risk_level="medium")
        await _advance_to(orch, "t_appr3", "discussing")
        task = await orch.store.get_task("t_appr3")

        result = await task_approval({
            "task_id": "t_appr3",
            "risk_level": "medium",
            "if_version": task["version"],
        })
        assert result.get("error") is None, f"评审失败: {result}"
        assert result["gate"] == "notify"
        assert result["approved"] is False
        # medium 不推进，仍在 discussing
        assert result["task"]["status"] == "discussing"


@pytest.mark.asyncio
class TestTaskValidate:
    async def test_all_auto_passed_advance_to_closing(self, orch_registered):
        """task_validate auto 标准全通过 → 推进到 closing。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_val", project_id="proj_tools", title="验收测试",
            risk_level="low")
        await _advance_to(orch, "t_val", "validating")
        await orch.store.add_criteria(
            task_id="t_val", description="文件存在", check_type="auto")
        await orch.store.add_criteria(
            task_id="t_val", description="hash 匹配", check_type="auto")

        result = await task_validate({"task_id": "t_val"})
        assert result.get("error") is None, f"验收失败: {result}"
        assert result["all_passed"] is True
        assert len(result["results"]) == 2
        assert all(r["verdict"] == "passed" for r in result["results"])
        # 已推进到 closing
        assert result["task"]["status"] == "closing"

    async def test_manual_criteria_not_all_passed(self, orch_registered):
        """task_validate 含 manual 标准 → 不全部 passed，不推进。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_val2", project_id="proj_tools", title="含manual验收",
            risk_level="low")
        await _advance_to(orch, "t_val2", "validating")
        await orch.store.add_criteria(
            task_id="t_val2", description="自动检查", check_type="auto")
        await orch.store.add_criteria(
            task_id="t_val2", description="人工确认", check_type="manual")

        result = await task_validate({"task_id": "t_val2"})
        assert result.get("error") is None, f"验收失败: {result}"
        assert result["all_passed"] is False
        # 仍在 validating（未推进）
        assert result["task"]["status"] == "validating"
        # manual 标准为 pending_user
        manual = [r for r in result["results"] if r["check_type"] == "manual"]
        assert len(manual) == 1
        assert manual[0]["verdict"] == "pending_user"


@pytest.mark.asyncio
class TestTaskCommitStage:
    async def test_commit_only(self, orch_registered):
        """task_commit_stage 仅提交 stage_output，不推进状态机。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_cmt", project_id="proj_tools", title="提交阶段",
            risk_level="low")
        await _advance_to(orch, "t_cmt", "discussing")

        result = await task_commit_stage({
            "task_id": "t_cmt",
            "stage_type": "discussing",
            "stage_output": "讨论结论：采用方案A",
        })
        assert result.get("error") is None, f"提交失败: {result}"
        assert result["stage"]["stage_type"] == "discussing"
        assert result["stage"]["stage_output"] == "讨论结论：采用方案A"
        assert result["stage"]["status"] == "committed"
        # 未推进状态机
        assert result.get("task") is None

    async def test_commit_with_advance(self, orch_registered):
        """task_commit_stage 提交 + 推进状态机。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_cmt2", project_id="proj_tools", title="提交并推进",
            risk_level="low")
        await _advance_to(orch, "t_cmt2", "discussing")
        task = await orch.store.get_task("t_cmt2")

        result = await task_commit_stage({
            "task_id": "t_cmt2",
            "stage_type": "discussing",
            "stage_output": "讨论完成，进入拆解",
            "target_status": "decomposing",
            "if_version": task["version"],
            "comment": "讨论结束",
        })
        assert result.get("error") is None, f"提交+推进失败: {result}"
        assert result["stage"]["stage_output"] == "讨论完成，进入拆解"
        assert result["task"]["status"] == "decomposing"


@pytest.mark.asyncio
class TestTaskLinkRun:
    async def test_link_run(self, orch_registered):
        """task_link_run 弱关联 terminal_session（mock 模式典型路径）。

        注：run_id/session_id 在 runs/sessions 表上有 FK 约束，
        mock 模式下传空（orchestrator.execute_coding 同款处理）；
        terminal_session_id 无 FK，可自由关联。
        """
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_link", project_id="proj_tools", title="关联测试",
            risk_level="low")

        result = await task_link_run({
            "task_id": "t_link",
            "terminal_session_id": "term_001",
            "role": "main_execution",
        })
        assert result.get("error") is None, f"关联失败: {result}"
        assert result["link"]["terminal_session_id"] == "term_001"
        assert result["link"]["role"] == "main_execution"
        # run_id/session_id 未传 → 落库为 None
        assert result["link"]["run_id"] is None
        assert result["link"]["session_id"] is None

        # 已落库
        runs = await orch.store.list_task_runs("t_link")
        assert len(runs) == 1
        assert runs[0]["terminal_session_id"] == "term_001"

    async def test_link_run_missing_target(self, orch_registered):
        """task_link_run 无任何 link target → 报错。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_link2", project_id="proj_tools", title="空关联",
            risk_level="low")

        result = await task_link_run({"task_id": "t_link2"})
        assert result["error"] == "missing_link_target"


@pytest.mark.asyncio
class TestTaskSubmitReport:
    async def test_submit_report_with_comment(self, orch_registered):
        """task_submit_report 提交报告 + 同步评论。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_rpt", project_id="proj_tools", title="报告测试",
            risk_level="low")

        result = await task_submit_report({
            "task_id": "t_rpt",
            "agent_id": "coding_agent",
            "content": "已完成编码，单测全通过",
            "artifact_ids": ["art_1", "art_2"],
            "self_check": {"criteria_1": "passed"},
            "comment_body": "请验收",
        })
        assert result.get("error") is None, f"提交失败: {result}"
        assert result["report"]["agent_id"] == "coding_agent"
        assert result["report"]["content"] == "已完成编码，单测全通过"
        assert result["report"]["artifact_ids"] == ["art_1", "art_2"]
        assert result["comment"] is not None
        assert result["comment"]["comment_type"] == "report"
        assert result["comment"]["body"] == "请验收"

        # 已落库
        reports = await orch.store.list_reports("t_rpt")
        assert len(reports) == 1
        cmts = await orch.store.list_comments("t_rpt", comment_type="report")
        assert len(cmts) == 1

    async def test_submit_report_no_comment(self, orch_registered):
        """task_submit_report 不带 comment_body → 只落 report。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_rpt2", project_id="proj_tools", title="纯报告",
            risk_level="low")

        result = await task_submit_report({
            "task_id": "t_rpt2",
            "agent_id": "task_planner",
            "content": "拆解完成",
        })
        assert result.get("error") is None, f"提交失败: {result}"
        assert result["report"]["agent_id"] == "task_planner"
        assert result["comment"] is None


@pytest.mark.asyncio
class TestTaskConvertIdea:
    async def test_convert_idea(self, orch_registered):
        """task_convert_idea idea → discussing（v1.2 立项后进讨论）。"""
        orch = orch_registered
        idea = await orch.store.submit_idea(
            project_id="proj_tools", content="这是一个灵感", source="manual")

        result = await task_convert_idea({
            "idea_id": idea["idea_id"],
            "task_id": "t_conv",
            "title": "转换后的任务",
        })
        assert result.get("error") is None, f"转换失败: {result}"
        assert result["task"]["status"] == "discussing"
        assert result["task"]["title"] == "转换后的任务"
        assert result["task"]["source_idea_id"] == idea["idea_id"]

        # idea 已标记 converted
        updated_idea = await orch.store.get_idea(idea["idea_id"])
        assert updated_idea["status"] == "converted"
        assert updated_idea["converted_task_id"] == "t_conv"

    async def test_convert_idea_not_found(self, orch_registered):
        """task_convert_idea 灵感不存在 → idea_not_found。"""
        result = await task_convert_idea({
            "idea_id": "idea_nonexistent",
            "task_id": "t_conv2",
        })
        assert result["error"] == "idea_not_found"


@pytest.mark.asyncio
class TestTaskAssignAgent:
    async def test_assign_agent(self, orch_registered):
        """task_assign_agent 指派 agent + 风格。"""
        orch = orch_registered
        # 先建 style（tasks.style_id 有 FK → agent_styles.style_id）
        await orch.store.create_style(
            style_id="cautious", name="谨慎",
            description="谨慎型执行风格")
        await orch.store.create_task(
            task_id="t_assign", project_id="proj_tools", title="指派测试",
            risk_level="low")

        result = await task_assign_agent({
            "task_id": "t_assign",
            "if_version": 0,
            "assignee_type": "agent",
            "assignee_id": "coding_agent",
            "assignee_name": "编码 Agent",
            "style_id": "cautious",
        })
        assert result.get("error") is None, f"指派失败: {result}"
        assert result["task"]["assignee_id"] == "coding_agent"
        assert result["task"]["assignee_name"] == "编码 Agent"
        assert result["task"]["style_id"] == "cautious"
        assert result["if_version"] == result["task"]["version"]

        # activity 已记录
        acts = await orch.store.list_activities("t_assign")
        assign_acts = [a for a in acts if "assign" in a["changes"]]
        assert len(assign_acts) == 1

    async def test_assign_version_conflict(self, orch_registered):
        """task_assign_agent 乐观锁冲突。"""
        orch = orch_registered
        await orch.store.create_task(
            task_id="t_assign2", project_id="proj_tools", title="冲突测试",
            risk_level="low")
        # 先用正确 version 改一次 assignee_id（模拟并发，version 已变）
        await orch.store.update_task_fields("t_assign2", 0, assignee_id="agent_a")

        # 用旧 version 0 → 冲突
        result = await task_assign_agent({
            "task_id": "t_assign2",
            "if_version": 0,
            "assignee_id": "agent_x",
        })
        assert result["error"] == "version_conflict"
