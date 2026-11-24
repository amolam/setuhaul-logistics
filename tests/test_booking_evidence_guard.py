"""Database-side booking evidence guard (migration 0030; docs/booking-renewal/DATABASE_BOOKING_EVIDENCE_GUARD_DESIGN.md).

Every new booking commitment is checked in the database, at the moment of the commitment, against the deployment the
caller declares: it must be the newest recorded manifest of its environment, for the caller's code, with current
evidence (or an owner-approved exception in its own scope) for every gate, judged by the database clock. Runs on a
dedicated database (manifests are append-only, so nothing here leaks into other tests' databases)."""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from conftest import BASE_DSN, _seed, as_role
from setuhaul.api.inbound import REQUIRED_GATES, deployment_id_for
from test_move_appointment import S1, S2, SCH, appt, at, confirmed, move, move_row, sch, staff, store  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
DB = "setuhaul_build_guard"
PILOT, TEST_ENV = "pilot-ap-southeast-2", "test-env"
REF, REF2 = "OWNER-APPROVAL-20261124-GUARD", "OWNER-APPROVAL-20261124-OTHER"
PASS_ONLY = REQUIRED_GATES
PILOT_EXC = {"WORKLOAD_IDENTITY": "NO-LIVE-DRIFT-DETECTION", "OUTBOUND_DELIVERY": "META-DEFERRED-TEST-SINK-ONLY"}


# ------------------------------------------------------------------------------------------------ database
@pytest.fixture(scope="module")
def dsn():
    """A fresh database with the guard ON (the owner's one-way step), seeded like the shared test database."""
    from conftest import _pg_available
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
        assert c.execute("SELECT enforced FROM setuhaul.booking_evidence_guard").fetchone()[0] is False  # fresh: off
        c.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'test: owner enables the guard'")
    return d


