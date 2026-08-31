"""log_pull_admin 单元测试 — 连接对象 + 拉取任务 CRUD、引用保护、级联删除。

覆盖（DESIGN_config_credential_refactor_v1.md §6.1 / Phase 1g 验收标准）：
- 连接 CRUD：credential_id 归一化落盘、key 模式默认私钥路径、被引用拒绝删除（409 语义）
- 拉取任务 CRUD：connection_id 必须存在、log_source_id 白名单校验、级联删除统一计划
- pull_logs 读取助手：load_pull_source_with_connection 的存在性判定
- 脱敏与连接参数拼装（_mask_host / _build_connect_kwargs，不真实建连）

全局路径（PATROL_YAML / SCHEDULES_YAML）通过 monkeypatch 指向 tmp，测试不触碰真实配置。
credential_store 用假实现注入，不读写 ~/.agentops。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# 确保 import orchestrator.* 可用（tests/ 无 __init__）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import log_pull_admin, schedules_admin
from orchestrator.log_pull_admin import (
    LogPullConfigError,
    ReferencedConnectionError,
    _build_connect_kwargs,
    _mask_host,
    delete_connection,
    delete_log_source,
    delete_source,
    list_connections,
    list_log_sources_detail,
    list_pull_sources,
    load_pull_source_with_connection,
    upsert_connection,
    upsert_log_source,
    upsert_source,
)

# 白名单 patrol.yaml（仅 log_sources 段，迁移后 patrol 只剩非敏感段）
PATROL_WITH_WHITELIST = """\
log_sources:
- id: seeyon
  name: "致远 OA"
  path: ./logs/seeyon
  allow_read: true
- id: oa_approval
  name: OA 审批日志
  path: ./logs/oa_approval
  allow_read: true

dag_run_patrol:
  interval_seconds: 60
