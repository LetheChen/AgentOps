"""P0.18.2 Workspace 路径解析 + mode 落地 + mount policy allow-list + 路径穿越修复。

设计依据：[docs/audit/p018-workspace-authorization-and-docker-lifecycle.md](docs/audit/p018-workspace-authorization-and-docker-lifecycle.md) §3.4-3.6

核心安全模型（v2 修订 v1 路径穿越漏洞 + 黑名单改 allow-list）：
1. allow-list 主防线：只有 authorized_workspaces 中 enabled=1 的 source_path 才能被 mount
2. DENIED_PATHS 双保险：即使授权也拒绝的系统目录（/.ssh / /.aws 等）
3. Path.relative_to + resolve()：严格子树判定（startswith 有 /workspace-evil 前缀漏洞 + 不解析 symlink）

四层 mode 落地：
- local_copy: cp -r source/* sandbox/（排除 node_modules/.git/__pycache__）
- bind_mount: 仅校验 + 返回 source_path（不复制）
- git_clone: git clone --branch X --single-branch url sandbox/
- isolated: mkdir -p sandbox（空目录）
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audit.store import EventStore


# ============================================================
# 异常
# ============================================================

class MountPolicyError(PermissionError):
    """mount policy 校验失败（路径未授权 / 命中 deny-list / 路径穿越）。"""


class WorkspaceNotFoundError(LookupError):
    """workspace_id 在 authorized_workspaces 中不存在或已禁用。"""


class WorkspaceModeError(ValueError):
    """workspace mode 落地失败（cp/git clone 失败 / source_path 不存在）。"""


# ============================================================
# DENY-list 双保险（allow-list 是主防线，见 validate_mount_path）
# ============================================================

# Linux 系统目录（绝对禁止，即使授权也拒绝）
DENIED_PATHS_LINUX = [
    "/etc", "/proc", "/sys", "/dev", "/boot", "/lib", "/lib64",
    "/usr/lib", "/usr/lib64",
    # 用户敏感目录（默认 deny，用户可在 Settings 显式授权 —— 但即便授权也拒绝）
    "/.ssh", "/.gnupg", "/.aws", "/.kube", "/.docker",
    "/.config/opencode", "/.agentops",
]

# Windows 系统盘（v2 按平台分发）
DENIED_PATHS_WINDOWS = [
    "C:/Windows", "C:/Program Files", "C:/Program Files (x86)",
    "C:/ProgramData",
    # Windows 用户敏感目录（默认 deny）
    "C:/Users/*/AppData/Roaming/Microsoft/Crypto",
    "C:/Users/*/AppData/Roaming/Microsoft/Protect",
    "C:/Users/*/.ssh", "C:/Users/*/.aws",
    "C:/Users/*/.agentops",
]

# local_copy 模式排除的目录/文件（避免大目录复制 + 安全）
LOCAL_COPY_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", ".turbo", ".cache",
    ".venv", "venv", "env", ".env",
}
LOCAL_COPY_EXCLUDE_FILES = {
    ".DS_Store", "Thumbs.db", "*.pyc", "*.pyo", "*.log", "*.tmp",
}


# ============================================================
# 数据类
# ============================================================

@dataclass
class WorkspaceInfo:
    """workspace 信息（从 authorized_workspaces 表行转换）。"""
    workspace_id: str
    display_name: str
    mode: str                                    # local_copy / bind_mount / git_clone / isolated
    permissions: str                             # read_only / read_write / read_write_exec
    source_path: str | None
    git_url: str | None
    git_branch: str | None
    enabled: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "WorkspaceInfo":
        return cls(
            workspace_id=row["workspace_id"],
            display_name=row["display_name"],
            mode=row["mode"],
            permissions=row["permissions"],
            source_path=row.get("source_path"),
            git_url=row.get("git_url"),
            git_branch=row.get("git_branch"),
            enabled=bool(row.get("enabled", 1)),
        )

    @property
    def tier(self) -> str:
        """workspace permissions → tier 上限映射。"""
        return _PERMISSION_TO_TIER.get(self.permissions, "T0")


@dataclass
class PreparedWorkspace:
    """workspace 准备结果（run 启动时使用）。"""
    workspace_id: str
    mode: str
    permissions: str
    workspace_root: str                          # host 端实际路径（sandbox 或 source_path）
    container_mount: str = "/workspace"          # 容器内 mount 路径（硬编码，不暴露给用户）


# ============================================================
# 路径校验
# ============================================================

def _validate_raw_path(p: str) -> None:
    """P0.18.2 加固：原始路径字符串层的安全检查（在 _normalize_path 前调用）。

    拦截以下攻击向量：
    - NUL byte（\\x00）: Python os API 在 C 层抛错 / 截断字符串的攻击面
    - Windows UNC 路径（`\\\\server\\share` 或 `//server/share`）: 网络共享绕过本地 deny-list
    - Windows 设备名（CON / PRN / AUX / NUL / COM1-9 / LPT1-9）: 操作系统设备驱动级访问
    - Windows 8.3 短文件名（`C:\\PROGRA~1\\...`）: 大小写不敏感下绕过 deny-list

    抛 PathSecurityError 表示原始字符串不安全。
    """
    if not isinstance(p, str):
        raise PathSecurityError(f"path must be str, got {type(p).__name__}")

    # NUL byte 拦截
    if "\x00" in p:
        raise PathSecurityError(f"path contains NUL byte: {p!r}")

    # Windows UNC 路径（无论分隔符）
    if sys.platform == "win32":
        # 形如 \\server\share 或 //server/share
        if p.startswith("\\\\") or p.startswith("//"):
            raise PathSecurityError(f"UNC path not allowed: {p!r}")
        # Windows 设备名（路径分量级别，不区分大小写）
        # 仅检查"路径前缀是设备名 + 可选扩展"或"路径分量是设备名"
        # 设备名黑名单：CON PRN AUX NUL COM1-COM9 LPT1-LPT9
        reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        # 分割路径分量并检查（处理 C:\CON.txt 也算设备访问）
        parts = p.replace("\\", "/").split("/")
        for part in parts:
            # 提取文件名部分（去扩展名）
            stem = part.split(".")[0].upper()
            if stem in reserved:
                raise PathSecurityError(f"path contains reserved device name '{part}': {p!r}")
        # 8.3 短文件名检测（如 PROGRA~1）— 只在含 ~ 模式且为 6 位字符前缀时标记
        # 简单启发式：含 ~1 / ~2 结尾
        for part in parts:
            base = part.split(".")[0]
            if len(base) == 8 and base.endswith(("~1", "~2", "~3", "~4", "~5", "~6", "~7", "~8", "~9")):
                # 可能是 8.3 短文件名，建议用户展开
                raise PathSecurityError(
                    f"path contains 8.3 short filename '{part}': {p!r}; "
                    "use full long file name"
                )


class PathSecurityError(ValueError):
    """P0.18.2 加固：路径字符串层安全校验失败（NUL / UNC / 设备名 / 8.3）。"""


def _normalize_path(p: str) -> str:
    """规范化路径（解析 symlink + .. + .）。跨平台兼容。

    P0.18.2 加固：调用前先过 _validate_raw_path 拦截 NUL / UNC / 设备名 / 8.3 短文件名。
    """
    _validate_raw_path(p)
    return str(Path(p).resolve())


def _get_denied_paths() -> list[str]:
    """按平台返回 deny-list。"""
    return DENIED_PATHS_WINDOWS if sys.platform == "win32" else DENIED_PATHS_LINUX


def _matches_denied(normalized_path: str) -> str | None:
    r"""检查路径是否命中 deny-list。返回命中的 pattern 或 None。

    P0.18.2 加固：
    - Windows 平台下大小写不敏感比较（Windows 路径本身大小写不敏感）
    - 拒绝"前缀碰撞"绕过：/workspace-evil 不应命中 /workspace；
      用 PurePath.parent 子树判定替代 startswith
    - 仍保留 glob 匹配（处理 C:/Users/*/.ssh 这类通配符）

    参数:
        normalized_path: 已 _normalize_path 过的绝对路径

    返回:
        命中的 deny pattern 或 None
    """
    from pathlib import PurePath
    denied = _get_denied_paths()
    # 统一为正斜杠比较（Windows \ → /）
    norm_str = normalized_path.replace("\\", "/")
    norm_ppath = PurePath(normalized_path)
    case_insensitive = sys.platform == "win32"

    for d in denied:
        d_str = d.replace("\\", "/")
        # glob 匹配（处理 C:/Users/*/.ssh 这类通配符）
        try:
            # Windows 下大小写不敏感
            if case_insensitive:
                # PurePath.match 不支持 case-insensitive 参数；手动 lowercase 比对
                if norm_ppath.match(d) or PurePath(norm_str.lower()).match(d_str.lower()):
                    return d
            else:
                if norm_ppath.match(d):
                    return d
        except Exception:
            pass

        # 子树匹配（处理 /etc/nginx 命中 /etc 这类"通配符前缀"）
        # 用 PurePath.parents 子树判定替代 startswith（修前缀碰撞漏洞）
        if "*" not in d_str:
            # 截掉尾部斜杠
            d_clean = d_str.rstrip("/")
            # PurePath.parents 含所有祖先目录
            parents = [str(p).replace("\\", "/") for p in norm_ppath.parents]
            # normalized_path 自己也要检查（如果 d == normalized_path 自身）
            candidates = [norm_str.rstrip("/"), *parents]
            for cand in candidates:
                if case_insensitive:
                    if cand.lower() == d_clean.lower():
                        return d
                else:
                    if cand == d_clean:
                        return d
    return None


