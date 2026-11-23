# Phase G3: observability

The existing staff console (`setuhaul/api/console_static`, served by the API under `/app/` with a strict CSP: scripts
only from this origin, no inline script, no eval, no framing) gained two tabs. No separate dashboard, no new asset file,
no new origin.

| Tab | Route | Roles | Scope |
|---|---|---|---|
| Optimizer | `GET /console/shipments/{id}/recommendations?days=1..3` | planner, ops_lead | the person's facilities; another facility's truck is 404 |
| Observability | `GET /console/observability?facility=&hours=1..744` | planner, ops_lead | the person's facilities (RLS); another facility is 403 |

Both follow the existing pattern: the role check is `PHASE_G_PERMISSIONS` in `setuhaul/api/service.py` (kept apart from
`PERMISSIONS`, which `tests/test_api.py` pins), and the facility scope is enforced by row-level security on every read.
The operations role keeps exactly its Step 6 permission set (`tests/test_console_me.py`).

## Sections and sources (every number is a stored row or a measured timing)

| Section | Metric | Source |
|---|---|---|
| Operational overview | shipments by status, current appointments by status, open escalations by reason, open approval requests, docks blocked now, active conversations | `shipments`, `appointments`, `escalations`, `approval_requests`, `dock_events`, `chat_messages` (RLS) |
| Agent performance | turns, model vs rules reader, fallbacks (with reasons), escalated turns, turn p50/p95 | `agent_turn_metrics` (0027) |
| | tool calls per tool: count, not-OK, p50/p95 ms | `agent_turn_metrics.tools` (tool, status, ms only) |
| | model calls, OK/errors/budget refusals, input/output tokens, cost USD, spent today, latency p50/p95, by model, by error | `model_usage`, `model_budget_day` through `observability_global()` — **all facilities** (the ledger has no facility column; it holds no driver data) |
| Optimization performance | runs, with options, fallbacks to the engine order, p50/p95/max ms, mean candidates and rejections, by status / outcome / caller | `optimization_runs` (0027) |
| Reliability | approval requests by status, booking events by action, driver notifications by status and retries, outbox by status, inbound received/processed/redelivered/retried/stuck > 5 min | `approval_requests`, `booking_events`, `notifications`, `outbox`, `inbound_receipts` |
| | idempotency keys (and in-progress), approval-workflow callbacks (retried, open, by outcome) | `observability_global()` |
| | **queue / DLQ depth** | **unavailable** in the console: it lives in SQS/CloudWatch (alarms exist in the stack); the console does not read AWS metrics. The inbound counters above are the database-side proxy |
| Evaluation results | last five runs: suite, mode, dataset, passed/total, failed, skipped, cost, time | `evaluation_runs` (0027) |

Anything with no rows in the window is shown as "unavailable: …" or "none recorded", never as zero invented data.
Every answer carries `data_label: "SYNTHETIC DATA - pilot/local only, no real drivers or shipments"`.

## Structured logs and correlation ids

* `setuhaul/ops/telemetry.py` emits one JSON object per line on the `setuhaul.telemetry` logger: `optimizer.run` and
  `agent.turn` events. Keys that could carry personal data or text (`phone`, `driver_id`, `name`, `text`, `body`,
  `message`, `note`, `summary`, `args`, `prompt`, …) are dropped whatever the caller passes; long strings are cut.
* Correlation id: every HTTP request gets one (a well-formed `X-Correlation-Id` is kept, anything else replaced) and it is
  echoed in the response. Every agent turn gets one in `Orchestrator.handle`. The same id is the optimizer run's
  `request_id`, the `agent_turn_metrics.turn_ref` and the `correlation_id` of each log line of that turn.
* In AWS the lines go to the functions' CloudWatch log groups (existing retention); nothing new is exported.

## Privacy guarantees (tested)
* `record_agent_turn` refuses any tool entry with keys other than `tool`, `status`, `ms` (`BAD_TURN`).
* `record_optimization_run` refuses a facility outside the caller's scope and a shipment or dock of another facility.
* The three 0027 tables are append-only (triggers).
* `test_driver_options_come_from_the_optimizer_and_the_turn_is_measured` asserts the logged events contain neither the
  driver's phone nor their words nor a driver id.
