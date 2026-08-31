"""folder_picker 模块单元测试。

覆盖：
1. PickResult 三态 + to_dict 序列化（success / cancelled / error）
2. Win32 PowerShell 脚本 payload 包含 IFileOpenDialog COM + FOS_PICKFOLDERS + SetTitle
3. _pick_win32 取消语义（returncode=2 → cancelled=True）
4. _pick_win32 错误语义（非 0/2 + stderr → error）
5. _pick_win32 成功：stdout 最后一行 → resolved 绝对路径
6. _pick_win32 空输出（防御性）→ cancelled
7. _pick_win32 initial_dir 验证（不存在则不传给 powershell）
8. _pick_win32 超时 → error
9. _pick_win32 powershell 不存在 → error
10. _pick_win32 选中非目录 → error
11. _pick_win32 解析失败（OSError）→ error
12. pick_folder 平台分发（mock sys.platform）
13. pick_folder 不支持的平台 → error
14. pick_folder_async 走 asyncio.to_thread（mock 验证）
15. macOS 取消语义（exit 1 + "User canceled" → cancelled）
16. macOS 成功：stdout 路径 → resolved
17. macOS 超时 → error
18. macOS osascript 不存在 → error
19. Linux zenity 取消（exit 1 → cancelled）
20. Linux zenity 成功 → resolved
21. Linux zenity 不存在 + kdialog 存在 → 兜底 kdialog
22. Linux zenity 不存在 + kdialog 不存在 → error
23. Linux kdialog 取消 → cancelled
24. Linux kdialog 成功 → resolved
25. platform_supports_native_picker per-platform detection
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import folder_picker
from api.folder_picker import (
    PickResult,
    _WIN32_PS_SCRIPT,
    pick_folder,
    pick_folder_async,
    platform_supports_native_picker,
)


# ============================================================
# PickResult 三态
# ============================================================

class TestPickResult:
    def test_success_implies_path_set(self):
        r = PickResult(path="C:\\Users\\me\\proj")
        assert r.success is True
        assert r.cancelled is False
        assert r.error is None

    def test_cancelled_implies_no_path(self):
        r = PickResult(cancelled=True)
        assert r.success is False
        assert r.cancelled is True

    def test_error_implies_no_path(self):
        r = PickResult(error="boom")
        assert r.success is False
        assert r.error == "boom"

    def test_to_dict_success(self):
        r = PickResult(path="C:\\Users\\me\\proj")
        d = r.to_dict()
        assert d == {"cancelled": False, "path": "C:\\Users\\me\\proj"}

    def test_to_dict_cancelled(self):
        r = PickResult(cancelled=True)
        d = r.to_dict()
        assert d == {"cancelled": True, "path": None}

    def test_to_dict_error(self):
        r = PickResult(error="powershell not found")
        d = r.to_dict()
        assert d == {"cancelled": False, "path": None, "error": "powershell not found"}

    def test_cancelled_disables_success_even_with_path(self):
        """path 存在但 cancelled=True → success=False（cancelled 优先级最高）。"""
        r = PickResult(path="/tmp/x", cancelled=True)
        # 设计语义：cancelled 视为用户主动取消，success 必须为 False
        # （即便 path 被设置过，也视为无效）
        assert r.success is False
        assert r.cancelled is True
        # to_dict 走 cancelled 分支
        d = r.to_dict()
        assert d["cancelled"] is True
        assert d["path"] is None


# ============================================================
# Win32 PowerShell 脚本 payload
# ============================================================

class TestWin32PowerShellScript:
    def test_contains_ifileopendialog_com_guid(self):
        """IFileOpenDialog COM 类 GUID（DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7）必须在脚本中。"""
        assert "DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7" in _WIN32_PS_SCRIPT

    def test_contains_ishellitem_com_guid(self):
        """IShellItem COM 接口 GUID 必须在脚本中。"""
        assert "43826D1E-E718-42EE-BC55-A1E261C37BFE" in _WIN32_PS_SCRIPT

    def test_contains_fos_pickfolders_flag(self):
        """FOS_PICKFOLDERS = 0x20（把 OpenFileDialog 切到选文件夹模式）必须定义。"""
        assert "FOS_PICKFOLDERS" in _WIN32_PS_SCRIPT
        assert "0x00000020" in _WIN32_PS_SCRIPT

    def test_contains_sigdn_filesyspath(self):
        """SIGDN_FILESYSPATH = 0x80058000（拿绝对路径）必须定义。"""
        assert "SIGDN_FILESYSPATH" in _WIN32_PS_SCRIPT
        assert "0x80058000" in _WIN32_PS_SCRIPT

    def test_contains_settitle_call(self):
        """SetTitle 必须被调用以设置对话框标题。"""
        assert "fd.SetTitle" in _WIN32_PS_SCRIPT

    def test_contains_setoptions_call(self):
        """SetOptions 必须被调用以切换到 PICKFOLDERS 模式。"""
        assert "fd.SetOptions" in _WIN32_PS_SCRIPT

    def test_contains_cancellation_handling(self):
        """用户取消（COMException 0x800704C7）必须被识别为取消。"""
        assert "0x800704C7" in _WIN32_PS_SCRIPT
        assert "__CANCELLED__" in _WIN32_PS_SCRIPT

    def test_contains_initial_dir_support(self):
        """initialDir 参数必须被处理（SetFolder）。"""
        assert "SetFolder" in _WIN32_PS_SCRIPT
        assert "SHCreateItemFromParsingName" in _WIN32_PS_SCRIPT

    def test_uses_unicode_string_marshalling(self):
        """string 必须用 LPWStr 封送（Windows COM 标准）。"""
        assert "LPWStr" in _WIN32_PS_SCRIPT

    def test_add_type_compiles_c_sharp(self):
        """必须用 Add-Type -Language CSharp 编译内嵌 C# 代码。"""
        assert "Add-Type" in _WIN32_PS_SCRIPT
        assert "CSharp" in _WIN32_PS_SCRIPT

    def test_exit_code_2_for_cancel(self):
        """用户取消对应 exit 2。"""
        assert "exit 2" in _WIN32_PS_SCRIPT

    def test_exit_code_1_for_error(self):
        """其他错误对应 exit 1。"""
        assert "exit 1" in _WIN32_PS_SCRIPT


