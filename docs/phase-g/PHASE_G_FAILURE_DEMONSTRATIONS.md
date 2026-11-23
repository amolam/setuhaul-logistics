# Phase G6: failure demonstrations

Seven **real** failures from this project's history (pilot and local), and four **CONTROLLED FAILURE INJECTIONS** made on
purpose in the isolated local environment. Every one has a regression test that runs in the final validation
(`PHASE_G_FINAL_VALIDATION_REPORT.md`). Sources: `docs/release/13_STEP6_GATE_EVIDENCE.md`,
`14_OPTION_D_ACTIVATION.md`, `15_RELEASE_NOTES.md`, `docs/challenge/SCENARIO_RUN.md`.

## Real failures

### F1. The approval relay could not start Step Functions executions (IAM)
| | |
|---|---|
| Initial condition | Pilot, Step 6 approval drill: three booking requests PENDING, relay running every minute |
| Failure | `states:StartExecution` denied: the relay's grant named the state machine, the code started the **alias** `live` |
| Expected | each request starts one execution |
| Actual | 0 executions; requests stayed PENDING with claims held (no booking was approved or lost) |
| Detection | drill check "Request (×3) → executions" failed; CloudWatch AccessDenied |
| Guardrail / fix | grant lists the alias and the machine; deployed 23 Nov (template diff: only that resource list) |
| Evidence | `docs/release/13_STEP6_GATE_EVIDENCE.md` (drill table; controlled deployment section) |
| Test | `tests/test_infra.py::test_the_relay_may_start_executions_through_the_alias` |
| Outcome | relay consumed 5 events, started 3 executions; BOOKING_WORKFLOW later PASS (drill 2, 10/10) |

### F2. Task functions required the state-machine ARN
| | |
|---|---|
| Initial condition | after F1's fix, the first executions ran their task Lambdas |
| Failure | `KeyError: 'SETUHAUL_APPROVAL_STATE_MACHINE_ARN'` in `_workflow()`: every function demanded the ARN; task functions cannot have it (CloudFormation cycle) and do not need it |
| Expected | token-recording and expiry tasks run |
| Actual | all 3 executions FAILED; an overdue request stayed OPEN (expiry depends on the task) |
| Detection | Step Functions execution history; ApprovalFailed alarm |
| Guardrail / fix | ARN optional in `_workflow()`; `start()` refuses with a clear configuration error when it is missing; failed runs redriven safely |
| Evidence | `13_STEP6_GATE_EVIDENCE.md` "Second deployed defect" |
| Test | `tests/test_aws.py::test_the_state_machine_task_lambdas_run_without_the_state_machine_arn` (fails with KeyError on the old code) |
| Outcome | redrive completed; overdue request EXPIRED by `system:expiry`, claim released |

### F3. The rules reader read 05:26 as 17:26
| | |
|---|---|
| Initial condition | scenario run, 23 Nov; driver: "anything after 05:26?" |
| Failure | the evening heuristic for bare hours ("after 5" = 17:00) also applied to a zero-padded 24-hour time |
| Expected | earliest start 05:26 |
| Actual | 17:26: options 12 hours later than asked |
| Detection | scenario run comparison of the requested window with the options |
| Guardrail / fix | zero-padded 24-hour times keep their morning meaning; "after 5" still means evening |
| Evidence | `docs/challenge/SCENARIO_RUN.md` |
| Test | `tests/test_scenario_run.py::test_a_zero_padded_24_hour_time_keeps_its_morning_meaning`; Phase G eval rows I02/I12 (window extraction) |
| Outcome | fixed before the Option D activation |

### F4. The activation writer was defined after the `__main__` guard (failed closed)
| | |
|---|---|
| Initial condition | Option D activation run 45128314359 |
| Failure | running the module executed the guard before the writer function existed: the manifest/evidence write never ran |
| Expected | manifest and evidence recorded; api and agent start with booking ON |
| Actual | nothing written; api and agent **refused to start** ≈07:10–07:23 UTC (fail-closed booking preconditions); no booking possible |
| Detection | activation workflow failed; `/health` and start-up refusals |
| Guardrail / fix | function order fixed; start-up gate (`check_booking_preconditions`) is what kept it safe |
| Evidence | `14_OPTION_D_ACTIVATION.md` "Corrections, stated openly" |
| Test | `tests/test_option_d.py::test_the_pipeline_command_runs_as_a_module_and_refuses_any_other_login` |
| Outcome | re-run succeeded; Option D active under OWNER-APPROVAL-20261123-OPTION-D |

