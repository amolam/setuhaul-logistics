"""Phase G.1 (G2): run the evaluation, score it with the deterministic evaluators, compare, and (optionally) sync.

    python -m evals.langsmith.run                       # offline: local isolated DB, scripted model, $0
    python -m evals.langsmith.run --skip-phase-g        # only the rules-reader regression cases (no database)
    python -m evals.langsmith.run --compare evals/results/phase_g_eval_20261123_1024.json
    python -m evals.langsmith.run --manifest            # print the dataset manifest (15 categories, version)
    python -m evals.langsmith.run --sync --experiment   # LangSmith Cloud: needs LANGSMITH_API_KEY, dev/eval/ci only

Restrictions (unchanged from Phase G): the runner only ever uses a fresh local PostgreSQL database
(evals/phase_g/local_env.assert_local refuses anything else, including the RDS Data API); it never reads the pilot
database, never calls a paid model (the model is scripted), and LangSmith sync refuses SETUHAUL_ENV=pilot/staging.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.langsmith import dataset as ds  # noqa: E402
from evals.langsmith.evaluators import (ARRIVAL_INTENTS, EVALUATOR_VERSION, rules_case_checks,  # noqa: E402
                                        score)
from setuhaul.agent import rules_extractor  # noqa: E402

RESULTS = ROOT / "evals" / "results"
BASELINE = RESULTS / "phase_g_eval_20261123_1024.json"     # Phase G final validation: 65 pass / 2 fail (I14, I15)


def guard_local_only(env: dict) -> None:
    """The evaluation never touches the pilot: no RDS Data API DSN, no pilot/staging environment."""
    if (env.get("SETUHAUL_DSN") or "").startswith("rds-data") or (env.get("SETUHAUL_TEST_DSN") or "").startswith("rds-data"):
        raise SystemExit("REFUSED: the evaluation runs on a local database only (SETUHAUL_DSN=rds-data is the pilot)")
    if (env.get("SETUHAUL_ENV") or "").lower() in ("pilot", "staging"):
        raise SystemExit("REFUSED: the evaluation runner does not run with SETUHAUL_ENV=pilot/staging")


def run_rules_cases(examples: list[dict]) -> dict[str, dict]:
    out = {}
    for e in examples:
        if e["inputs"].get("kind") != "rules_case":
            continue
        got = rules_extractor.extract(e["inputs"]["say"]).model_dump()
        checks = rules_case_checks(e["outputs"]["expected"], got)
        out[e["id"]] = {"result": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
                        "got": {k: got[k] for k in ("intent", "declared_eta_local", "declared_delay_min",
                                                    "earliest_start_local", "latest_finish_local", "latest_finish_kind",
                                                    "exception_type", "language")}}
    return out


def run_phase_g() -> dict[str, dict]:
    """The unchanged Phase G runner (all 67 scenarios), then its report's rows."""
    from evals.phase_g import run_eval
    before = set(RESULTS.glob("phase_g_eval_*.json"))
    run_eval.main([])
    new = sorted(set(RESULTS.glob("phase_g_eval_*.json")) - before) or sorted(RESULTS.glob("phase_g_eval_*.json"))
    rep = json.loads(new[-1].read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in rep["rows"]}
    for r in rows.values():                                  # arrival is judged from the reader's intent
        if r["id"] in ("I03", "I15"):
            got = rules_extractor.extract(next(e for e in ds._rows(ds.PHASE_G) if e["id"] == r["id"])["input"]["say"])
            r["checks"]["arrival"] = got.intent in ARRIVAL_INTENTS
    return rows | {"__report__": {"path": str(new[-1].relative_to(ROOT)), "summary": rep["summary"]}}


def run_g1_conversations(examples: list[dict]) -> dict[str, dict]:
    """The G.1 scenarios, through the same executors (fresh local database, scripted model, $0)."""
    from evals.phase_g import run_eval
    rows = [e for e in examples if e["metadata"]["source"].endswith("g1_conversations.jsonl")]
    if not rows:
        return {}
    env, out = run_eval.Env(), {}
    for e in rows:
        row = {"id": e["id"], "input": e["inputs"], "expected": e["outputs"]["expected"]}
        try:
            r = run_eval.EXECUTORS[e["inputs"]["kind"]](env, row)
            out[e["id"]] = {"result": "PASS" if not r["fail"] else "FAIL", "checks": r["checks"],
                            "failure_reason": "; ".join(r["fail"]) or None}
        except Exception as x:                                    # noqa: BLE001 — a crash is a failed row
            out[e["id"]] = {"result": "FAIL", "checks": {}, "failure_reason": f"{type(x).__name__}: {x}"[:300]}
        print(f"{out[e['id']]['result']:4} {e['id']:8} {e['category']:30} {out[e['id']]['failure_reason'] or ''}"[:200])
    return out


