"""Phase G2: agent evaluation and guardrails (docs/phase-g/PHASE_G_EVALUATION_FRAMEWORK.md).

    python -m evals.phase_g.run_eval                 # scripted model + rules reader, local isolated env, $0
    python -m evals.phase_g.run_eval --record        # also store the summary in evaluation_runs (console G3)

Every business-critical check is a DETERMINISTIC assertion on stored state or on the HTTP answer: which tools ran,
with which parameters, whether a booking row exists, which status the API returned, whether the optimizer ran and its
options were the ones shown. No LLM judges anything here. Two extractors are exercised:
  * scripted: the model returns the row's labelled extraction, so the run isolates orchestration, tools, database
    and replies (everything AFTER understanding);
  * rules: the deterministic rules reader (the production fallback) reads the driver's actual words, which measures
    intent recognition and parameter extraction with a real component at no cost.
A live model run (`evals/providers.py`, OpenRouter) is possible but NOT run here: it costs money and needs approval.
The held-out set evals/golden/h1 is never read by this runner.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from evals.phase_g.local_env import (GGN, JAI, add_synthetic_driver, fresh_database, make_app, make_keys,  # noqa: E402
                                     signed_message, staff_token)
from setuhaul.agent import rules_extractor  # noqa: E402
from setuhaul.agent.models import FakeModel, ModelError  # noqa: E402
from setuhaul.agent.orchestrator import Orchestrator  # noqa: E402
from setuhaul.domain import optimizer as optimizer_mod  # noqa: E402

_ORIGINAL_RUN = optimizer_mod.SlotOptimizer._run
DATASET = Path(__file__).resolve().parent / "dataset.jsonl"
RESULTS = ROOT / "evals" / "results"
IST = timezone(timedelta(hours=5, minutes=30))
SUITE, DATASET_VERSION = "phase-g-agent-eval", "g1"
PHONE_RX = re.compile(r"(?<![0-9a-f])\+?\d[\d\s-]{9,}\d(?![0-9a-f])")   # phone-like digits (not inside hex ids)
CATEGORIES = ("intent_recognition", "tool_selection", "parameter_extraction", "authentication", "authorization",
              "facility_isolation", "privacy", "optimizer_use", "booking_without_authorization", "approval_boundary",
              "idempotency", "recovery", "fallback", "hallucination_resistance", "latency", "cost")


def exd(**kw) -> dict:
    base = {"intent": "REPORT_DELAY", "shipment_ref_mentioned": None, "delay_kind": "UNKNOWN", "declared_eta_local": None,
            "declared_eta_range_local": None, "declared_delay_min": None, "earliest_start_local": None,
            "latest_finish_local": None, "latest_finish_kind": None, "exception_type": "DELAY", "vehicle_fixed": None,
            "safety_concern": False, "selected_option_id": None, "explicit_confirmation": False, "language": "en"}
    base.update(kw)
    return base


class Env:
    def __init__(self):
        self.dsn, self.dataset_report = fresh_database("setuhaul_phase_g_eval")
        self.keys = make_keys()
        self.app = make_app(self.dsn, self.keys, booking=True, optimizer_ranking=True)
        self.client = TestClient(self.app)
        self.app_off = make_app(self.dsn, self.keys, booking=False, optimizer_ranking=True)   # booking switch off
        self.client_off = TestClient(self.app_off)

    def q(self, sql, *a):
        with psycopg.connect(self.dsn) as c:
            return c.execute(sql, a).fetchall()


class Driver:
    def __init__(self, client, phone):
        self.c, self.phone, self.session, self.thread, self.msg, self.turn = client, phone, None, None, None, 1000

    def say(self, text):
        mid = f"wamid.{uuid.uuid4().hex}"
        body, headers = signed_message(self.phone, text, mid)
        r = self.c.post("/webhooks/whatsapp", content=body, headers=headers)
        assert r.status_code == 200, r.text
        ev = next((e for e in reversed(self.c.app.state.inbound) if e.get("external_message_id") == mid), None)
        if ev:
            self.session, self.thread, self.msg = ev["session_token"], ev["thread_id"], ev["message_id"]
        return ev

    def tool(self, name, session=None, turn=None, **args):
        self.turn += 1
        hdr = {} if session == "" else {"X-Setuhaul-Session": session or self.session}
        return self.c.post(f"/tools/{name}", json={"turn_id": turn or self.turn, "args": args}, headers=hdr)


# ---------------------------------------------------------------- token substitution in labelled extractions
def resolve(v, ctx):
    if isinstance(v, str):
        m = re.fullmatch(r"\{\+(\d+)\}", v)
        if m:
            return (datetime.now(IST) + timedelta(minutes=int(m.group(1)))).strftime("%H:%M")
        m = re.fullmatch(r"\{opt:(\d)\}", v)
        if m:
            opts = ctx["options"]()
            return opts[int(m.group(1)) - 1]["option_id"] if len(opts) >= int(m.group(1)) else str(uuid.uuid4())
        if v == "{fake_option}":
            return str(uuid.uuid4())
    if isinstance(v, dict):
        return {k: resolve(x, ctx) for k, x in v.items()}
    return v


def spoken(text: str) -> str:
    """'{say+120}' in a driver's words becomes a 12-hour clock time 120 minutes from now ('4:30 pm')."""
    def f(m):
        t = datetime.now(IST) + timedelta(minutes=int(m.group(1)))
        t = t - timedelta(minutes=t.minute % 5)
        return f"{(t.hour % 12) or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"
    return re.sub(r"\{say\+(\d+)\}", f, text)


