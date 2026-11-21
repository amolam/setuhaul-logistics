# Phase C: test report

## Phase E update (22 Nov 2026, branch `phase-e/aws-implementation`)

| | |
|---|---|
| Full suite | **924 passed, 1 skipped**, 0 failed, 0 deselected, 1,160 s (19 min 20 s), 00:07 → 01:27 IST |
| The skip | `test_data_api.py::test_the_suite_really_goes_through_the_data_api_backend`: runs only in the Data API mode below (by design) |
| New tests (60) | `test_data_api.py` (Data API backend), `test_aws.py` (Lambda entry points, Step Functions adapter, least-privilege logins), `test_infra.py` (CDK template assertions, manifest, WhatsApp templates), `test_migrate_data_api.py` (migrations and logins over the Data API) |
| Database tests through the RDS Data API backend (emulated): `test_api`, `test_db_requests`, `test_approvals`, `test_orchestrator`, `test_outbound`, `test_inbound_delivery`, `test_concurrency`, `test_data_api`, `test_aws` with `SETUHAUL_TEST_DATA_API=1` | First run: 343 passed, **1 failed** (`test_turn_facts_are_written_only_through_the_checked_function`: the refusal arrived with the right SQLSTATE but not as `psycopg.errors.InsufficientPrivilege`). Fixed: Data API errors now arrive as the exact psycopg class for their SQLSTATE (3 new tests). Re-run after the fix: **347 passed**, 0 failed, 17 min 03 s |
| After that last code change | the four AWS test files in normal mode: **63 passed, 1 skipped** (the fix touches only Data API error translation, which the psycopg path does not use; the full normal suite above predates it) |
| External services | none: AWS, Meta and OpenRouter are stubs or emulators; no paid call |

The section below is the release report for `v0.1.0-fde-pilot` (21–22 Nov), kept as written.


## Full suite (one run, after the last code change)

| | |
|---|---|
| Date | 21–22 Nov 2026, 23:47:02 → 00:05:26 IST |
| Command | `SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres" python -m pytest tests -q -p no:cacheprovider -rs -W default` |
| Environment | Windows 11, Python 3.14.7, PostgreSQL 18 local test cluster (port 55432), dependencies from `requirements-dev.txt` after the Phase D upgrades (FastAPI 0.165.0, Starlette 1.10.0, PyJWT 2.15.1, cryptography 50.0.2, pytest 9.2.0, httpx2 2.16.2) |
| **Passed** | **864** |
| Failed | 0 |
| Skipped | 0 |
| Deselected | 0 |
| Duration | 1,103 s (18 min 23 s) |
| Warnings | 10, all `ResourceWarning` (shown because of `-W default`; e.g. an unclosed SQLite connection in `test_golden.py`). None affects a result |
| External services | none called: OpenRouter is an httpx `MockTransport`, Bedrock and Secrets Manager are stubs, SQS is `LocalQueue` or a stub, WhatsApp is `LocalProvider`. No paid model call |
| Label | **verified** (run and read here) |

History for comparison: 743 (before the restructure), 800 (after Phase B), 842 (R1/R4), 862 (ledger-status fix), 864
(`/health`).

Also run during this work (subsets, not a substitute for the full run): the 132 provider tests after the ledger-status
fix; 392 API, agent and provider tests right after the dependency upgrade; the changed demo console in rules mode on a
throwaway database (races with N = 2, 10 and 50: exactly one winner each, every losing driver told, zero overlapping
dock claims, zero model calls); and, in the final review, 394 API, agent and provider tests in a clean virtual
environment installed only from the pinned requirement files (`PYTHONNOUSERSITE=1`), to show the pins are portable.

## Coverage of the required areas

| # | Area | Tests |
|---|---|---|
| 1 | Unit tests | `test_agent_units.py`, `test_eta.py`, `test_feasibility.py`, `test_phrase_checks.py`, `test_budget.py` |
| 2 | Agent extraction and orchestration | `test_agent_units.py`, `test_orchestrator.py`, `test_replay.py` (golden set end to end) |
| 3 | Hindi and Hinglish | `test_agent_units.py`, `test_orchestrator.py`, `test_outbound.py` (language-matched templates) |
| 4 | Numbered shipment selection | `test_agent_units.py`, `test_orchestrator.py`, `test_api.py` |
| 5 | ETA and slot logic | `test_eta.py` (property checks), `test_feasibility.py` (differential against the Phase 1 prototype), `test_pg_view.py` |
| 6 | Booking eligibility | `test_booking_flow.py`, `test_db_requests.py`, `test_confirmation_policy.py` |
| 7 | Approval invariants | `test_approvals.py` (B-WF-2), `test_confirmation_policy.py` (B-WF-2a, rules off by default) |
| 8 | PostgreSQL transactions | `test_db_requests.py`, `test_migrations.py`, `test_agent_not_operator.py` |
| 9 | Concurrency and locking | `test_concurrency.py`, `test_api.py` (50 drivers race for one slot), `test_approvals.py`, `test_providers.py` (concurrent budget reservations) |
| 10 | Idempotency | `test_db_requests.py`, `test_api.py`, `test_approvals.py`, `test_outbound.py`, `test_inbound_delivery.py` |
| 11 | Outbox and notification retries | `test_outbound.py` (B-WF-3), `test_inbound_delivery.py` (B-WF-1) |
| 12 | Provider timeout and failure | `test_providers.py` (retries, 429/5xx/timeouts, budget refusal, rules-reader fallback through the orchestrator) |
| 13 | OpenRouter response validation | `test_providers.py` (malformed shapes, usage/cost/provider metadata, unusable answers settled as errors) |
| 14 | Bedrock adapter contract | `test_providers.py::test_openrouter_and_bedrock_give_the_same_validated_extraction`, `test_agent_units.py` (stubbed client; no real Bedrock call: **blocked**, the AWS CLI is not signed in) |
| 15 | Security and configuration | `test_providers.py` (fail-closed selection, Secrets Manager, key never in logs), `test_api.py` (tokens, roles, `/health`), `test_inbound_delivery.py` (booking switch start-up gate), `test_agent_not_operator.py` |

## Not independently reproducible here

- AWS behaviour (SQS, Aurora, Step Functions, Cognito, IAM): not deployed; covered by the step 6 gates.
- Real Bedrock and real OpenRouter calls: deliberately not made (no paid calls without approval).
- Real WhatsApp delivery: not tested; local provider only.
