# Review request: SetuHaul complete local build, by business flow (after human-check review round 4 verdict)

**The local build is complete: inbound delivery, the agent, warehouse approvals (B-WF-2), confirmation rules
(B-WF-2a) and outbound notifications (B-WF-3), with nine end-to-end flow tests. 743 tests pass, none skipped.** This
package replaces per-milestone reviews, as agreed on 19 Nov 2026. It covers migrations 0010–0022 and decisions T45–T51.

**Local, not AWS.** Everything here runs on local PostgreSQL 18, with:
- a scripted model instead of the AI;
- locally signed Cognito tokens;
- `LocalQueue` instead of SQS;
- `LocalApprovalWorkflow` instead of Step Functions;
- `LocalProvider` instead of WhatsApp.

No AWS resource was touched and no paid model call was made. Real AWS acceptance is step 6, gated by
`DEPLOYMENT_GATES.md`.

**The production booking switch (`SETUHAUL_BOOKING_ENABLED`) stayed off throughout.** The booking path runs only under
the test harness.

**Please do not prepare the final consolidated report yet.** The owner asked for it only after this review is complete.

## Current status (21 Nov 2026)

- **Local build:** complete; 800 tests pass after Phase B (developer-reported full run).
- **Phase B (T52):** OpenRouter primary (Haiku 4.5 pinned to a zero-data-retention route), Secrets Manager, usage ledger and daily budget (migration 0023), Bedrock optional.
- **Human checks:** F5 and F6 confirmed by the owner. Native-speaker Hindi/Hinglish review done and closed (S4-R2-08).
- **B-AI-3:** superseded by OD-3; the Sonnet comparison was not run.
- **AWS step 6:** not started. B-AWS-0 (Sydney-only account) and all six gates in `DEPLOYMENT_GATES.md` are open.
- **Booking switch:** off. Deferred: "talk to a person" (S4-R2-08b).

Everything below is the review history, newest first. Statements such as "still pending" describe the state at the
time of that section and are kept as written.

## Native-speaker Hinglish and Hindi review (S4-R2-08, 21 Nov 2026)

The owner reviewed every Hinglish and Hindi driver message and wrote 15 test messages as drivers type them. Applied:
- the reviewed wording (Hinglish "slot stays"; a numbered order question in all three languages, answered by number);
- warehouse-outcome notices now go out in Hinglish or Hindi when the driver writes that way;
- the rules reader now reads 14 of the 15 test messages correctly (5 before).

The reviewer then approved the redrafted Hinglish deadline question and the unchanged "slot was taken" line, so
S4-R2-08 is closed. Recognising "talk to a person" (S4-R2-08b) is deferred by the owner. Details: `REVIEW_LOG.md`.

## Since your round 4 verdict

OD-3 accepted and H-5r4 closed, as recorded in `REVIEW_LOG.md`. Your one P3 cleanup is done (D-OD3). Production
(model extraction, deterministic option replies) is now described separately from evaluation (optional phrasing
ablation) in the `orchestrator.py` and `models.py` module docstrings, at the top of `evals/b_ai_3/README.md`, and in
`README.md`. No code changed.

**F5 and F6: the owner confirmed both by eye on 21 Nov 2026** in the demo, restarted on the OD-3 build. That closes the
visual-confirmation item. The automated reruns stay recorded as well. Remaining work is acceptance evidence only: the
native-speaker Hinglish / B-AI-3 review, and AWS step 6.

## Since your fourth human-check review

**Owner decision OD-3, as you recommended: production option replies call no phrasing model.** They are the
deterministic English, Hindi and Hinglish templates. AI extraction is unchanged and independent.

| ID | Asked | Done | Look at |
|---|---|---|---|
| OD-3 | Remove the phrasing-model call from production option replies; keep the three templates; keep extraction independent | `Orchestrator._options_reply` sends `replies.options_text(options, lang)`. `_phrase_options` and the `use_model_phrasing` switch are gone, and the coordinator's note is passed to no reply code, so it reaches no model. The old loop is kept only in `evals/phrase_ablation.py` for B-AI-3 re-runs | `setuhaul/agent/orchestrator.py`; `tests/test_orchestrator.py::test_od3_option_replies_are_the_template_and_call_no_phrasing_model` (three languages; the only model call is the extraction); `tests/test_flows.py::test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` (rewritten: the reply is exactly the system's list before and after a hand-back) |
| H-5r4 (small) | "I have the coordinator's update" passed with `note=None` | Removed from `SAFE_EXTRAS` in all three languages; the template is sent without it | `setuhaul/agent/replies.py`; `tests/test_agent_units.py::test_h5r4_no_courtesy_line_claims_a_coordinator_update` |

`PHRASE_SYSTEM` is unchanged (p5) and now used only by the eval. F5 and F6: the automated reruns are accepted as test
evidence; **the owner's visual confirmation is still pending.**

## Since your third human-check review

