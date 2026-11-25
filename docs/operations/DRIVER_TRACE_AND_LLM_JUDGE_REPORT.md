# Driver traceability (Part B) and offline LLM judge (Part A): implementation report

Branch `feature/driver-trace-llm-judge`, cut from `main` at `8c018cb`.

- **Not pushed, not merged, not deployed.**
- **No AWS change.**
- **Migrations:** 0041 and 0042 have been applied to local test databases only.
- **Tracing:** not enabled anywhere.
- **Paid model calls:** none made.

| Commit | Contents |
|---|---|
| `9a01610` | Part B: driver traceability (migration 0041) |
| `377348b` | Part A: offline LLM judge (migration 0042) |

## 1. Implementation summary

**Part B.** A driver turn's LangSmith trace, its CloudWatch `agent.turn` line and its Ops metrics row now name the driver and shipment by internal id. One id, such as `DRV-JAI-0042`, therefore finds the same turn in all three places.
- **The gate.** Every id passes one format gate at the export boundary. A malformed id is exported as `[INVALID_ID]`; it is never passed through, and nothing is substituted for it.
- **What never leaves:** phone numbers, names, Telegram chat or user ids, usernames, coordinates and message text.
- **Ops view.** The Ops view has Driver and Shipment columns and a driver filter. Roles and facility scope are unchanged.

**Part A.** An offline judge gives advisory scores for clarity, tone and language match on a frozen synthetic dataset.
- **Dry run.** It does a dry run by default.
- **Paid calls.** Paid calls go only through the existing metered OpenRouter route and the shared usage ledger, behind a per-run cap of at most USD 1 and a daily cap of at most USD 3.
- **Calibration.** Calibration against human scores is ready to run.
- **The paid run.** It waits for the owner's approval.

## 2. Architecture changes

- **New:**
  - `setuhaul/observability/identifiers.py`: the id format gate;
  - `evals/judge/`: the dataset builder and the frozen dataset;
  - `evals/langsmith/judge_run.py` and `judge_calibration.py`;
  - `OpenRouterModel.judge`, which is evaluation only.
- **Changed:**
  - `redaction.redact` and `telemetry.clean` apply the gate and drop the Telegram and location keys;
  - the orchestrator labels the root run with driver, shipment, facility and channel;
  - `record_turn_metrics` stores the ids;
  - `/console/observability?driver=` filters by driver, and the console renders the new columns;
  - `llm_judge.py`: the stub is replaced by the judge.
- **Unchanged:**
  - booking, dispatch, approval, authorisation, the kill switch and the evidence guard;
  - the OpenRouter routing, metering and retry rules;
  - the ledger functions;
  - role permissions.
- **Details:** `docs/observability/DRIVER_TRACEABILITY.md` and `docs/evaluation/LLM_JUDGE.md`.

## 3. Migrations

Both migrations are forward-only and additive, and each sets the search path itself.

| | What it does | Compatibility | Authorisation | Recovery |
|---|---|---|---|---|
| **0041** | Adds nullable `agent_turn_metrics.driver_id` and `shipment_id`, with format CHECKs. An id is allowed only on rows with a `facility_id`, because rows without a facility are visible to every staff scope. Adds a partial index on driver and time. `record_agent_turn` stores `NULL` for anything that fails. | Existing rows stay `NULL` and valid. The CHECKs are validated. The old call shape still works (tested). | RLS policy, append-only trigger, grants and owner unchanged (tested). | A later migration would restore the 0027 function body, then drop the constraints, the index and the columns. Nothing else reads them. |
| **0042** | Widens `model_usage.purpose` to also admit `judge`. | Existing rows are unaffected. | No grant or function change (tested). | Keep it: rows are never deleted. |

The migration sequence was checked before 0041 was added: 0040 was the latest on `main`.

## 4. Test results

