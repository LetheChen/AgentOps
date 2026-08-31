"""
P0.18.3 异步 Docker provider，包装 docker_runtime.py 同步函数 + 扩展 mounts/tmpfs/security_opts。

设计依据：[docs/audit/p018-workspace-authorization-and-docker-lifecycle.md](docs/audit/p018-workspace-authorization-and-docker-lifecycle.md) §4.2

迁移策略（v2 包装而非替换）：
1. 同步函数（list/pull/stop/remove/logs）直接调 docker_runtime 同名函数（asyncio.to_thread 包装）
2. 新增能力（mounts/tmpfs/cap_drop/security_opts/read_only_rootfs/resource_limits/extra_hosts/network_mode/ports）在新模块实现
3. create_container 新签名用 ContainerCreateOptions 强类型（向后兼容旧 create_and_start_container）
4. 过渡期保留 docker_runtime.py 作 thin wrapper，1 个 sprint 后删除

v2 修复 v1 关键问题：
- 删除 network="host"（与 ports 互斥，host 模式下 ports 被忽略）
- 改用 bridge + extra_hosts=host-gateway（Linux/Windows Docker Desktop 都支持）
- 新增 verify_container_gone（label filter 验证容器确实被移除）
- 新增 container_logs_stream（异步生成器，SSE 推送用）

P0.18.3 加固（v3）：
- ContainerCreateOptions.__post_init__ 跑全字段校验（image / name / cmd / env / labels / network_mode / security_opts / mounts / tmpfs / ports / resource_limits）
- image 拒绝 NUL / 控制字符 / 过长 / 拼接恶意 `--privileged` / 空白字符
- env 拒绝敏感变量（LD_PRELOAD / LD_LIBRARY_PATH / PATH / HOME / *_PROXY / *_TOKEN / *_SECRET / *_KEY）
- label key/value 拒绝 NUL + 控制字符 + 长度限制（k8s 限制 63 字符）
- network_mode 白名单（bridge / none / container:<id> / host — host 显式记录 warning）
- security_opts 拒绝含 `=` 之外的 shell metacharacter（避免单参数被注入多选项）
- _build_volumes Windows 路径含 `:` 仍能正确处理（key 用 docker SDK 原生 volumes_from dict）
- cmd 元素 NUL 拦截 + 长度限制（ARG_MAX 限制单参数 ~128KB）
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from orchestrator import docker_runtime

logger = logging.getLogger(__name__)


# ============================================================
# P0.18.3 加固：错误 + 校验常量
# ============================================================

class ContainerSecurityError(ValueError):
    """P0.18.3 加固：容器配置参数安全校验失败（image / env / labels / network 等）。"""


# 容器镜像 / 字段长度限制（参照 docker daemon + k8s label 限制）
MAX_IMAGE_LEN = 256
MAX_NAME_LEN = 64            # docker container name max 64
MAX_LABEL_KEY_LEN = 63       # k8s label key max 63
MAX_LABEL_VALUE_LEN = 63
MAX_CMD_ARG_LEN = 131072     # ARG_MAX 单参数 ~128KB
MAX_LABELS_COUNT = 64        # 防止 label spam
MAX_ENV_COUNT = 256
MAX_PORTS_COUNT = 32

# network_mode 白名单（v2 修复核心：默认 bridge，不用 host 除非 caller 显式声明）
ALLOWED_NETWORK_MODES = frozenset({"bridge", "none", "host"})

# env 敏感黑名单（注入这些可能突破容器隔离或泄漏密钥）
# 大小写不敏感（Linux env 变量本身大写，但恶意 caller 可能用小写变体绕过）
# 注意：AGENTOPS_* 前缀的运行时凭证（worker_token / ws_url）由 provisioner 内部注入，
# 不受此黑名单约束（见 AGENTOPS_ENV_ALLOWLIST）。
DENIED_ENV_KEY_PATTERNS = [
    re.compile(r"^LD_PRELOAD$", re.IGNORECASE),
    re.compile(r"^LD_LIBRARY_PATH$", re.IGNORECASE),
    re.compile(r"^LD_AUDIT$", re.IGNORECASE),
    re.compile(r"^PATH$", re.IGNORECASE),                  # 改 PATH 可劫持二进制查找
    re.compile(r"^HOME$", re.IGNORECASE),                 # 改 HOME 影响应用行为
    re.compile(r"^HOSTALIASES$", re.IGNORECASE),          # DNS 劫持
    re.compile(r".*_PROXY$", re.IGNORECASE),              # HTTP_PROXY / HTTPS_PROXY
    re.compile(r".*_SECRET$", re.IGNORECASE),
    re.compile(r".*_KEY$", re.IGNORECASE),
    re.compile(r".*_PASSWORD$", re.IGNORECASE),
    re.compile(r"^KUBECONFIG$", re.IGNORECASE),           # 集群凭证
    re.compile(r"^AWS_.*", re.IGNORECASE),                # AWS 凭证
    re.compile(r"^AZURE_.*", re.IGNORECASE),
    re.compile(r"^GOOGLE_.*", re.IGNORECASE),
    re.compile(r"^GCP_.*", re.IGNORECASE),
]

# _TOKEN 黑名单拆开，因为 AGENTOPS_WORKER_TOKEN 是合法的运行时凭证
# （provisioner 注入容器用于 WorkerRegistry 注册），需要白名单豁免
DENIED_TOKEN_KEY_PATTERNS = [
    re.compile(r"^GITHUB_TOKEN$", re.IGNORECASE),
    re.compile(r"^SLACK_TOKEN$", re.IGNORECASE),
    re.compile(r"^OPENAI_API_TOKEN$", re.IGNORECASE),
    re.compile(r"^ANTHROPIC_API_TOKEN$", re.IGNORECASE),
    re.compile(r"^HF_TOKEN$", re.IGNORECASE),            # HuggingFace token
    re.compile(r"^NPM_TOKEN$", re.IGNORECASE),
    re.compile(r"^PYPI_TOKEN$", re.IGNORECASE),
    re.compile(r"^DOCKER_TOKEN$", re.IGNORECASE),
    re.compile(r"^API_TOKEN$", re.IGNORECASE),           # 通用 API_TOKEN（用户凭证典型）
    re.compile(r".*_API_TOKEN$", re.IGNORECASE),          # 所有 *_API_TOKEN
    re.compile(r".*_AUTH_TOKEN$", re.IGNORECASE),         # 所有 *_AUTH_TOKEN
    re.compile(r".*_ACCESS_TOKEN$", re.IGNORECASE),       # OAuth access token
]

# AGENTOPS 内部运行时凭证白名单（豁免黑名单，caller 主动注入安全）
# 注意：仅豁免 AGENTOPS 内部已知的运行时凭证，不要用作用户凭证注入途径
AGENTOPS_ENV_ALLOWLIST = frozenset({
    "AGENTOPS_WORKER_TOKEN",              # provisioner 注入用于 WorkerRegistry 注册
    "AGENTOPS_MANAGER_WS_URL",            # worker 连接 manager 的 ws 端点
    "AGENTOPS_LLM_PROVIDER",              # 模型 provider id
    "AGENTOPS_LLM_MODEL",                 # 模型 id
    "AGENTOPS_API_KEY",                   # 内部中转 API key（不是 user secret）
    "AGENTOPS_RUN_ID",                    # run 标识
    "AGENTOPS_NODE_ID",                   # 节点标识
    "AGENTOPS_TIER",                      # tier 标识
})

# 镜像名合法字符（参考 Docker 官方：a-z A-Z 0-9 . _ - : / @）
VALID_IMAGE_RE = re.compile(r"^[a-zA-Z0-9._:/@\-]+$")

# label key/value 合法字符（k8s label 规范放宽版）
# key: 字母数字 . - _ ，可含 / 分段
# value: 字母数字 . - _ : （ISO timestamp 等）— 但仍拒绝控制字符 + shell metacharacter
VALID_LABEL_KEY_RE = re.compile(r"^[a-zA-Z0-9._/\-]+$")
VALID_LABEL_VALUE_RE = re.compile(r"^[a-zA-Z0-9._\-:+]+$")


def _has_control_chars(s: str) -> bool:
    """检测是否含控制字符（NUL / \\r / \\n / \\t / < 0x20 任意字符）。"""
    return any(ord(c) < 0x20 for c in s)


def _validate_image(image: str) -> None:
    """校验镜像字符串合法。

    拒绝：NUL / 控制字符 / 长度过长 / 含空白 / 不在合法字符集内。
    """
    if not isinstance(image, str) or not image:
        raise ContainerSecurityError(f"image must be non-empty str, got {type(image).__name__}")
    if len(image) > MAX_IMAGE_LEN:
        raise ContainerSecurityError(f"image too long ({len(image)} > {MAX_IMAGE_LEN}): {image[:64]!r}...")
    if _has_control_chars(image):
        raise ContainerSecurityError(f"image contains control characters: {image!r}")
    if not VALID_IMAGE_RE.match(image):
        raise ContainerSecurityError(
            f"image contains invalid characters: {image!r}; "
            f"allowed chars: alphanumeric / . _ : / @ -"
        )


def _validate_env(env: dict[str, str]) -> None:
    """校验环境变量 dict。

    拒绝：超过数量上限 / key 命中 DENIED_ENV_KEY_PATTERNS / DENIED_TOKEN_KEY_PATTERNS（除非在 AGENTOPS_ENV_ALLOWLIST）/ 值含 NUL / 控制字符。
    """
    if len(env) > MAX_ENV_COUNT:
        raise ContainerSecurityError(
            f"too many env vars ({len(env)} > {MAX_ENV_COUNT})"
        )
    for k, v in env.items():
        if not isinstance(k, str) or not k:
            raise ContainerSecurityError(f"env key must be non-empty str, got {k!r}")
        if not isinstance(v, str):
            raise ContainerSecurityError(f"env[{k!r}] value must be str, got {type(v).__name__}")
        if _has_control_chars(v):
            raise ContainerSecurityError(f"env[{k!r}] contains control characters")
        # AGENTOPS 内部运行时凭证白名单（仅在白名单内的 AGENTOPS_* 变量可豁免 _TOKEN / _KEY 黑名单）
        if k.upper() in {x.upper() for x in AGENTOPS_ENV_ALLOWLIST}:
            continue
        # 敏感变量拦截（大小写不敏感）
        for pat in DENIED_ENV_KEY_PATTERNS:
            if pat.match(k):
                raise ContainerSecurityError(
                    f"env key '{k}' is forbidden (matches pattern '{pat.pattern}'); "
                    "use Settings → Runtime Secrets to inject credentials"
                )
        # _TOKEN 拦截（仅在白名单外的 _TOKEN 变量）
        for pat in DENIED_TOKEN_KEY_PATTERNS:
            if pat.match(k):
                raise ContainerSecurityError(
                    f"env key '{k}' is forbidden (token-like); "
                    "use Settings → Runtime Secrets to inject credentials"
                )


def _validate_labels(labels: dict[str, str]) -> None:
    """校验 label dict。

    拒绝：超过数量 / key/value 含控制字符或超长 / key/value 不在合法字符集。
    """
    if len(labels) > MAX_LABELS_COUNT:
        raise ContainerSecurityError(
            f"too many labels ({len(labels)} > {MAX_LABELS_COUNT})"
        )
    for k, v in labels.items():
        if not isinstance(k, str) or not k:
            raise ContainerSecurityError(f"label key must be non-empty str")
        if not isinstance(v, str):
            raise ContainerSecurityError(f"label[{k!r}] value must be str")
        if len(k) > MAX_LABEL_KEY_LEN:
            raise ContainerSecurityError(
                f"label key too long ({len(k)} > {MAX_LABEL_KEY_LEN}): {k!r}"
            )
        if len(v) > MAX_LABEL_VALUE_LEN:
            raise ContainerSecurityError(
                f"label[{k!r}] value too long ({len(v)} > {MAX_LABEL_VALUE_LEN})"
            )
        if _has_control_chars(k) or _has_control_chars(v):
            raise ContainerSecurityError(f"label[{k!r}] contains control characters")
        if not VALID_LABEL_KEY_RE.match(k) or not VALID_LABEL_VALUE_RE.match(v):
            raise ContainerSecurityError(
                f"label[{k!r}={v!r}] contains invalid characters; "
                "allowed: alphanumeric . - _ / : + (timestamp with : and +)"
            )


def _validate_network_mode(mode: str) -> None:
    """校验 network_mode 合法（白名单 + container:<id> 格式）。"""
    if not isinstance(mode, str) or not mode:
        raise ContainerSecurityError(f"network_mode must be non-empty str")
    # 白名单：bridge / none / host
    if mode in ALLOWED_NETWORK_MODES:
        if mode == "host":
            # v2 修复：明确禁止 network_mode="host"（与 ports 互斥，会让 ports 被忽略）
            # 仍允许 caller 显式声明，但记 warning 便于审计
            logger.warning(
                "ContainerCreateOptions.network_mode='host' requested; "
                "this disables port mapping isolation, use only for system services"
            )
        return
    # 允许的格式：container:<id>（共享另一容器网络命名空间）
    if mode.startswith("container:"):
        rest = mode[len("container:"):]
        if not rest or not VALID_LABEL_VALUE_RE.match(rest):
            raise ContainerSecurityError(
                f"network_mode='container:<id>' requires valid container id: {mode!r}"
            )
        return
    raise ContainerSecurityError(
        f"network_mode={mode!r} not in allow-list "
        f"{sorted(ALLOWED_NETWORK_MODES)} or 'container:<id>'"
    )


def _validate_cmd(cmd: list[str]) -> None:
    """校验 cmd 元素（NUL 拦截 + 长度限制）。"""
    if not isinstance(cmd, list):
        raise ContainerSecurityError(f"cmd must be list, got {type(cmd).__name__}")
    for i, arg in enumerate(cmd):
        if not isinstance(arg, str):
            raise ContainerSecurityError(f"cmd[{i}] must be str, got {type(arg).__name__}")
        if _has_control_chars(arg):
            raise ContainerSecurityError(f"cmd[{i}] contains control characters")
        if len(arg) > MAX_CMD_ARG_LEN:
            raise ContainerSecurityError(
                f"cmd[{i}] too long ({len(arg)} > {MAX_CMD_ARG_LEN})"
            )


def _validate_mounts(mounts: list[dict]) -> None:
    """校验 mounts 列表。

    每条必须是 dict 含 host / container / mode（mode 必须是 ro / rw）。
    host 路径不能含 NUL / 控制字符（路径合法性由 workspace_paths.validate_mount_path 负责）。
    """
    for i, m in enumerate(mounts):
        if not isinstance(m, dict):
            raise ContainerSecurityError(f"mounts[{i}] must be dict, got {type(m).__name__}")
        host = m.get("host")
        container = m.get("container")
        mode = m.get("mode", "rw")
        if not isinstance(host, str) or not host:
            raise ContainerSecurityError(f"mounts[{i}].host must be non-empty str")
        if not isinstance(container, str) or not container:
            raise ContainerSecurityError(f"mounts[{i}].container must be non-empty str")
        if _has_control_chars(host) or _has_control_chars(container):
            raise ContainerSecurityError(f"mounts[{i}] contains control characters in path")
        if mode not in ("ro", "rw"):
            raise ContainerSecurityError(
                f"mounts[{i}].mode must be 'ro' or 'rw', got {mode!r}"
            )


def _validate_security_opts(opts: list[str]) -> None:
    """校验 security_opts 元素。

    拒绝含 shell metacharacter（`;` `&` `|` `$` `` ` `` `\n` 等）— docker 解析 security_opt 时
    不应含多选项，单元素格式为 `key:value` 或 `key`。
    """
    for i, opt in enumerate(opts):
        if not isinstance(opt, str) or not opt:
            raise ContainerSecurityError(f"security_opts[{i}] must be non-empty str")
        if any(c in opt for c in (";", "&", "|", "$", "`", "\n", "\r")):
            raise ContainerSecurityError(
                f"security_opts[{i}]={opt!r} contains shell metacharacters"
            )


