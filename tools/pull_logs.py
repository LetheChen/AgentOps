"""pull_logs — 远程日志拉取工具（log-puller workflow 消费）。

对应 config/tools/pull_logs.yaml。
按 ~/.agentops/private/log-pull.yaml 的 pull_sources 配置（引用 connections 连接对象），
从远程服务器 SFTP 拉取日志到本地 log_sources 白名单目录（复用 ops_tools 白名单校验，防路径遍历）。

设计要点：
- 传输用 paramiko SFTP 纯库实现（Windows 本机无 rsync；scp.exe 非交互输密码无 sshpass）
- 增量拉取：按每个远程文件的 (size, mtime) 位点过滤，只拉变化的文件
- 位点存 workspace/log-puller/state/<source_id>.json（跨 run 持久）
- 凭据从 credential_store 解密（id 形如 ssh:<connection_id>，Fernet 加密；
  迁移时已归一化，历史 "None" 字符串 bug 不再出现）
- 失败不抛异常，返回 status=error 的 JSON（report 节点据此 emit_alert）
- paramiko 缺依赖时返回 missing_dependency 错误（照 knowledge 依赖模式）

安全：
- 落盘目标必须是 log_sources 白名单已有 id（配置加载时已 fail-fast，此处双重校验）
- 工具只认配置里的 pull_source_id，不接受任意 host/path 参数（防越权）
"""
from __future__ import annotations

import fnmatch
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATE_DIR = Path("workspace/log-puller/state")


def _remote_glob(sftp: Any, pattern: str) -> list[str]:
    """SFTP 远程 glob：把 /dir/*.log 拆成目录列表 + fnmatch 匹配。"""
    pattern = pattern.rstrip("/")
    parent, _, name_pat = pattern.rpartition("/")
    if not name_pat:
        return []
    if not any(ch in name_pat for ch in "*?["):
        # 无通配符，直接判断文件是否存在
        try:
            sftp.stat(pattern)
            return [pattern]
        except FileNotFoundError:
            return []
    try:
        entries = sftp.listdir_attr(parent or "/")
    except FileNotFoundError:
        return []
    return sorted(
        f"{parent}/{e.filename}" for e in entries
        if fnmatch.fnmatch(e.filename, name_pat) and not e.filename.startswith(".")
    )


def _local_target_name(remote_path: str, used_names: dict[str, str]) -> str:
    """本地落盘文件名：默认 basename；同 basename 不同远程目录冲突时用全路径 sanitized。"""
    name = remote_path.rsplit("/", 1)[-1]
    if name in used_names and used_names[name] != remote_path:
        return remote_path.lstrip("/").replace("/", "_")
    used_names[name] = remote_path
    return name


