# Requirement → test traceability

Each row maps a requirement to the test that proves it locally. "Step 6" means the local test covers the logic, and
real AWS acceptance is still needed (`DEPLOYMENT_GATES.md`). Test names are checked by
`tests/test_traceability.py`, so a renamed or deleted test fails the build.

## End-to-end business flows (local; `tests/test_flows.py`)

Each flow runs the real signed webhook, API, roles, database functions, orchestrator, approval workflow port and
outbound relay. The model is scripted, Cognito tokens are signed locally, Step Functions is `LocalApprovalWorkflow` and
WhatsApp is `LocalProvider`. Real AWS acceptance is step 6.

| Flow | Test | AWS |
|---|---|---|
| F1 Delay → options → choice → YES → request → planner approves → the driver gets every reply once, in order, then the outcome | `tests/test_flows.py::test_f1_delay_to_approved_booking_and_the_driver_is_told` | Step 6 |
| F2 Warehouse rejects → slot released → driver told → can ask again | `tests/test_flows.py::test_f2_rejected_request_releases_the_slot_and_the_driver_can_ask_again` | Step 6 |
| F3 Nobody answers → workflow timeout → EXPIRED → person alerted → driver told | `tests/test_flows.py::test_f3_unanswered_request_expires_through_the_workflow_timeout` | Step 6 (real timeouts) |
| F4 Breakdown while pending → INVALIDATED → driver told; nothing left to approve | `tests/test_flows.py::test_f4_a_breakdown_while_pending_invalidates_and_the_driver_is_told` | Step 6 |
| F5 Safety → hand-over, no request; the coordinator message IS delivered | `tests/test_flows.py::test_f5_a_safety_report_hands_over_and_the_coordinator_message_is_delivered` | Step 6 |
| F6 Duplicate webhook and redelivered event → one turn, one message | `tests/test_flows.py::test_f6_duplicate_webhooks_and_a_redelivered_event_give_one_reply_on_the_phone` | Step 6 (real SQS) |
| F7 Ambiguous send settled by the receipt → no repeat | `tests/test_flows.py::test_f7_an_ambiguous_send_is_settled_by_the_receipt_without_a_repeat` | Step 6 (real WhatsApp) |
| F8 Authorised policy rule → confirmed in-turn, told once, never "approved" | `tests/test_flows.py::test_f8_an_authorised_policy_rule_confirms_without_a_person_and_says_so` | — |
| F9 Two coordinators at once → one outcome, one message | `tests/test_flows.py::test_f9_two_coordinators_deciding_at_once_give_one_outcome_and_one_message` | — |

Covered elsewhere and part of the same flows: 50 drivers racing for one slot
(`tests/test_api.py`), crashed and fenced worker turns (`tests/test_inbound_delivery.py`), approval races
(`tests/test_approvals.py`), provider failure modes (`tests/test_outbound.py`).

## Build invariant: the agent requests; the application and database decide

| Requirement | Test | AWS |
|---|---|---|
| The agent's functions are exactly an allowlist of requests | `tests/test_agent_not_operator.py::test_agent_functions_are_exactly_the_allowlist` | — |
| The agent cannot write any protected table | `tests/test_agent_not_operator.py::test_agent_can_write_no_protected_table` | — |
| The agent cannot approve, withdraw, set holds or read task tokens | `tests/test_approvals.py::test_the_agent_cannot_decide_withdraw_hold_or_read_tokens` | — |
| No role can manufacture a staff identity or approve | `tests/test_approvals.py::test_s15_no_other_role_can_manufacture_an_ops_lead_or_approve` | Step 6 (login → role mapping) |
| Console functions are executable by the console role only | `tests/test_approvals.py::test_console_functions_are_console_only` | Step 6 |

## B-WF-2 approval workflow (design revision 3)

