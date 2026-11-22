"""Integrity proof: N drivers ask for the same dock slot at the same instant; the database lets exactly one have it.

Hand-in item 5 of the SetuHaul FDE challenge ("a test that sends competing requests for the same slot and shows no
double booking"). Read-only with respect to the code: it builds a FRESH throwaway database, applies the real
migrations, seeds one facility with ONE dock, and drives N real drivers through the production service path:

    signed WhatsApp webhook -> BookingService tool calls (find options, select, read-back, driver YES attested by the
    inbound path) -> hold_option() PL/pgSQL function -> dock_occupancy EXCLUDE constraint

The N hold requests are released by a threading.Barrier, each on its own HTTP client and its own database
connection, so they really reach PostgreSQL at the same time. Afterwards it reads the raw database rows, prints them
and asserts the invariant. Exit code 0 only if every assertion holds.

    SETUHAUL_TEST_DSN="host=localhost port=55434 user=postgres" python scripts/integrity_proof.py [N]
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from setuhaul.agent import replies  # noqa: E402
from setuhaul.api.app import Settings, create_app  # noqa: E402
from setuhaul.db.migrate import migrate  # noqa: E402

BASE_DSN = os.environ.get("SETUHAUL_TEST_DSN", "host=localhost port=55434 user=postgres")
DBNAME = "setuhaul_integrity_proof"
IST = timezone(timedelta(hours=5, minutes=30))
JAI, DOCK = "FAC-JAI-01", "DOCK-JAI-D1"
META_SECRET = b"meta-app-secret-for-the-integrity-proof-only"
SESSION_SECRET = b"session-secret-for-the-integrity-proof-only-0123456789"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- fresh database, one facility, ONE dock
def build_db() -> str:
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DBNAME}")
    dsn = f"{BASE_DSN} dbname={DBNAME}"
    migrate(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:                     # same seeding style as tests/conftest.py
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO carriers VALUES ('CAR1','Carrier One',true)")
        c.execute("INSERT INTO facilities (facility_id,facility_name,open_time,close_time) VALUES ('FAC-JAI-01','Jaipur','00:00','23:59')")
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     ('FAC-JAI-01',1,10,10,30,'{"DAY":15,"NIGHT":15}',%s,30)""", (shifts,))
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg)
                     VALUES ('DOCK-JAI-D1','FAC-JAI-01','D1','STANDARD',false,30000)""")
        base = datetime.now(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        for h in range(72):                                              # the planning grid: hourly slots on D1
            s = base + timedelta(hours=h)
            c.execute("INSERT INTO appointment_slots VALUES (%s,%s,tstzrange(%s,%s,'[)'),'OPEN',NULL)",
                      (f"S-{DOCK}-{h}", DOCK, s, s + timedelta(hours=1)))
    return dsn


def add_driver(dsn: str, i: int, eta_at: datetime) -> tuple[str, str, str]:
    u = uuid.uuid4().hex[:6]
    drv, phone, sid, veh = f"DRV-{i:02d}-{u}", f"+91{uuid.uuid4().int % 10**10:010d}", f"SHP-{i:02d}-{u}", f"VEH-{i:02d}-{u}"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1',%s,%s)",
                  (drv, f"Driver {i:02d}", phone))
        c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}"))
        c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                     load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                     VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,%s,%s,'IN_TRANSIT')""",
                  (sid, f"ORD-{u}", drv, veh, JAI, eta_at - timedelta(hours=5), eta_at))
    return drv, phone, sid


