"""Harness Adapter package"""
from .protocol import (
    AgentClient,
    AgentEvent,
    AgentEventType,
    AgentRunContext,
    AgentUsage,
    HarnessRegistry,
    HarnessType,
    PermissionSet,
    ToolDefinition,
)
from .register import register_builtin_harnesses

__all__ = [
    "AgentClient", "AgentEvent", "AgentEventType", "AgentRunContext", "AgentUsage",
    "HarnessRegistry", "HarnessType", "PermissionSet", "ToolDefinition",
    "register_builtin_harnesses",
]

# Auto-register built-in harnesses on import
register_builtin_harnesses()
