"""move_appointment (migration 0025): a person applies ONE move of a stored proposal; PostgreSQL decides.

Own facilities FAC-SCH-01 / FAC-SCH-02 (created here, idempotently) so these tests share no dock time with the rest
of the suite. Lock tests use lock_timeout and always end their transactions in `finally`.
"""
from __future__ import annotations

import threading
import time as _time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import as_role
from setuhaul.api.db import Db
from setuhaul.api.security import Principal
from setuhaul.api.service import BookingService, ScheduleArgs, ServiceError
from setuhaul.domain.booking import BookingFlow

SCH, SCH2 = "FAC-SCH-01", "FAC-SCH-02"
S1, S2, S3, T1 = "DOCK-SCH-S1", "DOCK-SCH-S2", "DOCK-SCH-S3", "DOCK-SCH2-T1"
IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def sch(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("""INSERT INTO facilities (facility_id,facility_name,open_time,close_time) VALUES
                     (%s,'Scheduling test A','00:00','23:59'),(%s,'Scheduling test B','00:00','23:59')
                     ON CONFLICT DO NOTHING""", (SCH, SCH2))
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     (%s,1,10,10,30,'{"DAY":15,"NIGHT":15}',%s,30),(%s,1,10,10,30,'{"DAY":15,"NIGHT":15}',%s,30)
                     ON CONFLICT DO NOTHING""", (SCH, shifts, SCH2, shifts))
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg) VALUES
                     (%s,%s,'S1','STANDARD',false,30000),(%s,%s,'S2','STANDARD',false,30000),
                     (%s,%s,'S3','STANDARD',false,30000),(%s,%s,'T1','STANDARD',false,30000) ON CONFLICT DO NOTHING""",
                  (S1, SCH, S2, SCH, S3, SCH, T1, SCH2))
    return dsn


def at(h: float) -> datetime:
    """A whole-minute instant h hours from now (database clock and test clock agree to the second)."""
    return (datetime.now(IST) + timedelta(hours=h)).replace(second=0, microsecond=0)


def confirmed(conn, dock=S1, start_h=3.0, mins=55, facility=SCH, eta_h=2.0) -> dict:
    """A shipment with a CONFIRMED appointment and its claim on dock time (superuser fixture)."""
    u = uuid.uuid4().hex[:8]
    drv, veh, sid, aid = f"DRV-{u}", f"VEH-{u}", f"SHP-{u}", f"APT-T-{u}"
    conn.execute("SET search_path = setuhaul")
    conn.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)",
                 (drv, f"+91{uuid.uuid4().int % 10**10:010d}"))
    conn.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}"))
    conn.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                    load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                    VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,now()-interval '3 hours',%s,'IN_TRANSIT')""",
                 (sid, f"ORD-{u}", drv, veh, facility, at(eta_h)))
    s, e = at(start_h), at(start_h) + timedelta(minutes=mins)
    occ = conn.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                          VALUES (%s,%s,tstzrange(%s,%s,'[)'),'APPOINTMENT',%s,%s,'test') RETURNING occupancy_id""",
                       (dock, facility, s, e, sid, aid)).fetchone()[0]
    conn.execute("""INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,occupancy_id,status,is_current,booking_source)
                    VALUES (%s,%s,%s,tstzrange(%s,%s,'[)'),%s,'CONFIRMED',true,'PLANNER')""", (aid, sid, dock, s, e, occ))
    conn.execute("RESET search_path")
    return {"sid": sid, "drv": drv, "aid": aid, "dock": dock, "start": s, "end": e}


@contextmanager
def staff(conn, roles=("ops_lead",), facilities=(SCH,), who=None):
    with as_role(conn, "setuhaul_console", ",".join(facilities)) as c:
        c.execute("SELECT begin_staff_context(%s,%s,%s)",
                  (who or f"staff:t-{uuid.uuid4().hex[:6]}", list(roles), list(facilities)))
        yield c


def move_row(t: dict, dock: str, start: datetime, mins=55, seq=1) -> dict:
    return {"seq": seq, "shipment_id": t["sid"], "appointment_id": t["aid"], "action": "MOVE", "dock_id": dock,
            "start": start.isoformat(), "end": (start + timedelta(minutes=mins)).isoformat(),
            "from_dock_id": t["dock"], "from_start": t["start"].isoformat(), "score": {"total": 0}, "reason": "test"}


