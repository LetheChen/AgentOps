"""Native OS folder picker — 跨平台原生文件夹选择对话框。

设计依据：对齐 E:\\GitHub\\deepseek-harness\\packages\\host\\directory-picker-native
的 native backend（win32-dialog-bindings.ts）：**进程内 FFI 直调 IFileOpenDialog
COM vtable**，不 spawn 子进程、不现场编译，对话框 ~100-300ms 出现。

Windows 两级实现（快路径 → 兜底）：
1. **ctypes 进程内直调**（对齐 deepseek 的 koffi 方案）：CoInitializeEx(STA)
   → CoCreateInstance(FileOpenDialog) → vtable 调 SetOptions/SetTitle/SetFolder/
   Show/GetResult/GetDisplayName。无子进程开销、无 Add-Type 编译。
   超时由 watchdog 线程 EnumThreadWindows+WM_CLOSE 实现（deepseek closer 语义）。
2. **PowerShell 子进程兜底**（v0.18 原实现）：ctypes 路径环境不可用时回退
   （如线程已被 MTA 初始化）。慢（每次 spawn + Add-Type 编译 2-4s）但兼容。

macOS:   osascript 调用 AppleScript "choose folder"（系统标准对话框）
Linux:   zenity --file-selection --directory（kdialog 兜底）

vtable 槽位（Vista 冻结 ABI，与 deepseek win32-dialog-bindings.ts 一致）：
  IFileOpenDialog: Release=2 / Show=3 / SetOptions=9 / SetFolder=12 /
                   SetTitle=17 / GetResult=20
  IShellItem:      GetDisplayName=5

注意：对话框在 host 屏幕弹出（不是浏览器屏幕），所以后端必须运行在用户本地
机器上（v0.18+ 默认部署）。如果后端是远程 SSH/容器部署，应改用前端的
DirBrowser 组件（基于 /api/runtime/browse-dirs 的 in-app 目录浏览器）。
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 结果数据类
# ============================================================

@dataclass
class PickResult:
    """文件夹选择结果（三态：成功 / 取消 / 错误）。

    字段:
        path: 选中的绝对路径（已 resolve symlink / .. / .），仅 success=True 时有效
        cancelled: 用户取消（点 Cancel / ESC / 关闭对话框）
        error: 错误描述（subprocess 失败 / 超时 / 平台不支持）
    """
    path: Optional[str] = None
    cancelled: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.path is not None and not self.cancelled and not self.error

    def to_dict(self) -> dict:
        """序列化为 API 响应。"""
        if self.success:
            return {"cancelled": False, "path": self.path}
        if self.cancelled:
            return {"cancelled": True, "path": None}
        return {"cancelled": False, "path": None, "error": self.error}


# ============================================================
# 平台分发入口
# ============================================================

# 默认 5 分钟超时 — 用户可能长时间盯着对话框
DEFAULT_TIMEOUT_SEC = 300


def pick_folder(
    initial_dir: str | None = None,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> PickResult:
    """同步入口：弹出原生文件夹选择对话框，返回 PickResult。

    参数:
        initial_dir: 对话框初始打开的目录（None = 系统上次记住的位置）
        timeout_sec: 子进程超时（秒），超时后强制 kill

    返回:
        PickResult：
          - success: path 为绝对路径
          - cancelled: 用户取消
          - error: 平台不支持 / subprocess 失败 / 超时

    注意: 同步版本会阻塞当前线程。Web handler 请用 pick_folder_async。
    """
    if sys.platform == "win32":
        return _pick_win32(initial_dir, timeout_sec)
    if sys.platform == "darwin":
        return _pick_macos(initial_dir, timeout_sec)
    if sys.platform.startswith("linux"):
        return _pick_linux(initial_dir, timeout_sec)
    return PickResult(error=f"native folder picker not supported on platform: {sys.platform}")


async def pick_folder_async(
    initial_dir: str | None = None,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> PickResult:
    """异步入口：在线程池中跑同步实现，不阻塞 event loop。"""
    return await asyncio.to_thread(pick_folder, initial_dir, timeout_sec=timeout_sec)


# ============================================================
# Windows 快路径：ctypes 进程内直调 IFileOpenDialog COM
# （对齐 deepseek-harness win32-dialog-bindings.ts 的 koffi in-process 方案）
# ============================================================

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
# FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
_FOS_FOLDER_OPTIONS = 0x20 | 0x40 | 0x800
_WM_CLOSE = 0x10
# COM 常量转 int32 有符号（vtable 参数按 int 传）
_SIGDN_FILESYSPATH = 0x80058000 - (1 << 32)
_HRESULT_ERROR_CANCELLED = 0x800704C7 - (1 << 32)
_RPC_E_CHANGED_MODE = 0x80010106 - (1 << 32)

# vtable 槽位（Vista 冻结 ABI；与 deepseek win32-dialog-bindings.ts 及
# 本模块 PowerShell 版 C# 接口声明三方互相印证）
_SLOT_RELEASE = 2
_SLOT_SHOW = 3
_SLOT_SET_OPTIONS = 9
_SLOT_SET_FOLDER = 12
_SLOT_SET_TITLE = 17
_SLOT_GET_RESULT = 20
_SLOT_GET_DISPLAY_NAME = 5  # IShellItem


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    """'xxxxxxxx-xxxx-...' → Windows GUID 内存布局（小端 + 原始尾字节）。"""
    import uuid as _uuid
    u = _uuid.UUID(text)
    g = _GUID()
    g.Data1 = u.time_low
    g.Data2 = u.time_mid
    g.Data3 = u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


_CLSID_FILE_OPEN_DIALOG = "DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7"
_IID_IFILE_OPEN_DIALOG = "D57C7288-D4AD-4768-BE02-9D969532D960"
_IID_ISHELL_ITEM = "43826D1E-E718-42EE-BC55-A1E261C37BFE"

# EnumThreadWindows 回调：给目标线程的每个顶层窗口投递 WM_CLOSE。
# 模块级定义防止 callback 对象被 GC（deepseek closer 等价物，服务超时 abort）。
_enum_close_cb = None  # 惰性创建（仅 win32）


def _com_method(self_ptr: int, slot: int, restype, *argtypes):
    """绑定 COM 对象 vtable 第 slot 个方法为可调用。

    self_ptr: COM 接口指针（整型地址）
    vtable 布局：*(void***)self → vtable；vtable[slot] → 函数指针。
    """
    from ctypes import WINFUNCTYPE
    vtable = ctypes.cast(self_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
    return WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn_ptr)


def _pick_win32_inprocess(initial_dir: str | None, timeout_sec: float) -> Optional[PickResult]:
    """ctypes 进程内直调 IFileOpenDialog（快路径）。

    返回 None = 本路径环境不可用（调用方回退 PowerShell）：
      - 线程已被 MTA 初始化（RPC_E_CHANGED_MODE）
      - COM 装配阶段任何异常
    返回 PickResult = 真实结果（成功 / 取消 / 错误），不再回退。

    线程要求：调用线程做 CoInitializeEx(STA)（asyncio.to_thread 的池化线程
    满足；结束时 CoUninitialize 归零，线程可复用）。
    """
    import ctypes
    from ctypes import wintypes

    t0 = time.perf_counter()
    initialized_com = False
    dlg_ptr = 0
    psi_folder = 0
    psi_result = 0
    try:
        ole32 = ctypes.WinDLL("ole32")
        shell32 = ctypes.WinDLL("shell32")
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")

        # --- COM STA 初始化（对话框必须运行在 STA 线程） ---
        ole32.CoInitializeEx.restype = ctypes.c_long  # HRESULT
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        if hr == _RPC_E_CHANGED_MODE:
            # 线程已被 MTA 占用（罕见）→ 走 PowerShell 兜底
            return None
        if hr not in (0, 1):  # S_OK / S_FALSE
            logger.warning("CoInitializeEx hr=0x%08X，回退 PowerShell", hr & 0xFFFFFFFF)
            return None
        initialized_com = True

        # --- DPI 感知（best-effort，失败忽略 — deepseek 同策略） ---
        try:
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor-v2
        except Exception:
            pass

        # --- CoCreateInstance(FileOpenDialog) ---
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
        if hr != 0 or not dlg:
            logger.warning("CoCreateInstance(FileOpenDialog) hr=0x%08X，回退 PowerShell", hr & 0xFFFFFFFF)
            return None
        dlg_ptr = dlg.value or 0

        # --- SetOptions(FOS_PICKFOLDERS|FORCEFILESYSTEM|PATHMUSTEXIST) ---
        set_options = _com_method(dlg_ptr, _SLOT_SET_OPTIONS, ctypes.c_long, ctypes.c_uint32)
        hr = set_options(dlg_ptr, _FOS_FOLDER_OPTIONS)
        if hr != 0:
            return PickResult(error=f"SetOptions failed hr=0x{hr & 0xFFFFFFFF:08X}")

        # --- SetTitle ---
        set_title = _com_method(dlg_ptr, _SLOT_SET_TITLE, ctypes.c_long, ctypes.c_wchar_p)
        set_title(dlg_ptr, "选择项目工作区目录")  # 失败无伤大雅，不检查 hr

        # --- SetFolder（初始目录，可选） ---
        if initial_dir:
            p = Path(initial_dir)
            if p.is_dir():
                shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
                shell32.SHCreateItemFromParsingName.argtypes = [
                    ctypes.c_wchar_p, ctypes.c_void_p,
                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
                ]
                psi = ctypes.c_void_p()
                hr = shell32.SHCreateItemFromParsingName(
                    str(p.resolve()), None,
                    ctypes.byref(_guid(_IID_ISHELL_ITEM)), ctypes.byref(psi),
                )
                if hr == 0 and psi:
                    psi_folder = psi.value or 0
                    set_folder = _com_method(dlg_ptr, _SLOT_SET_FOLDER, ctypes.c_long, ctypes.c_void_p)
                    set_folder(dlg_ptr, psi_folder)

        logger.info(
            "folder picker COM 装配完成 %.0fms（无子进程/无编译）",
            (time.perf_counter() - t0) * 1000,
        )

        # --- watchdog：超时后向对话框线程所有窗口投递 WM_CLOSE（deepseek closer） ---
        kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        thread_id = kernel32.GetCurrentThreadId()

        global _enum_close_cb

        def _make_closer():
            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _close_hwnd(hwnd, _lparam):
                user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                return 1
            return _close_hwnd

        _enum_close_cb = _make_closer()
        dialog_done = threading.Event()
        timed_out = threading.Event()

        def _watchdog():
            if dialog_done.wait(timeout_sec):
                return
            timed_out.set()
            user32.EnumThreadWindows(thread_id, _enum_close_cb, 0)

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        # --- Show（阻塞至用户关闭对话框） ---
        show = _com_method(dlg_ptr, _SLOT_SHOW, ctypes.c_long, ctypes.c_void_p)
        try:
            hr = show(dlg_ptr, None)
        finally:
            dialog_done.set()

        if timed_out.is_set():
            return PickResult(error=f"folder picker timed out after {timeout_sec}s")
        if hr == _HRESULT_ERROR_CANCELLED:
            return PickResult(cancelled=True)
        if hr != 0:
            return PickResult(error=f"dialog Show failed hr=0x{hr & 0xFFFFFFFF:08X}")

        # --- GetResult → IShellItem → GetDisplayName(SIGDN_FILESYSPATH) ---
        get_result = _com_method(dlg_ptr, _SLOT_GET_RESULT, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
        item = ctypes.c_void_p()
        hr = get_result(dlg_ptr, ctypes.byref(item))
        if hr != 0 or not item:
            return PickResult(error=f"GetResult failed hr=0x{hr & 0xFFFFFFFF:08X}")
        psi_result = item.value or 0

        get_display_name = _com_method(
            psi_result, _SLOT_GET_DISPLAY_NAME,
            ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
        )
        name_ptr = ctypes.c_void_p()
        hr = get_display_name(psi_result, _SIGDN_FILESYSPATH, ctypes.byref(name_ptr))
        if hr != 0 or not name_ptr:
            return PickResult(error=f"GetDisplayName failed hr=0x{hr & 0xFFFFFFFF:08X}")

        selected = ctypes.wstring_at(name_ptr.value)
        ole32.CoTaskMemFree.restype = None
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree(name_ptr)

        # 路径验证（与 PowerShell 路径一致）
        try:
            resolved = str(Path(selected).resolve())
        except (OSError, ValueError) as e:
            return PickResult(error=f"selected path invalid: {selected!r} ({e})")
        if not Path(resolved).is_dir():
            return PickResult(error=f"selected path is not a directory: {resolved}")

        logger.info(
            "folder picker 完成：总交互 %.1fs（选 %s）",
            time.perf_counter() - t0, resolved,
        )
        return PickResult(path=resolved)

    except Exception as e:
        # 装配/执行异常 → 不确定状态，交由 PowerShell 兜底重试
        logger.warning("ctypes 进程内 folder picker 异常，回退 PowerShell: %s", e)
        return None
    finally:
        # 逆序释放（Release 槽位 2）
        try:
            if psi_result:
                release = _com_method(psi_result, _SLOT_RELEASE, ctypes.c_ulong)
                release(psi_result)
            if psi_folder:
                release = _com_method(psi_folder, _SLOT_RELEASE, ctypes.c_ulong)
                release(psi_folder)
            if dlg_ptr:
                release = _com_method(dlg_ptr, _SLOT_RELEASE, ctypes.c_ulong)
                release(dlg_ptr)
            if initialized_com:
                ole32.CoUninitialize()
        except Exception as e:
            logger.warning("COM 清理异常（忽略）: %s", e)


def _pick_win32(initial_dir: str | None, timeout_sec: float) -> PickResult:
    """Windows 分发：ctypes 进程内直调（快）→ PowerShell 子进程（兜底）。"""
    result = _pick_win32_inprocess(initial_dir, timeout_sec)
    if result is not None:
        return result
    return _pick_win32_powershell(initial_dir, timeout_sec)


# ============================================================
# Windows 兜底：PowerShell + IFileOpenDialog COM（子进程）
# ============================================================

# PowerShell 脚本要点（与 deepseek koffi 路径等价）：
# 1. C# Add-Type 声明 IFileOpenDialog COM 接口（vtable 顺序严格匹配）
# 2. FOS_PICKFOLDERS = 0x20 把 OpenFileDialog 切换为"选文件夹"模式
# 3. IShellItem.GetDisplayName(SIGDN_FILESYSPATH) 拿绝对路径
# 4. 用户取消抛 COMException HRESULT=0x800704C7 (ERROR_CANCELLED)
#
# 已实测：HKLM\SOFTWARE\Classes\CLSID\{DC1C5A9C-E88A-...} 注册在
# C:\Windows\System32\comdlg32.dll，Add-Type 编译通过，弹出现代文件夹选择器。
_WIN32_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    # 类型已编译过则跳过 Add-Type（节省 1-2s 编译时间）
    if (-not ('FolderPickerCs' -as [type])) {
        $source = @"
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
internal class FileOpenDialogRcw { }

[ComImport, Guid("D57C7288-D4AD-4768-BE02-9D969532D960"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IFileOpenDialogRcw {
    [PreserveSig] int Show(IntPtr hwndOwner);
    [PreserveSig] int SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
    [PreserveSig] int SetFileTypeIndex(uint iFileType);
    [PreserveSig] int GetFileTypeIndex(out uint piFileType);
    [PreserveSig] int Advise(IntPtr pfde, out uint pdwCookie);
    [PreserveSig] int Unadvise(uint dwCookie);
    [PreserveSig] int SetOptions(uint fos);
    [PreserveSig] int GetOptions(out uint pfos);
    [PreserveSig] int SetDefaultFolder(IntPtr psi);
    [PreserveSig] int SetFolder(IntPtr psi);
    [PreserveSig] int GetFolder(out IntPtr ppsi);
    [PreserveSig] int GetCurrentSelection(out IntPtr ppsi);
    [PreserveSig] int SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    [PreserveSig] int GetFileName(out IntPtr pszName);
    [PreserveSig] int SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
    [PreserveSig] int SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
    [PreserveSig] int SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
    [PreserveSig] int GetResult(out IntPtr ppsi);
    [PreserveSig] int AddPlace(IntPtr psi, int fdap);
    [PreserveSig] int SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
    [PreserveSig] int Close(int hr);
    [PreserveSig] int SetClientGuid(ref Guid guid);
    [PreserveSig] int ClearClientData();
    [PreserveSig] int SetFilter(IntPtr pFilter);
}

[ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IShellItemRcw {
    [PreserveSig] int BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    [PreserveSig] int GetParent(out IntPtr ppsi);
    [PreserveSig] int GetDisplayName(uint sigdnName, out IntPtr ppszName);
    [PreserveSig] int GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
    [PreserveSig] int Compare(IntPtr psi, uint hint, out int piOrder);
}

public static class FolderPickerCs {
    public const uint FOS_PICKFOLDERS = 0x00000020;
    public const uint SIGDN_FILESYSPATH = 0x80058000;
    // ERROR_CANCELLED: 用户点取消 / ESC / 关闭对话框
    public const uint HRESULT_ERROR_CANCELLED = 0x800704C7;

    public static string Pick(string title, string initialDir) {
        FileOpenDialogRcw dlg = new FileOpenDialogRcw();
        IFileOpenDialogRcw fd = (IFileOpenDialogRcw)dlg;
        int hr = fd.SetOptions(FOS_PICKFOLDERS);
        if (hr != 0) throw new Exception("SetOptions failed hr=0x" + hr.ToString("X"));
        hr = fd.SetTitle(title);
        if (hr != 0) throw new Exception("SetTitle failed hr=0x" + hr.ToString("X"));

        // 设置初始目录（如果存在且为目录）
        if (!string.IsNullOrEmpty(initialDir) && System.IO.Directory.Exists(initialDir)) {
            IntPtr psi = IntPtr.Zero;
            int sfn = SHCreateItemFromParsingName(initialDir, IntPtr.Zero, typeof(IShellItemRcw).GUID, out psi);
            if (sfn == 0 && psi != IntPtr.Zero) {
                fd.SetFolder(psi);
                System.Runtime.InteropServices.Marshal.Release(psi);
            }
        }

        // **重要修复**：hr=0x800704C7 (ERROR_CANCELLED) 即使用户点取消，也可能
        // 作为正常返回码（非异常路径）。必须在这里识别为取消，不能走 __ERROR__ 分支
        try {
            hr = fd.Show(IntPtr.Zero);
        } catch (System.Runtime.InteropServices.COMException ex) {
            if ((uint)ex.ErrorCode == HRESULT_ERROR_CANCELLED) return "__CANCELLED__";
            throw;
        }
        if ((uint)hr == HRESULT_ERROR_CANCELLED) return "__CANCELLED__";
        if (hr != 0) return "__ERROR__:Show hr=0x" + hr.ToString("X");

        IntPtr psiResult = IntPtr.Zero;
        hr = fd.GetResult(out psiResult);
        if (hr != 0 || psiResult == IntPtr.Zero) return "__ERROR__:GetResult hr=0x" + hr.ToString("X");
        IShellItemRcw si = (IShellItemRcw)System.Runtime.InteropServices.Marshal.GetTypedObjectForIUnknown(psiResult, typeof(IShellItemRcw));
        IntPtr pszPath;
        si.GetDisplayName(SIGDN_FILESYSPATH, out pszPath);
        string path = System.Runtime.InteropServices.Marshal.PtrToStringUni(pszPath);
        System.Runtime.InteropServices.Marshal.FreeCoTaskMem(pszPath);
        System.Runtime.InteropServices.Marshal.Release(psiResult);
        return path ?? "";
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, PreserveSig = true)]
    private static extern int SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
        IntPtr pbc,
        [MarshalAs(UnmanagedType.LPStruct)] Guid iIdIShellItem,
        out IntPtr ppv);
}
"@
        Add-Type -TypeDefinition $source -Language CSharp
    }

    # initialDir 由调用方通过 -initialDir: 命名参数传入（无需 $args[0] 转义）
    $path = [FolderPickerCs]::Pick("选择项目工作区目录", $initialDir)
    if ($path.StartsWith("__CANCELLED__")) { exit 2 }
    if ($path.StartsWith("__ERROR__:")) { Write-Error $path; exit 1 }
    # 最后一行 = 路径
    Write-Output $path
    exit 0
} catch {
    Write-Error $_.ToString()
    exit 1
}
"""


