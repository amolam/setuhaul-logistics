"""The owner's booking kill switch (Option D; migration 0026). No deployment is involved.

    python -m setuhaul.ops.kill_switch status
    python -m setuhaul.ops.kill_switch engage    --reason "why"
    python -m setuhaul.ops.kill_switch disengage --reason "why"

While ENGAGED the database refuses every new booking commitment (a hold or appointment claim on dock time, moving
one, a new appointment, confirming one), whatever the application or its deployment says. Releasing, rejecting,
expiring and cancelling still work. An ops_lead can do the same from the console (POST /console/booking-kill-switch).
This path uses the database owner's secret (the stack output OwnerSecretArn) and records an audit row in
booking_events with principal `owner:kill-switch-cli`, in the same transaction as the change.
"""
from __future__ import annotations

import argparse
import json
import sys

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
PRINCIPAL = "owner:kill-switch-cli"


def lit(s: str) -> str:
    """A SQL string literal (standard_conforming_strings is on for the owner session's statements)."""
    return "'" + s.replace("'", "''") + "'"


def statements(engaged: bool, reason: str) -> list[str]:
    if len(reason.strip()) < 5:
        raise ValueError("a reason of at least 5 characters is required")
    action = "BOOKING_KILL_ENGAGED" if engaged else "BOOKING_KILL_DISENGAGED"
    flag = "true" if engaged else "false"
    return [
        "SET LOCAL standard_conforming_strings = on",
        f"""INSERT INTO setuhaul.booking_events (principal, principal_role, action, reason, before_state, after_state)
            SELECT {lit(PRINCIPAL)}, 'owner', {lit(action)}, {lit(reason)},
                   jsonb_build_object('engaged', engaged), jsonb_build_object('engaged', {flag})
              FROM setuhaul.booking_kill_switch WHERE id = 1""",
        f"""UPDATE setuhaul.booking_kill_switch SET engaged = {flag}, changed_at = now(), changed_by = {lit(PRINCIPAL)},
                   reason = {lit(reason)} WHERE id = 1""",
    ]


def status(session) -> dict:
    row = session.query("SELECT engaged, changed_at::text, changed_by, reason FROM setuhaul.booking_kill_switch")[0]
    return {"engaged": bool(row[0]), "changed_at": row[1], "changed_by": row[2], "reason": row[3]}


def session_from_stack():
    import boto3
    from setuhaul.aws.migrate_data_api import OwnerSession
    out = {o["OutputKey"]: o["OutputValue"] for o in boto3.client("cloudformation", region_name=REGION)
           .describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    return OwnerSession(boto3.client("rds-data", region_name=REGION), out["ClusterArn"], out["OwnerSecretArn"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("status", "engage", "disengage"))
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    s = session_from_stack()
    if a.command != "status":
        s.run(statements(a.command == "engage", a.reason))
    print(json.dumps(status(s), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
