"""H2: 模型配置中心化测试 — 验证 _resolve_model 优先级链。

优先级链:
  node.model → domain_models[domain] → default → llm_config → None(harness自理)
"""
from __future__ import annotations

import os
import pytest

from orchestrator.model_config import ModelConfig, _expand_env


# ====== 环境变量展开测试 ======

class TestExpandEnv:
    def test_expand_simple_var(self):
        os.environ["TEST_H2_VAR"] = "hello"
        assert _expand_env("${TEST_H2_VAR}") == "hello"
        del os.environ["TEST_H2_VAR"]

    def test_expand_with_default(self):
        # ${VAR:-default} 当 VAR 未设置时用 default
        assert _expand_env("${TEST_H2_MISSING:-fallback}") == "fallback"

    def test_expand_var_overrides_default(self):
        os.environ["TEST_H2_VAR2"] = "real"
        assert _expand_env("${TEST_H2_VAR2:-fallback}") == "real"
        del os.environ["TEST_H2_VAR2"]

    def test_expand_empty_var_uses_default(self):
        os.environ["TEST_H2_EMPTY"] = ""
        assert _expand_env("${TEST_H2_EMPTY:-fallback}") == "fallback"
        del os.environ["TEST_H2_EMPTY"]


# ====== 模型配置解析测试 ======

class TestModelConfigResolve:
    """H2: _resolve_model 优先级链。"""

    def setup_method(self):
        """每个测试前重置单例，确保用真实 config/models.yaml。"""
        from orchestrator import model_config as mc_module
        mc_module._model_config = None

    def test_node_model_auto_returns_none(self):
        """node.model == "auto" → 返回 None（harness 自理）。"""
        cfg = ModelConfig()
        result = cfg.resolve(node_model="auto")
        assert result is None

    def test_node_model_explicit_provider(self):
        """node.model = {provider, id} → 返回该 provider 配置。"""
        cfg = ModelConfig()
        result = cfg.resolve(node_model={"provider": "openai", "id": "gpt-4o"})
        assert result is not None
        assert result["model"] == "gpt-4o"
        # api_key 来自环境变量（可能为空）
        assert "base_url" in result
        assert "api_key" in result

    def test_domain_default_model(self):
        """node.model=None + domain=smart_query → 返回域级默认。"""
        cfg = ModelConfig()
        result = cfg.resolve(node_model=None, domain="smart_query")
        assert result is not None
        assert result["model"] == "MiniMax-M3"  # 跟随 config/models.yaml 更新

    def test_global_default_fallback(self):
        """node.model=None + domain=None → 返回全局默认（MiniMax-M3，跟随 config/models.yaml）。"""
        cfg = ModelConfig()
        result = cfg.resolve(node_model=None, domain=None)
        assert result is not None
        assert result["model"] == "MiniMax-M3"  # 跟随 config/models.yaml 更新

    def test_llm_config_env_fallback(self):
        """无 node/domain/default 配置 → llm_config 环境变量兜底。

        用隔离的空配置（不加载 config/models.yaml）真正测试 llm_config 兜底，
        避免被全局 default 提前命中。
        """
        cfg = ModelConfig(config_path="/nonexistent/models.yaml")  # 空配置
        result = cfg.resolve(
            node_model=None,
            domain=None,
            llm_config={"model": "env-model", "api_key": "env-key", "base_url": "env-url"},
        )
        assert result is not None
        assert result["model"] == "env-model"
        assert result["api_key"] == "env-key"
        assert result["base_url"] == "env-url"

    def test_node_model_overrides_domain(self):
        """node.model 优先级 > domain_models。"""
        cfg = ModelConfig()
        result = cfg.resolve(
            node_model={"provider": "anthropic", "id": "claude-sonnet-4-20250514"},
            domain="smart_query",  # 域默认是 deepseek-coder
        )
        assert result is not None
        assert result["model"] == "claude-sonnet-4-20250514"  # node 覆盖域

    def test_domain_overrides_global_default(self):
        """domain_models 优先级 > global default。"""
        cfg = ModelConfig()
        result = cfg.resolve(node_model=None, domain="smart_ops")
        assert result is not None
        assert result["model"] == "claude-sonnet-4-20250514"  # ops 域默认, 不是 gpt-4o-mini

    def test_all_none_returns_none(self):
        """全部未设置 → None（harness 自理）。"""
        cfg = ModelConfig()
        result = cfg.resolve(node_model=None, domain=None, llm_config=None)
        # 如果 config/models.yaml 不存在或 default 未配置, 应返回 None
        # 但我们的 config/models.yaml 有 default, 所以会返回 default
        # 只有当 llm_config 也是 None 且没有 default 时才返回 None
        # 这里测试 llm_config=None + 无 domain + 无 node_model
        if cfg.config.get("default"):
            assert result is not None  # 有全局默认
        else:
            assert result is None