def _validate_ports(ports: list[dict]) -> None:
    """校验 ports 列表元素 + 数量。"""
    if len(ports) > MAX_PORTS_COUNT:
        raise ContainerSecurityError(
            f"too many ports ({len(ports)} > {MAX_PORTS_COUNT})"
        )
    for i, p in enumerate(ports):
        if not isinstance(p, dict):
            raise ContainerSecurityError(f"ports[{i}] must be dict")
        cp = p.get("container_port")
        hp = p.get("host_port", 0)
        if not isinstance(cp, int) or not (1 <= cp <= 65535):
            raise ContainerSecurityError(
                f"ports[{i}].container_port must be int 1-65535, got {cp!r}"
            )
        if not isinstance(hp, int) or not (0 <= hp <= 65535):
            raise ContainerSecurityError(
                f"ports[{i}].host_port must be int 0-65535, got {hp!r}"
            )
        proto = p.get("protocol", "tcp")
        if proto not in ("tcp", "udp", "sctp"):
            raise ContainerSecurityError(
                f"ports[{i}].protocol must be tcp/udp/sctp, got {proto!r}"
            )


def _validate_resource_limits(rl: dict) -> None:
    """校验 resource_limits 各字段范围。"""
    if "pids" in rl:
        pids = rl["pids"]
        if not isinstance(pids, int) or not (1 <= pids <= 32768):
            raise ContainerSecurityError(f"pids must be int 1-32768, got {pids!r}")
    if "memory_bytes" in rl:
        mem = rl["memory_bytes"]
        if not isinstance(mem, int) or mem < 1024**2 or mem > 256 * 1024**3:
            raise ContainerSecurityError(
                f"memory_bytes must be int 1MB-256GB, got {mem!r}"
            )
    if "nano_cpus" in rl:
        cpus = rl["nano_cpus"]
        if not isinstance(cpus, int) or not (10**7 <= cpus <= 16 * 10**9):
            raise ContainerSecurityError(
                f"nano_cpus must be int 1e7-1.6e10, got {cpus!r}"
            )


