"""会话工具集与 <tool_call> 文本解析（共享层）。

从已删除的 orchestrator/conversational.py 抽取，现由 SessionEngine 及 workflow/engine.py（DAG 节点）共用。
包含：ConversationState、make_conversational_tools、_extract_and_run_tool_calls 及内部依赖 helpers。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from harness import ToolDefinition
from orchestrator.protocol import DagEvent, DagEventType
from orchestrator.present_content import make_present_content_tool

logger = logging.getLogger(__name__)

# 事件 sink 类型：与 DagEngine 一致
EventSink = Callable[[DagEvent], Awaitable[None]]

@dataclass
class ConversationState:
    """对话引擎（SessionEngine / DAG 节点）的运行时状态。"""
    run_id: str
    agent_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)   # 对话历史
    todos: list[dict[str, Any]] = field(default_factory=list)      # task 模式 todo 列表
    emitted_widgets: list[str] = field(default_factory=list)       # 已 emit 的 widget_id
    surface_patch_seq: dict[str, int] = field(default_factory=dict)  # surface_id → 已 emit patch 数（patch）
    waiting_for: str | None = None                                 # 等待哪个 widget 的输入
    should_finalize: bool = False
    final_summary: str = ""
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    turn_count: int = 0
    max_turns: int = 50                                            # 轮次上限


def make_conversational_tools(
    state: ConversationState,
    event_sink: EventSink,
    agent_id: str | None = None,
    coordinator: "CrossDomainCoordinator | None" = None,
    parent_run_id: str = "",
) -> list[ToolDefinition]:
    """构造对话引擎专属工具集。

    三层来源（按顺序合并 + 去重 + allowed_tools 过滤）：
    1. 内置工具（make_conversational_tools 内联定义的）——present_content / todo / finalize / request_human_input
       这些是对话引擎主循环强依赖的，每个 agent 都必须有
    2. config/tools/*.yaml 定义的工具（log_query/wecom_notify/trigger_workflow 等）
       走 ToolConfig.handler_module 动态 import + handler_function 反射调用
    3. opencode harness 内置工具（read_file/write_file/bash）——handler=None，由 harness 原生执行

    agent_id 必传：根据 agent.yaml 的 allowed_tools 过滤工具集（Manager 看到 trigger_workflow，
    普通子 agent 看不到；权限最小化原则）。

    P0-2 新增参数：
      coordinator: 跨域协调器。非 None 时，request_cross_domain 工具的 handler 由
        coordinator.make_tool_handler(agent_id, parent_run_id) 闭包生成，注入 caller_agent
        和 coordinator 实例，完整走 12 步跨域事件流。None 时回退到 _load_agent_extra_tools
        默认反射装载（bare 函数，fast-fail「跨域协调器未初始化」）。
      parent_run_id: 父 run_id，传给跨域事件用于关联。
    """

    async def todo_handler(args: dict[str, Any]) -> dict[str, Any]:
        """task 模式：维护线性 todo 列表（v2：不再 emit widget，状态供 agent 逻辑；展示走 present_content 上大屏）。"""
        action = args.get("action", "add")
        item = args.get("item", "")
        if action == "add" and item:
            state.todos.append({"item": item, "status": "pending"})
        elif action == "complete" and item:
            for t in state.todos:
                if t["item"] == item:
                    t["status"] = "completed"
        elif action == "skip" and item:
            for t in state.todos:
                if t["item"] == item:
                    t["status"] = "skipped"
        return {"content": f"todo {action} ok", "current_todos": state.todos}

    async def finalize_handler(args: dict[str, Any]) -> dict[str, Any]:
        """Agent 主动结束对话。"""
        state.final_summary = args.get("summary", "")
        state.should_finalize = True
        return {"content": "finalizing"}

    async def request_human_input_handler(args: dict[str, Any]) -> dict[str, Any]:
        """v2：HIL 文本化——不再 emit form widget，仅设 waiting_for 阻塞等用户文本输入。

        agent 应在调用本工具前/同时输出提问文本到对话流。用户文字回复走
        chat_input（api/server.py:5414 widget-input 链路 → SessionEngine hil_queue）。

        防御性 emit：把 prompt 作为 agent_text 事件发到对话流，防止 LLM 没自觉输出
        提问文本时用户看不到任何提示（codex harness 不支持 waiting_for 主循环，
        turn 会直接结束，用户至少能在对话流看到提问内容）。
        """
        prompt = args.get("prompt", "请提供输入")
        widget_id = args.get("widget_id") or f"hil_{state.turn_count}"
        state.waiting_for = widget_id
        logger.info("request_human_input (v2 文本化): widget_id=%s prompt=%s", widget_id, prompt[:80])
        # 防御性 emit prompt 文本到对话流（agent_text 事件）
        from orchestrator.protocol import DagEvent, DagEventType
        await event_sink(DagEvent(
            type=DagEventType.NODE_PROGRESS,
            run_id=state.run_id,
            node_id=f"conv:{state.agent_id}",
            payload={"agent_text": f"📝 {prompt}", "hil_widget_id": widget_id},
            sequence=0,
        ))
        return {
            "content": f"waiting for human input on {widget_id} (text reply via chat)",
            "widget_id": widget_id,
            "prompt": prompt,
        }

    # 第 2 层：从 config/tools/*.yaml 加载 agent.allowed_tools 声明的额外工具
    # （trigger_workflow / log_query / wecom_notify 等）
    # _load_agent_extra_tools 内部已去重 base_tool_names + 反射 import handler
    extras = _load_agent_extra_tools(agent_id)

    # P0-2: request_cross_domain 工具闭包注入 —— 若 coordinator 非空，用闭包替换默认反射 handler
    # 不依赖 yaml 的 handler_module/handler_function，而是 coordinator.make_tool_handler 创建
    # 闭包把 caller_agent + coordinator + parent_run_id 注入；coordinator=None 时跳过，
    # 由 _load_agent_extra_tools 的反射装载保留 fast-fail 行为（向后兼容）
    if coordinator is not None and agent_id:
        try:
            from orchestrator.config_loader import get_system_config
            cfg = get_system_config()
            xcd_cfg = cfg.tools.get("request_cross_domain")
            if xcd_cfg is not None and agent_id in (xcd_cfg.allowed_domains or []):
                # 从 extras 里移除默认反射装载的 request_cross_domain（如果存在）
                extras = [t for t in extras if t.name != "request_cross_domain"]
                # 用 coordinator 闭包构造真正的跨域工具
                xcd_handler = coordinator.make_tool_handler(agent_id, parent_run_id)
                extras.insert(0, ToolDefinition(
                    name="request_cross_domain",
                    description=xcd_cfg.description or xcd_cfg.display_name or "经 Manager 中转的跨业务域请求",
                    input_schema=xcd_cfg.input_schema or {"type": "object"},
                    handler=xcd_handler,
                ))
                logger.debug(
                    "request_cross_domain 工具已注入 coordinator 闭包: agent=%s run_id=%s",
                    agent_id, parent_run_id,
                )
        except Exception as e:
            logger.warning("request_cross_domain 闭包注入失败，保留默认反射 handler: %s", e)

    base_tools = [
        # present_content：高层语义展示工具（主推），Agent 不接触 A2UI 协议
        make_present_content_tool(state, event_sink),
        ToolDefinition(
            name="todo",
            description="task 模式：维护线性 todo 列表。action: add/complete/skip",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "complete", "skip"]},
                    "item": {"type": "string"},
                },
                "required": ["action", "item"],
            },
            handler=todo_handler,
        ),
        ToolDefinition(
            name="finalize",
            description="主动结束对话，可附带总结",
            input_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            },
            handler=finalize_handler,
        ),
        ToolDefinition(
            name="request_human_input",
            description="请求人工输入（HIL），会暂停等待用户提交表单",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "提示用户的问题或上下文"},
                    "widget_id": {"type": "string", "description": "可选，widget 唯一 ID（不传则自动生成 hil_<turn>）"},
                    "fields": {
                        "type": "array",
                        "description": "表单字段列表；每个字段必须包含 label（用户可见的字段名）和 name（提交时的 key）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "字段 key，提交时作为 form data 的字段名"},
                                "label": {"type": "string", "description": "字段显示名称（用户必看的字段名）"},
                                "type": {"type": "string", "enum": ["text", "textarea", "number", "select", "checkbox", "date"]},
                                "required": {"type": "boolean", "default": False},
                                "placeholder": {"type": "string"},
                                "default": {},
                                "options": {
                                    "type": "array",
                                    "description": "select 字段的选项；每项可以是字符串或 {value, label}",
                                },
                            },
                            "required": ["name", "label", "type"],
                        },
                    },
                },
                "required": ["prompt"],
            },
            handler=request_human_input_handler,
        ),
    ]
    # 合并：base + extras（_load_agent_extra_tools 已跳过 base 工具名，不会重复）
    return base_tools + extras


async def _extract_and_run_tool_calls(
    text: str,
    tools: list[ToolDefinition],
    event_sink: Any,
    *,
    session_tier: str = "T3",
    has_workspace: bool = True,
) -> tuple[str, bool]:
    """检测文本中的 <tool_call>...</tool_call> 标记并异步执行对应 handler。

    返回 (processed_text, had_tool_calls)。
    opencode harness 不转发 tools → LLM 用文本模拟 tool call →
    对话引擎在此拦截并实际执行 present_content/finalize/todo 等。

    格式（标准）：<tool_call>{"name":"xxx","arguments":{...}}</tool_call>
    格式（兜底）：裸 JSON 块 {"name":"xxx","arguments":{...}} 或 {"type":"a2ui","props":{...}}
    —— MiniMax M3 / DeepSeek 有时不用 <tool_call> 包裹，直接在文本中输出 JSON。

    P0.18.10: 新增 session_tier / has_workspace 参数用于动态 tier 校验。
    默认 T3 + has_workspace=True（向后兼容：不传则不拦截）。
    """
    import re
    tool_map = {t.name: t for t in tools}
    had_any = False
    result = text

    # 第 1 遍：标准 <tool_call> 格式
    # 注意：不能用 \{.*?\}（非贪婪），嵌套 JSON 会在第一个 } 处截断
    # 直接匹配 <tool_call> 和 </tool_call> 之间的全部内容，交给 json.loads 解析
    pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
    matches = list(pattern.finditer(result))
    for m in reversed(matches):
        ok = await _try_execute_tool_call(
            result, m.start(), m.end(), m.group(1), tool_map,
            session_tier=session_tier, has_workspace=has_workspace,
        )
        if ok:
            had_any = True
            result = result[:m.start()] + ok + result[m.end():]

    # 第 2 遍：兜底检测裸 JSON 块（LLM 不用 <tool_call> 包裹时）
    # 查找 {"name":"xxx","arguments":{...}} 或 {"type":"a2ui","props":{...}} 格式
    # 用宽松匹配：找到顶级 JSON 对象（平衡大括号），尝试解析
    if not had_any:
        result, had_any = await _detect_bare_json_tool_calls(
            result, tool_map,
            getattr(event_sink, '__self__', None) or event_sink,
            session_tier=session_tier, has_workspace=has_workspace,
        )

    return result, had_any


async def _try_execute_tool_call(
    text: str, start: int, end: int, json_str: str,
    tool_map: dict[str, Any],
    *,
    session_tier: str = "T3",
    has_workspace: bool = True,
) -> str | None:
    """尝试解析并执行一个 tool_call JSON。成功返回替换文本，失败返回 None。

    P0.18.10: 执行前先调 check_tool_tier_permission 动态校验 tier。
    校验失败时返回错误提示文本（不抛异常，避免中断主循环）。
    """
    import inspect
    try:
        call_data = json.loads(json_str)
    except json.JSONDecodeError:
        return None
    name = call_data.get("name", "")
    args = call_data.get("arguments", {})
    if not isinstance(args, dict):
        args = {}

    tool_def = tool_map.get(name)
    if not tool_def or not tool_def.handler:
        return None

    # P0.18.10: 动态 tier 校验
    try:
        from orchestrator.workspace_paths import check_tool_tier_permission
        check_tool_tier_permission(
            tool_name=name,
            session_tier=session_tier,
            has_workspace=has_workspace,
        )
    except PermissionError as pe:
        return f"[tool:{name}] 拒绝: {pe}"

    try:
        handler_result = tool_def.handler(args)
        if inspect.iscoroutine(handler_result):
            handler_result = await handler_result
        result_text = json.dumps(handler_result, ensure_ascii=False) if isinstance(handler_result, dict) else str(handler_result)
        return f"[tool:{name}] {result_text}"
    except Exception as e:
        return f"[tool:{name}] 错误: {e}"


async def _detect_bare_json_tool_calls(
    text: str,
    tool_map: dict[str, Any],
    event_sink: Any,
    *,
    session_tier: str = "T3",
    has_workspace: bool = True,
) -> tuple[str, bool]:
    """兜底：检测文本中裸露的 JSON 块（没有 <​tool_call> 包裹），尝试匹配工具调用。

    匹配策略：仅有 name 字段的完整 tool_call JSON（所有 agent 工具走同一路径）：
      {"name":"<tool>","arguments":{...}}

    历史保留：emit_widget(type=a2ui) 隐式调用分支已废弃（emit_widget 工具整体移除，
    v2 后所有展示统一走 present_content）。
    """
    import re

    # 找所有顶级 JSON 对象：用平衡大括号检测
    # 放宽匹配：找至少包含 "name" 字段 + 已知 agent 工具名的 { ... } 块
    bare_pattern = re.compile(
        r'\{\s*"name"\s*:\s*"(?:present_content|todo|finalize|request_human_input)"[^}]*\}',
        re.DOTALL,
    )
    matches = list(bare_pattern.finditer(text))
    if not matches:
        return text, False

    had_any = False
    result = text

    for m in reversed(matches):
        json_str = m.group(0)
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # 尝试用更宽松的解析：找到闭合的大括号
            expanded = _extract_balanced_json(text, m.start())
            if expanded:
                try:
                    data = json.loads(expanded)
                    json_str = expanded
                except json.JSONDecodeError:
                    continue
            else:
                continue

        # 判定 1：有 name 字段 → 显式 tool call
        if "name" in data and "arguments" in data:
            replacement = await _try_execute_tool_call(
                result, m.start(), m.start() + len(json_str), json_str, tool_map,
                session_tier=session_tier, has_workspace=has_workspace,
            )
            if replacement:
                result = result[:m.start()] + replacement + result[m.start() + len(json_str):]
                had_any = True
                continue

        # 判定 2（已废弃）：emit_widget(type=a2ui) 隐式调用分支已删除。
        # 历史背景：DeepSeek 等模型偶发输出 {"type":"a2ui","props":{...}} 风格，
        # v2 改造后 emit_widget 工具整体移除，所有展示统一走 present_content 显式调用。
        # 此类 JSON 块现已被 bare_pattern 排除，落到此处说明 schema 不匹配 → 跳过。

    return result, had_any


def _extract_balanced_json(text: str, start: int) -> str | None:
    """从 start 位置开始提取平衡括号的 JSON 字符串。"""
    if start >= len(text) or text[start] != '{':
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _build_tools_prompt(tools: list[ToolDefinition]) -> str:
    """把 ToolDefinition 列表转为 LLM 可读的工具描述文本。

    opencode harness 不转发 tools 到 opencode server → LLM 不知道有 AgentOps 工具。
    此函数生成 system_prompt 追加段，告诉 LLM 可用工具 + 调用格式，
    对话引擎的 TEXT 后处理会检测 <tool_call> 标记并实际执行。

    格式示例：
      <tool_call>
      {"name": "present_content", "arguments": {"title":"...", "content_type":"table", "data":{...}}}
      </tool_call>
    """
    if not tools:
        return ""
    lines = [
        "## 重要：AgentOps 系统工具（优先级高于 opencode 内置工具）",
        "",
        "你是 AgentOps Manager Agent，运行在 opencode harness 之上。opencode 会暴露自己的内置工具",
        "（bash/edit/glob/grep/read/write/skill/task/webfetch/todowrite/question），但**以下 AgentOps 工具",
        "是你的核心能力**，通过 `<tool_call>` 文本标记调用（不在 opencode 工具列表中，但确实可用）。",
        "",
        "**当用户请求需要 present_content / trigger_workflow / request_cross_domain 等操作时，必须用 <​tool_call> 标记调用，",
        "不要说「工具不在可用工具列表中」或「我无法调用此工具」——这些工具是通过文本标记调用的，不需要在 opencode 工具列表中。**",
        "",
        "调用方式：在回复末尾用 <tool_call>...</tool_call> 包裹 JSON（每段只放一个 tool_call）：",
        "",
    ]
    for t in tools:
        params_desc = ""
        schema = t.input_schema or {}
        if schema:
            required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
            props = schema.get("properties", {})
            param_parts = []
            for name, schema in props.items():
                req_mark = "（必填）" if name in required else "（可选）"
                ptype = schema.get("type", "string")
                pdesc = schema.get("description", "")
                param_parts.append(f"    {name}: {ptype} {req_mark} — {pdesc}" if pdesc else f"    {name}: {ptype} {req_mark}")
            if param_parts:
                params_desc = "\n" + "\n".join(param_parts)
        lines.append(f"### {t.name}")
        lines.append(f"说明：{t.description}")
        if params_desc:
            lines.append(f"参数：{params_desc}")
        lines.append("")
    lines.append("调用格式（严格 JSON，不要多余文字）：")
    lines.append("<tool_call>")
    lines.append('{"name": "<工具名>", "arguments": {<参数>}}')
    lines.append("</tool_call>")
    return "\n".join(lines)


def _load_agent_extra_tools(agent_id: str | None) -> list[ToolDefinition]:
    """从 config.tools 加载 agent.allowed_tools 中声明的额外工具。

    只装载 agent.yaml allowed_tools 显式声明的工具（权限最小化）。
    工具定义在 config/tools/<tool_id>.yaml，通过 ToolConfig.handler_module 反射 import。
    返回的 ToolDefinition 含已绑定的 handler，LLM 可直接调用。

    base 工具名（present_content / todo / finalize / request_human_input）
    已在 base_tools 里内联定义，这里跳过避免重复。
    """
    if not agent_id:
        return []

    try:
        from orchestrator.config_loader import get_system_config
        cfg = get_system_config()
    except Exception as e:
        logger.debug("get_system_config 失败，跳过 config tools 装载: %s", e)
        return []

    agent = cfg.agents.get(agent_id)
    if not agent:
        return []

    # 1. 计算 effective allowed = agent.allowed_tools - agent.denied_tools
    #    域级 allowed_tools 不参与（per-agent 显式声明更严格）
    allowed = set(agent.allowed_tools) - set(agent.denied_tools)

    # 2. 收集 base 工具名（避免重复装载）
    base_tool_names = {
        "present_content", "todo", "finalize", "request_human_input",
    }

    # manager agent 可跨域调度，跳过 allowed_domains 过滤
    agent_domain = agent.domain or ""
    is_manager = agent_id == "manager"

    extras: list[ToolDefinition] = []
    for tool_id in allowed:
        if tool_id in base_tool_names:
            continue  # base 已包含
        tool_cfg = cfg.tools.get(tool_id)
        if not tool_cfg:
            # BUILTIN_TOOLS（bash/read_file/write_file 等）由 harness 自身提供，
            # 这里跳过——opencode/codex harness 收到 tool name 后会自己处理
            continue
        # 域级过滤：tool 的 allowed_domains 非空时，agent 必须匹配
        # manager 豁免（可跨域调度）
        if not is_manager and tool_cfg.allowed_domains:
            if agent_domain not in tool_cfg.allowed_domains and agent_id not in tool_cfg.allowed_domains:
                logger.debug("工具 %s allowed_domains=%s 不匹配 agent %s (domain=%s)，跳过",
                             tool_id, tool_cfg.allowed_domains, agent_id, agent_domain)
                continue
        handler = None
        if tool_cfg.handler_module == "cli":
            # CLI 类型工具（mm_search / mm_speech / mm_image / hyperframes_render ...）：
            # 用 cli_tools dispatcher 工厂包成 async handler，让 CodexAppServerClient /
            # local_llm harness 能直接调。同步子进程通过 asyncio.to_thread 包裹。
            try:
                from tools.cli_tools import sync_cli_handler
                handler = sync_cli_handler(tool_cfg)
            except Exception as e:
                logger.warning("工具 %s cli dispatcher 装载失败: %s", tool_id, e)
                continue
            if handler is None:
                continue
        elif tool_cfg.handler_module and tool_cfg.handler_function:
            # 普通 module:function 处理器
            try:
                import importlib
                mod = importlib.import_module(tool_cfg.handler_module)
                handler = getattr(mod, tool_cfg.handler_function, None)
                if handler is None:
                    logger.warning("工具 %s handler %s.%s 找不到",
                                   tool_id, tool_cfg.handler_module, tool_cfg.handler_function)
                    continue
            except Exception as e:
                logger.warning("工具 %s 装载失败: %s", tool_id, e)
                continue
        else:
            # handler_module/function 都为空 → 旧路径：跳过
            logger.debug("工具 %s 非 module:function handler，对话模式跳过", tool_id)
            continue
        extras.append(ToolDefinition(
            name=tool_cfg.tool_id,
            description=tool_cfg.description or tool_cfg.display_name,
            input_schema=tool_cfg.input_schema or {"type": "object"},
            handler=handler,
        ))

    return extras


def _load_inline_agent_tools(allowed_tools: list[str] | None) -> list[ToolDefinition]:
    """从 config.tools 加载 inline_agent.allowed_tools 中声明的工具。

    与 _load_agent_extra_tools 类似，但不依赖全局 agent 配置，
    直接用 inline_agent 的 allowed_tools 列表加载 config tools。
    用于 DAG workflow 的 inline_agent 节点（无顶层 agent 字段）。
    """
    if not allowed_tools:
        return []

    try:
        from orchestrator.config_loader import get_system_config
        cfg = get_system_config()
    except Exception as e:
        logger.debug("get_system_config 失败，跳过 inline_agent tools 装载: %s", e)
        return []

    # base 工具名（避免重复装载）
    base_tool_names = {
        "present_content", "todo", "finalize", "request_human_input",
    }

    extras: list[ToolDefinition] = []
    for tool_id in allowed_tools:
        if tool_id in base_tool_names:
            continue
        tool_cfg = cfg.tools.get(tool_id)
        if not tool_cfg:
            continue
        # inline_agent 不做域级过滤（节点已在 workflow YAML 显式声明 allowed_tools）
        handler = None
        if tool_cfg.handler_module == "cli":
            try:
                from tools.cli_tools import sync_cli_handler
                handler = sync_cli_handler(tool_cfg)
            except Exception as e:
                logger.warning("工具 %s cli dispatcher 装载失败: %s", tool_id, e)
                continue
            if handler is None:
                continue
        elif tool_cfg.handler_module and tool_cfg.handler_function:
            try:
                import importlib
                mod = importlib.import_module(tool_cfg.handler_module)
                handler = getattr(mod, tool_cfg.handler_function, None)
                if handler is None:
                    logger.warning("工具 %s handler %s.%s 找不到",
                                   tool_id, tool_cfg.handler_module, tool_cfg.handler_function)
                    continue
            except Exception as e:
                logger.warning("工具 %s 装载失败: %s", tool_id, e)
                continue
        else:
            continue
        extras.append(ToolDefinition(
            name=tool_cfg.tool_id,
            description=tool_cfg.description or tool_cfg.display_name,
            input_schema=tool_cfg.input_schema or {"type": "object"},
            handler=handler,
        ))

    return extras

