"""Database request/decision functions, driven through the real roles (agent, console, system).

The agent requests; the database decides. Every test runs the way the service will: one transaction per call,
SET LOCAL ROLE, transaction-local facility scope and driver id (RDS Data API pattern).

Confirmation path since migration 0004:
  record_read_back (agent, DB writes the read-back message) → driver message (system ingest) →
  attest_confirmation (system only) → hold_option / submit_booking_request verify the attestation.
"""
import itertools
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from conftest import as_role, new_shipment
from setuhaul.domain.booking import classify_confirmation

JAI, GGN = "FAC-JAI-01", "FAC-GGN-01"
_DAY = itertools.count(1)          # each test books its own day, so tests never compete for a slot
READ_BACK = "You selected a dock at a time. Shall I request it? Reply YES to confirm."


def k():
    return uuid.uuid4().hex                                 # an idempotency key


def setup_thread(conn, facility=JAI, dock="DOCK-JAI-D1", start_h=3.0, needs_approval=False, exact=False, with_shipment=True):
    """Driver, shipment, thread (opened by ingest), one option published by the feasibility role."""
    if not exact:
        start_h += 24 * next(_DAY)
    sid, drv = new_shipment(conn, facility)
    tid, oid = f"THR-{uuid.uuid4().hex[:8]}", uuid.uuid4()
    with as_role(conn, "setuhaul_ingest", None, drv) as c:
        c.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')",
                  (tid, drv, sid if with_shipment else None))
    with as_role(conn, "setuhaul_feasibility", facility) as c:
        if with_shipment:
            c.execute("""INSERT INTO shown_options VALUES (%s,%s,%s,%s,
                         tstzrange(now()+make_interval(mins=>%s), now()+make_interval(mins=>%s), '[)'),%s,1,0,now()+interval '5 min',now())""",
                      (oid, tid, sid, dock, int(start_h * 60), int(start_h * 60) + 55, needs_approval))
    return dict(sid=sid, drv=drv, tid=tid, oid=oid, fac=facility)


def publish_option(conn, t, dock, start_h):
    oid = uuid.uuid4()
    with as_role(conn, "setuhaul_feasibility", t["fac"]) as c:
        c.execute("""INSERT INTO shown_options VALUES (%s,%s,%s,%s,
                     tstzrange(now()+make_interval(mins=>%s), now()+make_interval(mins=>%s),'[)'),false,1,0,now()+interval '5 min',now())""",
                  (oid, t["tid"], t["sid"], dock, int(start_h * 60), int(start_h * 60) + 55))
    return oid


