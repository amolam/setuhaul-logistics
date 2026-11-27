"""Workstream B (27 Nov fix plan, migration 0048): free drivers, the explicit offer target, a planner-approved new time
for a confirmed booking (pending requests protected from competing bookings, the move atomic on approval), and open
escalations where a planner decides.

Own database (setuhaul_build_dispatch0048). Nothing leaves the process (Telegram test sink only).
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from setuhaul.api.app import Settings, create_app
from test_api import CLIENT, GGN, ISSUER, JAI, META_SECRET, SESSION_SECRET, H, keys, staff_token  # noqa: F401
from test_dispatch_offers import TG_SECRET, add_driver, make_database, offer, options, q
from test_lifecycle_0048 import booked

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def dsn():
    return make_database("setuhaul_build_dispatch0048")


@pytest.fixture(scope="module")
def app(dsn, keys):
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True, telegram_webhook_secret=TG_SECRET))


@pytest.fixture
def client(app):
    return TestClient(app)


def who(keys, *groups, fac=JAI, sub=None):
    over = {"sub": sub} if sub else {}
    return H(staff_token(keys, groups=groups, facilities=(fac,), **over))


def future_confirmed(dsn, hours=6):
    """A CONFIRMED appointment well ahead (its own dock, so the seeded docks D1/D2 stay free for the new time)."""
    start = datetime.now(IST).replace(minute=0, second=0, microsecond=0) + timedelta(hours=hours)
    return booked(dsn, start)


def state(dsn, appt):
    return q(dsn, """SELECT a.status, a.is_current, x.active FROM appointments a
                       JOIN dock_occupancy x ON x.occupancy_id = a.occupancy_id WHERE a.appointment_id=%s""", appt)[0]


def reschedule(client, keys, b, opt=None, key=None, headers=None, expect=200):
    o = opt or client.get(f"/console/appointments/{b['appt']}/reschedule-options", headers=who(keys, "dispatcher")).json()["options"][0]
    r = client.post(f"/console/appointments/{b['appt']}/reschedule-requests", headers=headers or who(keys, "dispatcher"),
                    json={"dock_code": o["dock_code"], "start": o["start"], "note": "driver asked for later",
                          "idempotency_key": key or uuid.uuid4().hex})
    assert r.status_code == expect, r.text
    return r.json(), o


# ================================================================ issue 6: a new time for a confirmed booking
def test_reschedule_goes_to_a_planner_and_keeps_the_booking_until_approved(client, dsn, keys):
    b = future_confirmed(dsn)
    opts = client.get(f"/console/appointments/{b['appt']}/reschedule-options", headers=who(keys, "dispatcher"))
    assert opts.status_code == 200 and opts.json()["options"], opts.text
    out, o = reschedule(client, keys, b)
    assert out["status"] == "PENDING" and out["kind"] == "PLANNER_REVIEW" and out["replaces"] == b["appt"]
    assert state(dsn, b["appt"]) == ("CONFIRMED", True, True)                # the original is untouched
    new = out["appointment_id"]
    assert state(dsn, new) == ("PENDING_CONFIRMATION", False, True)          # the new slot is claimed
    req = [r for r in client.get("/console/requests", headers=who(keys, "planner")).json()["requests"]
           if r["appointment_id"] == new]
    assert req and req[0]["kind"] == "PLANNER_REVIEW" and req[0]["replaces"] == b["appt"]
    # the planner approves: one transaction moves it
    r = client.post(f"/console/requests/{new}/decision", headers=who(keys, "planner"),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["replaced"] == b["appt"], r.text
    assert state(dsn, new) == ("CONFIRMED", True, True)
    assert state(dsn, b["appt"]) == ("CANCELLED", False, False)
    acts = [x[0] for x in q(dsn, "SELECT action FROM booking_events WHERE shipment_id=%s ORDER BY event_id", b["sid"])]
    assert acts[-2:] == ["RESCHEDULE_REQUESTED", "APPROVAL_APPROVED"]


def test_rejected_reschedule_releases_the_new_slot_and_keeps_the_booking(client, dsn, keys):
    b = future_confirmed(dsn)
    out, _ = reschedule(client, keys, b)
    r = client.post(f"/console/requests/{out['appointment_id']}/decision", headers=who(keys, "planner"),
                    json={"decision": "REJECT", "idempotency_key": uuid.uuid4().hex, "note": "keep it"})
    assert r.status_code == 200, r.text
    assert state(dsn, out["appointment_id"]) == ("REJECTED", False, False)
    assert state(dsn, b["appt"]) == ("CONFIRMED", True, True)


def test_a_pending_reschedule_cannot_lose_its_slot_to_a_competing_booking(client, dsn, keys):
    b = future_confirmed(dsn)
    out, o = reschedule(client, keys, b)
    other = add_driver(dsn)
    dock = q(dsn, "SELECT dock_id FROM docks WHERE facility_id=%s AND dock_code=%s", JAI, o["dock_code"])[0][0]
    with pytest.raises(psycopg.errors.ExclusionViolation):                 # the EXCLUDE constraint, whoever asks
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,expires_at,created_by)
                         VALUES (%s,%s,tstzrange(%s,%s,'[)'),'HOLD',%s,'x',now()+interval '3 minutes','test')""",
                      (dock, JAI, o["start"], o["end"], other["sid"]))
    # ... and the feasibility engine no longer offers it to anyone else
    assert all(not (x["dock_code"] == o["dock_code"] and x["start"] == o["start"])
               for x in client.get(f"/console/dispatch/shipments/{other['sid']}/options",
                                   headers=who(keys, "dispatcher")).json()["options"])
    assert state(dsn, out["appointment_id"])[2] is True


