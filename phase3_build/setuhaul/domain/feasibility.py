"""The single feasibility predicate (`find_options` / `is_feasible`), ported from the Phase 1 prototype.

Deterministic, no LLM. The engine reads a `FacilityData` view (loaded from the snapshot in tests and from
PostgreSQL in the service) and returns ranked options with plain-language rejection reasons.

Changes from `phase1_frame/feasibility_prototype.py`:
  * the effective ETA comes from `fuse_eta` (Phase 1 §3 rules) instead of a fixed source ranking;
  * facility timings come from `FacilityTimings` (per facility, versioned in `facility_config`), not constants;
  * `is_feasible(option)` re-checks one chosen option at confirmation time (decision 9, stale options).
The differential test `tests/test_feasibility.py` proves identical results to the prototype on the snapshot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .eta import EtaResult, FusionConfig, Signal, fuse_eta

ACTIVE = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
# S4-06 / T44: how far ahead the engine looks for the next operating day (a day with open published slots).
SEARCH_WINDOW_DAYS = 7
ETA_SOURCE_MAP = {"OPERATIONS_OVERRIDE": "OPERATIONS_OVERRIDE", "WAREHOUSE_ESTIMATE": "WAREHOUSE_ESTIMATE",
                  "DRIVER_DECLARED": "DRIVER_DECLARED"}


def ts(s) -> datetime | None:
    if not s:
        return None
    return s if isinstance(s, datetime) else datetime.fromisoformat(s)


def hm(d: datetime | None) -> str:
    return d.strftime("%H:%M") if d else "—"


@dataclass(frozen=True)
class FacilityTimings:
    gate_to_dock_min: int
    changeover_min: int
    eta_band: dict = field(default_factory=lambda: {"LOW": 30, "MEDIUM": 15, "HIGH": 0})


@dataclass
class FacilityData:
    """Plain rows, keyed like the snapshot. The service builds the same shape from PostgreSQL."""
    shipments: dict
    docks: dict
    facilities: dict
    slots: list
    appts: list
    events: list
    checkins: dict
    eta_updates: list
    rules: dict
    timings: dict                     # facility_id -> FacilityTimings
    extra_signals: dict = field(default_factory=dict)   # shipment_id -> [Signal] (FASTag, pins, telematics…)
    # PostgreSQL mode: every active claim on dock time (dock_occupancy rows). When set, committed work is read
    # from here instead of being re-derived from appointments, so holds by other drivers count as busy too.
    occupancy: list | None = None

    def __post_init__(self):
        self.slot_by_id = {s["slot_id"]: s for s in self.slots}


@dataclass(frozen=True)
class Option:
    dock: str
    start: datetime
    end: datetime
    slots: tuple[str, ...]
    needs_approval: bool


@dataclass
class Result:
    shipment_id: str
    now: datetime
    facts: dict
    eta: EtaResult | None = None
    options: list[Option] = field(default_factory=list)
    rejections: dict[str, list[str]] = field(default_factory=dict)
    outcome: str = ""
    outcome_code: str = ""            # OPTIONS | NEEDS_APPROVAL | NO_FEASIBLE_SLOT | CONTRADICTORY_DATA | NOT_ELIGIBLE | FIXED


class FeasibilityEngine:
    def __init__(self, data: FacilityData, fusion: FusionConfig = FusionConfig()):
        self.d = data
        self.fusion = fusion

    # ---- derived variables -----------------------------------------------------------
    def signals(self, sid: str) -> list[Signal]:
        s = self.d.shipments[sid]
        out = [Signal("ORIGINAL_PLAN", datetime.min.replace(tzinfo=ts(s["original_eta_ts"]).tzinfo),
                      ts(s["original_eta_ts"]))]
        for e in self.d.eta_updates:
            if e["shipment_id"] == sid:
                out.append(Signal(ETA_SOURCE_MAP[e["source_type"]], ts(e["created_at"]), ts(e["declared_eta_ts"]),
                                  confidence=e["confidence_code"]))
        ck = self.d.checkins.get(sid)
        if ck and ck.get("gate_in_ts"):
            out.append(Signal("GATE_IN", ts(ck["gate_in_ts"]), ts(ck["gate_in_ts"])))
        return out + list(self.d.extra_signals.get(sid, []))

    def effective_eta(self, sid: str, now: datetime) -> EtaResult:
        # Gate-in is handled by `ready` below (as in the prototype), so fusion here covers the trip itself.
        sig = [x for x in self.signals(sid) if x.source != "GATE_IN"]
        return fuse_eta(sig, now, self.fusion)

    def required_capability(self, s) -> str:
        heavy_kg = int(self.d.rules.get(s["destination_facility_id"], {}).get("HEAVY_DOCK_REQUIRED_KG", 10**9))
        if s["temperature_control_required"]:
            return "REEFER"
        if s["required_dock_type"] == "HEAVY" or s["load_weight_kg"] > heavy_kg:
            return "HEAVY"
        return "STANDARD"

    def dock_compatible(self, s, d) -> str | None:
        if d["facility_id"] != s["destination_facility_id"]:
            return "other facility"
        cap = self.required_capability(s)
        if d["dock_type"] != cap:
            return f"dock type {d['dock_type']} ≠ required {cap}"
        if s["load_weight_kg"] > d["max_vehicle_weight_kg"]:
            return f"load {s['load_weight_kg']} kg > dock limit {d['max_vehicle_weight_kg']} kg"
        if s["temperature_control_required"] and not d["supports_refrigerated"]:
            return "no refrigeration"
        return None

    def busy_window(self, sid: str):
        s = self.d.shipments[sid]
        start = ts(s["actual_departure_ts"] or s["planned_departure_ts"])
        ck = self.d.checkins.get(sid)
        if ck and ck["gate_out_ts"]:
            return start, ts(ck["gate_out_ts"])
        eta = ts(s["latest_eta_ts"] or s["original_eta_ts"])
        return start, eta + timedelta(minutes=s["expected_unload_min"])

    def assignment_conflicts(self, sid: str) -> list[str]:
        """TMS data validation (08_signoff §3). The DB truck constraint only stops overlapping dock claims."""
        s = self.d.shipments[sid]
        if s["current_status"] in ("CANCELLED", "COMPLETED"):
            return []
        a0, a1 = self.busy_window(sid)
        out = []
        for oid, o in self.d.shipments.items():
            if oid == sid or o["current_status"] in ("CANCELLED", "COMPLETED"):
                continue
            if o["driver_id"] != s["driver_id"] and o["vehicle_id"] != s["vehicle_id"]:
                continue
            b0, b1 = self.busy_window(oid)
            if a0 < b1 and b0 < a1:
                who = "driver" if o["driver_id"] == s["driver_id"] else "vehicle"
                out.append(f"{who} also on {oid} ({hm(b0)}–{hm(b1)})")
        return out

    def appt_window(self, a) -> tuple[str, datetime, datetime]:
        """(dock_id, start, end) of an appointment: via its slot (snapshot) or its own fields (PostgreSQL)."""
        if a.get("slot_id"):
            sl = self.d.slot_by_id[a["slot_id"]]
            return sl["dock_id"], ts(sl["slot_start_ts"]), ts(sl["slot_end_ts"])
        return a["dock_id"], ts(a["start"]), ts(a["end"])

    def committed_intervals(self, dock_id: str, now: datetime, exclude_shipment: str):
        if self.d.occupancy is not None:
            out = []
            for o in self.d.occupancy:
                if o["dock_id"] != dock_id or o["kind"] == "BLOCK" or o["shipment_id"] == exclude_shipment:
                    continue                                   # BLOCK rows are reported as dock events instead
                if o["kind"] == "HOLD" and o["expires_at"] and ts(o["expires_at"]) < now:
                    continue                                   # expired hold: released lazily by place_hold
                end = ts(o["end"])
                if o["kind"] == "FIXED":                       # truck in dock: may overrun its prediction
                    co = self.d.timings[self.d.docks[dock_id]["facility_id"]].changeover_min
                    end = max(end, now + timedelta(minutes=co))
                out.append((ts(o["start"]), end, o["ref_id"]))
            return out
        out = []
        for a in self.d.appts:
            if a["appointment_status"] not in ACTIVE or a["shipment_id"] == exclude_shipment:
                continue
            sl = self.d.slot_by_id[a["slot_id"]]
            if sl["dock_id"] != dock_id:
                continue
            s = self.d.shipments[a["shipment_id"]]
            co = self.d.timings[sl["facility_id"]].changeover_min
            ck = self.d.checkins.get(a["shipment_id"])
            if a["appointment_status"] == "IN_PROGRESS" and ck and ck["unload_start_ts"]:
                start = ts(ck["unload_start_ts"])
                end = max(now, start + timedelta(minutes=s["expected_unload_min"])) + timedelta(minutes=co)
            else:
                start = ts(sl["slot_start_ts"])
                end = max(ts(sl["slot_end_ts"]), start + timedelta(minutes=s["expected_unload_min"] + co))
            out.append((start, end, a["appointment_id"]))
        return out

    def dock_events(self, dock_id: str):
        return [(ts(e["event_start_ts"]), ts(e["event_end_ts"]) or datetime.max.replace(tzinfo=ts(e["event_start_ts"]).tzinfo),
                 f"{e['dock_event_id']} {e['event_type']}")
                for e in self.d.events if e["dock_id"] == dock_id and e["event_type"] in ("BREAKDOWN", "MAINTENANCE", "MANUAL_BLOCK")]

    # ---- the predicate -------------------------------------------------------------------
    def find_options(self, sid: str, now: datetime, earliest_start: datetime | None = None,
                     latest_finish: datetime | None = None, max_options: int = 3,
                     operating_days_ahead: int = 1) -> Result:
        """operating_days_ahead: how many operating days after the ready day are offered. Drivers (the agent) get 1
        (decision T44); a planner view may pass more. Closed days (no open published slot at the facility) are
        skipped, within SEARCH_WINDOW_DAYS calendar days of the ready day."""
        s = self.d.shipments[sid]
        fac = s["destination_facility_id"]
        tm = self.d.timings[fac]
        er = self.effective_eta(sid, now)
        eta, src, conf = er.eta, er.source, er.confidence
        ck = self.d.checkins.get(sid)
        arrived = bool(ck and ck["gate_in_ts"])
        eta_hi = eta + timedelta(minutes=tm.eta_band[conf])
        ready = (max(now, ts(ck["gate_in_ts"])) if arrived else max(now, eta_hi)) + timedelta(minutes=tm.gate_to_dock_min)
        occ = s["expected_unload_min"] + tm.changeover_min
        f = self.d.facilities[fac]
        # B-FEAS-1: each slot is judged by ITS OWN day's close and last-new-start time, and the search covers the day
        # the truck is ready and the next one. (Before, every slot was judged by the ready day's close, so a truck
        # ready late at night got no options even with free slots the next morning.) For a same-day slot this is the
        # Phase 1 prototype's rule unchanged.
        close_t = datetime.strptime(f["close_time"], "%H:%M").time()
        last_new = self.d.rules.get(fac, {}).get("LAST_NEW_START_TIME")
        last_new_t = datetime.strptime(last_new, "%H:%M").time() if last_new else close_t
        tz = ready.tzinfo
        ready_day = ready.astimezone(tz).date()
        # S4-06: the NEXT OPERATING day, not the next calendar day. A truck ready late on Friday at a facility closed
        # at the weekend is offered Monday morning.
        fac_docks = {k for k, v in self.d.docks.items() if v["facility_id"] == fac}
        open_days = sorted({ts(x["slot_start_ts"]).astimezone(tz).date() for x in self.d.slots
                            if x["dock_id"] in fac_docks and x["slot_status"] == "OPEN"
                            and ready_day < ts(x["slot_start_ts"]).astimezone(tz).date()
                            <= ready_day + timedelta(days=SEARCH_WINDOW_DAYS)})
        horizon = open_days[min(operating_days_ahead, len(open_days)) - 1] if open_days and operating_days_ahead > 0 \
            else ready_day

        def day_rules(start: datetime) -> tuple[datetime, datetime]:
            d0 = start.astimezone(tz).date()
            return datetime.combine(d0, close_t, tz), datetime.combine(d0, last_new_t, tz)
        conflicts = self.assignment_conflicts(sid)

        cur = next((a for a in self.d.appts if a["shipment_id"] == sid and a["is_current"] and a["appointment_status"] in ACTIVE), None)
        cur_txt, at_risk = "none", None
        if cur:
            cdock, cstart, cend = self.appt_window(cur)
            d = self.d.docks[cdock]
            grace = int(self.d.rules.get(fac, {}).get("NO_SHOW_GRACE_MIN", f["checkin_grace_min"]))
            # Owner decision OD-1 (18 Nov 2026): grace applies to the expected end of unloading + changeover, not to
            # dock arrival. The Phase 1 prototype also tested `ready > slot start + grace`; that check is removed.
            at_risk = (ready + timedelta(minutes=occ) > cend + timedelta(minutes=grace)) or bool(self.dock_compatible(s, d))
            cur_txt = f"{cur['appointment_id']} {d['dock_code']} {hm(cstart)}–{hm(cend)} ({cur['appointment_status']})"

        res = Result(sid, now, dict(
            status=s["current_status"], priority=s["priority_code"], weight_kg=s["load_weight_kg"],
            eta=hm(eta), eta_source=src, eta_confidence=conf, eta_window=f"{hm(eta)}–{hm(eta_hi)}",
            arrived=arrived, ready_ts=hm(ready), capability=self.required_capability(s),
            unload_min=s["expected_unload_min"], occupancy_min=occ,
            current_appointment=cur_txt, appointment_at_risk=at_risk,
            assignment_conflicts="; ".join(conflicts) or "none",
            earliest_start=hm(earliest_start), latest_finish=hm(latest_finish)), eta=er)

        if s["current_status"] in ("CANCELLED", "COMPLETED"):
            res.outcome, res.outcome_code = f"NOT ELIGIBLE: shipment is {s['current_status']}", "NOT_ELIGIBLE"
            return res
        if ck and ck["queue_state"] in ("IN_DOCK", "COMPLETED"):
            res.outcome, res.outcome_code = "FIXED TASK: truck is already in a dock; never re-slotted", "FIXED"
            return res
        for dock_id, d in sorted(self.d.docks.items(), key=lambda kv: kv[1]["dock_code"]):
            if d["facility_id"] != fac:
                continue
            reason = self.dock_compatible(s, d)
            if reason:
                res.rejections[d["dock_code"]] = [reason]
                continue
            dslots = [x for x in self.d.slots if x["dock_id"] == dock_id]
            if not dslots:
                res.rejections[d["dock_code"]] = ["no slots published"]
                continue
            slot_len = (ts(dslots[0]["slot_end_ts"]) - ts(dslots[0]["slot_start_ts"])).seconds // 60
            k = math.ceil(occ / slot_len)
            busy = self.committed_intervals(dock_id, now, sid)
            evts = self.dock_events(dock_id)
            reasons: list[str] = []
            for i, first in enumerate(dslots):
                run = dslots[i:i + k]
                start = ts(first["slot_start_ts"])
                if start.astimezone(tz).date() > horizon:
                    continue                                        # beyond the next operating day: not offered
                end = start + timedelta(minutes=occ)
                close, last_new_dt = day_rules(start)
                why = None
                if len(run) < k or any(ts(run[j]["slot_end_ts"]) != ts(run[j + 1]["slot_start_ts"]) for j in range(len(run) - 1)):
                    why = "not enough contiguous slots"
                elif start < ready:
                    why = None if start + timedelta(minutes=slot_len) <= ready else "starts before truck is ready"
                    if why is None:
                        continue
                elif earliest_start and start < earliest_start:
                    why = "before driver's earliest start"
                elif any(x["slot_status"] != "OPEN" for x in run):
                    bad = next(x for x in run if x["slot_status"] != "OPEN")
                    why = f"slot {bad['slot_status'].lower()} ({bad['block_reason']})"
                elif any(b0 < end and start < b1 for b0, b1, _ in busy):
                    why = "occupied by " + next(a for b0, b1, a in busy if b0 < end and start < b1)
                elif any(e0 < end and start < e1 for e0, e1, _ in evts):
                    why = next(n for e0, e1, n in evts if e0 < end and start < e1)
                elif end > close:
                    why = "ends after facility close"
                elif latest_finish and end > latest_finish:
                    why = "finishes after driver's latest finish"
                if why:
                    reasons.append(f"{hm(start)} {why}")
                    continue
                res.options.append(Option(d["dock_code"], start, end, tuple(x["slot_id"] for x in run), start >= last_new_dt))
            res.rejections[d["dock_code"]] = reasons[:4] + ([f"… {len(reasons) - 4} more"] if len(reasons) > 4 else [])

        res.options.sort(key=lambda o: (o.needs_approval, o.end, o.dock))
        res.options = res.options[:max_options]
        if conflicts:
            res.outcome, res.outcome_code = "ESCALATE (CONTRADICTORY_DATA): driver/vehicle double-assigned; options shown to ops only", "CONTRADICTORY_DATA"
        elif not res.options:
            res.outcome, res.outcome_code = "ESCALATE (NO_FEASIBLE_SLOT)", "NO_FEASIBLE_SLOT"
        elif all(o.needs_approval for o in res.options):
            res.outcome, res.outcome_code = "OPTIONS NEED APPROVAL (after last new start time)", "NEEDS_APPROVAL"
        else:
            res.outcome, res.outcome_code = f"{len(res.options)} feasible option(s)", "OPTIONS"
        return res

    def is_feasible(self, sid: str, option: Option, now: datetime) -> tuple[bool, str]:
        """Re-check one previously shown option at confirmation time. Returns (ok, reason)."""
        res = self.find_options(sid, now, max_options=10**6)
        if res.outcome_code in ("NOT_ELIGIBLE", "FIXED", "CONTRADICTORY_DATA"):
            return False, res.outcome
        for o in res.options:
            if o.dock == option.dock and o.start == option.start:
                return True, "still feasible"
        rej = res.rejections.get(option.dock, [])
        hit = next((r for r in rej if r.startswith(hm(option.start))), None)
        return False, hit or "no longer feasible (capacity or ETA changed)"


def is_no_show(slot_start: datetime, grace_min: int, gate_in: datetime | None, now: datetime) -> bool:
    """Owner decision OD-1: grace is how long after the slot start a truck may check in at the gate. Checking in exactly
    at the deadline is on time (strict deadline); a truck with no check-in becomes a no-show only after it.
    For the no-show sweeper (step 4); appointment feasibility uses the finish-based rule in `find_options`."""
    deadline = slot_start + timedelta(minutes=grace_min)
    return gate_in > deadline if gate_in is not None else now > deadline
