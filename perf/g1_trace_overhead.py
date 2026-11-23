"""Phase G.1 performance: what tracing costs a driver turn, how long a flush can take, and the cold-start imports.

    python perf/g1_trace_overhead.py [--turns 30]

Local only: the turns run on a fresh local PostgreSQL database (evals/phase_g/local_env), the model is scripted, and
the LangSmith exporter is an in-process capture (or one that fails / is slow): no network call, no cost. Results are
written to evals/results/g1_perf_<stamp>.json.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Capture:
    def __init__(self, fail=False, delay_s=0.0):
        self.batches, self.fail, self.delay_s = [], fail, delay_s

    def post(self, batch):
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise ConnectionError("langsmith down")
        self.batches.append(batch)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def micro(n=2000) -> dict:
    """One turn's worth of spans (13 runs, like a real options turn), redacted and queued, without a database."""
    from setuhaul.observability import tracing
    t = tracing.Tracer(tracing.TracerConfig(enabled=True, max_queue=n + 10), Capture())
    names = ["intent_extraction", "model_call", "parameter_check", "decide", "get_driver_context",
             "record_driver_report", "find_feasible_options", "optimizer", "final_response", "a", "b", "c"]
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        with t.turn(str(uuid.uuid4()), inputs={"text": "Traffic, reaching 4:30 pm"}, metadata={"correlation_id": "x"}):
            for nm in names:
                with t.span(nm, inputs={"message_id": 1, "declared_eta": "2026-11-23T16:30:00+05:30"}) as s:
                    s.end({"status": "OK", "options": 3})
        xs.append((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    t.flush(60)
    flush_ms = (time.perf_counter() - t0) * 1000
    return {"turns": n, "runs_per_turn": len(names) + 1, "record_ms_p50": round(pct(xs, 50), 3),
            "record_ms_p95": round(pct(xs, 95), 3), "export_capture_ms_per_turn": round(flush_ms / n, 3)}


def flush_bounds() -> dict:
    """Flush with a failing and with a slow exporter: bounded, never raises."""
    from setuhaul.observability import tracing
    out = {}
    for label, exp, budget in (("failing", Capture(fail=True), 0.5), ("slow_1s_per_post", Capture(delay_s=1.0), 0.5)):
        t = tracing.Tracer(tracing.TracerConfig(enabled=True, max_attempts=2), exp)
        for _ in range(5):
            with t.turn(str(uuid.uuid4())):
                pass
        t0 = time.perf_counter()
        stats = t.flush(budget)
        out[label] = {"budget_s": budget, "elapsed_s": round(time.perf_counter() - t0, 3),
                      "exported": stats["exported"], "dropped": stats["dropped"], "pending": stats["pending"],
                      "export_errors": stats["export_errors"]}
    return out


def turns(n: int) -> dict:
    """Real orchestrator turns on a local database: tracing off vs on (capture exporter) vs on (failing exporter)."""
    from fastapi.testclient import TestClient

    from evals.phase_g.local_env import add_synthetic_driver, fresh_database, make_app, make_keys, signed_message
    from evals.phase_g.run_eval import exd
    from setuhaul.agent.models import FakeModel
    from setuhaul.agent.orchestrator import Orchestrator
    from setuhaul.observability import tracing
    dsn, _ = fresh_database("setuhaul_phase_g1_perf")
    keys = make_keys()
    app = make_app(dsn, keys, booking=True, optimizer_ranking=True)
    client = TestClient(app)
    res = {}
    for label, tracer in (("tracing_off", tracing.Tracer()),
                          ("tracing_on_capture", tracing.Tracer(tracing.TracerConfig(enabled=True), Capture())),
                          ("tracing_on_failing_exporter", tracing.Tracer(tracing.TracerConfig(enabled=True), Capture(fail=True)))):
        tracing.install(tracer)
        xs, flush = [], []
        for i in range(n + 3):
            _, phone, _ = add_synthetic_driver(dsn)
            mid = f"wamid.{uuid.uuid4().hex}"
            body, headers = signed_message(phone, "Traffic, reaching late", mid)
            client.post("/webhooks/whatsapp", content=body, headers=headers)
            ev = next(e for e in reversed(app.state.inbound) if e.get("external_message_id") == mid)
            model = FakeModel(extractions=[exd(delay_kind="DELAY_DURATION", declared_delay_min=150,
                                               exception_type="TRAFFIC")])
            t0 = time.perf_counter()
            Orchestrator(app.state.service, app.state.sessions, model).handle(ev)
            ms = (time.perf_counter() - t0) * 1000
            t1 = time.perf_counter()
            tracer.flush(0.5) if tracer.enabled else None
            if i >= 3:                                        # warm-up turns are not counted
                xs.append(ms)
                flush.append((time.perf_counter() - t1) * 1000)
        res[label] = {"n": n, "turn_ms_p50": round(pct(xs, 50), 1), "turn_ms_p95": round(pct(xs, 95), 1),
                      "turn_ms_mean": round(statistics.mean(xs), 1), "flush_ms_p50": round(pct(flush, 50), 2),
                      "flush_ms_max": round(max(flush), 2)}
    tracing.install(tracing.Tracer())
    off, on = res["tracing_off"]["turn_ms_p50"], res["tracing_on_capture"]["turn_ms_p50"]
    res["overhead_p50_ms"] = round(on - off, 1)
    res["overhead_p50_pct"] = round(100 * (on - off) / off, 1) if off else None
    return res


def cold_imports() -> dict:
    """Import time of the new modules in a fresh interpreter (what a Lambda cold start adds)."""
    code = ("import time;t=time.perf_counter();import setuhaul.agent.orchestrator;a=time.perf_counter();"
            "import setuhaul.observability.langsmith, setuhaul.observability.tracing, setuhaul.agent.errors;"
            "b=time.perf_counter();print(round((a-t)*1000,1), round((b-a)*1000,1))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT).stdout.split()
    size = sum(p.stat().st_size for p in (ROOT / "setuhaul" / "observability").glob("*.py")) + \
        (ROOT / "setuhaul" / "agent" / "errors.py").stat().st_size
    return {"orchestrator_import_ms": float(out[0]), "extra_g1_modules_import_ms": float(out[1]),
            "new_source_bytes": size, "new_dependencies": []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=30)
    a = ap.parse_args()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "micro": micro(), "flush": flush_bounds(),
              "cold_start": cold_imports(), "turns": turns(a.turns)}
    out = ROOT / "evals" / "results" / f"g1_perf_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))
    print("->", out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
