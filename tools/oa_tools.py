"""办公自动化（OA）工具 —— 本地 mock OA 引擎。

对应 config/tools/submit_form.yaml + form_validate.yaml
+ approval_query.yaml + approval_flow.yaml。

设计决策：无外部 OA 系统可用时，落地为本地 mock 引擎（审批单/表单
持久化到 audit.db 的 oa_* 表），使 agent 可端到端跑通审批流程。
未来对接真实 OA（飞书审批/企业微信审批/自建 OA）时，仅需替换
DAO 层（_query/_advance/_submit/_validate 的 HTTP 调用），handler
签名与 input_schema 不变。

直连 audit.db 模式参考 tools/ops_tools.py:list_failed_runs。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_AUDIT_DB_PATH = "audit.db"

_VALID_STATUSES = {"pending", "approved", "rejected", "withdrawn"}
_VALID_ACTIONS = {"submit", "approve", "reject", "forward", "withdraw"}
_VALID_FORM_TYPES = {
    "leave_request", "expense_report", "purchase_request",
    "overtime_request", "business_trip", "general",
}

_OA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oa_form_submissions (
    form_id        TEXT PRIMARY KEY,
    form_type      TEXT NOT NULL,
    applicant      TEXT NOT NULL,
    fields_json    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK (form_type IN ('leave_request','expense_report','purchase_request',
                        'overtime_request','business_trip','general')),
    CHECK (status IN ('draft','submitted','approved','rejected','withdrawn'))
);
CREATE INDEX IF NOT EXISTS idx_oa_forms_applicant ON oa_form_submissions(applicant);
CREATE INDEX IF NOT EXISTS idx_oa_forms_type ON oa_form_submissions(form_type);

CREATE TABLE IF NOT EXISTS oa_approvals (
    approval_id    TEXT PRIMARY KEY,
    form_id        TEXT,
    title          TEXT NOT NULL,
    applicant      TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'general',
    status         TEXT NOT NULL DEFAULT 'pending',
    current_node   TEXT NOT NULL DEFAULT 'submit',
    assignee       TEXT,
    priority       TEXT NOT NULL DEFAULT 'normal',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    FOREIGN KEY (form_id) REFERENCES oa_form_submissions(form_id) ON DELETE SET NULL,
    CHECK (status IN ('pending','approved','rejected','withdrawn')),
    CHECK (priority IN ('low','normal','high','urgent'))
);
CREATE INDEX IF NOT EXISTS idx_oa_approvals_applicant ON oa_approvals(applicant);
CREATE INDEX IF NOT EXISTS idx_oa_approvals_status ON oa_approvals(status);
CREATE INDEX IF NOT EXISTS idx_oa_approvals_assignee ON oa_approvals(assignee);

CREATE TABLE IF NOT EXISTS oa_approval_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id    TEXT NOT NULL,
    action         TEXT NOT NULL,
    actor          TEXT NOT NULL,
    comment        TEXT,
    from_node      TEXT,
    to_node        TEXT,
    acted_at       TEXT NOT NULL,
    FOREIGN KEY (approval_id) REFERENCES oa_approvals(approval_id) ON DELETE CASCADE,
    CHECK (action IN ('submit','approve','reject','forward','withdraw'))
);
CREATE INDEX IF NOT EXISTS idx_oa_history_approval ON oa_approval_history(approval_id);
"""


def _resolve_db_path(config: dict[str, Any] | None) -> str:
    """解析 audit.db 路径（优先级：config > 环境变量 > 默认）。"""
    if config and config.get("audit_db_path"):
        return config["audit_db_path"]
    env = os.environ.get("AGENTOPS_AUDIT_DB")
    if env:
        return env
    return _DEFAULT_AUDIT_DB_PATH


def _ensure_oa_schema(conn: sqlite3.Connection) -> None:
    """幂等建表（首次调用时创建 oa_* 三表）。"""
    conn.executescript(_OA_SCHEMA_SQL)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str, read_only: bool = False) -> sqlite3.Connection:
    """连接 audit.db 并确保 OA schema 存在（读写模式才建表）。"""
    uri = f"file:{db_path}?mode=ro" if read_only else f"file:{db_path}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    if not read_only:
        _ensure_oa_schema(conn)
    return conn


# ── 表单校验（form_validate） ──────────────────────────────────

_REQUIRED_FIELDS: dict[str, list[str]] = {
    "leave_request":    ["start_date", "end_date", "reason"],
    "expense_report":   ["amount", "category", "description"],
    "purchase_request": ["item_name", "quantity", "estimated_cost"],
    "overtime_request": ["date", "start_time", "end_time", "reason"],
    "business_trip":    ["destination", "start_date", "end_date", "purpose"],
    "general":          [],
}

