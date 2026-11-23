"""Phase G against PostgreSQL: the optimizer in the driver path and the console, migration 0027 (runs, turn metrics,
evaluation runs, the global aggregate), role and facility scoping, PII-free telemetry, and concurrent booking of the
same recommended slot (the database decides; the optimizer never books). Runs in Data API mode too."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from setuhaul.agent.models import FakeModel
from setuhaul.agent.orchestrator import Orchestrator
from setuhaul.api.app import Settings, create_app
from setuhaul.ops import telemetry
from test_api import (CLIENT, GGN, H, ISSUER, IST, JAI, META_SECRET, SESSION_SECRET, Driver, add_driver, app, client,  # noqa: F401
                      dsn, keys, staff_token)
from test_orchestrator import exd, in_90

PHONE_DIGITS = __import__("re").compile(r"\+?\d{10,}")


@pytest.fixture(scope="module")
def ranked_app(dsn, keys):
    """The same database with the owner's optimizer-ranking switch ON (it is off by default)."""
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True, optimizer_ranking=True))


def one(dsn, sql, *a):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, a).fetchone()


# ---------------------------------------------------------------- console: recommendations
def test_planner_gets_structured_feasible_recommendations_and_the_run_is_recorded(client, dsn, keys):
    _, _, (sid,) = add_driver(dsn)
    r = client.get(f"/console/shipments/{sid}/recommendations", headers=H(staff_token(keys)))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "RECOMMENDATION" and b["not_a_booking"] is True and b["facility_id"] == JAI
    assert b["solver_status"] == "OPTIMAL" and b["options"] and all(o["feasible"] for o in b["options"])
    assert len(b["constraints_evaluated"]) == 12 and b["input_fingerprint"] and b["data_as_of"]
    assert r.headers["x-correlation-id"] == b["correlation_id"] == b["request_id"]
    assert GGN not in r.text and not PHONE_DIGITS.search(r.text)
    row = one(dsn, """SELECT caller, solver_status, options_count, facility_id FROM setuhaul.optimization_runs
                      WHERE request_id=%s""", b["request_id"])
    assert row == ("console", "OPTIMAL", len(b["options"]), JAI)


def test_recommendations_scope_and_roles(client, dsn, keys):
    _, _, (ggn_sid,) = add_driver(dsn, facility=GGN)
    _, _, (jai_sid,) = add_driver(dsn)
    assert client.get(f"/console/shipments/{ggn_sid}/recommendations", headers=H(staff_token(keys))).status_code == 404
    assert client.get("/console/shipments/NOPE/recommendations", headers=H(staff_token(keys))).status_code == 404
    ops = H(staff_token(keys, groups=("operations",)))
    assert client.get(f"/console/shipments/{jai_sid}/recommendations", headers=ops).status_code == 403
    lead = H(staff_token(keys, groups=("ops_lead",), facilities=(JAI, GGN)))
    g = client.get(f"/console/shipments/{ggn_sid}/recommendations", headers=lead)
    assert g.status_code == 200 and all(o["dock_id"].startswith("DOCK-GGN") for o in g.json()["options"])
    bad = client.get(f"/console/shipments/{jai_sid}/recommendations", headers=lead,
                     params={"earliest": "2026-12-02T15:00:00+05:30", "latest": "2026-12-02T12:00:00+05:30"})
    assert bad.status_code == 422
    assert client.get(f"/console/shipments/{jai_sid}/recommendations").status_code == 401


# ---------------------------------------------------------------- console: observability
def test_observability_sections_scope_and_roles(client, dsn, keys):
    r = client.get("/console/observability", headers=H(staff_token(keys, groups=("ops_lead",))))
    assert r.status_code == 200, r.text
    b = r.json()
    assert {"overview", "agent", "optimization", "reliability", "evaluation", "data_label"} <= set(b)
    assert "SYNTHETIC" in b["data_label"] and b["facilities"] == [JAI]
    assert b["reliability"]["queue_dlq"]["status"] == "unavailable"
    assert "model_usage" in b["agent"] and "idempotency" in b["reliability"]
    assert not PHONE_DIGITS.search(r.text) and GGN not in json.dumps(b["overview"])
    assert client.get("/console/observability", headers=H(staff_token(keys, groups=("operations",)))).status_code == 403
    assert client.get("/console/observability", params={"facility": GGN}, headers=H(staff_token(keys))).status_code == 403
    me = client.get("/console/me", headers=H(staff_token(keys, groups=("planner",)))).json()["permissions"]
    assert me["view_observability"] and me["view_recommendations"]


