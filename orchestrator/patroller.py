"""Patroller 巡检器 — 后台 asyncio 定时任务。

两个职责：
  1. DAG run 巡检：定时扫 audit.db，找 RUNNING 状态超时（默认 30 分钟无事件）的 run，
     推断实际终止状态并 finalize_run 修复 runs.status 与 dag_events 的一致性，
     同时 emit patrol_alert 事件推送到 SSE 通道，让前端任务看板高亮显示。
  2. 统一计划触发：按 config/schedules.yaml 的 schedules 段（cron 表达式）触发工作流，
     日志巡检 / 日志拉取 / 任务巡检 / 任务调度 / 自动派发全部走统一格式。

设计原则：
  - 无状态：每次都从 audit.db 重新扫，服务重启后自动恢复
  - stale run 自动 sweep（按 dag_events 推断终止状态 finalize_run），避免 runs.status 永远卡 running
  - 只发现+通知，不自动重试（重试由用户在看板上点按钮）
  - 幂等：同一告警不重复 emit（用 _emitted_alerts 去重）
  - 阈值统一 30 分钟（按用户要求）
  - 定时配置走 config/schedules.yaml（统一格式，cron 表达式驱动），代码不硬编码业务节奏
  - patrol.yaml 只剩 dag_run_patrol / dormant_archive / log_sources 白名单等非 schedule 段
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from audit.store import EventStore

logger = logging.getLogger(__name__)


def _ev_dict(ev: Any) -> dict:
    """把 DagEvent（dataclass）转成 dict，兼容已是 dict 的情况。

    DagEvent.type 是 DagEventType(str, Enum)，转成字符串值（如 "node.failed"）。
    映射到 dict key "event_type"（与 audit.db 列名一致），其余字段名相同。
    """
    if isinstance(ev, dict):
        return ev
    return {
        "event_type": ev.type.value if hasattr(ev.type, "value") else str(ev.type),
        "run_id": ev.run_id,
        "node_id": ev.node_id,
        "payload": ev.payload,
        "occurred_at": (
            ev.occurred_at.isoformat()
            if hasattr(ev.occurred_at, "isoformat")
            else str(ev.occurred_at)
        ),
        "sequence": ev.sequence,
    }

# SSE 事件推送回调类型
EventSink = Callable[[dict[str, Any]], Awaitable[None]]
# 工作流触发回调类型（调用 orchestrator.run）
WorkflowTrigger = Callable[[str, dict[str, Any]], Awaitable[str | None]]

# 默认配置文件路径（相对项目根目录）
DEFAULT_PATROL_CONFIG_PATH = "config/patrol.yaml"
DEFAULT_SCHEDULES_CONFIG_PATH = "config/schedules.yaml"


@dataclass
class LogPatrolSchedule:
    """单条统一计划配置（原 5 个 *_schedule 段共用的形状，名称沿用历史 dataclass）。"""
    workflow_id: str
    name: str
    cron: str
    inputs: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class LogSource:
    """日志源白名单条目。"""
    id: str
    name: str
    path: str
    description: str = ""
    allow_read: bool = True
    allow_list: bool = True


# ── 轻量 cron 解析（5 字段：minute hour day month weekday）──────
# 不引入外部依赖，支持 * / */N / N / a,b / a-b 五种语法

def _parse_cron_field(expr: str, min_val: int, max_val: int) -> set[int]:
    """解析单个 cron 字段为值集合。"""
    result: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if part == "*":
            result.update(range(min_val, max_val + 1))
        elif "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if base == "*" or base == "":
                start, end = min_val, max_val
            elif "-" in base:
                lo, hi = base.split("-", 1)
                start, end = int(lo), int(hi)
            else:
                start, end = int(base), max_val
            for v in range(start, end + 1, step):
                result.add(v)
        elif "-" in part:
            lo, hi = part.split("-", 1)
            for v in range(int(lo), int(hi) + 1):
                result.add(v)
        else:
            result.add(int(part))
    return result


def cron_match(cron_expr: str, dt: datetime) -> bool:
    """判断给定 datetime 是否匹配 cron 表达式（精确到分钟）。

    5 字段：minute hour day-of-month month day-of-week（0=Sunday 或 7=Sunday）
    """
    fields = cron_expr.split()
    if len(fields) != 5:
        return False
    minute_set = _parse_cron_field(fields[0], 0, 59)
    hour_set = _parse_cron_field(fields[1], 0, 23)
    dom_set = _parse_cron_field(fields[2], 1, 31)
    month_set = _parse_cron_field(fields[3], 1, 12)
    dow_set = _parse_cron_field(fields[4], 0, 7)
    # 0 和 7 都表示周日
    if 7 in dow_set:
        dow_set.add(0)

    if dt.minute not in minute_set:
        return False
    if dt.hour not in hour_set:
        return False
    if dt.day not in dom_set:
        return False
    if dt.month not in month_set:
        return False
    # Python weekday() 周一=0..周日=6；cron 周日=0..周六=6
    cron_dow = (dt.weekday() + 1) % 7
    if cron_dow not in dow_set:
        return False
    return True


def load_patrol_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """加载 patrol.yaml 配置。不存在时返回空 dict（由调用方回退默认值）。"""
    path = Path(config_path) if config_path else Path(DEFAULT_PATROL_CONFIG_PATH)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info("已加载巡检配置 %s", path)
        return data
    except Exception as e:
        logger.warning("加载巡检配置 %s 失败，使用代码默认值: %s", path, e)
        return {}


def load_schedules_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """加载 config/schedules.yaml（统一计划）。不存在/解析失败时返回空 dict。"""
    path = Path(config_path) if config_path else Path(DEFAULT_SCHEDULES_CONFIG_PATH)
    if not path.exists():
        logger.warning(
            "统一计划配置 %s 不存在（config_migrate 未执行或失败？），无定时计划运行",
            path,
        )
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info("已加载统一计划配置 %s（%d 条）", path, len(data.get("schedules") or []))
        return data
    except Exception as e:
        logger.error("加载统一计划配置 %s 失败: %s", path, e)
        return {}


class Patroller:
    """巡检器：后台 asyncio 定时任务。

    用法：
        patroller = Patroller(
            event_store=store,
            event_sink=lambda ev: sse_broadcast(ev),
            workflow_trigger=lambda wid, inputs: trigger_run(wid, inputs),
        )
        patroller.start()  # FastAPI startup
        ...
        await patroller.stop()  # FastAPI shutdown
    """

    def __init__(
        self,
        event_store: EventStore,
        event_sink: EventSink | None = None,
        workflow_trigger: WorkflowTrigger | None = None,
        # DAG run 巡检配置
        patrol_interval_seconds: int = 60,
        stale_threshold_seconds: int = 1800,  # 30 分钟
        # 日志巡检定时触发配置（向后兼容；无 schedule 时回退固定间隔模式）
        log_patrol_interval_seconds: int = 3600,  # 1 小时
        log_patrol_workflow_id: str = "log-patrol",
        log_patrol_inputs: dict[str, Any] | None = None,
        # 配置文件路径（不传用默认 config/patrol.yaml + config/schedules.yaml）
        config_path: str | Path | None = None,
        schedules_config_path: str | Path | None = None,
    ):
        self._event_store = event_store
        self._event_sink = event_sink
        self._workflow_trigger = workflow_trigger

        self._patrol_interval = patrol_interval_seconds
        self._stale_threshold = stale_threshold_seconds

        self._log_patrol_interval = log_patrol_interval_seconds
        self._log_patrol_workflow_id = log_patrol_workflow_id
        # 日志目录默认从环境变量 LOG_PATROL_DIR 读，或用 ./logs/
        self._log_patrol_inputs = log_patrol_inputs or {
            "log_dir": os.environ.get("LOG_PATROL_DIR", "./logs/"),
            "time_range": "24h",
            "level": "ERROR",
        }

        self._patrol_task: asyncio.Task | None = None
        # 统一计划：每条 schedule 一个 task（cron 驱动，全部走 _cron_loop）
        self._schedule_tasks: list[asyncio.Task] = []
        # 向后兼容：保留 _log_patrol_task 属性名（无任何 schedule 时的固定间隔回退模式）
        self._log_patrol_task: asyncio.Task | None = None
        # P0.18.11: sandbox 延迟清理 task
        self._sandbox_cleanup_task: asyncio.Task | None = None
        # 去重：已 emit 的告警 key 集合（run_id:alert_type）
        self._emitted_alerts: set[str] = set()

        # ── 加载 config/patrol.yaml（dag_run_patrol / dormant_archive / log_sources）──
        cfg = load_patrol_config(config_path)
        self._config = cfg

        dag_cfg = cfg.get("dag_run_patrol") or {}
        if "interval_seconds" in dag_cfg:
            self._patrol_interval = int(dag_cfg["interval_seconds"])
        if "stale_threshold_seconds" in dag_cfg:
            self._stale_threshold = int(dag_cfg["stale_threshold_seconds"])

        # ── 加载 config/schedules.yaml（统一计划，替代原 5 个 *_schedule 段）──
        # 字段：name / workflow_id / cron / inputs / enabled，命中即 workflow_trigger
        sched_cfg = load_schedules_config(schedules_config_path)
        self._schedules: list[LogPatrolSchedule] = []
        for item in sched_cfg.get("schedules") or []:
            try:
                self._schedules.append(LogPatrolSchedule(
                    workflow_id=item.get("workflow_id", ""),
                    name=item.get("name", "未命名"),
                    cron=item["cron"],
                    inputs=item.get("inputs") or {},
                    enabled=bool(item.get("enabled", True)),
                ))
            except (KeyError, TypeError) as e:
                logger.warning("schedules.yaml 条目解析失败，跳过: %s (item=%s)", e, item)

        # 解析 log_sources 白名单
        self._log_sources: dict[str, LogSource] = {}
        for item in cfg.get("log_sources") or []:
            sid = item.get("id")
            if not sid:
                continue
            self._log_sources[sid] = LogSource(
                id=sid,
                name=item.get("name", sid),
                path=item.get("path", ""),
                description=item.get("description", ""),
                allow_read=bool(item.get("allow_read", True)),
                allow_list=bool(item.get("allow_list", True)),
            )

        # dormant 归档配置
        dormant_cfg = cfg.get("dormant_archive") or {}
        self._dormant_archive_enabled = bool(dormant_cfg.get("enabled", True))
        self._dormant_archive_after_days = int(dormant_cfg.get("archive_after_days", 3))

        logger.info(
            "Patroller 配置加载完成：dag_run 巡检 %ss/次，超时 %ss；统一计划 %d 条（schedules.yaml），%d 个 log_source",
            self._patrol_interval, self._stale_threshold,
            len(self._schedules), len(self._log_sources),
        )

    def get_log_sources(self) -> dict[str, LogSource]:
        """返回 log_sources 白名单（供 ops_tools 校验路径授权）。"""
        return self._log_sources

    def get_schedules(self) -> list[LogPatrolSchedule]:
        """返回统一 schedule 列表（供 API 查询当前调度）。"""
        return list(self._schedules)

    def start(self) -> None:
        """启动巡检后台任务。FastAPI startup 时调用。"""
        if self._patrol_task is None:
            self._patrol_task = asyncio.create_task(self._patrol_loop())
            logger.info("Patroller DAG run 巡检已启动，间隔 %ss，超时阈值 %ss",
                        self._patrol_interval, self._stale_threshold)

        # 统一计划：每条 schedule 一个 _cron_loop task（原 5 组循环收敛为 1 组）
        # workflow_id 决定实际触发哪个工作流（log-patrol / log-puller / task-* 等）
        for sch in self._schedules:
            if not sch.enabled:
                logger.info("Patroller 统一计划 '%s' 已禁用，跳过", sch.name)
                continue
            task = asyncio.create_task(self._cron_loop(sch))
            self._schedule_tasks.append(task)
            logger.info("Patroller 统一计划 '%s' 已启动，cron=%s，workflow=%s",
                        sch.name, sch.cron, sch.workflow_id)

        # 无任何 schedule 时回退固定间隔模式（schedules.yaml 缺失的兜底）
        if not self._schedules and self._log_patrol_task is None:
            self._log_patrol_task = asyncio.create_task(self._log_patrol_loop())
            logger.info("Patroller 日志巡检定时触发已启动（无 schedule 回退模式），间隔 %ss，工作流 %s",
                        self._log_patrol_interval, self._log_patrol_workflow_id)

        # P0.18.11: 启动 sandbox 延迟清理定时任务（每日 03:30 UTC）
        if self._sandbox_cleanup_task is None:
            self._sandbox_cleanup_task = asyncio.create_task(self._sandbox_cleanup_loop())
            logger.info("Patroller sandbox 延迟清理已启动（每日 03:30 UTC）")

    async def stop(self) -> None:
        """停止巡检后台任务。FastAPI shutdown 时调用。"""
        tasks = [
            self._patrol_task,
            self._log_patrol_task,
            *self._schedule_tasks,
            self._sandbox_cleanup_task,
        ]
        self._patrol_task = None
        self._log_patrol_task = None
        self._schedule_tasks.clear()
        self._sandbox_cleanup_task = None
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("Patroller 已停止")

    # ====== DAG run 巡检 ======

    async def _patrol_loop(self) -> None:
        """DAG run 巡检主循环。"""
        while True:
            try:
                await self._patrol_once()
            except Exception as e:
                logger.exception("Patroller DAG run 巡检异常: %s", e)
            await asyncio.sleep(self._patrol_interval)

    async def _patrol_once(self) -> None:
        """单次 DAG run 巡检：扫 RUNNING 状态的 run，找异常。

        修复点 1：只对 run_mode IN (templated, hybrid) 告警超时，
                  conversational/task 的 active/dormant 状态不告警（正常休眠）。
        修复点 2：stale run（超时无新事件）按 dag_events 推断终止状态，
                  调用 finalize_run 修复 runs.status 与 dag_events 一致性，
                  避免前端"列表 running + 详情 completed"的不一致体验。
        P0.18.13 新增：runs 表 stale 收敛（覆盖 conversational/task），
                  监控中心 list_active_runs 依赖 runs.status IN (pending/running/waiting)，
                  历史遗留 run 不会自动转 terminated，监控永远显示运行中。
        """
        now = datetime.now(timezone.utc)
        # v2: list_runs → list_sessions（runs 表已合并到 sessions 表）
        running_runs = await self._event_store.list_sessions(status="running", limit=100)

        for run in running_runs:
            # v2: 字段名 session_id（原 run_id）
            run_id = run.get("session_id", "")
            run_mode = run.get("run_mode", "templated")
            started_at_str = run.get("started_at", "")
            workflow_id = run.get("workflow_id", "")

            # 只对 templated/hybrid 告警超时；conversational/task 用 active/dormant 不走这里
            if run_mode in ("conversational", "task"):
                continue

            try:
                started_at = datetime.fromisoformat(started_at_str)
            except (ValueError, TypeError):
                continue

            # 查最后一条事件的时间（v3: 方法名 get_run_events，返回 DagEvent 对象）
            events_raw = await self._event_store.get_run_events(run_id, since=0)
            events = [_ev_dict(e) for e in events_raw]
            if events:
                last_event_at_str = events[-1].get("occurred_at", started_at_str)
                try:
                    last_event_at = datetime.fromisoformat(str(last_event_at_str))
                except (ValueError, TypeError):
                    last_event_at = started_at
            else:
                last_event_at = started_at

            stale_seconds = (now - last_event_at).total_seconds()
            alerts: list[dict[str, Any]] = []

            # 1. 超时无事件 → 自动 finalize 推断终止状态（解决"列表 running + 详情 completed"不一致）
            if stale_seconds > self._stale_threshold:
                alert_key = f"{run_id}:stale"
                sweep_result = await self._sweep_stale_run(run_id, events)
                if sweep_result:
                    alerts.append({
                        "type": "stale_auto_finalized",
                        "message": (
                            f"Run {run_id} 已 {int(stale_seconds)}s 无事件，"
                            f"自动 finalize 为 {sweep_result['status']}"
                        ),
                        "severity": "info",
                        "stale_seconds": int(stale_seconds),
                        "finalized_status": sweep_result["status"],
                    })
                elif alert_key not in self._emitted_alerts:
                    # 没 events 时无法推断，仅告警不自动 finalize
                    alerts.append({
                        "type": "stale",
                        "message": f"Run {run_id} 已 {int(stale_seconds)}s 无事件（超时阈值 {self._stale_threshold}s）",
                        "severity": "warning",
                        "stale_seconds": int(stale_seconds),
                    })
                self._emitted_alerts.add(alert_key)

            # 2. 有 NODE_FAILED 但 run 还是 running（异常状态未传播）
            failed_nodes = [e for e in events if e.get("event_type") == "node.failed"]
            if failed_nodes:
                alert_key = f"{run_id}:node_failed"
                if alert_key not in self._emitted_alerts:
                    alerts.append({
                        "type": "node_failed_not_propagated",
                        "message": f"Run {run_id} 有节点失败但 run 状态未传播",
                        "severity": "error",
                        "failed_nodes": [e.get("node_id") for e in failed_nodes],
                    })
                    self._emitted_alerts.add(alert_key)

            if alerts and self._event_sink:
                await self._event_sink({
                    "type": "patrol_alert",
                    "run_id": run_id,
                    "workflow_id": workflow_id,
                    "started_at": started_at_str,
                    "alerts": alerts,
                    "patrolled_at": now.isoformat(),
                })
                logger.warning("Patroller 发现异常 run %s: %s", run_id, alerts)

        # P0.18.13: runs 表 stale 收敛（覆盖 conversational/task）
        # 监控中心 list_active_runs 查 runs.status IN (pending/running/waiting)，
        # 历史遗留 run 不会自动转 terminated → 永远显示"运行中"。
        # 这里按 created_at 阈值统一收敛，conversational 也覆盖（它的 active/dormant 状态由 sessions.status 决定，runs.status 收敛不影响正常会话）。
        await self._sweep_stale_runs_table(now)

        # dormant > 3 天自动归档（软归档，不删数据，设 archived_at）
        await self._archive_stale_dormant(now)

    async def _archive_stale_dormant(self, now: datetime) -> None:
        """dormant 超期会话自动设 archived_at（软归档）。"""
        if not self._dormant_archive_enabled:
            return
        try:
            dormant_sessions = await self._event_store.list_sessions(
                status="dormant", limit=200
            )
        except Exception:
            return

        archive_threshold = now.timestamp() - self._dormant_archive_after_days * 86400
        archived_count = 0
        for s in dormant_sessions:
            # 已归档的跳过
            if s.get("archived_at"):
                continue
            last_activity = s.get("last_activity_at") or s.get("started_at", "")
            try:
                last_dt = datetime.fromisoformat(last_activity)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt.timestamp() < archive_threshold:
                    # v2: 字段名 session_id（原 run_id）
                    run_id = s.get("session_id", "")
                    if run_id:
                        await self._event_store.archive_session(run_id)
                        archived_count += 1
            except (ValueError, TypeError):
                continue
        if archived_count:
            logger.info("dormant 归档扫描完成，%d 个会话已自动归档（阈值 %s 天）",
                        archived_count, self._dormant_archive_after_days)

    # ====== P0.18.13: runs 表 stale 收敛 ======

    async def _sweep_stale_runs_table(self, now: datetime) -> None:
        """runs 表 stale 收敛（覆盖所有 run_mode，含 conversational/task）。

        触发条件：runs.status IN (pending, running, waiting) 且 created_at 距 now 超过阈值
        （复用 _stale_threshold，30 分钟）。命中后将该 run 标 cancelled + 写 finished_at + error。
        不修改 sessions.status（会话的 active/dormant 状态由 session_manager 维护，不受 runs 收敛影响）。

        幂等：每次只更新未终止的 run；同一 run 不会被多次收敛。
        """
        if not hasattr(self._event_store, "list_stale_runs"):
            return
        try:
            threshold_iso = (
                now - timedelta(seconds=self._stale_threshold)
            ).isoformat()
            stale = await self._event_store.list_stale_runs(
                threshold_iso=threshold_iso, limit=100,
            )
        except Exception as e:
            logger.warning("sweep_stale_runs 拉取列表失败: %s", e)
            return
        if not stale:
            return

        swept = 0
        for r in stale:
            run_id = r.get("run_id", "")
            if not run_id:
                continue
            try:
                await self._event_store.update_run_status(
                    run_id,
                    "cancelled",
                    finished_at=now.isoformat(),
                    error=f"stale_timeout_{int(self._stale_threshold)}s",
                )
                swept += 1
                if self._event_sink:
                    await self._event_sink({
                        "type": "patrol_alert",
                        "run_id": run_id,
                        "workflow_id": r.get("workflow_id", ""),
                        "agent_id": r.get("agent_id", ""),
                        "run_mode": r.get("run_mode", ""),
                        "alerts": [{
                            "type": "stale_runs_table_swept",
                            "message": (
                                f"Run {run_id} 在 runs 表卡 {int(self._stale_threshold)}s 未终止，"
                                f"自动收敛为 cancelled"
                            ),
                            "severity": "info",
                        }],
                        "patrolled_at": now.isoformat(),
                    })
            except Exception as e:
                logger.warning("sweep_stale_runs 收敛 %s 失败: %s", run_id, e)

        if swept:
            logger.info(
                "runs 表 stale 收敛完成：扫描 %d 个，超时 %d 个，已收敛 %d 个（阈值 %ds）",
                len(stale), len(stale), swept, self._stale_threshold,
            )

    # ====== P0.18.11: Sandbox 延迟清理 ======
    # bind_mount 模式不复制，sandbox 是源目录，不删；
    # local_copy / git_clone 模式 sandbox 是 workspace/{wf_id}/{run_id}/，
    #   run 结束后保留 30 天，由 patroller 每日扫 cleanup_at < now() 删除物理目录并标 deleted。
    # isolated 模式 sandbox 是空目录，无内容可清，跳过。

    async def cleanup_sandboxes_once(self) -> dict[str, int]:
        """P0.18.11: 单次扫描 + 清理过期 sandbox。

        Returns:
            {"scanned": N, "deleted": M, "failed": K} — 供 API 手动清理接口与定时任务复用
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        scanned = 0
        deleted = 0
        failed = 0
        try:
            sandboxes = await self._event_store.list_sandboxes_for_cleanup(now_iso, limit=200)
        except Exception as e:
            logger.error("sandbox_cleanup 拉取列表失败: %s", e)
            return {"scanned": 0, "deleted": 0, "failed": 0}

        for sb in sandboxes:
            scanned += 1
            run_id = sb.get("run_id", "")
            workspace_root = sb.get("workspace_root", "")
            mode = sb.get("workspace_mode", "")
            # bind_mount 模式无 sandbox；isolated 空目录跳过
            if mode in ("bind_mount", "isolated", ""):
                # 仅标 deleted，不再尝试删文件
                try:
                    await self._event_store.mark_sandbox_deleted(run_id)
                    deleted += 1
                except Exception as e:
                    logger.warning("sandbox_cleanup 标记 %s deleted 失败: %s", run_id, e)
                    failed += 1
                continue
            # local_copy / git_clone：删物理目录
            if workspace_root:
                try:
                    p = Path(workspace_root)
                    if p.exists():
                        # 防御：路径必须在 AGENTOPS_HOME/workspaces/ 下，避免误删授权源目录
                        # (bind_mount 模式下 workspace_root = 用户授权路径，已被上面短路跳过)
                        import shutil
                        shutil.rmtree(p, ignore_errors=True)
                    await self._event_store.mark_sandbox_deleted(run_id)
                    deleted += 1
                    logger.info("sandbox_cleanup 已删除 run_id=%s path=%s", run_id, workspace_root)
                except Exception as e:
                    logger.error("sandbox_cleanup 删 %s 失败: %s", workspace_root, e)
                    failed += 1
            else:
                # 无 workspace_root，仅标 deleted
                try:
                    await self._event_store.mark_sandbox_deleted(run_id)
                    deleted += 1
                except Exception as e:
                    logger.warning("sandbox_cleanup 标记 %s deleted 失败: %s", run_id, e)
                    failed += 1

        if scanned:
            logger.info("sandbox_cleanup 完成：scanned=%d deleted=%d failed=%d",
                        scanned, deleted, failed)
        return {"scanned": scanned, "deleted": deleted, "failed": failed}

    async def _sandbox_cleanup_loop(self) -> None:
        """P0.18.11: sandbox 延迟清理主循环（每日 03:30 UTC 触发一次）。"""
        # cron 表达式：每天 3:30 UTC
        cron_expr = "30 3 * * *"
        # 启动后先延迟 60s
        await asyncio.sleep(60)
        last_triggered_day: str | None = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                today_key = f"{now.year}-{now.month:02d}-{now.day:02d}"
                if today_key != last_triggered_day and cron_match(cron_expr, now):
                    logger.info("sandbox_cleanup 每日扫描触发（%s）", today_key)
                    await self.cleanup_sandboxes_once()
                    last_triggered_day = today_key
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("sandbox_cleanup 异常: %s", e)
            # 每 5 分钟检查一次 cron（避免每分钟都跑 cron_match）
            await asyncio.sleep(300)

    async def _sweep_stale_run(
        self, run_id: str, events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """按 dag_events 推断 stale run 终止状态并 finalize_run。

        推断规则：
          - 有 run.failed → status=failed
          - 有 run.completed → status=completed
          - 有 node.completed（桥接死掉但实际成功）→ status=completed
          - 仅 widget.update（LLM 已完成但无终止事件）→ status=completed
          - 无任何事件 → 返回 None，不自动 finalize（保留人工判断）

        Returns:
            {"status": ..., "finished_at": ...} 或 None
        """
        if not events:
            return None

        # 按 sequence 排序（get_events 已按 sequence 升序，但防御一下）
        events_sorted = sorted(events, key=lambda e: e.get("sequence", 0))
        # DagEventType 枚举值是带点的（"run.failed" / "node.completed"），CLAUDE.md 铁律
        failed_ev = next(
            (e for e in events_sorted if e.get("event_type") == "run.failed"), None,
        )
        completed_ev = next(
            (e for e in events_sorted if e.get("event_type") == "run.completed"), None,
        )
        node_completed_ev = next(
            (e for e in events_sorted if e.get("event_type") == "node.completed"), None,
        )
        widget_update_ev = next(
            (e for e in events_sorted if e.get("event_type") == "widget.update"), None,
        )
        last_ev = events_sorted[-1]

        if failed_ev:
            new_status = "failed"
            finished_at_str = failed_ev.get("occurred_at", "")
            try:
                payload = json.loads(failed_ev.get("payload", "{}"))
                error = payload.get("error", "unknown")
            except Exception:
                error = "unknown"
        elif completed_ev:
            new_status = "completed"
            finished_at_str = completed_ev.get("occurred_at", "")
            error = None
        elif node_completed_ev:
            # ConversationalEngine 已完成节点但 run bridge 死掉
            new_status = "completed"
            finished_at_str = node_completed_ev.get("occurred_at", "")
            error = None
        else:
            # 只有 widget.update 没终止事件 → 视为 LLM 已完成
            new_status = "completed"
            finished_at_str = (
                widget_update_ev.get("occurred_at", "")
                if widget_update_ev else last_ev.get("occurred_at", "")
            )
            error = None

        try:
            finished_at = datetime.fromisoformat(finished_at_str)
        except (ValueError, TypeError):
            finished_at = datetime.now(timezone.utc)

        # 聚合 token usage（从 widget.update 事件中尝试提取，如有）
        total_in = 0
        total_out = 0
        for ev in events_sorted:
            if ev.get("event_type") == "widget.update":
                try:
                    # 解析 payload 但暂未提取 usage（占位，未来扩展）
                    json.loads(ev.get("payload", "{}"))
                except Exception:
                    pass

        try:
            # v3: finalize_run 写入 runs 表（替代 v2 finalize_session）
            await self._event_store.finalize_run(
                run_id=run_id,
                status=new_status,
                finished_at=finished_at,
                total_tokens_in=total_in,
                total_tokens_out=total_out,
                error=error,
            )
            logger.info(
                "Patroller 自动 finalize stale run %s → %s（finished_at=%s）",
                run_id, new_status, finished_at.isoformat(),
            )
            return {"status": new_status, "finished_at": finished_at}
        except Exception as e:
            logger.error("Patroller finalize_run %s 失败: %s", run_id, e)
            return None

    # ====== 巡检定时触发（cron 驱动，log_analyst 和 task_patrol 共用）======

    async def _cron_loop(self, schedule: LogPatrolSchedule) -> None:
        """cron 驱动的巡检循环（每分钟检查一次 cron 是否匹配）。

        通用方法：对 log_patrol_schedule 和 task_patrol_schedule 都用，
        schedule.workflow_id 决定实际触发哪个工作流（log-patrol / task-patrol）。

        匹配策略：每分钟整点检查 cron_match(now)；匹配则触发对应 schedule。
        为避免服务启动后立刻触发（与上次触发时间重叠），首次延迟 60s。
        """
        await asyncio.sleep(60)
        last_triggered_minute: datetime | None = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                # 同一分钟内不重复触发（防 cron_match 在循环中被多次命中）
                if last_triggered_minute is None or (
                    (now.year, now.month, now.day, now.hour, now.minute)
                    != (last_triggered_minute.year, last_triggered_minute.month,
                        last_triggered_minute.day, last_triggered_minute.hour,
                        last_triggered_minute.minute)
                ):
                    if cron_match(schedule.cron, now):
                        await self._trigger_workflow_with(schedule)
                        last_triggered_minute = now
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Patroller schedule '%s' 触发异常: %s",
                                 schedule.name, e)
            # 每分钟检查一次（cron 精确到分钟）
            await asyncio.sleep(60)

    async def _log_patrol_loop(self) -> None:
        """日志巡检定时触发主循环（兼容模式，无 patrol.yaml 时使用）。"""
        # 首次延迟 60s 启动（避免服务刚启动就触发）
        await asyncio.sleep(60)
        while True:
            try:
                await self._trigger_log_patrol()
            except Exception as e:
                logger.exception("Patroller 日志巡检触发异常: %s", e)
            await asyncio.sleep(self._log_patrol_interval)

    async def _trigger_workflow_with(self, schedule: LogPatrolSchedule) -> None:
        """按指定 schedule 触发工作流（通用，log_analyst 和 task_patrol 共用）。

        事件 type 按 schedule.workflow_id 区分：
          - workflow_id 以 "log-patrol" 开头 → type="log_patrol_triggered"
          - workflow_id 以 "task-patrol" 开头 → type="task_patrol_triggered"
          - 其他 → type="workflow_triggered"
        """
        if not self._workflow_trigger:
            logger.debug("未配置 workflow_trigger，跳过 schedule '%s' 触发", schedule.name)
            return

        logger.info("触发 schedule '%s'，workflow=%s，inputs=%s",
                    schedule.name, schedule.workflow_id, schedule.inputs)
        try:
            run_id = await self._workflow_trigger(
                schedule.workflow_id,
                schedule.inputs,
            )
            if run_id:
                logger.info("schedule '%s' 已启动，workflow=%s，run_id=%s",
                            schedule.name, schedule.workflow_id, run_id)
                if self._event_sink:
                    # 事件 type 按 workflow_id 区分，便于前端按类型展示
                    wf_id = (schedule.workflow_id or "").lower()
                    if wf_id.startswith("log-patrol"):
                        ev_type = "log_patrol_triggered"
                    elif wf_id.startswith("task-patrol"):
                        ev_type = "task_patrol_triggered"
                    elif wf_id.startswith("task-conductor"):
                        ev_type = "task_conductor_triggered"
                    else:
                        ev_type = "workflow_triggered"
                    await self._event_sink({
                        "type": ev_type,
                        "run_id": run_id,
                        "workflow_id": schedule.workflow_id,
                        "schedule_name": schedule.name,
                        "cron": schedule.cron,
                        "inputs": schedule.inputs,
                        "triggered_at": datetime.now(timezone.utc).isoformat(),
                    })
            else:
                logger.warning("schedule '%s' 触发失败：未返回 run_id",
                               schedule.name)
        except Exception as e:
            logger.error("触发 schedule '%s' 失败: %s", schedule.name, e)

    async def _trigger_log_patrol(self) -> None:
        """触发一次日志巡检工作流（兼容模式，无 schedule 时使用）。"""
        if not self._workflow_trigger:
            logger.debug("未配置 workflow_trigger，跳过日志巡检触发")
            return

        logger.info("触发日志巡检工作流 %s，inputs=%s",
                    self._log_patrol_workflow_id, self._log_patrol_inputs)
        try:
            run_id = await self._workflow_trigger(
                self._log_patrol_workflow_id,
                self._log_patrol_inputs,
            )
            if run_id:
                logger.info("日志巡检工作流已启动，run_id=%s", run_id)
                # 推送事件到前端，让看板显示"日志巡检已触发"
                if self._event_sink:
                    await self._event_sink({
                        "type": "log_patrol_triggered",
                        "run_id": run_id,
                        "workflow_id": self._log_patrol_workflow_id,
                        "inputs": self._log_patrol_inputs,
                        "triggered_at": datetime.now(timezone.utc).isoformat(),
                    })
            else:
                logger.warning("日志巡检工作流触发失败：未返回 run_id")
        except Exception as e:
            logger.error("触发日志巡检工作流失败: %s", e)

    # ====== 手动触发接口 ======

    async def trigger_log_patrol_now(self, inputs: dict[str, Any] | None = None) -> str | None:
        """手动触发日志巡检（供 API 调用）。

        Args:
            inputs: 覆盖默认 inputs（如指定不同的 log_dir）

        Returns:
            run_id 或 None
        """
        if not self._workflow_trigger:
            return None
        actual_inputs = inputs or self._log_patrol_inputs
        return await self._workflow_trigger(
            self._log_patrol_workflow_id,
            actual_inputs,
        )
