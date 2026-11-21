"""B-WF-2 warehouse approval workflow (design/B-WF-2_approval_design.md revision 3; migration 0015).

The agent can only ask. A request becomes CONFIRMED only when an authenticated warehouse person decides inside
decide_booking_request, before the deadline shared by the request and its claim. Everything here runs through the real
database roles; the booking switch is never turned on.
"""
import itertools
import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from conftest import as_role
from setuhaul.api.db import Db
from setuhaul.workflow.approvals import (STATES, TRANSITIONS, ApprovalOutboxRelay, LocalApprovalWorkflow, asl_shape)
from test_db_requests import (GGN, JAI, confirm, decide, driver_says, err, hold, k, setup_thread, short_deadline,
                              staff, submit)

IST = ZoneInfo("Asia/Kolkata")
_DAY = itertools.count(400)                    # days of their own: no dock contention with other modules


def option_at(conn, t, hour=10, dock="DOCK-JAI-D1", needs_approval=False, minutes=55):
    """An option at a fixed local time on a day no other test uses."""
    start = (datetime.now(IST) + timedelta(days=next(_DAY))).replace(hour=hour, minute=0, second=0, microsecond=0)
    oid = uuid.uuid4()
    with as_role(conn, "setuhaul_feasibility", t["fac"]) as c:
        c.execute("""INSERT INTO shown_options VALUES (%s,%s,%s,%s,tstzrange(%s,%s,'[)'),%s,1,0,now()+interval '5 min',now())""",
                  (oid, t["tid"], t["sid"], dock, start, start + timedelta(minutes=minutes), needs_approval))
    t["oid"], t["start"] = oid, start
    return oid


def pending(conn, hour=10, dock="DOCK-JAI-D1", needs_approval=False):
    t = setup_thread(conn)
    option_at(conn, t, hour, dock, needs_approval)
    hold(conn, t)
    return t, submit(conn, t)


def approval(conn, appt) -> dict:
    cur = conn.execute("SELECT * FROM setuhaul.approval_requests WHERE appointment_id=%s", (appt,))
    return dict(zip([d.name for d in cur.description], cur.fetchone()))


def appt_status(conn, appt):
    return conn.execute("SELECT status FROM setuhaul.appointments WHERE appointment_id=%s", (appt,)).fetchone()[0]


def claim_active(conn, appt):
    return conn.execute("SELECT active FROM setuhaul.dock_occupancy WHERE ref_id=%s", (appt,)).fetchone()[0]


def decided_events(conn, approval_id):
    return conn.execute("""SELECT count(*) FROM setuhaul.outbox WHERE event_type='ApprovalDecided' AND aggregate_id=%s""",
                        (str(approval_id),)).fetchone()[0]


def last_audit(conn, sid):
    return conn.execute("""SELECT principal, principal_role, action FROM setuhaul.booking_events WHERE shipment_id=%s
                           ORDER BY event_id DESC LIMIT 1""", (sid,)).fetchone()


def console(conn, sql, args=(), scope=JAI, roles=("planner",), who="staff:p1"):
    with as_role(conn, "setuhaul_console", scope) as c:
        staff(c, scope, roles, who)
        return c.execute(sql, args).fetchone()[0]


def withdraw(conn, appt, code="DRIVER_REQUESTED", roles=("operations",)):
    return console(conn, "SELECT withdraw_booking_request(%s,%s,%s,%s)", (appt, code, "driver called the dock office", k()),
                   roles=roles, who="staff:coord1")


# ================================================================ creation, states, deadline (§2, §3, §6)
def test_submit_creates_one_open_approval_bound_to_the_appointment_and_its_claim(conn):
    t, r = pending(conn)
    a = approval(conn, r["appointment_id"])
    assert r["status"] == "PENDING" and str(a["approval_id"]) == r["approval_id"]
    assert a["status"] == "OPEN" and a["kind"] == "STANDARD" and a["thread_id"] == t["tid"]
    occ = conn.execute("SELECT occupancy_id, kind, active, expires_at FROM setuhaul.dock_occupancy WHERE ref_id=%s",
                       (r["appointment_id"],)).fetchone()
    assert occ == (a["claim_occupancy_id"], "APPOINTMENT", True, None)          # the claim has no expiry of its own
    pe = conn.execute("SELECT pending_expires_at FROM setuhaul.appointments WHERE appointment_id=%s",
                      (r["appointment_id"],)).fetchone()[0]
    assert pe == a["expires_at"]
    # §6: deadline = least(TTL from now, slot start − lead time); the hold's own expiry plays no part
    assert a["expires_at"] <= t["start"] - timedelta(minutes=30)
    assert a["expires_at"] <= datetime.now(IST) + timedelta(minutes=15, seconds=5)
    ev = a["evidence"]
    assert ev["attestation_id"] == t["att"] and ev["rule_outcome"] == "NEEDS_PLANNER" and ev["inputs"]["kind"] == "STANDARD"


def test_the_database_classifies_after_hours_itself(conn):
    t, r = pending(conn, needs_approval=True)
    assert approval(conn, r["appointment_id"])["kind"] == "AFTER_HOURS_START"
    conn.execute("UPDATE setuhaul.facilities SET close_time='09:00' WHERE facility_id=%s", (JAI,))
    try:                                     # the option says nothing about hours: the database still sees 10:00 > 09:00
        t2 = setup_thread(conn)
        option_at(conn, t2, hour=10, dock="DOCK-JAI-D2")
        hold(conn, t2)
        r2 = submit(conn, t2)
        assert approval(conn, r2["appointment_id"])["kind"] == "AFTER_HOURS_START"
    finally:
        conn.execute("UPDATE setuhaul.facilities SET close_time='23:59' WHERE facility_id=%s", (JAI,))


def test_one_open_request_per_shipment(conn):
    t, r = pending(conn)
    option_at(conn, t, dock="DOCK-JAI-D2")
    try:
        hold(conn, t)                                                        # holding may already be refused...
        e = err(submit, conn, t)                                             # ...submitting certainly is
    except psycopg.Error as x:
        e = x
    assert "FINISH_CURRENT_BOOKING_FIRST" in str(e), str(e)
    n = conn.execute("SELECT count(*) FROM setuhaul.approval_requests WHERE shipment_id=%s AND status='OPEN'",
                     (t["sid"],)).fetchone()[0]
    assert n == 1


def test_a_final_state_is_permanent_and_rows_are_never_deleted(conn):
    t, r = pending(conn)
    decide(conn, r["appointment_id"], "REJECT")
    for sql in ("UPDATE setuhaul.approval_requests SET status='OPEN', decided_at=NULL WHERE appointment_id=%s",
                "UPDATE setuhaul.approval_requests SET outcome_reason='edited' WHERE appointment_id=%s",
                "DELETE FROM setuhaul.approval_requests WHERE appointment_id=%s"):
        e = err(conn.execute, sql, (r["appointment_id"],))                       # even the table owner
        assert "FINAL_STATE" in str(e) or "APPROVAL_IMMUTABLE" in str(e), sql


def test_open_requests_cannot_be_edited_except_by_their_outcome(conn):
    t, r = pending(conn)
    for sql in ("UPDATE setuhaul.approval_requests SET expires_at = expires_at + interval '1 hour' WHERE appointment_id=%s",
                "UPDATE setuhaul.approval_requests SET kind = 'STANDARD', facility_id = 'FAC-GGN-01' WHERE appointment_id=%s"):
        assert "APPROVAL_IMMUTABLE" in str(err(conn.execute, sql, (r["appointment_id"],))), sql