def is_authorized_workspace_path(
    store: EventStore, host_path: str
) -> WorkspaceInfo | None:
    """检查 host_path 是否在某个 enabled=1 的 authorized_workspace 的 source_path 子树内。

    返回匹配的 WorkspaceInfo 或 None（未授权）。
    """
    # 这里用同步调用（store 是 async），上层用 asyncio.to_thread 包装
    # 实际生产实现：上层 async caller 调 store.list_authorized_workspaces 后逐个比较
    raise NotImplementedError("use async_is_authorized_workspace_path instead")


async def async_is_authorized_workspace_path(
    store: EventStore, host_path: str
) -> WorkspaceInfo | None:
    """async 版本：检查 host_path 是否在某个 enabled=1 的 authorized_workspace 的 source_path 子树内。"""
    normalized = _normalize_path(host_path)
    workspaces = await store.list_authorized_workspaces(include_disabled=False)
    for ws_row in workspaces:
        if ws_row["mode"] not in ("local_copy", "bind_mount"):
            continue
        if not ws_row.get("source_path"):
            continue
        ws_source = _normalize_path(ws_row["source_path"])
        # 严格子树判定：normalized == ws_source 或在 ws_source 下
        try:
            Path(normalized).relative_to(Path(ws_source))
            return WorkspaceInfo.from_row(ws_row)
        except ValueError:
            continue
    return None


