"""Low-cost pilot operation (docs/release/09_LOW_COST_PILOT_PLAN.md): wake/sleep of the scheduled workers and the
Data API retry while a paused Aurora resumes. AWS is never called: EventBridge and Lambda are stubs, the database is
the local PostgreSQL.

Acceptance 1: wake and sleep are idempotent, sleep disables before it drains, and pending work survives a sleep.
Acceptance 4: queued driver replies are counted apart from replies delivered to the test sink.
"""
from __future__ import annotations

import io
import json

import pytest

from setuhaul.api.db import Db
from setuhaul.aws.data_api import RESUMING, begin_with_resume
from setuhaul.notify.outbound import OutboundRelay, TestSinkProvider
from setuhaul.ops.pilot_mode import WORKERS, PilotMode
from test_approvals import pending
from test_db_requests import decide

RULES = {w: f"setuhaul-pilot-{w}-every-minute" for w in WORKERS}
FUNCTIONS = {w: f"setuhaul-pilot-{w}" for w in WORKERS}


class StubEvents:
    def __init__(self, calls, state="DISABLED"):
        self.calls, self.state = calls, {n: state for n in RULES.values()}

    def describe_rule(self, Name):
        return {"Name": Name, "State": self.state[Name]}

    def enable_rule(self, Name):
        self.calls.append(("enable", Name))
        self.state[Name] = "ENABLED"

    def disable_rule(self, Name):
        self.calls.append(("disable", Name))
        self.state[Name] = "DISABLED"


class StubLambda:
    def __init__(self, calls, run=None):
        self.calls, self.run = calls, run

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append(("invoke", FunctionName))
        result = self.run(FunctionName) if self.run else {}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result).encode())}


@pytest.fixture
def pm_factory(dsn):
    def make(state="DISABLED", run=None):
        calls = []
        return PilotMode(StubEvents(calls, state), StubLambda(calls, run), Db(dsn), RULES, FUNCTIONS), calls
    return make


def test_wake_and_sleep_are_idempotent(pm_factory):
    pm, calls = pm_factory("DISABLED")
    assert pm.status()["mode"] == "ASLEEP"
    pm.wake()
    pm.wake()                                                       # already awake: nothing changes
    assert pm.status()["mode"] == "AWAKE" and [c for c in calls if c[0] == "enable"] == [("enable", n) for n in RULES.values()]
    pm.sleep(drain=False)
    pm.sleep(drain=False)
    assert pm.status()["mode"] == "ASLEEP" and len([c for c in calls if c[0] == "disable"]) == len(RULES)


def test_sleep_stops_new_runs_before_it_drains_due_work(pm_factory):
    pm, calls = pm_factory("ENABLED")
    out = pm.sleep()
    disables = [i for i, c in enumerate(calls) if c[0] == "disable"]
    invokes = [i for i, c in enumerate(calls) if c[0] == "invoke"]
    assert disables and invokes and max(disables) < min(invokes)        # no scheduled run can start after the drain
    assert [c[1] for c in calls if c[0] == "invoke"] == [FUNCTIONS[w] for w in WORKERS]
    assert out["after"]["mode"] == "ASLEEP" and all(d["status"] == 200 for d in out["drained"].values())


def test_pending_work_survives_sleep_and_queued_is_counted_apart_from_test_sink_delivery(pm_factory, conn, dsn):
    """A driver notice created while asleep waits in the database (QUEUED); after wake it is delivered to the test
    sink (counted separately, and never as a real-provider send)."""
    pm, _ = pm_factory("ENABLED")
    pm.sleep(drain=False)
    before = pm.work()
    t, r = pending(conn)
    decide(conn, r["appointment_id"], "REJECT")                     # an outcome notice is now owed to the driver
    asleep = pm.work()
    assert asleep["outbox_events_waiting_for_notification"] >= before["outbox_events_waiting_for_notification"] + 1
    assert asleep["notifications_delivered_to_test_sink"] == before["notifications_delivered_to_test_sink"]
    relay = OutboundRelay(Db(dsn), TestSinkProvider())
    relay.run_once(limit=1000)                                      # enqueue only would leave it QUEUED; run once
    pm.wake()
    for _ in range(3):                                              # the first ticks after wake
        relay.run_once(limit=1000)
    awake = pm.work()
    row = conn.execute("""SELECT status, provider_message_id FROM setuhaul.notifications
                           WHERE shipment_id = %s AND template_id = 'booking_rejected'""", (t["sid"],)).fetchone()
    assert row[0] == "SENT" and row[1].startswith("testsink.")
    assert awake["notifications_delivered_to_test_sink"] >= before["notifications_delivered_to_test_sink"] + 1
    assert awake["notifications_sent_by_a_real_provider"] == before["notifications_sent_by_a_real_provider"]


