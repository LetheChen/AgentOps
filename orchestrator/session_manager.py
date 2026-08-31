"""Session 管理器：管理长 Session 与子 Run 的关系。

职责：
1. 创建 / 恢复 Session
2. 维护 attached_run_ids（滚动窗口，上限 32）
3. Session 状态管理（active / dormant / archived）
4. 空闲超时转 dormant

设计理念：
- Session 是用户与 Manager Agent 的长对话，不主动拆分
- Run 是一次子任务执行，挂载到 Session 下
- 一个 Session 可关联多个 Run（上限 32），超过丢弃最老的
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from audit.store import EventStore

logger = logging.getLogger(__name__)

MAX_ATTACHED_RUNS = 32


@dataclass
class SessionState:
    """Session 运行时状态（内存缓存）。"""
    session_id: str
    user_id: str = ""
    agent_id: str = ""
    status: str = "active"  # active / dormant / archived
    attached_run_ids: list[str] = field(default_factory=list)
    message_count: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionManager:
    """Session 管理器单例。

    由 LocalSdkOrchestrator 持有，ConversationalEngine 通过它获取/创建 Session。
    trigger_workflow 工具通过 _registry.get_session_manager() 访问。
    """

    def __init__(self, event_store: "EventStore"):
        self._store = event_store
        self._active_sessions: dict[str, SessionState] = {}  # 内存缓存

    async def get_or_create_session(
        self,
        session_id: str,
        agent_id: str,
        user_id: str = "",
    ) -> SessionState:
        """获取或创建 Session。

        优先从内存缓存取，其次从数据库恢复，最后新建。
        """
        # 1. 内存缓存命中
        if session_id in self._active_sessions:
            return self._active_sessions[session_id]

        # 2. 从数据库恢复
        db_session = await self._store.get_session(session_id)
        if db_session:
            # v3: list_child_runs_of_session → JOIN parent_child_runs + runs
            attached = await self._store.list_child_runs_of_session(session_id)
            state = SessionState(
                session_id=db_session["session_id"],
                user_id=db_session.get("user_id", ""),
                agent_id=db_session["agent_id"],
                status=db_session["status"],
                # v3: 字段名 child_run_id
                attached_run_ids=[r["run_id"] for r in attached],
                message_count=db_session.get("message_count", 0),
                started_at=datetime.fromisoformat(db_session["started_at"]) if db_session.get("started_at") else datetime.now(timezone.utc),
                last_activity_at=datetime.fromisoformat(db_session["last_activity_at"]) if db_session.get("last_activity_at") else datetime.now(timezone.utc),
            )
        else:
            # 3. 新建
            state = SessionState(
                session_id=session_id,
                agent_id=agent_id,
                user_id=user_id,
            )
            await self._store.create_session(
                session_id=session_id,
                agent_id=agent_id,
                user_id=user_id,
            )
            logger.info("Session 创建: %s (agent=%s)", session_id, agent_id)

        self._active_sessions[session_id] = state
        return state

    async def attach_run(
        self,
        session_id: str,
        run_id: str,
        workflow_id: str = "",
    ) -> None:
        """将子 run 挂载到父 Session。超过上限时丢弃最老的。

        v3 改造：run_id 是子 run_id（trigger_workflow 派发的子任务）。
        parent_child_runs 表已由 trigger_workflow.record_parent_child_run 落库，
        此处只更新内存缓存 + 递增 attached_run_count。
        """
        state = await self.get_or_create_session(session_id, "")
        if run_id not in state.attached_run_ids:
            state.attached_run_ids.append(run_id)
            if len(state.attached_run_ids) > MAX_ATTACHED_RUNS:
                state.attached_run_ids = state.attached_run_ids[-MAX_ATTACHED_RUNS:]
            # v3: 递增 sessions.attached_run_count
            try:
                await self._store.increment_attached_run_count(session_id)
            except Exception:
                logger.warning("increment_attached_run_count 失败: session=%s", session_id[:12])
        state.last_activity_at = datetime.now(timezone.utc)

        # parent_child_runs 表已有记录（trigger_workflow 落库），这里只更新内存缓存
        await self._store.touch_session(session_id)

    async def touch(self, session_id: str) -> None:
        """更新 Session 最后活动时间。"""
        state = self._active_sessions.get(session_id)
        if state:
            state.last_activity_at = datetime.now(timezone.utc)
        await self._store.touch_session(session_id)

    async def list_attached_runs(self, session_id: str) -> list[dict[str, Any]]:
        """列出 Session 关联的所有子 run（v3: 走 list_child_runs_of_session）。"""
        return await self._store.list_child_runs_of_session(session_id)

    def get_cached_state(self, session_id: str) -> SessionState | None:
        """从内存缓存获取 Session 状态（不查库）。"""
        return self._active_sessions.get(session_id)
