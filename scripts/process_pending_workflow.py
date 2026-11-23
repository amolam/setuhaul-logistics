"""After the relay IAM fix: let the deployed relay process the workflow events that drill run 1 left pending, and prove
it did so safely. Owner-approved 23 Nov 2026 (condition 6). Nothing is cleaned up by hand.

    python scripts/process_pending_workflow.py [--out docs/release/evidence/pending_workflow_processed.json]

Run 1 (drill_approval_run1.json) left three synthetic requests whose executions never started:
  APPROVE  e0a3c0bb...  decided APPROVED   -> the execution must start, find the request closed, answer its own late token
  REJECT   ec99648f...  decided REJECTED   -> the same
  EXPIRE   2c6b6e0f...  OPEN past deadline -> the execution must start with a deadline already passed and expire it
plus two ApprovalDecided outbox events waiting for the relay. The relay is invoked (the schedule stays as it is), then
invoked again to show a second run does nothing. Decided rows must not change; no request may be decided twice.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
MASK = re.compile(r"\b\d{12}\b")
IDS = {"APPROVE": "e0a3c0bb-7d17-4181-9336-a64f0eb1f40f", "REJECT": "ec99648f-e0ad-4c32-b0ee-02bfde2d8e8a",
       "EXPIRE": "2c6b6e0f-a222-42c3-be5e-2099d7e87547"}


def c(n):
    return boto3.client(n, region_name=REGION)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "pending_workflow_processed.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    owner = OwnerSession(c("rds-data"), out["ClusterArn"], out["OwnerSecretArn"])
    sc = ("SELECT set_config('app.facility_scope','FAC-JAI-01,FAC-GGN-01',true)",)
    q = lambda s: [list(r) for r in owner.query(s, sc)]  # noqa: E731
    lam, sfn = c("lambda"), c("stepfunctions")
    machine = out["ApprovalAliasArn"].rsplit(":", 1)[0]
    exe = lambda i: machine.replace(":stateMachine:", ":execution:") + ":" + i  # noqa: E731
    ids = ",".join(f"'{v}'" for v in IDS.values())

    def snapshot():
        return {"requests": q(f"""SELECT approval_id::text, status, decided_by, decided_at::text, task_token IS NOT NULL
                                  FROM setuhaul.approval_requests WHERE approval_id IN ({ids}) ORDER BY 1"""),
                "appointments": q(f"""SELECT a.appointment_id, a.status, o.active FROM setuhaul.approval_requests r
                                      JOIN setuhaul.appointments a USING (appointment_id)
                                      LEFT JOIN setuhaul.dock_occupancy o ON o.occupancy_id = a.occupancy_id
                                      WHERE r.approval_id IN ({ids}) ORDER BY 1"""),
                "outbox": q(f"""SELECT aggregate_id, event_type, status FROM setuhaul.outbox WHERE aggregate_id IN ({ids})
                               ORDER BY 1, 2"""),
                "consumptions": q(f"""SELECT o.event_id, o.event_type, count(c.consumer), string_agg(c.consumer, ',')
                                      FROM setuhaul.outbox o LEFT JOIN setuhaul.outbox_consumptions c USING (event_id)
                                      WHERE o.aggregate_id IN ({ids}) GROUP BY 1, 2 ORDER BY 1"""),
                "callbacks": q(f"""SELECT approval_id::text, outcome, attempts FROM setuhaul.approval_callbacks
                                   WHERE approval_id IN ({ids}) ORDER BY 1"""),
                "booking_events": q(f"""SELECT shipment_id, principal, principal_role, action FROM setuhaul.booking_events
                                        WHERE shipment_id IN (SELECT shipment_id FROM setuhaul.approval_requests
                                                              WHERE approval_id IN ({ids})) ORDER BY event_id""")}

    before = snapshot()
    inv = lambda n: json.loads(lam.invoke(FunctionName=n, Payload=b"{}")["Payload"].read() or b"null")  # noqa: E731
    r1 = inv("setuhaul-pilot-approval-relay")
    deadline = time.time() + 240
    while time.time() < deadline:
        st = {}
        for p, i in IDS.items():
            try:
                st[p] = sfn.describe_execution(executionArn=exe(i))["status"]
            except sfn.exceptions.ExecutionDoesNotExist:
                st[p] = "NOT_STARTED"
        if all(v in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED") for v in st.values()):
            break
        time.sleep(5)
    r2 = inv("setuhaul-pilot-approval-relay")
    after = snapshot()
    hist = {}
    for p, i in IDS.items():
        h = sfn.get_execution_history(executionArn=exe(i), maxResults=100)["events"]
        hist[p] = [e["stateEnteredEventDetails"]["name"] for e in h if "stateEnteredEventDetails" in e]
    executions = len(sfn.list_executions(stateMachineArn=machine, maxResults=100)["executions"])
    req = {r[0]: r for r in after["requests"]}
    breq = {r[0]: r for r in before["requests"]}
    checks = {
        "the relay now starts executions (no AccessDenied)": isinstance(r1, dict) and "errorMessage" not in r1,
        "one execution per pending request, all SUCCEEDED": all(v == "SUCCEEDED" for v in st.values()) and executions == 3,
        "decided requests unchanged (status, decided_by, decided_at)": all(
            req[IDS[p]][1:4] == breq[IDS[p]][1:4] for p in ("APPROVE", "REJECT")),
        "decided executions did not take the expiry path": all("ExpireApproval" not in hist[p] for p in ("APPROVE", "REJECT")),
        "the overdue OPEN request expired through the workflow timeout": req[IDS["EXPIRE"]][1] == "EXPIRED"
            and "ExpireApproval" in hist["EXPIRE"],
        "the second relay run did nothing": isinstance(r2, dict) and r2.get("consumed") == 0,
        "each ApprovalDecided event consumed, once per consumer": all(
            n >= 1 and len(cs.split(",")) == len(set(cs.split(","))) for ev_id, et, n, cs in after["consumptions"]
            if et == "ApprovalDecided" and cs) and all(n >= 1 for ev_id, et, n, cs in after["consumptions"] if et == "ApprovalDecided"),
        "no request decided twice (one decision event each in the audit trail)": all(
            sum(1 for e in after["booking_events"] if e[3].startswith("APPROVAL_") and e[3] != "APPROVAL_EXPIRED"
                and e[0] == s) <= 1 for s in {e[0] for e in after["booking_events"]}),
    }
    report = {"collected_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "relay_runs": [r1, r2],
              "execution_status": st, "execution_states": hist, "executions_total": executions,
              "before": before, "after": after, "checks": checks, "all_pass": all(checks.values())}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k, flush=True)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