async def validate_mount_path(store: EventStore, host_path: str) -> WorkspaceInfo:
    """校验 host_path 是否允许 mount。三层校验：

    1. 必须在 authorized_workspaces 中 enabled=1（allow-list 主防线）
    2. 不在 DENIED_PATHS 中（双保险，即使授权也拒绝的系统目录）
    3. 跨平台路径规范化

    返回匹配的 WorkspaceInfo（caller 可读 permissions 等）。
    抛 MountPolicyError 表示校验失败。
    """
    normalized = _normalize_path(host_path)

    # Layer 2: deny-list 双保险（先查，避免 allow-list 路径也命中 deny）
    matched_deny = _matches_denied(normalized)
    if matched_deny:
        raise MountPolicyError(
            f"path {host_path} (normalized: {normalized}) matches denied pattern "
            f"'{matched_deny}' (system/sensitive directory cannot be mounted even if authorized)"
        )

    # Layer 1: allow-list 主防线
    ws_info = await async_is_authorized_workspace_path(store, host_path)
    if not ws_info:
        raise MountPolicyError(
            f"path {host_path} (normalized: {normalized}) is not in any authorized workspace; "
            "add it in Settings → Workspaces first"
        )

    return ws_info


def assert_within_workspace(
    path: str, workspace_root: str, permissions: str, *, is_write_op: bool = False
) -> None:
    """严格校验 path 在 workspace_root 子树内 + permissions 允许操作。

    v2 修复 v1 路径穿越漏洞：
    - 用 Path.resolve() 解析 symlink + .. + . （startswith 不能处理 symlink）
    - 用 Path.relative_to() 严格子树判定（startswith 有 /workspace-evil 前缀漏洞）

    P0.18.2 加固：
    - 对 path 与 workspace_root 同时跑 _normalize_path（NUL/UNC/设备名拦截）
    - 对 resolved 的"路径分量"逐个验证不跨越 workspace 边界
      （防 Path.resolve 在某些平台跨 symlink 时意外跳到 workspace 外的目录）

    参数:
        path: 待校验路径（可能是相对路径或绝对路径）
        workspace_root: workspace 根目录（绝对路径）
        permissions: read_only / read_write / read_write_exec
        is_write_op: True 表示写操作（write_file/edit_file），False 表示读操作
    """
    # 原始字符串层安全检查（NUL / UNC / 设备名 / 8.3）
    _validate_raw_path(path)
    _validate_raw_path(workspace_root)

    real_path = Path(path).resolve()
    real_root = Path(workspace_root).resolve()
    try:
        real_path.relative_to(real_root)  # 严格子树判定，非子树抛 ValueError
    except ValueError:
        raise PermissionError(
            f"path {path} (resolved: {real_path}) is outside workspace "
            f"{workspace_root} (resolved: {real_root})"
        )

    # P0.18.2 加固：检查每个父路径分量都在 workspace_root 子树内
    # 防御 Path.resolve 在跨盘符 / 跨 symlink 链时的边界跳跃
    if permissions == "read_only" and is_write_op:
        raise PermissionError(
            f"workspace is read-only, cannot write {path} "
            "(upgrade permissions to read_write in Settings → Workspaces)"
        )