def pull_logs(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """按 private/log-pull.yaml 配置从远程服务器 SFTP 拉取日志到本地白名单目录。

    Args:
        args: {"pull_source_id": "prod-seeyon"}
        config: handler 配置 {"timeout_seconds": 120}
                （连接参数固定读 ~/.agentops/private/log-pull.yaml，不接受路径覆盖）

    Returns:
        {
            "pull_source_id": str,
            "status": "ok" | "error",
            "files_pulled": int, "files_skipped": int, "bytes_pulled": int,
            "duration_seconds": float,
            "errors": [str], "local_dir": str
        }
    """
    from orchestrator.log_pull_admin import load_pull_source_with_connection

    pull_source_id = (args or {}).get("pull_source_id", "")
    started = time.monotonic()

    def _result(status: str, errors: list[str], **extra: Any) -> dict[str, Any]:
        return {
            "pull_source_id": pull_source_id,
            "status": status,
            "files_pulled": 0,
            "files_skipped": 0,
            "bytes_pulled": 0,
            "duration_seconds": round(time.monotonic() - started, 2),
            "errors": errors,
            **extra,
        }

    # 1. 解析 pull_source + connection（private/log-pull.yaml，敏感配置）
    loaded = load_pull_source_with_connection(pull_source_id)
    if not loaded:
        return _result("error", [f"pull_source_id '{pull_source_id}' 未在 ~/.agentops/private/log-pull.yaml 中配置（或引用的 connection 不存在）"])
    src, conn = loaded
    if not src.get("enabled", False):
        return _result("error", [f"拉取源 '{pull_source_id}' 已禁用（enabled=false）"])
    if not conn.get("enabled", True):
        return _result("error", [f"连接对象 '{src.get('connection_id', '')}' 已禁用（enabled=false）"])

    remote_paths = (src.get("remote") or {}).get("paths") or []
    local_source_id = (src.get("local") or {}).get("log_source_id", "")
    max_days = int((src.get("retention") or {}).get("local_max_days", 0))
    if not (remote_paths and local_source_id):
        return _result("error", [f"拉取源 '{pull_source_id}' 配置不完整（remote.paths/local.log_source_id）"])

    # 2. 双重校验：local.log_source_id 必须在 log_sources 白名单（复用 ops_tools 校验）
    from tools.ops_tools import _load_log_sources
    log_sources = _load_log_sources()
    local_src = log_sources.get(local_source_id)
    if not local_src:
        return _result("error", [f"local.log_source_id '{local_source_id}' 不在 log_sources 白名单，拒绝落盘"])
    if not local_src.get("allow_read", True):
        return _result("error", [f"log_source '{local_source_id}' allow_read=false，拒绝落盘"])
    local_dir = Path(local_src["path"]).expanduser()
    local_dir.mkdir(parents=True, exist_ok=True)

    # 3. 解密凭据（credential_id 在迁移/CRUD 时已归一化为 ssh:<connection_id>）
    from orchestrator.log_pull_admin import normalize_credential_id
    auth = conn.get("auth") or {}
    cred_id = normalize_credential_id(auth.get("credential_id"), conn.get("id", ""))
    secret: str | None = None
    try:
        from orchestrator.credential_store import get_credential_store
        secret = get_credential_store().get(cred_id)
    except Exception as e:
        logger.warning("credential_store 读取失败（%s）: %s", cred_id, e)

    # 4. paramiko 延迟导入（缺依赖时返回 missing_dependency，不炸节点）
    try:
        import paramiko
    except ImportError:
        return _result("error", ["missing_dependency: paramiko 未安装，执行 pip install paramiko>=3.4"])

    # 5. 建连（连接参数拼装复用 log_pull_admin，与 test_connection 同一逻辑）
    from orchestrator.log_pull_admin import _build_connect_kwargs
    connect_kwargs, err = _build_connect_kwargs(conn, secret)
    if err:
        return _result("error", [err])
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(**connect_kwargs)
    except Exception as e:
        return _result("error", [f"SFTP 连接失败 {conn.get('host', '')}:{conn.get('port', 22)}: {e}"])

    # 6. 增量拉取
    state_path = _STATE_DIR / f"{pull_source_id}.json"
    state: dict[str, dict[str, float]] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    files_pulled = 0
    files_skipped = 0
    bytes_pulled = 0
    errors: list[str] = []
    used_names: dict[str, str] = {}
    new_state: dict[str, dict[str, float]] = {}

    try:
        sftp = client.open_sftp()
        try:
            for pattern in remote_paths:
                try:
                    matches = _remote_glob(sftp, pattern)
                except Exception as e:
                    errors.append(f"远程路径展开失败 {pattern}: {e}")
                    continue
                if not matches:
                    errors.append(f"远程路径无匹配文件: {pattern}")
                    continue
                for rpath in matches:
                    try:
                        st = sftp.stat(rpath)
                    except Exception as e:
                        errors.append(f"stat 失败 {rpath}: {e}")
                        continue
                    prev = state.get(rpath)
                    if prev and prev.get("size") == st.st_size and prev.get("mtime") == st.st_mtime:
                        files_skipped += 1
                        new_state[rpath] = {"size": st.st_size, "mtime": st.st_mtime}
                        continue
                    target = local_dir / _local_target_name(rpath, used_names)
                    try:
                        sftp.get(rpath, str(target))
                        files_pulled += 1
                        bytes_pulled += st.st_size
                        new_state[rpath] = {"size": st.st_size, "mtime": st.st_mtime}
                    except Exception as e:
                        errors.append(f"下载失败 {rpath}: {e}")
        finally:
            sftp.close()
    finally:
        client.close()

    # 7. 位点持久化（即使部分失败也保存成功的，避免下次全量重拉）
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(new_state, indent=2), encoding="utf-8")
    except Exception as e:
        errors.append(f"位点文件写入失败: {e}")

    # 8. 本地保留期清理（按 mtime，超 local_max_days 删除）
    cleaned = 0
    if max_days > 0:
        cutoff = time.time() - max_days * 86400
        for f in local_dir.iterdir():
            if f.is_file():
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        cleaned += 1
                except OSError:
                    continue

    return {
        "pull_source_id": pull_source_id,
        "status": "error" if errors and files_pulled == 0 else "ok",
        "files_pulled": files_pulled,
        "files_skipped": files_skipped,
        "bytes_pulled": bytes_pulled,
        "duration_seconds": round(time.monotonic() - started, 2),
        "local_dir": str(local_dir),
        "files_cleaned": cleaned,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
    }
