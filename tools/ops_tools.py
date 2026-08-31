"""运维工具 — 日志扫描 + 系统资源探针 + DAG run 异常查询。

对应 config/tools/log_query.yaml + system_probe.yaml + list_failed_runs.yaml
+ server_status.yaml + server_restart.yaml + ssh_exec.yaml + db_migrate.yaml。

query_logs 为本地文件扫描版：扫描指定目录下所有日志文件（支持子目录按业务系统分类），
返回匹配关键字或级别的日志行。agent 也可通过 Bash 直接调 CLI：
  python tools/log_query.py --log-source-id seeyon [--keyword <kw>] [--level ERROR] [--time-range 24h]

白名单授权：log_source_id 优先于 log_dir；两者都传 log_source_id 时从 config/patrol.yaml
的 log_sources 解析实际路径；只传 log_dir 时校验路径必须 在某个 log_source.path 下（防路径遍历）。

system_probe 为本地资源探针：用 psutil 查进程/端口/磁盘/内存/CPU，
关键指标超阈值（进程死亡/端口不通/磁盘>90%/内存>90%）时标记 abnormal=True。
不依赖后端进程，可在 local_llm harness 子进程独立运行。

list_failed_runs 直接读 audit.db（不依赖 _orchestrator 内存状态）：
查最近 N 小时内 status='failed' 的 run + dag_events 中的 node.failed 事件。
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ── 日志扫描 ────────────────────────────────────────────────

# 日志级别关键字（不区分大小写匹配）
_LEVEL_PATTERNS: dict[str, list[str]] = {
    "FATAL": ["fatal", "critical", "panic", "segfault"],
    "ERROR": ["error", "exception", "traceback", "failed", "failure"],
    "WARN": ["warn", "warning", "deprecated"],
}

# 重大问题关键字（用于巡检告警判断）
CRITICAL_KEYWORDS: list[str] = [
    "OutOfMemoryError", "StackOverflow", "ConnectionRefused",
    " segfault", "panic", "core dumped",
    "disk full", "no space left", "permission denied",
    "connection timeout", "refused", "reset by peer",
    "database is locked", "deadlock",
    "service unavailable", "503", "502 bad gateway",
]

# 默认 patrol.yaml 路径（log_sources 白名单来源）
_DEFAULT_PATROL_CONFIG_PATH = "config/patrol.yaml"


# ── log_sources 白名单授权 ──────────────────────────────────

def _load_log_sources(config_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """从 patrol.yaml 加载 log_sources 白名单。

    Returns:
        {id: {"path": ..., "name": ..., "allow_read": ..., "allow_list": ...}}
    """
    path = Path(config_path) if config_path else Path(_DEFAULT_PATROL_CONFIG_PATH)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in data.get("log_sources") or []:
        sid = item.get("id")
        if not sid:
            continue
        result[sid] = {
            "path": item.get("path", ""),
            "name": item.get("name", sid),
            "description": item.get("description", ""),
            "allow_read": bool(item.get("allow_read", True)),
            "allow_list": bool(item.get("allow_list", True)),
        }
    return result


def _normalize_path(p: str | Path) -> Path:
    """路径规范化：resolve + 严格解析，防 ../ 路径遍历攻击。"""
    return Path(p).resolve(strict=False)


def _is_path_allowed(path: Path, allowed_roots: list[Path]) -> bool:
    """检查 path 是否在任一 allowed_root 下（含自身）。

    用 resolve 后的字符串前缀匹配 + 父子关系校验，防符号链接和 ../ 绕过。
    """
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    for root in allowed_roots:
        try:
            root_resolved = root.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        # path == root 或 path 在 root 下
        if resolved == root_resolved:
            return True
        try:
            resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def resolve_log_dir(
    log_source_id: str | None,
    log_dir: str | None,
    config_path: str | Path | None = None,
) -> tuple[str, str | None]:
    """解析日志目录 + 白名单校验。

    Args:
        log_source_id: 日志源 ID（优先）
        log_dir: 直接路径（次选，需在白名单内）
        config_path: patrol.yaml 路径

    Returns:
        (actual_dir, error_message)
        - 成功：(resolved_dir, None)
        - 失败：(original_dir_or_empty, error_message)
    """
    sources = _load_log_sources(config_path)

    # 1. log_source_id 优先
    if log_source_id:
        src = sources.get(log_source_id)
        if not src:
            return "", f"log_source_id '{log_source_id}' 未在 config/patrol.yaml 的 log_sources 白名单中"
        if not src.get("allow_read", True):
            return "", f"log_source_id '{log_source_id}' 已被禁用读取（allow_read=false）"
        if not src.get("path"):
            return "", f"log_source_id '{log_source_id}' 未配置 path"
        return src["path"], None

    # 2. log_dir 次选：校验路径必须在白名单任一 path 下
    if log_dir:
        if not sources:
            # 无白名单配置时放行（向后兼容本地开发 ./logs/）
            return log_dir, None
        allowed_roots = [Path(s["path"]) for s in sources.values() if s.get("path")]
        if not allowed_roots:
            return log_dir, None
        if _is_path_allowed(Path(log_dir), allowed_roots):
            return log_dir, None
        return log_dir, f"log_dir '{log_dir}' 不在 log_sources 白名单授权范围内（防路径遍历）"

    # 3. 都不传
    return "", "必须传 log_source_id 或 log_dir 参数（至少一个）"


def _parse_time_range(time_range: str) -> datetime:
    """解析时间范围字符串（1h/24h/7d）为起始时间 datetime。"""
    now = datetime.now(timezone.utc)
    tr = time_range.strip().lower()
    match = re.match(r"^(\d+)\s*(h|d|w|m)$", tr)
    if not match:
        return now - timedelta(hours=24)  # 默认 24h
    num = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        return now - timedelta(hours=num)
    if unit == "d":
        return now - timedelta(days=num)
    if unit == "w":
        return now - timedelta(weeks=num)
    if unit == "m":
        return now - timedelta(minutes=num)
    return now - timedelta(hours=24)


def _extract_log_timestamp(line: str) -> datetime | None:
    """从日志行提取时间戳，失败返回 None。

    支持常见格式：
      2026-07-13 10:23:45 ...
      2026-07-13T10:23:45Z ...
      [2026-07-13 10:23:45] ...
      13/Jul/2026:10:23:45 ...
    """
    # ISO 格式
    m = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1).replace("T", " "))
        except ValueError:
            pass
    # 带方括号
    m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1))
        except ValueError:
            pass
    # nginx 格式
    m = re.search(r"(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2})", line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d/%b/%Y:%H:%M:%S")
        except ValueError:
            pass
    return None


def _match_level(line: str, level: str) -> bool:
    """检查日志行是否匹配指定级别或更高级别。"""
    level = level.upper()
    # 级别优先级：FATAL > ERROR > WARN
    priority = {"FATAL": 0, "ERROR": 1, "WARN": 2}
    target_priority = priority.get(level, 1)
    for lv, patterns in _LEVEL_PATTERNS.items():
        if priority.get(lv, 99) <= target_priority:
            for p in patterns:
                if p in line.lower():
                    return True
    return False


def scan_log_directory(
    log_dir: str,
    keyword: str | None = None,
    level: str = "ERROR",
    time_range: str = "24h",
    max_lines: int = 500,
) -> dict[str, Any]:
    """扫描日志目录，返回匹配的日志行。

    目录结构约定：
      log_dir/
      ├── seeyon/         # 致远 OA 智能审批
      │   ├── app.log
      │   └── error.log
      ├── nginx/          # nginx
      │   └── access.log
      └── redis/          # redis

    Returns:
        {
            "total_files_scanned": int,
            "matched_lines": int,
            "truncated": bool,
            "entries": [
                {
                    "source": "seeyon/app.log",
                    "line_number": 1234,
                    "timestamp": "2026-07-13 10:23:45",
                    "level": "ERROR",
                    "content": "..."
                }
            ],
            "by_source": {"seeyon/app.log": 5, "nginx/access.log": 2}
        }
    """
    log_path = Path(log_dir)
    if not log_path.exists():
        return {
            "total_files_scanned": 0,
            "matched_lines": 0,
            "truncated": False,
            "entries": [],
            "by_source": {},
            "error": f"日志目录不存在: {log_dir}",
        }

    start_time = _parse_time_range(time_range)
    entries: list[dict[str, Any]] = []
    by_source: dict[str, int] = {}
    total_files = 0
    truncated = False

    # 遍历目录下所有文件（支持子目录）
    log_extensions = {".log", ".txt", ".out", ".err", ".stderr", ".stdout"}
    for file_path in sorted(log_path.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in log_extensions:
            continue

        total_files += 1
        rel_path = str(file_path.relative_to(log_path)).replace("\\", "/")
        source_matches = 0

        try:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.rstrip("\n\r")
                    if not line:
                        continue

                    # 时间范围过滤（统一为 offset-aware UTC 比较）
                    ts = _extract_log_timestamp(line)
                    if ts:
                        ts_utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
                        if ts_utc < start_time:
                            continue

                    # 级别过滤（有关键字时放宽级别要求，只要包含关键字就匹配）
                    if keyword:
                        if keyword.lower() not in line.lower():
                            continue
                    else:
                        if not _match_level(line, level):
                            continue

                    entries.append({
                        "source": rel_path,
                        "line_number": line_num,
                        "timestamp": ts.isoformat() if ts else None,
                        "level": _detect_level(line),
                        "content": line[:2000],  # 单行截断
                    })
                    source_matches += 1

                    if len(entries) >= max_lines:
                        truncated = True
                        break
        except (PermissionError, OSError):
            continue

        if source_matches > 0:
            by_source[rel_path] = source_matches

        if truncated:
            break

    return {
        "total_files_scanned": total_files,
        "matched_lines": len(entries),
        "truncated": truncated,
        "entries": entries,
        "by_source": by_source,
    }


def _detect_level(line: str) -> str:
    """检测日志行的级别。"""
    for lv in ["FATAL", "ERROR", "WARN"]:
        if _match_level(line, lv):
            return lv
    return "INFO"


def detect_critical_issues(scan_result: dict[str, Any]) -> list[dict[str, Any]]:
    """从扫描结果中检测重大问题（用于巡检告警判断）。

    判断标准：
      1. 出现 CRITICAL_KEYWORDS 中的关键字
      2. 同一错误 5 分钟内出现 > 10 次（频率告警）
      3. FATAL/CRITICAL 级别日志
    """
    critical: list[dict[str, Any]] = []
    entries = scan_result.get("entries", [])

    # 1. 关键字匹配
    for entry in entries:
        content = entry.get("content", "")
        for kw in CRITICAL_KEYWORDS:
            if kw.lower() in content.lower():
                critical.append({
                    "type": "critical_keyword",
                    "keyword": kw,
                    "source": entry.get("source"),
                    "line_number": entry.get("line_number"),
                    "content": content[:200],
                })
                break

    # 2. FATAL/CRITICAL 级别
    for entry in entries:
        if entry.get("level") in ("FATAL", "CRITICAL"):
            critical.append({
                "type": "fatal_level",
                "source": entry.get("source"),
                "line_number": entry.get("line_number"),
                "content": entry.get("content", "")[:200],
            })

    # 3. 频率告警：同一 source 5 分钟内 > 10 条
    by_source: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        src = entry.get("source", "")
        by_source.setdefault(src, []).append(entry)

    for src, src_entries in by_source.items():
        if len(src_entries) > 10:
            # 检查时间窗口（如果有时间戳）
            timestamps = [e for e in src_entries if e.get("timestamp")]
            if timestamps:
                ts_list = sorted([e["timestamp"] for e in timestamps])
                # 简化判断：首末时间差 < 5 分钟且 > 10 条
                try:
                    first = datetime.fromisoformat(ts_list[0])
                    last = datetime.fromisoformat(ts_list[-1])
                    if (last - first).total_seconds() < 300:  # 5 分钟
                        critical.append({
                            "type": "high_frequency",
                            "source": src,
                            "count": len(src_entries),
                            "time_window": "5min",
                        })
                except (ValueError, TypeError):
                    pass
            else:
                # 无时间戳，按数量判断
                if len(src_entries) > 50:
                    critical.append({
                        "type": "high_volume",
                        "source": src,
                        "count": len(src_entries),
                    })

    return critical


# ── async handler 接口（供 deterministic harness 调用）────────

async def query_logs(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """扫描日志目录，返回匹配的日志行（白名单授权版）。

    Args:
        args: {"log_source_id": "seeyon", "log_dir": "...", "keyword": "...",
               "level": "ERROR", "time_range": "24h", "lines": 500}
              log_source_id 优先于 log_dir；两者都传时用 log_source_id 从白名单解析路径；
              只传 log_dir 时校验路径必须在白名单授权范围内（防路径遍历）
        config: handler 配置 {"max_lines": 5000, "timeout_seconds": 30,
                              "patrol_config_path": "config/patrol.yaml"}

    Returns:
        scan_log_directory() 的返回结果 + critical_issues 字段；
        授权失败时返回 {"error": "..."} 不扫描
    """
    cfg = config or {}
    patrol_config_path = cfg.get("patrol_config_path", _DEFAULT_PATROL_CONFIG_PATH)

    log_source_id = args.get("log_source_id")
    log_dir_arg = args.get("log_dir")
    keyword = args.get("keyword")
    level = args.get("level", "ERROR")
    time_range = args.get("time_range", "24h")
    max_lines = min(
        args.get("lines", 500),
        cfg.get("max_lines", 5000),
    )

    # 白名单授权校验
    actual_log_dir, auth_error = resolve_log_dir(
        log_source_id=log_source_id,
        log_dir=log_dir_arg,
        config_path=patrol_config_path,
    )
    if auth_error:
        return {
            "error": auth_error,
            "log_source_id": log_source_id,
            "log_dir": log_dir_arg,
        }

    result = scan_log_directory(
        log_dir=actual_log_dir,
        keyword=keyword,
        level=level,
        time_range=time_range,
        max_lines=max_lines,
    )
    # 附加授权元信息（便于审计）
    result["resolved_log_dir"] = actual_log_dir
    result["log_source_id"] = log_source_id

    # 附加重大问题检测
    result["critical_issues"] = detect_critical_issues(result)
    return result


# ── 以下为 stub，后续迭代实现 ────────────────────────────────

async def execute_ssh_command(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """通过 SSH 执行远程命令。P5 待实现。"""
    raise NotImplementedError("ssh_exec 工具尚未实现（P5 待办）")


async def query_server_status(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """查询服务器状态。P5 待实现。"""
    raise NotImplementedError("server_status 工具尚未实现（P5 待办）")


async def restart_server(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """重启服务器（高危）。P5 待实现。"""
    raise NotImplementedError("server_restart 工具尚未实现（P5 待办）")


async def execute_db_migration(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行数据库迁移。P5 待实现。"""
    raise NotImplementedError("db_migrate 工具尚未实现（P5 待办）")