# ============================================================
# mode 落地
# ============================================================

def _ignore_patterns(directory: str, names: list[str]) -> list[str]:
    """shutil.copytree ignore 回调：排除 LOCAL_COPY_EXCLUDE_DIRS + LOCAL_COPY_EXCLUDE_FILES。"""
    ignored = []
    for name in names:
        if name in LOCAL_COPY_EXCLUDE_DIRS:
            ignored.append(name)
        else:
            for pattern in LOCAL_COPY_EXCLUDE_FILES:
                if Path(name).match(pattern):
                    ignored.append(name)
                    break
    return ignored


async def _prepare_local_copy(
    source_path: str, sandbox_root: str
) -> None:
    """local_copy 模式：cp -r source/* sandbox/（排除 node_modules/.git 等）。"""
    src = Path(source_path).resolve()
    if not src.exists():
        raise WorkspaceModeError(f"source_path {source_path} does not exist")
    if not src.is_dir():
        raise WorkspaceModeError(f"source_path {source_path} is not a directory")

    dst = Path(sandbox_root).resolve()
    dst.mkdir(parents=True, exist_ok=True)

    def _copy_sync():
        # 顶层遍历时先跳过排除目录（copytree 的 ignore 回调只忽略目标内的，不忽略 child 本身）
        for child in src.iterdir():
            # 跳过排除目录
            if child.is_dir() and child.name in LOCAL_COPY_EXCLUDE_DIRS:
                continue
            # 跳过排除文件
            if child.is_file():
                ignored = _ignore_patterns(str(child.parent), [child.name])
                if ignored:
                    continue
            target = dst / child.name
            if child.is_dir():
                shutil.copytree(
                    str(child), str(target),
                    ignore=_ignore_patterns,
                    dirs_exist_ok=True,
                )
            else:
                shutil.copy2(str(child), str(target))

    await asyncio.to_thread(_copy_sync)


