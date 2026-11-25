# LangSmith observability (G1)

Status: **implemented and tested locally; off by default; not deployed; never exported to LangSmith Cloud from this
work** (no LangSmith key exists yet: creating the account and the key is an owner action).

## 1. Decisions

| Decision | Choice | Why |
|---|---|---|
| Hosting | LangSmith **Cloud** (US `api.smith.langchain.com`, or EU `eu.api.smith.langchain.com`), synthetic data only | Brief; no self-hosting. CloudWatch Logs and X-Ray (`Tracing.ACTIVE` on every Lambda) stay the infrastructure layer. |
| Client | Own exporter over the REST API (`POST /runs/batch`) on the already-pinned `httpx`; **no LangSmith SDK** | The agent has no LangChain (it is a fixed deterministic pipeline), so the SDK's auto-instrumentation has nothing to hook. No new dependency: no package growth (agent zip 30.7 MB), no transitive deps for `pip-audit`, and no SDK environment variables (`LANGSMITH_TRACING`) that could export unredacted data. Every byte passes our redaction first. |
| Default | Off everywhere (`SETUHAUL_LANGSMITH_TRACING` unset) and, in CDK, behind the context flag `langsmith_tracing=on` | Deploying this branch as is creates no secret, no permission and no export (template identical to `main`, proven). |
| Projects | `setuhaul-dev` (dev/eval/ci) and `setuhaul-synthetic-pilot` (pilot), `setuhaul-synthetic-staging` | Enforced: the pilot cannot write to the dev project and dev cannot write to the pilot project. |
| Key | Secrets Manager `setuhaul/pilot/langsmith` (only when opted in), read by the **agent** only; dev/eval/ci may use `LANGSMITH_API_KEY` | Same rule as the OpenRouter key. The CDK placeholder is an explicit `{"api_key": ""}`, read as EMPTY_SECRET with tracing off: never a key-shaped random string (the OpenRouter lesson). |
| Endpoint | allowlist: the two LangSmith API hosts only | A typo or an injected variable cannot send traces anywhere else. |

## 2. What is traced (hierarchy)

