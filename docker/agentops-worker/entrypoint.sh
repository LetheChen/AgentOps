#!/bin/bash
# P0.18 agentops-worker entrypoint
# 启动 node-bridge WS server，codex 在 Manager 通过 WS 发 start 消息后才正式跑。

set -e

cd /opt/agentops-worker

# 准备 workspace 目录（如果 bind mount 失败，至少保证目录存在）
mkdir -p /workspace || true

export NODE_BRIDGE_PORT
export WORKSPACE
export PATH="/opt/agentops-worker/node_modules/.bin:${PATH}"

# 启动 WS server
exec node bridge.js
