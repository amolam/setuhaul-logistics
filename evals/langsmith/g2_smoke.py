"""Phase G.2: a minimal LangSmith evaluation integration over 10 existing synthetic scenarios.

One workflow, end to end:  agent execution -> LangSmith trace -> dataset -> deterministic evaluators -> experiment.

    python -m evals.langsmith.g2_smoke --check-config   # verify the configuration only (no network, no database)
    python -m evals.langsmith.g2_smoke --offline        # run + evaluate the 10 locally, tracing off, nothing uploaded
    python -m evals.langsmith.g2_smoke                  # full: dataset (create or reuse), traced run, experiment

Configuration (standard LangSmith names; nothing is read at application start-up, nothing is committed):
    LANGSMITH_TRACING   true | on       required for the full run (off by default)
    LANGSMITH_API_KEY   the key         set by the owner in the shell, never logged or printed (only its shape)
    LANGSMITH_PROJECT   default setuhaul-synthetic-pilot (where the agent traces go)
    LANGSMITH_ENDPOINT  only if the workspace needs it: https://api.smith.langchain.com (default) or the EU endpoint

What it reuses (nothing new on the agent path): the Phase G executors and isolated LOCAL database
(evals/phase_g/run_eval.py, local_env.py), the scripted model (FakeModel, $0), the G.1 tracer, redaction and export
guard (setuhaul/observability/tracing.py, redaction.py) and the G.1 REST client (observability/langsmith.py). The
agent's existing spans are the trace; this script only installs an exporting tracer for its own process.

Restrictions: local database only (no RDS Data API, never SETUHAUL_ENV=pilot/staging), no paid model call, no
AWS, no Meta/WhatsApp. LangSmith being down never stops the agent: the tracer swallows export errors; the script
still evaluates locally and says what it could not upload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.langsmith import g2_evaluators as ev  # noqa: E402
from setuhaul.observability.redaction import find_leaks, redact  # noqa: E402

PHASE_G = ROOT / "evals" / "phase_g" / "dataset.jsonl"
RESULTS = ROOT / "evals" / "results"
DATASET_NAME = "setuhaul-g2-smoke-10"
DEFAULT_PROJECT = "setuhaul-synthetic-pilot"
KEY_ENVIRONMENTS = ("dev", "eval", "ci")
MAX_MODEL_COST_USD = 0.0            # the model is scripted (FakeModel): no paid call is possible from this script

# The 10 scenarios, all existing rows of evals/phase_g/dataset.jsonl (original ids kept), with the reason each was
# picked. Order = execution order.
SELECTION = [
    ("C01", "normal_booking", "delay with a time: the optimizer ranks feasible options, nothing is held yet"),
    ("C03", "normal_booking", "the driver's earliest start is carried into the tool and honoured by every option"),
    ("C04", "normal_booking", "the full flow (options, pick, explicit yes) ends in a PENDING request for a human"),
    ("C06", "policy_authorization", "the model claims a yes the driver did not give: no hold, no request"),
    ("T04", "policy_authorization", "a driver session naming another driver's shipment is refused (403)"),
    ("A10", "approval_boundary", "the operations role cannot apply a schedule move (planner approval only)"),
    ("T06", "approval_boundary", "deciding a booking request is not an agent tool (404): only a human approves"),
    ("T05", "idempotency", "the same report on the same turn answers the same and stores one ETA signal"),
    ("C08", "failure_refusal", "the model is down: the rules reader answers, options still offered, no booking"),
    ("C12", "driver_communication", "a status question gets 'no active booking' and an offer, never a booking"),
]
CATEGORY_COUNTS = {"normal_booking": 3, "policy_authorization": 2, "approval_boundary": 2, "idempotency": 1,
                   "failure_refusal": 1, "driver_communication": 1}


# ---------------------------------------------------------------- configuration
def _on(v: str | None) -> bool:
    return (v or "").strip().lower() in ("on", "true", "1", "yes")


def config(env: dict) -> dict:
    """What the full run would use. Never contains the key (only whether it is present and its shape)."""
    from setuhaul.agent.errors import key_shape
    from setuhaul.observability.langsmith import ALLOWED_ENDPOINTS
    environment = (env.get("SETUHAUL_ENV") or "eval").strip().lower()
    endpoint = (env.get("LANGSMITH_ENDPOINT") or ALLOWED_ENDPOINTS[0]).strip().rstrip("/")
    if endpoint.endswith("/api/v1"):
        endpoint = endpoint[: -len("/api/v1")]
    key = (env.get("LANGSMITH_API_KEY") or "").strip()
    problems = []
    tracing_on = _on(env.get("LANGSMITH_TRACING"))
    if not tracing_on:
        problems.append("LANGSMITH_TRACING is not on (tracing is off by default)")
    if not key:
        problems.append("LANGSMITH_API_KEY is not set")
    if environment not in KEY_ENVIRONMENTS:
        problems.append(f"SETUHAUL_ENV={environment}: the smoke runs from dev/eval/ci only, never pilot/staging")
    if endpoint not in ALLOWED_ENDPOINTS:
        problems.append(f"LANGSMITH_ENDPOINT {endpoint!r} is not one of {ALLOWED_ENDPOINTS}")
    if (env.get("SETUHAUL_DSN") or "").startswith("rds-data") or (env.get("SETUHAUL_TEST_DSN") or "").startswith("rds-data"):
        problems.append("SETUHAUL_DSN points at the RDS Data API (the pilot): local database only")
    return {"ready": not problems, "tracing": tracing_on, "environment": environment,
            "project": (env.get("LANGSMITH_PROJECT") or DEFAULT_PROJECT).strip(), "endpoint": endpoint,
            "ui": endpoint.replace("://api.", "://").replace("://eu.api.", "://eu."),
            "key": f"present ({key_shape(key)})" if key else "missing", "problems": problems,
            "model": "scripted FakeModel (no paid call)", "max_model_cost_usd": MAX_MODEL_COST_USD}


# ---------------------------------------------------------------- dataset
def _source_rows() -> dict[str, dict]:
    rows = [json.loads(x) for x in PHASE_G.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["id"]: r for r in rows}


def _policy(expected: dict) -> str:
    a = str(expected.get("authorization") or "").upper()
    return "DENIED" if a.startswith("DENIED") else "ALLOWED"


def build() -> list[dict]:
    """The 10 examples: scenario id, input, expected outcome, expected policy decision, category. Deterministic."""
    src = _source_rows()
    missing = [sid for sid, _, _ in SELECTION if sid not in src]
    if missing:
        raise ValueError(f"selected scenarios missing from {PHASE_G.name}: {missing}")
    out = []
    for sid, cat, why in SELECTION:
        r = src[sid]
        expected = {k: v for k, v in r["expected"].items() if k not in ("authorization", "label_note", "response_traits")}
        ex = {"scenario_id": sid, "category": cat,
              "inputs": r["input"],
              "outputs": {"expected_outcome": expected, "expected_policy_decision": _policy(r["expected"])},
              "metadata": {"scenario_id": sid, "category": cat, "title": r["title"], "why": why,
                           "original_category": r["category"], "source": "evals/phase_g/dataset.jsonl",
                           "synthetic": True, "evaluators": ev.APPLIES[cat]}}
        ex["metadata"]["content_sha"] = _sha(ex["inputs"], ex["outputs"])
        out.append(ex)
    counts = {c: sum(e["category"] == c for e in out) for c in CATEGORY_COUNTS}
    if counts != CATEGORY_COUNTS or len(out) != 10:
        raise ValueError(f"category counts {counts} != {CATEGORY_COUNTS}")
    leaks = find_leaks([{k: e[k] for k in ("inputs", "outputs", "metadata")} for e in out])
    if leaks:                                       # synthetic only: refuse to upload anything secret- or PII-shaped
        raise ValueError(f"dataset content matches a redaction pattern: {leaks}")
    return out


def _sha(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def sync_dataset(client, examples: list[dict]) -> dict:
    """Create the dataset once and add only the scenarios it does not hold yet (matched on scenario_id). Re-running
    creates nothing. A stored example whose content differs is reported, never silently replaced."""
    found = client.find_dataset(DATASET_NAME)
    created = False
    if found is None:
        found = client.create_dataset(DATASET_NAME, "SetuHaul G.2 smoke: 10 existing synthetic scenarios "
                                      "(synthetic data only)", {"phase": "G.2", "examples": len(examples),
                                                                "evaluator_version": ev.EVALUATOR_VERSION})
        created = True
    ds_id = found["id"]
    existing = {} if created else {
        (x.get("metadata") or {}).get("scenario_id"): x for x in client.list_examples(ds_id)}
    new = [e for e in examples if e["scenario_id"] not in existing]
    if new:
        client.create_examples(ds_id, [{"inputs": e["inputs"], "outputs": e["outputs"], "metadata": e["metadata"]}
                                       for e in new])
        existing.update({(x.get("metadata") or {}).get("scenario_id"): x for x in client.list_examples(ds_id)})
    drift = sorted(e["scenario_id"] for e in examples if e["scenario_id"] in existing
                   and (existing[e["scenario_id"]].get("metadata") or {}).get("content_sha") != e["metadata"]["content_sha"])
    return {"dataset_id": ds_id, "tenant_id": found.get("tenant_id"), "created": created,
            "examples_added": [e["scenario_id"] for e in new], "content_drift": drift,
            "example_ids": {sid: x.get("id") for sid, x in existing.items() if sid}}


# ---------------------------------------------------------------- execution (the unchanged Phase G executors)
class RecordingExporter:
    """Wraps the G.1 LangSmithExporter: remembers the root run ids of batches LangSmith ACCEPTED (a failed post is
    not recorded, so no trace is ever reported that was not stored)."""

    def __init__(self, inner):
        self.inner, self.roots = inner, []

    def post(self, batch: list[dict]) -> None:
        self.inner.post(batch)
        self.roots += [p["id"] for p in batch if not p.get("parent_run_id")]


def run_examples(examples: list[dict], tracer=None, executors=None, env_factory=None) -> dict[str, dict]:
    """Run each scenario once through the Phase G executor for its kind (fresh local database, scripted model)."""
    from setuhaul.observability import tracing
    if executors is None or env_factory is None:
        from evals.phase_g import run_eval
        executors, env_factory = executors or run_eval.EXECUTORS, env_factory or run_eval.Env
    src = _source_rows()
    old = tracing.tracer()
    if tracer is not None:
        tracing.install(tracer)
    out = {}
    try:
        env = env_factory()
        for e in examples:
            sid = e["scenario_id"]
            row = src[sid]
            rec = getattr(getattr(tracer, "exporter", None), "roots", None)
            mark = len(rec) if rec is not None else 0
            t0 = time.perf_counter()
            try:
                r = executors[row["input"]["kind"]](env, row)
                res = {"result": "PASS" if not r["fail"] else "FAIL", "checks": r["checks"],
                       "failure_reason": "; ".join(r["fail"]) or None, "tools": r.get("tools", [])}
            except Exception as x:                                    # noqa: BLE001 — a crash is a failed row
                res = {"result": "FAIL", "checks": {}, "failure_reason": f"{type(x).__name__}: {x}"[:300], "tools": []}
            res["ms"] = int((time.perf_counter() - t0) * 1000)
            if tracer is not None:
                res["flush"] = tracer.flush(10.0)
                res["trace_ids"] = list(rec[mark:]) if rec is not None else []
            out[sid] = res
    finally:
        tracing.install(old)
    return out


def evaluate(examples: list[dict], results: dict[str, dict]) -> dict:
    rows = []
    for e in examples:
        res = results[e["scenario_id"]]
        scores = ev.score(e, res)
        ok = res["result"] == "PASS" and all(s["score"] != 0 for s in scores.values())
        rows.append({"scenario_id": e["scenario_id"], "category": e["category"], "result": "PASS" if ok else "FAIL",
                     "runner_result": res["result"], "failure_reason": res.get("failure_reason"), "scores": scores,
                     "trace_ids": res.get("trace_ids", [])})
    by_eval = {}
    for r in rows:
        for k, s in r["scores"].items():
            b = by_eval.setdefault(k, {"pass": 0, "fail": 0, "n_a": 0})
            b["pass" if s["score"] == 1 else "fail" if s["score"] == 0 else "n_a"] += 1
    passed = sum(r["result"] == "PASS" for r in rows)
    return {"totals": {"total": len(rows), "passed": passed, "failed": len(rows) - passed},
            "by_evaluator": by_eval, "examples": rows}


# ---------------------------------------------------------------- experiment upload
def upload_experiment(client, ds: dict, examples: list[dict], report: dict, project: str) -> dict:
    """One experiment session on the dataset; one run per example (linked to its example), with the agent trace ids
    in its outputs, and one feedback score per applicable evaluator."""
    name = f"{DATASET_NAME}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    sess = client.create_experiment(name, ds["dataset_id"], {"evaluator_version": ev.EVALUATOR_VERSION,
                                                             "mode": "scripted", "model_cost_usd": 0,
                                                             "trace_project": project, "phase": "G.2"}) or {}
    by_id = {e["scenario_id"]: e for e in examples}
    posts, feedback = [], []
    for r in report["examples"]:
        rid, now = str(uuid.uuid4()), datetime.now(timezone.utc)
        e = by_id[r["scenario_id"]]
        posts.append({"id": rid, "trace_id": rid, "name": f"g2:{r['scenario_id']}", "run_type": "chain",
                      "start_time": now.isoformat(), "end_time": now.isoformat(), "session_name": name,
                      "dotted_order": f"{now:%Y%m%dT%H%M%S%fZ}{rid}",
                      "reference_example_id": ds["example_ids"].get(r["scenario_id"]),
                      "inputs": redact(e["inputs"], content_allowed=True),
                      "outputs": {"result": r["result"], "runner_result": r["runner_result"],
                                  "agent_trace_ids": r["trace_ids"], "trace_project": project},
                      "extra": {"metadata": {"scenario_id": r["scenario_id"], "category": r["category"],
                                             "evaluator_version": ev.EVALUATOR_VERSION}},
                      "tags": ["setuhaul", "g2-smoke", "eval"]})
        feedback += [(rid, k, s["score"], s["comment"]) for k, s in r["scores"].items() if s["score"] is not None]
    leaks = find_leaks([{k: p[k] for k in ("inputs", "outputs", "extra", "name", "tags")} for p in posts])
    if leaks:
        raise ValueError(f"experiment runs match a redaction pattern: {leaks}")
    client.post_runs(posts)
    for rid, k, s, c in feedback:
        client.create_feedback(rid, k, s, c)
    return {"experiment_name": name, "experiment_id": sess.get("id"), "tenant_id": sess.get("tenant_id"),
            "runs": len(posts), "feedback": len(feedback)}


def trace_link(client, ui: str, run_id: str, attempts: int = 6) -> str | None:
    """The UI link LangSmith gives the stored run (`app_path`); ingestion is asynchronous, so ask a few times."""
    for i in range(attempts):
        try:
            r = client.get_run(run_id) or {}
            if r.get("app_path"):
                return ui.rstrip("/") + r["app_path"]
        except Exception:                                             # noqa: BLE001 — not stored yet
            pass
        time.sleep(min(2.0 * (i + 1), 5.0))
    return None


# ---------------------------------------------------------------- command
def _summary(report: dict) -> None:
    t = report["totals"]
    print(f"examples {t['total']}: {t['passed']} passed, {t['failed']} failed (evaluators {ev.EVALUATOR_VERSION})")
    for r in report["examples"]:
        sc = " ".join(f"{k}={'-' if s['score'] is None else int(s['score'])}" for k, s in r["scores"].items())
        print(f"  {r['result']:4} {r['scenario_id']:4} {r['category']:22} traces={len(r['trace_ids'])} {sc}"
              + (f"  [{r['failure_reason']}]" if r["failure_reason"] else ""))
    print("by evaluator: " + json.dumps(report["by_evaluator"]))


def main(argv=None, http=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-config", action="store_true", help="verify the configuration only")
    ap.add_argument("--offline", action="store_true", help="run and evaluate locally; tracing off, nothing uploaded")
    a = ap.parse_args(argv)
    env = dict(os.environ)
    cfg = config(env)
    print("config: " + json.dumps({k: v for k, v in cfg.items() if k != "ui"}))
    examples = build()
    print(f"dataset {DATASET_NAME}: {len(examples)} examples " + " ".join(e["scenario_id"] for e in examples))
    print(f"model calls: scripted, estimated maximum model cost ${MAX_MODEL_COST_USD:.2f}")
    if a.check_config:
        return 0 if cfg["ready"] else 2
    from evals.langsmith.run import guard_local_only
    guard_local_only(env)
    if a.offline:
        report = evaluate(examples, run_examples(examples))
        _summary(report)
        _write(report, cfg, None)
        return 0 if report["totals"]["failed"] == 0 else 1
    if not cfg["ready"]:
        print("NOT RUN: " + "; ".join(cfg["problems"]) + ". See docs/phase-g2/LANGSMITH_G2_SMOKE.md (setup).")
        return 2

    from setuhaul.observability import tracing
    from setuhaul.observability.langsmith import LangSmithClient, LangSmithExporter
    key = env["LANGSMITH_API_KEY"].strip()
    client = LangSmithClient(key, cfg["endpoint"], http=http, timeout_s=10.0)
    ls: dict = {}
    try:
        ls["dataset"] = sync_dataset(client, examples)
        tid = ls["dataset"].get("tenant_id")
        ls["dataset_url"] = f"{cfg['ui']}/o/{tid}/datasets/{ls['dataset']['dataset_id']}" if tid else None
    except Exception as x:                                            # noqa: BLE001 — LangSmith down: run locally
        ls["error"] = f"dataset sync failed ({type(x).__name__}: {str(x)[:120]}); evaluated locally, nothing uploaded"
    exporter = RecordingExporter(LangSmithExporter(LangSmithClient(key, cfg["endpoint"], http=http, timeout_s=5.0)))
    tracer = tracing.Tracer(tracing.TracerConfig(enabled=True, project=cfg["project"], environment=cfg["environment"],
                                                 agent_version="g2-smoke"), exporter)
    report = evaluate(examples, run_examples(examples, tracer=tracer))
    report["tracer_stats"] = dict(tracer.stats)
    if "error" not in ls:
        try:
            ls["experiment"] = upload_experiment(client, ls["dataset"], examples, report, cfg["project"])
            tid = ls["experiment"].get("tenant_id") or ls["dataset"].get("tenant_id")
            if tid and ls["experiment"].get("experiment_id"):
                ls["experiment_url"] = (f"{cfg['ui']}/o/{tid}/datasets/{ls['dataset']['dataset_id']}/compare"
                                        f"?selectedSessions={ls['experiment']['experiment_id']}")
        except Exception as x:                                        # noqa: BLE001
            ls["error"] = f"experiment upload failed ({type(x).__name__}: {str(x)[:120]})"
    first = next((t for r in report["examples"] for t in r["trace_ids"]), None)
    ls["trace_url"] = trace_link(client, cfg["ui"], first) if first else None
    ls["trace_id"] = first
    report["langsmith"] = ls
    _summary(report)
    print(f"tracer: {json.dumps(report['tracer_stats'])}")
    for k in ("dataset_url", "experiment_url", "trace_url"):
        print(f"{k}: {ls.get(k) or 'not available'}")
    if ls.get("dataset"):
        print(f"dataset created={ls['dataset']['created']} added={ls['dataset']['examples_added']} "
              f"drift={ls['dataset']['content_drift']}")
    if ls.get("error"):
        print("LANGSMITH: " + ls["error"])
    _write(report, cfg, ls)
    return 3 if ls.get("error") else (0 if report["totals"]["failed"] == 0 else 1)


def _write(report: dict, cfg: dict, ls: dict | None) -> Path:
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"g2_smoke_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    report.update(dataset_name=DATASET_NAME, evaluator_version=ev.EVALUATOR_VERSION, mode="scripted",
                  model_cost_usd=0.0, config={k: v for k, v in cfg.items() if k != "ui"},
                  selection=[{"scenario_id": s, "category": c, "why": w} for s, c, w in SELECTION])
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"report -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return out


if __name__ == "__main__":
    sys.exit(main())
