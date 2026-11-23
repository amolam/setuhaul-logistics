"""Phase G7: the ISOLATED local environment for the load test. Never the pilot, never production.

    python -m perf.serve_local --port 8765 --fixtures <scratch>/perf_fixtures.json
    locust -f perf/locustfile.py --headless -u 5 -r 1 -t 3m --host http://127.0.0.1:8765 --csv <scratch>/perf

What runs: a fresh local PostgreSQL database (`setuhaul_phase_g_perf`, the Phase G synthetic dataset), the real FastAPI
app under uvicorn on 127.0.0.1, booking under the local test harness (no AWS gates), the optimizer ranking on, and
two in-process agent workers that drain the local inbound queue through the real orchestrator. The model is ABSENT:
the deterministic rules reader extracts (no paid call, no network). Tokens are signed with a throwaway local key.
On exit (Ctrl+C or --seconds) it writes database-side measurements (turn, tool, optimizer and queue times) next to the
fixtures file.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
import uvicorn  # noqa: E402

from evals.phase_g.local_env import (GGN, JAI, META_SECRET, add_synthetic_driver, fresh_database, make_app,  # noqa: E402
                                     make_keys, staff_token)
from setuhaul.agent.orchestrator import Orchestrator  # noqa: E402


def workers(app, n: int, stop: threading.Event, stats: dict):
    orch = Orchestrator(app.state.service, app.state.sessions, None)          # no model: the rules reader

    def loop():
        while not stop.is_set():
            ev = app.state.inbound.receive()
            if ev is None:
                time.sleep(0.02)
                continue
            try:
                orch.handle(ev)
                stats["turns"] += 1
            except Exception:                                                 # noqa: BLE001 — counted, the run goes on
                stats["turn_errors"] += 1
    ts = [threading.Thread(target=loop, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    return ts


def db_measurements(dsn: str, since: float) -> dict:
    q = lambda c, sql: c.execute(sql).fetchone()
    with psycopg.connect(dsn) as c:
        c.execute("SET search_path = setuhaul")
        turn = q(c, """SELECT count(*), percentile_disc(0.5) WITHIN GROUP (ORDER BY turn_ms),
                              percentile_disc(0.95) WITHIN GROUP (ORDER BY turn_ms), max(turn_ms) FROM agent_turn_metrics""")
        tool = c.execute("""SELECT t->>'tool', count(*), percentile_disc(0.5) WITHIN GROUP (ORDER BY (t->>'ms')::int),
                                   percentile_disc(0.95) WITHIN GROUP (ORDER BY (t->>'ms')::int)
                              FROM agent_turn_metrics m, jsonb_array_elements(m.tools) t GROUP BY 1 ORDER BY 2 DESC""").fetchall()
        opt = q(c, """SELECT count(*), percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms),
                             percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms), max(duration_ms),
                             count(*) FILTER (WHERE solver_status NOT IN ('OPTIMAL','FEASIBLE','INFEASIBLE')) FROM optimization_runs""")
        queue = q(c, """SELECT count(*), count(*) FILTER (WHERE processed_at IS NULL),
                               percentile_disc(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - created_at) * 1000),
                               percentile_disc(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - created_at) * 1000)
                          FROM inbound_receipts""")
        appts = dict(c.execute("SELECT status, count(*) FROM appointments WHERE booking_source <> 'PLANNER' GROUP BY 1").fetchall())
        overlaps = q(c, """SELECT count(*) FROM dock_occupancy a JOIN dock_occupancy b ON a.dock_id = b.dock_id
                            AND a.occupancy_id < b.occupancy_id AND a.active AND b.active AND a.during && b.during""")[0]
    return {"agent_turns": {"n": turn[0], "p50_ms": turn[1], "p95_ms": turn[2], "max_ms": turn[3]},
            "tool_calls_ms (DB-bound; DB time proxy)": {r[0]: {"n": r[1], "p50": r[2], "p95": r[3]} for r in tool},
            "optimizer": {"runs": opt[0], "p50_ms": opt[1], "p95_ms": opt[2], "max_ms": opt[3], "unsafe_status": opt[4]},
            "queue_time_ms (receipt stored -> turn processed)": {"messages": queue[0], "unprocessed": queue[1],
                                                                "p50": float(queue[2]) if queue[2] is not None else None,
                                                                "p95": float(queue[3]) if queue[3] is not None else None},
            "bookings_created_by_status": appts, "double_booked_dock_overlaps": overlaps,
            "pure_db_time": "unavailable: pg_stat_statements is not installed in the local cluster"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--drivers", type=int, default=120)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    ap.add_argument("--fixtures", required=True)
    a = ap.parse_args()
    dsn, report = fresh_database("setuhaul_phase_g_perf")
    keys = make_keys()
    app = make_app(dsn, keys, booking=True, optimizer_ranking=True, console=True)
    drivers = []
    for i in range(a.drivers):
        _, phone, sids = add_synthetic_driver(dsn, facility=JAI if i % 5 else GGN, eta_min=60 + (i * 7) % 240)
        drivers.append({"phone": phone, "shipment_id": sids[0], "facility": JAI if i % 5 else GGN})
    fx = {"meta_secret": META_SECRET.decode(), "drivers": drivers,
          "planner": staff_token(keys, ("planner",), (JAI, GGN), ttl_s=6 * 3600),
          "ops_lead": staff_token(keys, ("ops_lead",), (JAI, GGN), ttl_s=6 * 3600),
          "dataset": {k: report[k] for k in ("coverage_pct", "extra_synthetic_slots", "non_synthetic_identities")}}
    Path(a.fixtures).write_text(json.dumps(fx), encoding="utf-8")
    stop, stats = threading.Event(), {"turns": 0, "turn_errors": 0}
    workers(app, a.workers, stop, stats)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning", access_log=False))
    t0 = time.time()
    if a.seconds:
        threading.Timer(a.seconds, lambda: setattr(server, "should_exit", True)).start()
    print(f"READY http://127.0.0.1:{a.port} drivers={len(drivers)} fixtures={a.fixtures}", flush=True)
    server.run()
    time.sleep(3)                                                    # let the workers finish queued turns
    stop.set()
    out = {"worker_stats": stats, "db": db_measurements(dsn, t0), "wall_s": round(time.time() - t0, 1)}
    Path(a.fixtures).with_name("perf_db_measurements.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out, default=str), flush=True)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
