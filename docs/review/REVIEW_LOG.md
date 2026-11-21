# Build review log

> **Paths in this log are historical (21 Nov 2026 restructure).** `phase3_build/setuhaul/`, `tests/`, `migrations/`, `evals/` and `demo/` are now at the repository root; `phase3_build/*.md` review files are in `docs/review/`, the gate, runbook and cost files in `docs/operations/`, the build README in `docs/3-build/`; `phase1_frame/` and `phase2_design/` documents are in `docs/1-frame/` and `docs/2-design/`; their scripts are in `scripts/`.


External review entries for Phase 3, newest last. Each response names the migration and tests that close it.

## R0.7 – Build Step 2, Round 2 (reviewer entry, verbatim points)

- Migration 0005 accepted as a forward-only extension.
- Four previous findings substantially addressed.
- Confirmation freshness policy accepted as deliberately conservative.
- Separate ingest and feasibility database roles accepted.
- Inbound receipt/reconciliation design accepted for tested scenarios.
- Central escalation event persistence accepted.
- New P0 concurrency serialization finding recorded.
- 245 Build tests and 88 Phase 2 checks reported passing on local PostgreSQL 18.6.
- Aurora validation remains open.
- Step 3 approved conditionally for isolated model integration.

## R0.7 response – migration 0006

| Item | Status | Where |
|---|---|---|
| **P0: freshness under concurrency.** A driver message being stored while a hold or request checked freshness was invisible to the check | **Fixed.** One per-conversation transaction lock (`thread_lock()`) is taken by every operation that orders a conversation: storing an inbound DRIVER message, writing the read-back, attesting a reply, verifying an attestation (hold and request), takeover and escalation. Held until commit. Either the message commits first and the old YES is refused, or the booking commits first and the message then supersedes the YES for the next step | `migrations/0006_serialize_per_thread.sql`; `tests/test_concurrency.py` (each test pauses one transaction mid-way): message in flight → hold waits then refuses; hold in flight → message waits then supersedes the request; the same through the real ingest code; takeover in flight → hold waits then refuses; the lock is per conversation, not global |
| Conservative freshness policy | **Documented** as an intentional safety policy (below) | `README.md` |
| P1 (step 4): enqueue must be an atomic claim | **Done now.** `enqueued_at` plus a conditional update; a FAILED attestation retry is also an atomic claim. The new 4-way simultaneous delivery test found a real defect (each duplicate queued an event) and it is fixed | `test_simultaneous_duplicate_webhooks_store_and_enqueue_once` |
| P1 (step 4): persistent SQS deduplication keyed on the inbound message id | Open, step 4 | Backlog B-WF-1 |
| P1 (step 4): SLA breach monitoring and escalation ownership | Open, step 4 | Backlog B-WF-3 |
| AWS workload identity → database role mapping | Open, step 6 | Backlog B-AWS-1 |
| Aurora verification (G1) | Open, step 6 | Backlog B-AWS-2 |

**Confirmation freshness policy (intentional):** any subsequent driver message after confirmation invalidates the previous confirmation, regardless of its apparent relevance. A new explicit YES is required before booking can proceed.

**Evidence:** 251 Build tests and 88 Phase 2 checks pass on local PostgreSQL 18.6. The concurrency file was run 5 more times in a row with no failures. Aurora is not yet verified.


## R0.8 – Build Step 2, Round 3 (reviewer findings)

| ID | Finding | Reviewer status |
|---|---|---|
| B2-01 … B2-12 | Freshness, provenance, takeover, central routing, idempotency, timezones, roles, recovery, central events, confirmation concurrency | Fixed |
| B2-13 | Shipment switching not serialized | New P1 |
| B2-14 | Durable inbound event delivery (claim committed before an in-memory append) | Step 4 |
| B2-15 | Concurrent HTTP webhook test through the FastAPI route | Recommended |
| P2 | 32-bit advisory-lock key could collide | Hardening |

Decision: **GO for step 3** (model integration). Keep the production booking switch disabled until B2-13 is fixed and inbound delivery is durable.

## R0.8 response – migration 0007

