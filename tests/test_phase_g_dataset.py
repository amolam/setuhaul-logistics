"""Phase G4: the synthetic dataset built from the trainer data (setuhaul/adapters/phase_g_dataset.py)."""
from __future__ import annotations

import sqlite3
from datetime import date

import psycopg
import pytest

from conftest import BASE_DSN, _pg_available
from setuhaul.adapters.phase_g_dataset import build, load_assumptions, synthetic_snapshot
from setuhaul.db.migrate import migrate

DB = "setuhaul_phase_g_dataset_test"


def test_synthetic_copy_changes_identities_and_dates_only(tmp_path):
    path, counts = synthetic_snapshot(date(2026, 12, 2), out_dir=tmp_path)
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM drivers WHERE driver_id NOT LIKE 'SYN-%' OR phone NOT LIKE '+999%'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM shipments WHERE shipment_id NOT LIKE 'SYN-%' OR driver_id NOT LIKE 'SYN-%'").fetchone()[0] == 0
    # whole days only: the trainer's 07:50 stays 07:50 (same offset), on the new day
    eta = con.execute("SELECT original_eta_ts FROM shipments WHERE shipment_id='SYN-SHP1001'").fetchone()[0]
    assert eta == "2026-12-02T07:50:00+05:30"
    assert counts["shipments"] == 21 and counts["appointments_active"] == 14


@pytest.fixture(scope="module")
def built():
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
    dsn = f"{BASE_DSN} dbname={DB}"
    migrate(dsn)
    return dsn, build(dsn, date(2026, 12, 2))


def test_build_covers_most_trainer_records_and_marks_them_synthetic(built):
    dsn, rep = built
    assert rep["non_synthetic_identities"] == 0 and rep["coverage_pct"] >= 90
    assert sorted(r[0] for r in rep["refused_by_database"]) == ["APT1004", "APT1005", "APT1014A", "APT1015A"]
    with psycopg.connect(dsn) as c:
        shifts = c.execute("SELECT shifts FROM setuhaul.facility_config WHERE facility_id='FAC-JAI-01' AND active").fetchone()[0]
        assert shifts == load_assumptions()["labour_shifts"]["FAC-JAI-01"]
        assert c.execute("SELECT count(*) FROM setuhaul.appointment_slots WHERE slot_id LIKE 'SYN-SLOT-%'").fetchone()[0] > 0
