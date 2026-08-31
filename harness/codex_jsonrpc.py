"""codex app-server JSON-RPC 客户端。

通过 stdin/stdout 与 codex app-server 子进程通信，使用 JSON-RPC 2.0 协议。

协议：
  请求：{"jsonrpc":"2.0","id":N,"method":"...","params":{...}}\n
  响应：{"jsonrpc":"2.0","id":N,"result":{...}}\n
  通知：{"jsonrpc":"2.0","method":"...","params":{...}}\n  （无 id）

核心方法：
  initialize              - JSON-RPC 握手
  thread/start            - 创建新线程
  thread/resume           - 恢复已有线程
  thread/list             - 列出线程（支持搜索）
  thread/name/set         - 设置线程名
  turn/start              - 启动一轮对话
  turn/interrupt          - 中断当前 turn
  thread/realtime/start   - 启动实时语音（WebRTC）
  thread/realtime/stop    - 停止实时语音
  thread/realtime/appendText - 追加文本输入
  thread/unsubscribe      - 取消订阅
  skills/extraRoots/set   - 设置 skill 根目录
  skills/list             - 列出可用 skills

通知方法（codex -> 本客户端）：
  item/tool/call               - 工具调用请求（需要响应）
  thread/realtime/started      - 语音会话已建立
  thread/realtime/sdp          - SDP answer
  thread/realtime/transcript/delta - 实时转录增量
  thread/realtime/transcript/done  - 完整转录
  thread/realtime/turn/started     - turn 开始
  thread/realtime/turn/completed   - turn 完成
  session.error                - 错误
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# 凭证安全：敏感 env key 模式
# 匹配这些模式的 key 在 docker exec 时用 `-e KEY`（不传值），由 docker daemon 从 host env 取值，
# 避免凭证明文出现在进程命令行（ps aux 可见）和子进程错误消息中。
_SECRET_ENV_PATTERNS = ("api_key", "apikey", "token", "secret", "authorization", "password")


def _is_secret_env_key(key: str) -> bool:
    """判断 env key 是否为敏感凭证（匹配 api_key/token/secret 等模式）。"""
    key_lower = key.lower()
    return any(p in key_lower for p in _SECRET_ENV_PATTERNS)


def _is_env_inherited_from_host(key: str, value: str) -> bool:
    """判断 env 值是否与 host env 相同（相同则可由 daemon 继承，不需显式传值）。"""
    return bool(value) and os.environ.get(key) == value


class CodexJsonRpcError(Exception):
    """codex JSON-RPC 错误。"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