def _pick_win32_powershell(initial_dir: str | None, timeout_sec: float) -> PickResult:
    """兜底路径：PowerShell 子进程加载 IFileOpenDialog COM。

    慢（每次 spawn powershell + Add-Type 现场编译 C#，实测 2-4s），仅在
    ctypes 进程内路径环境不可用时使用。

    退出码约定:
        0 = 成功（stdout 最后一行为路径）
        2 = 用户取消
        1 = 其他错误（stderr 含错误信息）

    实现注意：脚本含 C# here-string、UTF-8 中文标题、`$_.ToString()` 等特殊
    语法，通过 `powershell -Command "<大段脚本>"` 传递时会被外层 PowerShell
    误解析（实测表现为 exit 1 + stderr 输出整段脚本）。改为写入临时 .ps1
    文件、用 `-File` 执行，规避所有 quoting 问题。
    """
    # 验证 initial_dir 存在（不存在就传空字符串让 PowerShell 跳过 SetFolder）
    init_arg = ""
    if initial_dir:
        p = Path(initial_dir)
        if p.is_dir():
            init_arg = str(p.resolve())

    # 把脚本写入临时文件（UTF-8 BOM —— 关键：PowerShell 5.1 默认按系统编码
    # 读 .ps1，BOM 是唯一让它识别 UTF-8 的可靠信号；不带 BOM 会把中文标题
    # "选择项目工作区目录" 解析成乱码，导致引号被破坏、整个脚本报语法错误）
    # initialDir 通过 -initialDir: 命名参数传入，规避中文路径转义
    ps_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ps1",
            prefix="agentops_folder_picker_",
            delete=False,
            encoding="utf-8-sig",  # UTF-8 with BOM
        ) as f:
            ps_file = f.name
            f.write(_WIN32_PS_SCRIPT)

        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            # **关键**：IFileOpenDialog COM 是 STA 组件，PowerShell 5.1 默认 MTA
            # 会导致 dialog 渲染严重延迟（实测 5-10s）。强制 -STA 后 < 500ms
            "-STA",
            "-File", ps_file,
            f"-initialDir:{init_arg}",
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return PickResult(error=f"folder picker timed out after {timeout_sec}s")
        except FileNotFoundError:
            return PickResult(error="powershell not found in PATH (required on Windows)")
    finally:
        if ps_file and os.path.exists(ps_file):
            try:
                os.unlink(ps_file)
            except OSError:
                pass

    if completed.returncode == 2:
        return PickResult(cancelled=True)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() if completed.stderr else ""
        return PickResult(error=f"folder picker failed (exit {completed.returncode}): {stderr[:500]}")

    # 解析最后一行非空输出
    stdout = completed.stdout.strip()
    if not stdout:
        return PickResult(cancelled=True)  # 防御性：空输出按取消处理

    # PowerShell 有时会输出多行（Write-Verbose 等噪声），取最后一行
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return PickResult(cancelled=True)
    selected = lines[-1]

    # 验证路径真实存在（防止 COM 返回已删除路径）
    try:
        resolved = str(Path(selected).resolve())
    except (OSError, ValueError) as e:
        return PickResult(error=f"selected path invalid: {selected!r} ({e})")
    if not Path(resolved).is_dir():
        return PickResult(error=f"selected path is not a directory: {resolved}")

    return PickResult(path=resolved)


