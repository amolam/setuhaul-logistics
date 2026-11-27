"""Recovery Release A (migration 0033): the four defects found in the 24 Nov 2026 Option P deployment (D1 trigger
allow-list, D2 decider classification, D3 automatic revoke, D4 /health), the pipeline-shell defect found while fixing
D3 (D5), and the narrowly scoped recovery exception to Option P's no-chaining rule.

Database tests run on the Option P test database (guard and deployment identity enforced, as on the pilot), imported
from tests/test_provisional_authorization.py so every helper means exactly what it means there."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import BASE_DSN, _pg_available, _seed
from setuhaul.api.app import Settings, create_app
from setuhaul.ops import evidence_triggers as ET
from setuhaul.ops import gate_evidence as GE
from test_api import CLIENT, ISSUER, META_SECRET, SESSION_SECRET, keys  # noqa: F401
from test_booking_evidence_guard import declare, evidence, manifest, record
from test_move_appointment import SCH, confirmed, sch, staff  # noqa: F401
from test_provisional_authorization import (SHA, SLOT, as_login, auth_row, book, c, complete, dsn,  # noqa: F401
                                            env_name, events, fresh_ref, issue, login_name, new_release, refused,
                                            verdict)

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "migrations"
UUID_DECIDER = "staff:0f3c2a1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b"


# ------------------------------------------------------------------------------------------------ helpers
def recover(conn, d, recovery_of, ref=None, minutes=60, run="run-r"):
    ref = ref or fresh_ref()
    with conn.transaction():
        conn.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        return conn.execute("SELECT setuhaul.issue_provisional_authorization(%s,%s,%s,%s,%s,%s,%s)",
                            (d["env"], d["dep"], SHA, ref, run, minutes, recovery_of)).fetchone()[0]


def revoke(conn, env, reason="test: renewal failed"):
    with conn.transaction():
        conn.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        return conn.execute("SELECT setuhaul.revoke_provisional_authorization(%s,%s)", (env, reason)).fetchone()[0]


def auth_id(conn, d):
    return conn.execute("SELECT authorization_id FROM setuhaul.provisional_authorizations "
                        "WHERE environment=%s AND deployment_id=%s", (d["env"], d["dep"])).fetchone()[0]


def revoked_release(conn, env, ref=None):
    """The 24 Nov situation: a complete predecessor, a release whose window was issued and then REVOKED."""
    complete(conn, env)
    d1 = new_release(conn, env)
    issue(conn, d1, ref=ref)
    revoke(conn, env)
    return d1, auth_id(conn, d1)


def insert_auth(conn, d, prev_dep, issued, expires, status="ISSUED", closed=None, recovery_of=None):
    """A row written directly (the test superuser), to reach states the clock cannot: a lapsed or an old window."""
    return conn.execute("""INSERT INTO setuhaul.provisional_authorizations (environment, deployment_id, code_sha256,
                             predecessor_deployment, release_sha, approval_ref, pipeline_run, issued_by, issued_at,
                             expires_at, status, closed_at, closed_by, close_reason, recovery_of)
                           VALUES (%s,%s,%s,%s,%s,%s,'run-x','test', now() + %s::interval, now() + %s::interval, %s,
                                   CASE WHEN %s::text IS NULL THEN NULL ELSE now() + %s::interval END,
                                   CASE WHEN %s::text IS NULL THEN NULL ELSE 'test' END,
                                   CASE WHEN %s::text IS NULL THEN NULL ELSE 'test: direct row' END, %s)
                           RETURNING authorization_id""",
                        (d["env"], d["dep"], d["art"], prev_dep, SHA, fresh_ref(), issued, expires, status,
                         closed, closed, closed, closed, recovery_of)).fetchone()[0]


def other_dep(env):
    return {"env": env, "dep": "m-" + uuid.uuid4().hex + uuid.uuid4().hex[:8], "art": "a" * 64}


# ================================================================ the recovery exception (0033)
def test_a_revoked_release_is_recovered_once_by_a_new_release_under_a_new_approval(c, sch):
    env = env_name()
    d1, a1 = revoked_release(c, env)
    d2 = new_release(c, env)
    refused(lambda: issue(c, d2), "has no complete evidence")                       # the default rule stands
    out = recover(c, d2, a1)
    assert out["recovery_of"] == a1 and out["predecessor_deployment"] == d1["dep"]
    assert book(c, d2)                                                               # the new release's own window
    refused(lambda: book(c, d1), "DEPLOYMENT_SUPERSEDED")
    evidence(c, d2, "BOOKING_WORKFLOW")                                              # its own smoke-backed evidence
    assert auth_row(c, d2)[0] == "CONSUMED" and events(c, d2) == ["ISSUED", "CONSUMED"]
    detail = c.execute("""SELECT detail FROM setuhaul.provisional_authorization_events e
                          JOIN setuhaul.provisional_authorizations a USING (authorization_id)
                          WHERE a.deployment_id=%s AND e.action='ISSUED'""", (d2["dep"],)).fetchone()[0]
    assert detail["recovery_of"] == a1
    assert events(c, d1) == ["ISSUED", "REVOKED"]                                     # the revoked history is untouched


def test_recovery_is_refused_when_the_previous_release_is_complete(c):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    other_env = env_name()
    _, a = revoked_release(c, other_env)
    refused(lambda: recover(c, d, a), "RECOVERY_NOT_APPLICABLE")


def test_only_a_revoked_window_of_the_previous_release_in_this_environment_qualifies(c):
    env = env_name()
    d1, a1 = revoked_release(c, env)
    d2 = new_release(c, env)
    _, foreign = revoked_release(c, env_name())
    refused(lambda: recover(c, d2, foreign), "RECOVERY_MISMATCH")                     # another environment's
    refused(lambda: recover(c, d2, 10 ** 9), "RECOVERY_MISMATCH")                     # no such authorization
    # an EXPIRED window (lapsed, nobody acted) of the previous release: not a deliberate stop
    env2 = env_name()
    p = complete(c, env2)
    e1 = new_release(c, env2)
    lapsed = insert_auth(c, e1, p["dep"], "-3 hours", "-2 hours")
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        c.execute("SELECT setuhaul.expire_provisional_authorizations()")
    e2 = new_release(c, env2)
    # 0045: still refused without the owner's recorded approval for exactly this authorization
    refused(lambda: recover(c, e2, lapsed), "recoverable only with the owner's recorded approval")
    assert a1 and d1


def test_a_recovery_can_be_recovered_once_more_but_never_a_third_hop(c):
    """0049 (owner, 27 Nov 2026): exactly one additional hop; the third is refused, and the default rule still is."""
    env = env_name()
    _, a1 = revoked_release(c, env)
    d2 = new_release(c, env)
    a2 = recover(c, d2, a1)["authorization_id"]
    revoke(c, env)
    d3 = new_release(c, env)
    refused(lambda: issue(c, d3), "has no complete evidence")                         # the default rule is unchanged
    a3 = recover(c, d3, a2)["authorization_id"]                                         # the one additional hop
    assert a3
    revoke(c, env)
    d4 = new_release(c, env)
    refused(lambda: recover(c, d4, a3), "already a second recovery; no third hop")
    refused(lambda: issue(c, d4), "has no complete evidence")


def test_a_revoked_window_is_recovered_at_most_once(c):
    env = env_name()
    d1, a1 = revoked_release(c, env)
    d2 = new_release(c, env)
    recover(c, d2, a1)
    revoke(c, env)
    record(c, d1)                                     # d1 recorded again: it is the previous release once more
    d3 = new_release(c, env)
    refused(lambda: recover(c, d3, a1), "RECOVERY_USED")
    with pytest.raises(psycopg.errors.UniqueViolation):                 # the index holds even for a direct write
        insert_auth(c, other_dep(env), d1["dep"], "-1 minute", "+1 hour", "REVOKED", "0 seconds", recovery_of=a1)


def test_an_old_revocation_or_a_broken_audit_trail_does_not_qualify(c):
    env = env_name()
    p = complete(c, env)
    d1 = new_release(c, env)
    old = insert_auth(c, d1, p["dep"], "-8 days", "-7 days -23 hours", "REVOKED", "-7 days -22 hours")
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, old), "more than 7 days ago")
    env2 = env_name()
    p2 = complete(c, env2)
    e1 = new_release(c, env2)
    direct = insert_auth(c, e1, p2["dep"], "-10 minutes", "+50 minutes", "REVOKED", "-5 minutes")   # one event only
    e2 = new_release(c, env2)
    refused(lambda: recover(c, e2, direct), "audit trail of authorization")


def test_the_previous_release_may_lack_only_booking_workflow(c):
    env = env_name()
    p = complete(c, env)
    d1 = new_release(c, env, skip=("AWS_QUEUE",))                # its queue evidence is missing
    a1 = insert_auth(c, d1, p["dep"], "-1 minute", "+59 minutes")
    revoke(c, env)
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, a1), "lacks more than BOOKING_WORKFLOW")


def test_every_other_rule_still_applies_to_a_recovery(c):
    env = env_name()
    used = fresh_ref()
    _, a1 = revoked_release(c, env, ref=used)
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, a1, minutes=121), "1 to 120 minutes")            # hard maximum
    refused(lambda: recover(c, d2, a1, ref=used), "PROVISIONAL_APPROVAL_USED")      # the revoked window's approval
    # eight issued in this environment in 24 h (0044; the revoked ones count): a recovery is refused like any ninth
    env2 = env_name()
    _, b1 = revoked_release(c, env2)
    for _ in range(7):
        insert_auth(c, other_dep(env2), "m-" + "1" * 40, "-1 hour", "-30 minutes", "REVOKED", "-40 minutes")
    e2 = new_release(c, env2)
    refused(lambda: recover(c, e2, b1), "PROVISIONAL_RATE_LIMIT")
    # production and invalid names: refused before anything else
    for name in ("production-ap-southeast-2", "PROD", "not a name"):
        refused(lambda: recover(c, {"env": name, "dep": "m-" + "0" * 40}, a1), "PROVISIONAL_REFUSED")


def test_a_recovered_window_keeps_kill_switch_precedence_and_m2_identity(c, sch):
    env = env_name()
    d1, a1 = revoked_release(c, env)
    d2 = new_release(c, env)
    recover(c, d2, a1)
    refused(lambda: book(c, d2, login=login_name(d1)), "IDENTITY_MISMATCH")          # the revoked release's login
    c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = true, changed_by = 'test', reason = 'drill in recovery'")
    try:
        refused(lambda: book(c, d2), "BOOKING_KILLED")
    finally:
        c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = false, changed_by = 'test', reason = 'drill over'")
    assert book(c, d2, role="console") and book(c, d2, role="agent")
    assert verdict(c, d2, offset="61 minutes") is not None                              # expires like any window


def test_recovery_of_is_immutable_and_the_audit_append_only(c):
    env = env_name()
    _, a1 = revoked_release(c, env)
    d2 = new_release(c, env)
    a2 = recover(c, d2, a1)["authorization_id"]
    refused(lambda: c.execute("UPDATE setuhaul.provisional_authorizations SET recovery_of = NULL "
                              "WHERE authorization_id = %s", (a2,)), "PROVISIONAL_IMMUTABLE")
    refused(lambda: c.execute("DELETE FROM setuhaul.provisional_authorizations WHERE authorization_id = %s", (a2,)),
            "APPEND_ONLY")
    refused(lambda: c.execute("UPDATE setuhaul.provisional_authorization_events SET principal='x' "
                              "WHERE authorization_id = %s", (a2,)), "APPEND_ONLY")


def test_concurrent_recoveries_of_one_revoked_window_exactly_one_wins(dsn):  # noqa: F811
    with psycopg.connect(dsn, autocommit=True) as c0:
        env = env_name()
        _, a1 = revoked_release(c0, env)
        d2 = new_release(c0, env)
    results: list[str] = []
    barrier = threading.Barrier(4)

    def go(i):
        with psycopg.connect(dsn, autocommit=True) as cx:
            barrier.wait()
            try:
                recover(cx, d2, a1, run=f"run-{i}")
                results.append("ok")
            except psycopg.Error as e:
                results.append(str(e).split(":")[0])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert results.count("ok") == 1, results
    assert all(r in ("ok", "RECOVERY_USED", "PROVISIONAL_OPEN", "PROVISIONAL_ALREADY_ISSUED") for r in results), results


def test_concurrent_consumers_of_a_recovered_window_consume_exactly_once(dsn):  # noqa: F811
    with psycopg.connect(dsn, autocommit=True) as c0:
        env = env_name()
        _, a1 = revoked_release(c0, env)
        d2 = new_release(c0, env)
        recover(c0, d2, a1)
    barrier, errors = threading.Barrier(3), []

    def go():
        try:
            with psycopg.connect(dsn, autocommit=True) as cx:
                barrier.wait()
                evidence(cx, d2, "BOOKING_WORKFLOW")
        except psycopg.Error as e:                                 # pragma: no cover - reported below
            errors.append(str(e))

    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    with psycopg.connect(dsn) as cx:
        assert not errors, errors
        assert auth_row(cx, d2)[0] == "CONSUMED" and events(cx, d2) == ["ISSUED", "CONSUMED"]


def test_the_new_issuer_has_exactly_its_caller_and_the_old_entry_point_still_means_no_recovery(c):
    roles = ("setuhaul_agent", "setuhaul_console", "setuhaul_ingest", "setuhaul_feasibility", "setuhaul_system",
             "setuhaul_deploy_evidence")
    for fn, allowed in {"issue_provisional_authorization(text,text,text,text,text,integer,bigint)":
                        {"setuhaul_deploy_evidence"},
                        "booking_authorization_status(text,text,text)": {"setuhaul_ingest"},
                        "decider_kind_of(text,text)": set()}.items():
        got = {r for r in roles if c.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                             (r, f"setuhaul.{fn}")).fetchone()[0]}
        assert got == allowed, (fn, got)
        assert c.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (f"setuhaul.{fn}",)).fetchone()[0] \
            is False
    env = env_name()
    revoked_release(c, env)
    refused(lambda: issue(c, new_release(c, env)), "has no complete evidence")         # 6 arguments: no recovery


# ================================================================ D2: genuine console decisions versus drills
@pytest.mark.parametrize("decided_by, typ, kind", [
    (UUID_DECIDER, "HUMAN", "console_user"),                              # the console: staff:<Cognito sub>
    ("staff:step6-drill-planner", "HUMAN", "drill_staff"),
    ("staff:option-d-smoke-planner", "HUMAN", "drill_staff"),
    ("staff:0f3c2a1e5b6d4e7f8a9b0c1d2e3f4a5b", "HUMAN", "drill_staff"),  # not a UUID: a named identity
    ("planner@example.test", "HUMAN", "console_user"),
    ("system:expiry", "SYSTEM", "system"),
    (None, None, None)])
def test_the_decider_kind_rule(c, decided_by, typ, kind):
    assert c.execute("SELECT setuhaul.decider_kind_of(%s, %s)", (decided_by, typ)).fetchone()[0] == kind


def test_no_drill_smoke_or_probe_script_uses_a_uuid_shaped_staff_identity():
    """The rule depends on it: every scripted identity is a name, so only the console produces staff:<UUID>."""
    pat = re.compile(r"staff:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    for f in list((ROOT / "scripts").rglob("*.py")) + list((ROOT / "setuhaul" / "ops").rglob("*.py")):
        assert not pat.search(f.read_text(encoding="utf-8")), f
    assert 'f"staff:{p.sub}"' in (ROOT / "setuhaul" / "api" / "service.py").read_text(encoding="utf-8")


def _approval(conn, decided_by, approval_id=None, typ="HUMAN"):
    u = uuid.uuid4().hex[:8]
    aid = approval_id or str(uuid.uuid4())
    conn.execute("SET search_path = setuhaul")
    try:
        conn.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)",
                     (f"SYN-DRV-{u}", f"+999{uuid.uuid4().int % 10**9:09d}"))
        veh, sid, apt = f"SYN-VEH-{u}", f"SYN-SHP-{u}", f"APT-{u}"
        conn.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}"))
        conn.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,
                        destination_facility_id,load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,
                        current_status) VALUES (%s,%s,'CAR1',%s,%s,'FAC-JAI-01',10000,45,now()-interval '3 hours',
                        now()+interval '2 hours','IN_TRANSIT')""", (sid, f"ORD-{u}", f"SYN-DRV-{u}", veh))
        conn.execute("""INSERT INTO approval_requests (approval_id,kind,facility_id,shipment_id,proposed_action,evidence,
                        status,expires_at,decided_by,decided_at,decision_note,task_token,decided_by_type,decided_by_role,
                        outcome_reason) VALUES (%s,'STANDARD','FAC-JAI-01',%s,'{}','{}','EXPIRED',now()+interval '1 hour',
                        %s,now(),'n','tok',%s,'planner','EXPIRED')""", (aid, sid, decided_by, typ))
    finally:
        conn.execute("RESET search_path")
    return aid


