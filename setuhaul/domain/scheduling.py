"""Facility scheduling tool (brief p.10, optional): one facility, all its trucks, one deterministic PROPOSAL.

The conversation agent works for one driver. This module looks at one facility and every truck heading there
together, and proposes a sequence. It is a controlled, read-only planner:

  * It reads STRUCTURED data only (a `FacilitySnapshot`): release times, unload durations, priorities, promised
    windows, dock compatibility, trucks already in a dock and dock blocks. No chat text reaches it, so nothing a
    driver writes can change the ordering (break case S1).
  * It returns a `Proposal` with status "PROPOSAL". A proposal is not a booking: nothing changes until a person
    applies one move with `move_appointment`, and PostgreSQL re-checks that move under the booking locks.
  * Equal input gives equal output: every sort has a total, stable tie-break ending in the shipment id, and the
    input fingerprint is the sha256 of the canonical input.
  * Trucks already unloading (or otherwise fixed: dock blocks, a driver's live hold or pending request) are
    `FixedTask`s. They are never moved; they only occupy dock time.
  * A truck the rules cannot place is UNSCHEDULED with a reason, for a person. Nothing is invented.

Scheduling vocabulary (brief p.10): truck = job, dock = machine, latest ETA = release time, gate-in = available now,
unload duration = processing time, appointment = promised window, truck already unloading = fixed task,
priority = weight. Processing time here = expected unload + changeover, as in the feasibility engine.

Objective, in plain words: less waiting for the trucks that matter most, fewer missed appointments, and fewer
changes to times drivers were already told. As one transparent score per truck (lower is better), the Frame
spike's objective (docs/1-frame/solver_results.md):

    score = w * wait                 wait      = start - release (minutes in the yard)
          + 2 * w * lateness         lateness  = max(0, start - promised start)
          + 30 * changed             changed   = 1 if the dock or the start (beyond 15 min) differs from the promise
    unscheduled truck: 2000 * w      (so the score always prefers placing a truck if any feasible time exists)
    w = priority weight: CRITICAL 8, HIGH 4, NORMAL 2, LOW 1

Three rules, each a simple dispatch (no solver library; the Frame spike showed CP-SAT gains are not needed here):

  FCFS            first come, first served. Trucks already through the gate first, in gate-in order; then trucks
                  on the road in the order of their ORIGINAL planned arrival (the TMS plan the booking was made
                  from, not a later ETA). Each truck takes the earliest free compatible dock time.
  EARLIEST_ETA    by release time (latest effective ETA + confidence band + gate-to-dock, or gate-in for trucks
                  already here), then higher weight, then shipment id. Earliest free compatible dock time.
  FACILITY_SCORE  the proposed rule, three passes:
                  1. keep promises: every truck that can still make its promised window (ready by promised
                     start + no-show grace) is placed on its promised dock at max(promised start, ready), in
                     promise order, if that dock time is free;
                  2. dispatch the rest (no booking yet, a promise already lost, a promised dock that is blocked or
                     double-booked): at each step take the earliest dock time any of them can use; of the trucks
                     competing for it, the highest weight goes first (then earliest release, then id); each truck
                     goes where its score is lowest;
                  3. pull early trucks forward: a truck from pass 1 moves to an earlier free dock time only if its
                     score drops, i.e. the waiting it saves (w * minutes) outweighs the 30-point cost of changing
                     a time the driver was told.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta

from .feasibility import ACTIVE, FacilityData, FeasibilityEngine, hm, ts

WEIGHT = {"CRITICAL": 8, "HIGH": 4, "NORMAL": 2, "LOW": 1}
LATE_WEIGHT = 2              # alpha: per weighted minute late against the promise
CHANGE_PENALTY = 30          # beta: per changed promise
UNSCHEDULED_PENALTY = 2000   # gamma: per weight, a truck left for a person
CHANGE_TOL_MIN = 15          # a start within 15 minutes of the promise on the promised dock is not a change
HORIZON_H = 12               # rolling planning window from now
RULES = ("FCFS", "EARLIEST_ETA", "FACILITY_SCORE")
STATUS = "PROPOSAL"
NOT_A_BOOKING = ("Proposal only, not a booking. No appointment, hold or driver message changes until a person "
                 "applies a move and the database re-checks it.")
ENGINE_VERSION = "facility-dispatch-v1"


# ================================================================ input
@dataclass(frozen=True)
class Dock:
    dock_id: str
    dock_code: str


@dataclass(frozen=True)
class Promise:
    """The current CONFIRMED appointment: what the driver was told."""
    appointment_id: str
    dock_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Job:
    shipment_id: str
    priority: str
    release: datetime                     # earliest time the truck can be at a dock
    processing_min: int                   # unload + changeover
    compatible_docks: tuple[str, ...]     # dock ids, already filtered by the feasibility engine's rules
    arrived_at: datetime | None = None    # gate-in (FCFS key for trucks already here)
    planned_eta: datetime | None = None   # original TMS plan (FCFS key for trucks on the road)
    promise: Promise | None = None
    eta_source: str = ""
    flags: tuple[str, ...] = ()           # e.g. CONTRADICTORY_DATA: never planned, left for a person
    incompatible: tuple[str, ...] = ()    # "D5: dock type REEFER ≠ required STANDARD" (reasons, for people)

    @property
    def weight(self) -> int:
        return WEIGHT.get(self.priority, 2)


@dataclass(frozen=True)
class FixedTask:
    dock_id: str
    start: datetime
    end: datetime
    ref: str
    kind: str                             # IN_DOCK | BLOCK | HOLD | REQUEST_PENDING | BOOKED | APPOINTMENT


@dataclass(frozen=True)
class Hours:
    open: time
    last_new_start: time
    close: time


@dataclass(frozen=True)
class FacilitySnapshot:
    facility_id: str
    now: datetime
    docks: tuple[Dock, ...]
    jobs: tuple[Job, ...]
    fixed: tuple[FixedTask, ...]
    hours: Hours
    grace_min: int = 30
    horizon_h: int = HORIZON_H
    out_of_horizon: tuple[str, ...] = ()  # trucks ready after the window: not planned now, not an escalation

    @property
    def horizon_end(self) -> datetime:
        return self.now + timedelta(hours=self.horizon_h)

    def canonical(self) -> dict:
        def iso(d):
            return d.isoformat() if d else None
        return {
            "facility_id": self.facility_id, "now": iso(self.now), "grace_min": self.grace_min, "horizon_h": self.horizon_h,
            "hours": [self.hours.open.isoformat(), self.hours.last_new_start.isoformat(), self.hours.close.isoformat()],
            "docks": sorted([d.dock_id, d.dock_code] for d in self.docks),
            "jobs": sorted(([j.shipment_id, j.priority, iso(j.release), j.processing_min, sorted(j.compatible_docks),
                             iso(j.arrived_at), iso(j.planned_eta),
                             [j.promise.appointment_id, j.promise.dock_id, iso(j.promise.start), iso(j.promise.end)]
                             if j.promise else None, sorted(j.flags)] for j in self.jobs), key=lambda r: r[0]),
            "fixed": sorted([f.dock_id, iso(f.start), iso(f.end), f.ref, f.kind] for f in self.fixed),
            "out_of_horizon": sorted(self.out_of_horizon),
            "engine": ENGINE_VERSION,
        }

    def fingerprint(self) -> str:
        raw = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()


# ================================================================ output
@dataclass(frozen=True)
class Score:
    wait_min: int
    lateness_min: int
    changed: bool
    missed: bool
    wait_term: int
    lateness_term: int
    change_term: int

    @property
    def total(self) -> int:
        return self.wait_term + self.lateness_term + self.change_term


@dataclass(frozen=True)
class Assignment:
    shipment_id: str
    priority: str
    dock_id: str
    dock_code: str
    start: datetime
    end: datetime
    release: datetime
    action: str                 # KEEP (promise stands) | MOVE (change a CONFIRMED appointment) | PLAN (no booking yet)
    score: Score
    reason: str
    appointment_id: str | None = None
    promised_dock_id: str | None = None
    promised_dock_code: str | None = None
    promised_start: datetime | None = None
    promised_end: datetime | None = None
    depends_on: tuple[str, ...] = ()   # MOVE: moves to apply first (their current claim overlaps this target)
    apply_order: int | None = None     # MOVE: 1, 2, ... in a dependency-safe order; None in a swap cycle (a person)


@dataclass(frozen=True)
class Unscheduled:
    shipment_id: str
    priority: str
    reason_code: str            # NO_COMPATIBLE_DOCK | NO_CAPACITY_IN_HORIZON | CONTRADICTORY_DATA
    reason: str
    appointment_id: str | None = None


@dataclass
class Proposal:
    rule: str
    facility_id: str
    now: datetime
    input_fingerprint: str
    assignments: list[Assignment]
    unscheduled: list[Unscheduled]
    fixed: list[FixedTask]
    metrics: dict
    out_of_horizon: list[str] = field(default_factory=list)
    status: str = STATUS
    not_a_booking: bool = True

    @property
    def moves(self) -> list[Assignment]:
        return [a for a in self.assignments if a.action == "MOVE"]

    def to_dict(self) -> dict:
        def iso(d):
            return d.isoformat() if d else None
        return {
            "status": self.status, "not_a_booking": self.not_a_booking, "note": NOT_A_BOOKING, "rule": self.rule,
            "engine": ENGINE_VERSION, "facility_id": self.facility_id, "now": iso(self.now),
            "input_fingerprint": self.input_fingerprint, "metrics": dict(self.metrics),
            "assignments": [{
                "seq": i, "shipment_id": a.shipment_id, "priority": a.priority, "weight": WEIGHT.get(a.priority, 2),
                "appointment_id": a.appointment_id, "action": a.action, "dock_id": a.dock_id, "dock_code": a.dock_code,
                "start": iso(a.start), "end": iso(a.end), "release": iso(a.release),
                "promised": ({"dock_id": a.promised_dock_id, "dock_code": a.promised_dock_code,
                              "start": iso(a.promised_start), "end": iso(a.promised_end)} if a.promised_start else None),
                "depends_on": list(a.depends_on), "apply_order": a.apply_order,
                "score": {"wait_min": a.score.wait_min, "lateness_min": a.score.lateness_min,
                          "changed_promise": a.score.changed, "missed_promise": a.score.missed,
                          "wait_term": a.score.wait_term, "lateness_term": a.score.lateness_term,
                          "change_term": a.score.change_term, "total": a.score.total},
                "reason": a.reason} for i, a in enumerate(self.assignments, 1)],
            "unscheduled": [{"shipment_id": u.shipment_id, "priority": u.priority, "appointment_id": u.appointment_id,
                             "reason_code": u.reason_code, "reason": u.reason} for u in self.unscheduled],
            "fixed": [{"dock_id": f.dock_id, "start": iso(f.start), "end": iso(f.end), "ref": f.ref, "kind": f.kind}
                      for f in self.fixed],
            "out_of_horizon": list(self.out_of_horizon),
        }


# ================================================================ the board: dock time already taken
def _ceil_min(d: datetime) -> datetime:
    floor = d.replace(second=0, microsecond=0)
    return floor if floor == d else floor + timedelta(minutes=1)


def _minutes(a: datetime, b: datetime) -> int:
    """Whole minutes from a to b (b - a), rounded up: a wait of 30 s counts as 1 minute."""
    return math.ceil((b - a).total_seconds() / 60)


class _Board:
    def __init__(self, snap: FacilitySnapshot):
        self.snap = snap
        self.tz = snap.now.tzinfo
        self.code = {d.dock_id: d.dock_code for d in snap.docks}
        self.busy: dict[str, list[tuple[datetime, datetime, str]]] = {d.dock_id: [] for d in snap.docks}
        for f in snap.fixed:
            if f.dock_id in self.busy and f.end > snap.now:
                self.busy[f.dock_id].append((f.start, f.end, f"{f.kind} {f.ref}"))

    def _day(self, t: datetime) -> tuple[datetime, datetime, datetime, datetime]:
        day = t.astimezone(self.tz).date()
        h = self.snap.hours
        mk = lambda x, d=day: datetime.combine(d, x, self.tz)
        return mk(h.open), mk(h.last_new_start), mk(h.close), mk(h.open, day + timedelta(days=1))

    def conflict(self, dock: str, s: datetime, p: int) -> str | None:
        """Why dock time [s, s+p) cannot be used, or None."""
        e = s + timedelta(minutes=p)
        o, last, close, _ = self._day(s)
        if s < self.snap.now:
            return "in the past"
        if s < o:
            return "before opening"
        if s > last:
            return "after the last new start time"
        if e > close:
            return "ends after closing"
        hit = next((n for b0, b1, n in self.busy[dock] if b0 < e and s < b1), None)
        return f"occupied by {hit}" if hit else None

    def earliest(self, dock: str, t0: datetime, p: int) -> datetime | None:
        t = _ceil_min(max(t0, self.snap.now))
        for _ in range(10_000):                                   # bounded: every step moves t forward
            if t > self.snap.horizon_end:
                return None
            o, last, close, next_open = self._day(t)
            if t < o:
                t = o
                continue
            if t > last or t + timedelta(minutes=p) > close:
                t = next_open
                continue
            e = t + timedelta(minutes=p)
            hit = [b1 for b0, b1, _ in self.busy[dock] if b0 < e and t < b1]
            if hit:
                t = _ceil_min(max(hit))
                continue
            return t
        return None

    def overlap(self, dock: str, a: datetime, b: datetime) -> str | None:
        return next((n for b0, b1, n in self.busy[dock] if b0 < b and a < b1), None)

    def take(self, dock: str, a: datetime, b: datetime, ref: str):
        self.busy[dock].append((a, b, ref))

    def free(self, ref: str):
        for d in self.busy:
            self.busy[d] = [b for b in self.busy[d] if b[2] != ref]


# ================================================================ scoring
def score(snap: FacilitySnapshot, j: Job, dock: str, start: datetime) -> Score:
    w = j.weight
    wait = max(0, _minutes(j.release, start))
    late = changed = missed = 0
    if j.promise:
        late = max(0, _minutes(j.promise.start, start))
        changed = int(dock != j.promise.dock_id or abs((start - j.promise.start).total_seconds()) > CHANGE_TOL_MIN * 60)
        missed = int(start > j.promise.start + timedelta(minutes=snap.grace_min))
    return Score(wait, late, bool(changed), bool(missed), w * wait, LATE_WEIGHT * w * late, CHANGE_PENALTY * changed)


def _span(snap: FacilitySnapshot, j: Job, d: str, s: datetime) -> tuple[datetime, datetime]:
    """Dock time a placement holds. A KEPT promise also keeps its current claim in the database (nothing is applied
    for it), so it holds the union of the planned interval and that claim; no other row may be planned into it."""
    e = s + timedelta(minutes=j.processing_min)
    if j.promise and d == j.promise.dock_id and not score(snap, j, d, s).changed:
        return min(s, j.promise.start), max(e, j.promise.end)
    return s, e


def _usable(board: _Board, j: Job, d: str, s: datetime) -> bool:
    if board.conflict(d, s, j.processing_min) is not None:
        return False
    a, b = _span(board.snap, j, d, s)
    return board.overlap(d, a, b) is None


def _fit_on(board: _Board, j: Job, d: str) -> datetime | None:
    """Earliest start on dock d that is free, inside hours, and (for a kept promise) leaves its claim free too."""
    t0 = j.release
    for _ in range(2_000):
        s = board.earliest(d, t0, j.processing_min)
        if s is None or _usable(board, j, d, s):
            return s
        t0 = s + timedelta(minutes=1)
    return None


def _reserve(board: _Board, j: Job, d: str, s: datetime):
    a, b = _span(board.snap, j, d, s)
    board.take(d, a, b, j.shipment_id)


def _candidates(board: _Board, j: Job) -> list[tuple[datetime, str]]:
    out = []
    for d in j.compatible_docks:
        if d not in board.busy:
            continue
        s = _fit_on(board, j, d)
        if s is not None:
            out.append((s, d))
        if j.promise and j.promise.dock_id == d:                  # also the promised time itself, if still free
            ps = max(_ceil_min(j.release), j.promise.start)
            if ps != s and _usable(board, j, d, ps):
                out.append((ps, d))
    return out


def _best_by_score(board: _Board, j: Job) -> tuple[datetime, str, Score] | None:
    best = None
    for s, d in _candidates(board, j):
        sc = score(board.snap, j, d, s)
        promised = 0 if j.promise and j.promise.dock_id == d else 1
        key = (sc.total, s, promised, board.code[d])
        if best is None or key < best[0]:
            best = (key, s, d, sc)
    return (best[1], best[2], best[3]) if best else None


def _earliest_fit(board: _Board, j: Job) -> tuple[datetime, str] | None:
    """Earliest feasible start over the compatible docks; ties: the promised dock, then dock code (spike dispatch)."""
    best = None
    for d in j.compatible_docks:
        if d not in board.busy:
            continue
        s = _fit_on(board, j, d)
        if s is None:
            continue
        key = (s, 0 if j.promise and j.promise.dock_id == d else 1, board.code[d])
        if best is None or key < best[0]:
            best = (key, s, d)
    return (best[1], best[2]) if best else None


# ================================================================ the rules
def fcfs_key(j: Job):
    if j.arrived_at is not None:
        return (0, j.arrived_at, j.shipment_id)
    return (1, j.planned_eta or j.release, j.shipment_id)


def eta_key(j: Job):
    return (j.release, -j.weight, j.shipment_id)


def _screen(snap: FacilitySnapshot) -> tuple[list[Job], list[Unscheduled]]:
    plannable, out = [], []
    for j in sorted(snap.jobs, key=lambda x: x.shipment_id):
        aid = j.promise.appointment_id if j.promise else None
        if "CONTRADICTORY_DATA" in j.flags:
            out.append(Unscheduled(j.shipment_id, j.priority, "CONTRADICTORY_DATA",
                                   "driver or truck is assigned to an overlapping shipment; a person decides", aid))
        elif not j.compatible_docks:
            out.append(Unscheduled(j.shipment_id, j.priority, "NO_COMPATIBLE_DOCK",
                                   "no dock at this facility fits the load: " + ("; ".join(j.incompatible) or "none"), aid))
        else:
            plannable.append(j)
    return plannable, out


def _cause(snap: FacilitySnapshot, board: _Board, j: Job, d: str, s: datetime, blocked: str | None) -> str:
    """Plain-language reason for a placement (facts only, from the structured input)."""
    where = f"{board.code[d]} {hm(s)}"
    if not j.promise:
        return f"no booking yet; ready {hm(j.release)}; earliest suitable dock time {where}"
    p = j.promise
    told = f"{board.code[p.dock_id]} {hm(p.start)}" if p.dock_id in board.code else hm(p.start)
    sc = score(snap, j, d, s)
    if not sc.changed:
        at = "" if s == p.start else f", docks at {hm(s)}"
        return f"keeps its promise {told} (ready {hm(j.release)}{at})"
    if j.release > p.start + timedelta(minutes=snap.grace_min):
        return f"late: ready {hm(j.release)}, after promised {told} + {snap.grace_min} min grace; next suitable dock time {where}"
    if blocked:
        return f"promised {told} is not usable ({blocked}); next suitable dock time {where}"
    if s >= j.release > p.start and d == p.dock_id:
        return f"running late within the {snap.grace_min} min grace: ready {hm(j.release)}; same dock at {where}"
    if s < p.start:
        stay = score(snap, j, p.dock_id, p.start)
        return (f"early: ready {hm(j.release)}, promised {told}; {where} is free and lowers its score "
                f"{stay.total} → {sc.total} (saves {stay.wait_term - sc.wait_term} w·min, costs one change)")
    return f"promised {told} taken earlier in this order; next suitable dock time {where}"


def _assignment(snap, board, j: Job, d: str, s: datetime, reason: str) -> Assignment:
    sc = score(snap, j, d, s)
    action = "PLAN" if not j.promise else ("MOVE" if sc.changed else "KEEP")
    p = j.promise
    return Assignment(j.shipment_id, j.priority, d, board.code[d], s, s + timedelta(minutes=j.processing_min), j.release,
                      action, sc, reason, p.appointment_id if p else None, p.dock_id if p else None,
                      board.code.get(p.dock_id) if p else None, p.start if p else None, p.end if p else None)


def _list_dispatch(snap: FacilitySnapshot, key, rule: str) -> Proposal:
    board = _Board(snap)
    jobs, unsched = _screen(snap)
    placed = []
    for k, j in enumerate(sorted(jobs, key=key), 1):
        blocked = board.conflict(j.promise.dock_id, max(_ceil_min(j.release), j.promise.start), j.processing_min) \
            if j.promise and j.promise.dock_id in board.busy else None
        fit = _earliest_fit(board, j)
        if fit is None:
            unsched.append(Unscheduled(j.shipment_id, j.priority, "NO_CAPACITY_IN_HORIZON",
                                       f"no compatible dock free before the window ends ({hm(snap.horizon_end)})",
                                       j.promise.appointment_id if j.promise else None))
            continue
        s, d = fit
        _reserve(board, j, d, s)
        placed.append(_assignment(snap, board, j, d, s, f"{rule} position {k}: " + _cause(snap, board, j, d, s, blocked)))
    return _finish(snap, rule, placed, unsched)


def _facility_score(snap: FacilitySnapshot) -> Proposal:
    board = _Board(snap)
    jobs, unsched = _screen(snap)
    grace = timedelta(minutes=snap.grace_min)
    placed: dict[str, tuple[Job, str, datetime, str]] = {}       # sid -> (job, dock, start, reason-cause)
    blocked: dict[str, str] = {}

    # pass 1: keep promises that can still be kept
    rest = []
    for j in sorted(jobs, key=lambda x: (x.promise.start if x.promise else datetime.max.replace(tzinfo=snap.now.tzinfo),
                                         -x.weight, x.shipment_id)):
        p = j.promise
        if p and p.dock_id in j.compatible_docks and j.release <= p.start + grace:
            s = max(_ceil_min(j.release), p.start)
            why = board.conflict(p.dock_id, s, j.processing_min) or (
                None if _usable(board, j, p.dock_id, s)
                else f"occupied by {board.overlap(p.dock_id, *_span(snap, j, p.dock_id, s))}")
            if why is None:
                _reserve(board, j, p.dock_id, s)
                placed[j.shipment_id] = (j, p.dock_id, s, "pass 1")
                continue
            blocked[j.shipment_id] = why
        elif p and p.dock_id not in j.compatible_docks:
            blocked[j.shipment_id] = "promised dock does not fit this load"
        rest.append(j)

    # pass 2: dispatch the rest; trucks competing for the same dock time go by weight
    while rest:
        best = {}
        for j in rest:
            b = _best_by_score(board, j)
            if b is None:
                unsched.append(Unscheduled(j.shipment_id, j.priority, "NO_CAPACITY_IN_HORIZON",
                                           f"no compatible dock free before the window ends ({hm(snap.horizon_end)})",
                                           j.promise.appointment_id if j.promise else None))
            else:
                best[j.shipment_id] = (j, *b)
        if not best:
            break
        first = min(best.values(), key=lambda x: (x[1], board.code[x[2]], x[0].shipment_id))
        horizon = first[1] + timedelta(minutes=first[0].processing_min)
        competing = [x for x in best.values() if x[1] < horizon and first[2] in x[0].compatible_docks]
        j, s, d, _ = min(competing, key=lambda x: (-x[0].weight, x[0].release, x[0].shipment_id))
        _reserve(board, j, d, s)
        placed[j.shipment_id] = (j, d, s, "pass 2")
        rest = [x[0] for x in best.values() if x[0].shipment_id != j.shipment_id]

    # pass 3: pull early trucks forward when their own score drops
    for sid in sorted((k for k, v in placed.items() if v[3] == "pass 1"),
                      key=lambda k: (placed[k][0].release, -placed[k][0].weight, k)):
        j, d0, s0, _ = placed[sid]
        now_score = score(snap, j, d0, s0).total
        board.free(sid)
        b = _best_by_score(board, j)
        if b and b[2].total < now_score and b[0] < s0:
            s, d, _ = b
            _reserve(board, j, d, s)
            placed[sid] = (j, d, s, "pass 3")
        else:
            _reserve(board, j, d0, s0)

    out = []
    for j, d, s, ps in placed.values():
        out.append(_assignment(snap, board, j, d, s, f"{ps}: " + _cause(snap, board, j, d, s, blocked.get(j.shipment_id))))
    return _finish(snap, "FACILITY_SCORE", out, unsched)


def _apply_plan(placed: list[Assignment]) -> list[Assignment]:
    """Moves are applied one at a time, and each must find its target free in the database. A target can overlap
    the CURRENT claim of another truck this proposal also moves: that move goes first (depends_on). Apply order is a
    deterministic topological order; trucks in a cycle (a swap) get no order: one-at-a-time moves cannot do a swap,
    so a person handles it (or re-plans after one of them moves)."""
    moves = {a.shipment_id: a for a in placed if a.action == "MOVE"}
    deps = {x.shipment_id: tuple(sorted(
        y.shipment_id for y in moves.values()
        if y.shipment_id != x.shipment_id and y.promised_dock_id == x.dock_id
        and y.promised_start < x.end and x.start < y.promised_end)) for x in moves.values()}
    order, done, left = {}, set(), dict(deps)
    while left:
        ready = sorted((k for k, d in left.items() if set(d) <= done),
                       key=lambda k: (moves[k].start, moves[k].dock_code, k))
        if not ready:
            break                                                   # the rest depend on each other: a cycle
        k = ready[0]
        order[k] = len(order) + 1
        done.add(k)
        del left[k]
    return [replace(a, depends_on=deps.get(a.shipment_id, ()), apply_order=order.get(a.shipment_id)) for a in placed]


def _finish(snap: FacilitySnapshot, rule: str, placed: list[Assignment], unsched: list[Unscheduled]) -> Proposal:
    placed = _apply_plan(sorted(placed, key=lambda a: (a.start, a.dock_code, a.shipment_id)))
    unsched = sorted(unsched, key=lambda u: u.shipment_id)
    waits = [a.score.wait_min for a in placed]
    by_sid = {j.shipment_id: j for j in snap.jobs}
    m = {
        "trucks": len(snap.jobs), "planned": len(placed),
        "weighted_wait": sum(a.score.wait_term for a in placed),
        "total_wait": sum(waits), "max_wait": max(waits, default=0),
        "missed_promises": sum(a.score.missed for a in placed) + sum(1 for u in unsched if by_sid[u.shipment_id].promise),
        "changed_promises": sum(a.score.changed for a in placed),
        "kept_promises": sum(1 for a in placed if a.action == "KEEP"),
        "moves": sum(1 for a in placed if a.action == "MOVE"),
        "moves_in_a_cycle": sum(1 for a in placed if a.action == "MOVE" and a.apply_order is None),
        "unscheduled": len(unsched),
        "score": sum(a.score.total for a in placed) + sum(UNSCHEDULED_PENALTY * by_sid[u.shipment_id].weight for u in unsched),
        "fixed_tasks": len(snap.fixed),
    }
    _assert_valid(snap, placed)
    return Proposal(rule, snap.facility_id, snap.now, snap.fingerprint(), placed, unsched,
                    sorted(snap.fixed, key=lambda f: (f.dock_id, f.start, f.ref)), m, sorted(snap.out_of_horizon))


def _assert_valid(snap: FacilitySnapshot, placed: list[Assignment]):
    """Self-check of every proposal: compatible dock, not before release, no overlap with fixed work or each other."""
    jobs = {j.shipment_id: j for j in snap.jobs}
    per_dock: dict[str, list] = {}
    for f in snap.fixed:
        per_dock.setdefault(f.dock_id, []).append((f.start, f.end, f.ref))
    for a in placed:
        j = jobs[a.shipment_id]
        if a.dock_id not in j.compatible_docks or a.start < j.release - timedelta(minutes=1) or a.start < snap.now:
            raise AssertionError(f"planner produced an invalid assignment for {a.shipment_id}")
        a0, a1 = _span(snap, j, a.dock_id, a.start)
        for b0, b1, ref in per_dock.get(a.dock_id, []):
            if b0 < a1 and a0 < b1:
                raise AssertionError(f"planner overlap: {a.shipment_id} with {ref} on {a.dock_code}")
        per_dock.setdefault(a.dock_id, []).append((a0, a1, a.shipment_id))


def propose(snap: FacilitySnapshot, rule: str = "FACILITY_SCORE") -> Proposal:
    if rule == "FCFS":
        return _list_dispatch(snap, fcfs_key, "FCFS")
    if rule == "EARLIEST_ETA":
        return _list_dispatch(snap, eta_key, "EARLIEST_ETA")
    if rule == "FACILITY_SCORE":
        return _facility_score(snap)
    raise ValueError(f"unknown rule {rule!r}; one of {RULES}")


# ================================================================ snapshot from the feasibility engine's view
def _as_time(v, default: str) -> time:
    if isinstance(v, time):
        return v
    return datetime.strptime(v or default, "%H:%M").time()


def snapshot_from_data(data: FacilityData, facility_id: str, now: datetime, horizon_h: int = HORIZON_H) -> FacilitySnapshot:
    """Build the planner input from the SAME view and rules the feasibility engine uses (ETA fusion and confidence
    band, gate-to-dock, changeover, dock compatibility, committed dock time, dock events, double-assignment check).
    Works on the challenge snapshot (sqlite adapter) and on PostgreSQL (pg_view)."""
    eng = FeasibilityEngine(data)
    fac = data.facilities[facility_id]
    tm = data.timings[facility_id]
    rules = data.rules.get(facility_id, {})
    close_t = _as_time(fac.get("close_time"), "23:59")
    hours = Hours(_as_time(fac.get("open_time"), "00:00"),
                  _as_time(rules.get("LAST_NEW_START_TIME"), close_t.strftime("%H:%M")), close_t)
    grace = int(rules.get("NO_SHOW_GRACE_MIN", fac.get("checkin_grace_min", 30)))
    docks = sorted(((k, v) for k, v in data.docks.items() if v["facility_id"] == facility_id), key=lambda kv: kv[1]["dock_code"])
    horizon_end = now + timedelta(hours=horizon_h)

    appts_by_ship: dict[str, list] = {}
    for a in data.appts:
        appts_by_ship.setdefault(a["shipment_id"], []).append(a)
    live_holds = {o["shipment_id"] for o in (data.occupancy or [])
                  if o["kind"] == "HOLD" and not (o["expires_at"] and ts(o["expires_at"]) < now)}
    in_dock = {o["shipment_id"] for o in (data.occupancy or []) if o["kind"] == "FIXED"}

    jobs, movable_refs, out_of_horizon = [], set(), []
    for sid, s in sorted(data.shipments.items()):
        if s["destination_facility_id"] != facility_id or s["current_status"] in ("CANCELLED", "COMPLETED", "IN_DOCK"):
            continue
        ck = data.checkins.get(sid)
        if ck and ck.get("queue_state") in ("IN_DOCK", "COMPLETED"):
            continue                                                    # fixed: its dock time is a FixedTask below
        mine = appts_by_ship.get(sid, [])
        cur = next((a for a in mine if a.get("is_current") and a["appointment_status"] in ACTIVE), None)
        if cur and cur["appointment_status"] == "IN_PROGRESS":
            continue
        if sid in live_holds or any(a["appointment_status"] == "PENDING_CONFIRMATION" for a in mine):
            continue                                                    # a driver is mid-booking: their claim is fixed
        if sid in in_dock:
            continue                                                    # a FIXED claim: the truck is at a dock
        er = eng.effective_eta(sid, now)
        arrived = ck.get("gate_in_ts") if ck else None
        eta_hi = er.eta + timedelta(minutes=tm.eta_band[er.confidence])
        release = (max(now, ts(arrived)) if arrived else max(now, eta_hi)) + timedelta(minutes=tm.gate_to_dock_min)
        if release > horizon_end:
            out_of_horizon.append(sid)
            continue
        compat, why = [], []
        for dock_id, d in docks:
            r = eng.dock_compatible(s, d)
            (why.append(f"{d['dock_code']}: {r}") if r else compat.append(dock_id))
        promise = None
        if cur:
            pdock, pstart, pend = eng.appt_window(cur)
            promise = Promise(cur["appointment_id"], pdock, ts(pstart), ts(pend))
            movable_refs.add(cur["appointment_id"])
        flags = ("CONTRADICTORY_DATA",) if eng.assignment_conflicts(sid) else ()
        jobs.append(Job(sid, s["priority_code"], release, s["expected_unload_min"] + tm.changeover_min, tuple(compat),
                        ts(arrived) if arrived else None, ts(s["original_eta_ts"]), promise, er.source, flags, tuple(why)))

    status_by_ref = {a["appointment_id"]: a["appointment_status"] for a in data.appts}
    kind_by_ref = {o["ref_id"]: o["kind"] for o in (data.occupancy or []) if o.get("ref_id")}
    label = {"IN_PROGRESS": "IN_DOCK", "PENDING_CONFIRMATION": "REQUEST_PENDING", "CONFIRMED": "BOOKED",
             "FIXED": "IN_DOCK", "HOLD": "HOLD", "APPOINTMENT": "APPOINTMENT"}
    fixed = []
    for dock_id, _ in docks:
        for b0, b1, ref in eng.committed_intervals(dock_id, now, exclude_shipment=""):
            if ref in movable_refs or b1 <= now or b0 >= horizon_end:
                continue
            kind = label.get(kind_by_ref.get(ref) or status_by_ref.get(ref, ""), "BOOKED")
            fixed.append(FixedTask(dock_id, ts(b0), ts(b1), str(ref), kind))
        for e0, e1, name in eng.dock_events(dock_id):
            if e1 > now and e0 < horizon_end:
                fixed.append(FixedTask(dock_id, e0, min(e1, horizon_end), name.split(" ")[0], "BLOCK"))
    return FacilitySnapshot(facility_id, now, tuple(Dock(k, v["dock_code"]) for k, v in docks), tuple(jobs), tuple(fixed),
                            hours, grace, horizon_h, tuple(out_of_horizon))


def with_jobs(snap: FacilitySnapshot, jobs) -> FacilitySnapshot:
    return replace(snap, jobs=tuple(jobs))
