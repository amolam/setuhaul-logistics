"""Option D activation path: a Step 6 gate report becomes deployment evidence only for gates that PASSED, written by
the deploy-evidence identity, with the fingerprints the booking switch checks at start-up (setuhaul/ops/gate_evidence.py)."""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from setuhaul.api.inbound import BookingGateError, REQUIRED_GATES, SqsFifoQueue
from setuhaul.ops.gate_evidence import EvidenceRefused, live_fingerprints, rows_from_report, write
from test_inbound_delivery import URL, FakeSqs, app_db, booking_app, manifest

ROOT = Path(__file__).resolve().parents[1]


def report(status="PASS", **override):
    gates = {g: {"status": status, "checks": {"x": True}, "not_shown": []} for g in REQUIRED_GATES}
    for g, v in override.items():
        gates[g] = v
    return {"booking_enabled": False, "gates": gates}


def as_writer(dsn_, rows):
    with psycopg.connect(dsn_, autocommit=True) as c:
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
            return write(c, rows)


def test_only_passed_gates_become_evidence_and_the_switch_then_starts(guarded_dsn):
    dsn = guarded_dsn          # records evidence: needs a database whose guard is on (0031, M1)
    man, q = manifest(), SqsFifoQueue(URL, FakeSqs())
    fps = live_fingerprints(man, q, app_db(dsn))
    partial = report(AURORA={"status": "FAIL", "checks": {"engine is Aurora PostgreSQL 18.3+ (T33)": False}, "not_shown": []},
                     AWS_QUEUE={"status": "PARTIAL", "checks": {"x": True}, "not_shown": ["redrive drill"]},
                     OUTBOUND_DELIVERY={"status": "BLOCKED", "checks": {}, "not_shown": ["Meta test number"]})
    rows, refused = rows_from_report(partial, man, "test-env", "s3://e/run/r.json", b"{}", fps)
    assert {r["gate"] for r in rows} == {"RECOVERY_WORKER", "BOOKING_WORKFLOW", "WORKLOAD_IDENTITY"}
    assert set(refused) == {"AURORA", "AWS_QUEUE", "OUTBOUND_DELIVERY"} and "failing" in refused["AURORA"]
    as_writer(dsn, rows)
    with pytest.raises(BookingGateError, match="AWS_QUEUE: no evidence"):
        booking_app(dsn, q, man=man)                              # three refused gates keep the switch closed
    rows, refused = rows_from_report(report(), man, "test-env", "s3://e/run/r2.json", b"{}", fps)
    assert not refused and len(rows) == 6
    assert {r["validity_days"] for r in rows if r["gate"] in ("AURORA", "WORKLOAD_IDENTITY")} == {30}
    as_writer(dsn, rows)
    assert booking_app(dsn, q, man=man) is not None               # all six current and matching: it would start


def test_a_pass_with_a_failing_check_or_an_unshown_item_is_refused():
    man = manifest()
    fps = {g: "0" * 64 for g in REQUIRED_GATES}
    bad = report(AURORA={"status": "PASS", "checks": {"version": False}, "not_shown": []},
                 AWS_QUEUE={"status": "PASS", "checks": {"x": True}, "not_shown": ["drill"]})
    rows, refused = rows_from_report(bad, man, "test-env", "s3://e/r.json", b"{}", fps)
    assert {"AURORA", "AWS_QUEUE"} <= set(refused) and len(rows) == 4


def test_refusals_before_any_row():
    man, fps = manifest(), {g: "0" * 64 for g in REQUIRED_GATES}
    with pytest.raises(EvidenceRefused, match="manifest is for environment"):
        rows_from_report(report(), man, "pilot", "s3://e/r.json", b"{}", fps)
    with pytest.raises(EvidenceRefused, match="s3:// or https://"):
        rows_from_report(report(), man, "test-env", "file:///tmp/r.json", b"{}", fps)
    with pytest.raises(EvidenceRefused, match="booking switch off"):
        rows_from_report({**report(), "booking_enabled": True}, man, "test-env", "s3://e/r.json", b"{}", fps)


def test_application_roles_cannot_write_evidence(dsn):
    rows, _ = rows_from_report(report(), manifest(), "test-env", "s3://e/r.json", b"{}", {g: "0" * 64 for g in REQUIRED_GATES})
    for role in ("setuhaul_console", "setuhaul_system", "setuhaul_agent"):
        with psycopg.connect(dsn, autocommit=True) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute(f"SET LOCAL ROLE {role}")
                    write(c, rows[:1])


def test_the_live_pilot_report_yields_rows_only_for_exact_passes():
    """The report from the deployed pilot: only a gate whose status is exactly PASS (no failing check, nothing not
    shown) may become a row; every other gate, including AURORA's pilot exception, is refused."""
    path = ROOT / "docs" / "release" / "evidence" / "step6_gates.json"
    if not path.exists():
        pytest.skip("no live report in this checkout")
    live = json.loads(path.read_text(encoding="utf-8"))
    rows, refused = rows_from_report(live, manifest(), "test-env", "s3://e/r.json", path.read_bytes(),
                                     {g: "0" * 64 for g in REQUIRED_GATES})
    passed = {g for g, v in live["gates"].items()
              if v.get("status") == "PASS" and all(v.get("checks", {}).values()) and not v.get("not_shown")}
    assert {r["gate"] for r in rows} == passed
    assert set(refused) == set(REQUIRED_GATES) - passed
    assert "AURORA" in refused and "OUTBOUND_DELIVERY" in refused


def test_a_pilot_exception_is_never_written_as_gate_evidence():
    """T33-P (23 Nov 2026): the AURORA gate on the 17.9 pilot reports PASS WITH PILOT EXCEPTION. That is not a PASS:
    no evidence row is written for it, so it can never satisfy the booking switch without a separate owner decision."""
    man = manifest()
    exc = report(AURORA={"status": "PASS WITH PILOT EXCEPTION (T33-P)", "checks": {"x": True}, "not_shown": []})
    rows, refused = rows_from_report(exc, man, "test-env", "s3://e/r.json", b"{}", {g: "0" * 64 for g in REQUIRED_GATES})
    assert "AURORA" in refused and "PILOT EXCEPTION" in refused["AURORA"] and all(r["gate"] != "AURORA" for r in rows)