def evaluate(examples: list[dict], results: dict[str, dict]) -> dict:
    per_example, by_cat, by_eval = [], defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0, 0])
    for e in examples:
        res = results.get(e["id"])
        if res is None:
            continue
        scores = score(e, res)
        ok = res["result"] == "PASS"
        by_cat[e["category"]][0 if ok else 1] += 1
        for k, s in scores.items():
            by_eval[k][0 if s["score"] == 1 else 1 if s["score"] == 0 else 2] += 1
        per_example.append({"id": e["id"], "category": e["category"], "result": res["result"],
                            "failure_reason": res.get("failure_reason"), "scores": scores,
                            **({"got": res["got"]} if "got" in res else {})})
    total = len(per_example)
    passed = sum(x["result"] == "PASS" for x in per_example)
    return {"totals": {"total": total, "passed": passed, "failed": total - passed,
                       "pass_pct": round(100 * passed / total, 1) if total else None},
            "by_category": {c: {"passed": v[0], "failed": v[1]} for c, v in sorted(by_cat.items())},
            "by_evaluator": {k: {"pass": v[0], "fail": v[1], "n_a": v[2]} for k, v in sorted(by_eval.items())},
            "failures": [{"id": x["id"], "category": x["category"], "reason": x["failure_reason"],
                          "failed_evaluators": [k for k, s in x["scores"].items() if s["score"] == 0]}
                         for x in per_example if x["result"] != "PASS"],
            "examples": per_example}


def rules_metrics(examples: list[dict], results: dict[str, dict]) -> dict:
    """G4 measures: intent, deadline and arrival accuracy, and the false-positive delay rate."""
    rc = [(e, results[e["id"]]) for e in examples if e["inputs"].get("kind") == "rules_case" and e["id"] in results]

    def acc(pred):
        sel = [r for e, r in rc if pred(e, r)]
        ok = [r for r in sel if r["result"] == "PASS"]
        return {"n": len(sel), "correct": len(ok), "accuracy_pct": round(100 * len(ok) / len(sel), 1) if sel else None}
    not_delay = [(e, r) for e, r in rc if "REPORT_DELAY" not in e["outputs"]["expected"].get("intent", [])]
    fp = [x for x in not_delay if x[1]["got"]["intent"] == "REPORT_DELAY"]
    return {"intent": acc(lambda e, r: "intent" in r["checks"] or "intent_not" in r["checks"]),
            "deadline": acc(lambda e, r: e["category"] == "deadline" or "latest_finish_local" in e["outputs"]["expected"]),
            "arrival": acc(lambda e, r: "arrival" in r["checks"]),
            "false_positive_delay": {"n": len(not_delay), "false_positives": len(fp),
                                     "rate_pct": round(100 * len(fp) / len(not_delay), 1) if not_delay else None,
                                     "ids": [e["id"] for e, _ in fp]}}


def compare(before_path: Path, after: dict) -> dict:
    b = json.loads(Path(before_path).read_text(encoding="utf-8"))
    brows = {r["id"]: r["result"] for r in b["rows"]}
    arows = {x["id"]: x["result"] for x in after["examples"] if x["id"] in brows}
    return {"before": str(before_path), "before_totals": b["summary"]["totals"],
            "after_totals_same_rows": {"total": len(arows), "passed": sum(v == "PASS" for v in arows.values())},
            "newly_passing": sorted(k for k in arows if brows[k] != "PASS" and arows[k] == "PASS"),
            "newly_failing": sorted(k for k in arows if brows[k] == "PASS" and arows[k] != "PASS"),
            "missing": sorted(set(brows) - set(arows))}