def store(conn, rows, facility=SCH, roles=("planner",), ttl=600) -> str:
    with staff(conn, roles, (facility,)) as c:
        r = c.execute("SELECT record_schedule_proposal(%s,'FACILITY_SCORE','test',%s,0,'{}',%s,%s)",
                      (facility, "a" * 64, Jsonb(rows), ttl)).fetchone()[0]
    return r["proposal_id"]


def move(conn, proposal, appt, roles=("ops_lead",), facilities=(SCH,), key=None, who=None) -> dict:
    with staff(conn, roles, facilities, who) as c:
        return c.execute("SELECT move_appointment(%s,%s,%s,%s)",
                         (proposal, appt, key or uuid.uuid4().hex, "test move")).fetchone()[0]


def appt(conn, aid):
    return conn.execute("""SELECT status, is_current, dock_id, lower(during), replaced_appointment_id, booking_source
                             FROM setuhaul.appointments WHERE appointment_id=%s""", (aid,)).fetchone()


def active_claims(conn, dock):
    return conn.execute("""SELECT ref_id, lower(during), upper(during) FROM setuhaul.dock_occupancy
                            WHERE dock_id=%s AND active ORDER BY lower(during)""", (dock,)).fetchall()


# ================================================================ the happy path, with audit and driver notice
def test_a_stored_move_is_applied_once_with_audit_and_driver_notice(sch, conn):
    t = confirmed(conn, S1, 1.0)
    target = at(1.0)
    p = store(conn, [move_row(t, S2, target)])
    r = move(conn, p, t["aid"])
    assert r["outcome"] == "MOVED" and r["status"] == "CONFIRMED" and r["replaced"] == t["aid"] and r["dock_id"] == S2
    assert appt(conn, t["aid"])[:2] == ("CANCELLED", False)
    new = appt(conn, r["appointment_id"])
    assert new[:4] == ("CONFIRMED", True, S2, target) and new[4:] == (t["aid"], "SCHEDULING_TOOL")
    assert [x[0] for x in active_claims(conn, S2) if x[1] == target] == [r["appointment_id"]]
    assert t["aid"] not in [x[0] for x in active_claims(conn, S1)]                 # old claim released
    ev = conn.execute("""SELECT principal, principal_role, action, reason FROM setuhaul.booking_events
                          WHERE shipment_id=%s AND action='APPOINTMENT_MOVED'""", (t["sid"],)).fetchall()
    assert len(ev) == 1 and ev[0][0].startswith("staff:t-") and ev[0][1:] == ("ops_lead", "APPOINTMENT_MOVED", "test move")
    # the driver hears on the existing outbox → notification path, with the existing fixed template
    with as_role(conn, "setuhaul_system", f"{SCH},{SCH2},FAC-JAI-01,FAC-GGN-01,CENTRAL") as c:
        c.execute("SELECT enqueue_notifications(1000)")
        n = c.execute("SELECT template_id, payload->>'outcome', recipient_ref FROM notifications WHERE shipment_id=%s",
                      (t["sid"],)).fetchall()
    assert n == [("booking_superseded", "MOVED", f"driver:{t['drv']}")]
    # applied once: a second click (new key) is refused, the same key returns the stored answer
    with pytest.raises(psycopg.Error, match="ALREADY_APPLIED"):
        move(conn, p, t["aid"])


def test_the_same_idempotency_key_returns_the_stored_answer(sch, conn):
    t = confirmed(conn, S1, 2.0)
    p = store(conn, [move_row(t, S3, at(2.0))])
    k = uuid.uuid4().hex
    first = move(conn, p, t["aid"], key=k, who="staff:t-same")
    assert move(conn, p, t["aid"], key=k, who="staff:t-same") == first             # a retry by the same person
    with pytest.raises(psycopg.Error, match="IDEMPOTENCY_KEY_REUSED"):            # someone else's key is not theirs
        move(conn, p, t["aid"], key=k, who="staff:t-other")


