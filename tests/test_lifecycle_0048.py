"""Workstream A (27 Nov fix plan, migration 0048): the appointment lifecycle, the automatic no-show, its backfill, the
dispatcher's decision after a no-show and the ops_lead Controls reads.

Own database (setuhaul_build_lifecycle). Its lifecycle cutover is moved 7 days back (as superuser, test only) so the
minute job covers "recent" appointments, and appointments older than that stand in for the historical backlog the
backfill handles. Each appointment gets its own dock so past windows never collide. Nothing leaves the process.
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
from test_api import CLIENT, GGN, ISSUER, JAI, META_SECRET, SESSION_SECRET, H, keys, staff_token  # noqa: F401
from test_dispatch_offers import TG_SECRET, add_driver, make_database, q

GRACE_JAI = 30            # facility_config.no_show_grace_min seeded by make_database (Jaipur 30, Gurugram 20)
GRACE_GGN = 20


@pytest.fixture(scope="module")
def dsn():
    d = make_database("setuhaul_build_lifecycle")
    with psycopg.connect(d, autocommit=True) as c:                 # test only: history = older than 7 days
        c.execute("ALTER TABLE setuhaul.lifecycle_cutover DISABLE TRIGGER lifecycle_cutover_fixed")
        c.execute("UPDATE setuhaul.lifecycle_cutover SET started_at = now() - interval '7 days'")
        c.execute("ALTER TABLE setuhaul.lifecycle_cutover ENABLE TRIGGER lifecycle_cutover_fixed")
    return d


@pytest.fixture(scope="module")
def app(dsn, keys):
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True, telegram_webhook_secret=TG_SECRET))


@pytest.fixture
def client(app):
    return TestClient(app)


def who(keys, *groups, fac=JAI):
    return H(staff_token(keys, groups=groups, facilities=(fac,)))


def booked(dsn, start: datetime, minutes: int = 60, facility=JAI, status="CONFIRMED") -> dict:
    """A CONFIRMED appointment at `start` on a dock of its own (superuser fixture; the real booking path cannot create
    a past slot). Returns ids."""
    d = add_driver(dsn, facility=facility)
    u = uuid.uuid4().hex[:8]
    dock, appt = f"DOCK-T-{u}", f"APT-T-{u}"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg)
                     VALUES (%s,%s,%s,'STANDARD',false,30000)""", (dock, facility, f"T{u[:4]}"))
        occ = c.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                           VALUES (%s,%s,tstzrange(%s,%s,'[)'),'APPOINTMENT',%s,%s,'test') RETURNING occupancy_id""",
                        (dock, facility, start, start + timedelta(minutes=minutes), d["sid"], appt)).fetchone()[0]
        c.execute("""INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,occupancy_id,status,is_current,
                     booking_source,warehouse_confirmation_ref,confirmation_basis)
                     VALUES (%s,%s,%s,tstzrange(%s,%s,'[)'),%s,%s,true,'PLANNER','test','HUMAN')""",
                  (appt, d["sid"], dock, start, start + timedelta(minutes=minutes), occ, status))
    return {**d, "appt": appt, "dock": dock, "occ": occ}


def appt_row(dsn, appt):
    return q(dsn, """SELECT a.status, a.arrived_at IS NOT NULL, a.docked_at IS NOT NULL, a.completed_at IS NOT NULL,
                            a.no_show_basis, a.no_show_resolution, a.is_current, x.active, s.current_status
                       FROM appointments a JOIN dock_occupancy x ON x.occupancy_id = a.occupancy_id
                       JOIN shipments s ON s.shipment_id = a.shipment_id WHERE a.appointment_id=%s""", appt)[0]


def events(dsn, sid):
    return [r[0] for r in q(dsn, "SELECT action FROM booking_events WHERE shipment_id=%s ORDER BY event_id", sid)]


def run_job(dsn):
    with Db(dsn).tx("system", (JAI, GGN)) as c:
        return c.execute("SELECT process_no_shows(500)").fetchone()[0]


def now():
    return datetime.now(timezone.utc)


# ================================================================ the grace boundary (one definition)
def test_grace_boundary_is_exact_and_shared(dsn):
    with psycopg.connect(dsn) as c:
        c.execute("SET search_path = setuhaul")
        t0 = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
        due = lambda at, g=30: c.execute("SELECT no_show_due(%s,%s,%s)", (t0, g, at)).fetchone()[0]
        assert due(t0 + timedelta(minutes=30)) is False                         # exactly start + grace: arrival still OK
        assert due(t0 + timedelta(minutes=30, microseconds=1)) is True          # one microsecond later: no-show due
        assert due(t0 + timedelta(minutes=29, seconds=59)) is False
        assert due(t0 + timedelta(minutes=20, microseconds=1), 20) is True      # Gurugram's own 20 min
        assert due(t0, 0) is False and due(t0 + timedelta(microseconds=1), 0) is True


def test_each_facility_uses_its_own_grace(dsn):
    jai = booked(dsn, now() - timedelta(minutes=25))                 # Jaipur 30 min: not due yet
    ggn = booked(dsn, now() - timedelta(minutes=25), facility=GGN)   # Gurugram 20 min: due
    run_job(dsn)
    assert appt_row(dsn, jai["appt"])[0] == "CONFIRMED"
    assert appt_row(dsn, ggn["appt"])[0] == "NO_SHOW"


def test_no_show_just_before_and_after_the_boundary(dsn):
    before = booked(dsn, now() - timedelta(minutes=GRACE_JAI) + timedelta(seconds=20))     # deadline in 20 s
    after = booked(dsn, now() - timedelta(minutes=GRACE_JAI) - timedelta(seconds=2))       # deadline 2 s ago
    run_job(dsn)
    assert appt_row(dsn, before["appt"])[0] == "CONFIRMED"
    assert appt_row(dsn, after["appt"])[:1] == ("NO_SHOW",)


def test_arrival_accepted_until_the_deadline_and_refused_after(client, dsn, keys):
    inside = booked(dsn, now() - timedelta(minutes=GRACE_JAI) + timedelta(seconds=20))
    r = client.post(f"/console/appointments/{inside['appt']}/arrival", headers=who(keys, "operations"), json={})
    assert r.status_code == 200 and r.json()["arrived_at"], r.text
    late = booked(dsn, now() - timedelta(minutes=GRACE_JAI) - timedelta(seconds=2))
    r = client.post(f"/console/appointments/{late['appt']}/arrival", headers=who(keys, "operations"), json={})
    assert r.status_code == 409 and r.json()["error"] == "GRACE_PASSED"
    assert "grace period has passed" in r.json()["message"]                # issue 2: a plain sentence
    assert appt_row(dsn, late["appt"])[0] == "CONFIRMED"                   # refused: nothing changed


# ================================================================ the lifecycle, end to end
def test_full_lifecycle_gate_dock_completed(client, dsn, keys):
    b = booked(dsn, now() + timedelta(minutes=30))
    h = who(keys, "operations")
    r = client.post(f"/console/appointments/{b['appt']}/arrival", headers=h, json={"note": "at gate 2"})
    assert r.status_code == 200, r.text
    assert appt_row(dsn, b["appt"])[:3] == ("CONFIRMED", True, False) and appt_row(dsn, b["appt"])[8] == "AT_GATE"
    r = client.post(f"/console/appointments/{b['appt']}/dock", headers=h, json={})
    assert r.status_code == 200 and r.json()["status"] == "IN_PROGRESS"
    assert appt_row(dsn, b["appt"])[8] == "IN_DOCK" and appt_row(dsn, b["appt"])[7] is True     # dock time still held
    r = client.post(f"/console/appointments/{b['appt']}/completion", headers=who(keys, "ops_lead"), json={})
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    st, arr, dk, done, _, _, _, active, ship = appt_row(dsn, b["appt"])
    assert (st, arr, dk, done, active, ship) == ("COMPLETED", True, True, True, False, "COMPLETED")   # rest released
    assert events(dsn, b["sid"])[-3:] == ["GATE_ARRIVAL_RECORDED", "DOCK_START_RECORDED", "COMPLETION_RECORDED"]
    told = q(dsn, """SELECT m.body_masked FROM chat_messages m JOIN chat_threads t ON t.thread_id = m.thread_id
                      WHERE t.driver_id = %s AND m.sender_type = 'AGENT' ORDER BY m.message_id DESC LIMIT 1""", b["drv"])
    assert not told or "Unloading complete" in told[0][0]           # told only when the driver has a conversation


def test_steps_only_in_order(client, dsn, keys):
    b = booked(dsn, now() + timedelta(minutes=30))
    h = who(keys, "operations")
    r = client.post(f"/console/appointments/{b['appt']}/dock", headers=h, json={})
    assert r.status_code == 409 and r.json()["error"] == "NOT_ARRIVED"
    r = client.post(f"/console/appointments/{b['appt']}/completion", headers=h, json={})
    assert r.status_code == 409 and r.json()["error"] == "NOT_IN_DOCK"
    assert appt_row(dsn, b["appt"])[0] == "CONFIRMED"


def test_each_step_is_idempotent(client, dsn, keys):
    b = booked(dsn, now() + timedelta(minutes=30))
    h = who(keys, "operations")
    for step in ("arrival", "dock", "completion"):
        first = client.post(f"/console/appointments/{b['appt']}/{step}", headers=h, json={})
        again = client.post(f"/console/appointments/{b['appt']}/{step}", headers=h, json={})
        assert first.status_code == again.status_code == 200
        assert first.json()["replayed"] is False and again.json()["replayed"] is True
    assert events(dsn, b["sid"]).count("GATE_ARRIVAL_RECORDED") == 1
    assert events(dsn, b["sid"]).count("COMPLETION_RECORDED") == 1


def test_arrival_closes_a_pending_time_change(client, dsn, keys):
    """The truck came for its booking: a reschedule still waiting for a planner no longer applies (invalidated, its
    claim released), so a later approval can never cancel the booking the truck is at."""
    b = booked(dsn, now() + timedelta(minutes=20))
    occ_new = None
    with psycopg.connect(dsn, autocommit=True) as c:                      # a pending request, as the real path leaves it
        c.execute("SET search_path = setuhaul")
        c.execute("BEGIN")
        s = now() + timedelta(hours=5)
        occ_new = c.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                               VALUES (%s,%s,tstzrange(%s,%s,'[)'),'APPOINTMENT',%s,'APT-P-' || %s,'test') RETURNING occupancy_id""",
                            (b["dock"], JAI, s, s + timedelta(hours=1), b["sid"], b["appt"])).fetchone()[0]
        c.execute("""INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,occupancy_id,status,is_current,
                     booking_source,pending_expires_at,replaced_appointment_id)
                     VALUES ('APT-P-' || %s,%s,%s,tstzrange(%s,%s,'[)'),%s,'PENDING_CONFIRMATION',false,'DISPATCHER',
                             now() + interval '10 minutes',%s)""",
                  (b["appt"], b["sid"], b["dock"], s, s + timedelta(hours=1), occ_new, b["appt"]))
        c.execute("""INSERT INTO approval_requests (approval_id,kind,facility_id,shipment_id,proposed_action,evidence,status,
                     expires_at,appointment_id,claim_occupancy_id) VALUES (gen_random_uuid(),'PLANNER_REVIEW',%s,%s,'{}',
                     '{}','OPEN',now() + interval '10 minutes','APT-P-' || %s,%s)""", (JAI, b["sid"], b["appt"], occ_new))
        c.execute("COMMIT")
    r = client.post(f"/console/appointments/{b['appt']}/arrival", headers=who(keys, "operations"), json={})
    assert r.status_code == 200, r.text
    assert q(dsn, "SELECT status FROM approval_requests WHERE appointment_id = 'APT-P-' || %s", b["appt"])[0][0] == "INVALIDATED"
    assert q(dsn, "SELECT active FROM dock_occupancy WHERE occupancy_id=%s", occ_new)[0][0] is False
    assert appt_row(dsn, b["appt"])[0] == "CONFIRMED"


