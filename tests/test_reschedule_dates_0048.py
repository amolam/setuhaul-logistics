"""Workstream C (27 Nov fix plan, issue 9), through the orchestrator, the real API and the database: a requested day is
searched on that day only, said back with its date, refused plainly when it cannot be used, and a reschedule of a
confirmed booking becomes a request that leaves the booking as it is until decided. Scripted model and the rules reader
(no paid calls)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from setuhaul.agent.models import FakeModel
from setuhaul.agent.orchestrator import Orchestrator
from test_api import IST, JAI, Driver, H, add_driver, app, client, dsn, keys, staff_token  # noqa: F401
from test_orchestrator import converse_to_yes, db_one, exd, options_of


def day(n: int):
    d = (datetime.now(IST) + timedelta(days=n)).date()
    return d, f"{d.day} {d.strftime('%b')}"


def fresh(dsn, client):
    _, phone, _ = add_driver(dsn)
    return Driver(client, phone)


def test_options_are_on_the_requested_day_and_say_the_date(app, client, dsn):
    d = fresh(dsn, client)
    target, label = day(2)
    orch = Orchestrator(app.state.service, app.state.sessions,
                        FakeModel(extractions=[exd(intent="ASK_OPTIONS", requested_date_local=label, exception_type="UNKNOWN")]))
    r = orch.handle(d.say(f"Any slot on {label}?"))
    assert "options" in r.path, r.path
    opts = options_of(app, d)
    assert opts and all(datetime.fromisoformat(o["start_ts"]).astimezone(IST).date() == target for o in opts)
    assert f"on {label}" in r.reply                                         # the read-out names the day


def test_the_rules_reader_finds_the_same_day(app, client, dsn):
    d = fresh(dsn, client)
    target, label = day(2)
    r = Orchestrator(app.state.service, app.state.sessions, None).handle(d.say(f"any slot on {label}"))
    assert r.extractor == "rules" and "options" in r.path
    assert all(datetime.fromisoformat(o["start_ts"]).astimezone(IST).date() == target for o in options_of(app, d))


def test_no_slot_that_day_is_said_and_no_other_day_is_offered(app, client, dsn):
    d = fresh(dsn, client)
    _, label = day(5)                                    # within 7 days, but the test grid publishes 72 h of slots
    r = Orchestrator(app.state.service, app.state.sessions,
                     FakeModel(extractions=[exd(intent="ASK_OPTIONS", requested_date_local=label)])).handle(d.say(f"slot on {label}"))
    assert "no-slot-on-day" in r.path and r.escalation_id is None
    assert f"no free slot on {label}" in r.reply and "not booked any other day" in r.reply
    assert options_of(app, d) == []                                        # nothing was shown in its place


def test_a_day_beyond_seven_days_is_refused_before_any_search(app, client, dsn):
    d = fresh(dsn, client)
    _, label = day(9)
    r = Orchestrator(app.state.service, app.state.sessions,
                     FakeModel(extractions=[exd(intent="ASK_OPTIONS", requested_date_local=label)])).handle(d.say(f"slot on {label}"))
    assert r.path[-1] == "ask:requested_date" and "up to 7 days ahead" in r.reply
    assert "find_feasible_options" not in [t for t, _ in r.tools]


def test_the_service_applies_the_seven_day_rule_whatever_the_agent_asks(app, client, dsn):
    d = fresh(dsn, client)
    d.say("any slot?")                                   # the session exists once the driver has written
    p = app.state.sessions.verify(d.session)
    far, _ = day(9)
    past, _ = day(-1)
    svc = app.state.service
    for dt, why in ((far, "REQUESTED_DATE_TOO_FAR"), (past, "REQUESTED_DATE_PAST")):
        out = svc.call_tool(p, "find_feasible_options", 0, {"requested_date": dt.isoformat()})
        assert out["status"] == "NOT_FEASIBLE" and out["data"]["reason"] == why


def test_reschedule_of_a_confirmed_booking_is_a_request_that_keeps_the_booking(app, client, dsn, keys):
    d, orch, r3, _ = converse_to_yes(app, client, dsn)
    appt = db_one(dsn, """SELECT a.appointment_id FROM setuhaul.appointments a JOIN setuhaul.chat_threads t USING (shipment_id)
                          WHERE t.thread_id=%s AND a.status='PENDING_CONFIRMATION'""", d.thread)[0]
    r = client.post(f"/console/requests/{appt}/decision", headers=H(staff_token(keys, groups=("planner",))),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    target, label = day(2)
    orch.model.extractions.append(exd(intent="RESCHEDULE", requested_date_local=label, exception_type="UNKNOWN"))
    r4 = orch.handle(d.say(f"Reschedule to {label}"))
    assert "reschedule" in r4.path and "options" in r4.path, r4.path
    opts = options_of(app, d)
    assert opts and all(datetime.fromisoformat(o["start_ts"]).astimezone(IST).date() == target for o in opts)
    orch.model.extractions.append(exd(intent="BOOK", selected_option_id=opts[0]["option_id"]))
    r5 = orch.handle(d.say("1"))
    assert f"on {label}" in r5.reply                                       # the read-back states the date
    # "yes, <the same day>": the day matches the option, so it is NOT a new constraint (no re-offer); the strict-yes
    # rule (a yes with anything added is asked again, unchanged) asks for a plain YES, which then books
    orch.model.extractions.append(exd(intent="BOOK", explicit_confirmation=True, requested_date_local=label))
    r6 = orch.handle(d.say(f"yes, {label}"))
    assert r6.path == ["confirmation"] and "constraint-before-select" not in r6.path, r6.path
    orch.model.extractions.append(exd(intent="BOOK", explicit_confirmation=True))
    r7 = orch.handle(d.say("yes"))
    assert r7.path[:2] == ["confirmation", "book"], r7.path
    import psycopg
    with psycopg.connect(dsn) as c:
        st = c.execute("""SELECT a.status, a.is_current FROM setuhaul.appointments a
                            JOIN setuhaul.chat_threads t USING (shipment_id) WHERE t.thread_id=%s""", (d.thread,)).fetchall()
    assert ("CONFIRMED", True) in st                                       # the booking stands until the request is decided
    assert any(s == "PENDING_CONFIRMATION" for s, _ in st), st
