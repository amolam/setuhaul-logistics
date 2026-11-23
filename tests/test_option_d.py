"""Option D activation on the synthetic pilot (migration 0026; owner approval OWNER-APPROVAL-20261123-OPTION-D).

- the booking kill switch: the database refuses new booking commitments while engaged, whatever the application says;
  reductions still work; only an ops_lead (console) or the owner operates it, audited;
- owner-approved gate exceptions: only the three accepted pairs, pilot only, at most 30 days, append-only, written only
  by the pipeline login; the start-up check accepts one only under its own condition (T33-P: PostgreSQL 17.9);
- the manifest the pipeline records: the functions accept it only for their own code;
- the writer covers every gate with PASS evidence or an owner-approved exception, or writes nothing.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from setuhaul.api.inbound import (EXCEPTION_CODES, REQUIRED_GATES, BookingGateError, SqsFifoQueue, database_fingerprint,
                                  deployment_id_for, exception_problem, manifest_fingerprints, manifest_from_database)
from setuhaul.ops import gate_evidence as ge
from setuhaul.ops.kill_switch import statements as owner_kill_statements
from test_inbound_delivery import URL, FakeSqs, app_db, booking_app, manifest, write_evidence
from test_move_appointment import (S1, S2, SCH, appt, at, confirmed, move, move_row, sch, staff,  # noqa: F401
                                   store)
from conftest import as_role

ROOT = Path(__file__).resolve().parents[1]
PILOT = "pilot-ap-southeast-2"
REF = "OWNER-APPROVAL-20261123-OPTION-D"


@pytest.fixture
def killed(sch):
    """The switch engaged by an ops_lead for one test, and always disengaged afterwards (it is global)."""
    with psycopg.connect(sch, autocommit=True) as c:
        with staff(c, ("ops_lead",), (SCH,), who="staff:t-kill-lead") as s:
            assert s.execute("SELECT set_booking_kill_switch(true, 'staff:t-kill-lead', 'test: incident drill')").fetchone()[0]
    try:
        yield sch
    finally:
        with psycopg.connect(sch, autocommit=True) as c:
            c.execute("UPDATE setuhaul.booking_kill_switch SET engaged=false, changed_by='test', reason='test cleanup'")


def server_version(dsn_) -> int:
    with psycopg.connect(dsn_) as c:
        return int(c.execute("SELECT current_setting('server_version_num')").fetchone()[0])


# ================================================================ the kill switch
def test_the_kill_switch_refuses_every_new_booking_commitment(killed):
    with psycopg.connect(killed, autocommit=True) as c:
        with pytest.raises(psycopg.Error, match="BOOKING_KILLED"):
            with c.transaction():
                confirmed(c)                                        # a new claim on dock time + a new appointment
        n = c.execute("SELECT count(*) FROM setuhaul.booking_events WHERE action='BOOKING_KILL_ENGAGED' "
                      "AND principal='staff:t-kill-lead' AND principal_role='ops_lead'").fetchone()[0]
        assert n >= 1


def test_moves_and_confirmations_are_refused_but_reductions_still_work(sch):
    with psycopg.connect(sch, autocommit=True) as c:
        t = confirmed(c, dock=S1, start_h=30)
        other = confirmed(c, dock=S2, start_h=31)
        pid = store(c, [move_row(t, S1, at(32))])
        c.execute("UPDATE setuhaul.booking_kill_switch SET engaged=true, changed_by='test', reason='test engage'")
        try:
            with pytest.raises(psycopg.Error, match="BOOKING_KILLED"):
                move(c, pid, t["aid"])                              # changing a confirmed time
            assert appt(c, t["aid"])[0] == "CONFIRMED"
            # reductions: cancel one and release its claim
            c.execute("UPDATE setuhaul.appointments SET status='CANCELLED', is_current=false WHERE appointment_id=%s",
                      (other["aid"],))
            c.execute("UPDATE setuhaul.dock_occupancy SET active=false WHERE ref_id=%s", (other["aid"],))
            with pytest.raises(psycopg.Error, match="BOOKING_KILLED"):      # ...but not confirm it again
                c.execute("UPDATE setuhaul.appointments SET status='CONFIRMED' WHERE appointment_id=%s", (other["aid"],))
            with pytest.raises(psycopg.Error, match="BOOKING_KILLED"):      # ...nor re-activate its dock claim
                c.execute("UPDATE setuhaul.dock_occupancy SET active=true WHERE ref_id=%s", (other["aid"],))
            c.execute("UPDATE setuhaul.appointments SET status='CANCELLED', is_current=false WHERE appointment_id=%s",
                      (t["aid"],))
            # a new appointment request (no dock claim yet) is a commitment too
            with pytest.raises(psycopg.Error, match="BOOKING_KILLED"):
                with c.transaction():
                    c.execute("""INSERT INTO setuhaul.appointments (appointment_id, shipment_id, dock_id, during, status,
                                        booking_source, pending_expires_at)
                                 VALUES (%s, %s, %s, tstzrange(now() + interval '50 h', now() + interval '51 h'),
                                         'PENDING_CONFIRMATION', 'DRIVER_CHAT', now() + interval '10 min')""",
                              (f"APT-K-{uuid.uuid4().hex[:8]}", other["sid"], S2))
            # an operational dock block is not a booking commitment
            c.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id, facility_id, during, kind, ref_id, created_by)
                         VALUES (%s, %s, tstzrange(now() + interval '60 h', now() + interval '61 h'), 'BLOCK', 'blk-k', 'test')""",
                      (S2, SCH))
        finally:
            c.execute("UPDATE setuhaul.booking_kill_switch SET engaged=false, changed_by='test', reason='test cleanup'")
        confirmed(c, dock=S2, start_h=40)                           # disengaged: booking works again