def test_reschedule_is_idempotent_and_one_at_a_time(client, dsn, keys):
    b = future_confirmed(dsn)
    key = uuid.uuid4().hex
    me = who(keys, "dispatcher", sub="user-retrying-dispatcher")         # a retry by the same person replays
    first, o = reschedule(client, keys, b, key=key, headers=me)
    again, _ = reschedule(client, keys, b, opt=o, key=key, headers=me)
    stranger, _ = reschedule(client, keys, b, opt=o, key=key, expect=409)  # the same key from someone else: refused
    assert stranger["error"] == "IDEMPOTENCY_KEY_REUSED"
    assert again["appointment_id"] == first["appointment_id"] and again["replayed"] is True
    assert q(dsn, "SELECT count(*) FROM approval_requests WHERE shipment_id=%s", b["sid"])[0][0] == 1
    # a second, different request while one is pending is refused
    opts = client.get(f"/console/appointments/{b['appt']}/reschedule-options", headers=who(keys, "dispatcher")).json()["options"]
    alt = next(x for x in opts if (x["dock_code"], x["start"]) != (o["dock_code"], o["start"]))
    r, _ = reschedule(client, keys, b, opt=alt, expect=409)
    assert r["error"] in ("TIME_CHANGE_PENDING", "OPTION_NOT_FEASIBLE")


def test_two_dispatchers_racing_produce_one_request(client, dsn, keys):
    b = future_confirmed(dsn)
    opts = client.get(f"/console/appointments/{b['appt']}/reschedule-options", headers=who(keys, "dispatcher")).json()["options"]
    assert len(opts) >= 2
    results, barrier = [], threading.Barrier(2)

    def go(o):
        barrier.wait()
        r = client.post(f"/console/appointments/{b['appt']}/reschedule-requests", headers=who(keys, "dispatcher"),
                        json={"dock_code": o["dock_code"], "start": o["start"], "note": "race",
                              "idempotency_key": uuid.uuid4().hex})
        results.append(r.status_code)
    ts = [threading.Thread(target=go, args=(o,)) for o in opts[:2]]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(results)[0] == 200 and results.count(200) == 1, results
    assert q(dsn, "SELECT count(*) FROM approval_requests WHERE shipment_id=%s AND status='OPEN'", b["sid"])[0][0] == 1


