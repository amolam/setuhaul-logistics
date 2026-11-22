# Consolidated findings ledger

One line per finding from the Phase 3 build reviews (R0.7 to R3.2) and the B-WF-2 design reviews. Each line gives
its status and evidence; open items carry forward until they close. `REVIEW_LOG.md` has the full text of each review
and response. Phase 2 findings were closed at Phase 2 sign-off (`../docs/2-design/06_review_response.md`).

**Status key:**
- **Closed:** fixed and covered by a test that runs locally.
- **Step 6:** can only be proven on real AWS, as part of a deployment gate (`DEPLOYMENT_GATES.md`).
- **Open:** waiting on build work or the owner.

**Live booking stays NO-GO until step 6.** `SETUHAUL_BOOKING_ENABLED` is off in every environment.

## Open and carried forward

| ID | Finding | Owner / where | Status |
|---|---|---|---|
| S4-R2-08 / S4-09 / S4-10 | Native-speaker Hinglish check and B-AI-3 human review | Owner | Closed 21 Nov 2026: reviewed and applied (REVIEW_LOG "Native-speaker Hinglish and Hindi review"); the redrafted Hinglish deadline question and the unchanged "slot was taken" line approved by the reviewer. "Talk to a person" split out as S4-R2-08b (deferred) |
| S4-R2-05 | The pipeline's IAM identity is mapped to `setuhaul_deploy_evidence` and nothing else | WORKLOAD_IDENTITY gate | Step 6 |
| S4-R3-02 (part) | Drift detection: live AWS resources compared with the deployment manifest | WORKLOAD_IDENTITY, BOOKING_WORKFLOW gates | Step 6 |
| R2.1 §4 | SQS least privilege for the relay and worker; DLQ redrive procedure tested live | AWS_QUEUE gate | Step 6 |
| R2.1 §3 | Database activity auditing; no workload uses the owner role | AURORA gate | Step 6 |
| B-WF-2 §15 | The console workload's login maps to `setuhaul_console` alone; real Cognito groups | WORKLOAD_IDENTITY gate | Step 6 |
| B-WF-2 §9 | Real Step Functions `waitForTaskToken`, timeouts and callbacks; EventBridge Scheduler | BOOKING_WORKFLOW gate | Step 6 |
| B-AWS-0 | AWS account locked to ap-southeast-2; Claude on Bedrock not authorised | Owner / AWS | Open |
| B-WF-3 | Real WhatsApp sends, receipts and timeouts; duplicate rate after ambiguous sends; monitoring of undelivered notifications | OUTBOUND_DELIVERY gate | Step 6 |
| B-WF-3 | Proactive driver texts must become Meta-approved template messages; Hinglish variants wait for S4-R2-08 | Step 6 / owner | Open |
| B-WF-2a | Backlog line "start GGN night shift on automatic for the safe cases" conflicts with Q1; not applied. Needs an explicit owner decision naming the rule versions to switch on | Owner | Open |
| R3 Q1 | Step 6 identity proof, five points: console login maps only to `setuhaul_console`; no other workload can assume it; none can assume `setuhaul_rpc`, the owner or a superuser; the Data API transaction keeps the staff context; an operations user cannot create an ops_lead context. The local single-DSN `SET LOCAL ROLE` pattern must not become a shared production credential | WORKLOAD_IDENTITY gate | Step 6 |
| R3 Q3 | "Busier time" counts overlapping rows. Before any POLICY_CONFIRM rule relies on it: peak occupancy against usable capacity, kept in the evidence | Before any automatic rule | Open |
| R3 Q2 | Monitoring of ambiguous sends and visible duplicates; business sign-off on the residual repeat | OUTBOUND_DELIVERY gate / owner | Step 6 |
| R3 | The reviewer could not run the database tests; the full-suite result is developer-reported | Next review | Open |

## Closed

