"""terminal 模式执行驱动器单测（T1/T2，DESIGN_terminal_native_execution.md）。

验证：
- TerminalExecDriver.drive 全流程：ready/trust → prompt 落文件+短指令注入
  → 完成监测（内容稳定）→ transcript 提取 → 闭环收尾
- transcript 解析：claude jsonl（final_text/tokens）/ codex rollout jsonl（尽力）
- execute_coding exec_mode=terminal：conpty_host 走 TUI 派发 + 带 command 创建会话
- 降级：exec_mode=terminal 但 mock 后端 → tee；exec_mode=tee 显式 → tee

FakeTerminal：内存回放 TUI 帧（capture_pane 按脚本逐帧返回），
不依赖真实 ConPTY/CLI。
"""
import json
import os
import time
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audit.store import SqliteEventStore
from task.store import TaskStore
from task.orchestrator import TaskOrchestrator
from task.terminal_exec import TerminalExecDriver


# ============================================================
# FakeTerminal：脚本化 TUI 帧回放
# ============================================================

class FakeTerminal:
    """内存 terminal：capture_pane 逐帧回放脚本，send_keys/append_output 记录。"""

    backend_name = "conpty_host"

    def __init__(self, frames: list[str]):
        self._frames = list(frames)
        self.sent_keys: list[str] = []
        self.outputs: list[str] = []
        self.created: list[dict] = []
        self._frame_idx = 0

    async def create_session(self, name: str, cwd: str = "",
                             command: list | None = None) -> str:
        self.created.append({"name": name, "cwd": cwd, "command": command})
        return name

    async def capture_pane(self, terminal_id: str) -> str:
        if self._frame_idx < len(self._frames):
            frame = self._frames[self._frame_idx]
            self._frame_idx += 1
            return frame
        # 帧耗尽：停在最后一帧（模拟 TUI 静止）
        return self._frames[-1] if self._frames else ""

    async def send_keys(self, terminal_id: str, text: str) -> None:
        self.sent_keys.append(text)

    async def append_output(self, terminal_id: str, text: str) -> None:
        self.outputs.append(text)


class FakeOrchestrator:
    """闭环 spy：记录 _finalize_execution 调用参数。"""

    def __init__(self, store):
        self.store = store
        self._terminal = None
        self.finalized: dict | None = None

    async def _finalize_execution(self, *, task, harness, run_id,
                                  final_text, tokens_in, tokens_out, out=None):
        self.finalized = {"task_id": task["task_id"], "harness": harness,
                          "run_id": run_id, "final_text": final_text,
                          "tokens_in": tokens_in, "tokens_out": tokens_out}


def _claude_frames(working_frames: int = 3) -> list[str]:
    """典型 claude TUI 帧序列：trust → ready → working × N → idle 静止。"""
    frames = [
        "Do you trust the files in this folder?",
        "? for shortcuts",
    ]
    frames += [f"✳ Thinking… {i}" for i in range(working_frames)]
    frames += [
        "任务完成\n## 设计笔记\n- [a/b] 做了 X；为什么：Y",
        "任务完成\n## 设计笔记\n- [a/b] 做了 X；为什么：Y",
        "任务完成\n## 设计笔记\n- [a/b] 做了 X；为什么：Y",
        "任务完成\n## 设计笔记\n- [a/b] 做了 X；为什么：Y",
    ]
    return frames


def _write_claude_transcript(root: str, workspace: str, lines: list[dict]):
    """写 fixture claude jsonl 到 root/projects/<munged>/ 下。"""
    import re
    munged = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(workspace))
    d = os.path.join(root, "projects", munged)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "session_test.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for row in lines:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.utime(p, (time.time(), time.time()))
    return p


# ============================================================
# fixtures
# ============================================================