# ================================================================ automatic no-show
def test_no_show_releases_dock_time_audits_and_is_idempotent(dsn):
    b = booked(dsn, now() - timedelta(minutes=45))
    first = run_job(dsn)
    assert first["no_shows"] >= 1
    st, arr, _, _, basis, res, cur, active, ship = appt_row(dsn, b["appt"])
    assert (st, arr, basis, res, cur, active) == ("NO_SHOW", False, "AUTOMATIC", None, True, False)
    assert ship == "IN_TRANSIT"                                        # D3: the shipment is NOT put back anywhere
    run_job(dsn)                                                       # again: nothing more happens
    assert events(dsn, b["sid"]).count("NO_SHOW_RECORDED") == 1
    detail = q(dsn, """SELECT after_state FROM booking_events WHERE shipment_id=%s AND action='NO_SHOW_RECORDED'""", b["sid"])[0][0]
    assert detail["grace_min"] == GRACE_JAI and detail["dock_time_released"] is True


@pytest.mark.parametrize("exclude", ["arrived", "docked", "completed", "cancel_open", "cancel_approved", "cancelled",
                                     "shipment_at_gate", "not_current"])
def test_no_show_never_touches_appointments_with_activity(dsn, exclude):
    b = booked(dsn, now() - timedelta(minutes=50))
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        if exclude in ("arrived", "docked", "completed"):
            c.execute("UPDATE appointments SET arrived_at = now() - interval '40 minutes' WHERE appointment_id=%s", (b["appt"],))
        if exclude in ("docked", "completed"):
            c.execute("UPDATE appointments SET status='IN_PROGRESS', docked_at = now() - interval '30 minutes' "
                      "WHERE appointment_id=%s", (b["appt"],))
        if exclude == "completed":
            c.execute("UPDATE appointments SET status='COMPLETED', completed_at = now() WHERE appointment_id=%s", (b["appt"],))
        if exclude in ("cancel_open", "cancel_approved"):
            c.execute("""INSERT INTO cancellation_requests (request_id,appointment_id,shipment_id,facility_id,dock_id,during,
                         reason_code,note,status,requested_by,requested_role,idem_key,decided_by,decided_at)
                         SELECT gen_random_uuid(), appointment_id, shipment_id, %s, dock_id, during, 'OPERATIONS_DECISION',
                                'test', %s, 'staff:x', 'dispatcher', gen_random_uuid()::text,
                                CASE WHEN %s THEN 'staff:y' END, CASE WHEN %s THEN now() END
                           FROM appointments WHERE appointment_id=%s""",
                      (JAI, "APPROVED" if exclude == "cancel_approved" else "OPEN", exclude == "cancel_approved",
                       exclude == "cancel_approved", b["appt"]))
        if exclude == "cancelled":
            c.execute("UPDATE appointments SET status='CANCELLED', is_current=false WHERE appointment_id=%s", (b["appt"],))
        if exclude == "shipment_at_gate":
            c.execute("UPDATE shipments SET current_status='AT_GATE' WHERE shipment_id=%s", (b["sid"],))
        if exclude == "not_current":
            c.execute("UPDATE appointments SET is_current=false WHERE appointment_id=%s", (b["appt"],))
    before = q(dsn, "SELECT status FROM appointments WHERE appointment_id=%s", b["appt"])[0][0]
    run_job(dsn)
    assert q(dsn, "SELECT status FROM appointments WHERE appointment_id=%s", b["appt"])[0][0] == before != "NO_SHOW"
    assert "NO_SHOW_RECORDED" not in events(dsn, b["sid"])