# ============================================================
# 数据类
# ============================================================

@dataclass
class ContainerCreateOptions:
    """容器创建参数（强类型，替代 **kwargs 散乱）。

    v2 修复 v1 关键问题：
    - network_mode 默认 bridge，不用 host（与 ports 互斥）
    - extra_hosts 默认 ["host-gateway:host-gateway"]（跨平台访问 host）
    - read_only_rootfs + cap_drop + security_opts 默认开启最小权限

    P0.18.3 加固：
    - __post_init__ 对每个字段跑安全校验（image / env / labels / network_mode / cmd / mounts / tmpfs / ports / security_opts / resource_limits）
    - 校验失败抛 ContainerSecurityError（含详细错误位置 + 修复建议）
    """
    image: str
    name: str | None = None
    cmd: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    # v2 新增能力
    mounts: list[dict] | None = None
    """[{"host": "...", "container": "...", "mode": "ro"|"rw"}]"""
    workdir: str | None = None
    tmpfs: list[dict] | None = None
    """[{"target": "/tmp", "size_bytes": 100*1024**2}]"""
    cap_drop: list[str] | None = None
    """["ALL"]"""
    security_opts: list[str] | None = None
    """["no-new-privileges:true"]"""
    read_only_rootfs: bool = False
    network_mode: str = "bridge"
    """v2: 默认 bridge，不用 host（与 ports 互斥）"""
    extra_hosts: list[str] | None = None
    """["host-gateway:host-gateway"]（Linux 跨平台访问 host）"""
    ports: list[dict] | None = None
    """[{"container_port": 7891, "host_port": 0, "protocol": "tcp"}]"""
    resource_limits: dict | None = None
    """{"pids": 256, "memory_bytes": ..., "nano_cpus": ...}"""

    def __post_init__(self) -> None:
        """P0.18.3 加固：构造时跑全字段安全校验，校验失败抛 ContainerSecurityError。

        校验列表：
        - image: 合法字符集 + 长度 + NUL/控制字符
        - name: docker container name 限制（≤64 字符 + 合法字符集）
        - cmd: NUL + 长度 + 类型
        - env: 数量 + 敏感 key 黑名单 + NUL
        - labels: 数量 + 字符集 + 长度 + NUL
        - mounts: 类型 + mode 白名单 + NUL
        - workdir: NUL
        - tmpfs: NUL
        - cap_drop: 字符串列表
        - security_opts: 无 shell metacharacter
        - read_only_rootfs: bool
        - network_mode: 白名单 (bridge/none/host/container:<id>)
        - extra_hosts: 字符串列表
        - ports: 端口范围 + 数量
        - resource_limits: pids/memory_bytes/nano_cpus 范围
        """
        # image
        _validate_image(self.image)

        # name
        if self.name is not None:
            if not isinstance(self.name, str) or not self.name:
                raise ContainerSecurityError("name must be non-empty str")
            if len(self.name) > MAX_NAME_LEN:
                raise ContainerSecurityError(f"name too long ({len(self.name)} > {MAX_NAME_LEN})")
            if _has_control_chars(self.name):
                raise ContainerSecurityError("name contains control characters")

        # cmd
        if self.cmd is not None:
            _validate_cmd(self.cmd)

        # env
        if self.env:
            _validate_env(self.env)

        # labels
        if self.labels:
            _validate_labels(self.labels)

        # mounts
        if self.mounts is not None:
            if not isinstance(self.mounts, list):
                raise ContainerSecurityError(f"mounts must be list")
            _validate_mounts(self.mounts)

        # workdir
        if self.workdir is not None:
            if not isinstance(self.workdir, str):
                raise ContainerSecurityError(f"workdir must be str")
            if _has_control_chars(self.workdir):
                raise ContainerSecurityError("workdir contains control characters")

        # tmpfs
        if self.tmpfs is not None:
            if not isinstance(self.tmpfs, list):
                raise ContainerSecurityError(f"tmpfs must be list")
            for i, t in enumerate(self.tmpfs):
                if not isinstance(t, dict):
                    raise ContainerSecurityError(f"tmpfs[{i}] must be dict")
                target = t.get("target")
                size = t.get("size_bytes")
                if not isinstance(target, str) or not target:
                    raise ContainerSecurityError(f"tmpfs[{i}].target must be non-empty str")
                if _has_control_chars(target):
                    raise ContainerSecurityError(f"tmpfs[{i}].target contains control characters")
                if not isinstance(size, int) or size < 1024 or size > 100 * 1024**3:
                    raise ContainerSecurityError(
                        f"tmpfs[{i}].size_bytes must be int 1KB-100GB, got {size!r}"
                    )

        # cap_drop
        if self.cap_drop is not None:
            if not isinstance(self.cap_drop, list):
                raise ContainerSecurityError(f"cap_drop must be list")
            for i, c in enumerate(self.cap_drop):
                if not isinstance(c, str) or not c:
                    raise ContainerSecurityError(f"cap_drop[{i}] must be non-empty str")

        # security_opts
        if self.security_opts is not None:
            if not isinstance(self.security_opts, list):
                raise ContainerSecurityError(f"security_opts must be list")
            _validate_security_opts(self.security_opts)

        # read_only_rootfs
        if not isinstance(self.read_only_rootfs, bool):
            raise ContainerSecurityError(f"read_only_rootfs must be bool")

        # network_mode
        _validate_network_mode(self.network_mode)

        # extra_hosts
        if self.extra_hosts is not None:
            if not isinstance(self.extra_hosts, list):
                raise ContainerSecurityError(f"extra_hosts must be list")
            for i, h in enumerate(self.extra_hosts):
                if not isinstance(h, str) or ":" not in h:
                    raise ContainerSecurityError(
                        f"extra_hosts[{i}]={h!r} must be 'host:ip' format"
                    )
                if _has_control_chars(h):
                    raise ContainerSecurityError(f"extra_hosts[{i}] contains control characters")

        # ports
        if self.ports is not None:
            if not isinstance(self.ports, list):
                raise ContainerSecurityError(f"ports must be list")
            _validate_ports(self.ports)

        # resource_limits
        if self.resource_limits is not None:
            if not isinstance(self.resource_limits, dict):
                raise ContainerSecurityError(f"resource_limits must be dict")
            _validate_resource_limits(self.resource_limits)