@pytest.fixture
def c(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


def server_version(conn) -> int:
    return int(conn.execute("SELECT current_setting('server_version_num')").fetchone()[0])


def manifest(env=PILOT, art=None):
    art = art or hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    m = {"environment": env, "release": uuid.uuid4().hex[:8], "gates": {
        "RECOVERY_WORKER": {"artifact_sha256": art, "schedule": "rate(1 minute)", "config_version": "3",
                            "queue_visibility_timeout_s": 300},
        "BOOKING_WORKFLOW": {"state_machine_arn": "arn:aws:states:x:000000000000:stateMachine:b",
                             "state_machine_version": "1", "definition_sha256": "2" * 64},
        "WORKLOAD_IDENTITY": {"iam_policies_sha256": "3" * 64, "role_mapping": {"pipeline": "setuhaul_deploy_evidence"}},
        "OUTBOUND_DELIVERY": {"artifact_sha256": "4" * 64, "provider_config_sha256": "5" * 64,
                              "delivery_policy_version": "2"}}}
    return {"m": m, "env": env, "dep": deployment_id_for(m), "art": art}


def as_pipeline(conn, sql, params):
    """As the pipeline's evidence login (setuhaul_deploy_evidence), on an autocommit or an open-transaction connection."""
    with conn.transaction():
        conn.execute("SET ROLE setuhaul_deploy_evidence")     # undone by the savepoint rollback if the insert fails
        conn.execute(sql, params)
        conn.execute("RESET ROLE")


def record(conn, d):
    as_pipeline(conn, """INSERT INTO setuhaul.deployment_manifests (environment, deployment_id, artifact_sha256, manifest)
                         VALUES (%s,%s,%s,%s::jsonb)""", (d["env"], d["dep"], d["art"], json.dumps(d["m"])))


def evidence(conn, d, gate, expires="7 days", verified_ago="0 min", env=None):
    as_pipeline(conn, """INSERT INTO setuhaul.deployment_evidence (environment, deployment_id, gate, verified_at,
                                expires_at, artifact_uri, evidence_sha256, config_fingerprint)
                         VALUES (%s,%s,%s, now() - %s::interval, now() - %s::interval + %s::interval,
                                 's3://e/r.json', %s, %s)""",
                (env or d["env"], d["dep"], gate, verified_ago, verified_ago, expires, "a" * 64, "f" * 64))


def exception(conn, d, gate, code, ref=REF, days=7):
    as_pipeline(conn, """INSERT INTO setuhaul.deployment_exceptions (environment, deployment_id, gate, exception_code,
                                approval_ref, reason, config_fingerprint, expires_at)
                         VALUES (%s,%s,%s,%s,%s,'owner-approved pilot exception (test)',%s,
                                 now() + make_interval(days => %s))""",
                (d["env"], d["dep"], gate, code, ref, "f" * 64, days))


def authorized(conn, env=PILOT, art=None, exceptions=None, ref=REF):
    """Record a deployment as the pipeline does: its manifest, then PASS evidence or an exception for every gate."""
    d = manifest(env, art)
    record(conn, d)
    exc = exceptions or {}
    for g in REQUIRED_GATES:
        if g in exc:
            exception(conn, d, g, exc[g], ref)
        else:
            evidence(conn, d, g)
    return d


def declare(conn, d=None, env=None, dep=None, art=None, corr="corr-test"):
    """What setuhaul/api/db.py sets in every transaction (session-level here, so a test can run several statements)."""
    vals = (env if env is not None else (d["env"] if d else ""), dep if dep is not None else (d["dep"] if d else ""),
            art if art is not None else (d["art"] if d else ""), corr)
    conn.execute("SELECT set_config('app.environment', %s, false), set_config('app.deployment_id', %s, false), "
                 "set_config('app.code_sha256', %s, false), set_config('app.correlation_id', %s, false)", vals)


def refused(fn, code):
    with pytest.raises(psycopg.Error) as e:
        fn()
    assert f"BOOKING_EVIDENCE_{code}" in str(e.value), str(e.value)
    return e.value


def book(conn, **kw):
    with conn.transaction():
        return confirmed(conn, **kw)


# ================================================================ positive
def test_a_current_deployment_with_pass_evidence_may_book(c, sch):
    d = authorized(c, TEST_ENV)
    declare(c, d)
    t = book(c, start_h=20)
    assert appt(c, t["aid"])[0] == "CONFIRMED"


def test_the_pilot_may_book_with_pass_evidence_plus_its_owner_approved_exceptions(c, sch):
    exc = dict(PILOT_EXC)
    if 170009 <= server_version(c) < 180000:            # T33-P holds only on PostgreSQL 17.9+ (CI); local 18.x: PASS
        exc["AURORA"] = "T33-P"
    d = authorized(c, PILOT, exceptions=exc)
    declare(c, d)
    book(c, start_h=21)


def test_exceptions_never_stand_in_outside_a_pilot_environment(c, sch):
    d = authorized(c, TEST_ENV, exceptions=PILOT_EXC)              # same three exception rows, non-pilot environment
    declare(c, d)
    e = refused(lambda: book(c, start_h=22), "EVIDENCE_NOT_CURRENT")
    assert "WORKLOAD_IDENTITY exception not valid here" in str(e) and "OUTBOUND_DELIVERY exception not valid here" in str(e)


def test_t33p_is_accepted_only_on_postgresql_17(c, sch):
    d = authorized(c, PILOT, exceptions={**PILOT_EXC, "AURORA": "T33-P"})
    declare(c, d)
    if 170009 <= server_version(c) < 180000:
        book(c, start_h=23)
    else:
        assert "AURORA exception not valid here" in str(refused(lambda: book(c, start_h=23), "EVIDENCE_NOT_CURRENT"))


# ================================================================ identity and evidence failures
def test_an_undeclared_or_partly_declared_caller_is_refused(c, sch):
    authorized(c, TEST_ENV)
    declare(c)                                                     # v0.3.0 code: declares nothing
    refused(lambda: book(c, start_h=24), "DEPLOYMENT_UNDECLARED")
    declare(c, env=TEST_ENV, dep="", art="")
    refused(lambda: book(c, start_h=24), "DEPLOYMENT_UNDECLARED")


def test_no_manifest_for_the_environment_is_refused(c, sch):
    d = manifest("never-deployed")
    declare(c, d)
    refused(lambda: book(c, start_h=25), "NO_MANIFEST")


def test_a_manifest_without_evidence_is_refused_gate_by_gate(c, sch):
    d = manifest(TEST_ENV)
    record(c, d)
    declare(c, d)
    e = refused(lambda: book(c, start_h=26), "EVIDENCE_NOT_CURRENT")
    assert all(f"{g} no evidence" in str(e) for g in REQUIRED_GATES)


def test_expired_evidence_is_refused(c, sch):
    d = manifest(TEST_ENV)
    record(c, d)
    for g in REQUIRED_GATES:
        evidence(c, d, g, expires="1 hour" if g != "RECOVERY_WORKER" else "1 second")
    time.sleep(1.2)
    declare(c, d)
    assert "RECOVERY_WORKER evidence expired" in str(refused(lambda: book(c, start_h=27), "EVIDENCE_NOT_CURRENT"))


def test_evidence_of_a_previous_deployment_does_not_authorize_the_next(c, sch):
    old = authorized(c, TEST_ENV)
    new = manifest(TEST_ENV)
    record(c, new)                                                 # the new release is recorded, no evidence yet
    declare(c, new)
    refused(lambda: book(c, start_h=28), "EVIDENCE_NOT_CURRENT")
    declare(c, old)                                                # a warm container of the old release
    refused(lambda: book(c, start_h=28), "DEPLOYMENT_SUPERSEDED")


def test_a_caller_on_other_code_is_refused(c, sch):
    d = authorized(c, TEST_ENV)
    declare(c, d, art="9" * 64)
    refused(lambda: book(c, start_h=29), "ARTIFACT_MISMATCH")


def test_evidence_of_another_environment_does_not_count(c, sch):
    d = manifest(TEST_ENV)
    record(c, d)
    other = {**d, "env": "other-env"}
    for g in REQUIRED_GATES:
        evidence(c, other, g)                                      # same deployment id, wrong environment
    declare(c, d)
    refused(lambda: book(c, start_h=30), "EVIDENCE_NOT_CURRENT")


def test_a_missing_or_inconsistent_owner_approval_is_refused(c, sch):
    d = manifest(PILOT)
    record(c, d)
    for g in REQUIRED_GATES:
        if g == "WORKLOAD_IDENTITY":
            exception(c, d, g, PILOT_EXC[g], REF)
        elif g == "OUTBOUND_DELIVERY":
            exception(c, d, g, PILOT_EXC[g], REF2)                  # a second, different approval
        else:
            evidence(c, d, g)
    declare(c, d)
    refused(lambda: book(c, start_h=31), "APPROVAL_INCONSISTENT")
    d2 = manifest(PILOT)
    record(c, d2)
    for g in REQUIRED_GATES:
        if g == "WORKLOAD_IDENTITY":
            exception(c, d2, g, PILOT_EXC[g])
        elif g != "OUTBOUND_DELIVERY":                             # OUTBOUND: neither evidence nor an approval
            evidence(c, d2, g)
    declare(c, d2)
    assert "OUTBOUND_DELIVERY no evidence" in str(refused(lambda: book(c, start_h=31), "EVIDENCE_NOT_CURRENT"))
    with pytest.raises(psycopg.errors.CheckViolation):            # a malformed approval cannot even be recorded
        exception(c, d2, "OUTBOUND_DELIVERY", PILOT_EXC["OUTBOUND_DELIVERY"], ref="approved-by-me")


def test_an_evidence_lookup_that_cannot_read_is_refused(dsn, sch):
    with psycopg.connect(dsn) as conn:                             # one transaction, rolled back
        d = authorized(conn, TEST_ENV)
        declare(conn, d)
        conn.execute("REVOKE SELECT ON setuhaul.deployment_evidence FROM setuhaul_rpc")
        refused(lambda: confirmed(conn, start_h=32), "ACCESS_DENIED")
        conn.rollback()


def test_a_database_error_during_verification_is_refused(dsn, sch):
    with psycopg.connect(dsn) as conn:
        d = authorized(conn, TEST_ENV)
        declare(conn, d)
        conn.execute("""CREATE OR REPLACE FUNCTION setuhaul.evidence_max_validity(p_gate text) RETURNS interval
                        LANGUAGE plpgsql IMMUTABLE AS $$ BEGIN RAISE EXCEPTION 'boom'; END $$""")
        refused(lambda: confirmed(conn, start_h=33), "VERIFICATION_FAILED")
        conn.rollback()                                            # the real function is back


def test_a_rollback_cannot_reuse_the_earlier_evidence_of_the_same_deployment(c, sch):
    a = authorized(c, TEST_ENV)
    b = authorized(c, TEST_ENV)                                    # release b, authorized
    record(c, a)                                                   # roll back to a: its manifest is recorded again
    declare(c, a)
    e = refused(lambda: book(c, start_h=34), "EVIDENCE_NOT_CURRENT")
    assert "evidence predates this deployment record" in str(e)
    declare(c, b)
    refused(lambda: book(c, start_h=34), "DEPLOYMENT_SUPERSEDED")
    for g in REQUIRED_GATES:                                       # fresh evidence for the rollback deployment
        evidence(c, a, g)
    declare(c, a)
    book(c, start_h=34)


def test_a_rejection_names_the_deployment_and_correlation_but_no_booking_data(c, sch):
    d = manifest(TEST_ENV)
    record(c, d)
    declare(c, d, corr="9b2d7e1c-0000-4000-8000-000000000001")
    e = refused(lambda: book(c, start_h=35), "EVIDENCE_NOT_CURRENT")
    hint = e.diag.message_hint
    assert f"deployment={d['dep'][:14]}" in hint and "correlation=9b2d7e1c-0000-4000-8000-000000000001" in hint
    assert "environment=test-env" in hint and "table=" in hint
    assert "SHP-" not in str(e) + hint and "+91" not in str(e) + hint


# ================================================================ expiry, clock and concurrency
def test_a_warm_container_cannot_book_after_its_evidence_expires(dsn, sch):
    """The process keeps the deployment it accepted at start-up (Db.deployment); the database decides each time."""
    from setuhaul.api.db import Db
    d = manifest(TEST_ENV)
    with psycopg.connect(dsn, autocommit=True) as conn:
        record(conn, d)
        for g in REQUIRED_GATES:
            evidence(conn, d, g, expires="2 seconds" if g == "AWS_QUEUE" else "1 hour")
    db = Db(dsn)
    db.deployment = {"environment": d["env"], "deployment_id": d["dep"], "code_sha256": d["art"]}
    with db.tx("system") as t:                                     # the declaration reaches the database
        assert t.execute("SELECT current_setting('app.deployment_id'), current_setting('app.code_sha256'), "
                         "current_setting('app.environment')").fetchone() == (d["dep"], d["art"], d["env"])
    with psycopg.connect(dsn, autocommit=True) as conn:
        declare(conn, d)
        book(conn, start_h=36)                                     # before expiry: accepted
        time.sleep(2.3)
        assert "AWS_QUEUE evidence expired" in str(refused(lambda: book(conn, start_h=37), "EVIDENCE_NOT_CURRENT"))


@pytest.mark.parametrize("offset, ok", [("-1 microsecond", True), ("0", False), ("1 microsecond", False)])
def test_expiry_is_exclusive_at_the_database_instant(c, offset, ok):
    r = c.execute("""SELECT setuhaul.booking_evidence_current('AWS_QUEUE', t - interval '1 hour', t, t - interval '2 hours',
                                                             t + %s::interval)
                       FROM (SELECT clock_timestamp() AS t) x""", (offset,)).fetchone()[0]
    assert r is ok


def test_the_guard_reads_the_database_clock_not_the_transaction_start(dsn, sch):
    """A transaction that began while evidence was current and commits a booking after it expired is refused."""
    d = manifest(TEST_ENV)
    with psycopg.connect(dsn, autocommit=True) as conn:
        record(conn, d)
        for g in REQUIRED_GATES:
            evidence(conn, d, g, expires="2 seconds" if g == "BOOKING_WORKFLOW" else "1 hour")
    with psycopg.connect(dsn) as conn:
        declare(conn, d)
        conn.execute("SELECT now()")                               # the transaction starts now; evidence is current
        time.sleep(2.3)
        refused(lambda: confirmed(conn, start_h=38), "EVIDENCE_NOT_CURRENT")
        conn.rollback()


def waiting_on_advisory_lock(conn, pid) -> bool:
    return bool(conn.execute("""SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND NOT granted""",
                             (pid,)).fetchone())


def wait_until(pred, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_a_new_manifest_waits_for_an_in_flight_commitment(dsn, sch):
    d = authorized(psycopg.connect(dsn, autocommit=True), TEST_ENV)
    booking = psycopg.connect(dsn)
    declare(booking, d)
    confirmed(booking, start_h=39)                                 # commitment made, transaction still open
    done = {}

    def record_next():
        with psycopg.connect(dsn, autocommit=True) as p:
            done["pid"] = p.info.backend_pid
            record(p, manifest(TEST_ENV))
            done["ok"] = True
    th = threading.Thread(target=record_next)
    th.start()
    with psycopg.connect(dsn, autocommit=True) as watch:
        assert wait_until(lambda: "pid" in done and waiting_on_advisory_lock(watch, done["pid"]))
        assert "ok" not in done                                    # the manifest is blocked behind the commitment
        booking.commit()
        th.join(10)
    booking.close()
    assert done.get("ok")


def test_a_commitment_that_races_a_new_manifest_sees_it_and_is_refused(dsn, sch):
    d = authorized(psycopg.connect(dsn, autocommit=True), TEST_ENV)
    pipeline = psycopg.connect(dsn)                                # a transaction left open by the pipeline
    nxt = manifest(TEST_ENV)
    pipeline.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
    pipeline.execute("""INSERT INTO setuhaul.deployment_manifests (environment, deployment_id, artifact_sha256, manifest)
                        VALUES (%s,%s,%s,%s::jsonb)""", (nxt["env"], nxt["dep"], nxt["art"], json.dumps(nxt["m"])))
    out = {}

    def commit_old():
        with psycopg.connect(dsn, autocommit=True) as b:
            out["pid"] = b.info.backend_pid
            declare(b, d)
            try:
                book(b, start_h=40)
                out["result"] = "booked"
            except psycopg.Error as e:
                out["result"] = str(e)
    th = threading.Thread(target=commit_old)
    th.start()
    with psycopg.connect(dsn, autocommit=True) as watch:
        assert wait_until(lambda: "pid" in out and waiting_on_advisory_lock(watch, out["pid"]))
    pipeline.commit()                                              # the new manifest lands while the commitment waits
    th.join(10)
    pipeline.close()
    assert "BOOKING_EVIDENCE_DEPLOYMENT_SUPERSEDED" in out.get("result", ""), out


def test_a_commitment_outside_read_committed_is_refused(dsn, sch):
    d = authorized(psycopg.connect(dsn, autocommit=True), TEST_ENV)
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        declare(conn, d)
        refused(lambda: confirmed(conn, start_h=41), "ISOLATION")
        conn.rollback()


# ================================================================ partial deployment
@pytest.mark.parametrize("stale", ["agent", "api"])
def test_a_partly_updated_deployment_is_refused_for_the_stale_side(c, sch, stale):
    """The pipeline updated one function but not the other: the stale one still names the previous deployment and
    code (it found that manifest at start-up). Whichever side it is, its commitments are refused."""
    h = 42 if stale == "agent" else 56
    old = authorized(c, TEST_ENV)
    new = authorized(c, TEST_ENV)
    declare(c, old)                                                # the stale side, as it started
    refused(lambda: book(c, start_h=h), "DEPLOYMENT_SUPERSEDED")
    declare(c, new, art=old["art"])                                # names the new deployment, runs the old code
    refused(lambda: book(c, start_h=h), "ARTIFACT_MISMATCH")
    declare(c, new)                                                # the updated side
    book(c, start_h=h)


def test_a_deployment_whose_manifest_was_never_recorded_cannot_book(c, sch):
    authorized(c, TEST_ENV)
    unrecorded = manifest(TEST_ENV)                                # deployed, manifest step never ran
    declare(c, unrecorded)
    refused(lambda: book(c, start_h=43), "DEPLOYMENT_SUPERSEDED")


# ================================================================ kill switch and environments
def test_the_kill_switch_still_refuses_first_even_with_valid_evidence(c, sch):
    d = authorized(c, TEST_ENV)
    declare(c, d)
    c.execute("UPDATE setuhaul.booking_kill_switch SET engaged=true, changed_by='test', reason='test engage'")
    try:
        with pytest.raises(psycopg.Error, match="BOOKING_KILLED"):
            book(c, start_h=44)
    finally:
        c.execute("UPDATE setuhaul.booking_kill_switch SET engaged=false, changed_by='test', reason='test cleanup'")
    book(c, start_h=44)


def test_production_is_refused_whatever_its_evidence(c, sch):
    d = manifest("prod-ap-south-1")
    record(c, d)
    for g in REQUIRED_GATES:
        evidence(c, d, g)
    declare(c, d)
    refused(lambda: book(c, start_h=45), "PRODUCTION_DISABLED")
    with pytest.raises(psycopg.errors.CheckViolation):            # no exception can even be recorded for it
        exception(c, d, "OUTBOUND_DELIVERY", PILOT_EXC["OUTBOUND_DELIVERY"])


PRODUCTION_NAMES = ["prod", "production", "PROD", "Prod", "Production", "PRODUCTION", "pRoD", "ProDuction",
                    "prod-ap-south-1", "Prod-AP-South-1", "my-production", "my-PRODUCTION-2", "preprod", "pilot-prod"]


@pytest.mark.parametrize("name", PRODUCTION_NAMES)
def test_production_in_any_letter_case_is_refused_even_with_otherwise_valid_evidence(c, sch, name):
    """Review fix (PR #6): production is recognised case-insensitively and refused before anything else. The evidence
    tables accept only canonical lower-case names, so a mixed-case name can never have evidence of its own: the test
    records complete, current evidence for its lower-case twin and declares the mixed-case name with that deployment."""
    canonical = name.lower()
    d = manifest(canonical)
    record(c, d)
    for g in REQUIRED_GATES:
        evidence(c, d, g)                                          # complete, current PASS evidence
    declare(c, d, env=name)
    refused(lambda: book(c, start_h=60), "PRODUCTION_DISABLED")
    if name != canonical:                                          # a mixed-case name cannot be recorded at all
        with pytest.raises(psycopg.errors.CheckViolation):
            record(c, {**manifest(canonical), "env": name, "m": {**d["m"], "environment": name}})
    with pytest.raises(psycopg.errors.CheckViolation):            # and no exception for any production spelling
        exception(c, {**d, "env": canonical}, "OUTBOUND_DELIVERY", PILOT_EXC["OUTBOUND_DELIVERY"])
    assert c.execute("SELECT setuhaul.booking_exception_current(%s,'OUTBOUND_DELIVERY','META-DEFERRED-TEST-SINK-ONLY',"
                     "%s, now(), now() + interval '1 day', now() - interval '1 hour', now())",
                     (name, REF)).fetchone()[0] is False


@pytest.mark.parametrize("name, cls", [("pilot-ap-southeast-2", "pilot"), ("test-env", "other"), ("PROD", "production"),
                                       ("Pilot-ap-southeast-2", "invalid"), ("PILOT", "invalid"), ("Test-Env", "invalid"),
                                       ("pilot env", "invalid"), ("", "invalid"), (None, "invalid"),
                                       ("pilot-Production", "production"), ("x" * 33, "invalid")])
def test_the_environment_classification_is_strict(c, name, cls):
    assert c.execute("SELECT setuhaul.booking_environment_class(%s)", (name,)).fetchone()[0] == cls


@pytest.mark.parametrize("name", ["Test-Env", "PILOT-AP-SOUTHEAST-2", "Pilot-ap-southeast-2"])
def test_a_non_canonical_environment_is_refused_even_with_valid_evidence_for_its_twin(c, sch, name):
    d = authorized(c, name.lower(), exceptions=PILOT_EXC if name.lower().startswith("pilot") else None)
    h = 61 + 2 * ["Test-Env", "PILOT-AP-SOUTHEAST-2", "Pilot-ap-southeast-2"].index(name)
    declare(c, d, env=name)
    refused(lambda: book(c, start_h=h), "ENVIRONMENT_INVALID")
    declare(c, d)                                                  # the canonical name with the same evidence books
    book(c, start_h=h)


def test_an_exception_cannot_be_widened_through_evidence_data(c):
    d = manifest(PILOT)
    record(c, d)
    with pytest.raises(psycopg.errors.CheckViolation):            # another gate, or another code: refused at insert
        exception(c, d, "AWS_QUEUE", "T33-P")
    with pytest.raises(psycopg.errors.CheckViolation):
        exception(c, d, "OUTBOUND_DELIVERY", "WHATSAPP-ON")
    ok = c.execute("SELECT setuhaul.booking_exception_current('pilot-x','BOOKING_WORKFLOW','T33-P',%s, now(), "
                   "now() + interval '1 day', now() - interval '1 hour', now())", (REF,)).fetchone()[0]
    assert ok is False                                             # and the guard re-checks the pair itself


# ================================================================ lifecycle: reductions and metadata are never blocked
def test_reductions_progressions_and_metadata_need_no_evidence(dsn, sch):
    with psycopg.connect(dsn, autocommit=True) as conn:
        d = authorized(conn, TEST_ENV)
        declare(conn, d)
        a, b, x = book(conn, start_h=46), book(conn, start_h=47, dock=S2), book(conn, start_h=48)
        nxt = manifest(TEST_ENV)
        record(conn, nxt)                                          # from here on, nothing authorizes a commitment
        declare(conn)                                              # and the caller declares nothing at all
        conn.execute("UPDATE setuhaul.appointments SET at_risk = true WHERE appointment_id=%s", (a["aid"],))
        conn.execute("UPDATE setuhaul.appointments SET status='IN_PROGRESS' WHERE appointment_id=%s", (a["aid"],))
        conn.execute("UPDATE setuhaul.appointments SET status='COMPLETED' WHERE appointment_id=%s", (a["aid"],))
        conn.execute("UPDATE setuhaul.appointments SET status='CANCELLED', is_current=false WHERE appointment_id=%s",
                     (b["aid"],))
        conn.execute("UPDATE setuhaul.dock_occupancy SET active=false WHERE ref_id=%s", (b["aid"],))
        conn.execute("UPDATE setuhaul.appointments SET status='EXPIRED', is_current=false WHERE appointment_id=%s",
                     (x["aid"],))
        conn.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id, facility_id, during, kind, ref_id, created_by)
                        VALUES (%s, %s, tstzrange(now() + interval '90 h', now() + interval '91 h'), 'BLOCK',
                                'blk-guard', 'test')""", (S2, SCH))      # an operational block is not a booking
        # ...but nothing that commits again gets through
        refused(lambda: conn.execute("UPDATE setuhaul.appointments SET status='CONFIRMED' WHERE appointment_id=%s",
                                     (b["aid"],)), "DEPLOYMENT_UNDECLARED")
        refused(lambda: conn.execute("UPDATE setuhaul.dock_occupancy SET active=true WHERE ref_id=%s", (b["aid"],)),
                "DEPLOYMENT_UNDECLARED")
        refused(lambda: conn.execute("""UPDATE setuhaul.dock_occupancy SET during = tstzrange(lower(during) + interval
                                        '1 h', upper(during) + interval '1 h') WHERE ref_id=%s""", (x["aid"],)),
                "DEPLOYMENT_UNDECLARED")


def test_a_reschedule_through_the_console_is_a_guarded_commitment(dsn, sch):
    with psycopg.connect(dsn, autocommit=True) as conn:
        d = authorized(conn, TEST_ENV)
        declare(conn, d)
        t = book(conn, dock=S1, start_h=50)
        pid = store(conn, [move_row(t, S1, at(51))])
        declare(conn)                                              # an ops_lead on a process that declares nothing
        refused(lambda: move(conn, pid, t["aid"]), "DEPLOYMENT_UNDECLARED")
        assert appt(conn, t["aid"])[0] == "CONFIRMED" and appt(conn, t["aid"])[3] == t["start"]
        declare(conn, d)                                           # the current deployment: the move goes through
        r = move(conn, pid, t["aid"])
        assert r["status"] in ("MOVED", "OK", "APPLIED") or r.get("appointment_id"), r


# ================================================================ security
def test_no_role_can_call_the_guard_functions_or_switch_the_guard(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        f = conn.execute("""SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner),
                                   has_function_privilege('public', p.oid, 'EXECUTE')
                              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                             WHERE n.nspname='setuhaul' AND p.proname IN ('booking_evidence_verdict',
                                   'trg_require_booking_evidence', 'booking_evidence_current', 'booking_exception_current',
                                   'trg_manifest_waits_for_commitments', 'trg_booking_evidence_guard_one_way',
                                   'booking_environment_class')""").fetchall()
        by = {r[0]: r[1:] for r in f}
        assert len(by) == 7
        assert by["booking_evidence_verdict"][:2] == (True, "setuhaul_rpc")
        assert by["trg_require_booking_evidence"][:2] == (True, "setuhaul_rpc")
        assert not any(r[2] for r in by.values())                  # nothing executable by PUBLIC
        assert conn.execute("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname='setuhaul_rpc'").fetchone() == (False, False)
        for role in ("setuhaul_console", "setuhaul_agent", "setuhaul_system", "setuhaul_ingest", "setuhaul_feasibility",
                     "setuhaul_deploy_evidence"):
            assert conn.execute("SELECT has_function_privilege(%s, 'setuhaul.booking_evidence_verdict(text,text,text,"
                                "timestamptz)', 'EXECUTE')", (role,)).fetchone()[0] is False, role
            assert conn.execute("SELECT has_table_privilege(%s, 'setuhaul.booking_evidence_guard', 'UPDATE')",
                                (role,)).fetchone()[0] is False, role
    with psycopg.connect(dsn, autocommit=True) as conn:            # operations staff cannot turn it off
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with as_role(conn, "setuhaul_console", SCH) as s:
                s.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = false, reason = 'turn it off'")
        with pytest.raises(psycopg.Error, match="EVIDENCE_GUARD_ONE_WAY"):          # nor can the owner
            conn.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = false, reason = 'turn it off'")
        with pytest.raises(psycopg.Error, match="EVIDENCE_GUARD_SINGLE_ROW"):
            conn.execute("DELETE FROM setuhaul.booking_evidence_guard")
        with pytest.raises(psycopg.Error, match="APPEND_ONLY"):
            conn.execute("TRUNCATE setuhaul.booking_evidence_guard")
        assert conn.execute("SELECT enforced FROM setuhaul.booking_evidence_guard").fetchone()[0] is True


def test_valid_evidence_does_not_widen_facility_scope(dsn, sch):
    """Evidence authorizes a deployment, not a facility: a planner of another facility still cannot move a booking."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        d = authorized(conn, TEST_ENV)
        declare(conn, d)
        t = book(conn, dock=S1, start_h=52)
        pid = store(conn, [move_row(t, S1, at(53))])
        with pytest.raises(psycopg.Error):
            move(conn, pid, t["aid"], facilities=("FAC-GGN-01",))
        assert appt(conn, t["aid"])[3] == t["start"]


