"""Local staff console for manual checks of the 27 Nov fix plan (dispatcher, operations, planner, ops_lead).

Synthetic data only, on a dedicated local database whose name starts with `setuhaul_localconsole` (never a pilot or
shared database). No Telegram, no WhatsApp, no Cognito, no model: replies go to the in-process test sink, staff sign
in with local tokens (as scripts/telegram_test_bot.py does), and the no-show job runs every 20 seconds.

    python scripts/local_console.py --setup        # (re)create, migrate and seed the database
    python scripts/local_console.py                # console at http://127.0.0.1:8782/app-runner/<role>

The seed: two facilities with their real grace periods (Jaipur 30 min, Gurugram 20 min), docks and 96 h of slots,
eight drivers, and shipments in each state: needs booking, confirmed later today, confirmed tomorrow, booked 45 min ago
with no arrival (becomes a no-show), booked 10 min ago (still inside the grace), an open SAFETY escalation.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_DSN = "host=localhost port=55432 user=postgres dbname=setuhaul_localconsole"


def _dsn() -> str:
    dsn = os.environ.get("SETUHAUL_LOCAL_CONSOLE_DSN", DEFAULT_DSN)
    if not re.search(r"\bdbname=setuhaul_localconsole[a-z0-9_]*(\s|$)", dsn):
        sys.exit("refused: the database name must start with setuhaul_localconsole")
    return dsn


def setup(dsn: str) -> None:
    import psycopg
    from setuhaul.db.migrate import migrate
    name = re.search(r"dbname=(\S+)", dsn).group(1)
    base = re.sub(r"\s*dbname=\S+", "", dsn)
    with psycopg.connect(f"{base} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {name}")
    migrate(dsn)
    now = datetime.now(IST)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        # history first: the lifecycle cutover is moved one day back so the minute job covers today's bookings
        c.execute("ALTER TABLE lifecycle_cutover DISABLE TRIGGER lifecycle_cutover_fixed")
        c.execute("UPDATE lifecycle_cutover SET started_at = now() - interval '1 day'")
        c.execute("ALTER TABLE lifecycle_cutover ENABLE TRIGGER lifecycle_cutover_fixed")
        c.execute("INSERT INTO carriers VALUES ('SYN-CAR1','Synthetic Carriers',true)")
        c.execute("""INSERT INTO facilities (facility_id,facility_name,open_time,close_time) VALUES
                     ('FAC-JAI-01','Jaipur','00:00','23:59'),('FAC-GGN-01','Gurugram','00:00','23:59')""")
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     ('FAC-JAI-01',1,10,10,30,'{"DAY":30,"NIGHT":30}',%s,30),
                     ('FAC-GGN-01',1,15,10,20,'{"DAY":30,"NIGHT":30}',%s,30)""", (shifts, shifts))
        docks = [("DOCK-JAI-D1", "FAC-JAI-01", "D1"), ("DOCK-JAI-D2", "FAC-JAI-01", "D2"), ("DOCK-JAI-D3", "FAC-JAI-01", "D3"),
                 ("DOCK-GGN-D1", "FAC-GGN-01", "D1")]
        for d, f, k in docks:
            c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg)
                         VALUES (%s,%s,%s,'STANDARD',false,30000)""", (d, f, k))
        base = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
        for d, _, _ in docks:
            for h in range(99):
                s = base + timedelta(hours=h)
                c.execute("INSERT INTO appointment_slots VALUES (%s,%s,tstzrange(%s,%s,'[)'),'OPEN',NULL)",
                          (f"S-{d}-{h}", d, s, s + timedelta(hours=1)))
        drivers = []
        for i in range(1, 9):
            drv = f"SYN-DRV-L{i}"
            c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'SYN-CAR1',%s,%s)",
                      (drv, f"Synthetic Driver {i}", f"+99900000000{i:02d}"))
            c.execute("INSERT INTO vehicles VALUES (%s,'SYN-CAR1','32FT',%s,20000,false)", (f"SYN-VEH-L{i}", f"SYN-REG-{i}"))
            drivers.append(drv)

        def ship(i, eta_h):
            sid = f"SYN-SHP-L{i}"
            eta = now + timedelta(hours=eta_h)
            c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                         load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                         VALUES (%s,%s,'SYN-CAR1',%s,%s,'FAC-JAI-01',10000,45,%s,%s,'IN_TRANSIT')""",
                      (sid, f"ORD-L{i}", f"SYN-DRV-L{i}", f"SYN-VEH-L{i}", eta - timedelta(hours=5), eta))
            return sid

        def booked(sid, dock, start, minutes=55):
            appt = f"APT-L-{uuid.uuid4().hex[:10]}"
            occ = c.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                               VALUES (%s,'FAC-JAI-01',tstzrange(%s,%s,'[)'),'APPOINTMENT',%s,%s,'seed') RETURNING occupancy_id""",
                            (dock, start, start + timedelta(minutes=minutes), sid, appt)).fetchone()[0]
            c.execute("""INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,occupancy_id,status,is_current,
                         booking_source,warehouse_confirmation_ref,confirmation_basis)
                         VALUES (%s,%s,%s,tstzrange(%s,%s,'[)'),%s,'CONFIRMED',true,'PLANNER','seed','HUMAN')""",
                      (appt, sid, dock, start, start + timedelta(minutes=minutes), occ))
            return appt

        ship(1, 2)                                                           # needs booking
        booked(ship(2, 3), "DOCK-JAI-D1", now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=5))
        booked(ship(3, 26), "DOCK-JAI-D2", now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=27))
        booked(ship(4, -1), "DOCK-JAI-D3", now - timedelta(minutes=45))      # no arrival: becomes a no-show
        booked(ship(5, 0), "DOCK-JAI-D2", now - timedelta(minutes=10))       # inside the grace: record the arrival
        sid6 = ship(6, 4)                                                    # an open SAFETY escalation
        c.execute("""INSERT INTO escalations (escalation_id,reason_code,mode,facility_id,shipment_id,handoff,ack_due_at,created_at)
                     VALUES (gen_random_uuid(),'SAFETY','TAKE_OVER','FAC-JAI-01',%s,'{}',now() - interval '5 minutes',
                             now() - interval '15 minutes')""", (sid6,))
    print(f"database {re.search(r'dbname=(\S+)', dsn).group(1)} ready (synthetic)")


def make_app(dsn: str):
    from telegram_test_bot import make_app as runner_app             # the same local-token pattern, no bot behind it
    return runner_app(dsn, None, None, secrets.token_urlsafe(48))


def run(dsn: str, port: int) -> None:
    import uvicorn
    from setuhaul.api.db import Db
    app = make_app(dsn)
    threading.Thread(target=uvicorn.run, args=(app,), daemon=True,
                     kwargs={"host": "127.0.0.1", "port": port, "log_level": "warning"}).start()
    print(f"console: http://127.0.0.1:{port}/app-runner/<dispatcher|operations|planner|ops_lead>; Ctrl+C stops.")
    while True:
        try:
            with Db(dsn).tx("system", ("FAC-JAI-01", "FAC-GGN-01")) as c:
                ns = c.execute("SELECT process_no_shows(200)").fetchone()[0]
            if ns.get("no_shows"):
                print("no-show job:", ns)
            time.sleep(20)
        except KeyboardInterrupt:
            return
        except Exception as e:                                       # noqa: BLE001
            print("loop error:", type(e).__name__, str(e)[:120])
            time.sleep(5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--port", type=int, default=8782)
    a = ap.parse_args()
    if a.setup:
        setup(_dsn())
    else:
        run(_dsn(), a.port)
