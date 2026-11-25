"""Migration 0036: reassignment and cancellation of CONFIRMED appointments, Telegram outbound through the existing
relay (test sink), link-code throttling, the planner schedule and operations health reads, escalation actions.

Own database (setuhaul_build_dispatch2): may run beside tests/test_dispatch_offers.py. Nothing leaves the process:
the relay's Telegram transport here is TelegramTestSink and the default provider is LocalProvider.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from setuhaul.api.app import Settings, create_app
from setuhaul.api.db import Db
from setuhaul.notify.outbound import LocalProvider, OutboundRelay
from setuhaul.notify.telegram import TelegramTestSink
from test_api import CLIENT, GGN, ISSUER, JAI, META_SECRET, SESSION_SECRET, Driver, H, keys, staff_token  # noqa: F401
from test_dispatch_offers import (TG_SECRET, Tg, add_bare_driver, add_driver, appointments, link, make_database, offer,
                                  offer_status, ops, options, planner, q)


@pytest.fixture(scope="module")
def dsn():
    return make_database("setuhaul_build_dispatch2")


@pytest.fixture(scope="module")
def app(dsn, keys):
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True, telegram_webhook_secret=TG_SECRET))


@pytest.fixture
def client(app):
    return TestClient(app)


def lead(keys, fac=JAI):
    return H(staff_token(keys, groups=("ops_lead",), facilities=(fac,)))


def confirmed_booking(client, keys, dsn, d=None):
    """A shipment with a CONFIRMED appointment through the real path: offer -> accept -> planner confirms."""
    d = d or add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    assert "planner" in tg.press(offer(client, keys, d)["offer_id"], "A")
    (appt, _), = appointments(dsn, d["sid"])
    r = client.post(f"/console/requests/{appt}/decision", headers=planner(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    return d, tg, appt


def linked_new_driver(client, keys, dsn):
    """A second driver, linked to Telegram (linking needs a live shipment in the dispatcher's facility)."""
    tmp = add_driver(dsn, eta_at=datetime.now(timezone.utc) + timedelta(days=3))
    tg = link(client, keys, dsn, tmp["drv"])
    return tmp["drv"], tg


def reassign(client, keys, d, new, key=None, expect=200, headers=None):
    r = client.post("/console/dispatch/reassignments", headers=headers or ops(keys),
                    json={"shipment_id": d["sid"], "driver_id": new, "idempotency_key": key or uuid.uuid4().hex})
    assert r.status_code == expect, r.text
    return r.json()


def occupancy(dsn, appt):
    return q(dsn, """SELECT x.active, x.kind FROM dock_occupancy x JOIN appointments a ON a.occupancy_id = x.occupancy_id
                      WHERE a.appointment_id=%s""", appt)[0]


# ================================================================ confirmed reassignment
def test_confirmed_reassignment_switches_only_the_driver_on_the_existing_appointment(client, dsn, keys):
    d, told, appt = confirmed_booking(client, keys, dsn)
    new, tnew = linked_new_driver(client, keys, dsn)
    occ_before = q(dsn, "SELECT count(*) FROM dock_occupancy WHERE shipment_id=%s", d["sid"])[0][0]
    o = reassign(client, keys, d, new)
    assert o["kind"] == "CONFIRMED_REASSIGNMENT" and o["appointment_id"] == appt and o["previous_driver_id"] == d["drv"]
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == d["drv"]   # unchanged yet
    msg = q(dsn, "SELECT body_masked FROM chat_messages WHERE message_id=%s", o["read_back_message_id"])[0][0]
    assert "Existing appointment: dock" in msg
    assert "now assigned to you" in tnew.press(o["offer_id"], "A")
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == new
    assert appointments(dsn, d["sid"]) == [(appt, "CONFIRMED")]                     # the same appointment
    assert occupancy(dsn, appt) == (True, "APPOINTMENT")                              # capacity untouched
    assert q(dsn, "SELECT count(*) FROM dock_occupancy WHERE shipment_id=%s", d["sid"])[0][0] == occ_before
    assert q(dsn, "SELECT state, appointment_id FROM thread_flow WHERE thread_id=%s", o["thread_id"])[0] == ("BOOKED", appt)
    old_notice = q(dsn, """SELECT m.body_masked FROM chat_messages m JOIN chat_threads t USING (thread_id)
                            WHERE t.driver_id=%s AND m.sender_type='AGENT' ORDER BY m.message_id DESC LIMIT 1""", d["drv"])
    assert "reassigned to another driver" in old_notice[0][0]
    ev = q(dsn, "SELECT after_state FROM booking_events WHERE action='SHIPMENT_REASSIGNED' AND shipment_id=%s", d["sid"])
    assert ev[0][0]["appointment_id"] == appt and ev[0][0]["acknowledged_time"]
    tnew.press(o["offer_id"], "A")                                                    # duplicate acceptance: nothing new
    assert q(dsn, "SELECT count(*) FROM booking_events WHERE action='SHIPMENT_REASSIGNED' AND shipment_id=%s",
             d["sid"])[0][0] == 1
    # the read-back can never be held or booked a second time
    assert q(dsn, "SELECT expires_at <= now() FROM shown_options WHERE option_id=%s", o["option_id"])[0][0]


def test_confirmed_reassignment_refuses_a_driver_with_an_overlapping_trip(client, dsn, keys):
    eta = datetime.now(timezone.utc) + timedelta(hours=30)
    d, _, appt = confirmed_booking(client, keys, dsn, add_driver(dsn, eta_at=eta))
    busy, _, _ = confirmed_booking(client, keys, dsn, add_driver(dsn, eta_at=eta))
    r = reassign(client, keys, d, busy["drv"], expect=409)
    assert r["error"] == "DRIVER_HAS_OVERLAPPING_TRIP"
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == d["drv"]


def test_confirmed_reassignment_decline_or_expiry_keeps_everything(client, dsn, keys):
    d, _, appt = confirmed_booking(client, keys, dsn)
    new, tnew = linked_new_driver(client, keys, dsn)
    o = reassign(client, keys, d, new)
    assert "declined" in tnew.press(o["offer_id"], "D")
    o2 = reassign(client, keys, d, new)
    q(dsn, "UPDATE shown_options SET expires_at=now()-interval '1 second' WHERE option_id=%s", o2["option_id"])
    q(dsn, "UPDATE dispatch_offers SET expires_at=now()-interval '1 second' WHERE offer_id=%s", o2["offer_id"])
    assert "expired" in tnew.press(o2["offer_id"], "A")
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == d["drv"]
    assert appointments(dsn, d["sid"]) == [(appt, "CONFIRMED")] and occupancy(dsn, appt) == (True, "APPOINTMENT")


def test_concurrent_reassignments_only_the_latest_can_be_accepted(client, dsn, keys):
    d, _, appt = confirmed_booking(client, keys, dsn)
    a, ta = linked_new_driver(client, keys, dsn)
    b, tb = linked_new_driver(client, keys, dsn)
    oa = reassign(client, keys, d, a)
    ob = reassign(client, keys, d, b)
    assert offer_status(dsn, oa["offer_id"]) == "SUPERSEDED"
    assert "replaced" in ta.press(oa["offer_id"], "A")
    assert "now assigned to you" in tb.press(ob["offer_id"], "A")
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == b


def test_initial_booking_workflow_is_refused_for_a_confirmed_shipment_and_vice_versa(client, dsn, keys):
    d = add_driver(dsn)
    new = add_bare_driver(dsn)
    assert reassign(client, keys, d, new, expect=409)["error"] == "NO_CONFIRMED_APPOINTMENT"
    d2, _, _ = confirmed_booking(client, keys, dsn)
    assert offer(client, keys, d2, driver=new, opt=options(client, keys, d2["sid"])[0], expect=409)["error"] == \
        "SHIPMENT_ALREADY_BOOKED"


# ================================================================ cancellation
def cancel_request(client, keys, appt, key=None, headers=None, expect=200):
    r = client.post(f"/console/appointments/{appt}/cancellation-requests", headers=headers or ops(keys),
                    json={"reason_code": "DRIVER_UNAVAILABLE", "note": "driver sick, agreed by phone",
                          "idempotency_key": key or uuid.uuid4().hex})
    assert r.status_code == expect, r.text
    return r.json()


def decide(client, keys, rid, decision, headers=None, expect=200):
    r = client.post(f"/console/cancellation-requests/{rid}/decision", headers=headers or planner(keys),
                    json={"decision": decision, "note": "checked"})
    assert r.status_code == expect, r.text
    return r.json()


def test_cancellation_approved_releases_capacity_in_the_same_transaction(client, dsn, keys):
    d, _, appt = confirmed_booking(client, keys, dsn)
    key = uuid.uuid4().hex
    c = cancel_request(client, keys, appt, key=key)
    assert c["status"] == "OPEN" and cancel_request(client, keys, appt, key=key)["replayed"] is True
    assert cancel_request(client, keys, appt, expect=409)["error"] == "CANCELLATION_ALREADY_PENDING"
    assert occupancy(dsn, appt) == (True, "APPOINTMENT")                          # pending: capacity still reserved
    assert decide(client, keys, c["request_id"], "APPROVE", headers=ops(keys), expect=403)["error"] in (
        "FORBIDDEN", "ROLE_NOT_ALLOWED")                                            # the dispatcher role cannot decide
    r = decide(client, keys, c["request_id"], "APPROVE")
    assert r["status"] == "APPROVED"
    assert q(dsn, "SELECT status FROM appointments WHERE appointment_id=%s", appt)[0][0] == "CANCELLED"
    assert occupancy(dsn, appt) == (False, "APPOINTMENT")
    assert decide(client, keys, c["request_id"], "APPROVE", expect=409)["error"] == "ALREADY_DECIDED"
    assert cancel_request(client, keys, appt, expect=409)["error"] == "NOT_CANCELLABLE"
    note = q(dsn, """SELECT m.body_masked FROM chat_messages m JOIN chat_threads t USING (thread_id)
                     WHERE t.driver_id=%s AND m.sender_type='AGENT' ORDER BY m.message_id DESC LIMIT 1""", d["drv"])[0][0]
    assert "has been cancelled by the warehouse" in note
    assert q(dsn, "SELECT count(*) FROM booking_events WHERE action='APPOINTMENT_CANCELLED' AND shipment_id=%s",
             d["sid"])[0][0] == 1


def test_cancellation_rejected_keeps_the_appointment_and_capacity(client, dsn, keys):
    d, _, appt = confirmed_booking(client, keys, dsn)
    c = cancel_request(client, keys, appt)
    assert decide(client, keys, c["request_id"], "REJECT")["status"] == "REJECTED"
    assert appointments(dsn, d["sid"]) == [(appt, "CONFIRMED")] and occupancy(dsn, appt) == (True, "APPOINTMENT")


def test_requester_cannot_decide_own_cancellation_and_drivers_cannot_cancel(client, dsn, keys):
    d, _, appt = confirmed_booking(client, keys, dsn)
    tok = staff_token(keys, groups=("ops_lead",), facilities=(JAI,))
    r = client.post(f"/console/appointments/{appt}/cancellation-requests", headers=H(tok),
                    json={"reason_code": "OPERATIONS_DECISION", "note": "dock rework", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200
    r = client.post(f"/console/cancellation-requests/{r.json()['request_id']}/decision", headers=H(tok),
                    json={"decision": "APPROVE"})
    assert r.status_code == 403 and appointments(dsn, d["sid"]) == [(appt, "CONFIRMED")]
    Driver(client, d["phone"]).say("cancel my booking")                           # a driver message cancels nothing
    assert appointments(dsn, d["sid"]) == [(appt, "CONFIRMED")]


def test_cancellation_and_reassignment_exclude_each_other(client, dsn, keys):
    d, _, appt = confirmed_booking(client, keys, dsn)
    new, tnew = linked_new_driver(client, keys, dsn)
    o = reassign(client, keys, d, new)
    assert cancel_request(client, keys, appt, expect=409)["error"] == "REASSIGNMENT_IN_PROGRESS"
    tnew.press(o["offer_id"], "D")
    c = cancel_request(client, keys, appt)
    assert reassign(client, keys, d, new, expect=409)["error"] == "CANCELLATION_PENDING"
    # the appointment changes after the request (simulated): approval is refused, nothing is released
    q(dsn, "UPDATE cancellation_requests SET during = tstzrange(lower(during) + interval '1 hour', "
           "upper(during) + interval '1 hour') WHERE request_id=%s", c["request_id"])
    assert decide(client, keys, c["request_id"], "APPROVE", expect=409)["error"] == "APPOINTMENT_CHANGED"
    assert occupancy(dsn, appt) == (True, "APPOINTMENT")


def test_concurrent_cancellation_decisions_release_once(client, dsn, keys, app):
    d, _, appt = confirmed_booking(client, keys, dsn)
    c = cancel_request(client, keys, appt)
    barrier, codes = threading.Barrier(2), []

    def go(h):
        cl = TestClient(app)
        barrier.wait()
        codes.append(cl.post(f"/console/cancellation-requests/{c['request_id']}/decision", headers=h,
                             json={"decision": "APPROVE"}).status_code)

    ts = [threading.Thread(target=go, args=(x,)) for x in (planner(keys), lead(keys))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 409], codes
    assert q(dsn, "SELECT count(*) FROM booking_events WHERE action='APPOINTMENT_CANCELLED' AND shipment_id=%s",
             d["sid"])[0][0] == 1


# ================================================================ Telegram outbound through the existing relay
def relay(dsn, tg=None, provider=None):
    return OutboundRelay(Db(dsn), provider or LocalProvider(), telegram=tg)


def notif(dsn, message_id):
    return q(dsn, "SELECT status, attempts, provider_message_id FROM notifications WHERE chat_message_id=%s", message_id)


def test_offer_to_a_linked_driver_goes_to_telegram_with_buttons_and_unlinked_stays_on_the_old_route(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    plain = add_driver(dsn)
    o2 = offer(client, keys, plain)
    sink, wa = TelegramTestSink(), LocalProvider()
    relay(dsn, sink, wa).run_once(limit=200)
    sent = [x for x in sink.sent if x["chat"] == str(tg.uid)]
    assert len(sent) == 1 and "Booking offer" in sent[0]["text"]
    kb = sent[0]["reply_markup"]["inline_keyboard"]
    assert [b["callback_data"] for row in kb for b in row] == [f"o:{o['offer_id']}:A", f"o:{o['offer_id']}:D",
                                                                f"o:{o['offer_id']}:R"]
    assert notif(dsn, o["read_back_message_id"])[0][0] == "SENT"                      # handed over, not "delivered"
    assert any(to == plain["phone"] for to, *_ in wa.delivered)                        # unlinked: unchanged route
    assert notif(dsn, o2["read_back_message_id"])[0][0] == "SENT"
    listed = client.get("/console/dispatch/offers", headers=ops(keys)).json()["offers"]
    assert next(x for x in listed if x["offer_id"] == o["offer_id"])["delivery_state"] == "SENT"
    assert next(x for x in listed if x["offer_id"] == o["offer_id"])["status"] == "OFFERED"   # business state apart


def test_telegram_retry_and_unknown_keep_the_relay_semantics(client, dsn, keys):
    d = add_driver(dsn)
    link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    relay(dsn, TelegramTestSink(script=["retry"])).run_once(limit=200)
    st, attempts, _ = notif(dsn, o["read_back_message_id"])[0]
    assert st == "RETRY" and attempts == 1
    q(dsn, "UPDATE notifications SET next_retry_at = now() WHERE chat_message_id=%s", o["read_back_message_id"])
    relay(dsn, TelegramTestSink(script=["timeout"])).run_once(limit=200)
    assert notif(dsn, o["read_back_message_id"])[0][0] == "UNKNOWN"                   # a timeout is not a failure
    ops_list = client.get("/console/ops/notifications", params={"state": "UNKNOWN"}, headers=ops(keys)).json()
    row = next(x for x in ops_list["notifications"] if x["driver_id"] == d["drv"])
    assert row["route_now"] == "TELEGRAM" and row["attempts"] == 2 and row["unknown_deadline"]


def test_location_button_is_a_one_time_reply_keyboard_after_a_delay_report(client, dsn, keys, app):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    wa = Driver(client, d["phone"])
    wa.say("Running 90 min late")
    eta = (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()
    assert wa.tool("record_driver_report", message_id=wa.last_msg, declared_eta=eta, eta_precision="PRECISE",
                   delay_reason="TRAFFIC")["status"] == "OK"
    from setuhaul.api.security import SessionTokens
    p = SessionTokens(SESSION_SECRET).verify(wa.session)
    svc = app.state.service
    svc.record_reply(p, "Noted: about 90 minutes late.", reply_to=wa.last_msg, generation=0)
    sink = TelegramTestSink()
    relay(dsn, sink).run_once(limit=200)
    mine = [x for x in sink.sent if x["chat"] == str(tg.uid)]
    kb = mine[-1]["reply_markup"]
    assert kb["keyboard"][0][0] == {"text": "Share location once", "request_location": True} and kb["one_time_keyboard"]
    wa.say("ok")
    svc.record_reply(p, "Thanks.", reply_to=wa.last_msg, generation=0)
    sink2 = TelegramTestSink()
    relay(dsn, sink2).run_once(limit=200)
    assert all(x["reply_markup"] is None for x in sink2.sent if x["chat"] == str(tg.uid))   # asked once only
    assert "delay report still stands" in tg.text("Not now")
    assert q(dsn, "SELECT assessment, shared FROM driver_location_reports WHERE driver_id=%s", d["drv"]) == \
        [("UNAVAILABLE", False)]


def test_buttons_cleared_typed_answers_and_ambiguous_prompt(client, dsn, keys, app):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    assert "not sure whether that is a yes" in tg.text("haan lekin D3")
    assert offer_status(dsn, o["offer_id"]) == "OFFERED"                               # never guessed
    assert "with the warehouse planner" in tg.text("haan ji")                         # a typed yes = Accept
    assert offer_status(dsn, o["offer_id"]) == "ACCEPTED" and len(appointments(dsn, d["sid"])) == 1
    tg.press(o["offer_id"], "A")
    assert (str(tg.uid), 1) in app.state.telegram.cleared                             # the answered offer loses buttons
    other = add_driver(dsn)
    link(client, keys, dsn, other["drv"])
    before = len(app.state.telegram.cleared)
    tg.press(offer(client, keys, other)["offer_id"], "A")                              # not theirs: nothing cleared
    assert len(app.state.telegram.cleared) == before


# ================================================================ link-code throttling
def test_link_codes_are_throttled_per_account_and_redemption_is_serialized(client, dsn, keys, app):
    d = add_driver(dsn)
    code = client.post(f"/console/drivers/{d['drv']}/telegram-link-code", headers=ops(keys)).json()["code"]
    attacker = Tg(client)
    for _ in range(5):
        assert "did not work" in attacker.text("/start ABCDEFGHJK")
    assert "Too many attempts" in attacker.text(f"/start {code}")                     # even the right code, for now
    assert q(dsn, "SELECT count(*) FROM driver_channel_links WHERE driver_id=%s", d["drv"])[0][0] == 0
    assert "linked to SetuHaul" in Tg(client).text(f"/start {code}")                 # another account is not locked
    rows = q(dsn, "SELECT outcome FROM driver_link_attempts WHERE external_user_id=%s ORDER BY attempt_id",
             str(attacker.uid))
    assert [r[0] for r in rows] == ["INVALID"] * 5 + ["THROTTLED"]
    q(dsn, "UPDATE driver_link_attempts SET at = at - interval '16 minutes' WHERE external_user_id=%s", str(attacker.uid))
    assert "did not work" in attacker.text("/start ABCDEFGHJK")                       # the lockout expires
    # two accounts race for one code: exactly one link
    d2 = add_driver(dsn)
    code2 = client.post(f"/console/drivers/{d2['drv']}/telegram-link-code", headers=ops(keys)).json()["code"]
    barrier, replies = threading.Barrier(2), []

    def go(t):
        t.c = TestClient(app)
        barrier.wait()
        replies.append(t.text(f"/start {code2}"))

    ts = [threading.Thread(target=go, args=(Tg(client),)) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert q(dsn, "SELECT count(*) FROM driver_channel_links WHERE driver_id=%s AND revoked_at IS NULL", d2["drv"])[0][0] == 1
    # revoke and relink
    r = client.post(f"/console/drivers/{d2['drv']}/telegram-link/revoke", headers=ops(keys), json={"reason": "new phone"})
    assert r.status_code == 200 and r.json()["revoked"] == 1
    code3 = client.post(f"/console/drivers/{d2['drv']}/telegram-link-code", headers=ops(keys)).json()["code"]
    assert "linked to SetuHaul" in Tg(client).text(f"/start {code3}")
    board = client.get("/console/dispatch/shipments", headers=ops(keys)).json()["shipments"]
    row = next(x for x in board if x["shipment_id"] == d2["sid"])
    assert row["telegram"]["linked"] is True and "chat" not in str(row["telegram"])


# ================================================================ planner schedule and operations health
def test_schedule_shows_claims_units_and_a_stale_approval_is_refused(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    tg.press(offer(client, keys, d)["offer_id"], "A")
    (appt, _), = appointments(dsn, d["sid"])
    s = client.get(f"/console/facilities/{JAI}/schedule", headers=planner(keys)).json()
    assert s["capacity"]["unit"] == "dock-minutes" and s["as_of"] and s["capacity"]["total"] > 0
    p = next(x for x in s["pending"] if x["appointment_id"] == appt)
    assert p["capacity_claimed"] and p["source"] == "DISPATCHER_OFFER" and p["compatibility"] == ["compatible"]
    assert p["approval_kind"] == "PLANNER_REVIEW" and p["history"]
    assert any(i["kind"] == "PENDING_DECISION" and i["appointment_id"] == appt for i in s["items"])
    # the request's decision window passes before the planner acts: no false success, nothing confirmed
    q(dsn, "ALTER TABLE approval_requests DISABLE TRIGGER USER")
    try:
        q(dsn, "UPDATE approval_requests SET expires_at = now() - interval '1 second' WHERE appointment_id=%s", appt)
    finally:
        q(dsn, "ALTER TABLE approval_requests ENABLE TRIGGER USER")
    r = client.post(f"/console/requests/{appt}/decision", headers=planner(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 409 and r.json()["error"] in ("PENDING_EXPIRED", "ALREADY_DECIDED"), r.text
    assert q(dsn, "SELECT status FROM appointments WHERE appointment_id=%s", appt)[0][0] != "CONFIRMED"


def test_ops_views_and_escalation_actions_are_separate(client, dsn, keys):
    d = add_driver(dsn)
    eid = uuid.uuid4()
    q(dsn, """INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, handoff, ack_due_at)
              VALUES (%s,'NOTIFICATION_FAILED','DECIDE','FAC-JAI-01',%s,'{}',now()+interval '10 minutes')""",
      eid, d["sid"])
    r = client.post(f"/console/escalations/{eid}/acknowledge", headers=ops(keys), json={"note": "calling driver"})
    assert r.status_code == 200 and r.json()["status"] == "ACKNOWLEDGED"
    assert q(dsn, "SELECT status, resolved_at FROM escalations WHERE escalation_id=%s", eid)[0] == ("ACKNOWLEDGED", None)
    assert client.post(f"/console/escalations/{eid}/acknowledge", headers=ops(keys), json={}).status_code == 409
    assert client.post(f"/console/escalations/{eid}/resolve", headers=ops(keys), json={"resolution": "x"}).status_code == 422
    r = client.post(f"/console/escalations/{eid}/resolve", headers=ops(keys), json={"resolution": "driver reached by phone"})
    assert r.status_code == 200 and r.json()["status"] == "RESOLVED"
    h = client.get("/console/ops/health", headers=ops(keys)).json()
    assert h["as_of"] and isinstance(h["escalations_open"], int)
    audit = client.get("/console/ops/audit", params={"shipment_id": d["sid"]}, headers=ops(keys)).json()["events"]
    assert [e["action"] for e in audit][:2] == ["ESCALATION_RESOLVED", "ESCALATION_ACKNOWLEDGED"]
    for path in ("/console/ops/health", "/console/ops/notifications", "/console/ops/audit"):
        assert client.get(path, headers=planner(keys)).status_code == 403             # operations views only
    assert client.get(f"/console/facilities/{JAI}/schedule", headers=ops(keys, GGN)).status_code == 403
