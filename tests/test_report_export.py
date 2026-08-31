"""测试 ReportExporter 全链路（v1：导出流程 / 格式处理 / 存储验证）。

覆盖矩阵：
- 单元：路径白名单拒绝（路径穿越 / 超长 ID / 错误 format）
- 单元：Markdown → HTML 渲染（标题/列表/代码块）
- 集成：export() → 文件落盘 → hash 计算 → 历史落库 → verify_export 匹配
- 集成：重复导出（覆盖文件 + 新增历史）
- 集成：list_exports 倒序 + format 过滤
- 边界：unsafe task_id 抛 ValueError，不写文件，不落库

不依赖 FastAPI（测试纯导出器逻辑），不依赖 audit.db（独立临时 DB）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from task.exporter import (
    ReportExporter, _is_safe_id, _md_to_html, get_content_type,
    _ALLOWED_FORMATS,
)


# ============ Fixtures ============

@pytest.fixture
def workspace_root():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def db_conn():
    """独立内存 DB（含最小 tasks + task_reports schema，复用 FK）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE task_reports ("
        " report_id TEXT PRIMARY KEY,"
        " task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,"
        " agent_id TEXT, session_id TEXT, terminal_session_id TEXT,"
        " content TEXT NOT NULL, artifact_ids TEXT, acceptance_self_check TEXT,"
        " status TEXT NOT NULL DEFAULT 'submitted',"
        " version INTEGER NOT NULL DEFAULT 0, submitted_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?)",
        ("task_20260821_143015_abc123",),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def exporter(db_conn, workspace_root):
    lock = threading.Lock()
    exp = ReportExporter(conn=db_conn, db_lock=lock, workspace_root=workspace_root)
    exp.ensure_schema()
    return exp


@pytest.fixture
def sample_report():
    return {
        "report_id": "report_abc123def456",
        "task_id": "task_20260821_143015_abc123",
        "agent_id": "coding_agent",
        "session_id": "session_xyz",
        "terminal_session_id": None,
        "content": (
            "# 执行总结\n\n"
            "## 子任务\n"
            "- 步骤 1\n"
            "- 步骤 2\n\n"
            "## 代码\n"
            "```\nprint('hi')\n```\n"
        ),
        "artifact_ids": ["artifact_1", "artifact_2"],
        "acceptance_self_check": {"notes_count": 1, "notes_missing": False},
        "status": "submitted",
        "version": 1,
        "submitted_at": "2026-08-21T14:30:15+00:00",
    }


# ============ 1. 路径安全 ============

class TestPathSafety:
    def test_unsafe_task_id_rejected(self, exporter):
        for bad in ["../etc", "task_x/../foo", "foo;rm", "../../windows", ""]:
            with pytest.raises(ValueError, match="unsafe task_id"):
                exporter._resolve_path(task_id=bad, report_id="report_x", fmt="md")

    def test_unsafe_report_id_rejected(self, exporter):
        for bad in ["../etc", "foo/bar", "a" * 100, ""]:
            with pytest.raises(ValueError, match="unsafe report_id"):
                exporter._resolve_path(
                    task_id="task_20260821_143015_abc123",
                    report_id=bad, fmt="md")

    def test_unsupported_format_rejected(self, exporter):
        for bad in ["pdf", "exe", "MD", "mdx", ""]:
            with pytest.raises(ValueError, match="unsupported format"):
                exporter._resolve_path(
                    task_id="task_20260821_143015_abc123",
                    report_id="report_x", fmt=bad)

    def test_safe_id_helper(self):
        assert _is_safe_id("report_abc123", kind="report") is True
        assert _is_safe_id("task_20260821_143015_abc123", kind="task") is True
        for bad in ["../x", "foo/bar", "r" * 100, ""]:
            assert _is_safe_id(bad, kind="report") is False
            assert _is_safe_id(bad, kind="task") is False


# ============ 2. 格式处理 ============

class TestFormatHandlers:
    def test_markdown_passthrough(self, exporter, sample_report):
        data = exporter._format_markdown(sample_report)
        assert data.decode("utf-8") == sample_report["content"]

    def test_html_renders_basic_blocks(self, exporter, sample_report):
        data = exporter._format_html(sample_report).decode("utf-8")
        assert "<h1>执行总结</h1>" in data
        assert "<h2>子任务</h2>" in data
        assert "<li>步骤 1</li>" in data
        assert "<li>步骤 2</li>" in data
        assert "<pre><code>" in data
        assert "print(&#x27;hi&#x27;)" in data or "print('hi')" in data

    def test_html_escapes_user_content(self, exporter, sample_report):
        sample_report["content"] = '<script>alert("xss")</script>'
        data = exporter._format_html(sample_report).decode("utf-8")
        assert "<script>" not in data
        assert "&lt;script&gt;" in data

    def test_html_self_check_block(self, exporter, sample_report):
        data = exporter._format_html(sample_report).decode("utf-8")
        assert "自检清单" in data
        assert "notes_count" in data

    def test_html_handles_missing_self_check(self, exporter, sample_report):
        sample_report["acceptance_self_check"] = {}
        data = exporter._format_html(sample_report).decode("utf-8")
        assert "自检清单" not in data

    def test_json_includes_metadata(self, exporter, sample_report):
        data = json.loads(exporter._format_json(sample_report).decode("utf-8"))
        assert data["report_id"] == sample_report["report_id"]
        assert data["task_id"] == sample_report["task_id"]
        assert data["agent_id"] == "coding_agent"
        assert data["artifact_ids"] == ["artifact_1", "artifact_2"]
        assert data["acceptance_self_check"]["notes_count"] == 1
        assert "# 执行总结" in data["content"]

    def test_get_content_type(self):
        assert get_content_type("md").startswith("text/markdown")
        assert get_content_type("html").startswith("text/html")
        assert get_content_type("json").startswith("application/json")
        assert get_content_type("unknown").startswith("application/octet-stream")


class TestMarkdownToHtml:
    def test_heading_levels(self):
        md = "# h1\n## h2\n### h3"
        html = _md_to_html(md)
        assert "<h1>h1</h1>" in html
        assert "<h2>h2</h2>" in html
        assert "<h3>h3</h3>" in html

    def test_list(self):
        md = "- a\n- b\n- c"
        html = _md_to_html(md)
        assert "<ul>" in html
        assert "<li>a</li>" in html
        assert "<li>b</li>" in html
        assert "<li>c</li>" in html
        assert html.count("</ul>") == 1

    def test_code_block(self):
        md = "```\nfoo\nbar\n```"
        html = _md_to_html(md)
        assert "<pre><code>" in html
        assert "</code></pre>" in html

    def test_plain_paragraph(self):
        md = "hello world"
        html = _md_to_html(md)
        assert "<p>hello world</p>" in html

    def test_empty_input(self):
        assert _md_to_html("") == ""


# ============ 3. 导出流程 + 存储验证 ============

class TestExportFlow:
    def test_export_md_creates_file(self, exporter, sample_report, workspace_root):
        result = exporter.export(sample_report, fmt="md")
        path = Path(result["path"])
        assert path.exists()
        assert path.read_bytes().decode("utf-8") == sample_report["content"]
        assert result["format"] == "md"
        assert len(result["sha256"]) == 64
        assert result["size_bytes"] == len(sample_report["content"].encode("utf-8"))
        assert result["export_id"].startswith("export_")

    def test_export_html_creates_file(self, exporter, sample_report, workspace_root):
        result = exporter.export(sample_report, fmt="html")
        path = Path(result["path"])
        assert path.exists()
        assert "<!DOCTYPE html>" in path.read_text(encoding="utf-8")
        assert result["format"] == "html"

    def test_export_json_creates_file(self, exporter, sample_report, workspace_root):
        result = exporter.export(sample_report, fmt="json")
        path = Path(result["path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["report_id"] == sample_report["report_id"]

    def test_sha256_matches_actual_file(self, exporter, sample_report):
        result = exporter.export(sample_report, fmt="md")
        actual = hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest()
        assert actual == result["sha256"]

    def test_file_under_exports_root(self, exporter, sample_report, workspace_root):
        result = exporter.export(sample_report, fmt="md")
        target = Path(result["path"]).resolve()
        assert target.is_relative_to((workspace_root / "task_exports").resolve())

    def test_re_export_overwrites_and_adds_history(self, exporter, sample_report,
                                                   db_conn):
        first = exporter.export(sample_report, fmt="md")
        # 修改内容后再次导出
        sample_report["content"] = "# Updated"
        second = exporter.export(sample_report, fmt="md")
        assert first["sha256"] != second["sha256"]
        assert first["export_id"] != second["export_id"]
        # 历史应有 2 条
        history = exporter.list_exports(
            task_id=sample_report["task_id"], report_id=sample_report["report_id"])
        assert len(history) == 2
        # 倒序：最新的在前
        assert history[0]["export_id"] == second["export_id"]
        assert history[1]["export_id"] == first["export_id"]
        # 磁盘文件是新版
        assert "Updated" in Path(second["path"]).read_text(encoding="utf-8")

    def test_list_exports_orders_by_time_desc(self, exporter, sample_report):
        for fmt in ("md", "html", "json"):
            exporter.export(sample_report, fmt=fmt)
        history = exporter.list_exports(
            task_id=sample_report["task_id"], report_id=sample_report["report_id"])
        assert len(history) == 3
        assert {h["format"] for h in history} == set(_ALLOWED_FORMATS)
        times = [h["exported_at"] for h in history]
        assert times == sorted(times, reverse=True)

    def test_verify_export_matches_disk(self, exporter, sample_report):
        exporter.export(sample_report, fmt="md")
        result = exporter.verify_export(
            task_id=sample_report["task_id"],
            report_id=sample_report["report_id"], fmt="md")
        assert result["verified"] is True
        assert result["expected_sha256"] == result["actual_sha256"]

    def test_verify_export_detects_tampering(self, exporter, sample_report):
        result = exporter.export(sample_report, fmt="md")
        # 篡改文件
        Path(result["path"]).write_text("# tampered", encoding="utf-8")
        verify = exporter.verify_export(
            task_id=sample_report["task_id"],
            report_id=sample_report["report_id"], fmt="md")
        assert verify["verified"] is False
        assert verify["actual_sha256"] != verify["expected_sha256"]

    def test_verify_export_missing_file(self, exporter, sample_report):
        verify = exporter.verify_export(
            task_id=sample_report["task_id"],
            report_id=sample_report["report_id"], fmt="md")
        assert verify["verified"] is False
        assert verify["reason"] == "file_missing"

    def test_export_rejects_unsafe_id_without_writing(self, exporter, sample_report,
                                                      workspace_root):
        # 直接调 _resolve_path 触发校验
        with pytest.raises(ValueError):
            exporter._resolve_path(task_id="../bad", report_id="report_x", fmt="md")
        # workspace_root 不应该有 task_exports 目录
        assert not (workspace_root / "task_exports").exists()

    def test_export_rejects_bad_format(self, exporter, sample_report):
        with pytest.raises(ValueError, match="unsupported format"):
            exporter.export(sample_report, fmt="pdf")

    def test_schema_creates_table_idempotently(self, db_conn, workspace_root):
        """ensure_schema 多次调用不报错（幂等）。"""
        lock = threading.Lock()
        exp = ReportExporter(conn=db_conn, db_lock=lock, workspace_root=workspace_root)
        exp.ensure_schema()
        exp.ensure_schema()
        # 验证表存在
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_report_exports'"
        ).fetchall()
        assert len(rows) == 1


# ============ 4. 与 store 的集成（端到端） ============

class TestStoreIntegration:
    """测试 store.submit_report → exporter.export → 文件落盘 → 历史一致。"""

    def test_e2e_submit_then_export(self, db_conn, workspace_root):
        """复用与 store 相同的 schema，模拟 submit → export 全流程。"""
        lock = threading.Lock()
        exporter = ReportExporter(conn=db_conn, db_lock=lock,
                                  workspace_root=workspace_root)
        exporter.ensure_schema()

        # 1. 模拟 submit_report
        report_id = "report_e2e_test001"
        task_id = "task_20260821_143015_abc123"
        content = "# E2E\n\n- check\n"
        submitted_at = "2026-08-21T15:00:00+00:00"
        db_conn.execute(
            "INSERT INTO task_reports (report_id, task_id, agent_id, content, "
            "artifact_ids, acceptance_self_check, status, version, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report_id, task_id, "coding_agent", content,
             '["art_1"]', '{"k":"v"}', "submitted", 1, submitted_at),
        )
        db_conn.commit()

        # 2. 模拟 get_report 取出
        row = db_conn.execute(
            "SELECT * FROM task_reports WHERE report_id = ?", (report_id,)).fetchone()
        report = dict(row)
        report["artifact_ids"] = json.loads(report["artifact_ids"])
        report["acceptance_self_check"] = json.loads(report["acceptance_self_check"])

        # 3. 导出三种格式
        for fmt in ("md", "html", "json"):
            r = exporter.export(report, fmt=fmt)
            assert r["format"] == fmt
            assert Path(r["path"]).exists()
            assert len(r["sha256"]) == 64

        # 4. 历史应有 3 条
        history = exporter.list_exports(task_id=task_id, report_id=report_id)
        assert len(history) == 3

        # 5. verify_export 全部通过
        for fmt in ("md", "html", "json"):
            v = exporter.verify_export(task_id=task_id, report_id=report_id, fmt=fmt)
            assert v["verified"] is True, f"verify failed for {fmt}: {v}"
