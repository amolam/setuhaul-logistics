"""BOOKING_WORKFLOW gate: the approval workflow on the deployed synthetic pilot (owner-approved 23 Nov 2026).

    python scripts/drill_approval_workflow.py [--out docs/release/evidence/drill_approval.json]

Three synthetic booking requests (SYN-STEP6-* drivers and shipments, +999 phone numbers) go through the deployed
approval workflow, one per path:
  APPROVE  - a planner confirms it in the console role  -> appointment CONFIRMED, claim kept
  REJECT   - a planner rejects it                       -> appointment REJECTED, claim released
  EXPIRE   - nobody decides; the Step Functions timeout  -> request EXPIRED, claim released

How each request is made. The booking switch is OFF on every Lambda and stays OFF, so the agent's API refuses booking
tools. The drill therefore calls the same database functions the agent would, each under its own workload login
(RDS Data API secrets from the stack): ingest opens the thread and records the driver's reply, feasibility publishes the
option, the agent records the read-back, holds and submits. The deployed approval-relay Lambda starts the Step
Functions executions and delivers the decision callbacks; the deployed state machine and its three task Lambdas wait,
record task tokens and expire. Decisions go through decide_booking_request in the console role under a trusted staff
identity (what the console API does after verifying a Cognito token).

Also checked: no automatic approval (all three wait for a person), refusals (agent role, wrong facility, no staff
identity, a second decision), idempotency (same key twice gives one decision; a second relay run starts nothing; one
execution per request; one decided event and one notification per outcome), notifications only to the internal test
sink. The drill stops before any write if a non-synthetic driver, vehicle or carrier exists. Account ids are masked.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.aws.data_api import DataApiDb  # noqa: E402
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402
from setuhaul.aws.synthetic_seed import VIOLATIONS, synthetic_phone  # noqa: E402

REGION, STACK, FAC = "ap-southeast-2", "SetuHaulPilot", "FAC-JAI-01"
IST = timezone(timedelta(hours=5, minutes=30))
MASK = re.compile(r"\b\d{12}\b")
LOG: list[dict] = []
PLANNER = ("staff:step6-drill-planner", {"planner"}, (FAC,))


def c(name):
    return boto3.client(name, region_name=REGION)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def note(event, **kw):
    LOG.append({"t": now(), "event": event, **kw})
    print(now(), event, json.dumps(kw, default=str)[:300], flush=True)


def j(v):
    return v if isinstance(v, (dict, list)) or v is None else json.loads(v)


def refused(fn):
    try:
        fn()
        return {"refused": False}
    except Exception as e:                                                      # noqa: BLE001 — the expected refusal
        return {"refused": True, "sqlstate": getattr(e, "sqlstate", None),
                "message": (getattr(getattr(e, "diag", None), "message_primary", None) or str(e))[:140]}


def invoke(lam, name):
    r = lam.invoke(FunctionName=name, InvocationType="RequestResponse", Payload=b"{}")
    body = json.loads(r["Payload"].read() or b"null")
    return {"status": r["StatusCode"], "error": r.get("FunctionError"), "result": body}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "drill_approval.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    logins = json.loads(out["LoginSecretArns"])
    rd, lam, sfn = c("rds-data"), c("lambda"), c("stepfunctions")
    owner = OwnerSession(rd, out["ClusterArn"], out["OwnerSecretArn"])
    db = DataApiDb(out["ClusterArn"], "setuhaul", {r: logins[r] for r in ("agent", "ingest", "feasibility", "console", "system")},
                   rd, resume_wait_s=120)
    scope = f"SELECT set_config('app.facility_scope', '{FAC},FAC-GGN-01', true)"
    q = lambda sql: owner.query(sql, (scope,))  # noqa: E731
    machine = out["ApprovalAliasArn"].rsplit(":", 1)[0]
    ev: dict = {"machine": machine.rsplit(":", 1)[-1], "alias": out["ApprovalAliasArn"].rsplit(":", 2)[-2:]}

    # ---- 0. preconditions and the before picture
    bad = owner.query(VIOLATIONS)
    flags = {f["FunctionName"]: f.get("Environment", {}).get("Variables", {}).get("SETUHAUL_BOOKING_ENABLED")
             for f in lam.list_functions()["Functions"] if f["FunctionName"].startswith("setuhaul-pilot")}
    before = {"approval_requests": q("SELECT status, count(*) FROM setuhaul.approval_requests GROUP BY 1 ORDER BY 1"),
              "appointments": q("SELECT status, count(*) FROM setuhaul.appointments GROUP BY 1 ORDER BY 1"),
              "executions": len(sfn.list_executions(stateMachineArn=machine, maxResults=100)["executions"]),
              "booking_switch": flags, "non_synthetic_rows": [list(x) for x in bad]}
    ev["before"] = before
    note("before", **{k: v for k, v in before.items() if k != "booking_switch"})
    if bad or any(v not in ("false", None) for v in flags.values()):
        note("STOP: non-synthetic data present or a booking switch is not false")
        Path(a.out).write_text(json.dumps({"evidence": ev, "stopped": True, "log": LOG}, indent=1, default=str) + "\n")
        return 2

    # ---- 1. three synthetic requests, made by the roles that make them in production
    day = (datetime.now(IST) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    reqs = {}
    for path, hour in (("APPROVE", 9), ("REJECT", 10), ("EXPIRE", 11)):
        u = uuid.uuid4().hex[:8].upper()
        drv, veh, sid, tid = f"SYN-STEP6-DRV-{u}", f"SYN-STEP6-VEH-{u}", f"SYN-STEP6-SHP-{u}", f"THR-STEP6-{u}"
        phone = synthetic_phone(random.randrange(10**6, 10**10))
        owner.run(["SET LOCAL search_path = setuhaul", scope,
                   f"INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES ('{drv}','SYN-CAR1','Step 6 drill','{phone}')",
                   f"INSERT INTO vehicles VALUES ('{veh}','SYN-CAR1','32FT','SYN-STEP6-REG-{u}',20000,false)",
                   "INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,"
                   "load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status) VALUES "
                   f"('{sid}','SYN-STEP6-ORD-{u}','SYN-CAR1','{drv}','{veh}','{FAC}',10000,45,now()-interval '3 hours',"
                   "now()+interval '2 hours','IN_TRANSIT')"])
        start = day.replace(hour=hour)
        oid = uuid.uuid4()
        with db.tx("ingest", None, driver=drv) as t:
            t.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')", (tid, drv, sid))
        with db.tx("feasibility", (FAC,)) as t:
            t.execute("""INSERT INTO shown_options VALUES (%s,%s,%s,'DOCK-JAI-D3',tstzrange(%s,%s,'[)'),false,1,0,
                         now()+interval '5 min',now())""", (oid, tid, sid, start, start + timedelta(minutes=55)))
        with db.tx("agent", (FAC,), driver=drv) as t:
            t.execute("SELECT record_read_back(%s,%s,%s,'agent')",
                      (oid, tid, "You selected dock D3. Shall I request it? Reply YES to confirm."))
        with db.tx("ingest", None, driver=drv) as t:
            msg = t.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked)
                               VALUES (%s,'DRIVER','WHATSAPP',md5(%s),'YES') RETURNING message_id""",
                            (tid, "YES" + uuid.uuid4().hex)).fetchone()[0]
            att = j(t.execute("SELECT attest_confirmation(%s,%s,'YES','lexicon-v1','ingest')", (tid, msg)).fetchone()[0])
        with db.tx("agent", (FAC,), driver=drv) as t:
            held = j(t.execute("SELECT hold_option(%s,%s,%s,%s,'agent')",
                               (oid, tid, att["attestation_id"], "step6-hold-" + u)).fetchone()[0])
        with db.tx("agent", (FAC,), driver=drv) as t:
            sub = j(t.execute("SELECT submit_booking_request(%s,%s,%s,%s,'agent')",
                              (oid, tid, att["attestation_id"], "step6-submit-" + u)).fetchone()[0])
        reqs[path] = {"driver": drv, "shipment": sid, "thread": tid, "appointment": sub["appointment_id"],
                      "approval_id": str(sub["approval_id"]), "submit_status": sub["status"], "hold_status": held.get("status"),
                      "start_ist": start.isoformat(), "pending_expires_at": sub.get("pending_expires_at")}
        note(f"{path}: request submitted", **reqs[path])
    ev["requests"] = reqs

    # ---- 2. the deployed relay starts one execution per request; a second run starts nothing
    r1 = invoke(lam, "setuhaul-pilot-approval-relay")
    r2 = invoke(lam, "setuhaul-pilot-approval-relay")
    ev["relay"] = {"first": r1, "second": r2}
    note("relay runs", first=r1, second=r2)
    for _ in range(40):                                   # the token task stores each execution's task token
        tok = q("SELECT approval_id::text, task_token IS NOT NULL, status FROM setuhaul.approval_requests "
                f"WHERE approval_id IN ({','.join(repr(r['approval_id']) for r in reqs.values())})")
        if all(x[1] for x in tok):
            break
        time.sleep(3)
    ev["waiting"] = {x[0]: {"token_recorded": x[1], "status": x[2]} for x in tok}
    note("all three waiting for a person", waiting=ev["waiting"])

    # ---- 3. refusals before any decision
    ap_, rj = reqs["APPROVE"], reqs["REJECT"]

    def as_agent():
        with db.tx("agent", (FAC,), driver=ap_["driver"]) as t:
            t.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,'drill')", (ap_["appointment"], "step6-x-" + uuid.uuid4().hex))

    def other_facility():
        with db.tx("console", ("FAC-GGN-01",), staff=("staff:step6-ggn", {"planner"}, ("FAC-GGN-01",))) as t:
            t.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,'drill')", (ap_["appointment"], "step6-x-" + uuid.uuid4().hex))

    def no_staff():
        with db.tx("console", (FAC,)) as t:
            t.execute("SELECT decide_booking_request(%s,'CONFIRM',%s,'drill')", (ap_["appointment"], "step6-x-" + uuid.uuid4().hex))
    ev["refusals_before_decision"] = {"agent role decides": refused(as_agent),
                                      "planner of another facility decides": refused(other_facility),
                                      "console without a staff identity decides": refused(no_staff)}
    note("refusals", **ev["refusals_before_decision"])

    # ---- 4. decisions by a planner; the same key twice is one decision; a second decision is refused
    def decide(appt, decision, key):
        with db.tx("console", (FAC,), staff=PLANNER) as t:
            return j(t.execute("SELECT decide_booking_request(%s,%s,%s,'Step 6 drill')", (appt, decision, key)).fetchone()[0])
    k1, k2 = "step6-decide-" + uuid.uuid4().hex, "step6-decide-" + uuid.uuid4().hex
    d1 = decide(ap_["appointment"], "CONFIRM", k1)
    d1_again = decide(ap_["appointment"], "CONFIRM", k1)
    d2 = decide(rj["appointment"], "REJECT", k2)
    ev["decisions"] = {"APPROVE": d1, "APPROVE_same_key_again": d1_again, "REJECT": d2,
                       "second_decision_new_key": refused(lambda: decide(ap_["appointment"], "REJECT", "step6-decide-" + uuid.uuid4().hex))}
    note("decisions", approve=d1, reject=d2)
    ev["relay_after_decisions"] = invoke(lam, "setuhaul-pilot-approval-relay")
    note("relay after decisions", **ev["relay_after_decisions"])

    # ---- 5. expiry: wait for the Step Functions timeout of the third request
    exp_id = reqs["EXPIRE"]["approval_id"]
    for _ in range(80):
        st = q(f"SELECT status FROM setuhaul.approval_requests WHERE approval_id = '{exp_id}'")[0][0]
        if st != "OPEN":
            break
        time.sleep(15)
    note("expiry path", status=st)
    for _ in range(40):                                   # let the executions finish
        stat = {p: sfn.describe_execution(executionArn=f"{machine.replace(':stateMachine:', ':execution:')}:{r['approval_id']}")["status"]
                for p, r in reqs.items()}
        if all(s != "RUNNING" for s in stat.values()):
            break
        time.sleep(6)
    ev["relay_final"] = invoke(lam, "setuhaul-pilot-approval-relay")     # a no-op: nothing left to start or call back
    ev["outbound"] = invoke(lam, "setuhaul-pilot-outbound")              # driver notices go to the internal test sink

    # ---- 6. evidence per request
    paths = {}
    for p, r in reqs.items():
        arn = f"{machine.replace(':stateMachine:', ':execution:')}:{r['approval_id']}"
        ex = sfn.describe_execution(executionArn=arn)
        hist = sfn.get_execution_history(executionArn=arn, maxResults=100)["events"]
        states = [h["stateEnteredEventDetails"]["name"] for h in hist if "stateEnteredEventDetails" in h]
        audit = q(f"""SELECT principal, principal_role, action FROM setuhaul.booking_events WHERE shipment_id = '{r['shipment']}'
                      ORDER BY event_id""")
        appr = q(f"""SELECT status, decided_by, decided_at IS NOT NULL, kind FROM setuhaul.approval_requests
                     WHERE approval_id = '{r['approval_id']}'""")[0]
        appt = q(f"""SELECT a.status, a.is_current, o.active FROM setuhaul.appointments a
                     LEFT JOIN setuhaul.dock_occupancy o ON o.occupancy_id = a.occupancy_id
                     WHERE a.appointment_id = '{r['appointment']}'""")[0]
        decided_events = q(f"""SELECT count(*) FROM setuhaul.outbox WHERE event_type = 'ApprovalDecided'
                               AND aggregate_id = '{r['approval_id']}'""")[0][0]
        callbacks = q(f"""SELECT outcome FROM setuhaul.approval_callbacks WHERE approval_id = '{r['approval_id']}'""")
        notes_ = q(f"""SELECT template_id, status, left(coalesce(provider_message_id,''), 9) FROM setuhaul.notifications
                       WHERE shipment_id = '{r['shipment']}' ORDER BY created_at""")
        paths[p] = {"execution": {"status": ex["status"], "states": states,
                                  "executions_named_for_this_request": 1},
                    "approval": {"status": appr[0], "decided_by": appr[1], "decided": appr[2], "kind": appr[3],
                                 "decided_role": next((x[1] for x in audit if appr[1] and x[0] == appr[1]), None)},
                    "appointment": {"status": appt[0], "is_current": appt[1], "claim_active": appt[2]},
                    "audit": [list(x) for x in audit], "approval_decided_events": decided_events,
                    "callbacks": [x[0] for x in callbacks], "notifications": [list(x) for x in notes_]}
    ev["paths"] = paths
    after_exec = len(sfn.list_executions(stateMachineArn=machine, maxResults=100)["executions"])
    ev["after"] = {"executions": after_exec, "non_synthetic_rows": [list(x) for x in owner.query(VIOLATIONS)],
                   "booking_switch": {f["FunctionName"]: f.get("Environment", {}).get("Variables", {}).get("SETUHAUL_BOOKING_ENABLED")
                                      for f in lam.list_functions()["Functions"] if f["FunctionName"].startswith("setuhaul-pilot")}}
    P = paths
    checks = {
        "no automatic approval: all three requests waited, OPEN with a task token": all(
            v["token_recorded"] and v["status"] == "OPEN" for v in ev["waiting"].values()) and all(
            r["submit_status"] == "PENDING" for r in reqs.values()),
        "request: one execution per request, started by the deployed relay; a second relay run started none":
            r1["result"]["consumed"] >= 3 and r2["result"]["consumed"] == 0 and after_exec - before["executions"] == 3,
        "approve: planner decision -> approval APPROVED, appointment CONFIRMED, claim kept, execution succeeded":
            P["APPROVE"]["approval"]["status"] == "APPROVED" and P["APPROVE"]["approval"]["decided_role"] == "planner"
            and P["APPROVE"]["appointment"]["status"] == "CONFIRMED" and P["APPROVE"]["appointment"]["claim_active"] is True
            and P["APPROVE"]["execution"]["status"] == "SUCCEEDED" and "ExpireApproval" not in P["APPROVE"]["execution"]["states"],
        "reject: planner decision -> approval REJECTED, appointment REJECTED, claim released, execution succeeded":
            P["REJECT"]["approval"]["status"] == "REJECTED" and P["REJECT"]["approval"]["decided_role"] == "planner"
            and P["REJECT"]["appointment"]["status"] == "REJECTED" and P["REJECT"]["appointment"]["claim_active"] is False
            and P["REJECT"]["execution"]["status"] == "SUCCEEDED" and "ExpireApproval" not in P["REJECT"]["execution"]["states"],
        "expiry: no decision -> the state machine's timeout ran ExpireApproval, request EXPIRED, claim released":
            P["EXPIRE"]["approval"]["status"] == "EXPIRED" and "ExpireApproval" in P["EXPIRE"]["execution"]["states"]
            and P["EXPIRE"]["appointment"]["claim_active"] is False and P["EXPIRE"]["execution"]["status"] == "SUCCEEDED",
        "refused: agent role, another facility's planner, no staff identity, and a second decision":
            all(v["refused"] for v in ev["refusals_before_decision"].values()) and ev["decisions"]["second_decision_new_key"]["refused"],
        "idempotent: the same decision key twice returns the same decision": d1_again == d1,
        "no duplicate business action: one decided event per decided request, at most one callback each":
            all(P[p]["approval_decided_events"] == 1 for p in ("APPROVE", "REJECT")) and all(len(P[p]["callbacks"]) <= 1 for p in P),
        "driver notices only to the internal test sink (no WhatsApp)": all(
            n[2] in ("", "testsink.") for p in P for n in P[p]["notifications"]),
        "booking switch still false on every function; all data synthetic": not ev["after"]["non_synthetic_rows"]
            and all(v in ("false", None) for v in ev["after"]["booking_switch"].values()),
    }
    report = {"collected_utc": now(), "evidence": ev, "checks": checks, "all_pass": all(checks.values()), "log": LOG}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k, flush=True)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