| Item | Status | Where |
|---|---|---|
| **B2-13** shipment switch | **Fixed.** `bind_thread_shipment()` takes the conversation lock, re-reads the thread and refuses from database facts (an active hold or a pending warehouse request on the current shipment). It also refuses if the previous shipment's facility is outside the caller's scope (so a hold there cannot be missed). Shipment and flow change in one transaction; old options and confirmations are voided. The agent lost direct UPDATE on `chat_threads.shipment_id` | `migrations/0007_locked_shipment_switch.sql`; `tests/test_concurrency.py`: switch in flight → hold waits then refuses, no stranded hold; hold in flight → switch waits then refuses, conversation stays with the held shipment; rules test (other driver's shipment, incomplete scope, no direct update) |
| Lock convention review | **Done.** `release_hold()` and `hand_back_thread()` now take the conversation lock too. Every operation that changes a conversation's booking state is locked | Migration 0007 |
| **P2** 64-bit lock key | **Done.** `thread_lock()` uses `hashtextextended` | Catalogue test |
| **Production booking switch** | **Added, OFF by default.** `SETUHAUL_BOOKING_ENABLED` (default false): with it off, the agent can understand, find options, read back and record the yes, but `hold_option` and `submit_booking_request` return REQUIRES_HUMAN / BOOKING_DISABLED | `test_booking_switch_is_off_by_default` |
| **B2-14** durable inbound delivery | Open, **step 4**: transactional outbox for inbound events, published to SQS FIFO with the WhatsApp message id as persistent deduplication identity. Turning on the booking switch is gated on this | Backlog B-WF-1 |
| **B2-15** concurrent HTTP webhook test | Open, **step 4**, together with the durable queue | Backlog B-WF-1 |

**Evidence:** 256 Build tests and 88 Phase 2 checks pass on local PostgreSQL 18.6. The concurrency file plus the API switching test were run 5 more times in a row with no failures. Aurora is not yet verified.

## R0.9 – Build Step 2, Round 4 (reviewer findings, recorded 18 Nov 2026; fixes deferred by the owner)

| ID | Finding | Status | Priority | Gate |
|---|---|---|---|---|
| B2-13 | Shipment-switch serialization | Substantially fixed; the stale-read path is B2-17 | P1 | Live booking |
| B2-16 | `bind_thread_shipment()` does not itself check that the conversation belongs to the transaction's `app.driver_id` (the service checks, the database function does not); target shipment must belong to that authenticated driver | Open | P1 | Live booking |
| B2-17 | `_shipment()` decides whether to switch from a pre-lock read; a concurrent switch can leave a request generating options for the old shipment. Resolve and bind under the lock, return the authoritative shipment, re-check binding when publishing options; test through `BookingService` | Open | P1 | Live booking |
| B2-14 | Durable inbound event delivery | Step 4 (B-WF-1) | P1 | Live booking |
| B2-15 | Concurrent HTTP webhook test | Step 4 (B-WF-1) | P1 | Live booking |
| B2-18 | With the booking switch off, `hold_option` / `submit_booking_request` return REQUIRES_HUMAN but create no escalation. The step 3 orchestrator must turn REQUIRES_HUMAN into a durable `escalate_to_human`, shown by an integration test, so the driver is never promised a coordinator who was not notified | Open, **step 3** | P1 | Step 3 sign-off |
| P2 | 64-bit advisory lock | Fixed | — | — |

Decision: **GO** for step 3 model integration with the booking switch off. **NO-GO** for live booking until B2-16, B2-17, B2-14 and B2-15 are closed.

## R1.0 – Build Step 3, updated review (reviewer verdict, summarised)

Model evaluation **PASS**; provider switch **PASS with conditions**; Step 3 **Conditional GO**. Haiku 4.5 retained for extraction (results checked against the raw files); p2 accepted subject to held-out validation; OpenRouter accepted as a synthetic-data evaluation provider; the temperature fix accepted; India-only Bedrock production architecture and migration 0008 accepted. Recommendation: do not tune the prompt further against the same 60 scenarios.

### Response

| ID | Finding | Status | Where |
|---|---|---|---|
| S3-01 (P1) | A selected option could bypass a constraint stated in the same message | **Fixed.** A message with a new constraint never selects or confirms; the agent asks if the constraint is ambiguous, otherwise re-offers options under it. Covers "the second one, but I must leave by 1:30" and "yes, but I must be out of the gate by…" after a read-back | `orchestrator.py` (`constraint-before-select`); `test_orchestrator.py::test_s3_01_*` (2); G3 v2 BB10 |
| P1 latest-finish | "Unloading must finish by" and "should start by" were both accepted for either kind | **Fixed.** `kind_stated()` needs start words for UNLOAD_START and finish words for UNLOAD_END (English, Hinglish, Hindi), and asks when both or neither appear | `extraction.py`; `test_agent_units.py::test_latest_finish_kind_needs_the_drivers_words` (11 cases) |
| P1 residency | OpenRouter was blocked only when `SETUHAUL_ENV` equalled `production` | **Fixed (fail closed).** OpenRouter moved out of the deployed package into `evals/providers.py`. `production_model()` is the only deployed entry point (India Bedrock, explicit environment). OpenRouter needs an allowlisted environment, `SETUHAUL_DATA_CLASS=synthetic` and a capability, re-checked against the process environment | `evals/providers.py`, `models.py`; 15 tests including a fresh-process import check of every deployed module |
| Provider log | Sonnet calls were served by "Claude Platform on AWS", not Bedrock | **Corrected** in README and G3 README | — |
| S3-02 (P1) | Grace semantics of `appointment_at_risk` | **Behaviour unchanged, as recommended; pinned by boundary tests.** With occupancy shorter than the slot, the engine's extra condition makes ETA 12:21–12:30 "at risk" where Phase 1's formula would keep the slot. Owner decision OD-1 | `test_feasibility.py::test_s3_02_*` (4) |
| H04 policy | Breakdown not fixed = safety takeover | **Unchanged, as recommended.** Owner decision OD-2 | Backlog |
| AT06 | "A slot at 2 PM" | **Label kept** (requested start time, as the reviewer prefers). Prompt not tuned for it; the held-out set will show whether it is a pattern | G3 v2 README |
| Metrics | Silent fallbacks can look healthy | **Added to B-EV-3**: Sonnet success, template-fallback, grounding-rejection and retry rates | Backlog |

G3 v2 (`v2+sha256:d9fc6777cb68343f`) changes only BB10; v1 (`66a96947a6bf1ad7`, the reviewed version with the recorded model runs) is frozen by a test. 371 tests pass (local PostgreSQL 18.6).

Still open, as the reviewer lists: B-AWS-0, B-EV-1b (held-out set), B-EV-2 (end-to-end replay), B-AI-3 (templates vs Sonnet), B2-16, B2-17, B-WF-1, G1, and owner decisions OD-1 and OD-2.

## R1.1 – Owner decisions OD-1 and OD-2 (18 Nov 2026)

The reviewer proposed final policies; the owner adopted them.

| Decision | Implemented as | Tests |
|---|---|---|
| **OD-1** Grace is finish-based: at risk when `ready + occupancy > slot end + grace`; no-show = no gate check-in after slot start + grace (strict) | The engine's independent start-plus-grace check is removed (T39). `is_no_show()` is added for the step 4 sweeper | `test_feasibility.py::test_od1_*`: finish 1 min before the deadline keeps the slot; exactly at it keeps; 1 min after is at risk; late arrival finishing within grace keeps; check-in exactly at the no-show deadline is not a no-show; no check-in after it is |
| **OD-2** Three tiers, with operational escalation kept separate from takeover | Migration 0009 adds the BREAKDOWN escalation reason and lets the conversation remember the safety question. Breakdowns escalate in DECIDE mode; danger words ("dangerous", "unsafe", "not safe", "khatarnak", "middle of the highway", …) and a NO to the safety question become SAFETY TAKE_OVER (T40). Phase 1's rule is marked superseded | `test_agent_units.py::test_od2_breakdown_tiers` (10); `test_orchestrator.py::test_od2_*` (8): safe place (puncture, engine failure), danger, injury, unknown → NO → takeover, unknown → YES, repaired, takeover blocks automated replies, hand-back lets the agent resume |

Also found while doing this:
- **SHP1017 has no slack.** With the 15-minute band on a driver-declared ETA, anything after 12:05 is at risk, so v2's L01, L02 and ADV02 "keep the slot" labels were wrong under the design. They are relabelled in G3 v3, which also adds a 7-scenario policy group.
- **A bare "Gate-out." answer was re-asked,** because the model leaves the kind empty when no time is given. It is now read from the driver's words in code.

G3 v3 on Haiku 4.5 (prompt p3, via OpenRouter): 71/74 turns, every gate 1.00; rules 58/74.

## R1.2 – B-EV-2 end-to-end replay (18 Nov 2026)

`evals/replay.py` replays G3 through the real API, orchestrator, booking service and PostgreSQL. That separates model errors (live run) from orchestration errors (scripted run).

| Run | Scenarios | Turn outcomes | Hard-gate violations |
|---|---|---|---|
| First scripted run (v3) | 46/64 | 78/92 | 3 |
| Scripted after fixes (v4) | **64/64** | **92/92** | 0 |
| Live Haiku 4.5, p3 (v4) | **61/64** | 89/92 | 0 (BB09 fixed afterwards) |

Bugs found and fixed (details and tests: `evals/golden/v4/README.md`):
1. Ingest masked the date inside order references, so the model never saw `ORD-260804-020` (T41b).
2. A redelivery with a new provider id got a second reply (F1; T41a). Short replies are exempt; an existing test caught a first version that would have dropped a genuine second "yes".
3. "No slot" promised a coordinator without an escalation (B2-18).
4. A cancelled shipment got no "cancelled, contact dispatch" answer.
5. A status question with no valid booking was a dead end; it now offers slots.
6. Another driver's order reference was not refused.
7. A status question naming one of two orders was asked "which order?" again.
8. A confirmed ETA after a fixed breakdown escalated anyway.
9. An answer to the agent's own question lost the earlier request.
10. "haan safe hai" was not accepted as safe.
11. A half range discarded the whole extraction.
12. An earliest start plus a delay still triggered an ETA question.

Label corrections (G3 v4): THR002 and AT09 (no slot exists in the data), THR008 and AT01 (their seed bookings are refused at load), H07, P02, P03, and missing extraction labels on THR009 and AT05.

Open model-side findings (not tuned on this set; for B-EV-1b): BB12 (a claimed approval read as a booking request), AT02 ("since 9:05" read as an earliest start).

## R1.3 – B-EV-1b held-out set (18 Nov 2026)

`evals/golden/h1/`: 167 one-turn variants of G3 v4's 76 labelled turns (74 paraphrases, 76 extra-detail, 17 time-shift).
- **Built for independence.** The paraphrase and detail variants were written by Sonnet 5, which never saw the extraction prompt, schema or labels. Labels are copied from the base turn, or shifted, by code. Automatic checks found no lost or invented numbers, times, references or script changes. A review dropped 2 drifted paraphrases. The set was frozen at `2e72cb40af0b106d` before any model ran.
- **Results (Haiku 4.5, p3).** Held-out 158/167 (95 %), with every gate passing (ETA recall 0.98, intent 0.98, validity 1.00), against 73/76 on the base. Rules: 99/167, failing ETA recall (0.69) and intent (0.61).
- **Conclusion.** p3 is not overfitted to G3. Keep Haiku for extraction.
- **Recorded misses (not tuned on):** THR011, an ETA at the start of a conditional sentence; BB02, "choices after 2 PM" losing the earliest start; plus label-level disagreements (THR007, L08, H06) and the known AT06 and BB10. A p4 needs a new held-out set h2.
- **Tests:** `tests/test_golden.py` pins h1's hash, checks that every variant traces to its base labels (time variants shifted by exactly 40 min), and checks that all labels are self-consistent.
- **Limits:** extraction only (no multi-turn replay of variants); rewrites of G3 situations, not new situations; one reviewer.

## R1.4 – Tokenomics review (18 Nov 2026)

The reviewer confirmed: prompt caching is unavailable at about 1,900 tokens (Haiku 4.5's minimum is 4,096); Haiku batch is half price but suits offline runs only; output is about 47 % of the cost per extraction. Ordered fix: stop unnecessary calls, then reduce tokens, then make evaluation cheaper.

**Phase 1, done (no spend):** `evals/budget.py` (decision T42) is wired into every paid runner (`run_golden.py`, `replay.py`, `make_heldout.py`, `spike/bedrock_spike.py`).
- `--estimate-only`, and a hard $0.10 cap: above it, `--approved --max-usd X`.
- A per-call reserve, with retries included and failures not charged. The run stops before the call that would pass the limit.
- A result cache keyed by model, prompt and schema version, prompt and tool text, and input. Option ids and dates are normalised; a cached pick is remapped.
- Cost is recorded with its basis.
- The cache was backfilled from the recorded p3 runs (241 answers), so a full G3 v4 or h1 rerun now estimates **$0.00**. A full live replay estimates $0.35 and is refused without approval.
- Ledger: `COSTS.md`, with measured, computed and inferred kept apart (the replay spend is inferred). Tests: `tests/test_budget.py` (8).

**Phase 2, not started:** compact output plus batch (B-COST-2). It changes the model's response format, so it needs a paid, approved measurement on a new frozen held-out set before adoption.

**Phase 3:** B-AI-3 is reordered as deterministic checks, then a 20-situation human review, then a Sonnet judge only when separately approved.

## R1.5 – B-AI-3 templates vs Sonnet, free stages (18 Nov 2026)

No paid calls. `evals/phrase_ablation.py`, `evals/b_ai_3/`.
- **Scope found in the code:** a phrasing model is used only for option lists. Every other reply is a template.
- **Situations:** 20 real option replies from the scripted replay (the replay now records the options shown), frozen at `4ec8825e833f6aa7`. Both arms use the orchestrator's exact inputs (`replies.phrasing_inputs`, refactored out of the orchestrator).
- **Stages 1–4 on the templates:** grounding 20/20, completeness 20/20, policy 20/20, language 19/20, register 17/20. Three recorded Sonnet drafts pass every check, but are near-copies of the template.
- **Findings:**
  1. The reply language follows the last message: a Hindi conversation's options came back in English after a one-word "हाँ".
  2. There are no Hinglish templates, so Hinglish drivers get English.
- **Proposed** (pending the stage 5 human review, `review_sheet.md`): Hinglish templates plus a conversation-level language, both free in code; then remove Sonnet from option replies, per `07_…` §4.
- **The Sonnet arm** is wired through the spend guard with a $0 limit unless approved (estimate ≤ $0.12). Stage 6, the judge, is deferred.
- **Tests:** `tests/test_phrase_checks.py` (16).

## R1.6 – B-AI-3 language gaps fixed (18 Nov 2026)

No spend. Decision T43.
- **Hinglish templates** for every reply and question (`replies.HL`, `HL_ASK`). The same placeholders as English and Hindi; Latin letters only; dock codes, times and references unchanged, so grounding is unchanged.
- **The reply language follows the conversation** (`replies.reply_language`): Devanagari means Hindi; a reply of 2 words or fewer keeps the language of the driver's last longer message (the driver context now includes the last 6 driver messages); otherwise the extractor's language stands. No migration.
- **B-AI-3 checks on the 20 frozen situations:** templates now 20/20 on every stage (before: language 19/20, register 17/20).
- **Tests:** 3 in `test_agent_units.py` (one parametrised over 6 cases) and 3 end-to-end in `test_orchestrator.py`. `test_delay_to_pending_request_in_three_turns` now expects the Hinglish pending reply, which still says "NOT confirmed".
- **Proposed** (unchanged, pending the human review): remove Sonnet from option replies.
- **Also found during this round's full run:** `test_driver_can_switch_to_own_other_shipment_only_when_idle` failed at about 10:40 IST, because its second shipment's ETA lands near midnight and the engine plans slots only on the day the truck is ready. This is a real limitation: an overnight arrival is escalated instead of being offered a morning slot. It is now backlog **B-FEAS-1**; the test skips with that reason when its ETA crosses 22:00. Full suite: 436 passed, 1 skipped.

## R1.7 – B-FEAS-1 overnight arrivals (18 Nov 2026)

- **Cause:** `find_options` computed the closing time and the last-new-start time once, for the day the truck is ready, and judged every slot by them. A slot on the next day always "ended after facility close", so a truck ready at 23:00 got no options and was escalated.
- **Fix (decision T44):** each slot is judged by its own day's close and last-new-start time, and the search covers the ready day and the next operating day. Drivers see the date on every option ("8:00 AM on 20 Nov").
- **Parity:** for same-day slots this is the Phase 1 prototype's rule unchanged; the differential test (every shipment × 24 cases) and the PostgreSQL view tests still pass.
- **Tests:** `test_feasibility.py::test_b_feas_1_*` (3): a late truck is offered the first next-morning slot, not flagged for approval; nothing beyond the next day; same-day close and 21:00 approval unchanged. The time-of-day skip in `test_api.py::test_driver_can_switch_to_own_other_shipment_only_when_idle` is removed.
- **Replay:** the seed publishes slots for one day only, so the replay's overnight scenarios (L10, AT09, THR005) still escalate. That is correct for that data.

## R1.8 – Build step 4: the live-booking gates B2-14…B2-17 (18 Nov 2026)

| Finding | Fix | Evidence |
|---|---|---|
| **B2-16** `bind_thread_shipment()` relied on the service for driver ownership | Migration 0010: the function requires a driver session and the conversation's driver to match it; the target shipment must belong to that driver | `test_concurrency.py::test_b2_16_bind_checks_the_driver_session_itself` (wrong driver and no driver both refused, binding unchanged) |
| **B2-17** `_shipment()` decided from a pre-lock read | `resolve_thread_shipment()` reads, chooses and binds under the conversation lock and returns the authoritative shipment; a `shown_options` trigger re-checks the binding under the same lock (`STALE_SHIPMENT`); the service re-checks before marking options shown; the orchestrator retries once | `test_concurrency.py::test_b2_17_*` (3), `test_api.py::test_b2_17_*` (2, through `BookingService`: a turn waits for an in-flight switch and plans for the new shipment; options for the shipment just left are not shown), `test_orchestrator.py::test_b2_17_*` |
| **B2-14** durable inbound delivery | Migration 0011 and `setuhaul/api/inbound.py` (decision T45): the receipt is a transactional outbox; claim → send → mark published; database processing lease per turn; recovery sweep | `test_inbound_delivery.py`: a crash between claim and publish loses nothing; a duplicate after 6 min is still rejected; a crashed turn and a dead worker are redelivered and answered once; a forged session cannot claim another thread's message |
| **B2-15** concurrent HTTP webhook test | 4 threads post the same signed webhook through the FastAPI route at once | `test_inbound_delivery.py::test_four_simultaneous_http_deliveries_store_one_message_and_queue_one_event`: 1 message, 1 event |

- **Allowlists:** the agent gains `resolve_thread_shipment` (own thread, own shipment, locked); `setuhaul_ingest` gains UPDATE on the six new receipt columns. Both are pinned in `test_agent_not_operator.py`.
- **Not done here:** SQS itself (only the adapter, against a stub; a live run needs B-AWS-0); the visibility timeout must exceed the 120 s processing lease.
- **Booking switch:** stays **off**. All four gating findings are closed in code; turning it on is the reviewer's and owner's call.

## R1.9 – Build step 4, round 1 review (18 Nov 2026)

Reviewer decision: **Conditional GO for code development; NO-GO for live booking until step 6.** Accepted: migration 0010, locked shipment resolution, option binding, the outbox design, B-FEAS-1 per-day rules, the B-AI-3 template results.

| ID | Finding | P | Fix (migration 0012 unless stated) | Evidence |
|---|---|---|---|---|
| S4-01 | Webhook acknowledged a message it failed to store | P1 | Malformed messages are skipped; a storage failure makes the webhook answer 503 so the provider retries; a publish failure keeps 200 (the receipt is durable) | `test_s4_01_a_message_that_was_not_stored_is_not_acknowledged` |
| S4-02 | Lease could expire under a live worker; no fencing | P1 | Heartbeat renews the lease every third of its length (`on_heartbeat` can extend SQS visibility); `TURN_FENCE` makes every agent and feasibility write call `assert_turn_fence()` under the conversation lock; claim, renew, finish, release and the generation bump take the same lock | `test_s4_02_*` (3): a superseded worker, a long turn kept by its heartbeat, a worker that outlived its lease |
| S4-03 | Generation not checked at claim | P1 | `claim_turn` / `renew_turn` / `finish_turn` require the event's generation | `test_s4_03_an_event_for_another_generation_is_refused` |
| S4-04 | 0011 backfill assumes old enqueue claims were delivered | P1 | `RUNBOOK_0011.md` (pause, drain, migrate, check, review, reopen, resume) and `python -m setuhaul.ops.cutover_0011 check / reopen`. Historical rows are never changed automatically. 0011 has only run on test databases rebuilt every run | `test_s4_04_cutover_lists_only_uncertain_legacy_receipts_and_reopens_them` |
| S4-05 | Booking switch could run on LocalQueue | P1 | Start-up refuses: needs a durable queue; SQS attributes FIFO, DLQ, visibility ≥ 2 × lease; evidence rows for AWS_QUEUE, AURORA, RECOVERY_WORKER, BOOKING_WORKFLOW in `deployment_gates` (no application role can write it). Tests and the replay use `local_test_harness`, which `from_env` never sets | `test_s4_05_the_booking_switch_fails_closed` |
| S4-06 | Next calendar day, not next operating day | P2 | `find_options` skips closed days within 7 days; `operating_days_ahead` (1 for drivers) for a planner view. No new decision: this is T44 as written | `test_feasibility.py::test_s4_06_*` (3) |
| S4-07 | Audit principal supplied by the caller | P2 | `bind_thread_shipment()` records `driver:<app.driver_id>`; a different claimed principal is kept as `claimed_principal` | `test_concurrency.py::test_s4_07_*` |
| S4-08 | Duplicate reply after a crash before finishing | P2 | `chat_messages.reply_to` / `reply_generation`, unique for AGENT rows; `record_reply` returns the stored reply | `test_s4_08_*` |
| S4-09 | Hinglish register | P2 | Respectful forms throughout (bhej dijiye, chun lijiye, pahunchenge); "Ye slots khaali hain", "Jo slot chahiye, uska number bhej dijiye". Native-speaker review still open | B-AI-3 templates still 20/20 on all five checks |
| S4-10 | Human review should judge contextual completeness | P2 | Review sheet adds "answers what the driver asked" and flags AT04 and BB10 | `evals/b_ai_3/review_sheet.md` |

- **Found while fixing:** the recovery sweep re-claimed never-claimed receipts without an age check, so it could publish a message whose attestation was still running. It now uses `coalesce(enqueued_at, created_at)` against the threshold.
- **Allowlists:** agent and feasibility roles gain `assert_turn_fence` (read-only check); `deployment_gates` is on the reasoned no-RLS list (deployment metadata, readable only by `setuhaul_system`).
- **Fence scope:** the fence travels in-process (`contextvars`). If tools are later called over HTTP from a separate orchestrator process, it must travel with the call.
- **Still pending:** live SQS, Aurora, the recovery worker in AWS (step 6); B-WF-2, B-WF-3; the B-AI-3 human review and native-speaker check.

## R2.0 – Build step 4, round 2 review (18 Nov 2026)

Reviewer decision: **GO for continued development; NO-GO for live booking.** Accepted: the database fencing design, heartbeat and generation-aware claim and renewal, the webhook 503/200 split, the seven-day operating-day search, the audit principal, reply uniqueness, the review-sheet criteria. All R1.9 findings closed.

| ID | Finding | P | Fix (migration 0013 unless stated) | Evidence |
|---|---|---|---|---|
| S4-R2-01 | `finish_turn()` accepted an expired lease | P1 | `AND processing_until > clock_timestamp()`; `Orchestrator.handle` raises `TurnLeaseLost` when finishing fails, so the message stays unprocessed and is delivered again | `test_s4_r2_01_a_worker_cannot_finish_after_its_lease_expired` (expired, unclaimed, refused; redelivered; still one reply) |
| S4-R2-02 | A reply to a later message hid a lost one | P1 | The checker lists every receipt 0011 backfilled; later agent messages, the first one's time and the next driver message are shown as evidence. `RUNBOOK_0011.md` updated | `test_s4_r2_02_*`, `test_s4_04_*` |
| S4-R2-03 | `reopen_receipts()` could reopen a new message | P2 | Also requires `processed_at` and `published_at` set (service code) | `test_s4_r2_03_*` |
| S4-R2-04 | Evidence had no freshness or deployment binding | P1 | `deployment_evidence` replaces `deployment_gates`: environment, deployment id, `expires_at` ≤ 30 days, artifact URI and SHA-256, configuration fingerprint, `supersedes`, verifier = session user; append-only for every role. Start-up takes the latest row per gate for this environment and deployment; it must be unexpired, and for AWS_QUEUE and AURORA its fingerprint must equal the live one (`python -m setuhaul.ops.fingerprints` prints them for the pipeline) | `test_s4_r2_04_*`: other deployment, other environment, reconfigured queue, expired newest row, wrong database all refused |
| S4-R2-05 | Pipeline writer not demonstrated | P1 | `setuhaul_deploy_evidence`: INSERT and SELECT on the evidence table only, no members, no login. Mapping the pipeline's IAM identity to it is verified in step 6 (WORKLOAD_IDENTITY) | `test_s4_r2_05_*` (every application role refused; UPDATE, DELETE, TRUNCATE refused even for the owner), `test_deploy_evidence_role_can_only_insert_evidence` |
| S4-R2-06 | Six readiness areas | P1 | Gates WORKLOAD_IDENTITY and OUTBOUND_DELIVERY added; `DEPLOYMENT_GATES.md` defines what each covers and the monitoring it must show (oldest message age, DLQ depth, recovery failures, expired leases, webhook 5xx rate, failed confirmations, undelivered notifications) | `test_s4_05_*` requires all six |
| S4-R2-07 | Outbound must send the stored reply | P2 | Recorded in `DEPLOYMENT_GATES.md` (OUTBOUND_DELIVERY); to build and test in B-WF-3 | Open |
| S4-R2-08 | Human and native-speaker review | P2 | Pending with the owner | Open |

- **Limits stated plainly:** the service cannot open the evidence artifact; checking that it shows a pass is the pipeline's job before it writes the row. The four gates other than the queue and the database are bound to the deployment and expiry but have no live fingerprint the service can compute.
- **S4-06 note from the reviewer:** operating days come from published open slots, not a holiday calendar. That is acceptable while published slots are the source of availability.

## R2.1 – Build step 4, round 3 review (19 Nov 2026)

Reviewer decision: **GO for continued development; NO-GO for live booking until step 6.** Closed: S4-R2-01, -02, -03.
S4-R2-04 was accepted with qualifications and S4-R2-06 as a framework. S4-R2-05 was partially accepted; the IAM mapping
is step 6 work.

| ID | Finding | P | Fix (migration 0014 unless stated) | Evidence |
|---|---|---|---|---|
| S4-R3-01 | A future `verified_at` bypassed the 30-day limit | P1 | Insert trigger `deployment_evidence_fresh`, checked against `clock_timestamp()`: `verified_at` at most 5 minutes ahead; `expires_at` at most one window from now and from `verified_at`. The start-up check applies the same rules on read | `test_s4_r3_01_future_dated_evidence_is_refused` (the reviewer's example, 1 hour and 6 minutes ahead refused; 2 minutes of skew allowed) |
| S4-R3-02 | Four gates were bound only to the deployment id | P1 | An immutable deployment manifest ships with the artifact (`SETUHAUL_DEPLOYMENT_MANIFEST`). The deployment id is derived from it, so a changed worker build, state-machine version, IAM policy or sender configuration means a new id. Each of the four gates must carry its manifest section's fingerprint. Validity windows: 7 days for operational evidence (RECOVERY_WORKER, OUTBOUND_DELIVERY), 30 for configuration. `python -m setuhaul.ops.fingerprints` prints the id and all six fingerprints | `test_s4_r3_02_every_gate_is_bound_to_the_deployment_manifest`, `test_s4_r3_02_operational_evidence_expires_after_seven_days` |
| §3 | Append-only wording overstated | — | `DEPLOYMENT_GATES.md` now says the triggers stop ordinary DML, not a hostile administrator. The service refuses to start if either trigger is missing or disabled. Step 6 adds auditing | `test_r2_1_a_disabled_evidence_trigger_blocks_the_switch` |
| §4 | `verify()` only checked that a redrive policy exists | — | The service now checks the DLQ itself: it is a readable SQS FIFO queue, `maxReceiveCount` is 1–10, and it keeps messages at least as long as the queue. The DLQ attributes are in the AWS_QUEUE fingerprint. SQS least privilege is in the AWS_QUEUE gate (step 6) | `test_r2_1_the_dead_letter_queue_itself_is_verified` |
| §5 | Package import probe failed without psycopg | — | The probe tolerates a missing third-party driver (psycopg, boto3) and nothing else | `test_the_deployed_package_contains_no_non_india_client` |

- **Still outside the service:**
  - The manifest says what should be deployed. Drift detection against the live AWS resources is part of the step 6 gates.
  - The pipeline IAM identity mapping (S4-R2-05) is a step 6 acceptance test.
- **Still open:** S4-R2-07 (B-WF-3) and S4-R2-08 (owner).

## R3 – Complete local build review, by business flow (19 Nov 2026)

Reviewer verdict: **live booking NO-GO; local build conditionally accepted.** Not review-complete until the two P1
items are fixed. The reviewer could not run the database tests (no psycopg in their environment): 257 database-free
tests passed there and the 583-pass run stays developer-reported evidence. The final consolidated report is still on
hold.

| ID | Finding | P | Fix (migration 0019 unless stated) | Evidence |
|---|---|---|---|---|
| R3-P1-01 | Reject, withdraw and invalidation decided the deadline before the appointment and claim locks, so a late request could end WITHDRAWN, REJECTED or INVALIDATED instead of EXPIRED | P1 | `close_approval` takes the locks first, then reads `clock_timestamp()`. Past the deadline, a person's reject or withdraw is refused with `PENDING_EXPIRED` (as confirm already was), and an invalidation or supersession is recorded as EXPIRED, with the requested outcome kept in the note, the event and the escalation, so the change that caused it still commits. Every path to EXPIRED raises the PENDING_EXPIRED escalation | `test_r3_a_person_waiting_on_a_lock_past_the_deadline_cannot_reject_or_withdraw` (both), `test_r3_an_invalidation_waiting_on_a_lock_past_the_deadline_records_expired`, `test_r3_a_dock_block_after_the_deadline_records_expired_and_the_block_stands` |
| R3-P1-02 | Changes to destination, weight, dock type or refrigeration did not invalidate a pending request, and the decision did not re-check compatibility | P1 | The shipment trigger watches all four fields. `decide_booking_request` re-proves compatibility under its locks (`booking_incompatibility`, the same hard rules as the 0001 occupancy guard). The occupancy guard now also runs when a hold becomes a request's claim, so a change between option and request is caught at submission | `test_r3_a_booking_critical_shipment_change_invalidates_the_open_request` (4 fields), `test_r3_decide_re_proves_dock_compatibility_itself` (4 rules, trigger disabled), `test_r3_a_change_between_the_option_and_the_request_is_refused_at_hold_and_at_submission` |
| R3-P2-01 | Takeover suppression covered PENDING only; a RETRY could still go out, and a claimed reply could be sent after a takeover | P2 | Claim suppresses RETRY as well. An UNKNOWN reply is not resent after a takeover; it fails as `HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND` and the coordinator is told, because it may already be on the phone. `begin_send()` re-checks the takeover and the attempt number just before the provider call. The window between that check and the HTTP call cannot be fenced and is documented | `test_r3_takeover_after_a_retryable_failure_stops_the_retry`, `test_r3_takeover_after_an_ambiguous_send_is_not_resent_and_a_person_is_told`, `test_r3_takeover_after_the_claim_is_fenced_before_the_provider_call` |
| R3-P2-02 | WhatsApp 5xx was retried as if not accepted; a receipt that failed to store was still acknowledged | P2 | A 5xx is AMBIGUOUS (wait for a receipt, then at most one same-id resend). Only 429 and connection failures are retryable. A receipt that cannot be stored makes the webhook answer 503, so Meta redelivers the batch (receipts and messages are idempotent) | `test_r3_a_whatsapp_5xx_is_ambiguous_and_a_429_is_retryable`, `test_r3_a_receipt_that_cannot_be_stored_is_not_acknowledged` |
| R3-P2-03 | Console identity not enforced in every database function | P2 | `set_automation_paused`, `take_over_thread`, `hand_back_thread` and `block_dock` now need `staff_context`, check the role in the database (ops_lead to pause; operations or ops_lead to take over or hand back; planner or ops_lead to block a dock) and record the principal and role from it. The console role lost its direct INSERT on the audit log and outbox and its UPDATE of the thread's human flag | `test_r3_console_mutations_check_role_and_principal_in_the_database` (7 cases), `test_r3_console_mutations_need_the_trusted_staff_context`, `test_r3_the_console_cannot_write_audit_outbox_or_takeover_directly`, `test_r3_takeover_records_the_staff_principal_and_role` |
| R3-P2-04 | F7 and F8 could skip | P2 | F7 scripts the timeout for its own driver's number only (`LocalProvider.script_for`). F8 picks an offered slot the database itself judges inside opening hours. Both skips are now assertions | `test_f7_*`, `test_f8_*` (no `pytest.skip` left in `test_flows.py`) |

**Reviewer answers, recorded as carried-forward requirements:**
- **Q1 identity:** accepted for the approval functions. Step 6 must prove five things (in `DEPLOYMENT_GATES.md`):
  1. the console login maps only to `setuhaul_console`;
  2. no other workload can assume it;
  3. no workload can assume `setuhaul_rpc`, the owner or a superuser;
  4. the Data API transaction keeps the staff context across its statements;
  5. an ordinary operations user cannot create an ops_lead context.
  The local `Db.tx()` uses one DSN and `SET LOCAL ROLE`. That must not become a shared production credential.
- **Q2 delivery:** one identical repeat is accepted as an at-least-once trade-off, with the 5xx and receipt fixes
  above, monitoring of ambiguous sends and duplicates, and business acceptance of the residual risk.
- **Q3 busier time:** acceptable as a conservative heuristic that sends moves to a planner. Before any POLICY_CONFIRM
  rule relies on it, it must compare peak occupancy with usable capacity and keep the calculation in the evidence.
  No automatic rule is enabled.
- **Q4 step 6 order:**
  1. B-AWS-0 and workload identity;
  2. AURORA;
  3. AWS_QUEUE;
  4. RECOVERY_WORKER;
  5. BOOKING_WORKFLOW;
  6. OUTBOUND_DELIVERY.
  All six must be current against one manifest, with live drift detection.

**Found while building the R3 package:** F4 built its breakdown window with time-of-day arithmetic. For a slot
starting at 22:00 or later it sent end = start, which the API rightly refuses. It now uses start + 1 hour. No other
test in `test_flows.py` depends on the time of day; F8's slot is chosen by the database (R3-P2-04).

## R3.1 – Follow-up to the R3 response (19 Nov 2026)

Reviewer: R3 largely positive; two more findings before the local review is complete. Booking switch stays off; no
final report yet.

| ID | Finding | P | Fix (migration 0020) | Evidence |
|---|---|---|---|---|
| R3.1-P1 | `decide_booking_request` read the shipment and dock without locks. A change could hold its row lock, wait for the approval in its trigger, and commit after the confirmation, so the trigger saw a closed request | P1 | Lock order thread → shipment → dock → approval → appointment → claim. The decision SHARE-locks the shipment and dock before the approval and re-reads them under the lock. A change that waited for a decision is a change to a CONFIRMED booking; if the booking is no longer compatible, `flag_bookings_at_risk` raises BOOKING_AT_RISK. No path takes the reverse order. `setuhaul_rpc` gets UPDATE on a bookkeeping column (`updated_at`) only, because FOR SHARE requires it | `test_r31_a_shipment_change_that_got_there_first_makes_the_decision_wait_then_refuse`, `test_r31_a_change_that_arrives_during_the_decision_waits_and_a_person_is_told` (shipment, dock), `test_r31_decisions_racing_shipment_changes_never_deadlock_and_never_approve_silently` |
| R3.1-P2 | 0019 excluded planner from takeover and handback, contrary to §8 | P2 | Planner allowed in both functions and in the API permissions. The audit row records the actual staff role | `test_r31_a_planner_can_take_over_and_hand_back_at_their_facility`, `test_r31_a_planner_at_another_facility_cannot_take_over_or_hand_back` |

Existing tests changed by R3.1. The API permission matrix now gives planner takeover and handback, so
`test_matrix_matches_code` and `test_console_role_action_matrix` were updated. In
`test_r3_a_booking_critical_shipment_change_invalidates_the_open_request[destination_facility_id]` the decision now
locks the shipment first. A shipment moved to another facility is out of the planner's scope, so the refusal is
SHIPMENT_NOT_VISIBLE instead of ALREADY_DECIDED; it is still refused. Result: 620 passed. Development note: the
first run hung because one new test held a row lock after a failed assertion. The R3.1 lock tests now release the
lock in `finally` and use a 15 s `lock_timeout`, so a mistake fails fast.

## R3.2 – Follow-up to the R3.1 response (20 Nov 2026)

Reviewer: R3.1 is a good correction; one more P1 and two small items. Booking switch stays off; no final report yet.

| ID | Finding | Sev | Fix | Test |
|---|---|---|---|---|
| R3.2-P1 | `flag_bookings_at_risk` read the shipment and dock without serialising concurrent compatibility changes. A shipment going 1,000 → 1,500 kg and a dock going 2,000 → 1,200 kg each passed against the other's old row; both committed with no escalation | P1 | Migration 0021: one transaction-scoped advisory key per dock, taken by both triggers after their own row lock and before any approval row, in dock_id order. No row locks on the other table. The later change re-reads in a fresh snapshot and escalates. REPEATABLE READ is refused | `test_r32_shipment_and_dock_changes_to_one_booking_are_serialised_and_a_person_is_told` (shipment first, dock first), `test_r32_simultaneous_shipment_and_dock_changes_end_compatible_or_escalated`, `test_r32_a_compatibility_change_under_repeatable_read_is_refused` |
| R3.2-a | Planner audit assertion compared the row with itself | P3 | Asserts principal and role for takeover and handback | `test_r31_a_planner_can_take_over_and_hand_back_at_their_facility` |
| R3.2-b | Stale open item in REVIEW_REQUEST.md | P3 | Removed | n/a |

Proof: with 0021 removed, both deterministic R3.2 cases fail; the incompatible booking has no escalation. With 0021
they pass. The simultaneous-race test also passes without 0021, because a real collision is rare in a race. It guards
against deadlock and the invariant; the deterministic pair is the proof.

Result: 624 passed in one full run (12 min 24 s).

## R3.2 verdict (20 Nov 2026)

Reviewer: R3.2-P1 accepted; R3.1 items closed; no new P1 or P2. **Local Phase 3 implementation accepted. Production
booking NO-GO. AWS step 6 acceptance pending.** `SETUHAUL_BOOKING_ENABLED` stays off. The reviewer ran 257
database-free tests (17 skipped) and a compile check. The 624-test suite was not reproduced there (no psycopg), so
it remains developer-reported. Future hardening: a bulk transaction that updates several shipments or docks must
take the booking-compat keys in one global dock_id order for the whole transaction, not per trigger. The final
consolidated report has not been written yet; the next major review is step 6, on deployed resources.

## Human end-to-end check (demo console, 20 Nov 2026)

The owner walked the flows in `demo/` (local stack; rules extractor, no AI cost; booking switch off). Findings:

| ID | Finding | Sev | Fix | Test |
|---|---|---|---|---|
| H-1 | The rules extractor (also the production fallback when the model is down) did not read a bare time given as the answer to "what time will you reach?" ("12 baje", "12 ko", "1 pm"), nor Hindi word order ("shaam 6 baje pahunch jaunga"), nor "Mera late hai - 3 ghanta". The driver was asked the same question again and again. It also read "raat 10 baje" as 10:00 (it matched "at" inside "raat") | P2 | `rules_extractor`: Hindi-order times with day parts (subah/dopahar/shaam/raat), checked first; a bare time counts as the answer when the agent's last question was about the arrival time (`asked`, from turn facts, passed in by the orchestrator); a duration anywhere in a message that says "late" | `tests/test_agent_units.py::test_h1_*` (15 cases), `tests/test_orchestrator.py::test_h1_*` (the reported conversations, end to end) |
| H-2 | Demo console: a Gurugram driver's request did not show for the signed-in Jaipur planner (correct scoping, but not explained), so it looked stuck and then expired | Demo | The console follows the selected driver's facility and says why; a guided walkthrough runs each flow step by step with automatic checks | demo only |
| H-3 | After the first request was decided (rejected, expired, invalidated or approved), the conversation stayed in SUBMITTED for ever, because `BookingFlow.warehouse_decided` was never called. A rejected driver was told "reply here and we will find another one", then got "I could not find a slot... call your dispatcher". A driver delayed again after an approval could never get a new slot. The F2 flow test missed it: it checked that a slot lookup was attempted, not that slots were offered | P1 | `BookingService._flow` reads the appointment's status from the database when the stored state is SUBMITTED: CONFIRMED → BOOKED, anything else no longer pending → CLOSED. BOOKED may look for slots, and the new request replaces the confirmed booking through the warehouse | `test_f2_*` (now requires real slots), `test_h3_after_an_approved_booking_a_further_delay_gets_fresh_options_and_a_replacing_request` |
| H-4 | F6 in the demo: duplicates are, correctly, invisible on the phone, so the tester could not see anything happen | Demo | The phone panel shows a system note for each repeat, with the database counts (copies stored, replies stored); the guide checks them | demo only |
| H-5 | After a hand-back the agent continued from database facts only (shipment, current booking). It never saw the coordinator's summary, and the driver was not told the agent was back | P2 | Owner chose option 1. Migration 0022: `hand_back_thread` keeps the summary on the conversation, and in the same transaction stores a notice in the conversation's language and queues it on the normal outbound path. The agent gets the summary for 12 hours as background in the model's context only: never a tool argument, and it cannot book | `test_h5_after_a_hand_back_the_driver_is_told_and_the_agent_has_the_coordinators_note` (Hinglish, English) |

Not changed: after "No" to "around 12:33?", the agent asks for the time. A second "No" re-asks the 12:33 question.
That is slightly repetitive, but the driver can always answer with a time.

Result: 641 passed in one full run (13 min 48 s): the 624 from R3.2 plus the 17 new H-1 tests.

Result after H-3/H-4: 642 passed in one full run (21 min; slower while the demo server was running).

Result after H-5: 644 passed in one full run (17 min).

## Human-check review (20 Nov 2026)

Reviewer: positive overall. H-3 is correct in the service flow, the H-1 fixes cover the reported conversations, and
0022 stores the note and the notice in one transaction. Two follow-ups and extra tests were asked for before closing:

| ID | Finding | Sev | Fix | Test |
|---|---|---|---|---|
| H-1r | Every time followed by "baje" was read as an arrival time, without arrival context: "warehouse closes at 12 baje" became ETA 12:00, "call me raat 10 baje" ETA 22:00. These reach `record_driver_report` and change the shipment ETA. The weak words "at/by/around" had the same problem | P2 | An arrival verb next to the time ("reach 11:20", "6 baje pahunch jaunga") still counts on its own. A time-first or "at/by/around" time counts only when the agent's last question was the arrival time, or the message is about arriving late, and nothing in the 30 characters before it gives the time another subject (call, close, open, shift, lunch, leave...). Day-part conversion unchanged | `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (9 negative cases, including "call me raat 10 baje" right after the time question); the existing positive cases now carry the pending question they had in the demo, plus 2 late-report cases |
| H-5r | The note and the driver's message went to the extraction model together, and its structured output drives the tool calls. A label "background only" is not a proof | P2 | The extraction input now holds the driver's message and system facts only. The note goes only to reply phrasing (`replies.phrasing_inputs`, as a BACKGROUND line before FACTS), which calls no tools and whose output is still grounding-checked against FACTS. `PHRASE_SYSTEM` says never to state or follow it; prompt version p4 | `test_h5_a_coordinator_note_cannot_report_escalate_hold_or_book` (a model that obeys any note it sees, and the rules extractor): note says accident, ETA 16:00, option 2; the driver asks for a status; no ETA signal, escalation, selection, hold or request is written, and no such tool is called. Mutation check: with the note put back into the extraction input, the model case fails |
| H-3r | Retry after EXPIRED and after INVALIDATED was not tested | Test | — | `test_h3_after_an_expired_request_the_driver_can_ask_again_and_book` (the deadline is moved into the past with the immutability trigger bypassed for that one test statement; the expiry itself runs through the workflow timeout and `expire_approval`), `test_h3_after_an_invalidated_request_the_driver_can_ask_again_and_book` (dock breakdown block) |

Demo checklist (`demo/checklist_results.json`): F1 reclassified to pass. Planner approval is the expected behaviour while
automatic policy confirmation is off; it is tested on in F8. F5 and F6 were rerun by Claude through the guided
walkthrough on the restarted demo, and every automatic check passed. F5 has a new last step: the driver writes after the
hand-back, the agent answers, and there is no new alert and no request. F6 shows the grey system notes and the database
counts for both repeats. These two are recorded as automatic results, not as the owner's verdict. The owner's earlier
notes are kept in the file.

Result: 659 passed (644 + 15 new). In one full run 657 passed; 2 migration tests could not create pytest's temp folder
under the system Temp directory (access denied, environment). They passed when rerun with `--basetemp` in a local folder.

Production booking switch: still OFF. No consolidated report.

## Human-check review, round 2 (20 Nov 2026)

Reviewer: H-3r accepted. H-5r separates the note from extraction and from the tool path. Two P2 follow-ups:

| ID | Finding | Sev | Fix | Test |
|---|---|---|---|---|
| H-1r2 | Arrival context came from a word anywhere in the message, while the other-subject check looked only at the 30 characters before the time. Reproduced: "I'm late, at 12 baje the warehouse closes", "Traffic hai, at 1 pm lunch break", "Late ho gaya, 12 baje office band", "late, around 3 pm for the call", "pahunchne ke baad 12 baje call karna" all set an ETA | P2 | Clause-aware: the message is cut at punctuation and conjunctions (and/but/then/aur/phir/lekin/par...). A weak time is rejected if its own clause, before or after the time, gives it another subject. It is accepted only if its clause has an arrival word, or the agent's last question was the arrival time, or the clause is the bare time alone in a message about arriving late. The same other-subject check now guards "ke baad / after" times | `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (the 5 reported + 2 more; also asserts no "slot after" time), `test_h1_arrival_times_in_a_late_report_are_still_read` (6 legitimate cases, including "late, around 3 pm, call me if needed") |
| H-5r2 | The phrasing model has no tools, but the grounding check accepted "Your booking is guaranteed. The warehouse has cleared it." when FACTS held only options needing approval | P2 | Deterministic limits in `grounding_violations`, whatever the prompt or note says: a booking-status claim (approved, cleared, booked, reserved, pakka, ho gaya, पक्का, मंज़ूर हो...) needs a CONFIRMED booking in FACTS; an operational promise (guaranteed, no waiting, priority, will be ready, tension mat, गारंटी...) is never allowed, because no tool produces one. On a violation the model gets one redraft, then the fixed template is sent. "needs warehouse approval" is not a claim | `test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` (the phrasing model sees the note and obeys it in the draft and the redraft; the template is sent and stored; nothing requested), `test_h5r_a_phrased_reply_may_not_claim_a_status_or_make_a_promise` (12 drafts, English/Hinglish/Hindi), `test_h5r_every_option_template_passes_the_claim_check` (the fallback never trips). Mutation-checked: with the new checks off, the end-to-end test fails. The extraction-isolation test is kept |

Known limit: the claim check is a word list. It is deliberately conservative: a false alarm costs only the model's wording, because the template is then sent. A claim worded in a way the list does not know would still pass. Only option lists are model-phrased today; every other reply is a template.

Demo checklist: unchanged. F5 and F6 are still Claude's automated reruns (guided walkthrough, all automatic checks passed). They are not the owner's visual confirmation, which is still owed.

Result: 688 passed in one full run (14 min): 659 + 29 new.

Production booking switch: still OFF. No consolidated report.

## Human-check review, round 3 (21 Nov 2026)

Reviewer: R2 is a good improvement; 159 agent-unit tests, the traceability test and compilation pass on their side.
Two narrow P2 follow-ups:

| ID | Finding | Sev | Fix | Test |
|---|---|---|---|---|
| H-1r3 | "late, 12 baje, office band" (also "warehouse closes", "meeting hai"): the commas left the time as a bare clause, so the subject in the next clause was not seen, and the ETA became 12:00 | P2 | When a time is accepted only as a bare time in a late report, the clause before it and the clause after it are checked too. If either names an event that happens at a time (closes, band, khulta, meeting, lunch, break, shift, office, train...), the time is not taken as the arrival time. The report then has no ETA, so the agent asks for it. A request such as "call me if needed" is not such an event | `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (+4: the three reported and "late hoon. 3 pm. lunch break"), `test_h1_a_time_owned_by_another_event_makes_the_agent_ask_for_the_arrival_time` (asserts no ETA and that the arrival time is asked), `test_h1_arrival_times_in_a_late_report_are_still_read` (unchanged, includes "late, around 3 pm, call me if needed") |
| H-5r3 | The grounding check accepted semantic claims with no claim word: "green light", "all set", "good to go", "now active". A word list cannot close this | P2 | Constrained structure (`replies.structure_violations`, checked before the word lists). An option reply may be only the system's own option list: its fixed status lines and numbered lines, unchanged and in order (the numbers also select the option). It may add at most one line from a fixed set of courtesy lines (`SAFE_EXTRAS`, en/hi/hi-Latn), before or after the list. Any other wording means one redraft, then the template. All booking-status language comes from the system. The word lists stay as a second layer. Prompt version p5 | `test_h5r2_an_option_reply_may_not_add_any_wording_of_its_own` (the 4 reported, plus 3 more, each placed before, after, or instead of the closing line), `test_h5r2_the_option_list_with_at_most_one_fixed_line_is_accepted` (3 languages; rejects 2 extra lines, reordered or missing lines), `test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` (now 5 claims, including the 4 reported; asserts the reply is exactly the system's option list). Mutation check: without the structure check, 3 of the 5 reach the driver. The other 2 are caught by the word list and by the dock/time check |

Consequence, for the owner: the model's wording now adds nothing to an option list except, at most, one fixed courtesy
line. The coordinator's note can therefore influence only which courtesy line is used. Option replies are in effect
deterministic. B-AI-3 had already proposed dropping the phrasing model from option replies, and doing so would save its
cost. That is an owner decision, not made here: the call is still there and is still checked.

Known limit (H-1r3): the event list is a word list. An event the list does not know, in the next clause, could still own the time.

Demo checklist: unchanged. F5 and F6 are still Claude's automated reruns, not the owner's visual confirmation.

Result: 723 passed in one full run (22 min): 688 + 35 new.

Production booking switch: still OFF. No consolidated report.

## Human-check review, round 4 (21 Nov 2026), and owner decision OD-3

Reviewer: round 3 is positive. H-1r3 fixes the reproduced cases and keeps the legitimate arrival times; H-5r3 limits
option replies to the system's list and fixed courtesy lines. F5 and F6 automated reruns are accepted as test evidence;
the owner's visual confirmation is still pending.

**OD-3 (owner, 21 Nov 2026), on the reviewer's recommendation: remove the phrasing-model call from production option
replies.** After H-5r3 the model could only choose one of three fixed courtesy lines, so it added cost, latency and a
model in the reply path for no value. Option replies are now the deterministic English, Hindi and Hinglish templates.
AI extraction is unchanged and stays independent of the reply.

| ID | Finding | Sev | Fix | Test |
|---|---|---|---|---|
| OD-3 | Phrasing model adds no value to option replies | Owner decision | `Orchestrator._options_reply` returns `replies.options_text(options, lang)` and calls no model (path `phrase:template`). The `use_model_phrasing` switch and `_phrase_options` are removed, and the coordinator's note is no longer passed to any reply code, so it reaches no model at all. `phrasing_inputs` no longer takes a note (no BACKGROUND line). The old draft, check, redraft loop survives only in `evals/phrase_ablation.py` (`sonnet_arm`) so B-AI-3 can be re-run; it is not reachable from production | `tests/test_orchestrator.py::test_od3_option_replies_are_the_template_and_call_no_phrasing_model` (en / hi-Latn / hi: the reply equals the template, the only model call is the extraction, `phrase()` is never called even though a model that writes "D9, confirmed" is available); `tests/test_flows.py::test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` (now: before and after the hand-back the reply is exactly the system's list, no phrase call, the note reaches no model); `test_h5_after_a_hand_back_...` (the note is in no model call) |
| H-5r4 | `SAFE_EXTRAS` had "I have the coordinator's update.", which passed both checks even with `note=None` | P3 | Removed in all three languages (no model is called now, so the template is sent without it). The two lines left claim nothing | `tests/test_agent_units.py::test_h5r4_no_courtesy_line_claims_a_coordinator_update` (no courtesy line mentions a coordinator or an update; the prompt block has no note; the old line is now rejected by `structure_violations`) |

Unchanged: `structure_violations` and `grounding_violations` stay, used by the ablation and as a check if a phrasing
model is ever brought back. `PHRASE_SYSTEM` (p5) is unchanged and now used only by the eval, so no golden-set rerun is
needed. Costs: one fewer model call per option reply.

Result: 728 passed in one full run (14 min): 723 + 3 new unit tests + 2 net in the orchestrator tests (one fallback test replaced by three language cases).

Demo checklist: unchanged. F5 and F6: Claude's automated reruns, accepted as test evidence; the owner's visual
confirmation is still pending.

Production booking switch: still OFF. No consolidated report.

## Human-check review, round 4 verdict (21 Nov 2026)

Reviewer: **OD-3 accepted, H-5r4 closed.** They checked that `_options_reply` calls `replies.options_text` directly,
that `_phrase_options` and `use_model_phrasing` are gone, that the note reaches no reply code, and that the production
package has no `self.model.phrase()` call. Independent run: 336 selected tests passed (no skips), plus compilation. The
full 728-test suite was not reproduced there (no psycopg), so it stays developer-reported evidence.

| ID | Finding | Sev | Fix | Test |
|---|---|---|---|---|
| D-OD3 | Older descriptions still said production uses a phrasing model | P3 (docs) | Reworded to separate production (model extraction, deterministic option replies) from evaluation (optional phrasing ablation): the `orchestrator.py` module docstring, the `models.py` module docstring (`phrase()` is kept for the ablation and the spike only), `evals/b_ai_3/README.md` (an outcome note at the top; the rest marked historical) and `README.md` (step 3 row, the B-AI-3 proposal, "still needed") | Documentation only; database-free tests rerun |

Reviewer's status: the local Phase 3 implementation review is complete from the code-review side. Still pending, as
acceptance evidence: the owner's visual confirmation of F5 and F6, the native-speaker Hinglish / B-AI-3 review, and
AWS step 6. Production booking: NO-GO; `SETUHAUL_BOOKING_ENABLED` stays OFF. No consolidated report yet.

## Owner visual confirmation of F5 and F6 (21 Nov 2026)

The owner ran F5 (safety takeover, hand-back, the driver writes again) and F6 (WhatsApp repeats) in the demo console,
restarted on the OD-3 build, and reported both as **passed**. This is the owner's own visual confirmation, separate
from Claude's earlier automated reruns (which stay recorded in `demo/checklist_results.json` under each note).
`checklist_results.json` F5 and F6 now lead with the owner's confirmation.

Still pending, as acceptance evidence: the native-speaker Hinglish / B-AI-3 review and AWS step 6. Production booking:
NO-GO; `SETUHAUL_BOOKING_ENABLED` stays OFF. No consolidated report yet.

## Native-speaker Hinglish and Hindi review, S4-R2-08 (21 Nov 2026)

The owner reviewed every Hinglish and Hindi driver message in the shared review sheet ("SetuHaul Hinglish and Hindi
driver message review"). Result: 20 of 24 Hinglish agent messages OK, 23 of 24 Hindi OK, all 8 system notices OK
(including Claude's drafts for the 7 that were English only), and 15 reading-test messages written as drivers type.
Sign-off was left at "not approved" pending these changes.

| Item | Reviewer said | Done | Test |
|---|---|---|---|
| Hinglish #5 "slot was taken" | Rewrite: will you not offer another slot? | No text change: the line is always followed by fresh options, or by the no-slot message (`orchestrator.py`, the `lost` path). Put back to the reviewer to confirm | existing booking-race tests |
| Hinglish #6 "slot stays" | Drop the new-time sentence | Done (`HL["keep"]`); English and Hindi unchanged, as reviewed | `tests/test_agent_units.py::test_s4_r2_08_reviewed_hinglish_wording` |
| Hinglish #10 deadline question | Rewrite: not understood | Redrafted by Claude ("gate se bahar nikalna ... maal utarna shuru"); "utar" added to the unloading words so the answer is understood. **Awaiting the reviewer's OK** | `tests/test_agent_units.py::test_s4_r2_08_reviewed_hinglish_wording` |
| Hinglish and Hindi #11 order question | Number the orders; reply with the number | Done in all three languages (`replies.numbered`). A bare number ("2", "2 number", "no. 2") answers the question and is not read as a slot pick (`orchestrator._bare_number`, path `shipment-by-number`). The status path now also remembers this question | `tests/test_agent_units.py::test_s4_r2_08_the_order_question_numbers_the_orders`, `tests/test_agent_units.py::test_s4_r2_08_a_bare_number_is_read_as_the_answer_to_a_numbered_question`, `tests/test_orchestrator.py::test_s4_r2_08_the_order_question_is_numbered_and_a_number_answers_it` |
| System notices (Part B) | All OK | `notify/outbound.py`: `TEMPLATES_HL`, `TEMPLATES_HI`, `template_text`; the language is read from the driver's last longer message in the notification's thread (the agent's rule); no thread means English. Known limit: read at send time, so a resend after the driver changes language uses the new one. Nine flow and outbound tests now expect Hinglish, because their conversations end with "haan ji, kar do" | `tests/test_outbound.py::test_s4_r2_08_an_outcome_notice_is_sent_in_the_conversations_language`, `tests/test_outbound.py::test_s4_r2_08_every_outcome_notice_has_reviewed_hinglish_and_hindi` |
| Reading test (Part C) | 15 messages | Run through the rules reader (the AI reader was not run: paid). Before: 5 of 15 right. Fixed: "pahunch" spellings (pohonch, pohunch, pochun...), "Shaam 4 se 6" with a day part, a finished repair ("repairing ka kaam ho gaya", negation guarded), early arrival ("jaldi pahunch gaye", "warehouse ke paas"), Hindi-order deadline ("raat 10 baje se pehle gate se nikalna"), "1 number" as a slot pick, an arrival word with no time now asks for the time, and "haaji" as yes. Now 14 of 15 | `tests/test_agent_units.py::test_s4_r2_08_reading_test_arrival_times`, `tests/test_agent_units.py::test_s4_r2_08_reading_test_breakdowns_and_safety`, `tests/test_agent_units.py::test_s4_r2_08_reading_test_status_pick_and_yes` |
| Reading test #15 "baat kar sakte hai kya" | Talk to a person | **Open.** The extraction schema has no request for a person, so neither reader can act on it. Adding one changes the schema and prompt (decision T28: a golden-set run). Owner decision | none yet |

Result: 743 passed in one full run (15 min): 728 + 15 new.

Still pending from this review: the reviewer's OK on the redrafted Hinglish #10 and on #5, the "talk to a person"
decision, and optionally a paid AI-reader run on the 15 messages. Production booking switch: still OFF. No
consolidated report.

Owner decision (21 Nov 2026): S4-R2-08b ("talk to a person") is deferred, not built now.
Reviewer follow-up (21 Nov 2026): the redrafted Hinglish deadline question (#10) and the unchanged "slot was taken" line (#5) are approved. S4-R2-08 is closed; S4-R2-08b stays deferred. No code change beyond the source comment.
