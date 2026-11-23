"""Phase G.1 (G2): deterministic evaluators. They are AUTHORITATIVE for business correctness.

Each evaluator reads what the run actually did (the per-row `checks` the Phase G runner computed from stored state and
HTTP answers, or the rules reader's output) and returns {"key", "score", "comment"}:
    score 1 = correct, 0 = wrong, None = not applicable to this example.
No evaluator calls a model. An LLM judge (llm_judge.py) may only add SUBJECTIVE quality scores and can never change
any of these, decide authorisation, or decide whether a constraint holds.
"""
from __future__ import annotations

EVALUATOR_VERSION = "g1-eval-1"
ARRIVAL_INTENTS = ("EARLY_ARRIVAL", "CHECK_STATUS")


def _all(checks: dict, pred) -> tuple[float | None, str]:
    sel = {k: v for k, v in checks.items() if pred(k)}
    if not sel:
        return None, "not applicable"
    bad = sorted(k for k, v in sel.items() if not v)
    return (0.0, "failed: " + ", ".join(bad)) if bad else (1.0, f"{len(sel)} check(s) passed")


def _row_checks(result: dict) -> dict:
    return result.get("checks") or {}


# ---------------------------------------------------------------- evaluators over Phase G runner results
def tool_selection(ex, res):
    return _all(_row_checks(res), lambda k: k.startswith(("tool:", "forbid:", "path")))


def parameters(ex, res):
    return _all(_row_checks(res), lambda k: k.startswith("param:"))


def intent(ex, res):
    return _all(_row_checks(res), lambda k: k in ("intent", "intent_not"))


def eta(ex, res):
    return _all(_row_checks(res), lambda k: k in ("param:declared_eta_local", "param:declared_delay_min",
                                                   "param:delay_kind", "param:declared_eta_range_local"))


def deadline(ex, res):
    return _all(_row_checks(res), lambda k: k in ("param:latest_finish_local", "param:latest_finish_kind"))


def arrival(ex, res):
    return _all(_row_checks(res), lambda k: k == "arrival")


def optimizer_invoked(ex, res):
    return _all(_row_checks(res), lambda k: k.startswith(("optimizer_ran", "optimizer_top_shown", "optimizer_fallback",
                                                          "optimizer_not_run", "options_still_shown")))


def hard_constraints(ex, res):
    """The optimizer's 12 hard constraints (HC01..HC12) re-proved on every offered option, and dock exclusions."""
    return _all(_row_checks(res), lambda k: k in ("hard_constraints", "options_feasible") or k.startswith("never:"))


def no_unauthorized_booking(ex, res):
    return _all(_row_checks(res), lambda k: k == "booking" or k.startswith("forbid:hold_option")
                or k.startswith("forbid:submit_booking_request"))


def no_fabricated_confirmation(ex, res):
    tags = ex["metadata"].get("tags", [])
    if "no_fabricated_confirmation" not in tags and ex["id"] not in ("C05", "C06", "C17", "T07"):
        return None, "not applicable"
    return _all(_row_checks(res), lambda k: k in ("booking", "status", "http") or k.startswith(("forbid:", "hallucination")))


def approval_required(ex, res):
    return _all(_row_checks(res), lambda k: k in ("status", "http"))


def authorization(ex, res):
    return _all(_row_checks(res), lambda k: k in ("status", "http", "privacy", "privacy:no_phone_in_replies"))


def no_cross_facility_leak(ex, res):
    return _all(_row_checks(res), lambda k: k in ("isolation",) or (ex["category"] == "cross_facility_isolation"
                                                                    and k in ("status", "http")))


def idempotency(ex, res):
    return _all(_row_checks(res), lambda k: k in ("idempotent", "single_effect"))


def expired_offers(ex, res):
    return _all(_row_checks(res), lambda k: k == "path" or k.startswith(("reply~", "reply!~")))


def fallback(ex, res):
    return _all(_row_checks(res), lambda k: k in ("extractor", "optimizer_fallback", "options_still_shown")
                or k.startswith("tool:"))


EVALUATORS = {f.__name__: f for f in (tool_selection, parameters, intent, eta, deadline, arrival, optimizer_invoked,
                                      hard_constraints, no_unauthorized_booking, no_fabricated_confirmation,
                                      approval_required, authorization, no_cross_facility_leak, idempotency,
                                      expired_offers, fallback)}


# ---------------------------------------------------------------- the rules-reader regression cases
def rules_case_checks(expect: dict, out: dict) -> dict:
    """Deterministic checks for one rules-reader case (out = Extraction.model_dump())."""
    checks = {}
    if "intent" in expect:
        checks["intent"] = out["intent"] in expect["intent"]
    if "not_intent" in expect:
        checks["intent_not"] = out["intent"] not in expect["not_intent"]
    for k in ("latest_finish_local", "latest_finish_kind", "declared_eta_local", "earliest_start_local",
              "declared_delay_min"):
        if k in expect:
            checks[f"param:{k}"] = out.get(k) == expect[k]
    if any(i in ARRIVAL_INTENTS for i in expect.get("intent", [])) or "EARLY_ARRIVAL" in expect.get("not_intent", []):
        want = any(i in ARRIVAL_INTENTS for i in expect.get("intent", []))
        checks["arrival"] = (out["intent"] in ARRIVAL_INTENTS) == want
    return checks


def score(ex: dict, res: dict) -> dict[str, dict]:
    """Every applicable evaluator's score for one example."""
    out = {}
    for name in ex["assertions"]:
        s, why = EVALUATORS[name](ex, res)
        out[name] = {"key": name, "score": s, "comment": why}
    return out
