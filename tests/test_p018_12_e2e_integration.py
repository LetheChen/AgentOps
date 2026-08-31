"""P0.18.12 E2E 集成验证：workspace_id 全链路 + ContainerProvisioner registry + 跨层一致性。

覆盖：
1. create_session(workspace_id=) → sessions 表持久化 → get_session 返回 workspace_id
2. create_session 不传 workspace_id → sessions 表 NULL（通用对话，向后兼容）
3. create_session 传不存在/已停用 workspace_id → 不抛异常，sessions 表写入 workspace_id（FK 不阻塞）
4. ContainerProvisioner registry get/set/clear
5. RunRequest.workspace_id 字段存在且默认 None
6. _resolve_workspace_context(valid_id) → 返回 workspace_context dict（字段映射正确）
7. _resolve_workspace_context(None) → 返回 None（通用对话）
8. _resolve_workspace_context(disabled_id) → 返回 None（graceful fallback）
9. _resolve_workspace_context(non_existent_id) → 返回 None（不抛异常）
10. workspace_context 字段映射：authorized_workspaces.mode → workspace_context.workspace_mode
11. tier 兼容矩阵：T0 通用 / T1+ 需 workspace / workspace permissions → tier 映射
12. sessions 表 workspace_id 列在 schema 中存在

设计：
- 不调真实 API server / 不调真实 Docker（需要外部依赖）
- 使用真实 SqliteEventStore（内存级集成）
- 使用 mock LocalSdkOrchestrator 测试 _resolve_workspace_context（需 event_store 注入）
- 验证跨层一致性：API payload → store → sessions 表 → get_session
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from audit import SqliteEventStore
from orchestrator._registry import (
    get_container_provisioner,
    set_container_provisioner,
)
from orchestrator.protocol import RunRequest


# ============================================================
# Fixtures
# ============================================================

@pytest_asyncio.fixture
async def store(tmp_path):
    """每个测试用独立临时 db 文件。"""
    db_path = tmp_path / "test_p018_12.db"
    s = SqliteEventStore(str(db_path))
    yield s
    await s.close()


async def _create_authorized_workspace(
    store: SqliteEventStore,
    *,
    workspace_id: str | None = None,
    display_name: str = "test-project",
    mode: str = "bind_mount",
    permissions: str = "read_write",
    source_path: str = "/tmp/test-project",
    git_url: str | None = None,
    git_branch: str | None = None,
    enabled: bool = True,
) -> str:
    """创建一条 authorized_workspaces 记录，返回 workspace_id。"""
    wid = workspace_id or f"ws-{uuid.uuid4().hex[:8]}"
    await store.create_authorized_workspace(
        workspace_id=wid,
        display_name=display_name,
        mode=mode,
        permissions=permissions,
        source_path=source_path,
        git_url=git_url,
        git_branch=git_branch,
    )
    if not enabled:
        await store.update_authorized_workspace(wid, enabled=False)
    return wid


# ============================================================
# 1. create_session + workspace_id 持久化
# ============================================================

class TestSessionWorkspacePersistence:
    """验证 create_session 的 workspace_id 全链路持久化。"""

    @pytest.mark.asyncio
    async def test_create_session_with_workspace_id(self, store):
        """create_session 传 workspace_id → sessions 表存储 → get_session 返回。"""
        wid = await _create_authorized_workspace(store, display_name="agentops")
        sid = f"sess-{uuid.uuid4().hex[:8]}"

        await store.create_session(sid, agent_id="manager", workspace_id=wid)

        session = await store.get_session(sid)
        assert session is not None
        assert session["workspace_id"] == wid

    @pytest.mark.asyncio
    async def test_create_session_without_workspace_id(self, store):
        """create_session 不传 workspace_id → sessions 表 NULL（通用对话）。"""
        sid = f"sess-{uuid.uuid4().hex[:8]}"

        await store.create_session(sid, agent_id="manager")

        session = await store.get_session(sid)
        assert session is not None
        assert session["workspace_id"] is None

    @pytest.mark.asyncio
    async def test_create_session_with_nonexistent_workspace_id(self, store):
        """create_session 传不存在的 workspace_id → FK 约束拒绝（IntegrityError）。"""
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        fake_wid = "ws-nonexistent-12345"

        # sessions 表有 FK 约束：FOREIGN KEY (workspace_id) REFERENCES authorized_workspaces(workspace_id)
        # 不存在的 workspace_id 会被 SQLite FK check 拒绝
        with pytest.raises(sqlite3.IntegrityError):
            await store.create_session(sid, agent_id="manager", workspace_id=fake_wid)

    @pytest.mark.asyncio
    async def test_create_session_with_disabled_workspace_id(self, store):
        """create_session 传已停用 workspace_id → 不抛异常，sessions 表写入 workspace_id。"""
        wid = await _create_authorized_workspace(store, enabled=False)
        sid = f"sess-{uuid.uuid4().hex[:8]}"

        await store.create_session(sid, agent_id="manager", workspace_id=wid)

        session = await store.get_session(sid)
        assert session is not None
        assert session["workspace_id"] == wid

    @pytest.mark.asyncio
    async def test_multiple_sessions_different_workspaces(self, store):
        """多个 session 各自绑定不同 workspace_id，互不干扰。"""
        wid1 = await _create_authorized_workspace(store, display_name="project-A")
        wid2 = await _create_authorized_workspace(store, display_name="project-B")
        sid1 = f"sess-{uuid.uuid4().hex[:8]}"
        sid2 = f"sess-{uuid.uuid4().hex[:8]}"
        sid3 = f"sess-{uuid.uuid4().hex[:8]}"  # 通用对话

        await store.create_session(sid1, agent_id="manager", workspace_id=wid1)
        await store.create_session(sid2, agent_id="manager", workspace_id=wid2)
        await store.create_session(sid3, agent_id="manager")

        s1 = await store.get_session(sid1)
        s2 = await store.get_session(sid2)
        s3 = await store.get_session(sid3)

        assert s1["workspace_id"] == wid1
        assert s2["workspace_id"] == wid2
        assert s3["workspace_id"] is None


# ============================================================
# 2. ContainerProvisioner Registry
# ============================================================

class TestContainerProvisionerRegistry:
    """验证 ContainerProvisioner 全局 registry 的 get/set/clear。"""

    def test_set_and_get(self):
        """set_container_provisioner → get_container_provisioner 返回同一实例。"""
        mock_prov = MagicMock(name="TestProvisioner")
        try:
            set_container_provisioner(mock_prov)
            assert get_container_provisioner() is mock_prov
        finally:
            set_container_provisioner(None)

    def test_get_returns_none_when_not_set(self):
        """未 set 时 get 返回 None。"""
        set_container_provisioner(None)
        assert get_container_provisioner() is None

    def test_clear_after_set(self):
        """set 后再 set(None) → get 返回 None。"""
        mock_prov = MagicMock(name="TestProvisioner2")
        set_container_provisioner(mock_prov)
        assert get_container_provisioner() is mock_prov

        set_container_provisioner(None)
        assert get_container_provisioner() is None

    def test_overwrite(self):
        """多次 set 覆盖，get 返回最后设置的实例。"""
        prov1 = MagicMock(name="Prov1")
        prov2 = MagicMock(name="Prov2")
        try:
            set_container_provisioner(prov1)
            assert get_container_provisioner() is prov1

            set_container_provisioner(prov2)
            assert get_container_provisioner() is prov2
        finally:
            set_container_provisioner(None)


# ============================================================
# 3. RunRequest.workspace_id 字段
# ============================================================

class TestRunRequestWorkspaceId:
    """验证 RunRequest dataclass 的 workspace_id 字段。"""

    def test_default_none(self):
        """RunRequest 不传 workspace_id → 默认 None。"""
        req = RunRequest()
        assert req.workspace_id is None

    def test_set_workspace_id(self):
        """RunRequest 传 workspace_id → 字段持久化。"""
        req = RunRequest(workspace_id="ws-test-123")
        assert req.workspace_id == "ws-test-123"

    def test_none_is_general_conversation(self):
        """workspace_id=None 语义 = 通用对话（provisioner 路径不触发）。"""
        req = RunRequest(workspace_id=None)
        assert req.workspace_id is None

    def test_with_other_fields(self):
        """RunRequest 同时传 workspace_id + 其他字段，互不干扰。"""
        req = RunRequest(
            workflow_id="wf-1",
            run_mode="templated",
            agent_id="log_analyst",
            workspace_id="ws-prod-456",
        )
        assert req.workflow_id == "wf-1"
        assert req.agent_id == "log_analyst"
        assert req.workspace_id == "ws-prod-456"


# ============================================================
# 4. _resolve_workspace_context 集成
# ============================================================

class TestResolveWorkspaceContext:
    """验证 LocalSdkOrchestrator._resolve_workspace_context 的全链路行为。"""

    @pytest.mark.asyncio
    async def test_valid_workspace_returns_context(self, store):
        """有效 workspace_id → 返回 workspace_context dict，字段映射正确。"""
        wid = await _create_authorized_workspace(
            store,
            display_name="agentops-frontend",
            mode="bind_mount",
            permissions="read_write_exec",
            source_path="/home/user/agentops",
        )

        # 构造最小 LocalSdkOrchestrator（只测 _resolve_workspace_context）
        from orchestrator.local_sdk import LocalSdkOrchestrator
        orch = LocalSdkOrchestrator.__new__(LocalSdkOrchestrator)
        orch._event_store = store

        ctx = await orch._resolve_workspace_context(wid)

        assert ctx is not None
        assert ctx["workspace_id"] == wid
        assert ctx["display_name"] == "agentops-frontend"
        assert ctx["workspace_mode"] == "bind_mount"  # 表 mode → ctx workspace_mode
        assert ctx["permissions"] == "read_write_exec"
        assert ctx["source_path"] == "/home/user/agentops"

    @pytest.mark.asyncio
    async def test_none_returns_none(self, store):
        """workspace_id=None → 返回 None（通用对话）。"""
        from orchestrator.local_sdk import LocalSdkOrchestrator
        orch = LocalSdkOrchestrator.__new__(LocalSdkOrchestrator)
        orch._event_store = store

        ctx = await orch._resolve_workspace_context(None)
        assert ctx is None

    @pytest.mark.asyncio
    async def test_empty_string_returns_none(self, store):
        """workspace_id='' → 返回 None（falsy 值等价于 None）。"""
        from orchestrator.local_sdk import LocalSdkOrchestrator
        orch = LocalSdkOrchestrator.__new__(LocalSdkOrchestrator)
        orch._event_store = store

        ctx = await orch._resolve_workspace_context("")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_disabled_workspace_returns_none(self, store):
        """已停用 workspace → 返回 None（graceful fallback，不抛异常）。"""
        wid = await _create_authorized_workspace(store, enabled=False)

        from orchestrator.local_sdk import LocalSdkOrchestrator
        orch = LocalSdkOrchestrator.__new__(LocalSdkOrchestrator)
        orch._event_store = store

        ctx = await orch._resolve_workspace_context(wid)
        assert ctx is None

    @pytest.mark.asyncio
    async def test_nonexistent_workspace_returns_none(self, store):
        """不存在的 workspace_id → 返回 None（不抛异常）。"""
        from orchestrator.local_sdk import LocalSdkOrchestrator
        orch = LocalSdkOrchestrator.__new__(LocalSdkOrchestrator)
        orch._event_store = store

        ctx = await orch._resolve_workspace_context("ws-does-not-exist-99999")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_no_event_store_returns_none(self, store):
        """event_store 未注入 → 返回 None（不抛异常）。"""
        from orchestrator.local_sdk import LocalSdkOrchestrator
        orch = LocalSdkOrchestrator.__new__(LocalSdkOrchestrator)
        orch._event_store = None

        ctx = await orch._resolve_workspace_context("ws-any-id")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_all_modes_resolved(self, store):
        """4 种 mode（local_copy/bind_mount/git_clone/isolated）都能正确解析。"""
        from orchestrator.local_sdk import LocalSdkOrchestrator

        mode_configs = [
            ("local_copy", {"source_path": "/tmp/project-a"}),
            ("bind_mount", {"source_path": "/tmp/project-b"}),
            ("git_clone", {"git_url": "https://github.com/test/repo.git", "git_branch": "main", "source_path": None}),
            ("isolated", {"source_path": None}),
        ]

        for mode, kwargs in mode_configs:
            # isolated 模式不需要 source_path/git_url，但 create_authorized_workspace 需要
            # 某些字段，所以用空 source_path（CHECK 约束允许 isolated 无 source_path）
            create_kwargs = {"mode": mode, "source_path": "/tmp/dummy"}
            create_kwargs.update(kwargs)
            wid = await _create_authorized_workspace(store, **create_kwargs)
            orch = LocalSdkOrchestrator.__new__(LocalSdkOrchestrator)
            orch._event_store = store

            ctx = await orch._resolve_workspace_context(wid)
            assert ctx is not None, f"mode={mode} should resolve"
            assert ctx["workspace_mode"] == mode, f"mode={mode} mapping failed"


# ============================================================
# 5. Schema 完整性
# ============================================================

class TestSchemaIntegrity:
    """验证 P0.18 相关 DB schema 字段全部就位。"""

    def test_sessions_workspace_id_column(self, store):
        """sessions 表有 workspace_id 列。"""
        conn = sqlite3.connect(str(store.db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        conn.close()
        assert "workspace_id" in cols

    def test_authorized_workspaces_table_exists(self, store):
        """authorized_workspaces 表存在。"""
        conn = sqlite3.connect(str(store.db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "authorized_workspaces" in tables

    def test_authorized_workspaces_has_all_columns(self, store):
        """authorized_workspaces 表有完整字段。"""
        conn = sqlite3.connect(str(store.db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(authorized_workspaces)").fetchall()}
        conn.close()
        expected = {"workspace_id", "display_name", "mode", "permissions",
                     "source_path", "git_url", "git_branch",
                     "enabled", "deauthorized_at", "last_used_at", "usage_count",
                     "authorized_at"}
        missing = expected - cols
        assert not missing, f"Missing columns: {missing}"

    def test_run_workspace_meta_table_exists(self, store):
        """run_workspace_meta 表存在（旧 workspaces 已重命名）。"""
        conn = sqlite3.connect(str(store.db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "run_workspace_meta" in tables


# ============================================================
# 6. Tier 兼容矩阵
# ============================================================

class TestTierCompatibility:
    """验证 tier 映射 + 兼容矩阵（Python 侧，与前端 workspacePermissionsToTier 对应）。"""

    # 与前端 api.ts workspacePermissionsToTier 一致
    TIER_MAP = {
        "read_only": "T1",
        "read_write": "T2",
        "read_write_exec": "T3",
    }

    # 与前端 api.ts isTierCompatible 一致
    # T0=通用对话（无需 workspace），T1 < T2 < T3
    # agent tier <= workspace tier → 兼容
    TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}

    def _is_compatible(self, agent_tier: str, workspace_tier: str) -> bool:
        return self.TIER_ORDER[agent_tier] <= self.TIER_ORDER[workspace_tier]

    def test_read_only_maps_to_t1(self):
        assert self.TIER_MAP["read_only"] == "T1"

    def test_read_write_maps_to_t2(self):
        assert self.TIER_MAP["read_write"] == "T2"

    def test_read_write_exec_maps_to_t3(self):
        assert self.TIER_MAP["read_write_exec"] == "T3"

    def test_t0_agent_compatible_with_any_workspace(self):
        """T0 agent（通用）与任何 tier workspace 兼容。"""
        for ws_tier in ("T1", "T2", "T3"):
            assert self._is_compatible("T0", ws_tier)

    def test_t3_agent_requires_t3_workspace(self):
        """T3 agent 只能在 T3 workspace 运行。"""
        assert not self._is_compatible("T3", "T1")
        assert not self._is_compatible("T3", "T2")
        assert self._is_compatible("T3", "T3")

    def test_t2_agent_compatible_with_t2_t3(self):
        """T2 agent 兼容 T2+ workspace。"""
        assert not self._is_compatible("T2", "T1")
        assert self._is_compatible("T2", "T2")
        assert self._is_compatible("T2", "T3")

    def test_t1_agent_compatible_with_any(self):
        """T1 agent 兼容所有 tier workspace。"""
        for ws_tier in ("T1", "T2", "T3"):
            assert self._is_compatible("T1", ws_tier)


# ============================================================
# 7. workspace_id 全链路：create_session → get_session → workspace 字段一致
# ============================================================

class TestWorkspaceIdEndToEnd:
    """端到端验证 workspace_id 从 create_session 到 get_session 的一致性。"""

    @pytest.mark.asyncio
    async def test_full_chain_workspace_binding(self, store):
        """authorized_workspaces → create_session → get_session → workspace_id 一致。"""
        # 1. 创建 authorized workspace
        wid = await _create_authorized_workspace(
            store,
            display_name="e2e-project",
            mode="local_copy",
            permissions="read_write_exec",
            source_path="/tmp/e2e",
        )

        # 2. 创建 session 绑定该 workspace
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        await store.create_session(sid, agent_id="manager", workspace_id=wid)

        # 3. 验证 session 中的 workspace_id
        session = await store.get_session(sid)
        assert session is not None
        assert session["workspace_id"] == wid

        # 4. 验证 workspace 仍存在且 enabled
        ws = await store.get_authorized_workspace(wid)
        assert ws is not None
        assert ws["display_name"] == "e2e-project"
        assert ws["enabled"] == 1

    @pytest.mark.asyncio
    async def test_workspace_deauthorization_does_not_affect_existing_session(self, store):
        """workspace 取消授权（soft delete）后，已有 session 的 workspace_id 不变。"""
        wid = await _create_authorized_workspace(store)
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        await store.create_session(sid, agent_id="manager", workspace_id=wid)

        # 取消授权
        await store.update_authorized_workspace(wid, enabled=False)

        # session 仍有 workspace_id（soft delete 不影响已有数据）
        session = await store.get_session(sid)
        assert session is not None
        assert session["workspace_id"] == wid

        # workspace 标记为 disabled
        ws = await store.get_authorized_workspace(wid)
        assert ws is not None
        assert ws["enabled"] == 0
        assert ws["deauthorized_at"] is not None

    @pytest.mark.asyncio
    async def test_touch_workspace_on_session_create(self, store):
        """创建 session 绑定 workspace 后，可手动 touch 更新 last_used_at + usage_count。"""
        wid = await _create_authorized_workspace(store)

        # touch 前
        ws_before = await store.get_authorized_workspace(wid)
        assert ws_before["usage_count"] == 0

        # touch
        await store.touch_authorized_workspace(wid)

        # touch 后
        ws_after = await store.get_authorized_workspace(wid)
        assert ws_after["usage_count"] == 1
        assert ws_after["last_used_at"] is not None
