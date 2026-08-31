"""
HTTP-based Orchestrator implementations for the M0 benchmark.

These two classes implement the `Orchestrator` protocol by talking to an
external HTTP service. They are intentionally thin: the protocol contract is
defined in `orchestrator/protocol.py`, and the bench runner uses the same
interface as `LocalSdkOrchestrator`.

Both classes share a common availability probe (`_probe`) so the runner can
distinguish "service unreachable" from "service answered but run failed".

Service endpoints (current M0 candidates):
  - OpencodeOrchestrator: opencode headless server on http://127.0.0.1:4096
  - AgentOpsOrchestrator: agentOps manager on http://127.0.0.1:19191

Both endpoints follow a similar shape:
  - POST /api/dag/workflows/sync  (upload YAML)
  - POST /api/runs                 (create a run)
  - POST /api/runs/{run_id}/invoke (start execution)
  - GET  /api/runs/{run_id}/events (stream events)
  - GET  /api/runs/{run_id}        (final state)

If the service is not reachable, `run()` raises `ServiceUnavailable` so the
runner can record "service_unavailable" instead of a fake completion.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from orchestrator import (
    DagEvent,
    DagEventType,
    Orchestrator,
    RawHarnessEvent,
    RunHandle,
    RunRequest,
    RunState,
    RunStatus,
)
from workflow import WorkflowDefinition, load_workflow_yaml

logger = logging.getLogger(__name__)


class ServiceUnavailable(RuntimeError):
    """Raised when the remote Orchestrator service is not reachable."""


class _HttpOrchestratorBase(Orchestrator):
    """Common HTTP plumbing for Opencode / AgentOps orchestrators.

    Subclasses override `_health_url`, `_sync_url`, `_create_run_url`,
    `_invoke_url`, `_events_url`, `_get_run_url` to point at their service.
    """

    # Override these in subclasses
    _health_url: str = ""
    _sync_url: str = ""
    _create_run_url: str = ""
    _invoke_url_tmpl: str = ""        # "/api/runs/{run_id}/invoke"
    _events_url_tmpl: str = ""        # "/api/runs/{run_id}/events?since={since}"
    _get_run_url_tmpl: str = ""       # "/api/runs/{run_id}"
    _default_workflow_id: str = "hello-world"
    _service_label: str = "remote"

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        run_timeout: float = 60.0,
        workflow_id: str | None = None,
        workflow_path: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.run_timeout = run_timeout
        self.workflow_id = workflow_id or self._default_workflow_id
        self.workflow_path = workflow_path
        self._workflow_text: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_HttpOrchestratorBase":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    # -------- availability probe --------

    async def probe(self) -> bool:
        """Best-effort liveness check. Returns True iff service responded."""
        client = self._ensure_client()
        try:
            resp = await client.get(self._health_url)
            return resp.status_code < 500
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, OSError):
            return False

    # -------- workflow registration --------

    def load_workflow_file(self, path: str) -> WorkflowDefinition:
        """Parse the workflow locally (we still validate before sending)."""
        wf = load_workflow_yaml(path)
        self.workflow_id = wf.workflow_id
        self.workflow_path = path
        self._workflow_text = Path(path).read_text(encoding="utf-8")
        return wf

    def register_workflow(self, workflow: WorkflowDefinition) -> None:
        self.workflow_id = workflow.workflow_id
        self._workflow_text = workflow.description or ""  # fallback; real YAML not stored

    # -------- lifecycle --------

    async def run(self, req: RunRequest) -> RunHandle:
        client = self._ensure_client()

        # 1) Sync the workflow YAML to the remote service
        if self._workflow_text is None:
            raise ServiceUnavailable(
                f"[{self._service_label}] no workflow loaded; call load_workflow_file() first"
            )

        try:
            sync_resp = await client.post(
                self._sync_url,
                json={"workflow_id": self.workflow_id, "yaml": self._workflow_text},
            )
            sync_resp.raise_for_status()
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, OSError) as e:
            raise ServiceUnavailable(f"[{self._service_label}] unreachable at {self.base_url}: {e}")

        # 2) Create a run record
        try:
            create_resp = await client.post(
                self._create_run_url,
                json={
                    "workflow_id": self.workflow_id,
                    "inputs": req.inputs,
                    "user_id": req.user_id,
                    "tenant_id": req.tenant_id,
                },
            )
            create_resp.raise_for_status()
            run_payload = create_resp.json()
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, OSError) as e:
            raise ServiceUnavailable(f"[{self._service_label}] create run failed: {e}")

        run_id = run_payload.get("run_id") or run_payload.get("id")
        if not run_id:
            raise ServiceUnavailable(
                f"[{self._service_label}] create run returned no run_id: {run_payload}"
            )

        started_at = datetime.now(timezone.utc)

        # 3) Kick off execution in background so the caller can stream events
        asyncio.create_task(self._invoke(run_id))

        return RunHandle(
            run_id=run_id,
            workflow_id=self.workflow_id,
            started_at=started_at,
            cancel_token=run_id,
        )

    async def _invoke(self, run_id: str) -> None:
        """POST /invoke — fire-and-forget; failures surface via get_run."""
        client = self._ensure_client()
        try:
            await client.post(self._invoke_url_tmpl.format(run_id=run_id), json={})
        except Exception as e:  # pragma: no cover - best effort
            logger.warning("[%s] invoke(%s) failed: %s", self._service_label, run_id, e)

    # -------- event streaming --------

    async def stream_events(
        self, run_id: str, since: int = 0
    ) -> AsyncIterator[DagEvent | RawHarnessEvent]:
        """Poll events endpoint until run reaches a terminal state.

        Returns DagEvent objects synthesized from the remote event stream.
        """
        client = self._ensure_client()
        seq = since
        terminal = False
        deadline = time.monotonic() + self.run_timeout
        # Surface any HTTP error as ServiceUnavailable so the bench runner can record it
        try:
            await client.get(self._get_run_url_tmpl.format(run_id=run_id))
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, OSError) as e:
            raise ServiceUnavailable(f"[{self._service_label}] stream_events get_run failed: {e}")

        while not terminal:
            if time.monotonic() > deadline:
                break
            try:
                resp = await client.get(
                    self._events_url_tmpl.format(run_id=run_id, since=seq)
                )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, OSError) as e:
                raise ServiceUnavailable(
                    f"[{self._service_label}] stream_events poll failed: {e}"
                )

            events = payload.get("events") or payload.get("data") or []
            for raw in events:
                seq += 1
                et = raw.get("type") or raw.get("event_type") or ""
                node_id = raw.get("node_id")
                ev_payload = raw.get("payload") or {}
                try:
                    dag_type = DagEventType(et)
                except ValueError:
                    # Treat unknown event as a raw harness event
                    yield RawHarnessEvent(
                        harness=self._service_label,
                        event_type=et,
                        raw_payload=raw,
                        run_id=run_id,
                        node_id=node_id,
                    )
                    continue
                yield DagEvent(
                    type=dag_type,
                    run_id=run_id,
                    node_id=node_id,
                    payload=ev_payload,
                    sequence=seq,
                )
                if dag_type in (DagEventType.RUN_COMPLETED, DagEventType.RUN_FAILED, DagEventType.RUN_CANCELLED):
                    terminal = True

            # Check final state too (some servers only set it on /runs/{id})
            if not terminal:
                try:
                    rs = await client.get(self._get_run_url_tmpl.format(run_id=run_id))
                    rs.raise_for_status()
                    body = rs.json()
                    status = (body.get("status") or body.get("data", {}).get("status") or "").lower()
                    if status in ("completed", "failed", "cancelled"):
                        # synthesize the terminal event if we missed it
                        if status == "completed":
                            yield DagEvent(type=DagEventType.RUN_COMPLETED, run_id=run_id, sequence=seq + 1)
                        elif status == "failed":
                            yield DagEvent(
                                type=DagEventType.RUN_FAILED,
                                run_id=run_id,
                                payload={"error": body.get("error") or "remote run failed"},
                                sequence=seq + 1,
                            )
                        else:
                            yield DagEvent(type=DagEventType.RUN_CANCELLED, run_id=run_id, sequence=seq + 1)
                        terminal = True
                except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, OSError):
                    # transient; keep polling until deadline
                    pass

            if not terminal:
                await asyncio.sleep(0.05)

    # -------- query --------

    async def get_run(self, run_id: str) -> RunState:
        client = self._ensure_client()
        try:
            resp = await client.get(self._get_run_url_tmpl.format(run_id=run_id))
            resp.raise_for_status()
            body = resp.json()
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, OSError) as e:
            raise ServiceUnavailable(f"[{self._service_label}] get_run failed: {e}")

        data = body.get("data") or body
        status_str = (data.get("status") or "unknown").lower()
        try:
            status = RunStatus(status_str)
        except ValueError:
            status = RunStatus.FAILED

        node_states = data.get("node_states") or {}
        node_outputs = data.get("node_outputs") or {}
        usage = data.get("usage") or {}
        return RunState(
            run_id=run_id,
            workflow_id=data.get("workflow_id", self.workflow_id),
            status=status,
            started_at=_parse_dt(data.get("started_at")) or datetime.now(timezone.utc),
            finished_at=_parse_dt(data.get("finished_at")),
            node_states=node_states,
            node_outputs=node_outputs,
            total_tokens_input=int(usage.get("input_tokens", 0) or 0),
            total_tokens_output=int(usage.get("output_tokens", 0) or 0),
            total_cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
            error=data.get("error"),
        )

    # -------- not supported in M0 benchmark --------

    async def inject(self, run_id: str, node_id: str, instruction: str) -> None:
        logger.debug("[%s] inject not supported in bench: %s/%s", self._service_label, run_id, node_id)

    async def abort(self, run_id: str, reason: str = "") -> None:
        client = self._ensure_client()
        try:
            await client.post(
                f"{self.base_url}/api/runs/{run_id}/cancel",
                json={"reason": reason},
            )
        except Exception:
            pass

    async def resume_node(self, run_id: str, node_id: str, instruction: str) -> None:
        logger.debug("[%s] resume_node not supported in bench", self._service_label)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(float(value))
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            try:
                return datetime.utcfromtimestamp(float(value))
            except ValueError:
                return None
    return None


# ====== A: Opencode headless (:4096) ======

class OpencodeOrchestrator(_HttpOrchestratorBase):
    """M0 candidate A — opencode headless service on port 4096.

    Service endpoints follow the convention opencode exposes when run as
    `bun run --cwd packages/opencode --conditions=browser src/index.ts serve`.
    The exact route shape may evolve; we degrade gracefully to
    ServiceUnavailable when the host is not answering.
    """

    _service_label = "opencode"
    _health_url = "http://127.0.0.1:4096/health"
    _sync_url = "http://127.0.0.1:4096/api/dag/workflows/sync"
    _create_run_url = "http://127.0.0.1:4096/api/runs"
    _invoke_url_tmpl = "http://127.0.0.1:4096/api/runs/{run_id}/invoke"
    _events_url_tmpl = "http://127.0.0.1:4096/api/runs/{run_id}/events?since={since}"
    _get_run_url_tmpl = "http://127.0.0.1:4096/api/runs/{run_id}"
    _default_workflow_id = "hello-world"

    def __init__(self, base_url: str = "http://127.0.0.1:4096", **kwargs):
        super().__init__(base_url=base_url, **kwargs)
        # Override URLs to reflect the actual base_url argument
        self._health_url = f"{self.base_url}/health"
        self._sync_url = f"{self.base_url}/api/dag/workflows/sync"
        self._create_run_url = f"{self.base_url}/api/runs"
        self._invoke_url_tmpl = f"{self.base_url}/api/runs/{{run_id}}/invoke"
        self._events_url_tmpl = f"{self.base_url}/api/runs/{{run_id}}/events?since={{since}}"
        self._get_run_url_tmpl = f"{self.base_url}/api/runs/{{run_id}}"


# ====== B: AgentOps manager (:19191) ======

class AgentOpsOrchestrator(_HttpOrchestratorBase):
    """M0 candidate B — agentOps manager service on port 19191.

    Talks to the agentops_manager HTTP API (see
    agentops/agentops_manager/src/server). The /api/dag/workflows/sync and
    /api/runs endpoints are the relevant ones.
    """

    _service_label = "agentops"
    _health_url = "http://127.0.0.1:19191/health"
    _sync_url = "http://127.0.0.1:19191/api/dag/workflows/sync"
    _create_run_url = "http://127.0.0.1:19191/api/runs"
    _invoke_url_tmpl = "http://127.0.0.1:19191/api/runs/{run_id}/invoke"
    _events_url_tmpl = "http://127.0.0.1:19191/api/runs/{run_id}/events?since={since}"
    _get_run_url_tmpl = "http://127.0.0.1:19191/api/runs/{run_id}"
    _default_workflow_id = "hello-world"

    def __init__(self, base_url: str = "http://127.0.0.1:19191", **kwargs):
        super().__init__(base_url=base_url, **kwargs)
        self._health_url = f"{self.base_url}/health"
        self._sync_url = f"{self.base_url}/api/dag/workflows/sync"
        self._create_run_url = f"{self.base_url}/api/runs"
        self._invoke_url_tmpl = f"{self.base_url}/api/runs/{{run_id}}/invoke"
        self._events_url_tmpl = f"{self.base_url}/api/runs/{{run_id}}/events?since={{since}}"
        self._get_run_url_tmpl = f"{self.base_url}/api/runs/{{run_id}}"


__all__ = [
    "OpencodeOrchestrator",
    "AgentOpsOrchestrator",
    "ServiceUnavailable",
]
