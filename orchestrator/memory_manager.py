"""记忆管理器：分层管理 Session 记忆。

三层记忆：
1. 短期记忆：当前 messages 列表（在 ConversationalEngine 内）
2. 中期记忆：attached runs 的摘要（存 session_memory 表）
3. 长期记忆：跨 Session 的知识库（obsidian_vault，已有）

触发时机：
- Run 完成后：生成 run_summary 存入中期记忆
- messages 超过阈值时：压缩旧消息为 topic_summary
- 用户明确要求：将重要信息存入长期记忆（obsidian_vault）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from audit.store import EventStore

logger = logging.getLogger(__name__)

# 消息压缩阈值：超过此条数时触发压缩
MESSAGE_COMPRESS_THRESHOLD = 40
# 压缩后保留的最近消息数
MESSAGE_KEEP_RECENT = 10
# 记忆上下文 token 预算
MEMORY_CONTEXT_TOKEN_BUDGET = 2000


class MemoryManager:
    """记忆管理器。

    由 LocalSdkOrchestrator 持有，ConversationalEngine 通过 _registry 获取。
    """

    def __init__(
        self,
        event_store: "EventStore",
        llm_config: dict[str, Any] | None = None,
    ):
        self._store = event_store
        self._llm_config = llm_config or {}

    async def build_context(
        self,
        session_id: str,
        max_tokens: int = MEMORY_CONTEXT_TOKEN_BUDGET,
    ) -> str:
        """构建记忆上下文，注入到 system_prompt。

        从中期记忆中提取最相关的摘要，控制在 max_tokens 内。
        """
        memories = await self._store.list_session_memory(
            session_id=session_id,
            limit=10,
        )
        if not memories:
            return ""

        lines: list[str] = []
        token_count = 0
        for mem in memories:
            content = mem.get("content", "")
            # 粗略估算 token 数（1 中文字 ≈ 2 token）
            mem_tokens = len(content) // 2
            if token_count + mem_tokens > max_tokens:
                break
            mem_type = mem.get("memory_type", "unknown")
            lines.append(f"- [{mem_type}] {content}")
            token_count += mem_tokens

        return "\n".join(lines)

    async def summarize_run(
        self,
        session_id: str,
        run_id: str,
        workflow_id: str,
        run_events: list[dict[str, Any]],
    ) -> str:
        """Run 完成后生成摘要，存入中期记忆。

        摘要内容：任务目标 + 关键结果 + 耗时 + token 消耗。
        """
        # 从事件中提取关键信息
        node_summaries: list[str] = []
        total_tokens = 0
        for ev in run_events:
            ev_type = ev.get("type", "")
            payload = ev.get("payload") or {}
            if ev_type == "node.completed":
                node_id = ev.get("node_id", "")
                summary = (payload.get("summary") or "")[:200]
                tokens = (payload.get("tokens_in", 0) or 0) + (payload.get("tokens_out", 0) or 0)
                total_tokens += tokens
                node_summaries.append(f"  {node_id}: {summary}（{tokens} tokens）")

        summary_text = (
            f"任务 {workflow_id}（run_id={run_id[:12]}）\n"
            + ("节点完成:\n" + "\n".join(node_summaries) + "\n" if node_summaries else "无节点完成事件\n")
            + f"总消耗: {total_tokens} tokens"
        )

        # 存入中期记忆（v3: source_run_id 写入 session_memory.source_run_id，FK to runs）
        await self._store.add_session_memory(
            session_id=session_id,
            memory_type="run_summary",
            source_run_id=run_id,
            content=summary_text,
            tokens=len(summary_text) // 2,
            importance=0.7,
        )

        logger.info("Run 摘要已存入 session_memory: %s -> %s", run_id[:12], session_id[:12])
        return summary_text

    async def compress_messages_if_needed(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """消息数超过阈值时压缩旧消息。

        策略：保留最近 MESSAGE_KEEP_RECENT 条，旧消息压缩为 topic_summary 存入中期记忆。
        """
        if len(messages) < MESSAGE_COMPRESS_THRESHOLD:
            return messages

        # 分割为旧消息（要压缩）和近期消息（要保留）
        old_messages = messages[:-MESSAGE_KEEP_RECENT]
        recent_messages = messages[-MESSAGE_KEEP_RECENT:]

        # 生成旧消息的摘要（规则式，后续可用 LLM 增强）
        old_summary_parts: list[str] = []
        for msg in old_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                old_summary_parts.append(f"[{role}] {content[:100]}")
            elif isinstance(content, list):
                # content 可能是 list（多模态消息）
                text_parts = [p.get("text", "")[:100] for p in content if isinstance(p, dict) and "text" in p]
                old_summary_parts.append(f"[{role}] {' '.join(text_parts)[:100]}")

        old_summary = "早期对话摘要:\n" + "\n".join(old_summary_parts)

        # 存入中期记忆
        await self._store.add_session_memory(
            session_id=session_id,
            memory_type="topic_summary",
            content=old_summary,
            tokens=len(old_summary) // 2,
            importance=0.5,
        )

        logger.info(
            "消息压缩: %d 条 -> %d 条 + 1 条 topic_summary (session=%s)",
            len(messages), len(recent_messages), session_id[:12],
        )

        # 返回压缩后的消息列表（摘要指针 + 近期消息）
        return [
            {"role": "system", "content": f"[早期对话已压缩，见记忆库]\n{old_summary[:500]}..."},
            *recent_messages,
        ]
