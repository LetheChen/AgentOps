"""ReportExporter — task_reports 多格式导出链路（v1）。

设计目标：
1. 导出流程：读 task_reports row → 格式转换 → 写文件 → 计算 SHA-256 → 落 task_report_exports 历史表
2. 格式处理：md（原始）/ html（最小可读 Markdown 渲染）/ json（含 metadata 的结构化包）
3. 存储验证：路径白名单（task_id + report_id 必须匹配 uuid 前缀正则）+ SHA-256 + 文件大小记录

存储位置：<workspace_root>/task_exports/<task_id>/<report_id>.<ext>
路径白名单约束：仅允许 [a-z0-9_] 字符，文件扩展名限枚举 {md, html, json}，杜绝路径穿越。

调用方式：
- API 层：ReportExporter.export(report_row, fmt="md|html|json") -> dict {path, sha256, size_bytes, exported_at}
- 链路验证：tests/test_report_export.py 全链路 E2E（提交 → 导出 → 文件存在 → hash 匹配 → 再导出幂等）
"""
from __future__ import annotations

import hashlib
import html as _html_lib
import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------- 路径安全白名单 ----------

# _gen_id 输出形如 "report_<uuid12>" / "task_<uuid12>"，限定下述正则。
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
# 任务 ID 例外：保留 "task_" + 日期 + uuid16 形如 "task_20260821_143015_abc123def456ghi7"
_TASK_ID_PATTERN = re.compile(r"^task_[0-9]{8}_[0-9]{6}_[0-9a-f]{4,32}$")

_ALLOWED_FORMATS = ("md", "html", "json")
_FORMAT_EXT_MAP = {"md": "md", "html": "html", "json": "json"}
_FORMAT_CONTENT_TYPE = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


def _is_safe_id(value: str, *, kind: str = "report") -> bool:
    """校验 ID 仅含安全字符，防止路径穿越（.. / / / \\）。"""
    if not value or not isinstance(value, str):
        return False
    if kind == "task":
        return bool(_TASK_ID_PATTERN.match(value))
    return bool(_ID_PATTERN.match(value))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_export_id() -> str:
    return f"export_{uuid.uuid4().hex[:12]}"


# ---------- Markdown → HTML（最小可读渲染） ----------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE_FENCE_RE = re.compile(r"^```")
_LIST_RE = re.compile(r"^(\s*)([-*])\s+(.*)$")


def _md_to_html(md_text: str) -> str:
    """最小 Markdown → HTML 渲染。

    不引第三方依赖：仅处理 # 标题、列表、段落、代码块；其余原样转义。
    目标：报告离线阅读体验（带样式、可双击打开），不是通用 Markdown 转换器。
    """
    lines: list[str] = []
    in_code = False
    in_ul = False

    def _close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            lines.append("</ul>")
            in_ul = False

    for raw in (md_text or "").splitlines():
        if _CODE_FENCE_RE.match(raw.strip()):
            if in_code:
                lines.append("</code></pre>")
                in_code = False
            else:
                _close_ul()
                lines.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            lines.append(_html_lib.escape(raw))
            continue

        m = _HEADING_RE.match(raw)
        if m:
            _close_ul()
            level = len(m.group(1))
            lines.append(f"<h{level}>{_html_lib.escape(m.group(2))}</h{level}>")
            continue

        m = _LIST_RE.match(raw)
        if m:
            if not in_ul:
                lines.append("<ul>")
                in_ul = True
            lines.append(f"<li>{_html_lib.escape(m.group(3))}</li>")
            continue

        _close_ul()
        if raw.strip():
            lines.append(f"<p>{_html_lib.escape(raw)}</p>")
        else:
            lines.append("")

    _close_ul()
    if in_code:
        lines.append("</code></pre>")
    return "\n".join(lines)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       max-width: 860px; margin: 32px auto; padding: 0 24px; line-height: 1.6; color: #222; }}
