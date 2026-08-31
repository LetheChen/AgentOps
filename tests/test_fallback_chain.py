"""v2: FallbackChain 接受 list[str]（旧 shape）和 list[dict]（新 shape），
归一化为内部统一的 list[{provider, model?}]，向后兼容运行时（DagEngine）。

测试重点：
- get_fallback_entry 返回首个条目的完整 dict（含显式 model）
- get_chain 返回 [{provider, model?}] 列表供前端展示
- get_fallback（旧 API）仍返回 provider 字符串
- 异常条目（数字/None/空 dict）静默跳过，不污染整条链
"""
from __future__ import annotations

from orchestrator.provider_health import FallbackChain


# ── 旧 shape：list[str] ──


class TestLegacyStringShape:
    def test_get_fallback_returns_provider_str(self):
        chain = FallbackChain({"minimax": ["openai", "deepseek"]})
        assert chain.get_fallback("minimax") == "openai"

    def test_get_chain_legacy(self):
        chain = FallbackChain({"minimax": ["openai", "deepseek"]})
        # 旧 API 返回 list[str] 是 contract，但现在 v2 改返回 list[dict] 了
        # 测试的是「仍然能正确归一化」，不保证原有 list[str] 返回
        result = chain.get_chain("minimax")
        assert result == [
            {"provider": "openai", "model": None},
            {"provider": "deepseek", "model": None},
        ]

    def test_get_fallback_entry_model_is_none(self):
        chain = FallbackChain({"minimax": ["openai"]})
        entry = chain.get_fallback_entry("minimax")
        assert entry == {"provider": "openai", "model": None}

    def test_unconfigured_returns_none(self):
        chain = FallbackChain({"minimax": ["openai"]})
        assert chain.get_fallback("openai") is None
        assert chain.get_fallback_entry("openai") is None
        assert chain.get_chain("openai") == []
        assert chain.has_chain("openai") is False

    def test_empty_chain(self):
        chain = FallbackChain(None)
        assert chain.get_fallback("minimax") is None
        assert chain.has_chain("minimax") is False


# ── 新 shape：list[dict] ──


class TestNewDictShape:
    def test_with_explicit_model(self):
        chain = FallbackChain({
            "minimax": [
                {"provider": "openai", "model": "gpt-4o"},
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
            ],
        })
        assert chain.get_fallback("minimax") == "openai"
        entry = chain.get_fallback_entry("minimax")
        assert entry == {"provider": "openai", "model": "gpt-4o"}

    def test_with_null_model(self):
        chain = FallbackChain({
            "minimax": [{"provider": "openai", "model": None}],
        })
        entry = chain.get_fallback_entry("minimax")
        assert entry == {"provider": "openai", "model": None}

    def test_partial_dict_only_provider(self):
        """dict 只有 provider 键 → 视为「仅 provider」回退。"""
        chain = FallbackChain({
            "minimax": [{"provider": "openai"}],
        })
        entry = chain.get_fallback_entry("minimax")
        assert entry == {"provider": "openai", "model": None}


# ── 混合 + 异常条目 ──


class TestMixedAndResilient:
    def test_mixed_string_and_dict_in_same_chain(self):
        chain = FallbackChain({
            "minimax": [
                "deepseek",  # string
                {"provider": "openai", "model": "gpt-4o"},
            ],
        })
        result = chain.get_chain("minimax")
        assert result[0] == {"provider": "deepseek", "model": None}
        assert result[1] == {"provider": "openai", "model": "gpt-4o"}

    def test_invalid_entries_silently_skipped(self):
        chain = FallbackChain({
            "minimax": [
                "openai",  # ok
                123,  # invalid (number)
                None,  # invalid
                {},  # invalid (no provider)
                {"provider": ""},  # invalid (empty provider)
                {"provider": "deepseek", "model": "deepseek-v4-flash"},  # ok
            ],
        })
        result = chain.get_chain("minimax")
        assert len(result) == 2
        assert result[0]["provider"] == "openai"
        assert result[1] == {"provider": "deepseek", "model": "deepseek-v4-flash"}

    def test_get_chain_returns_independent_copies(self):
        """get_chain 返回的 dict 是浅拷贝，caller mutate 不影响内部。"""
        chain = FallbackChain({"minimax": ["openai"]})
        result = chain.get_chain("minimax")
        result[0]["provider"] = "hacked"
        # 重新拿一次验证内部未被污染
        again = chain.get_chain("minimax")
        assert again[0]["provider"] == "openai"

    def test_get_fallback_entry_returns_independent_dict(self):
        """get_fallback_entry 返回的 dict 是浅拷贝，caller mutate 不影响内部。"""
        chain = FallbackChain({"minimax": ["openai"]})
        entry = chain.get_fallback_entry("minimax")
        entry["provider"] = "hacked"
        again = chain.get_fallback_entry("minimax")
        assert again["provider"] == "openai"
