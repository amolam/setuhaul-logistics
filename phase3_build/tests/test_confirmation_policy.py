"""B-WF-2a risk-based confirmation policy (migration 0016), on the agreed B-WF-2 boundary.

One test per rule row (evaluated directly), then the risk inputs as the database computes them from real rows, the
person-always and policy guards, and the override-rate metric. Every row ships NEEDS_PLANNER or PERSON_ALWAYS (Q1).
"""
import json
import uuid
from datetime import timedelta

import psycopg
import pytest

from conftest import as_role
from test_approvals import approval, option_at, pending
from test_db_requests import JAI, decide, err, hold, setup_thread, submit

BASE = {"kind": "STANDARD", "moves_booking": False, "move_earlier": False, "move_later": False, "into_busier_time": False,
        "after_last_new_start": False, "critical_load": False, "reefer_load": False, "dock_downgrade": False,
        "safety": False, "contradictory_data": False}

# rule row → (inputs that should select it, outcome, request kind)
ROWS = {
    "person-safety": ({"safety": True}, "PERSON_ALWAYS", "STANDARD"),
    "person-contradictory-data": ({"contradictory_data": True}, "PERSON_ALWAYS", "STANDARD"),
    "planner-after-last-new-start": ({"after_last_new_start": True, "kind": "AFTER_HOURS_START"}, "NEEDS_PLANNER", "AFTER_HOURS_START"),
    "planner-after-hours": ({"kind": "AFTER_HOURS_START"}, "NEEDS_PLANNER", "AFTER_HOURS_START"),
    "planner-dock-downgrade": ({"moves_booking": True, "dock_downgrade": True}, "NEEDS_PLANNER", "DOCK_DOWNGRADE"),
    "planner-move-earlier": ({"moves_booking": True, "move_earlier": True}, "NEEDS_PLANNER", "PLANNER_REVIEW"),
    "planner-busier-time": ({"moves_booking": True, "move_later": True, "into_busier_time": True}, "NEEDS_PLANNER", "PLANNER_REVIEW"),
    "planner-critical-load": ({"critical_load": True}, "NEEDS_PLANNER", "PLANNER_REVIEW"),
    "planner-reefer-load": ({"reefer_load": True}, "NEEDS_PLANNER", "PLANNER_REVIEW"),
    "safe-new-booking": ({}, "NEEDS_PLANNER", "STANDARD"),
    "safe-move-later": ({"moves_booking": True, "move_later": True}, "NEEDS_PLANNER", "STANDARD"),
}


def evaluate(conn, inputs, facility=JAI):
    return conn.execute("""SELECT (x).rule_id, (x).outcome, (x).approval_kind, (x).auto_eligible
                             FROM (SELECT setuhaul.evaluate_confirmation_policy(%s, %s::jsonb) x) s""",
                        (facility, json.dumps({**BASE, **inputs}))).fetchone()


def evidence(conn, appt):
    return approval(conn, appt)["evidence"]


def book(conn, t, hour, dock="DOCK-JAI-D1"):
    """A confirmed current booking for this conversation's shipment."""
    option_at(conn, t, hour, dock)
    hold(conn, t)
    r = submit(conn, t)
    decide(conn, r["appointment_id"], "CONFIRM")
    return r


def move(conn, t, hour, dock="DOCK-JAI-D1", day_of=None):
    """A second request for the same shipment, on the same day as its booking (day_of) at another hour."""
    oid = uuid.uuid4()
    start = day_of.replace(hour=hour)
    with as_role(conn, "setuhaul_feasibility", t["fac"]) as c:
        c.execute("""INSERT INTO shown_options VALUES (%s,%s,%s,%s,tstzrange(%s,%s,'[)'),false,1,0,now()+interval '5 min',now())""",
                  (oid, t["tid"], t["sid"], dock, start, start + timedelta(minutes=55)))
    t["oid"] = oid
    hold(conn, t)
    return submit(conn, t)


# ================================================================ one test per rule row
@pytest.mark.parametrize("row", sorted(ROWS))
def test_each_rule_row_selects_its_outcome_and_kind(conn, row):
    inputs, outcome, kind = ROWS[row]
    rid, got_outcome, got_kind, auto = evaluate(conn, inputs)
    assert (rid, got_outcome, got_kind) == (f"{JAI}:{row}", outcome, kind)
    assert auto is row.startswith("safe-")                                  # only the safe cases are auto-eligible


def test_the_default_row_catches_everything_else(conn):
    rid, outcome, kind, auto = evaluate(conn, {"kind": "SCHEDULE_PROPOSAL", "moves_booking": True})
    assert (rid, outcome, kind, auto) == (f"default-{JAI}", "NEEDS_PLANNER", "STANDARD", False)