def test_the_requester_never_approves_their_own_time_change(client, dsn, keys):
    b = future_confirmed(dsn)
    me = who(keys, "ops_lead", sub="user-same-lead")
    out, _ = reschedule(client, keys, b, headers=me)
    r = client.post(f"/console/requests/{out['appointment_id']}/decision", headers=who(keys, "ops_lead", sub="user-same-lead"),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403 and "requested a time change" in r.json()["detail"], r.text
    assert state(dsn, b["appt"]) == ("CONFIRMED", True, True)
    r = client.post(f"/console/requests/{out['appointment_id']}/decision", headers=who(keys, "planner"),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200


@pytest.mark.parametrize("groups,code", [(("planner",), 403), (("dispatcher",), 200), (("operations",), 200)])
def test_who_may_ask_for_a_new_time(client, dsn, keys, groups, code):
    b = future_confirmed(dsn)
    reschedule(client, keys, b, headers=who(keys, *groups), expect=code)


def test_a_dispatcher_cannot_approve_and_the_database_agrees(client, dsn, keys):
    b = future_confirmed(dsn)
    out, _ = reschedule(client, keys, b)
    r = client.post(f"/console/requests/{out['appointment_id']}/decision", headers=who(keys, "dispatcher"),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403
    from setuhaul.api.db import Db
    with pytest.raises(psycopg.Error, match="ROLE_NOT_ALLOWED"):
        with Db(dsn).tx("console", (JAI,), staff=("staff:op", frozenset({"operations"}), (JAI,))) as c:
            c.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,NULL)", (out["appointment_id"], uuid.uuid4().hex))


def test_reschedule_refused_when_started_arrived_or_killed(client, dsn, keys):
    started = booked(dsn, datetime.now(timezone.utc) - timedelta(minutes=5))
    r = client.get(f"/console/appointments/{started['appt']}/reschedule-options", headers=who(keys, "dispatcher"))
    o = (r.json().get("options") or [None])[0] if r.status_code == 200 else None
    if o:
        r2, _ = reschedule(client, keys, started, opt=o, expect=409)
        assert r2["error"] == "APPOINTMENT_STARTED"
    b = future_confirmed(dsn)
    lead = who(keys, "ops_lead")
    assert client.post("/console/booking-kill-switch", headers=lead, json={"engaged": True, "reason": "drill now"}).status_code == 200
    try:
        r, _ = reschedule(client, keys, b, expect=409)
        assert r["error"] == "BOOKING_KILLED"
        assert state(dsn, b["appt"]) == ("CONFIRMED", True, True)
    finally:
        client.post("/console/booking-kill-switch", headers=lead, json={"engaged": False, "reason": "drill over"})


# ================================================================ issues 4 and 5: free drivers, explicit target
def test_free_drivers_exclude_busy_ones_and_never_the_assigned(client, dsn, keys):
    d = add_driver(dsn)
    o = options(client, keys, d["sid"])[0]
    free = add_driver(dsn)["drv"]                                           # a driver with a trip far from this slot
    busy = booked(dsn, datetime.fromisoformat(o["start"]) + timedelta(minutes=30))   # overlapping live booking
    at_gate = add_driver(dsn, status="AT_GATE")["drv"]
    done = booked(dsn, datetime.fromisoformat(o["start"]))                  # same time but COMPLETED: free again
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("""UPDATE appointments SET status='COMPLETED', arrived_at=now(), docked_at=now(), completed_at=now()
                      WHERE appointment_id=%s""", (done["appt"],))
        c.execute("UPDATE shipments SET current_status='COMPLETED' WHERE shipment_id=%s", (done["sid"],))
    r = client.get(f"/console/dispatch/shipments/{d['sid']}/free-drivers", headers=who(keys, "dispatcher"),
                   params={"start": o["start"], "end": o["end"]})
    assert r.status_code == 200, r.text
    ids = {x["driver_id"] for x in r.json()["drivers"]}
    assert free in ids and done["drv"] in ids
    assert d["drv"] not in ids and busy["drv"] not in ids and at_gate not in ids
    assert client.get(f"/console/dispatch/shipments/{d['sid']}/free-drivers", headers=who(keys, "planner"),
                      params={"start": o["start"], "end": o["end"]}).status_code == 403


def _send(client, keys, d, driver, target, o, expect):
    r = client.post("/console/dispatch/offers", headers=who(keys, "dispatcher"),
                    json={"shipment_id": d["sid"], "driver_id": driver, "vehicle_id": d["veh"], "dock_code": o["dock_code"],
                          "start": o["start"], "idempotency_key": uuid.uuid4().hex, "target": target})
    assert r.status_code == expect, r.text
    return r.json()


def test_another_driver_can_never_be_the_assigned_driver(client, dsn, keys):
    d = add_driver(dsn)
    o = options(client, keys, d["sid"])[0]
    assert _send(client, keys, d, d["drv"], "OTHER", o, 409)["error"] == "SAME_DRIVER"
    other = add_driver(dsn)["drv"]
    assert _send(client, keys, d, other, "ASSIGNED", o, 409)["error"] == "TARGET_MISMATCH"
    from setuhaul.api.db import Db                                       # the database refuses it as well
    with pytest.raises(psycopg.Error, match="SAME_DRIVER"):
        with Db(dsn).tx("console", (JAI,), staff=("staff:dp", frozenset({"dispatcher"}), (JAI,))) as c:
            c.execute("SELECT create_dispatch_offer_to('OTHER',%s,%s,%s,'DOCK-JAI-D1',%s,%s,false,0,300,'offer text here',%s,'staff:dp')",
                      (d["sid"], d["drv"], d["veh"], o["start"], o["end"], uuid.uuid4().hex))
    assert q(dsn, "SELECT count(*) FROM dispatch_offers WHERE shipment_id=%s", d["sid"])[0][0] == 0
    mine = _send(client, keys, d, d["drv"], "ASSIGNED", o, 200)
    assert mine["kind"] == "INITIAL_BOOKING" and mine["target"] == "ASSIGNED"
    theirs = _send(client, keys, d, other, "OTHER", o, 200)
    assert theirs["kind"] == "REASSIGNMENT" and theirs["driver_id"] == other
    # the assignment stays with the original driver until the new driver accepts
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == d["drv"]
    # one live offer per shipment: the first one was superseded
    assert q(dsn, "SELECT status FROM dispatch_offers WHERE offer_id=%s", mine["offer_id"])[0][0] == "SUPERSEDED"
    # a driver with a live offer for another shipment is not offered as free
    e = add_driver(dsn)
    eo = options(client, keys, e["sid"])[0]
    ids = {x["driver_id"] for x in client.get(f"/console/dispatch/shipments/{e['sid']}/free-drivers", headers=who(keys, "dispatcher"),
                                              params={"start": eo["start"], "end": eo["end"]}).json()["drivers"]}
    assert other not in ids


def test_offer_without_target_still_behaves_as_before(client, dsn, keys):
    d = add_driver(dsn)
    r = offer(client, keys, d)
    assert r["kind"] == "INITIAL_BOOKING" and "target" not in r


# ================================================================ issue 7: escalations where a person decides
def test_open_safety_escalation_is_visible_to_dispatch_and_the_planner(client, dsn, keys):
    d = add_driver(dsn)
    eid = uuid.uuid4()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("""INSERT INTO setuhaul.escalations (escalation_id,reason_code,mode,facility_id,shipment_id,handoff,ack_due_at,created_at)
                     VALUES (%s,'SAFETY','TAKE_OVER',%s,%s,'{}',now() - interval '5 minutes',now() - interval '15 minutes')""",
                  (eid, JAI, d["sid"]))
    s = {x["shipment_id"]: x for x in client.get("/console/dispatch/shipments", headers=who(keys, "dispatcher")).json()["shipments"]}[d["sid"]]
    assert s["open_safety_escalation"] is True and s["open_escalations"] == 1
    sched = client.get(f"/console/facilities/{JAI}/schedule", headers=who(keys, "planner")).json()
    e = next(x for x in sched["escalations_needing_decision"] if x["escalation_id"] == str(eid))
    assert e["overdue"] is True and e["banner"].startswith("Open safety escalation")
    esc = next(x for x in client.get("/console/escalations", headers=who(keys, "planner")).json()["escalations"]
               if str(x["escalation_id"]) == str(eid))
    assert esc["overdue"] is True
    r = client.post(f"/console/escalations/{eid}/resolve", headers=who(keys, "planner"), json={"resolution": "driver is safe, resolved"})
    assert r.status_code == 200
    s = {x["shipment_id"]: x for x in client.get("/console/dispatch/shipments", headers=who(keys, "dispatcher")).json()["shipments"]}[d["sid"]]
    assert s["open_safety_escalation"] is False
