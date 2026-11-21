# Phase C: test report

## Phase E review fixes (22 Nov 2026, branch `phase-e/aws-implementation-fixes`, from `250b794`)

Both runs below were made once, one after the other, after the last code change of the fix commit. Documentation was
edited during the first run; no code changed after it started.

| Run | Command | Result | Time |
|---|---|---|---|
| Full suite, normal mode | `SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres" python -m pytest tests -q -p no:cacheprovider -rs` | **960 passed, 1 skipped**, 0 failed | 03:39:56 → 03:57:11 IST (17 min 10 s) |
| Database test files through the RDS Data API backend (emulated) | the same with `SETUHAUL_TEST_DATA_API=1`, files `test_api`, `test_db_requests`, `test_approvals`, `test_orchestrator`, `test_outbound`, `test_inbound_delivery`, `test_concurrency`, `test_data_api`, `test_aws` | **368 passed**, 0 failed, 0 skipped | 03:57:31 → 04:06:13 IST (8 min 40 s) |

The skip is `test_data_api.py::test_the_suite_really_goes_through_the_data_api_backend`, which runs only in the
Data API mode (it passed there). Tests collected: 928 at `250b794`, 961 now. The fix commit adds 33: 12 in
`test_outbound.py` (E6-01), 9 in `test_aws.py` (E4-01, IAM-01, IAM-02), 12 in `test_infra.py` (E5-01 to E5-03,
IAM-01, IAM-02, E0-01, Hinglish). The earlier 924-passed run counted 925 because it predated `250b794`'s last change,
which added the three error-class tests.

Also run locally on Windows (the pipeline sequence, no AWS call; not CI evidence):

| Step | Result |
|---|---|
| `python scripts/build_lambda.py`, twice | both zips byte-identical, SHA-256 `d0761360cdf3f1199f6fe06484d350eb498717cdcd6f69bd6c3578767138abcd` |
| `python infra/app.py` | Cloud Assembly written to `cdk.out/` |
| `python -m setuhaul.ops.assembly cdk.out --zip build/lambda.zip` | passes: 8 functions, one staged zip, staged SHA-256 = built SHA-256 = the value above |
| `npx --yes aws-cdk@2.1148.0 ls --app cdk.out` | `SetuHaulPilot` (the pinned CLI reads the assembly) |
| `python -m setuhaul.ops.manifest cdk.out <stand-in outputs>` then the assembly check with `--manifest` | passes: the manifest's `artifact_sha256` is the staged zip's |

**Not yet run:** CI on GitHub (Ubuntu): the Ubuntu build reproducibility (E5-06), synthesis and the assembly check on
the runner. It runs when the branch is pushed, which waits for the owner's approval. Nothing was run in AWS.

Evidence labels: everything above is **verified** locally or **emulated** (the Data API mode, the Step Functions,
SQS, Secrets Manager and Meta stubs); nothing is **AWS-verified**.

## Phase E update (22 Nov 2026, branch `phase-e/aws-implementation`, commit `250b794`)

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