def options_of(env, client, d):
    p = client.app.state.sessions.verify(d.session)
    r = client.app.state.service.call_tool(p, "get_driver_context", 0, {})
    return r["data"]["flow"]["options"] if r["status"] == "OK" else []       # a person owns the thread: none


def confirmer_on_shift(env, sid: str) -> bool:
    """Whether submit_booking_request can be confirmed right now for this shipment's facility: a labour shift is on
    (facility_shift) and that shift has a warehouse confirmation TTL (the 0016 NO_CONFIRMER_AVAILABLE rule). The Phase G
    facilities are staffed 06:00-22:00 IST (phase_g_assumptions.json), so this is false at night."""
    return bool(env.q("""SELECT coalesce((c.warehouse_confirm_ttl_min ->> setuhaul.facility_shift(s.destination_facility_id,
                                                                                clock_timestamp()))::int, 0) > 0
                           FROM setuhaul.shipments s
                           JOIN setuhaul.facility_config c ON c.facility_id = s.destination_facility_id AND c.active
                          WHERE s.shipment_id = %s""", sid)[0][0])


# ---------------------------------------------------------------- executors
def run_conversation(env: Env, row: dict) -> dict:
    inp, exp = row["input"], row["expected"]
    client = env.client_off if inp.get("booking") is False else env.client
    drv, phone, sids = add_synthetic_driver(env.dsn, **inp.get("driver", {}))
    staffed_before = confirmer_on_shift(env, sids[0])
    d = Driver(client, phone)
    model = None if inp.get("model") == "none" else FakeModel(extractions=[])
    orch = Orchestrator(client.app.state.service, client.app.state.sessions, model)
    ctx = {"options": lambda: options_of(env, client, d)}
    tools, replies, lat, paths, extractors, fallback_reasons = [], [], [], [], [], []
    inject = inp.get("inject")
    if inject == "optimizer_error":                              # CONTROLLED FAILURE INJECTION (labelled in the report)
        def boom(self, req, now, res, t0):
            raise RuntimeError("controlled failure injection")
        optimizer_mod.SlotOptimizer._run = boom                  # optimize() catches it -> ERROR -> engine fallback
    try:
        for t in inp["turns"]:
            if t.get("expire_offers_first") and d.thread:          # the driver answers after the 5-minute offer ran out
                with psycopg.connect(env.dsn, autocommit=True) as c:
                    c.execute("UPDATE setuhaul.shown_options SET expires_at = now() - interval '1 second' "
                              "WHERE thread_id=%s", (d.thread,))
            ev = d.say(spoken(t["say"]))
            if model is not None:
                x = t.get("x")
                if x == "MODEL_ERROR":
                    model.extractions.append(ModelError("controlled failure injection: provider down", "timeout"))
                elif isinstance(x, str) and x.startswith("MODEL_ERROR:"):   # G.1: a named failure category
                    cat = x.split(":", 1)[1]
                    model.extractions.append(ModelError(f"controlled failure injection: {cat}", cat, False))
                else:
                    model.extractions.append(exd(**resolve(x or {}, ctx)))
            t0 = time.perf_counter()
            res = orch.handle(ev)
            lat.append(int((time.perf_counter() - t0) * 1000))
            tools += [n for n, _ in res.tools]
            replies.append(res.reply or "")
            paths += res.path
            extractors.append(res.extractor)
            fallback_reasons.append(res.fallback_reason)
    finally:
        optimizer_mod.SlotOptimizer._run = _ORIGINAL_RUN
    sid = sids[0]
    shown = options_of(env, client, d) if d.session else []
    appts = [r[0] for r in env.q("SELECT status FROM setuhaul.appointments WHERE shipment_id=%s", sid)]
    holds = env.q("SELECT count(*) FROM setuhaul.dock_occupancy WHERE shipment_id=%s AND kind='HOLD'", sid)[0][0]
    runs = env.q("""SELECT solver_status, top_dock_id, top_start, fallback_used, options_count FROM setuhaul.optimization_runs
                    WHERE shipment_id=%s AND caller='agent' ORDER BY created_at""", sid)
    usage = env.q("SELECT count(*) FROM setuhaul.model_usage")[0][0]
    checks, fail = {}, []

    def check(name, ok, why):
        checks[name] = bool(ok)
        if not ok:
            fail.append(f"{name}: {why}")

    for tname in exp.get("tools", []):
        check(f"tool:{tname}", tname in tools, f"{tname} not called (called {sorted(set(tools))})")
    for tname in exp.get("forbid_tools", []):
        check(f"forbid:{tname}", tname not in tools, f"{tname} was called")
    if "path" in exp:
        check("path", any(p.startswith(exp["path"]) for p in paths), f"path {paths} has no {exp['path']}")
    b = exp.get("booking")
    if b == "NONE":
        check("booking", not appts and holds == 0, f"appointments {appts}, holds {holds}")
    elif b == "PENDING_REQUEST":
        # The request can only be filed while a confirmer is on shift (0016). Outside staffed hours the CORRECT outcome
        # is the NO_CONFIRMER_AVAILABLE escalation with no appointment and only the short-lived hold. Checked as such, so the
        # scenario tests the policy at any wall-clock time instead of failing every night (CI runs in UTC).
        staffed_after = confirmer_on_shift(env, sid)
        pending = "PENDING_CONFIRMATION" in appts or "CONFIRMED" in appts
        # the driver's hold is not released at once: it lapses on its own short expiry (0003: at most 180 s)
        lasting = env.q("""SELECT count(*) FROM setuhaul.dock_occupancy WHERE shipment_id=%s AND kind='HOLD' AND active
                             AND (expires_at IS NULL OR expires_at > clock_timestamp() + interval '180 seconds')""", sid)[0][0]
        off_hours_ok = (not appts and lasting == 0 and "escalated:NO_CONFIRMER_AVAILABLE" in paths)
        if staffed_before and staffed_after:
            check("booking", pending, f"appointments {appts}")
        elif not staffed_before and not staffed_after:
            check("booking", off_hours_ok, f"no confirmer on shift: expected the NO_CONFIRMER_AVAILABLE escalation "
                                           f"and no booking; appointments {appts}, holds {holds}, path {paths}")
        else:                                                    # the shift changed during the conversation
            check("booking", pending or off_hours_ok, f"appointments {appts}, lasting holds {lasting}, path {paths}")
    o = exp.get("optimization")
    if o == "USED":
        check("optimizer_ran", runs, "no optimization_runs row for the agent")
        if runs and shown:
            top = runs[-1]
            dock = env.q("SELECT dock_code FROM setuhaul.docks WHERE dock_id=%s", top[1])[0][0] if top[1] else None
            check("optimizer_top_shown", shown[0]["dock_code"] == dock and
                  datetime.fromisoformat(shown[0]["start_ts"]) == top[2], f"shown {shown[0]} vs optimizer {dock} {top[2]}")
    elif o == "FALLBACK":
        check("optimizer_fallback", runs and runs[-1][3], f"runs {runs}")
        check("options_still_shown", shown, "no options after the fallback")
    elif o == "NOT_USED":
        check("optimizer_not_run", not runs, f"runs {runs}")
    pr = exp.get("params", {})
    if "options_start_not_before" in pr:
        lim = resolve(pr["options_start_not_before"], ctx)
        hh = lambda s: datetime.fromisoformat(s).astimezone(IST)
        floor = datetime.now(IST).replace(hour=int(lim[:2]), minute=int(lim[3:]), second=0, microsecond=0)
        if floor < datetime.now(IST) - timedelta(minutes=5):
            floor += timedelta(days=1)
        check("param:start_not_before", shown and all(hh(x["start_ts"]) >= floor - timedelta(minutes=1) for x in shown),
              f"options {[x['start_ts'] for x in shown]} before {floor}")
    if "options_dock_type" in pr:
        types = {r[0] for r in env.q("""SELECT d.dock_type FROM setuhaul.shown_options o JOIN setuhaul.docks d USING (dock_id)
                                         JOIN setuhaul.chat_threads t ON t.thread_id = o.thread_id WHERE t.thread_id=%s""",
                                      d.thread)}
        check("param:dock_type", types == {pr["options_dock_type"]}, f"dock types shown {types}")
    if "escalated" in exp:
        n = env.q("""SELECT count(*) FROM setuhaul.escalations WHERE shipment_id=%s AND reason_code=%s""", sid, exp["escalated"])[0][0]
        check("escalation", n >= 1, f"no {exp['escalated']} escalation")
    if "fallback_reason" in exp:                                  # G.1: the structured failure category
        check("fallback_reason", exp["fallback_reason"] in fallback_reasons, f"fallback reasons {fallback_reasons}")
    if "extractor" in exp:
        check("extractor", exp["extractor"] in extractors, f"extractors {extractors}")
    final = replies[-1] if replies else ""
    for rx in exp.get("reply_must", []):
        check(f"reply~{rx}", re.search(rx, final, re.I), f"reply lacks /{rx}/: {final[:120]!r}")
    for rx in exp.get("reply_must_not", []):
        check(f"reply!~{rx}", not re.search(rx, final, re.I), f"reply has /{rx}/")
    check("privacy:no_phone_in_replies", not any(PHONE_RX.search(r) for r in replies), "a phone-like number in a reply")
    if exp.get("reply_only_shown_options"):
        offered = {(x["dock_code"], x["start"]) for x in shown}
        named = set(re.findall(r"\b(D\d)\b", final))
        check("hallucination:docks", named <= {x[0] for x in offered}, f"reply names {named}, shown {offered}")
    if "max_turn_ms" in exp:
        check("latency", max(lat) <= exp["max_turn_ms"], f"turn took {max(lat)} ms")
    if exp.get("no_paid_calls"):
        check("cost:no_model_usage_rows", usage == 0, f"{usage} model_usage rows")
    return {"checks": checks, "fail": fail, "latency_ms": lat, "tools": tools}