# ================================================================ who may do it
def test_role_refusals(sch, conn):
    t = confirmed(conn, S1, 3.0)
    with pytest.raises(psycopg.Error, match="ROLE_NOT_ALLOWED"):
        store(conn, [move_row(t, S2, at(3.0))], roles=("operations",))         # storing: planner or ops_lead
    p = store(conn, [move_row(t, S2, at(3.0))], roles=("planner",))
    for roles in (("planner",), ("operations",), ("operations", "planner")):    # moving a promise: ops_lead only
        with pytest.raises(psycopg.Error, match="ROLE_NOT_ALLOWED"):
            move(conn, p, t["aid"], roles=roles)
    assert appt(conn, t["aid"])[0] == "CONFIRMED"


def test_without_a_staff_identity_nothing_runs(sch, conn):
    t = confirmed(conn, S1, 4.0)
    p = store(conn, [move_row(t, S2, at(4.0))])
    with pytest.raises(psycopg.Error, match="UNTRUSTED_CONTEXT"):
        with as_role(conn, "setuhaul_console", SCH) as c:                         # console role, no begin_staff_context
            c.execute("SELECT move_appointment(%s,%s,%s,NULL)", (p, t["aid"], uuid.uuid4().hex))


def test_the_agent_cannot_call_it_or_store_proposals(sch, conn):
    for fn in ("move_appointment(uuid,text,text,text)",
               "record_schedule_proposal(text,text,text,text,bigint,jsonb,jsonb,integer)"):
        who = {r[0] for r in conn.execute("""SELECT r.rolname FROM pg_roles r WHERE r.rolname LIKE 'setuhaul_%%'
                                              AND NOT r.rolcanlogin   -- group roles (login roles inherit one each)
                                              AND has_function_privilege(r.oid, %s::regprocedure, 'EXECUTE')""",
                                          (f"setuhaul.{fn}",))}
        assert who == {"setuhaul_console", "setuhaul_rpc"}, (fn, who)
    t = confirmed(conn, S1, 5.0)
    p = store(conn, [move_row(t, S2, at(5.0))])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_role(conn, "setuhaul_agent", SCH, t["drv"]) as c:
            c.execute("SELECT setuhaul.move_appointment(%s,%s,%s,NULL)", (p, t["aid"], uuid.uuid4().hex))
    for tbl in ("schedule_proposals", "schedule_proposal_rows"):
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            for role in ("setuhaul_agent", "setuhaul_feasibility", "setuhaul_ingest"):
                assert not conn.execute("SELECT has_table_privilege(%s,%s,%s)", (role, f"setuhaul.{tbl}", priv)).fetchone()[0]
    # the app-layer gate refuses it as an agent tool in every state, and the service has no such tool
    assert BookingFlow("THR").allow("move_appointment").reason == "TOOL_NOT_AVAILABLE_TO_AGENT"
    svc = BookingService(Db(sch), lambda: datetime.now(timezone.utc), (SCH,), booking_enabled=True)
    drv = Principal("driver", t["drv"], driver_id=t["drv"], thread_id="THR-none")
    with pytest.raises(ServiceError) as e:
        svc.call_tool(drv, "move_appointment", 1, {"proposal_id": str(p), "appointment_id": t["aid"]})
    assert e.value.code == "TOOL_NOT_AVAILABLE_TO_AGENT"


# ================================================================ facility isolation
def test_cross_facility_and_foreign_proposals_are_refused(sch, conn):
    a = confirmed(conn, S1, 6.0)
    b = confirmed(conn, T1, 6.0, facility=SCH2)
    pa = store(conn, [move_row(a, S2, at(6.0))])
    pb = store(conn, [move_row(b, T1, at(7.0))], facility=SCH2)
    with pytest.raises(psycopg.Error, match="PROPOSAL_UNKNOWN"):                 # SCH2 staff cannot see SCH's proposal
        move(conn, pa, a["aid"], facilities=(SCH2,))
    with pytest.raises(psycopg.Error, match="NOT_A_PROPOSED_MOVE"):              # a proposal does not cover another
        move(conn, pb, a["aid"], facilities=(SCH, SCH2))                           # facility's appointment
    with pytest.raises(psycopg.Error, match="SHIPMENT_NOT_IN_FACILITY"):         # nor can one be stored with it
        store(conn, [move_row(b, S2, at(6.0))])
    with pytest.raises(psycopg.Error, match="FORBIDDEN"):
        _store_out_of_scope(conn, a)
    with pytest.raises(psycopg.Error):                                           # a dock of another facility (FK)
        store(conn, [move_row(a, T1, at(6.0))])
    assert appt(conn, a["aid"])[0] == appt(conn, b["aid"])[0] == "CONFIRMED"


