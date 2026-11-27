"""Migration 0045 (owner decision, 26 Nov 2026): bounded recovery of an EXPIRED provisional authorization.

An expired window is recoverable only when the owner's approval for exactly that authorization is recorded
(provisional_expired_recovery_approvals), once, within seven days of its expiry, never from a recovery, with an
ISSUED, EXPIRED audit trail; every other issuer rule still applies and no existing row changes."""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from test_booking_evidence_guard import record
from test_move_appointment import SCH, sch  # noqa: F401
from test_provisional_authorization import (auth_row, book, c, complete, dsn, env_name, events, fresh_ref,  # noqa: F401
                                            issue, new_release, refused)
from test_recovery_release_a import insert_auth, other_dep, recover, revoke, revoked_release

MIG = Path(__file__).resolve().parents[1] / "migrations" / "0045_recover_expired_provisional.sql"


def approve(conn, a, d):
    """The owner's recorded approval to recover authorization a (written as the test superuser, as 0045 does for 68)."""
    conn.execute("INSERT INTO setuhaul.provisional_expired_recovery_approvals (authorization_id, environment, "
                 "deployment_id, basis, evidence_ref) VALUES (%s,%s,%s,'test: owner approved recovery','test-ref')",
                 (a, d["env"], d["dep"]))


def lapsed_release(conn, env, expired="-2 hours"):
    """The 26 Nov situation: a complete predecessor, then a release whose window lapsed without renewal (still ISSUED
    in the table until the next issuance sweeps it to EXPIRED, exactly like authorization 68)."""
    p = complete(conn, env)
    d1 = new_release(conn, env)
    a1 = insert_auth(conn, d1, p["dep"], "-3 hours", expired)
    return d1, a1


def test_an_owner_approved_expired_window_is_recovered_once_and_its_history_is_kept(c, sch):  # noqa: F811
    env = env_name()
    d1, a1 = lapsed_release(c, env)
    approve(c, a1, d1)
    d2 = new_release(c, env)
    refused(lambda: issue(c, d2), "has no complete evidence")                       # the default rule stands
    out = recover(c, d2, a1)
    assert out["recovery_of"] == a1 and out["predecessor_deployment"] == d1["dep"]
    assert auth_row(c, d1)[0] == "EXPIRED" and events(c, d1) == ["ISSUED", "EXPIRED"]   # swept, never rewritten
    assert book(c, d2)                                                               # the new release's own window
    refused(lambda: book(c, d1), "DEPLOYMENT_SUPERSEDED")


def test_an_expired_window_without_the_owner_approval_is_refused(c):
    env = env_name()
    d1, a1 = lapsed_release(c, env)
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, a1), "recoverable only with the owner's recorded approval")
    # an approval recorded for another deployment does not count
    env2 = env_name()
    e1, b1 = lapsed_release(c, env2)
    approve(c, b1, other_dep(env2))
    e2 = new_release(c, env2)
    refused(lambda: recover(c, e2, b1), "recoverable only with the owner's recorded approval")


def test_an_approved_expired_window_is_recovered_at_most_once(c):
    env = env_name()
    d1, a1 = lapsed_release(c, env)
    approve(c, a1, d1)
    d2 = new_release(c, env)
    recover(c, d2, a1)
    revoke(c, env)
    record(c, d1)                                     # d1 recorded again: it is the previous release once more
    d3 = new_release(c, env)
    refused(lambda: recover(c, d3, a1), "RECOVERY_USED")


def test_a_recovery_from_an_expired_window_cannot_be_chained(c):
    env = env_name()
    d1, a1 = lapsed_release(c, env)
    approve(c, a1, d1)
    d2 = new_release(c, env)
    a2 = recover(c, d2, a1)["authorization_id"]
    revoke(c, env)                                    # the recovery's own window ends without renewal
    approve(c, a2, d2)                                # even with an approval recorded for it
    d3 = new_release(c, env)
    a3 = recover(c, d3, a2)["authorization_id"]       # 0049: one additional hop is allowed ...
    revoke(c, env)
    approve(c, a3, d3)
    d4 = new_release(c, env)
    refused(lambda: recover(c, d4, a3), "no third hop")   # ... never a third


