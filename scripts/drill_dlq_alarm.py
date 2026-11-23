"""AWS_QUEUE gate, monitoring: prove the DLQ-depth alarm fires (owner-authorised 23 Nov 2026).

    python scripts/drill_dlq_alarm.py [--out docs/release/evidence/drill_dlq_alarm.json]

The first DLQ drill (drill_dlq_summary.json) proved dead-lettering and redrive, but the message never stayed in the
DLQ long enough for the DlqNotEmpty alarm (maximum of ApproximateNumberOfMessagesVisible over 5 minutes > 0) to see it.
Here one new tagged, non-JSON message is dead-lettered the same way and then HELD in the DLQ until the alarm goes to
ALARM; then only that message is removed (receipt-handle delete after matching its tag; no purge) and the alarm must
return to OK. No business data is touched (the consumer fails it at the JSON parse). Account ids are masked.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from drill_dlq import depth, failures_logged, peek  # noqa: E402

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
MASK = re.compile(r"\b\d{12}\b")
LOG: list[dict] = []


def c(n):
    return boto3.client(n, region_name=REGION)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def note(event, **kw):
    LOG.append({"t": now(), "event": event, **kw})
    print(now(), event, json.dumps(kw, default=str)[:260], flush=True)


def alarm_state(cw):
    a = cw.describe_alarms(AlarmNamePrefix="SetuHaulPilot-DlqNotEmpty")["MetricAlarms"][0]
    return a["AlarmName"], a["StateValue"]


def wait(fn, minutes, every=20):
    end = time.time() + minutes * 60
    while time.time() < end:
        r = fn()
        if r:
            return r
        time.sleep(every)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "drill_dlq_alarm.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    sqs, cw, logs, lam = c("sqs"), c("cloudwatch"), c("logs"), c("lambda")
    q_url = out["QueueUrl"]
    dlq_arn = json.loads(sqs.get_queue_attributes(QueueUrl=q_url, AttributeNames=["RedrivePolicy"])["Attributes"]["RedrivePolicy"])["deadLetterTargetArn"]
    dlq_url = sqs.get_queue_url(QueueName=dlq_arn.rsplit(":", 1)[-1])["QueueUrl"]
    group = lam.get_function_configuration(FunctionName="setuhaul-pilot-agent").get("LoggingConfig", {}).get("LogGroup") \
        or "/aws/lambda/setuhaul-pilot-agent"
    name, st0 = alarm_state(cw)
    ev = {"alarm": name.rsplit("-", 1)[0], "alarm_state_before": st0,
          "before": {"queue": depth(sqs, q_url), "dlq": depth(sqs, dlq_url)}}
    note("before", **ev["before"], alarm=st0)
    if sum(ev["before"]["queue"].values()) or sum(ev["before"]["dlq"].values()) or st0 != "OK":
        note("STOP: not a clean starting point")
        Path(a.out).write_text(json.dumps({"evidence": ev, "stopped": True, "log": LOG}, indent=1) + "\n")
        return 2
    tag = f"STEP6-DLQ-ALARM-{uuid.uuid4().hex[:12]}"
    t0 = int(time.time() * 1000)
    sent = sqs.send_message(QueueUrl=q_url, MessageGroupId=tag, MessageDeduplicationId=tag,
                            MessageBody=f"{tag} synthetic alarm drill: deliberately not a driver event (not JSON)")
    ev["message"] = {"tag": tag, "message_id": sent["MessageId"], "sent_at": now()}
    note("sent", **ev["message"])
    in_dlq = wait(lambda: sum(depth(sqs, dlq_url).values()) >= 1, 40, 30)
    n, types = failures_logged(logs, group, t0)
    mine, others = peek(sqs, dlq_url, tag)
    ev["dead_lettered"] = {"in_dlq": bool(in_dlq), "at": now(), "consumer_failures": n, "error_types": types,
                           "tag_matches": len(mine) == 1, "other_messages": len(others)}
    note("dead-lettered", **ev["dead_lettered"])
    if not in_dlq or len(mine) != 1 or others:
        note("STOP: the DLQ does not hold exactly the tracked message")
        Path(a.out).write_text(json.dumps({"evidence": ev, "stopped": True, "log": LOG}, indent=1) + "\n")
        return 2
    fired = wait(lambda: alarm_state(cw)[1] == "ALARM", 20, 30)
    ev["alarm_fired"] = {"state": alarm_state(cw)[1], "at": now(), "fired": bool(fired)}
    note("alarm", **ev["alarm_fired"])
    removed = False
    r = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, VisibilityTimeout=30, WaitTimeSeconds=5)
    for m in r.get("Messages", []):
        if tag in m["Body"]:
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
            removed = True
        else:
            sqs.change_message_visibility(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
    ev["tracked_message_removed"] = removed
    note("removed only the tracked message" if removed else "NOT removed")
    ok = wait(lambda: alarm_state(cw)[1] == "OK", 20, 30)
    hist = cw.describe_alarm_history(AlarmName=name, HistoryItemType="StateUpdate", StartDate=datetime.fromtimestamp(t0 / 1000, timezone.utc),
                                     EndDate=datetime.now(timezone.utc))["AlarmHistoryItems"]
    ev["alarm_history"] = [(h["Timestamp"].isoformat(), h["HistorySummary"]) for h in hist]
    ev["after"] = {"queue": depth(sqs, q_url), "dlq": depth(sqs, dlq_url), "alarm": alarm_state(cw)[1]}
    checks = {
        "clean start (both queues empty, alarm OK)": True,
        "the tracked message was dead-lettered after consumer failures at the JSON parse":
            ev["dead_lettered"]["in_dlq"] and n >= 5 and types == ["JSONDecodeError"],
        "the DLQ held exactly the tracked message": ev["dead_lettered"]["tag_matches"] and not ev["dead_lettered"]["other_messages"],
        "the DlqNotEmpty alarm went to ALARM while the message was in the DLQ": bool(fired),
        "only the tracked message was removed": removed,
        "the alarm returned to OK and both queues are empty": bool(ok) and not sum(ev["after"]["queue"].values())
            and not sum(ev["after"]["dlq"].values()),
    }
    report = {"collected_utc": now(), "evidence": ev, "checks": checks, "all_pass": all(checks.values()), "log": LOG}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k, flush=True)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