def test_the_guard_checks_the_same_gates_as_the_start_up_check(dsn):
    with psycopg.connect(dsn) as conn:
        # 0032 moved the gate loop into booking_gate_problems, which the verdict (and the Option P issuer) call
        verdict = [r[0] for r in conn.execute("SELECT prosrc FROM pg_proc WHERE proname='booking_evidence_verdict'")]
        assert any("booking_gate_problems(" in v for v in verdict)
        src = conn.execute("SELECT prosrc FROM pg_proc WHERE proname='booking_gate_problems'").fetchone()[0]
    gates = src.split("ARRAY[", 1)[1].split("]", 1)[0].replace("'", "").replace("\n", " ").split(",")
    assert tuple(g.strip() for g in gates) == REQUIRED_GATES


def test_the_guard_uses_the_kill_switch_definition_of_a_commitment(dsn):
    with psycopg.connect(dsn) as conn:
        kill = conn.execute("SELECT prosrc FROM pg_proc WHERE proname='trg_refuse_booking_when_killed'").fetchone()[0]
        guard = conn.execute("SELECT prosrc FROM pg_proc WHERE proname='trg_require_booking_evidence'").fetchone()[0]

    def rule(src):
        body = src.split("IF TG_TABLE_NAME = 'dock_occupancy' THEN", 1)[1].split("END IF;", 1)[0]
        return " ".join(ln.split("--")[0].strip() for ln in body.splitlines() if ln.split("--")[0].strip())
    assert rule(kill) == rule(guard)
    names = [r[0] for r in psycopg.connect(dsn).execute(
        """SELECT tgname FROM pg_trigger WHERE tgrelid = 'setuhaul.appointments'::regclass AND NOT tgisinternal
            AND tgname LIKE 'a%%' ORDER BY tgname""").fetchall()]
    assert names[:2] == ["aa_booking_kill_switch", "ab_booking_evidence_guard"]   # kill switch first