def test_commit_time_check_refuses_an_open_request_without_its_claim(conn):
    """§6 E: whatever path deactivates the claim, the transaction fails unless the request closes with it."""
    t, r = pending(conn)
    with pytest.raises(psycopg.Error, match="OPEN_REQUEST_WITHOUT_CLAIM"):
        with conn.transaction():
            conn.execute("UPDATE setuhaul.dock_occupancy SET active=false WHERE ref_id=%s", (r["appointment_id"],))
    with pytest.raises(psycopg.Error, match="APPROVAL_INCONSISTENT"):
        with conn.transaction():
            conn.execute("UPDATE setuhaul.appointments SET status='CONFIRMED' WHERE appointment_id=%s", (r["appointment_id"],))
    assert approval(conn, r["appointment_id"])["status"] == "OPEN" and claim_active(conn, r["appointment_id"])


# ================================================================ approve / reject (§3, §5, §8, §18)
def test_approve_confirms_with_the_session_principal_one_event_and_an_audit_row(conn):
    t, r = pending(conn)
    d = decide(conn, r["appointment_id"], "CONFIRM")
    a = approval(conn, r["appointment_id"])
    assert d["outcome"] == "APPROVED" and a["status"] == "APPROVED" and appt_status(conn, r["appointment_id"]) == "CONFIRMED"
    assert (a["decided_by"], a["decided_by_type"], a["decided_by_role"]) == ("staff:p1", "HUMAN", "planner")
    assert decided_events(conn, a["approval_id"]) == 1
    assert last_audit(conn, t["sid"]) == ("staff:p1", "planner", "APPROVAL_APPROVED")
    basis = conn.execute("SELECT confirmation_basis FROM setuhaul.appointments WHERE appointment_id=%s",
                         (r["appointment_id"],)).fetchone()[0]
    assert basis == "HUMAN"


def test_reject_releases_the_claim_and_records_the_decider(conn):
    t, r = pending(conn)
    decide(conn, r["appointment_id"], "REJECT")
    a = approval(conn, r["appointment_id"])
    assert a["status"] == "REJECTED" and appt_status(conn, r["appointment_id"]) == "REJECTED"
    assert not claim_active(conn, r["appointment_id"]) and decided_events(conn, a["approval_id"]) == 1
    assert last_audit(conn, t["sid"]) == ("staff:p1", "planner", "APPROVAL_REJECTED")


@pytest.mark.parametrize("roles,kind_after_hours,ok", [
    (("operations",), False, True), (("operations",), True, False),
    (("planner",), True, True), (("ops_lead",), True, True)])
def test_q2_the_role_needed_depends_on_the_request_kind(conn, roles, kind_after_hours, ok):
    t, r = pending(conn, needs_approval=kind_after_hours)
    if ok:
        assert decide(conn, r["appointment_id"], "CONFIRM", roles=roles)["outcome"] == "APPROVED"
        assert approval(conn, r["appointment_id"])["decided_by_role"] == roles[0]
    else:
        assert "ROLE_NOT_ALLOWED" in str(err(decide, conn, r["appointment_id"], "CONFIRM", JAI, None, roles))
        assert approval(conn, r["appointment_id"])["status"] == "OPEN"


def test_q2_role_rules_match_the_agreed_table(conn):
    rows = set(conn.execute("SELECT kind, role FROM setuhaul.approval_role_rules"))
    assert {r for k_, r in rows if k_ == "STANDARD"} == {"operations", "planner", "ops_lead"}
    assert {r for k_, r in rows if k_ == "AFTER_HOURS_START"} == {"planner", "ops_lead"}
    assert {r for k_, r in rows if k_ == "DOCK_DOWNGRADE"} == {"planner", "ops_lead"}
    assert {r for k_, r in rows if k_ == "MOVE_CONFIRMED_PROMISE"} == {"ops_lead"}
    assert {r for k_, r in rows if k_ == "PRIORITY_OVERRIDE"} == {"ops_lead"}


def test_b_the_database_re_proves_the_booking_without_the_service(conn):
    """§5: called directly (no service feasibility check), the function refuses what it must refuse."""
    t, r = pending(conn)
    conn.execute("UPDATE setuhaul.shipments SET current_status='IN_DOCK' WHERE shipment_id=%s", (t["sid"],))
    a = approval(conn, r["appointment_id"])                  # the shipment change already invalidated it (§6)
    assert a["status"] == "INVALIDATED" and a["outcome_reason"] == "SHIPMENT_CHANGED"
    assert "ALREADY_DECIDED" in str(err(decide, conn, r["appointment_id"], "CONFIRM"))


# ================================================================ §14 the database clock
def test_s14_a_transaction_that_started_before_the_deadline_cannot_approve_after_it(conn, dsn):
    t, r = short_deadline(conn, seconds=3)
    with psycopg.connect(dsn, autocommit=True) as c2:
        with pytest.raises(psycopg.Error, match="PENDING_EXPIRED"):
            with as_role(c2, "setuhaul_console", JAI) as c:
                staff(c)
                q = "SELECT now() < expires_at FROM approval_requests WHERE appointment_id=%s"
                assert c.execute(q, (r["appointment_id"],)).fetchone()[0] is True    # started before the deadline
                time.sleep(3.5)
                assert c.execute(q, (r["appointment_id"],)).fetchone()[0] is True    # now() still says "before"
                c.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,NULL)", (r["appointment_id"], k()))
    assert approval(conn, r["appointment_id"])["status"] == "OPEN"                  # nothing changed; the sweep will expire it
    with as_role(conn, "setuhaul_system", f"{JAI},{GGN}") as c:
        c.execute("SELECT expire_pending_requests()")
    assert approval(conn, r["appointment_id"])["status"] == "EXPIRED"


def test_expiry_closes_the_request_and_its_claim_with_the_system_principal(conn):
    t, r = short_deadline(conn, seconds=2)
    time.sleep(2.3)
    with as_role(conn, "setuhaul_system", f"{JAI},{GGN}") as c:
        assert c.execute("SELECT expire_pending_requests()").fetchone()[0] >= 1
    a = approval(conn, r["appointment_id"])
    assert (a["status"], a["decided_by"], a["decided_by_type"]) == ("EXPIRED", "system:expiry", "SYSTEM")
    assert appt_status(conn, r["appointment_id"]) == "EXPIRED" and not claim_active(conn, r["appointment_id"])
    assert decided_events(conn, a["approval_id"]) == 1
    assert last_audit(conn, t["sid"]) == ("system:expiry", "system", "APPROVAL_EXPIRED")


# ================================================================ §15 trusted identity
@pytest.mark.parametrize("role", ["setuhaul_agent", "setuhaul_feasibility", "setuhaul_ingest", "setuhaul_system"])
def test_s15_no_other_role_can_manufacture_an_ops_lead_or_approve(conn, role):
    t, r = pending(conn)
    appt = r["appointment_id"]

    def attempt(sql, params):
        with as_role(conn, role, JAI, t["drv"]) as c:
            c.execute("""SELECT set_config('app.staff_roles','ops_lead',true), set_config('app.principal','staff:boss',true),
                                set_config('app.facility_scope','FAC-JAI-01',true)""")
            c.execute(sql, params)

    for sql, params in (
            ("SELECT decide_booking_request(%s,'CONFIRM',%s,NULL)", (appt, k())),
            ("SELECT begin_staff_context('staff:boss', ARRAY['ops_lead'], ARRAY['FAC-JAI-01'])", ()),
            ("INSERT INTO staff_context VALUES (txid_current(), pg_backend_pid(), 'staff:boss', ARRAY['ops_lead'], "
             "ARRAY['FAC-JAI-01'], now())", ()),
            ("SELECT withdraw_booking_request(%s,'DATA_CORRECTION','spoofed',%s)", (appt, k())),
            ("SELECT set_booking_hold('FAC-JAI-01', NULL, 'spoofed')", ()),
            ("UPDATE approval_requests SET status='APPROVED' WHERE appointment_id=%s", (appt,))):
        e = err(attempt, sql, params)
        assert e.sqlstate == "42501", (role, sql, str(e))
    assert approval(conn, appt)["status"] == "OPEN"


