#!/usr/bin/env bash
# ============================================================
# AgentOps — 本地源码一键安装脚本 (Linux / macOS)
# ============================================================
#
# 用途：不依赖 Docker，直接在主机上跑后端 + 前端。
#       适合开发调试 / 无 Docker 场景。
#
# 用法：
#   ./install-local.sh
#
# 依赖：Python 3.11+ / Node 20+ / npm
# ============================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[AgentOps]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[X]${NC} $*" >&2; exit 1; }

# ── 0. 前置检查 ─────────────────────────────────────
command -v python3 >/dev/null 2>&1 || die "python3 未安装"
command -v node    >/dev/null 2>&1 || die "node 未安装（需 Node 20+）"
command -v npm     >/dev/null 2>&1 || die "npm 未安装"

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
NODE_VER=$(node -v | tr -d 'v')
log "Python $PY_VER / Node $NODE_VER"

# ── 1. 项目根目录 ──────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 2. Python 虚拟环境 ─────────────────────────────
if [ ! -d .venv ]; then
    log "创建 Python 虚拟环境 .venv"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
ok "激活虚拟环境"

log "升级 pip + 安装 Python 依赖"
pip install --upgrade pip --quiet
pip install -e . --quiet
ok "Python 依赖已安装"

# ── 3. .env ────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    warn "已创建 .env（默认配置）；如需企业微信推送 / 大模型 API Key 请编辑 .env"
else
    ok ".env 已存在"
fi

# ── 4. 前端依赖 ────────────────────────────────────
if [ ! -d web/node_modules ]; then
    log "安装前端依赖（首次约 2-5 分钟）"
    pushd web >/dev/null
    npm install --no-audit --no-fund
    popd >/dev/null
    ok "前端依赖已安装"
else
    ok "前端依赖已存在"
fi

# ── 5. 数据库初始化 ───────────────────────────────
log "初始化 audit.db（如未存在）"
python -c "from audit import SqliteEventStore; s = SqliteEventStore('audit.db'); print(f'[init] audit.db ready: {s.db_path}')"
ok "数据库就绪"

# ── 6. 启动服务 ────────────────────────────────────
log "启动后端（端口 1987）+ 前端（端口 5173）"

# 后端后台启动
mkdir -p logs
nohup python -u -m uvicorn api.server:app --host 0.0.0.0 --port 1987 \
    > logs/backend.log 2>&1 &
BACKEND_PID=$!
echo $BACKEND_PID > .backend.pid
log "后端 PID: $BACKEND_PID（日志: logs/backend.log）"

# 前端后台启动
pushd web >/dev/null
nohup npm run dev -- --host 0.0.0.0 > ../logs/frontend.log 2>&1 &
FRONTEND_PID=$!
echo $FRONTEND_PID > ../.frontend.pid
popd >/dev/null
log "前端 PID: $FRONTEND_PID（日志: logs/frontend.log）"

# ── 7. 等待就绪 ──────────────────────────────────
log "等待后端就绪（最多 60 秒）"
READY=0
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:1987/ >/dev/null 2>&1; then
        READY=1; break
    fi
    sleep 2
done

if [ "$READY" = "1" ]; then
    ok "后端已就绪"
else
    warn "后端健康检查超时，请查看 logs/backend.log"
fi

cat <<EOF

${GREEN}========================================
  AgentOps 本地启动成功
========================================${NC}

访问入口：
  - 前端 UI : http://localhost:5173
  - API     : http://localhost:1987
  - Swagger : http://localhost:1987/docs

停止服务：
  kill \$(cat .backend.pid .frontend.pid)

日志：
  tail -f logs/backend.log
  tail -f logs/frontend.log

EOF
