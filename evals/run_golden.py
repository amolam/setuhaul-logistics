"""Run the golden set's extraction labels through an extractor and report the Phase 2 gate metrics.

    python evals/run_golden.py --extractor rules             # no AI baseline, free, runs anywhere
    python evals/run_golden.py --extractor model             # Haiku 4.5 via the provider switch (costs cents):
        SETUHAUL_ENV is always required. AWS (default): India profile only. OpenRouter: SETUHAUL_ENV=eval,
        SETUHAUL_DATA_CLASS=synthetic, SETUHAUL_MODEL_PROVIDER=openrouter, OPENROUTER_API_KEY (decision T38)

Gates (06_review_response.md): structured-output validity >= 99 %, ETA precision/recall >= 95 %, intent accuracy
>= 95 %. This runner covers the extraction layer only. Outcomes, operations, end state, grounding and the rubric are
labelled in the dataset for the end-to-end runner (needs the seed fixture loader; see README).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.golden import grade, load, option_ids, user_block  # noqa: E402
from setuhaul.agent import rules_extractor                     # noqa: E402
from setuhaul.agent.extraction import SCHEMA_VERSION, Extraction  # noqa: E402
from setuhaul.agent.prompts import PROMPT_VERSION              # noqa: E402

GATES = {"schema_valid": 0.99, "eta_precision": 0.95, "eta_recall": 0.95, "intent_accuracy": 0.95}


def picked(s, only: list[str] | None) -> bool:
    return not only or any(s.id == x or s.id.startswith(x + "#") for x in only)


def run(extractor: str, version: str = "v4", limit: int | None = None, only: list[str] | None = None,
        budget=None, cache=None, estimate_only: bool = False) -> dict:
    """Paid runs go through evals/budget.py: estimate first, cap, per-call reserve, cache (decision T42)."""
    scenarios, ds = load(version)
    scenarios = [s for s in scenarios if picked(s, only)]
    model, est, stopped = None, None, None
    if extractor == "model":
        from evals.budget import Budget, BudgetExceeded, Cache, GuardedModel, print_estimate
        from evals.providers import model_from_env
        budget, cache = budget or Budget(), cache or Cache()
        raw_model = model_from_env()
        todo = [(s, t) for s in scenarios for t in s.turns if t.x is not None and t.duplicate_of is None][:limit]
        cached = sum(cache.has(raw_model.extract_model, "extract", user_block(s, t)) for s, t in todo)
        est = print_estimate("run_golden " + version, raw_model.extract_model, len(todo) - cached, cached, "extract", budget)
        if estimate_only:
            return {"estimate_only": True, "estimated_usd": est}
        try:
            budget.authorise(est)
        except BudgetExceeded as e:
            raise SystemExit(f"refused: {e}")
        model = GuardedModel(raw_model, budget, cache)
    rows, fields = [], defaultdict(Counter)
    eta = Counter()
    valid = Counter()
    tokens = Counter()
    for s in scenarios:
        if stopped:
            break
        for i, t in enumerate(s.turns):
            if t.x is None or t.duplicate_of is not None:
                continue
            if limit and len(rows) >= limit:
                break
            row = {"scenario": s.id, "turn": i, "say": t.say}
            if model is None:
                ex = rules_extractor.extract(t.say, option_ids(t), awaiting_confirmation=t.ctx.awaiting is not None)
                valid["ok"] += 1
            else:
                from setuhaul.agent.models import ModelError
                try:
                    raw, tr = model.extract(user_block(s, t))
                    # reported usage only: a count the provider did not report stays out of the totals (G.1)
                    tokens.update({k: v for k, v in (("input", tr.input_tokens), ("output", tr.output_tokens),
                                                     ("cache_read", tr.cache_read_tokens)) if v is not None})
                    row.update(latency_ms=tr.latency_ms, served_by=tr.region, raw=raw)
                except BudgetExceeded as e:                      # stop the whole run: nothing more is sent
                    stopped = str(e)
                    break
                except ModelError as e:
                    if not rows:
                        raise SystemExit(f"Bedrock not reachable, nothing recorded: {e}")
                    row["error"] = str(e)
                    valid["error"] += 1
                    rows.append(row)
                    continue
                try:
                    ex = Extraction(**raw)
                    valid["ok"] += 1
                except Exception as e:                           # off-contract output counts against validity
                    row["error"] = f"schema: {e}"
                    valid["invalid"] += 1
                    rows.append(row)
                    continue
                if tr.outcome != "cached":
                    time.sleep(0.2)
            bad, got = grade(ex, s, t)
            row["mismatches"] = {k: list(v) for k, v in bad.items()}
            for k in t.x:
                fields[k]["right" if k not in bad else "wrong"] += 1
            if "eta" in t.x:                                    # precision / recall on resolved ETAs
                want, have = t.x["eta"], got["eta"]
                if have is not None and have == want:
                    eta["tp"] += 1
                else:
                    if have is not None:
                        eta["fp"] += 1
                    if want is not None:
                        eta["fn"] += 1
            if "safety" in bad and "safety" not in t.x:
                fields["safety(unlabelled)"]["wrong"] += 1
            rows.append(row)

    rate = lambda c: round(c["right"] / (c["right"] + c["wrong"]), 3) if c["right"] + c["wrong"] else None
    n_valid = sum(valid.values())
    metrics = {
        "turns": len(rows),
        "turns_all_fields_right": sum(1 for r in rows if not r.get("error") and not r.get("mismatches")),
        "schema_valid": round(valid["ok"] / n_valid, 3) if n_valid else None,
        "eta_precision": round(eta["tp"] / (eta["tp"] + eta["fp"]), 3) if eta["tp"] + eta["fp"] else None,
        "eta_recall": round(eta["tp"] / (eta["tp"] + eta["fn"]), 3) if eta["tp"] + eta["fn"] else None,
        "intent_accuracy": rate(fields["intent"]),
        "per_field": {k: rate(v) for k, v in sorted(fields.items())},
    }
    metrics["gates"] = {k: ("PASS" if metrics[k] is not None and metrics[k] >= v else "FAIL") for k, v in GATES.items()}
    return {"run": {"date": datetime.now().isoformat(timespec="seconds"), "dataset": ds, "extractor": extractor,
                    "schema_version": SCHEMA_VERSION, "prompt_version": PROMPT_VERSION if model else None,
                    "provider": type(model).__name__ if model else None,
                    "served_by": sorted({r["served_by"] for r in rows if r.get("served_by")}),
                    "model_id": model.extract_model if model else None, "tokens": dict(tokens),
                    "cost": model.budget.report(est) if model else None, "stopped": stopped,
                    "only": only,
                    "latency_ms_p50": sorted(r["latency_ms"] for r in rows if r.get("latency_ms"))[len([r for r in rows if r.get("latency_ms")]) // 2]
                    if any(r.get("latency_ms") for r in rows) else None},
            "metrics": metrics, "turns": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", choices=["rules", "model"], default="rules")
    ap.add_argument("--version", default="v4")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="*", help="scenario ids (a base id also selects its h1 variants)")
    from evals.budget import add_args, from_args
    add_args(ap)
    a = ap.parse_args()
    budget, cache = from_args(a)
    res = run(a.extractor, a.version, a.limit, a.only, budget, cache, a.estimate_only)
    if res.get("estimate_only"):
        return
    out = Path(__file__).parent / "results" / f"golden_{a.version}_{a.extractor}_{datetime.now():%Y%m%d_%H%M}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    m = res["metrics"]
    print(json.dumps({"run": res["run"], **{k: v for k, v in m.items()}}, indent=2, ensure_ascii=False))
    for r in res["turns"]:
        if r.get("error") or r.get("mismatches"):
            print(f"  {r['scenario']}#{r['turn']}: {r.get('error') or r['mismatches']}")
    print("wrote", out)


if __name__ == "__main__":
    main()
