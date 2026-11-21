"""Deterministic ETA fusion (`fuse_eta`), per Phase 1 `04_eta_signals_and_tracking.md` §3.

No LLM is involved. The model only *extracts* what the driver said; this function decides what we believe.
Pure function: same signals + same `now` + same config → same answer. Unit- and property-tested.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Highest trust first (Phase 1 §3 precedence).
EVIDENCE_SOURCES = ("TELEMATICS", "LOCATION_PIN", "SIM_PING", "FASTAG")
ALL_SOURCES = ("GATE_IN", "OPERATIONS_OVERRIDE", *EVIDENCE_SOURCES, "WAREHOUSE_ESTIMATE", "DRIVER_DECLARED", "ORIGINAL_PLAN")
LEVELS = ("LOW", "MEDIUM", "HIGH")


@dataclass(frozen=True)
class Signal:
    source: str
    observed_at: datetime
    derived_eta: datetime                    # for evidence sources: position + route time, already computed
    uncertainty_min: int = 0
    confidence: str | None = None            # explicit only for human sources (driver precise/vague, warehouse, plan)
    position: tuple[float, float] | None = None   # (lat, lon), never sent to the LLM

    def __post_init__(self):
        if self.source not in ALL_SOURCES:
            raise ValueError(f"unknown source {self.source}")
        if self.confidence is not None and self.confidence not in LEVELS:
            raise ValueError(f"bad confidence {self.confidence}")


@dataclass(frozen=True)
class FusionConfig:
    fresh_min: dict = field(default_factory=lambda: {"TELEMATICS": 15, "LOCATION_PIN": 30, "SIM_PING": 45, "FASTAG": 240})
    max_truck_speed_kmh: float = 80.0
    optimistic_tolerance_min: int = 15       # driver earlier than evidence by more than this → evidence wins
    pessimistic_ask_min: int = 60            # driver later than evidence by more than this → keep driver, ask once
    eta_conflict_min: int = 30               # agreement threshold that lifts SIM / FASTag to HIGH
    signal_stale_min: int = 120              # newest evidence older than this → confidence down one level
    default_confidence: dict = field(default_factory=lambda: {
        "OPERATIONS_OVERRIDE": "HIGH", "WAREHOUSE_ESTIMATE": "MEDIUM", "DRIVER_DECLARED": "MEDIUM", "ORIGINAL_PLAN": "MEDIUM"})


@dataclass(frozen=True)
class EtaResult:
    eta: datetime
    source: str
    confidence: str
    uncertainty_min: int
    flags: tuple[str, ...] = ()
    evidence_eta: datetime | None = None
    driver_eta: datetime | None = None

    @property
    def needs_driver_question(self) -> bool:
        return "PESSIMISTIC_DECLARATION" in self.flags


def _down(level: str) -> str:
    return LEVELS[max(0, LEVELS.index(level) - 1)]


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = map(lambda p: (math.radians(p[0]), math.radians(p[1])), (a, b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _drop_impossible(signals: list[Signal], cfg: FusionConfig) -> tuple[list[Signal], bool]:
    """Implied speed between consecutive positioned fixes above the limit → drop the newer fix."""
    fixes = sorted((s for s in signals if s.position is not None), key=lambda s: s.observed_at)
    dropped: set[int] = set()
    last = None
    for s in fixes:
        if last is not None:
            hours = (s.observed_at - last.observed_at).total_seconds() / 3600
            km = _km(last.position, s.position)
            if (hours <= 0 and km > 0.5) or (hours > 0 and km / hours > cfg.max_truck_speed_kmh):
                dropped.add(id(s))
                continue
        last = s
    return [s for s in signals if id(s) not in dropped], bool(dropped)


def _latest(signals: list[Signal], source: str) -> Signal | None:
    c = [s for s in signals if s.source == source]
    # total order, so the answer never depends on the order signals arrived in
    return max(c, key=lambda s: (s.observed_at, s.derived_eta, s.uncertainty_min, s.confidence or "")) if c else None


def fuse_eta(signals: list[Signal], now: datetime, cfg: FusionConfig = FusionConfig()) -> EtaResult:
    """Choose the effective ETA, its confidence and flags from all signals observed up to `now`."""
    usable = [s for s in signals if s.observed_at <= now]          # never look into the future (replays, tests)
    if not usable:
        raise ValueError("no signals: every shipment has at least ORIGINAL_PLAN")
    usable, inconsistent = _drop_impossible(usable, cfg)
    flags = ["SIGNAL_INCONSISTENT"] if inconsistent else []

    gate = _latest(usable, "GATE_IN")
    if gate:                                                        # ground truth: the truck is here
        return EtaResult(gate.observed_at, "GATE_IN", "HIGH", 0, tuple(flags))
    ops = _latest(usable, "OPERATIONS_OVERRIDE")
    if ops:                                                         # a human decision outranks devices
        return EtaResult(ops.derived_eta, "OPERATIONS_OVERRIDE", ops.confidence or "HIGH", ops.uncertainty_min, tuple(flags))

    driver = _latest(usable, "DRIVER_DECLARED")
    evidence = None
    for src in EVIDENCE_SOURCES:                                    # freshest valid signal from the best source
        s = _latest(usable, src)
        if s and now - s.observed_at <= timedelta(minutes=cfg.fresh_min[src]):
            evidence = s
            break

    if evidence:
        conf = "HIGH" if evidence.source in ("TELEMATICS", "LOCATION_PIN") else "MEDIUM"
        if driver and abs((driver.derived_eta - evidence.derived_eta).total_seconds()) <= cfg.eta_conflict_min * 60:
            conf = "HIGH"                                           # independent sources agree
        if now - evidence.observed_at > timedelta(minutes=cfg.signal_stale_min):
            conf = _down(conf)
            flags.append("STALE_EVIDENCE")
        eta, src, unc = evidence.derived_eta, evidence.source, evidence.uncertainty_min
        if driver:
            gap = (driver.derived_eta - evidence.derived_eta).total_seconds() / 60
            if gap < -cfg.optimistic_tolerance_min:                 # driver says earlier than physically likely
                conf = "LOW"
                flags.append("OPTIMISTIC_DECLARATION")
            elif gap > cfg.pessimistic_ask_min:                     # driver may know of a stop or repair
                eta, src, unc, conf = driver.derived_eta, "DRIVER_DECLARED", driver.uncertainty_min, "LOW"
                flags.append("PESSIMISTIC_DECLARATION")
        return EtaResult(eta, src, conf, unc, tuple(flags), evidence.derived_eta, driver.derived_eta if driver else None)

    for src in ("WAREHOUSE_ESTIMATE", "DRIVER_DECLARED", "ORIGINAL_PLAN"):
        s = _latest(usable, src)
        if s:
            conf = s.confidence or cfg.default_confidence[src]
            return EtaResult(s.derived_eta, src, conf, s.uncertainty_min, tuple(flags), None,
                             driver.derived_eta if driver else None)
    raise ValueError("only evidence signals that are too old, and no plan")
