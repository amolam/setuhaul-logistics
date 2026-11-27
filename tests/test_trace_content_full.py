"""Workstream C (27 Nov fix plan, issue 8; owner decision Q3): full LangSmith trace content for the synthetic pilot.

Proves on synthetic examples, BEFORE the setting is enabled, that full content mode exports what a person needs to debug
a turn (driver text and context, the model's per-turn input and raw output, the prompt and schema version, tool calls
and results, rule outcomes and the decision path, correlation) and never what it must not (credentials, tokens,
secrets; driver phone numbers, e-mails, names, chat ids and other direct identifiers), while operational data that is
not driver PII stays readable. Nothing here leaves the process (a capturing exporter).
"""
from __future__ import annotations

import json
import uuid

import pytest

from setuhaul.agent.models import FakeModel
from setuhaul.agent.orchestrator import Orchestrator
from setuhaul.agent.prompts import PROMPT_VERSION
from setuhaul.observability import redaction, tracing
from setuhaul.observability.langsmith import content_mode_from_env
from test_api import GGN, JAI, Driver, add_driver, app, client, dsn, keys  # noqa: F401
from test_orchestrator import exd, in_90

SYNTHETIC_SECRETS = {
    "openrouter": "sk-or-v1-0123456789abcdef0123456789abcdef",
    "langsmith": "lsv2_pt_0123456789abcdef0123456789abcdef_0123456789",
    "jwt": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ",
    "aws_key_id": "AKIAABCDEFGHIJKLMNOP",
    "meta": "EAAGm0PX4ZCpsBAOZCZA0123456789abcdef",
    "bearer": "Bearer abcdefghijklmnop.qrstuvwxyz",
}
DRIVER = {"name": "Ramesh Kumar Synthetic", "phone": "+919812345678", "email": "ramesh.synthetic@example.com",
          "chat_id": "7123456789", "username": "ramesh_syn_driver"}


class Capture(tracing.Exporter):
    def __init__(self):
        self.batches = []

    def post(self, batch):
        self.batches.append(batch)


def _tracer(cap, mode):
    return tracing.Tracer(tracing.TracerConfig(enabled=True, project="setuhaul-synthetic-pilot", environment="pilot",
                                               content_mode=mode, content_allowed=mode != "off"), cap)


def _sample_turn() -> dict:
    """A synthetic turn as the spans record it."""
    return {
        "text": f"Hi this is {DRIVER['name']}, reschedule to 28 Nov 4 pm please. Call {DRIVER['phone']} or "
                f"{DRIVER['email']}. Telegram {DRIVER['chat_id']}",
        "model_input": f"Now (India time): 2026-11-27 10:30\nShipment ORD-2026-001 (SYN-SHP-TEST-1), current slot: D2 "
                       f"8:00 PM on 26 Nov CONFIRMED\nDriver message: reschedule to 28 Nov, I am {DRIVER['name']}",
        "raw_output": {"intent": "RESCHEDULE", "requested_date_local": "28 Nov", "shipment_ref_mentioned": None},
        "prompt_version": PROMPT_VERSION, "schema_version": "record_driver_report/v2",
        "system": "You read one WhatsApp message ... (the fixed instructions)",
        "result": {"status": "OK", "data": {"shipment_id": "SYN-SHP-TEST-1", "facility_id": "FAC-JAI-01",
                                            "options": [{"dock_code": "D2", "start": "4:00 PM on 28 Nov",
                                                         "option_id": "6f1c2b0e-1111-4222-8333-444455556666"}],
                                            "recent_driver_messages": [[41, f"I am {DRIVER['name']}"]]},
                   "requires_human": None},
        "rule": {"rule_id": "planner-move-earlier", "outcome": "NEEDS_PLANNER", "kind": "PLANNER_REVIEW"},
        "path": ["options", "reschedule"], "reason": "dock D3 under maintenance",
        "driver_id": "SYN-DRV-TEST-1", "appointment_id": "APT-0123456789abcdef", "correlation_id": str(uuid.uuid4()),
        # identifiers and credentials as they could appear by key
        "phone_e164": DRIVER["phone"], "driver_name": DRIVER["name"], "chat_id": DRIVER["chat_id"],
        "first_name": "Ramesh", "username": DRIVER["username"], "latitude": 26.91, "longitude": 75.78,
        "api_key": SYNTHETIC_SECRETS["openrouter"], "authorization": SYNTHETIC_SECRETS["bearer"],
        "session_token": "s3ss10n-t0k3n-value", "webhook_secret": "telegram-webhook-secret-0123456789",
        "note": f"keys seen: {SYNTHETIC_SECRETS['langsmith']} {SYNTHETIC_SECRETS['jwt']} {SYNTHETIC_SECRETS['aws_key_id']} "
                f"{SYNTHETIC_SECRETS['meta']} arn:aws:iam::123456789012:role/x",
    }


