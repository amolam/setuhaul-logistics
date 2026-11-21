"""fuse_eta: Phase 1 §3 rules as tests, plus randomized property checks (stdlib random, fixed seeds)."""
import random
from datetime import datetime, timedelta

import pytest

from setuhaul.domain.eta import FusionConfig, Signal, fuse_eta

NOW = datetime.fromisoformat("2026-08-04T10:00:00+05:30")
PLAN = Signal("ORIGINAL_PLAN", NOW - timedelta(hours=6), NOW + timedelta(hours=1))


def m(x):
    return timedelta(minutes=x)


def test_plan_only():
    r = fuse_eta([PLAN], NOW)
    assert (r.eta, r.source, r.confidence) == (NOW + m(60), "ORIGINAL_PLAN", "MEDIUM")


def test_gate_in_beats_everything():
    r = fuse_eta([PLAN, Signal("DRIVER_DECLARED", NOW - m(5), NOW + m(90)),
                  Signal("GATE_IN", NOW - m(2), NOW - m(2))], NOW)
    assert (r.source, r.confidence, r.eta) == ("GATE_IN", "HIGH", NOW - m(2))


def test_operations_override_beats_devices():
    r = fuse_eta([PLAN, Signal("TELEMATICS", NOW - m(1), NOW + m(30)),
                  Signal("OPERATIONS_OVERRIDE", NOW - m(10), NOW + m(75))], NOW)
    assert r.source == "OPERATIONS_OVERRIDE" and r.eta == NOW + m(75)


def test_fresh_telematics_is_high():
    r = fuse_eta([PLAN, Signal("TELEMATICS", NOW - m(5), NOW + m(40))], NOW)
    assert (r.source, r.confidence) == ("TELEMATICS", "HIGH")


def test_stale_telematics_is_ignored_and_pin_used():
    r = fuse_eta([PLAN, Signal("TELEMATICS", NOW - m(20), NOW + m(40)),
                  Signal("LOCATION_PIN", NOW - m(10), NOW + m(50))], NOW)
    assert r.source == "LOCATION_PIN"


def test_fastag_medium_raised_to_high_when_driver_agrees():
    fast = Signal("FASTAG", NOW - m(20), NOW + m(80))
    assert fuse_eta([PLAN, fast], NOW).confidence == "MEDIUM"
    r = fuse_eta([PLAN, fast, Signal("DRIVER_DECLARED", NOW - m(3), NOW + m(90))], NOW)
    assert (r.source, r.confidence) == ("FASTAG", "HIGH")


def test_optimistic_driver_loses_to_evidence():
    """Driver says 10:30 but the last toll crossing implies 11:30 → plan for 11:30, LOW, flagged."""
    r = fuse_eta([PLAN, Signal("FASTAG", NOW - m(10), NOW + m(90)),
                  Signal("DRIVER_DECLARED", NOW - m(2), NOW + m(30))], NOW)
    assert r.eta == NOW + m(90) and r.confidence == "LOW" and "OPTIMISTIC_DECLARATION" in r.flags


def test_pessimistic_driver_is_kept_but_asked():
    r = fuse_eta([PLAN, Signal("LOCATION_PIN", NOW - m(5), NOW + m(30)),
                  Signal("DRIVER_DECLARED", NOW - m(2), NOW + m(150))], NOW)
    assert r.source == "DRIVER_DECLARED" and r.eta == NOW + m(150) and r.needs_driver_question


def test_stale_evidence_downgrades_confidence():
    cfg = FusionConfig(fresh_min={"TELEMATICS": 15, "LOCATION_PIN": 30, "SIM_PING": 45, "FASTAG": 300}, signal_stale_min=120)
    r = fuse_eta([PLAN, Signal("FASTAG", NOW - m(200), NOW + m(60))], NOW, cfg)
    assert r.confidence == "LOW" and "STALE_EVIDENCE" in r.flags


def test_impossible_jump_dropped():
    a = Signal("TELEMATICS", NOW - m(14), NOW + m(60), position=(26.9, 75.8))     # Jaipur
    b = Signal("TELEMATICS", NOW - m(4), NOW + m(5), position=(28.45, 77.0))       # Gurugram 10 min later
    r = fuse_eta([PLAN, a, b], NOW)
    assert "SIGNAL_INCONSISTENT" in r.flags and r.eta == NOW + m(60)


def test_future_signals_ignored():
    r = fuse_eta([PLAN, Signal("DRIVER_DECLARED", NOW + m(5), NOW + m(200))], NOW)
    assert r.source == "ORIGINAL_PLAN"


def test_driver_confidence_comes_from_extraction_not_default():
    r = fuse_eta([PLAN, Signal("DRIVER_DECLARED", NOW - m(1), NOW + m(120), confidence="LOW")], NOW)
    assert (r.source, r.confidence) == ("DRIVER_DECLARED", "LOW")


def test_unknown_source_rejected():
    with pytest.raises(ValueError):
        Signal("TELEPATHY", NOW, NOW)


@pytest.mark.parametrize("seed", range(40))
def test_properties_random(seed):
    """For any mix of signals: deterministic, never uses a future signal, confidence is valid,
    and a gate-in always wins."""
    rnd = random.Random(seed)
    sources = ["TELEMATICS", "LOCATION_PIN", "SIM_PING", "FASTAG", "WAREHOUSE_ESTIMATE", "DRIVER_DECLARED"]
    sig = [PLAN] + [Signal(rnd.choice(sources), NOW - m(rnd.randint(-30, 300)), NOW + m(rnd.randint(-30, 300)),
                           confidence=rnd.choice([None, "LOW", "MEDIUM", "HIGH"]))
                    for _ in range(rnd.randint(0, 8))]
    a, b = fuse_eta(sig, NOW), fuse_eta(list(reversed(sig)), NOW)
    assert a == b                                                    # order-independent
    assert a.confidence in ("LOW", "MEDIUM", "HIGH")
    used = [s for s in sig if s.source == a.source and s.observed_at <= NOW]
    assert used, "result must come from a signal observed by now"
    g = Signal("GATE_IN", NOW - m(1), NOW - m(1))
    assert fuse_eta(sig + [g], NOW).source == "GATE_IN"
