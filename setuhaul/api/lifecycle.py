"""27 Nov fix plan (migration 0048): the console actions and reads for

  Workstream A · Operations  the ops_lead Controls tab, the appointment lifecycle (gate, dock, completed), the automatic
                             no-show and its audited backfill, and the dispatcher's decision after a no-show (D1–D3);
  Workstream B · Dispatch    free drivers, an explicit offer target, a planner-approved new time for a confirmed booking,
                             and open escalations where the planner decides.

Every action is checked twice: here by role (the matrices below, which /console/me reports), and again by the database
(role, facility scope, state, idempotency), which also writes the audit row. The planner approval workflow is not
touched: a dispatcher's new time becomes an ordinary PLANNER_REVIEW request that decide_booking_request decides.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import psycopg

from .security import Principal

log = logging.getLogger("setuhaul.lifecycle")

# D1: operations and ops_lead record gate, dock and completion; the dispatcher and the planner do not.
# D3: after a no-show the dispatcher (or operations / ops_lead, who hold the dispatch actions) decides.
LIFECYCLE_PERMISSIONS: dict[str, frozenset] = {
    "record_arrival":     frozenset({"operations", "ops_lead"}),
    "record_dock":        frozenset({"operations", "ops_lead"}),
    "record_completion":  frozenset({"operations", "ops_lead"}),
    "view_arrivals":      frozenset({"operations", "ops_lead", "planner"}),
    "resolve_no_show":    frozenset({"dispatcher", "operations", "ops_lead"}),
    # Workstream B
    "request_reschedule": frozenset({"dispatcher", "operations", "ops_lead"}),
    "view_free_drivers":  frozenset({"dispatcher", "operations", "ops_lead"}),
    # The ops_lead Controls tab (issue 1): the reads behind it. The writes keep their existing permissions
    # (booking_kill_switch, booking_hold, pause: ops_lead only).
    "view_controls":      frozenset({"ops_lead"}),
}

# Plain sentences for the refusals the new actions can produce (issue 2: never a raw error code on screen).
REFUSALS = {
    "ROLE_NOT_ALLOWED": "Your role cannot do this.",
    "FORBIDDEN": "Your role cannot do this.",
    "ALREADY_NO_SHOW": "The grace period had passed and the booking became a no-show. The dispatcher decides next.",
    "GRACE_PASSED": "The grace period has passed; the arrival cannot be recorded. The dispatcher rebooks.",
    "NOT_ARRIVABLE": "Only a confirmed booking can be marked as arrived.",
    "SHIPMENT_NOT_ARRIVABLE": "This shipment is already in a dock, completed or cancelled.",
    "NOT_DOCKABLE": "Only a confirmed booking whose truck has arrived can be marked at dock.",
    "NOT_ARRIVED": "Record the gate arrival first.",
    "NOT_IN_DOCK": "Record the truck at the dock first.",
    "NOT_A_NO_SHOW": "This booking is not a no-show.",
    "ALREADY_RESOLVED": "A decision was already made for this no-show.",
    "NO_SHOW_UNRESOLVED": "This shipment had a no-show: the dispatcher decides first (Rebook or Request cancellation).",
    "NOT_RESCHEDULABLE": "Only a confirmed booking can get a new time.",
    "APPOINTMENT_STARTED": "The booking has already started.",
    "ALREADY_ARRIVED": "The truck has already arrived for this booking.",
    "SAME_SLOT": "That is the booking's current dock and time.",
    "TIME_CHANGE_PENDING": "A request for this shipment is already waiting for a planner.",
    "CANCELLATION_PENDING": "A cancellation is waiting for a decision.",
    "REASSIGNMENT_IN_PROGRESS": "An offer to a driver is still open for this shipment.",
    "SLOT_TAKEN": "That dock and time was just taken. Reload the options.",
    "SLOT_TOO_LATE": "Too close to the start for a planner to decide.",
    "NO_CONFIRMER_AVAILABLE": "No planner is on shift to decide right now.",
    "OPERATIONAL_HOLD": "Bookings at this facility or dock are on hold.",
    "AUTOMATION_PAUSED": "Automation is paused at this facility.",
    "BOOKING_KILLED": "The booking kill switch is engaged.",
    "TARGET_MISMATCH": '"This driver" must be the assigned driver.',
    "SAME_DRIVER": '"Another driver" cannot be the assigned driver.',
    "OPTION_NOT_FEASIBLE": "That dock and time is not feasible now. Reload the options.",
    "DRIVER_HAS_OVERLAPPING_TRIP": "That driver has another trip at an overlapping time.",
}
LIFECYCLE_CONFLICTS = {"ALREADY_NO_SHOW", "GRACE_PASSED", "NOT_ARRIVABLE", "SHIPMENT_NOT_ARRIVABLE", "NOT_DOCKABLE",
                       "NOT_ARRIVED", "NOT_IN_DOCK", "NOT_A_NO_SHOW", "ALREADY_RESOLVED", "NO_SHOW_UNRESOLVED",
                       "NOT_RESCHEDULABLE", "ALREADY_ARRIVED", "SAME_SLOT", "TIME_CHANGE_PENDING", "SLOT_TAKEN",
                       "SLOT_TOO_LATE", "NO_CONFIRMER_AVAILABLE", "OPERATIONAL_HOLD", "AUTOMATION_PAUSED",
                       "TARGET_MISMATCH", "BOOKING_KILLED", "CAPACITY_LOST"}


def refusal_text(code: str) -> str:
    return REFUSALS.get(code, "The action was refused.")


class LifecycleMixin:
    """Mixed into BookingService (service.py): uses its db, require(), _staff_fn() and _dispatch_engine()."""

    # ------------------------------------------------------------------ Workstream A · lifecycle steps
    def record_arrival(self, p: Principal, appointment_id: str, note: str | None = None) -> dict:
        self.require(p, "record_arrival")
        return self._staff_fn(p, "SELECT record_gate_arrival(%s,%s,%s)", (appointment_id, f"staff:{p.sub}", note))

    def record_dock(self, p: Principal, appointment_id: str, note: str | None = None) -> dict:
        self.require(p, "record_dock")
        return self._staff_fn(p, "SELECT record_dock_start(%s,%s,%s)", (appointment_id, f"staff:{p.sub}", note))

    def record_completion(self, p: Principal, appointment_id: str, note: str | None = None) -> dict:
        self.require(p, "record_completion")
        out = self._staff_fn(p, "SELECT record_completion(%s,%s,%s)", (appointment_id, f"staff:{p.sub}", note))
        from ..notify.kick import kick_outbound
        kick_outbound("completion")                          # "Unloading complete" goes out now, not on the next tick
        return out

    def resolve_no_show(self, p: Principal, appointment_id: str, note: str) -> dict:
        """D3: Rebook. The shipment returns to Needs booking; a planner approves the new booking as today."""
        self.require(p, "resolve_no_show")
        return self._staff_fn(p, "SELECT resolve_no_show(%s,'REBOOK',%s,%s)", (appointment_id, f"staff:{p.sub}", note))

    def process_no_shows(self, limit: int = 200) -> dict:
        """D2, the recovery worker every minute (system role): appointments with no gate arrival by start + the
        facility's grace become NO_SHOW. Only deadlines after the 0048 cutover; history goes through the backfill."""
        with self.db.tx("system", (*self.facilities,)) as c:
            return c.execute("SELECT process_no_shows(%s)", (limit,)).fetchone()[0]

    def backfill_no_shows(self, dry_run: bool, principal: str, reason: str, reviewed_run: str | None = None) -> dict:
        """Q1, owner-run (scripts/no_show_backfill.py): the dry-run report first; apply only what was reviewed."""
        with self.db.tx("system", (*self.facilities,)) as c:
            return c.execute("SELECT backfill_no_shows(%s,%s,%s,%s)", (dry_run, principal, reason, reviewed_run)).fetchone()[0]

    def arrivals(self, p: Principal, facility: str, hours: int = 24) -> dict:
        """The Arrivals list (Ops health and Schedule): bookings from 2 h ago to `hours` ahead, each with its state,
        its no-show deadline (start + this facility's grace) and the next step the caller may record. A driver's
        message that says they have reached is shown as a prompt, never recorded as an arrival by itself."""
        self.require(p, "view_arrivals")
        if facility not in p.facilities:
            from .service import ServiceError
            raise ServiceError(403, "FORBIDDEN", "facility outside your scope")
        now = datetime.now(timezone.utc)
        lo, hi = now - timedelta(hours=2), now + timedelta(hours=max(1, min(hours, 48)))
        with self.db.tx("console", (facility,), readonly=True) as c:
            grace = c.execute("SELECT no_show_grace_min FROM facility_config WHERE facility_id=%s AND active",
                              (facility,)).fetchone()
            grace = grace[0] if grace else None
            rows = c.execute("""
                SELECT a.appointment_id, a.shipment_id, s.order_reference, s.driver_id, k.dock_code, lower(a.during),
                       upper(a.during), a.status, a.arrived_at, a.docked_at, a.completed_at, a.no_show_at,
                       a.no_show_resolution, s.current_status, no_show_deadline(lower(a.during), %s),
                       (SELECT m.body_masked FROM chat_threads t JOIN chat_messages m ON m.thread_id = t.thread_id
                         WHERE t.shipment_id = a.shipment_id AND m.sender_type = 'DRIVER'
                           AND m.created_at > now() - interval '2 hours'
                         ORDER BY m.created_at DESC LIMIT 1),
                       (SELECT m.created_at FROM chat_threads t JOIN chat_messages m ON m.thread_id = t.thread_id
                         WHERE t.shipment_id = a.shipment_id AND m.sender_type = 'DRIVER'
                           AND m.created_at > now() - interval '2 hours'
                         ORDER BY m.created_at DESC LIMIT 1)
                  FROM appointments a JOIN docks k ON k.dock_id = a.dock_id
                  JOIN shipments s ON s.shipment_id = a.shipment_id
                 WHERE k.facility_id = %s AND a.during && tstzrange(%s, %s, '[)')
                   AND a.status IN ('CONFIRMED','IN_PROGRESS','COMPLETED','NO_SHOW')
                   AND (a.is_current OR a.status IN ('COMPLETED','NO_SHOW'))
                 ORDER BY lower(a.during), a.appointment_id LIMIT 200""", (grace, facility, lo, hi)).fetchall()
        from ..agent.rules_extractor import arrived_now
        can = {a: bool(p.roles & LIFECYCLE_PERMISSIONS[a]) for a in ("record_arrival", "record_dock", "record_completion")}
        out = []
        for r in rows:
            status, arrived, deadline = r[7], r[8], r[14]
            if status == "CONFIRMED" and arrived is None:
                nxt = "ARRIVED_AT_GATE" if deadline is None or now <= deadline else "AWAITING_NO_SHOW"
            elif status == "CONFIRMED":
                nxt = "AT_DOCK"
            elif status == "IN_PROGRESS":
                nxt = "COMPLETED"
            else:
                nxt = None
            action = {"ARRIVED_AT_GATE": "record_arrival", "AT_DOCK": "record_dock", "COMPLETED": "record_completion"}.get(nxt)
            said = r[15] if status == "CONFIRMED" and arrived is None and r[15] and arrived_now(r[15]) else None
            out.append({"appointment_id": r[0], "shipment_id": r[1], "order_reference": r[2], "driver_id": r[3],
                        "dock_code": r[4], "start": r[5].isoformat(), "end": r[6].isoformat(), "status": status,
                        "shipment_status": r[13], "arrived_at": _iso(arrived), "docked_at": _iso(r[9]),
                        "completed_at": _iso(r[10]), "no_show_at": _iso(r[11]), "no_show_resolution": r[12],
                        "no_show_deadline": _iso(deadline), "next_step": nxt,
                        "you_can_record": bool(action and can[action]),
                        "driver_says_arrived": {"at": _iso(r[16]), "text": said[:160]} if said else None})
        return {"facility_id": facility, "as_of": now.isoformat(), "no_show_grace_min": grace,
                "rule": "no gate arrival by start + grace -> automatic no-show (checked every minute)",
                "who_records": "operations or ops lead", "arrivals": out}

    # ------------------------------------------------------------------ Workstream A · the Controls tab (issue 1)
    def list_holds(self, p: Principal, facility: str | None = None, include_cleared: bool = False) -> list[dict]:
        self.require(p, "view_controls")
        with self.db.tx("console", self._scope(p, facility), readonly=True) as c:
            rows = c.execute("""SELECT h.hold_id, h.facility_id, h.dock_id, k.dock_code, h.reason, h.set_by, h.set_at,
                                       h.cleared_by, h.cleared_at
                                  FROM booking_holds h LEFT JOIN docks k ON k.dock_id = h.dock_id
                                 WHERE %s OR h.cleared_at IS NULL
                                 ORDER BY h.cleared_at IS NOT NULL, h.set_at DESC LIMIT 100""", (include_cleared,)).fetchall()
        return [{"hold_id": r[0], "facility_id": r[1], "dock_id": r[2], "dock_code": r[3], "scope": "dock" if r[2] else "facility",
                 "reason": r[4], "set_by": r[5], "set_at": _iso(r[6]), "cleared_by": r[7], "cleared_at": _iso(r[8]),
                 "open": r[8] is None} for r in rows]

    def controls(self, p: Principal) -> dict:
        """Everything the ops_lead Controls tab shows: the kill switch (state, who changed it last and why), the
        facilities' automation pause (and who paused it), and the open booking holds. Read only."""
        self.require(p, "view_controls")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            ks = c.execute("SELECT kill_switch_detail()").fetchone()[0]
            facs = c.execute("SELECT facility_controls(%s)", (list(p.facilities),)).fetchone()[0]
        return {"kill_switch": ks, "facilities": facs, "holds": self.list_holds(p),
                "booking_enabled": self.booking_enabled,
                "not_here": "Revoking a release's provisional authorization stays a command-line action."}

    # ------------------------------------------------------------------ Workstream B · dispatch
    def free_drivers(self, p: Principal, shipment_id: str, start: datetime, end: datetime) -> dict:
        """Issue 4: drivers free for this slot (same carrier, active, not at a gate or in a dock, no overlapping live
        booking within 2 h, no live offer for another shipment). The assigned driver is never listed."""
        self.require(p, "view_free_drivers")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            row = c.execute("SELECT driver_id FROM shipments WHERE shipment_id=%s", (shipment_id,)).fetchone()
            if not row:
                from .service import ServiceError
                raise ServiceError(404, "NOT_FOUND", "no such shipment")
            try:
                drivers = c.execute("SELECT free_drivers_for(%s,%s,%s)", (shipment_id, start, end)).fetchone()[0]
            except psycopg.Error as e:
                from .service import ServiceError
                raise ServiceError(400, (e.diag.message_primary or "ERROR").split(":")[0]) from e
        return {"shipment_id": shipment_id, "assigned_driver_id": row[0], "start": start.isoformat(),
                "end": end.isoformat(), "drivers": drivers,
                "rule": "free = active, same carrier, not at a gate or in a dock, no live booking within 2 h of this "
                        "slot (completed and no-show trips no longer count), no open offer for another shipment"}

    def reschedule_options(self, p: Principal, appointment_id: str) -> dict:
        """Issue 6: feasible slots for a CONFIRMED booking before its start (the same engine as every offer). A
        recommendation only: the request goes to a planner, and the booking stays as it is until approved."""
        self.require(p, "request_reschedule")
        appt = self._confirmed_appointment(p, appointment_id)
        (fac, ref, drv, veh), res, sv, _ = self._dispatch_engine(p, appt["shipment_id"])
        opts = [o for o in res.options if not (o.dock == appt["dock_code"] and o.start == appt["start_dt"])]
        from .service import human_time
        return {"appointment_id": appointment_id, "shipment_id": appt["shipment_id"], "order_reference": ref,
                "current": {"dock_code": appt["dock_code"], "start": appt["start_dt"].isoformat(),
                            "start_label": human_time(appt["start_dt"])},
                "outcome": res.outcome_code, "schedule_version": sv,
                "options": [{"dock_code": o.dock, "start": o.start.isoformat(), "end": o.end.isoformat(),
                             "start_label": human_time(o.start)} for o in opts],
                "note": "A planner decides every new time (always). The booking stays confirmed until then."}

    def request_reschedule(self, p: Principal, appointment_id: str, dock_code: str, start: datetime, key: str,
                           note: str) -> dict:
        self.require(p, "request_reschedule")
        appt = self._confirmed_appointment(p, appointment_id)
        _, res, _, docks = self._dispatch_engine(p, appt["shipment_id"])
        hit = next((o for o in res.options if o.dock == dock_code and o.start == start), None)
        if hit is None:
            from .service import ServiceError
            raise ServiceError(409, "OPTION_NOT_FEASIBLE", refusal_text("OPTION_NOT_FEASIBLE"))
        out = self._staff_fn(p, "SELECT request_reschedule(%s,%s,%s,%s,%s,%s,%s)",
                             (appointment_id, docks[hit.dock], hit.start, hit.end, key, f"staff:{p.sub}", note))
        return out

    def _confirmed_appointment(self, p: Principal, appointment_id: str) -> dict:
        with self.db.tx("console", p.facilities, readonly=True) as c:
            r = c.execute("""SELECT a.shipment_id, k.dock_code, lower(a.during), a.status, a.is_current
                               FROM appointments a JOIN docks k ON k.dock_id = a.dock_id
                              WHERE a.appointment_id=%s""", (appointment_id,)).fetchone()
        from .service import ServiceError
        if not r:
            raise ServiceError(404, "APPOINTMENT_UNKNOWN")
        if r[3] != "CONFIRMED" or not r[4]:
            raise ServiceError(409, "NOT_RESCHEDULABLE", refusal_text("NOT_RESCHEDULABLE"))
        return {"shipment_id": r[0], "dock_code": r[1], "start_dt": r[2]}


def _iso(v):
    return v.isoformat() if v is not None else None
