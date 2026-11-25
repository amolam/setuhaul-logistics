# Dispatcher booking, Telegram, reassignment, cancellation: workflow contracts and AWS readiness

This covers branch `feature/dispatcher-booking-telegram-dashboards` (migrations 0035 and 0036). It is a review and preparation document: nothing here is deployed, provisioned or activated.

## 1. Workflow contracts (authoritative states)

Each state lives in its own table. No screen merges two of them into one status.

| Concern | Where | States |
|---|---|---|
| Offer (business) | `dispatch_offers.status` | OFFERED, ACCEPTED, DECLINED, ALTERNATIVE_REQUESTED, EXPIRED (an OFFERED row past `expires_at` reads as EXPIRED), SUPERSEDED, UNAVAILABLE (with `status_reason`) |
| Offer kind | `dispatch_offers.kind` | INITIAL_BOOKING, REASSIGNMENT (before any booking), CONFIRMED_REASSIGNMENT |
| Booking request | `appointments.status` | PENDING_CONFIRMATION, then CONFIRMED / REJECTED / EXPIRED; also CANCELLED |
| Planner decision | `approval_requests.status` | OPEN, then APPROVED / REJECTED / EXPIRED / SUPERSEDED / WITHDRAWN. Dispatcher bookings are `PLANNER_REVIEW`. |
| Cancellation | `cancellation_requests.status` | OPEN, then APPROVED / REJECTED |
| Delivery | `notifications.status` | PENDING, SENDING, SENT, RETRY, UNKNOWN (timeout: may have arrived), FAILED (escalated), DELIVERED (WhatsApp receipts only) |
| Telegram link | `driver_channel_links`, `driver_link_codes`, `driver_link_attempts` | linked / revoked; each code is open, consumed, revoked or expired |
| Escalation | `escalations.status` | OPEN, then ACKNOWLEDGED (owned, not resolved), then RESOLVED |

**Three different timers:**

| Timer | Length | What it covers |
|---|---|---|
| Offer TTL | `SETUHAUL_OPTION_TTL_S`, 300 s | How long a driver may answer. It reserves nothing. |
| Dock hold | 180 s | From the driver's yes to the booking request, within one flow. |
| The request's claim | `pending_expires_at`, the per-shift confirm TTL | At submit, the hold row *becomes* the request's APPOINTMENT claim, which has no expiry of its own. It holds the dock time until the planner decides or the request expires. |

**How a decision re-checks.** `decide_booking_request` locks the request, the appointment and the claim, then refuses `PENDING_EXPIRED`, `CAPACITY_LOST`, `SLOT_TOO_LATE` and compatibility changes. A stale schedule can therefore never produce a false approval. The console shows the 409 and refreshes.

**Rules enforced in the database:**
- Driver acceptance is never confirmation: only a planner or ops_lead confirms.
- The `operations` role (the temporary dispatcher) cannot decide `PLANNER_REVIEW`.
- A confirmed reassignment changes only `shipments.driver_id`, after the new driver's yes and under row locks. It never touches the appointment row or its dock claim.
- A cancellation cancels the appointment and releases its dock claim in one transaction, decided by someone other than the requester.
- Cancellation and reassignment exclude each other.

## 2. AWS readiness checklist (controlled synthetic pilot)