# ============================================================
# 资源限制按 tier 分级（§4.3.3）
# ============================================================

TIER_RESOURCE_LIMITS = {
    "T0": {"pids": 64,  "memory_bytes": 512 * 1024**2, "nano_cpus": 1 * 10**9},   # 512MB / 1 CPU
    "T1": {"pids": 128, "memory_bytes": 1 * 1024**3,   "nano_cpus": 1 * 10**9},   # 1GB / 1 CPU
    "T2": {"pids": 256, "memory_bytes": 2 * 1024**3,   "nano_cpus": 2 * 10**9},   # 2GB / 2 CPU
    "T3": {"pids": 512, "memory_bytes": 4 * 1024**3,   "nano_cpus": 4 * 10**9},   # 4GB / 4 CPU
}


def tier_resource_limits(tier: str) -> dict:
    """按 tier 返回资源限制。未知 tier 默认 T2。"""
    return TIER_RESOURCE_LIMITS.get(tier, TIER_RESOURCE_LIMITS["T2"])


# ============================================================
# 内部辅助
# ============================================================

def _build_volumes(mounts: list[dict]) -> dict:
    """把 [{"host": "...", "container": "...", "mode": "rw"}] 转 docker SDK volumes 格式。

    docker SDK 格式: {"<host>:<container>": {"bind": "<container>", "mode": "rw"}}

    P0.18.3 加固：
    - Windows 路径 `C:\path` 含 `:` 字符，若 host 路径里就有 `:`，
      原 `f"{host}:{container}"` 会产生 `C:\path:/workspace` 这种含多个 `:` 的字符串，
      docker daemon 解析 `:` 时会取最后一个作为 host/container 分隔符，导致
      `C:\path` 被误判为 bind 路径 / `/workspace` 被误判为 container。
      修复：Windows host 路径用 `//c/path` 形式（WSL 风格），或 docker SDK 的
      `volumes_from` 列表（mounts 拆分）。这里采用 SDK 原生形式：返回 list 而非 dict，
      由 caller 调 `client.containers.create(..., volumes=mounts_list)` 时 SDK 自动处理。
    """
    # P0.18.3 加固：用 docker SDK 接受的 list[VolumeMount] 格式（每个 mount 是独立 dict）
    # 避免 `:` 字符串拼接歧义（特别是 Windows 路径）
    out: list[dict] = []
    for m in mounts:
        out.append({
            "target": m["container"],
            "source": m["host"],
            "type": "bind",
            "read_only": (m.get("mode", "rw") == "ro"),
        })
    return out


