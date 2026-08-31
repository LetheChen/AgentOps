"""V1 状态机 14 态单测（设计文档 §3.1 + V1 验收点②）。

覆盖：
- 14 态枚举完整性
- 31 条转移表合法性
- can_transition / get_allowed_transitions / is_terminal
- 终态不可转移
- 风险门槛 resolve_review_gate
- 拆解策略 resolve_decompose_strategy
- P0 函数未被破坏（向后兼容）
"""
import pytest
from task.status import (
    TaskStatus, Transition, TRANSITIONS, TERMINAL, PAUSABLE,
    can_transition, get_allowed_transitions, is_terminal,
    resolve_review_gate, resolve_decompose_strategy,
    P0_TRANSITIONS, can_transition_p0, get_p0_allowed_transitions, is_p0_terminal,
)


class TestV1StatusEnum:
    """14 态枚举完整性。"""

    def test_14_states_present(self):
        expected = {
            "idea", "backlog", "discussing", "reviewing", "closed",
            "decomposing", "in_progress", "validating", "closing",
            "paused", "blocked", "failed", "canceled", "abandoned",
        }
        actual = {s.value for s in TaskStatus}
        assert actual == expected, f"枚举不匹配: 缺 {expected - actual}, 多 {actual - expected}"

    def test_no_claimed(self):
        """CLAIMED 不在设计文档 14 态中，应已移除。"""
        assert not hasattr(TaskStatus, "CLAIMED")


class TestV1Transitions:
    """转移表合法性（v1.2 主线重排：backlog=可执行任务池）。"""

    def test_transition_count(self):
        # v1.2 重排后：主线 12 + 评审打回 2 + 验收回退 3 + 任意回退 4 + backlog 退回 2
        # + 阻塞 3 + 暂停 3 + 异常 2 + 强制终态 10 + 自动归档 11 = 52
        assert len(TRANSITIONS) == 52, f"转移数应为 52，实际 {len(TRANSITIONS)}"

    def test_v12_mainline_order(self):
        """v1.2 主线：idea→discussing→decomposing→reviewing→backlog→in_progress→validating→closing→closed。"""
        mainline = [("idea", "discussing"), ("discussing", "decomposing"),
                    ("decomposing", "reviewing"), ("reviewing", "backlog"),
                    ("backlog", "in_progress"), ("in_progress", "validating"),
                    ("validating", "closing"), ("closing", "closed")]
        for f, t in mainline:
            assert can_transition(f, t), f"主线边 {f}→{t} 应合法"

    def test_v11_gate_and_rollback_edges(self):
        """v1.2：idea→discussing 硬性人工门禁（原 idea→backlog 挪位）+ 任意阶段回退边。"""
        tr = next(t for t in TRANSITIONS
                  if t.from_status.value == "idea" and t.to_status.value == "discussing")
        assert tr.requires_user is True, "idea→discussing 必须用户审核立项"
        for f, t in [("closing", "validating"), ("closing", "in_progress"),
                     ("closing", "decomposing"), ("closing", "discussing"),
                     ("backlog", "idea"), ("backlog", "discussing")]:
            assert can_transition(f, t), f"回退边 {f}→{t} 应合法"

    def test_v12_removed_old_edges(self):
        """v1.2 删除的旧边：旧主线 idea→backlog、decomposing→in_progress、discussing→reviewing 岔路。"""
        for f, t in [("idea", "backlog"), ("decomposing", "in_progress"),
                     ("discussing", "reviewing")]:
            assert not can_transition(f, t), f"旧边 {f}→{t} 应已删除"

    def test_v12_review_gate_edges(self):
        """v1.2 评审节点：通过入待办池 + 打回讨论/重拆。"""
        assert can_transition("reviewing", "backlog")
        assert can_transition("reviewing", "discussing")
        assert can_transition("reviewing", "decomposing")

    def test_v12_parent_finalize_edge(self):
        """v1.2 父任务收尾：backlog→validating（子任务全终态后自动进验收）。"""
        assert can_transition("backlog", "validating")

    def test_no_self_to_terminal(self):
        """终态不应有出发转移。"""
        for tr in TRANSITIONS:
            assert tr.from_status not in TERMINAL, f"终态 {tr.from_status} 不应有出发转移"

    def test_review_gate_branch(self):
        """v1.2：reviewing 是主线必经（不再是 discussing 时期高风险岔路）。"""
        assert can_transition("discussing", "decomposing")
        assert not can_transition("discussing", "reviewing")

    def test_validation_rollback_three_levels(self):
        """验收回退三级。"""
        assert can_transition("validating", "in_progress")
        assert can_transition("validating", "decomposing")
        assert can_transition("validating", "discussing")

    def test_blocked_recoverable(self):
        """阻塞可恢复。"""
        assert can_transition("in_progress", "blocked")
        assert can_transition("validating", "blocked")
        assert can_transition("blocked", "in_progress")

    def test_pausable_states(self):
        """仅 in_progress/validating 可暂停。"""
        assert can_transition("in_progress", "paused")
        assert can_transition("validating", "paused")
        assert can_transition("paused", "in_progress")
        assert PAUSABLE == frozenset({TaskStatus.IN_PROGRESS, TaskStatus.VALIDATING})

    def test_failed_retryable(self):
        """失败可重试。"""
        assert can_transition("in_progress", "failed")
        assert can_transition("failed", "in_progress")

    def test_cancel_from_any_active(self):
        """任意活跃态可取消。"""
        active = {TaskStatus.IDEA, TaskStatus.BACKLOG, TaskStatus.DISCUSSING,
                  TaskStatus.REVIEWING, TaskStatus.DECOMPOSING, TaskStatus.IN_PROGRESS,
                  TaskStatus.VALIDATING, TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.FAILED}
        for s in active:
            assert can_transition(s, TaskStatus.CANCELED), f"{s} 应能取消"

    def test_v22_auto_archive_from_any_active(self):
        """v2.2：任意活跃态（含 closing）可自动归档（requires_user=False）。"""
        active = [TaskStatus.IDEA, TaskStatus.BACKLOG, TaskStatus.DISCUSSING,
                  TaskStatus.REVIEWING, TaskStatus.DECOMPOSING, TaskStatus.IN_PROGRESS,
                  TaskStatus.VALIDATING, TaskStatus.CLOSING, TaskStatus.PAUSED,
                  TaskStatus.BLOCKED, TaskStatus.FAILED]
        for s in active:
            assert can_transition(s.value, TaskStatus.ABANDONED.value), \
                f"{s} 应能自动归档"
        # requires_user=False（agent 可执行，task_conductor 自动归档依赖此）
        for tr in TRANSITIONS:
            if (tr.from_status == TaskStatus.IN_PROGRESS
                    and tr.to_status == TaskStatus.ABANDONED):
                assert tr.requires_user is False

    def test_v22_closing_canceled(self):
        """v2.2：closing→canceled 补齐（用户取消关闭中的任务）。"""
        assert can_transition("closing", "canceled")