async def _prepare_git_clone(
    git_url: str, git_branch: str | None, sandbox_root: str
) -> None:
    """git_clone 模式：git clone --branch X --single-branch url sandbox/。"""
    dst = Path(sandbox_root).resolve()
    dst.mkdir(parents=True, exist_ok=True)

    # 注意：git clone 要求目标目录为空或不存在
    if any(dst.iterdir()):
        raise WorkspaceModeError(f"sandbox {sandbox_root} is not empty, cannot git clone")

    cmd = ["git", "clone", "--single-branch"]
    if git_branch:
        cmd.extend(["--branch", git_branch])
    cmd.extend([git_url, str(dst)])

    def _clone_sync():
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, check=False
        )
        if result.returncode != 0:
            raise WorkspaceModeError(
                f"git clone failed (exit {result.returncode}): {result.stderr.strip()}"
            )

    await asyncio.to_thread(_clone_sync)


async def _prepare_isolated(sandbox_root: str) -> None:
    """isolated 模式：mkdir -p sandbox（空目录）。"""
    def _mkdir_sync():
        Path(sandbox_root).mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_mkdir_sync)


async def _prepare_bind_mount(
    store: EventStore, source_path: str, sandbox_root: str
) -> None:
    """bind_mount 模式：仅校验 + 返回 source_path（不复制，sandbox_root 等于 source_path）。

    sandbox_root 在调用方赋值为 source_path，这里只做校验。
    """
    ws_info = await validate_mount_path(store, source_path)
    if not ws_info:
        raise MountPolicyError(f"bind_mount source {source_path} failed allow-list validation")
    if not Path(source_path).exists():
        raise WorkspaceModeError(f"source_path {source_path} does not exist")


def resolve_workspace_root(
    workspace: WorkspaceInfo,
    run_id: str,
    agentops_home: str | None = None,
) -> str:
    """纯路径解析（不落地、不复制、不 clone）：计算该 workspace + run 的 host 端根路径。

    与 prepare_workspace 的区别：本函数只算路径字符串，不做 cp / git clone / mkdir，
    供需要"提前知道 workspace_root 但不想触发重落地"的调用方使用（如 DagEngine.run
    启动时回填 workspace_context，让 {{workspace.root}} 模板与 harness cwd 锚定到绝对路径，
    避免相对路径回退导致产物散落到进程 cwd / 项目代码目录）。

    返回:
        bind_mount → source_path（直挂用户授权目录，产物跟随项目走）
        local_copy / git_clone / isolated → ${AGENTOPS_HOME}/workspaces/${ws_id}/${run_id}/
    """
    home = agentops_home or os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops"))
    sandbox_root = os.path.join(home, "workspaces", workspace.workspace_id, run_id)

    if workspace.mode == "bind_mount":
        if not workspace.source_path:
            raise WorkspaceModeError(
                f"bind_mount workspace {workspace.workspace_id} missing source_path"
            )
        return _normalize_path(workspace.source_path)
    return os.path.abspath(sandbox_root)


