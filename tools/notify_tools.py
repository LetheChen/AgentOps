"""通知工具模块 — 企业微信推送。

对应 config/tools/wecom_notify.yaml。
CLI 入口在 tools/wecom_notify.py，本模块提供 Python 函数式调用接口
（供 deterministic harness 或其他 Python 代码直接 import）。
"""
from __future__ import annotations

from typing import Any

# 复用 CLI 版本的实现，避免重复代码
from tools.wecom_notify import send_wecom


async def send_wecom_notification(
    args: dict[str, Any], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """发送企业微信群机器人消息（async handler 接口）。

    Args:
        args: {"content": "消息内容", "msg_type": "markdown"|"text"}
        config: handler 配置（暂未使用，webhook URL 从环境变量读）

    Returns:
        {"ok": bool, "status_code": int, "response": str}
    """
    content = args.get("content", "")
    msg_type = args.get("msg_type", "markdown")
    if not content:
        return {"ok": False, "status_code": 0, "response": "content 不能为空"}

    # send_wecom 是同步函数（urllib），直接调用即可
    return send_wecom(content=content, msg_type=msg_type)
