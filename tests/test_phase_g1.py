"""Phase G.1: LangSmith tracing and redaction, the model-failure taxonomy, secret handling and key refresh, budget
accounting, the rules reader (I14 deadlines, I15 arrivals), console corrections and links, and the evaluation datasets.

No test makes a network call: OpenRouter and LangSmith are httpx MockTransports, Secrets Manager is a stub, and the
database is the local test cluster. Nothing here reads a real key or touches AWS.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import socket
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest

from setuhaul.agent import errors as E
from setuhaul.agent import rules_extractor
from setuhaul.agent.extraction import FACILITY_TZ, FACILITY_TZ_NAME, kind_stated, local_to_ts
from setuhaul.agent.models import DeferredModel, FakeModel, ModelError, Trace
from setuhaul.agent.openrouter import OpenRouterModel, load_openrouter_key_meta
from setuhaul.agent.orchestrator import TURN_NS, Orchestrator
from setuhaul.observability import redaction, tracing
from setuhaul.observability.langsmith import (LangSmithClient, LangSmithConfigError, load_langsmith_key,
                                              project_from_env, tracer_from_env)
from test_api import GGN, JAI, Driver, H, add_driver, app, client, dsn, keys, staff_token  # noqa: F401
from test_orchestrator import exd, in_90

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"          # fake, key-shaped: must never appear anywhere
LS_KEY = "lsv2_pt_0123456789abcdef0123456789abcdef_0123456789"


class Secrets:
    def __init__(self, value=None, error=None, version="v-1"):
        self.value, self.error, self.version, self.asked = value, error, version, 0

    def get_secret_value(self, SecretId):
        self.asked += 1
        if self.error:
            raise self.error
        return {"SecretString": self.value, "VersionId": self.version}


class ClientError(Exception):                                   # botocore.exceptions.ClientError's shape
    def __init__(self, code):
        super().__init__(f"An error occurred ({code}) for arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:x")
        self.response = {"Error": {"Code": code}}


PILOT = {"SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "synthetic", "SETUHAUL_MODEL_PROVIDER": "openrouter",
         "SETUHAUL_OPENROUTER_SECRET_ID": "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:setuhaul/pilot/openrouter",
         "SETUHAUL_LLM_DAILY_BUDGET_USD": "0.25"}


# ================================================================ G3: the taxonomy
def test_the_taxonomy_has_exactly_the_sixteen_categories_and_every_one_has_an_outcome_class():
    assert len(E.CATEGORIES) == 16 and set(E.OUTCOME_CLASS) == set(E.CATEGORIES)
    assert {E.outcome_class(c) for c in E.CATEGORIES} == {"NOT_STARTED", "BUDGET_REFUSAL", "PROVIDER_FAILURE"}
    for legacy, cat in E.LEGACY.items():                        # every name v0.3.0 wrote maps to one of the sixteen
        assert E.normalize(legacy) == cat in E.CATEGORIES
    assert E.normalize("something new") == "UNKNOWN_PROVIDER_ERROR"


@pytest.mark.parametrize("status,message,category,detail", [
    (401, "No auth credentials found", "PROVIDER_AUTH_ERROR", "auth"),
    (403, "Insufficient permissions", "PROVIDER_AUTH_ERROR", "auth"),
    (403, "Key limit exceeded: $3.00 of $3.00 used", "PROVIDER_AUTH_ERROR", "credits"),
    (402, "", "PROVIDER_AUTH_ERROR", "credits"),
    (429, "", "PROVIDER_RATE_LIMIT", "rate_limited"),
    (500, "", "PROVIDER_SERVER_ERROR", "server_error"),
    (503, "No endpoints found matching your data policy", "PROVIDER_SERVER_ERROR", "provider_unavailable"),
    (404, "", "MODEL_NOT_FOUND", "model_not_found"),
    (400, "anthropic/claude-x is not a valid model ID", "MODEL_NOT_FOUND", "model_not_found"),
    (400, "max_tokens must be positive", "INVALID_REQUEST", "bad_request"),
    (408, "", "REQUEST_TIMEOUT", "upstream_timeout"),
    (504, "", "REQUEST_TIMEOUT", "upstream_timeout"),
])
def test_http_answers_are_classified(status, message, category, detail):
    assert E.classify_http(status, message) == (category, detail)


def test_transport_failures_tell_dns_tls_and_network_apart_and_know_what_was_sent():
    dns = httpx.ConnectError("x")
    dns.__cause__ = socket.gaierror(-2, "Name or service not known")
    tls = httpx.ConnectError("x")
    tls.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    assert E.classify_transport(dns) == ("DNS_ERROR", "dns", False, True)
    assert E.classify_transport(tls) == ("TLS_ERROR", "tls", False, False)
    assert E.classify_transport(httpx.ConnectError("refused")) == ("NETWORK_ERROR", "connect_error", False, True)
    assert E.classify_transport(httpx.ConnectTimeout("slow")) == ("NETWORK_ERROR", "connect_timeout", False, True)
    assert E.classify_transport(httpx.ReadTimeout("slow")) == ("REQUEST_TIMEOUT", "read_timeout", True, False)


@pytest.mark.parametrize("secrets,category", [
    (Secrets(error=ClientError("ResourceNotFoundException")), "MISSING_SECRET"),
    (Secrets(error=ClientError("AccessDeniedException")), "SECRET_ACCESS_DENIED"),
    (Secrets(error=ClientError("DecryptionFailure")), "SECRET_ACCESS_DENIED"),
    (Secrets(error=ClientError("InternalServiceError")), "SECRET_RETRIEVAL_ERROR"),
    (Secrets(""), "EMPTY_SECRET"),
    (Secrets(json.dumps({"api_key": ""})), "EMPTY_SECRET"),
    (Secrets("{not json"), "SECRET_RETRIEVAL_ERROR"),
])
def test_secret_problems_are_classified_and_never_reveal_the_value_or_the_account(secrets, category):
    with pytest.raises(ModelError) as e:
        load_openrouter_key_meta(PILOT, "pilot", secrets)
    assert e.value.category == category and e.value.retryable is False
    assert "123456789012" not in str(e.value) and "arn:aws" not in str(e.value) and e.value.__cause__ is None
    assert E.outcome_class(e.value.category) == "NOT_STARTED"


def test_a_missing_secret_configuration_is_missing_secret():
    env = {k: v for k, v in PILOT.items() if k != "SETUHAUL_OPENROUTER_SECRET_ID"}
    with pytest.raises(ModelError) as e:
        load_openrouter_key_meta(env, "pilot", Secrets(KEY))
    assert e.value.category == "MISSING_SECRET"


def test_the_key_shape_is_described_without_the_key():
    key, meta = load_openrouter_key_meta(PILOT, "pilot", Secrets(json.dumps({"api_key": KEY}), version="ver-7"))
    assert key == KEY and meta == {"source": "secrets_manager", "version_id": "ver-7", "shape": "openrouter_format"}
    _, meta = load_openrouter_key_meta(PILOT, "pilot", Secrets("Zx9_random-placeholder-32-chars-long"))
    assert meta["shape"] == "unrecognized_format"                 # what the CloudFormation placeholder looks like
    assert E.key_shape("") == "empty" and KEY not in json.dumps(meta)


# ================================================================ G3: key refresh and the deferred client
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _ok():
    return httpx.Response(200, json={"id": "g", "provider": "Amazon Bedrock",
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001},
                                     "choices": [{"message": {"tool_calls": [{"function": {
                                         "name": "record_driver_report", "arguments": json.dumps(exd())}}]}}]})


def test_after_a_401_the_key_is_re_read_once_per_window_and_the_failed_call_is_never_repeated():
    seen, answers = [], [httpx.Response(401, json={"error": {"message": "invalid key"}}), _ok()]

    def handler(req):
        seen.append(req.headers["Authorization"])
        return answers.pop(0)
    clock, loads = Clock(), []

    def loader():
        loads.append(1)
        return "sk-or-v1-the-real-key-set-by-the-owner-0000"
    m = OpenRouterModel("placeholder-from-cloudformation", client=httpx.Client(transport=httpx.MockTransport(handler)),
                        key_loader=loader, key_refresh_s=300, clock=clock)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == "PROVIDER_AUTH_ERROR" and len(seen) == 1     # one request, no retry, no extra call
    clock.t += 299
    answers.insert(0, httpx.Response(401))
    with pytest.raises(ModelError):
        m.extract("x")                                                      # inside the window: not re-read
    assert loads == [] and len(seen) == 2
    clock.t += 301
    m.extract("x")                                                          # re-read, then one request with it
    assert loads == [1] and seen[-1] == "Bearer sk-or-v1-the-real-key-set-by-the-owner-0000"


def test_a_failed_re_read_is_a_secret_category_and_sends_nothing():
    clock, sent = Clock(), []

    def loader():
        raise ModelError("the secret holds no OpenRouter key", "EMPTY_SECRET", False)
    m = OpenRouterModel("x", client=httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(401))),
                        key_loader=loader, key_refresh_s=0, clock=clock)
    with pytest.raises(ModelError):
        m.extract("x")
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == "EMPTY_SECRET" and len(sent) == 1


def test_a_failed_call_reports_no_tokens_and_a_good_one_reports_what_the_provider_said():
    m = OpenRouterModel("k", client=httpx.Client(transport=httpx.MockTransport(lambda r: _ok())))
    _, tr = m.extract("x")
    assert (tr.input_tokens, tr.output_tokens, tr.cost_usd) == (10, 2, 0.00001)
    assert (Trace("extract", "m", "r").input_tokens, Trace("extract", "m", "r").output_tokens) == (None, None)


def test_a_secret_problem_degrades_to_the_rules_reader_and_is_retried_later():
    clock, built = Clock(), []

    def factory():
        built.append(1)
        if len(built) < 3:
            raise ModelError("the secret holds no OpenRouter key", "EMPTY_SECRET", False)
        return FakeModel(extractions=[exd()])
    m = DeferredModel(factory, retry_after_s=300, clock=clock).prime()
    assert m.error.category == "EMPTY_SECRET" and built == [1]
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.category == "EMPTY_SECRET" and e.value.detail == "not_started" and built == [1]   # not rebuilt yet
    clock.t += 301
    with pytest.raises(ModelError):
        m.extract("x")
    clock.t += 301
    assert m.extract("x")[0]["intent"] == "REPORT_DELAY" and built == [1, 1, 1]


def test_a_refused_environment_is_still_fatal_not_deferred():
    def factory():
        raise ModelError("CONFIG: SETUHAUL_DATA_CLASS=synthetic is required", "config", False)
    with pytest.raises(ModelError):
        DeferredModel(factory).prime()


def test_no_model_configured_is_not_an_error_and_never_crashes(monkeypatch):
    """Provider `none`: the factory returns None. The Lambda passes no model (rules reader, as before G.1), and a
    DeferredModel asked to extract anyway raises a ModelError (the turn falls back), never an AttributeError."""
    from setuhaul.aws import lambdas
    m = DeferredModel(lambda: None).prime()
    assert m.client is None and m.error is None
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.detail == "not_configured"
    monkeypatch.setattr("setuhaul.agent.models.model_for", lambda env, db=None, **kw: None)
    monkeypatch.setattr(lambdas, "runtime", lambda: type("R", (), {"state": type("S", (), {
        "service": type("V", (), {"db": None})(), "sessions": None})()})())
    assert lambdas._orchestrator().model is None


# ================================================================ G1: redaction
def test_redaction_drops_personal_and_secret_keys_and_scrubs_strings():
    raw = {"phone": "+919812345678", "session_token": "abc", "Authorization": "Bearer x", "api_key": KEY,
           "text": "call me on 9812345678", "note": "free text",
           "meta": {"x-api-key": LS_KEY, "who": "driver.one@example.com", "arn": "arn:aws:iam::123456789012:role/x",
                    "jwt": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbHVl", "aws": "AKIAABCDEFGHIJKLMNOP",
                    "said": f"key {KEY} and {LS_KEY} and +99900000001234"},
           "ok": {"intent": "REPORT_DELAY", "latest_finish_local": "17:00", "turn_ref": str(uuid.uuid4())}}
    out = redaction.redact(raw)
    text = json.dumps(out)
    for leaked in (KEY, LS_KEY, "9812345678", "driver.one@example.com", "123456789012", "AKIAABCDEFGHIJKLMNOP",
                   "eyJhbGciOiJSUzI1NiJ9", "+99900000001234", "abc"):
        assert leaked not in text, leaked
    assert "phone" not in out and "session_token" not in out and "Authorization" not in out and "api_key" not in out
    assert out["text"] == out["note"] == redaction.REDACTED                  # content is off by default
    assert out["ok"]["latest_finish_local"] == "17:00" and out["ok"]["turn_ref"] == raw["ok"]["turn_ref"]
    assert redaction.find_leaks(out) == []
    on = redaction.redact({"text": "late, call +919812345678"}, content_allowed=True)
    assert on["text"] == "late, call [PHONE]"                               # content on: still scrubbed


# ================================================================ G1: the tracer
class Capture(tracing.Exporter):
    def __init__(self, fail=False):
        self.batches, self.fail = [], fail

    def post(self, batch):
        if self.fail:
            raise httpx.ConnectError("langsmith down")
        self.batches.append(batch)


def _tracer(exporter, **cfg):
    return tracing.Tracer(tracing.TracerConfig(enabled=True, project="setuhaul-dev", environment="ci", **cfg), exporter)


def test_spans_nest_into_one_trace_with_langsmith_ids():
    cap = Capture()
    t = _tracer(cap)
    tid = str(uuid.uuid4())
    with t.turn(tid, inputs={"text": "hello"}) as root:
        with t.span("intent_extraction") as a:
            with t.span("model_call", "llm") as b:
                b.end({"intent": "REPORT_DELAY"})
        with t.span("find_feasible_options", "tool"):
            pass
    t.flush(1)
    runs = {r["name"]: r for r in cap.batches[0]}
    assert runs["agent_turn"]["id"] == tid == runs["model_call"]["trace_id"]
    assert runs["intent_extraction"]["parent_run_id"] == tid and runs["model_call"]["parent_run_id"] == a.id
    assert runs["model_call"]["dotted_order"].startswith(runs["intent_extraction"]["dotted_order"] + ".")
    assert runs["model_call"]["run_type"] == "llm" and runs["find_feasible_options"]["run_type"] == "tool"
    assert runs["agent_turn"]["inputs"]["text"] == redaction.REDACTED and root.id == tid and b.id


def test_tracing_off_is_a_no_op_and_spans_outside_a_turn_record_nothing():
    t = tracing.Tracer()
    with t.turn(str(uuid.uuid4())) as root, t.span("x") as s:
        assert root is tracing.NULL_RUN and s is tracing.NULL_RUN
    assert t.pending() == 0 and not t.enabled


def test_an_exporter_failure_is_counted_bounded_and_never_raised():
    t = _tracer(Capture(fail=True), max_attempts=2)
    with t.turn(str(uuid.uuid4())):
        pass
    stats = t.flush(1)
    assert stats["export_errors"] == 2 and stats["dropped"] == 1 and stats["pending"] == 0


def test_a_full_queue_drops_new_turns_instead_of_growing():
    t = _tracer(Capture(), max_queue=2)
    for _ in range(5):
        with t.turn(str(uuid.uuid4())):
            pass
    assert t.pending() == 2 and t.stats["dropped"] == 3


def test_the_export_guard_drops_a_trace_that_still_looks_secret():
    t = _tracer(Capture())
    with t.turn(str(uuid.uuid4())) as root:
        root.tags.append("+919812345678")                             # tags are not redacted: the guard catches it
    assert t.pending() == 0 and t.stats["blocked_leaks"] == 1


def test_an_exception_in_the_turn_is_recorded_as_its_type_and_still_raised():
    cap = Capture()
    t = _tracer(cap)
    with pytest.raises(ValueError):
        with t.turn(str(uuid.uuid4())):
            with t.span("decide"):
                raise ValueError(f"secret {KEY}")
    t.flush(1)
    errs = {r["name"]: r.get("error") for r in cap.batches[0]}
    assert errs == {"agent_turn": "ValueError", "decide": "ValueError"}


@pytest.mark.parametrize("env,why", [
    ({"SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "synthetic", "SETUHAUL_LANGSMITH_TRACING": "on",
      "LANGSMITH_API_KEY": LS_KEY}, "INVALID_REQUEST"),               # an environment key is refused in the pilot
    ({"SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "synthetic", "SETUHAUL_LANGSMITH_TRACING": "on"}, "MISSING_SECRET"),
    ({"SETUHAUL_ENV": "dev", "SETUHAUL_DATA_CLASS": "real", "SETUHAUL_LANGSMITH_TRACING": "on",
      "LANGSMITH_API_KEY": LS_KEY}, "INVALID_REQUEST"),               # synthetic data only
    ({"SETUHAUL_ENV": "dev", "SETUHAUL_DATA_CLASS": "synthetic", "SETUHAUL_LANGSMITH_TRACING": "on",
      "LANGSMITH_API_KEY": LS_KEY, "SETUHAUL_LANGSMITH_ENDPOINT": "https://evil.example.com"}, "INVALID_REQUEST"),
])
def test_tracing_configuration_problems_leave_tracing_off_and_the_agent_running(env, why, caplog):
    caplog.set_level(logging.WARNING)
    t = tracer_from_env(env)
    assert not t.enabled and why in caplog.text and LS_KEY not in caplog.text


def test_tracing_turns_on_with_a_key_from_secrets_manager_and_separate_projects():
    env = {"SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "synthetic", "SETUHAUL_LANGSMITH_TRACING": "on",
           "SETUHAUL_LANGSMITH_SECRET_ID": "setuhaul/pilot/langsmith"}
    t = tracer_from_env(env, secrets_client=Secrets(json.dumps({"api_key": LS_KEY})))
    assert t.enabled and t.config.project == "setuhaul-synthetic-pilot" and not t.config.content_allowed
    with pytest.raises(LangSmithConfigError):
        project_from_env({"SETUHAUL_LANGSMITH_PROJECT": "setuhaul-dev"}, "pilot")
    with pytest.raises(LangSmithConfigError):
        project_from_env({"SETUHAUL_LANGSMITH_PROJECT": "setuhaul-synthetic-pilot"}, "dev")
    with pytest.raises(LangSmithConfigError) as e:                     # the CDK placeholder {"api_key": ""}
        load_langsmith_key(env, "pilot", Secrets(json.dumps({"api_key": ""})))
    assert e.value.category == "EMPTY_SECRET"


def test_the_langsmith_client_sends_the_key_only_as_a_header_and_never_echoes_it():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(500, json={"detail": f"bad key {LS_KEY}"})
    c = LangSmithClient(LS_KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError) as e:
        c.post_runs([{"id": "x"}])
    assert LS_KEY not in str(e.value) and seen[0].headers["x-api-key"] == LS_KEY
    assert seen[0].url.path == "/runs/batch" and LS_KEY not in seen[0].content.decode()


def test_trace_links_are_built_only_from_a_configured_project_url():
    tid = str(uuid.uuid4())
    assert tracing.trace_url(None, tid) is None and tracing.trace_url("https://smith.langchain.com/o/1/projects/p/2", "x") is None
    assert tracing.trace_url("https://smith.langchain.com/o/1/projects/p/2/", tid) == \
        f"https://smith.langchain.com/o/1/projects/p/2/r/{tid}?trace_id={tid}"


# ================================================================ G1: a real turn, traced end to end
@pytest.fixture
def traced():
    cap = Capture()
    old = tracing.tracer()
    tracing.install(_tracer(cap))
    yield cap
    tracing.install(old)


def test_a_turn_is_traced_with_hierarchy_correlation_and_no_personal_data(app, client, dsn, traced):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    ev = d.say(f"Traffic. Reaching around {in_90()}. Any slot? My number is {phone}")
    corr = ev["correlation_id"]
    assert corr and uuid.UUID(corr)                                    # the webhook request's id rides the event
    r = Orchestrator(app.state.service, app.state.sessions, FakeModel(extractions=[exd(
        delay_kind="ARRIVAL_TIME", declared_eta_local=in_90(), exception_type="TRAFFIC")])).handle(ev)
    tracing.tracer().flush(2)
    runs = [x for b in traced.batches for x in b]
    by = {x["name"]: x for x in runs}
    assert {"agent_turn", "intent_extraction", "model_call", "parameter_check", "decide", "get_driver_context",
            "record_driver_report", "find_feasible_options", "final_response"} <= set(by)
    root = by["agent_turn"]
    meta = root["extra"]["metadata"]
    assert root["id"] == r.turn_ref == r.correlation_id == meta["correlation_id"]   # ONE id per turn (Phase G)
    assert meta["ingest_correlation_id"] == corr == r.ingest_correlation_id        # joins the API's request log
    gen = int(ev.get("generation", 0))
    assert r.turn_ref == str(uuid.uuid5(TURN_NS, f"{corr}:{int(ev['message_id'])}:{gen}"))  # one id per delivery
    assert all(x["trace_id"] == r.turn_ref for x in runs)
    assert by["model_call"]["parent_run_id"] == by["intent_extraction"]["id"]
    assert by["find_feasible_options"]["parent_run_id"] == by["decide"]["id"]
    blob = json.dumps(runs)
    assert phone not in blob and phone[1:] not in blob and "Traffic. Reaching" not in blob   # no text, no phone
    with psycopg.connect(dsn) as c:                                    # metrics row keyed by the same turn_ref
        assert c.execute("SELECT count(*) FROM setuhaul.agent_turn_metrics WHERE turn_ref=%s",
                         (r.turn_ref,)).fetchone()[0] == 1


def test_langsmith_down_changes_nothing_about_the_turn(app, client, dsn):
    old = tracing.tracer()
    try:
        for exporter in (Capture(), Capture(fail=True)):
            tracing.install(_tracer(exporter))
            _, phone, _ = add_driver(dsn)
            d = Driver(client, phone)
            model = FakeModel(extractions=[exd(delay_kind="ARRIVAL_TIME", declared_eta_local=in_90(),
                                               exception_type="TRAFFIC")])
            r = Orchestrator(app.state.service, app.state.sessions, model).handle(d.say(f"Reaching {in_90()}"))
            stats = tracing.tracer().flush(1)
            assert r.reply.startswith("Available slots") and len(model.calls) == 1   # same answer, one model call
            assert [t for t, _ in r.tools].count("find_feasible_options") == 1     # no tool ran twice
            assert stats["export_errors"] == (2 if exporter.fail else 0)
    finally:
        tracing.install(old)


def test_a_bug_inside_tracing_never_reaches_the_turn(app, client, dsn, monkeypatch):
    old = tracing.tracer()
    tracing.install(_tracer(Capture()))
    monkeypatch.setattr(tracing.Run, "end", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tracing bug")))
    try:
        _, phone, _ = add_driver(dsn)
        d = Driver(client, phone)
        r = Orchestrator(app.state.service, app.state.sessions, FakeModel(extractions=[exd(
            delay_kind="ARRIVAL_TIME", declared_eta_local=in_90(), exception_type="TRAFFIC")])).handle(d.say("late"))
        assert r.reply.startswith("Available slots") and tracing.tracer().stats["tracing_errors"] > 0
    finally:
        tracing.install(old)


def test_a_never_started_model_is_recorded_as_its_category(app, client, dsn):
    def factory():
        raise ModelError("the secret holds no OpenRouter key", "EMPTY_SECRET", False)
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    r = Orchestrator(app.state.service, app.state.sessions, DeferredModel(factory).prime()).handle(
        d.say(f"Traffic. Reaching around {in_90()}. Any slot?"))
    assert (r.extractor, r.fallback_reason, r.fallback_detail) == ("rules-fallback", "EMPTY_SECRET", "not_started")
    assert r.reply.startswith("Available slots")


# ================================================================ G3: budget accounting on the ledger
def _ledger(dsn, sql, *a):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, a).fetchall()


def test_refusals_errors_and_estimates_are_kept_apart_and_reconcile(dsn):
    from setuhaul.agent.metering import ModelBudget
    from setuhaul.api.db import Db
    with psycopg.connect(dsn, autocommit=True) as c:
        before =c.execute("SELECT spent_usd, reserved_usd FROM setuhaul.model_budget_day "
                           "WHERE day=(clock_timestamp() AT TIME ZONE 'UTC')::date").fetchone() or (Decimal(0), Decimal(0))
    b = ModelBudget(Db(dsn), "ci", Decimal(before[0]) + Decimal(before[1]) + Decimal("0.010"))
    ok = b.reserve(uuid.uuid4(), 1, "extract", "m", "amazon-bedrock", Decimal("0.004"))
    b.settle(ok, ok=False, sent=True, cost_usd=0, error="PROVIDER_AUTH_ERROR")     # a 401: charged 0, released
    est = b.reserve(uuid.uuid4(), 1, "extract", "m", "amazon-bedrock", Decimal("0.004"))
    b.settle(est, ok=False, sent=True, cost_usd=None, error="REQUEST_TIMEOUT")     # unknown: reservation kept = estimate
    refused = b.reserve(uuid.uuid4(), 1, "extract", "m", "amazon-bedrock", Decimal("0.009"))
    assert (ok.status, est.status, refused.status) == ("RESERVED", "RESERVED", "BUDGET_EXHAUSTED")
    rows = {r[0]: r[1:] for r in _ledger(dsn, """SELECT request_id, status, cost_usd, cost_source, input_tokens
                                                  FROM setuhaul.model_usage WHERE request_id = ANY(%s)""",
                                         [ok.request_id, est.request_id, refused.request_id])}
    assert rows[ok.request_id] == ("ERROR", Decimal("0"), "provider", None)            # no fabricated tokens
    assert rows[est.request_id] == ("ERROR", Decimal("0.004"), "reservation", None)   # labelled an estimate
    assert rows[refused.request_id] == ("BUDGET_EXHAUSTED", Decimal("0"), "not_sent", None)
    spent, reserved = _ledger(dsn, "SELECT spent_usd, reserved_usd FROM setuhaul.model_budget_day "
                                   "WHERE day=(clock_timestamp() AT TIME ZONE 'UTC')::date")[0]
    assert reserved == before[1] and spent == before[0] + Decimal("0.004")             # every reservation released
    with psycopg.connect(dsn) as c:
        c.execute("SET ROLE setuhaul_console")
        d = c.execute("SELECT setuhaul.observability_model_detail(now() - interval '1 hour')").fetchone()[0]
    assert d["attempts"] == d["ok"] + d["errors"] + d["budget_refusals"] + d["unsettled"]
    assert Decimal(d["cost_estimated_usd"]) >= Decimal("0.004") and d["by_cost_source"]["not_sent"] >= 1
    assert d["budget_day_boundary"].startswith("00:00 UTC")


def test_the_new_read_only_function_is_for_console_and_system_only(dsn):
    for role, allowed in (("setuhaul_console", True), ("setuhaul_system", True), ("setuhaul_agent", False),
                          ("setuhaul_ingest", False), ("setuhaul_feasibility", False)):
        with psycopg.connect(dsn) as c:
            assert c.execute("SELECT has_function_privilege(%s, 'setuhaul.observability_model_detail(timestamptz)', "
                             "'EXECUTE')", (role,)).fetchone()[0] is allowed, role
    with psycopg.connect(dsn) as c:
        prop = c.execute("""SELECT p.provolatile, p.prosecdef, r.rolname FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
                            WHERE p.proname='observability_model_detail'""").fetchone()
    assert prop == ("s", True, "setuhaul_rpc")                          # STABLE (reads only), definer, NOBYPASSRLS owner


# ================================================================ G4: the rules reader
CASES = [json.loads(x) for x in (ROOT / "evals/langsmith/rules_reader_cases.jsonl").read_text(encoding="utf-8").splitlines()
         if x.strip()]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_rules_reader_regression_case(case):
    from evals.langsmith.evaluators import rules_case_checks
    out = rules_extractor.extract(case["say"]).model_dump()
    checks = rules_case_checks(case["expect"], out)
    assert checks and all(checks.values()), (case["say"], {k: v for k, v in checks.items() if not v}, out)


def test_i14_is_a_17_00_completion_deadline_in_the_facility_time_zone():
    ex = rules_extractor.extract("Need to finish unloading by 5 pm")
    assert (ex.latest_finish_local, ex.latest_finish_kind, ex.declared_eta_local, ex.earliest_start_local) == \
        ("17:00", "UNLOAD_END", None, None)
    now = datetime(2026, 11, 23, 4, 0, tzinfo=timezone.utc)            # 09:30 in Jaipur, 15:00 in Sydney
    ts = local_to_ts(ex.latest_finish_local, now)
    assert ts.utcoffset() == timedelta(hours=5, minutes=30) and (ts.hour, ts.minute) == (17, 0)
    assert FACILITY_TZ_NAME == "Asia/Kolkata" and FACILITY_TZ.utcoffset(None) == timedelta(hours=5, minutes=30)
    with pytest.raises(ValueError):
        local_to_ts("17:00", datetime(2026, 11, 23, 4, 0))                # a naive "now" is never the server's clock


def test_the_facility_zone_matches_the_iana_zone():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        z = zoneinfo.ZoneInfo(FACILITY_TZ_NAME)
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no IANA tz database on this machine")
    for month in (1, 7):
        assert datetime(2026, month, 1, tzinfo=z).utcoffset() == FACILITY_TZ.utcoffset(None)


def test_loading_counts_as_dock_work_but_start_and_finish_are_still_told_apart():
    assert kind_stated("UNLOAD_END", "Must finish loading by 17:00")
    assert not kind_stated("UNLOAD_END", "loading starts at 5")
    assert not kind_stated("UNLOAD_END", "We need to be done by 4 pm")   # D1: ambiguous, the agent asks


def test_english_behaviour_is_preserved_on_the_phase_g_intent_rows():
    rows = [json.loads(x) for x in (ROOT / "evals/phase_g/dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    for r in (x for x in rows if x["input"]["kind"] == "intent"):
        out = rules_extractor.extract(r["input"]["say"]).model_dump()
        want = r["expected"]["intent"] if isinstance(r["expected"]["intent"], list) else [r["expected"]["intent"]]
        assert out["intent"] in want, (r["id"], out["intent"])
        for k, v in r["expected"].get("params", {}).items():
            assert out[k] == v, (r["id"], k, out[k])


# ================================================================ G5/G6: console
STATIC = ROOT / "setuhaul" / "api" / "console_static"


def test_the_console_has_no_hardcoded_booking_state_or_fake_shipment_and_no_langsmith_credential():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "assets" / "app.js").read_text(encoding="utf-8")
    assert "booking execution OFF</span>" not in html and 'id="bookingpill"' in html and 'id="killpill"' in html
    assert "SHP-SYN-0001" not in html + js and "SYN-SHP-XXXXXXXX" in js
    assert "status unavailable" in js
    assert not re.search(r"pill\([^)]*production", js, re.I)             # no pill ever says production
    assert not re.search(r"x-api-key|lsv2_|LANGSMITH_API_KEY|api\.smith\.langchain\.com", js)


def test_the_console_validates_the_same_shipment_format_as_the_server(client, dsn, keys):
    from setuhaul.api.app import SHIPMENT_ID_PATTERN
    js = (STATIC / "assets" / "app.js").read_text(encoding="utf-8")
    assert f'const SHIPMENT_ID_PATTERN = "{SHIPMENT_ID_PATTERN}";' in js
    for good in ("SYN-SHP-2172ACB8", "SYN-SHP1001", "NOPE", "SHP-99399e2f-0"):   # ids are case-sensitive: both cases
        assert re.fullmatch(SHIPMENT_ID_PATTERN, good)
    for bad in ("SY", "-SYN-SHP-1", "SYN SHP 1", "SYN.SHP.1", "SYN-SHP-1;DROP", "S" * 81):
        assert not re.fullmatch(SHIPMENT_ID_PATTERN, bad)
    for bad in ("SY", "SYN.SHP.1", "-SYN-SHP-1", "S" * 81):
        r = client.get(f"/console/shipments/{bad}/recommendations", headers=H(staff_token(keys)))
        assert r.status_code == 422, bad                                 # refused before any database read


def test_observability_splits_model_outcomes_and_links_for_planner_and_ops_lead_only(client, dsn, keys, monkeypatch):
    monkeypatch.setenv("SETUHAUL_LANGSMITH_TRACING", "on")
    monkeypatch.setenv("SETUHAUL_LANGSMITH_UI_URL", "https://smith.langchain.com/o/org/projects/p/proj")
    for role in ("planner", "ops_lead"):
        b = client.get("/console/observability", headers=H(staff_token(keys, (role,)))).json()
        mo = b["agent"]["model_outcomes"]
        assert mo["turns"]["reconciles"] is True and mo["attempts"]["reconciles"] is True
        assert set(mo["turns"]["fallback_by_class"]) == {"PROVIDER_FAILURE", "BUDGET_REFUSAL", "NOT_STARTED"}
        assert "estimated_usd" in mo["cost"] and mo["budget"]["day_boundary"].startswith("00:00 UTC")
        ls = b["langsmith"]
        assert ls["project_url"].startswith("https://smith.langchain.com/") and ls["tracing"] == "on"
        assert all(t["trace_url"] is None or t["trace_url"].startswith(ls["project_url"]) for t in b["agent"]["recent_turns"])
        assert all(t["facility_id"] in (JAI, None) for t in b["agent"]["recent_turns"])     # facility scope (RLS)
        assert "x-api-key" not in json.dumps(b) and "lsv2_" not in json.dumps(b)
    assert client.get("/console/observability", headers=H(staff_token(keys, ("operations",)))).status_code == 403


def test_a_link_that_is_not_configured_is_reported_unavailable(client, keys, monkeypatch):
    monkeypatch.delenv("SETUHAUL_LANGSMITH_UI_URL", raising=False)
    monkeypatch.setenv("SETUHAUL_LANGSMITH_UI_URL", "https://evil.example.com/phish")
    ls = client.get("/console/observability", headers=H(staff_token(keys, ("ops_lead",)))).json()["langsmith"]
    assert ls["project_url"] is None and ls["project_status"].startswith("unavailable")


def test_health_reports_booking_and_the_kill_switch_separately(client):
    h = client.get("/health").json()
    assert isinstance(h["booking_enabled"], bool) and "environment" in h
    assert "booking_kill_switch" in h if h["booking_enabled"] else True


# ================================================================ Lambda: degrade, never crash, flush never raises
def test_the_agent_lambda_flush_never_raises_and_a_down_exporter_is_only_logged(caplog, monkeypatch):
    from setuhaul.aws import lambdas
    old = tracing.tracer()
    try:
        tracing.install(_tracer(Capture(fail=True)))
        with tracing.tracer().turn(str(uuid.uuid4())):
            pass
        caplog.set_level(logging.INFO)
        lambdas._flush_traces()
        assert "trace export" in caplog.text
        monkeypatch.setattr(tracing.Tracer, "flush", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        lambdas._flush_traces()                                         # still no exception
    finally:
        tracing.install(old)


# ================================================================ G2: datasets and evaluation
def test_the_dataset_keeps_all_67_scenarios_in_15_categories_with_a_content_version():
    from evals.langsmith import dataset as ds
    ex, man = ds.build()
    assert man["phase_g_scenarios"] == 67 and len(ds.CATEGORIES) == 15
    assert all(man["by_category"][c] >= 1 for c in ds.CATEGORIES)
    assert {e["id"] for e in ex} >= {"I14", "I15"} and all(e["assertions"] for e in ex)
    assert man["dataset_version"] == ds.build()[1]["dataset_version"]  # deterministic
    for e in ds.langsmith_examples(ex, man["dataset_version"]):
        assert set(e) == {"inputs", "outputs", "metadata"} and e["metadata"]["category"] in ds.CATEGORIES
        assert e["metadata"]["synthetic"] is True


def test_the_runner_refuses_the_pilot_and_sync_refuses_pilot_environments(monkeypatch):
    from evals.langsmith import run
    with pytest.raises(SystemExit):
        run.guard_local_only({"SETUHAUL_DSN": "rds-data"})
    with pytest.raises(SystemExit):
        run.guard_local_only({"SETUHAUL_ENV": "pilot"})
    monkeypatch.setenv("SETUHAUL_ENV", "pilot")
    with pytest.raises(SystemExit):
        run.sync_to_langsmith([], {"dataset_name": "x", "dataset_version": "v"}, {"examples": []}, False)


def test_dataset_and_experiment_sync_use_the_rest_api_once_per_version(monkeypatch):
    from evals.langsmith import dataset as ds, run
    ex, man = ds.build()
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": str(uuid.uuid4())})
    monkeypatch.setenv("SETUHAUL_ENV", "dev")
    monkeypatch.setenv("LANGSMITH_API_KEY", LS_KEY)
    report = {"examples": [{"id": "I01", "category": "intent", "result": "PASS",
                            "scores": {"intent": {"score": 1.0, "comment": "ok"}}}]}
    out = run.sync_to_langsmith(ex, man, report, True, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["dataset_created"] and out["dataset_version"] == man["dataset_version"] and out["runs"] == 1
    assert [p for _, p in calls] == ["/datasets", "/datasets", "/examples/bulk", "/sessions", "/runs/batch", "/feedback"]


def test_the_llm_judge_is_off_and_never_authoritative():
    from evals.langsmith import llm_judge
    from evals.langsmith.evaluators import EVALUATORS
    with pytest.raises(llm_judge.JudgeDisabled):
        llm_judge.judge({}, "reply", {"SETUHAUL_ENV": "pilot"})
    assert not set(llm_judge.QUALITY_KEYS) & set(EVALUATORS) and set(llm_judge.NEVER_JUDGED) <= set(EVALUATORS)


@pytest.mark.parametrize("turns,fallbacks,model,code", [
    ({"turns": 0, "model_turns": 0, "rules_turns": 0, "fallbacks": 0}, {}, {"calls": 0}, "NO_ACTIVITY"),
    ({"turns": 3, "model_turns": 0, "rules_turns": 3, "fallbacks": 3}, {"EMPTY_SECRET": 3}, {"calls": 0}, "NOT_STARTED"),
    # the pilot's real 23 Nov window, as v0.3.0 wrote it: 27 'auth' errors, 8 refusals, 0 OK
    ({"turns": 35, "model_turns": 0, "rules_turns": 35, "fallbacks": 35}, {"auth": 27, "budget_exhausted": 8},
     {"calls": 35, "ok": 0, "errors": 27, "budget_refusals": 8, "unsettled": 0,
      "by_error": {"auth": 27, "budget_exhausted": 8}}, "PROVIDER_FAILING"),
    ({"turns": 2, "model_turns": 0, "rules_turns": 2, "fallbacks": 2}, {"budget_exhausted": 2},
     {"calls": 2, "ok": 0, "errors": 0, "budget_refusals": 2, "unsettled": 0}, "BUDGET_REFUSED"),
    ({"turns": 3, "model_turns": 2, "rules_turns": 1, "fallbacks": 1}, {"PROVIDER_RATE_LIMIT": 1},
     {"calls": 3, "ok": 2, "errors": 1, "budget_refusals": 0, "unsettled": 0}, "DEGRADED"),
    ({"turns": 2, "model_turns": 2, "rules_turns": 0, "fallbacks": 0}, {},
     {"calls": 2, "ok": 2, "errors": 0, "budget_refusals": 0, "unsettled": 0}, "OK"),
])
def test_model_outcome_status_and_reconciliation(turns, fallbacks, model, code):
    from setuhaul.api.service import BookingService
    out = BookingService._model_outcomes(turns, fallbacks, model, None)
    assert out["status_code"] == code and out["turns"]["reconciles"] and out["attempts"]["reconciles"]
    assert all(k in E.CATEGORIES for k in out["turns"]["fallback_by_category"])
    assert all(k in E.CATEGORIES for k in out["attempts"]["by_category"])
    assert "migration 0028 not applied" in out["cost"]["note"]          # without 0028 the split is not guessed
