# Telegram test bot, dispatcher role, journey validation: report

Branch `feature/dispatcher-booking-telegram-dashboards`, on top of `d37c938`. This cycle did no push, merge, deployment or AWS provisioning, and sent no real Telegram message.

**Headline:**
- The dedicated `dispatcher` role is implemented and tested.
- The test-bot mode is implemented and fails closed.
- Every journey (A to G) passes against the in-process test sink and against a fake Bot API.
- Real Telegram Bot API validation is **BLOCKED**. No test-bot token or test chat id was available in this environment, and credentials must not be requested in chat. The owner-run runner `scripts/telegram_test_bot.py` is ready, and §D.3 gives the exact steps.

---

## A. Repository state

| Item | Value |
|---|---|
| Branch | `feature/dispatcher-booking-telegram-dashboards` |
| Baseline | `d37c938`; all six earlier commits present and unchanged |
| Commits this cycle | `ba02d9c` dispatcher role (0037); `8ef0148` Telegram test-bot mode and runner; then the commit that adds this report |
| Working tree | clean after the final commit |
| Push / merge / deploy / AWS provisioning | none |
| `main`, `release/fde-sydney-pilot`, migration 0034 | untouched |

Correction to the earlier summary: `27f44e0` is "Location assessor and Telegram transport helpers (pure modules)". The Phase 0 gap report lives in the shared notes folder, not in a commit.

## B. Cognito dispatcher group

**Was it straightforward?** Yes. Roles are passed from `cognito:groups` into the transaction's staff identity (`begin_staff_context`) and checked again inside each definer function. A new role therefore needed:

| Layer | Change |
|---|---|
| CDK (`infra/setuhaul_stack.py`) | A `dispatcher` group (`AWS::Cognito::UserPoolGroup`), alongside the existing three. Additive: one new resource. |
| API (`security.py`, `service.py`) | `dispatcher` added to `STAFF_ROLES`. It gets `view_dispatch`, `send_offer`, `link_driver` and `request_cancellation`. |
| Database (`0037_dispatcher_role.sql`) | `begin_staff_context` accepts the role. `staff_role_for` ranks it lowest. The five dispatch functions accept it: send an offer, issue a link code, revoke a link, reassign a confirmed appointment, request a cancellation. These are `CREATE OR REPLACE` of the current bodies with only the role list changed, so owners and grants are kept. |
| Console (`app.js`) | Tabs the server would refuse are hidden. A dispatcher-only account starts on Dispatch. Every call is still checked by the server. |

**What a dispatcher cannot do:**
- Approve or reject a booking request, a `PLANNER_REVIEW`, a move or a cancellation. `approval_role_rules` is unchanged; its CHECK does not even admit `dispatcher`.
- See Schedule, Ops health, the kill switch, conversations, the optimizer or observability.

The database refuses these decisions on its own, independently of the API (tested).

**Compatibility.** `operations` keeps every permission it had, including all dispatch actions. Nothing was removed or reinterpreted.

**Moving users (owner, after the CDK change is deployed):**
1. Deploy the stack change. This adds the empty group; nobody gains access.
2. For each person who dispatches, `aws cognito-idp admin-add-user-to-group --user-pool-id <pool> --username <user> --group-name dispatcher`. Keep their facility group.
3. Once they work as `dispatcher`, decide separately whether to remove them from `operations`. Removing `operations` from the dispatch permission lists is a later change that needs its own approval.

**Rollback:**
- Remove users from `dispatcher` with `admin-remove-user-from-group`. Users still in `operations` lose nothing.
- Code rollback: redeploy the previous release. Migration 0037 is forward-only and harmless when nobody holds the role. To revert it, re-run the 0035/0036 function bodies.

**Tests:** see the `test_dispatcher_role.py` row in §G. The module covers:
- `/console/me` for a dispatcher;
- the full journey as a dispatcher (link, offer, accept, 403 on approval, planner confirms, cancellation request, 403 on the cancellation decision, planner rejects, reassignment, revoke);
- 403 on the schedule, ops, notification, cancellation-list and kill-switch endpoints;
- the database refusing `decide_booking_request` for a dispatcher;
- unknown roles still rejected;
- `operations` compatibility.

