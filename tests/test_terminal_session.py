"""TerminalSessionManager 单测（V7 阶段，§4.3）。

验证：
- MockBackend：完整流程 create/send/capture/list/destroy
- TerminalSessionManager(backend="mock")：委托方法正常工作
- 自动检测：Windows psmux 不可用→mock（monkeypatch shutil.which）
- stream_pane：async generator 产出 capture_pane 结果 + 异常 yield ""
- PsmuxBackend/TmuxBackend._run：命令不存在/非零退出抛 RuntimeError
- _detect_available_backend：psmux/tmux/mock 三分支
- Windows ProactorEventLoop 守护：模块导入不抛异常 + helper 逻辑（mock 验证）
"""
import asyncio
import inspect
import sys

import pytest

from task import terminal_session as ts
from task.terminal_session import (
    MockBackend,
    PsmuxBackend,
    TerminalBackend,
    TerminalSessionManager,
    TmuxBackend,
    _detect_available_backend,
    _ensure_windows_proactor_policy,
)


# ============================================================
# MockBackend
# ============================================================

@pytest.mark.asyncio
class TestMockBackend:
    async def test_full_flow(self):
        b = MockBackend()
        # create
        sid = await b.create_session("task_1")
        assert sid == "task_1"
        # send_keys 累积
        await b.send_keys("task_1", "echo hello")
        await b.send_keys("task_1", "ls -la")
        # capture_pane 返回累积文本（join \n）
        pane = await b.capture_pane("task_1")
        assert pane == "echo hello\nls -la"
        # list
        assert await b.list_sessions() == ["task_1"]
        # 再建一个
        await b.create_session("task_2")
        assert set(await b.list_sessions()) == {"task_1", "task_2"}
        # destroy
        await b.destroy_session("task_1")
        assert await b.list_sessions() == ["task_2"]

    async def test_capture_unknown_returns_empty(self):
        b = MockBackend()
        assert await b.capture_pane("nope") == ""

    async def test_create_idempotent(self):
        b = MockBackend()
        await b.create_session("s")
        await b.create_session("s")  # 重复创建不抛、不清空
        await b.send_keys("s", "kept")
        assert await b.capture_pane("s") == "kept"
        assert await b.list_sessions() == ["s"]

    async def test_destroy_unknown_no_error(self):
        b = MockBackend()
        await b.destroy_session("ghost")  # 不抛


# ============================================================
# TerminalSessionManager 委托（mock backend）
# ============================================================

@pytest.mark.asyncio
class TestManagerMock:
    async def test_delegate_methods(self):
        mgr = TerminalSessionManager(backend="mock")
        assert mgr.backend_name == "mock"
        tid = await mgr.create_session("task_x")
        assert tid == "task_x"
        await mgr.send_keys(tid, "echo banner")
        pane = await mgr.capture_pane(tid)
        assert "echo banner" in pane
        assert await mgr.list_sessions() == ["task_x"]
        await mgr.destroy_session(tid)
        assert await mgr.list_sessions() == []

    async def test_invalid_backend_raises(self):
        with pytest.raises(ValueError):
            TerminalSessionManager(backend="bogus")


def test_backend_is_abstract():
    """TerminalBackend 不可实例化（ABC）。"""
    with pytest.raises(TypeError):
        TerminalBackend()  # type: ignore[abstract]


# ============================================================
# 自动检测（TerminalSessionManager() 无参）
# ============================================================

