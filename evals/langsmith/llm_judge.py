"""Offline LLM judge (Part A): advisory scores for SUBJECTIVE reply quality, on synthetic datasets only.

Exactly three dimensions, each 1-5 with a short rationale, an exact quote from the reply as evidence, and an explicit
"insufficient evidence" flag (then the score is null, never guessed):

    quality:clarity         can a driver on the road act on it
    quality:tone            polite, calm, no blame
    quality:language_match  the conversation's language and script

What it may NEVER decide (NEVER_JUDGED; the deterministic evaluators in evaluators.py / g2_evaluators.py are the only
authority): authorisation, facility isolation, bookings, holds, approvals, constraints, idempotency, expiry,
fabrication, tools, extracted values. A judge score never changes an example's PASS/FAIL, never gates a release or a
deployment and never changes or re-sends an agent reply. This module is the OFFLINE judge (dev/eval datasets); nothing
in the application imports it. The pilot's advisory online judge (setuhaul/agent/online_judge.py, LLM_JUDGE.md section
12) shares the prompt, tool and validator through setuhaul/agent/judge_core.py, which this module re-exports.

Safety of the call:
  * the evaluated text (driver messages and the reply) is UNTRUSTED DATA: it is fenced, its angle brackets are
    neutralised, and the rubric tells the model to ignore any instruction inside it;
  * the answer must be one forced tool call matching JUDGE_TOOL; anything else, or a quote that is not in the reply,
    is malformed: one retry (a new, separately metered call), then the dimension scores are recorded as null;
  * paid calls go only through the existing metered OpenRouter client (pinned ZDR provider, data_collection deny,
    temperature 0) and the existing usage ledger (0023, purpose 'judge' since 0042), behind a per-run cap (RunBudget)
    of at most USD 1. The shared daily cap is the ledger's; nothing here keeps a second daily total.

Off unless: SETUHAUL_LLM_JUDGE=on, SETUHAUL_ENV in dev/eval, SETUHAUL_DATA_CLASS=synthetic and an owner-approved
SETUHAUL_LLM_JUDGE_BUDGET_USD (<= 1). The runner (judge_run.py) defaults to a dry run that makes no call at all.
"""
from __future__ import annotations

import os
import threading
import uuid
from decimal import Decimal

JUDGE_RUN_CAP_MAX_USD = Decimal("1.00")          # owner decision: no judge run may be allowed to spend more
JUDGE_DAILY_CAP_MAX_USD = Decimal("3.00")        # owner decision: the combined agent + judge daily cap on the ledger
JUDGE_ENVIRONMENTS = ("dev", "eval")

# The prompt, tool, validation and scoring loop live in setuhaul/agent/judge_core.py (one source for the offline and
# the online pilot judge); re-exported here unchanged.
from setuhaul.agent.judge_core import (  # noqa: E402,F401
    QUALITY_KEYS, DIMENSIONS, NEVER_JUDGED, JUDGE_PROMPT_VERSION, MAX_TOKENS, RATIONALE_MAX,
    EVIDENCE_MAX, LANGUAGE_NAMES, JudgeMalformed, JUDGE_SYSTEM, JUDGE_TOOL, _DIM,
    _fence, build_prompt, DimensionScore, Judgement, _WS, _norm,
    _keys, validate_judgement, score_reply, _acc)


class JudgeDisabled(RuntimeError):
    pass