def test_correlation_id_is_echoed_or_replaced(client, keys):
    cid = str(uuid.uuid4())
    assert client.get("/health", headers={"X-Correlation-Id": cid}).headers["x-correlation-id"] == cid
    other = client.get("/health", headers={"X-Correlation-Id": "<script>"}).headers["x-correlation-id"]
    assert uuid.UUID(other) and other != "<script>"


# ---------------------------------------------------------------- driver path with the optimizer ranking
def test_driver_options_come_from_the_optimizer_and_the_turn_is_measured(ranked_app, dsn, caplog):
    c = TestClient(ranked_app)
    _, phone, (sid,) = add_driver(dsn)
    d = Driver(c, phone)
    orch = Orchestrator(ranked_app.state.service, ranked_app.state.sessions,
                        FakeModel(extractions=[exd(delay_kind="ARRIVAL_TIME", declared_eta_local=in_90())]))
    with caplog.at_level(logging.INFO, logger="setuhaul.telemetry"):
        res = orch.handle(d.say("Traffic, reaching in 90 minutes, any slot?"))
    assert "options" in res.path
    p = ranked_app.state.sessions.verify(d.session)
    shown = ranked_app.state.service.call_tool(p, "get_driver_context", 0, {})["data"]["flow"]["options"]
    run = one(dsn, """SELECT solver_status, top_dock_id, top_start, request_id, fallback_used FROM setuhaul.optimization_runs
                      WHERE shipment_id=%s AND caller='agent' ORDER BY created_at DESC LIMIT 1""", sid)
    assert run and run[0] == "OPTIMAL" and run[4] is False
    dock = one(dsn, "SELECT dock_code FROM setuhaul.docks WHERE dock_id=%s", run[1])[0]
    assert (shown[0]["dock_code"], datetime.fromisoformat(shown[0]["start_ts"])) == (dock, run[2])
    assert run[3] == res.correlation_id                                  # one id: turn, optimizer run, logs
    turn = one(dsn, "SELECT facility_id, extractor, tools, turn_ms FROM setuhaul.agent_turn_metrics WHERE turn_ref=%s",
               res.correlation_id)
    assert turn[0] == JAI and turn[1] == "model" and turn[3] >= 0
    assert {t["tool"] for t in turn[2]} >= {"get_driver_context", "record_driver_report", "find_feasible_options"}
    assert all(set(t) == {"tool", "status", "ms"} for t in turn[2])
    logged = [json.loads(r.getMessage()) for r in caplog.records if r.name == "setuhaul.telemetry"]
    assert {x["event"] for x in logged} >= {"optimizer.run", "agent.turn"}
    assert all(x["correlation_id"] == res.correlation_id for x in logged)
    blob = json.dumps(logged)
    assert phone not in blob and phone[1:] not in blob and "Traffic" not in blob and "driver_id" not in blob


def test_optimizer_failure_falls_back_to_the_engine_options(ranked_app, dsn, monkeypatch):
    from setuhaul.domain import optimizer as om

    def boom(self, req, now, res, t0):
        raise RuntimeError("controlled failure injection")
    monkeypatch.setattr(om.SlotOptimizer, "_run", boom)
    c = TestClient(ranked_app)
    _, phone, (sid,) = add_driver(dsn)
    d = Driver(c, phone)
    orch = Orchestrator(ranked_app.state.service, ranked_app.state.sessions,
                        FakeModel(extractions=[exd(delay_kind="ARRIVAL_TIME", declared_eta_local=in_90())]))
    res = orch.handle(d.say("Traffic, reaching in 90 minutes"))
    assert "options" in res.path                                         # the driver still gets the engine's options
    assert one(dsn, """SELECT solver_status, fallback_used FROM setuhaul.optimization_runs WHERE shipment_id=%s""",
               sid) == ("ERROR", True)