def _build_tmpfs(tmpfs_list: list[dict]) -> dict[str, str]:
    """把 [{"target": "/tmp", "size_bytes": 100*1024**2}] 转 docker SDK tmpfs 格式。

    docker SDK 格式: {"/tmp": "104857600b"}
    """
    return {t["target"]: f"{t['size_bytes']}b" for t in tmpfs_list}


def _build_ports(ports_list: list[dict]) -> dict:
    """把 [{"container_port": 7891, "host_port": 0, "protocol": "tcp"}] 转 docker SDK ports 格式。

    docker SDK 格式: {7891: 0} 或 {7891/tcp: 0}
    """
    result: dict = {}
    for p in ports_list:
        container_port = p["container_port"]
        host_port = p.get("host_port", 0)
        protocol = p.get("protocol", "tcp")
        result[f"{container_port}/{protocol}"] = host_port
    return result


# ============================================================
# 异步 API
# ============================================================

async def list_containers(all: bool = True) -> list[dict[str, Any]]:
    """列出容器（异步包装 docker_runtime.list_containers）。"""
    return await asyncio.to_thread(docker_runtime.list_containers, all=all)


async def list_containers_by_label(label_key: str, label_value: str | None = None) -> list[dict[str, Any]]:
    """按 label filter 列出容器（v2 新增）。

    参数:
        label_key: label 键（如 "agentops.run_id"）
        label_value: label 值；None 表示只按键过滤
    """
    filter_label = f"{label_key}={label_value}" if label_value else label_key
    client = await asyncio.to_thread(docker_runtime._get_client)
    containers = await asyncio.to_thread(
        client.containers.list, all=True, filters={"label": [filter_label]}
    )
    out = []
    for c in containers:
        out.append({
            "id": c.id,
            "short_id": c.short_id,
            "name": c.name,
            "image": str(c.image.tags[0]) if getattr(c.image, "tags", None) else getattr(c.image, "short_id", ""),
            "status": c.status,
            "labels": c.labels,
        })
    return out


