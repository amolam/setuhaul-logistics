"""The engine reading PostgreSQL gives the same answers as the engine reading the snapshot.

The snapshot is loaded into PostgreSQL through the real constraints. The database refuses four known-bad seed
bookings (APT1004, APT1005, APT1014A, APT1015A: audit A03 / A04 / A08). The snapshot side is given the same
input (those four removed), and then every shipment × clock time must match exactly.
"""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from conftest import BASE_DSN, _pg_available
from setuhaul.adapters.pg_seed import seed
from setuhaul.adapters.pg_view import load_view
from setuhaul.adapters.sqlite_snapshot import load
from setuhaul.db.migrate import migrate
from setuhaul.domain.feasibility import FeasibilityEngine

IST = timezone(timedelta(hours=5, minutes=30))
T = lambda h: datetime.fromisoformat(f"2026-08-04T{h}:00+05:30")


@pytest.fixture(scope="module")
def snap():
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS setuhaul_build_snap WITH (FORCE)")
        c.execute("CREATE DATABASE setuhaul_build_snap")
    d = f"{BASE_DSN} dbname=setuhaul_build_snap"
    migrate(d)
    info = seed(d)
    return d, info


def pg_engine(dsn):
    with psycopg.connect(dsn) as c:
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_feasibility")
            c.execute("SET LOCAL search_path = setuhaul")
            c.execute("SET LOCAL TimeZone = 'Asia/Kolkata'")
            c.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01,FAC-GGN-01', true)")
            return FeasibilityEngine(load_view(c, T("10:00"), horizon_h=48))


def test_database_refuses_exactly_the_known_bad_bookings(snap):
    _, info = snap
    refused = sorted(label.split()[0] for label, _ in info["rejected"])
    assert refused == ["APT1004", "APT1005", "APT1014A", "APT1015A"], info["rejected"]


def test_pg_view_matches_snapshot(snap):
    dsn, info = snap
    refused = {label.split()[0] for label, _ in info["rejected"]}
    data = load()
    data.appts = [a for a in data.appts if a["appointment_id"] not in refused]
    a_eng, b_eng = FeasibilityEngine(data), pg_engine(dsn)
    diffs = []
    for sid in sorted(data.shipments):
        for h in ("08:30", "09:35", "10:00", "11:15", "13:00", "16:26"):
            a, b = a_eng.find_options(sid, T(h)), b_eng.find_options(sid, T(h))
            ka = (a.outcome, [(o.dock, o.start, o.end, o.needs_approval) for o in a.options], a.facts["ready_ts"])
            kb = (b.outcome, [(o.dock, o.start, o.end, o.needs_approval) for o in b.options], b.facts["ready_ts"])
            if ka != kb:
                diffs.append((sid, h, ka, kb))
    assert diffs == [], diffs[:3]


def test_a_live_hold_by_another_driver_hides_the_slot(snap):
    """In PostgreSQL mode, other drivers' live holds are busy time (the snapshot has no holds at all)."""
    dsn, _ = snap
    eng = pg_engine(dsn)
    sid = next(s for s in sorted(eng.d.shipments)
               if eng.find_options(s, T("10:00")).outcome_code == "OPTIONS")
    first = eng.find_options(sid, T("10:00")).options[0]
    dock = next(d for d, r in eng.d.docks.items() if r["dock_code"] == first.dock
                and r["facility_id"] == eng.d.shipments[sid]["destination_facility_id"])
    eng.d.occupancy.append({"dock_id": dock, "kind": "HOLD", "shipment_id": "OTHER", "ref_id": "hold-x",
                            "expires_at": T("10:02"), "start": first.start, "end": first.end})
    again = eng.find_options(sid, T("10:00")).options
    assert all(not (o.dock == first.dock and o.start == first.start) for o in again)
    later = eng.find_options(sid, T("10:05")).options                       # that hold has expired by 10:05
    assert any(o.dock == first.dock and o.start == first.start for o in later) or \
        eng.find_options(sid, T("10:05")).outcome_code != "OPTIONS"
