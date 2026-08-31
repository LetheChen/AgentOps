"""视频质检工具 — 用 ffprobe 解析 mp4 元数据，校验时长/分辨率/音轨。

对应 config/tools/ffprobe_check.yaml。
通过 subprocess 调用系统 ffprobe 命令，解析 JSON 输出，提取 mp4 的时长/分辨率/
音轨/编码/码率等元数据，并按容差校验时长是否匹配目标值。

校验项：
  - file_exists：mp4 文件存在
  - duration_match：|实际时长 - 目标时长| <= tolerance_seconds
  - has_audio：存在音频流
  - resolution_valid：宽高均 > 0

设计要点：
  - ffprobe 未安装时优雅降级返回 ok=False（不抛异常），让 agent 可据此判定"需人工"
  - async 函数内用 asyncio.to_thread 包 subprocess.run，避免阻塞事件循环
  - fail-safe load_dotenv（参考 tools/wecom_notify.py 第 19-25 行，保持模块风格一致）

参考: config/agents/quality_inspector.yaml, workflows/video-pipeline.yaml
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

# fail-safe：加载项目根目录 .env（override=False 不覆盖已有环境变量）
# 本工具不直接读 env，但保持与 tools/ 其他模块一致的加载风格
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_path, override=False)
    except Exception:
        pass  # dotenv 不可用时降级（环境变量可能已由父进程注入）

logger = logging.getLogger(__name__)


async def ffprobe_check(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """用 ffprobe 解析 mp4 元数据并校验时长/分辨率/音轨。

    Args:
        args: {
            "mp4_path": "video.mp4",
            "target_duration": 60,        # 目标时长（秒）
            "tolerance_seconds": 5        # 时长容差（秒）
        }
        config: {"timeout_seconds": 30}  # ffprobe 子进程超时

    Returns:
        {
            "ok": bool,
            "mp4_path": str,
            "exists": bool,
            "duration_seconds": float | None,
            "target_duration": int | None,
            "duration_diff": float | None,
            "duration_ok": bool,
            "width": int | None,
            "height": int | None,
            "has_audio": bool,
            "audio_codec": str | None,
            "video_codec": str | None,
            "bit_rate": int | None,
            "checks": [{"name": str, "passed": bool, ...}],
            "errors": [str]  # 失败项的错误描述
        }
        ffprobe 未安装时返回 {"ok": False, "errors": ["ffprobe 未安装或不在 PATH"]}；
        文件不存在时返回 {"ok": False, "exists": False, "errors": ["文件不存在"]}。
    """
    cfg = config or {}
    timeout = cfg.get("timeout_seconds", 30)

    mp4_path = args.get("mp4_path", "")
    target_duration = args.get("target_duration")
    tolerance = args.get("tolerance_seconds", 5)

    errors: list[str] = []
    checks: list[dict[str, Any]] = []

    # 1. 文件存在性校验
    path = Path(mp4_path)
    exists = path.exists()
    checks.append({"name": "file_exists", "passed": exists})
    if not exists:
        errors.append(f"文件不存在: {mp4_path}")
        logger.warning("ffprobe_check 文件不存在 path=%s", mp4_path)
        return {
            "ok": False,
            "mp4_path": mp4_path,
            "exists": False,
            "duration_seconds": None,
            "target_duration": target_duration,
            "duration_diff": None,
            "duration_ok": False,
            "width": None,
            "height": None,
            "has_audio": False,
            "audio_codec": None,
            "video_codec": None,
            "bit_rate": None,
            "checks": checks,
            "errors": errors,
        }

    # 2. 调用 ffprobe（asyncio.to_thread 包裹避免阻塞事件循环）
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    logger.info(
        "ffprobe_check 调用 path=%s target=%s tol=%s",
        mp4_path, target_duration, tolerance,
    )
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        # ffprobe 未安装或不在 PATH
        logger.error("ffprobe 未安装或不在 PATH")
        errors.append("ffprobe 未安装或不在 PATH")
        return {
            "ok": False,
            "mp4_path": mp4_path,
            "exists": True,
            "duration_seconds": None,
            "target_duration": target_duration,
            "duration_diff": None,
            "duration_ok": False,
            "width": None,
            "height": None,
            "has_audio": False,
            "audio_codec": None,
            "video_codec": None,
            "bit_rate": None,
            "checks": checks + [
                {"name": "duration_match", "passed": False},
                {"name": "has_audio", "passed": False},
                {"name": "resolution_valid", "passed": False},
            ],
            "errors": errors,
        }
    except subprocess.TimeoutExpired:
        logger.error("ffprobe 超时 path=%s timeout=%s", mp4_path, timeout)
        errors.append(f"ffprobe 执行超时（{timeout}s）")
        return {
            "ok": False,
            "mp4_path": mp4_path,
            "exists": True,
            "duration_seconds": None,
            "target_duration": target_duration,
            "duration_diff": None,
            "duration_ok": False,
            "width": None,
            "height": None,
            "has_audio": False,
            "audio_codec": None,
            "video_codec": None,
            "bit_rate": None,
            "checks": checks + [
                {"name": "duration_match", "passed": False},
                {"name": "has_audio", "passed": False},
                {"name": "resolution_valid", "passed": False},
            ],
            "errors": errors,
        }

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:500]
        logger.error("ffprobe 返回非 0 code=%s stderr=%s", proc.returncode, stderr)
        errors.append(f"ffprobe 解析失败（code={proc.returncode}）: {stderr}")
        return {
            "ok": False,
            "mp4_path": mp4_path,
            "exists": True,
            "duration_seconds": None,
            "target_duration": target_duration,
            "duration_diff": None,
            "duration_ok": False,
            "width": None,
            "height": None,
            "has_audio": False,
            "audio_codec": None,
            "video_codec": None,
            "bit_rate": None,
            "checks": checks + [
                {"name": "duration_match", "passed": False},
                {"name": "has_audio", "passed": False},
                {"name": "resolution_valid", "passed": False},
            ],
            "errors": errors,
        }

    # 3. 解析 ffprobe JSON 输出
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        logger.error("ffprobe 输出 JSON 解析失败: %s", e)
        errors.append(f"ffprobe 输出 JSON 解析失败: {e}")
        return {
            "ok": False,
            "mp4_path": mp4_path,
            "exists": True,
            "duration_seconds": None,
            "target_duration": target_duration,
            "duration_diff": None,
            "duration_ok": False,
            "width": None,
            "height": None,
            "has_audio": False,
            "audio_codec": None,
            "video_codec": None,
            "bit_rate": None,
            "checks": checks + [
                {"name": "duration_match", "passed": False},
                {"name": "has_audio", "passed": False},
                {"name": "resolution_valid", "passed": False},
            ],
            "errors": errors,
        }

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    # 提取时长（format.duration，秒）
    duration_seconds: float | None = None
    try:
        if fmt.get("duration"):
            duration_seconds = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration_seconds = None

    # 找视频流
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    width = int(video_stream.get("width", 0)) if video_stream else 0
    height = int(video_stream.get("height", 0)) if video_stream else 0
    video_codec = video_stream.get("codec_name") if video_stream else None

    # 找音频流
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    has_audio = audio_stream is not None
    audio_codec = audio_stream.get("codec_name") if audio_stream else None

    # 码率（优先 format.bit_rate，回退 video_stream.bit_rate）
    bit_rate: int | None = None
    try:
        br_raw = fmt.get("bit_rate") or (video_stream.get("bit_rate") if video_stream else None)
        bit_rate = int(br_raw) if br_raw else None
    except (TypeError, ValueError):
        bit_rate = None

    # 4. 时长校验
    duration_diff: float | None = None
    duration_ok = False
    if duration_seconds is not None and target_duration is not None:
        duration_diff = round(duration_seconds - float(target_duration), 3)
        duration_ok = abs(duration_diff) <= float(tolerance)
        checks.append({"name": "duration_match", "passed": duration_ok, "diff": duration_diff})
        if not duration_ok:
            errors.append(
                f"时长偏差 {duration_diff}s 超出容差 ±{tolerance}s"
                f"（实际 {duration_seconds}s / 目标 {target_duration}s）"
            )
    else:
        checks.append({"name": "duration_match", "passed": False})
        if duration_seconds is None:
            errors.append("ffprobe 未返回时长字段")
        if target_duration is None:
            errors.append("未传 target_duration 参数")

    # 5. 音轨校验
    checks.append({"name": "has_audio", "passed": has_audio})
    if not has_audio:
        errors.append("无音频流")

    # 6. 分辨率校验
    resolution_valid = width > 0 and height > 0
    checks.append({"name": "resolution_valid", "passed": resolution_valid})
    if not resolution_valid:
        errors.append(f"分辨率无效（width={width} height={height}）")

    ok = all(c["passed"] for c in checks)
    logger.info(
        "ffprobe_check 完成 path=%s ok=%s duration=%s audio=%s res=%sx%s",
        mp4_path, ok, duration_seconds, has_audio, width, height,
    )

    return {
        "ok": ok,
        "mp4_path": mp4_path,
        "exists": True,
        "duration_seconds": duration_seconds,
        "target_duration": target_duration,
        "duration_diff": duration_diff,
        "duration_ok": duration_ok,
        "width": width or None,
        "height": height or None,
        "has_audio": has_audio,
        "audio_codec": audio_codec,
        "video_codec": video_codec,
        "bit_rate": bit_rate,
        "checks": checks,
        "errors": errors,
    }
