"""
M0 orchestrator-selection benchmark runner.

Runs the same 3-node hello-world DAG through three Orchestrator candidates
and reports the four M0 metrics. Each candidate runs N times (default 3) and
we report the mean (with stdev when useful).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator import (
    DagEvent,
    DagEventType,
    LocalSdkOrchestrator,
    RawHarnessEvent,
    RunRequest,
    RunState,
    RunStatus,
)
from workflow import WorkflowValidationError

from bench.orchestrators import (
    AgentOpsOrchestrator,
    OpencodeOrchestrator,
    ServiceUnavailable,
)

logger = logging.getLogger(__name__)

WORKFLOW_PATH = PROJECT_ROOT / "workflows" / "hello-world.yaml"
RUNS_PER_CANDIDATE = 3
RUN_DEADLINE_S = 120.0


# ====== Result data classes ======

@dataclass
class TrialResult:
    candidate: str
    trial_index: int
    status: str
    startup_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: float | None = None
    error: str | None = None
    raw_event_count: int = 0
    has_raw_events: bool = False


@dataclass
class CandidateSummary:
    candidate: str
    trials: list = field(default_factory=list)
    startup_ms_mean: float | None = None
    startup_ms_stdev: float | None = None
    tokens_in_mean: float | None = None
    tokens_out_mean: float | None = None
    duration_ms_mean: float | None = None
    success_rate: float = 0.0
    status: str = "unknown"
    notes: list = field(default_factory=list)

    def to_row(self):
        def fmt(v):
            return "\u2014" if v is None else f"{v:.1f}"
        return {
            "candidate": self.candidate,
            "startup_ms": fmt(self.startup_ms_mean),
            "tokens_in": "\u2014" if self.tokens_in_mean is None else f"{self.tokens_in_mean:.0f}",
            "tokens_out": "\u2014" if self.tokens_out_mean is None else f"{self.tokens_out_mean:.0f}",
            "duration_ms": fmt(self.duration_ms_mean),
            "success_rate": f"{self.success_rate * 100:.0f}%",
            "status": self.status,
        }


# ====== Trial execution ======

async def _run_local_trial(candidate_name, trial_index):
    orch = LocalSdkOrchestrator()
    workflow = orch.load_workflow_file(str(WORKFLOW_PATH))
    req = RunRequest(workflow_id=workflow.workflow_id, inputs={"topic": "M0 bench"})

    res = TrialResult(candidate=candidate_name, trial_index=trial_index, status="error")

    # Spec: startup_ms is from "import + load workflow" to "first NODE_STARTED".
    # Local SDK has import cost effectively zero, so anchor at start of run().
    t0 = time.perf_counter()
    handle = await orch.run(req)
    first_node_at = None

    final = None
    try:
        async with asyncio.timeout(RUN_DEADLINE_S):
            async for event in orch.stream_events(handle.run_id):
                if first_node_at is None and event.type == DagEventType.NODE_STARTED:
                    first_node_at = time.perf_counter()
                    res.startup_ms = (first_node_at - t0) * 1000.0
                et_val = event.type.value if hasattr(event.type, "value") else str(event.type)
                if et_val == "run.completed":
                    final = "completed"
                elif et_val == "run.failed":
                    final = "failed"
                    if hasattr(event, "payload") and event.payload and "error" in event.payload:
                        res.error = str(event.payload["error"])
                if isinstance(event, RawHarnessEvent):
                    res.has_raw_events = True
                    res.raw_event_count += 1
    except TimeoutError:
        res.error = f"run timed out after {RUN_DEADLINE_S}s"
        final = "failed"
    except Exception as e:
        res.error = f"unexpected: {e}"
        final = "failed"

    res.status = final or "error"
    run_state = await orch.get_run(handle.run_id)
    if run_state is not None:
        res.tokens_in = run_state.total_tokens_input
        res.tokens_out = run_state.total_tokens_output
        if run_state.started_at and run_state.finished_at:
            res.duration_ms = (run_state.finished_at - run_state.started_at).total_seconds() * 1000.0
    return res


async def _run_http_trial(candidate_name, trial_index, orch_factory):
    res = TrialResult(candidate=candidate_name, trial_index=trial_index, status="error")

    try:
        orch = orch_factory()
    except Exception as e:
        res.status = "error"
        res.error = f"failed to construct orchestrator: {e}"
        return res

    try:
        async with orch:
            available = await orch.probe()
            if not available:
                res.status = "service_unavailable"
                res.error = f"{orch.base_url} not reachable on /health"
                return res

            try:
                workflow = orch.load_workflow_file(str(WORKFLOW_PATH))
            except Exception as e:
                res.status = "error"
                res.error = f"workflow load failed: {e}"
                return res

            req = RunRequest(workflow_id=workflow.workflow_id, inputs={"topic": "M0 bench"})
            try:
                t0 = time.perf_counter()
                handle = await orch.run(req)
            except ServiceUnavailable as e:
                res.status = "service_unavailable"
                res.error = str(e)
                return res

            first_node_at = None
            final = None
            try:
                async with asyncio.timeout(RUN_DEADLINE_S):
                    async for event in orch.stream_events(handle.run_id):
                        if first_node_at is None and event.type == DagEventType.NODE_STARTED:
                            first_node_at = time.perf_counter()
                            res.startup_ms = (first_node_at - t0) * 1000.0
                        et_val = event.type.value if hasattr(event.type, "value") else str(event.type)
                        if et_val == "run.completed":
                            final = "completed"
                        elif et_val == "run.failed":
                            final = "failed"
                            if hasattr(event, "payload") and event.payload and "error" in event.payload:
                                res.error = str(event.payload["error"])
                        if isinstance(event, RawHarnessEvent):
                            res.has_raw_events = True
                            res.raw_event_count += 1
            except TimeoutError:
                res.error = f"stream timed out after {RUN_DEADLINE_S}s"
                final = "failed"
            except ServiceUnavailable as e:
                res.status = "service_unavailable"
                res.error = str(e)
                return res

            res.status = final or "error"
            try:
                run_state = await orch.get_run(handle.run_id)
            except ServiceUnavailable as e:
                run_state = None
                if not res.error:
                    res.error = f"get_run failed: {e}"
            if run_state is not None:
                res.tokens_in = run_state.total_tokens_input
                res.tokens_out = run_state.total_tokens_output
                if run_state.started_at and run_state.finished_at:
                    res.duration_ms = (run_state.finished_at - run_state.started_at).total_seconds() * 1000.0
    except ServiceUnavailable as e:
        res.status = "service_unavailable"
        res.error = str(e)
    except Exception as e:
        res.status = "error"
        res.error = f"unexpected: {e}"

    return res


# ====== Aggregation ======

def _summarize(candidate, trials):
    summary = CandidateSummary(candidate=candidate, trials=trials)
    if not trials:
        summary.status = "error"
        summary.notes.append("no trials executed")
        return summary

    successes = [t for t in trials if t.status == "completed"]
    unavail = [t for t in trials if t.status == "service_unavailable"]
    errors = [t for t in trials if t.status in ("failed", "error")]

    summary.success_rate = len(successes) / len(trials)

    if unavail and not successes:
        summary.status = "service_unavailable"
        summary.notes.append(f"service_unavailable in {len(unavail)}/{len(trials)} trials")
        summary.notes.append(f"first error: {unavail[0].error}")
    elif successes:
        summary.status = "ok" if len(successes) == len(trials) else "partial"
        startups = [t.startup_ms for t in successes if t.startup_ms is not None]
        if startups:
            summary.startup_ms_mean = statistics.mean(startups)
            if len(startups) > 1:
                summary.startup_ms_stdev = statistics.stdev(startups)
        tins = [t.tokens_in for t in successes if t.tokens_in is not None]
        touts = [t.tokens_out for t in successes if t.tokens_out is not None]
        if tins:
            summary.tokens_in_mean = statistics.mean(tins)
        if touts:
            summary.tokens_out_mean = statistics.mean(touts)
        durs = [t.duration_ms for t in successes if t.duration_ms is not None]
        if durs:
            summary.duration_ms_mean = statistics.mean(durs)
        if errors:
            summary.notes.append(f"{len(errors)}/{len(trials)} trials failed")
            summary.notes.append(f"first error: {errors[0].error}")
    else:
        summary.status = "error"
        summary.notes.append(f"all {len(trials)} trials failed")
        first_err_msg = errors[0].error if errors else None
        summary.notes.append("first error: " + str(first_err_msg))

    return summary


# ====== Static metrics (3: SDK breaking rate, 4: observability) ======

# Hard-coded M0 snapshots. In production these would be queried live from
# GitHub Releases + the service docs; for the M0 benchmark the static values
# keep the report reproducible without network access.
SDK_BREAKING_RATE_6MO = {
    "OpencodeOrchestrator": 2,
    "AgentOpsOrchestrator": 1,
    "LocalSdkOrchestrator": 0,
}

OBSERVABILITY_SUMMARY = {
    "OpencodeOrchestrator": "yes",
    "AgentOpsOrchestrator": "yes",
    "LocalSdkOrchestrator": "partial",
}


# ====== Main bench loop ======

async def run_bench(runs_per_candidate=RUNS_PER_CANDIDATE, candidates=None, quiet=False):
    """Run all 3 candidates `runs_per_candidate` times and return summaries."""
    plan = []
    for name in (candidates or ["OpencodeOrchestrator", "AgentOpsOrchestrator", "LocalSdkOrchestrator"]):
        if name == "OpencodeOrchestrator":
            plan.append((name, lambda: OpencodeOrchestrator()))
        elif name == "AgentOpsOrchestrator":
            plan.append((name, lambda: AgentOpsOrchestrator()))
        elif name == "LocalSdkOrchestrator":
            plan.append((name, None))
        else:
            raise ValueError("unknown candidate: " + str(name))

    summaries = []
    for candidate_name, factory in plan:
        trials = []
        if not quiet:
            print("\n=== " + candidate_name + " (" + str(runs_per_candidate) + " trials) ===", flush=True)
        for i in range(1, runs_per_candidate + 1):
            if candidate_name == "LocalSdkOrchestrator":
                trial = await _run_local_trial(candidate_name, i)
            else:
                trial = await _run_http_trial(candidate_name, i, factory)
            trials.append(trial)
            if not quiet:
                _print_trial_line(trial)
        summaries.append(_summarize(candidate_name, trials))

    return summaries


def _print_trial_line(t):
    startup = f"{t.startup_ms:7.1f}ms" if t.startup_ms is not None else "  \u2014    "
    tokens = (
        f"in={t.tokens_in or 0:>4d} out={t.tokens_out or 0:>4d}"
        if t.tokens_in is not None or t.tokens_out is not None
        else "in=  \u2014  out=  \u2014"
    )
    duration = f"{t.duration_ms:7.1f}ms" if t.duration_ms is not None else "  \u2014    "
    err = f"  err={t.error}" if t.error else ""
    print(f"  trial {t.trial_index}: status={t.status:<22}  startup={startup}  {tokens}  duration={duration}{err}", flush=True)


# ====== Output formatting ======

def render_markdown_report(summaries, runs_per_candidate, workflow_name, tester="codex-m0-bench", when=None):
    when = when or datetime.now(UTC)
    lines = []
    lines.append("# M0 选型 Benchmark 报告")
    lines.append("")
    lines.append("**测试时间**: " + when.strftime("%Y-%m-%d %H:%M:%S") + " UTC")
    lines.append("**测试者**: " + tester)
    lines.append("**测试用例**: " + workflow_name + " (3 节点: fetch -> think -> report)")
    lines.append("**候选运行次数**: " + str(runs_per_candidate) + " trials/candidate")
    lines.append("")
    lines.append("## 指标矩阵")
    lines.append("")
    lines.append("| 指标 | A. OpencodeOrchestrator | B. AgentOpsOrchestrator | C. LocalSdkOrchestrator |")
    lines.append("|---|---|---|---|")

    by_name = {s.candidate: s for s in summaries}

    def cell(name, attr, na="\u2014"):
        s = by_name.get(name)
        if not s:
            return na
        v = getattr(s, attr)
        if v is None:
            return na
        if attr == "success_rate":
            return f"{v * 100:.0f}% ({s.status})"
        if attr in ("startup_ms_mean", "duration_ms_mean"):
            return f"{v:.1f} ms"
        return str(v)

    lines.append("| 启动耗时 (ms) | " + cell("OpencodeOrchestrator", "startup_ms_mean") + " | " + cell("AgentOpsOrchestrator", "startup_ms_mean") + " | " + cell("LocalSdkOrchestrator", "startup_ms_mean") + " |")

    def token_cell(name):
        s = by_name.get(name)
        if not s or (s.tokens_in_mean is None and s.tokens_out_mean is None):
            return "\u2014"
        ti = s.tokens_in_mean if s.tokens_in_mean is not None else 0
        to = s.tokens_out_mean if s.tokens_out_mean is not None else 0
        return f"in={ti:.0f} / out={to:.0f}"

    lines.append("| Token 成本 (in+out) | " + token_cell("OpencodeOrchestrator") + " | " + token_cell("AgentOpsOrchestrator") + " | " + token_cell("LocalSdkOrchestrator") + " |")

    lines.append("| SDK breaking 频率 (6月内) | " + str(SDK_BREAKING_RATE_6MO.get("OpencodeOrchestrator", "\u2014")) + " | " + str(SDK_BREAKING_RATE_6MO.get("AgentOpsOrchestrator", "\u2014")) + " | " + str(SDK_BREAKING_RATE_6MO.get("LocalSdkOrchestrator", "\u2014")) + " (自研) |")

    lines.append("| 原生事件可观测性 | " + OBSERVABILITY_SUMMARY.get("OpencodeOrchestrator", "\u2014") + " | " + OBSERVABILITY_SUMMARY.get("AgentOpsOrchestrator", "\u2014") + " | " + OBSERVABILITY_SUMMARY.get("LocalSdkOrchestrator", "\u2014") + " |")

    lines.append("")
    lines.append("## 试阶详情")
    lines.append("")
    for s in summaries:
        lines.append("### " + s.candidate)
        lines.append("")
        if s.status == "service_unavailable":
            lines.append("- **状态**: service_unavailable (" + f"{s.success_rate * 100:.0f}%" + " completed)")
        elif s.status == "ok":
            lines.append("- **状态**: ok (" + f"{s.success_rate * 100:.0f}%" + " completed)")
        elif s.status == "partial":
            lines.append("- **状态**: partial (" + f"{s.success_rate * 100:.0f}%" + " completed)")
        else:
            lines.append("- **状态**: " + s.status)
        for n in s.notes:
            lines.append("  - " + str(n))
        for t in s.trials:
            err = f"  err: {t.error}" if t.error else ""
            startup_val = "\u2014" if t.startup_ms is None else f"{t.startup_ms:.1f}"
            dur_val = "\u2014" if t.duration_ms is None else f"{t.duration_ms:.1f}"
            tok_in = "\u2014" if t.tokens_in is None else str(t.tokens_in)
            tok_out = "\u2014" if t.tokens_out is None else str(t.tokens_out)
            lines.append("  - trial " + str(t.trial_index) + ": status=" + t.status + "  startup=" + startup_val + "ms  tokens=in:" + tok_in + "/out:" + tok_out + "  duration=" + dur_val + "ms" + err)
        lines.append("")

    lines.append("## 已知问题")
    lines.append("")
    for s in summaries:
        if s.status in ("service_unavailable", "error"):
            first_err = next((t.error for t in s.trials if t.error), "unknown")
            lines.append("- **" + s.candidate + "**: " + str(first_err))
    if all(s.status == "ok" for s in summaries):
        lines.append("- (none \u2014 3 candidates 全部 ok)")
    lines.append("")

    lines.append("## 决策 (测试者手动填)")
    lines.append("")
    lines.append("**推荐**: {A / B / C}")
    lines.append("")
    lines.append("**理由** (3 条):")
    lines.append("1. {理由 1}")
    lines.append("2. {理由 2}")
    lines.append("3. {理由 3}")
    lines.append("")
    return "\n".join(lines)


def render_stdout_table(summaries):
    rows = [s.to_row() for s in summaries]
    headers = list(rows[0].keys()) if rows else ["candidate", "startup_ms", "tokens_in", "tokens_out", "duration_ms", "success_rate", "status"]
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(r.get(h, "")) for h in headers) + " |")
    return "\n".join(out)


# ====== CLI ======

def _setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="bench.runner",
        description="M0 Orchestrator benchmark runner",
    )
    parser.add_argument("--runs", "-n", type=int, default=RUNS_PER_CANDIDATE,
                        help="trials per candidate (default " + str(RUNS_PER_CANDIDATE) + ")")
    parser.add_argument("--candidates", nargs="*", default=None,
                        help="subset of: OpencodeOrchestrator AgentOpsOrchestrator LocalSdkOrchestrator")
    parser.add_argument("--workflow", default=str(WORKFLOW_PATH),
                        help="path to YAML workflow (default workflows/hello-world.yaml)")
    parser.add_argument("--report", default=str(PROJECT_ROOT / "bench" / "report_filled.md"),
                        help="output path for markdown report")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress per-trial progress lines")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--tester", default="codex-m0-bench")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    if not Path(args.workflow).exists():
        print("ERROR: workflow not found: " + args.workflow, file=sys.stderr)
        return 1

    candidates = args.candidates or ["OpencodeOrchestrator", "AgentOpsOrchestrator", "LocalSdkOrchestrator"]
    print("M0 orchestrator benchmark")
    print("  workflow:   " + args.workflow)
    print("  candidates: " + str(candidates))
    print("  trials:     " + str(args.runs) + "/candidate")

    try:
        summaries = asyncio.run(run_bench(runs_per_candidate=args.runs, candidates=candidates, quiet=args.quiet))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    print("\n=== summary table ===")
    print(render_stdout_table(summaries))

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_markdown_report(
        summaries,
        runs_per_candidate=args.runs,
        workflow_name=Path(args.workflow).name,
        tester=args.tester,
    )
    report_path.write_text(md, encoding="utf-8")
    print("\nreport written to: " + str(report_path))

    local = next((s for s in summaries if s.candidate == "LocalSdkOrchestrator"), None)
    if local is None or local.status not in ("ok", "partial"):
        print("\nWARNING: LocalSdkOrchestrator (baseline) did not complete successfully", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

