"""Phase G1: deterministic dock/slot optimizer for ONE arriving or delayed truck (docs/phase-g/PHASE_G_OPTIMIZATION_DESIGN.md).

What it does
  * Candidate generation is the existing feasibility predicate: `FeasibilityEngine.find_options` over every published
    slot start on the ready day and the next operating day(s). Nothing is duplicated: compatibility, ETA fusion,
    readiness, committed dock time, dock events, close time and the last-new-start rule stay in one place.
  * Every candidate is then RE-PROVEN against the twelve hard constraints (HC01..HC12) by an independent validator
    that re-derives each fact from the raw rows. Labour (HC07) and facility capacity (HC10) exist only here. An option
    that fails any hard constraint is never returned; it is listed under `rejected` with the failing constraint.
  * The surviving options are scored on documented soft objectives (integer arithmetic, lower is better) and ranked
    with a total, stable tie-break. Exhaustive evaluation of a finite candidate set is exact: when every candidate was
    evaluated inside the time limit the status is OPTIMAL (optimal over the published slot grid).

What it never does
  * It never commits, holds or requests a booking and never writes the database. The booking service and the
    database functions (hold_option, submit_booking_request, decide_booking_request, move_appointment) stay the only
    path to a booking, and they re-check everything under their own locks.
  * It never reads chat text. No model is involved; a language model may only EXPLAIN the structured output.
  * It never uses another facility's docks, slots, claims or events (HC12 asserts it on every option).

Solver choice: OR-Tools CP-SAT was evaluated and not used (design doc §3): the per-truck problem is a choice among at
most (docks x slot starts) ~ a few hundred candidates, which exhaustive enumeration solves exactly in milliseconds,
while the ortools wheel (~30 MB + protobuf) would change the reproducible Lambda zip and the Python 3.13 build. The
facility-wide sequencing problem remains `scheduling.py` (dispatch rules), as decided in the Frame spike.

Determinism: no randomness (SEED is recorded as 0 for completeness), integer scores, sorted iteration everywhere,
tie-break (score, start, end, dock code). Equal input -> equal output and equal `input_fingerprint`.
"""
from __future__ import annotations

import hashlib
import json
import time as _time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable

from .feasibility import FacilityData, FeasibilityEngine, Option, hm, ts

ENGINE_VERSION = "slot-optimizer-v1"
SOLVER = "exhaustive-enumeration"          # exact over the finite candidate set; no third-party solver
SOLVER_VERSION = "1.0.0"
SEED = 0                                   # no randomness anywhere; recorded for reproducibility
ASSUMPTIONS = Path(__file__).resolve().parents[1] / "adapters" / "phase_g_assumptions.json"
PRIORITY_WEIGHT = {"CRITICAL": 8, "HIGH": 4, "NORMAL": 2, "LOW": 1}

# Solver status vocabulary (CP-SAT compatible where it applies, plus the safe outcomes).
OPTIMAL, FEASIBLE, INFEASIBLE, UNKNOWN = "OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"
MODEL_INVALID, STALE_DATA, ERROR = "MODEL_INVALID", "STALE_DATA", "ERROR"
STATUSES = (OPTIMAL, FEASIBLE, INFEASIBLE, UNKNOWN, MODEL_INVALID, STALE_DATA, ERROR)

HARD_CONSTRAINTS = {
    "HC01": "dock belongs to the shipment's facility",
    "HC02": "dock is operational (no breakdown, no blocked slot)",
    "HC03": "dock is compatible with cargo and vehicle (type, weight, refrigeration)",
    "HC04": "slot is inside operating hours (open .. close)",
    "HC05": "no maintenance or manual block overlaps the slot",
    "HC06": "no overlap with any active claim on the dock (confirmed, pending, hold, in dock)",
    "HC07": "a labour crew is available for the whole slot",
    "HC08": "duration covers unload + changeover",
    "HC09": "truck can be at the dock by the start (ready time, driver's earliest/latest)",
    "HC10": "facility concurrent-unload capacity is respected",
    "HC11": "every existing CONFIRMED / IN_PROGRESS appointment of another shipment is respected",
    "HC12": "only this facility's data is used",
}


