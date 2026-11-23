# Phase G.1 — Gap assessment (written before any change)

Base: `main` at `951a320` (PR #3 merged as `1d304f8`, released as v0.3.0). Branch: `feature/phase-g1-langsmith-model-reliability`.
Assessed 23 Nov 2026. AWS account ids are masked (`************`). No secret value was read or printed: Secrets Manager was
queried for **metadata only** (`DescribeSecret`, `ListSecretVersionIds`), the database through **read-only** `SELECT`s in
`SET TRANSACTION READ ONLY` transactions as the `system` login (the RDS Data API takes the secret ARN; the value never reaches
this workstation), and CloudWatch Logs through `FilterLogEvents`.

## 1. The OpenRouter failures (G3): root causes from evidence

The console showed 35 model calls in 24 h: 0 OK, 27 errors, 8 budget refusals, 0 tokens, $0. The suspected cause was an
empty `setuhaul/pilot/openrouter` secret. **The evidence does not support "empty".** There are two independent causes.

### 1a. 27 errors: OpenRouter answers HTTP 401 to a CloudFormation-generated placeholder "key"

| Evidence | Finding |
|---|---|
| `cdk.out/SetuHaulPilot.template.json`, resource `OpenRouterKey…` | `"GenerateSecretString": {}`: `sm.Secret(...)` with neither `secret_string_value` nor `generate_secret_string` makes CloudFormation **generate a random 32-character password** at create time. The secret was never empty. |
| `DescribeSecret` / `ListSecretVersionIds` | One version (`AWSCURRENT`), created 22 Nov 2026 11:59:22 UTC, the same instant as the secret itself; `LastChangedDate` = creation. **The owner has never put an OpenRouter key in it.** `LastAccessedDate` 23 Nov: the agent reads it. |
| Agent logs (`SetuHaulPilot-LogsAgent…`) | `model_call … requested=amazon-bedrock actual=None status=401 category=auth latency_ms=15 cost=0` then `ALERT model_call auth: OpenRouter refused the key or its credit (status 401)`, for every attempt on 22 and 23 Nov. |
| Ledger `model_usage` | 27 rows `ERROR / auth`, `cost_source=provider`, cost 0, tokens NULL (8 on 22 Nov, 19 on 23 Nov). |
| Cold start | `model_for()` runs at cold start and **raises** on a missing/empty key; turns would then fail outright, not fall back. They fell back, so the key loaded: secret present, readable (IAM `GetSecretValue` + KMS decrypt work), right region, parsed, passed to the client. |
| Network | 15 ms round trips with an HTTP status from OpenRouter: DNS, egress (the agent Lambda is outside the VPC), TLS and the endpoint all work. |
| `scripts/e2e_aws.py` scenario A5 | Titled *"model unavailable (OpenRouter refuses the key): the rules reader answers"*: the AWS end-to-end run **expected** this 401. |

Root cause: a **configuration gap, not a code defect**. The infrastructure creates a placeholder value that looks like a
key and the owner step "put the OpenRouter key in the secret" was never done. Contributing code gaps:

* G3-C1 the error taxonomy is coarse (`auth`, `credits`, `config`) and cannot tell an empty secret, a placeholder, a
  denied secret read or a provider auth failure apart;
* G3-C2 a missing/empty/unreadable secret at cold start **fails every turn** (the orchestrator cannot be built), instead
  of degrading to the rules reader with a recorded reason ("calls that never started" are invisible);
* G3-C3 the key is cached for the container's life: after the owner sets the real key, warm containers keep sending the
  placeholder until they are recycled (stale cache);
* G3-C4 `Trace` reports `0` tokens for a failed call (a fabricated zero; the ledger correctly stores NULL).

### 1b. 8 budget refusals at $0: residue of the AWS end-to-end drill A6, not a budget defect

| Evidence | Finding |
|---|---|
| `model_budget_day` | 22 Nov (UTC): `spent_usd = 0.250000` (= the $0.25 cap), `reserved_usd = 0`, environment label `pilot-ap-southeast-2`. 23 Nov: spent 0, reserved 0, label `pilot`. |
| `model_usage` 22 Nov | Every row has cost 0 (`not_sent` or `provider`). Nothing the ledger recorded explains $0.25. |
| `scripts/e2e_aws.py` scenario A6 | `INSERT … model_budget_day … 'pilot-ap-southeast-2' … ; UPDATE model_budget_day SET spent_usd = greatest(spent_usd, 0.25)` for the current UTC day: the **deliberate** "daily budget spent" drill. The label it writes is the one on the 22 Nov row. |
| Logs | `model_call budget_exhausted … cap_usd=0.25` on 22 Nov only (12:44–15:54 UTC); none on 23 Nov. |

The budget enforcement itself is correct: reservations were all released (`reserved_usd = 0` both days, no `RESERVED`
row left), refusals are recorded as `BUDGET_EXHAUSTED` with cost 0 and `not_sent`, failures were charged 0 because
OpenRouter rejects a 401 before inference. The day is the UTC calendar day (00:00 UTC = 05:30 IST), so the console's
"last 24 hours" window on 23 Nov still contained the drill's 22 Nov refusals. Gaps:

* G3-B1 the drill leaves the day spent, so real calls are refused for the rest of that UTC day and the console cannot
  tell drill spend from real spend;
* G3-B2 the console shows `cost_usd`, but does not say when a cost is the provider's figure and when it is the kept
  reservation (an estimate), and does not say the budget day is UTC.

### 1c. Other things seen in the logs (no change in this phase)

`json.decoder.JSONDecodeError` at `lambdas.py:136` every 5 minutes on 23 Nov 01:28–04:12 UTC: the DLQ alarm drill
(`scripts/drill_dlq_alarm.py`) deliberately sends a body that is "not JSON". This is expected behaviour: the record is
redelivered, then dead-lettered.

## 2. Gaps by workstream

| # | Area | State at `951a320` | Gap |
|---|---|---|---|
| G1 | LangSmith tracing | None. **No LangChain or LangSmith code exists**: the orchestrator is a fixed deterministic pipeline (`setuhaul/agent/orchestrator.py`); the model is called once, for extraction, over raw HTTP (`openrouter.py`). Observability = `telemetry.emit` JSON lines + `agent_turn_metrics` + X-Ray ACTIVE on Lambdas. | Need spans for the whole lifecycle with hierarchy; central redaction; non-blocking export; key from Secrets Manager; separate projects. |
| G1 | Correlation | The webhook request gets an `X-Correlation-Id`, but `Orchestrator.handle` mints a **new** id per turn: API and agent logs cannot be joined. Optimizer runs use the turn id. Booking/approval events carry no correlation id. | Carry the request id through the queue event; derive a stable per-turn trace id; log tool/booking/approval ids under it. |
| G2 | Evaluation | `evals/phase_g/run_eval.py`, 67 rows in 16 internal categories, deterministic checks, local-only DSN guard (`local_env.assert_local`). 65/67: I14 and I15 fail (rules reader). | No LangSmith datasets, experiments, evaluator versioning, before/after comparison, or the 15 brief categories (ETA/delay, deadline, arrival, expired slots, Hindi/Hinglish, model-unavailable fallback are not categories today). |
| G3 | Provider reliability | See §1. Retry policy is sound (bounded, per-attempt metered, never re-routed). | Structured 16-category taxonomy; degrade instead of crash on secret problems; bounded key refresh; no fabricated tokens; console split of outcomes. |
| G4 | Rules reader | I14 "Need to finish unloading by 5 pm" → intent `OTHER`, no deadline. I15 "pahunch gaya hoon gate pe" → `REPORT_DELAY` (the arrival verb alone means "about arriving"). Time zone: `IST` constant in `extraction.local_to_ts` (facility table says `Asia/Kolkata` for both facilities); never server-local, but implicit. | Completion-deadline grammar; present-state arrival grammar with negatives; explicit facility time zone. |
| G5 | Console | `index.html` hardcodes `booking execution OFF` while the pilot runs with booking **ON** (public `/health`: `booking_enabled: true`, kill switch `disengaged`). Optimizer placeholder `SHP-SYN-0001` is not a real format (pilot ids are `SYN-SHP-<8 hex>`). Empty states do not say *why* a panel is empty. `/health` reports `version 0.2.0` (stale; release is v0.3.0). | Live status pills, format help + validation, explanatory empty states, model-outcome split. |
| G6 | LangSmith links | None. | Project/trace/experiment links, eval status, dataset version, failure category; planner/ops_lead only (`view_observability` already excludes `operations`). |

## 3. Constraints carried into the build

* No deploy, no workflow trigger, no secret change, no budget change, no booking/kill-switch/approval change, no paid call.
* The existing CDK `OpenRouterKey` resource is left **byte-for-byte unchanged**: changing `GenerateSecretString` would make
  CloudFormation regenerate the value and could overwrite a key the owner sets later (a test now pins this).
* LangSmith is off by default everywhere and, in the stack, behind a context flag (`langsmith_tracing`, default off), so
  deploying this branch as-is creates no new secret and sends nothing to LangSmith.
* Lambda package: avoid new heavy dependencies (the agent zip is 30.7 MB). The LangSmith exporter is written on the
  already-pinned `httpx`, not the LangSmith SDK (see LANGSMITH_OBSERVABILITY.md §Decision).
