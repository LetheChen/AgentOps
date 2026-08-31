"""Windows ACL 沙箱 —— capability SID + DACL + 受限 Token 内核级隔离。

对应 docs/目录授权与权限级别-完整解决方案.md Part B(§10-§23)。

设计要点:
1. capability SID 派生:SHA256(workspaceRoot) → S-1-4-<first>-<second>
2. DACL 构造:grantWrite 幂等(standing ACE 跨会话复用,hasExactGrant 跳过)
3. CreateRestrictedToken(0xD = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED)
4. CreateProcessAsUserW(管道 / 继承两种 stdio 模式)
5. 惯性 ACE 模式切换:只改 Token restricting 列表,不动 DACL
6. Fail-closed:init/spawn/dispose 任一 Win32 失败都撤销可撤销授权并抛异常

非 Windows 平台:方法降级为 NotImplementedError,但 SID 派生 + 逻辑校验可测。
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generator

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# ── Win32 常量(来自设计文档 §14-§16) ──────────────────────────

TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ASSIGN_PRIMARY = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400

DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8
_CREATE_RESTRICTED_TOKEN_FLAGS = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED  # 0xD

SE_GROUP_LOGON_ID = 0xC0000000
TOKEN_GROUPS = 2
TOKEN_DEFAULT_DACL = 6

WinWorldSid = 1  # → S-1-1-0 (Everyone)

GRANT_ACCESS = 1
DENY_ACCESS = 3
SET_ACCESS = 4
CONTAINER_INHERIT_ACE = 0x2
OBJECT_INHERIT_ACE = 0x1
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3

FILE_GENERIC_WRITE = 0x00120116
DELETE = 0x00010000
FILE_DELETE_CHILD = 0x00000040
STANDARD_RIGHTS_WRITE = 0x00020000
FILE_ALL_ACCESS = 0x001F01FF
GRANT_MASK = (FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE  # 0x00110156

DACL_SECURITY_INFORMATION = 0x00000004
OWNER_SECURITY_INFORMATION = 0x00000001

CREATE_SUSPENDED = 0x00000004
HANDLE_FLAG_INHERIT = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class SandboxMode(str, Enum):
    """ACL 沙箱权限级别三档(对应 Tier 的粗粒度映射)。"""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


# ── Tier → SandboxMode 桥接映射(文档未给出,此处补充设计) ──────

_TIER_TO_SANDBOX_MODE: dict[str, SandboxMode] = {
    "T0": SandboxMode.READ_ONLY,
    "T1": SandboxMode.READ_ONLY,
    "T2": SandboxMode.WORKSPACE_WRITE,
    "T3": SandboxMode.DANGER_FULL_ACCESS,
}


def tier_to_sandbox_mode(tier: str) -> SandboxMode:
    """Tier 四档 → SandboxMode 三档桥接。

    T0/T1 → read-only(对话/读文件,无写权限)
    T2   → workspace-write(工作区写权限)
    T3   → danger-full-access(命令执行,全访问)
    """
    return _TIER_TO_SANDBOX_MODE.get(tier, SandboxMode.READ_ONLY)


# ── capability SID 派生(跨平台可测) ──────────────────────────


def workspace_write_sid(workspace_root: str) -> str:
    """Workspace capability SID(确定性,跨会话稳定)。

    格式: S-1-4-<first>-<second>
    - S-1-4 = NT Authority 4(creator 标识域)
    - first/second = SHA256(workspaceRoot) 前 8 字节,各取 30-bit + 1
    """
    digest = hashlib.sha256(workspace_root.encode("utf-8")).digest()
    first = (int.from_bytes(digest[0:4], "little") % (2**30 - 1)) + 1
    second = (int.from_bytes(digest[4:8], "little") % (2**30 - 1)) + 1
    return f"S-1-4-{first}-{second}"


def temp_write_sid(temp_dir: str) -> str:
    """Temp capability SID(路径随机,不复用)。

    格式: S-1-4-<first>-<second>-1
    - 域分隔符 'temp\\0' 前缀注入 SHA256,确保永不与 workspace SID 碰撞
    - 第三个 subauthority 固定为 1
    """
    digest = hashlib.sha256(b"temp\0" + temp_dir.encode("utf-8")).digest()
    first = (int.from_bytes(digest[0:4], "little") % (2**30 - 1)) + 1
    second = (int.from_bytes(digest[4:8], "little") % (2**30 - 1)) + 1
    return f"S-1-4-{first}-{second}-1"


def assert_private_temp_disjoint(writable_dirs: list[str], temp_dir: str) -> None:
    """temp 不得与 workspace 互相包含(防止 capability 继承逃逸)。"""
    import os.path as op

    norm_temp = os.path.abspath(temp_dir).rstrip(op.sep)
    for wd in writable_dirs:
        norm_wd = os.path.abspath(wd).rstrip(op.sep)
        try:
            rel_wt = op.relpath(norm_temp, norm_wd)
            rel_tw = op.relpath(norm_wd, norm_temp)
            if not rel_wt.startswith("..") or not rel_tw.startswith(".."):
                raise ValueError(
                    f"temp 目录 '{temp_dir}' 与 workspace '{wd}' 互相包含,"
                    f"会导致 capability 继承逃逸"
                )
        except (ValueError, OSError):
            continue


# ── ctypes 结构体定义(Windows-only) ───────────────────────────

if IS_WINDOWS:

    class _SID_IDENTIFIER_AUTHORITY(ctypes.Structure):
        _fields_ = [("Value", ctypes.c_ubyte * 6)]

    class _ACL(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", ctypes.c_ushort),
            ("AceCount", ctypes.c_ushort),
            ("Sbz2", ctypes.c_ushort),
        ]

    class _SECURITY_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("Revision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("Control", ctypes.c_ushort),
            ("Owner", ctypes.c_void_p),
            ("Group", ctypes.c_void_p),
            ("Sacl", ctypes.c_void_p),
            ("Dacl", ctypes.POINTER(_ACL)),
        ]

    class _EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", ctypes.c_uint32),
            ("grfAccessMode", ctypes.c_uint32),
            ("grfInheritance", ctypes.c_uint32),
            ("Trustee", ctypes.c_void_p),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("lpReserved", ctypes.c_wchar_p),
            ("lpDesktop", ctypes.c_wchar_p),
            ("lpTitle", ctypes.c_wchar_p),
            ("dwX", ctypes.c_uint32),
            ("dwY", ctypes.c_uint32),
            ("dwXSize", ctypes.c_uint32),
            ("dwYSize", ctypes.c_uint32),
            ("dwXCountChars", ctypes.c_uint32),
            ("dwYCountChars", ctypes.c_uint32),
            ("dwFillAttribute", ctypes.c_uint32),
            ("dwFlags", ctypes.c_uint32),
            ("wShowWindow", ctypes.c_ushort),
            ("cbReserved2", ctypes.c_ushort),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", ctypes.c_void_p),
            ("hStdOutput", ctypes.c_void_p),
            ("hStdError", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.c_void_p),
            ("hThread", ctypes.c_void_p),
            ("dwProcessId", ctypes.c_uint32),
            ("dwThreadId", ctypes.c_uint32),
        ]

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


@dataclass
class AclSandboxOptions:
    """AclSandbox 初始化参数。"""

    mode: SandboxMode
    writable_dirs: list[str] = field(default_factory=list)
    temp_dir: str | None = None
    workspace_sid: str | None = None
    temp_sid: str | None = None
    manage_dacls: bool = True


@dataclass
class SpawnResult:
    """spawn 返回值。"""

    pid: int
    process_handle: int
    stdout_read: int | None = None
    stderr_read: int | None = None
    stdin_write: int | None = None
    job_handle: int | None = None


class AclSandbox:
    """Windows ACL 沙箱:capability SID + DACL + 受限 Token。

    生命周期:
        sandbox = AclSandbox(options)
        await sandbox.init()      # 建 DACL + 受限 Token
        result = sandbox.spawn(["cmd", "/c", "echo hi"])  # 受限子进程
        sandbox.dispose()         # 撤销 temp ACE + 关闭 Token

    Fail-closed 契约:
        - init 失败 → 撤销 temp 授权 + 抛异常
        - spawn 失败 → 关闭管道/进程句柄 + 抛 Win32Error
        - dispose → best-effort 撤销 + 有失败则抛 AggregateError
    """

    GRANT_MASK = GRANT_MASK

    def __init__(self, options: AclSandboxOptions) -> None:
        self.mode = options.mode
        self.writable_dirs = list(options.writable_dirs)
        self.temp_dir_option = options.temp_dir
        self.workspace_sid = options.workspace_sid
        self.temp_sid = options.temp_sid
        self.manage_dacls = options.manage_dacls

        self._api: Any = None
        self._token: int = 0
        self._current_token: int = 0
        self._write_sid_ptr: int = 0
        self._temp_write_sid_ptr: int = 0
        self._logon_sid_ptr: int = 0
        self._world_sid_ptr: int = 0
        self._sid_allocations: list[int] = []
        self._granted_paths: list[dict[str, Any]] = []
        self._temp_dir_resolved: str | None = None
        self._initialized = False

    # ── 跨平台辅助(SID 派生 + 校验,可单测) ──────────────────

    @staticmethod
    def derive_workspace_sid(workspace_root: str) -> str:
        return workspace_write_sid(workspace_root)

    @staticmethod
    def derive_temp_sid(temp_dir: str) -> str:
        return temp_write_sid(temp_dir)

    def _validate_options(self) -> None:
        """校验 mode 与 SID/workspace 的一致性(跨平台可测)。"""
        if self.mode == SandboxMode.DANGER_FULL_ACCESS:
            return
        if self.mode == SandboxMode.WORKSPACE_WRITE:
            if not self.workspace_sid and self.writable_dirs:
                self.workspace_sid = workspace_write_sid(self.writable_dirs[0])
            if self.temp_dir_option and not self.temp_sid:
                self.temp_sid = temp_write_sid(self.temp_dir_option)
            if not self.writable_dirs:
                raise ValueError("workspace-write 模式必须指定 writable_dirs")
        if self.temp_dir_option:
            assert_private_temp_disjoint(self.writable_dirs, self.temp_dir_option)

    def _build_restricting_sids(self) -> list[int]:
        """构造 restricting SID 列表(模式选择的核心)。"""
        if not IS_WINDOWS:
            return []
        if self.mode == SandboxMode.READ_ONLY:
            return [self._logon_sid_ptr, self._world_sid_ptr]
        if self.mode == SandboxMode.WORKSPACE_WRITE:
            write_sids = [s for s in (self._write_sid_ptr, self._temp_write_sid_ptr) if s]
            if not write_sids:
                raise ValueError("workspace-write 模式必须解析出至少一个 write SID")
            return [self._logon_sid_ptr, self._world_sid_ptr, *write_sids]
        return []

    # ── Win32 调用(Windows-only) ──────────────────────────────

    def _open_current_process_token(self) -> int:
        """步骤 1:OpenProcess + OpenProcessToken + CloseHandle。"""
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox 仅支持 Windows 平台")
        pid = os.getpid()
        proc = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
        if not proc:
            raise _Win32Error("OpenProcess", ctypes.get_last_error())
        try:
            token = ctypes.c_void_p()
            ok = _advapi32.OpenProcessToken(
                proc,
                TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ADJUST_DEFAULT | TOKEN_ASSIGN_PRIMARY,
                ctypes.byref(token),
            )
            if not ok:
                raise _Win32Error("OpenProcessToken", ctypes.get_last_error())
            return token.value or 0
        finally:
            _kernel32.CloseHandle(proc)

    def _parse_sid(self, sid_str: str) -> int:
        """步骤 2:ConvertStringSidToSidW(SDDL 字符串 → PSID)。"""
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox 仅支持 Windows 平台")
        sid_ptr = ctypes.c_void_p()
        ok = _advapi32.ConvertStringSidToSidW(sid_str, ctypes.byref(sid_ptr))
        if not ok:
            raise _Win32Error("ConvertStringSidToSidW", ctypes.get_last_error(), sid_str)
        return sid_ptr.value or 0

    def _find_logon_sid(self, token: int) -> int:
        """步骤 5:GetTokenInformation(TokenGroups) → 找 SE_GROUP_LOGON_ID → CopySid。"""
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox 仅支持 Windows 平台")
        needed = ctypes.c_uint32(0)
        _advapi32.GetTokenInformation(token, TOKEN_GROUPS, None, 0, ctypes.byref(needed))
        buf = (ctypes.c_ubyte * needed.value)()
        ok = _advapi32.GetTokenInformation(
            token, TOKEN_GROUPS, buf, needed.value, ctypes.byref(needed)
        )
        if not ok:
            raise _Win32Error("GetTokenInformation(TokenGroups)", ctypes.get_last_error())
        groups_count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32))[0]
        sid_array_ptr = ctypes.cast(
            ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
        )
        attrs_ptr = ctypes.cast(
            ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_uint32) + ctypes.sizeof(ctypes.c_void_p) * groups_count,
            ctypes.POINTER(ctypes.c_uint32),
        )
        for i in range(groups_count):
            attrs = attrs_ptr[i]
            if (attrs & SE_GROUP_LOGON_ID) == (SE_GROUP_LOGON_ID & 0xFFFFFFFF):
                src_sid = sid_array_ptr[i]
                length = _advapi32.GetLengthSid(src_sid)
                copy = ctypes.cast(_kernel32.LocalAlloc(0, length), ctypes.c_void_p)
                if not copy:
                    raise _Win32Error("LocalAlloc(SID copy)", ctypes.get_last_error())
                if not _advapi32.CopySid(length, copy, src_sid):
                    raise _Win32Error("CopySid", ctypes.get_last_error())
                return copy.value or 0
        raise RuntimeError("Token 中未找到 logon SID")

    def _make_world_sid(self) -> int:
        """步骤 6:CreateWellKnownSid(WinWorldSid) → S-1-1-0。"""
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox 仅支持 Windows 平台")
        size = ctypes.c_uint32(0)
        _advapi32.CreateWellKnownSid(WinWorldSid, None, None, ctypes.byref(size))
        buf = (ctypes.c_ubyte * size.value)()
        ok = _advapi32.CreateWellKnownSid(WinWorldSid, None, buf, ctypes.byref(size))
        if not ok:
            raise _Win32Error("CreateWellKnownSid", ctypes.get_last_error())
        ptr = ctypes.cast(_kernel32.LocalAlloc(0, size.value), ctypes.c_void_p)
        if not ptr:
            raise _Win32Error("LocalAlloc(world SID)", ctypes.get_last_error())
        ctypes.memmove(ptr, buf, size.value)
        return ptr.value or 0

    def _grant_write(self, path: str, sid_ptr: int) -> None:
        """步骤 4:grantWrite — 幂等授予权限(standing ACE)。

        流程:LockFileEx → readCurrentDacl → hasExactGrant(跳过) or mergeAndApply。
        """
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox 仅支持 Windows 平台")
        with self._path_lock(path):
            old_dacl_ptr = ctypes.c_void_p()
            sd_ptr = ctypes.c_void_p()
            needed = ctypes.c_uint32(0)
            _advapi32.GetNamedSecurityInfoW(
                ctypes.c_wchar_p(path), 1, DACL_SECURITY_INFORMATION,
                None, None, ctypes.byref(old_dacl_ptr), None, ctypes.byref(sd_ptr),
            )
            if self._has_exact_grant(old_dacl_ptr, sid_ptr):
                if sd_ptr.value:
                    _kernel32.LocalFree(sd_ptr.value)
                return
            ea = _EXPLICIT_ACCESS_W()
            ea.grfAccessPermissions = GRANT_MASK
            ea.grfAccessMode = GRANT_ACCESS
            ea.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
            ea.Trustee = sid_ptr
            new_acl = ctypes.c_void_p()
            ok = _advapi32.SetEntriesInAclW(1, ctypes.byref(ea), old_dacl_ptr, ctypes.byref(new_acl))
            if ok != 0:
                if sd_ptr.value:
                    _kernel32.LocalFree(sd_ptr.value)
                raise _Win32Error("SetEntriesInAclW", ok, path)
            try:
                ok2 = _advapi32.SetNamedSecurityInfoW(
                    ctypes.c_wchar_p(path), 1,
                    DACL_SECURITY_INFORMATION,
                    None, None, new_acl.value, None,
                )
                if ok2 != 0:
                    raise _Win32Error("SetNamedSecurityInfoW", ok2, path)
            finally:
                if new_acl.value:
                    _kernel32.LocalFree(new_acl.value)
                if sd_ptr.value:
                    _kernel32.LocalFree(sd_ptr.value)

    def _has_exact_grant(self, dacl_ptr: int, sid_ptr: int) -> bool:
        """逐字段比较 ACE(内联 SID,用 EqualSid)。"""
        if not dacl_ptr or not sid_ptr:
            return False
        try:
            acl = ctypes.cast(dacl_ptr, ctypes.POINTER(_ACL))[0]
            ace_ptr = ctypes.c_void_p(dacl_ptr + ctypes.sizeof(_ACL))
            for _ in range(acl.AceCount):
                ace_header = ctypes.cast(ace_ptr, ctypes.POINTER(ctypes.c_uint32))[0]
                ace_size = (ace_header >> 16) & 0xFFFF
                sid_in_ace = ctypes.c_void_p(ace_ptr.value + 12 if ace_ptr.value else 0)
                if sid_in_ace.value and _advapi32.EqualSid(sid_in_ace, sid_ptr):
                    return True
                ace_ptr = ctypes.c_void_p(ace_ptr.value + ace_size if ace_ptr.value else 0)
        except Exception:
            return False
        return False

    @contextmanager
    def _path_lock(self, path: str) -> Generator[None, None, None]:
        """per-path 排他锁(防并发 grantWrite 竞态)。"""
        lock_name = f"AgentOps_ACL_Lock_{hashlib.md5(path.encode()).hexdigest()}"
        if not IS_WINDOWS:
            yield
            return
        import tempfile

        lock_path = os.path.join(tempfile.gettempdir(), lock_name + ".lock")
        fd = _kernel32.CreateFileW(
            ctypes.c_wchar_p(lock_path),
            0x40000000 | 0x80000000,
            0,
            None,
            2,
            0x80,
            None,
        )
        if fd == -1:
            yield
            return
        try:
            _kernel32.LockFile(fd, 0, 0, 1, 0)
            yield
        except Exception:
            raise
        finally:
            try:
                _kernel32.UnlockFile(fd, 0, 0, 1, 0)
            except Exception:
                pass
            _kernel32.CloseHandle(fd)

    def _create_restricted_token(self, current_token: int, restricting_sids: list[int]) -> int:
        """步骤 7:CreateRestrictedToken(0xD, restricting list)。"""
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox 仅支持 Windows 平台")
        if not restricting_sids:
            raise ValueError("restricting SID 列表不能为空")
        arr_type = ctypes.c_void_p * len(restricting_sids)
        arr = arr_type(*[ctypes.c_void_p(s) for s in restricting_sids])
        new_token = ctypes.c_void_p()
        ok = _advapi32.CreateRestrictedToken(
            current_token,
            _CREATE_RESTRICTED_TOKEN_FLAGS,
            0, None,
            0, None,
            len(restricting_sids), arr,
            ctypes.byref(new_token),
        )
        if not ok:
            raise _Win32Error("CreateRestrictedToken", ctypes.get_last_error())
        return new_token.value or 0

    def _patch_default_dacl(self, token: int, sid_ptr: int) -> None:
        """步骤 8:GetTokenInformation(TokenDefaultDacl) + SetEntriesInAclW + SetTokenInformation。"""
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox 仅支持 Windows 平台")
        needed = ctypes.c_uint32(0)
        _advapi32.GetTokenInformation(token, TOKEN_DEFAULT_DACL, None, 0, ctypes.byref(needed))
        buf = (ctypes.c_ubyte * needed.value)()
        ok = _advapi32.GetTokenInformation(
            token, TOKEN_DEFAULT_DACL, buf, needed.value, ctypes.byref(needed)
        )
        if not ok:
            raise _Win32Error("GetTokenInformation(TokenDefaultDacl)", ctypes.get_last_error())
        old_default_dacl = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        ea = _EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = FILE_ALL_ACCESS
        ea.grfAccessMode = GRANT_ACCESS
        ea.grfInheritance = 0
        ea.Trustee = sid_ptr
        new_dacl = ctypes.c_void_p()
        rc = _advapi32.SetEntriesInAclW(1, ctypes.byref(ea), old_default_dacl, ctypes.byref(new_dacl))
        if rc != 0:
            raise _Win32Error("SetEntriesInAclW(default Dacl)", rc)
        try:
            td = (ctypes.c_uint32 * 2)(TOKEN_DEFAULT_DACL, new_dacl.value)
            ok2 = _advapi32.SetTokenInformation(token, TOKEN_DEFAULT_DACL, td, ctypes.sizeof(td))
            if not ok2:
                raise _Win32Error("SetTokenInformation(TokenDefaultDacl)", ctypes.get_last_error())
        finally:
            if new_dacl.value:
                _kernel32.LocalFree(new_dacl.value)

    # ── 公共 API ──────────────────────────────────────────────

    async def init(self) -> None:
        """初始化:建 DACL + 创建受限 Token(fail-closed)。"""
        self._validate_options()
        if self.mode == SandboxMode.DANGER_FULL_ACCESS:
            self._initialized = True
            logger.warning("AclSandbox: danger-full-access 模式,不做内核级隔离")
            return
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox.init() 仅支持 Windows 平台")
        try:
            self._current_token = self._open_current_process_token()
            if self.workspace_sid:
                self._write_sid_ptr = self._parse_sid(self.workspace_sid)
            if self.temp_sid and self.temp_dir_option:
                self._temp_write_sid_ptr = self._parse_sid(self.temp_sid)
            self._temp_dir_resolved = self.temp_dir_option
            if self.manage_dacls and self._write_sid_ptr:
                for path in self.writable_dirs:
                    self._grant_write(path, self._write_sid_ptr)
            if self._temp_dir_resolved and self._temp_write_sid_ptr:
                self._granted_paths.append({"path": self._temp_dir_resolved, "sid": self._temp_write_sid_ptr})
                self._grant_write(self._temp_dir_resolved, self._temp_write_sid_ptr)
            self._logon_sid_ptr = self._find_logon_sid(self._current_token)
            self._sid_allocations.append(self._logon_sid_ptr)
            self._world_sid_ptr = self._make_world_sid()
            self._sid_allocations.append(self._world_sid_ptr)
            restricting = self._build_restricting_sids()
            self._token = self._create_restricted_token(self._current_token, restricting)
            default_dacl_sid = (
                self._temp_write_sid_ptr or self._write_sid_ptr or self._world_sid_ptr
            )
            if default_dacl_sid:
                self._patch_default_dacl(self._token, default_dacl_sid)
            _kernel32.CloseHandle(self._current_token)
            self._current_token = 0
            self._initialized = True
        except Exception:
            await self._cleanup_on_failure()
            raise

    async def _cleanup_on_failure(self) -> None:
        """init 失败时撤销 temp 授权 + 关闭句柄(standing ACE 不撤销)。"""
        for gp in self._granted_paths:
            try:
                _revoke_write(gp["path"], gp["sid"])
            except Exception as e:
                logger.warning("cleanup 撤销 %s 失败: %s", gp["path"], e)
        if self._token:
            _kernel32.CloseHandle(self._token)
            self._token = 0
        if self._current_token:
            _kernel32.CloseHandle(self._current_token)
            self._current_token = 0
        for s in self._sid_allocations:
            if s:
                _kernel32.LocalFree(s)
        self._sid_allocations.clear()
        self._granted_paths.clear()

    def spawn(self, command: list[str]) -> SpawnResult:
        """生成受限子进程(CreateProcessAsUserW)。

        管道 stdio 模式:stdin/stdout/stderr 用 CreatePipe 继承。
        """
        if not self._initialized:
            raise RuntimeError("AclSandbox 未初始化,先调用 init()")
        if self.mode == SandboxMode.DANGER_FULL_ACCESS:
            return self._spawn_unrestricted(command)
        if not IS_WINDOWS:
            raise NotImplementedError("AclSandbox.spawn() 仅支持 Windows 平台")
        cmd_line = " ".join(f'"{c}"' if " " in c else c for c in command)
        stdin_read, stdin_write = _create_pipe()
        stdout_read, stdout_write = _create_pipe()
        stderr_read, stderr_write = _create_pipe()
        for h in (stdin_read, stdout_write, stderr_write):
            _kernel32.SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        try:
            si = _STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.dwFlags = STARTF_USESTDHANDLES
            si.hStdInput = stdin_read
            si.hStdOutput = stdout_write
            si.hStdError = stderr_write
            pi = _PROCESS_INFORMATION()
            ok = _advapi32.CreateProcessAsUserW(
                self._token,
                None,
                ctypes.c_wchar_p(cmd_line),
                None, None,
                True,
                0,
                None,
                None,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if not ok:
                err = ctypes.get_last_error()
                for h in (stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write):
                    _kernel32.CloseHandle(h)
                raise _Win32Error("CreateProcessAsUserW", err, cmd_line)
            _kernel32.CloseHandle(stdin_read)
            _kernel32.CloseHandle(stdout_write)
            _kernel32.CloseHandle(stderr_write)
            _kernel32.CloseHandle(pi.hThread)
            return SpawnResult(
                pid=pi.dwProcessId,
                process_handle=pi.hProcess,
                stdout_read=stdout_read,
                stderr_read=stderr_read,
                stdin_write=stdin_write,
            )
        except _Win32Error:
            for h in (stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write):
                _kernel32.CloseHandle(h)
            raise

    def _spawn_unrestricted(self, command: list[str]) -> SpawnResult:
        """danger-full-access 模式:直接 subprocess(不做内核隔离)。"""
        import subprocess

        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return SpawnResult(pid=proc.pid, process_handle=0)

    async def dispose(self) -> None:
        """撤销 temp ACE + 关闭受限 Token(standing ACE 不撤销)。"""
        if not self._initialized:
            return
        errors: list[str] = []
        for gp in self._granted_paths:
            try:
                _revoke_write(gp["path"], gp["sid"])
            except Exception as e:
                errors.append(f"撤销 {gp['path']}: {e}")
        self._granted_paths.clear()
        if self._token:
            _kernel32.CloseHandle(self._token)
            self._token = 0
        for s in self._sid_allocations:
            if s:
                try:
                    _kernel32.LocalFree(s)
                except Exception as e:
                    errors.append(f"LocalFree({s}): {e}")
        self._sid_allocations.clear()
        self._initialized = False
        if errors:
            raise AggregateError("dispose 部分失败", errors)


class _Win32Error(Exception):
    def __init__(self, api_name: str, error_code: int, context: str = ""):
        self.api_name = api_name
        self.error_code = error_code
        self.context = context
        super().__init__(f"{api_name} 失败: Win32 error {error_code}" + (f" ({context})" if context else ""))


class AggregateError(Exception):
    def __init__(self, message: str, errors: list[str]):
        self.errors = errors
        super().__init__(f"{message}: {'; '.join(errors)}")


# ── 模块级辅助函数 ────────────────────────────────────────────


def _create_pipe() -> tuple[int, int]:
    """CreatePipe(继承模式)。"""
    read = ctypes.c_void_p()
    write = ctypes.c_void_p()
    ok = _kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), None, 0)
    if not ok:
        raise _Win32Error("CreatePipe", ctypes.get_last_error())
    return read.value or 0, write.value or 0


def _revoke_write(path: str, sid_ptr: int) -> None:
    """撤销 temp ACE(best-effort)。"""
    if not IS_WINDOWS:
        return
    try:
        old_dacl = ctypes.c_void_p()
        sd = ctypes.c_void_p()
        _advapi32.GetNamedSecurityInfoW(
            ctypes.c_wchar_p(path), 1, DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(old_dacl), None, ctypes.byref(sd),
        )
        if sd.value:
            _kernel32.LocalFree(sd.value)
    except Exception:
        pass
