# Phase G.1 final validation report

Branch `feature/phase-g1-langsmith-model-reliability` from `main` `951a320`. Validation date 23 Nov 2026.
All results below are **local** unless a section says "AWS pilot (observed, read-only)". Nothing was deployed, no secret
was read or changed, no paid model call was made, and the release branch was not touched.

## 1. Summary table

| Area | Result |
|---|---|
| Full regression (local PostgreSQL 18.6) | **1311 passed, 0 failed, 2 skipped** (after the fixes in §2.3; first run 1306 / 4 failed) |
| Data API backend (emulated, CI file list) | **559 passed, 0 failed, 0 skipped** |
| Phase G evaluation, 67 scenarios | **67 / 67 pass** (baseline 65 / 67): I14 and I15 now pass, 0 newly failing |
| G.1 evaluation dataset, 111 examples, 15 categories | **111 / 111 pass**, 0 failing evaluators; dataset `g1-710318ed7e5e`, evaluators `g1-eval-1`, cost $0 |
| Rules reader (37 cases) | intent 37/37, deadline 12/12, arrival 20/20, false-positive delay **0 / 17 (0 %)** |
| I14 "Need to finish unloading by 5 pm" | **PASS**: deadline 17:00 +05:30, kind `UNLOAD_END`, intent `ASK_OPTIONS`, no ETA |
| I15 "pahunch gaya hoon gate pe" | **PASS**: arrival now (`EARLY_ARRIVAL`), not a delay |
| OpenRouter errors (pilot) | root cause found: the secret holds the CloudFormation-generated placeholder (HTTP 401). Owner action O1 |
| Budget refusals (pilot) | root cause found: residue of the e2e drill A6 on 22 Nov UTC; budget code correct. Fixed in the drill script |
| Tracing overhead | about 1.3 ms per turn to record 13 runs (p50; p95 1.5 ms); real turns p50 939 ms off vs 952 ms on (within run-to-run noise); flush bounded (§6) |
| LangSmith down / slow / buggy | turn result identical; flush bounded (tested) |
| PII / secrets in traces | none (redaction + export guard; tested on a real turn) |
| Default deployment | CDK template identical to `main` apart from the code asset (122 resources each, proven by synth) |

## 2. Tests

### 2.1 Full regression (local PostgreSQL 18.6, `python -m pytest tests`)

**1311 passed, 0 failed, 2 skipped** in 22 min 28 s (final run, after the fixes in §2.3). Phase G's final report
had 1202 passed / 2 skipped; the increase is the new G.1 tests (`tests/test_phase_g1.py`, plus additions in
`test_infra.py` and `test_providers.py`). No existing test was removed or relaxed. Two existing provider tests were migrated
to the new category names: they assert `(category, detail) == (normalize(old), old)`, so they now check both the new
category and the old name. The malformed-usage test now asserts that the trace tokens equal the ledger tokens.

Skipped (both pre-existing, unchanged):
* `tests/test_aurora_owner.py:126`: `SETUHAUL_TEST_FRESH_DSN` not set (needs an empty PostgreSQL cluster; CI covers it).
* `tests/test_data_api.py:155`: only runs when the suite goes through the Data API.

### 2.2 Data API backend (emulated, `SETUHAUL_TEST_DATA_API=1`, the CI list including `tests/test_phase_g1.py`)

**559 passed, 0 failed, 0 skipped** in 11 min 22 s, run after the fixes in §2.3 (Phase G: 453 passed; the increase is
`tests/test_phase_g1.py` added to the CI list).

### 2.3 Defects found by the final validation, grouped by root cause, and fixed

The first full run gave **1306 passed, 4 failed, 2 skipped** (21 min 37 s). The 4 failures had three root causes, all
in G.1 code. Each was fixed in the code. No test was weakened, and the Phase G tests are unchanged.

