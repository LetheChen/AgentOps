"""H2: 模型配置加载器 + 优先级链解析。

优先级链:
  node.model (节点显式覆盖, auto 或 {provider, id})
    ↓ 未设置则
  agent_def.model_config (AgentDefinition 中的模型配置)  [P5 启用]
    ↓ 未设置则
  domain_models[domain] (域级默认模型)                    [P5 启用]
    ↓ 未设置则
  providers.default (全局默认)
    ↓ 未设置则
  llm_config / 环境变量 (向后兼容, 保护现有 workflow)
    ↓ 未设置则
  harness 自理 (model: auto)
"""
from __future__ import annotations

import copy
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from config.provider_catalog import get_provider_defaults, PROVIDER_DEFAULTS

logger = logging.getLogger(__name__)


def _expand_env(value: str) -> str:
    """展开 ${VAR} 和 ${VAR:-default} 语法。"""
    def replacer(match):
        var_name = match.group(1)
        default_val = match.group(2)
        env_val = os.environ.get(var_name, "")
        return env_val if env_val else (default_val or "")
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.+?))?\}", replacer, value)


def _expand_dict(d: Any) -> Any:
    """递归展开 dict/list 中的 ${VAR} 引用。"""
    if isinstance(d, str):
        return _expand_env(d)
    if isinstance(d, dict):
        return {k: _expand_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_expand_dict(v) for v in d]
    return d