@dataclass(frozen=True)
class Weights:
    """Soft objectives, integer points per unit (lower total is better). See design doc §4 for the reasoning."""
    wait_per_min: int = 3            # x priority weight x perishable factor: yard wait (start - ready)
    gap_per_min: int = 1             # arrival-to-start gap (start - fused ETA), what the driver experiences
    perishable_factor: int = 2       # reefer / temperature-controlled cargo counts double
    change_penalty: int = 60         # moving an existing confirmed appointment (other dock or > 15 min)
    shift_per_min: int = 1           # per minute moved from the promised start
    idle_per_min: int = 1            # crew idle gap created before the slot on that dock (capped 120)
    overtime_per_min: int = 5        # minutes after the last-new-start time / shift end (needs approval)
    utilisation_per_pct: int = 1     # per % of the dock's day already booked (spreads load)
    bottleneck_penalty: int = 40     # slot where the facility would be at its capacity limit


@dataclass(frozen=True)
class OptimizerConfig:
    weights: Weights = field(default_factory=Weights)
    time_limit_ms: int = 2000         # wall-clock budget for validation + scoring
    max_options: int = 3
    operating_days_ahead: int = 1     # as offered to drivers (decision T44); planners may pass more
    snapshot_max_age_s: int = 120     # older facility data -> STALE_DATA, no options
    eta_stale_after_min: int = 240    # latest ETA evidence older than this (truck not arrived) -> warning flag
    change_tolerance_min: int = 15
    idle_cap_min: int = 120
    # Assumptions (configurable; docs/phase-g/PHASE_G_DATASET_AND_ASSUMPTIONS.md). Labour: crews per shift come from
    # facility_config.shifts[*].crews; when absent, `default_crews` (None = one crew per dock, i.e. never binding).
    default_crews: int | None = None
    labour: dict = field(default_factory=dict)         # facility_id -> [{"start":"HH:MM","end":"HH:MM","crews":n}]
    capacity: dict = field(default_factory=dict)       # facility_id -> max concurrent unloads (default: dock count)

    @classmethod
    def from_assumptions(cls, path: Path = ASSUMPTIONS) -> "OptimizerConfig":
        """The configurable assumptions (capacity, staleness, time limit). Labour crews are read per facility from
        facility_config.shifts[*].crews at run time; `labour` here would override them and is left empty."""
        a = json.loads(path.read_text(encoding="utf-8"))
        return cls(time_limit_ms=int(a.get("optimizer_time_limit_ms", 2000)),
                   snapshot_max_age_s=int(a.get("snapshot_max_age_s", 120)),
                   eta_stale_after_min=int(a.get("eta_stale_after_min", 240)),
                   default_crews=a.get("default_crews_when_missing"), capacity=dict(a.get("facility_capacity", {})))

    def as_dict(self) -> dict:
        return {"weights": asdict(self.weights), "time_limit_ms": self.time_limit_ms, "max_options": self.max_options,
                "operating_days_ahead": self.operating_days_ahead, "snapshot_max_age_s": self.snapshot_max_age_s,
                "eta_stale_after_min": self.eta_stale_after_min, "change_tolerance_min": self.change_tolerance_min,
                "default_crews": self.default_crews, "labour": self.labour, "capacity": self.capacity, "seed": SEED}


@dataclass(frozen=True)
class OptimizationRequest:
    shipment_id: str
    facility_id: str | None = None     # when given it must match the shipment's destination (else MODEL_INVALID)
    earliest_start: datetime | None = None
    latest_finish: datetime | None = None
    request_id: str = ""
    data_as_of: datetime | None = None # when the facility rows were read (None = now)
    schedule_version: int | None = None


@dataclass
class RankedOption:
    rank: int
    dock_id: str
    dock_code: str
    start: datetime
    end: datetime
    wait_min: int
    gap_min: int
    duration_min: int
    needs_approval: bool
    feasible: bool
    score: int
    score_terms: dict
    reasons: list[str]
    slots: tuple[str, ...] = ()

    def option(self) -> Option:
        return Option(self.dock_code, self.start, self.end, self.slots, self.needs_approval)

    def to_dict(self) -> dict:
        return {"rank": self.rank, "dock_id": self.dock_id, "dock_code": self.dock_code, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "wait_min": self.wait_min, "gap_min": self.gap_min,
                "duration_min": self.duration_min, "needs_approval": self.needs_approval, "feasible": self.feasible,
                "score": self.score, "score_terms": self.score_terms, "reasons": self.reasons}