| # | Root cause | Failing tests | Fix | Proof |
|---|---|---|---|---|
| V1 | The new console/route shipment pattern allowed upper case only. Shipment ids are case-sensitive and some sources use lower-case hex (`SHP-99399e2f-0`), so the route answered 422 for real ids. | `test_phase_g.py::test_planner_gets_structured_feasible_recommendations_and_the_run_is_recorded`, `::test_recommendations_scope_and_roles` | Pattern `^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$` (server and console, still kept equal by a test). My own G.1 test had encoded the wrong assumption ("lower case → 422"). It now asserts that mixed-case ids pass and that malformed ids get 422: too short, leading dash, dot, space, `;`, over 80 characters. | targeted run: pass |
| V2 | `DeferredModel` wrapped "no model configured" (provider `none`, factory returns `None`), and the first extraction hit `None.extract` → the agent Lambda failed the SQS message. | `test_aws.py::test_a_driver_message_through_the_api_lambda_and_the_agent_lambda` | The Lambda passes no model when none is configured, as before G.1. `DeferredModel` now raises a `ModelError` (`not_configured`), never an `AttributeError`. New test `test_no_model_configured_is_not_an_error_and_never_crashes`. | targeted run: pass |
| V3 | G.1 made the webhook's correlation id the turn's correlation id. Phase G's invariant is ONE id per turn, shared by the turn metrics, the optimizer run and the logs. One webhook can carry several messages, and a message can be redelivered, so that id was no longer unique per turn. | `test_phase_g.py::test_driver_options_come_from_the_optimizer_and_the_turn_is_measured` | The turn's id is again its own: `turn_ref = uuid5(webhook id : message : generation)` = `correlation_id` = trace id. The webhook id is carried separately as `ingest_correlation_id` on every agent log line and on the trace, so API and agent logs still join. The G.1 test now asserts both ids. | targeted run: pass |

Targeted rerun after the fixes: `test_phase_g1.py`, `test_providers.py`, `test_phase_g.py` and the failing Lambda
test: **253 passed**. Then the full regression was run again (§2.1).

## 3. Evaluation (local, $0, scripted model)

Command `python -m evals.langsmith.run`. Report `evals/results/g1_langsmith_eval_20261123_1426.json`. The Phase G
runner's own report is `evals/results/phase_g_eval_20261123_1425.json`.

* **Phase G 67 scenarios: 67 pass / 0 fail** (baseline `phase_g_eval_20261123_1024.json`: 65 / 2). Newly passing: **I14,
  I15**. Newly failing: none. Missing: none.
* **G.1 dataset: 111 examples, 111 pass**. That is 67 Phase G scenarios + 7 G.1 conversations (F01–F04 fallback per
  failure category, X01–X02 expired slots, P01 idempotency) + 37 rules-reader cases.

| Category | Pass / total |
|---|---|
| intent | 8 / 8 |
| tool_selection | 6 / 6 |
| parameter_extraction | 5 / 5 |
| eta_delay | 6 / 6 |
| deadline | 12 / 12 |
| arrival | 15 / 15 |
| optimization | 12 / 12 |
| booking | 7 / 7 |
| approval | 4 / 4 |
| authn_authz | 9 / 9 |
| cross_facility_isolation | 5 / 5 |
| idempotency | 2 / 2 |
| expired_slots | 3 / 3 |
| hindi_hinglish | 11 / 11 |
| model_unavailable_fallback | 6 / 6 |

Evaluators (pass / fail / not applicable): intent 53/0/4, arrival 20/0/6, deadline 12/0/0, eta 5/0/1, parameters
9/0/7, tool_selection 9/0/4, optimizer_invoked 5/0/13, hard_constraints 7/0/5, no_unauthorized_booking 20/0/2,
no_fabricated_confirmation 4/0/3, approval_required 4/0/0, authorization 14/0/0, no_cross_facility_leak 6/0/11,
idempotency 2/0/0, expired_offers 3/0/0, fallback 6/0/0. **Failure inspection list: empty.**

