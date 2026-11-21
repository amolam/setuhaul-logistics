"""Validate schema.sql on a real PostgreSQL and prove the integrity design with the snapshot.

1. Apply schema.sql to a fresh database.
2. Load the SQLite snapshot. Physical reality first (trucks in dock, dock blocks), then bookings in booking order.
   Every row the constraints REJECT is reported: these should be exactly the data-audit findings.
3. Concurrency proof: N parallel holds on one dock window, many rounds -> exactly one winner each round.
4. Overlap proof: random overlapping windows of different lengths -> zero overlapping active occupancy.
5. Rule proofs: weight/reefer/heavy/wrong-facility/cancelled rejected; booking_events append-only.

Usage:  python phase2_design/validate_schema.py "host=localhost port=55432 user=postgres"
Writes phase2_design/validation_results.md
"""
from __future__ import annotations

import random
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.types.range import Range

HERE = Path(__file__).resolve().parent
SQLITE = HERE.parent / "data" / "setuhaul_freight_operations.db"
SCHEMA = (HERE / "schema.sql").read_text(encoding="utf-8")
OUT = HERE / "validation_results.md"
BASE_DSN = sys.argv[1] if len(sys.argv) > 1 else "host=localhost port=55432 user=postgres"
DBNAME = "setuhaul_validate"
DSN = f"{BASE_DSN} dbname={DBNAME}"
CHANGEOVER = 10
ts = datetime.fromisoformat
report: list[str] = []


def log(line: str = ""):
    print(line)
    report.append(line)


def fresh_db():
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DBNAME}")
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(SCHEMA)
        n = c.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='setuhaul'").fetchone()[0]
    log(f"- Schema applied cleanly on {psycopg.connect(DSN).info.server_version // 10000}.x: **{n} tables**, exclusion constraints, triggers, RLS policies.")


def rng(a: datetime, b: datetime) -> Range:
    return Range(a, b, "[)")


def load_snapshot(c: psycopg.Connection):
    s = sqlite3.connect(SQLITE)
    s.row_factory = sqlite3.Row
    q = lambda sql: [dict(r) for r in s.execute(sql)]
    c.execute("SET search_path = setuhaul")
    for r in q("SELECT * FROM carriers"):
        c.execute("INSERT INTO carriers VALUES (%s,%s,%s)", (r["carrier_id"], r["carrier_name"], bool(r["active_flag"])))
    for r in q("SELECT * FROM drivers"):
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164,status) VALUES (%s,%s,%s,%s,%s)",
                  (r["driver_id"], r["carrier_id"], r["driver_name"], r["phone"].replace("-", ""), r["driver_status"]))
    for r in q("SELECT * FROM vehicles"):
        c.execute("INSERT INTO vehicles VALUES (%s,%s,%s,%s,%s,%s)", (r["vehicle_id"], r["carrier_id"], r["vehicle_type_code"],
                  r["registration_number"], r["capacity_kg"], bool(r["refrigeration_capable"])))
    for r in q("SELECT * FROM facilities"):
        c.execute("INSERT INTO facilities (facility_id,facility_name,timezone,open_time,close_time) VALUES (%s,%s,%s,%s,%s)",
                  (r["facility_id"], r["facility_name"], r["timezone"], r["open_time"], r["close_time"]))
    c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,checkin_early_limit_min,
                 heavy_dock_required_kg,last_new_start_local,warehouse_confirm_ttl_min,shifts) VALUES
                 ('FAC-JAI-01',1,10,10,30,60,25000,'21:00','{"DAY":15,"NIGHT":10}','[{"name":"DAY","start":"06:00","end":"14:00"},{"name":"NIGHT","start":"14:00","end":"22:00"}]'),
                 ('FAC-GGN-01',1,15,10,20,NULL,NULL,NULL,'{"DAY":15,"NIGHT":0}','[{"name":"DAY","start":"07:00","end":"15:00"},{"name":"NIGHT","start":"15:00","end":"21:00"}]')""")
    for r in q("SELECT * FROM docks"):
        c.execute("INSERT INTO docks VALUES (%s,%s,%s,%s,%s,%s,%s)", (r["dock_id"], r["facility_id"], r["dock_code"], r["dock_type"],
                  bool(r["supports_refrigerated"]), r["max_vehicle_weight_kg"], 90 if r["dock_type"] == "HEAVY" else 60))
    shp = {r["shipment_id"]: r for r in q("SELECT * FROM shipments")}
    for r in shp.values():
        c.execute("""INSERT INTO shipments VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (r["shipment_id"], r["order_reference"], r["carrier_id"], r["driver_id"], r["vehicle_id"], r["destination_facility_id"],
                   r["load_weight_kg"], r["required_dock_type"], bool(r["temperature_control_required"]), r["priority_code"],
                   r["expected_unload_min"], r["planned_departure_ts"], r["actual_departure_ts"], r["original_eta_ts"],
                   r["current_status"], r["updated_at"]))
    for r in q("SELECT * FROM appointment_slots"):
        c.execute("INSERT INTO appointment_slots VALUES (%s,%s,%s,%s,%s)", (r["slot_id"], r["dock_id"],
                  rng(ts(r["slot_start_ts"]), ts(r["slot_end_ts"])), r["slot_status"], r["block_reason"]))
    log(f"- Reference data loaded: {len(shp)} shipments, {s.execute('select count(*) from docks').fetchone()[0]} docks, "
        f"{s.execute('select count(*) from appointment_slots').fetchone()[0]} slots (slot non-overlap constraint satisfied).")

    rejected, accepted = [], []

    def try_insert(label, sql, params, why_expected=""):
        try:
            with c.transaction():
                c.execute(sql, params)
            accepted.append(label)
        except psycopg.Error as e:
            code = e.sqlstate
            msg = (e.diag.message_primary or str(e)).split("\n")[0]
            if code == "23P01":
                msg = "EXCLUSION: overlaps an existing occupancy on the same dock"
            rejected.append((label, msg, why_expected))

    # 1. physical reality: dock blocks
    for e in q("SELECT * FROM dock_status_events"):
        c.execute("INSERT INTO dock_events VALUES (%s,%s,%s,%s,%s,%s,%s)", (e["dock_event_id"], e["dock_id"], e["event_type"],
                  rng(ts(e["event_start_ts"]), ts(e["event_end_ts"])), e["reason"], "loader", e["created_at"]))
        if e["event_type"] in ("BREAKDOWN", "MAINTENANCE"):
            fac = e["dock_id"].split("-")[1]
            try_insert(f"BLOCK {e['dock_event_id']} {e['dock_id']}",
                       "INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,ref_id,created_by) VALUES (%s,%s,%s,'BLOCK',%s,'loader')",
                       (e["dock_id"], f"FAC-{fac}-01", rng(ts(e["event_start_ts"]), ts(e["event_end_ts"])), e["dock_event_id"]))
    # 2. trucks physically in dock (fixed), predicted end = unload_start + expected unload (+ changeover)
    for k in q("SELECT * FROM facility_checkins WHERE queue_state='IN_DOCK'"):
        s0 = ts(k["unload_start_ts"])
        e0 = s0 + timedelta(minutes=shp[k["shipment_id"]]["expected_unload_min"] + CHANGEOVER)
        try_insert(f"FIXED {k['shipment_id']} {k['actual_dock_id']} {s0:%H:%M}-{e0:%H:%M}",
                   "INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by) VALUES (%s,%s,%s,'FIXED',%s,%s,'loader')",
                   (k["actual_dock_id"], k["facility_id"], rng(s0, e0), k["shipment_id"], k["checkin_id"]))
    # 3. active bookings in the order they were made; occupancy = slot start + unload + changeover (true dock time)
    appts = q("""SELECT a.*, sl.dock_id, sl.facility_id, sl.slot_start_ts, sl.slot_end_ts FROM appointments a
                 JOIN appointment_slots sl USING (slot_id)
                 WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED') ORDER BY a.booked_at""")
    for a in appts:
        s0 = ts(a["slot_start_ts"])
        e0 = max(ts(a["slot_end_ts"]), s0 + timedelta(minutes=shp[a["shipment_id"]]["expected_unload_min"] + CHANGEOVER))
        try_insert(f"{a['appointment_status']} {a['appointment_id']} {a['shipment_id']} {a['dock_id'].split('-')[-1]} {s0:%H:%M}-{e0:%H:%M}",
                   "INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by) VALUES (%s,%s,%s,'APPOINTMENT',%s,%s,'loader')",
                   (a["dock_id"], a["facility_id"], rng(s0, e0), a["shipment_id"], a["appointment_id"]))
    log(f"- Occupancy load: **{len(accepted)} accepted, {len(rejected)} rejected by constraints**:")
    log("")
    log("| Rejected row | Constraint message |")
    log("|---|---|")
    for label, msg, _ in rejected:
        log(f"| {label} | {msg} |")
    log("")
    return shp


