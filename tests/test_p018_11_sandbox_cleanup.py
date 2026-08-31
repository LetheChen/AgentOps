"""P0.18.11: sandbox 延迟清理 — Patroller.cleanup_sandboxes_once + API endpoint 单元测试。"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


class _FakeEventStore:
    """最小 EventStore 替身，只实现 sandbox cleanup 三个方法。"""

    def __init__(self, rows):
        self._rows = rows
        self.marked_deleted: list[str] = []
        self.calls: list[str] = []

    async def list_sandboxes_for_cleanup(self, now_iso: str, limit: int = 100):
        self.calls.append(f"list:{now_iso}:{limit}")
        return list(self._rows)

    async def mark_sandbox_deleted(self, run_id: str) -> None:
        self.calls.append(f"mark_deleted:{run_id}")
        self.marked_deleted.append(run_id)


@pytest.fixture
def fake_store():
    return _FakeEventStore([])


@pytest.fixture
def tmp_workspace(tmp_path):
    """临时 workspace 目录，含若干 run 子目录。"""
    runs = ["run_a", "run_b"]
    for r in runs:
        (tmp_path / r).mkdir()
        (tmp_path / r / "data.txt").write_text("payload", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_marks_deleted_for_bind_mount(fake_store):
    """bind_mount 模式：无 sandbox 目录可删，只标 deleted。"""
    from orchestrator.patroller import Patroller

    fake_store._rows = [
        {"run_id": "r1", "workspace_root": "/home/user/project", "workspace_mode": "bind_mount"},
    ]
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()
    assert result == {"scanned": 1, "deleted": 1, "failed": 0}
    assert "r1" in fake_store.marked_deleted


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_marks_deleted_for_isolated(fake_store):
    """isolated 模式：无内容可清，只标 deleted。"""
    from orchestrator.patroller import Patroller

    fake_store._rows = [
        {"run_id": "r2", "workspace_root": "/agentops/workspaces/abc/r2", "workspace_mode": "isolated"},
    ]
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()
    assert result == {"scanned": 1, "deleted": 1, "failed": 0}
    assert "r2" in fake_store.marked_deleted


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_deletes_local_copy_dir(fake_store, tmp_workspace):
    """local_copy 模式：删除物理 sandbox 目录 + 标 deleted。"""
    from orchestrator.patroller import Patroller

    target = tmp_workspace / "run_a"
    assert target.exists()

    fake_store._rows = [
        {"run_id": "run_a", "workspace_root": str(target), "workspace_mode": "local_copy"},
    ]
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()

    assert result["scanned"] == 1
    assert result["deleted"] == 1
    assert result["failed"] == 0
    assert not target.exists(), "sandbox 物理目录必须被删除"
    assert "run_a" in fake_store.marked_deleted


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_handles_git_clone(fake_store, tmp_workspace):
    """git_clone 模式：同 local_copy，删除物理目录。"""
    from orchestrator.patroller import Patroller

    target = tmp_workspace / "run_b"
    fake_store._rows = [
        {"run_id": "run_b", "workspace_root": str(target), "workspace_mode": "git_clone"},
    ]
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()

    assert result == {"scanned": 1, "deleted": 1, "failed": 0}
    assert not target.exists()


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_marks_deleted_when_path_missing(fake_store):
    """物理目录已被外部删除时：仅标 deleted，不报错。"""
    from orchestrator.patroller import Patroller

    fake_store._rows = [
        {
            "run_id": "r3",
            "workspace_root": "/tmp/definitely/not/exist/abc",
            "workspace_mode": "local_copy",
        },
    ]
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()
    assert result == {"scanned": 1, "deleted": 1, "failed": 0}


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_handles_empty_workspace_root(fake_store):
    """无 workspace_root（极端情况）：仅标 deleted，不尝试删文件。"""
    from orchestrator.patroller import Patroller

    fake_store._rows = [
        {"run_id": "r4", "workspace_root": "", "workspace_mode": "local_copy"},
    ]
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()
    assert result == {"scanned": 1, "deleted": 1, "failed": 0}


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_continues_after_one_failure(fake_store, tmp_workspace):
    """一个 sandbox 删失败时，不影响其余 sandbox。"""
    from orchestrator.patroller import Patroller

    target_a = tmp_workspace / "not_a_dir.txt"
    target_a.write_text("x")
    target_b = tmp_workspace / "run_a"

    fake_store._rows = [
        {"run_id": "ra", "workspace_root": str(target_a), "workspace_mode": "local_copy"},
        {"run_id": "rb", "workspace_root": str(target_b), "workspace_mode": "local_copy"},
    ]
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()
    assert result["scanned"] == 2
    assert result["deleted"] >= 1
    assert set(fake_store.marked_deleted) == {"ra", "rb"}


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_returns_empty_on_store_error(fake_store):
    """EventStore 抛错时返回全零，不抛出异常（防御性）。"""
    from orchestrator.patroller import Patroller

    fake_store.list_sandboxes_for_cleanup = AsyncMock(side_effect=RuntimeError("db locked"))
    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()
    assert result == {"scanned": 0, "deleted": 0, "failed": 0}


@pytest.mark.asyncio
async def test_cleanup_sandboxes_once_no_eligible_returns_zeros(fake_store):
    """无待清理 sandbox 时返回全零。"""
    from orchestrator.patroller import Patroller

    p = Patroller(event_store=fake_store)
    result = await p.cleanup_sandboxes_once()
    assert result == {"scanned": 0, "deleted": 0, "failed": 0}


def test_patroller_init_has_sandbox_cleanup_task_attr():
    """Patroller 暴露 _sandbox_cleanup_task 属性（start/stop 钩子需要）。"""
    from orchestrator.patroller import Patroller
    p = Patroller(event_store=_FakeEventStore([]))
    assert p._sandbox_cleanup_task is None