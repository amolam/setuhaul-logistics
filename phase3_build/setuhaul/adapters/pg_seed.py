"""Load the challenge snapshot into PostgreSQL through the real constraints (test and demo data).

Port of the Phase 2 validator's loader, extended for Build: it also writes `appointments`, ETA signals
(warehouse / driver / operations updates and gate-ins) and chat threads. Runs as the migration owner, because
seeding is a deployment step, not an application action. Rows the database refuses (the four known-bad seed
bookings) are returned, never forced in.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.types.range import Range

from .sqlite_snapshot import SNAPSHOT

CHANGEOVER = 10
FACILITY_CONFIG = """INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
  checkin_early_limit_min,heavy_dock_required_kg,last_new_start_local,warehouse_confirm_ttl_min,shifts) VALUES
  ('FAC-JAI-01',1,10,10,30,60,25000,'21:00','{"DAY":15,"NIGHT":10}','[{"name":"DAY","start":"06:00","end":"14:00"},{"name":"NIGHT","start":"14:00","end":"22:00"}]'),
  ('FAC-GGN-01',1,15,10,20,NULL,NULL,NULL,'{"DAY":15,"NIGHT":0}','[{"name":"DAY","start":"07:00","end":"15:00"},{"name":"NIGHT","start":"15:00","end":"21:00"}]')"""


def ts(s):
    return datetime.fromisoformat(s) if s else None


def rng(a, b):
    return Range(a, b, "[)")


def seed(dsn: str, path: Path = SNAPSHOT) -> dict:
    s = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    s.row_factory = sqlite3.Row
    q = lambda sql: [dict(r) for r in s.execute(sql)]
    rejected: list[tuple[str, str]] = []
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")

        def attempt(label, sql, params):
            try:
                with c.transaction():
                    return c.execute(sql, params).fetchone() if "RETURNING" in sql else c.execute(sql, params)
            except psycopg.Error as e:
                rejected.append((label, (e.diag.message_primary or str(e)).split("\n")[0]))
                return None

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
        c.execute(FACILITY_CONFIG)
        for r in q("SELECT * FROM docks"):
            c.execute("INSERT INTO docks VALUES (%s,%s,%s,%s,%s,%s,%s)", (r["dock_id"], r["facility_id"], r["dock_code"], r["dock_type"],
                      bool(r["supports_refrigerated"]), r["max_vehicle_weight_kg"], 90 if r["dock_type"] == "HEAVY" else 60))
        shp = {r["shipment_id"]: r for r in q("SELECT * FROM shipments")}
        for r in shp.values():
            c.execute("INSERT INTO shipments VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (r["shipment_id"], r["order_reference"], r["carrier_id"], r["driver_id"], r["vehicle_id"], r["destination_facility_id"],
                       r["load_weight_kg"], r["required_dock_type"], bool(r["temperature_control_required"]), r["priority_code"],
                       r["expected_unload_min"], r["planned_departure_ts"], r["actual_departure_ts"], r["original_eta_ts"],
                       r["current_status"], r["updated_at"]))
        for r in q("SELECT * FROM appointment_slots"):
            c.execute("INSERT INTO appointment_slots VALUES (%s,%s,%s,%s,%s)", (r["slot_id"], r["dock_id"],
                      rng(ts(r["slot_start_ts"]), ts(r["slot_end_ts"])), r["slot_status"], r["block_reason"]))
        for e in q("SELECT * FROM dock_status_events"):
            c.execute("INSERT INTO dock_events VALUES (%s,%s,%s,%s,%s,%s,%s)", (e["dock_event_id"], e["dock_id"], e["event_type"],
                      rng(ts(e["event_start_ts"]), ts(e["event_end_ts"])), e["reason"], "loader", e["created_at"]))
            if e["event_type"] in ("BREAKDOWN", "MAINTENANCE"):          # same rule as the Phase 2 validator
                fac = c.execute("SELECT facility_id FROM docks WHERE dock_id=%s", (e["dock_id"],)).fetchone()[0]
                attempt(f"BLOCK {e['dock_event_id']}",
                        "INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,ref_id,created_by) VALUES (%s,%s,%s,'BLOCK',%s,'loader')",
                        (e["dock_id"], fac, rng(ts(e["event_start_ts"]), ts(e["event_end_ts"])), e["dock_event_id"]))
        # trucks physically in dock
        for k in q("SELECT * FROM facility_checkins WHERE queue_state='IN_DOCK'"):
            s0 = ts(k["unload_start_ts"])
            e0 = s0 + timedelta(minutes=shp[k["shipment_id"]]["expected_unload_min"] + CHANGEOVER)
            attempt(f"FIXED {k['shipment_id']}",
                    "INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by) "
                    "VALUES (%s,%s,%s,'FIXED',%s,%s,'loader')",
                    (k["actual_dock_id"], k["facility_id"], rng(s0, e0), k["shipment_id"], k["checkin_id"]))
        # gate-ins become signals (ground truth for arrival)
        for k in q("SELECT * FROM facility_checkins WHERE gate_in_ts IS NOT NULL"):
            c.execute("""INSERT INTO eta_signals (shipment_id,source,observed_at,derived_eta,confidence,raw_ref)
                         VALUES (%s,'GATE_IN',%s,%s,'HIGH',%s)""", (k["shipment_id"], k["gate_in_ts"], k["gate_in_ts"], k["checkin_id"]))
        # bookings, in the order they were made: occupancy first (the constraints decide), then the appointment row
        appts = q("""SELECT a.*, sl.dock_id, sl.facility_id, sl.slot_start_ts, sl.slot_end_ts FROM appointments a
                     JOIN appointment_slots sl USING (slot_id)
                     WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') ORDER BY a.booked_at""")
        for a in appts:
            s0 = ts(a["slot_start_ts"])
            occ_id = None
            if a["appointment_status"] == "IN_PROGRESS":
                row = c.execute("SELECT occupancy_id FROM dock_occupancy WHERE kind='FIXED' AND shipment_id=%s", (a["shipment_id"],)).fetchone()
                occ_id = row[0] if row else None
            else:
                e0 = max(ts(a["slot_end_ts"]), s0 + timedelta(minutes=shp[a["shipment_id"]]["expected_unload_min"] + CHANGEOVER))
                row = attempt(a["appointment_id"],
                              "INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by) "
                              "VALUES (%s,%s,%s,'APPOINTMENT',%s,%s,'loader') RETURNING occupancy_id",
                              (a["dock_id"], a["facility_id"], rng(s0, e0), a["shipment_id"], a["appointment_id"]))
                if row is None:
                    continue                                     # refused by the database: not a real booking
                occ_id = row[0]
            pend = ts(a["booked_at"]) + timedelta(minutes=15) if a["appointment_status"] == "PENDING_CONFIRMATION" else None
            # B-WF-2 (migration 0015): a pending appointment always has its OPEN approval request, in one statement
            attempt(f"APPT {a['appointment_id']}",
                    """WITH ap AS (
                         INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,occupancy_id,status,is_current,
                                                   booking_source,pending_expires_at,created_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,'PLANNER',%s,%s) RETURNING appointment_id, shipment_id, status)
                       INSERT INTO approval_requests (approval_id,kind,facility_id,shipment_id,proposed_action,evidence,status,
                                                      expires_at,appointment_id,claim_occupancy_id)
                       SELECT gen_random_uuid(),'STANDARD',%s,ap.shipment_id,'{"source":"planner"}','{"imported_from":"snapshot"}',
                              'OPEN',%s,ap.appointment_id,%s FROM ap WHERE ap.status = 'PENDING_CONFIRMATION'
                       RETURNING approval_id""",
                    (a["appointment_id"], a["shipment_id"], a["dock_id"], rng(s0, ts(a["slot_end_ts"])), occ_id,
                     a["appointment_status"], bool(a["is_current"]), pend, a["booked_at"], a["facility_id"], pend, occ_id))
        # ETA updates from people
        for e in q("SELECT * FROM eta_updates"):
            c.execute("""INSERT INTO eta_signals (shipment_id,source,observed_at,derived_eta,confidence,raw_ref)
                         VALUES (%s,%s,%s,%s,%s,%s)""",
                      (e["shipment_id"], e["source_type"], e["created_at"], e["declared_eta_ts"], e["confidence_code"], e["eta_update_id"]))
        for t in q("SELECT * FROM chat_threads"):
            c.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status,opened_at) VALUES (%s,%s,%s,'OPEN',%s)",
                      (t["thread_id"], t["driver_id"], t["shipment_id"], t["opened_at"]))
    s.close()
    return {"shipments": len(shp), "rejected": rejected}