def _store_out_of_scope(conn, t):
    with staff(conn, ("ops_lead",), (SCH2,)) as c:                               # facility not in this person's scope
        c.execute("SELECT record_schedule_proposal(%s,'FCFS','test',%s,0,'{}',%s,600)",
                  (SCH, "b" * 64, Jsonb([move_row(t, S2, at(6.0))])))


# ================================================================ stale, expired, changed proposals
def test_expired_proposal_is_refused(sch, conn):
    t = confirmed(conn, S1, 7.0)
    pid = uuid.uuid4()
    conn.execute("""INSERT INTO setuhaul.schedule_proposals VALUES
                    (%s,%s,'FACILITY_SCORE','test',%s,0,'{}','staff:x','planner',now()-interval '20 minutes',
                     now()-interval '10 minutes')""", (pid, SCH, "c" * 64))
    r = move_row(t, S2, at(7.0))
    conn.execute("""INSERT INTO setuhaul.schedule_proposal_rows (proposal_id,seq,facility_id,shipment_id,appointment_id,
                    appointment_row_version,action,dock_id,start_at,end_at,from_dock_id,from_start_at,score,reason)
                    VALUES (%s,1,%s,%s,%s,1,'MOVE',%s,%s,%s,%s,%s,'{}','test')""",
                 (pid, SCH, t["sid"], t["aid"], S2, r["start"], r["end"], t["dock"], t["start"]))
    with pytest.raises(psycopg.Error, match="PROPOSAL_EXPIRED"):
        move(conn, pid, t["aid"])
    with pytest.raises(psycopg.Error, match="BAD_TTL"):
        store(conn, [move_row(t, S2, at(7.0))], ttl=3600)


def test_a_proposal_for_an_appointment_that_changed_since_is_stale(sch, conn):
    t = confirmed(conn, S1, 8.0)
    p = store(conn, [move_row(t, S2, at(8.0))])
    conn.execute("UPDATE setuhaul.appointments SET row_version = row_version + 1 WHERE appointment_id=%s", (t["aid"],))
    with pytest.raises(psycopg.Error, match="APPOINTMENT_CHANGED"):
        move(conn, p, t["aid"])
    stale = dict(move_row(t, S2, at(8.0)), from_start=(t["start"] - timedelta(hours=1)).isoformat())
    with pytest.raises(psycopg.Error, match="APPOINTMENT_CHANGED"):              # computed from an older picture
        store(conn, [stale])


def test_a_move_not_in_the_proposal_is_refused(sch, conn):
    t = confirmed(conn, S1, 9.0)
    keep = dict(move_row(t, S1, t["start"]), action="KEEP")
    p = store(conn, [keep])
    with pytest.raises(psycopg.Error, match="NOT_A_PROPOSED_MOVE"):
        move(conn, p, t["aid"])


def test_overlapping_rows_in_one_proposal_are_refused(sch, conn):
    a, b = confirmed(conn, S1, 10.0), confirmed(conn, S2, 10.0)
    with pytest.raises(psycopg.Error, match="PROPOSAL_ROWS_OVERLAP"):
        store(conn, [move_row(a, S3, at(10.0)), move_row(b, S3, at(10.25), seq=2)])


