"""TaskStore — 任务管理模块持久化层（P0 4 表 CRUD + 乐观锁）。

设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.1
- 独立类，不继承 EventStore ABC（避免实现上百个 session/run 抽象方法）
- 共享 SqliteEventStore._conn + _db_lock，复用同一 WAL 连接
- 所有写操作带 version 乐观锁，冲突返回 None
- asyncio.to_thread(self._exec, ...) 对齐 audit/store.py 模式
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from task.status import TaskStatus

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """ISO 8601 UTC 时间戳（对齐 audit/store.py）。"""
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str) -> str:
    """生成带前缀的唯一 ID（对齐 audit/store.py 风格）。"""
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dt_to_str(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class TaskStore:
    """任务管理模块存储层（P0：projects/tasks/task_stages/global_revision 4 表）。"""

    def __init__(self, conn: sqlite3.Connection, db_lock: threading.Lock):
        self._conn = conn
        self._db_lock = db_lock

    # ====== 内部辅助 ======

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._db_lock:
            return self._conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._exec(sql, params).fetchone()
        return dict(row) if row else None

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        rows = self._exec(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _parse_json(val: Any) -> Any:
        if isinstance(val, str) and val:
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return val
        return val

    @staticmethod
    def _dump_json(val: Any) -> str:
        if val is None:
            return ""
        return json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val

    # ====== global_revision ======

    async def get_revision(self) -> int:
        """获取全局版本号（前端轮询判断刷新）。"""
        row = await asyncio.to_thread(self._fetchone,
            "SELECT revision FROM global_revision WHERE singleton = 1")
        return row["revision"] if row else 0

    # ====== projects ======

    async def create_project(self, *, project_id: str, name: str, type: str = "code",
                             local_path: str = "", github_url: str = "",
                             feishu_doc_token: str = "", workspace_id: str = "",
                             metadata: dict | None = None) -> dict:
        now = _now_iso()
        await asyncio.to_thread(self._exec,
            "INSERT INTO projects (project_id, name, type, local_path, github_url, "
            "feishu_doc_token, workspace_id, next_task_number, metadata, version, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?)",
            (project_id, name, type, local_path or None, github_url or None,
             feishu_doc_token or None, workspace_id or None, self._dump_json(metadata),
             now, now))
        return await self.get_project(project_id)

    async def get_project(self, project_id: str) -> dict | None:
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM projects WHERE project_id = ?", (project_id,))
        if row:
            row["metadata"] = self._parse_json(row.get("metadata"))
        return row

    async def list_projects(self) -> list[dict]:
        rows = await asyncio.to_thread(self._fetchall,
            "SELECT * FROM projects ORDER BY created_at DESC")
        for r in rows:
            r["metadata"] = self._parse_json(r.get("metadata"))
        return rows

    async def alloc_task_number(self, project_id: str) -> tuple[str, int]:
        """分配任务序号（projects.next_task_number 自增），返回 (identifier, new_number)。"""
        # 乐观锁更新 next_task_number
        row = await asyncio.to_thread(self._fetchone,
            "SELECT next_task_number, name FROM projects WHERE project_id = ?", (project_id,))
        if not row:
            raise ValueError(f"project {project_id} not found")
        num = row["next_task_number"]
        # 生成 identifier 前缀（项目名首字母大写 + 序号）
        prefix = (row["name"][:3] or "TSK").upper().replace(" ", "-")
        identifier = f"{prefix}-{num}"
        await asyncio.to_thread(self._exec,
            "UPDATE projects SET next_task_number = ?, updated_at = ? WHERE project_id = ?",
            (num + 1, _now_iso(), project_id))
        return identifier, num

    # ====== tasks ======

    async def create_task(self, *, task_id: str, project_id: str, title: str,
                          description: str = "", status: str = "idea",
                          task_type: str = "code", risk_level: str = "medium",
                          creator_type: str = "user", creator_id: str = "",
                          creator_name: str = "", thread_id: str = "",
                          identifier: str = "", parent_task_id: str = "",
                          sort_order: float = 0,
                          source_idea_id: str = "") -> dict:
        now = _now_iso()
        # source_idea_id 为 V1 字段，P0 库可能无此列；安全 INSERT（列不存在则忽略）
        cols = ["task_id", "project_id", "identifier", "parent_task_id", "title",
                "description", "status", "task_type", "risk_level", "creator_type",
                "creator_id", "creator_name", "thread_id", "sort_order", "version",
                "created_at", "updated_at"]
        vals = [task_id, project_id, identifier or None, parent_task_id or None, title,
                description or None, status, task_type, risk_level, creator_type,
                creator_id or None, creator_name or None, thread_id or None,
                sort_order, 0, now, now]
        if source_idea_id:
            cols.append("source_idea_id")
            vals.append(source_idea_id)
        placeholders = ", ".join("?" * len(cols))
        await asyncio.to_thread(self._exec,
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(vals))
        return await self.get_task(task_id)

    async def get_task(self, task_id: str) -> dict | None:
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return row

    async def list_tasks(self, *, project_id: str = "", status: str = "",
                         assignee_id: str = "", limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if assignee_id:
            sql += " AND assignee_id = ?"
            params.append(assignee_id)
        sql += " ORDER BY sort_order ASC, created_at DESC LIMIT ?"
        params.append(limit)
        return await asyncio.to_thread(self._fetchall, sql, tuple(params))

    async def search_tasks(self, query: str, *, project_id: str = "",
                           status: str = "", limit: int = 50) -> list[dict]:
        """V2-W4：任务搜索（LIKE 模糊匹配，支持中英文）。

        Args:
            query: 搜索关键词（在 title/description/identifier 中模糊匹配）
            project_id: 可选，按项目过滤
            status: 可选，按状态过滤
            limit: 返回条数上限

        Returns:
            匹配的任务列表，按 created_at DESC 排序
        """
        if not query.strip():
            return []
        pattern = f"%{query.strip()}%"
        sql = (
            "SELECT * FROM tasks WHERE "
            "(title LIKE ? OR description LIKE ? OR identifier LIKE ?) "
        )
        params: list = [pattern, pattern, pattern]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return await asyncio.to_thread(self._fetchall, sql, tuple(params))

    async def update_task_status(self, task_id: str, new_status: str,
                                 if_version: int) -> dict | None:
        """乐观锁更新任务状态。冲突（version 不匹配）返回 None。"""
        now = _now_iso()
        closed_at = now if new_status == TaskStatus.CLOSED.value else None
        cur = await asyncio.to_thread(self._exec,
            "UPDATE tasks SET status = ?, version = version + 1, updated_at = ?, "
            "closed_at = COALESCE(?, closed_at) "
            "WHERE task_id = ? AND version = ?",
            (new_status, now, closed_at, task_id, if_version))
        if cur.rowcount == 0:
            return None  # 乐观锁冲突
        return await self.get_task(task_id)

    async def update_task_fields(self, task_id: str, if_version: int,
                                 **fields) -> dict | None:
        """乐观锁更新任务字段（title/description/risk_level/assignee_* 等）。"""
        if not fields:
            return await self.get_task(task_id)
        # parent_task_id 变更：环检测（祖先链不能包含自己）+ 同步 relations 表
        new_parent = fields.get("parent_task_id", ...)
        if new_parent is not ...:
            if new_parent and new_parent == task_id:
                raise ValueError("父任务不能是任务自身")
            if new_parent:
                seen = {task_id}
                cursor: str | None = new_parent
                while cursor:
                    if cursor in seen:
                        raise ValueError("父任务设置会形成环（祖先链中包含自身）")
                    seen.add(cursor)
                    row = await asyncio.to_thread(self._fetchone,
                        "SELECT parent_task_id FROM tasks WHERE task_id = ?", (cursor,))
                    cursor = row["parent_task_id"] if row else None
        allowed = {"title", "description", "task_type", "risk_level",
                   "assignee_type", "assignee_id", "assignee_name",
                   "sort_order", "thread_id", "terminal_session_id", "style_id",
                   "approved", "parent_task_id"}
        sets = []
        params = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return await self.get_task(task_id)
        sets.append("version = version + 1")
        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.extend([task_id, if_version])
        cur = await asyncio.to_thread(self._exec,
            f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ? AND version = ?",
            tuple(params))
        if cur.rowcount == 0:
            return None
        # 同步 relations：删除旧 parent 关系，写入新关系（source=父, target=子）
        if new_parent is not ...:
            await asyncio.to_thread(self._exec,
                "DELETE FROM task_relations WHERE relation_type = 'parent' AND target_task_id = ?",
                (task_id,))
            if new_parent:
                await asyncio.to_thread(self._exec,
                    "INSERT INTO task_relations (relation_id, relation_type, "
                    "source_task_id, target_task_id, created_at) VALUES (?, 'parent', ?, ?, ?)",
                    (_gen_id("rel"), new_parent, task_id, _now_iso()))
        return await self.get_task(task_id)

    # ====== task_stages ======

    async def create_stage(self, *, stage_id: str, task_id: str, stage_type: str,
                           assigned_agent: str = "", stage_input: str = "",
                           stage_output: str = "") -> dict:
        now = _now_iso()
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_stages (stage_id, task_id, stage_type, status, "
            "assigned_agent, stage_input, stage_output, version, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?, 0, ?)",
            (stage_id, task_id, stage_type, assigned_agent or None,
             stage_input or None, stage_output or None, now))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_stages WHERE stage_id = ?", (stage_id,))

    async def commit_stage(self, stage_id: str, if_version: int,
                           stage_output: str, status: str = "committed") -> dict | None:
        """乐观锁提交阶段产出。"""
        now = _now_iso()
        cur = await asyncio.to_thread(self._exec,
            "UPDATE task_stages SET stage_output = ?, status = ?, "
            "version = version + 1, committed_at = ? "
            "WHERE stage_id = ? AND version = ?",
            (stage_output, status, now, stage_id, if_version))
        if cur.rowcount == 0:
            return None
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_stages WHERE stage_id = ?", (stage_id,))

    async def list_stages(self, task_id: str) -> list[dict]:
        return await asyncio.to_thread(self._fetchall,
            "SELECT * FROM task_stages WHERE task_id = ? ORDER BY started_at ASC",
            (task_id,))

    # ============================================================
    # V1 增量方法（13 张新表 CRUD）
    # 设计文档 §4.1（V1 扩展）。仅当 task_v1_enabled=True 时可用。
    # ============================================================

    # ------ ideas（灵感池 + 噪声抑制） ------
    async def submit_idea(self, *, project_id: str, content: str,
                          source: str = "manual", source_ref: str = "",
                          tags: list[str] | None = None,
                          auto_draft: bool = False) -> dict:
        """自动接入（GitHub/飞书/对话）→ status=draft；手动录入 → status=open。"""
        idea_id = _gen_id("idea")
        now = _now_iso()
        status = "draft" if auto_draft else "open"
        await asyncio.to_thread(self._exec,
            "INSERT INTO ideas (idea_id, project_id, source, source_ref, content, "
            "tags, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (idea_id, project_id, source, source_ref or None, content,
             self._dump_json(tags or []), status, now))
        return await self.get_idea(idea_id)

    async def get_idea(self, idea_id: str) -> dict | None:
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM ideas WHERE idea_id = ?", (idea_id,))
        if row:
            row["tags"] = self._parse_json(row.get("tags"))
        return row

    async def list_ideas(self, project_id: str = "", status: str = "") -> list[dict]:
        sql = "SELECT * FROM ideas WHERE 1=1"
        params: list = []
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        rows = await asyncio.to_thread(self._fetchall, sql, tuple(params))
        for r in rows:
            r["tags"] = self._parse_json(r.get("tags"))
        return rows

    async def confirm_idea(self, idea_id: str, if_version: int) -> dict:
        """draft → open（用户拖拽确认）。乐观锁。"""
        cur = await asyncio.to_thread(self._exec,
            "UPDATE ideas SET status = 'open', version = version + 1 "
            "WHERE idea_id = ? AND version = ? AND status = 'draft'",
            (idea_id, if_version))
        if cur.rowcount == 0:
            return {"ok": False, "conflict": True, "idea": await self.get_idea(idea_id)}
        return {"ok": True, "idea": await self.get_idea(idea_id)}

    async def convert_idea_to_task(self, idea_id: str, task_id: str,
                                   title: str | None = None) -> dict:
        """idea → task（v1.2：用户显式转换视为立项，进入 discussing），并回写 idea.converted_task_id。"""
        idea = await self.get_idea(idea_id)
        if not idea:
            raise ValueError(f"idea {idea_id} not found")
        task = await self.create_task(
            task_id=task_id,
            project_id=idea["project_id"],
            title=title or idea["content"][:80],
            description=idea["content"],
            status="discussing",
            source_idea_id=idea_id,
        )
        await asyncio.to_thread(self._exec,
            "UPDATE ideas SET status = 'converted', converted_task_id = ?, "
            "version = version + 1 WHERE idea_id = ?",
            (task["task_id"], idea_id))
        return task

    # ------ task_relations（依赖 + 环检测） ------
    async def add_relation(self, source_task_id: str, target_task_id: str,
                           relation_type: str) -> dict:
        relation_id = _gen_id("rel")
        try:
            await asyncio.to_thread(self._exec,
                "INSERT INTO task_relations (relation_id, relation_type, "
                "source_task_id, target_task_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (relation_id, relation_type, source_task_id, target_task_id, _now_iso()))
        except sqlite3.IntegrityError as e:
            if "RELATION_CYCLE" in str(e):
                return {"ok": False, "error": "relation_cycle"}
            raise
        return {"ok": True, "relation_id": relation_id}

    async def list_blocked_by(self, task_id: str) -> list[dict]:
        """查询阻塞某任务的前置任务（blocks 关系的 source）。"""
        return await asyncio.to_thread(self._fetchall,
            "SELECT t.* FROM tasks t JOIN task_relations r "
            "ON t.task_id = r.source_task_id "
            "WHERE r.target_task_id = ? AND r.relation_type = 'blocks'",
            (task_id,))

    async def list_relations(self, task_id: str = "") -> list[dict]:
        sql = "SELECT * FROM task_relations WHERE 1=1"
        params: list = []
        if task_id:
            sql += " AND (source_task_id = ? OR target_task_id = ?)"
            params.extend([task_id, task_id])
        return await asyncio.to_thread(self._fetchall, sql, tuple(params))

    async def list_active_tasks_with_last_activity(self, *, project_id: str = "") -> list[dict]:
        """调度扫描专用（§4.10.8）：活跃（非终态）任务 + 最后活动时间。

        last_activity_at = max(tasks.updated_at, 最后一条 activity 的 created_at)，
        单 SQL JOIN 免 N+1。task_activities 表仅 V1 存在，P0 库回退 updated_at。
        """
        has_activities = await asyncio.to_thread(self._fetchone,
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_activities'")
        if has_activities:
            sql = (
                "SELECT t.*, MAX(COALESCE(a.created_at, t.updated_at), t.updated_at) AS last_activity_at "
                "FROM tasks t LEFT JOIN task_activities a ON a.task_id = t.task_id "
                "WHERE t.status NOT IN ('closed','canceled','abandoned')"
            )
        else:
            sql = (
                "SELECT t.*, t.updated_at AS last_activity_at FROM tasks t "
                "WHERE t.status NOT IN ('closed','canceled','abandoned')"
            )
        params: list = []
        if project_id:
            sql += " AND t.project_id = ?"
            params.append(project_id)
        sql += " GROUP BY t.task_id ORDER BY t.created_at ASC"
        return await asyncio.to_thread(self._fetchall, sql, tuple(params))

    # ------ task_reports / task_comments（博客评论模式） ------
    async def submit_report(self, *, task_id: str, agent_id: str, content: str,
                            session_id: str = "", terminal_session_id: str = "",
                            artifact_ids: list[str] | None = None,
                            self_check: dict | None = None) -> dict:
        report_id = _gen_id("report")
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_reports (report_id, task_id, agent_id, session_id, "
            "terminal_session_id, content, artifact_ids, acceptance_self_check, "
            "submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report_id, task_id, agent_id, session_id or None,
             terminal_session_id or None, content,
             self._dump_json(artifact_ids or []),
             self._dump_json(self_check or {}), _now_iso()))
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_reports WHERE report_id = ?", (report_id,))
        if row:
            row["artifact_ids"] = self._parse_json(row.get("artifact_ids"))
            row["acceptance_self_check"] = self._parse_json(row.get("acceptance_self_check"))
        return row

    async def get_report(self, report_id: str) -> dict | None:
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_reports WHERE report_id = ?", (report_id,))
        if row:
            row["artifact_ids"] = self._parse_json(row.get("artifact_ids"))
            row["acceptance_self_check"] = self._parse_json(row.get("acceptance_self_check"))
        return row

    async def list_reports(self, task_id: str) -> list[dict]:
        rows = await asyncio.to_thread(self._fetchall,
            "SELECT * FROM task_reports WHERE task_id = ? ORDER BY submitted_at DESC",
            (task_id,))
        for r in rows:
            r["artifact_ids"] = self._parse_json(r.get("artifact_ids"))
            r["acceptance_self_check"] = self._parse_json(r.get("acceptance_self_check"))
        return rows

    async def add_comment(self, *, task_id: str, body: str, author_type: str,
                          author_id: str = "", author_name: str = "",
                          comment_type: str = "discussion", report_id: str = "",
                          decision: str | None = None,
                          rollback_target: str | None = None,
                          thread_id: str = "",
                          mentions: list[str] | None = None) -> dict:
        comment_id = _gen_id("cmt")
        now = _now_iso()
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_comments (comment_id, task_id, report_id, author_type, "
            "author_id, author_name, body, comment_type, decision, rollback_target, "
            "thread_id, mentions, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (comment_id, task_id, report_id or None, author_type, author_id or None,
             author_name or None, body, comment_type, decision, rollback_target,
             thread_id or None, self._dump_json(mentions or []) or None, now, now))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_comments WHERE comment_id = ?", (comment_id,))

    async def list_comments(self, task_id: str, comment_type: str = "") -> list[dict]:
        sql = "SELECT * FROM task_comments WHERE task_id = ?"
        params: list = [task_id]
        if comment_type:
            sql += " AND comment_type = ?"
            params.append(comment_type)
        sql += " ORDER BY created_at ASC"
        rows = await asyncio.to_thread(self._fetchall, sql, tuple(params))
        for r in rows:
            r["mentions"] = self._parse_json(r.get("mentions")) or []
        return rows

    # ------ acceptance_criteria ------
    async def add_criteria(self, *, task_id: str, description: str,
                           check_type: str = "manual") -> dict:
        criteria_id = _gen_id("crit")
        await asyncio.to_thread(self._exec,
            "INSERT INTO acceptance_criteria (criteria_id, task_id, description, "
            "check_type) VALUES (?, ?, ?, ?)",
            (criteria_id, task_id, description, check_type))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM acceptance_criteria WHERE criteria_id = ?", (criteria_id,))

    async def list_criteria(self, task_id: str) -> list[dict]:
        return await asyncio.to_thread(self._fetchall,
            "SELECT * FROM acceptance_criteria WHERE task_id = ? ORDER BY rowid ASC",
            (task_id,))

    async def update_criteria_status(self, criteria_id: str, if_version: int,
                                     status: str) -> dict | None:
        cur = await asyncio.to_thread(self._exec,
            "UPDATE acceptance_criteria SET status = ?, version = version + 1, "
            "checked_at = ? WHERE criteria_id = ? AND version = ?",
            (status, _now_iso(), criteria_id, if_version))
        if cur.rowcount == 0:
            return None
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM acceptance_criteria WHERE criteria_id = ?", (criteria_id,))

    # ------ design_docs / doc_change_proposals / design_doc_changes ------
    async def create_doc(self, *, doc_id: str, project_id: str, title: str,
                         path: str, content_hash: str = "") -> dict:
        await asyncio.to_thread(self._exec,
            "INSERT INTO design_docs (doc_id, project_id, title, path, content_hash, "
            "version, last_updated_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (doc_id, project_id, title, path, content_hash or None, _now_iso()))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM design_docs WHERE doc_id = ?", (doc_id,))

    async def get_doc(self, doc_id: str) -> dict | None:
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM design_docs WHERE doc_id = ?", (doc_id,))
        if row:
            row["affected_by_tasks"] = self._parse_json(row.get("affected_by_tasks"))
        return row

    async def list_docs(self, project_id: str) -> list[dict]:
        rows = await asyncio.to_thread(self._fetchall,
            "SELECT * FROM design_docs WHERE project_id = ? ORDER BY last_updated_at DESC",
            (project_id,))
        for r in rows:
            r["affected_by_tasks"] = self._parse_json(r.get("affected_by_tasks"))
        return rows

    async def create_doc_proposal(self, *, doc_id: str, task_id: str,
                                  change_type: str, new_content: str,
                                  rationale: str = "", section_path: str = "",
                                  old_content_hash: str = "") -> dict:
        proposal_id = _gen_id("proposal")
        await asyncio.to_thread(self._exec,
            "INSERT INTO doc_change_proposals (proposal_id, doc_id, task_id, "
            "change_type, section_path, old_content_hash, new_content, rationale, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (proposal_id, doc_id, task_id, change_type, section_path,
             old_content_hash or None, new_content, rationale, _now_iso()))
        return await self.get_doc_proposal(proposal_id)

    async def get_doc_proposal(self, proposal_id: str) -> dict | None:
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM doc_change_proposals WHERE proposal_id = ?", (proposal_id,))

    async def list_doc_proposals(self, task_id: str = "", status: str = "") -> list[dict]:
        sql = "SELECT * FROM doc_change_proposals WHERE 1=1"
        params: list = []
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return await asyncio.to_thread(self._fetchall, sql, tuple(params))

    async def apply_doc_proposal(self, proposal_id: str, if_version: int,
                                 new_hash: str) -> dict:
        """应用提案：更新提案状态 + 写变更历史 + 更新 design_docs.content_hash。"""
        cur = await asyncio.to_thread(self._exec,
            "UPDATE doc_change_proposals SET status = 'applied', applied_at = ?, "
            "version = version + 1 WHERE proposal_id = ? AND version = ? AND status = 'approved'",
            (_now_iso(), proposal_id, if_version))
        if cur.rowcount == 0:
            return {"ok": False, "conflict": True}
        proposal = await self.get_doc_proposal(proposal_id)
        # 写变更历史
        await asyncio.to_thread(self._exec,
            "INSERT INTO design_doc_changes (change_id, doc_id, task_id, proposal_id, "
            "change_type, section_path, prev_hash, new_hash, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_gen_id("docchg"), proposal["doc_id"], proposal["task_id"], proposal_id,
             proposal["change_type"], proposal["section_path"],
             proposal["old_content_hash"], new_hash, _now_iso()))
        # 更新文档元数据
        await asyncio.to_thread(self._exec,
            "UPDATE design_docs SET content_hash = ?, last_updated_by_task = ?, "
            "last_updated_at = ?, version = version + 1 WHERE doc_id = ?",
            (new_hash, proposal["task_id"], _now_iso(), proposal["doc_id"]))
        return {"ok": True}

    # ------ task_runs（弱关联） ------
    async def link_task_run(self, *, task_id: str, role: str = "main_execution",
                            run_id: str = "", session_id: str = "",
                            terminal_session_id: str = "") -> dict:
        link_id = _gen_id("link")
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_runs (link_id, task_id, run_id, session_id, "
            "terminal_session_id, role, linked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (link_id, task_id, run_id or None, session_id or None,
             terminal_session_id or None, role, _now_iso()))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_runs WHERE link_id = ?", (link_id,))

    async def list_task_runs(self, task_id: str) -> list[dict]:
        return await asyncio.to_thread(self._fetchall,
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY linked_at DESC",
            (task_id,))

    # ------ task_activities（字段级变更） ------
    async def add_activity(self, *, task_id: str, actor_type: str,
                           actor_id: str = "", actor_name: str = "",
                           changes: dict | None = None) -> dict:
        activity_id = _gen_id("act")
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_activities (activity_id, task_id, actor_type, actor_id, "
            "actor_name, changes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (activity_id, task_id, actor_type, actor_id or None,
             actor_name or None, self._dump_json(changes or {}), _now_iso()))
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_activities WHERE activity_id = ?", (activity_id,))
        if row:
            row["changes"] = self._parse_json(row.get("changes"))
        return row

    async def list_activities(self, task_id: str) -> list[dict]:
        rows = await asyncio.to_thread(self._fetchall,
            "SELECT * FROM task_activities WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,))
        for r in rows:
            r["changes"] = self._parse_json(r.get("changes"))
        return rows

    # ------ task_artifacts ------
    async def add_artifact(self, *, task_id: str, type: str, path: str = "",
                           content_hash: str = "", description: str = "") -> dict:
        artifact_id = _gen_id("art")
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_artifacts (artifact_id, task_id, type, path, "
            "content_hash, description, version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (artifact_id, task_id, type, path or None, content_hash or None,
             description or None, _now_iso()))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_artifacts WHERE artifact_id = ?", (artifact_id,))

    async def list_artifacts(self, task_id: str) -> list[dict]:
        return await asyncio.to_thread(self._fetchall,
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,))

    # ------ task_events（审计日志） ------
    async def add_event(self, *, task_id: str, event_type: str, actor: str = "",
                        stage_id: str = "", payload: dict | None = None) -> dict:
        event_id = _gen_id("evt")
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_events (event_id, task_id, stage_id, event_type, "
            "actor, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, task_id, stage_id or None, event_type, actor or None,
             self._dump_json(payload or {}), _now_iso()))
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_events WHERE event_id = ?", (event_id,))
        if row:
            row["payload"] = self._parse_json(row.get("payload"))
        return row

    async def list_events(self, task_id: str) -> list[dict]:
        rows = await asyncio.to_thread(self._fetchall,
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,))
        for r in rows:
            r["payload"] = self._parse_json(r.get("payload"))
        return rows

    # ------ agent_styles ------
    async def create_style(self, *, style_id: str, name: str,
                           description: str = "",
                           system_prompt_overlay: str = "",
                           permissions_overlay: dict | None = None,
                           model_overlay: dict | None = None) -> dict:
        await asyncio.to_thread(self._exec,
            "INSERT INTO agent_styles (style_id, name, description, "
            "system_prompt_overlay, permissions_overlay, model_overlay, version) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (style_id, name, description or None, system_prompt_overlay or None,
             self._dump_json(permissions_overlay or {}),
             self._dump_json(model_overlay or {})))
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM agent_styles WHERE style_id = ?", (style_id,))
        if row:
            row["permissions_overlay"] = self._parse_json(row.get("permissions_overlay"))
            row["model_overlay"] = self._parse_json(row.get("model_overlay"))
        return row

    async def get_style(self, style_id: str) -> dict | None:
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM agent_styles WHERE style_id = ?", (style_id,))
        if row:
            row["permissions_overlay"] = self._parse_json(row.get("permissions_overlay"))
            row["model_overlay"] = self._parse_json(row.get("model_overlay"))
        return row

    async def list_styles(self) -> list[dict]:
        rows = await asyncio.to_thread(self._fetchall,
            "SELECT * FROM agent_styles ORDER BY name ASC")
        for r in rows:
            r["permissions_overlay"] = self._parse_json(r.get("permissions_overlay"))
            r["model_overlay"] = self._parse_json(r.get("model_overlay"))
        return rows

    # ============================================================
    # V3：终端会话注册表 + 布局持久化（设计文档 §4.13.1）
    # ============================================================

    async def register_terminal_session(self, *, terminal_session_id: str,
                                        task_id: str = "", kind: str = "shell") -> dict:
        """注册终端会话（execute_coding 的 agent 窗格 / 手动新建窗口共用）。"""
        now = _now_iso()
        await asyncio.to_thread(self._exec,
            "INSERT OR REPLACE INTO terminal_sessions "
            "(terminal_session_id, task_id, kind, status, created_at) VALUES (?, ?, ?, 'active', ?)",
            (terminal_session_id, task_id or None, kind, now))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM terminal_sessions WHERE terminal_session_id = ?",
            (terminal_session_id,))

    async def list_terminal_sessions(self, status: str = "") -> list[dict]:
        sql = "SELECT * FROM terminal_sessions"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        return await asyncio.to_thread(self._fetchall, sql, params)

    async def update_terminal_session_status(self, terminal_session_id: str,
                                             status: str) -> dict | None:
        """更新会话状态（done=正常结束 / dead=手动关闭）。"""
        ended = _now_iso() if status in ("done", "dead") else None
        await asyncio.to_thread(self._exec,
            "UPDATE terminal_sessions SET status = ?, ended_at = COALESCE(?, ended_at) "
            "WHERE terminal_session_id = ?",
            (status, ended, terminal_session_id))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM terminal_sessions WHERE terminal_session_id = ?",
            (terminal_session_id,))

    async def delete_terminal_session(self, terminal_session_id: str) -> dict | None:
        """物理删除会话记录（关闭会话/移除窗格后，列表与 SSE 不再回推）。"""
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM terminal_sessions WHERE terminal_session_id = ?",
            (terminal_session_id,))
        await asyncio.to_thread(self._exec,
            "DELETE FROM terminal_sessions WHERE terminal_session_id = ?",
            (terminal_session_id,))
        return row

    async def save_terminal_layout(self, *, user_id: str, panes: list[dict]) -> dict:
        """保存窗格布局（每用户单行，UPSERT）。"""
        layout_id = _gen_id("tlay")
        now = _now_iso()
        await asyncio.to_thread(self._exec,
            "INSERT INTO terminal_layouts (layout_id, user_id, panes, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET panes = excluded.panes, "
            "updated_at = excluded.updated_at",
            (layout_id, user_id, self._dump_json(panes), now))
        return await self.get_terminal_layout(user_id) or {}

    async def get_terminal_layout(self, user_id: str = "local") -> dict | None:
        row = await asyncio.to_thread(self._fetchone,
            "SELECT * FROM terminal_layouts WHERE user_id = ?", (user_id,))
        if row:
            row["panes"] = self._parse_json(row.get("panes")) or []
        return row

    # ============================================================
    # v1.1 生命周期自动化：design_notes 四态笔记 + 贡献账本 + 声望
    # （DESIGN_task_lifecycle_automation_v1.md §5.2/§5.3.1）
    # ============================================================

    async def add_design_note(self, *, task_id: str, project_id: str,
                               content: str, status: str = "proposed",
                               supersedes: str = "", reject_reason: str = "",
                               source_run: str = "") -> dict:
        """新增设计笔记（agent 执行中硬性产出，默认 proposed 态）。"""
        note_id = _gen_id("note")
        now = _now_iso()
        await asyncio.to_thread(self._exec,
            "INSERT INTO design_notes (note_id, task_id, project_id, status, "
            "content, supersedes, reject_reason, source_run, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, task_id, project_id, status, content,
             supersedes or None, reject_reason or None, source_run or None, now, now))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM design_notes WHERE note_id = ?", (note_id,))

    async def list_design_notes(self, task_id: str = "", project_id: str = "",
                                status: str = "") -> list[dict]:
        """查询设计笔记（task/project/status 任意组合过滤）。"""
        sql = "SELECT * FROM design_notes WHERE 1=1"
        params: list = []
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return await asyncio.to_thread(self._fetchall, sql, tuple(params))

    async def update_design_note_status(self, note_id: str, new_status: str,
                                        *, reject_reason: str = "",
                                        supersedes: str = "") -> dict | None:
        """笔记状态流转（四态机：proposed→implemented/rejected，implemented→archived）。

        implemented 晋升时若带 supersedes（被取代的旧笔记），旧笔记自动转 archived
        （决策演进链，DESIGN §5.3.1）。
        """
        valid = {"proposed", "implemented", "rejected", "archived"}
        if new_status not in valid:
            raise ValueError(f"非法笔记状态: {new_status}")
        if new_status == "rejected" and not reject_reason:
            raise ValueError("rejected 态必须记录否决理由")
        await asyncio.to_thread(self._exec,
            "UPDATE design_notes SET status = ?, reject_reason = ?, supersedes = ?, "
            "updated_at = ? WHERE note_id = ?",
            (new_status, reject_reason or None, supersedes or None,
             _now_iso(), note_id))
        # 晋升 implemented 时自动归档被取代的旧笔记（演进链）
        if new_status == "implemented" and supersedes:
            await asyncio.to_thread(self._exec,
                "UPDATE design_notes SET status = 'archived', updated_at = ? "
                "WHERE note_id = ? AND status = 'implemented'",
                (_now_iso(), supersedes))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM design_notes WHERE note_id = ?", (note_id,))

    async def add_contribution(self, *, task_id: str, agent_id: str,
                               contribution_pct: float, basis: str = "",
                               round_num: int = 1) -> dict:
        """记录讨论贡献（收敛 agent 初评 + 用户可改后重记）。"""
        if not (0 <= contribution_pct <= 100):
            raise ValueError("contribution_pct 必须在 0-100")
        contribution_id = _gen_id("contrib")
        await asyncio.to_thread(self._exec,
            "INSERT INTO task_contributions (contribution_id, task_id, agent_id, "
            "contribution_pct, basis, round, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contribution_id, task_id, agent_id, contribution_pct,
             basis or None, round_num, _now_iso()))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM task_contributions WHERE contribution_id = ?",
            (contribution_id,))

    async def list_contributions(self, task_id: str = "") -> list[dict]:
        sql = "SELECT * FROM task_contributions"
        params: tuple = ()
        if task_id:
            sql += " WHERE task_id = ?"
            params = (task_id,)
        sql += " ORDER BY created_at ASC"
        return await asyncio.to_thread(self._fetchall, sql, params)

    async def effective_reputation(self, agent_id: str,
                                   half_life_days: float = 30.0) -> float:
        """时间衰减有效声望：Σ 贡献 × exp(-λ × 天数)，λ = ln2 / 半衰期。

        反马太效应之一（DESIGN §5.2）：声望反映近期状态而非历史功勋，
        30 天前的贡献权重减半。账本不删，溯源能力完整保留。
        """
        rows = await asyncio.to_thread(self._fetchall,
            "SELECT contribution_pct, created_at FROM task_contributions "
            "WHERE agent_id = ?", (agent_id,))
        import math
        lam = math.log(2) / half_life_days
        now = datetime.now(timezone.utc)
        total = 0.0
        for r in rows:
            try:
                created = datetime.fromisoformat(r["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days = max((now - created).total_seconds() / 86400.0, 0.0)
                total += float(r["contribution_pct"]) * math.exp(-lam * days)
            except (ValueError, TypeError):
                continue
        return round(total, 4)

    async def upsert_agent_profile(self, agent_id: str, *,
                                   reputation: float | None = None,
                                   tasks_won_delta: int = 0,
                                   avg_report_score: float | None = None) -> dict:
        """更新声望档案快照（有效声望主查询走 effective_reputation 账本加权）。"""
        now = _now_iso()
        await asyncio.to_thread(self._exec,
            "INSERT INTO agent_profiles (agent_id, reputation, reputation_updated_at, "
            "tasks_won, contribution_total, avg_report_score, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "reputation = COALESCE(excluded.reputation, reputation), "
            "reputation_updated_at = CASE WHEN excluded.reputation IS NOT NULL "
            "THEN excluded.reputation_updated_at ELSE reputation_updated_at END, "
            "tasks_won = tasks_won + ?, "
            "avg_report_score = COALESCE(excluded.avg_report_score, avg_report_score), "
            "updated_at = excluded.updated_at",
            (agent_id, reputation, now if reputation is not None else None,
             0, avg_report_score, now, tasks_won_delta))
        return await asyncio.to_thread(self._fetchone,
            "SELECT * FROM agent_profiles WHERE agent_id = ?", (agent_id,))

    async def list_agent_profiles(self) -> list[dict]:
        return await asyncio.to_thread(self._fetchall,
            "SELECT * FROM agent_profiles ORDER BY reputation DESC")
