"""服务器连接 + 日志拉取任务管理（前端「凭据管理」两个 Tab 的后端支撑）。

职责：
- 读写 ~/.agentops/private/log-pull.yaml（敏感文件，不进 git）：
  connections 段（服务器连接对象）+ pull_sources 段（拉取任务）
- credential_id 归一化：空 / None / 字符串 "None" → ssh:<connection_id>
  （修复历史 bug：patrol.yaml 时代 `str(None)` 落盘导致凭据查找落空）
- 删除连接时校验"无 pull_source 引用"，否则拒绝
- test_connection：paramiko 建连后立即断开，返回 ok/耗时/错误
- 校验：pull_source 的 local.log_source_id 必须在 patrol.yaml 的 log_sources 白名单
- 拉取计划已迁移至 config/schedules.yaml（见 schedules_admin.py），本模块不再管理计划

既有策略：配置不做热加载（避免运行中定时器漂移），回写后重启后端生效。
设计文档：docs/product-design/DESIGN_config_credential_refactor_v1.md §6.1
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

# private 配置（敏感：真实 IP/端口/用户名/远程路径，不进 git）
PRIVATE_YAML = Path.home() / ".agentops" / "private" / "log-pull.yaml"
# patrol.yaml（仅剩 log_sources 白名单等非敏感段）
PATROL_YAML = Path(__file__).resolve().parents[1] / "config" / "patrol.yaml"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class LogPullConfigError(ValueError):
    """配置校验失败（API 层转 400；被引用删除转 409 由 API 层判断 status 字段处理）。"""


# ── credential_id 归一化 ─────────────────────────────────────

# 连接类型白名单（conn_type 字段，缺省 ssh 向后兼容）
CONN_TYPES = ("ssh", "mysql")


def normalize_credential_id(raw: Any, connection_id: str, conn_type: str = "ssh") -> str:
    """空 / None / 字符串 "None" / 纯空白 → <conn_type>:<connection_id>，其余原样返回。

    历史 bug：patrol.yaml 时代前端传 null，后端 str(None) 落盘成字符串 "None"，
    truthy 不触发运行时兜底，凭据查找 get("None") 落空。归一化统一收敛。

    conn_type 缺省 ssh（向后兼容既有连接对象）；mysql 连接凭据前缀为 mysql:。
    """
    s = str(raw).strip() if raw is not None else ""
    if not s or s == "None":
        return f"{conn_type}:{connection_id}"
    return s


# ── ruamel round-trip（保留注释与整体结构）──────────────────────

def _load_yaml(path: Path) -> Any:
    yaml_io = YAML()
    yaml_io.preserve_quotes = True
    # 读纯文本再 load（Windows 下把文件流交给 ruamel 会持有句柄，导致后续 os.replace 失败）
    text = path.read_text(encoding="utf-8")
    return yaml_io.load(text)


def _dump_yaml(data: Any, path: Path) -> None:
    yaml_io = YAML()
    yaml_io.preserve_quotes = True
    yaml_io.width = 4096  # 避免长行被折叠
    # 序列化到内存，再原子写（先临时文件再替换，防止写一半损坏配置）
    buf = io.StringIO()
    yaml_io.dump(data, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix="logpull_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(buf.getvalue())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _read_doc(path: Path | None = None) -> Any:
    """读 private/log-pull.yaml；不存在时返回含空列表的骨架。"""
    p = Path(path) if path else PRIVATE_YAML
    if not p.exists():
        return {"connections": [], "pull_sources": []}
    data = _load_yaml(p) or {}
    if data.get("connections") is None:
        data["connections"] = []
    if data.get("pull_sources") is None:
        data["pull_sources"] = []
    return data


def _write_doc(data: Any, path: Path | None = None) -> None:
    _dump_yaml(data, Path(path) if path else PRIVATE_YAML)


# ── 服务器连接 CRUD ─────────────────────────────────────────

def _mask_host(host: str) -> str:
    """IP/主机名脱敏显示（192.168.1.100 → 192.168.*.*）。"""
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.*.*"
    if len(host) > 4:
        return host[:2] + "***"
    return host


def list_connections(path: Path | None = None) -> list[dict[str, Any]]:
    """连接对象列表（含凭据状态与引用方，host 全量返回——编辑表单需要）。"""
    data = _read_doc(path)
    sources = data.get("pull_sources") or []
    result = []
    for c in data.get("connections") or []:
        auth = c.get("auth") or {}
        cid = c.get("id", "")
        conn_type = c.get("conn_type", "ssh")
        credential_id = normalize_credential_id(auth.get("credential_id"), cid, conn_type)
        credential_present = False
        try:
            from orchestrator.credential_store import get_credential_store
            credential_present = get_credential_store().get(credential_id) is not None
        except Exception as e:
            logger.warning("credential_store 查询失败（%s）: %s", credential_id, e)
        result.append({
            "id": cid,
            "name": c.get("name", ""),
            "conn_type": conn_type,
            "host": c.get("host", ""),
            "port": int(c.get("port", 22 if conn_type == "ssh" else 3306)),
            "username": c.get("username", ""),
            "database": c.get("database", ""),
            "auth_type": auth.get("type", "key"),
            "credential_id": credential_id,
            "credential_present": credential_present,
            "private_key_path": auth.get("private_key_path", ""),
            "enabled": bool(c.get("enabled", True)),
            "referenced_by": [
                s.get("id", "") for s in sources
                if s.get("connection_id") == cid
            ],
        })
    return result


def upsert_connection(p: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """新增/更新连接对象（按 id upsert，回写 private/log-pull.yaml）。

    conn_type 支持 ssh（默认，向后兼容）/ mysql。mysql 连接强制 password 认证，
    携带 database 字段，凭据前缀 mysql:<id>。
    """
    data = _read_doc(path)
    conn_id = str(p.get("id", "")).strip()
    if not _ID_RE.match(conn_id):
        raise LogPullConfigError(f"连接 ID 只能含字母/数字/._- 且以字母数字开头：{conn_id!r}")
    name = str(p.get("name", "")).strip() or conn_id
    host = str(p.get("host", "")).strip()
    if not host:
        raise LogPullConfigError("服务器地址不能为空")

    conn_type = str(p.get("conn_type", "ssh")).strip() or "ssh"
    if conn_type not in CONN_TYPES:
        raise LogPullConfigError(f"连接类型只支持 {' / '.join(CONN_TYPES)}：{conn_type!r}")

    default_port = 22 if conn_type == "ssh" else 3306
    port = int(p.get("port", default_port))
    if not (1 <= port <= 65535):
        raise LogPullConfigError("端口必须在 1-65535")

    username = str(p.get("username", "")).strip()
    if not username:
        raise LogPullConfigError("用户名不能为空")

    if conn_type == "mysql":
        # MySQL 连接只支持密码认证（本项目首版仅接 MySQL 密码凭据）
        auth_type = "password"
        database = str(p.get("database", "")).strip()
    else:
        auth_type = str(p.get("auth_type", "key"))
        if auth_type not in ("key", "password"):
            raise LogPullConfigError("认证方式只支持 key / password")
        database = ""

    # 归一化：杜绝 str(None) 落盘；前缀随 conn_type
    credential_id = normalize_credential_id(p.get("credential_id"), conn_id, conn_type)

    auth: dict[str, Any] = {"type": auth_type, "credential_id": credential_id}
    if auth_type == "key":
        key_path = str(p.get("private_key_path", "")).strip()
        auth["private_key_path"] = key_path or f"~/.agentops/ssh/{conn_id}.key"

    node: dict[str, Any] = {
        "id": conn_id,
        "name": name,
        "conn_type": conn_type,
        "host": host,
        "port": port,
        "username": username,
        "auth": auth,
        "enabled": bool(p.get("enabled", True)),
    }
    if conn_type == "mysql" and database:
        node["database"] = database

    conns = data["connections"]
    for i, existing in enumerate(conns):
        if existing.get("id") == conn_id:
            conns[i] = node  # 更新
            break
    else:
        conns.append(node)  # 新增
    _write_doc(data, path)
    return {"id": conn_id, "status": "stored"}


def delete_connection(conn_id: str, path: Path | None = None) -> dict[str, Any]:
    """删除连接对象；被 pull_source 引用时抛错（API 层转 409）。"""
    data = _read_doc(path)
    conns = data.get("connections") or []
    remaining = [c for c in conns if c.get("id") != conn_id]
    if len(remaining) == len(conns):
        raise LogPullConfigError(f"连接不存在：{conn_id}")
    referenced_by = [
        s.get("id", "") for s in (data.get("pull_sources") or [])
        if s.get("connection_id") == conn_id
    ]
    if referenced_by:
        raise ReferencedConnectionError(conn_id, referenced_by)
    data["connections"] = remaining
    _write_doc(data, path)
    return {"id": conn_id, "status": "deleted"}


class ReferencedConnectionError(LogPullConfigError):
    """连接被拉取任务引用，拒绝删除（API 层转 409）。"""

    def __init__(self, conn_id: str, referenced_by: list[str]):
        self.conn_id = conn_id
        self.referenced_by = referenced_by
        super().__init__(
            f"连接 '{conn_id}' 被拉取任务引用，请先删除或改绑：{', '.join(referenced_by)}"
        )


# ── 连接测试（paramiko 建连后立即断开）────────────────────────

def _build_connect_kwargs(conn: dict[str, Any], secret: str | None) -> tuple[dict[str, Any], str | None]:
    """按连接对象 + 凭据拼装 paramiko connect 参数；返回 (kwargs, error)。"""
    auth = conn.get("auth") or {}
    kwargs: dict[str, Any] = {
        "hostname": conn.get("host", ""),
        "port": int(conn.get("port", 22)),
        "username": conn.get("username", ""),
        "timeout": 20, "banner_timeout": 20, "auth_timeout": 20,
    }
    if auth.get("type") == "key":
        key_path = Path(auth.get("private_key_path", "")).expanduser()
        if not key_path.exists():
            return {}, f"私钥文件不存在: {key_path}（请手动放置，权限 0600）"
        kwargs["key_filename"] = str(key_path)
        if secret:
            kwargs["passphrase"] = secret
    else:
        if not secret:
            return {}, (
                f"password 模式但 credential_store 无凭据 "
                f"'{auth.get('credential_id', '')}'（请先在服务器连接 Tab 录入）"
            )
        kwargs["password"] = secret
    return kwargs, None


def test_connection(conn_id: str, path: Path | None = None) -> dict[str, Any]:
    """连接测试：按 conn_type 分流（ssh→paramiko / mysql→pymysql）。返回 {ok, latency_ms, error}。

    同步阻塞函数（网络 IO），API 层用 asyncio.to_thread 调用。
    """
    data = _read_doc(path)
    conn = next(
        (c for c in (data.get("connections") or []) if c.get("id") == conn_id), None,
    )
    if not conn:
        raise LogPullConfigError(f"连接不存在：{conn_id}")

    conn_type = conn.get("conn_type", "ssh")
    secret: str | None = None
    cred_id = normalize_credential_id((conn.get("auth") or {}).get("credential_id"), conn_id, conn_type)
    try:
        from orchestrator.credential_store import get_credential_store
        secret = get_credential_store().get(cred_id)
    except Exception as e:
        logger.warning("credential_store 读取失败（%s）: %s", cred_id, e)

    if conn_type == "mysql":
        return _test_mysql_connection(conn, secret)
    return _test_ssh_connection(conn, secret)


def _test_ssh_connection(conn: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """paramiko 建连 → 立即断开（ssh 类型连接测试）。"""
    kwargs, err = _build_connect_kwargs(conn, secret)
    if err:
        return {"ok": False, "latency_ms": None, "error": err}

    try:
        import paramiko
    except ImportError:
        return {"ok": False, "latency_ms": None,
                "error": "missing_dependency: paramiko 未安装，执行 pip install paramiko>=3.4"}

    started = time.monotonic()
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(**kwargs)
        client.close()
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000), "error": None}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e)}


def _test_mysql_connection(conn: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """pymysql 建连 → 立即断开（mysql 类型连接测试）。

    仅做 TCP + 认证握手（不跑 SQL），验证 host/port/username/password 可用。
    """
    if not secret:
        return {"ok": False, "latency_ms": None,
                "error": "password 模式但 credential_store 无凭据（请先在数据库连接 Tab 录入密码）"}

    try:
        import pymysql
    except ImportError:
        return {"ok": False, "latency_ms": None,
                "error": "missing_dependency: pymysql 未安装，执行 pip install pymysql"}

    started = time.monotonic()
    try:
        kwargs: dict[str, Any] = {
            "host": conn.get("host", ""),
            "port": int(conn.get("port", 3306)),
            "user": conn.get("username", ""),
            "password": secret,
            "connect_timeout": 5,
        }
        db = conn.get("database", "")
        if db:
            kwargs["database"] = db
        db_conn = pymysql.connect(**kwargs)
        db_conn.close()
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000), "error": None}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e)}


# ── 拉取任务 CRUD ───────────────────────────────────────────

def list_pull_sources(path: Path | None = None) -> list[dict[str, Any]]:
    """拉取任务列表（join 连接对象显示信息，host 脱敏）。

    schedules 字段：引用该源的统一计划（含下次触发时间，读 config/schedules.yaml）。
    """
    from orchestrator.schedules_admin import list_schedules, next_cron_run

    data = _read_doc(path)
    conns_by_id = {c.get("id", ""): c for c in (data.get("connections") or [])}
    all_schedules = list_schedules()
    result = []
    for s in data.get("pull_sources") or []:
        sid = s.get("id", "")
        conn = conns_by_id.get(s.get("connection_id", ""))
        linked = []
        for sc in all_schedules:
            if (sc.get("inputs") or {}).get("pull_source_id") != sid:
                continue
            linked.append({
                "name": sc.get("name", ""),
                "cron": sc.get("cron", ""),
                "enabled": bool(sc.get("enabled", True)),
                "next_run": (
                    next_cron_run(sc["cron"]).isoformat()
                    if sc.get("enabled") and sc.get("cron") else None
                ),
            })
        result.append({
            "id": sid,
            "name": s.get("name", ""),
            "connection_id": s.get("connection_id", ""),
            "connection": {
                "name": (conn or {}).get("name", ""),
                "host_masked": _mask_host((conn or {}).get("host", "")),
            } if conn else None,
            "remote_paths": list((s.get("remote") or {}).get("paths") or []),
            "local_log_source_id": (s.get("local") or {}).get("log_source_id", ""),
            "local_max_days": int((s.get("retention") or {}).get("local_max_days", 7)),
            "enabled": bool(s.get("enabled", False)),
            "schedules": linked,
        })
    return result


def list_log_source_ids() -> list[str]:
    """log_sources 白名单 id 列表（前端下拉框用；白名单仍在 patrol.yaml）。"""
    data = _load_yaml(PATROL_YAML) if PATROL_YAML.exists() else {}
    return [ls.get("id", "") for ls in (data.get("log_sources") or []) if ls.get("id")]


# ── log_sources 白名单 CRUD（本地日志目录管理；白名单留在 patrol.yaml，是 pull_logs/ops_tools 的读取依赖）──

def _load_patrol_doc() -> dict[str, Any]:
    """读 patrol.yaml（dict，缺文件给空结构）。"""
    return _load_yaml(PATROL_YAML) if PATROL_YAML.exists() else {}


def _dump_patrol_doc(data: dict[str, Any]) -> None:
    _dump_yaml(data, PATROL_YAML)


def list_log_sources_detail(path: Path | None = None) -> list[dict[str, Any]]:
    """本地日志目录列表（含被哪些拉取任务引用）。"""
    patrol = _load_patrol_doc()
    private = _read_doc(path)
    refs: dict[str, list[str]] = {}
    for s in private.get("pull_sources") or []:
        lid = (s.get("local") or {}).get("log_source_id", "")
        if lid:
            refs.setdefault(lid, []).append(s.get("id", ""))
    result = []
    for ls in patrol.get("log_sources") or []:
        if not ls.get("id"):
            continue
        result.append({
            "id": ls.get("id", ""),
            "name": ls.get("name", ""),
            "path": ls.get("path", ""),
            "description": ls.get("description", ""),
            "allow_read": bool(ls.get("allow_read", True)),
            "allow_list": bool(ls.get("allow_list", True)),
            "referenced_by": refs.get(ls.get("id", ""), []),
        })
    return result


def upsert_log_source(p: dict[str, Any]) -> dict[str, Any]:
    """新增/更新本地日志目录（按 id upsert，回写 patrol.yaml log_sources 白名单）。"""
    source_id = str(p.get("id", "")).strip()
    if not _ID_RE.match(source_id):
        raise LogPullConfigError(f"目录 ID 只能含字母/数字/._- 且以字母数字开头：{source_id!r}")
    path = str(p.get("path", "")).strip()
    if not path:
        raise LogPullConfigError("本地存储路径不能为空")

    patrol = _load_patrol_doc()
    entries = patrol.setdefault("log_sources", [])
    existed = any(ls.get("id") == source_id for ls in entries)
    node = {
        "id": source_id,
        "name": str(p.get("name", "")).strip() or source_id,
        "path": path,
        "description": str(p.get("description", "")).strip(),
        "allow_read": bool(p.get("allow_read", True)),
        "allow_list": bool(p.get("allow_list", True)),
    }
    if existed:
        entries[:] = [node if ls.get("id") == source_id else ls for ls in entries]
    else:
        entries.append(node)
    _dump_patrol_doc(patrol)
    return {"id": source_id, "status": "updated" if existed else "created"}


def delete_log_source(source_id: str, path: Path | None = None) -> dict[str, Any]:
    """删除本地日志目录。被拉取任务引用时拒绝（引用缺失会导致 pull_logs 加载报错）。"""
    patrol = _load_patrol_doc()
    entries = patrol.get("log_sources") or []
    if not any(ls.get("id") == source_id for ls in entries):
        raise LogPullConfigError(f"本地日志目录不存在：{source_id!r}")

    private = _read_doc(path)
    refs = [
        s.get("id", "") for s in (private.get("pull_sources") or [])
        if (s.get("local") or {}).get("log_source_id") == source_id
    ]
    if refs:
        raise ReferencedConnectionError(source_id, refs)

    patrol["log_sources"] = [ls for ls in entries if ls.get("id") != source_id]
    _dump_patrol_doc(patrol)
    return {"id": source_id, "status": "deleted"}


def upsert_source(p: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """新增/更新拉取任务（按 id upsert，回写 private/log-pull.yaml）。

    连接参数（host/port/username/auth）不再接收——由 connection_id 引用连接对象。
    """
    data = _read_doc(path)
    source_id = str(p.get("id", "")).strip()
    if not _ID_RE.match(source_id):
        raise LogPullConfigError(f"任务 ID 只能含字母/数字/._- 且以字母数字开头：{source_id!r}")
    connection_id = str(p.get("connection_id", "")).strip()
    if connection_id not in [c.get("id") for c in (data.get("connections") or [])]:
        raise LogPullConfigError(f"connection_id 必须是已配置的连接对象：{connection_id!r}")
    remote_paths = [str(x).strip() for x in (p.get("remote_paths") or []) if str(x).strip()]
    if not remote_paths:
        raise LogPullConfigError("远程抽取目录至少填一个")
    local_id = str(p.get("local_log_source_id", "")).strip()
    whitelist = list_log_source_ids()
    if local_id not in whitelist:
        raise LogPullConfigError(
            f"本地日志目录 log_source_id 必须是 log_sources 白名单已有 id：{local_id!r}"
        )
    max_days = int(p.get("local_max_days", 7))
    if max_days < 1:
        raise LogPullConfigError("本地保留天数至少 1 天")

    node = {
        "id": source_id,
        "name": str(p.get("name", "")).strip() or source_id,
        "connection_id": connection_id,
        "remote": {"paths": remote_paths},
        "local": {"log_source_id": local_id},
        "retention": {"local_max_days": max_days},
        "enabled": bool(p.get("enabled", False)),
    }
    sources = data["pull_sources"]
    for i, existing in enumerate(sources):
        if existing.get("id") == source_id:
            sources[i] = node  # 更新
            break
    else:
        sources.append(node)  # 新增
    _write_doc(data, path)
    return {"id": source_id, "status": "stored"}


def delete_source(source_id: str, path: Path | None = None,
                  schedules_path: Path | None = None) -> dict[str, Any]:
    """删除拉取任务（级联删除引用它的统一计划，计划在 config/schedules.yaml）。"""
    from orchestrator.schedules_admin import delete_schedules_by_pull_source

    data = _read_doc(path)
    sources = data.get("pull_sources") or []
    remaining = [s for s in sources if s.get("id") != source_id]
    if len(remaining) == len(sources):
        raise LogPullConfigError(f"拉取源不存在：{source_id}")
    data["pull_sources"] = remaining
    _write_doc(data, path)
    removed_schedules = delete_schedules_by_pull_source(source_id, schedules_path)
    return {"id": source_id, "status": "deleted", "removed_schedules": removed_schedules}


# ── 供 tools/pull_logs.py 复用的读取助手 ───────────────────────

def load_pull_source_with_connection(
    pull_source_id: str, path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """加载拉取任务及其引用的连接对象；任一不存在返回 None。"""
    data = _read_doc(path)
    src = next(
        (s for s in (data.get("pull_sources") or []) if s.get("id") == pull_source_id),
        None,
    )
    if not src:
        return None
    conn = next(
        (c for c in (data.get("connections") or [])
         if c.get("id") == src.get("connection_id")),
        None,
    )
    if not conn:
        return None
    return src, conn