def make_synthetic_shipments(c, n, facility="FAC-JAI-01", prefix="SYN", vehicle=None):
    """Each synthetic shipment gets its own truck unless `vehicle` is given (truck-level exclusion test)."""
    ids = []
    for i in range(n):
        sid = f"{prefix}{i:04d}"
        vid = vehicle or f"V{sid}"
        c.execute("INSERT INTO vehicles VALUES (%s,'CAR001','32FT_SXL',%s,15000,false) ON CONFLICT DO NOTHING", (vid, f"REG-{vid}"))
        c.execute("""INSERT INTO shipments VALUES (%s,%s,'CAR001','DRV001',%s,%s,10000,'STANDARD',false,'NORMAL',45,
                     '2026-08-05T06:00+05:30',NULL,'2026-08-05T12:00+05:30','IN_TRANSIT',now()) ON CONFLICT DO NOTHING""",
                  (sid, f"ORD-{sid}", vid, facility))
        ids.append(sid)
    return ids


def as_agent(conn, scope="FAC-JAI-01"):
    """Least-privilege tool-service session: agent role + facility scope (no superuser, no table writes on capacity)."""
    conn.execute("SET search_path = setuhaul")
    conn.execute("SET ROLE setuhaul_agent")
    conn.execute("SELECT set_config('app.facility_scope', %s, false)", (scope,))


def contention_rounds(n_users: int, rounds: int):
    winners_per_round, lost, other = [], 0, []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        ships = make_synthetic_shipments(c, n_users)
    for r in range(rounds):
        start = datetime.fromisoformat("2026-08-05T13:00:00+05:30") + timedelta(hours=r % 8)
        window = rng(start, start + timedelta(minutes=55))
        opts = []
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("SET search_path = setuhaul")
            for i, sid in enumerate(ships):
                oid = uuid.uuid4()
                c.execute("INSERT INTO shown_options VALUES (%s,%s,%s,'DOCK-JAI-D2',%s,false,1,0,now()+interval '5 min',now())",
                          (oid, f"T{i}", sid, window))
                opts.append((oid, f"T{i}"))
        barrier = threading.Barrier(n_users)
        results = []
        lock = threading.Lock()

        def worker(oid, tid):
            conn = psycopg.connect(DSN, autocommit=True)
            as_agent(conn)
            barrier.wait()
            try:
                conn.execute("SELECT * FROM place_hold(%s,%s,'loadtest',interval '3 min')", (oid, tid))
                res = "WON"
            except psycopg.errors.ExclusionViolation:
                res = "LOST"
            except psycopg.Error as e:
                res = f"ERR {e.sqlstate}"
            finally:
                conn.close()
            with lock:
                results.append(res)

        threads = [threading.Thread(target=worker, args=o) for o in opts]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners_per_round.append(results.count("WON"))
        lost += results.count("LOST")
        other += [x for x in results if x not in ("WON", "LOST")]
        with psycopg.connect(DSN, autocommit=True) as c:   # release this round's holds so shipments can hold again
            c.execute("UPDATE setuhaul.dock_occupancy SET active=false WHERE kind='HOLD'")
    return winners_per_round, lost, other


def overlap_storm(n_users: int):
    """Different-length, partially overlapping windows on one dock; afterwards no two active rows may overlap."""
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        ships = make_synthetic_shipments(c, n_users)
        base = datetime.fromisoformat("2026-08-06T09:00:00+05:30")
        opts = []
        for i, sid in enumerate(ships):
            s0 = base + timedelta(minutes=random.randrange(0, 240, 5))
            oid = uuid.uuid4()
            c.execute("INSERT INTO shown_options VALUES (%s,%s,%s,'DOCK-JAI-D4',%s,false,1,0,now()+interval '5 min',now())",
                      (oid, f"S{i}", sid, rng(s0, s0 + timedelta(minutes=random.choice([55, 70, 85, 100])))))
            opts.append((oid, f"S{i}"))
    barrier = threading.Barrier(n_users)
    won = []

    def worker(oid, tid):
        conn = psycopg.connect(DSN, autocommit=True)
        as_agent(conn)
        barrier.wait()
        try:
            conn.execute("SELECT * FROM place_hold(%s,%s,'loadtest',interval '3 min')", (oid, tid))
            won.append(tid)
        except psycopg.Error:
            pass
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=o) for o in opts]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with psycopg.connect(DSN) as c:
        overlaps = c.execute("""SELECT count(*) FROM setuhaul.dock_occupancy a JOIN setuhaul.dock_occupancy b
                                ON a.dock_id=b.dock_id AND a.occupancy_id<b.occupancy_id AND a.during && b.during
                                WHERE a.active AND b.active""").fetchone()[0]
    return len(won), overlaps