def _blob(x) -> str:
    return json.dumps(x, ensure_ascii=False)


# ================================================================ the redaction rules, on synthetic examples
def test_full_mode_keeps_what_debugging_needs():
    out = redaction.redact(_sample_turn(), mode="full", pii_terms=[DRIVER["name"], DRIVER["chat_id"]])
    assert "reschedule to 28 Nov 4 pm please" in out["text"]                      # driver message
    assert "Driver message: reschedule to 28 Nov" in out["model_input"]            # model input, per turn
    assert out["raw_output"]["requested_date_local"] == "28 Nov"                   # raw model output
    assert out["prompt_version"] == PROMPT_VERSION and out["schema_version"].endswith("/v2")
    assert out["result"]["data"]["options"][0]["start"] == "4:00 PM on 28 Nov"    # tool result
    assert out["rule"] == {"rule_id": "planner-move-earlier", "outcome": "NEEDS_PLANNER", "kind": "PLANNER_REVIEW"}
    assert out["path"] == ["options", "reschedule"] and out["reason"] == "dock D3 under maintenance"
    # operational data that is not driver PII stays readable
    assert out["driver_id"] == "SYN-DRV-TEST-1" and out["appointment_id"] == "APT-0123456789abcdef"
    assert out["result"]["data"]["facility_id"] == "FAC-JAI-01" and "SYN-SHP-TEST-1" in out["model_input"]
    assert "D2" in out["model_input"] and "ORD-2026-001" in out["model_input"]


def test_full_mode_removes_driver_identifiers_and_every_credential():
    out = redaction.redact(_sample_turn(), mode="full", pii_terms=[DRIVER["name"], DRIVER["chat_id"]])
    blob = _blob(out)
    for v in (DRIVER["name"], DRIVER["phone"], DRIVER["phone"][3:], DRIVER["email"], DRIVER["chat_id"],
              DRIVER["username"], *SYNTHETIC_SECRETS.values(), "123456789012", "s3ss10n-t0k3n-value",
              "telegram-webhook-secret-0123456789", "the fixed instructions"):
        assert v not in blob, v
    for k in ("phone_e164", "driver_name", "chat_id", "first_name", "username", "latitude", "longitude", "api_key",
              "authorization", "session_token", "webhook_secret", "system"):
        assert k not in out, k
    assert "[PII]" in out["text"] and "[PHONE]" in out["text"] and "[EMAIL]" in out["text"]
    assert "[PII]" in out["result"]["data"]["recent_driver_messages"][0][1]      # nested text too
    assert redaction.find_leaks(out, [DRIVER["name"], DRIVER["chat_id"]]) == []


@pytest.mark.parametrize("mode", ["off", "on"])
def test_lower_modes_are_unchanged_and_never_export_model_inputs(mode):
    out = redaction.redact(_sample_turn(), mode=mode, pii_terms=[DRIVER["name"]])
    assert "model_input" not in out and "raw_output" not in out and "system" not in out
    assert out["text"] == (redaction.REDACTED if mode == "off" else out["text"])
    assert DRIVER["name"] not in _blob(out) and DRIVER["phone"] not in _blob(out)


def test_full_mode_allows_long_text_but_bounded():
    long = "slot please " * 1000
    assert len(redaction.redact({"text": long}, mode="full")["text"]) <= redaction.MAX_STR_FULL + 1
    assert len(redaction.redact({"text": long}, mode="on")["text"]) <= redaction.MAX_STR + 1


