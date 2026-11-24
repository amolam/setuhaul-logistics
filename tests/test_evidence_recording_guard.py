"""M1 (PR #6 LLD review, migration 0031): no deployment evidence is recorded while the booking evidence guard is off.

A fresh database (guard OFF) must fail closed: the pipeline cannot record a manifest, gate evidence or an owner-approved
exception there, so the start-up check never finds evidence and the booking switch never starts, until the owner takes
the one-way step that turns the guard on. Runs on its own databases: the guard is one-way, so a test that turns it on
cannot share a database with a test that needs it off."""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import psycopg
import pytest

from conftest import BASE_DSN, _pg_available, _seed
from setuhaul.api.inbound import GUARD_TRIGGERS, REQUIRED_GATES, guards_enabled
from setuhaul.ops import gate_evidence as ge
from test_booking_evidence_guard import PILOT, REF, as_pipeline, evidence, exception, manifest, record

ROOT = Path(__file__).resolve().parents[1]
TABLES = ("deployment_manifests", "deployment_evidence", "deployment_exceptions")
APP_ROLES = ("setuhaul_agent", "setuhaul_console", "setuhaul_ingest", "setuhaul_feasibility", "setuhaul_system")
LOCK_KEY = "hashtextextended('setuhaul.booking_evidence_guard', 0)"


def fresh_db(name: str, folder: Path | None = None) -> str:
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    from setuhaul.db.migrate import migrate
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {name}")
    d = f"{BASE_DSN} dbname={name}"
    migrate(d, folder) if folder else migrate(d)
    _seed(d)
    return d


@pytest.fixture(scope="module")
def off_dsn():
    """A fresh database exactly as migrations leave it: no recorded deployment, guard OFF. Never turned on here."""
    return fresh_db("setuhaul_build_m1_off")


@pytest.fixture(scope="module")
def on_dsn():
    d = fresh_db("setuhaul_build_m1_on")
    with psycopg.connect(d, autocommit=True) as c:
        c.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'test: owner enables the guard'")
    return d


def guard_on(conn) -> bool:
    return conn.execute("SELECT enforced FROM setuhaul.booking_evidence_guard").fetchone()[0]


def counts(conn) -> tuple[int, ...]:
    return tuple(conn.execute(f"SELECT count(*) FROM setuhaul.{t}").fetchone()[0] for t in TABLES)


def refused_off(fn):
    with pytest.raises(psycopg.Error) as e:
        fn()
    assert "EVIDENCE_GUARD_OFF" in str(e.value), str(e.value)
    return e.value


# ================================================================ fresh database: fails closed
def test_a_fresh_database_starts_with_the_guard_off_and_records_nothing(off_dsn):
    d = manifest(PILOT)
    with psycopg.connect(off_dsn, autocommit=True) as c:
        assert guard_on(c) is False
        refused_off(lambda: record(c, d))
        refused_off(lambda: evidence(c, d, "AWS_QUEUE"))
        refused_off(lambda: exception(c, d, "WORKLOAD_IDENTITY", "NO-LIVE-DRIFT-DETECTION"))
        assert counts(c) == (0, 0, 0)


def test_the_owner_cannot_record_evidence_either_while_the_guard_is_off(off_dsn):
    """The trigger fires for every role: the owner's path is to enable the guard first (one-way), not to write around
    it with ordinary DML."""
    d = manifest(PILOT)
    with psycopg.connect(off_dsn, autocommit=True) as c:
        assert c.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0] is True
        refused_off(lambda: c.execute(
            "INSERT INTO setuhaul.deployment_manifests (environment, deployment_id, artifact_sha256, manifest) "
            "VALUES (%s,%s,%s,%s::jsonb)", (d["env"], d["dep"], d["art"], json.dumps(d["m"]))))
        assert counts(c) == (0, 0, 0)


def test_a_full_pipeline_activation_on_a_fresh_database_writes_nothing(off_dsn):
    """gate_evidence.activate is one transaction: the manifest insert is refused first, so no evidence or exception is
    left behind, and the error names the cause."""
    d = manifest(PILOT)
    rows = [{"environment": PILOT, "deployment_id": d["dep"], "gate": g, "validity_days": 7,
             "artifact_uri": "s3://e/r.json", "evidence_sha256": "a" * 64, "config_fingerprint": "f" * 64}
            for g in REQUIRED_GATES]
    with psycopg.connect(off_dsn) as c:
        with pytest.raises(psycopg.Error, match="EVIDENCE_GUARD_OFF: deployment_manifests"):
            with c.transaction():
                c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
                ge.record_manifest(c, d["m"], PILOT)
                ge.write(c, rows)
        with pytest.raises(psycopg.Error, match="EVIDENCE_GUARD_OFF: deployment_evidence"):
            with c.transaction():                    # evidence alone (a renewal) is refused too
                c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
                ge.write(c, rows)
        assert counts(c) == (0, 0, 0)


