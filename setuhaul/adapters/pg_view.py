"""Build the feasibility engine's `FacilityData` from PostgreSQL.

Called by the booking service, not by the agent. It runs in its own read-only transaction as the system role,
scoped to the facilities involved, because a correct answer needs facts the driver must never see (other
drivers' shipments for double-assignment checks, other trucks' holds). Only the driver's own options leave
the service.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..domain.eta import Signal
from ..domain.feasibility import FacilityData, FacilityTimings

HUMAN_SOURCES = ("OPERATIONS_OVERRIDE", "WAREHOUSE_ESTIMATE", "DRIVER_DECLARED")
EVIDENCE = ("TELEMATICS", "LOCATION_PIN", "SIM_PING", "FASTAG")
QUEUE = {"IN_DOCK": "IN_DOCK", "COMPLETED": "COMPLETED", "AT_GATE": "WAITING", "WAITING": "WAITING"}


def _rows(c, sql, params=()):
    cur = c.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_view(c, now: datetime, horizon_h: int = 36) -> FacilityData:
    """`c` is a connection already inside a transaction with role and scope set (see db.Db.tx)."""
    lo, hi = now - timedelta(hours=horizon_h), now + timedelta(hours=horizon_h)
    shipments = {r["shipment_id"]: dict(r, latest_eta_ts=None) for r in _rows(c, "SELECT * FROM shipments")}
    docks = {r["dock_id"]: r for r in _rows(c, "SELECT * FROM docks")}
    cfg = {r["facility_id"]: r for r in _rows(c, "SELECT * FROM facility_config WHERE active")}
    facilities = {}
    for f in _rows(c, "SELECT * FROM facilities"):
        k = cfg.get(f["facility_id"], {})
        facilities[f["facility_id"]] = dict(f, close_time=f["close_time"].strftime("%H:%M"),
                                            checkin_grace_min=k.get("no_show_grace_min", 30),
                                            shifts=k.get("shifts") or [])            # Phase G: labour crews per shift
    rules = {fid: {k: v for k, v in {
        "HEAVY_DOCK_REQUIRED_KG": r["heavy_dock_required_kg"],
        "LAST_NEW_START_TIME": r["last_new_start_local"].strftime("%H:%M") if r["last_new_start_local"] else None,
        "NO_SHOW_GRACE_MIN": r["no_show_grace_min"]}.items() if v is not None} for fid, r in cfg.items()}
    timings = {fid: FacilityTimings(r["gate_to_dock_min"], r["changeover_min"]) for fid, r in cfg.items()}
    slots = _rows(c, """SELECT s.slot_id, s.dock_id, d.facility_id, lower(s.slot) AS slot_start_ts, upper(s.slot) AS slot_end_ts,
                               s.slot_status, s.block_reason
                          FROM appointment_slots s JOIN docks d USING (dock_id)
                         WHERE s.slot && tstzrange(%s, %s) ORDER BY s.dock_id, lower(s.slot)""", (lo, hi))
    appts = _rows(c, """SELECT appointment_id, shipment_id, dock_id, lower(during) AS start, upper(during) AS "end",
                               status AS appointment_status, is_current
                          FROM appointments WHERE status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""")
    events = _rows(c, """SELECT dock_event_id, dock_id, event_type, lower(during) AS event_start_ts, upper(during) AS event_end_ts
                           FROM dock_events WHERE during && tstzrange(%s, %s)""", (lo, hi))
    occupancy = _rows(c, """SELECT dock_id, kind, shipment_id, ref_id, expires_at, lower(during) AS start, upper(during) AS "end"
                              FROM dock_occupancy WHERE active AND during && tstzrange(%s, %s)""", (lo, hi))
    signals = _rows(c, "SELECT * FROM eta_signals ORDER BY shipment_id, observed_at")
    eta_updates, extra, gate = [], {}, {}
    for s in signals:
        if s["source"] in HUMAN_SOURCES:
            eta_updates.append({"shipment_id": s["shipment_id"], "source_type": s["source"], "created_at": s["observed_at"],
                                "declared_eta_ts": s["derived_eta"], "confidence_code": s["confidence"]})
        elif s["source"] in EVIDENCE:
            extra.setdefault(s["shipment_id"], []).append(
                Signal(s["source"], s["observed_at"], s["derived_eta"], s["uncertainty_min"], None))
        elif s["source"] == "GATE_IN":
            gate[s["shipment_id"]] = s["observed_at"]
    checkins = {}
    for sid, sh in shipments.items():
        if sid in gate or sh["current_status"] in QUEUE:
            checkins[sid] = {"gate_in_ts": gate.get(sid), "gate_out_ts": None, "unload_start_ts": None,
                             "queue_state": QUEUE.get(sh["current_status"], "WAITING")}
    return FacilityData(shipments=shipments, docks=docks, facilities=facilities, slots=slots, appts=appts, events=events,
                        checkins=checkins, eta_updates=eta_updates, rules=rules, timings=timings, extra_signals=extra,
                        occupancy=occupancy)