One trace per driver turn delivery. The root run id = the turn's `turn_ref` = the turn's `correlation_id`
(deterministic from the webhook's correlation id, the message and the delivery generation).

```
agent_turn (chain)                       inputs: message_ref (hash), generation, attestation_status
├── get_driver_context (tool)            outputs: status
├── intent_extraction (chain)            outputs: intent, delay kind, times, kinds, language, flags (no text)
│   ├── model_call (llm)                 metadata: provider, model, latency, attempts, prompt version;
│   │                                    outputs: intent, usage_metadata (ONLY if the provider reported it), cost_usd
│   │                                    on failure: error = "CATEGORY (detail)", error_category, outcome_class
│   └── rules_reader (chain)             on fallback: fallback_reason = category
├── parameter_check (chain)              outputs: missing, problems, safety, resolved timestamps
├── decide (chain)                       outputs: path, escalation
│   ├── record_driver_report (tool)      outputs: status, appointment_at_risk, shipment_id
│   ├── find_feasible_options (tool)     outputs: status, number of options
│   │   └── optimizer (chain)            outputs: solver status, outcome, options, rejected, candidates, duration
│   ├── select_option / confirm_selection / hold_option / submit_booking_request (tool)
│   │                                    outputs: status, code, appointment_id, approval_id
│   │                                    metadata: policy_outcome (BOOKING_DISABLED, HUMAN_ACTIVE, AUTOMATION_PAUSED, …)
│   └── escalate_to_human (tool)         outputs: escalation_id
└── final_response (chain)               outputs: replied, language, reply_chars, template (reply text only if content on)
```

Covered: the request (root inputs + correlation), intent, parameter extraction, the model call and its response or
failure, tool selection and execution, the optimizer, policy checks (`policy_outcome` from the service's refusal codes),
approval (`approval_id` and status from `submit_booking_request`), the booking service (hold/submit tools), the final
response, exceptions (run `error` = exception type only) and the fallback (`rules_reader`, `fallback_reason`).

Safe metadata on the root: `environment`, `agent_version`, `correlation_id` (= `turn_ref`), `ingest_correlation_id`
(the webhook request's id), `turn_ref`, `conversation_ref` (a
16-hex SHA-256 of the thread id, never the id), `provider`, `model`, `facility_id`, `fallback_reason`/`error_category`.
Driver traceability (migration 0041): also `driver_id`, `shipment_id` and `channel`, format-gated (see
`docs/observability/DRIVER_TRACEABILITY.md`).
Tokens and cost appear **only when the provider returned them**. A failed call has no usage block.

## 3. Correlation

```mermaid
sequenceDiagram
    participant WA as Webhook (API Lambda)
    participant Q as SQS FIFO
    participant AG as Agent Lambda
    participant DB as Aurora (ledger, metrics, runs)
    participant CW as CloudWatch Logs
    participant LS as LangSmith Cloud
    WA->>WA: X-Correlation-Id = C (kept if a valid UUID, else new)
    WA->>CW: request lines (C)
    WA->>Q: event {..., correlation_id: C}
    Q->>AG: event
    AG->>AG: turn id T = uuid5(C:message:generation) (the turn's correlation_id AND turn_ref)
    AG->>DB: optimization_runs.request_id = T; agent_turn_metrics.turn_ref = T
    AG->>CW: agent.tool {T, ingest C, tool, status, appointment_id, approval_id}; agent.turn {T, ingest C, ...}; "agent turn … correlation_id=T turn_ref=T ingest_correlation_id=C"
    AG-->>LS: after the batch: trace id T, metadata.ingest_correlation_id = C (bounded flush, may be dropped)
    Note over CW,LS: console "Recent turns" shows T and a trace link; search CloudWatch for C or T
```

* API → agent: the webhook's correlation id C now rides the queue event (before G.1 the agent minted an unrelated one)
  and is logged as `ingest_correlation_id` on every agent line and on the trace.
* The turn keeps ONE id of its own, T (the Phase G invariant: turn metrics, optimizer run, tool events, logs and
  trace share it). T is not C because one webhook can carry several messages and a message can be redelivered: T is
  unique per message and delivery. (Found by the final regression: an early G.1 draft used C as the turn's id and broke
  that invariant; fixed before the PR.)
* Booking and approval events carry no correlation column (no booking-table change in G.1). They are joined through
  the `agent.tool` log line, which has `appointment_id` / `approval_id` plus C and T, and through the trace's tool spans.

## 4. Redaction (`setuhaul/observability/redaction.py`)

Applied to inputs, outputs, metadata and error text when a turn ends, before it is queued:

* dropped keys: phone/msisdn/wa_id, driver name, email, address, session/tokens, Authorization, cookies, api_key, any
  `*secret*`, `*password*`, `*_token`, `*_key`, signatures, prompts/messages/user_block;
* content keys (`text`, `body`, `message`, `reply`, `note`, `summary`, `reason`, …) become `[REDACTED]` unless
  `SETUHAUL_TRACE_CONTENT=on` (synthetic data only), and even then they are scrubbed;
* scrubbed patterns: JWTs, Bearer tokens, `sk-or-…`/`sk-…`/`lsv2_…` keys, AWS access key ids and secret keys, Meta
  `EAA…` tokens, e-mails, phones (E.164, +999 synthetic, Indian mobiles), 12-digit account ids (also inside ARNs);
* ids (`driver_id`, `shipment_id`, `facility_id`): exported only when well-formed, otherwise `[INVALID_ID]`
  (`observability/identifiers.py`); Telegram chat/user ids, usernames and coordinates are dropped keys;
* limits: strings ≤ 300 chars, ≤ 25 list items, depth ≤ 6;
* **export guard**: after redaction the payload is scanned again, and a trace that still matches any pattern is dropped
  whole (`blocked_leaks` counter).

## 5. Failure handling, trace loss and limits

LangSmith is non-critical. Proven by tests:

| Property | How |
|---|---|
| never fails a request or a booking | every recording call is wrapped (`tracing.safe(lambda: …)`); a bug inside tracing is counted (`tracing_errors`) and the turn carries on (`test_a_bug_inside_tracing_never_reaches_the_turn`) |
| export is non-blocking for the turn | export happens in `agent_handler` **after** every record's outcome is final; the turn's DB work, reply and SQS result are already decided |
| never changes authorisation, never duplicates a tool, never adds a model call | spans only record; `test_langsmith_down_changes_nothing_about_the_turn` compares exporter up vs down: same reply, one model call, `find_feasible_options` once |
| bounded | flush budget `SETUHAUL_TRACE_FLUSH_S` (0.5 s) + at most one in-flight POST (HTTP timeout 1 s); measured in the performance report |
| bounded memory | queue ≤ 200 turns, ≤ 60 runs per turn; beyond that, dropped and counted |

**Trace loss** (by design; the agent is never slowed or failed to save a trace). A trace is lost when:
tracing is off or the turn is sampled out (`SETUHAUL_LANGSMITH_SAMPLE_RATE`); the queue is full; a batch fails twice
(`max_attempts=2`, no further retry); the flush budget ends with traces queued and the container is then recycled; the
Lambda times out; or the export guard blocks it. Counters (`exported`, `dropped`, `export_errors`, `blocked_leaks`,
`pending`) are logged after each flush (`trace export {...}`). CloudWatch lines and the `agent_turn_metrics` row are
written regardless, so a lost trace never loses the facts.

## 6. Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `SETUHAUL_LANGSMITH_TRACING` | off | `on` to enable |
| `SETUHAUL_LANGSMITH_SECRET_ID` | none | pilot/staging: required (Secrets Manager) |
| `LANGSMITH_API_KEY` | none | dev/eval/ci only; refused in pilot/staging |
| `SETUHAUL_LANGSMITH_PROJECT` | by environment | dev ↔ pilot cross-writing refused |
| `SETUHAUL_LANGSMITH_ENDPOINT` | `https://api.smith.langchain.com` | allowlist (US, EU) |
| `SETUHAUL_LANGSMITH_UI_URL` | none | the project page (console links); must be `https://[eu.]smith.langchain.com/…` |
| `SETUHAUL_TRACE_CONTENT` | off | sanitised message text, synthetic data only |
| `SETUHAUL_LANGSMITH_SAMPLE_RATE` | 1.0 | 0..1 |
| `SETUHAUL_TRACE_FLUSH_S` | 0.5 | per agent invocation |

CDK: `cdk synth -c langsmith_tracing=on -c langsmith_ui_url=https://smith.langchain.com/o/<org>/projects/p/<project>`
adds the `setuhaul/pilot/langsmith` secret (placeholder `{"api_key": ""}`), grants it to the agent only, and sets the
variables above (the api function gets only the tracing flag and the UI URL for links).

## 7. Enabling on the pilot (owner actions, paid-service decision)

1. Create a LangSmith Cloud account and workspace (owner; its plan and price are the owner's decision: a new external
   service). Create the project `setuhaul-synthetic-pilot` and an API key.
2. Deploy with `-c langsmith_tracing=on -c langsmith_ui_url=…` through the release procedure.
3. Put the key in `setuhaul/pilot/langsmith` as `{"api_key": "<key>"}` (owner enters it). Tracing stays off with
   `EMPTY_SECRET` in the log until then.
4. Verify with OPENROUTER_PILOT_DIAGNOSIS.md §8 step 3 (one trace, no PII, ids match CloudWatch).

## 8. Not verified

The REST calls follow LangSmith's public API (`/runs/batch`, `/datasets`, `/examples/bulk`, `/sessions`, `/feedback`,
header `x-api-key`) and are unit-tested against a mock transport. They were **not** exercised against LangSmith Cloud,
because no key exists and no external call was allowed. The first real export (step 4 above) is that verification.
