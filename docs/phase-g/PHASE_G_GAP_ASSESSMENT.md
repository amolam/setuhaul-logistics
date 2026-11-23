# Phase G gap assessment

Base: `main` at `285e90a` (Phase F merged, PR amolam/setuhaul-logistics#2). Branch: `feature/phase-g-intelligent-decisioning`.
Each row is one requirement from the owner's Phase G brief. **Before** is what `285e90a` already had; **Gap** is what was
missing; **Phase G** is what this branch adds; **Evidence** is where to check it.

| # | Requirement (brief) | Before (285e90a) | Gap | Phase G change | Evidence | Status |
|---|---|---|---|---|---|---|
| G1.1 | Recommend dock/slot options for an arriving or delayed truck | `FeasibilityEngine.find_options` returned feasible options sorted by (approval, end, dock) | No objective beyond earliest end; no labour or capacity; no structured solver output | `setuhaul/domain/optimizer.py` (`SlotOptimizer`) reuses the engine for candidates, re-proves 12 hard constraints, scores soft objectives, returns a structured result | `tests/test_optimizer.py` | Done |
| G1.2 | Prefer CP-SAT if it fits the Lambda zip, else document | Frame spike (`docs/1-frame/solver_results.md`) found CP-SAT unnecessary for the facility problem | Per-truck decision not analysed | Exhaustive enumeration (exact over the slot grid); CP-SAT rejected for size/reproducibility, trade-off documented | `PHASE_G_OPTIMIZATION_DESIGN.md` §3 | Done |
| G1.3 | 12 hard constraints, never an infeasible option | 9 of 12 enforced inside the engine; labour (HC07) and facility capacity (HC10) absent; no independent re-check | Labour, capacity, independent validator | `SlotOptimizer.check()` re-derives HC01–HC12 for every candidate; oracle + fuzz tests | `test_property_*` in `tests/test_optimizer.py` | Done |
| G1.4 | Soft objectives with documented weights | Facility planner had a score (wait, lateness, change) | No per-truck objective | `Weights` (wait × priority × perishable, gap, disruption, labour idle, overtime, utilisation, bottleneck) | Design doc §4 | Done |
| G1.5 | Structured output (request id … data version) | Engine `Result` (free-text outcome) | Status, timings, rejected alternatives, fingerprint | `OptimizationResult.to_dict()`; console route; `optimization_runs` table | `GET /console/shipments/{id}/recommendations` | Done |
| G1.6 | Safe outcomes (no slot, missing/stale data, timeout, inconsistent input, engine failure) | Engine escalated NO_FEASIBLE_SLOT / CONTRADICTORY_DATA | Stale data, timeout, engine error, bad input | `MODEL_INVALID`, `STALE_DATA`, `UNKNOWN`/`FEASIBLE` on timeout, `ERROR` + engine fallback in the driver path | tests `test_missing_or_bad_data…`, `test_stale…`, `test_timeout…`, `test_engine_failure…` | Done |
| G1.7 | Optimizer never books; LLM only explains | Booking only via DB functions; replies are templates (OD-3) | — | Optimizer is pure and read-only; output carries `not_a_booking`; driver path still publishes options through `shown_options` and books via `hold_option`/`submit_booking_request` | `tests/test_phase_g.py::test_two_drivers_racing…` | Done |
| G2.1 | Evaluation across 16 dimensions with deterministic business checks | `evals/` (golden G3 sets, held-out h1, replay, budget) for extraction and flows | Authorization, isolation, privacy, optimizer use, idempotency, fallback, latency, cost in one suite | `evals/phase_g/dataset.jsonl` + `run_eval.py` (scripted + rules reader; no LLM judge) | `PHASE_G_EVALUATION_FRAMEWORK.md`, `evals/results/phase_g_eval_*.json` | Done |
| G2.2 | Report totals, per-category, accuracies, latency, cost | Golden reports per run | Single Phase G report | `summarize()` in the runner; stored in `evaluation_runs` and shown in the console | Final validation report | Done |
| G3.1 | Extend the existing console (CSP) | Console tabs: conversations, requests, escalations, docks | No metrics | Tabs **Optimizer** and **Observability** in `console_static` (same CSP, same origin, no new asset files) | screenshots in the PR | Done |
| G3.2 | Agent performance from `model_usage`, tool calls, latency | `model_usage` ledger (no console access) | Tool-call and turn metrics were not persisted | `agent_turn_metrics` + `record_agent_turn`; `observability_global()` aggregates the model ledger | `tests/test_phase_g.py` | Done |
| G3.3 | Reliability: booking failures, retries, idempotency, queue/DLQ, recovery, approvals | Data existed in several tables | No single read; DLQ depth not in DB | Console aggregates; DLQ shown **unavailable** with the reason (lives in SQS/CloudWatch) | `/console/observability` | Done (DLQ: unavailable by design) |
| G3.4 | Structured logs, correlation ids, no PII | Plain `logging` messages | No correlation id | `setuhaul/ops/telemetry.py` (PII keys dropped), `X-Correlation-Id`, `CORRELATION_ID` contextvar through turn → optimizer run → metrics | `test_telemetry_never_logs_personal_fields` | Done |
| G3.5 | Role and facility scoping | `PERMISSIONS` + RLS | — | `PHASE_G_PERMISSIONS` (planner, ops_lead); RLS on `optimization_runs`, `agent_turn_metrics` | `test_recommendations_scope_and_roles` | Done |
| G4 | Dataset from the trainer data, minimal changes, synthetic marking, configurable assumptions | Trainer SQLite snapshot + `pg_seed`; pilot seed with `SYN-`/`+999` | No synthetic copy of the trainer data; no labour/capacity assumptions | `setuhaul/adapters/phase_g_dataset.py` + `phase_g_assumptions.json` | `PHASE_G_DATASET_AND_ASSUMPTIONS.md` (coverage 90.1 %) | Done |
| G5 | Ten documents with Mermaid diagrams matching code and deployment | Phase 1–F docs | Phase G set | `docs/phase-g/*.md` | this folder | Done |
| G6 | 3–4+ failure demonstrations | Failures recorded in docs/release 13–15 | Not consolidated with tests | `PHASE_G_FAILURE_DEMONSTRATIONS.md` (7 real + 2 controlled injections) | that document | Done |
| G7 | Locust 4–5 users, isolated local env | None | Load test | `perf/serve_local.py`, `perf/locustfile.py` | `PHASE_G_PERFORMANCE_REPORT.md` | Done |
| — | Migration additive, next number 0027 | 0001–0026 | — | `migrations/0027_phase_g_observability.sql` (3 tables, 4 definer functions, grants) | `tests/test_agent_not_operator.py` (allow-lists updated with reasons) | Done |

## Not in scope (unchanged)
Live GPS, FASTag (future ETA signals only), traffic, reinforcement learning, predictive maintenance, autonomous
rescheduling, multi-facility global optimization, production load tests, live Meta, production booking.

## Found while assessing (not fixed here; follow-up)
* `GET /console/facilities/{id}/schedule-proposal` (Phase F) reads through the **feasibility** role, which the pilot's
  api function has no login for (`infra/setuhaul_stack.py` `WORKLOAD_ROLES["api"] = ("ingest", "console")`). On the
  pilot it would fail the same way the console Confirm did (failure F5 in `PHASE_G_FAILURE_DEMONSTRATIONS.md`). The new
  Phase G recommendation route avoids this by reading with the console role. Fixing the schedule route is a separate
  change to Phase F code.
