#!/usr/bin/env bash
# video-pipeline 容器节点手动启动 + 任务下发研究脚本
# 用途：绕过当前未接通的 manager→worker WS 架构，直接验证 bridge.js 能否跑通
# 前提：已构建 agentops-worker-video 镜像（含 mmx + hyperframes + ffmpeg）
# 用法：bash research-container-node.sh

set -e

IMAGE="agentops-worker-video:latest"
WORKSPACE="E:/Project/AgentOps/workspace/video-pipeline/run_20260823_195153_387296"
BRIDGE_PORT=7891

echo "=== 1. 检查镜像 ==="
docker images "$IMAGE" --format "{{.Repository}}:{{.Tag}} {{.Size}}" | head -3

echo ""
echo "=== 2. 启动容器（映射 7891 + 挂载 workspace + 注入 key）==="
# 注意：MINIMAX_API_KEY 通过 env 注入，bridge.js 的 start 消息里 openai_api_key 字段
# 会被 codex 子进程当 OPENAI_API_KEY 用（minimax 兼容 OpenAI 协议）
docker run -d --name video-research \
  -p ${BRIDGE_PORT}:7891 \
  -v "${WORKSPACE}:/workspace" \
  -v "E:/Project/AgentOps/web:/opt/agentops-worker/web:ro" \
  -v "E:/Project/AgentOps/tools:/opt/agentops-worker/tools:ro" \
  -v "E:/Project/AgentOps/config:/opt/agentops-worker/config:ro" \
  -e MINIMAX_API_KEY="${MINIMAX_API_KEY:-sk-your-key-here}" \
  -e OPENAI_BASE_URL="https://api.minimaxi.com/v1" \
  -e PYTHONIOENCODING=utf-8 \
  -w /workspace \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --tmpfs /tmp:size=500m \
  "$IMAGE"

echo "容器已启动，等 bridge.js WS server 起来..."
sleep 3

echo ""
echo "=== 3. 验证容器内 bridge.js 监听 7891 ==="
docker exec video-research sh -c 'netstat -tlnp 2>/dev/null || ss -tlnp 2>/dev/null || echo "no netstat/ss"'
docker logs video-research 2>&1 | head -10

echo ""
echo "=== 4. 用 Python WS client 发 start 消息（模拟 manager）==="
# Windows 可能没 wscat，用 Python websocket-client 替代
python -c "
import asyncio, json, sys
async def main():
    try:
        import websockets
    except ImportError:
        print('ERROR: pip install websockets'); sys.exit(1)
    uri = 'ws://localhost:${BRIDGE_PORT}/bridge'
    print(f'connecting {uri} ...')
    async with websockets.connect(uri) as ws:
        print('connected, sending start message...')
        start_msg = {
            'type': 'start',
            # bridge.js:61 把这个注入 codex env 当 OPENAI_API_KEY
            # minimax 兼容 OpenAI 协议，用 minimax key 即可
            'openai_api_key': '${MINIMAX_API_KEY:-sk-your-key-here}',
            'openai_base_url': 'https://api.minimaxi.com/v1',
            'model': 'MiniMax-M3',           # bridge.js:133 传给 codex thread/start
            'prompt': '你是调研员。请用一句话介绍瑞利散射。',  # bridge.js:144 传给 turn/start
            'tools': [],                     # bridge.js:145
            'sandbox': 'danger-full-access', # bridge.js:136
            # 'skill_roots': ['/workspace/skills'],  # bridge.js:123 可选
        }
        await ws.send(json.dumps(start_msg))
        print('start sent, receiving events (15s timeout)...')
        try:
            for _ in range(20):
                msg = await asyncio.wait_for(ws.recv(), timeout=15)
                parsed = json.loads(msg)
                t = parsed.get('type', '')
                if t == 'event':
                    ev = parsed.get('event', {})
                    et = ev.get('type', '')
                    print(f'  [event] {et}: {json.dumps(ev, ensure_ascii=False)[:200]}')
                    if et in ('done', 'error', 'ready'):
                        break
                elif t == 'stderr':
                    print(f'  [stderr] {parsed.get(\"line\", \"\")[:200]}')
                elif t == 'exit':
                    print(f'  [exit] code={parsed.get(\"code\")}')
                    break
                else:
                    print(f'  [{t}] {json.dumps(parsed, ensure_ascii=False)[:200]}')
        except asyncio.TimeoutError:
            print('  (timeout, no more events)')
    print('done')
asyncio.run(main())
" 2>&1

echo ""
echo "=== 5. 清理 ==="
echo "容器仍在运行，可手动进入研究："
echo "  docker exec -it video-research bash"
echo "  docker stop video-research && docker rm video-research"