# ============================================================
# _pick_win32 行为（mock subprocess.run）
# ============================================================

def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    """构造 subprocess.CompletedProcess 替身。"""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


class TestPickWin32:
    def test_cancelled_returncode_2(self, tmp_path):
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=2)
            result = folder_picker._pick_win32(None, 30.0)
        assert result.cancelled is True
        assert result.path is None
        assert result.error is None
        assert result.success is False

    def test_success_returns_resolved_path(self, tmp_path):
        target = tmp_path / "my-project"
        target.mkdir()
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout=str(target))
            result = folder_picker._pick_win32(None, 30.0)
        assert result.success is True
        assert result.cancelled is False
        assert result.error is None
        # 路径会被 Path.resolve() 解析（处理 .. / symlink）
        assert Path(result.path).resolve() == target.resolve()
        assert Path(result.path).is_dir()

    def test_success_takes_last_nonempty_line(self, tmp_path):
        """PowerShell 可能输出多行噪声，取最后一行非空。"""
        target = tmp_path / "proj"
        target.mkdir()
        # 模拟 verbose 输出 + 末行路径
        stdout = f"VERBOSE: performing operation\n{str(target)}\n"
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout=stdout)
            result = folder_picker._pick_win32(None, 30.0)
        assert result.success is True
        assert Path(result.path).resolve() == target.resolve()

    def test_empty_stdout_defensively_treated_as_cancel(self):
        """空 stdout → 防御性按取消处理。"""
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout="")
            result = folder_picker._pick_win32(None, 30.0)
        assert result.cancelled is True
        assert result.path is None

    def test_whitespace_only_stdout_treated_as_cancel(self):
        """只有空白 → 取消。"""
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout="   \n  \n")
            result = folder_picker._pick_win32(None, 30.0)
        assert result.cancelled is True

    def test_non_zero_returncode_propagates_stderr_as_error(self):
        """非 0/2 退出码 + stderr → error。"""
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(
                returncode=1,
                stderr="Add-Type: csc.exe not found",
            )
            result = folder_picker._pick_win32(None, 30.0)
        assert result.cancelled is False
        assert result.success is False
        assert result.error is not None
        assert "exit 1" in result.error
        assert "Add-Type: csc.exe not found" in result.error

    def test_timeout_returns_error(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["powershell"], timeout=30),
        ):
            result = folder_picker._pick_win32(None, 30.0)
        assert result.error is not None
        assert "timed out" in result.error
        assert "30" in result.error

    def test_powershell_not_found_returns_error(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=FileNotFoundError("powershell"),
        ):
            result = folder_picker._pick_win32(None, 30.0)
        assert result.error is not None
        assert "powershell not found" in result.error

    def test_selected_path_must_be_directory(self, tmp_path):
        """选中的路径如果不是目录 → error。"""
        # 创建一个文件，COM 不太可能返回文件，但防御性测试
        file = tmp_path / "afile.txt"
        file.write_text("hi")
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout=str(file))
            result = folder_picker._pick_win32(None, 30.0)
        assert result.error is not None
        assert "not a directory" in result.error

    def test_initial_dir_only_passed_if_exists(self, tmp_path):
        """initial_dir 不存在 → 不传给 powershell（传空字符串）。"""
        valid = tmp_path / "valid"
        valid.mkdir()
        invalid = "Z:\\nope\\does-not-exist"

        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=2)
            # 不存在的路径
            folder_picker._pick_win32(invalid, 30.0)
            args_invalid = mock_run.call_args[0][0]
            # 命名参数形式："-initialDir:<value>"，无效路径 → 值部分为空
            init_arg_flag = args_invalid[-1]
            assert init_arg_flag == "-initialDir:"

            # 存在的路径
            folder_picker._pick_win32(str(valid), 30.0)
            args_valid = mock_run.call_args[0][0]
            init_arg_valid = args_valid[-1]
            assert init_arg_valid == f"-initialDir:{Path(str(valid)).resolve()}"

    def test_powershell_invocation_uses_noprofile_noninteractive(self):
        """必须用 -NoProfile -NonInteractive -ExecutionPolicy Bypass -File -STA 避免污染用户环境。

        关键：脚本含 here-string / 中文 / $_.ToString() 等特殊语法，必须用
        临时 .ps1 文件 + -File 执行（不能用 -Command，否则会被外层 PS 误解析）。
        必须用 -STA：IFileOpenDialog 是 STA COM 组件，MTA 模式下 dialog 严重延迟。
        """
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=2)
            folder_picker._pick_win32(None, 30.0)
            cmd = mock_run.call_args[0][0]
        assert "powershell" in cmd
        assert "-NoProfile" in cmd
        assert "-NonInteractive" in cmd
        assert "-ExecutionPolicy" in cmd
        assert "Bypass" in cmd
        assert "-STA" in cmd
        assert "-File" in cmd
        # 找到 -File 后面的位置，应该是 .ps1 临时文件
        file_idx = cmd.index("-File")
        ps_path = cmd[file_idx + 1]
        assert ps_path.endswith(".ps1")


