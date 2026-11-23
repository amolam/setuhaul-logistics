"""The B-WF-2 approval workflow on AWS Step Functions (build item E4; BOOKING_WORKFLOW gate).

Implements the `ApprovalWorkflow` port that `LocalApprovalWorkflow` implements in tests, for the state machine in
`setuhaul/workflow/approval_workflow.asl.json`. Step Functions only waits and wakes: every decision is made and
recorded in PostgreSQL first, and PostgreSQL wins every disagreement (B-WF-2 design §9).

* `start` = StartExecution named after the approval id, so a second start is a no-op (ExecutionAlreadyExists).
* The WaitForDecision task stores its task token through `record_task_token` (migration 0015). If the request is
  already closed when the token arrives, the token is kept with `record_late_task_token` (0024) and answered at once,
  so the execution never waits for nothing; if that answer fails, the reconciler retries it.
* `callback` reads the token and calls SendTaskSuccess. A finished, timed-out or unknown task is treated as done.
* The timeout path calls `expire_approval`; NOT_DUE (the database clock is behind) raises, so the state machine's
  Retry tries again a few seconds later.
* Completion tracking (review E4-01, migration 0024): a callback counts as done only on a definite answer from Step
  Functions (SendTaskSuccess returned, or the task had already finished), recorded by `complete_task_callback` in
  `approval_callbacks` (a decided approval row itself never changes). Throttling, a network error or any other unclear
  outcome records nothing, so the callback stays due.
* `reconcile` claims the closed requests whose callback is not confirmed and is due (`claim_task_callbacks`: each
  claim counts an attempt and backs off 1, 2, 4 ... 30 minutes), so a confirmed callback is never re-sent and an
  unconfirmed one is retried at a falling rate instead of every minute.
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
        if not self.arn:
            raise RuntimeError("CONFIG: SETUHAUL_APPROVAL_STATE_MACHINE_ARN is required to start approval executions")
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
        return self._answer(approval_id, row[0])

    def _answer(self, approval_id, token: str) -> bool:
        """SendTaskSuccess, then record the definite answer. Anything unclear raises before anything is recorded, so
        the token is kept and the reconciler tries again. True only when this call delivered the callback."""
        try:
            self.sfn.send_task_success(taskToken=token, output="{}")
            outcome = "DELIVERED"
        except Exception as e:
            if _code(e) not in _DONE:
                raise
            outcome = "TASK_FINISHED"                              # finished, timed out or unknown: nothing to wake
        with self.db.tx("system", self._facilities()) as c:
            c.execute("SELECT complete_task_callback(%s::uuid, %s)", (str(approval_id), outcome))
        return outcome == "DELIVERED"

    def reconcile(self, within_hours: int = 24, limit: int = 50) -> int:
        """Re-sends callbacks that are not confirmed and are due. Returns how many were delivered now."""
        with self.db.tx("system", self._facilities()) as c:          # the claim commits before anything is sent
            due = c.execute("SELECT approval_id, task_token FROM claim_task_callbacks(%s, %s)",
                            (within_hours, limit)).fetchall()
        delivered = 0
        for approval_id, token in due:
            try:
                delivered += self._answer(approval_id, token)
            except Exception as e:                                 # kept for the next due time; never swallowed silently
                log.warning("callback for approval %s not confirmed (%s); retried after the backoff",
                            approval_id, _code(e) or type(e).__name__)
        return delivered

    # ---------------------------------------------------------------- state machine task handlers
    def record_token(self, approval_id: str, token: str) -> bool:
        with self.db.tx("system", self._facilities()) as c:
            open_ = c.execute("SELECT record_task_token(%s::uuid, %s)", (str(approval_id), token)).fetchone()[0]
        if not open_:                                              # already decided: do not wait for the timeout
            with self.db.tx("system", self._facilities()) as c:
                c.execute("SELECT record_late_task_token(%s::uuid, %s)", (str(approval_id), token))
            try:
                self._answer(approval_id, token)
            except Exception as e:                                 # the token is stored; the reconciler retries it
                log.warning("late token for approval %s not answered yet (%s)", approval_id,
                            _code(e) or type(e).__name__)
        return open_

    def expire(self, approval_id: str) -> str:
        with self.db.tx("system", self._facilities()) as c:
            outcome = c.execute("SELECT expire_approval(%s::uuid)", (str(approval_id),)).fetchone()[0]
        if outcome == "NOT_DUE":
            raise NotDueYet(str(approval_id))
        return outcome