class ModelConfig:
    """模型配置加载器 — 从 config/models.yaml 加载并解析, 支持写回。"""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            project_root = Path(__file__).parent.parent
            config_path = str(project_root / "config" / "models.yaml")
        self.config_path = config_path
        self._config: dict[str, Any] = {}
        self._raw: dict[str, Any] = {}           # 原始 YAML（未展开 env），用于写回
        self._loaded = False
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        """加载 models.yaml，展开环境变量。保留 _raw 用于写回。"""
        path = Path(self.config_path)
        if not path.exists():
            self._config = {}
            self._raw = {}
            self._loaded = True
            return self._config

        with open(path, encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {}
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self._loaded = True
        return self._config

    @property
    def config(self) -> dict[str, Any]:
        if not self._loaded:
            self.load()
        return self._config

    def save(self) -> None:
        """将当前配置写回 models.yaml，保留 ${ENV} 引用。"""
        path = Path(self.config_path)
        with self._lock:
            # 写临时文件后原子替换
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.dump(self._raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            tmp_path.replace(path)
            logger.info("models.yaml saved")

    def add_model(self, provider_id: str, model: dict[str, Any]) -> None:
        """向 provider 添加模型。若 provider 不存在则自动创建。"""
        providers = self._raw.setdefault("providers", {})
        if provider_id not in providers:
            # 从 PROVIDER_DEFAULTS 初始化 provider
            defaults = PROVIDER_DEFAULTS.get(provider_id, {})
            providers[provider_id] = {
                "api_key": f"${{{provider_id.upper()}_API_KEY}}",
                "base_url": defaults.get("base_url", ""),
                "protocol": defaults.get("protocol", "openai_compatible"),
                "auth_type": defaults.get("auth_type", "bearer"),
                "models": [],
            }
        prov = providers[provider_id]
        if "models" not in prov:
            prov["models"] = []
        # 去重：已有同 id 的模型则替换
        existing = [m for m in prov["models"] if m.get("id") == model.get("id")]
        if existing:
            idx = prov["models"].index(existing[0])
            prov["models"][idx] = model
        else:
            prov["models"].append(model)
        # 刷新内存中的展开配置
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info("Model '%s/%s' added", provider_id, model.get("id"))

    def remove_model(self, provider_id: str, model_id: str) -> bool:
        """从 provider 移除模型。返回是否成功。"""
        providers = self._raw.get("providers", {})
        prov = providers.get(provider_id)
        if not prov:
            return False
        models = prov.get("models", [])
        to_remove = [m for m in models if m.get("id") == model_id]
        if not to_remove:
            return False
        models.remove(to_remove[0])
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info("Model '%s/%s' removed", provider_id, model_id)
        return True

    def remove_provider(self, provider_id: str) -> bool:
        """删除供应商（含其全部模型）。返回是否成功。

        如果该供应商是 default 或 manager_model，会一并清除引用。
        """
        providers = self._raw.get("providers", {})
        if provider_id not in providers:
            return False
        del providers[provider_id]
        # 清除 default / manager_model 中对该供应商的引用
        default = self._raw.get("default")
        if isinstance(default, dict) and default.get("provider") == provider_id:
            del self._raw["default"]
        manager = self._raw.get("manager_model")
        if isinstance(manager, dict) and manager.get("provider") == provider_id:
            del self._raw["manager_model"]
        # 清除 fallback_chains 中对该供应商的引用
        chains = self._raw.get("fallback_chains", {})
        for domain, chain in list(chains.items()):
            if isinstance(chain, list):
                chains[domain] = [c for c in chain if not (isinstance(c, dict) and c.get("provider") == provider_id)]
                if not chains[domain]:
                    del chains[domain]
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info("Provider '%s' removed (incl. models, default/manager/fallback refs)", provider_id)
        return True

    def update_provider(self, provider_id: str, updates: dict[str, Any]) -> None:
        """更新 provider 配置（base_url, protocol, auth_type 等，不写 api_key）。"""
        providers = self._raw.setdefault("providers", {})
        if provider_id not in providers:
            defaults = PROVIDER_DEFAULTS.get(provider_id, {})
            providers[provider_id] = {
                "api_key": "REPLACE_WITH_YOUR_API_KEY",
                "base_url": defaults.get("base_url", ""),
                "protocol": defaults.get("protocol", "openai_compatible"),
                "auth_type": defaults.get("auth_type", "bearer"),
                "models": [],
            }
        prov = providers[provider_id]
        # 只更新非 api_key、非 models 字段
        for k, v in updates.items():
            if k not in ("api_key", "models"):
                prov[k] = v
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info("Provider '%s' updated: %s", provider_id, list(updates.keys()))

    def set_default_model(self, provider_id: str, model_id: str) -> None:
        """设置全局默认模型。"""
        self._raw["default"] = {"provider": provider_id, "model": model_id}
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info("Default model set to %s/%s", provider_id, model_id)

    def set_manager_model(self, provider_id: str, model_id: str) -> None:
        """设置 Manager 模型。"""
        self._raw["manager_model"] = {"provider": provider_id, "model": model_id}
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info("Manager model set to %s/%s", provider_id, model_id)

    def set_fallback_chains(self, chains: dict[str, list[Any]]) -> None:
        """设置全局 fallback 链，写回 models.yaml 的 fallback_chains 字段。

        Args:
            chains: {provider_id: [entry, ...]}。
                每条 entry 可以是：
                - str: 仅 provider_id，fallback 时用该 provider 默认 model（兼容旧 shape）
                - dict: {provider: str, model: str | None}（model=None 等同旧 shape）

        校验：
        - keys/provider_id 必须是非空字符串，否则 raise ValueError（fail-loud）
        - 空 dict 表示清空 fallback_chains 字段（fail-loud 原则：用户主动清空 = 显式声明"无 fallback"）

        副作用：
        - _raw['fallback_chains'] = chains（保留原始 shape 不归一化，UI 可混合两种）
        - 刷新 _config + save() 原子写入 models.yaml
        - 注意：不主动失效 _fallback_chain 单例（orchestrator.get_fallback_chain 是懒加载，
          下次调用时会重新读 mc.config，符合现有 set_default_model 行为）
        """
        normalized: dict[str, list[Any]] = {}
        for pid, chain in chains.items():
            if not isinstance(pid, str) or not pid:
                raise ValueError(f"fallback_chains key must be non-empty string, got {pid!r}")
            if chain is None:
                continue  # 跳过空 provider
            if not isinstance(chain, list):
                raise ValueError(
                    f"fallback_chains[{pid}] must be list, got {type(chain).__name__}"
                )
            entries: list[Any] = []
            for entry in chain:
                if isinstance(entry, str):
                    if not entry:
                        raise ValueError(f"fallback_chains[{pid}] 中空字符串不允许")
                    entries.append(entry)
                elif isinstance(entry, dict):
                    provider = entry.get("provider")
                    if not isinstance(provider, str) or not provider:
                        raise ValueError(
                            f"fallback_chains[{pid}] dict 项必须含非空 provider, got {entry!r}"
                        )
                    model_val = entry.get("model")
                    if model_val is not None and (not isinstance(model_val, str) or not model_val):
                        raise ValueError(
                            f"fallback_chains[{pid}] dict.model 必须为非空字符串或 null, got {model_val!r}"
                        )
                    # 保留 None — 序列化时会被 yaml.dump 写成 "model: null"
                    entries.append({"provider": provider, "model": model_val})
                else:
                    raise ValueError(
                        f"fallback_chains[{pid}] 项必须是 string 或 dict, got {type(entry).__name__}"
                    )
            normalized[pid] = entries
        if not normalized:
            # 显式清空：删字段而非写空 dict（避免 YAML 出现空的 fallback_chains: {}）
            self._raw.pop("fallback_chains", None)
        else:
            self._raw["fallback_chains"] = normalized
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info(
            "Fallback chains updated: %d chains (%s)",
            len(normalized),
            ", ".join(sorted(normalized.keys())),
        )

    def add_provider(
        self,
        provider_id: str,
        base_url: str,
        protocol: str = "openai_compatible",
        auth_type: str = "bearer",
        api_key_env: str = "",
    ) -> None:
        """创建自定义供应商（如本地 Ollama）。若已存在则更新配置。"""
        providers = self._raw.setdefault("providers", {})
        providers[provider_id] = {
            "api_key": f"${{{api_key_env or (provider_id.upper() + '_API_KEY')}}}",
            "base_url": base_url,
            "protocol": protocol,
            "auth_type": auth_type,
            "models": [],
        }
        self._config = _expand_dict(copy.deepcopy(self._raw))
        self.save()
        logger.info("Provider '%s' created (base_url=%s)", provider_id, base_url)

    def list_all_models(self) -> dict[str, list[dict[str, Any]]]:
        """返回 {provider_id: [model_dict, ...]} 映射。"""
        result: dict[str, list[dict[str, Any]]] = {}
        for pid in PROVIDER_DEFAULTS:
            provider = self.get_provider(pid)
            if provider and provider.get("models"):
                result[pid] = provider["models"]
        return result

    def get_provider(self, provider_name: str) -> dict[str, str] | None:
        """获取提供商配置（api_key + base_url + protocol/auth_type）。"""
        providers = self.config.get("providers", {})
        raw = providers.get(provider_name)
        if not raw:
            return None
        defaults = get_provider_defaults(provider_name)
        merged = {**defaults, **raw}
        return merged

    def _build_result(
        self,
        provider_name: str,
        model_id: str,
        provider: dict[str, Any],
    ) -> dict[str, str]:
        """组装 resolve 返回值，含 protocol/auth_type。

        api_key 优先级链：CredentialStore（前端表单录入）> models.yaml（${ENV} 已展开）。
        与 HealthChecker.check_provider 保持一致（D-028 修复）。
        """
        defaults = get_provider_defaults(provider_name)
        protocol = provider.get("protocol") or defaults.get("protocol", "openai_compatible")
        auth_type = provider.get("auth_type") or defaults.get("auth_type", "bearer")
        base_url = provider.get("base_url") or defaults.get("base_url", "")

        # api_key 优先 CredentialStore（前端表单录入），回退 models.yaml
        api_key = ""
        try:
            from orchestrator.credential_store import get_credential_store
            store = get_credential_store()
            stored = store.get(provider_name)
            if stored:
                api_key = stored
        except Exception as e:
            logger.warning(
                "CredentialStore 查询失败 provider=%s: %s（回退 models.yaml）",
                provider_name, e,
            )
        if not api_key:
            api_key = provider.get("api_key", "")

        return {
            "base_url": base_url,
            "api_key": api_key,
            "model": model_id,
            "provider": provider_name,
            "protocol": protocol,
            "auth_type": auth_type,
        }

    def resolve(
        self,
        node_model: dict | str | None = None,
        agent_model: dict | str | None = None,
        domain: str | None = None,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        """按优先级链解析模型配置。返回 None = harness 自理。"""
        if node_model == "auto" or agent_model == "auto":
            return None

        model_spec = node_model or agent_model

        # 字符串格式 "provider/model"（如 "minimax/MiniMax-M2.7-highspeed"）
        if isinstance(model_spec, str) and "/" in model_spec:
            provider_name, model_id = model_spec.split("/", 1)
            provider = self.get_provider(provider_name)
            if provider and model_id:
                return self._build_result(provider_name, model_id, provider)

        if isinstance(model_spec, dict) and model_spec:
            provider_name = model_spec.get("provider", "")
            model_id = model_spec.get("id") or model_spec.get("model", "")
            provider = self.get_provider(provider_name)
            if provider and model_id:
                return self._build_result(provider_name, model_id, provider)

        if domain:
            domain_models = self.config.get("domain_models", {})
            domain_spec = domain_models.get(domain)
            if isinstance(domain_spec, dict) and domain_spec:
                provider_name = domain_spec.get("provider", "")
                model_id = domain_spec.get("model", "")
                provider = self.get_provider(provider_name)
                if provider and model_id:
                    return self._build_result(provider_name, model_id, provider)

        default_spec = self.config.get("default")
        if isinstance(default_spec, dict) and default_spec:
            provider_name = default_spec.get("provider", "")
            model_id = default_spec.get("model", "")
            provider = self.get_provider(provider_name)
            if provider and model_id:
                return self._build_result(provider_name, model_id, provider)

        if llm_config and llm_config.get("model"):
            model_str = llm_config.get("model", "")
            provider_name = ""
            model_id = model_str
            if "/" in model_str:
                provider_name, model_id = model_str.split("/", 1)
            return {
                "base_url": llm_config.get("base_url", ""),
                "api_key": llm_config.get("api_key", ""),
                "model": model_id,
                "provider": provider_name,
                "protocol": llm_config.get("protocol", "openai_compatible"),
                "auth_type": llm_config.get("auth_type", "bearer"),
            }

        return None

    def get_price(self, provider: str, model: str) -> tuple[float, float]:
        """从 models.yaml 读取 (input_per_1k, output_per_1k) 价格（usd）。"""
        providers = self.config.get("providers", {})
        p = providers.get(provider, {})
        for m in p.get("models", []):
            if m.get("id") == model:
                return (
                    float(m.get("price_input_per_1k", 0.0)),
                    float(m.get("price_output_per_1k", 0.0)),
                )
        return 0.0, 0.0


_model_config: ModelConfig | None = None


def get_model_config() -> ModelConfig:
    """获取全局 ModelConfig 单例。"""
    global _model_config
    if _model_config is None:
        _model_config = ModelConfig()
    return _model_config
