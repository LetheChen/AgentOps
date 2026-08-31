"""任务状态机单测（P0 5 态转移表 + requires_user）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.9.2
覆盖：
- P0 5 态正向转移（idea→backlog→discussing→reviewing→closed）
- requires_user 字段（reviewing→closed 是用户决策）
- 退回路径（reviewing→backlog/reviewing→discussing）
- 非法转移拒绝
- 终态判定
"""
from __future__ import annotations

import pytest

from task.status import (
    TaskStatus,
    P0_TRANSITIONS,
    can_transition_p0,
    get_p0_allowed_transitions,
    is_p0_terminal,
)


class TestP0Transitions:
    """P0 5 态转移表测试。"""

    def test_p0_has_9_transitions(self):
        """P0 转移表共 9 条（4 正向 + 1 closed + 2 退回 + 3 废弃）。"""
        assert len(P0_TRANSITIONS) == 9

    def test_forward_path(self):
        """正向路径：idea→backlog→discussing→reviewing→closed。"""
        assert can_transition_p0("idea", "backlog")
        assert can_transition_p0("backlog", "discussing")
        assert can_transition_p0("discussing", "reviewing")
        assert can_transition_p0("reviewing", "closed")

    def test_reviewing_to_closed_requires_user(self):
        """reviewing→closed 必须是用户决策（requires_user=True）。"""
        transitions = get_p0_allowed_transitions("reviewing")
        closed_trans = [t for t in transitions if t[0] == TaskStatus.CLOSED]
        assert len(closed_trans) == 1
        assert closed_trans[0][2] is True  # requires_user

    def test_forward_auto_transitions_not_require_user(self):
        """前 4 步正向推进是自动的（requires_user=False）。"""
        assert can_transition_p0("idea", "backlog")
        trans = get_p0_allowed_transitions("idea")
        backlog_trans = [t for t in trans if t[0] == TaskStatus.BACKLOG]
        assert backlog_trans[0][2] is False  # 自动推进

    def test_rollback_paths(self):
        """退回路径：reviewing→backlog / reviewing→discussing。"""
        assert can_transition_p0("reviewing", "backlog")
        assert can_transition_p0("reviewing", "discussing")

    def test_rollback_requires_user(self):
        """退回是用户决策（requires_user=True）。"""
        transitions = get_p0_allowed_transitions("reviewing")
        rollback_trans = [t for t in transitions if t[0] in (TaskStatus.BACKLOG, TaskStatus.DISCUSSING)]
        assert len(rollback_trans) == 2
        for _, _, ru in rollback_trans:
            assert ru is True

    def test_abandon_paths(self):
        """废弃路径：idea/backlog/discussing→closed。"""
        assert can_transition_p0("idea", "closed")
        assert can_transition_p0("backlog", "closed")
        assert can_transition_p0("discussing", "closed")

    def test_illegal_transitions_rejected(self):
        """非法转移被拒绝。"""
        # 跳跃：idea→discussing（非法，必须经 backlog）
        assert not can_transition_p0("idea", "discussing")
        # 反向：backlog→idea（非法）
        assert not can_transition_p0("backlog", "idea")
        # closed 是终态，不可再转移
        assert not can_transition_p0("closed", "idea")
        # reviewing→in_progress（V1 态，P0 不允许）
        assert not can_transition_p0("reviewing", "in_progress")

    def test_closed_is_terminal(self):
        """closed 是终态。"""
        assert is_p0_terminal("closed")
        assert is_p0_terminal(TaskStatus.CLOSED)

    def test_non_terminal_states(self):
        """非终态判定。"""
        assert not is_p0_terminal("idea")
        assert not is_p0_terminal("backlog")
        assert not is_p0_terminal("discussing")
        assert not is_p0_terminal("reviewing")

    def test_get_transitions_returns_tuples(self):
        """get_p0_allowed_transitions 返回三元组 (to, action, requires_user)。"""
        trans = get_p0_allowed_transitions("idea")
        assert len(trans) >= 1
        for t in trans:
            assert len(t) == 3
            assert isinstance(t[0], TaskStatus)
            assert isinstance(t[1], str)
            assert isinstance(t[2], bool)

    def test_reviewing_has_3_transitions(self):
        """reviewing 有 3 个合法转移：closed/backlog/discussing。"""
        trans = get_p0_allowed_transitions("reviewing")
        assert len(trans) == 3
        targets = {t[0] for t in trans}
        assert targets == {TaskStatus.CLOSED, TaskStatus.BACKLOG, TaskStatus.DISCUSSING}

    def test_closed_has_no_transitions(self):
        """closed 是终态，无合法转移。"""
        trans = get_p0_allowed_transitions("closed")
        assert len(trans) == 0