| Requirement | Test |
|---|---|
| §2 Submission creates one OPEN approval, bound to the appointment and its claim | `tests/test_approvals.py::test_submit_creates_one_open_approval_bound_to_the_appointment_and_its_claim` |
| §2 One open request per shipment | `tests/test_approvals.py::test_one_open_request_per_shipment` |
| §2 Final states are permanent; no deletion | `tests/test_approvals.py::test_a_final_state_is_permanent_and_rows_are_never_deleted` |
| §2 Only the outcome of an OPEN request may change | `tests/test_approvals.py::test_open_requests_cannot_be_edited_except_by_their_outcome` |
| §3 Approve: principal from the session, one event, one audit row | `tests/test_approvals.py::test_approve_confirms_with_the_session_principal_one_event_and_an_audit_row` |
| §3 Reject releases the claim | `tests/test_approvals.py::test_reject_releases_the_claim_and_records_the_decider` |
| §4 A: the YES is bound to driver, shipment, slot; used once | `tests/test_approvals.py::test_a_the_yes_is_used_once_and_bound_to_driver_and_slot` |
| §4 A: any later driver message voids an unused YES | `tests/test_db_requests.py::test_any_later_driver_message_supersedes_even_if_not_attested` |
| §5 B: the database re-proves the booking | `tests/test_approvals.py::test_b_the_database_re_proves_the_booking_without_the_service` |
| §5 B: the database classifies after-hours itself | `tests/test_approvals.py::test_the_database_classifies_after_hours_itself` |
| §6 E: an OPEN request always has its live claim (commit-time check) | `tests/test_approvals.py::test_commit_time_check_refuses_an_open_request_without_its_claim` |
| §6 D: a dock block invalidates in the same transaction | `tests/test_approvals.py::test_d_a_dock_block_over_the_claim_invalidates_in_the_same_transaction` |
| §6 D: shipment, dock and hours changes invalidate | `tests/test_approvals.py::test_d_shipment_dock_and_hours_changes_invalidate_with_a_system_principal` |
| §6 A confirmed booking still blocks a dock block | `tests/test_db_requests.py::test_block_dock_refuses_to_overwrite_a_booking` |
| §7 Q4: withdrawal records reason code and coordinator | `tests/test_approvals.py::test_q4_withdrawal_records_the_reason_and_the_coordinator` |
| §7 Q4: supersession is recorded separately | `tests/test_approvals.py::test_q4_a_newer_booking_supersedes_and_is_recorded_as_such` |
| §8 Q2: the role required depends on the kind | `tests/test_approvals.py::test_q2_the_role_needed_depends_on_the_request_kind` |
| §8 Q2: the role table matches the agreed mapping | `tests/test_approvals.py::test_q2_role_rules_match_the_agreed_table` |
| §9 C: outcome and event exist with the workflow stopped | `tests/test_approvals.py::test_c_the_outcome_and_its_event_exist_with_the_workflow_stopped` |
| §9 The workflow start is idempotent; a decision ends it | `tests/test_approvals.py::test_workflow_start_is_idempotent_and_a_decision_ends_it` |
| §9 Timeout expires in PostgreSQL; a late timer changes nothing | `tests/test_approvals.py::test_workflow_timeout_expires_in_postgres_and_changes_nothing_when_already_decided` |
| §9 The reconciler finishes a lost callback | `tests/test_approvals.py::test_the_reconciler_finishes_an_execution_whose_callback_was_lost` |
| §9 The ASL definition matches the local workflow (Step 6: real run) | `tests/test_approvals.py::test_the_asl_definition_matches_the_local_workflow` |
| §10 Q1: every rule ships NEEDS_PLANNER; rules are append-only | `tests/test_approvals.py::test_q1_every_rule_ships_needing_a_planner` |
| §10 / §19 Policy confirmation is never an approval and keeps its evidence | `tests/test_approvals.py::test_q1_a_policy_confirmation_is_never_an_approval_and_keeps_its_evidence` |
| §10 The retired per-shift switch confirms nothing | `tests/test_db_requests.py::test_the_retired_auto_confirm_switch_confirms_nothing` |
| §11 Q3: an operational hold blocks approval and new requests, not rejection | `tests/test_approvals.py::test_q3_an_operational_hold_blocks_approval_and_new_requests_but_not_rejection` |
| §11 Q3: an automation pause does not stop people | `tests/test_approvals.py::test_q3_an_automation_pause_does_not_stop_people` |
| §14 A transaction that started before the deadline cannot approve after it | `tests/test_approvals.py::test_s14_a_transaction_that_started_before_the_deadline_cannot_approve_after_it` |
| §14 Expiry closes request and claim; system principal | `tests/test_approvals.py::test_expiry_closes_the_request_and_its_claim_with_the_system_principal` |
| §15 The console needs a context from this transaction | `tests/test_approvals.py::test_s15_the_console_needs_a_context_from_this_transaction` |
| §15 The DB layer attaches an identity only to console writes | `tests/test_approvals.py::test_s15_the_db_layer_only_attaches_an_identity_to_console_writes` |
| §16 Races give exactly one outcome and no deadlock | `tests/test_approvals.py::test_races_resolve_to_exactly_one_outcome` |
| §16 Approve vs the sweep at the deadline | `tests/test_approvals.py::test_race_approve_vs_the_sweep_at_the_deadline` |
| §17 One ApprovalDecided per approval | `tests/test_approvals.py::test_s17_one_approval_decided_event_per_approval` |
| §17 Consuming an event twice has one effect | `tests/test_approvals.py::test_s17_consuming_an_event_twice_has_one_effect` |