class CodexJsonRpcClient:
    """codex app-server JSON-RPC 客户端（stdin/stdout 通信）。

    生命周期：
      1. start() - 启动 codex app-server 子进程
      2. initialize() - JSON-RPC 握手
      3. request(method, params) - 发送请求并等待响应
      4. notifications() - 迭代通知消息
      5. respond_tool_call(id, content) - 响应工具调用
      6. close() - 关闭子进程
    """

    def __init__(
        self,
        codex_bin: str = "codex",
        cwd: str = ".",
        env: dict[str, str] | None = None,
        args: list[str] | None = None,
        container_id: str | None = None,
    ):
        self.codex_bin = codex_bin
        self.cwd = cwd
        self.env = env or {}
        self.args = args or ["app-server"]
        # 方案A：container_id 非空时，通过 `docker exec -i <container_id> codex app-server`
        # 在容器内启动 codex（容器只需有 codex 二进制，不跑 agentops 服务）
        self.container_id = container_id
        self.process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._notification_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closing = False

    async def start(self) -> None:
        """启动 codex app-server 子进程。

        方案A：当 container_id 非空时，通过 `docker exec -i <container_id> codex app-server`
        在容器内启动 codex。容器只需有 codex 二进制（agentops-worker 镜像内置），
        不跑 agentops 服务、不需要 WS bridge。stdin/stdout/stderr 通过 docker exec 管道传输。
        """
        if self.process:
            return

        full_env = {**os.environ, **self.env}

        if self.container_id:
            # 方案A：docker exec 模式 — 在容器内执行 codex
            # -i 保持 stdin 打开（JSON-RPC 双向通信需要）
            # 凭证安全：
            #   敏感 key（API_KEY/TOKEN/SECRET 等）且值与 host env 相同时，
            #   用 `-e KEY`（不传值），由 docker daemon 从自身 env 取值，
            #   避免凭证明文出现在进程命令行（ps aux 可见）和子进程错误消息中。
            #   非敏感 key 或动态凭证（值与 host env 不同）仍用 `-e KEY=value`。
            exec_args = ["docker", "exec", "-i"]
            env_log_keys: list[str] = []
            for key, value in self.env.items():
                if not value:
                    continue
                if _is_secret_env_key(key) and _is_env_inherited_from_host(key, value):
                    # 敏感凭证且 host env 已有相同值 → daemon 继承，不传值
                    exec_args.extend(["-e", key])
                    env_log_keys.append(f"{key}(inherited)")
                else:
                    # 非敏感 或 动态凭证（值与 host env 不同）→ 显式传值
                    exec_args.extend(["-e", f"{key}={value}"])
                    env_log_keys.append(key)
            exec_args.extend([
                self.container_id,
                self.codex_bin,  # 容器内 codex（PATH 中查找）
                *self.args,
            ])
            logger.info(
                "codex app-server 启动（docker exec）container=%s codex=%s args=%s env_keys=%s",
                self.container_id, self.codex_bin, self.args, env_log_keys,
            )
            self.process = await asyncio.create_subprocess_exec(
                *exec_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
            )
        else:
            # 旧路径：host 本地 spawn codex
            bin_path = self.codex_bin
            if not os.path.isabs(bin_path):
                import shutil as _shutil
                found = _shutil.which(bin_path)
                if found:
                    bin_path = found
                else:
                    for suffix in ["", ".cmd", ".exe", ".bat"]:
                        candidate = bin_path + suffix
                        if os.path.exists(candidate):
                            bin_path = candidate
                            break

            self.process = await asyncio.create_subprocess_exec(
                bin_path,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=full_env,
            )
            logger.info("codex app-server 启动（本地）pid=%s bin=%s", self.process.pid, bin_path)

        # 启动 stdout 读取任务
        self._read_task = asyncio.create_task(self._read_stdout())
        # 启动 stderr 排空任务（防止管道阻塞）
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # 监听进程退出
        asyncio.create_task(self._watch_exit())

    async def _watch_exit(self) -> None:
        """监听子进程意外退出。"""
        if not self.process:
            return
        await self.process.wait()
        if not self._closing:
            logger.error("codex app-server 意外退出 code=%s", self.process.returncode)
            # 推入 sentinel 让 notifications() 退出
            await self._notification_queue.put(None)
            # 取消所有 pending request
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(
                        RuntimeError(f"codex app-server exited (code={self.process.returncode})")
                    )
            self._pending.clear()

    async def _read_stdout(self) -> None:
        """逐行读取 stdout，分发到 pending request 或 notification queue。"""
        if not self.process or not self.process.stdout:
            return
        while True:
            try:
                line = await self.process.stdout.readline()
            except Exception:
                break
            if not line:
                break
            try:
                msg = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                logger.debug("codex stdout 非 JSON: %s", line[:200])
                continue

            # 有 id -> 是 response
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if "error" in msg and msg["error"]:
                    err = msg["error"]
                    fut.set_exception(CodexJsonRpcError(
                        code=err.get("code", -1),
                        message=err.get("message", "Unknown error"),
                        data=err.get("data"),
                    ))
                else:
                    fut.set_result(msg.get("result", {}))
            else:
                # notification（无 id 或 id 不在 pending 中）
                await self._notification_queue.put(msg)

    async def _drain_stderr(self) -> None:
        """排空 stderr（防止管道阻塞），记录诊断日志。"""
        if not self.process or not self.process.stderr:
            return
        while True:
            try:
                chunk = await self.process.stderr.read(4096)
            except Exception:
                break
            if not chunk:
                break
            # stderr 只记 debug，不转发（可能含敏感信息）
            logger.debug("codex stderr: %s", chunk.decode(errors="replace")[:200])

    async def initialize(self, timeout: float = 30.0) -> dict:
        """JSON-RPC 握手（需要 experimentalApi 支持 dynamicTools）。"""
        return await self.request("initialize", {
            "clientInfo": {
                "name": "agentops",
                "title": "AgentOps",
                "version": "0.1.0",
            },
            "capabilities": {
                "experimentalApi": True,
            },
        }, timeout=timeout)

    async def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """发送 JSON-RPC 请求并等待响应。"""
        if not self.process or not self.process.stdin:
            raise RuntimeError("codex app-server 未启动")

        self._request_id += 1
        req_id = self._request_id
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            msg["params"] = params

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        line = json.dumps(msg) + "\n"
        self.process.stdin.write(line.encode())
        # drain 超时保护：docker exec 子进程卡死不读 stdin 时避免永久挂起
        await asyncio.wait_for(self.process.stdin.drain(), timeout=10.0)

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"codex JSON-RPC 请求超时 ({timeout}s): {method}")

    async def notifications(self) -> AsyncIterator[dict]:
        """迭代 notification 消息（无 id 的 JSON-RPC 消息）。"""
        while True:
            msg = await self._notification_queue.get()
            if msg is None:  # sentinel
                break
            yield msg

    async def respond_tool_call(self, request_id: int, content: str, success: bool = True) -> None:
        """响应 item/tool/call 请求。"""
        if not self.process or not self.process.stdin:
            return
        msg = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "contentItems": [{"type": "inputText", "text": content}],
                "success": success,
            },
        }
        self.process.stdin.write((json.dumps(msg) + "\n").encode())
        # drain 超时保护：tool call 响应写入不得永久阻塞
        await asyncio.wait_for(self.process.stdin.drain(), timeout=10.0)

    async def close(self) -> None:
        """关闭子进程。"""
        self._closing = True
        await self._notification_queue.put(None)  # sentinel

        if self._read_task:
            self._read_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            self.process = None

        # 取消所有 pending
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("codex app-server closed"))
        self._pending.clear()