def test_only_an_ops_lead_or_the_owner_operates_the_kill_switch(sch):
    with psycopg.connect(sch, autocommit=True) as c:
        for roles in (("planner",), ("operations",)):
            with pytest.raises(psycopg.Error, match="ROLE_NOT_ALLOWED|FORBIDDEN"):
                with staff(c, roles, (SCH,), who="staff:t-x") as s:
                    s.execute("SELECT set_booking_kill_switch(true, 'staff:t-x', 'not allowed')")
        with pytest.raises(psycopg.Error, match="REASON_REQUIRED"):
            with staff(c, ("ops_lead",), (SCH,), who="staff:t-y") as s:
                s.execute("SELECT set_booking_kill_switch(true, 'staff:t-y', '')")
        with pytest.raises(psycopg.Error):                          # no staff identity: nothing runs
            with as_role(c, "setuhaul_console", SCH) as s:
                s.execute("SELECT set_booking_kill_switch(true, 'staff:t-z', 'no identity')")
        for role in ("setuhaul_agent", "setuhaul_ingest", "setuhaul_system", "setuhaul_console", "setuhaul_feasibility"):
            with pytest.raises(psycopg.Error) as e:
                with as_role(c, role, SCH) as s:
                    s.execute("UPDATE booking_kill_switch SET engaged = true")
            assert e.value.sqlstate == "42501", role
            with pytest.raises(psycopg.Error):
                with as_role(c, role, SCH) as s:
                    s.execute("SELECT set_booking_kill_switch(true, 'x', 'by an application role')")
        with pytest.raises(psycopg.Error, match="KILL_SWITCH_SINGLE_ROW"):
            c.execute("INSERT INTO setuhaul.booking_kill_switch VALUES (1, true, now(), 'x', 'y')")
        assert c.execute("SELECT engaged FROM setuhaul.booking_kill_switch").fetchone()[0] is False


def test_the_owner_cli_changes_the_switch_and_audits_it_in_one_transaction(sch):
    with psycopg.connect(sch, autocommit=True) as c:
        with c.transaction():
            for s in owner_kill_statements(True, "owner: drill"):
                c.execute(s)
        assert c.execute("SELECT engaged, changed_by FROM setuhaul.booking_kill_switch").fetchone() == \
            (True, "owner:kill-switch-cli")
        with c.transaction():
            for s in owner_kill_statements(False, "owner: drill over"):
                c.execute(s)
        rows = c.execute("""SELECT action, before_state->>'engaged', after_state->>'engaged' FROM setuhaul.booking_events
                             WHERE principal='owner:kill-switch-cli' ORDER BY event_id DESC LIMIT 2""").fetchall()
        assert rows == [("BOOKING_KILL_DISENGAGED", "true", "false"), ("BOOKING_KILL_ENGAGED", "false", "true")]
    with pytest.raises(ValueError):
        owner_kill_statements(True, "no")


