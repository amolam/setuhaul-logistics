"""The offline LLM judge recorded as a LangSmith dataset experiment (existing G.1/G.2 LangSmith client, no AWS runtime).

No network: OpenRouter is an httpx MockTransport, LangSmith is the in-memory FakeLangSmith from the G.2 tests, and the
ledger is the private local database of test_llm_judge (setuhaul_build_judge).
"""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from evals.langsmith import judge_langsmith as JL
from evals.langsmith import judge_run as JR
from evals.langsmith.judge_reference_compare import load_reference
from setuhaul.agent.openrouter import OpenRouterModel
from setuhaul.observability.langsmith import LangSmithClient
from test_llm_judge import ON, RECS, VERSION, _answer, _db, _today, good_args, ledger_dsn  # noqa: F401
from test_phase_g2 import LS_KEY, FakeLangSmith

REF = load_reference()
ACCEPTED = {i: tuple(s[d] for d in ("clarity", "tone", "language_match")) for i, s in REF["scores"].items()}


def env_for(ledger_dsn):
    return dict(ON, SETUHAUL_LLM_JUDGE_BUDGET_USD="0.35", SETUHAUL_LLM_DAILY_BUDGET_USD="3",
                OPENROUTER_API_KEY="sk-or-v1-test", SETUHAUL_JUDGE_LEDGER_DSN=ledger_dsn)


def scripted_openrouter(scores=None):
    """Answers each record with a quote from its own reply and the given scores (default: the AI reference; 4 for
    records without one). Counts requests."""
    sent = []

    def handler(req):
        sent.append(1)
        user = json.loads(req.content)["messages"][1]["content"]
        reply = user.split("<assistant_reply>\n")[1].split("\n</assistant_reply>")[0]
        rid = next(r["id"] for r in RECS if r["situation"].replace("<", "‹").replace(">", "›") in user)
        c, t, lm = (scores or ACCEPTED).get(rid, (4, 4, 4))
        a = good_args(reply)
        for d, v in zip(("clarity", "tone", "language_match"), (c, t, lm)):
            a[d]["score"] = v
        return _answer(a)
    return OpenRouterModel("sk-or-v1-test", client=httpx.Client(transport=httpx.MockTransport(handler))), sent


def ls_pair(fake):
    cfg = {"ui": "https://smith.langchain.com", "ready": True}
    return cfg, LangSmithClient(LS_KEY, http=fake.http())


def _room(ledger_dsn):
    s, r = _today(ledger_dsn)
    if Decimal(s) + Decimal(r) > Decimal("2"):
        pytest.skip("today's test ledger is already near the 3 USD cap")


def test_the_paid_run_is_recorded_as_a_langsmith_dataset_experiment(ledger_dsn, monkeypatch, tmp_path):
    _room(ledger_dsn)
    monkeypatch.setattr(JR, "RESULTS", tmp_path)
    fake = FakeLangSmith()
    client, sent = scripted_openrouter()
    res = JR.run_with_langsmith(RECS, VERSION, env_for(ledger_dsn), VERSION, client=client, db=_db(ledger_dsn),
                                ls=ls_pair(fake))
    assert len(sent) == 30 and res["judged"] == 30 and "error" not in res["langsmith"]
    ds = fake.datasets[0]
    assert ds["name"] == f"setuhaul-llm-judge-{VERSION}" and ds["metadata"]["dataset_version"] == VERSION
    assert len(fake.examples) == 30 and {e["metadata"]["example_id"] for e in fake.examples} == {r["id"] for r in RECS}
    j16 = next(e for e in fake.examples if e["metadata"]["example_id"] == "J16")
    assert j16["outputs"]["ai_reference_scores"] == REF["scores"]["J16"]
    assert j16["outputs"]["reference_note"] == "AI reference (not human calibration)"
    sess = fake.sessions[0]
    meta = sess["extra"]["metadata"]
    assert sess["reference_dataset_id"] == ds["id"] and meta["model"] == "anthropic/claude-haiku-4.5"
    assert meta["dataset_version"] == VERSION and meta["prompt_version"] == "judge-p2"
    assert meta["independent_human_calibration"] is False and meta["advisory_only"] is True
    assert meta["statuses"]["independent_human_calibration"].startswith("OUTSTANDING")
    runs = list(fake.runs.values())
    example_ids = {e["id"] for e in fake.examples}
    assert len(runs) == 30 and all(r["reference_example_id"] in example_ids for r in runs)
    assert all(r["session_name"] == sess["name"] for r in runs)
    one = next(r for r in runs if r["inputs"]["example_id"] == "J01")
    assert set(one["outputs"]["scores"]) == {"clarity", "tone", "language_match"}
    assert one["outputs"]["evidence"]["clarity"] in BY_REPLY["J01"]
    assert {"input_tokens", "output_tokens", "cost_usd", "latency_ms", "served_model"} <= set(one["extra"]["metadata"])
    assert len(fake.feedback) == 90 and {f["key"] for f in fake.feedback} == {
        "quality:clarity", "quality:tone", "quality:language_match"}
    assert all('quote: "' in f["comment"] for f in fake.feedback)
    blob = json.dumps([fake.datasets, fake.examples, fake.sessions, runs, fake.feedback])
    assert "sk-or-v1" not in blob and LS_KEY not in blob                      # keys only ever in headers
    assert res["acceptance"]["accepted"] is True and res["acceptance"]["rule1_bad_replies_caught"] == "5/5"
    saved = json.loads(open(res["path"], encoding="utf-8").read())
    assert saved["langsmith"]["experiment"]["runs"] == 30 and saved["acceptance"]["accepted"] is True


