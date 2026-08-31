# P0.16 node 容器化执行 + 实时事件回传方案

> 基准测试日期：2026-08-10
> 测试场景：multi-actor-live-report（3 并行 actor，codex harness + MiniMax-M3）
> 对比项目：外部参考方案（同 codex app-server + MiniMax-M3，但 node 跑在 Docker 中）
> 核心目标：**node 在 Docker 容器中执行** + **容器内 subagent 事件实时回传 Manager/前端**

## 一、动机与目标

### 1.1 现状痛点

1. **Windows 本地执行工具不稳定**：当前 node 内的 codex app-server 子进程直接跑在 Windows 宿主机上
   - `find` / `grep` / `read_file` / `edit` 等工具调用受 NTFS 权限/路径空格（`Program Files`）/长路径影响，偶尔需要多次重试
   - 之前测试中为了解决 `Codex binary not found` 临时打了 shim 到 `/tmp/codex-shim/codex.cmd`（无空格路径）才跑通
2. **跨平台一致性差**：开发在 macOS / Windows / WSL 切换时，codex 子进程的行为差异显著
3. **多 actor 并发仍有 429 风险**：外部参考方案 容器化方案天然让 worker 串行，但 AgentOps 当前没有节流

### 1.2 目标

| 目标 | 验收标准 |
|------|---------|
| **G1：node 容器化执行** | 每个 actor / subagent 对应一个 docker 容器（`runtime_placement=docker_container`） |
| **G2：事件实时回传** | 容器内 codex 产生的 AgentEvent（TEXT/TOOL_USE/USAGE/ERROR）≤200ms 内推到 Manager → 前端 SSE → activity feed |
| **G3：subagent 会话可见性不退化** | `subagents` 表依然实时写入；前端依然能看到 1 manager + 3 subagent 共 4 条 session |
| **G4：可降级** | 若 Docker 不可用，自动 fallback 到 in_process（保持现状） |
| **G5：与现有 P0.16 节流/重试兼容** | 容器内的 codex 仍然受 `_api_semaphores` 限流，仍然走 429 退避重试 |

### 1.3 前置工程 gap 清单（Copilot 评审发现）

实施容器化之前必须先补齐以下 4 项缺失，否则 G3 可见性、G1 容器调度、G4 fallback 都会失败：

