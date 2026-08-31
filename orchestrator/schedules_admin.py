"""统一计划管理（前端「定时计划」页的后端支撑）。

职责：
- 读写 config/schedules.yaml 的 schedules 段（ruamel round-trip 保注释回写）
- 校验：cron 5 字段合法、id 唯一（upsert 语义）、workflow_id 非空
- 计算每条 enabled schedule 的下次触发时间（复用 patroller.cron_match，分钟级暴力前搜）

id 策略：schedule_id 为不可变主键（新建时由 name slug 自动生成，运行期用户不能改）；
       name 是展示字段，可任意改；前端为 ID 不可变做了锁死，后端不再承担"按 name 唯一键"语义。

既有策略：schedules.yaml 不做热加载（避免运行中定时器漂移），回写后重启后端生效。
设计文档：docs/product-design/DESIGN_config_credential_refactor_v1.md §6.2
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from orchestrator.patroller import cron_match

logger = logging.getLogger(__name__)

# schedules.yaml 路径：orchestrator/ 的上级即项目根
SCHEDULES_YAML = Path(__file__).resolve().parents[1] / "config" / "schedules.yaml"

_MAX_SEARCH_MINUTES = 366 * 24 * 60  # cron 下次触发搜索上限：一年

# 统一计划支持的工作流（SchedulesPage 下拉数据来自 /api/agent/workflows，此处不做白名单限制）


class SchedulesConfigError(ValueError):
    """配置校验失败（API 层转 400）。"""


# ── id 生成 ─────────────────────────────────────────────────────

# slug 规则：保留字母/数字/下划线/连字符；中文等非 ASCII 字符丢弃；
# 首字符必须为字母或下划线（避免数字开头导致 yaml key 解析混淆，也避免与 cron 字段冲突）。
_SLUG_RE = re.compile(r"[A-Za-z0-9_\-]+")
_SLUG_HEAD_RE = re.compile(r"[^A-Za-z_]")  # 首字符需为字母或下划线


def _slugify_id(name: str, taken: set[str]) -> str:
    """从 name 派生稳定 id；冲突时追加 -2/-3/... 后缀。

    规则：
    - 抽取出所有 [A-Za-z0-9_-] 片段（中文/空格/标点丢弃），下划线折叠
    - 首字符非字母/下划线时前置 "s_"
    - 仍为空则回落到 "schedule"
    - 与 taken 冲突时追加 "-2"、"-" 等递增后缀
    """
    raw = name.strip()
    parts = _SLUG_RE.findall(raw)
    base = "_".join(p for p in parts if p)
    if not base:
        base = "schedule"
    if _SLUG_HEAD_RE.match(base[0]):
        base = "s_" + base
    base = base[:64]  # 防过长
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


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
        dir=str(path.parent), prefix="schedules_", suffix=".tmp"
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
    """读 yaml 文档；不存在时返回空 dict（调用方按需初始化）。"""
    p = Path(path) if path else SCHEDULES_YAML
    if not p.exists():
        return {}
    return _load_yaml(p) or {}


# ── 下次触发时间 ─────────────────────────────────────────────

def next_cron_run(cron_expr: str, from_dt: datetime | None = None) -> datetime | None:
    """从 from_dt 起暴力搜索下一个匹配 cron 的分钟（上限一年）。"""
    dt = (from_dt or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_SEARCH_MINUTES):
        if cron_match(cron_expr, dt):
            return dt
        dt += timedelta(minutes=1)
    return None


def _validate_cron(cron_expr: str) -> None:
    fields = cron_expr.split()
    if len(fields) != 5:
        raise SchedulesConfigError("cron 必须是 5 字段：minute hour day month weekday")
    # _parse_cron_field 只做语法解析、不查单值边界（61 也会进集合，运行时永不匹配），
    # 校验层必须自己比对解析结果与合法区间，否则非法 cron 能落盘成"永不触发"的死计划
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    try:
        from orchestrator.patroller import _parse_cron_field
        for field, (lo, hi) in zip(fields, bounds):
            values = _parse_cron_field(field, lo, hi)
            bad = sorted(v for v in values if v < lo or v > hi)
            if bad:
                raise ValueError(f"字段 {field!r} 越界 {bad}（合法 {lo}-{hi}）")
    except (ValueError, IndexError) as e:
        raise SchedulesConfigError(f"cron 表达式非法：{cron_expr!r}（{e}）") from e


# ── 读取 ─────────────────────────────────────────────────────

def list_schedules(path: Path | None = None) -> list[dict[str, Any]]:
    """读统一 schedules，附下次触发时间。"""
    data = _read_doc(path)
    result = []
    for sc in data.get("schedules") or []:
        enabled = bool(sc.get("enabled", True))
        cron = sc.get("cron", "")
        result.append({
            "id": str(sc.get("id", "")).strip(),
            "name": sc.get("name", ""),
            "workflow_id": sc.get("workflow_id", ""),
            "cron": cron,
            "enabled": enabled,
            "inputs": dict(sc.get("inputs") or {}),
            "next_run": next_cron_run(cron).isoformat() if enabled and cron else None,
        })
    return result


# ── upsert / delete ─────────────────────────────────────────

def upsert_schedule(p: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """新增/更新计划（按 id upsert，回写 schedules.yaml）。

    入参：
    - id：可选；不提供则按 name slug 自动生成；提供则按该 id upsert（存在覆盖、不存在新增）
    - name：必填（展示字段，可后续修改）
    - 其余校验同前

    行为：
    - 提供 id 且已存在 → 原地覆盖（id 不可变，name 跟随更新）
    - 提供 id 且不存在 → 按指定 id 新增（前端"指定 id 复制计划"场景）
    - 不提供 id → 视为新建，按 slug 生成唯一 id
    """
    data = _read_doc(path)
    name = str(p.get("name", "")).strip()
    if not name:
        raise SchedulesConfigError("计划名称不能为空")
    workflow_id = str(p.get("workflow_id", "")).strip()
    if not workflow_id:
        raise SchedulesConfigError("workflow_id 不能为空")
    cron_expr = str(p.get("cron", "")).strip()
    _validate_cron(cron_expr)
    inputs = p.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise SchedulesConfigError("inputs 必须是对象（JSON 键值对）")

    if data.get("schedules") is None:
        data["schedules"] = []
    schedules = data["schedules"]
    existing_ids = {str(s.get("id", "")).strip() for s in schedules if str(s.get("id", "")).strip()}

    requested_id_raw = p.get("id")
    # Pydantic model_dump 对未传字段也会输出 None（默认值）；显式 None 视为未指定
    if requested_id_raw is None:
        requested_id = ""
    else:
        requested_id = str(requested_id_raw).strip()
    if requested_id:
        # 按指定 id upsert：存在则覆盖，不存在则新增（不静默拒绝，避免无法指定目标 id）
        schedule_id = requested_id
    else:
        # 不指定 id：按 name slug 生成，冲突加 -N 后缀
        schedule_id = _slugify_id(name, existing_ids)

    node = {
        "id": schedule_id,
        "workflow_id": workflow_id,
        "name": name,
        "cron": cron_expr,
        "inputs": inputs,
        "enabled": bool(p.get("enabled", True)),
    }
    found = False
    for i, existing in enumerate(schedules):
        if str(existing.get("id", "")).strip() == schedule_id:
            schedules[i] = node
            found = True
            break
    if not found:
        schedules.append(node)
    _dump_yaml(data, Path(path) if path else SCHEDULES_YAML)
    return {"id": schedule_id, "name": name, "status": "stored"}


def delete_schedule(schedule_id: str, path: Path | None = None) -> dict[str, Any]:
    """按 id 删除计划。

    历史兼容：旧 yaml 可能存在缺 id 的条目（config_migrate 未补齐），此时仍按 name 兜底匹配。
    """
    data = _read_doc(path)
    schedules = data.get("schedules") or []
    sid = str(schedule_id).strip()
    remaining = [sc for sc in schedules if str(sc.get("id", "")).strip() != sid]
    if len(remaining) == len(schedules):
        # 兜底按 name 查找（一次性兼容历史 API / 历史 yaml）
        remaining = [sc for sc in schedules if sc.get("name") != sid]
        if len(remaining) == len(schedules):
            raise SchedulesConfigError(f"计划不存在：{sid}")
    data["schedules"] = remaining
    _dump_yaml(data, Path(path) if path else SCHEDULES_YAML)
    return {"id": sid, "status": "deleted"}


def delete_schedules_by_pull_source(pull_source_id: str, path: Path | None = None) -> int:
    """删除引用指定拉取源的全部计划（拉取源删除时级联，返回删除条数）。"""
    data = _read_doc(path)
    schedules = data.get("schedules") or []
    kept = [
        sc for sc in schedules
        if (sc.get("inputs") or {}).get("pull_source_id") != pull_source_id
    ]
    removed = len(schedules) - len(kept)
    if removed:
        data["schedules"] = kept
        _dump_yaml(data, Path(path) if path else SCHEDULES_YAML)
    return removed


def ensure_ids(path: Path | None = None) -> int:
    """懒迁移：补齐 schedules.yaml 中缺 id 的条目。已有 id 不覆盖；返回补齐条数。"""
    data = _read_doc(path)
    schedules = data.get("schedules") or []
    if not schedules:
        return 0
    taken: set[str] = {
        str(sc.get("id", "")).strip()
        for sc in schedules
        if str(sc.get("id", "")).strip()
    }
    filled = 0
    for sc in schedules:
        if not str(sc.get("id", "")).strip():
            sc["id"] = _slugify_id(str(sc.get("name", "")), taken)
            taken.add(sc["id"])
            filled += 1
    if filled:
        _dump_yaml(data, Path(path) if path else SCHEDULES_YAML)
        logger.info("schedules.id 懒迁移：补齐 %d 条", filled)
    return filled
