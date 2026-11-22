"""Wake or put to sleep the synthetic pilot's scheduled workers (low-cost operation; docs/release/09_LOW_COST_PILOT_PLAN.md).

    python -m setuhaul.ops.pilot_mode status         # read-only
    python -m setuhaul.ops.pilot_mode wake           # before a test session
    python -m setuhaul.ops.pilot_mode sleep          # after it (drains due work first; --no-drain to skip)
    python -m setuhaul.ops.pilot_mode pause-check    # read-only: has Aurora reached 0 ACU? (CloudWatch, last 30 min)

The recovery, outbound and approval-relay workers run every minute and each one queries the database, so while they
run Aurora can never reach 0 ACU. The stack therefore creates their rules DISABLED; this command switches them.

Safety (acceptance 1):
* Both commands are idempotent: they set a state, never toggle it, and report what they found and what they left.
* Nothing is stored in the schedules. Pending work lives in PostgreSQL (outbox events, notifications, approval
  callbacks), so sleeping loses nothing: it is picked up on the first tick after `wake`.
* `sleep` first disables the rules, so no new scheduled run starts, then invokes each worker once and waits for it
  (drain), so work that was due goes out before the pilot sleeps. A scheduled run that started just before the rules
  were disabled finishes on its own; running it and the drain together is safe because every worker claims its rows
  under a lock and is fenced by attempt numbers (claim_notifications, consume_outbox_event, claim_task_callbacks).
* What is not affected by sleep: the agent (SQS triggers it), the API, and Step Functions executions, whose tasks
  call their Lambdas directly; `status` lists running executions.

Queued versus delivered (acceptance 4): `status` separates notifications that are QUEUED (not yet sent; while asleep a
driver reply waits here) from those DELIVERED TO THE TEST SINK (status SENT or DELIVERED with a `testsink.` id). A
test-sink delivery is never a WhatsApp delivery: nothing reaches a phone in this pilot.
"""
from __future__ import annotations

import argparse
import json
import sys

STACK, REGION = "SetuHaulPilot", "ap-southeast-2"
WORKERS = ("recovery", "outbound", "approval-relay")
SCOPE_SQL = "SELECT facility_id FROM facilities"
COUNTS = """
SELECT
  (SELECT count(*) FROM notifications WHERE status IN ('PENDING', 'RETRY', 'SENDING', 'UNKNOWN')),
  (SELECT count(*) FROM notifications WHERE status IN ('SENT', 'DELIVERED') AND provider_message_id LIKE 'testsink.%'),
  (SELECT count(*) FROM notifications WHERE status IN ('SENT', 'DELIVERED')
                                        AND coalesce(provider_message_id, '') NOT LIKE 'testsink.%'),
  (SELECT count(*) FROM notifications WHERE status = 'FAILED'),
  (SELECT count(*) FROM pending_outbox_events('notification-enqueue',
                                              ARRAY['AgentReplyStored','ApprovalDecided','BookingConfirmed'], 100000)),
  (SELECT count(*) FROM pending_outbox_events('approval-workflow', ARRAY['BookingRequested','ApprovalDecided'], 100000))
"""