def test_history_before_the_cutover_is_left_for_the_backfill(dsn):
    old = booked(dsn, now() - timedelta(days=8))
    run_job(dsn)
    assert appt_row(dsn, old["appt"])[0] == "CONFIRMED"


# ================================================================ gate arrival vs no-show: locking and races
def test_a_person_recording_the_arrival_holds_off_the_job(dsn):
    """The arrival transaction holds the row: the job skips it (SKIP LOCKED); after the arrival commits it is no
    longer eligible. Both cannot win."""
    b = booked(dsn, now() - timedelta(minutes=GRACE_JAI) + timedelta(seconds=30))
    db = Db(dsn)
    staff = ("staff:ops-1", frozenset({"operations"}), (JAI,))
    with db.tx("console", (JAI,), staff=staff) as c:
        c.execute("SELECT record_gate_arrival(%s,%s,NULL)", (b["appt"], "staff:ops-1"))
        # the deadline passes while the arrival is still uncommitted: the job must not mark it
        with psycopg.connect(dsn, autocommit=True) as c2:
            c2.execute("SET search_path = setuhaul")
            c2.execute("UPDATE facility_config SET no_show_grace_min = 0 WHERE facility_id = %s AND active", (JAI,))
        try:
            run_job(dsn)
        finally:
            with psycopg.connect(dsn, autocommit=True) as c2:
                c2.execute("UPDATE setuhaul.facility_config SET no_show_grace_min = %s WHERE facility_id = %s AND active",
                           (GRACE_JAI, JAI))
    run_job(dsn)
    st, arr, *_ = appt_row(dsn, b["appt"])
    assert (st, arr) == ("CONFIRMED", True)


