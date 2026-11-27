"""Historical NO_SHOW backfill (27 Nov 2026 fix plan, owner decision Q1; migration 0048). Owner-run, after 0048 is applied.

The automatic no-show job (every minute) only handles appointments whose deadline (start + the facility's
no_show_grace_min) falls AFTER migration 0048 was applied. Older CONFIRMED appointments are history: this script
handles them, in two committed, audited steps.

    # 1. the dry run: records a report (no_show_backfill_runs + booking_events), changes nothing
    python scripts/no_show_backfill.py dry-run --reason "27 Nov backlog review"

    # 2. after reviewing that report: apply exactly that set (refused if the eligible set changed since)
    python scripts/no_show_backfill.py apply --run <run_id of the dry run> --reason "27 Nov backlog, reviewed"

    # status: the recorded runs
    python scripts/no_show_backfill.py runs

Eligible: a current CONFIRMED appointment past its deadline with no gate arrival, no dock activity, no completion and
no cancellation (open or approved), whose shipment is not at a gate, in a dock, completed or cancelled, and with no
open time-change request. Everything else is listed as excluded, with the reason. Applying twice transitions nothing
the second time. A backfilled appointment's driver is not messaged (the slot passed before the lifecycle existed);
the dispatcher sees it in the Dispatch tab's No-show filter.

Pilot: the database owner's secret through the RDS Data API (stack outputs ClusterArn and OwnerSecretArn; the owner's
own AWS sign-in). Local: --dsn "host=localhost port=55432 user=postgres dbname=..." (the system role).
Nothing secret is printed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
FACILITIES = ("FAC-JAI-01", "FAC-GGN-01")


def lit(s: str | None) -> str:
    return "NULL" if s is None else "'" + str(s).replace("'", "''") + "'"


def call_sql(dry_run: bool, principal: str, reason: str, run: str | None) -> str:
    if not re.fullmatch(r"(owner|staff|system):[A-Za-z0-9_.@-]{1,60}", principal):
        raise ValueError("principal must look like owner:<name>")
    if len(reason.strip()) < 5:
        raise ValueError("a reason of at least 5 characters is required")
    if run is not None and not re.fullmatch(r"[0-9a-f-]{36}", run):
        raise ValueError("--run must be the run id (uuid) of the reviewed dry run")
    return (f"SELECT backfill_no_shows({'true' if dry_run else 'false'}, {lit(principal)}, {lit(reason)}, "
            f"{lit(run)}::uuid)::text")


def summary(res: dict) -> str:
    lines = [f"run {res['run_id']}  dry_run={res['dry_run']}  cutover={res['cutover_at']}",
             f"eligible: {len(res['eligible'])}   excluded: {len(res['excluded'])}   transitioned: {res['transitioned']}"]
    for x in res["eligible"]:
        lines.append(f"  ELIGIBLE  {x['appointment_id']}  {x['shipment_id']}  {x['facility_id']}  start {x['start']}  "
                     f"deadline {x['deadline']} (grace {x['grace_min']} min)")
    for x in res["excluded"]:
        lines.append(f"  excluded  {x['appointment_id']}  {x['shipment_id']}  {x['reason']}")
    return "\n".join(lines)


def run_local(dsn: str, sql: str):
    from setuhaul.api.db import Db
    with Db(dsn).tx("system", FACILITIES) as c:
        return c.execute(sql).fetchone()[0]


def run_pilot(sql: str):
    """One committed Data API transaction as the owner, acting as the system role (the function's grant)."""
    import boto3
    from setuhaul.aws.data_api import Connection, begin_with_resume
    from setuhaul.aws.migrate_data_api import join_group_roles
    out = {o["OutputKey"]: o["OutputValue"] for o in boto3.client("cloudformation", region_name=REGION)
           .describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    client = boto3.client("rds-data", region_name=REGION)
    kw = {"resourceArn": out["ClusterArn"], "secretArn": out["OwnerSecretArn"]}
    tid = begin_with_resume(client, {"database": "setuhaul", **kw}, 120)
    try:
        conn = Connection(client, out["ClusterArn"], out["OwnerSecretArn"], "setuhaul", tid)
        if not conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
            join_group_roles(conn)
        conn.execute("SET LOCAL ROLE setuhaul_system")
        conn.execute("SET LOCAL search_path = setuhaul")
        conn.execute(f"SELECT set_config('app.facility_scope', {lit(','.join(FACILITIES))}, true)")
        row = conn.execute(sql).fetchone()[0]
        client.commit_transaction(transactionId=tid, **kw)
        return row
    except Exception:
        client.rollback_transaction(transactionId=tid, **kw)
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("dry-run", "apply", "runs"))
    ap.add_argument("--reason", default="")
    ap.add_argument("--run", default=None, help="apply: the run id of the reviewed dry run")
    ap.add_argument("--principal", default="owner:no-show-backfill")
    ap.add_argument("--dsn", default=None, help="a local database instead of the pilot")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.command == "runs":
        sql = ("SELECT coalesce(jsonb_agg(jsonb_build_object('run_id', run_id, 'dry_run', dry_run, 'principal', principal, "
               "'started_at', started_at, 'eligible', jsonb_array_length(eligible), 'excluded', jsonb_array_length(excluded), "
               "'transitioned', transitioned) ORDER BY started_at), '[]')::text FROM no_show_backfill_runs")
    else:
        if a.command == "apply" and not a.run:
            ap.error("apply needs --run <the dry run's id>: review the dry-run report first")
        sql = call_sql(a.command == "dry-run", a.principal, a.reason, a.run if a.command == "apply" else None)
    raw = run_local(a.dsn, sql) if a.dsn else run_pilot(sql)
    res = json.loads(raw) if isinstance(raw, str) else raw
    if a.json or a.command == "runs":
        print(json.dumps(res, indent=2, default=str))
    else:
        print(summary(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