def rule_checks():
    rows = []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01,FAC-GGN-01', false)")

        def attempt(label, sql, params):
            try:
                with c.transaction():
                    c.execute(sql, params)
                rows.append((label, "ACCEPTED (unexpected)"))
            except psycopg.Error as e:
                rows.append((label, f"REJECTED: {(e.diag.message_primary or '').split(chr(10))[0]}"))

        ins = "INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by) VALUES (%s,%s,%s,'APPOINTMENT',%s,'t','test')"
        w = rng(ts("2026-08-07T10:00+05:30"), ts("2026-08-07T11:25+05:30"))
        attempt("SHP1014 (21 t) on D1 (20 t max)", ins, ("DOCK-JAI-D1", "FAC-JAI-01", w, "SHP1014"))
        attempt("SHP1015 (reefer) on D2 (standard)", ins, ("DOCK-JAI-D2", "FAC-JAI-01", w, "SHP1015"))
        attempt("SHP1016 (31 t heavy) on D4", ins, ("DOCK-JAI-D4", "FAC-JAI-01", w, "SHP1016"))
        attempt("SHP1021 (Gurugram) on Jaipur D1", ins, ("DOCK-JAI-D1", "FAC-JAI-01", w, "SHP1021"))
        attempt("SHP1019 (cancelled) on D1", ins, ("DOCK-JAI-D1", "FAC-JAI-01", w, "SHP1019"))
        attempt("Second appointment row for SHP1013 (current, active)",
                "INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,status,booking_source,pending_expires_at) VALUES "
                "('X1','SHP1013','DOCK-JAI-D1',%s,'CONFIRMED','DRIVER_CHAT',NULL),('X2','SHP1013','DOCK-JAI-D4',%s,'CONFIRMED','DRIVER_CHAT',NULL)", (w, w))
        c.execute("INSERT INTO booking_events (principal,principal_role,action) VALUES ('t','test','x')")
        attempt("UPDATE on booking_events (audit trail)", "UPDATE booking_events SET action='y'", ())
        attempt("Duplicate exception with same dedupe_key",
                "INSERT INTO chat_threads (thread_id,driver_id,status) VALUES ('TT','DRV006','OPEN') ON CONFLICT DO NOTHING; "
                "INSERT INTO driver_exceptions VALUES ('E1','SHP1006','DRV006','TT','TRAFFIC','OPEN','DRV006-SHP1006-0934',now()),"
                "('E2','SHP1006','DRV006','TT','TRAFFIC','OPEN','DRV006-SHP1006-0934',now())", ())
        # paused facility refuses holds
        sid = make_synthetic_shipments(c, 1)[0]
        oid = uuid.uuid4()
        c.execute("INSERT INTO shown_options VALUES (%s,'P1',%s,'DOCK-JAI-D1',%s,false,1,0,now()+interval '5 min',now())",
                  (oid, sid, rng(ts("2026-08-08T10:00+05:30"), ts("2026-08-08T10:55+05:30"))))
        c.execute("UPDATE facilities SET automation_paused = true WHERE facility_id='FAC-JAI-01'")
        attempt("place_hold while facility automation is paused", "SELECT * FROM place_hold(%s,'P1','t',interval '3 min')", (oid,))
        c.execute("UPDATE facilities SET automation_paused = false WHERE facility_id='FAC-JAI-01'")
        # expired option
        oid2 = uuid.uuid4()
        c.execute("INSERT INTO shown_options VALUES (%s,'P2',%s,'DOCK-JAI-D1',%s,false,1,0,now()-interval '1 min',now())",
                  (oid2, sid, rng(ts("2026-08-08T12:00+05:30"), ts("2026-08-08T12:55+05:30"))))
        attempt("place_hold on an expired option", "SELECT * FROM place_hold(%s,'P2','t',interval '3 min')", (oid2,))
        # truck-level exclusion: two different shipments on the SAME truck at overlapping times on different docks
        a, b = make_synthetic_shipments(c, 2, prefix="TRK", vehicle="VTRUCK1")
        wt = rng(ts("2026-08-09T10:00+05:30"), ts("2026-08-09T11:00+05:30"))
        c.execute(ins, ("DOCK-JAI-D1", "FAC-JAI-01", wt, a))
        attempt("Same truck (VTRUCK1) booked on D2 for another shipment at an overlapping time", ins, ("DOCK-JAI-D2", "FAC-JAI-01", wt, b))
    return rows


