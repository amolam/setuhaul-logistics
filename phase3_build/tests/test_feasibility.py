"""Feasibility engine: differential test against the Phase 1 prototype, worked examples, and invariants."""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from setuhaul.adapters.sqlite_snapshot import load
from setuhaul.domain.eta import FusionConfig
from setuhaul.domain.feasibility import FeasibilityEngine, Option, ts

ROOT = Path(__file__).resolve().parents[2]
T = lambda h: datetime.fromisoformat(f"2026-08-04T{h}:00+05:30")
# The prototype treated the plan as HIGH confidence; the production default is MEDIUM (Phase 1 §1: S0 is
# "low once the trip starts"). For the equivalence proof the engine is run with the prototype's setting.
PROTO_FUSION = FusionConfig(default_confidence={"OPERATIONS_OVERRIDE": "HIGH", "WAREHOUSE_ESTIMATE": "MEDIUM",
                                                "DRIVER_DECLARED": "MEDIUM", "ORIGINAL_PLAN": "HIGH"})


@pytest.fixture(scope="module")
def data():
    return load()


@pytest.fixture(scope="module")
def proto():
    spec = importlib.util.spec_from_file_location("proto", ROOT / "phase1_frame" / "feasibility_prototype.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["proto"] = mod                       # dataclasses resolve types through sys.modules
    spec.loader.exec_module(mod)
    return mod.Snapshot(mod.DB)


def test_differential_against_phase1_prototype(data, proto):
    """Every shipment × 8 clock times × 3 preference cases: identical options and outcomes."""
    eng = FeasibilityEngine(data, PROTO_FUSION)
    cases = 0
    for sid in sorted(data.shipments):
        for h in ("07:00", "08:30", "09:35", "10:00", "11:15", "13:00", "16:26", "19:00"):
            for es, lf in ((None, None), (T("12:00"), None), (None, T("13:30"))):
                a = eng.find_options(sid, T(h), earliest_start=es, latest_finish=lf)
                b = proto.find_options(sid, T(h), earliest_start=es, latest_finish=lf)
                assert a.outcome == b.outcome, (sid, h, es, lf)
                assert [(o.dock, o.start, o.end, list(o.slots), o.needs_approval) for o in a.options] == \
                       [(o.dock, o.start, o.end, o.slots, o.needs_approval) for o in b.options], (sid, h)
                assert a.rejections == b.rejections, (sid, h)
                cases += 1
    assert cases == len(data.shipments) * 24


def test_worked_example_shp1015_reefer_escalates(data):
    r = FeasibilityEngine(data, PROTO_FUSION).find_options("SHP1015", T("16:26"))
    assert r.outcome_code == "NO_FEASIBLE_SLOT" and not r.options


def test_worked_example_shp1014_never_d1(data):
    """21 t never goes to D1 (20 t limit): the pending APT1014A request must not be confirmed (A03)."""
    r = FeasibilityEngine(data, PROTO_FUSION).find_options("SHP1014", T("09:52"), max_options=50)
    assert all(o.dock != "D1" for o in r.options)
    assert any("kg > dock limit" in x for x in r.rejections.get("D1", []))


def test_worked_example_shp1006_after_12(data):
    r = FeasibilityEngine(data, PROTO_FUSION).find_options("SHP1006", T("09:35"), earliest_start=T("12:00"))
    assert r.options and all(o.start >= T("12:00") for o in r.options)


@pytest.mark.parametrize("h", ["08:00", "10:00", "12:30", "15:00"])
def test_invariants_on_every_option(data, h):
    """Any option offered: compatible dock, starts after ready, no overlap with committed work or dock
    events, ends before close, contiguous slots."""
    eng = FeasibilityEngine(data)          # production fusion defaults
    now = T(h)
    for sid, s in data.shipments.items():
        r = eng.find_options(sid, now, max_options=100)
        for o in r.options:
            dock = next(d for d in data.docks.values() if d["dock_code"] == o.dock and d["facility_id"] == s["destination_facility_id"])
            assert eng.dock_compatible(s, dock) is None
            ready = datetime.combine(now.date(), datetime.strptime(r.facts["ready_ts"], "%H:%M").time(), now.tzinfo)
            assert o.start + timedelta(minutes=60) > ready
            for b0, b1, _ in eng.committed_intervals(dock["dock_id"], now, sid):
                assert not (b0 < o.end and o.start < b1)
            for e0, e1, _ in eng.dock_events(dock["dock_id"]):
                assert not (e0 < o.end and o.start < e1)
            close = datetime.combine(o.start.date(), datetime.strptime(data.facilities[s["destination_facility_id"]]["close_time"], "%H:%M").time(), now.tzinfo)
            assert o.end <= close
            sl = [data.slot_by_id[x] for x in o.slots]
            assert all(ts(sl[i]["slot_end_ts"]) == ts(sl[i + 1]["slot_start_ts"]) for i in range(len(sl) - 1))


def test_is_feasible_detects_staleness(data):
    eng = FeasibilityEngine(data, PROTO_FUSION)
    now = T("09:35")
    sid, r = next((sid, r) for sid in sorted(data.shipments)
                  if (r := eng.find_options(sid, now)).outcome_code == "OPTIONS")
    o = r.options[0]
    assert eng.is_feasible(sid, o, now) == (True, "still feasible")
    late = o.start + timedelta(minutes=61)               # the option's start has passed
    ok, why = eng.is_feasible(sid, o, late)
    assert not ok and why
    bogus = Option(o.dock, o.start + timedelta(minutes=7), o.end, o.slots, False)
    assert eng.is_feasible(sid, bogus, now)[0] is False


def test_is_feasible_refuses_contradictory_data(data):
    eng = FeasibilityEngine(data, PROTO_FUSION)
    r = eng.find_options("SHP1006", T("09:35"), earliest_start=T("12:00"))
    assert r.outcome_code == "CONTRADICTORY_DATA"
    ok, why = eng.is_feasible("SHP1006", r.options[0], T("09:35"))
    assert not ok and "CONTRADICTORY_DATA" in why


def test_assignment_conflicts_escalate(data):
    """SHP1006 / SHP1021: double-assigned driver or truck → CONTRADICTORY_DATA, never bookable options."""
    eng = FeasibilityEngine(data, PROTO_FUSION)
    codes = {sid: eng.find_options(sid, T("10:00")).outcome_code for sid in data.shipments}
    flagged = [sid for sid, c in codes.items() if c == "CONTRADICTORY_DATA"]
    assert flagged, "the snapshot contains known double assignments (audit A01 / A02)"


# ---------------------------------------------------------------- owner decision OD-1: finish-based grace (closes S3-02)
# At risk when ready + occupancy > slot end + grace (Phase 1, 02_variables.md). Grace no longer applies to dock arrival.
# SHP1017: D1 12:00-13:00, unload 50 min; changeover set to 0 so occupancy is 50; gate-to-dock 10; grace 30; the ETA is
# an operations override (HIGH confidence, no band). So: finish = ETA + 10 + 50, deadline = 13:00 + 30 = 13:30.
from setuhaul.domain.eta import EtaResult  # noqa: E402
from setuhaul.domain.feasibility import FacilityTimings, is_no_show  # noqa: E402


def at_risk_for(eta_hhmm: str) -> bool:
    d = load(timings={"FAC-JAI-01": FacilityTimings(gate_to_dock_min=10, changeover_min=0),
                      "FAC-GGN-01": FacilityTimings(gate_to_dock_min=15, changeover_min=0)})
    eng = FeasibilityEngine(d, PROTO_FUSION)
    eng.effective_eta = lambda sid, now: EtaResult(T(eta_hhmm), "OPERATIONS_OVERRIDE", "HIGH", 0)
    return eng.find_options("SHP1017", T("10:00"), max_options=1).facts["appointment_at_risk"]


@pytest.mark.parametrize("eta,risk,case", [
    ("12:29", False, "finish 13:29, one minute before the grace deadline: keep the slot"),
    ("12:30", False, "finish 13:30, exactly at the grace deadline: keep the slot"),
    ("12:31", True, "finish 13:31, one minute after the grace deadline: at risk"),
    ("12:25", False, "at the dock 12:35, after slot start + grace, but finishing 13:25: keep the slot"),
])
def test_od1_grace_applies_to_the_finish(eta, risk, case):
    assert at_risk_for(eta) is risk, case


@pytest.mark.parametrize("gate_in,now,no_show,case", [
    ("12:30", "12:45", False, "check-in exactly at the no-show deadline: not a no-show (strict deadline)"),
    ("12:31", "12:45", True, "check-in one minute after the deadline: no-show"),
    (None, "12:31", True, "no check-in after the deadline: no-show"),
    (None, "12:30", False, "no check-in yet, deadline not passed: not yet"),
])
def test_od1_no_show_deadline(gate_in, now, no_show, case):
    assert is_no_show(T("12:00"), 30, T(gate_in) if gate_in else None, T(now)) is no_show, case


# ---------------------------------------------------------------- B-FEAS-1: an overnight arrival gets the next morning
def with_next_days(days=2):
    """The snapshot, plus a copy of 4 August's slots for each following day (all open)."""
    d = load()
    base = [x for x in d.slots]
    for n in range(1, days + 1):
        for x in base:
            y = dict(x, slot_id=f"{x['slot_id']}+{n}", slot_status="OPEN", block_reason=None)
            for k in ("slot_start_ts", "slot_end_ts"):
                y[k] = (ts(x[k]) + timedelta(days=n)).isoformat()
            d.slots.append(y)
    d.slots.sort(key=lambda x: (x["dock_id"], ts(x["slot_start_ts"])))
    return d


def late_engine(d, eta="23:10"):
    eng = FeasibilityEngine(d, PROTO_FUSION)
    eng.effective_eta = lambda sid, now: EtaResult(T(eta), "OPERATIONS_OVERRIDE", "HIGH", 0)
    return eng


def test_b_feas_1_a_truck_ready_late_at_night_is_offered_the_next_morning():
    r = late_engine(with_next_days()).find_options("SHP1017", T("20:00"), max_options=3)
    assert r.outcome_code == "OPTIONS", r.rejections
    assert all(o.start.date() == T("08:00").date() + timedelta(days=1) for o in r.options)
    assert min(o.start for o in r.options) == T("08:00") + timedelta(days=1)       # the first morning slot
    assert not any(o.needs_approval for o in r.options)        # morning slots: the 21:00 rule of the NEXT day applies


def test_b_feas_1_nothing_beyond_the_next_operating_day():
    r = late_engine(with_next_days()).find_options("SHP1017", T("20:00"), max_options=10**6)
    last_day = T("08:00").date() + timedelta(days=1)
    assert r.options and all(o.start.date() <= last_day for o in r.options)


def test_b_feas_1_same_day_rules_are_unchanged():
    eng = FeasibilityEngine(with_next_days(), PROTO_FUSION)
    eng.effective_eta = lambda sid, now: EtaResult(T("20:20"), "OPERATIONS_OVERRIDE", "HIGH", 0)
    r = eng.find_options("SHP1017", T("18:00"), max_options=10**6)
    same_day = [o for o in r.options if o.start.date() == T("08:00").date()]
    assert same_day and all(o.needs_approval for o in same_day if o.start >= T("21:00"))
    assert not any(o.end > T("22:00") and o.start.date() == T("08:00").date() for o in r.options)   # close holds


# ---------------------------------------------------------------- R1.9 S4-06: next OPERATING day, not next calendar day
def closed_days(d, *offsets):
    """Remove every slot on the given day offsets after 4 August: the facility is closed those days."""
    day0 = T("08:00").date()
    d.slots = [x for x in d.slots if (ts(x["slot_start_ts"]).astimezone(T("08:00").tzinfo).date() - day0).days not in offsets]
    return d


def test_s4_06_a_weekend_is_skipped_to_the_next_operating_day():
    r = late_engine(closed_days(with_next_days(4), 1, 2)).find_options("SHP1017", T("20:00"), max_options=3)
    assert r.outcome_code == "OPTIONS", r.rejections
    monday = T("08:00").date() + timedelta(days=3)
    assert all(o.start.date() == monday for o in r.options)
    assert min(o.start for o in r.options) == T("08:00") + timedelta(days=3)


def test_s4_06_only_one_operating_day_for_drivers_more_for_a_planner():
    d = closed_days(with_next_days(4), 1, 2)
    days = {o.start.date() for o in late_engine(d).find_options("SHP1017", T("20:00"), max_options=10**6).options}
    assert days == {T("08:00").date() + timedelta(days=3)}
    days = {o.start.date() for o in late_engine(d).find_options("SHP1017", T("20:00"), max_options=10**6,
                                                                operating_days_ahead=2).options}
    assert days == {T("08:00").date() + timedelta(days=3), T("08:00").date() + timedelta(days=4)}


def test_s4_06_nothing_beyond_the_search_window():
    from setuhaul.domain.feasibility import SEARCH_WINDOW_DAYS
    d = closed_days(with_next_days(SEARCH_WINDOW_DAYS + 1), *range(1, SEARCH_WINDOW_DAYS + 1))
    r = late_engine(d).find_options("SHP1017", T("20:00"), max_options=3)
    assert r.outcome_code != "OPTIONS" and not r.options

