"""P0.18.1 Workspace 授权模型 + DB schema + CRUD 测试。

覆盖验收：
1. authorized_workspaces 表存在 + 与旧 workspaces 表（已重命名为 run_workspace_meta）区分
2. CRUD：create / get / list / update / delete（soft delete）
3. mode/permissions CHECK 约束生效
4. record_run_workspace_meta 写入 + authorized_workspace_id 关联
5. mark_sandbox_for_cleanup + list_sandboxes_for_cleanup + mark_sandbox_deleted
6. record_provisioned_worker 含 workspace_id + tier 字段
7. sessions 表 workspace_id / workspace_locked 字段存在
8. runs 表 workspace_root / workspace_mode / authorized_workspace_id 字段存在
9. 旧 workspaces 表已重命名为 run_workspace_meta（迁移逻辑）
10. 兼容 create_workspace() 旧 caller（写入 run_workspace_meta 表）
11. touch_authorized_workspace 更新 last_used_at + usage_count
12. update_authorized_workspace enabled=false 填 deauthorized_at；enabled=true 清空
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio

from audit import EventStore, SqliteEventStore


@pytest_asyncio.fixture
async def store(tmp_path):
    """每个测试用独立临时 db 文件。"""
    db_path = tmp_path / "test_p018.db"
    s = SqliteEventStore(str(db_path))
    yield s
    await s.close()


# ============================================================
# Schema 校验
# ============================================================

@pytest.mark.asyncio
async def test_authorized_workspaces_table_exists(store):
    """authorized_workspaces 表存在。"""
    conn = sqlite3.connect(str(store.db_path))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "authorized_workspaces" in tables, "authorized_workspaces table should exist"


@pytest.mark.asyncio
async def test_old_workspaces_renamed_to_run_workspace_meta(store):
    """旧 workspaces 表已重命名为 run_workspace_meta（不再有 workspaces 表）。"""
    conn = sqlite3.connect(str(store.db_path))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "run_workspace_meta" in tables, "run_workspace_meta table should exist"
    # 新部署的 audit.db 不应有 workspaces 表（只有 run_workspace_meta）
    assert "workspaces" not in tables, "old workspaces table should be renamed"


@pytest.mark.asyncio
async def test_sessions_table_has_workspace_columns(store):
    """sessions 表含 workspace_id + workspace_locked 字段。"""
    conn = sqlite3.connect(str(store.db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    conn.close()
    assert "workspace_id" in cols
    assert "workspace_locked" in cols


@pytest.mark.asyncio
async def test_runs_table_has_workspace_columns(store):
    """runs 表含 workspace_root + workspace_mode + authorized_workspace_id 字段。"""
    conn = sqlite3.connect(str(store.db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    conn.close()
    assert "workspace_root" in cols
    assert "workspace_mode" in cols
    assert "authorized_workspace_id" in cols


@pytest.mark.asyncio
async def test_subagent_provisioned_workers_has_workspace_tier(store):
    """subagent_provisioned_workers 表含 workspace_id + tier 字段。"""
    conn = sqlite3.connect(str(store.db_path))
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(subagent_provisioned_workers)"
    ).fetchall()}
    conn.close()
    assert "workspace_id" in cols
    assert "tier" in cols


# ============================================================
# Authorized Workspaces CRUD
# ============================================================

@pytest.mark.asyncio
async def test_create_authorized_workspace_local_copy(store):
    """创建 local_copy 工作区。"""
    ws_id = str(uuid.uuid4())
    ws = await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test-project",
        mode="local_copy",
        permissions="read_write",
        description="测试项目",
        source_path="/tmp/test-project",
    )
    assert ws["workspace_id"] == ws_id
    assert ws["display_name"] == "test-project"
    assert ws["mode"] == "local_copy"
    assert ws["permissions"] == "read_write"
    assert ws["enabled"] == 1
    assert ws["usage_count"] == 0
    assert ws["authorized_at"] is not None


@pytest.mark.asyncio
async def test_create_authorized_workspace_bind_mount(store):
    """创建 bind_mount 工作区。"""
    ws_id = str(uuid.uuid4())
    ws = await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="agentops",
        mode="bind_mount",
        permissions="read_write_exec",
        source_path="E:/Project/agentops",
    )
    assert ws["mode"] == "bind_mount"
    assert ws["permissions"] == "read_write_exec"


@pytest.mark.asyncio
async def test_create_authorized_workspace_git_clone(store):
    """创建 git_clone 工作区。"""
    ws_id = str(uuid.uuid4())
    ws = await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="external-repo",
        mode="git_clone",
        permissions="read_only",
        git_url="https://github.com/example/repo.git",
        git_branch="main",
    )
    assert ws["mode"] == "git_clone"
    assert ws["git_url"] == "https://github.com/example/repo.git"
    assert ws["git_branch"] == "main"


@pytest.mark.asyncio
async def test_create_authorized_workspace_check_constraint_invalid_mode(store):
    """CHECK 约束：非法 mode 抛异常。"""
    ws_id = str(uuid.uuid4())
    with pytest.raises(Exception):
        await store.create_authorized_workspace(
            workspace_id=ws_id,
            display_name="bad",
            mode="invalid_mode",  # 非法
            permissions="read_only",
            source_path="/tmp",
        )


@pytest.mark.asyncio
async def test_create_authorized_workspace_check_constraint_missing_source_path(store):
    """CHECK 约束：local_copy 必须有 source_path。"""
    ws_id = str(uuid.uuid4())
    with pytest.raises(Exception):
        await store.create_authorized_workspace(
            workspace_id=ws_id,
            display_name="bad",
            mode="local_copy",
            permissions="read_only",
            # source_path 缺失
        )


@pytest.mark.asyncio
async def test_get_authorized_workspace(store):
    """get 单个工作区。"""
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test",
        mode="isolated",
        permissions="read_only",
    )
    fetched = await store.get_authorized_workspace(ws_id)
    assert fetched is not None
    assert fetched["workspace_id"] == ws_id

    # 不存在的 ID
    none_result = await store.get_authorized_workspace("nonexistent-id")
    assert none_result is None


@pytest.mark.asyncio
async def test_list_authorized_workspaces_excludes_disabled(store):
    """list 默认不含 disabled 工作区。"""
    ws1 = await store.create_authorized_workspace(
        workspace_id=str(uuid.uuid4()),
        display_name="enabled-ws",
        mode="isolated",
        permissions="read_only",
    )
    ws2_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws2_id,
        display_name="disabled-ws",
        mode="isolated",
        permissions="read_only",
    )
    await store.delete_authorized_workspace(ws2_id)

    enabled_only = await store.list_authorized_workspaces(include_disabled=False)
    assert len(enabled_only) == 1
    assert enabled_only[0]["display_name"] == "enabled-ws"

    all_ws = await store.list_authorized_workspaces(include_disabled=True)
    assert len(all_ws) == 2


@pytest.mark.asyncio
async def test_update_authorized_workspace_fields(store):
    """更新 display_name / permissions。"""
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="original",
        mode="local_copy",
        permissions="read_only",
        source_path="/tmp",
    )
    updated = await store.update_authorized_workspace(
        ws_id,
        display_name="renamed",
        permissions="read_write_exec",
    )
    assert updated["display_name"] == "renamed"
    assert updated["permissions"] == "read_write_exec"


@pytest.mark.asyncio
async def test_update_authorized_workspace_disable_sets_deauthorized_at(store):
    """enabled=false 填 deauthorized_at。"""
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test",
        mode="isolated",
        permissions="read_only",
    )
    updated = await store.update_authorized_workspace(ws_id, enabled=False)
    assert updated["enabled"] == 0
    assert updated["deauthorized_at"] is not None


@pytest.mark.asyncio
async def test_update_authorized_workspace_re_enable_clears_deauthorized_at(store):
    """重新启用时 deauthorized_at 清空。"""
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test",
        mode="isolated",
        permissions="read_only",
    )
    await store.update_authorized_workspace(ws_id, enabled=False)
    updated = await store.update_authorized_workspace(ws_id, enabled=True)
    assert updated["enabled"] == 1
    assert updated["deauthorized_at"] is None


@pytest.mark.asyncio
async def test_delete_authorized_workspace_soft_delete(store):
    """delete 是 soft delete：enabled=0 + deauthorized_at 填充。"""
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test",
        mode="isolated",
        permissions="read_only",
    )
    found = await store.delete_authorized_workspace(ws_id)
    assert found is True

    # 记录仍在，但 enabled=0
    ws = await store.get_authorized_workspace(ws_id)
    assert ws is not None  # soft delete，记录还在
    assert ws["enabled"] == 0
    assert ws["deauthorized_at"] is not None

    # 删除不存在的 ID 返回 False
    found_again = await store.delete_authorized_workspace("nonexistent")
    assert found_again is False


@pytest.mark.asyncio
async def test_touch_authorized_workspace_increments_usage(store):
    """touch 更新 last_used_at + usage_count += 1。"""
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test",
        mode="isolated",
        permissions="read_only",
    )
    before = await store.get_authorized_workspace(ws_id)
    assert before["usage_count"] == 0
    assert before["last_used_at"] is None

    await store.touch_authorized_workspace(ws_id)
    await store.touch_authorized_workspace(ws_id)

    after = await store.get_authorized_workspace(ws_id)
    assert after["usage_count"] == 2
    assert after["last_used_at"] is not None


# ============================================================
# run_workspace_meta + sandbox 清理
# ============================================================

@pytest.mark.asyncio
async def test_record_run_workspace_meta_basic(store):
    """record_run_workspace_meta 写入 + authorized_workspace_id 关联。"""
    # 先创建 authorized_workspace
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test",
        mode="local_copy",
        permissions="read_write",
        source_path="/tmp/test",
    )
    # 创建 session + run（满足 FK 约束）
    await store.create_session("sess_test_1", agent_id="manager", user_id="u1")
    await store.init_run(
        run_id="run_test_1",
        session_id="sess_test_1",
        run_mode="conversational",
        agent_id="manager",
        initial_message="test",
    )
    # 写 run_workspace_meta
    await store.record_run_workspace_meta(
        run_id="run_test_1",
        workflow_id="",
        workspace_root="/sandbox/run_test_1",
        absolute_root="/sandbox/run_test_1",
        mode=0o755,
        authorized_workspace_id=ws_id,
        cleanup_at=None,
    )
    # 验证
    conn = sqlite3.connect(str(store.db_path))
    row = conn.execute(
        "SELECT workspace_root, authorized_workspace_id, cleanup_status, cleanup_at "
        "FROM run_workspace_meta WHERE run_id = ?",
        ("run_test_1",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "/sandbox/run_test_1"
    assert row[1] == ws_id
    assert row[2] == "active"
    assert row[3] is None


@pytest.mark.asyncio
async def test_mark_sandbox_for_cleanup(store):
    """mark_sandbox_for_cleanup 设置 cleanup_at + cleanup_status='scheduled'。"""
    await store.create_session("sess_test_2", agent_id="manager", user_id="u1")
    await store.init_run(
        run_id="run_test_2",
        session_id="sess_test_2",
        run_mode="conversational",
        agent_id="manager",
        initial_message="test",
    )
    await store.record_run_workspace_meta(
        run_id="run_test_2",
        workflow_id="",
        workspace_root="/sandbox/run_test_2",
        absolute_root="/sandbox/run_test_2",
        mode=0o755,
    )
    cleanup_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    await store.mark_sandbox_for_cleanup("ws_id_xxx", "run_test_2", cleanup_at)

    conn = sqlite3.connect(str(store.db_path))
    row = conn.execute(
        "SELECT cleanup_at, cleanup_status FROM run_workspace_meta WHERE run_id = ?",
        ("run_test_2",),
    ).fetchone()
    conn.close()
    assert row[0] == cleanup_at
    assert row[1] == "scheduled"


@pytest.mark.asyncio
async def test_list_sandboxes_for_cleanup(store):
    """list_sandboxes_for_cleanup 返回 cleanup_at <= now 的记录。"""
    await store.create_session("sess_test_3", agent_id="manager", user_id="u1")
    await store.init_run(
        run_id="run_test_3",
        session_id="sess_test_3",
        run_mode="conversational",
        agent_id="manager",
        initial_message="test",
    )
    past_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await store.record_run_workspace_meta(
        run_id="run_test_3",
        workflow_id="",
        workspace_root="/sandbox/run_test_3",
        absolute_root="/sandbox/run_test_3",
        mode=0o755,
    )
    await store.mark_sandbox_for_cleanup("ws_x", "run_test_3", past_time)

    now_iso = datetime.now(timezone.utc).isoformat()
    sandboxes = await store.list_sandboxes_for_cleanup(now_iso)
    assert len(sandboxes) >= 1
    found = any(s["run_id"] == "run_test_3" for s in sandboxes)
    assert found, "run_test_3 should be in cleanup list"


@pytest.mark.asyncio
async def test_mark_sandbox_deleted(store):
    """mark_sandbox_deleted 标记 cleanup_status='deleted'。"""
    await store.create_session("sess_test_4", agent_id="manager", user_id="u1")
    await store.init_run(
        run_id="run_test_4",
        session_id="sess_test_4",
        run_mode="conversational",
        agent_id="manager",
        initial_message="test",
    )
    await store.record_run_workspace_meta(
        run_id="run_test_4",
        workflow_id="",
        workspace_root="/sandbox/run_test_4",
        absolute_root="/sandbox/run_test_4",
        mode=0o755,
    )
    await store.mark_sandbox_deleted("run_test_4")

    conn = sqlite3.connect(str(store.db_path))
    row = conn.execute(
        "SELECT cleanup_status FROM run_workspace_meta WHERE run_id = ?",
        ("run_test_4",),
    ).fetchone()
    conn.close()
    assert row[0] == "deleted"


# ============================================================
# 兼容旧 create_workspace() caller
# ============================================================

@pytest.mark.asyncio
async def test_legacy_create_workspace_compat(store):
    """旧 create_workspace() 仍可用（写入 run_workspace_meta 表，不含 authorized_workspace_id）。"""
    await store.create_session("sess_test_5", agent_id="manager", user_id="u1")
    await store.init_run(
        run_id="run_test_5",
        session_id="sess_test_5",
        run_mode="conversational",
        agent_id="manager",
        initial_message="test",
    )
    # 用旧签名调用
    await store.create_workspace(
        run_id="run_test_5",
        workflow_id="wf_x",
        workspace_root="workspace/wf_x/run_test_5/",
        absolute_root="/abs/wf_x/run_test_5/",
        mode=0o660,
    )
    # 验证写入 run_workspace_meta 表
    conn = sqlite3.connect(str(store.db_path))
    row = conn.execute(
        "SELECT workspace_root, mode, cleanup_status, authorized_workspace_id "
        "FROM run_workspace_meta WHERE run_id = ?",
        ("run_test_5",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "workspace/wf_x/run_test_5/"
    assert row[1] == 0o660
    assert row[2] == "active"
    assert row[3] is None  # 旧 caller 不含 authorized_workspace_id


# ============================================================
# record_provisioned_worker 含 workspace_id + tier
# ============================================================

@pytest.mark.asyncio
async def test_record_provisioned_worker_with_workspace_and_tier(store):
    """record_provisioned_worker 接受 workspace_id + tier 参数。"""
    ws_id = str(uuid.uuid4())
    await store.create_authorized_workspace(
        workspace_id=ws_id,
        display_name="test",
        mode="local_copy",
        permissions="read_write_exec",
        source_path="/tmp",
    )
    await store.create_session("sess_test_6", agent_id="manager", user_id="u1")
    await store.init_run(
        run_id="run_test_6",
        session_id="sess_test_6",
        run_mode="conversational",
        agent_id="manager",
        initial_message="test",
    )
    # provision_subagent 创建 subagent
    await store.provision_subagent(
        subagent_id="sub_test_6",
        actor_id="run_test_6:node_1",
        run_id="run_test_6",
        node_id="node_1",
        harness_type="opencode",
        lease_generation=1,
    )
    # record_provisioned_worker 含 workspace_id + tier
    await store.record_provisioned_worker(
        subagent_id="sub_test_6",
        lease_generation=1,
        worker_id="worker_test_6",
        runtime_placement="docker_container",
        container_id="container_abc",
        workspace_id=ws_id,
        tier="T3",
    )
    # 验证
    conn = sqlite3.connect(str(store.db_path))
    row = conn.execute(
        "SELECT workspace_id, tier, container_id FROM subagent_provisioned_workers "
        "WHERE worker_id = ?",
        ("worker_test_6",),
    ).fetchone()
    conn.close()
    assert row[0] == ws_id
    assert row[1] == "T3"
    assert row[2] == "container_abc"


# ============================================================
# 迁移：旧 audit.db 含 workspaces 表
# ============================================================

@pytest.mark.asyncio
async def test_migration_old_workspaces_table_renamed(tmp_path):
    """旧 audit.db 含 workspaces 表 → 启动 SqliteEventStore 后应自动重命名为 run_workspace_meta。"""
    db_path = tmp_path / "test_migration.db"
    # 1. 手动建一个旧 workspaces 表
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            parent_run_id TEXT,
            workflow_id TEXT,
            workflow_revision INTEGER NOT NULL DEFAULT 1,
            run_mode TEXT NOT NULL,
            agent_id TEXT,
            initial_message TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            inputs TEXT,
            final_outputs TEXT,
            error TEXT,
            started_at TEXT,
            finished_at TEXT,
            total_tokens_in INTEGER NOT NULL DEFAULT 0,
            total_tokens_out INTEGER NOT NULL DEFAULT 0,
            total_cost_usd REAL NOT NULL DEFAULT 0.0,
            cancellation_reason TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            last_activity_at TEXT NOT NULL,
            dormant_at TEXT,
            archived_at TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            attached_run_count INTEGER NOT NULL DEFAULT 0,
            thread_id TEXT,
            thread_name TEXT,
            thread_tool_digest TEXT,
            voice_active INTEGER NOT NULL DEFAULT 0,
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE workspaces (
            run_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            absolute_root TEXT NOT NULL,
            mode INTEGER NOT NULL DEFAULT 448,
            size_bytes INTEGER,
            cleanup_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        INSERT INTO sessions VALUES ('s1', 'u1', 'manager', 'test', 'active', '2026-08-11', NULL, NULL, 0, 0, NULL, NULL, NULL, 0, NULL, '2026-08-11', '2026-08-11');
        INSERT INTO runs VALUES ('r1', 's1', NULL, 'wf', 1, 'templated', NULL, NULL, 'completed', NULL, NULL, NULL, '2026-08-11', '2026-08-11', 0, 0, 0.0, NULL, NULL, '2026-08-11', '2026-08-11');
        INSERT INTO workspaces VALUES ('r1', 'wf', 'workspace/wf/r1/', '/abs/wf/r1/', 448, NULL, NULL, '2026-08-11');
    """)
    conn.commit()
    conn.close()

    # 2. 启动 SqliteEventStore → 触发迁移
    s = SqliteEventStore(str(db_path))
    await s.close()

    # 3. 验证 workspaces 表已被重命名为 run_workspace_meta
    conn = sqlite3.connect(str(db_path))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "run_workspace_meta" in tables
    # 旧 workspaces 表不应存在（已 rename）
    assert "workspaces" not in tables

    # 4. 验证数据保留
    row = conn.execute(
        "SELECT run_id, workspace_root FROM run_workspace_meta WHERE run_id = ?",
        ("r1",),
    ).fetchone()
    assert row is not None
    assert row[0] == "r1"
    assert row[1] == "workspace/wf/r1/"

    # 5. 验证新列已补（cleanup_status / authorized_workspace_id）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(run_workspace_meta)").fetchall()}
    assert "cleanup_status" in cols
    assert "authorized_workspace_id" in cols
    conn.close()
