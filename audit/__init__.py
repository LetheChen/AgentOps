"""审计层：事件持久化 + 链路追踪 + 合规审计。

对外只暴露 EventStore 协议和 SqliteEventStore 实现。
"""
from audit.store import EventStore, SqliteEventStore

__all__ = ["EventStore", "SqliteEventStore"]