@pytest_asyncio.fixture
async def task_env(tmp_path):
    """真 TaskStore + 一个 in_progress 任务 + workspace 目录。"""
    db = os.path.join(tmp_path, "t.db")
    conn = SqliteEventStore(db_path=db, task_v1_enabled=True)
    store = TaskStore(conn._conn, conn._db_lock)
    orch = TaskOrchestrator(store, p0_mode=False)
    await orch.store.create_project(
        project_id="proj_te", name="terminal执行测试", type="code")
    await orch.store.create_task(
        task_id="t_te1", project_id="proj_te", title="终端执行",
        risk_level="low")
    task = await orch.store.get_task("t_te1")
    for target in ["discussing", "decomposing", "reviewing", "backlog",
                   "in_progress"]:
        r = await orch.advance_stage(
            task_id="t_te1", target_status=target,
            if_version=task["version"], actor="user")
        assert r["ok"], f"推进失败: {r}"
        task = r["task"]
    workspace = os.path.join(tmp_path, "ws")
    os.makedirs(workspace, exist_ok=True)
    yield orch, task, workspace
    conn._conn.close()


# ============================================================
# T1：driver 单元
# ============================================================

@pytest.mark.asyncio
class TestTerminalExecDriver:

    async def test_drive_happy_path(self, task_env, tmp_path):
        """全流程：trust 自动确认 → 短指令注入 → 稳定判完成 → transcript 提取 → 闭环。"""
        orch, task, workspace = task_env
        terminal = FakeTerminal(_claude_frames())

        claude_root = os.path.join(tmp_path, "claude")
        _write_claude_transcript(claude_root, workspace, [
            {"type": "user", "message": {"role": "user", "content": "执行任务"}},
            {"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "任务完成\n## 设计笔记\n- [a/b] 做了 X；为什么：Y"}],
                "usage": {"input_tokens": 100, "output_tokens": 50}}},
            {"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "任务完成\n## 设计笔记\n- [a/b] 做了 X；为什么：Y"}],
                "usage": {"input_tokens": 150, "cache_read_input_tokens": 30,
                          "output_tokens": 80}}},
        ])

        fake_orch = FakeOrchestrator(orch.store)
        driver = TerminalExecDriver(terminal, orch.store,
                                    claude_root=claude_root,
                                    codex_root=os.path.join(tmp_path, "codex"))
        await driver.drive(
            orchestrator=fake_orch, terminal_id="task_t_te1", task=task,
            harness="claude_code", workspace=workspace,
            system_prompt="# 任务\n- 标题: 终端执行", run_id="run_test1")

        # trust 弹窗被自动 Enter（send_keys 空串）
        assert "" in terminal.sent_keys
        # 短指令注入（单行，含 prompt 文件相对路径）
        inject = [k for k in terminal.sent_keys if k and "task_t_te1_prompt.md" in k]
        assert len(inject) == 1
        assert "\n" not in inject[0]
        # prompt 文件已落盘
        prompt_file = os.path.join(workspace, ".agentops", "task_t_te1_prompt.md")
        assert os.path.isfile(prompt_file)
        assert "终端执行" in open(prompt_file, encoding="utf-8").read()

        # transcript 提取：final_text + tokens（in=max+cache，out=sum）
        fin = fake_orch.finalized
        assert fin is not None
        assert "设计笔记" in fin["final_text"]
        assert fin["tokens_in"] == 180  # max(100, 150+30)
        assert fin["tokens_out"] == 130  # 50+80

        # 状态行写入窗格（可观测）
        assert any("[terminal-exec] 完成" in o for o in terminal.outputs)

        # activity：run_complete 记录（exec_mode=terminal）
        acts = await orch.store.list_activities("t_te1")
        rc = [a for a in acts if "run_complete" in a["changes"]]
        assert len(rc) == 1
        assert rc[0]["changes"]["run_complete"]["after"]["exec_mode"] == "terminal"

    async def test_transcript_missing_falls_back_to_pane(self, task_env, tmp_path):
        """transcript 找不到 → pane 尾部文本兜底（source=pane，tokens=0）。"""
        orch, task, workspace = task_env
        terminal = FakeTerminal(_claude_frames())
        fake_orch = FakeOrchestrator(orch.store)
        driver = TerminalExecDriver(
            terminal, orch.store,
            claude_root=os.path.join(tmp_path, "claude_nonexistent"),
            codex_root=os.path.join(tmp_path, "codex"))
        await driver.drive(
            orchestrator=fake_orch, terminal_id="task_t_te1", task=task,
            harness="claude_code", workspace=workspace,
            system_prompt="# 任务", run_id="run_test2")

        fin = fake_orch.finalized
        assert fin is not None
        assert "设计笔记" in fin["final_text"]  # 来自 pane 尾部
        assert fin["tokens_in"] == 0
        acts = await orch.store.list_activities("t_te1")
        rc = [a for a in acts if "run_complete" in a["changes"]][0]
        assert rc["changes"]["run_complete"]["after"]["result_source"] == "pane"

    async def test_monitor_timeout_keeps_task_open(self, task_env, tmp_path,
                                                   monkeypatch):
        """完成监测超时：任务保留 in_progress，不调 finalize。"""
        import task.terminal_exec as te
        orch, task, workspace = task_env
        # working 特征持续变化 → 永不稳定；monitor 超时改小加速测试
        # （T4 后超时从 features 配置读取，注入缓存覆盖）
        monkeypatch.setattr(te, "_FEATURES_CACHE", {
            "launch_cmd": te.LAUNCH_CMD, "launch_cli": te.LAUNCH_CLI,
            "ready_hints": te.READY_HINTS, "trust_hints": te.TRUST_HINTS,
            "working_hints": te.WORKING_HINTS,
            "timeouts": {
                "ready_timeout_s": 30.0, "monitor_timeout_s": 0.5,
                "monitor_interval_s": 0.05, "monitor_stable_n": 4,
                "flush_wait_s": 0.01,
                "transcript_since_slack_s": 5.0}})
        frames = ["? for shortcuts"] + \
            [f"✳ Working {i}" for i in range(1000)]
        terminal = FakeTerminal(frames)
        fake_orch = FakeOrchestrator(orch.store)
        driver = TerminalExecDriver(
            terminal, orch.store,
            claude_root=os.path.join(tmp_path, "claude_x"),
            codex_root=os.path.join(tmp_path, "codex_x"))
        await driver.drive(
            orchestrator=fake_orch, terminal_id="task_t_te1", task=task,
            harness="claude_code", workspace=workspace,
            system_prompt="# 任务", run_id="run_test3")

        assert fake_orch.finalized is None  # 未闭环
        acts = await orch.store.list_activities("t_te1")
        assert any("terminal_exec_timeout" in a["changes"] for a in acts)
        fresh = await orch.store.get_task("t_te1")
        assert fresh["status"] == "in_progress"  # 未强转