def test_s15_the_console_needs_a_context_from_this_transaction(conn):
    t, r = pending(conn)
    appt = r["appointment_id"]
    sql = "SELECT decide_booking_request(%s,'CONFIRM',%s,NULL)"
    with pytest.raises(psycopg.Error, match="UNTRUSTED_CONTEXT"):                  # settings alone are not an identity
        with as_role(conn, "setuhaul_console", JAI) as c:
            c.execute("SELECT set_config('app.staff_roles','ops_lead',true), set_config('app.principal','staff:boss',true)")
            c.execute(sql, (appt, k()))
    with as_role(conn, "setuhaul_console", JAI) as c:                              # a pooled connection: tx 1 commits...
        staff(c, roles=("ops_lead",))
    with pytest.raises(psycopg.Error, match="UNTRUSTED_CONTEXT"):                  # ...tx 2 on the same connection has none
        with as_role(conn, "setuhaul_console", JAI) as c:
            c.execute(sql, (appt, k()))
    with pytest.raises(psycopg.Error, match="UNTRUSTED_CONTEXT"):                  # rolled back = never existed
        with as_role(conn, "setuhaul_console", JAI) as c:
            c.execute("SAVEPOINT s1")
            staff(c, roles=("ops_lead",))
            c.execute("ROLLBACK TO SAVEPOINT s1")
            c.execute(sql, (appt, k()))
    assert "APPOINTMENT_UNKNOWN" in str(err(decide, conn, appt, "CONFIRM", GGN))      # another facility's identity
    for bad in ((["superadmin"], [JAI]), (["ops_lead"], []), (["ops_lead"], ["FAC-NOPE"])):
        with pytest.raises(psycopg.Error, match="BAD_STAFF_CONTEXT"):
            with as_role(conn, "setuhaul_console", JAI) as c:
                c.execute("SELECT begin_staff_context('staff:x', %s, %s)", bad)
    with pytest.raises(psycopg.Error, match="BAD_STAFF_CONTEXT"):                  # one identity per transaction
        with as_role(conn, "setuhaul_console", JAI) as c:
            staff(c)
            staff(c, roles=("ops_lead",), who="staff:other")
    assert approval(conn, appt)["status"] == "OPEN"


def test_s15_the_db_layer_only_attaches_an_identity_to_console_writes(dsn):
    db = Db(dsn)
    for role, ro in (("agent", False), ("system", False), ("console", True)):
        with pytest.raises(ValueError):
            with db.tx(role, (JAI,), readonly=ro, staff=("staff:x", {"ops_lead"}, (JAI,))):
                pass


# ================================================================ §6 D: invalidation
def test_d_a_dock_block_over_the_claim_invalidates_in_the_same_transaction(conn):
    t, r = pending(conn, dock="DOCK-JAI-D3")
    s = t["start"]
    console(conn, "SELECT block_dock(%s,'DOCK-JAI-D3',%s,%s,'BREAKDOWN','hydraulics','staff:ops1')",
            (f"DE-{uuid.uuid4().hex[:8]}", s - timedelta(minutes=30), s + timedelta(hours=1)), who="staff:ops1")
    a = approval(conn, r["appointment_id"])
    assert (a["status"], a["outcome_reason"], a["decided_by"], a["decided_by_type"]) == \
        ("INVALIDATED", "CAPACITY_LOST", "staff:ops1", "HUMAN")
    assert appt_status(conn, r["appointment_id"]) == "CANCELLED" and decided_events(conn, a["approval_id"]) == 1


def test_d_shipment_dock_and_hours_changes_invalidate_with_a_system_principal(conn):
    t1, r1 = pending(conn)
    conn.execute("UPDATE setuhaul.shipments SET current_status='CANCELLED' WHERE shipment_id=%s", (t1["sid"],))
    a1 = approval(conn, r1["appointment_id"])
    assert (a1["status"], a1["outcome_reason"], a1["decided_by"]) == ("INVALIDATED", "SHIPMENT_CHANGED", "system:shipment_changed")

    t2, r2 = pending(conn, dock="DOCK-JAI-D2")
    conn.execute("UPDATE setuhaul.docks SET max_vehicle_weight_kg = 29000 WHERE dock_id='DOCK-JAI-D2'")
    conn.execute("UPDATE setuhaul.docks SET max_vehicle_weight_kg = 30000 WHERE dock_id='DOCK-JAI-D2'")
    a2 = approval(conn, r2["appointment_id"])
    assert (a2["status"], a2["outcome_reason"]) == ("INVALIDATED", "DOCK_UNAVAILABLE")

    t3, r3 = pending(conn, hour=10)
    conn.execute("UPDATE setuhaul.facilities SET close_time='09:00' WHERE facility_id=%s", (JAI,))
    conn.execute("UPDATE setuhaul.facilities SET close_time='23:59' WHERE facility_id=%s", (JAI,))
    a3 = approval(conn, r3["appointment_id"])
    assert (a3["status"], a3["outcome_reason"], a3["decided_by_type"]) == ("INVALIDATED", "OUTSIDE_HOURS", "SYSTEM")
    for a in (a1, a2, a3):
        assert decided_events(conn, a["approval_id"]) == 1


# ================================================================ §7 withdraw vs supersede
def test_q4_withdrawal_records_the_reason_and_the_coordinator(conn):
    t, r = pending(conn)
    driver_says(conn, t, "cancel kar do")                    # a later driver message does not cancel it silently
    assert approval(conn, r["appointment_id"])["status"] == "OPEN"
    with pytest.raises(psycopg.Error, match="BAD_REASON_CODE"):
        withdraw(conn, r["appointment_id"], code="BECAUSE")
    w = withdraw(conn, r["appointment_id"])
    a = approval(conn, r["appointment_id"])
    assert w["outcome"] == "WITHDRAWN"
    assert (a["status"], a["withdraw_reason_code"], a["decided_by"], a["decided_by_role"]) == \
        ("WITHDRAWN", "DRIVER_REQUESTED", "staff:coord1", "operations")
    assert a["decision_note"] == "driver called the dock office" and not claim_active(conn, r["appointment_id"])
    assert "ALREADY_DECIDED" in str(err(withdraw, conn, r["appointment_id"]))


