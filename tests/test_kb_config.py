"""tools/kb_config.py 单元测试。

覆盖 11 个用例：
- 正常路径：默认加载、单域查询、列表过滤、向后兼容、路径解析
- 异常路径：yaml 缺失、schema 非法、未知 domain、reload 行为

参考设计：docs/knowledge-base/DESIGN_knowledge_domains_configurable.md §3.4
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# 把项目根加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools import kb_config  # noqa: E402  · 项目惯例 sys.path.insert 后 import
from tools.kb_config import ConfigError, DomainMeta, DomainsConfig, load_domains, reload_domains  # noqa: E402


# ====== 正常路径 ======

def test_load_default_domains():
    """加载默认 config/knowledge/domains.yaml，应成功 + 5 domain。"""
    cfg = load_domains()
    assert isinstance(cfg, DomainsConfig)
    assert len(cfg.domains) == 5
    assert set(cfg.domains.keys()) == {
        "weekly-report", "proposal-planning", "content-curation",
        "video-production", "smart-query",
    }
    # vault_root 和 write_whitelist 应正确加载
    assert cfg.vault_root == "E:\\Document"
    assert isinstance(cfg.write_whitelist, list)
    assert len(cfg.write_whitelist) >= 5


def test_get_domain_meta():
    """取单个 domain 元数据。"""
    meta = kb_config.get_domain_meta("weekly-report")
    assert isinstance(meta, DomainMeta)
    assert meta.domain_id == "weekly-report"
    assert meta.display_name == "周报助手知识库"
    assert meta.description != ""
    assert meta.schema == "llm_wiki"
    assert meta.supports_lint is True
    assert meta.kb_root == "config/knowledge/weekly-report"
    assert meta.vault_write_dir == "Weekly"
    assert "patterns" in meta.categories


def test_get_domain_meta_video_production():
    """video-production 是异构 schema，应正确识别。"""
    meta = kb_config.get_domain_meta("video-production")
    assert meta is not None
    assert meta.schema == "video_production"
    assert meta.supports_lint is False
    assert meta.vault_write_dir is None  # 不写 vault
    assert "references" in meta.categories


def test_get_domain_meta_unknown():
    """未知 domain 返回 None，不抛异常。"""
    assert kb_config.get_domain_meta("nonexistent") is None


def test_list_domain_ids():
    """返回所有 domain id 列表。"""
    ids = kb_config.list_domain_ids()
    assert len(ids) == 5
    assert "weekly-report" in ids


def test_list_llm_wiki_domains():
    """过滤 schema=llm_wiki，应返回 4 个（不含 video_production）。"""
    llm_wiki = kb_config.list_llm_wiki_domains()
    assert len(llm_wiki) == 4
    assert all(d.schema == "llm_wiki" for d in llm_wiki)
    domain_ids = {d.domain_id for d in llm_wiki}
    assert domain_ids == {"weekly-report", "proposal-planning", "content-curation", "smart-query"}


def test_list_lintable_domains():
    """过滤 supports_lint=true，应返回 3 个（不含 video_production）。"""
    lintable = kb_config.list_lintable_domains()
    assert len(lintable) == 3
    assert all(d.supports_lint for d in lintable)


def test_get_domain_map_backward_compat():
    """get_domain_map 返回 {id: dir_name}，仅 llm_wiki domain。"""
    m = kb_config.get_domain_map()
    assert m == {
        "weekly-report": "weekly-report",
        "proposal-planning": "proposal-planning",
        "content-curation": "content-curation",
        "smart-query": "smart-query",
    }
    # video-production 不在（向后兼容原 DOMAIN_MAP 行为）
    assert "video-production" not in m


def test_resolve_kb_root():
    """resolve_kb_root 返回绝对路径，且已存在。"""
    p = kb_config.resolve_kb_root("weekly-report")
    assert p.is_absolute()
    assert p.name == "weekly-report"
    assert p.exists()  # 已初始化


def test_resolve_kb_root_unknown():
    """未知 domain 抛 ConfigError。"""
    with pytest.raises(ConfigError, match="未知 domain"):
        kb_config.resolve_kb_root("nonexistent")


def test_reload_domains():
    """reload_domains 强制重读（不依赖 file watch）。

    清空缓存后再调 load_domains()，新对象但内容相同。
    """
    cfg1 = load_domains()
    cfg2 = reload_domains()
    # reload 后是新对象（缓存已清空）
    assert cfg1 is not cfg2
    # 但内容相同（磁盘文件未改）
    assert cfg1.source_path == cfg2.source_path
    assert set(cfg1.domains.keys()) == set(cfg2.domains.keys())
    # 注意：frozen dataclass 内嵌的 list 不可直接比较，所以只比较顶层


# ====== 异常路径 ======

def test_missing_yaml_raises(tmp_path):
    """yaml 不存在抛 ConfigError。"""
    with pytest.raises(ConfigError, match="domains.yaml 不存在"):
        load_domains(tmp_path / "nonexistent.yaml")


def test_invalid_yaml_syntax_raises(tmp_path):
    """yaml 语法错误抛 ConfigError。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("invalid: yaml: : :\n  - broken", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML 解析失败"):
        load_domains(bad)


def test_missing_vault_root_raises(tmp_path):
    """vault_root 字段缺失抛 ConfigError。"""
    bad = tmp_path / "no_vault.yaml"
    bad.write_text(yaml.dump({
        "domains": {"x": {"kb_root": "x", "schema": "llm_wiki"}}
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="缺少 vault_root"):
        load_domains(bad)


def test_invalid_schema_raises(tmp_path):
    """schema 字段非法抛 ConfigError。"""
    bad = tmp_path / "bad_schema.yaml"
    bad.write_text(yaml.dump({
        "vault_root": "E:\\Document",
        "domains": {"x": {"kb_root": "x", "schema": "wrong_value"}}
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="schema 必须是"):
        load_domains(bad)


def test_empty_domains_raises(tmp_path):
    """domains 字段为空抛 ConfigError。"""
    bad = tmp_path / "empty.yaml"
    bad.write_text(yaml.dump({"vault_root": "E:\\Document", "domains": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="domains 字段必须是非空"):
        load_domains(bad)


def test_missing_kb_root_raises(tmp_path):
    """domain 缺少 kb_root 抛 ConfigError。"""
    bad = tmp_path / "no_kb_root.yaml"
    bad.write_text(yaml.dump({
        "vault_root": "E:\\Document",
        "domains": {"x": {"schema": "llm_wiki"}}
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="缺少 kb_root"):
        load_domains(bad)


def test_optional_fields_defaults():
    """domain 可选字段有默认值（display_name fallback to domain_id 等）。"""
    # 用自定义 yaml 测试
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.dump({
            "vault_root": "C:\\Test",
            "domains": {
                "minimal-domain": {
                    "kb_root": "kb/minimal",
                    "schema": "llm_wiki",
                }
            }
        }, f, allow_unicode=True)
        tmp_path = Path(f.name)
    try:
        cfg = load_domains(tmp_path)
        meta = cfg.domains["minimal-domain"]
        assert meta.display_name == "minimal-domain"  # fallback
        assert meta.description == ""
        assert meta.vault_write_dir is None
        assert meta.categories == []
        assert meta.category_layout == {}
        assert meta.supports_lint is False
        assert meta.bound_agents == []
        assert meta.note == ""
    finally:
        tmp_path.unlink()