# ================================================================ trucks in a dock are fixed
def test_in_progress_and_in_dock_trucks_are_never_moved(sch, conn):
    a, b, c = confirmed(conn, S1, 11.0), confirmed(conn, S2, 11.0), confirmed(conn, S3, 11.0)
    pa = store(conn, [move_row(a, S2, at(11.0))])
    pb = store(conn, [move_row(b, S3, at(11.0))])
    pc = store(conn, [move_row(c, S1, at(11.0))])
    conn.execute("UPDATE setuhaul.appointments SET status='IN_PROGRESS' WHERE appointment_id=%s", (a["aid"],))
    with pytest.raises(psycopg.Error, match="APPOINTMENT_IN_PROGRESS"):
        move(conn, pa, a["aid"])
    conn.execute("UPDATE setuhaul.shipments SET current_status='IN_DOCK' WHERE shipment_id=%s", (b["sid"],))
    with pytest.raises(psycopg.Error, match="SHIPMENT_IN_DOCK"):
        move(conn, pb, b["aid"])
    conn.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                    VALUES (%s,%s,tstzrange(now(), now()+interval '20 minutes','[)'),'FIXED',%s,'in-dock','test')""",
                 (S3, SCH, c["sid"]))
    with pytest.raises(psycopg.Error, match="SHIPMENT_IN_DOCK"):
        move(conn, pc, c["aid"])
    with pytest.raises(psycopg.Error, match="APPOINTMENT_NOT_MOVABLE"):          # nor can such a move be stored
        store(conn, [move_row(a, S2, at(11.0))])


def test_hard_booking_rules_are_re_proved(sch, conn):
    a = confirmed(conn, S1, 12.0)
    p = store(conn, [move_row(a, S2, at(12.0))])
    blocker = confirmed(conn, S2, 12.25)                                        # someone took that dock time meanwhile
    with pytest.raises(psycopg.Error, match="SLOT_TAKEN"):
        move(conn, p, a["aid"])
    b = confirmed(conn, S1, 13.0)
    pb = store(conn, [move_row(b, S3, at(13.0))])
    conn.execute("UPDATE setuhaul.shipments SET load_weight_kg=40000 WHERE shipment_id=%s", (b["sid"],))
    with pytest.raises(psycopg.Error, match="DOCK_WEIGHT_EXCEEDED"):
        move(conn, pb, b["aid"])
    assert appt(conn, a["aid"])[0] == appt(conn, blocker["aid"])[0] == "CONFIRMED"


def test_repeatable_read_is_refused(sch, conn):
    t = confirmed(conn, S1, 14.0)
    p = store(conn, [move_row(t, S2, at(14.0))])
    with pytest.raises(psycopg.Error, match="COMPAT_CHANGE_NEEDS_READ_COMMITTED"):
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SET LOCAL ROLE setuhaul_console")
            conn.execute("SET LOCAL search_path = setuhaul")
            conn.execute("SELECT begin_staff_context('staff:t-rr', ARRAY['ops_lead'], ARRAY[%s])", (SCH,))
            conn.execute("SELECT move_appointment(%s,%s,%s,NULL)", (p, t["aid"], uuid.uuid4().hex))


# ================================================================ concurrency: exactly one wins, never an overlap
def _waiting(conn, pid: int, timeout=10.0) -> bool:
    end = _time.monotonic() + timeout
    while _time.monotonic() < end:
        if conn.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone()[0] == "Lock":
            return True
        _time.sleep(0.05)
    return False


def _race(dsn, conn, first, second):
    """`first(c)` runs and holds its transaction open; `second(c)` starts and must wait on a lock; then the first
    commits. Returns (first result, second result or exception)."""
    c1, c2 = psycopg.connect(dsn, autocommit=True), psycopg.connect(dsn, autocommit=True)
    out: dict = {}
    tx = c1.transaction()
    try:
        tx.__enter__()
        out["first"] = first(c1)

        def run():
            try:
                c2.execute("SET lock_timeout = '15s'")
                out["second"] = second(c2)
            except Exception as e:                                            # noqa: BLE001 - reported to the test
                out["second"] = e
        th = threading.Thread(target=run)
        th.start()
        assert _waiting(conn, c2.info.backend_pid), "the second change did not wait for the first"
        tx.__exit__(None, None, None)
        tx = None
        th.join(20)
        assert not th.is_alive()
    finally:
        if tx is not None:
            tx.__exit__(RuntimeError, RuntimeError("rollback"), None)
        c1.close()
        c2.close()
    return out["first"], out["second"]


def _move_in(c, proposal, aid):
    with staff(c) as x:
        return x.execute("SELECT move_appointment(%s,%s,%s,NULL)", (proposal, aid, uuid.uuid4().hex)).fetchone()[0]


def _move_open(c, proposal, aid):
    """Inside the caller's open transaction (no inner commit)."""
    c.execute("SET LOCAL ROLE setuhaul_console")
    c.execute("SET LOCAL search_path = setuhaul")
    c.execute("SELECT set_config('app.facility_scope', %s, true)", (SCH,))
    c.execute("SELECT begin_staff_context(%s, ARRAY['ops_lead'], ARRAY[%s])", (f"staff:t-{uuid.uuid4().hex[:6]}", SCH))
    return c.execute("SELECT move_appointment(%s,%s,%s,NULL)", (proposal, aid, uuid.uuid4().hex)).fetchone()[0]