Failure-category distribution exercised by the fallback rows (F01–F04 and the Phase G model-failure rows): each
injected category is recorded as `fallback_reason` with options still offered. The full 16-category table is in
OPENROUTER_PILOT_DIAGNOSIS.md §5. Every category class (not started, budget refusal, provider failure) is covered by
`tests/test_phase_g1.py`.

LLM judge: off (no approved budget). LangSmith sync/experiment: **not run** (no key; uploading datasets to a third
party is an owner decision). The sync's call sequence is tested against a mock transport.

## 4. OpenRouter diagnosis (AWS pilot, observed read-only on 23 Nov 2026)

Full evidence: OPENROUTER_PILOT_DIAGNOSIS.md.

* **Secret** `setuhaul/pilot/openrouter`: present, in ap-southeast-2, one version (`AWSCURRENT`), never changed since
  creation. It is **non-empty**: it holds the random 32-character value CloudFormation generated
  (`GenerateSecretString: {}`). Retrieval works (role and KMS OK, `LastAccessedDate` 23 Nov). OpenRouter rejects the
  value with **HTTP 401** in about 15 ms. That is the 27 errors: $0, no tokens, correctly accounted. Value not read.
* **Budget**: the 8 refusals at $0 on 22 Nov UTC come from e2e drill A6, which set that UTC day's `spent_usd` to the cap
  ($0.25) and left it there. `reserved_usd` = 0, so no reservation leaked. Budget code correct. The drill now gives the
  amount back in a `finally` block.
* **Code gaps fixed (not deployed)**: a secret problem no longer stops every turn (DeferredModel; the rules reader
  answers and the category is recorded); the key is re-read after a 401 (at most every 5 min), so the owner's key is
  picked up without a redeploy; the key's shape is logged (never its value) and alerts if it is not an OpenRouter key;
  secret errors no longer carry the ARN; tokens are NULL (not 0) when not reported; the 16-category taxonomy; the
  console splits errors, refusals, never-started and unsettled, and provider-reported vs estimated cost.
* **Controlled verification**: the procedure is written (OPENROUTER_PILOT_DIAGNOSIS.md §8) and **not executed**. It
  needs the owner's key, an approved spend (about $0.01) and the deployment of this branch.

## 5. Tracing validation (local)

* Hierarchy: one trace per turn delivery. Spans: `agent_turn` → `intent_extraction` → (`model_call` | `rules_reader`),
  `parameter_check`, `decide` → tool spans → `optimizer`, and `final_response`. All runs share the trace id = `turn_ref`
  = the turn's `correlation_id`; the webhook id is in `ingest_correlation_id` (test
  `test_a_turn_is_traced_with_hierarchy_correlation_and_no_personal_data`).
* PII redaction: the driver's phone (with and without `+`) and message text are absent from the exported payload of a
  real turn. Unit tests cover dropped keys, scrubbing (JWT, Bearer, `sk-or`, `lsv2_`, AKIA, AWS secret, Meta token,
  email, ARN account ids, phones), the size limits and the export guard.
* Failure isolation: with LangSmith refusing, timing out, or the tracer raising inside a span, the turn's reply, tools,
  ledger rows and metrics are identical to a turn with tracing off (tested). The Lambda flush never raises (tested).
* Not verified against LangSmith Cloud (no key). The REST payloads follow the documented `/runs/batch` schema and
  are checked against a mock transport.

## 6. Performance (local)

Command `python perf/g1_trace_overhead.py`. Report `evals/results/g1_perf_20261123_1500.json`. Local workstation,
fresh local PostgreSQL, scripted model. The exporter is an in-process capture, or one that fails or is slow; no network.

