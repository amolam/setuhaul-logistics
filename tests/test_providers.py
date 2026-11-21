"""Phase B (decision T52): provider selection, OpenRouter pinning and failure handling, Secrets Manager credentials,
the usage ledger and the daily LLM budget, and the rules-reader fallback. No test makes a network call: OpenRouter
is an httpx MockTransport, Bedrock and Secrets Manager are stubs, and the budget runs on the local test database.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from decimal import Decimal

import httpx
import psycopg
import pytest

from setuhaul.agent.extraction import Extraction
from setuhaul.agent.metering import ModelBudget, Reservation
from setuhaul.agent.models import BedrockModel, FakeModel, ModelError, model_for, with_retry
from setuhaul.agent.openrouter import HAIKU_OR, OpenRouterModel, load_openrouter_key
from setuhaul.agent.orchestrator import Orchestrator
from setuhaul.api.db import Db
from test_api import Driver, add_driver, app, client, dsn, keys  # noqa: F401
from test_orchestrator import exd, in_90, options_of

KEY = "test-openrouter-key-0123456789abcdef"            # a fake key: it must never appear in a log or an error message
ARGS = {"intent": "REPORT_DELAY", "shipment_ref_mentioned": None, "delay_kind": "ARRIVAL_TIME",
        "declared_eta_local": "11:20", "declared_eta_range_local": None, "declared_delay_min": None,
        "earliest_start_local": None, "latest_finish_local": None, "latest_finish_kind": None,
        "exception_type": "TRAFFIC", "vehicle_fixed": None, "safety_concern": False, "selected_option_id": None,
        "explicit_confirmation": False, "language": "en"}
SYN = {"SETUHAUL_DATA_CLASS": "synthetic"}


def ok_reply(provider="Amazon Bedrock", cost=0.0012, args=ARGS):
    return httpx.Response(200, json={"id": "gen-1", "provider": provider,
                                     "usage": {"prompt_tokens": 900, "completion_tokens": 80, "cost": cost},
                                     "choices": [{"message": {"tool_calls": [{"function": {
                                         "name": "record_driver_report", "arguments": json.dumps(args)}}]}}]})


class Script:
    """Answers each request with the next scripted item: an httpx.Response or an exception to raise."""
    def __init__(self, *items):
        self.items, self.requests = list(items), []

    def __call__(self, req):
        self.requests.append(req)
        v = self.items.pop(0)
        if isinstance(v, Exception):
            raise v
        return v


class FakeMeter:
    def __init__(self, status="RESERVED", fail=False):
        self.status, self.fail, self.reserved, self.settled = status, fail, [], []

    def reserve(self, call_id, attempt, purpose, model, provider, reserve_usd):
        if self.fail:
            raise psycopg.OperationalError("db down")
        r = Reservation(uuid.uuid4(), call_id, attempt, self.status, reserve_usd)
        self.reserved.append(r)
        return r

    def settle(self, r, **kw):
        self.settled.append((r, kw))
        return Decimal("0")


def model(script, meter=None, **kw):
    waits = []
    m = OpenRouterModel(KEY, client=httpx.Client(transport=httpx.MockTransport(script)), meter=meter,
                        sleep=waits.append, **kw)
    return m, waits


# ================================================================ provider selection (model_for)
@pytest.mark.parametrize("env,error", [
    ({}, "SETUHAUL_ENV must be set"),
    ({"SETUHAUL_ENV": "production", **SYN, "SETUHAUL_MODEL_PROVIDER": "openrouter"}, "production is not enabled"),
    ({"SETUHAUL_ENV": "prod", **SYN, "SETUHAUL_MODEL_PROVIDER": "openrouter"}, "not allowed"),
    ({"SETUHAUL_ENV": "pilot", "SETUHAUL_MODEL_PROVIDER": "openrouter"}, "SETUHAUL_DATA_CLASS=synthetic"),
    ({"SETUHAUL_ENV": "eval", "SETUHAUL_DATA_CLASS": "real", "SETUHAUL_MODEL_PROVIDER": "openrouter"}, "synthetic"),
    ({"SETUHAUL_ENV": "eval", **SYN}, "must be openrouter or bedrock"),                        # no default provider
    ({"SETUHAUL_ENV": "eval", **SYN, "SETUHAUL_MODEL_PROVIDER": "gemini"}, "must be openrouter or bedrock"),
    ({"SETUHAUL_ENV": "eval", **SYN, "SETUHAUL_MODEL_PROVIDER": "openrouter", "OPENROUTER_API_KEY": KEY,
      "SETUHAUL_OR_PROVIDER": "anthropic"}, "not a verified ZDR route"),                       # not on the ZDR list
    ({"SETUHAUL_ENV": "pilot", **SYN, "SETUHAUL_MODEL_PROVIDER": "bedrock"}, "not metered yet"),
    ({"SETUHAUL_ENV": "eval", **SYN, "SETUHAUL_MODEL_PROVIDER": "bedrock", "SETUHAUL_BEDROCK_REGION": "us-east-1"},
     "not allowed"),
])
def test_provider_selection_fails_closed(env, error):
    with pytest.raises(ModelError, match=error) as e:
        model_for(env, bedrock_client=object())
    assert e.value.retryable is False


@pytest.mark.parametrize("environment", ["dev", "eval", "ci"])
def test_openrouter_with_an_environment_key_in_dev_eval_ci(environment):
    m = model_for({"SETUHAUL_ENV": environment, **SYN, "SETUHAUL_MODEL_PROVIDER": "openrouter", "OPENROUTER_API_KEY": KEY})
    assert isinstance(m, OpenRouterModel) and m.extract_model == HAIKU_OR and m.provider == "amazon-bedrock"
    assert m.meter is None                                                   # dev tools have their own run caps


def test_bedrock_stays_selectable_for_development_in_sydney():
    m = model_for({"SETUHAUL_ENV": "dev", **SYN, "SETUHAUL_MODEL_PROVIDER": "bedrock"}, bedrock_client=object())
    assert isinstance(m, BedrockModel) and m.region == "ap-southeast-2" and m.extract_model.startswith("au.")


class Secrets:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.asked = value, error, []

    def get_secret_value(self, SecretId):
        self.asked.append(SecretId)
        if self.error:
            raise self.error
        return {"SecretString": self.value}


PILOT = {"SETUHAUL_ENV": "pilot", **SYN, "SETUHAUL_MODEL_PROVIDER": "openrouter",
         "SETUHAUL_OPENROUTER_SECRET_ID": "setuhaul/pilot/openrouter", "SETUHAUL_LLM_DAILY_BUDGET_USD": "2.00"}


@pytest.mark.parametrize("environment", ["staging", "pilot"])
def test_staging_and_pilot_read_the_key_from_secrets_manager_and_meter_every_call(environment):
    s = Secrets(json.dumps({"api_key": KEY}))
    m = model_for({**PILOT, "SETUHAUL_ENV": environment}, db=object(), secrets_client=s)
    assert s.asked == ["setuhaul/pilot/openrouter"] and m._headers["Authorization"] == f"Bearer {KEY}"
    assert isinstance(m.meter, ModelBudget) and m.meter.daily_cap_usd == Decimal("2.00")
    assert m.phrase_model is None                                            # OD-3: no phrasing model in the pilot


@pytest.mark.parametrize("env,error", [
    ({**PILOT, "OPENROUTER_API_KEY": KEY}, "not accepted in pilot"),
    ({k: v for k, v in PILOT.items() if k != "SETUHAUL_OPENROUTER_SECRET_ID"}, "SECRET_ID is required"),
    ({k: v for k, v in PILOT.items() if k != "SETUHAUL_LLM_DAILY_BUDGET_USD"}, "DAILY_BUDGET_USD"),
    ({**PILOT, "SETUHAUL_LLM_DAILY_BUDGET_USD": "0"}, "must be positive"),
    ({**PILOT, "SETUHAUL_LLM_DAILY_BUDGET_USD": "lots"}, "number of US dollars"),
    ({**PILOT, "SETUHAUL_OR_EXTRACT_MODEL": "anthropic/claude-sonnet-5"}, "not approved for pilot"),
])
def test_pilot_configuration_fails_closed(env, error):
    with pytest.raises(ModelError, match=error):
        model_for(env, db=object(), secrets_client=Secrets(KEY))


def test_pilot_needs_the_usage_ledger():
    with pytest.raises(ModelError, match="usage ledger"):
        model_for(PILOT, db=None, secrets_client=Secrets(KEY))


def test_a_secret_that_cannot_be_read_fails_without_revealing_anything():
    class Denied(Exception):
        pass
    with pytest.raises(ModelError) as e:
        load_openrouter_key(PILOT, "pilot", Secrets(error=Denied(f"AccessDenied for {KEY}")))
    assert KEY not in str(e.value) and "Denied" in str(e.value) and e.value.__cause__ is None
    with pytest.raises(ModelError, match="holds no OpenRouter key"):
        load_openrouter_key(PILOT, "pilot", Secrets(json.dumps({"other": "x"})))
    assert load_openrouter_key(PILOT, "pilot", Secrets(f"  {KEY}\n")) == KEY            # a plain-text secret


# ================================================================ the request: pinned, no fallbacks, ZDR
def test_the_request_pins_model_and_provider_with_zero_retention_and_records_the_provider():
    s = Script(ok_reply())
    m, _ = model(s)
    out, tr = m.extract("Driver message: Reaching around 11:20")
    body = json.loads(s.requests[0].content)
    assert body["model"] == HAIKU_OR and "models" not in body and "route" not in body
    assert body["provider"] == {"order": ["amazon-bedrock"], "only": ["amazon-bedrock"], "allow_fallbacks": False,
                                "require_parameters": True, "data_collection": "deny", "zdr": True}
    assert (tr.requested_provider, tr.actual_provider, tr.cost_usd, tr.attempts) == ("amazon-bedrock", "Amazon Bedrock", 0.0012, 1)
    assert Extraction(**out).declared_eta_local == "11:20"


def test_an_answer_from_any_other_provider_is_refused():
    m, _ = model(Script(ok_reply(provider="Anthropic")))
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == "provider_mismatch"


# ================================================================ failures, retries and their accounting
@pytest.mark.parametrize("items,category,requests,waits", [
    ([httpx.Response(429, headers={"Retry-After": "1"}), ok_reply()], None, 2, [1.0]),
    ([httpx.Response(429, headers={"Retry-After": "30"})], "rate_limited", 1, []),     # too long to wait inside a turn
    ([httpx.Response(429), httpx.Response(429)], "rate_limited", 2, [0.5]),
    ([httpx.Response(500), ok_reply()], None, 2, []),
    ([httpx.Response(502), httpx.Response(502)], "server_error", 2, []),
    ([httpx.Response(503, json={"error": {"message": "No endpoints found matching your data policy"}})],
     "provider_unavailable", 1, []),                                               # no re-route, no retry
    ([httpx.Response(504)], "upstream_timeout", 1, []),
    ([httpx.Response(408)], "upstream_timeout", 1, []),
    ([httpx.Response(401, json={"error": {"message": "No auth credentials found"}})], "auth", 1, []),
    ([httpx.Response(403)], "auth", 1, []),
    ([httpx.Response(402)], "credits", 1, []),
    ([httpx.Response(400)], "bad_request", 1, []),
    ([httpx.ConnectError("refused"), ok_reply()], None, 2, []),
    ([httpx.ConnectTimeout("slow"), httpx.ConnectTimeout("slow")], "connect_timeout", 2, []),
    ([httpx.ReadTimeout("no answer")], "read_timeout", 1, []),                     # may be billed: never retried
    ([httpx.Response(200, content=b"<html>oops")], "malformed", 1, []),
])
def test_failures_are_bounded_final_and_categorised(items, category, requests, waits):
    s = Script(*items)
    m, slept = model(s)
    if category is None:
        _, tr = m.extract("x")
        assert tr.attempts == 2
    else:
        with pytest.raises(ModelError) as e:
            m.extract("x")
        assert e.value.category == category and e.value.retryable is False
    assert len(s.requests) == requests and slept == waits


@pytest.mark.parametrize("item,sent,cost", [
    (httpx.ConnectError("refused"), False, None),            # never left the process: charged nothing
    (httpx.ReadTimeout("no answer"), True, None),            # unknown: the ledger keeps the whole reservation
    (httpx.Response(429, headers={"Retry-After": "60"}), True, 0),   # OpenRouter rejected it before inference
    (httpx.Response(502), True, None),
])
def test_every_attempt_is_reserved_and_settled_with_what_is_known(item, sent, cost):
    meter = FakeMeter()
    m, _ = model(Script(item, item), meter)
    with pytest.raises(ModelError):
        m.extract("x")
    assert [r.attempt for r in meter.reserved] == list(range(1, len(meter.reserved) + 1))
    assert len(meter.settled) == len(meter.reserved)                         # one settlement per attempt, no more
    r, kw = meter.settled[0]
    assert kw["ok"] is False and kw["sent"] is sent and kw["cost_usd"] == cost
    assert all(x.call_id == meter.reserved[0].call_id for x in meter.reserved)   # retries share the logical call


def test_a_retry_is_two_attempts_and_only_the_answer_counts_as_success():
    meter = FakeMeter()
    m, _ = model(Script(httpx.Response(500), ok_reply(cost=0.0011)), meter)
    m.extract("x")
    (r1, k1), (r2, k2) = meter.settled
    assert (r1.attempt, k1["ok"], r2.attempt, k2["ok"]) == (1, False, 2, True)
    assert (k2["input_tokens"], k2["output_tokens"], k2["cost_usd"], k2["actual_provider"]) == (900, 80, 0.0011, "Amazon Bedrock")
    assert k2["latency_ms"] >= 0 and k2["generation_id"] == "gen-1"


@pytest.mark.parametrize("meter,category", [(FakeMeter(status="BUDGET_EXHAUSTED"), "budget_exhausted"),
                                             (FakeMeter(status="DUPLICATE"), "duplicate"),
                                             (FakeMeter(fail=True), "meter_error")])
def test_no_paid_request_is_sent_without_a_reservation(meter, category):
    s = Script(ok_reply())
    m, _ = model(s, meter)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == category and s.requests == []


def test_the_key_never_reaches_logs_or_errors(caplog):
    caplog.set_level(logging.DEBUG)
    for item in (httpx.Response(401, json={"error": {"message": "invalid key"}}), httpx.ReadTimeout("t"), ok_reply()):
        m, _ = model(Script(item), FakeMeter())
        try:
            m.extract("x")
        except ModelError as e:
            assert KEY not in str(e) and "Bearer" not in str(e)
    assert KEY not in caplog.text and "Bearer" not in caplog.text
    assert "ALERT" in caplog.text                                           # the 401 raised an alert line


def test_with_retry_does_not_repeat_a_final_error():
    calls = []

    def f():
        calls.append(1)
        raise ModelError("read timeout", "read_timeout", retryable=False)
    with pytest.raises(ModelError):
        with_retry(f)
    assert len(calls) == 1


# ================================================================ provider contract
def test_openrouter_and_bedrock_give_the_same_validated_extraction():
    class Stub:
        def converse(self, **kw):
            return {"output": {"message": {"content": [{"toolUse": {"name": "record_driver_report", "input": ARGS}}]}},
                    "usage": {"inputTokens": 900, "outputTokens": 80}, "metrics": {"latencyMs": 500}}
    a, _ = model(Script(ok_reply()))[0].extract("Driver message: Reaching around 11:20")
    b, _ = BedrockModel(client=Stub()).extract("Driver message: Reaching around 11:20")
    assert Extraction(**a) == Extraction(**b)


# ================================================================ the ledger and the daily budget (migration 0023)
def _day(dsn):
    with psycopg.connect(dsn) as c:
        r = c.execute("""SELECT coalesce(spent_usd,0), coalesce(reserved_usd,0) FROM setuhaul.model_budget_day
                         WHERE day = (clock_timestamp() AT TIME ZONE 'UTC')::date""").fetchone()
    return (Decimal(r[0]), Decimal(r[1])) if r else (Decimal(0), Decimal(0))


def _row(dsn, rid):
    with psycopg.connect(dsn) as c:
        cur = c.execute("SELECT * FROM setuhaul.model_usage WHERE request_id=%s", (rid,))
        return dict(zip([d.name for d in cur.description], cur.fetchone()))


def budget_with_room(dsn, room):
    spent, reserved = _day(dsn)
    return ModelBudget(Db(dsn), "ci", spent + reserved + Decimal(str(room)))


def test_reserve_and_settle_account_tokens_cost_and_latency(dsn):
    b = budget_with_room(dsn, "1.00")
    before = _day(dsn)
    r = b.reserve(uuid.uuid4(), 1, "extract", HAIKU_OR, "amazon-bedrock", Decimal("0.004"))
    assert r.status == "RESERVED" and _day(dsn) == (before[0], before[1] + Decimal("0.004"))
    charged = b.settle(r, ok=True, sent=True, actual_provider="Amazon Bedrock", input_tokens=900, output_tokens=80,
                       cost_usd=0.0012, latency_ms=640, generation_id="gen-1")
    assert charged == Decimal("0.0012") and _day(dsn) == (before[0] + Decimal("0.0012"), before[1])
    row = _row(dsn, r.request_id)
    assert (row["status"], row["cost_source"], row["input_tokens"], row["output_tokens"], row["latency_ms"],
            row["actual_provider"], row["requested_provider"]) == ("OK", "provider", 900, 80, 640, "Amazon Bedrock",
                                                                    "amazon-bedrock")
    with psycopg.connect(dsn) as c:                                         # the budget day is the UTC day
        assert row["day"] == c.execute("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date").fetchone()[0]
    assert b.settle(r, ok=True, sent=True, cost_usd=9.99) == Decimal("0.0012")          # settling twice changes nothing
    assert _day(dsn) == (before[0] + Decimal("0.0012"), before[1])


@pytest.mark.parametrize("sent,cost,charged,source", [(False, None, "0", "not_sent"), (True, None, "0.004", "reservation"),
                                                      (True, 0, "0", "provider")])
def test_failed_attempts_are_charged_what_is_known(dsn, sent, cost, charged, source):
    b = budget_with_room(dsn, "1.00")
    before = _day(dsn)
    r = b.reserve(uuid.uuid4(), 1, "extract", HAIKU_OR, "amazon-bedrock", Decimal("0.004"))
    assert b.settle(r, ok=False, sent=sent, cost_usd=cost, error="x") == Decimal(charged)
    assert _day(dsn) == (before[0] + Decimal(charged), before[1])
    assert (_row(dsn, r.request_id)["status"], _row(dsn, r.request_id)["cost_source"]) == ("ERROR", source)


def test_an_attempt_cannot_be_reserved_twice(dsn):
    b = budget_with_room(dsn, "1.00")
    call = uuid.uuid4()
    assert b.reserve(call, 1, "extract", HAIKU_OR, "amazon-bedrock", Decimal("0.004")).status == "RESERVED"
    before = _day(dsn)
    assert b.reserve(call, 1, "extract", HAIKU_OR, "amazon-bedrock", Decimal("0.004")).status == "DUPLICATE"
    assert _day(dsn) == before                                              # nothing more was reserved
    with psycopg.connect(dsn) as c:                                         # the same request id, another attempt
        rid = c.execute("SELECT request_id FROM setuhaul.model_usage WHERE call_id=%s", (call,)).fetchone()[0]
    with Db(dsn).tx("agent") as c:
        assert c.execute("SELECT reserve_model_call(%s,%s,2,'ci','extract',%s,'amazon-bedrock',0.004,1000)",
                         (rid, uuid.uuid4(), HAIKU_OR)).fetchone()[0] == "DUPLICATE"


def test_the_budget_is_never_exceeded_by_concurrent_attempts(dsn):
    b = budget_with_room(dsn, "0.050")
    results, barrier = [], threading.Barrier(20)

    def go():
        barrier.wait()
        results.append(b.reserve(uuid.uuid4(), 1, "extract", HAIKU_OR, "amazon-bedrock", Decimal("0.010")).status)
    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count("RESERVED") == 5 and results.count("BUDGET_EXHAUSTED") == 15
    spent, reserved = _day(dsn)
    assert spent + reserved <= b.daily_cap_usd


def test_an_exhausted_budget_records_the_refusal_and_sends_nothing(dsn):
    b = budget_with_room(dsn, "0.001")
    r = b.reserve(uuid.uuid4(), 1, "extract", HAIKU_OR, "amazon-bedrock", Decimal("0.004"))
    row = _row(dsn, r.request_id)
    assert (r.status, row["status"], row["error_category"], row["reserved_usd"]) == (
        "BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED", "budget_exhausted", 0)
    s = Script(ok_reply())
    m, _ = model(s, b)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == "budget_exhausted" and s.requests == []


def test_each_utc_day_has_its_own_budget(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("""INSERT INTO setuhaul.model_budget_day (day, environment, spent_usd)
                     VALUES ((clock_timestamp() AT TIME ZONE 'UTC')::date - 1, 'ci', 1000)
                     ON CONFLICT (day) DO UPDATE SET spent_usd = 1000""")
    b = budget_with_room(dsn, "0.010")                                      # yesterday's 1000 USD does not count
    assert b.reserve(uuid.uuid4(), 1, "extract", HAIKU_OR, "amazon-bedrock", Decimal("0.004")).status == "RESERVED"


def test_no_application_role_can_edit_the_ledger(dsn):
    for role in ("setuhaul_agent", "setuhaul_console", "setuhaul_system", "setuhaul_ingest"):
        with psycopg.connect(dsn) as c:
            c.execute(f"SET ROLE {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE setuhaul.model_budget_day SET spent_usd = 0")


# ================================================================ the orchestrator: always the rules reader
@pytest.mark.parametrize("category", ["budget_exhausted", "read_timeout", "provider_unavailable", "auth", "malformed",
                                      "provider_mismatch", "meter_error"])
def test_any_provider_failure_ends_in_the_rules_reader_and_booking_still_works(app, client, dsn, category):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    model = FakeModel(extractions=[ModelError(category, category, retryable=False), exd()])
    r = Orchestrator(app.state.service, app.state.sessions, model).handle(
        d.say(f"Traffic. Reaching around {in_90()}. Any slot?"))
    assert r.extractor == "rules-fallback" and r.fallback_reason == category
    assert len(model.calls) == 1                                            # not retried, no second provider
    assert r.reply.startswith("Available slots") and len(options_of(app, d)) >= 1


def test_an_answer_outside_the_schema_ends_in_the_rules_reader(app, client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    model = FakeModel(extractions=[{**ARGS, "approve_booking": True}])     # an extra key is off-contract
    r = Orchestrator(app.state.service, app.state.sessions, model).handle(
        d.say(f"Traffic. Reaching around {in_90()}. Any slot?"))
    assert r.extractor == "rules-fallback" and r.fallback_reason == "invalid_schema"


# ================================================================ R1: malformed answers, field by field (review of 59032f6)
GOOD_CHOICES = [{"message": {"tool_calls": [{"function": {"name": "record_driver_report", "arguments": json.dumps(ARGS)}}]}}]
PROBES = [
    {"provider": "Amazon Bedrock", "usage": "x", "choices": []},
    {"provider": "Amazon Bedrock", "usage": {"prompt_tokens_details": "x"}, "choices": []},
    {"provider": "Amazon Bedrock", "usage": {}, "choices": "oops"},
    {"provider": "Amazon Bedrock", "usage": {}, "choices": [{"message": {"tool_calls": "x"}}]},
    {"provider": "Amazon Bedrock", "usage": {}, "choices": [{"message": {"tool_calls": [{"function": "x"}]}}]},
]
MORE_SHAPES = [
    {"provider": "Amazon Bedrock", "choices": [None]},
    {"provider": "Amazon Bedrock", "choices": [{"message": "text"}]},
    {"provider": "Amazon Bedrock", "choices": [{"message": {"tool_calls": [
        {"function": {"name": "record_driver_report", "arguments": "[1, 2]"}}]}}]},       # JSON, but not an object
    {"provider": "Amazon Bedrock", "choices": [{"message": {"tool_calls": [
        {"function": {"name": "record_driver_report", "arguments": 42}}]}}]},
]


@pytest.mark.parametrize("body", PROBES + MORE_SHAPES)
def test_a_malformed_answer_is_a_classified_model_error_and_the_attempt_is_settled(body):
    meter = FakeMeter()
    m, _ = model(Script(httpx.Response(200, json=body)), meter)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert (e.value.category, e.value.retryable) == ("malformed", False)
    assert len(meter.settled) == 1                                          # settled once, whatever the shape


@pytest.mark.parametrize("body", PROBES)
def test_a_malformed_answer_ends_in_the_rules_reader_through_the_orchestrator(app, client, dsn, body):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    m, _ = model(Script(httpx.Response(200, json=body)), FakeMeter())
    r = Orchestrator(app.state.service, app.state.sessions, m).handle(d.say(f"Traffic. Reaching around {in_90()}. Any slot?"))
    assert r.extractor == "rules-fallback" and r.fallback_reason == "malformed"
    assert r.reply.startswith("Available slots") and len(options_of(app, d)) >= 1     # booking flow unaffected


@pytest.mark.parametrize("usage", ["x", ["a"], {"prompt_tokens": "900", "completion_tokens": "80"},
                                   {"prompt_tokens": -1, "completion_tokens": True},
                                   {"prompt_tokens": 900, "completion_tokens": 80, "prompt_tokens_details": "x"}])
def test_malformed_usage_metadata_is_unknown_never_trusted(usage):
    meter = FakeMeter()
    m, _ = model(Script(httpx.Response(200, json={"provider": "Amazon Bedrock", "usage": usage, "choices": GOOD_CHOICES})), meter)
    out, tr = m.extract("x")                                                # a valid answer is still used
    assert Extraction(**out).declared_eta_local == "11:20"
    _, kw = meter.settled[0]
    assert kw["ok"] is True and kw["cost_usd"] is None                      # unknown cost: the reservation is kept
    assert kw["input_tokens"] in (None, 900) and kw["output_tokens"] in (None, 80)
    assert isinstance(tr.input_tokens, int) and isinstance(tr.cache_read_tokens, int)


@pytest.mark.parametrize("cost,charged", [("0.001", None), (-1, None), (True, None), ([0.1], None), ({"usd": 1}, None),
                                          (float("inf"), None), (0, 0.0), (0.0012, 0.0012)])
def test_cost_metadata_is_used_only_when_it_is_a_real_amount(cost, charged):
    meter = FakeMeter()
    body = json.dumps({"provider": "Amazon Bedrock", "usage": {"prompt_tokens": 900, "completion_tokens": 80, "cost": cost},
                       "choices": GOOD_CHOICES})                            # json.dumps writes inf as Infinity
    m, _ = model(Script(httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})), meter)
    m.extract("x")
    assert meter.settled[0][1]["cost_usd"] == charged


@pytest.mark.parametrize("provider,category", [(None, "malformed"), ("", "malformed"), (123, "malformed"),
                                               (["Amazon Bedrock"], "malformed"), ("Anthropic", "provider_mismatch"),
                                               ("amazon bedrock", "provider_mismatch")])
def test_provider_metadata_must_name_the_pinned_provider_exactly(provider, category):
    meter = FakeMeter()
    body = {"usage": {"cost": 0.001}, "choices": GOOD_CHOICES}
    if provider is not None:
        body["provider"] = provider
    m, _ = model(Script(httpx.Response(200, json=body)), meter)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == category
    _, kw = meter.settled[0]
    assert kw["ok"] is False and kw["cost_usd"] == 0.001                    # billed: the real cost is still recorded


# ================================================================ R4: a spent limit is `credits`, a refusal is `auth`
@pytest.mark.parametrize("message,category", [
    ("Key limit exceeded: $3.00 of $3.00 used", "credits"),
    ("Workspace budget reached ($5 of $5 used)", "credits"),
    ("Guardrail spending limit reached", "credits"),
    ("Insufficient credits on this key", "credits"),
    ("Insufficient permissions", "auth"),
    ("Request blocked by moderation", "auth"),
    ("", "auth"),
])
def test_a_403_is_credits_only_when_it_names_a_spending_limit(message, category, caplog):
    s = Script(httpx.Response(403, json={"error": {"message": message}}))
    m, _ = model(s)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == category and len(s.requests) == 1 and "ALERT" in caplog.text


def test_a_403_body_that_is_not_json_is_auth():
    m, _ = model(Script(httpx.Response(403, content=b"<html>forbidden")))
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == "auth"


# ================================================================ programming errors are not hidden
def test_a_bug_in_the_ledger_code_is_not_swallowed():
    class Buggy(FakeMeter):
        def settle(self, r, **kw):
            raise RuntimeError("bug")
    m, _ = model(Script(ok_reply()), Buggy())
    with pytest.raises(RuntimeError):                                       # only database errors are tolerated
        m.extract("x")


# ================================================================ final review: an unusable answer is an unsuccessful attempt
def unusable_reply(choices):
    """HTTP 200 from the pinned route with real usage and cost, but an answer that cannot be used."""
    return httpx.Response(200, json={"id": "gen-9", "provider": "Amazon Bedrock",
                                     "usage": {"prompt_tokens": 900, "completion_tokens": 80, "cost": 0.0012},
                                     "choices": choices})


UNUSABLE = [[], "oops", [{"message": {"tool_calls": "x"}}], [{"message": {"tool_calls": [{"function": "x"}]}}],
            [{"message": {"content": "no tool call"}}],
            [{"message": {"tool_calls": [{"function": {"name": "record_driver_report", "arguments": "{not json"}}]}}],
            [{"message": {"tool_calls": [{"function": {"name": "record_driver_report", "arguments": "[1]"}}]}}]]


@pytest.mark.parametrize("choices", UNUSABLE)
def test_an_unusable_answer_is_charged_its_real_cost_but_settled_as_an_error(choices):
    meter = FakeMeter()
    m, _ = model(Script(unusable_reply(choices)), meter)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert (e.value.category, e.value.retryable) == ("malformed", False)
    assert len(meter.reserved) == 1 and len(meter.settled) == 1             # one attempt, settled exactly once
    kw = meter.settled[0][1]
    assert (kw["ok"], kw["error"], kw["sent"]) == (False, "malformed", True)
    assert (kw["cost_usd"], kw["input_tokens"], kw["output_tokens"], kw["actual_provider"], kw["generation_id"]) == \
           (0.0012, 900, 80, "Amazon Bedrock", "gen-9")


def test_a_usable_answer_is_still_settled_as_a_success():
    meter = FakeMeter()
    m, _ = model(Script(ok_reply()), meter)
    out, tr = m.extract("x")
    assert out == ARGS and len(meter.settled) == 1
    assert (meter.settled[0][1]["ok"], meter.settled[0][1]["error"]) == (True, None)


def test_an_empty_phrasing_reply_is_settled_as_an_error():
    meter = FakeMeter()
    m, _ = model(Script(unusable_reply([{"message": {"content": "  "}}])), meter, phrase_model="anthropic/claude-sonnet-5")
    with pytest.raises(ModelError) as e:
        m.phrase("facts")
    assert e.value.category == "malformed"
    assert len(meter.settled) == 1 and (meter.settled[0][1]["ok"], meter.settled[0][1]["cost_usd"]) == (False, 0.0012)


class RecordingBudget(ModelBudget):
    def __init__(self, *a, fail_settle=False, **kw):
        super().__init__(*a, **kw)
        self.fail_settle, self.reservations, self.settle_calls = fail_settle, [], 0

    def reserve(self, *a, **kw):
        r = super().reserve(*a, **kw)
        self.reservations.append(r)
        return r

    def settle(self, r, **kw):
        self.settle_calls += 1
        if self.fail_settle:
            raise psycopg.OperationalError("connection lost during settle")
        return super().settle(r, **kw)


def test_an_unusable_answer_is_recorded_in_the_ledger_as_an_error_with_its_real_cost(dsn):
    spent, reserved = _day(dsn)
    b = RecordingBudget(Db(dsn), "ci", spent + reserved + Decimal("1.00"))
    m, _ = model(Script(unusable_reply([{"message": {"content": "no tool call"}}])), b)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == "malformed"
    assert len(b.reservations) == 1 and b.settle_calls == 1
    row = _row(dsn, b.reservations[0].request_id)
    assert (row["status"], row["error_category"], row["cost_source"], row["cost_usd"], row["input_tokens"],
            row["output_tokens"], row["generation_id"]) == ("ERROR", "malformed", "provider", Decimal("0.0012"), 900, 80,
                                                            "gen-9")
    assert _day(dsn) == (spent + Decimal("0.0012"), reserved)               # charged once, reservation released
    with psycopg.connect(dsn) as c:                                         # one ledger row for the whole call
        assert c.execute("SELECT count(*) FROM setuhaul.model_usage WHERE call_id=%s",
                         (b.reservations[0].call_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("reply,category", [(lambda: unusable_reply([]), "malformed"),
                                            (lambda: ok_reply(provider="Other Cloud"), "provider_mismatch")])
def test_a_failed_settlement_neither_double_charges_nor_hides_the_original_error(dsn, reply, category):
    spent, reserved = _day(dsn)
    b = RecordingBudget(Db(dsn), "ci", spent + reserved + Decimal("1.00"), fail_settle=True)
    m, _ = model(Script(reply()), b)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == category                                     # the answer's error, not the database's
    assert len(b.reservations) == 1 and b.settle_calls == 1                 # no second attempt, no second settle
    r = b.reservations[0]
    row = _row(dsn, r.request_id)
    assert row["status"] == "RESERVED"                                      # the reservation stays held (conservative)
    assert _day(dsn) == (spent, reserved + row["reserved_usd"])             # counted once, as the reservation only


def test_a_failed_settlement_of_a_good_answer_still_returns_the_answer(dsn):
    spent, reserved = _day(dsn)
    b = RecordingBudget(Db(dsn), "ci", spent + reserved + Decimal("1.00"), fail_settle=True)
    m, _ = model(Script(ok_reply()), b)
    out, _ = m.extract("x")
    assert out == ARGS and b.settle_calls == 1
    assert _day(dsn) == (spent, reserved + _row(dsn, b.reservations[0].request_id)["reserved_usd"])


@pytest.mark.parametrize("choices", UNUSABLE)
def test_an_unusable_answer_through_the_orchestrator_gives_the_rules_reader_reply_and_options(app, client, dsn, choices):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    meter = FakeMeter()
    m, _ = model(Script(unusable_reply(choices)), meter)
    r = Orchestrator(app.state.service, app.state.sessions, m).handle(d.say(f"Traffic. Reaching around {in_90()}. Any slot?"))
    assert r.extractor == "rules-fallback" and r.fallback_reason == "malformed"
    assert r.reply.startswith("Available slots") and len(options_of(app, d)) >= 1
    assert len(meter.settled) == 1 and meter.settled[0][1]["ok"] is False
