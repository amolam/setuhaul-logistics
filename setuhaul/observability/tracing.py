"""Phase G.1 (G1): agent tracing with a run hierarchy, exported to LangSmith Cloud (synthetic data only).

    with tracing.turn(trace_id, name="agent_turn", metadata=...) as root:      # one trace per driver turn
        with tracing.span("intent_extraction", "chain") as s: ...                # children nest by context
            with tracing.span("model_call", "llm") as m: m.end(outputs=...)
        with tracing.span("find_feasible_options", "tool"): ...
            with tracing.span("optimizer", "chain"): ...                         # opened by the service, same context

Rules (docs/phase-g1/LANGSMITH_OBSERVABILITY.md):
  * Tracing never changes behaviour. Every span call is wrapped so that a tracing bug, a full buffer or an exporter
    failure is swallowed and counted: no request, booking, authorisation, tool execution or model call depends on it.
    Spans only RECORD what the code already did; they never call a tool or a model.
  * Nothing is exported unredacted: inputs, outputs, metadata and error text all pass `redaction.redact` when the run is
    finished (inside this process), before the run is queued.
  * Export is off unless configured (`tracer()` is a no-op Tracer by default). When on, finished turns go to a bounded
    in-memory queue; a background thread posts them in batches; the Lambda handler calls `flush(timeout)` after the turn
    has committed. A run that cannot be queued or posted is dropped and counted (`dropped`), never retried more than
    `max_attempts` times, never written to disk.
  * Ids: the root run id is the turn's `turn_ref` (a UUID), so the console can link a turn to its LangSmith trace and
    to its CloudWatch lines (which carry the same `turn_ref` and `correlation_id`).
"""
from __future__ import annotations

import contextvars
import logging
import random
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .redaction import find_leaks, redact

log = logging.getLogger("setuhaul.tracing")
RUN_TYPES = ("chain", "llm", "tool", "retriever", "prompt", "parser", "embedding")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dotted(start: datetime, run_id: str) -> str:
    return f"{start.strftime('%Y%m%dT%H%M%S%fZ')}{run_id}"


@dataclass
class Run:
    id: str
    trace_id: str
    name: str
    run_type: str
    start_time: datetime
    parent_run_id: str | None = None
    dotted_order: str = ""
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    error: str | None = None
    end_time: datetime | None = None

    def set(self, **metadata) -> "Run":
        """Add safe metadata (redacted at the end like everything else)."""
        self.metadata.update(metadata)
        return self

    def end(self, outputs: dict | None = None, error: str | None = None) -> "Run":
        if outputs:
            self.outputs.update(outputs)
        if error:
            self.error = error
        return self

    def as_post(self, project: str, content_allowed: bool) -> dict:
        """The LangSmith run-create body, redacted."""
        body = {"id": self.id, "trace_id": self.trace_id, "dotted_order": self.dotted_order, "name": self.name,
                "run_type": self.run_type, "start_time": self.start_time.isoformat(),
                "end_time": (self.end_time or _now()).isoformat(), "session_name": project,
                "inputs": redact(self.inputs, content_allowed=content_allowed),
                "outputs": redact(self.outputs, content_allowed=content_allowed),
                "extra": {"metadata": redact(self.metadata, content_allowed=False)},
                "tags": [str(t)[:40] for t in self.tags[:10]]}
        if self.parent_run_id:
            body["parent_run_id"] = self.parent_run_id
        if self.error:
            body["error"] = redact(str(self.error), content_allowed=False)
        return body


class _NullRun(Run):
    def __init__(self):
        super().__init__(id="", trace_id="", name="", run_type="chain", start_time=_now())

    def set(self, **metadata):
        return self

    def end(self, outputs=None, error=None):
        return self


NULL_RUN = _NullRun()
_current: contextvars.ContextVar[Run | None] = contextvars.ContextVar("setuhaul_trace_run", default=None)
_buffer: contextvars.ContextVar[list | None] = contextvars.ContextVar("setuhaul_trace_buffer", default=None)


@dataclass
class TracerConfig:
    enabled: bool = False
    project: str = "setuhaul-dev"
    environment: str = "dev"
    content_allowed: bool = False
    sample_rate: float = 1.0
    max_queue: int = 200                  # turns waiting to be exported; beyond this new turns are dropped
    max_runs_per_turn: int = 60
    max_attempts: int = 2                 # per batch: one retry, then dropped
    agent_version: str = "unknown"
    release_commit: str | None = None     # Full Activation: the git commit of the release (metadata only)
    ui_url: str | None = None             # for links only (console), never a credential


