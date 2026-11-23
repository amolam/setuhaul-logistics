"""Phase G1: the deterministic slot optimizer (setuhaul/domain/optimizer.py). Pure: no database, network or model.

Each scenario builds a small facility in the PostgreSQL-shaped view (occupancy ledger) so every hard constraint can be
switched on or off precisely. Two property tests (the trainer snapshot, and a seeded random fuzz) assert that NO
returned option ever breaks a hard constraint, checked by an oracle written independently of the optimizer.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from setuhaul.adapters.sqlite_snapshot import load
from setuhaul.domain.feasibility import FacilityData, FacilityTimings
from setuhaul.domain.optimizer import (ERROR, FEASIBLE, HARD_CONSTRAINTS, INFEASIBLE, MODEL_INVALID, OPTIMAL,
                                       STALE_DATA, UNKNOWN, OptimizationRequest, OptimizerConfig, SlotOptimizer,
                                       Weights, validate_options)

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 12, 2, tzinfo=IST)
JAI, GGN = "FAC-JAI-01", "FAC-GGN-01"


def at(h: int, m: int = 0, day: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=h, minutes=m)


def ship(sid, fac=JAI, eta=at(10), unload=45, prio="NORMAL", reefer=False, kg=10000, dock_type="ANY", status="IN_TRANSIT",
         driver=None, vehicle=None):
    return {"shipment_id": sid, "destination_facility_id": fac, "temperature_control_required": reefer,
            "required_dock_type": dock_type, "load_weight_kg": kg, "current_status": status,
            "driver_id": driver or f"DRV-{sid}", "vehicle_id": vehicle or f"VEH-{sid}", "actual_departure_ts": None,
            "planned_departure_ts": eta - timedelta(hours=5), "latest_eta_ts": None, "original_eta_ts": eta,
            "expected_unload_min": unload, "priority_code": prio}


def world(shipments, occupancy=(), events=(), appts=(), checkins=None, eta_updates=(), days=2, ggn=True,
          rules=None, blocked=()) -> FacilityData:
    docks = {"DOCK-JAI-D1": {"facility_id": JAI, "dock_code": "D1", "dock_type": "STANDARD", "max_vehicle_weight_kg": 20000,
                             "supports_refrigerated": False},
             "DOCK-JAI-D2": {"facility_id": JAI, "dock_code": "D2", "dock_type": "STANDARD", "max_vehicle_weight_kg": 30000,
                             "supports_refrigerated": False},
             "DOCK-JAI-D3": {"facility_id": JAI, "dock_code": "D3", "dock_type": "REEFER", "max_vehicle_weight_kg": 30000,
                             "supports_refrigerated": True},
             "DOCK-JAI-D4": {"facility_id": JAI, "dock_code": "D4", "dock_type": "HEAVY", "max_vehicle_weight_kg": 45000,
                             "supports_refrigerated": False}}
    if ggn:
        docks["DOCK-GGN-D1"] = {"facility_id": GGN, "dock_code": "D1", "dock_type": "STANDARD",
                                "max_vehicle_weight_kg": 30000, "supports_refrigerated": False}
    slots = []
    for dock_id, d in docks.items():
        for day in range(days):
            for h in range(6, 22):
                s = at(h, day=day)
                sid = f"S-{dock_id}-{day}-{h}"
                slots.append({"slot_id": sid, "dock_id": dock_id, "facility_id": d["facility_id"], "slot_start_ts": s,
                              "slot_end_ts": s + timedelta(hours=1),
                              "slot_status": "BLOCKED" if (dock_id, day, h) in blocked else "OPEN",
                              "block_reason": "test block" if (dock_id, day, h) in blocked else None})
    facilities = {JAI: {"facility_id": JAI, "open_time": "06:00", "close_time": "22:00", "checkin_grace_min": 30, "shifts": []},
                  GGN: {"facility_id": GGN, "open_time": "06:00", "close_time": "22:00", "checkin_grace_min": 20, "shifts": []}}
    return FacilityData(shipments={s["shipment_id"]: s for s in shipments}, docks=docks, facilities=facilities, slots=slots,
                        appts=list(appts), events=list(events), checkins=checkins or {}, eta_updates=list(eta_updates),
                        rules=rules or {JAI: {"LAST_NEW_START_TIME": "21:00", "HEAVY_DOCK_REQUIRED_KG": 25000}, GGN: {}},
                        timings={JAI: FacilityTimings(10, 10), GGN: FacilityTimings(15, 10)},
                        occupancy=list(occupancy))


def claim(dock, start, end, ref, kind="BOOKED", sid=None):
    return {"dock_id": dock, "kind": kind, "shipment_id": sid or f"OTHER-{ref}", "ref_id": ref, "expires_at": None,
            "start": start, "end": end}


def run(data, sid="S1", now=at(8), cfg=OptimizerConfig(), **req):
    return SlotOptimizer(data, cfg).optimize(OptimizationRequest(sid, **req), now)


# ---------------------------------------------------------------- independent oracle (not the optimizer's validator)
def oracle_violations(data: FacilityData, sid: str, now: datetime, result, earliest=None, latest=None) -> list[str]:
    s = data.shipments[sid]
    fac = s["destination_facility_id"]
    tm = data.timings[fac]
    out = []
    for o in result.options:
        d = data.docks[o.dock_id]
        if d["facility_id"] != fac:
            out.append(f"{o.dock_code}: other facility")
        if s["temperature_control_required"] and not d["supports_refrigerated"]:
            out.append(f"{o.dock_code}: not reefer")
        if s["load_weight_kg"] > d["max_vehicle_weight_kg"]:
            out.append(f"{o.dock_code}: overweight")
        f = data.facilities[fac]
        day = o.start.date()
        open_dt = datetime.combine(day, datetime.strptime(f["open_time"], "%H:%M").time(), o.start.tzinfo)
        close_dt = datetime.combine(day, datetime.strptime(f["close_time"], "%H:%M").time(), o.start.tzinfo)
        if o.start < open_dt or o.end > close_dt:
            out.append(f"{o.dock_code} {o.start}: outside hours")
        if (o.end - o.start) < timedelta(minutes=s["expected_unload_min"] + tm.changeover_min):
            out.append(f"{o.dock_code}: too short")
        if o.start < now:
            out.append(f"{o.dock_code}: in the past")
        if earliest and o.start < earliest or latest and o.end > latest:
            out.append(f"{o.dock_code}: outside the driver's window")
        for c in data.occupancy or []:
            if c["dock_id"] == o.dock_id and c["shipment_id"] != sid and c["start"] < o.end and o.start < c["end"]:
                out.append(f"{o.dock_code} {o.start}: overlaps {c['ref_id']}")
        for e in data.events:
            end = e["event_end_ts"] or datetime.max.replace(tzinfo=o.start.tzinfo)
            if e["dock_id"] == o.dock_id and e["event_start_ts"] < o.end and o.start < end:
                out.append(f"{o.dock_code} {o.start}: dock event {e['dock_event_id']}")
        for x in o.slots:
            sl = data.slot_by_id[x]
            if sl["slot_status"] != "OPEN" or sl["dock_id"] != o.dock_id:
                out.append(f"{o.dock_code}: slot {x} not usable")
    return out


def assert_safe(data, sid, now, r, **kw):
    assert oracle_violations(data, sid, now, r, **kw) == []
    ready = datetime.fromisoformat(r.ready_at) if r.ready_at else now
    assert validate_options(SlotOptimizer(data), sid, now, r.options, ready, kw.get("earliest"), kw.get("latest")) == []


# ---------------------------------------------------------------- scenarios
def test_normal_arrival_is_optimal_ranked_and_structured():
    data = world([ship("S1", eta=at(10))])
    r = run(data)
    assert r.solver_status == OPTIMAL and r.outcome_code == "OPTIONS" and r.recommended_action == "OFFER"
    assert 1 <= len(r.options) <= 3 and [o.rank for o in r.options] == list(range(1, len(r.options) + 1))
    assert [o.score for o in r.options] == sorted(o.score for o in r.options)
    assert all(o.start >= datetime.fromisoformat(r.ready_at) for o in r.options)
    assert {o.dock_code for o in r.options} <= {"D1", "D2"}              # STANDARD cargo: never reefer or heavy here
    d = r.to_dict()
    for k in ("request_id", "facility_id", "shipment_id", "input_eta", "options", "rejected", "constraints_evaluated",
              "solver_status", "duration_ms", "data_as_of", "input_fingerprint", "solver", "solver_version", "config"):
        assert k in d
    assert set(d["constraints_evaluated"]) == set(HARD_CONSTRAINTS) and len(HARD_CONSTRAINTS) == 12
    assert all({"dock_code", "start", "end", "wait_min", "duration_min", "feasible", "score", "reasons"} <= set(o)
               for o in d["options"])
    assert d["not_a_booking"] is True
    assert_safe(data, "S1", at(8), r)


def test_delayed_truck_gets_later_options():
    on_time = run(world([ship("S1", eta=at(10))]))
    late = run(world([ship("S1", eta=at(10))], eta_updates=[{"shipment_id": "S1", "source_type": "DRIVER_DECLARED",
                                                              "created_at": at(7, 50), "declared_eta_ts": at(14),
                                                              "confidence_code": "HIGH"}]))
    assert late.solver_status == OPTIMAL
    assert min(o.start for o in late.options) >= at(14) > min(o.start for o in on_time.options)


def test_early_arrival_at_the_gate_is_served_from_now():
    data = world([ship("S1", eta=at(15), status="AT_GATE")],
                 checkins={"S1": {"gate_in_ts": at(8, 5), "gate_out_ts": None, "unload_start_ts": None, "queue_state": "WAITING"}})
    r = run(data, now=at(8, 10))
    assert r.solver_status == OPTIMAL and r.options[0].start < at(15)       # served long before its planned ETA
    assert r.options[0].start >= at(8, 20)                                   # gate-to-dock 10 min after now
    assert_safe(data, "S1", at(8, 10), r)


def test_late_truck_moving_a_confirmed_appointment_is_scored_as_disruption():
    appt = {"appointment_id": "APT-1", "shipment_id": "S1", "dock_id": "DOCK-JAI-D1", "start": at(9), "end": at(10),
            "appointment_status": "CONFIRMED", "is_current": True}
    data = world([ship("S1", eta=at(9))], appts=[appt], occupancy=[claim("DOCK-JAI-D1", at(9), at(10), "APT-1", sid="S1")],
                 eta_updates=[{"shipment_id": "S1", "source_type": "DRIVER_DECLARED", "created_at": at(8),
                               "declared_eta_ts": at(12), "confidence_code": "HIGH"}])
    r = run(data, now=at(8, 30))
    assert r.solver_status == OPTIMAL
    assert all(o.score_terms["disruption"] > 0 for o in r.options)          # the 09:00 promise is lost: every option moves
    assert any("changes the confirmed appointment" in x for x in r.options[0].reasons)


def test_full_docks_no_feasible_slot_escalates():
    occ = [claim(d, at(6), at(22, day=1) + timedelta(hours=2), f"FULL-{d}") for d in ("DOCK-JAI-D1", "DOCK-JAI-D2")]
    occ += [claim(d, at(6, day=1), at(22, day=1), f"FULL2-{d}") for d in ("DOCK-JAI-D1", "DOCK-JAI-D2")]
    data = world([ship("S1")], occupancy=occ)
    r = run(data)
    assert r.solver_status == INFEASIBLE and r.outcome_code == "NO_FEASIBLE_SLOT" and r.recommended_action == "ESCALATE"
    assert r.options == [] and r.rejected                                    # every alternative has a reason


def test_maintenance_window_excludes_the_dock_and_says_why():
    ev = {"dock_event_id": "DE-1", "dock_id": "DOCK-JAI-D1", "event_type": "MAINTENANCE", "event_start_ts": at(9),
          "event_end_ts": at(22)}
    data = world([ship("S1")], events=[ev])
    r = run(data)
    assert r.solver_status == OPTIMAL and all(o.dock_code != "D1" or o.start.date() != at(9).date() for o in r.options)
    assert any(x["dock_code"] == "D1" and "MAINTENANCE" in x["reason"] for x in r.rejected)
    assert_safe(data, "S1", at(8), r)


def test_breakdown_open_ended_takes_the_dock_out_of_service():
    ev = {"dock_event_id": "DE-2", "dock_id": "DOCK-JAI-D2", "event_type": "BREAKDOWN", "event_start_ts": at(7),
          "event_end_ts": None}
    data = world([ship("S1")], events=[ev])
    r = run(data)
    assert r.options and all(o.dock_code != "D2" for o in r.options)
    assert_safe(data, "S1", at(8), r)


def test_labour_shortage_rejects_slots_without_a_free_crew():
    # one crew on the day shift; D2 is busy 10:00-14:00, so D1 has no crew then (HC07), only after 14:00
    cfg = OptimizerConfig(labour={JAI: [{"start": "06:00", "end": "22:00", "crews": 1}]})
    data = world([ship("S1", eta=at(10))], occupancy=[claim("DOCK-JAI-D2", at(10), at(14), "BUSY")])
    r = run(data, cfg=cfg)
    assert r.solver_status == OPTIMAL and all(o.start >= at(14) for o in r.options)
    assert any("HC07" in x.get("failed", []) for x in r.rejected)
    unlimited = run(data)                                                   # default: one crew per dock, never binding
    assert min(o.start for o in unlimited.options) < at(14)


def test_no_crew_rostered_means_no_option():
    cfg = OptimizerConfig(labour={JAI: [{"start": "06:00", "end": "22:00", "crews": 0}]})
    r = run(world([ship("S1", eta=at(10))]), cfg=cfg)
    assert r.solver_status == INFEASIBLE and r.options == []


def test_slot_must_lie_inside_a_rostered_shift():
    # crews only 12:00-14:00 and 06:00-07:00: a 55-minute slot must fit entirely inside one of them
    cfg = OptimizerConfig(max_options=20, labour={JAI: [{"start": "12:00", "end": "14:00", "crews": 2},
                                                        {"start": "06:00", "end": "07:00", "crews": 2}]})
    r = run(world([ship("S1", eta=at(10))]), cfg=cfg)
    assert r.options and all(o.start.hour in (12, 13, 6) for o in r.options)


def test_labour_from_facility_config_shifts():
    data = world([ship("S1", eta=at(10))], occupancy=[claim("DOCK-JAI-D2", at(10), at(14), "BUSY")])
    data.facilities[JAI]["shifts"] = [{"name": "DAY", "start": "06:00", "end": "22:00", "crews": 1}]
    r = run(data)
    assert all(o.start >= at(14) for o in r.options)


def test_facility_capacity_limit_is_a_hard_constraint():
    cfg = OptimizerConfig(capacity={JAI: 1})
    data = world([ship("S1", eta=at(10))], occupancy=[claim("DOCK-JAI-D2", at(10), at(13), "BUSY")])
    r = run(data, cfg=cfg)
    assert all(o.start >= at(13) for o in r.options) and any("HC10" in x.get("failed", []) for x in r.rejected)


def test_incompatible_cargo_reefer_only_on_reefer_and_heavy_only_on_heavy():
    data = world([ship("R1", reefer=True), ship("H1", kg=40000)])
    r = run(data, sid="R1")
    assert r.options and {o.dock_code for o in r.options} == {"D3"}
    h = run(data, sid="H1")
    assert h.options and {o.dock_code for o in h.options} == {"D4"}
    assert any("REEFER" in x["reason"] or "refrigeration" in x["reason"] for x in r.rejected if x["dock_code"] == "D1")
    assert_safe(data, "R1", at(8), r) or assert_safe(data, "H1", at(8), h)


def test_perishable_time_critical_cargo_prefers_the_earliest_slot():
    # D1 is busy most of the day (high utilisation); D2 frees up only at 13:00. With a heavy utilisation weight a LOW
    # priority dry load is steered to the empty dock later; perishable CRITICAL cargo still takes the earliest slot.
    w = Weights(utilisation_per_pct=30)
    occ = [claim("DOCK-JAI-D1", at(6), at(10), "A"), claim("DOCK-JAI-D1", at(12), at(21), "B"),
           claim("DOCK-JAI-D2", at(6), at(13), "C")]
    dry = world([ship("S1", eta=at(9, 30), prio="LOW")], occupancy=occ)
    cold = world([ship("S1", eta=at(9, 30), prio="CRITICAL", reefer=False)], occupancy=occ)
    r_dry = run(dry, cfg=OptimizerConfig(weights=w))
    r_cold = run(cold, cfg=OptimizerConfig(weights=w))
    assert r_dry.options[0].start > r_cold.options[0].start
    assert r_cold.options[0].start == at(10) and r_cold.options[0].dock_code == "D1"
    # the perishable factor doubles the wait cost of a reefer load
    reefer = run(world([ship("S1", eta=at(9, 30), reefer=True)]))
    normal = run(world([ship("S1", eta=at(9, 30))]))
    assert reefer.options[0].score_terms["wait"] == 2 * normal.options[0].score_terms["wait"] or \
        normal.options[0].score_terms["wait"] == 0


def test_competing_trucks_never_get_overlapping_options_once_one_is_booked():
    data = world([ship("S1", eta=at(10)), ship("S2", eta=at(10))])
    a, b = run(data, sid="S1"), run(data, sid="S2")
    assert (a.options[0].dock_id, a.options[0].start) == (b.options[0].dock_id, b.options[0].start)   # same best slot
    # S1 books it (the database decides); S2's next recommendation must not overlap it
    top = a.options[0]
    data2 = world([ship("S1", eta=at(10)), ship("S2", eta=at(10))],
                  occupancy=[claim(top.dock_id, top.start, top.end, "APT-S1", sid="S1")])
    b2 = run(data2, sid="S2")
    assert all(not (o.dock_id == top.dock_id and o.start < top.end and top.start < o.end) for o in b2.options)
    assert_safe(data2, "S2", at(8), b2)


def test_contradictory_data_goes_to_a_person():
    data = world([ship("S1", eta=at(10), driver="D-X"), ship("S2", eta=at(10, 30), driver="D-X")])
    r = run(data)
    assert r.solver_status == MODEL_INVALID and r.outcome_code == "CONTRADICTORY_DATA" and r.recommended_action == "ESCALATE"


def test_closing_time_rolls_to_the_next_operating_day():
    data = world([ship("S1", eta=at(21, 15))])
    r = run(data, now=at(20, 30))
    assert r.options and all(o.start.date() == at(6, day=1).date() for o in r.options)
    assert any("close" in x["reason"] or "contiguous" in x["reason"] or "ready" in x["reason"] for x in r.rejected)
    assert_safe(data, "S1", at(20, 30), r)


def test_after_last_new_start_needs_approval_and_counts_overtime():
    data = world([ship("S1", eta=at(20, 30))], days=1)
    r = run(data, now=at(19))
    assert r.options and all(o.needs_approval for o in r.options) and r.recommended_action == "OFFER_WITH_APPROVAL"
    assert all(o.score_terms["overtime"] > 0 for o in r.options)


def test_driver_window_is_respected():
    data = world([ship("S1", eta=at(9))])
    r = run(data, earliest_start=at(13), latest_finish=at(16))
    assert r.options and all(at(13) <= o.start and o.end <= at(16) for o in r.options)
    assert_safe(data, "S1", at(8), r, earliest=at(13), latest=at(16))


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d.shipments.pop("S1"), "UNKNOWN_SHIPMENT"),
    (lambda d: d.timings.pop(JAI), "FACILITY_NOT_CONFIGURED"),
    (lambda d: d.shipments["S1"].__setitem__("expected_unload_min", 0), "BAD_DURATION"),
])
def test_missing_or_bad_data_is_model_invalid(mutate, code):
    data = world([ship("S1")])
    mutate(data)
    r = run(data)
    assert r.solver_status == MODEL_INVALID and r.outcome_code == code and r.options == []
    assert r.recommended_action == "ESCALATE"


def test_inconsistent_inputs_are_refused():
    data = world([ship("S1")])
    assert run(data, earliest_start=at(14), latest_finish=at(12)).outcome_code == "INCONSISTENT_WINDOW"
    assert run(data, facility_id=GGN).outcome_code == "FACILITY_MISMATCH"


def test_stale_data_returns_no_options_and_asks_for_a_refresh():
    r = run(world([ship("S1")]), data_as_of=at(8) - timedelta(minutes=10))
    assert r.solver_status == STALE_DATA and r.options == [] and r.recommended_action == "REFRESH_DATA"


def test_stale_eta_is_flagged_but_still_answered():
    data = world([ship("S1", eta=at(12))], eta_updates=[{"shipment_id": "S1", "source_type": "DRIVER_DECLARED",
                                                         "created_at": at(2), "declared_eta_ts": at(12),
                                                         "confidence_code": "MEDIUM"}])
    r = run(data)
    assert r.options and any(w.startswith("ETA_STALE") for w in r.warnings)


def test_timeout_returns_feasible_or_unknown_never_unchecked_options():
    ticks = iter(range(0, 10**6))
    clock = lambda: next(ticks) * 0.001                                   # 1 ms per clock read
    cfg = OptimizerConfig(time_limit_ms=4)
    data = world([ship("S1")])
    r = SlotOptimizer(data, cfg, clock=clock).optimize(OptimizationRequest("S1"), at(8))
    assert r.solver_status in (FEASIBLE, UNKNOWN) and any("time limit" in w for w in r.warnings)
    assert r.candidates_evaluated < 20
    assert_safe(data, "S1", at(8), r)
    ticks2 = iter(range(0, 10**6, 1000))                                  # 1 s per read: nothing evaluated
    r2 = SlotOptimizer(data, cfg, clock=lambda: next(ticks2) * 0.001).optimize(OptimizationRequest("S1"), at(8))
    assert r2.solver_status == UNKNOWN and r2.options == [] and r2.recommended_action == "ESCALATE"


def test_engine_failure_is_a_safe_error():
    data = world([ship("S1")])
    opt = SlotOptimizer(data)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    opt.engine.find_options = boom
    r = opt.optimize(OptimizationRequest("S1"), at(8))
    assert r.solver_status == ERROR and r.options == [] and r.recommended_action == "ESCALATE"
    assert "disk on fire" not in " ".join(r.warnings)                       # no internals leak


def test_not_eligible_and_fixed_trucks_are_not_planned():
    data = world([ship("S1", status="CANCELLED"), ship("S2", status="IN_DOCK")],
                 checkins={"S2": {"gate_in_ts": at(7), "gate_out_ts": None, "unload_start_ts": None, "queue_state": "IN_DOCK"}})
    assert run(data, sid="S1").outcome_code == "NOT_ELIGIBLE"
    assert run(data, sid="S2").outcome_code == "FIXED"


def test_facility_isolation_other_facility_data_is_never_used():
    occ = [claim("DOCK-GGN-D1", at(6), at(22), "GGN-BUSY")]
    data = world([ship("S1", fac=GGN), ship("J1")], occupancy=occ + [claim("DOCK-JAI-D1", at(10), at(11), "JAI-X")])
    j = run(data, sid="J1")
    assert all(data.docks[o.dock_id]["facility_id"] == JAI for o in j.options)
    g = run(data, sid="S1")                                                  # GGN's only dock is full all day
    assert all(data.docks[o.dock_id]["facility_id"] == GGN for o in g.options)
    assert all(x["dock_code"] == "D1" for x in g.rejected)                   # JAI docks are not even considered
    opt = SlotOptimizer(data)
    hc = opt.check("J1", JAI, "DOCK-GGN-D1", at(12), at(13), at(8), at(8), None, None)
    assert not hc["HC01"][0] and not hc["HC12"][0]


def test_blocked_slot_is_not_operational():
    data = world([ship("S1", eta=at(9))], blocked={("DOCK-JAI-D1", 0, 10), ("DOCK-JAI-D2", 0, 10)})
    r = run(data)
    assert all(o.start != at(10) for o in r.options)


def test_deterministic_and_reproducible():
    data = world([ship("S1"), ship("S2", eta=at(11))], occupancy=[claim("DOCK-JAI-D1", at(11), at(12), "X")])
    a, b = run(data, request_id="R-1"), run(data, request_id="R-1")
    da, db = a.to_dict(), b.to_dict()
    da.pop("duration_ms"), db.pop("duration_ms")
    assert da == db and a.input_fingerprint == b.input_fingerprint and len(a.input_fingerprint) == 64
    assert a.config["seed"] == 0 and a.solver == "exhaustive-enumeration"


def test_property_trainer_snapshot_never_returns_an_infeasible_option():
    """The trainer's dataset: every shipment at eight clock times, plus driver windows. Zero violations."""
    data = load()
    data.facilities = {k: dict(v, shifts=[]) for k, v in data.facilities.items()}
    T = lambda h: datetime.fromisoformat(f"2026-08-04T{h}:00+05:30")
    opt = SlotOptimizer(data, OptimizerConfig(max_options=50))
    checked = statuses = 0
    for sid in sorted(data.shipments):
        for h in ("07:00", "08:30", "09:35", "10:00", "11:15", "13:00", "16:26", "19:00"):
            for es, lf in ((None, None), (T("12:00"), None), (None, T("15:30"))):
                r = opt.optimize(OptimizationRequest(sid, earliest_start=es, latest_finish=lf), T(h))
                statuses += 1
                if r.options:
                    ready = datetime.fromisoformat(r.ready_at)
                    assert validate_options(opt, sid, T(h), r.options, ready, es, lf) == [], (sid, h)
                    fac = data.shipments[sid]["destination_facility_id"]
                    for o in r.options:
                        d = data.docks[o.dock_id]
                        assert d["facility_id"] == fac and o.start >= ready
                        assert opt.engine.dock_compatible(data.shipments[sid], d) is None
                        checked += 1
    assert statuses == len(data.shipments) * 24 and checked > 100


