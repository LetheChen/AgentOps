"""Lightweight Docker runtime adapter using the official docker SDK.

Provides helper functions used by api.server to list/pull/create/stop/remove containers
and to fetch logs. The implementation is defensive: if the docker SDK is not installed or
Docker daemon is not reachable, functions raise RuntimeError with diagnostic messages.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    import docker
    from docker.errors import DockerException
except Exception as e:  # pragma: no cover - runtime optional
    docker = None  # type: ignore
    DockerException = Exception  # type: ignore


def _get_client():
    if docker is None:
        raise RuntimeError("docker SDK not installed; please install 'docker' package")
    try:
        return docker.from_env()
    except DockerException as e:
        raise RuntimeError(f"failed to connect to Docker daemon: {e}")


def list_containers(all: bool = True) -> List[Dict[str, Any]]:
    client = _get_client()
    try:
        containers = client.containers.list(all=all)
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
    except DockerException as e:
        raise RuntimeError(f"docker list_containers failed: {e}")


def pull_image(image: str) -> Dict[str, Any]:
    client = _get_client()
    try:
        res = client.images.pull(image)
        return {"image_id": getattr(res, "id", None), "tags": getattr(res, "tags", [])}
    except DockerException as e:
        raise RuntimeError(f"docker pull failed: {e}")


def create_and_start_container(image: str, name: Optional[str] = None, cmd: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None, labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    client = _get_client()
    try:
        container = client.containers.create(image=image, name=name, command=cmd, environment=env or {}, labels=labels or {}, detach=True)
        container.start()
        return {"id": container.id, "short_id": container.short_id, "name": container.name}
    except DockerException as e:
        raise RuntimeError(f"docker create/start failed: {e}")


def stop_container(container_id: str, timeout: int = 10) -> None:
    client = _get_client()
    try:
        c = client.containers.get(container_id)
        c.stop(timeout=timeout)
    except DockerException as e:
        raise RuntimeError(f"docker stop failed: {e}")


def remove_container(container_id: str, force: bool = False) -> None:
    client = _get_client()
    try:
        c = client.containers.get(container_id)
        c.remove(force=force)
    except DockerException as e:
        raise RuntimeError(f"docker remove failed: {e}")


def container_logs(container_id: str, tail: int = 200) -> str:
    client = _get_client()
    try:
        c = client.containers.get(container_id)
        logs = c.logs(tail=tail, stderr=True, stdout=True)
        if isinstance(logs, bytes):
            try:
                return logs.decode("utf-8", errors="replace")
            except Exception:
                return str(logs)
        return str(logs)
    except DockerException as e:
        raise RuntimeError(f"docker logs failed: {e}")


def container_exists(container_id: str) -> bool:
    """检查容器是否仍然存在。

    用于 remove 后验证容器是否真的被清理。
    """
    client = _get_client()
    try:
        client.containers.get(container_id)
        return True
    except DockerException:
        return False


def cleanup_orphan_worker_containers() -> List[Dict[str, str]]:
    """清扫孤儿 worker 容器（后端启动时调用）。

    背景：DagEngine 在进程内执行，后端进程被强杀（stop.ps1 / 崩溃）时
    正在执行的 run 来不及走 finally 清理，容器泄漏残留。

    判定：进程重启后所有 worker 容器都是孤儿（resume 会重建容器，不复用旧的）。
    识别规则（满足其一）：
      - 容器名以 ``ao_`` 开头（engine 旧路径 ao_{run_id}_{node}_{gen}
        与 provisioner 路径 ao_ao-...-L{n} 都是此前缀）
      - label agentops.kind == "agentops-worker"

    Returns:
        已清理的容器列表 [{id, name, removed}]；docker 不可用时抛 RuntimeError，
        调用方需自行容错。
    """
    cleaned: List[Dict[str, str]] = []
    for c in list_containers(all=True):
        name = c.get("name", "")
        labels = c.get("labels") or {}
        is_worker = name.startswith("ao_") or labels.get("agentops.kind") == "agentops-worker"
        if not is_worker:
            continue
        cid = c["id"]
        try:
            stop_container(cid)
        except Exception:
            pass  # 已退出/已停止的容器 stop 会报错，忽略
        try:
            remove_container(cid, True)
            cleaned.append({"id": cid, "name": name, "removed": "true"})
        except Exception as e:
            cleaned.append({"id": cid, "name": name, "removed": f"failed: {e}"})
    return cleaned