def test_q4_a_newer_booking_supersedes_and_is_recorded_as_such(conn):
    t, r = pending(conn)
    newer = f"APT-{uuid.uuid4().hex}"
    start = t["start"] + timedelta(hours=3)
    with conn.transaction():
        conn.execute("SELECT setuhaul.begin_staff_context('staff:planner9', ARRAY['planner'], ARRAY['FAC-JAI-01'])")
        occ = conn.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                              VALUES ('DOCK-JAI-D2','FAC-JAI-01',tstzrange(%s,%s,'[)'),'APPOINTMENT',%s,%s,'staff:planner9')
                              RETURNING occupancy_id""", (start, start + timedelta(minutes=55), t["sid"], newer)).fetchone()[0]
        conn.execute("""INSERT INTO setuhaul.appointments (appointment_id,shipment_id,dock_id,during,occupancy_id,status,is_current,
                                                           booking_source,confirmation_basis)
                        VALUES (%s,%s,'DOCK-JAI-D2',tstzrange(%s,%s,'[)'),%s,'CONFIRMED',true,'WAREHOUSE','HUMAN')""",
                     (newer, t["sid"], start, start + timedelta(minutes=55), occ))
    a = approval(conn, r["appointment_id"])
    assert (a["status"], a["superseded_by"], a["decided_by"]) == ("SUPERSEDED", newer, "staff:planner9")
    assert a["withdraw_reason_code"] is None and appt_status(conn, r["appointment_id"]) == "CANCELLED"


# ================================================================ §11 Q3 holds and pauses
def test_q3_an_operational_hold_blocks_approval_and_new_requests_but_not_rejection(conn):
    t, r = pending(conn)
    t2, r2 = pending(conn, dock="DOCK-JAI-D2")
    assert "ROLE_NOT_ALLOWED" in str(err(console, conn, "SELECT set_booking_hold(%s,NULL,'flooded yard')", (JAI,)))
    hid = console(conn, "SELECT set_booking_hold(%s,NULL,'flooded yard')", (JAI,), roles=("ops_lead",), who="staff:lead1")
    try:
        assert "OPERATIONAL_HOLD" in str(err(decide, conn, r["appointment_id"], "CONFIRM"))
        assert decide(conn, r2["appointment_id"], "REJECT")["outcome"] == "REJECTED"
        t3 = setup_thread(conn)
        option_at(conn, t3, dock="DOCK-JAI-D3")
        hold(conn, t3)
        assert "OPERATIONAL_HOLD" in str(err(submit, conn, t3))
    finally:
        assert console(conn, "SELECT clear_booking_hold(%s,'yard dry')", (hid,), roles=("ops_lead",), who="staff:lead1") is True
    assert decide(conn, r["appointment_id"], "CONFIRM")["outcome"] == "APPROVED"


def test_q3_an_automation_pause_does_not_stop_people(conn):
    t, r = pending(conn)
    console(conn, "SELECT set_automation_paused(%s,true,'staff:lead1','storm')", (JAI,), roles=("ops_lead",), who="staff:lead1")
    try:
        assert decide(conn, r["appointment_id"], "CONFIRM")["outcome"] == "APPROVED"
    finally:
        console(conn, "SELECT set_automation_paused(%s,false,'staff:lead1','clear')", (JAI,), roles=("ops_lead",), who="staff:lead1")


# ================================================================ §4 A: the driver's YES
def test_a_the_yes_is_used_once_and_bound_to_driver_and_slot(conn):
    t, r = pending(conn)
    a = conn.execute("""SELECT driver_id, shipment_id, dock_id, consumed_by_appointment FROM setuhaul.confirmation_attestations
                        WHERE attestation_id=%s""", (t["att"],)).fetchone()
    assert a == (t["drv"], t["sid"], "DOCK-JAI-D1", r["appointment_id"])
    decide(conn, r["appointment_id"], "REJECT")
    assert "ATTESTATION_USED" in str(err(submit, conn, t, t["att"], k()))

    t2 = setup_thread(conn)
    option_at(conn, t2, dock="DOCK-JAI-D2")
    att = confirm(conn, t2)
    with as_role(conn, "setuhaul_agent", t2["fac"], "DRV-someone-else") as c:     # another driver's session
        with pytest.raises(psycopg.Error, match="ATTESTATION_INVALID|THREAD_UNKNOWN|OPTION_UNKNOWN"):
            c.execute("SELECT hold_option(%s,%s,%s,%s,'agent')", (t2["oid"], t2["tid"], att, k()))
    conn.execute("ALTER TABLE setuhaul.confirmation_attestations DISABLE TRIGGER attestation_snapshot")
    try:                                                                            # a snapshot that no longer matches
        conn.execute("UPDATE setuhaul.confirmation_attestations SET slot = tstzrange(now(), now()+interval '1 h') "
                     "WHERE attestation_id=%s", (att,))
    finally:
        conn.execute("ALTER TABLE setuhaul.confirmation_attestations ENABLE TRIGGER attestation_snapshot")
    assert "ATTESTATION_INVALID" in str(err(hold, conn, t2, att))


# ================================================================ §10 / §19 policy confirmation (Q1)
def test_q1_every_rule_ships_needing_a_planner(conn):
    rows = conn.execute("""SELECT DISTINCT ON (rule_id) rule_id, outcome FROM setuhaul.confirmation_rules
                           WHERE created_by LIKE 'migration:%' ORDER BY rule_id, version DESC""").fetchall()
    assert rows and all(o in ("NEEDS_PLANNER", "PERSON_ALWAYS") for _, o in rows)
    assert "APPEND_ONLY" in str(err(conn.execute, "UPDATE setuhaul.confirmation_rules SET outcome='POLICY_CONFIRM'"))


def test_q1_a_policy_confirmation_is_never_an_approval_and_keeps_its_evidence(conn):
    rid = f"test-{uuid.uuid4().hex[:6]}"
    conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome,
                                                              auto_eligible,authorised_by)
                    VALUES (%s,1,%s,1,'{"kind":"STANDARD"}','POLICY_CONFIRM',true,'TEST-ONLY')""", (rid, JAI))
    try:
        t = setup_thread(conn)
        option_at(conn, t)
        hold(conn, t)
        r = submit(conn, t)
        assert r["status"] == "CONFIRMED" and r["basis"] == "POLICY" and r["approval_id"] is None
        row = conn.execute("""SELECT confirmation_basis, policy_rule_id, policy_rule_version, policy_evidence
                              FROM setuhaul.appointments WHERE appointment_id=%s""", (r["appointment_id"],)).fetchone()
        assert row[:3] == ("POLICY", rid, 1)
        ev = row[3]
        assert ev["attestation_id"] == t["att"] and ev["inputs"]["kind"] == "STANDARD" and ev["rule_condition"] == {"kind": "STANDARD"}
        assert ev["evaluated_at"]
        n = conn.execute("SELECT count(*) FROM setuhaul.approval_requests WHERE shipment_id=%s", (t["sid"],)).fetchone()[0]
        assert n == 0                                                       # no approval row, so never "APPROVED"
        assert last_audit(conn, t["sid"]) == (f"policy:{rid}@v1", "policy", "POLICY_CONFIRM_BOOKING")
        t2, r2 = pending(conn, needs_approval=True, dock="DOCK-JAI-D2")       # after-hours is never policy-confirmed
        assert r2["status"] == "PENDING"
    finally:
        conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome)
                        VALUES (%s,2,%s,1,'{"retired": true}','NEEDS_PLANNER')""", (rid, JAI))   # matches nothing


# ================================================================ races (§16): exactly one outcome, no deadlock
def _race(dsn, *calls):
    out, gate = [None] * len(calls), threading.Barrier(len(calls))

    def run(i, fn):
        with psycopg.connect(dsn, autocommit=True) as c:
            gate.wait()
            try:
                out[i] = ("ok", fn(c))
            except psycopg.Error as e:
                out[i] = ("err", e.sqlstate, (e.diag.message_primary or "").split(":")[0])

    ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(calls)]
    [x.start() for x in ts]
    [x.join(30) for x in ts]
    assert all(o and o[1:2] != ("40P01",) for o in out), out                 # no deadlock
    return out


@pytest.mark.parametrize("other", ["approve", "reject", "withdraw", "block"])
def test_races_resolve_to_exactly_one_outcome(conn, dsn, other):
    t, r = pending(conn, dock="DOCK-JAI-D3")
    appt, s = r["appointment_id"], t["start"]
    calls = {
        "approve": lambda c: decide(c, appt, "CONFIRM"),
        "reject": lambda c: decide(c, appt, "REJECT"),
        "withdraw": lambda c: withdraw(c, appt),
        "block": lambda c: console(c, "SELECT block_dock(%s,'DOCK-JAI-D3',%s,%s,'BREAKDOWN','x','staff:p1')",
                                   (f"DE-{uuid.uuid4().hex[:8]}", s, s + timedelta(minutes=30))),
    }
    out = _race(dsn, calls["approve"], calls[other])
    a = approval(conn, appt)
    assert a["status"] != "OPEN" and decided_events(conn, a["approval_id"]) == 1, out
    oks = [o for o in out if o[0] == "ok"]
    if other == "block" and a["status"] == "APPROVED":
        assert out[1][1] == "23P01"                                          # approved first: the block meets a booking
    else:
        assert len(oks) == 1 or other == "approve" and len(oks) == 1, out


def test_race_approve_vs_the_sweep_at_the_deadline(conn, dsn):
    t, r = short_deadline(conn, seconds=2)
    time.sleep(2.2)
    out = _race(dsn, lambda c: decide(c, r["appointment_id"], "CONFIRM"),
                lambda c: [as_role, c.execute("SET ROLE setuhaul_system"),
                           c.execute("SELECT set_config('app.facility_scope', %s, false)", (f"{JAI},{GGN}",)),
                           c.execute("SELECT setuhaul.expire_pending_requests()").fetchone()[0]][-1])
    a = approval(conn, r["appointment_id"])
    if a["status"] == "OPEN":                                                # the sweep skipped the locked row
        with as_role(conn, "setuhaul_system", f"{JAI},{GGN}") as c:
            c.execute("SELECT expire_pending_requests()")
        a = approval(conn, r["appointment_id"])
    assert a["status"] == "EXPIRED" and out[0][0] == "err" and out[0][2] in ("PENDING_EXPIRED", "ALREADY_DECIDED"), out
    assert decided_events(conn, a["approval_id"]) == 1


# ================================================================ §17 events and idempotent consumers
def test_s17_one_approval_decided_event_per_approval(conn):
    t, r = pending(conn)
    d = decide(conn, r["appointment_id"], "CONFIRM")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("""INSERT INTO setuhaul.outbox (facility_id, aggregate, aggregate_id, event_type, payload)
                        VALUES (%s,'approval',%s,'ApprovalDecided','{}')""", (JAI, d["approval_id"]))


def test_s17_consuming_an_event_twice_has_one_effect(conn):
    t, r = pending(conn)
    eid = conn.execute("""SELECT event_id FROM setuhaul.outbox WHERE aggregate_id=%s AND event_type='BookingRequested'""",
                       (r["appointment_id"],)).fetchone()[0]
    with as_role(conn, "setuhaul_system", JAI) as c:
        assert c.execute("SELECT consume_outbox_event('test-consumer', %s)", (eid,)).fetchone()[0] is True
    with as_role(conn, "setuhaul_system", JAI) as c:
        assert c.execute("SELECT consume_outbox_event('test-consumer', %s)", (eid,)).fetchone()[0] is False
    with as_role(conn, "setuhaul_agent", JAI, t["drv"]) as c:
        with pytest.raises(psycopg.Error) as e:
            c.execute("SELECT consume_outbox_event('test-consumer', %s)", (eid,))
    assert e.value.sqlstate == "42501"


# ================================================================ §9 the workflow only waits
def test_the_asl_definition_matches_the_local_workflow():
    states, edges = asl_shape()
    assert states == set(STATES) and edges == TRANSITIONS


def test_workflow_start_is_idempotent_and_a_decision_ends_it(conn, dsn):
    db = Db(dsn)
    wf = LocalApprovalWorkflow(db)
    relay = ApprovalOutboxRelay(db, wf)
    t, r = pending(conn)
    relay.run_once()
    relay.run_once()                                                         # redelivery: no second execution
    ex = wf.executions[r["approval_id"]]
    assert ex.state == "WaitForDecision" and ex.history == ["NotifyApprovers"]
    assert wf.start(r["approval_id"], ex.deadline) is False
    decide(conn, r["appointment_id"], "CONFIRM")
    relay.run_once()
    assert ex.state == "Done" and ex.outcome == "CALLBACK"


def test_c_the_outcome_and_its_event_exist_with_the_workflow_stopped(conn, dsn):
    """C: notification reads ApprovalDecided from the outbox; it never depends on the workflow."""
    wf = LocalApprovalWorkflow(Db(dsn))
    wf.stopped = True
    t, r = pending(conn)
    decide(conn, r["appointment_id"], "CONFIRM")
    a = approval(conn, r["appointment_id"])
    payload = conn.execute("""SELECT payload FROM setuhaul.outbox WHERE event_type='ApprovalDecided' AND aggregate_id=%s""",
                           (str(a["approval_id"]),)).fetchone()[0]
    assert payload["outcome"] == "APPROVED" and payload["appointment_id"] == r["appointment_id"]
    assert wf.executions == {}


def test_workflow_timeout_expires_in_postgres_and_changes_nothing_when_already_decided(conn, dsn):
    db = Db(dsn)
    t1, r1 = short_deadline(conn, seconds=2)
    wf = LocalApprovalWorkflow(db)
    ApprovalOutboxRelay(db, wf).run_once()
    time.sleep(2.3)
    wf.tick()
    ex = wf.executions[r1["approval_id"]]
    assert ex.outcome == "EXPIRED" and ex.state == "Done" and approval(conn, r1["appointment_id"])["status"] == "EXPIRED"

    t2, r2 = pending(conn, dock="DOCK-JAI-D2")
    decide(conn, r2["appointment_id"], "REJECT")
    with as_role(conn, "setuhaul_system", JAI) as c:                       # a late timer: PostgreSQL already decided
        assert c.execute("SELECT expire_approval(%s)", (r2["approval_id"],)).fetchone()[0] == "REJECTED"
    assert approval(conn, r2["appointment_id"])["status"] == "REJECTED"


def test_the_reconciler_finishes_an_execution_whose_callback_was_lost(conn, dsn):
    db = Db(dsn)
    wf = LocalApprovalWorkflow(db)
    t, r = pending(conn)
    wf.start(r["approval_id"], datetime.now(IST) + timedelta(minutes=10))
    decide(conn, r["appointment_id"], "REJECT")                            # no relay run: the callback is "lost"
    assert wf.reconcile() == 1 and wf.executions[r["approval_id"]].state == "Done"


# ================================================================ the agent never approves
def test_the_agent_cannot_decide_withdraw_hold_or_read_tokens(conn):
    t, r = pending(conn)
    with as_role(conn, "setuhaul_agent", JAI, t["drv"]) as c:
        assert c.execute("SELECT has_column_privilege('setuhaul_agent','setuhaul.approval_requests','task_token','SELECT')"
                         ).fetchone()[0] is False
    for fn in ("decide_booking_request(text,text,text,text)", "withdraw_booking_request(text,text,text,text)",
               "set_booking_hold(text,text,text)", "clear_booking_hold(bigint,text)",
               "begin_staff_context(text,text[],text[])", "expire_approval(uuid)", "record_task_token(uuid,text)"):
        ok = conn.execute("SELECT has_function_privilege('setuhaul_agent', %s, 'EXECUTE')", (f"setuhaul.{fn}",)).fetchone()[0]
        assert ok is False, fn


def test_console_functions_are_console_only(conn):
    for fn in ("decide_booking_request(text,text,text,text)", "withdraw_booking_request(text,text,text,text)",
               "set_booking_hold(text,text,text)", "clear_booking_hold(bigint,text)", "begin_staff_context(text,text[],text[])"):
        who = {r[0] for r in conn.execute("""SELECT r.rolname FROM pg_roles r WHERE r.rolname LIKE 'setuhaul_%%'
                                              AND has_function_privilege(r.oid, %s::regprocedure, 'EXECUTE')""",
                                          (f"setuhaul.{fn}",))}
        assert who == {"setuhaul_console", "setuhaul_rpc"}, (fn, who)
    raw = json.dumps(sorted(r[0] for r in conn.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants WHERE grantee='setuhaul_console' "
        "AND table_name IN ('approval_requests','staff_context','booking_holds')")))
    assert "INSERT" not in raw and "UPDATE" not in raw and "DELETE" not in raw


# ================================================================ R3-P1-01 every outcome respects the deadline after its locks
def _past_the_deadline_while_waiting_on_a_lock(conn, dsn, appt, action):
    """`action(c)` starts before the deadline, then waits on the appointment's row lock until after it."""
    exp = approval(conn, appt)["expires_at"]
    locker = psycopg.connect(dsn)
    locker.execute("SELECT 1 FROM setuhaul.appointments WHERE appointment_id=%s FOR UPDATE", (appt,))
    out = {}

    def go():
        try:
            with psycopg.connect(dsn, autocommit=True) as c2:
                out["ok"] = action(c2)
        except psycopg.Error as e:
            out["err"] = e
    th = threading.Thread(target=go)
    th.start()
    time.sleep(0.4)
    remaining = (exp - datetime.now(exp.tzinfo)).total_seconds()
    assert th.is_alive() and remaining > 0, "the action must start before the deadline and wait on the lock"
    time.sleep(remaining + 0.5)
    locker.rollback()
    locker.close()
    th.join(20)
    return out