def test_property_random_worlds_never_return_an_infeasible_option():
    rng = random.Random(20261202)                                           # fixed seed: reproducible
    checked = 0
    for case in range(150):
        ships = [ship(f"S{i}", eta=at(rng.randint(6, 21), rng.choice((0, 15, 30, 45))), unload=rng.choice((30, 45, 60, 90, 150)),
                      prio=rng.choice(("LOW", "NORMAL", "HIGH", "CRITICAL")), reefer=rng.random() < 0.2,
                      kg=rng.choice((8000, 15000, 22000, 40000))) for i in range(3)]
        occ = []
        for _ in range(rng.randint(0, 8)):
            d = rng.choice(["DOCK-JAI-D1", "DOCK-JAI-D2", "DOCK-JAI-D3", "DOCK-JAI-D4"])
            s = at(rng.randint(6, 20), day=rng.randint(0, 1))
            occ.append(claim(d, s, s + timedelta(minutes=rng.choice((30, 60, 120, 240))), f"C{len(occ)}"))
        evs = [{"dock_event_id": f"E{case}", "dock_id": rng.choice(["DOCK-JAI-D1", "DOCK-JAI-D2", "DOCK-JAI-D3"]),
                "event_type": rng.choice(("MAINTENANCE", "BREAKDOWN", "MANUAL_BLOCK")),
                "event_start_ts": at(rng.randint(6, 20)), "event_end_ts": at(rng.randint(6, 20), day=1)}] if rng.random() < 0.5 else []
        crews = rng.choice((None, 1, 2))
        cfg = OptimizerConfig(max_options=10, labour={JAI: [{"start": "06:00", "end": "22:00", "crews": crews}]} if crews else {},
                              capacity={JAI: rng.choice((2, 3, 4))})
        data = world(ships, occupancy=occ, events=evs)
        now = at(rng.randint(6, 19), rng.choice((0, 20, 40)))
        es = at(rng.randint(8, 18)) if rng.random() < 0.3 else None
        for s in ships:
            r = SlotOptimizer(data, cfg).optimize(OptimizationRequest(s["shipment_id"], earliest_start=es), now)
            assert r.solver_status in (OPTIMAL, INFEASIBLE, MODEL_INVALID), r.solver_status
            assert oracle_violations(data, s["shipment_id"], now, r, earliest=es) == []
            if r.options:
                opt = SlotOptimizer(data, cfg)
                assert validate_options(opt, s["shipment_id"], now, r.options, datetime.fromisoformat(r.ready_at), es) == []
                for o in r.options:                                          # labour and capacity, independently
                    busy = sum(1 for c in occ if c["start"] < o.end and o.start < c["end"])
                    if crews:
                        assert busy < crews or any(c["start"] >= o.start or c["end"] <= o.end for c in occ)
                checked += len(r.options)
    assert checked > 200
