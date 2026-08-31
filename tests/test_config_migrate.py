"""config_migrate 单元测试 — 旧 patrol.yaml 段 → private + schedules.yaml 一次性迁移。

覆盖（DESIGN_config_credential_refactor_v1.md §9 / Phase 1 验收标准）：
- 拆分迁移：log_pull_sources → connections + pull_sources；5 个 *_schedule → 统一 schedules
- ID 映射保真：pull_source_id 迁移前后一致
- credential_id 归一化：None / "None" / 空 → ssh:<id>（修复 MaxKB 现存 bug）
- 幂等：跑两次结果一致；目标文件已存在则跳过
- patrol.yaml 清理：旧段删除、其余内容（log_sources / dag_run_patrol / dormant_archive）保留
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# 确保 import orchestrator.* 可用（tests/ 无 __init__）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import config_migrate
from orchestrator.log_pull_admin import normalize_credential_id

# 模拟旧 patrol.yaml（含真实迁移涉及的全部 6 个旧段 + 3 个保留段）
OLD_PATROL = """\
# 旧版 patrol.yaml（迁移前）
dag_run_patrol:
  interval_seconds: 60
  stale_threshold_seconds: 1800

dormant_archive:
  enabled: true
  archive_after_days: 3

log_sources:
- id: seeyon
  name: "致远 OA"
  path: ./logs/seeyon
  allow_read: true

log_patrol_schedule:
- workflow_id: log-patrol
  name: "Nginx 日志巡检"
  cron: "0 * * * *"
  inputs:
    log_source_id: nginx
  enabled: true

log_pull_sources:
- id: prod-seeyon
  name: 生产-致远OA
  host: 192.168.1.100
  port: 22
  username: logreader
  auth:
    type: key
    credential_id: ssh:prod-seeyon
    private_key_path: ~/.agentops/ssh/prod-seeyon.key
  remote:
    paths:
    - /data/seeyon/logs/*.log
  local:
    log_source_id: seeyon
  retention:
    local_max_days: 7
  enabled: false
- id: MaxKB
  name: MaxKB
  host: 10.3.75.137
  port: 65520
  username: root
  auth:
    type: password
    credential_id: None
  remote:
    paths:
    - /opt/AI_Agent_Platform/logs/app
  local:
    log_source_id: oa_approval
  retention:
    local_max_days: 7
  enabled: true

log_pull_schedule:
- workflow_id: log-puller
  name: MaxKB工作日一天3次抽取
  cron: 0 9,12,18 * * 1-5
  inputs:
    pull_source_id: MaxKB
  enabled: true

task_patrol_schedule:
- workflow_id: task-patrol
  name: "任务异常巡检"
  cron: "*/10 * * * *"
  inputs:
    check_scope: task
  enabled: false

task_conductor_schedule:
- workflow_id: task-conductor
  name: "任务阶段演进调度"
  cron: "*/15 * * * *"
  inputs:
    dry_run: false
  enabled: true

task_dispatcher_schedule:
- workflow_id: task-dispatcher
  name: "任务自动派发调度"
  cron: "*/5 * * * *"
  inputs:
    project_id: ""
  enabled: true
