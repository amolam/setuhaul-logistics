"""Option D pre-activation check 4: the booking kill switch works on the deployed pilot (migration 0026).

    python scripts/verify_kill_switch.py [--out docs/release/evidence/kill_switch_live.json]

Both operator paths, each engaged and disengaged, audited:
  - the owner (setuhaul/ops/kill_switch.py statements, owner secret, principal owner:kill-switch-cli);
  - an ops_lead through the console database role under a trusted staff identity (set_booking_kill_switch), with a
    planner refused.
While engaged, a booking commitment (a HOLD claim on dock time for a synthetic SYN- shipment, 20 days ahead) is
refused BOOKING_KILLED; while disengaged the same statement gets past the switch. Every attempt runs in a transaction
that ends with a deliberate error, so nothing is ever committed. The switch is left DISENGAGED. Synthetic data only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from drill_approval_workflow import refused  # noqa: E402
from setuhaul.aws.data_api import DataApiDb  # noqa: E402
from setuhaul.ops.kill_switch import session_from_stack, statements, status  # noqa: E402

REGION, STACK, FAC = "ap-southeast-2", "SetuHaulPilot", "FAC-JAI-01"
MASK = re.compile(r"\b\d{12}\b")


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "kill_switch_live.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in boto3.client("cloudformation", region_name=REGION)
           .describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    s = session_from_stack()
    db = DataApiDb(out["ClusterArn"], "setuhaul", {"console": json.loads(out["LoginSecretArns"])["console"]},
                   boto3.client("rds-data", region_name=REGION), resume_wait_s=120)
    scope = f"SELECT set_config('app.facility_scope', '{FAC}', true)"
    sid, dock = s.query(f"""SELECT sh.shipment_id, d.dock_id FROM setuhaul.shipments sh
                              JOIN setuhaul.docks d ON d.facility_id = sh.destination_facility_id
                             WHERE sh.destination_facility_id = {q(FAC)} AND sh.shipment_id LIKE 'SYN-%'
                             ORDER BY sh.shipment_id LIMIT 1""", (scope,))[0]

    def commitment():
        """A booking commitment that can never commit: the transaction ends with a division by zero."""
        s.run(["SET LOCAL search_path = setuhaul", scope,
               f"""INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, shipment_id, ref_id, expires_at, created_by)
                   VALUES ({q(dock)}, {q(FAC)}, tstzrange(now() + interval '20 days', now() + interval '20 days 1 hour', '[)'),
                           'HOLD', {q(sid)}, 'kill-switch-check', now() + interval '20 days', 'kill-switch-check')""",
               "SELECT 1/0"])

    def ops(engaged, roles=("ops_lead",), who="staff:option-d-kill-lead"):
        with db.tx("console", (FAC,), staff=(who, set(roles), (FAC,))) as t:
            return t.execute("SELECT set_booking_kill_switch(%s,%s,%s)",
                             (engaged, who, "Option D pre-activation kill switch check")).fetchone()[0]

    ev, checks = {"synthetic_shipment": sid, "dock": dock, "start": status(s)}, {}
    checks["starts disengaged"] = ev["start"]["engaged"] is False
    # owner path
    s.run(statements(True, "Option D pre-activation check: owner engages"))
    ev["owner_engaged"] = {"status": status(s), "commitment": refused(commitment)}
    s.run(statements(False, "Option D pre-activation check: owner disengages"))
    ev["owner_disengaged"] = {"status": status(s), "commitment": refused(commitment)}
    checks["owner engage: a booking commitment is refused BOOKING_KILLED"] = (
        ev["owner_engaged"]["status"]["engaged"] and "BOOKING_KILLED" in ev["owner_engaged"]["commitment"]["message"])
    checks["owner disengage: the same statement gets past the switch (and is still never committed)"] = (
        not ev["owner_disengaged"]["status"]["engaged"]
        and "BOOKING_KILLED" not in ev["owner_disengaged"]["commitment"]["message"])
    # console path
    ev["planner_refused"] = refused(lambda: ops(True, ("planner",), "staff:option-d-kill-planner"))
    ev["ops_lead_engage"] = ops(True)
    ev["ops_lead_engaged"] = {"status": status(s), "commitment": refused(commitment)}
    ev["ops_lead_disengage"] = ops(False)
    ev["final"] = status(s)
    checks["a planner cannot operate it"] = ev["planner_refused"]["refused"] and "ROLE_NOT_ALLOWED" in ev["planner_refused"]["message"]
    checks["ops_lead engage via the console role: commitment refused BOOKING_KILLED"] = (
        ev["ops_lead_engaged"]["status"]["engaged"] and "BOOKING_KILLED" in ev["ops_lead_engaged"]["commitment"]["message"])
    checks["left disengaged"] = ev["final"]["engaged"] is False
    audit = s.query("""SELECT action, principal, principal_role, at::text FROM setuhaul.booking_events
                        WHERE action LIKE 'BOOKING_KILL_%' ORDER BY event_id DESC LIMIT 4""")
    ev["audit"] = [list(r) for r in audit]
    checks["every change audited (owner x2, ops_lead x2)"] = [(r[1], r[0]) for r in reversed(audit)] == [
        ("owner:kill-switch-cli", "BOOKING_KILL_ENGAGED"), ("owner:kill-switch-cli", "BOOKING_KILL_DISENGAGED"),
        ("staff:option-d-kill-lead", "BOOKING_KILL_ENGAGED"), ("staff:option-d-kill-lead", "BOOKING_KILL_DISENGAGED")]
    leftovers = s.query("SELECT count(*) FROM setuhaul.dock_occupancy WHERE ref_id = 'kill-switch-check'")[0][0]
    checks["nothing was committed"] = leftovers == 0
    report = {"collected_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "evidence": ev,
              "checks": checks, "all_pass": all(checks.values())}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
