"""TaskOrchestrator — 任务管理模块调度层（P0 状态推进 + V1 agent 调度/回退/关闭）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.2
- P0：5 态机 + 乐观锁 + 冲突重试一次
- V1：14 态机 + 风险门槛 + execute_coding 派发 + 三级回退 + Closing 硬约束
- p0_mode 切换校验函数（P0/V1 共用一个 orchestrator 类）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from uuid import uuid4

from task.status import (
    TaskStatus,
    # P0 5 态
    can_transition_p0,
    get_p0_allowed_transitions,
    is_p0_terminal,
    # V1 14 态
    can_transition,
    get_allowed_transitions,
    is_terminal,
    resolve_review_gate,
)
from task.store import TaskStore
from task.terminal_exec import get_launch_cmd

logger = logging.getLogger(__name__)

# 合法任务状态值集合（创建任务时校验初始状态用）
_VALID_STATUS_VALUES = {s.value for s in TaskStatus}


class TaskOrchestrator:
    """任务管理模块调度器（P0 + V1）。

    Args:
        task_store: TaskStore 实例
        p0_mode: True=用 P0 5 态机，False=用 V1 14 态机
        style_loader: agent 风格加载器（V1 execute_coding 用，可空）
        terminal_manager: terminal 会话管理器（V1 execute_coding 用，可空）
    """

    def __init__(self, task_store: TaskStore, p0_mode: bool = True,
                 style_loader: Any = None, terminal_manager: Any = None,
                 llm_config: dict | None = None):
        self.store = task_store
        self.p0_mode = p0_mode
        self._styles = style_loader
        self._terminal = terminal_manager
        self._llm_config = llm_config or {}

    # ====== 状态机切换辅助 ======

    def _can_transition(self, frm: str, to: str) -> bool:
        return can_transition_p0(frm, to) if self.p0_mode else can_transition(frm, to)

    def _get_allowed(self, status: str) -> list[tuple]:
        """返回 [(to_value, action, requires_user), ...]。"""
        if self.p0_mode:
            return [(t.value, a, ru) for t, a, ru in get_p0_allowed_transitions(status)]
        return get_allowed_transitions(status)

    def _is_terminal(self, status: str) -> bool:
        return is_p0_terminal(status) if self.p0_mode else is_terminal(status)

    # ====== 状态推进（核心） ======

    async def advance_stage(self, *, task_id: str, target_status: str,
                            if_version: int, actor: str = "user",
                            thread_id: str = "", comment: str = "",
                            stage_output: str = "") -> dict:
        """推进任务状态（核心方法，P0/V1 共用）。

        Args:
            task_id: 任务 ID
            target_status: 目标状态
            if_version: 乐观锁版本号
            actor: 触发者（user/agent），用于 requires_user 校验
            thread_id: 对话线程 ID（透传到 tasks.thread_id）
            comment: 转移备注（存 stage_output）
            stage_output: 阶段产出（可选）

        Returns:
            {"ok": True, "task": {...}, "if_version": int} 或
            {"ok": False, "error": "...", "task": {...}|None}
        """
        # 1. 读当前任务
        task = await self.store.get_task(task_id)
        if not task:
            return {"ok": False, "error": "task_not_found", "task": None}

        current_status = task["status"]

        # 2. 终态校验
        if self._is_terminal(current_status):
            return {"ok": False, "error": "already_terminal",
                    "task": task, "message": f"任务已终态（{current_status}），不可再转移"}

        # 3. 状态机合法性校验
        if not self._can_transition(current_status, target_status):
            allowed = self._get_allowed(current_status)
            allowed_str = ", ".join(f"{t}({a})" for t, a, _ in allowed) or "无"
            return {"ok": False, "error": "illegal_transition", "task": task,
                    "message": f"非法转移：{current_status}→{target_status}。合法目标：{allowed_str}"}

        # 4. requires_user 校验（agent 不能触发用户决策态）
        allowed = self._get_allowed(current_status)
        target_trans = next((tr for tr in allowed if tr[0] == target_status), None)
        if target_trans and target_trans[2] and actor == "agent":
            logger.warning(
                "agent 尝试触发用户决策态转移 %s→%s（requires_user=True），已拒绝。",
                current_status, target_status)
            return {"ok": False, "error": "requires_user_approval", "task": task,
                    "message": f"转移 {current_status}→{target_status} 需用户审批，agent 不可自动触发"}

        # 5. V1 风险门槛（v1.2：门禁挪到 reviewing → backlog 放行边）
        #    high 风险任务 agent 不可自动放行入待办池，必须用户确认
        if (not self.p0_mode
                and target_status == TaskStatus.BACKLOG.value
                and current_status == TaskStatus.REVIEWING.value):
            gate = resolve_review_gate(task.get("risk_level", "medium"))
            if gate == "manual" and actor == "agent":
                return {"ok": False, "error": "review_required_for_high_risk", "task": task,
                        "message": "高风险任务评审必须用户确认放行，agent 不可自动通过"}

        # 6. 乐观锁更新（冲突重试一次）
        updated = await self.store.update_task_status(task_id, target_status, if_version)
        if updated is None:
            latest = await self.store.get_task(task_id)
            if not latest:
                return {"ok": False, "error": "task_not_found_on_retry", "task": None}
            if latest["version"] == if_version:
                return {"ok": False, "error": "update_failed", "task": latest}
            if not self._can_transition(latest["status"], target_status):
                return {"ok": False, "error": "illegal_transition_after_retry",
                        "task": latest,
                        "message": f"重试时发现状态已变为 {latest['status']}，不可转移到 {target_status}"}
            updated = await self.store.update_task_status(
                task_id, target_status, latest["version"])
            if updated is None:
                return {"ok": False, "error": "conflict_retry_failed", "task": latest}

        # 7. 可选：更新 thread_id
        if thread_id and task.get("thread_id") != thread_id:
            await self.store.update_task_fields(
                updated["task_id"], updated["version"], thread_id=thread_id)
            updated = await self.store.get_task(task_id)

        # 8. 写 task_activities 字段级变更（V1 表存在时）
        try:
            await self.store.add_activity(
                task_id=task_id,
                actor_type="user" if actor == "user" else "agent",
                actor_name=actor,
                changes={"status": {"before": current_status, "after": target_status}})
        except Exception as e:
            logger.debug("add_activity 失败（不阻塞主流程）: %s", e)

        # 9. 可选：建 stage 记录
        if comment or stage_output:
            stage_id = f"stage_{task_id}_{updated['version']}"
            try:
                await self.store.create_stage(
                    stage_id=stage_id, task_id=task_id,
                    stage_type=target_status,
                    assigned_agent=actor if actor == "agent" else "",
                    stage_input=comment or "",
                    stage_output=stage_output or "")
                await self.store.commit_stage(stage_id, 0, stage_output or comment or "")
            except Exception as e:
                logger.warning("create_stage 失败（不阻塞主流程）: %s", e)

        # 10. v1.2 级联：父任务评审通过（reviewing→backlog）时，
        #     reviewing 态子任务跟随进待办池（拆分方案整体放行，子任务不单独评审）
        if (not self.p0_mode
                and target_status == TaskStatus.BACKLOG.value
                and current_status == TaskStatus.REVIEWING.value):
            try:
                children = [t for t in await self.store.list_tasks(limit=500)
                            if t.get("parent_task_id") == task_id
                            and t["status"] == TaskStatus.REVIEWING.value]
                for child in children:
                    await self.store.update_task_status(
                        child["task_id"], TaskStatus.BACKLOG.value, child["version"])
                    await self.store.add_activity(
                        task_id=child["task_id"],
                        actor_type="system", actor_name="cascade",
                        changes={"status": {"before": TaskStatus.REVIEWING.value,
                                            "after": TaskStatus.BACKLOG.value},
                                 "note": "父任务评审通过，子任务级联入待办池"})
                if children:
                    logger.info("父任务 %s 评审通过，%d 个子任务级联入待办池",
                                task_id, len(children))
            except Exception as e:
                logger.warning("子任务级联推进失败（不阻塞主流程）: %s", e)

        return {"ok": True, "task": updated, "if_version": updated["version"]}

    # ============================================================
    # V1 显式 agent 调度（p0_mode=False 时可用）
    # ============================================================

    async def execute_coding(self, task_id: str, style_id: str = "default",
                             if_version: int = 0, harness: str = "claude_code",
                             exec_mode: str = "terminal") -> dict:
        """派发 coding_agent 执行（绑定 terminal 会话 + workspace 沙箱）。

        流程：
        1. 校验任务处于 in_progress
        2. 校验 project 已挂接 authorized_workspaces
        3. 创建/复用 terminal session（subprocess 后端 cwd=workspace；
           terminal 模式 conpty_host 带 command 直跑 TUI）
        4. 装配 prompt（基础铁律 + 任务上下文 + 风格 overlay）
        5. 派发 coding_agent：
           - exec_mode=terminal（默认）：ConPTY/psmux/tmux 直跑原生 claude/codex
             TUI（DESIGN_terminal_native_execution.md，用户可实时观看 + 介入）
           - exec_mode=tee 或后端不支持：HarnessRegistry 后台子进程 + 事件流 tee
        6. terminal 写启动横幅（append_output，真实 shell 不当命令执行）
        7. 弱关联 task_run
        8. 记录 task_activities
        """
        task = await self.store.get_task(task_id)
        if not task:
            return {"ok": False, "error": "task_not_found"}

        # 友好 UX：backlog 态点「执行编码」= 用户主动推进意图 → 自动转移
        # （backlog→in_progress 是 v1.2 主线非 requires_user 转移，actor="user"）
        if task["status"] == TaskStatus.BACKLOG.value:
            r = await self.advance_stage(
                task_id=task_id, target_status=TaskStatus.IN_PROGRESS.value,
                if_version=task["version"], actor="user",
                comment="触发执行编码，自动从 backlog 推进")
            if not r.get("ok"):
                return {"ok": False, "error": r["error"],
                        "hint": f"自动推进 backlog→in_progress 失败: "
                                f"{r.get('message', '')}"}
            task = r["task"]  # 重读最新版本（含新 version）

        if task["status"] != TaskStatus.IN_PROGRESS.value:
            return {"ok": False, "error": "task_not_in_progress",
                    "hint": f"当前状态 {task['status']}，需先推进到 in_progress"}

        project = await self.store.get_project(task["project_id"])
        if not project:
            return {"ok": False, "error": "project_not_found"}
        workspace_id = project.get("workspace_id") or ""
        # workspace 无效时回退服务端 cwd（终端 cwd 与 agent 子进程 cwd 一致）
        workspace_path = workspace_id if workspace_id and os.path.isdir(workspace_id) \
            else os.getcwd()

        # terminal 模式仅真实多路复用后端可用（subprocess/mock 无 TUI 能力 → 降级 tee）
        backend_name = getattr(self._terminal, "backend_name", "mock") \
            if self._terminal is not None else "mock"
        native_mode = (exec_mode == "terminal"
                       and backend_name in ("conpty_host", "psmux", "tmux"))

        # 1. 创建/复用 terminal session
        terminal_id = ""
        if self._terminal is not None:
            launch_cmd = get_launch_cmd()
            if native_mode and backend_name == "conpty_host" \
                    and harness in launch_cmd:
                # ConPTY：command 直跑原生 TUI（一次创建带 command，避免多余 shell 横幅）
                terminal_id = await self._terminal.create_session(
                    f"task_{task_id}", cwd=workspace_path,
                    command=launch_cmd[harness])
            else:
                terminal_id = await self._terminal.create_session(
                    f"task_{task_id}", cwd=workspace_path)
            # 回写 terminal_session_id 到任务
            cur_ver = task["version"]
            await self.store.update_task_fields(
                task_id, cur_ver, terminal_session_id=terminal_id)
            # V3：注册终端会话（Coding 终端页自动上屏依据，§4.13.4）
            try:
                await self.store.register_terminal_session(
                    terminal_session_id=terminal_id, task_id=task_id,
                    kind="agent")
            except Exception as e:
                logger.debug("register_terminal_session 失败（不阻塞主流程）: %s", e)

        # 2. 装配 prompt
        style_overlay = ""
        if self._styles is not None:
            style_overlay = await self._styles.get_overlay(style_id)
        system_prompt = await self._build_coding_prompt(task, project, style_overlay)

        # 3. 派发 coding_agent（terminal：真 PTY 直跑 TUI；tee：后台子进程事件流）
        if native_mode and harness in ("claude_code", "codex"):
            dispatch = await self._dispatch_terminal_agent(
                task=task, harness=harness, terminal_id=terminal_id,
                workspace=workspace_path, system_prompt=system_prompt)
            effective_mode = "terminal"
        else:
            dispatch = await self._dispatch_coding_agent(
                agent_id="coding_agent", workspace_id=workspace_path,
                terminal_id=terminal_id, system_prompt=system_prompt, task=task,
                harness=harness)
            effective_mode = "tee"

        # 4. terminal 面板写入启动横幅（append_output：tee 文本不进 shell）
        if self._terminal is not None and terminal_id:
            await self._terminal.append_output(
                terminal_id,
                f"[task:{task.get('identifier') or task_id}] "
                f"coding_agent({harness}) dispatched (run={dispatch.get('run_id')}"
                f" mode={effective_mode})")

        # 5. 弱关联 task_run（run_id/session_id 不在 runs/sessions 表中，且 FK 约束
        #    强制开启，一律传空；run_id 已记入 dispatch activity；terminal 真实关联）
        await self.store.link_task_run(
            task_id=task_id, role="main_execution",
            run_id="", session_id="", terminal_session_id=terminal_id)

        # 6. 记录 task_activities
        try:
            await self.store.add_activity(
                task_id=task_id, actor_type="agent", actor_name="coding_agent",
                changes={"dispatch": {"after": {
                    "style_id": style_id, "workspace_id": workspace_path,
                    "harness": harness,
                    "run_id": dispatch.get("run_id"), "mock": dispatch.get("mock", False),
                    "exec_mode": effective_mode}}})
        except Exception as e:
            logger.debug("add_activity 失败: %s", e)

        return {"ok": True, "terminal_session_id": terminal_id, "style_id": style_id,
                "workspace_id": workspace_path, "run_id": dispatch.get("run_id"),
                "session_id": dispatch.get("session_id"), "mock": dispatch.get("mock", False),
                "harness": harness, "exec_mode": effective_mode, "task": task}

    async def _dispatch_terminal_agent(self, *, task: dict, harness: str,
                                        terminal_id: str, workspace: str,
                                        system_prompt: str) -> dict:
        """terminal 模式派发：后台 TerminalExecDriver 驱动原生 TUI 执行。

        - 会话已在 execute_coding 创建（conpty_host 带 command 直跑 TUI；
          psmux/tmux 为 shell，driver 内 send_keys 启动 CLI）
        - ready/trust 检测 → 长 prompt 落文件 + 短指令注入 → 完成监测
          → transcript 提取 → 复用 _finalize_execution 闭环
        """
        from task.terminal_exec import TerminalExecDriver

        run_id = f"run_{uuid4().hex[:12]}"
        driver = TerminalExecDriver(self._terminal, self.store)
        asyncio.create_task(driver.drive(
            orchestrator=self, terminal_id=terminal_id, task=task,
            harness=harness, workspace=workspace, system_prompt=system_prompt,
            run_id=run_id))
        return {"run_id": run_id, "mock": False}

    async def _build_coding_prompt(self, task: dict, project: dict,
                                   style_overlay: str) -> str:
        """装配 coding_agent 的 system prompt：基础铁律 + 任务上下文 + 风格 overlay。"""
        criteria = await self.store.list_criteria(task["task_id"])
        comments = await self.store.list_comments(task["task_id"])
        ctx = [
            f"# 任务\n- identifier: {task.get('identifier')}\n- 标题: {task['title']}",
            f"- 描述: {task.get('description', '')}",
            f"- 风险等级: {task.get('risk_level')} | 类型: {task.get('task_type')}",
            f"# 工作区\n- workspace_id: {project.get('workspace_id')}\n"
            f"- local_path: {project.get('local_path')}",
        ]
        if criteria:
            ctx.append("# 验收标准\n" + "\n".join(
                f"- [{c['check_type']}] {c['description']}" for c in criteria))
        if comments:
            # 取最近 5 条
            recent = comments[-5:]
            ctx.append("# 最近评论（视为当前需求）\n" + "\n".join(
                f"- {c.get('author_name', '?')}: {c['body']}" for c in recent))
        # v1.1 硬性设计笔记规则（生命周期自动化 §5.3.1：记忆是执行中的义务产出）
        ctx.append(
            "# 设计笔记（硬性要求）\n"
            "每一个非琐碎变更，必须在最终输出的「## 设计笔记」段落中添加至少一条笔记；"
            "只有纯粹的机械性编辑（格式化、重命名、纯文案调整）可以豁免。\n"
            "每条笔记格式：`- [模块/文件] 做了什么：...；为什么：...`（缺「为什么」不合格）。\n"
            "未遵守视为交付不完整。")
        return "\n\n".join(ctx) + ("\n\n" + style_overlay if style_overlay else "")

    async def _dispatch_coding_agent(self, agent_id: str, workspace_id: str,
                                     terminal_id: str, system_prompt: str,
                                     task: dict, harness: str = "claude_code") -> dict:
        """经 HarnessRegistry 派发真实 coding agent（claude CLI / codex app-server）。

        - claude_code：ClaudeCodeClient 起 claude CLI 子进程（--print stream-json）
        - codex：CodexAppServerClient 经 codex app-server（JSON-RPC）
        - 事件流由后台协程消费并 tee 到 terminal scrollback（Coding 终端页实时可观测）
        - terminal 为 mock 后端（单测）或 HarnessRegistry 不可用时回退 mock
        """
        run_id = f"run_{uuid4().hex[:12]}"
        session_id = f"tsess_{uuid4().hex[:10]}"
        # terminal 为 mock 后端（单测）时派发也走 mock，保证测试确定性
        term_backend = getattr(self._terminal, "backend_name", "mock")
        if term_backend == "mock":
            tid = task["task_id"][:8]
            return {"run_id": f"mock_run_{tid}", "session_id": f"mock_session_{tid}",
                    "mock": True}
        try:
            from harness import AgentRunContext, HarnessRegistry, HarnessType
            ht = HarnessType.CODEX if harness == "codex" else HarnessType.CLAUDE_CODE
            client = HarnessRegistry.create(ht, timeout=3600)
            # workspace：无效路径回退服务端 cwd，避免子进程 spawn 失败
            ws = workspace_id if workspace_id and os.path.isdir(workspace_id) \
                else os.getcwd()
            context = AgentRunContext(
                system_prompt=system_prompt, model="", api_key="", base_url="",
                workspace=ws, session_id=session_id,
                protocol="openai_compatible" if ht is HarnessType.CODEX
                         else "anthropic_compatible")
            user_prompt = f"执行任务：{task['title']}"
            if task.get("description"):
                user_prompt += f"\n\n{task['description']}"
            asyncio.create_task(self._run_agent_and_tee(
                client=client, prompt=user_prompt, context=context,
                terminal_id=terminal_id, task=task, harness=ht.value, run_id=run_id))
            return {"run_id": run_id, "session_id": session_id, "mock": False}
        except Exception as e:
            logger.warning("真实 harness 派发失败，回退 mock: %s", e)
            tid = task["task_id"][:8]
            return {"run_id": f"mock_run_{tid}", "session_id": f"mock_session_{tid}",
                    "mock": True}

    async def _run_agent_and_tee(self, *, client, prompt: str, context,
                                 terminal_id: str, task: dict, harness: str,
                                 run_id: str) -> None:
        """后台消费 agent 事件流：tee 到终端 scrollback，完成后写 task activity。"""
        from harness import AgentEventType

        tee = self._terminal
        final_text = ""
        tokens_in = tokens_out = 0

        async def _out(text: str) -> None:
            if tee is not None and terminal_id:
                try:
                    await tee.append_output(terminal_id, text)
                except Exception:
                    pass

        try:
            await _out(f"[{harness}] run={run_id} 启动 · workspace={context.workspace}")
            async for ev in client.run(prompt, [], context):
                et = getattr(ev.type, "value", ev.type)
                if et == AgentEventType.TEXT.value and ev.text:
                    final_text += ev.text
                    await _out(ev.text)
                elif et == AgentEventType.THINKING.value and ev.text:
                    await _out(f"[thinking] {ev.text[:300]}")
                elif et == AgentEventType.TOOL_USE.value:
                    await _out(f"[tool] {ev.tool_name} "
                               f"{json.dumps(ev.tool_input, ensure_ascii=False)[:300]}")
                elif et == AgentEventType.USAGE.value and ev.usage:
                    tokens_in = max(tokens_in, ev.usage.input_tokens)
                    tokens_out = max(tokens_out, ev.usage.output_tokens)
                elif et == AgentEventType.ERROR.value:
                    await _out(f"[error] {ev.error_message}")
            await _out(f"[{harness}] 完成 · tokens_in={tokens_in} tokens_out={tokens_out}")
            try:
                await self.store.add_activity(
                    task_id=task["task_id"], actor_type="agent",
                    actor_name=f"{harness}_agent",
                    changes={"run_complete": {"after": {
                        "run_id": run_id, "harness": harness,
                        "tokens_in": tokens_in, "tokens_out": tokens_out,
                        "summary": (final_text or "")[:500]}}})
            except Exception as e:
                logger.debug("run_complete activity 写入失败: %s", e)
            # v1.1 执行闭环（生命周期自动化 §5.7）：
            # 笔记提取 → 总结报告 → 评论区发布 → 自动转 validating
            try:
                await self._finalize_execution(
                    task=task, harness=harness, run_id=run_id,
                    final_text=final_text, tokens_in=tokens_in,
                    tokens_out=tokens_out, out=_out)
            except Exception as e:
                logger.warning("执行闭环收尾失败（不阻塞 run 结果）: %s", e)
        except Exception as e:
            logger.warning("agent run 后台协程异常: %s", e)
            await _out(f"[error] agent run 异常: {e}")

    # ============================================================
    # v1.1 执行闭环：笔记提取 + 总结报告 + 状态推进（§5.7/§5.3.1）
    # ============================================================

    @staticmethod
    def _extract_design_notes(final_text: str) -> tuple[list[str], str]:
        """从 agent 最终输出提取「## 设计笔记」段落条目。

        Returns: (笔记条目列表, 去除笔记段后的正文)
        """
        import re as _re
        m = _re.search(r"^##\s*设计笔记\s*$([\s\S]*?)(?=^##\s|\Z)",
                       final_text, _re.MULTILINE)
        if not m:
            return [], final_text
        section = m.group(1)
        notes = [ln.lstrip("- ").strip() for ln in section.splitlines()
                 if ln.strip().startswith("-")]
        body = final_text[:m.start()] + final_text[m.end():]
        return [n for n in notes if n], body.strip()

    async def _finalize_execution(self, *, task: dict, harness: str,
                                  run_id: str, final_text: str,
                                  tokens_in: int, tokens_out: int,
                                  out=None) -> None:
        """run 完成后的收尾：笔记落库 → 报告落库 → 评论区发布 → 转 validating。"""
        task_id = task["task_id"]
        agent_name = f"{harness}_agent"

        # 1. 提取设计笔记（proposed 态落库）
        notes, body = self._extract_design_notes(final_text or "")
        for note in notes[:20]:  # 上限防滥用
            try:
                await self.store.add_design_note(
                    task_id=task_id, project_id=task["project_id"],
                    content=note, status="proposed", source_run=run_id)
            except Exception as e:
                logger.debug("design note 落库失败: %s", e)

        # 2. 组装总结报告（结构化模板，报告主体引用 agent 输出）
        notes_section = ("\n".join(f"- {n}" for n in notes)) if notes else \
            "（本次执行未产出设计笔记——非琐碎变更缺失笔记属交付不完整，验收时请关注）"
        report_md = (
            f"# 执行总结报告\n\n"
            f"- 任务：{task.get('identifier') or task_id} {task['title']}\n"
            f"- 执行者：{agent_name}（run={run_id}）\n"
            f"- token：in={tokens_in} out={tokens_out}\n\n"
            f"## 执行输出\n\n{(body or final_text or '（无文本输出）')[:4000]}\n\n"
            f"## 设计笔记（{len(notes)} 条 · proposed 待验收晋升）\n\n{notes_section}")
        report = await self.store.submit_report(
            task_id=task_id, agent_id=agent_name, content=report_md,
            terminal_session_id=task.get("terminal_session_id") or "",
            self_check={"notes_count": len(notes),
                        "notes_missing": len(notes) == 0})

        # 3. 报告发布到评论区（report 类型，@用户提醒验收）
        preview = report_md[:1200] + ("\n\n…（完整报告见报告页）" if len(report_md) > 1200 else "")
        try:
            await self.store.add_comment(
                task_id=task_id, body=preview, author_type="agent",
                author_name=agent_name, comment_type="report",
                report_id=(report or {}).get("report_id", ""))
        except Exception as e:
            logger.debug("报告评论发布失败: %s", e)

        # 4. 自动转 validating（in_progress→validating requires_user=False）
        fresh = await self.store.get_task(task_id)
        if fresh and fresh["status"] == TaskStatus.IN_PROGRESS.value:
            res = await self.advance_stage(
                task_id=task_id, target_status=TaskStatus.VALIDATING.value,
                if_version=fresh["version"], actor="agent",
                comment=f"run={run_id} 完成，报告已生成，进入验收",
                stage_output=report_md[:2000])
            if out and res.get("ok"):
                await out(f"[task:{task.get('identifier') or task_id}] "
                          f"报告已生成（notes={len(notes)}），任务转入 validating")
            elif out:
                await out(f"[task:{task.get('identifier') or task_id}] "
                          f"转 validating 失败: {res.get('error')}")

    # ============================================================
    # V3：@agent 评论交互（设计文档 §4.12）
    # ============================================================

    async def respond_to_mention(self, task_id: str, agent_id: str,
                                 question: str) -> dict:
        """任务详情页评论区被 @ 后生成 agent 回复并落库（任意阶段可用）。

        流程（对齐 skills/task-card-execution/SKILL.md 动作序列）：
        1. 读任务卡 → 2. 查关系（父/子/blocked_by）→ 3. 查关联文档索引
        → 4. 装配上下文 → 5. 生成回复（harness 可用时走 LLM，否则结构化模板）
        → 6. 以 author_type=agent 写 task_comments

        Returns: {"ok": True, "comment": {...}} 或 {"ok": False, "error": "..."}
        """
        task = await self.store.get_task(task_id)
        if not task:
            return {"ok": False, "error": "task_not_found"}

        # 1-3. 装配上下文（关系 + 文档索引 + 最近评论）
        relations = await self.store.list_relations(task_id)
        blockers = await self.store.list_blocked_by(task_id)
        docs = []
        try:
            all_docs = await self.store.list_docs(task["project_id"])
            docs = all_docs[:5]
        except Exception as e:
            logger.debug("list_docs 失败（不阻塞回复）: %s", e)
        comments = (await self.store.list_comments(task_id))[-20:]

        parent_id = task.get("parent_task_id") or ""
        parent = parent_id and await self.store.get_task(parent_id) or None
        children = []
        for r in relations:
            if r.get("relation_type") == "parent_child" and r.get("source_task_id") == task_id:
                child = await self.store.get_task(r["target_task_id"])
                if child:
                    children.append(child)

        # 5. 生成回复：优先 harness（LLM），失败回退结构化模板（真实数据，不臆造）
        reply = await self._generate_mention_reply(
            agent_id=agent_id, question=question, task=task,
            parent=parent, children=children, blockers=blockers,
            docs=docs, comments=comments)

        # 6. 落库（author_type=agent，前端渲染 agent 回复卡片）
        comment = await self.store.add_comment(
            task_id=task_id, body=reply,
            author_type="agent", author_id=agent_id, author_name=agent_id,
            comment_type="discussion")
        return {"ok": True, "comment": comment}

    async def _generate_mention_reply(self, *, agent_id: str, question: str,
                                      task: dict, parent: dict | None,
                                      children: list[dict], blockers: list[dict],
                                      docs: list[dict], comments: list[dict]) -> str:
        """生成 @ 提问回复：harness 可用走 LLM，否则基于真实数据拼结构化回复。"""
        # 上下文装配（§4.12.2：后端拼装，防 agent 盲查）
        ctx_lines = [
            f"# 任务卡片\n- identifier: {task.get('identifier')}\n- 标题: {task['title']}",
            f"- 状态: {task['status']} | 风险: {task.get('risk_level')} | 类型: {task.get('task_type')}",
            f"- 描述: {task.get('description', '') or '（空）'}",
        ]
        if parent:
            ctx_lines.append(f"# 父任务\n- {parent.get('identifier')}: {parent['title']}"
                             f"（{parent['status']}）")
        if children:
            ctx_lines.append("# 子任务\n" + "\n".join(
                f"- {c.get('identifier')}: {c['title']}（{c['status']}）" for c in children))
        if blockers:
            ctx_lines.append("# 上游阻塞（blocked_by）\n" + "\n".join(
                f"- {b.get('identifier') or b.get('task_id', '')[:12]}"
                f"（{b.get('status', '?')}）" for b in blockers))
        else:
            ctx_lines.append("# 上游阻塞（blocked_by）\n- 无")
        if docs:
            ctx_lines.append("# 关联文档索引\n" + "\n".join(
                f"- {d.get('title')}（{d.get('path', '')}）" for d in docs))
        else:
            ctx_lines.append("# 关联文档索引\n- 无关联方案文档（建议关联）")
        if comments:
            ctx_lines.append("# 最近评论\n" + "\n".join(
                f"- {c.get('author_name', '?')}: {c['body'][:120]}" for c in comments[-8:]))
        context = "\n\n".join(ctx_lines)

        skill_instructions = (
            "你被 @ 提问。按以下要求回复：结论 → 依据（引用任务字段/关系/文档）→ 建议动作。"
            "只给建议不改任务状态；数据获取只走已装配上下文，禁止臆造任务数据；一次 @ 一回复。")

        # LLM 回复：LocalLlmClient（openai_compatible /chat/completions）；
        # 配置缺失或调用失败回退结构化模板（真实数据，不臆造）
        if self._llm_config.get("base_url") and self._llm_config.get("api_key"):
            try:
                from harness.local_llm import LocalLlmClient
                from harness.protocol import AgentRunContext, AgentEventType

                client = LocalLlmClient(
                    base_url=self._llm_config.get("base_url", ""),
                    api_key=self._llm_config.get("api_key", ""),
                    model=self._llm_config.get("model", ""),
                    timeout=120.0,
                )
                context_obj = AgentRunContext(
                    system_prompt=f"你是 {agent_id}（任务管理模块 agent）。{skill_instructions}",
                    model=self._llm_config.get("model", ""),
                    api_key=self._llm_config.get("api_key", ""),
                    base_url=self._llm_config.get("base_url", ""),
                    workspace="",
                    session_id=f"mention-{task.get('identifier') or task.get('task_id', '')[:16]}",
                    protocol="openai_compatible",
                )
                parts: list[str] = []
                async for event in client.run(
                        f"{context}\n\n# 用户提问\n{question}", [], context_obj):
                    if event.type == AgentEventType.TEXT and event.text:
                        parts.append(event.text)
                if parts:
                    import re as _re
                    text = "".join(parts).strip()
                    # 规范化 <think> 块：未闭合的补上 </think>（防前端解析错乱）。
                    # 思考过程保留在正文中，由前端折叠展示（可展开查看全量）。
                    if "<think>" in text and "</think>" not in text:
                        text = text + "\n</think>"
                    if text:
                        return text
                logger.warning("mention reply LLM 无输出 (%s)，回退模板", agent_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("mention reply LLM 调用失败 (%s)，回退模板: %s", agent_id, e)
        else:
            logger.warning("mention reply LLM 未配置（base_url/api_key 为空），回退模板")

        # 结构化模板回复（真实数据，无 LLM 依赖）
        gap_items = []
        if not task.get("description"):
            gap_items.append("任务描述为空，建议补充背景与目标")
        if not children and not parent:
            gap_items.append("无父子任务关系，如需拆分建议建立子任务")
        if not blockers:
            upstream = "无上游阻塞，可直接推进"
        else:
            pending = [b for b in blockers if b.get("status") not in ("closed", "done")]
            upstream = (f"{len(pending)} 个上游任务未完成（"
                        + ", ".join(b.get("identifier", "?") for b in pending) + "）" if pending
                        else "上游任务均已完成")
        doc_hint = (f"已关联 {len(docs)} 份文档" if docs else "无关联方案文档，建议关联")
        suggestions = []
        if task["status"] == "idea":
            suggestions.append("当前在 idea 阶段，建议先补充描述再推进 backlog")
        elif task["status"] == "backlog":
            suggestions.append("可推进到 discussing 进行方案讨论")
        elif task["status"] == "discussing":
            suggestions.append("讨论充分后建议建立子任务/依赖关系再推进")
        elif task["status"] == "in_progress":
            suggestions.append("执行中可通过详情页底部终端观察 coding 过程")
        if pending_blockers := [b for b in blockers if b.get("status") not in ("closed", "done")]:
            suggestions.append(f"建议先处理上游阻塞：{pending_blockers[0].get('identifier', '?')}")

        return (
            f"**结论**：已读取任务卡与关系信息，针对「{question[:80]}」给出以下分析。\n\n"
            f"**依据**：\n"
            f"- 任务状态：{task['status']}（v{task['version']}），风险 {task.get('risk_level')}\n"
            f"- 依赖：{upstream}\n"
            f"- 文档：{doc_hint}\n"
            + (f"- 信息缺口：{'；'.join(gap_items)}\n" if gap_items else "")
            + f"\n**建议动作**：\n"
            + ("\n".join(f"- {s}" for s in suggestions) if suggestions else "- 暂无额外建议，按当前计划推进")
            + f"\n\n（由 {agent_id} 基于 skills/task-card-execution 生成；仅提供建议，不修改任务状态）")

    # ============================================================
    # V1 验收回退（三级，用户决策）
    # ============================================================

    async def rollback_task(self, task_id: str, rollback_target: str = "",
                            if_version: int = 0, comment: str = "",
                            target_status: str = "") -> dict:
        """用户决定退回到指定阶段。

        两种调用方式（target_status 优先）：
        1. 精确回退：传 target_status（in_progress/decomposing/discussing/reviewing/backlog/idea 等），
           直接走 advance_stage 状态机校验合法性（合法转移表 TRANSITIONS 已覆盖所有"退回"边）。
        2. 三级别名（向后兼容）：rollback_target=local|partial|global
           local   → in_progress（仅回退最近一步）
           partial → decomposing（回退到拆解）
           global  → discussing（回到方案讨论）

        Args:
            task_id: 任务 ID
            rollback_target: local|partial|global（旧别名，与 target_status 二选一）
            if_version: 乐观锁版本号
            comment: 退回备注
            target_status: 直接指定目标阶段（优先于 rollback_target）
        """
        target_map = {
            "local":   TaskStatus.IN_PROGRESS.value,
            "partial": TaskStatus.DECOMPOSING.value,
            "global":  TaskStatus.DISCUSSING.value,
        }

        # 解析目标阶段：target_status 优先，rollback_target 别名 fallback
        if target_status:
            target = target_status
            # 顺手归一化 rollback_target（评论记录用），若未提供别名则按 target 反查
            rollback_target = rollback_target or next(
                (k for k, v in target_map.items() if v == target), "")
        else:
            target = target_map.get(rollback_target)
            if not target:
                return {"ok": False, "error": "invalid_rollback_target",
                        "hint": "rollback_target 必须是 local/partial/global，或直接提供 target_status"}

        # 记录退回评论（comment_type=review, decision=request_changes）
        # 注：CHECK 约束 (decision IN ('approve','request_changes') OR decision IS NULL) 不允许 'reject'，
        # 前端历史代码误传 'reject'，此处统一规范化为 'request_changes'（与设计文档 §4.5.3 一致）
        await self.store.add_comment(
            task_id=task_id,
            body=comment or f"退回：{rollback_target or target}",
            author_type="user", comment_type="review",
            decision="request_changes",
            rollback_target=rollback_target or target)

        return await self.advance_stage(
            task_id=task_id, target_status=target, if_version=if_version,
            actor="user", comment=comment)

    # ============================================================
    # V1 Closing 硬约束检查
    # ============================================================

    async def close_task(self, task_id: str, if_version: int) -> dict:
        """关闭前硬约束检查（全部满足才 closed）：
        1. 交付物验收通过（acceptance_criteria 全部 passed）
        2. 受影响设计文档已回写（doc_change_proposals 无 pending）
        3. 关联任务/依赖已标注（task_relations 存在或明确声明无）

        流程：硬约束检查通过后，validating → closing → closed 两步推进。
        """
        task = await self.store.get_task(task_id)
        if not task:
            return {"ok": False, "error": "task_not_found"}

        # 1. 验收标准（无标准时视为通过；有标准则必须全 passed）
        criteria = await self.store.list_criteria(task_id)
        if criteria and any(c["status"] != "passed" for c in criteria):
            not_passed = [c["criteria_id"] for c in criteria if c["status"] != "passed"]
            return {"ok": False, "error": "acceptance_not_passed",
                    "detail": not_passed,
                    "message": "存在未通过的验收标准"}

        # 2. 文档回写（该任务关联的 pending 提案必须为空）
        pending_proposals = await self.store.list_doc_proposals(
            task_id=task_id, status="pending")
        if pending_proposals:
            return {"ok": False, "error": "doc_proposals_pending",
                    "detail": [p["proposal_id"] for p in pending_proposals],
                    "message": "存在未处理的文档变更提案"}

        # 3. 关联标注（软约束：有 parent 或至少一条 relation）
        #    P0/V1 从简：跳过此项（设计文档标注 V1 校验，但当前无强制字段）
        #    若需启用，查 task_relations WHERE source_task_id=? OR target_task_id=?

        # 两步推进：validating → closing → closed
        # 若当前已在 closing，直接推进到 closed
        if task["status"] == TaskStatus.CLOSING.value:
            return await self.advance_stage(
                task_id=task_id, target_status=TaskStatus.CLOSED.value,
                if_version=if_version, actor="user")

        # 若当前在 validating，先推进到 closing
        step1 = await self.advance_stage(
            task_id=task_id, target_status=TaskStatus.CLOSING.value,
            if_version=if_version, actor="user")
        if not step1["ok"]:
            return step1
        return await self.advance_stage(
            task_id=task_id, target_status=TaskStatus.CLOSED.value,
            if_version=step1["task"]["version"], actor="user")

    # ====== P0 便捷方法（保留向后兼容） ======

    async def submit_idea(self, *, task_id: str, project_id: str, title: str,
                          description: str = "", thread_id: str = "",
                          creator_id: str = "", creator_name: str = "",
                          risk_level: str = "medium", status: str = "") -> dict:
        """建任务（默认 status=idea；可指定合法初始状态）。"""
        initial_status = status if status and status in _VALID_STATUS_VALUES \
            else TaskStatus.IDEA.value
        identifier, _ = await self.store.alloc_task_number(project_id)
        task = await self.store.create_task(
            task_id=task_id, project_id=project_id, title=title,
            description=description, status=initial_status,
            risk_level=risk_level, creator_type="user" if creator_id else "agent",
            creator_id=creator_id, creator_name=creator_name,
            thread_id=thread_id, identifier=identifier)
        return {"ok": True, "task": task, "if_version": task["version"]}

    async def create_project(self, *, project_id: str, name: str, type: str = "code",
                             local_path: str = "", workspace_id: str = "") -> dict:
        """建项目。"""
        project = await self.store.create_project(
            project_id=project_id, name=name, type=type,
            local_path=local_path, workspace_id=workspace_id)
        return {"ok": True, "project": project}

    async def list_tasks(self, project_id: str = "") -> dict:
        """查任务列表。"""
        tasks = await self.store.list_tasks(project_id=project_id)
        return {"ok": True, "tasks": tasks}

    async def get_transitions(self, status: str) -> list[dict]:
        """查合法转移目标（供前端禁用非法列）。"""
        trans = self._get_allowed(status)
        return [{"to": t, "action": a, "requires_user": ru} for t, a, ru in trans]