| ID | Asked | Fix | Look at |
|---|---|---|---|
| H-1r3 (P2) | A bare time between commas must check the neighbouring clause; ask instead of updating the ETA; keep "late, around 3 pm, call me if needed" | A time taken only as a bare time in a late report is rejected if the clause before or after it names an event that happens at a time (closes, band, meeting, lunch, shift, office...). The report then has no ETA, so the agent asks for the arrival time. "Call me if needed" is a request, not such an event, so the positive case is kept | `setuhaul/agent/rules_extractor.py` (`_EVENT_OWNER`, `_neighbours`); `test_h1_a_time_owned_by_another_event_makes_the_agent_ask_for_the_arrival_time`, `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (+4), `test_h1_arrival_times_in_a_late_report_are_still_read` |
| H-5r3 (P2) | No semantic claims; prefer a constrained structure over a growing word list | `replies.structure_violations`: an option reply may be only the system's own option list (fixed status lines and numbered lines, unchanged, in order) plus at most one fixed courtesy line (`SAFE_EXTRAS`, en/hi/hi-Latn) before or after it. Anything else means one redraft, then the template. Booking-status language comes only from the system; the word lists stay as a second layer. Prompt p5 | `setuhaul/agent/replies.py`, `orchestrator.py` `_phrase_options`; `test_h5r2_an_option_reply_may_not_add_any_wording_of_its_own` (your 4 + 3, in three positions), `test_h5r2_the_option_list_with_at_most_one_fixed_line_is_accepted`, `test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` (5 claims, reply must equal the system's list; mutation-checked) |

**Consequence to confirm with the owner:** option replies are now in effect deterministic. The model can add only one
fixed courtesy line, so the coordinator's note can affect only that choice. Dropping the phrasing model from option
replies (proposed in B-AI-3) is left to the owner.

The demo checklist is unchanged. **F5 and F6 are Claude's automated reruns, not the owner's visual confirmation.**

## Since your second human-check review

| ID | Asked | Fix | Look at |
|---|---|---|---|
| H-1r2 (P2) | Clause-aware time reading, context on both sides; the 5 reproduced cases as negative tests | The message is cut into clauses at punctuation and conjunctions. A time without an arrival word is rejected if its own clause names another subject before or after it (call, close, band, lunch, meeting...). It is accepted only with an arrival word in its clause, the agent's pending arrival-time question, or as a bare time in its own clause in a late report. The same guard applies to "ke baad / after" times | `setuhaul/agent/rules_extractor.py` (`_clause`, `_about_arriving`); `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (your 5 + 2), `test_h1_arrival_times_in_a_late_report_are_still_read` (6) |
| H-5r2 (P2) | Phrased replies must not claim unsupported status or make commitments | Deterministic, in `grounding_violations`: a booking-status claim needs a CONFIRMED booking in FACTS; an operational promise is never allowed. After one failed redraft the fixed template is sent. Word lists in English, Hinglish and Hindi; conservative by design (a false alarm only costs the model's wording) | `setuhaul/agent/replies.py` (`_STATUS_CLAIM`, `_PROMISE`); `test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` (an obeying phrasing model, mutation-checked), `test_h5r_a_phrased_reply_may_not_claim_a_status_or_make_a_promise` (12), `test_h5r_every_option_template_passes_the_claim_check`. Extraction-isolation test kept |

The demo checklist is unchanged. **F5 and F6 are Claude's automated reruns, not the owner's visual confirmation**, which is still owed.

## Since your human-check review: the follow-ups

| ID | Asked | Fix | Look at |
|---|---|---|---|
| H-1r (P2) | "closes at 12 baje" and "call me raat 10 baje" set the ETA | An arrival word next to the time ("reach 11:20", "6 baje pahunch jaunga") still counts on its own. A time-first or "at/by/around" time counts only after the agent's arrival-time question, or in a message about arriving late, and never if the 30 characters before it give it another subject (call, close, open, shift, lunch, leave...). Day-part conversion unchanged | `setuhaul/agent/rules_extractor.py` (`_ETA`, `_ETA_HI_ARR`, `_ETA_WEAK`, `_ARRIVAL_CTX`, `_NON_ARRIVAL`); `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (9 negatives). The positive "12 baje" cases now carry the pending question they had in the demo |
| H-5r (P2) | The note went to the extraction model with the driver's message | `_user_block` (extraction input) holds the driver's message and system facts only. The note reaches only `replies.phrasing_inputs` as a BACKGROUND line before FACTS: that step calls no tools and stays grounding-checked against FACTS. `PHRASE_SYSTEM` says never to state or follow it (prompt p4) | `setuhaul/agent/orchestrator.py`, `replies.py`, `prompts.py`; `test_h5_a_coordinator_note_cannot_report_escalate_hold_or_book` (a model that obeys any note it sees, and the rules extractor). Mutation-checked: with the note back in the extraction input the model case fails |
| H-3r | Retry after EXPIRED and INVALIDATED | Tests only | `test_h3_after_an_expired_request_the_driver_can_ask_again_and_book`, `test_h3_after_an_invalidated_request_the_driver_can_ask_again_and_book`. In the expired test the deadline is moved into the past with the immutability trigger bypassed for that one statement; the expiry itself runs through the workflow timeout and `expire_approval` |
| Checklist | F1, F5, F6 | F1 reclassified to pass: planner approval is expected while policy confirmation is off (F8 covers it on). F5 and F6 were rerun by Claude through the guided walkthrough on the restarted demo, and all automatic checks passed. F5 has a new last step: after the hand-back the driver asks for a status and the agent answers with no new alert or request. F6 shows the system notes and the database counts. **These are automatic results, not the owner's verdict** | `demo/checklist_results.json`, `demo/console.html` |

## Since your R3.2 acceptance: the owner's hands-on check

After your verdict, the owner walked F1–F9 by hand in a local demo console (`demo/`: a driver's phone and the
warehouse console on the local stack, with the rules extractor, so no AI cost and the booking switch off). Their
results are in `demo/checklist_results.json`. This found three defects in production code, all fixed:

| ID | Found | Fix | Look at |
|---|---|---|---|
| H-3 (P1) | After the first request was decided (rejected, expired, invalidated or approved), the conversation stayed in SUBMITTED for ever: `BookingFlow.warehouse_decided` was never called. A rejected driver was promised "reply here and we will find another one", then got "call your dispatcher". A driver delayed again after an approval could never get a new slot. **F2 missed it**: it checked that a slot lookup was attempted, not that slots were offered | `BookingService._flow` reads the appointment's status from the database when the stored state is SUBMITTED: CONFIRMED → BOOKED, anything else no longer pending → CLOSED. BOOKED may look for slots; the new request replaces the confirmed booking and goes to the warehouse like any other | `setuhaul/api/service.py` `_flow`, `setuhaul/domain/booking.py` `ALLOWED`; `test_f2_*` (now requires real slots), `test_h3_after_an_approved_booking_*` |
| H-1 (P2) | The rules extractor, which is also the production fallback when the model is down, ignored bare times given in answer to "what time will you reach?" ("12 baje", "12 ko", "1 pm"). It also ignored Hindi word order ("shaam 6 baje pahunch jaunga"), read "raat 10 baje" as 10:00, and missed "Mera late hai - 3 ghanta". The driver was asked the same question again and again | Hindi-order times with day parts, checked first. A bare time counts only when the agent's last question was about the arrival time (`asked`, from turn facts). A duration counts anywhere in a message that says "late" | `setuhaul/agent/rules_extractor.py`; `test_h1_*` in `test_agent_units.py` and `test_orchestrator.py` |
| H-5 (P2, owner decision) | After a hand-back the agent had no idea what the call settled, and the driver was not told they could write again | Migration 0022: `hand_back_thread` keeps the summary on the conversation, and in the same transaction stores a notice in the conversation's language and queues it on the normal outbound path. The agent gets the summary for 12 hours as **background in the model's context only**: never a tool argument, and it cannot book. Please check this boundary | `migrations/0022_handback_context.sql`, `Orchestrator._user_block`; `test_h5_*` (Hinglish, English) |

H-2 and H-4 were demo-console usability fixes, not product defects. Details are in `REVIEW_LOG.md` § Human end-to-end check.

## R3.2 response: shipment and dock changes to one confirmed booking

| ID | Fix (`migrations/0021_booking_compat_serialization.sql`) | Look at |
|---|---|---|
| R3.2-P1 | Compatibility changes to the same confirmed booking are serialised by one transaction-scoped advisory lock per dock ("booking compatibility of dock D"). A dock change takes its own dock's key. A shipment change takes the key of every dock holding a current CONFIRMED booking for it, in dock_id order. The second of two changes waits, then checks against the first one's committed values in a fresh READ COMMITTED snapshot, and raises BOOKING_AT_RISK if the booking no longer fits. The triggers take no row lock on the other table. The key comes after the trigger's own row lock and before the approval rows, so the order is thread → shipment → dock → compat key (dock_id order) → approval → appointment → claim. `decide_booking_request` takes no key; its SHARE locks from 0020 already serialise it with both changes. A compatibility change under REPEATABLE READ, which would keep the old snapshot, is refused (`COMPAT_CHANGE_NEEDS_READ_COMMITTED`) | `test_r32_shipment_and_dock_changes_to_one_booking_are_serialised_and_a_person_is_told` (shipment first, dock first): your 1,000 → 1,500 kg / 2,000 → 1,200 kg case, with the first change held open. Both cases **fail without 0021** (checked by running them with the migration removed). `test_r32_simultaneous_shipment_and_dock_changes_end_compatible_or_escalated` (8 simultaneous pairs: no deadlock; final state compatible or escalated), `test_r32_a_compatibility_change_under_repeatable_read_is_refused` |
| R3.2-a | The planner audit test compared the row with itself. It now asserts `("staff:plan1", "planner", "TAKE_OVER_THREAD")` and `("staff:plan1", "planner", "HAND_BACK_THREAD")` | `test_r31_a_planner_can_take_over_and_hand_back_at_their_facility` |
| R3.2-b | Removed the stale open item saying pause, takeover and handback take the audit principal from the console API (fixed in R3, migration 0019) | this file, *Still open* |

## R3.1 response: the two follow-up findings

| ID | Fix (`migrations/0020_compatibility_locks.sql`) | Look at |
|---|---|---|
| R3.1-P1 | One lock order for decisions and changes: thread → **shipment row → dock row** → approval → appointment → claim. A shipment or dock UPDATE takes its row lock and then (in its trigger) the approval rows. `decide_booking_request` now SHARE-locks the shipment and dock **before** the approval row and re-reads them under the lock. A change that got there first makes the decision wait, then refuse (`ALREADY_DECIDED`, request INVALIDATED). A change that arrives during a decision waits for it, so it is a change to a CONFIRMED booking. If that leaves the booking incompatible, the trigger raises a `BOOKING_AT_RISK` escalation; a confirmed booking is never cancelled silently. No other path locks a shipment or dock row: expiry, reject, withdraw and the sweeps don't, and `block_dock`'s foreign-key check takes only FOR KEY SHARE, which doesn't conflict | `test_r31_a_shipment_change_that_got_there_first_*`, `test_r31_a_change_that_arrives_during_the_decision_*` (shipment, dock), `test_r31_decisions_racing_shipment_changes_never_deadlock_*` (8 races) |
| R3.1-P2 | Planner may take over and hand back (B-WF-2 §8), in the database and the API. The audit row records the staff role itself; before, every takeover was recorded as `operations` | `test_r31_a_planner_can_take_over_and_hand_back_at_their_facility`, `test_r31_a_planner_at_another_facility_cannot_*` |

## R3 response: what changed since your review

All six R3 findings are fixed. The changes are in `migrations/0019_review_r3_fixes.sql`, `setuhaul/notify/outbound.py`
and `setuhaul/api/app.py`, plus the tests. The full row-by-row record is in `REVIEW_LOG.md` (R3) and the new section
at the end of `TRACEABILITY.md`.

| ID | Fix | Look at |
|---|---|---|
| R3-P1-01 | `close_approval` takes the appointment and claim locks, then reads `clock_timestamp()`. Past the deadline, a person's reject or withdraw is refused (`PENDING_EXPIRED`) and the sweep records EXPIRED. An invalidation or supersession is recorded as EXPIRED, with the requested outcome kept, so the change that caused it commits. Every path to EXPIRED raises the escalation | `test_r3_a_person_waiting_on_a_lock_past_the_deadline_*` (reject, withdraw), `test_r3_an_invalidation_waiting_on_a_lock_*`, `test_r3_a_dock_block_after_the_deadline_*` |
| R3-P1-02 | Destination, weight, dock type and refrigeration changes invalidate open requests. `decide_booking_request` re-proves compatibility (`booking_incompatibility`) and refuses a shipment outside the decider's scope. The occupancy guard re-runs when a hold becomes a claim | `test_r3_a_booking_critical_shipment_change_*` (4 fields), `test_r3_decide_re_proves_dock_compatibility_itself` (4 rules, invalidation trigger disabled), `test_r3_a_change_between_the_option_and_the_request_*` |
| R3-P2-01 | Takeover stops PENDING and RETRY replies. An UNKNOWN reply is not resent; it fails as `HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND` and the coordinator is told. `begin_send()` re-checks just before the provider call; the gap between that check and the HTTP call cannot be fenced | `test_r3_takeover_*` (3) |
| R3-P2-02 | A WhatsApp 5xx is AMBIGUOUS; only 429 and connection failures are retryable. A receipt that cannot be stored makes the webhook answer 503 | `test_r3_a_whatsapp_5xx_*`, `test_r3_a_receipt_that_cannot_be_stored_*` |
| R3-P2-03 | Pause, takeover, handback and dock block need `staff_context`, check the role in the database and record the principal from it. The console role lost its direct writes to the audit log, the outbox and the human flag | `test_r3_console_mutations_*`, `test_r3_the_console_cannot_write_*`, `test_r3_takeover_records_*` |
| R3-P2-04 | F7 scripts its timeout for its own driver only; F8 picks a slot the database judges inside hours. No `pytest.skip` remains in `test_flows.py` | `test_f7_*`, `test_f8_*` |

Your four answers are recorded as carried-forward requirements:
- `DEPLOYMENT_GATES.md` lists the five identity proofs and your step 6 order, with the warning about the single-DSN
  local pattern.
- `FINDINGS_LEDGER.md` carries the busier-time metric requirement and the duplicate monitoring.

**On reproduction:** the database tests need `psycopg` (`pip install -r requirements.txt`) and PostgreSQL 18 at
`SETUHAUL_TEST_DSN`. Without them, about 280 database-free tests run. The full 743-pass run is still
developer-reported until it is run on your side.

## How to read it

| Document | What it is |
|---|---|
| `TRACEABILITY.md` | Requirement → test, with a section per business flow. `tests/test_traceability.py` fails if a named test disappears |
| `FINDINGS_LEDGER.md` | Every finding from R0.7 to R2.1 and the design reviews, with status and evidence. Open and step 6 items are carried forward |
| `docs/2-design/B-WF-2_approval_design.md` | The approved approval boundary (revision 3), plus the rule rows (§20) |
| `docs/2-design/B-WF-3_outbound_design.md` | Outbound delivery semantics and their limits |
| `DEPLOYMENT_GATES.md` | The six gates the booking switch needs, which only AWS can satisfy |
| `REVIEW_LOG.md` | Every earlier review and response |

To run the tests:

```bash
python -m pytest tests -q
```

This takes about 11 minutes with PostgreSQL at `SETUHAUL_TEST_DSN`. Without a database, the database-free tests run
on their own (about 280).

## The flows

### F1–F2 · A late driver gets a slot; the warehouse decides

Driver message → signed webhook → durable receipt → queue → orchestrator (lease, fence) → extraction (scripted) →
feasibility → options → choice → read-back → the driver's YES, attested by the inbound path → hold →
`submit_booking_request` → a person decides → the driver is told.

| Look at | Where |
|---|---|
| The agent only asks | `submit_booking_request` (0016, built from 0015). The YES is bound to message, driver, shipment, conversation, slot and selection, and used once (`verify_attestation`, 0015) |
| A person decides | `decide_booking_request` (0015): `staff_ctx()`; the role per kind (`approval_role_rules`); every invariant re-proven; `clock_timestamp()` after the locks |
| Trusted identity | `begin_staff_context` (console only), `staff_context` keyed by transaction; `Db.tx(..., staff=)`; `BookingService._staff` |
| Driver told | `ApprovalDecided` in the same transaction → `enqueue_notifications` → `OutboundRelay` (0017/0018) |
| Tests | `test_flows.py::test_f1_*`, `test_f2_*`; `test_approvals.py`; `test_db_requests.py` |

### F3–F4 · A request that can no longer be honoured never stays open

| Look at | Where |
|---|---|
| One deadline for the request and its claim | `submit_booking_request`: `least(clock + TTL, slot start − lead time)`; the hold becomes the claim, with no expiry of its own |
| Commit-time invariant | `check_approval_consistent` and three deferred constraint triggers (0015) |
| Expiry | `expire_pending_requests`, `expire_approval` (workflow timeout); `LocalApprovalWorkflow.tick`; ASL in `setuhaul/workflow/approval_workflow.asl.json` |
| Invalidation in the same transaction | `block_dock` → `invalidate_open_requests`; shipment, dock and hours triggers; `close_approval` |
| Tests | `test_f3_*`, `test_f4_*`; `test_approvals.py::test_d_*`, `test_s14_*`, `test_commit_time_check_*` |

### F5 · Safety and contradictory data always go to a person

| Look at | Where |
|---|---|
| Person-always rules, whatever the rule order | 0016: `f_safety` / `f_contra` → `REQUIRES_PERSON`; nothing is created |
| The hand-over message reaches the driver | 0018: `chat_threads.human_active_since`; `claim_notifications` suppresses only replies stored before the takeover |
| Tests | `test_f5_*`; `test_confirmation_policy.py::test_person_always_*` |

### F6–F7 · Duplicates, crashes and lost answers

| Look at | Where |
|---|---|
| Inbound: one turn per message | 0011–0013 (unchanged since R2.1) |
| Outbound: at least once, at most one same-id resend | 0017: `claim_notifications`, `record_send_result` (fenced by attempt number), `record_delivery_status`, `reconcile_notifications`. Stable id = `md5(event:channel:recipient)`. `WhatsAppCloudProvider.supports_idempotency = False`; our id travels as `biz_opaque_callback_data` |
| The stored reply is sent (S4-R2-07) | The notification carries only the message id and hash; the body is read from the row and checked (`CANONICAL_MISMATCH`). Only turn replies are sent (0018) |
| Tests | `test_f6_*`, `test_f7_*`; `test_outbound.py` (lost answer with an idempotent and a non-idempotent provider; no third send; stale worker) |

### F8 · Deterministic policy confirmation (B-WF-2a), off by default

| Look at | Where |
|---|---|
| Rule rows | `install_confirmation_rules` (0016); design §20. Every row ships NEEDS_PLANNER or PERSON_ALWAYS (owner decision Q1) |
| Policy confirmation | Needs `auto_eligible`, `authorised_by` and no risk flag. Evidence is stored on the appointment. It is never an approval row and never a person |
| Override rate | `policy_rule_outcomes`; `GET /console/policy/outcomes` |
| Tests | `test_f8_*`; `test_confirmation_policy.py` (one test per rule row) |

### F9 · Concurrency

Two coordinators at once give one outcome and one message (`test_f9_*`). The approval races, approve against approve,
reject, withdraw, a block and the expiry sweep, give exactly one outcome and no deadlock (`test_approvals.py`). Fifty
drivers racing for one slot are in `test_api.py`.

## Defects the flow tests found (fixed in 0018)

- **The safety hand-over message was suppressed.** 0017 suppressed every unsent agent reply once a person had taken
  over, including the "a coordinator will call you" reply stored just after the hand-over.
- **The read-back went out twice:** once as the evidence row and once as the turn reply. The evidence row also raised
  a false NOTIFICATION_FAILED.
- **An in-turn policy confirmation was announced twice:** by a template and by the agent's reply.

## Questions for you

1. **Identity boundary (B-WF-2 §15).** The console role plus `begin_staff_context` plus a per-transaction row: is that
   enough inside the database? Is "the console login maps only to `setuhaul_console`" the right step 6 acceptance
   test?
2. **Delivery semantics (T51).** With WhatsApp, which has no idempotency key, is "at most one identical repeat after an
   ambiguous send" acceptable, given that a receipt usually settles the send first?
3. **"Busier time" (B-WF-2a).** It compares active occupancy at the facility now with the new time. Is that measure
   good enough for the planner rule?
4. **What should step 6 prove first?** The gates, in the order you would want evidence for them.

## Still open (carried forward in `FINDINGS_LEDGER.md`)

- **Step 6 (AWS):**
  - real SQS (DLQ, least privilege), Aurora, Step Functions, Cognito and WhatsApp;
  - the pipeline and console logins mapped to their database roles;
  - drift detection against the deployment manifest;
  - approved WhatsApp templates;
  - B-AWS-0, the AWS account and model access.
- **Owner:**
  - the native-speaker Hinglish check and the B-AI-3 human review (S4-R2-08);
  - whether any safe-case rule should confirm automatically (GGN night is not switched on).

---

*Earlier step 4 rounds:*

*Step 4, round 4:*

**S4-R3-01 and S4-R3-02 are fixed, and so are the three smaller points (§3, §4, §5).** Migration 0014, decision T48; `REVIEW_LOG.md` § R2.1. 478 tests pass.

Please check:
1. **S4-R3-01:** `migrations/0014_evidence_freshness.sql` and `test_s4_r3_01_*`.
2. **S4-R3-02:** `manifest_problems`, `deployment_id_for`, `manifest_fingerprints` and `check_booking_preconditions` in `setuhaul/api/inbound.py`; `DEPLOYMENT_GATES.md`. Is a manifest-derived deployment id, plus drift detection in step 6, the binding you meant?
3. **§4:** `SqsFifoQueue._dead_letter`.

---

*Step 4, round 3:*

**S4-R2-01…04 and -06 are fixed; -05 has its writer role and waits for step 6; -07 belongs to B-WF-3; -08 is with the owner.** Migration 0013, decision T47; `REVIEW_LOG.md` § R2.0. 473 tests pass.

Please check:
1. **S4-R2-01:** `finish_turn` in `setuhaul/api/service.py` and `TurnLeaseLost` in `Orchestrator.handle`.
2. **S4-R2-02/03:** `legacy_uncertain_receipts` and `reopen_receipts`; `RUNBOOK_0011.md` step 4.
3. **S4-R2-04/05/06:** `migrations/0013_deployment_evidence.sql`, `check_booking_preconditions`, `DEPLOYMENT_GATES.md`. Is a 30-day maximum and a per-deployment binding the right freshness rule?

---

*Step 4, round 2:*

**Every R1.9 finding is addressed, except the native-speaker check (S4-09) and the human review itself (S4-10).** Migration 0012, decision T46; `REVIEW_LOG.md` § R1.9. 467 tests pass. The booking switch stays off, and now cannot be turned on without the step 6 evidence.

Please check:
1. **Fencing (S4-02/S4-03):** `assert_turn_fence()` in 0012, `TURN_FENCE` in `setuhaul/api/db.py`, `Orchestrator.handle` (heartbeat, generation), and the three live-worker tests in `tests/test_inbound_delivery.py`.
2. **S4-01:** the webhook's split between malformed, not stored (503) and stored but not published (200).
3. **S4-05:** `check_booking_preconditions` and `SqsFifoQueue.verify`. Are four gates the right set, and is "the pipeline writes `deployment_gates` with its own credentials" enough evidence?
4. **S4-04:** `RUNBOOK_0011.md` and the uncertain-receipt rule (no processing claim ever taken, no agent message afterwards).
5. **S4-06:** the 7-day search window for the next operating day.

---

*Step 4, round 1:*

**The four findings that gate turning on `SETUHAUL_BOOKING_ENABLED` are closed in code.** Migrations 0010–0011, decision T45; `REVIEW_LOG.md` § R1.8. 455 tests pass.

Please check:
1. **`migrations/0010_bound_shipment_under_lock.sql`:** the driver check in `bind_thread_shipment()`, `resolve_thread_shipment()`, and the `shown_options` binding trigger (it takes the conversation lock inside the feasibility role's insert).
2. **`migrations/0011_inbound_outbox.sql` and `setuhaul/api/inbound.py`:** claim → send → mark published; the processing lease in `Orchestrator.handle`; the recovery thresholds (30 s unpublished, 6 min stalled); the generation bump when a retry recovers an attestation.
3. **`tests/test_inbound_delivery.py`** against the acceptance criteria in `08_signoff_and_backlog.md` step 4.
4. **Whether the booking switch can be turned on** before SQS has run in AWS, or only after step 6.

---

*Step 3, round 9:*

**A truck ready late at night is now offered the next morning's slots instead of being escalated.** Decision T44; `REVIEW_LOG.md` § R1.7.

Please check:
1. **`domain/feasibility.py::find_options`:** the per-slot day rules (close, 21:00 last new start) and the horizon (the ready day plus the next).
2. **The horizon choice:** is "the next operating day" right, or should a planner see more?
3. **`tests/test_feasibility.py::test_b_feas_1_*`**, and the removed time-of-day skip in `test_api.py`.

---

*Round 8:*

**Both language gaps found by the B-AI-3 checks are fixed in code (decision T43). The templates now pass every check on 20/20 situations.** No spend. Details: `REVIEW_LOG.md` § R1.6, `evals/b_ai_3/README.md`.

Please check:
1. **The Hinglish templates** (`agent/replies.py`, `HL` and `HL_ASK`): natural enough for drivers? A native speaker's read would help.
2. **The language rule** (`replies.reply_language`): Devanagari means Hindi; a short reply keeps the conversation's language; otherwise the extractor's language stands.
3. **The decision:** with the gaps closed, remove Sonnet from option replies unless the human review (`evals/b_ai_3/review_sheet.md`) finds a difference that matters.

---

*Round 7:*

**Templates vs Sonnet, without spending.** A phrasing model touches only option lists. On 20 real option replies, the templates pass grounding, completeness and policy 20/20; they fall short on language (19/20) and Hinglish register (17/20), and both gaps can be closed in code at no cost. Details: `evals/b_ai_3/README.md`, `REVIEW_LOG.md` § R1.5.

Please check:
1. **The checks** (`evals/phrase_ablation.py::checks`, `tests/test_phrase_checks.py`): anything a reviewer would expect code to catch that they miss?
2. **The proposed decision:** add Hinglish templates and a conversation-level language, then remove Sonnet from option replies, unless the human review finds a difference that matters.
3. **Stage 5:** the review sheet `evals/b_ai_3/review_sheet.md` is ready for a human reviewer; the Sonnet column fills only if the Sonnet arm is approved (≤ $0.12).

---

*Round 6:*

**Phase 1 of your cost roadmap is in place. It spent nothing.** Details: `REVIEW_LOG.md` § R1.4, decision T42, `COSTS.md`.

Please check:
1. **`evals/budget.py`.** The cap and approval rules, the per-call reserve (can anything bypass it?), and the cache key, especially the normalisation of option ids and dates, and the remapping of a cached pick.
2. **The backfill from recorded p3 outputs.** A full G3 v4 or h1 rerun now costs $0.00, and a full live replay is refused without approval. Is reusing recorded outputs acceptable as Level 1 evidence?
3. **`COSTS.md`.** Is the measured, computed and inferred distinction clear enough?

Proposed next, both needing your go-ahead because they involve spend: B-COST-2 (compact output plus batch, measured on a new held-out h2), and B-AI-3 stages 1–5 (free: deterministic reply checks plus a 20-situation human review sheet).

---

*Round 5:*

**The held-out set exists and p3 holds up on it.** Haiku 4.5 (p3) scores 158/167 (95 %) on `evals/golden/h1/`, with every gate passing; the base G3 score is 73/76, and the rules baseline gets 99/167. Details: `evals/golden/h1/README.md`, `REVIEW_LOG.md` § R1.3.

Please check:
1. **Independence** (`evals/make_heldout.py`). The generator saw only messages and plain facts, never the prompt or labels; labels are inherited by code; the set was frozen before evaluation. Is that independent enough? Its known limit is that the variants are rewrites of G3 situations, not new situations.
2. **The review** (`generation.json`, `h1/README.md`): 2 variants dropped, 3 drifted ones kept on purpose (P02, ADV07, P01). A human spot-check of the variants is welcome.
3. **The recorded misses** (THR011, BB02): fix now in a p4, measured on a new h2, or leave until real driver messages exist?

Step 3 status: B-AI-1 extraction ✅ (G3 and held-out); B-EV-2 ✅ (scripted gate, live 61/64). Still open: B-AI-3 (templates vs Sonnet, with the judge) and B-AWS-0 (India Bedrock account).

---

*Round 4:*

**B-EV-2 is in place.** Scripted replay: 64/64 scenarios, zero hard-gate violations, run on every commit as `tests/test_replay.py`. Live Haiku: 61/64, zero hard-gate violations. The replay found 12 bugs; all are fixed. Details: `evals/golden/v4/README.md`, `REVIEW_LOG.md` § R1.2, decision T41. 400 tests pass.

Please check:
1. **The fixture** (`evals/replay.py` docstring and `Fixture`): the seed is moved by whole days to tomorrow, the service runs on the scenario clock, and there are two replay-only database changes (an ETA-stamping trigger and 24-hour shifts). Is the time-shift approach sound?
2. **T41:** the content-duplicate rule (longer than three words, 2 minutes) and order references exempt from masking (`api/service.py::ingest`).
3. **The behaviour changes:** a status question with no booking offers slots; a cancelled shipment is told to contact dispatch; another driver's order is refused (`agent/orchestrator.py`).
4. **G3 v4 label corrections**, especially THR002 and AT09 (the data has no slot) and THR008 / AT01 (seed bookings refused at load).

Next: B-EV-1b (held-out variants), then the templates-vs-Sonnet judge ablation (B-AI-3).

---

*Round 3:*

**OD-1 and OD-2 are implemented together, with the boundary tests you specified.** Details: `REVIEW_LOG.md` § R1.1; decisions T39 and T40 in `docs/2-design/01_decision_register.md`.

Please check:
1. **OD-1:** `domain/feasibility.py` (the start-plus-grace check is removed; `is_no_show`) and `tests/test_feasibility.py::test_od1_*`.
2. **OD-2:** `migrations/0009_breakdown_escalation.sql`, `agent/extraction.py` (`SAFE_PLACE`, the danger words, `Checked.breakdown`), `agent/orchestrator.py` (`_breakdown`, `_safety_answer`), and `tests/test_orchestrator.py::test_od2_*`, including takeover and hand-back.
3. **G3 v3** (`evals/golden/v3/README.md`): the seed finding that SHP1017 has no slack (L01, L02 and ADV02 relabelled), the H04 change, and the 7-scenario policy group. Haiku p3: 71/74, every gate passes.

Next, in your order: B-EV-2 (end-to-end replay of G3 through the orchestrator and database), then B-EV-1b (held-out set).

---

*Round 2:*

**All four code findings are fixed. Two policy questions are with the owner.** Full table: `REVIEW_LOG.md` § R1.0. 371 tests pass.

Please check:
1. **S3-01:** `orchestrator.py`, the `constraint-before-select` branch, and `tests/test_orchestrator.py::test_s3_01_*`.
2. **Latest-finish start vs end:** `extraction.py::kind_stated` and its 11 test cases.
3. **Residency, fail-closed:** `evals/providers.py` (the switch now lives outside the deployed package), `models.py::production_model`, and the provider tests in `tests/test_agent_units.py`. They include a fresh-process check that no deployed module loads the eval providers.
4. **S3-02:** `tests/test_feasibility.py::test_s3_02_*` pins today's at-risk boundaries; the behaviour is unchanged pending the owner (OD-1).
5. **G3 v2** (`evals/golden/v2/README.md`): only BB10 changed; v1 is frozen.

Not in this round: B-EV-2 (end-to-end replay) and B-EV-1b (held-out set) are next, in your recommended order; OD-1 and OD-2 await the owner.

---

*Round 1:*

**Scope:** what changed since round 4 (GO for step 3 with the booking switch off). 332 tests pass (local PostgreSQL 18.6). The live model runs are **not** in this round: the AWS account cannot call Claude yet (see "Not in this round").

## What changed
1. **Agent pipeline** (`setuhaul/agent/`): the model is used in two places only. Haiku fills `record_driver_report` with a forced tool call, and Sonnet may phrase options, checked for grounding. Code checks every value, chooses every tool, and falls back to rules and templates when the model fails. Every REQUIRES_HUMAN becomes a durable escalation before a coordinator is promised (closes R0.9 B2-18). Proven with a scripted model through the real API and database in `tests/test_orchestrator.py`.
2. **Golden dataset G3** (`evals/golden/v1/`): 60 scenarios in the signed-off composition, with frozen clocks, expected extraction, outcome, required and forbidden tools, end state, hard-gate and rubric labels. `evals/run_golden.py` grades extraction and records the dataset hash. Rules-only baseline: 51/66 turns fully right, ETA recall 0.82 and intent accuracy 0.76, both below the 0.95 gate.
3. **Four findings from G3, fixed:**
   - The safety words no longer fire on cargo names.
   - A delay that still fits keeps the slot.
   - A bare delay proposes the computed arrival and asks for a YES (decision D1).
   - Facts from earlier turns are kept in the database through **migration `0008_turn_facts.sql`**. Only `note_turn_facts()` writes them: it checks the driver owns the thread, takes the conversation lock, accepts fixed keys and a capped size, and the facts expire after 2 h. The agent cannot write the table.
4. **Bedrock spike:** `scripts/spike/bedrock_spike.py`. B1 is done: the India profiles exist and route only to ap-south-1 / ap-south-2.

## Please check
1. `migrations/0008_turn_facts.sql` and `tests/test_agent_not_operator.py`: does the new function keep the agent a requester, not an operator?
2. **The G3 labels.** They are my reading of the design. In particular:
   - THR001, THR006 and THR007 escalate as CONTRADICTORY_DATA, because the drivers are double-assigned (F3).
   - BB10 expects no question for "leave by 9".
   - L01, L02 and ADV02 expect the slot to be kept.
3. **The D1 change (UX).** A bare delay ("late by 45 minutes") now costs the driver one YES before options. The alternative is to assume the time and state it in the options reply. Which do you prefer?
4. `evals/golden/v1/README.md`: findings, fixes and the revision log (v1 was revised once, before this review).

## Model results (added after the first draft of this request)
The AWS account cannot call Claude yet (B-AWS-0), so the runs went through **OpenRouter** on synthetic data, behind a provider switch (decision **T38**). Production stays India-only: OpenRouter is refused unless the run is explicitly marked synthetic-only, and always in production.
- **G3 extraction:** Haiku 4.5 with prompt p2 got **65/66** turns fully right, with every gate at 1.00. The rules baseline got 51/66 and fails ETA recall and intent accuracy. The untuned p1 got 58/66 and failed validity. Each p2 change is tied to a run-1 failure in `evals/golden/v1/README.md`.
- **Bug found:** Sonnet 5 rejects `temperature`, and the Bedrock client sent it. Fixed in both clients and tested; phrasing drafts now pass the grounding check 3/3.
- **Please check:**
  - the code check on latest-finish wording (`extraction.py`, `_KIND_WORDS`);
  - the H04 label change (a breakdown not yet fixed escalates as a safety case, per Phase 1);
  - the AT06 label;
  - that p2 was tuned on G3, so a held-out set is still needed (B-EV-1b).

## Not in this round
- **The India Bedrock runs.** The AWS account is locked to ap-southeast-2, which has no `in.` profiles, and Claude access is not enabled there (B-AWS-0).
- The templates-vs-Sonnet judge ablation.
- The end-to-end G3 replay runner (B-EV-2).
- B2-16 and B2-17 (deferred, they gate live booking).
- Step 4 items.

---

*Step 2, round 4:*

**Round 4 answers R0.8.** B2-13 (shipment switching) is fixed in one locked function, with paused-transaction tests in both orders; release and hand-back now take the lock; the lock key is 64-bit; a production booking switch exists and is OFF by default. B2-14 and B2-15 are in the step 4 backlog and gate the switch. Full response: `REVIEW_LOG.md`. 256 tests and 88 checks pass (local PostgreSQL 18.6).

---

*Round 3:*

**Round 3 answers R0.7.** The P0 concurrency finding is fixed with a per-conversation lock on every operation that orders a conversation, proven by `tests/test_concurrency.py` (each test pauses one transaction mid-way). The step 4 enqueue-claim item is also done. Full response: `REVIEW_LOG.md`. 251 tests and 88 checks pass (local PostgreSQL 18.6).

---

*Round 2:*

**Round 2 answers your review of 0004.** All four items are done: P0-1 attestation freshness, P0-2 separate inbound and feasibility roles, P1-1 webhook-retry recovery plus reconciler, P1-2 central escalation events. See `README.md` §"Step 2 final corrections" and `migrations/0005_attestation_freshness_and_ingest_role.sql`. 245 tests and 88 validator checks pass (local PostgreSQL 18.6). One consequence to confirm: after a YES, any further driver message needs a fresh YES.

---

*Round 1 request (kept for reference):*

**Scope:** `phase3_build/` (deterministic backend, before any model or AgentCore integration).
**Asked:** confirm the step 2 review corrections are complete and correct, and that no new direct write path for the agent exists.

## What changed since the last review
- **Migration 0004** (forward-only; 0001 is byte-identical to the frozen Phase 2 schema, 0002–0003 untouched).
- Service and API changes in `setuhaul/api/service.py` and `setuhaul/api/app.py`.
- Each finding, its fix and its tests are listed in `README.md` § "Step 2 review corrections".

## Where to look first
1. `migrations/0004_step2_review_fixes.sql`: `record_read_back`, `attest_confirmation` (system role only), `verify_attestation`, the new `hold_option` / `submit_booking_request` wrappers, `record_driver_eta`, `escalate_thread`, `take_over_thread`, `hand_back_thread`, the central queue policies, and section E (narrower grants).
2. `tests/test_agent_not_operator.py`: the agent's complete allowlist of tables, columns and functions.
3. `tests/test_db_requests.py` and `tests/test_api.py`: the tests for P0-1…P0-3 and P1-4…P1-8.

## Evidence
- 227 Build tests pass; the Phase 2 validator passes 88/88.
- **Local community PostgreSQL 18.6 only.** Aurora PostgreSQL 18.3+ through the RDS Data API is gate G1, in step 6.

## Known open items (by design)
- Planner override with a written reason: step 4 (human-in-the-loop workflow).
- The feasibility recheck and the decision run as two transactions; the database re-checks capacity at decision time.
- Console roles are enforced by the application; facility scope is enforced by the database.

## Questions for the reviewer
1. Is attestation by the inbound (system) path the right trust boundary, given that the tool service also uses the read-only system role for feasibility?
2. Is the CENTRAL queue (NULL `facility_id` plus scope token) acceptable, or should it be a first-class queue table?
3. Anything that should block the start of step 3 (model integration)?
