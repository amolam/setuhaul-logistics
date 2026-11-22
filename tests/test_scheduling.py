"""Facility scheduling tool (setuhaul/domain/scheduling.py): pure, deterministic, proposal only.

No database, no network, no model. The scenario (early truck, late truck, truck already unloading) is the one
scripts/scheduling_compare.py prints and docs/3-build/SCHEDULING_TOOL.md reports.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from setuhaul.adapters.sqlite_snapshot import load
from setuhaul.domain.scheduling import (NOT_A_BOOKING, RULES, STATUS, Dock, FacilitySnapshot, FixedTask, Hours, Job,
                                        Promise, propose, snapshot_from_data, with_jobs)

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("scheduling_compare", ROOT / "scripts" / "scheduling_compare.py")
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)
at = compare.at
JAI_NOW = datetime.fromisoformat("2026-08-04T09:56:00+05:30")


@pytest.fixture(scope="module")
def scenario() -> FacilitySnapshot:
    return compare.scenario()


@pytest.fixture(scope="module")
def jai() -> FacilitySnapshot:
    return snapshot_from_data(load(), "FAC-JAI-01", JAI_NOW)


def overlaps(p, snap):
    per = {}
    for f in snap.fixed:
        per.setdefault(f.dock_id, []).append((f.start, f.end, f.ref))
    bad = []
    for a in p.assignments:
        for b0, b1, ref in per.get(a.dock_id, []):
            if b0 < a.end and a.start < b1:
                bad.append((a.shipment_id, ref))
        per.setdefault(a.dock_id, []).append((a.start, a.end, a.shipment_id))
    return bad


# ---------------------------------------------------------------- the contract: a proposal, never a booking
@pytest.mark.parametrize("rule", RULES)
def test_output_is_marked_as_a_proposal_not_a_booking(scenario, rule):
    d = propose(scenario, rule).to_dict()
    assert d["status"] == STATUS == "PROPOSAL" and d["not_a_booking"] is True and d["note"] == NOT_A_BOOKING
    assert "proposal_id" not in d                       # only the console's stored copy gets an id a move can use
    assert len(d["input_fingerprint"]) == 64 and d["rule"] == rule
    for a in d["assignments"]:
        s = a["score"]
        assert s["total"] == s["wait_term"] + s["lateness_term"] + s["change_term"]
        assert a["reason"] and a["action"] in ("KEEP", "MOVE", "PLAN")


def test_unknown_rule_is_refused(scenario):
    with pytest.raises(ValueError):
        propose(scenario, "WHATEVER_THE_DRIVER_SAID")


def test_the_input_has_no_free_text_field():
    """Break case S1: a driver's words cannot reach the ordering, because no input field carries text."""
    text_like = {"text", "message", "body", "note", "comment", "driver_message", "notes"}
    for cls in (Job, Promise, FixedTask, FacilitySnapshot, Dock):
        assert not ({f.name for f in dataclasses.fields(cls)} & text_like), cls


# ---------------------------------------------------------------- determinism and tie-breaking
@pytest.mark.parametrize("rule", RULES)
def test_equal_input_gives_equal_output(scenario, jai, rule):
    for snap in (scenario, jai):
        a = json.dumps(propose(snap, rule).to_dict(), sort_keys=True)
        b = json.dumps(propose(snap, rule).to_dict(), sort_keys=True)
        shuffled = with_jobs(snap, reversed(snap.jobs))           # input order does not matter
        c = json.dumps(propose(shuffled, rule).to_dict(), sort_keys=True)
        assert a == b == c


@pytest.mark.parametrize("rule", RULES)
def test_ties_break_by_shipment_id(rule):
    D = ("K1",)
    twin = lambda sid: Job(sid, "NORMAL", at("09:00"), 30, D, planned_eta=at("09:00"))
    base = FacilitySnapshot("F", at("09:00"), (Dock("K1", "D1"),), (twin("SHP-B"), twin("SHP-A")), (),
                            Hours(time(6), time(21), time(22)))
    p = propose(base, rule)
    assert [a.shipment_id for a in p.assignments] == ["SHP-A", "SHP-B"]
    assert propose(with_jobs(base, reversed(base.jobs)), rule).to_dict() == p.to_dict()


def test_fingerprint_changes_when_the_input_changes(scenario):
    fp = scenario.fingerprint()
    j0 = scenario.jobs[0]
    assert with_jobs(scenario, scenario.jobs).fingerprint() == fp
    assert with_jobs(scenario, (dataclasses.replace(j0, release=j0.release + timedelta(minutes=1)),) + scenario.jobs[1:]).fingerprint() != fp
    assert with_jobs(scenario, (dataclasses.replace(j0, priority="CRITICAL"),) + scenario.jobs[1:]).fingerprint() != fp
    assert dataclasses.replace(scenario, fixed=()).fingerprint() != fp
    assert dataclasses.replace(scenario, now=scenario.now + timedelta(minutes=1)).fingerprint() != fp