| Round | ID | Finding | Closed by | Evidence |
|---|---|---|---|---|
| R0.8 | B2-13 | Shipment switching not serialized | 0007 | `test_concurrency.py` |
| R0.8 | P2 | 32-bit advisory-lock key | 0007 | `test_conversation_lock_uses_a_64_bit_key` |
| R0.9 | B2-16 | `bind_thread_shipment()` did not check that the conversation belongs to the driver | 0010 | `test_concurrency.py` (B2-16) |
| R0.9 | B2-17 | Shipment switch decided from a pre-lock read | 0010 | `test_concurrency.py`, `test_api.py` (B2-17) |
| R0.9 | B2-14 | Durable inbound delivery | 0011 | `test_inbound_delivery.py` |
| R0.9 | B2-15 | Concurrent HTTP webhook test | 0011 | `test_inbound_delivery.py` (4-way HTTP) |
| R0.9 | B2-18 | Booking tools with the switch off | Service | `test_api.py` |
| R1.0 | S3-01, S3-02, latest-finish, residency | Step 3 extraction and residency findings | Step 3 round 2 | `REVIEW_LOG.md` § R1.0 |
| R1.9 | S4-01 … S4-08 | Webhook storage, lease fencing, generation, 0011 cutover, switch on LocalQueue, operating day, audit principal, duplicate reply | 0012 | `test_inbound_delivery.py`, `test_feasibility.py`, `test_concurrency.py` |
| R2.0 | S4-R2-01 … 04, 06 | Expired lease finishing, cutover checker, reopen guard, deployment evidence, six gates | 0013 | `test_inbound_delivery.py` |
| R2.0 | S4-R2-05 (role) | Dedicated evidence writer role | 0013 | `test_deploy_evidence_role_can_only_insert_evidence` |
| R2.1 | S4-R3-01 | Future-dated evidence | 0014 | `test_s4_r3_01_future_dated_evidence_is_refused` |
| R2.1 | S4-R3-02 | Gates bound to an immutable manifest; per-gate validity windows | 0014 + service | `test_s4_r3_02_*` |
| R2.1 | §3, §4, §5 | Trigger wording and start-up check; DLQ verification; import probe | 0014 + service | `test_r2_1_*`, `test_the_deployed_package_contains_no_non_india_client` |
| B-WF-2 rev 1 | Q1–Q4 | Policy rules closed by default; role per kind; pause vs hold; withdrawal | 0015 | `test_approvals.py::test_q1_*`, `test_q2_*`, `test_q3_*`, `test_q4_*` |
| B-WF-2 rev 2 | A | The driver's YES bound to message, driver, shipment, conversation, slot, version; used once | 0015 | `test_a_the_yes_is_used_once_and_bound_to_driver_and_slot` |
| B-WF-2 rev 2 | B | The database re-proves every booking invariant; the service check is advisory | 0015 | `test_b_*`, `test_commit_time_check_*` |
| B-WF-2 rev 2 | C | A durable `ApprovalDecided` event with each outcome; notification never depends on the workflow | 0015 | `test_c_the_outcome_and_its_event_exist_with_the_workflow_stopped` |
| B-WF-2 rev 2 | D | Requests invalidated when their capacity or slot is lost | 0015 | `test_d_*` |
| B-WF-2 rev 2 | E | One deadline for the claim and the request | 0015 | `test_submit_creates_one_open_approval_*`, `test_commit_time_check_*` |
| B-WF-2 rev 3 | §14 | Deadlines decided with `clock_timestamp()` after locks | 0015 | `test_s14_a_transaction_that_started_before_the_deadline_cannot_approve_after_it` |
| B-WF-2 rev 3 | §15 | Trusted staff identity; spoofing refused | 0015 | `test_s15_*` |
| B-WF-2 rev 3 | §16 | Lock order; races give exactly one outcome and no deadlock | 0015 | `test_races_resolve_to_exactly_one_outcome`, `test_race_approve_vs_the_sweep_at_the_deadline` |
| B-WF-2 rev 3 | §17 | One `ApprovalDecided` per approval; idempotent consumers | 0015 | `test_s17_*` |
| B-WF-2 rev 3 | §18 | Audit principal for every outcome | 0015 | `test_approve_*`, `test_reject_*`, `test_expiry_*`, `test_d_*`, `test_q4_*`, `test_q1_a_policy_*` |
| B-WF-2 rev 3 | §19 | Policy evidence: rule id, version, inputs, attestation, time | 0015 | `test_q1_a_policy_confirmation_is_never_an_approval_and_keeps_its_evidence` |
| B-WF-2 (found in build) | — | The system role could set `approval_requests.status` directly (a 0001 grant) | 0015 revokes it | `test_s15_no_other_role_can_manufacture_an_ops_lead_or_approve[setuhaul_system]` |
| R2.0 | S4-R2-07 | The outbound sender sends the stored reply, never a regenerated one | 0017 (locally; live in step 6) | `test_s4_r2_07_the_stored_reply_is_what_is_sent_and_a_changed_row_is_refused` |
| B-WF-3 | Backlog | Durable outbound delivery: stable ids, retries with backoff, ambiguity without blind resends, reconciliation, provider idempotency where supported | 0017 | `tests/test_outbound.py` |
| B-WF-3 (found in build) | — | The reply hash covered the full text while only 2,000 characters are stored, so a long reply could never be verified | Service: hash the stored text | `test_s4_r2_07_*` |
| Package check (found) | — | One 1-second-lease timing test failed once when the suite ran from the zip on a loaded machine (TURN_FENCED). The fencing behaved correctly; the test's timing margin was too thin | Lease-timing tests use 2–3 s leases | `test_inbound_delivery.py::test_s4_02_*`, `test_s4_r2_01_*` |
| Flow tests (found) | — | A safety hand-over suppressed the driver's "a coordinator will call you" message (0017 suppressed every unsent reply once a person took over) | 0018: suppress only replies stored before the takeover time | `test_f5_*`, `test_a_person_taking_over_stops_an_unsent_agent_reply` |
| Flow tests (found) | — | The read-back went to the driver twice (evidence row and turn reply), and the evidence row raised a false NOTIFICATION_FAILED | 0018: only turn replies are sent | `test_f1_*` |
| Flow tests (found) | — | A policy confirmation told the driver twice (template and in-turn reply) | 0018: no template for in-turn confirmations | `test_f8_*` |
| B-WF-2a | Backlog | Per-facility rule table: person-always, planner and safe-case rows; one test per row; override-rate metric | 0016 | `tests/test_confirmation_policy.py` |
| B-WF-2a (found in build) | — | A low-priority-number rule could shadow the person-always rows | 0016: safety and contradictory data refuse regardless of rule order | `test_person_always_holds_even_when_a_rule_would_shadow_it` |
| R3 | R3-P1-01 | Reject, withdraw and invalidation decided the deadline before their locks | 0019 | `test_r3_a_person_waiting_on_a_lock_past_the_deadline_cannot_reject_or_withdraw`, `test_r3_an_invalidation_waiting_on_a_lock_past_the_deadline_records_expired` |
| R3 | R3-P1-02 | Shipment destination, weight, dock type or refrigeration changes left a request approvable | 0019 | `test_r3_a_booking_critical_shipment_change_invalidates_the_open_request`, `test_r3_decide_re_proves_dock_compatibility_itself` |
| R3 | R3-P2-01 | Takeover did not stop RETRY or claimed replies | 0019 | `test_r3_takeover_after_a_retryable_failure_stops_the_retry`, `test_r3_takeover_after_the_claim_is_fenced_before_the_provider_call` |
| R3 | R3-P2-02 | WhatsApp 5xx retried blindly; unstored receipts acknowledged | `outbound.py`, `app.py` | `test_r3_a_whatsapp_5xx_is_ambiguous_and_a_429_is_retryable`, `test_r3_a_receipt_that_cannot_be_stored_is_not_acknowledged` |
| R3 | R3-P2-03 / B-WF-2 limit | Pause, takeover, handback and dock block not bound to `staff_context` in the database | 0019 | `test_r3_console_mutations_check_role_and_principal_in_the_database` |
| R3 | R3-P2-04 | F7 and F8 could skip | `test_flows.py` | no `pytest.skip` in `test_flows.py` |
| R3.1 | R3.1-P1 | Decision read shipment and dock unlocked; a concurrent change could commit after the confirmation unseen | 0020 | `test_r31_a_change_that_arrives_during_the_decision_waits_and_a_person_is_told`, `test_r31_decisions_racing_shipment_changes_never_deadlock_and_never_approve_silently` |
| R3.1 | R3.1-P2 | Planner excluded from takeover and handback | 0020 | `test_r31_a_planner_can_take_over_and_hand_back_at_their_facility` |
| R3.2 | R3.2-P1 | Concurrent shipment and dock changes to one confirmed booking each checked the other's old row; incompatible booking with no escalation | 0021 | `test_r32_shipment_and_dock_changes_to_one_booking_are_serialised_and_a_person_is_told` (shipment first, dock first), `test_r32_simultaneous_shipment_and_dock_changes_end_compatible_or_escalated`, `test_r32_a_compatibility_change_under_repeatable_read_is_refused` |
| R3.2 | R3.2-a | Planner audit test did not assert the role, nor the handback row | `test_approvals.py` | `test_r31_a_planner_can_take_over_and_hand_back_at_their_facility` |
| R3.2 | R3.2-b | Stale open item in REVIEW_REQUEST.md (audit principal not from staff_context; fixed in R3) | `REVIEW_REQUEST.md` | n/a (document) |
| R3.2 | R3.2-H1 (open, hardening) | Bulk admin transactions updating several shipments/docks must take booking-compat keys in one global dock_id order per transaction | — | not a release blocker; no bulk path exists today |
| Human | H-1 | Rules extractor (fallback) did not read bare or Hindi-order times; driver asked the same question repeatedly | `rules_extractor.py`, `orchestrator.py` | `test_h1_rules_read_hindi_order_and_bare_answer_times`, `test_h1_the_answer_to_kitne_baje_is_understood_without_the_model`, `test_h1_a_bare_time_answers_the_arrival_time_check` |
| Human | H-2 | Demo console did not show other-facility requests or explain why | `demo/` | demo only |
| Human | H-3 | Conversation stuck in SUBMITTED after any decision: no new slot after a rejection/expiry, nor after a further delay once approved | `service.py`, `booking.py` | `test_f2_rejected_request_releases_the_slot_and_the_driver_can_ask_again`, `test_h3_after_an_approved_booking_a_further_delay_gets_fresh_options_and_a_replacing_request` |
| Human | H-4 | Demo did not show duplicate/redelivery evidence | `demo/` | demo only |
| Human | H-5 | Hand-back gave the agent no context of the call; the driver was not told | 0022, `service.py`, `orchestrator.py` | `test_h5_after_a_hand_back_the_driver_is_told_and_the_agent_has_the_coordinators_note` |
| Human review | H-1r | Time-first phrases ("closes at 12 baje", "call me raat 10 baje") set the ETA without arrival context | `rules_extractor.py` | `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` |
| Human review | H-5r | Coordinator note was in the extraction input, whose output drives tool calls | `orchestrator.py`, `replies.py`, `prompts.py` (p4) | `test_h5_a_coordinator_note_cannot_report_escalate_hold_or_book` (obeying model, rules) |
| Human review | H-3r | No retry tests after EXPIRED and INVALIDATED | tests | `test_h3_after_an_expired_request_the_driver_can_ask_again_and_book`, `test_h3_after_an_invalidated_request_the_driver_can_ask_again_and_book` |
| Human review 2 | H-1r2 | Arrival context taken from anywhere in the message; other-subject check only before the time | `rules_extractor.py` | `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (+7), `test_h1_arrival_times_in_a_late_report_are_still_read` |
| Human review 2 | H-5r2 | Grounding check accepted unsupported status claims and promises in phrased replies | `replies.py` (`_STATUS_CLAIM`, `_PROMISE`) | `test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything`, `test_h5r_a_phrased_reply_may_not_claim_a_status_or_make_a_promise`, `test_h5r_every_option_template_passes_the_claim_check` |
| Human review 3 | H-1r3 | A bare time between commas ignored the event named in the next clause ("late, 12 baje, office band") | `rules_extractor.py` (`_EVENT_OWNER`, `_neighbours`) | `test_h1_a_time_owned_by_another_event_makes_the_agent_ask_for_the_arrival_time`, `test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` (+4) |
| Human review 3 | H-5r3 | Semantic status claims with no claim word ("green light", "all set", "good to go", "now active") passed the grounding check | `replies.py` (`structure_violations`, `SAFE_EXTRAS`), `orchestrator.py`, `prompts.py` (p5) | `test_h5r2_an_option_reply_may_not_add_any_wording_of_its_own`, `test_h5r2_the_option_list_with_at_most_one_fixed_line_is_accepted`, `test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` (5 claims) |
| Human review 4 | OD-3 | Owner decision: no phrasing model in production option replies; deterministic en / hi / hi-Latn templates; extraction unchanged | `orchestrator.py` (`_options_reply`; `_phrase_options` and `use_model_phrasing` removed), `replies.py` (`phrasing_inputs` without a note), `evals/phrase_ablation.py` (keeps the old loop for re-runs) | `tests/test_orchestrator.py::test_od3_option_replies_are_the_template_and_call_no_phrasing_model`, `tests/test_flows.py::test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` |
| Human review 4 | H-5r4 | "I have the coordinator's update" was an allowed courtesy line even with no note | `replies.py` (`SAFE_EXTRAS`) | `tests/test_agent_units.py::test_h5r4_no_courtesy_line_claims_a_coordinator_update` |
| Human review 4 verdict | D-OD3 | Docs still described a production phrasing model (P3, non-blocking) | `orchestrator.py` and `models.py` docstrings, `evals/b_ai_3/README.md`, `README.md` | Documentation only |
| Owner acceptance | F5/F6 | Owner's visual confirmation of F5 and F6, pending since review round 1 | `demo/checklist_results.json` | Owner confirmed both by eye, 21 Nov 2026 |
| Native review | S4-R2-08 | Reviewed Hinglish/Hindi wording, numbered order question, outcome notices in the driver's language, reading-test fixes | `replies.py`, `orchestrator.py`, `rules_extractor.py`, `extraction.py`, `domain/booking.py`, `notify/outbound.py` | `tests/test_agent_units.py::test_s4_r2_08_reviewed_hinglish_wording`, `tests/test_orchestrator.py::test_s4_r2_08_the_order_question_is_numbered_and_a_number_answers_it`, `tests/test_outbound.py::test_s4_r2_08_an_outcome_notice_is_sent_in_the_conversations_language`, `tests/test_agent_units.py::test_s4_r2_08_reading_test_arrival_times`, `tests/test_agent_units.py::test_s4_r2_08_reading_test_breakdowns_and_safety`, `tests/test_agent_units.py::test_s4_r2_08_reading_test_status_pick_and_yes` |
| Native review | S4-R2-08b | A driver asking to talk to a person ("baat kar sakte hai kya") is not recognised: no such request in the extraction schema | Deferred by the owner (21 Nov 2026): not in this build; schema + prompt change with a golden-set run when taken up | none yet |

## Status notes (21 Nov 2026)

- **B-AI-3** (templates vs Sonnet for option replies): superseded by OD-3. Stages 1–4 ran; the Sonnet arm and the
  stage 5 human scoring were not run. The templates were reviewed by a native speaker instead (S4-R2-08, closed).
- **B-AWS-0** (AWS account limited to Sydney, Claude not enabled there on 18 Nov): open. It no longer blocks the
  synthetic-data pilot, which will use OpenRouter; Bedrock stays optional.
- **Step 6 gates** AWS_QUEUE, AURORA, RECOVERY_WORKER, BOOKING_WORKFLOW, WORKLOAD_IDENTITY, OUTBOUND_DELIVERY: open, no
  AWS evidence yet. Local tests do not count as gate evidence.
- **Phase B (21 Nov 2026), decision T52:** OpenRouter primary for the synthetic Sydney pilot (Haiku 4.5 pinned to the
  `amazon-bedrock` zero-data-retention route, fallbacks off), Secrets Manager credentials in staging/pilot, usage ledger
  and daily budget (migration 0023), rules-reader fallback on every failure, Bedrock optional (`au.` profile in Sydney).
  Supersedes T4/T38 and modifies T24 for this pilot. Tests: `tests/test_providers.py`.
- **Phase B review (21 Nov 2026), commit 59032f6:** R1 (High) malformed OpenRouter answers could raise outside
  `ModelError`: **closed** (every field type-checked; unusable answers settled once as `ERROR`/`malformed` with their
  real cost, then the rules reader; only database errors tolerated by the ledger code). R4 (Medium) credit-limit 403s
  classed as `auth`: **closed** (`credits`). R2 (Low, caller-supplied cap/cost), R3 (Low, no text length limits),
  R5 (Info, crashed-process reservations): **open follow-ups**. Tests: `tests/test_providers.py`.
- **Phase D (21 Nov 2026):** secret scan of files and history clean; 47 dependency advisories closed by upgrading
  FastAPI, Starlette, PyJWT, cryptography and pytest (`docs/release/02_SECURITY_SCAN.md`).
- **Phase E (22 Nov 2026), branch phase-e/aws-implementation:** E0–E6 implemented and tested locally, not deployed
  (`docs/release/07_AWS_IMPLEMENTATION.md`). Found and fixed while building: the Data API would have broken untyped
  string parameters (fixed by literal binding); `/health` used the system role (now ingest); a CDK dependency cycle;
  a non-reproducible Lambda zip. Open: notices outside the 24-hour WhatsApp window need template sending (owner
  decision); application secret split; `/tools` roles on the API workload; action SHA pins.
- **Phase E consolidated review of 250b794 (22 Nov 2026), branch phase-e/aws-implementation-fixes:** fixed locally:
  E5-01 synthesis output directory, E5-02 deploy the checked assembly (`cdk deploy --app cdk.out`), E5-03 the zip is
  the Lambda asset and a credential-free check hashes the staged asset (`setuhaul/ops/assembly.py`), E6-01 approved
  templates outside the 24-hour window with escalation when none is approved, IAM-01 WhatsApp token in its own secret
  (outbound only), IAM-02 `/tools` off in staging and pilot (API logins ingest + console), E4-01 callback completion
  tracking (migration 0024, `approval_callbacks`), E5-04 actions pinned by SHA and the CDK CLI by exact version, E5-05
  rollback documented as redeploying the previous assembly, E0-01 4xx alarm renamed. Outstanding: E5-06 (Ubuntu
  reproducibility, runs in CI), E3-01 to E3-04 (AURORA gate), the OIDC deploy role, Meta submission and the Hinglish
  gate. Record: `docs/release/07_AWS_IMPLEMENTATION.md`; tests: `docs/release/01_TEST_REPORT.md`.
- **Phase E second review round (22 Nov 2026), on top of 5a9a16d:** E4-02 (High) callback backoff cast to integer
  before the cap, overflowing from 31 attempts: **resolved** (exponent capped first in 0024;
  `test_e4_02_the_backoff_is_capped_at_30_minutes_without_integer_overflow`). E5-07 (Medium) the assembly check
  ignored `S3Bucket`: **resolved** (each function's bucket and key must match one destination of the staged asset;
  `test_e5_07_*` and `test_the_assembly_check_fails_when_a_function_points_elsewhere`). E5-08 (High) a manual
  dispatch could deploy from any branch: **resolved in the repository** (allow-list `refs/heads/release/fde-sydney-pilot`
  in the job condition and a first-step guard, typed confirmation kept; `tests/test_pipeline.py`); the matching OIDC
  trust (subject with `ref`, exact `StringEquals`, `infra/github_oidc_deploy_trust_policy.json`) is documented, and
  its enforcement in GitHub and AWS stays an **external gate**. Record: `docs/release/07_AWS_IMPLEMENTATION.md`.
