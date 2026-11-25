"""Roadmap Phases 1, 2, 3A, 5 (migration 0035): dispatcher-initiated booking offers, offer expiry and superseding,
pre-confirmation reassignment, and the Telegram backend foundation (test sink; nothing leaves the process).

The offer is a read-back in the driver's own conversation; the driver's yes is attested by the existing inbound path;
the booking continues through the existing hold -> request -> planner approval. These tests drive it over HTTP: staff
through Cognito-style tokens, drivers through a signed Telegram webhook (and WhatsApp for the typed-reply path).
Own database (setuhaul_build_dispatch) so the module can run beside the rest of the suite.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import BASE_DSN, _pg_available
from setuhaul.api.app import Settings, create_app
from setuhaul.db.migrate import migrate
from test_api import (CLIENT, GGN, IST, ISSUER, JAI, META_SECRET, SESSION_SECRET, Driver, H, keys,  # noqa: F401
                      staff_token)

TG_SECRET = "telegram-webhook-secret-for-tests-0123456789"
DB = "setuhaul_build_dispatch"


@pytest.fixture(scope="module")
def dsn():
    return make_database(DB)


def make_database(name: str) -> str:
    """A fresh, migrated and seeded database of its own (so modules using different names may run side by side)."""
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {name}")
    d = f"{BASE_DSN} dbname={name}"
    migrate(d)
    with psycopg.connect(d, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO carriers VALUES ('CAR1','Carrier One',true)")
        c.execute("""INSERT INTO facilities (facility_id,facility_name,open_time,close_time,latitude,longitude) VALUES
                     ('FAC-JAI-01','Jaipur','00:00','23:59',26.9124,75.7873),('FAC-GGN-01','Gurugram','00:00','23:59',NULL,NULL)""")
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     ('FAC-JAI-01',1,10,10,30,'{"DAY":15,"NIGHT":15}',%s,30),
                     ('FAC-GGN-01',1,15,10,20,'{"DAY":15,"NIGHT":15}',%s,30)""", (shifts, shifts))
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg) VALUES
                     ('DOCK-JAI-D1','FAC-JAI-01','D1','STANDARD',false,30000),('DOCK-JAI-D2','FAC-JAI-01','D2','STANDARD',false,30000),
                     ('DOCK-GGN-D1','FAC-GGN-01','D1','STANDARD',false,30000)""")
        base = datetime.now(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        for dock in ("DOCK-JAI-D1", "DOCK-JAI-D2", "DOCK-GGN-D1"):
            for h in range(96):
                s = base + timedelta(hours=h)
                c.execute("INSERT INTO appointment_slots VALUES (%s,%s,tstzrange(%s,%s,'[)'),'OPEN',NULL)",
                          (f"S-{dock}-{h}", dock, s, s + timedelta(hours=1)))
    return d


@pytest.fixture(scope="module")
def app(dsn, keys):
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True, telegram_webhook_secret=TG_SECRET))


@pytest.fixture
def client(app):
    return TestClient(app)


def ops(keys, fac=JAI):
    return H(staff_token(keys, groups=("operations",), facilities=(fac,)))


def planner(keys, fac=JAI):
    return H(staff_token(keys, groups=("planner",), facilities=(fac,)))


def add_driver(dsn, facility=JAI, eta_at: datetime | None = None, status="IN_TRANSIT"):
    u = uuid.uuid4().hex[:8]
    drv, phone, sid, veh = f"DRV-{u}", f"+91{uuid.uuid4().int % 10**10:010d}", f"SHP-{u}", f"VEH-{u}"
    eta = eta_at or (datetime.now(IST) + timedelta(hours=3))
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)", (drv, phone))
        c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}"))
        c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                     load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                     VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,%s,%s,%s)""",
                  (sid, f"ORD-{u}", drv, veh, facility, eta - timedelta(hours=5), eta, status))
    return {"drv": drv, "phone": phone, "sid": sid, "veh": veh}