def _kinds(dsn_, ids):
    with psycopg.connect(dsn_) as cx:
        cx.execute("SET ROLE setuhaul_deploy_evidence")
        cx.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01', true)")
        return {str(r[0]): r[1] for r in cx.execute(
            "SELECT approval_id, decider_kind FROM setuhaul.approval_execution_evidence(%s::uuid[])", (ids,))}


def _attestation_block() -> str:
    t = (MIG / "0033_recovery_and_decider_provenance.sql").read_text(encoding="utf-8")
    return t[t.index("-- BEGIN historical-attestation"):t.index("-- END historical-attestation")]


def test_console_and_drill_decisions_are_told_apart_end_to_end(dsn, c):  # noqa: F811
    console, drill = _approval(c, UUID_DECIDER), _approval(c, "staff:step6-drill-planner")
    assert _kinds(dsn, [console, drill]) == {console: "console_user", drill: "drill_staff"}


def test_the_historical_correction_is_written_once_only_for_that_console_decision_and_changes_no_approval(dsn, c):  # noqa: F811
    block = "SET search_path = setuhaul, public;\n" + _attestation_block()
    the_id = "29ffac03-d8bd-4dac-b35c-66b0f8ebf24b"
    count = "SELECT count(*) FROM setuhaul.approval_decider_attestations WHERE approval_id=%s"
    c.execute(block)                                                     # no such approval here: nothing written
    assert c.execute(count, (the_id,)).fetchone()[0] == 0
    with c.transaction():                                                # that id decided by a drill: never attested
        _approval(c, "staff:step6-drill-planner", approval_id=the_id)
        c.execute(block)
        assert c.execute(count, (the_id,)).fetchone()[0] == 0
        raise psycopg.Rollback()
    _approval(c, UUID_DECIDER, approval_id=the_id, typ="HUMAN")
    with c.transaction():
        c.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01', true)")
        before = c.execute("SELECT to_jsonb(r) FROM setuhaul.approval_requests r WHERE approval_id=%s",
                           (the_id,)).fetchone()[0]
    c.execute(block)
    c.execute(block)                                                     # idempotent
    rows = c.execute("""SELECT decider_kind, previous_kind, evidence_ref FROM setuhaul.approval_decider_attestations
                        WHERE approval_id=%s""", (the_id,)).fetchall()
    assert rows == [("console_user", "drill_staff", rows[0][2])] and "owner_attribution_20261123.json" in rows[0][2]
    with c.transaction():
        c.execute("SELECT set_config('app.facility_scope', 'FAC-JAI-01', true)")
        after = c.execute("SELECT to_jsonb(r) FROM setuhaul.approval_requests r WHERE approval_id=%s",
                          (the_id,)).fetchone()[0]
    assert after == before                                               # provenance kept: the approval is untouched
    assert _kinds(dsn, [the_id]) == {the_id: "console_user"}
    refused(lambda: c.execute("UPDATE setuhaul.approval_decider_attestations SET decider_kind='drill_staff' "
                              "WHERE approval_id=%s", (the_id,)), "APPEND_ONLY")
    refused(lambda: c.execute("DELETE FROM setuhaul.approval_decider_attestations WHERE approval_id=%s", (the_id,)),
            "APPEND_ONLY")


