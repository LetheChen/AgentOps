"""D-068 解释器锚定回归测试。

背景：2026-08-29 smart-query 工作流全链路失败，根因是 command 节点
create_subprocess_shell 按 PATH 解析裸 `python`，漂移到 WorkBuddy managed
3.13（缺 claude_agent_sdk/sqlglot）。修复 = 模板开头裸 `python` 锚定为
后端自身解释器 sys.executable。

测试直接连 anchor_cli_interpreter 纯函数（遵循项目"纯逻辑抽函数供测试直连"约定）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先导入 audit 打破 workflow.engine 的循环导入（与 test_p018_5_engine_integration.py 一致）
from audit import SqliteEventStore  # noqa: F401
from workflow.engine import anchor_cli_interpreter


def test_anchor_replaces_leading_bare_python():
    """smart-query 实际模板形态：python tools/db_cli.py ..."""
    out = anchor_cli_interpreter("python tools/db_cli.py resolve-schema --database mysql:audit_reader")
    assert out == f'"{sys.executable}" tools/db_cli.py resolve-schema --database mysql:audit_reader'


def test_anchor_replaces_python_c_form():
    """refuse 兜底节点模板形态：python -c "..." """
    out = anchor_cli_interpreter('python -c "import json; print(1)"')
    assert out.startswith(f'"{sys.executable}" -c ')


def test_anchor_noop_for_docker_exec_template():
    """容器内执行模板（docker exec ... python ...）不以 python 开头，不受影响。"""
    tpl = 'docker exec my_ctr python -c "print(1)"'
    assert anchor_cli_interpreter(tpl) == tpl


def test_anchor_noop_for_non_python_template():
    """非 python 开头的模板（curl/等）原样返回。"""
    tpl = "curl -s http://127.0.0.1:1987/health"
    assert anchor_cli_interpreter(tpl) == tpl


def test_anchor_handles_bare_python_only():
    """模板就是裸 `python`（无参数）也要替换。"""
    out = anchor_cli_interpreter("python")
    assert out == f'"{sys.executable}"'


def test_anchor_does_not_touch_python_mid_string():
    """python 出现在参数中间（如文件名）不被替换。"""
    tpl = "run_tool.sh --use python3.12"
    assert anchor_cli_interpreter(tpl) == tpl