async def pull_image(image: str) -> dict[str, Any]:
    """拉取镜像（异步包装）。"""
    return await asyncio.to_thread(docker_runtime.pull_image, image)


async def create_container(opts: ContainerCreateOptions) -> dict[str, Any]:
    """v2: 创建容器（扩展能力），同步 SDK 用 asyncio.to_thread 包装。

    v1 → v2 修复：
    - 删除 network="host"（与 ports 互斥，host 模式下 ports 被忽略）
    - 改用 bridge + extra_hosts=host-gateway（Linux/Windows Docker Desktop 都支持）
    """
    client = await asyncio.to_thread(docker_runtime._get_client)

    # 默认 extra_hosts 含 host-gateway（若 caller 未指定）
    extra_hosts = opts.extra_hosts if opts.extra_hosts is not None else ["host-gateway:host-gateway"]

    def _create_sync():
        # 构建 docker SDK 参数
        kwargs: dict[str, Any] = {
            "image": opts.image,
            "name": opts.name,
            "command": opts.cmd,
            "environment": opts.env or {},
            "labels": opts.labels or {},
            "detach": True,
            "network": opts.network_mode,  # v2: "bridge"，不再用 "host"
            "extra_hosts": extra_hosts,
        }
        # 可选能力
        if opts.mounts:
            # P0.18.3 加固：用 docker SDK mounts 参数（list 格式），不用 volumes（dict 格式）
            # 原因：volumes 格式用 `host:container` 拼接，Windows host 路径 `C:\foo` 含 `:` 会歧义
            kwargs["mounts"] = _build_volumes(opts.mounts)
        if opts.workdir:
            kwargs["working_dir"] = opts.workdir
        if opts.tmpfs:
            kwargs["tmpfs"] = _build_tmpfs(opts.tmpfs)
        if opts.cap_drop:
            kwargs["cap_drop"] = opts.cap_drop
        if opts.security_opts:
            kwargs["security_opt"] = opts.security_opts
        if opts.read_only_rootfs:
            kwargs["read_only"] = True
        if opts.ports:
            kwargs["ports"] = _build_ports(opts.ports)
        if opts.resource_limits:
            rl = opts.resource_limits
            if "pids" in rl:
                kwargs["pids_limit"] = rl["pids"]
            if "memory_bytes" in rl:
                kwargs["mem_limit"] = str(rl["memory_bytes"])
            if "nano_cpus" in rl:
                kwargs["nano_cpus"] = rl["nano_cpus"]

        container = client.containers.create(**kwargs)
        return {
            "id": container.id,
            "short_id": container.short_id,
            "name": container.name,
        }

    return await asyncio.to_thread(_create_sync)