**Owner approval still needed:** deploying the CDK change, and assigning real users.

## C. Test-bot configuration

One loader, `setuhaul.notify.telegram.telegram_from_env`, chooses Telegram delivery for both the relay (Lambda `telegram_transport`) and webhook replies (API settings).

| `SETUHAUL_TELEGRAM_DELIVERY` | Effect |
|---|---|
| `test_sink` (default) | In-process sink; nothing leaves the process. Automated tests use only this or a fake Bot API. |
| `off` | No Telegram routing. |
| `test_bot` | The real Bot API for a dedicated **test** bot, wrapped in `AllowlistedTelegram`. |
| `live` | Unchanged; needs `SETUHAUL_TELEGRAM_LIVE_AUTHORIZED=true` and the token from Secrets Manager. Not used. |

**`test_bot` refuses to start (raises) unless all of these hold:**
- `SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED=true`. This is a separate flag from `..._LIVE_AUTHORIZED`, which neither enables nor widens test mode.
- `SETUHAUL_ENV` is `local` or `dev`. `pilot`, `staging`, `production` and unset are all refused.
- `SETUHAUL_TELEGRAM_TEST_CHAT_IDS` lists 1–10 positive numeric private-chat ids. Usernames and group ids are refused.
- A token that looks like a bot token, from Secrets Manager (`SETUHAUL_TELEGRAM_BOT_SECRET_ID`) or, locally, from `SETUHAUL_TELEGRAM_TEST_BOT_TOKEN`. The live mode never accepts a plain-variable token.

**The allowlist is enforced twice:**
- Outbound: a send to any other chat returns `PERMANENT/NOT_ALLOWLISTED` before any network call. The relay records FAILED with an escalation, so it is visible, never silent.
- Inbound: an update from an account outside the allowlist is not processed at all. There is no link, no reply and no Bot API call; the constant `{"ok": true}` is returned.

**Secrets:**
- The owner sets them in their own shell or in Secrets Manager.
- Nothing prints, logs or returns the token, the webhook secret or chat ids.
- In the provider, the token appears only in the request URL path, never in a body (tested).
- None of these values is in the repository.

**Webhook:**
- The route exists only when `SETUHAUL_TELEGRAM_WEBHOOK_SECRET` (≥ 32 characters) is set.
- `X-Telegram-Bot-Api-Secret-Token` is compared in constant time.
- Bodies are limited to 64 KB.
- Only private chats are accepted. This now also applies to callback queries; a callback from a group chat is ignored.
- Identity is the numeric `from.id`.
- Updates and callbacks are deduplicated by `tg:<update_id>` and `tgcb:<callback id>`.

**Isolation choice: local long polling, not a public webhook.** The runner (§D.3) polls `getUpdates` and passes each update to the **real** `/webhooks/telegram` route in-process, with a random per-run secret. Nothing is exposed to the internet, and no production or pilot routing changes.

**Public webhook alternatives, not configured:**
- A tunnel (for example `cloudflared`) to a local API. This exposes a developer machine, and the tunnel provider sees the traffic.
- An isolated AWS dev stack with its own API Gateway. This needs a new environment and owner approval.

Either one also needs `setWebhook` with `secret_token`, and long polling must then stop.

## D. Telegram validation results

### D.1 Real Telegram Bot API: BLOCKED (credentials not available here)

