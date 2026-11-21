# B-WF-3: durable outbound notifications

Built locally on 19 Nov 2026 (migration `0017_outbound_delivery.sql`, decision T51). It builds on the B-WF-2 boundary:
every outcome writes its event in the same transaction, and the driver's notification reads that event, never the
workflow. Closes review finding S4-R2-07 locally. Real-provider acceptance is the OUTBOUND_DELIVERY gate in step 6.

## Flow

```
agent reply row (chat_messages, AGENT) ──trigger──▶ outbox AgentReplyStored ──┐
ApprovalDecided, policy BookingConfirmed (outbox) ─────────────────────────────┼─▶ enqueue_notifications()
                       one row per (event, channel); id = md5(event : channel : recipient); consumed in the same tx
claim_notifications() ──▶ provider.send(to, text, idempotency_key = notification id) ──▶ record_send_result()
provider status webhook ──▶ record_delivery_status()  (provider id, or our id echoed back)
reconcile_notifications(): stuck SENDING → UNKNOWN; UNKNOWN past its deadline → one resend with the same id, then FAILED
```

## Guarantees and their limits

| Property | How | Test |
|---|---|---|
| The stored reply is what is sent (S4-R2-07) | The notification carries only the message id and content hash. `claim_notifications()` returns the stored body and refuses with `CANONICAL_MISMATCH` (FAILED, plus escalation) if the hash no longer matches. The relay has no model and no text of its own besides fixed templates | `test_s4_r2_07_*` |
| Stable notification ids | `md5('setuhaul-notification:' ‖ event ‖ channel ‖ recipient)`; unique per (event, channel) and per (reply, channel) | `test_an_agent_reply_becomes_one_notification_with_a_stable_id` |
| Enqueueing is idempotent | `consume_outbox_event('notification-enqueue')` in the same transaction as the insert | same |
| Retries | RETRY with exponential backoff (30 s × 2ⁿ⁻¹), at most 4 attempts, then FAILED with a NOTIFICATION_FAILED escalation | `test_retryable_*`, `test_retries_used_up_*`, `test_a_permanent_error_*` |
| Ambiguous sends are never blindly re-sent | A timeout means UNKNOWN. A receipt in the 2-minute window resolves it; otherwise one resend with the **same id**; a second ambiguity means FAILED, never a third send (enforced by the 0001 trigger) | `test_a_timeout_*`, `test_a_second_ambiguity_*` |
| Lost provider answer reconciled | A worker that dies mid-send leaves SENDING; reconciliation moves it to UNKNOWN (`WORKER_LOST`), then the one resend | `test_a_lost_provider_answer_*` |
| Stale workers are fenced | `record_send_result` needs the current attempt number; a late answer only records the provider id | `test_a_stale_worker_*` |
| Concurrent relays | `FOR UPDATE SKIP LOCKED` claim; the claim commits before sending | `test_concurrent_relays_*` |
| A person takes over | A reply stored **before** the takeover is not sent (PENDING), not retried (RETRY) and not resent (UNKNOWN: it may already be on the phone, so it fails as `HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND` and the coordinator is told). `begin_send()` re-checks the takeover and the attempt number just before the provider call; the gap between that check and the HTTP call cannot be fenced. A reply stored after the takeover (the safety hand-over message) is sent (0018, 0019) | `test_a_person_taking_over_*`, `test_r3_takeover_*`, `test_f5_*` |
| One message per turn | Only turn replies (`reply_to` set) are sent; the read-back evidence row is never sent on its own. In-turn policy confirmations get no template (0018) | `test_f1_*`, `test_f8_*` |
| Receipts | Through the existing signed webhook (`value.statuses`); repeats, unknown ids and out-of-order receipts are harmless. A receipt that cannot be stored makes the webhook answer 503, so the provider redelivers (0019 review fix) | `test_delivery_receipts_*`, `test_r3_a_receipt_that_cannot_be_stored_is_not_acknowledged` |

**Delivery is at least once, with at most one same-id resend per notification. It is never exactly once.**

- **A provider that honours an idempotency key** absorbs the resend. `LocalProvider(idempotent=True)` models this; the
  test shows one message on the phone.
- **WhatsApp error mapping (review R3).** Connection failures and HTTP 429 are retryable: the message was not taken.
  Any 5xx is ambiguous, because nothing in Meta's contract says a 5xx means "not accepted". It waits for a receipt,
  then gets at most one same-id resend. A blind retry could put several copies on the phone.
- **The WhatsApp Cloud API has no idempotency key** (`supports_idempotency = False`). After an ambiguous send the
  driver may see one identical repeat; the test shows exactly two identical messages and never three. Our id travels
  as `biz_opaque_callback_data`, so a receipt can resolve an UNKNOWN without the provider's message id. That makes the
  resend rare, but not impossible.

## Step 6 and other open points

- **Approved templates.** WhatsApp only lets a business start a conversation, or write after 24 hours of silence,
  with a Meta-approved template message. The fixed English texts in `setuhaul/notify/outbound.py::TEMPLATES` must
  become approved templates. Hinglish and Hindi variants were approved in the native-speaker review (S4-R2-08,
  21 Nov 2026) and are sent in the conversation's language (`notify/outbound.py`, `template_text`).
- **Real-provider acceptance (OUTBOUND_DELIVERY gate):** WhatsApp sends, real receipt webhooks and timeouts, the
  duplicate rate after ambiguous sends, and monitoring of undelivered notifications.
- **Coordinator notifications** (PAGE, CONSOLE) are not built. Failures already raise a NOTIFICATION_FAILED escalation,
  which the console lists.
