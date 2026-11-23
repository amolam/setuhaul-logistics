# Phase G7: performance report

**Run:** 23 Nov 2026, local Windows workstation, branch `feature/phase-g-intelligent-decisioning` (commit after the
validation fixes). **Isolation:** local FastAPI app (uvicorn, 127.0.0.1:8765) + local PostgreSQL 18.6 (database
`setuhaul_phase_g_perf`, Phase G synthetic dataset). Never the pilot, never production: `perf/serve_local.py` refuses
any non-local database and `perf/locustfile.py` refuses any non-local host. No model was called (the deterministic rules
reader extracted every driver message): **LLM cost $0.00**.

## Setup
| Item | Value |
|---|---|
| Tool | Locust 2.37.10 (throwaway venv, not a project dependency) |
| Users | **5 concurrent** (3 driver users, 2 planner users), spawn 1/s, **4 minutes** |
| Driver workflow | delay report → "1" (selection) → "haan ji, kar do" (booking request); new synthetic driver each flow (120 in the pool) |
| Planner workflow | recommendations (optimization) ×3, list + CONFIRM a pending request (approval) ×2, dashboard ×1 |
| Agent | two in-process workers drain the local inbound queue through the real orchestrator; optimizer ranking ON |
| Booking | local test harness (no AWS gates); database rules, locks and exclusion constraints as in production |

## HTTP results (Locust, client side)

| Workflow | Requests | Failures | p50 ms | p95 ms | Max ms | req/s |
|---|---|---|---|---|---|---|
| 1 delay report (webhook) | 84 | 0 | 590 | 710 | 924 | 0.35 |
| 2 selection (webhook) | 83 | 0 | 560 | 720 | 785 | 0.35 |
| 3 booking request (webhook) | 82 | 0 | 580 | 730 | 842 | 0.34 |
| 4 optimization (recommendations) | 114 | 0 | 310 | 370 | 456 | 0.48 |
| 5a planner approval: list | 69 | 0 | 110 | 140 | 170 | 0.29 |
| 5b planner approval: decide | 69 | 0 | 310 | 490 | 532 | 0.29 |
| 6 dashboard view | 43 | 0 | 190 | 230 | 243 | 0.18 |
| **Aggregated** | **544** | **0 (0.00 %)** | **400** | **690** | **924** | **2.28** |

## Server-side measurements (`perf_db_measurements.json`, from stored rows)

| Measure | n | p50 | p95 | Max |
|---|---|---|---|---|
| Agent turn (orchestrator, end to end) | 249 turns, 0 errors | 1 911 ms | 2 279 ms | 2 440 ms |
| **Optimizer run** (driver path + console) | 214 runs, 0 unsafe statuses | **24 ms** | **39 ms** | 49 ms |
| Queue time (receipt stored → turn processed) | 249 messages, 0 unprocessed | 3 177 ms | 4 605 ms | — |
| Tool `find_feasible_options` (view read + optimizer + publish) | 84 | 873 ms | 1 036 ms | — |
| Tool `hold_option` | 56 | 468 ms | 1 132 ms | — |
| Tool `submit_booking_request` | 40 | 418 ms | 513 ms | — |
| Tool `get_driver_context` | 276 | 279 ms | 370 ms | — |

* **DB time:** pure statement time is **unavailable** (`pg_stat_statements` is not installed in the local cluster). Tool
  latencies are the proxy: each tool is one to three database transactions with nothing else expensive except the
  optimizer, whose own time is listed separately (≈ 3 % of `find_feasible_options`).
* **Queue time** is dominated by the two local workers (a turn takes ≈ 1.9 s and drivers send a new message every
  1.5–3 s); it measures this harness's worker count, not SQS.
* **Correctness under load:** 26 bookings CONFIRMED by planners and 1 still pending; 56 holds attempted, 40 requests
  submitted (the rest lost a race for the same slot and were re-offered or escalated: 13 escalations); **0 overlapping
  dock claims** (`double_booked_dock_overlaps = 0`); 0 failed turns.

## Limits (read before quoting these numbers)
* **5 users is not production-scale proof.** It shows the workflows hold together and that the optimizer is cheap
  (p95 39 ms against a 2 000 ms limit); it says nothing about Lambda cold starts, the RDS Data API's per-statement HTTP
  overhead, Aurora, SQS or API Gateway.
* Local workstation, one PostgreSQL 18.6 instance (CI and the pilot run 17.9), client and server on the same machine.
* The model was absent; with Haiku via OpenRouter a turn adds one extraction call (its latency is not measured here) and its cost, capped at $0.25/day.
* Webhook latency includes synchronous ingest (message, receipt, attestation) and the in-process queue publish only;
  the agent's work is asynchronous and measured server-side.
* A production load test is out of scope (owner's brief).

## Reproduce
```
python -m perf.serve_local --port 8765 --seconds 330 --fixtures <scratch>/perf_fixtures.json
PERF_FIXTURES=<scratch>/perf_fixtures.json locust -f perf/locustfile.py --headless -u 5 -r 1 -t 4m --host http://127.0.0.1:8765 --csv <scratch>/locust
```
