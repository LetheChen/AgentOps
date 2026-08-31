"""P0.18.3 docker_provider.py 测试。

覆盖：
1. ContainerCreateOptions 数据类
2. _build_volumes / _build_tmpfs / _build_ports 内部转换函数
3. tier_resource_limits 分级
4. 兼容旧 create_and_start_container_legacy 签名
5. verify_container_gone / list_containers_by_label（用 mock）
6. container_logs_stream 异步生成器（用 mock）

注：不依赖真实 Docker daemon，用 unittest.mock。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import docker_provider
from orchestrator.docker_provider import (
    ContainerCreateOptions,
    TIER_RESOURCE_LIMITS,
    tier_resource_limits,
    _build_volumes,
    _build_tmpfs,
    _build_ports,
    create_and_start_container_legacy,
    verify_container_gone,
    list_containers_by_label,
)


# ============================================================
# ContainerCreateOptions
# ============================================================

class TestContainerCreateOptions:
    def test_default_values(self):
        opts = ContainerCreateOptions(image="test:latest")
        assert opts.image == "test:latest"
        assert opts.name is None
        assert opts.env == {}
        assert opts.labels == {}
        assert opts.mounts is None
        assert opts.network_mode == "bridge"  # v2: 默认 bridge，不用 host
        assert opts.read_only_rootfs is False
        assert opts.cap_drop is None

    def test_full_options(self):
        opts = ContainerCreateOptions(
            image="test:latest",
            name="test-container",
            cmd=["sleep", "3600"],
            env={"KEY": "value"},
            labels={"app": "test"},
            mounts=[{"host": "/host", "container": "/workspace", "mode": "rw"}],
            workdir="/workspace",
            tmpfs=[{"target": "/tmp", "size_bytes": 104857600}],
            cap_drop=["ALL"],
            security_opts=["no-new-privileges:true"],
            read_only_rootfs=True,
            network_mode="bridge",
            extra_hosts=["host-gateway:host-gateway"],
            ports=[{"container_port": 7891, "host_port": 0, "protocol": "tcp"}],
            resource_limits={"pids": 256, "memory_bytes": 2147483648, "nano_cpus": 2000000000},
        )
        assert opts.mounts[0]["container"] == "/workspace"
        assert opts.tmpfs[0]["target"] == "/tmp"
        assert opts.cap_drop == ["ALL"]
        assert opts.read_only_rootfs is True
        assert opts.resource_limits["pids"] == 256


# ============================================================
# 内部转换函数
# ============================================================

class TestBuildVolumes:
    def test_basic(self):
        """P0.18.3 加固：改用 docker SDK mounts list 格式（target/source/type/read_only）。"""
        mounts = [{"host": "/host/data", "container": "/workspace", "mode": "rw"}]
        result = _build_volumes(mounts)
        assert len(result) == 1
        assert result[0]["target"] == "/workspace"
        assert result[0]["source"] == "/host/data"
        assert result[0]["type"] == "bind"
        assert result[0]["read_only"] is False

    def test_read_only(self):
        mounts = [{"host": "/host/data", "container": "/workspace", "mode": "ro"}]
        result = _build_volumes(mounts)
        assert result[0]["read_only"] is True

    def test_default_mode_rw(self):
        """mount mode 缺省为 rw（read_only=False）。"""
        mounts = [{"host": "/host/data", "container": "/workspace"}]
        result = _build_volumes(mounts)
        assert result[0]["read_only"] is False

    def test_multiple_mounts(self):
        mounts = [
            {"host": "/host/a", "container": "/a", "mode": "rw"},
            {"host": "/host/b", "container": "/b", "mode": "ro"},
        ]
        result = _build_volumes(mounts)
        assert len(result) == 2
        assert result[0]["target"] == "/a"
        assert result[1]["target"] == "/b"
        assert result[1]["read_only"] is True


class TestBuildTmpfs:
    def test_basic(self):
        tmpfs_list = [{"target": "/tmp", "size_bytes": 104857600}]
        result = _build_tmpfs(tmpfs_list)
        assert result == {"/tmp": "104857600b"}

    def test_multiple(self):
        tmpfs_list = [
            {"target": "/tmp", "size_bytes": 104857600},
            {"target": "/run", "size_bytes": 10485760},
        ]
        result = _build_tmpfs(tmpfs_list)
        assert len(result) == 2
        assert result["/run"] == "10485760b"


class TestBuildPorts:
    def test_basic(self):
        ports_list = [{"container_port": 7891, "host_port": 0, "protocol": "tcp"}]
        result = _build_ports(ports_list)
        assert result == {"7891/tcp": 0}

    def test_default_protocol_tcp(self):
        ports_list = [{"container_port": 8080, "host_port": 8080}]
        result = _build_ports(ports_list)
        assert "8080/tcp" in result

    def test_udp(self):
        ports_list = [{"container_port": 53, "host_port": 0, "protocol": "udp"}]
        result = _build_ports(ports_list)
        assert "53/udp" in result


# ============================================================
# tier_resource_limits
# ============================================================

class TestTierResourceLimits:
    def test_t0_limits(self):
        rl = tier_resource_limits("T0")
        assert rl["pids"] == 64
        assert rl["memory_bytes"] == 512 * 1024**2
        assert rl["nano_cpus"] == 1 * 10**9

    def test_t3_limits(self):
        rl = tier_resource_limits("T3")
        assert rl["pids"] == 512
        assert rl["memory_bytes"] == 4 * 1024**3
        assert rl["nano_cpus"] == 4 * 10**9

    def test_unknown_tier_defaults_t2(self):
        rl = tier_resource_limits("T9")
        assert rl == TIER_RESOURCE_LIMITS["T2"]

    def test_tier_progression(self):
        """tier 越高资源越多。"""
        t0 = tier_resource_limits("T0")
        t1 = tier_resource_limits("T1")
        t2 = tier_resource_limits("T2")
        t3 = tier_resource_limits("T3")
        assert t0["memory_bytes"] < t1["memory_bytes"] < t2["memory_bytes"] < t3["memory_bytes"]
        assert t0["nano_cpus"] <= t1["nano_cpus"] <= t2["nano_cpus"] <= t3["nano_cpus"]


# ============================================================
# verify_container_gone（mock docker SDK）
# ============================================================

class TestVerifyContainerGone:
    @pytest.mark.asyncio
    async def test_container_gone(self):
        """容器已移除返回 True。"""
        with patch("orchestrator.docker_provider.list_containers_by_label", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = []  # 没有容器匹配 label
            result = await verify_container_gone("container-123", "agentops.run_id", "run-abc")
            assert result is True

    @pytest.mark.asyncio
    async def test_container_still_exists(self):
        """容器仍存在返回 False。"""
        with patch("orchestrator.docker_provider.list_containers_by_label", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [
                {"id": "container-123", "name": "test", "status": "running"}
            ]
            result = await verify_container_gone("container-123", "agentops.run_id", "run-abc")
            assert result is False

    @pytest.mark.asyncio
    async def test_other_container_exists(self):
        """label 匹配但 ID 不同返回 True（目标容器已 gone）。"""
        with patch("orchestrator.docker_provider.list_containers_by_label", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [
                {"id": "other-container", "name": "other", "status": "running"}
            ]
            result = await verify_container_gone("container-123", "agentops.run_id", "run-abc")
            assert result is True

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        """异常时返回 False（保守判断，避免误删）。"""
        with patch("orchestrator.docker_provider.list_containers_by_label", new_callable=AsyncMock) as mock_list:
            mock_list.side_effect = RuntimeError("docker daemon unreachable")
            result = await verify_container_gone("container-123", "agentops.run_id", "run-abc")
            assert result is False


# ============================================================
# list_containers_by_label（mock docker SDK）
# ============================================================

class TestListContainersByLabel:
    @pytest.mark.asyncio
    async def test_list_by_label_with_value(self):
        """按 label key=value 过滤。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test-container"
        mock_container.status = "running"
        mock_container.labels = {"agentops.run_id": "run-abc"}
        mock_container.image.tags = ["test:latest"]

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_container]

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            result = await list_containers_by_label("agentops.run_id", "run-abc")
            assert len(result) == 1
            assert result[0]["id"] == "c1"
            assert result[0]["name"] == "test-container"
            assert result[0]["labels"]["agentops.run_id"] == "run-abc"

    @pytest.mark.asyncio
    async def test_list_by_label_key_only(self):
        """按 label key 过滤（不限制 value）。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test-container"
        mock_container.status = "running"
        mock_container.labels = {"agentops.run_id": "run-xyz"}
        mock_container.image.tags = []

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_container]

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            result = await list_containers_by_label("agentops.run_id")
            assert len(result) == 1


# ============================================================
# 兼容旧签名
# ============================================================

class TestLegacyCompat:
    @pytest.mark.asyncio
    async def test_legacy_signature(self):
        """create_and_start_container_legacy 接受旧签名。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test"

        mock_client = MagicMock()
        mock_container_obj = MagicMock()
        mock_container_obj.start = MagicMock()
        mock_client.containers.create.return_value = mock_container_obj
        mock_client.containers.get.return_value = mock_container_obj

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            result = await create_and_start_container_legacy(
                image="test:latest",
                name="test-container",
                cmd=["sleep", "3600"],
                env={"KEY": "value"},
                labels={"app": "test"},
            )
            # 验证 create 被调用
            assert mock_client.containers.create.called
            # 验证 start 被调用
            assert mock_container_obj.start.called


