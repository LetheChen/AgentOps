"""任务状态机（P0 5 态 + V1 14 态扩展）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §3.1 + §4.9.2
- P0：idea → backlog → discussing → reviewing → closed（5 态）
- requires_user 字段区分自动推进与用户决策（评审吸收点 ①）
- 纯函数，无副作用，便于单测
"""
from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class TaskStatus(str, Enum):
    """任务状态枚举（P0 5 态 + V1 9 态，共 14 态）。"""
    # P0 5 态
    IDEA = "idea"
    BACKLOG = "backlog"
    DISCUSSING = "discussing"
    REVIEWING = "reviewing"
    CLOSED = "closed"
    # V1 9 态（P0 不用）
    DECOMPOSING = "decomposing"
    IN_PROGRESS = "in_progress"
    VALIDATING = "validating"
    CLOSING = "closing"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"
    ABANDONED = "abandoned"


class Transition(NamedTuple):
    """状态转移定义。

    Attributes:
        from_status: 起始状态
        to_status: 目标状态
        action: 转移动作描述
        requires_user: 是否需要用户决策（True=用户审批，False=自动推进）
    """
    from_status: TaskStatus
    to_status: TaskStatus
    action: str
    requires_user: bool = False


# P0 5 态转移表（显式定义，不从 14 态过滤，确保 reviewing→closed 直达）
P0_TRANSITIONS: list[Transition] = [
    # 正向推进（前 4 步自动）
    Transition(TaskStatus.IDEA,       TaskStatus.BACKLOG,     "立项",            False),
    Transition(TaskStatus.BACKLOG,    TaskStatus.DISCUSSING,  "触发方案讨论",    False),
    Transition(TaskStatus.DISCUSSING, TaskStatus.REVIEWING,   "提交方案评审",    False),
    # reviewing 是用户决策态：closed 必须用户审批
    Transition(TaskStatus.REVIEWING,  TaskStatus.CLOSED,      "评审通过，关闭任务", True),
    # 退回（用户决策）
    Transition(TaskStatus.REVIEWING,  TaskStatus.BACKLOG,     "退回立项",        True),
    Transition(TaskStatus.REVIEWING,  TaskStatus.DISCUSSING,  "退回讨论",        True),
    # 废弃/取消（用户决策）
    Transition(TaskStatus.IDEA,       TaskStatus.CLOSED,      "废弃灵感",        True),
    Transition(TaskStatus.BACKLOG,    TaskStatus.CLOSED,      "取消待办",        True),
    Transition(TaskStatus.DISCUSSING, TaskStatus.CLOSED,      "取消讨论",        True),
]

# P0 终态集合（不可再转移）
P0_TERMINAL: frozenset[TaskStatus] = frozenset({TaskStatus.CLOSED})

# P0 可暂停集合（P0 无 paused 态，全不可暂停）
P0_PAUSABLE: frozenset[TaskStatus] = frozenset()


def can_transition_p0(from_status: TaskStatus | str, to_status: TaskStatus | str) -> bool:
    """检查 P0 状态转移是否合法。"""
    f = TaskStatus(from_status) if isinstance(from_status, str) else from_status
    t = TaskStatus(to_status) if isinstance(to_status, str) else to_status
    return any(tr.from_status == f and tr.to_status == t for tr in P0_TRANSITIONS)


def get_p0_allowed_transitions(from_status: TaskStatus | str) -> list[tuple[TaskStatus, str, bool]]:
    """返回 P0 某状态的合法转移目标列表。

    Returns:
        [(to_status, action, requires_user), ...]
    """
    f = TaskStatus(from_status) if isinstance(from_status, str) else from_status
    return [(tr.to_status, tr.action, tr.requires_user) for tr in P0_TRANSITIONS
            if tr.from_status == f]


def is_p0_terminal(status: TaskStatus | str) -> bool:
    """判断是否终态（不可再转移）。"""
    s = TaskStatus(status) if isinstance(status, str) else status
    return s in P0_TERMINAL


# ============================================================
# V1 完整状态机：14 态 + 45 条转移（设计文档 §3.1，v2.2 补 abandoned 入边 + closing→canceled）
# ============================================================