| Gap | 现状（Copilot 已确认） | 影响 | 修正归属 |
|-----|-------------------------|------|----------|
| **subagent 生命周期没接入 engine** | `engine.py:1389` 注释 `# 后续 v4 引入 SubagentStore.provision_subagent 后填入`；`provision_subagent` / `record_provisioned_worker` 仅 store.py 暴露，**engine 未调用** | G3 不成立：容器跑完 subagents 表仍是空的 | **P0-1** |
| **`runtime_execution` 字段不存在** | `schema.py` 的 `WorkflowNode` 类定义（[schema.py:155](workflow/schema.py#L155)）无此字段；`loader.py` 也不识别 | engine 不知道节点该选哪个 adapter | **P0-0** |
| **缺 docker Python 依赖** | `pyproject.toml` 不含 `docker` 包；无 `import docker` 痕迹 | 写不了 `harness/container.py` | **P0-2** |
| **Windows bind mount 性能 trade-off** | NTFS → WSL2 VM 的 bind mount IO 延迟 2-5ms × 调用次数；用 volume 则 host 看不到文件，破坏 `_harvest_file_outputs` | workspace 收割链路 vs IO 性能的取舍 | **§4.7 待用户决策** |

> **修正后优先级**：P0-0 schema → P0-1 subagent lifecycle → P0-2 docker 依赖 → P0-3 ContainerNodeAdapter → P0-4 bridge 端口协议。

## 二、架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│  AgentOps Manager (host, FastAPI)                                   │
│                                                                     │
│  DagEngine._execute_node()                                          │
│       │                                                             │
│       │ 解析 node.runtime_execution == "docker"                     │
│       ▼                                                             │
│  ContainerNodeAdapter  ──── docker run ────►  ┌──────────────────┐  │
│  (harness/container.py)                       │ codex-node       │  │
│       │                                       │ container        │  │
│       │  bind mount:                           │                  │  │
│       │    host: workspace/<wf>/<run>/        │  ┌────────────┐  │  │
│       │    ->  /workspace (in container)      │  │codex app-  │  │  │
│       │                                       │  │server      │  │  │
│       │  env:                                 │  └─────┬──────┘  │  │
│       │    OPENAI_API_KEY                     │        │ JSON-RPC│  │
│       │    OPENAI_BASE_URL                    │  ┌─────▼──────┐  │  │
│       │    AGENTOPS_RUN_ID                    │  │node-bridge │  │  │
│       │    AGENTOPS_NODE_ID                   │  │(Node WS    │  │  │
│       │    AGENTOPS_EVENT_SINK_URL            │  │ server)    │  │  │
│       │                                       │  └─────┬──────┘  │  │
│       │                                       │        │ WS      │  │
│       │                                       └────────┼─────────┘  │
│       │                                                │            │
│       │  ◄──────── AgentEvent stream (TEXT/...) ───────┘            │
│       │                                                             │
│       ▼                                                             │
│  self.event_sink(ev)  →  SSE  →  前端 DagViewer                      │
└─────────────────────────────────────────────────────────────────────┘
```

**关键点**：
- 容器内的 codex app-server **不直接**和 Manager 通信
- 中间层是 **node-bridge**（Node.js WS server，跟随 codex 启动；容器内监听固定端口 7891，host 端口由 Docker 动态分配）
- Manager 通过 `container.ports` API 拿到 host 端口后连 WS，收到 AgentEvent 转发到 `event_sink`，下游链路（SSE / audit / 前端）和现状完全一致

## 三、运行时数据通路（与现有表/事件兼容）

### 3.1 subagents 表：写 `runtime_placement=docker_container`

> **⚠️ 前置依赖 P0-1**：当前 `DagEngine._execute_node` **没有**调用 `provision_subagent` / `terminate_subagent`（[engine.py:1389](workflow/engine.py#L1389) 注释：`# 后续 v4 引入 SubagentStore.provision_subagent 后填入`）。本节描述的目标行为要求先把 engine 接入 subagent 生命周期，**in_process 路径同样要补**，否则 G3 退化。

复用现有 schema（[audit/store.py:141-175](audit/store.py#L141-L175)），无需迁移：

```sql
INSERT INTO subagents (
    subagent_id, actor_id, run_id, node_id, lease_generation,
    harness_type, harness_instance_id,
    status, runtime_placement,
    container_id, process_id, thread_id,
    workspace_ref, ...
) VALUES (
    ?, ?, ?, ?, 1,
    'codex', ?,                        -- harness_type
    'provisioning', ?,                 -- runtime_placement: in_process | docker_container
    NULL, NULL, NULL,                  -- container_id 稍后回填（仅 docker_container）
    '/workspace/<run_id>/<node_id>',
    ...
);
```

`container_id` 在 `docker run` 成功后回填（拿到 container ID 后做一次 UPDATE），保证前端能根据 `container_id` 查到。

**engine 接入点（核心改动 P0-1）**：

```python
# workflow/engine.py  _execute_node 入口
async def _execute_node(self, node, nstate, global_inputs):
    # 1. 决定 runtime_placement
    runtime = node.runtime_execution or "in_process"

    # 2. P0-1: 调 provision_subagent 写表（in_process / docker_container 都走这条）
    subagent_id = f"{self.run_state.run_id}.{node.id}.{uuid4().hex[:6]}"
    await self.event_store.provision_subagent(
        subagent_id=subagent_id,
        actor_id=node.id,
        run_id=self.run_state.run_id,
        node_id=node.id,
        harness_type="codex",
        runtime_placement=runtime,
        workspace_ref=ctx.workspace,
    )
    nstate.subagent_id = subagent_id

    try:
        # 3. 选 adapter 并跑
        harness = self._create_harness(node, runtime)
        async for event in harness.run(prompt, tools, ctx):
            ...  # 现有逻辑不变
    finally:
        # 4. P0-1: 调 terminate_subagent 更新 status
        await self.event_store.terminate_subagent(
            subagent_id=subagent_id,
            status="completed" if success else "failed",
            container_id=getattr(nstate, "container_id", None),
        )
```

### 3.2 事件流：bridge_run_events 适配

**现状路径**（harness in_process）：
```
harness.run() async iter  →  AgentEvent
                          →  self.event_sink(DagEvent)
                          →  bridge_run_events(run_id, dag_event)
                          →  EventBus.publish → SSE → 前端
```

**容器化路径**（本次新增）：
```
container.codex stdout JSON-RPC
  → 容器内 node-bridge WS server
  → Manager: ContainerNodeAdapter.run() async iter AgentEvent
  → 完全复用 self.event_sink → bridge_run_events → SSE → 前端
```

**关键不变性**：`engine._execute_node` 中的 `async for event in harness.run()` 接口不变。
- `in_process` 时 `harness` 是 `CodexAppServerClient`（直接 spawn 子进程）
- `docker_container` 时 `harness` 是 `ContainerNodeAdapter`（通过 WS 接收事件）

`_execute_node` 内的所有事件处理（TEXT / USAGE / TOOL_USE / ERROR）**不需要改**。

### 3.3 visibility 保留说明

| 维度 | in_process | docker_container |
|------|------------|------------------|
| sessions 表 | +1 | +1（同一 run_id） |
| subagents 表 | +1, runtime_placement='in_process' | +1, runtime_placement='docker_container', container_id 填入 |
| 前端 activity feed | ✅ | ✅（通过 event_sink 同一通路） |
| 同 run 多 subagent 计数 | ✅ | ✅ |
| 子 session 文件（provider 配置）| host CODEX_HOME | 容器内 CODEX_HOME（镜像预置 / bind mount 配置目录） |

## 四、实施步骤

### 4.1 (P0) 构建 `codex-node` 镜像

**新文件**：`docker/codex-node/Dockerfile`

```dockerfile
FROM node:22-slim

WORKDIR /opt/codex-node
# Aliyun mirror
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g' \
             /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      bash ca-certificates curl git openssh-client python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# codex CLI（与 host 一致：@openai/codex@0.144.5）
COPY package.json package-lock.json* ./
RUN npm ci
# 验证：codex --version 必须能跑
RUN codex --version

# node-bridge：把 codex JSON-RPC 流包成 WS server，供 Manager 连
COPY bridge.js /opt/codex-node/bridge.js
COPY entrypoint.sh /opt/codex-node/entrypoint.sh
RUN chmod +x /opt/codex-node/entrypoint.sh

ENV NODE_BRIDGE_PORT=7891       # 容器内 WS 固定监听端口；host 端口由 Docker -p 动态分配
ENV WORKSPACE=/workspace

ENTRYPOINT ["/opt/codex-node/entrypoint.sh"]
```

**entrypoint.sh 职责**：
1. 启动 node-bridge：`node bridge.js --port=${NODE_BRIDGE_PORT}`（固定 7891）
2. 等待 Manager 连入（带 30s 超时）
3. 启动 `codex app-server`，把 stdio 转给 node-bridge
4. 收到 WS 端的 `start` 消息后才把 stdin pipe 给 codex（cold start 友好）

**package.json**（最小依赖）：
```json
{
  "dependencies": {
    "@openai/codex": "0.144.5",
    "ws": "^8.18.0"
  }
}
```

**预构建 + 缓存**：CI / 本地一次性 `docker build -t codex-node:latest`，运行时直接用本地镜像（避免每次拉镜像拖慢冷启动）。

### 4.2 (P0) ContainerNodeAdapter（harness 适配器）

**新文件**：`harness/container.py`

**位置原则**：
- harness/ 里只放 ABC 实现，不知道 DAG 拓扑（符合 [python-backend.md](../.claude/rules/python-backend.md) anti-pattern 规则）
- `ContainerNodeAdapter` 实现 `AgentClient` ABC → 注册到 `HarnessRegistry`
- Manager 通过 `runtime_execution="docker"` 选择这个 adapter，**不是**硬编码

**核心 API**：

```python
class ContainerNodeAdapter(AgentClient):
    """通过 Docker 容器执行 codex node，事件经 WS 回传。"""

    def __init__(
        self,
        image: str = "codex-node:latest",
        docker_host: str | None = None,        # None → 用默认 docker.sock
        workspace_root: str | None = None,    # host 侧 workspace 根
        runtime_placement: str = "docker_container",
    ):
        self.image = image
        self.docker = docker.DockerClient(base_url=docker_host)
        self.workspace_root = workspace_root or str(Path.cwd())
        self.runtime_placement = runtime_placement

    async def run(
        self, prompt: str, tools: list[dict], ctx: AgentRunContext,
    ) -> AsyncIterator[AgentEvent]:
        """spawn 容器 -> 连 WS -> 透传 AgentEvent."""
        container_id, host_port = await self._spawn_container(ctx)
        try:
            async with self._connect_bridge(container_id, host_port) as bridge:
                # 1. 发 start 消息，把 prompt/tools 发给容器内 codex
                await bridge.send_start(prompt=prompt, tools=tools, ctx=ctx)
                # 2. async iter AgentEvent
                async for event in bridge.events():
                    yield event
        finally:
            await self._cleanup_container(container_id)

    async def _spawn_container(self, ctx: AgentRunContext) -> tuple[str, int]:
        """docker run -d + bind mount workspace + 注入 env + 暴露 WS 端口。
        
        返回 (container_id, host_bridge_port)：
        - container_id: docker container ID，用于后续 docker rm / exec
        - host_bridge_port: host 侧 127.0.0.1 上 WS server 的实际端口
        """
        host_ws = Path(self.workspace_root) / ctx.workspace.lstrip("/")
        host_ws.mkdir(parents=True, exist_ok=True)

        env = self._build_container_env(ctx)
        mounts = [
            docker.types.Mount(
                source=str(host_ws), target="/workspace",
                type="bind",
            ),
        ]

        # 可选：bind mount 只读 codex 配置（避免容器内生成 .codex 脏数据）
        # mounts.append(Mount(source="/etc/agentops/codex", target="/root/.codex",
        #                     type="bind", read_only=True))

        container = self.docker.containers.run(
            self.image,
            command=["/opt/codex-node/entrypoint.sh"],
            environment=env,
            mounts=mounts,
            ports={
                "7891/tcp": None,    # host 端口由 Docker 自动分配（=0 的语义）
            },
            detach=True,
            remove=False,
            network_mode="bridge",
            labels={
                "agentops.run_id": ctx.session_id.split(".", 1)[0],
                "agentops.node_id": ctx.session_id.rsplit(".", 1)[-1],
            },
        )

        # 关键修正（Copilot 评审指出的 port=0 bug）：
        # container.ports 在 container.run 后立即读可能为空（Docker daemon 还在分配），
        # 需要 reload 后轮询直到端口就绪（≤5s）
        host_bridge_port = await self._await_bridge_port(container, timeout=5.0)

        # 回写 container_id 到 subagents 表（P0-1 lifecycle）
        await self._update_subagent_container_id(ctx.session_id, container.id)
        return container.id, host_bridge_port

    async def _await_bridge_port(
        self, container, timeout: float = 5.0,
    ) -> int:
        """轮询直到 Docker daemon 把 host 端口分配好。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            container.reload()
            bindings = container.ports.get("7891/tcp") or []
            if bindings and bindings[0].get("HostPort"):
                return int(bindings[0]["HostPort"])
            await asyncio.sleep(0.1)
        # 超时——容器没暴露端口，直接 raise 触发 engine 层 fallback
        raise RuntimeError(
            f"Container {container.id[:12]} did not expose 7891/tcp within {timeout}s"
        )

    async def _connect_bridge(self, container_id: str, host_port: int):
        """连 WS server 到 host_port（bridge 协议见 §4.4）。"""
        import websockets
        url = f"ws://127.0.0.1:{host_port}/bridge"
        return await websockets.connect(url, max_size=10 * 1024 * 1024)

    def _build_container_env(self, ctx: AgentRunContext) -> dict[str, str]:
        """注入 API key + 标识信息（不要持久化任何 key 到镜像）。"""
        return {
            "OPENAI_API_KEY": ctx.api_key,
            "OPENAI_BASE_URL": ctx.base_url,
            "AGENTOPS_RUN_ID": ctx.session_id.split(".", 1)[0],
            "AGENTOPS_NODE_ID": ctx.session_id.rsplit(".", 1)[-1],
            "AGENTOPS_BRIDGE_PORT": "7891",   # 容器内固定端口（host 端口由 Docker 分配）
        }
```

**HarnessRegistry 注册**（[harness/registry.py](harness/registry.py)）：
```python
# 默认不启用 docker；通过配置或节点级开关打开
if os.environ.get("AGENTOPS_NODE_RUNTIME") == "docker":
    HarnessRegistry.register("codex", ContainerNodeAdapter)
else:
    HarnessRegistry.register("codex", CodexAppServerClient)
```

### 4.3 (P0) 节点级开关：runtime_execution 字段

**新文件**：[workflow/yaml_schema.py](workflow/yaml_schema.py)（如已有则修改）

```python
@dataclass
class WorkflowNode:
    # ... 现有字段
    runtime_execution: str = "in_process"   # in_process | docker_container
```

**workflow yaml 示例**：
```yaml
- id: live_report_actor
  type: agent
  agent: live_report_actor
  runtime_execution: docker_container     # ← 新增
  config:
    timeout_seconds: 600
```

**engine 端**：`create_harness_for_node` 根据 `node.runtime_execution` 选择 adapter：

```python
def create_harness_for_node(self, node: WorkflowNode) -> AgentClient:
    if node.runtime_execution == "docker_container" and self._docker_available():
        return ContainerNodeAdapter(image="codex-node:latest")
    return CodexAppServerClient(...)   # 现状
```

### 4.4 (P0) 容器事件回传：bridge 协议

**node-bridge 协议**（容器内 → Manager）：

```jsonc
// Manager → Container: start 一个 turn
{
  "type": "start",
  "prompt": "...",
  "tools": [...],
  "model": "MiniMax-M3",
  "thread_options": {"persist_session": false}
}

// Container → Manager: AgentEvent 流
{"type": "event", "event": {"type": "text", "text": "..."}}
{"type": "event", "event": {"type": "tool_use", "tool_name": "bash", "args": {...}}}
{"type": "event", "event": {"type": "usage", "input_tokens": 100, "output_tokens": 50}}
{"type": "event", "event": {"type": "error", "error_message": "..."}}
{"type": "event", "event": {"type": "done"}}

// Container → Manager: stderr 透传 + 退出通知
{"type": "stderr", "line": "..."}        // codex stderr 透传
{"type": "exit", "code": 0}
```

> **修正（Copilot 评审）**：原方案中 `{"type": "ready", "port": 45678}` 已删除。
> 原因：bridge 监听端口在容器内固定为 7891；host 端口由 Docker 在 `docker run -p 7891/tcp:None` 时动态分配，
> Manager 通过 `container.ports['7891/tcp'][0]['HostPort']` 获取（见 §4.2 `_await_bridge_port`）。
> 容器内无需再回传端口号——它一直就是 7891。

**延迟要求**：从容器内 codex stdout 写出 → Manager 收到 AgentEvent，p99 ≤200ms（同机 docker 网络 < 5ms；WS framing ≈ 1ms；JSON 序列化 < 5ms）。

### 4.5 (P1) 容器生命周期与 subagent 状态联动

| subagents.status | 容器状态 | 说明 |
|-------------------|----------|------|
| `provisioning` | created, not started | docker run 成功，未启动 codex |
| `running` | running, bridge connected | 已连 WS，等待/产出 AgentEvent |
| `awaiting_handoff` | running, bridge idle | 当前 turn 完成，等下一个 prompt |
| `completed` | exited 0 | 正常退出 |
| `failed` | exited non-zero | 错误退出，container 未被 remove |
| `cancelled` | killed by Manager | user cancel / 超时 kill |
| `cleanup_failed` | exited but docker rm 失败 | 手动清理告警 |

**强制清理**：每次 run 结束 / cancel / 异常时，扫一遍 `status NOT IN ('completed','failed')` 且 `runtime_placement='docker_container'` 的 subagent，强制 `docker rm -f`。

### 4.6 (P1) 容器内 workspace 收割（output_files）

`_execute_node` 末尾的 `_harvest_file_outputs` **不变**（**前提：采用 §4.7 决策 A bind mount**）：
- bind mount 是双向的（host:workspace ↔ container:/workspace）
- 容器内 agent 通过 `Write` 工具写文件到 `/workspace/xxx`
- host 直接读 `host_ws/xxx` 即可

完全保留现有 output_files 收割链路。

### 4.7 (待决策) workspace 挂载策略：bind mount vs volume

> **Copilot 评审指出**：NTFS → WSL2 VM（Docker Desktop Windows）的 bind mount IO 延迟 2-5ms × 调用次数。
> 3 actor 各做几十次工具调用，累积可能增加 100-500ms 开销，且有 Windows 路径权限/编码风险。
> 但用 docker volume 替代 bind mount 会让 host 看不到容器内写入的文件，**破坏 `_harvest_file_outputs`**。

#### 决策点：三种方案对比

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| **A. bind mount（默认）** | host ↔ container 双向可见，harvest 链路 0 改动 | Windows 下 IO 慢、路径权限问题 | 开发态、本地测试 |
| **B. named volume + 反向同步** | IO 快 | 需要写一个 `sync-sidecar` 把 volume → host 同步；事件延迟增加 | 生产 Linux 环境 |
| **C. 仅 bind mount output 目录** | output_files 走 bind mount，其他目录走 volume | 需要 `output_files` 显式声明路径，灵活性下降 | output 已知且少的 workflow |

**默认推荐**：开发环境采用 **方案 A**；生产化前需评估 **方案 B** 或 **方案 C**。

**待用户决策**：实施 P0 时按哪种方案落地？

## 五、与现有 P0.16 优化的关系

P0.16 原本的 2.1-2.4（API 信号量 / 429 重试 / 同 provider 重试 / 可配置 limit）**仍然全部保留**，作为**容器化方案之下的内层优化**：

| 优化层级 | 范围 | 文件 |
|----------|------|------|
| **L1：容器隔离**（本次新增 P0） | 进程级隔离 + 跨平台一致 | harness/container.py |
| **L2：API 信号量**（原 2.1） | provider 级并行限流 | workflow/engine.py |
| **L3：429 退避重试**（原 2.2） | harness turn 级重试 | harness/codex_appserver.py（容器内同样适用，因为 codex 跑在容器里，但 manager 容器内代码相同）|
| **L4：同 provider 重试**（原 2.3） | engine 节点级重试 | workflow/engine.py |
| **L5：可配置 limit**（原 2.4） | provider 配置 | config/models.yaml |

**L3 重要细节**：429 重试是 codex **app-server 内部**行为。容器化后 codex 跑在容器内，重试逻辑不变；Manager 看到的只是正常流 / 最终 ERROR。Manager 不需要感知 429 重试过程，但**仍可在 L4（engine 层）做同 provider 重试**——如果容器内 codex 4 次重试都失败后 Manager 收到 ERROR event，则 engine 层可以重启容器重试整个 node。

## 六、实施优先级

> **执行顺序很重要**：P0-0 → P0-1 → P0-2 → P0-3 → P0-4，每步完成后单独跑回归再继续。

| 优先级 | 改动 | 文件 | 工作量 | 风险 | 前置 |
|--------|------|------|--------|------|------|
| **P0-0** | 4.3 节点级开关 `runtime_execution` | workflow/schema.py + workflow/loader.py | 0.5d | 低 | — |
| **P0-1** | engine 接入 subagent 生命周期（in_process 也要做） | workflow/engine.py（_execute_node 入口/出口调 provision_subagent / terminate_subagent）| 0.5d | 低 | P0-0 |
| **P0-2** | 加 docker Python 依赖 | pyproject.toml | 0.1d | 低 | — |
| **P0-3** | 4.1 构建 `codex-node` 镜像（含固定端口 7891）| docker/codex-node/*（新目录）| 0.5d | 低 | P0-2 |
| **P0-3** | 4.2 ContainerNodeAdapter（含端口轮询）| harness/container.py（新文件）| 1d | 中 - 需 docker 联调 | P0-3 镜像 |
| **P0-4** | 4.4 bridge 协议 + node-bridge 实现 | docker/codex-node/bridge.js + harness/container.py | 1d | 中 - 需 WS 联调 | P0-3 |
| **P1** | 4.5 容器生命周期状态联动 | audit/store.py + harness/container.py | 0.5d | 低 | P0-4 |
| **P1** | §五 保留 2.1-2.4 并发优化（信号量 / 429 / 同 provider / 可配置）| workflow/engine.py + codex_appserver.py | 1d | 低 | P0-1 |
| **P2** | 4.7 mount 策略决策落地（默认 bind mount）| harness/container.py + 可能 sync-sidecar | 0.5d | 中 | P0-4 |
| **P2** | 镜像预热池（避免冷启动 3-5s）| docker warm pool | 1d | 低 | P0-3 |

**总工作量**：约 5-6 天（不含测试）；核心 P0-0..P0-4 约 4 天可跑通 happy path。

**P0 验收门槛**：每个 P0 步骤完成后必须跑：
- `python cli.py validate workflows/multi-actor-live-report.yaml`
- `python -m pytest tests/test_engine.py tests/test_audit.py -v`
- 再开始下一步

## 七、测试验证

### 7.1 单元测试

```python
# tests/test_container_node_adapter.py

async def test_container_node_adapter_runs_codex():
    """验证容器内 codex 跑通一个 turn 并把 TEXT 事件回传。"""
    adapter = ContainerNodeAdapter(image="codex-node:latest")
    events = []
    async for ev in adapter.run(
        prompt="echo hi",
        tools=[],
        ctx=AgentRunContext(
            system_prompt="", model="MiniMax-M3",
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
            workspace="/tmp/test_ws",
            session_id="run-1.test_node",
        ),
    ):
        events.append(ev)
    assert any(e.type == AgentEventType.TEXT for e in events)
    assert any(e.type == AgentEventType.DONE for e in events)

async def test_container_node_adapter_writes_container_id_to_subagents():
    """验证 docker run 成功后 subagents 表的 container_id 被回填。"""
    ...
```

### 7.2 端到端验证

```bash
# 1. 跑 multi-actor-live-report，节点全部设为 docker
curl -X POST http://127.0.0.1:8000/api/agent/run \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id":"multi-actor-live-report",
    "run_mode":"templated",
    "inputs":{"topic":"Docker isolation","depth":"medium"}
  }'

# 2. 预期：3 actor 全部在容器中跑（docker ps 看得到 3 个 codex-node）
# 3. 预期：subagents 表有 3 条 runtime_placement='docker_container' 记录
# 4. 预期：前端 SSE / activity feed 实时看到 3 个 subagent 的 TEXT 事件
# 5. 预期：总耗时 2-5 min（与 外部参考方案 接近，比 in_process 慢 10-20%）
```

### 7.3 回归对照

| 场景 | in_process（当前） | docker_container（目标） |
|------|---------------------|---------------------------|
| Windows 工具调用稳定性 | ❌ 偶发失败需重试 | ✅ 容器内 Linux 一致 |
| 3 actor 并发 429 | ❌ 2/3 失败（基线） | ✅ L1+L2+L3 三层防护 |
| 前端可见 subagent 数 | 4（1 manager + 3） | 4（同一链路） |
| 冷启动延迟 | ~1s | ~3-5s（docker run + image pull 首次） |
| 总耗时 baseline | 31s 失败 | 目标 2-5 min 完成 |

## 八、风险与缓解

| 风险 | 缓解 |
|------|------|
| Docker Desktop 在 Windows 上默认 bind mount 慢 | 用 `:cached` 模式 / 显式 volume 替代路径 bind |
| 容器冷启动 3-5s 拖慢 latency | 加镜像预热池（P2），keep 2-3 个 idle container |
| 容器内 codex 写入 workspace 但 host 没看到 | bind mount 双向 + 测试用 `os.path.getmtime` 验证 |
| API key 出现在容器 env 中 | 容器 short-lived（单 node 生命周期）+ exit 后自动销毁 |
| docker.sock 权限问题 | 用 `DOCKER_HOST=tcp://...` 走 daemon socket，权限更可控 |
| node-bridge 启动失败导致容器空转 | entrypoint.sh 30s 内 Manager 没连入就 `exit 1` 触发子进程清理 |

## 九、与 外部参考方案 对比的最终结论

| 维度 | 外部参考方案 | AgentOps（新方案） |
|------|----------|---------------------|
| node 执行环境 | ✅ 容器化（必选） | ✅ 容器化（可配置） |
| 跨平台稳定性 | ✅ Linux 容器 | ✅ Linux 容器 |
| 并发节流 | ✅ max_parallelism 串行 Worker | ✅ _api_semaphores + 容器调度双层 |
| subagent 实时可见性 | ❌ Worker 文件系统，难聚合 | ✅ subagents 表 + event_sink 实时 |
| 多 actor 会话计数 | ⚠️ 1 manager + N worker 不直观 | ✅ sessions 表精确计数 |
| 可降级 | ❌ 必须容器 | ✅ docker 不可用 → in_process 自动 fallback |

**结论**：外部参考方案 容器化带来稳定性但牺牲可见性。AgentOps 新方案**两者兼得**——用容器换稳定性，用 subagents 表 + event_sink 实时回传换可见性。

## 十、附录

### A. 容器事件回传延迟基准（预期）

- 同机 docker bridge 网络：1-5ms
- WS framing（一次 JSON event ~ 1KB）：< 1ms
- node-bridge JSON 序列化：< 5ms
- Manager 端 async iter 调度：< 10ms
- **端到端 p99**：< 200ms ✅

### B. 镜像构建 / 预热命令

```bash
# 构建
docker build -t codex-node:latest -f docker/codex-node/Dockerfile .

# 预热池（可选，P2）
docker run -d --rm codex-node:latest /bin/sleep infinity  # 启动 idle container
docker exec <id> /opt/codex-node/entrypoint.sh             # 第一次触发才初始化
```

### C. 相关文件清单

| 文件 | 状态 | 说明 | 对应 P0 |
|------|------|------|---------|
| `docs/p016-codex-concurrency-optimization.md` | 本文档 | 新方案 | — |
| `pyproject.toml` | 改 | 加 `docker>=7.0.0` 依赖 | P0-2 |
| `workflow/schema.py` | 改 | `WorkflowNode` 加 `runtime_execution: str = "in_process"` | P0-0 |
| `workflow/loader.py` | 改 | 解析 yaml `runtime_execution` 字段 | P0-0 |
| `workflow/engine.py` | 改 | `_execute_node` 入口/出口调 subagent 生命周期；`_create_harness` 按 runtime 选 adapter | P0-1 + P0-3 |
| `audit/store.py` | 改 | 新增 `terminate_subagent()` API（如缺）；`provision_subagent` 已存在 | P0-1 |
| `harness/container.py` | 新增 | ContainerNodeAdapter（含 `_await_bridge_port` 端口轮询）| P0-3 |
| `harness/registry.py` | 改 | 注册 ContainerNodeAdapter（按 env / 配置开关）| P0-3 |
| `docker/codex-node/Dockerfile` | 新增 | 镜像构建（容器内固定 7891）| P0-3 |
| `docker/codex-node/bridge.js` | 新增 | 容器内 WS 桥 + codex stdio 透传 | P0-4 |
| `docker/codex-node/entrypoint.sh` | 新增 | 容器启动入口 | P0-3 |
| `docker/codex-node/package.json` | 新增 | 镜像内 npm 依赖（@openai/codex 0.144.5 + ws）| P0-3 |
| `config/agents/manager.yaml` | 改（可选） | 全局默认 `runtime_execution` | P0-3 |
| `tests/test_container_node_adapter.py` | 新增 | 容器跑通 + 端口轮询 + subagent 表写入 | 验证 |
| `tests/test_engine_subagent_lifecycle.py` | 新增 | engine 接入 provision/terminate 后 subagents 行数 = 节点数 | P0-1 验证 |
