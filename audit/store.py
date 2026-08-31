"""EventStore — 事件持久化层（v3 三层架构）。

v3 改造（2026-08-09）：
  - sessions 表收窄：只装用户↔Agent 对话，删除 workflow_id/run_mode/inputs/final_outputs/started_at/finished_at
  - 新增 runs 表：DAG 执行实例（含 workflow_id/run_mode/inputs/final_outputs/status/tokens 等）
  - 新增 subagents 表：一次性执行体（含 actor_id/run_id/node_id/lease_generation/harness_type/status 等）
  - 新增 10+ 张配套表：workflows/workflow_revisions/run_events/handoffs/node_executions/
                       workspaces/run_artifacts/run_memory/parent_child_runs/run_skill_contexts/
                       subagent_commands/subagent_checkpoints/subagent_provisioned_workers/
                       agents/nodes/users
  - dag_events → run_events（FK 从 sessions 改为 runs）
  - raw_harness_events 新增 run_id/subagent_id 列 + FK
  - widget_inputs 新增 run_id 列
  - usage_records 新增 run_id/subagent_id 列
  - parent_child_sessions → parent_child_runs
  - session_events 新增 run_id 列（v3 新增）
  - session_memory.source_session_id → source_run_id（FK to runs）
  - sessions.status 收窄为 active/dormant/archived（移除 running/completed/failed/cancelled）
  - 新增 SessionStatus / SubagentStatus 枚举（v3）

存储选型：SQLite（P0~P2）。生产可切 Postgres，接口层抽象 EventStore 协议。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.security_store import SecurityStoreMixin
from orchestrator.protocol import DagEvent, DagEventType

logger = logging.getLogger(__name__)


# ====== DDL ======

_SCHEMA = """
-- ============================================================
-- Layer 1: Session（用户↔Agent 对话）
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,                     -- "session_<timestamp>"
    user_id             TEXT NOT NULL,
    agent_id            TEXT NOT NULL,                        -- manager / coding_agent / knowledge_agent
    title               TEXT,
    status              TEXT NOT NULL DEFAULT 'active',       -- active / dormant / archived
    last_activity_at    TEXT NOT NULL,
    dormant_at          TEXT,
    archived_at         TEXT,
    message_count       INTEGER NOT NULL DEFAULT 0,
    attached_run_count  INTEGER NOT NULL DEFAULT 0,           -- trigger_workflow 派发的 run 数
    thread_id           TEXT,
    thread_name         TEXT,
    thread_tool_digest  TEXT,
    voice_active        INTEGER NOT NULL DEFAULT 0,
    workspace_id        TEXT,                                 -- v2 P0.18.1: FK 到 authorized_workspaces（null=通用对话）
    workspace_locked    INTEGER NOT NULL DEFAULT 0,           -- v2 P0.18.1: session 内锁定 workspace
    metadata            JSON,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK (status IN ('active', 'dormant', 'archived')),
    FOREIGN KEY (workspace_id) REFERENCES authorized_workspaces(workspace_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last_activity ON sessions(last_activity_at DESC);

-- ============================================================
-- Workflow 注册表（启动时从 yaml 扫描）
-- ============================================================
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id         TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT,
    current_revision    INTEGER NOT NULL DEFAULT 1,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_revisions (
    workflow_id         TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    yaml_text           TEXT NOT NULL,
    yaml_hash           TEXT NOT NULL,
    node_ids            JSON NOT NULL,
    agent_ids           JSON NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (workflow_id, revision),
    UNIQUE (yaml_hash),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_workflow_revisions_workflow ON workflow_revisions(workflow_id, revision DESC);

-- ============================================================
-- Layer 2: Run（DAG 执行实例）
-- ============================================================
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,                     -- "run_<timestamp>_<nano>"
    session_id          TEXT NOT NULL,                        -- 挂载到哪个 session
    parent_run_id       TEXT,                                 -- 嵌套 run 的父 run
    workflow_id         TEXT,                                 -- templated/hybrid 必填
    workflow_revision   INTEGER NOT NULL DEFAULT 1,
    run_mode            TEXT NOT NULL,                        -- templated/hybrid/conversational/task
    agent_id            TEXT,                                 -- conversational/task 必填
    initial_message     TEXT,                                 -- conversational/task 必填
    status              TEXT NOT NULL DEFAULT 'pending',      -- pending/running/waiting/completed/failed/cancelled
    inputs              JSON,
    final_outputs       JSON,
    error               TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    total_tokens_in     INTEGER NOT NULL DEFAULT 0,
    total_tokens_out    INTEGER NOT NULL DEFAULT 0,
    total_cost_usd      REAL NOT NULL DEFAULT 0.0,
    cancellation_reason TEXT,
    workspace_root      TEXT NOT NULL DEFAULT '',             -- v2 P0.18.1: 实际 host 路径
    workspace_mode      TEXT NOT NULL DEFAULT '',             -- v2 P0.18.1: 落地后的 mode（local_copy/bind_mount/git_clone/isolated）
    authorized_workspace_id TEXT,                             -- v2 P0.18.1: FK 到 authorized_workspaces（null=通用对话）
    metadata            JSON,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY (parent_run_id) REFERENCES runs(run_id) ON DELETE SET NULL,
    FOREIGN KEY (authorized_workspace_id) REFERENCES authorized_workspaces(workspace_id) ON DELETE SET NULL,
    CHECK (status IN ('pending', 'running', 'waiting', 'completed', 'failed', 'cancelled')),
    CHECK ((run_mode IN ('conversational', 'task') AND agent_id IS NOT NULL AND initial_message IS NOT NULL)
        OR (run_mode IN ('templated', 'hybrid') AND workflow_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON runs(workflow_id, workflow_revision);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id);

-- ============================================================
-- Layer 3: Subagent（一次性执行体）
-- ============================================================
CREATE TABLE IF NOT EXISTS subagents (
    subagent_id         TEXT PRIMARY KEY,                     -- 物理身份
    actor_id            TEXT NOT NULL,                        -- 逻辑身份：run_id:node_id
    run_id              TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    lease_generation    INTEGER NOT NULL DEFAULT 1,
    harness_type        TEXT NOT NULL,
    harness_instance_id TEXT,
    status              TEXT NOT NULL DEFAULT 'provisioning', -- provisioning/running/handoff/cleanup/completed/failed
    runtime_placement   TEXT NOT NULL DEFAULT 'in_process',
    workspace_ref       TEXT,
    container_id        TEXT,
    process_id          INTEGER,
    thread_id           TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    terminated_at       TEXT,
    cleanup_status      TEXT,
    error               TEXT,
    metadata            JSON,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    CHECK (status IN ('provisioning', 'running', 'handoff', 'cleanup', 'completed', 'failed')),
    CHECK (runtime_placement IN ('in_process', 'docker_container', 'subprocess')),
    CHECK (actor_id = run_id || ':' || node_id)
);
CREATE INDEX IF NOT EXISTS idx_subagents_run ON subagents(run_id);
CREATE INDEX IF NOT EXISTS idx_subagents_actor ON subagents(actor_id);
CREATE INDEX IF NOT EXISTS idx_subagents_actor_lease ON subagents(actor_id, lease_generation DESC);
CREATE INDEX IF NOT EXISTS idx_subagents_status ON subagents(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_subagents_active_actor
    ON subagents(run_id, node_id)
    WHERE status IN ('provisioning', 'running', 'handoff');

-- ============================================================
-- Run 级事件流（v2 dag_events → v3 run_events）
-- ============================================================
CREATE TABLE IF NOT EXISTS run_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    sequence            INTEGER NOT NULL,
    event_type          TEXT NOT NULL,
    node_id             TEXT,
    subagent_id         TEXT,
    payload             JSON NOT NULL,
    payload_digest      TEXT NOT NULL,
    occurred_at         TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_run_events_node ON run_events(run_id, node_id);
CREATE INDEX IF NOT EXISTS idx_run_events_subagent ON run_events(run_id, subagent_id);
CREATE INDEX IF NOT EXISTS idx_run_events_type ON run_events(run_id, event_type);

-- ============================================================
-- 双通道原始事件（FK 改 runs/subagents）
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_harness_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    subagent_id         TEXT NOT NULL,
    node_id             TEXT,
    harness             TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    raw_payload         JSON NOT NULL,
    payload_digest      TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (subagent_id) REFERENCES subagents(subagent_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_raw_events_run ON raw_harness_events(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_events_subagent ON raw_harness_events(subagent_id);
CREATE INDEX IF NOT EXISTS idx_raw_events_harness ON raw_harness_events(harness, event_type);

-- ============================================================
-- Handoff 记录
-- ============================================================
CREATE TABLE IF NOT EXISTS handoffs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    from_node_id        TEXT NOT NULL,
    from_subagent_id    TEXT NOT NULL,
    to_node_id          TEXT NOT NULL,
    port                TEXT NOT NULL DEFAULT 'default',
    payload             JSON NOT NULL,
    payload_digest      TEXT NOT NULL,
    payload_size        INTEGER NOT NULL,
    summary             TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',      -- pending/applied/failed/corrected
    applied_at          TEXT,
    failure_reason      TEXT,
    occurred_at         TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (from_subagent_id) REFERENCES subagents(subagent_id) ON DELETE CASCADE,
    CHECK (status IN ('pending', 'applied', 'failed', 'corrected'))
);
CREATE INDEX IF NOT EXISTS idx_handoffs_run ON handoffs(run_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_from ON handoffs(run_id, from_node_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_to ON handoffs(run_id, to_node_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(run_id, status);

-- ============================================================
-- 节点执行记录（跨 lease_generation 历史）
-- ============================================================
CREATE TABLE IF NOT EXISTS node_executions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    node_type           TEXT NOT NULL,
    lease_generation    INTEGER NOT NULL DEFAULT 1,
    subagent_id         TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    started_at          TEXT,
    finished_at         TEXT,
    duration_ms         INTEGER,
    tokens_in           INTEGER NOT NULL DEFAULT 0,
    tokens_out          INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0.0,
    resolved_provider   TEXT,
    resolved_model      TEXT,
    error               TEXT,
    error_type          TEXT,
    upstream_inputs     JSON,
    outputs             JSON,
    skip_if_expr        TEXT,
    file_outputs        JSON,
    metadata            JSON,
    UNIQUE (run_id, node_id, lease_generation),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (subagent_id) REFERENCES subagents(subagent_id) ON DELETE SET NULL,
    CHECK (status IN ('pending', 'ready', 'waiting', 'running', 'completed', 'failed', 'skipped'))
);
CREATE INDEX IF NOT EXISTS idx_node_executions_run ON node_executions(run_id);
CREATE INDEX IF NOT EXISTS idx_node_executions_status ON node_executions(run_id, status);
CREATE INDEX IF NOT EXISTS idx_node_executions_subagent ON node_executions(subagent_id);

-- ============================================================
-- 用量记录（FK 改 runs）
-- ============================================================
CREATE TABLE IF NOT EXISTS usage_records (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL,
    node_id                 TEXT NOT NULL,
    subagent_id             TEXT,
    provider_id             TEXT NOT NULL,
    model                   TEXT NOT NULL,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    duration_ms             INTEGER NOT NULL DEFAULT 0,
    cost_usd                REAL NOT NULL DEFAULT 0.0,
    fallback_from_provider  TEXT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_usage_run ON usage_records(run_id);
CREATE INDEX IF NOT EXISTS idx_usage_node ON usage_records(run_id, node_id);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_records(provider_id);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_records(created_at);

-- ============================================================
-- Widget 输入（HIL 介入点，FK 改 runs）
-- ============================================================
CREATE TABLE IF NOT EXISTS widget_inputs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    widget_id           TEXT NOT NULL,
    node_id             TEXT,
    input_payload       JSON NOT NULL,
    user_id             TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    submitted_at        TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_widget_inputs_run ON widget_inputs(run_id);
CREATE INDEX IF NOT EXISTS idx_widget_inputs_widget ON widget_inputs(widget_id);

-- ============================================================
-- Run Workspace 元数据（v2 P0.18.1 重命名：原 workspaces → run_workspace_meta）
-- 说明：每个 run 的 sandbox 路径 + 清理时间。与 authorized_workspaces（用户授权表）区分
-- ============================================================
CREATE TABLE IF NOT EXISTS run_workspace_meta (
    run_id              TEXT PRIMARY KEY,
    workflow_id         TEXT NOT NULL,
    workspace_root      TEXT NOT NULL,
    absolute_root       TEXT NOT NULL,
    mode                INTEGER NOT NULL DEFAULT 448,
    size_bytes          INTEGER,
    cleanup_at          TEXT,                            -- ISO 8601, 由 patroller 延迟清理
    cleanup_status      TEXT NOT NULL DEFAULT 'active',  -- active/scheduled/deleted
    authorized_workspace_id TEXT,                        -- FK 到 authorized_workspaces（v2 新增，nullable）
    created_at          TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (authorized_workspace_id) REFERENCES authorized_workspaces(workspace_id) ON DELETE SET NULL,
    CHECK (cleanup_status IN ('active', 'scheduled', 'deleted'))
);
CREATE INDEX IF NOT EXISTS idx_run_workspace_meta_cleanup ON run_workspace_meta(cleanup_at);
CREATE INDEX IF NOT EXISTS idx_run_workspace_meta_ws ON run_workspace_meta(authorized_workspace_id);

-- ============================================================
-- 用户授权的工作区注册表（v2 P0.18.1 新增）
-- 用户在 Settings → Workspaces 添加"已授权目录"，每个授权包含 path/mode/permissions
-- ============================================================
CREATE TABLE IF NOT EXISTS authorized_workspaces (
    workspace_id        TEXT PRIMARY KEY,                -- uuid v4
    display_name        TEXT NOT NULL,
    description         TEXT,
    mode                TEXT NOT NULL,                   -- local_copy / bind_mount / git_clone / isolated
    source_path         TEXT,                            -- local_copy/bind_mount: host 绝对路径
    git_url             TEXT,                            -- git_clone: https URL
    git_branch          TEXT,                            -- git_clone: 分支名
    permissions         TEXT NOT NULL,                   -- read_only / read_write / read_write_exec
    authorized_at       TEXT NOT NULL,                   -- ISO 8601, 用户授权时间
    last_used_at        TEXT,
    usage_count         INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1,      -- 取消授权后 enabled=0
    deauthorized_at     TEXT,                            -- soft delete 时间戳（audit trail）
    extra               TEXT,                            -- JSON: 排除 glob / 最大尺寸等
    CHECK (mode IN ('local_copy', 'bind_mount', 'git_clone', 'isolated')),
    CHECK (permissions IN ('read_only', 'read_write', 'read_write_exec')),
    CHECK (enabled IN (0, 1)),
    -- mode 与路径字段一致性约束
    CHECK (
        (mode IN ('local_copy', 'bind_mount') AND source_path IS NOT NULL)
        OR (mode = 'git_clone' AND git_url IS NOT NULL)
        OR (mode = 'isolated')
    )
);
CREATE INDEX IF NOT EXISTS idx_auth_workspaces_enabled ON authorized_workspaces(enabled);
CREATE INDEX IF NOT EXISTS idx_auth_workspaces_last_used ON authorized_workspaces(last_used_at DESC);

-- ============================================================
-- 系统设置（key-value，v2 新增：onboarding 状态 + manager 默认工作区等）
-- ============================================================
CREATE TABLE IF NOT EXISTS system_settings (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- ============================================================
-- Run 产物
-- ============================================================
CREATE TABLE IF NOT EXISTS run_artifacts (
    run_id              TEXT NOT NULL,
    name                TEXT NOT NULL,
    artifact_id         TEXT UNIQUE NOT NULL,
    file_path           TEXT NOT NULL,
    file_size           INTEGER NOT NULL,
    file_digest         TEXT NOT NULL,
    mime_type           TEXT,
    upload_token_hash   TEXT,
    upload_expires_at   TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, name),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run ON run_artifacts(run_id);

-- ============================================================
-- Run 摘要记忆（v2 session_memory.source_session_id → v3 source_run_id）
-- ============================================================
CREATE TABLE IF NOT EXISTS run_memory (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    memory_type         TEXT NOT NULL,                        -- run_summary/topic_summary/user_preference/cross_session_fact
    content             TEXT NOT NULL,
    tokens              INTEGER NOT NULL DEFAULT 0,
    importance          REAL NOT NULL DEFAULT 0.5,
    created_at          TEXT NOT NULL,
    expires_at          TEXT,
    UNIQUE (session_id, memory_type, run_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    CHECK (memory_type IN ('run_summary', 'topic_summary', 'user_preference', 'cross_session_fact'))
);
CREATE INDEX IF NOT EXISTS idx_run_memory_session ON run_memory(session_id, importance DESC, created_at DESC);

-- session_memory（保留用于 backward compat + 旧代码引用）
CREATE TABLE IF NOT EXISTS session_memory (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL,
    memory_type         TEXT NOT NULL,
    source_run_id       TEXT,
    content             TEXT NOT NULL,
    tokens              INTEGER NOT NULL DEFAULT 0,
    importance          REAL NOT NULL DEFAULT 0.5,
    created_at          TEXT NOT NULL,
    expires_at          TEXT,
    UNIQUE (session_id, memory_type, source_run_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (source_run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_session_memory_session ON session_memory(session_id, importance DESC, created_at DESC);

-- ============================================================
-- Run 嵌套关系（v2 parent_child_sessions → v3 parent_child_runs）
-- ============================================================
CREATE TABLE IF NOT EXISTS parent_child_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_run_id       TEXT NOT NULL,
    child_run_id        TEXT NOT NULL,
    parent_session_id   TEXT NOT NULL,
    child_session_id    TEXT NOT NULL,
    created_via         TEXT NOT NULL,                        -- trigger_workflow/request_cross_domain/dynamic_dag
    created_at          TEXT NOT NULL,
    UNIQUE (parent_run_id, child_run_id),
    FOREIGN KEY (parent_run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (child_run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_parent_child_parent ON parent_child_runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_parent_child_child ON parent_child_runs(child_run_id);
CREATE INDEX IF NOT EXISTS idx_parent_child_parent_session ON parent_child_runs(parent_session_id);

-- ============================================================
-- Skill 注入上下文（append-only）
-- ============================================================
CREATE TABLE IF NOT EXISTS run_skill_contexts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    skill_id            TEXT NOT NULL,
    context_digest      TEXT NOT NULL,
    context_json        JSON NOT NULL,
    injected_at         TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_run_skill_contexts_run ON run_skill_contexts(run_id);

-- ============================================================
-- Subagent 命令投递
-- ============================================================
CREATE TABLE IF NOT EXISTS subagent_commands (
    command_id          TEXT PRIMARY KEY,
    subagent_id         TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    command_type        TEXT NOT NULL,                        -- interrupt/cancel/retry/reassign/inject_hil
    payload             JSON NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',      -- pending/delivered/claimed/acknowledged/failed
    idempotency_key     TEXT NOT NULL,
    delivered_at        TEXT,
    claimed_at          TEXT,
    acknowledged_at     TEXT,
    failed_at           TEXT,
    failure_reason      TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (subagent_id, idempotency_key),
    FOREIGN KEY (subagent_id) REFERENCES subagents(subagent_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    CHECK (status IN ('pending', 'delivered', 'claimed', 'acknowledged', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_subagent_commands_subagent ON subagent_commands(subagent_id);
CREATE INDEX IF NOT EXISTS idx_subagent_commands_run ON subagent_commands(run_id);
CREATE INDEX IF NOT EXISTS idx_subagent_commands_status ON subagent_commands(status);

-- ============================================================
-- Subagent checkpoint 追加流
-- ============================================================
CREATE TABLE IF NOT EXISTS subagent_checkpoints (
    subagent_id         TEXT NOT NULL,
    checkpoint_version  INTEGER NOT NULL,
    checkpoint_json     JSON NOT NULL,
    checkpoint_sha256   TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (subagent_id, checkpoint_version),
    FOREIGN KEY (subagent_id) REFERENCES subagents(subagent_id) ON DELETE CASCADE,
    CHECK (length(checkpoint_sha256) = 64)
);
CREATE TRIGGER IF NOT EXISTS trg_subagent_checkpoints_no_update
    BEFORE UPDATE ON subagent_checkpoints
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'subagent_checkpoints is append-only');
    END;
CREATE INDEX IF NOT EXISTS idx_subagent_checkpoints_subagent ON subagent_checkpoints(subagent_id, checkpoint_version DESC);

-- ============================================================
-- Subagent 物理容器映射
-- ============================================================
CREATE TABLE IF NOT EXISTS subagent_provisioned_workers (
    subagent_id         TEXT NOT NULL,
    lease_generation    INTEGER NOT NULL,
    worker_id           TEXT NOT NULL,
    runtime_placement   TEXT NOT NULL,
    container_id        TEXT,
    process_id          INTEGER,
    thread_id           TEXT,
    workspace_id        TEXT,                                 -- v2 P0.18.1: FK 到 authorized_workspaces
    tier                TEXT,                                 -- v2 P0.18.1: T0/T1/T2/T3
    status              TEXT NOT NULL DEFAULT 'active',       -- active/releasing/released/failed
    started_at          TEXT NOT NULL,
    released_at         TEXT,
    cleanup_status      TEXT,
    UNIQUE (subagent_id, lease_generation),
    UNIQUE (worker_id),
    FOREIGN KEY (subagent_id) REFERENCES subagents(subagent_id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id) REFERENCES authorized_workspaces(workspace_id) ON DELETE SET NULL,
    CHECK (status IN ('active', 'releasing', 'released', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_subagent_workers_subagent ON subagent_provisioned_workers(subagent_id);
CREATE INDEX IF NOT EXISTS idx_subagent_workers_status ON subagent_provisioned_workers(status);

-- ============================================================
-- Agent 注册表（启动时从 yaml 扫描）
-- ============================================================
CREATE TABLE IF NOT EXISTS agents (
    agent_id            TEXT PRIMARY KEY,
    domain              TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    description         TEXT,
    harness             TEXT NOT NULL,
    model               TEXT,
    system_prompt       TEXT,
    output_files        JSON,
    permissions         JSON,
    knowledge_bases     JSON,
    max_concurrent_runs INTEGER NOT NULL DEFAULT 1,
    timeout_seconds     INTEGER NOT NULL DEFAULT 600,
    cost_limit_per_run  REAL,
    yaml_hash           TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_domain ON agents(domain);
CREATE INDEX IF NOT EXISTS idx_agents_active ON agents(is_active);

-- ============================================================
-- Node 定义（从 workflow yaml 提取）
-- ============================================================
CREATE TABLE IF NOT EXISTS nodes (
    workflow_id         TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    name                TEXT NOT NULL,
    type                TEXT NOT NULL,
    agent_id            TEXT,
    harness             TEXT,
    model               TEXT,
    domain              TEXT,
    business_role       TEXT,
    role_prompt         TEXT,
    timeout_seconds     INTEGER,
    skip_if             TEXT,
    inputs              JSON,
    outputs             JSON,
    branches            JSON,
    gateway_kind        TEXT,
    condition           TEXT,
    config              JSON,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (workflow_id, node_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    CHECK (type IN ('agent', 'parallel_branch', 'gateway'))
);
CREATE INDEX IF NOT EXISTS idx_nodes_workflow ON nodes(workflow_id);

-- ============================================================
-- 用户表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    user_id             TEXT PRIMARY KEY,
    display_name        TEXT,
    email               TEXT,
    role                TEXT NOT NULL DEFAULT 'user',
    is_active           INTEGER NOT NULL DEFAULT 1,
    metadata            JSON,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- ============================================================
-- Session 消息 / 事件（保留 Thread 模式持久化）
-- ============================================================
CREATE TABLE IF NOT EXISTS session_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    turn_id         TEXT,
    message_type    TEXT NOT NULL DEFAULT 'text',             -- v3 收紧 NOT NULL
    metadata        JSON,
    created_at      TEXT NOT NULL,
    UNIQUE (session_id, sequence),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_session_messages_turn ON session_messages(session_id, turn_id);

CREATE TABLE IF NOT EXISTS session_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    node_id         TEXT,
    run_id          TEXT,                                     -- v3 新增：关联 run
    payload         JSON NOT NULL,
    occurred_at     TEXT NOT NULL,
    UNIQUE (session_id, sequence),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id);
CREATE INDEX IF NOT EXISTS idx_session_events_type ON session_events(session_id, event_type);
CREATE INDEX IF NOT EXISTS idx_session_events_run ON session_events(run_id);

-- ============================================================
-- Lint issues（v2 不变）
-- ============================================================
CREATE TABLE IF NOT EXISTS lint_issues (
    id              TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    type            TEXT NOT NULL,
    severity        TEXT NOT NULL,                            -- critical/warning/info
    page_a          TEXT,
    page_b          TEXT,
    description     TEXT NOT NULL,
    auto_fixable    INTEGER NOT NULL DEFAULT 0,
    detected_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',          -- pending/resolved/ignored
    resolved_at     TEXT,
    resolved_by     TEXT,
    resolution_note TEXT,
    CHECK (severity IN ('critical', 'warning', 'info')),
    CHECK (status IN ('pending', 'resolved', 'ignored'))
);
CREATE INDEX IF NOT EXISTS idx_lint_issues_domain_status ON lint_issues(domain, status);
CREATE INDEX IF NOT EXISTS idx_lint_issues_status ON lint_issues(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lint_issues_dedup
    ON lint_issues(domain, type, COALESCE(page_a, ''), COALESCE(page_b, ''));
"""

# ====== 任务管理模块 DDL（P0 + V1） ======
# 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §2.2
# 落地方式：SqliteEventStore.__init__ 在 executescript(_SCHEMA) 后追加 executescript(_TASK_SCHEMA_P0)；
#           task_v1_enabled=True 时再追加 executescript(_TASK_SCHEMA_V1)。

_TASK_SCHEMA_P0 = """
-- ============================================================
-- P0 任务管理：4 表 + 9 触发器
-- 设计约定：version 乐观锁 / ISO 8601 时间戳 / TEXT 存 JSON
-- ============================================================

-- 1. global_revision：单行表，全局版本号，前端轮询判断刷新
CREATE TABLE IF NOT EXISTS global_revision (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision  INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT OR IGNORE INTO global_revision (singleton, revision) VALUES (1, 0);

-- 2. projects：项目表
CREATE TABLE IF NOT EXISTS projects (
    project_id        TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL DEFAULT 'code',
    local_path        TEXT,
    github_url        TEXT,
    feishu_doc_token  TEXT,
    workspace_id      TEXT,
    next_task_number  INTEGER NOT NULL DEFAULT 1,
    metadata          TEXT,
    version           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- 3. tasks：任务表（P0 5 态 + 前置 thread_id，V1 claim 协议依赖）
CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(project_id),
    identifier        TEXT,
    parent_task_id    TEXT REFERENCES tasks(task_id),
    title             TEXT NOT NULL,
    description       TEXT,
    status            TEXT NOT NULL DEFAULT 'idea',
    task_type         TEXT NOT NULL DEFAULT 'code',
    risk_level        TEXT NOT NULL DEFAULT 'medium',
    creator_type      TEXT NOT NULL DEFAULT 'user',
    creator_id        TEXT,
    creator_name      TEXT,
    assignee_type     TEXT,
    assignee_id       TEXT,
    assignee_name     TEXT,
    thread_id         TEXT,
    sort_order        REAL NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    closed_at         TEXT,
    CHECK (status IN ('idea','backlog','discussing','reviewing','closed')),
    CHECK (risk_level IN ('low','medium','high'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_thread ON tasks(thread_id);

-- 4. task_stages：阶段产出（P0 存 agent 输出，无 terminal）
CREATE TABLE IF NOT EXISTS task_stages (
    stage_id        TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id),
    stage_type      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    assigned_agent  TEXT,
    stage_input     TEXT,
    stage_output    TEXT,
    version         INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT,
    committed_at    TEXT,
    CHECK (status IN ('pending','running','committed','failed'))
);
CREATE INDEX IF NOT EXISTS idx_stages_task ON task_stages(task_id);

-- ============================================================
-- global_revision 触发器（P0：projects/tasks/task_stages 3 表 × 3 操作 = 9 个）
-- ============================================================
CREATE TRIGGER IF NOT EXISTS projects_rev_after_insert AFTER INSERT ON projects
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS projects_rev_after_update AFTER UPDATE ON projects
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS projects_rev_after_delete AFTER DELETE ON projects
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;

CREATE TRIGGER IF NOT EXISTS tasks_rev_after_insert AFTER INSERT ON tasks
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS tasks_rev_after_update AFTER UPDATE ON tasks
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS tasks_rev_after_delete AFTER DELETE ON tasks
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;

CREATE TRIGGER IF NOT EXISTS stages_rev_after_insert AFTER INSERT ON task_stages
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS stages_rev_after_update AFTER UPDATE ON task_stages
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS stages_rev_after_delete AFTER DELETE ON task_stages
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
"""

# V1 DDL（17 表补全 + 42 触发器由 gen_global_revision_triggers.py 生成）
# V1 启用时由 task_v1_enabled 参数控制 executescript；P0 阶段为空字符串，no-op。
# 触发器标记段（GEN_TRIGGERS_BEGIN..END）由 scripts/gen_global_revision_triggers.py 产出。
_TASK_SCHEMA_V1 = """
-- ============================================================
-- V1 任务管理：补全至 17 张表
-- 设计约定：
--   1. 所有写表带 version 字段（乐观锁）
--   2. 时间戳统一 ISO 8601 字符串（对齐现有 _SCHEMA）
--   3. JSON 字段用 TEXT 存（SQLite 无原生 JSON 类型）
--   4. FK 一律 ON DELETE CASCADE / SET NULL，与现有 store.py 一致
-- ============================================================

-- ---------- 1. projects（P0 已有，V1 仅保证存在） ----------
CREATE TABLE IF NOT EXISTS projects (
    project_id        TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL DEFAULT 'code',  -- code|knowledge|hybrid
    local_path        TEXT,
    github_url        TEXT,
    feishu_doc_token  TEXT,
    workspace_id      TEXT REFERENCES authorized_workspaces(workspace_id) ON DELETE SET NULL,
    next_task_number  INTEGER NOT NULL DEFAULT 1,
    metadata          TEXT,                          -- JSON
    version           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (type IN ('code', 'knowledge', 'hybrid'))
);

-- ---------- 2. ideas ----------
CREATE TABLE IF NOT EXISTS ideas (
    idea_id           TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source            TEXT NOT NULL DEFAULT 'manual', -- conversation|github_issue|feishu_doc|manual
    source_ref        TEXT,                           -- issue#42 / session_id
    content           TEXT NOT NULL,
    tags              TEXT,                           -- JSON 数组
    status            TEXT NOT NULL DEFAULT 'draft',  -- draft|open|converted|archived
    confidence        TEXT NOT NULL DEFAULT 'medium', -- high|medium|low
    priority          TEXT NOT NULL DEFAULT 'P2',     -- P0|P1|P2
    converted_task_id TEXT,
    version           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    CHECK (source IN ('conversation', 'github_issue', 'feishu_doc', 'manual')),
    CHECK (status IN ('draft', 'open', 'converted', 'archived')),
    CHECK (confidence IN ('high', 'medium', 'low'))
);
CREATE INDEX IF NOT EXISTS idx_ideas_project ON ideas(project_id);
CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status);

-- ---------- 3. tasks（P0 已有，V1 补全字段；对已建表 CREATE IF NOT EXISTS 不重建，新字段由 _TASK_V1_ALTERS 补充） ----------
CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    identifier         TEXT,                           -- AGENTOPS-12
    parent_task_id     TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    source_idea_id     TEXT REFERENCES ideas(idea_id) ON DELETE SET NULL,
    title              TEXT NOT NULL,
    description        TEXT,
    status             TEXT NOT NULL DEFAULT 'idea',   -- 14 态枚举见 §三
    task_type          TEXT NOT NULL DEFAULT 'code',   -- code|knowledge|design|hybrid
    risk_level         TEXT NOT NULL DEFAULT 'medium', -- low|medium|high
    creator_type       TEXT NOT NULL DEFAULT 'user',   -- user|agent
    creator_id         TEXT,
    creator_name       TEXT,
    assignee_type      TEXT,                           -- user|agent
    assignee_id        TEXT,
    assignee_name      TEXT,
    style_id           TEXT REFERENCES agent_styles(style_id) ON DELETE SET NULL,
    terminal_session_id TEXT,
    sort_order         REAL NOT NULL DEFAULT 0,
    thread_id          TEXT,
    approved           INTEGER,                        -- NULL|0|1（Reviewing 瞬时标记）
    version            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    closed_at          TEXT,
    archived_at        TEXT,
    CHECK (status IN ('idea','backlog','discussing','reviewing','decomposing',
                      'in_progress','validating','closing','closed',
                      'paused','blocked','failed','canceled','abandoned')),
    CHECK (task_type IN ('code','knowledge','design','hybrid')),
    CHECK (risk_level IN ('low','medium','high'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

-- ---------- 4. task_relations（V1 新增，替代 dependencies JSON） ----------
CREATE TABLE IF NOT EXISTS task_relations (
    relation_id     TEXT PRIMARY KEY,
    relation_type   TEXT NOT NULL,                     -- parent|blocks|related
    source_task_id  TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    target_task_id  TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    version         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    CHECK (relation_type IN ('parent','blocks','related')),
    CHECK (source_task_id <> target_task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_rel_source ON task_relations(source_task_id);
CREATE INDEX IF NOT EXISTS idx_task_rel_target ON task_relations(target_task_id);

-- 环检测触发器：禁止 parent 关系成环
CREATE TRIGGER IF NOT EXISTS task_relations_prevent_parent_cycle
BEFORE INSERT ON task_relations
WHEN NEW.relation_type = 'parent'
BEGIN
    SELECT RAISE(ABORT, 'RELATION_CYCLE') WHERE EXISTS (
        WITH RECURSIVE ancestors(id) AS (
            SELECT source_task_id FROM task_relations
            WHERE relation_type = 'parent' AND target_task_id = NEW.source_task_id
            UNION
            SELECT tr.source_task_id FROM task_relations tr
            JOIN ancestors ON tr.target_task_id = ancestors.id
            WHERE tr.relation_type = 'parent'
        )
        SELECT 1 FROM ancestors WHERE id = NEW.target_task_id
    );
END;

-- ---------- 5. task_stages（P0 已有，V1 补全 stage 类型） ----------
CREATE TABLE IF NOT EXISTS task_stages (
    stage_id        TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    stage_type      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending|running|committed|failed
    assigned_agent  TEXT,
    style_id        TEXT,
    stage_input     TEXT,                              -- JSON
    stage_output    TEXT,                              -- JSON
    version         INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT,
    committed_at    TEXT,
    CHECK (status IN ('pending','running','committed','failed'))
);
CREATE INDEX IF NOT EXISTS idx_stages_task ON task_stages(task_id);

-- ---------- 6. task_events（审计日志，V1+ 用） ----------
CREATE TABLE IF NOT EXISTS task_events (
    event_id    TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    stage_id    TEXT,
    event_type  TEXT NOT NULL,                         -- 见需求附录 B 枚举
    actor       TEXT,                                  -- user|agent_id
    payload     TEXT,                                  -- JSON
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);

-- ---------- 7. task_activities（字段级变更） ----------
CREATE TABLE IF NOT EXISTS task_activities (
    activity_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    actor_type   TEXT NOT NULL,                        -- user|agent
    actor_id     TEXT,
    actor_name   TEXT,
    changes      TEXT,                                 -- JSON {field: {before, after}}
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_activities_task ON task_activities(task_id, created_at);

-- ---------- 8. task_artifacts ----------
CREATE TABLE IF NOT EXISTS task_artifacts (
    artifact_id   TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    type          TEXT NOT NULL,                       -- code|doc|report|data
    path          TEXT,
    content_hash  TEXT,
    description   TEXT,
    version       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    CHECK (type IN ('code','doc','report','data'))
);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id);

-- ---------- 9. task_reports（博客评论模式的「文章」） ----------
CREATE TABLE IF NOT EXISTS task_reports (
    report_id            TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    agent_id             TEXT,
    session_id           TEXT,
    terminal_session_id  TEXT,
    content              TEXT NOT NULL,                -- markdown 正文
    artifact_ids         TEXT,                         -- JSON 数组
    acceptance_self_check TEXT,                        -- JSON
    status               TEXT NOT NULL DEFAULT 'submitted', -- submitted|approved|changes_requested
    version              INTEGER NOT NULL DEFAULT 0,
    submitted_at         TEXT NOT NULL,
    CHECK (status IN ('submitted','approved','changes_requested'))
);
CREATE INDEX IF NOT EXISTS idx_task_reports_task ON task_reports(task_id, submitted_at DESC);

-- ---------- 10. task_comments（通用评论：report/review/discussion 一表通吃） ----------
CREATE TABLE IF NOT EXISTS task_comments (
    comment_id     TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    report_id      TEXT REFERENCES task_reports(report_id) ON DELETE SET NULL,
    author_type    TEXT NOT NULL,                      -- user|agent
    author_id      TEXT,
    author_name    TEXT,
    body           TEXT NOT NULL,                      -- markdown
    comment_type   TEXT NOT NULL DEFAULT 'discussion', -- report|review|discussion
    decision       TEXT,                               -- approve|request_changes|NULL（仅 review）
    rollback_target TEXT,                              -- in_progress|decomposing|discussing
    thread_id      TEXT,
    version        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK (author_type IN ('user','agent')),
    CHECK (comment_type IN ('report','review','discussion')),
    CHECK (decision IN ('approve','request_changes') OR decision IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id, created_at);

-- ---------- 11. acceptance_criteria ----------
CREATE TABLE IF NOT EXISTS acceptance_criteria (
    criteria_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    description  TEXT NOT NULL,
    check_type   TEXT NOT NULL DEFAULT 'manual',       -- auto|manual
    status       TEXT NOT NULL DEFAULT 'pending',      -- pending|passed|failed
    version      INTEGER NOT NULL DEFAULT 0,
    checked_at   TEXT,
    CHECK (check_type IN ('auto','manual')),
    CHECK (status IN ('pending','passed','failed'))
);
CREATE INDEX IF NOT EXISTS idx_acceptance_task ON acceptance_criteria(task_id);

-- ---------- 12. design_docs ----------
CREATE TABLE IF NOT EXISTS design_docs (
    doc_id               TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    title                TEXT NOT NULL,
    path                 TEXT NOT NULL,               -- 指向 project.local_path 下文件
    content_hash         TEXT,
    affected_by_tasks    TEXT,                        -- JSON 数组
    last_updated_by_task TEXT,
    version              INTEGER NOT NULL DEFAULT 0,
    last_updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_design_docs_project ON design_docs(project_id);

-- ---------- 13. doc_change_proposals ----------
CREATE TABLE IF NOT EXISTS doc_change_proposals (
    proposal_id      TEXT PRIMARY KEY,
    doc_id           TEXT NOT NULL REFERENCES design_docs(doc_id) ON DELETE CASCADE,
    task_id          TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    change_type      TEXT NOT NULL,                   -- add|modify|deprecate|replace
    section_path     TEXT,
    old_content_hash TEXT,
    new_content      TEXT,
    rationale        TEXT,
    status           TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected|applied
    version          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    applied_at       TEXT,
    CHECK (change_type IN ('add','modify','deprecate','replace')),
    CHECK (status IN ('pending','approved','rejected','applied'))
);
CREATE INDEX IF NOT EXISTS idx_doc_proposals_task ON doc_change_proposals(task_id, status);

-- ---------- 14. design_doc_changes（变更历史） ----------
CREATE TABLE IF NOT EXISTS design_doc_changes (
    change_id    TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES design_docs(doc_id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    proposal_id  TEXT REFERENCES doc_change_proposals(proposal_id) ON DELETE SET NULL,
    change_type  TEXT NOT NULL,
    section_path TEXT,
    prev_hash    TEXT,
    new_hash     TEXT,
    changed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_changes_doc ON design_doc_changes(doc_id, changed_at DESC);

-- ---------- 15. agent_styles ----------
CREATE TABLE IF NOT EXISTS agent_styles (
    style_id               TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    description            TEXT,
    system_prompt_overlay  TEXT,
    permissions_overlay    TEXT,                       -- JSON {denied_tools_add: [...]}
    model_overlay          TEXT,                       -- JSON {id, provider}
    version                INTEGER NOT NULL DEFAULT 0
);

-- ---------- 16. task_runs（弱关联 Run/Session/Terminal） ----------
CREATE TABLE IF NOT EXISTS task_runs (
    link_id             TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    run_id              TEXT REFERENCES runs(run_id) ON DELETE SET NULL,     -- 可空
    session_id          TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    terminal_session_id TEXT,
    role                TEXT NOT NULL DEFAULT 'main_execution', -- main_execution|validation|discussion
    linked_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id);

-- ---------- 17. global_revision（P0 已有） ----------
CREATE TABLE IF NOT EXISTS global_revision (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision  INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT OR IGNORE INTO global_revision (singleton, revision) VALUES (1, 0);

-- ---------- 18. design_notes（v1.1 生命周期自动化：四态设计笔记） ----------
-- 记忆引擎核心表：agent 执行中硬性产出，非事后总结（DESIGN_task_lifecycle_automation §5.3.1）
CREATE TABLE IF NOT EXISTS design_notes (
    note_id       TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    project_id    TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'proposed',   -- proposed|implemented|rejected|archived
    content       TEXT NOT NULL,                      -- 必须含「为什么」设计理由
    supersedes    TEXT REFERENCES design_notes(note_id) ON DELETE SET NULL, -- 取代的旧笔记（演进链）
    reject_reason TEXT,                               -- rejected 态必填：否决理由
    source_run    TEXT,                               -- 产出该笔记的 run_id
    version       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK (status IN ('proposed','implemented','rejected','archived'))
);
CREATE INDEX IF NOT EXISTS idx_design_notes_task ON design_notes(task_id, status);
CREATE INDEX IF NOT EXISTS idx_design_notes_project ON design_notes(project_id, status);

-- ---------- 19. task_contributions（v1.1：讨论贡献账本） ----------
CREATE TABLE IF NOT EXISTS task_contributions (
    contribution_id TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    agent_id        TEXT NOT NULL,
    contribution_pct REAL NOT NULL CHECK (contribution_pct >= 0 AND contribution_pct <= 100),
    basis           TEXT,                             -- 评定依据（被采纳观点溯源）
    round           INTEGER NOT NULL DEFAULT 1,       -- 讨论轮次
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contributions_task ON task_contributions(task_id);
CREATE INDEX IF NOT EXISTS idx_contributions_agent ON task_contributions(agent_id, created_at DESC);

-- ---------- 20. agent_profiles（v1.1：声望档案，时间衰减按 created_at 加权计算） ----------
CREATE TABLE IF NOT EXISTS agent_profiles (
    agent_id            TEXT PRIMARY KEY,
    reputation          REAL NOT NULL DEFAULT 0,      -- 最近一次快照值（有效声望查询走账本加权）
    reputation_updated_at TEXT,
    tasks_won           INTEGER NOT NULL DEFAULT 0,   -- 拿到执行权的次数
    contribution_total  REAL NOT NULL DEFAULT 0,
    avg_report_score    REAL,
    updated_at          TEXT NOT NULL
);

-- ============================================================
-- global_revision 触发器（V1：14 表 × 3 操作 = 42 个）
-- 由 scripts/gen_global_revision_triggers.py 生成，勿手改。
-- P0 的 projects/task_stages 触发器已在 _TASK_SCHEMA_P0 内联，不在此重复。
-- ============================================================
-- >>> GEN_TRIGGERS_BEGIN (auto-generated, do not edit)
CREATE TRIGGER IF NOT EXISTS ideas_rev_after_insert AFTER INSERT ON ideas
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS ideas_rev_after_update AFTER UPDATE ON ideas
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS ideas_rev_after_delete AFTER DELETE ON ideas
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_relations_rev_after_insert AFTER INSERT ON task_relations
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_relations_rev_after_update AFTER UPDATE ON task_relations
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_relations_rev_after_delete AFTER DELETE ON task_relations
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_events_rev_after_insert AFTER INSERT ON task_events
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_events_rev_after_update AFTER UPDATE ON task_events
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_events_rev_after_delete AFTER DELETE ON task_events
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_activities_rev_after_insert AFTER INSERT ON task_activities
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_activities_rev_after_update AFTER UPDATE ON task_activities
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_activities_rev_after_delete AFTER DELETE ON task_activities
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_artifacts_rev_after_insert AFTER INSERT ON task_artifacts
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_artifacts_rev_after_update AFTER UPDATE ON task_artifacts
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_artifacts_rev_after_delete AFTER DELETE ON task_artifacts
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_reports_rev_after_insert AFTER INSERT ON task_reports
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_reports_rev_after_update AFTER UPDATE ON task_reports
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_reports_rev_after_delete AFTER DELETE ON task_reports
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_comments_rev_after_insert AFTER INSERT ON task_comments
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_comments_rev_after_update AFTER UPDATE ON task_comments
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_comments_rev_after_delete AFTER DELETE ON task_comments
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS acceptance_criteria_rev_after_insert AFTER INSERT ON acceptance_criteria
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS acceptance_criteria_rev_after_update AFTER UPDATE ON acceptance_criteria
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS acceptance_criteria_rev_after_delete AFTER DELETE ON acceptance_criteria
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS design_docs_rev_after_insert AFTER INSERT ON design_docs
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS design_docs_rev_after_update AFTER UPDATE ON design_docs
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS design_docs_rev_after_delete AFTER DELETE ON design_docs
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS doc_change_proposals_rev_after_insert AFTER INSERT ON doc_change_proposals
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS doc_change_proposals_rev_after_update AFTER UPDATE ON doc_change_proposals
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS doc_change_proposals_rev_after_delete AFTER DELETE ON doc_change_proposals
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS design_doc_changes_rev_after_insert AFTER INSERT ON design_doc_changes
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS design_doc_changes_rev_after_update AFTER UPDATE ON design_doc_changes
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS design_doc_changes_rev_after_delete AFTER DELETE ON design_doc_changes
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS agent_styles_rev_after_insert AFTER INSERT ON agent_styles
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS agent_styles_rev_after_update AFTER UPDATE ON agent_styles
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS agent_styles_rev_after_delete AFTER DELETE ON agent_styles
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_runs_rev_after_insert AFTER INSERT ON task_runs
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_runs_rev_after_update AFTER UPDATE ON task_runs
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS task_runs_rev_after_delete AFTER DELETE ON task_runs
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS tasks_rev_after_insert AFTER INSERT ON tasks
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS tasks_rev_after_update AFTER UPDATE ON tasks
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER IF NOT EXISTS tasks_rev_after_delete AFTER DELETE ON tasks
BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;
-- >>> GEN_TRIGGERS_END
"""

# ============================================================
# _TASK_SCHEMA_V3（V3 信息架构：终端注册表 + 布局持久化）
# 设计文档：docs/product-design/task-manage/DESIGN_task_management_module.md §4.13.1
# 落地方式：SqliteEventStore.__init__ 在 _TASK_SCHEMA_V1 后追加 executescript(_TASK_SCHEMA_V3)
#           （与 task_v1_enabled 同机制，V3 依赖 V1 的 14 态 tasks 表）。
# ============================================================
_TASK_SCHEMA_V3 = """
-- ---------- 18. terminal_sessions（V3：页面窗格与 psmux/tmux session 的映射） ----------
CREATE TABLE IF NOT EXISTS terminal_sessions (
    terminal_session_id TEXT PRIMARY KEY,          -- = psmux/tmux session name（task_{task_id} 或 manual_{id}）
    task_id            TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,  -- agent 窗格关联任务；手动窗口 NULL
    kind               TEXT NOT NULL DEFAULT 'shell',   -- agent / codex / claude / shell
    status             TEXT NOT NULL DEFAULT 'active',  -- active / done / dead
    created_at         TEXT NOT NULL,
    ended_at           TEXT,
    CHECK (kind IN ('agent','codex','claude','shell')),
    CHECK (status IN ('active','done','dead'))
);
CREATE INDEX IF NOT EXISTS idx_terminal_sessions_task ON terminal_sessions(task_id);

-- ---------- 19. terminal_layouts（V3：窗格布局持久化，每用户单行） ----------
CREATE TABLE IF NOT EXISTS terminal_layouts (
    layout_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL DEFAULT 'local',
    panes      TEXT NOT NULL,                      -- JSON: [{terminal_session_id, x, y, w, h}, ...]
    updated_at TEXT NOT NULL,
    UNIQUE(user_id)
);
"""

# task_comments 表 V3 新增 mentions 列（@agent 评论交互，§4.12.2）
_TASK_V3_ALTERS = [
    "ALTER TABLE task_comments ADD COLUMN mentions TEXT",  # JSON 数组：["coding_agent", ...]
]

# tasks 表 V1 新增 5 字段的安全补充（对已建 P0 表 ALTER ADD COLUMN）
# SQLite 不支持 ADD COLUMN IF NOT EXISTS，故在 __init__ 中用 PRAGMA 检查后逐条执行。
_TASK_V1_ALTERS = [
    "ALTER TABLE tasks ADD COLUMN source_idea_id TEXT REFERENCES ideas(idea_id) ON DELETE SET NULL",
    "ALTER TABLE tasks ADD COLUMN style_id TEXT REFERENCES agent_styles(style_id) ON DELETE SET NULL",
    "ALTER TABLE tasks ADD COLUMN terminal_session_id TEXT",
    "ALTER TABLE tasks ADD COLUMN approved INTEGER",
    "ALTER TABLE tasks ADD COLUMN archived_at TEXT",
]


# 脱敏：匹配 key 名（不含 token_count 等合法字段）
_REDACT_KEY = re.compile(
    r"^(api[_-]?key|authorization|bearer|secret|password|token)$",
    re.IGNORECASE,
)


def _redact_value(obj: Any) -> Any:
    """递归脱敏 dict/list 中的敏感字段。"""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _REDACT_KEY.match(str(k)):
                out[k] = "***"
            elif isinstance(v, str) and (
                v.lower().startswith("bearer ")
                or v.lower().startswith("sk-")
                or "api_key" in k.lower()
            ):
                out[k] = "***"
            else:
                out[k] = _redact_value(v)
        return out
    if isinstance(obj, list):
        return [_redact_value(item) for item in obj]
    return obj


def _dt_to_str(dt: datetime) -> str:
    """datetime → ISO 字符串（含时区）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    """ISO 字符串 → datetime。"""
    return datetime.fromisoformat(s)


def _digest_json(obj: Any) -> str:
    """计算 payload 的 SHA256（64 字符 hex）。用于 v3 内容寻址。"""
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ====== 协议 ======

class EventStore(ABC):
    """事件存储协议（v3 三层架构：sessions + runs + subagents）。

    逻辑上拆分为三个子协议：
      - SessionStore（sessions / session_messages / session_events / session_memory）
      - RunStore（runs / run_events / handoffs / node_executions / usage_records /
                  widget_inputs / workspaces / run_artifacts / run_memory /
                  parent_child_runs / run_skill_contexts / raw_harness_events）
      - SubagentStore（subagents / subagent_commands / subagent_checkpoints /
                       subagent_provisioned_workers）
    但实现类只需继承统一 EventStore ABC，避免 3 个抽象基类的依赖地狱。
    """

    # ============================================================
    # Layer 1: Session 管理
    # ============================================================

    @abstractmethod
    async def create_session(self, session_id: str, agent_id: str,
                             user_id: str = "", title: str = "",
                             workspace_id: str | None = None,
                             permission_level: str | None = None) -> None:
        """创建 Session 记录（v3: agent_id NOT NULL；status 默认 active）。

        P0.18.7b: workspace_id 关联 authorized_workspaces（None=通用对话）。
        permission_level: 会话级权限（与 workspace 解耦），创建时从 workspace.permissions 初始化。
        """

    @abstractmethod
    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """获取 Session 元数据。"""

    @abstractmethod
    async def touch_session(self, session_id: str) -> None:
        """更新 Session 最后活动时间。"""

    @abstractmethod
    async def update_session_status(self, session_id: str, status: str,
                                    last_activity: bool = True) -> None:
        """更新 session status（active/dormant/archived）。"""

    @abstractmethod
    async def update_session_title(self, session_id: str, title: str) -> None:
        """更新会话标题。"""

    @abstractmethod
    async def update_session_permission_level(self, session_id: str, permission_level: str) -> None:
        """更新会话级权限级别（与 workspace 解耦，随时可切换、立即生效）。"""

    @abstractmethod
    async def archive_session(self, session_id: str) -> None:
        """软归档会话（archived_at）。"""

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """删除 session 记录（异常路径 cleanup 用）。"""

    @abstractmethod
    async def increment_attached_run_count(self, session_id: str) -> None:
        """递增 session 的 attached_run_count（trigger_workflow 派发时调用）。"""

    @abstractmethod
    async def list_sessions(self, workflow_id: str | None = None,
                            status: str | None = None,
                            search: str | None = None,
                            limit: int = 50,
                            offset: int = 0) -> list[dict[str, Any]]:
        """session 列表筛选（仅对话层）。v3: 不再按 workflow_id 过滤（移至 runs 表）。"""

    @abstractmethod
    async def count_sessions(self, workflow_id: str | None = None,
                              status: str | None = None,
                              search: str | None = None) -> int:
        """session 总数。"""

    @abstractmethod
    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """单次 session 概要（v3: 不含 workflow_id/run_mode/inputs/final_outputs，这些在 runs 表）。"""

    @abstractmethod
    async def update_session_thread(
        self, session_id: str, thread_id: str, thread_name: str, tool_digest: str,
    ) -> None:
        """更新 session thread 信息（opencode/codex thread 复用）。"""

    @abstractmethod
    async def update_session_voice(self, session_id: str, voice_active: bool) -> None:
        """更新 session 语音模式状态。"""

    # ----- Session 消息 / 事件 -----

    @abstractmethod
    async def append_session_message(
        self, session_id: str, role: str, content: str | dict | list,
        turn_id: str | None = None, message_type: str = "text",
        metadata: dict | None = None,
    ) -> int:
        """追加一条 session 消息。返回 sequence。"""

    @abstractmethod
    async def get_session_messages(self, session_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """获取 session 消息历史。"""

    @abstractmethod
    async def append_session_event(
        self, session_id: str, event_type: str, payload: dict,
        node_id: str | None = None,
        run_id: str | None = None,
    ) -> int:
        """追加一条 session 事件。返回 sequence。v3: 新增 run_id 参数。"""

    @abstractmethod
    async def get_session_events(self, session_id: str, since: int = 0, limit: int = 10000) -> list[dict[str, Any]]:
        """获取 session 事件历史。"""

    # ----- Session 记忆 -----

    @abstractmethod
    async def add_session_memory(self, session_id: str, memory_type: str,
                                  content: str, source_run_id: str | None = None,
                                  tokens: int = 0, importance: float = 0.5,
                                  expires_at: str | None = None) -> None:
        """添加 Session 中期记忆。v3: source_session_id → source_run_id（FK to runs）。"""

    @abstractmethod
    async def list_session_memory(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """查询 Session 记忆。"""

    # ----- v2 兼容 session_memory 接口（保留 source_session_id 形式） -----

    @abstractmethod
    async def add_session_memory_v2(self, session_id: str, memory_type: str,
                                     content: str, source_session_id: str | None = None,
                                     tokens: int = 0, importance: float = 0.5,
                                     expires_at: str | None = None) -> None:
        """v2 兼容接口：source_session_id 实际值会被写到 source_run_id（语义同源）。"""

    # ============================================================
    # Layer 2: Run 管理
    # ============================================================

    @abstractmethod
    async def init_run(self, run_id: str, session_id: str,
                        workflow_id: str | None = None,
                        run_mode: str = "conversational",
                        agent_id: str | None = None,
                        initial_message: str | None = None,
                        parent_run_id: str | None = None,
                        inputs: dict | None = None) -> None:
        """创建 Run 记录（v3: session_id NOT NULL 必填）。"""

    @abstractmethod
    async def finalize_run(self, run_id: str, status: str,
                           finished_at: datetime | None = None,
                           total_tokens_in: int = 0,
                           total_tokens_out: int = 0,
                           total_cost_usd: float = 0.0,
                           error: str | None = None,
                           final_outputs: dict | None = None,
                           cancellation_reason: str | None = None) -> None:
        """Run 结束时更新状态。"""

    @abstractmethod
    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """获取 run 元数据。"""

    @abstractmethod
    async def list_runs(self, session_id: str | None = None,
                         workflow_id: str | None = None,
                         status: str | None = None,
                         limit: int = 50,
                         offset: int = 0) -> list[dict[str, Any]]:
        """run 列表筛选（v3: session_id / workflow_id / status 任一过滤）。"""

    @abstractmethod
    async def count_runs(self, session_id: str | None = None,
                          workflow_id: str | None = None,
                          status: str | None = None) -> int:
        """run 总数。"""

    @abstractmethod
    async def get_run_summary(self, run_id: str) -> dict[str, Any]:
        """run 概要（含 inputs/final_outputs/tokens/error 等）。"""

    @abstractmethod
    async def list_active_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        """查询所有运行中的 run（status IN pending, running, waiting），供监控中心展示。"""

    @abstractmethod
    async def list_stale_runs(
        self,
        threshold_iso: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """P0.18.13：查询未终止且 created_at < threshold_iso 的 run，供 patroller 收敛。"""

    @abstractmethod
    async def update_run_status(
        self,
        run_id: str,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        error: str | None = None,
    ) -> None:
        """更新 run 状态。可选回填 started_at/finished_at/error。

        实现层（SQLiteEventStore）会自动在状态转移时回填缺失字段：
          - pending/waiting → running：started_at 缺则填 now
          - 任意 → completed/failed/cancelled：finished_at 缺则填 now
        """

    # ----- Run 事件流 -----

    @abstractmethod
    async def append_run_event(self, run_id: str, event_type: str, payload: dict,
                                node_id: str | None = None,
                                subagent_id: str | None = None) -> int:
        """追加一条 run 事件（v3: 写入 run_events 表）。返回 sequence。"""

    @abstractmethod
    async def get_run_events(self, run_id: str, since: int = 0,
                              limit: int = 10000) -> list[DagEvent]:
        """按 sequence 查历史 DagEvent（兼容旧接口，列名=字段名，无需映射）。"""

    @abstractmethod
    async def get_node_detail(self, run_id: str, node_id: str) -> dict[str, Any]:
        """聚合某节点详情。v3: 从 run_events + raw_harness_events + handoffs + widget_inputs 聚合。"""

    @abstractmethod
    async def get_current_node(self, run_id: str) -> str | None:
        """查 run 最近一条 node.* 事件的 node_id。"""

    # ----- Raw harness 事件 -----

    @abstractmethod
    async def append_raw_event(self, run_id: str, subagent_id: str,
                                node_id: str | None,
                                harness: str, event_type: str,
                                raw_payload: dict[str, Any]) -> None:
        """追加一条原始 harness 事件（v3: 必填 run_id + subagent_id）。"""

    # ----- HIL / Widget -----

    @abstractmethod
    async def append_widget_input(self, run_id: str, widget_id: str,
                                   payload: dict[str, Any],
                                   session_id: str,
                                   node_id: str | None = None,
                                   user_id: str = "") -> None:
        """记录 HIL 介入点（v3: FK 必填 run_id + session_id）。"""

    # ----- Usage -----

    @abstractmethod
    async def record_usage(
        self,
        run_id: str,
        node_id: str,
        provider_id: str,
        model: str,
        *,
        subagent_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        duration_ms: int = 0,
        cost_usd: float = 0.0,
        fallback_from_provider: str | None = None,
    ) -> None:
        """记录节点级用量明细（v3: FK to runs + 可选 subagent_id）。"""

    @abstractmethod
    async def get_usage_summary(
        self,
        *,
        days: int = 30,
        provider_id: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按日聚合用量（供统计面板）。v3: 新增 run_id 过滤。"""

    @abstractmethod
    async def get_usage_breakdown(self, *, days: int = 30) -> dict[str, list[dict[str, Any]]]:
        """多维度用量穿透：按业务(workflow)/Agent/服务商/模型聚合（供监控中心明细弹窗）。"""

    @abstractmethod
    async def get_quota_status(self, quota_config: dict[str, Any]) -> list[dict[str, Any]]:
        """按 quota.yaml 配置查询各 provider 滑动窗口配额使用情况。"""

    # ----- Handoffs / Node executions / Workspaces / Artifacts / Skill contexts -----

    @abstractmethod
    async def record_handoff(self, run_id: str, from_node_id: str,
                              from_subagent_id: str, to_node_id: str,
                              port: str, payload: dict,
                              payload_size: int, summary: str | None = None) -> int:
        """记录 handoff 事件（v3: 新表 handoffs）。返回 id。"""

    @abstractmethod
    async def list_handoffs(self, run_id: str,
                             from_node_id: str | None = None,
                             to_node_id: str | None = None) -> list[dict[str, Any]]:
        """列出 run 的 handoffs。"""

    @abstractmethod
    async def update_handoff_status(self, handoff_id: int, status: str,
                                     failure_reason: str | None = None) -> None:
        """更新 handoff 状态（applied/failed/corrected）。"""

    @abstractmethod
    async def upsert_node_execution(
        self, run_id: str, node_id: str, node_type: str,
        lease_generation: int, status: str,
        subagent_id: str | None = None,
        resolved_provider: str | None = None,
        resolved_model: str | None = None,
    ) -> None:
        """写入或更新 node_executions 行。"""

    @abstractmethod
    async def create_workspace(self, run_id: str, workflow_id: str,
                                workspace_root: str, absolute_root: str,
                                mode: int = 448) -> None:
        """写入 per-run workspace 元数据（v2 P0.18.1: 表已重命名为 run_workspace_meta）。

        此方法保留向后兼容，新 caller 应优先用 record_run_workspace_meta。
        """

    @abstractmethod
    async def record_run_workspace_meta(
        self,
        run_id: str,
        workflow_id: str,
        workspace_root: str,
        absolute_root: str,
        mode: int = 448,
        authorized_workspace_id: str | None = None,
        cleanup_at: str | None = None,
    ) -> None:
        """v2 P0.18.1 新增：写入 per-run workspace 元数据（含 authorized_workspace_id 关联）。"""

    @abstractmethod
    async def mark_sandbox_for_cleanup(
        self,
        workspace_id: str,
        run_id: str,
        cleanup_at: str,
    ) -> None:
        """v2 P0.18.1 新增：标记 sandbox 延迟清理（patroller 每日扫 cleanup_at < now() 删除目录）。"""

    @abstractmethod
    async def list_sandboxes_for_cleanup(self, now_iso: str, limit: int = 100) -> list[dict[str, Any]]:
        """v2 P0.18.1 新增：列出待清理的 sandbox（patroller 调用）。"""

    @abstractmethod
    async def mark_sandbox_deleted(self, run_id: str) -> None:
        """v2 P0.18.1 新增：sandbox 物理删除后标记 cleanup_status='deleted'。"""

    # ----- Authorized Workspaces CRUD（v2 P0.18.1 新增） -----

    @abstractmethod
    async def create_authorized_workspace(
        self,
        workspace_id: str,
        display_name: str,
        mode: str,
        permissions: str,
        description: str | None = None,
        source_path: str | None = None,
        git_url: str | None = None,
        git_branch: str | None = None,
        extra: dict | None = None,
    ) -> dict[str, Any]:
        """新增用户授权工作区。返回新建记录。"""

    @abstractmethod
    async def get_authorized_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        """获取单个授权工作区详情。"""

    @abstractmethod
    async def list_authorized_workspaces(
        self,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        """列出所有授权工作区。include_disabled=True 含已取消授权的。"""

    @abstractmethod
    async def update_authorized_workspace(
        self,
        workspace_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        permissions: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        """更新授权工作区字段。enabled=False 时填 deauthorized_at。返回更新后记录。"""

    @abstractmethod
    async def delete_authorized_workspace(self, workspace_id: str) -> bool:
        """soft delete：enabled=0 + deauthorized_at=now。返回是否找到记录。"""

    @abstractmethod
    async def touch_authorized_workspace(self, workspace_id: str) -> None:
        """更新 last_used_at + usage_count += 1（run 启动时调用）。"""

    @abstractmethod
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        """读取系统设置（system_settings 表）。"""

    @abstractmethod
    async def set_setting(self, key: str, value: str) -> None:
        """写入/更新系统设置（upsert）。"""

    @abstractmethod
    async def record_run_artifact(self, run_id: str, name: str, artifact_id: str,
                                   file_path: str, file_size: int,
                                   file_digest: str, mime_type: str | None = None) -> None:
        """写入 run artifact（产物元数据）。"""

    @abstractmethod
    async def add_run_skill_context(self, run_id: str, skill_id: str,
                                     context_json: dict) -> None:
        """注入 run skill 上下文（append-only）。"""

    # ----- Parent / Child Runs -----

    @abstractmethod
    async def record_parent_child_run(self, parent_run_id: str, child_run_id: str,
                                       parent_session_id: str,
                                       child_session_id: str,
                                       created_via: str = "trigger_workflow") -> None:
        """记录 run 嵌套关系（v3: 替代 v2 record_parent_child）。"""

    @abstractmethod
    async def list_child_runs_of(self, parent_run_id: str) -> list[dict[str, Any]]:
        """列出某 parent run 派发的所有 child run 记录（parent_child_runs 表）。"""

    @abstractmethod
    async def list_parent_runs_of(self, child_run_id: str) -> list[dict[str, Any]]:
        """反向查询：某 run 由哪些父 run 派发。"""

    @abstractmethod
    async def list_child_runs_of_session(self, session_id: str) -> list[dict[str, Any]]:
        """JOIN parent_child_runs + runs，返回某 session 派发的所有 run。"""

    # ----- Agent 统计 -----

    @abstractmethod
    async def get_agent_stats(self, agent_id: str, workflow_ids: list[str] | None = None) -> dict[str, Any]:
        """聚合某 agent 的真实运行统计（v3: 从 runs 表聚合）。"""

    # ============================================================
    # Layer 3: Subagent 管理
    # ============================================================

    @abstractmethod
    async def provision_subagent(self, subagent_id: str, actor_id: str,
                                  run_id: str, node_id: str,
                                  harness_type: str,
                                  lease_generation: int = 1,
                                  runtime_placement: str = "in_process",
                                  harness_instance_id: str | None = None,
                                  thread_id: str | None = None) -> None:
        """创建 subagent 记录（v3: 必填 run_id/node_id/lease_generation + actor_id = run_id:node_id）。"""

    @abstractmethod
    async def update_subagent_status(self, subagent_id: str, status: str,
                                      error: str | None = None) -> None:
        """更新 subagent 状态。"""

    @abstractmethod
    async def terminate_subagent(self, subagent_id: str, cleanup_status: str = "released") -> None:
        """subagent terminate：写 finished_at + terminated_at + cleanup_status。"""

    @abstractmethod
    async def get_subagent(self, subagent_id: str) -> dict[str, Any] | None:
        """获取 subagent 记录。"""

    @abstractmethod
    async def list_subagents_for_run(self, run_id: str,
                                      lease_generation: int | None = None) -> list[dict[str, Any]]:
        """列出某 run 的所有 subagent（可按 lease_generation 过滤）。"""

    @abstractmethod
    async def get_active_subagent(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        """查询 (run_id, node_id) 当前 active 的 subagent（最多 1 条，由部分 UNIQUE 保证）。"""

    @abstractmethod
    async def list_active_subagents(self) -> list[dict[str, Any]]:
        """P0.17: 列出所有 status='active' 的 subagent JOIN subagent_provisioned_workers。

        返回字段包含 subagent_id / actor_id / run_id / node_id / harness_type / runtime_placement
        / worker_id / container_id / process_id / thread_id / started_at 等。
        """

    @abstractmethod
    async def update_worker_status(self, subagent_id: str, status: str) -> None:
        """更新 subagent_provisioned_workers.status（容器销毁时标记 released/failed）。"""

    @abstractmethod
    async def increment_lease_generation(self, run_id: str, node_id: str) -> int:
        """纠错重派：递增 (run_id, node_id) 的 lease_generation，返回新值。"""

    @abstractmethod
    async def record_subagent_checkpoint(self, subagent_id: str,
                                          checkpoint_version: int,
                                          checkpoint_json: dict) -> None:
        """追加 subagent checkpoint（v3: append-only，触发器阻断 UPDATE）。"""

    @abstractmethod
    async def record_provisioned_worker(self, subagent_id: str, lease_generation: int,
                                          worker_id: str, runtime_placement: str,
                                          container_id: str | None = None,
                                          process_id: int | None = None,
                                          thread_id: str | None = None,
                                          workspace_id: str | None = None,
                                          tier: str | None = None) -> None:
        """写入 subagent 物理容器映射（v2 P0.18.1: 新增 workspace_id + tier 字段）。"""

    # ============================================================
    # 通用：Lint / Quota / Cleanup
    # ============================================================

    @abstractmethod
    async def append_lint_issue(self, domain: str, type_: str, severity: str,
                                description: str, page_a: str | None = None,
                                page_b: str | None = None, auto_fixable: bool = False) -> str:
        """写入 lint issue。"""

    @abstractmethod
    async def list_lint_issues(self, domain: str | None = None, status: str | None = None,
                               type_: str | None = None, severity: str | None = None,
                               limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        """查询 lint issue 列表 + 总数。"""

    @abstractmethod
    async def get_lint_issue(self, issue_id: str) -> dict | None:
        """查询单个 lint issue。"""

    @abstractmethod
    async def update_lint_issue_status(self, issue_id: str, status: str,
                                       resolved_by: str = "user", resolution_note: str = "") -> bool:
        """更新 issue 状态。"""

    @abstractmethod
    async def get_lint_summary(self, domain: str) -> dict:
        """返回 {total, critical, warning, info, pending, resolved, ignored}。"""

    @abstractmethod
    async def close(self) -> None: ...


# ====== SQLite 实现 ======

class _CachedCursor:
    """线程安全的结果代理：在 ``_db_lock`` 内完成 fetch，消除 cursor 惰性 fetch 的竞态。

    背景（D-068）：``sqlite3`` 的 cursor 结果集是**连接级**的惰性 fetch。原
    ``_exec`` 只返回裸 cursor，``with self._db_lock`` 在 return 时即释放；随后
    ``await asyncio.to_thread(self._exec, ...)`` 恢复、真正调用 ``fetchone()`` 前，
    同一连接上的下一次 ``execute`` 会重置/污染旧 cursor，导致 ``fetchone()``
    偶发返回 None。高并发下（如监控中心同时挂载 4+ 轮询请求）会把一个**有效**
    session token 误判为 ``invalid_or_revoked_session``，前端收到 401 即弹登录页，
    形成「切页面 → 弹登录」的反馈循环；也会把有效用户误判为 ``user_disabled``（403）。

    修复：``_exec`` 在 lock 内就完成 fetch（读语句）或固化 ``rowcount``/``lastrowid``
    （写语句），返回本代理。调用点零改动（``fetchone``/``fetchall``/``rowcount``/
    ``lastrowid`` 语义兼容）。
    """

    __slots__ = ("_rows", "_idx", "rowcount", "lastrowid", "description")

    def __init__(self, rows=None, *, rowcount: int = 0,
                 lastrowid=None, description=None):
        self._rows = list(rows) if rows else []
        self._idx = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rows

    def close(self) -> None:
        self._rows = []


class SqliteEventStore(SecurityStoreMixin, EventStore):
    """v3 SQLite 实现：单文件 WAL 模式。

    ``SecurityStoreMixin`` 提供 security 系列表的访问方法（S4），
    定义在 ``audit/security_store.py``，只依赖本类的 ``_exec``。
    """

    def __init__(self, db_path: str = "audit.db", task_v1_enabled: bool = False):
        self.db_path = str(Path(db_path).resolve())
        self._write_lock = asyncio.Lock()
        self._db_lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")  # v3 FK 约束
        self._conn.executescript(_SCHEMA)
        # 任务管理模块 DDL（P0 始终执行；V1 由 task_v1_enabled 控制）
        self._conn.executescript(_TASK_SCHEMA_P0)
        if task_v1_enabled and _TASK_SCHEMA_V1:
            # P0→V1 迁移：仅当 tasks 仍是 P0 旧结构（无 approved 列，V1 独有）时才
            # DROP 重建（P0 的 5 态 CHECK 约束会阻塞 V1 转移，CHECK 无法 ALTER）。
            # 已是 V1 结构时必须跳过 DROP —— 否则每次重启都清空 tasks，
            # 且 FK ON DELETE CASCADE 会连带清空 task_activities/comments/runs。
            # V1 DDL 全部 CREATE IF NOT EXISTS，对已建表幂等 no-op，数据安全。
            # 注意：P0 表也有 risk_level 列，不能用 risk_level 判断版本。
            _task_cols = {row[1] for row in
                          self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if _task_cols and "approved" not in _task_cols:
                self._conn.executescript(
                    "DROP TABLE IF EXISTS task_stages;\n"
                    "DROP TABLE IF EXISTS tasks;\n")
            self._conn.executescript(_TASK_SCHEMA_V1)
            # tasks 表 V1 新增 5 字段：对已建表安全 ALTER（PRAGMA 检查列是否存在）
            existing_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            for alter_sql in _TASK_V1_ALTERS:
                col_name = alter_sql.split("ADD COLUMN ")[1].split()[0]
                if col_name not in existing_cols:
                    self._conn.execute(alter_sql)
            # V3：终端注册表 + 布局表 + task_comments.mentions 列（§4.12/§4.13）
            self._conn.executescript(_TASK_SCHEMA_V3)
            comment_cols = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(task_comments)").fetchall()}
            for alter_sql in _TASK_V3_ALTERS:
                col_name = alter_sql.split("ADD COLUMN ")[1].split()[0]
                if col_name not in comment_cols:
                    self._conn.execute(alter_sql)
        self._migrate_v2_to_v3()
        self._init_security_schema()
        self._dropped_events: int = 0

    # ====== security schema（安全认证访问 MVP · S1/S2/S3）======

    def _init_security_schema(self) -> None:
        """建 security 系列表 + 种子数据 + 首次启动的 admin 账号。

        拆分到独立模块 ``audit/security_schema.py``（5600+ 行的本文件不宜再塞 DDL）。
        设计见 ``docs/security-mvp-plan-2026-08-29.md``。

        容错策略：
          - DDL 迁移失败 → 直接抛（schema 不完整，后续所有 security 接口都会错）
          - bootstrap 失败（典型：缺 argon2-cffi） → 只记 error 不阻断，
            原因是不装这个依赖也不该让 AgentOps 整体起不来；
            schema 已建好，装完依赖重启即可自动补建 admin。
        """
        from audit.security_schema import bootstrap_first_user, migrate_security_schema

        migrate_security_schema(self._conn)
        try:
            bootstrap_first_user(self._conn)
        except Exception as exc:
            logger.error(
                "[security] bootstrap_first_user 失败，初始 admin 未创建。"
                "装完依赖后重启会自动补建。原因：%s", exc, exc_info=True,
            )

    # ====== v2 → v3 schema migration ======

    def _migrate_v2_to_v3(self) -> None:
        """v3 启动时迁移：v2 旧表（已不在 v3 schema 中）DROP，避免 CREATE TABLE IF NOT EXISTS 失败。

        v2 旧表（v93 合并版，2026-08-06 引入；2026-08-09 v98 反转 → v3 三层拆分）：
          - dag_events           → v3 改为 run_events（FK to runs）
          - parent_child_sessions → v3 改为 parent_child_runs（FK to runs）
          - v2 sessions 表含 workflow_id/run_mode/inputs/final_outputs → v3 收窄（数据丢失不可逆，
            按用户 v93「所有历史数据都可以删除」决定直接 DROP 表重建）

        旧 usage_records.session_id 列在 v3 改名为 run_id；旧 session_events 没 run_id 列。
        CREATE TABLE IF NOT EXISTS 对已存在但结构不同的表不会重建，CREATE INDEX 会因列不存在
        报「no such column: run_id」整库启动失败。所以这里 DROP 旧表让 v3 schema 重建。

        新部署（无 audit.db）→ _migrate_v2_to_v3 是 no-op（SQLite 没有旧表，DROP IF EXISTS 不报错）。
        """
        # v2 旧表：DROP IF EXISTS 避免新部署 no-op 也安全
        self._conn.executescript("""
            DROP TABLE IF EXISTS dag_events;
            DROP TABLE IF EXISTS parent_child_sessions;
        """)
        # v2 sessions 表字段收窄：旧表含 workflow_id/run_mode
        # 但 v3 sessions 表用相同列名 + 不同 CHECK 约束；CREATE TABLE IF NOT EXISTS 不重建
        # 直接 DROP 重建（数据已迁移无意义，按用户授权丢失）
        existing_sessions_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if existing_sessions_cols and "workflow_id" in existing_sessions_cols:
            # v2 sessions 表（含 workflow_id/run_mode）→ DROP 重建为 v3 收窄版
            self._conn.executescript("""
                DROP TABLE IF EXISTS sessions;
            """)
        # v2 usage_records：旧表含 session_id 列，v3 改为 run_id
        existing_usage_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(usage_records)").fetchall()
        }
        if existing_usage_cols and "run_id" not in existing_usage_cols:
            self._conn.executescript("""
                DROP TABLE IF EXISTS usage_records;
            """)
        # v2 session_events：旧表没 run_id 列，v3 新增
        existing_session_events_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(session_events)").fetchall()
        }
        if existing_session_events_cols and "run_id" not in existing_session_events_cols:
            self._conn.executescript("""
                DROP TABLE IF EXISTS session_events;
            """)
        # v2 widget_inputs：旧表用 session_id 作 FK 主键，v3 改用 run_id
        existing_widget_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(widget_inputs)").fetchall()
        }
        if existing_widget_cols and "run_id" not in existing_widget_cols:
            self._conn.executescript("""
                DROP TABLE IF EXISTS widget_inputs;
            """)
        # v2 raw_harness_events：旧表没 run_id + subagent_id 双 FK
        existing_raw_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(raw_harness_events)").fetchall()
        }
        if existing_raw_cols and "run_id" not in existing_raw_cols:
            self._conn.executescript("""
                DROP TABLE IF EXISTS raw_harness_events;
            """)
        # v2 P0.18.1 迁移：旧 workspaces 表 → run_workspace_meta
        # 现有 audit.db 中可能已有 workspaces 表（per-run 元数据），重命名保留数据
        existing_tables = {
            row[0] for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "workspaces" in existing_tables:
            # 旧 workspaces 表存在 → 强制重命名为 run_workspace_meta
            # （先 DROP 新表以避免 ALTER RENAME 冲突，因为 SCHEMA 已在迁移前执行）
            self._conn.executescript("""
                DROP TABLE IF EXISTS run_workspace_meta;
                ALTER TABLE workspaces RENAME TO run_workspace_meta;
            """)
        # 补 run_workspace_meta 新列（若旧表已重命名但缺新列）
        rwm_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(run_workspace_meta)").fetchall()
        }
        if rwm_cols and "cleanup_status" not in rwm_cols:
            self._conn.executescript(
                "ALTER TABLE run_workspace_meta ADD COLUMN cleanup_status TEXT NOT NULL DEFAULT 'active';"
            )
        if rwm_cols and "authorized_workspace_id" not in rwm_cols:
            self._conn.executescript(
                "ALTER TABLE run_workspace_meta ADD COLUMN authorized_workspace_id TEXT;"
            )
        # 补 sessions 表新列（workspace_id / workspace_locked）
        sessions_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if sessions_cols and "workspace_id" not in sessions_cols:
            self._conn.executescript(
                "ALTER TABLE sessions ADD COLUMN workspace_id TEXT;"
            )
        if sessions_cols and "workspace_locked" not in sessions_cols:
            self._conn.executescript(
                "ALTER TABLE sessions ADD COLUMN workspace_locked INTEGER NOT NULL DEFAULT 0;"
            )
        # 会话级权限级别（与 workspace 解耦）：read_only / read_write / read_write_exec / full_access
        # 创建会话时从 workspace.permissions 初始化，之后可独立切换
        if sessions_cols and "permission_level" not in sessions_cols:
            self._conn.executescript(
                "ALTER TABLE sessions ADD COLUMN permission_level TEXT;"
            )
        # 补 runs 表新列（workspace_root / workspace_mode / authorized_workspace_id）
        runs_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if runs_cols and "workspace_root" not in runs_cols:
            self._conn.executescript(
                "ALTER TABLE runs ADD COLUMN workspace_root TEXT NOT NULL DEFAULT '';"
            )
        if runs_cols and "workspace_mode" not in runs_cols:
            self._conn.executescript(
                "ALTER TABLE runs ADD COLUMN workspace_mode TEXT NOT NULL DEFAULT '';"
            )
        if runs_cols and "authorized_workspace_id" not in runs_cols:
            self._conn.executescript(
                "ALTER TABLE runs ADD COLUMN authorized_workspace_id TEXT;"
            )
        # 补 subagent_provisioned_workers 表新列（workspace_id / tier）
        spw_cols = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(subagent_provisioned_workers)"
            ).fetchall()
        }
        if spw_cols and "workspace_id" not in spw_cols:
            self._conn.executescript(
                "ALTER TABLE subagent_provisioned_workers ADD COLUMN workspace_id TEXT;"
            )
        if spw_cols and "tier" not in spw_cols:
            self._conn.executescript(
                "ALTER TABLE subagent_provisioned_workers ADD COLUMN tier TEXT;"
            )

    # ====== 内部辅助 ======

    def _exec(self, sql: str, params: tuple = ()) -> _CachedCursor:
        """执行 SQL 并返回线程安全的结果代理（见 ``_CachedCursor`` 说明）。

        - 读语句（``cur.description is not None``）：在 lock 内 ``fetchall()``，
          结果缓存进代理，杜绝连接级惰性 fetch 的并发污染。
        - 写语句：在 lock 内固化 ``rowcount`` / ``lastrowid``，调用方照常读取。
        """
        with self._db_lock:
            cur = self._conn.execute(sql, params)
            if cur.description is not None:
                rows = cur.fetchall()
                return _CachedCursor(rows, rowcount=len(rows), description=cur.description)
            return _CachedCursor(rowcount=cur.rowcount, lastrowid=cur.lastrowid)

    def _executemany(self, sql: str, params_list: list[tuple]) -> None:
        with self._db_lock:
            self._conn.executemany(sql, params_list)

    # ============================================================
    # Layer 1: Session 实现
    # ============================================================

    async def create_session(self, session_id: str, agent_id: str,
                             user_id: str = "", title: str = "",
                             workspace_id: str | None = None,
                             permission_level: str | None = None) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT OR IGNORE INTO sessions "
            "(session_id, user_id, agent_id, status, title, last_activity_at, created_at, updated_at, workspace_id, permission_level) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (session_id, user_id, agent_id, title or "", now, now, now, workspace_id, permission_level),
        )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._exec, "SELECT * FROM sessions WHERE session_id = ?", (session_id,),
        )
        r = rows.fetchone()
        return dict(r) if r else None

    async def touch_session(self, session_id: str) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE sessions SET last_activity_at = ?, updated_at = ? WHERE session_id = ?",
            (now, now, session_id),
        )

    async def update_session_status(self, session_id: str, status: str,
                                    last_activity: bool = True) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        if last_activity:
            await asyncio.to_thread(
                self._exec,
                "UPDATE sessions SET status=?, last_activity_at=?, updated_at=? WHERE session_id=?",
                (status, now, now, session_id),
            )
        else:
            await asyncio.to_thread(
                self._exec,
                "UPDATE sessions SET status=?, updated_at=? WHERE session_id=?",
                (status, now, session_id),
            )

    async def update_session_title(self, session_id: str, title: str) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE sessions SET title=?, updated_at=? WHERE session_id=?",
            (title, now, session_id),
        )

    async def update_session_permission_level(self, session_id: str, permission_level: str) -> None:
        """更新会话级权限级别（与 workspace 解耦，随时可切换、立即生效）。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE sessions SET permission_level=?, updated_at=? WHERE session_id=?",
            (permission_level, now, session_id),
        )

    async def archive_session(self, session_id: str) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE sessions SET status='archived', archived_at=?, updated_at=? WHERE session_id=?",
            (now, now, session_id),
        )

    async def delete_session(self, session_id: str) -> None:
        await asyncio.to_thread(
            self._exec, "DELETE FROM sessions WHERE session_id=?", (session_id,),
        )

    async def increment_attached_run_count(self, session_id: str) -> None:
        await asyncio.to_thread(
            self._exec,
            "UPDATE sessions SET attached_run_count = attached_run_count + 1, "
            "updated_at = ? WHERE session_id = ?",
            (_dt_to_str(datetime.now(timezone.utc)), session_id),
        )

    async def list_sessions(self, workflow_id: str | None = None,
                            status: str | None = None,
                            search: str | None = None,
                            limit: int = 50,
                            offset: int = 0) -> list[dict[str, Any]]:
        """v3: workflow_id 参数保留向后兼容，但 sessions 表已无此字段，会自动忽略。"""
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if search:
            sql += " AND (title LIKE ? OR session_id LIKE ?)"
            params.append(f"%{search}%")
            params.append(f"%{search}%")
        sql += " ORDER BY COALESCE(last_activity_at, updated_at) DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        rows = await asyncio.to_thread(self._exec, sql, tuple(params))
        return [dict(r) for r in rows.fetchall()]

    async def count_sessions(self, workflow_id: str | None = None,
                              status: str | None = None,
                              search: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM sessions WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if search:
            sql += " AND (title LIKE ? OR session_id LIKE ?)"
            params.append(f"%{search}%")
            params.append(f"%{search}%")
        row = await asyncio.to_thread(self._exec, sql, tuple(params))
        return row.fetchone()[0]

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self._exec, "SELECT * FROM sessions WHERE session_id=?", (session_id,),
        )
        r = row.fetchone()
        if not r:
            return {"error": "session not found", "session_id": session_id}
        result = dict(r)
        if result.get("metadata"):
            try:
                result["metadata"] = json.loads(result["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        ev_count = await asyncio.to_thread(
            self._exec, "SELECT COUNT(*) as c FROM session_events WHERE session_id=?", (session_id,),
        )
        result["event_count"] = ev_count.fetchone()["c"]
        return result

    async def update_session_thread(
        self, session_id: str, thread_id: str, thread_name: str, tool_digest: str,
    ) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        def _do():
            self._exec(
                "UPDATE sessions SET thread_id = ?, thread_name = ?, thread_tool_digest = ?, "
                "last_activity_at = ?, updated_at = ? WHERE session_id = ?",
                (thread_id, thread_name, tool_digest, now, now, session_id),
            )
        await asyncio.to_thread(_do)

    async def update_session_voice(self, session_id: str, voice_active: bool) -> None:
        def _do():
            self._exec(
                "UPDATE sessions SET voice_active = ? WHERE session_id = ?",
                (1 if voice_active else 0, session_id),
            )
        await asyncio.to_thread(_do)

    # ----- Session 消息 / 事件 -----

    async def append_session_message(
        self, session_id: str, role: str, content: str | dict | list,
        turn_id: str | None = None, message_type: str = "text",
        metadata: dict | None = None,
    ) -> int:
        now = _dt_to_str(datetime.now(timezone.utc))
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False, default=str)

        def _do():
            row = self._exec(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM session_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = row["next_seq"] if row else 1
            self._exec(
                "INSERT INTO session_messages(session_id, sequence, role, content, turn_id, message_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, seq, role, content, turn_id, message_type, meta_json, now),
            )
            self._exec(
                "UPDATE sessions SET message_count = message_count + 1, last_activity_at = ?, updated_at = ? "
                "WHERE session_id = ?",
                (now, now, session_id),
            )
            return seq
        return await asyncio.to_thread(_do)

    async def get_session_messages(self, session_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        def _do():
            rows = self._exec(
                "SELECT * FROM session_messages WHERE session_id = ? ORDER BY sequence LIMIT ?",
                (session_id, limit),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                raw = d.get("content")
                if isinstance(raw, str) and raw.startswith(("{", "[")):
                    try:
                        d["content"] = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        pass
                result.append(d)
            return result
        return await asyncio.to_thread(_do)

    async def append_session_event(
        self, session_id: str, event_type: str, payload: dict,
        node_id: str | None = None,
        run_id: str | None = None,
    ) -> int:
        now = _dt_to_str(datetime.now(timezone.utc))
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)

        def _do():
            row = self._exec(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM session_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = row["next_seq"] if row else 1
            self._exec(
                "INSERT INTO session_events(session_id, sequence, event_type, node_id, run_id, payload, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, seq, event_type, node_id, run_id, payload_json, now),
            )
            return seq
        return await asyncio.to_thread(_do)

    async def get_session_events(self, session_id: str, since: int = 0, limit: int = 10000) -> list[dict[str, Any]]:
        def _do():
            rows = self._exec(
                "SELECT * FROM session_events WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (session_id, since, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_do)

    # ----- Session 记忆 -----

    async def add_session_memory(self, session_id: str, memory_type: str,
                                  content: str, source_run_id: str | None = None,
                                  tokens: int = 0, importance: float = 0.5,
                                  expires_at: str | None = None) -> None:
        """v3 标准接口：source_run_id 写入 session_memory.source_run_id（FK to runs）。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO session_memory (session_id, memory_type, source_run_id, content, tokens, importance, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, memory_type, source_run_id) DO UPDATE SET "
            "content = excluded.content, tokens = excluded.tokens, importance = excluded.importance",
            (session_id, memory_type, source_run_id, content, tokens, importance, now, expires_at),
        )

    async def add_session_memory_v2(self, session_id: str, memory_type: str,
                                     content: str, source_session_id: str | None = None,
                                     tokens: int = 0, importance: float = 0.5,
                                     expires_at: str | None = None) -> None:
        """v2 兼容：source_session_id 实际写到 source_run_id（v3 字段重命名）。"""
        await self.add_session_memory(
            session_id=session_id,
            memory_type=memory_type,
            content=content,
            source_run_id=source_session_id,  # 同源映射
            tokens=tokens,
            importance=importance,
            expires_at=expires_at,
        )

    async def list_session_memory(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT id, session_id, memory_type, source_run_id, content, tokens, importance, created_at, expires_at "
            "FROM session_memory WHERE session_id = ? "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            (session_id, limit),
        )
        return [dict(r) for r in rows.fetchall()]

    # ============================================================
    # Layer 2: Run 实现
    # ============================================================

    async def init_run(self, run_id: str, session_id: str,
                        workflow_id: str | None = None,
                        run_mode: str = "conversational",
                        agent_id: str | None = None,
                        initial_message: str | None = None,
                        parent_run_id: str | None = None,
                        inputs: dict | None = None) -> None:
        """v3: session_id 必填；conversational/task 模式 agent_id + initial_message 必填。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT OR REPLACE INTO runs "
            "(run_id, session_id, parent_run_id, workflow_id, run_mode, agent_id, initial_message, "
            " status, inputs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (run_id, session_id, parent_run_id, workflow_id, run_mode, agent_id, initial_message,
             json.dumps(inputs, ensure_ascii=False) if inputs else None, now, now),
        )

    async def finalize_run(self, run_id: str, status: str,
                           finished_at: datetime | None = None,
                           total_tokens_in: int = 0,
                           total_tokens_out: int = 0,
                           total_cost_usd: float = 0.0,
                           error: str | None = None,
                           final_outputs: dict | None = None,
                           cancellation_reason: str | None = None) -> None:
        ts = _dt_to_str(finished_at or datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE runs SET status=?, finished_at=?, total_tokens_in=?, total_tokens_out=?, "
            "total_cost_usd=?, error=?, final_outputs=?, cancellation_reason=?, updated_at=? WHERE run_id=?",
            (status, ts, total_tokens_in, total_tokens_out, total_cost_usd, error,
             json.dumps(final_outputs, ensure_ascii=False, default=str) if final_outputs else None,
             cancellation_reason, ts, run_id),
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(self._exec, "SELECT * FROM runs WHERE run_id=?", (run_id,))
        r = rows.fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("inputs", "final_outputs", "metadata"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    async def list_runs(self, session_id: str | None = None,
                         workflow_id: str | None = None,
                         status: str | None = None,
                         limit: int = 50,
                         offset: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if workflow_id:
            sql += " AND workflow_id=?"
            params.append(workflow_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY COALESCE(started_at, created_at) DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        rows = await asyncio.to_thread(self._exec, sql, tuple(params))
        return [dict(r) for r in rows.fetchall()]

    async def count_runs(self, session_id: str | None = None,
                          workflow_id: str | None = None,
                          status: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM runs WHERE 1=1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if workflow_id:
            sql += " AND workflow_id=?"
            params.append(workflow_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        row = await asyncio.to_thread(self._exec, sql, tuple(params))
        return row.fetchone()[0]

    async def get_run_summary(self, run_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(self._exec, "SELECT * FROM runs WHERE run_id=?", (run_id,))
        r = row.fetchone()
        if not r:
            return {"error": "run not found", "run_id": run_id}
        result = dict(r)
        for k in ("inputs", "final_outputs", "metadata"):
            if result.get(k):
                try:
                    result[k] = json.loads(result[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        ev_count = await asyncio.to_thread(
            self._exec, "SELECT COUNT(*) as c FROM run_events WHERE run_id=?", (run_id,),
        )
        result["event_count"] = ev_count.fetchone()["c"]
        return result

    async def list_active_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT run_id, session_id, workflow_id, run_mode, agent_id, status, started_at, "
            "total_tokens_in, total_tokens_out "
            "FROM runs WHERE status IN ('pending', 'running', 'waiting') "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows.fetchall()]

    async def list_stale_runs(
        self,
        threshold_iso: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """P0.18.13：查未终止且 created_at < threshold_iso 的 run。

        不区分 run_mode：conversational/task 也覆盖。
        会话活跃状态由 sessions 表维护，runs.status 收敛不影响 session_manager 正常工作。
        """
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT run_id, session_id, workflow_id, run_mode, agent_id, status, "
            "created_at, started_at, finished_at "
            "FROM runs WHERE status IN ('pending', 'running', 'waiting') "
            "AND created_at < ? "
            "ORDER BY created_at ASC LIMIT ?",
            (threshold_iso, limit),
        )
        return [dict(r) for r in rows.fetchall()]

    async def update_run_status(
        self,
        run_id: str,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        error: str | None = None,
    ) -> None:
        """更新 run 状态。

        P0.18.13：支持回填 started_at/finished_at/error；
        关键修复：转 running 时若 started_at 为 NULL，自动回填当前 UTC 时间，
        避免前端 formatElapsed 算出 49 万小时（new Date(null).getTime()=0）。
        状态转移规则：
          - 任意 → running：若 started_at 为 NULL 则回填 now；外部可显式传 started_at 覆盖
          - 任意 → completed/failed/cancelled：finished_at 默认为 now；外部可显式传
        """
        now = _dt_to_str(datetime.now(timezone.utc))
        # 转 running 时自动回填 started_at（幂等：已存在不覆盖）
        effective_started = started_at
        if status == "running" and not started_at:
            row = await asyncio.to_thread(
                self._exec, "SELECT started_at FROM runs WHERE run_id=?", (run_id,),
            )
            existing = row.fetchone()
            if existing and not existing["started_at"]:
                effective_started = now
        # 转终止态时自动回填 finished_at
        effective_finished = finished_at
        if status in ("completed", "failed", "cancelled") and not finished_at:
            effective_finished = now

        await asyncio.to_thread(
            self._exec,
            "UPDATE runs SET status=?, updated_at=?, "
            "started_at=COALESCE(?, started_at), "
            "finished_at=COALESCE(?, finished_at), "
            "error=COALESCE(?, error) "
            "WHERE run_id=?",
            (status, now, effective_started, effective_finished, error, run_id),
        )

    # ----- Run 事件流 -----

    async def append_run_event(self, run_id: str, event_type: str, payload: dict,
                                node_id: str | None = None,
                                subagent_id: str | None = None) -> int:
        """v3: payload_digest 自动计算。返回 sequence。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        digest = _digest_json(payload)

        def _do():
            row = self._exec(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = row["next_seq"] if row else 1
            self._exec(
                "INSERT INTO run_events(run_id, sequence, event_type, node_id, subagent_id, "
                "payload, payload_digest, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, event_type, node_id, subagent_id, payload_json, digest, now),
            )
            return seq
        return await asyncio.to_thread(_do)

    async def get_run_events(self, run_id: str, since: int = 0,
                              limit: int = 10000) -> list[DagEvent]:
        """v3: 列名 run_id 直接对应 DagEvent.run_id 字段（无需映射）。"""
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM run_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (run_id, since, limit),
        )
        result = []
        for r in rows.fetchall():
            try:
                ev_type = DagEventType(r["event_type"])
            except ValueError:
                continue
            result.append(DagEvent(
                type=ev_type,
                run_id=r["run_id"],
                node_id=r["node_id"],
                payload=json.loads(r["payload"]),
                occurred_at=_str_to_dt(r["occurred_at"]),
                sequence=r["sequence"],
            ))
        return result

    async def get_node_detail(self, run_id: str, node_id: str) -> dict[str, Any]:
        events_rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM run_events WHERE run_id=? AND node_id=? ORDER BY sequence",
            (run_id, node_id),
        )
        events = [dict(r) for r in events_rows.fetchall()]
        raw_rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM raw_harness_events WHERE run_id=? AND node_id=? ORDER BY id",
            (run_id, node_id),
        )
        raw_events = [dict(r) for r in raw_rows.fetchall()]
        handoffs_rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM handoffs WHERE run_id=? AND (from_node_id=? OR to_node_id=?) ORDER BY id",
            (run_id, node_id, node_id),
        )
        handoffs_events = [dict(r) for r in handoffs_rows.fetchall()]
        hil_rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM widget_inputs WHERE run_id=? AND (node_id=? OR widget_id LIKE ?) ORDER BY id",
            (run_id, node_id, f"%{node_id}%"),
        )
        hil_events = [dict(r) for r in hil_rows.fetchall()]

        started = next((e for e in events if e["event_type"] == "node.started"), None)
        completed = next((e for e in events if e["event_type"] == "node.completed"), None)
        failed = next((e for e in events if e["event_type"] == "node.failed"), None)

        return {
            "run_id": run_id,
            "node_id": node_id,
            "events": events,
            "raw_events": raw_events,
            "handoffs": handoffs_events,
            "hil_events": hil_events,
            "started_at": started["occurred_at"] if started else None,
            "finished_at": (completed or failed)["occurred_at"] if (completed or failed) else None,
            "status": "failed" if failed else ("completed" if completed else "unknown"),
            "input_payload": json.loads(started["payload"]) if started else None,
            "output_payload": json.loads(completed["payload"]) if completed else None,
            "error": json.loads(failed["payload"]).get("error") if failed else None,
        }

    async def get_current_node(self, run_id: str) -> str | None:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT node_id FROM run_events WHERE run_id=? AND event_type LIKE 'node.%' "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        )
        r = rows.fetchone()
        return r["node_id"] if r else None

    # ----- Raw harness 事件 -----

    async def append_raw_event(self, run_id: str, subagent_id: str,
                                node_id: str | None,
                                harness: str, event_type: str,
                                raw_payload: dict[str, Any]) -> None:
        try:
            safe_payload = _redact_value(raw_payload)
            payload_json = json.dumps(safe_payload, ensure_ascii=False, default=str)
            digest = _digest_json(safe_payload)
            await asyncio.to_thread(
                self._exec,
                "INSERT INTO raw_harness_events (run_id, subagent_id, node_id, harness, event_type, "
                "raw_payload, payload_digest, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, subagent_id, node_id, harness, event_type, payload_json, digest,
                 _dt_to_str(datetime.now(timezone.utc))),
            )
        except Exception as e:
            logger.warning("append_raw_event 落库失败（不阻塞推送）: %s", e)
            self._dropped_events += 1

    # ----- Widget inputs -----

    async def append_widget_input(self, run_id: str, widget_id: str,
                                   payload: dict[str, Any],
                                   session_id: str,
                                   node_id: str | None = None,
                                   user_id: str = "") -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO widget_inputs (run_id, widget_id, node_id, input_payload, user_id, session_id, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, widget_id, node_id,
             json.dumps(payload, ensure_ascii=False, default=str),
             user_id, session_id, _dt_to_str(datetime.now(timezone.utc))),
        )

    # ----- Usage records -----

    async def record_usage(
        self,
        run_id: str,
        node_id: str,
        provider_id: str,
        model: str,
        *,
        subagent_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        duration_ms: int = 0,
        cost_usd: float = 0.0,
        fallback_from_provider: str | None = None,
    ) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO usage_records
               (run_id, node_id, subagent_id, provider_id, model, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, duration_ms, cost_usd,
                fallback_from_provider, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, node_id, subagent_id, provider_id, model,
             input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
             duration_ms, cost_usd, fallback_from_provider, now),
        )

    async def get_usage_summary(
        self,
        *,
        days: int = 30,
        provider_id: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT date(created_at) AS day,
                   provider_id,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_usd) AS cost_usd,
                   COUNT(*) AS node_count
            FROM usage_records
            WHERE created_at >= datetime('now', ?)
        """
        params: list[Any] = [f"-{days} days"]
        if provider_id:
            sql += " AND provider_id = ?"
            params.append(provider_id)
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " GROUP BY day, provider_id ORDER BY day DESC"
        rows = await asyncio.to_thread(self._exec, sql, tuple(params))
        return [dict(r) for r in rows.fetchall()]

    async def get_usage_breakdown(self, *, days: int = 30) -> dict[str, list[dict[str, Any]]]:
        """多维度用量穿透：JOIN runs 按业务/Agent/服务商/模型聚合。

        - by_workflow: runs.workflow_id（NULL 归为"会话运行"，即 conversational/task 模式）
        - by_agent: runs.agent_id（NULL 归为"工作流运行"）
        - by_provider / by_model: usage_records 自带维度
        """
        since = f"-{days} days"
        select_tmpl = """
            SELECT {dims}
                   SUM(u.input_tokens + u.output_tokens) AS tokens,
                   SUM(u.input_tokens) AS input_tokens,
                   SUM(u.output_tokens) AS output_tokens,
                   SUM(u.cache_read_tokens + u.cache_creation_tokens) AS cache_tokens,
                   SUM(u.cost_usd) AS cost_usd,
                   COUNT(DISTINCT u.run_id) AS run_count
            FROM usage_records u{join}
            WHERE u.created_at >= datetime('now', ?)
            GROUP BY {group} ORDER BY tokens DESC
        """
        queries = {
            "by_workflow": select_tmpl.format(
                dims="COALESCE(r.workflow_id, '会话运行') AS dim,",
                join=" JOIN runs r ON u.run_id = r.run_id",
                group="dim",
            ),
            "by_agent": select_tmpl.format(
                dims="COALESCE(r.agent_id, '工作流运行') AS dim,",
                join=" JOIN runs r ON u.run_id = r.run_id",
                group="dim",
            ),
            "by_provider": select_tmpl.format(
                dims="u.provider_id AS dim,", join="", group="dim",
            ),
            "by_model": select_tmpl.format(
                dims="u.provider_id AS provider_id, u.model AS dim,",
                join="", group="u.provider_id, u.model",
            ),
        }

        result: dict[str, list[dict[str, Any]]] = {}
        for key, sql in queries.items():
            rows = await asyncio.to_thread(self._exec, sql, (since,))
            result[key] = [dict(r) for r in rows.fetchall()]
        return result

    async def get_quota_status(self, quota_config: dict[str, Any]) -> list[dict[str, Any]]:
        providers = quota_config.get("providers") or {}
        thresholds = quota_config.get("alert_thresholds") or {}
        yellow_pct = int(thresholds.get("yellow", 80))
        red_pct = int(thresholds.get("red", 95))

        display_names = {
            "minimax": "MiniMax", "openai": "OpenAI",
            "anthropic": "Anthropic", "deepseek": "DeepSeek",
        }

        result: list[dict[str, Any]] = []
        for pid, pcfg in providers.items():
            window_hours = int(pcfg.get("window_hours", 1))
            total_tokens = int(pcfg.get("total_tokens", 0))
            models = pcfg.get("models") or []

            rows = await asyncio.to_thread(
                self._exec,
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS used, "
                "MIN(created_at) AS earliest, MAX(created_at) AS latest, COUNT(*) AS cnt "
                "FROM usage_records WHERE provider_id=? AND created_at >= datetime('now', ?)",
                (pid, f"-{window_hours} hours"),
            )
            row = rows.fetchone()
            used = int(row["used"] or 0)
            earliest_str = row["earliest"]
            latest_str = row["latest"]

            earliest_dt = _str_to_dt(earliest_str) if earliest_str else None
            reset_dt: datetime | None = None
            reset_in_seconds = 0
            if earliest_dt is not None:
                from datetime import timedelta
                reset_dt = earliest_dt + timedelta(hours=window_hours)
                now = datetime.now(timezone.utc)
                if earliest_dt.tzinfo is None:
                    earliest_dt = earliest_dt.replace(tzinfo=timezone.utc)
                reset_in_seconds = max(0, int((reset_dt - now).total_seconds()))

            percentage = (used / total_tokens * 100) if total_tokens > 0 else 0.0
            if percentage >= red_pct:
                alert_level = "red"
            elif percentage >= yellow_pct:
                alert_level = "yellow"
            else:
                alert_level = "normal"

            result.append({
                "provider_id": pid,
                "display_name": display_names.get(pid, pid),
                "window_hours": window_hours,
                "total_tokens": total_tokens,
                "used_tokens": used,
                "percentage": round(percentage, 2),
                "earliest_record_at": earliest_dt.isoformat() if earliest_dt else None,
                "reset_at": reset_dt.isoformat() if reset_dt else None,
                "reset_in_seconds": reset_in_seconds,
                "models": models,
                "alert_level": alert_level,
                "description": pcfg.get("description", ""),
            })
        return result

    # ----- Handoffs / Node executions / Workspaces / Artifacts -----

    async def record_handoff(self, run_id: str, from_node_id: str,
                              from_subagent_id: str, to_node_id: str,
                              port: str, payload: dict,
                              payload_size: int, summary: str | None = None) -> int:
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        digest = _digest_json(payload)
        now = _dt_to_str(datetime.now(timezone.utc))
        def _do():
            cur = self._exec(
                "INSERT INTO handoffs(run_id, from_node_id, from_subagent_id, to_node_id, port, "
                "payload, payload_digest, payload_size, summary, status, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (run_id, from_node_id, from_subagent_id, to_node_id, port,
                 payload_json, digest, payload_size, summary, now),
            )
            return cur.lastrowid
        return await asyncio.to_thread(_do)

    async def list_handoffs(self, run_id: str,
                             from_node_id: str | None = None,
                             to_node_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM handoffs WHERE run_id=?"
        params: list[Any] = [run_id]
        if from_node_id:
            sql += " AND from_node_id=?"
            params.append(from_node_id)
        if to_node_id:
            sql += " AND to_node_id=?"
            params.append(to_node_id)
        sql += " ORDER BY id"
        rows = await asyncio.to_thread(self._exec, sql, tuple(params))
        return [dict(r) for r in rows.fetchall()]

    async def update_handoff_status(self, handoff_id: int, status: str,
                                     failure_reason: str | None = None) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        applied_at = now if status == "applied" else None
        await asyncio.to_thread(
            self._exec,
            "UPDATE handoffs SET status=?, applied_at=?, failure_reason=? WHERE id=?",
            (status, applied_at, failure_reason, handoff_id),
        )

    async def upsert_node_execution(
        self, run_id: str, node_id: str, node_type: str,
        lease_generation: int, status: str,
        subagent_id: str | None = None,
        resolved_provider: str | None = None,
        resolved_model: str | None = None,
    ) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO node_executions(run_id, node_id, node_type, lease_generation, subagent_id, "
            "status, resolved_provider, resolved_model, started_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}') "
            "ON CONFLICT(run_id, node_id, lease_generation) DO UPDATE SET "
            "status=excluded.status, resolved_provider=excluded.resolved_provider, "
            "resolved_model=excluded.resolved_model",
            (run_id, node_id, node_type, lease_generation, subagent_id,
             status, resolved_provider, resolved_model, now),
        )

    async def create_workspace(self, run_id: str, workflow_id: str,
                                workspace_root: str, absolute_root: str,
                                mode: int = 448) -> None:
        """v2 P0.18.1: 兼容旧 caller，写入 run_workspace_meta 表（不含 authorized_workspace_id）。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT OR REPLACE INTO run_workspace_meta"
            "(run_id, workflow_id, workspace_root, absolute_root, mode, cleanup_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (run_id, workflow_id, workspace_root, absolute_root, mode, now),
        )

    async def record_run_workspace_meta(
        self,
        run_id: str,
        workflow_id: str,
        workspace_root: str,
        absolute_root: str,
        mode: int = 448,
        authorized_workspace_id: str | None = None,
        cleanup_at: str | None = None,
    ) -> None:
        """v2 P0.18.1: 写入 per-run workspace 元数据（含 authorized_workspace_id 关联 + cleanup_at）。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        cleanup_status = "scheduled" if cleanup_at else "active"
        await asyncio.to_thread(
            self._exec,
            "INSERT OR REPLACE INTO run_workspace_meta"
            "(run_id, workflow_id, workspace_root, absolute_root, mode, cleanup_at, cleanup_status, "
            "authorized_workspace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, workflow_id, workspace_root, absolute_root, mode,
             cleanup_at, cleanup_status, authorized_workspace_id, now),
        )

    async def mark_sandbox_for_cleanup(
        self,
        workspace_id: str,
        run_id: str,
        cleanup_at: str,
    ) -> None:
        """v2 P0.18.1: 标记 sandbox 延迟清理。"""
        await asyncio.to_thread(
            self._exec,
            "UPDATE run_workspace_meta SET cleanup_at = ?, cleanup_status = 'scheduled' "
            "WHERE run_id = ?",
            (cleanup_at, run_id),
        )

    async def list_sandboxes_for_cleanup(self, now_iso: str, limit: int = 100) -> list[dict[str, Any]]:
        """v2 P0.18.1: 列出待清理的 sandbox（patroller 调用）。"""
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT run_id, workflow_id, workspace_root, absolute_root, authorized_workspace_id, cleanup_at "
            "FROM run_workspace_meta "
            "WHERE cleanup_status = 'scheduled' AND cleanup_at IS NOT NULL AND cleanup_at <= ? "
            "ORDER BY cleanup_at ASC LIMIT ?",
            (now_iso, limit),
        )
        return [dict(r) for r in rows.fetchall()]

    async def mark_sandbox_deleted(self, run_id: str) -> None:
        """v2 P0.18.1: sandbox 物理删除后标记 cleanup_status='deleted'。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE run_workspace_meta SET cleanup_status = 'deleted', size_bytes = 0 "
            "WHERE run_id = ?",
            (run_id,),
        )

    # ============================================================
    # Authorized Workspaces CRUD 实现（v2 P0.18.1 新增）
    # ============================================================

    async def create_authorized_workspace(
        self,
        workspace_id: str,
        display_name: str,
        mode: str,
        permissions: str,
        description: str | None = None,
        source_path: str | None = None,
        git_url: str | None = None,
        git_branch: str | None = None,
        extra: dict | None = None,
    ) -> dict[str, Any]:
        now = _dt_to_str(datetime.now(timezone.utc))
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO authorized_workspaces"
            "(workspace_id, display_name, description, mode, source_path, git_url, git_branch, "
            "permissions, authorized_at, usage_count, enabled, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)",
            (workspace_id, display_name, description, mode, source_path,
             git_url, git_branch, permissions, now, extra_json),
        )
        return await self.get_authorized_workspace(workspace_id) or {}

    async def get_authorized_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM authorized_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        r = rows.fetchone()
        return dict(r) if r else None

    async def list_authorized_workspaces(
        self,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        if include_disabled:
            sql = "SELECT * FROM authorized_workspaces ORDER BY last_used_at DESC NULLS LAST, display_name ASC"
            rows = await asyncio.to_thread(self._exec, sql)
        else:
            sql = ("SELECT * FROM authorized_workspaces WHERE enabled = 1 "
                   "ORDER BY last_used_at DESC NULLS LAST, display_name ASC")
            rows = await asyncio.to_thread(self._exec, sql)
        return [dict(r) for r in rows.fetchall()]

    async def update_authorized_workspace(
        self,
        workspace_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        permissions: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        """更新授权工作区字段。enabled=False 时填 deauthorized_at；enabled=True 时清空。"""
        sets: list[str] = []
        params: list[Any] = []
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if permissions is not None:
            sets.append("permissions = ?")
            params.append(permissions)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
            now = _dt_to_str(datetime.now(timezone.utc))
            sets.append("deauthorized_at = ?")
            params.append(None if enabled else now)
        if not sets:
            return await self.get_authorized_workspace(workspace_id)
        params.append(workspace_id)
        await asyncio.to_thread(
            self._exec,
            f"UPDATE authorized_workspaces SET {', '.join(sets)} WHERE workspace_id = ?",
            tuple(params),
        )
        return await self.get_authorized_workspace(workspace_id)

    async def delete_authorized_workspace(self, workspace_id: str) -> bool:
        """soft delete：enabled=0 + deauthorized_at=now。返回是否找到记录。"""
        existing = await self.get_authorized_workspace(workspace_id)
        if not existing:
            return False
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE authorized_workspaces SET enabled = 0, deauthorized_at = ? WHERE workspace_id = ?",
            (now, workspace_id),
        )
        return True

    async def touch_authorized_workspace(self, workspace_id: str) -> None:
        """更新 last_used_at + usage_count += 1（run 启动时调用）。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE authorized_workspaces SET last_used_at = ?, usage_count = usage_count + 1 "
            "WHERE workspace_id = ?",
            (now, workspace_id),
        )

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        """读取系统设置（system_settings 表）。"""
        cursor = await asyncio.to_thread(
            self._exec,
            "SELECT value FROM system_settings WHERE key = ?",
            (key,),
        )
        row = cursor.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        """写入/更新系统设置（upsert）。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )

    async def record_run_artifact(self, run_id: str, name: str, artifact_id: str,
                                   file_path: str, file_size: int,
                                   file_digest: str, mime_type: str | None = None) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT OR REPLACE INTO run_artifacts(run_id, name, artifact_id, file_path, "
            "file_size, file_digest, mime_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, name, artifact_id, file_path, file_size, file_digest, mime_type, now),
        )

    async def add_run_skill_context(self, run_id: str, skill_id: str,
                                     context_json: dict) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        digest = _digest_json(context_json)
        ctx_json = json.dumps(context_json, ensure_ascii=False, default=str)
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO run_skill_contexts(run_id, skill_id, context_digest, context_json, injected_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, skill_id, digest, ctx_json, now),
        )

    # ----- Parent / Child Runs -----

    async def record_parent_child_run(self, parent_run_id: str, child_run_id: str,
                                       parent_session_id: str,
                                       child_session_id: str,
                                       created_via: str = "trigger_workflow") -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO parent_child_runs (parent_run_id, child_run_id, parent_session_id, "
            "child_session_id, created_via, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(parent_run_id, child_run_id) DO UPDATE SET "
            "created_via=excluded.created_via, created_at=excluded.created_at",
            (parent_run_id, child_run_id, parent_session_id, child_session_id, created_via, now),
        )

    async def list_child_runs_of(self, parent_run_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT id, parent_run_id, child_run_id, parent_session_id, child_session_id, "
            "created_via, created_at FROM parent_child_runs WHERE parent_run_id=? "
            "ORDER BY created_at DESC, id DESC",
            (parent_run_id,),
        )
        return [dict(r) for r in rows.fetchall()]

    async def list_parent_runs_of(self, child_run_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT id, parent_run_id, child_run_id, parent_session_id, child_session_id, "
            "created_via, created_at FROM parent_child_runs WHERE child_run_id=? "
            "ORDER BY created_at DESC",
            (child_run_id,),
        )
        return [dict(r) for r in rows.fetchall()]

    async def list_child_runs_of_session(self, session_id: str) -> list[dict[str, Any]]:
        """返回某 session 关联的所有 run（替代 v2 list_child_sessions）。

        双路来源：
          1. runs.session_id + run_mode IN (templated/hybrid)——覆盖从工作台/
             工作流页直接发起的 run（这类 run 不写 parent_child_runs；
             conversational 父 run 会被排除，不算 session 的"子 run"）
          2. parent_child_runs JOIN ——覆盖 conversational/task 模式派生的子 run
        UNION 去重后按 started_at 倒序。
        """
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT r.* FROM runs r WHERE r.session_id = ? "
            "AND r.run_mode IN ('templated', 'hybrid') "
            "UNION "
            "SELECT r.* FROM runs r "
            "INNER JOIN parent_child_runs pcr ON r.run_id = pcr.child_run_id "
            "WHERE pcr.parent_session_id = ? "
            "ORDER BY started_at DESC, run_id DESC",
            (session_id, session_id),
        )
        return [dict(r) for r in rows.fetchall()]

    # ----- Agent stats -----

    async def get_agent_stats(self, agent_id: str, workflow_ids: list[str] | None = None) -> dict[str, Any]:
        """聚合某 agent 的真实运行统计（v3: 从 runs 表按 agent_id 或 workflow_id 聚合）。"""
        conditions = []
        params: list = []
        if workflow_ids:
            placeholders = ",".join("?" for _ in workflow_ids)
            conditions.append(f"workflow_id IN ({placeholders})")
            params.extend(workflow_ids)
        else:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        where_clause = " OR ".join(conditions)

        rows = await asyncio.to_thread(
            self._exec,
            f"SELECT status, COUNT(*) AS c FROM runs WHERE {where_clause} GROUP BY status",
            tuple(params),
        )
        status_counts = {r["status"]: r["c"] for r in rows.fetchall()}
        total = sum(status_counts.values())

        last_row = await asyncio.to_thread(
            self._exec,
            f"SELECT started_at, finished_at, status, total_tokens_in, total_tokens_out, "
            f"total_cost_usd FROM runs WHERE {where_clause} "
            "ORDER BY COALESCE(started_at, created_at) DESC LIMIT 1",
            tuple(params),
        )
        last = last_row.fetchone()

        tokens_in = await asyncio.to_thread(
            self._exec,
            f"SELECT COALESCE(SUM(total_tokens_in), 0) AS s FROM runs WHERE {where_clause}",
            tuple(params),
        )
        tokens_out = await asyncio.to_thread(
            self._exec,
            f"SELECT COALESCE(SUM(total_tokens_out), 0) AS s FROM runs WHERE {where_clause}",
            tuple(params),
        )
        cost = await asyncio.to_thread(
            self._exec,
            f"SELECT COALESCE(SUM(total_cost_usd), 0.0) AS s FROM runs WHERE {where_clause}",
            tuple(params),
        )

        return {
            "total_runs": total,
            "status_counts": status_counts,
            "running": status_counts.get("running", 0),
            "completed": status_counts.get("completed", 0),
            "failed": status_counts.get("failed", 0),
            "last_run_at": last["started_at"] if last else None,
            "last_run_status": last["status"] if last else None,
            "last_run_finished_at": last["finished_at"] if last else None,
            "total_tokens_input": tokens_in.fetchone()["s"],
            "total_tokens_output": tokens_out.fetchone()["s"],
            "total_cost_usd": cost.fetchone()["s"],
        }

    # ============================================================
    # Layer 3: Subagent 实现
    # ============================================================

    async def provision_subagent(self, subagent_id: str, actor_id: str,
                                  run_id: str, node_id: str,
                                  harness_type: str,
                                  lease_generation: int = 1,
                                  runtime_placement: str = "in_process",
                                  harness_instance_id: str | None = None,
                                  thread_id: str | None = None) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO subagents(subagent_id, actor_id, run_id, node_id, lease_generation, "
            "harness_type, harness_instance_id, status, runtime_placement, thread_id, "
            "started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?, ?)",
            (subagent_id, actor_id, run_id, node_id, lease_generation,
             harness_type, harness_instance_id, runtime_placement, thread_id, now, now, now),
        )

    async def update_subagent_status(self, subagent_id: str, status: str,
                                      error: str | None = None) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        if status in ("completed", "failed"):
            await asyncio.to_thread(
                self._exec,
                "UPDATE subagents SET status=?, finished_at=?, error=?, updated_at=? WHERE subagent_id=?",
                (status, now, error, now, subagent_id),
            )
        else:
            await asyncio.to_thread(
                self._exec,
                "UPDATE subagents SET status=?, error=?, updated_at=? WHERE subagent_id=?",
                (status, error, now, subagent_id),
            )

    async def terminate_subagent(self, subagent_id: str, cleanup_status: str = "released") -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "UPDATE subagents SET terminated_at=?, cleanup_status=?, status='completed', updated_at=? "
            "WHERE subagent_id=?",
            (now, cleanup_status, now, subagent_id),
        )

    async def get_subagent(self, subagent_id: str) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(self._exec, "SELECT * FROM subagents WHERE subagent_id=?", (subagent_id,))
        r = rows.fetchone()
        return dict(r) if r else None

    async def list_subagents_for_run(self, run_id: str,
                                      lease_generation: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM subagents WHERE run_id=?"
        params: list[Any] = [run_id]
        if lease_generation is not None:
            sql += " AND lease_generation=?"
            params.append(lease_generation)
        sql += " ORDER BY created_at, subagent_id"
        rows = await asyncio.to_thread(self._exec, sql, tuple(params))
        return [dict(r) for r in rows.fetchall()]

    async def get_active_subagent(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        """v3: 部分 UNIQUE 索引保证 (run_id, node_id) 同时只有 1 条 active subagent。"""
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM subagents WHERE run_id=? AND node_id=? "
            "AND status IN ('provisioning', 'running', 'handoff') LIMIT 1",
            (run_id, node_id),
        )
        r = rows.fetchone()
        return dict(r) if r else None

    async def list_active_subagents(self) -> list[dict[str, Any]]:
        """P0.17: 列出所有 active 物理 worker（subagent_provisioned_workers.status='active'），JOIN subagents。

        用于 Runtime Environment 面板展示「正在运行的 Worker」。

        重要：三重过滤：
          1. `w.status='active'`：worker 行未被 `update_worker_status` 改为 released
          2. `s.status IN ('provisioning','running','handoff','cleanup')`：subagent 真实生命周期活跃态
          3. `s.started_at > datetime('now','-1 hour')`：1 小时内启动的（DAG 节点超时通常分钟级）

        原因：`subagent_provisioned_workers.status` 只在 `container_provisioner.cleanup_worker()`
        一条路径被改为 `released`；in_process/subprocess worker 不走容器 provisioner，
        DAG 异常中断 / 进程崩溃 / 用户提前关闭 / patroller 收敛 runs 但漏掉 subagents 等场景下
        worker 行不会被更新，导致 audit.db 堆积大量「孤儿 active worker」记录。
        `subagents.status` 是 subagent 真实生命周期信号，更可靠；但仍可能卡 running（如宿主进程
        崩溃后没正常 finalize），所以再加 1 小时时间兜底，覆盖最坏情况。
        """
        sql = """
            SELECT
                s.subagent_id,
                s.actor_id,
                s.run_id,
                s.node_id,
                s.harness_type,
                s.harness_instance_id,
                s.thread_id,
                s.runtime_placement,
                s.status           AS subagent_status,
                s.started_at       AS subagent_started_at,
                w.worker_id,
                w.container_id,
                w.process_id,
                w.lease_generation,
                w.runtime_placement AS worker_runtime_placement,
                w.workspace_id     AS worker_workspace_id,
                w.tier             AS worker_tier,
                w.status            AS worker_status,
                w.started_at        AS worker_started_at
            FROM subagent_provisioned_workers w
            JOIN subagents s ON s.subagent_id = w.subagent_id AND s.lease_generation = w.lease_generation
            WHERE w.status = 'active'
              AND s.status IN ('provisioning', 'running', 'handoff', 'cleanup')
              AND s.started_at > datetime('now', '-1 hour')
            ORDER BY w.started_at DESC
        """
        rows = await asyncio.to_thread(self._exec, sql, ())
        return [dict(r) for r in rows.fetchall()]

    async def increment_lease_generation(self, run_id: str, node_id: str) -> int:
        """纠错重派：返回新 lease_generation（= MAX + 1）。"""
        def _do():
            row = self._exec(
                "SELECT COALESCE(MAX(lease_generation), 0) AS max_lease FROM subagents WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            return (row["max_lease"] or 0) + 1
        return await asyncio.to_thread(_do)

    async def record_subagent_checkpoint(self, subagent_id: str,
                                          checkpoint_version: int,
                                          checkpoint_json: dict) -> None:
        now = _dt_to_str(datetime.now(timezone.utc))
        ck_json = json.dumps(checkpoint_json, ensure_ascii=False, default=str)
        digest = _digest_json(checkpoint_json)
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO subagent_checkpoints(subagent_id, checkpoint_version, checkpoint_json, "
            "checkpoint_sha256, created_at) VALUES (?, ?, ?, ?, ?)",
            (subagent_id, checkpoint_version, ck_json, digest, now),
        )

    async def record_provisioned_worker(self, subagent_id: str, lease_generation: int,
                                          worker_id: str, runtime_placement: str,
                                          container_id: str | None = None,
                                          process_id: int | None = None,
                                          thread_id: str | None = None,
                                          workspace_id: str | None = None,
                                          tier: str | None = None) -> None:
        """v2 P0.18.1: 扩展 workspace_id + tier 字段。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO subagent_provisioned_workers(subagent_id, lease_generation, worker_id, "
            "runtime_placement, container_id, process_id, thread_id, workspace_id, tier, "
            "status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (subagent_id, lease_generation, worker_id, runtime_placement,
             container_id, process_id, thread_id, workspace_id, tier, now),
        )

    async def update_worker_status(self, subagent_id: str, status: str) -> None:
        """更新 subagent_provisioned_workers.status（容器销毁时标记 released/failed）。"""
        now = _dt_to_str(datetime.now(timezone.utc))
        released_at = now if status in ("released", "failed") else None
        await asyncio.to_thread(
            self._exec,
            "UPDATE subagent_provisioned_workers SET status=?, released_at=COALESCE(released_at, ?) WHERE subagent_id=?",
            (status, released_at, subagent_id),
        )

    # ============================================================
    # Lint / Quota / Cleanup（基本不变）
    # ============================================================

    async def append_lint_issue(self, domain: str, type_: str, severity: str,
                                description: str, page_a: str | None = None,
                                page_b: str | None = None, auto_fixable: bool = False) -> str:
        issue_id = str(uuid.uuid4())
        now = _dt_to_str(datetime.now(timezone.utc))
        auto_fixable_val = 1 if auto_fixable else 0
        try:
            await asyncio.to_thread(
                self._exec,
                """
                INSERT INTO lint_issues (id, domain, type, severity, page_a, page_b,
                                         description, auto_fixable, detected_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (issue_id, domain, type_, severity, page_a, page_b,
                 description, auto_fixable_val, now),
            )
            return issue_id
        except sqlite3.IntegrityError:
            await asyncio.to_thread(
                self._exec,
                """
                UPDATE lint_issues
                SET detected_at = ?, status = 'pending', severity = ?,
                    description = ?, auto_fixable = ?,
                    resolved_at = NULL, resolved_by = NULL, resolution_note = NULL
                WHERE domain = ? AND type = ?
                  AND COALESCE(page_a, '') = COALESCE(?, '')
                  AND COALESCE(page_b, '') = COALESCE(?, '')
                """,
                (now, severity, description, auto_fixable_val,
                 domain, type_, page_a, page_b),
            )
            row = await asyncio.to_thread(
                self._exec,
                """
                SELECT id FROM lint_issues
                WHERE domain = ? AND type = ?
                  AND COALESCE(page_a, '') = COALESCE(?, '')
                  AND COALESCE(page_b, '') = COALESCE(?, '')
                """,
                (domain, type_, page_a, page_b),
            )
            r = row.fetchone()
            return r["id"] if r else issue_id

    async def list_lint_issues(self, domain: str | None = None, status: str | None = None,
                               type_: str | None = None, severity: str | None = None,
                               limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        where_parts: list[str] = []
        params: list[Any] = []
        if domain:
            where_parts.append("domain = ?")
            params.append(domain)
        if status:
            where_parts.append("status = ?")
            params.append(status)
        if type_:
            where_parts.append("type = ?")
            params.append(type_)
        if severity:
            where_parts.append("severity = ?")
            params.append(severity)
        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        total_row = await asyncio.to_thread(
            self._exec,
            f"SELECT COUNT(*) AS c FROM lint_issues{where_clause}",
            tuple(params),
        )
        total = total_row.fetchone()["c"]

        list_rows = await asyncio.to_thread(
            self._exec,
            f"SELECT * FROM lint_issues{where_clause} ORDER BY detected_at DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        )
        issues = []
        for r in list_rows.fetchall():
            d = dict(r)
            d["auto_fixable"] = bool(d.get("auto_fixable", 0))
            issues.append(d)
        return issues, total

    async def get_lint_issue(self, issue_id: str) -> dict | None:
        rows = await asyncio.to_thread(
            self._exec, "SELECT * FROM lint_issues WHERE id = ?", (issue_id,),
        )
        r = rows.fetchone()
        if not r:
            return None
        d = dict(r)
        d["auto_fixable"] = bool(d.get("auto_fixable", 0))
        return d

    async def update_lint_issue_status(self, issue_id: str, status: str,
                                       resolved_by: str = "user", resolution_note: str = "") -> bool:
        now = _dt_to_str(datetime.now(timezone.utc))
        if status in ("resolved", "ignored"):
            resolved_at = now
        else:
            resolved_at = None
        cur = await asyncio.to_thread(
            self._exec,
            """
            UPDATE lint_issues
            SET status = ?, resolved_at = ?, resolved_by = ?, resolution_note = ?
            WHERE id = ?
            """,
            (status, resolved_at, resolved_by, resolution_note, issue_id),
        )
        return cur.rowcount > 0

    async def get_lint_summary(self, domain: str) -> dict:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT severity, status, COUNT(*) AS c FROM lint_issues WHERE domain = ? GROUP BY severity, status",
            (domain,),
        )
        summary = {
            "total": 0, "critical": 0, "warning": 0, "info": 0,
            "pending": 0, "resolved": 0, "ignored": 0,
        }
        for r in rows.fetchall():
            sev = r["severity"]
            st = r["status"]
            cnt = r["c"]
            summary["total"] += cnt
            if sev in summary:
                summary[sev] += cnt
            if st in summary:
                summary[st] += cnt
        return summary

    async def close(self) -> None:
        await asyncio.to_thread(self._conn.close)