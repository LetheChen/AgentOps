"""登录限流 + 恒定响应时间（S5）。

设计见 ``docs/security-mvp-plan-2026-08-29.md`` §4.3（限流）与 §10（真实客户端 IP）。

解决两个安全问题：

1. **防暴力破解**（§4.3）
   同 IP 5 分钟内失败 > 10 次、或同用户名失败 > 5 次 → 锁定 15 分钟（HTTP 423）。
   计数落在 ``security_login_attempts`` 表，数据访问在 ``audit/security_store.py``。

2. **防时序侧信道**（D12）
   "用户不存在"和"密码错误"必须花一样的时间，否则攻击者能用响应耗时枚举出
   哪些用户名真实存在。做法是用户名查不到时用同一个 argon2 参数跑一次假校验。

真实客户端 IP（§10）
   默认**不信任** ``X-Forwarded-For``——它可被客户端任意伪造，一旦盲信，限流形同
   虚设（攻击者每次换一个 XFF 就能绕过 IP 维度）。只有直连对端在
   ``AGENTOPS_TRUSTED_PROXIES``（逗号分隔的 IP / CIDR）里时才取 XFF 最左段。
   标准容器部署下 uvicorn 的 ``--proxy-headers`` + ``--forwarded-allow-ips`` 已经
   把 ``request.client.host`` 改写成真实客户端 IP，本函数的 XFF 分支只是兜底。
"""
from __future__ import annotations

import ipaddress
import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status

from audit.security_schema import hash_password, verify_password
from audit.security_store import (
    LOGIN_LOCK_SEC,
    LOGIN_MAX_FAILURES_IP,
    LOGIN_MAX_FAILURES_USER,
    LOGIN_WINDOW_SEC,
)

logger = logging.getLogger(__name__)

__all__ = [
    "client_ip",
    "parse_iso",
    "check_login_rate_limit",
    "record_login_failure",
    "reset_login_attempts",
    "verify_password_constant_time",
    "login_keys",
]


# ============================================================
# 工具
# ============================================================

def parse_iso(value: str | None) -> datetime | None:
    """ISO 字符串 → aware datetime。空值 / 格式非法返回 None。

    库里存的都是 ``datetime.now(timezone.utc).isoformat()``，带 ``+00:00``，
    ``fromisoformat`` 能直接解析出 aware 对象，不需要补时区。
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _normalize_ip(raw: str | None) -> str:
    """统一 IP 表示：去空白，并把 IPv4-mapped IPv6（::ffff:1.2.3.4）还原成 IPv4。"""
    if not raw:
        return ""
    raw = raw.strip()
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    # uvicorn 在双栈监听下常把 IPv4 对端报成 ::ffff:1.2.3.4，
    # 不归一化的话 CIDR 匹配会失败（ipaddress 不会自动等价）。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ip)


_TRUSTED_PROXIES: list[ipaddress._BaseNetwork] | None = None


def _trusted_proxies() -> list[ipaddress._BaseNetwork]:
    """惰性解析 ``AGENTOPS_TRUSTED_PROXIES``（进程内只解析一次）。

    格式示例::

        AGENTOPS_TRUSTED_PROXIES=172.16.0.0/12,10.0.0.0/8,127.0.0.1
    """
    global _TRUSTED_PROXIES
    if _TRUSTED_PROXIES is None:
        nets: list[ipaddress._BaseNetwork] = []
        for part in os.environ.get("AGENTOPS_TRUSTED_PROXIES", "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                logger.warning("[security] AGENTOPS_TRUSTED_PROXIES 含非法条目，已忽略：%s", part)
        _TRUSTED_PROXIES = nets
    return _TRUSTED_PROXIES


def client_ip(request: Request) -> str:
    """取客户端 IP，用于限流计数与审计日志。

    信任链：直连对端必须落在 ``AGENTOPS_TRUSTED_PROXIES`` 内，才采信 ``X-Forwarded-For``
    的最左段；否则一律用直连对端。没有配置可信代理时直接返回直连对端。
    """
    peer = _normalize_ip(request.client.host if request.client else None)

    trusted = _trusted_proxies()
    if not trusted or not peer:
        return peer

    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in net for net in trusted):
        # 不信任的来源写的 XFF 一律忽略，防伪造绕过限流
        return peer

    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return peer
    # XFF 形如 "client, proxy1, proxy2" —— 原始客户端在最左
    return _normalize_ip(xff.split(",")[0]) or peer


_FAKE_HASH: str | None = None


def _fake_hash() -> str:
    """惰性生成诱饵 hash（一次约 50-100ms，只在首次调用时付这个代价）。"""
    global _FAKE_HASH
    if _FAKE_HASH is None:
        _FAKE_HASH = hash_password("decoy-password-that-never-matches")
    return _FAKE_HASH


def verify_password_constant_time(password_hash: str | None, password: str) -> bool:
    """恒定时间校验：hash 为空（用户不存在）时对着诱饵 hash 跑一次 argon2。

    这样"用户名不存在"和"密码错误"的 CPU 开销一致，堵掉用响应耗时枚举用户名的
    侧信道。``password_hash`` 为空串时 ``verify_password`` 会直接短路返回 False，
    所以这里必须替换成诱饵再校验。
    """
    return verify_password(password_hash or _fake_hash(), password)


# ============================================================
# 限流
# ============================================================

def login_keys(ip: str, username: str) -> tuple[str, str]:
    """限流键。IP 维度与用户名维度分开计数，任一超限即锁。"""
    return f"ip:{ip}", f"user:{username}"


async def check_login_rate_limit(
    store, ip: str, username: str
) -> None:
    """登录前置检查。被锁时抛 423（带 ``Retry-After`` 秒数）。

    注意：**即便密码正确，被锁也照样拒绝**（方案 §3.3）。否则攻击者在猜到正确
    密码的瞬间就能绕过锁定，限流失去意义。
    """
    ip_key, user_key = login_keys(ip, username)
    for key in (ip_key, user_key):
        if await store.is_login_locked(key):
            row = await store.get_login_attempt(key)
            until = parse_iso(row.get("locked_until")) if row else None
            retry = LOGIN_LOCK_SEC
            if until:
                retry = max(1, int((until - datetime.now(timezone.utc)).total_seconds()))
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail={
                    "error": "too_many_failed_attempts",
                    "message": "登录失败次数过多，请稍后再试",
                    "retry_after": retry,
                },
                headers={"Retry-After": str(retry)},
            )


async def record_login_failure(store, ip: str, username: str | None = None) -> None:
    """记一次登录失败。

    - IP 维度必记（用户名可能根本不存在，只能按 IP 兜底）
    - 用户名维度仅当用户存在时记：不存在时按 IP 记即可，否则攻击者能用随机
      用户名把根本不存在的账号"锁死"，构成针对合法用户的拒绝服务。
    """
    ip_key, user_key = login_keys(ip, username or "")
    await store.record_login_failure(
        ip_key, max_failures=LOGIN_MAX_FAILURES_IP,
        window_sec=LOGIN_WINDOW_SEC, lock_sec=LOGIN_LOCK_SEC,
    )
    if username:
        await store.record_login_failure(
            user_key, max_failures=LOGIN_MAX_FAILURES_USER,
            window_sec=LOGIN_WINDOW_SEC, lock_sec=LOGIN_LOCK_SEC,
        )


async def reset_login_attempts(store, ip: str, username: str) -> None:
    """登录成功 → 清空两个维度的失败计数。"""
    await store.reset_login_attempts(*login_keys(ip, username))
