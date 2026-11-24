"""Option P (single-use provisional booking authorization) and M2 (deployment identity), migration 0032.

Runs on its own database with the evidence guard AND deployment identity enforced (the pilot's state after 0032).
Every test uses its own pilot environment name, so the per-environment rules (one open authorization, two per 24 h)
never leak between tests. Booking commitments are made under the release's own login (SET SESSION AUTHORIZATION to a
test login named setuhaul_login_<role>_<12 hex>, as the Data API authenticates with the release's secret), so the
database's identity check is exercised exactly as in AWS."""
from __future__ import annotations

import itertools
import json
import threading
import uuid
from contextlib import contextmanager

import psycopg
import pytest

from conftest import BASE_DSN, _pg_available, _seed, as_role
from setuhaul.api.inbound import REQUIRED_GATES, deployment_id_for, provisional_window
from test_booking_evidence_guard import as_pipeline, declare, evidence, exception, manifest, record
from test_move_appointment import SCH, confirmed, sch, staff  # noqa: F401

DB = "setuhaul_build_provisional"
REF_A, REF_B, REF_C, REF_D = (f"OWNER-APPROVAL-20261124-PROV-{x}" for x in "ABCD")
SHA = "c" * 40
SLOT = itertools.count()


def env_name() -> str:
    return f"pilot-p{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def dsn():
    """A fresh database with the guard and deployment identity enforced (the owner's one-way steps; on the pilot
    0030 and 0032 set both at migration time)."""
    from setuhaul.db.migrate import migrate
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
    d = f"{BASE_DSN} dbname={DB}"
    migrate(d)
    _seed(d)
    with psycopg.connect(d, autocommit=True) as c:
        c.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'test: owner enables the guard'")
        c.execute("UPDATE setuhaul.booking_evidence_guard SET identity_enforced = true, reason = 'test: owner enables M2'")
    yield d
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:      # cluster-wide test logins
        for (r,) in c.execute("SELECT rolname FROM pg_roles WHERE rolname ~ '^setuhaul_login_(agent|console)_[0-9a-f]{12}$'"
                              ).fetchall():
            c.execute(f"DROP ROLE IF EXISTS {r}")