## B-WF-2a confirmation policy (migration 0016)

| Requirement | Test |
|---|---|
| One test per rule row | `tests/test_confirmation_policy.py::test_each_rule_row_selects_its_outcome_and_kind` |
| The default row catches everything else | `tests/test_confirmation_policy.py::test_the_default_row_catches_everything_else` |
| Q1: every facility has every row; none confirms | `tests/test_confirmation_policy.py::test_every_facility_has_every_row_and_none_confirms` |
| A policy rule needs authorisation and auto-eligibility | `tests/test_confirmation_policy.py::test_a_policy_rule_needs_an_authorisation_and_auto_eligibility` |
| A plain new booking is the safe case and still needs a person | `tests/test_confirmation_policy.py::test_a_plain_new_booking_is_the_safe_case_and_still_needs_a_person` |
| Critical load needs a planner | `tests/test_confirmation_policy.py::test_critical_load_needs_a_planner_not_operations` |
| Move earlier, later, into a busier time | `tests/test_confirmation_policy.py::test_moving_a_booking_earlier_later_and_into_a_busier_time` |
| After the last new start | `tests/test_confirmation_policy.py::test_after_last_new_start_needs_a_planner` |
| Reefer load and dock downgrade | `tests/test_confirmation_policy.py::test_reefer_load_and_dock_downgrade` |
| Person always creates nothing | `tests/test_confirmation_policy.py::test_person_always_creates_nothing` |
| Person always holds whatever the rule order | `tests/test_confirmation_policy.py::test_person_always_holds_even_when_a_rule_would_shadow_it` |
| An authorised rule never confirms a risky request | `tests/test_confirmation_policy.py::test_an_authorised_policy_rule_never_confirms_a_risky_request` |
| Override rate per rule is reported | `tests/test_confirmation_policy.py::test_override_rate_per_rule_is_reported_to_planners` |

## B-WF-3 outbound delivery (migration 0017)

| Requirement | Test | AWS |
|---|---|---|
| One notification per reply, stable id, payload without text | `tests/test_outbound.py::test_an_agent_reply_becomes_one_notification_with_a_stable_id` | — |
| S4-R2-07: the stored reply is sent; a changed row is refused | `tests/test_outbound.py::test_s4_r2_07_the_stored_reply_is_what_is_sent_and_a_changed_row_is_refused` | Step 6 |
| Retries back off | `tests/test_outbound.py::test_retryable_errors_back_off_and_then_send` | — |
| Retries used up: failure is escalated | `tests/test_outbound.py::test_retries_used_up_fail_and_tell_a_person` | — |
| Permanent error fails at once | `tests/test_outbound.py::test_a_permanent_error_fails_at_once_and_tells_a_person` | — |
| A timeout is UNKNOWN; a receipt resolves it without resend | `tests/test_outbound.py::test_a_timeout_is_unknown_and_a_receipt_resolves_it_without_a_resend` | Step 6 |
| Lost answer reconciled; at most one same-id resend; idempotent provider absorbs it | `tests/test_outbound.py::test_a_lost_provider_answer_is_reconciled_with_at_most_one_same_id_resend` | Step 6 |
| No third send | `tests/test_outbound.py::test_a_second_ambiguity_after_the_resend_fails_instead_of_a_third_send` | — |
| Stale workers fenced | `tests/test_outbound.py::test_a_stale_worker_cannot_move_the_state_but_its_provider_id_is_kept` | — |
| Concurrent relays send once | `tests/test_outbound.py::test_concurrent_relays_send_each_notification_once` | — |
| Approval outcome notifies the driver with a fixed template | `tests/test_outbound.py::test_an_approval_outcome_notifies_the_driver_with_a_fixed_template` | Step 6 (approved templates) |
| Takeover stops an unsent agent reply | `tests/test_outbound.py::test_a_person_taking_over_stops_an_unsent_agent_reply` | — |
| Receipts through the signed webhook | `tests/test_outbound.py::test_delivery_receipts_arrive_through_the_signed_webhook` | Step 6 |
| WhatsApp adapter: no idempotency claimed; outcomes mapped | `tests/test_outbound.py::test_whatsapp_adapter_is_honest_about_idempotency_and_maps_outcomes` | Step 6 |
| Only the system sends; only the webhook path records receipts | `tests/test_outbound.py::test_only_the_system_sends_and_only_the_webhook_path_records_receipts` | — |