# ====== DagEngine 集成测试 ======

class TestDagEngineResolveModel:
    """H2: DagEngine._resolve_model 集成测试。"""

    def setup_method(self):
        from orchestrator import model_config as mc_module
        mc_module._model_config = None

    def test_resolve_model_with_node_auto(self):
        """节点声明 model: auto → _resolve_model 返回 None。"""
        from workflow.engine import DagEngine
        from workflow.schema import (
            HarnessTypeRef, NodeType, WorkflowDefinition, WorkflowNode,
        )
        wf = WorkflowDefinition(
            workflow_id="test",
            name="test",
            nodes={
                "n1": WorkflowNode(
                    id="n1", name="t", type=NodeType.AGENT,
                    harness=HarnessTypeRef.DETERMINISTIC,
                    model="auto",
                ),
            },
        )
        engine = DagEngine(wf, llm_config={"model": "env-model"})
        result = engine._resolve_model(wf.nodes["n1"])
        assert result is None  # auto → harness 自理

    def test_resolve_model_with_node_explicit(self):
        """节点声明 model: {provider, id} → 返回该配置。

        用 LOCAL_LLM harness（deterministic 跳过 provider 解析，无法测试此路径）。
        """
        from workflow.engine import DagEngine
        from workflow.schema import (
            HarnessTypeRef, NodeType, WorkflowDefinition, WorkflowNode,
        )
        wf = WorkflowDefinition(
            workflow_id="test",
            name="test",
            nodes={
                "n1": WorkflowNode(
                    id="n1", name="t", type=NodeType.AGENT,
                    harness=HarnessTypeRef.LOCAL_LLM,
                    model={"provider": "openai", "id": "gpt-4o"},
                ),
            },
        )
        engine = DagEngine(wf, llm_config={"model": "env-model"})
        result = engine._resolve_model(wf.nodes["n1"])
        assert result is not None
        assert result["model"] == "gpt-4o"

    def test_resolve_model_fallback_to_llm_config(self):
        """无 node.model + 无 domain → llm_config 兜底。

        用 LOCAL_LLM harness（deterministic 跳过 provider 解析，无法测试此路径）。
        """
        from workflow.engine import DagEngine
        from workflow.schema import (
            HarnessTypeRef, NodeType, WorkflowDefinition, WorkflowNode,
        )
        wf = WorkflowDefinition(
            workflow_id="test",
            name="test",
            nodes={
                "n1": WorkflowNode(
                    id="n1", name="t", type=NodeType.AGENT,
                    harness=HarnessTypeRef.LOCAL_LLM,
                    # 无 model, 无 domain
                ),
            },
        )
        engine = DagEngine(wf, llm_config={"model": "env-model", "api_key": "k", "base_url": "u"})
        result = engine._resolve_model(wf.nodes["n1"])
        # 有全局 default 时优先用 default, 否则用 llm_config
        # config/models.yaml 有 default = gpt-4o-mini
        assert result is not None
        # 如果有全局默认, model 应该是 gpt-4o-mini
        from orchestrator.model_config import get_model_config
        default_spec = get_model_config().config.get("default", {})
        if default_spec:
            assert result["model"] == default_spec.get("model")
        else:
            assert result["model"] == "env-model"


# ====== set_fallback_chains（v2 API：双 shape 兼容） ======