# ================================================================ 0033 on top of the live (0032) schema
def test_0033_applies_on_a_0032_database_with_a_revoked_window_and_keeps_every_earlier_fingerprint(tmp_path):
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    from setuhaul.db.migrate import migrate
    pinned = {"0028": "6939beefa773eb27cf6e4d14509882ba45a42521a0fbfefd27ec128cde5df177",
              "0029": "11e1f54b6f25b72d0e49ad513bca5b7ffcc2c60cba0bd983b61c95f09e06d91e",
              "0030": "8f91c62c2da5a109bde2957c1fc4ca0344c9505a85771b716a1f6117692d8dde",
              "0031": "6573237664d95f9e9ad3183221f565dbcb515afae1eda7e2c6d4391dabff5c2d",
              "0032": "b80e4c2470b08131a11808f9c639805b356772414cada749f06330a400671375"}   # = the live pilot's
    for p in MIG.glob("00[23][0-9]_*.sql"):
        v = p.name[:4]
        if v in pinned:
            assert hashlib.sha256(p.read_bytes()).hexdigest() == pinned[v], p.name
    old = tmp_path / "upto0032"
    old.mkdir()
    for p in MIG.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if p.name[:4] <= "0032":
            shutil.copy(p, old / p.name)
    db = "setuhaul_build_upgrade_0033"
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as cx:
        cx.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        cx.execute(f"CREATE DATABASE {db}")
    d = f"{BASE_DSN} dbname={db}"
    try:
        assert migrate(d, old)[-1] == "0032_provisional_authorization_and_identity"
        _seed(d)
        with psycopg.connect(d, autocommit=True) as cx:
            cx.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'test: as on the pilot'")
            cx.execute("UPDATE setuhaul.booking_evidence_guard SET identity_enforced = true, reason = 'test: M2'")
            env = env_name()
            d1, a1 = revoked_release(cx, env)                       # the 24 Nov state, written by 0032's functions
            the_id = _approval(cx, UUID_DECIDER, approval_id="29ffac03-d8bd-4dac-b35c-66b0f8ebf24b")
        applied = migrate(d)                                    # 0033, then any later forward migration (0034)
        assert applied[0] == "0033_recovery_and_decider_provenance" and all(v > "0033" for v in applied[1:])
        with psycopg.connect(d, autocommit=True) as cx:
            assert dict(cx.execute("SELECT left(version,4), sha256 FROM setuhaul_meta.schema_migrations "
                                   "WHERE version >= '0028' AND version < '0033'").fetchall()) == pinned
            assert events(cx, d1) == ["ISSUED", "REVOKED"]                      # history preserved
            assert cx.execute("SELECT recovery_of FROM setuhaul.provisional_authorizations WHERE authorization_id=%s",
                              (a1,)).fetchone()[0] is None
            assert _kinds(d, [the_id]) == {the_id: "console_user"}
            d2 = new_release(cx, env)
            assert recover(cx, d2, a1)["recovery_of"] == a1
            assert ET.trigger_problems(cx.execute(ET.QUERY).fetchall()) == []
        assert migrate(d) == []                                                  # nothing left; hashes verified
    finally:
        with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as cx:
            cx.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


