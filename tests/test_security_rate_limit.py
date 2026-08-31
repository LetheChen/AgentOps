"""S5 限流 + 恒定响应时间测试。

验收标准（方案 §8）：
  - 11 次失败被锁 15min
  - 不存在用户也走假 hash（恒定响应时间）
"""
from __future__ import annotations

import ipaddress
import os
import time
from pathlib import Path

import pytest
from fastapi import HTTPException, Request

from api.security import rate_limit as rl
from audit.store import SqliteEventStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 关掉 bootstrap，理由同 test_security_store.py
    monkeypatch.setattr(
        "audit.security_schema.bootstrap_first_user", lambda conn: None
    )
    return SqliteEventStore(str(tmp_path / "audit.db"))


def _req(host: str | None = "1.2.3.4", headers: dict[str, str] | None = None) -> Request:
    """构造最小 Request：只用到 request.client.host 和 request.headers。"""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": (host, 54321) if host else None,
    }
    return Request(scope)


# ============================================================
# 恒定响应时间（D12）
# ============================================================

@pytest.mark.asyncio
async def test_verify_password_constant_time_matches_real_verify():
    from audit.security_schema import hash_password

    real = hash_password("correct-horse-battery-staple")
    assert rl.verify_password_constant_time(real, "correct-horse-battery-staple") is True
    assert rl.verify_password_constant_time(real, "wrong") is False
    assert rl.verify_password_constant_time(None, "anything") is False, "空 hash 走诱饵，必 False"
    assert rl.verify_password_constant_time("", "anything") is False


@pytest.mark.asyncio
async def test_missing_user_costs_same_order_as_wrong_password():
    """时序侧信道：用户不存在 vs 密码错误，耗时必须同量级（不能差出 10 倍）。"""
    from audit.security_schema import hash_password

    real = hash_password("known-password-123")

    t0 = time.perf_counter()
    rl.verify_password_constant_time(None, "whatever")
    missing_user = time.perf_counter() - t0

    t0 = time.perf_counter()
    rl.verify_password_constant_time(real, "wrong-password")
    wrong_password = time.perf_counter() - t0

    ratio = max(missing_user, wrong_password) / max(min(missing_user, wrong_password), 1e-6)
    assert ratio < 10, (
        f"用户不存在 {missing_user * 1000:.1f}ms vs 密码错误 {wrong_password * 1000:.1f}ms "
        f"（比值 {ratio:.1f}）—— 差异过大会泄露用户名是否存在"
    )


# ============================================================
# 限流
# ============================================================

@pytest.mark.asyncio
async def test_ip_locked_after_max_failures(store: SqliteEventStore):
    for _ in range(rl.LOGIN_MAX_FAILURES_IP - 1):
        await rl.record_login_failure(store, ip="9.9.9.9")
        await rl.check_login_rate_limit(store, ip="9.9.9.9", username="alice")

    await rl.record_login_failure(store, ip="9.9.9.9")
    with pytest.raises(HTTPException) as exc:
        await rl.check_login_rate_limit(store, ip="9.9.9.9", username="alice")

    assert exc.value.status_code == 423
    assert exc.value.detail["error"] == "too_many_failed_attempts"
    assert 0 < exc.value.detail["retry_after"] <= rl.LOGIN_LOCK_SEC
    assert exc.value.headers["Retry-After"] == str(exc.value.detail["retry_after"])


@pytest.mark.asyncio
async def test_locked_rejects_even_correct_password(store: SqliteEventStore):
    """方案 §3.3：被锁后密码正确也拒绝，否则限流可被"猜中即绕过"。"""
    for _ in range(rl.LOGIN_MAX_FAILURES_IP):
        await rl.record_login_failure(store, ip="7.7.7.7")

    with pytest.raises(HTTPException) as exc:
        await rl.check_login_rate_limit(store, ip="7.7.7.7", username="alice")
    assert exc.value.status_code == 423

    # 唯有显式重置才解锁（= 登录成功路径）
    await rl.reset_login_attempts(store, ip="7.7.7.7", username="alice")
    await rl.check_login_rate_limit(store, ip="7.7.7.7", username="alice")