# ============================================================
# T1：transcript 解析（纯函数）
# ============================================================

class TestTranscriptParsing:

    def test_claude_parse(self, tmp_path):
        p = os.path.join(tmp_path, "s.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            rows = [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "第一轮回复"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}},
                {"type": "assistant", "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash"},
                        {"type": "text", "text": "最终回复"}],
                    "usage": {"input_tokens": 20, "cache_read_input_tokens": 5,
                              "output_tokens": 7}}},
            ]
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        r = TerminalExecDriver._parse_claude_transcript(p)
        assert r["final_text"] == "最终回复"  # 最后一条 assistant 的 text
        assert r["tokens_in"] == 25  # max(10, 20+5)
        assert r["tokens_out"] == 12  # 5+7

    def test_codex_parse(self, tmp_path):
        p = os.path.join(tmp_path, "rollout-x.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            rows = [
                {"type": "session_meta", "payload": {"cwd": "/tmp"}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "codex 回复"}]}},
                {"type": "event_msg", "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 30, "cached_input_tokens": 10,
                        "output_tokens": 15}}}},
            ]
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        r = TerminalExecDriver._parse_codex_transcript(p)
        assert r["final_text"] == "codex 回复"
        assert r["tokens_in"] == 40
        assert r["tokens_out"] == 15

    def test_claude_parse_missing_file(self, tmp_path):
        assert TerminalExecDriver._parse_claude_transcript(
            os.path.join(tmp_path, "nope.jsonl")) is None