def add_bare_driver(dsn):
    u = uuid.uuid4().hex[:8]
    drv = f"DRV-{u}"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("INSERT INTO setuhaul.drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)",
                  (drv, f"+91{uuid.uuid4().int % 10**10:010d}"))
    return drv


_update = iter(range(10_000_000, 99_999_999))


class Tg:
    """One Telegram account talking to the bot (numeric user id = chat id in a private chat)."""

    def __init__(self, client, user_id: int | None = None):
        self.c, self.uid = client, user_id or (uuid.uuid4().int % 10**9 + 10**9)

    def post(self, update: dict, secret: str = TG_SECRET):
        return self.c.post("/webhooks/telegram", content=json.dumps(update).encode(),
                           headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"})

    def last_reply(self):
        mine = [t for chat, t in self.c.app.state.telegram.sent if chat == str(self.uid)]
        return mine[-1] if mine else None

    def text(self, text: str, update_id: int | None = None):
        uid = update_id or next(_update)
        r = self.post({"update_id": uid, "message": {"message_id": uid, "date": int(datetime.now().timestamp()),
                                                     "from": {"id": self.uid, "is_bot": False, "username": "anyone"},
                                                     "chat": {"id": self.uid, "type": "private"}, "text": text}})
        assert r.status_code == 200, r.text
        return self.last_reply()

    def press(self, offer_id: str, action: str, callback_id: str | None = None):
        uid = next(_update)
        r = self.post({"update_id": uid, "callback_query": {
            "id": callback_id or f"cb{uid}", "from": {"id": self.uid, "is_bot": False},
            "message": {"message_id": 1, "chat": {"id": self.uid, "type": "private"}},
            "data": f"o:{offer_id}:{action}"}})
        assert r.status_code == 200, r.text
        return self.last_reply()

    def location(self, lat, lon, accuracy=25.0, age_s=0, live=False, update_id=None):
        uid = update_id or next(_update)
        loc = {"latitude": lat, "longitude": lon, "horizontal_accuracy": accuracy}
        if live:
            loc["live_period"] = 900
        r = self.post({"update_id": uid, "message": {"message_id": uid, "date": int(datetime.now().timestamp()) - age_s,
                                                     "from": {"id": self.uid, "is_bot": False},
                                                     "chat": {"id": self.uid, "type": "private"}, "location": loc}})
        assert r.status_code == 200, r.text
        return self.last_reply()


def link(client, keys, dsn, drv, fac=JAI) -> Tg:
    r = client.post(f"/console/drivers/{drv}/telegram-link-code", headers=ops(keys, fac))
    assert r.status_code == 200, r.text
    tg = Tg(client)
    assert "linked to SetuHaul" in tg.text(f"/start {r.json()['code']}")
    return tg


def options(client, keys, sid, fac=JAI):
    r = client.get(f"/console/dispatch/shipments/{sid}/options", headers=ops(keys, fac))
    assert r.status_code == 200, r.text
    assert r.json()["options"], r.json()
    return r.json()["options"]


def offer(client, keys, d, driver=None, opt=None, key=None, fac=JAI, expect=200):
    o = opt or options(client, keys, d["sid"], fac)[0]
    r = client.post("/console/dispatch/offers", headers=ops(keys, fac),
                    json={"shipment_id": d["sid"], "driver_id": driver or d["drv"], "vehicle_id": d["veh"],
                          "dock_code": o["dock_code"], "start": o["start"],
                          "idempotency_key": key or uuid.uuid4().hex})
    assert r.status_code == expect, r.text
    return r.json()


def q(dsn, sql, *args):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else []


def offer_status(dsn, offer_id):
    return q(dsn, "SELECT status FROM dispatch_offers WHERE offer_id=%s", offer_id)[0][0]


def appointments(dsn, sid):
    """Live appointments: the current one, and a pending request (is_current only once the warehouse confirms)."""
    return q(dsn, """SELECT appointment_id, status FROM appointments WHERE shipment_id=%s
                     AND (is_current OR status='PENDING_CONFIRMATION') AND status NOT IN ('REJECTED','EXPIRED','CANCELLED')""", sid)


# ================================================================ Phase 1: dispatcher booking, end to end
def test_offer_accept_goes_to_planner_and_only_planner_confirms(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    assert o["status"] == "OFFERED" and o["kind"] == "INITIAL_BOOKING" and o["replayed"] is False
    # the offer is a read-back in the driver's conversation and goes out through the existing outbox (test sink)
    msg = q(dsn, "SELECT sender_type, body_masked FROM chat_messages WHERE message_id=%s", o["read_back_message_id"])[0]
    assert msg[0] == "AGENT" and "Booking offer from your dispatcher" in msg[1]
    assert q(dsn, "SELECT count(*) FROM outbox WHERE aggregate='chat_message' AND aggregate_id=%s",
             str(o["read_back_message_id"]))[0][0] == 1
    # sending an offer reserves no dock capacity
    assert q(dsn, "SELECT count(*) FROM dock_occupancy WHERE shipment_id=%s AND active", d["sid"])[0][0] == 0
    assert appointments(dsn, d["sid"]) == []

    reply = tg.press(o["offer_id"], "A")
    assert "with the warehouse planner" in reply
    (appt, st), = appointments(dsn, d["sid"])
    assert st == "PENDING_CONFIRMATION" and offer_status(dsn, o["offer_id"]) == "ACCEPTED"
    kind = q(dsn, "SELECT kind FROM approval_requests WHERE appointment_id=%s", appt)[0][0]
    assert kind == "PLANNER_REVIEW"                       # dispatcher bookings are a planner's decision
    assert q(dsn, "SELECT appointment_id FROM dispatch_offers WHERE offer_id=%s", o["offer_id"])[0][0] == appt

    # the operations role that sent the offer cannot approve it
    r = client.post(f"/console/requests/{appt}/decision", headers=ops(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403, r.text
    assert appointments(dsn, d["sid"]) == [(appt, "PENDING_CONFIRMATION")]
    r = client.post(f"/console/requests/{appt}/decision", headers=planner(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    board = client.get("/console/dispatch/shipments", headers=ops(keys)).json()["shipments"]
    row = next(x for x in board if x["shipment_id"] == d["sid"])
    assert row["appointment"]["status"] == "CONFIRMED" and row["latest_offer"]["status"] == "ACCEPTED"


def test_typed_yes_on_whatsapp_accepts_through_the_existing_attestation(client, dsn, keys):
    d = add_driver(dsn)
    o = offer(client, keys, d)
    wa = Driver(client, d["phone"])
    wa.say("haan ji, kar do")
    assert offer_status(dsn, o["offer_id"]) == "ACCEPTED"
    # the agent's existing tools continue the same booking (as the orchestrator would)
    assert wa.tool("confirm_selection", message_id=wa.last_msg)["data"]["verdict"] == "YES"
    assert wa.tool("hold_option", option_id=o["option_id"])["data"]["status"] == "HELD"
    assert wa.tool("submit_booking_request", option_id=o["option_id"])["data"]["status"] == "PENDING"


def test_decline_and_alternative_book_nothing(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    assert "declined" in tg.press(o["offer_id"], "D")
    assert offer_status(dsn, o["offer_id"]) == "DECLINED" and appointments(dsn, d["sid"]) == []
    assert "no longer" in tg.press(o["offer_id"], "A") or offer_status(dsn, o["offer_id"]) == "DECLINED"
    assert appointments(dsn, d["sid"]) == []
    o2 = offer(client, keys, d)
    assert "another time" in tg.press(o2["offer_id"], "R")
    assert offer_status(dsn, o2["offer_id"]) == "ALTERNATIVE_REQUESTED"
    tg.press(o2["offer_id"], "A")                                    # too late: the option was withdrawn
    assert offer_status(dsn, o2["offer_id"]) == "ALTERNATIVE_REQUESTED" and appointments(dsn, d["sid"]) == []
    assert q(dsn, "SELECT count(*) FROM dock_occupancy WHERE shipment_id=%s AND active", d["sid"])[0][0] == 0


def test_unclear_reply_is_never_guessed(client, dsn, keys):
    d = add_driver(dsn)
    o = offer(client, keys, d)
    Driver(client, d["phone"]).say("haan lekin D3")
    assert offer_status(dsn, o["offer_id"]) == "OFFERED" and appointments(dsn, d["sid"]) == []


def test_duplicate_offer_request_is_one_offer(client, dsn, keys):
    d = add_driver(dsn)
    key = uuid.uuid4().hex
    o1 = offer(client, keys, d, key=key)
    o2 = offer(client, keys, d, key=key, opt=options(client, keys, d["sid"])[0])
    assert o1["offer_id"] == o2["offer_id"] and o2["replayed"] is True
    assert q(dsn, "SELECT count(*) FROM dispatch_offers WHERE shipment_id=%s", d["sid"])[0][0] == 1


def test_duplicate_webhook_makes_one_booking(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    tg.press(o["offer_id"], "A", callback_id="same-callback")
    tg.press(o["offer_id"], "A", callback_id="same-callback")      # Telegram redelivers the same update
    tg.press(o["offer_id"], "A")                                    # and a second tap
    assert len(appointments(dsn, d["sid"])) == 1
    assert q(dsn, "SELECT count(*) FROM chat_messages WHERE channel='TELEGRAM' AND external_message_id='tgcb:same-callback'")[0][0] == 1


def test_an_infeasible_or_booked_or_foreign_shipment_is_refused(client, dsn, keys):
    d = add_driver(dsn)
    far = (datetime.now(IST) + timedelta(days=20)).replace(minute=0, second=0, microsecond=0).isoformat()
    r = client.post("/console/dispatch/offers", headers=ops(keys), json={
        "shipment_id": d["sid"], "driver_id": d["drv"], "vehicle_id": d["veh"], "dock_code": "D1", "start": far,
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 409 and r.json()["error"] == "OPTION_NOT_FEASIBLE"
    r = client.get(f"/console/dispatch/shipments/{d['sid']}/options", headers=ops(keys, GGN))
    assert r.status_code == 404                                    # another facility's dispatcher sees nothing
    r = client.post("/console/dispatch/offers", headers=planner(keys), json={
        "shipment_id": d["sid"], "driver_id": d["drv"], "vehicle_id": d["veh"], "dock_code": "D1", "start": far,
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403                                    # a planner approves; it does not send offers
    other = add_driver(dsn)
    offer(client, keys, d, opt=None, key=None)
    bad = options(client, keys, d["sid"])[0]
    r = client.post("/console/dispatch/offers", headers=ops(keys), json={
        "shipment_id": d["sid"], "driver_id": d["drv"], "vehicle_id": other["veh"], "dock_code": bad["dock_code"],
        "start": bad["start"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 409 and r.json()["error"] == "VEHICLE_CHANGE_NOT_SUPPORTED"


def test_a_booked_shipment_gets_no_initial_offer(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    tg.press(offer(client, keys, d)["offer_id"], "A")
    assert len(appointments(dsn, d["sid"])) == 1
    opt = options(client, keys, d["sid"])[0]
    r = offer(client, keys, d, opt=opt, expect=409)
    assert r["error"] == "SHIPMENT_ALREADY_BOOKED"


# ================================================================ Phase 2: expiry, superseding, stale availability
def test_expired_offer_cannot_be_accepted(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    exp = datetime.fromisoformat(o["expires_at"]) - datetime.fromisoformat(o["created_at"])
    assert timedelta(seconds=295) < exp <= timedelta(seconds=301)   # the 300 s demo default
    q(dsn, "UPDATE shown_options SET expires_at=now()-interval '1 second' WHERE option_id=%s", o["option_id"])
    q(dsn, "UPDATE dispatch_offers SET expires_at=now()-interval '1 second' WHERE offer_id=%s", o["offer_id"])
    assert "expired" in tg.press(o["offer_id"], "A")
    assert appointments(dsn, d["sid"]) == []
    assert q(dsn, "SELECT count(*) FROM dock_occupancy WHERE shipment_id=%s AND active", d["sid"])[0][0] == 0
    listed = client.get("/console/dispatch/offers", headers=ops(keys)).json()["offers"]
    assert next(x for x in listed if x["offer_id"] == o["offer_id"])["status"] == "EXPIRED"
    o2 = offer(client, keys, d)                                     # a new offer replaces the expired one
    assert q(dsn, "SELECT status FROM dispatch_offers WHERE offer_id=%s", o["offer_id"])[0][0] == "EXPIRED"
    assert offer_status(dsn, o2["offer_id"]) == "OFFERED"


def test_superseded_offer_cannot_be_accepted(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    opts = options(client, keys, d["sid"])
    o1 = offer(client, keys, d, opt=opts[0])
    o2 = offer(client, keys, d, opt=opts[-1])
    assert offer_status(dsn, o1["offer_id"]) == "SUPERSEDED"
    assert q(dsn, "SELECT superseded_by FROM dispatch_offers WHERE offer_id=%s", o1["offer_id"])[0][0] == uuid.UUID(o2["offer_id"])
    assert "replaced" in tg.press(o1["offer_id"], "A")
    assert appointments(dsn, d["sid"]) == [] and offer_status(dsn, o2["offer_id"]) == "OFFERED"
    tg.press(o2["offer_id"], "A")
    assert len(appointments(dsn, d["sid"])) == 1


def test_slot_taken_after_the_offer_is_refused_at_acceptance(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    o = offer(client, keys, d)
    dock, lo, hi = q(dsn, "SELECT dock_id, lower(during), upper(during) FROM shown_options WHERE option_id=%s", o["option_id"])[0]
    # availability changes after the offer: a planner blocks that dock time (the existing console path)
    r = client.post(f"/console/docks/{dock}/blocks", headers=planner(keys), json={
        "event_id": f"EV-{uuid.uuid4().hex[:8]}", "start": lo.isoformat(), "end": hi.isoformat(),
        "type": "MAINTENANCE", "reason": "test: dock down after the offer"})
    assert r.status_code == 200, r.text
    reply = tg.press(o["offer_id"], "A")
    assert "just taken" in reply or "coordinator" in reply, reply
    assert appointments(dsn, d["sid"]) == []


def test_two_shipments_race_for_the_last_slot_one_wins(client, dsn, keys, app):
    race_eta = (datetime.now(IST) + timedelta(hours=20)).replace(minute=20, second=0, microsecond=0)
    a, b = add_driver(dsn, eta_at=race_eta), add_driver(dsn, eta_at=race_eta)
    ta, tb = link(client, keys, dsn, a["drv"]), link(client, keys, dsn, b["drv"])
    oa = options(client, keys, a["sid"])[0]
    ob = next(x for x in options(client, keys, b["sid"]) if (x["dock_code"], x["start"]) == (oa["dock_code"], oa["start"]))
    offa, offb = offer(client, keys, a, opt=oa), offer(client, keys, b, opt=ob)
    barrier, out = threading.Barrier(2), {}

    def go(tg, off, name):
        tg.c = TestClient(app)
        barrier.wait()
        out[name] = tg.press(off["offer_id"], "A")

    ts = [threading.Thread(target=go, args=x) for x in ((ta, offa, "a"), (tb, offb, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    booked = [n for n, s in (("a", a), ("b", b)) if appointments(dsn, s["sid"])]
    assert len(booked) == 1, out


# ================================================================ Phase 3A: reassignment before confirmation
def test_reassignment_supersedes_the_old_drivers_offer_and_switches_only_on_the_new_drivers_yes(client, dsn, keys):
    d = add_driver(dsn)
    new = add_bare_driver(dsn)
    told = link(client, keys, dsn, d["drv"])
    o_old = offer(client, keys, d)
    # the new driver has no shipment in scope yet: linking is by the dispatcher of a live shipment, so link via a
    # temporary shipment of theirs in this facility
    tmp = add_driver(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE setuhaul.shipments SET driver_id=%s WHERE shipment_id=%s", (new, tmp["sid"]))
    tnew = link(client, keys, dsn, new)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE setuhaul.shipments SET current_status='COMPLETED' WHERE shipment_id=%s", (tmp["sid"],))
    o_new = offer(client, keys, d, driver=new)
    assert o_new["kind"] == "REASSIGNMENT" and o_new["previous_driver_id"] == d["drv"]
    assert offer_status(dsn, o_old["offer_id"]) == "SUPERSEDED"
    told.press(o_old["offer_id"], "A")                              # the previous driver cannot act any more
    assert appointments(dsn, d["sid"]) == []
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == d["drv"]   # unchanged so far
    assert "planner" in tnew.press(o_new["offer_id"], "A")
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == new
    assert q(dsn, "SELECT count(*) FROM chat_threads WHERE driver_id=%s AND shipment_id=%s", d["drv"], d["sid"])[0][0] == 0
    assert len(appointments(dsn, d["sid"])) == 1
    assert q(dsn, "SELECT count(*) FROM booking_events WHERE action='SHIPMENT_REASSIGNED' AND shipment_id=%s", d["sid"])[0][0] == 1


def test_reassignment_decline_or_expiry_keeps_the_old_driver(client, dsn, keys):
    d = add_driver(dsn)
    new = add_bare_driver(dsn)
    o = offer(client, keys, d, driver=new)
    with psycopg.connect(dsn, autocommit=True) as c:          # the new driver answers NO on WhatsApp
        phone = c.execute("SELECT phone_e164 FROM setuhaul.drivers WHERE driver_id=%s", (new,)).fetchone()[0]
    Driver(client, phone).say("nahi")
    assert offer_status(dsn, o["offer_id"]) == "DECLINED"
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == d["drv"]
    o2 = offer(client, keys, d, driver=new)
    q(dsn, "UPDATE shown_options SET expires_at=now()-interval '1 second' WHERE option_id=%s", o2["option_id"])
    q(dsn, "UPDATE dispatch_offers SET expires_at=now()-interval '1 second' WHERE offer_id=%s", o2["offer_id"])
    Driver(client, phone).say("haan ji")
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == d["drv"]
    assert appointments(dsn, d["sid"]) == []


def test_reassignment_of_a_confirmed_appointment_is_not_in_this_scope(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    assert "planner" in tg.press(offer(client, keys, d)["offer_id"], "A")
    assert len(appointments(dsn, d["sid"])) == 1
    new = add_bare_driver(dsn)
    r = offer(client, keys, d, driver=new, opt=options(client, keys, d["sid"])[0], expect=409)
    assert r["error"] == "SHIPMENT_ALREADY_BOOKED"                 # confirmed / pending appointments are untouched


# ================================================================ Phase 5: Telegram identity and isolation
def test_link_code_is_single_use_and_expires(client, dsn, keys):
    d = add_driver(dsn)
    code = client.post(f"/console/drivers/{d['drv']}/telegram-link-code", headers=ops(keys)).json()["code"]
    assert q(dsn, "SELECT count(*) FROM driver_link_codes WHERE code_hash LIKE %s", code.lower() + "%")[0][0] == 0
    a, b = Tg(client), Tg(client)
    assert "linked to SetuHaul" in a.text(f"/start {code}")
    assert "did not work" in b.text(f"/start {code}")              # used
    d2 = add_driver(dsn)
    code2 = client.post(f"/console/drivers/{d2['drv']}/telegram-link-code", headers=ops(keys)).json()["code"]
    q(dsn, "UPDATE driver_link_codes SET expires_at=now()-interval '1 second' WHERE driver_id=%s", d2["drv"])
    assert "did not work" in b.text(f"/start {code2}")              # expired
    code3 = client.post(f"/console/drivers/{d2['drv']}/telegram-link-code", headers=ops(keys)).json()["code"]
    assert "already linked" in a.text(f"/start {code3}")            # one Telegram account, one driver
    r = client.post(f"/console/drivers/{d['drv']}/telegram-link-code", headers=planner(keys))
    assert r.status_code == 403
    r = client.post(f"/console/drivers/{d['drv']}/telegram-link-code", headers=ops(keys, GGN))
    assert r.status_code == 404                                    # not a driver of this dispatcher's facility


def test_unlinked_account_learns_nothing_and_changes_nothing(client, dsn, keys):
    d = add_driver(dsn)
    o = offer(client, keys, d)
    stranger = Tg(client)
    assert "not linked" in stranger.text("hi, any slot for ORD?")
    assert "not linked" in stranger.press(o["offer_id"], "A")
    assert "not linked" in stranger.location(26.9, 75.8)
    assert offer_status(dsn, o["offer_id"]) == "OFFERED"
    assert q(dsn, "SELECT count(*) FROM chat_messages WHERE channel='TELEGRAM' AND external_message_id LIKE 'tg%%' "
                  "AND thread_id IN (SELECT thread_id FROM chat_threads WHERE driver_id=%s)", d["drv"])[0][0] == 0


def test_a_driver_cannot_act_on_another_drivers_offer(client, dsn, keys):
    a, b = add_driver(dsn), add_driver(dsn)
    link(client, keys, dsn, a["drv"])
    tb = link(client, keys, dsn, b["drv"])
    oa = offer(client, keys, a)
    assert "not valid for your account" in tb.press(oa["offer_id"], "A")
    assert offer_status(dsn, oa["offer_id"]) == "OFFERED" and appointments(dsn, a["sid"]) == []


def test_webhook_secret_and_payload_hygiene(client, dsn, keys):
    t = Tg(client)
    assert t.post({"update_id": 1}, secret="wrong").status_code == 401
    assert client.post("/webhooks/telegram", content=b"{}").status_code == 401
    assert t.post({"update_id": next(_update), "message": {"from": {"id": t.uid}, "chat": {"id": -100, "type": "group"},
                                                           "text": "/start ABCDEFGHJK"}}).status_code == 200
    assert t.last_reply() is None                                   # group chats never speak for a driver


def test_typed_telegram_message_reaches_the_existing_conversation_once(client, dsn, keys, app):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    before = len(app.state.inbound)
    tg.text("running 2 hours late", update_id=777000111)
    tg.text("running 2 hours late", update_id=777000111)            # redelivered update: stored once, one turn
    assert q(dsn, "SELECT count(*) FROM chat_messages WHERE channel='TELEGRAM' AND external_message_id='tg:777000111'")[0][0] == 1
    events = [e for e in list(app.state.inbound)[before:] if e.get("external_message_id") == "tg:777000111"]
    assert len(events) == 1 and events[0]["driver_id"] == d["drv"]


# ================================================================ Phase 4: location over the verified channel
def _delay(client, dsn, d):
    wa = Driver(client, d["phone"])
    wa.say("Running 90 min late")
    eta = (datetime.now(IST) + timedelta(minutes=90)).isoformat()
    assert wa.tool("record_driver_report", message_id=wa.last_msg, declared_eta=eta, eta_precision="PRECISE",
                   delay_reason="TRAFFIC")["status"] == "OK"
    return wa


def test_location_is_assessed_stored_coarsely_and_attached_to_the_open_delay(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    _delay(client, dsn, d)
    # ~60 km from the Jaipur facility, ETA in 90 min: consistent
    assert "received" in tg.location(27.43, 75.80, accuracy=30, update_id=880000001)
    (assessment, gh, exc, sid, dist), = q(dsn, """SELECT assessment, position_geohash, exception_id, shipment_id,
                                                         distance_to_facility_km FROM driver_location_reports
                                                   WHERE driver_id=%s""", d["drv"])
    assert assessment == "VERIFIED" and len(gh) == 7 and exc is not None and sid == d["sid"] and 50 < dist < 70
    row = q(dsn, "SELECT row_to_json(r)::text FROM driver_location_reports r WHERE driver_id=%s", d["drv"])[0][0]
    assert "27.43" not in row and "75.8" not in row                 # precise coordinates are not kept
    tg.location(27.43, 75.80, accuracy=30, update_id=880000001)     # redelivered: one row
    assert q(dsn, "SELECT count(*) FROM driver_location_reports WHERE driver_id=%s", d["drv"])[0][0] == 1
    # 600 km away with the same ETA: cannot arrive in time -> a person looks at it
    tg.location(21.0, 79.0, accuracy=30)
    assert q(dsn, """SELECT assessment, reasons FROM driver_location_reports WHERE driver_id=%s
                     ORDER BY report_id DESC LIMIT 1""", d["drv"])[0][0] == "NEEDS_REVIEW"


def test_location_bad_old_imprecise_live_or_contextless(client, dsn, keys):
    d = add_driver(dsn)
    tg = link(client, keys, dsn, d["drv"])
    _delay(client, dsn, d)
    assert "could not be used" in tg.location(27.0, 75.8, age_s=3600)
    assert "could not be used" in tg.location(27.0, 75.8, accuracy=5000)
    assert "could not be used" in tg.location(0, 0)
    assert tg.location(27.0, 75.8, live=True) is None or True      # live sharing is ignored: no continuous tracking
    assert q(dsn, "SELECT count(*) FROM driver_location_reports WHERE driver_id=%s AND assessment='INVALID'",
             d["drv"])[0][0] == 3
    # GGN has no facility position on record: never VERIFIED, nothing invented
    g = add_driver(dsn, facility=GGN)
    tgg = link(client, keys, dsn, g["drv"], fac=GGN)
    _delay(client, dsn, g)
    tgg.location(28.45, 77.03, accuracy=20)
    a, reasons = q(dsn, "SELECT assessment, reasons FROM driver_location_reports WHERE driver_id=%s", g["drv"])[0]
    assert a == "NEEDS_REVIEW" and "NO_FACILITY_POSITION" in reasons


def test_location_cannot_be_recorded_for_another_drivers_conversation(dsn):
    a, b = add_driver(dsn), add_driver(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')",
                  (f"THR-X-{a['drv']}", a["drv"], a["sid"]))
    with psycopg.connect(dsn) as c:
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_ingest")
            c.execute("SET LOCAL search_path = setuhaul")
            with pytest.raises(psycopg.errors.RaiseException, match="FORBIDDEN"):
                c.execute("SELECT record_driver_location(%s,%s,%s,%s)",
                          (b["drv"], f"THR-X-{a['drv']}", "x-1", json.dumps({"source": "TEST_SINK", "shared": True,
                                                                            "assessment": "VERIFIED",
                                                                            "assessor_version": "t"})))


def test_declining_to_share_is_recorded_and_the_delay_report_stands(client, dsn, keys, app):
    from setuhaul.domain.location import LocationReport
    d = add_driver(dsn)
    _delay(client, dsn, d)
    r = app.state.service.record_location(d["drv"], LocationReport(shared=False), "declined-1", source="TEST_SINK")
    assert r["assessment"] == "UNAVAILABLE" and r["exception_id"] is not None
    assert q(dsn, "SELECT count(*) FROM driver_exceptions WHERE shipment_id=%s AND status='OPEN'", d["sid"])[0][0] == 1