@pytest.mark.asyncio
async def test_username_dimension_locked_independently(store: SqliteEventStore):
    """用户名维度 5 次即锁（比 IP 的 10 次更严）。"""
    for i in range(rl.LOGIN_MAX_FAILURES_USER - 1):
        await rl.record_login_failure(store, ip=f"10.0.0.{i}", username="bob")
        await rl.check_login_rate_limit(store, ip=f"10.0.0.{i}", username="bob")

    await rl.record_login_failure(store, ip="10.0.0.99", username="bob")
    with pytest.raises(HTTPException) as exc:
        await rl.check_login_rate_limit(store, ip="10.0.0.99", username="bob")
    assert exc.value.status_code == 423


@pytest.mark.asyncio
async def test_unknown_username_only_counts_ip(store: SqliteEventStore):
    """用户不存在时只记 IP，不记用户名——否则随机用户名能锁死合法账号（DoS）。"""
    await rl.record_login_failure(store, ip="5.5.5.5")  # 不传 username

    assert await store.get_login_attempt("ip:5.5.5.5") is not None
    assert await store.get_login_attempt("user:") is None, "空用户名不该建行"


# ============================================================
# 客户端 IP（§10）
# ============================================================

def test_client_ip_direct_peer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AGENTOPS_TRUSTED_PROXIES", raising=False)
    rl._TRUSTED_PROXIES = None

    assert rl.client_ip(_req("1.2.3.4")) == "1.2.3.4"
    assert rl.client_ip(_req(None)) == ""


def test_client_ip_ignores_untrusted_xff(monkeypatch: pytest.MonkeyPatch):
    """没配可信代理时，XFF 一律忽略——否则客户端伪造头就能绕过限流。"""
    monkeypatch.delenv("AGENTOPS_TRUSTED_PROXIES", raising=False)
    rl._TRUSTED_PROXIES = None

    req = _req("1.2.3.4", {"X-Forwarded-For": "8.8.8.8"})
    assert rl.client_ip(req) == "1.2.3.4"


def test_client_ip_trusts_xff_from_known_proxy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_TRUSTED_PROXIES", "127.0.0.1,10.0.0.0/8")
    rl._TRUSTED_PROXIES = None

    req = _req("10.1.2.3", {"X-Forwarded-For": "203.0.113.9, 10.1.2.3"})
    assert rl.client_ip(req) == "203.0.113.9", "取 XFF 最左段（原始客户端）"

    # 直连对端不在可信段 → XFF 不采信
    req2 = _req("1.2.3.4", {"X-Forwarded-For": "203.0.113.9"})
    assert rl.client_ip(req2) == "1.2.3.4"


def test_client_ip_normalizes_ipv4_mapped_ipv6(monkeypatch: pytest.MonkeyPatch):
    """::ffff:1.2.3.4 必须归一化成 1.2.3.4，否则 CIDR 匹配不到。"""
    monkeypatch.setenv("AGENTOPS_TRUSTED_PROXIES", "10.0.0.0/8")
    rl._TRUSTED_PROXIES = None

    assert rl.client_ip(_req("::ffff:1.2.3.4")) == "1.2.3.4"

    req = _req("::ffff:10.1.2.3", {"X-Forwarded-For": "203.0.113.9"})
    assert rl.client_ip(req) == "203.0.113.9"


def test_client_ip_invalid_proxy_entry_ignored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_TRUSTED_PROXIES", "not-an-ip,10.0.0.0/8")
    rl._TRUSTED_PROXIES = None

    assert rl._trusted_proxies() == [ipaddress.ip_network("10.0.0.0/8")]


# ============================================================
# parse_iso
# ============================================================

def test_parse_iso():
    from datetime import datetime, timezone

    dt = rl.parse_iso(datetime.now(timezone.utc).isoformat())
    assert dt is not None and dt.tzinfo is not None
    assert rl.parse_iso(None) is None
    assert rl.parse_iso("") is None
    assert rl.parse_iso("not-a-date") is None


@pytest.fixture(autouse=True)
def _reset_trusted_proxies():
    """_TRUSTED_PROXIES 是模块级缓存，用例间必须复位，否则互相污染。"""
    rl._TRUSTED_PROXIES = None
    yield
    rl._TRUSTED_PROXIES = None
    os.environ.pop("AGENTOPS_TRUSTED_PROXIES", None)