| # | Capability | Real Bot API | Covered by (fake Bot API / sink) |
|---|---|---|---|
| 1 | Send an offer | BLOCKED | PASS (fake API: `sendMessage` with text) |
| 2 | Inline Accept / Decline / Other time buttons | BLOCKED | PASS (fake API: `inline_keyboard` callback data `o:<offer>:A/D/R`) |
| 3 | Receive button callbacks | BLOCKED | PASS (webhook route, journeys A, B, C, E) |
| 4 | Clear buttons after an answer | BLOCKED | PASS (`answerCallbackQuery`, then `editMessageReplyMarkup`, then the reply; another driver's tap clears nothing) |
| 5 | Stale, duplicate and expired callbacks | BLOCKED | PASS (superseded, declined and expired offers refused; a redelivered callback creates no second booking action) |
| 6 | Alternative-time clarification | BLOCKED | PASS (sink: `R` gives ALTERNATIVE_REQUESTED and the reply asks for a time) |
| 7 | Typed YES / NO | BLOCKED | PASS (sink: "haan ji" accepts through the same path as the button) |
| 8 | Ambiguous typed reply | BLOCKED | PASS (sink: "haan lekin D3" is never guessed; the driver is asked again) |
| 9 | One-time `request_location` keyboard | BLOCKED | PASS (fake API: `keyboard[0][0].request_location`, `one_time_keyboard`, sent once per open delay) |
| 10 | Shared location | BLOCKED | PASS (assessed; 7-character geohash; raw coordinates not stored; live location ignored; reply removes the keyboard) |
| 11 | "Not now" | BLOCKED | PASS (recorded as UNAVAILABLE; the delay report stands; the keyboard is removed) |
| 12 | Booking approval / rejection outcome | BLOCKED | PASS (fake API: after the planner confirms, the relay sends the confirmation) |
| 13 | Reassignment and cancellation outcomes | BLOCKED | PASS (sink: the new driver gets "now assigned", the old driver gets a notice, the cancellation decision is notified) |
| 14 | API errors, rate limits, timeouts | BLOCKED | PASS (fake API: 429 gives RETRY; 5xx gives AMBIGUOUS (UNKNOWN); 400/403 give FAILED; a connect error gives RETRY; a timeout gives UNKNOWN, never FAILED; a button-clear failure never raises or reverses the answer) |

The "fake Bot API" is `httpx.MockTransport` in `tests/test_telegram_test_bot.py`. It runs the real `TelegramBotProvider` code and request shapes, but **it is not Telegram**. Message rendering, client keyboard behaviour, Telegram's retry timing and real rate limits remain unverified until §D.3 is run.

### D.2 Not tested anywhere

- How Telegram clients render the keyboards: needs a real device.
- Clearing an offer's buttons after a *typed* YES or NO. The backend refuses later taps, but the buttons stay visible. Clearing them needs the offer message's Telegram message id, which the relay records as `provider_message_id`, `tg.<id>`. This is a small follow-up if wanted.

### D.3 How the owner runs the real test-bot exercise

1. Create a dedicated test bot with BotFather. Do not reuse any bot that real people use.
2. Each test phone opens the bot and sends `/start`. Get each account's numeric id, for example from the `from.id` of `getUpdates` in your own shell.
3. In your own PowerShell session (never in chat or in a file in the repo), set:
   - `$env:SETUHAUL_TELEGRAM_DELIVERY="test_bot"`
   - `$env:SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED="true"`
   - `$env:SETUHAUL_ENV="local"`
   - `$env:SETUHAUL_TELEGRAM_TEST_CHAT_IDS="<id1>,<id2>"`
   - `$env:SETUHAUL_TELEGRAM_TEST_BOT_TOKEN="<token>"`
   - `$env:SETUHAUL_TEST_BOT_DSN="host=localhost port=55432 user=postgres dbname=setuhaul_tgbot"`
4. Run `python scripts/telegram_test_bot.py --setup`. This creates a fresh database with synthetic data only: two facilities, docks, 4 days of slots, and drivers `DRV-TEST-1..4` with shipments.
5. Run `python scripts/telegram_test_bot.py`, then open `http://127.0.0.1:8781/app-runner/dispatcher`, `/app-runner/planner` and `/app-runner/ops_lead`.
6. Run journeys A–G from §E using the console and the test phones.

The runner:
- refuses a bot that has a webhook set, and a database not named `setuhaul_tgbot*`;
- runs driver turns with the orchestrator's no-model mode (rules extractor and templates), so no model is called.

The runner was smoke-tested locally against a scripted fake Bot API:
- the poll, webhook and reply path worked: an allowlisted account got a reply, a stranger got nothing;
- the setup step and the database-name refusal worked;
- `/app-runner/dispatcher` signed in as a dispatcher showing only Dispatch, with four synthetic shipments.

## E. Business journeys (test sink and fake Bot API; real Telegram BLOCKED)

| Journey | Result | Authoritative evidence |
|---|---|---|
| A. Initial booking | PASS | The offer goes `OFFERED`, then `ACCEPTED` when the driver taps. The appointment goes `PENDING_CONFIRMATION`, with the hold turned into the claim. A dispatcher's attempt to approve gets 403, and the database refuses the role. The planner's CONFIRM makes it `CONFIRMED`. The relay sends the confirmation to the driver. A redelivered callback creates no second appointment. |
| B. Decline / other time | PASS | `DECLINED` or `ALTERNATIVE_REQUESTED`; no appointment and no occupancy. A new offer supersedes the old one, and the old button changes nothing. |
| C. Expiry and slot loss | PASS | An expired offer is answered "expired" and no appointment is created. A slot taken before acceptance gives `SLOT_TAKEN`. A stale planner approval gives 409 `PENDING_EXPIRED` / `CAPACITY_LOST` (existing tests and the earlier harness check). |
| D. Delay and optional location | PASS | The delay is recorded through `record_driver_report`. The location keyboard is sent once. A shared location gives `VERIFIED` with a geohash, and the delay stands. "Not now" gives `UNAVAILABLE`, and the delay stands. A live location is ignored. No proximity or ETA claim is made from location alone (assessment reasons only). |
| E. Confirmed reassignment | PASS | Only `shipments.driver_id` changes, after the new driver's yes. The appointment and the dock claim are unchanged. The old driver is notified. Decline, expiry, overlap (`DRIVER_HAS_OVERLAPPING_TRIP`), concurrent offers (only the latest can be accepted) and exclusion with cancellation are all covered. It also works as `dispatcher`. |
| F. Confirmed cancellation | PASS | The requester cannot decide. Rejection keeps the appointment and the claim. Approval cancels the appointment and releases the occupancy in one transaction. The driver is notified. Cancellation and reassignment block each other. A dispatcher can request but gets 403 on the decision. |
| G. Delivery failures | PASS | 429 gives `RETRY`; a timeout gives `UNKNOWN`, not `FAILED`. The Ops view shows the route `TELEGRAM` and the attempts. The offer stays `OFFERED`, so delivery never touches business state. Acknowledging an escalation is separate from resolving it (0036 tests). |

## F. Security and authorization

| Area | State |
|---|---|
| Unlinked account | Generic "not linked" reply only; no driver, shipment or booking data. In test-bot mode, a non-allowlisted account gets no reply at all and cannot redeem a code. The code stays valid for the real account (tested). |
| Identity | Numeric `from.id` only. A username is never used. One account links one driver at a time. |
| Callback authorization | Re-checked in the database against the offer, the driver and the status. Another driver's offer gives `NOT_FOUND`, and nothing is cleared. |
| Replay / idempotency | Update and callback ids are unique keys. Console actions take idempotency keys. |
| Link-code throttling | 5 failures per account per 15 min; 100 across all accounts per 5 min; attempts audited without codes; codes hashed (0036 tests). |
| Production isolation | `test_bot` is refused outside `local`/`dev`. The pilot stack sets no Telegram delivery, so the default `test_sink` applies there. No webhook is registered anywhere. |
| WhatsApp / Meta | Unchanged: the pilot outbound uses `test_sink` with `SETUHAUL_WHATSAPP_ENABLED=false`. The runner uses `LocalProvider` for non-Telegram routes, so nothing goes to Meta. |
| Booking switch / kill switch | Unchanged. Neither is touched by this cycle. |
| Roles | dispatcher: dispatch actions only. operations: unchanged (dispatch, ops health, STANDARD approvals). planner and ops_lead: unchanged decision rights. |

## G. Tests

Run on local PostgreSQL 18.6. pytest-xdist is not installed, so independent groups ran as separate processes, each module with its own database where it creates one.

All commands were run as `python -m pytest <modules> -q -p no:cacheprovider --basetemp=<scratch>`.

| Group | Modules | Result | Time |
|---|---|---|---|
| R | `test_dispatcher_role` (alone, while it was being written) | 10 passed | 10 s |
| C1 | `test_console_me test_infra test_agent_not_operator test_migrations test_recovery_release_a test_approvals test_api` (with 0037 and the role changes) | **276 passed** | 5:22 |
| C2 | `test_dispatch_offers test_reassign_cancel_telegram_out test_location_telegram_units` (with 0037, in parallel with C1) | **76 passed** | 2:11 |
| T | `test_telegram_test_bot` (alone, while it was being written) | 23 passed | 16 s |
| C3 | `test_aws test_outbound test_inbound_delivery test_location_telegram_units test_telegram_test_bot test_console_me` (final Telegram code) | **162 passed** | 2:54 |
| C4 | `test_dispatch_offers test_reassign_cancel_telegram_out test_dispatcher_role` (final code, in parallel with C3) | **51 passed** | 2:23 |

There were no failures in the final runs. During development, R and T failed first on test mistakes: a wrong column name, a missing request field, the `SendResult.outcome` field name, and one allowlisted account per journey. The code fix found by T was answering the callback before clearing the buttons.

C1 ran on the role changes. The later Telegram changes, to `app.py`, `service.py`, `lambdas.py` and `telegram.py`, are covered by C3 and C4.

**Other checks:**
- The runner smoke test: `--setup` on a local `setuhaul_tgbot_smoke` database, the refusal of a wrong database, and the loop against a scripted fake Bot API.
- The browser harness (local Postgres): a dispatcher sees only Dispatch, and operations, planner and ops_lead keep their tabs.
- The runner console `/app-runner/dispatcher` was checked with the shipment detail and its actions.
- The Data API emulator: `SETUHAUL_TEST_DATA_API=1 pytest tests/test_dispatcher_role.py` gave 10 passed in 11 s. It was also added to the CI emulator list.

**Why no full regression this cycle:**
- The changes touch staff role handling (one migration of function bodies, API permissions) and Telegram transport selection.
- Every module that pins permissions, grants, migrations, CDK resources, the API, approvals, the relay, inbound delivery, AWS handlers, dispatch, reassignment and cancellation was run.
- The previous full run on `d37c938` passed 1680 tests.
- A full run is recommended once, before any merge, together with CI on PostgreSQL 17.9.

## H. AWS readiness

**Completed locally:**
- the CDK synth test includes the dispatcher group;
- migration 0037 is applied on fresh databases (local PostgreSQL), with the Data API emulator list updated in CI;
- the test-bot configuration fails closed;
- the runner was smoke-tested.

**Proposed, not applied (each needs owner review):**
1. **Cognito `dispatcher` group.** Already in the CDK diff of this branch. Deploying it adds an empty group.
2. **Telegram for an AWS environment, only when one is approved.** The pilot must stay on `test_sink`.
   - A secret `setuhaul/<env>/telegram` holding `{"token": ...}`.
   - `telegram_secret.grant_read(outbound_fn)` and `grant_read(api_fn)` (replies).
   - Outbound and API environment: `SETUHAUL_TELEGRAM_DELIVERY`, plus `SETUHAUL_TELEGRAM_BOT_SECRET_ID`.
   - A `TELEGRAM_WEBHOOK_SECRET` key in the app secret, loaded like `META_APP_SECRET`. This needs a 2-line `SECRET_KEYS` change in `lambdas.py`.
   - `setWebhook` with `secret_token`.
   - `test_bot` mode is deliberately refused in `pilot`. A test bot in AWS would need a separate `dev` stack.
3. **Alarm on throttled link attempts.** A metric filter on the audit or log line, plus an alarm to the existing SNS topic.
4. **Optional:** an alarm when no Telegram update arrives for N hours, once live.

**Deliberately not done:**
- no provisioning;
- no secret created or changed;
- no webhook registered;
- no pilot Telegram delivery;
- no workers enabled;
- no deployment;
- no users assigned.

## I. Outstanding risks and next decisions

1. **Blocker: run the real test-bot exercise (§D.3).** It needs the owner's bot token and test phones, about 30–45 minutes. It is the only way to close D.1.
2. **Approve the dispatcher CDK change and assign dispatchers** (§B). Then decide separately when `operations` stops being a dispatcher.
3. Decide on the Telegram AWS items in §H only when a non-local test environment or the pilot is actually wanted.
4. **Small follow-up:** clear buttons after a typed YES/NO (§D.2).
5. Merge, release and activation remain separate decisions, each needing fresh evidence and a full regression and CI run.
