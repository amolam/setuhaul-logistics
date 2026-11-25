"""Demo fast track: the read-only pre-deployment readiness check (setuhaul/ops/readiness.py).

The database part runs on the provisional-authorization test database (migrations 0032/0033 applied, guard and
identity enforced): it issues real authorizations through the real function and checks that the readiness check reads
the SAME limit, rows and approvals the function enforces, inside a READ ONLY transaction. The assessment is tested
as a pure function.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from setuhaul.ops import readiness as rd
from test_provisional_authorization import c, complete, dsn, env_name, issue, new_release  # noqa: F401

NOW = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)
REF = "OWNER-APPROVAL-20261125-G2LS-TEST"
SHA = "4d793eb20dfeffbb4a21eb143c3b8601713c2c55"
ART = "261da9ce3860" + "0" * 52


def good_report(**kw):
    r = {"mode": "renewal", "environment": "pilot-ap-southeast-2", "collected_utc": (NOW - timedelta(hours=1)).isoformat(),
         "approval_ref": REF, "artifact_sha256": ART, "booking_enabled": True,
         "booking_observed": {"booking_state": "authorized", "booking_enabled": True, "source": "/health",
                              "observed_utc": (NOW - timedelta(hours=1)).isoformat()},
         "gates": {"AWS_QUEUE": {"status": "PASS"}, "RECOVERY_WORKER": {"status": "PASS"}}}
    r.update(kw)
    return r


def state(**kw):
    s = dict(environment="pilot-ap-southeast-2", now=NOW, limit=2, limit_source="test",
             issued_24h=[NOW - timedelta(hours=12)], open_window=None, approval_used=False,
             running={"deployment_id": "m-" + "b" * 40, "artifact_sha256": ART, "release_sha": "b" * 40},
             booking={"booking_state": "authorized", "kill_switch_engaged": False}, stack_status="UPDATE_COMPLETE",
             health={"booking_kill_switch": "disengaged"}, report=good_report())
    s.update(kw)
    return rd.State(**s)


def by(checks, name):
    return next(c for c in checks if c.name.startswith(name))


# ------------------------------------------------------------------------------------------------ assessment
def test_ready_when_every_condition_holds_and_the_valid_report_is_reused():
    checks = rd.assess(state(), REF, SHA)
    assert not rd.blockers(checks)
    assert "reused, not regenerated" in by(checks, "five-gate report").detail
    assert by(checks, "capacity for a retry").ok is False and by(checks, "capacity for a retry").blocking is False


def test_full_capacity_blocks_and_says_exactly_when_it_clears():
    t1, t2 = NOW - timedelta(hours=11, minutes=1, seconds=30), NOW - timedelta(hours=3, minutes=23)
    s = state(issued_24h=[t1, t2])
    c = by(rd.assess(s, REF, SHA), "provisional capacity")
    assert not c.ok and c.blocking and f"{t1 + timedelta(hours=24, seconds=1):%Y-%m-%dT%H:%M:%S}Z" in c.action
    later = t1 + timedelta(hours=24, seconds=2)
    assert by(rd.assess(s, REF, SHA, deploy_at=later), "provisional capacity").ok        # judged at the deploy time
    assert by(rd.assess(state(issued_24h=[t1, t2], limit=4), REF, SHA), "provisional capacity").ok   # a raised limit


def test_every_other_blocker_is_reported_together_with_an_action():
    s = state(approval_used=True, open_window={"authorization_id": 9, "expires_at": "x"}, stack_status="UPDATE_IN_PROGRESS",
              booking={"booking_state": "off", "kill_switch_engaged": True}, health={"booking_kill_switch": "engaged"},
              report=good_report(approval_ref="OWNER-APPROVAL-20261124-RECOVERY-A",
                                 collected_utc=(NOW - timedelta(hours=30)).isoformat()), limit=None,
              limit_source="found []")
    b = rd.blockers(rd.assess(s, "not-a-ref", "short"))
    names = {c.name for c in b}
    assert names == {"approval reference format", "approval reference unused", "release commit",
                     "provisional capacity (rolling 24 h)", "no open provisional window",
                     "running release authorized (no chaining)", "kill switch disengaged", "stack ready",
                     "five-gate report reusable"}
    assert all(c.action for c in b)
    rep = by(b, "five-gate report")
    assert "not fresh" in rep.detail and "names approval" in rep.detail and "step6_gate_evidence.py" in rep.action


def test_a_report_is_reused_only_for_the_running_release_and_the_deploy_time():
    assert not by(rd.assess(state(report=good_report(artifact_sha256="a" * 64)), REF, SHA), "five-gate").ok
    assert not by(rd.assess(state(), REF, SHA, deploy_at=NOW + timedelta(hours=23, minutes=30)), "five-gate").ok
    s = state(report=good_report(gates={"AWS_QUEUE": {"status": "PASS"}, "RECOVERY_WORKER": {"status": "FAIL"}}))
    assert "RECOVERY_WORKER" in by(rd.assess(s, REF, SHA), "five-gate").detail
    assert not by(rd.assess(state(report=None, report_error="missing.json: FileNotFoundError"), REF, SHA), "five-gate").ok


# ------------------------------------------------------------------------------------------------ the database's answers
def test_the_check_reads_the_limit_and_rows_the_issuing_function_enforces_read_only(c):          # noqa: F811
    env = env_name()
    complete(c, env)
    d1 = new_release(c, env)
    ref1 = f"OWNER-APPROVAL-20261125-R{uuid.uuid4().hex[:6]}"
    issue(c, d1, ref=ref1)
    with c.transaction():
        c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
        c.execute("SELECT setuhaul.revoke_provisional_authorization(%s,'test: close d1')", (env,))
    with c.transaction():
        c.execute("SET TRANSACTION READ ONLY")
        db = rd.read_database(c, env, ref1)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):     # the check cannot write, even by mistake
            c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = engaged")
    assert db["limit"] == 2 and db["limit_source"] == "issue_provisional_authorization source"
    assert len(db["issued"]) == 1 and db["approval_used"] and db["open"] is None
    newest = c.execute("SELECT deployment_id FROM setuhaul.deployment_manifests WHERE environment=%s "
                       "ORDER BY manifest_id DESC LIMIT 1", (env,)).fetchone()[0]
    assert db["running"]["deployment_id"] == newest
    with c.transaction():
        c.execute("SET TRANSACTION READ ONLY")
        assert rd.read_database(c, env, "OWNER-APPROVAL-20261125-UNUSED-X")["approval_used"] is False
        assert rd.read_database(c, env_name(), ref1)["issued"] == []       # per environment


def test_a_recovery_run_is_judged_by_the_recovery_rules():
    s = state(booking={"booking_state": "off", "kill_switch_engaged": False},
              report=good_report(booking_enabled=False, booking_observed={
                  "booking_state": "off", "booking_enabled": False, "source": "/health",
                  "observed_utc": (NOW - timedelta(hours=1)).isoformat()}))
    assert not rd.blockers(rd.assess(s, REF, SHA, recovery=True))
    assert "running release authorized (no chaining)" in {c.name for c in rd.blockers(rd.assess(s, REF, SHA))}