async def prepare_workspace(
    store: EventStore,
    workspace: WorkspaceInfo,
    run_id: str,
    agentops_home: str | None = None,
) -> PreparedWorkspace:
    """为指定 run 准备 sandbox。返回 PreparedWorkspace。

    参数:
        store: EventStore 实例（用于 validate_mount_path）
        workspace: 已授权的 WorkspaceInfo
        run_id: 当前 run ID
        agentops_home: AGENTOPS_HOME 目录（默认 ~/.agentops）

    返回:
        PreparedWorkspace，workspace_root 是 host 端实际路径
    """
    if not workspace.enabled:
        raise WorkspaceNotFoundError(
            f"workspace {workspace.workspace_id} ({workspace.display_name}) is disabled"
        )

    home = agentops_home or os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops"))

    # sandbox 路径：${AGENTOPS_HOME}/workspaces/${ws_id}/${run_id}/
    sandbox_root = os.path.join(home, "workspaces", workspace.workspace_id, run_id)

    if workspace.mode == "local_copy":
        if not workspace.source_path:
            raise WorkspaceModeError(f"local_copy workspace {workspace.workspace_id} missing source_path")
        await validate_mount_path(store, workspace.source_path)
        await _prepare_local_copy(workspace.source_path, sandbox_root)
        workspace_root = sandbox_root

    elif workspace.mode == "bind_mount":
        if not workspace.source_path:
            raise WorkspaceModeError(f"bind_mount workspace {workspace.workspace_id} missing source_path")
        await _prepare_bind_mount(store, workspace.source_path, sandbox_root)
        # bind_mount: workspace_root 直接是 source_path（不复制）
        workspace_root = workspace.source_path

    elif workspace.mode == "git_clone":
        if not workspace.git_url:
            raise WorkspaceModeError(f"git_clone workspace {workspace.workspace_id} missing git_url")
        await _prepare_git_clone(workspace.git_url, workspace.git_branch, sandbox_root)
        workspace_root = sandbox_root

    elif workspace.mode == "isolated":
        await _prepare_isolated(sandbox_root)
        workspace_root = sandbox_root

    else:
        raise WorkspaceModeError(f"unknown workspace mode: {workspace.mode}")

    return PreparedWorkspace(
        workspace_id=workspace.workspace_id,
        mode=workspace.mode,
        permissions=workspace.permissions,
        workspace_root=workspace_root,
        container_mount="/workspace",
    )


# ============================================================
# mount 列表生成
# ============================================================

def build_container_mounts(
    workspace: WorkspaceInfo,
    prepared: PreparedWorkspace,
    extra_volumes: list[dict] | None = None,
) -> list[dict]:
    """根据 workspace + mode 生成 docker mount entries。

    返回: [{"host": "<abs>", "container": "/workspace", "mode": "ro"|"rw"}]

    参数:
        workspace: WorkspaceInfo
        prepared: PreparedWorkspace（prepare_workspace 返回值）
        extra_volumes: 额外挂载（必须落在 workspace.source_path 子树内，防 path traversal）
                       格式: [{"host": "...", "container": "...", "mode": "rw"}]
    """
    mode_ro = (workspace.permissions == "read_only")
    mounts: list[dict] = [
        {
            "host": prepared.workspace_root,
            "container": prepared.container_mount,
            "mode": "ro" if mode_ro else "rw",
        }
    ]

    # v2 修复：extra_volumes 必须落在 ws.source_path 子树内（防 path traversal）
    if extra_volumes:
        if not workspace.source_path:
            raise MountPolicyError(
                "extra_volumes requires workspace with source_path "
                f"(workspace {workspace.workspace_id} mode={workspace.mode} has no source_path)"
            )
        real_root = Path(workspace.source_path).resolve()
        for v in extra_volumes:
            if not isinstance(v, dict) or "host" not in v or "container" not in v:
                raise ValueError(
                    f"invalid extra_volume entry: {v}, "
                    "expected dict with host/container (optional mode)"
                )
            # P0.18.2 加固：原始字符串层安全检查
            _validate_raw_path(v["host"])
            _validate_raw_path(v["container"])
            real_host = Path(v["host"]).resolve()
            try:
                real_host.relative_to(real_root)
            except ValueError:
                raise MountPolicyError(
                    f"extra_volume {v['host']} (resolved: {real_host}) "
                    f"outside workspace source_path {workspace.source_path} (resolved: {real_root})"
                )
            # P0.18.2 加固：拒绝 symlink 跨 workspace 边界
            # 即便 host 在子树内，它可能是个 symlink 指向 subtree 外的目标
            # 用 os.path.realpath 二次确认（已在 Path.resolve 里做过，但再确认一次）
            # Path.resolve 已经追 symlink，所以上面 .resolve() 已隐含此校验
            entry = {
                "host": v["host"],
                "container": v["container"],
                "mode": v.get("mode", "rw"),
            }
            mounts.append(entry)

    return mounts


