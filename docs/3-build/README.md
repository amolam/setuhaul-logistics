# Phase 3 · Build

Build follows the order in `docs/2-design/08_signoff_and_backlog.md` §4.

## Current status (21 Nov 2026)

| Area | Status |
|---|---|
| Local build (steps 1–5) | **Done.** 800 tests pass on local PostgreSQL 18 (after Phase B; developer-reported full run, 21 Nov 2026) |
| Human checks | Demo walkthroughs F1–F9 done; F5 and F6 confirmed by the owner (21 Nov). Native-speaker Hindi/Hinglish review done and applied (S4-R2-08, 21 Nov) |
| Replies | Fixed templates in English, Hindi and Hinglish; no phrasing model in production (OD-3). B-AI-3 is superseded by OD-3: the Sonnet comparison and the human scoring sheet were **not** run |
| Model provider | **Phase B done (T52):** `model_for()` builds OpenRouter (primary, Haiku 4.5 pinned to the `amazon-bedrock` zero-data-retention route, key from Secrets Manager in staging/pilot, every attempt metered against a daily budget, migration 0023) or Bedrock (optional). Details below |
| AWS (step 6) | **Not started.** B-AWS-0: the AWS account is limited to Sydney (`ap-southeast-2`). The six gates in `DEPLOYMENT_GATES.md` (AWS_QUEUE, AURORA, RECOVERY_WORKER, BOOKING_WORKFLOW, WORKLOAD_IDENTITY, OUTBOUND_DELIVERY) have no AWS evidence yet |
| Booking switch | `SETUHAUL_BOOKING_ENABLED` **off** |
| Deferred by the owner | "Talk to a person" requests (S4-R2-08b); device evidence (FASTag, location pins, SIM, telematics); the driver app is cancelled |

**Configurations.** *Development*: local PostgreSQL, scripted or rules model, no paid calls. *Evaluation*: OpenRouter on
synthetic data, approved and capped runs only (`COSTS.md`). *Staging / pilot*: AWS Sydney, synthetic data, booking switch
off; not built yet.

The sections below are the build history, kept as written; dated statements describe the state on that date.

## Model provider (decision T52, Phase B)

| Setting | Pilot value |
|---|---|
| Entry point | `setuhaul.agent.models.model_for()`, called once at start-up; the provider never changes during a conversation |
| Environments | `dev`, `eval`, `ci`, `staging`, `pilot`; `production` is refused. `SETUHAUL_DATA_CLASS=synthetic` is mandatory |
| Primary provider | OpenRouter, model `anthropic/claude-haiku-4.5` (extraction only; replies are templates, OD-3) |
| Upstream route | Pinned to `amazon-bedrock`: `order` and `only` name it, `allow_fallbacks: false`, no model fallback list. A response from another provider is refused |
| Data retention | `zdr: true` and `data_collection: deny` on every request. On 21 Nov 2026 OpenRouter listed this route as zero-data-retention with safety retention off. ZDR is OpenRouter's routing promise, not proof of the upstream provider's compliance; the data is synthetic in any case |
| Credentials | staging/pilot: AWS Secrets Manager, `SETUHAUL_OPENROUTER_SECRET_ID` (an environment-variable key is refused). dev/eval/ci: `OPENROUTER_API_KEY` allowed. The key is never logged or put in an error |
| Timeouts | connect 3 s, response 10 s |
| Retries | at most one: a connection failure before sending, a 429 with Retry-After ≤ 2 s, a 500/502. Never: read timeout, 504/408, 503 (no route meets the pin/ZDR rules), 401/403/402, 400, malformed answers, provider mismatch, budget refusal |
| Fallback | every failure ends in the rules reader (`TurnResult.fallback_reason` says why); never another provider |
| Usage ledger | migration 0023: one row per attempt (request id, call id, attempt, model, requested and actual provider, tokens, cost, latency, status, error category); no prompt or driver text |
| Daily budget | `SETUHAUL_LLM_DAILY_BUDGET_USD`, UTC day, reserved atomically before each attempt; exhausted → no request, rules reader. Approved: **$0.25/day**, resets 00:00 UTC; OpenRouter key limit $3.00/month, auto top-ups off (owner, 21 Nov 2026) |
| Bedrock | optional: region `ap-southeast-2`, `au.` geographic profile; not metered yet, so dev/eval/ci only. Model access in the Sydney account not verified (the AWS session was not signed in) |