@pytest.fixture
def c(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


# ------------------------------------------------------------------------------------------------ helpers
def login_name(d, role="console") -> str:
    return f"setuhaul_login_{role}_{d['art'][:12]}"


@contextmanager
def as_login(conn, d, role="console"):
    """Authenticate (session_user) as the release's login, as the Data API does with the release's secret. Test logins
    are superusers so the fixtures' direct inserts still work; the guard judges session_user, not privileges."""
    name = d if isinstance(d, str) else login_name(d, role)
    conn.execute(f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN "
                 f"CREATE ROLE {name} LOGIN SUPERUSER; END IF; END$$")
    conn.execute(f"SET SESSION AUTHORIZATION {name}")
    try:
        yield conn
    finally:
        conn.execute("RESET SESSION AUTHORIZATION")


def complete(conn, env, art=None, ref=REF_A):
    """A release recorded with complete NORMAL evidence for all six gates (the predecessor)."""
    d = manifest(env, art)
    record(conn, d)
    for g in REQUIRED_GATES:
        evidence(conn, d, g)
    return d


def new_release(conn, env, exceptions=None, ref=REF_A, skip=()):
    """A new release recorded with its five non-workflow gates (no BOOKING_WORKFLOW evidence)."""
    d = manifest(env)
    record(conn, d)
    for g in REQUIRED_GATES:
        if g == "BOOKING_WORKFLOW" or g in skip:
            continue
        if exceptions and g in exceptions:
            exception(conn, d, g, exceptions[g], ref)
        else:
            evidence(conn, d, g)
    return d


def fresh_ref() -> str:
    return f"OWNER-APPROVAL-20261124-T{uuid.uuid4().hex[:10]}"


def issue(conn, d, ref=None, minutes=60, sha=SHA, run="run-1"):
    """Issue as the pipeline. Approval references are single-use database-wide, so each call gets a fresh one."""
    ref = ref or fresh_ref()
    with conn.transaction():
        conn.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        out = conn.execute("SELECT setuhaul.issue_provisional_authorization(%s,%s,%s,%s,%s,%s)",
                           (d["env"], d["dep"], sha, ref, run, minutes)).fetchone()[0]
    return out


def refused(fn, code):
    with pytest.raises(psycopg.Error) as e:
        fn()
    assert code in str(e.value), str(e.value)
    return e.value


def book(conn, d, role="console", login=None):
    """One booking commitment by the release (declared, under its login), on a fresh dock slot."""
    declare(conn, d)
    with as_login(conn, login or d, role):
        with conn.transaction():
            return confirmed(conn, start_h=3.0 + 1.25 * next(SLOT))


def verdict(conn, d, offset="0 seconds", login=None):
    return conn.execute("SELECT setuhaul.booking_evidence_verdict(%s,%s,%s, clock_timestamp() + %s::interval, %s)",
                        (d["env"], d["dep"], d["art"], offset, login or login_name(d))).fetchone()[0]


def auth_row(conn, d):
    return conn.execute("""SELECT status, issued_at, expires_at, consumed_evidence_id, closed_by
                             FROM setuhaul.provisional_authorizations WHERE environment=%s AND deployment_id=%s""",
                        (d["env"], d["dep"])).fetchone()


def events(conn, d):
    return [r[0] for r in conn.execute("""SELECT action FROM setuhaul.provisional_authorization_events
                                          WHERE environment=%s AND deployment_id=%s ORDER BY event_id""",
                                       (d["env"], d["dep"]))]


# ================================================================ lifecycle: issue -> book -> consume
def test_the_full_window_issue_book_smoke_evidence_consumes_atomically(c, sch):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    refused(lambda: book(c, d), "BOOKING_WORKFLOW no evidence")             # before issuance: refused
    ref = fresh_ref()
    out = issue(c, d, ref=ref)
    assert out["deployment_id"] == d["dep"] and out["approval_ref"] == ref
    assert book(c, d)                                                         # the smoke may book inside the window
    assert book(c, d, role="agent")                                           # the agent's login too
    evidence(c, d, "BOOKING_WORKFLOW")                                        # the renewal closes it
    st, issued, expires, ev_id, closed_by = auth_row(c, d)
    assert st == "CONSUMED" and ev_id is not None and closed_by == "postgres"
    assert (expires - issued).total_seconds() == 3600                         # 60 minutes by default
    assert events(c, d) == ["ISSUED", "CONSUMED"]
    assert book(c, d)                                                         # now on normal evidence
    assert verdict(c, d, "2 hours") is None                                   # no longer bound to the window
    evidence(c, d, "BOOKING_WORKFLOW")                                        # later renewals are ordinary


def test_the_window_is_exclusive_at_its_expiry_by_the_database_clock(c):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    out = issue(c, d, minutes=60)
    exp = out["expires_at"]
    at = lambda q: c.execute(f"SELECT setuhaul.booking_evidence_verdict(%s,%s,%s, %s::timestamptz {q}, %s)",  # noqa: E731
                             (d["env"], d["dep"], d["art"], exp, login_name(d))).fetchone()[0]
    assert at("- interval '1 microsecond'") is None
    assert "provisional authorization is EXPIRED" in at("")
    assert "provisional authorization is EXPIRED" in at("+ interval '1 microsecond'")
    assert c.execute("SELECT setuhaul.booking_evidence_verdict(%s,%s,%s, %s::timestamptz - interval '1 second', %s)",
                     (d["env"], d["dep"], d["art"], out["issued_at"], login_name(d))).fetchone()[0]   # not before


def test_an_expired_window_refuses_booking_and_its_evidence_and_cannot_be_extended(c, sch):
    """Simulated time: the owner inserts an authorization whose window already ended (the issuer always starts at
    clock_timestamp(); this is the only way to reach an expired window without waiting an hour)."""
    env = env_name()
    prev = complete(c, env)
    d = new_release(c, env)
    c.execute("""INSERT INTO setuhaul.provisional_authorizations (environment, deployment_id, code_sha256,
                         predecessor_deployment, release_sha, approval_ref, pipeline_run, issued_by, issued_at, expires_at)
                 VALUES (%s,%s,%s,%s,%s,%s,'run-x','test', clock_timestamp() - interval '61 minutes',
                         clock_timestamp() - interval '1 minute')""",
              (env, d["dep"], d["art"], prev["dep"], SHA, f"OWNER-APPROVAL-20261124-X{uuid.uuid4().hex[:6]}"))
    refused(lambda: book(c, d), "provisional authorization is EXPIRED")
    snap = c.execute("SELECT setuhaul.booking_provisional_snapshot(%s,%s)", (env, d["dep"])).fetchone()[0]
    assert snap["status"] == "EXPIRED" and snap["open"] is False
    refused(lambda: evidence(c, d, "BOOKING_WORKFLOW"), "PROVISIONAL_CLOSED")       # no late close
    refused(lambda: c.execute("""UPDATE setuhaul.provisional_authorizations SET expires_at = expires_at + interval '1 hour'
                                  WHERE environment=%s""", (env,)), "PROVISIONAL_IMMUTABLE")   # no extension
    refused(lambda: issue(c, d), "PROVISIONAL_ALREADY_ISSUED")                        # no second window
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        assert c.execute("SELECT setuhaul.expire_provisional_authorizations()").fetchone()[0] >= 1
    assert auth_row(c, d)[0] == "EXPIRED" and events(c, d) == ["ISSUED", "EXPIRED"]
    refused(lambda: c.execute("UPDATE setuhaul.provisional_authorizations SET status='ISSUED' WHERE environment=%s",
                              (env,)), "PROVISIONAL_FINAL")


# ================================================================ issuance conditions
def test_production_and_invalid_environments_are_refused_categorically(c):
    for env, code in (("prod-ap-south-1", "never production"), ("pilot-PROD", "never production"),
                      ("Pilot-X", "never production"), ("staging-x", "never production")):
        d = {"env": env, "dep": "m-" + "1" * 40, "art": "2" * 64}
        refused(lambda: issue(c, d), code)
    with pytest.raises(psycopg.Error):                                            # the table refuses it too
        c.execute("""INSERT INTO setuhaul.provisional_authorizations (environment, deployment_id, code_sha256,
                             predecessor_deployment, release_sha, approval_ref, pipeline_run, issued_by, issued_at,
                             expires_at) VALUES ('prod-x', %s, %s, %s, %s, 'OWNER-APPROVAL-20261124-ZZZ', 'r', 't',
                             now(), now() + interval '1 minute')""", ("m-" + "1" * 40, "2" * 64, "m-" + "3" * 40, SHA))


def test_the_window_is_at_most_two_hours_and_60_minutes_by_default(c):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    refused(lambda: issue(c, d, minutes=121), "1 to 120 minutes")
    refused(lambda: issue(c, d, minutes=0), "1 to 120 minutes")
    out = issue(c, d, minutes=120)
    assert auth_row(c, d)[2] - auth_row(c, d)[1] == __import__("datetime").timedelta(hours=2)
    assert out["expires_at"]


def test_the_previous_release_must_have_complete_normal_evidence_no_chaining(c):
    env = env_name()
    d1 = new_release(c, env)                                    # a first release with no predecessor at all
    refused(lambda: issue(c, d1), "no previous release")
    base = complete(c, env)
    d2 = new_release(c, env)
    issue(c, d2)                                                 # d2 runs on a provisional window
    d3 = new_release(c, env)
    refused(lambda: issue(c, d3), "previous release")             # d2 was never fully evidenced: no chain
    assert base


def test_the_five_other_gates_must_pass_normally_under_this_approval(c):
    env = env_name()
    complete(c, env)
    d = new_release(c, env, skip=("AWS_QUEUE",))
    refused(lambda: issue(c, d), "AWS_QUEUE no evidence")
    env2 = env_name()
    complete(c, env2)
    d2 = new_release(c, env2, exceptions={"WORKLOAD_IDENTITY": "NO-LIVE-DRIFT-DETECTION"}, ref=REF_C)
    refused(lambda: issue(c, d2), "another owner approval")
    assert issue(c, d2, ref=REF_C)


def test_only_the_newest_release_and_one_authorization_per_deployment_and_approval(c):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    used = fresh_ref()
    issue(c, d, ref=used)
    refused(lambda: issue(c, d), "PROVISIONAL_OPEN")                               # while it is open
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        c.execute("SELECT setuhaul.revoke_provisional_authorization(%s,'test: closed')", (env,))
    refused(lambda: issue(c, d), "PROVISIONAL_ALREADY_ISSUED")                     # and after: once per deployment
    env2 = env_name()
    complete(c, env2)
    d2 = new_release(c, env2)
    refused(lambda: issue(c, d2, ref=used), "PROVISIONAL_APPROVAL_USED")          # an approval is used once
    old = complete(c, env2)
    refused(lambda: issue(c, d2), "not the newest recorded deployment")           # superseded by `old`
    assert old


def test_one_open_window_per_environment_and_two_per_24_hours(c):
    env = env_name()
    complete(c, env)
    d1 = new_release(c, env)
    issue(c, d1, ref=f"OWNER-APPROVAL-20261124-O{uuid.uuid4().hex[:6]}")
    d2 = complete(c, env)                                        # a fully evidenced release (activation path)
    d3 = new_release(c, env)
    refused(lambda: issue(c, d3, ref=f"OWNER-APPROVAL-20261124-O{uuid.uuid4().hex[:6]}"), "PROVISIONAL_OPEN")
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        assert c.execute("SELECT setuhaul.revoke_provisional_authorization(%s,'test: close d1')", (env,)).fetchone()[0] == 1
    issue(c, d3, ref=f"OWNER-APPROVAL-20261124-O{uuid.uuid4().hex[:6]}")       # second in 24 h
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        c.execute("SELECT setuhaul.revoke_provisional_authorization(%s,'test: close d3')", (env,))
    complete(c, env)
    d5 = new_release(c, env)
    refused(lambda: issue(c, d5, ref=f"OWNER-APPROVAL-20261124-O{uuid.uuid4().hex[:6]}"), "PROVISIONAL_RATE_LIMIT")
    assert d2


def test_concurrent_issuers_for_one_environment_exactly_one_wins(dsn):
    with psycopg.connect(dsn, autocommit=True) as c0:
        env = env_name()
        complete(c0, env)
        d = new_release(c0, env)
    results: list[str] = []
    barrier = threading.Barrier(4)

    def go(i):
        with psycopg.connect(dsn, autocommit=True) as cx:
            barrier.wait()
            try:
                issue(cx, d, ref=f"OWNER-APPROVAL-20261124-R{i}{uuid.uuid4().hex[:4]}", run=f"run-{i}")
                results.append("ok")
            except psycopg.Error as e:
                results.append(str(e).split(":")[0])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert results.count("ok") == 1, results
    assert all(r in ("ok", "PROVISIONAL_OPEN", "PROVISIONAL_ALREADY_ISSUED") for r in results), results


def test_concurrent_consumers_consume_exactly_once(dsn):
    with psycopg.connect(dsn, autocommit=True) as c0:
        env = env_name()
        complete(c0, env)
        d = new_release(c0, env)
        issue(c0, d)
    barrier, errors = threading.Barrier(3), []

    def go():
        try:
            with psycopg.connect(dsn, autocommit=True) as cx:
                barrier.wait()
                evidence(cx, d, "BOOKING_WORKFLOW")
        except psycopg.Error as e:                                 # pragma: no cover - reported below
            errors.append(str(e))

    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    with psycopg.connect(dsn) as cx:
        assert not errors, errors
        assert auth_row(cx, d)[0] == "CONSUMED"
        assert events(cx, d) == ["ISSUED", "CONSUMED"]            # one consumption, audited once


def test_a_rolled_back_issuance_leaves_nothing(c):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    rb = fresh_ref()
    with pytest.raises(RuntimeError):
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
            c.execute("SELECT setuhaul.issue_provisional_authorization(%s,%s,%s,%s,'run-rb',60)",
                      (env, d["dep"], SHA, rb))
            raise RuntimeError("pipeline step failed")
    assert auth_row(c, d) is None and events(c, d) == []
    assert issue(c, d, ref=rb)                                      # the approval was not consumed by the rollback


# ================================================================ revocation, kill switch
def test_the_pipeline_revokes_at_once_and_the_release_cannot_close_afterwards(c, sch):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    assert book(c, d)
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        refused(lambda: c.execute("SELECT setuhaul.revoke_provisional_authorization(%s, 'x')", (env,)), "REASON_REQUIRED")
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        assert c.execute("SELECT setuhaul.revoke_provisional_authorization(%s,'smoke failed')", (env,)).fetchone()[0] == 1
    refused(lambda: book(c, d), "provisional authorization is REVOKED")
    refused(lambda: evidence(c, d, "BOOKING_WORKFLOW"), "PROVISIONAL_CLOSED")
    assert events(c, d) == ["ISSUED", "REVOKED"]


def test_an_ops_lead_revokes_through_the_console_and_a_planner_cannot(c, sch):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    with pytest.raises(psycopg.Error, match="ROLE_NOT_ALLOWED|FORBIDDEN|not allowed"):
        with staff(c, roles=("planner",), who="staff:planner-1") as s:
            s.execute("SELECT revoke_provisional_authorization_staff(%s,%s,%s)", (env, "staff:planner-1", "stop it now"))
    with pytest.raises(psycopg.Error, match="PRINCIPAL|principal"):                  # cannot name someone else
        with staff(c, roles=("ops_lead",), who="staff:ops-1") as s:
            s.execute("SELECT revoke_provisional_authorization_staff(%s,%s,%s)", (env, "staff:other", "not me at all"))
    assert auth_row(c, d)[0] == "ISSUED"
    with staff(c, roles=("ops_lead",), who="staff:ops-1") as s:
        assert s.execute("SELECT revoke_provisional_authorization_staff(%s,%s,%s)",
                         (env, "staff:ops-1", "ops lead stops the window")).fetchone()[0] == 1
    assert auth_row(c, d)[0] == "REVOKED" and auth_row(c, d)[4] == "staff:ops-1"


def test_the_kill_switch_still_refuses_first_inside_a_window(c, sch):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = true, changed_by = 'test', reason = 'drill inside P'")
    try:
        refused(lambda: book(c, d), "BOOKING_KILLED")
    finally:
        c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = false, changed_by = 'test', reason = 'drill over'")
    assert book(c, d)


# ================================================================ M2: deployment identity
def test_only_the_releases_own_login_may_book_even_with_a_correct_declaration(c, sch):
    env = env_name()
    prev = complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    refused(lambda: book(c, d, login="postgres_wrong_login"), "IDENTITY_MISMATCH")     # any other login
    refused(lambda: book(c, d, login=login_name(prev)), "IDENTITY_MISMATCH")          # the previous release's
    declare(c, d)
    with c.transaction():                                        # the owner's own session declares correctly: refused
        refused(lambda: confirmed(c, start_h=3.0 + 1.25 * next(SLOT)), "IDENTITY_MISMATCH")
    assert book(c, d, role="console") and book(c, d, role="agent")


def test_identity_is_required_on_normal_evidence_too(c, sch):
    env = env_name()
    d = complete(c, env)
    refused(lambda: book(c, d, login="setuhaul_login_console_000000000000"), "IDENTITY_MISMATCH")
    assert book(c, d)


def test_set_role_cannot_change_the_authenticated_identity(c, sch):
    env = env_name()
    d = complete(c, env)
    declare(c, d)
    with as_login(c, "setuhaul_login_console_ffffffffffff"):
        with c.transaction():
            c.execute(f"SET LOCAL ROLE {login_name(d)}" if False else "SET LOCAL ROLE setuhaul_console")
            assert c.execute("SELECT session_user").fetchone()[0] == "setuhaul_login_console_ffffffffffff"
            c.execute("RESET ROLE")
            refused(lambda: confirmed(c, start_h=3.0 + 1.25 * next(SLOT)), "IDENTITY_MISMATCH")


def test_identity_enforcement_is_one_way_and_needs_the_guard(c):
    refused(lambda: c.execute("UPDATE setuhaul.booking_evidence_guard SET identity_enforced = false, reason = 'off now'"),
            "EVIDENCE_GUARD_ONE_WAY")
    fresh = "setuhaul_build_provisional_off"
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {fresh} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {fresh}")
    from setuhaul.db.migrate import migrate
    migrate(f"{BASE_DSN} dbname={fresh}")
    with psycopg.connect(f"{BASE_DSN} dbname={fresh}", autocommit=True) as f:
        assert f.execute("SELECT enforced, identity_enforced FROM setuhaul.booking_evidence_guard").fetchone() == (False, False)
        refused(lambda: f.execute("UPDATE setuhaul.booking_evidence_guard SET identity_enforced = true, reason = 'M2 alone'"),
                "EVIDENCE_GUARD_ORDER")
        f.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'guard on'")
        d = {"env": "pilot-off", "dep": "m-" + "1" * 40, "art": "2" * 64}
        with f.transaction():
            f.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
            refused(lambda: f.execute("SELECT setuhaul.issue_provisional_authorization(%s,%s,%s,%s,'r',60)",
                                      (d["env"], d["dep"], SHA, REF_A)), "deployment identity (M2) is not enforced")


# ================================================================ append-only audit and least privilege
def test_authorizations_and_their_audit_are_append_only_for_everyone(c):
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    ref = fresh_ref()
    issue(c, d, ref=ref)
    refused(lambda: c.execute("DELETE FROM setuhaul.provisional_authorizations WHERE environment=%s", (env,)), "APPEND_ONLY")
    refused(lambda: c.execute("TRUNCATE setuhaul.provisional_authorizations CASCADE"), "APPEND_ONLY")
    refused(lambda: c.execute("UPDATE setuhaul.provisional_authorization_events SET principal='x'"), "APPEND_ONLY")
    refused(lambda: c.execute("DELETE FROM setuhaul.provisional_authorization_events"), "APPEND_ONLY")
    refused(lambda: c.execute("UPDATE setuhaul.provisional_authorizations SET release_sha=%s WHERE environment=%s",
                              ("d" * 40, env)), "PROVISIONAL_IMMUTABLE")
    ev = c.execute("""SELECT principal, detail FROM setuhaul.provisional_authorization_events
                       WHERE environment=%s""", (env,)).fetchone()
    assert ev[0] == "postgres" and ev[1]["approval_ref"] == ref and ev[1]["release_sha"] == SHA


APP_ROLES = ("setuhaul_agent", "setuhaul_console", "setuhaul_ingest", "setuhaul_feasibility", "setuhaul_system",
             "setuhaul_deploy_evidence")


def test_no_application_role_touches_the_tables_and_each_function_has_exactly_its_callers(c):
    for role in APP_ROLES:
        for t in ("provisional_authorizations", "provisional_authorization_events"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert c.execute("SELECT has_table_privilege(%s, %s, %s)", (role, f"setuhaul.{t}", priv)).fetchone()[0] \
                    is False, (role, t, priv)
    callers = {"issue_provisional_authorization(text,text,text,text,text,integer)": {"setuhaul_deploy_evidence"},
               "revoke_provisional_authorization(text,text)": {"setuhaul_deploy_evidence"},
               "expire_provisional_authorizations()": {"setuhaul_deploy_evidence"},
               "revoke_provisional_authorization_staff(text,text,text)": {"setuhaul_console"},
               "booking_provisional_snapshot(text,text)": {"setuhaul_system", "setuhaul_ingest", "setuhaul_agent",
                                                          "setuhaul_deploy_evidence"},
               "booking_evidence_verdict(text,text,text,timestamptz,text)": set(),
               "booking_gate_problems(text,text,timestamptz,timestamptz,text)": set()}
    for fn, allowed in callers.items():
        got = {r for r in APP_ROLES if c.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                                 (r, f"setuhaul.{fn}")).fetchone()[0]}
        assert got == allowed, (fn, got)
        assert c.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (f"setuhaul.{fn}",)).fetchone()[0] \
            is False, fn
    owners = dict(c.execute("""SELECT p.proname, pg_get_userbyid(p.proowner) FROM pg_proc p
                                JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname='setuhaul'
                                 AND p.prosecdef AND p.proname ~ 'provisional|booking_evidence_verdict'""").fetchall())
    assert owners and set(owners.values()) == {"setuhaul_rpc"}, owners