# V1 完整合法转移表（v1.2 主线重排，DESIGN_task_lifecycle_automation_v1.md §3.1）
# v1.2 变更（backlog 语义重构为「可执行任务池」）：
#   旧主线：idea→backlog→discussing→decomposing→in_progress（reviewing 是高风险岔路）
#   新主线：idea→discussing→decomposing→reviewing→backlog→in_progress
#   - 立项门禁 idea→discussing（用户审核开题，agent 不可代办）
#   - reviewing 变主线必经：评审拆分方案，执行前最后一道门
#   - backlog = 评审通过的可执行任务池（dispatcher 从此派发）
#   - 父任务：评审通过随子任务进 backlog 等待，子任务全终态后 backlog→validating 收尾
TRANSITIONS: list[Transition] = [
    # 正向主线（v1.2 五段：立项→讨论→拆解→评审→待办→执行）
    # 立项门禁：灵感必须用户确认才投入讨论资源（原 idea→backlog 门禁挪位）
    Transition(TaskStatus.IDEA,        TaskStatus.DISCUSSING,   "用户审核立项",    True),
    Transition(TaskStatus.DISCUSSING,  TaskStatus.DISCUSSING,   "多轮讨论",        False),
    Transition(TaskStatus.DISCUSSING,  TaskStatus.DECOMPOSING,  "方案讨论完成，自动拆解", False),
    Transition(TaskStatus.DECOMPOSING, TaskStatus.REVIEWING,    "拆分完成，提交评审", False),
    # 评审放行：low/medium 自动入待办池；high 由 advance_stage 运行时门禁强制人审
    Transition(TaskStatus.REVIEWING,   TaskStatus.BACKLOG,      "评审通过，入待办池", False),
    Transition(TaskStatus.BACKLOG,     TaskStatus.IN_PROGRESS,  "调度 agent 执行", False),
    Transition(TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS,  "并行子任务调度",  False),
    # 父任务收尾：backlog 态父任务等全部子任务终态后自动进验收
    Transition(TaskStatus.BACKLOG,     TaskStatus.VALIDATING,   "子任务全部完成，父任务收尾", False),
    Transition(TaskStatus.IN_PROGRESS, TaskStatus.VALIDATING,   "agent 执行完成",  False),
    Transition(TaskStatus.VALIDATING,  TaskStatus.CLOSING,      "验收通过",        True),
    Transition(TaskStatus.CLOSING,     TaskStatus.CLOSED,       "关闭（硬约束满足）", False),
    # 评审打回（v1.2：评审不通过退回讨论/重拆，用户决策）
    Transition(TaskStatus.REVIEWING,   TaskStatus.DISCUSSING,   "评审打回：方案有问题", True),
    Transition(TaskStatus.REVIEWING,   TaskStatus.DECOMPOSING,  "评审打回：拆分不当", True),
    # 验收回退（三级，用户决策）
    Transition(TaskStatus.VALIDATING,  TaskStatus.IN_PROGRESS,  "local 执行偏差",  True),
    Transition(TaskStatus.VALIDATING,  TaskStatus.DECOMPOSING,  "partial 拆解错",  True),
    Transition(TaskStatus.VALIDATING,  TaskStatus.DISCUSSING,   "global 方案错",   True),
    # v1.1 任意阶段回退边（生命周期自动化方案 §4.2：验收效果有问题可退回前面任意阶段，
    # 回退必须携带原因，注入下一轮 prompt 形成带记忆的迭代循环）
    Transition(TaskStatus.CLOSING,     TaskStatus.VALIDATING,   "关闭前退回重验",  True),
    Transition(TaskStatus.CLOSING,     TaskStatus.IN_PROGRESS,  "关闭前退回执行",  True),
    Transition(TaskStatus.CLOSING,     TaskStatus.DECOMPOSING,  "关闭前退回分解",  True),
    Transition(TaskStatus.CLOSING,     TaskStatus.DISCUSSING,   "关闭前退回方案",  True),
    # 待办池退回（v1.2：backlog 任务发现问题时退回讨论/灵感，用户决策）
    Transition(TaskStatus.BACKLOG,     TaskStatus.DISCUSSING,   "退回讨论",        True),
    Transition(TaskStatus.BACKLOG,     TaskStatus.IDEA,         "退回灵感重新讨论", True),
    # 外部阻塞
    Transition(TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED,      "外部阻塞",        False),
    Transition(TaskStatus.VALIDATING,  TaskStatus.BLOCKED,      "外部阻塞",        False),
    Transition(TaskStatus.BLOCKED,     TaskStatus.IN_PROGRESS,  "阻塞解除恢复",    False),
    # 可恢复暂停（仅 in_progress/validating 可暂停）
    Transition(TaskStatus.IN_PROGRESS, TaskStatus.PAUSED,       "用户暂停",        True),
    Transition(TaskStatus.VALIDATING,  TaskStatus.PAUSED,       "用户暂停",        True),
    Transition(TaskStatus.PAUSED,      TaskStatus.IN_PROGRESS,  "用户恢复",        True),
    # 异常态
    Transition(TaskStatus.IN_PROGRESS, TaskStatus.FAILED,       "agent 崩溃/超时", False),
    Transition(TaskStatus.FAILED,      TaskStatus.IN_PROGRESS,  "修复后重试",      False),
    # 强制终态（用户主动，任意活跃态均可）
    Transition(TaskStatus.IDEA,        TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.BACKLOG,     TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.DISCUSSING,  TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.REVIEWING,   TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.DECOMPOSING, TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.IN_PROGRESS, TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.VALIDATING,  TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.CLOSING,     TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.PAUSED,      TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.BLOCKED,     TaskStatus.CANCELED,     "用户取消",        True),
    Transition(TaskStatus.FAILED,      TaskStatus.CANCELED,     "用户取消",        True),

    # 自动归档（v2.2：任意活跃态 30 天无活动 → abandoned，由 task_conductor 定时执行）
    Transition(TaskStatus.IDEA,        TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.BACKLOG,     TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.DISCUSSING,  TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.REVIEWING,   TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.DECOMPOSING, TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.IN_PROGRESS, TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.VALIDATING,  TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.CLOSING,     TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.PAUSED,      TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.BLOCKED,     TaskStatus.ABANDONED,    "30天无活动自动归档", False),
    Transition(TaskStatus.FAILED,      TaskStatus.ABANDONED,    "30天无活动自动归档", False),
]

