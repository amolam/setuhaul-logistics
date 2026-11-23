"""Phase G3: structured, PII-free log events with correlation ids.

One JSON object per line on the `setuhaul.telemetry` logger (CloudWatch Logs in AWS, stderr locally):

    {"event": "optimizer.run", "correlation_id": "...", "facility_id": "FAC-JAI-01", "solver_status": "OPTIMAL", ...}

Rules (tests/test_phase_g_observability.py pins them):
  * every event carries `event` and `correlation_id` (a turn's or a request's uuid; never a phone, driver id or text);
  * keys that could carry personal data or message text are dropped before formatting, whatever the caller passes;
  * values are numbers, booleans, short codes or ids from the fixed vocabularies; long strings are cut to 120 chars.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

log = logging.getLogger("setuhaul.telemetry")
PII_KEYS = frozenset({"phone", "phone_e164", "driver_phone", "driver_id", "driver_name", "name", "text", "body",
                      "body_masked", "message", "reply", "say", "session", "session_token", "token", "email",
                      "recipient_ref", "note", "summary", "args", "arguments", "prompt"})


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def clean(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k.lower() in PII_KEYS:
            continue
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, str) and len(v) > 120:
            v = v[:120]
        elif not isinstance(v, (int, float, bool, str, type(None), list, dict)):
            v = str(v)[:120]
        out[k] = v
    return out


def emit(event: str, correlation_id: str | None, **fields) -> dict:
    """Log one structured event and return what was logged (tests read it back)."""
    rec = {"event": event, "correlation_id": correlation_id or new_correlation_id(), **clean(fields)}
    log.info(json.dumps(rec, sort_keys=True, default=str))
    return rec
