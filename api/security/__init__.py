"""安全认证访问 MVP 的后端实现（方案 docs/security-mvp-plan-2026-08-29.md）。

分层：
  - ``audit/security_schema.py``  DDL + 种子数据 + bootstrap（S1/S2/S3）
  - ``audit/security_store.py``   数据访问 Mixin（S4）
  - ``api/security/rate_limit.py`` 登录限流 + 恒定响应时间（S5）
  - ``api/security/deps.py``       FastAPI 鉴权依赖链（S6）
  - ``api/security/auth.py``       ``/api/auth/*`` 路由（S7）

本包只放"认证/授权"本身，业务权限点定义仍由 config 与 audit 层的
security_permissions 表决定。
"""

__all__ = ["rate_limit", "deps"]
