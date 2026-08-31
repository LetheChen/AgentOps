"""task_decompose_decide 工具：多因子分解决策树（确定性）。

设计文档：docs/product-design/DESIGN_task_lifecycle_automation_v1.md §5.4
- 四因子（LLM 评估）：workload / coupling / dependency / risk_isolation
- 决策树 100% 确定性（无 LLM 参与），输出策略名对齐 resolve_decompose_strategy
  （simple→none / medium→single / complex→recursive 三档语义）
- 产出「分解决策书」存 task_reports（为什么拆/不拆，各因子判定结果）
- 核心洞察：拆分收益只有并行提速与失败隔离，成本是上下文割裂；
  上下文传递成本 > 并行收益时，拆分是负优化。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 因子合法值白名单（LLM 不能发明新值）
_WORKLOAD_VALUES = {"simple", "medium", "complex"}
_COUPLING_VALUES = {"high", "low"}
_DEPENDENCY_VALUES = {"serial", "independent"}


def _decide(factors: dict) -> tuple[str, list[str]]:
    """跑决策树，返回 (strategy, 判定轨迹)。

    strategy: none（不拆，单 agent 直接执行）
              single（不拆，单 agent 依次执行，prompt 内附执行顺序计划）
              recursive（拆：父子任务 + DAG）
    """
    steps: list[str] = []

    # F1 工作量因子
    workload = factors["workload"]
    if workload == "simple":
        steps.append(f"F1 工作量=simple（小当量）→ 不拆：单 agent 直接执行")
        return "none", steps
    steps.append(f"F1 工作量={workload}（中大）→ 进入耦合度判定")

    # F2 上下文耦合度因子
    coupling = factors["coupling"]
    if coupling == "high":
        steps.append(f"F2 耦合度=high（同域改动为主）→ 不拆：单 agent 依次执行（拆开只会上下文割裂）")
        return "single", steps
    steps.append(f"F2 耦合度={coupling}（低耦合）→ 进入依赖结构判定")

    # F3 依赖结构因子
    dependency = factors["dependency"]
    if dependency == "serial":
        steps.append(f"F3 依赖结构=serial（串行依赖链）→ 不拆：单 agent 按序执行（拆开只加通信开销）")
        return "single", steps
    steps.append(f"F3 依赖结构={dependency}（天然独立可并行）→ 进入风险隔离判定")

    # F4 风险隔离因子
    if factors["need_risk_isolation"]:
        steps.append("F4 需隔离高风险部分 → 拆：高风险块独立 + 低风险块打包")
    else:
        steps.append("F4 无隔离需求 → 拆：按独立子块并行（并发度受全局上限约束）")
    return "recursive", steps


def _build_decision_book(task: dict, factors: dict, reasons: dict,
                         strategy: str, steps: list[str]) -> str:
    """生成分解决策书 Markdown（可解释、可追溯、用户可否决）。"""
    strategy_desc = {
        "none": "不拆：单 agent 直接执行（任务当量小，拆分开销大于收益）",
        "single": "不拆：单 agent 依次执行（高耦合/串行依赖，prompt 内附执行顺序计划）",
        "recursive": "拆：父子任务 + DAG 编排（低耦合、天然独立，可并行或需风险隔离）",
    }[strategy]
    risk = task.get("risk_level", "medium")
    lines = [
        f"# 分解决策书 · {task.get('identifier') or task['task_id']}",
        "",
        f"- 任务：{task.get('title', '')}",
        f"- 风险级别：{risk}",
        f"- 决策策略：**{strategy}** —— {strategy_desc}",
        "",
        "## 四因子判定轨迹",
    ]
    lines.extend(f"{i+1}. {s}" for i, s in enumerate(steps))
    lines.append("")
    lines.append("## 因子评估依据")
    defaults = {
        "workload": "改动面估算（文件数/模块数/预估 token）",
        "coupling": "改动文件重叠度、模块归属聚类",
        "dependency": "方案文档中的功能依赖描述",
        "risk_isolation": "风险级别 + 失败半径评估",
    }
    for key in ("workload", "coupling", "dependency"):
        val = reasons.get(key) or defaults[key]
        lines.append(f"- **{key}** = `{factors.get(key)}`：{val}")
    iso_val = reasons.get("risk_isolation") or defaults["risk_isolation"]
    lines.append(f"- **need_risk_isolation** = `{factors.get('need_risk_isolation')}`：{iso_val}")
    if strategy == "recursive" and risk == "high":
        lines.append("")
        lines.append("> ⚠ 高风险拆分方案：须用户在评论区确认后方可进入自动调度。")
    lines.append("")
    lines.append("## 执行编排建议")
    if strategy == "none":
        lines.append("任务保持单 agent 直接执行，无需创建子任务，直接进入调度派发。")
    elif strategy == "single":
        lines.append("任务保持单 agent 依次执行：在派发 prompt 中附执行顺序计划"
                     "（按方案文档的模块顺序），无需创建子任务。")
    else:
        lines.append("按独立子块创建子任务（parent 指向本任务），并补全子任务间 blocks 依赖关系；"
                     "并发度受全局调度上限约束。")
    return "\n".join(lines)


async def task_decompose_decide(args: dict) -> dict:
    """多因子分解决策树（确定性判定 + 决策书落库）。

    args:
        task_id (str, required): 任务 ID（应处于 decomposing 阶段）
        workload (str, required): 工作量因子 simple|medium|complex
        coupling (str, required): 上下文耦合度 high|low
        dependency (str, required): 依赖结构 serial|independent
        need_risk_isolation (bool, optional): 是否需风险隔离，默认 false
        reasons (dict, optional): 各因子评估依据 {workload: "...", ...}
    """
    from orchestrator._registry import get_task_orchestrator

    orch = get_task_orchestrator()
    if orch is None:
        return {"content": "决策失败：task_orchestrator 未初始化", "error": "orchestrator_unavailable"}

    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"content": "决策失败：缺少 task_id", "error": "missing_task_id"}

    factors = {
        "workload": (args.get("workload") or "").strip(),
        "coupling": (args.get("coupling") or "").strip(),
        "dependency": (args.get("dependency") or "").strip(),
        "need_risk_isolation": bool(args.get("need_risk_isolation", False)),
    }
    if factors["workload"] not in _WORKLOAD_VALUES:
        return {"content": f"决策失败：workload 必须是 {'|'.join(_WORKLOAD_VALUES)}",
                "error": "invalid_workload"}
    if factors["coupling"] not in _COUPLING_VALUES:
        return {"content": f"决策失败：coupling 必须是 {'|'.join(_COUPLING_VALUES)}",
                "error": "invalid_coupling"}
    if factors["dependency"] not in _DEPENDENCY_VALUES:
        return {"content": f"决策失败：dependency 必须是 {'|'.join(_DEPENDENCY_VALUES)}",
                "error": "invalid_dependency"}

    task = await orch.store.get_task(task_id)
    if not task:
        return {"content": "决策失败：任务不存在", "error": "task_not_found"}

    reasons = args.get("reasons") if isinstance(args.get("reasons"), dict) else {}

    strategy, steps = _decide(factors)
    book = _build_decision_book(task, factors, reasons, strategy, steps)

    # 决策书存 task_reports（决策资产，用户可否决高风险拆分）
    report = await orch.store.submit_report(
        task_id=task_id, agent_id="task_decomposer", content=book)

    logger.info("task_decompose_decide: %s → %s", task_id, strategy)
    return {
        "content": f"分解决策完成：策略={strategy}（决策书已存报告 {report['report_id']}）",
        "strategy": strategy,
        "decision_book": book,
        "report_id": report["report_id"],
        "risk_level": task.get("risk_level", "medium"),
    }