def test_unauthorized_roles_are_still_refused_by_privilege_first(off_dsn, on_dsn):
    """The new trigger adds a refusal; it removes none. Application roles never had INSERT, guard on or off."""
    d = manifest(PILOT)
    for dsn_ in (off_dsn, on_dsn):
        with psycopg.connect(dsn_, autocommit=True) as c:
            for role in APP_ROLES + ("setuhaul_rpc",):
                for t in TABLES:
                    assert c.execute("SELECT has_table_privilege(%s, %s, 'INSERT')",
                                     (role, f"setuhaul.{t}")).fetchone()[0] is False, (role, t)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with c.transaction():
                        c.execute(f"SET LOCAL ROLE {role}")
                        c.execute("INSERT INTO setuhaul.deployment_evidence (environment, deployment_id, gate, expires_at,"
                                  " artifact_uri, evidence_sha256, config_fingerprint) VALUES (%s,%s,'AWS_QUEUE',"
                                  " now() + interval '1 day', 's3://x/y', %s, %s)", (PILOT, d["dep"], "a" * 64, "0" * 64))


# ================================================================ guard on: the pipeline records as before
def test_with_the_guard_on_the_pipeline_records_manifest_evidence_and_exceptions(on_dsn):
    with psycopg.connect(on_dsn, autocommit=True) as c:
        before = counts(c)
        d = manifest(PILOT)
        record(c, d)
        for g in REQUIRED_GATES:
            if g == "WORKLOAD_IDENTITY":
                exception(c, d, g, "NO-LIVE-DRIFT-DETECTION")
            else:
                evidence(c, d, g)
        assert counts(c) == (before[0] + 1, before[1] + len(REQUIRED_GATES) - 1, before[2] + 1)


def test_the_existing_evidence_rules_still_apply_with_the_guard_on(on_dsn):
    """0031 adds a precondition; the freshness (0014), production (0026) and append-only rules are unchanged."""
    d = manifest(PILOT)
    with psycopg.connect(on_dsn, autocommit=True) as c:
        record(c, d)
        with pytest.raises(psycopg.Error) as e:
            evidence(c, d, "AWS_QUEUE", expires="40 days")
        assert "EVIDENCE_GUARD_OFF" not in str(e.value)
        with pytest.raises(psycopg.Error, match="APPEND_ONLY"):
            c.execute("DELETE FROM setuhaul.deployment_manifests")


# ================================================================ the one-way step, concurrency and rollback
def test_turning_the_guard_on_is_atomic_with_what_the_transaction_records(tmp_path):
    """Uncommitted: another session still sees it OFF and is refused (it does not wait). Rolled back: nothing changed.
    Committed: recording works from then on, and it cannot be undone."""
    dsn_ = fresh_db("setuhaul_build_m1_switch")
    d = manifest(PILOT)
    with psycopg.connect(dsn_) as owner, psycopg.connect(dsn_, autocommit=True) as pipeline:
        pipeline.execute("SET lock_timeout = '3s'")
        # 1. the owner turns it on and records in the same transaction, then rolls back: nothing remains
        with pytest.raises(RuntimeError):
            with owner.transaction():
                owner.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'owner: first try'")
                record(owner, d)
                assert counts(owner)[0] == 1                 # visible inside the owner's own transaction
                raise RuntimeError("abandon")
        assert guard_on(pipeline) is False and counts(pipeline) == (0, 0, 0)
        refused_off(lambda: record(pipeline, d))
        # 2. uncommitted ON: the pipeline reads the committed OFF and is refused at once, without blocking
        owner.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'owner: enable'")
        t0 = time.monotonic()
        refused_off(lambda: record(pipeline, d))
        assert time.monotonic() - t0 < 2.0
        # 3. committed: the pipeline records
        owner.commit()
        record(pipeline, d)
        assert counts(pipeline)[0] == 1
        with pytest.raises(psycopg.Error, match="EVIDENCE_GUARD_ONE_WAY"):
            with owner.transaction():
                owner.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = false, reason = 'owner: undo it'")


def test_a_refused_manifest_never_takes_or_waits_for_the_manifest_lock(off_dsn):
    """aa_evidence_requires_guard fires before 0030's manifest lock: with a booking commitment holding the shared lock,
    a manifest on a guard-off database is refused at once instead of queueing behind it."""
    d = manifest(PILOT)
    held, release = threading.Event(), threading.Event()

    def commitment():
        with psycopg.connect(off_dsn) as c:
            c.execute(f"SELECT pg_advisory_xact_lock_shared({LOCK_KEY})")
            held.set()
            release.wait(10)

    t = threading.Thread(target=commitment)
    t.start()
    try:
        assert held.wait(10)
        with psycopg.connect(off_dsn, autocommit=True) as c:
            c.execute("SET lock_timeout = '2s'")
            t0 = time.monotonic()
            refused_off(lambda: record(c, d))                # not 55P03 lock_timeout
            assert time.monotonic() - t0 < 1.5
    finally:
        release.set()
        t.join(10)