"""


@pytest.fixture()
def env(tmp_path: Path):
    """构造迁移三件套路径（patrol / private / schedules 全在 tmp 下）。"""
    patrol = tmp_path / "patrol.yaml"
    private = tmp_path / "private" / "log-pull.yaml"
    schedules = tmp_path / "schedules.yaml"
    patrol.write_text(OLD_PATROL, encoding="utf-8")
    return patrol, private, schedules


class TestMigration:
    def test_split_migration(self, env):
        """旧段 → connections + pull_sources + 统一 schedules 三向拆分。"""
        patrol, private, schedules = env
        summary = config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
        )
        assert summary["migrated"] is True
        assert summary["private_written"] is True
        assert summary["schedules_written"] is True
        assert summary["patrol_cleaned"] is True

        # private：2 个 connection + 2 个 pull_source
        pdata = yaml.safe_load(private.read_text(encoding="utf-8"))
        assert len(pdata["connections"]) == 2
        assert len(pdata["pull_sources"]) == 2

        # schedules：5 段合并共 5 条（log_patrol 1 + log_pull 1 + task_patrol 1 + conductor 1 + dispatcher 1）
        sdata = yaml.safe_load(schedules.read_text(encoding="utf-8"))
        assert len(sdata["schedules"]) == 5
        names = [s["name"] for s in sdata["schedules"]]
        assert "Nginx 日志巡检" in names
        assert "MaxKB工作日一天3次抽取" in names
        assert "任务自动派发调度" in names

    def test_credential_id_normalization(self, env):
        """MaxKB 的 credential_id: None → ssh:MaxKB（归一化修 bug 的关键用例）。"""
        patrol, private, schedules = env
        config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
        )
        pdata = yaml.safe_load(private.read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in pdata["connections"]}
        # key 模式显式配置的保持不变（零重录）
        assert by_id["prod-seeyon"]["auth"]["credential_id"] == "ssh:prod-seeyon"
        # password 模式历史 "None"（字符串）→ 归一化为 ssh:MaxKB
        assert by_id["MaxKB"]["auth"]["credential_id"] == "ssh:MaxKB"

    def test_id_mapping_preserved(self, env):
        """pull_source id 沿用原值 → schedule 的 inputs.pull_source_id 不变。"""
        patrol, private, schedules = env
        config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
        )
        pdata = yaml.safe_load(private.read_text(encoding="utf-8"))
        src_ids = {s["id"] for s in pdata["pull_sources"]}
        assert src_ids == {"prod-seeyon", "MaxKB"}
        # connection_id 正确回连
        for s in pdata["pull_sources"]:
            assert s["connection_id"] == s["id"]

        sdata = yaml.safe_load(schedules.read_text(encoding="utf-8"))
        maxkb_sched = next(
            s for s in sdata["schedules"]
            if s["inputs"].get("pull_source_id") == "MaxKB"
        )
        assert maxkb_sched["workflow_id"] == "log-puller"
        assert maxkb_sched["cron"] == "0 9,12,18 * * 1-5"
        assert maxkb_sched["enabled"] is True

    def test_patrol_cleanup_keeps_other_sections(self, env):
        """patrol.yaml 旧段删除，log_sources / dag_run_patrol / dormant_archive 保留。"""
        patrol, private, schedules = env
        config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
        )
        pdata = yaml.safe_load(patrol.read_text(encoding="utf-8"))
        # 旧段全部删除
        for section in list(config_migrate.OLD_SCHEDULE_SECTIONS) + ["log_pull_sources"]:
            assert section not in pdata, f"{section} 应已从 patrol.yaml 删除"
        # 保留段原样
        assert pdata["dag_run_patrol"]["interval_seconds"] == 60
        assert pdata["dormant_archive"]["archive_after_days"] == 3
        assert pdata["log_sources"][0]["id"] == "seeyon"

    def test_idempotent_rerun(self, env):
        """幂等：第二次运行（目标已存在）跳过，三个文件内容不变。"""
        patrol, private, schedules = env
        config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
        )
        private_text = private.read_text(encoding="utf-8")
        schedules_text = schedules.read_text(encoding="utf-8")
        patrol_text = patrol.read_text(encoding="utf-8")

        summary = config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
        )
        assert summary["migrated"] is False
        assert private.read_text(encoding="utf-8") == private_text
        assert schedules.read_text(encoding="utf-8") == schedules_text
        assert patrol.read_text(encoding="utf-8") == patrol_text

    def test_partial_failure_resume(self, env):
        """上次运行只写了 private（patrol 清理失败）→ 本次跳过 private、清理 patrol。"""
        patrol, private, schedules = env
        # 模拟半途失败：private 已存在，patrol 旧段未清理
        config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
            force=True,
        )
        # 手动把旧段写回 patrol（模拟上次失败现场）
        old = yaml.safe_load(OLD_PATROL)
        pdata = yaml.safe_load(patrol.read_text(encoding="utf-8"))
        pdata["log_pull_sources"] = old["log_pull_sources"]
        pdata["log_pull_schedule"] = old["log_pull_schedule"]
        import io
        from ruamel.yaml import YAML
        buf = io.StringIO()
        y = YAML()
        y.width = 4096
        y.dump(pdata, buf)
        patrol.write_text(buf.getvalue(), encoding="utf-8")

        private_before = private.read_text(encoding="utf-8")
        summary = config_migrate.run_once(
            patrol_path=patrol, private_path=private, schedules_path=schedules,
        )
        # private/schedules 已存在 → 跳过重建，只清理 patrol
        assert summary["private_written"] is False
        assert summary["schedules_written"] is False
        assert summary["patrol_cleaned"] is True
        assert private.read_text(encoding="utf-8") == private_before
        pdata = yaml.safe_load(patrol.read_text(encoding="utf-8"))
        assert "log_pull_sources" not in pdata
        assert "log_pull_schedule" not in pdata

    def test_no_old_sections_noop(self, tmp_path: Path):
        """无旧段 → 直接返回不迁移。"""
        patrol = tmp_path / "patrol.yaml"
        patrol.write_text("dag_run_patrol:\n  interval_seconds: 60\n", encoding="utf-8")
        summary = config_migrate.run_once(
            patrol_path=patrol,
            private_path=tmp_path / "private" / "log-pull.yaml",
            schedules_path=tmp_path / "schedules.yaml",
        )
        assert summary["migrated"] is False
        assert not (tmp_path / "private" / "log-pull.yaml").exists()
        assert not (tmp_path / "schedules.yaml").exists()


class TestNormalizeCredentialId:
    """归一化函数的边界（迁移与新 CRUD 共用）。"""

    def test_none_value(self):
        assert normalize_credential_id(None, "maxkb") == "ssh:maxkb"

    def test_string_none(self):
        assert normalize_credential_id("None", "maxkb") == "ssh:maxkb"

    def test_empty_and_blank(self):
        assert normalize_credential_id("", "x") == "ssh:x"
        assert normalize_credential_id("  ", "x") == "ssh:x"

    def test_explicit_kept(self):
        assert normalize_credential_id("ssh:prod-seeyon", "prod-seeyon") == "ssh:prod-seeyon"
        assert normalize_credential_id("custom-id", "x") == "custom-id"
