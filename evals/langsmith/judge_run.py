"""Offline LLM judge runner (Part A). DRY RUN BY DEFAULT: no key is read, no client is built, no call is made.

    python -m evals.langsmith.judge_run                        # dry run: the report the owner approves (or not)
    python -m evals.langsmith.judge_run --init-ledger          # create + migrate the LOCAL ledger database (no call)
    python -m evals.langsmith.judge_run --paid --approve <dataset_version>    # the paid run, owner-started only

The paid run refuses to start unless ALL of these hold (each is checked before any key is read):
  * --paid and --approve naming the exact dataset version the owner reviewed in the dry run;
  * SETUHAUL_LLM_JUDGE=on, SETUHAUL_ENV=dev|eval, SETUHAUL_DATA_CLASS=synthetic,
    0 < SETUHAUL_LLM_JUDGE_BUDGET_USD <= 1 (the run cap), 0 < SETUHAUL_LLM_DAILY_BUDGET_USD <= 3 (the ledger's daily
    cap, shared with every agent call on the same ledger database);
  * the ledger database is local (localhost), migrated through 0042 (purpose 'judge');
  * the dry-run UPPER-BOUND estimate (every record retried once, at the ledger's reservation price) fits the run cap;
  * OPENROUTER_API_KEY is set in the owner's own shell (never printed; only its shape is logged).
Every call then goes through the existing metered OpenRouter client (pinned ZDR provider, data_collection deny,
temperature 0) and the shared ledger, behind the per-run cap (llm_judge.RunBudget). A budget refusal stops the run.

Output: evals/results/judge_<UTC timestamp>.json with the scores, rationales, evidence quotes, model, provider, the
served model version, prompt and dataset versions, token usage, estimated and actual cost, ledger before/after and
timestamps. The judge's scores are advisory: nothing reads them to pass, fail, gate or change anything.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.judge.build_dataset import DATASET, LANGS, SOURCES, version_of  # noqa: E402
from evals.langsmith import llm_judge as J  # noqa: E402

RESULTS = ROOT / "evals" / "results"
LEDGER_DSN_DEFAULT = "host=localhost port=55432 user=postgres dbname=setuhaul_judge_ledger"
EXPECTED_OUTPUT_TOKENS = 260           # three short rationales and quotes, as a forced tool call
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
PAID_RUN_ATTEMPTS = 1                  # owner decision (25 Nov 2026): no automatic retry may incur a second charge


class DatasetError(ValueError):
    pass


# ---------------------------------------------------------------- dataset
def load_dataset(path: Path = DATASET) -> tuple[list[dict], str]:
    """Records and the dataset version, after validating every record (synthetic, complete, no personal data)."""
    from setuhaul.observability.redaction import find_leaks
    raw = path.read_bytes()
    recs, seen = [], set()
    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            raise DatasetError(f"line {n}: not JSON") from None
        need = {"id", "source", "template", "language", "conversation", "situation", "reply", "calibration",
                "contrast_note"}
        if not isinstance(r, dict) or set(r) != need:
            raise DatasetError(f"line {n}: fields must be exactly {sorted(need)}")
        if not isinstance(r["id"], str) or not r["id"] or r["id"] in seen:
            raise DatasetError(f"line {n}: missing or duplicate id")
        seen.add(r["id"])
        if r["language"] not in LANGS or r["source"] not in SOURCES or not isinstance(r["calibration"], bool):
            raise DatasetError(f"{r['id']}: bad language, source or calibration flag")
        conv = r["conversation"]
        if (not isinstance(conv, list) or not 1 <= len(conv) <= 4
                or not all(isinstance(m, str) and 0 < len(m) <= 300 for m in conv)):
            raise DatasetError(f"{r['id']}: conversation must be 1-4 messages of 1-300 characters")
        for k, lim in (("reply", 1000), ("situation", 300)):
            if not isinstance(r[k], str) or not 0 < len(r[k]) <= lim:
                raise DatasetError(f"{r['id']}: {k} must be 1-{lim} characters")
        if find_leaks({"c": conv, "r": r["reply"], "s": r["situation"]}):
            raise DatasetError(f"{r['id']}: contains phone-, e-mail-, key- or account-shaped text (synthetic only)")
        recs.append(r)
    if not recs:
        raise DatasetError("empty dataset")
    return recs, version_of(raw)


# ---------------------------------------------------------------- estimate (no call)
def estimate(recs: list[dict]) -> dict:
    """Expected and upper-bound tokens and cost. Upper bound = the ledger's own reservation formula (the amount each
    call must fit before it is sent) for EVERY record, PAID_RUN_ATTEMPTS times (1: no retry)."""
    from setuhaul.agent.openrouter import HAIKU_OR, MIN_RESERVATION, PRICE
    p_in, p_out = PRICE[HAIKU_OR]
    exp_in = exp_out = 0
    upper = Decimal(0)
    for r in recs:
        system, user = J.build_prompt(r)
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "tools": [{"type": "function", "function": {"name": J.JUDGE_TOOL["name"],
                                                            "parameters": J.JUDGE_TOOL["input_schema"]}}],
                "max_tokens": J.MAX_TOKENS}
        chars = len(json.dumps(body, ensure_ascii=False))
        exp_in += math.ceil(chars / 3)                     # conservative: Devanagari costs more tokens per character
        exp_out += EXPECTED_OUTPUT_TOKENS
        reserve = max(MIN_RESERVATION, ((len(json.dumps(body)) // 3 + 1) * p_in + J.MAX_TOKENS * p_out) * Decimal("1.2"))
        upper += PAID_RUN_ATTEMPTS * reserve
    expected = exp_in * p_in + exp_out * p_out
    return {"calls_expected": len(recs), "calls_worst_case": PAID_RUN_ATTEMPTS * len(recs), "retries": "none",
            "input_tokens_expected": exp_in, "output_tokens_expected": exp_out,
            "cost_usd_expected": str(expected.quantize(Decimal("0.0001"))),
            "cost_usd_upper_bound": str(upper.quantize(Decimal("0.0001"))),
            "price_basis": f"{HAIKU_OR}: USD {p_in * 1_000_000}/M input, {p_out * 1_000_000}/M output "
                           "(setuhaul/agent/openrouter.py PRICE, OpenRouter list price 21 Nov 2026)"}


# ---------------------------------------------------------------- ledger
def assert_local(dsn: str) -> None:
    parts = dict(p.split("=", 1) for p in dsn.split() if "=" in p)
    if parts.get("host", "localhost") not in LOCAL_HOSTS:
        raise SystemExit("REFUSED: the judge ledger must be a local database (no shared environment)")


def ledger_today(dsn: str) -> dict:
    """Today's (UTC) spend and open reservations on the ledger, read only. Never raises."""
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as c:
            row = c.execute("""SELECT spent_usd, reserved_usd FROM setuhaul.model_budget_day
                                WHERE day = (now() AT TIME ZONE 'UTC')::date""").fetchone()
            judge_ok = c.execute("""SELECT pg_get_constraintdef(oid) LIKE '%judge%' FROM pg_constraint
                                     WHERE conname = 'model_usage_purpose_check'""").fetchone()
            by = dict(c.execute("""SELECT purpose, coalesce(sum(cost_usd), 0)::text FROM setuhaul.model_usage
                                    WHERE day = (now() AT TIME ZONE 'UTC')::date GROUP BY 1""").fetchall())
        return {"available": True, "spent_usd": str(row[0] if row else 0), "reserved_usd": str(row[1] if row else 0),
                "spent_by_purpose": by, "judge_purpose_migrated": bool(judge_ok and judge_ok[0])}
    except Exception as e:                                                       # noqa: BLE001
        return {"available": False, "reason": f"{type(e).__name__} (run --init-ledger to create the local ledger)"}