# ============================================================
# pick_folder 平台分发
# ============================================================

class TestPickFolderDispatch:
    def test_win32_dispatch(self):
        with patch.object(folder_picker, "_pick_win32") as mock:
            mock.return_value = PickResult(path="C:\\x")
            with patch.object(folder_picker.sys, "platform", "win32"):
                result = pick_folder()
        mock.assert_called_once()
        assert result.path == "C:\\x"

    def test_macos_dispatch(self):
        with patch.object(folder_picker, "_pick_macos") as mock:
            mock.return_value = PickResult(path="/Users/me/proj")
            with patch.object(folder_picker.sys, "platform", "darwin"):
                result = pick_folder()
        mock.assert_called_once()
        assert result.path == "/Users/me/proj"

    def test_linux_dispatch(self):
        with patch.object(folder_picker, "_pick_linux") as mock:
            mock.return_value = PickResult(path="/home/me/proj")
            with patch.object(folder_picker.sys, "platform", "linux"):
                result = pick_folder()
        mock.assert_called_once()
        assert result.path == "/home/me/proj"

    def test_unsupported_platform(self):
        with patch.object(folder_picker.sys, "platform", "freebsd"):
            result = pick_folder()
        assert result.success is False
        assert result.error is not None
        assert "not supported" in result.error
        assert "freebsd" in result.error


# ============================================================
# pick_folder_async 走 asyncio.to_thread
# ============================================================

class TestPickFolderAsync:
    def test_runs_in_thread_pool(self):
        """async 入口必须通过 asyncio.to_thread 包装（不阻塞 event loop）。"""
        with patch.object(folder_picker.asyncio, "to_thread") as mock_to_thread:
            # to_thread 在真实环境返回 coroutine，await 后拿到结果
            # mock 直接返回结果（绕过 await），简化测试
            mock_to_thread.return_value = PickResult(path="C:\\x")
            # 把 pick_folder_async 包成 coroutine 再 await
            result = asyncio.run(_await_async(pick_folder_async(None, timeout_sec=30.0)))
        assert mock_to_thread.called
        # 确认传给 to_thread 的是 pick_folder 函数
        assert mock_to_thread.call_args[0][0] is pick_folder
        assert result.path == "C:\\x"

    def test_forwards_initial_dir_and_timeout(self):
        with patch.object(folder_picker.asyncio, "to_thread") as mock_to_thread:
            mock_to_thread.return_value = PickResult(path="C:\\x")
            asyncio.run(_await_async(pick_folder_async("D:\\start", timeout_sec=42.0)))
        call_args = mock_to_thread.call_args
        # 第一个位置参数是 fn
        assert call_args[0][0] is pick_folder
        # 第一个 positional after fn 应该是 initial_dir
        assert call_args[0][1] == "D:\\start"
        # timeout_sec 是 keyword
        assert call_args[1].get("timeout_sec") == 42.0