## Inbound delivery and deployment gates (steps 4 R1.9 to R2.1)

| Requirement | Test | AWS |
|---|---|---|
| One turn per message; expired lease cannot finish | `tests/test_inbound_delivery.py::test_s4_r2_01_a_worker_cannot_finish_after_its_lease_expired` | Step 6 (real SQS) |
| Booking switch fails closed | `tests/test_inbound_delivery.py::test_s4_05_the_booking_switch_fails_closed` | Step 6 |
| Evidence cannot be future-dated | `tests/test_inbound_delivery.py::test_s4_r3_01_future_dated_evidence_is_refused` | — |
| Every gate bound to the deployment manifest | `tests/test_inbound_delivery.py::test_s4_r3_02_every_gate_is_bound_to_the_deployment_manifest` | Step 6 (drift detection) |
| The DLQ itself is verified | `tests/test_inbound_delivery.py::test_r2_1_the_dead_letter_queue_itself_is_verified` | Step 6 |

## Complete-build review R3 (migration 0019)

| Requirement | Test |
|---|---|
| A person's reject or withdraw that waits on a lock past the deadline is refused; the sweep records EXPIRED | `tests/test_approvals.py::test_r3_a_person_waiting_on_a_lock_past_the_deadline_cannot_reject_or_withdraw` |
| An invalidation that waits on a lock past the deadline records EXPIRED, and its trigger's change commits | `tests/test_approvals.py::test_r3_an_invalidation_waiting_on_a_lock_past_the_deadline_records_expired` |
| A dock block after the deadline records EXPIRED; the block stands | `tests/test_approvals.py::test_r3_a_dock_block_after_the_deadline_records_expired_and_the_block_stands` |
| Each booking-critical shipment field invalidates an open request | `tests/test_approvals.py::test_r3_a_booking_critical_shipment_change_invalidates_the_open_request` |
| The decision re-proves dock compatibility itself | `tests/test_approvals.py::test_r3_decide_re_proves_dock_compatibility_itself` |
| A change between option and request is refused at hold and at submission | `tests/test_approvals.py::test_r3_a_change_between_the_option_and_the_request_is_refused_at_hold_and_at_submission` |
| Console mutations check role and principal in the database | `tests/test_approvals.py::test_r3_console_mutations_check_role_and_principal_in_the_database` |
| Console mutations need the trusted staff context | `tests/test_approvals.py::test_r3_console_mutations_need_the_trusted_staff_context` |
| The console cannot write the audit log, outbox or human flag directly | `tests/test_approvals.py::test_r3_the_console_cannot_write_audit_outbox_or_takeover_directly` |
| Takeover is audited with the staff principal and role | `tests/test_approvals.py::test_r3_takeover_records_the_staff_principal_and_role` |
| Takeover stops a retry | `tests/test_outbound.py::test_r3_takeover_after_a_retryable_failure_stops_the_retry` |
| Takeover after an ambiguous send: no resend, a person is told | `tests/test_outbound.py::test_r3_takeover_after_an_ambiguous_send_is_not_resent_and_a_person_is_told` |
| Takeover after the claim is fenced before the provider call | `tests/test_outbound.py::test_r3_takeover_after_the_claim_is_fenced_before_the_provider_call` |
| WhatsApp 5xx is ambiguous; 429 is retryable | `tests/test_outbound.py::test_r3_a_whatsapp_5xx_is_ambiguous_and_a_429_is_retryable` |
| A receipt that cannot be stored is not acknowledged | `tests/test_outbound.py::test_r3_a_receipt_that_cannot_be_stored_is_not_acknowledged` |

