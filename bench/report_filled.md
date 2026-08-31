# M0 选型 Benchmark 报告

**测试时间**: 2026-08-25 13:33:26 UTC
**测试者**: codex-m0-bench
**测试用例**: hello-world.yaml (3 节点: fetch -> think -> report)
**候选运行次数**: 3 trials/candidate

## 指标矩阵

| 指标 | A. OpencodeOrchestrator | B. AgentOpsOrchestrator | C. LocalSdkOrchestrator |
|---|---|---|---|
| 启动耗时 (ms) | — | — | 106.1 ms |
| Token 成本 (in+out) | — | — | in=100 / out=57 |
| SDK breaking 频率 (6月内) | 2 | 1 | 0 (自研) |
| 原生事件可观测性 | yes | yes | partial |

## 试阶详情

### OpencodeOrchestrator

- **状态**: service_unavailable (0% completed)
  - service_unavailable in 3/3 trials
  - first error: http://127.0.0.1:4096 not reachable on /health
  - trial 1: status=service_unavailable  startup=—ms  tokens=in:—/out:—  duration=—ms  err: http://127.0.0.1:4096 not reachable on /health
  - trial 2: status=service_unavailable  startup=—ms  tokens=in:—/out:—  duration=—ms  err: http://127.0.0.1:4096 not reachable on /health
  - trial 3: status=service_unavailable  startup=—ms  tokens=in:—/out:—  duration=—ms  err: http://127.0.0.1:4096 not reachable on /health

### AgentOpsOrchestrator

- **状态**: service_unavailable (0% completed)
  - service_unavailable in 3/3 trials
  - first error: http://127.0.0.1:19191 not reachable on /health
  - trial 1: status=service_unavailable  startup=—ms  tokens=in:—/out:—  duration=—ms  err: http://127.0.0.1:19191 not reachable on /health
  - trial 2: status=service_unavailable  startup=—ms  tokens=in:—/out:—  duration=—ms  err: http://127.0.0.1:19191 not reachable on /health
  - trial 3: status=service_unavailable  startup=—ms  tokens=in:—/out:—  duration=—ms  err: http://127.0.0.1:19191 not reachable on /health

### LocalSdkOrchestrator

- **状态**: ok (100% completed)
  - trial 1: status=completed  startup=97.7ms  tokens=in:100/out:57  duration=6.4ms
  - trial 2: status=completed  startup=106.0ms  tokens=in:100/out:57  duration=4.0ms
  - trial 3: status=completed  startup=114.7ms  tokens=in:100/out:57  duration=5.0ms

## 已知问题

- **OpencodeOrchestrator**: http://127.0.0.1:4096 not reachable on /health
- **AgentOpsOrchestrator**: http://127.0.0.1:19191 not reachable on /health

## 决策 (测试者手动填)

**推荐**: {A / B / C}

**理由** (3 条):
1. {理由 1}
2. {理由 2}
3. {理由 3}