async def _await_async(awaitable):
    """辅助函数：显式 await 一个 awaitable，方便 mock 不需要正确模拟 coroutine。"""
    return await awaitable


# ============================================================
# macOS
# ============================================================

class TestPickMacos:
    def test_user_canceled_treated_as_cancel(self):
        """osascript 取消（exit 非 0 + stderr "User canceled"）→ cancelled。"""
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(
                returncode=1,
                stderr="execution error: User canceled. (-128)",
            )
            result = folder_picker._pick_macos(None, 30.0)
        assert result.cancelled is True
        assert result.path is None

    def test_success_returns_resolved_path(self, tmp_path):
        target = tmp_path / "macproj"
        target.mkdir()
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout=str(target))
            result = folder_picker._pick_macos(None, 30.0)
        assert result.success is True
        assert Path(result.path).resolve() == target.resolve()

    def test_non_cancel_error_propagates(self):
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=99, stderr="some error")
            result = folder_picker._pick_macos(None, 30.0)
        assert result.error is not None
        assert "exit 99" in result.error

    def test_timeout_returns_error(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["osascript"], timeout=30),
        ):
            result = folder_picker._pick_macos(None, 30.0)
        assert result.error is not None
        assert "timed out" in result.error

    def test_osascript_not_found(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=FileNotFoundError("osascript"),
        ):
            result = folder_picker._pick_macos(None, 30.0)
        assert result.error is not None
        assert "osascript not found" in result.error

    def test_empty_stdout_treated_as_cancel(self):
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout="")
            result = folder_picker._pick_macos(None, 30.0)
        assert result.cancelled is True


# ============================================================
# Linux zenity
# ============================================================

class TestPickLinuxZenity:
    def test_zenity_cancel(self):
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=1)
            result = folder_picker._run_zenity(None, 30.0)
        assert result.cancelled is True

    def test_zenity_success(self, tmp_path):
        target = tmp_path / "linproj"
        target.mkdir()
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout=str(target))
            result = folder_picker._run_zenity(None, 30.0)
        assert result.success is True
        assert Path(result.path).resolve() == target.resolve()

    def test_zenity_timeout(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["zenity"], timeout=30),
        ):
            result = folder_picker._run_zenity(None, 30.0)
        assert result.error is not None
        assert "zenity timed out" in result.error

    def test_zenity_not_found_returns_error_with_specific_message(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=FileNotFoundError("zenity"),
        ):
            result = folder_picker._run_zenity(None, 30.0)
        assert result.error is not None
        assert "zenity not found" in result.error

    def test_zenity_passes_directory_flag_and_title(self, tmp_path):
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=1)
            folder_picker._run_zenity(str(tmp_path), 30.0)
            args = mock_run.call_args[0][0]
        assert "zenity" in args
        assert "--file-selection" in args
        assert "--directory" in args
        # 标题参数
        assert any(a.startswith("--title=") for a in args)
        # 初始目录（仅当存在时）
        assert any(a.startswith("--filename=") for a in args)


# ============================================================
# Linux kdialog（兜底）
# ============================================================

class TestPickLinuxKdialog:
    def test_kdialog_cancel(self):
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=1)
            result = folder_picker._run_kdialog(None, 30.0)
        assert result.cancelled is True

    def test_kdialog_success(self, tmp_path):
        target = tmp_path / "kdproj"
        target.mkdir()
        with patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0, stdout=str(target))
            result = folder_picker._run_kdialog(None, 30.0)
        assert result.success is True
        assert Path(result.path).resolve() == target.resolve()

    def test_kdialog_not_found(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=FileNotFoundError("kdialog"),
        ):
            result = folder_picker._run_kdialog(None, 30.0)
        assert result.error is not None
        assert "neither zenity nor kdialog" in result.error

    def test_kdialog_timeout(self):
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["kdialog"], timeout=30),
        ):
            result = folder_picker._run_kdialog(None, 30.0)
        assert result.error is not None
        assert "kdialog timed out" in result.error


# ============================================================
# Linux _pick_linux 兜底逻辑
# ============================================================

