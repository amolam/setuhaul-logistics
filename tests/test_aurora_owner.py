"""Deployment blocker found before the first AWS deploy: the migrations, the login provisioning and the seed were only
ever run by a superuser, and Aurora's owner is not one (a CREATEROLE member of rds_superuser). As such a role, migration
0001 failed at once (`ALTER ROLE ... NOSUPERUSER`), then `ALTER ... OWNER TO setuhaul_rpc` (needs SET ROLE and CREATE
on the schema), then the seed (RLS is forced on the owner) and a re-run of the login provisioning.

The unit tests below run everywhere. The end-to-end test needs an EMPTY PostgreSQL cluster (roles are cluster-wide,
so the suite's own cluster cannot show a first-time role creation by a non-superuser): it runs when
SETUHAUL_TEST_FRESH_DSN names a superuser connection to such a cluster (CI starts one; locally any scratch cluster).
"""
from __future__ import annotations

import os
import re

import psycopg
import pytest

from setuhaul.aws.migrate_data_api import MigrationError, adapt_for_non_superuser

FRESH = os.environ.get("SETUHAUL_TEST_FRESH_DSN")
OWNER = "aurora_like_owner"


class FakeConn:
    def __init__(self, rows=None):
        self.sql, self.rows = [], rows or {}

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        row = self.rows.get(params[0]) if params else None

        class R:
            def fetchone(self_inner):
                return row
        return R()


def test_role_attribute_statements_are_checked_not_sent():
    conn = FakeConn({"setuhaul_agent": (False, False), "setuhaul_rpc": (False, False)})
    assert adapt_for_non_superuser(conn, "ALTER ROLE setuhaul_agent   NOSUPERUSER NOBYPASSRLS", {}) is None
    out = adapt_for_non_superuser(conn, "ALTER ROLE setuhaul_rpc     NOSUPERUSER NOBYPASSRLS NOLOGIN", {})
    assert out == "ALTER ROLE setuhaul_rpc NOLOGIN"                       # the clauses a non-superuser may set
    assert all("pg_roles" in s for s, _ in conn.sql)                       # only checks were sent


def test_a_role_that_really_has_superuser_or_bypassrls_stops_the_migration():
    with pytest.raises(MigrationError, match="only a superuser"):
        adapt_for_non_superuser(FakeConn({"setuhaul_agent": (True, False)}), "ALTER ROLE setuhaul_agent NOSUPERUSER NOBYPASSRLS", {})
    with pytest.raises(MigrationError):
        adapt_for_non_superuser(FakeConn({}), "ALTER ROLE setuhaul_missing NOSUPERUSER", {})


def test_create_is_granted_once_before_the_first_ownership_change():
    conn, state = FakeConn(), {}
    stmt = "ALTER FUNCTION place_hold(text) OWNER TO setuhaul_rpc"
    assert adapt_for_non_superuser(conn, stmt, state) == stmt
    adapt_for_non_superuser(conn, "ALTER FUNCTION other() OWNER TO setuhaul_rpc", state)
    assert [s for s, _ in conn.sql] == ["GRANT CREATE ON SCHEMA setuhaul TO setuhaul_rpc"] and state["create_granted"]
    plain = FakeConn()
    assert adapt_for_non_superuser(plain, "-- OWNER TO setuhaul_rpc in a comment\nSELECT 1", {}) .startswith("--")
    assert plain.sql == []                                                 # a comment does not trigger the grant


# ---------------------------------------------------------------- end to end on an empty cluster
@pytest.fixture(scope="module")
def fresh():
    if not FRESH:
        pytest.skip("SETUHAUL_TEST_FRESH_DSN not set (needs an empty PostgreSQL cluster)")
    try:
        psycopg.connect(f"{FRESH} dbname=postgres", connect_timeout=3).close()
    except psycopg.OperationalError:
        pytest.skip("SETUHAUL_TEST_FRESH_DSN not reachable")
    with psycopg.connect(f"{FRESH} dbname=postgres", autocommit=True) as c:
        for db in ("setuhaul", "setuhaul_ref"):
            c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        for (r,) in c.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'setuhaul\\_%'").fetchall():
            c.execute(f"DROP ROLE {r}")
        n = c.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'setuhaul\\_%'").fetchone()[0]
        if n:
            pytest.skip("the cluster already has setuhaul roles owned by other databases; it must be empty")
        c.execute(f"DROP ROLE IF EXISTS {OWNER}")
        c.execute(f"CREATE ROLE {OWNER} LOGIN CREATEROLE CREATEDB")         # Aurora's owner: not a superuser
        c.execute(f"CREATE DATABASE setuhaul OWNER {OWNER}")
    return re.sub(r"user=\S+", f"user={OWNER}", FRESH)


def test_migrations_logins_and_seed_run_as_a_non_superuser_owner_and_give_the_superuser_catalog(fresh):
    from data_api_emulator import EmulatedRdsData
    from setuhaul.aws.migrate_data_api import OwnerSession, migrate_data_api
    from setuhaul.aws.provision_logins import provision
    from setuhaul.aws.synthetic_seed import seed
    from setuhaul.db.migrate import discover, migrate
    from test_migrate_data_api import CATALOG

    s = OwnerSession(EmulatedRdsData(f"{fresh} dbname=setuhaul"), "arn:cluster", "arn:owner")
    assert migrate_data_api(s) == [v for v, _, _ in discover()]
    assert migrate_data_api(s) == []
    logins = {r: {"username": f"setuhaul_login_{r}", "password": f"synthetic-{r}-" + "x" * 24}
              for r in ("agent", "console", "ingest", "feasibility", "system", "deploy_evidence")}
    provision(s, logins)
    provision(s, logins)                                                   # a re-run (every deploy) must work too
    assert seed(s)["slots"] > 0

    with psycopg.connect(f"{FRESH} dbname=postgres", autocommit=True) as c:
        c.execute("CREATE DATABASE setuhaul_ref")
    migrate(f"{FRESH} dbname=setuhaul_ref")                                # the same migrations, as a superuser

    def catalog(db, owner):
        with psycopg.connect(f"{FRESH} dbname={db}", autocommit=True) as c:
            rows = c.execute(CATALOG).fetchall()
        return [tuple(re.sub(rf"\b{owner}\b", "OWNER", str(x)) for x in r) for r in rows if r[0] != "mig"]
    su = psycopg.connect(f"{FRESH} dbname=postgres").execute("SELECT current_user").fetchone()[0]
    assert catalog("setuhaul", OWNER) == catalog("setuhaul_ref", su)
    with psycopg.connect(f"{FRESH} dbname=setuhaul", autocommit=True) as c:
        assert c.execute("SELECT has_schema_privilege('setuhaul_rpc','setuhaul','CREATE')").fetchone()[0] is False
        members = dict(c.execute("""SELECT u.rolname, array_agg(r.rolname) FROM pg_roles u
                                      JOIN pg_auth_members m ON m.member = u.oid JOIN pg_roles r ON r.oid = m.roleid
                                     WHERE u.rolname LIKE 'setuhaul!_login!_%' ESCAPE '!' GROUP BY 1""").fetchall())
    assert {u: sorted(v) for u, v in members.items()} == {f"setuhaul_login_{r}": [f"setuhaul_{r}"] for r in logins}