# ================================================================ migration: on where a deployment is hosted, off elsewhere
def test_0030_turns_the_guard_on_only_where_a_deployment_is_recorded(tmp_path):
    from conftest import _pg_available
    from setuhaul.db.migrate import migrate
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable")
    name = "setuhaul_build_guard_mig"
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c0:
        c0.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c0.execute(f"CREATE DATABASE {name}")
    d = f"{BASE_DSN} dbname={name}"
    before = tmp_path / "m"
    before.mkdir()
    for p in sorted((ROOT / "migrations").glob("*.sql")):
        if p.name < "0030":
            shutil.copy(p, before / p.name)
    migrate(d, before)
    with psycopg.connect(d, autocommit=True) as conn:              # the pilot's situation: a deployment is recorded
        record(conn, manifest(PILOT))
    assert "0030_booking_evidence_guard" in migrate(d)
    with psycopg.connect(d, autocommit=True) as conn:
        row = conn.execute("SELECT enforced, reason FROM setuhaul.booking_evidence_guard").fetchone()
        assert row[0] is True and "hosts a" in row[1]          # 0030's reason, or 0032's when it also enforces M2
        declare(conn)                                              # v0.3.0 code declares nothing: refused at once
        with pytest.raises(psycopg.Error, match="BOOKING_EVIDENCE_DEPLOYMENT_UNDECLARED"):
            conn.execute("""INSERT INTO setuhaul.dock_occupancy (dock_id, facility_id, during, kind, ref_id, created_by)
                            VALUES ('DOCK-X', 'FAC-X', tstzrange(now(), now() + interval '1 h'), 'HOLD', 'h', 't')""")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c0:
        c0.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