| Item | State on this branch | Evidence still needed / owner decision |
|---|---|---|
| Migrations 0035, 0036 | Forward-only. Verified on local PostgreSQL 18.6 and through the emulated Data API. | CI on PostgreSQL 17.9, and the `migrate_data_api` run in the pilot pipeline at deploy time. |
| DB roles and grants | No new database role. New definer functions are owned by `setuhaul_rpc`. The pinned privilege tests were updated deliberately (ingest +7 functions; console reads). | Review the grant diff. |
| Cognito | 0037: a dedicated `dispatcher` group (CDK) and role (API and database). It has dispatch actions only and no decisions. `operations` keeps its dispatch permissions for compatibility. | **Owner:** deploy the group, assign dispatchers, and decide later when `operations` stops dispatching (`TELEGRAM_TEST_BOT_VALIDATION_REPORT.md` §B). |
| Telegram webhook route | `/webhooks/telegram` exists only when `SETUHAUL_TELEGRAM_WEBHOOK_SECRET` (≥ 32 characters) is set. Not set in the CDK stack, so the route is absent in the pilot. The existing `{proxy+}` API route would serve it; no API Gateway change is needed. | **Owner:** create the webhook secret in Secrets Manager, add an environment mapping for the API function (CDK change, review needed), then `setWebhook` with `secret_token`. |
| Telegram bot token | Read only from Secrets Manager (`SETUHAUL_TELEGRAM_BOT_SECRET_ID`), and only when `SETUHAUL_TELEGRAM_DELIVERY=live` **and** `SETUHAUL_TELEGRAM_LIVE_AUTHORIZED=true`. | **Owner:** create the bot (BotFather), store the token, grant the outbound function read access (CDK/IAM change, review needed). Real sends need explicit authorization. |
| Outbound routing | The relay routes linked drivers to Telegram. The default transport is the in-process test sink, and unlinked drivers keep the existing route. `test_bot` mode means a dedicated test bot, allowlisted chats only, and `local`/`dev` only. | **Validated over the real Bot API** with the owner's test bot (report §D.0, 25 Nov 2026): link, revoke and relink, offers and buttons, journeys A, C, E and F, both planner outcomes, typed replies. Not exercised for real: sharing a location, "Not now", a real failure in the Ops view. |
| Migrations 0037–0040 | 0037 dispatcher role; 0038 offer message lookup; 0039 a decided request no longer blocks re-offers; 0040 a closed offer can never be booked through the conversation. All forward-only and additive. | They run in the existing `migrate_data_api` step at deploy time. 0039 and 0040 fix defects found in the real run. |
| Retries, idempotency | Unchanged relay semantics: the same claim, the RETRY back-off, UNKNOWN on timeout with at most one same-id resend, and FAILED with an escalation. Updates and callbacks are deduplicated by id. | — |
| Link-code abuse | 5 failures per account per 15 min; 100 failures per 5 min across all accounts. Attempts are audited without codes. | Add a CloudWatch metric or alarm on THROTTLED attempts (CDK change). |
| Monitoring | Existing alarms cover 5xx, 4xx, the DLQ and function errors. The Ops health tab shows the last Telegram inbound time. | **Optional:** an alarm on no Telegram updates for N hours once live. |
| Driver traceability (0041) | Traces, `agent.turn` log lines and the Ops view name a turn's driver and shipment by internal id, format-gated (`[INVALID_ID]` otherwise). No phone, name, Telegram id, coordinates or text. Ops view: Driver and Shipment columns, driver filter, planner/ops_lead only, facility-scoped (`docs/observability/DRIVER_TRACEABILITY.md`). | 0041 runs in the existing `migrate_data_api` step at deploy time. Tracing stays off until the owner enables it. |
| Offline LLM judge (0042) | Advisory clarity/tone/language scores on a frozen synthetic dataset only; dry run by default; paid calls through the existing metered OpenRouter route and shared usage ledger (purpose `judge`) behind a per-run cap of USD 1 and a daily cap of USD 3. Never gates anything, never sees pilot traces (`docs/evaluation/LLM_JUDGE.md`). | **Owner:** approve the dry-run report before the first paid run (~30 calls, ≤ USD 0.35 upper bound); hand-score the 20 calibration replies. 0042 runs in `migrate_data_api` at deploy time (widens one CHECK). |
| Redaction | Location is stored as a 7-character geohash. Link codes are hashed. The Telegram id and chat are never sent to the console. The bot token is never logged. | — |
| Kill switch, evidence guard | Untouched. Cancellation and driver changes are not "new commitments", so both stay enforced. | — |
| Workers | Scheduled rules are still disabled by default. Nothing on this branch wakes them. | — |
| Deploy and rollback | No change to `ci.yml` deploy jobs, beyond adding the new tests to the Data API test list. Deploying remains release-branch only, with typed confirmation. | Merge, release and activation are separate owner decisions with fresh evidence. |
