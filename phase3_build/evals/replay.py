"""B-EV-2: replay the golden set end to end, through the real API, orchestrator, booking service and PostgreSQL.

    python evals/replay.py                       # scripted model: returns each turn's labelled extraction (free)
    python evals/replay.py --model live          # the eval provider switch (evals/providers.py), e.g. Haiku via OpenRouter

The scripted run tests everything AFTER extraction: decisions, tool calls, database end state and reply rules. The
live run tests the whole chain: message -> model -> code checks -> tools -> database -> reply. Comparing the two
separates model errors from orchestration errors.

Fixture (per scenario, in its own database cloned from a migrated template):
  * the challenge snapshot, loaded through every constraint by `adapters/pg_seed.py`, moved by whole days so that
    the scenario's 4 August becomes tomorrow; whole days keep every time-of-day rule (opening hours, 21:00 last start,
    shifts) and keep all database expiry times consistent with the database clock;
  * the service and orchestrator run on the scenario clock;
  * replay-only, never in production migrations: (1) a trigger stamps driver-declared ETA signals at the scenario
    clock, because the engine never uses a signal observed after its own clock; (2) the facility shifts are widened
    to cover the whole day, because the database picks the warehouse shift from the real time of day;
  * seed threads: a THR scenario keeps its own seed thread open; every other scenario starts a fresh conversation.
A scenario that starts mid-conversation (options already shown, or a read-back awaiting a yes) is set up through
the real tools. If the engine's options differ from those the label assumed, checks that name a dock are N/A.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from evals.golden import DATE, Scenario, Turn, load, oracle_raw  # noqa: E402
from setuhaul.adapters.pg_seed import seed  # noqa: E402
from setuhaul.adapters.sqlite_snapshot import SNAPSHOT  # noqa: E402
from setuhaul.agent.models import FakeModel  # noqa: E402
from setuhaul.agent.orchestrator import Orchestrator  # noqa: E402
from setuhaul.api.app import Settings, create_app  # noqa: E402
from setuhaul.api.service import human_time  # noqa: E402
from setuhaul.db.migrate import migrate  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
META = b"replay-meta-app-secret-0123456789abcdef"
FACS = ("FAC-JAI-01", "FAC-GGN-01")
SKIP_NEEDS = {"scheduler": "needs the re-sequencing scheduler (step 4)"}
_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})(.*)$")
_DOCK = re.compile(r"\bD\d\b")


# ---------------------------------------------------------------- fixture
def rebased_snapshot(days: int) -> Path:
    """A copy of the SQLite snapshot with every date moved by `days` whole days."""
    tmp = Path(tempfile.mkdtemp(prefix="setuhaul_replay_")) / "snapshot.db"
    tmp.write_bytes(Path(SNAPSHOT).read_bytes())
    c = sqlite3.connect(tmp)

    def shift(v):
        if not isinstance(v, str):
            return v
        m = _ISO.match(v)
        if not m:
            return v
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            return v
        return (d + timedelta(days=days)).isoformat() + m.group(2)
    c.create_function("shift", 1, shift)
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        c.execute(f'UPDATE "{t}" SET ' + ", ".join(f'"{x}" = shift("{x}")' for x in cols))   # one statement: checks hold
    c.commit()
    c.close()
    return tmp


REPLAY_SQL = """
CREATE SCHEMA setuhaul_replay;
CREATE TABLE setuhaul_replay.clock (id int PRIMARY KEY, off interval NOT NULL);
INSERT INTO setuhaul_replay.clock VALUES (1, '0');
CREATE FUNCTION setuhaul_replay.stamp() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
  IF NEW.source = 'DRIVER_DECLARED' THEN
    NEW.observed_at := now() + (SELECT off FROM setuhaul_replay.clock WHERE id = 1);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER replay_stamp BEFORE INSERT ON setuhaul.eta_signals FOR EACH ROW EXECUTE FUNCTION setuhaul_replay.stamp();
UPDATE setuhaul.facility_config SET shifts =
  '[{"name":"DAY","start":"06:00","end":"22:00"},{"name":"NIGHT","start":"22:00","end":"06:00"}]';