| Step | Status |
|---|---|
| 1. Database and rules | ✅ Done |
| 2. Deterministic backend (API, auth, tool contracts) | ✅ Done, including four rounds of review corrections (migrations 0004–0007). See `REVIEW_LOG.md` |
| 3. Agent integration (Haiku extraction, deterministic replies since OD-3) | ✅ Done locally: golden sets, held-out set and replay run on OpenRouter (synthetic data). The live Bedrock spike (B2–B4) is blocked by B-AWS-0. Booking switch stays **off** |
| 4. Workflow and humans (SQS, Step Functions, notifications) | ✅ Done locally with stand-ins (LocalQueue, LocalApprovalWorkflow, LocalProvider): B-WF-1, B-WF-2, B-WF-2a, B-WF-3 |
| 5. Evaluation and observability | ✅ Done locally: G3 v1–v4, held-out h1, end-to-end replay, cost ledger, traces |
| 6. AWS deployment and demo | **Not started** (B-AWS-0; six gates in `DEPLOYMENT_GATES.md`). Local demo console done |

## Step 4 so far: the live-booking gates (18 Nov 2026)

**Shipment binding (B2-16, B2-17, migration 0010).** `bind_thread_shipment()` now checks, inside the database, that the
conversation belongs to the session's driver. The service no longer decides from a read taken before the lock:
`resolve_thread_shipment()` reads the thread, picks the shipment (named, else bound, else the driver's only live one) and
binds it under the conversation lock, and returns the authoritative shipment. Publishing options re-checks the binding
under the same lock; options for a shipment the conversation has just left are refused (`STALE_SHIPMENT`) and the
orchestrator asks again for the current one.

**Durable inbound delivery (B-WF-1 / B2-14, B2-15, migration 0011, decision T45).** The inbound receipt is the outbox:

| Stage | Who | What makes it safe |
|---|---|---|
| Stored | webhook → `ingest()` | Message and receipt in one transaction; simultaneous deliveries store once |
| Claimed + published | `InboundRelay.publish()` | One delivery wins the claim; sent with deduplication id = WhatsApp id (#generation) and group = driver, **then** marked published |
| Processed | `Orchestrator.handle()` | Database processing lease before the turn, `processed_at` after it; a processed message is refused at any age |
| Recovered | `InboundRelay.recover()` | Stored-but-unpublished after 30 s, and stalled after 6 min with no live lease, are published again |

The webhook does its database work off the event loop, so simultaneous deliveries really run in parallel.

**Review R1.9 (migration 0012, decision T46).** The webhook answers 503 when a valid message was not stored, so the provider retries (S4-01). The lease is claimed for the event's generation, renewed by a heartbeat, and every write a turn makes (agent and feasibility transactions) first calls `assert_turn_fence()` under the conversation lock: a worker that lost its lease, or whose message got a new generation, commits nothing more (S4-02/S4-03). One reply per inbound message and generation (S4-08). With the booking switch on, the service refuses to start without a verified FIFO queue and evidence for four deployment gates (S4-05). Before applying 0011 where real messages exist, follow `RUNBOOK_0011.md` (S4-04).

**Review R2.0 (migration 0013, decision T47).** A worker marks a message processed only while its lease is live; otherwise the turn ends with `TurnLeaseLost` and the message is delivered again. The cutover checker lists every backfilled receipt and shows later replies beside it. Deployment evidence is append-only and must be current for this environment and deployment; for the queue and the database its fingerprint must match the live configuration (`DEPLOYMENT_GATES.md`).

**Review R2.1 (migration 0014, decision T48).** Evidence cannot be dated into the future. Operational evidence lasts 7 days. The deployment id is derived from the deployment manifest (`SETUHAUL_DEPLOYMENT_MANIFEST`), so every gate is bound to a fingerprint. The queue check now covers the dead-letter queue itself.

**B-WF-2 warehouse approvals (migration 0015, decision T49).** The agent only asks. A warehouse person approves inside `decide_booking_request`, with an identity the console API records for that transaction only. `setuhaul/workflow/approvals.py` is the local stand-in for Step Functions; it only waits. Findings: `FINDINGS_LEDGER.md`. Requirement → test: `TRACEABILITY.md`.

## Step 3 so far: the model at the edges, code in the middle

One driver turn is a fixed pipeline (`setuhaul/agent/orchestrator.py`):

```
inbound event → [Haiku] extract → code checks (extraction.py) → code decides the next tool → BookingService tools
              → reply: template (OD-3: no phrasing model in production) → stored as an AGENT message
```

| Piece | File | What it guarantees |
|---|---|---|
| Extraction contract | `agent/extraction.py` | The Phase 1 §6c schema verbatim; unknown keys rejected; local times resolved in code ("ETA 6:30" at 4:24 PM → 6:30 PM); impossible values rejected; ambiguity becomes a question (repair time vs delay, "leave by 1:30" = gate-out or unloading?); safety from the model **or** a word list |
| Rules-only extractor | `agent/rules_extractor.py` | The no-AI baseline for the ablation and the fallback when the model is down. Correct on all 12 seed driver messages |
| Model clients | `agent/models.py`, `agent/prompts.py` | Bedrock Converse with India profiles only (code refuses any other model id or region); forced tool call; prompt and schema versions plus token and latency recorded on every call (T28). A scripted `FakeModel` for tests |
| Replies | `agent/replies.py` | English and Hindi templates; grounding check: every dock, time, date and reference must come from this turn's facts, dock/time pairs must be real, "confirmed" only when CONFIRMED |
| Orchestrator | `agent/orchestrator.py` | Code chooses every tool. The model's "yes" never books: the database attestation decides. Model failure → rules + templates (F5). Draft fails grounding twice → template. **Any REQUIRES_HUMAN becomes a durable escalation before the driver is told a coordinator will help; if that fails, no coordinator is promised (R0.9 B2-18)** |

Proven with a scripted model through the real API and database (`test_orchestrator.py`): delay → options → "2" → database read-back → "haan ji, kar do" → hold → warehouse request in three turns; booking switch off → escalation row before the promise; failed escalation → no promise; model down → rules and templates, options still offered; ungrounded draft → template; a prompt-injected "confirm D1 at 9 AM" books nothing; a model claiming "yes" cannot override the stored "yes but 2 pm"; safety → takeover, then the agent stays silent; two shipments → asks which.

Found and fixed while building step 3: the driver context never showed the current appointment (appointments are facility-scoped and the read had no scope); a status question during a pending request answered "no active booking"; the 50-driver race test could drift across a slot boundary (now uses a fixed ETA).

**Bedrock spike (18 Nov):** B1 done. Both India profiles are ACTIVE in ap-south-1 and route only to ap-south-1 / ap-south-2. Haiku 5.5 and Sonnet 5.5 are offered only as `global.`, so we stay on Haiku 4.5 and Sonnet 5. B4 is blocked: AWS is still verifying the account, and Bedrock refused every call (nothing billed). `scripts/spike/bedrock_spike.py` is ready to re-run. B2 (the IAM deny on `global.`) changes account settings and waits for approval.

**AWS blocker (18 Nov, later):** the account is a new single-region account locked to ap-southeast-2 (Sydney). Sydney has no `in.` profiles, only `au.` and `global.`, and Claude access is not enabled there either: Haiku 4.5, Sonnet 5 and Haiku 5.5 are `NOT_AUTHORIZED`, and a test call returns "Operation not allowed". Haiku 5.5 has no India profile anywhere, so the design stays on Haiku 4.5. Next: enable Claude in Sydney for an eval-only run on synthetic G3 data (a recorded exception; production keeps the India-only guard), and get an AWS account that can use ap-south-1 before step 6.

**Provider switch (decision T38, hardened after the step 3 review):** the deployed package (`setuhaul/`) has one model entry point, `production_model()`: India Bedrock only. It needs an explicit `SETUHAUL_ENV` and refuses any other provider. The AWS/OpenRouter switch lives outside it, in `evals/providers.py` (`model_from_env()`), which deployed code never imports; a test imports every deployed module in a fresh process to prove it. OpenRouter starts only when `SETUHAUL_ENV` is on an allowlist (dev, eval, ci; a missing or unknown value is refused) and `SETUHAUL_DATA_CLASS=synthetic`. It needs a `SyntheticEvalOnly` capability, and both the capability and the client re-check the real process environment. Requests ask for no data collection; that is a preference, not a guarantee, so the synthetic-data rule stays. Every trace records the upstream provider.

**First model results (18 Nov, OpenRouter, synthetic data; Haiku served by Amazon Bedrock, Sonnet by Claude Platform on AWS):**

**Owner decisions (18 Nov): OD-1 finish-based grace (T39), OD-2 three-tier breakdown escalation (T40, migration 0009).** On G3 v3 (60 core + 7 policy scenarios) Haiku 4.5 with prompt p3 gets 71/74 turns right with every gate at 1.00; rules get 58/74. Seed finding: SHP1017's slot has no slack, so with the 15-minute band on a driver-declared ETA anything after 12:05 is at risk; L01, L02 and ADV02 were relabelled in v3.

**End-to-end replay, B-EV-2 (18 Nov):** `evals/replay.py` plays every G3 scenario through the real API, orchestrator, booking service and PostgreSQL (seed moved to tomorrow, scenario clock). With a scripted model (perfect extraction) **64/64** pass, with zero hard-gate violations; it runs in the suite as `tests/test_replay.py`. With live Haiku 4.5 **61/64** pass, with zero hard-gate violations. The first scripted run passed 46/64 with 3 hard-gate violations. The replay found 12 bugs (details: `evals/golden/v4/README.md`), including masking that hid order references from the model, a redelivery with a new id answered twice, and a coordinator promised without an escalation.

**Held-out set, B-EV-1b (18 Nov):** `evals/golden/h1/` has 167 variants of the labelled turns: paraphrases and extra-detail rewrites by Sonnet 5, which never saw the extraction prompt, plus time shifts by code. They were reviewed (2 dropped) and frozen before any model ran on them. Haiku 4.5 p3 scores 158/167 (95 %) with every gate passing (base G3: 73/76); rules score 99/167. The prompt is not overfitted to G3. Findings for a future p4 (which would need a new held-out set h2): an ETA at the start of a conditional sentence is missed (THR011), and "choices after 2 PM" loses the earliest start (BB02).

**B-AI-3, free stages (18 Nov):** a phrasing model touches only one reply type, the option list. The checks on 20 option replies from the replay found two gaps: a Hindi conversation answered in English (the language followed the last message), and Hinglish drivers always answered in English. **Both are fixed in code (T43): Hinglish templates for every reply, and a conversation-level reply language.** The templates now pass grounding, completeness, language, policy and register on 20/20. Proposed then: remove Sonnet from option replies. **Done since (21 Nov): owner decision OD-3.** Production: model extraction, deterministic option replies. Evaluation: the optional phrasing-model ablation in `evals/phrase_ablation.py`. The native-speaker review of the templates was done on 21 Nov (S4-R2-08, in a shared review sheet). B-AI-3 is superseded by OD-3: the Sonnet arm and the human scoring of `evals/b_ai_3/review_sheet.md` were never run. Details: `evals/b_ai_3/README.md`.

**Overnight arrivals, B-FEAS-1 (18 Nov, decision T44):** the feasibility engine judged every slot by the ready day's closing time, so a truck ready late at night got no options and was escalated even when the next morning had free slots. Each slot is now judged by its own day's close and 21:00 rule, and the search covers the ready day and the next one. Same-day results are unchanged: the Phase 1 differential test still passes.

**Model spend (decision T42, 18 Nov).** Routine development makes **no paid model calls**:

| Level | What runs | Cost |
|---|---|---|
| 1 (default) | `pytest` (rules extractor, scripted end-to-end replay, contracts, grounding, safety, scheduling); `run_golden.py --extractor rules`; any model run whose inputs are cached | $0 |
| 2 (approved) | `--extractor model` / `--model live` for the affected scenarios (`--only`), held-out runs at milestones | estimated first; over $0.10 needs `--approved --max-usd X` |

Every paid runner supports `--estimate-only`, `--approved`, `--max-usd` and `--no-cache`. Ledger and price assumptions: `COSTS.md`.
- **G3 extraction:** Haiku 4.5 with prompt p2 got 65/66 turns fully right; validity, ETA precision and recall, and intent accuracy are all 1.00. The rules baseline got 51/66. Decision: keep Haiku for extraction. The untuned p1 got 58/66 and failed validity on a `"null"` string in a boolean field.
- **Cost and caveat:** about $0.0035 per extraction. p2 was tuned on G3, so a held-out perturbed set is still needed (B-EV-1b).
- **Sonnet 5 phrasing:** 3/3 drafts grounded, after removing `temperature`. Sonnet 5 rejects it, and the Bedrock path had the same bug.
- Details: `evals/golden/v1/README.md`.

**Golden dataset G3 (18 Nov): built.** `evals/golden/v1/` has 60 scenarios (13 brief beats, 10 delay ladder, 12 seed threads, 10 audit traps, 8 Hindi / Hinglish, 7 adversarial), each with a frozen clock, expected extraction, outcome, required and forbidden tools, end state, hard-gate and rubric labels. `evals/run_golden.py` grades extraction and records the dataset hash. Rules baseline: 51/66 turns fully right, ETA recall 0.82 and intent accuracy 0.76 (both below the 0.95 gate). That is the bar Haiku must beat.

**Fixed from G3 (18 Nov):** (1) the safety word list no longer fires on cargo names ("medical load"); (2) a delay that still fits the slot keeps it (`appointment_at_risk` false); (3) a bare delay proposes the computed arrival and asks for a YES (decision D1, THR003); (4) facts from earlier turns are kept in the database (migration 0008, `note_turn_facts()`), never in the model. Details and proof: `evals/golden/v1/README.md`.

**Still needed for step 3 (as of 18 Nov; updated 21 Nov):** done since: the end-to-end replay (B-EV-2), the held-out set (B-EV-1b) and the native-speaker review (S4-R2-08); B-AI-3 superseded by OD-3. Still open: the same runs on Bedrock once B-AWS-0 is solved, now optional because OpenRouter is the pilot's primary provider.

## Where the evidence comes from

| Evidence | Where it runs | What it proves | What it does **not** prove |
|---|---|---|---|
| `tests/` (800 tests) and `scripts/design/validate_schema.py` (88 checks) | **Local community PostgreSQL 18.6** on a developer machine | Business rules, constraints, RLS, least privilege, function behaviour, API behaviour, concurrency on one host | Aurora storage and I/O, Aurora parameter groups and extension allow-list, RDS Data API transport and limits, IAM, API Gateway, network |
| Aurora PostgreSQL 18.3+ run through the Data API | **Not run yet** (gate G1, step 6) | The same suites on the production engine | — |

The production target is **Aurora PostgreSQL 18.3+** (decision T33): Aurora PostgreSQL 18 is generally available since June 2026, and the RDS Data API supports 18.3 and later in ap-south-1 and ap-south-2. The earlier plan for a local PostgreSQL 16 run is withdrawn.

## The invariant this phase protects

> The agent is never a privileged database operator. It understands and **requests** actions; the application and the database **decide** whether they are allowed.

| Layer | What enforces it |
|---|---|
| Identity | Who the agent acts for comes from the verified webhook (Meta signature → phone → driver), carried in a session token that travels only on the internal queue. Tool arguments cannot name a driver or thread. |
| Feasibility | Computed by the service under a read-only system role; options are **published by the system role**. The agent role cannot insert options. |
| Confirmation | The database writes the read-back message. The inbound path (its own role, `setuhaul_ingest`) classifies each driver reply and writes an **attestation**. The agent role cannot write or change attestations. `hold_option()` and `submit_booking_request()` verify it: same thread, same option, verdict YES, still the latest selection, **and no later driver message since** (a later NO, an unclear reply, or any message at all withdraws the YES). |
| Provenance | `record_driver_eta()` requires a DRIVER message in the caller's thread, for that thread's shipment and driver. |
| People | A TAKE_OVER escalation or a console takeover sets the human flag, thread status and HUMAN flow state in one transaction and expires the thread's attestations. Every agent tool and every agent database function then refuses. |
| Privileges | One database role per responsibility (below). The agent role can insert only AGENT chat messages, tracking consents and its own flow row, and can call 6 request functions. Every role's tables, columns and functions are pinned by catalogue tests. |

### Database roles

| Role | Responsibility | Writes |
|---|---|---|
| `setuhaul_agent` | Requests from the agent tools | AGENT messages, tracking consents, its flow row; 6 request functions |
| `setuhaul_ingest` | Verified inbound WhatsApp messages | DRIVER messages (restrictive RLS), threads, inbound receipts; `attest_confirmation()` |
| `setuhaul_feasibility` | Feasibility reads | Publishes computed options only |
| `setuhaul_console` | People, through the console | Operator messages, escalations; decision functions |
| `setuhaul_system` | Background jobs: expiry, relay, mirror sync | Job-specific columns and functions; no longer inbound, options or attestations |
| `setuhaul_rpc` | Owns the SECURITY DEFINER functions; no login, no members | Only through those functions, under the caller's RLS scope |

None of them is superuser or BYPASSRLS. A read-only transaction is not treated as a security boundary; a separate role is.

## Step 2 final corrections (external review of 0004 → migration 0005)

| # | Finding | Fix | Tests |
|---|---|---|---|
| P0-1 | An earlier YES stayed usable after a later NO or unclear reply | Only the newest driver message can be attested; attesting a reply expires earlier attestations for that selection; `latest_reply_message_id`; `verify_attestation()` refuses if any later driver message exists. The flow re-applies the latest reply | DB: YES → NO, YES → UNCLEAR, YES → unattested message, NO after hold blocks submit, late retry of an older message never overrides a newer one, superseded attestations expired. API: YES → NO cannot book; YES confirmed then "actually wait" blocks the hold |
| P0-2 | Inbound processing and feasibility ran as the broad system role | New `setuhaul_ingest` and `setuhaul_feasibility` roles; system role lost inbound, option-publishing and attestation rights | Exact table, column and function allowlists for both roles; ingest cannot write AGENT messages or attestations; system cannot attest or store inbound messages |
| P1-1 | A retried webhook skipped an attestation that had failed | `inbound_receipts` per message; a duplicate delivery resumes PENDING / FAILED work; a reconciler retries in the background | Classifier fails once → webhook retry recovers (one message, one attestation, 2 attempts); reconciler recovers without a retry |
| P1-2 | CENTRAL escalations produced no event | Every escalation emits an outbox event; CENTRAL events have `facility_id` NULL and are visible only with CENTRAL scope. Delivery to central operations is the notification relay's job (step 4) | CENTRAL event visible to central scope, invisible to JAI |

**Confirmation freshness policy (intentional, accepted by the reviewer):** any subsequent driver message after confirmation invalidates the previous confirmation, regardless of its apparent relevance. A new explicit YES is required before booking can proceed.

## Concurrency (review R0.7 → migration 0006)

The freshness check only sees committed messages, so a driver message being stored at the same instant as a hold could be missed. Every operation that orders a conversation now takes one **per-conversation lock** (`thread_lock()`) and holds it until commit: storing an inbound driver message, writing the read-back, attesting a reply, verifying an attestation (hold and request), takeover and escalation. Either the message commits first and the old YES is refused, or the booking commits first and the message supersedes the YES for the next step. Lock order is always conversation → dock, so it cannot deadlock with the dock lock. `tests/test_concurrency.py` pauses one transaction mid-way in each case and proves the other waits and then lands in the safe order; it also covers four simultaneous deliveries of one webhook (one message, one queue event), which found and fixed a duplicate-event defect.

## Step 2 review corrections (first round → migration 0004)

Two reviews of step 2 (internal, then external) found the issues below. All were confirmed against the running API first, then fixed in **migration 0004** (forward-only; 0001–0003 untouched) and the service. Each has tests.

| # | Finding | Fix | Tests |
|---|---|---|---|
| P0-1 | A "yes" sent **before the read-back** could confirm a booking | Read-back written by the DB; attestation by the trusted inbound path only; hold and submit verify it | `test_db_requests.py`: yes before read-back, agent message, NO / UNCLEAR, other thread, agent cannot write attestations or selections, changed selection, expired attestation, core functions not callable. `test_api.py`: early yes refused |
| P0-2 | ETA reports were not tied to a real driver message | `record_driver_eta()` checks message, thread, shipment and driver; agent lost direct INSERT on signals and exceptions | Foreign, agent and non-existent message ids refused (DB and API); wrong shipment; out-of-range ETA |
| P0-3 | Agent kept writing after a person took over; TAKE_OVER escalation did not hand over | Atomic takeover (flag + status + flow + expire attestations); every tool refuses; safety reports create a durable TAKE_OVER escalation; hand-back needs a summary and starts the agent clean | Hold, submit, read-back, attestation and escalation after takeover; safety report → escalation row + human flag; hand-back |
| P1-4 | Unassigned escalations defaulted to Jaipur | `facility_id` NULL = CENTRAL queue, visible only with `CENTRAL` in scope | DB and API: JAI planner cannot see it, central ops can |
| P1-5 | ETA recording and escalation were not idempotent | Idempotency keys + one ETA per driver message + one open escalation per thread and reason | Duplicate calls in DB and API |
| P1-6 | Times without a timezone were accepted; no ordering checks | `AwareDatetime`; earliest before latest; block start before end | Naive and reversed values → NEEDS_INFO / 422 |
| P1-7 | Console role / action rules were scattered | One `PERMISSIONS` matrix (below) | 3 roles × 6 actions, plus a test that the matrix in code equals the documented one |
| P1-8 | G1 still referred to PostgreSQL 16 | Target is Aurora PostgreSQL 18.3+ (T33); G1 pass condition revised in `08_signoff_and_backlog.md` | Validator report header regenerated |
| Internal | Webhook returned the driver's session token to the caller | Constant `{"status":"received"}`; token only on the internal queue (SQS in AWS) | Token and thread id never in the response; unknown numbers get the same answer |
| Internal | One malformed message failed the whole WhatsApp batch | Per-message isolation; unreadable body still 200 | Bad message next to a good one |
| Internal | A driver could not talk about their own second shipment | Thread may move to another own shipment while idle; refused while a hold or request is in progress | Switch when idle; 409 mid-booking |
| Internal | An expired option could be selected | Expiry checked at selection (and in the DB) | Expired option → OPTION_EXPIRED |
| Found while fixing | The agent role could insert options, audit rows, outbox events (e.g. a fake BookingConfirmed) and idempotency keys directly | Revoked; written only by functions or the system role | `test_agent_not_operator.py`, `test_db_requests.py` |

### Console role / action matrix

The database enforces **facility scope** (RLS); the application enforces **role**.

| Action | planner | ops_lead | operations |
|---|:-:|:-:|:-:|
| List requests and escalations | ✅ | ✅ | ✅ |
| Confirm / reject a booking request | ✅ | ✅ | — |
| Block a dock | ✅ | ✅ | — |
| Pause / resume a facility | — | ✅ | — |
| Take over a conversation | — | ✅ | ✅ |
| Hand a conversation back | — | ✅ | ✅ |

Central ops users carry `CENTRAL` in their facility scope to see unassigned escalations and threads.

## Layout

| Path | Contents |
|---|---|
| `migrations/0001_core_schema.sql` | The frozen Phase 2 schema, byte-identical (a test checks this) |
| `migrations/0002_request_functions.sql` | Request and decision functions; agent cannot write DRIVER messages |
| `migrations/0003_hold_needs_driver_yes.sql` | Hold requires driver evidence; `thread_flow` |
| `migrations/0004_step2_review_fixes.sql` | Attestations, read-backs, ETA provenance, atomic takeover and hand-back, central queue, narrower grants |
| `migrations/0005_attestation_freshness_and_ingest_role.sql` | Attestation freshness, `setuhaul_ingest` and `setuhaul_feasibility` roles, inbound receipts, central escalation events |
| `migrations/0006_serialize_per_thread.sql` | Per-conversation lock on every ordering operation; atomic enqueue claim |
| `migrations/0007_locked_shipment_switch.sql` | Shipment switching in one locked function; lock on release and hand-back; 64-bit lock key |
| `migrations/0009_breakdown_escalation.sql` | OD-2: BREAKDOWN escalation reason; the safety question is remembered across turns |
| `migrations/0008_turn_facts.sql` | Facts from earlier turns per conversation, written only by `note_turn_facts()` (own thread, locked, fixed keys, 2 h expiry) |
| `migrations/0010_bound_shipment_under_lock.sql` | B2-16 / B2-17: driver check inside `bind_thread_shipment()`; `resolve_thread_shipment()` under the lock; options re-check the binding |
| `migrations/0011_inbound_outbox.sql` | B-WF-1: inbound receipts as an outbox (published, generation) and a processing lease (owner, until, processed). Cutover: `RUNBOOK_0011.md` |
| `migrations/0012_turn_fencing_and_gates.sql` | R1.9: `assert_turn_fence()`, one reply per inbound message, audit principal from the session, `deployment_gates` (replaced in 0013) |
| `migrations/0013_deployment_evidence.sql` | R2.0: append-only `deployment_evidence` (environment, deployment, expiry, artifact hash, configuration fingerprint), writer role `setuhaul_deploy_evidence`, six gates. See `DEPLOYMENT_GATES.md` |
| `migrations/0014_evidence_freshness.sql` | R2.1: insert-time freshness against the database clock (no future `verified_at`), per-gate validity windows (7 days operational, 30 configuration) |
| `migrations/0015_approval_workflow.sql` | B-WF-2: approval states and transitions, trusted staff identity, one deadline per request and claim, invalidation, operational holds, versioned policy rules, `ApprovalDecided` and idempotent consumers. Design: `docs/2-design/B-WF-2_approval_design.md` |
| `migrations/0016_confirmation_policy.sql` | B-WF-2a: per-facility confirmation rule rows, database-computed risk inputs, person-always and kind floors, authorised policy rules only, override-rate view. Design §20 |
| `migrations/0017_outbound_delivery.sql` | B-WF-3: outbound notifications from stored replies and approval outcomes; stable ids, retries, ambiguity and reconciliation; receipts from the signed webhook. Design: `docs/2-design/B-WF-3_outbound_design.md` |
| `migrations/0018_takeover_time.sql` | Found by the flow tests: takeover time (the safety message is delivered), only turn replies are sent (no duplicate read-back), no template for in-turn policy confirmations |
| `migrations/0019_review_r3_fixes.sql` | Complete-build review R3: the deadline is checked after the locks for every outcome; shipment changes and dock compatibility are re-proved; takeover stops retries and claimed sends; every console mutation uses the trusted staff identity |
| `migrations/0020_compatibility_locks.sql` | Review R3.1: decisions lock the shipment and dock before the approval (one lock order with the invalidation triggers); confirmed bookings made incompatible raise BOOKING_AT_RISK; planner may take over and hand back |
| `migrations/0021_booking_compat_serialization.sql` | Review R3.2: shipment and dock changes to one confirmed booking serialised by a per-dock advisory key; the later change re-checks and raises BOOKING_AT_RISK; REPEATABLE READ refused |
| `migrations/0022_handback_context.sql` | Human test H-5: on hand-back the driver is told (in the conversation's language) and the coordinator's note is kept for the agent as background |
| `demo/` | Human end-to-end check: `python demo/server.py`, then http://127.0.0.1:8765/demo. Driver phone + warehouse console + F1–F9 checklist on the local stack (rules extractor, no AI cost; booking switch off). See `demo/README.md` |
| `setuhaul/db/migrate.py` | Forward-only runner: ordered, one transaction per file, SHA-256 recorded, edits refused |
| `setuhaul/domain/` | `fuse_eta`, feasibility engine, booking flow and yes/no classifier |
| `setuhaul/adapters/` | Snapshot loader, PostgreSQL seed, PostgreSQL view for the engine |
| `setuhaul/api/` | Auth (Cognito, session tokens, Meta HMAC), transaction helper, service, FastAPI app, inbound queue and relay (`inbound.py`) |

## Run

```bash
pip install -r requirements-dev.txt
```

```bash
SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres" python -m pytest tests -q
```

## Results (18 Nov 2026, local PostgreSQL 18.6)

**800 passed** in about 18 minutes (the replay gate takes about 3½) (17 Nov, steps 1–2: 256 passed; `test_concurrency.py` was then run 5 more times in a row with no failures). On this Windows machine, the two `tmp_path` tests in `test_migrations.py` need `--basetemp` pointed at a writable folder.

**Production booking switch:** `SETUHAUL_BOOKING_ENABLED` (default **false**). Off, the agent can understand, find options, read back and record the yes, but cannot hold or request a slot (REQUIRES_HUMAN / BOOKING_DISABLED). The API tests turn it on explicitly. The four review findings that gated it (B2-14…B2-17) are closed in code (migrations 0010–0011); it stays off until step 6 (reviewer R1.9: NO-GO before real SQS, Aurora, recovery worker and booking workflow are verified). Turning it on without those now fails at start-up. The Phase 2 validator passes 88/88 on the frozen schema.

| Suite | Tests | What it proves |
|---|---:|---|
| `test_eta.py` | 53 | Phase 1 §3 fusion rules, plus 40 randomized property runs |
| `test_feasibility.py` | 25 | Identical to the Phase 1 prototype for every shipment × 24 scenarios; invariants on every option |
| `test_pg_view.py` | 3 | The database refuses exactly the four known-bad seed bookings; the engine reading PostgreSQL matches the snapshot exactly |
| `test_booking_flow.py` | 57 | Tool ordering gate, invented options refused, 42 yes / no / unclear phrasings |
| `test_migrations.py` | 4 | Baseline byte-identical; re-run no-op; edits refused; failures roll back |
| `test_agent_not_operator.py` | 17 | Exact allowlists for the agent, ingest and feasibility roles; the system role lost inbound duties; no role bypasses RLS; every table RLS-forced or reasoned; definer functions safe |
| `test_db_requests.py` | 44 | Database functions through the real roles, including every P0 / P1 fix from both review rounds |
| `test_concurrency.py` | 14 | Paused-transaction proofs of per-conversation ordering, including shipment switching in both orders; simultaneous duplicate webhooks; the lock is per conversation, not global |
| `test_agent_units.py` | 105 | Extraction checks, rules baseline on the 12 seed messages, grounding, India-only guard, Converse and OpenRouter request shapes (stubbed, nothing sent), the provider switch's refusals, no temperature on Sonnet |
| `test_orchestrator.py` | 33 | Whole conversations through the orchestrator, API and database with a scripted model, including the four G3 fixes and the `thread_facts` write guard |
| `test_phrase_checks.py` | 16 | B-AI-3 stages 1–4: each check catches its problem, the templates pass, the 20 frozen situations stay correct, and the Sonnet arm cannot spend without approval |
| `test_budget.py` | 8 | T42: cap, approval needs a limit, the run stops before the call that would pass it, retries count, failures are not charged, cache hits (including new option ids and dates), unpriced models never called |
| `test_replay.py` | 3 | B-EV-2: G3 v4 end to end with a scripted model; every playable scenario passes, no hard-gate violations, only the 3 known skips |
| `test_golden.py` | 7 | G3: 60 scenarios with the signed-off composition; every id exists in the seed; labels are self-consistent with the code checks; rules baseline is deterministic and records the dataset hash |
| `test_api.py` | 63 | End to end over HTTP, including 50 drivers racing for one slot (1 held, 49 lost with fresh options), 9 bad-token cases, the 18-case role matrix, and every review fix |
| `test_inbound_delivery.py` | 26 | B-WF-1 / B2-14 / B2-15: 4 simultaneous HTTP deliveries → 1 message, 1 event; crash between claim and publish loses nothing; duplicate after 6 min still rejected; crashed turn and dead worker redelivered; SQS adapter ids |

## Still open

- **Step 4:** Step Functions approvals (B-WF-2), risk-based confirmation rules (B-WF-2a), the notification relay (B-WF-3); SLA breach monitoring and escalation ownership. Durable inbound delivery is done locally; SQS itself is exercised in step 6.
- **Step 6:** mapping AWS workload identities (Lambda / AgentCore roles) to these database roles; Aurora verification (G1).
- **Central queue delivery:** events are durable in the outbox; routing them to the central operations channel is part of the notification relay (step 4). Until then the console polls `/console/escalations`, and each escalation carries its acknowledgement SLA (5 min safety, 10 min otherwise).
- **`move_appointment`** waits for the scheduling tool.
- **Planner override with a reason** (for a NOT_FEASIBLE recheck or contradictory data) belongs to the human-in-the-loop work in step 4.
- **Recheck-then-decide is two transactions.** The database re-checks capacity at decision time, so the remaining window is small; noted, not fixed.
- **Role separation inside the console** is enforced by the application, not by separate database roles.
- **Not yet in AWS:** API Gateway (JWT authorizer, IAM), the Data API behind `Db.tx()`, SQS behind `SqsFifoQueue` (the adapter is tested with a stub only). Cognito access-token customisation (the `facility_scope` claim) may need a paid Cognito feature plan; to verify in step 6.