def test_the_export_guard_drops_a_turn_that_still_contains_a_registered_identifier():
    cap = Capture()
    t = _tracer(cap, "full")
    with t.turn(str(uuid.uuid4())) as root:
        t.add_pii_terms(DRIVER["name"])
        # metadata is never content: a name smuggled into a span name would still be caught by the guard
        with t.span(f"call {DRIVER['name']}"):
            pass
    t.flush(1)
    assert not cap.batches and t.stats["blocked_leaks"] == 1


def test_full_content_is_for_the_synthetic_pilot_only():
    syn = {"SETUHAUL_DATA_CLASS": "synthetic", "SETUHAUL_TRACE_CONTENT": "full"}
    assert content_mode_from_env(syn, "pilot") == "full"
    for env_name in ("staging", "dev", "eval", "ci", "prod"):
        assert content_mode_from_env(syn, env_name) == "off"
    assert content_mode_from_env({**syn, "SETUHAUL_DATA_CLASS": "real"}, "pilot") == "off"
    assert content_mode_from_env({"SETUHAUL_TRACE_CONTENT": "on"}, "staging") == "on"
    assert content_mode_from_env({}, "pilot") == "off"


# ================================================================ a real turn, traced in full mode
@pytest.fixture
def full_traced():
    cap = Capture()
    old = tracing.tracer()
    tracing.install(_tracer(cap, "full"))
    yield cap
    tracing.install(old)


def test_a_real_turn_in_full_mode_shows_the_whole_decision_without_identifiers(app, client, dsn, full_traced):
    drv, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    with __import__("psycopg").connect(dsn, autocommit=True) as c:           # a synthetic driver name to mask
        c.execute("UPDATE setuhaul.drivers SET driver_name=%s WHERE driver_id=%s", (DRIVER["name"], drv))
    ev = d.say(f"Traffic. Reaching around {in_90()}. Any slot? I am {DRIVER['name']}, number {phone}")
    r = Orchestrator(app.state.service, app.state.sessions, FakeModel(extractions=[exd(
        delay_kind="ARRIVAL_TIME", declared_eta_local=in_90(), exception_type="TRAFFIC")])).handle(ev)
    tracing.tracer().flush(2)
    runs = [x for b in full_traced.batches for x in b]
    by = {x["name"]: x for x in runs}
    assert by, "the turn was not exported (blocked by the guard?)"
    blob = _blob(runs)
    # what a person debugging the turn needs
    assert "Traffic. Reaching around" in by["intent_extraction"]["inputs"]["text"]
    assert "Driver message: Traffic." in by["model_call"]["inputs"]["model_input"]
    assert by["model_call"]["inputs"]["prompt_version"] == PROMPT_VERSION
    assert by["model_call"]["outputs"]["raw_output"]["exception_type"] == "TRAFFIC"
    assert by["find_feasible_options"]["outputs"]["result"]["status"] in ("OK", "NOT_FEASIBLE")
    assert by["decide"]["outputs"]["path"] and by["final_response"]["outputs"]["reply"]
    assert by["agent_turn"]["extra"]["metadata"]["correlation_id"] == r.correlation_id
    # ... and none of the driver's identifiers
    assert DRIVER["name"] not in blob and phone not in blob and phone[3:] not in blob


def test_a_first_name_alone_is_masked_too(app, client, dsn, full_traced):
    """The driver writes only part of their registered name: each part is a registered identifier."""
    drv, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    with __import__("psycopg").connect(dsn, autocommit=True) as c:
        c.execute("UPDATE setuhaul.drivers SET driver_name=%s WHERE driver_id=%s", (DRIVER["name"], drv))
    ev = d.say(f"Ramesh here. Reaching around {in_90()}. Any slot?")
    Orchestrator(app.state.service, app.state.sessions, FakeModel(extractions=[exd(
        delay_kind="ARRIVAL_TIME", declared_eta_local=in_90(), exception_type="TRAFFIC")])).handle(ev)
    tracing.tracer().flush(2)
    runs = [x for b in full_traced.batches for x in b]
    assert runs, "the turn was not exported (blocked by the guard?)"
    blob = _blob(runs)
    assert "Ramesh" not in blob and "[PII] here" in blob
