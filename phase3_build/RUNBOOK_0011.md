# Runbook: cutting over to the inbound outbox (migration 0011)

Review R1.9, finding S4-04. Migration 0011 marks every existing inbound receipt as published and processed, so the new
recovery sweep does not replay history to drivers. Under the old path, a receipt with an enqueue claim proves only that
the claim was recorded, not that the in-memory queue ever received the event. This runbook closes that gap.

**Where it applies:** any environment holding real inbound messages from before 0011. So far 0011 has run only on test
databases that are rebuilt on every run; no deployed environment has pre-0011 data.

## Steps

1. **Pause ingestion.** Point the WhatsApp webhook at the maintenance response (HTTP 503), so Meta keeps retrying and nothing is acknowledged.
2. **Drain the old queue.** Let the running workers finish every event already in the in-memory queue. Stop them when the queue is empty and no turn has run for 2 minutes.
3. **Apply 0011 and later migrations** (`python -m setuhaul.db.migrate`).
4. **List the uncertain receipts:**

   ```bash
   python -m setuhaul.ops.cutover_0011 check
   ```

   It lists **every** receipt that 0011 marked processed although no processing claim was ever taken. Later agent
   messages in the same conversation do not exclude a receipt (review R2.0, S4-R2-02): an agent message may answer a
   later driver message, not this one. They are shown beside it, with the next driver message, to help the review.
   The exit code is 1 when there are any.
5. **Review each one** with operations: was this message answered (by the agent, a coordinator or by phone), and is the conversation still live? Record the decision in the change ticket.
6. **Re-open the ones that still need an answer:**

   ```bash
   python -m setuhaul.ops.cutover_0011 reopen 1234 1240
   ```

   Each gets a new generation. The next recovery sweep publishes it, and the processing lease gives it exactly one turn.
7. **Resume ingestion** through the new outbox. Watch the first sweep's count and the dead-letter queue.

## What it does not do

It never changes historical receipts by itself. Only the IDs an operator passes to `reopen` are touched, and only those
that 0011 backfilled: published and processed, with no processing owner and no deliveries. A mistyped ID of a new,
not-yet-processed message is refused (S4-R2-03).