## Review R3.1 (migration 0020)

| Requirement | Test |
|---|---|
| A change that got there first makes the decision wait, then refuse | `tests/test_approvals.py::test_r31_a_shipment_change_that_got_there_first_makes_the_decision_wait_then_refuse` |
| A shipment or dock change during a decision waits for it; an incompatible confirmed booking is escalated | `tests/test_approvals.py::test_r31_a_change_that_arrives_during_the_decision_waits_and_a_person_is_told` |
| Decisions racing shipment changes never deadlock and never approve silently | `tests/test_approvals.py::test_r31_decisions_racing_shipment_changes_never_deadlock_and_never_approve_silently` |
| A planner can take over and hand back at their facility | `tests/test_approvals.py::test_r31_a_planner_can_take_over_and_hand_back_at_their_facility` |
| A planner at another facility cannot | `tests/test_approvals.py::test_r31_a_planner_at_another_facility_cannot_take_over_or_hand_back` |

## Review R3.2 (migration 0021)

| Requirement | Test |
|---|---|
| A shipment change and a dock change to one confirmed booking are serialised; the later one sees both and escalates (reviewer's 1,000 → 1,500 kg / 2,000 → 1,200 kg case, either order) | `tests/test_approvals.py::test_r32_shipment_and_dock_changes_to_one_booking_are_serialised_and_a_person_is_told` |
| Simultaneous shipment and dock changes never deadlock; the final state is compatible or has a durable BOOKING_AT_RISK escalation | `tests/test_approvals.py::test_r32_simultaneous_shipment_and_dock_changes_end_compatible_or_escalated` |
| A compatibility change under REPEATABLE READ is refused | `tests/test_approvals.py::test_r32_a_compatibility_change_under_repeatable_read_is_refused` |
| Planner takeover and handback audit rows record principal and role `planner` | `tests/test_approvals.py::test_r31_a_planner_can_take_over_and_hand_back_at_their_facility` |

## Human end-to-end check (demo console)

| Requirement | Test |
|---|---|
| Without the model, a bare time answers the arrival-time question; Hindi word order and day parts are read | `tests/test_agent_units.py::test_h1_rules_read_hindi_order_and_bare_answer_times`, `tests/test_agent_units.py::test_h1_a_bare_number_is_a_time_only_as_an_answer_to_the_time_question`, `tests/test_agent_units.py::test_h1_duration_after_the_late_word` |
| The reported conversations reach slot options in one answer | `tests/test_orchestrator.py::test_h1_the_answer_to_kitne_baje_is_understood_without_the_model`, `tests/test_orchestrator.py::test_h1_a_bare_time_answers_the_arrival_time_check` |
| After a rejection the driver gets real new slots (H-3) | `tests/test_flows.py::test_f2_rejected_request_releases_the_slot_and_the_driver_can_ask_again` |
| After an approval, a further delay gets fresh slots and a request that replaces the confirmed booking (H-3) | `tests/test_flows.py::test_h3_after_an_approved_booking_a_further_delay_gets_fresh_options_and_a_replacing_request` |
| On hand-back the driver is told in their language, and the agent gets the coordinator's note as background (H-5) | `tests/test_flows.py::test_h5_after_a_hand_back_the_driver_is_told_and_the_agent_has_the_coordinators_note` |
| A time that is not about arriving never sets the ETA; time-first phrases need an arrival verb, the pending time question or a late report (H-1 review) | `tests/test_agent_units.py::test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` |
| Driver facts are extracted from the driver's message only; the coordinator's note reaches no model (since OD-3) and cannot report, escalate, hold or book (H-5 review) | `tests/test_flows.py::test_h5_a_coordinator_note_cannot_report_escalate_hold_or_book`, `tests/test_flows.py::test_h5_after_a_hand_back_the_driver_is_told_and_the_agent_has_the_coordinators_note` |
| After an EXPIRED or INVALIDATED request the driver gets fresh slots and can book a new request (H-3 review) | `tests/test_flows.py::test_h3_after_an_expired_request_the_driver_can_ask_again_and_book`, `tests/test_flows.py::test_h3_after_an_invalidated_request_the_driver_can_ask_again_and_book` |
| A time is read clause by clause: another subject on either side of it in its clause means it is not an arrival time; legitimate late-report times still read (H-1r2) | `tests/test_agent_units.py::test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta`, `tests/test_agent_units.py::test_h1_arrival_times_in_a_late_report_are_still_read` |
| A phrased reply cannot claim a booking status that FACTS do not show, nor make an operational promise, even when a coordinator note tells the model to; the template is sent instead (H-5r2) | `tests/test_flows.py::test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything`, `tests/test_agent_units.py::test_h5r_a_phrased_reply_may_not_claim_a_status_or_make_a_promise`, `tests/test_agent_units.py::test_h5r_every_option_template_passes_the_claim_check` |
| A bare time whose neighbouring clause names an event at a time is not an arrival time; the agent asks for the arrival time instead (H-1r3) | `tests/test_agent_units.py::test_h1_a_time_owned_by_another_event_makes_the_agent_ask_for_the_arrival_time`, `tests/test_agent_units.py::test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta` |
| An option reply is the system's own option list, unchanged, plus at most one fixed courtesy line; booking-status language comes only from the system (H-5r3) | `tests/test_agent_units.py::test_h5r2_an_option_reply_may_not_add_any_wording_of_its_own`, `tests/test_agent_units.py::test_h5r2_the_option_list_with_at_most_one_fixed_line_is_accepted`, `tests/test_flows.py::test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` |
| Owner decision OD-3: option replies are the deterministic template in the conversation's language (en / hi / hi-Latn); no phrasing model is called, and a coordinator note reaches no model | `tests/test_orchestrator.py::test_od3_option_replies_are_the_template_and_call_no_phrasing_model`, `tests/test_flows.py::test_h5r_a_note_cannot_make_the_reply_claim_approval_or_promise_anything` |
| No fixed courtesy line claims a coordinator update; the removed line is rejected as the model's own wording (round 4) | `tests/test_agent_units.py::test_h5r4_no_courtesy_line_claims_a_coordinator_update` |
| Native review S4-R2-08: the reviewed Hinglish wording is used (slot-stays line, deadline question) | `tests/test_agent_units.py::test_s4_r2_08_reviewed_hinglish_wording` |
| The order question numbers the orders in every language, and a bare number answers it (not a slot pick) | `tests/test_agent_units.py::test_s4_r2_08_the_order_question_numbers_the_orders`, `tests/test_agent_units.py::test_s4_r2_08_a_bare_number_is_read_as_the_answer_to_a_numbered_question`, `tests/test_orchestrator.py::test_s4_r2_08_the_order_question_is_numbered_and_a_number_answers_it` |
| Warehouse-outcome notices go out in the conversation's language (en / hi / hi-Latn), all reviewed | `tests/test_outbound.py::test_s4_r2_08_an_outcome_notice_is_sent_in_the_conversations_language`, `tests/test_outbound.py::test_s4_r2_08_every_outcome_notice_has_reviewed_hinglish_and_hindi` |
| The reviewer's reading-test messages are read correctly by the rules reader (14 of 15; "talk to a person" open) | `tests/test_agent_units.py::test_s4_r2_08_reading_test_arrival_times`, `tests/test_agent_units.py::test_s4_r2_08_reading_test_breakdowns_and_safety`, `tests/test_agent_units.py::test_s4_r2_08_reading_test_status_pick_and_yes` |
| Demo checklist: F1 pass (planner approval expected while policy confirmation is off). F5 and F6: Claude's automated reruns passed, and the owner confirmed both by eye on 21 Nov 2026 | `demo/checklist_results.json` |
