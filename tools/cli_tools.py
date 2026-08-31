"""
CLI tools dispatcher — 通用 async handler 打包器，把 yaml 中的 `handler: cli` 转为可执行子进程。
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEBUG_LOG = Path(__file__).resolve().parent.parent / "temp" / "cli_tools_debug.log"


def _debug_write(msg: str) -> None:
    try:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def sync_cli_handler(tool_cfg: Any, working_dir: str | None = None) -> Any:
    command_template = (tool_cfg.handler_config or {}).get("command", "")
    timeout = (tool_cfg.handler_config or {}).get("timeout", 60)
    raw_working_dir = working_dir or (tool_cfg.handler_config or {}).get("working_dir") or "."

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if not command_template:
            return {"content": f"ERROR: tool '{tool_cfg.tool_id}' has no command configured", "ok": False}

        _debug_write(f"{tool_cfg.tool_id} CALLED: args={_json.dumps(args, ensure_ascii=False)[:500]}")

        try:
            # 用 shlex.split 检测未替换占位符，但不用于实际执行
            tokens = shlex.split(command_template)
            import re as _re
            unreplaced = []
            for token in tokens:
                for k, v in args.items():
                    token = token.replace("{" + k + "}", str(v))
                unreplaced.extend(_re.findall(r"\{(\w+)\}", token))
            unreplaced = [u for u in unreplaced if u != "workspace"]
            if unreplaced:
                _debug_write(f"{tool_cfg.tool_id} UNREPLACED: {unreplaced}")
                return {
                    "content": f"ERROR: 缺少参数: {', '.join(unreplaced)}。LLM 传参: {_json.dumps(args, ensure_ascii=False)[:300]}",
                    "ok": False, "returncode": -1,
                }
            ws_root = str(raw_working_dir).rstrip("/") + "/"
            # 构造完整命令字符串：在模板上做参数替换 + {{workspace.root}} 替换
            full_cmd = command_template
            for k, v in args.items():
                full_cmd = full_cmd.replace("{" + k + "}", str(v))
            full_cmd = full_cmd.replace("{{workspace.root}}", ws_root)
        except ValueError as e:
            _debug_write(f"{tool_cfg.tool_id} TEMPLATE_ERROR: {e}  template={command_template}")
            return {"content": f"ERROR: 模板渲染失败: {e}", "ok": False}

        cwd = str(Path(raw_working_dir.replace("{{workspace.root}}", str(Path.cwd()))).resolve())
        _debug_write(f"{tool_cfg.tool_id} EXEC: cmd={full_cmd} cwd={cwd} timeout={timeout}")

        # 自动为 output 参数创建父目录（mm_speech/mm_image 等工具写入文件时目录可能不存在）
        out_val = args.get("output", "")
        if out_val:
            out_path = Path(str(out_val).replace("{{workspace.root}}", ws_root))
            if not out_path.is_absolute():
                out_path = Path(cwd) / out_path
            out_path.parent.mkdir(parents=True, exist_ok=True)

        # Windows 兼容：用 subprocess.run(shell=True) 执行完整命令字符串
        # 解决三个问题：
        # 1. asyncio.create_subprocess_exec 对 .CMD 脚本不支持（NotImplementedError）
        # 2. shlex.split 对含空格参数值错误分词（如 --composition "code/composition.html"）
        # 3. .CMD 脚本需要 shell 查找 PATH
        # 另外：清除 MINIMAX_BASE_URL 环境变量，因为 .env 设了 /v1 后缀，
        # mmx CLI 的 search/speech/image API 不走 /v1 前缀，会 404
        import os as _os
        clean_env = _os.environ.copy()
        clean_env.pop("MINIMAX_BASE_URL", None)
        def _run_blocking():
            try:
                res = subprocess.run(
                    full_cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    timeout=timeout,
                    env=clean_env,
                )
                return res
            except subprocess.TimeoutExpired:
                return None

        res = await asyncio.to_thread(_run_blocking)
        if res is None:
            _debug_write(f"{tool_cfg.tool_id} TIMEOUT after {timeout}s")
            return {"content": f"ERROR: command timed out after {timeout}s", "returncode": -1, "ok": False}
        stdout_bytes = res.stdout or b""
        stderr_bytes = res.stderr or b""
        rc = res.returncode or 0

        content = ((stdout_bytes or b"").decode("utf-8", errors="ignore")) + (
            ("\n[stderr]\n" + (stderr_bytes or b"").decode("utf-8", errors="ignore"))
            if stderr_bytes else ""
        )
        _debug_write(
            f"{tool_cfg.tool_id} DONE: rc={rc} stdout={len(stdout_bytes or b'')}B "
            f"stderr={len(stderr_bytes or b'')}B  preview={content[:400]}"
        )
        return {"content": content, "returncode": rc, "ok": rc == 0}

    return handler