def _expired_escalation(conn, approval_id):
    return conn.execute("""SELECT handoff->>'requested_outcome' FROM setuhaul.escalations WHERE reason_code='PENDING_EXPIRED'
                           AND handoff->>'approval_id' = %s""", (str(approval_id),)).fetchall()


@pytest.mark.parametrize("who", ["reject", "withdraw"])
def test_r3_a_person_waiting_on_a_lock_past_the_deadline_cannot_reject_or_withdraw(conn, dsn, who):
    t, r = short_deadline(conn, seconds=4)
    appt = r["appointment_id"]
    act = {"reject": lambda c: console(c, "SELECT decide_booking_request(%s,'REJECT',%s,NULL)", (appt, k())),
           "withdraw": lambda c: withdraw(c, appt)}[who]
    out = _past_the_deadline_while_waiting_on_a_lock(conn, dsn, appt, act)
    assert "PENDING_EXPIRED" in str(out.get("err")), out
    assert approval(conn, appt)["status"] == "OPEN"                       # nothing changed; the sweep records EXPIRED
    with as_role(conn, "setuhaul_system", f"{JAI},{GGN}") as c:
        c.execute("SELECT expire_pending_requests()")
    a = approval(conn, appt)
    assert (a["status"], a["outcome_reason"]) == ("EXPIRED", "NO_DECISION_BY_DEADLINE")
    assert decided_events(conn, a["approval_id"]) == 1 and len(_expired_escalation(conn, a["approval_id"])) == 1


