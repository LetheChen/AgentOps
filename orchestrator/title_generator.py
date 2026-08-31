"""会话标题异步生成器。

策略：
1. 兜底：首条用户消息截取前 30 字
2. LLM 生成：后台异步调 LLM 生成 5-15 字摘要标题

调用方式（fire-and-forget，不阻塞主流程）：
    asyncio.create_task(generate_and_update_title(run_id, user_message, event_store, llm_config))
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from audit.store import EventStore

logger = logging.getLogger(__name__)

# 标题长度约束
TITLE_MAX_LEN = 30
TITLE_FALLBACK_LEN = 30


def _fallback_title(message: str) -> str:
    """兜底标题：首条消息截取前 30 字。"""
    title = message.strip().replace("\n", " ")[:TITLE_FALLBACK_LEN]
    return title if title else "新会话"


async def generate_and_update_title(
    run_id: str,
    user_message: str,
    event_store: EventStore,
    llm_config: dict,
) -> None:
    """异步生成会话标题并更新到 sessions 表。

    先用兜底标题立即更新，再后台调 LLM 生成更好的标题覆盖。
    """
    # 1. 立即用兜底标题
    fallback = _fallback_title(user_message)
    try:
        await event_store.update_session_title(run_id, fallback)
    except Exception as e:
        logger.warning("更新兜底标题失败: %s", e)
        return

    # 2. 后台调 LLM 生成（失败不影响兜底标题）
    api_key = llm_config.get("api_key", "")
    base_url = llm_config.get("base_url", "")
    model_full = llm_config.get("model", "")
    if not api_key or not base_url or not model_full:
        # 无 LLM 配置，保持兜底标题
        return

    # 去掉 provider/ 前缀（minimax/MiniMax-M3 → MiniMax-M3）
    model_id = model_full.split("/", 1)[-1] if "/" in model_full else model_full

    prompt = (
        f"请为以下用户消息生成一个简洁的会话标题（5-15字，不要引号、不要标点结尾）：\n\n"
        f"{user_message[:500]}"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": "你是会话标题生成器，只输出标题文本，不加任何修饰。"},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 50,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            title = data["choices"][0]["message"]["content"].strip()
            # 清理：去掉引号、换行
            title = title.strip("\"'""''「」「」").replace("\n", " ")
            if title and len(title) <= TITLE_MAX_LEN:
                await event_store.update_session_title(run_id, title)
                logger.info("会话 %s 标题已更新: %s", run_id, title)
    except Exception as e:
        logger.debug("LLM 生成标题失败（保持兜底标题）: %s", e)
