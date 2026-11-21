"""Load the challenge snapshot (SQLite, read-only) into the engine's `FacilityData` view."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..domain.feasibility import FacilityData, FacilityTimings

SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "setuhaul_freight_operations.db"

# Phase 1 §6a seed timings per facility (facility_config in PostgreSQL in the service).
SEED_TIMINGS = {
    "FAC-JAI-01": FacilityTimings(gate_to_dock_min=10, changeover_min=10),
    "FAC-GGN-01": FacilityTimings(gate_to_dock_min=15, changeover_min=10),
}


def load(path: Path = SNAPSHOT, timings: dict | None = None) -> FacilityData:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda sql: [dict(r) for r in con.execute(sql)]
    rules: dict = {}
    for r in q("SELECT * FROM facility_rules WHERE active_flag = 1"):
        rules.setdefault(r["facility_id"], {})[r["rule_type"]] = r["rule_value"]
    data = FacilityData(
        shipments={r["shipment_id"]: r for r in q("SELECT * FROM shipments")},
        docks={r["dock_id"]: r for r in q("SELECT * FROM docks")},
        facilities={r["facility_id"]: r for r in q("SELECT * FROM facilities")},
        slots=q("SELECT * FROM appointment_slots ORDER BY dock_id, slot_start_ts"),
        appts=q("SELECT * FROM appointments"),
        events=q("SELECT * FROM dock_status_events"),
        checkins={r["shipment_id"]: r for r in q("SELECT * FROM facility_checkins")},
        eta_updates=q("SELECT * FROM eta_updates"),
        rules=rules,
        timings=timings or SEED_TIMINGS,
    )
    con.close()
    return data
