"""Phase G.2: the 10-example LangSmith smoke (evals/langsmith/g2_smoke.py, g2_evaluators.py).

Focused checks only: configuration and a missing key, tracing off by default, trace creation with tracing on, fail-open
when LangSmith is down, dataset idempotency, the evaluators, and one end-to-end run (agent -> trace -> dataset ->
evaluators -> experiment) against an IN-MEMORY LangSmith (httpx MockTransport). No test makes a network call, reads a
real key, calls a model or touches AWS. The database is the local test cluster (evals/phase_g/local_env.py).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from evals.langsmith import g2_evaluators as ev
from evals.langsmith import g2_smoke as g2
from setuhaul.observability import tracing
from setuhaul.observability.langsmith import LangSmithClient, tracer_from_env
from setuhaul.observability.redaction import find_leaks

ROOT = Path(__file__).resolve().parents[1]
LS_KEY = "lsv2_pt_0123456789abcdef0123456789abcdef_0123456789"      # fake, key-shaped: must never appear anywhere
IDS = ["C01", "C03", "C04", "C06", "T04", "A10", "T06", "T05", "C08", "C12"]


class FakeLangSmith:
    """A stateful in-memory stand-in for the LangSmith REST routes the G.1 client uses."""

    def __init__(self, down: bool = False):
        self.down, self.datasets, self.examples, self.sessions, self.runs, self.feedback = down, [], [], [], {}, []
        self.requests = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("langsmith unavailable")
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, req.headers.get("x-api-key"), req.content or b""))
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        p = req.url.path
        if req.method == "GET" and p == "/datasets":
            return httpx.Response(200, json=[d for d in self.datasets if d["name"] == q.get("name")])
        if req.method == "POST" and p == "/datasets":
            d = dict(body, id=str(uuid.uuid4()), tenant_id="tenant-fake")
            self.datasets.append(d)
            return httpx.Response(200, json=d)
        if req.method == "GET" and p == "/examples":
            return httpx.Response(200, json=[e for e in self.examples if e["dataset_id"] == q.get("dataset")])
        if req.method == "POST" and p == "/examples/bulk":
            made = [dict(e, id=str(uuid.uuid4())) for e in body]
            self.examples += made
            return httpx.Response(200, json=made)
        if req.method == "POST" and p == "/sessions":
            s = dict(body, id=str(uuid.uuid4()), tenant_id="tenant-fake")
            self.sessions.append(s)
            return httpx.Response(200, json=s)
        if req.method == "POST" and p == "/runs/batch":
            for r in body["post"]:
                self.runs[r["id"]] = r
            return httpx.Response(202, json={})
        if req.method == "POST" and p == "/feedback":
            self.feedback.append(body)
            return httpx.Response(200, json={"id": str(uuid.uuid4())})
        if req.method == "GET" and p.startswith("/runs/"):
            rid = p.rsplit("/", 1)[1]
            if rid in self.runs:
                return httpx.Response(200, json={"id": rid, "app_path": f"/o/tenant-fake/projects/p/x/r/{rid}?trace_id={rid}"})
            return httpx.Response(404)
        return httpx.Response(404)

    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self))


READY = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": LS_KEY, "SETUHAUL_ENV": "eval"}


@pytest.fixture
def ready_env(monkeypatch, tmp_path):
    for k in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT", "SETUHAUL_ENV",
              "SETUHAUL_DSN"):
        monkeypatch.delenv(k, raising=False)
    # keep SETUHAUL_TEST_DSN: it names the local test cluster (CI: port 5432); the executors read it lazily
    for k, v in READY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(g2, "RESULTS", tmp_path)
    return tmp_path


# ---------------------------------------------------------------- configuration and missing key
def test_config_is_off_by_default_and_a_missing_key_is_reported_without_running(monkeypatch, tmp_path, capsys):
    for k in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT", "SETUHAUL_ENV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(g2, "RESULTS", tmp_path)
    c = g2.config({})
    assert not c["ready"] and not c["tracing"] and c["key"] == "missing" and c["project"] == "setuhaul-synthetic-pilot"
    assert g2.main(["--check-config"]) == 2
    assert g2.main([]) == 2                                   # the full run refuses before touching anything
    out = capsys.readouterr().out
    assert "NOT RUN" in out and "LANGSMITH_API_KEY is not set" in out and not list(tmp_path.iterdir())


def test_config_with_a_key_shows_only_its_shape_and_refuses_pilot_and_unknown_endpoints():
    c = g2.config(READY)
    assert c["ready"] and LS_KEY not in json.dumps(c) and c["key"].startswith("present")
    assert c["ui"] == "https://smith.langchain.com"
    eu = g2.config(dict(READY, LANGSMITH_ENDPOINT="https://eu.api.smith.langchain.com/api/v1"))
    assert eu["ready"] and eu["endpoint"] == "https://eu.api.smith.langchain.com" and eu["ui"] == "https://eu.smith.langchain.com"
    ap = g2.config(dict(READY, LANGSMITH_ENDPOINT="https://apac.api.smith.langchain.com"))
    assert ap["ready"] and ap["ui"] == "https://apac.smith.langchain.com"
    ws = g2.config(dict(READY, LANGSMITH_WORKSPACE_ID="0F0E0D0C-0000-4000-8000-000000000001"))
    assert ws["ready"] and ws["workspace_id"] == "0f0e0d0c-0000-4000-8000-000000000001" and "personal" in ws["key"]
    assert not g2.config(dict(READY, LANGSMITH_WORKSPACE_ID="not-a-uuid"))["ready"]
    assert not g2.config(dict(READY, SETUHAUL_ENV="pilot"))["ready"]
    assert not g2.config(dict(READY, LANGSMITH_ENDPOINT="https://evil.example.com"))["ready"]
    assert not g2.config(dict(READY, SETUHAUL_DSN="rds-data://cluster"))["ready"]


def test_nothing_in_the_application_imports_the_smoke_and_no_key_is_committed():
    for f in (ROOT / "setuhaul").rglob("*.py"):
        assert "g2_smoke" not in f.read_text(encoding="utf-8"), f
    for f in (ROOT / "evals" / "langsmith" / "g2_smoke.py", ROOT / "evals" / "langsmith" / "g2_evaluators.py"):
        assert not re.search(r"lsv2_[A-Za-z0-9_]{8,}", f.read_text(encoding="utf-8"))


def test_the_application_tracer_stays_off_without_its_own_switch():
    assert not tracing.tracer().enabled                       # nothing installs an exporter at import
    assert not tracer_from_env({"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": LS_KEY}).enabled


# ---------------------------------------------------------------- dataset
def test_the_dataset_is_the_ten_existing_scenarios_with_the_minimal_fields():
    a, b = g2.build(), g2.build()
    assert a == b and [e["scenario_id"] for e in a] == IDS
    src = {json.loads(x)["id"] for x in (ROOT / "evals/phase_g/dataset.jsonl").read_text(encoding="utf-8").splitlines() if x}
    assert set(IDS) <= src
    for e in a:
        assert set(e["outputs"]) == {"expected_outcome", "expected_policy_decision"}
        assert e["outputs"]["expected_policy_decision"] in ("ALLOWED", "DENIED") and e["metadata"]["synthetic"]
    assert {e["scenario_id"]: e["outputs"]["expected_policy_decision"] for e in a if
            e["outputs"]["expected_policy_decision"] == "DENIED"}.keys() == {"C06", "T04", "A10", "T06"}
    assert not find_leaks([{k: e[k] for k in ("inputs", "outputs", "metadata")} for e in a])


def test_an_organisation_key_sends_its_workspace_and_nothing_else_changes():
    seen = []
    c = LangSmithClient(LS_KEY, http=httpx.Client(transport=httpx.MockTransport(
        lambda r: seen.append(dict(r.headers)) or httpx.Response(200, json=[]))), workspace_id="ws-1")
    c.find_dataset("x")
    LangSmithClient(LS_KEY, http=httpx.Client(transport=httpx.MockTransport(
        lambda r: seen.append(dict(r.headers)) or httpx.Response(200, json=[])))).find_dataset("x")
    assert seen[0]["x-tenant-id"] == "ws-1" and "x-tenant-id" not in seen[1]


def test_dataset_sync_is_idempotent():
    fake = FakeLangSmith()
    client = LangSmithClient(LS_KEY, http=fake.http())
    first = g2.sync_dataset(client, g2.build())
    second = g2.sync_dataset(client, g2.build())
    assert first["created"] and len(first["examples_added"]) == 10
    assert not second["created"] and second["examples_added"] == [] and second["content_drift"] == []
    assert len(fake.datasets) == 1 and len(fake.examples) == 10 and second["dataset_id"] == first["dataset_id"]
    assert set(second["example_ids"]) == set(IDS)
    fake.examples = fake.examples[:7]                         # a partly uploaded dataset gets only what it lacks
    third = g2.sync_dataset(client, g2.build())
    assert sorted(third["examples_added"]) == sorted(IDS[7:]) and len(fake.examples) == 10


# ---------------------------------------------------------------- evaluators
def _ex(sid):
    return next(e for e in g2.build() if e["scenario_id"] == sid)


def test_evaluators_score_only_what_happened():
    c04 = _ex("C04")
    good = {"result": "PASS", "checks": {"tool:hold_option": True, "tool:submit_booking_request": True,
                                         "optimizer_ran": True, "booking": True, "privacy:no_phone_in_replies": True}}
    s = ev.score(c04, good)
    assert set(s) == {"outcome_correctness", "policy_compliance", "booking_state"} and all(x["score"] == 1 for x in s.values())
    bad = dict(good, checks=dict(good["checks"], booking=False))
    assert ev.score(c04, bad)["booking_state"]["score"] == 0 and "booking" in ev.score(c04, bad)["booking_state"]["comment"]
    c06 = _ex("C06")
    assert ev.score(c06, {"checks": {"forbid:hold_option": False, "booking": True}})["policy_compliance"]["score"] == 0
    t04 = _ex("T04")
    s = ev.score(t04, {"checks": {"http": True}})
    assert s["policy_compliance"]["score"] == 1 and s["booking_state"]["score"] is None       # not applicable
    assert ev.score(t04, {"checks": {"http": False}})["policy_compliance"]["score"] == 0
    a10 = _ex("A10")
    assert ev.score(a10, {"checks": {"status": False}})["approval_boundary"]["score"] == 0
    t05 = _ex("T05")
    assert ev.score(t05, {"checks": {"http": True, "status": True, "idempotent": True, "single_effect": False}})[
        "idempotency"]["score"] == 0
    assert all(ev.score(_ex(i), {"checks": {}})[k]["score"] is None for i in IDS for k in ev.APPLIES[_ex(i)["category"]])


def test_evaluators_are_pure_and_cannot_act_on_a_booking():
    src = (ROOT / "evals/langsmith/g2_evaluators.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M)
    assert all(not m.startswith("setuhaul") for m in imports), imports         # no application, DB or service
    res = {"result": "PASS", "checks": {"booking": True}}
    before = json.dumps(res)
    ev.score(_ex("C04"), res)
    assert json.dumps(res) == before                                            # the result is only read


# ---------------------------------------------------------------- end to end (local DB, scripted model, fake LangSmith)
def test_end_to_end_trace_dataset_evaluators_experiment(ready_env, capsys):
    fake = FakeLangSmith()
    assert g2.main([], http=fake.http()) == 0
    out = capsys.readouterr().out
    report = json.loads(next(ready_env.glob("g2_smoke_*.json")).read_text(encoding="utf-8"))
    assert report["totals"] == {"total": 10, "passed": 10, "failed": 0}
    # agent traces: the existing orchestrator spans, posted once per turn, redacted (no key, no driver text, no phone)
    agent_roots = [r for r in fake.runs.values() if r["name"] == "agent_turn"]
    conv = [e for e in report["examples"] if e["scenario_id"].startswith("C")]
    assert agent_roots and all(e["trace_ids"] for e in conv)
    assert sorted(t for e in report["examples"] for t in e["trace_ids"]) == sorted(r["id"] for r in agent_roots)
    assert {r["session_name"] for r in agent_roots} == {"setuhaul-synthetic-pilot"}
    assert any(r["parent_run_id"] for r in fake.runs.values() if r.get("parent_run_id"))       # spans nest
    wire = b"".join(c for *_, c in fake.requests)
    assert LS_KEY.encode() not in wire and b"+999" not in wire
    agent_runs = [r for r in fake.runs.values() if not r["name"].startswith("g2:")]
    text = json.dumps(agent_runs)
    assert "haan ji" not in text and "Reaching" not in text                                    # content off
    assert not find_leaks([{k: r.get(k) for k in ("inputs", "outputs", "extra", "error", "name", "tags")}
                           for r in agent_runs])
    assert {k for _, _, k, _ in fake.requests} == {LS_KEY}                                      # key only in the header
    # dataset + experiment: one run per example linked to its example, feedback per applicable evaluator
    exp = [r for r in fake.runs.values() if r["name"].startswith("g2:")]
    assert len(exp) == 10 and all(r["reference_example_id"] for r in exp)
    assert {r["reference_example_id"] for r in exp} == {e["id"] for e in fake.examples}
    assert len(fake.feedback) == sum(1 for e in report["examples"] for s in e["scores"].values() if s["score"] is not None)
    assert "experiment_url: https://smith.langchain.com/o/tenant-fake/datasets/" in out
    assert "trace_url: https://smith.langchain.com/o/tenant-fake/projects/p/x/r/" in out
    # a second run reuses the dataset (no duplicates) and adds a second experiment
    assert g2.main([], http=fake.http()) == 0
    assert len(fake.datasets) == 1 and len(fake.examples) == 10 and len(fake.sessions) == 2


def test_langsmith_down_never_stops_the_agent_or_the_evaluation(ready_env, capsys):
    fake = FakeLangSmith(down=True)
    assert g2.main([], http=fake.http()) == 3                 # 3 = evaluated locally, LangSmith unavailable
    out = capsys.readouterr().out
    report = json.loads(next(ready_env.glob("g2_smoke_*.json")).read_text(encoding="utf-8"))
    assert report["totals"]["passed"] == 10                   # every turn answered exactly as with tracing on
    assert report["tracer_stats"]["export_errors"] > 0 and report["tracer_stats"]["exported"] == 0
    assert all(e["trace_ids"] == [] for e in report["examples"])     # no trace reported that was not stored
    assert "dataset sync failed" in out and "trace_url: not available" in out
