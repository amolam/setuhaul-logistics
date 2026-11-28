# Synthetic Pilot Full Activation: booking, live Telegram, LLM judge and LangSmith

**Status:** implemented and tested locally on `feature/driver-trace-llm-judge` (on top of PR #17). The commit is local only.

**Not done** (each needs separate owner approval, see §10):
- not pushed, merged or deployed;
- no webhook registered, and no real Telegram message sent;
- no AWS secret created or changed;
- no paid model call made.

The existing approval reference `OWNER-APPROVAL-20261125-SYNTHETIC-PILOT-RELEASE-01` is **not** used by anything here and does **not** cover this release.

## 1. What changed

| Area | Change |
|---|---|
| Feature controls | `setuhaul/ops/features.py` resolves six independent controls per environment. Production always resolves booking, live Telegram and the judge to OFF. `/health` gains a `features` block of booleans and modes only. |
| Telegram | `live` is allowed only with `SETUHAUL_ENV=pilot` and synthetic data. It is refused in production and every other environment. |
| | New outbound kill switch `SETUHAUL_TELEGRAM_OUTBOUND_ENABLED=false`: nothing is sent, and each message FAILS with an escalation (visible, never silent). |
| | New inbound kill switch `SETUHAUL_TELEGRAM_WEBHOOK_ENABLED=false`: the route returns 404. |
| | The webhook secret comes from Secrets Manager (`SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID`). A plain variable is accepted only in local, dev, ci or eval. The secret must be 32–256 characters of `[A-Za-z0-9_-]`. |
| | If Telegram fails, only Telegram stops: an unreadable token or secret leaves the webhook route absent and logs an ALERT. Console, booking and health keep working. |
| Online LLM judge | `setuhaul/agent/online_judge.py` is advisory and runs in the agent worker after the turn is final. No new AWS resource. |
| | It reuses the accepted judge unchanged. The prompt, tool and validation moved to `setuhaul/agent/judge_core.py`, and `evals/langsmith/llm_judge.py` re-exports them, so both judges use one source. |
| LangSmith | On by default for the **pilot** stack (context `langsmith_tracing=off` opts out); other stacks are still opt-in. |
| | New traces: `telegram_inbound` (API), `telegram_outbound` (sender, one per send) and `llm_judge` (agent, linked to the turn). Existing `agent_turn` traces are unchanged. |
| | Every root run carries the environment, `agent_version`/release and `release_commit`. Content stays off (redacted). |
| Migration | `0043_online_judge_advisory.sql` (additive; §6). |
| CDK | §3. |
| Workflow | `activate-booking.yml` passes every control explicitly, with new inputs `telegram_live_approval` and `llm_judge_budget_usd`; LangSmith now defaults to `on`. Its template check pins each control (§3). |

### Files changed

- **New:**
  - `setuhaul/ops/features.py`
  - `setuhaul/agent/judge_core.py`
  - `setuhaul/agent/online_judge.py`
  - `migrations/0043_online_judge_advisory.sql`
  - `tests/test_full_activation.py`
  - this runbook
- **Modified:**
  - `setuhaul/notify/telegram.py`
  - `setuhaul/api/app.py`
  - `setuhaul/api/service.py` (one tracing line)
  - `setuhaul/aws/lambdas.py`
  - `setuhaul/agent/orchestrator.py`
  - `setuhaul/observability/tracing.py`
  - `setuhaul/observability/langsmith.py`
  - `evals/langsmith/llm_judge.py`
  - `infra/setuhaul_stack.py`
  - `infra/app.py`
  - `.github/workflows/activate-booking.yml`
  - `docs/evaluation/LLM_JUDGE.md` (§12)
  - `tests/test_infra.py`
  - `tests/test_api.py`
  - `tests/test_telegram_test_bot.py`

## 2. Configuration matrix

| Control (variable) | Synthetic pilot after authorized activation | Pilot default (no extra approval) | Production / non-synthetic |
|---|---|---|---|
| BOOKING_ENABLED (`SETUHAUL_BOOKING_ENABLED`) | true on api and agent, via `activate-booking` with fresh evidence | false (`ci.yml`) | false (resolves OFF) |
| TELEGRAM_DELIVERY_MODE (`SETUHAUL_TELEGRAM_DELIVERY`) | `live` (api and outbound) | `test_sink` (unset) | `live` refused; code raises |
| TELEGRAM_LIVE_AUTHORIZED | true, only with the context `telegram_live_approval` | unset | refused |
| TELEGRAM_OUTBOUND_ENABLED | true | (unset = true; test sink) | n/a |
| TELEGRAM_WEBHOOK_ENABLED | true, with the secret from Secrets Manager | no route (no secret) | n/a |
| LLM_JUDGE_ENABLED | true **and** a budget of 0 < x ≤ 1 USD/day | true but budget `0`: every turn skipped with the reason | refused |
| LANGSMITH_TRACING_ENABLED (`SETUHAUL_LANGSMITH_TRACING`) | on (agent, api, outbound) | on (traces flow once the key is set) | unchanged policy (opt-in, synthetic only) |
| Telegram local test bot | refused (`test_bot` is local/dev only) | refused | refused |
| WhatsApp / Meta | `SETUHAUL_WHATSAPP_ENABLED=false`, test sink | same | same |

No single flag enables more than one control (`test_no_single_flag_turns_on_another_capability`). The kill switch still overrides booking in the database, whatever the switch says.

## 3. CDK and infrastructure changes (synthesised in tests only)

New context values:
- `telegram_live_approval`: OWNER-APPROVAL-…; pilot only.
- `llm_judge_budget_usd`: 0–1; default 0.
- `llm_judge_sample_rate`: default 0.2.
- `release_commit`: a git SHA.

`langsmith_tracing` now defaults to ON for the pilot.

| Resource / setting | Without the Telegram approval | With `telegram_live_approval` |
|---|---|---|
| Telegram bot token | none | **imported by name** `setuhaul/pilot/telegram/bot`. The owner creates it, and CDK never creates, writes or outputs it. Read by api + outbound only (`secret:setuhaul/pilot/telegram/bot-??????`). |
| Telegram webhook secret | none | **created** `setuhaul/pilot/telegram/webhook`, generated as 48 characters with no punctuation and KMS-encrypted. Read by the **api only**. |
| api variables | unchanged + judge info + LangSmith | + `SETUHAUL_TELEGRAM_DELIVERY=live`, `…LIVE_AUTHORIZED=true`, `…BOT_SECRET_ID` (a name), `…WEBHOOK_SECRET_ID` (an ARN), `…WEBHOOK_ENABLED=true`, `…OUTBOUND_ENABLED=true`, `…LIVE_APPROVAL` (the reference) |
| outbound variables | + LangSmith | + the same, without the webhook variables |
| agent variables | + `SETUHAUL_LLM_JUDGE_ENABLED=true`, `…DAILY_BUDGET_USD`, `…SAMPLE_RATE`, `…RUN_CAP_USD=0.01`, `…TIMEOUT_S=20` | same |
| LangSmith key `setuhaul/pilot/langsmith` | placeholder `{"api_key": ""}`, read by agent + **api + outbound** | same |
| All functions | + `SETUHAUL_RELEASE=release-<artifact hash>`, `SETUHAUL_RELEASE_COMMIT` when given | same |
| Outputs | + `TelegramMode`, `LlmJudge`, `LangSmithTracing` | `TelegramMode = LIVE (<ref>)` |

There is no new Lambda, queue, schedule, role or network path. API Gateway already proxies every path (`{proxy+}`), so `/webhooks/telegram` needs no infrastructure.

**Consequence for `ci.yml`.** A `ci.yml` deploy now also turns LangSmith ON in the pilot, as the brief asks, and sets the judge to budget 0. Booking stays OFF, as before.

## 4. Secrets: exact names, never values

| Secret | Who creates and fills it | Format | Read by |
|---|---|---|---|
| `setuhaul/pilot/telegram/bot` | **Owner**, in Secrets Manager, before the first deploy with `telegram_live_approval` | the bare bot token, or `{"token": "<token>"}` | api, outbound |
| `setuhaul/pilot/telegram/webhook` | CDK generates it on that deploy | 48 alphanumerics (bare string, or `{"secret": "…"}` if replaced) | api |
| `setuhaul/pilot/langsmith` | exists (G.1); the owner sets `{"api_key": "<key>"}` | JSON | agent, api, outbound |
| `setuhaul/pilot/openrouter` | exists | unchanged | agent (extraction and the judge) |

Secret values are never logged, returned by `/health`, written to traces (the redaction rules drop `*_token`, `*_key` and `secret*`; the export guard drops whatever still matches), or put in CDK outputs.

**Fail-closed order matters.** Create the bot-token secret **before** deploying with `telegram_live_approval`. Otherwise the API logs `ALERT Telegram live unavailable` and runs without the webhook. Only Telegram is down, nothing else.

## 5. LLM judge: eligibility and cost controls

| Rule | Value |
|---|---|
| Eligible | An agent turn with a turn_ref, a non-empty reply of 2,000 characters or less, and the driver's message. Synthetic pilot, dev or eval only. |
| Not eligible | Link codes, button taps and location shares (not agent turns), turns without a reply, and production. |
| Dimensions | clarity, tone, language_match (prompt `judge-p2`; the strict validator unchanged). Policy compliance, factual correctness and booking safety stay with the **deterministic** checks (`NEVER_JUDGED`); the judge never scores them. |
| Sampling | `SETUHAUL_LLM_JUDGE_SAMPLE_RATE` (default 0.2), deterministic per turn. Tracing stays at 100% independently (`SETUHAUL_LANGSMITH_SAMPLE_RATE=1.0`). |
| Daily budget | `SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD`, **default 0 = nothing sent**, maximum 1.00. Enforced under a transaction advisory lock with `judge_spent_today()` (0043). |
| Agent protection | The judge may use only `shared cap − SETUHAUL_LLM_JUDGE_AGENT_RESERVE_USD` (default half), enforced by `reserve_model_call` in the same transaction. It cannot starve the agent, and nothing exceeds the shared cap. |
| Per judgement (per run) | `SETUHAUL_LLM_JUDGE_RUN_CAP_USD`, default 0.01, maximum 0.05, checked against the reservation. At list price one judgement costs about USD 0.003 (run 2: 0.098 for 30). |
| Retries and timeout | 1 attempt, no malformed retry, no provider retry; read timeout 20 s (maximum 25). |
| Loops and duplicates | The ledger call id is derived from turn_ref, so a redelivered turn returns `DUPLICATE` and is never paid twice. One row per turn. A judgement never enqueues or triggers anything. |
| Budget exhausted | No call is sent. A row is recorded as `SKIPPED` with the reason `budget:judge_daily_cap`, `budget:shared_daily_cap` or `budget:run_cap`, and logged. |
| Recorded | `agent_turn_judgements`: turn_ref (= the turn's trace id), correlation_id, the judge's own trace id, driver, shipment and facility ids, status, reason, model, provider, prompt and eval versions, three scores, rationale and evidence (a quote of the **assistant's** reply only), tokens, cost, latency, `advisory = true` (CHECK), `calibration_status = HUMAN_CALIBRATION_OUTSTANDING`. The model_usage ledger also has purpose `judge` with the provider cost. |
| Advisory | The judge imports no service, tool, booking, kill-switch or authorization code. Its only SQL is the ledger and `record_turn_judgement` (a test enforces this). Nothing reads the judgements to decide anything. |

Known limitations, carried from `LLM_JUDGE.md` §11:
- false language-mismatch alarms on correct Hinglish (J13);
- harsh clarity scores on terse but actionable replies (J14, J17).

Human calibration is outstanding, so every score is advisory.

## 6. Database migration impact (0043, forward-only, additive)

- New table `agent_turn_judgements`, built like `agent_turn_metrics` (0027):
  - RLS forced, with a facility-scope policy;
  - append-only (UPDATE and DELETE raise `APPEND_ONLY`);
  - `REVOKE ALL FROM PUBLIC`; SELECT for `setuhaul_system`; writes only through the function, in the turn's facility scope.
- New functions, both SECURITY DEFINER, owned by `setuhaul_rpc`, EXECUTE for `setuhaul_agent` only:
  - `judge_spent_today()`;
  - `record_turn_judgement(jsonb)`.
- No existing table, function, grant, role, booking, kill-switch or ledger object changes.
- 0001–0042 are untouched.
- Malformed driver and shipment ids are stored as NULL, as in 0041.

## 7. Targeted test results (local, no paid or network call)

Results on 25 Nov 2026:

| Run | Result |
|---|---|
| `tests/test_full_activation.py` | 49 passed |
| Existing suites together with it (Telegram, driver traceability, AWS handlers, G.1/G.2, Recovery Release A, migrations, agent-not-operator, LLM judge, infra, api) | 762 passed, 2 failed |
| After fixing those 2 (0043 now forces RLS and is append-only; the agent allowlist names the two new functions, with reasons): `test_full_activation.py` + `test_agent_not_operator.py` re-run | 66 passed |
| Affected suites re-run (migrations, driver traceability) | passed |

The 2 failures were the schema-invariant tests in `test_agent_not_operator.py`, which correctly flagged the new table and functions. `tests/test_full_activation.py` has 49 tests and passes. The relevant existing suites were run alongside it:
- Telegram: dispatch offers, location, outbound and the test bot;
- driver traceability;
- AWS handlers;
- G.1 and G.2 LangSmith;
- Recovery Release A;
- migrations;
- agent-not-operator;
- the LLM judge suites;
- infra (CDK);
- api.

## 8. LangSmith trace verification

**Local evidence** (tests, no network; the `Capture` exporter records the exact payload that would be posted):

| Trace | What the test checks |
|---|---|
| `telegram_outbound` | carries `chat_tag`, `notification_ref`, the outcome, environment `pilot` and `release_commit`; **not** the chat id, the text or the token (`test_outbound_sends_are_traced…`) |
| `llm_judge` | the root id equals the stored `trace_id`; `parent_trace_id` = the agent turn; `quality:*` and `advisory=true` in metadata; `quality:*` feedback attached to the turn's run; the driver's words are absent (`test_a_scored_turn_is_persisted…`) |
| outage | exporter down: the send still succeeds, and `export_errors` is counted (`test_a_tracing_outage_never_breaks_a_send`) |

`agent_turn` keeps the driver, shipment and facility ids and the parent/child spans (existing G.1 and driver-traceability tests).

**Pilot evidence (after deploy, owner-approved):** in the `setuhaul-synthetic-pilot` project, filter by:
- `metadata.component = telegram_inbound` / `telegram_outbound` / `llm_judge`;
- `metadata.release_commit = <sha>`;
- an `agent_turn` trace's `turn_ref`; its `llm_judge` trace has `parent_trace_id` equal to it.

Then confirm:
- no token, chat id, phone or message text in any trace;
- the project's retention and access settings in LangSmith. This is an owner decision.

## 9. Activation sequence

Each step after step 3 needs the approval named in §10.

1. **CI.** PR CI is green on the exact commit. Confirm the release commit.
2. **Approvals.** Confirm the owner's booking reference and, separately, the Telegram reference and the judge budget.
3. **Readiness.** Collect a fresh five-gate report (under 24 h old), and check readiness against the exact release commit (`python -m setuhaul.ops.readiness`).
4. **Bot token.** The owner creates `setuhaul/pilot/telegram/bot` with the token (never pasted into chat or Git).
5. **Merge.** Merge PR #17 (and this commit) into main, then main into the release branch, and commit the gate report.
6. **Provisional activation.** Run `activate-booking` with:
   - `mode=provisional`;
   - `approval_ref=<booking ref>`;
   - `langsmith=on`;
   - `telegram_live_approval=<telegram ref>` (or empty to keep the sink);
   - `llm_judge_budget_usd=<approved>` (or 0).

   This deploys and applies 0043 through the pipeline's migration step.
7. **Smoke.** Run `smoke_option_d.py`, then the renewal report, then `activate-booking mode=renewal` with the **same** Telegram, judge and LangSmith inputs. Verify `/health`: `booking_enabled=true`, `booking_state=authorized`, the kill switch disengaged.
8. **Telegram secrets.** `/health.features.telegram_webhook_enabled=true` and `telegram_delivery_mode=live`, and no ALERT in the API log.
9. **Register the webhook (owner).** Read the generated secret and call `setWebhook` with `url=<ApiUrl>webhooks/telegram`, `secret_token=<secret>`, `allowed_updates=["message","callback_query"]`, `drop_pending_updates=true`. Verify with `getWebhookInfo`: no `last_error_message`.
10. **Live delivery** is in effect with step 6's Telegram reference. To hold it until the end-to-end test, set `SETUHAUL_TELEGRAM_OUTBOUND_ENABLED=false` first (§11).
11. **Judge.** It runs within the approved daily budget. Check `agent_turn_judgements` and `model_usage WHERE purpose='judge'`.
12. **LangSmith.** Verify traces as in §8.
13. **End-to-end** test with authorized pilot drivers (§12).
14. **Kill switches and rollback.** Exercise each one (§11), then `pilot_mode sleep` if the pilot is idle.

## 10. Remaining blockers and required owner approvals

| # | Needed | Why |
|---|---|---|
| 1 | Approval to push this commit to PR #17, or to a separate PR | The commit is local only |
| 2 | Approval to merge PR #17 and deploy | Section 10 of the brief |
| 3 | **A new owner approval reference for live Telegram** (`telegram_live_approval`) | `OWNER-APPROVAL-20261125-SYNTHETIC-PILOT-RELEASE-01` does not cover live Telegram. The workflow refuses to reuse the booking reference. |
| 4 | **A daily budget for online judging** (USD 0–1) and a sample rate | Without them the judge is configured but sends nothing |
| 5 | The owner creates `setuhaul/pilot/telegram/bot` and fills the LangSmith key | Secrets are owner-only |
| 6 | **LangSmith retention and access** for `setuhaul-synthetic-pilot` | The brief requires confirmation; content stays redacted |
| 7 | Webhook registration and the first real Telegram messages | Section 10 of the brief |
| 8 | The list of authorized pilot drivers (Telegram link codes issued by staff) | End-to-end test |
| 9 | The last provisional issuance: the deploy in step 6 consumes it | 2 per rolling 24 h |
| 10 | Human calibration of the judge | Its scores stay advisory |

## 11. Rollback (each is independent; none changes authorization records or production)

| Goal | Action | Effect |
|---|---|---|
| Booking OFF, Telegram unchanged | `python -m setuhaul.ops.kill_switch engage --reason "…"` (or the console) | The database refuses new commitments. Telegram, the judge and tracing are unaffected. |
| Telegram outbound OFF, booking unchanged | On `setuhaul-pilot-outbound` and `setuhaul-pilot-api`: `SETUHAUL_TELEGRAM_OUTBOUND_ENABLED=false` | Nothing is sent. Messages FAIL with an escalation, so dispatchers see them. Inbound continues. |
| Telegram inbound OFF | `deleteWebhook` (owner), and/or `SETUHAUL_TELEGRAM_WEBHOOK_ENABLED=false` on the api | The route returns 404. Outbound and booking are unaffected. |
| Judge OFF, agent untouched | On the agent: `SETUHAUL_LLM_JUDGE_ENABLED=false` (or budget 0) | Every turn is `DISABLED`. Agent turns are unchanged. |
| Tracing OFF, workflows untouched | On agent, api and outbound: `SETUHAUL_LANGSMITH_TRACING=off`. Durable: the next deploy with `langsmith=off`. | Tracing is a no-op. Nothing else depends on it. |
| Everything back to the sink | The next `activate-booking` (or `ci.yml`) without `telegram_live_approval` | The Telegram variables and the webhook secret's use disappear. The bot secret is untouched. |

A Lambda configuration change is a new function version **without a code change**. The booking start-up gates compare code hashes, not variables, so booking evidence is unaffected. Record each change in the run sheet.

## 12. Telegram end-to-end test plan (authorized pilot drivers only)

**Preconditions:**
- §9 steps 1–9 are done;
- two or more drivers have link codes issued by staff (console → driver → Telegram link code);
- each driver uses their own Telegram account and a private chat with the bot.

| # | Step | Expected |
|---|---|---|
| 1 | Driver A sends `/start <code>` | "You are linked…". A `telegram_inbound` trace with A's driver id. |
| 2 | Driver B does the same with B's code. Then a third account sends `/start <A's used code>`. | B is linked. The third account gets "That code did not work…", and nothing is linked. |
| 3 | An unlinked account sends text | "This chat is not linked…" only. No data, no agent turn. |
| 4 | A dispatcher sends A an offer | Only A receives buttons. A `telegram_outbound` trace with `notification_ref`; the notification is SENT. |
| 5 | A taps Accept; B replays A's callback (forwarded) | A's slot is held and the request filed. B gets "This button is not valid for your account". |
| 6 | Telegram redelivers the same update (simulate with a repeated `update_id`) | One stored message, one booking. |
| 7 | A types a free-text question | An agent turn reply. If sampled and the budget is approved, an `llm_judge` row and trace (advisory). |
| 8 | Set the outbound kill switch | The next offer is FAILED with an escalation. No message reaches A. Inbound still works. Then restore. |
| 9 | Disable the webhook | Telegram reports 404 in `getWebhookInfo`. Then restore. |
| 10 | Engage the booking kill switch | Accept is refused safely. Telegram keeps working. Then disengage. |
| 11 | Wrong `X-Telegram-Bot-Api-Secret-Token` | 401. Nothing stored. |

## 13. Owner-approved settings (26 Nov 2026) and pre-deployment gate status

### Settings and where each one is applied

| Setting | Value | Applied by |
|---|---|---|
| Judge daily budget | USD 0.50 | `activate-booking` input `llm_judge_budget_usd=0.5`. CDK sets `SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD=0.5` on the agent. The code refuses anything above 1.00. |
| Judge sample rate | 0.20 | the CDK default (`llm_judge_sample_rate=0.2`) |
| Per-judgement cap | USD 0.01 | fixed in CDK; the code maximum is 0.05 |
| Agent reserve, single attempt, 20 s timeout, duplicate prevention, provider and cost recorded | unchanged | `online_judge.py` (§5) |
| LangSmith tracing | ON by default, project `setuhaul-synthetic-pilot`, content off | input `langsmith=on` (the default) |
| LangSmith retention | 30 days | **Owner, in LangSmith; plan-dependent** (checked against LangSmith's public docs on 26 Nov 2026; this session has no LangSmith key, so the organization's actual plan and settings were NOT read). Retention has two tiers: base (fixed 14 days) and extended (Cloud default 180 days). On **Enterprise**, the extended duration can be set to exactly **30 days**: Settings → Usage configuration → the workspace's Data retention policy, or `PUT /api/v1/ttl-settings` `{"resource_type": "run", "ttl_days": 30}`. That is native, and needs no scheduled deletion. On a plan without that choice: use the base tier (14 days, shorter than approved), or extended plus a scheduled `POST /api/v1/runs/delete` of traces older than 30 days. A retention change applies to **new traces only**; datasets never expire. |
| LangSmith access | restricted to authorized pilot operators | **Owner, in LangSmith; plan-dependent.** A dedicated workspace for `setuhaul-synthetic-pilot` with only the pilot operators as members. Per-user workspace roles (Viewer, Editor and so on) and custom roles are Enterprise only; on Plus every workspace member is Workspace Admin, so membership is the only control. The key in `setuhaul/pilot/langsmith` should be a service key for that workspace. |
| Booking | ON after authorized activation | `approval_ref=OWNER-APPROVAL-20261125-SYNTHETIC-PILOT-RELEASE-01` (booking only) |
| Telegram inbound and outbound | LIVE | `telegram_live_approval=OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-TELEGRAM-LIVE-01` + `telegram_authorized_drivers=<owner's list>` |

The provisional and renewal runs of one release must use **identical** inputs. The judge budget is part of the deployed template, so it is set at step 7, the provisional activation. Before Telegram is live, the only agent turns in the pilot come from the smoke test, so the judge spends little or nothing before step 12.

To hold the judge until step 12 instead, deploy with `llm_judge_budget_usd=0` and later set `SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD=0.5` on `setuhaul-pilot-agent`. That is a configuration change only, so booking evidence is unaffected.

### Authorized pilot drivers (item A): BLOCKED, waiting for the owner's list

- Live Telegram now **requires** an explicit list of SetuHaul driver ids (`SETUHAUL_TELEGRAM_AUTHORIZED_DRIVERS`), between 2 and 50 `DRV-…` ids. It never contains a phone, name or chat id.
- The list is passed as the `telegram_authorized_drivers` input and validated by the workflow, by CDK and by the code.
- With the list in place:
  - staff cannot issue a link code for an unlisted driver (403 `DRIVER_NOT_AUTHORIZED_FOR_TELEGRAM`);
  - an account linked to an unlisted driver is treated as unlinked;
  - the relay refuses to send to an unlisted driver's chat (FAILED with `TELEGRAM_DRIVER_NOT_AUTHORIZED` and an escalation).
- Identity is still proved only by a **staff-issued, single-use, expiring link code** redeemed from the driver's own Telegram account, matched on Telegram's numeric user id.
- Link codes, tokens and chat ids are never committed.

**Needed from the owner:**
- at least two real pilot driver ids;
- for each, the person who will receive the link code out of band and confirm their Telegram account.

No driver identity has been invented.

### Human calibration (item B): PREPARED, NOT PERFORMED

`evals/judge/pilot_calibration.py`:
- **`export`** reads the pilot database read-only. It writes a scoring sheet of sampled SCORED pilot turns, using the masked driver request and the assistant reply, plus J13 and J14 for the false-positive review. The judge's scores go in the last columns, and the instructions tell reviewers to hide them while scoring.
- **`report`** applies the existing 80%-within-±1 rule and reports MAE, bias, disagreements of 2 or more points, and the J13/J14 outcome.
- It **refuses** any row without `reviewer_kind=human` and a reviewer id. AI reference scores and judge self-evaluation never count.

The sheet needs judged pilot turns, so it can only be exported after deployment and live use. Until the owner accepts a CALIBRATED report, every score stays advisory.

### Pre-deployment gates, checked read-only on 26 Nov 2026 (no issuance consumed)

| # | Gate | Status |
|---|---|---|
| 1 | Green CI on the exact commit | `f47e24d`: test and synth passed. **The allowlist and calibration commit needs its own green CI run** before it can be the release commit. |
| 2 | Separate approval references | The Telegram reference passes the workflow and CDK format checks and differs from the booking reference. Its registration and validity are confirmed only when `activate-booking` accepts it at dispatch. |
| 3 | A fresh five-gate report against the exact release commit | **Not yet.** The last report (collected 2026-11-25T17:27:50Z) was checked against a simulated release that predates `f47e24d`. Re-collect it or re-check it once the release commit exists. |
| 4 | Owner-created Telegram bot secret | **MISSING**: `setuhaul/pilot/telegram/bot` does not exist (metadata check only; no value read). |
| 5 | LangSmith secret and access | The secret `setuhaul/pilot/langsmith` does not exist yet. **That is expected**: the first deploy with LangSmith on creates it as `{"api_key": ""}`, and the owner fills it afterwards (until then tracing logs `EMPTY_SECRET` and stays off). Retention and access are owner decisions (above). |
| A | Authorized driver list | **MISSING** (owner) |

Steps 6 onward (merge, release branch, provisional activation) wait until every row above passes.