"""


class Fixture:
    def __init__(self, base_dsn: str):
        self.base = base_dsn
        self.tpl = "setuhaul_replay_tpl"
        self.db = "setuhaul_replay"
        self.target = (datetime.now(IST) + timedelta(days=1)).date()
        self.days = (self.target - date.fromisoformat(DATE)).days
        self.snapshot = rebased_snapshot(self.days)
        with psycopg.connect(f"{base_dsn} dbname=postgres", autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {self.db} WITH (FORCE)")
            c.execute(f"DROP DATABASE IF EXISTS {self.tpl} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {self.tpl}")
        migrate(f"{base_dsn} dbname={self.tpl}")

    @property
    def dsn(self) -> str:
        return f"{self.base} dbname={self.db}"

    def at(self, hhmm: str) -> datetime:
        return datetime.fromisoformat(f"{self.target.isoformat()}T{hhmm}:00+05:30")

    def fresh(self, s: Scenario) -> dict:
        with psycopg.connect(f"{self.base} dbname=postgres", autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {self.db} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {self.db} TEMPLATE {self.tpl}")
        info = seed(self.dsn, self.snapshot)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(REPLAY_SQL)
            keep = s.id if re.fullmatch(r"THR0\d\d", s.id) else None
            c.execute("UPDATE setuhaul.chat_threads SET status='CLOSED' WHERE thread_id IS DISTINCT FROM %s", (keep,))
        return info

    def set_clock(self, t: datetime):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("UPDATE setuhaul_replay.clock SET off = %s::timestamptz - now() - interval '60 seconds' WHERE id = 1", (t,))

    def q(self, sql: str, *args):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("SET search_path = setuhaul")
            return c.execute(sql, args).fetchall()


class Clock:
    def __init__(self):
        self.t = datetime.now(IST)

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- one driver on WhatsApp
class Phone:
    def __init__(self, client, app, phone: str):
        self.client, self.app, self.phone = client, app, phone
        self.session = self.thread = self.last_msg = None
        self.sent: list[str] = []

    def say(self, text: str, external_id: str | None = None) -> dict | None:
        mid = external_id or f"wamid.{uuid.uuid4().hex}"
        self.sent.append(mid)
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": self.phone.lstrip("+"), "id": mid, "type": "text", "text": {"body": text}}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(META, body, hashlib.sha256).hexdigest()
        r = self.client.post("/webhooks/whatsapp", content=body,
                             headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        assert r.status_code == 200, r.text
        ev = next((e for e in reversed(self.app.state.inbound) if e.get("external_message_id") == mid), None)
        if ev:
            self.session, self.thread, self.last_msg = ev["session_token"], ev["thread_id"], ev["message_id"]
        return ev


# ---------------------------------------------------------------- judging one turn
def outcome_of(res, fx: Fixture) -> str:
    if res is None:
        return "DEDUPED"
    path = res.path
    if "human-owns-thread" in path:
        return "SILENT"
    if res.escalation_id:
        row = fx.q("SELECT reason_code FROM escalations WHERE escalation_id=%s", res.escalation_id)
        if row:
            return f"ESCALATED:{row[0][0]}"
    last = path[-1] if path else ""
    tools = dict(res.tools)
    if last.startswith("ask:"):
        return "ASK:" + last[4:]
    if last == "ask-shipment":
        return "ASK:shipment_id"
    if "keep-current" in path:
        return "KEEP_CURRENT"
    if "book" in path:
        if tools.get("hold_option") in ("LOST", "OPTION_EXPIRED"):
            return "LOST_REOFFER"
        if tools.get("submit_booking_request") == "OK":
            return "PENDING_REQUEST"
    if path == ["confirmation"]:
        return "ASK:confirmation" if "find_feasible_options" not in tools else "OPTIONS_SHOWN"
    if last == "select":
        return "READ_BACK"
    if "options" in path:
        return "OPTIONS_SHOWN" if tools.get("find_feasible_options") == "OK" else "UNCLEAR"
    if "refused" in path:
        return "REFUSED"
    if "status" in path or "no-live-shipment" in path:
        return "STATUS"
    if "safe-confirmed" in path or "cancel" in path:
        return "ACK"
    return "UNCLEAR"


def subsequence(want: list[str], got: list[str]) -> bool:
    it = iter(got)
    return all(any(w == g for g in it) for w in want)


def shown_now(fx: Fixture, thread: str) -> list[dict]:
    rows = fx.q("""SELECT k.dock_code, lower(o.during), upper(o.during), o.needs_approval FROM shown_options o
                   JOIN docks k USING (dock_id) WHERE o.thread_id=%s
                    AND o.created_at = (SELECT max(created_at) FROM shown_options WHERE thread_id=%s) ORDER BY o.rank""",
                thread, thread)
    return [{"dock": d, "start": a.astimezone(IST), "end": b.astimezone(IST), "approval": n} for d, a, b, n in rows]


def option_checks(checks: dict, opts: list[dict], fx: Fixture) -> list[str]:
    bad = []
    hm = lambda x: fx.at(x)
    for k, v in checks.items():
        if k == "start_not_before":
            bad += [f"{o['dock']} {o['start']:%H:%M} starts before {v}" for o in opts if o["start"] < hm(v)]
        elif k == "dock_in":
            bad += [f"{o['dock']} not in {v}" for o in opts if o["dock"] not in v]
        elif k == "dock_not_in":
            bad += [f"{o['dock']} is excluded" for o in opts if o["dock"] in v]
        elif k == "not_offered":
            bad += [f"{o['dock']} {o['start']:%H:%M} offered" for o in opts if f"{o['dock']} {o['start']:%H:%M}" in v]
        elif k == "min_window_min":
            bad += [f"{o['dock']} window {(o['end'] - o['start']).seconds // 60} < {v}" for o in opts
                    if (o["end"] - o["start"]).total_seconds() < v * 60]
        elif k == "gate_out_by":
            bad += [f"{o['dock']} ends {o['end']:%H:%M} after {v}" for o in opts if o["end"] > hm(v)]
        elif k == "needs_approval_from":
            bad += [f"{o['dock']} {o['start']:%H:%M} lacks approval" for o in opts if o["start"] >= hm(v) and not o["approval"]]
    return bad


# ---------------------------------------------------------------- one scenario
def play(s: Scenario, fx: Fixture, live: bool, guarded=None) -> dict:
    out = {"id": s.id, "group": s.group, "turns": [], "end": {}, "skipped": None, "notes": []}
    why = [SKIP_NEEDS[n] for n in s.needs if n in SKIP_NEEDS]
    if any("parallel_drivers" in t.checks for t in s.turns):
        why.append("parallel race: covered by tests/test_api.py (50 drivers, one slot)")
    if "multi_driver" in s.needs and "active_requests_on_slot" in s.end:
        why.append("same-slot race needs both drivers to be offered one identical slot; covered by tests/test_api.py")
    if why:
        out["skipped"] = "; ".join(why)
        return out

    info = fx.fresh(s)
    clock = Clock()
    clock.t = fx.at(s.now)
    app = create_app(Settings(dsn=fx.dsn, cognito_issuer="https://cognito-idp.ap-south-1.amazonaws.com/replay",
                              cognito_client_ids={"replay"}, session_secret=b"replay-session-secret-0123456789abcdef01",
                              meta_app_secret=META, facilities=FACS, jwks={"keys": []},
                              booking_enabled=bool(s.config.get("booking_enabled")), local_test_harness=True), clock=clock)
    client = TestClient(app)
    if live:
        model = guarded                                    # evals/budget.py GuardedModel (T42)
    else:
        model = FakeModel()
    orch = Orchestrator(app.state.service, app.state.sessions, model, clock=clock)
    phones: dict[str, Phone] = {}
    before = fx.q("SELECT appointment_id, status, is_current FROM appointments ORDER BY 1")
    t0 = s.turns[0]

    def phone(d: str) -> Phone:
        if d not in phones:
            p = fx.q("SELECT phone_e164 FROM drivers WHERE driver_id=%s", d)[0][0]
            phones[d] = Phone(client, app, p)
        return phones[d]

    ctx_match, remap = True, {}
    if t0.ctx.options:                                       # the conversation starts after options were shown
        ph = phone(t0.driver or s.driver)
        fx.set_clock(clock.t)
        ev = ph.say("(earlier in this conversation)")
        p = app.state.sessions.verify(ev["session_token"])
        r = app.state.service.call_tool(p, "find_feasible_options", ev["message_id"], {})
        if r["status"] != "OK":
            out["skipped"] = f"setup could not show options: {r['status']} {r['data']}"
            return out
        flow = app.state.service.call_tool(p, "get_driver_context", ev["message_id"], {})["data"]["flow"]["options"]
        actual = [f"{o['dock_code']} {datetime.fromisoformat(o['start_ts']).astimezone(IST):%H:%M}" for o in flow]
        ctx_match = actual[:len(t0.ctx.options)] == t0.ctx.options
        remap = dict(zip(t0.ctx.options, actual))             # label option -> the option shown at that position
        if not ctx_match:
            out["notes"].append(f"engine offered {actual}, the label assumed {t0.ctx.options}: dock-specific checks N/A")
        if t0.ctx.awaiting is not None:
            oid = r["data"]["options"][min(t0.ctx.awaiting, len(r["data"]["options"]) - 1)]["option_id"]
            sel = app.state.service.call_tool(p, "select_option", ev["message_id"], {"option_id": oid})
            if sel["status"] != "OK":
                out["skipped"] = f"setup could not select: {sel['status']}"
                return out

    for i, t in enumerate(s.turns):
        clock.t = fx.at(t.at or s.now)
        fx.set_clock(clock.t)
        if t.event:
            apply_event(t.event, fx, remap)
        ph = phone(t.driver or s.driver)
        # A duplicate is redelivered with a NEW provider id and the same text, as in the seed (THR001 wa-9001 → THR009
        # wa-9009). A retry with the same id is the trivial case, covered by tests/test_api.py.
        ev = ph.say(t.say)
        if not live:
            model.extractions.append(lambda block, x=t.x: oracle_raw(x, re.findall(r"option_id=(\S+)", block)))
        res = orch.handle(ev) if ev else None
        got = outcome_of(res, fx)
        want = [t.outcome] if isinstance(t.outcome, str) else t.outcome
        tools = [n for n, _ in res.tools] if res else []
        fails = []
        if got not in want:
            fails.append(f"outcome {got}, expected {' / '.join(want)}")
        if t.ops and not subsequence(t.ops, tools):
            fails.append(f"tools {tools} lack {t.ops} in order")
        hard = [f"forbidden tool {x}" for x in t.forbid if x in tools]
        reply = (res.reply or "") if res else ""
        na = []
        for m in t.reply.must:
            if not ctx_match and _DOCK.search(m):
                na.append(f"must '{m}'")
            elif m.lower() not in reply.lower():
                fails.append(f"reply lacks '{m}'")
        for m in t.reply.must_not:
            if m.lower() in reply.lower():
                hard.append(f"reply contains '{m}'")
        shown = shown_now(fx, ph.thread) if got == "OPTIONS_SHOWN" and "find_feasible_options" in tools else []
        if t.checks and shown:
            fails += option_checks(t.checks, shown, fx)
        out["turns"].append({"turn": i, "say": t.say, "outcome": got, "expected": want, "path": res.path if res else [],
                             "tools": tools, "reply": reply, "fails": fails, "hard": hard, "na": na,
                             "extractor": res.extractor if res else None,
                             # the options as the driver saw them (input to the B-AI-3 phrasing ablation)
                             "shown": [{"dock_code": o["dock"], "start": human_time(o["start"]),
                                        "end": human_time(o["end"]), "needs_approval": o["approval"]} for o in shown],
                             "extraction": {k: v for k, v in (res.extraction or {}).items() if v not in (None, False, "UNKNOWN")}
                             if res else None})

    out["end"] = end_checks(s, fx, phones, before)
    out["seed_rejected"] = len(info["rejected"])
    return out


def apply_event(ev: str, fx: Fixture, remap: dict | None = None):
    m = re.fullmatch(r"block (D\d) (\d\d:\d\d)-(\d\d:\d\d) (\w+)", ev)
    if not m:
        raise ValueError(f"unsupported event {ev!r}")
    dock, start = m.group(1), m.group(2)
    a, b = fx.at(m.group(2)), fx.at(m.group(3))
    target = (remap or {}).get(f"{dock} {start}")
    if target and target != f"{dock} {start}":                  # the label meant "the option shown here"
        dock, start = target.split()
        a, b = fx.at(start), fx.at(start) + (b - a)
    dock_id = f"DOCK-JAI-{dock}"
    with psycopg.connect(fx.dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO dock_events VALUES (%s,%s,%s,tstzrange(%s,%s,'[)'),%s,'replay',now())",
                  (f"EV-{uuid.uuid4().hex[:8]}", dock_id, m.group(4), a, b, "replay event"))
        c.execute("""INSERT INTO dock_occupancy (dock_id,facility_id,during,kind,ref_id,created_by)
                     VALUES (%s,'FAC-JAI-01',tstzrange(%s,%s,'[)'),'BLOCK','replay','replay')""", (dock_id, a, b))


def end_checks(s: Scenario, fx: Fixture, phones: dict, before) -> dict:
    res = {}
    threads = [p.thread for p in phones.values() if p.thread]
    drivers = list(phones)
    for k, v in s.end.items():
        if k == "escalation":
            got = [r[0] for r in fx.q("SELECT reason_code FROM escalations WHERE thread_id = ANY(%s)", threads)]
            res[k] = (v in got, got)
        elif k == "escalation_mode":
            got = [r[0] for r in fx.q("SELECT mode FROM escalations WHERE thread_id = ANY(%s) ORDER BY created_at", threads)]
            res[k] = (bool(got) and got[0] == v, got)
        elif k == "escalations":
            got = dict(Counter(r[0] for r in fx.q("SELECT reason_code FROM escalations WHERE thread_id = ANY(%s)", threads)))
            res[k] = ((sum(got.values()) == v) if isinstance(v, int) else got == v, got)
        elif k == "human_active":
            got = [r[0] for r in fx.q("SELECT human_active FROM chat_threads WHERE thread_id = ANY(%s)", threads)]
            res[k] = (all(g is v for g in got) and bool(got), got)
        elif k == "exceptions":
            got = fx.q("SELECT count(*) FROM driver_exceptions WHERE driver_id = ANY(%s)", drivers)[0][0]
            res[k] = (got == v, got)
        elif k == "agent_replies":
            got = fx.q("SELECT count(*) FROM chat_messages WHERE thread_id = ANY(%s) AND sender_type='AGENT'", threads)[0][0]
            res[k] = (got == v, got)
        elif k == "appointments_changed":
            after = fx.q("SELECT appointment_id, status, is_current FROM appointments ORDER BY 1")
            res[k] = ((after == before) == (v == 0), len(set(after) ^ set(before)))
        elif k == "priority":
            got = {sid: fx.q("SELECT priority_code FROM shipments WHERE shipment_id=%s", sid)[0][0] for sid in v}
            res[k] = (got == v, got)
        elif k == "holds_created":
            got = fx.q("SELECT count(*) FROM dock_occupancy WHERE kind='HOLD'")[0][0]
            res[k] = (got == v, got)
        elif k == "no_overlapping_occupancy":
            got = fx.q("""SELECT count(*) FROM dock_occupancy a JOIN dock_occupancy b ON a.dock_id=b.dock_id
                          AND a.occupancy_id < b.occupancy_id AND a.during && b.during WHERE a.active AND b.active""")[0][0]
            res[k] = (got == 0, got)
        elif k == "slot_holders":
            ok, got = True, {}
            for slot, ids in v.items():
                dock, hhmm = slot.split()
                rows = [r[0] for r in fx.q("""SELECT a.appointment_id FROM appointments a JOIN docks d USING (dock_id)
                         WHERE d.dock_code=%s AND d.facility_id='FAC-JAI-01' AND lower(a.during)=%s
                           AND a.status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""", dock, fx.at(hhmm))]
                got[slot] = rows
                ok &= sorted(rows) == sorted(ids)
            res[k] = (ok, got)
        elif k == "active_requests_on_slot":
            ok, got = True, {}
            for slot, n in v.items():
                dock, hhmm = slot.split()
                c = fx.q("""SELECT count(*) FROM appointments a JOIN docks d USING (dock_id) WHERE d.dock_code=%s
                            AND d.facility_id='FAC-JAI-01' AND lower(a.during)=%s AND a.status='PENDING_CONFIRMATION'""",
                         dock, fx.at(hhmm))[0][0]
                got[slot] = c
                ok &= c == n
            res[k] = (ok, got)
        else:
            res[k] = (None, "not checked by this runner")
    return {k: {"pass": v[0], "got": v[1]} for k, v in res.items()}


# ---------------------------------------------------------------- run
def run(version: str = "v4", live: bool = False, only: list[str] | None = None, base_dsn: str | None = None,
        budget=None, cache=None, estimate_only: bool = False) -> dict:
    import os
    scenarios, ds = load(version)
    guarded, est, stopped = None, None, None
    if live:
        from evals.budget import Budget, BudgetExceeded, Cache, GuardedModel, print_estimate
        from evals.providers import model_from_env
        budget, cache = budget or Budget(), cache or Cache()
        raw = model_from_env()
        turns = sum(len(x.turns) for x in scenarios if not only or x.id in only)
        # upper bound: one extraction per turn; the cache can only lower it (blocks depend on live state)
        est = print_estimate("replay " + version, raw.extract_model, turns, 0, "extract", budget)
        if estimate_only:
            return {"estimate_only": True, "estimated_usd": est}
        try:
            budget.authorise(est)
        except BudgetExceeded as e:
            raise SystemExit(f"refused: {e}")
        guarded = GuardedModel(raw, budget, cache)
    fx = Fixture(base_dsn or os.environ.get("SETUHAUL_TEST_DSN", "host=localhost port=55432 user=postgres"))
    rows = []
    t_start = time.monotonic()
    for s in scenarios:
        if only and s.id not in only:
            continue
        if stopped:
            break
        try:
            r = play(s, fx, live, guarded)
        except Exception as e:                                 # a crash fails that scenario, not the run
            if type(e).__name__ == "BudgetExceeded":                # stop the whole run: nothing more is sent
                stopped = str(e)
                print("STOPPED:", stopped, flush=True)
                break
            r = {"id": s.id, "group": s.group, "turns": [], "end": {}, "skipped": None, "error": f"{type(e).__name__}: {e}"}
        r["pass"] = r.get("skipped") is None and not r.get("error") and \
            all(not t["fails"] and not t["hard"] for t in r["turns"]) and \
            all(v["pass"] is not False for v in r["end"].values())
        rows.append(r)
        status = "SKIP" if r.get("skipped") else ("PASS" if r["pass"] else "FAIL")
        print(f"{s.id:7} {status}", (r.get("skipped") or r.get("error") or
                                     "; ".join(f for t in r["turns"] for f in t["fails"] + t["hard"]) or
                                     "; ".join(f"end {k}: {v['got']}" for k, v in r["end"].items() if v["pass"] is False))[:220],
              flush=True)
    played = [r for r in rows if not r.get("skipped")]
    turns = [t for r in played for t in r["turns"]]
    by_group = defaultdict(lambda: [0, 0])
    for r in played:
        by_group[r["group"]][0] += r["pass"]
        by_group[r["group"]][1] += 1
    summary = {
        "dataset": ds, "model": "live" if live else "scripted (labelled extraction)", "target_date": fx.target.isoformat(),
        "scenarios": len(rows), "skipped": len(rows) - len(played), "played": len(played),
        "scenarios_passed": sum(r["pass"] for r in played),
        "turns": len(turns), "turn_outcome_correct": sum(t["outcome"] in t["expected"] for t in turns),
        "hard_gate_violations": sum(len(t["hard"]) for t in turns),
        "errors": sum(1 for r in played if r.get("error")),
        "by_group": {g: f"{p}/{n}" for g, (p, n) in sorted(by_group.items())},
        "seconds": round(time.monotonic() - t_start),
        "cost": guarded.budget.report(est) if guarded else {"spent_usd": 0, "basis": "no paid calls"},
        "stopped": stopped,
    }
    return {"summary": summary, "scenarios": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["scripted", "live"], default="scripted")
    ap.add_argument("--version", default="v4")
    ap.add_argument("--only", nargs="*")
    from evals.budget import add_args, from_args
    add_args(ap)
    a = ap.parse_args()
    budget, cache = from_args(a)
    res = run(a.version, a.model == "live", a.only, None, budget, cache, a.estimate_only)
    if res.get("estimate_only"):
        return
    out = Path(__file__).parent / "results" / f"replay_{a.version}_{a.model}_{datetime.now():%Y%m%d_%H%M}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(res["summary"], indent=2))
    print("wrote", out)


if __name__ == "__main__":
    main()
