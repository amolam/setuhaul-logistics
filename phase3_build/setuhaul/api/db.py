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


class Db:
    def __init__(self, dsn: str):
        self.dsn = dsn

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
                c.execute("SELECT set_config('app.facility_scope', %s, true)", (",".join(scope or ()),))
                c.execute("SELECT set_config('app.driver_id', %s, true)", (driver or "",))
                if staff is not None:
                    principal, roles, facilities = staff
                    c.execute("SELECT begin_staff_context(%s,%s,%s)", (principal, sorted(roles), list(facilities)))
                fence = TURN_FENCE.get()
                if fence and not readonly and role in FENCED_ROLES:
                    c.execute("SELECT assert_turn_fence(%s,%s,%s)", fence)
                yield c