def rls_checks():
    """Re-review A: facility RLS on every facility-owned table, reads and writes, no bypass roles, fail closed."""
    rows = []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        for fac in ("FAC-JAI-01", "FAC-GGN-01"):
            c.execute("""INSERT INTO escalations (escalation_id,reason_code,mode,facility_id,handoff,ack_due_at)
                         VALUES (%s,'NO_FEASIBLE_SLOT','DECIDE',%s,'{}',now()+interval '10 min')""", (uuid.uuid4(), fac))
            ev = c.execute("INSERT INTO outbox (facility_id,aggregate,aggregate_id,event_type,payload) VALUES (%s,'x','x','T','{}') RETURNING event_id",
                           (fac,)).fetchone()[0]
            c.execute("""INSERT INTO notifications (notification_id,facility_id,source_event_id,channel,recipient_ref,template_id,payload)
                         VALUES (%s,%s,%s,'WHATSAPP','drv-ref','T1','{}')""", (uuid.uuid4(), fac, ev))
            c.execute("INSERT INTO booking_events (principal,principal_role,action,facility_id) VALUES ('t','test','x',%s)", (fac,))

        def count_as(role, table, scope):
            with c.transaction():
                c.execute(f"SET LOCAL ROLE {role}")
                if scope is not None:
                    c.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
                return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

        jai_occ = c.execute("SELECT count(*) FROM dock_occupancy WHERE facility_id='FAC-JAI-01' AND active").fetchone()[0]
        rows.append(("Console, scope GGN: escalations", count_as("setuhaul_console", "escalations", "FAC-GGN-01"), 1))
        rows.append(("Console, scope JAI+GGN: escalations", count_as("setuhaul_console", "escalations", "FAC-JAI-01,FAC-GGN-01"), 2))
        rows.append(("Console, scope unset (after DISCARD ALL): escalations", count_as("setuhaul_console", "escalations", None), 0))
        rows.append(("Agent, scope JAI: active dock occupancy (JAI rows)",
                     count_as("setuhaul_agent", "dock_occupancy WHERE active AND facility_id='FAC-JAI-01'", "FAC-JAI-01"), jai_occ))
        rows.append(("Agent, scope GGN: dock occupancy (all JAI rows hidden)",
                     count_as("setuhaul_agent", "dock_occupancy WHERE facility_id='FAC-JAI-01'", "FAC-GGN-01"), 0))
        for role, t in (("setuhaul_console", "outbox"), ("setuhaul_console", "notifications"),
                        ("setuhaul_console", "booking_events WHERE facility_id IS NOT NULL"),
                        ("setuhaul_agent", "appointments"), ("setuhaul_agent", "shown_options")):
            rows.append((f"{role.split('_')[1].title()}, scope unset: {t.split()[0]}", count_as(role, t, None), 0))
        rows.append(("System job, scope = all facilities: outbox", count_as("setuhaul_system", "outbox", "FAC-JAI-01,FAC-GGN-01"), 2))

        # write path: WITH CHECK blocks writing into a facility outside the scope
        try:
            with c.transaction():
                c.execute("SET LOCAL ROLE setuhaul_rpc")   # the only role allowed to insert capacity rows
                c.execute("SELECT set_config('app.facility_scope','FAC-GGN-01',true)")
                c.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,ref_id,created_by)
                             VALUES ('DOCK-JAI-D1','FAC-JAI-01',%s,'BLOCK','x','t')""",
                          (rng(ts("2026-08-10T10:00+05:30"), ts("2026-08-10T11:00+05:30")),))
            write_blocked = 0
        except psycopg.errors.InsufficientPrivilege as e:
            write_blocked = 1 if "row-level security" in str(e) else 0
        rows.append(("Function-owner role scoped to GGN inserts a JAI dock block (WITH CHECK)", write_blocked, 1))

        # non-superuser owner is also subject to RLS (FORCE)
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='setuhaul_owner') "
                  "THEN CREATE ROLE setuhaul_owner NOLOGIN; END IF; END $$")
        c.execute("GRANT USAGE ON SCHEMA setuhaul TO setuhaul_owner")
        c.execute("GRANT EXECUTE ON FUNCTION scope_allows(text) TO setuhaul_owner")
        c.execute("ALTER TABLE escalations OWNER TO setuhaul_owner")
        rows.append(("Non-superuser table owner, scope unset (FORCE)", count_as("setuhaul_owner", "escalations", None), 0))

        bad = c.execute("""SELECT count(*) FROM pg_roles WHERE rolname IN ('setuhaul_console','setuhaul_agent','setuhaul_system','setuhaul_rpc')
                           AND (rolsuper OR rolbypassrls)""").fetchone()[0]
        rows.append(("App roles that are SUPERUSER or BYPASSRLS", bad, 0))
    rows += rls_driver_checks()
    return rows


# Tables deliberately without RLS, with the reason. Anything not RLS-forced and not listed here fails the coverage check.
NO_RLS_ALLOWLIST = {
    "carriers": "reference data", "drivers": "reference data (identity resolved by the API, not queried freely)",
    "vehicles": "reference data", "facilities": "reference data", "facility_config": "reference data",
    "docks": "reference data", "idempotency_keys": "hashed keys, no facility or personal data",
}


def rls_driver_checks():
    """Third review P0-A: eta_signals, driver_exceptions, tracking_consents. Driver or facility access, reads and writes."""
    rows = []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        # two real shipments with different drivers at different facilities
        a = c.execute("""SELECT shipment_id, driver_id, destination_facility_id FROM shipments
                         WHERE destination_facility_id='FAC-JAI-01' ORDER BY shipment_id LIMIT 1""").fetchone()
        b = c.execute("""SELECT shipment_id, driver_id, destination_facility_id FROM shipments
                         WHERE destination_facility_id <> 'FAC-JAI-01' AND driver_id <> %s ORDER BY shipment_id LIMIT 1""",
                      (a[1],)).fetchone()
        for sid, drv, _fac in (a, b):
            c.execute("""INSERT INTO eta_signals (shipment_id,source,observed_at,derived_eta,confidence)
                         VALUES (%s,'LOCATION_PIN',now(),now()+interval '1 hour','MEDIUM')""", (sid,))
            c.execute("""INSERT INTO tracking_consents VALUES (%s,%s,%s,'LOCATION_PIN',now(),now()+interval '1 day',NULL,'v1')""",
                      (uuid.uuid4(), drv, sid))
            tid = f"RLS-{sid}"
            c.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')", (tid, drv, sid))
            c.execute("""INSERT INTO driver_exceptions VALUES (%s,%s,%s,%s,'DELAY','OPEN',%s,now())""",
                      (f"EX-{sid}", sid, drv, tid, f"dk-{sid}"))

        def run_as(role, scope, driver, sql, params=()):
            with c.transaction():
                c.execute(f"SET LOCAL ROLE {role}")
                if scope is not None:
                    c.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
                if driver is not None:
                    c.execute("SELECT set_config('app.driver_id', %s, true)", (driver,))
                cur = c.execute(sql, params)
                return cur.fetchone()[0] if cur.description else None

        def blocked(role, scope, driver, sql, params):
            """1 only when ROW-LEVEL SECURITY blocked the write (a missing grant would prove nothing about RLS)."""
            try:
                run_as(role, scope, driver, sql, params)
                return 0
            except psycopg.errors.InsufficientPrivilege as e:
                return 1 if "row-level security" in str(e) else 0

        for t in ("eta_signals", "tracking_consents", "driver_exceptions"):
            q_a = f"SELECT count(*) FROM {t} WHERE shipment_id = %s"
            rows.append((f"Driver A session: own rows in {t}", run_as("setuhaul_agent", None, a[1], q_a, (a[0],)), 1))
            rows.append((f"Driver A session: driver B's rows in {t}", run_as("setuhaul_agent", None, a[1], q_a, (b[0],)), 0))
            rows.append((f"Driver A session + JAI scope also set: driver B's rows in {t} (driver context is strict)",
                         run_as("setuhaul_agent", f"FAC-JAI-01,{b[2]}", a[1], q_a, (b[0],)), 0))
            rows.append((f"Staff scope JAI: other facility's rows in {t}", run_as("setuhaul_console", "FAC-JAI-01", None, q_a, (b[0],)), 0))
            rows.append((f"Staff scope JAI: own facility rows in {t}", run_as("setuhaul_console", "FAC-JAI-01", None, q_a, (a[0],)), 1))
            rows.append((f"No scope, no driver: {t}", run_as("setuhaul_agent", None, None, f"SELECT count(*) FROM {t}"), 0))

        rows.append(("Driver A writes an ETA signal for driver B's shipment (WITH CHECK)", blocked(
            "setuhaul_agent", None, a[1], """INSERT INTO eta_signals (shipment_id,source,observed_at,derived_eta,confidence)
                                             VALUES (%s,'LOCATION_PIN',now(),now(),'LOW')""", (b[0],)), 1))
        rows.append(("Driver A records consent in driver B's name", blocked(
            "setuhaul_agent", None, a[1], "INSERT INTO tracking_consents VALUES (%s,%s,%s,'LOCATION_PIN',now(),now(),NULL,'v1')",
            (uuid.uuid4(), b[1], b[0])), 1))
        rows.append(("Driver A records own consent against driver B's shipment", blocked(
            "setuhaul_agent", None, a[1], "INSERT INTO tracking_consents VALUES (%s,%s,%s,'LOCATION_PIN',now(),now(),NULL,'v1')",
            (uuid.uuid4(), a[1], b[0])), 1))
        rows.append(("Tool service scoped to JAI inserts an exception for another facility's shipment", blocked(
            "setuhaul_agent", "FAC-JAI-01", None, "INSERT INTO driver_exceptions VALUES ('EX-X',%s,%s,%s,'DELAY','OPEN','dk-x',now())",
            (b[0], b[1], f"RLS-{b[0]}")), 1))
        rows.append(("Driver A updates driver B's exception (rows affected)", run_as(
            "setuhaul_agent", None, a[1], "WITH u AS (UPDATE driver_exceptions SET status='CLOSED' WHERE shipment_id=%s RETURNING 1) SELECT count(*) FROM u",
            (b[0],)), 0))
        ok = run_as("setuhaul_agent", None, a[1], """WITH i AS (INSERT INTO eta_signals (shipment_id,source,observed_at,derived_eta,confidence)
                     VALUES (%s,'LOCATION_PIN',now(),now(),'LOW') RETURNING 1) SELECT count(*) FROM i""", (a[0],))
        rows.append(("Driver A writes an ETA signal for own shipment", ok, 1))

        # coverage: every table is RLS-forced or allowlisted with a reason
        unprotected = [r[0] for r in c.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='setuhaul' AND c.relkind='r' AND NOT (c.relrowsecurity AND c.relforcerowsecurity)""")]
        rows.append(("Tables neither RLS-forced nor on the reasoned allowlist",
                     len([t for t in unprotected if t not in NO_RLS_ALLOWLIST]), 0))
    return rows