# ================================================================ owner-approved exceptions
def insert_exception(dsn_, env, dep, gate, code, fp="0" * 64, days="7 days", ref=REF, role="setuhaul_deploy_evidence"):
    with psycopg.connect(dsn_, autocommit=True) as c:
        with c.transaction():
            c.execute(f"SET LOCAL ROLE {role}")
            c.execute("""INSERT INTO setuhaul.deployment_exceptions (environment, deployment_id, gate, exception_code,
                                approval_ref, reason, config_fingerprint, expires_at)
                         VALUES (%s,%s,%s,%s,%s,'accepted by the owner for the pilot',%s, now() + %s::interval)""",
                      (env, dep, gate, code, ref, fp, days))


def test_only_the_three_accepted_exceptions_for_the_pilot_can_be_recorded(dsn):
    dep = f"dep-{uuid.uuid4().hex[:8]}"
    for g, code in EXCEPTION_CODES.items():
        insert_exception(dsn, PILOT, dep, g, code)
    for g, code, env, days, ref, msg in (
            ("AWS_QUEUE", "T33-P", PILOT, "7 days", REF, "check"),                     # not an accepted gate
            ("AURORA", "NO-LIVE-DRIFT-DETECTION", PILOT, "7 days", REF, "check"),      # wrong code for the gate
            ("AURORA", "T33-P", "prod-ap-south-1", "7 days", REF, "check"),            # never production
            ("AURORA", "T33-P", PILOT, "31 days", REF, "check"),                       # at most 30 days
            ("AURORA", "T33-P", PILOT, "7 days", "yes", "check")):                     # the owner's written reference
        with pytest.raises(psycopg.Error, match=msg):
            insert_exception(dsn, env, dep, g, code, days=days, ref=ref)
    for role in ("setuhaul_agent", "setuhaul_console", "setuhaul_ingest", "setuhaul_system", "setuhaul_feasibility"):
        with pytest.raises(psycopg.Error) as e:
            insert_exception(dsn, PILOT, dep, "AURORA", "T33-P", role=role)
        assert e.value.sqlstate == "42501", role
    with psycopg.connect(dsn, autocommit=True) as c:
        for sql in ("UPDATE setuhaul.deployment_exceptions SET expires_at = now() + interval '20 days'",
                    "DELETE FROM setuhaul.deployment_exceptions", "TRUNCATE setuhaul.deployment_exceptions",
                    "DELETE FROM setuhaul.deployment_manifests"):
            with pytest.raises(psycopg.Error, match="APPEND_ONLY"):
                c.execute(sql)
        who = c.execute("SELECT DISTINCT recorded_by FROM setuhaul.deployment_exceptions WHERE deployment_id=%s",
                        (dep,)).fetchall()
    assert who == [("postgres",)]


def test_exception_rules_hold_even_for_a_row_that_got_past_the_database():
    fp = "f" * 64
    ok = lambda g, env=PILOT, ver=170009, code=None, fresh=True, rowfp=fp: exception_problem(  # noqa: E731
        g, (code or EXCEPTION_CODES[g], fresh, rowfp, REF), fp, env, ver)
    assert ok("AURORA") is None and ok("WORKLOAD_IDENTITY") is None and ok("OUTBOUND_DELIVERY") is None
    assert "17.9" in ok("AURORA", ver=180003) and "17.9" in ok("AURORA", ver=170008)
    assert "pilot" in ok("AURORA", env="prod-ap-south-1") and "pilot" in ok("WORKLOAD_IDENTITY", env="staging")
    assert ok("AURORA", fresh=False) == "expired"
    assert "different configuration" in ok("OUTBOUND_DELIVERY", rowfp="e" * 64)
    assert "not an exception" in ok("AWS_QUEUE", code="T33-P")


