"""Driver location check (roadmap Phase 4): a one-off, explicitly shared position that corroborates a delay report.

Deterministic and pure: same report + same context + same config -> same assessment. It never decides a booking,
never proves identity (the verified channel does that) and never tracks: one shared fix is assessed and stored
coarsely (a 7-character geohash, about 150 m), and the precise coordinates are dropped.

Assessments:
  VERIFIED      the fix is valid, fresh, accurate enough and consistent with the journey facts we actually hold
  NEEDS_REVIEW  valid and fresh, but a fact contradicts it or the journey context needed to judge it is missing
  UNAVAILABLE   the driver did not share a location (declined, or nothing usable arrived); a delay report still stands
  INVALID       coordinates out of range, a timestamp in the future, or a fix too old or too imprecise to use

No route is invented. With no facility position on record, consistency cannot be judged and the answer is
NEEDS_REVIEW, never VERIFIED.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ASSESSOR_VERSION = "location-v1"
VERIFIED, NEEDS_REVIEW, UNAVAILABLE, INVALID = "VERIFIED", "NEEDS_REVIEW", "UNAVAILABLE", "INVALID"
_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"


@dataclass(frozen=True)
class LocationConfig:
    max_age_s: int = 600                 # SETUHAUL_DRIVER_LOCATION_MAX_AGE_S
    max_accuracy_m: float = 1000.0       # SETUHAUL_DRIVER_LOCATION_MAX_ACCURACY_M
    proximity_km: float = 2.0            # SETUHAUL_DRIVER_LOCATION_PROXIMITY_M / 1000: "at the facility"
    max_truck_speed_kmh: float = 80.0    # the ETA fusion's limit (domain/eta.py FusionConfig)
    future_skew_s: int = 120             # a fix this far in the future is a bad clock or a forged timestamp

    @classmethod
    def from_env(cls, env: dict | None = None) -> "LocationConfig":
        e = os.environ if env is None else env
        return cls(max_age_s=int(e.get("SETUHAUL_DRIVER_LOCATION_MAX_AGE_S", "600")),
                   max_accuracy_m=float(e.get("SETUHAUL_DRIVER_LOCATION_MAX_ACCURACY_M", "1000")),
                   proximity_km=float(e.get("SETUHAUL_DRIVER_LOCATION_PROXIMITY_M", "2000")) / 1000)


@dataclass(frozen=True)
class LocationReport:
    shared: bool
    latitude: float | None = None
    longitude: float | None = None
    observed_at: datetime | None = None
    accuracy_m: float | None = None


@dataclass(frozen=True)
class JourneyContext:
    """Only facts the database holds. Any of them may be missing."""
    has_shipment: bool
    facility_position: tuple[float, float] | None = None
    declared_eta: datetime | None = None
    previous_geohash: str | None = None
    previous_observed_at: datetime | None = None


@dataclass(frozen=True)
class Assessment:
    assessment: str
    reasons: tuple[str, ...] = ()
    geohash: str | None = None
    distance_to_facility_km: float | None = None
    observed_at: datetime | None = None
    accuracy_m: float | None = None
    version: str = ASSESSOR_VERSION
    shared: bool = True

    def as_record(self, source: str) -> dict:
        return {"source": source, "shared": self.shared, "assessment": self.assessment, "reasons": list(self.reasons),
                "position_geohash": self.geohash, "observed_at": self.observed_at.isoformat() if self.observed_at else None,
                "accuracy_m": self.accuracy_m,
                "distance_to_facility_km": round(self.distance_to_facility_km, 1)
                if self.distance_to_facility_km is not None else None,
                "assessor_version": self.version}


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = map(lambda p: (math.radians(p[0]), math.radians(p[1])), (a, b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def geohash(lat: float, lon: float, precision: int = 7) -> str:
    lat_r, lon_r, out, bit, ch, even = [-90.0, 90.0], [-180.0, 180.0], [], 0, 0, True
    while len(out) < precision:
        r, v = (lon_r, lon) if even else (lat_r, lat)
        mid = (r[0] + r[1]) / 2
        if v >= mid:
            ch, r[0] = ch * 2 + 1, mid
        else:
            ch, r[1] = ch * 2, mid
        even, bit = not even, bit + 1
        if bit == 5:
            out.append(_B32[ch])
            bit, ch = 0, 0
    return "".join(out)


def geohash_center(gh: str) -> tuple[float, float]:
    lat_r, lon_r, even = [-90.0, 90.0], [-180.0, 180.0], True
    for c in gh:
        v = _B32.index(c)
        for i in range(4, -1, -1):
            r = lon_r if even else lat_r
            mid = (r[0] + r[1]) / 2
            if (v >> i) & 1:
                r[0] = mid
            else:
                r[1] = mid
            even = not even
    return (lat_r[0] + lat_r[1]) / 2, (lon_r[0] + lon_r[1]) / 2


def _valid_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def assess(report: LocationReport, ctx: JourneyContext, now: datetime, cfg: LocationConfig = LocationConfig()) -> Assessment:
    if not report.shared:
        return Assessment(UNAVAILABLE, ("DRIVER_DID_NOT_SHARE",), shared=False)
    lat, lon = report.latitude, report.longitude
    if not (_valid_number(lat) and _valid_number(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180) \
            or (lat == 0 and lon == 0):
        return Assessment(INVALID, ("BAD_COORDINATES",))
    acc = report.accuracy_m
    if acc is not None and (not _valid_number(acc) or acc < 0):
        return Assessment(INVALID, ("BAD_ACCURACY",))
    obs = report.observed_at
    if obs is None or obs.tzinfo is None:
        return Assessment(INVALID, ("NO_TIMESTAMP",))
    gh = geohash(lat, lon)                                       # from here on only the coarse position is kept
    base = dict(geohash=gh, observed_at=obs, accuracy_m=acc)
    if obs > now + timedelta(seconds=cfg.future_skew_s):
        return Assessment(INVALID, ("TIMESTAMP_IN_FUTURE",), **base)
    if now - obs > timedelta(seconds=cfg.max_age_s):
        return Assessment(INVALID, ("STALE_LOCATION",), **base)
    if acc is not None and acc > cfg.max_accuracy_m:
        return Assessment(INVALID, ("POOR_ACCURACY",), **base)

    reasons: list[str] = []
    if acc is None:
        reasons.append("ACCURACY_UNKNOWN")
    if not ctx.has_shipment:
        return Assessment(NEEDS_REVIEW, tuple(reasons + ["NO_SHIPMENT_CONTEXT"]), **base)
    if ctx.previous_geohash and ctx.previous_observed_at:        # the same impossible-speed rule as the ETA fusion
        hours = (obs - ctx.previous_observed_at).total_seconds() / 3600
        d = km(geohash_center(ctx.previous_geohash), (lat, lon))
        if (hours <= 0 and d > 0.5) or (hours > 0 and d / hours > cfg.max_truck_speed_kmh):
            reasons.append("IMPOSSIBLE_MOVEMENT_SINCE_LAST_SHARE")
    if ctx.facility_position is None:
        return Assessment(NEEDS_REVIEW, tuple(reasons + ["NO_FACILITY_POSITION"]), **base)
    dist = km((lat, lon), ctx.facility_position)
    base["distance_to_facility_km"] = dist
    if ctx.declared_eta is not None:
        hours_left = (ctx.declared_eta - now).total_seconds() / 3600
        if dist > cfg.proximity_km and hours_left > 0 and dist / hours_left > cfg.max_truck_speed_kmh:
            reasons.append("CANNOT_ARRIVE_BY_DECLARED_ETA")
        elif dist > cfg.proximity_km and hours_left <= 0:
            reasons.append("DECLARED_ETA_PASSED_BUT_FAR_AWAY")
        elif dist <= cfg.proximity_km and hours_left * 60 > 90:
            reasons.append("AT_FACILITY_BUT_REPORTS_LONG_DELAY")
    else:
        reasons.append("NO_DECLARED_ETA")
    hard = {"IMPOSSIBLE_MOVEMENT_SINCE_LAST_SHARE", "CANNOT_ARRIVE_BY_DECLARED_ETA", "DECLARED_ETA_PASSED_BUT_FAR_AWAY",
            "AT_FACILITY_BUT_REPORTS_LONG_DELAY", "NO_DECLARED_ETA"}
    return Assessment(NEEDS_REVIEW if hard & set(reasons) else VERIFIED, tuple(reasons), **base)