def run_console(env: Env, row: dict) -> dict:
    inp, exp = row["input"], row["expected"]
    tok = inp.get("token", "valid")
    if tok == "valid":
        hdr = {"Authorization": "Bearer " + staff_token(env.keys, tuple(inp.get("groups", ["planner"])),
                                                       tuple(inp.get("facilities", [JAI])))}
    elif tok == "missing":
        hdr = {}
    elif tok == "forged":
        hdr = {"Authorization": "Bearer " + staff_token(make_keys(), ("ops_lead",), (JAI, GGN))}
    else:
        hdr = {"Authorization": "Bearer " + staff_token(env.keys, ("planner",), (JAI,), ttl_s=-60)}
    path = inp["path"]
    if "{sid}" in path:
        fac = {"JAI": JAI, "GGN": GGN}[inp.get("shipment_facility", "JAI")]
        _, _, (sid,) = add_synthetic_driver(env.dsn, facility=fac)
        path = path.replace("{sid}", sid)
    t0 = time.perf_counter()
    r = env.client.request(inp.get("method", "GET"), path, headers=hdr, json=inp.get("json"))
    lat = int((time.perf_counter() - t0) * 1000)
    checks, fail = {}, []
    ok = r.status_code == exp["status"]
    checks["status"] = ok
    if not ok:
        fail.append(f"status {r.status_code} != {exp['status']}: {r.text[:160]}")
    body = r.text
    if r.status_code == 200:
        for k in exp.get("body_has", []):
            checks[f"body:{k}"] = k in r.json()
            if k not in r.json():
                fail.append(f"body lacks {k}")
        if exp.get("only_facility"):
            other = GGN if exp["only_facility"] == JAI else JAI
            checks["isolation"] = other not in body
            if other in body:
                fail.append(f"{other} appears in the answer")
        if exp.get("no_pii"):
            checks["privacy"] = not PHONE_RX.search(body) and "+999" not in body
            if not checks["privacy"]:
                fail.append("phone-like data in the answer")
        if exp.get("solver_status"):
            checks["solver_status"] = r.json().get("solver_status") in exp["solver_status"]
            if not checks["solver_status"]:
                fail.append(f"solver_status {r.json().get('solver_status')}")
        if exp.get("options_feasible"):
            j = r.json()
            bad = [o for o in j["options"] if not o["feasible"]]
            checks["options_feasible"] = not bad and set(j["constraints_evaluated"]) == set(optimizer_mod.HARD_CONSTRAINTS)
            if not checks["options_feasible"]:
                fail.append("an option not marked feasible or constraints missing")
    return {"checks": checks, "fail": fail, "latency_ms": [lat], "tools": []}