h1, h2, h3 {{ border-bottom: 1px solid #eee; padding-bottom: 4px; }}
pre {{ background: #f6f8fa; padding: 12px; border-radius: 4px; overflow-x: auto; }}
code {{ font-family: Menlo, Consolas, monospace; }}
.meta {{ color: #666; font-size: 13px; border-left: 3px solid #4a90e2; padding-left: 12px;
        margin-bottom: 24px; }}
.self-check {{ background: #f9fbe7; padding: 12px; border-radius: 4px; font-size: 13px; }}
</style>
</head>
<body>
<div class="meta">
  <strong>任务：</strong>{task_label}<br>
  <strong>报告 ID：</strong>{report_id}<br>
  <strong>提交者：</strong>{agent_id}<br>
  <strong>提交时间：</strong>{submitted_at}<br>
</div>
{body}
{self_check_block}
</body>
</html>
"""


# ---------- 导出器 ----------

class ReportExporter:
    """报告导出器：负责格式转换 + 文件落盘 + hash 计算 + 历史落库。

    依赖注入（不直接 import TaskStore 类）：
    - conn / db_lock：复用 audit.db 同连接（task_reports 已在那里）
    - workspace_root：导出文件根目录（默认 <PROJECT_ROOT>/workspace）

    历史表 task_report_exports 在 ensure_schema() 启动时幂等创建。
    """

    def __init__(self, *, conn: sqlite3.Connection, db_lock: threading.Lock,
                 workspace_root: Path):
        self._conn = conn
        self._db_lock = db_lock
        self._workspace_root = Path(workspace_root).resolve()
        self._exports_root = (self._workspace_root / "task_exports").resolve()

    # ---------- Schema ----------

    def ensure_schema(self) -> None:
        """幂等创建 task_report_exports 表（同步，建表 < 100ms 不放 to_thread）。"""
        with self._db_lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS task_report_exports ("
                " export_id   TEXT PRIMARY KEY,"
                " report_id   TEXT NOT NULL REFERENCES task_reports(report_id) ON DELETE CASCADE,"
                " task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,"
                " format      TEXT NOT NULL,"
                " path        TEXT NOT NULL,"
                " sha256      TEXT NOT NULL,"
                " size_bytes  INTEGER NOT NULL,"
                " exported_at TEXT NOT NULL,"
                " CHECK (format IN ('md','html','json'))"
                ")"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_report_exports_report "
                "ON task_report_exports(report_id, exported_at DESC)"
            )
            self._conn.commit()

    # ---------- 路径计算 ----------

    def _resolve_path(self, *, task_id: str, report_id: str, fmt: str) -> Path:
        """计算导出文件绝对路径，含安全校验。"""
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError(f"unsupported format: {fmt}（must be one of {_ALLOWED_FORMATS}）")
        if not _is_safe_id(task_id, kind="task"):
            raise ValueError(f"unsafe task_id: {task_id!r}")
        if not _is_safe_id(report_id, kind="report"):
            raise ValueError(f"unsafe report_id: {report_id!r}")
        target = (self._exports_root / task_id /
                  f"{report_id}.{_FORMAT_EXT_MAP[fmt]}").resolve()
        # 路径穿越兜底：即使正则通过，仍校验解析后路径仍在 exports_root 下
        try:
            target.relative_to(self._exports_root)
        except ValueError as e:
            raise ValueError(f"path traversal detected: {target}") from e
        return target

    # ---------- 格式化 ----------

    @staticmethod
    def _format_markdown(report: dict) -> bytes:
        return (report.get("content") or "").encode("utf-8")

    @staticmethod
    def _format_html(report: dict) -> bytes:
        body = _md_to_html(report.get("content") or "")
        self_check = report.get("acceptance_self_check") or {}
        if isinstance(self_check, str):
            try:
                self_check = json.loads(self_check)
            except json.JSONDecodeError:
                self_check = {}
        if self_check:
            self_check_block = (
                "<div class=\"self-check\"><strong>自检清单：</strong>"
                f"<pre>{_html_lib.escape(json.dumps(self_check, ensure_ascii=False, indent=2))}"
                "</pre></div>"
            )
        else:
            self_check_block = ""
        title = f"任务报告 {report.get('report_id', '')}"
        task_label = (report.get("task_id") or "")
        rendered = _HTML_TEMPLATE.format(
            title=_html_lib.escape(title),
            task_label=_html_lib.escape(task_label),
            report_id=_html_lib.escape(report.get("report_id") or ""),
            agent_id=_html_lib.escape(report.get("agent_id") or ""),
            submitted_at=_html_lib.escape(report.get("submitted_at") or ""),
            body=body,
            self_check_block=self_check_block,
        )
        return rendered.encode("utf-8")

    @staticmethod
    def _format_json(report: dict) -> bytes:
        payload = {
            "report_id": report.get("report_id"),
            "task_id": report.get("task_id"),
            "agent_id": report.get("agent_id"),
            "session_id": report.get("session_id"),
            "terminal_session_id": report.get("terminal_session_id"),
            "status": report.get("status"),
            "submitted_at": report.get("submitted_at"),
            "version": report.get("version"),
            "artifact_ids": report.get("artifact_ids") or [],
            "acceptance_self_check": report.get("acceptance_self_check") or {},
            "content": report.get("content") or "",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def _format(self, report: dict, fmt: str) -> bytes:
        if fmt == "md":
            return self._format_markdown(report)
        if fmt == "html":
            return self._format_html(report)
        if fmt == "json":
            return self._format_json(report)
        raise ValueError(f"unsupported format: {fmt}")

    # ---------- 文件写入 + 校验 ----------

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _record_export(self, *, export_id: str, report: dict, fmt: str,
                       path: Path, sha256: str, size: int) -> None:
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO task_report_exports "
                "(export_id, report_id, task_id, format, path, sha256, size_bytes, exported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (export_id, report["report_id"], report["task_id"], fmt,
                 str(path), sha256, size, _now_iso()),
            )
            self._conn.commit()

    # ---------- 主入口 ----------

    def export(self, report: dict, *, fmt: str) -> dict[str, Any]:
        """同步执行导出：格式转换 → 写文件 → 计算 hash → 落历史。

        返回 {export_id, path, sha256, size_bytes, format, exported_at}。
        重复调用同 fmt 会覆盖文件并新增一条历史（hash 必重算，确保文件实际写入）。
        """
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError(f"unsupported format: {fmt}")
        task_id = report["task_id"]
        report_id = report["report_id"]
        target = self._resolve_path(task_id=task_id, report_id=report_id, fmt=fmt)
        target.parent.mkdir(parents=True, exist_ok=True)

        data = self._format(report, fmt)
        # 写文件：临时文件 → rename 原子化，避免半写状态
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

        # 写后再算 hash（确认磁盘上的真实内容）
        sha = self._sha256_file(target)
        size = target.stat().st_size
        export_id = _gen_export_id()
        self._record_export(export_id=export_id, report=report, fmt=fmt,
                            path=target, sha256=sha, size=size)
        return {
            "export_id": export_id,
            "path": str(target),
            "sha256": sha,
            "size_bytes": size,
            "format": fmt,
            "exported_at": _now_iso(),
        }

    # ---------- 历史查询 ----------

    def list_exports(self, *, task_id: str, report_id: str) -> list[dict[str, Any]]:
        """列出指定报告的导出历史（按时间倒序）。"""
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT export_id, format, path, sha256, size_bytes, exported_at "
                "FROM task_report_exports WHERE report_id = ? AND task_id = ? "
                "ORDER BY exported_at DESC",
                (report_id, task_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def verify_export(self, *, task_id: str, report_id: str, fmt: str) -> dict[str, Any]:
        """重新计算磁盘文件的 hash，与最近一次同 fmt 导出的记录比对。

        返回 {verified, expected_sha256, actual_sha256, path}。
        用于：定时巡检 / 用户报告"下载文件打不开"时的诊断 / 存储验证。
        """
        target = self._resolve_path(task_id=task_id, report_id=report_id, fmt=fmt)
        if not target.exists():
            return {"verified": False, "reason": "file_missing",
                    "expected_sha256": None, "actual_sha256": None,
                    "path": str(target)}
        actual = self._sha256_file(target)
        expected = None
        with self._db_lock:
            row = self._conn.execute(
                "SELECT sha256 FROM task_report_exports "
                "WHERE report_id = ? AND task_id = ? AND format = ? "
                "ORDER BY exported_at DESC LIMIT 1",
                (report_id, task_id, fmt),
            ).fetchone()
        if row:
            expected = row["sha256"]
        return {"verified": (expected == actual),
                "expected_sha256": expected, "actual_sha256": actual,
                "path": str(target)}


def get_content_type(fmt: str) -> str:
    """对外暴露：format → HTTP Content-Type 映射。"""
    return _FORMAT_CONTENT_TYPE.get(fmt, "application/octet-stream")