# ============================================================
# macOS: osascript
# ============================================================

_MACOS_TITLE = "选择项目工作区目录"


def _pick_macos(initial_dir: str | None, timeout_sec: float) -> PickResult:
    """macOS: osascript 调用 AppleScript choose folder。

    -e 'choose folder with prompt "..."' 弹出系统标准文件夹选择器
    -e 'POSIX path of ...'  把 AppleScript alias 转成 POSIX 绝对路径

    错误码:
        0 = 成功（stdout 为路径）
        非 0 = 取消 (-128) 或其他错误
    """
    title_escaped = _MACOS_TITLE.replace('"', '\\"')
    script_lines = []
    if initial_dir:
        init_escaped = initial_dir.replace('"', '\\"')
        script_lines.append(f'set defaultLocation to POSIX file "{init_escaped}"')
    script_lines.append(f'set selectedFolder to choose folder with prompt "{title_escaped}"')
    script_lines.append('POSIX path of selectedFolder')

    try:
        completed = subprocess.run(
            ["osascript", "-e", script_lines[0]] +
            sum([["-e", s] for s in script_lines[1:]], []),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PickResult(error=f"folder picker timed out after {timeout_sec}s")
    except FileNotFoundError:
        return PickResult(error="osascript not found (required on macOS)")

    if completed.returncode != 0:
        stderr = completed.stderr.strip() if completed.stderr else ""
        # AppleScript 取消 = exit -128 (实际是 1) with stderr "User canceled. (-128)"
        if "User canceled" in stderr or "-128" in stderr or completed.returncode == 1:
            return PickResult(cancelled=True)
        return PickResult(error=f"folder picker failed (exit {completed.returncode}): {stderr[:500]}")

    selected = completed.stdout.strip()
    if not selected:
        return PickResult(cancelled=True)
    try:
        resolved = str(Path(selected).resolve())
    except (OSError, ValueError) as e:
        return PickResult(error=f"selected path invalid: {selected!r} ({e})")
    if not Path(resolved).is_dir():
        return PickResult(error=f"selected path is not a directory: {resolved}")
    return PickResult(path=resolved)


# ============================================================
# Linux: zenity / kdialog
# ============================================================

_LINUX_TITLE = "选择项目工作区目录"


def _pick_linux(initial_dir: str | None, timeout_sec: float) -> PickResult:
    """Linux: 优先 zenity，kdialog 兜底。

    zenity --file-selection --directory 让其切换为"选目录"模式
    取消退出码 1 (zenity) / 1 (kdialog)
    """
    # 先试 zenity
    result = _run_zenity(initial_dir, timeout_sec)
    if result.error and ("no such file" in result.error.lower() or "not found" in result.error.lower()):
        # zenity 不存在，兜底 kdialog
        return _run_kdialog(initial_dir, timeout_sec)
    return result


def _run_zenity(initial_dir: str | None, timeout_sec: float) -> PickResult:
    args = ["zenity", "--file-selection", "--directory", f"--title={_LINUX_TITLE}"]
    if initial_dir and Path(initial_dir).is_dir():
        args.append(f"--filename={initial_dir}/")
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PickResult(error=f"zenity timed out after {timeout_sec}s")
    except FileNotFoundError:
        return PickResult(error="zenity not found in PATH")

    if completed.returncode == 1:
        return PickResult(cancelled=True)
    if completed.returncode != 0:
        return PickResult(error=f"zenity failed (exit {completed.returncode}): {(completed.stderr or '').strip()[:500]}")

    selected = completed.stdout.strip()
    if not selected:
        return PickResult(cancelled=True)
    try:
        resolved = str(Path(selected).resolve())
    except (OSError, ValueError) as e:
        return PickResult(error=f"selected path invalid: {selected!r} ({e})")
    if not Path(resolved).is_dir():
        return PickResult(error=f"selected path is not a directory: {resolved}")
    return PickResult(path=resolved)


def _run_kdialog(initial_dir: str | None, timeout_sec: float) -> PickResult:
    args = ["kdialog", "--getexistingdirectory", ".", "--title", _LINUX_TITLE]
    if initial_dir and Path(initial_dir).is_dir():
        args[2] = initial_dir
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PickResult(error=f"kdialog timed out after {timeout_sec}s")
    except FileNotFoundError:
        return PickResult(error="neither zenity nor kdialog found (install one)")

    if completed.returncode == 1:
        return PickResult(cancelled=True)
    if completed.returncode != 0:
        return PickResult(error=f"kdialog failed (exit {completed.returncode}): {(completed.stderr or '').strip()[:500]}")

    selected = completed.stdout.strip()
    if not selected:
        return PickResult(cancelled=True)
    try:
        resolved = str(Path(selected).resolve())
    except (OSError, ValueError) as e:
        return PickResult(error=f"selected path invalid: {selected!r} ({e})")
    if not Path(resolved).is_dir():
        return PickResult(error=f"selected path is not a directory: {resolved}")
    return PickResult(path=resolved)


# ============================================================
# 工具：探测平台支持（前端可用来决定是否显示"原生选择"按钮）
# ============================================================

# 探测缓存：platform_supports_native_picker 原本每次都 spawn powershell 探测
# （~1s），且 pick-folder 端点每次调用都先探测一次 —— 这是弹窗慢的第二元凶。
_native_picker_supported_cache: bool | None = None


def platform_supports_native_picker() -> bool:
    """前端调用：根据 host 平台决定是否暴露"原生选择"按钮（结果缓存）。

    Windows：直接 True —— ctypes 是 Python 标准库自带（零成本探测）；
    真正的 powershell 可用性检查推迟到 fallback 路径内部（ctypes 失败才走到）。
    macOS / Linux：_which 探测（零成本）。
    """
    global _native_picker_supported_cache
    if _native_picker_supported_cache is not None:
        return _native_picker_supported_cache
    if sys.platform == "win32":
        _native_picker_supported_cache = True
    elif sys.platform == "darwin":
        _native_picker_supported_cache = _which("osascript") is not None
    elif sys.platform.startswith("linux"):
        _native_picker_supported_cache = (
            _which("zenity") is not None or _which("kdialog") is not None
        )
    else:
        _native_picker_supported_cache = False
    return _native_picker_supported_cache


def _which(cmd: str) -> str | None:
    """简易 which 实现：检查 PATH 中是否存在可执行文件。"""
    import shutil
    return shutil.which(cmd)
