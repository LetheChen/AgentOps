"""SessionEngine - Thread 模式 Session 引擎。

替代 ConversationalEngine，核心区别：
  - session_id 作为唯一标识（不是 run_id）
  - 多轮上下文由 harness thread 维护（不靠客户端拼接消息）
  - 消息持久化到 session_messages 表
  - 事件用新类型：turn.started / turn.progress / turn.completed / session.dormant
  - Thread Lease 并发控制（同一 session 同时只能有一个活跃 turn）

生命周期：
  1. 首次对话：createSession() -> SessionEngine.start_turn(message)
  2. 后续对话：SessionEngine.start_turn(message)（harness thread resume）
  3. 语音模式：SessionEngine.start_voice(sdp) / stop_voice()
  4. 关闭：SessionEngine.close()

参考：docs/architecture/DESIGN_thread_session_refactor.md
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from harness.protocol import (
    AgentEventType,
    AgentRunContext,
    HarnessRegistry,
    HarnessType,
)
from orchestrator.protocol import (
    DagEvent,
    DagEventType,
    RunStatus,
    SessionStatus,
)
from orchestrator.conversation_kit import (
    make_conversational_tools,
    ConversationState,
    _build_tools_prompt,
    _extract_and_run_tool_calls,
)

logger = logging.getLogger(__name__)

# idle 超时：无新消息时 session 转 dormant
IDLE_TIMEOUT_SECONDS = 120


@dataclass
class TurnResult:
    """一轮对话（turn）的最终结果，供 P4 hybrid 等调用方 await 拿产物。

    字段：
      turn_id: turn 唯一 ID
      status: "completed" / "failed" / "cancelled"
      summary: finalize 工具写入的最终摘要（无则回退 assistant_text）
      assistant_text: 本轮累积的流式文本
      total_tokens_input / total_tokens_output: 本轮 token 统计
    """
    turn_id: str
    status: str
    summary: str = ""
    assistant_text: str = ""
    total_tokens_input: int = 0
    total_tokens_output: int = 0


class SessionEngine:
    """Thread 模式 Session 引擎。

    每个 session_id 对应一个 SessionEngine 实例。
    多轮上下文由 harness thread 维护（不靠客户端拼接）。
    """

    def __init__(
        self,
        session_id: str,
        agent_id: str,
        llm_config: dict[str, Any],
        event_sink: Callable[[DagEvent], Awaitable[None]],
        harness_type: HarnessType,
        system_prompt: str,
        event_store: Any = None,
        cross_domain_coordinator: Any = None,
        workspace_id: str | None = None,
        agent_tier: str = "T2",
        approval_service: Any = None,
    ):
        self.session_id = session_id
        self.agent_id = agent_id
        self.llm_config = llm_config
        self.event_sink = event_sink
        self.harness_type = harness_type
        self.approval_service = approval_service
        self.system_prompt = system_prompt
        self.event_store = event_store
        self.coordinator = cross_domain_coordinator
        self.workspace_id = workspace_id
        self.agent_tier = agent_tier

        self._cancel = asyncio.Event()
        self._pending_inputs: dict[str, asyncio.Queue] = {}
        self._active = False
        self._turn_count = 0
        self._total_tokens_input = 0
        self._total_tokens_output = 0

        # 新 session 重置该 agent 的 surface 状态：phase tracker / dedup 按 actor
        # 全局存（_PHASE_TRACKER 只按 actor_id 键控），不重置的话上一 session 的
        # final phase 会拒绝本 session 的 started（phase_not_monotonic），
        # 残留 surface_id dedup 也会吞掉新 session 的首条事件。
        try:
            from tools.report_surface_state import reset_run_surface_state
            reset_run_surface_state([self.agent_id])
        except Exception as e:
            logger.debug("重置 %s surface 状态失败（不阻塞）: %s", self.agent_id, e)

    async def _read_permission_level(self) -> str | None:
        """实时读取生效的权限级别（会话级优先，回退 workspace 级）。

        deepseek-harness 对齐：策略按调用解析（per-call resolve）而非 turn 快照，
        权限切换对后续工具调用立即生效。
        """
        if not self.event_store:
            return None
        try:
            sess_row = await self.event_store.get_session(self.session_id)
            sess_perm_level = (sess_row or {}).get("permission_level")
            if sess_perm_level:
                return sess_perm_level
            if self.workspace_id is not None:
                ws_row = await self.event_store.get_authorized_workspace(self.workspace_id)
                if ws_row:
                    return ws_row.get("permissions")
        except Exception as e:
            logger.warning("读取权限级别失败（回退 None）: %s", e)
        return None

    async def _effective_tier_now(self) -> str:
        """实时计算有效 tier = min(权限级别 tier, agent tier)。"""
        from orchestrator.workspace_paths import effective_tier, permission_level_to_tier
        level = await self._read_permission_level()
        if not level:
            return self.agent_tier
        return effective_tier(permission_level_to_tier(level), self.agent_tier)

    async def _check_tool_permission(self, tool_name: str) -> None:
        """harness 工具执行前的动态权限校验（fail-closed）。

        作为 AgentRunContext.permission_check 注入：codex dynamicTools 的
        item/tool/call 路径此前完全绕过 tier 校验，此回调补上（每次调用
        实时读取权限，切换立即生效）。抛 PermissionError = 拒绝。

        P2（deepseek-harness 对齐）：tier 不足时不直接拒绝，先走
        allowed-once 审批——用户「允许本次」则放行这一次调用，
        其余结果（rejected/cancelled/unavailable）一律 fail closed。
        结构性禁止（通用对话未绑定 workspace）不进审批。
        """
        from orchestrator.workspace_paths import (
            REQUIRES_WORKSPACE_TOOLS,
            TierPermissionError,
            check_tool_tier_permission,
        )
        tier = await self._effective_tier_now()
        has_workspace = self.workspace_id is not None

        # 结构性禁止：无 workspace 的会话禁用文件/命令类工具（不可审批放行）
        if not has_workspace and tool_name in REQUIRES_WORKSPACE_TOOLS:
            raise TierPermissionError(
                f"通用对话（未绑定 workspace）禁止调用工具 {tool_name}"
            )

        try:
            check_tool_tier_permission(
                tool_name=tool_name,
                session_tier=tier,
                has_workspace=has_workspace,
            )
        except TierPermissionError as e:
            # tier 不足 → allowed-once 审批（无审批服务时维持旧行为：直接拒绝）
            if self.approval_service is None:
                raise
            outcome = await self.approval_service.request(
                self.session_id,
                tool_name,
                reason=str(e),
            )
            if outcome != "allowed-once":
                raise TierPermissionError(
                    f"用户未授权 {tool_name} 的本次执行（outcome={outcome}）"
                ) from e
            # allowed-once：仅本次放行，不改变会话权限级别
            logger.info(
                "session=%s tool=%s 获得用户 allowed-once 放行（tier=%s）",
                self.session_id, tool_name, tier,
            )

    async def _resolve_workspace_path(self) -> str:
        """workspace_id → 实际目录路径（authorized_workspaces.source_path）。

        会话跑错目录（"会话跳出 agentops 项目"）的根因修复：
        此前 workspace=os.getcwd() —— 取决于 API 服务进程的启动目录，
        而不是会话绑定的 workspace。

        解析规则：
          - workspace_id 存在 → authorized_workspaces.source_path
            （bind_mount 直接用源目录；local_copy 会话场景不复制 sandbox，
             直接读源目录 —— 对话式交互不应每 turn 复制整个项目）
          - 无 workspace_id / 查询失败 → 回退 os.getcwd()（兼容未绑定
            workspace 的旧会话；claude_sdk/claude_code harness 有
            cwd fail-fast，空 workspace 会显式报错而不是静默跑错）
        """
        if self.workspace_id and self.event_store:
            try:
                row = await self.event_store.get_authorized_workspace(
                    self.workspace_id
                )
                if row:
                    source = row.get("source_path")
                    if source:
                        return source
                    logger.warning(
                        "workspace %s (mode=%s) 无 source_path，回退进程 cwd",
                        self.workspace_id, row.get("mode"),
                    )
                else:
                    logger.warning(
                        "workspace %s 不存在于 authorized_workspaces，回退进程 cwd",
                        self.workspace_id,
                    )
            except Exception as e:
                logger.warning(
                    "解析 workspace %s 失败（回退进程 cwd）: %s",
                    self.workspace_id, e,
                )
        return os.getcwd()

    async def start_turn(self, message: str) -> TurnResult | None:
        """执行一轮对话（turn）。

        流程：
          1. 持久化 user message -> session_messages
          2. emit turn.started
          3. 创建 harness client，调 client.run(message, tools, context)
          4. 转发 AgentEvent -> DagEvent -> SSE
          5. turn 完成后持久化 assistant message -> session_messages
          6. emit turn.completed
        """
        if self._active:
            # 已有 turn 在执行，将消息放入队列
            queue = self._pending_inputs.setdefault("chat_input", asyncio.Queue())
            queue.put_nowait({"message": message})
            return

        self._active = True
        self._cancel.clear()
        turn_id = f"turn_{uuid4().hex[:8]}"
        self._turn_count += 1

        try:
            # 1. 持久化 user message
            if self.event_store:
                await self.event_store.append_session_message(
                    self.session_id, "user", message, turn_id=turn_id,
                )

            # 2. emit turn.started
            await self.event_sink(DagEvent(
                type=DagEventType.TURN_STARTED,
                run_id=self.session_id,
                node_id=f"conv:{self.agent_id}",
                payload={"turn_id": turn_id, "message": message[:200]},
                sequence=0,
            ))

            # 2.5 计算 effective_tier / has_workspace / sandbox_mode（会话级权限优先）
            # 权限级别与 workspace 解耦：优先读 session.permission_level
            # 无 session 级权限时回退到 workspace.permissions（兼容旧逻辑）
            has_workspace = self.workspace_id is not None
            session_tier = await self._effective_tier_now()
            # P1（deepseek-harness 对齐）：权限级别同时推导 codex sandbox 模式，
            # 堵住「会话 read_only 但 codex 内 danger-full-access」的权限断联。
            # turn 级快照：thread/start 与 thread/resume 均携带，权限收紧自下个 turn 生效；
            # turn 内实时收紧由 permission_check 回调兜底。
            from orchestrator.workspace_paths import permission_level_to_sandbox_mode
            sandbox_mode = permission_level_to_sandbox_mode(
                await self._read_permission_level()
            )

            # 3. 创建 harness client
            try:
                logger.info(
                    "SessionEngine turn 启动: agent=%s harness=%s workspace_id=%s",
                    self.agent_id, self.harness_type.value, self.workspace_id,
                )
                client = HarnessRegistry.create(self.harness_type)
                logger.info(
                    "SessionEngine harness 实例化: %s", type(client).__name__
                )
            except Exception as e:
                await self._emit_failed(turn_id, f"harness 创建失败: {e}")
                return TurnResult(turn_id=turn_id, status="failed", summary=str(e))

            # 4. 构造工具
            # 创建轻量 ConversationState 供 make_conversational_tools 使用
            conv_state = ConversationState(
                run_id=self.session_id,
                agent_id=self.agent_id,
                turn_count=self._turn_count,
            )
            tools = make_conversational_tools(
                conv_state, self.event_sink,
                agent_id=self.agent_id,
                coordinator=self.coordinator,
                parent_run_id=self.session_id,
            )

            # A2UI surface 工具（对话场景 → 右侧 Supervision 面板）：
            # 双门槛注入——agent.yaml allowed_tools 显式声明（授权）
            # 且 config/actors/<agent_id>/actor_visual_profile.json 存在（view 白名单）。
            #
            # 两类工具（阶段 2 改造）：
            # 1. present_content_surface（兼容期保留）：content_type → _map_* → report_surface_state 校验链
            # 2. upsert_generated_view（新增，manager 自由 A2UI）：独立校验链，不走 report_surface_state
            #    present_content 将在阶段 2 后期改为 upsert_generated_view 的语法糖
            try:
                from orchestrator.config_loader import get_system_config
                agent_def = get_system_config().agents.get(self.agent_id)
                if agent_def:
                    allowed = agent_def.allowed_tools or []
                    # 新路径：upsert_generated_view（manager 自由 A2UI，escape hatch）
                    if "upsert_generated_view" in allowed:
                        from tools.upsert_generated_view import make_upsert_generated_view_tool
                        tools.append(make_upsert_generated_view_tool(
                            actor_id=self.agent_id,
                            run_id=self.session_id,
                            event_sink=self.event_sink,
                            node_id=f"conv:{self.agent_id}",
                        ))
                    # v2：present_content_surface 已裁撤（manager 展示型走 present_content → upsert 链 → 大屏）
                    # make_present_content_surface_tool 函数保留供 DAG fallback 路径（workflow/engine.py）使用
            except Exception as e:
                logger.debug("agent %s 跳过 surface 工具注入: %s", self.agent_id, e)

            # 5. 构造 context
            system_prompt = self.system_prompt
            # 🆕 注入 WorkflowRegistry 动态注册表（替代 manager.yaml 硬编码 workflow 路由表）
            try:
                from orchestrator._registry import get_workflow_registry
                wf_reg = get_workflow_registry()
                if wf_reg:
                    wf_section = wf_reg.build_prompt_section()
                    if wf_section:
                        system_prompt = f"{system_prompt}\n\n{wf_section}"
            except Exception as e:
                logger.warning("注入 workflow registry 失败（不阻塞）: %s", e)
            # 🆕 Phase 2: 注入 SkillRegistry metadata 段（不全量 inline body，LLM 按需调 read_skill）
            try:
                from orchestrator._registry import get_skill_registry
                skill_reg = get_skill_registry()
                if skill_reg:
                    # 从 ConfigLoader 反查当前 agent 的业务域（_shared 域 skill 所有 agent 可见）
                    agent_domain = "manager"  # 默认兜底
                    try:
                        from orchestrator.config_loader import get_system_config
                        cfg = get_system_config()
                        agent_def = cfg.agents.get(self.agent_id)
                        if agent_def and getattr(agent_def, "domain", None):
                            agent_domain = agent_def.domain
                    except Exception:
                        pass  # ConfigLoader 不可用时用默认 manager 域
                    skill_section = skill_reg.build_prompt_section(agent_domain)
                    if skill_section:
                        system_prompt = f"{system_prompt}\n\n{skill_section}"
            except Exception as e:
                logger.warning("注入 skill registry 失败（不阻塞）: %s", e)
            # 🚑 补丁：注入工具描述到 system_prompt（v1 conversational.py:743-746 等价逻辑）
            # opencode harness 不转发 tools → LLM 不知道有 trigger_workflow/present_content 等工具
            # 必须把工具描述 + <tool_call> 调用格式注入 system_prompt，LLM 才会用文本模拟调用
            #
            # claude_sdk 例外：工具通过 in-process MCP server 原生注册
            # （tool_use 协议），注入文本标记协议反而会让模型发出无人解析的
            # <tool_call>...】 文本（harness 不做文本协议后处理）。
            if self.harness_type != HarnessType.CLAUDE_SDK:
                tools_prompt = _build_tools_prompt(tools)
                if tools_prompt:
                    system_prompt = f"{system_prompt}\n\n## 可用工具\n{tools_prompt}"
            # 注入记忆上下文
            try:
                from orchestrator._registry import get_memory_manager
                mem_mgr = get_memory_manager()
                if mem_mgr:
                    memory_context = await mem_mgr.build_context(session_id=self.session_id)
                    if memory_context:
                        system_prompt = f"{system_prompt}\n\n## 历史记忆\n{memory_context}"
            except Exception as e:
                logger.warning("注入记忆上下文失败（不阻塞）: %s", e)

            # 截断限制：MiniMax-M3 context window 128K tokens，4000 字符太保守会把 A2UI 路由表截掉
            # （manager.yaml system_prompt 6501 字符，A2UI 段从 4269 开始）
            # 提升到 20000 字符（约 5000 tokens），足够容纳 system_prompt + tools_prompt + memory_context
            context = AgentRunContext(
                system_prompt=system_prompt[:20000],
                model=self.llm_config.get("model", ""),
                api_key=self.llm_config.get("api_key", ""),
                base_url=self.llm_config.get("base_url", ""),
                workspace=await self._resolve_workspace_path(),
                session_id=self.session_id,
                persist_session=True,
                provider=self.llm_config.get("provider") or self.llm_config.get("model_provider"),
                service_tier=self.llm_config.get("service_tier"),
                reasoning_effort=self.llm_config.get("reasoning_effort"),
                tools=tools,
                # P1：会话权限推导的沙箱模式（None 时 harness 用部署默认）
                sandbox_mode=sandbox_mode,
                # P1：工具执行前动态权限校验（补上 dynamicTools 路径缺失的 tier 拦截）
                permission_check=self._check_tool_permission,
            )

            # 6. 调用 harness
            # 🚑 补丁：设置当前活跃 run_id，让 trigger_workflow 等工具 handler 能反查父会话
            # v1 conversational.py:933-934 的等价逻辑
            from orchestrator._registry import set_current_active_run_id
            set_current_active_run_id(self.session_id)
            assistant_text = ""
            try:
                async for ev in client.run(message, tools, context):
                    if self._cancel.is_set():
                        break

                    if ev.type == AgentEventType.TEXT and ev.text:
                        # 🚑 补丁：拦截 <tool_call> 标记并执行 handler
                        # opencode harness 不转发 tools → LLM 用文本模拟 tool_call
                        # v1 conversational.py:940-942 的等价逻辑
                        processed_text, _had_tool_calls = await _extract_and_run_tool_calls(
                            ev.text, tools, self.event_sink,
                            session_tier=session_tier,
                            has_workspace=has_workspace,
                        )
                        assistant_text += processed_text
                        # emit turn.progress（实时输出）
                        await self.event_sink(DagEvent(
                            type=DagEventType.TURN_PROGRESS,
                            run_id=self.session_id,
                            node_id=f"conv:{self.agent_id}",
                            payload={"text": processed_text, "turn_id": turn_id},
                            sequence=0,
                        ))

                    elif ev.type == AgentEventType.THINKING and ev.text:
                        # claude_sdk：ThinkingBlock 结构化转发（前端可折叠展示）
                        await self.event_sink(DagEvent(
                            type=DagEventType.TURN_PROGRESS,
                            run_id=self.session_id,
                            node_id=f"conv:{self.agent_id}",
                            payload={"text": ev.text, "turn_id": turn_id, "is_thinking": True},
                            sequence=0,
                        ))

                    elif ev.type == AgentEventType.TOOL_USE:
                        # v74：工具调用属于对话区内容（类 Claude Code 时间线），
                        # 不再 emit widget.update 推到右栏（v73 错把 TOOL_USE 当 widget → 右栏显示「未知组件: tool_use」+ JSON）
                        await self.event_sink(DagEvent(
                            type=DagEventType.CONVERSATION_TOOL_USE,
                            run_id=self.session_id,
                            node_id=f"conv:{self.agent_id}",
                            payload={
                                "tool_use_id": ev.tool_use_id,
                                "tool_name": ev.tool_name,
                                "input": ev.tool_input,
                                "turn_id": turn_id,
                            },
                            sequence=0,
                        ))

                    elif ev.type == AgentEventType.USAGE and ev.usage:
                        self._total_tokens_input += ev.usage.input_tokens
                        self._total_tokens_output += ev.usage.output_tokens

                    elif ev.type == AgentEventType.ERROR:
                        logger.warning("SessionEngine agent error: %s", ev.error_message)
                        await self.event_sink(DagEvent(
                            type=DagEventType.TURN_PROGRESS,
                            run_id=self.session_id,
                            node_id=f"conv:{self.agent_id}",
                            payload={"text": f"⚠️ {ev.error_message}", "turn_id": turn_id, "is_error": True},
                            sequence=0,
                        ))

                    elif ev.type == AgentEventType.DONE:
                        break
            finally:
                # 🚑 补丁：清空上下文，避免下个 turn 误判
                set_current_active_run_id(None)

            # 7. 持久化 assistant message
            if assistant_text and self.event_store:
                await self.event_store.append_session_message(
                    self.session_id, "assistant", assistant_text, turn_id=turn_id,
                    metadata={"tokens_input": self._total_tokens_input, "tokens_output": self._total_tokens_output},
                )

            # 8. emit turn.completed
            await self.event_sink(DagEvent(
                type=DagEventType.TURN_COMPLETED,
                run_id=self.session_id,
                node_id=f"conv:{self.agent_id}",
                payload={
                    "turn_id": turn_id,
                    "turn_count": self._turn_count,
                    "summary": assistant_text[:200] if assistant_text else "",
                    "total_tokens_input": self._total_tokens_input,
                    "total_tokens_output": self._total_tokens_output,
                },
                sequence=0,
            ))

            # 更新 session 状态
            if self.event_store:
                await self.event_store.update_session_status(
                    self.session_id, SessionStatus.ACTIVE.value, last_activity=False,
                )

            # G3：返回 TurnResult，供 P4 hybrid 等调用方 await 拿 summary/tokens
            final_summary = getattr(conv_state, "final_summary", "") or assistant_text
            return TurnResult(
                turn_id=turn_id,
                status="completed",
                summary=final_summary,
                assistant_text=assistant_text,
                total_tokens_input=self._total_tokens_input,
                total_tokens_output=self._total_tokens_output,
            )

        except Exception as e:
            logger.exception("SessionEngine turn 异常 session=%s", self.session_id)
            await self._emit_failed(turn_id, str(e))
            return TurnResult(turn_id=turn_id, status="failed", summary=str(e))
        finally:
            # P2：turn 结束清理挂起审批（落 cancelled，前端弹窗随 approval.decided 消失）
            if self.approval_service is not None:
                try:
                    self.approval_service.cancel_pending(self.session_id)
                except Exception as e:
                    logger.warning("清理挂起审批失败（不阻塞）: %s", e)
            self._active = False

    async def _emit_failed(self, turn_id: str, error: str) -> None:
        """emit 失败事件。"""
        await self.event_sink(DagEvent(
            type=DagEventType.TURN_FAILED,
            run_id=self.session_id,
            node_id=f"conv:{self.agent_id}",
            payload={"turn_id": turn_id, "error": error},
            sequence=0,
        ))
        # 同时 emit 一条 session.dormant 让前端恢复发送按钮
        await self.event_sink(DagEvent(
            type=DagEventType.SESSION_DORMANT,
            run_id=self.session_id,
            payload={"reason": "turn_failed", "error": error},
            sequence=0,
        ))

    async def cancel(self, reason: str = "user_cancelled") -> None:
        """取消当前 turn。"""
        self._cancel.set()
        await self.event_sink(DagEvent(
            type=DagEventType.SESSION_DORMANT,
            run_id=self.session_id,
            payload={"reason": "cancelled"},
            sequence=0,
        ))

    def submit_widget_input(self, widget_id: str, payload: dict) -> None:
        """提交 widget 输入（HIL 表单 / chat_input）。"""
        if widget_id == "chat_input":
            text = payload.get("message", "") if isinstance(payload, dict) else str(payload)
            queue = self._pending_inputs.get("chat_input")
            if queue is None:
                queue = asyncio.Queue()
                self._pending_inputs["chat_input"] = queue
            queue.put_nowait({"message": text})
        else:
            queue = self._pending_inputs.get(widget_id)
            if queue is None:
                queue = asyncio.Queue()
                self._pending_inputs[widget_id] = queue
            queue.put_nowait(payload)
