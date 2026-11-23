"""Phase G4: the evaluation / performance dataset, built from the TRAINER dataset with minimal changes.

    python -m setuhaul.adapters.phase_g_dataset "host=localhost port=55432 user=postgres dbname=setuhaul_phase_g"

Foundation: the trainer's SQLite snapshot (`data/setuhaul_freight_operations.db`, identical to the copy inside
`docs/brief/21182924-SetuHaul_Data_Package.zip`), loaded by the existing `pg_seed.seed` loader through every database
constraint. The only changes, all mechanical and recorded in the coverage report:

  1. Identity (synthetic marking, as the pilot seed requires): carrier, driver, vehicle, shipment, order, thread ids get
     the `SYN-` prefix; every driver phone becomes an ITU-reserved `+999` number; names, licences and registrations are
     replaced with synthetic labels. No real person or phone number is kept.
  2. Time: every timestamp moves by WHOLE days so the trainer's day (2026-08-04) becomes `target_day`. Whole days keep
     every time-of-day rule (opening hours, 21:00 last new start, shifts) exactly as the trainer wrote them.
  3. Assumptions the trainer data does not contain (labour crews per shift, facility capacity) come from
     `phase_g_assumptions.json` and are written into facility_config.shifts[*].crews; they are configurable.
  4. Extra hourly OPEN slots for the next days on every trainer dock (`extra_hourly_slot_days`), so load tests and
     evaluations run at any wall-clock time; they are marked `SYN-SLOT-...` and never replace a trainer slot.

Rows the database refuses (the trainer's known-bad bookings) are reported, never forced in.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from .pg_seed import seed
from .sqlite_snapshot import SNAPSHOT

ASSUMPTIONS_FILE = Path(__file__).resolve().parent / "phase_g_assumptions.json"
TRAINER_DAY = date(2026, 8, 4)
IST = timezone(timedelta(hours=5, minutes=30))
TS_COLUMNS = {                                              # every timestamp column of the trainer tables
    "shipments": ("planned_departure_ts", "actual_departure_ts", "original_eta_ts", "latest_eta_ts", "created_at",
                  "updated_at"),
    "appointment_slots": ("slot_start_ts", "slot_end_ts"),
    "appointments": ("booked_at", "confirmed_at", "cancelled_at", "updated_at"),
    "dock_status_events": ("event_start_ts", "event_end_ts", "created_at"),
    "eta_updates": ("declared_eta_ts", "created_at"),
    "facility_checkins": ("gate_in_ts", "yard_queue_enter_ts", "dock_in_ts", "unload_start_ts", "unload_end_ts",
                          "gate_out_ts", "updated_at"),
    "chat_threads": ("opened_at", "closed_at"),
    "chat_messages": ("message_ts", "extracted_eta_ts"),
    "driver_exceptions": ("reported_at", "declared_eta_ts", "earliest_acceptable_ts", "latest_acceptable_ts"),
    "operational_messages": ("sent_at",),
}
ID_COLUMNS = {"carrier_id", "driver_id", "vehicle_id", "shipment_id", "order_reference", "thread_id",
              "reported_by_driver_id"}                      # prefixed with SYN- everywhere (references stay consistent)


def load_assumptions(path: Path = ASSUMPTIONS_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cols(con, table) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def synthetic_snapshot(target_day: date, src: Path = SNAPSHOT, out_dir: Path | None = None) -> tuple[Path, dict]:
    """Copy the trainer snapshot and apply changes 1 and 2. Returns (path, per-table record counts)."""
    out_dir = out_dir or Path(tempfile.mkdtemp(prefix="setuhaul_phase_g_"))
    dst = out_dir / "phase_g_synthetic.db"
    shutil.copyfile(src, dst)
    shift = (target_day - TRAINER_DAY).days
    con = sqlite3.connect(dst)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    counts["appointments_active"] = con.execute("""SELECT count(*) FROM appointments WHERE appointment_status IN
                                                   ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""").fetchone()[0]
    con.execute("PRAGMA foreign_keys=OFF")
    for t in tables:
        cols = _cols(con, t)
        sets = [f"{c} = CASE WHEN {c} IS NULL OR {c} LIKE 'SYN-%' THEN {c} ELSE 'SYN-' || {c} END"
                for c in cols if c in ID_COLUMNS]
        for c in TS_COLUMNS.get(t, ()):
            if c in cols:                                   # ISO text: move the date part only, keep time and offset
                sets.append(f"{c} = CASE WHEN {c} IS NULL OR {c} = '' THEN {c} ELSE "
                            f"date(substr({c}, 1, 10), '{shift:+d} days') || substr({c}, 11) END")
        if sets:
            con.execute(f"UPDATE {t} SET {', '.join(sets)}")
    n = 0
    for (drv,) in con.execute("SELECT driver_id FROM drivers ORDER BY driver_id").fetchall():
        n += 1
        con.execute("UPDATE drivers SET phone = ?, driver_name = ?, licence_number = ? WHERE driver_id = ?",
                    (f"+999{n:011d}", f"Synthetic Driver {n}", f"SYN-LIC-{n:04d}", drv))
    if "vehicles" in tables:
        for i, (veh,) in enumerate(con.execute("SELECT vehicle_id FROM vehicles ORDER BY vehicle_id").fetchall(), 1):
            con.execute("UPDATE vehicles SET registration_number = ? WHERE vehicle_id = ?", (f"SYN-REG-{i:04d}", veh))
    if "carriers" in tables:
        con.execute("UPDATE carriers SET carrier_name = 'Synthetic ' || carrier_name WHERE carrier_name NOT LIKE 'Synthetic%'")
    con.commit()
    con.close()
    return dst, counts


def build(dsn: str, target_day: date | None = None, assumptions: dict | None = None) -> dict:
    """Seed `dsn` (an empty, migrated database) with the Phase G dataset. Returns the coverage report."""
    a = assumptions or load_assumptions()
    target_day = target_day or datetime.now(IST).date()
    path, trainer_counts = synthetic_snapshot(target_day)
    res = seed(dsn, path)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        for fac, shifts in a["labour_shifts"].items():
            c.execute("UPDATE facility_config SET shifts = %s WHERE facility_id = %s AND active", (Jsonb(shifts), fac))
        start_h, end_h = a["extra_slot_hours"]["start"], a["extra_slot_hours"]["end"]
        docks = [r[0] for r in c.execute("SELECT dock_id FROM docks ORDER BY dock_id")]
        added = 0
        for d in range(0, int(a["extra_hourly_slot_days"]) + 1):
            day = target_day + timedelta(days=d)
            for dock in docks:
                for h in range(start_h, end_h):
                    s = datetime(day.year, day.month, day.day, h, tzinfo=IST)
                    cur = c.execute("""INSERT INTO appointment_slots
                                       SELECT %s, %s, tstzrange(%s, %s, '[)'), 'OPEN', NULL
                                        WHERE NOT EXISTS (SELECT 1 FROM appointment_slots
                                                           WHERE dock_id = %s AND slot && tstzrange(%s, %s, '[)'))""",
                                    (f"SYN-SLOT-{dock}-{s:%Y%m%d%H}", dock, s, s + timedelta(hours=1), dock, s,
                                     s + timedelta(hours=1)))
                    added += cur.rowcount
        loaded = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                  ("carriers", "drivers", "vehicles", "facilities", "docks", "shipments", "appointments", "dock_events",
                   "eta_signals", "chat_threads")}
        bad = c.execute("""SELECT count(*) FROM drivers WHERE driver_id NOT LIKE 'SYN-%' OR phone_e164 NOT LIKE '+999%'""").fetchone()[0]
    applicable = {"carriers": trainer_counts.get("carriers", 0), "drivers": trainer_counts.get("drivers", 0),
                  "vehicles": trainer_counts.get("vehicles", 0), "facilities": trainer_counts.get("facilities", 0),
                  "docks": trainer_counts.get("docks", 0), "shipments": trainer_counts.get("shipments", 0),
                  "appointments": trainer_counts.get("appointments_active", 0),   # history rows are not bookings
                  "dock_events": trainer_counts.get("dock_status_events", 0),
                  "chat_threads": trainer_counts.get("chat_threads", 0)}
    carried = {k: loaded[k] for k in applicable}
    total_app, total_car = sum(applicable.values()), sum(min(carried[k], v) for k, v in applicable.items())
    return {"target_day": target_day.isoformat(), "trainer_records": trainer_counts, "loaded": loaded,
            "applicable_carried": carried, "coverage_pct": round(100 * total_car / max(total_app, 1), 1),
            "refused_by_database": res["rejected"], "extra_synthetic_slots": added, "non_synthetic_identities": bad,
            "assumptions": {k: v for k, v in a.items() if not k.startswith("_")}}


def main(argv: list[str]) -> int:
    print(json.dumps(build(argv[1]), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