def test_an_expiry_more_than_seven_days_ago_does_not_qualify(c):
    env = env_name()
    p = complete(c, env)
    d1 = new_release(c, env)
    old = insert_auth(c, d1, p["dep"], "-8 days -1 hour", "-8 days")
    approve(c, old, d1)
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, old), "more than 7 days ago")


def test_a_still_open_or_consumed_window_is_not_an_expired_one(c):
    env = env_name()
    p = complete(c, env)
    d1 = new_release(c, env)
    a1 = insert_auth(c, d1, p["dep"], "-10 minutes", "+50 minutes")                 # open: never swept
    approve(c, a1, d1)
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, a1), "is ISSUED, only a REVOKED or an owner-approved EXPIRED")   # nothing issued
    assert auth_row(c, d1)[0] == "ISSUED"


def test_a_broken_audit_trail_does_not_qualify(c):
    env = env_name()
    p = complete(c, env)
    d1 = new_release(c, env)
    direct = insert_auth(c, d1, p["dep"], "-3 hours", "-2 hours", "EXPIRED", "-2 hours")   # one event only
    approve(c, direct, d1)
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, direct), "audit trail of authorization")


def test_every_other_issuer_rule_still_applies(c):
    env = env_name()
    used = fresh_ref()
    p = complete(c, env)
    d1 = new_release(c, env)
    a1 = insert_auth(c, d1, p["dep"], "-3 hours", "-2 hours")
    c.execute("UPDATE setuhaul.provisional_authorizations SET status = status WHERE false")      # no-op
    approve(c, a1, d1)
    d2 = new_release(c, env)
    refused(lambda: recover(c, d2, a1, minutes=121), "1 to 120 minutes")
    for name in ("production-ap-southeast-2", "PROD"):
        refused(lambda: recover(c, {"env": name, "dep": "m-" + "0" * 40}, a1), "PROVISIONAL_REFUSED")
    out = recover(c, d2, a1, ref=used)
    refused(lambda: recover(c, other_dep(env), a1, ref=used), "PROVISIONAL_REFUSED")   # not the newest release
    assert out["approval_ref"] == used


def test_the_approval_record_is_append_only_and_written_by_0045_only_for_authorization_68(c):
    env = env_name()
    d1, a1 = lapsed_release(c, env)
    approve(c, a1, d1)
    for sql in ("UPDATE setuhaul.provisional_expired_recovery_approvals SET basis = 'changed basis here'",
                "DELETE FROM setuhaul.provisional_expired_recovery_approvals"):
        with pytest.raises(psycopg.Error):
            with c.transaction():
                c.execute(sql)
    src = MIG.read_text(encoding="utf-8")
    block = src[src.index("-- BEGIN owner-approved-expired-recovery"):src.index("-- END owner-approved-expired-recovery")]
    assert "a.authorization_id = 68" in block and "24c388c1d73a2f07654f2f0a31307e99aef01f80" in block
    assert "OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-PATCH-01" in block
    n = c.execute("SELECT count(*) FROM setuhaul.provisional_expired_recovery_approvals").fetchone()[0]
    with c.transaction():
        c.execute("SET LOCAL search_path = setuhaul, public")
        c.execute(block)                              # on a database without that authorization 68: nothing written
    assert c.execute("SELECT count(*) FROM setuhaul.provisional_expired_recovery_approvals").fetchone()[0] == n


def test_the_issuer_keeps_its_owner_and_single_caller(c):
    owner = c.execute("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = "
                      "'setuhaul.issue_provisional_authorization(text,text,text,text,text,integer,bigint)'::regprocedure"
                      ).fetchone()[0]
    assert owner == "setuhaul_rpc"
    callers = {r[0] for r in c.execute(
        "SELECT grantee FROM information_schema.routine_privileges WHERE routine_schema = 'setuhaul' "
        "AND routine_name = 'issue_provisional_authorization' AND privilege_type = 'EXECUTE'").fetchall()}
    assert callers - {"setuhaul_rpc", "postgres"} == {"setuhaul_deploy_evidence"}