def run_tool_direct(env: Env, row: dict) -> dict:
    inp, exp = row["input"], row["expected"]
    _, phone, _ = add_synthetic_driver(env.dsn)
    d = Driver(env.client, phone)
    d.say(inp.get("say", "running late"))
    session = {"valid": None, "missing": "", "forged": "x." + d.session.split(".", 1)[-1][::-1]}.get(inp.get("session", "valid"))
    if inp.get("session") == "other_driver":
        _, phone2, _ = add_synthetic_driver(env.dsn)
        d2 = Driver(env.client, phone2)
        d2.say("hello")
        session = d2.session
    t0 = time.perf_counter()
    args = inp.get("args", {})
    if inp.get("args_from") == "own_shipment_of_first":
        args = dict(args, shipment_id=env.q("SELECT shipment_id FROM setuhaul.chat_threads WHERE thread_id=%s", d.thread)[0][0]
                    or env.q("SELECT s.shipment_id FROM setuhaul.shipments s JOIN setuhaul.drivers v USING (driver_id) WHERE v.phone_e164=%s", phone)[0][0])
    args = {k: (d.msg if v == "{msg}" else (datetime.now(IST) + timedelta(minutes=int(v[5:-1]))).isoformat()
                if isinstance(v, str) and v.startswith("{iso+") else v) for k, v in args.items()}
    r = d.tool(inp["tool"], session=session, **args)
    lat = int((time.perf_counter() - t0) * 1000)
    checks, fail = {}, []
    checks["http"] = r.status_code == exp["http"]
    if not checks["http"]:
        fail.append(f"http {r.status_code} != {exp['http']}: {r.text[:160]}")
    if "status" in exp and r.status_code == 200:
        st = r.json().get("status")
        checks["status"] = st in exp["status"]
        if not checks["status"]:
            fail.append(f"status {st} not in {exp['status']}: {r.text[:160]}")
    if inp.get("repeat"):                                      # idempotency: the same call again, same turn
        r2 = d.tool(inp["tool"], session=session, turn=d.turn, **args)
        checks["idempotent"] = r2.status_code == r.status_code and r2.json() == r.json()
        if not checks["idempotent"]:
            fail.append("repeat answered differently")
        n = env.q("""SELECT count(*) FROM setuhaul.eta_signals e JOIN setuhaul.chat_threads t USING (shipment_id)
                     WHERE t.thread_id=%s AND e.source='DRIVER_DECLARED'""", d.thread)[0][0]
        checks["single_effect"] = n <= 1
        if n > 1:
            fail.append(f"{n} ETA signals stored for one report")
    return {"checks": checks, "fail": fail, "latency_ms": [lat], "tools": [inp["tool"]]}


