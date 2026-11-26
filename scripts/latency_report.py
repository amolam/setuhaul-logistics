"""Read-only: end-to-end Telegram latency on the synthetic pilot, per stage, from the timestamps PostgreSQL records.

    python scripts/latency_report.py                       # the two test drivers, last 2 hours
    python scripts/latency_report.py --minutes 30 --drivers SYN-DRV-TEST-1

For each driver message that got an agent reply, the stages are:
  received   chat_messages.created_at of the DRIVER message (the webhook's transaction: received and persisted)
  queued     inbound_receipts.published_at (the event is on the SQS queue)
  reply      chat_messages.created_at of the AGENT reply to it (agent done; its outbox event committed)
  outbound   notifications.created_at (the relay turned the reply into a notification)
  sending    notifications.sending_started_at (the provider call began)
  sent       notifications.updated_at of the SENT notification (the provider accepted it)
It prints each interaction and p50 / p95 for every stage and end to end (received -> sent). Ids and timings only:
no message text, no chat id. Runs in a READ ONLY transaction over the RDS Data API.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
SCOPE = "SELECT set_config('app.facility_scope', 'FAC-JAI-01,FAC-GGN-01,CENTRAL', true)"
SQL = """
SELECT t.driver_id, d.message_id, d.created_at, r.published_at, a.created_at, n.created_at, n.sending_started_at,
       CASE WHEN n.status IN ('SENT', 'DELIVERED') THEN n.updated_at END, n.status,
       split_part(coalesce(n.provider_message_id, ''), '.', 1)
  FROM setuhaul.chat_messages d
  JOIN setuhaul.chat_threads t ON t.thread_id = d.thread_id
  LEFT JOIN setuhaul.inbound_receipts r ON r.message_id = d.message_id
  LEFT JOIN LATERAL (SELECT message_id, created_at FROM setuhaul.chat_messages x
                      WHERE x.reply_to = d.message_id AND x.sender_type = 'AGENT' ORDER BY x.message_id LIMIT 1) a ON true
  LEFT JOIN LATERAL (SELECT * FROM setuhaul.notifications y WHERE y.chat_message_id = a.message_id
                      ORDER BY y.created_at LIMIT 1) n ON true
 WHERE d.sender_type = 'DRIVER' AND d.channel = 'TELEGRAM' AND t.driver_id = ANY(%s)
   AND d.created_at > now() - make_interval(mins => %s)
 ORDER BY d.created_at"""


def _ts(v):
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace(" ", "T").replace("+00", "+00:00") if str(v).endswith("+00")
                                  else str(v).replace(" ", "T"))


def pct(xs, p):
    xs = sorted(xs)
    return None if not xs else xs[min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=120)
    ap.add_argument("--drivers", default="SYN-DRV-TEST-1,SYN-DRV-TEST-2")
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in boto3.client("cloudformation", region_name=REGION)
           .describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    db = OwnerSession(boto3.client("rds-data", region_name=REGION), out["ClusterArn"], out["OwnerSecretArn"])
    drivers = [d for d in a.drivers.split(",") if d]
    sql = SQL.replace("ANY(%s)", "ANY(ARRAY[" + ",".join("'" + d.replace("'", "") + "'" for d in drivers) + "])")
    sql = sql.replace("make_interval(mins => %s)", f"make_interval(mins => {int(a.minutes)})")
    rows = db.query(sql, (SCOPE,))
    stages = {"received->queued": [], "queued->reply": [], "reply->outbound": [], "outbound->sending": [],
              "sending->sent": [], "received->sent (end to end)": []}
    print("driver           msg        received             queue  reply  outbnd  start   sent   total  status")
    for drv, mid, rec, pub, rep, nq, ns, sent, st, via in rows:
        rec, pub, rep, nq, ns, sent = map(_ts, (rec, pub, rep, nq, ns, sent))
        d = lambda x, y: (y - x).total_seconds() if x and y else None   # noqa: E731
        vals = [d(rec, pub), d(pub, rep), d(rep, nq), d(nq, ns), d(ns, sent), d(rec, sent)]
        for k, v in zip(stages, vals):
            if v is not None:
                stages[k].append(v)
        f = lambda v: f"{v:6.1f}" if v is not None else "     -"      # noqa: E731
        print(f"{drv:<16} {mid:<10} {str(rec)[:19]}  {'  '.join(f(v) for v in vals)}  {st or 'no reply'} {via or ''}")
    print("\nstage                         n     p50     p95     max   (seconds)")
    for k, xs in stages.items():
        if xs:
            print(f"{k:<28} {len(xs):>3}  {pct(xs, 50):6.1f}  {pct(xs, 95):6.1f}  {max(xs):6.1f}")
    if stages["received->sent (end to end)"]:
        print(f"\nmean end to end: {statistics.mean(stages['received->sent (end to end)']):.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
