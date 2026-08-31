"""TerminalSessionManager — 终端会话抽象层（psmux/tmux/subprocess/mock 四后端）。

设计文档：docs/product-design/DESIGN_task_management_module_v1.md §4.3

Windows 约束（评审缺点 4 修复）：
    psmux/tmux/subprocess 后端均依赖 asyncio.create_subprocess_exec，Windows 上子进程
    IO 必须在 ProactorEventLoop 上运行（默认 SelectorEventLoop 不支持子进程
    stdout/stderr 管道，会抛 NotImplementedError）。因此模块导入时即设置
    WindowsProactorEventLoopPolicy（非 Windows / 非主线程 / 已设置时自动跳过）。

    启动命令须用 `python -u -m uvicorn api.server:app --host 0.0.0.0 --port 8000`
    （-u 关闭 stdout 缓冲，SSE/terminal 流不卡顿），禁止 --reload（reload 会 fork
    子进程并重置事件循环策略，导致 psmux 子进程丢失）。

后端选择策略：
    - Windows：首选 psmux（原生），不可用回退 subprocess（真实 shell 管道）
    - 非 Windows：首选 tmux，不可用回退 subprocess
    - mock 仅在显式指定（单测）时使用

与 TaskOrchestrator.execute_coding 的契约：
    - create_session(name) -> str：返回 terminal_id（psmux/tmux 中即 session name）
    - send_keys(terminal_id, text) -> None：真实后端 = 写 shell stdin（执行命令）
    - append_output(terminal_id, text) -> None：直接写 scrollback（agent 输出 tee）
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import AsyncIterator


# ============================================================
# Windows ProactorEventLoop 守护
# ============================================================

def _ensure_windows_proactor_policy() -> None:
    """Windows 下确保 ProactorEventLoop（子进程 IO 必需）。

    非主线程或已设置时 set_event_loop_policy 可能抛错，忽略即可；非 Windows 直接返回。
    """
    if sys.platform != "win32":
        return
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        # 非主线程或策略不可用，忽略（不影响已运行的 loop）
        pass


# 模块导入即守护，防遗漏
_ensure_windows_proactor_policy()


# ============================================================
# TerminalBackend 抽象基类
# ============================================================

class TerminalBackend(ABC):
    """终端后端协议：屏蔽 psmux / tmux / subprocess / mock 差异。"""

    @abstractmethod
    async def create_session(self, name: str) -> str:
        """创建会话，返回 session id（= name）。"""

    @abstractmethod
    async def capture_pane(self, name: str) -> str:
        """捕获当前 pane 文本。"""

    @abstractmethod
    async def send_keys(self, name: str, keys: str) -> None:
        """向会话发送按键（真实后端 = 写 shell stdin，命令会被执行）。"""

    @abstractmethod
    async def list_sessions(self) -> list[str]:
        """列出所有会话名。"""

    @abstractmethod
    async def destroy_session(self, name: str) -> None:
        """销毁会话。"""

    async def append_output(self, name: str, text: str) -> None:
        """向 scrollback 直接写文本（agent 输出 tee，不经过 shell 执行）。

        默认实现回退 send_keys（mock 等纯缓冲后端语义等价）；
        真实 shell 后端必须覆盖，避免把输出文本当命令执行。
        """
        await self.send_keys(name, text)


# ============================================================
# 子进程后端基类（psmux/tmux 共用）
# ============================================================

class _SubprocessBackend(TerminalBackend):
    """psmux/tmux 共用基类：子进程调用 + 命令格式一致，仅二进制名不同。"""

    _binary: str = ""

    async def _run(self, *args: str) -> str:
        """调用后端二进制，返回 stdout 文本。失败抛 RuntimeError（明确错误）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"{self._binary} 未安装或不在 PATH 中") from e
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self._binary} {' '.join(args)} 失败 (rc={proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}")
        return stdout.decode(errors="replace")

    async def create_session(self, name: str) -> str:
        # new-session / new 两种写法 psmux/tmux 均接受 new-session 别名
        await self._run("new-session", "-s", name, "-d")
        return name

    async def capture_pane(self, name: str) -> str:
        return await self._run("capture-pane", "-t", name, "-p")

    async def send_keys(self, name: str, keys: str) -> None:
        # 末尾 Enter 作为独立 key，模拟回车提交
        await self._run("send-keys", "-t", name, keys, "Enter")

    async def destroy_session(self, name: str) -> None:
        await self._run("kill-session", "-t", name)

    async def list_sessions(self) -> list[str]:
        # 子类覆盖：解析格式不同
        raise NotImplementedError


# ============================================================
# PsmuxBackend（Windows 原生首选）
# ============================================================

class PsmuxBackend(_SubprocessBackend):
    """Windows 原生 psmux（首选）。命令对齐 tmux。"""

    _binary = "psmux"

    async def list_sessions(self) -> list[str]:
        # psmux ls 输出形如 `name: 1 windows (created ...)`，按 `:` 取首段
        try:
            out = await self._run("ls")
        except RuntimeError:
            # 无运行中的 server / 无会话
            return []
        sessions: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            sessions.append(line.split(":", 1)[0].strip())
        return sessions


# ============================================================
# TmuxBackend（WSL2/Linux 降级备选）
# ============================================================

class TmuxBackend(_SubprocessBackend):
    """WSL2/Linux tmux（降级备选）。命令与 psmux 完全一致，仅二进制名 tmux。"""

    _binary = "tmux"

    async def list_sessions(self) -> list[str]:
        # -F #{session_name} 每行输出一个会话名
        try:
            out = await self._run("list-sessions", "-F", "#{session_name}")
        except RuntimeError:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]