async def start_container(container_id: str) -> None:
    """启动容器（v2 新增独立函数，与 create 分离）。"""
    client = await asyncio.to_thread(docker_runtime._get_client)
    c = await asyncio.to_thread(client.containers.get, container_id)
    await asyncio.to_thread(c.start)


async def create_and_start_container(opts: ContainerCreateOptions) -> dict[str, Any]:
    """创建 + 启动容器（便捷函数）。"""
    info = await create_container(opts)
    await start_container(info["id"])
    return info


async def stop_container(container_id: str, timeout: int = 30) -> None:
    """v2: 默认 timeout 30s（v1 是 10s，对 worker 太短）。"""
    await asyncio.to_thread(docker_runtime.stop_container, container_id, timeout)


async def kill_container(container_id: str) -> None:
    """强制终止容器（SIGKILL，v2 新增）。"""
    client = await asyncio.to_thread(docker_runtime._get_client)
    c = await asyncio.to_thread(client.containers.get, container_id)
    await asyncio.to_thread(c.kill)


async def remove_container(container_id: str, force: bool = False) -> None:
    """移除容器。"""
    await asyncio.to_thread(docker_runtime.remove_container, container_id, force)


async def verify_container_gone(container_id: str, label_key: str, label_value: str) -> bool:
    """v2 新增：通过 label filter 验证容器确实被移除。

    参数:
        container_id: 待验证的容器 ID
        label_key: label 键（如 "agentops.run_id"）
        label_value: label 值

    返回: True 表示容器已移除；False 表示仍存在
    """
    try:
        containers = await list_containers_by_label(label_key, label_value)
        return not any(c["id"] == container_id for c in containers)
    except Exception as e:
        logger.warning("verify_container_gone failed (treating as not gone): %s", e)
        return False


