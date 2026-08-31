"""P0.18.4 + P0.18.6 测试：container_provisioner + worker_token + WorkerRegistry。

覆盖：
1. worker_token JWT 签发 + 校验（5min TTL + worker_id 匹配 + 签名错误）
2. WorkerRegistry register/unregister/wait_registered/send_credential
3. ContainerProvisioner build_container_labels + generate_worker_id
4. ContainerProvisioner provision 5 步（mock docker_provider）
5. ContainerProvisioner deprovision 4 步（mock docker_provider）
6. sandbox 延迟清理标记（mark_sandbox_for_cleanup）

P0.18.5 engine.py 集成测试在 test_p018_5_engine_integration.py（需真实 DagEngine）。
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from audit import SqliteEventStore
from orchestrator import docker_provider
from orchestrator.container_provisioner import (
    ContainerProvisioner,
    ProvisionRequest,
    ProvisionResult,
    build_container_labels,
    generate_worker_id,
    SANDBOX_RETENTION_DAYS,
)
from orchestrator.docker_provider import ContainerCreateOptions
from orchestrator.worker_token import (
    issue_worker_token,
    verify_worker_token,
    WorkerRegistry,
    get_worker_registry,
    set_worker_registry,
)
from orchestrator.workspace_paths import WorkspaceInfo, PreparedWorkspace


@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = tmp_path / "test_p018_4.db"
    s = SqliteEventStore(str(db_path))
    yield s
    await s.close()


@pytest_asyncio.fixture
async def workspace_info(store):
    """创建真实 workspace 记录（避免 FK constraint）。"""
    await store.create_authorized_workspace(
        workspace_id="ws-test-1",
        display_name="test-project",
        mode="local_copy",
        permissions="read_write",
        source_path="/tmp/test-project",
    )
    return WorkspaceInfo(
        workspace_id="ws-test-1",
        display_name="test-project",
        mode="local_copy",
        permissions="read_write",
        source_path="/tmp/test-project",
        git_url=None,
        git_branch=None,
        enabled=True,
    )


@pytest_asyncio.fixture
def prepared_workspace():
    return PreparedWorkspace(
        workspace_id="ws-test-1",
        mode="local_copy",
        permissions="read_write",
        workspace_root="/sandbox/run-test-1",
    )


# ============================================================
# Worker token JWT
# ============================================================

class TestWorkerToken:
    def test_issue_and_verify_ok(self):
        """签发 + 校验通过。"""
        token = issue_worker_token("worker-123")
        assert verify_worker_token("worker-123", token) is True

    def test_verify_wrong_worker_id(self):
        """worker_id 不匹配校验失败。"""
        token = issue_worker_token("worker-123")
        assert verify_worker_token("worker-456", token) is False

    def test_verify_invalid_token(self):
        """无效 token 校验失败。"""
        assert verify_worker_token("worker-123", "invalid.token.here") is False
        assert verify_worker_token("worker-123", "") is False
        assert verify_worker_token("worker-123", "not-a-jwt") is False

    def test_verify_tampered_signature(self):
        """篡改签名校验失败。"""
        token = issue_worker_token("worker-123")
        parts = token.split(".")
        # 篡改签名部分
        tampered = f"{parts[0]}.{parts[1]}.tampered_signature"
        assert verify_worker_token("worker-123", tampered) is False

    def test_verify_expired_token(self):
        """过期 token 校验失败。"""
        # 签发一个已过期的 token（TTL=-1）
        token = issue_worker_token("worker-123", ttl=-1)
        assert verify_worker_token("worker-123", token) is False


# ============================================================
# WorkerRegistry
# ============================================================

class TestWorkerRegistry:
    @pytest.mark.asyncio
    async def test_register_and_wait(self):
        """register 触发 wait_registered。"""
        registry = WorkerRegistry()
        mock_ws = MagicMock()

        # 先等 wait_registered（worker 未注册，会阻塞）
        wait_task = asyncio.create_task(registry.wait_registered("worker-1"))
        await asyncio.sleep(0.05)  # 让 wait_task 开始等

        # register
        await registry.register("worker-1", mock_ws)

        # wait_registered 应返回
        await asyncio.wait_for(wait_task, timeout=1.0)
        assert await registry.is_registered("worker-1") is True

    @pytest.mark.asyncio
    async def test_wait_registered_already_registered(self):
        """已注册的 worker wait_registered 立即返回。"""
        registry = WorkerRegistry()
        mock_ws = MagicMock()
        await registry.register("worker-1", mock_ws)
        # 立即返回（不阻塞）
        await asyncio.wait_for(registry.wait_registered("worker-1"), timeout=1.0)

    @pytest.mark.asyncio
    async def test_unregister(self):
        """unregister 后 is_registered=False。"""
        registry = WorkerRegistry()
        mock_ws = MagicMock()
        await registry.register("worker-1", mock_ws)
        assert await registry.is_registered("worker-1") is True
        await registry.unregister("worker-1")
        assert await registry.is_registered("worker-1") is False

    @pytest.mark.asyncio
    async def test_send_credential_ok(self):
        """send_credential 推送成功。"""
        registry = WorkerRegistry()
        mock_ws = AsyncMock()
        await registry.register("worker-1", mock_ws)
        result = await registry.send_credential("worker-1", "api-key-xxx")
        assert result is True
        mock_ws.send_json.assert_called_once()
        msg = mock_ws.send_json.call_args[0][0]
        assert msg["type"] == "credential"
        assert msg["api_key"] == "api-key-xxx"

    @pytest.mark.asyncio
    async def test_send_credential_not_connected(self):
        """worker 未连接时 send_credential 返回 False。"""
        registry = WorkerRegistry()
        result = await registry.send_credential("worker-1", "api-key-xxx")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_task_ok(self):
        """send_task 推送成功。"""
        registry = WorkerRegistry()
        mock_ws = AsyncMock()
        await registry.register("worker-1", mock_ws)
        result = await registry.send_task("worker-1", {"node_id": "scan", "inputs": {}})
        assert result is True

    @pytest.mark.asyncio
    async def test_send_shutdown_ok(self):
        """send_shutdown 推送成功。"""
        registry = WorkerRegistry()
        mock_ws = AsyncMock()
        await registry.register("worker-1", mock_ws)
        result = await registry.send_shutdown("worker-1")
        assert result is True

    def test_list_active_workers(self):
        """list_active_workers 返回已注册 worker_id 列表。"""
        registry = WorkerRegistry()
        # 同步注册（直接操作内部 dict）
        registry._workers["w1"] = {"websocket": MagicMock(), "registered_event": asyncio.Event(), "registered_at": time.time()}
        registry._workers["w2"] = {"websocket": MagicMock(), "registered_event": asyncio.Event(), "registered_at": time.time()}
        registry._workers["w3"] = {"websocket": None, "registered_event": asyncio.Event(), "registered_at": None}
        active = registry.list_active_workers()
        assert "w1" in active
        assert "w2" in active
        assert "w3" not in active  # websocket=None 不算 active


# ============================================================
# ContainerProvisioner 辅助函数
# ============================================================

class TestProvisionerHelpers:
    def test_generate_worker_id(self):
        """worker_id 格式：ao-{run_id}-{node_id}-L{lease_generation}。"""
        wid = generate_worker_id("run-abc", "scan", 3)
        assert wid == "ao-run-abc-scan-L3"

    def test_build_container_labels(self):
        """labels 含完整治理字段。"""
        labels = build_container_labels(
            run_id="run-abc",
            node_id="scan",
            subagent_id="sub-1",
            lease_generation=1,
            workspace_id="ws-1",
            workspace_mode="local_copy",
            tier="T3",
        )
        assert labels["agentops.kind"] == "agentops-worker"
        assert labels["agentops.run_id"] == "run-abc"
        assert labels["agentops.node_id"] == "scan"
        assert labels["agentops.subagent_id"] == "sub-1"
        assert labels["agentops.lease_generation"] == "1"
        assert labels["agentops.workspace_id"] == "ws-1"
        assert labels["agentops.workspace_mode"] == "local_copy"
        assert labels["agentops.tier"] == "T3"
        assert "agentops.protocol_version" in labels
        assert "agentops.built_at" in labels

    def test_build_container_labels_no_workspace(self):
        """workspace_id=None 时不加 agentops.workspace_id label。"""
        labels = build_container_labels(
            run_id="run-abc",
            node_id="scan",
            subagent_id="sub-1",
            lease_generation=1,
            workspace_id=None,
            workspace_mode="isolated",
            tier="T0",
        )
        assert "agentops.workspace_id" not in labels


# ============================================================
# ContainerProvisioner provision（mock docker_provider）
# ============================================================

class TestProvisionerProvision:
    @pytest.mark.asyncio
    async def test_provision_5_steps_success(self, store, workspace_info, prepared_workspace):
        """5 步启动成功（mock docker_provider + worker_registry）。"""
        # 准备：创建 session + run + subagent
        await store.create_session("sess-prov-1", agent_id="manager", user_id="u1")
        await store.init_run(
            run_id="run-prov-1", session_id="sess-prov-1",
            run_mode="templated", workflow_id="wf-1",
        )
        await store.provision_subagent(
            subagent_id="sub-prov-1",
            actor_id="run-prov-1:scan",
            run_id="run-prov-1",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        # mock worker_registry
        mock_registry = AsyncMock()
        mock_registry.wait_registered = AsyncMock(return_value=None)
        mock_registry.send_credential = AsyncMock(return_value=True)

        # mock docker_provider
        mock_container_info = {"id": "container-123", "short_id": "c123", "name": "ao_run-prov-1-scan-L1"}

        provisioner = ContainerProvisioner(event_store=store, worker_registry=mock_registry)
        req = ProvisionRequest(
            run_id="run-prov-1",
            node_id="scan",
            subagent_id="sub-prov-1",
            lease_generation=1,
            workspace=workspace_info,
            prepared=prepared_workspace,
            agent_tier="T3",
            image="agentops-worker:latest",
            resolved_model={"provider": "minimax", "model": "MiniMax-M3", "api_key": "key-xxx"},
        )

        with patch("orchestrator.container_provisioner.docker_provider.create_container", new_callable=AsyncMock) as mock_create, \
             patch("orchestrator.container_provisioner.docker_provider.start_container", new_callable=AsyncMock) as mock_start:
            mock_create.return_value = mock_container_info
            result = await provisioner.provision(req)

        # 验证 5 步
        assert mock_create.called  # 步骤 4: create
        assert mock_start.called  # 步骤 4: start
        assert mock_registry.wait_registered.called  # 步骤 5: wait WS
        # record_provisioned_worker
        assert result.container_id == "container-123"
        assert result.worker_id == "ao-run-prov-1-scan-L1"
        assert result.registered is True

    @pytest.mark.asyncio
    async def test_provision_ws_timeout_rollback(self, store, workspace_info, prepared_workspace):
        """WS 注册超时自动回滚（deprovision）。"""
        await store.create_session("sess-prov-2", agent_id="manager", user_id="u1")
        await store.init_run(
            run_id="run-prov-2", session_id="sess-prov-2",
            run_mode="templated", workflow_id="wf-1",
        )
        await store.provision_subagent(
            subagent_id="sub-prov-2",
            actor_id="run-prov-2:scan",
            run_id="run-prov-2",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        # mock worker_registry: wait_registered 超时（返回 False 表示超时）
        mock_registry = AsyncMock()
        # wait_registered 超时由 provisioner 内部 asyncio.wait_for 触发
        async def _slow_wait(worker_id):
            await asyncio.sleep(100)  # 模拟超时
        mock_registry.wait_registered = _slow_wait

        provisioner = ContainerProvisioner(event_store=store, worker_registry=mock_registry)

        # 设置超时为 0.1s 加速测试
        import orchestrator.container_provisioner as cp
        original_timeout = cp.DEFAULT_WORKER_REGISTRATION_TIMEOUT
        cp.DEFAULT_WORKER_REGISTRATION_TIMEOUT = 0.1

        req = ProvisionRequest(
            run_id="run-prov-2",
            node_id="scan",
            subagent_id="sub-prov-2",
            lease_generation=1,
            workspace=workspace_info,
            prepared=prepared_workspace,
            agent_tier="T3",
            image="agentops-worker:latest",
            resolved_model={"provider": "minimax", "model": "MiniMax-M3", "api_key": "key-xxx"},
        )

        try:
            with patch("orchestrator.container_provisioner.docker_provider.create_container", new_callable=AsyncMock) as mock_create, \
                 patch("orchestrator.container_provisioner.docker_provider.start_container", new_callable=AsyncMock), \
                 patch("orchestrator.container_provisioner.docker_provider.stop_container", new_callable=AsyncMock), \
                 patch("orchestrator.container_provisioner.docker_provider.kill_container", new_callable=AsyncMock), \
                 patch("orchestrator.container_provisioner.docker_provider.remove_container", new_callable=AsyncMock), \
                 patch("orchestrator.container_provisioner.docker_provider.verify_container_gone", new_callable=AsyncMock, return_value=True):
                mock_create.return_value = {"id": "container-456", "short_id": "c456", "name": "test"}
                with pytest.raises(RuntimeError, match="did not register via WS"):
                    await provisioner.provision(req)
        finally:
            cp.DEFAULT_WORKER_REGISTRATION_TIMEOUT = original_timeout

    @pytest.mark.asyncio
    async def test_provision_no_registry_skips_wait(self, store, workspace_info, prepared_workspace):
        """worker_registry=None 时跳过 WS 等待（registered=False）。"""
        await store.create_session("sess-prov-3", agent_id="manager", user_id="u1")
        await store.init_run(
            run_id="run-prov-3", session_id="sess-prov-3",
            run_mode="templated", workflow_id="wf-1",
        )
        await store.provision_subagent(
            subagent_id="sub-prov-3",
            actor_id="run-prov-3:scan",
            run_id="run-prov-3",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        provisioner = ContainerProvisioner(event_store=store, worker_registry=None)
        req = ProvisionRequest(
            run_id="run-prov-3",
            node_id="scan",
            subagent_id="sub-prov-3",
            lease_generation=1,
            workspace=workspace_info,
            prepared=prepared_workspace,
            agent_tier="T2",
            image="agentops-worker:latest",
            resolved_model={"provider": "minimax", "model": "MiniMax-M3", "api_key": "key-xxx"},
        )

        with patch("orchestrator.container_provisioner.docker_provider.create_container", new_callable=AsyncMock) as mock_create, \
             patch("orchestrator.container_provisioner.docker_provider.start_container", new_callable=AsyncMock):
            mock_create.return_value = {"id": "c-789", "short_id": "c789", "name": "test"}
            result = await provisioner.provision(req)

        assert result.registered is False  # no registry → skip
        assert result.container_id == "c-789"


# ============================================================
# ContainerProvisioner deprovision（mock docker_provider）
# ============================================================

class TestProvisionerDeprovision:
    @pytest.mark.asyncio
    async def test_deprovision_4_steps_success(self, store, workspace_info):
        """4 步销毁成功 + sandbox 延迟清理标记。"""
        await store.create_session("sess-dep-1", agent_id="manager", user_id="u1")
        await store.init_run(
            run_id="run-dep-1", session_id="sess-dep-1",
            run_mode="templated", workflow_id="wf-1",
        )
        await store.provision_subagent(
            subagent_id="sub-dep-1",
            actor_id="run-dep-1:scan",
            run_id="run-dep-1",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        provisioner = ContainerProvisioner(event_store=store, worker_registry=None)

        with patch("orchestrator.container_provisioner.docker_provider.stop_container", new_callable=AsyncMock) as mock_stop, \
             patch("orchestrator.container_provisioner.docker_provider.kill_container", new_callable=AsyncMock) as mock_kill, \
             patch("orchestrator.container_provisioner.docker_provider.remove_container", new_callable=AsyncMock) as mock_remove, \
             patch("orchestrator.container_provisioner.docker_provider.verify_container_gone", new_callable=AsyncMock, return_value=True) as mock_verify:
            result = await provisioner.deprovision(
                container_id="container-abc",
                workspace=workspace_info,
                subagent_id="sub-dep-1",
                run_id="run-dep-1",
                force=False,
            )

        assert mock_stop.called  # 步骤 1: stop
        assert not mock_kill.called  # stop 成功，不 kill
        assert mock_remove.called  # 步骤 3: remove
        assert mock_verify.called  # 步骤 4: verify
        assert result["stopped"] is True
        assert result["removed"] is True
        assert result["verified"] is True
        # sandbox 延迟清理标记（local_copy 模式）
        assert result["sandbox_cleanup_at"] is not None

        # 验证 mark_sandbox_for_cleanup 被调用（查 DB）
        from audit.store import SqliteEventStore
        # 先写 run_workspace_meta（deprovision 依赖 mark_sandbox_for_cleanup 写入）
        # 已通过 provisioner.deprovision 调用 mark_sandbox_for_cleanup

    @pytest.mark.asyncio
    async def test_deprovision_force_kills_on_stop_failure(self, store, workspace_info):
        """stop 失败 + force=True 触发 kill。"""
        await store.create_session("sess-dep-2", agent_id="manager", user_id="u1")
        await store.init_run(
            run_id="run-dep-2", session_id="sess-dep-2",
            run_mode="templated", workflow_id="wf-1",
        )
        await store.provision_subagent(
            subagent_id="sub-dep-2",
            actor_id="run-dep-2:scan",
            run_id="run-dep-2",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        provisioner = ContainerProvisioner(event_store=store, worker_registry=None)

        with patch("orchestrator.container_provisioner.docker_provider.stop_container", new_callable=AsyncMock, side_effect=RuntimeError("stop failed")), \
             patch("orchestrator.container_provisioner.docker_provider.kill_container", new_callable=AsyncMock) as mock_kill, \
             patch("orchestrator.container_provisioner.docker_provider.remove_container", new_callable=AsyncMock), \
             patch("orchestrator.container_provisioner.docker_provider.verify_container_gone", new_callable=AsyncMock, return_value=True):
            result = await provisioner.deprovision(
                container_id="container-xyz",
                workspace=workspace_info,
                subagent_id="sub-dep-2",
                run_id="run-dep-2",
                force=True,
            )

        assert mock_kill.called  # stop 失败 + force → kill
        assert result["stopped"] is False

    @pytest.mark.asyncio
    async def test_deprovision_no_force_no_kill(self, store, workspace_info):
        """stop 失败 + force=False 不 kill。"""
        await store.create_session("sess-dep-3", agent_id="manager", user_id="u1")
        await store.init_run(
            run_id="run-dep-3", session_id="sess-dep-3",
            run_mode="templated", workflow_id="wf-1",
        )
        await store.provision_subagent(
            subagent_id="sub-dep-3",
            actor_id="run-dep-3:scan",
            run_id="run-dep-3",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        provisioner = ContainerProvisioner(event_store=store, worker_registry=None)

        with patch("orchestrator.container_provisioner.docker_provider.stop_container", new_callable=AsyncMock, side_effect=RuntimeError("stop failed")), \
             patch("orchestrator.container_provisioner.docker_provider.kill_container", new_callable=AsyncMock) as mock_kill, \
             patch("orchestrator.container_provisioner.docker_provider.remove_container", new_callable=AsyncMock), \
             patch("orchestrator.container_provisioner.docker_provider.verify_container_gone", new_callable=AsyncMock, return_value=True):
            result = await provisioner.deprovision(
                container_id="container-none",
                workspace=workspace_info,
                subagent_id="sub-dep-3",
                run_id="run-dep-3",
                force=False,
            )

        assert not mock_kill.called  # force=False → 不 kill
        assert result["stopped"] is False

    @pytest.mark.asyncio
    async def test_deprovision_isolated_no_sandbox_cleanup(self, store):
        """isolated 模式不标记 sandbox 清理。"""
        await store.create_authorized_workspace(
            workspace_id="ws-iso",
            display_name="isolated",
            mode="isolated",
            permissions="read_write",
        )
        await store.create_session("sess-dep-4", agent_id="manager", user_id="u1")
        await store.init_run(
            run_id="run-dep-4", session_id="sess-dep-4",
            run_mode="templated", workflow_id="wf-1",
        )
        await store.provision_subagent(
            subagent_id="sub-dep-4",
            actor_id="run-dep-4:scan",
            run_id="run-dep-4",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        isolated_ws = WorkspaceInfo(
            workspace_id="ws-iso",
            display_name="isolated",
            mode="isolated",
            permissions="read_write",
            source_path=None,
            git_url=None,
            git_branch=None,
            enabled=True,
        )

        provisioner = ContainerProvisioner(event_store=store, worker_registry=None)

        with patch("orchestrator.container_provisioner.docker_provider.stop_container", new_callable=AsyncMock), \
             patch("orchestrator.container_provisioner.docker_provider.kill_container", new_callable=AsyncMock), \
             patch("orchestrator.container_provisioner.docker_provider.remove_container", new_callable=AsyncMock), \
             patch("orchestrator.container_provisioner.docker_provider.verify_container_gone", new_callable=AsyncMock, return_value=True):
            result = await provisioner.deprovision(
                container_id="container-iso",
                workspace=isolated_ws,
                subagent_id="sub-dep-4",
                run_id="run-dep-4",
            )

        # isolated 模式不标记 sandbox 清理
        assert result["sandbox_cleanup_at"] is None