# ---------------------------------------------------------------- hard rules of every proposal
@pytest.mark.parametrize("rule", RULES)
def test_no_overlaps_and_compatibility_respected(scenario, jai, rule):
    for snap in (scenario, jai):
        p = propose(snap, rule)
        assert overlaps(p, snap) == []
        jobs = {j.shipment_id: j for j in snap.jobs}
        for a in p.assignments:
            j = jobs[a.shipment_id]
            assert a.dock_id in j.compatible_docks
            assert a.start >= j.release - timedelta(minutes=1) and a.start >= snap.now
            assert a.end == a.start + timedelta(minutes=j.processing_min)
        assert {a.shipment_id for a in p.assignments}.isdisjoint({u.shipment_id for u in p.unscheduled})
        assert len(p.assignments) + len(p.unscheduled) == len(snap.jobs)          # every truck is accounted for


@pytest.mark.parametrize("rule", RULES)
def test_fixed_tasks_are_never_moved(scenario, jai, rule):
    for snap in (scenario, jai):
        p = propose(snap, rule)
        assert p.fixed == sorted(snap.fixed, key=lambda f: (f.dock_id, f.start, f.ref))
        fixed_refs = {f.ref for f in snap.fixed}
        assert not fixed_refs & {a.appointment_id for a in p.assignments}


def test_reefer_and_heavy_loads_go_only_to_their_docks(jai):
    for rule in RULES:
        p = propose(jai, rule)
        dock = {a.shipment_id: a.dock_code for a in p.assignments}
        assert dock.get("SHP1010") == "D5"            # reefer
        assert dock.get("SHP1011") == "D6"            # heavy


def test_the_challenge_snapshot_inputs(jai):
    ids = {j.shipment_id for j in jai.jobs}
    assert "SHP1002" not in ids                       # unloading in D2: a fixed task, not a job
    assert {"SHP1001", "SHP1008", "SHP1019"}.isdisjoint(ids)                    # completed / cancelled
    assert {"SHP1013", "SHP1014"}.isdisjoint(ids)     # a pending request is the driver's claim: fixed
    kinds = {(f.ref, f.kind) for f in jai.fixed}
    assert ("APT1002", "IN_DOCK") in kinds and ("DEVT001", "BLOCK") in kinds
    assert ("APT1013A", "REQUEST_PENDING") in kinds


# ---------------------------------------------------------------- infeasible trucks are left for a person
def test_infeasible_trucks_are_unscheduled_never_invented():
    D = ("K1",)
    snap = FacilitySnapshot(
        "F", at("09:00"), (Dock("K1", "D1"),),
        (Job("SHP-NODOCK", "HIGH", at("09:00"), 30, (), incompatible=("D1: no refrigeration",)),
         Job("SHP-NOROOM", "NORMAL", at("09:00"), 30, D),
         Job("SHP-DOUBLE", "CRITICAL", at("09:00"), 30, D, flags=("CONTRADICTORY_DATA",)),
         Job("SHP-TOOLATE", "LOW", at("20:50"), 120, D)),             # cannot end before the 22:00 close
        (FixedTask("K1", at("08:00"), at("21:30"), "DEVT9", "BLOCK"),),
        Hours(time(6), time(21), time(22)), horizon_h=12)
    for rule in RULES:
        p = propose(snap, rule)
        assert p.assignments == []
        assert {u.shipment_id: u.reason_code for u in p.unscheduled} == {
            "SHP-NODOCK": "NO_COMPATIBLE_DOCK", "SHP-NOROOM": "NO_CAPACITY_IN_HORIZON",
            "SHP-DOUBLE": "CONTRADICTORY_DATA", "SHP-TOOLATE": "NO_CAPACITY_IN_HORIZON"}
        assert p.metrics["unscheduled"] == 4 and p.metrics["score"] == 2000 * (4 + 2 + 8 + 1)


def test_last_new_start_time_is_respected():
    snap = FacilitySnapshot("F", at("20:30"), (Dock("K1", "D1"),),
                            (Job("SHP-1", "NORMAL", at("20:30"), 30, ("K1",)),),
                            (FixedTask("K1", at("20:00"), at("21:10"), "X", "IN_DOCK"),), Hours(time(6), time(21), time(22)),
                            horizon_h=2)
    for rule in RULES:
        p = propose(snap, rule)
        assert [u.reason_code for u in p.unscheduled] == ["NO_CAPACITY_IN_HORIZON"]       # 21:10 > 21:00 last start


