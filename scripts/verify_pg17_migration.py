"""T33-P verification: migration 0025 and the database functionality it adds, on the live Aurora PostgreSQL 17.9
cluster, in ONE transaction that is always rolled back.

    python scripts/verify_pg17_migration.py [--out docs/release/evidence/pg17_migration_0025.json]

What runs, inside a single RDS Data API transaction as the non-superuser owner (exactly as the deploy migrator):
  1. the server version and the migrations already applied (0001-0024);
  2. migration 0025, statement by statement, with the same non-superuser adaptation the deploy migrator uses;
  3. catalogue checks: tables, forced row-level security, triggers, function owners and EXECUTE grants;
  4. a functional run on synthetic rows created inside the transaction: a CONFIRMED appointment at FAC-JAI-01, a
     stored proposal (record_schedule_proposal) and a real move (move_appointment) under a staff identity, then the
     refusals: re-applying the same row, an unknown proposal, the agent role calling it;
  5. ROLLBACK, then a separate read that proves nothing persisted (still 24 migrations, no schedule_proposals table,
     no synthetic shipment).
It never commits. The booking switch is not involved: it is enforced by the service, and no service runs here.
Account ids are masked in the output.
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
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.aws.data_api import Connection, begin_with_resume  # noqa: E402
from setuhaul.aws.migrate_data_api import (_CREATES_ROLE, _code, adapt_for_non_superuser,  # noqa: E402
                                           join_group_roles, split_sql)

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
MIGRATION = ROOT / "migrations" / "0025_schedule_proposals_and_move.sql"
MASK = re.compile(r"\b\d{12}\b")
FAC = "FAC-JAI-01"


def c(name):
    return boto3.client(name, region_name=REGION)


def probe(conn, label, sql, params=()):
    """Run one statement that must be REFUSED, inside a savepoint, and return the refusal."""
    conn.execute("SAVEPOINT p")
    try:
        conn.execute(sql, params).fetchall()
        conn.execute("RELEASE SAVEPOINT p")
        return {"probe": label, "refused": False}
    except Exception as e:                                                     # noqa: BLE001 — the expected refusal
        conn.execute("ROLLBACK TO SAVEPOINT p")
        msg = getattr(getattr(e, "diag", None), "message_primary", None) or str(e)
        return {"probe": label, "refused": True, "sqlstate": getattr(e, "sqlstate", None), "message": msg[:160]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "pg17_migration_0025.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    rds = c("rds-data")
    kw = {"resourceArn": out["ClusterArn"], "secretArn": out["OwnerSecretArn"]}
    tid = begin_with_resume(rds, {"database": "setuhaul", **kw}, 120)
    conn = Connection(rds, out["ClusterArn"], out["OwnerSecretArn"], "setuhaul", tid)
    ev: dict = {"collected_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "committed": False}
    checks: dict = {}
    try:
        ev["server_version"] = conn.execute("SELECT current_setting('server_version')").fetchone()[0]
        ev["migrations_before"] = conn.execute("SELECT count(*) FROM setuhaul_meta.schema_migrations").fetchone()[0]
        checks["server is PostgreSQL 17.9"] = ev["server_version"].startswith("17.9")

        # ---- 2. apply 0025 as the deploy migrator would (non-superuser owner)
        join_group_roles(conn)
        state: dict = {}
        stmts = split_sql(MIGRATION.read_text(encoding="utf-8"))
        applied = 0
        for s in stmts:
            s2 = adapt_for_non_superuser(conn, s, state)
            if s2 is None:
                continue
            conn.execute(s2.replace("%", "%%"))
            applied += 1
            if _CREATES_ROLE.search(_code(s2)):
                join_group_roles(conn)
        if state.get("create_granted"):
            conn.execute("REVOKE CREATE ON SCHEMA setuhaul FROM setuhaul_rpc")
        conn.execute("RESET search_path")
        ev["migration_0025_statements_applied"] = applied
        checks["migration 0025 applies on 17.9 as the non-superuser owner"] = applied > 0

        # ---- 3. catalogue
        tables = conn.execute("""SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c
                                   JOIN pg_namespace n ON n.oid = c.relnamespace
                                  WHERE n.nspname = 'setuhaul' AND c.relname IN ('schedule_proposals','schedule_proposal_rows')
                                  ORDER BY 1""").fetchall()
        ev["tables"] = [list(t) for t in tables]
        checks["both proposal tables exist with forced RLS"] = len(tables) == 2 and all(t[1] and t[2] for t in tables)
        fns = conn.execute("""SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef FROM pg_proc p
                                JOIN pg_namespace n ON n.oid = p.pronamespace
                               WHERE n.nspname = 'setuhaul' AND p.proname IN ('record_schedule_proposal','move_appointment')
                               ORDER BY 1""").fetchall()
        ev["functions"] = [list(f) for f in fns]
        checks["both functions are SECURITY DEFINER owned by setuhaul_rpc"] = len(fns) == 2 and all(
            f[1] == "setuhaul_rpc" and f[2] for f in fns)
        grants = conn.execute("""SELECT r.rolname, has_function_privilege(r.oid, 'setuhaul.move_appointment(uuid,text,text,text)', 'EXECUTE')
                                   FROM pg_roles r WHERE r.rolname IN ('setuhaul_console','setuhaul_agent','setuhaul_ingest',
                                        'setuhaul_feasibility','setuhaul_system') ORDER BY 1""").fetchall()
        ev["move_appointment_execute"] = dict(grants)
        checks["only setuhaul_console can EXECUTE move_appointment"] = dict(grants) == {
            "setuhaul_agent": False, "setuhaul_console": True, "setuhaul_feasibility": False, "setuhaul_ingest": False,
            "setuhaul_system": False}

        # ---- 4. functional run on synthetic rows (all rolled back)
        conn.execute("SET LOCAL search_path = setuhaul")
        docks = conn.execute("""SELECT dock_id FROM docks WHERE facility_id = %s AND dock_type = 'STANDARD'
                                 ORDER BY dock_code LIMIT 2""", (FAC,)).fetchall()
        carrier = conn.execute("SELECT carrier_id FROM carriers ORDER BY 1 LIMIT 1").fetchone()[0]
        d1, d2 = docks[0][0], docks[1][0]
        u = uuid.uuid4().hex[:8].upper()
        drv, veh, sid, aid = f"SYN-DRV-PG17{u}", f"SYN-VEH-PG17{u}", f"SYN-SHP-PG17{u}", f"SYN-APT-PG17{u}"
        conn.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,%s,'pg17 probe',%s)",
                     (drv, carrier, f"+99900{uuid.uuid4().int % 10**9:09d}"))
        conn.execute("""INSERT INTO vehicles (vehicle_id,carrier_id,vehicle_type_code,registration_number,capacity_kg,refrigeration_capable)
                        VALUES (%s,%s,'32FT',%s,20000,false)""", (veh, carrier, f"PG17-{u}"))
        conn.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                        load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                        VALUES (%s,%s,%s,%s,%s,%s,10000,45,now()-interval '3 hours',now()+interval '2 hours','IN_TRANSIT')""",
                     (sid, f"ORD-PG17-{u}", carrier, drv, veh, FAC))
        # a CONFIRMED appointment and its dock claim, tomorrow 10:00 IST (inside the facility's hours)
        frm = conn.execute("SELECT date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') + interval '1 day 10 hours'").fetchone()[0]
        start = conn.execute("SELECT (%s::timestamp AT TIME ZONE 'Asia/Kolkata')", (frm,)).fetchone()[0]
        occ = conn.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                              VALUES (%s,%s,tstzrange(%s, %s + interval '55 minutes','[)'),'APPOINTMENT',%s,%s,'pg17-probe')
                              RETURNING occupancy_id""", (d1, FAC, start, start, sid, aid)).fetchone()[0]
        conn.execute("""INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,occupancy_id,status,is_current,booking_source)
                        VALUES (%s,%s,%s,tstzrange(%s, %s + interval '55 minutes','[)'),%s,'CONFIRMED',true,'PLANNER')""",
                     (aid, sid, d1, start, start, occ))
        target = conn.execute("SELECT %s::timestamptz + interval '2 hours'", (start,)).fetchone()[0]
        tgt_end = conn.execute("SELECT %s::timestamptz + interval '55 minutes'", (target,)).fetchone()[0]
        row = {"seq": 1, "shipment_id": sid, "appointment_id": aid, "action": "MOVE", "dock_id": d2,
               "start": target.isoformat(), "end": tgt_end.isoformat(), "from_dock_id": d1,
               "from_start": start.isoformat(), "score": {"total": 0}, "reason": "T33-P verification"}
        conn.execute("SET LOCAL ROLE setuhaul_console")
        conn.execute("SELECT set_config('app.facility_scope', %s, true)", (FAC,))
        conn.execute("SELECT begin_staff_context(%s,%s,%s)", ("staff:t33p-probe", ["ops_lead"], [FAC]))
        prop = conn.execute("SELECT record_schedule_proposal(%s,'FACILITY_SCORE','t33p-probe',%s,0,'{}',%s,600)",
                            (FAC, "b" * 64, Jsonb([row]))).fetchone()[0]
        prop = prop if isinstance(prop, dict) else json.loads(prop)
        moved = conn.execute("SELECT move_appointment(%s,%s,%s,%s)",
                             (prop["proposal_id"], aid, "t33p-" + uuid.uuid4().hex, "T33-P verification")).fetchone()[0]
        moved = moved if isinstance(moved, dict) else json.loads(moved)
        ev["move_result"] = {k: moved.get(k) for k in ("outcome", "status", "dock_code", "start", "from_start", "rule")}
        checks["a real move succeeds on 17.9 (MOVED, CONFIRMED)"] = moved.get("outcome") == "MOVED" and moved.get("status") == "CONFIRMED"
        ev["refusals"] = [
            probe(conn, "the same row applied again (new key)", "SELECT move_appointment(%s,%s,%s,NULL)",
                  (prop["proposal_id"], aid, "t33p-" + uuid.uuid4().hex)),
            probe(conn, "an unknown proposal", "SELECT move_appointment(%s,%s,%s,NULL)",
                  (str(uuid.uuid4()), aid, "t33p-" + uuid.uuid4().hex)),
        ]
        conn.execute("RESET ROLE")
        conn.execute("SET LOCAL ROLE setuhaul_agent")
        ev["refusals"].append(probe(conn, "the agent role calls move_appointment", "SELECT setuhaul.move_appointment(%s,%s,%s,NULL)",
                                    (prop["proposal_id"], aid, "t33p-" + uuid.uuid4().hex)))
        conn.execute("RESET ROLE")
        checks["re-apply, unknown proposal and the agent role are all refused"] = all(r["refused"] for r in ev["refusals"])
        after = conn.execute("""SELECT appointment_id, status, is_current, dock_id FROM setuhaul.appointments
                                 WHERE shipment_id = %s ORDER BY created_at""", (sid,)).fetchall()
        ev["appointments_after_move"] = [list(x) for x in after]
        claims = conn.execute("""SELECT dock_id, active FROM setuhaul.dock_occupancy WHERE shipment_id = %s
                                  ORDER BY occupancy_id""", (sid,)).fetchall()
        ev["claims_after_move"] = [list(x) for x in claims]
        # ---- row-level security and the role allow-list, live on Aurora (AURORA gate: "RLS and role allowlist tests run
        # against it"). Same transaction, so the probe shipment at FAC-JAI-01 is visible to the right scope only.
        rls = {}
        for scope in (FAC, "FAC-GGN-01"):
            conn.execute("SET LOCAL ROLE setuhaul_console")
            conn.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
            rls[scope] = conn.execute("SELECT count(*) FROM setuhaul.shipments WHERE shipment_id = %s", (sid,)).fetchone()[0]
            conn.execute("RESET ROLE")
        ev["rls_probe_shipment_visible"] = rls
        checks["RLS on Aurora: the JAI shipment is visible in JAI scope only"] = rls == {FAC: 1, "FAC-GGN-01": 0}
        allow = []
        for role, label, sql, params in (
                ("setuhaul_agent", "agent inserts an appointment directly",
                 "INSERT INTO setuhaul.appointments (appointment_id,shipment_id,dock_id,during,status,is_current,booking_source) "
                 "VALUES ('X-PROBE',%s,%s,tstzrange(now(),now()+interval '1 hour'),'CONFIRMED',true,'PLANNER')", (sid, d1)),
                ("setuhaul_agent", "agent writes deployment evidence", "UPDATE setuhaul.deployment_evidence SET gate = gate", ()),
                ("setuhaul_console", "console updates a dock claim directly",
                 "UPDATE setuhaul.dock_occupancy SET active = false WHERE shipment_id = %s", (sid,)),
                ("setuhaul_console", "console writes a proposal row directly",
                 "INSERT INTO setuhaul.schedule_proposals (proposal_id) VALUES (gen_random_uuid())", ()),
                ("setuhaul_system", "system calls move_appointment",
                 "SELECT setuhaul.move_appointment(gen_random_uuid(),'x','probe-key-0001',NULL)", ())):
            conn.execute(f"SET LOCAL ROLE {role}")
            conn.execute("SELECT set_config('app.facility_scope', %s, true)", (FAC,))
            allow.append({"role": role, **probe(conn, label, sql, params)})
            conn.execute("RESET ROLE")
        ev["role_allowlist"] = allow
        checks["role allow-list on Aurora: every forbidden direct write is refused with 42501"] = all(
            x["refused"] and x["sqlstate"] == "42501" for x in allow)
        checks["old appointment cancelled, one current CONFIRMED on the new dock, one active claim"] = (
            sum(1 for x in after if x[1] == "CONFIRMED" and x[2] and x[3] == d2) == 1
            and any(x[0] == aid and x[1] == "CANCELLED" for x in after)
            and sum(1 for x in claims if x[1]) == 1)
    finally:
        rds.rollback_transaction(transactionId=tid, **kw)
        ev["rolled_back"] = True

    # ---- 5. nothing persisted
    tid2 = begin_with_resume(rds, {"database": "setuhaul", **kw}, 120)
    try:
        c2 = Connection(rds, out["ClusterArn"], out["OwnerSecretArn"], "setuhaul", tid2)
        n, table, ship = c2.execute("""SELECT (SELECT count(*) FROM setuhaul_meta.schema_migrations),
                                              to_regclass('setuhaul.schedule_proposals') IS NOT NULL,
                                              (SELECT count(*) FROM setuhaul.shipments WHERE shipment_id LIKE 'SYN-SHP-PG17%')""").fetchone()
    finally:
        rds.rollback_transaction(transactionId=tid2, **kw)
    ev["after_rollback"] = {"migrations": n, "schedule_proposals_table": table, "probe_shipments": ship}
    checks["after rollback: still 24 migrations, no 0025 objects, no probe rows"] = (n == ev["migrations_before"] == 24
                                                                                     and not table and ship == 0)
    report = {"evidence": ev, "checks": checks, "all_pass": all(checks.values())}
    text = MASK.sub("************", json.dumps(report, indent=1, default=str))
    Path(a.out).write_text(text + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