"""


class _FakeCredentialStore:
    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = secrets or {}

    def get(self, credential_id: str) -> str | None:
        return self._secrets.get(credential_id)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """private/patrol/schedules 三件套全部指向 tmp，并注入假 credential_store。"""
    private = tmp_path / "log-pull.yaml"
    patrol = tmp_path / "patrol.yaml"
    sched = tmp_path / "schedules.yaml"
    patrol.write_text(PATROL_WITH_WHITELIST, encoding="utf-8")
    # list_pull_sources / delete_source / upsert_source 内部无参调用这些全局路径
    monkeypatch.setattr(log_pull_admin, "PATROL_YAML", patrol)
    monkeypatch.setattr(schedules_admin, "SCHEDULES_YAML", sched)
    # 假凭据库：MaxKB 有密码，其他无
    import orchestrator.credential_store as cs
    monkeypatch.setattr(
        cs, "get_credential_store",
        lambda: _FakeCredentialStore({"ssh:MaxKB": "s3cret"}),
    )
    return private


def _conn(**kw) -> dict:
    """标准连接对象入参（credential_id=None 触发归一化路径）。"""
    base = {
        "id": "prod-seeyon",
        "name": "生产-致远OA",
        "host": "192.168.1.100",
        "port": 22,
        "username": "logreader",
        "auth_type": "key",
        "credential_id": None,
    }
    base.update(kw)
    return base


def _source(**kw) -> dict:
    """标准拉取任务入参。"""
    base = {
        "id": "prod-seeyon",
        "name": "生产-致远OA拉取",
        "connection_id": "prod-seeyon",
        "remote_paths": ["/data/seeyon/logs/*.log"],
        "local_log_source_id": "seeyon",
        "local_max_days": 7,
        "enabled": True,
    }
    base.update(kw)
    return base


# ====== 连接对象 CRUD ======

class TestConnectionCRUD:
    def test_upsert_normalizes_credential_id(self, env):
        """credential_id=None → 落盘为 ssh:<id>（杜绝 str(None) 历史bug）。"""
        upsert_connection(_conn(), path=env)
        data = yaml.safe_load(env.read_text(encoding="utf-8"))
        assert data["connections"][0]["auth"]["credential_id"] == "ssh:prod-seeyon"

    def test_upsert_update_overwrites(self, env):
        upsert_connection(_conn(), path=env)
        upsert_connection(_conn(host="10.0.0.1"), path=env)
        items = list_connections(path=env)
        assert len(items) == 1
        assert items[0]["host"] == "10.0.0.1"

    def test_key_mode_default_private_key_path(self, env):
        """key 模式未填私钥路径 → 默认 ~/.agentops/ssh/<id>.key。"""
        upsert_connection(_conn(private_key_path=""), path=env)
        data = yaml.safe_load(env.read_text(encoding="utf-8"))
        assert data["connections"][0]["auth"]["private_key_path"] == "~/.agentops/ssh/prod-seeyon.key"

    def test_password_mode_no_key_path(self, env):
        upsert_connection(_conn(auth_type="password"), path=env)
        data = yaml.safe_load(env.read_text(encoding="utf-8"))
        assert "private_key_path" not in data["connections"][0]["auth"]

    def test_list_joins_credential_state(self, env):
        upsert_connection(_conn(id="MaxKB", auth_type="password"), path=env)
        upsert_connection(_conn(id="prod-seeyon"), path=env)
        items = {c["id"]: c for c in list_connections(path=env)}
        assert items["MaxKB"]["credential_present"] is True   # 假库里有 ssh:MaxKB
        assert items["MaxKB"]["credential_id"] == "ssh:MaxKB"
        assert items["prod-seeyon"]["credential_present"] is False  # 假库里没有
        assert items["prod-seeyon"]["port"] == 22

    def test_validations(self, env):
        cases = [
            _conn(id="bad id!"),          # ID 非法字符
            _conn(id=""),                  # ID 为空
            _conn(host=""),                # host 为空
            _conn(port=0),                 # 端口越界（下界）
            _conn(port=65536),             # 端口越界（上界）
            _conn(username=""),            # 用户名为空
            _conn(auth_type="token"),      # 不支持的认证方式
        ]
        for p in cases:
            with pytest.raises(LogPullConfigError):
                upsert_connection(p, path=env)
        assert not env.exists()  # 校验失败不落盘

    def test_delete_connection(self, env):
        upsert_connection(_conn(), path=env)
        r = delete_connection("prod-seeyon", path=env)
        assert r == {"id": "prod-seeyon", "status": "deleted"}
        assert list_connections(path=env) == []

    def test_delete_missing_raises(self, env):
        with pytest.raises(LogPullConfigError):
            delete_connection("nope", path=env)

    def test_delete_referenced_rejected(self, env):
        """被 pull_source 引用的连接拒绝删除（API 层转 409）。"""
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)
        with pytest.raises(ReferencedConnectionError) as ei:
            delete_connection("prod-seeyon", path=env)
        assert ei.value.referenced_by == ["prod-seeyon"]
        # 连接仍在
        assert len(list_connections(path=env)) == 1


# ====== 拉取任务 CRUD ======

class TestSourceCRUD:
    def test_upsert_and_list(self, env):
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)
        items = list_pull_sources(path=env)
        assert len(items) == 1
        s = items[0]
        assert s["id"] == "prod-seeyon"
        assert s["connection_id"] == "prod-seeyon"
        assert s["remote_paths"] == ["/data/seeyon/logs/*.log"]
        assert s["local_log_source_id"] == "seeyon"
        assert s["local_max_days"] == 7
        assert s["enabled"] is True
        # join 连接对象（host 脱敏显示）
        assert s["connection"]["host_masked"] == "192.168.*.*"
        assert s["connection"]["name"] == "生产-致远OA"

    def test_connection_must_exist(self, env):
        with pytest.raises(LogPullConfigError):
            upsert_source(_source(connection_id="ghost"), path=env)

    def test_remote_paths_required(self, env):
        upsert_connection(_conn(), path=env)
        with pytest.raises(LogPullConfigError):
            upsert_source(_source(remote_paths=[]), path=env)
        with pytest.raises(LogPullConfigError):
            upsert_source(_source(remote_paths=["  "]), path=env)  # 纯空白过滤后为空

    def test_local_id_whitelist(self, env):
        """local.log_source_id 必须在 patrol.yaml 白名单内（防路径遍历）。"""
        upsert_connection(_conn(), path=env)
        with pytest.raises(LogPullConfigError):
            upsert_source(_source(local_log_source_id="../../etc"), path=env)

    def test_max_days_minimum(self, env):
        upsert_connection(_conn(), path=env)
        with pytest.raises(LogPullConfigError):
            upsert_source(_source(local_max_days=0), path=env)

    def test_update_overwrites(self, env):
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)
        upsert_source(_source(local_max_days=30), path=env)
        items = list_pull_sources(path=env)
        assert len(items) == 1
        assert items[0]["local_max_days"] == 30

    def test_delete_source_cascades_schedules(self, env):
        """删除拉取源 → 级联删除引用它的统一计划（config/schedules.yaml）。"""
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)
        # 建两条计划：一条引用该源，一条无关
        schedules_admin.upsert_schedule({
            "name": "MaxKB抽取", "workflow_id": "log-puller", "cron": "0 9 * * *",
            "inputs": {"pull_source_id": "prod-seeyon"},
        })
        schedules_admin.upsert_schedule({
            "name": "巡检", "workflow_id": "log-patrol", "cron": "0 8 * * *",
            "inputs": {"log_source_id": "seeyon"},
        })
        r = delete_source("prod-seeyon", path=env)
        assert r["status"] == "deleted"
        assert r["removed_schedules"] == 1
        # 源已删，无关计划保留
        assert list_pull_sources(path=env) == []
        remaining = schedules_admin.list_schedules()
        assert [s["name"] for s in remaining] == ["巡检"]

    def test_delete_missing_source_raises(self, env):
        with pytest.raises(LogPullConfigError):
            delete_source("nope", path=env)

    def test_list_includes_linked_schedules(self, env):
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)
        schedules_admin.upsert_schedule({
            "name": "MaxKB抽取", "workflow_id": "log-puller", "cron": "0 9,12,18 * * 1-5",
            "inputs": {"pull_source_id": "prod-seeyon"}, "enabled": True,
        })
        items = list_pull_sources(path=env)
        linked = items[0]["schedules"]
        assert len(linked) == 1
        assert linked[0]["name"] == "MaxKB抽取"
        assert linked[0]["cron"] == "0 9,12,18 * * 1-5"
        assert linked[0]["next_run"] is not None


# ====== 本地日志目录（log_sources 白名单）CRUD ======

class TestLogSourceCRUD:
    def test_list_detail_includes_refs(self, env):
        """列表含被哪些拉取任务引用。"""
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)  # 引用 seeyon
        items = {d["id"]: d for d in list_log_sources_detail(path=env)}
        assert items["seeyon"]["referenced_by"] == ["prod-seeyon"]
        assert items["oa_approval"]["referenced_by"] == []
        assert items["seeyon"]["path"] == "./logs/seeyon"

    def test_upsert_create_and_update(self, env):
        r = upsert_log_source({"id": "maxkb", "name": "MaxKB", "path": "./logs/maxkb"})
        assert r == {"id": "maxkb", "status": "created"}
        # 更新
        r = upsert_log_source({"id": "maxkb", "name": "MaxKB2", "path": "./logs/maxkb_v2"})
        assert r["status"] == "updated"
        items = {d["id"]: d for d in list_log_sources_detail(path=env)}
        assert items["maxkb"]["name"] == "MaxKB2"
        assert items["maxkb"]["path"] == "./logs/maxkb_v2"
        # 原有条目不受影响
        assert items["seeyon"]["path"] == "./logs/seeyon"

    def test_upsert_preserves_other_sections(self, env):
        """回写 patrol.yaml 不丢非 log_sources 段（dag_run_patrol 等）。"""
        upsert_log_source({"id": "maxkb", "name": "MaxKB", "path": "./logs/maxkb"})
        data = yaml.safe_load(log_pull_admin.PATROL_YAML.read_text(encoding="utf-8"))
        assert data["dag_run_patrol"]["interval_seconds"] == 60
        assert len(data["log_sources"]) == 3

    def test_upsert_validations(self, env):
        with pytest.raises(LogPullConfigError):
            upsert_log_source({"id": "bad id!", "path": "./x"})
        with pytest.raises(LogPullConfigError):
            upsert_log_source({"id": "ok", "path": "  "})

    def test_delete_unreferenced(self, env):
        r = delete_log_source("oa_approval", path=env)
        assert r == {"id": "oa_approval", "status": "deleted"}
        ids = [d["id"] for d in list_log_sources_detail(path=env)]
        assert "oa_approval" not in ids and "seeyon" in ids

    def test_delete_missing_raises(self, env):
        with pytest.raises(LogPullConfigError):
            delete_log_source("nope", path=env)

    def test_delete_referenced_rejected(self, env):
        """被拉取任务引用的目录拒绝删除（引用缺失会导致 pull_logs 加载报错）。"""
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)  # 引用 seeyon
        with pytest.raises(ReferencedConnectionError) as ei:
            delete_log_source("seeyon", path=env)
        assert ei.value.referenced_by == ["prod-seeyon"]
        # 白名单条目仍在
        ids = [d["id"] for d in list_log_sources_detail(path=env)]
        assert "seeyon" in ids


# ====== pull_logs 读取助手 ======

class TestLoadPullSourceWithConnection:
    def test_found(self, env):
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)
        pair = load_pull_source_with_connection("prod-seeyon", path=env)
        assert pair is not None
        src, conn = pair
        assert src["local"]["log_source_id"] == "seeyon"
        assert conn["host"] == "192.168.1.100"

    def test_source_missing(self, env):
        assert load_pull_source_with_connection("nope", path=env) is None

    def test_connection_missing(self, env):
        """源存在但引用的连接被外部删掉 → None（防脏数据半连接）。"""
        upsert_connection(_conn(), path=env)
        upsert_source(_source(), path=env)
        # 外部直接改文件模拟脏数据：删掉连接段
        data = yaml.safe_load(env.read_text(encoding="utf-8"))
        data["connections"] = []
        env.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert load_pull_source_with_connection("prod-seeyon", path=env) is None


# ====== 脱敏与连接参数拼装 ======

class TestMaskHost:
    def test_ipv4(self):
        assert _mask_host("192.168.1.100") == "192.168.*.*"

    def test_long_hostname(self):
        assert _mask_host("prod-log-server") == "pr***"

    def test_short_value_untouched(self):
        assert _mask_host("abc") == "abc"


class TestBuildConnectKwargs:
    def test_key_mode(self, env, tmp_path):
        key = tmp_path / "id_rsa"
        key.write_text("FAKE KEY", encoding="utf-8")
        conn = {
            "host": "10.0.0.1", "port": 22, "username": "root",
            "auth": {"type": "key", "private_key_path": str(key)},
        }
        kwargs, err = _build_connect_kwargs(conn, None)
        assert err is None
        assert kwargs["hostname"] == "10.0.0.1"
        assert kwargs["username"] == "root"
        assert kwargs["key_filename"] == str(key)
        assert "password" not in kwargs

    def test_key_mode_missing_file(self, env):
        conn = {
            "host": "10.0.0.1", "port": 22, "username": "root",
            "auth": {"type": "key", "private_key_path": "/nonexistent/key"},
        }
        kwargs, err = _build_connect_kwargs(conn, None)
        assert err is not None and "私钥文件不存在" in err
        assert kwargs == {}

    def test_password_mode_with_secret(self, env):
        conn = {
            "host": "10.0.0.1", "port": 65520, "username": "root",
            "auth": {"type": "password", "credential_id": "ssh:MaxKB"},
        }
        kwargs, err = _build_connect_kwargs(conn, "s3cret")
        assert err is None
        assert kwargs["password"] == "s3cret"
        assert kwargs["port"] == 65520
        assert "key_filename" not in kwargs

    def test_password_mode_without_secret(self, env):
        conn = {
            "host": "10.0.0.1", "port": 22, "username": "root",
            "auth": {"type": "password", "credential_id": "ssh:ghost"},
        }
        kwargs, err = _build_connect_kwargs(conn, None)
        assert err is not None and "credential_store" in err
        assert kwargs == {}