class TestPickLinuxFallback:
    def test_zenity_missing_falls_back_to_kdialog(self, tmp_path):
        target = tmp_path / "fb"
        target.mkdir()
        # zenity 报 not found error → 触发 kdialog 兜底
        zenity_result = PickResult(error="zenity not found in PATH")
        kdialog_result = PickResult(path=str(target))
        with patch.object(folder_picker, "_run_zenity", return_value=zenity_result), \
             patch.object(folder_picker, "_run_kdialog", return_value=kdialog_result) as mock_kd:
            result = folder_picker._pick_linux(None, 30.0)
        mock_kd.assert_called_once()
        assert result.success is True

    def test_zenity_present_skips_kdialog(self, tmp_path):
        """zenity 成功时不调用 kdialog。"""
        target = tmp_path / "fb"
        target.mkdir()
        zenity_result = PickResult(path=str(target))
        with patch.object(folder_picker, "_run_zenity", return_value=zenity_result), \
             patch.object(folder_picker, "_run_kdialog") as mock_kd:
            result = folder_picker._pick_linux(None, 30.0)
        mock_kd.assert_not_called()
        assert result.success is True

    def test_zenity_user_cancel_skips_kdialog(self):
        """zenity 用户取消时不调用 kdialog（取消 ≠ not found）。"""
        zenity_result = PickResult(cancelled=True)
        with patch.object(folder_picker, "_run_zenity", return_value=zenity_result), \
             patch.object(folder_picker, "_run_kdialog") as mock_kd:
            result = folder_picker._pick_linux(None, 30.0)
        mock_kd.assert_not_called()
        assert result.cancelled is True


# ============================================================
# platform_supports_native_picker
# ============================================================

class TestPlatformSupport:
    def test_win32_checks_powershell_availability(self):
        with patch.object(folder_picker.sys, "platform", "win32"), \
             patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=0)
            supported = platform_supports_native_picker()
        assert supported is True
        # 验证调用了 powershell
        assert "powershell" in mock_run.call_args[0][0]

    def test_win32_uses_exit_0_sentinel(self):
        """实现用 `powershell -Command exit 0` 作为探活指令（不检查 returncode）。"""
        with patch.object(folder_picker.sys, "platform", "win32"), \
             patch.object(folder_picker.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(returncode=42)  # 即便 returncode 非 0
            supported = platform_supports_native_picker()
        # 实现只检查 subprocess.run 不抛异常，不检查 returncode
        # （exit 0 失败的情况 = powershell 损坏，后续实际 pickFolder 会暴露）
        assert supported is True
        # 验证命令
        cmd = mock_run.call_args[0][0]
        assert "powershell" in cmd
        assert "exit 0" in cmd

    def test_win32_returns_false_if_powershell_not_found(self):
        with patch.object(folder_picker.sys, "platform", "win32"), \
             patch.object(folder_picker.subprocess, "run",
                          side_effect=FileNotFoundError("powershell")):
            supported = platform_supports_native_picker()
        assert supported is False

    def test_win32_returns_false_if_powershell_times_out(self):
        with patch.object(folder_picker.sys, "platform", "win32"), \
             patch.object(folder_picker.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd=["powershell"], timeout=5)):
            supported = platform_supports_native_picker()
        assert supported is False

    def test_macos_checks_osascript(self):
        with patch.object(folder_picker.sys, "platform", "darwin"), \
             patch.object(folder_picker, "_which", return_value="/usr/bin/osascript"):
            supported = platform_supports_native_picker()
        assert supported is True

    def test_macos_returns_false_if_no_osascript(self):
        with patch.object(folder_picker.sys, "platform", "darwin"), \
             patch.object(folder_picker, "_which", return_value=None):
            supported = platform_supports_native_picker()
        assert supported is False

    def test_linux_supports_zenity(self):
        with patch.object(folder_picker.sys, "platform", "linux"), \
             patch.object(folder_picker, "_which", side_effect=lambda c: "/usr/bin/zenity" if c == "zenity" else None):
            supported = platform_supports_native_picker()
        assert supported is True

    def test_linux_supports_kdialog_without_zenity(self):
        with patch.object(folder_picker.sys, "platform", "linux"), \
             patch.object(folder_picker, "_which", side_effect=lambda c: "/usr/bin/kdialog" if c == "kdialog" else None):
            supported = platform_supports_native_picker()
        assert supported is True

    def test_linux_returns_false_without_both(self):
        with patch.object(folder_picker.sys, "platform", "linux"), \
             patch.object(folder_picker, "_which", return_value=None):
            supported = platform_supports_native_picker()
        assert supported is False

    def test_unsupported_platform(self):
        with patch.object(folder_picker.sys, "platform", "freebsd"):
            assert platform_supports_native_picker() is False