def lifecycle_checks():
    """Re-review C and D: hold expiry releases capacity; one notification per event and channel."""
    rows = []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01,FAC-GGN-01', false)")
        a, b = make_synthetic_shipments(c, 2, prefix="EXP")
        w = rng(ts("2026-08-11T10:00+05:30"), ts("2026-08-11T10:55+05:30"))
        o1, o2 = uuid.uuid4(), uuid.uuid4()
        for o, sid, tid in ((o1, a, "E1"), (o2, b, "E2")):
            c.execute("INSERT INTO shown_options VALUES (%s,%s,%s,'DOCK-JAI-D3',%s,false,1,0,now()+interval '5 min',now())", (o, tid, sid, w))
        c.execute("SELECT * FROM place_hold(%s,'E1','t',interval '3 min')", (o1,))
        try:
            c.execute("SELECT * FROM place_hold(%s,'E2','t',interval '3 min')", (o2,))
            rows.append(("Second driver while the first hold is live", "ACCEPTED (unexpected)"))
        except psycopg.errors.ExclusionViolation:
            rows.append(("Second driver while the first hold is live", "REJECTED: slot held (LOST)"))
        c.execute("UPDATE dock_occupancy SET expires_at = now() - interval '1 second' WHERE kind='HOLD' AND shipment_id=%s", (a,))
        c.execute("SELECT * FROM place_hold(%s,'E2','t',interval '3 min')", (o2,))
        active_a = c.execute("SELECT count(*) FROM dock_occupancy WHERE shipment_id=%s AND active", (a,)).fetchone()[0]
        rows.append(("Second driver after the first hold expired (3-min TTL passed)",
                     f"ACCEPTED; expired hold released in the same transaction (first driver active holds = {active_a})"))
        ev = c.execute("INSERT INTO outbox (facility_id,aggregate,aggregate_id,event_type,payload) VALUES ('FAC-JAI-01','a','1','BookingRequested','{}') RETURNING event_id").fetchone()[0]
        c.execute("""INSERT INTO notifications (notification_id,facility_id,source_event_id,channel,recipient_ref,template_id,payload)
                     VALUES (%s,'FAC-JAI-01',%s,'WHATSAPP','r','T1','{}')""", (uuid.uuid4(), ev))
        try:
            c.execute("""INSERT INTO notifications (notification_id,facility_id,source_event_id,channel,recipient_ref,template_id,payload)
                         VALUES (%s,'FAC-JAI-01',%s,'WHATSAPP','r','T1','{}')""", (uuid.uuid4(), ev))
            rows.append(("Relay retry creates a second WhatsApp notification for the same event", "ACCEPTED (unexpected)"))
        except psycopg.errors.UniqueViolation:
            rows.append(("Relay retry creates a second WhatsApp notification for the same event", "REJECTED: one notification per event and channel"))
        try:
            c.execute("UPDATE notifications SET status='SENDING' WHERE source_event_id=%s", (ev,))
            c.execute("UPDATE notifications SET status='RETRY' WHERE source_event_id=%s", (ev,))
            rows.append(("RETRY without next_retry_at", "ACCEPTED (unexpected)"))
        except psycopg.errors.CheckViolation:
            rows.append(("RETRY without next_retry_at", "REJECTED: retry must be scheduled"))
    return rows


