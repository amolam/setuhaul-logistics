# Phase G final validation report

| | |
|---|---|
| Branch | `feature/phase-g-intelligent-decisioning` |
| Base | `main` at **`285e90a`** (Phase F merge, PR amolam/setuhaul-logistics#2) |
| Validated code | `f0ef0dd` (integration freeze `49fdf3b` plus the validation fixes) |
| Date | 23 Nov 2026 |
| Environment | Local Windows workstation, Python 3.14.7, PostgreSQL **18.6** (local test cluster, port 55432). PostgreSQL **17.9** (CI and the pilot) is covered by the PR's CI run, which pins `postgres:17.9` |
| Isolation | Local database and in-process app only. No AWS call, no deployment, no push to `release/fde-sydney-pilot`, no workflow dispatch, no `pilot_mode`, `kill_switch` or drill script. The deployed pilot (`v0.2.1`) was not touched |
| Paid model calls | **None**. LLM cost **$0.00** |

## Procedure (as the owner's mandate required)
1. Build everything; during building only syntax, import, static, migration-consistency and targeted checks.
2. Integration freeze at `49fdf3b`.
3. **One comprehensive validation:** full suite on PostgreSQL, Data API mode, the Phase G evaluation, Locust, the
   frontend (served page, CSP, console errors) and the docs.
4. Defects fixed at the root, each re-checked with targeted tests.
5. **One full regression** on the final code (`f0ef0dd`): full suite on PostgreSQL, then the Data API set.

## Defects found by the validation and their fixes

| # | Found by | Defect | Root cause | Fix | Targeted check |
|---|---|---|---|---|---|
| V1 | full suite | `test_s4_02_the_heartbeat_keeps_a_long_turn…` failed: `AttributeError` on a `None` turn result | the new per-turn timing assumed `_turn` always returns a result | guarded; metrics only for real turns | that test + Phase G suites (68 passed) |
| V2 | full suite | the recommendation answer named `FAC-GGN-01` to a JAI-only planner | the result's `config` echoed every facility's labour and capacity | the config in a result is the request's facility only (HC12) | `test_planner_gets_structured…` |
| V3 | Data API run | the privacy assertion flagged a run of digits inside the 64-hex input fingerprint | the test's phone regex was too broad (false positive, random per run) | E.164 pattern plus the driver's actual number; same fix in the eval runner | `tests/test_phase_g.py` in Data API mode (12 passed) |
| V4 | evaluation | 16 intent rows crashed; C10 crashed | runner defects: the rules reader returns a pydantic model; a thread owned by a person has no `flow` | runner fixed | evaluation re-run |
| V5 | evaluation | C12 and T04 failed | labels contradicted documented behaviour (a status question with no booking offers slots by design AT02; another driver's shipment is refused with HTTP 403) | labels corrected, with `label_note` in the rows | evaluation re-run |
| V6 | review of the diff | three edited files (`orchestrator.py`, `replies.py`, `db.py`) were saved with LF instead of their committed CRLF | editor default | original line endings restored (diff now shows only the real changes) | `git diff --stat` |

## Results

### Tests (final regression, `f0ef0dd`)
| Suite | Total | Passed | Failed | Skipped |
|---|---|---|---|---|
| Full suite, PostgreSQL 18.6 | 1 204 | **1 202** | **0** | 2 |
| Database tests through the RDS Data API backend (emulated; the CI job's file list + `test_phase_g.py`) | 453 | **453** | **0** | 0 |

Skipped, with reasons (both pre-existing and expected locally):
* `tests/test_aurora_owner.py:126`: needs `SETUHAUL_TEST_FRESH_DSN` (an empty PostgreSQL cluster).
* `tests/test_data_api.py:155`: runs only when the whole suite goes through the Data API.

The first validation run of the full suite (on the freeze commit `49fdf3b`) gave 1 200 passed, 2 failed (V1, V2), 2 skipped.

### Optimization (G1)
| Check | Result |
|---|---|
| `tests/test_optimizer.py` | **33 / 33**: every scenario in the brief (normal, delayed, early, late, full docks, maintenance, breakdown, labour shortage and roster, capacity, incompatible cargo, perishable priority, competing trucks, contradictory data, closing time, after the last new start, driver window, missing/bad data, inconsistent input, stale data and stale ETA, timeout, engine failure, ineligible/fixed, isolation, blocked slot, determinism) |
| Property: no returned option breaks a hard constraint | **0 violations** on the trainer snapshot (21 shipments × 8 times × 3 windows = 504 runs) and on 150 seeded random worlds (oracle written independently of the optimizer) |
| Concurrent booking of the same recommended slot | exactly one HELD; 0 overlapping claims (`test_two_drivers_racing…`); 0 overlaps after the load test |
| Evaluation optimization correctness | 41 / 41 checks |
| Optimizer time under load | p50 24 ms, p95 39 ms, max 49 ms (214 runs; limit 2 000 ms) |

### Evaluation (G2) — `evals/results/phase_g_eval_20261123_1024.json`
| Metric | Value |
|---|---|
| Rows | **67**: 65 passed, 2 failed, 0 skipped (**97.0 %**) |
| Tool-selection accuracy | 41 / 41 (100 %) |
| Parameter accuracy | 17 / 18 (94.4 %) |
| Intent accuracy (rules reader, the production fallback) | 14 / 16 (87.5 %) |
| Authorization correctness | 36 / 36 (100 %) |
| Optimization correctness | 41 / 41 (100 %) |
| Booking correctness | 20 / 20 (100 %) |
| Privacy | 22 / 22 (100 %) |
| Latency | all checks p50 8 ms, p95 1 694 ms; conversation turns p50 1 603 ms, p95 1 771 ms |
| Cost | $0.00 |

The two failures are **findings in the rules reader** (not fixed here; follow-up): I14 "Need to finish unloading by 5 pm"
→ intent OTHER, no latest finish; I15 "pahunch gaya hoon gate pe" (arrived at the gate) → read as a delay report. With
the model available these go through model extraction; the fallback reader should learn them.

### Security and database
| Check | Result |
|---|---|
| `tests/test_agent_not_operator.py` (role allow-lists, RLS coverage, definer safety, PUBLIC execute) | 17 / 17; allow-lists changed only for 0027 with written reasons; the **agent role gains nothing** |
| Migration 0027 | additive (3 tables, 4 definer functions owned by `setuhaul_rpc`, pinned `search_path`, EXECUTE revoked from PUBLIC, RLS forced on the two facility tables, `evaluation_runs` listed with reason); applies on a fresh database; works through the Data API path (jsonb/uuid only) |
| Console roles | operations keeps exactly its Step 6 set (`test_console_me.py`); new routes planner/ops_lead; another facility = 404/403 |
| Secrets scan of the diff | no tokens, keys, account ids or personal e-mail |
| PII in logs and metrics | none (tests assert phone, words and driver id absent) |

### Frontend
No build step (static files, same allow-list). `node --check app.js` passes; the served page under `/app/` rendered
both new tabs with **0 console errors** under the strict CSP. Screenshots: `docs/phase-g/screenshots/`.

### Load test (G7) — `PHASE_G_PERFORMANCE_REPORT.md`
**5 users**, 4 minutes, 544 requests, **0 errors (0.00 %)**, aggregate **p50 400 ms, p95 690 ms**, 2.28 req/s; 249 agent
turns, 0 failed; 0 double bookings.

### Dataset (G4)
Coverage of applicable trainer records **95.8 %** (91 of 95); 0 non-synthetic identities; the 4 known-bad trainer
bookings refused by the database.

### Docs (G5)
10 of 10 documents; 8 Mermaid diagrams (HLD L1, HLD L2, data, driver booking flow, delayed-truck/rescheduling flow,
failure and recovery flow, console recommendation flow, optimization decision flow); every referenced file and test
exists (checked by script).

## Acceptance checklist
- [x] G1 optimizer: hard constraints, soft objectives with documented weights, structured output, safe outcomes,
      determinism, never books, LLM never involved; tests including "no infeasible option ever".
- [x] G2 evaluation: 16 dimensions, deterministic business checks, report with totals, categories, accuracies,
      latency and cost; no LLM judge (limits documented).
- [x] G3 observability in the existing console, role and facility scoped, measured data only, "unavailable" where not
      measurable (queue/DLQ depth), synthetic label, structured logs with correlation ids, no PII.
- [x] G4 dataset from the trainer data with minimal, documented changes and configurable assumptions.
- [x] G5 ten documents with diagrams matching the code and deployment.
- [x] G6 seven real failures + four labelled controlled injections, each with a test.
- [x] G7 Locust, 5 users, isolated local environment, limits stated.
- [x] Migration 0027 additive; PostgreSQL 18.6 locally; **17.9 via the PR's CI**.
- [x] One PR, not merged, not deployed.

## Limitations
* PostgreSQL 17.9 was not run locally (only 18.6 is installed); the PR's CI pins 17.9.
* The optimizer ranking is **off by default** in the driver path (`SETUHAUL_OPTIMIZER_RANKING`); turning it on in an
  environment is the owner's booking-policy decision.
* The new Hindi/Hinglish `options_expired` wording needs the native-speaker review used for S4-R2-08.
* The console header pills ("AWS pilot", "booking execution OFF") are pre-existing static text and also appear on the
  local screenshots.
* No live-model evaluation (paid; needs approval). Load numbers are local and small-scale.