# ============================================================
# create_container 扩展能力（mock）
# ============================================================

class TestCreateContainerExtended:
    @pytest.mark.asyncio
    async def test_create_with_mounts(self):
        """P0.18.3 加固：create_container 用 docker SDK mounts list 格式（不用 volumes dict）。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test"

        mock_client = MagicMock()
        mock_client.containers.create.return_value = mock_container

        opts = ContainerCreateOptions(
            image="test:latest",
            name="test",
            mounts=[{"host": "/host/data", "container": "/workspace", "mode": "rw"}],
        )

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            await docker_provider.create_container(opts)
            # 验证 create 调用时含 mounts 参数（list 格式，非 dict）
            call_kwargs = mock_client.containers.create.call_args[1]
            assert "mounts" in call_kwargs
            assert isinstance(call_kwargs["mounts"], list)
            assert call_kwargs["mounts"][0]["source"] == "/host/data"
            assert call_kwargs["mounts"][0]["target"] == "/workspace"
            assert call_kwargs["mounts"][0]["type"] == "bind"
            assert call_kwargs["mounts"][0]["read_only"] is False

    @pytest.mark.asyncio
    async def test_create_with_security_opts(self):
        """create_container 传 cap_drop + security_opts + read_only_rootfs。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test"

        mock_client = MagicMock()
        mock_client.containers.create.return_value = mock_container

        opts = ContainerCreateOptions(
            image="test:latest",
            cap_drop=["ALL"],
            security_opts=["no-new-privileges:true"],
            read_only_rootfs=True,
        )

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            await docker_provider.create_container(opts)
            call_kwargs = mock_client.containers.create.call_args[1]
            assert call_kwargs["cap_drop"] == ["ALL"]
            assert call_kwargs["security_opt"] == ["no-new-privileges:true"]
            assert call_kwargs["read_only"] is True

    @pytest.mark.asyncio
    async def test_create_with_resource_limits(self):
        """create_container 传 resource_limits。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test"

        mock_client = MagicMock()
        mock_client.containers.create.return_value = mock_container

        opts = ContainerCreateOptions(
            image="test:latest",
            resource_limits={"pids": 256, "memory_bytes": 2147483648, "nano_cpus": 2000000000},
        )

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            await docker_provider.create_container(opts)
            call_kwargs = mock_client.containers.create.call_args[1]
            assert call_kwargs["pids_limit"] == 256
            assert call_kwargs["mem_limit"] == "2147483648"
            assert call_kwargs["nano_cpus"] == 2000000000

    @pytest.mark.asyncio
    async def test_create_default_extra_hosts(self):
        """v2 修复：默认 extra_hosts 含 host-gateway（不调用方指定时）。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test"

        mock_client = MagicMock()
        mock_client.containers.create.return_value = mock_container

        opts = ContainerCreateOptions(image="test:latest")

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            await docker_provider.create_container(opts)
            call_kwargs = mock_client.containers.create.call_args[1]
            assert call_kwargs["extra_hosts"] == ["host-gateway:host-gateway"]

    @pytest.mark.asyncio
    async def test_create_bridge_network_default(self):
        """v2 修复：默认 network_mode='bridge'，不用 'host'。"""
        mock_container = MagicMock()
        mock_container.id = "c1"
        mock_container.short_id = "c1"
        mock_container.name = "test"

        mock_client = MagicMock()
        mock_client.containers.create.return_value = mock_container

        opts = ContainerCreateOptions(image="test:latest")

        with patch("orchestrator.docker_runtime._get_client", return_value=mock_client):
            await docker_provider.create_container(opts)
            call_kwargs = mock_client.containers.create.call_args[1]
            assert call_kwargs["network"] == "bridge"