def test_every_facility_has_every_row_and_none_confirms(conn):
    for fac in (JAI, "FAC-GGN-01"):
        rows = conn.execute("""SELECT DISTINCT ON (rule_id) rule_id, outcome FROM setuhaul.confirmation_rules
                               WHERE facility_id=%s AND created_by LIKE 'migration:%%' ORDER BY rule_id, version DESC""",
                            (fac,)).fetchall()
        ids = {r[0] for r in rows}
        assert {f"{fac}:{r}" for r in ROWS} | {f"default-{fac}"} <= ids
        assert all(o in ("NEEDS_PLANNER", "PERSON_ALWAYS") for _, o in rows)   # Q1; GGN night NOT switched on


def test_a_policy_rule_needs_an_authorisation_and_auto_eligibility(conn):
    for auto, auth in ((True, None), (False, "T99")):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome,
                                                                     auto_eligible,authorised_by)
                            VALUES (%s,1,%s,500,'{}','POLICY_CONFIRM',%s,%s)""", (f"x-{uuid.uuid4().hex[:6]}", JAI, auto, auth))


# ================================================================ inputs computed by the database
def test_a_plain_new_booking_is_the_safe_case_and_still_needs_a_person(conn):
    t, r = pending(conn)
    ev = evidence(conn, r["appointment_id"])
    assert r["status"] == "PENDING" and ev["rule_id"] == f"{JAI}:safe-new-booking"
    assert ev["rule_auto_eligible"] is True and ev["rule_outcome"] == "NEEDS_PLANNER"
    assert approval(conn, r["appointment_id"])["kind"] == "STANDARD"


def test_critical_load_needs_a_planner_not_operations(conn):
    t = setup_thread(conn)
    conn.execute("UPDATE setuhaul.shipments SET priority_code='CRITICAL' WHERE shipment_id=%s", (t["sid"],))
    option_at(conn, t)
    hold(conn, t)
    r = submit(conn, t)
    a = approval(conn, r["appointment_id"])
    assert a["evidence"]["rule_id"] == f"{JAI}:planner-critical-load" and a["kind"] == "PLANNER_REVIEW"
    assert "ROLE_NOT_ALLOWED" in str(err(decide, conn, r["appointment_id"], "CONFIRM", JAI, None, ("operations",)))
    assert decide(conn, r["appointment_id"], "CONFIRM", roles=("planner",))["outcome"] == "APPROVED"


def test_moving_a_booking_earlier_later_and_into_a_busier_time(conn):
    t = setup_thread(conn)
    b = book(conn, t, 14, "DOCK-JAI-D1")
    day = t["start"]
    earlier = move(conn, t, 10, "DOCK-JAI-D1", day)
    ev = evidence(conn, earlier["appointment_id"])
    assert ev["rule_id"] == f"{JAI}:planner-move-earlier" and ev["inputs"]["move_earlier"] is True
    assert approval(conn, earlier["appointment_id"])["kind"] == "PLANNER_REVIEW"
    decide(conn, earlier["appointment_id"], "REJECT")

    later = move(conn, t, 17, "DOCK-JAI-D1", day)                             # nothing else at 17:00
    ev = evidence(conn, later["appointment_id"])
    assert ev["rule_id"] == f"{JAI}:safe-move-later" and ev["inputs"]["into_busier_time"] is False
    decide(conn, later["appointment_id"], "REJECT")

    busy = day.replace(hour=19)                                              # two docks blocked at 19:00
    for d in ("DOCK-JAI-D2", "DOCK-JAI-D3"):
        conn.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id,facility_id,during,kind,ref_id,created_by)
                        VALUES (%s,%s,tstzrange(%s,%s,'[)'),'BLOCK',%s,'test')""",
                     (d, JAI, busy, busy + timedelta(hours=1), f"DE-{uuid.uuid4().hex[:6]}"))
    busier = move(conn, t, 19, "DOCK-JAI-D1", day)
    ev = evidence(conn, busier["appointment_id"])
    assert ev["rule_id"] == f"{JAI}:planner-busier-time" and ev["inputs"]["load_new"] > ev["inputs"]["load_old"]
    assert b["status"] == "PENDING"                         # book() submitted it as a request, then a planner approved it


def test_after_last_new_start_needs_a_planner(conn):
    conn.execute("UPDATE setuhaul.facility_config SET last_new_start_local='09:00' WHERE facility_id=%s AND active", (JAI,))
    try:
        t, r = pending(conn, hour=10)
        a = approval(conn, r["appointment_id"])
        assert a["evidence"]["rule_id"] == f"{JAI}:planner-after-last-new-start" and a["kind"] == "AFTER_HOURS_START"
    finally:
        conn.execute("UPDATE setuhaul.facility_config SET last_new_start_local=NULL WHERE facility_id=%s AND active", (JAI,))


