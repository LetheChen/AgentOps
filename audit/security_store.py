"""安全认证模块的数据访问层（S4）。

``SecurityStoreMixin`` 被 ``SqliteEventStore`` 继承，提供 security 系列 7 张表的
async 方法 + 写入节流。DDL / 迁移 / 密码哈希在 ``audit/security_schema.py``，
本模块只管增删改查。

宿主约定
--------
Mixin 只依赖宿主提供 ``self._exec(sql, params) -> sqlite3.Cursor``
（见 ``audit/store.py:2097``，内部带 ``threading.Lock``）。
所有 DB 调用统一走 ``asyncio.to_thread(self._exec, ...)``，与 v3 现有方法风格一致。

节流策略（D20）
--------------
``last_seen_at`` / ``last_used_at`` 是"每次有效请求都要更新"的高频字段。
不节流会把每次 API 调用变成一次写事务，高 QPS 下 SQLite WAL 写锁会成瓶颈。
故做进程内 LRU 节流：**距上次落库 < 30s 直接跳过**。
进程崩溃最多丢失 30s 的"最后活跃时间"，对在线状态展示无实质影响。

PAT 校验缓存（D20）
------------------
argon2 verify 单次约 50-100ms，每个 API 请求跑一次会明显拖慢响应。
故对校验通过的 PAT 做 60s 进程内缓存，key 是 **token 的 SHA-256**（不存明文）。
撤销 / 轮换 / 用户禁用时必须主动失效对应缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

from audit.security_schema import _now_iso, hash_password, verify_password

# ============================================================
# 常量
# ============================================================

_B62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

PAT_PREFIX_LEN = 12      # security_api_tokens.prefix；用于按索引查候选行
PAT_SECRET_LEN = 22      # 秘密部分；62^22 ≈ 2.7×10^39
SESSION_TOKEN_BYTES = 32

LAST_SEEN_THROTTLE_SEC = 30     # last_seen_at 落库最小间隔
TOUCH_THROTTLE_SEC = 30         # auth_session / api_token 的 last_used_* 同理
PAT_CACHE_TTL_SEC = 60          # PAT 校验结果缓存时长（D20）

# 登录限流（S4 只提供数据层，策略常量供 S5 复用）
LOGIN_WINDOW_SEC = 300          # 统计窗口 5 min
LOGIN_MAX_FAILURES_IP = 10      # 同 IP 窗口内失败上限
LOGIN_MAX_FAILURES_USER = 5     # 同用户名窗口内失败上限
LOGIN_LOCK_SEC = 900            # 触发后锁定 15 min


def _b62(n: int) -> str:
    """生成 n 位 base62 随机串（secrets，非 random）。"""
    return "".join(secrets.choice(_B62_ALPHABET) for _ in range(n))


# ============================================================
# 节流 LRU（零依赖：OrderedDict 实现，避免为一个 LRU 引入 cachetools）
# ============================================================

class _ThrottleCache:
    """极简 LRU + TTL 缓存，用于写入节流和 PAT 校验缓存。

    只服务单进程内的性能优化；进程重启即失效，不影响正确性。
    """

    def __init__(self, maxsize: int = 10000) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any, default: Any = None) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        expires_at, value = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return default
        self._data.move_to_end(key)
        return value

    def put(self, key: Any, value: Any, ttl: float) -> None:
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def pop(self, key: Any, default: Any = None) -> Any:
        item = self._data.pop(key, None)
        return item[1] if item is not None else default

    def clear(self) -> None:
        self._data.clear()

    def pop_by_value(self, value: Any) -> int:
        """删除所有 value 等于 ``value`` 的项，返回删除数量。

        用于 PAT 撤销：缓存 key 是 token 的 SHA-256，撤销时手里只有 token_id，
        无法反推 key，只能按 value 反查。
        """
        victims = [k for k, (_, v) in self._data.items() if v == value]
        for k in victims:
            self._data.pop(k, None)
        return len(victims)

    def __len__(self) -> int:
        return len(self._data)


# 进程内单例
_last_seen_cache = _ThrottleCache(maxsize=10000)
_touch_cache = _ThrottleCache(maxsize=10000)
_pat_cache = _ThrottleCache(maxsize=5000)


def invalidate_pat_cache(token_sha256: str) -> None:
    """撤销 / 轮换 / 用户禁用时调用，避免已撤销的 token 继续命中 60s 缓存。"""
    _pat_cache.pop(token_sha256, None)


# ============================================================
# Mixin
# ============================================================

class SecurityStoreMixin:
    """security 系列表的 async 访问方法。

    依赖宿主：``self._exec(sql, params) -> sqlite3.Cursor``。
    """

    # ====== 用户 ======

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        cur = await asyncio.to_thread(
            self._exec, "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        r = cur.fetchone()
        return dict(r) if r else None

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        cur = await asyncio.to_thread(
            self._exec, "SELECT * FROM users WHERE username = ?", (username,)
        )
        r = cur.fetchone()
        return dict(r) if r else None

    async def list_users(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM users"
        if not include_disabled:
            sql += " WHERE disabled_at IS NULL"
        sql += " ORDER BY created_at"
        cur = await asyncio.to_thread(self._exec, sql)
        return [dict(r) for r in cur.fetchall()]

    async def create_user(
        self,
        user_id: str,
        username: str,
        password: str,
        *,
        display_name: str = "",
        email: str = "",
        must_reset_password: bool = True,
    ) -> str:
        """创建用户。密码明文只在本方法内出现，入库的是 argon2 hash。"""
        now = _now_iso()
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO users(user_id, username, display_name, email, password_hash, "
            "                  must_reset_password, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, username, display_name or username, email or None,
                hash_password(password), int(must_reset_password), now, now,
            ),
        )
        return user_id

    async def update_user(self, user_id: str, **fields: Any) -> None:
        """改资料。只允许白名单字段，避免调用方拼出 `password_hash = ?` 之类。"""
        allowed = {"display_name", "email", "password_hash",
                   "must_reset_password", "last_login_at", "last_seen_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"不允许更新的字段: {sorted(bad)}")
        if not fields:
            return
        fields = dict(fields, updated_at=_now_iso())
        sets = ", ".join(f"{k} = ?" for k in fields)
        await asyncio.to_thread(
            self._exec,
            f"UPDATE users SET {sets} WHERE user_id = ?",
            (*fields.values(), user_id),
        )

    async def set_user_password(self, user_id: str, new_password: str) -> None:
        await self.update_user(
            user_id, password_hash=hash_password(new_password), must_reset_password=0
        )

    async def set_user_disabled(
        self,
        user_id: str,
        disabled: bool,
        *,
        by_user_id: str | None = None,
        reason: str = "",
    ) -> None:
        """启停用户。**禁用时级联吊销该用户全部 session + PAT**（方案 §4.4）。

        启用（disabled=False）不做任何恢复——已吊销的凭证不可复活，需重新签发。
        """
        now = _now_iso()
        if not disabled:
            await asyncio.to_thread(
                self._exec,
                "UPDATE users SET disabled_at = NULL, updated_at = ? WHERE user_id = ?",
                (now, user_id),
            )
            return

        await asyncio.to_thread(
            self._exec,
            "UPDATE users SET disabled_at = ?, updated_at = ? WHERE user_id = ?",
            (now, now, user_id),
        )
        await self.revoke_all_sessions(
            user_id, revoked_by_user_id=by_user_id,
            reason=f"user_disabled:{reason}" if reason else "user_disabled",
        )
        await self.revoke_all_api_tokens(
            user_id, revoked_by_user_id=by_user_id,
            reason=f"user_disabled:{reason}" if reason else "user_disabled",
        )

    async def touch_last_seen(self, user_id: str, *, throttle: bool = True) -> bool:
        """更新 last_seen_at。返回是否真的落库（False = 被节流跳过）。"""
        now = time.monotonic()
        last = _last_seen_cache.get(user_id, 0.0)
        if throttle and (now - last) < LAST_SEEN_THROTTLE_SEC:
            return False
        _last_seen_cache.put(user_id, now, ttl=LAST_SEEN_THROTTLE_SEC)
        await asyncio.to_thread(
            self._exec,
            "UPDATE users SET last_seen_at = ? WHERE user_id = ?",
            (_now_iso(), user_id),
        )
        return True

    # ====== 角色 / 权限 ======

    async def list_permissions(self) -> list[dict[str, Any]]:
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM security_permissions ORDER BY resource, action",
        )
        return [dict(r) for r in cur.fetchall()]

    async def list_roles(self) -> list[dict[str, Any]]:
        cur = await asyncio.to_thread(
            self._exec, "SELECT * FROM security_roles ORDER BY role_id"
        )
        return [dict(r) for r in cur.fetchall()]

    async def get_role(self, role_id: str) -> dict[str, Any] | None:
        cur = await asyncio.to_thread(
            self._exec, "SELECT * FROM security_roles WHERE role_id = ?", (role_id,)
        )
        r = cur.fetchone()
        return dict(r) if r else None

    async def list_user_roles(self, user_id: str) -> list[dict[str, Any]]:
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT r.*, ur.granted_at, ur.granted_by "
            "FROM security_user_roles ur JOIN security_roles r USING (role_id) "
            "WHERE ur.user_id = ? ORDER BY r.role_id",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    async def list_role_members(
        self, role_id: str, *, active_only: bool = True
    ) -> list[str]:
        """某角色的成员 user_id 列表。

        ``active_only`` 排除已禁用用户——判断"还有谁能管安全"时要按活跃成员算，
        否则一个被禁用的 owner 会让"唯一 owner"的护栏永远不触发。
        """
        sql = (
            "SELECT ur.user_id FROM security_user_roles ur "
            "JOIN users u ON u.user_id = ur.user_id WHERE ur.role_id = ?"
        )
        if active_only:
            sql += " AND u.disabled_at IS NULL"
        sql += " ORDER BY ur.user_id"
        cur = await asyncio.to_thread(self._exec, sql, (role_id,))
        return [r["user_id"] for r in cur.fetchall()]

    async def bind_user_role(
        self, user_id: str, role_id: str, *, granted_by: str | None = None
    ) -> None:
        """绑定角色。role_owner 的绑定只应由 bootstrap 做（解绑有触发器拦截）。"""
        await asyncio.to_thread(
            self._exec,
            "INSERT OR IGNORE INTO security_user_roles(user_id, role_id, granted_by, granted_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, role_id, granted_by, _now_iso()),
        )

    async def unbind_user_role(self, user_id: str, role_id: str) -> None:
        """解绑角色。role_owner 会被 DB 触发器 ``prevent_owner_unbind`` 拒绝。"""
        await asyncio.to_thread(
            self._exec,
            "DELETE FROM security_user_roles WHERE user_id = ? AND role_id = ?",
            (user_id, role_id),
        )

    async def list_role_permissions(self, role_id: str) -> list[str]:
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT perm_id FROM security_role_permissions WHERE role_id = ? ORDER BY perm_id",
            (role_id,),
        )
        return [r["perm_id"] for r in cur.fetchall()]

    async def get_permission_matrix(self) -> dict[str, list[str]]:
        """返回 {role_id: [perm_id, ...]}，前端权限矩阵直接用。"""
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT role_id, perm_id FROM security_role_permissions ORDER BY role_id, perm_id",
        )
        matrix: dict[str, list[str]] = {}
        for r in cur.fetchall():
            matrix.setdefault(r["role_id"], []).append(r["perm_id"])
        return matrix

    async def compute_user_scopes(self, user_id: str) -> str:
        """多角色权限**并集**，返回空格分隔的 perm_id 串。

        ``require_scope`` 用 ``scope not in scopes.split()`` 判断，所以这里用空格分隔。
        禁用的用户返回空串（S6 还会再显式拦一次，这里是双保险）。
        """
        user = await self.get_user(user_id)
        if not user or user.get("disabled_at"):
            return ""
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT DISTINCT rp.perm_id "
            "FROM security_user_roles ur "
            "JOIN security_role_permissions rp ON ur.role_id = rp.role_id "
            "WHERE ur.user_id = ? "
            "ORDER BY rp.perm_id",
            (user_id,),
        )
        return " ".join(r["perm_id"] for r in cur.fetchall())

    # ====== 认证 Session ======

    async def create_auth_session(
        self,
        user_id: str,
        *,
        scope: str = "",
        user_agent: str | None = None,
        ip: str | None = None,
        sliding_days: int = 7,
        absolute_days: int = 30,
    ) -> tuple[str, dict[str, Any]]:
        """创建登录会话。返回 (明文 token, session_row)。

        **明文 token 只在返回值里出现一次**，库里只存 SHA-256。
        """
        session_id = secrets.token_hex(16)
        raw_token = "ses_" + _b62(SESSION_TOKEN_BYTES)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        await asyncio.to_thread(
            self._exec,
            "INSERT INTO security_auth_sessions"
            "(session_id, user_id, token_hash, user_agent, ip, scope, created_at, "
            " last_used_at, absolute_expires_at, sliding_expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, user_id, token_hash, user_agent, ip, scope,
                now_iso, now_iso,
                (now + timedelta(days=absolute_days)).isoformat(),
                (now + timedelta(days=sliding_days)).isoformat(),
            ),
        )
        row = await self.get_auth_session(session_id)
        return raw_token, row or {}

    async def get_auth_session(self, session_id: str) -> dict[str, Any] | None:
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM security_auth_sessions WHERE session_id = ?", (session_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None

    async def get_auth_session_by_token(self, raw_token: str) -> dict[str, Any] | None:
        """按明文 token 查 session。SHA-256 比对，安全性来自 token 本身的熵。"""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM security_auth_sessions WHERE token_hash = ?", (token_hash,),
        )
        r = cur.fetchone()
        return dict(r) if r else None

    async def list_auth_sessions(
        self, user_id: str | None = None, *, active_only: bool = True
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM security_auth_sessions"
        clauses, params = [], []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if active_only:
            clauses.append("revoked_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_used_at DESC"
        cur = await asyncio.to_thread(self._exec, sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    async def extend_sliding_expiry(
        self, session_id: str, *, sliding_days: int = 7
    ) -> str | None:
        """滑动续期。返回新的 sliding_expires_at。"""
        new_exp = (datetime.now(timezone.utc) + timedelta(days=sliding_days)).isoformat()
        await asyncio.to_thread(
            self._exec,
            "UPDATE security_auth_sessions SET sliding_expires_at = ? WHERE session_id = ?",
            (new_exp, session_id),
        )
        return new_exp

    async def revoke_auth_session(
        self,
        session_id: str,
        *,
        revoked_by_user_id: str | None = None,
        reason: str = "",
    ) -> bool:
        cur = await asyncio.to_thread(
            self._exec,
            "UPDATE security_auth_sessions "
            "SET revoked_at = ?, revoked_by_user_id = ?, revoked_reason = ? "
            "WHERE session_id = ? AND revoked_at IS NULL",
            (_now_iso(), revoked_by_user_id, reason, session_id),
        )
        return cur.rowcount > 0

    async def revoke_all_sessions(
        self,
        user_id: str,
        *,
        except_session_id: str | None = None,
        revoked_by_user_id: str | None = None,
        reason: str = "",
    ) -> int:
        """吊销某用户全部 session。``except_session_id`` 用于「注销其他设备」保留当前。"""
        now = _now_iso()
        if except_session_id:
            cur = await asyncio.to_thread(
                self._exec,
                "UPDATE security_auth_sessions "
                "SET revoked_at = ?, revoked_by_user_id = ?, revoked_reason = ? "
                "WHERE user_id = ? AND revoked_at IS NULL AND session_id != ?",
                (now, revoked_by_user_id, reason, user_id, except_session_id),
            )
        else:
            cur = await asyncio.to_thread(
                self._exec,
                "UPDATE security_auth_sessions "
                "SET revoked_at = ?, revoked_by_user_id = ?, revoked_reason = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, revoked_by_user_id, reason, user_id),
            )
        return cur.rowcount

    async def touch_auth_session(self, session_id: str, *, throttle: bool = True) -> bool:
        """更新 last_used_at（节流）。返回是否真的落库。"""
        now = time.monotonic()
        last = _touch_cache.get(f"s:{session_id}", 0.0)
        if throttle and (now - last) < TOUCH_THROTTLE_SEC:
            return False
        _touch_cache.put(f"s:{session_id}", now, ttl=TOUCH_THROTTLE_SEC)
        await asyncio.to_thread(
            self._exec,
            "UPDATE security_auth_sessions SET last_used_at = ? WHERE session_id = ?",
            (_now_iso(), session_id),
        )
        return True

    # ====== API 令牌（PAT）======

    def generate_api_token(self) -> tuple[str, str, str, str]:
        """生成 PAT。返回 (明文 token, token_id, prefix, last4)。

        明文只在返回值里出现一次；库里存 argon2 hash + prefix（查询用）+ last4（UI 用）。

        格式：``pat_<12 位 prefix><22 位 secret>``
        - prefix 参与索引查询，所以必须是 token 明文的固定前段
        - secret 保证熵（62^22 ≈ 2.7×10^39）
        """
        prefix = _b62(PAT_PREFIX_LEN)
        secret = _b62(PAT_SECRET_LEN)
        raw = f"pat_{prefix}{secret}"
        token_id = "tok_" + _b62(12)
        return raw, token_id, prefix, raw[-4:]

    async def create_api_token(
        self,
        user_id: str,
        name: str,
        *,
        scopes: str = "",
        expires_in_days: int = 30,
    ) -> tuple[str, dict[str, Any]]:
        """创建 PAT。返回 (明文 token, token_row)。**明文只在这里出现一次**。"""
        raw, token_id, prefix, last4 = self.generate_api_token()
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO security_api_tokens"
            "(token_id, user_id, name, token_hash, prefix, last4, scopes, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id, user_id, name, hash_password(raw), prefix, last4,
                scopes, (now + timedelta(days=expires_in_days)).isoformat(),
                now.isoformat(),
            ),
        )
        row = await self.get_api_token(token_id)
        return raw, row or {}

    async def get_api_token(self, token_id: str) -> dict[str, Any] | None:
        cur = await asyncio.to_thread(
            self._exec, "SELECT * FROM security_api_tokens WHERE token_id = ?", (token_id,)
        )
        r = cur.fetchone()
        return dict(r) if r else None

    async def verify_api_token(self, raw_token: str) -> dict[str, Any] | None:
        """校验 PAT 明文。命中返回 token_row，否则 None。

        流程：取 prefix → 按索引查候选行 → argon2 verify → 60s 缓存结果。
        argon2 无法按 hash 查询（每次 salt 不同），所以必须靠 prefix 缩小范围。

        性能（D20）：verify 约 50-100ms，缓存命中后可忽略。
        撤销 / 轮换 / 用户禁用都会调 ``invalidate_pat_cache`` 主动失效。
        """
        if not raw_token.startswith("pat_") or len(raw_token) < 4 + PAT_PREFIX_LEN:
            return None

        token_sha = hashlib.sha256(raw_token.encode()).hexdigest()
        cached_id = _pat_cache.get(token_sha)
        if cached_id:
            row = await self.get_api_token(cached_id)
            if row and not row.get("revoked_at"):
                return row
            _pat_cache.pop(token_sha, None)

        prefix = raw_token[4:4 + PAT_PREFIX_LEN]
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM security_api_tokens WHERE prefix = ?", (prefix,),
        )
        # prefix 有唯一性保证但没建 UNIQUE 约束，理论上可能多行 → 逐个 verify
        for candidate in cur.fetchall():
            row = dict(candidate)
            if not verify_password(row["token_hash"], raw_token):
                continue
            if row.get("revoked_at"):
                return None
            _pat_cache.put(token_sha, row["token_id"], ttl=PAT_CACHE_TTL_SEC)
            return row
        return None

    async def list_api_tokens(
        self, user_id: str | None = None, *, active_only: bool = True
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM security_api_tokens"
        clauses, params = [], []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if active_only:
            clauses.append("revoked_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        cur = await asyncio.to_thread(self._exec, sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    async def revoke_api_token(
        self,
        token_id: str,
        *,
        revoked_by_user_id: str | None = None,
        reason: str = "",
    ) -> bool:
        """撤销 PAT（不可恢复）。"""
        cur = await asyncio.to_thread(
            self._exec,
            "UPDATE security_api_tokens "
            "SET revoked_at = ?, revoked_by_user_id = ?, revoked_reason = ? "
            "WHERE token_id = ? AND revoked_at IS NULL",
            (_now_iso(), revoked_by_user_id, reason, token_id),
        )
        if cur.rowcount > 0:
            # 明文不在库里，无法反推缓存 key（token 的 SHA-256），只能按 value 反查
            _pat_cache.pop_by_value(token_id)
        return cur.rowcount > 0

    async def revoke_all_api_tokens(
        self,
        user_id: str,
        *,
        revoked_by_user_id: str | None = None,
        reason: str = "",
    ) -> int:
        cur = await asyncio.to_thread(
            self._exec,
            "UPDATE security_api_tokens "
            "SET revoked_at = ?, revoked_by_user_id = ?, revoked_reason = ? "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (_now_iso(), revoked_by_user_id, reason, user_id),
        )
        if cur.rowcount > 0:
            _pat_cache.clear()
        return cur.rowcount

    async def touch_api_token(
        self, token_id: str, ip: str | None = None, *, throttle: bool = True
    ) -> bool:
        """更新 last_used_at / last_used_ip（节流）。"""
        now = time.monotonic()
        last = _touch_cache.get(f"t:{token_id}", 0.0)
        if throttle and (now - last) < TOUCH_THROTTLE_SEC:
            return False
        _touch_cache.put(f"t:{token_id}", now, ttl=TOUCH_THROTTLE_SEC)
        await asyncio.to_thread(
            self._exec,
            "UPDATE security_api_tokens SET last_used_at = ?, last_used_ip = ? "
            "WHERE token_id = ?",
            (_now_iso(), ip, token_id),
        )
        return True

    # ====== 登录失败限流（数据层；策略见 S5）======

    async def get_login_attempt(self, key: str) -> dict[str, Any] | None:
        cur = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM security_login_attempts WHERE key = ?", (key,),
        )
        r = cur.fetchone()
        return dict(r) if r else None

    async def is_login_locked(self, key: str) -> bool:
        row = await self.get_login_attempt(key)
        if not row or not row.get("locked_until"):
            return False
        return datetime.fromisoformat(row["locked_until"]) > datetime.now(timezone.utc)

    async def record_login_failure(
        self,
        key: str,
        *,
        max_failures: int,
        window_sec: int = LOGIN_WINDOW_SEC,
        lock_sec: int = LOGIN_LOCK_SEC,
    ) -> bool:
        """记一次失败。返回本次是否触发锁定。

        超过统计窗口的旧计数自动重置，避免"一年前失败过 3 次"一直累加。
        """
        now = datetime.now(timezone.utc)
        row = await self.get_login_attempt(key)

        if row is None:
            failures = 1
            first_fail_at = now
        else:
            first_fail_at = datetime.fromisoformat(row["first_fail_at"])
            if (now - first_fail_at).total_seconds() > window_sec:
                failures, first_fail_at = 1, now      # 窗口过期，重新开始计
            else:
                failures = int(row["failures"]) + 1

        locked_until = None
        if failures >= max_failures:
            locked_until = (now + timedelta(seconds=lock_sec)).isoformat()

        await asyncio.to_thread(
            self._exec,
            "INSERT INTO security_login_attempts(key, failures, first_fail_at, locked_until) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET failures = ?, first_fail_at = ?, locked_until = ?",
            (key, failures, first_fail_at.isoformat(), locked_until,
             failures, first_fail_at.isoformat(), locked_until),
        )
        return locked_until is not None

    async def lock_user_login(self, username: str, *, lock_sec: int) -> str:
        """运维手动锁定某用户的**登录**（不吊销已有凭证）。返回 locked_until。

        复用 ``security_login_attempts`` + ``user:<username>`` 键，登录路径的
        ``check_login_rate_limit`` 天然会命中，不需要给 ``users`` 表加列。

        只挡"新登录"：已签发的 session / PAT 仍然可用，直到自然过期。
        要立刻踢人请调 ``revoke_all_sessions`` / ``revoke_all_api_tokens``
        （S8 的 ``/users/{id}/revoke-all``）。
        """
        now = datetime.now(timezone.utc)
        locked_until = (now + timedelta(seconds=lock_sec)).isoformat()
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO security_login_attempts(key, failures, first_fail_at, locked_until) "
            "VALUES (?, 0, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET locked_until = ?",
            (f"user:{username}", now.isoformat(), locked_until, locked_until),
        )
        return locked_until

    async def unlock_user_login(self, username: str) -> bool:
        """提前解除手动锁定。整行删掉（连带清空失败计数），给用户一个干净起点。

        只清 ``locked_until`` 不行：若当时 failures 已到 4，下一次失败就立刻
        重新锁上，运维会觉得"解锁没生效"。
        """
        cur = await asyncio.to_thread(
            self._exec,
            "DELETE FROM security_login_attempts WHERE key = ?",
            (f"user:{username}",),
        )
        return cur.rowcount > 0

    async def reset_login_attempts(self, *keys: str) -> None:
        """登录成功后清空失败计数。"""
        if not keys:
            return
        placeholders = ",".join("?" * len(keys))
        await asyncio.to_thread(
            self._exec,
            f"DELETE FROM security_login_attempts WHERE key IN ({placeholders})",
            tuple(keys),
        )

    async def cleanup_login_attempts(self, *, older_than_sec: int = 86400) -> int:
        """清理过期失败计数，防止表无限增长（方案 §9）。

        两种行该清：
          1. 未锁定且超过统计窗口的（普通失败计数）
          2. **锁定已过期的**（locked_until 是过去时间，但字段非 NULL）
             —— 只判 `locked_until IS NULL` 会让这些行永久残留，被锁过的
             IP / 用户名会一直在表里，是实打实的泄漏。
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_sec)).isoformat()
        cur = await asyncio.to_thread(
            self._exec,
            "DELETE FROM security_login_attempts "
            "WHERE first_fail_at < ? AND (locked_until IS NULL OR locked_until < ?)",
            (cutoff, cutoff),
        )
        return cur.rowcount

    # ====== 在线状态（§2.5）======

    @staticmethod
    def is_online(row: dict[str, Any], *, within_sec: int = 300) -> bool:
        """按 last_seen_at 判断是否在线（默认 5 分钟内有活动）。"""
        last = row.get("last_seen_at")
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except (TypeError, ValueError):
            return False
        return (datetime.now(timezone.utc) - last_dt).total_seconds() <= within_sec
