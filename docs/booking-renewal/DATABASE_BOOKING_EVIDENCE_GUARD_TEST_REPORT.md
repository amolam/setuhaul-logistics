# Database-side booking evidence guard: test report

Branch `feature/database-booking-evidence-guard` (base `main` `e280a40`). Local validation, 24 Nov 2026. Nothing
deployed, no workflow triggered, the migration not applied to any live database.

## 1. Environment and commands

* PostgreSQL **18.6** (local, `localhost:55432`). CI runs **17.9**, the pilot's engine, where the T33-P branch of the
  exception rule is exercised. Python 3.14, psycopg 3.
* The guard tests run against real PostgreSQL in a **dedicated database** (`setuhaul_build_guard`), plus a second
  throwaway database for the migration-enablement test. Manifests are append-only, so nothing leaks into other tests'
  databases. Only `test_both_database_backends_declare_the_deployment_in_every_transaction` uses the Data API emulator
  (`tests/data_api_emulator.py`), and that emulator also runs against real PostgreSQL.

```
python -m pytest tests/test_booking_evidence_guard.py -q
python -m pytest tests -q                                      # full regression
SETUHAUL_TEST_DATA_API=1 python -m pytest -q <ci.yml list>      # Data API backend
```

## 2. Results

| Suite | Result |
|---|---|
| `tests/test_booking_evidence_guard.py` | **39 passed** (31 s), 0 failed, 0 skipped |
| Neighbouring suites (kill switch/activation, moves, booking switch, gate evidence, security pins, migrations, Data API) | 121 passed, 1 skipped (the pre-existing Data-API-only test) after the fix in §5 |
| Full regression | **1350 passed, 0 failed, 2 skipped** (21 min 3 s), final run after the fix in §5 (= `main`'s 1311 + the 39 new tests). The skips are pre-existing: `test_aurora_owner.py:126` (needs an empty cluster; CI covers it), `test_data_api.py:155` (Data API mode only) |
| Data API backend (`ci.yml` list) | **559 passed** (11 min 12 s, `SETUHAUL_TEST_DATA_API=1`). Run before the final `Db.tx` fix, which touches only the psycopg path, not `DataApiDb` |

## 3. Concurrency and security results

* **Manifest vs in-flight commitment:** a manifest insert was observed waiting on the advisory lock (`pg_locks`, not
  granted) until the booking transaction committed, then completed.
* **Commitment vs in-flight manifest:** a commitment by the old deployment waited on the lock. When the manifest
  committed, it read the new manifest and was refused `DEPLOYMENT_SUPERSEDED`.
* **Expiry:** a container's deployment, still declared after its evidence expired, was refused. A transaction that
  started before expiry and booked after it was refused (statement-time database clock). The boundary is exclusive:
  −1 µs valid, 0 and +1 µs expired.
* **Security:**
  * no PUBLIC execute on any guard function; the definer functions are owned by `setuhaul_rpc`, which is
    NOBYPASSRLS and not superuser;
  * no application role and not the evidence identity can execute the verdict or UPDATE the guard row;
  * operations (console role) can't turn it off, and the owner can't turn it off either (one-way);
  * delete and truncate are refused;
  * facility isolation is unchanged under valid evidence;
  * the kill switch still refuses first;
  * all pre-existing kill-switch and security tests pass.

## 4. Acceptance traceability (`tests/test_booking_evidence_guard.py`)

| Requirement | Test |
|---|---|
| valid manifest + evidence permits a booking | `test_a_current_deployment_with_pass_evidence_may_book` |
| valid evidence + approved exception, only in scope | `test_the_pilot_may_book_with_pass_evidence_plus_its_owner_approved_exceptions`, `test_exceptions_never_stand_in_outside_a_pilot_environment`, `test_t33p_is_accepted_only_on_postgresql_17` |
| booking works while the kill switch is disengaged | `test_the_kill_switch_still_refuses_first_even_with_valid_evidence` (books after disengaging) |
| missing manifest | `test_no_manifest_for_the_environment_is_refused` |
| missing evidence | `test_a_manifest_without_evidence_is_refused_gate_by_gate` |
| expired evidence | `test_expired_evidence_is_refused` |
| evidence of a previous deployment; superseded manifest | `test_evidence_of_a_previous_deployment_does_not_authorize_the_next` |
| different artifact hash | `test_a_caller_on_other_code_is_refused` |
| different environment | `test_evidence_of_another_environment_does_not_count` |
| invalid / missing approval | `test_a_missing_or_inconsistent_owner_approval_is_refused` |
| evidence lookup failure | `test_an_evidence_lookup_that_cannot_read_is_refused` (`ACCESS_DENIED`) |
| database error during lookup | `test_a_database_error_during_verification_is_refused` (`VERIFICATION_FAILED`) |
| missing / inconsistent identity | `test_an_undeclared_or_partly_declared_caller_is_refused` |
| rollback without fresh evidence | `test_a_rollback_cannot_reuse_the_earlier_evidence_of_the_same_deployment` |
| warm container after expiry | `test_a_warm_container_cannot_book_after_its_evidence_expires` |
| just before / at / just after expiry | `test_expiry_is_exclusive_at_the_database_instant` (3 cases), `test_the_guard_reads_the_database_clock_not_the_transaction_start` |
| concurrent manifest update and booking | `test_a_new_manifest_waits_for_an_in_flight_commitment`, `test_a_commitment_that_races_a_new_manifest_sees_it_and_is_refused` |
| concurrent expiry and booking | time-based at statement time (the two expiry tests). There is no lock window to race |
| verification unavailable | the two lookup-failure tests (the database itself is the evidence service) |
| snapshot isolation | `test_a_commitment_outside_read_committed_is_refused` |
| API updated / agent not, and the reverse | `test_a_partly_updated_deployment_is_refused_for_the_stale_side[agent|api]` |
| manifest never recorded / incomplete deployment | `test_a_deployment_whose_manifest_was_never_recorded_cannot_book` |
| kill switch engaged (with valid evidence) | `test_the_kill_switch_still_refuses_first_even_with_valid_evidence`; trigger order in `test_the_guard_uses_the_kill_switch_definition_of_a_commitment` |
| production can't use exceptions; independently disabled | `test_production_is_refused_whatever_its_evidence` |
| exception scope can't be widened | `test_an_exception_cannot_be_widened_through_evidence_data` |
| cancellation, expiry, release, metadata, progressions | `test_reductions_progressions_and_metadata_need_no_evidence` |
| no update bypasses the guard (re-confirm, re-activate, change time) | same test (three refusals) |
| rescheduling per existing policy | `test_a_reschedule_through_the_console_is_a_guarded_commitment` (real `move_appointment`) |
| unauthorized role / operations can't execute or bypass | `test_no_role_can_call_the_guard_functions_or_switch_the_guard` |
| cross-facility | `test_valid_evidence_does_not_widen_facility_scope` |
| no RLS bypass | `test_no_role_can_call_…` (rpc NOBYPASSRLS), RLS pins in `test_agent_not_operator.py` |
| same gates as the start-up check | `test_the_guard_checks_the_same_gates_as_the_start_up_check` |
| ON where a deployment is hosted, OFF on a fresh DB | `test_0030_turns_the_guard_on_only_where_a_deployment_is_recorded`, the fixture's fresh-DB assertion |
| both backends declare the deployment | `test_both_database_backends_declare_the_deployment_in_every_transaction` |
| the app declares what its start-up check accepted | `test_the_app_declares_the_deployment_its_start_up_check_accepted` |
| diagnostics correlate without PII | `test_a_rejection_names_the_deployment_and_correlation_but_no_booking_data` |

## 5. Defects found during validation

| Root cause | Test | Resolution |
|---|---|---|
| The new single-row table has no RLS (like the kill switch). The schema pin requires every table to force RLS or carry a written reason. | `test_agent_not_operator.py::test_every_table_is_rls_forced_or_reasoned` | Added `booking_evidence_guard` to the reasoned list, with its reason (no facility data, no application privilege, read only by the trigger). Still an exact list. |
| First full run: **1349 passed, 1 failed** (`test_api.py::test_fifty_drivers_race_for_one_slot`). 48 of the 50 threads hit `401 ExpiredSignatureError`: the drivers' 15-minute session tokens expired before their hold calls, because the suite ran slowly. The guard wasn't involved: that database is fresh, so the guard is OFF and the trigger returns before reading anything. But my `Db.tx` change had added a round trip to every local transaction. | `test_api.py::test_fifty_drivers_race_for_one_slot` | Folded all of `Db.tx`'s `set_config` calls into **one** statement, which is fewer round trips than `main` (two became one). The test passed 3 of 3 runs alone (about 2 min each), and the full regression was rerun (§2). |
| Two of my own test-helper issues: `RESET ROLE` in a `finally` hid an expected CheckViolation, and two parametrized cases booked the same dock slot (correctly refused by the exclusion constraint). | `test_booking_evidence_guard.py` | Helper and slots fixed; no assertion weakened. |

## 6. Known limitations

See the design, §13: the self-declared deployment identity (a trust boundary unchanged from today); fingerprints checked
at start-up only; the gate list repeated in SQL (pinned); a fresh environment starts OFF until the owner enables it; the
live pilot isn't exercised (not deployed by design).

## 7. Review update (PR #6 feedback, 24 Nov 2026)

### 7.1 Changes
* **Production detection is case-insensitive, and environment classification is strict.** The new
  `booking_environment_class` makes any name containing "prod" in any letter case production; anything else that isn't
  canonical (`^[a-z0-9-]{2,32}$`) is invalid. It's used by both `booking_evidence_verdict` (`PRODUCTION_DISABLED` /
  `ENVIRONMENT_INVALID`) and `booking_exception_current`. The pilot-exception restrictions are unchanged (pilot only,
  the 3 pairs, T33-P only on 17.x).