def test_two_proposals_moving_different_trucks_to_the_same_slot_exactly_one_wins(sch, conn):
    a, b = confirmed(conn, S1, 15.0), confirmed(conn, S2, 15.0)
    slot = at(15.0)
    pa = store(conn, [move_row(a, S3, slot)])                                   # two planners, two live proposals
    pb = store(conn, [move_row(b, S3, slot + timedelta(minutes=20))])
    first, second = _race(sch, conn, lambda c: _move_open(c, pa, a["aid"]), lambda c: _move_in(c, pb, b["aid"]))
    assert first["outcome"] == "MOVED"
    assert isinstance(second, psycopg.Error) and "SLOT_TAKEN" in str(second)
    claims = [x for x in active_claims(conn, S3) if x[1] < slot + timedelta(hours=2) and x[2] > slot]
    assert [x[0] for x in claims] == [first["appointment_id"]]                  # one claim, no overlap
    assert appt(conn, b["aid"])[:3] == ("CONFIRMED", True, S2)                  # the loser is untouched


def test_the_same_move_clicked_twice_applies_once(sch, conn):
    t = confirmed(conn, S1, 16.0)
    p = store(conn, [move_row(t, S2, at(16.0))])
    first, second = _race(sch, conn, lambda c: _move_open(c, p, t["aid"]), lambda c: _move_in(c, p, t["aid"]))
    assert first["outcome"] == "MOVED"
    assert isinstance(second, psycopg.Error) and "ALREADY_APPLIED" in str(second)
    assert conn.execute("SELECT count(*) FROM setuhaul.appointments WHERE replaced_appointment_id=%s",
                        (t["aid"],)).fetchone()[0] == 1


def test_a_move_racing_a_dock_block_exactly_one_wins(sch, conn):
    t = confirmed(conn, S1, 17.0)
    slot = at(17.0)
    p = store(conn, [move_row(t, S3, slot)])

    def block_as_principal(c):
        with as_role(c, "setuhaul_console", SCH) as x:
            who = f"staff:t-{uuid.uuid4().hex[:6]}"
            x.execute("SELECT begin_staff_context(%s, ARRAY['planner'], ARRAY[%s])", (who, SCH))
            return x.execute("SELECT block_dock(%s,%s,%s,%s,'MAINTENANCE','race test',%s)",
                             (f"EV-{uuid.uuid4().hex[:8]}", S3, slot, slot + timedelta(minutes=30), who)).fetchone()[0]
    first, second = _race(sch, conn, lambda c: _move_open(c, p, t["aid"]), block_as_principal)
    assert first["outcome"] == "MOVED"
    assert isinstance(second, psycopg.Error) and second.sqlstate == "23P01"   # the EXCLUDE constraint decided
    claims = [x for x in active_claims(conn, S3) if x[1] < slot + timedelta(minutes=30) and x[2] > slot]
    assert [x[0] for x in claims] == [first["appointment_id"]]


# ================================================================ service and HTTP layer
def chain(stored: dict, sid: str) -> list[dict]:
    """The MOVE rows to apply, in the proposal's apply order, so that `sid`'s move finds its target free."""
    rows = {r["shipment_id"]: r for r in stored["assignments"] if r["action"] == "MOVE"}
    need, todo = set(), [sid]
    while todo:
        x = todo.pop()
        if x not in need:
            need.add(x)
            todo += rows[x]["depends_on"]
    assert all(rows[x]["apply_order"] for x in need), "a swap cycle cannot be applied one move at a time"
    return sorted((rows[x] for x in need), key=lambda r: r["apply_order"])


def _svc(dsn, enabled=True, facilities=(SCH, SCH2)):
    return BookingService(Db(dsn), lambda: datetime.now(timezone.utc), facilities, booking_enabled=enabled)


def _staff(roles=("ops_lead",), facilities=(SCH,)):
    return Principal("staff", f"t-{uuid.uuid4().hex[:6]}", frozenset(roles), tuple(facilities))