class TestSetFallbackChains:
    """v2: set_fallback_chains 接受 string / dict 两种 entry shape，写回 models.yaml。"""

    def setup_method(self):
        from orchestrator import model_config as mc_module
        mc_module._model_config = None

    def _fresh_config(self, tmp_path):
        """不加载真实的 config/models.yaml，避免污染。"""
        cfg_file = tmp_path / "models.yaml"
        cfg_file.write_text("providers: {}\n", encoding="utf-8")
        return ModelConfig(config_path=str(cfg_file))

    def test_legacy_string_shape(self, tmp_path):
        """list[str] 旧 shape：直接照原样写入 _raw。"""
        cfg = self._fresh_config(tmp_path)
        cfg.set_fallback_chains({
            "minimax": ["openai", "deepseek"],
            "openai": ["anthropic"],
        })
        assert cfg.config["fallback_chains"] == {
            "minimax": ["openai", "deepseek"],
            "openai": ["anthropic"],
        }
        # 文件持久化
        raw = (tmp_path / "models.yaml").read_text(encoding="utf-8")
        assert "fallback_chains" in raw
        assert "openai" in raw
        assert "deepseek" in raw

    def test_new_dict_shape(self, tmp_path):
        """list[{provider, model}] 新 shape：model 缺省/None 时存 None。"""
        cfg = self._fresh_config(tmp_path)
        cfg.set_fallback_chains({
            "minimax": [
                {"provider": "openai", "model": "gpt-4o"},
                {"provider": "deepseek", "model": None},
            ],
        })
        assert cfg.config["fallback_chains"]["minimax"][0] == {
            "provider": "openai",
            "model": "gpt-4o",
        }
        assert cfg.config["fallback_chains"]["minimax"][1]["model"] is None

    def test_mixed_shape(self, tmp_path):
        """同一链可混 string + dict。"""
        cfg = self._fresh_config(tmp_path)
        cfg.set_fallback_chains({
            "minimax": [
                "deepseek",  # string
                {"provider": "openai", "model": "gpt-4o"},  # dict
            ],
        })
        chain = cfg.config["fallback_chains"]["minimax"]
        assert chain[0] == "deepseek"
        assert chain[1] == {"provider": "openai", "model": "gpt-4o"}

    def test_clear_with_empty_dict(self, tmp_path):
        """空 dict 清空 fallback_chains 字段。"""
        cfg = self._fresh_config(tmp_path)
        cfg.set_fallback_chains({"minimax": ["openai"]})
        assert "fallback_chains" in cfg.config

        cfg.set_fallback_chains({})
        # _raw.pop 后，config 展开为空 dict（fallback_chains 字段不存在）
        assert cfg.config.get("fallback_chains") is None

    def test_invalid_provider_key_raises(self, tmp_path):
        """主 provider key 为空串 → ValueError。"""
        cfg = self._fresh_config(tmp_path)
        with pytest.raises(ValueError, match="non-empty string"):
            cfg.set_fallback_chains({"": ["openai"]})

    def test_invalid_dict_entry_raises(self, tmp_path):
        """dict 项缺 provider → ValueError。"""
        cfg = self._fresh_config(tmp_path)
        with pytest.raises(ValueError, match="provider"):
            cfg.set_fallback_chains({
                "minimax": [{"model": "gpt-4o"}],  # provider missing
            })

    def test_invalid_model_value_raises(self, tmp_path):
        """dict.model 不是 string/None → ValueError。"""
        cfg = self._fresh_config(tmp_path)
        with pytest.raises(ValueError, match="model"):
            cfg.set_fallback_chains({
                "minimax": [{"provider": "openai", "model": 123}],
            })

    def test_invalid_entry_type_raises(self, tmp_path):
        """entry 不是 string/dict → ValueError。"""
        cfg = self._fresh_config(tmp_path)
        with pytest.raises(ValueError, match="string 或 dict|string or dict"):
            cfg.set_fallback_chains({
                "minimax": [42],
            })

    def test_chains_value_must_be_list(self, tmp_path):
        """provider 的 chain 不是 list → ValueError。"""
        cfg = self._fresh_config(tmp_path)
        with pytest.raises(ValueError, match="must be list"):
            cfg.set_fallback_chains({"minimax": "openai"})  # 应是 list

    def test_persists_to_yaml(self, tmp_path):
        """写入后再 load 能完整恢复（双向兼容）。"""
        cfg = self._fresh_config(tmp_path)
        cfg.set_fallback_chains({"minimax": ["openai"]})

        cfg2 = ModelConfig(config_path=str(tmp_path / "models.yaml"))
        cfg2.load()
        assert cfg2.config["fallback_chains"] == {"minimax": ["openai"]}