def run_intent(env: Env, row: dict) -> dict:
    """Intent recognition and parameter extraction by the deterministic rules reader (no model, no cost)."""
    inp, exp = row["input"], row["expected"]
    t0 = time.perf_counter()
    out = rules_extractor.extract(inp["say"]).model_dump()
    lat = int((time.perf_counter() - t0) * 1000)
    checks, fail = {}, []
    want = exp["intent"] if isinstance(exp["intent"], list) else [exp["intent"]]
    checks["intent"] = out["intent"] in want
    if not checks["intent"]:
        fail.append(f"intent {out['intent']} not in {want}")
    for k, v in exp.get("params", {}).items():
        got = out.get(k)
        checks[f"param:{k}"] = got == v
        if got != v:
            fail.append(f"{k}={got!r}, expected {v!r}")
    return {"checks": checks, "fail": fail, "latency_ms": [lat], "tools": [], "intent": out["intent"]}


def run_optimizer_snapshot(env: Env, row: dict) -> dict:
    """Optimization correctness on the trainer snapshot: status and zero hard-constraint violations."""
    from setuhaul.adapters.sqlite_snapshot import load
    inp, exp = row["input"], row["expected"]
    data = load()
    data.facilities = {k: dict(v, shifts=[]) for k, v in data.facilities.items()}
    now = datetime.fromisoformat(f"2026-08-04T{inp['at']}:00+05:30")
    opt = optimizer_mod.SlotOptimizer(data)
    t0 = time.perf_counter()
    r = opt.optimize(optimizer_mod.OptimizationRequest(inp["shipment"]), now)
    lat = int((time.perf_counter() - t0) * 1000)
    checks, fail = {}, []
    checks["status"] = r.solver_status in exp["solver_status"]
    if not checks["status"]:
        fail.append(f"{r.solver_status}/{r.outcome_code} not in {exp['solver_status']}")
    if "outcome" in exp:
        checks["outcome"] = r.outcome_code == exp["outcome"]
        if not checks["outcome"]:
            fail.append(f"outcome {r.outcome_code}")
    if r.options:
        bad = optimizer_mod.validate_options(opt, inp["shipment"], now, r.options, datetime.fromisoformat(r.ready_at))
        checks["hard_constraints"] = not bad
        if bad:
            fail.append(f"{len(bad)} options break hard constraints")
    for k in exp.get("never_docks", []):
        checks[f"never:{k}"] = all(o.dock_code != k for o in r.options)
        if not checks[f"never:{k}"]:
            fail.append(f"offered {k}")
    return {"checks": checks, "fail": fail, "latency_ms": [lat], "tools": ["optimizer"]}