# ============================================================
# T2：execute_coding 分支与降级
# ============================================================

class SpyDriver:
    """替身 driver：记录 drive 调用参数，不起真实协程/不碰 ~/.claude。"""
    calls: list[dict] = []

    def __init__(self, terminal, store, claude_root=None, codex_root=None):
        pass

    async def drive(self, **kw):
        SpyDriver.calls.append(kw)


@pytest.mark.asyncio
class TestExecuteCodingExecMode:

    async def test_terminal_mode_dispatches_native(self, task_env, monkeypatch):
        """conpty_host 后端 + exec_mode=terminal：带 command 创建会话 + terminal 派发。"""
        SpyDriver.calls.clear()
        monkeypatch.setattr("task.terminal_exec.TerminalExecDriver", SpyDriver)
        orch, task, workspace = task_env
        # project 挂接 workspace（execute_coding 读 project.workspace_id）
        conn = orch.store._conn
        with conn:
            conn.execute(
                "UPDATE projects SET workspace_id = ? WHERE project_id = 'proj_te'",
                (workspace,))

        terminal = FakeTerminal(_claude_frames())
        orch._terminal = terminal
        r = await orch.execute_coding("t_te1", exec_mode="terminal")
        assert r["ok"]
        assert r["exec_mode"] == "terminal"
        assert r["mock"] is False
        assert r["run_id"].startswith("run_")
        # 会话创建带了 command（ConPTY 直跑 claude TUI）
        assert terminal.created
        assert terminal.created[0]["command"] == \
            ["cmd.exe", "/q", "/d", "/c", "claude"]
        assert terminal.created[0]["cwd"] == workspace
        # driver 被派发（terminal_id/harness/workspace 正确）
        assert len(SpyDriver.calls) == 1
        assert SpyDriver.calls[0]["terminal_id"] == "task_t_te1"
        assert SpyDriver.calls[0]["harness"] == "claude_code"
        assert SpyDriver.calls[0]["workspace"] == workspace
        # activity 记录 exec_mode=terminal
        acts = await orch.store.list_activities("t_te1")
        dispatch = [a for a in acts if "dispatch" in a["changes"]][-1]
        assert dispatch["changes"]["dispatch"]["after"]["exec_mode"] == "terminal"

    async def test_terminal_mode_degrades_on_mock_backend(self, task_env):
        """exec_mode=terminal 但 terminal 无 backend_name（mock）→ 降级 tee。"""
        orch, task, workspace = task_env

        class LegacyMockTerminal:
            """旧测试 mock：无 backend_name 属性（getattr 默认 mock）。"""

            def __init__(self):
                self.sessions = {}

            async def create_session(self, name, cwd=""):
                self.sessions[name] = []
                return name

            async def send_keys(self, tid, text):
                pass

            async def append_output(self, tid, text):
                self.sessions.setdefault(tid, []).append(text)

        orch._terminal = LegacyMockTerminal()
        r = await orch.execute_coding("t_te1", exec_mode="terminal")
        assert r["ok"]
        assert r["exec_mode"] == "tee"  # 自动降级
        assert r["mock"] is True  # mock 后端 → mock 派发（现有行为）

    async def test_tee_mode_explicit(self, task_env, monkeypatch):
        """exec_mode=tee 显式：即使 conpty_host 后端也走 tee（回归旧行为）。"""
        orch, task, workspace = task_env
        dispatched = []

        async def _spy_dispatch(**kw):
            dispatched.append(kw)
            return {"run_id": "tee_run_x", "session_id": "s_x", "mock": True}

        monkeypatch.setattr(orch, "_dispatch_coding_agent", _spy_dispatch)
        terminal = FakeTerminal([])
        orch._terminal = terminal
        r = await orch.execute_coding("t_te1", exec_mode="tee")
        assert r["ok"]
        assert r["exec_mode"] == "tee"
        # 走了 tee 派发分支（spy 被调用，不起真实 harness 子进程）
        assert len(dispatched) == 1
        # 会话创建不带 command（tee 模式空 shell）
        assert terminal.created[0]["command"] is None

    async def test_default_exec_mode_is_terminal(self, task_env, monkeypatch):
        """默认 exec_mode=terminal（不传参）。"""
        SpyDriver.calls.clear()
        monkeypatch.setattr("task.terminal_exec.TerminalExecDriver", SpyDriver)
        orch, task, workspace = task_env
        terminal = FakeTerminal(_claude_frames())
        orch._terminal = terminal
        r = await orch.execute_coding("t_te1")
        assert r["exec_mode"] == "terminal"
        assert len(SpyDriver.calls) == 1


