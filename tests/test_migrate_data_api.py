"""E5: migrations over the RDS Data API give exactly the schema the psycopg runner gives.

A fresh database is migrated statement by statement through the Data API backend and the local emulator; its catalog
(tables, columns, functions and their bodies, constraints, indexes, triggers, policies, privileges) must equal that of
the suite's database, which `setuhaul.db.migrate` built. Also: re-running applies nothing; an edited migration is refused.
"""
from __future__ import annotations

import shutil

import psycopg
import pytest

from conftest import BASE_DSN
from data_api_emulator import EmulatedRdsData
from setuhaul.aws.migrate_data_api import OwnerSession, migrate_data_api, split_sql
from setuhaul.db.migrate import MIGRATIONS, MigrationError, discover

ARN, OWNER = "arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul", "arn:aws:secretsmanager:ap-southeast-2:0:secret:owner"
DBNAME = "setuhaul_dataapi_migrate_test"

CATALOG = """
SELECT 'rel', c.relname, c.relkind::text, coalesce(c.relacl::text, ''), c.relrowsecurity::text
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname IN ('setuhaul', 'setuhaul_meta')
UNION ALL
SELECT 'col', c.relname || '.' || a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull::text,
       coalesce(pg_get_expr(d.adbin, d.adrelid), '')
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE n.nspname IN ('setuhaul', 'setuhaul_meta') AND a.attnum > 0 AND NOT a.attisdropped
UNION ALL
SELECT 'fn', p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', md5(p.prosrc),
       coalesce(p.proacl::text, ''), p.prosecdef::text || array_to_string(coalesce(p.proconfig, '{}'), ',')
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'setuhaul'
UNION ALL
SELECT 'con', c.relname || '.' || k.conname, pg_get_constraintdef(k.oid), '', ''
  FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'setuhaul'
UNION ALL
SELECT 'idx', indexrelid::regclass::text, pg_get_indexdef(indexrelid), '', ''
  FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'setuhaul'
UNION ALL
SELECT 'trg', c.relname || '.' || t.tgname, t.tgenabled::text, pg_get_triggerdef(t.oid), ''
  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'setuhaul' AND NOT t.tgisinternal
UNION ALL
SELECT 'pol', c.relname || '.' || p.polname, coalesce(pg_get_expr(p.polqual, p.polrelid), ''),
       coalesce(pg_get_expr(p.polwithcheck, p.polrelid), ''), array_to_string(p.polroles::regrole[], ',')
  FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
UNION ALL
SELECT 'mig', version, sha256, '', '' FROM setuhaul_meta.schema_migrations
ORDER BY 1, 2, 3
"""


@pytest.fixture(scope="module")
def fresh(dsn):
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DBNAME}")
    d = f"{BASE_DSN} dbname={DBNAME}"
    yield d
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")


def catalog(d):
    with psycopg.connect(d, autocommit=True) as c:
        return c.execute(CATALOG).fetchall()


def test_split_respects_quotes_comments_and_function_bodies():
    sql = """SELECT 'a;b'; -- x; y
             CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $fn$ BEGIN RETURN 1; END $fn$;
             /* c; /* nested; */ */ SELECT "q;", $$ ; $$ ;
             ;  -- only a comment
             """
    assert [s.split()[0] for s in split_sql(sql)] == ["SELECT", "--", "/*"]
    assert len(split_sql(sql)) == 3 and "RETURN 1; END" in split_sql(sql)[1]


def test_every_migration_splits_into_complete_statements():
    for version, path, _ in discover():
        for s in split_sql(path.read_text(encoding="utf-8")):
            assert s.count("$$") % 2 == 0, (version, s[:80])


def test_data_api_migrations_build_exactly_the_same_schema(dsn, fresh):
    session = OwnerSession(EmulatedRdsData(fresh), ARN, OWNER)
    applied = migrate_data_api(session)
    assert applied == [v for v, _, _ in discover()]
    assert catalog(fresh) == catalog(dsn)
    assert migrate_data_api(session) == []                           # nothing left to apply


def test_an_edited_migration_is_refused_over_the_data_api_too(fresh, tmp_path):
    folder = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, folder)
    last = sorted(folder.glob("*.sql"))[-1]
    last.write_text(last.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="immutable"):
        migrate_data_api(OwnerSession(EmulatedRdsData(fresh), ARN, OWNER), folder)


# ================================================================ E5: one login per role
def test_each_login_is_a_member_of_exactly_one_application_role(dsn):
    import uuid
    from setuhaul.aws.provision_logins import provision
    u = uuid.uuid4().hex[:8]
    logins = {r: {"username": f"test_login_{u}_{r}", "password": f"pw-{uuid.uuid4().hex}{uuid.uuid4().hex}"}
              for r in ("agent", "console", "system")}
    session = OwnerSession(EmulatedRdsData(dsn), ARN, OWNER)
    try:
        assert provision(session, logins) == [v["username"] for v in logins.values()]
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"GRANT setuhaul_console TO test_login_{u}_agent")          # drift: a second role
        provision(session, logins)                                                # re-run: ALTER, and drift removed
        with psycopg.connect(dsn, autocommit=True) as c:
            rows = c.execute("""SELECT m.rolname, r.rolname, m.rolcanlogin, m.rolcreaterole, m.rolsuper
                                  FROM pg_auth_members a JOIN pg_roles m ON m.oid = a.member JOIN pg_roles r ON r.oid = a.roleid
                                 WHERE m.rolname LIKE %s ORDER BY 1""", (f"test_login_{u}_%",)).fetchall()
        assert {(m, r) for m, r, *_ in rows} == {(f"test_login_{u}_{k}", f"setuhaul_{k}") for k in logins}
        assert all(login and not cr and not su for _, _, login, cr, su in rows)
        agent_dsn = dsn.replace("user=postgres", f"user=test_login_{u}_agent password={logins['agent']['password']}")
        with psycopg.connect(agent_dsn, autocommit=True) as c:                    # the login cannot reach another role
            c.execute("SET ROLE setuhaul_agent")
            with pytest.raises(psycopg.Error):
                c.execute("SET ROLE setuhaul_console")
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            for v in logins.values():
                c.execute(f"DROP ROLE IF EXISTS {v['username']}")


def test_login_names_and_passwords_are_checked_before_anything_runs(dsn):
    from setuhaul.aws.provision_logins import provision
    session = OwnerSession(EmulatedRdsData(dsn), ARN, OWNER)
    with pytest.raises(ValueError):
        provision(session, {"agent": {"username": "postgres; DROP ROLE x", "password": "x" * 30}})
    with pytest.raises(ValueError):
        provision(session, {"agent": {"username": "setuhaul_login_agent", "password": "short"}})
    with pytest.raises(ValueError):
        provision(session, {"owner": {"username": "setuhaul_login_owner", "password": "x" * 30}})