@dataclass
class OptimizationResult:
    request_id: str
    facility_id: str | None
    shipment_id: str
    input_eta: str | None
    ready_at: str | None
    solver: str
    solver_version: str
    engine_version: str
    solver_status: str
    outcome_code: str
    recommended_action: str            # OFFER | OFFER_WITH_APPROVAL | ESCALATE | REFRESH_DATA | NONE
    options: list[RankedOption] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    constraints_evaluated: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    candidates_evaluated: int = 0
    duration_ms: int = 0
    data_as_of: str | None = None
    schedule_version: int | None = None
    input_fingerprint: str = ""
    config: dict = field(default_factory=dict)
    not_a_booking: bool = True

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("options",)}
        d["options"] = [o.to_dict() for o in self.options]
        return d


# ================================================================ helpers
def _t(v, default: str) -> time:
    if isinstance(v, time):
        return v
    return datetime.strptime(str(v or default)[:5], "%H:%M").time()


def _mins(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def _overlap(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def _max_concurrent(intervals: list[tuple[datetime, datetime]], s: datetime, e: datetime) -> int:
    """Peak number of the given intervals that are simultaneously inside [s, e)."""
    pts = []
    for a, b in intervals:
        a, b = max(a, s), min(b, e)
        if a < b:
            pts += [(a, 1), (b, -1)]
    cur = peak = 0
    for _, d in sorted(pts, key=lambda x: (x[0], x[1])):            # ends (-1) before starts at the same instant
        cur += d
        peak = max(peak, cur)
    return peak


class SlotOptimizer:
    def __init__(self, data: FacilityData, config: OptimizerConfig = OptimizerConfig(),
                 clock: Callable[[], float] = _time.perf_counter, engine: FeasibilityEngine | None = None):
        self.d, self.cfg, self.clock = data, config, clock
        self.engine = engine or FeasibilityEngine(data)

    # ---------------------------------------------------------------- facility facts (this facility only)
    def _docks(self, fac: str) -> dict:
        return {k: v for k, v in self.d.docks.items() if v["facility_id"] == fac}

    def _claims(self, fac: str, now: datetime, exclude: str) -> list[tuple[str, datetime, datetime, str]]:
        """(dock_id, start, end, ref) of every active claim on this facility's docks except the truck's own."""
        out = []
        for dock_id in sorted(self._docks(fac)):
            for s, e, ref in self.engine.committed_intervals(dock_id, now, exclude):
                out.append((dock_id, s, e, ref))
        return out

    def _confirmed(self, fac: str, exclude: str) -> list[tuple[str, datetime, datetime, str]]:
        """HC11's independent source: the appointment rows themselves (not the occupancy ledger)."""
        docks = self._docks(fac)
        out = []
        for a in self.d.appts:
            if a["shipment_id"] == exclude or a["appointment_status"] not in ("CONFIRMED", "IN_PROGRESS"):
                continue
            if a.get("is_current") is False:
                continue
            dock_id, s, e = self.engine.appt_window(a)
            if dock_id in docks:
                out.append((dock_id, s, e, a["appointment_id"]))
        return out

    def _labour(self, fac: str, at: datetime, n_docks: int) -> tuple[int, bool]:
        """(crews on shift at `at`, assumed?) from config, then facility_config.shifts[*].crews, then the default."""
        shifts = self.cfg.labour.get(fac)
        assumed = False
        if shifts is None:
            shifts = [s for s in (self.d.facilities.get(fac, {}).get("shifts") or []) if "crews" in s]
        if not shifts:
            return (self.cfg.default_crews if self.cfg.default_crews is not None else n_docks), True
        t = at.time()                                                       # wall time in the slot's zone
        for s in shifts:
            a, b = _t(s["start"], "00:00"), _t(s["end"], "00:00")
            inside = (a <= t < b) if a < b else (t >= a or t < b)          # a shift may cross midnight
            if inside:
                return int(s["crews"]), assumed
        return 0, assumed                                                   # nobody rostered: no labour

    def _capacity(self, fac: str, n_docks: int) -> int:
        return int(self.cfg.capacity.get(fac, n_docks))

    # ---------------------------------------------------------------- hard constraints
    def check(self, sid: str, fac: str, dock_id: str, start: datetime, end: datetime, now: datetime, ready: datetime,
              earliest: datetime | None, latest: datetime | None, slots: tuple[str, ...] = ()) -> dict[str, tuple[bool, str]]:
        """Independent validator. Every constraint is re-derived from the rows; returns {HCxx: (ok, detail)}."""
        s = self.d.shipments[sid]
        docks = self._docks(fac)
        d = self.d.docks.get(dock_id)
        tm = self.d.timings[fac]
        f = self.d.facilities[fac]
        tz = start.tzinfo
        out: dict[str, tuple[bool, str]] = {}
        out["HC01"] = (bool(d) and d["facility_id"] == fac == s["destination_facility_id"],
                       f"dock {dock_id} at {d['facility_id'] if d else '?'}")
        evts = [e for e in self.d.events if e["dock_id"] == dock_id]
        brk = [e for e in evts if e["event_type"] == "BREAKDOWN"
               and _overlap(ts(e["event_start_ts"]), ts(e["event_end_ts"]) or datetime.max.replace(tzinfo=tz), start, end)]
        bad_slot = [x for x in slots if self.d.slot_by_id.get(x, {}).get("slot_status", "OPEN") != "OPEN"]
        out["HC02"] = (not brk and not bad_slot, "breakdown " + brk[0]["dock_event_id"] if brk else
                       ("slot not open: " + bad_slot[0] if bad_slot else "operational"))
        why = self.engine.dock_compatible(s, d) if d else "unknown dock"
        out["HC03"] = (why is None, why or f"{d['dock_type']} fits {self.engine.required_capability(s)}")
        day = start.astimezone(tz).date()
        open_dt = datetime.combine(day, _t(f.get("open_time"), "00:00"), tz)
        close_dt = datetime.combine(day, _t(f.get("close_time"), "23:59"), tz)
        out["HC04"] = (open_dt <= start and end <= close_dt, f"{hm(start)}-{hm(end)} within {hm(open_dt)}-{hm(close_dt)}")
        mnt = [e for e in evts if e["event_type"] in ("MAINTENANCE", "MANUAL_BLOCK")
               and _overlap(ts(e["event_start_ts"]), ts(e["event_end_ts"]) or datetime.max.replace(tzinfo=tz), start, end)]
        out["HC05"] = (not mnt, f"{mnt[0]['dock_event_id']} {mnt[0]['event_type']}" if mnt else "no maintenance window")
        claims = self._claims(fac, now, sid)
        hit = [c for c in claims if c[0] == dock_id and _overlap(c[1], c[2], start, end)]
        out["HC06"] = (not hit, f"occupied by {hit[0][3]}" if hit else "dock free")
        # crews must be rostered for the WHOLE slot: sampled at the start and every 15 minutes to the end
        samples = [self._labour(fac, start + timedelta(minutes=m), len(docks)) for m in range(0, max(1, _mins(start, end)), 15)]
        crews, assumed = min(c for c, _ in samples), any(a for _, a in samples)
        busy = [(c[1], c[2]) for c in claims]
        peak = _max_concurrent(busy, start, end)
        out["HC07"] = (peak + 1 <= crews, f"{peak} crew(s) busy of {crews}{' (assumed)' if assumed else ''}")
        need = s["expected_unload_min"] + tm.changeover_min
        out["HC08"] = (_mins(start, end) >= need and s["expected_unload_min"] > 0, f"{_mins(start, end)} min >= {need} min")
        ok9 = start >= ready and (earliest is None or start >= earliest) and (latest is None or end <= latest)
        out["HC09"] = (ok9, f"ready {hm(ready)}, start {hm(start)}"
                       + (f", earliest {hm(earliest)}" if earliest else "") + (f", latest {hm(latest)}" if latest else ""))
        cap = self._capacity(fac, len(docks))
        out["HC10"] = (peak + 1 <= cap, f"{peak + 1} concurrent of capacity {cap}")
        conf = [c for c in self._confirmed(fac, sid) if c[0] == dock_id and _overlap(c[1], c[2], start, end)]
        out["HC11"] = (not conf, f"confirmed {conf[0][3]} on this dock" if conf else "no confirmed appointment displaced")
        foreign = [x for x in slots if self.d.slot_by_id.get(x, {}).get("dock_id") not in docks]
        out["HC12"] = (bool(d) and dock_id in docks and not foreign and all(c[0] in docks for c in claims),
                       "facility-only data" if not foreign else f"foreign slot {foreign[0]}")
        return out

    # ---------------------------------------------------------------- objective
    def score(self, sid: str, fac: str, dock_id: str, start: datetime, end: datetime, ready: datetime, eta: datetime,
              needs_approval: bool, claims: list, last_new: datetime) -> tuple[int, dict, list[str]]:
        w = self.cfg.weights
        s = self.d.shipments[sid]
        pw = PRIORITY_WEIGHT.get(s["priority_code"], 2) * (w.perishable_factor if s["temperature_control_required"] else 1)
        wait = max(0, _mins(ready, start))
        gap = max(0, _mins(eta, start))
        terms = {"wait": w.wait_per_min * pw * wait, "gap": w.gap_per_min * gap}
        reasons = [f"yard wait {wait} min (priority {s['priority_code']}"
                   + (", perishable" if s["temperature_control_required"] else "") + ")"]
        cur = next((a for a in self.d.appts if a["shipment_id"] == sid and a.get("is_current", True)
                    and a["appointment_status"] == "CONFIRMED"), None)
        change = 0
        if cur:
            cdock, cstart, _ = self.engine.appt_window(cur)
            moved = abs(_mins(cstart, start))
            if cdock != dock_id or moved > self.cfg.change_tolerance_min:
                change = w.change_penalty + w.shift_per_min * moved
                reasons.append(f"changes the confirmed appointment {cur['appointment_id']} ({moved} min"
                               + (", other dock" if cdock != dock_id else "") + ")")
            else:
                reasons.append("keeps the confirmed appointment")
        terms["disruption"] = change
        prev = [c[2] for c in claims if c[0] == dock_id and c[2] <= start and c[2].date() == start.date()]
        idle = min(self.cfg.idle_cap_min, _mins(max(prev), start)) if prev else 0
        terms["labour_idle"] = w.idle_per_min * idle
        overtime = max(0, _mins(last_new, end)) if needs_approval else 0
        terms["overtime"] = w.overtime_per_min * overtime
        if overtime:
            reasons.append(f"{overtime} min after the last new start time: needs approval")
        day0 = datetime.combine(start.date(), time(0), start.tzinfo)
        booked = sum(_mins(max(c[1], day0), min(c[2], day0 + timedelta(days=1)))
                     for c in claims if c[0] == dock_id and _overlap(c[1], c[2], day0, day0 + timedelta(days=1)))
        util = min(100, booked * 100 // (24 * 60))
        terms["utilisation"] = w.utilisation_per_pct * util
        n_docks = len(self._docks(fac))
        peak = _max_concurrent([(c[1], c[2]) for c in claims], start, end)
        bottleneck = peak + 2 > self._capacity(fac, n_docks)            # this truck makes the facility full
        terms["bottleneck"] = w.bottleneck_penalty if bottleneck else 0
        if bottleneck:
            reasons.append("facility at capacity during this slot")
        return sum(terms.values()), terms, reasons

    # ---------------------------------------------------------------- the run
    def optimize(self, req: OptimizationRequest, now: datetime) -> OptimizationResult:
        t0 = self.clock()
        rid = req.request_id or str(uuid.uuid4())
        res = OptimizationResult(rid, req.facility_id, req.shipment_id, None, None, SOLVER, SOLVER_VERSION, ENGINE_VERSION,
                                 UNKNOWN, "", "NONE", constraints_evaluated=dict(HARD_CONSTRAINTS),
                                 data_as_of=(req.data_as_of or now).isoformat(), schedule_version=req.schedule_version,
                                 config=self.cfg.as_dict())
        try:
            self._run(req, now, res, t0)
        except Exception as e:                                   # noqa: BLE001 — the engine failing is a safe outcome
            res.options, res.solver_status, res.outcome_code = [], ERROR, "ENGINE_ERROR"
            res.recommended_action = "ESCALATE"
            res.warnings.append(f"optimizer error: {type(e).__name__}")
        res.duration_ms = int((self.clock() - t0) * 1000)
        return res

    def _invalid(self, res: OptimizationResult, code: str, why: str):
        res.solver_status, res.outcome_code, res.recommended_action = MODEL_INVALID, code, "ESCALATE"
        res.warnings.append(why)

    def _run(self, req: OptimizationRequest, now: datetime, res: OptimizationResult, t0: float):
        sid = req.shipment_id
        s = self.d.shipments.get(sid)
        if s is None:
            return self._invalid(res, "UNKNOWN_SHIPMENT", "shipment not in the facility data")
        fac = s["destination_facility_id"]
        res.facility_id = req.facility_id or fac
        if req.facility_id and req.facility_id != fac:
            return self._invalid(res, "FACILITY_MISMATCH", "requested facility is not the shipment's destination")
        if fac not in self.d.timings or fac not in self.d.facilities:
            return self._invalid(res, "FACILITY_NOT_CONFIGURED", "no active facility configuration")
        if not s.get("expected_unload_min") or s["expected_unload_min"] <= 0:
            return self._invalid(res, "BAD_DURATION", "expected unload duration missing or not positive")
        if req.earliest_start and req.latest_finish and req.latest_finish <= req.earliest_start:
            return self._invalid(res, "INCONSISTENT_WINDOW", "latest finish is not after earliest start")
        if req.data_as_of is not None and (now - req.data_as_of).total_seconds() > self.cfg.snapshot_max_age_s:
            res.solver_status, res.outcome_code, res.recommended_action = STALE_DATA, "STALE_DATA", "REFRESH_DATA"
            res.warnings.append(f"facility data is {int((now - req.data_as_of).total_seconds())} s old "
                                f"(limit {self.cfg.snapshot_max_age_s} s): re-read before offering")
            return
        if not self._docks(fac):
            return self._invalid(res, "NO_DOCKS", "facility has no docks")

        eng = self.engine.find_options(sid, now, req.earliest_start, req.latest_finish, max_options=10**6,
                                       operating_days_ahead=self.cfg.operating_days_ahead)
        er = eng.eta
        if er is None:
            return self._invalid(res, "NO_ETA", "no arrival estimate could be derived")
        tm = self.d.timings[fac]
        res.input_eta = er.eta.isoformat() if er else None
        ck = self.d.checkins.get(sid)
        arrived = bool(ck and ck.get("gate_in_ts"))
        if er is not None:
            ready = (max(now, ts(ck["gate_in_ts"])) if arrived else max(now, er.eta + timedelta(minutes=tm.eta_band[er.confidence]))) \
                + timedelta(minutes=tm.gate_to_dock_min)
            res.ready_at = ready.isoformat()
        latest_obs = max((ts(e["created_at"]) for e in self.d.eta_updates if e["shipment_id"] == sid), default=None)
        if not arrived and latest_obs is not None and _mins(latest_obs, now) > self.cfg.eta_stale_after_min:
            res.warnings.append(f"ETA_STALE: latest ETA report is {_mins(latest_obs, now)} min old")
        if eng.outcome_code in ("NOT_ELIGIBLE", "FIXED"):
            res.solver_status, res.outcome_code, res.recommended_action = MODEL_INVALID, eng.outcome_code, "NONE"
            res.warnings.append(eng.outcome)
            return
        for code, reasons in sorted(eng.rejections.items()):
            for r in reasons:
                res.rejected.append({"dock_code": code, "reason": r, "stage": "feasibility"})

        docks_by_code = {v["dock_code"]: k for k, v in self._docks(fac).items()}
        claims = self._claims(fac, now, sid)
        f = self.d.facilities[fac]
        last_new_t = self.d.rules.get(fac, {}).get("LAST_NEW_START_TIME") or f.get("close_time")
        scored, timed_out = [], False
        for i, o in enumerate(eng.options):
            if (self.clock() - t0) * 1000 > self.cfg.time_limit_ms:
                timed_out = True
                res.warnings.append(f"time limit {self.cfg.time_limit_ms} ms reached after {i} of {len(eng.options)} candidates")
                break
            res.candidates_evaluated += 1
            dock_id = docks_by_code[o.dock]
            hc = self.check(sid, fac, dock_id, o.start, o.end, now, ready, req.earliest_start, req.latest_finish, o.slots)
            failed = [k for k, (ok, _) in hc.items() if not ok]
            if failed:
                res.rejected.append({"dock_code": o.dock, "start": o.start.isoformat(), "stage": "hard_constraints",
                                     "failed": failed, "reason": "; ".join(f"{k} {hc[k][1]}" for k in failed)})
                continue
            last_new = datetime.combine(o.start.date(), _t(last_new_t, "23:59"), o.start.tzinfo)
            total, terms, reasons = self.score(sid, fac, dock_id, o.start, o.end, ready, er.eta, o.needs_approval, claims, last_new)
            scored.append((total, o.start, o.end, o.dock, dock_id, o, terms, reasons))
        scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        for rank, (total, start, end, code, dock_id, o, terms, reasons) in enumerate(scored[:self.cfg.max_options], 1):
            res.options.append(RankedOption(rank, dock_id, code, start, end, max(0, _mins(ready, start)),
                                            max(0, _mins(er.eta, start)), _mins(start, end), o.needs_approval, True,
                                            total, terms, reasons, o.slots))
        for total, start, _, code, _, _, _, _ in scored[self.cfg.max_options:self.cfg.max_options + 10]:
            res.rejected.append({"dock_code": code, "start": start.isoformat(), "stage": "ranking",
                                 "reason": f"feasible but ranked lower (score {total})"})
        res.input_fingerprint = self.fingerprint(req, now, eng, claims)
        if eng.outcome_code == "CONTRADICTORY_DATA":
            res.solver_status, res.outcome_code, res.recommended_action = MODEL_INVALID, "CONTRADICTORY_DATA", "ESCALATE"
            res.warnings.append("driver or vehicle double-assigned: options are for a person, not the driver")
            return
        if timed_out:
            res.solver_status = FEASIBLE if res.options else UNKNOWN
        else:
            res.solver_status = OPTIMAL if res.options else INFEASIBLE
        if not res.options:
            res.outcome_code, res.recommended_action = ("NO_FEASIBLE_SLOT" if not timed_out else "TIMEOUT"), "ESCALATE"
        elif all(o.needs_approval for o in res.options):
            res.outcome_code, res.recommended_action = "NEEDS_APPROVAL", "OFFER_WITH_APPROVAL"
        else:
            res.outcome_code, res.recommended_action = "OPTIONS", "OFFER"

    def fingerprint(self, req: OptimizationRequest, now: datetime, eng, claims) -> str:
        body = {"shipment": req.shipment_id, "now": now.isoformat(), "es": req.earliest_start and req.earliest_start.isoformat(),
                "lf": req.latest_finish and req.latest_finish.isoformat(),
                "candidates": [(o.dock, o.start.isoformat(), o.end.isoformat(), o.needs_approval) for o in eng.options],
                "claims": [(c[0], c[1].isoformat(), c[2].isoformat()) for c in claims], "config": self.cfg.as_dict(),
                "engine": ENGINE_VERSION}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def validate_options(opt: SlotOptimizer, sid: str, now: datetime, options: list[RankedOption], ready: datetime,
                     earliest=None, latest=None) -> list[tuple[RankedOption, list[str]]]:
    """Test/audit helper: re-check returned options; returns those that break any hard constraint (must be empty)."""
    fac = opt.d.shipments[sid]["destination_facility_id"]
    bad = []
    for o in options:
        hc = opt.check(sid, fac, o.dock_id, o.start, o.end, now, ready, earliest, latest, o.slots)
        failed = [k for k, (ok, _) in hc.items() if not ok]
        if failed:
            bad.append((o, failed))
    return bad