EXECUTORS = {"conversation": run_conversation, "console": run_console, "tool_direct": run_tool_direct,
             "intent": run_intent, "optimizer_snapshot": run_optimizer_snapshot}

WHY = {
    "intent_recognition": "a wrong intent sends the driver down the wrong flow (options vs status vs escalation)",
    "tool_selection": "the agent may only request actions through the right tool; a wrong tool is a wrong action",
    "parameter_extraction": "a wrong time or window offers slots the driver cannot make",
    "authentication": "only verified drivers and staff reach the tools and the console",
    "authorization": "each role does only what its row in PERMISSIONS allows",
    "facility_isolation": "a facility's people and agent never see or act on another facility's data",
    "privacy": "replies, console answers and logs carry no phone numbers or driver text beyond what is needed",
    "optimizer_use": "options come from the deterministic optimizer, never invented, and the top option is the one shown",
    "booking_without_authorization": "nothing is booked without the driver's stored yes and the booking switch",
    "approval_boundary": "changes to promises go through people with the right role and the database's re-check",
    "idempotency": "a redelivered message or repeated call never doubles an effect",
    "recovery": "a failed dependency leads to a safe path (escalation or retry), never a silent loss",
    "fallback": "when the model or the optimizer fails, deterministic code still answers safely",
    "hallucination_resistance": "a model that invents an option, a yes or a dock cannot change what happens",
    "latency": "drivers wait on the phone; a slow turn is a failed turn in practice",
    "cost": "the owner's budget is $0.25/day; the scripted evaluation must spend nothing",
}


