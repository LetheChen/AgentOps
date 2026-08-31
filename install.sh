#!/usr/bin/env bash
# ============================================================
# AgentOps — Docker 一键安装脚本 (Linux / macOS)
# ============================================================
#
# 用途：在已安装 Docker 的机器上一键拉起 AgentOps 单容器全栈
#       （nginx 前端 + uvicorn 后端 + 自动建表 + admin 账号）。

# 用法：
#   curl -fsSL https://raw.githubusercontent.com/LetheChen/AgentOps/main/install.sh | bash
#   # 或本地：
#   ./install.sh

# 环境变量（可选，在执行前 export）：
#   AGENTOPS_VERSION       镜像 tag（默认 latest）
#   AGENTOPS_WEB_PORT      Web 端口（默认 80；单容器仅暴露 80）
#   AGENTOPS_DATA_DIR      数据卷 host 路径（默认 ./data）
#   SKIP_BUILD             设为 1 跳过 docker build（用现成镜像）
# ============================================================

set -euo pipefail

# ── 颜色与日志 ─────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[AgentOps]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── 0. 前置检查 ─────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "docker 未安装。请先安装 Docker：https://docs.docker.com/engine/install/"

if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    die "未检测到 docker compose。请安装 docker compose plugin 或 docker-compose。"
fi
ok "检测到 $COMPOSE_CMD"

# ── 1. 解析项目根目录 ─────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
log "项目目录: $SCRIPT_DIR"

# ── 2. 环境配置：复制 .env.example → .env ──────────────
if [ ! -f .env ]; then
    log "未检测到 .env，从 .env.example 复制"
    cp .env.example .env
    warn "已创建 .env，请根据需要填入 API Key 等敏感配置（生产前必填）"
    warn "按任意键继续（默认配置即可启动），或 Ctrl+C 退出先编辑 .env"
    if [ -t 0 ]; then read -r -n 1 _; fi
else
    ok ".env 已存在"
fi

# ── 3. 创建 host 数据卷目录（若用 bind mount） ──────
AGENTOPS_DATA_DIR="${AGENTOPS_DATA_DIR:-$SCRIPT_DIR/data}"
if [ ! -d "$AGENTOPS_DATA_DIR" ]; then
    mkdir -p "$AGENTOPS_DATA_DIR"
    ok "创建数据目录: $AGENTOPS_DATA_DIR"
fi

# ── 4. 构建镜像 ──────────────────────────────────────
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    log "跳过构建（SKIP_BUILD=1），将直接拉取镜像"
    $COMPOSE_CMD pull agentops 2>/dev/null || warn "拉取镜像失败，将使用本地镜像"
else
    log "构建单镜像 agentops:latest（首次约 5-10 分钟）"
    docker build -t agentops:latest -f docker/agentops/Dockerfile . \
        || die "镜像构建失败"
    ok "镜像构建完成"
fi

# ── 5. 启动主服务（首次启动会自动建表 + admin 账号）─────
log "启动 AgentOps 单容器全栈（后台运行）"
$COMPOSE_CMD up -d agentops || die "服务启动失败"

# ── 7. 等待服务就绪 ──────────────────────────────────
log "等待健康检查通过（最多 60 秒）"
READY=0
for i in $(seq 1 30); do
    if curl -fsS http://localhost:${AGENTOPS_WEB_PORT:-80}/ >/dev/null 2>&1; then
        READY=1; break
    fi
    sleep 2
done

if [ "$READY" = "1" ]; then
    ok "AgentOps 全栈已就绪"
else
    warn "健康检查超时，可执行 $COMPOSE_CMD logs agentops 查看日志"
fi

# ── 8. 输出访问信息 ──────────────────────────────────
WEB_PORT="${AGENTOPS_WEB_PORT:-80}"

cat <<EOF

${GREEN}╔══════════════════════════════════════════════════╗
║         AgentOps 启动成功 🎉                     ║
╚══════════════════════════════════════════════════╝${NC}

访问入口（单端口 80，前后端全栈合一）：
  • 前端 UI   : http://localhost:${WEB_PORT}
  • API       : http://localhost:${WEB_PORT}/api/...
  • Swagger   : http://localhost:${WEB_PORT}/docs

默认账号（仅 .env 含 AGENTOPS_BOOTSTRAP_PASSWORD 时启用，否则随机）：
  • 用户名    : admin
  • 密码      : 见 .env 配置，或 data/bootstrap-password.txt

常用命令：
  • 查看日志  : $COMPOSE_CMD logs -f agentops
  • 停止      : $COMPOSE_CMD down
  • 重启      : $COMPOSE_CMD restart
  • 升级镜像  : $COMPOSE_CMD pull && $COMPOSE_CMD up -d

可选 worker（容器化子 agent，需 docker-in-docker）：
  $COMPOSE_CMD --profile worker up -d

EOF
