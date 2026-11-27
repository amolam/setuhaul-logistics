"""27 Nov fix plan, release ordering: the AURORA gate's trigger check is migration-aware.

The gate runs BEFORE the provisional activation, and the activation is the only step that applies this release's
migrations (0047, 0048). So the gate judges a trigger that 0048 introduces against the migration ledger:
  * 0048 not recorded as applied -> the pre-migration state is required: the trigger must be absent;
  * 0048 recorded as applied     -> the trigger is required exactly as listed (missing, disabled or altered = problem).
A missing trigger is never acceptable once its migration is recorded. Every other trigger stays unconditional."""
from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

from conftest import BASE_DSN, _pg_available
from setuhaul.db.migrate import migrate
from setuhaul.ops import evidence_triggers as ET

MIG = Path(__file__).resolve().parents[1] / "migrations"
ALL = {p.name[:4] for p in MIG.glob("[0-9][0-9][0-9][0-9]_*.sql")}
BEFORE_0048 = ALL - {"0048"}
TRIGGER = "dock_occupancy.no_show_blocks_hold"

pytestmark = pytest.mark.skipif(not _pg_available(), reason="needs the local PostgreSQL")


def ledger(cx) -> set[str]:
    """As the gate reads it: the migration numbers in setuhaul_meta.schema_migrations."""
    return {v.split("_", 1)[0] for (v,) in cx.execute("SELECT version FROM setuhaul_meta.schema_migrations")}


def test_only_the_0048_trigger_depends_on_the_ledger():
    assert ET.INTRODUCED_BY == {("dock_occupancy", "no_show_blocks_hold"): "0048"}
    assert all(n in ET.EXPECTED[t] for t, n in ET.INTRODUCED_BY)               # still in the full inventory
    assert ET.expected_for(None) is ET.EXPECTED                                # no ledger: everything required
    assert "no_show_blocks_hold" not in ET.expected_for(BEFORE_0048)["dock_occupancy"]
    assert ET.expected_for(ALL) == ET.EXPECTED
    assert ET.pending_triggers(BEFORE_0048) == [f"{TRIGGER} (migration 0048 not yet applied)"]
    assert ET.pending_triggers(ALL) == []


def test_before_and_after_0048_on_a_real_upgrade(tmp_path):
    old = tmp_path / "upto0047"
    old.mkdir()
    for p in MIG.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if p.name[:4] <= "0047":
            shutil.copy(p, old / p.name)
    db = "setuhaul_build_gate_0048"
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as cx:
        cx.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        cx.execute(f"CREATE DATABASE {db}")
    d = f"{BASE_DSN} dbname={db}"
    try:
        migrate(d, old)
        with psycopg.connect(d, autocommit=True) as cx:
            applied = ledger(cx)
            assert "0048" not in applied and "0047" in applied
            rows = cx.execute(ET.QUERY).fetchall()
            # the pre-activation gate: the pre-migration state passes ...
            assert ET.trigger_problems(rows, applied) == []
            # ... and the old, ledger-blind check would have failed it (the circular dependency this removes)
            assert ET.trigger_problems(rows) == [f"{TRIGGER}: missing"]
            # the pre-migration state is required: the trigger by hand, before its migration is recorded, fails
            with cx.transaction():
                cx.execute("""CREATE FUNCTION setuhaul.trg_no_show_blocks_booking() RETURNS trigger LANGUAGE plpgsql
                              AS $$ BEGIN RETURN NEW; END $$""")
                cx.execute("""CREATE TRIGGER no_show_blocks_hold BEFORE INSERT ON setuhaul.dock_occupancy
                              FOR EACH ROW EXECUTE FUNCTION setuhaul.trg_no_show_blocks_booking()""")
                assert ET.trigger_problems(cx.execute(ET.QUERY).fetchall(), applied) == [
                    f"{TRIGGER}: present, but migration 0048 is not recorded as applied"]
                raise psycopg.Rollback()
        migrate(d)                                                               # what the activation run does
        with psycopg.connect(d, autocommit=True) as cx:
            applied = ledger(cx)
            assert "0048" in applied
            assert ET.trigger_problems(cx.execute(ET.QUERY).fetchall(), applied) == []
            assert ET.pending_triggers(applied) == []
            # recorded as applied: missing, disabled or altered is a failure, never "not applied yet"
            with cx.transaction():
                cx.execute("DROP TRIGGER no_show_blocks_hold ON setuhaul.dock_occupancy")
                assert ET.trigger_problems(cx.execute(ET.QUERY).fetchall(), applied) == [f"{TRIGGER}: missing"]
                raise psycopg.Rollback()
            with cx.transaction():
                cx.execute("ALTER TABLE setuhaul.dock_occupancy DISABLE TRIGGER no_show_blocks_hold")
                assert ET.trigger_problems(cx.execute(ET.QUERY).fetchall(), applied) == [
                    f"{TRIGGER}: disabled (tgenabled 'D')"]
                raise psycopg.Rollback()
    finally:
        with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as cx:
            cx.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_other_triggers_stay_unconditional_whatever_the_ledger_says():
    """An empty or partial ledger never excuses a trigger older than this release."""
    rows = [(t, n, "O", fn, ty) for t, want in ET.EXPECTED.items() for n, (fn, ty) in want.items()
            if (t, n) != ("deployment_evidence", "aa_evidence_requires_guard")
            and (t, n) not in ET.INTRODUCED_BY]
    assert ET.trigger_problems(rows, set()) == ["deployment_evidence.aa_evidence_requires_guard: missing"]