# ================================================================ D1: the complete trigger set
def test_the_expected_trigger_set_is_exactly_what_a_migrated_database_has(c):
    rows = c.execute(ET.QUERY).fetchall()
    assert ET.trigger_problems(rows) == []
    assert {(t, n) for t, n, *_ in rows} == {(t, n) for t, want in ET.EXPECTED.items() for n in want}
    assert len(ET.EXPECTED["deployment_evidence"]) == 4                  # 0013, 0014, 0031, 0032 (was: exactly two)


def test_missing_disabled_unexpected_and_misconfigured_triggers_are_each_reported(c):
    rows = c.execute(ET.QUERY).fetchall()
    drop = [r for r in rows if r[1] != "ab_provisional_consumption"]
    assert ET.trigger_problems(drop) == ["deployment_evidence.ab_provisional_consumption: missing"]
    off = [(t, n, "D" if n == "aa_booking_kill_switch" and t == "appointments" else e, f, ty) for t, n, e, f, ty in rows]
    assert ET.trigger_problems(off) == ["appointments.aa_booking_kill_switch: disabled (tgenabled 'D')"]
    swapped = [(t, n, e, "trg_activation_append_only" if n == "deployment_evidence_fresh" else f, ty)
               for t, n, e, f, ty in rows]
    assert ET.trigger_problems(swapped) == ["deployment_evidence.deployment_evidence_fresh: runs "
                                            "trg_activation_append_only, expected trg_deployment_evidence_fresh"]
    after = [(t, n, e, f, 5 if n == "aa_evidence_requires_guard" and t == "deployment_manifests" else ty)
             for t, n, e, f, ty in rows]
    assert ET.trigger_problems(after) == ["deployment_manifests.aa_evidence_requires_guard: timing/events 5, "
                                          "expected 7"]
    with c.transaction():                                                # a real extra trigger, rolled back
        c.execute("CREATE TRIGGER zz_extra BEFORE INSERT ON setuhaul.deployment_evidence "
                  "FOR EACH ROW EXECUTE FUNCTION setuhaul.trg_deployment_evidence_fresh()")
        c.execute("ALTER TABLE setuhaul.deployment_exceptions DISABLE TRIGGER deployment_exceptions_clock")
        live = ET.trigger_problems(c.execute(ET.QUERY).fetchall())
        assert sorted(live) == ["deployment_evidence.zz_extra: unexpected trigger (trg_deployment_evidence_fresh)",
                                "deployment_exceptions.deployment_exceptions_clock: disabled (tgenabled 'D')"]
        raise psycopg.Rollback()
    assert ET.trigger_problems(c.execute(ET.QUERY).fetchall()) == []