# ================================================================ the application declares its deployment
def test_both_database_backends_declare_the_deployment_in_every_transaction(dsn):
    from data_api_emulator import ALL_ROLES, EmulatedRdsData
    from setuhaul.api.db import CORRELATION_ID, Db
    from setuhaul.aws.data_api import DataApiDb
    dep = {"environment": TEST_ENV, "deployment_id": "m-" + "a" * 40, "code_sha256": "b" * 64}
    q = ("SELECT current_setting('app.environment'), current_setting('app.deployment_id'), "
         "current_setting('app.code_sha256'), current_setting('app.correlation_id')")
    tok = CORRELATION_ID.set("corr-xyz")
    try:
        for db in (Db(dsn), DataApiDb("arn:aws:rds:x:000000000000:cluster:c", DB, ALL_ROLES, EmulatedRdsData(dsn))):
            with db.tx("system") as t:
                assert t.execute(q).fetchone() == ("", "", "", "corr-xyz")          # nothing declared yet
            db.deployment = dep
            with db.tx("agent", ("FAC-JAI-01",)) as t:
                assert t.execute(q).fetchone() == (TEST_ENV, "m-" + "a" * 40, "b" * 64, "corr-xyz")
    finally:
        CORRELATION_ID.reset(tok)


def test_a_guard_refusal_reaches_the_driver_and_the_planner_like_a_kill_switch_refusal():
    """The runbook's promise for the rollout pause: the agent's booking tool gets ERROR with the guard's code (and so
    escalates to a person, as for BOOKING_KILLED); the console maps it to a 400 naming the code."""
    from setuhaul.domain.booking import map_db_error
    msg = "BOOKING_EVIDENCE_DEPLOYMENT_UNDECLARED: the transaction does not name its environment, deployment and code"
    assert map_db_error("P0001", msg) == map_db_error("P0001", "BOOKING_KILLED: x") == "ERROR"
    assert msg.split(":")[0] == "BOOKING_EVIDENCE_DEPLOYMENT_UNDECLARED"


def test_the_app_declares_the_deployment_its_start_up_check_accepted(dsn, caplog):
    import logging
    from test_inbound_delivery import FakeSqs, URL, all_gates, app_db, booking_app, manifest as delivery_manifest
    from setuhaul.api.inbound import SqsFifoQueue, database_fingerprint
    man = delivery_manifest()
    q = SqsFifoQueue(URL, FakeSqs())
    all_gates(dsn, man, q.fingerprint(), database_fingerprint(app_db(dsn)))
    with caplog.at_level(logging.INFO, logger="setuhaul.api"):
        app = booking_app(dsn, q, man=man)
    art = man["gates"]["RECOVERY_WORKER"]["artifact_sha256"]
    assert app.state.service.db.deployment == {"environment": "test-env", "deployment_id": deployment_id_for(man),
                                               "code_sha256": art}
    # the rollout runbook checks this line in the api's and the agent's logs after each container start
    line = f"booking evidence guard: this process declares environment=test-env deployment={deployment_id_for(man)} " \
           f"code={art[:12]}"
    assert any(r.getMessage() == line for r in caplog.records), [r.getMessage() for r in caplog.records]