# ---------------------------------------------------------------- 0027 functions: validation, scope, append-only
def _as(conn, role, scope):
    conn.execute(f"SET LOCAL ROLE {role}")
    conn.execute("SET LOCAL search_path = setuhaul")
    conn.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))


def test_record_functions_refuse_cross_facility_and_extra_fields(dsn):
    _, _, (sid,) = add_driver(dsn)
    base = {"request_id": "r1", "facility_id": JAI, "shipment_id": sid, "caller": "eval", "engine": "slot-optimizer-v1",
            "solver_status": "OPTIMAL", "outcome_code": "OPTIONS", "recommended_action": "OFFER", "options_count": 1,
            "rejected_count": 0, "candidates_evaluated": 3, "duration_ms": 2, "top_dock_id": "DOCK-JAI-D1"}
    with psycopg.connect(dsn) as c:
        with c.transaction():
            _as(c, "setuhaul_feasibility", JAI)
            assert c.execute("SELECT record_optimization_run(%s)", (Jsonb(base),)).fetchone()[0]
        for bad, code in (({**base, "top_dock_id": "DOCK-GGN-D1"}, "CROSS_FACILITY"),
                          ({**base, "facility_id": GGN}, "FORBIDDEN"),
                          ({**base, "solver_status": "GUESS"}, "check")):
            with pytest.raises(psycopg.Error, match=code), c.transaction():
                _as(c, "setuhaul_feasibility", JAI)
                c.execute("SELECT record_optimization_run(%s)", (Jsonb(bad),))
        with pytest.raises(psycopg.Error, match="BAD_TURN"), c.transaction():
            _as(c, "setuhaul_feasibility", JAI)
            c.execute("SELECT record_agent_turn(%s)", (Jsonb({"turn_ref": str(uuid.uuid4()), "facility_id": JAI,
                      "extractor": "model", "turn_ms": 5, "tools": [{"tool": "find_feasible_options", "status": "OK",
                                                                       "ms": 3, "text": "+919876543210"}]}),))
        with pytest.raises(psycopg.Error, match="permission denied"), c.transaction():
            _as(c, "setuhaul_agent", JAI)                                 # the agent role gains nothing in 0027
            c.execute("SELECT record_optimization_run(%s)", (Jsonb(base),))
        with pytest.raises(psycopg.Error, match="APPEND_ONLY"), c.transaction():
            c.execute("UPDATE setuhaul.optimization_runs SET duration_ms = 0")
        with c.transaction():                                            # console sees only its scope (RLS)
            _as(c, "setuhaul_console", GGN)
            assert c.execute("SELECT count(*) FROM optimization_runs WHERE shipment_id=%s", (sid,)).fetchone()[0] == 0


def test_evaluation_runs_are_written_only_by_the_system_role(dsn):
    body = {"suite": "phase-g-test", "mode": "scripted", "dataset_version": "t", "total": 2, "passed": 2, "failed": 0,
            "skipped": 0, "summary": {"x": 1}, "started_at": "2026-11-23T10:00:00+00:00",
            "finished_at": "2026-11-23T10:01:00+00:00"}
    with psycopg.connect(dsn) as c:
        with c.transaction():
            _as(c, "setuhaul_system", "")
            c.execute("SELECT record_evaluation_run(%s)", (Jsonb(body),))
        with pytest.raises(psycopg.Error, match="permission denied"), c.transaction():
            _as(c, "setuhaul_console", JAI)
            c.execute("SELECT record_evaluation_run(%s)", (Jsonb(body),))
        with pytest.raises(psycopg.Error), c.transaction():
            _as(c, "setuhaul_system", "")
            c.execute("SELECT record_evaluation_run(%s)", (Jsonb({**body, "passed": 1}),))   # totals must add up
        with c.transaction():
            _as(c, "setuhaul_console", JAI)
            g = c.execute("SELECT observability_global(now() - interval '1 hour')").fetchone()[0]
            assert set(g) == {"since", "model_usage", "idempotency", "approval_callbacks"}