def test_r3_an_invalidation_waiting_on_a_lock_past_the_deadline_records_expired(conn, dsn):
    t, r = short_deadline(conn, seconds=4)
    appt = r["appointment_id"]
    out = _past_the_deadline_while_waiting_on_a_lock(
        conn, dsn, appt,
        lambda c: c.execute("UPDATE setuhaul.shipments SET load_weight_kg = load_weight_kg + 1 WHERE shipment_id=%s",
                            (t["sid"],)))
    assert "err" not in out, out                                         # the shipment change itself still commits
    a = approval(conn, appt)
    assert (a["status"], a["outcome_reason"], a["decided_by"]) == ("EXPIRED", "NO_DECISION_BY_DEADLINE", "system:expiry")
    assert "invalidated" in a["decision_note"]
    assert appt_status(conn, appt) == "EXPIRED" and not claim_active(conn, appt)
    assert decided_events(conn, a["approval_id"]) == 1
    assert _expired_escalation(conn, a["approval_id"]) == [("INVALIDATED",)]


def test_r3_a_dock_block_after_the_deadline_records_expired_and_the_block_stands(conn):
    t, r = short_deadline(conn, seconds=2, dock="DOCK-JAI-D3")
    time.sleep(2.3)
    start = conn.execute("SELECT lower(during) FROM setuhaul.appointments WHERE appointment_id=%s",
                         (r["appointment_id"],)).fetchone()[0]
    console(conn, "SELECT block_dock(%s,'DOCK-JAI-D3',%s,%s,'BREAKDOWN','hydraulics','staff:p1')",
            (f"DE-{uuid.uuid4().hex[:8]}", start, start + timedelta(minutes=50)))
    a = approval(conn, r["appointment_id"])
    assert (a["status"], a["decided_by_type"]) == ("EXPIRED", "SYSTEM")
    assert _expired_escalation(conn, a["approval_id"]) == [("INVALIDATED",)]


# ================================================================ R3-P1-02 shipment changes and dock compatibility
def _changed(conn, sid, field):
    s = conn.execute(f"SELECT {field} FROM setuhaul.shipments WHERE shipment_id=%s", (sid,)).fetchone()[0]
    return {"destination_facility_id": lambda: GGN if s != GGN else JAI, "load_weight_kg": lambda: s + 1,
            "required_dock_type": lambda: "HEAVY" if s != "HEAVY" else "ANY",
            "temperature_control_required": lambda: not s}[field]()


@pytest.mark.parametrize("field", ["destination_facility_id", "load_weight_kg", "required_dock_type",
                                   "temperature_control_required"])
def test_r3_a_booking_critical_shipment_change_invalidates_the_open_request(conn, field):
    t, r = pending(conn)
    conn.execute(f"UPDATE setuhaul.shipments SET {field} = %s WHERE shipment_id=%s",
                 (_changed(conn, t["sid"], field), t["sid"]))
    a = approval(conn, r["appointment_id"])
    assert (a["status"], a["outcome_reason"]) == ("INVALIDATED", "SHIPMENT_CHANGED")
    refusal = str(err(decide, conn, r["appointment_id"], "CONFIRM"))  # R3.1: shipment locked first; a moved one
    assert "ALREADY_DECIDED" in refusal or (                          # has left the planner's facility scope
        field == "destination_facility_id" and "SHIPMENT_NOT_VISIBLE" in refusal)


@pytest.mark.parametrize("field,value,code", [
    ("load_weight_kg", 30001, "DOCK_WEIGHT_EXCEEDED"),
    ("temperature_control_required", True, "DOCK_NOT_REFRIGERATED"),
    ("required_dock_type", "HEAVY", "DOCK_NOT_HEAVY"),
    # moved to another facility: the decider's scope no longer sees it, refused before the dock rule is even reached
    ("destination_facility_id", GGN, "SHIPMENT_NOT_VISIBLE|DOCK_WRONG_FACILITY")])
