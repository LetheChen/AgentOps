#!/bin/bash
# ============================================================
# AgentOps 单镜像统一启动脚本（容器内由 tini 调用）
# ============================================================
#
# 职责：
#   1. 加载 .env（若存在）
#   2. 准备数据目录
#   3. 首次启动：自动建表 + 创建 admin 账号（幂等）
#   4. 后台启动 uvicorn (127.0.0.1:1987)
#   5. 前台启动 nginx (0.0.0.0:80) — 主进程，SIGTERM 触发优雅停止
#
# 关键环境变量：
#   AGENTOPS_HOME            数据目录（默认 /app/data）
#   AUDIT_DB_PATH            SQLite 数据库路径（默认 /app/data/audit.db）
#   AGENTOPS_WEB_PORT        nginx 监听端口（默认 80；容器内修改需重 build）
#   AGENTOPS_BOOTSTRAP_USERNAME / AGENTOPS_BOOTSTRAP_PASSWORD
#                            首次启动自动创建的 admin 账号
#                            留空 → 自动生成 16 位随机密码写到
#                            ${AGENTOPS_HOME}/bootstrap-password.txt
#   LOG_LEVEL                uvicorn 日志级别（默认 info）
#   AGENTOPS_SKIP_BOOTSTRAP  设为 1 跳过 init_admin（init 已运行时）
# ============================================================

set -e

# ── 0. 加载 .env ─────────────────────────────────────────
if [ -f /app/.env ]; then
    echo "[start.sh] loading /app/.env"
    set -a
    # shellcheck disable=SC1091
    . /app/.env
    set +a
elif [ -f /.env ]; then
    echo "[start.sh] loading /.env"
    set -a
    # shellcheck disable=SC1091
    . /.env
    set +a
fi

# ── 1. 准备数据目录 ───────────────────────────────────────
mkdir -p "${AGENTOPS_HOME:-/app/data}"
mkdir -p /app/logs
mkdir -p /app/workspace

if [ -n "${AUDIT_DB_PATH}" ]; then
    mkdir -p "$(dirname "${AUDIT_DB_PATH}")"
fi

# ── 2. 首次启动：建表 + admin 账号（幂等）────────────────
if [ "${AGENTOPS_SKIP_BOOTSTRAP:-0}" = "1" ]; then
    echo "[start.sh] AGENTOPS_SKIP_BOOTSTRAP=1 → 跳过 init_admin"
else
    echo "[start.sh] 首次启动：建表 + admin 账号（幂等操作）"
    python /docker-init/init_admin.py || {
        echo "[start.sh][WARN] init_admin 失败（数据库可能已有），继续启动"
    }
fi

# ── 3. 优雅停止：SIGTERM/SIGINT 转发给子进程 ─────────────
shutdown() {
    echo "[start.sh] received shutdown signal, stopping..."
    # 优先停止 nginx（停止接收新连接）
    if [ -n "${NGINX_PID:-}" ] && kill -0 "${NGINX_PID}" 2>/dev/null; then
        kill -TERM "${NGINX_PID}" 2>/dev/null || true
        wait "${NGINX_PID}" 2>/dev/null || true
    fi
    # 再停 uvicorn（处理已建立的请求）
    if [ -n "${UVICORN_PID:-}" ] && kill -0 "${UVICORN_PID}" 2>/dev/null; then
        kill -TERM "${UVICORN_PID}" 2>/dev/null || true
        wait "${UVICORN_PID}" 2>/dev/null || true
    fi
    exit 0
}
trap shutdown SIGTERM SIGINT

# ── 4. 后台启动 uvicorn（API :1987）────────────────────────
echo "[start.sh] starting uvicorn on 127.0.0.1:1987"
echo "[start.sh]   AGENTOPS_HOME=${AGENTOPS_HOME:-/app/data}"
echo "[start.sh]   AUDIT_DB_PATH=${AUDIT_DB_PATH:-/app/data/audit.db}"

python -u -m uvicorn api.server:app \
    --host 127.0.0.1 \
    --port 1987 \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --log-level "${LOG_LEVEL:-info}" &
UVICORN_PID=$!

# 等待 uvicorn ready（最多 30 秒）
for i in {1..30}; do
    if curl -fsS http://127.0.0.1:1987/ >/dev/null 2>&1; then
        echo "[start.sh] uvicorn ready (after ${i}s)"
        break
    fi
    if ! kill -0 "${UVICORN_PID}" 2>/dev/null; then
        echo "[start.sh][ERROR] uvicorn exited early"
        exit 1
    fi
    sleep 1
done

# ── 5. 前台启动 nginx（Web :80，主进程）──────────────────
echo "[start.sh] starting nginx on 0.0.0.0:80 (foreground)"

# nginx 必须以 root 绑定 80 端口（agentops 用户无法 bind < 1024）
# 启动后用 sed 把 worker 进程切到 agentops 用户（master 保持 root 以便 reload）
nginx -g 'daemon off;' &
NGINX_PID=$!

# 等待 nginx 完全就绪（PID 出现 + 端口监听）
for i in {1..10}; do
    if curl -fsS http://127.0.0.1/ >/dev/null 2>&1; then
        echo "[start.sh] nginx ready (after ${i}s)"
        break
    fi
    sleep 1
done

echo "[start.sh] AgentOps 全栈启动完成"
echo "[start.sh]   前端入口: http://localhost/"
echo "[start.sh]   API      : http://localhost/api/..."
echo "[start.sh]   Swagger  : http://localhost/docs"

# 等待任一子进程退出则触发 shutdown
wait -n "${UVICORN_PID}" "${NGINX_PID}" || true

echo "[start.sh] 一个子进程已退出，触发整体 shutdown"
shutdown