def test_the_job_holding_the_row_makes_a_late_arrival_fail_cleanly(dsn):
    b = booked(dsn, now() - timedelta(minutes=GRACE_JAI + 5))
    db = Db(dsn)
    errors, done = [], threading.Event()
    with db.tx("system", (JAI, GGN)) as c:
        c.execute("SELECT process_no_shows(500)")                # marks it and keeps the row locked until commit
        assert c.execute("SELECT status FROM appointments WHERE appointment_id=%s", (b["appt"],)).fetchone()[0] == "NO_SHOW"

        def arrive():
            try:
                with db.tx("console", (JAI,), staff=("staff:ops-2", frozenset({"operations"}), (JAI,))) as c3:
                    c3.execute("SELECT record_gate_arrival(%s,%s,NULL)", (b["appt"], "staff:ops-2"))
            except psycopg.Error as e:
                errors.append(e.diag.message_primary)
            done.set()
        t = threading.Thread(target=arrive)
        t.start()
        assert not done.wait(1.0)                                # blocked on the row lock
    t.join(10)
    assert errors and errors[0].startswith("ALREADY_NO_SHOW")
    assert appt_row(dsn, b["appt"])[:2] == ("NO_SHOW", False)


def test_simultaneous_arrivals_and_jobs_leave_one_consistent_outcome(dsn):
    """Many arrivals and job runs at once at the deadline: every appointment ends EITHER arrived OR no-show, each with
    exactly one audit row, never both (the CHECK constraint also forbids both)."""
    bs = [booked(dsn, now() - timedelta(minutes=GRACE_JAI) + timedelta(seconds=1)) for _ in range(6)]
    db = Db(dsn)
    barrier = threading.Barrier(12)

    def arrive(b):
        barrier.wait()
        try:
            with db.tx("console", (JAI,), staff=("staff:ops-3", frozenset({"operations"}), (JAI,))) as c:
                c.execute("SELECT record_gate_arrival(%s,%s,NULL)", (b["appt"], "staff:ops-3"))
        except psycopg.Error:
            pass

    def job():
        barrier.wait()
        try:
            run_job(dsn)
        except psycopg.Error:
            pass
    import time
    time.sleep(1.2)                                              # now the deadline is reached
    ts = [threading.Thread(target=arrive, args=(b,)) for b in bs] + [threading.Thread(target=job) for _ in range(6)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    run_job(dsn)
    for b in bs:
        st, arr, *_ = appt_row(dsn, b["appt"])
        ev = events(dsn, b["sid"])
        assert (st == "NO_SHOW") != arr
        assert ev.count("NO_SHOW_RECORDED") + ev.count("GATE_ARRIVAL_RECORDED") == 1


# ================================================================ after a no-show (D3)
def test_unresolved_no_show_waits_for_the_dispatcher(client, dsn, keys):
    b = booked(dsn, now() - timedelta(minutes=50))
    run_job(dsn)
    board = {s["shipment_id"]: s for s in client.get("/console/dispatch/shipments", headers=who(keys, "dispatcher")).json()["shipments"]}
    s = board[b["sid"]]
    assert s["workflow"] == "NO_SHOW" and s["bookable"] is False and s["no_show"]["appointment_id"] == b["appt"]
    # nothing can book it before the dispatcher decides: not a dispatcher offer...
    with pytest.raises(psycopg.Error, match="NO_SHOW_UNRESOLVED"):
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SET search_path = setuhaul")
            c.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,expires_at,created_by)
                         VALUES (%s,%s,tstzrange(now()+interval '5 hours',now()+interval '6 hours','[)'),'HOLD',%s,'x',
                                 now()+interval '3 minutes','test')""", (b["dock"], JAI, b["sid"]))


def test_dispatcher_rebook_returns_it_to_needs_booking(client, dsn, keys):
    b = booked(dsn, now() - timedelta(minutes=50))
    run_job(dsn)
    r = client.post(f"/console/appointments/{b['appt']}/no-show/rebook", headers=who(keys, "planner"), json={"note": "rebook"})
    assert r.status_code == 403                                                  # a planner does not decide this
    r = client.post(f"/console/appointments/{b['appt']}/no-show/rebook", headers=who(keys, "dispatcher"),
                    json={"note": "driver delayed, rebook"})
    assert r.status_code == 200 and r.json()["resolution"] == "REBOOK", r.text
    again = client.post(f"/console/appointments/{b['appt']}/no-show/rebook", headers=who(keys, "dispatcher"),
                        json={"note": "driver delayed, rebook"})
    assert again.status_code == 200 and again.json()["replayed"] is True
    s = {x["shipment_id"]: x for x in client.get("/console/dispatch/shipments", headers=who(keys, "dispatcher")).json()["shipments"]}[b["sid"]]
    assert s["workflow"] == "NEW_BOOKING" and s["bookable"] is True and s["no_show"] is None
    assert appt_row(dsn, b["appt"])[:1] == ("NO_SHOW",)                          # the record stays
    assert "NO_SHOW_REBOOK" in events(dsn, b["sid"])


def test_cancellation_after_no_show_goes_to_a_planner(client, dsn, keys):
    b = booked(dsn, now() - timedelta(minutes=50))
    run_job(dsn)
    r = client.post(f"/console/appointments/{b['appt']}/cancellation-requests", headers=who(keys, "dispatcher"),
                    json={"reason_code": "DRIVER_UNAVAILABLE", "note": "driver not coming", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    assert appt_row(dsn, b["appt"])[5] == "CANCELLATION_REQUESTED"
    r = client.post(f"/console/cancellation-requests/{rid}/decision", headers=who(keys, "planner"), json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text
    assert appt_row(dsn, b["appt"])[5:7] == ("CANCELLED", False)
    # it does NOT come back as "Needs booking": it stays on the board as cancelled after a no-show (D3)
    s = {x["shipment_id"]: x for x in client.get("/console/dispatch/shipments", headers=who(keys, "dispatcher")).json()["shipments"]}[b["sid"]]
    assert s["workflow"] == "NO_SHOW" and s["bookable"] is False and s["no_show"]["resolution"] == "CANCELLED"


def test_rejected_cancellation_after_no_show_hands_it_back(client, dsn, keys):
    b = booked(dsn, now() - timedelta(minutes=50))
    run_job(dsn)
    rid = client.post(f"/console/appointments/{b['appt']}/cancellation-requests", headers=who(keys, "dispatcher"),
                      json={"reason_code": "DRIVER_UNAVAILABLE", "note": "x driver", "idempotency_key": uuid.uuid4().hex}).json()["request_id"]
    r = client.post(f"/console/cancellation-requests/{rid}/decision", headers=who(keys, "planner"), json={"decision": "REJECT"})
    assert r.status_code == 200
    assert appt_row(dsn, b["appt"])[5] is None                                   # waiting for the dispatcher again


# ================================================================ backfill (Q1): dry run first, auditable, idempotent
def _backfill(dsn, dry, run=None):
    with Db(dsn).tx("system", (JAI, GGN)) as c:
        return c.execute("SELECT backfill_no_shows(%s,'owner:test','27 Nov backlog',%s)", (dry, run)).fetchone()[0]


def test_backfill_dry_run_then_apply_exactly_the_reviewed_set(dsn):
    elig = booked(dsn, now() - timedelta(days=9))
    arrived = booked(dsn, now() - timedelta(days=9, hours=2))
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE setuhaul.appointments SET arrived_at = now() - interval '9 days' WHERE appointment_id=%s",
                  (arrived["appt"],))
    with pytest.raises(psycopg.Error, match="DRY_RUN_REQUIRED"):
        _backfill(dsn, False)
    dry = _backfill(dsn, True)
    assert elig["appt"] in [x["appointment_id"] for x in dry["eligible"]]
    assert {"appointment_id": arrived["appt"], "shipment_id": arrived["sid"], "reason": "ARRIVED"} in dry["excluded"]
    assert dry["transitioned"] == 0 and appt_row(dsn, elig["appt"])[0] == "CONFIRMED"       # a dry run changes nothing
    # the eligible set changes after the review: apply refuses, a new dry run is needed
    newer = booked(dsn, now() - timedelta(days=10))
    with pytest.raises(psycopg.Error, match="DRY_RUN_STALE"):
        _backfill(dsn, False, dry["run_id"])
    dry2 = _backfill(dsn, True)
    out = _backfill(dsn, False, dry2["run_id"])
    assert out["transitioned"] == len(dry2["eligible"]) >= 2
    for b in (elig, newer):
        assert appt_row(dsn, b["appt"])[:1] == ("NO_SHOW",) and appt_row(dsn, b["appt"])[4] == "BACKFILL"
        assert "NO_SHOW_BACKFILLED" in events(dsn, b["sid"])
    assert appt_row(dsn, arrived["appt"])[0] == "CONFIRMED"
    again = _backfill(dsn, False, dry2["run_id"])                       # idempotent: nothing left to do
    assert again["transitioned"] == 0
    runs = q(dsn, "SELECT dry_run, transitioned FROM no_show_backfill_runs ORDER BY started_at")
    assert [r[0] for r in runs].count(True) >= 2 and sum(r[1] for r in runs) == out["transitioned"]
    with pytest.raises(psycopg.Error, match="append-only"):
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("DELETE FROM setuhaul.no_show_backfill_runs")


def test_backfill_driver_is_not_messaged_about_history(dsn):
    b = booked(dsn, now() - timedelta(days=12))
    dry = _backfill(dsn, True)
    _backfill(dsn, False, dry["run_id"])
    assert q(dsn, """SELECT count(*) FROM chat_messages m JOIN chat_threads t ON t.thread_id = m.thread_id
                      WHERE t.driver_id=%s AND m.body_masked LIKE '%%grace period%%'""", b["drv"])[0][0] == 0


# ================================================================ RBAC (API and database)
@pytest.mark.parametrize("groups,ok", [(("operations",), True), (("ops_lead",), True), (("planner",), False),
                                       (("dispatcher",), False)])
def test_who_may_record_lifecycle_steps(client, dsn, keys, groups, ok):
    b = booked(dsn, now() + timedelta(minutes=30))
    r = client.post(f"/console/appointments/{b['appt']}/arrival", headers=who(keys, *groups), json={})
    assert r.status_code == (200 if ok else 403), r.text


def test_database_refuses_a_lifecycle_step_without_the_role(dsn):
    b = booked(dsn, now() + timedelta(minutes=30))
    for roles in ({"dispatcher"}, {"planner"}):
        with pytest.raises(psycopg.Error, match="ROLE_NOT_ALLOWED"):
            with Db(dsn).tx("console", (JAI,), staff=("staff:x", frozenset(roles), (JAI,))) as c:
                c.execute("SELECT record_gate_arrival(%s,'staff:x',NULL)", (b["appt"],))


def test_lifecycle_respects_the_facility_boundary(client, dsn, keys):
    b = booked(dsn, now() + timedelta(minutes=30))                          # Jaipur
    r = client.post(f"/console/appointments/{b['appt']}/arrival", headers=who(keys, "operations", fac=GGN), json={})
    assert r.status_code == 404                                              # indistinguishable from unknown


def test_arrivals_list_shows_the_next_step_and_the_drivers_claim_as_a_prompt(client, dsn, keys):
    b = booked(dsn, now() + timedelta(minutes=10))
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')",
                  (f"THR-{b['appt']}", b["drv"], b["sid"]))
        c.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked)
                     VALUES (%s,'DRIVER','TELEGRAM','h','I have reached the gate')""", (f"THR-{b['appt']}",))
    r = client.get(f"/console/facilities/{JAI}/arrivals", headers=who(keys, "operations"))
    assert r.status_code == 200
    row = next(x for x in r.json()["arrivals"] if x["appointment_id"] == b["appt"])
    assert row["next_step"] == "ARRIVED_AT_GATE" and row["you_can_record"] is True
    assert row["driver_says_arrived"]["text"].startswith("I have reached")
    assert appt_row(dsn, b["appt"])[1] is False                              # a driver's words are never the record
    pl = client.get(f"/console/facilities/{JAI}/arrivals", headers=who(keys, "planner")).json()
    assert next(x for x in pl["arrivals"] if x["appointment_id"] == b["appt"])["you_can_record"] is False
    assert client.get(f"/console/facilities/{JAI}/arrivals", headers=who(keys, "dispatcher")).status_code == 403


