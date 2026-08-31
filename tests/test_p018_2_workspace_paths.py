"""P0.18.2 workspace_paths.py 测试。

覆盖：
1. Path.relative_to 严格子树判定（修 startswith 路径穿越漏洞）
2. allow-list 主防线：未授权路径拒绝
3. DENIED_PATHS 双保险：系统目录即使授权也拒绝
4. mode 落地：local_copy（含排除规则）/ bind_mount / git_clone / isolated
5. build_container_mounts + extra_volumes path traversal 防护
6. tier 兼容性 + effective_tier
7. symlink 解析（Path.resolve）
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from audit import SqliteEventStore
from orchestrator.workspace_paths import (
    MountPolicyError,
    WorkspaceNotFoundError,
    WorkspaceInfo,
    PreparedWorkspace,
    assert_within_workspace,
    async_is_authorized_workspace_path,
    validate_mount_path,
    prepare_workspace,
    resolve_workspace_root,
    build_container_mounts,
    tier_compatible,
    effective_tier,
    _normalize_path,
    _matches_denied,
    LOCAL_COPY_EXCLUDE_DIRS,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = tmp_path / "test_p018_2.db"
    s = SqliteEventStore(str(db_path))
    yield s
    await s.close()


@pytest_asyncio.fixture
async def workspace_with_source(store, tmp_path):
    """创建一个有真实 source_path 的 authorized_workspace。"""
    source_dir = tmp_path / "project-src"
    source_dir.mkdir()
    (source_dir / "README.md").write_text("# test project")
    (source_dir / "main.py").write_text("print('hello')")
    # 创建应被排除的目录
    (source_dir / "node_modules").mkdir()
    (source_dir / "node_modules" / "lib.js").write_text("// lib")
    (source_dir / ".git").mkdir()
    (source_dir / ".git" / "config").write_text("[core]")

    ws = await store.create_authorized_workspace(
        workspace_id="ws-test-1",
        display_name="test-project",
        mode="local_copy",
        permissions="read_write",
        source_path=str(source_dir),
    )
    return ws, source_dir


# ============================================================
# Path.relative_to 严格子树判定（修 startswith 漏洞）
# ============================================================

class TestAssertWithinWorkspace:
    """v2 修复 v1 startswith 路径穿越漏洞。"""

    def test_path_within_workspace_ok(self, tmp_path):
        """子树内路径通过。"""
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        target = ws_root / "subdir" / "file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("test")
        assert_within_workspace(str(target), str(ws_root), "read_write", is_write_op=False)

    def test_path_outside_workspace_rejected(self, tmp_path):
        """子树外路径拒绝。"""
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(PermissionError, match="outside workspace"):
            assert_within_workspace(str(outside), str(ws_root), "read_write")

    def test_startswith_prefix_vulnerability_fixed(self, tmp_path):
        """v2 修复：startswith('/workspace') 会通过 '/workspace-evil'，Path.relative_to 不会。"""
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        # 构造前缀碰撞：workspace-evil 不在 workspace 子树内
        evil = tmp_path / "workspace-evil"
        evil.mkdir()
        evil_file = evil / "secret.txt"
        evil_file.write_text("stolen")
        # Path.relative_to 应拒绝（不在 workspace 子树内）
        with pytest.raises(PermissionError, match="outside workspace"):
            assert_within_workspace(str(evil_file), str(ws_root), "read_write")

    def test_symlink_escape_rejected(self, tmp_path):
        """v2 修复：symlink 指向 workspace 外的目录应被拒绝（Path.resolve 解析 symlink）。"""
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        outside = tmp_path / "outside-secret"
        outside.mkdir()
        outside_file = outside / "secret.txt"
        outside_file.write_text("secret")
        # 在 workspace 内创建指向 outside 的 symlink
        symlink = ws_root / "escape"
        try:
            symlink.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation not supported on this platform")
        # symlink 路径解析后指向 outside，应被拒绝
        with pytest.raises(PermissionError, match="outside workspace"):
            assert_within_workspace(str(symlink / "secret.txt"), str(ws_root), "read_write")

    def test_dotdot_traversal_rejected(self, tmp_path):
        """../../ 路径穿越应被拒绝（单层 ../ 仍在子树内，需要双层才能跳出）。"""
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        (ws_root / "subdir").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # 构造 ../../outside 路径（跳出 ws_root）
        target = ws_root / "subdir" / ".." / ".." / "outside"
        with pytest.raises(PermissionError, match="outside workspace"):
            assert_within_workspace(str(target), str(ws_root), "read_write")

    def test_read_only_workspace_rejects_write(self, tmp_path):
        """read_only workspace 拒绝写操作。"""
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        target = ws_root / "file.txt"
        target.write_text("test")
        with pytest.raises(PermissionError, match="read-only"):
            assert_within_workspace(str(target), str(ws_root), "read_only", is_write_op=True)

    def test_read_only_workspace_allows_read(self, tmp_path):
        """read_only workspace 允许读操作。"""
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        target = ws_root / "file.txt"
        target.write_text("test")
        assert_within_workspace(str(target), str(ws_root), "read_only", is_write_op=False)


# ============================================================
# allow-list 主防线
# ============================================================

class TestAllowList:
    """v2: allow-list 是主防线，DENIED_PATHS 仅双保险。"""

    @pytest.mark.asyncio
    async def test_authorized_path_passes(self, store, workspace_with_source):
        """已授权路径通过 allow-list。"""
        ws, source_dir = workspace_with_source
        result = await async_is_authorized_workspace_path(store, str(source_dir))
        assert result is not None
        assert result.workspace_id == "ws-test-1"

    @pytest.mark.asyncio
    async def test_unauthorized_path_rejected(self, store, tmp_path):
        """未授权路径被 allow-list 拒绝。"""
        random_dir = tmp_path / "unauthorized"
        random_dir.mkdir()
        result = await async_is_authorized_workspace_path(store, str(random_dir))
        assert result is None

    @pytest.mark.asyncio
    async def test_subpath_of_authorized_passes(self, store, workspace_with_source):
        """已授权路径的子路径通过。"""
        ws, source_dir = workspace_with_source
        sub = source_dir / "subdir"
        sub.mkdir()
        result = await async_is_authorized_workspace_path(store, str(sub))
        assert result is not None

    @pytest.mark.asyncio
    async def test_validate_mount_path_returns_workspace_info(self, store, workspace_with_source):
        """validate_mount_path 返回 WorkspaceInfo。"""
        ws, source_dir = workspace_with_source
        info = await validate_mount_path(store, str(source_dir))
        assert info.workspace_id == "ws-test-1"
        assert info.mode == "local_copy"

    @pytest.mark.asyncio
    async def test_validate_mount_path_rejects_unauthorized(self, store, tmp_path):
        """validate_mount_path 拒绝未授权路径。"""
        random_dir = tmp_path / "unauthorized"
        random_dir.mkdir()
        with pytest.raises(MountPolicyError, match="not in any authorized workspace"):
            await validate_mount_path(store, str(random_dir))


# ============================================================
# DENIED_PATHS 双保险
# ============================================================

class TestDenyList:
    """DENIED_PATHS 是双保险，即使授权也拒绝。"""

    def test_denied_linux_paths(self):
        """Linux 系统目录命中 deny-list。"""
        if sys.platform == "win32":
            pytest.skip("Linux-specific test")
        assert _matches_denied("/etc/nginx") == "/etc"
        assert _matches_denied("/proc/123") == "/proc"
        assert _matches_denied("/.ssh/config") == "/.ssh"

    def test_denied_windows_paths(self):
        """Windows 系统目录命中 deny-list。"""
        if sys.platform != "win32":
            pytest.skip("Windows-specific test")
        assert _matches_denied("C:/Windows/System32") is not None
        assert _matches_denied("C:/Program Files/app") is not None

    def test_normal_path_not_denied(self, tmp_path):
        """普通路径不命中 deny-list。"""
        assert _matches_denied(str(tmp_path)) is None


# ============================================================
# mode 落地
# ============================================================

class TestPrepareWorkspace:
    """四层 mode 落地。"""

    @pytest.mark.asyncio
    async def test_prepare_local_copy(self, store, workspace_with_source, tmp_path):
        """local_copy: 复制 source 到 sandbox，排除 node_modules/.git。"""
        ws, source_dir = workspace_with_source
        ws_info = WorkspaceInfo.from_row(ws)
        prepared = await prepare_workspace(
            store, ws_info, run_id="run-test-1", agentops_home=str(tmp_path / "home")
        )
        assert prepared.mode == "local_copy"
        assert prepared.workspace_id == "ws-test-1"
        # sandbox 路径
        sandbox = Path(prepared.workspace_root)
        assert sandbox.exists()
        # 检查文件已复制
        assert (sandbox / "README.md").read_text() == "# test project"
        assert (sandbox / "main.py").read_text() == "print('hello')"
        # 检查排除目录
        assert not (sandbox / "node_modules").exists()
        assert not (sandbox / ".git").exists()

    @pytest.mark.asyncio
    async def test_prepare_isolated(self, store, tmp_path):
        """isolated: 创建空 sandbox。"""
        ws = await store.create_authorized_workspace(
            workspace_id="ws-iso-1",
            display_name="isolated-test",
            mode="isolated",
            permissions="read_write_exec",
        )
        ws_info = WorkspaceInfo.from_row(ws)
        prepared = await prepare_workspace(
            store, ws_info, run_id="run-iso-1", agentops_home=str(tmp_path / "home")
        )
        assert prepared.mode == "isolated"
        sandbox = Path(prepared.workspace_root)
        assert sandbox.exists()
        assert sandbox.is_dir()
        # 空目录
        assert not any(sandbox.iterdir())

    @pytest.mark.asyncio
    async def test_prepare_bind_mount(self, store, workspace_with_source, tmp_path):
        """bind_mount: workspace_root = source_path（不复制）。"""
        ws, source_dir = workspace_with_source
        # 改 mode 为 bind_mount
        await store.update_authorized_workspace("ws-test-1", )
        ws_updated = await store.get_authorized_workspace("ws-test-1")
        # 直接改 DB（update_authorized_workspace 不支持改 mode，用 SQL）
        import sqlite3
        conn = sqlite3.connect(str(store.db_path))
        conn.execute("UPDATE authorized_workspaces SET mode='bind_mount' WHERE workspace_id='ws-test-1'")
        conn.commit()
        conn.close()
        ws_info = WorkspaceInfo.from_row(await store.get_authorized_workspace("ws-test-1"))
        prepared = await prepare_workspace(
            store, ws_info, run_id="run-bind-1", agentops_home=str(tmp_path / "home")
        )
        assert prepared.mode == "bind_mount"
        # bind_mount: workspace_root = source_path
        assert prepared.workspace_root == str(source_dir)

    @pytest.mark.asyncio
    async def test_prepare_disabled_workspace_rejected(self, store, tmp_path):
        """disabled workspace 拒绝。"""
        ws = await store.create_authorized_workspace(
            workspace_id="ws-disabled",
            display_name="disabled",
            mode="isolated",
            permissions="read_write",
        )
        await store.delete_authorized_workspace("ws-disabled")
        ws_row = await store.get_authorized_workspace("ws-disabled")
        ws_info = WorkspaceInfo.from_row(ws_row)
        with pytest.raises(WorkspaceNotFoundError, match="disabled"):
            await prepare_workspace(store, ws_info, run_id="r1", agentops_home=str(tmp_path))

    @pytest.mark.asyncio
    async def test_prepare_local_copy_missing_source(self, store, tmp_path):
        """local_copy 源目录不存在报错。"""
        ws = await store.create_authorized_workspace(
            workspace_id="ws-missing-src",
            display_name="missing-src",
            mode="local_copy",
            permissions="read_write",
            source_path=str(tmp_path / "nonexistent"),
        )
        ws_info = WorkspaceInfo.from_row(ws)
        with pytest.raises(Exception, match="does not exist"):
            await prepare_workspace(store, ws_info, run_id="r1", agentops_home=str(tmp_path))


# ============================================================
# build_container_mounts + extra_volumes path traversal 防护
# ============================================================

class TestBuildContainerMounts:
    """mount 列表生成 + extra_volumes 安全校验。"""

    def test_basic_mount_local_copy(self, workspace_with_source, tmp_path):
        """local_copy 基本挂载。"""
        ws, source_dir = workspace_with_source
        ws_info = WorkspaceInfo.from_row(ws)
        prepared = PreparedWorkspace(
            workspace_id="ws-test-1",
            mode="local_copy",
            permissions="read_write",
            workspace_root=str(tmp_path / "sandbox"),
        )
        mounts = build_container_mounts(ws_info, prepared)
        assert len(mounts) == 1
        assert mounts[0]["host"] == str(tmp_path / "sandbox")
        assert mounts[0]["container"] == "/workspace"
        assert mounts[0]["mode"] == "rw"

    def test_basic_mount_read_only(self, workspace_with_source, tmp_path):
        """read_only workspace 的 mount mode 是 ro。"""
        ws, source_dir = workspace_with_source
        ws_info = WorkspaceInfo.from_row(ws)
        ws_info.permissions = "read_only"
        prepared = PreparedWorkspace(
            workspace_id="ws-test-1",
            mode="local_copy",
            permissions="read_only",
            workspace_root=str(tmp_path / "sandbox"),
        )
        mounts = build_container_mounts(ws_info, prepared)
        assert mounts[0]["mode"] == "ro"

    def test_extra_volumes_within_source_ok(self, workspace_with_source, tmp_path):
        """extra_volumes 在 source_path 子树内通过。"""
        ws, source_dir = workspace_with_source
        ws_info = WorkspaceInfo.from_row(ws)
        sub_dir = source_dir / "data"
        sub_dir.mkdir()
        prepared = PreparedWorkspace(
            workspace_id="ws-test-1",
            mode="local_copy",
            permissions="read_write",
            workspace_root=str(tmp_path / "sandbox"),
        )
        mounts = build_container_mounts(ws_info, prepared, extra_volumes=[
            {"host": str(sub_dir), "container": "/data", "mode": "rw"}
        ])
        assert len(mounts) == 2
        assert mounts[1]["host"] == str(sub_dir)
        assert mounts[1]["container"] == "/data"

    def test_extra_volumes_outside_source_rejected(self, workspace_with_source, tmp_path):
        """extra_volumes 在 source_path 外被拒绝（path traversal 防护）。"""
        ws, source_dir = workspace_with_source
        ws_info = WorkspaceInfo.from_row(ws)
        outside = tmp_path / "outside"
        outside.mkdir()
        prepared = PreparedWorkspace(
            workspace_id="ws-test-1",
            mode="local_copy",
            permissions="read_write",
            workspace_root=str(tmp_path / "sandbox"),
        )
        with pytest.raises(MountPolicyError, match="outside workspace source_path"):
            build_container_mounts(ws_info, prepared, extra_volumes=[
                {"host": str(outside), "container": "/evil"}
            ])

    def test_extra_volumes_invalid_format_rejected(self, workspace_with_source, tmp_path):
        """extra_volumes 格式错误被拒绝。"""
        ws, source_dir = workspace_with_source
        ws_info = WorkspaceInfo.from_row(ws)
        prepared = PreparedWorkspace(
            workspace_id="ws-test-1",
            mode="local_copy",
            permissions="read_write",
            workspace_root=str(tmp_path / "sandbox"),
        )
        with pytest.raises(ValueError, match="invalid extra_volume entry"):
            build_container_mounts(ws_info, prepared, extra_volumes=[
                "not-a-dict"  # 应该是 dict
            ])


# ============================================================
# tier 兼容性
# ============================================================

class TestTierCompatibility:
    """tier 兼容矩阵 + effective_tier 计算。"""

    def test_workspace_supports_agent_lower_tier(self):
        """workspace T3 支持 agent T1（agent 能力低于 workspace 授权）。"""
        assert tier_compatible("T3", "T1") is True
        assert tier_compatible("T3", "T2") is True
        assert tier_compatible("T3", "T3") is True

    def test_workspace_rejects_agent_higher_tier(self):
        """workspace T1 拒绝 agent T3（agent 能力超过 workspace 授权）。"""
        assert tier_compatible("T1", "T3") is False
        assert tier_compatible("T1", "T2") is False
        assert tier_compatible("T2", "T3") is False

    def test_workspace_t0_rejects_all(self):
        """workspace T0（通用对话）拒绝所有 agent。"""
        assert tier_compatible("T0", "T1") is False
        assert tier_compatible("T0", "T2") is False
        assert tier_compatible("T0", "T3") is False

    def test_effective_tier_min(self):
        """effective_tier = min(workspace, agent)。"""
        assert effective_tier("T3", "T1") == "T1"
        assert effective_tier("T1", "T3") == "T1"
        assert effective_tier("T2", "T2") == "T2"
        assert effective_tier("T3", "T3") == "T3"
        assert effective_tier("T0", "T3") == "T0"


# ============================================================
# WorkspaceInfo.from_row
# ============================================================

class TestWorkspaceInfo:
    """WorkspaceInfo 数据类。"""

    @pytest.mark.asyncio
    async def test_from_row(self, workspace_with_source):
        ws, _ = workspace_with_source
        info = WorkspaceInfo.from_row(ws)
        assert info.workspace_id == "ws-test-1"
        assert info.display_name == "test-project"
        assert info.mode == "local_copy"
        assert info.permissions == "read_write"
        assert info.enabled is True

    @pytest.mark.asyncio
    async def test_tier_property(self, workspace_with_source):
        ws, _ = workspace_with_source
        info = WorkspaceInfo.from_row(ws)
        assert info.tier == "T2"  # read_write → T2

    @pytest.mark.asyncio
    async def test_tier_property_read_only(self, store, tmp_path):
        ws = await store.create_authorized_workspace(
            workspace_id="ws-ro",
            display_name="ro",
            mode="isolated",
            permissions="read_only",
        )
        info = WorkspaceInfo.from_row(ws)
        assert info.tier == "T1"

    @pytest.mark.asyncio
    async def test_tier_property_read_write_exec(self, store, tmp_path):
        ws = await store.create_authorized_workspace(
            workspace_id="ws-rwx",
            display_name="rwx",
            mode="isolated",
            permissions="read_write_exec",
        )
        info = WorkspaceInfo.from_row(ws)
        assert info.tier == "T3"


# ============================================================
# resolve_workspace_root（纯路径解析，不落地）
# ============================================================

class TestResolveWorkspaceRoot:
    """产物目录锚定：resolve_workspace_root 按 mode 计算绝对根路径。"""

    def test_bind_mount_returns_source_path(self):
        ws = WorkspaceInfo(
            workspace_id="ws-bm", display_name="proj", mode="bind_mount",
            permissions="read_write_exec", source_path="E:/Project/AgentOps",
            git_url=None, git_branch=None, enabled=True,
        )
        root = resolve_workspace_root(ws, "run_1")
        # bind_mount 直挂用户授权目录，产物跟随项目走
        assert root == _normalize_path("E:/Project/AgentOps")

    def test_isolated_returns_sandbox(self):
        ws = WorkspaceInfo(
            workspace_id="ws-iso", display_name="iso", mode="isolated",
            permissions="read_write", source_path=None,
            git_url=None, git_branch=None, enabled=True,
        )
        root = resolve_workspace_root(ws, "run_1")
        home = os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops"))
        assert root == os.path.abspath(os.path.join(home, "workspaces", "ws-iso", "run_1"))

    def test_local_copy_returns_sandbox(self):
        ws = WorkspaceInfo(
            workspace_id="ws-cp", display_name="cp", mode="local_copy",
            permissions="read_write", source_path="E:/somewhere",
            git_url=None, git_branch=None, enabled=True,
        )
        root = resolve_workspace_root(ws, "run_1")
        home = os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops"))
        assert root == os.path.abspath(os.path.join(home, "workspaces", "ws-cp", "run_1"))

    def test_bind_mount_missing_source_raises(self):
        ws = WorkspaceInfo(
            workspace_id="ws-bad", display_name="bad", mode="bind_mount",
            permissions="read_write", source_path=None,
            git_url=None, git_branch=None, enabled=True,
        )
        from orchestrator.workspace_paths import WorkspaceModeError
        with pytest.raises(WorkspaceModeError):
            resolve_workspace_root(ws, "run_1")