class Exporter:
    """Anything with `post(batch: list[dict]) -> None` (raises on failure). LangSmithExporter is the real one."""

    def post(self, batch: list[dict]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class Tracer:
    """Collects one turn's runs in a context-local buffer, redacts them when the turn ends, queues them for export."""

    def __init__(self, config: TracerConfig | None = None, exporter: Exporter | None = None):
        self.config = config or TracerConfig()
        self.exporter = exporter
        self._q: deque[list[dict]] = deque()
        self._lock = threading.Lock()
        self.stats = {"turns": 0, "runs": 0, "exported": 0, "dropped": 0, "export_errors": 0, "blocked_leaks": 0,
                      "tracing_errors": 0, "sampled_out": 0}

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.exporter is not None)

    # ------------------------------------------------------------ recording (never raises)
    @contextmanager
    def turn(self, trace_id: str, name: str = "agent_turn", inputs: dict | None = None, metadata: dict | None = None):
        """The root run of one trace. Yields a Run (a no-op one when tracing is off or this turn is sampled out)."""
        if not self.enabled:
            yield NULL_RUN
            return
        if self.config.sample_rate < 1.0 and random.random() >= self.config.sample_rate:
            self.stats["sampled_out"] += 1
            yield NULL_RUN
            return
        try:
            tid = str(uuid.UUID(str(trace_id)))
            start = _now()
            root = Run(id=tid, trace_id=tid, name=name, run_type="chain", start_time=start,
                       dotted_order=_dotted(start, tid), inputs=dict(inputs or {}),
                       metadata={"environment": self.config.environment, "agent_version": self.config.agent_version,
                                 "release_commit": self.config.release_commit, **(metadata or {})},
                       tags=["setuhaul", self.config.environment])
            buf = [root]
            tok_b, tok_r = _buffer.set(buf), _current.set(root)
        except Exception:                                   # noqa: BLE001 — tracing never breaks a turn
            self.stats["tracing_errors"] += 1
            yield NULL_RUN
            return
        try:
            yield root
        except BaseException as e:
            root.error = f"{type(e).__name__}"
            raise
        finally:
            try:
                root.end_time = _now()
                _current.reset(tok_r)
                _buffer.reset(tok_b)
                self._enqueue(buf)
            except Exception:                               # noqa: BLE001
                self.stats["tracing_errors"] += 1

    @contextmanager
    def span(self, name: str, run_type: str = "chain", inputs: dict | None = None, metadata: dict | None = None):
        """A child of the current run. Outside a traced turn (or with tracing off) it is a no-op."""
        parent, buf = _current.get(), _buffer.get()
        if parent is None or buf is None or isinstance(parent, _NullRun) or len(buf) >= self.config.max_runs_per_turn:
            yield NULL_RUN
            return
        try:
            rid, start = str(uuid.uuid4()), _now()
            run = Run(id=rid, trace_id=parent.trace_id, name=name[:60],
                      run_type=run_type if run_type in RUN_TYPES else "chain", start_time=start,
                      parent_run_id=parent.id, dotted_order=f"{parent.dotted_order}.{_dotted(start, rid)}",
                      inputs=dict(inputs or {}), metadata=dict(metadata or {}))
            buf.append(run)
            tok = _current.set(run)
        except Exception:                                   # noqa: BLE001
            self.stats["tracing_errors"] += 1
            yield NULL_RUN
            return
        try:
            yield run
        except BaseException as e:
            if not run.error:
                run.error = f"{type(e).__name__}"
            raise
        finally:
            try:
                run.end_time = _now()
                _current.reset(tok)
            except Exception:                               # noqa: BLE001
                self.stats["tracing_errors"] += 1

    def current(self) -> Run:
        r = _current.get()
        return r if r is not None else NULL_RUN

    # ------------------------------------------------------------ export (never raises)
    def _enqueue(self, runs: list[Run]) -> None:
        posts = [r.as_post(self.config.project, self.config.content_allowed) for r in runs]
        # ids, times and dotted_order are generated here (UUIDs, timestamps); the guard checks the payload fields
        leaks = find_leaks([{k: p.get(k) for k in ("inputs", "outputs", "extra", "error", "name", "tags")} for p in posts])
        if leaks:                                           # defence in depth: never export what still looks secret
            self.stats["blocked_leaks"] += 1
            log.warning("trace dropped by the export guard (%s pattern(s) still present)", len(leaks))
            return
        with self._lock:
            if len(self._q) >= self.config.max_queue:
                self.stats["dropped"] += len(posts)
                return
            self._q.append(posts)
            self.stats["turns"] += 1
            self.stats["runs"] += len(posts)

    def pending(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._q)

    def flush(self, timeout_s: float = 0.5) -> dict:
        """Post what is queued, in this thread, until done or `timeout_s` has passed. Returns counters. Called by the
        Lambda handler AFTER the turn has committed; whatever is not posted in time stays queued for the next flush of
        this container (or is lost with it: documented trace loss)."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._q:
                    break
                batch = self._q.popleft()
            ok = False
            for _ in range(max(1, self.config.max_attempts)):
                if time.monotonic() >= deadline:
                    break
                try:
                    self.exporter.post(batch)
                    ok = True
                    break
                except Exception as e:                      # noqa: BLE001 — export failure is non-critical
                    self.stats["export_errors"] += 1
                    log.warning("trace export failed (%s)", type(e).__name__)
            if ok:
                self.stats["exported"] += len(batch)
            else:
                self.stats["dropped"] += len(batch)
        return dict(self.stats, pending=self.pending())


_TRACER = Tracer()


def tracer() -> Tracer:
    return _TRACER


def install(t: Tracer) -> Tracer:
    """Replace the process tracer (Lambda cold start, tests)."""
    global _TRACER
    _TRACER = t
    return t


def span(name: str, run_type: str = "chain", inputs: dict | None = None, metadata: dict | None = None):
    return _TRACER.span(name, run_type, inputs, metadata)


def current() -> Run:
    return _TRACER.current()


def trace_url(ui_url: str | None, trace_id: str | None) -> str | None:
    """A link to one trace in the LangSmith UI, or None when the project link is not configured (never guessed)."""
    if not ui_url or not trace_id:
        return None
    try:
        tid = str(uuid.UUID(str(trace_id)))
    except ValueError:
        return None
    base = ui_url.rstrip("/")
    return f"{base}/r/{tid}?trace_id={tid}"


def safe(fn, *a, **kw) -> Any:
    """Run a tracing helper, swallow anything it raises."""
    try:
        return fn(*a, **kw)
    except Exception:                                       # noqa: BLE001
        _TRACER.stats["tracing_errors"] += 1
        return None
