"""config_migrate — 一次性配置迁移（lifespan 启动时自动执行）。

迁移内容（DESIGN_config_credential_refactor_v1.md §9）：
- patrol.yaml 的 log_pull_sources → ~/.agentops/private/log-pull.yaml
  （拆成 connections + pull_sources 两个列表；credential_id 归一化修复历史 bug）
- patrol.yaml 的 5 个 *_schedule 段 → config/schedules.yaml 的统一 schedules 段
- 从 patrol.yaml 删除已迁出的旧段（ruamel round-trip，保留其余内容与注释）

策略（本地研发环境，无兼容期）：
- 幂等：目标文件已存在则跳过对应迁移（--force 强制从旧段重建）
- 原子写：三个文件全部先写临时文件，再依次 os.replace；任一步失败记 ERROR 不落盘
- 迁移失败：启动日志报 ERROR，人工修复后重启（不回退双读）
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from orchestrator.log_pull_admin import normalize_credential_id
from orchestrator.schedules_admin import _slugify_id

logger = logging.getLogger(__name__)

PATROL_YAML = Path(__file__).resolve().parents[1] / "config" / "patrol.yaml"
PRIVATE_YAML = Path.home() / ".agentops" / "private" / "log-pull.yaml"
SCHEDULES_YAML = Path(__file__).resolve().parents[1] / "config" / "schedules.yaml"

# 旧 schedule 段名 → 条目缺 workflow_id 时的默认值（与旧 patroller 解析逻辑一致）
OLD_SCHEDULE_SECTIONS: dict[str, str] = {
    "log_patrol_schedule": "log-patrol",
    "log_pull_schedule": "log-puller",
    "task_patrol_schedule": "task-patrol",
    "task_conductor_schedule": "task-conductor",
    "task_dispatcher_schedule": "task-dispatcher",
}

_PRIVATE_HEADER = (
    "# ~/.agentops/private/log-pull.yaml — 服务器连接对象 + 日志拉取任务\n"
    "# 敏感文件（真实 IP/端口/用户名/远程路径）：不进 git，勿拷入项目目录\n"
    "# 凭据（密码/私钥口令）不在本文件：加密存 credential_store（id 形如 ssh:<connection_id>）\n"
    "# 修改后重启后端生效（不做热加载）\n"
)

_SCHEDULES_HEADER = (
    "# config/schedules.yaml — 统一定时计划（所有 cron 驱动的自动任务）\n"
    "# 唯一键：name（upsert 语义）；由前端「定时计划」页管理\n"
    "# inputs 原样透传给 orchestrator.run(workflow_id, inputs)\n"
    "# 修改后重启后端生效（不做热加载）\n"
)


def _yaml_io() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    return y


def _serialize(data: Any, header: str = "") -> str:
    buf = io.StringIO()
    _yaml_io().dump(data, buf)
    return header + buf.getvalue()


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + os.replace 原子写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix="migrate_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _build_private_doc(old_sources: list[Any]) -> dict[str, Any]:
    """把旧 log_pull_sources 拆成 connections + pull_sources。

    ID 映射规则（§9.2）：
    - connection id = 原 source id（凭据锚点不变，SSH 凭据零重录）
    - credential_id 空/None/"None" → ssh:<id>（归一化，修复 MaxKB 现存 bug）
    - pull_source id = 原 source id（schedule 的 inputs.pull_source_id 不变）
    """
    connections: list[dict[str, Any]] = []
    pull_sources: list[dict[str, Any]] = []
    seen_conn_ids: set[str] = set()
    for s in old_sources:
        sid = str(s.get("id", "")).strip()
        if not sid:
            logger.warning("迁移跳过无 id 的 log_pull_sources 条目: %s", s)
            continue
        auth = dict(s.get("auth") or {})
        # 归一化（历史 bug：str(None) 落盘成字符串 "None"）
        auth["credential_id"] = normalize_credential_id(auth.get("credential_id"), sid)
        if sid not in seen_conn_ids:
            seen_conn_ids.add(sid)
            connections.append({
                "id": sid,
                "name": s.get("name", sid),
                "host": s.get("host", ""),
                "port": int(s.get("port", 22)),
                "username": s.get("username", ""),
                "auth": auth,
                "enabled": bool(s.get("enabled", False)),
            })
        pull_sources.append({
            "id": sid,
            "name": s.get("name", sid),
            "connection_id": sid,
            "remote": dict(s.get("remote") or {}),
            "local": dict(s.get("local") or {}),
            "retention": dict(s.get("retention") or {}),
            "enabled": bool(s.get("enabled", False)),
        })
    return {"connections": connections, "pull_sources": pull_sources}


def _build_schedules_items(patrol_data: Any) -> list[dict[str, Any]]:
    """把 5 个旧 schedule 段合并为统一 schedules 列表（字段原样平移，并补 id）。"""
    items: list[dict[str, Any]] = []
    taken: set[str] = set()
    for section, default_wf in OLD_SCHEDULE_SECTIONS.items():
        for item in patrol_data.get(section) or []:
            node = dict(item)
            if not str(node.get("workflow_id", "")).strip():
                node["workflow_id"] = default_wf
            # 旧 yaml 没有 id，按 name slug 生成稳定 id（与运行期新建语义一致）
            if not str(node.get("id", "")).strip():
                node["id"] = _slugify_id(str(node.get("name", "")), taken)
                taken.add(node["id"])
            else:
                taken.add(str(node["id"]))
            items.append(node)
    return items


def run_once(
    force: bool = False,
    patrol_path: Path | None = None,
    private_path: Path | None = None,
    schedules_path: Path | None = None,
) -> dict[str, Any]:
    """执行一次迁移。返回执行摘要（供日志与测试断言）。"""
    patrol = Path(patrol_path) if patrol_path else PATROL_YAML
    private = Path(private_path) if private_path else PRIVATE_YAML
    schedules = Path(schedules_path) if schedules_path else SCHEDULES_YAML

    summary: dict[str, Any] = {
        "migrated": False, "private_written": False, "schedules_written": False,
        "patrol_cleaned": False, "connections": 0, "pull_sources": 0, "schedules": 0,
    }
    if not patrol.exists():
        return summary

    data = _yaml_io().load(patrol.read_text(encoding="utf-8")) or {}

    old_sources = list(data.get("log_pull_sources") or [])
    old_sched_items = _build_schedules_items(data)
    if not old_sources and not old_sched_items:
        return summary  # 无旧段，无需迁移

    # ── 1. private/log-pull.yaml ──
    private_text: str | None = None
    if old_sources and (force or not private.exists()):
        private_doc = _build_private_doc(old_sources)
        private_text = _serialize(private_doc, _PRIVATE_HEADER)
        summary["connections"] = len(private_doc["connections"])
        summary["pull_sources"] = len(private_doc["pull_sources"])
        summary["private_written"] = True

    # ── 2. config/schedules.yaml ──
    schedules_text: str | None = None
    if old_sched_items and (force or not schedules.exists()):
        schedules_text = _serialize({"schedules": old_sched_items}, _SCHEDULES_HEADER)
        summary["schedules"] = len(old_sched_items)
        summary["schedules_written"] = True

    # ── 3. patrol.yaml 清理（仅当迁移目标已满足：刚写入或已存在）──
    patrol_text: str | None = None
    if old_sources and (private_text is not None or private.exists()):
        del data["log_pull_sources"]
        summary["patrol_cleaned"] = True
    for section in OLD_SCHEDULE_SECTIONS:
        if data.get(section) and (schedules_text is not None or schedules.exists()):
            del data[section]
            summary["patrol_cleaned"] = True
    if summary["patrol_cleaned"]:
        patrol_text = _serialize(data)

    # ── 4. 原子写（全部成功才替换；任一步失败整体不落盘）──
    try:
        if private_text is not None:
            _atomic_write(private, private_text)
        if schedules_text is not None:
            _atomic_write(schedules, schedules_text)
        if patrol_text is not None:
            _atomic_write(patrol, patrol_text)
    except Exception as e:
        logger.error("config_migrate 写入失败（配置未完整落盘，请人工检查）: %s", e)
        raise

    summary["migrated"] = (
        summary["private_written"] or summary["schedules_written"] or summary["patrol_cleaned"]
    )
    if summary["migrated"]:
        logger.info(
            "config_migrate 完成：private=%s（%d connections / %d pull_sources），"
            "schedules=%s（%d 条），patrol.yaml 旧段清理=%s",
            summary["private_written"], summary["connections"], summary["pull_sources"],
            summary["schedules_written"], summary["schedules"], summary["patrol_cleaned"],
        )
    return summary


def main() -> None:
    """CLI 入口：python -m orchestrator.config_migrate [--force]"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="patrol.yaml 旧段一次性迁移")
    parser.add_argument("--force", action="store_true",
                        help="目标文件已存在时也强制从旧段重建（旧段已清理则无效果）")
    args = parser.parse_args()
    summary = run_once(force=args.force)
    print(summary)


if __name__ == "__main__":
    main()
