"""P0.18.6 Worker token JWT 签发 + 校验 + WorkerRegistry WS 注册管理。

设计依据：[docs/audit/p018-workspace-authorization-and-docker-lifecycle.md](docs/audit/p018-workspace-authorization-and-docker-lifecycle.md) §4.3.4

Worker token 协议：
- JWT 格式，5min TTL
- payload: {worker_id, iat, exp}
- 签名：HS256 + 共享密钥（从 AGENTOPS_WORKER_JWT_SECRET 环境变量读，默认随机生成）

WorkerRegistry：
- worker WS connect 后注册到 registry
- provisioner 通过 wait_registered 等 worker 上线
- send_credential 推送 api_key（Phase 1 明文，Phase 2 加密）
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

WORKER_TOKEN_TTL_SECONDS = 300  # 5 分钟
WORKER_TOKEN_ALGORITHM = "HS256"


# ============================================================
# JWT 签发 + 校验（自实现，不引入 PyJWT 依赖）
# ============================================================

_cached_jwt_secret: str | None = None


def _get_jwt_secret() -> str:
    """从环境变量读 JWT 密钥，默认随机生成（重启后失效，适合开发期）。

    生产部署应通过 AGENTOPS_WORKER_JWT_SECRET 环境变量注入固定密钥。
    缓存到模块级变量，避免每次调用重新生成（issue 和 verify 必须用同一 secret）。
    """
    global _cached_jwt_secret
    if _cached_jwt_secret is not None:
        return _cached_jwt_secret
    secret = os.environ.get("AGENTOPS_WORKER_JWT_SECRET")
    if not secret:
        secret = secrets.token_urlsafe(32)
        logger.warning(
            "AGENTOPS_WORKER_JWT_SECRET not set, using random secret "
            "(worker tokens will invalidate on restart)"
        )
    _cached_jwt_secret = secret
    return secret


def _reset_jwt_secret_cache() -> None:
    """重置 secret 缓存（测试用）。"""
    global _cached_jwt_secret
    _cached_jwt_secret = None


def _b64encode(data: bytes) -> str:
    """URL-safe base64 编码（无 padding）。"""
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    """URL-safe base64 解码（自动补 padding）。"""
    import base64
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _jwt_sign(payload: dict, secret: str) -> str:
    """HS256 签名生成 JWT。"""
    header = {"alg": WORKER_TOKEN_ALGORITHM, "typ": "JWT"}
    header_b64 = _b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature_b64 = _b64encode(signature)
    return f"{signing_input}.{signature_b64}"


def _jwt_verify(token: str, secret: str) -> dict | None:
    """HS256 验证 JWT。返回 payload 或 None（校验失败）。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}"
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        actual_signature = _b64decode(signature_b64)
        if not hmac.compare_digest(expected_signature, actual_signature):
            return None
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        # 检查 exp
        if "exp" in payload and time.time() > payload["exp"]:
            return None
        return payload
    except Exception as e:
        logger.warning("JWT verify failed: %s", e)
        return None


# ============================================================
# Worker token 签发 + 校验
# ============================================================

def issue_worker_token(worker_id: str, ttl: int = WORKER_TOKEN_TTL_SECONDS) -> str:
    """签发 worker token（JWT 5min TTL）。

    参数:
        worker_id: worker 唯一标识
        ttl: 有效期秒数（默认 300 = 5 分钟）

    返回: JWT token 字符串
    """
    now = int(time.time())
    payload = {
        "worker_id": worker_id,
        "iat": now,
        "exp": now + ttl,
    }
    return _jwt_sign(payload, _get_jwt_secret())


def verify_worker_token(worker_id: str, token: str) -> bool:
    """校验 worker token。

    参数:
        worker_id: 期望的 worker_id（token payload 中的 worker_id 必须匹配）
        token: JWT token 字符串

    返回: True 校验通过；False 校验失败（签名错误/过期/worker_id 不匹配）
    """
    payload = _jwt_verify(token, _get_jwt_secret())
    if not payload:
        return False
    if payload.get("worker_id") != worker_id:
        return False
    return True


# ============================================================
# WorkerRegistry（worker WS 注册管理）
# ============================================================