def test_r3_decide_re_proves_dock_compatibility_itself(conn, field, value, code):
    """Even with the invalidation trigger out of the way, the decision function refuses an incompatible booking."""
    t, r = pending(conn)
    with pytest.raises(psycopg.Error, match=code):
        with conn.transaction():                                        # rolled back: the trigger is re-enabled
            conn.execute("ALTER TABLE setuhaul.shipments DISABLE TRIGGER invalidate_on_shipment")
            conn.execute(f"UPDATE setuhaul.shipments SET {field} = %s WHERE shipment_id=%s", (value, t["sid"]))
            assert approval(conn, r["appointment_id"])["status"] == "OPEN"
            decide(conn, r["appointment_id"], "CONFIRM")
    assert approval(conn, r["appointment_id"])["status"] == "OPEN"


def test_r3_a_change_between_the_option_and_the_request_is_refused_at_hold_and_at_submission(conn):
    t = setup_thread(conn)
    option_at(conn, t)
    hold(conn, t)                                                       # held while compatible
    conn.execute("UPDATE setuhaul.shipments SET load_weight_kg = 30001 WHERE shipment_id=%s", (t["sid"],))
    assert "DOCK_" in str(err(submit, conn, t))                          # the hold→claim conversion re-proves it
    t2 = setup_thread(conn)
    option_at(conn, t2)
    conn.execute("UPDATE setuhaul.shipments SET temperature_control_required = true WHERE shipment_id=%s", (t2["sid"],))
    assert "DOCK_NOT_REFRIGERATED" in str(err(hold, conn, t2))          # changed after the option was published


# ================================================================ R3-P2-03 console identity for every mutation
@pytest.mark.parametrize("sql,roles,code", [
    ("SELECT block_dock('DE-x','DOCK-JAI-D1',now(),now()+interval '1h','BREAKDOWN','x','staff:p1')", ("operations",),
     "ROLE_NOT_ALLOWED"),
    ("SELECT block_dock('DE-x','DOCK-JAI-D1',now(),now()+interval '1h','BREAKDOWN','x','staff:other')", ("planner",),
     "PRINCIPAL_MISMATCH"),
    ("SELECT set_automation_paused('FAC-JAI-01',true,'staff:p1','x')", ("planner",), "ROLE_NOT_ALLOWED"),
    ("SELECT set_automation_paused('FAC-JAI-01',true,'staff:boss','x')", ("ops_lead",), "PRINCIPAL_MISMATCH"),
    ("SELECT take_over_thread('THR-x','staff:boss','x')", ("operations",), "PRINCIPAL_MISMATCH"),
])
def test_r3_console_mutations_check_role_and_principal_in_the_database(conn, sql, roles, code):
    with pytest.raises(psycopg.Error, match=code):
        console(conn, sql, roles=roles)


@pytest.mark.parametrize("sql", ["SELECT set_automation_paused('FAC-JAI-01',true,'staff:p1','x')",
                                 "SELECT take_over_thread('THR-x','staff:p1','x')",
                                 "SELECT hand_back_thread('THR-x','staff:p1','long enough summary')"])
def test_r3_console_mutations_need_the_trusted_staff_context(conn, sql):
    with pytest.raises(psycopg.Error, match="UNTRUSTED_CONTEXT"):
        with as_role(conn, "setuhaul_console", JAI) as c:
            c.execute(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO booking_events (principal,principal_role,action,facility_id) VALUES ('staff:fake','ops_lead','x','FAC-JAI-01')",
    "INSERT INTO outbox (facility_id,aggregate,aggregate_id,event_type,payload) "
    "VALUES ('FAC-JAI-01','appointment','x','BookingConfirmed','{}')",
    "UPDATE chat_threads SET human_active = true WHERE thread_id = 'THR-x'"])
def test_r3_the_console_cannot_write_audit_outbox_or_takeover_directly(conn, sql):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_role(conn, "setuhaul_console", JAI) as c:
            c.execute(sql)


def test_r3_takeover_records_the_staff_principal_and_role(conn):
    t = setup_thread(conn)
    console(conn, "SELECT take_over_thread(%s,'staff:ops9','driver upset')", (t["tid"],), roles=("operations",),
            who="staff:ops9")
    assert last_audit(conn, t["sid"]) == ("staff:ops9", "operations", "TAKE_OVER_THREAD")


# ================================================================ R3.1-P1 decision vs concurrent shipment / dock change
def _bg(fn):
    out = {}

    def go():
        try:
            out["ok"] = fn()
        except psycopg.Error as e:
            out["err"] = e
    th = threading.Thread(target=go)
    th.start()
    return th, out


def _decide_in(dsn, appt):
    def run():
        with psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=15s") as c:
            return decide(c, appt, "CONFIRM")
    return run


def _at_risk(conn, sid):
    return conn.execute("""SELECT handoff->>'cause' FROM setuhaul.escalations WHERE reason_code='BOOKING_AT_RISK'
                           AND shipment_id=%s""", (sid,)).fetchall()


def test_r31_a_shipment_change_that_got_there_first_makes_the_decision_wait_then_refuse(conn, dsn):
    t, r = pending(conn)
    changer = psycopg.connect(dsn)
    changer.execute("UPDATE setuhaul.shipments SET load_weight_kg = 30001 WHERE shipment_id=%s", (t["sid"],))
    th, out = _bg(_decide_in(dsn, r["appointment_id"]))
    time.sleep(0.5)
    assert th.is_alive()                                                # waits on the shipment row
    changer.commit()
    changer.close()
    th.join(20)
    assert "ALREADY_DECIDED" in str(out.get("err")), out
    assert approval(conn, r["appointment_id"])["status"] == "INVALIDATED"


@pytest.mark.parametrize("what", ["shipment", "dock"])
def test_r31_a_change_that_arrives_during_the_decision_waits_and_a_person_is_told(conn, dsn, what):
    t, r = pending(conn, dock="DOCK-JAI-D3")
    appt = r["appointment_id"]
    pause = psycopg.connect(dsn)                                        # holds the decision after its first locks
    pause.execute("SELECT 1 FROM setuhaul.appointments WHERE appointment_id=%s FOR UPDATE", (appt,))
    dth, dout = _bg(_decide_in(dsn, appt))
    time.sleep(0.5)
    change = {"shipment": ("UPDATE setuhaul.shipments SET load_weight_kg = 30001 WHERE shipment_id=%s", (t["sid"],)),
              "dock": ("UPDATE setuhaul.docks SET max_vehicle_weight_kg = 1000 WHERE dock_id=%s", ("DOCK-JAI-D3",))}[what]

    def apply_change():
        with psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=15s") as c:
            c.execute(*change)
    cth, cout = _bg(apply_change)
    try:
        time.sleep(0.5)
        assert dth.is_alive() and cth.is_alive(), (dout, cout)          # the change waits for the decision
        pause.rollback()
        dth.join(20)
        cth.join(20)
        assert "ok" in dout and "ok" in cout, (dout, cout)              # no deadlock, nothing refused
        assert approval(conn, appt)["status"] == "APPROVED" and appt_status(conn, appt) == "CONFIRMED"
        assert _at_risk(conn, t["sid"]) == [("SHIPMENT_CHANGED" if what == "shipment" else "DOCK_CHANGED",)]
    finally:
        pause.close()                                                   # never leave the decision blocked
        dth.join(20)
        cth.join(20)
        if what == "dock":
            conn.execute("UPDATE setuhaul.docks SET max_vehicle_weight_kg = 30000 WHERE dock_id='DOCK-JAI-D3'")