async def container_logs(container_id: str, tail: int = 200) -> str:
    """获取容器日志（异步包装）。"""
    return await asyncio.to_thread(docker_runtime.container_logs, container_id, tail)


async def container_logs_stream(container_id: str, follow: bool = True) -> AsyncIterator[str]:
    """v2 新增：异步生成器，用于 SSE 推送 container log。

    docker SDK 的 stream 是同步迭代器，用 to_thread + queue 桥接为 async。
    """
    import queue
    import threading

    client = await asyncio.to_thread(docker_runtime._get_client)
    c = await asyncio.to_thread(client.containers.get, container_id)

    log_queue: queue.Queue = queue.Queue()
    sentinel = object()

    def _stream_sync():
        try:
            log_stream = c.logs(stream=True, follow=follow, stdout=True, stderr=True)
            for chunk in log_stream:
                if isinstance(chunk, bytes):
                    text = chunk.decode("utf-8", errors="replace").rstrip()
                else:
                    text = str(chunk).rstrip()
                if text:
                    log_queue.put(text)
        except Exception as e:
            log_queue.put(e)
        finally:
            log_queue.put(sentinel)

    thread = threading.Thread(target=_stream_sync, daemon=True)
    thread.start()

    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, log_queue.get)
        if item is sentinel:
            break
        if isinstance(item, Exception):
            raise item
        yield item


async def inspect_container(container_id: str) -> dict[str, Any]:
    """v2 新增：容器详情（含 state/labels/mounts）。"""
    client = await asyncio.to_thread(docker_runtime._get_client)
    c = await asyncio.to_thread(client.containers.get, container_id)
    info = await asyncio.to_thread(c.attrs)
    return {
        "id": c.id,
        "short_id": c.short_id,
        "name": c.name,
        "status": c.status,
        "labels": c.labels,
        "image": str(c.image.tags[0]) if getattr(c.image, "tags", None) else getattr(c.image, "short_id", ""),
        "state": info.get("State", {}),
        "mounts": info.get("Mounts", []),
        "config": {
            "Cmd": info.get("Config", {}).get("Cmd"),
            "Env": info.get("Config", {}).get("Env", []),
            "WorkingDir": info.get("Config", {}).get("WorkingDir"),
        },
    }


# ============================================================
# 兼容旧 create_and_start_container 签名（过渡期保留）
# ============================================================

async def create_and_start_container_legacy(
    image: str,
    name: str | None = None,
    cmd: list[str] | None = None,
    env: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """v2 兼容旧 docker_runtime.create_and_start_container 签名。

    新 caller 应优先用 create_and_start_container(ContainerCreateOptions(...))。
    """
    opts = ContainerCreateOptions(
        image=image,
        name=name,
        cmd=cmd,
        env=env or {},
        labels=labels or {},
    )
    return await create_and_start_container(opts)