class WorkerRegistry:
    """worker WS 注册表 + provisioner 等待 worker 上线。

    设计：
    - worker WS connect 后调 register(worker_id, websocket)
    - provisioner 调 wait_registered(worker_id) 等 worker 上线（asyncio.Event）
    - send_credential(worker_id, api_key) 通过 WS 推送凭据
    """

    def __init__(self):
        # worker_id → {"websocket": ws, "registered_event": asyncio.Event}
        self._workers: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def register(self, worker_id: str, websocket) -> None:
        """worker WS connect 后注册。触发 wait_registered 的 asyncio.Event。"""
        async with self._lock:
            event = asyncio.Event()
            if worker_id in self._workers:
                # 旧连接存在，复用 event（已 set 状态）
                event = self._workers[worker_id].get("registered_event", asyncio.Event())
            self._workers[worker_id] = {
                "websocket": websocket,
                "registered_event": event,
                "registered_at": time.time(),
            }
            event.set()
            logger.info("worker %s registered", worker_id)

    async def unregister(self, worker_id: str) -> None:
        """worker WS disconnect 后注销。"""
        async with self._lock:
            self._workers.pop(worker_id, None)
            logger.info("worker %s unregistered", worker_id)

    async def wait_registered(self, worker_id: str) -> None:
        """等 worker 注册。provisioner 调用，配合 asyncio.wait_for 设置超时。"""
        # 若 worker 已注册，event 已 set，立即返回
        async with self._lock:
            entry = self._workers.get(worker_id)
        if entry:
            await entry["registered_event"].wait()
            return
        # worker 未注册，创建 event 等待（register 时会 set）
        async with self._lock:
            if worker_id not in self._workers:
                self._workers[worker_id] = {
                    "websocket": None,
                    "registered_event": asyncio.Event(),
                    "registered_at": None,
                }
            event = self._workers[worker_id]["registered_event"]
        await event.wait()

    async def is_registered(self, worker_id: str) -> bool:
        """检查 worker 是否已注册。"""
        async with self._lock:
            entry = self._workers.get(worker_id)
            return entry is not None and entry["websocket"] is not None

    async def send_credential(self, worker_id: str, api_key: str) -> bool:
        """通过 WS 推送 api_key 给 worker。

        Phase 1（本期）：明文 JSON 推送（仅本机 docker，安全靠 bridge 网络隔离）
        Phase 2（后续）：ed25519 + AES 加密（见方案 §4.3.4）

        返回: True 推送成功；False worker 未连接
        """
        async with self._lock:
            entry = self._workers.get(worker_id)
            if not entry or not entry["websocket"]:
                logger.warning("send_credential: worker %s not connected", worker_id)
                return False
            websocket = entry["websocket"]

        # Phase 1: 明文推送（Phase 2 改加密）
        message = {
            "type": "credential",
            "api_key": api_key,
            # Phase 2 加这些字段：
            # "encrypted_with": "ed25519:alice_pubkey_hash",
            # "encrypted_payload": "<base64 AES encrypted>"
        }
        try:
            await websocket.send_json(message)
            logger.info("credential sent to worker %s", worker_id)
            return True
        except Exception as e:
            logger.error("send_credential to worker %s failed: %s", worker_id, e)
            return False

    async def send_task(self, worker_id: str, task_config: dict) -> bool:
        """通过 WS 推送任务配置给 worker。"""
        async with self._lock:
            entry = self._workers.get(worker_id)
            if not entry or not entry["websocket"]:
                return False
            websocket = entry["websocket"]
        try:
            message = {"type": "task", **task_config}
            await websocket.send_json(message)
            return True
        except Exception as e:
            logger.error("send_task to worker %s failed: %s", worker_id, e)
            return False

    async def send_shutdown(self, worker_id: str) -> bool:
        """通知 worker 关闭。"""
        async with self._lock:
            entry = self._workers.get(worker_id)
            if not entry or not entry["websocket"]:
                return False
            websocket = entry["websocket"]
        try:
            await websocket.send_json({"type": "shutdown"})
            return True
        except Exception as e:
            logger.error("send_shutdown to worker %s failed: %s", worker_id, e)
            return False

    def list_active_workers(self) -> list[str]:
        """列出已注册的 worker_id（同步，用于 status 查询）。"""
        return [
            wid for wid, entry in self._workers.items()
            if entry.get("websocket") is not None
        ]


# ============================================================
# 全局单例
# ============================================================

_global_registry: WorkerRegistry | None = None


def get_worker_registry() -> WorkerRegistry:
    """获取全局 WorkerRegistry 单例。"""
    global _global_registry
    if _global_registry is None:
        _global_registry = WorkerRegistry()
    return _global_registry


def set_worker_registry(registry: WorkerRegistry) -> None:
    """设置全局 WorkerRegistry（测试用）。"""
    global _global_registry
    _global_registry = registry