# ---------------------------------------------------------------- the brief's case: early, late and in-progress trucks
def test_early_late_and_in_progress_trucks_share_one_facility(scenario):
    props = {r: propose(scenario, r) for r in RULES}
    for r, p in props.items():
        by = {a.shipment_id: a for a in p.assignments}
        # in progress: D1 is busy until 09:40 and nothing is placed there before
        assert all(not (a.dock_id == "DK1" and a.start < at("09:40")) for a in p.assignments), r
        assert p.fixed == list(scenario.fixed)
        # late: never before it can be there; its promise (D1 09:45) is missed under every rule
        assert by["SHP-LATE"].start >= at("10:35") and by["SHP-LATE"].score.missed, r
        assert p.metrics["unscheduled"] == 0 and p.metrics["missed_promises"] == 1, r
    fcfs, eta, score = (props[r] for r in ("FCFS", "EARLIEST_ETA", "FACILITY_SCORE"))
    # FCFS serves the early truck at once (it came first) and pays with other trucks' promises
    assert {a.shipment_id: (a.dock_code, a.start) for a in fcfs.assignments}["SHP-EARLY"] == ("D2", at("09:00"))
    assert fcfs.metrics["changed_promises"] == 5
    # the facility score keeps every promise it can and changes only the late truck's
    s = {a.shipment_id: a for a in score.assignments}
    assert {k for k, a in s.items() if a.action == "KEEP"} == {"SHP-A", "SHP-D", "SHP-B", "SHP-EARLY"}
    assert s["SHP-LATE"].action == "MOVE" and score.metrics["changed_promises"] == 1
    # ... and it is honest about the price: more waiting than the simple rules (the early truck waits for its slot)
    assert score.metrics["weighted_wait"] > max(fcfs.metrics["weighted_wait"], eta.metrics["weighted_wait"])
    assert score.metrics["score"] < min(fcfs.metrics["score"], eta.metrics["score"])
    assert (fcfs.metrics["weighted_wait"], eta.metrics["weighted_wait"], score.metrics["weighted_wait"]) == (95, 112, 185)
    assert (fcfs.metrics["score"], eta.metrics["score"], score.metrics["score"]) == (725, 796, 615)


def test_moves_carry_a_safe_apply_order_and_swaps_go_to_a_person(scenario):
    """One move at a time must find its target free in the database; a target inside another moved truck's CURRENT
    claim waits for that move (depends_on); a cycle cannot be applied one at a time at all."""
    fcfs = {a.shipment_id: a for a in propose(scenario, "FCFS").moves}
    assert fcfs["SHP-B"].apply_order == 1 and fcfs["SHP-LATE"].depends_on == ("SHP-B",) and fcfs["SHP-LATE"].apply_order == 2
    assert {k for k, a in fcfs.items() if a.apply_order is None} == {"SHP-EARLY", "SHP-A", "SHP-D"}   # A <-> D swap
    score = propose(scenario, "FACILITY_SCORE")
    assert [(a.shipment_id, a.depends_on, a.apply_order) for a in score.moves] == [("SHP-LATE", (), 1)]
    assert score.metrics["moves_in_a_cycle"] == 0
    for rule in RULES:                                   # a KEPT promise keeps its claim: nothing is planned into it
        p = propose(scenario, rule)
        keeps = [a for a in p.assignments if a.action == "KEEP"]
        for a in p.assignments:
            for k in keeps:
                if k is not a and k.dock_id == a.dock_id:
                    assert not (k.promised_start < a.end and a.start < k.promised_end), (rule, a.shipment_id, k.shipment_id)


def test_an_early_truck_is_pulled_forward_only_when_its_score_drops():
    D = ("K1", "K2")
    early = Job("SHP-E", "NORMAL", at("09:00"), 30, D, arrived_at=at("08:50"),
                promise=Promise("APT-E", "K1", at("11:00"), at("11:30")))
    snap = FacilitySnapshot("F", at("09:00"), (Dock("K1", "D1"), Dock("K2", "D2")), (early,), (),
                            Hours(time(6), time(21), time(22)))
    a = propose(snap, "FACILITY_SCORE").assignments[0]
    assert (a.action, a.start, a.score.total) == ("MOVE", at("09:00"), 30)       # 120 min x 2 > one change (30)
    lowprio = dataclasses.replace(early, priority="LOW", release=at("10:40"))    # 20 min x 1 < 30: stays
    a = propose(with_jobs(snap, (lowprio,)), "FACILITY_SCORE").assignments[0]
    assert (a.action, a.start) == ("KEEP", at("11:00"))


def test_a_promise_on_a_blocked_dock_is_replanned_with_its_reason():
    D = ("K1", "K2")
    j = Job("SHP-1", "HIGH", at("09:00"), 60, D, promise=Promise("APT-1", "K1", at("09:30"), at("10:30")))
    snap = FacilitySnapshot("F", at("09:00"), (Dock("K1", "D1"), Dock("K2", "D2")), (j,),
                            (FixedTask("K1", at("09:00"), at("12:00"), "DEVT7", "BLOCK"),), Hours(time(6), time(21), time(22)))
    a = propose(snap, "FACILITY_SCORE").assignments[0]
    assert a.dock_code == "D2" and a.action == "MOVE" and "not usable" in a.reason and "DEVT7" in a.reason
