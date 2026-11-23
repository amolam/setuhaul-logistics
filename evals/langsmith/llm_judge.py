"""Phase G.1 (G2): LLM-as-judge, for SUBJECTIVE reply quality only. Disabled; not run in G.1.

What it may judge (feedback keys in the `quality:` namespace, 1-5):
  quality:clarity        is the reply easy to act on for a driver on the road
  quality:tone           polite, calm, no blame
  quality:language_match the reply is in the driver's language/script (en / hi / hi-Latn)

What it may NEVER decide (the deterministic evaluators in evaluators.py are the only authority):
  authorisation, facility isolation, whether a booking/hold/approval happened or was allowed, hard constraints,
  idempotency, expired offers, fabricated confirmations, tool choice, extracted times. A judge score never changes an
  example's PASS/FAIL and is never used to gate a release.

Why it is off: every judged example is a paid model call. Turning it on needs the owner's approval of a budget
(SETUHAUL_LLM_JUDGE_BUDGET_USD) and runs in dev/eval only, on synthetic data, through the same metered OpenRouter
client (pinned provider, ZDR, daily ledger) as everything else.
"""
from __future__ import annotations

import os

QUALITY_KEYS = ("quality:clarity", "quality:tone", "quality:language_match")
NEVER_JUDGED = ("authorization", "no_cross_facility_leak", "no_unauthorized_booking", "approval_required",
                "hard_constraints", "idempotency", "expired_offers", "no_fabricated_confirmation", "tool_selection",
                "parameters", "deadline", "eta", "arrival")


class JudgeDisabled(RuntimeError):
    pass


def judge_enabled(env: dict | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    return ((env.get("SETUHAUL_LLM_JUDGE") or "").lower() == "on" and (env.get("SETUHAUL_ENV") or "") in ("dev", "eval")
            and bool((env.get("SETUHAUL_LLM_JUDGE_BUDGET_USD") or "").strip()))


def judge(example: dict, reply: str, env: dict | None = None) -> dict:
    """Placeholder interface. Raises unless explicitly enabled with an approved budget; even then G.1 ships no model
    call here (owner decision pending), so it raises JudgeDisabled."""
    if not judge_enabled(env):
        raise JudgeDisabled("LLM judge is off: subjective quality only, needs SETUHAUL_LLM_JUDGE=on, a dev/eval "
                            "environment and an owner-approved SETUHAUL_LLM_JUDGE_BUDGET_USD")
    raise JudgeDisabled("LLM judge model calls are not implemented in G.1 (paid calls need owner approval)")
