"""terminal_host.py — 独立常驻 ConPTY 终端宿主进程（Windows）。

为什么独立进程：PTY 会话必须比 AgentOps 后端活得久——后端重启/升级时，
coding agent 的 claude/codex TUI 会话与手动终端不能死。本进程持有全部
winpty ConPTY 会话，AgentOps 后端经 localhost HTTP 访问；后端重启后
自动重连（会话列表与屏幕内容均在内存中保留）。

依赖：仅 stdlib + pywinpty（ConPTY）+ pyte（VT 终端模拟）。

端点（均 127.0.0.1:1988）：
    GET    /health
    GET    /sessions                      -> [{"name", "alive", "pid"}]
    POST   /sessions {name, cwd, command} -> 创建（command 为空 = 交互 shell；
                                              ["cmd.exe","/q","/d","/c","claude"] = TUI）
    GET    /sessions/{name}/screen        -> {"content": scrollback + 当前屏}
    POST   /sessions/{name}/input {data}  -> 写 PTY stdin（TUI 键盘输入）
    POST   /sessions/{name}/append {text} -> tee 文本进 scrollback（agent 输出）
    DELETE /sessions/{name}

启动：`python -u task/terminal_host.py`（由 terminal_session.ConPtyHostBackend
按需拉起，DETACHED 进程独立存活；也可手动常驻）。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pyte
import winpty

HOST = "127.0.0.1"
PORT = int(os.environ.get("TERMINAL_HOST_PORT", "1988"))
COLS, ROWS = 120, 34
HISTORY_LINES = 400


def _line_text(line) -> str:
    """pyte history 行对象转纯文本（不同版本元素类型不同，防御处理）。"""
    if isinstance(line, (list, tuple)):  # pyte 0.8.x：行 = list[Char]
        return "".join(getattr(c, "data", "") for c in line)
    data = getattr(line, "data", None)
    if isinstance(data, str):
        return data
    try:
        return str(line)
    except Exception:  # noqa: BLE001
        return ""


class PtySession:
    """单个 ConPTY 会话：winpty 进程 + pyte 终端模拟 + 后台读线程。"""

    def __init__(self, name: str, cwd: str = "", command: list | None = None):
        self.name = name
        argv = command or ["cmd.exe", "/q", "/d"]
        self.pty = winpty.PtyProcess.spawn(argv, cwd=cwd or None,
                                           dimensions=(ROWS, COLS))
        # winpty read() 返回已解码 str → 用 pyte.Stream（勿用 ByteStream）
        self.screen = pyte.HistoryScreen(COLS, ROWS, history=HISTORY_LINES)
        self.stream = pyte.Stream()
        self.stream.attach(self.screen)
        self.lock = threading.Lock()
        self.alive = True
        # ConPTY 启动即发 DA1 查询（\x1b[c），不响应则不画初始屏。
        # 回 VT100 响应激活渲染（cmd 横幅/提示符/TUI 首帧）。
        try:
            self.pty.write("\x1b[?1;2c")
        except (OSError, ValueError):
            self.alive = False
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def _read_loop(self) -> None:
        while self.alive:
            try:
                data = self.pty.read(4096)  # str（winpty 已解码）
            except (EOFError, OSError, ValueError):
                self.alive = False
                break
            if not data:
                continue
            with self.lock:
                try:
                    self.stream.feed(data)
                except Exception:  # noqa: BLE001 — pyte 解析异常不杀线程
                    pass

    def screen_text(self) -> str:
        """scrollback + 当前屏（对齐 tmux capture-pane 语义）。"""
        with self.lock:
            hist = [_line_text(l) for l in self.screen.history.top]
            cur = list(self.screen.display)
        return "\n".join(hist + cur).rstrip("\n")

    def write(self, data: str) -> None:
        try:
            self.pty.write(data)
        except (OSError, ValueError):
            self.alive = False

    def append_tee(self, text: str) -> None:
        """agent 输出 tee：作为已渲染行进入 scrollback（不经过 shell）。"""
        with self.lock:
            for line in text.splitlines() or [text]:
                self.screen.history.top.append(line)

    def destroy(self) -> None:
        self.alive = False
        try:
            self.pty.terminate()
        except Exception:  # noqa: BLE001
            pass


class _Sessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[str, PtySession] = {}

    def create(self, name: str, cwd: str = "", command: list | None = None) -> dict:
        with self._lock:
            if name in self._map:  # 幂等：后端重启后重连既有会话
                return {"ok": True, "reused": True}
            self._map[name] = PtySession(name, cwd=cwd, command=command)
            return {"ok": True, "reused": False}

    def get(self, name: str) -> PtySession | None:
        with self._lock:
            return self._map.get(name)

    def names(self) -> list[dict]:
        with self._lock:
            return [{"name": n, "alive": s.alive} for n, s in self._map.items()]

    def destroy(self, name: str) -> None:
        with self._lock:
            s = self._map.pop(name, None)
        if s:
            s.destroy()


SESSIONS = _Sessions()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True})
        elif self.path == "/sessions":
            self._json({"sessions": SESSIONS.names()})
        elif self.path.startswith("/sessions/") and self.path.endswith("/screen"):
            name = self.path[len("/sessions/"):-len("/screen")]
            s = SESSIONS.get(name)
            self._json({"content": s.screen_text()} if s
                       else {"content": "", "error": "not_found"})
        else:
            self._json({"error": "bad_path"}, 404)

    def do_POST(self):
        if self.path == "/sessions":
            b = self._body()
            name = str(b.get("name") or "").strip()
            if not name:
                return self._json({"error": "name required"}, 400)
            try:
                r = SESSIONS.create(name, cwd=str(b.get("cwd") or ""),
                                    command=b.get("command") or None)
                self._json(r)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)
        elif "/input" in self.path:
            name = self.path[len("/sessions/"):].split("/")[0]
            s = SESSIONS.get(name)
            if not s:
                return self._json({"error": "not_found"}, 404)
            s.write(str(self._body().get("data") or ""))
            self._json({"ok": True})
        elif "/append" in self.path:
            name = self.path[len("/sessions/"):].split("/")[0]
            s = SESSIONS.get(name)
            if not s:
                return self._json({"error": "not_found"}, 404)
            s.append_tee(str(self._body().get("text") or ""))
            self._json({"ok": True})
        else:
            self._json({"error": "bad_path"}, 404)

    def do_DELETE(self):
        name = self.path[len("/sessions/"):]
        if not name:
            return self._json({"error": "name required"}, 400)
        SESSIONS.destroy(name)
        self._json({"ok": True})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[terminal_host] listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