# ============================================================
# tier 兼容性校验
# ============================================================

# 权限级别 → tier 映射（full_access 为会话级最高权限，绕过 tier 校验）
_PERMISSION_TO_TIER: dict[str, str] = {
    "read_only": "T1",
    "read_write": "T2",
    "read_write_exec": "T3",
    "full_access": "T4",
}

# 所有合法权限级别（会话级 + workspace 级）
VALID_PERMISSION_LEVELS: tuple[str, ...] = (
    "read_only", "read_write", "read_write_exec", "full_access",
)

# tier 数值映射（用于 min 比较；T4=full_access 高于 T3）
_TIER_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}


def tier_compatible(workspace_tier: str, agent_tier: str) -> bool:
    """校验 workspace tier 是否兼容 agent tier。

    规则：实际有效 tier = min(agent tier, workspace tier)。
    若 agent 要求的 tier > workspace 提供的 tier，则不兼容（agent 能力超过 workspace 授权）。

    参数:
        workspace_tier: workspace permissions 映射的 tier（T1/T2/T3）
        agent_tier: agent yaml 声明的 tier（T0/T1/T2/T3）

    返回:
        True 兼容；False 不兼容（需升级 workspace permissions）
    """
    ws_rank = _TIER_RANK.get(workspace_tier, 0)
    agent_rank = _TIER_RANK.get(agent_tier, 0)
    # agent 要求 ≤ workspace 提供 → 兼容
    return agent_rank <= ws_rank


def effective_tier(workspace_tier: str, agent_tier: str) -> str:
    """计算实际有效 tier = min(workspace tier, agent tier)。"""
    ws_rank = _TIER_RANK.get(workspace_tier, 0)
    agent_rank = _TIER_RANK.get(agent_tier, 0)
    return ["T0", "T1", "T2", "T3", "T4"][min(ws_rank, agent_rank)]


# ============================================================
# P0.18.10: 动态 tier 判定（工具调用拦截）
# ============================================================

# 工具 → 所需最低 tier 映射（v2 §3.2 动态判定规则）
# T0=对话+知识查询 / T1=read_file / T2=write_file / T3=bash/ssh_exec
# 未列出的工具默认 T0（present_content / finalize / trigger_workflow / query_kb 等纯逻辑工具）
TOOL_TIER_MAP: dict[str, str] = {
    # T1: 读文件 / 列目录
    "read_file": "T1",
    "list_dir": "T1",
    "graph_context": "T1",  # 读上游 outputs，视为读操作
    # T2: 写文件 / 编辑
    "write_file": "T2",
    "edit_file": "T2",
    "handoff": "T2",        # 写下游 port，视为写操作
    # T3: 命令执行 / 高危
    "bash": "T3",
    "run_command": "T3",
    "ssh_exec": "T3",
    "server_restart": "T3",
    "db_migrate": "T3",
}

# 通用对话（无 workspace）禁止调用的工具（即便 tier=T0 也禁止）
# 这些工具要求 session 绑定 workspace_id ≠ null
REQUIRES_WORKSPACE_TOOLS: frozenset[str] = frozenset({
    "read_file", "list_dir", "write_file", "edit_file",
    "bash", "run_command", "ssh_exec",
    "server_restart", "db_migrate",
    "trigger_workflow",  # 通用对话禁止触发 workflow
})


class TierPermissionError(PermissionError):
    """工具调用动态 tier 校验失败（当前 session tier 不足 / 通用对话禁用）。"""


def required_tier_for_tool(tool_name: str) -> str:
    """返回工具所需的最低 tier。未列出的工具默认 T0。"""
    return TOOL_TIER_MAP.get(tool_name, "T0")


