"""Scenario run on the SUPPLIED challenge snapshot (brief p.6-7: "Test on this snapshot"; ladder of situations p.7).

What runs, all of it real code:
- the original SQLite snapshot (`data/setuhaul_freight_operations.db`), loaded into PostgreSQL through the real constraints
  by `setuhaul.adapters.pg_seed.seed`. The database refuses the four known-bad seed bookings (audit A03/A04/A08);
- the signed inbound webhook, the API and the booking service;
- the agent orchestrator with the deterministic RULES reader. No model is called and nothing is billed;
- the feasibility engine and the database escalation function.

The service clock is fixed to the snapshot day (4 Aug 2026, Asia/Kolkata) at the time of each driver's real message,
so the snapshot's ETAs, dock events and bookings mean what they meant that day. The booking path is NOT exercised
here: it is off in this app (`booking_enabled=False`), because the database's own clock is today and a hold on
4 Aug 2026 would be a booking in the past. Booking under contention is shown by the synthetic scenario run
(`tests/test_scenario_run.py`) and the integrity proof (`scripts/integrity_proof.py`).

Cases, taken from the brief's ladder (each driver sends the exact words recorded in the snapshot):
- L1 THR011 / SHP1017: the driver only asks about options. The agent shows options and books nothing;
- L2 THR003 / SHP1013: "I am late by one hour.". The agent asks before it calls a tool;
- L2 THR010 / DRV004: the driver has two live shipments. The agent asks which one, never guesses;
- L6 THR005 / SHP1015: a reefer load, and the only reefer dock is under maintenance from 18:00. No feasible slot, so
  the agent ESCALATES to a coordinator with a database-built hand-off record instead of inventing a slot.

Usage (needs a reachable PostgreSQL 18; creates and drops its own database `setuhaul_snapshot_scenario`):
    SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres" python scripts/snapshot_scenario.py
    ... --record     also writes docs/challenge/evidence/snapshot_scenario_run.json
Exit code 0 only if every expectation holds.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from setuhaul.adapters.pg_seed import seed  # noqa: E402
from setuhaul.adapters.sqlite_snapshot import SNAPSHOT, load  # noqa: E402
from setuhaul.domain.feasibility import FeasibilityEngine  # noqa: E402
from setuhaul.agent.orchestrator import Orchestrator  # noqa: E402
from setuhaul.api.app import Settings, create_app  # noqa: E402
from setuhaul.db.migrate import migrate  # noqa: E402

BASE_DSN = os.environ.get("SETUHAUL_TEST_DSN", "host=localhost port=55432 user=postgres")
DBNAME = "setuhaul_snapshot_scenario"
DSN = f"{BASE_DSN} dbname={DBNAME}"
IST = timezone(timedelta(hours=5, minutes=30))
META_SECRET = b"meta-app-secret-for-the-snapshot-scenario-only"
SESSION_SECRET = b"session-secret-for-the-snapshot-scenario-only-0123456789"
OUT = ROOT / "docs" / "challenge" / "evidence" / "snapshot_scenario_run.json"


class Clock:
    """The service clock: the snapshot day, set to the moment of each driver's recorded message."""

    def __init__(self):
        self.now = datetime(2026, 8, 4, 9, 0, tzinfo=IST)

    def __call__(self):
        return self.now


def one(sql, *a):
    with psycopg.connect(DSN, autocommit=True) as c:
        return c.execute(sql, a).fetchone()


def say(client, phone, text):
    """Send one message through the signed webhook and return the inbound event the orchestrator receives."""
    mid = f"wamid.snapshot.{uuid.uuid4().hex}"
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": phone.lstrip("+"), "id": mid, "type": "text", "text": {"body": text}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(META_SECRET, body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    return next(e for e in reversed(client.app.state.inbound) if e.get("external_message_id") == mid)


# case id, ladder level, thread, driver, shipment, the driver's recorded words, recorded time, expectation
CASES = [
    ("L1", 1, "THR011", "DRV001", "SHP1017", "What slots are possible after 1 PM if I reach around 12:45?", "10:02",
     "options"),
    ("L2a", 2, "THR003", "DRV013", "SHP1013", "I am late by one hour.", "09:19", "ask"),
    ("L2b", 2, "THR010", "DRV004", None, "I will be late by 45 minutes.", "09:31", "ask_shipment"),
    ("L6", 6, "THR005", "DRV015", "SHP1015", "Evening traffic. ETA 6:30. Can the reefer unload tonight?", "16:24",
     "escalate"),
]