| Run | Result |
|---|---|
| Full regression on `377348b`, local PostgreSQL 18.6 | **1855 passed, 2 skipped, 0 failed** (28:32). The previous baseline was 1729, plus 60 traceability tests and 66 judge tests. |
| Part B related suites, local | 285 passed. 6 errors were Windows temp-folder permission errors, and all of them passed on a rerun with a private temp folder (32 passed). |
| Part B through the Data API emulator (traceability, Phase G, G.1) | 178 passed |
| `tests/test_driver_traceability.py` | 60 passed |
| `tests/test_llm_judge.py` | 66 passed |
| Dataset reproducibility (`build_dataset --check`) | OK, `judge-v1-579de0ebb1ad` |

**Pinned tests changed on purpose, in `tests/test_phase_g.py`:**
- The `agent.turn` line now carries the gated driver id instead of none.
- A malformed `driver_id` is now logged as `[INVALID_ID]` instead of being dropped.

## 5. Trace example with synthetic ids (captured from a real local turn)

The driver's message deliberately contained a phone number, the name "Ramesh Kumar", the Telegram chat id `5512345678` and the coordinates `26.9124,75.7873`. The turn was labelled as Telegram.

**LangSmith, root run `agent_turn`** (`id` = `trace_id` = `b0d57952-…393f`), metadata:

```json
{"correlation_id": "b0d57952-…393f", "turn_ref": "b0d57952-…393f", "ingest_correlation_id": "b2bba377-…e4",
 "conversation_ref": "e57070a3d38fb90d", "driver_id": "DRV-JAI-FFGXBBNV", "shipment_id": "SHP-FFGXBBNV-0",
 "facility_id": "FAC-JAI-01", "channel": "telegram", "provider": "FakeModel", "environment": "ci"}
```

**CloudWatch, `agent.turn` line:** the same `driver_id`, `shipment_id`, `facility_id`, `channel`, `turn_ref` and `correlation_id`, plus the tools, path and timings.

**Console, `agent_turn_metrics` row:** `FAC-JAI-01 | DRV-JAI-FFGXBBNV | SHP-FFGXBBNV-0`, under the same `turn_ref`.

## 6. Redaction verification

The leak check on the exported trace batch and the log line came back false for every item:
- the phone number, both with and without `+`;
- "Ramesh";
- the Telegram chat id;
- the latitude;
- the message text.

**Unit tests cover:**
- phones, including ones shaped to look like ids;
- Telegram chat ids, both bare and shaped to look like ids;
- names, e-mail addresses, usernames and coordinates;
- wrong-kind ids, wrong types and lengths;
- missing ids.

**The gate is format only.** It is pinned as not proof that data is synthetic: tracing still refuses `SETUHAUL_DATA_CLASS` set to anything other than `synthetic`.

## 7. Ops console evidence

There's no screenshot. A preview server would have had to share the test databases while the regression was rebuilding them.

**What the tests show instead** (in `tests/test_driver_traceability.py`):
- the Driver and Shipment columns and their values;
- the filter returns only that driver's turns;
- the filter refuses a phone number, a name, a chat id, a shipment id or a phone-shaped driver id with a 422, and the response doesn't echo the value;
- `operations` and `dispatcher` get 403;
- a planner at Gurugram never sees a Jaipur driver;
- the page markup contains the column and the filter.

## 8. LLM judge calibration report

**Not run yet: it needs two owner steps.**
1. Hand-score the 20 replies in `evals/judge/calibration_sheet_v1.md` into `evals/judge/human_scores_v1.json`, before seeing any judge output.
2. After the approved paid run, run `python -m evals.langsmith.judge_calibration evals/results/judge_<UTC>.json`.

**What the report covers:**
- agreement within ±1, overall and by dimension;
- mean absolute error;
- bias;
- per-language agreement;
- the contrasts on their own;
- 2-point disagreements;
- systematic over- or under-scoring.

**Acceptance bar:** 80% of paired scores within ±1, fixed in advance. Below that, the judge stays experimental.

## 9. Dry-run cost report

From `docs/evaluation/llm_judge_dry_run_v1.txt`:

