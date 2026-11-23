"""RECOVERY_WORKER gate: forced lease loss on the deployed synthetic pilot (owner-authorised 23 Nov 2026).

    python scripts/drill_lease_loss.py [--out docs/release/evidence/drill_lease_loss.json]

One synthetic driver message (SYN-STEP6-LEASE-* driver, +999 number) goes through two failures in a row:
  1. the API "crashes" after storing it and before publishing it: the receipt is written through the ingest role with
     the same statements the service uses (chat_messages + inbound_receipts, enqueued), and nothing is sent to SQS;
  2. a worker W1 takes the processing lease (the service's claim_turn statement, 20-second lease) and "crashes": it
     never renews or finishes.
After the lease expires, W1 tries to carry on and must be refused three times: renew (renew_turn), the turn fence
(assert_turn_fence, as the agent role) and finish (finish_turn). Then the DEPLOYED recovery function (invoked once;
the schedules stay disabled) must re-claim the receipt and publish it to the real FIFO queue, the DEPLOYED agent must
take a new lease and finish the turn, and W1 must still be fenced out afterwards. One agent reply, no duplicate.
Preconditions (else STOP): the inbound queue is empty and no other receipt is unprocessed, so recovery can only touch
the drill's message. Booking stays off; any reply goes to the internal test sink. Account ids are masked.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.aws.data_api import DataApiDb  # noqa: E402
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402
from setuhaul.aws.synthetic_seed import VIOLATIONS, synthetic_phone  # noqa: E402

REGION, STACK, FAC = "ap-southeast-2", "SetuHaulPilot", "FAC-JAI-01"
MASK = re.compile(r"\b\d{12}\b")
W1, LEASE_S = "step6-crashed-worker-W1", 20
LOG: list[dict] = []


def c(n):
    return boto3.client(n, region_name=REGION)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def note(event, **kw):
    LOG.append({"t": now(), "event": event, **kw})
    print(now(), event, json.dumps(kw, default=str)[:280], flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "drill_lease_loss.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    logins = json.loads(out["LoginSecretArns"])
    rd, lam, sqs = c("rds-data"), c("lambda"), c("sqs")
    owner = OwnerSession(rd, out["ClusterArn"], out["OwnerSecretArn"])
    db = DataApiDb(out["ClusterArn"], "setuhaul", {r: logins[r] for r in ("ingest", "agent")}, rd, resume_wait_s=120)
    sc = ("SELECT set_config('app.facility_scope','FAC-JAI-01,FAC-GGN-01,CENTRAL',true)",)
    q = lambda s: [list(r) for r in owner.query(s, sc)]  # noqa: E731
    qa = sqs.get_queue_attributes(QueueUrl=out["QueueUrl"], AttributeNames=["ApproximateNumberOfMessages",
                                  "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    # receipts are driver-scoped by RLS; the owner reads them through pg_stat-free SQL as the table owner's scope
    open_receipts = q("""SELECT count(*) FROM setuhaul.inbound_receipts WHERE processed_at IS NULL""")[0][0]
    bad = owner.query(VIOLATIONS)
    ev = {"before": {"queue": qa, "unprocessed_receipts": open_receipts, "non_synthetic_rows": [list(x) for x in bad]}}
    note("before", **ev["before"])
    if bad or open_receipts or any(int(v) for v in qa.values()):
        note("STOP: not a clean starting point (other work could be touched)")
        Path(a.out).write_text(json.dumps({"evidence": ev, "stopped": True, "log": LOG}, indent=1, default=str) + "\n")
        return 2

    # ---- 1. a synthetic driver and conversation, and the stored-but-unpublished message (API crash after store)
    u = uuid.uuid4().hex[:8].upper()
    drv, veh, sid, tid = f"SYN-STEP6-LEASE-DRV-{u}", f"SYN-STEP6-LEASE-VEH-{u}", f"SYN-STEP6-LEASE-SHP-{u}", f"THR-STEP6L-{u}"
    phone = synthetic_phone(random.randrange(10**6, 10**10))
    owner.run(["SET LOCAL search_path = setuhaul", sc[0],
               f"INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES ('{drv}','SYN-CAR1','Step 6 lease drill','{phone}')",
               f"INSERT INTO vehicles VALUES ('{veh}','SYN-CAR1','32FT','SYN-STEP6L-REG-{u}',20000,false)",
               "INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,"
               "load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status) VALUES "
               f"('{sid}','SYN-STEP6L-ORD-{u}','SYN-CAR1','{drv}','{veh}','{FAC}',10000,45,now()-interval '3 hours',"
               "now()+interval '2 hours','IN_TRANSIT')"])
    ext = f"wamid.SYN.STEP6.LEASE.{uuid.uuid4().hex}"
    text = "Traffic near Neemrana, reaching the gate in about 2 hours"
    with db.tx("ingest", None, driver=drv) as t:
        t.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')", (tid, drv, sid))
        mid = t.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,external_message_id,content_hash,body_masked)
                           VALUES (%s,'DRIVER','WHATSAPP',%s,md5(%s),%s) RETURNING message_id""", (tid, ext, text, text)).fetchone()[0]
        t.execute("""INSERT INTO inbound_receipts (message_id, thread_id, attestation_status, enqueued, enqueued_at)
                     VALUES (%s,%s,'NOT_NEEDED',true,now() - interval '40 seconds')""", (mid, tid))
    ev["message"] = {"driver": drv, "shipment": sid, "thread": tid, "message_id": mid, "external_id": ext}
    note("stored, never published (API crash after store)", message_id=mid)

    # ---- 2. worker W1 takes the lease (claim_turn's statement) and crashes
    with db.tx("ingest", None, driver=drv) as t:
        t.execute("SELECT thread_lock(%s)", (tid,))
        gen = t.execute("""UPDATE inbound_receipts SET processing_owner=%s, deliveries=deliveries+1,
                                  processing_until=clock_timestamp()+make_interval(secs=>%s)
                            WHERE message_id=%s AND thread_id=%s AND processed_at IS NULL
                              AND (processing_until IS NULL OR processing_until < clock_timestamp())
                        RETURNING generation""", (W1, LEASE_S, mid, tid)).fetchone()[0]
    ev["w1"] = {"owner": W1, "generation": gen, "lease_s": LEASE_S}
    note("W1 took the lease and crashed", generation=gen)
    with db.tx("agent", (FAC,), driver=drv) as t:                      # while the lease is live, W1 passes the fence
        t.execute("SELECT assert_turn_fence(%s,%s,%s)", (mid, W1, gen))
    ev["w1"]["fence_while_lease_live"] = "passed"
    time.sleep(LEASE_S + 6)

    # ---- 3. after expiry W1 is refused: renew, fence, finish
    def renew():
        with db.tx("ingest", None, driver=drv) as t:
            t.execute("SELECT thread_lock(%s)", (tid,))
            return t.execute("""UPDATE inbound_receipts SET processing_until=clock_timestamp()+make_interval(secs=>%s)
                                 WHERE message_id=%s AND processing_owner=%s AND generation=%s AND processed_at IS NULL
                                   AND processing_until > clock_timestamp() RETURNING 1""", (LEASE_S, mid, W1, gen)).fetchone()

    def fence():
        try:
            with db.tx("agent", (FAC,), driver=drv) as t:
                t.execute("SELECT assert_turn_fence(%s,%s,%s)", (mid, W1, gen))
            return "passed"
        except Exception as e:                                         # noqa: BLE001 — the expected refusal
            return (getattr(getattr(e, "diag", None), "message_primary", None) or str(e))[:120]

    def finish():
        with db.tx("ingest", None, driver=drv) as t:
            t.execute("SELECT thread_lock(%s)", (tid,))
            return t.execute("""UPDATE inbound_receipts SET processed_at=now(), processing_until=NULL
                                 WHERE message_id=%s AND processing_owner=%s AND generation=%s AND processed_at IS NULL
                                   AND processing_until > clock_timestamp() RETURNING 1""", (mid, W1, gen)).fetchone()
    ev["w1_after_expiry"] = {"renew": "refused" if renew() is None else "ACCEPTED",
                             "fence": fence(), "finish": "refused" if finish() is None else "ACCEPTED"}
    note("W1 after lease expiry", **ev["w1_after_expiry"])

    # ---- 4. the deployed recovery function re-publishes; the deployed agent takes it over
    t0 = int(time.time() * 1000)
    rec = json.loads(lam.invoke(FunctionName="setuhaul-pilot-recovery", Payload=b"{}")["Payload"].read() or b"null")
    ev["recovery"] = rec
    note("recovery invoked", result=rec)
    row = None
    for _ in range(60):
        row = q(f"""SELECT processed_at IS NOT NULL, processing_owner, deliveries, generation, published_at IS NOT NULL
                    FROM setuhaul.inbound_receipts WHERE message_id = {mid}""")[0]
        if row[0]:
            break
        time.sleep(5)
    replies = q(f"""SELECT message_id, reply_generation, left(body_masked, 80) FROM setuhaul.chat_messages
                    WHERE reply_to = {mid} AND sender_type = 'AGENT'""")
    ev["after_recovery"] = {"processed": row[0], "owner": row[1], "deliveries": row[2], "generation": row[3],
                            "published": row[4], "agent_replies": replies}
    note("after recovery", **ev["after_recovery"])
    ev["w1_after_takeover"] = {"fence": fence(), "finish": "refused" if finish() is None else "ACCEPTED"}
    logs = c("logs")
    g = lam.get_function_configuration(FunctionName="setuhaul-pilot-agent").get("LoggingConfig", {}).get("LogGroup") \
        or "/aws/lambda/setuhaul-pilot-agent"
    turns = logs.filter_log_events(logGroupName=g, startTime=t0, filterPattern='"agent turn message="')["events"]
    ev["agent_turn_logs"] = [re.sub(r"\s+", " ", e["message"])[:200] for e in turns]
    outbound = json.loads(lam.invoke(FunctionName="setuhaul-pilot-outbound", Payload=b"{}")["Payload"].read() or b"null")
    notes_ = q(f"""SELECT template_id, status, left(coalesce(provider_message_id,''), 9) FROM setuhaul.notifications
                   WHERE shipment_id = '{sid}'""")
    ev["notifications"] = notes_
    qa2 = sqs.get_queue_attributes(QueueUrl=out["QueueUrl"], AttributeNames=["ApproximateNumberOfMessages",
                                   "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    ev["after"] = {"queue": qa2, "outbound": outbound}
    checks = {
        "clean start: empty queue, no other unprocessed receipt, all data synthetic": True,
        "W1 passed the fence while its lease was live": ev["w1"]["fence_while_lease_live"] == "passed",
        "after lease expiry W1's renewal was refused (an expired lease is never revived)": ev["w1_after_expiry"]["renew"] == "refused",
        "after lease expiry W1 was fenced out (TURN_FENCED)": "TURN_FENCED" in ev["w1_after_expiry"]["fence"],
        "after lease expiry W1 could not mark the message processed": ev["w1_after_expiry"]["finish"] == "refused",
        "the deployed recovery function re-published exactly this message to the real queue":
            isinstance(rec, dict) and rec.get("republished") == 1,
        "the deployed agent took over the lease and finished the turn": bool(row[0]) and row[1] not in (None, W1) and row[2] >= 2,
        "exactly one agent reply for the message (no duplicate)": len(replies) == 1,
        "W1 still fenced out after the takeover": "TURN_FENCED" in ev["w1_after_takeover"]["fence"]
            and ev["w1_after_takeover"]["finish"] == "refused",
        "any driver notice went to the internal test sink only": all(n[2] in ("", "testsink.") for n in notes_),
        "queue empty afterwards": not any(int(v) for v in qa2.values()),
    }
    report = {"collected_utc": now(), "evidence": ev, "checks": checks, "all_pass": all(checks.values()), "log": LOG}
    Path(a.out).write_text(MASK.sub("************", json.dumps(report, indent=1, default=str)) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("ok " if v else "NO ") + k, flush=True)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