class TestAutoDetect:
    def test_windows_psmux_available(self, monkeypatch):
        monkeypatch.setattr(ts.sys, "platform", "win32")
        monkeypatch.setattr(ts.shutil, "which",
                            lambda cmd: "/usr/bin/psmux" if cmd == "psmux" else None)
        mgr = TerminalSessionManager()
        assert mgr.backend_name == "psmux"

    def test_windows_no_psmux_falls_back_conpty_host(self, monkeypatch):
        """Windows 无 psmux + winpty 可用 → conpty_host（业务内持久化终端）。"""
        monkeypatch.setattr(ts.sys, "platform", "win32")
        monkeypatch.setattr(ts.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(ts, "_winpty_available", lambda: True)
        # 不真正拉 host：用 MockBackend 顶替 ConPtyHostBackend 构造
        monkeypatch.setattr(ts, "ConPtyHostBackend", ts.MockBackend)
        mgr = TerminalSessionManager()
        assert mgr.backend_name == "conpty_host"

    def test_linux_tmux_available(self, monkeypatch):
        monkeypatch.setattr(ts.sys, "platform", "linux")
        monkeypatch.setattr(ts.shutil, "which",
                            lambda cmd: "/usr/bin/tmux" if cmd == "tmux" else None)
        mgr = TerminalSessionManager()
        assert mgr.backend_name == "tmux"

    def test_linux_no_tmux_falls_back_subprocess(self, monkeypatch):
        """Linux 无 tmux 时回退 subprocess（真实 shell 管道）。"""
        monkeypatch.setattr(ts.sys, "platform", "linux")
        monkeypatch.setattr(ts.shutil, "which", lambda cmd: None)
        mgr = TerminalSessionManager()
        assert mgr.backend_name == "subprocess"


# ============================================================
# _detect_available_backend
# ============================================================

class TestDetectAvailableBackend:
    def test_psmux(self, monkeypatch):
        monkeypatch.setattr(ts.sys, "platform", "win32")
        monkeypatch.setattr(ts.shutil, "which",
                            lambda cmd: "/p" if cmd == "psmux" else None)
        assert _detect_available_backend() == "psmux"

    def test_tmux(self, monkeypatch):
        monkeypatch.setattr(ts.sys, "platform", "linux")
        monkeypatch.setattr(ts.shutil, "which",
                            lambda cmd: "/t" if cmd == "tmux" else None)
        assert _detect_available_backend() == "tmux"

    def test_neither_returns_subprocess(self, monkeypatch):
        """tmux 缺失时回退 subprocess（真实 shell），不再选 mock。"""
        monkeypatch.setattr(ts.sys, "platform", "linux")
        monkeypatch.setattr(ts.shutil, "which", lambda cmd: None)
        assert _detect_available_backend() == "subprocess"

    def test_windows_no_psmux_returns_conpty_host(self, monkeypatch):
        """Windows 无 psmux 但 winpty/pyte 可用 → conpty_host（业务内持久化终端）。"""
        monkeypatch.setattr(ts.sys, "platform", "win32")
        monkeypatch.setattr(ts.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(ts, "_winpty_available", lambda: True)
        assert _detect_available_backend() == "conpty_host"

    def test_windows_no_winpty_returns_subprocess(self, monkeypatch):
        """Windows 无 psmux 且 winpty 缺失 → subprocess（真实 shell 管道降级）。"""
        monkeypatch.setattr(ts.sys, "platform", "win32")
        monkeypatch.setattr(ts.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(ts, "_winpty_available", lambda: False)
        assert _detect_available_backend() == "subprocess"

    def test_windows_ignores_tmux(self, monkeypatch):
        """Windows 上即使 tmux 可用也不选它（避免 WSL 依赖）。"""
        monkeypatch.setattr(ts.sys, "platform", "win32")
        monkeypatch.setattr(ts.shutil, "which",
                            lambda cmd: "/t" if cmd == "tmux" else None)
        monkeypatch.setattr(ts, "_winpty_available", lambda: True)
        assert _detect_available_backend() == "conpty_host"


# ============================================================
# stream_pane
# ============================================================

def test_stream_pane_is_async_generator():
    """stream_pane 是 async generator function。"""
    assert inspect.isasyncgenfunction(TerminalSessionManager.stream_pane)


@pytest.mark.asyncio
class TestStreamPane:
    async def test_yields_capture_pane_result(self):
        mgr = TerminalSessionManager(backend="mock")
        await mgr.create_session("s")
        await mgr.send_keys("s", "line1")
        seen: list[str] = []
        async for chunk in mgr.stream_pane("s", interval=0.01):
            seen.append(chunk)
            if len(seen) >= 2:
                break
        assert len(seen) == 2
        assert all("line1" in c for c in seen)

    async def test_exception_yields_empty_string(self, monkeypatch):
        mgr = TerminalSessionManager(backend="mock")

        async def boom(name: str) -> str:
            raise RuntimeError("capture failed")

        monkeypatch.setattr(mgr._backend, "capture_pane", boom)
        seen: list[str] = []
        async for chunk in mgr.stream_pane("s", interval=0.01):
            seen.append(chunk)
            if len(seen) >= 2:
                break
        assert seen == ["", ""]

    async def test_interval_over_half_is_capped(self, monkeypatch):
        """interval > 0.5 应被收敛到 0.5（≤500ms 约束）。"""
        slept: list[float] = []

        async def fake_sleep(seconds: float):
            slept.append(seconds)

        monkeypatch.setattr(ts.asyncio, "sleep", fake_sleep)
        mgr = TerminalSessionManager(backend="mock")
        await mgr.create_session("s")
        count = 0
        async for _ in mgr.stream_pane("s", interval=2.0):
            count += 1
            if count >= 2:
                break
        assert slept and all(s == 0.5 for s in slept)

    async def test_nonpositive_interval_uses_default(self, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(seconds: float):
            slept.append(seconds)

        monkeypatch.setattr(ts.asyncio, "sleep", fake_sleep)
        mgr = TerminalSessionManager(backend="mock")
        await mgr.create_session("s")
        count = 0
        async for _ in mgr.stream_pane("s", interval=0):
            count += 1
            if count >= 2:
                break
        assert slept and all(s == 0.5 for s in slept)


# ============================================================
# PsmuxBackend / TmuxBackend _run 失败
# ============================================================

class _FakeProc:
    """模拟 asyncio 子进程返回对象。"""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
class TestSubprocessBackendFailure:
    async def test_psmux_binary_missing(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise FileNotFoundError("psmux not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        b = PsmuxBackend()
        with pytest.raises(RuntimeError, match="psmux"):
            await b.create_session("x")

    async def test_tmux_binary_missing(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise FileNotFoundError("tmux not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        b = TmuxBackend()
        with pytest.raises(RuntimeError, match="tmux"):
            await b.create_session("y")

    async def test_psmux_nonzero_exit(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(1, b"", b"no server running")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        b = PsmuxBackend()
        with pytest.raises(RuntimeError, match="失败"):
            await b.create_session("z")

    async def test_tmux_list_sessions_empty_on_failure(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(1, b"", b"no server running")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        b = TmuxBackend()
        # list_sessions 在无 server 时不抛，返回 []
        assert await b.list_sessions() == []

    async def test_psmux_list_sessions_parses_output(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(0,
                             b"task_a: 1 windows (created ...)\n"
                             b"task_b: 2 windows (created ...)\n", b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        b = PsmuxBackend()
        assert await b.list_sessions() == ["task_a", "task_b"]

    async def test_tmux_list_sessions_parses_output(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(0, b"task_a\ntask_b\n", b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        b = TmuxBackend()
        assert await b.list_sessions() == ["task_a", "task_b"]

    async def test_send_keys_passes_enter_key(self, monkeypatch):
        captured: list[list[str]] = []

        async def fake_exec(*args, **kwargs):
            captured.append(list(args))
            return _FakeProc(0, b"", b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        b = TmuxBackend()
        await b.send_keys("sess", "echo hi")
        # args = [binary, "send-keys", "-t", "sess", "echo hi", "Enter"]
        assert captured[0][0] == "tmux"
        assert captured[0][1:] == ["send-keys", "-t", "sess", "echo hi", "Enter"]


# ============================================================
# Windows ProactorEventLoop 守护
# ============================================================

class TestWindowsProactorGuard:
    def test_module_imports_without_error(self):
        """模块已成功导入（顶部 import 已证明），且暴露预期 API。"""
        assert hasattr(ts, "TerminalSessionManager")
        assert hasattr(ts, "_ensure_windows_proactor_policy")
        assert hasattr(ts, "TerminalBackend")

    def test_non_windows_does_nothing(self, monkeypatch):
        monkeypatch.setattr(ts.sys, "platform", "linux")
        calls: list = []
        monkeypatch.setattr(ts.asyncio, "set_event_loop_policy",
                            lambda policy: calls.append(policy))
        _ensure_windows_proactor_policy()
        assert calls == []

    def test_windows_sets_proactor_policy(self, monkeypatch):
        monkeypatch.setattr(ts.sys, "platform", "win32")

        class FakePolicy:
            pass

        calls: list = []
        # WindowsProactorEventLoopPolicy 仅 Windows 存在，raising=False 跨平台兼容
        monkeypatch.setattr(ts.asyncio, "WindowsProactorEventLoopPolicy",
                            FakePolicy, raising=False)
        monkeypatch.setattr(ts.asyncio, "set_event_loop_policy",
                            lambda policy: calls.append(policy))
        _ensure_windows_proactor_policy()
        assert len(calls) == 1
        assert isinstance(calls[0], FakePolicy)

    def test_windows_set_failure_swallowed(self, monkeypatch):
        """非主线程或策略设置失败时不抛。"""
        monkeypatch.setattr(ts.sys, "platform", "win32")

        class FakePolicy:
            pass

        def boom(policy):
            raise RuntimeError("not main thread")

        monkeypatch.setattr(ts.asyncio, "WindowsProactorEventLoopPolicy",
                            FakePolicy, raising=False)
        monkeypatch.setattr(ts.asyncio, "set_event_loop_policy", boom)
        # 不抛异常
        _ensure_windows_proactor_policy()

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="仅 Windows 验证 Proactor 策略已设置")
    def test_proactor_policy_active_on_windows(self):
        assert isinstance(asyncio.get_event_loop_policy(),
                          asyncio.WindowsProactorEventLoopPolicy)