def test_concurrent_pipeline_writers_are_all_refused_while_off(off_dsn):
    d = manifest(PILOT)
    errors: list[str] = []

    def writer(g):
        try:
            with psycopg.connect(off_dsn, autocommit=True) as c:
                evidence(c, d, g)
        except psycopg.Error as e:
            errors.append(str(e))

    ts = [threading.Thread(target=writer, args=(g,)) for g in REQUIRED_GATES for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert len(errors) == len(ts) and all("EVIDENCE_GUARD_OFF" in e for e in errors)
    with psycopg.connect(off_dsn) as c:
        assert counts(c)[1] == 0


# ================================================================ migration time and the fail-closed reading
def test_a_database_that_already_holds_evidence_has_its_guard_turned_on_by_the_migration(tmp_path):
    """Before 0031 a database could hold evidence but no manifest (0013 predates manifests): 0030 left its guard OFF.
    0031 turns it on, so 'evidence exists implies guard on' holds for every database."""
    pre = tmp_path / "migrations"
    pre.mkdir()
    for p in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if p.name < "0031":
            shutil.copy(p, pre / p.name)
    dsn_ = fresh_db("setuhaul_build_m1_legacy", folder=pre)
    d = manifest(PILOT)
    with psycopg.connect(dsn_, autocommit=True) as c:
        assert guard_on(c) is False
        evidence(c, d, "AWS_QUEUE")                          # allowed before 0031
        from setuhaul.db.migrate import migrate
        assert migrate(dsn_) == ["0031_evidence_recording_requires_guard"]
        assert guard_on(c) is True
        assert "0031" in c.execute("SELECT reason FROM setuhaul.booking_evidence_guard").fetchone()[0]


def test_an_unreadable_or_missing_guard_row_refuses_recording(tmp_path):
    """Only reachable by the owner disabling 0030's own triggers; the M1 trigger still reads a missing row as OFF."""
    dsn_ = fresh_db("setuhaul_build_m1_missing")
    d = manifest(PILOT)
    with psycopg.connect(dsn_) as c:
        c.execute("UPDATE setuhaul.booking_evidence_guard SET enforced = true, reason = 'test: owner enables the guard'")
        c.execute("ALTER TABLE setuhaul.booking_evidence_guard DISABLE TRIGGER USER")
        c.execute("DELETE FROM setuhaul.booking_evidence_guard")
        refused_off(lambda: record(c, d))
        c.rollback()


# ================================================================ least privilege and start-up visibility
def test_the_m1_trigger_adds_no_privilege_to_any_role(on_dsn):
    with psycopg.connect(on_dsn, autocommit=True) as c:
        f = c.execute("""SELECT p.prosecdef, pg_get_userbyid(p.proowner), has_function_privilege('public', p.oid, 'EXECUTE')
                           FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                          WHERE n.nspname = 'setuhaul' AND p.proname = 'trg_evidence_requires_guard'""").fetchall()
        assert f == [(True, "setuhaul_rpc", False)]
        assert c.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname='setuhaul_rpc'"
                         ).fetchone()[:2] == (False, False)
        for role in APP_ROLES + ("setuhaul_deploy_evidence",):
            assert c.execute("SELECT has_function_privilege(%s, 'setuhaul.trg_evidence_requires_guard()', 'EXECUTE')",
                             (role,)).fetchone()[0] is False, role
            for priv in ("SELECT", "UPDATE", "INSERT", "DELETE"):
                assert c.execute("SELECT has_table_privilege(%s, 'setuhaul.booking_evidence_guard', %s)",
                                 (role, priv)).fetchone()[0] is False, (role, priv)
        trig = c.execute("""SELECT r.relname, t.tgname, t.tgenabled FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid
                             WHERE t.tgname = 'aa_evidence_requires_guard' ORDER BY 1""").fetchall()
        assert trig == [(t, "aa_evidence_requires_guard", "O") for t in sorted(TABLES)]
        for t in TABLES:                                     # it is the first BEFORE INSERT trigger on each table
            first = c.execute("""SELECT t.tgname FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid
                                  WHERE r.relname = %s AND NOT t.tgisinternal ORDER BY t.tgname LIMIT 1""",
                              (t,)).fetchone()[0]
            assert first == "aa_evidence_requires_guard", t


def test_the_start_up_check_refuses_if_the_m1_trigger_is_missing_or_disabled():
    every = {"guards": [list(g) for g in GUARD_TRIGGERS]}
    assert guards_enabled(every)
    for t in TABLES:
        assert (t, "aa_evidence_requires_guard") in GUARD_TRIGGERS
        assert not guards_enabled({"guards": [list(g) for g in GUARD_TRIGGERS - {(t, "aa_evidence_requires_guard")}]})
    for t in ("dock_occupancy", "appointments"):
        assert not guards_enabled({"guards": [list(g) for g in GUARD_TRIGGERS - {(t, "ab_booking_evidence_guard")}]})