def test_r31_decisions_racing_shipment_changes_never_deadlock_and_never_approve_silently(conn, dsn):
    for _ in range(8):
        t, r = pending(conn)
        go = threading.Barrier(2)

        def decide_now():
            with psycopg.connect(dsn, autocommit=True) as c:
                go.wait()
                return decide(c, r["appointment_id"], "CONFIRM")

        def change_now():
            with psycopg.connect(dsn, autocommit=True) as c:
                go.wait()
                c.execute("UPDATE setuhaul.shipments SET load_weight_kg = 30001 WHERE shipment_id=%s", (t["sid"],))
        (dth, dout), (cth, cout) = _bg(decide_now), _bg(change_now)
        dth.join(20)
        cth.join(20)
        assert all(getattr(o.get("err"), "sqlstate", None) != "40P01" for o in (dout, cout)), (dout, cout)
        assert "ok" in cout, cout
        status = approval(conn, r["appointment_id"])["status"]
        if status == "APPROVED":                                        # the decision was first: a person is told
            assert _at_risk(conn, t["sid"]), "approved, then made incompatible, with no escalation"
        else:
            assert status == "INVALIDATED" and "ALREADY_DECIDED" in str(dout.get("err")), (status, dout)


# ================================================================ R3.1-P2 planner inherits operations permissions
def test_r31_a_planner_can_take_over_and_hand_back_at_their_facility(conn):
    t = setup_thread(conn)
    console(conn, "SELECT take_over_thread(%s,'staff:plan1','driver upset')", (t["tid"],), roles=("planner",), who="staff:plan1")
    assert last_audit(conn, t["sid"]) == ("staff:plan1", "planner", "TAKE_OVER_THREAD")   # R3.2: principal AND role
    console(conn, "SELECT hand_back_thread(%s,'staff:plan1','Called the driver, slot stays')", (t["tid"],),
            roles=("planner",), who="staff:plan1")
    assert last_audit(conn, t["sid"]) == ("staff:plan1", "planner", "HAND_BACK_THREAD")
    assert conn.execute("SELECT human_active FROM setuhaul.chat_threads WHERE thread_id=%s", (t["tid"],)).fetchone()[0] is False


def test_r31_a_planner_at_another_facility_cannot_take_over_or_hand_back(conn):
    t = setup_thread(conn)                                              # a JAI conversation
    with pytest.raises(psycopg.Error, match="THREAD_UNKNOWN"):
        console(conn, "SELECT take_over_thread(%s,'staff:plan2','x')", (t["tid"],), scope=GGN, roles=("planner",),
                who="staff:plan2")
    console(conn, "SELECT take_over_thread(%s,'staff:plan1','x')", (t["tid"],), roles=("planner",), who="staff:plan1")
    with pytest.raises(psycopg.Error, match="THREAD_NOT_IN_HUMAN_HANDS"):
        console(conn, "SELECT hand_back_thread(%s,'staff:plan2','long enough summary')", (t["tid"],), scope=GGN,
                roles=("planner",), who="staff:plan2")



# ================================================================ R3.2-P1 shipment and dock changes to one confirmed booking
def _confirmed_booking(conn, weight=1000, limit=2000, dock="DOCK-JAI-D3"):
    """A CONFIRMED booking: shipment of `weight` kg at a dock whose limit is `limit` kg (compatible)."""
    conn.execute("UPDATE setuhaul.docks SET max_vehicle_weight_kg = %s WHERE dock_id=%s", (limit, dock))
    t = setup_thread(conn)
    conn.execute("UPDATE setuhaul.shipments SET load_weight_kg = %s WHERE shipment_id=%s", (weight, t["sid"]))
    option_at(conn, t, 10, dock)
    hold(conn, t)
    r = submit(conn, t)
    decide(conn, r["appointment_id"], "CONFIRM")
    assert appt_status(conn, r["appointment_id"]) == "CONFIRMED"
    return t, r


def _compat_now(conn, sid, dock="DOCK-JAI-D3"):
    return conn.execute("""SELECT setuhaul.booking_incompatibility(s, d) FROM setuhaul.shipments s, setuhaul.docks d
                           WHERE s.shipment_id=%s AND d.dock_id=%s""", (sid, dock)).fetchone()[0]


SHIP_UP = "UPDATE setuhaul.shipments SET load_weight_kg = 1500 WHERE shipment_id=%s"
DOCK_DOWN = "UPDATE setuhaul.docks SET max_vehicle_weight_kg = 1200 WHERE dock_id='DOCK-JAI-D3'"


@pytest.mark.parametrize("first", ["shipment", "dock"])
def test_r32_shipment_and_dock_changes_to_one_booking_are_serialised_and_a_person_is_told(conn, dsn, first):
    """Reviewer's case: 1,000 -> 1,500 kg and 2,000 -> 1,200 kg. Each is compatible with the other's OLD value."""
    t, r = _confirmed_booking(conn)
    sql = {"shipment": (SHIP_UP, (t["sid"],)), "dock": (DOCK_DOWN, ())}
    second = "dock" if first == "shipment" else "shipment"
    a = psycopg.connect(dsn, options="-c lock_timeout=15s")            # first change, transaction left open
    th = None
    try:
        a.execute(*sql[first])
        assert _at_risk(conn, t["sid"]) == []                           # alone it is still compatible

        def run_second():
            with psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=15s") as c:
                c.execute(*sql[second])
        th, out = _bg(run_second)
        time.sleep(0.5)
        assert th.is_alive(), out                                       # the second change waits on the compat key
        a.commit()
        th.join(20)
        assert "ok" in out, out
        assert _compat_now(conn, t["sid"]).startswith("DOCK_WEIGHT_EXCEEDED")
        cause = {"shipment": "SHIPMENT_CHANGED", "dock": "DOCK_CHANGED"}[second]
        assert _at_risk(conn, t["sid"]) == [(cause,)]                   # the later change saw both and escalated
    finally:
        a.close()
        if th is not None:
            th.join(20)
        conn.execute("UPDATE setuhaul.docks SET max_vehicle_weight_kg = 30000 WHERE dock_id='DOCK-JAI-D3'")


def test_r32_simultaneous_shipment_and_dock_changes_end_compatible_or_escalated(conn, dsn):
    try:
        for _ in range(8):
            t, r = _confirmed_booking(conn)
            go = threading.Barrier(2)

            def change(sql, args):
                def run():
                    with psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=15s") as c:
                        go.wait()
                        c.execute(sql, args)
                return run
            (sth, sout), (dth, dout) = _bg(change(SHIP_UP, (t["sid"],))), _bg(change(DOCK_DOWN, ()))
            sth.join(20)
            dth.join(20)
            assert "ok" in sout and "ok" in dout, (sout, dout)          # no deadlock, nothing refused
            assert _compat_now(conn, t["sid"]) is None or _at_risk(conn, t["sid"]), \
                "confirmed booking left incompatible with no BOOKING_AT_RISK escalation"
    finally:
        conn.execute("UPDATE setuhaul.docks SET max_vehicle_weight_kg = 30000 WHERE dock_id='DOCK-JAI-D3'")


def test_r32_a_compatibility_change_under_repeatable_read_is_refused(conn, dsn):
    t, r = _confirmed_booking(conn)
    try:
        with psycopg.connect(dsn) as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            with pytest.raises(psycopg.Error, match="COMPAT_CHANGE_NEEDS_READ_COMMITTED"):
                c.execute(SHIP_UP, (t["sid"],))
    finally:
        conn.execute("UPDATE setuhaul.docks SET max_vehicle_weight_kg = 30000 WHERE dock_id='DOCK-JAI-D3'")