_FIELD_PATTERNS: dict[str, str] = {
    "start_date":  r"^\d{4}-\d{2}-\d{2}$",
    "end_date":    r"^\d{4}-\d{2}-\d{2}$",
    "date":        r"^\d{4}-\d{2}-\d{2}$",
    "amount":      r"^\d+(\.\d{1,2})?$",
    "estimated_cost": r"^\d+(\.\d{1,2})?$",
    "quantity":    r"^\d+$",
}


async def validate_oa_form(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验 OA 表单字段完整性与格式合法性。

    Returns:
        {"valid": bool, "errors": [{"field", "code", "message"}], "warnings": [...]}
    """
    form_type = args.get("form_type", "general")
    fields: dict[str, Any] = args.get("fields", {})
    errors: list[dict[str, str]] = []
    warnings: list[str] = []

    if form_type not in _VALID_FORM_TYPES:
        errors.append({"field": "form_type", "code": "INVALID_TYPE",
                       "message": f"未知表单类型: {form_type}（支持: {', '.join(sorted(_VALID_FORM_TYPES))}）"})
        return {"valid": False, "errors": errors, "warnings": warnings}

    required = _REQUIRED_FIELDS.get(form_type, [])
    for rf in required:
        if rf not in fields or not str(fields[rf]).strip():
            errors.append({"field": rf, "code": "MISSING_REQUIRED",
                           "message": f"必填字段缺失: {rf}"})

    for fname, value in fields.items():
        pattern = _FIELD_PATTERNS.get(fname)
        if pattern and value is not None and str(value).strip():
            if not re.match(pattern, str(value)):
                errors.append({"field": fname, "code": "FORMAT_ERROR",
                               "message": f"字段 {fname} 格式不合法（期望 {pattern}）"})

    if form_type == "leave_request":
        sd = fields.get("start_date")
        ed = fields.get("end_date")
        if sd and ed and sd > ed:
            errors.append({"field": "end_date", "code": "DATE_ORDER",
                           "message": "结束日期不能早于开始日期"})
        if sd and ed and sd == ed:
            warnings.append("请假开始日期等于结束日期（视为 1 天）")

    if form_type == "expense_report":
        try:
            amt = float(fields.get("amount", 0))
            if amt <= 0:
                errors.append({"field": "amount", "code": "INVALID_VALUE",
                               "message": "金额必须大于 0"})
            if amt > 100000:
                warnings.append("大额报销（>10万）建议附发票照片")
        except (ValueError, TypeError):
            pass

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── 表单提交（submit_form） ─────────────────────────────────────

async def submit_oa_form(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """提交 OA 表单，持久化到 audit.db 并创建审批单。

    Returns:
        {"form_id", "approval_id", "status", "created_at"}
    """
    form_type = args.get("form_type", "general")
    fields: dict[str, Any] = args.get("fields", {})
    applicant = args.get("applicant") or args.get("user_id") or "unknown"
    title = args.get("title") or f"{form_type}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    validation = await validate_oa_form(args, config)
    if not validation["valid"]:
        return {
            "form_id": None,
            "approval_id": None,
            "status": "rejected",
            "errors": validation["errors"],
            "message": "表单校验失败，未提交",
        }

    db_path = _resolve_db_path(config)
    form_id = f"OA-F-{uuid.uuid4().hex[:12].upper()}"
    approval_id = f"OA-A-{uuid.uuid4().hex[:12].upper()}"
    now = _now_iso()
    fields_json = json.dumps(fields, ensure_ascii=False)
    category = form_type if form_type != "general" else "general"

    try:
        conn = _connect(db_path)
    except sqlite3.Error as e:
        logger.error("submit_oa_form: 连接 audit.db 失败: %s", e)
        return {"form_id": None, "approval_id": None, "status": "error",
                "message": f"数据库连接失败: {e}"}

    try:
        conn.execute(
            "INSERT INTO oa_form_submissions (form_id, form_type, applicant, fields_json, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (form_id, form_type, applicant, fields_json, "submitted", now, now),
        )
        conn.execute(
            "INSERT INTO oa_approvals (approval_id, form_id, title, applicant, category, status, current_node, assignee, priority, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, form_id, title, applicant, category, "pending", "submitted", applicant, "normal", now, now),
        )
        conn.execute(
            "INSERT INTO oa_approval_history (approval_id, action, actor, comment, from_node, to_node, acted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (approval_id, "submit", applicant, "表单提交", None, "submitted", now),
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error("submit_oa_form: 写入失败: %s", e)
        return {"form_id": None, "approval_id": None, "status": "error",
                "message": f"写入数据库失败: {e}"}
    finally:
        conn.close()

    return {
        "form_id": form_id,
        "approval_id": approval_id,
        "status": "pending",
        "created_at": now,
        "message": "表单已提交，审批单已创建",
    }


# ── 审批查询（approval_query） ──────────────────────────────────

async def query_approval_status(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """查询审批单状态与历史。

    支持按 approval_id 精确查询，或按 applicant/status/date 范围查询。
    """
    db_path = _resolve_db_path(config)
    max_records = (config or {}).get("max_records", 100)

    try:
        conn = _connect(db_path, read_only=True)
    except sqlite3.Error as e:
        logger.error("query_approval_status: 连接 audit.db 失败: %s", e)
        return {"approvals": [], "total": 0, "error": f"数据库连接失败: {e}"}

    try:
        approval_id = args.get("approval_id")
        if approval_id:
            row = conn.execute(
                "SELECT * FROM oa_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if not row:
                return {"approvals": [], "total": 0, "message": "审批单不存在"}
            history_rows = conn.execute(
                "SELECT * FROM oa_approval_history WHERE approval_id = ? ORDER BY acted_at ASC", (approval_id,)
            ).fetchall()
            approval = dict(row)
            approval["history"] = [dict(h) for h in history_rows]
            return {"approvals": [approval], "total": 1}

        conditions = []
        params: list[Any] = []
        for col in ("applicant", "status", "assignee"):
            val = args.get(col)
            if val and val != "all":
                conditions.append(f"{col} = ?")
                params.append(val)
        date_from = args.get("date_from")
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        date_to = args.get("date_to")
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to + "T23:59:59Z")
        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM oa_approvals{where_clause} ORDER BY created_at DESC LIMIT ?",
            (*params, max_records),
        ).fetchall()
        return {"approvals": [dict(r) for r in rows], "total": len(rows)}
    except sqlite3.Error as e:
        logger.error("query_approval_status: 查询失败: %s", e)
        return {"approvals": [], "total": 0, "error": f"查询失败: {e}"}
    finally:
        conn.close()


# ── 审批流转（approval_flow） ──────────────────────────────────

async def advance_approval_flow(args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """推进审批流程（approve/reject/forward/submit/withdraw）。

    单节点 mock 流转：approve → approved，reject → rejected，
    forward → 转交 assignee（仍 pending），withdraw → withdrawn。
    """
    approval_id = args.get("approval_id")
    action = args.get("action")
    actor = args.get("actor") or args.get("user_id") or "unknown"
    comment = args.get("comment", "")
    forward_to = args.get("forward_to")

    if not approval_id or not action:
        return {"success": False, "message": "approval_id 和 action 为必填"}
    if action not in _VALID_ACTIONS:
        return {"success": False, "message": f"非法 action: {action}（支持: {', '.join(sorted(_VALID_ACTIONS))}）"}
    if action == "forward" and not forward_to:
        return {"success": False, "message": "action=forward 时 forward_to 必填"}

    db_path = _resolve_db_path(config)
    try:
        conn = _connect(db_path)
    except sqlite3.Error as e:
        logger.error("advance_approval_flow: 连接 audit.db 失败: %s", e)
        return {"success": False, "message": f"数据库连接失败: {e}"}

    try:
        row = conn.execute(
            "SELECT * FROM oa_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if not row:
            return {"success": False, "message": f"审批单 {approval_id} 不存在"}
        if row["status"] != "pending":
            return {"success": False, "message": f"审批单已终结（status={row['status']}），不可再操作"}

        now = _now_iso()
        new_status = row["status"]
        new_assignee = row["assignee"]
        new_node = row["current_node"]

        if action == "approve":
            new_status = "approved"
            new_node = "approved"
            new_assignee = actor
        elif action == "reject":
            new_status = "rejected"
            new_node = "rejected"
            new_assignee = actor
        elif action == "forward":
            new_assignee = forward_to
            new_node = f"forwarded_to:{forward_to}"
        elif action == "withdraw":
            new_status = "withdrawn"
            new_node = "withdrawn"

        conn.execute(
            "UPDATE oa_approvals SET status=?, current_node=?, assignee=?, updated_at=? WHERE approval_id=?",
            (new_status, new_node, new_assignee, now, approval_id),
        )
        conn.execute(
            "INSERT INTO oa_approval_history (approval_id, action, actor, comment, from_node, to_node, acted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (approval_id, action, actor, comment, row["current_node"], new_node, now),
        )
        conn.commit()
        return {
            "success": True,
            "approval_id": approval_id,
            "old_status": row["status"],
            "new_status": new_status,
            "current_node": new_node,
            "assignee": new_assignee,
            "acted_at": now,
            "message": f"审批单已 {action}",
        }
    except sqlite3.Error as e:
        conn.rollback()
        logger.error("advance_approval_flow: 流转失败: %s", e)
        return {"success": False, "message": f"流转失败: {e}"}
    finally:
        conn.close()