# ================================================================ the gate itself (AWS faked; the ledger as on the pilot)
def _collector():
    import importlib.util
    spec = importlib.util.spec_from_file_location("step6_collector_0048", MIG.parent / "scripts" / "step6_gate_evidence.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Rds:
    def describe_db_clusters(self, **_):
        return {"DBClusters": [{"Engine": "aurora-postgresql", "EngineVersion": "18.3"}]}


def _gate(monkeypatch, versions, trigger_rows):
    m = _collector()

    class Owner:
        def __init__(self, *_):
            pass

        def query(self, sql):
            if "schema_migrations" in sql:
                return [(v, m.sha((MIG / f"{v}.sql").read_bytes())) for v in versions]
            if "pg_trigger" in sql:
                return trigger_rows
            if "relrowsecurity" in sql:
                return [(10, 10)]
            return [("18.3",)]

    monkeypatch.setattr(m, "c", lambda name: _Rds())
    monkeypatch.setattr(m, "OwnerSession", Owner)
    ev, checks, _, _ = m.gate_aurora({"ClusterArn": "arn:test", "OwnerSecretArn": "arn:test-secret"})
    return ev, checks


KEY = "evidence and booking guard triggers: exactly the expected set, enabled and configured"


def test_the_gate_before_activation_passes_without_the_0048_trigger(monkeypatch):
    stems = sorted(p.stem for p in MIG.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    full = [(t, n, "O", fn, ty) for t, want in ET.EXPECTED.items() for n, (fn, ty) in want.items()]
    pre = [r for r in full if (r[0], r[1]) not in ET.INTRODUCED_BY]
    ev, checks = _gate(monkeypatch, [s for s in stems if s[:4] != "0048"], pre)
    assert checks[KEY] is True and ev["repo_only_migrations"] == ["0048"]
    assert ev["evidence_triggers_pending_migration"] == [f"{TRIGGER} (migration 0048 not yet applied)"]
    # the same database state with 0048 recorded as applied: a FAIL, never "pending"
    ev, checks = _gate(monkeypatch, stems, pre)
    assert checks[KEY] is False and ev["evidence_trigger_problems"] == [f"{TRIGGER}: missing"]
    assert ev["evidence_triggers_pending_migration"] == []
    # after the activation applied 0048 and its trigger: passes
    ev, checks = _gate(monkeypatch, stems, full)
    assert checks[KEY] is True and ev["evidence_trigger_problems"] == []
