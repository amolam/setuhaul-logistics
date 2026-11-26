"""Facility approval mode (migration 0047): MANUAL (default) or AUTO, set per facility by an authorised planner.

MANUAL keeps the planner workflow. AUTO confirms an eligible driver-selected slot at once through the existing
deterministic confirmation path; every mandatory approval still goes to a planner (or a person). The mode is read and
recorded when the driver's confirmed selection is submitted. Database, API, authorization, workflow, failure and
concurrency tests, plus the console's settings view.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

import psycopg
import pytest

from conftest import as_role
from test_approvals import approval, option_at, pending
from test_db_requests import JAI, decide, err, hold, setup_thread, staff, submit

GGN = "FAC-GGN-01"


# ---------------------------------------------------------------- helpers
def mode_row(conn, fac=JAI):
    return conn.execute("""SELECT approval_mode, approval_mode_version, approval_mode_set_by
                             FROM setuhaul.facilities WHERE facility_id=%s""", (fac,)).fetchone()


def set_mode(conn, mode, fac=JAI, roles=("planner",), who="staff:p1", expected=None, reason="test change", scope=None):
    if expected is None:
        expected = mode_row(conn, fac)[1]
    scope = scope or fac
    with as_role(conn, "setuhaul_console", scope) as c:
        staff(c, scope, roles, who)
        return c.execute("SELECT set_facility_approval_mode(%s,%s,%s,%s,%s)",
                         (fac, mode, expected, who, reason)).fetchone()[0]


@pytest.fixture
def auto(conn):
    """JAI in AUTO for one test; back to MANUAL afterwards whatever happens."""
    set_mode(conn, "AUTO")
    try:
        yield
    finally:
        if mode_row(conn)[0] != "MANUAL":
            set_mode(conn, "MANUAL", reason="test cleanup")


def audit(conn, action, fac=JAI):
    return conn.execute("""SELECT principal, principal_role, before_state, after_state, reason FROM setuhaul.booking_events
                           WHERE action=%s AND facility_id=%s ORDER BY event_id DESC LIMIT 1""", (action, fac)).fetchone()


def appt(conn, appointment_id):
    cur = conn.execute("SELECT * FROM setuhaul.appointments WHERE appointment_id=%s", (appointment_id,))
    return dict(zip([d.name for d in cur.description], cur.fetchone()))


# ================================================================ default and MANUAL
def test_every_facility_starts_manual(conn):
    for fac in (JAI, GGN):
        assert mode_row(conn, fac)[0] == "MANUAL"                         # nothing in the migration turns AUTO on


def test_manual_keeps_the_planner_workflow_and_records_the_mode(conn):
    assert mode_row(conn)[0] == "MANUAL"
    t, r = pending(conn)
    a = approval(conn, r["appointment_id"])
    assert r["status"] == "PENDING" and r["approval_mode"] == "MANUAL" and r["auto_approved"] is False
    assert a["status"] == "OPEN" and a["kind"] == "STANDARD"
    assert a["evidence"]["approval_mode"] == "MANUAL" and a["evidence"]["rule_id"] == f"{JAI}:safe-new-booking"
    assert decide(conn, r["appointment_id"], "CONFIRM")["outcome"] == "APPROVED"


# ================================================================ AUTO: eligible slots confirm at once
def test_auto_confirms_an_eligible_slot_through_the_existing_confirmation_path(conn, auto):
    ver = mode_row(conn)[1]
    t, r = pending(conn)
    assert r["status"] == "CONFIRMED" and r["approval_id"] is None and r["auto_approved"] is True
    a = appt(conn, r["appointment_id"])
    assert a["status"] == "CONFIRMED" and a["is_current"] and a["confirmation_basis"] == "POLICY"
    assert a["policy_rule_id"] == f"{JAI}:safe-new-booking"
    ev = a["policy_evidence"]
    assert (ev["approval_mode"], ev["approval_mode_version"], ev["approval_mode_set_by"]) == ("AUTO", ver, "staff:p1")
    assert ev["dispatcher_offer"] is False
    # no planner request, the claim stays active, the confirmation event goes out, and the audit names the mode version
    assert conn.execute("SELECT count(*) FROM setuhaul.approval_requests WHERE appointment_id=%s",
                        (r["appointment_id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT active, kind FROM setuhaul.dock_occupancy WHERE ref_id=%s",
                        (r["appointment_id"],)).fetchone() == (True, "APPOINTMENT")
    assert conn.execute("""SELECT count(*) FROM setuhaul.outbox WHERE aggregate='appointment' AND aggregate_id=%s
                           AND event_type='BookingConfirmed'""", (r["appointment_id"],)).fetchone()[0] == 1
    p, role, _, after, _ = conn.execute("""SELECT principal, principal_role, before_state, after_state, reason
                                             FROM setuhaul.booking_events WHERE action='AUTO_CONFIRM_BOOKING'
                                              AND shipment_id=%s""", (t["sid"],)).fetchone()
    assert p == f"approval-mode:{JAI}@v{ver}" and role == "policy" and after["status"] == "CONFIRMED"


def test_a_replayed_submit_returns_the_same_confirmation(conn, auto):
    t, r = pending(conn)
    key = uuid.uuid4().hex
    t2 = setup_thread(conn)
    option_at(conn, t2)
    hold(conn, t2)
    first = submit(conn, t2, key=key)
    assert first["status"] == "CONFIRMED"
    assert submit(conn, t2, key=key) == first                              # idempotent: nothing twice
    assert conn.execute("""SELECT count(*) FROM setuhaul.booking_events WHERE action='AUTO_CONFIRM_BOOKING'
                           AND shipment_id=%s""", (t2["sid"],)).fetchone()[0] == 1


# ================================================================ AUTO: mandatory approvals take precedence
def test_auto_still_sends_a_critical_load_to_a_planner(conn, auto):
    t = setup_thread(conn)
    conn.execute("UPDATE setuhaul.shipments SET priority_code='CRITICAL' WHERE shipment_id=%s", (t["sid"],))
    option_at(conn, t)
    hold(conn, t)
    r = submit(conn, t)
    a = approval(conn, r["appointment_id"])
    assert r["status"] == "PENDING" and r["auto_approved"] is False and a["kind"] == "PLANNER_REVIEW"
    assert a["evidence"]["approval_mode"] == "AUTO"
    assert "ROLE_NOT_ALLOWED" in str(err(decide, conn, r["appointment_id"], "CONFIRM", JAI, None, ("operations",)))


def test_auto_still_sends_an_after_hours_slot_to_a_planner(conn, auto):
    t, r = pending(conn, needs_approval=True)
    assert r["status"] == "PENDING" and approval(conn, r["appointment_id"])["kind"] == "AFTER_HOURS_START"


def test_auto_never_confirms_when_a_person_is_required(conn, auto):
    t = setup_thread(conn)
    conn.execute("""INSERT INTO setuhaul.escalations (escalation_id,reason_code,mode,facility_id,shipment_id,handoff,ack_due_at)
                    VALUES (gen_random_uuid(),'SAFETY','TAKE_OVER',%s,%s,'{}',now()+interval '10 min')""", (JAI, t["sid"]))
    option_at(conn, t)
    hold(conn, t)
    assert "REQUIRES_PERSON" in str(err(submit, conn, t))
    assert conn.execute("SELECT count(*) FROM setuhaul.appointments WHERE shipment_id=%s", (t["sid"],)).fetchone()[0] == 0


def test_auto_does_not_confirm_a_rule_that_is_not_auto_eligible(conn, auto):
    rid = f"manual-only-{uuid.uuid4().hex[:6]}"
    conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome)
                    VALUES (%s,1,%s,1,'{}','NEEDS_PLANNER')""", (rid, JAI))      # shadows the safe rows, not eligible
    try:
        t, r = pending(conn)
        assert r["status"] == "PENDING" and approval(conn, r["appointment_id"])["evidence"]["rule_id"] == rid
    finally:
        conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome)
                        VALUES (%s,2,%s,1,'{"retired": true}','NEEDS_PLANNER')""", (rid, JAI))


def test_existing_validations_still_refuse_in_auto(conn, auto):
    t = setup_thread(conn)
    option_at(conn, t)
    hold(conn, t)
    conn.execute("UPDATE setuhaul.facilities SET automation_paused=true WHERE facility_id=%s", (JAI,))
    try:
        assert "AUTOMATION_PAUSED" in str(err(submit, conn, t))
    finally:
        conn.execute("UPDATE setuhaul.facilities SET automation_paused=false WHERE facility_id=%s", (JAI,))
    # the driver's attestation is bound to the selected option: another option is refused before anything is booked
    assert "ATTESTATION_INVALID" in str(err(submit, conn, {**t, "oid": uuid.uuid4()}))


# ================================================================ the mode is evaluated when the selection is submitted
def test_switching_mode_never_rewrites_an_earlier_request(conn):
    t, r = pending(conn)                                                   # MANUAL: with a planner
    set_mode(conn, "AUTO")
    try:
        a = approval(conn, r["appointment_id"])
        assert a["status"] == "OPEN" and a["evidence"]["approval_mode"] == "MANUAL"
        t2, r2 = pending(conn, dock="DOCK-JAI-D2")                         # the next selection sees AUTO
        assert r2["status"] == "CONFIRMED"
    finally:
        set_mode(conn, "MANUAL", reason="test cleanup")
    t3, r3 = pending(conn)
    assert r3["status"] == "PENDING"                                       # and MANUAL again after switching back


# ================================================================ authorization (database)
def test_only_planners_and_ops_leads_in_scope_may_change_the_mode(conn):
    assert "ROLE_NOT_ALLOWED" in str(err(set_mode, conn, "AUTO", JAI, ("operations",)))
    assert "ROLE_NOT_ALLOWED" in str(err(set_mode, conn, "AUTO", JAI, ("dispatcher",)))
    assert "FORBIDDEN" in str(err(set_mode, conn, "AUTO", GGN, ("planner",), "staff:p1", None, "x y z", JAI))
    with as_role(conn, "setuhaul_console", JAI) as c:                     # no trusted staff identity
        with pytest.raises(psycopg.Error, match="UNTRUSTED_CONTEXT"):
            c.execute("SELECT set_facility_approval_mode(%s,'AUTO',0,'staff:p1','x y z')", (JAI,))
    with as_role(conn, "setuhaul_console", JAI) as c:                     # the audit principal must be the identity
        staff(c, JAI, ("planner",), "staff:p1")
        with pytest.raises(psycopg.Error, match="PRINCIPAL_MISMATCH"):
            c.execute("SELECT set_facility_approval_mode(%s,'AUTO',0,'staff:someone-else','x y z')", (JAI,))
    for role in ("setuhaul_console", "setuhaul_agent"):                   # nobody writes the column directly
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with as_role(conn, role, JAI) as c:
                c.execute("UPDATE facilities SET approval_mode='AUTO' WHERE facility_id=%s", (JAI,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_role(conn, "setuhaul_agent", JAI) as c:
            c.execute("SELECT set_facility_approval_mode(%s,'AUTO',0,'agent','x y z')", (JAI,))
    assert mode_row(conn)[0] == "MANUAL"


def test_an_ops_lead_may_change_it_and_every_change_is_audited(conn):
    v0 = mode_row(conn)[1]
    out = set_mode(conn, "AUTO", roles=("ops_lead",), who="staff:lead1", reason="night shift trial")
    try:
        assert out["changed"] is True and out["version"] == v0 + 1 and out["set_by"] == "staff:lead1"
        p, role, before, after, reason = audit(conn, "SET_APPROVAL_MODE")
        assert (p, role, reason) == ("staff:lead1", "ops_lead", "night shift trial")
        assert before == {"approval_mode": "MANUAL", "version": v0} and after == {"approval_mode": "AUTO", "version": v0 + 1}
    finally:
        set_mode(conn, "MANUAL", roles=("ops_lead",), who="staff:lead1", reason="test cleanup")


# ================================================================ failures
def test_invalid_mode_missing_reason_and_stale_version_are_refused(conn):
    assert "INVALID_MODE" in str(err(set_mode, conn, "SOMETIMES"))
    assert "REASON_REQUIRED" in str(err(set_mode, conn, "AUTO", JAI, ("planner",), "staff:p1", None, "   "))
    v = mode_row(conn)[1]
    assert "VERSION_CONFLICT" in str(err(set_mode, conn, "AUTO", JAI, ("planner",), "staff:p1", v + 5))
    assert mode_row(conn)[:2] == ("MANUAL", v)


def test_setting_the_current_mode_is_a_no_op_without_an_audit_row(conn):
    n = conn.execute("SELECT count(*) FROM setuhaul.booking_events WHERE action='SET_APPROVAL_MODE'").fetchone()[0]
    v = mode_row(conn)[1]
    out = set_mode(conn, "MANUAL", expected=v + 99)                       # already MANUAL: idempotent, even if stale
    assert out["changed"] is False and out["version"] == v
    assert conn.execute("SELECT count(*) FROM setuhaul.booking_events WHERE action='SET_APPROVAL_MODE'").fetchone()[0] == n


def test_auto_must_name_who_set_it(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE setuhaul.facilities SET approval_mode='AUTO', approval_mode_set_by=NULL WHERE facility_id=%s",
                     (GGN,))


# ================================================================ concurrency
def _set_in_tx(dsn, mode, expected, hold_event=None, release=None, out=None, who="staff:p1"):
    with psycopg.connect(dsn) as c:
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_console")
            c.execute("SET LOCAL search_path = setuhaul")
            c.execute("SELECT set_config('app.facility_scope', %s, true)", (JAI,))
            staff(c, JAI, ("planner",), who)
            try:
                out.append(c.execute("SELECT set_facility_approval_mode(%s,%s,%s,%s,'race')",
                                     (JAI, mode, expected, who)).fetchone()[0])
            except psycopg.Error as e:
                out.append(e.diag.message_primary)
                raise
            finally:
                if hold_event:
                    hold_event.set()
                    release.wait(10)


def test_two_planners_changing_the_mode_at_once_never_both_win(conn, dsn):
    v = mode_row(conn)[1]
    held, release, a, b = threading.Event(), threading.Event(), [], []
    ta = threading.Thread(target=_set_in_tx, args=(dsn, "AUTO", v, held, release, a, "staff:pa"))
    ta.start()
    assert held.wait(10)                                                  # A holds the facility row, uncommitted
    tb = threading.Thread(target=lambda: _safe(_set_in_tx, dsn, "MANUAL", v, None, None, b, "staff:pb"))
    tb.start()
    tb.join(1.0)
    assert tb.is_alive()                                                   # B waits for A's row lock
    release.set()
    ta.join(10)
    tb.join(10)
    try:
        assert a[0]["changed"] is True and a[0]["approval_mode"] == "AUTO"
        assert isinstance(b[0], str) and b[0].startswith("VERSION_CONFLICT")   # B saw a stale version: refused
        assert mode_row(conn)[:2] == ("AUTO", v + 1)
    finally:
        set_mode(conn, "MANUAL", reason="test cleanup")


def _safe(fn, *a):
    try:
        fn(*a)
    except psycopg.Error:
        pass


def test_two_drivers_racing_for_one_slot_in_auto_produce_one_booking(conn, auto):
    """The concurrency protection is unchanged: one dock slot, one winner; the loser's hold is refused."""
    t1, t2 = setup_thread(conn), setup_thread(conn)
    option_at(conn, t1, 11)
    start = t1["start"]
    oid = uuid.uuid4()
    with as_role(conn, "setuhaul_feasibility", JAI) as c:
        c.execute("""INSERT INTO shown_options VALUES (%s,%s,%s,'DOCK-JAI-D1',tstzrange(%s,%s,'[)'),false,1,0,
                     now()+interval '5 min',now())""", (oid, t2["tid"], t2["sid"], start, start.replace(minute=55)))
    t2["oid"] = oid
    hold(conn, t1)
    r1 = submit(conn, t1)
    assert r1["status"] == "CONFIRMED"
    e = err(hold, conn, t2)
    assert e.sqlstate in ("23P01", "P0001")                               # the exclusion constraint / slot taken
    assert conn.execute("""SELECT count(*) FROM setuhaul.dock_occupancy WHERE dock_id='DOCK-JAI-D1' AND active
                           AND during && tstzrange(%s,%s,'[)')""", (start, start.replace(minute=55))).fetchone()[0] == 1


# ================================================================ console UI (static)
STATIC = Path(__file__).resolve().parents[1] / "setuhaul" / "api" / "console_static"


def test_the_console_has_a_planner_settings_view_for_the_mode():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "assets" / "app.js").read_text(encoding="utf-8")
    assert '<button data-t="settings">Facility settings</button>' in html
    assert 'settings: "view_approval_mode"' in js                         # hidden when the server would refuse it
    assert "async function renderSettings(" in js and "/approval-mode`" in js
    assert 'value="${v}"' in js and "Manual: planner approval required for every booking" in js
    assert "Automatic: automatically approve eligible driver-selected slots" in js
    assert "expected_version: m.version" in js and "e.status === 409" in js  # stale edits are refused, not overwritten
    assert 'm.can_edit ? "" : "disabled"' in js                           # read only for roles that cannot edit