# ================================================================ issue 1: the ops_lead Controls reads
def test_controls_are_ops_lead_only_and_show_who_changed_what(client, dsn, keys):
    lead = who(keys, "ops_lead")
    r = client.post("/console/holds", headers=lead, json={"facility_id": JAI, "reason": "yard full"})
    assert r.status_code == 200
    hid = r.json()["hold_id"]
    client.post(f"/console/facilities/{JAI}/pause", headers=lead, json={"paused": True, "reason": "drill"})
    c = client.get("/console/controls", headers=lead).json()
    assert any(h["hold_id"] == hid and h["open"] for h in c["holds"])
    fac = next(f for f in c["facilities"] if f["facility_id"] == JAI)
    assert fac["automation_paused"] is True and fac["last_change"]["reason"] == "drill"
    assert fac["no_show_grace_min"] == GRACE_JAI
    assert set(c["kill_switch"]) >= {"engaged", "changed_by", "changed_at", "reason"}
    client.post(f"/console/facilities/{JAI}/pause", headers=lead, json={"paused": False, "reason": "drill over"})
    client.post(f"/console/holds/{hid}/clear", headers=lead, json={"note": "cleared"})
    assert all(h["hold_id"] != hid for h in client.get("/console/holds", headers=lead).json()["holds"])
    for g in ("planner", "operations", "dispatcher"):
        assert client.get("/console/controls", headers=who(keys, g)).status_code == 403
        assert client.get("/console/holds", headers=who(keys, g)).status_code == 403
        assert client.post("/console/booking-kill-switch", headers=who(keys, g),
                           json={"engaged": True, "reason": "not allowed"}).status_code == 403


def test_console_me_reports_the_new_permissions_by_role(client, keys):
    perms = lambda g: client.get("/console/me", headers=who(keys, g)).json()["permissions"]
    assert perms("ops_lead")["view_controls"] and not perms("planner")["view_controls"]
    assert perms("operations")["record_arrival"] and not perms("dispatcher")["record_arrival"]
    assert not perms("planner")["record_arrival"] and perms("planner")["view_arrivals"]
    assert perms("dispatcher")["resolve_no_show"] and perms("dispatcher")["request_reschedule"]
    assert not perms("planner")["request_cancellation"] and not perms("planner")["send_offer"]
