"""P0.17/P0.18 runtime environment status aggregator.

集中查询 Docker daemon / agentops-worker 镜像 / 源码指纹 / 活跃 subagent，
供前端 Runtime Settings 页面展示。

状态机定义见 docs/p017-runtime-environment-panel.md §二。
镜像重命名（P0.18）：codex-node → agentops-worker（不再绑定具体 harness）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    import docker
    from docker.errors import APIError, DockerException, NotFound
except Exception:  # pragma: no cover - runtime optional
    docker = None  # type: ignore
    APIError = DockerException = NotFound = Exception  # type: ignore

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────

WORKER_IMAGE_LABEL = "agentops.kind=agentops-worker"
WORKER_IMAGE_TAG_LATEST = "agentops-worker:latest"
PROTOCOL_VERSION = "0.1.0"
WORKER_IMAGE_VERSION = "0.1.0"


# ─────────────────────────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_client() -> Any:
    """获取 docker 客户端，失败抛 RuntimeError 携带 reason_code。"""
    if docker is None:
        raise RuntimeError("docker SDK not installed; pip install docker>=7.1.0,<8")
    try:
        return docker.from_env()
    except DockerException as e:
        # 区分 daemon 不可达 vs 其他
        msg = str(e).lower()
        if "permission denied" in msg or "access is denied" in msg:
            raise RuntimeError("docker_permission_denied: cannot access docker socket") from e
        if "cannot connect" in msg or "named pipe" in msg or "no such file" in msg or "docker daemon" in msg:
            raise RuntimeError("docker_daemon_unavailable: docker daemon not reachable") from e
        raise RuntimeError(f"docker_error: {e}") from e


def _detect_docker_cli_missing() -> bool:
    """检查 docker CLI 是否在 PATH（daemon 不可达时区分别）。"""
    from shutil import which
    return which("docker") is None


def compute_source_fingerprint(repo_root: str) -> str:
    """返回 git HEAD 短 SHA（16 位）。失败时返回 "unknown"。"""
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:16]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


def _is_git_clean(repo_root: str) -> bool:
    """git status 干净返回 True。失败返回 False（dirty 兜底）。"""
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        return out.returncode == 0 and not out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _project_root() -> str:
    """定位 AgentOps 项目根（runtime_environment.py 在 orchestrator/ 下）。"""
    here = Path(__file__).resolve()
    return str(here.parent.parent)  # orchestrator/ → AgentOps/


# ─────────────────────────────────────────────────────────────
# 1. Docker daemon 状态
# ─────────────────────────────────────────────────────────────

def get_docker_status() -> Dict[str, Any]:
    """检查 Docker daemon 健康。

    返回结构参考 docs/p017-runtime-environment-panel.md §3.1。
    """
    result: Dict[str, Any] = {
        "status": "unknown",
        "version": None,
        "platform": None,
        "reason_code": None,
        "reason": None,
    }

    # 先检查 CLI 是否在 PATH
    if _detect_docker_cli_missing():
        result["status"] = "error"
        result["reason_code"] = "docker_cli_missing"
        result["reason"] = "Docker CLI 未安装或不在 PATH"
        return result

    try:
        client = _get_client()
        version_info = client.version()
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("docker_permission_denied"):
            result["status"] = "error"
            result["reason_code"] = "docker_permission_denied"
            result["reason"] = "Docker 权限被拒绝（检查 docker.sock / docker 组）"
        elif msg.startswith("docker_daemon_unavailable"):
            result["status"] = "error"
            result["reason_code"] = "docker_daemon_unavailable"
            result["reason"] = "Docker daemon 未启动"
        else:
            result["status"] = "error"
            result["reason_code"] = "docker_error"
            result["reason"] = msg
        return result
    except Exception as e:
        result["status"] = "error"
        result["reason_code"] = "docker_error"
        result["reason"] = str(e)
        return result

    # Windows Docker Desktop 默认是 linux 容器，但 platform 字段是 dict
    platform_raw = version_info.get("Platform") or version_info.get("Os") or ""
    if isinstance(platform_raw, dict):
        platform_str = f"{platform_raw.get('Name', '')}/{platform_raw.get('Arch', '')}"
    else:
        arch = version_info.get("Arch", "")
        platform_str = f"{platform_raw}/{arch}" if arch else str(platform_raw)

    # Windows 上如果用的是 Windows 容器引擎（不是 Linux 容器），提示
    if os.name == "nt" and platform_raw and "windows" in str(platform_raw).lower() and "linux" not in str(platform_raw).lower():
        result["status"] = "error"
        result["reason_code"] = "docker_linux_engine_required"
        result["reason"] = "Windows 下需切换到 Linux 容器（在 Docker Desktop Settings 中切换）"
        result["version"] = version_info.get("Version")
        result["platform"] = platform_str
        return result

    result["status"] = "ready"
    result["version"] = version_info.get("Version")
    result["platform"] = platform_str
    return result


# ─────────────────────────────────────────────────────────────
# 2. agentops-worker 镜像状态
# ─────────────────────────────────────────────────────────────

def _list_worker_images(client: Any) -> List[Dict[str, Any]]:
    """列出所有 agentops.kind=agentops-worker 镜像。

    兼容旧 codex-node 镜像：filter 同时匹配两个 label，便于迁移期发现旧镜像。
    """
    out: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for label_value in ("agentops-worker", "codex-node"):
        try:
            images = client.images.list(filters={"label": f"agentops.kind={label_value}"})
        except (DockerException, APIError) as e:
            logger.warning("list %s images failed: %s", label_value, e)
            continue
        for img in images:
            if img.id in seen_ids:
                continue
            seen_ids.add(img.id)
            labels = img.labels or {}
            tags = img.tags or []
            out.append({
                "id": img.id,
                "short_id": img.short_id,
                "tags": tags,
                "source_fingerprint": labels.get("agentops.source_fingerprint", "unknown"),
                "protocol_version": labels.get("agentops.protocol_version", "unknown"),
                "version": labels.get("agentops.version", "unknown"),
                "built_at": labels.get("agentops.built_at", ""),
                "size_bytes": img.attrs.get("Size", 0),
                "created_at": img.attrs.get("Created", ""),
                "labels": labels,
                "legacy": label_value == "codex-node",  # 标识旧镜像（迁移期）
            })
    # 排序优先用 built_at（每次构建都更新），fallback 用 created_at（镜像层时间，CACHED 时不变）
    if out:
        out.sort(
            key=lambda x: x.get("built_at") or x.get("created_at") or "",
            reverse=True,
        )
        out[0]["selected"] = True
        for x in out[1:]:
            x["selected"] = False
    return out


def get_worker_image_status() -> Dict[str, Any]:
    """检查 agentops-worker 镜像状态（ready / stale / missing / incompatible）。

    返回结构参考 docs/p017-runtime-environment-panel.md §3.1。
    迁移期：同时识别 legacy codex-node 镜像并标记 legacy=True。
    """
    repo_root = _project_root()
    current_fingerprint = compute_source_fingerprint(repo_root)

    base: Dict[str, Any] = {
        "status": "checking",
        "image_id": None,
        "tag": None,
        "version": None,
        "protocol_version": None,
        "source_fingerprint": current_fingerprint,
        "compatibility": "unknown",
        "reason_code": None,
        "reason": None,
    }

    try:
        client = _get_client()
    except RuntimeError as e:
        base["status"] = "unknown"
        base["reason_code"] = "docker_error"
        base["reason"] = str(e)
        return base

    images = _list_worker_images(client)
    if not images:
        base["status"] = "missing"
        base["reason_code"] = "worker_image_missing"
        base["reason"] = "未找到 agentops-worker 镜像，请点击「重新构建」"
        return base

    # 挑 selected（最新创建的）
    selected = next((i for i in images if i.get("selected")), images[0])
    base["image_id"] = selected["id"]
    base["tag"] = selected["tags"][0] if selected["tags"] else WORKER_IMAGE_TAG_LATEST
    base["version"] = selected["version"]
    base["protocol_version"] = selected["protocol_version"]

    # 协议版本不匹配 → incompatible
    if selected["protocol_version"] != PROTOCOL_VERSION:
        base["status"] = "incompatible"
        base["compatibility"] = "incompatible"
        base["reason_code"] = "worker_image_incompatible"
        base["reason"] = (
            f"镜像协议版本 {selected['protocol_version']} ≠ 当前 {PROTOCOL_VERSION}"
        )
        return base

    # 源码指纹不一致 → stale
    if selected["source_fingerprint"] != current_fingerprint and current_fingerprint != "unknown":
        base["status"] = "stale"
        base["compatibility"] = "stale"
        base["reason_code"] = "worker_image_stale"
        base["reason"] = (
            f"源码指纹 {current_fingerprint[:10]}… 与镜像 {selected['source_fingerprint'][:10]}… 不一致"
        )
        return base

    base["status"] = "ready"
    base["compatibility"] = "current"
    return base


# ─────────────────────────────────────────────────────────────
# 3. 活跃 subagent 列表
# ─────────────────────────────────────────────────────────────

async def get_connected_workers(event_store: Any | None) -> Dict[str, Any]:
    """查询所有 status='active' 的 subagent 运行实例。

    event_store: audit/store.py 的 EventStore 实例，可以为 None（front 端降级）。
    """
    if event_store is None:
        return {"workers": [], "count": 0}

    try:
        # EventStore.list_subagents_for_run 不带 run_id 过滤；这里直接查 DB
        # 用 list_subagents_for_run 拼接或新方法。最简单方案：list 全部 active subagent。
        # 复用 audit._exec 私有方法或暴露新查询。
        # 这里用 list_subagents_for_run(active=True) 简化版，按需求可扩展。
        rows = await event_store.list_active_subagents() if hasattr(
            event_store, "list_active_subagents"
        ) else []
    except Exception as e:
        logger.warning("get_connected_workers failed: %s", e)
        return {"workers": [], "count": 0, "error": str(e)}

    workers = []
    for r in rows:
        workers.append({
            "subagent_id": r.get("subagent_id"),
            "worker_id": r.get("worker_id"),
            "runtime_placement": r.get("runtime_placement"),
            "container_id": r.get("container_id"),
            "status": r.get("worker_status") or r.get("subagent_status"),
            "started_at": r.get("worker_started_at") or r.get("subagent_started_at"),
            "lease_generation": r.get("lease_generation"),
            "run_id": r.get("run_id"),
            "node_id": r.get("node_id"),
            "actor_id": r.get("actor_id"),
            "workspace_id": r.get("worker_workspace_id"),
            "tier": r.get("worker_tier"),
        })
    return {"workers": workers, "count": len(workers)}


# ─────────────────────────────────────────────────────────────
# 4. 源码状态
# ─────────────────────────────────────────────────────────────

def get_source_status() -> Dict[str, Any]:
    repo_root = _project_root()
    return {
        "available": (Path(repo_root) / ".git").exists() or compute_source_fingerprint(repo_root) != "unknown",
        "fingerprint": compute_source_fingerprint(repo_root),
        "git_status": "clean" if _is_git_clean(repo_root) else "dirty",
    }


# ─────────────────────────────────────────────────────────────
# 5. 镜像构建
# ─────────────────────────────────────────────────────────────

class BuildRegistry:
    """内存里的 build 状态注册表（单进程）。"""

    def __init__(self) -> None:
        self._builds: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create(self) -> str:
        async with self._lock:
            for bid, b in self._builds.items():
                if b["status"] in ("queued", "running"):
                    return bid  # 已有 build，返回旧的 id
            bid = str(uuid.uuid4())
            self._builds[bid] = {
                "build_id": bid,
                "status": "queued",
                "logs": [],
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
            }
            return bid

    async def start(self, build_id: str) -> None:
        async with self._lock:
            if build_id in self._builds:
                self._builds[build_id]["status"] = "running"
                self._builds[build_id]["started_at"] = _now_iso()

    async def append_log(self, build_id: str, line: str) -> None:
        async with self._lock:
            if build_id in self._builds:
                self._builds[build_id]["logs"].append({"line": line, "ts": _now_iso()})

    async def finish(self, build_id: str, exit_code: int) -> None:
        async with self._lock:
            if build_id in self._builds:
                self._builds[build_id]["status"] = "completed" if exit_code == 0 else "failed"
                self._builds[build_id]["exit_code"] = exit_code
                self._builds[build_id]["finished_at"] = _now_iso()

    async def get(self, build_id: str) -> Dict[str, Any] | None:
        async with self._lock:
            b = self._builds.get(build_id)
            if not b:
                return None
            return {
                "build_id": b["build_id"],
                "status": b["status"],
                "logs": list(b["logs"]),
                "started_at": b["started_at"],
                "finished_at": b["finished_at"],
                "exit_code": b["exit_code"],
            }

    async def latest_active(self) -> Dict[str, Any] | None:
        async with self._lock:
            for b in self._builds.values():
                if b["status"] in ("queued", "running"):
                    return {
                        "build_id": b["build_id"],
                        "status": b["status"],
                        "started_at": b["started_at"],
                        "finished_at": b["finished_at"],
                        "exit_code": b["exit_code"],
                    }
            return None


build_registry = BuildRegistry()


async def build_worker_image(
    build_id: str,
    on_log: Optional[Callable[[str], Awaitable[None]]] = None,
    force: bool = False,
) -> int:
    """执行 docker build，广播日志到 on_log。返回 exit_code。

    Dockerfile 路径: docker/agentops-worker/Dockerfile（项目根相对）。
    镜像 tag: 仅 agentops-worker:latest（不打哈希 tag，避免孤儿 tag 积累；
    版本审计信息走 label: agentops.source_fingerprint / built_at）
    labels: agentops.kind=agentops-worker / version / protocol_version / source_fingerprint / built_at
    """
    repo_root = _project_root()
    source_fingerprint = compute_source_fingerprint(repo_root)
    dockerfile = Path(repo_root) / "docker" / "agentops-worker" / "Dockerfile"
    if not dockerfile.exists():
        raise RuntimeError(f"Dockerfile 不存在: {dockerfile}")

    tag_latest = WORKER_IMAGE_TAG_LATEST

    labels = {
        "agentops.kind": "agentops-worker",
        "agentops.version": WORKER_IMAGE_VERSION,
        "agentops.protocol_version": PROTOCOL_VERSION,
        "agentops.source_fingerprint": source_fingerprint,
        "agentops.built_at": _now_iso(),
    }
    label_args: List[str] = []
    for k, v in labels.items():
        label_args.extend(["--label", f"{k}={v}"])

    cmd = [
        "docker", "build",
        "-f", str(dockerfile),
        "-t", tag_latest,
        *label_args,
        repo_root,
    ]

    await build_registry.start(build_id)

    async def _log(line: str) -> None:
        await build_registry.append_log(build_id, line)
        if on_log:
            await on_log(line)

    await _log(f"$ {' '.join(cmd)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=repo_root,
        )
    except FileNotFoundError:
        await _log("错误: docker CLI 未找到")
        await build_registry.finish(build_id, 127)
        return 127

    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        await _log(text)

    exit_code = await proc.wait()
    await build_registry.finish(build_id, exit_code)
    return exit_code


# ─────────────────────────────────────────────────────────────
# 6. 顶层聚合
# ─────────────────────────────────────────────────────────────

def compute_overall(
    docker_status: Dict[str, Any],
    worker_image: Dict[str, Any],
    build: Dict[str, Any],
) -> str:
    """综合状态优先级：docker_error > build_failed > incompatible > missing > stale > building > ready。"""
    if docker_status["status"] == "error":
        return "docker_error"
    if build["status"] in ("queued", "running"):
        return "building"
    if worker_image["status"] == "incompatible":
        return "incompatible"
    if worker_image["status"] == "missing":
        return "missing"
    if worker_image["status"] == "stale":
        return "stale"
    if worker_image["status"] == "build_failed":
        return "build_failed"
    if worker_image["status"] == "ready":
        return "ready"
    return "checking"


async def get_environment_snapshot(event_store: Any | None = None) -> Dict[str, Any]:
    """顶层：聚合 docker + worker_image + images + workers + source + build + overall。"""
    docker_status = get_docker_status()
    worker_image = get_worker_image_status()
    images = []
    if docker_status["status"] == "ready":
        try:
            client = _get_client()
            images = _list_worker_images(client)
        except Exception:
            images = []
    source = get_source_status()
    workers = await get_connected_workers(event_store)
    build_summary = await build_registry.latest_active() or {
        "build_id": None,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    }
    overall = compute_overall(docker_status, worker_image, build_summary)
    return {
        "docker": docker_status,
        "worker_image": worker_image,
        "images": images,
        "source": source,
        "build": build_summary,
        "connected_workers": workers["count"],
        "workers": workers["workers"],
        "overall": overall,
    }
