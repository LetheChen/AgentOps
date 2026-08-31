# P0.18 agentops-worker 镜像：容器内跑 codex app-server + node-bridge WS server。
#
# 镜像重命名（P0.18）：codex-node → agentops-worker
# 原因：worker 应能跑任意 backend（codex / local_llm / claude_code / opencode / future），
# 命名不该绑定具体 harness。参考 docs/audit/docker-worker-architecture-comparison-2026-08-11.md §4.1
#
# Base: node:22-slim（与 agentops-worker 镜像对齐）。
# 镜像内 codex 版本与 host 端 @openai/codex@0.144.5 保持一致。
# 镜像内 WS server 端口固定 7891；host 端口由 Docker -p 动态分配（运行时查询 container.ports）。
#
# Build context：项目根目录（AgentOps/），构建命令：
#   docker build -t agentops-worker:latest -f docker/agentops-worker/Dockerfile .

FROM node:22-slim

# Aliyun apt mirror
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      bash ca-certificates curl git openssh-client python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agentops-worker

# npm install：把 @openai/codex-linux-x64 显式 alias 到 version-tagged 包，
# 确保镜像内必有 linux x64_64 平台二进制（@openai/codex wrapper 通过 PLATFORM_PACKAGE_BY_TARGET 查找）。
COPY docker/agentops-worker/package.json ./
RUN npm config set registry https://registry.npmmirror.com && \
    npm install --omit=dev --no-audit --no-fund && \
    ls -la node_modules/.bin/ && \
    echo "--- bin/codex.js ---" && \
    cat node_modules/@openai/codex/bin/codex.js | head -5 && \
    echo "--- testing codex via npx ---" && \
    npx --no-install codex --version

COPY docker/agentops-worker/bridge.js /opt/agentops-worker/bridge.js
COPY docker/agentops-worker/entrypoint.sh /opt/agentops-worker/entrypoint.sh
RUN chmod +x /opt/agentops-worker/entrypoint.sh

# 容器内 WS 固定监听端口
ENV NODE_BRIDGE_PORT=7891
ENV WORKSPACE=/workspace
ENV PATH="/opt/agentops-worker/node_modules/.bin:${PATH}"
# 注意：以下 labels 仅作为 hint，运行时由 docker build --label 注入最新值
LABEL agentops.kind="agentops-worker" \
      agentops.version="0.1.0" \
      agentops.protocol_version="0.1.0"

ENTRYPOINT ["/opt/agentops-worker/entrypoint.sh"]
