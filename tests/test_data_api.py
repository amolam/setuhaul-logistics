"""E3: the RDS Data API database backend (setuhaul/aws/data_api.py) against the local emulator of the rds-data client.

The whole suite can also run through this backend: SETUHAUL_TEST_DATA_API=1 (see conftest.py). These tests cover what
that cannot show: hostile strings, every result type, error translation, rollback, and the per-role login refusal.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from data_api_emulator import ALL_ROLES, EmulatedRdsData
from setuhaul.api.db import Db
from setuhaul.aws.data_api import DataApiDatabaseError, DataApiDb, bind, literal, make_db

ARN = "arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul"


@pytest.fixture
def em(dsn):
    return EmulatedRdsData(dsn)


@pytest.fixture
def ddb(em):
    return DataApiDb(ARN, "setuhaul", ALL_ROLES, em)


HOSTILE = ["it's", "''; DROP TABLE drivers; --", "back\\slash", "\\'", "E'\\x41'", "$$dollar$$", "%s %% %(x)s",
           "naïve हिंदी 🚚", "line\nbreak\ttab", "", "  ", "{a,b}", '"quoted"', "\\\\'\\\\"]


@pytest.mark.parametrize("s", HOSTILE)
def test_hostile_strings_round_trip_unchanged(ddb, s):
    with ddb.tx("system") as c:
        assert c.execute("SELECT %s::text, length(%s)", (s, s)).fetchone() == (s, len(s))


def test_a_nul_character_is_refused_before_anything_is_sent():
    with pytest.raises(ValueError):
        literal("a\x00b")


def test_placeholders_and_parameters_must_match():
    with pytest.raises(ValueError):
        bind("SELECT %s, %s", (1,))
    with pytest.raises(ValueError):
        bind("SELECT %s", (1, 2))
    assert bind("SELECT 100%% WHERE x LIKE %s", ("a%",)) == "SELECT 100% WHERE x LIKE 'a%'"
    assert bind("SELECT %s::uuid[] AND %s::text AND y", (["a"], 1)) == "SELECT CAST('{\"a\"}' AS uuid[]) AND CAST(1 AS text) AND y"


def test_every_result_type_comes_back_as_psycopg_would_return_it(ddb, dsn):
    now = datetime(2026, 11, 22, 5, 30, 15, 123456, tzinfo=timezone.utc)
    u = uuid.uuid4()
    # range and interval values can be SENT (literals), but the real Data API cannot RETURN them (test below)
    sql = """SELECT %s::int, %s::bigint, %s::numeric, %s::float8, %s::bool, %s::text, %s::timestamptz, %s::date,
                    %s::uuid, %s::jsonb, lower(%s::tstzrange), upper(%s::tstzrange), %s::text[], %s::int[],
                    extract(epoch FROM %s::interval)::int, NULL::text, %s::bytea"""
    rng = Range(now, now + timedelta(hours=1), "[)")
    params = (7, 2**40, Decimal("12.3400"), 1.5, True, "x", now, date(2026, 11, 22), u, Jsonb({"a": [1, "b"]}),
              rng, rng, ["a", "b,c", None], [1, 2], timedelta(minutes=90), b"\x00\xff")
    with ddb.tx("system") as c:
        got = c.execute(sql, params).fetchone()
    with psycopg.connect(dsn, autocommit=True) as p:
        p.execute("SET TimeZone = 'Asia/Kolkata'")
        want = p.execute(sql, params).fetchone()
    assert got == want


def test_range_and_interval_results_are_refused_as_aurora_refuses_them(ddb):
    """Regression (pilot, 22 Nov 2026): the Data API refused SELECT o.during (tstzrange) with
    UnsupportedResultException, so every option list failed on AWS while the emulator returned it. The emulator now
    refuses the same types, so the whole Data API-mode suite guards against them."""
    now = datetime(2026, 11, 22, 5, 30, tzinfo=timezone.utc)
    for sql, arg in (("SELECT %s::tstzrange", Range(now, now + timedelta(hours=1), "[)")),
                     ("SELECT %s::interval", timedelta(minutes=5)), ("SELECT now() - %s::timestamptz", now)):
        with pytest.raises(psycopg.Error, match="UnsupportedResultException"):
            with ddb.tx("system") as c:
                c.execute(sql, (arg,)).fetchone()


def test_rows_iterate_and_rowcount_reports_updates(ddb):
    with ddb.tx("system") as c:
        assert [r[0] for r in c.execute("SELECT generate_series(1, 3)")] == [1, 2, 3]
        cur = c.execute("SELECT 1 AS one, 'x' AS two")
        assert [d.name for d in cur.description] == ["one", "two"]
        assert c.execute("SELECT 1 WHERE false").fetchone() is None


def test_a_database_refusal_keeps_its_sqlstate_and_message(ddb):
    with pytest.raises(psycopg.Error) as e:
        with ddb.tx("system") as c:
            c.execute("DO $$ BEGIN RAISE EXCEPTION 'HOLD_CONFLICT: slot taken' USING ERRCODE = 'P0001'; END $$")
    assert isinstance(e.value, DataApiDatabaseError)
    assert (e.value.sqlstate, e.value.diag.message_primary) == ("P0001", "HOLD_CONFLICT: slot taken")


def test_permission_denied_is_42501_as_with_psycopg(ddb):
    with pytest.raises(psycopg.Error) as e:
        with ddb.tx("agent") as c:
            c.execute("UPDATE deployment_evidence SET gate = gate")
    assert e.value.sqlstate == "42501"


def test_an_error_rolls_the_whole_transaction_back(ddb, dsn):
    tag = f"rollback-{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError):
        with ddb.tx("system") as c:
            c.execute("SELECT set_config('app.x', %s, false)", (tag,))
            c.execute("CREATE TEMP TABLE t_rb(x text)")
            raise RuntimeError("boom")
    assert ddb.client._tx == {}                                  # nothing left open


def test_a_workload_without_a_login_for_a_role_cannot_use_it(em):
    agent_only = DataApiDb(ARN, "setuhaul", {"agent": ALL_ROLES["agent"]}, em)
    with pytest.raises(PermissionError):
        with agent_only.tx("console"):
            pass
    with pytest.raises(ValueError):
        DataApiDb(ARN, "setuhaul", {"owner": "arn:x"}, em)


def test_role_scope_and_staff_context_are_set_inside_the_transaction(ddb, dsn):
    q = "SELECT current_user, current_setting('app.facility_scope'), current_setting('TimeZone')"
    staff = ("staff:ops@example.test", {"operations"}, ("FAC-JAI-01",))
    with ddb.tx("console", ("FAC-JAI-01", "FAC-GGN-01"), staff=staff) as c:
        got = c.execute(q).fetchone()
    with Db(dsn).tx("console", ("FAC-JAI-01", "FAC-GGN-01"), staff=staff) as c:
        want = c.execute(q).fetchone()
    assert got == want and got[0] == "setuhaul_console" and got[2] == "Asia/Kolkata"
    with ddb.tx("system", readonly=True) as c:
        with pytest.raises(psycopg.Error) as e:
            c.execute("CREATE TEMP TABLE t_ro(x int)")
    assert e.value.sqlstate == "25006"                            # read-only transaction


def test_make_db_selects_the_backend(monkeypatch, em):
    assert isinstance(make_db("host=localhost"), Db)
    monkeypatch.setenv("SETUHAUL_DB_CLUSTER_ARN", ARN)
    monkeypatch.setenv("SETUHAUL_DB_SECRET_ARNS", '{"system": "arn:aws:secretsmanager:ap-southeast-2:0:secret:s"}')
    import setuhaul.aws.data_api as mod
    monkeypatch.setattr(mod.DataApiDb, "from_env", classmethod(lambda cls, env=None, client=None: cls(
        ARN, "setuhaul", {"system": "s"}, em)))
    assert isinstance(make_db("rds-data"), DataApiDb)


@pytest.mark.skipif(os.environ.get("SETUHAUL_TEST_DATA_API") != "1", reason="only when the suite runs through the Data API")
def test_the_suite_really_goes_through_the_data_api_backend(dsn):
    with Db(dsn).tx("system") as c:
        assert type(c).__module__ == "setuhaul.aws.data_api"


def test_transaction_setup_costs_two_round_trips_and_matches_psycopg(ddb, em, dsn):
    before = em.statements
    with ddb.tx("agent", ("FAC-JAI-01",), driver="DRV-X") as c:
        setup = em.statements - before
        got = c.execute("SELECT current_user, current_setting('search_path'), current_setting('TimeZone'), "
                        "current_setting('app.facility_scope'), current_setting('app.driver_id'), "
                        "current_setting('standard_conforming_strings')").fetchone()
    with Db(dsn).tx("agent", ("FAC-JAI-01",), driver="DRV-X") as c:
        want = c.execute("SELECT current_user, current_setting('search_path'), current_setting('TimeZone'), "
                         "current_setting('app.facility_scope'), current_setting('app.driver_id'), "
                         "current_setting('standard_conforming_strings')").fetchone()
    assert setup == 2 and got == want


@pytest.mark.parametrize("sql,cls", [
    ("UPDATE deployment_evidence SET gate = gate", psycopg.errors.InsufficientPrivilege),
    ("SELECT 1/0", psycopg.errors.DivisionByZero),
    ("SELECT * FROM no_such_table", psycopg.errors.UndefinedTable)])
def test_errors_arrive_as_the_same_psycopg_class(ddb, sql, cls):
    with pytest.raises(cls) as e:
        with ddb.tx("agent") as c:
            c.execute(sql)
    assert isinstance(e.value, DataApiDatabaseError) and e.value.sqlstate == cls.sqlstate


def test_aurora_database_errors_keep_their_message_and_sqlstate():
    """Regression (pilot, 22 Nov 2026): Aurora answers SQL errors with DatabaseErrorException, not
    BadRequestException; the server's message (HOLD_CONFLICT, FORBIDDEN, BAD_RESERVATION ...) must survive so the
    service maps it exactly as with a direct connection."""
    import psycopg
    from setuhaul.aws.data_api import translate

    class E(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.response = {"Error": {"Code": code, "Message": msg}}
    for code in ("DatabaseErrorException", "BadRequestException"):
        t = translate(E(code, "ERROR: HOLD_CONFLICT: slot taken\n  Where: PL/pgSQL function place_hold; SQLState: P0001"))
        assert t.sqlstate == "P0001" and t.diag.message_primary == "HOLD_CONFLICT: slot taken"
        assert isinstance(t, psycopg.errors.RaiseException)
    t = translate(E("DatabaseErrorException", "ERROR: permission denied for table x; SQLState: 42501"))
    assert isinstance(t, psycopg.errors.InsufficientPrivilege) and t.diag.message_primary == "permission denied for table x"
    assert translate(E("ForbiddenException", "not authorized")).diag.message_primary is None

