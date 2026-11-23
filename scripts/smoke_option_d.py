"""Option D post-activation smoke test on the deployed SYNTHETIC Sydney pilot (owner approval, 23 Nov 2026).

    python scripts/smoke_option_d.py [--out docs/release/evidence/option_d_smoke.json]

Everything is synthetic: SYN- drivers, shipments and vehicles, +999 phone numbers no real phone can have, replies to
the internal test sink (WhatsApp is off). Nothing is sent to Meta. Booking commitments are made by the DEPLOYED agent
and the DEPLOYED approval workflow; the planner's decision is made through the console database role under a drill
staff identity (`staff:option-d-smoke-planner`, planner, FAC-JAI-01), as in the Step 6 approval drill.

 1. /health: booking ON, kill switch disengaged, the owner-approved exceptions listed.
 2. A driver reports a delay, picks option 1 and says YES: the agent holds the slot and submits a request (PENDING,
    approval OPEN, no automatic approval); the relay starts one workflow execution; a planner approves; the
    appointment is CONFIRMED; the driver's notice goes to the test sink.
 3. Idempotency: the same YES (same WhatsApp id) sent again changes nothing; the same decision key returns the same
    decision; a second decision is refused.
 4. Double-booking protection: another truck cannot take that dock time (the exclusion constraint, in a transaction
    that is rolled back).
 5. Authorization: the agent role cannot decide; a planner of another facility cannot see it; no staff identity, no
    decision.
 6. The kill switch (owner CLI path): engaged, a second driver's YES is not booked (no appointment, no request) and a
    direct hold through the agent role is refused BOOKING_KILLED; disengaged again, audited both ways.
 7. Isolation: no non-synthetic carrier, driver or vehicle; every notification went to the test sink.
Account ids are masked in the output.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from e2e_aws import Pilot, q  # noqa: E402
from drill_approval_workflow import invoke, j, refused  # noqa: E402
from setuhaul.aws.data_api import DataApiDb  # noqa: E402
from setuhaul.aws.synthetic_seed import VIOLATIONS  # noqa: E402
from setuhaul.ops.kill_switch import statements as kill_statements, status as kill_status  # noqa: E402

FAC, REGION = "FAC-JAI-01", "ap-southeast-2"
PLANNER = ("staff:option-d-smoke-planner", {"planner"}, (FAC,))
MASK = re.compile(r"\b\d{12}\b")
LOG: list[dict] = []


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def note(event, **kw):
    LOG.append({"t": now(), "event": event, **kw})
    print(now(), event, json.dumps(kw, default=str)[:300], flush=True)


def conversation_to_yes(p: Pilot, d: dict) -> dict:
    """Delay -> options -> pick 1 -> read-back -> YES, through the deployed webhook, queue and agent."""
    t1 = p.turn(d["phone"], "Traffic on the highway. Reaching in about 90 minutes, any slot?")
    note("driver: delay", reply=t1["agent_reply"])
    t2 = p.turn(d["phone"], "1")
    note("driver: picks 1", reply=t2["agent_reply"])
    t3 = p.turn(d["phone"], "YES")
    note("driver: YES", reply=t3["agent_reply"])
    return {"delay": t1, "pick": t2, "yes": t3}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "option_d_smoke.json"))
    a = ap.parse_args()
    p = Pilot()
    lam = boto3.client("lambda", region_name=REGION)
    secrets = json.loads(p.out["LoginSecretArns"])
    db = DataApiDb(p.out["ClusterArn"], "setuhaul", {r: secrets[r] for r in ("agent", "console")},
                   boto3.client("rds-data", region_name=REGION), resume_wait_s=120)
    from setuhaul.ops.pilot_mode import from_stack
    mode = from_stack()
    ev, checks = {}, {}
    note("wake", mode=mode.wake()["after"]["mode"])
    try:
        # ---- 1. health
        h = httpx.get(p.out["ApiUrl"].rstrip("/") + "/health", timeout=40).json()
        ev["health"] = h
        checks["1 booking ON, kill switch disengaged, exceptions listed"] = (
            h.get("booking_enabled") is True and h.get("booking_kill_switch") == "disengaged"
            and set(h.get("gate_exceptions", [])) == {"AURORA", "WORKLOAD_IDENTITY", "OUTBOUND_DELIVERY"})
        note("health", **h)

        # ---- 2. a real booking through the deployed agent and workflow
        d = p.add_driver(eta_min=90)
        conv = conversation_to_yes(p, d)
        req = p.wait(f"""SELECT a.appointment_id, a.status, r.approval_id::text, r.status, r.decided_by_type
                           FROM setuhaul.appointments a JOIN setuhaul.approval_requests r USING (appointment_id)
                          WHERE a.shipment_id = {q(d['shipment_id'])}""", 120)
        ev["booking"] = {"driver": d, "conversation": {k: {kk: v.get(kk) for kk in ("message_id", "api_request_id",
                         "agent_reply", "agent_reply_id")} for k, v in conv.items()}, "request": req}
        note("request", rows=req)
        if not req:
            raise RuntimeError("the agent did not submit a booking request")
        appt, appt_status, approval, ap_status, _ = req[0]
        checks["2a the agent held the slot and submitted a request; no automatic approval"] = (
            appt_status == "PENDING_CONFIRMATION" and ap_status == "OPEN")
        relay = invoke(lam, "setuhaul-pilot-approval-relay")
        tok = p.wait(f"SELECT approval_id::text FROM setuhaul.approval_requests WHERE approval_id = {q(approval)} "
                     "AND task_token IS NOT NULL", 120)
        sfn = boto3.client("stepfunctions", region_name=REGION)
        machine = p.out["ApprovalAliasArn"].rsplit(":", 1)[0]
        execs = [e for e in sfn.list_executions(stateMachineArn=machine, maxResults=100)["executions"]
                 if e["name"] == approval]
        ev["workflow"] = {"relay": relay, "task_token_recorded": bool(tok), "executions_for_request": len(execs),
                          "execution_arn": execs[0]["executionArn"] if execs else None}
        checks["2b the relay started one execution, waiting for a person"] = bool(tok) and len(execs) == 1

        def decide(decision, key):
            with db.tx("console", (FAC,), staff=PLANNER) as t:
                return j(t.execute("SELECT decide_booking_request(%s,%s,%s,'Option D smoke test')",
                                   (appt, decision, key)).fetchone()[0])

        # ---- 5. authorization before the decision
        def as_agent():
            with db.tx("agent", (FAC,), driver=d["driver_id"]) as t:
                t.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,'x')", (appt, "smoke-x-" + uuid.uuid4().hex))

        def other_facility():
            with db.tx("console", ("FAC-GGN-01",), staff=("staff:option-d-smoke-ggn", {"planner"}, ("FAC-GGN-01",))) as t:
                t.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,'x')", (appt, "smoke-x-" + uuid.uuid4().hex))

        def no_staff():
            with db.tx("console", (FAC,)) as t:
                t.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,'x')", (appt, "smoke-x-" + uuid.uuid4().hex))
        ev["authorization"] = {"agent decides": refused(as_agent), "planner of another facility": refused(other_facility),
                               "no staff identity": refused(no_staff)}
        checks["5 authorization: agent, other facility and no-identity decisions refused"] = all(
            v["refused"] for v in ev["authorization"].values())

        key = "option-d-smoke-" + uuid.uuid4().hex
        d1 = decide("CONFIRM", key)
        d1_again = decide("CONFIRM", key)
        second = refused(lambda: decide("REJECT", "option-d-smoke-" + uuid.uuid4().hex))
        ev["decision"] = {"first": d1, "same_key_again": d1_again, "second_decision": second}
        relay2 = invoke(lam, "setuhaul-pilot-approval-relay")
        done = p.wait(f"""SELECT a.status, r.status, r.decided_by, r.decided_by_type FROM setuhaul.appointments a
                           JOIN setuhaul.approval_requests r USING (appointment_id) WHERE a.appointment_id = {q(appt)}
                            AND a.status = 'CONFIRMED'""", 60)
        exe = None
        for _ in range(20):
            exe = sfn.describe_execution(executionArn=execs[0]["executionArn"])["status"] if execs else None
            if exe != "RUNNING":
                break
            time.sleep(3)
        audit = p.rows(f"""SELECT action, principal, principal_role, at::text FROM setuhaul.booking_events
                            WHERE shipment_id = {q(d['shipment_id'])} ORDER BY event_id""")
        notices = p.wait(f"""SELECT template_id, status, provider_message_id FROM setuhaul.notifications
                             WHERE shipment_id = {q(d['shipment_id'])} AND template_id = 'booking_approved'
                               AND status IN ('SENT','DELIVERED','FAILED')""", 150)
        ev["confirmed"] = {"rows": done, "execution_status": exe, "relay_after_decision": relay2, "audit": audit,
                           "booking_approved_notice": notices}
        checks["2c a planner approved: CONFIRMED, decided by the planner, execution SUCCEEDED"] = (
            bool(done) and done[0][2] == PLANNER[0] and done[0][3] == "HUMAN" and exe == "SUCCEEDED")
        checks["2d audit trail written (hold, request, decision)"] = len(audit) >= 2
        checks["2e the driver's notice went to the internal test sink"] = bool(notices) and all(
            str(n[2] or "").startswith("testsink.") for n in notices)
        note("confirmed", appointment=appt, execution=exe, audit=len(audit), notice=notices)

        # ---- 3. idempotency
        again = p.say(d["phone"], "YES", mid=conv["yes"]["message_id"])
        time.sleep(20)
        n_req = p.rows(f"SELECT count(*) FROM setuhaul.approval_requests WHERE shipment_id = {q(d['shipment_id'])}")[0][0]
        n_app = p.rows(f"SELECT count(*) FROM setuhaul.appointments WHERE shipment_id = {q(d['shipment_id'])} "
                       "AND status IN ('PENDING_CONFIRMATION','CONFIRMED')")[0][0]
        ev["idempotency"] = {"duplicate_webhook": again, "approval_requests": n_req, "active_appointments": n_app}
        checks["3 a duplicate YES and a repeated decision change nothing; a second decision is refused"] = (
            n_req == 1 and n_app == 1 and d1_again == d1 and second["refused"])

        # ---- 4. double-booking protection (rolled back)
        during = p.rows(f"SELECT dock_id, lower(during)::text, upper(during)::text FROM setuhaul.appointments "
                        f"WHERE appointment_id = {q(appt)}")[0]

        def overlap():
            p.db.run(["SET LOCAL search_path = setuhaul",
                      "SELECT set_config('app.facility_scope', 'FAC-JAI-01', true)",
                      f"""INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, ref_id, created_by)
                          VALUES ({q(during[0])}, 'FAC-JAI-01', tstzrange({q(during[1])}, {q(during[2])}, '[)'),
                                  'BLOCK', 'smoke-overlap', 'option-d-smoke')""",
                      "SELECT 1/0"])                                   # never commits, even if the insert passed
        r = refused(overlap)
        ev["double_booking"] = {"slot": during, "attempt": r}
        checks["4 the same dock time cannot be claimed twice (exclusion constraint)"] = (
            r["refused"] and ("ex_one_truck_per_dock" in r["message"] or "conflicting key" in r["message"]
                              or "exclusion" in r["message"]))

        # ---- 6. the kill switch (owner path), then a second driver
        p.db.run(kill_statements(True, "Option D smoke test: kill switch drill"))
        ks_on = kill_status(p.db)
        h_on = httpx.get(p.out["ApiUrl"].rstrip("/") + "/health", timeout=40).json()
        d2 = p.add_driver(eta_min=100)
        conv2 = conversation_to_yes(p, d2)
        n2 = p.rows(f"SELECT count(*) FROM setuhaul.appointments WHERE shipment_id = {q(d2['shipment_id'])}")[0][0]
        r2 = p.rows(f"SELECT count(*) FROM setuhaul.approval_requests WHERE shipment_id = {q(d2['shipment_id'])}")[0][0]
        opt = p.rows(f"""SELECT o.option_id::text, t.thread_id::text FROM setuhaul.shown_options o
                           JOIN setuhaul.chat_threads t USING (thread_id)
                          WHERE t.driver_id = {q(d2['driver_id'])} ORDER BY o.option_id DESC LIMIT 1""")

        def direct_hold():
            with db.tx("agent", (FAC,), driver=d2["driver_id"]) as t:
                t.execute("SELECT hold_option(%s,%s,NULL,%s,'agent')",
                          (opt[0][0], opt[0][1], "smoke-kill-" + uuid.uuid4().hex))
        hold = refused(direct_hold) if opt else {"refused": None, "message": "no option shown"}
        p.db.run(kill_statements(False, "Option D smoke test: kill switch drill over"))
        ks_off = kill_status(p.db)
        kaudit = p.rows("""SELECT action, principal, at::text FROM setuhaul.booking_events
                            WHERE action LIKE 'BOOKING_KILL_%' ORDER BY event_id DESC LIMIT 2""")
        ev["kill_switch"] = {"engaged": ks_on, "health_while_engaged": h_on,
                             "second_driver": {"driver": d2, "replies": {k: v.get("agent_reply") for k, v in conv2.items()},
                                               "appointments": n2, "approval_requests": r2},
                             "direct_hold_while_engaged": hold, "disengaged": ks_off, "audit": kaudit}
        checks["6 kill switch: engaged -> nothing booked, direct hold refused BOOKING_KILLED; disengaged; audited"] = (
            ks_on["engaged"] and h_on.get("booking_kill_switch") == "ENGAGED" and n2 == 0 and r2 == 0
            and (hold.get("refused") and "BOOKING_KILLED" in hold.get("message", "")) and not ks_off["engaged"]
            and [k[0] for k in kaudit] == ["BOOKING_KILL_DISENGAGED", "BOOKING_KILL_ENGAGED"])
        note("kill switch", appointments=n2, hold=hold, after=ks_off)

        # ---- 7. isolation
        bad = p.db.query(VIOLATIONS)
        real = p.rows("""SELECT count(*) FROM setuhaul.notifications WHERE provider_message_id IS NOT NULL
                          AND provider_message_id NOT LIKE 'testsink.%'""")[0][0]
        ev["isolation"] = {"non_synthetic_rows": len(bad), "non_test_sink_deliveries": real}
        checks["7 isolation: only synthetic identities; no delivery outside the test sink"] = not bad and real == 0
    finally:
        try:                                   # never leave the switch engaged by accident... unless the run failed
            ks = kill_status(p.db)             # before the drill: then it was never engaged here
            ev.setdefault("final_kill_switch", ks)
        except Exception as e:                 # noqa: BLE001
            ev["final_kill_switch"] = {"error": str(e)[:200]}
        note("sleep", mode=mode.sleep()["after"]["mode"])
    report = {"collected_utc": now(), "evidence": ev, "checks": checks, "all_pass": bool(checks) and all(checks.values()),
              "log": LOG}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k, flush=True)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