def driver_says(conn, t, text="haan kar do") -> int:
    """Inbound webhook ingest (setuhaul_ingest) records the driver's real message."""
    with as_role(conn, "setuhaul_ingest", None, t["drv"]) as c:
        return c.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked)
                            VALUES (%s,'DRIVER','WHATSAPP',md5(%s),%s) RETURNING message_id""",
                         (t["tid"], text + uuid.uuid4().hex, text)).fetchone()[0]


def read_back(conn, t, oid=None):
    with as_role(conn, "setuhaul_agent", t["fac"], t["drv"]) as c:
        return c.execute("SELECT record_read_back(%s,%s,%s,'agent')", (oid or t["oid"], t["tid"], READ_BACK)).fetchone()[0]


def attest(conn, t, msg, verdict="YES"):
    with as_role(conn, "setuhaul_ingest", None, t["drv"]) as c:
        return c.execute("SELECT attest_confirmation(%s,%s,%s,'lexicon-v1','ingest')", (t["tid"], msg, verdict)).fetchone()[0]


def confirm(conn, t, text="haan kar do", oid=None) -> str:
    """The full trusted confirmation: read-back, driver reply, classification, attestation."""
    read_back(conn, t, oid)
    msg = driver_says(conn, t, text)
    a = attest(conn, t, msg, classify_confirmation(text))
    return a["attestation_id"]


def hold(conn, t, att=None, key=None, oid=None):
    att = att or confirm(conn, t, oid=oid)
    t["att"] = att
    with as_role(conn, "setuhaul_agent", t["fac"], t["drv"]) as c:
        return c.execute("SELECT hold_option(%s,%s,%s,%s,'agent')", (oid or t["oid"], t["tid"], att, key or k())).fetchone()[0]


def submit(conn, t, att=None, key=None, oid=None):
    with as_role(conn, "setuhaul_agent", t["fac"], t["drv"]) as c:
        return c.execute("SELECT submit_booking_request(%s,%s,%s,%s,'agent')",
                         (oid or t["oid"], t["tid"], att or t.get("att") or uuid.uuid4(), key or k())).fetchone()[0]


def staff(c, scope=JAI, roles=("planner",), who="staff:p1"):
    """What the console API does after verifying the Cognito token (B-WF-2 §15)."""
    c.execute("SELECT begin_staff_context(%s,%s,%s)", (who, list(roles), scope.split(",")))


def decide(conn, appt, decision, scope=JAI, key=None, roles=("planner",)):
    with as_role(conn, "setuhaul_console", scope) as c:
        staff(c, scope, roles)
        return c.execute("SELECT decide_booking_request(%s,%s,%s,'ok')", (appt, decision, key or k())).fetchone()[0]


def run(conn, role, scope, drv, sql, params=()):
    with as_role(conn, role, scope, drv) as c:
        cur = c.execute(sql, params)
        return cur.fetchone() if cur.description else None


def run_staff(conn, scope, roles, who, sql, params=()):
    """A console call under a trusted staff identity (R3-P2-03: every console mutation needs one)."""
    with as_role(conn, "setuhaul_console", scope) as c:
        staff(c, scope, roles, who)
        cur = c.execute(sql, params)
        return cur.fetchone() if cur.description else None


def err(fn, *a):
    with pytest.raises(psycopg.Error) as e:
        fn(*a)
    return e.value


def denied(fn, *a) -> bool:
    return err(fn, *a).sqlstate == "42501"


# ---------------------------------------------------------------- the MVP path, end to end in the DB
def test_happy_path_request_then_warehouse_confirms(conn):
    t = setup_thread(conn)
    hold(conn, t)
    r = submit(conn, t)
    assert r["status"] == "PENDING" and r["pending_expires_at"]
    occ = conn.execute("SELECT kind, active FROM setuhaul.dock_occupancy WHERE ref_id=%s", (r["appointment_id"],)).fetchone()
    assert occ == ("APPOINTMENT", True)                                   # same capacity row, no gap
    d = decide(conn, r["appointment_id"], "CONFIRM")
    assert d["status"] == "CONFIRMED"
    a = conn.execute("SELECT status, is_current FROM setuhaul.appointments WHERE appointment_id=%s", (r["appointment_id"],)).fetchone()
    assert a == ("CONFIRMED", True)
    events = [x[0] for x in conn.execute("SELECT event_type FROM setuhaul.outbox WHERE aggregate_id=%s ORDER BY event_id",
                                         (r["appointment_id"],))]
    assert events == ["BookingRequested", "BookingConfirmed"]
    audit = [x[0] for x in conn.execute("SELECT action FROM setuhaul.booking_events WHERE shipment_id=%s ORDER BY event_id", (t["sid"],))]
    assert audit == ["HOLD_OPTION", "SUBMIT_BOOKING_REQUEST", "APPROVAL_APPROVED"]


def test_reschedule_replaces_old_booking_only_on_confirmation(conn):
    t = setup_thread(conn)
    hold(conn, t)
    first = submit(conn, t)
    decide(conn, first["appointment_id"], "CONFIRM")
    o2 = publish_option(conn, t, "DOCK-JAI-D2", 5)
    hold(conn, t, oid=o2)
    second = submit(conn, t, oid=o2)
    assert second["replaces"] == first["appointment_id"]
    st = lambda a: conn.execute("SELECT status,is_current FROM setuhaul.appointments WHERE appointment_id=%s", (a,)).fetchone()
    assert st(first["appointment_id"]) == ("CONFIRMED", True)            # still valid while the request is pending
    decide(conn, second["appointment_id"], "CONFIRM")
    assert st(first["appointment_id"]) == ("CANCELLED", False)
    assert st(second["appointment_id"]) == ("CONFIRMED", True)


# ---------------------------------------------------------------- P0-1: confirmation is attested, never asserted
def test_agent_cannot_insert_a_driver_message(conn):
    t = setup_thread(conn)
    e = err(run, conn, "setuhaul_agent", JAI, t["drv"],
            "INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked) VALUES (%s,'DRIVER','WHATSAPP','h','yes')",
            (t["tid"],))
    assert e.sqlstate == "42501" and "row-level security" in str(e)


def test_yes_sent_before_the_read_back_is_not_attested(conn):
    t = setup_thread(conn)
    early = driver_says(conn, t, "yes")                                  # e.g. answering "are you still on NH48?"
    read_back(conn, t)
    assert attest(conn, t, early) is None
    assert "ATTESTATION_INVALID" in str(err(hold, conn, t, uuid.uuid4()))


def test_agent_message_cannot_be_attested(conn):
    t = setup_thread(conn)
    read_back(conn, t)
    agent_msg = run(conn, "setuhaul_agent", JAI, t["drv"],
                    "INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked) "
                    "VALUES (%s,'AGENT','WHATSAPP','h','yes') RETURNING message_id", (t["tid"],))[0]
    assert attest(conn, t, agent_msg) is None


def test_a_no_or_unclear_reply_cannot_hold(conn):
    t = setup_thread(conn)
    att = confirm(conn, t, "yes but make it 2 pm")
    assert "NO_DRIVER_CONFIRMATION" in str(err(hold, conn, t, att))
    t2 = setup_thread(conn)
    att2 = confirm(conn, t2, "nahi")
    assert "NO_DRIVER_CONFIRMATION" in str(err(hold, conn, t2, att2))


def test_attestation_from_another_thread_is_invalid(conn):
    a, b = setup_thread(conn), setup_thread(conn)
    att_b = confirm(conn, b)
    assert "ATTESTATION_INVALID" in str(err(hold, conn, a, att_b))


def test_agent_cannot_write_attestations_or_selections(conn):
    t = setup_thread(conn)
    rb = read_back(conn, t)
    msg = driver_says(conn, t)
    assert denied(run, conn, "setuhaul_agent", JAI, t["drv"], "SELECT attest_confirmation(%s,%s,'YES','x','agent')", (t["tid"], msg))
    assert denied(run, conn, "setuhaul_agent", JAI, t["drv"],
                  "INSERT INTO confirmation_attestations VALUES (gen_random_uuid(),%s,%s,%s,%s,'YES','x','agent',now(),now()+interval '1h')",
                  (rb["selection_id"], t["tid"], t["oid"], msg))
    att = attest(conn, t, msg)["attestation_id"]
    assert denied(run, conn, "setuhaul_agent", JAI, t["drv"],
                  "UPDATE confirmation_attestations SET verdict='YES' WHERE attestation_id=%s", (att,))
    assert denied(run, conn, "setuhaul_agent", JAI, t["drv"],
                  "INSERT INTO option_selections (thread_id,option_id,read_back_message_id) VALUES (%s,%s,%s)",
                  (t["tid"], t["oid"], rb["read_back_message_id"]))


def test_changed_selection_or_expired_attestation_cannot_hold(conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    o2 = publish_option(conn, t, "DOCK-JAI-D2", 24 * next(_DAY) + 4)
    read_back(conn, t, o2)                                               # the driver picked something else since
    assert "SELECTION_CHANGED" in str(err(hold, conn, t, att))
    t2 = setup_thread(conn)
    att2 = confirm(conn, t2)
    conn.execute("UPDATE setuhaul.confirmation_attestations SET expires_at = now() - interval '1 s' WHERE attestation_id=%s", (att2,))
    assert "ATTESTATION_EXPIRED" in str(err(hold, conn, t2, att2))


def test_agent_cannot_bypass_through_core_functions(conn):
    t = setup_thread(conn)
    msg = driver_says(conn, t)
    for sql in ("SELECT * FROM place_hold(%s,%s,'agent',interval '3 min')",
                "SELECT hold_option_core(%s,%s,1,'kkkkkkkkkkkkkkkkkkkk','agent')",
                "SELECT submit_booking_request_core(%s,%s,1,'kkkkkkkkkkkkkkkkkkkk','agent')"):
        assert denied(run, conn, "setuhaul_agent", JAI, t["drv"], sql, (t["oid"], t["tid"])), sql
    assert msg


def test_agent_cannot_publish_options(conn):
    t = setup_thread(conn)
    assert denied(run, conn, "setuhaul_agent", JAI, t["drv"],
                  """INSERT INTO shown_options VALUES (gen_random_uuid(),%s,%s,'DOCK-JAI-D1',
                     tstzrange(now(), now()+interval '1h','[)'),false,1,0,now()+interval '5 min',now())""", (t["tid"], t["sid"]))


def test_submit_without_hold_or_after_expiry(conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    assert "HOLD_NOT_FOUND" in str(err(submit, conn, t, att))
    hold(conn, t, att)
    conn.execute("UPDATE setuhaul.dock_occupancy SET expires_at = now() - interval '1 s' WHERE ref_id=%s", (str(t["oid"]),))
    assert "HOLD_EXPIRED" in str(err(submit, conn, t, att))


# ---------------------------------------------------------------- P0-3: a person owns the thread
def takeover(conn, t, scope=JAI):
    run_staff(conn, scope, ("operations",), "staff:ops1", "SELECT take_over_thread(%s,'staff:ops1','driver upset')", (t["tid"],))


def test_takeover_blocks_hold_submit_readback_and_attestation(conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    hold(conn, t, att)
    takeover(conn, t)
    state = conn.execute("""SELECT t.human_active, t.status, f.state FROM setuhaul.chat_threads t
                            JOIN setuhaul.thread_flow f USING (thread_id) WHERE t.thread_id=%s""", (t["tid"],)).fetchone()
    assert state == (True, "WAITING_FOR_HUMAN", "HUMAN")                  # atomic: thread, status and flow together
    e = str(err(submit, conn, t, att))
    assert "ATTESTATION_EXPIRED" in e or "HUMAN_ACTIVE" in e             # takeover expires attestations
    assert "HUMAN_ACTIVE" in str(err(read_back, conn, t))
    t2 = setup_thread(conn)
    read_back(conn, t2)
    takeover(conn, t2)
    assert attest(conn, t2, driver_says(conn, t2)) is None              # no automated confirmations for a person's thread
    assert "HUMAN_ACTIVE" in str(err(run, conn, "setuhaul_agent", JAI, t2["drv"],
                                     "SELECT escalate_thread(%s,'DRIVER_REQUEST','DECIDE','x','y',%s,'agent')", (t2["tid"], k())))


def test_handback_starts_the_agent_clean(conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    takeover(conn, t)
    e = err(run_staff, conn, JAI, ("operations",), "staff:ops1", "SELECT hand_back_thread(%s,'staff:ops1','short')", (t["tid"],))
    assert "HANDBACK_SUMMARY_REQUIRED" in str(e)
    run_staff(conn, JAI, ("operations",), "staff:ops1", "SELECT hand_back_thread(%s,'staff:ops1','Called driver; slot stays as is')", (t["tid"],))
    st = conn.execute("SELECT human_active FROM setuhaul.chat_threads WHERE thread_id=%s", (t["tid"],)).fetchone()[0]
    assert st is False
    assert "ATTESTATION_EXPIRED" in str(err(hold, conn, t, att))         # nothing from before the takeover is reusable
    assert "OPTION_EXPIRED" in str(err(read_back, conn, t))


def test_agent_and_console_cannot_flip_the_takeover_flag_directly(conn):
    t = setup_thread(conn)
    for role in ("setuhaul_agent", "setuhaul_console"):
        assert denied(run, conn, role, JAI, t["drv"] if role == "setuhaul_agent" else None,
                      "UPDATE chat_threads SET human_active = false WHERE thread_id=%s", (t["tid"],)), role


# ---------------------------------------------------------------- P0-2: ETA provenance and idempotency
def eta(conn, t, msg, key=None, shipment=None, hours=2, reason="TRAFFIC", when=None):
    when = when or datetime.now(timezone.utc) + timedelta(hours=hours)
    return run(conn, "setuhaul_agent", t["fac"], t["drv"],
               "SELECT record_driver_eta(%s,%s,%s,%s,'PRECISE',%s,%s,'agent')",
               (t["tid"], msg, shipment or t["sid"], when, reason, key or k()))[0]


def test_eta_requires_a_driver_message_in_this_thread(conn):
    t, other = setup_thread(conn), setup_thread(conn)
    foreign = driver_says(conn, other)
    agent_msg = run(conn, "setuhaul_agent", JAI, t["drv"], "INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked) "
                    "VALUES (%s,'AGENT','WHATSAPP','h','x') RETURNING message_id", (t["tid"],))[0]
    for msg in (foreign, agent_msg, 987654321):
        assert "MESSAGE_NOT_FROM_DRIVER" in str(err(eta, conn, t, msg)), msg


def test_eta_must_be_for_this_threads_shipment(conn):
    t, other = setup_thread(conn), setup_thread(conn)
    msg = driver_says(conn, t)
    assert "SHIPMENT_NOT_IN_THREAD" in str(err(eta, conn, t, msg, None, other["sid"]))
    assert "ETA_OUT_OF_RANGE" in str(err(eta, conn, t, msg, None, None, 24 * 30))


def test_eta_recording_is_idempotent(conn):
    t = setup_thread(conn)
    msg, key, when = driver_says(conn, t), k(), datetime.now(timezone.utc) + timedelta(hours=2)
    a, b = eta(conn, t, msg, key, when=when), eta(conn, t, msg, key, when=when)
    assert a == b and a["new_signal"] is True
    c = eta(conn, t, msg, when=when)                                                  # same message, new key: still one signal
    assert c["new_signal"] is False
    n = conn.execute("SELECT count(*) FROM setuhaul.eta_signals WHERE shipment_id=%s AND source='DRIVER_DECLARED'", (t["sid"],)).fetchone()[0]
    ex = conn.execute("SELECT count(*) FROM setuhaul.driver_exceptions WHERE shipment_id=%s", (t["sid"],)).fetchone()[0]
    assert (n, ex) == (1, 1)


def test_agent_cannot_write_signals_or_exceptions_directly(conn):
    t = setup_thread(conn)
    assert denied(run, conn, "setuhaul_agent", JAI, t["drv"],
                  "INSERT INTO eta_signals (shipment_id,source,observed_at,derived_eta,confidence) VALUES (%s,'DRIVER_DECLARED',now(),now(),'LOW')",
                  (t["sid"],))
    assert denied(run, conn, "setuhaul_agent", JAI, t["drv"],
                  "INSERT INTO driver_exceptions VALUES ('E',%s,%s,%s,'DELAY','OPEN','dk',now())", (t["sid"], t["drv"], t["tid"]))


# ---------------------------------------------------------------- P0-3 / P1-4 / P1-5: escalation
def escalate(conn, t, mode="DECIDE", reason="DRIVER_REQUEST", key=None, scope=None):
    return run(conn, "setuhaul_agent", scope if scope is not None else t["fac"], t["drv"],
               "SELECT escalate_thread(%s,%s,%s,'driver text','we told them',%s,'agent')", (t["tid"], reason, mode, key or k()))[0]


def test_take_over_escalation_is_atomic_and_durable(conn):
    t = setup_thread(conn)
    r = escalate(conn, t, "TAKE_OVER", "SAFETY")
    row = conn.execute("""SELECT e.mode, e.reason_code, e.facility_id, t.human_active, f.state FROM setuhaul.escalations e
                          JOIN setuhaul.chat_threads t USING (thread_id) JOIN setuhaul.thread_flow f USING (thread_id)
                          WHERE e.escalation_id=%s""", (r["escalation_id"],)).fetchone()
    assert row == ("TAKE_OVER", "SAFETY", JAI, True, "HUMAN")


def test_escalation_is_idempotent(conn):
    t = setup_thread(conn)
    key = k()
    a, b = escalate(conn, t, key=key), escalate(conn, t, key=key)
    c = escalate(conn, t)                                                  # new turn, same reason: same open escalation
    assert a == b and c["escalation_id"] == a["escalation_id"]
    n = conn.execute("SELECT count(*) FROM setuhaul.escalations WHERE thread_id=%s", (t["tid"],)).fetchone()[0]
    assert n == 1


def test_unknown_shipment_goes_to_the_central_queue(conn):
    t = setup_thread(conn, with_shipment=False)
    r = escalate(conn, t, scope="")
    assert r["queue"] == "CENTRAL"
    fac = conn.execute("SELECT facility_id FROM setuhaul.escalations WHERE escalation_id=%s", (r["escalation_id"],)).fetchone()[0]
    assert fac is None                                                     # never a default facility
    see = "SELECT count(*) FROM escalations WHERE escalation_id=%s"
    assert run(conn, "setuhaul_console", JAI, None, see, (r["escalation_id"],))[0] == 0
    assert run(conn, "setuhaul_console", "CENTRAL", None, see, (r["escalation_id"],))[0] == 1


def test_agent_cannot_insert_escalations_audit_or_outbox(conn):
    t = setup_thread(conn)
    for sql in ("INSERT INTO escalations (escalation_id,reason_code,mode,facility_id,handoff,ack_due_at) VALUES (gen_random_uuid(),'SAFETY','DECIDE',%s,'{}',now())",
                "INSERT INTO booking_events (principal,principal_role,action,facility_id) VALUES ('a','agent','x',%s)",
                "INSERT INTO outbox (facility_id,aggregate,aggregate_id,event_type,payload) VALUES (%s,'appointment','x','BookingConfirmed','{}')"):
        assert denied(run, conn, "setuhaul_agent", JAI, t["drv"], sql, (JAI,)), sql


# ---------------------------------------------------------------- warehouse rules (step 1, unchanged behaviour)
def test_paused_facility_refuses(conn):
    t = setup_thread(conn)
    hold(conn, t)
    run_staff(conn, JAI, ("ops_lead",), "staff:lead1", "SELECT set_automation_paused('FAC-JAI-01', true, 'staff:lead1', 'yard flooded')")
    try:
        assert "AUTOMATION_PAUSED" in str(err(submit, conn, t))
    finally:
        run_staff(conn, JAI, ("ops_lead",), "staff:lead1", "SELECT set_automation_paused('FAC-JAI-01', false, 'staff:lead1', 'resumed')")


def test_no_confirmer_on_shift_goes_to_a_person(conn):
    t = setup_thread(conn, facility=GGN, dock="DOCK-GGN-D1")
    hold(conn, t)
    assert "NO_CONFIRMER_AVAILABLE" in str(err(submit, conn, t))


def test_too_close_to_slot_start_for_warehouse_answer(conn):
    t = setup_thread(conn, start_h=0.5, exact=True)
    hold(conn, t)
    assert "NO_CONFIRMER_AVAILABLE" in str(err(submit, conn, t))


def test_the_retired_auto_confirm_switch_confirms_nothing(conn):
    """B-WF-2 §10: the per-shift switch is no longer read. Only a versioned policy rule could confirm, and every rule
    ships as NEEDS_PLANNER (tests/test_approvals.py covers a forced policy rule)."""
    conn.execute("""UPDATE setuhaul.facility_config SET auto_confirm_chat_bookings='{"DAY":true,"NIGHT":true}'
                    WHERE facility_id='FAC-JAI-01'""")
    try:
        t = setup_thread(conn)
        hold(conn, t)
        assert submit(conn, t)["status"] == "PENDING"
    finally:
        conn.execute("UPDATE setuhaul.facility_config SET auto_confirm_chat_bookings='{}' WHERE facility_id='FAC-JAI-01'")


def test_submit_and_hold_are_idempotent(conn):
    t = setup_thread(conn)
    att, hk = confirm(conn, t), k()
    assert hold(conn, t, att, hk) == hold(conn, t, att, hk)
    key = k()
    assert submit(conn, t, att, key) == submit(conn, t, att, key)
    n = conn.execute("SELECT count(*) FROM setuhaul.appointments WHERE shipment_id=%s", (t["sid"],)).fetchone()[0]
    assert n == 1
    other = setup_thread(conn)
    hold(conn, other)
    assert "IDEMPOTENCY_KEY_REUSED" in str(err(submit, conn, other, None, key))


def test_double_click_approve_returns_first_result(conn):
    t = setup_thread(conn)
    hold(conn, t)
    r = submit(conn, t)
    key = k()
    assert decide(conn, r["appointment_id"], "CONFIRM", key=key) == decide(conn, r["appointment_id"], "CONFIRM", key=key)
    assert "ALREADY_DECIDED" in str(err(decide, conn, r["appointment_id"], "REJECT"))


def test_other_facility_planner_cannot_decide(conn):
    t = setup_thread(conn)
    hold(conn, t)
    r = submit(conn, t)
    assert "APPOINTMENT_UNKNOWN" in str(err(decide, conn, r["appointment_id"], "CONFIRM", GGN))


def test_agent_cannot_decide_expire_block_pause_or_take_over(conn):
    t = setup_thread(conn)
    hold(conn, t)
    r = submit(conn, t)
    for sql in ("SELECT decide_booking_request(%s,'CONFIRM','kkkkkkkkkkkkkkkkkk',NULL)",
                "SELECT expire_pending_requests() WHERE %s::text IS NOT NULL",
                "SELECT block_dock('X',%s,now(),now()+interval '1h','BREAKDOWN','x','agent')",
                "SELECT set_automation_paused(%s,true,'agent','x')",
                "SELECT take_over_thread(%s,'agent','x')",
                "SELECT hand_back_thread(%s,'agent','xxxxxxxxxxxx')"):
        assert denied(run, conn, "setuhaul_agent", JAI, t["drv"], sql, (r["appointment_id"],)), sql


def test_reject_releases_capacity(conn):
    t = setup_thread(conn)
    hold(conn, t)
    r = submit(conn, t)
    decide(conn, r["appointment_id"], "REJECT")
    act = conn.execute("SELECT active FROM setuhaul.dock_occupancy WHERE ref_id=%s", (r["appointment_id"],)).fetchone()[0]
    assert act is False


def short_deadline(conn, seconds=3, dock="DOCK-JAI-D2"):
    """A request whose deadline (slot start minus the 30-minute lead time) is `seconds` away: the real path, no
    tampering with the deadline (B-WF-2 §6)."""
    t = setup_thread(conn, with_shipment=True, start_h=0, exact=True)
    oid = uuid.uuid4()
    with as_role(conn, "setuhaul_feasibility", t["fac"]) as c:
        c.execute("""INSERT INTO shown_options VALUES (%s,%s,%s,%s,
                     tstzrange(now()+interval '30 min'+make_interval(secs=>%s), now()+interval '80 min','[)'),
                     false,1,0,now()+interval '5 min',now())""", (oid, t["tid"], t["sid"], dock, seconds))
    t["oid"] = oid
    hold(conn, t)
    return t, submit(conn, t)


def test_expiry_releases_and_escalates_never_approves(conn):
    import time
    t, r = short_deadline(conn)
    time.sleep(3.2)
    assert "PENDING_EXPIRED" in str(err(decide, conn, r["appointment_id"], "CONFIRM"))   # refused before the sweep
    n = run(conn, "setuhaul_system", f"{JAI},{GGN}", None, "SELECT expire_pending_requests()")[0]
    assert n >= 1
    st = conn.execute("SELECT status FROM setuhaul.appointments WHERE appointment_id=%s", (r["appointment_id"],)).fetchone()[0]
    assert st == "EXPIRED"
    esc = conn.execute("SELECT count(*) FROM setuhaul.escalations WHERE shipment_id=%s AND reason_code='PENDING_EXPIRED'",
                       (t["sid"],)).fetchone()[0]
    assert esc == 1


def test_release_hold_is_idempotent(conn):
    t = setup_thread(conn)
    hold(conn, t)
    sql = "SELECT release_hold(%s,%s,'agent')"
    assert run(conn, "setuhaul_agent", JAI, t["drv"], sql, (t["oid"], t["tid"]))[0] == 1
    assert run(conn, "setuhaul_agent", JAI, t["drv"], sql, (t["oid"], t["tid"]))[0] == 0


def block(conn, scope, sql, args):
    with as_role(conn, "setuhaul_console", scope) as c:
        staff(c, scope, ("planner",), "staff:ops1")
        return c.execute(sql, args).fetchone()


def test_block_dock_refuses_to_overwrite_a_booking(conn):
    """A confirmed booking makes the block fail (23P01); a pending request under the block is invalidated instead
    (B-WF-2 §6, D)."""
    t = setup_thread(conn, dock="DOCK-JAI-D3", start_h=8, exact=True)
    hold(conn, t)
    r = submit(conn, t)
    decide(conn, r["appointment_id"], "CONFIRM")
    e = err(block, conn, JAI,
            "SELECT block_dock(%s,'DOCK-JAI-D3',now()+interval '7 hours',now()+interval '10 hours','BREAKDOWN','hydraulics','staff:ops1')",
            (f"DE-{uuid.uuid4().hex[:6]}",))
    assert e.sqlstate == "23P01"
    eid = f"DE-{uuid.uuid4().hex[:6]}"
    sql = "SELECT block_dock(%s,'DOCK-JAI-D3',now()+interval '20 hours',now()+interval '21 hours','MAINTENANCE','service','staff:ops1')"
    a = block(conn, JAI, sql, (eid,))[0]
    b = block(conn, JAI, sql, (eid,))[0]
    assert a == b
    assert "FORBIDDEN" in str(err(block, conn, GGN, sql, (f"DE-{uuid.uuid4().hex[:6]}",)))
    assert "UNTRUSTED_CONTEXT" in str(err(run, conn, "setuhaul_console", JAI, None, sql, (f"DE-{uuid.uuid4().hex[:6]}",)))



# ---------------------------------------------------------------- 0005 P0-1: a later reply overrides an earlier YES
def test_yes_then_no_old_yes_cannot_be_reused(conn):
    t = setup_thread(conn)
    att_yes = confirm(conn, t, "haan kar do")
    attest(conn, t, driver_says(conn, t, "nahi, ruko"), "NO")
    assert "CONFIRMATION_SUPERSEDED" in str(err(hold, conn, t, att_yes))


def test_yes_then_unclear_old_yes_cannot_be_reused(conn):
    t = setup_thread(conn)
    att_yes = confirm(conn, t, "yes")
    attest(conn, t, driver_says(conn, t, "kya 3 baje ho sakta hai?"), "UNCLEAR")
    assert "CONFIRMATION_SUPERSEDED" in str(err(hold, conn, t, att_yes))


def test_any_later_driver_message_supersedes_even_if_not_attested(conn):
    """The later message's attestation failed (or has not run yet): the old YES is still not usable."""
    t = setup_thread(conn)
    att_yes = confirm(conn, t, "yes")
    driver_says(conn, t, "wait")
    assert "CONFIRMATION_SUPERSEDED" in str(err(hold, conn, t, att_yes))


