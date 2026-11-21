"""Shared fixtures. Database tests need a local PostgreSQL (default: the throwaway cluster on port 55432).

    SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres" python -m pytest phase3_build/tests -q
"""
from __future__ import annotations

import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE_DSN = os.environ.get("SETUHAUL_TEST_DSN", "host=localhost port=55432 user=postgres")
DBNAME = "setuhaul_build_test"


def _pg_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(f"{BASE_DSN} dbname=postgres", connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def dsn():
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    import psycopg
    from setuhaul.db.migrate import migrate
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DBNAME}")
    d = f"{BASE_DSN} dbname={DBNAME}"
    migrate(d)
    _seed(d)
    return d


def _seed(d: str):
    import psycopg
    with psycopg.connect(d, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO carriers VALUES ('CAR1','Carrier One',true)")
        c.execute("""INSERT INTO facilities (facility_id,facility_name,open_time,close_time) VALUES
                     ('FAC-JAI-01','Jaipur','00:00','23:59'),('FAC-GGN-01','Gurugram','00:00','23:59')""")
        # two shifts covering the whole day so tests behave the same at any wall-clock time
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     ('FAC-JAI-01',1,10,10,30,'{"DAY":15,"NIGHT":15}',%s,30),
                     ('FAC-GGN-01',1,15,10,20,'{"DAY":0,"NIGHT":0}',%s,30)""", (shifts, shifts))
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg) VALUES
                     ('DOCK-JAI-D1','FAC-JAI-01','D1','STANDARD',false,30000),
                     ('DOCK-JAI-D2','FAC-JAI-01','D2','STANDARD',false,30000),
                     ('DOCK-JAI-D3','FAC-JAI-01','D3','STANDARD',false,30000),
                     ('DOCK-GGN-D1','FAC-GGN-01','D1','STANDARD',false,30000)""")


def new_shipment(conn, facility="FAC-JAI-01", driver=None) -> tuple[str, str]:
    """A fresh driver + truck + shipment (superuser fixture). Returns (shipment_id, driver_id)."""
    u = uuid.uuid4().hex[:8]
    drv, veh, sid = driver or f"DRV-{u}", f"VEH-{u}", f"SHP-{u}"
    conn.execute("INSERT INTO setuhaul.drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s) "
                 "ON CONFLICT DO NOTHING", (drv, f"+91{uuid.uuid4().int % 10**10:010d}"))
    conn.execute("INSERT INTO setuhaul.vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}"))
    conn.execute("""INSERT INTO setuhaul.shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                    load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                    VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,now()-interval '3 hours',now()+interval '2 hours','IN_TRANSIT')""",
                 (sid, f"ORD-{u}", drv, veh, facility))
    return sid, drv


@contextmanager
def as_role(conn, role: str, scope: str | None = None, driver: str | None = None):
    """One RDS Data API style transaction: SET LOCAL ROLE + transaction-local scope, then statements."""
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {role}")
        conn.execute("SET LOCAL search_path = setuhaul")
        if scope is not None:
            conn.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
        if driver is not None:
            conn.execute("SELECT set_config('app.driver_id', %s, true)", (driver,))
        yield conn


@pytest.fixture
def conn(dsn):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c