# ── 系统资源探针（task_monitor agent 用）─────────────────────

# 资源告警阈值（百分比）
_DISK_USAGE_THRESHOLD = 90.0      # 磁盘使用率 > 90% → abnormal
_MEMORY_USAGE_THRESHOLD = 90.0    # 内存使用率 > 90% → abnormal
_CPU_USAGE_THRESHOLD = 90.0       # CPU 使用率 > 90% → abnormal

# 服务名 → 进程名匹配规则（小写 substring 匹配，便于跨平台）
# 巡检 agent 可传 target_services="agentops_backend,agentops_frontend"，按这里的映射解析为实际进程名
_SERVICE_PROCESS_MAP: dict[str, list[str]] = {
    "agentops_backend": ["python", "uvicorn", "api.server", "agent-ops"],
    "agentops_frontend": ["node", "vite", "npm"],
    "opencode": ["opencode"],
}

# 默认目标端口（与前端开发服务一致）
_DEFAULT_TARGET_PORTS = "1987,5173"


def _parse_int_csv(s: str) -> list[int]:
    """解析逗号分隔的整数列表（容错：忽略空段/非数字）。"""
    result: list[int] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


def _check_port_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str | None]:
    """检查 TCP 端口是否可达（socket connect）。

    Returns:
        (reachable, error_message)
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except (TimeoutError, socket.timeout) as e:
        return False, f"timeout: {e}"
    except (ConnectionRefusedError, OSError) as e:
        return False, f"refused: {e}"
    except Exception as e:
        return False, f"error: {e}"


def _find_processes_by_name(name_patterns: list[str]) -> list[dict[str, Any]]:
    """按进程名小写 substring 匹配，返回所有匹配进程的元信息。

    用 psutil.process_iter 遍历，name + cmdline 一起匹配（避免 python 进程被误判）。
    """
    try:
        import psutil
    except ImportError:
        return [{"error": "psutil 未安装"}]

    patterns_lower = [p.lower() for p in name_patterns if p]
    matches: list[dict[str, Any]] = []
    seen_pids: set[int] = set()

    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            info = proc.info
            name = (info.get("name") or "").lower()
            cmdline_list = info.get("cmdline") or []
            cmdline_str = " ".join(cmdline_list).lower()
            for pat in patterns_lower:
                if pat in name or pat in cmdline_str:
                    pid = info.get("pid")
                    if pid in seen_pids:
                        break
                    seen_pids.add(pid)
                    mem_info = info.get("memory_info")
                    rss_mb = (mem_info.rss / 1024 / 1024) if mem_info else 0
                    matches.append({
                        "pid": pid,
                        "name": info.get("name"),
                        "cmdline": " ".join(cmdline_list)[:300],
                        "memory_rss_mb": round(rss_mb, 2),
                    })
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return matches


def _probe_disk(disk_path: str) -> dict[str, Any]:
    """探针单个磁盘分区使用率。"""
    try:
        import psutil
    except ImportError:
        return {"path": disk_path, "error": "psutil 未安装"}

    try:
        usage = psutil.disk_usage(disk_path)
        total_gb = usage.total / 1024 / 1024 / 1024
        used_gb = usage.used / 1024 / 1024 / 1024
        free_gb = usage.free / 1024 / 1024 / 1024
        percent = usage.percent
        return {
            "path": disk_path,
            "total_gb": round(total_gb, 2),
            "used_gb": round(used_gb, 2),
            "free_gb": round(free_gb, 2),
            "percent": round(percent, 2),
            "abnormal": percent > _DISK_USAGE_THRESHOLD,
        }
    except Exception as e:
        return {"path": disk_path, "error": str(e)}


def _probe_memory() -> dict[str, Any]:
    """探针内存使用率。"""
    try:
        import psutil
    except ImportError:
        return {"error": "psutil 未安装"}

    try:
        vm = psutil.virtual_memory()
        total_gb = vm.total / 1024 / 1024 / 1024
        used_gb = vm.used / 1024 / 1024 / 1024
        available_gb = vm.available / 1024 / 1024 / 1024
        return {
            "total_gb": round(total_gb, 2),
            "used_gb": round(used_gb, 2),
            "available_gb": round(available_gb, 2),
            "percent": round(vm.percent, 2),
            "abnormal": vm.percent > _MEMORY_USAGE_THRESHOLD,
        }
    except Exception as e:
        return {"error": str(e)}


def _probe_cpu() -> dict[str, Any]:
    """探针 CPU 使用率。

    interval=1.0 阻塞 1 秒采样（psutil 标准 API）；per_cpu=True 返回每核使用率。
    """
    try:
        import psutil
    except ImportError:
        return {"error": "psutil 未安装"}

    try:
        overall = psutil.cpu_percent(interval=1.0)
        per_cpu = psutil.cpu_percent(interval=0.0, percpu=True)
        return {
            "percent": round(overall, 2),
            "per_cpu_percent": [round(p, 2) for p in per_cpu],
            "core_count": psutil.cpu_count(logical=True) or 0,
            "abnormal": overall > _CPU_USAGE_THRESHOLD,
        }
    except Exception as e:
        return {"error": str(e)}


async def system_probe(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """系统资源与进程探针（task_monitor agent probe 节点用）。

    检测：
      - 进程存活（按 name 匹配 psutil.process_iter）
      - 端口可达（socket.create_connection 3s 超时）
      - 磁盘使用率（psutil.disk_usage）
      - 内存使用率（psutil.virtual_memory）
      - CPU 使用率（psutil.cpu_percent，1s 采样）

    Args:
        args: {
            "check_scope": "all|process|port|disk|memory|cpu|task",  # 默认 all；task 走 list_failed_runs 路径
            "target_services": "agentops_backend,agentops_frontend",  # 逗号分隔服务名
            "target_ports": "1987,5173",  # 逗号分隔端口
            "disk_path": "/",  # 磁盘路径
            "host": "127.0.0.1",  # 端口探测目标 host
        }
        config: 暂未使用（保留扩展位）

    Returns:
        {
            "processes": [{service, found, count, samples: [...]}],
            "ports": [{port, reachable, error}],
            "disk": {...},
            "memory": {...},
            "cpu": {...},
            "abnormal": bool,  # 任一指标 abnormal 即 True
            "checked_at": ISO8601,
        }
    """
    # config 参数保留扩展位（未来可加阈值覆盖等配置），暂未使用
    _ = config or {}
    scope = str(args.get("check_scope", "all")).lower()
    target_services = str(args.get("target_services", "")).strip()
    target_ports = str(args.get("target_ports", _DEFAULT_TARGET_PORTS)).strip()
    disk_path = str(args.get("disk_path", "/" if os.name != "nt" else "C:\\"))
    host = str(args.get("host", "127.0.0.1"))

    result: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "abnormal": False,
    }

    # task 范围只查失败 run，留给 list_failed_runs 处理；这里返回空字段保持结构稳定
    if scope == "task":
        result["scope"] = "task"
        result["note"] = "task scope 请调用 list_failed_runs 工具查失败 run"
        return result

    # 1. 进程检测
    if scope in ("all", "process"):
        processes: list[dict[str, Any]] = []
        if target_services:
            for svc in target_services.split(","):
                svc = svc.strip()
                if not svc:
                    continue
                patterns = _SERVICE_PROCESS_MAP.get(svc, [svc])
                samples = _find_processes_by_name(patterns)
                found = bool(samples) and not any("error" in s for s in samples)
                if not found:
                    result["abnormal"] = True
                processes.append({
                    "service": svc,
                    "found": found,
                    "count": len(samples) if found else 0,
                    "samples": samples[:5],  # 截前 5 个避免响应过大
                })
        result["processes"] = processes

    # 2. 端口检测
    if scope in ("all", "port"):
        ports: list[dict[str, Any]] = []
        for port in _parse_int_csv(target_ports):
            reachable, err = _check_port_reachable(host, port)
            if not reachable:
                result["abnormal"] = True
            ports.append({"port": port, "host": host, "reachable": reachable, "error": err})
        result["ports"] = ports

    # 3. 磁盘使用率
    if scope in ("all", "disk"):
        disk = _probe_disk(disk_path)
        if disk.get("abnormal"):
            result["abnormal"] = True
        result["disk"] = disk

    # 4. 内存使用率
    if scope in ("all", "memory"):
        memory = _probe_memory()
        if memory.get("abnormal"):
            result["abnormal"] = True
        result["memory"] = memory

    # 5. CPU 使用率
    if scope in ("all", "cpu"):
        cpu = _probe_cpu()
        if cpu.get("abnormal"):
            result["abnormal"] = True
        result["cpu"] = cpu

    result["scope"] = scope
    logger.info(
        "system_probe 完成 scope=%s abnormal=%s services=%s ports=%s",
        scope, result["abnormal"], target_services, target_ports,
    )
    return result


# ── DAG run 异常查询（task_monitor agent probe 节点用）───────

# 默认 audit.db 路径（项目根目录）
_DEFAULT_AUDIT_DB_PATH = "audit.db"


def _parse_time_range_hours(time_range: str) -> int:
    """解析时间范围字符串为小时数（1h/24h/7d 等）。

    无法解析时回退到 1 小时。
    """
    tr = (time_range or "1h").strip().lower()
    m = re.match(r"^(\d+)\s*(h|d|w|m)$", tr)
    if not m:
        return 1
    num = int(m.group(1))
    unit = m.group(2)
    if unit == "h":
        return num
    if unit == "d":
        return num * 24
    if unit == "w":
        return num * 24 * 7
    if unit == "m":
        # 分钟换算为小时（向上取整，至少 1）
        return max(1, (num + 59) // 60)
    return 1


def _resolve_audit_db_path(config: dict[str, Any] | None) -> str:
    """从 config 解析 audit.db 实际路径。

    优先级：config.audit_db_path > 环境变量 AGENTOPS_AUDIT_DB > 默认 audit.db
    """
    if config:
        p = config.get("audit_db_path")
        if p:
            return str(p)
    env_path = os.environ.get("AGENTOPS_AUDIT_DB")
    if env_path:
        return env_path
    return _DEFAULT_AUDIT_DB_PATH


async def list_failed_runs(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """查询最近失败的 DAG run + 节点失败事件（task_monitor agent probe 节点用）。

    直接读 audit.db，不依赖 _orchestrator 内存状态（巡检 agent 可能在子进程）。

    Args:
        args: {"time_range": "1h", "limit": 20}
        config: {"audit_db_path": "audit.db"}  # 可选，覆盖默认路径

    Returns:
        {
            "failed_runs": [{run_id, workflow_id, status, started_at, finished_at, error}],
            "failed_nodes": [{run_id, node_id, occurred_at, payload}],
            "total": int,
            "time_range": "1h",
            "audit_db_path": str,
        }
    """
    cfg = config or {}
    time_range = str(args.get("time_range", "1h"))
    hours = _parse_time_range_hours(time_range)
    limit = int(args.get("limit", 20))
    limit = max(1, min(limit, 200))  # 防过大查询

    db_path = _resolve_audit_db_path(cfg)
    result: dict[str, Any] = {
        "failed_runs": [],
        "failed_nodes": [],
        "total": 0,
        "time_range": time_range,
        "hours": hours,
        "audit_db_path": db_path,
    }

    if not Path(db_path).exists():
        logger.warning("list_failed_runs: audit.db 不存在 path=%s", db_path)
        result["error"] = f"audit.db 不存在: {db_path}"
        return result

    # sqlite3 同步接口在协程中阻塞，但 audit.db 是本地小文件，N=200 内 < 50ms，可接受
    # 如未来量级增长，可改为 asyncio.to_thread
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        logger.error("list_failed_runs: 连接 audit.db 失败: %s", e)
        result["error"] = f"连接 audit.db 失败: {e}"
        return result

    try:
        # 1. 查 failed 状态的 run（按 started_at 过滤时间范围，按时间倒序）
        #    SQLite datetime('now', '-N hours') 用 UTC
        rows = conn.execute(
            "SELECT run_id, workflow_id, run_mode, agent_id, status, started_at, finished_at, error "
            "FROM runs WHERE status='failed' "
            "AND started_at >= datetime('now', ?) "
            "ORDER BY started_at DESC LIMIT ?",
            (f"-{hours} hours", limit),
        ).fetchall()
        failed_runs: list[dict[str, Any]] = []
        for r in rows:
            failed_runs.append({
                "run_id": r["run_id"],
                "workflow_id": r["workflow_id"],
                "run_mode": r["run_mode"],
                "agent_id": r["agent_id"],
                "status": r["status"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "error": r["error"],
            })

        # 2. 查 dag_events 中的 node.failed 事件（同一时间范围）
        node_rows = conn.execute(
            "SELECT run_id, node_id, occurred_at, payload "
            "FROM dag_events WHERE event_type='node.failed' "
            "AND occurred_at >= datetime('now', ?) "
            "ORDER BY occurred_at DESC LIMIT ?",
            (f"-{hours} hours", limit),
        ).fetchall()
        failed_nodes: list[dict[str, Any]] = []
        for r in node_rows:
            # payload 是 JSON 字符串，尝试解析失败则原样保留
            payload_raw = r["payload"]
            try:
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            except (ValueError, TypeError):
                payload = payload_raw
            failed_nodes.append({
                "run_id": r["run_id"],
                "node_id": r["node_id"],
                "occurred_at": r["occurred_at"],
                "payload": payload,
            })

        result["failed_runs"] = failed_runs
        result["failed_nodes"] = failed_nodes
        result["total"] = len(failed_runs) + len(failed_nodes)
        logger.info(
            "list_failed_runs 完成 hours=%s failed_runs=%d failed_nodes=%d total=%d",
            hours, len(failed_runs), len(failed_nodes), result["total"],
        )
        return result
    except sqlite3.Error as e:
        logger.error("list_failed_runs: 查询失败: %s", e)
        result["error"] = f"查询失败: {e}"
        return result
    finally:
        conn.close()
