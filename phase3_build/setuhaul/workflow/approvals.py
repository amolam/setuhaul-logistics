"""B-WF-2 approval wait (design/B-WF-2_approval_design.md §9).

    BookingRequested (outbox) ─▶ start(approval_id)            ← execution name = approval id: a second start is a no-op
       NotifyApprovers ─▶ WaitForDecision (until the deadline)
          ├─ ApprovalDecided (outbox) ─▶ callback ─▶ Done
          └─ deadline passes ─▶ expire_approval() in PostgreSQL ─▶ Done   ← changes nothing if already decided

The workflow never decides. Every outcome is recorded in PostgreSQL first, with its ApprovalDecided event; the driver's
notification (B-WF-3) reads that event, not this workflow. LocalApprovalWorkflow stands in for Step Functions locally;
approval_workflow.asl.json is the AWS definition, and a test keeps the two in step. Real Step Functions runs are step 6
acceptance (BOOKING_WORKFLOW gate).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger("setuhaul.workflow")

ASL_PATH = Path(__file__).with_name("approval_workflow.asl.json")
STATES = ("NotifyApprovers", "WaitForDecision", "ExpireApproval", "Done")
TRANSITIONS = {"NotifyApprovers": {"WaitForDecision"}, "WaitForDecision": {"Done", "ExpireApproval"},
               "ExpireApproval": {"Done"}, "Done": set()}
CONSUMER = "approval-workflow"


class ApprovalWorkflow(Protocol):
    def start(self, approval_id: str, deadline: datetime) -> bool: ...
    def callback(self, approval_id: str) -> bool: ...


@dataclass
class Execution:
    approval_id: str
    deadline: datetime
    state: str = "NotifyApprovers"
    history: list = field(default_factory=list)
    outcome: str | None = None

    def go(self, nxt: str):
        assert nxt in TRANSITIONS[self.state], (self.state, nxt)
        self.history.append(self.state)
        self.state = nxt


class LocalApprovalWorkflow:
    """In-process Step Functions stand-in with an injectable clock. `notify_approvers` is the reminder side effect."""

    def __init__(self, db, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 notify_approvers: Callable[[str], None] | None = None):
        self.db, self.clock, self.notify = db, clock, notify_approvers or (lambda _a: None)
        self.executions: dict[str, Execution] = {}
        self.stopped = False                                        # tests: the workflow is down

    def start(self, approval_id: str, deadline: datetime) -> bool:
        if self.stopped or approval_id in self.executions:
            return False                                            # ExecutionAlreadyExists: the same execution
        ex = Execution(approval_id, deadline)
        self.executions[approval_id] = ex
        self.notify(approval_id)
        ex.go("WaitForDecision")
        return True

    def callback(self, approval_id: str) -> bool:
        """SendTaskSuccess carrying the approval id only. Unknown or finished executions are treated as done
        (Step Functions answers TaskTimedOut / InvalidToken)."""
        ex = self.executions.get(approval_id)
        if self.stopped or ex is None or ex.state != "WaitForDecision":
            return False
        ex.go("Done")
        ex.outcome = "CALLBACK"
        return True

    def tick(self) -> int:
        """The timeout path: past the deadline, ask PostgreSQL to expire. PostgreSQL decides what actually happened."""
        if self.stopped:
            return 0
        n = 0
        for ex in self.executions.values():
            if ex.state == "WaitForDecision" and ex.deadline <= self.clock():
                ex.go("ExpireApproval")
                with self.db.tx("system", self._all_facilities()) as c:
                    ex.outcome = c.execute("SELECT expire_approval(%s)", (ex.approval_id,)).fetchone()[0]
                if ex.outcome == "NOT_DUE":                         # the local clock ran ahead of the database clock
                    ex.state = "WaitForDecision"
                    ex.history.pop()
                    continue
                ex.go("Done")
                n += 1
        return n

    def reconcile(self) -> int:
        """One-minute reconciler: finish executions whose request is no longer OPEN (a lost callback)."""
        running = [a for a, ex in self.executions.items() if ex.state == "WaitForDecision"]
        if not running:
            return 0
        with self.db.tx("system", self._all_facilities(), readonly=True) as c:
            closed = {str(r[0]) for r in c.execute(
                "SELECT approval_id FROM approval_requests WHERE approval_id = ANY(%s::uuid[]) AND status <> 'OPEN'",
                (running,))}
        return sum(self.callback(a) for a in closed)

    def _all_facilities(self) -> tuple[str, ...]:
        with self.db.tx("system", readonly=True) as c:
            return tuple(r[0] for r in c.execute("SELECT facility_id FROM facilities"))


class ApprovalOutboxRelay:
    """Feeds the workflow from the outbox. Each event is consumed once per consumer (outbox_consumptions, §17); the
    effect runs first and is idempotent (start by name, callback on a finished execution is a no-op), so a crash between
    effect and consumption only repeats a harmless effect."""

    def __init__(self, db, workflow: ApprovalWorkflow):
        self.db, self.wf = db, workflow

    def run_once(self) -> int:
        with self.db.tx("system", readonly=True) as c:
            facs = [r[0] for r in c.execute("SELECT facility_id FROM facilities")]
        with self.db.tx("system", facs, readonly=True) as c:
            events = c.execute("SELECT event_id, event_type, payload FROM pending_outbox_events(%s, %s)",
                               (CONSUMER, ["BookingRequested", "ApprovalDecided"])).fetchall()
        n = 0
        for event_id, kind, payload in events:
            payload = payload if isinstance(payload, dict) else json.loads(payload)
            if kind == "BookingRequested" and payload.get("approval_id"):
                deadline = payload["pending_expires_at"]
                deadline = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
                self.wf.start(str(payload["approval_id"]), deadline)
            elif kind == "ApprovalDecided":
                self.wf.callback(str(payload["approval_id"]))
            with self.db.tx("system", facs) as c:
                if c.execute("SELECT consume_outbox_event(%s,%s)", (CONSUMER, event_id)).fetchone()[0]:
                    n += 1
        return n


def asl_shape() -> tuple[set, dict]:
    """State names and transitions of the AWS definition, for the structural test."""
    asl = json.loads(ASL_PATH.read_text(encoding="utf-8"))
    states = set(asl["States"])
    edges = {}
    for name, st in asl["States"].items():
        nxt = set()
        if "Next" in st:
            nxt.add(st["Next"])
        for c in st.get("Catch", []):
            nxt.add(c["Next"])
        edges[name] = nxt
    return states, edges
