"""Model usage ledger and the application-level daily LLM budget (decision T52, migration 0023).

Every paid attempt is reserved in PostgreSQL before it is sent and settled after it. The reservation is conservative
(an upper bound for the request), the cap check and the reservation happen in one locked statement, so concurrent
attempts cannot pass the cap together, and each attempt has its own request id, so it can never be counted twice.
When the budget is exhausted the attempt is not sent: the caller gets a ModelError and the agent uses the rules
reader. Booking and approval logic never depend on this module.

Nothing here records prompt text, driver messages, extracted data or keys.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger("setuhaul.model_usage")


@contextmanager
def _ledger_tx(db):
    """The ledger is spend accounting, not part of the conversation: its transaction runs outside the turn fence.
    Inside a turn the agent's writes are fenced (assert_turn_fence), and the fence can only see the turn's receipt
    with the turn's driver or facility scope, which a ledger transaction has none of; fenced, every reservation failed
    with TURN_FENCED (seen on the AWS pilot, 22 Nov 2026). The cap is still checked in the same locked statement."""
    from ..api.db import TURN_FENCE
    token = TURN_FENCE.set(None)
    try:
        with db.tx("agent") as c:
            yield c
    finally:
        TURN_FENCE.reset(token)


@dataclass
class Reservation:
    request_id: uuid.UUID
    call_id: uuid.UUID
    attempt: int
    status: str                       # RESERVED | BUDGET_EXHAUSTED | DUPLICATE
    reserved_usd: Decimal


class ModelBudget:
    """Talks to reserve_model_call() / settle_model_call() as the agent role (the role whose turn makes the call)."""

    def __init__(self, db, environment: str, daily_cap_usd: Decimal | float | str):
        cap = Decimal(str(daily_cap_usd))
        if cap < 0:
            raise ValueError("the daily LLM budget cannot be negative")
        self.db, self.environment, self.daily_cap_usd = db, environment, cap

    def reserve(self, call_id: uuid.UUID, attempt: int, purpose: str, model: str, provider: str,
                reserve_usd: Decimal) -> Reservation:
        rid = uuid.uuid4()
        with _ledger_tx(self.db) as c:
            status = c.execute("SELECT reserve_model_call(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (rid, call_id, attempt, self.environment, purpose, model, provider,
                                reserve_usd, self.daily_cap_usd)).fetchone()[0]
        if status == "BUDGET_EXHAUSTED":
            log.warning("model_call budget_exhausted call=%s attempt=%s cap_usd=%s", call_id, attempt, self.daily_cap_usd)
        return Reservation(rid, call_id, attempt, status, reserve_usd)

    def settle(self, r: Reservation, *, ok: bool, sent: bool, actual_provider: str | None = None,
               input_tokens: int | None = None, output_tokens: int | None = None, cost_usd: float | None = None,
               latency_ms: int | None = None, error: str | None = None, generation_id: str | None = None) -> Decimal:
        with _ledger_tx(self.db) as c:
            charged = c.execute("SELECT settle_model_call(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                (r.request_id, "OK" if ok else "ERROR", actual_provider, input_tokens, output_tokens,
                                 None if cost_usd is None else Decimal(str(cost_usd)), sent, latency_ms, error,
                                 generation_id)).fetchone()[0]
        log.info("model_call settled call=%s attempt=%s status=%s provider=%s in=%s out=%s cost_usd=%s latency_ms=%s "
                 "error=%s", r.call_id, r.attempt, "OK" if ok else "ERROR", actual_provider, input_tokens,
                 output_tokens, charged, latency_ms, error)
        return Decimal(charged)
