"""P2-3: Provider 健康检查 + Fallback 链。

设计原则：
- 被动检查（不主动轮询）：只在用户点"测试连接"或节点失败时触发
- fail-loud > fail-quiet：只有用户显式配置 fallback_chains 才切换，否则原样报错
- HealthChecker.check_provider 同步实现（httpx.Client），API 层用 asyncio.to_thread 包裹

用法：
    # 健康检查
    checker = HealthChecker()
    result = checker.check_provider("minimax")
    # → {"ok": True, "latency_ms": 120, "error": None}

    # Fallback 链（从 models.yaml 的 fallback_chains 字段读）
    chain = FallbackChain({"minimax": ["openai", "deepseek"]})
    chain.get_fallback("minimax")  # → "openai"
    chain.get_fallback("openai")   # → None（未配置）
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class HealthChecker:
    """被动检查 provider 可用性。

    不主动轮询，只在以下场景触发：
    - 用户点"测试连接"按钮
    - 节点执行失败后诊断
    - 手动调用 check_provider
    """

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout

    def check_provider(self, provider_id: str, mode: str = "api") -> dict[str, Any]:
        """检查 provider 连通性。

        Args:
            provider_id: 供应商 ID
            mode: 检查模式 — "api"（直接 API 调用 GET /models）或 "token"（仅校验凭证存在性和格式）

        返回 {ok: bool, latency_ms: int, error: str|None, mode: str}。
        """
        from orchestrator.credential_store import get_credential_store
        from orchestrator.model_config import get_model_config

        mc = get_model_config()
        provider = mc.get_provider(provider_id)
        if not provider:
            return {"ok": False, "latency_ms": 0, "error": f"未知 provider: {provider_id}", "mode": mode}

        base_url = provider.get("base_url", "")
        if not base_url:
            return {"ok": False, "latency_ms": 0, "error": "缺少 base_url", "mode": mode}

        # api_key 优先 CredentialStore（前端表单录入），回退 models.yaml（${ENV} 已展开）
        store = get_credential_store()
        api_key = store.get(provider_id) or provider.get("api_key", "")

        auth_type = provider.get("auth_type", "bearer")

        # ── Token 模式：仅校验凭证存在性和格式，不发网络请求 ──
        if mode == "token":
            if not api_key:
                return {"ok": False, "latency_ms": 0, "error": "未配置 API Key 凭证", "mode": "token"}
            # 基本格式校验
            if auth_type == "bearer" and not api_key.startswith(("sk-", "Bearer ", "Bearer-")):
                # 不强制前缀，只检查非空长度
                if len(api_key) < 8:
                    return {"ok": False, "latency_ms": 0, "error": "API Key 格式异常（长度不足）", "mode": "token"}
            return {
                "ok": True,
                "latency_ms": 0,
                "error": None,
                "mode": "token",
                "detail": f"凭证已配置（{auth_type}，{len(api_key)} 字符）",
            }

        # ── API 模式：直接调用 GET {base_url}/models ──
        headers = self._build_auth_headers(api_key, auth_type)

        url = f"{base_url.rstrip('/')}/models"
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(url, headers=headers)
            latency_ms = int((time.perf_counter() - start) * 1000)
            if resp.status_code < 400:
                logger.info("HealthCheck %s ok (%dms)", provider_id, latency_ms)
                return {"ok": True, "latency_ms": latency_ms, "error": None, "mode": "api"}
            err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning("HealthCheck %s failed: %s", provider_id, err_msg)
            return {"ok": False, "latency_ms": latency_ms, "error": err_msg, "mode": "api"}
        except httpx.TimeoutException:
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.warning("HealthCheck %s timeout (%dms)", provider_id, latency_ms)
            return {"ok": False, "latency_ms": latency_ms, "error": "timeout", "mode": "api"}
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.warning("HealthCheck %s error: %s", provider_id, e)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e), "mode": "api"}

    @staticmethod
    def _build_auth_headers(api_key: str, auth_type: str) -> dict[str, str]:
        """根据 auth_type 构造认证 header。"""
        if not api_key:
            return {}
        if auth_type == "x-api-key":
            return {"x-api-key": api_key}
        # 默认 bearer
        return {"Authorization": f"Bearer {api_key}"}


class FallbackChain:
    """用户显式配置的 fallback 链。

    从 models.yaml 的 fallback_chains 字段读取，格式（v2 起支持 provider + 显式 model）：
        fallback_chains:
          minimax:
            - openai                                  # 仅 provider，fallback 时用该 provider 默认 model
            - {provider: deepseek, model: deepseek-v4-flash}  # 显式 provider + model

    设计原则：fail-loud > fail-quiet。
    - 只有用户显式配置了 fallback_chain 才切换
    - 未配置返回 None，调用方应原样报错（让用户感知失败）
    """

    def __init__(self, fallback_config: dict[str, list[Any]] | None):
        # 内部归一化为 list[{provider: str, model: str|None}]
        # 兼容旧 shape: list[str]，也接受新 shape: list[dict]
        raw_chains: dict[str, list[Any]] = fallback_config or {}
        self._chains: dict[str, list[dict[str, str | None]]] = {}
        for pid, chain in raw_chains.items():
            entries: list[dict[str, str | None]] = []
            for item in chain or []:
                if isinstance(item, str):
                    if not item:
                        continue
                    entries.append({"provider": item, "model": None})
                elif isinstance(item, dict):
                    provider = item.get("provider")
                    if not isinstance(provider, str) or not provider:
                        continue
                    model_val = item.get("model")
                    model = model_val if isinstance(model_val, str) and model_val else None
                    entries.append({"provider": provider, "model": model})
                # 其他类型（数字/None）忽略——fail-loud 通过 type hint 兜底
            self._chains[pid] = entries

    def get_fallback(self, provider_id: str) -> str | None:
        """返回首个 fallback provider_id（向后兼容旧 API）。

        `model_config.remove_provider` 的孤儿清理仍按 provider_id 字符串过滤。
        """
        entries = self._chains.get(provider_id, [])
        return entries[0]["provider"] if entries else None

    def get_fallback_entry(self, provider_id: str) -> dict[str, str | None] | None:
        """返回首个 fallback 完整条目 {provider, model}。

        - model 为 None 表示用 fallback provider 的默认 model（兼容旧配置）
        - 未配置返回 None（fail-loud）
        """
        entries = self._chains.get(provider_id, [])
        if not entries:
            return None
        return dict(entries[0])  # 拷贝避免 caller mutate

    def get_chain(self, provider_id: str) -> list[dict[str, str | None]]:
        """返回完整 fallback 链，每项 {provider, model}（model 可能为 None）。

        用于 API 响应和前端展示。每个 dict 是浅拷贝，caller 改 dict 不影响内部状态。
        """
        return [dict(e) for e in self._chains.get(provider_id, [])]

    def has_chain(self, provider_id: str) -> bool:
        """是否为该 provider 配置了 fallback。"""
        return bool(self._chains.get(provider_id))


# ====== 单例 ======

_health_checker: HealthChecker | None = None
_fallback_chain: FallbackChain | None = None


def get_health_checker() -> HealthChecker:
    """获取全局 HealthChecker 单例。"""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker


def get_fallback_chain() -> FallbackChain:
    """获取全局 FallbackChain 单例（从 models.yaml 加载 fallback_chains）。

    models.yaml 未配置 fallback_chains 字段时，返回空链（所有 get_fallback 返回 None）。
    """
    global _fallback_chain
    if _fallback_chain is None:
        from orchestrator.model_config import get_model_config
        mc = get_model_config()
        fallback_config = mc.config.get("fallback_chains", {}) or {}
        _fallback_chain = FallbackChain(fallback_config)
    return _fallback_chain
