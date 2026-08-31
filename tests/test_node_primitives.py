"""
P0.1 测试：3 类新增节点原语 (command / await_command / while)。

覆盖：
  - schema/loader 解析 3 类新节点 config
  - validator 校验规则（互斥 / 多 await_command 报错 / while feedback edge max_traversals 校验）
  - 简单 workflow 包含 3 类新原语，cli.py validate 3 层通过
"""
from __future__ import annotations

import pytest

from workflow.loader import load_workflow_text
from workflow.schema import (
    AwaitCommandNodeConfig,
    CommandNodeConfig,
    NodeType,
    WhileNodeConfig,
)
from workflow.validator import WorkflowValidationError, validate_workflow


# ── Schema/Loader 测试 ─────────────────────────────────────────────


class TestCommandConfig:
    """command 原语 schema/loader 解析测试。"""

    def test_parse_command_config_basic(self):
        yaml_text = """
workflow_id: test-cmd
name: 测试 command
nodes:
  probe_audio:
    type: command
    name: 探测音频
    command_config:
      cli_template: "ffprobe -v error -show_entries format=duration {audio_path}"
      timeout_seconds: 30
      parse_stdout: "int(float(stdout.strip()) * 1000)"
    after: []
    inputs: [audio_path]
    outputs:
      success:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        node = wf.nodes["probe_audio"]
        assert node.type == NodeType.COMMAND
        assert isinstance(node.command_config, CommandNodeConfig)
        assert node.command_config.cli_template.startswith("ffprobe")
        assert node.command_config.timeout_seconds == 30
        assert node.command_config.parse_stdout is not None
        assert "stdout" in node.command_config.parse_stdout

    def test_parse_command_config_missing_raises(self):
        """type=command 必须有 command_config。"""
        yaml_text = """
workflow_id: test-cmd-bad
name: 测试 command 缺 config
nodes:
  bad_node:
    type: command
    name: 无 config
    after: []
    inputs: []
    outputs:
      success:
        to: ""
"""
        with pytest.raises(Exception, match="必须有 command_config"):
            load_workflow_text(yaml_text)

    def test_command_config_on_wrong_type_raises(self):
        """非 command 类型不应有 command_config。"""
        yaml_text = """
workflow_id: test-cmd-bad-type
name: 测试
nodes:
  agent_node:
    type: agent
    name: agent 但带了 command_config
    command_config:
      cli_template: "echo wrong"
    after: []
    inputs: []
    outputs:
      success:
        to: ""
"""
        with pytest.raises(Exception, match="不应有 command_config"):
            load_workflow_text(yaml_text)


class TestAwaitCommandConfig:
    """await_command 原语 schema/loader 解析测试。"""

    def test_parse_await_command_config_basic(self):
        yaml_text = """
workflow_id: test-await
name: 测试 await_command
nodes:
  wait_input:
    type: await_command
    name: 等用户输入
    await_command_config:
      target_actors: [research, synthesis]
      command_port: command
      expiry_seconds: 3600
      max_commands: 5
    after: []
    inputs: []
    outputs:
      timeout:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        node = wf.nodes["wait_input"]
        assert node.type == NodeType.AWAIT_COMMAND
        assert isinstance(node.await_command_config, AwaitCommandNodeConfig)
        assert node.await_command_config.target_actors == ["research", "synthesis"]
        assert node.await_command_config.command_port == "command"
        assert node.await_command_config.expiry_seconds == 3600
        assert node.await_command_config.max_commands == 5

    def test_await_command_defaults(self):
        """默认值：command_port=command, expiry=86400, max=10。"""
        yaml_text = """
workflow_id: test-await-default
name: 测试默认配置
nodes:
  wait_input:
    type: await_command
    name: 等
    await_command_config:
      target_actors: [a]
    after: []
    inputs: []
    outputs:
      timeout:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        cfg = wf.nodes["wait_input"].await_command_config
        assert cfg.command_port == "command"
        assert cfg.expiry_seconds == 86400
        assert cfg.max_commands == 10


class TestWhileConfig:
    """while 原语 schema/loader 解析测试。"""

    def test_parse_while_config_basic(self):
        yaml_text = """
workflow_id: test-while
name: 测试 while
nodes:
  retry_loop:
    type: while
    name: 重试循环
    while_config:
      continue_if: "loop_source.passed == False"
      max_iterations: 3
      backoff_seconds: [0, 2, 5]
      feedback_edge_max_traversals: 3
    after: []
    inputs: [loop_source]
    outputs:
      done:
        to: ""
      exhausted:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        node = wf.nodes["retry_loop"]
        assert node.type == NodeType.WHILE
        assert isinstance(node.while_config, WhileNodeConfig)
        assert "passed" in node.while_config.continue_if
        assert node.while_config.max_iterations == 3
        assert node.while_config.backoff_seconds == [0, 2, 5]
        assert node.while_config.feedback_edge_max_traversals == 3

    def test_while_feedback_max_exceeds_iterations_raises(self):
        """feedback_edge_max_traversals > max_iterations 应拒绝。"""
        yaml_text = """
workflow_id: test-while-bad
name: 测试 while 不一致
nodes:
  bad_loop:
    type: while
    name: 错配
    while_config:
      continue_if: "loop_source.passed == False"
      max_iterations: 2
      feedback_edge_max_traversals: 5
    after: []
    inputs: [loop_source]
    outputs:
      done:
        to: ""
"""
        with pytest.raises(Exception, match="不能大于 max_iterations"):
            load_workflow_text(yaml_text)


