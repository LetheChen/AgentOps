"""ctypes 进程内 folder picker 测试（Windows-only，对齐 deepseek in-process 直调）。

1. COM 装配冒烟：CoInitializeEx + CoCreateInstance + SetOptions/SetTitle + Release
   全链路（不调 Show），验证 GUID / vtable 槽位正确。
2. 端到端超时路径：真实弹出对话框 → 无人操作 → watchdog EnumThreadWindows+WM_CLOSE
   关闭 → 返回 timed out。证明 Show 槽位、closer、清理全部工作。
3. 探测缓存：platform_supports_native_picker 毫秒级返回（不再 spawn powershell）。
"""
from __future__ import annotations

import ctypes
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only ctypes path")

from api.folder_picker import (  # noqa: E402
    _CLSCTX_INPROC_SERVER,
    _CLSID_FILE_OPEN_DIALOG,
    _COINIT_APARTMENTTHREADED,
    _FOS_FOLDER_OPTIONS,
    _GUID,
    _IID_IFILE_OPEN_DIALOG,
    _SLOT_RELEASE,
    _SLOT_SET_OPTIONS,
    _SLOT_SET_TITLE,
    _com_method,
    _guid,
    _pick_win32_inprocess,
    platform_supports_native_picker,
)


def test_com_assembly_smoke():
    """COM 装配全链路（无 Show）：GUID、vtable 槽位（9/17/2）、Release 正确。

    槽位若错，SetOptions/SetTitle 会返回垃圾 HRESULT 或直接崩溃。
    """
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.restype = ctypes.c_long
    hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    assert hr in (0, 1)  # S_OK / S_FALSE
    try:
        ole32.CoCreateInstance.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        dlg = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_FILE_OPEN_DIALOG)), None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(_IID_IFILE_OPEN_DIALOG)), ctypes.byref(dlg),
        )
        assert hr == 0, f"CoCreateInstance hr=0x{hr & 0xFFFFFFFF:08X}"
        assert dlg.value

        dlg_ptr = dlg.value
        # SetOptions 槽位 9
        set_options = _com_method(dlg_ptr, _SLOT_SET_OPTIONS, ctypes.c_long, ctypes.c_uint32)
        assert set_options(dlg_ptr, _FOS_FOLDER_OPTIONS) == 0
        # SetTitle 槽位 17
        set_title = _com_method(dlg_ptr, _SLOT_SET_TITLE, ctypes.c_long, ctypes.c_wchar_p)
        assert set_title(dlg_ptr, "smoke") == 0
        # Release 槽位 2
        release = _com_method(dlg_ptr, _SLOT_RELEASE, ctypes.c_ulong)
        assert release(dlg_ptr) >= 0
    finally:
        ole32.CoUninitialize()


def test_inprocess_dialog_timeout_closes():
    """端到端超时：真实弹对话框 → watchdog WM_CLOSE 关闭 → timed out 错误。

    注意：本测试会在桌面闪现一个对话框窗口约 2 秒（预期行为）。
    """
    t0 = time.perf_counter()
    result = _pick_win32_inprocess(None, timeout_sec=2.0)
    elapsed = time.perf_counter() - t0

    assert result is not None
    assert result.error is not None and "timed out" in result.error
    assert not result.cancelled and result.path is None
    # 确实等了 ~2s（watchdog 触发而非立即失败）
    assert elapsed >= 1.8, f"elapsed={elapsed:.2f}s — 对话框未真正 Show"


def test_probe_is_instant_and_true():
    """探测缓存：不再 spawn powershell（原实现每次 ~1s）。"""
    t0 = time.perf_counter()
    assert platform_supports_native_picker() is True
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"probe took {elapsed * 1000:.0f}ms — 疑似又走了子进程探测"
