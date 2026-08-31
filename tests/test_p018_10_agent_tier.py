"""P0.18.10 agent yaml tier 字段 + 兼容矩阵校验 + 动态 tier 拦截测试。

覆盖验收：
1. 10 个 agent yaml 都有 tier 字段且取值合法
2. AgentDefinition.tier 默认 T2（未声明时）
3. AgentDefinition.tier 非法值降级 T2
4. ConfigLoader.validate() tier × allowed_tools 一致性校验
5. workspace_paths.check_tool_tier_permission 三层校验
6. workspace_paths.TOOL_TIER_MAP / required_tier_for_tool
7. workspace_paths.tier_compatible / effective_tier
8. conversational._try_execute_tool_call 动态 tier 拦截
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.config_loader import AgentDefinition, ConfigLoader, get_system_config
from orchestrator.workspace_paths import (
    TOOL_TIER_MAP,
    REQUIRES_WORKSPACE_TOOLS,
    TierPermissionError,
    check_tool_tier_permission,
    effective_tier,
    required_tier_for_tool,
    tier_compatible,
)


# ============================================================
# 1. agent yaml tier 字段校验
# ============================================================

AGENTS_DIR = PROJECT_ROOT / "config" / "agents"

# 预期 tier 映射（与方案 §3.1 一致）
EXPECTED_TIERS = {
    "manager": "T0",          # 通用对话入口，动态升 tier
    "video_creator": "T3",    # write_file + bash（ffprobe）
    "log_analyst": "T2",      # read 日志 + write 报告，无 shell
    "task_monitor": "T2",     # system_probe + write 报告
    "smart_query": "T1",      # 只读 SQL
    "smart_ops": "T3",        # ssh_exec / server_restart
    "proposal_planner": "T2", # read cases + write Reports
    "smart_form": "T0",       # 仅 OA HTTP API
    "smart_approval": "T0",   # 仅审批 HTTP API
    "smart_analysis": "T1",   # 只读 SQL + 分析报告
}


@pytest.mark.parametrize("agent_id,expected_tier", list(EXPECTED_TIERS.items()))
def test_agent_yaml_has_tier_field(agent_id: str, expected_tier: str):
    """每个 agent yaml 都有 tier 字段，取值与预期一致。"""
    yaml_path = AGENTS_DIR / f"{agent_id}.yaml"
    assert yaml_path.exists(), f"agent yaml 不存在: {yaml_path}"

    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    assert "tier" in raw, f"agent {agent_id} 缺少 tier 字段"
    assert raw["tier"] == expected_tier, (
        f"agent {agent_id} tier 应为 {expected_tier}，实际 {raw['tier']}"
    )


def test_all_agent_tiers_are_valid():
    """所有 agent tier 取值在 {T0, T1, T2, T3} 范围内。"""
    valid_tiers = {"T0", "T1", "T2", "T3"}
    for yaml_path in sorted(AGENTS_DIR.glob("*.yaml")):
        with open(yaml_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        tier = raw.get("tier", "T2")
        assert tier in valid_tiers, (
            f"agent {yaml_path.stem} tier={tier} 不合法，应在 {valid_tiers} 内"
        )


# ============================================================
# 2. AgentDefinition.tier 默认值 + 非法值降级
# ============================================================

def test_agent_definition_tier_default():
    """AgentDefinition.tier 默认 T2。"""
    agent = AgentDefinition(agent_id="test", domain="test", display_name="Test")
    assert agent.tier == "T2"


def test_parse_agent_tier_default_when_missing():
    """yaml 未声明 tier 时默认 T2。"""
    loader = ConfigLoader()
    raw = {"agent_id": "test", "domain": "test"}
    agent = loader._parse_agent(raw)
    assert agent.tier == "T2"


def test_parse_agent_tier_invalid_value_falls_back():
    """yaml tier 非法值（如 T5）降级为 T2。"""
    loader = ConfigLoader()
    raw = {"agent_id": "test", "domain": "test", "tier": "T5"}
    agent = loader._parse_agent(raw)
    assert agent.tier == "T2"


def test_parse_agent_tier_case_insensitive():
    """yaml tier 大小写不敏感（t3 → T3）。"""
    loader = ConfigLoader()
    raw = {"agent_id": "test", "domain": "test", "tier": "t3"}
    agent = loader._parse_agent(raw)
    assert agent.tier == "T3"


# ============================================================
# 3. ConfigLoader.validate() tier × allowed_tools 一致性
# ============================================================

def test_validate_tier_allowed_tools_consistency_pass():
    """T3 agent 允许 bash/write_file（T3 ≥ T2/T3）。"""
    loader = ConfigLoader()
    from orchestrator.config_loader import SystemConfig
    config = SystemConfig(
        agents={
            "test": AgentDefinition(
                agent_id="test",
                domain="test",
                display_name="Test",
                tier="T3",
                allowed_tools=["read_file", "write_file", "bash"],
            )
        }
    )
    errors = loader.validate(config)
    # 过滤掉 domain 校验错误（test domain 未定义）
    tier_errors = [e for e in errors if "tier=" in e]
    assert tier_errors == []


def test_validate_tier_allowed_tools_violation_t1_with_write_file():
    """T1 agent 不允许 write_file（需 T2）→ 校验失败。"""
    loader = ConfigLoader()
    from orchestrator.config_loader import SystemConfig
    config = SystemConfig(
        agents={
            "test": AgentDefinition(
                agent_id="test",
                domain="test",
                display_name="Test",
                tier="T1",
                allowed_tools=["read_file", "write_file"],  # write_file 需 T2
            )
        }
    )
    errors = loader.validate(config)
    tier_errors = [e for e in errors if "tier=" in e and "write_file" in e]
    assert len(tier_errors) == 1
    assert "tier=T1" in tier_errors[0]
    assert "需 T2" in tier_errors[0]


def test_validate_tier_allowed_tools_violation_t0_with_bash():
    """T0 agent 不允许 bash（需 T3）→ 校验失败。"""
    loader = ConfigLoader()
    from orchestrator.config_loader import SystemConfig
    config = SystemConfig(
        agents={
            "test": AgentDefinition(
                agent_id="test",
                domain="test",
                display_name="Test",
                tier="T0",
                allowed_tools=["bash"],  # bash 需 T3
            )
        }
    )
    errors = loader.validate(config)
    tier_errors = [e for e in errors if "tier=" in e and "bash" in e]
    assert len(tier_errors) == 1
    assert "tier=T0" in tier_errors[0]
    assert "需 T3" in tier_errors[0]


def test_validate_all_real_agents_pass():
    """所有真实 agent yaml 的 tier × allowed_tools 一致性校验通过。"""
    loader = ConfigLoader()
    config = loader.load_all()
    errors = loader.validate(config)
    tier_errors = [e for e in errors if "tier=" in e]
    assert tier_errors == [], f"agent tier × allowed_tools 不一致: {tier_errors}"


# ============================================================
# 4. workspace_paths.TOOL_TIER_MAP / required_tier_for_tool
# ============================================================

def test_tool_tier_map_covers_key_tools():
    """TOOL_TIER_MAP 覆盖关键工具。"""
    assert TOOL_TIER_MAP["read_file"] == "T1"
    assert TOOL_TIER_MAP["write_file"] == "T2"
    assert TOOL_TIER_MAP["bash"] == "T3"
    assert TOOL_TIER_MAP["ssh_exec"] == "T3"
    assert TOOL_TIER_MAP["server_restart"] == "T3"
    assert TOOL_TIER_MAP["db_migrate"] == "T3"


def test_required_tier_for_tool_known():
    """known tool 返回映射 tier。"""
    assert required_tier_for_tool("read_file") == "T1"
    assert required_tier_for_tool("write_file") == "T2"
    assert required_tier_for_tool("bash") == "T3"


def test_required_tier_for_tool_unknown_defaults_t0():
    """unknown tool 默认 T0。"""
    assert required_tier_for_tool("finalize") == "T0"
    assert required_tier_for_tool("nonexistent_tool") == "T0"


def test_requires_workspace_tools_contains_file_and_exec():
    """REQUIRES_WORKSPACE_TOOLS 包含文件读写 + 命令执行 + trigger_workflow。"""
    assert "read_file" in REQUIRES_WORKSPACE_TOOLS
    assert "write_file" in REQUIRES_WORKSPACE_TOOLS
    assert "bash" in REQUIRES_WORKSPACE_TOOLS
    assert "ssh_exec" in REQUIRES_WORKSPACE_TOOLS
    assert "trigger_workflow" in REQUIRES_WORKSPACE_TOOLS


# ============================================================
# 5. check_tool_tier_permission 三层校验
# ============================================================

def test_check_permission_general_chat_blocks_write_file():
    """通用对话（has_workspace=False）禁止 write_file。"""
    with pytest.raises(TierPermissionError) as exc:
        check_tool_tier_permission(
            tool_name="write_file",
            session_tier="T0",
            has_workspace=False,
        )
    assert "通用对话" in str(exc.value)
    assert "write_file" in str(exc.value)


def test_check_permission_general_chat_blocks_bash():
    """通用对话禁止 bash。"""
    with pytest.raises(TierPermissionError) as exc:
        check_tool_tier_permission(
            tool_name="bash",
            session_tier="T0",
            has_workspace=False,
        )
    assert "通用对话" in str(exc.value)


def test_check_permission_general_chat_blocks_trigger_workflow():
    """通用对话禁止 trigger_workflow。"""
    with pytest.raises(TierPermissionError) as exc:
        check_tool_tier_permission(
            tool_name="trigger_workflow",
            session_tier="T0",
            has_workspace=False,
        )
    assert "通用对话" in str(exc.value)


def test_check_permission_t0_session_blocks_read_file():
    """T0 session 绑定 workspace 后仍不能调 read_file（需 T1）。"""
    with pytest.raises(TierPermissionError) as exc:
        check_tool_tier_permission(
            tool_name="read_file",
            session_tier="T0",
            has_workspace=True,
        )
    assert "tier=T0" in str(exc.value)
    assert "需 T1" in str(exc.value)


def test_check_permission_t1_session_blocks_write_file():
    """T1 session 不能调 write_file（需 T2）。"""
    with pytest.raises(TierPermissionError) as exc:
        check_tool_tier_permission(
            tool_name="write_file",
            session_tier="T1",
            has_workspace=True,
        )
    assert "tier=T1" in str(exc.value)
    assert "需 T2" in str(exc.value)


def test_check_permission_t2_session_blocks_bash():
    """T2 session 不能调 bash（需 T3）。"""
    with pytest.raises(TierPermissionError) as exc:
        check_tool_tier_permission(
            tool_name="bash",
            session_tier="T2",
            has_workspace=True,
        )
    assert "tier=T2" in str(exc.value)
    assert "需 T3" in str(exc.value)


def test_check_permission_t3_session_allows_all():
    """T3 session 允许所有工具（read/write/bash）。"""
    check_tool_tier_permission("read_file", "T3", True)
    check_tool_tier_permission("write_file", "T3", True)
    check_tool_tier_permission("bash", "T3", True)
    check_tool_tier_permission("ssh_exec", "T3", True)


def test_check_permission_t1_session_allows_read_file():
    """T1 session 允许 read_file。"""
    check_tool_tier_permission("read_file", "T1", True)


def test_check_permission_t2_session_allows_write_file():
    """T2 session 允许 write_file。"""
    check_tool_tier_permission("write_file", "T2", True)


def test_check_permission_unknown_tool_always_allowed():
    """未知工具（不在 TOOL_TIER_MAP）默认 T0，任何 session tier 都允许。"""
    check_tool_tier_permission("custom_tool", "T0", has_workspace=False)
    check_tool_tier_permission("custom_tool", "T3", has_workspace=True)


# ============================================================
# 6. tier_compatible / effective_tier
# ============================================================

def test_tier_compatible_matrix():
    """workspace × agent tier 兼容矩阵（方案 §3.3）。"""
    # read_only (T1) × T0/T1 → 兼容
    assert tier_compatible("T1", "T0") is True
    assert tier_compatible("T1", "T1") is True
    # read_only (T1) × T2/T3 → 不兼容
    assert tier_compatible("T1", "T2") is False
    assert tier_compatible("T1", "T3") is False
    # read_write (T2) × T0/T1/T2 → 兼容
    assert tier_compatible("T2", "T0") is True
    assert tier_compatible("T2", "T1") is True
    assert tier_compatible("T2", "T2") is True
    # read_write (T2) × T3 → 不兼容
    assert tier_compatible("T2", "T3") is False
    # read_write_exec (T3) × 全部 → 兼容
    assert tier_compatible("T3", "T0") is True
    assert tier_compatible("T3", "T1") is True
    assert tier_compatible("T3", "T2") is True
    assert tier_compatible("T3", "T3") is True


def test_effective_tier_min_computation():
    """effective_tier = min(workspace, agent)。"""
    assert effective_tier("T1", "T3") == "T1"
    assert effective_tier("T3", "T1") == "T1"
    assert effective_tier("T2", "T2") == "T2"
    assert effective_tier("T3", "T3") == "T3"
    assert effective_tier("T0", "T3") == "T0"
    # workspace=null → 通用对话 → T0（传 T0 表示无 workspace）
    assert effective_tier("T0", "T2") == "T0"


# ============================================================
# 7. conversational._try_execute_tool_call 动态 tier 拦截
# ============================================================

@pytest.mark.asyncio
async def test_try_execute_tool_call_blocks_bash_on_t0_session():
    """_try_execute_tool_call 在 T0 session 调 bash → 返回拒绝文本。"""
    from orchestrator.conversation_kit import _try_execute_tool_call
    from harness import ToolDefinition

    async def bash_handler(args):
        return {"exit_code": 0, "stdout": "should not reach here"}

    tool_map = {
        "bash": ToolDefinition(
            name="bash",
            description="test",
            input_schema={},
            handler=bash_handler,
        )
    }

    json_str = '{"name":"bash","arguments":{"command":"ls"}}'
    result = await _try_execute_tool_call(
        text=json_str, start=0, end=len(json_str), json_str=json_str,
        tool_map=tool_map,
        session_tier="T0",
        has_workspace=True,
    )
    assert result is not None
    assert "拒绝" in result
    assert "bash" in result
    # handler 不应被执行
    assert "should not reach here" not in result


@pytest.mark.asyncio
async def test_try_execute_tool_call_blocks_write_file_general_chat():
    """_try_execute_tool_call 通用对话调 write_file → 拒绝。"""
    from orchestrator.conversation_kit import _try_execute_tool_call
    from harness import ToolDefinition

    async def write_handler(args):
        return {"ok": True}

    tool_map = {
        "write_file": ToolDefinition(
            name="write_file",
            description="test",
            input_schema={},
            handler=write_handler,
        )
    }

    json_str = '{"name":"write_file","arguments":{"path":"/etc/x","content":"x"}}'
    result = await _try_execute_tool_call(
        text=json_str, start=0, end=len(json_str), json_str=json_str,
        tool_map=tool_map,
        session_tier="T0",
        has_workspace=False,  # 通用对话
    )
    assert result is not None
    assert "拒绝" in result
    assert "通用对话" in result


@pytest.mark.asyncio
async def test_try_execute_tool_call_allows_t3_bash():
    """_try_execute_tool_call T3 session 调 bash → 正常执行。"""
    from orchestrator.conversation_kit import _try_execute_tool_call
    from harness import ToolDefinition

    async def bash_handler(args):
        return {"exit_code": 0, "stdout": "ok"}

    tool_map = {
        "bash": ToolDefinition(
            name="bash",
            description="test",
            input_schema={},
            handler=bash_handler,
        )
    }

    json_str = '{"name":"bash","arguments":{"command":"ls"}}'
    result = await _try_execute_tool_call(
        text=json_str, start=0, end=len(json_str), json_str=json_str,
        tool_map=tool_map,
        session_tier="T3",
        has_workspace=True,
    )
    assert result is not None
    assert "[tool:bash]" in result
    assert "ok" in result


@pytest.mark.asyncio
async def test_try_execute_tool_call_default_args_backward_compatible():
    """_try_execute_tool_call 不传 session_tier 默认 T3（向后兼容，不拦截）。"""
    from orchestrator.conversation_kit import _try_execute_tool_call
    from harness import ToolDefinition

    async def bash_handler(args):
        return {"exit_code": 0}

    tool_map = {
        "bash": ToolDefinition(
            name="bash",
            description="test",
            input_schema={},
            handler=bash_handler,
        )
    }

    json_str = '{"name":"bash","arguments":{}}'
    # 不传 session_tier / has_workspace → 默认 T3 + True → 不拦截
    result = await _try_execute_tool_call(
        text=json_str, start=0, end=len(json_str), json_str=json_str,
        tool_map=tool_map,
    )
    assert result is not None
    assert "[tool:bash]" in result
    assert "拒绝" not in result


@pytest.mark.asyncio
async def test_extract_and_run_tool_calls_propagates_tier():
    """_extract_and_run_tool_calls 把 session_tier 传给 _try_execute_tool_call。"""
    from orchestrator.conversation_kit import _extract_and_run_tool_calls
    from harness import ToolDefinition

    async def bash_handler(args):
        return {"exit_code": 0, "stdout": "leaked"}

    tools = [
        ToolDefinition(
            name="bash",
            description="test",
            input_schema={},
            handler=bash_handler,
        )
    ]

    text = '<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>'
    # T0 session + has_workspace → 拦截
    result, had_calls = await _extract_and_run_tool_calls(
        text, tools, None,
        session_tier="T0", has_workspace=True,
    )
    assert had_calls is True
    assert "拒绝" in result
    assert "leaked" not in result
