"""AWS_QUEUE / RECOVERY_WORKER gate: a dead-letter drill on the deployed synthetic pilot (owner-approved 23 Nov 2026).

    python scripts/drill_dlq.py [--out docs/release/evidence/drill_dlq.json]

One synthetic, tagged message is tracked through dead-lettering and redrive:
  0. preconditions: the inbound queue and its dead-letter queue are both EMPTY (a redrive moves every message in the
     DLQ, so the drill refuses to run if anything else is there), and the agent's queue consumer is enabled;
  1. send one message to the inbound FIFO queue, in its own message group, whose body is not a driver event (it is not
     JSON). The agent function fails it at json.loads, before any database access, and reports it as a batch-item
     failure; SQS redelivers it after the visibility timeout and, after maxReceiveCount (5) receives, moves it to the DLQ;
  2. confirm the DLQ holds exactly this message (body tag), then redrive it to the source queue with SQS
     StartMessageMoveTask and wait for the task to complete (1 message moved);
  3. watch the consumer receive it again and dead-letter it again (the poison message is still poison: nothing is lost
     and nothing is processed twice);
  4. remove only this tracked message from the DLQ (receipt-handle delete after matching the tag; no purge);
  5. after: both queues empty, inbound_receipts unchanged (no turn ran), every agent failure logged at the JSON parse.
No business record is read or written by the message. Account ids are masked in the output.
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
sys.path.insert(0, str(ROOT))
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
MASK = re.compile(r"\b\d{12}\b")
LOG: list[dict] = []


def c(name):
    return boto3.client(name, region_name=REGION)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def note(event, **kw):
    LOG.append({"t": now(), "event": event, **kw})
    print(now(), event, json.dumps(kw, default=str)[:300], flush=True)


def depth(sqs, url):
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                 "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"])["Attributes"]
    return {k.replace("ApproximateNumberOfMessages", "") or "visible": int(v) for k, v in a.items()}


def failures_logged(logs, group, since_ms):
    n, types, token = 0, set(), None
    while True:
        kw = {"logGroupName": group, "startTime": since_ms, "filterPattern": '"agent turn failed for SQS message"'}
        r = logs.filter_log_events(**kw, **({"nextToken": token} if token else {}))
        for e in r["events"]:
            n += 1
        token = r.get("nextToken")
        if not token:
            break
    token = None
    while True:
        r = logs.filter_log_events(logGroupName=group, startTime=since_ms, filterPattern='"JSONDecodeError"',
                                   **({"nextToken": token} if token else {}))
        types |= {"JSONDecodeError"} if r["events"] else set()
        token = r.get("nextToken")
        if not token:
            break
    return n, sorted(types)


def wait_for_dlq(sqs, dlq_url, minutes, label):
    end = time.time() + minutes * 60
    while time.time() < end:
        d = depth(sqs, dlq_url)
        if sum(d.values()) >= 1:
            note(f"{label}: message is in the DLQ", dlq=d)
            return True
        time.sleep(30)
    note(f"{label}: timed out waiting for the DLQ")
    return False


def peek(sqs, dlq_url, tag):
    """Receive from the DLQ briefly, keep only the tracked message's facts, and make it visible again."""
    r = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, VisibilityTimeout=30, WaitTimeSeconds=5,
                            AttributeNames=["All"])
    msgs = r.get("Messages", [])
    mine = [m for m in msgs if tag in m["Body"]]
    others = [m for m in msgs if tag not in m["Body"]]
    for m in msgs:
        sqs.change_message_visibility(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
    return mine, others


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "drill_dlq.json"))
    ap.add_argument("--resume-tag", help="continue tracking an already-sent drill message (no new message is sent)")
    ap.add_argument("--resume-id")
    ap.add_argument("--sent-at", help="ISO time the resumed message was sent (UTC)")
    ap.add_argument("--after-redrive", help="ISO time of a redrive that already completed: run phase 2 only")
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    sqs, logs, lam = c("sqs"), c("logs"), c("lambda")
    q_url = out["QueueUrl"]
    qa = sqs.get_queue_attributes(QueueUrl=q_url, AttributeNames=["QueueArn", "RedrivePolicy", "VisibilityTimeout"])["Attributes"]
    dlq_arn = json.loads(qa["RedrivePolicy"])["deadLetterTargetArn"]
    dlq_url = sqs.get_queue_url(QueueName=dlq_arn.rsplit(":", 1)[-1])["QueueUrl"]
    agent = lam.get_function_configuration(FunctionName="setuhaul-pilot-agent")
    group = agent.get("LoggingConfig", {}).get("LogGroup") or "/aws/lambda/setuhaul-pilot-agent"
    esm = lam.list_event_source_mappings(FunctionName="setuhaul-pilot-agent")["EventSourceMappings"]
    owner = OwnerSession(c("rds-data"), out["ClusterArn"], out["OwnerSecretArn"])
    # cumulative write counters: they count every insert/update whatever row-level security would show
    receipts = lambda: list(owner.query("SELECT n_tup_ins, n_tup_upd FROM pg_stat_user_tables "  # noqa: E731
                                        "WHERE schemaname = 'setuhaul' AND relname = 'inbound_receipts'")[0])

    ev = {"queue": q_url.rsplit("/", 1)[-1], "dlq": dlq_url.rsplit("/", 1)[-1],
          "max_receive_count": json.loads(qa["RedrivePolicy"])["maxReceiveCount"],
          "visibility_timeout_s": int(qa["VisibilityTimeout"]),
          "consumer": [{"state": m["State"], "batch_size": m["BatchSize"], "response": m.get("FunctionResponseTypes")}
                       for m in esm]}
    before = {"queue": depth(sqs, q_url), "dlq": depth(sqs, dlq_url), "inbound_receipts": receipts()}
    ev["before"] = before
    if a.resume_tag:
        # run 1 sent the message and stopped when the operator's AWS session expired; the message kept retrying
        sent_ms = int(datetime.fromisoformat(a.sent_at).timestamp() * 1000)
        n, _ = failures_logged(logs, group, sent_ms)
        ids = set()
        tok = None
        while True:
            r = logs.filter_log_events(logGroupName=group, startTime=sent_ms,
                                       filterPattern='"agent turn failed for SQS message"', **({"nextToken": tok} if tok else {}))
            ids |= {re.search(r"message (\S+)", e["message"]).group(1) for e in r["events"]}
            tok = r.get("nextToken")
            if not tok:
                break
        in_flight = sum(before["queue"].values()) + sum(before["dlq"].values())
        tracked = set(a.resume_id.split(","))      # SQS gives a redriven message a new id; both are the tracked one
        if ids - tracked or in_flight != 1:
            note("STOP: on resume, something other than the tracked message is in flight", ids=sorted(ids), before=before)
            return 2
        ev["resumed"] = {"from_run": 1, "failures_so_far": n, "only_tracked_message_failing": True,
                         "baseline_note": "inbound_receipts counters taken at resume; run 1 recorded none before its crash"}
        tag, t0 = a.resume_tag, sent_ms
        ev["message"] = {"tag": tag, "message_ids": sorted(tracked), "group": tag, "sent_at": a.sent_at, "note": "the redrive gave the message a new SQS id; identity is proven by the body tag in the DLQ"}
        note("resumed tracking", message_id=a.resume_id, failures_so_far=n)
    elif sum(before["queue"].values()) or sum(before["dlq"].values()):
        note("STOP: a queue is not empty; the drill would touch other messages", before=before)
        ev["stopped"] = True
        Path(a.out).write_text(MASK.sub("************", json.dumps({"evidence": ev, "log": LOG}, indent=1, default=str)) + "\n", encoding="utf-8")
        return 2
    if not esm or esm[0]["State"] != "Enabled":
        note("STOP: the agent's queue consumer is not enabled")
        return 2

    if not a.resume_tag:
        tag = f"STEP6-DLQ-DRILL-{uuid.uuid4().hex[:12]}"
        body = f"{tag} synthetic drill message: deliberately not a driver event (not JSON); no business data"
        t0 = int(time.time() * 1000)
        sent = sqs.send_message(QueueUrl=q_url, MessageBody=body, MessageGroupId=tag, MessageDeduplicationId=tag)
        ev["message"] = {"tag": tag, "message_id": sent["MessageId"], "group": tag, "sequence": sent.get("SequenceNumber"),
                         "sent_at": now()}
        note("sent the tracked message", message_id=sent["MessageId"], tag=tag)

    # ---- 1. dead-lettering
    ok1 = True if a.after_redrive else wait_for_dlq(sqs, dlq_url, 40, "phase 1")
    n1, types1 = failures_logged(logs, group, t0)
    mine, others = peek(sqs, dlq_url, tag)
    ev["dead_lettered"] = {"in_dlq": ok1, "agent_failures_logged": n1, "error_types": types1,
                           "dlq_message_matches_tag": len(mine) == 1, "other_messages_in_dlq": len(others),
                           "dlq_message_id": mine[0]["MessageId"] if mine else None,
                           "receive_count_on_source": None}
    if a.after_redrive:
        ok1, mine, others = True, [{"MessageId": "(moved by the earlier redrive)"}], []
    if not ok1 or len(mine) != 1 or others:
        note("STOP: the DLQ does not hold exactly the tracked message", mine=len(mine), others=len(others))
        ev["stopped"] = True
    else:
        # ---- 2. redrive (DLQ -> its source queue)
        if a.after_redrive:
            t1 = int(datetime.fromisoformat(a.after_redrive).timestamp() * 1000)
            note("continuing after the completed redrive", at=a.after_redrive)
        else:
            t1 = int(time.time() * 1000)
            task = sqs.start_message_move_task(SourceArn=dlq_arn)
            note("redrive started", task=task["TaskHandle"][:24] + "...")
        for _ in range(60):
            time.sleep(5)
            st = [x for x in sqs.list_message_move_tasks(SourceArn=dlq_arn, MaxResults=1)["Results"]]
            if st and st[0]["Status"] in ("FAILED", "CANCELLED"):
                break
            if st and st[0]["Status"] == "COMPLETED" and st[0].get("ApproximateNumberOfMessagesMoved") == 1                     and sum(depth(sqs, dlq_url).values()) == 0:
                break
        ev["redrive"] = {"status": st[0]["Status"], "moved": st[0].get("ApproximateNumberOfMessagesMoved"),
                         "to_move": st[0].get("ApproximateNumberOfMessagesToMove"), "destination": "source queue (default)"}
        note("redrive finished", **ev["redrive"])
        # ---- 3. back through the consumer, and dead-lettered again
        ok2 = wait_for_dlq(sqs, dlq_url, 40, "phase 2")
        n2, types2 = failures_logged(logs, group, t1)
        mine2, others2 = peek(sqs, dlq_url, tag)
        ev["after_redrive"] = {"redelivered_to_consumer": n2 > 0, "agent_failures_logged": n2, "error_types": types2,
                               "dead_lettered_again": ok2, "dlq_message_matches_tag": len(mine2) == 1,
                               "other_messages_in_dlq": len(others2)}
        # ---- 4. remove only the tracked message
        removed = False
        if len(mine2) == 1 and not others2:
            r = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, VisibilityTimeout=30, WaitTimeSeconds=5)
            for m in r.get("Messages", []):
                if tag in m["Body"]:
                    sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
                    removed = True
                else:
                    sqs.change_message_visibility(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
        ev["tracked_message_removed"] = removed
        note("tracked message removed from the DLQ" if removed else "tracked message NOT removed")
    time.sleep(10)
    after = {"queue": depth(sqs, q_url), "dlq": depth(sqs, dlq_url), "inbound_receipts": receipts()}
    ev["after"] = after
    checks = {
        "queues held nothing but the tracked message": (not sum(before["queue"].values()) and not sum(before["dlq"].values()))
            or ev.get("resumed", {}).get("only_tracked_message_failing") is True,
        "the tracked message was dead-lettered after repeated consumer failures":
            ev.get("dead_lettered", {}).get("in_dlq") is True and ev["dead_lettered"]["agent_failures_logged"] >= 5,
        "the DLQ held exactly the tracked message": ev.get("dead_lettered", {}).get("dlq_message_matches_tag") is True
            and ev["dead_lettered"]["other_messages_in_dlq"] == 0,
        "redrive moved exactly 1 message back to the source queue":
            ev.get("redrive", {}).get("status") == "COMPLETED" and ev["redrive"].get("moved") == 1,
        "the redriven message reached the consumer again and was dead-lettered again (nothing lost)":
            ev.get("after_redrive", {}).get("redelivered_to_consumer") is True and ev["after_redrive"]["dead_lettered_again"] is True,
        "every failure happened at the JSON parse (no business code ran)":
            set(ev.get("dead_lettered", {}).get("error_types", [])) == {"JSONDecodeError"},
        "no inbound receipt or turn was created (business records unchanged)": after["inbound_receipts"] == before["inbound_receipts"],
        "only the tracked message was removed; both queues empty after": ev.get("tracked_message_removed") is True
            and not sum(after["queue"].values()) and not sum(after["dlq"].values()),
    }
    report = {"collected_utc": now(), "evidence": ev, "checks": checks, "all_pass": all(checks.values()), "log": LOG}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k, flush=True)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
