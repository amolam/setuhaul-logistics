"""The B-WF-2 approval workflow on AWS Step Functions (build item E4; BOOKING_WORKFLOW gate).

Implements the `ApprovalWorkflow` port that `LocalApprovalWorkflow` implements in tests, for the state machine in
`setuhaul/workflow/approval_workflow.asl.json`. Step Functions only waits and wakes: every decision is made and
recorded in PostgreSQL first, and PostgreSQL wins every disagreement (B-WF-2 design §9).

* `start` = StartExecution named after the approval id, so a second start is a no-op (ExecutionAlreadyExists).
* The WaitForDecision task stores its task token through `record_task_token` (migration 0015). If the request is
  already closed when the token arrives, the token is answered at once, so the execution never waits for nothing.
* `callback` reads the token and calls SendTaskSuccess. A finished, timed-out or unknown task is treated as done.
* The timeout path calls `expire_approval`; NOT_DUE (the database clock is behind) raises, so the state machine's
  Retry tries again a few seconds later.
* `reconcile` re-sends callbacks for requests that are no longer OPEN but still have a token (a lost callback).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("setuhaul.stepfunctions")

_DONE = ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken")


def _code(e: Exception) -> str:
    return ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")


class NotDueYet(RuntimeError):
    """expire_approval answered NOT_DUE: the Step Functions Retry runs it again."""


class StepFunctionsApprovalWorkflow:
    def __init__(self, db, state_machine_arn: str, client=None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if client is None:
            import boto3
            client = boto3.client("stepfunctions")
        self.db, self.arn, self.sfn, self.clock = db, state_machine_arn, client, clock

    def _facilities(self) -> tuple[str, ...]:
        with self.db.tx("system", readonly=True) as c:
            return tuple(r[0] for r in c.execute("SELECT facility_id FROM facilities"))

    # ---------------------------------------------------------------- the ApprovalWorkflow port
    def start(self, approval_id: str, deadline: datetime) -> bool:
        timeout_s = max(1, int((deadline - self.clock()).total_seconds()))
        try:
            self.sfn.start_execution(stateMachineArn=self.arn, name=str(approval_id),
                                     input=json.dumps({"approval_id": str(approval_id), "timeout_s": timeout_s}))
            return True
        except Exception as e:
            if _code(e) == "ExecutionAlreadyExists":
                return False
            raise

    def callback(self, approval_id: str) -> bool:
        with self.db.tx("system", self._facilities(), readonly=True) as c:
            row = c.execute("SELECT task_token FROM approval_requests WHERE approval_id = %s::uuid",
                            (str(approval_id),)).fetchone()
        if not row or not row[0]:
            return False                                           # no token yet; the token handler answers it
        return self._send(row[0])

    def _send(self, token: str) -> bool:
        try:
            self.sfn.send_task_success(taskToken=token, output="{}")
            return True
        except Exception as e:
            if _code(e) in _DONE:
                return False
            raise

    def reconcile(self, within_hours: int = 24) -> int:
        with self.db.tx("system", self._facilities(), readonly=True) as c:
            tokens = [r[0] for r in c.execute(
                """SELECT task_token FROM approval_requests
                    WHERE status <> 'OPEN' AND task_token IS NOT NULL
                      AND decided_at > clock_timestamp() - make_interval(hours => %s)""", (within_hours,))]
        return sum(self._send(t) for t in tokens)

    # ---------------------------------------------------------------- state machine task handlers
    def record_token(self, approval_id: str, token: str) -> bool:
        with self.db.tx("system", self._facilities()) as c:
            open_ = c.execute("SELECT record_task_token(%s::uuid, %s)", (str(approval_id), token)).fetchone()[0]
        if not open_:
            self._send(token)                                      # already decided: do not wait for the timeout
        return open_

    def expire(self, approval_id: str) -> str:
        with self.db.tx("system", self._facilities()) as c:
            outcome = c.execute("SELECT expire_approval(%s::uuid)", (str(approval_id),)).fetchone()[0]
        if outcome == "NOT_DUE":
            raise NotDueYet(str(approval_id))
        return outcome