def init_ledger(dsn: str) -> None:
    import psycopg

    from setuhaul.db.migrate import migrate
    assert_local(dsn)
    parts = dict(p.split("=", 1) for p in dsn.split() if "=" in p)
    name = parts.get("dbname", "")
    if not name.startswith("setuhaul_judge"):
        raise SystemExit("REFUSED: the judge ledger database name must start with setuhaul_judge")
    base = " ".join(f"{k}={v}" for k, v in parts.items() if k != "dbname")
    with psycopg.connect(f"{base} dbname=postgres", autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            c.execute(f'CREATE DATABASE "{name}"')
    print("applied:", migrate(dsn) or "(nothing new)")


# ---------------------------------------------------------------- report
def config_from_env(env: dict) -> dict:
    def dec(k):
        try:
            v = Decimal((env.get(k) or "").strip())
            return v if v.is_finite() else None
        except Exception:
            return None
    return {"judge_flag": (env.get("SETUHAUL_LLM_JUDGE") or "off"), "environment": env.get("SETUHAUL_ENV") or "(unset)",
            "data_class": env.get("SETUHAUL_DATA_CLASS") or "(unset)",
            "run_cap_usd": dec("SETUHAUL_LLM_JUDGE_BUDGET_USD"), "daily_cap_usd": dec("SETUHAUL_LLM_DAILY_BUDGET_USD"),
            "ledger_dsn": env.get("SETUHAUL_JUDGE_LEDGER_DSN") or LEDGER_DSN_DEFAULT,
            "key_present": bool((env.get("OPENROUTER_API_KEY") or "").strip())}


def refusals(cfg: dict, est: dict, ledger: dict, approve: str | None, version: str) -> list[str]:
    """Every reason the paid run would be refused. Empty only when everything holds."""
    out = []
    env = {"SETUHAUL_LLM_JUDGE": cfg["judge_flag"], "SETUHAUL_ENV": cfg["environment"],
           "SETUHAUL_DATA_CLASS": cfg["data_class"], "SETUHAUL_LLM_JUDGE_BUDGET_USD": str(cfg["run_cap_usd"] or "")}
    if not J.judge_enabled(env):
        out.append("judge gate: needs SETUHAUL_LLM_JUDGE=on, SETUHAUL_ENV=dev|eval, SETUHAUL_DATA_CLASS=synthetic and "
                   f"0 < SETUHAUL_LLM_JUDGE_BUDGET_USD <= {J.JUDGE_RUN_CAP_MAX_USD}")
    dc = cfg["daily_cap_usd"]
    if dc is None or not Decimal(0) < dc <= J.JUDGE_DAILY_CAP_MAX_USD:
        out.append(f"daily cap: SETUHAUL_LLM_DAILY_BUDGET_USD must be > 0 and <= {J.JUDGE_DAILY_CAP_MAX_USD} "
                   "(the combined agent + judge cap on the ledger)")
    rc = cfg["run_cap_usd"] or Decimal(0)
    if Decimal(est["cost_usd_upper_bound"]) > min(rc, J.JUDGE_RUN_CAP_MAX_USD):
        out.append(f"estimate: the upper bound USD {est['cost_usd_upper_bound']} exceeds the run cap USD {rc}")
    if not ledger.get("available"):
        out.append(f"ledger: not available ({ledger.get('reason')})")
    elif not ledger.get("judge_purpose_migrated"):
        out.append("ledger: not migrated through 0042 (purpose 'judge')")
    elif dc is not None and Decimal(ledger["spent_usd"]) + Decimal(ledger["reserved_usd"]) >= dc:
        out.append("ledger: today's daily cap is already used up")
    if not cfg["key_present"]:
        out.append("key: OPENROUTER_API_KEY is not set in this shell (the owner sets it; it is never printed)")
    if approve != version:
        out.append(f"approval: --approve must name the reviewed dataset version ({version})")
    return out


def dry_run_report(recs, version, env, approve=None) -> dict:
    from setuhaul.agent.openrouter import DEFAULT_PROVIDER, HAIKU_OR, VERIFIED_ZDR_ROUTES
    cfg = config_from_env(env)
    est = estimate(recs)
    ledger = ledger_today(cfg["ledger_dsn"]) if cfg["ledger_dsn"] else {"available": False}
    by_lang = {lg: sum(1 for r in recs if r["language"] == lg) for lg in LANGS}
    return {
        "kind": "llm_judge_dry_run", "generated_at": datetime.now(timezone.utc).isoformat(), "paid_calls_made": 0,
        "dataset": {"path": DATASET.relative_to(ROOT).as_posix(), "version": version, "records": len(recs),
                    "calibration_records": sum(r["calibration"] for r in recs),
                    "contrast_records": sum(r["source"] == "contrast" for r in recs), "by_language": by_lang},
        "model": {"model": HAIKU_OR, "provider": DEFAULT_PROVIDER, "provider_name": VERIFIED_ZDR_ROUTES[DEFAULT_PROVIDER],
                  "routing": "pinned provider, no fallbacks, zdr=true, data_collection=deny", "temperature": 0,
                  "max_tokens": J.MAX_TOKENS, "model_version": "reported by OpenRouter per call (paid run only)"},
        "prompt_version": J.JUDGE_PROMPT_VERSION, "dimensions": list(J.QUALITY_KEYS),
        "estimate": est,
        "limits": {"run_cap_usd": str(cfg["run_cap_usd"]) if cfg["run_cap_usd"] is not None else "(unset)",
                   "run_cap_max_usd": str(J.JUDGE_RUN_CAP_MAX_USD),
                   "daily_cap_usd": str(cfg["daily_cap_usd"]) if cfg["daily_cap_usd"] is not None else "(unset)",
                   "daily_cap_max_usd": str(J.JUDGE_DAILY_CAP_MAX_USD)},
        "ledger_today": ledger,
        "paid_run_would_be_refused_because": refusals(cfg, est, ledger, approve, version),
        "business_effects": "none: the judge reads the frozen dataset only; no agent, database row other than the "
                            "ledger, booking, dispatch, release gate or deployment is touched",
    }


def print_report(rep: dict) -> None:
    d, m, e, lim, led = rep["dataset"], rep["model"], rep["estimate"], rep["limits"], rep["ledger_today"]
    print("LLM JUDGE - DRY RUN (no model call was made)")
    print(f"  dataset       {d['path']}  version {d['version']}")
    print(f"  records       {d['records']} ({d['calibration_records']} calibration, {d['contrast_records']} contrast)"
          f"  by language {d['by_language']}")
    print(f"  model         {m['model']} via {m['provider_name']} ({m['routing']}), temperature 0")
    print(f"  prompt        {rep['prompt_version']}   dimensions {', '.join(rep['dimensions'])}")
    print(f"  calls         {e['calls_expected']} expected, {e['calls_worst_case']} at most (no retries)")
    print(f"  tokens        ~{e['input_tokens_expected']} input, ~{e['output_tokens_expected']} output (expected)")
    print(f"  cost USD      ~{e['cost_usd_expected']} expected, {e['cost_usd_upper_bound']} upper bound")
    print(f"                {e['price_basis']}")
    print(f"  run cap       {lim['run_cap_usd']} (max {lim['run_cap_max_usd']})   daily cap {lim['daily_cap_usd']}"
          f" (max {lim['daily_cap_max_usd']}, shared with the agent on this ledger)")
    if led.get("available"):
        print(f"  ledger today  spent USD {led['spent_usd']}, reserved USD {led['reserved_usd']}, "
              f"by purpose {led['spent_by_purpose']}, judge purpose migrated: {led['judge_purpose_migrated']}")
    else:
        print(f"  ledger today  unavailable: {led.get('reason')}")
    ref = rep["paid_run_would_be_refused_because"]
    print("  paid run      " + ("READY (still needs the owner to start it)" if not ref else "would be REFUSED:"))
    for r in ref:
        print(f"                - {r}")


# ---------------------------------------------------------------- the paid run (owner-started only)
def paid_run(recs, version, env, approve, *, client=None, db=None, preflight=None) -> dict:
    """Refuses (SystemExit) unless every condition holds. `client`/`db` are injected by tests (fakes only).
    `preflight` (e.g. the LangSmith dataset sync) runs after every gate and BEFORE any paid call; if it raises, no
    paid call is made."""
    rep = dry_run_report(recs, version, env, approve)
    if rep["paid_run_would_be_refused_because"]:
        print_report(rep)
        raise SystemExit("REFUSED: the paid run does not start (see the reasons above)")
    if preflight is not None:
        preflight()
    cfg = config_from_env(env)
    assert_local(cfg["ledger_dsn"])
    from setuhaul.agent.metering import ModelBudget
    if db is None:
        from setuhaul.api.db import Db
        db = Db(cfg["ledger_dsn"])
    budget = J.RunBudget(ModelBudget(db, cfg["environment"], cfg["daily_cap_usd"]), cfg["run_cap_usd"])
    if client is None:
        from setuhaul.agent.openrouter import openrouter_from_env
        client = openrouter_from_env(env, cfg["environment"], db=db)     # reads the key from the owner's shell
    client.meter = budget                                                # every call: run cap, then the ledger
    client.max_attempts = PAID_RUN_ATTEMPTS                              # no transport/5xx retry either
    started = datetime.now(timezone.utc)
    before = ledger_today(cfg["ledger_dsn"])
    out, stopped = [], None
    for r in recs:
        try:
            out.append(J.judge(r, client, env, max_attempts=PAID_RUN_ATTEMPTS).to_dict() | {"language": r["language"], "source": r["source"],
                                                            "calibration": r["calibration"]})
        except Exception as e:                                           # noqa: BLE001 — stop, never retry past it
            stopped = f"{type(e).__name__}: {getattr(e, 'category', '')} {str(e)[:160]}".strip()
            break
    after = ledger_today(cfg["ledger_dsn"])
    res = {"kind": "llm_judge_run", "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
           "dataset_version": version, "records": len(recs), "judged": len(out), "stopped_early": stopped,
           "prompt_version": J.JUDGE_PROMPT_VERSION, "model": rep["model"]["model"],
           "provider": rep["model"]["provider"],
           "served_models": sorted({x["served_model"] for x in out if x.get("served_model")}),
           "estimate": rep["estimate"],
           "actual": {"charged_usd_by_ledger": str(budget.charged_usd), "calls_reserved": budget.calls_reserved,
                      "refused_by_run_cap": budget.refused_by_run_cap, "refused_by_daily_cap": budget.refused_by_daily_cap,
                      "input_tokens": sum(x["input_tokens"] or 0 for x in out),
                      "output_tokens": sum(x["output_tokens"] or 0 for x in out),
                      "requests": sum(x["attempts"] for x in out),
                      "latency_ms_total": sum(x["latency_ms"] or 0 for x in out),
                      "unparseable": sum(x["status"] == "UNPARSEABLE" for x in out)},
           "ledger_before": before, "ledger_after": after, "advisory_only": True, "judgements": out}
    res["comparison"] = compare_with_reference(out)
    res["acceptance"] = acceptance(res["comparison"])
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"judge_{started.strftime('%Y%m%d_%H%M%S')}.json"
    res["path"] = str(path)
    path.write_bytes(json.dumps(res, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
    return res


def compare_with_reference(judgements: list[dict]) -> dict | None:
    """The accepted AI reference comparison (ALWAYS_5 baseline, negative cases, separate statuses). Never raises."""
    try:
        from evals.langsmith.judge_reference_compare import full_report, load_reference
        return full_report(judgements, load_reference())
    except Exception as e:                                               # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def acceptance(comparison: dict | None) -> dict:
    """The two owner-approved rules (25 Nov 2026), reported apart from agreement and human calibration."""
    if not comparison or "judge" not in comparison:
        return {"accepted": False, "why": "no comparison"}
    j, b = comparison["judge"], comparison["baseline"]
    jn, bn = j["negative_cases"], b["negative_cases"]
    rule1 = jn["cases_identified"] == jn["cases_total"] == 5
    rule2 = (j["overall"]["mae"] is not None and j["overall"]["mae"] < b["overall"]["mae"]
             and jn["targets_identified"] > bn["targets_identified"])
    return {"rule1_bad_replies_caught": f"{jn['cases_identified']}/{jn['cases_total']}", "rule1_passed": rule1,
            "rule2_beats_always_5": rule2, "judge_mae": j["overall"]["mae"], "always_5_mae": b["overall"]["mae"],
            "accepted": rule1 and rule2, "agreement_within_1": j["overall"]["within_1"],
            "human_calibration": comparison["statuses"]["independent_human_calibration"]}


def _langsmith(env):
    """(cfg, client) from the owner's shell, or SystemExit naming what is missing. Never prints the key."""
    from evals.langsmith.judge_langsmith import ls_config
    from setuhaul.observability.langsmith import LangSmithClient
    cfg = ls_config(env)
    if not cfg["ready"]:
        raise SystemExit("REFUSED (LangSmith): " + "; ".join(cfg["problems"]))
    return cfg, LangSmithClient(env["LANGSMITH_API_KEY"].strip(), cfg["endpoint"], timeout_s=10.0,
                                workspace_id=cfg["workspace_id"])


def upload(res: dict, recs: list[dict], version: str, cfg, client, ds=None) -> dict:
    from evals.langsmith import judge_langsmith as JL
    from evals.langsmith.judge_reference_compare import load_reference
    if ds is None:
        ds = JL.sync_dataset(client, JL.examples(recs, version, load_reference()), version)
    exp = JL.upload_experiment(client, ds, res, res.get("comparison"))
    return {"dataset": {k: ds[k] for k in ("dataset_name", "dataset_id", "created", "examples_added",
                                            "content_drift", "missing")},
            "experiment": exp, "experiment_url": JL.experiment_url(cfg, ds, exp)}


def run_with_langsmith(recs, version, env, approve, *, client=None, db=None, ls=None) -> dict:
    """The paid run recorded as a LangSmith experiment. `ls` = (cfg, LangSmith client) in tests."""
    from evals.langsmith import judge_langsmith as JL
    from evals.langsmith.judge_reference_compare import load_reference
    cfg, lsc = ls or _langsmith(env)
    state: dict = {}

    def preflight():                                     # before any paid call: a LangSmith problem costs nothing
        ds = JL.sync_dataset(lsc, JL.examples(recs, version, load_reference()), version)
        if ds["content_drift"] or ds["missing"]:
            raise SystemExit(f"REFUSED (LangSmith dataset): drift {ds['content_drift']}, missing {ds['missing']}")
        state["ds"] = ds
    res = paid_run(recs, version, env, approve, client=client, db=db, preflight=preflight)
    try:
        res["langsmith"] = upload(res, recs, version, cfg, lsc, state["ds"])
    except Exception as e:                                               # noqa: BLE001 — never re-run the model
        res["langsmith"] = {"error": f"upload failed ({type(e).__name__}); retry with --langsmith-upload "
                                     f"{res['path']} (no model call)"}
    Path(res["path"]).write_bytes(json.dumps(res, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Offline LLM judge: dry run by default.")
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--json", type=Path, help="also write the dry-run report to this file")
    ap.add_argument("--init-ledger", action="store_true", help="create and migrate the local ledger database")
    ap.add_argument("--paid", action="store_true", help="the paid run (owner only; see the module doc)")
    ap.add_argument("--approve", help="the dataset version the owner approved from the dry run")
    ap.add_argument("--langsmith", action="store_true", help="record the paid run as a LangSmith experiment")
    ap.add_argument("--langsmith-upload", type=Path, help="upload an existing results file (no model call)")
    a = ap.parse_args(argv)
    env = dict(os.environ)
    if a.init_ledger:
        init_ledger(env.get("SETUHAUL_JUDGE_LEDGER_DSN") or LEDGER_DSN_DEFAULT)
        return 0
    recs, version = load_dataset(a.dataset)
    if a.langsmith_upload:                              # upload only: never a model call
        res = json.loads(a.langsmith_upload.read_text(encoding="utf-8"))
        if res.get("dataset_version") != version:
            raise SystemExit("REFUSED: the results file is for another dataset version")
        cfg, client = _langsmith(env)
        res["langsmith"] = upload(res, recs, version, cfg, client)
        a.langsmith_upload.write_bytes(json.dumps(res, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
        print(json.dumps(res["langsmith"], indent=1))
        return 0
    if not a.paid:
        rep = dry_run_report(recs, version, env, a.approve)
        print_report(rep)
        if a.json:
            a.json.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return 0
    res = run_with_langsmith(recs, version, env, a.approve) if a.langsmith else paid_run(recs, version, env, a.approve)
    act = res["actual"]
    print(f"judged {res['judged']}/{res['records']}, requests {act['calls_reserved']}, charged USD "
          f"{act['charged_usd_by_ledger']}, tokens in/out {act['input_tokens']}/{act['output_tokens']}, "
          f"latency ms {act['latency_ms_total']}, stopped: {res['stopped_early']}")
    print("acceptance: " + json.dumps(res["acceptance"]))
    print("langsmith: " + json.dumps(res.get("langsmith", "not requested"))[:600])
    print(f"results: {res['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
