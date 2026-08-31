"""schedules_admin 单元测试 — 统一计划 CRUD + cron 校验 + next_run + 级联删除。

覆盖（DESIGN_config_credential_refactor_v1.md §6.2 / Phase 1g 验收标准）：
- next_cron_run：分钟级暴力前搜的确定性用例（固定 from_dt）
- upsert：id 不可变主键、name 可任意改；同名不再被覆盖而是生成 -N 后缀
- slug：name 派生 id 的稳定性、冲突处理、特殊字符
- 校验失败：空 name / 空 workflow_id / 非法 cron（字段数、越界值）/ id 不存在
- delete：按 id 删除、id 缺失兜底按 name、删除不存在报错
- delete_schedules_by_pull_source：拉取源删除时的级联清理
- ensure_ids：懒迁移为缺 id 条目补 id
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

# 确保 import orchestrator.* 可用（tests/ 无 __init__）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.schedules_admin import (
    SchedulesConfigError,
    _slugify_id,
    delete_schedule,
    delete_schedules_by_pull_source,
    ensure_ids,
    list_schedules,
    next_cron_run,
    upsert_schedule,
)


@pytest.fixture()
def sched_path(tmp_path: Path) -> Path:
    return tmp_path / "schedules.yaml"


# ====== next_cron_run（确定性：固定 from_dt）======

class TestNextCronRun:
    def test_every_minute(self):
        dt = datetime(2026, 8, 22, 10, 30)
        assert next_cron_run("* * * * *", from_dt=dt) == datetime(2026, 8, 22, 10, 31)

    def test_daily_at_nine(self):
        dt = datetime(2026, 8, 22, 10, 30)
        assert next_cron_run("0 9 * * *", from_dt=dt) == datetime(2026, 8, 23, 9, 0)

    def test_step_ten_minutes(self):
        dt = datetime(2026, 8, 22, 10, 59)
        assert next_cron_run("*/10 * * * *", from_dt=dt) == datetime(2026, 8, 22, 11, 0)

    def test_same_minute_excluded(self):
        """from_dt 所在分钟不计入（下一次从 +1 分钟开始搜）。"""
        dt = datetime(2026, 8, 22, 10, 0)
        assert next_cron_run("0 * * * *", from_dt=dt) == datetime(2026, 8, 22, 11, 0)

    def test_weekday_only(self):
        """周五 18:05 之后 → 下周一 09:00（周六日跳过）。"""
        dt = datetime(2026, 8, 21, 18, 5)  # 2026-08-21 是周五
        assert next_cron_run("0 9 * * 1-5", from_dt=dt) == datetime(2026, 8, 24, 9, 0)


# ====== upsert / list ======

class TestUpsert:
    def test_create_and_list(self, sched_path):
        upsert_schedule({
            "name": "测试计划",
            "workflow_id": "log-patrol",
            "cron": "0 9 * * *",
            "inputs": {"log_source_id": "seeyon"},
            "enabled": True,
        }, path=sched_path)
        items = list_schedules(path=sched_path)
        assert len(items) == 1
        assert items[0]["name"] == "测试计划"
        assert items[0]["workflow_id"] == "log-patrol"
        assert items[0]["enabled"] is True
        assert items[0]["inputs"] == {"log_source_id": "seeyon"}
        # 中文名 slug 后取不到 ASCII 片段 → 走 fallback "schedule"，首字符非字母/下划线则前置 s_
        assert items[0]["id"] == "schedule"
        assert items[0]["next_run"] is not None  # enabled 且 cron 合法

    def test_update_by_id_overwrites(self, sched_path):
        """id 是唯一键：带同一 id 再次 upsert → 覆盖；name 可变。"""
        upsert_schedule({"id": "p1", "name": "n1", "workflow_id": "w1", "cron": "0 9 * * *"},
                         path=sched_path)
        upsert_schedule({"id": "p1", "name": "n1-renamed", "workflow_id": "w2", "cron": "0 10 * * *"},
                         path=sched_path)
        items = list_schedules(path=sched_path)
        assert len(items) == 1  # id 唯一键
        assert items[0]["name"] == "n1-renamed"  # name 跟随更新
        assert items[0]["workflow_id"] == "w2"
        assert items[0]["cron"] == "0 10 * * *"

    def test_same_name_creates_distinct_ids(self, sched_path):
        """同名重复创建 → 各自落到独立 id（不再按 name 覆盖）。"""
        upsert_schedule({"name": "p1", "workflow_id": "w1", "cron": "0 9 * * *"}, path=sched_path)
        upsert_schedule({"name": "p1", "workflow_id": "w2", "cron": "0 10 * * *"}, path=sched_path)
        items = list_schedules(path=sched_path)
        assert len(items) == 2
        assert {it["id"] for it in items} == {"p1", "p1-2"}

    def test_enabled_defaults_true(self, sched_path):
        upsert_schedule({"name": "p1", "workflow_id": "w", "cron": "* * * * *"}, path=sched_path)
        assert list_schedules(path=sched_path)[0]["enabled"] is True

    def test_disabled_has_no_next_run(self, sched_path):
        upsert_schedule(
            {"name": "p1", "workflow_id": "w", "cron": "0 9 * * *", "enabled": False},
            path=sched_path,
        )
        items = list_schedules(path=sched_path)
        assert items[0]["enabled"] is False
        assert items[0]["next_run"] is None

    def test_missing_file_lists_empty(self, sched_path):
        assert list_schedules(path=sched_path) == []


# ====== slug 规则 ======

class TestSlug:
    def test_basic_slug(self):
        assert _slugify_id("hello-world", set()) == "hello-world"

    def test_chinese_only_falls_back_to_schedule(self):
        # 纯中文/纯标点取不到 [A-Za-z0-9_-] 片段 → 回落 "schedule"
        assert _slugify_id("致远日志巡检", set()) == "schedule"
        assert _slugify_id("!!!", set()) == "schedule"

    def test_mixed_keeps_ascii_token(self):
        # 中英混合：抽 ASCII 片段作为 id
        assert _slugify_id("致远 OA 日志巡检", set()) == "OA"

    def test_digit_prefix_gets_s_prefix(self):
        assert _slugify_id("123abc", set()) == "s_123abc"

    def test_collision_appends_suffix(self):
        assert _slugify_id("p", {"p"}) == "p-2"
        assert _slugify_id("p", {"p", "p-2"}) == "p-3"

    def test_empty_name_uses_schedule(self):
        assert _slugify_id("", set()) == "schedule"


# ====== upsert 校验 ======

class TestUpsertValidation:
    def test_empty_name(self, sched_path):
        with pytest.raises(SchedulesConfigError):
            upsert_schedule({"name": "", "workflow_id": "w", "cron": "* * * * *"}, path=sched_path)

    def test_empty_workflow_id(self, sched_path):
        with pytest.raises(SchedulesConfigError):
            upsert_schedule({"name": "p", "workflow_id": "", "cron": "* * * * *"}, path=sched_path)

    def test_cron_wrong_field_count(self, sched_path):
        with pytest.raises(SchedulesConfigError):
            upsert_schedule({"name": "p", "workflow_id": "w", "cron": "* * *"}, path=sched_path)

    def test_cron_out_of_range(self, sched_path):
        with pytest.raises(SchedulesConfigError):
            upsert_schedule({"name": "p", "workflow_id": "w", "cron": "61 * * * *"}, path=sched_path)

    def test_cron_garbage(self, sched_path):
        with pytest.raises(SchedulesConfigError):
            upsert_schedule({"name": "p", "workflow_id": "w", "cron": "not-a-cron"}, path=sched_path)

    def test_inputs_must_be_dict(self, sched_path):
        with pytest.raises(SchedulesConfigError):
            upsert_schedule(
                {"name": "p", "workflow_id": "w", "cron": "* * * * *", "inputs": ["not", "dict"]},
                path=sched_path,
            )

    def test_invalid_not_written(self, sched_path):
        """校验失败不落盘。"""
        with pytest.raises(SchedulesConfigError):
            upsert_schedule({"name": "p", "workflow_id": "w", "cron": "bad"}, path=sched_path)
        assert not sched_path.exists()

    def test_explicit_id_creates_new_entry(self, sched_path):
        """带 id 且不存在 → 按指定 id 新增（用于「指定 id 复制计划」场景）。"""
        upsert_schedule(
            {"id": "ghost", "name": "p", "workflow_id": "w", "cron": "* * * * *"},
            path=sched_path,
        )
        items = list_schedules(path=sched_path)
        assert len(items) == 1
        assert items[0]["id"] == "ghost"

    def test_id_none_falls_back_to_slug(self, sched_path):
        """Pydantic model_dump 对未传字段也会输出 None（默认值）；视同未指定，走 slug。"""
        upsert_schedule(
            {"id": None, "name": "测试计划A", "workflow_id": "w", "cron": "* * * * *"},
            path=sched_path,
        )
        items = list_schedules(path=sched_path)
        assert items[0]["id"] == "A"  # 从 name 抽到 ASCII token "A"
        assert items[0]["id"] != "None"  # 不能字面落到字符串 "None"


# ====== delete ======

class TestDelete:
    def test_delete_existing(self, sched_path):
        upsert_schedule({"name": "p1", "workflow_id": "w", "cron": "* * * * *"}, path=sched_path)
        r = delete_schedule("p1", path=sched_path)
        assert r == {"id": "p1", "status": "deleted"}
        assert list_schedules(path=sched_path) == []

    def test_delete_missing_raises(self, sched_path):
        with pytest.raises(SchedulesConfigError):
            delete_schedule("nope", path=sched_path)

    def test_delete_one_keeps_others(self, sched_path):
        upsert_schedule({"name": "a", "workflow_id": "w", "cron": "* * * * *"}, path=sched_path)
        upsert_schedule({"name": "b", "workflow_id": "w", "cron": "* * * * *"}, path=sched_path)
        delete_schedule("a", path=sched_path)
        assert [s["name"] for s in list_schedules(path=sched_path)] == ["b"]

    def test_delete_falls_back_to_name_when_id_missing(self, sched_path):
        """兼容历史 yaml：缺 id 条目按 name 兜底删除。"""
        sched_path.write_text(
            "schedules:\n- workflow_id: w\n  name: legacy\n  cron: '* * * * *'\n",
            encoding="utf-8",
        )
        delete_schedule("legacy", path=sched_path)
        assert list_schedules(path=sched_path) == []


# ====== 拉取源级联删除 ======

class TestCascadeDelete:
    def test_delete_by_pull_source(self, sched_path):
        upsert_schedule(
            {"name": "a", "workflow_id": "log-puller", "cron": "* * * * *",
             "inputs": {"pull_source_id": "MaxKB"}},
            path=sched_path,
        )
        upsert_schedule(
            {"name": "b", "workflow_id": "log-puller", "cron": "* * * * *",
             "inputs": {"pull_source_id": "prod-seeyon"}},
            path=sched_path,
        )
        upsert_schedule(
            {"name": "c", "workflow_id": "log-patrol", "cron": "* * * * *",
             "inputs": {"log_source_id": "seeyon"}},
            path=sched_path,
        )
        removed = delete_schedules_by_pull_source("MaxKB", path=sched_path)
        assert removed == 1
        assert [s["name"] for s in list_schedules(path=sched_path)] == ["b", "c"]

    def test_no_match_returns_zero_no_write(self, sched_path):
        upsert_schedule({"name": "a", "workflow_id": "w", "cron": "* * * * *"}, path=sched_path)
        before = sched_path.read_text(encoding="utf-8")
        assert delete_schedules_by_pull_source("nothing", path=sched_path) == 0
        assert sched_path.read_text(encoding="utf-8") == before  # 无删除不回写


# ====== 懒迁移 ======

class TestEnsureIds:
    def test_fills_missing_ids(self, sched_path):
        """懒迁移：为缺 id 的条目按 name slug 补 id，已有 id 不覆盖。"""
        sched_path.write_text(
            "schedules:\n"
            "- workflow_id: w\n  name: legacy-no-id\n  cron: '* * * * *'\n"
            "- id: explicit\n  workflow_id: w\n  name: legacy-with-id\n  cron: '* * * * *'\n",
            encoding="utf-8",
        )
        filled = ensure_ids(path=sched_path)
        assert filled == 1
        items = list_schedules(path=sched_path)
        ids = {it["id"] for it in items}
        assert "explicit" in ids  # 已有的不覆盖
        assert "legacy-no-id" in ids  # 缺的补齐

    def test_no_op_when_all_have_id(self, sched_path):
        upsert_schedule({"id": "x", "name": "n", "workflow_id": "w", "cron": "* * * * *"},
                         path=sched_path)
        before = sched_path.read_text(encoding="utf-8")
        assert ensure_ids(path=sched_path) == 0
        assert sched_path.read_text(encoding="utf-8") == before  # 不落盘