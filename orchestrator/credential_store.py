"""CredentialStore — 用户表单录入的 API Key 加密存储。

设计：
- Fernet (AES-128-CBC + HMAC-SHA256) 对称加密
- master_key 存 ~/.agentops/master_key（文件权限 0600）
- 只用于前端表单录入路径，不影响 models.yaml 的 ${ENV_VAR} 路径
- ModelConfig 优先查 CredentialStore，未找到回退 models.yaml

安全模型：
- 威胁：攻击者拿到 audit.db / 源码 → 无法解密 api_key（无 master_key）
- 局限：攻击者拿到 master_key 文件 + audit.db → 可解密（单机场景可接受）
- 不防御：内存中的明文 key（Python 进程可读）
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# master_key 存储路径
_MASTER_KEY_PATH = Path.home() / ".agentops" / "master.key"
# 凭证数据库路径
_CREDENTIAL_DB_PATH = Path.home() / ".agentops" / "credentials.db"

# 主机类凭据前缀（区别于模型供应商 api_key），按前缀归类 kind
_HOST_CREDENTIAL_PREFIXES = ("ssh:", "mysql:", "pg:", "mssql:")


def _classify_credential_kind(provider_id: str) -> str:
    """按 provider_id 前缀分类凭据 kind。

    - "ssh:" / "mysql:" / "pg:" / "mssql:" 等主机类前缀 → 返回前缀名（如 "ssh" / "mysql"）
    - 其他（无前缀，如 "minimax"）→ "provider"（模型供应商 api_key）
    """
    for prefix in _HOST_CREDENTIAL_PREFIXES:
        if provider_id.startswith(prefix):
            return prefix.rstrip(":")
    return "provider"


def _ensure_key() -> bytes:
    """获取或生成 master_key（首次调用自动生成）。"""
    _MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _MASTER_KEY_PATH.exists():
        return _MASTER_KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    _MASTER_KEY_PATH.write_bytes(key)
    # Windows 不支持 chmod 0600，但 try 一下（Linux/Mac 生效）
    try:
        os.chmod(_MASTER_KEY_PATH, 0o600)
    except OSError:
        pass
    logger.info("Generated new master key at %s", _MASTER_KEY_PATH)
    return key


class CredentialStore:
    """加密存储用户表单录入的 API Key。

    用法：
        store = CredentialStore()
        store.store("minimax", "sk-xxx123")
        key = store.get("minimax")  # → "sk-xxx123"
    """

    def __init__(self, db_path: Path | None = None, key_path: Path | None = None):
        self._db_path = str(db_path or _CREDENTIAL_DB_PATH)
        self._key_path = key_path or _MASTER_KEY_PATH
        self._fernet = Fernet(_ensure_key())
        self._init_db()

    def _init_db(self) -> None:
        """初始化 credentials.db。"""
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                provider_id TEXT PRIMARY KEY,
                encrypted_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def store(self, provider_id: str, api_key: str) -> None:
        """加密存储 api_key。"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        encrypted = self._fernet.encrypt(api_key.encode()).decode()
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR REPLACE INTO credentials (provider_id, encrypted_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (provider_id, encrypted, now, now),
        )
        conn.commit()
        conn.close()
        logger.info("Stored credential for provider '%s'", provider_id)

    def get(self, provider_id: str) -> str | None:
        """解密获取 api_key。未找到返回 None。"""
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT encrypted_key FROM credentials WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        try:
            return self._fernet.decrypt(row[0].encode()).decode()
        except Exception as e:
            logger.error("Failed to decrypt credential for '%s': %s", provider_id, e)
            return None

    def delete(self, provider_id: str) -> bool:
        """删除凭证。返回是否删除了记录。"""
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute(
            "DELETE FROM credentials WHERE provider_id = ?",
            (provider_id,),
        )
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
        return deleted

    def list_providers(self) -> list[dict[str, Any]]:
        """列出所有已存储凭证的 provider（不含明文 key）。

        kind 字段区分用途：
        - "provider": 模型供应商 api_key（原有用途）
        - "ssh": SSH 主机凭据（log-puller / ssh_exec 复用，id 形如 ssh:<source_id>）
        - "mysql" / "pg" / "mssql": 数据库主机凭据（id 形如 mysql:<connection_id> 等）
        """
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT provider_id, created_at, updated_at FROM credentials ORDER BY provider_id"
        ).fetchall()
        conn.close()
        return [
            {
                "provider_id": r[0],
                "kind": _classify_credential_kind(r[0]),
                "created_at": r[1],
                "updated_at": r[2],
            }
            for r in rows
        ]


# 单例
_credential_store: CredentialStore | None = None


def get_credential_store() -> CredentialStore:
    """获取全局 CredentialStore 单例。"""
    global _credential_store
    if _credential_store is None:
        _credential_store = CredentialStore()
    return _credential_store
