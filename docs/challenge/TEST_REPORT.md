# Consolidated verification — 23 Nov 2026

One consolidated run after all the challenge-completion work was integrated on branch `phase-f/challenge-completion`.
- **Machine:** Windows 11, Python 3.14.
- **Database:** brand-new PostgreSQL 18.6 clusters created for this run. Reused clusters keep cluster-wide `setuhaul_login_*` roles, which make the role-grant tests fail.
- **Constraints:** no network, no AWS, no paid model calls.

| # | Check | Command | Result | Time |
|---|---|---|---|---|
| 1 | Full repository suite (includes scheduling, `move_appointment`, booking flow and approvals, concurrency and integrity, recovery and outbox, gate evidence, scenario run, infrastructure synthesis and security assertions, console site and its security headers) | `SETUHAUL_TEST_DSN="host=localhost port=55435 user=postgres" python -m pytest tests -q -p no:cacheprovider -rfEs` | **1,135 passed, 0 failed, 2 skipped** | 18 min 47 s |
| 2 | Data API mode: CI's list plus the new database tests (`test_move_appointment`, `test_gate_evidence`, `test_scenario_run`) | `SETUHAUL_TEST_DATA_API=1 SETUHAUL_TEST_DSN="…port=55436…" python -m pytest -q tests/test_api.py tests/test_db_requests.py tests/test_approvals.py tests/test_orchestrator.py tests/test_outbound.py tests/test_inbound_delivery.py tests/test_concurrency.py tests/test_data_api.py tests/test_aws.py tests/test_console_threads.py tests/test_move_appointment.py tests/test_gate_evidence.py tests/test_scenario_run.py` | **426 passed, 0 failed** | 10 min 33 s |
| 3 | Skip 1 of run 1, rerun so it actually executes: Aurora-like non-superuser owner on an empty cluster | `SETUHAUL_TEST_FRESH_DSN="…port=55437…" python -m pytest -q tests/test_aurora_owner.py` | **5 passed** | 6 s |
| 4 | Lambda build, CDK synthesis, console secret scan, assembly check (CI synth job) | `python scripts/build_lambda.py && python infra/app.py && python -m setuhaul.ops.console_config --check setuhaul/api/console_static && python -m setuhaul.ops.assembly cdk.out --zip build/lambda.zip` | exit 0. Console: 0 problems. Assembly: 11 functions checked, built zip sha256 `4437e51f…` | — |
| 5 | Integrity proof script (Break) | `python scripts/integrity_proof.py 20` | 9 of 9 checks. 1 claim on the slot, 0 overlaps (`evidence/integrity_proof.txt`) | — |

**After the runs:** two byte-level changes were made, with no change to behaviour.
- `migrations/0025_schedule_proposals_and_move.sql` was converted from CRLF to LF, to match every other migration, before its first commit.
- Line endings were restored in seven edited Python and Markdown files.

The migration's dependents were rerun:
- Normal mode, `tests/test_migrations.py test_migrate_data_api.py test_move_appointment.py test_scheduling.py test_agent_not_operator.py test_approvals.py test_outbound.py`: **192 passed**.
- Data API mode, `test_move_appointment.py test_scenario_run.py`: **23 passed**.

**The second skip in run 1:** `test_data_api.py:155` runs only when the whole suite goes through the Data API backend, and is skipped by design otherwise. It is the same skip CI has.

**Not run here:**
- CI's Ubuntu job on PostgreSQL 17. It runs when this branch is pushed; see the final report for its result.
- Anything against AWS that changes state. The live checks of 22–23 Nov were read-only: `docs/release/13_STEP6_GATE_EVIDENCE.md`, `docs/release/evidence/security_live.json`.
- `pip_audit` (it needs the network).

**During development** (not the authoritative result):
- The Break worker ran 20 test files one at a time: 881 passed and 1 skipped (`evidence/break_test_runs.log`). Other workers were editing files during part of that window.
- The scheduling worker's focused run on a reused cluster showed 3 role-grant failures caused by leftover login roles. Those three tests pass in run 1 above.