### F5. Console Confirm returned HTTP 500
| | |
|---|---|
| Initial condition | Option D active; owner confirms a request in the hosted console |
| Failure | `BookingService.decide` ran an advisory feasibility re-check needing a **feasibility** login the api function does not have |
| Expected | CONFIRMED |
| Actual | HTTP 500 on every Confirm (no data changed) |
| Detection | owner's hands-on test (drills had decided through database roles directly) |
| Guardrail / fix | skip the advisory where the login is absent; `decide_booking_request` still re-proves every rule; no permission widened (`8fe6935`) |
| Phase G lesson applied | the new console routes (recommendations, observability) read **only with the console role**; `tests/test_phase_g.py` runs them through the same app; gap assessment flags the Phase F schedule-proposal route that still uses the feasibility role |
| Test | `tests/test_option_d.py::test_a_console_confirm_works_with_the_deployed_api_logins_only` |
| Outcome | redeployed (run 45137914143); baseline v0.2.1 |

### F6. The read-back appeared twice in the console
| | |
|---|---|
| Initial condition | owner reviews a conversation after a booking |
| Failure | the stored read-back record (what the YES is checked against) was rendered like a sent message |
| Expected | the driver's message list shows the read-back once |
| Actual | it looked duplicated; the driver had received it once |
| Detection | owner's hands-on test |
| Guardrail / fix | `read_back_record` flag; console labels it "READ-BACK RECORD (not sent)" (`4f7da62`) |
| Test | `tests/test_console_threads.py::test_the_read_back_record_is_marked_and_was_never_sent` |
| Outcome | fixed in v0.2.1 |

### F7. A pick after the 5-minute offer expired got "Sorry, I did not understand" — **fixed in Phase G**
| | |
|---|---|
| Initial condition | options shown; the driver answers "1" after the offer expired (`shown_options.expires_at`) |
| Failure | `get_driver_context` lists only unexpired options, so the pick matched nothing and the turn fell through to `unclear` |
| Expected | say the slots expired and offer the current ones |
| Actual (v0.2.1) | "Sorry, I did not understand. When will you reach the warehouse?" |
| Detection | v0.2.1 release notes, open item |
| Fix (root cause) | the orchestrator detects a pick (number, BOOK intent or option id) while the flow is OPTIONS_SHOWN with no live options, replies `options_expired` and re-offers through the optimizer; nothing is selected or held from the stale pick. Hindi/Hinglish wording awaits native review |
| Test | `tests/test_phase_g.py::test_a_pick_after_the_offer_expired_is_answered_with_fresh_slots` (scripted model and rules reader); eval row C20 |
| Outcome | see the final validation report |

## Controlled failure injections (deliberate, local only)

| Id | Injection | Expected safe behaviour | Test / row |
|---|---|---|---|
| CI-1 | **CONTROLLED FAILURE INJECTION:** `SlotOptimizer._run` raises | status ERROR, no internals leak, driver still gets the engine's options, `fallback_used = true` recorded | `test_optimizer_failure_falls_back_to_the_engine_options`, eval C09, `test_engine_failure_is_a_safe_error` |
| CI-2 | **CONTROLLED FAILURE INJECTION:** model raises `ModelError` (provider down) | rules reader extracts; options still offered; no paid call | eval C08; existing `test_model_down_uses_rules_and_templates_booking_unaffected` |
| CI-3 | **CONTROLLED FAILURE INJECTION:** optimizer clock jumps past the time limit | FEASIBLE with only proven options, or UNKNOWN with none; never an unchecked option | `test_timeout_returns_feasible_or_unknown_never_unchecked_options` |
| CI-4 | **CONTROLLED FAILURE INJECTION:** two drivers, same recommended slot, holds fired together | exactly one HELD; no overlapping claims (exclusion constraint) | `test_two_drivers_racing_for_the_same_recommended_slot_one_wins` |
