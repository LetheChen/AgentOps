"""Codex App-Server Harness - 通过 codex app-server 子进程管理 Thread 生命周期。

核心流程：
  1. 获取 Thread Lease（同一 session 独占）
  2. spawn codex app-server 子进程（JSON-RPC over stdio）
  3. Thread 管理：内存缓存 -> thread/list -> thread/start（三级降级）
  4. turn/start + notification 循环
  5. 流式输出：item/agentMessage/delta -> TEXT
  6. 工具调用：item/tool/call -> handler 执行 -> respond_tool_call
  7. 清理：thread/unsubscribe + 关闭子进程 + 释放 lease

"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import uuid4

from .codex_jsonrpc import CodexJsonRpcClient, CodexJsonRpcError
from .protocol import (
    AgentClient,
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    AgentUsage,
    HarnessType,
    ToolDefinition,
    assert_protocol_compatible,
)
from .thread_lease import acquire_thread_lease, ThreadLease

logger = logging.getLogger(__name__)

# 内存缓存：session_id -> {thread_id, tool_digest}
_native_sessions: dict[str, dict[str, str]] = {}

# credential redaction 关键词
_SECRET_KEYS = frozenset({
    "apiKey", "api_key", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "Authorization", "auth_token", "secret", "token",
})


# ====== 数据结构 ======

@dataclass
class _AgentMessageState:
    """跟踪单个 agentMessage item 的流式状态。"""
    phase: str | None = None       # "commentary" | "final_answer" | None
    deltas: list[str] = field(default_factory=list)
    deltas_yielded: bool = False   # delta 是否已流式 emit


# ====== 模块级工具函数 ======

def _compute_tool_digest(tools: list[ToolDefinition]) -> str:
    """计算 tools schema 的哈希。tools 变了 -> 新 thread。"""
    digest_input = json.dumps([
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ], sort_keys=True)
    return hashlib.sha256(digest_input.encode()).hexdigest()


def _thread_name(session_id: str, tool_digest: str) -> str:
    """生成 codex thread 名称：agentops-{sha256(sessionId)[:16]}-{toolDigest[:16]}"""
    session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return f"agentops-{session_hash}-{tool_digest[:16]}"


def _resolve_codex_bin() -> str:
    """解析 codex 二进制路径（返回绝对路径，兼容 Windows asyncio 子进程）。"""
    for env_var in ("CODEX_BIN_PATH", "AGENTOPS_CODEX_BIN"):
        path = os.environ.get(env_var, "")
        if path and os.path.exists(path):
            return path
    found = shutil.which("codex")
    if found:
        return found
    for candidate in [
        "/d/Program Files/nodejs/node_global/codex",
        "/d/Program Files/nodejs/node_global/codex.cmd",
        "/d/Program Files/nodejs/node_global/codex.exe",
        "C:/Program Files/nodejs/node_global/codex",
        "C:/Program Files/nodejs/node_global/codex.cmd",
        "C:/Program Files/nodejs/node_global/codex.exe",
        os.path.expanduser("~/.npm-global/bin/codex"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return "codex"


def _redact(obj: dict[str, Any]) -> dict[str, Any]:
    """抹除敏感字段，用于 debug 日志。"""
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if any(sk.lower() in k.lower() for sk in _SECRET_KEYS):
            out[k] = "[REDACTED]"
        elif isinstance(v, str):
            out[k] = _redact_string(v)
        else:
            out[k] = v
    return out


def _redact_string(s: str) -> str:
    """抹除字符串中的 bearer token / API key 模式。"""
    import re
    s = re.sub(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED]", s)
    return s


# ====== 主类 ======

class CodexAppServerClient(AgentClient):
    """codex app-server harness：通过 JSON-RPC 管理 Thread 生命周期。

    harness_type=CODEX。每次 run() 对应一个或多个 turn：
      1. 获取 Thread Lease（防止并发）
      2. 启动 codex app-server 子进程 + initialize
      3. Thread 管理：内存缓存 -> thread/list -> thread/start
      4. turn/start + 监听 notification
      5. 流式输出 + 工具调用 + thinking
      6. 清理
    """

    def __init__(self, timeout: float = 300.0, max_iterations: int = 10,
                 turn_idle_timeout: float = 240.0):
        self.timeout = timeout
        self.max_iterations = max_iterations
        # turn 内 notification 空闲看门狗：连续 turn_idle_timeout 秒无任何
        # notification（delta/item/tool call/turn completed）判定为模型流挂死
        # （如 MiniMax 无响应），主动中断 turn，避免节点协程永久挂起。
        self.turn_idle_timeout = turn_idle_timeout
        self._agent_messages: dict[str, _AgentMessageState] = {}

    @property
    def harness_type(self) -> HarnessType:
        return HarnessType.CODEX

    async def run(
        self,
        prompt: str,
        tools: list[ToolDefinition],
        context: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """执行一次对话 turn（含 thread 创建/resume + turn/start + notification 循环）。

        Yields:
            AgentEvent: TEXT / THINKING / TOOL_USE / TOOL_RESULT / USAGE / ERROR / DONE。
        """
        assert_protocol_compatible(
            HarnessType.CODEX, context.protocol or "openai_compatible"
        )

        session_id = context.session_id
        persist = context.persist_session

        # 1. 获取 Thread Lease
        lease = await acquire_thread_lease(session_id, f"turn:{uuid4().hex[:8]}")
        if not lease:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error_message="该 session 已有活跃的 turn 或语音连接，请等待当前操作完成",
            )
            yield AgentEvent(type=AgentEventType.DONE)
            return

        client: CodexJsonRpcClient | None = None
        thread_id = ""
        usage_total = AgentUsage()
        final_text = ""

        # 去掉 provider/ 前缀（codex 用裸模型名如 claude-sonnet-4-20250514）
        model = context.model or ""
        if "/" in model:
            model = model.split("/", 1)[1]

        # P1（deepseek-harness 对齐）：沙箱模式优先取会话权限级别推导值，
        # 环境变量降级为部署级默认。此前固定环境变量默认 danger-full-access，
        # 会话切 read_only 后 codex 内仍是全盘可写（权限断联）。
        sandbox = (
            context.sandbox_mode
            or os.environ.get("AGENTOPS_CODEX_MANAGER_SANDBOX", "danger-full-access")
        )
        # 方案A：容器内执行时，cwd 必须是容器内路径（/workspace），而非 host 路径
        if context.container_id:
            cwd = "/workspace"
        else:
            cwd = context.workspace or os.getcwd()

        try:
            # 2. 启动 codex app-server
            # 方案A：当 context.container_id 非空时，CodexJsonRpcClient 通过 docker exec 在容器内启动 codex
            if context.container_id:
                # 容器内 codex 在 PATH 中（agentops-worker 镜像 ENV PATH 含 node_modules/.bin）
                codex_bin = "codex"
                # 写入 config.toml（codex 需要模型提供商配置才能连接 LLM API）
                await self._ensure_container_config(context.container_id, context)
            else:
                codex_bin = _resolve_codex_bin()
            env = self._build_env(context)
            client = CodexJsonRpcClient(
                codex_bin=codex_bin,
                cwd=cwd,
                env=env,
                container_id=context.container_id,
            )
            await client.start()

            try:
                await client.initialize(timeout=30.0)
            except Exception as e:
                logger.error("codex app-server initialize 失败: %s", e)
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error_message=f"codex app-server 初始化失败: {e}",
                )
                yield AgentEvent(type=AgentEventType.DONE)
                return

            # 3. 加载 skills（如果有）
            if context.skill_roots:
                try:
                    await client.request("skills/extraRoots/set", {
                        "extraRoots": context.skill_roots,
                    }, timeout=10.0)
                    await client.request("skills/list", {
                        "cwds": [cwd],
                        "forceReload": True,
                    }, timeout=10.0)
                except Exception as e:
                    logger.warning("codex skills 加载失败（不阻塞）: %s", e)

            # 4. Thread 管理
            tool_digest = _compute_tool_digest(tools)
            thread_id = await self._ensure_thread(
                client, session_id, tools, tool_digest, context, persist, model, sandbox, cwd
            )

            # 5. turn 循环（max_iterations 安全网）
            tool_map = {t.name: t for t in tools}
            turn_prompt = context.resumed_prompt if context.resumed_prompt else prompt

            for iteration in range(1, self.max_iterations + 1):
                # abort 检查
                if context.abort_signal and getattr(context.abort_signal, "is_set", lambda: False)():
                    logger.info("codex turn 被 abort（iteration=%d）", iteration)
                    break

                # turn/start
                turn_input: list[dict[str, Any]] = []
                if iteration == 1:
                    turn_input = [{"type": "text", "text": turn_prompt, "text_elements": []}]

                turn_params: dict[str, Any] = {
                    "threadId": thread_id,
                    "input": turn_input,
                    "cwd": cwd,
                }
                if model:
                    turn_params["model"] = model

                try:
                    turn_result = await client.request("turn/start", turn_params, timeout=30.0)
                except (CodexJsonRpcError, TimeoutError) as e:
                    logger.error("codex turn/start 失败: %s", e)
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        error_message=f"codex turn/start 失败: {e}",
                    )
                    yield AgentEvent(type=AgentEventType.DONE, usage=usage_total)
                    return

                turn_id = (
                    turn_result.get("turn_id")
                    or (turn_result.get("turn", {}) or {}).get("id", "")
                )
                logger.info("codex turn 开始 thread=%s turn=%s iteration=%d", thread_id, turn_id, iteration)

                # 6. notification 循环（带 idle 看门狗：
                #    连续 turn_idle_timeout 秒无任何 notification → 判定流挂死）
                turn_complete = False
                notif_iter = client.notifications().__aiter__()
                while True:
                    try:
                        notification = await asyncio.wait_for(
                            notif_iter.__anext__(),
                            timeout=self.turn_idle_timeout,
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        watchdog_msg = (
                            f"codex turn 空闲看门狗触发：{self.turn_idle_timeout}s 无任何 "
                            f"notification（模型流疑似挂死），中断 turn thread={thread_id}"
                        )
                        logger.error(watchdog_msg)
                        try:
                            await client.request(
                                "turn/interrupt",
                                {"threadId": thread_id, "turnId": turn_id},
                                timeout=5.0,
                            )
                        except Exception:
                            pass
                        yield AgentEvent(
                            type=AgentEventType.ERROR,
                            error_message=watchdog_msg,
                        )
                        turn_complete = True
                        break
                    method = notification.get("method", "")
                    params = notification.get("params", {}) or {}
                    req_id = notification.get("id")

                    logger.debug("codex notification: method=%s id=%s", method, req_id)

                    # abort 检查（在 notification 循环内）
                    if context.abort_signal and getattr(context.abort_signal, "is_set", lambda: False)():
                        try:
                            await client.request("turn/interrupt", {
                                "threadId": thread_id, "turnId": turn_id,
                            }, timeout=5.0)
                        except Exception:
                            pass
                        yield AgentEvent(type=AgentEventType.ERROR, error_message="用户取消")
                        turn_complete = True
                        break

                    # === 工具调用请求（需要响应）===
                    if method == "item/tool/call" and req_id is not None:
                        async for ev in self._handle_tool_call(client, req_id, params, tool_map, context):
                            yield ev
                        continue

                    # === 其他 notification -> 事件映射 ===
                    mapped_events = self._map_notification(method, params)
                    for ev in mapped_events:
                        yield ev
                        # 累积最终文本
                        if ev.type == AgentEventType.TEXT and ev.text:
                            final_text += ev.text

                    # turn 完成
                    if method == "turn/completed":
                        # 解析 usage
                        result = params.get("result", {}) if isinstance(params, dict) else {}
                        usage_data = result.get("usage", {}) if isinstance(result, dict) else {}
                        if isinstance(usage_data, dict):
                            usage_total = AgentUsage(
                                input_tokens=usage_total.input_tokens + int(usage_data.get("input_tokens", 0)),
                                output_tokens=usage_total.output_tokens + int(usage_data.get("output_tokens", 0)),
                            )
                        turn_complete = True
                        break

                    # 会话级错误
                    if method == "session.error":
                        error = params.get("error", {})
                        err_msg = str(error) if isinstance(error, dict) else str(error)
                        yield AgentEvent(type=AgentEventType.ERROR, error_message=err_msg)
                        turn_complete = True
                        break

                    # 普通错误（不 break，等 turn/completed 收尾）
                    if method == "error":
                        error = params.get("error", {})
                        if isinstance(error, dict):
                            err_msg = error.get("message", str(error))
                        else:
                            err_msg = str(params)
                        logger.warning("codex turn error: %s", err_msg)
                        yield AgentEvent(type=AgentEventType.ERROR, error_message=err_msg)
                        continue

                if turn_complete:
                    break

            # 迭代超限
            else:
                logger.warning("codex 超过最大迭代次数 %d", self.max_iterations)
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    error_message=f"超过最大迭代次数 ({self.max_iterations})",
                )

            # 7. emit usage + done
            logger.info(
                "codex turn 完成 thread=%s tokens_in=%d tokens_out=%d",
                thread_id, usage_total.input_tokens, usage_total.output_tokens,
            )
            yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
            yield AgentEvent(type=AgentEventType.TURN_COMPLETE, turn_number=1)
            yield AgentEvent(
                type=AgentEventType.DONE,
                text=final_text or None,
                usage=usage_total,
            )

        except BaseException as e:
            # BaseException：CancelledError（节点超时取消）也要 emit ERROR + DONE，
            # 保证 engine 侧事件流完整收尾（否则节点协程静默卡死）。
            if not isinstance(e, (GeneratorExit, StopAsyncIteration)):
                logger.exception("CodexAppServer harness 异常 session=%s", session_id)
                try:
                    yield AgentEvent(type=AgentEventType.USAGE, usage=usage_total)
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        error_message=f"codex harness 异常: {e}",
                        usage=usage_total,
                    )
                    yield AgentEvent(type=AgentEventType.DONE, usage=usage_total)
                except (GeneratorExit, StopAsyncIteration):
                    pass
        finally:
            # 清理路径加固：所有 await 在节点超时取消（CancelledError 环境）下
            # 必须仍然完成 —— 用 shield + wait_for 保证 lease / 子进程必然释放。
            # （原版 except Exception 捕获不到 CancelledError，导致 lease 泄漏、
            #   docker exec 子进程不清理，节点协程挂死在 finally。）
            if client and thread_id:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(client.request(
                            "thread/unsubscribe", {"threadId": thread_id}, timeout=5.0,
                        )),
                        timeout=8.0,
                    )
                except BaseException:
                    pass
            # 释放 Thread Lease（同步，安全）
            lease.release()
            # 关闭子进程（shield：取消上下文中也必须完成）
            if client:
                try:
                    await asyncio.wait_for(asyncio.shield(client.close()), timeout=15.0)
                except BaseException:
                    # shield 防 cancel，但 wait_for 超时仍可能到 —— 兜底强杀进程句柄
                    try:
                        await asyncio.shield(client.close())
                    except BaseException:
                        pass
            # 清理 agent message 状态
            self._agent_messages.clear()

    # ====== 环境构建 ======

    async def _ensure_container_config(self, container_id: str, context: AgentRunContext) -> None:
        """方案A：容器内执行 codex 前，写入 ~/.codex/config.toml 模型提供商配置。

        容器镜像（agentops-worker）不内置 config.toml，codex 不知道 base_url / wire_api。
        本方法通过 docker cp 写入最小化 config.toml，让 codex 能正确连接 LLM API。
        """
        import asyncio as _asyncio
        import tempfile as _tempfile

        # 从 context.model 提取裸模型名（去掉 provider/ 前缀）
        model = context.model or ""
        if "/" in model:
            model = model.split("/", 1)[1]
        if not model:
            model = "MiniMax-M3"

        base_url = context.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.minimaxi.com/v1"

        # P1：沙箱模式跟随会话权限级别（与 host 侧 thread/start 的 sandbox 参数一致）
        sandbox_mode = (
            context.sandbox_mode
            or os.environ.get("AGENTOPS_CODEX_MANAGER_SANDBOX", "danger-full-access")
        )

        # 最小化 config.toml：只含模型提供商配置（不含 host 路径、插件等）
        # env_key 告诉 codex 从哪个环境变量读 API key（旧字段 requires_openai_auth 已废弃）
        config_content = (
            'model_provider = "custom"\n'
            f'model = "{model}"\n'
            'disable_response_storage = true\n'
            f'sandbox_mode = "{sandbox_mode}"\n'
            '\n'
            '[model_providers.custom]\n'
            'name = "minimax"\n'
            f'base_url = "{base_url}"\n'
            'wire_api = "responses"\n'
            'env_key = "OPENAI_API_KEY"\n'
        )

        # 写到 host 临时文件，再 docker cp 到容器（避免 shell quoting 问题）
        with _tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False, encoding="utf-8") as f:
            f.write(config_content)
            host_path = f.name

        try:
            # mkdir + docker cp
            proc = await _asyncio.create_subprocess_exec(
                "docker", "exec", container_id, "mkdir", "-p", "/root/.codex",
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            proc = await _asyncio.create_subprocess_exec(
                "docker", "cp", host_path, f"{container_id}:/root/.codex/config.toml",
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode(errors="replace") if stderr else ""
                logger.warning(
                    "写入容器 config.toml 失败（docker cp）container=%s rc=%s err=%s",
                    container_id, proc.returncode, err[:200],
                )
            else:
                logger.info("容器 config.toml 已写入 container=%s model=%s", container_id, model)
        finally:
            try:
                os.unlink(host_path)
            except OSError:
                pass

    def _build_env(self, context: AgentRunContext) -> dict[str, str]:
        """构建 codex 子进程环境变量。

        codex 的 config.toml 配置了 [model_providers.custom]，走 OpenAI wire_api=responses，
        所以 codex 读 OPENAI_API_KEY / OPENAI_BASE_URL。
        不隔离 CODEX_HOME：让 codex 读全局 ~/.codex/config.toml 的 provider 配置。
        """
        env: dict[str, str] = {}

        # API key：优先 context.api_key，然后环境变量
        api_key = context.api_key or os.environ.get("OPENAI_API_KEY") or ""
        if api_key:
            env["OPENAI_API_KEY"] = api_key

        # base_url：如果 context 有且与 config.toml 不同，覆盖
        if context.base_url:
            env["OPENAI_BASE_URL"] = context.base_url
        elif os.environ.get("OPENAI_BASE_URL"):
            env["OPENAI_BASE_URL"] = os.environ["OPENAI_BASE_URL"]

        logger.debug("codex env: %s", _redact(env))
        return env

    # ====== Thread 管理 ======

    async def _ensure_thread(
        self,
        client: CodexJsonRpcClient,
        session_id: str,
        tools: list[ToolDefinition],
        tool_digest: str,
        context: AgentRunContext,
        persist: bool,
        model: str,
        sandbox: str,
        cwd: str,
    ) -> str:
        """查找并 resume 已有 thread，或创建新 thread。

        优先级：
          1. 内存缓存（session_id -> {thread_id, tool_digest}）
          2. thread/list 搜索（按 name 匹配）
          3. thread/start 创建新线程
        """
        thread_name = _thread_name(session_id, tool_digest)
        # codex 的 modelProvider 必须匹配 ~/.codex/config.toml 的 [model_providers.X] key，
        # 而非 models.yaml 的 provider ID（如 "minimax" -> codex config 里是 "custom"）。
        # 不传 modelProvider 让 codex 用 config.toml 的 model_provider 默认值。
        _CODEX_PROVIDER_KEYS = {"openai", "anthropic", "custom"}
        raw_provider = context.provider or context.extra.get("modelProvider") or ""
        provider = raw_provider if raw_provider in _CODEX_PROVIDER_KEYS else ""

        # 1. 内存缓存检查
        cached = _native_sessions.get(session_id)
        if cached and cached["tool_digest"] == tool_digest:
            try:
                resume_params: dict[str, Any] = {
                    "threadId": cached["thread_id"],
                    "cwd": cwd,
                    "sandbox": sandbox,
                    "approvalPolicy": "never",
                }
                if model:
                    resume_params["model"] = model
                if provider:
                    resume_params["modelProvider"] = provider

                result = await client.request("thread/resume", resume_params, timeout=15.0)
                tid = result.get("thread_id") or result.get("threadId") or cached["thread_id"]
                logger.info("codex thread resume（内存缓存） thread=%s", tid)
                return tid
            except Exception as e:
                logger.warning("codex thread resume 失败（内存缓存），将重新搜索: %s", e)
                _native_sessions.pop(session_id, None)

        # 2. thread/list 搜索
        try:
            listed = await client.request("thread/list", {
                "limit": 20,
                "sourceKinds": ["appServer"],
                "cwd": cwd,
                "searchTerm": thread_name,
                "useStateDbOnly": True,
            }, timeout=15.0)
            entries = listed.get("data", []) if isinstance(listed, dict) else []
            if isinstance(entries, list):
                match = next(
                    (e for e in entries if isinstance(e, dict) and e.get("name") == thread_name and e.get("id")),
                    None,
                )
                if match:
                    try:
                        resume_params = {
                            "threadId": str(match["id"]),
                            "cwd": cwd,
                            "sandbox": sandbox,
                            "approvalPolicy": "never",
                        }
                        if model:
                            resume_params["model"] = model
                        if provider:
                            resume_params["modelProvider"] = provider

                        result = await client.request("thread/resume", resume_params, timeout=15.0)
                        tid = result.get("thread_id") or result.get("threadId") or str(match["id"])
                        _native_sessions[session_id] = {"thread_id": tid, "tool_digest": tool_digest}
                        logger.info("codex thread resume（thread/list） thread=%s", tid)
                        return tid
                    except Exception as e:
                        logger.warning("codex thread resume 失败（thread/list），将创建新线程: %s", e)
        except Exception as e:
            logger.warning("codex thread/list 失败（不阻塞）: %s", e)

        # 3. thread/start 创建新线程
        dynamic_tools = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in tools
        ]
        start_params: dict[str, Any] = {
            "baseInstructions": context.system_prompt,
            "developerInstructions": None,
            "cwd": cwd,
            "dynamicTools": dynamic_tools,
            "ephemeral": not persist,
            "approvalPolicy": "never",
            "sandbox": sandbox,
        }
        if model:
            start_params["model"] = model
        if provider:
            start_params["modelProvider"] = provider
        if context.service_tier:
            start_params["serviceTier"] = context.service_tier
        if context.reasoning_effort:
            start_params["reasoningEffort"] = context.reasoning_effort

        result = await client.request("thread/start", start_params, timeout=30.0)
        tid = (
            result.get("thread_id")
            or result.get("threadId")
            or (result.get("thread", {}) or {}).get("id")
        )
        if not tid:
            raise RuntimeError("codex thread/start 未返回 thread_id")

        # thread/name/set（持久化时命名，便于后续 thread/list 搜索）
        if persist:
            try:
                await client.request("thread/name/set", {
                    "threadId": tid,
                    "name": thread_name,
                }, timeout=10.0)
            except Exception as e:
                logger.warning("codex thread/name/set 失败（不阻塞）: %s", e)

        _native_sessions[session_id] = {"thread_id": tid, "tool_digest": tool_digest}
        logger.info("codex thread 创建 thread=%s name=%s", tid, thread_name)
        return tid

    # ====== 工具调用处理 ======

    async def _handle_tool_call(
        self,
        client: CodexJsonRpcClient,
        request_id: int,
        params: dict,
        tool_map: dict[str, ToolDefinition],
        context: AgentRunContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """处理 codex 的 item/tool/call 请求。

        codex 发来工具调用请求 -> 我们执行 handler -> 返回结果。
        这与 item/started+item/completed 的 mcpToolCall 不同：
        - item/tool/call: codex 请求我们执行（需要 respond_tool_call）
        - item/started/completed: codex 自己执行并通知结果

        P1（deepseek-harness 对齐）：执行前调用 context.permission_check 做
        动态 tier 校验（fail-closed，此前该路径完全绕过权限拦截）。
        """
        tool_name = params.get("tool", params.get("name", ""))
        call_id = params.get("callId", params.get("call_id", str(request_id)))

        # 解析参数
        raw_args = params.get("arguments", params.get("input", {}))
        if isinstance(raw_args, str):
            try:
                tool_args = json.loads(raw_args)
            except json.JSONDecodeError:
                tool_args = {"_raw": raw_args}
        else:
            tool_args = raw_args if isinstance(raw_args, dict) else {}

        yield AgentEvent(
            type=AgentEventType.TOOL_USE,
            tool_use_id=call_id,
            tool_name=tool_name,
            tool_input=tool_args,
        )

        # 执行 handler
        content_text = ""
        is_error = False
        handler = tool_map.get(tool_name)
        if handler and handler.handler:
            # P1：动态权限校验（fail-closed：校验异常一律拒绝，不放行）
            if context is not None and context.permission_check is not None:
                try:
                    check_result = context.permission_check(tool_name)
                    if hasattr(check_result, "__await__"):
                        await check_result
                except PermissionError as pe:
                    logger.info(
                        "codex tool=%s 被权限校验拒绝 session=%s: %s",
                        tool_name, context.session_id, pe,
                    )
                    content_text = f"权限不足，工具调用被拒绝: {pe}"
                    is_error = True
                    await client.respond_tool_call(request_id, content_text, success=False)
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        tool_use_id=call_id,
                        tool_name=tool_name,
                        tool_result=content_text,
                        tool_is_error=True,
                    )
                    return
                except Exception as e:
                    # fail-closed：校验器自身故障也拒绝（deepseek unavailable 语义）
                    logger.warning("codex tool=%s 权限校验器异常（fail-closed 拒绝）: %s", tool_name, e)
                    content_text = f"权限校验器异常，工具调用被拒绝: {e}"
                    is_error = True
                    await client.respond_tool_call(request_id, content_text, success=False)
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        tool_use_id=call_id,
                        tool_name=tool_name,
                        tool_result=content_text,
                        tool_is_error=True,
                    )
                    return
            try:
                result = await handler.handler(tool_args)
                if isinstance(result, dict):
                    # 提取 content 文本
                    content = result.get("content", result)
                    if isinstance(content, list):
                        # [{type: "text", text: "..."}, ...]
                        content_text = "\n".join(
                            b.get("text", str(b)) for b in content if isinstance(b, dict)
                        )
                    else:
                        content_text = str(content)
                    is_error = bool(result.get("is_error", False))
                else:
                    content_text = str(result)
            except Exception as e:
                logger.exception("codex tool=%s handler 异常", tool_name)
                content_text = f"tool error: {e}"
                is_error = True
        else:
            content_text = f"未知工具 {tool_name}（未注册 handler）"
            is_error = True

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            tool_use_id=call_id,
            tool_name=tool_name,
            tool_result=content_text,
            tool_is_error=is_error,
        )

        # 响应 codex 的 tool/call 请求
        await client.respond_tool_call(request_id, content_text, success=not is_error)

    # ====== notification 映射 ======

    def _map_notification(
        self,
        method: str,
        params: dict,
    ) -> list[AgentEvent]:
        """将 codex notification 映射为 AgentEvent 列表。

        处理所有非 item/tool/call 的 notification。
        item/tool/call 在主循环中单独处理（需要 respond_tool_call）。
        """
        events: list[AgentEvent] = []
        if params is None:
            params = {}

        if method == "item/agentMessage/delta":
            # 流式文本增量
            delta = params.get("delta", "")
            item_id = params.get("itemId", "")
            if delta:
                state = self._agent_messages.setdefault(item_id, _AgentMessageState())
                state.deltas.append(delta)
                state.deltas_yielded = True
                events.append(AgentEvent(type=AgentEventType.TEXT, text=delta))

        elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            # 推理过程增量
            delta = params.get("delta") or params.get("text") or ""
            if delta:
                events.append(AgentEvent(type=AgentEventType.THINKING, text=delta))

        elif method == "item/started":
            # item 开始：记录类型和 phase
            item = params.get("item", params)
            root = item.get("root", item) if isinstance(item, dict) else item
            if not isinstance(root, dict):
                return events
            item_type = root.get("type", "")

            if item_type == "agentMessage":
                item_id = root.get("id", "")
                phase = root.get("phase")  # "commentary" | "final_answer"
                state = self._agent_messages.setdefault(item_id, _AgentMessageState())
                state.phase = phase

            elif item_type == "commandExecution":
                # codex 内置 Bash 工具开始执行
                events.append(AgentEvent(
                    type=AgentEventType.TOOL_USE,
                    tool_use_id=root.get("id", ""),
                    tool_name="bash",
                    tool_input={"command": root.get("command", "")},
                ))

            elif item_type == "mcpToolCall":
                # MCP 工具调用开始（codex 内部 MCP，非 dynamicTools）
                events.append(AgentEvent(
                    type=AgentEventType.TOOL_USE,
                    tool_use_id=root.get("id", ""),
                    tool_name=root.get("tool", ""),
                    tool_input=root.get("arguments", {}),
                ))

        elif method == "item/completed":
            # item 完成：输出最终内容
            item = params.get("item", params)
            root = item.get("root", item) if isinstance(item, dict) else item
            if not isinstance(root, dict):
                return events
            item_type = root.get("type", "")

            if item_type == "agentMessage":
                item_id = root.get("id", "")
                state = self._agent_messages.pop(item_id, _AgentMessageState())
                # 优先用 completed 的完整文本，fallback 到 delta 拼接
                text = root.get("text", "") or "".join(state.deltas)
                if text and not state.deltas_yielded:
                    # delta 没流式过，补发完整文本
                    events.append(AgentEvent(type=AgentEventType.TEXT, text=text))
                # deltas 已流式过的不重复发（final_text 在主循环累积）
                # 但如果 completed 有完整文本且与 delta 拼接不同，用 completed 的
                # （主循环会累积所有 TEXT 事件，所以这里只在没流式过时补发）

            elif item_type == "commandExecution":
                # codex 内置 Bash 执行完成
                exit_code = root.get("exitCode", root.get("exit_code", 0))
                output = root.get("aggregatedOutput", root.get("aggregated_output", ""))
                events.append(AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    tool_use_id=root.get("id", ""),
                    tool_name="bash",
                    tool_result=output,
                    tool_is_error=(exit_code is not None and exit_code != 0),
                ))

            elif item_type == "mcpToolCall":
                # MCP 工具调用完成
                error = root.get("error")
                result = root.get("result", {})
                content = ""
                if error and isinstance(error, dict):
                    content = error.get("message", str(error))
                elif result and isinstance(result, dict):
                    blocks = result.get("content", [])
                    if isinstance(blocks, list):
                        content = "\n".join(
                            b.get("text", str(b)) for b in blocks if isinstance(b, dict)
                        )
                events.append(AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    tool_use_id=root.get("id", ""),
                    tool_name=root.get("tool", ""),
                    tool_result=content,
                    tool_is_error=bool(error),
                ))

        elif method == "turn/started":
            # turn 开始：记录 turn_id（主循环已处理）
            pass

        elif method == "turn/completed":
            # turn 完成：flush 未完成的 agentMessage deltas
            for item_id, state in list(self._agent_messages.items()):
                text = "".join(state.deltas)
                if text and not state.deltas_yielded:
                    events.append(AgentEvent(type=AgentEventType.TEXT, text=text))
                self._agent_messages.pop(item_id, None)

        elif method == "thread/realtime/transcript/delta":
            # 语音模式：实时转录增量
            delta = params.get("delta", "")
            if delta:
                events.append(AgentEvent(type=AgentEventType.TEXT, text=delta))

        elif method == "thread/realtime/transcript/done":
            # 语音模式：完整转录
            text = params.get("text", "")
            if text:
                events.append(AgentEvent(type=AgentEventType.TEXT, text=text))

        # 其他 notification 静默忽略
        return events