BY_REPLY = {r["id"]: r["reply"] for r in RECS}


def test_langsmith_down_before_the_run_means_no_paid_call(ledger_dsn, monkeypatch, tmp_path):
    _room(ledger_dsn)
    monkeypatch.setattr(JR, "RESULTS", tmp_path)
    client, sent = scripted_openrouter()
    with pytest.raises(Exception):
        JR.run_with_langsmith(RECS, VERSION, env_for(ledger_dsn), VERSION, client=client, db=_db(ledger_dsn),
                              ls=ls_pair(FakeLangSmith(down=True)))
    assert sent == []


def test_dataset_drift_refuses_the_run_before_any_paid_call(ledger_dsn, monkeypatch, tmp_path):
    _room(ledger_dsn)
    monkeypatch.setattr(JR, "RESULTS", tmp_path)
    fake = FakeLangSmith()
    cfg, lsc = ls_pair(fake)
    exs = JL.examples(RECS, VERSION, REF)
    exs[0]["metadata"]["content_sha"] = "tampered"
    JL.sync_dataset(lsc, exs, VERSION)                                       # LangSmith holds a different J01
    client, sent = scripted_openrouter()
    with pytest.raises(SystemExit):
        JR.run_with_langsmith(RECS, VERSION, env_for(ledger_dsn), VERSION, client=client, db=_db(ledger_dsn),
                              ls=(cfg, lsc))
    assert sent == []


def test_an_upload_failure_after_the_run_never_repeats_a_paid_call(ledger_dsn, monkeypatch, tmp_path):
    _room(ledger_dsn)
    monkeypatch.setattr(JR, "RESULTS", tmp_path)
    fake = FakeLangSmith()
    real = JL.upload_experiment

    def boom(*a, **k):
        raise httpx.ConnectError("langsmith went away")
    monkeypatch.setattr(JL, "upload_experiment", boom)
    client, sent = scripted_openrouter()
    res = JR.run_with_langsmith(RECS[:3], VERSION, env_for(ledger_dsn), VERSION, client=client, db=_db(ledger_dsn),
                                ls=ls_pair(fake))
    assert len(sent) == 3 and "--langsmith-upload" in res["langsmith"]["error"]
    monkeypatch.setattr(JL, "upload_experiment", real)
    cfg, lsc = ls_pair(fake)
    up = JR.upload(json.loads(open(res["path"], encoding="utf-8").read()), RECS, VERSION, cfg, lsc)
    assert up["experiment"]["runs"] == 3 and len(sent) == 3                   # upload only: no model call


def test_acceptance_applies_both_approved_rules_and_keeps_statuses_apart():
    from evals.langsmith.judge_reference_compare import full_report

    def fab(scores):
        return [{"example_id": i, "status": "SCORED",
                 "dimensions": {d: {"score": v} for d, v in zip(("clarity", "tone", "language_match"), s)}}
                for i, s in scores.items()]
    good = JR.acceptance(full_report(fab(ACCEPTED), REF))
    assert good["accepted"] and good["rule1_passed"] and good["rule2_beats_always_5"]
    assert good["human_calibration"].startswith("OUTSTANDING")
    lazy = JR.acceptance(full_report(fab({i: (5, 5, 5) for i in ACCEPTED}), REF))
    assert not lazy["accepted"] and lazy["rule1_bad_replies_caught"] == "0/5" and lazy["agreement_within_1"] > 0.8
    four = dict(ACCEPTED, J20=(4, 5, 5))
    almost = JR.acceptance(full_report(fab(four), REF))
    assert not almost["accepted"] and not almost["rule1_passed"] and almost["rule2_beats_always_5"]
    assert JR.acceptance(None) == {"accepted": False, "why": "no comparison"}


def test_only_offline_experiment_routes_are_used_never_an_online_evaluator():
    import ast
    import inspect
    src = inspect.getsource(JL)
    calls = {n.func.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "client"}
    assert calls == {"find_dataset", "create_dataset", "list_examples", "create_examples", "create_experiment",
                     "post_runs", "create_feedback"}


def test_the_langsmith_config_follows_the_g2_rules():
    assert JL.ls_config({"SETUHAUL_ENV": "eval"})["ready"] is False                         # no key
    assert JL.ls_config({"SETUHAUL_ENV": "pilot", "LANGSMITH_API_KEY": LS_KEY})["ready"] is False
    ok = JL.ls_config({"SETUHAUL_ENV": "eval", "LANGSMITH_API_KEY": LS_KEY})
    assert ok["ready"] is True and LS_KEY not in json.dumps(ok)