def test_a_drain_run_beside_a_scheduled_run_sends_each_notice_once(pm_factory, conn, dsn):
    """In-flight safety: the drain may overlap a scheduled run that started just before sleep. Two relays running at
    once still send one notice once (claims are locked and fenced)."""
    import threading
    t, r = pending(conn)
    decide(conn, r["appointment_id"], "REJECT")
    sink_calls = []

    class Counting(TestSinkProvider):
        def send(self, to, text, key):
            sink_calls.append(key)
            return super().send(to, text, key)
    relays = [OutboundRelay(Db(dsn), Counting()) for _ in range(2)]
    for _ in range(3):
        th = [threading.Thread(target=x.run_once, kwargs={"limit": 1000}) for x in relays]
        [x.start() for x in th]
        [x.join() for x in th]
    nid = conn.execute("""SELECT notification_id FROM setuhaul.notifications
                           WHERE shipment_id = %s AND template_id = 'booking_rejected'""", (t["sid"],)).fetchone()[0]
    assert sink_calls.count(str(nid)) == 1


def test_pause_check_reports_whether_the_cluster_reached_zero_acu(pm_factory):
    from datetime import datetime, timedelta, timezone
    pm, _ = pm_factory()
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)

    class CW:
        def __init__(self, mins):
            self.mins = mins

        def get_metric_statistics(self, **kw):
            assert kw["MetricName"] == "ServerlessDatabaseCapacity" and kw["Dimensions"][0]["Value"] == "setuhaul-pilot"
            return {"Datapoints": [{"Timestamp": now - timedelta(minutes=i), "Minimum": m, "Maximum": m}
                                   for i, m in enumerate(self.mins)]}
    pm.cluster_id = "setuhaul-pilot"
    pm.cw = CW([0, 0, 0.5, 1.0])
    paused = pm.pause_check(now=now)
    assert paused["reached_zero_acu"] and paused["paused_minutes"] == 2 and paused["latest_acu"] == 0
    pm.cw = CW([0.5, 0.5, 0.5])
    assert pm.pause_check(now=now)["reached_zero_acu"] is False


# ---------------------------------------------------------------- Data API resume retry
class Resuming(Exception):
    def __init__(self, code=RESUMING):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class Client:
    def __init__(self, fail_times, code=RESUMING):
        self.left, self.code, self.calls = fail_times, code, 0

    def begin_transaction(self, **kw):
        self.calls += 1
        if self.left:
            self.left -= 1
            raise Resuming(self.code)
        return {"transactionId": "tx-1"}


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_begin_waits_while_the_database_resumes():
    c, clock = Client(fail_times=4), Clock()
    assert begin_with_resume(c, {}, max_wait_s=60, sleep=clock.sleep, clock=clock) == "tx-1"
    assert c.calls == 5 and clock.slept == [1.0, 2.0, 4.0, 5.0]


def test_begin_gives_up_after_the_wait_and_never_retries_other_errors():
    c, clock = Client(fail_times=100), Clock()
    with pytest.raises(Resuming):
        begin_with_resume(c, {}, max_wait_s=20, sleep=clock.sleep, clock=clock)
    assert clock.t <= 20
    other = Client(fail_times=1, code="BadRequestException")
    with pytest.raises(Resuming):
        begin_with_resume(other, {}, max_wait_s=60, sleep=Clock().sleep, clock=Clock())
    assert other.calls == 1


def test_application_transactions_ride_out_a_resume(dsn, monkeypatch):
    from data_api_emulator import ALL_ROLES, EmulatedRdsData
    from setuhaul.aws.data_api import DataApiDb
    em = EmulatedRdsData(dsn)
    real, left = em.begin_transaction, {"n": 2}

    def flaky(**kw):
        if left["n"]:
            left["n"] -= 1
            raise Resuming()
        return real(**kw)
    em.begin_transaction = flaky
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = DataApiDb("arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul", "setuhaul", ALL_ROLES, em, resume_wait_s=30)
    with db.tx("system", readonly=True) as c:
        assert c.execute("SELECT 1").fetchone() == (1,)
    assert left["n"] == 0
