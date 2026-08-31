"""P0.18.5 DagEngine 集成 workspace 授权 + tier 资源限制 + sandbox 延迟清理标记测试。

覆盖：
1. DagEngine.__init__ 接收 workspace_context + container_provisioner 参数
2. _workspace_root 优先使用 workspace_context.workspace_root
3. _try_provision_via_provisioner 前置检查（无 provisioner / 无 workspace_context / 无 event_store 返回 False）
4. _provision_via_provisioner_async 成功路径（mock provisioner.provision）
5. _provision_via_provisioner_async 失败回退（provision 异常时返回 False + 标记 subagent failed）
6. _cleanup_provisioned_subagent 走 provisioner.deprovision 路径（_provisioned_via_provisioner[node.id]=True）
7. _cleanup_provisioned_subagent 走旧路径 + mark_sandbox_for_cleanup（local_copy / git_clone 模式）
8. _cleanup_provisioned_subagent 走旧路径 + isolated 模式不调 mark_sandbox_for_cleanup

设计：
- 不调真实 DagEngine.run()（需真实 workflow + harness），只测试新增的 helper 方法
- 用 mock WorkflowDefinition / WorkflowNode / ContainerProvisioner / EventStore
- 验证 helper 方法的行为契约，不验证端到端流程（端到端在 E2E 测试）
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

import pytest
import pytest_asyncio

from audit import SqliteEventStore
from workflow.engine import DagEngine, NodeExecutionState
from workflow.schema import (
    HarnessTypeRef,
    NodeType,
    OutputRoute,
    RuntimePlacementRef,
    WorkflowDefinition,
    WorkflowNode,
)
from orchestrator.protocol import RunStatus


# ============================================================
# Fixtures
# ============================================================

@pytest_asyncio.fixture
async def store(tmp_path):
    """每个测试用独立临时 db 文件。"""
    db_path = tmp_path / "test_p018_5.db"
    s = SqliteEventStore(str(db_path))
    yield s
    await s.close()


def _make_workflow() -> WorkflowDefinition:
    """构造最小可用 WorkflowDefinition（不真正执行，仅供 DagEngine 初始化）。"""
    node = WorkflowNode(
        id="scan",
        name="Scan",
        type=NodeType.AGENT,
        agent="log_analyst",
        harness=HarnessTypeRef.OPENCODE,
        runtime_placement=RuntimePlacementRef.DOCKER_CONTAINER,
        outputs={"report": OutputRoute(to="next")},
    )
    return WorkflowDefinition(
        workflow_id="wf-test",
        name="Test Workflow",
        nodes={"scan": node},
    )


def _make_workspace_context(mode: str = "local_copy") -> dict:
    return {
        "workspace_id": "ws-test-1",
        "display_name": "test-project",
        "workspace_root": "/sandbox/test-workspace",
        "workspace_mode": mode,
        "permissions": "read_write",
        "source_path": "/tmp/test-project",
    }


# ============================================================
# 1. __init__ 参数注入
# ============================================================

class TestDagEngineInit:
    def test_accepts_workspace_context_and_provisioner(self):
        """DagEngine 接收 workspace_context + container_provisioner 关键字参数。"""
        wf = _make_workflow()
        mock_provisioner = MagicMock()
        ws_ctx = _make_workspace_context()

        engine = DagEngine(
            workflow=wf,
            workspace_context=ws_ctx,
            container_provisioner=mock_provisioner,
        )

        assert engine._workspace_context is ws_ctx
        assert engine._container_provisioner is mock_provisioner
        assert engine._provisioned_via_provisioner == {}

    def test_defaults_to_none(self):
        """不传 workspace_context / container_provisioner 时默认 None（向后兼容）。"""
        wf = _make_workflow()
        engine = DagEngine(workflow=wf)

        assert engine._workspace_context is None
        assert engine._container_provisioner is None


# ============================================================
# 2. _workspace_root 优先使用 workspace_context
# ============================================================

class TestWorkspaceRoot:
    def test_uses_workspace_context_when_set(self):
        """有 workspace_context 时 _workspace_root 返回授权工作区路径。"""
        wf = _make_workflow()
        ws_ctx = _make_workspace_context()
        engine = DagEngine(workflow=wf, workspace_context=ws_ctx)

        # 必须先设置 run_id（_workspace_root 依赖 self.run_state.run_id 在 fallback 路径）
        engine.run_state.run_id = "run_test_1"
        assert engine._workspace_root() == "/sandbox/test-workspace"

    def test_falls_back_to_default_when_no_context(self):
        """无 workspace_context 时回退到 ${AGENTOPS_HOME}/workspaces/{wf_id}/{run_id}/（绝对路径，杜绝产物散落到 cwd）。"""
        wf = _make_workflow()
        engine = DagEngine(workflow=wf)

        engine.run_state.run_id = "run_test_1"
        root = engine._workspace_root()
        assert root == os.path.abspath(
            os.path.join(os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops")),
                         "workspaces", wf.workflow_id, "run_test_1")
        )

    def test_falls_back_when_workspace_root_missing(self):
        """workspace_context 存在但缺 workspace_root 字段时回退默认（绝对路径）。"""
        wf = _make_workflow()
        ws_ctx = {"workspace_id": "ws-x"}  # 缺 workspace_root
        engine = DagEngine(workflow=wf, workspace_context=ws_ctx)

        engine.run_state.run_id = "run_test_2"
        root = engine._workspace_root()
        assert root == os.path.abspath(
            os.path.join(os.environ.get("AGENTOPS_HOME", os.path.expanduser("~/.agentops")),
                         "workspaces", wf.workflow_id, "run_test_2")
        )


# ============================================================
# 3. _try_provision_via_provisioner 前置检查
# ============================================================

class TestTryProvisionViaProvisioner:
    def test_returns_false_when_no_provisioner(self):
        """未注入 container_provisioner → False。"""
        wf = _make_workflow()
        engine = DagEngine(
            workflow=wf,
            event_store=MagicMock(),
            workspace_context=_make_workspace_context(),
        )
        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)

        result = engine._try_provision_via_provisioner(
            node, nstate, "sub-1", 1, "python:3.11",
        )
        assert result is False

    def test_returns_false_when_no_workspace_context(self):
        """无 workspace_context → False。"""
        wf = _make_workflow()
        engine = DagEngine(
            workflow=wf,
            event_store=MagicMock(),
            container_provisioner=MagicMock(),
        )
        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)

        result = engine._try_provision_via_provisioner(
            node, nstate, "sub-1", 1, "python:3.11",
        )
        assert result is False

    def test_returns_false_when_no_event_store(self):
        """无 event_store → False（provisioner 内部需要 store）。"""
        wf = _make_workflow()
        engine = DagEngine(
            workflow=wf,
            container_provisioner=MagicMock(),
            workspace_context=_make_workspace_context(),
        )
        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)

        result = engine._try_provision_via_provisioner(
            node, nstate, "sub-1", 1, "python:3.11",
        )
        assert result is False

    def test_returns_true_when_all_conditions_met(self):
        """所有条件满足 → True（前置检查通过，真正 provision 在 _provision_via_provisioner_async）。"""
        wf = _make_workflow()
        engine = DagEngine(
            workflow=wf,
            event_store=MagicMock(),
            container_provisioner=MagicMock(),
            workspace_context=_make_workspace_context(),
        )
        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)

        result = engine._try_provision_via_provisioner(
            node, nstate, "sub-1", 1, "python:3.11",
        )
        assert result is True


# ============================================================
# 4. _provision_via_provisioner_async 成功路径
# ============================================================

class TestProvisionViaProvisionerAsync:
    @pytest.mark.asyncio
    async def test_provision_success(self, store):
        """provision 成功 → 填充 nstate + 返回 True。"""
        wf = _make_workflow()
        # isolated 模式：不需要 source_path，避免 validate_mount_path 失败
        ws_ctx = _make_workspace_context(mode="isolated")
        engine = DagEngine(
            workflow=wf,
            event_store=store,
            workspace_context=ws_ctx,
        )
        engine.run_state.run_id = "run_prov_ok"

        # 准备：DB 写入 authorized_workspace + session + run + subagent（满足 FK）
        await store.create_authorized_workspace(
            workspace_id="ws-test-1",
            display_name="test-project",
            mode="isolated",  # 与 ws_ctx 一致
            permissions="read_write",
        )
        await store.create_session("sess_prov", agent_id="log_analyst", user_id="u1")
        await store.init_run(
            run_id="run_prov_ok", session_id="sess_prov",
            run_mode="templated", workflow_id="wf-test",
        )
        await store.provision_subagent(
            subagent_id="sub-prov-1",
            actor_id="run_prov_ok:scan",
            run_id="run_prov_ok",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        # mock provisioner.provision 返回 ProvisionResult-like 对象
        mock_provisioner = MagicMock()
        mock_result = MagicMock()
        mock_result.worker_id = "ao-run_prov_ok-scan-L1"
        mock_result.container_id = "container-abc"
        mock_provisioner.provision = AsyncMock(return_value=mock_result)
        engine._container_provisioner = mock_provisioner

        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)

        result = await engine._provision_via_provisioner_async(
            node, nstate, "sub-prov-1", 1, "python:3.11-slim",
        )

        assert result is True
        assert nstate.provisioned_worker_id == "ao-run_prov_ok-scan-L1"
        assert nstate.provisioned_container_id == "container-abc"
        assert mock_provisioner.provision.called

    @pytest.mark.asyncio
    async def test_provision_failure_returns_false_and_marks_subagent(self, store):
        """provision 异常 → 返回 False + 标记 subagent failed。"""
        wf = _make_workflow()
        ws_ctx = _make_workspace_context(mode="isolated")
        engine = DagEngine(
            workflow=wf,
            event_store=store,
            workspace_context=ws_ctx,
        )
        engine.run_state.run_id = "run_prov_fail"

        await store.create_authorized_workspace(
            workspace_id="ws-test-1",
            display_name="test-project",
            mode="isolated",
            permissions="read_write",
        )
        await store.create_session("sess_prov_fail", agent_id="log_analyst", user_id="u1")
        await store.init_run(
            run_id="run_prov_fail", session_id="sess_prov_fail",
            run_mode="templated", workflow_id="wf-test",
        )
        await store.provision_subagent(
            subagent_id="sub-prov-fail",
            actor_id="run_prov_fail:scan",
            run_id="run_prov_fail",
            node_id="scan",
            harness_type="opencode",
            lease_generation=1,
        )

        # mock provisioner.provision 抛异常
        mock_provisioner = MagicMock()
        mock_provisioner.provision = AsyncMock(side_effect=RuntimeError("docker daemon not running"))
        engine._container_provisioner = mock_provisioner

        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)

        result = await engine._provision_via_provisioner_async(
            node, nstate, "sub-prov-fail", 1, "python:3.11-slim",
        )

        assert result is False
        # subagent 应被标记为 failed
        subagents = await store.list_subagents_for_run("run_prov_fail")
        assert any(s["status"] == "failed" for s in subagents)


# ============================================================
# 5. _cleanup_provisioned_subagent 路径分流
# ============================================================

class TestCleanupProvisionedSubagent:
    @pytest.mark.asyncio
    async def test_cleanup_via_provisioner_deprovision(self, store):
        """_provisioned_via_provisioner[node.id]=True → 走 provisioner.deprovision。"""
        wf = _make_workflow()
        ws_ctx = _make_workspace_context(mode="local_copy")
        engine = DagEngine(
            workflow=wf,
            event_store=store,
            workspace_context=ws_ctx,
        )
        engine.run_state.run_id = "run_cleanup_prov"

        # mock provisioner.deprovision
        mock_provisioner = MagicMock()
        mock_provisioner.deprovision = AsyncMock(return_value={
            "stopped": True, "removed": True, "verified": True,
            "sandbox_cleanup_at": "2026-09-10T00:00:00",
        })
        engine._container_provisioner = mock_provisioner

        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)
        nstate.provisioned_subagent_id = "sub-1"
        nstate.provisioned_container_id = "container-xyz"
        # 标记此节点通过 provisioner 启动
        engine._provisioned_via_provisioner["scan"] = True

        await engine._cleanup_provisioned_subagent(nstate, cleanup_status="released")

        # 验证调用了 provisioner.deprovision（不是旧路径 docker_runtime）
        assert mock_provisioner.deprovision.called
        # _provisioned_via_provisioner 应被清理
        assert "scan" not in engine._provisioned_via_provisioner

    @pytest.mark.asyncio
    async def test_cleanup_legacy_path_with_sandbox_marking(self, store):
        """旧路径 + local_copy 模式 → 调 mark_sandbox_for_cleanup。"""
        wf = _make_workflow()
        ws_ctx = _make_workspace_context(mode="local_copy")
        engine = DagEngine(
            workflow=wf,
            event_store=store,
            workspace_context=ws_ctx,
        )
        engine.run_state.run_id = "run_cleanup_legacy"

        # 准备 DB（mark_sandbox_for_cleanup 依赖 run_workspace_meta 已写入）
        await store.create_authorized_workspace(
            workspace_id="ws-test-1",
            display_name="test-project",
            mode="local_copy",
            permissions="read_write",
            source_path="/tmp/test-project",
        )
        await store.create_session("sess_legacy", agent_id="log_analyst", user_id="u1")
        await store.init_run(
            run_id="run_cleanup_legacy", session_id="sess_legacy",
            run_mode="templated", workflow_id="wf-test",
        )
        # 写 run_workspace_meta（mark_sandbox_for_cleanup 依赖此行存在）
        await store.record_run_workspace_meta(
            run_id="run_cleanup_legacy",
            workflow_id="wf-test",
            workspace_root="/sandbox/run_cleanup_legacy",
            absolute_root="/sandbox/run_cleanup_legacy",
            mode=0o660,
            authorized_workspace_id="ws-test-1",
        )

        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)
        nstate.provisioned_subagent_id = "sub-legacy"
        nstate.provisioned_container_id = "container-legacy"
        # 不标记 _provisioned_via_provisioner，走旧路径

        # mock docker_runtime 避免真实调 docker
        with patch("workflow.engine.docker_runtime.stop_container") as mock_stop, \
             patch("workflow.engine.docker_runtime.remove_container") as mock_remove:
            await engine._cleanup_provisioned_subagent(nstate, cleanup_status="released")
            assert mock_stop.called
            assert mock_remove.called

        # 验证 mark_sandbox_for_cleanup 写入了 DB
        # 直接查 run_workspace_meta 表，cleanup_at 应非空且 cleanup_status='scheduled'
        import sqlite3
        conn = sqlite3.connect(str(store.db_path))
        row = conn.execute(
            "SELECT cleanup_at, cleanup_status FROM run_workspace_meta WHERE run_id = ?",
            ("run_cleanup_legacy",),
        ).fetchone()
        conn.close()
        assert row is not None, "run_workspace_meta 行应存在"
        assert row[0] is not None, "cleanup_at 应非空（local_copy 模式触发 mark_sandbox_for_cleanup）"
        assert row[1] == "scheduled", f"cleanup_status 应为 scheduled，实际 {row[1]}"

    @pytest.mark.asyncio
    async def test_cleanup_legacy_path_isolated_no_sandbox_marking(self, store):
        """旧路径 + isolated 模式 → 不调 mark_sandbox_for_cleanup。"""
        wf = _make_workflow()
        ws_ctx = _make_workspace_context(mode="isolated")
        engine = DagEngine(
            workflow=wf,
            event_store=store,
            workspace_context=ws_ctx,
        )
        engine.run_state.run_id = "run_cleanup_iso"

        await store.create_authorized_workspace(
            workspace_id="ws-test-1",
            display_name="test-project",
            mode="isolated",
            permissions="read_write",
        )
        await store.create_session("sess_iso", agent_id="log_analyst", user_id="u1")
        await store.init_run(
            run_id="run_cleanup_iso", session_id="sess_iso",
            run_mode="templated", workflow_id="wf-test",
        )
        await store.record_run_workspace_meta(
            run_id="run_cleanup_iso",
            workflow_id="wf-test",
            workspace_root="/sandbox/iso",
            absolute_root="/sandbox/iso",
            mode=0o660,
            authorized_workspace_id="ws-test-1",
        )

        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)
        nstate.provisioned_subagent_id = "sub-iso"
        nstate.provisioned_container_id = "container-iso"

        with patch("workflow.engine.docker_runtime.stop_container"), \
             patch("workflow.engine.docker_runtime.remove_container"):
            await engine._cleanup_provisioned_subagent(nstate, cleanup_status="released")

        # isolated 模式不应写入 cleanup_at
        from datetime import datetime
        now_iso = datetime.now(timezone.utc).isoformat()
        sandboxes = await store.list_sandboxes_for_cleanup(now_iso)
        assert not any(s["run_id"] == "run_cleanup_iso" for s in sandboxes), \
            "isolated 模式不应触发 mark_sandbox_for_cleanup"

    @pytest.mark.asyncio
    async def test_cleanup_no_provisioned_state_is_noop(self, store):
        """无 provisioned_subagent_id + 无 provisioned_container_id → 直接 return。"""
        wf = _make_workflow()
        engine = DagEngine(workflow=wf, event_store=store)
        node = wf.nodes["scan"]
        nstate = NodeExecutionState(node=node)
        # nstate.provisioned_subagent_id / provisioned_container_id 均为 None

        # 不应抛异常
        await engine._cleanup_provisioned_subagent(nstate)
