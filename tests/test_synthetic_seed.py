"""Synthetic baseline seed over the RDS Data API (setuhaul/aws/synthetic_seed.py), on its own fresh database.

The seed is what a pilot database starts from: it must apply through the owner session the migrations use, be
idempotent, give open slots relative to now, and refuse to run (fail closed) if any non-synthetic identity exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from conftest import BASE_DSN
from data_api_emulator import EmulatedRdsData
from setuhaul.aws.migrate_data_api import OwnerSession, migrate_data_api
from setuhaul.aws.synthetic_seed import PHONE_PREFIX, seed, synthetic_phone, synthetic_violations

ARN, OWNER = "arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul", "arn:aws:secretsmanager:ap-southeast-2:0:secret:owner"
DBNAME = "setuhaul_synthetic_seed_test"


@pytest.fixture(scope="module")
def session(dsn):                                    # `dsn` only to make the suite's PostgreSQL check run first
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DBNAME}")
    d = f"{BASE_DSN} dbname={DBNAME}"
    s = OwnerSession(EmulatedRdsData(d), ARN, OWNER)
    migrate_data_api(s)
    s.dsn = d
    yield s
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")


def test_the_seed_applies_over_the_data_api_and_is_idempotent(session):
    now = datetime.now(timezone.utc)
    first = seed(session, now)
    assert first == {"facilities": 2, "docks": 4, "slots": 4 * 96}
    assert seed(session, now) == first                                   # nothing added twice
    later = seed(session, now + timedelta(hours=3))                       # a rolling window adds only new hours
    assert later["slots"] == 4 * 99
    with psycopg.connect(session.dsn, autocommit=True) as c:
        open_now = c.execute("""SELECT count(*) FROM setuhaul.appointment_slots
                                 WHERE slot_status = 'OPEN' AND slot @> now()""").fetchone()[0]
        carriers = c.execute("SELECT carrier_id FROM setuhaul.carriers").fetchall()
    assert open_now == 4 and carriers == [("SYN-CAR1",)]


def test_synthetic_phone_numbers_are_in_the_reserved_country_code():
    assert synthetic_phone(1) == "+99900000000001" and synthetic_phone(1).startswith(PHONE_PREFIX)
    assert len(synthetic_phone(10**11 - 1)) == 15                          # E.164 maximum length
    with pytest.raises(ValueError):
        synthetic_phone(10**11)


def test_a_non_synthetic_identity_blocks_the_seed(session):
    assert synthetic_violations(session) == []
    with psycopg.connect(session.dsn, autocommit=True) as c:
        c.execute("INSERT INTO setuhaul.drivers (driver_id,carrier_id,driver_name,phone_e164) "
                  "VALUES ('SYN-DRV-OK','SYN-CAR1','ok',%s)", (synthetic_phone(42),))
        assert synthetic_violations(session) == []
        c.execute("INSERT INTO setuhaul.drivers (driver_id,carrier_id,driver_name,phone_e164) "
                  "VALUES ('SYN-DRV-REALPHONE','SYN-CAR1','x','+919000010001')")
    try:
        assert synthetic_violations(session) == [("driver", "SYN-DRV-REALPHONE")]
        with pytest.raises(RuntimeError, match="SYNTHETIC_DATA"):
            seed(session)
    finally:
        with psycopg.connect(session.dsn, autocommit=True) as c:
            c.execute("DELETE FROM setuhaul.drivers WHERE driver_id LIKE 'SYN-DRV-%'")
