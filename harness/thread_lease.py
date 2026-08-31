"""Thread Lease - 同一 session 的并发控制。

同一 session 同时只能有一个活跃的 turn 或语音连接。
防止用户快速连发两条消息导致 thread 状态混乱。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# 全局 lease 表：session_id -> owner
_active_leases: dict[str, str] = {}
_lock = asyncio.Lock()


@dataclass
class ThreadLease:
    """Thread 租约。release() 后释放 session 的独占权。"""
    owner: str
    session_id: str
    _released: bool = False
    _release_fn: Callable[[], object] | None = None

    def release(self) -> None:
        """释放租约。"""
        if self._released:
            return
        self._released = True
        if self._release_fn:
            self._release_fn()


async def acquire_thread_lease(session_id: str, owner: str) -> ThreadLease | None:
    """获取 thread lease。

    Args:
        session_id: Session ID。
        owner: 租约持有者标识（如 "turn:uuid" / "voice:realtime"）。

    Returns:
        ThreadLease: 获取成功。None: 该 session 已有活跃 lease。
    """
    key = session_id.strip()
    if not key:
        raise ValueError("thread lease 需要 session_id")

    async with _lock:
        if key in _active_leases:
            return None
        _active_leases[key] = owner

    lease = ThreadLease(owner=owner, session_id=key)

    # 包装 release 方法，自动清理全局表
    async def _do_release():
        async with _lock:
            if _active_leases.get(key) == owner:
                del _active_leases[key]

    def _sync_release():
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_do_release(), loop=loop)
            else:
                loop.run_until_complete(_do_release())
        except RuntimeError:
            # 没有 event loop，直接同步清理
            if _active_leases.get(key) == owner:
                _active_leases.pop(key, None)

    lease._release_fn = _sync_release
    logger.info("thread lease 获取 session=%s owner=%s", key, owner)
    return lease


def thread_lease_owner(session_id: str) -> str | None:
    """查询当前持有 lease 的 owner。"""
    return _active_leases.get(session_id.strip())


def _clear_all_leases() -> None:
    """清除所有 lease（测试用）。"""
    _active_leases.clear()
