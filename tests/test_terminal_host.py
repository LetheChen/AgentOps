"""terminal_host 集成测试：真实 ConPTY 会话（Windows + pywinpty 环境）。

覆盖：host 子进程拉起、shell 会话交互（echo 执行）、append tee、销毁。
TUI（claude/codex）渲染不进 CI——依赖外部 CLI 与账号，浏览器验收覆盖。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytest.importorskip("winpty")
pytest.importorskip("pyte")

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="ConPTY 仅 Windows")

HOST_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "task", "terminal_host.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def host_port():
    """起独立 host 子进程（随机端口），测完杀掉。"""
    port = _free_port()
    env = dict(os.environ, TERMINAL_HOST_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "-u", HOST_PY], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("terminal_host 未在预期时间内就绪")
    yield port
    proc.kill()


def _call(port: int, method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=15).read() or b"{}")


def _screen(port: int, name: str) -> str:
    return _call(port, "GET", f"/sessions/{name}/screen").get("content", "")


@pytest.mark.asyncio
async def test_shell_session_interactive(host_port):
    """shell 会话：echo 命令真实执行并渲染到屏幕。"""
    _call(host_port, "POST", "/sessions",
          {"name": "it_shell", "cwd": os.getcwd()})
    time.sleep(1)
    _call(host_port, "POST", "/sessions/it_shell/input",
          {"data": "echo integration-ok\r"})
    # 等命令执行 + pyte 渲染
    for _ in range(20):
        time.sleep(0.5)
        if "integration-ok" in _screen(host_port, "it_shell"):
            break
    assert "integration-ok" in _screen(host_port, "it_shell")
    _call(host_port, "DELETE", "/sessions/it_shell")


@pytest.mark.asyncio
async def test_append_tee(host_port):
    """append：tee 文本进 scrollback（agent 输出可见）。"""
    _call(host_port, "POST", "/sessions", {"name": "it_tee"})
    time.sleep(0.5)
    _call(host_port, "POST", "/sessions/it_tee/append",
          {"text": "[agent] tee-ok line"})
    assert "tee-ok" in _screen(host_port, "it_tee")
    _call(host_port, "DELETE", "/sessions/it_tee")


@pytest.mark.asyncio
async def test_create_idempotent(host_port):
    """create 幂等：重名不报错（后端重启重连语义）。"""
    _call(host_port, "POST", "/sessions", {"name": "it_dup"})
    r = _call(host_port, "POST", "/sessions", {"name": "it_dup"})
    assert r == {"ok": True, "reused": True}
    _call(host_port, "DELETE", "/sessions/it_dup")