# ============================================================
# SubprocessBackend（真实 shell 管道，psmux/tmux 缺失时的降级方案）
# ============================================================

class SubprocessBackend(TerminalBackend):
    """真实 shell 子进程后端：spawn cmd.exe / sh，stdin/stdout 管道。

    - send_keys：写 shell stdin（命令真实执行，前端输入框可直接敲命令）
    - append_output：直接写 scrollback（coding agent 输出 tee，不当命令执行）
    - capture_pane：返回 scrollback 尾部（默认 400 行）

    Windows 用 cmd.exe /q（关回显），非 Windows 用 /bin/sh。
    """

    _SCROLLBACK_LINES = 400

    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._buffers: dict[str, list[str]] = {}
        self._readers: dict[str, asyncio.Task] = {}
        # 会话工作目录（create_session 时可指定，agent 会话 = workspace）
        self._cwds: dict[str, str] = {}

    async def create_session(self, name: str, cwd: str = "") -> str:
        if name in self._procs:
            return name
        if sys.platform == "win32":
            argv = ["cmd.exe", "/q", "/d"]
        else:
            argv = ["/bin/sh"]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd or None,
        )
        self._procs[name] = proc
        self._buffers[name] = [f"[session {name} started]"]
        if cwd:
            self._cwds[name] = cwd
        # 后台持续读 stdout → scrollback
        self._readers[name] = asyncio.create_task(self._drain(name))
        return name

    async def _drain(self, name: str) -> None:
        proc = self._procs.get(name)
        buf = self._buffers.get(name)
        if proc is None or proc.stdout is None or buf is None:
            return
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                buf.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                del buf[:-self._SCROLLBACK_LINES]
        except (asyncio.CancelledError, Exception):
            pass

    async def capture_pane(self, name: str) -> str:
        buf = self._buffers.get(name)
        if not buf:
            return ""
        return "\n".join(buf)

    async def send_keys(self, name: str, keys: str) -> None:
        proc = self._procs.get(name)
        if proc is None or proc.stdin is None:
            return
        # 命令回显（cmd /q 不回显，手动补一条保证可读性）
        buf = self._buffers.get(name)
        if buf is not None and keys.strip():
            buf.append(f"> {keys}")
        try:
            proc.stdin.write((keys + "\r\n" if sys.platform == "win32"
                              else keys + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass

    async def append_output(self, name: str, text: str) -> None:
        """agent 输出 tee：直接写 scrollback，不进 shell。"""
        buf = self._buffers.get(name)
        if buf is None:
            return
        for line in text.splitlines() or [text]:
            buf.append(line)
        del buf[:-self._SCROLLBACK_LINES]

    async def list_sessions(self) -> list[str]:
        return [n for n, p in self._procs.items() if p.returncode is None]

    async def destroy_session(self, name: str) -> None:
        reader = self._readers.pop(name, None)
        if reader:
            reader.cancel()
        proc = self._procs.pop(name, None)
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, Exception):
                pass
        self._buffers.pop(name, None)
        self._cwds.pop(name, None)


# ============================================================
# MockBackend（单测/显式指定）
# ============================================================

class MockBackend(TerminalBackend):
    """单测/无 psmux/tmux 时的 mock（不真正起进程）。

    buffer 用 dict[str, list[str]] 存储：每个 session 的 send_keys 文本累积。
    capture_pane 返回该 session 累积的所有文本（join "\\n"）。
    """

    def __init__(self) -> None:
        self._buffers: dict[str, list[str]] = {}

    async def create_session(self, name: str) -> str:
        if name not in self._buffers:
            self._buffers[name] = []
        return name

    async def capture_pane(self, name: str) -> str:
        if name not in self._buffers:
            return ""
        return "\n".join(self._buffers[name])

    async def send_keys(self, name: str, keys: str) -> None:
        if name not in self._buffers:
            self._buffers[name] = []
        self._buffers[name].append(keys)

    async def list_sessions(self) -> list[str]:
        return list(self._buffers.keys())

    async def destroy_session(self, name: str) -> None:
        self._buffers.pop(name, None)


# ============================================================
# ConPtyHostBackend（独立常驻 ConPTY 宿主：业务内持久化终端）
# ============================================================

def _winpty_available() -> bool:
    """pywinpty + pyte 是否可 import（Windows ConPTY 方案依赖）。"""
    try:
        import pyte  # noqa: F401
        import winpty  # noqa: F401
        return True
    except ImportError:
        return False


class ConPtyHostBackend(TerminalBackend):
    """经独立 terminal_host 进程管理 ConPTY 会话（Windows 推荐）。

    - 会话由 host 进程持有（DETACHED，独立存活）：AgentOps 后端重启/升级时
      claude/codex TUI 与 shell 会话不丢，重启后自动重连（create 幂等）
    - TUI 完整可用：ConPTY 伪终端 + host 内 pyte 终端模拟（VT100/ANSI）
    - HTTP 走 127.0.0.1:1988（urllib + asyncio.to_thread，零额外依赖）
    """

    HOST_URL = "http://127.0.0.1:1988"

    def __init__(self) -> None:
        self._ensure_host()

    # ---------- host 进程管理 ----------

    def _http_sync(self, method: str, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.HOST_URL + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
        return json.loads(body) if body else {}

    async def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        return await asyncio.to_thread(self._http_sync, method, path, payload)

    def _health_sync(self) -> bool:
        try:
            self._http_sync("GET", "/health")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _ensure_host(self) -> None:
        """host 未运行则拉起（DETACHED 独立进程，后端退出不影响它）。"""
        if self._health_sync():
            return
        host_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "terminal_host.py")
        if not os.path.isfile(host_py):
            raise RuntimeError(f"terminal_host.py not found: {host_py}")
        # DETACHED_PROCESS(0x8) | CREATE_NEW_PROCESS_GROUP(0x200)：
        # 独立进程组 + 无控制台，宿主与 AgentOps 后端生命周期解耦
        subprocess.Popen(
            [sys.executable, "-u", host_py],
            cwd=os.path.dirname(host_py),
            creationflags=0x00000008 | 0x00000200,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True)
        for _ in range(30):  # 最多等 ~9s
            if self._health_sync():
                return
            time.sleep(0.3)
        raise RuntimeError("terminal_host 拉起失败（127.0.0.1:1988 无响应）")

    # ---------- TerminalBackend 协议 ----------

    async def create_session(self, name: str, cwd: str = "",
                             command: list | None = None) -> str:
        await self._call("POST", "/sessions",
                         {"name": name, "cwd": cwd, "command": command})
        return name

    async def capture_pane(self, name: str) -> str:
        r = await self._call("GET", f"/sessions/{name}/screen")
        return r.get("content", "")

    async def send_keys(self, name: str, keys: str) -> None:
        # 与 psmux/tmux 对齐：keys + Enter（提交语义；TUI 场景即“发送这条输入”）
        await self._call("POST", f"/sessions/{name}/input",
                         {"data": keys + "\r"})

    async def append_output(self, name: str, text: str) -> None:
        await self._call("POST", f"/sessions/{name}/append", {"text": text})

    async def list_sessions(self) -> list[str]:
        r = await self._call("GET", "/sessions")
        return [s["name"] for s in r.get("sessions", []) if s.get("alive")]

    async def destroy_session(self, name: str) -> None:
        await self._call("DELETE", f"/sessions/{name}")


# ============================================================
# 后端检测
# ============================================================

def _detect_available_backend() -> str:
    """检测可用的终端后端。

    Windows：psmux（原生多路复用，未装）→ conpty_host（独立常驻 ConPTY 宿主，
    TUI 完整可用 + 后端重启会话不丢，推荐）→ subprocess（真实 shell 管道降级）。
    非 Windows：tmux → subprocess。
    mock 不自动选择（仅单测显式指定）——保证终端真实可用。
    """
    if sys.platform == "win32":
        if shutil.which("psmux"):
            return "psmux"
        if _winpty_available():
            return "conpty_host"
        return "subprocess"
    if shutil.which("tmux"):
        return "tmux"
    return "subprocess"


# ============================================================
# TerminalSessionManager
# ============================================================

class TerminalSessionManager:
    """运行时按环境自动选后端：Windows → psmux/conpty_host，否则 tmux。

    Args:
        backend: "psmux"/"conpty_host"/"tmux"/"subprocess"/"mock" 显式指定；
                 None 自动检测。
    """

    def __init__(self, backend: str | None = None) -> None:
        if backend is None:
            backend = _detect_available_backend()
        if backend == "psmux":
            self._backend: TerminalBackend = PsmuxBackend()
        elif backend == "conpty_host":
            self._backend: TerminalBackend = ConPtyHostBackend()
        elif backend == "tmux":
            self._backend: TerminalBackend = TmuxBackend()
        elif backend == "subprocess":
            self._backend: TerminalBackend = SubprocessBackend()
        elif backend == "mock":
            self._backend: TerminalBackend = MockBackend()
        else:
            raise ValueError(
                f"未知 backend: {backend!r}（可选 psmux/conpty_host/tmux/subprocess/mock）")
        self._backend_name = backend

    @property
    def backend_name(self) -> str:
        """当前后端名（psmux/conpty_host/tmux/subprocess/mock）。"""
        return self._backend_name

    async def create_session(self, name: str, cwd: str = "",
                             command: list | None = None) -> str:
        """创建终端会话。

        - conpty_host：cwd + command 均支持（command 直接跑 TUI，如 claude/codex）
        - subprocess：仅 cwd（command 由调用方经 send_keys 处理）
        - psmux/tmux/mock：均忽略（幂等）
        """
        if isinstance(self._backend, ConPtyHostBackend):
            return await self._backend.create_session(name, cwd=cwd,
                                                      command=command)
        if isinstance(self._backend, SubprocessBackend):
            return await self._backend.create_session(name, cwd=cwd)
        return await self._backend.create_session(name)

    async def capture_pane(self, name: str) -> str:
        return await self._backend.capture_pane(name)

    async def send_keys(self, terminal_id: str, text: str) -> None:
        await self._backend.send_keys(terminal_id, text)

    async def append_output(self, terminal_id: str, text: str) -> None:
        """agent 输出 tee：直接写 scrollback（真实 shell 后端不当命令执行）。"""
        await self._backend.append_output(terminal_id, text)

    async def list_sessions(self) -> list[str]:
        return await self._backend.list_sessions()

    async def destroy_session(self, name: str) -> None:
        await self._backend.destroy_session(name)

    async def stream_pane(self, name: str,
                          interval: float = 0.5) -> AsyncIterator[str]:
        """定时 capture-pane 供 SSE，async generator。

        - 间隔 ≤ 500ms（interval > 0.5 自动收敛到 0.5；非正用默认 0.5）
        - capture_pane 异常时 yield "" 不抛（保证 SSE 不断流）
        - 无限产出，消费方 break / aclose 即停止
        """
        if interval <= 0 or interval > 0.5:
            interval = 0.5
        while True:
            try:
                yield await self._backend.capture_pane(name)
            except Exception:
                yield ""
            await asyncio.sleep(interval)
