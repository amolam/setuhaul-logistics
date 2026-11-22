"""Synthetic baseline data for an AWS pilot database, applied over the RDS Data API as the owner (deployment step).

    python -m setuhaul.aws.synthetic_seed            # SETUHAUL_DB_CLUSTER_ARN, SETUHAUL_DB_OWNER_SECRET_ARN

Aurora has no network path in, so the psycopg seed loaders cannot reach it; this runs the same kind of baseline the
local demo uses (two facilities open all day, four docks, hourly slots) through the migration runner's owner session,
in one transaction. It is idempotent: a re-run only adds the slots of hours not yet seeded (a rolling window), so it
can be re-run before an end-to-end session.

Synthetic identity rule (fail closed): every carrier, driver and vehicle the pilot holds must carry the `SYN-` id
prefix, and every driver phone number is in the ITU-reserved `+999` country code, which no network routes. The run
refuses to start, and `synthetic_violations()` reports, if the database holds any row that breaks the rule. No real
phone number or person is ever written.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

SYN = "SYN-"
PHONE_PREFIX = "+999"                     # E.164 country code 999: reserved by the ITU, never assigned or routable
FACILITIES = (("FAC-JAI-01", "Jaipur DC (synthetic)"), ("FAC-GGN-01", "Gurugram DC (synthetic)"))
DOCKS = (("DOCK-JAI-D1", "FAC-JAI-01", "D1"), ("DOCK-JAI-D2", "FAC-JAI-01", "D2"), ("DOCK-JAI-D3", "FAC-JAI-01", "D3"),
         ("DOCK-GGN-D1", "FAC-GGN-01", "D1"))
SHIFTS = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
IST = timezone(timedelta(hours=5, minutes=30))

VIOLATIONS = """
SELECT 'carrier', carrier_id FROM setuhaul.carriers WHERE carrier_id NOT LIKE 'SYN-%'
UNION ALL SELECT 'driver', driver_id FROM setuhaul.drivers WHERE driver_id NOT LIKE 'SYN-%' OR phone_e164 NOT LIKE '+999%'
UNION ALL SELECT 'vehicle', vehicle_id FROM setuhaul.vehicles WHERE vehicle_id NOT LIKE 'SYN-%'
ORDER BY 1, 2"""


def synthetic_phone(n: int) -> str:
    if not 0 <= n < 10**11:
        raise ValueError(n)
    return f"{PHONE_PREFIX}{n:011d}"


def baseline_statements(now: datetime | None = None, hours: int = 96) -> list[str]:
    """SQL statements (no parameters) for the baseline, slots from two hours ago for `hours` hours."""
    base = (now or datetime.now(IST)).astimezone(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    fac = ",".join(f"('{f}','{name}','00:00','23:59')" for f, name in FACILITIES)
    docks = ",".join(f"('{d}','{f}','{code}','STANDARD',false,30000)" for d, f, code in DOCKS)
    slots = []
    for d, _, _ in DOCKS:
        for h in range(hours):
            s = base + timedelta(hours=h)
            slots.append(f"('S-{d}-{s:%Y%m%d%H}','{d}',tstzrange('{s.isoformat()}','{(s + timedelta(hours=1)).isoformat()}','[)'),"
                         "'OPEN',NULL)")
    return [
        "SET LOCAL search_path = setuhaul",
        # RLS is forced on the facility tables and Aurora's owner is not a superuser: scope to the seeded facilities
        f"SELECT set_config('app.facility_scope', '{','.join(f for f, _ in FACILITIES)}', true)",
        f"INSERT INTO carriers VALUES ('{SYN}CAR1','Synthetic Carriers',true) ON CONFLICT DO NOTHING",
        f"INSERT INTO facilities (facility_id,facility_name,open_time,close_time) VALUES {fac} ON CONFLICT DO NOTHING",
        "INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,"
        "warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES "
        f"('FAC-JAI-01',1,10,10,30,'{{\"DAY\":5,\"NIGHT\":5}}','{SHIFTS}',30),"
        f"('FAC-GGN-01',1,15,10,20,'{{\"DAY\":5,\"NIGHT\":5}}','{SHIFTS}',30) ON CONFLICT DO NOTHING",
        "INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg) "
        f"VALUES {docks} ON CONFLICT DO NOTHING",
        f"INSERT INTO appointment_slots VALUES {','.join(slots)} ON CONFLICT DO NOTHING",
    ]


def synthetic_violations(session) -> list[tuple]:
    """Rows that break the synthetic identity rule (empty = the rule holds)."""
    return [tuple(r) for r in session.query(VIOLATIONS)]


def seed(session, now: datetime | None = None) -> dict:
    bad = synthetic_violations(session)
    if bad:
        raise RuntimeError(f"SYNTHETIC_DATA: {len(bad)} non-synthetic rows present (e.g. {bad[0][0]}); refusing to seed")
    session.run(baseline_statements(now))
    scope = f"SELECT set_config('app.facility_scope', '{','.join(f for f, _ in FACILITIES)}', true)"
    counts = session.query("SELECT (SELECT count(*) FROM setuhaul.facilities), (SELECT count(*) FROM setuhaul.docks), "
                           "(SELECT count(*) FROM setuhaul.appointment_slots)", (scope,))[0]
    return {"facilities": counts[0], "docks": counts[1], "slots": counts[2]}


def main() -> int:
    if os.environ.get("SETUHAUL_DATA_CLASS", "synthetic") != "synthetic":
        raise SystemExit("SYNTHETIC_DATA: this seed runs only for synthetic environments")
    import boto3
    from .migrate_data_api import OwnerSession
    session = OwnerSession(boto3.client("rds-data", region_name=os.environ.get("AWS_REGION", "ap-southeast-2")),
                           os.environ["SETUHAUL_DB_CLUSTER_ARN"], os.environ["SETUHAUL_DB_OWNER_SECRET_ARN"],
                           os.environ.get("SETUHAUL_DB_NAME", "setuhaul"))
    print(seed(session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