| | |
|---|---|
| Dataset | `judge-v1-579de0ebb1ad`, 30 records (20 calibration, 5 contrast; en 16, hi 7, hi-Latn 7) |
| Model | `anthropic/claude-haiku-4.5` via Amazon Bedrock: pinned, no fallbacks, `zdr=true`, `data_collection=deny`, temperature 0. The served model version is recorded on each call. |
| Prompt | `judge-p1` |
| Calls | 30 expected, 60 at most (one retry each) |
| Tokens | about 38,100 input and 7,800 output |
| Cost | **about USD 0.08 expected; USD 0.35 upper bound**, which is under the USD 1 run cap |
| Caps | run USD 1.00; daily USD 3.00 |
| Ledger today | local `setuhaul_judge_ledger`: USD 0 spent, USD 0 reserved |
| Paid run | refused until the key is set in the owner's shell and `--approve judge-v1-579de0ebb1ad` is given |

## 10. Budget enforcement evidence

These tests run against a real local ledger:
- **Shared cap.** Agent and judge share one daily cap. An agent reservation refuses the judge, and the agent's *actual* settled cost frees room again.
- **Concurrency on the ledger.** 16 concurrent agent and judge reservations: exactly 6 × USD 0.003 succeed under a USD 0.020 cap.
- **Concurrency on the run cap.** 30 concurrent judge reservations: exactly 8 × USD 0.006 under a USD 0.050 run cap.
- **Unknown cost.** A timeout is charged the full reservation (`cost_source = reservation`), and that amount still counts against the run cap.
- **Not sent.** A connection failure before sending is charged 0 (`not_sent`).
- **Malformed answers.** The answer and its retry are both charged at the provider-reported cost.
- **Run cap refusal.** A call refused by the run cap is never sent.
- **Paid run isolation.** A paid run with a fake transport writes only the ledger and its results file. Every other table is unchanged.

## 11. Documentation updated

- `docs/observability/DRIVER_TRACEABILITY.md` (new);
- `docs/evaluation/LLM_JUDGE.md` (new);
- the dry-run report;
- `docs/plans/LLM_JUDGE_AND_TRACE_DRIVER_ID_PLAN.md`;
- pointers in `docs/phase-g1/LANGSMITH_OBSERVABILITY.md`, `docs/phase-g1/LANGSMITH_EVALUATION.md` and `docs/phase-g/PHASE_G_OBSERVABILITY.md`.

## 12. Readiness checklist

`docs/operations/DISPATCH_TELEGRAM_READINESS.md` §2 gains rows for driver traceability (0041) and the offline LLM judge (0042).

## Readiness assessment

**Part B is ready to merge, after review.**
- **Evidence:** it is complete and tested, and the full regression is green.
- **What changes at deploy time:** 0041 runs in the existing `migrate_data_api` step.
- **Tracing:** stays off until the owner separately enables it.

**Part A is ready to merge as code.** Its results are not ready to rely on:
- the paid run is unapproved and unrun;
- calibration hasn't been done, so the judge is **experimental** until it passes calibration.

**Blockers and owner decisions:**
1. **Paid run approval.** Approve the dry-run report and start the paid run yourself, with the key set in your own shell. About 30 calls, upper bound USD 0.35.
2. **Calibration scores.** Hand-score the 20 calibration replies.
3. **Combined cap scope.** The ledger is per database, so the judge's USD 3 daily cap is shared only with agent calls on the same **local** ledger. It is not combined with the pilot agent's spend in AWS. If one cap across pilot and evaluation is required, the proposal is a dedicated evaluation OpenRouter key with a key-level credit limit, set by you. No parallel budget mechanism was built.
4. **Hex-suffixed ids.** Random hex-suffixed synthetic ids (`DRV-{uuid8}`) occasionally contain 7 or more digits in a row, and then export as `[INVALID_ID]`. This is the deliberate fail-closed choice; readable synthetic ids are unaffected.
5. **Before any real-data tracing:** a keyed HMAC pseudonym with key management, and explicit approval. It is designed but not built.
6. **Push, PR, merge and deploy** each need your approval.