class PilotMode:
    def __init__(self, events, lam, db, rule_names: dict[str, str], function_names: dict[str, str], sfn=None,
                 state_machine_arn: str | None = None, cloudwatch=None, cluster_id: str | None = None, rds=None):
        self.events, self.lam, self.db, self.sfn = events, lam, db, sfn
        self.rules, self.functions, self.machine = rule_names, function_names, state_machine_arn
        self.cw, self.cluster_id, self.rds = cloudwatch, cluster_id, rds

    def pause_events(self, minutes: int = 1440, now=None) -> dict:
        """Acceptance 2, authoritative: RDS records every auto-pause and resume as a db-cluster event ("Successfully
        paused the DB instance ...", "Successfully resumed ..."). The ACU metric stopped publishing for the express
        cluster on 22 Nov 2026, so the events, not the metric, say whether the database is paused."""
        from datetime import datetime, timedelta, timezone
        end = now or datetime.now(timezone.utc)
        evs, marker = [], None
        while True:
            r = self.rds.describe_events(SourceType="db-cluster", SourceIdentifier=self.cluster_id,
                                         StartTime=end - timedelta(minutes=minutes), EndTime=end,
                                         **({"Marker": marker} if marker else {}))
            evs += r.get("Events", [])
            marker = r.get("Marker")
            if not marker:
                break
        done = [(e["Date"], "PAUSED" if "Successfully paused" in e["Message"] else "RESUMED")
                for e in sorted(evs, key=lambda e: e["Date"])
                if "Successfully paused" in e["Message"] or "Successfully resumed" in e["Message"]]
        pending = [e for e in evs if "Initiated pause" in e["Message"] or "Initiated resume" in e["Message"]]
        last = done[-1] if done else None
        return {"cluster": self.cluster_id, "source": "RDS cluster events", "minutes": minutes,
                "pauses": sum(1 for _, k in done if k == "PAUSED"), "resumes": sum(1 for _, k in done if k == "RESUMED"),
                "state": last[1] if last else "UNKNOWN (no pause or resume in the window)",
                "since": last[0] if last else None, "initiated_events": len(pending)}

    def pause_check(self, minutes: int = 30, now=None) -> dict:
        """The cluster's ServerlessDatabaseCapacity (ACU) per minute; paused = a minute at 0. Kept as a secondary
        signal: prefer pause_events() (the metric can stop publishing while the cluster is paused)."""
        from datetime import datetime, timedelta, timezone
        end = now or datetime.now(timezone.utc)
        r = self.cw.get_metric_statistics(Namespace="AWS/RDS", MetricName="ServerlessDatabaseCapacity",
                                          Dimensions=[{"Name": "DBClusterIdentifier", "Value": self.cluster_id}],
                                          StartTime=end - timedelta(minutes=minutes), EndTime=end, Period=60,
                                          Statistics=["Minimum", "Maximum"])
        points = sorted(r.get("Datapoints", []), key=lambda d: d["Timestamp"])
        zero = [d["Timestamp"] for d in points if d["Minimum"] == 0]
        return {"cluster": self.cluster_id, "minutes": minutes, "datapoints": len(points),
                "paused_minutes": len(zero), "first_paused_at": zero[0] if zero else None,
                "latest_acu": points[-1]["Maximum"] if points else None,
                "reached_zero_acu": bool(zero)}

    def rule_states(self) -> dict[str, str]:
        return {w: self.events.describe_rule(Name=n)["State"] for w, n in self.rules.items()}

    def work(self) -> dict:
        with self.db.tx("system", readonly=True) as c:
            scope = [r[0] for r in c.execute(SCOPE_SQL)] + ["CENTRAL"]
        with self.db.tx("system", scope, readonly=True) as c:
            q, sink, other, failed, outbox_n, outbox_a = c.execute(COUNTS).fetchone()
        return {"notifications_queued_not_delivered": q,
                "notifications_delivered_to_test_sink": sink,
                "notifications_sent_by_a_real_provider": other,          # must stay 0 in this pilot
                "notifications_failed": failed,
                "outbox_events_waiting_for_notification": outbox_n,
                "outbox_events_waiting_for_approval_workflow": outbox_a}

    def running_executions(self) -> int | None:
        if not (self.sfn and self.machine):
            return None
        base = self.machine.rsplit(":", 1)[0] if self.machine.count(":") > 6 else self.machine
        return len(self.sfn.list_executions(stateMachineArn=base, statusFilter="RUNNING", maxResults=100)["executions"])

    def status(self) -> dict:
        states = self.rule_states()
        mode = "AWAKE" if all(s == "ENABLED" for s in states.values()) else \
               "ASLEEP" if all(s == "DISABLED" for s in states.values()) else "MIXED"
        return {"mode": mode, "schedules": states, "work": self.work(),
                "running_approval_executions": self.running_executions(),
                "note": "queued = stored, not yet sent; test-sink deliveries are not WhatsApp deliveries"}

    def wake(self) -> dict:
        before = self.rule_states()
        for w, n in self.rules.items():
            if before[w] != "ENABLED":
                self.events.enable_rule(Name=n)
        return {"before": before, "after": self.status()}

    def sleep(self, drain: bool = True) -> dict:
        before = self.rule_states()
        for w, n in self.rules.items():
            if before[w] != "DISABLED":
                self.events.disable_rule(Name=n)                    # first: no new scheduled run can start
        drained = {}
        if drain:
            for w in WORKERS:                                       # then deliver what is due, and wait for it
                r = self.lam.invoke(FunctionName=self.functions[w], InvocationType="RequestResponse", Payload=b"{}")
                body = r["Payload"].read() if hasattr(r.get("Payload"), "read") else r.get("Payload", b"")
                drained[w] = {"status": r.get("StatusCode"), "error": r.get("FunctionError"),
                              "result": json.loads(body or b"null")}
        return {"before": before, "drained": drained, "after": self.status()}


def from_stack(region: str = REGION) -> PilotMode:
    import boto3
    from ..aws.data_api import DataApiDb
    cf = boto3.client("cloudformation", region_name=region)
    out = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    names = out["ScheduleRuleNames"].split(",")
    rules = {w: next(n for n in names if f"-{w}-every-minute" in n) for w in WORKERS}
    env = rules[WORKERS[0]].split("-")[1]
    logins = json.loads(out["LoginSecretArns"])
    db = DataApiDb(out["ClusterArn"], "setuhaul", {"system": logins["system"]},
                   boto3.client("rds-data", region_name=region), resume_wait_s=120)
    return PilotMode(boto3.client("events", region_name=region), boto3.client("lambda", region_name=region), db, rules,
                     {w: f"setuhaul-{env}-{w}" for w in WORKERS}, boto3.client("stepfunctions", region_name=region),
                     out.get("ApprovalAliasArn"), boto3.client("cloudwatch", region_name=region), f"setuhaul-{env}",
                     boto3.client("rds", region_name=region))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="wake / sleep / status of the synthetic pilot's scheduled workers")
    ap.add_argument("command", choices=("status", "wake", "sleep", "pause-check"))
    ap.add_argument("--no-drain", action="store_true", help="sleep without invoking the workers once first")
    a = ap.parse_args(argv)
    pm = from_stack()
    result = (pm.status() if a.command == "status" else pm.wake() if a.command == "wake"
              else {"events": pm.pause_events(), "acu_metric": pm.pause_check()} if a.command == "pause-check"
              else pm.sleep(not a.no_drain))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