def test_the_aurora_gate_uses_the_complete_set_not_two_names():
    src = (ROOT / "scripts" / "step6_gate_evidence.py").read_text(encoding="utf-8")
    assert "both evidence triggers enabled" not in src and '"deployment_evidence_append_only": "O"' not in src
    # 27 Nov fix plan: migration-aware (the ledger decides whether 0048's trigger is required; tests/test_gate_triggers_0048.py)
    assert "trigger_problems(triggers, applied)" in src and "EVIDENCE_TRIGGER_QUERY" in src


# ================================================================ D3: automatic revoke, never a silent open window
def test_revocation_needs_no_artifact_uri_and_recording_still_does(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(GE, "revoke_from_pipeline", lambda env, reason: calls.append((env, reason)) or 0)
    assert GE.main(["--environment", "pilot-ap-southeast-2", "--revoke-provisional", "pipeline run 1-1 failed"]) == 0
    assert calls == [("pilot-ap-southeast-2", "pipeline run 1-1 failed")]
    with pytest.raises(SystemExit):
        GE.main(["--report", "r.json", "--manifest", "m.json", "--activate", "OWNER-APPROVAL-20261124-X1Y"])
    assert "--artifact-uri is required" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        GE.main(["--report", "r.json", "--manifest", "m.json", "--artifact-uri", "https://x/y", "--recovery-of", "1",
                 "--activate", "OWNER-APPROVAL-20261124-X1Y"])
    assert "--recovery-of applies only to --provisional" in capsys.readouterr().err


def test_the_open_window_guard_exits_nonzero_only_when_a_window_is_open(monkeypatch):
    monkeypatch.setattr(GE, "open_provisional_from_pipeline", lambda env: {"open": True, "authorization_id": 7})
    assert GE.main(["--environment", "pilot-x", "--assert-no-open-provisional"]) == 1
    monkeypatch.setattr(GE, "open_provisional_from_pipeline", lambda env: None)
    assert GE.main(["--environment", "pilot-x", "--assert-no-open-provisional"]) == 0


def _steps(job: str) -> list[str]:
    t = (ROOT / ".github" / "workflows" / "activate-booking.yml").read_text(encoding="utf-8")
    body = t[t.index(f"\n  {job}:\n"):]
    nxt = re.search(r"\n  [a-z-]+:\n", body[3:])
    body = body[:nxt.start() + 3] if nxt else body
    return ["      - " + s for s in body.split("\n      - ")[1:]]


def test_after_issuance_every_failure_or_cancellation_reaches_the_revoke_and_the_open_window_guard():
    steps = _steps("activate")
    names = [re.search(r"name: (.*)", s).group(1) if "name:" in s.split("\n")[0] else s.split("\n")[0] for s in steps]
    issue_at = next(i for i, n in enumerate(names) if n.startswith("Record the manifest"))
    revoke_at = next(i for i, n in enumerate(names) if "revoke the provisional authorization" in n)
    guard_at = next(i for i, n in enumerate(names) if "no provisional authorization may stay open" in n)
    assert issue_at < revoke_at < guard_at
    for i in (revoke_at, guard_at):
        cond = re.search(r"\n\s+if: (.*)", steps[i]).group(1)
        assert cond == "(failure() || cancelled()) && env.MODE == 'provisional'", cond
    # every step that can fail after the issuance commit, and the issuance step itself, is a failure point those
    # conditions cover: none of them may hide its failure
    for s in steps[issue_at:revoke_at]:
        assert "continue-on-error" not in s
    assert "--artifact-uri" not in steps[revoke_at] and "--revoke-provisional" in steps[revoke_at]
    assert "--assert-no-open-provisional" in steps[guard_at] and "exit 1" in steps[guard_at]
    t = (ROOT / ".github" / "workflows" / "activate-booking.yml").read_text(encoding="utf-8")
    assert "defaults:\n  run:\n    shell: bash\n" in t                  # D5: -eo pipefail, so `python | tee` fails


def test_the_ci_workflow_also_runs_with_pipefail():
    t = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "defaults:\n  run:\n    shell: bash\n" in t


def test_the_pipeline_revoke_and_guard_run_end_to_end_through_the_data_api(c, monkeypatch):
    """After issuance, whatever fails: the workflow's exact revoke command closes the window (REVOKED, audited) and the
    guard then finds nothing open. Before the revoke, the guard reports the open window."""
    from data_api_emulator import EmulatedRdsData
    import boto3
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    em = EmulatedRdsData(c.info.dsn)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: em)
    monkeypatch.setenv("SETUHAUL_DB_CLUSTER_ARN", "arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul")
    monkeypatch.setenv("SETUHAUL_DB_SECRET_ARNS", json.dumps({"deploy_evidence": "arn:secret:deploy_evidence"}))
    monkeypatch.setenv("SETUHAUL_DB_NAME", c.info.dbname)
    assert GE.main(["--environment", env, "--assert-no-open-provisional"]) == 1          # open: the run must fail
    assert GE.main(["--environment", env, "--revoke-provisional",
                    "pipeline run 9-1 failed after issuance"]) == 0
    assert auth_row(c, d)[0] == "REVOKED" and events(c, d) == ["ISSUED", "REVOKED"]
    assert GE.main(["--environment", env, "--assert-no-open-provisional"]) == 0
    assert GE.main(["--environment", env, "--revoke-provisional", "a second failure step"]) == 0   # idempotent


