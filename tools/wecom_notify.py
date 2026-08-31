#!/usr/bin/env python
"""tools/wecom_notify.py — 企业微信群机器人推送 CLI。

agent 通过 Bash 调用：
  python tools/wecom_notify.py --content "告警内容" [--msg-type markdown]

webhook URL 从环境变量 WECOM_WEBHOOK_URL 读取（敏感信息不进配置文件）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# fail-safe：加载项目根目录的 .env（override=False 不覆盖已有环境变量）
# 确保工具在任意进程执行时都能读到 WECOM_WEBHOOK_URL
# （opencode harness 工具调用可能不在后端主进程执行）
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=False)


def send_wecom(content: str, msg_type: str = "markdown", webhook_url: str | None = None) -> dict:
    """发送企业微信机器人消息。

    Args:
        content: 消息内容（markdown 类型支持企业微信 markdown 语法）
        msg_type: 消息类型 text / markdown（默认 markdown）
        webhook_url: 企业微信机器人 webhook URL，None 则从环境变量读

    Returns:
        {"ok": bool, "status_code": int, "response": str}
    """
    url = webhook_url or os.environ.get("WECOM_WEBHOOK_URL", "")
    if not url:
        return {
            "ok": False,
            "status_code": 0,
            "response": "WECOM_WEBHOOK_URL 环境变量未设置",
        }

    # 企业微信机器人消息体格式
    if msg_type == "markdown":
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
    else:
        payload = {"msgtype": "text", "text": {"content": content}}

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return {
                "ok": resp.status == 200 and '"errcode":0' in body,
                "status_code": resp.status,
                "response": body,
            }
    except urllib.error.URLError as e:
        return {"ok": False, "status_code": 0, "response": f"URLError: {e}"}
    except Exception as e:
        return {"ok": False, "status_code": 0, "response": f"Exception: {e}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="企业微信群机器人推送")
    parser.add_argument("--content", required=True, help="消息内容")
    parser.add_argument("--msg-type", default="markdown", choices=["text", "markdown"],
                        help="消息类型（默认 markdown）")
    args = parser.parse_args()

    result = send_wecom(content=args.content, msg_type=args.msg_type)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
