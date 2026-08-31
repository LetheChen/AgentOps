"""task/terminal_exec.py — coding agent 原生终端执行驱动器（T1）。

设计文档：docs/product-design/task-manage/DESIGN_terminal_native_execution.md
真 PTY 直跑 claude/codex 原生 TUI 的后台驱动协程：
- ready/trust 检测（轮询 capture_pane 特征匹配，trust 弹窗自动 Enter）
- 长 prompt 落 {workspace}/.agentops/task_{task_id}_prompt.md，终端只注单行短指令
- 完成监测（pane 内容连续稳定 + 无工作特征，双条件防误判）
- 结果回收走 transcript jsonl（claude 精确 / codex 尽力），屏幕解析兜底
- 收尾喂 orchestrator._finalize_execution（笔记提取/报告/转 validating，零改动复用）

依赖方向：本模块只依赖 terminal 抽象层协议（create_session/capture_pane/send_keys/
append_output）与 TaskStore 协议，orchestrator 经参数注入（防循环 import）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================
# TUI 启动命令与特征库（集中可配置；CLI 版本文案变化只改这里）
# ============================================================

#: conpty_host 后端：command 直跑 TUI（terminal_host.py 注释原例）
LAUNCH_CMD: dict[str, list[str]] = {
    "claude_code": ["cmd.exe", "/q", "/d", "/c", "claude"],
    "codex": ["cmd.exe", "/q", "/d", "/c", "codex"],
}

#: psmux/tmux 后端：shell 里 send_keys 启动的 CLI 名
LAUNCH_CLI: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
}

#: TUI 首帧特征（pane 包含任一即 ready）
READY_HINTS: dict[str, list[str]] = {
    "claude_code": ["? for shortcuts", "? shortcut"],
    "codex": ["Ask Codex", "TRUST", "codex"],
}

#: trust 弹窗特征（出现即 send Enter 确认，否则卡死在目录信任确认）
TRUST_HINTS: dict[str, list[str]] = {
    "claude_code": ["trust the files in this folder", "Do you trust"],
    "codex": ["trust this folder", "Trust this directory"],
}

#: TUI 工作中特征（pane 包含任一说明 agent 仍在干活，不判完成）
WORKING_HINTS: dict[str, list[str]] = {
    "claude_code": ["esc to interrupt", "✳", "esc interrupt"],
    "codex": ["esc to interrupt", "Thinking", "Working"],
}

#: ready 检测超时（秒）：超时仍注入 prompt（CLI 可能已 ready 但特征不匹配）
READY_TIMEOUT_S = 30.0
#: 完成监测超时（秒）：对齐 harness timeout=3600
MONITOR_TIMEOUT_S = 3600.0
#: 监测轮询间隔（秒）
MONITOR_INTERVAL_S = 2.0
#: 内容稳定判定：连续 N 次轮询 pane 无变化（N × 间隔 = 稳定窗口）
MONITOR_STABLE_N = 4
#: transcript flush 等待（秒）：完成后等 CLI 把尾部消息写盘
FLUSH_WAIT_S = 2.0
#: transcript 定位：mtime 下界前移量（秒），容忍文件系统时间精度
TRANSCRIPT_SINCE_SLACK_S = 5.0

#: config/terminal_features.yaml 路径（T4 特征库配置化，懒加载）
_FEATURES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "terminal_features.yaml")
_FEATURES_CACHE: dict | None = None


def get_features() -> dict:
    """T4 特征库：config/terminal_features.yaml + 内置默认懒加载合并。

    文件缺失/解析失败/字段缺失 → 全部回退内置默认；测试可 monkeypatch
    `task.terminal_exec._FEATURES_CACHE` 注入自定义特征。
    """
    global _FEATURES_CACHE
    if _FEATURES_CACHE is not None:
        return _FEATURES_CACHE

    defaults: dict = {
        "launch_cmd": LAUNCH_CMD,
        "launch_cli": LAUNCH_CLI,
        "ready_hints": READY_HINTS,
        "trust_hints": TRUST_HINTS,
        "working_hints": WORKING_HINTS,
        "timeouts": {
            "ready_timeout_s": READY_TIMEOUT_S,
            "monitor_timeout_s": MONITOR_TIMEOUT_S,
            "monitor_interval_s": MONITOR_INTERVAL_S,
            "monitor_stable_n": MONITOR_STABLE_N,
            "flush_wait_s": FLUSH_WAIT_S,
            "transcript_since_slack_s": TRANSCRIPT_SINCE_SLACK_S,
        },
    }
    try:
        import yaml
        with open(_FEATURES_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for section in ("launch_cmd", "launch_cli", "ready_hints",
                        "trust_hints", "working_hints"):
            if section in data and isinstance(data[section], dict):
                defaults[section].update(data[section])
        if isinstance(data.get("timeouts"), dict):
            defaults["timeouts"].update(data["timeouts"])
    except Exception as e:  # noqa: BLE001 — 配置缺失/损坏回退内置默认
        logger.debug("terminal_features.yaml 加载失败，使用内置默认: %s", e)
    _FEATURES_CACHE = defaults
    return defaults


def get_launch_cmd() -> dict[str, list[str]]:
    """conpty_host 后端 TUI 启动命令（orchestrator 派发用）。"""
    return get_features()["launch_cmd"]


def _hint_in_pane(pane: str, hints: list[str]) -> bool:
    """pane 内容包含任一特征（大小写不敏感）。"""
    low = pane.lower()
    return any(h.lower() in low for h in hints)


# ============================================================
# TerminalExecDriver
# ============================================================

class TerminalExecDriver:
    """单个 coding agent 原生终端执行的驱动器（一次 drive 对应一个任务执行）。

    Args:
        terminal: TerminalSessionManager（或测试 fake，需 backend_name 属性）
        store: TaskStore（activity 落库）
        claude_root: claude transcript 根目录（默认 ~/.claude，测试可注入）
        codex_root: codex transcript 根目录（默认 ~/.codex，测试可注入）
    """

    def __init__(self, terminal: Any, store: Any,
                 claude_root: str | None = None,
                 codex_root: str | None = None):
        self._terminal = terminal
        self._store = store
        self._claude_root = claude_root or os.path.expanduser("~/.claude")
        self._codex_root = codex_root or os.path.expanduser("~/.codex")

    # ---------- 主流程 ----------

    async def drive(self, *, orchestrator: Any, terminal_id: str, task: dict,
                    harness: str, workspace: str, system_prompt: str,
                    run_id: str, started_at: float | None = None) -> None:
        """后台驱动全流程：启动 → ready → 注入 → 监测 → 收结果 → 闭环。

        orchestrator 经参数注入（duck-typing：只需 _finalize_execution 方法），
        避免 task.terminal_exec ↔ task.orchestrator 循环 import。
        """
        started = started_at if started_at is not None else time.time()
        task_id = task["task_id"]
        feat = get_features()
        to = feat["timeouts"]

        async def out(text: str) -> None:
            try:
                await self._terminal.append_output(terminal_id, f"{text}\n")
            except Exception:  # noqa: BLE001 — tee 失败不阻塞驱动
                pass

        try:
            await out(f"[terminal-exec] run={run_id} harness={harness} "
                      f"workspace={workspace}")

            # 0. psmux/tmux：shell 已起，send_keys 启动 CLI TUI
            backend = getattr(self._terminal, "backend_name", "")
            if backend in ("psmux", "tmux"):
                cli = feat["launch_cli"][harness]
                await self._terminal.send_keys(terminal_id, cli)
                await out(f"[terminal-exec] shell 内启动 {cli} TUI")

            # 1. ready 检测（含 trust 自动确认）
            ready = await self._wait_ready(terminal_id, harness, out, feat)
            if not ready:
                await out("[terminal-exec] ready 检测超时，仍尝试注入任务指令")

            # 2. prompt 落文件 + 注入短指令
            prompt_rel = await self._inject_prompt(
                terminal_id, task, workspace, system_prompt)
            await out(f"[terminal-exec] 任务指令已注入（上下文见 {prompt_rel}）")

            # 3. 完成监测
            done = await self._monitor(terminal_id, harness, out, to)
            if not done:
                await self._activity(task_id, "terminal_exec_timeout", {
                    "run_id": run_id, "harness": harness,
                    "timeout_s": to["monitor_timeout_s"]})
                await out(f"[terminal-exec] 监测超时（{to['monitor_timeout_s']:.0f}s），"
                          f"任务保留 in_progress，可人工介入窗格续跑")
                return

            # 4. transcript 提取（屏幕解析兜底）
            result = await self._collect_result(
                harness, workspace, started, terminal_id, to)
            await out(f"[terminal-exec] 完成 · tokens_in={result['tokens_in']} "
                      f"tokens_out={result['tokens_out']} "
                      f"source={result.get('source', 'transcript')}")

            # 5. run_complete activity（对齐 tee 模式字段）
            await self._activity(task_id, "run_complete", {
                "run_id": run_id, "harness": harness,
                "tokens_in": result["tokens_in"],
                "tokens_out": result["tokens_out"],
                "summary": (result["final_text"] or "")[:500],
                "exec_mode": "terminal",
                "result_source": result.get("source", "transcript")})

            # 6. 执行闭环（笔记提取/报告/评论区/转 validating，零改动复用）
            await orchestrator._finalize_execution(
                task=task, harness=harness, run_id=run_id,
                final_text=result["final_text"],
                tokens_in=result["tokens_in"],
                tokens_out=result["tokens_out"], out=out)
        except Exception as e:  # noqa: BLE001 — 驱动协程兜底
            logger.warning("terminal-exec drive 异常: %s", e)
            await out(f"[terminal-exec][error] 驱动协程异常: {e}")
            await self._activity(task_id, "terminal_exec_error", {
                "run_id": run_id, "error": str(e)})

    # ---------- ready / trust 检测 ----------

    async def _wait_ready(self, terminal_id: str, harness: str,
                          out, feat: dict) -> bool:
        """轮询 pane 直到 TUI 首帧特征出现；trust 弹窗先 Enter 确认。"""
        to = feat["timeouts"]
        trust_hints = feat["trust_hints"].get(harness, [])
        ready_hints = feat["ready_hints"].get(harness, [])
        deadline = time.monotonic() + to["ready_timeout_s"]
        while time.monotonic() < deadline:
            pane = await self._safe_capture(terminal_id)
            if _hint_in_pane(pane, trust_hints):
                await out("[terminal-exec] 检测到 trust 弹窗，自动确认…")
                await self._terminal.send_keys(terminal_id, "")
            elif _hint_in_pane(pane, ready_hints):
                return True
            await asyncio.sleep(1.0)
        return False

    # ---------- prompt 注入 ----------

    async def _inject_prompt(self, terminal_id: str, task: dict,
                             workspace: str, system_prompt: str) -> str:
        """完整上下文落文件，终端只注单行短指令（规避 TUI 长文本转义问题）。"""
        prompt_dir = os.path.join(workspace, ".agentops")
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_path = os.path.join(
            prompt_dir, f"task_{task['task_id']}_prompt.md")
        Path(prompt_path).write_text(system_prompt, encoding="utf-8")
        rel = os.path.relpath(prompt_path, workspace).replace("\\", "/")
        await self._terminal.send_keys(
            terminal_id,
            f"请阅读 {rel} 文件并严格按其中要求执行任务，完成后在输出中包含「## 设计笔记」段落")
        return rel

    # ---------- 完成监测 ----------

    async def _monitor(self, terminal_id: str, harness: str, out,
                       to: dict) -> bool:
        """双条件空闲判定：内容连续稳定 + 无工作特征（spinner 等）。"""
        stable, last_pane = 0, ""
        working_hints = get_features()["working_hints"].get(harness, [])
        deadline = time.monotonic() + to["monitor_timeout_s"]
        while time.monotonic() < deadline:
            await asyncio.sleep(to["monitor_interval_s"])
            pane = await self._safe_capture(terminal_id)
            working = _hint_in_pane(pane, working_hints)
            stable = stable + 1 if (pane == last_pane and not working) else 0
            last_pane = pane
            if stable >= to["monitor_stable_n"]:
                return True
        return False

    # ---------- 结果回收 ----------

    async def _collect_result(self, harness: str, workspace: str,
                              started_at: float,
                              terminal_id: str, to: dict) -> dict:
        """transcript jsonl 提取（首选）→ 屏幕解析（兜底）。"""
        await asyncio.sleep(to["flush_wait_s"])
        result = (self._collect_claude(workspace, started_at, to)
                  if harness == "claude_code"
                  else self._collect_codex(workspace, started_at, to))
        if result is not None:
            result.setdefault("source", "transcript")
            return result
        # 兜底：pane 尾部非空文本
        pane = await self._safe_capture(terminal_id)
        lines = [ln.rstrip() for ln in pane.splitlines() if ln.strip()]
        return {"final_text": "\n".join(lines[-50:]),
                "tokens_in": 0, "tokens_out": 0, "source": "pane"}

    def _collect_claude(self, workspace: str, started_at: float,
                        to: dict | None = None) -> dict | None:
        """claude transcript：~/.claude/projects/<munged-cwd>/*.jsonl。

        定位 = munged 目录优先 + mtime 窗口（started_at - slack 起）最新文件。
        """
        to = to or get_features()["timeouts"]
        root = os.path.join(self._claude_root, "projects")
        if not os.path.isdir(root):
            return None
        munged = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(workspace))
        since = started_at - to["transcript_since_slack_s"]
        candidates: list[str] = []
        proj_dir = os.path.join(root, munged)
        scan_dirs = [proj_dir] if os.path.isdir(proj_dir) else [
            os.path.join(root, d) for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))]
        for d in scan_dirs:
            try:
                for f in os.listdir(d):
                    if f.endswith(".jsonl") and \
                            os.path.getmtime(os.path.join(d, f)) >= since:
                        candidates.append(os.path.join(d, f))
            except OSError:
                continue
        if not candidates:
            return None
        latest = max(candidates, key=os.path.getmtime)
        return self._parse_claude_transcript(latest)

    @staticmethod
    def _parse_claude_transcript(path: str) -> dict | None:
        """解析 claude jsonl：最后一条 assistant 的 text blocks + usage。

        - final_text：逐行扫描，取最后 type==assistant 行的 text block 拼接
        - tokens_in：max(input + cache_read + cache_creation)（末轮近似总量）
        - tokens_out：sum(output)（各轮输出之和）
        """
        final_text, texts = "", []
        tokens_in_max, tokens_out_sum = 0, 0
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") != "assistant":
                        continue
                    msg = row.get("message") or {}
                    blocks = [b.get("text", "") for b in
                              (msg.get("content") or [])
                              if isinstance(b, dict) and b.get("type") == "text"]
                    if blocks:
                        texts = blocks  # 只保留最后一条 assistant 的 text
                    usage = msg.get("usage") or {}
                    ti = (usage.get("input_tokens", 0)
                          + usage.get("cache_read_input_tokens", 0)
                          + usage.get("cache_creation_input_tokens", 0))
                    tokens_in_max = max(tokens_in_max, ti)
                    tokens_out_sum += usage.get("output_tokens", 0)
        except OSError:
            return None
        final_text = "\n".join(t for t in texts if t)
        return {"final_text": final_text,
                "tokens_in": tokens_in_max, "tokens_out": tokens_out_sum}

    def _collect_codex(self, workspace: str, started_at: float,
                       to: dict | None = None) -> dict | None:
        """codex transcript：~/.codex/sessions/**/rollout-*.jsonl（尽力解析）。"""
        to = to or get_features()["timeouts"]
        root = os.path.join(self._codex_root, "sessions")
        if not os.path.isdir(root):
            return None
        since = started_at - to["transcript_since_slack_s"]
        candidates = [
            os.path.join(dp, f)
            for dp, _, fs in os.walk(root)
            for f in fs
            if f.startswith("rollout-") and f.endswith(".jsonl")
            and os.path.getmtime(os.path.join(dp, f)) >= since]
        if not candidates:
            return None
        latest = max(candidates, key=os.path.getmtime)
        return self._parse_codex_transcript(latest)

    @staticmethod
    def _parse_codex_transcript(path: str) -> dict | None:
        """解析 codex rollout jsonl（尽力）：
        - response_item payload type==message role==assistant 的 output_text
        - token_count 事件 payload.info.total_token_usage
        """
        texts: list[str] = []
        tokens_in_max, tokens_out_sum = 0, 0
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = row.get("payload") or {}
                    ptype = payload.get("type", "")
                    if ptype == "message" and payload.get("role") == "assistant":
                        parts = [c.get("text", "") for c in
                                 (payload.get("content") or [])
                                 if isinstance(c, dict)]
                        if parts:
                            texts = parts
                    elif ptype == "token_count":
                        info = payload.get("info") or {}
                        total = info.get("total_token_usage") or {}
                        tokens_in_max = max(
                            tokens_in_max,
                            total.get("input_tokens", 0)
                            + total.get("cached_input_tokens", 0))
                        tokens_out_sum = max(
                            tokens_out_sum, total.get("output_tokens", 0))
        except OSError:
            return None
        return {"final_text": "\n".join(t for t in texts if t),
                "tokens_in": tokens_in_max, "tokens_out": tokens_out_sum}

    # ---------- 辅助 ----------

    async def _safe_capture(self, terminal_id: str) -> str:
        try:
            return await self._terminal.capture_pane(terminal_id)
        except Exception:  # noqa: BLE001 — 采集失败按空屏处理，监测不断流
            return ""

    async def _activity(self, task_id: str, kind: str, payload: dict) -> None:
        try:
            await self._store.add_activity(
                task_id=task_id, actor_type="agent",
                actor_name="terminal_exec",
                changes={kind: {"after": payload}})
        except Exception as e:  # noqa: BLE001 — activity 失败不阻塞主流程
            logger.debug("terminal-exec activity 写入失败: %s", e)

    # ---------- T3：断点收尾（后端重启补偿） ----------

    @classmethod
    async def reconcile_stale(cls, orchestrator: Any,
                              max_age_s: float = 3600.0,
                              limit: int = 10) -> list[str]:
        """后端重启后的补偿收尾：扫描「in_progress + 有 terminal_session_id +
        updated_at 超过 max_age_s」的任务，从 transcript 补 _finalize_execution。

        触发时机：api/server.py lifespan 启动异步调一次。幂等：已完成收尾的
        任务状态已转 validating（不再 in_progress），不会被再次选中。

        Returns:
            已完成补偿收尾的 task_id 列表。
        """
        store = orchestrator.store
        tasks = await store.list_tasks(limit=500)
        now = time.time()
        reconciled: list[str] = []
        for t in tasks:
            if t.get("status") != "in_progress" \
                    or not t.get("terminal_session_id"):
                continue
            upd = t.get("updated_at")
            if not upd:
                continue
            ts = cls._parse_ts(upd)
            if ts is None or now - ts < max_age_s:
                continue
            if len(reconciled) >= limit:
                break
            try:
                ok = await cls._reconcile_one(orchestrator, t, ts)
                if ok:
                    reconciled.append(t["task_id"])
            except Exception as e:  # noqa: BLE001 — 单个任务失败不阻塞整体
                logger.warning("reconcile %s 失败: %s", t["task_id"], e)
        return reconciled

    @classmethod
    async def _reconcile_one(cls, orchestrator: Any, task: dict,
                             started_ts: float) -> bool:
        """单个任务补偿收尾：transcript 提取 → 喂 _finalize_execution。"""
        store = orchestrator.store
        task_id = task["task_id"]
        # harness / run_id 从 dispatch activity 读（terminal 模式派发时记录）
        harness, run_id = "claude_code", f"reconcile_{task_id[:8]}"
        try:
            acts = await store.list_activities(task_id)
            for a in acts:
                d = (a.get("changes") or {}).get("dispatch", {}).get("after", {})
                if d.get("exec_mode") == "terminal":
                    harness = d.get("harness", harness)
                    run_id = d.get("run_id") or run_id
                    break
        except Exception as e:  # noqa: BLE001 — activity 读取失败不阻塞
            logger.debug("reconcile 读 dispatch activity 失败: %s", e)
        # workspace：project.workspace_id，无效回退服务端 cwd
        workspace = ""
        try:
            proj = await store.get_project(task["project_id"])
            workspace = (proj or {}).get("workspace_id") or ""
        except Exception:  # noqa: BLE001
            workspace = ""
        if not workspace or not os.path.isdir(workspace):
            workspace = os.getcwd()
        # transcript 提取（terminal 可能已重建，仅用 pane 兜底；transcript 独立于后端）
        driver = cls(orchestrator._terminal, store)
        to = get_features()["timeouts"]
        result = await driver._collect_result(
            harness, workspace, started_ts, task["terminal_session_id"], to)
        if not (result.get("final_text") or "").strip():
            logger.info("reconcile %s 无结果可收尾（transcript 缺失且 pane 空）",
                        task_id)
            return False
        # 补收尾（笔记提取/报告/评论区/转 validating）
        await orchestrator._finalize_execution(
            task=task, harness=harness, run_id=run_id,
            final_text=result["final_text"],
            tokens_in=result["tokens_in"],
            tokens_out=result["tokens_out"], out=None)
        await store.add_activity(
            task_id=task_id, actor_type="agent", actor_name="terminal_exec",
            changes={"reconcile": {"after": {
                "run_id": run_id, "harness": harness,
                "tokens_in": result["tokens_in"],
                "tokens_out": result["tokens_out"],
                "result_source": result.get("source", "transcript"),
                "note": "后端重启补偿收尾"}}})
        return True

    @staticmethod
    def _parse_ts(iso: str) -> float | None:
        """解析 ISO 时间戳为 epoch 秒（兼容末尾 Z）。"""
        try:
            return datetime.fromisoformat(
                iso.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None