def test_no_after_hold_stops_the_request(conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    hold(conn, t, att)
    attest(conn, t, driver_says(conn, t, "cancel"), "NO")
    assert "CONFIRMATION_SUPERSEDED" in str(err(submit, conn, t, att))


def test_late_retry_of_an_older_message_never_overrides_a_newer_one(conn):
    t = setup_thread(conn)
    read_back(conn, t)
    older = driver_says(conn, t, "nahi")
    newer = driver_says(conn, t, "haan")
    att_new = attest(conn, t, newer, "YES")["attestation_id"]
    assert attest(conn, t, older, "NO") is None                  # e.g. a webhook retry arriving late
    assert hold(conn, t, att_new)["status"] == "HELD"


def test_superseded_attestations_are_expired(conn):
    t = setup_thread(conn)
    att_yes = confirm(conn, t, "yes")
    attest(conn, t, driver_says(conn, t, "no"), "NO")
    exp = conn.execute("SELECT expires_at <= now() FROM setuhaul.confirmation_attestations WHERE attestation_id=%s", (att_yes,)).fetchone()[0]
    assert exp is True


# ---------------------------------------------------------------- 0005 P0-2 / P1-2
def test_ingest_cannot_write_agent_messages_or_attestations(conn):
    t = setup_thread(conn)
    assert denied(run, conn, "setuhaul_ingest", None, t["drv"],
                  "INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked) VALUES (%s,'AGENT','WHATSAPP','h','x')",
                  (t["tid"],))
    rb = read_back(conn, t)
    msg = driver_says(conn, t)
    assert denied(run, conn, "setuhaul_ingest", None, t["drv"],
                  "INSERT INTO confirmation_attestations VALUES (gen_random_uuid(),%s,%s,%s,%s,'YES','x','i',now(),now()+interval '1h')",
                  (rb["selection_id"], t["tid"], t["oid"], msg))


def test_system_role_is_no_longer_the_inbound_path(conn):
    t = setup_thread(conn)
    msg = driver_says(conn, t)
    for sql in ("SELECT attest_confirmation(%s,%s,'YES','x','system')",
                "INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked) VALUES (%s,'DRIVER','WHATSAPP','h',%s::text)"):
        assert denied(run, conn, "setuhaul_system", JAI, t["drv"], sql, (t["tid"], msg)), sql


def test_central_escalation_emits_a_durable_event(conn):
    t = setup_thread(conn, with_shipment=False)
    r = escalate(conn, t, scope="")
    q = "SELECT count(*) FROM outbox WHERE aggregate='escalation' AND aggregate_id=%s"
    assert run(conn, "setuhaul_system", "CENTRAL", None, q, (r["escalation_id"],))[0] == 1
    assert run(conn, "setuhaul_system", JAI, None, q, (r["escalation_id"],))[0] == 0