# ================================================================ the start-up check sees an open window only
def test_the_start_up_check_accepts_only_an_open_window_for_this_code(c):
    from setuhaul.api.db import Db
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    db = Db(c.info.dsn)
    assert provisional_window(db, env, d["dep"], d["m"]) is None              # nothing issued
    ref = fresh_ref()
    issue(c, d, ref=ref)
    w = provisional_window(db, env, d["dep"], d["m"])
    assert w and w["approval_ref"] == ref and w["release_sha"] == SHA
    other = json.loads(json.dumps(d["m"]))
    other["gates"]["RECOVERY_WORKER"]["artifact_sha256"] = "9" * 64
    assert provisional_window(db, env, d["dep"], other) is None               # other code: no window
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        c.execute("SELECT setuhaul.revoke_provisional_authorization(%s,'test: revoke')", (env,))
    assert provisional_window(db, env, d["dep"], d["m"]) is None              # revoked: closed at once
    assert deployment_id_for(d["m"]) == d["dep"]



# ================================================================ the pipeline path (setuhaul.ops.gate_evidence)
def test_the_pipeline_records_the_release_and_issues_in_one_transaction_or_nothing(c):
    """gate_evidence.provisional: manifest, the five non-workflow gates and the authorization in ONE transaction on the
    deploy-evidence login. The report must be a fresh renewal report from the RUNNING release under this approval.
    On PostgreSQL 17.9 (CI, the pilot) the AURORA exception (T33-P) holds and the window opens; on 18 the database
    refuses the exception and the whole transaction is rolled back (nothing written)."""
    from datetime import datetime, timezone
    from setuhaul.ops import gate_evidence as ge
    from test_inbound_delivery import manifest as full_manifest
    from test_option_d import SINK, live_report
    env = "pilot-ap-southeast-2"                                  # the renewal report's environment
    running = complete(c, env)
    report, _ = live_report()
    ref = fresh_ref()
    report.update({"mode": "renewal", "booking_enabled": True, "environment": env, "approval_ref": ref,
                   "collected_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "artifact_sha256": running["art"]})
    raw = json.dumps(report).encode()
    man = full_manifest(env=env, release=uuid.uuid4().hex[:8])
    fps = {g: uuid.uuid4().hex * 2 for g in REQUIRED_GATES}
    uri = "https://github.com/x/y/blob/abc/docs/release/evidence/provisional_gates.json"
    ver = int(c.execute("SELECT current_setting('server_version_num')").fetchone()[0])

    def run(rep=report, m=man, ref_=None):
        with psycopg.connect(c.info.dsn) as cx:
            with cx.transaction():
                cx.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
                cx.execute("SET LOCAL search_path = setuhaul")
                return ge.provisional(cx, rep, raw, m, env, uri, ref_ or ref, fps, ver, SINK, SHA, "run-e2e")

    def count():
        return c.execute("SELECT count(*) FROM setuhaul.deployment_manifests WHERE environment=%s", (env,)).fetchone()[0]

    before = count()
    for bad, msg in (({**report, "artifact_sha256": "e" * 64}, "not collected on the running release"),
                     ({**report, "collected_utc": "2026-11-01T00:00:00+00:00"}, "not fresh"),
                     ({**report, "booking_enabled": False}, "running pilot")):
        with pytest.raises(ge.EvidenceRefused, match=msg):
            run(rep=bad)
    with pytest.raises(ge.EvidenceRefused, match="names approval"):
        run(ref_=fresh_ref())
    assert count() == before
    if 170009 <= ver < 180000:
        out = run()
        dep = deployment_id_for(man)
        assert out["provisional"]["deployment_id"] == dep and "BOOKING_WORKFLOW" not in out["evidence"]
        assert c.execute("SELECT count(*) FROM setuhaul.deployment_evidence WHERE deployment_id=%s "
                         "AND gate='BOOKING_WORKFLOW'", (dep,)).fetchone()[0] == 0
        assert c.execute("SELECT setuhaul.booking_provisional_snapshot(%s,%s)", (env, dep)).fetchone()[0]["open"] is True
        with pytest.raises(ge.EvidenceRefused, match="already the newest"):
            run()
    else:
        with pytest.raises((psycopg.Error, ge.EvidenceRefused), match="PROVISIONAL_REFUSED|AURORA"):
            run()
        assert count() == before                                  # the manifest and gates were rolled back too


def test_closing_requires_the_smoke_inside_the_window(c):
    """gate_evidence.provisional_window_problems: the renewal that closes a provisional release must carry a smoke that
    started after issuance and finished before expiry, bound to the authorized release SHA."""
    from datetime import datetime, timedelta, timezone
    from setuhaul.ops import gate_evidence as ge
    env = env_name()
    prev = complete(c, env)
    d = new_release(c, env)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    c.execute("""INSERT INTO setuhaul.provisional_authorizations (environment, deployment_id, code_sha256,
                         predecessor_deployment, release_sha, approval_ref, pipeline_run, issued_by, issued_at, expires_at)
                 VALUES (%s,%s,%s,%s,%s,%s,'run-w','test', %s, %s)""",
              (env, d["dep"], d["art"], prev["dep"], SHA, fresh_ref(), now - timedelta(minutes=10),
               now + timedelta(minutes=50)))

    def rep(start, end, sha=SHA):
        sid = "SMOKE-" + start.strftime("%Y%m%dT%H%M%SZ") + "-abcd1234"
        return {"gates": {"BOOKING_WORKFLOW": {"evidence": {"fresh_smoke": {
            "smoke_id": sid, "collected_utc": end.isoformat(timespec="seconds"), "release_sha": sha}}}}}

    c.execute("SET search_path = setuhaul")
    assert ge.provisional_window_problems(c, rep(now - timedelta(minutes=5), now), env, d["dep"]) == []
    assert "before the authorization" in ge.provisional_window_problems(
        c, rep(now - timedelta(minutes=30), now), env, d["dep"])[0]
    assert "outside the window" in ge.provisional_window_problems(
        c, rep(now - timedelta(minutes=5), now + timedelta(hours=2)), env, d["dep"])[0]
    assert "release SHA" in ge.provisional_window_problems(
        c, rep(now - timedelta(minutes=5), now, sha="d" * 40), env, d["dep"])[0]
    c.execute("UPDATE setuhaul.provisional_authorizations SET status='REVOKED', close_reason='test revoke' "
              "WHERE environment=%s", (env,))
    assert "REVOKED" in ge.provisional_window_problems(c, rep(now - timedelta(minutes=5), now), env, d["dep"])[0]
    assert ge.provisional_window_problems(c, rep(now, now), env_name(), "m-" + "0" * 40) == []   # no window: no rule
    c.execute("RESET search_path")


# ================================================================ M2: login provisioning (owner session, pipeline)
class FakeOwner:
    def __init__(self, names):
        self.names, self.statements = names, []

    def query(self, sql, *a):
        return [] if "rolsuper" in sql else [(n,) for n in self.names]

    def run(self, statements, *a):
        self.statements = statements


def test_provisioning_a_release_login_switches_off_every_other_login_of_that_role():
    from setuhaul.aws.provision_logins import provision
    old, new = "setuhaul_login_console_aaaaaaaaaaaa", "setuhaul_login_console_bbbbbbbbbbbb"
    s = FakeOwner(["setuhaul_login_console", old, "setuhaul_login_agent", "setuhaul_login_system"])
    pw = "p" * 40
    provision(s, {"console": {"username": new, "password": pw},
                  "system": {"username": "setuhaul_login_system", "password": pw}})
    text = "\n".join(s.statements)
    assert f"CREATE ROLE {new} WITH LOGIN" in text
    assert f"ALTER ROLE {old} WITH NOLOGIN" in text and "ALTER ROLE setuhaul_login_console WITH NOLOGIN" in text
    assert f"usename = '{old}'" in text                                       # its sessions are ended
    assert "setuhaul_login_agent WITH NOLOGIN" not in text                     # other roles untouched
    assert "setuhaul_login_system WITH NOLOGIN" not in text                    # non-release roles never rotated
    # a rollback provisions the previous name again: it is re-enabled and the newer one switched off
    s2 = FakeOwner(["setuhaul_login_console", old, new])
    provision(s2, {"console": {"username": old, "password": pw}})
    t2 = "\n".join(s2.statements)
    assert f"ALTER ROLE {old} WITH LOGIN" in t2 and f"ALTER ROLE {new} WITH NOLOGIN" in t2


def test_a_release_login_must_belong_to_its_role():
    from setuhaul.aws.provision_logins import provision
    for user in ("setuhaul_login_agent_bbbbbbbbbbbb", "setuhaul_login_console_xyz",
                 "setuhaul_login_console_ABCDEFABCDEF"):
        with pytest.raises(ValueError):
            provision(FakeOwner([]), {"console": {"username": user, "password": "p" * 40}})


def test_the_pipeline_stops_before_provisioning_if_a_secret_does_not_name_the_release():
    from setuhaul.aws.provision_logins import release_problems
    rel = "abcdefabcdef"
    ok = {"agent": {"username": f"setuhaul_login_agent_{rel}"}, "console": {"username": f"setuhaul_login_console_{rel}"},
          "system": {"username": "setuhaul_login_system"}}
    assert release_problems(ok, rel) == []
    stale = {**ok, "console": {"username": "setuhaul_login_console"}}          # CloudFormation did not regenerate it
    assert release_problems(stale, rel) == [
        "console: the secret names 'setuhaul_login_console', expected setuhaul_login_console_abcdefabcdef"]
    assert release_problems(ok, "nothex") != []