class TestV1Terminal:
    """终态校验。"""

    @pytest.mark.parametrize("status", ["closed", "canceled", "abandoned"])
    def test_is_terminal_true(self, status):
        assert is_terminal(status)

    @pytest.mark.parametrize("status", ["idea", "backlog", "discussing", "reviewing",
                                         "decomposing", "in_progress", "validating", "closing",
                                         "paused", "blocked", "failed"])
    def test_is_terminal_false(self, status):
        assert not is_terminal(status)

    def test_terminal_no_outgoing(self):
        """终态 get_allowed_transitions 返回空。"""
        for s in ["closed", "canceled", "abandoned"]:
            assert get_allowed_transitions(s) == []

    def test_terminal_cannot_transition(self):
        """终态不可转移到任何状态。"""
        for s in ["closed", "canceled", "abandoned"]:
            for d in ["idea", "backlog", "in_progress", "closed"]:
                assert not can_transition(s, d)


class TestV1GetAllowed:
    """get_allowed_transitions 返回三元组。"""

    def test_returns_tuples_with_requires_user(self):
        result = get_allowed_transitions("reviewing")
        assert len(result) > 0
        for item in result:
            assert len(item) == 3
            to_status, action, requires_user = item
            assert isinstance(to_status, str)
            assert isinstance(action, str)
            assert isinstance(requires_user, bool)

    def test_reviewing_includes_decompose_and_rollback(self):
        result = get_allowed_transitions("reviewing")
        targets = [t[0] for t in result]
        assert "decomposing" in targets
        assert "backlog" in targets
        assert "canceled" in targets

    def test_invalid_status_returns_empty(self):
        assert get_allowed_transitions("nonexistent") == []
        assert can_transition("nonexistent", "backlog") is False


class TestRiskGate:
    """风险分级 → 评审门槛（§3.2）。"""

    def test_low_auto(self):
        assert resolve_review_gate("low") == "auto"

    def test_medium_notify(self):
        assert resolve_review_gate("medium") == "notify"

    def test_high_manual(self):
        assert resolve_review_gate("high") == "manual"

    def test_unknown_defaults_notify(self):
        assert resolve_review_gate("unknown") == "notify"


class TestDecomposeStrategy:
    """复杂度 → 拆解策略（§3.3）。"""

    @pytest.mark.parametrize("complexity,expected", [
        ("simple", "none"),
        ("medium", "single"),
        ("complex", "recursive"),
    ])
    def test_strategy_mapping(self, complexity, expected):
        assert resolve_decompose_strategy(complexity) == expected

    def test_unknown_defaults_single(self):
        assert resolve_decompose_strategy("unknown") == "single"


class TestP0BackwardCompat:
    """P0 函数向后兼容（V1 扩展未破坏 P0）。"""

    def test_p0_transitions_intact(self):
        assert len(P0_TRANSITIONS) == 9

    def test_p0_can_transition(self):
        assert can_transition_p0("idea", "backlog")
        assert can_transition_p0("reviewing", "closed")
        # idea→closed 在 P0 合法（废弃灵感，用户决策）
        assert can_transition_p0("idea", "closed")

    def test_p0_terminal(self):
        assert is_p0_terminal("closed")
        assert not is_p0_terminal("idea")

    def test_p0_allowed_returns_tuples(self):
        result = get_p0_allowed_transitions("reviewing")
        # P0 reviewing: closed, backlog, discussing（3 条，P0 无 canceled）
        assert len(result) == 3