def sync_to_langsmith(examples, manifest, report, experiment: bool, http=None) -> dict:
    """Push the dataset (once per version) and, optionally, the experiment (runs + evaluator feedback)."""
    from setuhaul.observability.langsmith import LangSmithClient, endpoint_from_env, load_langsmith_key
    env = dict(os.environ)
    environment = (env.get("SETUHAUL_ENV") or "dev").lower()
    if environment not in ("dev", "eval", "ci"):
        raise SystemExit("REFUSED: LangSmith sync runs from dev/eval/ci only (separate projects, never the pilot's)")
    client = LangSmithClient(load_langsmith_key(env, environment), endpoint_from_env(env), http=http)
    name = f"{manifest['dataset_name']}:{manifest['dataset_version']}"
    found = client.find_dataset(name)
    out = {"dataset_name": name, "dataset_version": manifest["dataset_version"], "evaluator_version": EVALUATOR_VERSION}
    if found:
        out["dataset_id"], out["dataset_created"] = found["id"], False
    else:
        d = client.create_dataset(name, "SetuHaul G.1 agent evaluation (synthetic data only)",
                                  {k: manifest[k] for k in ("dataset_version", "by_category", "examples")})
        out["dataset_id"], out["dataset_created"] = d["id"], True
        client.create_examples(d["id"], ds.langsmith_examples(examples, manifest["dataset_version"]))
    if experiment:
        exp_name = f"setuhaul-g1-{manifest['dataset_version']}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
        sess = client.create_experiment(exp_name, out["dataset_id"], {"evaluator_version": EVALUATOR_VERSION,
                                                                       "mode": "scripted", "cost_usd": 0})
        now = datetime.now(timezone.utc).isoformat()
        posts, feedback = [], []
        for x in report["examples"]:
            rid = str(uuid.uuid4())
            posts.append({"id": rid, "trace_id": rid, "name": f"eval:{x['id']}", "run_type": "chain",
                          "start_time": now, "end_time": now, "session_name": exp_name,
                          "inputs": {"example_id": x["id"]}, "outputs": {"result": x["result"]},
                          "extra": {"metadata": {"category": x["category"], "dataset_version": manifest["dataset_version"]}},
                          "dotted_order": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}{rid}"})
            feedback += [(rid, k, s["score"], s["comment"]) for k, s in x["scores"].items() if s["score"] is not None]
        client.post_runs(posts)
        for rid, k, s, c in feedback:
            client.create_feedback(rid, k, s, c)
        out.update(experiment_name=exp_name, experiment_id=(sess or {}).get("id"), runs=len(posts), feedback=len(feedback))
    return out


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-phase-g", action="store_true", help="only the rules-reader regression cases")
    ap.add_argument("--compare", default=str(BASELINE), help="before report (Phase G runner JSON)")
    ap.add_argument("--manifest", action="store_true")
    ap.add_argument("--sync", action="store_true", help="push the dataset to LangSmith (dev/eval/ci, needs a key)")
    ap.add_argument("--experiment", action="store_true", help="also upload the experiment (runs + feedback)")
    a = ap.parse_args(argv)
    guard_local_only(dict(os.environ))
    examples, manifest = ds.build()
    if a.manifest:
        print(json.dumps(manifest, indent=1))
        return 0
    started = datetime.now(timezone.utc)
    results = run_rules_cases(examples)
    report_path = None
    if not a.skip_phase_g:
        pg = run_phase_g()
        report_path = pg.pop("__report__")
        results.update(pg)
        results.update(run_g1_conversations(examples))
    report = evaluate(examples, results)
    report.update(manifest=manifest, evaluator_version=EVALUATOR_VERSION, mode="scripted", cost_usd=0.0,
                  started_at=started.isoformat(), finished_at=datetime.now(timezone.utc).isoformat(),
                  phase_g_report=report_path, rules_reader=rules_metrics(examples, results),
                  llm_judge="not run: subjective quality only, never authoritative (evals/langsmith/llm_judge.py)")
    if not a.skip_phase_g and a.compare:
        report["comparison"] = compare(Path(a.compare), report)
    if a.sync or a.experiment:
        report["langsmith"] = sync_to_langsmith(examples, manifest, report, a.experiment)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"g1_langsmith_eval_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    t = report["totals"]
    print(f"examples {t['total']}: {t['passed']} passed, {t['failed']} failed ({t['pass_pct']}%) "
          f"dataset {manifest['dataset_version']} evaluators {EVALUATOR_VERSION} -> {out.relative_to(ROOT)}")
    print(json.dumps(report["rules_reader"]))
    if "comparison" in report:
        c = report["comparison"]
        print(f"vs baseline: newly passing {c['newly_passing']}, newly failing {c['newly_failing']}")
    for f in report["failures"]:
        print(f"FAIL {f['id']} [{f['category']}] {f['failed_evaluators']} {f['reason'] or ''}"[:220])
    return 0 if t["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