# ================================================================ D4: /health tells the database's truth
def _app(dsn_, env, keys_, booking=True):
    return create_app(Settings(dsn=dsn_, cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                               session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                               facilities=("FAC-JAI-01", "FAC-GGN-01"), jwks=keys_["jwks"], booking_enabled=booking,
                               local_test_harness=True, environment=env))


def test_health_reports_the_database_verdict_on_every_call(c, keys):  # noqa: F811
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    app = _app(c.info.dsn, env, keys)
    app.state.service.db.deployment = {"environment": env, "deployment_id": d["dep"], "code_sha256": d["art"]}
    client = TestClient(app)
    h = client.get("/health").json()
    assert h["booking_enabled"] is False and h["booking_state"] == "refused" and "BOOKING_WORKFLOW" in h["booking_refusal"]
    issue(c, d)
    h = client.get("/health").json()
    assert h["booking_enabled"] is True and h["booking_state"] == "authorized" and h["booking_provisional_until"]
    c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = true, changed_by = 'test', reason = 'health drill'")
    try:
        h = client.get("/health").json()
        assert h["booking_enabled"] is False and h["booking_kill_switch"] == "ENGAGED" and h["booking_state"] == "refused"
    finally:
        c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = false, changed_by = 'test', reason = 'over'")
    revoke(c, env)
    h = client.get("/health").json()                                  # the same warm container, after revocation
    assert h["booking_enabled"] is False and h["booking_state"] == "refused"
    assert "REVOKED" in h["booking_refusal"] and "booking_provisional_until" not in h