# V1 终态集合（不可再转移）
TERMINAL: frozenset[TaskStatus] = frozenset({TaskStatus.CLOSED, TaskStatus.CANCELED, TaskStatus.ABANDONED})

# V1 可暂停态
PAUSABLE: frozenset[TaskStatus] = frozenset({TaskStatus.IN_PROGRESS, TaskStatus.VALIDATING})


def can_transition(from_status: TaskStatus | str, to_status: TaskStatus | str) -> bool:
    """检查 V1 状态转移是否合法。无效状态返回 False。"""
    try:
        f = TaskStatus(from_status) if isinstance(from_status, str) else from_status
        t = TaskStatus(to_status) if isinstance(to_status, str) else to_status
    except ValueError:
        return False
    if f in TERMINAL:
        return False
    return any(tr.from_status == f and tr.to_status == t for tr in TRANSITIONS)


def get_allowed_transitions(from_status: TaskStatus | str) -> list[tuple[str, str, bool]]:
    """返回 V1 某状态的合法转移目标列表。

    Returns:
        [(to_status_value, action, requires_user), ...]  供前端渲染可选操作。
    """
    try:
        f = TaskStatus(from_status) if isinstance(from_status, str) else from_status
    except ValueError:
        return []
    if f in TERMINAL:
        return []
    return [(tr.to_status.value, tr.action, tr.requires_user)
            for tr in TRANSITIONS if tr.from_status == f]


def is_terminal(status: TaskStatus | str) -> bool:
    """判断是否 V1 终态。"""
    s = TaskStatus(status) if isinstance(status, str) else status
    return s in TERMINAL


def resolve_review_gate(risk_level: str) -> str:
    """风险分级 → 评审门槛（设计文档 §3.2）。

    low    → auto（直接标记 approved 进 Decomposing）
    medium → notify（通知用户，可放行）
    high   → manual（强制人审）
    """
    return {"low": "auto", "medium": "notify", "high": "manual"}.get(risk_level, "notify")


def resolve_decompose_strategy(complexity: str) -> str:
    """复杂度 → 拆解策略（设计文档 §3.3）。

    simple  → none（不拆，直接 in_progress）
    medium  → single（拆 1 层）
    complex → recursive（递归拆，多层级）
    """
    return {"simple": "none", "medium": "single", "complex": "recursive"}.get(complexity, "single")
