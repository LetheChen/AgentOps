"""tools/kb_config.py
知识域配置加载器（Single Source of Truth：config/knowledge/domains.yaml）。

三个工具（ingest_source / query_kb / lint_knowledge）+ 一个 API（GET /api/knowledge/domains）
都通过此模块读配置，替代原本在各自模块顶部硬编码的 DOMAIN_MAP。

特性：
- 启动时加载（fail-fast：yaml 缺失或 schema 错误抛 ConfigError）
- 模块级单例 + 缓存（dict，进程内不重读磁盘）
- 提供 reload_domains() 接口（运维手动 reload，热重载 M2 再做 file watch）
- 不与前端耦合：仅暴露 dict + dataclass，不做 HTTP 序列化

参考设计：docs/knowledge-base/DESIGN_knowledge_domains_configurable.md §3.1
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# 默认配置路径：config/knowledge/domains.yaml
DEFAULT_DOMAINS_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "knowledge" / "domains.yaml"
)


class ConfigError(Exception):
    """domains.yaml 配置错误（缺失 / 解析失败 / schema 不合法）"""


@dataclass(frozen=True)
class DomainMeta:
    """单个知识域元数据（来自 domains.yaml 的一个 entry）。

    frozen=True 让实例不可变（线程安全 + 防止下游误改）。
    """
    domain_id: str
    display_name: str
    description: str
    kb_root: str                       # 相对项目根，例如 "config/knowledge/weekly-report"
    vault_write_dir: str | None        # 相对 vault root；None = 不写 vault
    schema: str                        # llm_wiki | video_production
    categories: list[str] = field(default_factory=list)
    category_layout: dict[str, str] = field(default_factory=dict)
    supports_lint: bool = False
    bound_agents: list[str] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class DomainsConfig:
    """整个 domains.yaml 解析结果。"""
    vault_root: str
    write_whitelist: list[str]
    domains: dict[str, DomainMeta]    # key = domain_id
    source_path: str                  # yaml 绝对路径，方便排查错误


# ====== 模块级单例 + 缓存 ======
_lock = threading.Lock()
_cached: DomainsConfig | None = None
_loaded_from: Path | None = None


def _parse_yaml(path: Path) -> DomainsConfig:
    """解析 yaml 文件 → DomainsConfig。失败抛 ConfigError。"""
    if not path.exists():
        raise ConfigError(f"domains.yaml 不存在: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"domains.yaml YAML 解析失败: {e}") from e

    # 1. vault_root（必填）
    vault_root = raw.get("vault_root", "")
    if not vault_root:
        raise ConfigError("domains.yaml 缺少 vault_root 字段")

    # 2. write_whitelist（必填，可为空列表）
    write_whitelist = raw.get("write_whitelist", [])
    if not isinstance(write_whitelist, list):
        raise ConfigError("write_whitelist 必须是 list")

    # 3. domains（必填，非空 dict）
    domains_raw = raw.get("domains", {})
    if not isinstance(domains_raw, dict) or not domains_raw:
        raise ConfigError("domains 字段必须是非空 dict")

    parsed: dict[str, DomainMeta] = {}
    for domain_id, cfg in domains_raw.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"domain '{domain_id}' 必须是 dict")

        # 必填字段
        kb_root = cfg.get("kb_root")
        if not kb_root:
            raise ConfigError(f"domain '{domain_id}' 缺少 kb_root")

        schema = cfg.get("schema")
        if schema not in ("llm_wiki", "video_production"):
            raise ConfigError(
                f"domain '{domain_id}' schema 必须是 llm_wiki | video_production，实际: {schema}"
            )

        # 可选字段（带默认值）
        parsed[domain_id] = DomainMeta(
            domain_id=domain_id,
            display_name=cfg.get("display_name", domain_id),
            description=cfg.get("description", ""),
            kb_root=kb_root,
            vault_write_dir=cfg.get("vault_write_dir"),
            schema=schema,
            categories=cfg.get("categories", []),
            category_layout=cfg.get("category_layout", {}),
            supports_lint=cfg.get("supports_lint", False),
            bound_agents=cfg.get("bound_agents", []),
            note=cfg.get("note", ""),
        )

    return DomainsConfig(
        vault_root=vault_root,
        write_whitelist=write_whitelist,
        domains=parsed,
        source_path=str(path),
    )


def load_domains(config_path: str | Path | None = None) -> DomainsConfig:
    """加载 domains.yaml（带缓存）。返回 DomainsConfig。

    首次调用：从磁盘读 + 缓存
    后续调用：返回缓存（除非传 config_path 强制重读）

    Args:
        config_path: 可选，指定 yaml 路径（不传用默认 DEFAULT_DOMAINS_PATH）

    Returns:
        DomainsConfig 实例

    Raises:
        ConfigError: yaml 缺失 / 解析失败 / schema 不合法
    """
    global _cached, _loaded_from

    target = Path(config_path) if config_path else DEFAULT_DOMAINS_PATH

    with _lock:
        if _cached is not None and _loaded_from == target:
            return _cached
        _cached = _parse_yaml(target)
        _loaded_from = target
        logger.info("Loaded domains.yaml from %s (%d domains)", target, len(_cached.domains))
        return _cached


def reload_domains() -> DomainsConfig:
    """强制重新读 yaml（运维手动 reload 用，不做 file watch）。

    清空缓存后再调 load_domains()，一定走磁盘。
    """
    global _cached, _loaded_from
    with _lock:
        _cached = None
        _loaded_from = None
    return load_domains()


def get_domain_meta(domain_id: str) -> DomainMeta | None:
    """取单个 domain 的元数据，未找到返回 None。

    自动触发 load_domains()（首次调用时缓存）。
    """
    cfg = load_domains()
    return cfg.domains.get(domain_id)


def list_domain_ids() -> list[str]:
    """所有 domain id 列表。"""
    return list(load_domains().domains.keys())


def list_llm_wiki_domains() -> list[DomainMeta]:
    """过滤 schema=llm_wiki 的 domain（ingest_source / query_kb / lint_knowledge 只服务 llm_wiki）。

    video_production 走专用 query_knowledge.py（异构 schema），不混入。
    """
    return [d for d in load_domains().domains.values() if d.schema == "llm_wiki"]


def list_lintable_domains() -> list[DomainMeta]:
    """过滤 supports_lint=true 的 domain（前端仪表盘用 + lint 路由校验）。"""
    return [d for d in load_domains().domains.values() if d.supports_lint]


def get_domain_map() -> dict[str, str]:
    """返回 {domain_id: dir_name}，替代原硬编码 DOMAIN_MAP。

    例如 {"weekly-report": "weekly-report", "proposal-planning": "proposal-planning", ...}
    仅对 llm_wiki schema 的 domain 生成（video_production 不在原 DOMAIN_MAP）。

    这个函数是给三个工具最小改动用的——它们原本 import `DOMAIN_MAP` 这个 dict，
    现在改成调用这个函数，保持向后兼容。
    """
    return {d.domain_id: Path(d.kb_root).name for d in list_llm_wiki_domains()}


def resolve_kb_root(domain_id: str) -> Path:
    """domain_id → config/knowledge/<kb_root> 的绝对路径。

    用于 ingest_source 写入路径计算（替代原来 `KB_ROOT / DOMAIN_MAP[domain]`）。

    Args:
        domain_id: domain id（如 "weekly-report"）

    Returns:
        绝对路径（已存在或不存在均可，调用方决定是否创建）

    Raises:
        ConfigError: 未知 domain
    """
    meta = get_domain_meta(domain_id)
    if meta is None:
        raise ConfigError(f"未知 domain '{domain_id}'")
    project_root = Path(__file__).resolve().parent.parent
    return project_root / meta.kb_root