def test_telemetry_never_logs_personal_fields():
    rec = telemetry.emit("x.y", None, phone="+919876543210", driver_id="DRV1", text="hello", body="b", note="n",
                         facility_id=JAI, n=3, long="z" * 500)
    assert set(rec) == {"event", "correlation_id", "facility_id", "n", "long"} and len(rec["long"]) == 120


# ---------------------------------------------------------------- concurrency: same recommended slot, one winner
def test_two_drivers_racing_for_the_same_recommended_slot_one_wins(ranked_app, dsn):
    eta = datetime.now(IST).replace(second=0, microsecond=0) + timedelta(minutes=95)
    c = TestClient(ranked_app)
    drivers = []
    for _ in range(2):
        _, phone, (sid,) = add_driver(dsn, eta_at=eta)
        d = Driver(c, phone)
        d.say("Running late, slot please")
        assert d.tool("record_driver_report", message_id=d.last_msg, declared_eta=eta.isoformat(), eta_precision="PRECISE",
                      delay_reason="TRAFFIC")["status"] == "OK"
        opts = d.tool("find_feasible_options")["data"]["options"]
        drivers.append((d, opts[0]))
    (d1, o1), (d2, o2) = drivers
    assert (o1["dock_code"], o1["start"]) == (o2["dock_code"], o2["start"])     # the same best slot for both
    for d, o in drivers:
        d.tool("select_option", option_id=o["option_id"])
        d.say("haan ji, kar do")
        assert d.tool("confirm_selection", message_id=d.last_msg)["data"]["verdict"] == "YES"
    out, barrier = {}, threading.Barrier(2)

    def go(i, d, o):
        barrier.wait()
        out[i] = d.tool("hold_option", option_id=o["option_id"])
    ts = [threading.Thread(target=go, args=(i, d, o)) for i, (d, o) in enumerate(drivers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    held = [r for r in out.values() if r["status"] == "OK" and r["data"].get("status") == "HELD"]
    assert len(held) == 1, out                                          # the database decided: exactly one hold
    n = one(dsn, """SELECT count(*) FROM setuhaul.dock_occupancy a JOIN setuhaul.dock_occupancy b
                     ON a.dock_id = b.dock_id AND a.occupancy_id < b.occupancy_id AND a.active AND b.active
                    AND a.during && b.during""")[0]
    assert n == 0


# ---------------------------------------------------------------- v0.2.1 open item: a pick after the offer expired
@pytest.mark.parametrize("scripted", [True, False])
def test_a_pick_after_the_offer_expired_is_answered_with_fresh_slots(app, client, dsn, scripted):
    _, phone, (sid,) = add_driver(dsn)
    d = Driver(client, phone)
    model = FakeModel(extractions=[exd(delay_kind="ARRIVAL_TIME", declared_eta_local=in_90())]) if scripted else None
    orch = Orchestrator(app.state.service, app.state.sessions, model)
    say = "Traffic, reaching in 90 minutes" if scripted else f"Traffic, reaching by {in_90()}"
    assert "options" in orch.handle(d.say(say)).path
    p = app.state.sessions.verify(d.session)
    first = app.state.service.call_tool(p, "get_driver_context", 0, {})["data"]["flow"]["options"]
    with psycopg.connect(dsn, autocommit=True) as c:                     # the 5-minute offer runs out
        c.execute("UPDATE setuhaul.shown_options SET expires_at = now() - interval '1 second' WHERE thread_id=%s",
                  (d.thread,))
    if scripted:
        model.extractions.append(exd(intent="BOOK", selected_option_id=first[0]["option_id"]))
    r = orch.handle(d.say("1"))
    assert "options-expired" in r.path and "unclear" not in r.path, r.path
    assert r.reply.startswith("Those slots have expired") and "did not understand" not in r.reply
    fresh = app.state.service.call_tool(p, "get_driver_context", 0, {})["data"]["flow"]["options"]
    assert fresh and {o["option_id"] for o in fresh}.isdisjoint({o["option_id"] for o in first})
    assert "hold_option" not in [t for t, _ in r.tools]                  # nothing was booked from the stale pick
