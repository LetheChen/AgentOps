// P0.18 agentops-worker bridge
// 把 codex app-server JSON-RPC（stdio）包装成 WebSocket server。
// 协议见 docs/p016/§4.4（容器内 start 消息 → AgentEvent 事件流）。
// 镜像重命名（P0.18）：codex-node → agentops-worker（不再绑定具体 harness）

const { WebSocketServer } = require('ws');
const { spawn } = require('child_process');
const path = require('path');

const PORT = parseInt(process.env.NODE_BRIDGE_PORT || '7891', 10);
const WORKSPACE = process.env.WORKSPACE || '/workspace';
const CODEX_BIN = process.env.CODEX_BIN || 'codex';

function log(...args) {
  process.stderr.write(`[bridge] ${args.join(' ')}\n`);
}

async function main() {
  const wss = new WebSocketServer({ port: PORT, path: '/bridge' });

  wss.on('listening', () => {
    log(`WS server listening on ws://0.0.0.0:${PORT}/bridge`);
  });

  wss.on('connection', (ws) => {
    log('Manager connected');

    let codex = null;
    let buffer = '';
    let idCounter = 0;
    const pending = new Map();  // request_id -> {resolve, reject}

    function send(msg) {
      if (ws.readyState === ws.OPEN) {
        ws.send(JSON.stringify(msg));
      }
    }

    function sendToCodex(obj) {
      if (codex && codex.stdin.writable) {
        codex.stdin.write(JSON.stringify(obj) + '\n');
      }
    }

    ws.on('message', async (raw) => {
      let msg;
      try {
        msg = JSON.parse(raw.toString());
      } catch (e) {
        log('JSON parse error:', e.message);
        return;
      }

      if (msg.type === 'start') {
        // 1. spawn codex app-server
        log(`Starting codex app-server (cwd=${WORKSPACE})`);
        codex = spawn(CODEX_BIN, ['app-server'], {
          cwd: WORKSPACE,
          env: {
            ...process.env,
            OPENAI_API_KEY: msg.openai_api_key || process.env.OPENAI_API_KEY,
            OPENAI_BASE_URL: msg.openai_base_url || process.env.OPENAI_BASE_URL,
          },
          stdio: ['pipe', 'pipe', 'pipe'],
        });

        codex.stdout.on('data', (chunk) => {
          buffer += chunk.toString();
          let nl;
          while ((nl = buffer.indexOf('\n')) >= 0) {
            const line = buffer.slice(0, nl);
            buffer = buffer.slice(nl + 1);
            if (!line.trim()) continue;
            try {
              const parsed = JSON.parse(line);
              // 响应 → 解析给 pending
              if (parsed.id !== undefined && pending.has(parsed.id)) {
                const { resolve } = pending.get(parsed.id);
                pending.delete(parsed.id);
                resolve(parsed);
              } else if (parsed.method === 'codex/event') {
                // AgentEvent 推给 Manager
                send({ type: 'event', event: parsed.params?.msg || parsed.params });
              } else if (parsed.method === 'error') {
                send({ type: 'event', event: { type: 'error', error_message: JSON.stringify(parsed.params) } });
              } else if (parsed.method === 'exit') {
                // app-server 主动退出
                send({ type: 'event', event: { type: 'done' } });
              }
            } catch (e) {
              log('codex parse error:', e.message, line.slice(0, 80));
            }
          }
        });

        codex.stderr.on('data', (chunk) => {
          const text = chunk.toString();
          log('codex stderr:', text.trim());
          send({ type: 'stderr', line: text.trim() });
        });

        codex.on('exit', (code, signal) => {
          log(`codex exited code=${code} signal=${signal}`);
          send({ type: 'event', event: { type: 'done' } });
          send({ type: 'exit', code });
        });

        // 2. 初始化：initialize + thread/start
        async function rpc(method, params) {
          const id = ++idCounter;
          return new Promise((resolve, reject) => {
            pending.set(id, { resolve, reject });
            sendToCodex({ id, method, params });
          });
        }

        try {
          await rpc('initialize', {
            clientInfo: { name: 'agentops-worker', version: '0.1.0' },
            capabilities: { experimentalApi: true },
          });

          if (msg.skill_roots) {
            try {
              await rpc('skills/extraRoots/set', { extraRoots: msg.skill_roots });
              await rpc('skills/list', { cwds: [WORKSPACE], forceReload: true });
            } catch (e) {
              log('skills load warning:', e.message);
            }
          }

          const thread = await rpc('thread/start', {
            model: msg.model || 'MiniMax-M3',
            cwd: WORKSPACE,
            approvalPolicy: 'never',
            sandbox: msg.sandbox || 'danger-full-access',
          });
          const threadId = thread.result?.threadId || thread.result?.thread_id;
          log(`thread started: ${threadId}`);

          // 第一个 turn
          await rpc('turn/start', {
            threadId,
            input: msg.prompt,
            tools: msg.tools || [],
          });

          send({ type: 'event', event: { type: 'ready', thread_id: threadId } });
        } catch (e) {
          log('init failed:', e.message);
          send({ type: 'event', event: { type: 'error', error_message: e.message } });
        }
      } else if (msg.type === 'turn' && codex) {
        // 后续 turn
        try {
          await sendToCodex({
            id: ++idCounter,
            method: 'turn/start',
            params: {
              threadId: msg.thread_id,
              input: msg.prompt,
              tools: msg.tools || [],
            },
          });
        } catch (e) {
          log('turn failed:', e.message);
        }
      } else if (msg.type === 'shutdown') {
        log('Manager requested shutdown');
        if (codex) {
          codex.kill('SIGTERM');
        }
        ws.close();
      }
    });

    ws.on('close', () => {
      log('Manager disconnected');
      if (codex) {
        codex.kill('SIGTERM');
        codex = null;
      }
    });

    ws.on('error', (err) => {
      log('WS error:', err.message);
    });
  });

  wss.on('error', (err) => {
    log('WSS error:', err.message);
    process.exit(1);
  });

  // 优雅退出
  process.on('SIGTERM', () => {
    log('SIGTERM received, closing');
    wss.close(() => process.exit(0));
  });
}

main().catch((e) => {
  log('fatal:', e.message);
  process.exit(1);
});
