"""Full Activation (synthetic pilot): independent feature controls, live-Telegram guards and kill switches, the advisory
online LLM judge (budget, sampling, idempotency, failure isolation), LangSmith coverage of Telegram and the judge.

NO PAID CALL and NO NETWORK: every model is a fake, every Telegram API is an httpx.MockTransport, every secret is a
fake Secrets Manager client. The database tests use a private fresh database migrated through 0043.

Existing suites that cover the rest of the Telegram requirements (run alongside this file):
  test_dispatch_offers.py      webhook secret / payload hygiene, duplicate webhooks, unlinked accounts, one driver cannot
                               act on another driver's offer, link codes single-use and expiring
  test_location_telegram_units.py, test_reassign_cancel_telegram_out.py   parsing, outbound routing, relay outcomes
  test_telegram_test_bot.py    the local test bot stays local/dev and allowlisted
  test_driver_traceability.py  driver ids in traces, two drivers stay apart
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from conftest import BASE_DSN, _pg_available
from setuhaul.agent import judge_core as JC
from setuhaul.agent import online_judge as OJ
from setuhaul.notify import telegram as tg
from setuhaul.notify.outbound import ACCEPTED, AMBIGUOUS, PERMANENT, RETRYABLE
from setuhaul.observability.tracing import Exporter, Tracer, TracerConfig
from setuhaul.ops.features import resolve
from test_api import keys  # noqa: F401  (the JWKS fixture for the app test)

TOKEN = "123456789:" + "A" * 35
WEBHOOK = "w" * 40
PILOT = {"SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "synthetic"}


class Sm:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        if self.value is None:
            raise RuntimeError("AccessDenied " + TOKEN)
        return {"SecretString": self.value}


# ================================================================ feature controls
def test_the_pilot_resolves_every_control_on_after_authorized_activation():
    f = resolve({**PILOT, "SETUHAUL_BOOKING_ENABLED": "true", "SETUHAUL_TELEGRAM_DELIVERY": "live",
                 "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true", "SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID": "arn:x",
                 "SETUHAUL_LLM_JUDGE_ENABLED": "true", "SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD": "0.25",
                 "SETUHAUL_LANGSMITH_TRACING": "on"})
    assert (f.booking_enabled, f.telegram_delivery_mode, f.telegram_live_authorized, f.telegram_outbound_enabled,
            f.telegram_webhook_enabled, f.llm_judge_enabled, f.langsmith_tracing_enabled) == (
        True, "live", True, True, True, True, True)
    assert not f.production and f.notes == ()


@pytest.mark.parametrize("env", [{"SETUHAUL_ENV": "production", "SETUHAUL_DATA_CLASS": "synthetic"},
                                 {"SETUHAUL_ENV": "prod"}, {"SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "real"}])
def test_production_resolves_booking_live_telegram_and_the_judge_off_whatever_the_variables_say(env):
    f = resolve({**env, "SETUHAUL_BOOKING_ENABLED": "true", "SETUHAUL_TELEGRAM_DELIVERY": "live",
                 "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true", "SETUHAUL_LLM_JUDGE_ENABLED": "true",
                 "SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD": "0.5"})
    assert f.production and not f.booking_enabled and f.telegram_delivery_mode == "refused"
    assert not f.telegram_live_authorized and not f.llm_judge_enabled


def test_no_single_flag_turns_on_another_capability():
    for k, v in (("SETUHAUL_BOOKING_ENABLED", "true"), ("SETUHAUL_TELEGRAM_LIVE_AUTHORIZED", "true"),
                 ("SETUHAUL_LLM_JUDGE_ENABLED", "true"), ("SETUHAUL_LANGSMITH_TRACING", "on")):
        f = resolve({**PILOT, k: v})
        on = {n for n in ("booking_enabled", "llm_judge_enabled", "langsmith_tracing_enabled",
                          "telegram_webhook_enabled") if getattr(f, n)}
        assert len(on) <= 1 and f.telegram_delivery_mode == "test_sink", (k, on)


def test_the_judge_flag_without_an_approved_budget_resolves_off_with_the_reason():
    f = resolve({**PILOT, "SETUHAUL_LLM_JUDGE_ENABLED": "true", "SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD": "0"})
    assert not f.llm_judge_enabled and any("no approved daily budget" in n for n in f.notes)


# ================================================================ Telegram: live guards, kill switches, secrets
def test_live_telegram_is_refused_outside_the_pilot_and_without_authorization():
    live = {"SETUHAUL_TELEGRAM_DELIVERY": "live", "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true",
            "SETUHAUL_TELEGRAM_BOT_SECRET_ID": "setuhaul/pilot/telegram/bot"}
    for env in ({"SETUHAUL_ENV": "production", "SETUHAUL_DATA_CLASS": "synthetic"}, {"SETUHAUL_ENV": "staging"},
                {"SETUHAUL_ENV": "dev"}):
        with pytest.raises(RuntimeError, match="SETUHAUL_ENV in"):
            tg.telegram_from_env({**env, **live}, Sm(TOKEN))
    with pytest.raises(RuntimeError, match="DATA_CLASS"):
        tg.telegram_from_env({"SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "real", **live}, Sm(TOKEN))
    with pytest.raises(RuntimeError, match="LIVE_AUTHORIZED"):
        tg.telegram_from_env({**PILOT, **live, "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "false"}, Sm(TOKEN))
    t, allow = tg.telegram_from_env({**PILOT, **live}, Sm(json.dumps({"token": TOKEN})))
    assert isinstance(t, tg.TelegramBotProvider) and allow is None


def test_the_outbound_kill_switch_sends_nothing_and_fails_visibly():
    sm = Sm(TOKEN)
    t, _ = tg.telegram_from_env({**PILOT, "SETUHAUL_TELEGRAM_DELIVERY": "live", "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true",
                                 "SETUHAUL_TELEGRAM_BOT_SECRET_ID": "x", "SETUHAUL_TELEGRAM_OUTBOUND_ENABLED": "false"}, sm)
    assert isinstance(t, tg.PausedTelegram)
    r = t.send("1234567890", "hello", "k1")
    assert r.outcome == PERMANENT and r.error == "TELEGRAM_OUTBOUND_DISABLED"     # relay: FAILED + escalation
    t.clear_buttons("1234567890", 5)
    t.answer_callback("cb")                                                           # no network, no exception
    sink, _ = tg.telegram_from_env({"SETUHAUL_TELEGRAM_OUTBOUND_ENABLED": "false"})
    assert isinstance(sink, tg.PausedTelegram)
    assert tg.telegram_from_env({"SETUHAUL_TELEGRAM_DELIVERY": "off", "SETUHAUL_TELEGRAM_OUTBOUND_ENABLED": "false"}) == (None, None)


def test_the_webhook_secret_comes_from_secrets_manager_and_is_validated_without_leaking():
    assert tg.webhook_secret_from_env({**PILOT, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID": "arn:w"},
                                      Sm(json.dumps({"secret": WEBHOOK}))) == WEBHOOK
    assert tg.webhook_secret_from_env({**PILOT, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID": "arn:w"}, Sm(WEBHOOK)) == WEBHOOK
    with pytest.raises(RuntimeError, match="not accepted here"):
        tg.webhook_secret_from_env({**PILOT, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": WEBHOOK})
    with pytest.raises(RuntimeError, match="32-256"):
        tg.webhook_secret_from_env({**PILOT, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID": "arn:w"}, Sm("short"))
    with pytest.raises(RuntimeError, match="unreadable") as e:
        tg.webhook_secret_from_env({**PILOT, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID": "arn:w"}, Sm(None))
    assert TOKEN not in str(e.value)
    sm = Sm(WEBHOOK)
    assert tg.webhook_secret_from_env({**PILOT, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID": "arn:w",
                                       "SETUHAUL_TELEGRAM_WEBHOOK_ENABLED": "false"}, sm) is None and sm.calls == 0
    assert tg.webhook_secret_from_env({"SETUHAUL_ENV": "local", "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": WEBHOOK}) == WEBHOOK
    assert tg.webhook_secret_from_env({}) is None


def _bot(status, body=None, headers=None):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, json=body if body is not None else {}, headers=headers or {})
    return tg.TelegramBotProvider(TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))), seen


@pytest.mark.parametrize("status,body,outcome", [
    (200, {"ok": True, "result": {"message_id": 77}}, ACCEPTED), (429, {"ok": False}, RETRYABLE),
    (500, {}, AMBIGUOUS), (403, {"ok": False}, PERMANENT), (400, {}, PERMANENT), (200, {"ok": True}, AMBIGUOUS)])
def test_outbound_delivery_outcomes_map_to_the_relays_retry_rules(status, body, outcome):
    bot, seen = _bot(status, body)
    r = bot.send("1234567890", "Your slot is D1 at 1:00 PM", "notif-1")
    assert r.outcome == outcome and len(seen) == 1                   # one request per attempt: retries are the relay's
    if outcome == ACCEPTED:
        assert r.provider_message_id == tg.provider_ref("1234567890", 77)
    assert TOKEN not in (r.error or "")


def test_the_api_stays_up_without_the_webhook_when_live_telegram_cannot_start(monkeypatch, dsn, keys):
    """Telegram fails closed ON TELEGRAM ONLY: the console and health still answer; no webhook route."""
    from fastapi.testclient import TestClient
    from setuhaul.api import app as app_mod
    for k, v in {**PILOT, "SETUHAUL_TELEGRAM_DELIVERY": "live", "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true",
                 "SETUHAUL_TELEGRAM_BOT_SECRET_ID": ""}.items():
        monkeypatch.setenv(k, v)
    out = app_mod._telegram_from_env()
    assert out == {"telegram_mode": "live_unavailable"}
    from test_api import CLIENT, GGN, ISSUER, JAI, META_SECRET, SESSION_SECRET
    c = TestClient(app_mod.create_app(app_mod.Settings(
        dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
        meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"], **out)))
    assert c.post("/webhooks/telegram", content=b"{}").status_code == 404
    h = c.get("/health").json()
    assert h["telegram"] == "live_unavailable" and h["features"]["telegram_webhook_enabled"] is False
    assert TOKEN not in json.dumps(h)


def test_the_webhook_route_is_absent_when_its_kill_switch_is_off(monkeypatch):
    from setuhaul.api import app as app_mod
    monkeypatch.setenv("SETUHAUL_TELEGRAM_WEBHOOK_SECRET", WEBHOOK)
    monkeypatch.setenv("SETUHAUL_ENV", "dev")
    assert app_mod._telegram_from_env()["telegram_webhook_secret"] == WEBHOOK
    monkeypatch.setenv("SETUHAUL_TELEGRAM_WEBHOOK_ENABLED", "false")
    assert app_mod._telegram_from_env()["telegram_webhook_secret"] is None


# ================================================================ tracing helpers
class Capture(Exporter):
    def __init__(self, fail=False):
        self.batches, self.fail, self.client = [], fail, None

    def post(self, batch):
        if self.fail:
            raise httpx.ConnectError("down")
        self.batches.append(batch)


def tracer(fail=False) -> Tracer:
    return Tracer(TracerConfig(enabled=True, environment="pilot", project="setuhaul-synthetic-pilot",
                               agent_version="release-abc", release_commit="96a49ec"), Capture(fail))


def test_outbound_sends_are_traced_with_a_chat_tag_never_the_chat_id_text_or_token():
    t = tracer()
    bot, _ = _bot(200, {"ok": True, "result": {"message_id": 9}})
    r = tg.TracedTelegram(bot, t).send("5550001234", "Reply 1 to accept D1", "notif-42")
    assert r.outcome == ACCEPTED
    t.flush(1)
    (run,), = t.exporter.batches
    blob = json.dumps(run)
    assert run["name"] == "telegram_outbound" and "5550001234" not in blob and "Reply 1" not in blob and TOKEN not in blob
    meta = run["extra"]["metadata"]
    assert meta["chat_tag"] == tg.chat_tag("5550001234") and meta["notification_ref"] == "notif-42"
    assert meta["outcome"] == ACCEPTED and meta["environment"] == "pilot" and meta["release_commit"] == "96a49ec"


def test_a_tracing_outage_never_breaks_a_send():
    t = tracer(fail=True)
    bot, seen = _bot(200, {"ok": True, "result": {"message_id": 9}})
    assert tg.TracedTelegram(bot, t).send("5550001234", "x", "k").outcome == ACCEPTED and len(seen) == 1
    stats = t.flush(0.5)
    assert stats["export_errors"] >= 1 and stats["exported"] == 0

    class Broken:
        enabled = True

        def turn(self, *a, **k):
            raise RuntimeError("tracer bug")
    bot2, seen2 = _bot(200, {"ok": True, "result": {"message_id": 9}})
    assert tg.TracedTelegram(bot2, Broken()).send("5550001234", "x", "k").outcome == ACCEPTED and len(seen2) == 1


# ================================================================ online judge: configuration (no database)
def _judge_env(**kw):
    return {**PILOT, "SETUHAUL_LLM_JUDGE_ENABLED": "true", "SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD": "0.20",
            "SETUHAUL_LLM_DAILY_BUDGET_USD": "0.50", "SETUHAUL_LLM_JUDGE_SAMPLE_RATE": "1", **kw}


@pytest.mark.parametrize("change,reason", [
    ({"SETUHAUL_LLM_JUDGE_ENABLED": "false"}, "llm_judge"),
    ({"SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD": "0"}, "no approved daily budget"),
    ({"SETUHAUL_ENV": "production"}, "allowed only in"),
    ({"SETUHAUL_DATA_CLASS": "real"}, "allowed only in"),
    ({"SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD": "1.5"}, "above the"),
    ({"SETUHAUL_LLM_JUDGE_RUN_CAP_USD": "0.5"}, "per-judgement cap"),
    ({"SETUHAUL_LLM_JUDGE_AGENT_RESERVE_USD": "0.5"}, "no room"),
    ({"SETUHAUL_LLM_JUDGE_SAMPLE_RATE": "0"}, "sample rate")])
def test_the_judge_is_off_unless_every_condition_holds(change, reason):
    cfg = OJ.config_from_env(_judge_env(**change))
    assert not cfg.enabled and reason in cfg.reason


def test_the_judge_config_defaults_are_bounded():
    cfg = OJ.config_from_env(_judge_env(SETUHAUL_LLM_JUDGE_TIMEOUT_S="999"))
    assert cfg.enabled and cfg.timeout_s == OJ.TIMEOUT_MAX_S and cfg.run_cap_usd == Decimal("0.01")
    assert cfg.agent_reserve_usd == Decimal("0.25")                   # half the shared cap is kept for the agent


def test_a_disabled_judge_builds_no_client_so_no_paid_call_is_possible(monkeypatch):
    import setuhaul.agent.openrouter as orm
    monkeypatch.setattr(orm, "load_openrouter_key", lambda *a, **k: pytest.fail("no key may be read"))
    j = OJ.online_judge_from_env({**PILOT, "SETUHAUL_LLM_JUDGE_ENABLED": "true"}, db=None)
    assert j.client is None
    out = j.after_turn(SimpleNamespace(turn_ref=str(uuid.uuid4()), reply="hello"), "hi", "en")
    assert out["status"] == "DISABLED" and "budget" in out["reason"]


def test_sampling_is_deterministic_per_turn_and_follows_the_rate():
    refs = [str(uuid.uuid4()) for _ in range(2000)]
    assert all(OJ.sampled(r, 0.3) == OJ.sampled(r, 0.3) for r in refs[:50])
    share = sum(OJ.sampled(r, 0.3) for r in refs) / len(refs)
    assert 0.25 < share < 0.35 and not any(OJ.sampled(r, 0.0) for r in refs[:50])


def test_the_judge_module_cannot_reach_booking_tools_or_the_service():
    """Advisory by construction: the judge imports no service, tool, booking, kill-switch or authorization code, and
    its only database writes are the ledger and record_turn_judgement."""
    import inspect
    import re
    src = inspect.getsource(OJ) + inspect.getsource(JC)
    imports = "\n".join(l for l in src.splitlines() if re.match(r"\s*(from|import)\s", l))
    for forbidden in (r"api\.(service|app|inbound|security)", r"\bservice\b", "orchestrator", "kill_switch", "booking",
                      "workflow", "notify", "domain"):
        assert not re.search(forbidden, imports), (forbidden, imports)
    assert re.findall(r"from \.\.api\.\w+ import [\w, ]+", imports) == ["from ..api.db import TURN_FENCE"]
    calls = set(re.findall(r"SELECT ([a-z_]+)\(", src))
    assert calls == {"pg_advisory_xact_lock", "judge_spent_today", "reserve_model_call", "record_turn_judgement"}, calls


def test_the_offline_and_online_judge_share_one_prompt_and_validator():
    from evals.langsmith import llm_judge as J
    assert J.JUDGE_SYSTEM is JC.JUDGE_SYSTEM and J.validate_judgement is JC.validate_judgement
    assert JC.JUDGE_PROMPT_VERSION == "judge-p2" and (JC.RATIONALE_MAX, JC.EVIDENCE_MAX) == (200, 120)


# ================================================================ online judge with the database (fresh, through 0043)
@pytest.fixture(scope="module")
def jdsn():
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    from setuhaul.db.migrate import migrate
    name = "setuhaul_build_fullact"
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {name}")
    d = f"{BASE_DSN} dbname={name}"
    migrate(d)
    return d


@pytest.fixture
def jdb(jdsn):
    from setuhaul.api.db import Db
    with psycopg.connect(jdsn, autocommit=True) as c:                  # each test starts with an empty day
        c.execute("DELETE FROM setuhaul.model_usage")
        c.execute("DELETE FROM setuhaul.model_budget_day")
        c.execute("TRUNCATE setuhaul.agent_turn_judgements")       # append-only: no DELETE
    return Db(jdsn)


REPLY = "Available slots: 1) D1 at 1:00 PM 2) D2 at 2:00 PM. Reply with the number you want."


def _answer(scores=(4, 5, 5), quote="Reply with the number you want"):
    return {d: {"score": s, "rationale": f"{d} judged from the reply.", "evidence": quote, "insufficient_evidence": False}
            for d, s in zip(JC.DIMENSIONS, scores)}


class FakeJudgeClient:
    """Behaves like OpenRouterModel.judge: reserves through .meter, then 'answers'. Never any network."""
    extract_model = "anthropic/claude-haiku-4.5"

    def __init__(self, answer=None, raise_=None, reserve_usd="0.004", cost="0.0031"):
        self.answer, self.raise_, self.meter, self.calls = answer or _answer(), raise_, None, 0
        self.reserve_usd, self.cost = Decimal(reserve_usd), cost

    def judge(self, system, user, tool):
        from setuhaul.agent.models import ModelError, Trace
        call_id = uuid.uuid4()
        r = self.meter.reserve(call_id, 1, "judge", self.extract_model, "amazon-bedrock", self.reserve_usd)
        if r.status != "RESERVED":
            raise ModelError(f"model call not sent: {r.status}", "BUDGET_EXCEEDED", False, detail=r.status.lower())
        self.calls += 1
        tr = Trace("judge", self.extract_model, "openrouter:amazon-bedrock")
        if self.raise_:
            self.meter.settle(r, ok=False, sent=True, error="timeout")
            raise self.raise_
        tr.input_tokens, tr.output_tokens, tr.cost_usd, tr.latency_ms = 900, 120, float(self.cost), 800
        tr.actual_provider = "Amazon Bedrock"
        self.meter.settle(r, ok=True, sent=True, actual_provider="Amazon Bedrock", input_tokens=900, output_tokens=120,
                          cost_usd=float(self.cost), latency_ms=800)
        return {"args": self.answer, "served_model": self.extract_model}, tr


def _turn(**kw):
    base = dict(turn_ref=str(uuid.uuid4()), reply=REPLY, correlation_id="corr-1", driver_id="DRV-JAI-0042",
                shipment_id="SHP-JAI-0007", facility_id="FAC-JAI-01")
    return SimpleNamespace(**{**base, **kw})


def _row(jdsn, ref):
    with psycopg.connect(jdsn) as c:
        c.row_factory = psycopg.rows.dict_row
        return c.execute("SELECT * FROM setuhaul.agent_turn_judgements WHERE turn_ref = %s", (ref,)).fetchone()


def _judge(jdb, client, tr=None, **env):
    return OJ.OnlineJudge(OJ.config_from_env(_judge_env(**env)), jdb, client, tr)


def test_a_scored_turn_is_persisted_advisory_and_correlated_to_the_turn_and_its_trace(jdb, jdsn):
    t = tracer()
    fb = []
    t.exporter.client = SimpleNamespace(create_feedback=lambda *a: fb.append(a))
    res = _turn()
    out = _judge(jdb, FakeJudgeClient(), t).after_turn(res, "mujhe slot chahiye", "en")
    assert out["status"] == "SCORED" and out["scores"] == {"clarity": 4, "tone": 5, "language_match": 5}
    row = _row(jdsn, res.turn_ref)
    assert row["advisory"] is True and row["calibration_status"] == "HUMAN_CALIBRATION_OUTSTANDING"
    assert (row["prompt_version"], row["eval_version"]) == ("judge-p2", "online-judge-v1")
    assert (row["driver_id"], row["shipment_id"], row["facility_id"], row["correlation_id"]) == (
        "DRV-JAI-0042", "SHP-JAI-0007", "FAC-JAI-01", "corr-1")
    assert row["model"] == "anthropic/claude-haiku-4.5" and row["provider"] == "Amazon Bedrock"
    assert row["cost_usd"] == Decimal("0.003100") and row["input_tokens"] == 900
    assert row["details"]["clarity"]["evidence"] == "Reply with the number you want"
    assert "mujhe" not in json.dumps(row, default=str)                # the driver's words are never stored
    t.flush(1)
    (run,), = t.exporter.batches
    assert run["name"] == "llm_judge" and str(row["trace_id"]) == run["id"]
    meta = run["extra"]["metadata"]
    assert meta["parent_trace_id"] == res.turn_ref and meta["advisory"] is True and meta["quality:clarity"] == 4
    assert "mujhe" not in json.dumps(run)
    assert {a[1] for a in fb} == {"quality:clarity", "quality:tone", "quality:language_match"} and fb[0][0] == res.turn_ref
    with psycopg.connect(jdsn) as c:
        assert c.execute("SELECT purpose, status, cost_source FROM setuhaul.model_usage").fetchall() == [
            ("judge", "OK", "provider")]


def test_a_redelivered_turn_is_never_paid_for_twice(jdb, jdsn):
    client, res = FakeJudgeClient(), _turn()
    j = _judge(jdb, client)
    assert j.after_turn(res, "hi", "en")["status"] == "SCORED"
    assert j.after_turn(res, "hi", "en")["status"] == "DUPLICATE" and client.calls == 1
    with psycopg.connect(jdsn) as c:
        assert c.execute("SELECT count(*) FROM setuhaul.model_usage").fetchone()[0] == 1


def test_the_judges_daily_cap_skips_and_records_the_reason_without_sending(jdb, jdsn):
    client = FakeJudgeClient(reserve_usd="0.009", cost="0.009")
    j = _judge(jdb, client, SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD="0.02")
    outs = [j.after_turn(_turn(), "hi", "en")["status"] for _ in range(4)]
    assert outs == ["SCORED", "SCORED", "SKIPPED", "SKIPPED"] and client.calls == 2
    with psycopg.connect(jdsn) as c:
        spent = c.execute("SELECT setuhaul.judge_spent_today()").fetchone()[0]
        reasons = [r[0] for r in c.execute("SELECT reason FROM setuhaul.agent_turn_judgements WHERE status='SKIPPED'")]
    assert spent <= Decimal("0.02") and reasons == ["budget:judge_daily_cap"] * 2


def test_the_agents_reserve_is_protected_from_the_judge(jdb, jdsn):
    with psycopg.connect(jdsn, autocommit=True) as c:                  # the agent already spent most of the shared cap
        c.execute("INSERT INTO setuhaul.model_budget_day (day, environment, spent_usd) "
                  "VALUES ((clock_timestamp() AT TIME ZONE 'UTC')::date, 'pilot', 0.245)")
    client = FakeJudgeClient(reserve_usd="0.009")
    out = _judge(jdb, client).after_turn(_turn(), "hi", "en")           # shared 0.50, agent reserve 0.25
    assert out == {"status": "SKIPPED", "reason": "budget:shared_daily_cap", "scores": {d: None for d in JC.DIMENSIONS}}
    assert client.calls == 0


def test_the_per_judgement_cap_refuses_an_expensive_call(jdb):
    client = FakeJudgeClient(reserve_usd="0.05")
    out = _judge(jdb, client).after_turn(_turn(), "hi", "en")
    assert out["status"] == "SKIPPED" and out["reason"] == "budget:run_cap" and client.calls == 0


@pytest.mark.parametrize("answer,frag", [
    ({"judgement": _answer()}, "missing"),
    (_answer(quote="a paraphrase that is not in the reply"), "not a quote"),
    (_answer(scores=(9, 5, 5)), "integer from 1 to 5")])
def test_invalid_judge_output_is_recorded_unparseable_never_retried(jdb, jdsn, answer, frag):
    client, res = FakeJudgeClient(answer=answer), _turn()
    out = _judge(jdb, client).after_turn(res, "hi", "en")
    assert out["status"] == "UNPARSEABLE" and frag in out["reason"] and client.calls == 1
    row = _row(jdsn, res.turn_ref)
    assert row["clarity"] is None and row["status"] == "UNPARSEABLE"


def test_a_provider_timeout_is_recorded_and_never_raises(jdb, jdsn):
    from setuhaul.agent.models import ModelError
    res = _turn()
    out = _judge(jdb, FakeJudgeClient(raise_=ModelError("read timeout", "PROVIDER_TIMEOUT", False))).after_turn(res, "hi", "en")
    assert out["status"] == "ERROR" and out["reason"].startswith("provider:")
    assert _row(jdsn, res.turn_ref)["status"] == "ERROR"


def test_a_database_failure_while_recording_is_visible_and_never_raises(jdb, caplog):
    class BrokenDb:
        def tx(self, *a, **k):
            raise psycopg.OperationalError("down")
    j = OJ.OnlineJudge(OJ.config_from_env(_judge_env()), BrokenDb(), FakeJudgeClient(), None)
    out = j.after_turn(_turn(), "hi", "en")
    assert out["status"] in ("ERROR", "SKIPPED") and j.stats["errors"] + j.stats["record_errors"] >= 1


@pytest.mark.parametrize("res,text,why", [
    (None, "hi", "no_turn"), (_turn(turn_ref=None), "hi", "no_turn"), (_turn(reply=""), "hi", "no_reply"),
    (_turn(reply="x" * 2001), "hi", "reply_too_long"), (_turn(), "", "no_driver_message")])
def test_ineligible_turns_are_not_judged(jdb, res, text, why):
    client = FakeJudgeClient()
    out = _judge(jdb, client).after_turn(res, text, "en")
    assert out == {"status": "INELIGIBLE", "reason": why} and client.calls == 0


def test_judgement_rows_are_advisory_by_construction_and_only_the_agent_can_write_them(jdsn):
    with psycopg.connect(jdsn, autocommit=True) as c:
        rls = c.execute("""SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                            WHERE oid = 'setuhaul.agent_turn_judgements'::regclass""").fetchone()
        assert rls == (True, True)
        ref = str(uuid.uuid4())
        c.execute("SELECT setuhaul.record_turn_judgement(%s::jsonb)", (json.dumps(
            {"turn_ref": ref, "status": "SKIPPED", "prompt_version": "p", "eval_version": "e"}),))
        with pytest.raises(psycopg.errors.RaiseException, match="APPEND_ONLY"):
            c.execute("UPDATE setuhaul.agent_turn_judgements SET status='SCORED' WHERE turn_ref=%s", (ref,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO setuhaul.agent_turn_judgements (turn_ref, status, prompt_version, eval_version, advisory) "
                      "VALUES (gen_random_uuid(), 'SCORED', 'p', 'e', false)")
        grants = c.execute("""SELECT grantee FROM information_schema.routine_privileges
                               WHERE routine_schema='setuhaul' AND routine_name='record_turn_judgement'
                                 AND privilege_type='EXECUTE'""").fetchall()
        assert {g[0] for g in grants} <= {"setuhaul_agent", "setuhaul_rpc", "postgres"}
        writers = c.execute("""SELECT grantee FROM information_schema.table_privileges WHERE table_schema='setuhaul'
                                 AND table_name='agent_turn_judgements' AND privilege_type IN ('INSERT','UPDATE','DELETE')""").fetchall()
        assert {w[0] for w in writers} <= {"setuhaul_rpc", "postgres"}
        assert c.execute("SELECT setuhaul.record_turn_judgement(%s::jsonb)", (json.dumps({
            "turn_ref": str(uuid.uuid4()), "status": "SCORED", "prompt_version": "p", "eval_version": "e",
            "driver_id": "+919876543210"}),)).fetchone()[0] is True
        assert c.execute("SELECT count(*) FROM setuhaul.agent_turn_judgements WHERE driver_id IS NOT NULL").fetchone()[0] == 0


def test_the_agent_worker_runs_the_judge_after_the_turn_and_a_judge_failure_never_fails_the_record(monkeypatch):
    from setuhaul.aws import lambdas
    calls = []

    class Orch:
        lease_s, on_heartbeat = 120, None

        def handle(self, body):
            return SimpleNamespace(reply="ok", path=["x"], extractor="model", fallback_reason=None, correlation_id="c",
                                   turn_ref=str(uuid.uuid4()), ingest_correlation_id=None, driver_text="hi",
                                   language="en")

    class Boom:
        def after_turn(self, *a):
            calls.append(a)
            raise RuntimeError("judge exploded")
    monkeypatch.setenv("SETUHAUL_QUEUE_URL", "q")
    monkeypatch.setattr(lambdas, "_orchestrator", lambda: Orch())
    monkeypatch.setattr(lambdas, "_online_judge", lambda: Boom())
    out = lambdas.agent_handler({"Records": [{"receiptHandle": "r", "messageId": "m1", "body": "{}"}]}, None,
                                sqs=SimpleNamespace(change_message_visibility=lambda **k: None))
    assert out == {"batchItemFailures": []} and len(calls) == 1