def mirror_rls_checks():
    """Fourth review: RLS on shipments, appointment_slots, dock_events, and legitimate access still works."""
    rows = []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        jai = c.execute("""SELECT shipment_id, driver_id FROM shipments WHERE destination_facility_id='FAC-JAI-01'
                           AND current_status NOT IN ('CANCELLED','COMPLETED') ORDER BY shipment_id LIMIT 1""").fetchone()
        other = c.execute("""SELECT shipment_id, driver_id FROM shipments WHERE driver_id <> %s ORDER BY shipment_id LIMIT 1""",
                          (jai[1],)).fetchone()
        n_jai = c.execute("SELECT count(*) FROM shipments WHERE destination_facility_id='FAC-JAI-01'").fetchone()[0]
        n_own = c.execute("SELECT count(*) FROM shipments WHERE driver_id=%s", (jai[1],)).fetchone()[0]
        n_slots = c.execute("""SELECT count(*) FROM appointment_slots s JOIN docks d USING (dock_id)
                               WHERE d.facility_id='FAC-JAI-01'""").fetchone()[0]
        c.execute("""INSERT INTO dock_events VALUES ('DE-RLS','DOCK-JAI-D1','MAINTENANCE',%s,'test','t',now())
                     ON CONFLICT DO NOTHING""", (rng(ts("2026-08-12T10:00+05:30"), ts("2026-08-12T11:00+05:30")),))

        def q(role, scope, driver, sql, params=()):
            with c.transaction():
                c.execute(f"SET LOCAL ROLE {role}")
                if scope is not None:
                    c.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
                if driver is not None:
                    c.execute("SELECT set_config('app.driver_id', %s, true)", (driver,))
                return c.execute(sql, params).fetchone()[0]

        cnt = "SELECT count(*) FROM shipments WHERE shipment_id=%s"
        rows.append(("Driver A reads own shipment", q("setuhaul_agent", None, jai[1], cnt, (jai[0],)), 1))
        rows.append(("Driver A reads all shipments: sees only their own", q("setuhaul_agent", None, jai[1], "SELECT count(*) FROM shipments"), n_own))
        rows.append(("Driver A reads driver B's shipment", q("setuhaul_agent", None, jai[1], cnt, (other[0],)), 0))
        rows.append(("Driver A + JAI facility scope reads driver B's shipment (driver context is strict)",
                     q("setuhaul_agent", "FAC-JAI-01,FAC-GGN-01", jai[1], cnt, (other[0],)), 0))
        rows.append(("GGN staff read JAI shipments", q("setuhaul_console", "FAC-GGN-01", None,
                     "SELECT count(*) FROM shipments WHERE destination_facility_id='FAC-JAI-01'"), 0))
        rows.append(("JAI staff read JAI shipments", q("setuhaul_console", "FAC-JAI-01", None,
                     "SELECT count(*) FROM shipments WHERE destination_facility_id='FAC-JAI-01'"), n_jai))
        rows.append(("No scope, no driver: shipments", q("setuhaul_agent", None, None, "SELECT count(*) FROM shipments"), 0))
        rows.append(("GGN staff read JAI appointment slots", q("setuhaul_console", "FAC-GGN-01", None,
                     "SELECT count(*) FROM appointment_slots s JOIN docks d USING (dock_id) WHERE d.facility_id='FAC-JAI-01'"), 0))
        rows.append(("JAI staff read JAI appointment slots", q("setuhaul_console", "FAC-JAI-01", None,
                     "SELECT count(*) FROM appointment_slots s JOIN docks d USING (dock_id) WHERE d.facility_id='FAC-JAI-01'"), n_slots))
        rows.append(("GGN staff read a JAI dock event", q("setuhaul_console", "FAC-GGN-01", None,
                     "SELECT count(*) FROM dock_events WHERE dock_event_id='DE-RLS'"), 0))
        rows.append(("JAI staff read a JAI dock event", q("setuhaul_console", "FAC-JAI-01", None,
                     "SELECT count(*) FROM dock_events WHERE dock_event_id='DE-RLS'"), 1))
        # legitimate access through the dependent policies still works after shipments got RLS
        tid = f"RLS-{jai[0]}"
        c.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN') ON CONFLICT DO NOTHING",
                  (tid, jai[1], jai[0]))
        rows.append(("Driver A still reads own chat thread (policy looks up shipments)",
                     q("setuhaul_agent", None, jai[1], "SELECT count(*) FROM chat_threads WHERE thread_id=%s", (tid,)), 1))
        rows.append(("JAI staff still read that thread", q("setuhaul_console", "FAC-JAI-01", None,
                     "SELECT count(*) FROM chat_threads WHERE thread_id=%s", (tid,)), 1))
        rows.append(("GGN staff cannot read that thread", q("setuhaul_console", "FAC-GGN-01", None,
                     "SELECT count(*) FROM chat_threads WHERE thread_id=%s", (tid,)), 0))
        # fail closed: driver A's session (with facility scope) tries to hold a slot for another driver's JAI shipment
        b = c.execute("""SELECT shipment_id FROM shipments WHERE destination_facility_id='FAC-JAI-01' AND driver_id<>%s
                         AND current_status NOT IN ('CANCELLED','COMPLETED') ORDER BY shipment_id LIMIT 1""", (jai[1],)).fetchone()[0]
        oid = uuid.uuid4()
        c.execute("INSERT INTO shown_options VALUES (%s,'FC1',%s,'DOCK-JAI-D3',%s,false,1,0,now()+interval '5 min',now())",
                  (oid, b, rng(ts("2026-08-13T10:00+05:30"), ts("2026-08-13T10:55+05:30"))))
        try:
            q("setuhaul_agent", "FAC-JAI-01", jai[1], "SELECT count(*) FROM place_hold(%s,'FC1','t',interval '3 min')", (oid,))
            res = 0
        except psycopg.Error as e:
            res = 1 if "SHIPMENT_NOT_VISIBLE" in str(e) else 0
        rows.append(("Driver A's session holds a slot for driver B's shipment (compat trigger fails closed)", res, 1))
    return rows