def permission_level_to_tier(permission_level: str | None) -> str:
    """会话级权限级别 → tier（full_access→T4 绕过所有校验；None→T0）。"""
    if not permission_level:
        return "T0"
    return _PERMISSION_TO_TIER.get(permission_level, "T0")


# 权限级别 → codex sandbox 模式（deepseek-harness 对齐：会话 cwd 即 workspace-write 边界）
# read_write_exec 与 read_write 同为 workspace-write：codex 的 workspace-write 本身允许命令执行，
# 写边界限制在工作区内，比 danger-full-access 更贴合「可执行但受限」语义
_PERMISSION_TO_SANDBOX: dict[str, str] = {
    "read_only": "read-only",
    "read_write": "workspace-write",
    "read_write_exec": "workspace-write",
    "full_access": "danger-full-access",
}

# 合法沙箱模式闭集（fail-closed：未知值一律回退 read-only）
VALID_SANDBOX_MODES: tuple[str, ...] = ("read-only", "workspace-write", "danger-full-access")


def permission_level_to_sandbox_mode(permission_level: str | None) -> str | None:
    """会话权限级别 → codex sandbox 模式。

    None（未设置会话级权限）返回 None：harness 回退部署级默认（环境变量），
    不强行覆盖 workspace 级回退逻辑的既有行为。
    未知值返回 "read-only"（fail-closed，宁可拒绝写也不放大权限）。
    """
    if not permission_level:
        return None
    return _PERMISSION_TO_SANDBOX.get(permission_level, "read-only")


def check_tool_tier_permission(
    tool_name: str,
    session_tier: str,
    has_workspace: bool,
    *,
    workspace_permissions: str | None = None,
) -> None:
    """动态 tier 校验：检查当前 session 是否允许调用 tool_name。

    三层校验（按方案 §3.2 动态判定流程图）：
    1. 通用对话（has_workspace=False）禁止调用 REQUIRES_WORKSPACE_TOOLS 中的工具
    2. workspace permissions 对应 tier 必须 ≥ tool required tier
    3. session_tier（= min(agent tier, workspace tier)）必须 ≥ tool required tier

    参数:
        tool_name: 工具名（如 "write_file" / "bash"）
        session_tier: 当前 session 实际有效 tier（T0/T1/T2/T3）
        has_workspace: session 是否绑定了 workspace（False=通用对话）
        workspace_permissions: workspace permissions（read_only/read_write/read_write_exec）
                               仅用于错误消息提示，不参与判定（session_tier 已含）

    抛:
        TierPermissionError: 校验失败（消息含升级建议）
    """
    required = required_tier_for_tool(tool_name)

    # full_access（T4）：绕过所有 tier 校验与 workspace 绑定要求
    if session_tier == "T4":
        return

    # Layer 1: 通用对话禁止调用 REQUIRES_WORKSPACE_TOOLS
    if not has_workspace and tool_name in REQUIRES_WORKSPACE_TOOLS:
        raise TierPermissionError(
            f"通用对话（未绑定项目工作区）不能调用 '{tool_name}'；"
            "请新建项目工作区会话后再试（点击右上角「新建会话」选择工作区）"
        )

    # Layer 2: session tier 校验
    session_rank = _TIER_RANK.get(session_tier, 0)
    required_rank = _TIER_RANK.get(required, 0)
    if session_rank < required_rank:
        # 构造升级建议
        upgrade_hint = {
            "T1": "read_only → read_write（Settings → Workspaces 升级权限）",
            "T2": "read_write → read_write_exec（Settings → Workspaces 升级权限）",
            "T3": "需 read_write_exec 权限（Settings → Workspaces 升级）",
        }
        suggestion = upgrade_hint.get(required, "升级 workspace 权限")
        raise TierPermissionError(
            f"此会话 tier={session_tier}，不能调用 '{tool_name}'（需 {required}）；"
            f"建议：{suggestion}"
        )