* **Startup declaration log line** (api and agent), for the rollout verification.
* **Runbook:** `DATABASE_BOOKING_EVIDENCE_GUARD_RUNBOOK.md`.
* No Data API code changed in this update.

### 7.2 New tests (all with otherwise-valid evidence where the scenario allows)
| Test | Cases |
|---|---|
| `test_production_in_any_letter_case_is_refused_even_with_otherwise_valid_evidence` | 14: `prod`, `production`, `PROD`, `Prod`, `Production`, `PRODUCTION`, `pRoD`, `ProDuction`, `prod-ap-south-1`, `Prod-AP-South-1`, `my-production`, `my-PRODUCTION-2`, `preprod`, `pilot-prod`. Complete PASS evidence is recorded for the lower-case name (the evidence tables accept nothing else), and the declared name is refused `PRODUCTION_DISABLED`. A mixed-case manifest is refused by the table CHECK; no exception can be recorded for any of them; `booking_exception_current` is false for each |
| `test_the_environment_classification_is_strict` | 11 names → production / pilot / other / invalid |
| `test_a_non_canonical_environment_is_refused_even_with_valid_evidence_for_its_twin` | 3: `Test-Env`, `PILOT-AP-SOUTHEAST-2`, `Pilot-ap-southeast-2` → `ENVIRONMENT_INVALID`, while the canonical twin books |
| `test_a_guard_refusal_reaches_the_driver_and_the_planner_like_a_kill_switch_refusal` | the agent's tool status and the console code for a guard refusal |
| `test_the_app_declares_the_deployment_its_start_up_check_accepted` (extended) | the exact startup log line |

Guard tests: **68** (39 before review + 29).

### 7.3 Results
| Run | Where | PostgreSQL | Result |
|---|---|---|---|
| guard + security pins + kill switch + booking switch + migrations (targeted) | local | 18.6 | 127 passed |
| full regression | local | 18.6 | **1379 passed, 0 failed, 2 skipped** (26 min 51 s; = 1350 + the 29 review tests; pre-existing skips) |
| GitHub CI run [45334016050](https://github.com/amolam/setuhaul-logistics/actions/runs/45334016050) on `e68f3b6` (before review) | GitHub Actions | **17.9** (`postgres:17.9`) | `test`: **1351 passed, 1 skipped** (full suite, incl. the 39 guard tests and the T33-P path on 17.9) + Data API step **559 passed**; `synth` passed; `deploy` skipped |
| GitHub CI on the review commit | GitHub Actions | 17.9 | «CI2» |

Local and CI results are kept separate. The T33-P exception rule is exercised on PostgreSQL 17.9 only in CI
(`test_t33p_is_accepted_only_on_postgresql_17` and the pilot positive test take the 17.x branch there).