def test_booking_switch_off_refuses_the_move(sch, conn):
    t = confirmed(conn, S1, 18.0)
    p = store(conn, [move_row(t, S2, at(18.0))])
    with pytest.raises(ServiceError) as e:
        _svc(sch, enabled=False).move_appointment(_staff(), t["aid"], p, uuid.uuid4().hex)
    assert (e.value.http, e.value.code) == (409, "BOOKING_DISABLED")
    assert appt(conn, t["aid"])[0] == "CONFIRMED"
    from setuhaul.api.app import Settings
    assert Settings.__dataclass_fields__["booking_enabled"].default is False          # off unless switched on
    with pytest.raises(ServiceError) as e:                                            # role checked too (switch on)
        _svc(sch).move_appointment(_staff(("planner",)), t["aid"], p, uuid.uuid4().hex)
    assert (e.value.http, e.value.code) == (403, "FORBIDDEN")


def test_a_move_the_truck_can_no_longer_make_is_refused(sch, conn):
    """Stale ETA: the stored move starts before the truck's CURRENT ready time (service check, before the database)."""
    t = confirmed(conn, S1, 22.0, eta_h=21.5)
    p = store(conn, [move_row(t, S3, at(3.5))])
    with pytest.raises(ServiceError) as e:
        _svc(sch).move_appointment(_staff(), t["aid"], p, uuid.uuid4().hex)
    assert (e.value.http, e.value.code) == (409, "NOT_FEASIBLE")
    assert appt(conn, t["aid"])[:2] == ("CONFIRMED", True)


def test_the_planner_proposes_and_a_person_applies_one_move_end_to_end(sch, conn):
    """Through the service (and so the Data API path when SETUHAUL_TEST_DATA_API=1): an early truck with a promise
    four hours out is proposed an earlier dock time; the stored proposal's move is applied by an ops lead."""
    t = confirmed(conn, S1, 19.0, eta_h=0.5)
    svc = _svc(sch)
    preview = svc.schedule_proposal(_staff(("planner",)), SCH)
    assert preview["status"] == "PROPOSAL" and preview["stored"] is False and preview["proposal_id"] is None
    stored = svc.store_schedule_proposal(_staff(("planner",)), SCH, "FACILITY_SCORE", 600)
    assert stored["stored"] is True and stored["proposal_id"] and len(stored["input_fingerprint"]) == 64
    mine = next(r for r in stored["assignments"] if r["shipment_id"] == t["sid"])
    assert mine["action"] == "MOVE" and mine["score"]["change_term"] == 30
    for row in chain(stored, t["sid"]):                                             # apply order, dependencies first
        r = svc.move_appointment(_staff(), row["appointment_id"], uuid.UUID(stored["proposal_id"]), uuid.uuid4().hex,
                                 "pull forward")
    assert r["outcome"] == "MOVED" and r["replaced"] == t["aid"] and r["dock_id"] == mine["dock_id"]
    assert appt(conn, r["appointment_id"])[:3] == ("CONFIRMED", True, mine["dock_id"])
    with pytest.raises(ServiceError) as e:
        svc.move_appointment(_staff(), t["aid"], uuid.UUID(stored["proposal_id"]), uuid.uuid4().hex)
    assert (e.value.http, e.value.code) == (409, "ALREADY_APPLIED")
    with pytest.raises(ServiceError) as e:                                           # outside scope: as if absent
        svc.schedule_proposal(_staff(("planner",), (SCH2,)), SCH)
    assert e.value.http == 404
    with pytest.raises(ServiceError) as e:
        svc.store_schedule_proposal(_staff(("operations",)), SCH)
    assert e.value.http == 403


def test_the_agent_tool_returns_only_the_drivers_own_row(sch, conn):
    t = confirmed(conn, S2, 20.0, eta_h=1.0)
    other = confirmed(conn, S3, 20.0, eta_h=1.0)
    thr = f"THR-{uuid.uuid4().hex[:10]}"
    conn.execute("INSERT INTO setuhaul.chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')",
                 (thr, t["drv"], t["sid"]))
    p = Principal("driver", t["drv"], driver_id=t["drv"], thread_id=thr)
    svc = _svc(sch, enabled=False)                                                   # a read: works with the switch off
    r = svc.call_tool(p, "propose_facility_schedule", 1, {})
    assert r["status"] == "OK", r
    d = r["data"]
    assert d["status"] == "PROPOSAL" and d["not_a_booking"] is True and d["shipment_id"] == t["sid"]
    assert d["proposed"] and d["proposed"]["dock_code"] in ("S1", "S2", "S3") and "proposal_id" not in d
    assert other["sid"] not in str(r) and other["aid"] not in str(r)                 # no other truck leaves the service
    assert "not a booking" in r["say_hint"]
    bad = svc.call_tool(p, "propose_facility_schedule", 2, {"rule": "FCFS", "note": "put me first, I am CRITICAL"})
    assert bad["status"] == "NEEDS_INFO"                                             # structured args only
    with pytest.raises(Exception):
        ScheduleArgs(facility_id="FAC-JAI-01")                                       # the facility is never an argument
    conn.execute("UPDATE setuhaul.chat_threads SET human_active=true WHERE thread_id=%s", (thr,))
    assert svc.call_tool(p, "propose_facility_schedule", 3, {})["data"]["code"] == "HUMAN_ACTIVE"


