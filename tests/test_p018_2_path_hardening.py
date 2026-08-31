"""P0.18.2 加固 — 路径穿越修复进一步加固的单元测试。

覆盖 6 个加固点：
1. NUL byte（\x00）拦截
2. Windows UNC 路径（\\\\server\\share）拦截
3. Windows 设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）拦截
4. Windows 8.3 短文件名（PROGRA~1）拦截
5. Windows 大小写不敏感的 deny-list（前缀绕过）
6. symlink 跨 workspace 逃逸（extra_volumes / assert_within_workspace）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from orchestrator.workspace_paths import (
    PathSecurityError,
    MountPolicyError,
    WorkspaceInfo,
    PreparedWorkspace,
    _normalize_path,
    _validate_raw_path,
    _matches_denied,
    assert_within_workspace,
    build_container_mounts,
)


# ============================================================
# 加固点 1：NUL byte 拦截
# ============================================================

class TestNulByteRejection:
    """NUL byte (\x00) 拦截 — Python os API 在 C 层抛错 / 截断。"""

    def test_nul_in_path_rejected(self):
        """路径含 NUL 字节应拒绝。"""
        with pytest.raises(PathSecurityError, match="NUL byte"):
            _validate_raw_path("/tmp/safe\x00../../etc/passwd")

    def test_nul_at_start_rejected(self):
        with pytest.raises(PathSecurityError, match="NUL byte"):
            _validate_raw_path("\x00/tmp/safe")

    def test_nul_in_normalize_rejected(self):
        """_normalize_path 应拦截 NUL byte（避免传给 C API）。"""
        with pytest.raises(PathSecurityError, match="NUL byte"):
            _normalize_path("/tmp/\x00evil")

    def test_normal_path_no_nul_ok(self, tmp_path):
        """普通路径不应触发 NUL byte 拒绝。"""
        result = _normalize_path(str(tmp_path))
        assert result  # 不抛错


# ============================================================
# 加固点 2：Windows UNC 路径拦截
# ============================================================

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
class TestUNCPathRejection:
    """Windows UNC 路径（\\\\server\\share）拦截 — 网络共享绕过本地 deny-list。"""

    def test_unc_backslashes_rejected(self):
        with pytest.raises(PathSecurityError, match="UNC"):
            _validate_raw_path(r"\\evil-server\share\etc\passwd")

    def test_unc_forward_slashes_rejected(self):
        with pytest.raises(PathSemanticError if False else PathSecurityError, match="UNC"):
            _validate_raw_path("//evil-server/share/etc/passwd")


# ============================================================
# 加固点 3：Windows 设备名拦截
# ============================================================

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
class TestReservedDeviceNameRejection:
    """Windows 设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）拦截。"""

    @pytest.mark.parametrize("device", [
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM5", "COM9",
        "LPT1", "LPT3", "LPT9",
    ])
    def test_reserved_device_name_rejected(self, device):
        with pytest.raises(PathSecurityError, match="reserved device"):
            _validate_raw_path(f"C:/Users/Alice/{device}.txt")

    @pytest.mark.parametrize("device", ["con", "Prn", "AUX", "com1"])
    def test_reserved_device_name_case_insensitive_rejected(self, device):
        """大小写不敏感也应拦截（Windows 设备名不区分大小写）。"""
        with pytest.raises(PathSecurityError, match="reserved device"):
            _validate_raw_path(f"D:/foo/{device}")

    def test_normal_filename_with_con_substring_ok(self, tmp_path):
        """文件名含 con 子串（如 console.log）不应误拦截。"""
        # console.log 的 stem = console ≠ "CON"
        _validate_raw_path(str(tmp_path / "console.log"))


# ============================================================
# 加固点 4：Windows 8.3 短文件名拦截
# ============================================================

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
class TestShortFilenameRejection:
    """Windows 8.3 短文件名（PROGRA~1）拦截 — 大小写不敏感下绕过 deny-list。"""

    @pytest.mark.parametrize("short", [
        "PROGRA~1", "PROGRA~2", "ADMINI~1",
        "TESTIN~1", "DOCUME~1",
    ])
    def test_8_3_short_filename_rejected(self, short):
        with pytest.raises(PathSecurityError, match="8.3"):
            _validate_raw_path(f"C:/{short}/secret.txt")

    def test_tilde_with_long_prefix_ok(self):
        """长前缀后跟 ~1 不应误拦截（启发式只命中 8 字符前缀）。"""
        # "longprefx~1.txt" stem = "longprefx~1" — 但 Windows 8.3 短文件名最大是 8 字符前缀，
        # 真实场景下 "<9 chars>~N" 不太可能是 8.3。但"abcdefgh~1"（恰好 8 字符）会被识别。
        # 用 9 字符前缀：abcdefghX~1 不会被误拦截。
        _validate_raw_path("C:/Users/Alice/longprefx9~1.txt")


# ============================================================
# 加固点 5：Windows 大小写不敏感的 deny-list
# ============================================================

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
class TestDenyListCaseInsensitive:
    """Windows deny-list 大小写不敏感。"""

    def test_lowercase_windows_denied(self):
        """C:/windows（不含大写 W）也应被 deny。"""
        assert _matches_denied("c:/windows/system32") is not None

    def test_mixed_case_windows_denied(self):
        assert _matches_denied("C:/WINDOWS/System32") is not None

    def test_uppercase_program_files_denied(self):
        assert _matches_denied("C:/PROGRAM FILES/app") is not None

    def test_prefix_collision_not_denied(self, tmp_path):
        """前缀碰撞：/workspace-evil 不应被 deny-list 里的 /workspace 误命中。

        用本地 tmp_path 模拟：/tmp-evil vs /tmp 不会误命中。
        """
        # 临时构造：在 tmp_path 下创建 workspace 与 workspace-evil
        ws = tmp_path / "workspace"
        ws.mkdir()
        ws_evil = tmp_path / "workspace-evil"
        ws_evil.mkdir()
        # 两个目录都不会在 DENIED_PATHS_LINUX / DENIED_PATHS_WINDOWS 中
        assert _matches_denied(str(ws)) is None
        assert _matches_denied(str(ws_evil)) is None


# ============================================================
# 加固点 6：symlink 跨 workspace 逃逸（extra_volumes）
# ============================================================

class TestSymlinkEscapeInExtraVolumes:
    """P0.18.2 加固：symlink 指向 workspace source_path 外的目标应拒绝。"""

    def test_symlink_escape_rejected_in_extra_volumes(self, tmp_path):
        """extra_volumes 中的 host 是 symlink，指向 source_path 外应拒绝。"""
        ws_source = tmp_path / "project"
        ws_source.mkdir()
        outside = tmp_path / "outside-secret"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")

        # 在 ws_source 内创建指向 outside 的 symlink
        symlink_path = ws_source / "escape-link"
        try:
            symlink_path.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation not supported on this platform")

        workspace = WorkspaceInfo(
            workspace_id="ws-sym-1",
            display_name="sym-test",
            mode="local_copy",
            permissions="read_write",
            source_path=str(ws_source),
            git_url=None,
            git_branch=None,
            enabled=True,
        )
        prepared = PreparedWorkspace(
            workspace_id="ws-sym-1",
            mode="local_copy",
            permissions="read_write",
            workspace_root=str(ws_source),
        )

        # extra_volume host = symlink，resolve 后指向 outside → 应被拒绝
        with pytest.raises(MountPolicyError, match="outside workspace"):
            build_container_mounts(
                workspace, prepared,
                extra_volumes=[{"host": str(symlink_path), "container": "/data"}],
            )

    def test_symlink_within_subtree_ok(self, tmp_path):
        """symlink 指向 workspace 子树内其他位置应允许（Path.resolve 解析后仍在子树内）。"""
        ws_source = tmp_path / "project"
        ws_source.mkdir()
        subdir = ws_source / "subdir"
        subdir.mkdir()
        target_file = subdir / "data.txt"
        target_file.write_text("ok")

        symlink_path = ws_source / "link-to-subdir"
        try:
            symlink_path.symlink_to(subdir)
        except OSError:
            pytest.skip("symlink creation not supported on this platform")

        workspace = WorkspaceInfo(
            workspace_id="ws-sym-2",
            display_name="sym-test-2",
            mode="local_copy",
            permissions="read_write",
            source_path=str(ws_source),
            git_url=None,
            git_branch=None,
            enabled=True,
        )
        prepared = PreparedWorkspace(
            workspace_id="ws-sym-2",
            mode="local_copy",
            permissions="read_write",
            workspace_root=str(ws_source),
        )
        # symlink 指向 subdir（子树内）→ 应通过
        mounts = build_container_mounts(
            workspace, prepared,
            extra_volumes=[{"host": str(symlink_path), "container": "/data"}],
        )
        assert len(mounts) == 2  # workspace + extra_volume


# ============================================================
# 加固点 6b：assert_within_workspace 中 NUL/UNC 防护
# ============================================================

class TestAssertWithinWorkspaceHardening:
    """P0.18.2 加固：assert_within_workspace 也走 _validate_raw_path。"""

    def test_nul_byte_in_path_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(PathSecurityError, match="NUL byte"):
            assert_within_workspace(str(ws) + "/safe\x00.txt", str(ws), "read_write")

    def test_nul_byte_in_workspace_root_rejected(self, tmp_path):
        """workspace_root 含 NUL 也应拒绝（防止 caller 注入恶意 root）。"""
        with pytest.raises(PathSecurityError, match="NUL byte"):
            assert_within_workspace("/tmp/safe", "/tmp/ws\x00/evil", "read_write")


# ============================================================
# 既有测试兼容性
# ============================================================

def test_existing_safe_paths_still_work(tmp_path):
    """加固后不应破坏既有合法路径使用。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target = ws / "subdir" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("test")
    # 合法路径仍然通过
    assert_within_workspace(str(target), str(ws), "read_write")
    assert_within_workspace(str(target), str(ws), "read_only", is_write_op=False)