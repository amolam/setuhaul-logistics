"""Demo release: Telegram conversation visibility, event-driven outbound delivery and end-to-end trace correlation.

Found in the 26 Nov 2026 E2E test (measured on the pilot): the planner's conversation missed what the bot sent at
once from the webhook and the fixed notices (booking_approved); the page refreshed only by hand; and a reply waited
for the sender's one-minute tick (reply stored -> notification created: p50 44 s, p95 59 s; end to end p50 54 s).
The four traces of one exchange (telegram_inbound, agent_turn, llm_judge, telegram_outbound) were unrelated roots.

Own database (setuhaul_build_demo). Drivers talk over the signed Telegram webhook to the local app (test sink).
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from setuhaul.api.app import Settings, create_app
from setuhaul.notify import kick as K
from setuhaul.notify import telegram as tg
from setuhaul.notify.outbound import OUTBOUND_TRACE, OutboundRelay, TestSinkProvider
from setuhaul.observability import tracing
from setuhaul.observability.identifiers import is_valid
from test_api import CLIENT, GGN, ISSUER, JAI, META_SECRET, SESSION_SECRET, keys  # noqa: F401
from test_dispatch_offers import (TG_SECRET, Tg, add_driver, appointments, link, make_database, offer, planner,
                                  q)
from test_full_activation import FakeJudgeClient, _judge, _turn, jdb, jdsn, tracer  # noqa: F401


@pytest.fixture(scope="module")
def dsn():
    return make_database("setuhaul_build_demo")


@pytest.fixture(scope="module")
def app(dsn, keys):  # noqa: F811
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True, telegram_webhook_secret=TG_SECRET))


@pytest.fixture
def client(app):
    return TestClient(app)


def valid_driver(dsn):
    while True:                                  # the id gate refuses ids with 7+ digits in a row (see test_full_activation)
        d = add_driver(dsn)
        if is_valid("driver_id", d["drv"]):
            return d


def thread_of(dsn, drv):
    return q(dsn, "SELECT thread_id FROM chat_threads WHERE driver_id=%s ORDER BY opened_at DESC LIMIT 1", drv)[0][0]


def messages(dsn, thread):
    return q(dsn, """SELECT sender_type, channel, external_message_id, body_masked FROM chat_messages
                     WHERE thread_id=%s ORDER BY created_at, message_id""", thread)


def outbox_for(dsn, mids):
    return q(dsn, "SELECT count(*) FROM outbox WHERE aggregate='chat_message' AND aggregate_id = ANY(%s)",
             [str(m) for m in mids])[0][0]


# ================================================================ 1. visibility: inbound, bot replies, notices
def test_an_inbound_message_is_stored_once_in_order_and_retries_add_nothing(client, dsn, keys, app):  # noqa: F811
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    before = len(app.state.inbound)
    for uid, text in ((910000001, "running 2 hours late"), (910000002, "traffic near Jaipur toll"),
                      (910000003, "will reach by evening")):
        t.text(text, update_id=uid)
    t.text("running 2 hours late", update_id=910000001)                    # Telegram webhook retry
    th = thread_of(dsn, d["drv"])
    drv_msgs = [m for m in messages(dsn, th) if m[0] == "DRIVER"]
    assert [m[2] for m in drv_msgs] == ["tg:910000001", "tg:910000002", "tg:910000003"]   # once each, in order
    evs = [e for e in list(app.state.inbound)[before:] if e.get("driver_id") == d["drv"]]
    assert len(evs) == 3                                                   # one agent turn per message, none for the retry


def test_the_bots_immediate_reply_is_stored_once_as_a_bot_message_and_never_sent_again(client, dsn, keys):  # noqa: F811
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    t.text("running late, any slot?", update_id=920000001)               # opens the conversation
    assert t.text("/start", update_id=920000002) == tg.TEXT_ALREADY_LINKED
    assert t.text("/start", update_id=920000002) == tg.TEXT_ALREADY_LINKED  # webhook retry of the same update
    th = thread_of(dsn, d["drv"])
    bot = q(dsn, """SELECT message_id, sender_type, channel, external_message_id, body_masked FROM chat_messages
                    WHERE thread_id=%s AND sender_type='SYSTEM'""", th)
    assert [(b[1], b[2], b[3], b[4]) for b in bot] == [("SYSTEM", "TELEGRAM", "tgr:tg:920000002", tg.TEXT_ALREADY_LINKED)]
    assert outbox_for(dsn, [b[0] for b in bot]) == 0                      # SYSTEM rows never enter the outbox


def test_an_offer_answer_the_driver_received_is_in_the_conversation(client, dsn, keys):  # noqa: F811
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    reply = t.press(o["offer_id"], "A", callback_id="cbdemo1")
    assert "with the warehouse planner" in reply
    rows = [m for m in messages(dsn, thread_of(dsn, d["drv"])) if m[0] == "SYSTEM"]
    assert rows == [("SYSTEM", "TELEGRAM", "tgr:tgcb:cbdemo1", reply)]


def test_two_drivers_on_one_bot_never_see_each_others_messages(client, dsn, keys):  # noqa: F811
    a, b = valid_driver(dsn), valid_driver(dsn)
    ta, tb = link(client, keys, dsn, a["drv"]), link(client, keys, dsn, b["drv"])
    ta.text("driver A: stuck at the toll", update_id=930000001)
    tb.text("driver B: tyre puncture", update_id=930000002)
    ta.text("/start", update_id=930000003)
    tb.text("/start", update_id=930000004)
    tha, thb = thread_of(dsn, a["drv"]), thread_of(dsn, b["drv"])
    assert tha != thb
    ma, mb = messages(dsn, tha), messages(dsn, thb)
    assert {m[2] for m in ma} == {"tg:930000001", "tgr:tg:930000003"} and {m[2] for m in mb} == {"tg:930000002",
                                                                                                     "tgr:tg:930000004"}
    assert all("driver B" not in (m[3] or "") for m in ma) and all("driver A" not in (m[3] or "") for m in mb)
    sent = {chat for chat, _ in client.app.state.telegram.sent}
    assert {str(ta.uid), str(tb.uid)} <= sent                              # each reply went to its own chat


def test_a_bot_reply_is_only_stored_for_the_transactions_own_driver_and_open_conversation(dsn, app):
    a, b = valid_driver(dsn), valid_driver(dsn)
    db = app.state.service.db
    with db.tx("ingest", driver=a["drv"]) as c:                             # no conversation yet: nothing stored
        assert c.execute("SELECT record_bot_reply(%s,%s,%s)", (a["drv"], "tgr:tg:1", "hello")).fetchone()[0] is None
    with pytest.raises(Exception) as e:
        with db.tx("ingest", driver=a["drv"]) as c:
            c.execute("SELECT record_bot_reply(%s,%s,%s)", (b["drv"], "tgr:tg:2", "hello"))
    assert "only the transaction's own driver" in str(e.value)
    with pytest.raises(Exception) as e:
        with db.tx("ingest", driver=a["drv"]) as c:
            c.execute("SELECT record_bot_reply(%s,%s,%s)", (a["drv"], "not-a-telegram-ref", "hello"))
    assert "tgr:" in str(e.value)


def test_a_failure_to_store_the_bot_reply_is_visible_and_never_fails_the_update(client, dsn, keys, app, caplog,  # noqa: F811
                                                                                 monkeypatch):
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    t.text("late again", update_id=940000001)
    svc = app.state.service
    real = svc.db.tx

    def tx(role, *a, **k):
        cm = real(role, *a, **k)
        if role == "ingest" and k.get("driver") == d["drv"] and not getattr(tx, "armed", False):
            tx.armed = True                                          # the record_bot_reply transaction only
            raise psycopg.OperationalError("database went away")
        return cm
    monkeypatch.setattr(svc, "_record_bot_reply", lambda drv, ext, text: (_ for _ in ()).throw(RuntimeError()) if False
                        else type(svc)._record_bot_reply(SimpleNamespace(db=SimpleNamespace(tx=tx)), drv, ext, text))
    with caplog.at_level("WARNING"):
        assert t.text("/start", update_id=940000002) == tg.TEXT_ALREADY_LINKED   # the driver still gets the reply
    assert "ALERT bot reply not stored" in caplog.text


def test_a_fixed_notice_the_driver_received_is_stored_once_in_the_conversation(client, dsn, keys, app, monkeypatch):  # noqa: F811
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    monkeypatch.setenv("SETUHAUL_TELEGRAM_AUTHORIZED_DRIVERS", d["drv"])
    o = offer(client, keys, d)
    t.press(o["offer_id"], "A")
    (appt, _), = appointments(dsn, d["sid"])
    r = client.post(f"/console/requests/{appt}/decision", headers=planner(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    sink = tg.TelegramTestSink()
    relay = OutboundRelay(app.state.service.db, TestSinkProvider(), telegram=sink)
    relay.run_once(limit=50)
    relay.run_once(limit=50)                                               # a second pass stores nothing again
    th = thread_of(dsn, d["drv"])
    notices = q(dsn, """SELECT m.channel, m.body_masked, n.template_id FROM chat_messages m
                        JOIN notifications n ON m.external_message_id = 'notice:' || n.notification_id::text
                        WHERE m.thread_id=%s AND m.sender_type='SYSTEM'""", th)
    assert ("TELEGRAM", "Your dock booking is confirmed by the warehouse.", "booking_approved") in notices
    assert len([n for n in notices if n[2] == "booking_approved"]) == 1
    # an agent reply is never duplicated as a notice: only template sends (no stored body) are stored this way
    assert all(n[2] != "agent_reply" for n in notices)


def test_the_conversation_api_shows_bot_messages_and_notices_with_the_driver_messages(client, dsn, keys):  # noqa: F811
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    t.text("running 30 min late", update_id=950000001)
    t.text("/start", update_id=950000002)
    th = thread_of(dsn, d["drv"])
    r = client.get(f"/console/threads/{th}", headers=planner(keys))
    assert r.status_code == 200, r.text
    senders = [(m["sender"], m["channel"]) for m in r.json()["messages"]]
    assert ("DRIVER", "TELEGRAM") in senders and ("SYSTEM", "TELEGRAM") in senders
    listed = client.get("/console/threads", headers=planner(keys)).json()["threads"]
    assert any(x["thread_id"] == th for x in listed)


def test_the_dashboard_refreshes_conversations_on_its_own():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "setuhaul" / "api" / "console_static" / "assets" / "app.js").read_text(
        encoding="utf-8")
    assert "const LIVE_REFRESH_MS = 3000;" in js and "setInterval(liveRefresh, LIVE_REFRESH_MS)" in js
    assert 'document.visibilityState !== "visible"' in js                  # paused while the tab is hidden
    assert "if(quiet && sig === threadSig) return;" in js                  # nothing redrawn when nothing changed


# ================================================================ 2. latency: event-driven outbound
class FakeLambda:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def invoke(self, **kw):
        if self.fail:
            raise RuntimeError("throttled")
        self.calls.append(kw)
        return {"StatusCode": 202}


@pytest.fixture
def fresh_kick(monkeypatch):
    monkeypatch.setattr(K, "_last", {"t": 0.0})


def test_a_kick_invokes_the_sender_asynchronously_and_never_waits(fresh_kick):
    lam = FakeLambda()
    assert K.kick_outbound("agent_reply", client=lam, env={"SETUHAUL_OUTBOUND_FUNCTION": "setuhaul-pilot-outbound"})
    (call,), = [lam.calls]
    assert call["FunctionName"] == "setuhaul-pilot-outbound" and call["InvocationType"] == "Event"


def test_without_the_function_name_or_on_failure_nothing_breaks_and_the_tick_stays_the_fallback(fresh_kick):
    assert K.kick_outbound("x", client=FakeLambda(), env={}) is False                # tests, local runs
    assert K.kick_outbound("x", client=FakeLambda(fail=True), env={"SETUHAUL_OUTBOUND_FUNCTION": "f"}) is False


def test_one_burst_kicks_once(fresh_kick):
    lam, env = FakeLambda(), {"SETUHAUL_OUTBOUND_FUNCTION": "f"}
    assert K.kick_outbound("a", client=lam, env=env) and not K.kick_outbound("b", client=lam, env=env)
    assert len(lam.calls) == 1


def test_the_agent_worker_kicks_the_sender_after_a_committed_reply_only(monkeypatch):
    from setuhaul.aws import lambdas
    kicks = []
    replies = iter(["Reply with the number you want.", None])

    class Orch:
        lease_s, on_heartbeat = 120, None

        def handle(self, body):
            return SimpleNamespace(reply=next(replies), path=["x"], extractor="model", fallback_reason=None,
                                   correlation_id="c", turn_ref=str(uuid.uuid4()), ingest_correlation_id=None)
    monkeypatch.setenv("SETUHAUL_QUEUE_URL", "q")
    monkeypatch.setattr(lambdas, "_orchestrator", lambda: Orch())
    monkeypatch.setattr(lambdas, "_judge_after_turn", lambda res: None)
    monkeypatch.setattr(lambdas, "_kick", kicks.append)
    sqs = SimpleNamespace(change_message_visibility=lambda **k: None)
    for mid in ("m1", "m2"):
        lambdas.agent_handler({"Records": [{"receiptHandle": "r", "messageId": mid, "body": "{}"}]}, None, sqs=sqs)
    assert kicks == ["agent_reply"]                                        # the turn without a reply kicks nothing


def test_a_planners_decision_kicks_the_sender(client, dsn, keys, monkeypatch):  # noqa: F811
    kicks = []
    monkeypatch.setattr(K, "kick_outbound", lambda reason, **kw: kicks.append(reason) or True)
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    t.press(o["offer_id"], "A")
    (appt, _), = appointments(dsn, d["sid"])
    r = client.post(f"/console/requests/{appt}/decision", headers=planner(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and kicks == ["decision"]


def test_the_stack_lets_only_the_agent_and_api_kick_the_sender_with_no_retries():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "infra" / "setuhaul_stack.py").read_text(encoding="utf-8")
    assert "for f in (agent_fn, api_fn):\n            outbound_fn.grant_invoke(f)" in src
    assert 'f.add_environment("SETUHAUL_OUTBOUND_FUNCTION", outbound_fn.function_name)' in src
    assert "outbound_fn.configure_async_invoke(retry_attempts=0" in src


# ================================================================ 3. trace correlation
def test_the_judge_is_a_child_run_of_its_agent_turn_and_feedback_stays_on_the_turn(jdb):  # noqa: F811
    t = tracer()
    fb = []
    t.exporter.client = SimpleNamespace(create_feedback=lambda *a: fb.append(a))
    res = _turn()
    with t.turn(res.turn_ref, "agent_turn", metadata={"thread_id": "abc123"}) as root:
        pass
    res.trace_parent = tracing.Tracer.parent_ref(root)
    res.conversation_ref = "abc123"
    assert _judge(jdb, FakeJudgeClient(), t).after_turn(res, "mujhe slot chahiye", "en")["status"] == "SCORED"
    t.flush(1)
    runs = {r["name"]: r for b in t.exporter.batches for r in b}
    turn, judge = runs["agent_turn"], runs["llm_judge"]
    assert judge["trace_id"] == turn["id"] == res.turn_ref and judge["parent_run_id"] == turn["id"]
    assert judge["dotted_order"].startswith(turn["dotted_order"] + ".")
    assert judge["extra"]["metadata"]["thread_id"] == turn["extra"]["metadata"]["thread_id"] == "abc123"
    assert {a[1] for a in fb} == {"quality:clarity", "quality:tone", "quality:language_match"}
    assert all(a[0] == res.turn_ref for a in fb)                           # feedback on the agent turn, as before
    assert "mujhe" not in json.dumps(judge)


def test_a_child_of_an_untraced_turn_is_not_traced():
    t = tracer()
    with t.child(None, str(uuid.uuid4()), "llm_judge") as run:
        assert run is tracing.NULL_RUN
    assert t.flush(0.2)["exported"] == 0


def test_the_agent_turn_names_its_conversation_and_message_by_opaque_refs():
    from setuhaul.agent.orchestrator import Orchestrator
    meta = Orchestrator._trace_meta(SimpleNamespace(model=None), "c", "t", {"thread_id": "THR-abc", "message_id": 42})
    assert meta["thread_id"] == meta["conversation_ref"] == tracing.ref("THR-abc") and "THR-abc" not in json.dumps(meta)
    assert meta["message_ref"] == tracing.ref(42)


def test_the_outbound_trace_carries_the_conversation_and_the_answered_message():
    t = tracer()
    sink = tg.TelegramTestSink()
    OUTBOUND_TRACE.set({"thread_id": tracing.ref("THR-abc"), "reply_to_message_ref": tracing.ref(42)})
    try:
        tg.TracedTelegram(sink, t).send("5550001234", "Reply 1", "notif-1")
    finally:
        OUTBOUND_TRACE.set(None)
    t.flush(1)
    (run,), = t.exporter.batches
    meta = run["extra"]["metadata"]
    assert meta["thread_id"] == tracing.ref("THR-abc") and meta["reply_to_message_ref"] == tracing.ref(42)
    assert "5550001234" not in json.dumps(run) and "Reply 1" not in json.dumps(run)


def test_the_inbound_trace_is_the_turns_ingest_id_and_names_the_conversation(client, dsn, keys, app):  # noqa: F811
    d = valid_driver(dsn)
    t = link(client, keys, dsn, d["drv"])
    cap = tracer()
    old = tracing.tracer()
    tracing.install(cap)
    try:
        cid = str(uuid.uuid4())
        before = len(app.state.inbound)
        r = client.post("/webhooks/telegram", content=json.dumps({"update_id": 960000001, "message": {
            "message_id": 1, "date": 1764064800, "from": {"id": t.uid, "is_bot": False},
            "chat": {"id": t.uid, "type": "private"}, "text": "running late by an hour"}}).encode(),
            headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET, "Content-Type": "application/json",
                     "x-correlation-id": cid})
        assert r.status_code == 200
        cap.flush(1)
    finally:
        tracing.install(old)
    (run,) = [x for b in cap.exporter.batches for x in b if x["name"] == "telegram_inbound"]
    assert run["id"] == cid                                                # = the agent turn's ingest_correlation_id
    ev = next(e for e in list(app.state.inbound)[before:] if e.get("driver_id") == d["drv"])
    assert ev["correlation_id"] == cid
    meta = run["extra"]["metadata"]
    assert meta["thread_id"] == tracing.ref(thread_of(dsn, d["drv"])) and meta["message_ref"] == tracing.ref(ev["message_id"])
    assert "webhook_received_at" in meta and str(t.uid) not in json.dumps(run) and "running late" not in json.dumps(run)