# ============================================================
# T4：特征库配置化
# ============================================================

class TestFeatureConfig:

    def test_get_features_defaults(self, monkeypatch):
        """无配置（或配置缺失）时回退内置默认。"""
        import task.terminal_exec as te
        monkeypatch.setattr(te, "_FEATURES_CACHE", None)
        monkeypatch.setattr(te, "_FEATURES_PATH",
                            os.path.join("nonexistent", "x.yaml"))
        f = te.get_features()
        assert f["launch_cmd"]["claude_code"] == \
            ["cmd.exe", "/q", "/d", "/c", "claude"]
        assert f["timeouts"]["monitor_timeout_s"] == 3600.0

    def test_get_features_merges_config(self, tmp_path, monkeypatch):
        """config yaml 覆盖默认；缺失字段保持默认。"""
        import task.terminal_exec as te
        cfg = tmp_path / "terminal_features.yaml"
        cfg.write_text(
            "ready_hints:\n"
            "  claude_code:\n"
            "    - '新版本提示语'\n"
            "timeouts:\n"
            "  monitor_timeout_s: 7200\n",
            encoding="utf-8")
        monkeypatch.setattr(te, "_FEATURES_CACHE", None)
        monkeypatch.setattr(te, "_FEATURES_PATH", str(cfg))
        f = te.get_features()
        assert "新版本提示语" in f["ready_hints"]["claude_code"]
        assert f["timeouts"]["monitor_timeout_s"] == 7200
        # 未配置字段用默认
        assert f["timeouts"]["ready_timeout_s"] == 30.0
        assert f["launch_cmd"]["codex"][-1] == "codex"

    def test_get_launch_cmd(self, monkeypatch):
        import task.terminal_exec as te
        monkeypatch.setattr(te, "_FEATURES_CACHE", None)
        monkeypatch.setattr(te, "_FEATURES_PATH",
                            os.path.join("nonexistent", "x.yaml"))
        cmd = te.get_launch_cmd()
        assert cmd["claude_code"] == ["cmd.exe", "/q", "/d", "/c", "claude"]


# ============================================================
# T3：断点收尾（reconcile）
# ============================================================

