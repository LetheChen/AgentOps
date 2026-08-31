"""Provider 默认元数据 — 系统知识，用户可在 models.yaml 覆盖。"""
from __future__ import annotations

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "minimax": {
        "protocol": "openai_compatible",
        "auth_type": "bearer",
        "base_url": "https://api.minimaxi.com/v1",
    },
    "anthropic": {
        "protocol": "anthropic_compatible",
        "auth_type": "x-api-key",
        "base_url": "https://api.anthropic.com/v1",
    },
    "deepseek": {
        "protocol": "openai_compatible",
        "auth_type": "bearer",
        "base_url": "https://api.deepseek.com/v1",
    },
    "kimi": {
        "protocol": "openai_compatible",
        "auth_type": "bearer",
        "base_url": "https://api.moonshot.cn/v1",
    },
    "openai": {
        "protocol": "openai_compatible",
        "auth_type": "bearer",
        "base_url": "https://api.openai.com/v1",
    },
    "glm": {
        "protocol": "openai_compatible",
        "auth_type": "bearer",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "火山方舟": {
        "protocol": "openai_compatible",
        "auth_type": "bearer",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
}


def get_provider_defaults(provider_id: str) -> dict[str, str]:
    """返回 provider 默认 protocol/auth_type/base_url。"""
    return dict(PROVIDER_DEFAULTS.get(provider_id, {}))