def test_the_switch_starts_with_pass_evidence_plus_the_accepted_exceptions(dsn):
    q = SqsFifoQueue(URL, FakeSqs())
    man = manifest(env=PILOT)
    dep, fps = deployment_id_for(man), {"AWS_QUEUE": q.fingerprint(), "AURORA": database_fingerprint(app_db(dsn)),
                                        **manifest_fingerprints(man)}
    for g in ("AWS_QUEUE", "RECOVERY_WORKER", "BOOKING_WORKFLOW"):
        write_evidence(dsn, PILOT, dep, g, fp=fps[g])
    with pytest.raises(BookingGateError, match="WORKLOAD_IDENTITY: no evidence"):
        booking_app(dsn, q, env=PILOT, man=man)
    insert_exception(dsn, PILOT, dep, "WORKLOAD_IDENTITY", EXCEPTION_CODES["WORKLOAD_IDENTITY"], fp="e" * 64)
    with pytest.raises(BookingGateError, match="WORKLOAD_IDENTITY: no evidence for this deployment and exception "
                                               "NO-LIVE-DRIFT-DETECTION does not hold \\(recorded for a different"):
        booking_app(dsn, q, env=PILOT, man=man)                    # bound to the configuration, like evidence
    for g in ("WORKLOAD_IDENTITY", "OUTBOUND_DELIVERY", "AURORA"):
        insert_exception(dsn, PILOT, dep, g, EXCEPTION_CODES[g], fp=fps[g])
    if 170009 <= server_version(dsn) < 180000:                     # CI: postgres:17.9, the pilot's engine
        app = booking_app(dsn, q, env=PILOT, man=man)
        assert set(app.state.gate_exceptions) == {"AURORA", "WORKLOAD_IDENTITY", "OUTBOUND_DELIVERY"}
        assert all(REF in v for v in app.state.gate_exceptions.values())
    else:                                                          # PostgreSQL 18: T33-P does not apply
        with pytest.raises(BookingGateError, match="T33-P covers PostgreSQL 17.9"):
            booking_app(dsn, q, env=PILOT, man=man)
        write_evidence(dsn, PILOT, dep, "AURORA", fp=fps["AURORA"])
        app = booking_app(dsn, q, env=PILOT, man=man)
        assert set(app.state.gate_exceptions) == {"WORKLOAD_IDENTITY", "OUTBOUND_DELIVERY"}
    from fastapi.testclient import TestClient
    h = TestClient(app).get("/health").json()
    assert h["booking_enabled"] is True and h["booking_kill_switch"] == "disengaged"
    assert "WORKLOAD_IDENTITY" in h["gate_exceptions"]
    # an exception never applies to another environment, even with the same deployment id
    with pytest.raises(BookingGateError):
        booking_app(dsn, q, env="pilot-other", man={**man, "environment": "pilot-other"})


# ================================================================ the recorded manifest
def record(dsn_, man, env=None, dep=None, art=None):
    env = env or man["environment"]
    with psycopg.connect(dsn_, autocommit=True) as c:
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
            c.execute("""INSERT INTO setuhaul.deployment_manifests (environment, deployment_id, artifact_sha256, manifest)
                         VALUES (%s,%s,%s,%s::jsonb)""",
                      (env, dep or deployment_id_for(man), art or man["gates"]["RECOVERY_WORKER"]["artifact_sha256"],
                       json.dumps(man)))


