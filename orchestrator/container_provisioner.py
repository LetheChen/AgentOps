"""P0.18.4 容器 provisioner：5 步启动 + 4 步销毁 + label 治理 + wait_registration。

设计依据：[docs/audit/p018-workspace-authorization-and-docker-lifecycle.md](docs/audit/p018-workspace-authorization-and-docker-lifecycle.md) §4.3

5 步启动：
1. build mount list（workspace.mode 决定）
2. build labels（治理 + 检索）
3. build env（连接信息 + LLM 配置，不含凭据明文）
4. create + start container
5. wait WS connect 30s + record_provisioned_worker

4 步销毁：
1. graceful stop（SIGTERM, 30s timeout）
2. kill（SIGKILL，若 stop 失败 + force=True）
3. remove（force=True）
4. verify_container_gone + update_subagent_status

v2 修复 v1 关键问题：
- worker 注册改 WS connect 事件（不再 HTTP 轮询）
- API key 通过 WS encrypted message 传（不进 env）
- sandbox 延迟清理（run 结束不立即清理，patroller 每日扫）
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from orchestrator import docker_provider
from orchestrator.docker_provider import ContainerCreateOptions, tier_resource_limits
from orchestrator.workspace_paths import (
    WorkspaceInfo,
    PreparedWorkspace,
    build_container_mounts,
)

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

PROTOCOL_VERSION = "p018-v1"
MANAGER_PORT = int(os.environ.get("AGENTOPS_MANAGER_PORT", "1987"))
DEFAULT_WORKER_REGISTRATION_TIMEOUT = 30  # 秒
DEFAULT_GRACEFUL_STOP_TIMEOUT = 30         # 秒
SANDBOX_RETENTION_DAYS = 30                # sandbox 保留 30 天


# ============================================================
# 数据类
# ============================================================

@dataclass
class ProvisionRequest:
    """容器 provision 请求。"""
    run_id: str
    node_id: str
    subagent_id: str
    lease_generation: int
    workspace: WorkspaceInfo
    prepared: PreparedWorkspace
    agent_tier: str                             # T0/T1/T2/T3
    image: str
    resolved_model: dict[str, Any]              # {provider, model, api_key, ...}
    agent_type: str = "generic"
    extra_env: dict[str, str] | None = None
    extra_volumes: list[dict] | None = None
    worker_port: int = 7891                     # worker 监听端口


@dataclass
class ProvisionResult:
    """容器 provision 结果。"""
    container_id: str
    worker_id: str
    container_name: str
    registered: bool                            # WS 是否在超时内注册
    workspace_id: str
    tier: str


# ============================================================
# Label 治理
# ============================================================

def build_container_labels(
    run_id: str,
    node_id: str,
    subagent_id: str,
    lease_generation: int,
    workspace_id: str | None,
    workspace_mode: str,
    tier: str,
) -> dict[str, str]:
    """构建容器 labels（治理 + 检索）。"""
    labels = {
        "agentops.kind": "agentops-worker",
        "agentops.run_id": run_id,
        "agentops.node_id": node_id,
        "agentops.subagent_id": subagent_id,
        "agentops.lease_generation": str(lease_generation),
        "agentops.workspace_mode": workspace_mode,
        "agentops.tier": tier,
        "agentops.protocol_version": PROTOCOL_VERSION,
        "agentops.built_at": _now_iso(),
    }
    if workspace_id:
        labels["agentops.workspace_id"] = workspace_id
    return labels


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_iso_plus(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ============================================================
# Worker ID 生成
# ============================================================

def generate_worker_id(run_id: str, node_id: str, lease_generation: int) -> str:
    """生成 worker_id：ao-{run_id}-{node_id}-L{lease_generation}。"""
    return f"ao-{run_id}-{node_id}-L{lease_generation}"


# ============================================================
# ContainerProvisioner
# ============================================================

class ContainerProvisioner:
    """5 步启动 + 4 步销毁容器，集成 workspace 授权 + tier 资源限制 + WS 注册等待。

    依赖：
    - docker_provider：异步 docker SDK 封装
    - workspace_paths：mount 列表生成
    - EventStore：record_provisioned_worker / update_subagent_status / mark_sandbox_for_cleanup
    - worker_registry：WS 注册等待（P0.18.6 实现，这里用接口注入）
    """

    def __init__(
        self,
        event_store,
        worker_registry=None,
        manager_port: int | None = None,
    ):
        """
        参数:
            event_store: EventStore 实例
            worker_registry: WorkerRegistry 实例（P0.18.6）；None 时跳过 WS 等待
            manager_port: manager HTTP/WS 端口（默认从环境变量读）
        """
        self._store = event_store
        self._worker_registry = worker_registry
        self._manager_port = manager_port or MANAGER_PORT

    # ============================================================
    # 5 步启动
    # ============================================================

    async def provision(self, req: ProvisionRequest) -> ProvisionResult:
        """5 步启动容器。

        步骤：
        1. build mount list（workspace.mode 决定）
        2. build labels
        3. build env
        4. create + start container
        5. wait WS connect 30s + record_provisioned_worker

        失败时自动回滚（deprovision）。
        """
        worker_id = generate_worker_id(req.run_id, req.node_id, req.lease_generation)
        container_name = f"ao_{worker_id}"

        # 步骤 1: build mount list
        mounts = build_container_mounts(
            workspace=req.workspace,
            prepared=req.prepared,
            extra_volumes=req.extra_volumes,
        )

        # 步骤 2: build labels
        labels = build_container_labels(
            run_id=req.run_id,
            node_id=req.node_id,
            subagent_id=req.subagent_id,
            lease_generation=req.lease_generation,
            workspace_id=req.workspace.workspace_id,
            workspace_mode=req.workspace.mode,
            tier=req.agent_tier,
        )

        # 步骤 3: build env（连接信息 + LLM 配置，不含凭据明文）
        env = self._build_env(req, worker_id)
        if req.extra_env:
            env.update(req.extra_env)

        # 步骤 4: create + start container
        opts = ContainerCreateOptions(
            image=req.image,
            name=container_name,
            env=env,
            labels=labels,
            mounts=mounts,
            workdir="/workspace",
            network_mode="bridge",  # v2: 不用 host
            extra_hosts=["host-gateway:host-gateway"],
            ports=[{"container_port": req.worker_port, "host_port": 0, "protocol": "tcp"}],
            resource_limits=tier_resource_limits(req.agent_tier),
            read_only_rootfs=True,
            cap_drop=["ALL"],
            security_opts=["no-new-privileges:true"],
            tmpfs=[{"target": "/tmp", "size_bytes": 100 * 1024**2}],
        )

        container_info: dict[str, Any] | None = None
        try:
            container_info = await docker_provider.create_container(opts)
            await docker_provider.start_container(container_info["id"])
        except Exception as e:
            # Bug 修复：start_container 失败后清理已创建的容器，避免泄漏
            logger.error("provision create/start failed for worker %s: %s", worker_id, e)
            if container_info and container_info.get("id"):
                try:
                    await self.deprovision(
                        container_id=container_info["id"],
                        workspace=req.workspace,
                        subagent_id=req.subagent_id,
                        run_id=req.run_id,
                        force=True,
                    )
                except Exception as cleanup_err:
                    logger.error("cleanup after start failure also failed for worker %s: %s", worker_id, cleanup_err)
            raise

        # 步骤 5: wait WS connect + record
        registered = False
        if self._worker_registry:
            try:
                registered = await self._wait_worker_ws_connect(
                    worker_id, timeout=DEFAULT_WORKER_REGISTRATION_TIMEOUT
                )
                if not registered:
                    # WS 超时，回滚
                    await self.deprovision(
                        container_id=container_info["id"],
                        workspace=req.workspace,
                        subagent_id=req.subagent_id,
                        run_id=req.run_id,
                        force=True,
                    )
                    raise RuntimeError(
                        f"worker {worker_id} did not register via WS within {DEFAULT_WORKER_REGISTRATION_TIMEOUT}s"
                    )
            except Exception as e:
                logger.error("worker %s WS wait failed: %s", worker_id, e)
                await self.deprovision(
                    container_id=container_info["id"],
                    workspace=req.workspace,
                    subagent_id=req.subagent_id,
                    run_id=req.run_id,
                    force=True,
                )
                raise

        # record_provisioned_worker（v2 扩展 workspace_id + tier）
        # Bug 修复：record/update 失败后清理已启动的容器，避免泄漏
        try:
            await self._store.record_provisioned_worker(
                subagent_id=req.subagent_id,
                lease_generation=req.lease_generation,
                worker_id=worker_id,
                runtime_placement="docker_container",
                container_id=container_info["id"],
                workspace_id=req.workspace.workspace_id,
                tier=req.agent_tier,
            )
            await self._store.update_subagent_status(req.subagent_id, "running")
        except Exception as e:
            logger.error("record_provisioned_worker failed for worker %s: %s", worker_id, e)
            await self.deprovision(
                container_id=container_info["id"],
                workspace=req.workspace,
                subagent_id=req.subagent_id,
                run_id=req.run_id,
                force=True,
            )
            raise

        return ProvisionResult(
            container_id=container_info["id"],
            worker_id=worker_id,
            container_name=container_name,
            registered=registered,
            workspace_id=req.workspace.workspace_id,
            tier=req.agent_tier,
        )

    def _build_env(self, req: ProvisionRequest, worker_id: str) -> dict[str, str]:
        """构建容器 env（连接信息 + LLM 配置，不含凭据明文）。"""
        # v2: worker 通过 host-gateway 访问 manager（bridge 网络）
        ws_url = f"ws://host-gateway:{self._manager_port}/ws/projects/default/workers/{worker_id}"
        env = {
            "AGENTOPS_MANAGER_WS_URL": ws_url,
            "AGENTOPS_WORKER_ID": worker_id,
            # v2: worker_token 是一次性短期 token（5min TTL），由 manager 签发
            # 实际 token 生成在 P0.18.6 worker_token.py 实现，这里只占位
            "AGENTOPS_WORKER_TOKEN": _placeholder_worker_token(worker_id),
            "AGENTOPS_LLM_PROVIDER": req.resolved_model.get("provider", ""),
            "AGENTOPS_LLM_MODEL": req.resolved_model.get("model", ""),
            # v2: api_key 由 manager 通过 WS encrypted message 传，不进 env
            "AGENTOPS_WORKSPACE_MOUNT": "/workspace",  # 硬编码
            "AGENTOPS_AGENT_TYPE": req.agent_type,
            "AGENTOPS_AGENT_TIER": req.agent_tier,
            "AGENTOPS_RUN_ID": req.run_id,
            "AGENTOPS_NODE_ID": req.node_id,
        }
        return env

    async def _wait_worker_ws_connect(
        self, worker_id: str, timeout: float
    ) -> bool:
        """等 worker WS connect 事件。

        P0.18.6 实现 worker_registry 后，这里调 worker_registry.wait_registered。
        本期（P0.18.4）若 worker_registry=None，跳过等待返回 False。
        """
        if not self._worker_registry:
            logger.warning("worker_registry not configured, skipping WS wait for %s", worker_id)
            return False
        try:
            await asyncio.wait_for(
                self._worker_registry.wait_registered(worker_id),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning("worker %s WS connect timeout (%ss)", worker_id, timeout)
            return False
        except Exception as e:
            logger.error("worker %s WS wait error: %s", worker_id, e)
            return False

    # ============================================================
    # 4 步销毁
    # ============================================================

    async def deprovision(
        self,
        container_id: str,
        workspace: WorkspaceInfo,
        subagent_id: str,
        run_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """4 步销毁容器 + sandbox 延迟清理标记。

        步骤：
        1. graceful stop（SIGTERM, 30s timeout）
        2. kill（SIGKILL，若 stop 失败 + force=True）
        3. remove（force=True）
        4. verify_container_gone + update_subagent_status + mark_sandbox_for_cleanup

        v2 修复：sandbox 不在此处清理，由 patroller 延迟清理（保留 30 天）
        """
        stopped_cleanly = True

        # 步骤 1: graceful stop
        try:
            await docker_provider.stop_container(container_id, timeout=DEFAULT_GRACEFUL_STOP_TIMEOUT)
        except Exception as e:
            logger.warning("stop container %s failed: %s", container_id, e)
            stopped_cleanly = False

        # 步骤 2: kill（若 stop 失败 + force=True）
        if not stopped_cleanly and force:
            try:
                await docker_provider.kill_container(container_id)
            except Exception as e:
                logger.warning("kill container %s failed: %s", container_id, e)

        # 步骤 3: remove
        try:
            await docker_provider.remove_container(container_id, force=True)
        except Exception as e:
            logger.error("remove container %s failed: %s", container_id, e)

        # 步骤 4: verify + update status + mark sandbox
        verified = await docker_provider.verify_container_gone(
            container_id=container_id,
            label_key="agentops.run_id",
            label_value=run_id,
        )

        await self._store.update_subagent_status(subagent_id, "completed")

        # 同步标记 provisioned worker 为 released（避免前端「正在运行的 Worker」列表堆积孤儿记录）
        if hasattr(self._store, "update_worker_status"):
            try:
                await self._store.update_worker_status(subagent_id, "released")
            except Exception as e:
                logger.warning("update_worker_status(%s, released) failed: %s", subagent_id, e)

        # v2: sandbox 延迟清理（不在此处删目录，patroller 每日扫）
        if workspace.mode in ("local_copy", "git_clone"):
            cleanup_at = _now_iso_plus(days=SANDBOX_RETENTION_DAYS)
            await self._store.mark_sandbox_for_cleanup(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                cleanup_at=cleanup_at,
            )

        return {
            "stopped": stopped_cleanly,
            "removed": True,
            "verified": verified,
            "sandbox_cleanup_at": _now_iso_plus(days=SANDBOX_RETENTION_DAYS)
                if workspace.mode in ("local_copy", "git_clone") else None,
        }

    # ============================================================
    # 凭据推送（v2 P0.18.6 集成）
    # ============================================================

    async def send_worker_credentials(
        self,
        worker_id: str,
        api_key: str,
    ) -> bool:
        """通过 WS encrypted message 推送 api_key 给 worker。

        P0.18.6 完整实现 ed25519 + AES 加密协议。
        本期（P0.18.4）若 worker_registry=None，跳过返回 False。
        """
        if not self._worker_registry:
            logger.warning("worker_registry not configured, skipping credential push for %s", worker_id)
            return False
        return await self._worker_registry.send_credential(worker_id, api_key)


# ============================================================
# 占位：worker_token（P0.18.6 实现）
# ============================================================

def _placeholder_worker_token(worker_id: str) -> str:
    """v2 P0.18.4 占位：实际 token 由 P0.18.6 worker_token.py 签发。

    本期返回 placeholder，P0.18.6 替换为 JWT 5min TTL。
    """
    return f"placeholder-token-for-{worker_id}"