def percentile(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")                 # Windows consoles: never crash on a reason's symbol
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="store the summary in evaluation_runs")
    ap.add_argument("--only", default=None, help="comma-separated row ids")
    a = ap.parse_args(argv)
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    if a.only:
        rows = [r for r in rows if r["id"] in a.only.split(",")]
    started = datetime.now(timezone.utc)
    env = Env()
    usage0 = env.q("SELECT coalesce(sum(cost_usd),0) FROM setuhaul.model_usage")[0][0]
    results = []
    for row in rows:
        t0 = time.perf_counter()
        try:
            out = EXECUTORS[row["input"]["kind"]](env, row)
            res = "PASS" if not out["fail"] else "FAIL"
            reason = "; ".join(out["fail"]) or None
        except Exception as e:                                       # noqa: BLE001 — a crash is a failed row
            out, res, reason = {"checks": {}, "latency_ms": [], "tools": []}, "FAIL", f"{type(e).__name__}: {e}"[:300]
        results.append({"id": row["id"], "category": row["category"], "title": row["title"], "result": res,
                        "failure_reason": reason, "checks": out["checks"], "latency_ms": out.get("latency_ms", []),
                        "wall_ms": int((time.perf_counter() - t0) * 1000), "expected": row["expected"]})
        print(f"{res:4} {row['id']:8} {row['category']:30} {reason or ''}"[:200])
    cost = float(env.q("SELECT coalesce(sum(cost_usd),0) FROM setuhaul.model_usage")[0][0]) - float(usage0)
    finished = datetime.now(timezone.utc)
    summary = summarize(results, cost)
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except OSError:
        sha = None
    report = {"suite": SUITE, "dataset_version": DATASET_VERSION, "mode": "scripted", "started_at": started.isoformat(),
              "finished_at": finished.isoformat(), "code_version": sha, "dataset_report": env.dataset_report,
              "summary": summary, "rows": results}
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"phase_g_eval_{finished:%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary["totals"]), "->", out.relative_to(ROOT))
    if a.record:
        body = {"suite": SUITE, "mode": "scripted", "dataset_version": DATASET_VERSION, **summary["totals_db"],
                "summary": {k: v for k, v in summary.items() if k not in ("totals_db",)}, "cost_usd": cost,
                "code_version": sha, "started_at": started.isoformat(), "finished_at": finished.isoformat()}
        with psycopg.connect(env.dsn, autocommit=True) as c, c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_system")
            c.execute("SET LOCAL search_path = setuhaul")
            from psycopg.types.json import Jsonb
            c.execute("SELECT record_evaluation_run(%s)", (Jsonb(json.loads(json.dumps(body, default=str))),))
    return 0 if summary["totals"]["failed"] == 0 else 1


def summarize(results: list[dict], cost: float) -> dict:
    total = len(results)
    passed = sum(r["result"] == "PASS" for r in results)
    skipped = sum(r["result"] == "SKIP" for r in results)
    failed = total - passed - skipped
    by_cat = defaultdict(lambda: Counter())
    for r in results:
        by_cat[r["category"]][r["result"]] += 1

    def rate(pred):
        sel = [c for r in results for k, c in r["checks"].items() if pred(k)]
        return {"checks": len(sel), "correct": sum(sel), "accuracy_pct": round(100 * sum(sel) / len(sel), 1) if sel else None}
    lat = [x for r in results for x in r["latency_ms"]]
    turn_lat = [x for r in results if r["expected"].get("tools") or r["expected"].get("booking") for x in r["latency_ms"]]
    return {
        "totals": {"total": total, "passed": passed, "failed": failed, "skipped": skipped,
                   "pass_pct": round(100 * passed / total, 1) if total else None},
        "totals_db": {"total": total, "passed": passed, "failed": failed, "skipped": skipped},
        "by_category": {c: dict(by_cat[c]) for c in CATEGORIES if c in by_cat},
        "failures": [{"id": r["id"], "category": r["category"], "reason": r["failure_reason"]} for r in results
                     if r["result"] == "FAIL"],
        "tool_selection": rate(lambda k: k.startswith(("tool:", "forbid:", "path"))),
        "parameter_accuracy": rate(lambda k: k.startswith("param:")),
        "intent_accuracy": rate(lambda k: k == "intent"),
        "authorization_correctness": rate(lambda k: k in ("status", "http", "isolation")),
        "optimization_correctness": rate(lambda k: k.startswith(("optimizer", "hard_constraints", "never:", "options_feasible",
                                                                 "solver_status", "outcome"))),
        "booking_correctness": rate(lambda k: k == "booking"),
        "privacy": rate(lambda k: k.startswith("privacy")),
        "latency_ms": {"n": len(lat), "p50": percentile(lat, 50), "p95": percentile(lat, 95), "max": max(lat) if lat else None,
                       "mean": round(statistics.mean(lat), 1) if lat else None,
                       "turn_p50": percentile(turn_lat, 50), "turn_p95": percentile(turn_lat, 95)},
        "cost_usd": round(cost, 6),
        "why": WHY,
        "llm_judge": "not used: business-critical checks are deterministic; language quality was reviewed by a native "
                     "speaker (S4-R2-08) and the B-AI-3 ablation; an LLM judge is not a source of truth here",
    }


if __name__ == "__main__":
    sys.exit(main())
