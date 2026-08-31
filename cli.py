"""
v2.1 main entry — CLI for running workflows.

Usage:
    python -m ai_agent_platform_v2.cli run workflows/hello-world.yaml --input topic="..."
    python -m ai_agent_platform_v2.cli validate workflows/hello-world.yaml
    python -m ai_agent_platform_v2.cli list-tools
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Ensure package import works whether run as module or script
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
# 启动时加载 .env 文件（WECOM_WEBHOOK_URL / MINIMAX_API_KEY 等敏感配置）
load_dotenv(PROJECT_ROOT / ".env")

from orchestrator import LocalSdkOrchestrator, RunRequest
from workflow import load_workflow_yaml, validate_workflow, WorkflowValidationError


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def print_event(event) -> None:
    """Pretty-print a DagEvent to stdout."""
    seq = getattr(event, "sequence", 0)
    arrow = "→"
    et = str(event.type).split(".")[-1].upper().ljust(15)
    node = event.node_id or "-"
    payload_summary = ""
    if hasattr(event, "payload") and event.payload:
        if "duration_ms" in event.payload:
            payload_summary = f" (dur={event.payload['duration_ms']}ms)"
        elif "error" in event.payload:
            payload_summary = f" ({event.payload['error']})"
        elif "agent_text" in event.payload:
            text = str(event.payload["agent_text"])
            payload_summary = f' "{text[:80]}{"..." if len(text) > 80 else ""}"'
        elif "outputs" in event.payload:
            outputs = event.payload["outputs"]
            payload_summary = f" (outputs={list(outputs.keys()) if outputs else '[]'})"
    print(f"  [{seq:03d}] {arrow} {et} node={node:20s}{payload_summary}")


async def cmd_run(args) -> int:
    """Run a workflow and stream events."""
    setup_logging(args.verbose)

    workflow_path = Path(args.workflow)
    if not workflow_path.exists():
        print(f"ERROR: Workflow file not found: {workflow_path}", file=sys.stderr)
        return 1

    # Parse inputs from --input key=value
    inputs: dict = {}
    for kv in args.input or []:
        if "=" not in kv:
            print(f"ERROR: Invalid --input format: {kv} (expected key=value)", file=sys.stderr)
            return 1
        k, v = kv.split("=", 1)
        # Try to JSON-parse (for dict/list/int), else keep as string
        try:
            inputs[k] = json.loads(v)
        except json.JSONDecodeError:
            inputs[k] = v

    # Create orchestrator
    orch = LocalSdkOrchestrator(llm_config={
        "api_key": args.api_key or "",
        "base_url": args.base_url or "",
        "model": args.model or "",
    })

    # Load + validate workflow
    try:
        workflow = orch.load_workflow_file(str(workflow_path))
    except WorkflowValidationError as e:
        print(f"ERROR: Workflow validation failed:")
        for err in e.errors:
            print(f"  - {err}")
        return 1
    except Exception as e:
        print(f"ERROR: Failed to load workflow: {e}", file=sys.stderr)
        return 1

    print(f"╭─ Workflow: {workflow.workflow_id} ({workflow.name})")
    print(f"│  nodes: {len(workflow.nodes)}")
    print(f"│  inputs: {inputs}")
    print(f"╰─ starting run...")
    print()

    # Submit run
    req = RunRequest(workflow_id=workflow.workflow_id, inputs=inputs)
    handle = await orch.run(req)
    print(f"  run_id: {handle.run_id}")
    print()

    # Stream events
    print("─" * 60)
    print("DAG EVENTS:")
    print("─" * 60)
    final_state = None
    async for event in orch.stream_events(handle.run_id):
        if hasattr(event, "type"):
            print_event(event)
            # DagEventType is enum, compare via .value
            et_val = event.type.value if hasattr(event.type, "value") else str(event.type)
            if et_val == "run.completed":
                final_state = "COMPLETED"
            elif et_val == "run.failed":
                final_state = "FAILED"
                if hasattr(event, "payload") and event.payload and "error" in event.payload:
                    print(f"\n  ERROR: {event.payload['error']}", file=sys.stderr)
            elif et_val == "run.cancelled":
                final_state = "CANCELLED"

    print()
    print("─" * 60)
    print(f"FINAL: {final_state}")
    run_state = await orch.get_run(handle.run_id)
    if run_state:
        print(f"  duration: {(run_state.finished_at - run_state.started_at).total_seconds():.2f}s"
              if run_state.finished_at else "  (incomplete)")
        print(f"  total_tokens_in:  {run_state.total_tokens_input}")
        print(f"  total_tokens_out: {run_state.total_tokens_output}")

    return 0 if final_state == "COMPLETED" else 1


def cmd_validate(args) -> int:
    setup_logging(args.verbose)
    workflow_path = Path(args.workflow)
    try:
        workflow = load_workflow_yaml(str(workflow_path))
        # v88 新增：加载 config/agents/ 启用跨文件语义校验
        # agent 存在性 / agent 路由完备性 / output_files 匹配
        agent_configs = _load_agent_configs_for_validation(workflow_path)
        validate_workflow(workflow, agent_configs=agent_configs)
        print(f"OK: {workflow.workflow_id} ({workflow.name}) — {len(workflow.nodes)} nodes, 3 层校验通过")
        return 0
    except WorkflowValidationError as e:
        print(f"FAIL: Workflow validation ({len(e.errors)} error(s)):")
        for err in e.errors:
            print(f"  - {err}")
        if e.warnings:
            print(f"\nWARNINGS ({len(e.warnings)}):")
            for w in e.warnings:
                print(f"  - {w}")
        return 1
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _load_agent_configs_for_validation(workflow_path: Path) -> dict:
    """加载 config/agents/*.yaml 用于跨文件语义校验。

    查找策略：从 workflow_path 向上找项目根（含 config/agents/ 的目录）。
    找不到则返回空 dict（跳过跨文件校验，只跑单文件校验）。
    """
    try:
        from orchestrator.config_loader import ConfigLoader
        # 从 workflow 文件路径向上找 config/ 目录
        current = workflow_path.resolve().parent
        for _ in range(5):  # 最多向上 5 层
            config_dir = current / "config"
            if config_dir.exists():
                loader = ConfigLoader(config_dir=str(config_dir))
                return loader.load_all().agents
            parent = current.parent
            if parent == current:
                break
            current = parent
        return {}
    except Exception:
        # 任何加载异常都返回空 dict，不阻断校验（降级为单文件校验）
        return {}


def cmd_list_tools(args) -> int:
    from harness import HarnessRegistry
    print("Registered harnesses:")
    for h in HarnessRegistry.available():
        print(f"  - {h.value}")
    return None  # let main() handle exit code


def cmd_export_report(args) -> int:
    """导出 / 验证 / 列出报告导出历史。

    Examples:
        python cli.py export-report task_20260821_143015_abc123 report_abc123def456 --format md
        python cli.py export-report <tid> <rid> --format html
        python cli.py export-report <tid> <rid> --verify-only --format md
        python cli.py export-report <tid> <rid> --list-exports
    """
    setup_logging(args.verbose)
    import sqlite3
    import threading
    from task.exporter import ReportExporter

    db_path = PROJECT_ROOT / "audit.db"
    if not db_path.exists():
        print(f"ERROR: audit.db 不存在: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lock = threading.Lock()
    try:
        exporter = ReportExporter(conn=conn, db_lock=lock,
                                  workspace_root=PROJECT_ROOT / "workspace")
        exporter.ensure_schema()

        if args.list_exports:
            rows = exporter.list_exports(task_id=args.task_id, report_id=args.report_id)
            print(f"导出历史: task={args.task_id} report={args.report_id} count={len(rows)}")
            for r in rows:
                print(f"  - {r['exported_at']}  {r['format']:4s}  "
                      f"sha256={r['sha256'][:12]}...  size={r['size_bytes']}B  "
                      f"path={r['path']}")
            return 0

        if args.verify_only:
            r = exporter.verify_export(task_id=args.task_id, report_id=args.report_id,
                                       fmt=args.format)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0 if r.get("verified") else 2

        # 实际导出流程（CLI 同步上下文直接走 conn，不需要 TaskStore async wrapper）
        row = conn.execute(
            "SELECT * FROM task_reports WHERE report_id = ? AND task_id = ?",
            (args.report_id, args.task_id),
        ).fetchone()
        if not row:
            print(f"ERROR: report 不存在: task_id={args.task_id} report_id={args.report_id}",
                  file=sys.stderr)
            return 1
        report = dict(row)
        # JSON 列反转义（与 store.get_report 一致）
        for key in ("artifact_ids", "acceptance_self_check"):
            val = report.get(key)
            if isinstance(val, str) and val:
                try:
                    report[key] = json.loads(val)
                except json.JSONDecodeError:
                    pass

        r = exporter.export(report, fmt=args.format)
        print("EXPORTED:")
        print(f"  format:    {r['format']}")
        print(f"  export_id: {r['export_id']}")
        print(f"  path:      {r['path']}")
        print(f"  sha256:    {r['sha256']}")
        print(f"  size:      {r['size_bytes']} bytes")
        print(f"  exported_at: {r['exported_at']}")
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-ops",
        description="AgentOps CLI — multi-agent DAG orchestration",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="Run a workflow")
    p_run.add_argument("workflow", help="Path to YAML workflow file")
    p_run.add_argument("--input", "-i", action="append", help="Input as key=value (can repeat)")
    p_run.add_argument("--api-key", help="LLM API key (for non-deterministic harness)")
    p_run.add_argument("--base-url", help="LLM base URL")
    p_run.add_argument("--model", help="LLM model name")
    p_run.set_defaults(func=cmd_run)

    # validate
    p_val = subparsers.add_parser("validate", help="Validate workflow YAML")
    p_val.add_argument("workflow")
    p_val.set_defaults(func=cmd_validate)

    # list-tools
    p_lt = subparsers.add_parser("list-tools", help="List available harnesses")
    p_lt.set_defaults(func=cmd_list_tools)

    # export-report
    p_exp = subparsers.add_parser(
        "export-report",
        help="Export a task report to md/html/json (链路：读 DB → 格式转换 → 文件落盘 → SHA-256 校验)")
    p_exp.add_argument("task_id", help="task_reports.task_id")
    p_exp.add_argument("report_id", help="task_reports.report_id")
    p_exp.add_argument("--format", choices=["md", "html", "json"], default="md",
                       help="导出格式（默认 md）")
    p_exp.add_argument("--verify-only", action="store_true",
                       help="仅校验已导出文件 hash（不重新生成）")
    p_exp.add_argument("--list-exports", action="store_true",
                       help="列出该 report 的所有导出历史")
    p_exp.set_defaults(func=cmd_export_report)


    args = parser.parse_args()
    result = args.func(args)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result or 0


if __name__ == "__main__":
    sys.exit(main())
