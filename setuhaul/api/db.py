"""Database access in the RDS Data API pattern: one transaction per call, role and scope set inside it.

Locally this opens a psycopg connection per transaction. In AWS the same `tx()` contract is implemented over the
RDS Data API (BeginTransaction → ExecuteStatement… → Commit), which resets session state between calls, so scope
is always transaction-local and can never leak from one request to the next.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

import psycopg

# One database identity per responsibility (step 2 final review, P0-2). A read-only transaction is not a security
# boundary; a separate role is.
ROLES = {"agent": "setuhaul_agent",              # requests from the agent tools
         "console": "setuhaul_console",          # people, through the console
         "ingest": "setuhaul_ingest",            # verified inbound messages and confirmation attestations
         "feasibility": "setuhaul_feasibility",  # read feasibility inputs, publish computed options
         "system": "setuhaul_system"}            # background jobs (expiry, relay, reconciler, mirror sync)


# S4-02/S4-03: set by Orchestrator.handle for the duration of one turn: (message_id, owner, generation). Every write
# transaction the turn opens as the agent or feasibility role first proves, under the conversation lock, that this
# worker still holds the turn; a superseded worker commits nothing more.
TURN_FENCE: ContextVar[tuple[int, str, int] | None] = ContextVar("setuhaul_turn_fence", default=None)
FENCED_ROLES = ("agent", "feasibility")
# Phase G3: the correlation id of the current turn or console request (structured logs, optimizer runs, turn metrics).
CORRELATION_ID: ContextVar[str | None] = ContextVar("setuhaul_correlation_id", default=None)


def deployment_settings(deployment: dict | None) -> tuple[str, str, str, str]:
    """(environment, deployment id, code hash, correlation id) for the transaction's app.* settings (0030). Empty
    strings when this process declared no deployment: the guard then treats the caller as undeclared."""
    d = deployment or {}
    return (str(d.get("environment") or ""), str(d.get("deployment_id") or ""), str(d.get("code_sha256") or ""),
            str(CORRELATION_ID.get() or ""))


class Db:
    def __init__(self, dsn: str):
        self.dsn = dsn
        # 0030: the deployment this process runs, declared in every transaction for the database's booking evidence
        # guard ({"environment", "deployment_id", "code_sha256"}). Set by create_app once the start-up check accepted
        # the manifest; None otherwise (then a guarded database refuses this process's booking commitments).
        self.deployment: dict | None = None

    @contextmanager
    def tx(self, role: str, scope: tuple[str, ...] | list[str] | None = None, driver: str | None = None,
           readonly: bool = False, staff=None) -> Iterator[psycopg.Connection]:
        """`staff` = (principal, roles, facilities) from a verified Cognito token, console role only (B-WF-2 §15). The
        database records it for this transaction alone; approval functions trust nothing else."""
        if role not in ROLES:
            raise ValueError(role)
        if staff is not None and (role != "console" or readonly):
            raise ValueError("a staff identity belongs to a console write transaction")
        with psycopg.connect(self.dsn, autocommit=True) as c:
            with c.transaction():
                if readonly:
                    c.execute("SET TRANSACTION READ ONLY")
                c.execute(f"SET LOCAL ROLE {ROLES[role]}")
                c.execute("SET LOCAL search_path = setuhaul")
                c.execute("SET LOCAL TimeZone = 'Asia/Kolkata'")
                # one round trip for all transaction settings (0030 added the deployment declaration to it)
                c.execute("SELECT set_config('app.facility_scope', %s, true), set_config('app.driver_id', %s, true), "
                          "set_config('app.environment', %s, true), set_config('app.deployment_id', %s, true), "
                          "set_config('app.code_sha256', %s, true), set_config('app.correlation_id', %s, true)",
                          (",".join(scope or ()), driver or "", *deployment_settings(self.deployment)))
                if staff is not None:
                    principal, roles, facilities = staff
                    c.execute("SELECT begin_staff_context(%s,%s,%s)", (principal, sorted(roles), list(facilities)))
                fence = TURN_FENCE.get()
                if fence and not readonly and role in FENCED_ROLES:
                    c.execute("SELECT assert_turn_fence(%s,%s,%s)", fence)
                yield c
