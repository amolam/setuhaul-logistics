"""Redrive the approval executions that failed on the missing-ARN defect (23 Nov 2026), after the fix is deployed.

    python scripts/redrive_failed_executions.py [--out docs/release/evidence/redrive_20261123.json]

Step Functions RedriveExecution resumes a FAILED standard execution from the state that failed: here ExpireApproval.
  - APPROVE / REJECT (decided): expire_approval returns the request's status and changes nothing;
  - EXPIRE (OPEN, deadline passed): expire_approval marks it EXPIRED and releases its claim (the designed path).
No row is edited by hand. Afterwards the outbound function is invoked once so any driver notice goes to the internal
test sink (never WhatsApp). Account ids are masked.
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
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "redrive_20261123.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    owner = OwnerSession(c("rds-data"), out["ClusterArn"], out["OwnerSecretArn"])
    sc = ("SELECT set_config('app.facility_scope','FAC-JAI-01,FAC-GGN-01',true)",)
    q = lambda s: [list(r) for r in owner.query(s, sc)]  # noqa: E731
    sfn, lam = c("stepfunctions"), c("lambda")
    exe = lambda i: out["ApprovalAliasArn"].rsplit(":", 1)[0].replace(":stateMachine:", ":execution:") + ":" + i  # noqa: E731
    ids = ",".join(f"'{v}'" for v in IDS.values())

    def snap():
        return {"requests": q(f"""SELECT r.approval_id::text, r.status, r.decided_by, r.decided_at::text, a.status, o.active
                                  FROM setuhaul.approval_requests r JOIN setuhaul.appointments a USING (appointment_id)
                                  LEFT JOIN setuhaul.dock_occupancy o ON o.occupancy_id = a.occupancy_id
                                  WHERE r.approval_id IN ({ids}) ORDER BY 1"""),
                "audit": q(f"""SELECT e.shipment_id, e.principal, e.principal_role, e.action FROM setuhaul.booking_events e
                               WHERE e.shipment_id IN (SELECT shipment_id FROM setuhaul.approval_requests WHERE approval_id IN ({ids}))
                               AND e.action LIKE 'APPROVAL_%' ORDER BY e.event_id"""),
                "notifications": q(f"""SELECT shipment_id, template_id, status, left(coalesce(provider_message_id,''), 9)
                                       FROM setuhaul.notifications WHERE shipment_id IN (SELECT shipment_id FROM
                                       setuhaul.approval_requests WHERE approval_id IN ({ids})) ORDER BY created_at""")}

    before = snap()
    status_before = {p: sfn.describe_execution(executionArn=exe(i))["status"] for p, i in IDS.items()}
    redriven = {}
    for p, i in IDS.items():
        if status_before[p] == "FAILED":
            sfn.redrive_execution(executionArn=exe(i))
            redriven[p] = True
    end = time.time() + 300
    while time.time() < end:
        st = {p: sfn.describe_execution(executionArn=exe(i))["status"] for p, i in IDS.items()}
        if all(v != "RUNNING" for v in st.values()):
            break
        time.sleep(5)
    outbound = json.loads(lam.invoke(FunctionName="setuhaul-pilot-outbound", Payload=b"{}")["Payload"].read() or b"null")
    after = snap()
    redrive_count = {p: sfn.describe_execution(executionArn=exe(i)).get("redriveCount") for p, i in IDS.items()}
    b, a2 = {r[0]: r for r in before["requests"]}, {r[0]: r for r in after["requests"]}
    checks = {
        "all three executions were FAILED and were redriven once": all(v == "FAILED" for v in status_before.values())
            and all(v == 1 for v in redrive_count.values()),
        "all three executions SUCCEEDED after the redrive": all(v == "SUCCEEDED" for v in st.values()),
        "decided requests unchanged (status, decided_by, decided_at, appointment, claim)": all(
            a2[IDS[p]][1:] == b[IDS[p]][1:] for p in ("APPROVE", "REJECT")),
        "the overdue request EXPIRED through expire_approval, its claim released": a2[IDS["EXPIRE"]][1] == "EXPIRED"
            and a2[IDS["EXPIRE"]][4] == "EXPIRED" and a2[IDS["EXPIRE"]][5] is False,
        "one decision record per request in the audit trail (no duplicates)": all(
            sum(1 for e in after["audit"] if e[0] == s) == 1 for s in {e[0] for e in after["audit"]}) and len(after["audit"]) == 3,
        "driver notices only to the internal test sink": all(n[3] in ("", "testsink.") for n in after["notifications"]),
    }
    report = {"collected_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status_before": status_before,
              "status_after": st, "redrive_count": redrive_count, "outbound": outbound, "before": before, "after": after,
              "checks": checks, "all_pass": all(checks.values())}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k, flush=True)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
