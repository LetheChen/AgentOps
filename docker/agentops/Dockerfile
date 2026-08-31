# ===== AgentOps 单镜像（前后端合并） =====
#
# 设计：
#   - 单镜像同时包含 Python 后端 (uvicorn :1987) + 前端 dist (nginx :80)
#   - nginx 在 80 端口提供：前端 SPA 静态托管 + /api 反代到 127.0.0.1:1987
#   - 主进程 start.sh 后台拉起 uvicorn + 前台拉起 nginx；SIGTERM 优雅停止
#   - 容器对外只暴露 80 端口，访问 localhost 即可全栈
#
# 多阶段构建：
#   - web-builder : Node 22 → pnpm/npm install + vite build
#   - py-builder  : Python 3.12-slim → 安装运行时依赖（pip cache 留在这层）
#   - runtime     : Python 3.12-slim + nginx 1.27-alpine 安装 + nginx.conf + 启动脚本
#
# 构建命令（在项目根目录）：
#   docker build -t agentops:latest -f docker/agentops/Dockerfile .

# ----------------------------------------------------------------------
# Stage 1: web-builder — 构建前端 dist
# ----------------------------------------------------------------------
FROM node:22-alpine AS web-builder

WORKDIR /build

# 阿里云 npm 镜像（中国大陆构建加速；海外构建可改为 registry.npmjs.org）
RUN npm config set registry https://registry.npmmirror.com

# 单独复制依赖清单，最大化利用层缓存
COPY web/package.json web/package-lock.json* ./

RUN npm install --no-audit --no-fund

# 复制源码并构建
COPY web/ ./
RUN npm run build

# ----------------------------------------------------------------------
# Stage 2: py-builder — 安装 Python 运行时依赖
# ----------------------------------------------------------------------
FROM python:3.12-slim AS py-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# 系统依赖：build-essential 编译 argon2-cffi / cryptography / paramiko
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential gcc libffi-dev libssl-dev git curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

# 安装运行时依赖（base 套件，不含 [knowledge] 扩展，避免 whisper/torch 体积）
RUN pip install --upgrade pip && \
    pip install \
        "pydantic>=2.7" \
        "pyyaml>=6.0" \
        "httpx>=0.27" \
        "asyncio-throttle>=1.0.2" \
        "typer>=0.12" \
        "rich>=13.7" \
        "anyio>=4.4" \
        "python-dotenv>=1.0" \
        "psutil>=5.9" \
        "docker>=7.1.0,<8" \
        "paramiko>=3.4" \
        "argon2-cffi>=23.1" \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "python-multipart>=0.0.12" \
        "websockets>=13.0" \
        "aiofiles>=24.1" \
        "cryptography>=43.0" \
        "pyjwt>=2.9"

# ----------------------------------------------------------------------
# Stage 3: runtime — Python 后端 + nginx 前端
# ----------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENTOPS_HOME=/app/data \
    AUDIT_DB_PATH=/app/data/audit.db

# 运行时系统依赖：curl (healthcheck) + nginx + tini (pid 1 收僵尸) + supervisor 改用自写脚本
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl tini nginx && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 1000 agentops && \
    useradd --system --uid 1000 --gid agentops --home /app --shell /bin/bash agentops

# 从 py-builder 复制 site-packages
COPY --from=py-builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=py-builder /usr/local/bin /usr/local/bin

# 前端 dist（web-builder 产物）
COPY --from=web-builder /build/dist /usr/share/nginx/html

# nginx 配置（SPA fallback + /api 反代到 127.0.0.1:1987）
COPY docker/agentops/nginx.conf /etc/nginx/conf.d/default.conf
# 禁用 nginx 默认站点（避免 listen 80 冲突）
RUN rm -f /etc/nginx/sites-enabled/default

WORKDIR /app

# 复制业务代码（与原 api 镜像一致）
COPY --chown=agentops:agentops api/              ./api/
COPY --chown=agentops:agentops orchestrator/     ./orchestrator/
COPY --chown=agentops:agentops harness/          ./harness/
COPY --chown=agentops:agentops workflow/         ./workflow/
COPY --chown=agentops:agentops audit/            ./audit/
COPY --chown=agentops:agentops task/             ./task/
COPY --chown=agentops:agentops tools/            ./tools/
COPY --chown=agentops:agentops skills/           ./skills/
COPY --chown=agentops:agentops config/           ./config/
COPY --chown=agentops:agentops workflows/        ./workflows/
COPY --chown=agentops:agentops pyproject.toml cli.py ./

# 数据目录
RUN mkdir -p /app/data /app/logs /app/workspace && \
    chown -R agentops:agentops /app

# 启动脚本 + init 脚本
COPY --chown=agentops:agentops docker/agentops/start.sh /start.sh
COPY --chown=agentops:agentops docker/agentops/init_admin.py /docker-init/init_admin.py
RUN chmod +x /start.sh

USER agentops

# 仅暴露 80（前端 + /api 反代到容器内 :1987）
EXPOSE 80

# tini 收僵尸进程；SIGTERM 由 start.sh 转发给 nginx + uvicorn
ENTRYPOINT ["/usr/bin/tini", "--", "/start.sh"]

# 默认前台启动（compose 不覆盖）
CMD []

# 健康检查：GET /（nginx 健康路由）即可同时验证前后端
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1/ || exit 1