def privilege_checks():
    """Fourth review P0: least privilege. RLS limits rows; grants limit operations."""
    rows = []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")

        def denied(role, scope, sql, params=()):
            """1 when the database refused for lack of PRIVILEGE (not RLS)."""
            try:
                with c.transaction():
                    c.execute(f"SET LOCAL ROLE {role}")
                    c.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
                    c.execute(sql, params)
                return 0
            except psycopg.errors.InsufficientPrivilege as e:
                return 0 if "row-level security" in str(e) else 1

        J = "FAC-JAI-01"
        rows.append(("Agent UPDATEs dock_occupancy directly", denied("setuhaul_agent", J,
                     "UPDATE dock_occupancy SET active=false WHERE facility_id='FAC-JAI-01'"), 1))
        rows.append(("Agent INSERTs an appointment directly", denied("setuhaul_agent", J,
                     "INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,status,booking_source) "
                     "VALUES ('PX','SHP1013','DOCK-JAI-D1','[2026-08-20 10:00+05:30,2026-08-20 11:00+05:30)','CONFIRMED','DRIVER_CHAT')"), 1))
        rows.append(("Agent UPDATEs a notification", denied("setuhaul_agent", J, "UPDATE notifications SET status='FAILED'"), 1))
        rows.append(("Agent approves its own booking request (approval_requests)", denied("setuhaul_agent", J,
                     "UPDATE approval_requests SET status='APPROVED'"), 1))
        rows.append(("Agent reads Step Functions task tokens", denied("setuhaul_agent", J, "SELECT task_token FROM approval_requests"), 1))
        rows.append(("Agent clears the human-takeover flag", denied("setuhaul_agent", J, "UPDATE chat_threads SET human_active=false"), 1))
        rows.append(("Agent pauses or un-pauses a facility", denied("setuhaul_agent", J, "UPDATE facilities SET automation_paused=false"), 1))
        rows.append(("Agent DELETEs a chat message", denied("setuhaul_agent", J, "DELETE FROM chat_messages"), 1))
        rows.append(("Console UPDATEs dock_occupancy directly", denied("setuhaul_console", J,
                     "UPDATE dock_occupancy SET active=false WHERE facility_id='FAC-JAI-01'"), 1))
        rows.append(("Console moves an escalation to another facility (column not granted)", denied("setuhaul_console", J,
                     "UPDATE escalations SET facility_id='FAC-GGN-01'"), 1))
        rows.append(("Console acknowledges an escalation (granted column)", denied("setuhaul_console", J,
                     "UPDATE escalations SET status='ACKNOWLEDGED', acknowledged_at=now() WHERE facility_id='FAC-JAI-01'"), 0))
        rows.append(("System job writes dock_occupancy directly", denied("setuhaul_system", J,
                     "UPDATE dock_occupancy SET active=false"), 1))
        rows.append(("Any app role UPDATEs booking_events", denied("setuhaul_system", J, "UPDATE booking_events SET action='x'"), 1))
        rows.append(("Agent calls the sweeper function (not granted)", denied("setuhaul_agent", J, "SELECT release_expired_holds()"), 1))

        # the intended path works: the agent places a hold only through place_hold()
        sid = make_synthetic_shipments(c, 1, prefix="PRV")[0]
        oid = uuid.uuid4()
        c.execute("INSERT INTO shown_options VALUES (%s,'PV1',%s,'DOCK-JAI-D3',%s,false,1,0,now()+interval '5 min',now())",
                  (oid, sid, rng(ts("2026-08-14T10:00+05:30"), ts("2026-08-14T10:55+05:30"))))
        rows.append(("Agent places a hold through place_hold() (intended path)", denied("setuhaul_agent", J,
                     "SELECT * FROM place_hold(%s,'PV1','t',interval '3 min')", (oid,)), 0))
        try:
            with c.transaction():
                c.execute("SET LOCAL ROLE setuhaul_agent")
                c.execute("SELECT set_config('app.facility_scope', 'FAC-GGN-01', true)")
                c.execute("SELECT * FROM place_hold(%s,'PV1','t',interval '3 min')", (oid,))
            out = 0
        except psycopg.Error as e:
            out = 1 if "OPTION_UNKNOWN" in str(e) else 0
        rows.append(("Agent scoped to GGN calls place_hold() on a JAI option (RLS applies inside the definer function)", out, 1))

        # catalogue checks
        pub = c.execute("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                           WHERE n.nspname='setuhaul' AND has_function_privilege('public', p.oid, 'EXECUTE')""").fetchone()[0]
        rows.append(("Functions executable by PUBLIC", pub, 0))
        weak = c.execute("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                            WHERE n.nspname='setuhaul' AND p.prosecdef
                              AND (pg_get_userbyid(p.proowner) <> 'setuhaul_rpc'
                                   OR NOT coalesce(p.proconfig::text LIKE '%search_path=setuhaul, pg_catalog, pg_temp%', false))""").fetchone()[0]
        rows.append(("SECURITY DEFINER functions not owned by setuhaul_rpc or without a pinned search_path", weak, 0))
        mem = c.execute("""SELECT count(*) FROM unnest(ARRAY['setuhaul_console','setuhaul_agent','setuhaul_system']) r
                           WHERE pg_has_role(r, 'setuhaul_rpc', 'MEMBER')""").fetchone()[0]
        rows.append(("App roles that can become the function owner", mem, 0))
        dele = c.execute("""SELECT count(*) FROM information_schema.role_table_grants WHERE table_schema='setuhaul'
                            AND privilege_type IN ('DELETE','TRUNCATE')
                            AND grantee IN ('setuhaul_console','setuhaul_agent','setuhaul_system','setuhaul_rpc','PUBLIC')""").fetchone()[0]
        rows.append(("DELETE / TRUNCATE grants to any app role", dele, 0))
    return rows


def notification_checks():
    """Fourth review P1 / gate G2 (database part): the UNKNOWN recovery resend happens at most once."""
    rows = []
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")

        def new_notification():
            ev = c.execute("""INSERT INTO outbox (facility_id,aggregate,aggregate_id,event_type,payload)
                              VALUES ('FAC-JAI-01','a','n','BookingConfirmed','{}') RETURNING event_id""").fetchone()[0]
            nid = uuid.uuid4()
            c.execute("""INSERT INTO notifications (notification_id,facility_id,source_event_id,channel,recipient_ref,template_id,payload)
                         VALUES (%s,'FAC-JAI-01',%s,'WHATSAPP','r','T1','{}')""", (nid, ev))
            return nid

        def worker(nid, status):
            """The notification worker's own role and scope."""
            try:
                with c.transaction():
                    c.execute("SET LOCAL ROLE setuhaul_system")
                    c.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01', true)")
                    c.execute("UPDATE notifications SET status=%s WHERE notification_id=%s", (status, nid))
                return "allowed"
            except psycopg.Error as e:
                return "DENIED" if "NOTIFICATION_" in str(e) else f"error {e.sqlstate}"

        def state(nid):
            return c.execute("SELECT status, unknown_resend_count, unknown_since IS NOT NULL, unknown_deadline > now() "
                             "FROM notifications WHERE notification_id=%s", (nid,)).fetchone()

        n = new_notification()
        seq = [("PENDING → SENDING", "SENDING", "allowed"),
               ("Provider times out: SENDING → UNKNOWN", "UNKNOWN", "allowed"),
               ("Grace passed, recovery resend: UNKNOWN → SENDING", "SENDING", "allowed"),
               ("Times out again: SENDING → UNKNOWN", "UNKNOWN", "allowed"),
               ("Second recovery resend: UNKNOWN → SENDING", "SENDING", "DENIED"),
               ("Escalate instead: UNKNOWN → FAILED", "FAILED", "allowed"),
               ("Re-open a FAILED notification: FAILED → SENDING", "SENDING", "DENIED")]
        for label, st, exp in seq:
            rows.append(("Repeated timeouts. " + label, worker(n, st), exp))
        s = state(n)
        rows.append(("Repeated timeouts. Final state: status, recovery resends used", f"{s[0]}, {s[1]}", "FAILED, 1"))

        n2 = new_notification()
        for st in ("SENDING", "UNKNOWN"):
            worker(n2, st)
        s = state(n2)
        rows.append(("UNKNOWN records when and until when to wait", f"since set={s[2]}, deadline in future={s[3]}", "since set=True, deadline in future=True"))
        rows.append(("Delayed webhook arrives: UNKNOWN → DELIVERED", worker(n2, "DELIVERED"), "allowed"))
        rows.append(("Late duplicate webhook or worker re-sends: DELIVERED → SENDING", worker(n2, "SENDING"), "DENIED"))

        n3 = new_notification()
        worker(n3, "SENDING")
        c.execute("ALTER TABLE notifications DISABLE TRIGGER notification_transition")   # test fixture: simulate a stale claim
        c.execute("UPDATE notifications SET sending_started_at = now() - interval '10 minutes' WHERE notification_id=%s", (n3,))
        c.execute("ALTER TABLE notifications ENABLE TRIGGER notification_transition")
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_system")
            c.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01', true)")
            reaped = c.execute("SELECT reap_stuck_sends(interval '2 minutes')").fetchone()[0]
        rows.append(("Worker crashed mid-send (SENDING for 10 min): reaper moves it to UNKNOWN, not PENDING",
                     f"{reaped} reaped, now {state(n3)[0]}", "1 reaped, now UNKNOWN"))
        try:
            c.execute("UPDATE notifications SET unknown_resend_count = 2 WHERE notification_id=%s", (n3,))
            r = "allowed"
        except psycopg.errors.CheckViolation:
            r = "DENIED"
        rows.append(("Set unknown_resend_count to 2", r, "DENIED"))
        worker(n3, "SENDING")
        try:
            c.execute("UPDATE notifications SET unknown_resend_count = 0 WHERE notification_id=%s", (n3,))
            r = "allowed"
        except psycopg.Error as e:
            r = "DENIED" if "NOTIFICATION_" in str(e) else f"error {e.sqlstate}"
        rows.append(("Reset the resend counter to 0 to allow another resend", r, "DENIED"))
    return rows


def main():
    t0 = time.time()
    log("# Schema validation and integrity proof (generated)")
    log("")
    with psycopg.connect(DSN) as vc:
        ver = vc.execute("SHOW server_version").fetchone()[0]
    log(f"Run {datetime.now():%Y-%m-%d %H:%M} against a local PostgreSQL {ver}. Source: `validate_schema.py`. "
        "Production target is Aurora PostgreSQL 18.3+ (decision T33); the same script must pass there through the Data API (gate G1).")
    log("")
    log("## 1. Schema and snapshot load")
    log("")
    fresh_db()
    with psycopg.connect(DSN, autocommit=True) as c:
        load_snapshot(c)
    log("## 2. Same-slot contention")
    log("")
    for n, rounds in ((10, 30), (50, 20), (200, 10)):
        wins, lost, other = contention_rounds(n, rounds)
        ok = all(w == 1 for w in wins)
        log(f"- **{n} parallel holds × {rounds} rounds:** winners per round = {sorted(set(wins))}, losers = {lost}, other errors = {len(other)} → "
            f"{'**PASS: exactly one winner every round**' if ok else '**FAIL**'}")
    log("")
    log("## 3. Overlapping windows of different lengths")
    log("")
    won, overlaps = overlap_storm(150)
    log(f"- 150 parallel holds, random start (09:00–13:00) and length (55–100 min) on one dock: {won} granted, "
        f"**{overlaps} overlapping active occupancies** → {'**PASS**' if overlaps == 0 else '**FAIL**'}")
    log("")
    log("## 4. Rule and audit guards")
    log("")
    log("| Attempt | Result |")
    log("|---|---|")
    for label, res in rule_checks():
        log(f"| {label} | {res} |")
    log("")
    log("## 5. Row-level security: every facility-owned table, Data API transaction pattern")
    log("")
    log("Reads show rows visible. Write attempts show 1 when RLS blocked the write.")
    log("")
    log("| Caller and attempt | Got | Expected | Result |")
    log("|---|---:|---:|---|")
    for label, got, exp in rls_checks():
        log(f"| {label} | {got} | {exp} | {'PASS' if got == exp else '**FAIL**'} |")
    log("")
    log("Tables without RLS, each with a stated reason (the coverage check fails on any table missing from this list):")
    log("")
    for t, why in NO_RLS_ALLOWLIST.items():
        log(f"- `{t}`: {why}")
    log("")
    log("### 5b. TMS / WMS mirror tables (closes B-RLS-2) and dependent access")
    log("")
    log("| Caller and attempt | Got | Expected | Result |")
    log("|---|---:|---:|---|")
    for label, got, exp in mirror_rls_checks():
        log(f"| {label} | {got} | {exp} | {'PASS' if got == exp else '**FAIL**'} |")
    log("")
    log("## 6. Hold expiry and notification de-duplication")
    log("")
    log("| Check | Result |")
    log("|---|---|")
    for label, res in lifecycle_checks():
        log(f"| {label} | {res} |")
    log("")
    log("## 7. Least privilege: grants limit operations, RLS limits rows")
    log("")
    log("1 = the database refused for lack of privilege; 0 = allowed. Denials caused by RLS are not counted here.")
    log("")
    log("| Role and attempt | Got | Expected | Result |")
    log("|---|---:|---:|---|")
    for label, got, exp in privilege_checks():
        log(f"| {label} | {got} | {exp} | {'PASS' if got == exp else '**FAIL**'} |")
    log("")
    log("## 8. Notification recovery resend is enforced once (gate G2, database part)")
    log("")
    log("| Scenario | Got | Expected | Result |")
    log("|---|---|---|---|")
    for label, got, exp in notification_checks():
        log(f"| {label} | {got} | {exp} | {'PASS' if got == exp else '**FAIL**'} |")
    log("")
    log(f"_Total runtime {time.time() - t0:.1f} s._")
    OUT.write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
