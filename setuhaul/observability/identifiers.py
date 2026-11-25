"""Driver traceability: the one format gate for the internal ids that may appear in traces, logs and Ops metrics.

Only three kinds of id may leave the process to a trace sink (LangSmith) or a log sink (CloudWatch):

    driver_id    contains the token DRV-   e.g. DRV-JAI-0042, SYN-DRV-1a2b3c4d, DRV-TEST-1
    shipment_id  contains the token SHP-   e.g. SHP-TEST-1, SYN-SHP-1a2b3c4d-0
    facility_id  starts with FAC-          e.g. FAC-JAI-01

and only when the value passes ALL of:
  * 3-48 characters of [A-Za-z0-9_-], starting with a letter or digit (no spaces, '@', '+', '.', ':');
  * the kind's token is present (a driver id must say DRV-, a shipment id SHP-, a facility id must start FAC-);
  * no run of 7 or more digits (a phone number, a Telegram chat id or user id, an account number cannot hide in it).

Anything else exports as INVALID_ID ("[INVALID_ID]"), never the value and never a substitute id. A missing value stays
missing (None), so "no driver known" and "a driver id that failed the gate" are different in a trace.

**The gate is not proof that an id is synthetic.** A real driver could be given an id that passes it. What keeps real
data out of traces is that tracing refuses to start unless SETUHAUL_DATA_CLASS=synthetic (observability/langsmith.py).
If real-data tracing is ever proposed, ids must be replaced by a keyed HMAC pseudonym (key in Secrets Manager) with
explicit approval: docs/plans/LLM_JUDGE_AND_TRACE_DRIVER_ID_PLAN.md §B4.

Migration 0041 enforces the same rules (same regular expressions) on agent_turn_metrics.driver_id / shipment_id.
tests/test_driver_traceability.py pins every rule.
"""
from __future__ import annotations

import re

INVALID_ID = "[INVALID_ID]"
ID_KINDS = ("driver_id", "shipment_id", "facility_id")

_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$")
_LONG_DIGITS = re.compile(r"[0-9]{7,}")
_TOKEN = {
    "driver_id": re.compile(r"(^|-)DRV-"),
    "shipment_id": re.compile(r"(^|-)SHP-"),
    "facility_id": re.compile(r"^FAC-"),
}
# Keys (normalised: lower-case, '-' as '_') that carry one of the kinds above, wherever they appear in a payload.
KEY_KIND = {"driver_id": "driver_id", "shipment_id": "shipment_id", "thread_shipment_id": "shipment_id",
            "facility_id": "facility_id"}


def is_valid(kind: str, value) -> bool:
    """True when `value` is a well-formed id of `kind`. Never proof that the id is synthetic (see the module doc)."""
    if kind not in _TOKEN or not isinstance(value, str):
        return False
    return bool(_SHAPE.match(value)) and not _LONG_DIGITS.search(value) and bool(_TOKEN[kind].search(value))


def export_id(kind: str, value):
    """What a trace or log line may carry for this id: the id itself, None when absent, INVALID_ID otherwise."""
    if value is None or value == "":
        return None
    return value if is_valid(kind, value) else INVALID_ID


def stored_id(kind: str, value) -> str | None:
    """What agent_turn_metrics may store: the id, or None (a database column never holds the INVALID_ID marker)."""
    return value if is_valid(kind, value) else None