# ── Validator 测试 ──────────────────────────────────────────────


class TestCommandValidator:
    """command 节点的 validator 校验规则测试。"""

    def test_command_node_missing_success_port_raises(self):
        """command 节点必须声明 outputs.success port。"""
        yaml_text = """
workflow_id: test-cmd-validate
name: 测试 command 校验
nodes:
  cmd_node:
    type: command
    name: 命令
    command_config:
      cli_template: "echo hi"
    after: []
    inputs: []
    outputs:
      output_only:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        with pytest.raises(WorkflowValidationError) as exc:
            validate_workflow(wf)
        assert any("outputs.success" in e for e in exc.value.errors)

    def test_command_node_with_success_port_validates(self):
        """command 节点带 success port 应通过。"""
        yaml_text = """
workflow_id: test-cmd-ok
name: 测试 command 通过
nodes:
  cmd_node:
    type: command
    name: 命令
    command_config:
      cli_template: "echo hi"
    after: []
    inputs: []
    outputs:
      success:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        validate_workflow(wf)  # 不抛


class TestAwaitCommandValidator:
    """await_command 节点的 MULTIPLE_AWAIT_COMMAND 错误码测试。"""

    def test_multiple_await_command_raises(self):
        """整工作流 ≤ 1 个 await_command，否则协议冲突。"""
        yaml_text = """
workflow_id: test-multi-await
name: 测试多 await_command
nodes:
  wait_a:
    type: await_command
    name: 等 A
    await_command_config:
      target_actors: [a]
    after: []
    inputs: []
    outputs:
      timeout:
        to: ""
  wait_b:
    type: await_command
    name: 等 B
    await_command_config:
      target_actors: [b]
    after: [wait_a]
    inputs: []
    outputs:
      timeout:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        with pytest.raises(WorkflowValidationError) as exc:
            validate_workflow(wf)
        assert any("MULTIPLE" in e or "≤ 1" in e for e in exc.value.errors)

    def test_single_await_command_validates(self):
        """单个 await_command 应通过。"""
        yaml_text = """
workflow_id: test-single-await
name: 单 await_command
nodes:
  only_wait:
    type: await_command
    name: 等
    await_command_config:
      target_actors: [a]
    after: []
    inputs: []
    outputs:
      timeout:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        validate_workflow(wf)  # 不抛


class TestWhileValidator:
    """while 节点的图论校验测试。"""

    def test_while_feedback_max_exceeds_iterations_in_workflow_raises(self):
        """while 节点 feedback_edge_max_traversals > max_iterations 报错。"""
        yaml_text = """
workflow_id: test-while-bad-feedback
name: while feedback 越界
nodes:
  bad_loop:
    type: while
    name: 越界循环
    while_config:
      continue_if: "loop_source == 'retry'"
      max_iterations: 2
      feedback_edge_max_traversals: 5
    after: []
    inputs: [loop_source]
    outputs:
      done:
        to: ""
"""
        # loader 已经拒绝（因为 feedback > iter）
        with pytest.raises(Exception):
            load_workflow_text(yaml_text)


# ── 集成测试：3 类原语在同一 workflow 中 ──────────────────────────────


class TestMixedPrimitives:
    """混合 3 类新原语的 workflow 端到端校验。"""

    def test_all_three_primitives_in_one_workflow(self):
        yaml_text = """
workflow_id: test-mixed-primitives
name: 混合 3 类原语
nodes:
  probe:
    type: command
    name: 探测
    command_config:
      cli_template: "ffprobe -v error {video_path}"
    after: []
    inputs: [video_path]
    outputs:
      success:
        to: "retry_loop.in:loop_source"

  retry_loop:
    type: while
    name: 重试循环
    while_config:
      continue_if: "loop_source.passed == False"
      max_iterations: 3
    after: [probe]
    inputs: [loop_source]
    outputs:
      done:
        to: "wait_user.in:ready"

  wait_user:
    type: await_command
    name: 等用户
    await_command_config:
      target_actors: [user]
    after: [retry_loop]
    inputs: [ready]
    outputs:
      timeout:
        to: ""
"""
        wf = load_workflow_text(yaml_text)
        # 3 层校验通过
        validate_workflow(wf)
        assert wf.nodes["probe"].type == NodeType.COMMAND
        assert wf.nodes["retry_loop"].type == NodeType.WHILE
        assert wf.nodes["wait_user"].type == NodeType.AWAIT_COMMAND