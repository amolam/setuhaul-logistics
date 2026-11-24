# Booking evidence renewal gate: test report

Branch `feature/booking-evidence-renewal-gate` from `main` `e280a40`. Validated 23–24 Nov 2026, **locally** (PostgreSQL
18.6, Python 3.14; CI runs PostgreSQL 17.9). Nothing was deployed, no workflow was triggered, and the v0.3.0 pilot was not
changed. The only AWS calls were read-only, made during the assessment (Step Functions history, CloudWatch logs,
Lambda and stack configuration, `/health`).

## 1. Results

| Suite | Result |
|---|---|
| New: `tests/test_renewal_gate.py` | **64 passed** (31 test functions; 4 against PostgreSQL for migration 0029) |
| Full regression (`python -m pytest tests`) | **1375 passed, 0 failed, 2 skipped** (45 min 22 s), after the fix in §4. The skips are pre-existing: `test_aurora_owner.py:126` (needs an empty cluster; CI covers it) and `test_data_api.py:155` (Data API mode only) |
| Data API backend (CI list) | **559 passed** (23 min 52 s; `SETUHAUL_TEST_DATA_API=1`, the `ci.yml` list) |
| Existing activation tests (`test_gate_evidence`, `test_option_d`, `test_infra`, `test_migrations`) | 76 passed (before the full run; included in it) |

One existing security pin was extended by exactly one named function (§4); no other existing test was changed.

## 2. Coverage of the acceptance list

| Area | Tests (`tests/test_renewal_gate.py` unless noted) |
|---|---|
| Activation unchanged | `test_an_activation_report_still_must_be_booking_off`, `test_the_activation_collector_is_unchanged_and_renewal_is_opt_in`, all of `test_gate_evidence.py` and `test_option_d.py` (unchanged, pass) |
| Renewal succeeds when valid | `test_the_live_shaped_pilot_passes_renewal_with_each_execution_in_its_category`, `test_a_valid_renewal_report_becomes_evidence_for_the_renewed_gates` |
| Configuration: api off, agent off, unexpected function on, odd value, wrong environment, non-synthetic | `test_booking_configuration_is_exact` (6 cases) |
| Kill switch, /health environment, booking off in /health, approval missing or malformed, BookingMode off | `test_health_kill_switch_and_owner_approval` (6), `test_an_engaged_kill_switch_can_be_the_expected_state_but_is_never_assumed` |
| Attribution: drill (A) | `test_a_documented_step6_drill_is_category_a` |
| Valid synthetic smoke (B), historical and fresh | live-shaped test |
| Missing evidence → UNRESOLVED, named | `test_an_execution_without_evidence_is_unresolved_and_named` |
| Record not visible → UNRESOLVED | `test_an_execution_whose_record_is_not_visible_is_unresolved` |
| Non-synthetic, wrong facility, OPEN after finish, APPROVED but pending, decided by SYSTEM | `test_a_record_that_contradicts_its_execution_fails` (5) |
| Failed execution | `test_a_failed_execution_fails` |
| Smoke for the wrong facility or shipment | `test_smoke_evidence_for_the_wrong_facility_or_shipment_fails` |
| Duplicate evidence | `test_duplicate_evidence_fails` |
| Smoke that did not pass | `test_a_smoke_record_that_did_not_pass_cannot_attribute` |
| Category C exactly two; third id; no confirmation; DB mismatch (status, appointment, window, decider, type); history mismatch | `test_category_c_*`, `test_a_third_execution_in_the_owner_record_rejects_the_whole_record`, `test_an_owner_record_without_the_owner_confirmation_is_rejected` |
| C is never smoke evidence | `test_category_c_is_never_smoke_evidence_and_without_a_fresh_smoke_the_gate_fails` |
| Fresh smoke: wrong release SHA, wrong code, wrong environment, stale, failed, missing check, old format | `test_the_fresh_smoke_must_be_bound_to_the_release_and_deployment` (7) |
| Evidence writer: wrong code hash, stale, environment, release binding, BOOKING_WORKFLOW not PASS, approval mismatch | `test_a_renewal_report_is_refused_unless_bound_and_fresh` (5), `test_a_renewal_report_whose_booking_workflow_did_not_pass_is_refused`, `test_the_renewal_approval_must_be_the_one_this_run_uses` |
| Exceptions keep scope | `test_the_approved_exceptions_keep_their_scope_in_renewal_wording`, `test_a_valid_renewal_report_…` (exceptions never become evidence rows) |
| RLS: authorized collector, unauthorized roles, cross-facility, restricted fields, policies unchanged | `test_0029_*` (4 tests, 5 roles) |
| Smoke release binding, no overwrite | `test_the_smoke_test_is_release_bound_and_never_overwrites_the_23_nov_record` |
| Smoke behaviour (booking, duplicate, double-booking, authorization, isolation, kill switch, approval, audit) | the unchanged script's own 11 checks; **not run** (it writes to the live pilot). Locally, the same behaviours are covered by `test_option_d.py`, `test_approvals.py`, `test_concurrency.py` and `test_api.py` in the full regression |
| Immutable evidence rows | existing `test_gate_evidence.py::test_application_roles_cannot_write_evidence` and the 0013/0014 triggers (unchanged) |

## 3. Not verified (needs the pilot, outside this task)

* The renewal collector against AWS: needs 0029 deployed. Until then the gate reports every execution UNRESOLVED with
  the record-access blocker, so it fails closed.
* Database verification of `73cac501`, `87915a37`, `29ffac03`: same dependency.
* A fresh renewal-format smoke run: writes to the live pilot, so it wasn't run.
* The workflow's YAML is validated by GitHub on push (local PyYAML is broken). The static tests pin its content.

## 4. Defects found during validation

First full run: **1374 passed, 1 failed, 2 skipped** (42 min 33 s). One root cause:

| Root cause | Test | Resolution |
|---|---|---|
| 0029 grants `setuhaul_deploy_evidence` EXECUTE on a new function. The existing security pin keeps an **exact allow-list** of the functions that role may execute. | `tests/test_agent_not_operator.py::test_deploy_evidence_role_can_only_insert_evidence` | This privilege is the intended, owner-authorized one (narrow, read-only evidence access). It was added to the allow-list with a `# 0029` comment, the same way 0014 and 0026 extended it. The test is still an exact allow-list (`<=` over named functions), and its table and column assertions are unchanged. The PR calls this out for review. |

Earlier, while writing the tests, the schema caught a design error before any commit: a final approval always records
`decided_by` (including the workflow's own expiry), so the first draft of 0029 would have labelled a SYSTEM expiry as
`console_user`. Fixed: `decider_kind` is `system` when `decided_by_type = 'SYSTEM'`. A test pins it.