def main() -> int:
    record = "--record" in sys.argv[1:]
    sys.stdout.reconfigure(encoding="utf-8")
    t_start = time.perf_counter()
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DBNAME}")
    migrate(DSN)
    info = seed(DSN)
    refused = sorted(label.split()[0] for label, _ in info["rejected"])
    print("SetuHaul scenario run on the supplied snapshot (data/setuhaul_freight_operations.db)")
    print(f"database {DBNAME} on {BASE_DSN}; reader: deterministic rules reader (no model call)")
    print(f"seed: bookings the database refused (known-bad, audit A03/A04/A08): {refused}")

    clock = Clock()
    app = create_app(Settings(dsn=DSN, cognito_issuer="https://example.invalid/issuer", cognito_client_ids={"c"},
                              session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                              facilities=("FAC-JAI-01", "FAC-GGN-01"), jwks={"keys": []},
                              booking_enabled=False, local_test_harness=True), clock=clock)
    client = TestClient(app)
    orch = Orchestrator(app.state.service, app.state.sessions, model=None, clock=clock)   # rules reader, snapshot clock

    checks, turns = [], []
    for cid, level, thread, drv, sid, text, hhmm, expect in CASES:
        h, m = map(int, hhmm.split(":"))
        clock.now = datetime(2026, 8, 4, h, m, tzinfo=IST)
        phone = one("SELECT phone_e164 FROM setuhaul.drivers WHERE driver_id=%s", drv)[0]
        t0 = time.perf_counter()
        r = orch.handle(say(client, phone, text))
        ms = round((time.perf_counter() - t0) * 1000)
        row = {"case": cid, "ladder_level": level, "snapshot_thread": thread, "driver": drv, "shipment": sid,
               "clock": clock.now.isoformat(), "driver_says": text, "agent_replies": r.reply, "path": r.path,
               "turn_ms": ms, "tools": [list(t) for t in r.tools], "extraction": r.extraction}
        print(f"\n== {cid} (ladder level {level}) {thread} {drv} {sid or '(two live shipments)'} at {hhmm} IST")
        print(f"   driver: {text!r}")
        print(f"   agent : {r.reply!r}")
        print(f"   path  : {r.path}   ({ms} ms)")
        if expect == "options":
            ok = "options" in r.path and r.reply.startswith("Available slots")
            n_appts = one("""SELECT count(*) FROM setuhaul.appointments WHERE shipment_id=%s
                             AND status IN ('HELD','PENDING_CONFIRMATION')""", sid)[0]
            row["evidence"] = {"new_holds_or_requests": n_appts}
            checks.append((f"{cid}: options shown, nothing held or requested", ok and n_appts == 0))
        elif expect == "ask":
            ok = any(p.startswith("ask:") for p in r.path) and "options" not in r.path
            checks.append((f"{cid}: asks one clarification question before any option", ok))
        elif expect == "ask_shipment":
            ok = any("shipment" in p for p in r.path) and "options" not in r.path
            checks.append((f"{cid}: asks which shipment (two live), offers nothing", ok))
        elif expect == "escalate":
            esc = one("""SELECT reason_code, mode, status, handoff, created_at FROM setuhaul.escalations
                         WHERE shipment_id=%s ORDER BY created_at DESC LIMIT 1""", sid)
            ok = ("escalated:NO_FEASIBLE_SLOT" in r.path and "coordinator" in r.reply.lower()
                  and esc is not None and esc[0] == "NO_FEASIBLE_SLOT" and esc[2] == "OPEN" and bool(esc[3]))
            offered = one("""SELECT count(*) FROM setuhaul.appointments WHERE shipment_id=%s
                             AND status IN ('HELD','PENDING_CONFIRMATION')""", sid)[0]
            row["evidence"] = {"escalation": {"reason_code": esc[0], "mode": esc[1], "status": esc[2],
                                              "handoff": esc[3]} if esc else None,
                               "slots_invented_or_held": offered}
            print(f"   escalation: {esc[0] if esc else None} mode={esc[1] if esc else None} "
                  f"status={esc[2] if esc else None}")
            print(f"   hand-off  : {json.dumps(esc[3], default=str)[:600] if esc else None}")
            checks.append((f"{cid}: NO_FEASIBLE_SLOT escalation, OPEN, with a hand-off record", ok))
            checks.append((f"{cid}: no slot invented or held for the escalated shipment", offered == 0))
            # Why, independently of the service: the same engine on the SQLite snapshot, and the dock events table
            pure = FeasibilityEngine(load()).find_options(sid, clock.now)
            import sqlite3
            lite = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
            reefer = lite.execute("""SELECT d.dock_code, e.event_type, e.event_start_ts, e.event_end_ts
                                     FROM dock_status_events e JOIN docks d USING (dock_id)
                                     WHERE d.facility_id='FAC-JAI-01' AND d.supports_refrigerated=1""").fetchall()
            n_reefer = lite.execute("""SELECT count(*) FROM docks WHERE facility_id='FAC-JAI-01'
                                       AND supports_refrigerated=1""").fetchone()[0]
            row["why"] = {"snapshot_engine_outcome": pure.outcome_code, "facts": pure.facts,
                          "reefer_docks_at_jaipur": n_reefer, "reefer_dock_events": reefer,
                          "rejections": {str(k): str(v) for k, v in dict(pure.rejections).items()}}
            print(f"   why: snapshot engine says {pure.outcome_code}; Jaipur reefer docks: {n_reefer}; "
                  f"their events: {reefer}; capability {pure.facts.get('capability')}, ready {pure.facts.get('ready_ts')}")
            checks.append((f"{cid}: independent check: snapshot engine also finds no feasible slot",
                           pure.outcome_code == "NO_FEASIBLE_SLOT"))
        turns.append(row)

    print("\n== Checks")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    passed = sum(ok for _, ok in checks)
    elapsed = time.perf_counter() - t_start
    print(f"\nRESULT: {'PASS' if passed == len(checks) else 'FAIL'}  ({passed}/{len(checks)} checks)  "
          f"elapsed {elapsed:.1f} s")
    if record:
        OUT.write_text(json.dumps({"recorded_at": datetime.now(IST).isoformat(timespec="seconds"),
                                   "data": "supplied snapshot data/setuhaul_freight_operations.db via pg_seed",
                                   "reader": "deterministic rules reader (no model call)",
                                   "booking": "off in this run (snapshot date is in the past for the database)",
                                   "refused_seed_bookings": refused, "turns": turns,
                                   "checks": [{"check": n, "pass": ok} for n, ok in checks]},
                                  indent=1, default=str, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"recorded: {OUT.relative_to(ROOT)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