def test_the_functions_accept_only_the_newest_manifest_for_their_own_code(dsn):
    env = f"pilot-m{uuid.uuid4().hex[:6]}"
    with pytest.raises(BookingGateError, match="no deployment manifest"):
        manifest_from_database(app_db(dsn), env, "1" * 64)
    old = manifest(env=env, RECOVERY_WORKER={"artifact_sha256": "a" * 64})
    record(dsn, old)
    assert manifest_from_database(app_db(dsn), env, "a" * 64) == old
    new = manifest(env=env, RECOVERY_WORKER={"artifact_sha256": "b" * 64})
    with pytest.raises(BookingGateError, match="other code"):     # new code deployed, its manifest not yet recorded
        manifest_from_database(app_db(dsn), env, "b" * 64)
    record(dsn, new)
    assert manifest_from_database(app_db(dsn), env, "b" * 64) == new
    with pytest.raises(BookingGateError, match="other code"):     # the old code no longer matches the newest
        manifest_from_database(app_db(dsn), env, "a" * 64)
    forged = manifest(env=env, RECOVERY_WORKER={"artifact_sha256": "c" * 64})
    record(dsn, forged, dep="m-" + "0" * 40)                       # an id that is not the content's
    with pytest.raises(BookingGateError, match="does not match its deployment id"):
        manifest_from_database(app_db(dsn), env, "c" * 64)
    with pytest.raises(psycopg.Error, match="check"):              # the row must name its own artifact
        record(dsn, forged, art="d" * 64)


# ================================================================ the pipeline's activation step
def live_report():
    raw = (ROOT / "docs" / "release" / "evidence" / "step6_gates.json").read_bytes()
    return json.loads(raw), raw


SINK = {"SETUHAUL_NOTIFICATION_MODE": "test_sink", "SETUHAUL_WHATSAPP_ENABLED": "false"}


def test_activation_writes_manifest_evidence_and_exceptions_or_nothing(dsn):
    report, raw = live_report()
    man = manifest(env=PILOT)
    fps = {g: uuid.uuid4().hex * 2 for g in REQUIRED_GATES}
    uri = "https://github.com/amolam/setuhaul-logistics/blob/abc/docs/release/evidence/step6_gates.json"

    def run(**kw):
        args = dict(report=report, raw_report=raw, manifest=man, environment=PILOT, artifact_uri=uri, approval_ref=REF,
                    fingerprints=fps, server_version_num=170009, outbound_env=SINK)
        args.update(kw)
        with psycopg.connect(dsn) as c:
            with c.transaction():
                c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
                return ge.activate(c, **args)

    for bad, msg in (({"server_version_num": 180003}, "AURORA: T33-P does not hold"),
                     ({"outbound_env": {**SINK, "SETUHAUL_WHATSAPP_ENABLED": "true"}}, "OUTBOUND_DELIVERY"),
                     ({"approval_ref": "approved"}, "written approval reference"),
                     ({"environment": "prod-ap-south-1", "manifest": manifest(env="prod-ap-south-1")}, "never to production")):
        with pytest.raises(ge.EvidenceRefused, match=msg):
            run(**bad)
    wi = json.loads(json.dumps(report))
    wi["gates"]["WORKLOAD_IDENTITY"]["not_shown"].append("something else")
    with pytest.raises(ge.EvidenceRefused, match="WORKLOAD_IDENTITY: only drift detection"):
        run(report=wi)
    with psycopg.connect(dsn) as c:                                # nothing was written by any refused run
        assert c.execute("SELECT count(*) FROM setuhaul.deployment_manifests WHERE environment=%s", (PILOT,)).fetchone()[0] == 0
    out = run()
    assert out["deployment_id"] == deployment_id_for(man) and out["approval_ref"] == REF
    passed = {g for g, v in report["gates"].items() if v["status"] == "PASS"}
    assert set(out["evidence"]) == passed
    assert out["exceptions"] == {g: EXCEPTION_CODES[g] for g in REQUIRED_GATES if g not in passed}
    with psycopg.connect(dsn) as c:
        ev = c.execute("SELECT gate FROM setuhaul.deployment_evidence WHERE deployment_id=%s", (out["deployment_id"],)).fetchall()
        ex = c.execute("""SELECT gate, approval_ref, expires_at - recorded_at <= interval '7 days 1 min'
                            FROM setuhaul.deployment_exceptions WHERE deployment_id=%s""", (out["deployment_id"],)).fetchall()
    assert {g for (g,) in ev} == passed and all(r[1] == REF and r[2] for r in ex)
    assert not {g for (g,) in ev} & {g for g, *_ in ex}            # an exception is never written as evidence