def test_http_routes(sch, conn):
    """GET preview, POST store, POST move: auth, scope (404), the switch (409 BOOKING_DISABLED) and a real move."""
    import json
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient

    from setuhaul.api.app import Settings, create_app
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key()))
    jwk.update(kid="k", alg="RS256", use="sig")
    iss = "https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_SCHED"

    def tok(groups, facs=(SCH,)):
        now = int(time.time())
        return {"Authorization": "Bearer " + jwt.encode(
            {"iss": iss, "sub": f"u-{uuid.uuid4().hex[:6]}", "token_use": "access", "client_id": "c", "iat": now,
             "exp": now + 600, "cognito:groups": list(groups), "facility_scope": ",".join(facs)},
            k, algorithm="RS256", headers={"kid": "k"})}

    def client(enabled):
        return TestClient(create_app(Settings(dsn=sch, cognito_issuer=iss, cognito_client_ids={"c"},
                                              session_secret=b"s" * 40, meta_app_secret=b"m" * 40, facilities=(SCH, SCH2),
                                              jwks={"keys": [jwk]}, booking_enabled=enabled, local_test_harness=True)))
    t = confirmed(conn, S1, 21.0, eta_h=0.5)
    off, on = client(False), client(True)
    g = off.get(f"/console/facilities/{SCH}/schedule-proposal", params={"rule": "EARLIEST_ETA"}, headers=tok(["planner"]))
    assert g.status_code == 200 and g.json()["status"] == "PROPOSAL" and g.json()["rule"] == "EARLIEST_ETA"
    assert off.get(f"/console/facilities/{SCH}/schedule-proposal", headers=tok(["planner"], (SCH2,))).status_code == 404
    assert off.get(f"/console/facilities/{SCH}/schedule-proposal").status_code == 401
    assert off.get(f"/console/facilities/{SCH}/schedule-proposal", params={"rule": "BY_TEXT"},
                   headers=tok(["planner"])).status_code == 422
    s = off.post(f"/console/facilities/{SCH}/schedule-proposals", json={"rule": "FACILITY_SCORE"}, headers=tok(["planner"]))
    assert s.status_code == 200 and s.json()["stored"] is True, s.text
    pid = s.json()["proposal_id"]
    rows = chain(s.json(), t["sid"])
    for row in rows[:-1]:                                                            # its dependencies first
        assert on.post(f"/console/appointments/{row['appointment_id']}/move", headers=tok(["ops_lead"]),
                       json={"proposal_id": pid, "idempotency_key": uuid.uuid4().hex}).status_code == 200
    body = {"proposal_id": pid, "idempotency_key": uuid.uuid4().hex, "note": "pull forward"}
    r = off.post(f"/console/appointments/{t['aid']}/move", json=body, headers=tok(["ops_lead"]))
    assert (r.status_code, r.json()["error"]) == (409, "BOOKING_DISABLED")
    r = on.post(f"/console/appointments/{t['aid']}/move", json=body, headers=tok(["planner"]))
    assert r.status_code == 403
    r = on.post(f"/console/appointments/{t['aid']}/move", json=body, headers=tok(["ops_lead"], (SCH2,)))
    assert r.status_code == 404                                                      # another facility's proposal
    r = on.post(f"/console/appointments/{t['aid']}/move", json=body, headers=tok(["ops_lead"]))
    assert r.status_code == 200 and r.json()["outcome"] == "MOVED", r.text
    assert r.headers["cache-control"] == "no-store"