def test_health_fails_closed_when_the_verdict_cannot_be_read(c, keys, monkeypatch):  # noqa: F811
    import setuhaul.api.app as A
    env = env_name()
    d = complete(c, env)
    app = _app(c.info.dsn, env, keys)
    app.state.service.db.deployment = {"environment": env, "deployment_id": d["dep"], "code_sha256": d["art"]}
    monkeypatch.setattr(A, "booking_authorization", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    h = TestClient(app).get("/health").json()
    assert h["booking_enabled"] is False and h["booking_state"] == "unknown"


def test_health_with_the_switch_off_says_off(c, keys):  # noqa: F811
    h = TestClient(_app(c.info.dsn, env_name(), keys, booking=False)).get("/health").json()
    assert h["booking_enabled"] is False and h["booking_state"] == "off" and "booking_switch" not in h


def test_a_cold_container_whose_gates_fail_starts_booking_off_and_reports_why(monkeypatch, c, keys):  # noqa: F811
    """The 24 Nov outage: the gate check raised inside the Lambda runtime and the whole API returned 502. Now the
    container runs booking-OFF, serves the console and /health, and reports the refusal (fail closed for booking)."""
    import setuhaul.api.app as A
    from setuhaul.api.inbound import BookingGateError
    from setuhaul.aws import lambdas
    real = A.create_app
    env = env_name()
    seen = []

    def create(settings, *a, **k):
        seen.append(settings.booking_enabled)
        if settings.booking_enabled:
            raise BookingGateError("deployment gates not satisfied: BOOKING_WORKFLOW: no evidence for this deployment")
        return real(settings, *a, **k)

    monkeypatch.setattr(A, "create_app", create)
    monkeypatch.setattr(A.Settings, "from_env", classmethod(lambda cls: Settings(
        dsn=c.info.dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
        meta_app_secret=META_SECRET, facilities=("FAC-JAI-01",), jwks=keys["jwks"], booking_enabled=True,
        environment=env)))
    monkeypatch.setattr(lambdas, "load_app_secrets", lambda env_: env_)
    lambdas.runtime.cache_clear()
    try:
        app = lambdas.runtime()
        assert seen == [True, False]                                     # tried with booking ON, runs booking-OFF
        h = TestClient(app).get("/health")
        assert h.status_code == 200
        body = h.json()
        assert body["booking_enabled"] is False and body["booking_state"] == "refused"
        assert body["booking_switch"] == "on" and "BOOKING_WORKFLOW" in body["booking_refusal"]
        assert app.state.service.booking_enabled is False                # booking tools refuse in this container
    finally:
        lambdas.runtime.cache_clear()


def test_warm_and_cold_containers_report_the_same_database_truth(monkeypatch, c, keys):  # noqa: F811
    """One database state (a REVOKED window), two containers: a WARM one that started while the window was open (its
    start-up snapshot still names the window) and a COLD one started afterwards (its gates fail, so it runs booking-OFF).
    Both report what the database says now: booking not enabled, refused, no provisional window."""
    import setuhaul.api.app as A
    from setuhaul.api.inbound import BookingGateError
    from setuhaul.aws import lambdas
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    out = issue(c, d)
    warm = _app(c.info.dsn, env, keys)
    warm.state.service.db.deployment = {"environment": env, "deployment_id": d["dep"], "code_sha256": d["art"]}
    warm.state.provisional = {"authorization_id": out["authorization_id"], "expires_at": out["expires_at"]}  # stale
    assert TestClient(warm).get("/health").json()["booking_state"] == "authorized"
    revoke(c, env)
    real = A.create_app

    def create(settings, *a, **k):
        if settings.booking_enabled:
            raise BookingGateError("deployment gates not satisfied: BOOKING_WORKFLOW: the provisional authorization "
                                   "is REVOKED")
        return real(settings, *a, **k)

    monkeypatch.setattr(A, "create_app", create)
    monkeypatch.setattr(A.Settings, "from_env", classmethod(lambda cls: Settings(
        dsn=c.info.dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
        meta_app_secret=META_SECRET, facilities=("FAC-JAI-01",), jwks=keys["jwks"], booking_enabled=True,
        environment=env)))
    monkeypatch.setattr(lambdas, "load_app_secrets", lambda env_: env_)
    lambdas.runtime.cache_clear()
    try:
        cold = lambdas.runtime()
        hw, hc = TestClient(warm).get("/health").json(), TestClient(cold).get("/health").json()
    finally:
        lambdas.runtime.cache_clear()
    for h in (hw, hc):
        assert h["booking_enabled"] is False and h["booking_state"] == "refused" and h["booking_switch"] == "on"
        assert "booking_provisional_until" not in h and h["booking_kill_switch"] == "disengaged"
    assert "REVOKED" in hw["booking_refusal"] and "REVOKED" in hc["booking_refusal"]
    assert verdict(c, d) is not None                                     # and the database itself refuses


def test_the_database_refuses_even_when_a_container_believes_booking_is_on(c, sch):  # noqa: F811
    """Database authority: whatever a warm container's start-up state says, a commitment after revocation is refused
    by the database (0030/0032), so /health reporting the database is reporting the thing that actually decides."""
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    assert book(c, d)
    revoke(c, env)
    refused(lambda: book(c, d), "provisional authorization is REVOKED")
    st = c.execute("SELECT setuhaul.booking_authorization_status(%s,%s,%s)", (env, d["dep"], d["art"])).fetchone()[0]
    assert st["authorized"] is False and "REVOKED" in st["verdict"] and st["provisional_open"] is False


# ================================================================ check 1 (owner review): the D2 correction is inert
def test_an_attestation_can_never_relabel_a_decision(dsn, c):  # noqa: F811
    """H1: the kind always comes from the immutable decided_by. An attestation that agrees changes nothing; one that
    disagrees reads as attestation_conflict, which matches no record (so even the owner cannot relabel a drill)."""
    drill, console = _approval(c, "staff:step6-drill-planner"), _approval(c, UUID_DECIDER)
    for aid, kind in ((drill, "console_user"), (console, "console_user")):
        c.execute("""INSERT INTO setuhaul.approval_decider_attestations (approval_id, decider_kind, basis, evidence_ref)
                     VALUES (%s, %s, 'test: an attestation written directly by the owner', 'test-ref')""", (aid, kind))
    assert _kinds(dsn, [drill, console]) == {drill: "attestation_conflict", console: "console_user"}


def _collector():
    import importlib.util
    spec = importlib.util.spec_from_file_location("step6_collector", ROOT / "scripts" / "step6_gate_evidence.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeLogs:
    def __init__(self, events, retention=7):
        self.events, self.retention, self.calls = events, retention, []

    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": logGroupNamePrefix, "retentionInDays": self.retention}]}

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        rid = kw["filterPattern"].strip('"')
        return {"events": [e for e in self.events if rid in e["message"]
                           and kw["startTime"] <= e["timestamp"] <= kw["endTime"]]}


def test_the_collector_corroborates_owner_requests_only_from_the_stacks_own_api_log(tmp_path):
    from datetime import datetime, timezone
    m = _collector()
    owner = json.loads((ROOT / "docs" / "release" / "evidence" / "owner_attribution_20261123.json").read_text(
        encoding="utf-8"))
    c2 = "29ffac03-d8bd-4dac-b35c-66b0f8ebf24b"
    req = next(e for e in owner["executions"] if e["approval_id"] == c2)["cloudwatch"]["requests"][0]
    t = datetime.fromisoformat(req["t_utc"].replace("Z", "+00:00"))
    line = {"timestamp": int(t.timestamp() * 1000), "message": f"[INFO]\t{req['t_utc']}\t{req['request_id']}\t{req['line']}"}
    now = datetime(2026, 11, 25, tzinfo=timezone.utc)
    group = "SetuHaulPilot-LogsApi-from-the-stack"
    fake = FakeLogs([line])
    got = m.owner_api_log_corroboration(owner, group, now, logs=fake, repo_root=tmp_path)
    assert got[c2]["status"] == "verified" and got[c2]["events"][0]["request_id"] == req["request_id"]
    assert fake.calls and all(call["logGroupName"] == group for call in fake.calls)   # never the record's own group
    forged = {**line, "message": line["message"].replace(" 200", " 500")}         # same id, different outcome
    assert m.owner_api_log_corroboration(owner, group, now, logs=FakeLogs([forged]),
                                         repo_root=tmp_path)[c2]["status"] == "missing"
    assert m.owner_api_log_corroboration(owner, group, now, logs=FakeLogs([]),
                                         repo_root=tmp_path)[c2]["status"] == "missing"
    late = datetime(2026, 12, 5, tzinfo=timezone.utc)                              # past the 7-day retention
    assert m.owner_api_log_corroboration(owner, group, late, logs=FakeLogs([line]),
                                         repo_root=tmp_path)[c2]["status"] == "beyond_retention_unverified"
    import subprocess                                                              # M-3: committed in git
    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    rel = tmp_path / "docs" / "release" / "evidence" / "renewal_gates.json"
    rel.parent.mkdir(parents=True)
    rel.write_text(json.dumps({"gates": {"BOOKING_WORKFLOW": {"evidence": {
        "api_log_corroboration": {c2: got[c2]}}}}}), encoding="utf-8")
    assert m.owner_api_log_corroboration(owner, group, late, logs=FakeLogs([]),
                                         repo_root=tmp_path)[c2]["status"] == "beyond_retention_unverified"
    git("add", "-A")
    git("commit", "-q", "-m", "renewal report")
    assert m.owner_api_log_corroboration(owner, group, late, logs=FakeLogs([]),
                                         repo_root=tmp_path)[c2]["status"] == "previously_verified"
    assert m.owner_api_log_corroboration(owner, None, now, logs=FakeLogs([line]),
                                         repo_root=tmp_path)[c2]["status"] == "missing"


def test_0034_applies_on_a_0033_database_and_changes_only_the_rate_limit(tmp_path):
    """0034 (owner policy change): forward-only on the live 0033 state; the 0033 file keeps the live pilot's
    fingerprint and the authorization history is untouched; afterwards an environment may hold four issuances in
    24 h, never a fifth, and the issuing function keeps its owner and grants."""
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    from setuhaul.db.migrate import migrate
    assert hashlib.sha256((MIG / "0033_recovery_and_decider_provenance.sql").read_bytes()).hexdigest() == \
        "4d7af180d025e774369672566051d3623cb3018007dd31c5ab9a2d04a68a7ec4"            # = the live pilot's 0033
    old = tmp_path / "upto0033"
    old.mkdir()
    for p in MIG.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if p.name[:4] <= "0033":
            shutil.copy(p, old / p.name)
    db = "setuhaul_build_upgrade_0034"
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as cx:
        cx.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        cx.execute(f"CREATE DATABASE {db}")
    d = f"{BASE_DSN} dbname={db}"
    try:
        assert migrate(d, old)[-1] == "0033_recovery_and_decider_provenance"
        _seed(d)
        with psycopg.connect(d, autocommit=True) as cx:
            cx.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'test: as on the pilot'")
            cx.execute("UPDATE setuhaul.booking_evidence_guard SET identity_enforced = true, reason = 'test: M2'")
            env = env_name()
            d1, _ = revoked_release(cx, env)                        # history written by 0033's function
            insert_auth(cx, other_dep(env), "m-" + "1" * 40, "-2 hours", "-90 minutes", "REVOKED", "-100 minutes")
            before = cx.execute("SELECT authorization_id, status, issued_at, closed_at FROM "
                                "setuhaul.provisional_authorizations ORDER BY 1").fetchall()
            complete(cx, env)                                       # a fully evidenced predecessor (no chaining)
            d3 = new_release(cx, env)
            refused(lambda: issue(cx, d3), "PROVISIONAL_RATE_LIMIT")             # under 0033 two is the limit
        applied = migrate(d)                                    # 0034, then any later forward migration (0035)
        assert applied[0] == "0034_provisional_limit_4" and all(v > "0034" for v in applied[1:])
        with psycopg.connect(d, autocommit=True) as cx:
            assert cx.execute("SELECT authorization_id, status, issued_at, closed_at FROM "
                              "setuhaul.provisional_authorizations ORDER BY 1").fetchall() == before
            assert events(cx, d1) == ["ISSUED", "REVOKED"]                       # history preserved
            issue(cx, d3)                                                         # the third, now allowed
            revoke(cx, env)
            complete(cx, env)
            d4 = new_release(cx, env)
            issue(cx, d4)                                                         # the fourth
            revoke(cx, env)
            complete(cx, env)
            d5 = new_release(cx, env)
            issue(cx, d5)                                                         # the fifth: 0044 allows eight
            assert cx.execute("SELECT setuhaul.provisional_rate_limit(%s)", (env,)).fetchone()[0] == 8
            fn = "'setuhaul.issue_provisional_authorization(text,text,text,text,text,integer,bigint)'::regprocedure"
            assert cx.execute(f"SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = {fn}").fetchone()[0] == \
                "setuhaul_rpc"
            assert cx.execute(f"SELECT has_function_privilege('setuhaul_deploy_evidence', {fn}, 'EXECUTE'), "
                              f"has_function_privilege('setuhaul_agent', {fn}, 'EXECUTE')").fetchone() == (True, False)
    finally:
        with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as cx:
            cx.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