# ---------------------------------------------------------------- gate
def judge_enabled(env: dict | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    try:
        cap = Decimal((env.get("SETUHAUL_LLM_JUDGE_BUDGET_USD") or "").strip())
    except Exception:
        return False
    return ((env.get("SETUHAUL_LLM_JUDGE") or "").strip().lower() == "on"
            and (env.get("SETUHAUL_ENV") or "").strip().lower() in JUDGE_ENVIRONMENTS
            and (env.get("SETUHAUL_DATA_CLASS") or "").strip().lower() == "synthetic"
            and cap.is_finite() and Decimal(0) < cap <= JUDGE_RUN_CAP_MAX_USD)


def _require_enabled(env: dict | None) -> None:
    if not judge_enabled(env):
        raise JudgeDisabled("LLM judge is off: subjective quality only; needs SETUHAUL_LLM_JUDGE=on, SETUHAUL_ENV=dev or "
                            "eval, SETUHAUL_DATA_CLASS=synthetic and an owner-approved SETUHAUL_LLM_JUDGE_BUDGET_USD "
                            f"(at most {JUDGE_RUN_CAP_MAX_USD})")


# ---------------------------------------------------------------- one judgement
def judge(example: dict, client, env: dict | None = None, dimensions=QUALITY_KEYS, max_attempts: int = 2) -> Judgement:
    """Score one dataset record, offline. Refused unless the offline gate is on (judge_enabled); the scoring itself is
    judge_core.score_reply (malformed answers get ONE retry unless max_attempts=1; budget refusals and provider errors
    propagate, so the runner stops)."""
    bad = [k for k in dimensions if k not in QUALITY_KEYS]
    if bad:
        raise ValueError(f"the LLM judge only scores {QUALITY_KEYS}; refused: {bad}")
    _require_enabled(env)
    return score_reply(example, client, dimensions, max_attempts)


# ---------------------------------------------------------------- the per-run cap, in front of the shared ledger
class RunBudget:
    """The judge's per-run cap (<= USD 1) layered IN FRONT of the shared usage ledger (metering.ModelBudget).

    It narrows, never widens: a call must fit the run cap here AND the daily cap in the ledger (reserve_model_call's
    locked statement, shared with the agent). Reservations held by this run count against the run cap until settled;
    a settle that fails keeps the reservation counted (conservative), exactly like the ledger. Thread-safe."""

    def __init__(self, ledger, run_cap_usd: Decimal | float | str):
        cap = Decimal(str(run_cap_usd))
        if not cap.is_finite() or cap <= 0 or cap > JUDGE_RUN_CAP_MAX_USD:
            raise ValueError(f"the judge run cap must be > 0 and <= {JUDGE_RUN_CAP_MAX_USD}")
        self.ledger, self.run_cap_usd = ledger, cap
        self.environment, self.daily_cap_usd = getattr(ledger, "environment", None), getattr(ledger, "daily_cap_usd", None)
        self._lock = threading.Lock()
        self._held: dict = {}
        self.charged_usd = Decimal(0)
        self.refused_by_run_cap = 0
        self.refused_by_daily_cap = 0
        self.calls_reserved = 0

    def committed(self) -> Decimal:
        with self._lock:
            return self.charged_usd + sum(self._held.values(), Decimal(0))

    def reserve(self, call_id, attempt, purpose, model, provider, reserve_usd):
        from setuhaul.agent.metering import Reservation
        if purpose != "judge":
            raise ValueError("RunBudget meters judge calls only")
        amount = Decimal(str(reserve_usd))
        slot = uuid.uuid4()
        with self._lock:
            if self.charged_usd + sum(self._held.values(), Decimal(0)) + amount > self.run_cap_usd:
                self.refused_by_run_cap += 1
                return Reservation(uuid.uuid4(), call_id, attempt, "BUDGET_EXHAUSTED", Decimal(0))
            self._held[slot] = amount                          # hold the run slot before asking the ledger
        try:
            r = self.ledger.reserve(call_id, attempt, purpose, model, provider, amount)
        except BaseException:
            with self._lock:
                self._held.pop(slot, None)
            raise
        with self._lock:
            self._held.pop(slot, None)
            if r.status == "RESERVED":
                self._held[r.request_id] = amount
                self.calls_reserved += 1
            elif r.status == "BUDGET_EXHAUSTED":
                self.refused_by_daily_cap += 1
        return r

    def settle(self, r, **kw):
        charged = Decimal(str(self.ledger.settle(r, **kw)))   # raises: the hold stays counted (conservative)
        with self._lock:
            self._held.pop(r.request_id, None)
            self.charged_usd += charged
        return charged