@pytest.mark.asyncio
class TestReconcileStale:

    async def test_reconcile_stale_task(self, task_env, tmp_path):
        """in_progress 超时 + terminal 任务：transcript 补收尾 + activity 记录。"""
        orch, task, workspace = task_env
        # project 挂 workspace
        conn = orch.store._conn
        with conn:
            conn.execute(
                "UPDATE projects SET workspace_id = ? WHERE project_id = 'proj_te'",
                (workspace,))
        # 造 dispatch activity（terminal 模式，供 harness/run_id 读取）
        await orch.store.add_activity(
            task_id="t_te1", actor_type="agent", actor_name="coding_agent",
            changes={"dispatch": {"after": {
                "harness": "claude_code", "run_id": "run_stale1",
                "exec_mode": "terminal"}}})
        # 任务 updated_at 改成 2h 前（触发 stale）
        with conn:
            conn.execute(
                "UPDATE tasks SET updated_at = datetime('now','-2 hours') "
                "WHERE task_id = 't_te1'")
        # 用 fake orchestrator（复用真 store，_finalize_execution 走 spy）
        fake_orch = FakeOrchestrator(orch.store)
        # monkeypatch _collect_result：_reconcile_one 内部自建 driver 用默认
        # ~/.claude，测试不碰真实 transcript，注入 fixture 结果
        import task.terminal_exec as te
        monkeypatch = pytest.MonkeyPatch()

        async def _spy_collect(self, harness, workspace, started, terminal_id,
                               to):
            return {"final_text": "补偿输出\n## 设计笔记\n- [x] 完成；为什么：原因",
                    "tokens_in": 10, "tokens_out": 5, "source": "transcript"}

        monkeypatch.setattr(te.TerminalExecDriver, "_collect_result",
                            _spy_collect)
        try:
            ok = await te.TerminalExecDriver._reconcile_one(
                fake_orch, task, time.time() - 7200)
        finally:
            monkeypatch.undo()
        assert ok
        assert fake_orch.finalized is not None
        assert "补偿输出" in fake_orch.finalized["final_text"]
        # reconcile activity 落库
        acts = await orch.store.list_activities("t_te1")
        assert any("reconcile" in a["changes"] for a in acts)

    async def test_reconcile_skips_fresh_task(self, task_env):
        """in_progress 但 updated_at 未超时 → 不进补偿名单。"""
        orch, task, workspace = task_env
        conn = orch.store._conn
        with conn:
            conn.execute(
                "UPDATE tasks SET terminal_session_id = 'task_t_te1' "
                "WHERE task_id = 't_te1'")
        # updated_at 默认 now → 未超 1h
        import task.terminal_exec as te
        called = []
        monkeypatch = pytest.MonkeyPatch()

        async def _spy_reconcile_one(orchestrator, t, ts):
            called.append(t["task_id"])
            return True

        monkeypatch.setattr(te.TerminalExecDriver, "_reconcile_one",
                            _spy_reconcile_one)
        try:
            result = await te.TerminalExecDriver.reconcile_stale(
                orch, max_age_s=3600.0, limit=10)
        finally:
            monkeypatch.undo()
        assert "t_te1" not in result
        assert called == []  # fresh 任务不被补偿

    async def test_reconcile_stale_scan(self, task_env, tmp_path):
        """reconcile_stale 扫描只命中 stale 任务，且调用收尾。"""
        orch, task, workspace = task_env
        conn = orch.store._conn
        with conn:
            conn.execute(
                "UPDATE tasks SET terminal_session_id = 'task_t_te1' "
                "WHERE task_id = 't_te1'")
            conn.execute(
                "UPDATE tasks SET updated_at = datetime('now','-3 hours') "
                "WHERE task_id = 't_te1'")
        # spy 掉 _reconcile_one 避免真实 transcript 依赖
        import task.terminal_exec as te
        called = []

        async def _spy_reconcile_one(orchestrator, t, ts):
            called.append(t["task_id"])
            return True

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(te.TerminalExecDriver, "_reconcile_one",
                            _spy_reconcile_one)
        try:
            result = await te.TerminalExecDriver.reconcile_stale(
                orch, max_age_s=3600.0, limit=10)
        finally:
            monkeypatch.undo()
        assert "t_te1" in result
        assert "t_te1" in called


class FakeReconOrch:
    """_reconcile_one 最小依赖（无 terminal，仅 store）。"""

    def __init__(self, store):
        self.store = store
        self._terminal = None