| Measure | Result |
|---|---|
| Recording cost (2000 synthetic turns × 13 runs, incl. redaction) | p50 **1.30 ms**, p95 1.53 ms per turn |
| Real turns, tracing off (n=30) | p50 939 ms, p95 1075 ms |
| Real turns, tracing on, capture exporter (n=30) | p50 952 ms, p95 1009 ms |
| Real turns, tracing on, failing exporter (n=30) | p50 956 ms, p95 984 ms |
| Overhead, p50 on − off | 13 ms (1.4 %). That is within run-to-run noise: p95 was lower with tracing on. The turn time is dominated by the local database. |
| Flush, exporter always failing (budget 0.5 s) | returns in 0.0 s: 2 attempts per batch, 5 runs dropped and counted, 10 export errors, no exception |
| Flush, exporter taking 1 s per post (budget 0.5 s) | returns after **1.0 s**: 1 batch sent, 4 left pending (dropped on the next bounded flush or at the queue cap) |
| Cold start | orchestrator import 728 ms (pre-existing); the G.1 modules add **0.6 ms**; 37 KB of new source; **no new dependency** |

Finding: the flush budget is checked between posts, so a post already in flight can overrun it by its own duration.
In production one post is capped at `EXPORT_TIMEOUT_S = 1.0` s, so the worst-case flush after a batch is the budget + 1 s.
The flush runs after the SQS batch's outcome is decided and the reply is stored, so it never delays or changes a reply.
It only adds to the Lambda's billed time. Documented in LANGSMITH_OBSERVABILITY.md (failure handling).

## 7. Frontend (local harness, browser-checked)

Screenshots in `screenshots/` (01–08): live status pills from `/health` (booking ON, kill switch separate, "status
unavailable" on failure), model use split and reconciled, provider vs estimated cost, LangSmith links and recent turns,
optimizer format validation and a recommendation, operations → 403, and the service-failure empty state. The pattern
change in V1 widens what the console accepts (lower case). The screenshots' flows are unchanged; the static tests
assert the console and the server use the same pattern.

## 8. Kept separate

### 8.1 Local results (this report)
§2, §3, §5, §6, §7: local PostgreSQL, scripted model, local harness. $0.

### 8.2 AWS pilot observations (read-only, 23 Nov 2026)
§4 evidence E1–E8: secret metadata, agent logs, ledger rows through the Data API in a READ ONLY transaction, Lambda
configuration with values masked. `/health`: `booking_enabled: true`, kill switch `disengaged`, `version 0.2.0`.

### 8.3 Implemented, not deployed
Everything on this branch: taxonomy, DeferredModel, key refresh, migration 0028, tracing and the LangSmith exporter
(off by default), correlation ids, console G5/G6, rules reader G4, evaluation framework, drill fix. Deployment is
the owner's decision through the release process.

### 8.4 Operator actions (owner)
Numbered as in OPENROUTER_PILOT_DIAGNOSIS.md §7:
* **O1**: the owner puts the real OpenRouter key into `setuhaul/pilot/openrouter`. With this branch deployed, a warm
  agent re-reads it at most 5 minutes after its next 401. On v0.3.0 as deployed, it is picked up at the next cold start.
* **O2**: budget: leave `SETUHAUL_LLM_DAILY_BUDGET_USD` at 0.25. The 22 Nov drill residue is in the past; do not edit
  ledger rows.
* **O3**: deploy this branch through the normal release process, if approved (migration 0028 is additive). The booking
  evidence must be renewed for the new code hash, as usual.
* **O4**: optional, LangSmith: choose a plan, create a key, deploy with `-c langsmith_tracing=on -c
  langsmith_ui_url=…`, and put the key into `setuhaul/pilot/langsmith`.

### 8.5 Paid actions needing approval
* The controlled OpenRouter verification (≈ $0.01 within the existing $0.25/day cap; no budget increase).
* Any LangSmith Cloud plan or usage beyond the free tier, and any dataset upload (`--sync`).
* An LLM judge (off; needs an owner-approved budget).

## 9. Limitations
* Hindi in Devanagari script: deadline/arrival detection is for Latin-script Hinglish only.
* LangSmith Cloud ingestion not verified end to end (no key).
* `/health` still reports `version 0.2.0` (pre-existing; follow-up).
* Trace export is best effort: at most 200 queued runs, 60 per turn, a bounded flush; a dropped trace is counted and
  logged, never retried into the next turn.