def test_reefer_load_and_dock_downgrade(conn):
    conn.execute("""INSERT INTO setuhaul.docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg)
                    VALUES ('DOCK-JAI-R1',%s,'R1','REEFER',true,30000), ('DOCK-JAI-H1',%s,'H1','HEAVY',false,40000)
                    ON CONFLICT DO NOTHING""", (JAI, JAI))
    t = setup_thread(conn)
    conn.execute("UPDATE setuhaul.shipments SET temperature_control_required=true WHERE shipment_id=%s", (t["sid"],))
    option_at(conn, t, dock="DOCK-JAI-R1")
    hold(conn, t)
    r = submit(conn, t)
    assert evidence(conn, r["appointment_id"])["rule_id"] == f"{JAI}:planner-reefer-load"

    t2 = setup_thread(conn)
    book(conn, t2, 12, "DOCK-JAI-H1")                                       # booked on a heavy dock...
    down = move(conn, t2, 15, "DOCK-JAI-D2", t2["start"])                   # ...moved to a standard one
    a = approval(conn, down["appointment_id"])
    assert a["evidence"]["rule_id"] == f"{JAI}:planner-dock-downgrade" and a["kind"] == "DOCK_DOWNGRADE"
    assert "ROLE_NOT_ALLOWED" in str(err(decide, conn, down["appointment_id"], "CONFIRM", JAI, None, ("operations",)))


@pytest.mark.parametrize("reason,label", [("SAFETY", "person-safety"), ("CONTRADICTORY_DATA", "person-contradictory-data")])
def test_person_always_creates_nothing(conn, reason, label):
    t = setup_thread(conn)
    conn.execute("""INSERT INTO setuhaul.escalations (escalation_id,reason_code,mode,facility_id,shipment_id,handoff,ack_due_at)
                    VALUES (gen_random_uuid(),%s,'TAKE_OVER',%s,%s,'{}',now()+interval '10 min')""", (reason, JAI, t["sid"]))
    option_at(conn, t)
    hold(conn, t)
    e = err(submit, conn, t)
    assert "REQUIRES_PERSON" in str(e) and label in str(e)
    n = conn.execute("SELECT count(*) FROM setuhaul.approval_requests WHERE shipment_id=%s", (t["sid"],)).fetchone()[0]
    m = conn.execute("SELECT count(*) FROM setuhaul.appointments WHERE shipment_id=%s", (t["sid"],)).fetchone()[0]
    assert (n, m) == (0, 0)


def test_person_always_holds_even_when_a_rule_would_shadow_it(conn):
    rid = f"shadow-{uuid.uuid4().hex[:6]}"
    conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome)
                    VALUES (%s,1,%s,1,'{}','NEEDS_PLANNER')""", (rid, JAI))
    try:
        t = setup_thread(conn)
        conn.execute("""INSERT INTO setuhaul.escalations (escalation_id,reason_code,mode,facility_id,shipment_id,handoff,ack_due_at)
                        VALUES (gen_random_uuid(),'SAFETY','TAKE_OVER',%s,%s,'{}',now()+interval '10 min')""", (JAI, t["sid"]))
        option_at(conn, t)
        hold(conn, t)
        assert "REQUIRES_PERSON" in str(err(submit, conn, t))
    finally:
        conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome)
                        VALUES (%s,2,%s,1,'{"retired": true}','NEEDS_PLANNER')""", (rid, JAI))


def test_an_authorised_policy_rule_never_confirms_a_risky_request(conn):
    rid = f"auth-{uuid.uuid4().hex[:6]}"
    conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome,
                                                             auto_eligible,authorised_by)
                    VALUES (%s,1,%s,1,'{}','POLICY_CONFIRM',true,'TEST-ONLY')""", (rid, JAI))
    try:
        t = setup_thread(conn)
        conn.execute("UPDATE setuhaul.shipments SET priority_code='CRITICAL' WHERE shipment_id=%s", (t["sid"],))
        option_at(conn, t)
        hold(conn, t)
        r = submit(conn, t)
        assert r["status"] == "PENDING" and approval(conn, r["appointment_id"])["kind"] == "PLANNER_REVIEW"
        t2, r2 = pending(conn, dock="DOCK-JAI-D2")                           # a plain booking: the rule applies
        assert r2["status"] == "CONFIRMED" and r2["policy_rule"] == f"{rid}@v1"
    finally:
        conn.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome)
                        VALUES (%s,2,%s,1,'{"retired": true}','NEEDS_PLANNER')""", (rid, JAI))


# ================================================================ override-rate metric
def test_override_rate_per_rule_is_reported_to_planners(conn):
    def row():
        with as_role(conn, "setuhaul_console", JAI) as c:
            r = c.execute("""SELECT approved, rejected, override_rate FROM policy_rule_outcomes
                             WHERE facility_id=%s AND rule_id=%s""", (JAI, f"{JAI}:safe-new-booking")).fetchone()
        return r or (0, 0, None)

    a0, r0, _ = row()
    for decision in ("CONFIRM", "CONFIRM", "CONFIRM", "REJECT"):
        t, r = pending(conn)
        decide(conn, r["appointment_id"], decision)
    a1, r1, rate = row()
    assert (a1 - a0, r1 - r0) == (3, 1)
    assert float(rate) == round(r1 / (a1 + r1), 3)
