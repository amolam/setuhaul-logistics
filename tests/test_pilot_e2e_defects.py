"""Synthetic pilot E2E defects (26 Nov 2026), found on the live pilot with a real Telegram account. Each test pins one
fix; the module reuses the dispatcher/Telegram harness of test_dispatch_offers (its own database)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from setuhaul.agent.orchestrator import Orchestrator
from setuhaul.aws import lambdas
from setuhaul.notify import telegram as tg_texts
from test_api import IST, keys  # noqa: F401
from test_dispatch_offers import (Tg, add_driver, app, appointments, client, dsn, link, offer,  # noqa: F401
                                  offer_status, ops, q)


# ---------------------------------------------------------------- 1. offer acceptance without an agent login (AWS api)
def _continuations(app_, before):
    return [e for e in list(app_.state.inbound)[before:] if e.get("kind") == "offer_continuation"]


def test_an_accepted_offer_in_a_workload_without_the_agent_login_is_finished_by_the_agent(client, dsn, keys, app,
                                                                                      monkeypatch):
    d = add_driver(dsn, eta_at=datetime.now(IST) + timedelta(hours=30))
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    svc = app.state.service
    monkeypatch.setattr(svc, "_has_login", lambda role: role != "agent")   # the deployed api: ingest + console only
    before = len(app.state.inbound)
    reply = tg.press(o["offer_id"], "A")
    assert reply == tg_texts.OFFER_REPLIES["QUEUED"]                       # no PermissionError, no 503
    assert offer_status(dsn, o["offer_id"]) == "ACCEPTED" and appointments(dsn, d["sid"]) == []
    (ev,) = _continuations(app, before)
    assert ev["driver_id"] == d["drv"] and ev["offer_id"] == o["offer_id"]
    monkeypatch.undo()                                                     # the agent worker: every login it needs
    r = svc.continue_accepted_offer(ev)
    assert r["result"] == "SUBMITTED"
    assert [s for _, s in appointments(dsn, d["sid"])] == ["PENDING_CONFIRMATION"]
    replies = q(dsn, "SELECT body_masked FROM chat_messages WHERE sender_type='AGENT' AND reply_to=%s",
                ev["message_id"])
    assert [b for (b,) in replies] == [tg_texts.OFFER_REPLIES["SUBMITTED"]]   # the driver hears the outcome
    r2 = svc.continue_accepted_offer(ev)                                   # SQS redelivery: nothing new
    assert len(appointments(dsn, d["sid"])) == 1
    assert len(q(dsn, "SELECT 1 FROM chat_messages WHERE sender_type='AGENT' AND reply_to=%s", ev["message_id"])) == 1
    assert r2["result"] in ("SUBMITTED", "ACCEPTED")


def test_a_typed_yes_without_the_agent_login_also_queues_the_continuation(client, dsn, keys, app, monkeypatch):
    d = add_driver(dsn, eta_at=datetime.now(IST) + timedelta(hours=31))
    tg = link(client, keys, dsn, d["drv"])
    offer(client, keys, d)
    monkeypatch.setattr(app.state.service, "_has_login", lambda role: role != "agent")
    before = len(app.state.inbound)
    assert tg.text("yes") == tg_texts.OFFER_REPLIES["QUEUED"]
    assert len(_continuations(app, before)) == 1


def test_the_agent_handler_runs_a_continuation_instead_of_a_turn(monkeypatch):
    calls = []

    class Svc:
        def continue_accepted_offer(self, body):
            calls.append(body)
            return {"result": "SUBMITTED"}

    class Orch:
        svc, lease_s, on_heartbeat = Svc(), 120, None

        def handle(self, ev):
            raise AssertionError("a continuation is not an agent turn")

    monkeypatch.setattr(lambdas, "_orchestrator", lambda: Orch())
    monkeypatch.setenv("SETUHAUL_QUEUE_URL", "https://sqs.example/q.fifo")
    body = {"kind": "offer_continuation", "driver_id": "DRV-x", "offer_id": str(uuid.uuid4()), "message_id": 5}
    out = lambdas.agent_handler({"Records": [{"receiptHandle": "r", "messageId": "m1",
                                              "body": __import__("json").dumps(body)}]}, None, sqs=object())
    assert out == {"batchItemFailures": []} and calls == [body]


# ---------------------------------------------------------------- 2. link replies
def test_link_replies_say_what_happened(client, dsn, keys):
    a = add_driver(dsn)
    tg = link(client, keys, dsn, a["drv"])
    assert tg.text("/start") == tg_texts.TEXT_ALREADY_LINKED              # not just "You are linked"
    assert tg.text("/start not-a-code") == tg_texts.TEXT_LINK_FAILED       # never read as a bare /start
    b = add_driver(dsn)
    code = client.post(f"/console/drivers/{b['drv']}/telegram-link-code", headers=ops(keys)).json()["code"]
    assert tg.text(code) == tg_texts.TEXT_ACCOUNT_TAKEN                    # typed without /start, same account
    assert q(dsn, "SELECT count(*) FROM driver_channel_links WHERE driver_id=%s", b["drv"])[0][0] == 0
    other = Tg(client)
    assert "linked to SetuHaul" in other.text(code)                        # the driver's own account links
    assert q(dsn, "SELECT count(*) FROM driver_channel_links WHERE driver_id=%s", b["drv"])[0][0] == 1


def test_ordinary_words_are_never_read_as_a_code():
    for t in ("YES", "1", "RESCHEDULE", "Traffic on the highway", "running late", "OK THANKS"):
        assert not tg_texts.looks_like_code(t), t


# ---------------------------------------------------------------- 3. reschedule with nothing booked
def test_reschedule_with_no_booking_says_so_instead_of_asking_for_yes(client, dsn, keys, app):
    d = add_driver(dsn, eta_at=datetime.now(IST) + timedelta(hours=32))
    tg = link(client, keys, dsn, d["drv"])
    orch = Orchestrator(app.state.service, app.state.sessions, model=None)

    def turn(text):
        before = len(app.state.inbound)
        tg.text(text)
        ev = next(e for e in list(app.state.inbound)[before:] if e.get("driver_id") == d["drv"])
        return orch.handle(ev).reply

    assert "1." in turn("Traffic on the highway. Reaching around 1:45 PM, any slot?")
    assert "YES" in turn("1")                                              # a read-back now waits for a yes
    reply = turn("please reschedule my slot")
    assert "no booking" in reply.lower() and "dispatcher" in reply.lower()
    assert appointments(dsn, d["sid"]) == []


# ---------------------------------------------------------------- 4. schedule proposal without a feasibility login
def test_the_schedule_proposal_works_without_a_feasibility_login(client, keys, app, monkeypatch):
    from test_dispatch_offers import planner
    monkeypatch.setattr(app.state.service, "_has_login", lambda role: role in ("ingest", "console"))
    r = client.get("/console/facilities/FAC-JAI-01/schedule-proposal", headers=planner(keys))
    assert r.status_code == 200, r.text                                    # was a 500 (PermissionError) on AWS


# ---------------------------------------------------------------- 5. no bot token in the logs
def test_http_client_request_lines_are_not_logged():
    for name in lambdas.QUIET_HTTP_LOGGERS:
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


# ---------------------------------------------------------------- 6. a container that declared a superseded deployment
class _Db:
    def __init__(self, dep):
        self.deployment = dep


class _App:
    def __init__(self, dep):
        self.state = type("S", (), {"service": type("Svc", (), {"db": _Db(dep)})(),
                                    "booking_gate": {"switch": True, "ok": True}})()


def test_a_booking_on_container_on_a_superseded_deployment_of_its_code_rebuilds(monkeypatch):
    import setuhaul.api.inbound as inbound
    newest = {"deployment_id": "m-new", "artifact_sha256": "code-a"}
    monkeypatch.setattr(inbound, "gate_snapshot", lambda db, env=None, dep=None: {"manifest": newest})
    t = [1000.0]
    rt = lambdas._Runtime(clock=lambda: t[0])
    old = _App({"environment": "pilot-x", "deployment_id": "m-old", "code_sha256": "code-a"})
    new = _App({"environment": "pilot-x", "deployment_id": "m-new", "code_sha256": "code-a"})
    rt._app, rt._settings = old, type("St", (), {"booking_enabled": True})()
    rebuilt = []
    monkeypatch.setattr(rt, "_retry", lambda cur: rebuilt.append(cur) or new)
    assert rt.get() is new and rebuilt == [old]                            # superseded: one start-up re-check
    t[0] += 10
    assert rt.get() is new                                                 # throttled
    t[0] += 31
    assert rt.get() is new and rebuilt == [old]                            # current deployment: never rebuilt


def test_other_code_or_an_unreadable_manifest_never_rebuilds(monkeypatch):
    import setuhaul.api.inbound as inbound
    app_ = _App({"environment": "pilot-x", "deployment_id": "m-old", "code_sha256": "code-a"})
    monkeypatch.setattr(inbound, "gate_snapshot",
                        lambda db, env=None, dep=None: {"manifest": {"deployment_id": "m-new", "artifact_sha256": "b"}})
    assert lambdas._Runtime._superseded(app_) is False                      # a new release is not this container's

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(inbound, "gate_snapshot", broken)
    assert lambdas._Runtime._superseded(app_) is False