# ---------------------------------------------------------------- a driver on WhatsApp + the agent's tool calls
class Driver:
    def __init__(self, client, phone, sid, drv):
        self.c, self.phone, self.sid, self.drv = client, phone, sid, drv
        self.turn, self.session, self.last_msg = 0, None, None

    def say(self, text):
        mid = f"wamid.{uuid.uuid4().hex}"
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": self.phone.lstrip("+"), "id": mid, "type": "text", "text": {"body": text}}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(META_SECRET, body, hashlib.sha256).hexdigest()
        r = self.c.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        assert r.status_code == 200, r.text
        ev = next(e for e in reversed(self.c.app.state.inbound) if e.get("external_message_id") == mid)
        self.session, self.last_msg = ev["session_token"], ev["message_id"]

    def tool(self, name, **args):
        self.turn += 1
        r = self.c.post(f"/tools/{name}", json={"turn_id": self.turn, "args": args}, headers={"X-Setuhaul-Session": self.session})
        assert r.status_code == 200, (r.status_code, r.text)
        return r.json()


def q(dsn, sql, args=()):
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute(sql, args).fetchall()


def main() -> int:
    t0 = datetime.now(timezone.utc)
    say(f"SetuHaul integrity proof - {N} drivers, one slot")
    say(f"run at {t0.isoformat(timespec='seconds')}  |  database {DBNAME} on {BASE_DSN}")
    dsn = build_db()
    say("server:", q(dsn, "SELECT version()")[0][0])
    app = create_app(Settings(dsn=dsn, cognito_issuer="https://cognito-idp.example/none", cognito_client_ids={"none"},
                              session_secret=SESSION_SECRET, meta_app_secret=META_SECRET, facilities=(JAI,),
                              jwks={"keys": []}, booking_enabled=True, local_test_harness=True))
    setup = TestClient(app)

    # every truck has the same fixed ETA (30 h out, :20 past the hour), so every driver is offered the same first slot
    eta = (datetime.now(IST) + timedelta(hours=30)).replace(minute=20, second=0, microsecond=0)
    drivers = []
    for i in range(1, N + 1):
        drv, phone, sid = add_driver(dsn, i, eta)
        d = Driver(setup, phone, sid, drv)
        d.say("Stuck in traffic, running late. Any slot?")
        o = d.tool("find_feasible_options")["data"]["options"][0]
        d.tool("select_option", option_id=o["option_id"])              # the agent records the read-back ...
        d.say("haan ji, kar do")                                         # ... and the driver says YES
        assert d.tool("confirm_selection", message_id=d.last_msg)["data"]["verdict"] == "YES"
        drivers.append((d, o))
    offered = {(o["dock_code"], o["start"]) for _, o in drivers}
    assert len(offered) == 1, f"drivers were offered different slots: {offered}"
    o0 = drivers[0][1]
    win = q(dsn, "SELECT dock_id, lower(during), upper(during) FROM setuhaul.shown_options WHERE option_id=%s", (o0["option_id"],))[0]
    lo, hi = win[1], win[2]

    say("\n== 1. Initial state")
    say(f"facility {JAI}, docks in the facility: {q(dsn, 'SELECT count(*) FROM setuhaul.docks')[0][0]} ({DOCK})")
    say(f"slot every driver was offered and said YES to: dock {o0['dock_code']}, {lo.astimezone(IST):%Y-%m-%d %H:%M}-"
        f"{hi.astimezone(IST):%H:%M} IST (dock occupancy window incl. unload + changeover)")
    pre = q(dsn, "SELECT count(*) FROM setuhaul.dock_occupancy WHERE active AND dock_id=%s AND during && tstzrange(%s,%s)", (DOCK, lo, hi))[0][0]
    say(f"active claims (holds/bookings/blocks) on that window before the race: {pre}")
    say(f"drivers with a confirmed (attested) YES for this exact slot: {len(drivers)}")
    assert pre == 0

    # ---------------------------------------------------------------- the race
    say(f"\n== 2. The race: {N} hold requests released by one barrier, {N} threads, {N} HTTP clients, {N} DB connections")
    barrier, results, lock = threading.Barrier(N), [], threading.Lock()

    def go(d, o):
        d.c = TestClient(app)                                            # own client per driver
        barrier.wait()
        s = time.perf_counter()
        r = d.tool("hold_option", option_id=o["option_id"])
        e = time.perf_counter()
        with lock:
            results.append((d, r, s, e))

    ts = [threading.Thread(target=go, args=x) for x in drivers]
    [t.start() for t in ts]
    [t.join(120) for t in ts]
    assert len(results) == N, f"only {len(results)} of {N} requests returned"
    starts, ends = [r[2] for r in results], [r[3] for r in results]
    say(f"spread of request start times: {1000 * (max(starts) - min(starts)):.1f} ms; "
        f"first answer after {1000 * (min(ends) - min(starts)):.1f} ms; "
        f"requests already sent before the first answer came back: {sum(s < min(ends) for s in starts)}/{N}")

    say("\n== 3. What each driver's request got back (service envelope)")
    for d, r, s, e in sorted(results, key=lambda x: x[0].sid):
        extra = r["data"].get("hold_expires_at") if r["status"] == "OK" else \
            f"code={r['data'].get('code')} fresh_options={len(r['data'].get('fresh_options', []))}"
        say(f"  {d.sid}  status={r['status']:<5} {r['data'].get('status', '')} {extra}")
    statuses = [r["status"] for _, r, _, _ in results]
    winners = [d for d, r, _, _ in results if r["status"] == "OK"]
    say(f"totals: OK={statuses.count('OK')} LOST={statuses.count('LOST')} other={N - statuses.count('OK') - statuses.count('LOST')}")

    loser = next(r for _, r, _, _ in results if r["status"] == "LOST")
    say("\n== 4. What a losing driver is told (the orchestrator's fixed template, setuhaul/agent/replies.py)")
    fresh = loser["data"].get("fresh_options", [])
    msg = replies.t("lost", "en") + "\n" + (replies.options_text(fresh, "en") if fresh else replies.t("no_slot", "en"))
    say("  " + msg.replace("\n", "\n  "))

    say("\n== 4b. Database right after the race, before anything else happens")
    for r in q(dsn, """SELECT occupancy_id, kind, shipment_id, lower(during), upper(during), active, expires_at
                         FROM setuhaul.dock_occupancy WHERE dock_id=%s AND during && tstzrange(%s,%s) ORDER BY 1""", (DOCK, lo, hi)):
        say(f"  id={r[0]} kind={r[1]} shipment={r[2]} {r[3].astimezone(IST):%H:%M}-{r[4].astimezone(IST):%H:%M} "
            f"active={r[5]} hold_expires={r[6].astimezone(IST):%H:%M:%S}")

    # the winner continues to a booking request (an appointment waiting for the warehouse)
    w = winners[0]
    sub = w.tool("submit_booking_request", option_id=dict((d.sid, o) for d, o in drivers)[w.sid]["option_id"])
    say(f"\nwinner {w.sid} submits the booking request: {sub['status']} {sub['data'].get('status')} {sub['data'].get('appointment_id')}")

    # ---------------------------------------------------------------- the database, raw
    race_sids = [d.sid for d, _ in drivers]
    say("\n== 5. ACTUAL database outcome")
    rows = q(dsn, """SELECT occupancy_id, dock_id, kind, shipment_id, lower(during), upper(during), active
                       FROM setuhaul.dock_occupancy WHERE dock_id=%s AND during && tstzrange(%s,%s) ORDER BY occupancy_id""",
             (DOCK, lo, hi))
    say("dock_occupancy rows touching the raced window (all, active or not):")
    for r in rows:
        say(f"  id={r[0]} dock={r[1]} kind={r[2]:<11} shipment={r[3]} {r[4].astimezone(IST):%H:%M}-{r[5].astimezone(IST):%H:%M} active={r[6]}")
    active_sids = q(dsn, """SELECT count(DISTINCT shipment_id) FROM setuhaul.dock_occupancy
                             WHERE active AND dock_id=%s AND during && tstzrange(%s,%s)""", (DOCK, lo, hi))[0][0]
    holds = q(dsn, """SELECT count(*) FROM setuhaul.dock_occupancy WHERE active AND kind='HOLD' AND dock_id=%s
                       AND during && tstzrange(%s,%s)""", (DOCK, lo, hi))[0][0]
    appts = q(dsn, """SELECT appointment_id, shipment_id, status FROM setuhaul.appointments
                       WHERE shipment_id = ANY(%s) AND status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""", (race_sids,))
    overlap = q(dsn, """SELECT count(*) FROM setuhaul.dock_occupancy a JOIN setuhaul.dock_occupancy b
                          ON a.dock_id=b.dock_id AND a.occupancy_id<b.occupancy_id AND a.during && b.during
                         AND a.shipment_id IS DISTINCT FROM b.shipment_id
                       WHERE a.active AND b.active""")[0][0]
    fresh_ids = [f["option_id"] for _, r, _, _ in results if r["status"] == "LOST" for f in r["data"].get("fresh_options", [])]
    fresh_overlap = q(dsn, """SELECT count(*) FROM setuhaul.shown_options WHERE option_id = ANY(%s::uuid[])
                               AND dock_id=%s AND during && tstzrange(%s,%s)""", (fresh_ids, DOCK, lo, hi))[0][0]
    say(f"Q5 fresh options handed to losers: {len(fresh_ids)}; of those overlapping the taken window: {fresh_overlap}")
    say(f"Q1 shipments with an active claim on that dock window: {active_sids}")
    say(f"Q2 active HOLD rows on that window: {holds}")
    say(f"Q3 active appointments among the {N} racing shipments: {len(appts)} -> {appts}")
    say(f"Q4 pairs of active claims of DIFFERENT shipments overlapping on ANY dock (whole database): {overlap}")

    # ---------------------------------------------------------------- the last line of defence, attacked directly
    say("\n== 6. Bypass attempt: insert a second overlapping claim directly, as the database superuser")
    say("   (superuser bypasses row-level security and grants; only the EXCLUDE constraint is left)")
    victim = next(s for s in race_sids if s != w.sid)
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id,facility_id,during,kind,shipment_id,ref_id,created_by)
                         VALUES (%s,%s,tstzrange(%s,%s,'[)'),'APPOINTMENT',%s,'manual-bypass','superuser')""",
                      (DOCK, JAI, lo, hi, victim))
        bypass = "INSERTED (DEFECT)"
    except psycopg.Error as e:
        bypass = f"REFUSED sqlstate={e.sqlstate} {e.diag.message_primary}"
    say(f"   result: {bypass}")

    # ---------------------------------------------------------------- the invariant
    say("\n== 7. Invariant checks")
    checks = {
        f"exactly one of {N} hold requests succeeded": statuses.count("OK") == 1,
        f"the other {N - 1} were told LOST (slot taken)": statuses.count("LOST") == N - 1,
        "every loser received fresh options in the same answer": all(r["data"].get("fresh_options")
                                                                    for _, r, _, _ in results if r["status"] == "LOST"),
        "no fresh option offered to a loser overlaps the taken window": fresh_overlap == 0,
        "one shipment holds the dock window in the database": active_sids == 1,
        "at most one active HOLD on the window": holds <= 1,
        "exactly one active appointment among the racers (the winner's)": len(appts) == 1 and appts[0][1] == w.sid,
        "zero overlapping claims of different shipments on any dock": overlap == 0,
        "direct superuser insert of an overlapping claim is refused (23P01)": bypass.startswith("REFUSED sqlstate=23P01"),
    }
    for k, v in checks.items():
        say(f"  [{'PASS' if v else 'FAIL'}] {k}")
    ok = all(checks.values())
    say(f"\nRESULT: {'PASS' if ok else 'FAIL'}  ({sum(checks.values())}/{len(checks)} checks)  "
        f"elapsed {(datetime.now(timezone.utc) - t0).total_seconds():.1f} s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
