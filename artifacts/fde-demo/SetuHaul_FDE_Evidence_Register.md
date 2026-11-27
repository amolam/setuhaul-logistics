# SetuHaul FDE Demo: Evidence Register

**Prepared:** 26 Nov 2026 · **Source of truth:** `origin/release/fde-sydney-pilot` at `e3c5bbe` (evidence head); deployed release commit `4b9ac56` · **All data synthetic.**
**Updated 27 Nov 2026:** Part D adds the 27 Nov evidence (fix plan release, recovery releases, DEMO-04). Where a
26 Nov row below is superseded, the row says so and points to Part D.

Every figure in `SetuHaul_FDE_Demo.pptx`, the playbook, the Q&A guide, the appendix, the cost analysis and the gap analysis appears in this register with its source. A claim that is not here should not be made.

**Status vocabulary** (from the master prompt, §3):

| Status | Meaning here |
|---|---|
| Designed | Written design exists; no code |
| Implemented | Code exists on the release branch |
| Unit tested / Integration tested | Covered by tests in `tests/` (local PostgreSQL) |
| CI-validated | Passed in GitHub Actions on the release commit (`ci` or `activate-booking`) |
| Pilot-demonstrated | Observed on the synthetic pilot (AWS Sydney) with recorded evidence |
| Deployed | Running in the synthetic pilot now |
| Production verified | **Nothing.** There is no production deployment |
| Planned / not implemented | Not built |
| Reported | Stated by the session that ran it on 26 Nov; **no committed artifact**. Verify before quoting |

Confidence: **High** = a committed file, CI log or live read-only AWS check. **Medium** = a committed document summarising a run whose raw output is not committed, or a figure computed from committed data. **Low** = reported only.

---

## Part A. Stage 1 discovery report

### A1. Source branch and commit

| Item | Value | How verified |
|---|---|---|
| Presentation source of truth | `release/fde-sydney-pilot` (remote), HEAD `e3c5bbe` ("DEMO-01 renewal evidence: smoke 11/11 on 4b9ac56 and renewal gate report") | `git log origin/release/fde-sydney-pilot` |
| Deployed release commit | `4b9ac56` (its gate evidence was collected from running release `31af6a6`; code is PR #33 + PR #35/#36) | smoke and renewal JSON `release_sha` = `4b9ac565…`; stack updated 26 Nov 14:21 UTC |
| Local working tree | branch `feat/renewal-category-e` at `e913385`. **Its code matches the release.** `git diff HEAD origin/release/fde-sydney-pilot` touches only 5 evidence JSON files | `git diff --stat` |
| Local release branch | **stale** (`e104a5b`, 24 Nov); not used | `git log release/fde-sydney-pilot` |
| Uncommitted change | `docs/operations/FULL_ACTIVATION_RUNBOOK.md` (owner edit). **Left untouched** | `git status` |

### A2. Relevant branches and PRs

All 36 PRs are merged. None is open. The ones the demo rests on:

| PR | What | Release PR |
|---|---|---|
| #1 | Phase E: AWS pilot (Option A), hosted staff console | – |
| #3 | Phase G: optimizer, observability tabs, evaluation suite, Locust | – |
| #4 | Phase G.1: LangSmith observability, model reliability | – |
| #5, #6, #8, #9 | Booking evidence renewal gate, database-side evidence guard (0030), Option P provisional authorization (0032), Recovery Release A (0033) | – |
| #11 | Phase G.2: LangSmith evaluation smoke (10 cases) | – |
| #15 | Dispatcher booking over Telegram, dashboards, dispatcher role (0035–0040) | #16 |
| #17 | Driver traceability (0041), offline LLM judge (0042), judge comparison | #18 |
| #23 | Synthetic pilot patch: 8 E2E defects, provisional limit 8 (0044) | #24 |
| #29, #31 | 0045 bounded recovery of an expired authorization; readiness fix | #30, #32 |
| #33 | Demo release: Telegram conversation visibility (0046), event-driven outbound, LangSmith thread correlation, `scripts/latency_report.py` | #34 |
| #35 | Renewal category E: owner-authorised Telegram test-driver bookings | #36 |

### A3. Verified capabilities (summary; detail in Part B)

- Driver delay handling: an LLM extraction (Claude Haiku 4.5 via OpenRouter, one forced tool call) feeds a fixed orchestration pipeline in code. Replies are deterministic templates in English, Hindi and Hinglish. A rules reader takes over when the model fails.
- Booking: feasibility in code (12 hard constraints), per-dock lock plus an EXCLUDE constraint, holds, and requests. A planner approves through a Step Functions approval workflow. Kill switch and expiring provisional authorization are database state.
- Telegram: live inbound and outbound for two synthetic test drivers only (allowlist). WhatsApp/Meta is deferred.
- Staff console: served by the API Lambda, with Cognito MFA and four staff roles (planner, ops_lead, operations, dispatcher). It has Dispatch, Schedule, Ops, Optimizer and Observability tabs.
- Observability: a custom LangSmith exporter. One `thread_id` correlates the main conversation path (inbound, agent turn, judge, outbound); 4 link-code and button-callback inbound runs lack it. Pseudonymous internal ids pass a format gate. No message text, phone numbers or chat ids are exported (26 Nov; **superseded on the synthetic pilot on 27 Nov**: full operational content, name/phone/credentials removed, one tree per message — Part D).
- Evaluation: golden sets v1–v4, held-out h1, live and scripted replay, the Phase G/G1/G2 deterministic suites, and the offline LLM judge accepted under the owner's two rules. Offline human calibration was completed on 26 Nov (owner-scored, one reviewer, not blind to the AI reference).

**Corrections to the brief:**
- **No LangChain** is used. The package is only named in LangSmith endpoint URLs; tracing uses the project's own REST exporter (`setuhaul/observability/langsmith.py`).
- **No Vercel.** The staff console is served by the API Lambda behind API Gateway.
- The LLM does **not** select tools and does **not** write replies (owner decision OD-3).

### A4. Deployment and pilot status (checked read-only, 26 Nov, without touching the database)

| Item | Value | Source |
|---|---|---|
| Stack | `SetuHaulPilot`, `UPDATE_COMPLETE`, last updated 26 Nov 2026 14:21 UTC | `aws cloudformation describe-stacks` |
| Booking | `ON (OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-DEMO-01)` | stack output `BookingMode` |
| Telegram | `LIVE (OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-TELEGRAM-LIVE-01)` | stack output `TelegramMode` |
| LLM judge | `ON budget USD 0.5/day sample 0.2` | stack output `LlmJudge` |
| LangSmith tracing | `ON` | stack output `LangSmithTracing` |
| Pilot schedules | recovery, outbound and approval-relay rules all **DISABLED**, so the pilot is asleep | `aws events list-rules` |
| Production | Refused by code (`production` environment rejected) | `setuhaul/agent/models.py`, docs/3-build/README.md |
| Authorization validity after renewal | **Not re-checked.** Reading `/health` would resume Aurora. The pre-renewal reading showed `booking_provisional_until` 26 Nov 20:35 IST; the renewal run then passed. The ~7-day validity was **reported** by the previous session | renewal_gates.json, run 45836951650 |

### A5. Latest test and CI evidence (release commits)

| Run | Workflow | Commit | Result |
|---|---|---|---|
| 45836948268 | ci | `e3c5bbe` | test ✔, synth ✔, deploy skipped. **2080 passed, 1 skipped** (full suite, PostgreSQL 17.9). **678 passed** (Data API emulated subset) |
| 45836951650 | activate-booking (renewal) | `e3c5bbe` | test ✔ (2080 passed, 1 skipped), synth ✔, activate ✔ |
| 45833537353 | activate-booking (provisional) | `4b9ac56` | test ✔ (2080 passed, 1 skipped), synth ✔, activate ✔ |
| 45833484508 | ci | `4b9ac56` | success |

The brief's "about 544 tests" was the **targeted local** run in PR #33, not the full suite. A flaky redaction test has been seen in **local** runs (reported); CI on the release passed.

### A6. Golden dataset and evaluation status

Golden sets v1–v4 and held-out set h1 were run with the model on 18 Nov (prompt p3); v4, h1 and live replay v4 were re-run on the deployed prompt p5 on 26 Nov. Live replay v4 passed 62/64 on p5 (61/64 on p3). The scripted Phase G/G1 suite passed 111/111. G2 LangSmith smoke passed 10/10 (scripted). The offline judge was run twice on 25 Nov; prompt judge-p2 is **ACCEPTED** under both owner rules, and the owner's human scores (26 Nov) put it within ±1 on 93.3% of 60 ratings: **ACCEPTED for advisory use** (one reviewer, not blind). Evals are **not** a CI gate: paid evals run manually with approval, and scripted suites run in tests.

### A7. Observability status

LangSmith project `setuhaul-synthetic-pilot` is on. Since the demo release, the main conversation path is thread-correlated across 4 components and the judge runs as a child run. **Gaps:**
- 4 inbound runs have no `thread_id` (link codes and button callbacks);
- there is no trace-based alerting;
- the agent turn has no per-stage breakdown.

### A8. Cost and performance data actually available

| Data | Available? |
|---|---|
| OpenRouter spend, evals and dev to 18 Nov | Yes: ≈ $3.16 (key checkpoint), itemised in `docs/operations/COSTS.md` |
| Offline judge spend | Yes: $0.106965 + $0.098315 = **$0.205280** (usage ledger in the result files) |
| Pilot LLM spend since 23 Nov | **No.** It lives in the pilot's `model_usage` table. Reading it would resume Aurora, so it was not read |
| Pilot judge cost per judgement | Reported: ≈ $0.0033 (9 turns scored on 26 Nov) |
| AWS bill | **No.** Not in the repository. Owner to read the Billing console. Only estimates exist (`docs/release/09`) |
| LangSmith, Telegram, Cognito bills | **No** |
| Pilot Telegram latency | Before: committed (commit `ff4076d`, 7 exchanges). After: **reported** (6 exchanges, `scripts/latency_report.py` output not committed) |
| Local load test | Yes (`docs/phase-g/PHASE_G_PERFORMANCE_REPORT.md`, 23 Nov, rules reader, local only) |

### A9. Important gaps and missing evidence

1. Post-release latency figures and the "9 scored turns" are reported, not committed.
2. There is no AWS billing data.
3. Human judge calibration has one reviewer, who had seen the AI reference the day before; it is not blind and not multi-reviewer. The online pilot judge has not been calibrated on pilot rows (`pilot_calibration.py` not run).
4. There is no current screenshot of Telegram, the conversation view or LangSmith. The screenshots in `screenshots/` are from Phase G/G1 (23 Nov).
5. Authorization validity was not re-read.
6. There is no production baseline for business outcomes.
7. The latency targets (< 15 s, < 2 s) are not written in the repository; they are the owner's demo targets as reported.

---

## Part B. Claim register

### B1. Business problem and scope

| Claim | Evidence | File / report / commit | Status | Confidence |
|---|---|---|---|---|
| 280–360 loads/day, 6 facilities, 32 docks, 15–25 exception msgs/day, 20–35 msgs in a 30-min spike, 5 coordinators/shift, 0 live GPS feeds | Client brief figures, transcribed | `docs/1-frame/01_problem_definition_sheet.md` §2; `docs/brief/*.pdf` | Brief input, not measured | High (as brief data) |
| "High-correctness system under burst contention" framing | Phase 1 analysis | same file §2 "Consequence" | Designed | High |
| Five personas and decision rights | Stakeholder table; role set in code | `01_problem_definition_sheet.md` §3; `setuhaul/api/security.py` (`STAFF_ROLES = planner, ops_lead, operations, dispatcher`); PR #15 | Implemented | High |
| Dispatcher requests but decides nothing | Role definition | PR #15 (migration 0037), `tests/test_dispatcher_role.py` | CI-validated | High |
| Manual process shown on slide 2 | **Assumption** drawn from the brief; not observed | slide 2 label | Assumption | – |

### B2. Architecture and AI design

| Claim | Evidence | File / report / commit | Status | Confidence |
|---|---|---|---|---|
| LLM used only for extraction, one call per turn, decides nothing | Module docstring and code | `setuhaul/agent/orchestrator.py` lines 1–11 | Implemented, CI-validated | High |
| Forced tool `record_driver_report`; pydantic `extra="forbid"` | Schema | `setuhaul/agent/extraction.py` `class Extraction` | CI-validated | High |
| Replies are templates (EN, HI, Hinglish); no model writes replies (OD-3) | Templates; build log | `setuhaul/agent/replies.py`; `docs/3-build/README.md` | Deployed | High |
| Model: `anthropic/claude-haiku-4.5` via OpenRouter, pinned `amazon-bedrock` route, `zdr: true`, no fallback model | Provider table | `docs/3-build/README.md` "Model provider"; `setuhaul/agent/openrouter.py` | Deployed | High |
| Timeouts connect 3 s / response 10 s; ≤ 1 retry; fallback rules reader | Same | same | CI-validated | High |
| Prompt version `p5-2026-11-21`; driver text is "data, not instructions" | Prompt file | `setuhaul/agent/prompts.py` | Deployed | High |
| 10 agent tools (names on slide 7) | `tool("…")` calls | `orchestrator.py` | Implemented | High |
| Safety-word net in code (EN/HI/Hinglish) | Regex | `extraction.py` `SAFETY_WORDS` | CI-validated | High |
| FastAPI via Mangum on API Lambda; console served by the API | Requirements; stack | `requirements-aws.txt`; `infra/setuhaul_stack.py`; stack output `ConsoleUrl` under the API | Deployed | High |
| SQS FIFO inbound with DLQ; Step Functions approval; EventBridge 1-min schedules; Secrets Manager; KMS; CloudWatch alarms → SNS | Stack | `infra/setuhaul_stack.py`; stack outputs | Deployed | High |
| Aurora PostgreSQL 17.9 Serverless v2 via Data API (owner exception T33-P) | Gates; CI image `postgres:17.9` | renewal_gates.json `AURORA`; CI log | Deployed | High |
| 46 migrations, forward-only | Directory listing | `migrations/0001…0046` | Implemented | High |
| No NAT, no RDS Proxy; scale to zero | Plan | `docs/release/09_LOW_COST_PILOT_PLAN.md` | Deployed (design choice) | High |
| No LangChain; no Vercel | Grep of code and requirements | `requirements*.txt`; `setuhaul/observability/langsmith.py` | Fact | High |
| Event-driven outbound plus 1-min fallback tick | Commit message, code | `ff4076d`; `setuhaul/notify/kick.py` | Deployed | High |

### B3. Guardrails, security, reliability

| Claim | Evidence | File / report / commit | Status | Confidence |
|---|---|---|---|---|
| Live smoke 11/11 on `4b9ac56` (checks 1, 2a–2e, 3, 4, 5, 6, 7) | Smoke JSON, `all_pass: true` | `docs/release/evidence/smoke/option_d_smoke_20261126T140741Z.json` (release branch) | Pilot-demonstrated | High |
| Check 3: duplicate YES / repeated decision change nothing | same | same | Pilot-demonstrated | High |
| Check 4: same dock time cannot be claimed twice (exclusion constraint) | same | same | Pilot-demonstrated | High |
| Check 5: agent, other-facility and no-identity decisions refused | same | same | Pilot-demonstrated | High |
| Check 6: kill switch engaged → `BOOKING_KILLED`, escalated, nothing booked | same | same | Pilot-demonstrated | High |
| Check 7: only synthetic identities; no delivery outside the test sink or Telegram allowlist | same | same | Pilot-demonstrated | High |
| Integrity proof: 20 concurrent holds → 1 winner, 0 overlaps, 9/9 checks; 50 in the suite | Proof document plus raw output | `docs/challenge/INTEGRITY_PROOF.md`; `docs/challenge/evidence/integrity_proof.txt` | Integration tested (local PG 18.6) | High |
| Declined offer can never be held or booked | Migration and test | `migrations/0040_*`; PR #15 | CI-validated | High |
| Requester cannot decide own request | Migration 0036 | PR #15 | CI-validated | High |
| Telegram allowlist fails closed | Code | `setuhaul/notify/telegram.py` `AllowlistedTelegram` | Deployed | High |
| Renewal gates: AWS_QUEUE, RECOVERY_WORKER, BOOKING_WORKFLOW PASS; AURORA PASS WITH PILOT EXCEPTION; WORKLOAD_IDENTITY PARTIAL; OUTBOUND_DELIVERY BLOCKED, under three owner-approved exceptions | Gate report | `docs/release/evidence/renewal_gates.json` (release branch) | Pilot-demonstrated | High |
| Renewal attribution categories A–E, nothing unresolved | Renewal run plus PR #35 | run 45836951650; `setuhaul/ops/renewal.py` | CI-validated + pilot | Medium (attribution detail reported) |
| DLQ, lease-loss and approval-workflow drills | Drill evidence | `docs/release/evidence/drill_*.json/.log` | Pilot-demonstrated (23 Nov) | High |
| Approval expiry via Step Functions timeout | ASL `States.Timeout → ExpireApproval`; drill | `setuhaul/workflow/approval_workflow.asl.json`; `drill_approval_run2.json` | Pilot-demonstrated | High |
| Offer expiry 5 min; hold TTL 180 s | Code | `setuhaul/api/service.py` | Implemented | High |
| No automatic chase when a driver does not answer | Absence in code and docs | – | Gap | Medium |

### B4. AI evaluation

| Claim | Evidence | File | Status | Confidence |
|---|---|---|---|---|
| Held-out h1, 167 turns, model, **prompt p5** (26 Nov): intent 100%, ETA recall 97.8%, ETA precision 100%, option pick 91.7% (11/12), all fields right 158/167 (94.6%), schema-valid 100%, all gates PASS; $0.6125 | Result file | `evals/results/golden_h1_model_20261126_2242.json` | Tested (paid eval run) | High |
| Held-out h1, model, prompt p3 (18 Nov): intent 98.0%, ETA recall 97.8%, option pick 100%, all fields right 158/167 | Result file | `evals/results/golden_h1_model_20261118_0907.json` | Tested (paid eval run) | High |
| h1 p5 misses (9 turns): BB05 pick (expected option 1, got none; **new on p5**); earliest time ×3 (BB02, AT06 ×2); ETA ×2 (THR011); latest kind (BB10); exception kind (L08); language hi vs hi-Latn (H06). THR007 intent miss on p3 is fixed on p5. Causes not established | Result file `turns[].mismatches` | both h1 model files | Tested | High |
| Held-out h1, rules reader: intent 61.2%, ETA recall 69.2%, all fields 99/167 (59.3%) | Result file | `golden_h1_rules_20261118_0907.json` | Tested | High |
| Golden v4, 76 turns, **prompt p5** (26 Nov): 74/76 all fields right, intent 100%, ETA recall 100%, all gates PASS; misses BB10 latest kind, AT06 earliest; $0.132 (36 paid, 40 cached) | Result file | `golden_v4_model_20261126_2234.json` | Tested (paid) | High |
| Golden v4, 76 turns, prompt p3: model 73/76 all fields right, intent 100%, ETA recall 100% (gates PASS); rules 60/76 | Result files | `golden_v4_model_20261118_0910.json`, `golden_v4_rules_…` | Tested | High |
| Golden v1 prompt history: p1 schema-valid 95.5% → p2 100% | Result files | `golden_v1_model_20261118_0228/0234/0237.json` | Tested | High |
| All golden, held-out and replay datasets are synthetic | Dataset files; h1 generated by Sonnet | `evals/golden/*` | Fact | High |
| All model-backed evals (golden v4, held-out h1, live replay v4) re-run on the deployed prompt p5 on 26 Nov | `prompt_version: p5-2026-11-21` in the result files | `golden_*_model_20261126_*.json`; `replay_v4_live_20261126_2305.json` | Tested | High |
| Extraction latency p50 2.17–2.40 s (eval runs) | Result files `latency_ms_p50` | golden_* model files | Tested | High |
| Live replay v4, **prompt p5** (26 Nov, target date 27 Nov): 62/64 scenarios, 90/92 turns, 0 hard-gate violations, 0 errors, 3 skipped; $0.3017 | Result file | `evals/results/replay_v4_live_20261126_2305.json` | Tested (paid) | High |
| Live replay v4, prompt p3 (18 Nov, target date 19 Nov): 61/64, 89/92, 0 hard-gate violations; failures BB09, BB12, AT02 | Result file | `evals/results/replay_v4_live_20261118_0803.json` | Tested (paid) | High |
| Replay failures on p5: BB12 (status question answered with slot options; `get_booking_status` not called) and AT02 (escalated NO_FEASIBLE_SLOT where options were expected). Both also failed on p3, at a different target date, so they are persistent; root causes not established. BB09 failed on p3 and passes on p5 | Result files (`pass`, runner output) | both replay files | Tested; cause unknown | High |
| Scripted replay v4: 64/64, 92/92 | Result file | `replay_v4_scripted_20261118_1020.json` | Tested | High |
| G1 agent eval 111/111 (67 Phase G scenarios + 44 regression cases), scripted, $0 | Result file | `evals/results/g1_langsmith_eval_20261123_1426.json` | Tested | High |
| Phase G eval 67/67: tool selection 41/41, parameters 18/18, intent 16/16, authorization 36/36, booking 20/20, privacy 22/22 | Result file | `evals/results/phase_g_eval_20261123_1425.json` | Tested | High |
| G2 LangSmith smoke 10/10, 96 runs exported, 0 leaks | Result file | `evals/results/g2_smoke_20261125_015757.json` | Tested | High |
| Judge dataset: 30 synthetic replies (J01–J30), 3 dimensions | Dataset | `evals/judge/dataset_v1.jsonl`; `docs/evaluation/LLM_JUDGE.md` | Implemented | High |
| Judge p1: 18/30 unparseable, $0.106965, NOT accepted | Result plus report | `evals/results/judge_20261125_170901.json`; `LLM_JUDGE.md` | Tested | High |
| Judge p2: 95.0% within ±1 on 60 ratings, exact 76.7%, MAE 0.333; baseline 88.3% / MAE 0.55; 5/5 negative cases vs 0/5; 0 unparseable; $0.098315; ACCEPTED under both owner rules | Result plus report | `judge_20261125_172110.json`; `docs/evaluation/llm_judge_run2_report.md`; `LLM_JUDGE.md` | Tested | High |
| The 95.0% above is against model-generated reference scores ("ChatGPT independent reference"), so that figure is model-to-model agreement | Label in the result file | same | Fact | High |
| **Human calibration (offline, 26 Nov):** 20 cases, 60 ratings, scorer Amol. Judge-p2 within ±1 on 93.3%, exact 83.3%, average error 0.30, bias −0.13; per dimension clarity 90% / tone 95% / language 95% within ±1. Verdict ACCEPTED (advisory scores). Always-5 baseline against the same scores: 88.3% within ±1, exact 80.0%, average error 0.517 | `judge_calibration` output (re-run locally, free, no file written) | `evals/judge/human_scores_v1.json`; `judge_20261125_172110.json` | Tested (one reviewer) | Medium |
| Calibration limits: one reviewer; not blind (the reviewer had reviewed the AI reference on 25 Nov). The owner's scores equal the AI reference on 53/60 ratings (owner-score agreement with the reference, not judge agreement) | File comparison | `human_scores_v1.json` vs `ai_reference_scores_v1.json` | Fact | High |
| 4 disagreements of 2+ points against the owner's scores: J13 language (owner 5, judge 1) and J14 clarity (5, 2) are judge false positives; J17 clarity (5, 2) and J20 tone (1, 3) are planted-defect cases where the judge missed part of the defect | `disagreements_2plus` | same | Tested | High |
| Online pilot judge rows still record `HUMAN_CALIBRATION_OUTSTANDING`; calibration on pilot rows (`pilot_calibration.py`) not run | Code constant | `setuhaul/agent/online_judge.py` | Gap | High |
| Online judge: advisory, 20% deterministic sampling, run cap, judge sub-cap inside the shared cap, one attempt, never raises | Code | `setuhaul/agent/online_judge.py` | Deployed | High |
| After deploy, judge skipped with `budget:shared_daily_cap`; exact usage balance not available | Reported observation | – | Reported | Low |
| Configured judge budget $0.50/day is the judge's own sub-cap; judge reservations are also checked against the shared $0.25/day cap minus the agent reserve (default half the cap; no override in the stack). **Superseded by DEMO-04:** shared $2.00, judge $0.75 (Part D) | Code and stack output | `setuhaul/agent/online_judge.py` (`JudgeLedger.reserve`, `agent_reserve_usd`); stack output `LlmJudge`; `infra/setuhaul_stack.py` | Deployed (until DEMO-04) | High |
| Invalid model output is not retried; that turn falls back to the rules reader (no separate re-ask path) | Code | `setuhaul/agent/orchestrator.py` `_extract_inner` | CI-validated | High |
| 9 turns scored earlier on 26 Nov (`quality:clarity/tone/language_match`) at ≈ $0.0033 each | Reported observation | – | Reported | Low |

### B5. Observability

| Claim | Evidence | File / commit | Status | Confidence |
|---|---|---|---|---|
| One `thread_id` (sha256, 16 hex) across `telegram_inbound`, `agent_turn`, `llm_judge`, `telegram_outbound` on the main conversation path (not the 4 link-code/callback inbound runs) | Commit message plus tests | `ff4076d`; `tests/test_demo_reliability.py` | CI-validated; pilot-demonstrated (reported) | High (code) / Medium (pilot) |
| `llm_judge` is a child run of `agent_turn`; `quality:*` feedback stays on the turn | same | same | CI-validated | High |
| Driver, shipment and facility ids plus channel on traces, CloudWatch `agent.turn` lines and the Ops metrics row; malformed id → `[INVALID_ID]` | PR #17 | `setuhaul/observability/identifiers.py`; `tests/test_driver_traceability.py` | CI-validated | High |
| LLM span carries model, input/output tokens, `cost_usd` | Code | `orchestrator.py` ~line 418 | Implemented | High |
| No message text, chat id, phone or token in metadata. **Superseded on the synthetic pilot (owner, 27 Nov):** content `full` exports message text, model input/output and tool results, and **DEMO-04** masks only the driver's name and phone and removes credentials (Part D). The `off`/`on` modes keep the old rule | Commit plus redaction | `ff4076d`; `setuhaul/observability/redaction.py` | CI-validated | High |
| Tracing overhead p50 13 ms (1.4%) per turn | Perf file | `evals/results/g1_perf_20261123_1500.json` | Tested (local) | High |
| 4 inbound runs without `thread_id` | Reported observation | – | Known issue | Low/Medium |

### B6. Performance

| Claim | Evidence | Source | Status | Confidence |
|---|---|---|---|---|
| Before: received→queued p50 1.2 s; queued→agent reply 8.3 s (p50); reply→notification p50 43.9 s / p95 58.6 s; send 2.0 s; end to end p50 53.7 s / p95 72.6 s (7 exchanges) | Commit message | `ff4076d`; PR #33 body | Pilot-demonstrated | High |
| After: end to end p50 13.1 s / p95 15.0 s; reply→outbound p95 1.3 s; queue→send p95 ≈ 2.2 s; agent turn ("LLM reply") 8.5–11 s; 6 exchanges. **Preliminary** | latency_report output | `scripts/latency_report.py` (output not committed) | Reported | Low |
| Before/after comparability: the script and the before numbers come from the same commit (`ff4076d`) with matching stage names. End to end and reply→outbound use the same timestamps. "send" vs "queue-to-send" cannot be mapped with certainty. Samples are small and different (7 vs 6). No speed-up multiple or service level is claimed | Script source + commit message | `scripts/latency_report.py`; `ff4076d` | Analysis | Medium |
| Targets < 15 s end to end and < 2 s outbound | Owner's demo targets | not in repo | Reported | Low |
| Two drivers booked, English and Hinglish; 17 messages stored, none lost | Reported observation | – | Reported | Low |
| Local Locust: 544 requests, 0 failures, p50 400 ms, p95 690 ms; agent turn p95 2,279 ms; optimizer p95 39 ms; 0 overlapping bookings | Perf report | `docs/phase-g/PHASE_G_PERFORMANCE_REPORT.md` | Tested (local only) | Medium (raw file not reviewed here) |
| Aurora resume from 0 ACU: 14.6 s first Data API call | Report | `docs/release/10_EXPRESS_AURORA.md` | Pilot-demonstrated (once) | Medium |
| Phase G turn p50 1,940 ms / p95 2,270 ms (scripted, local) | Result file | `phase_g_eval_20261123_1425.json` | Tested | High |

### B7. Cost

| Claim | Evidence | Source | Status | Confidence |
|---|---|---|---|---|
| OpenRouter spend for evals and dev to 18 Nov ≈ $3.16 (key checkpoint: $4.59 used − $1.43 before) | Ledger table | `docs/operations/COSTS.md` | Actual | High |
| Line items: G3 runs $1.23 (348 calls, computed), h1 scoring $0.60 (167), Sonnet generation $0.16 (65), spikes $0.14, probes ~$0.03 (inferred), live replays ~$1.00 (inferred) | same | same | Actual (computed / inferred as labelled) | High / Medium |
| Offline judge runs $0.205280 (ledger) | Result files | `judge_*.json` `actual.charged_usd_by_ledger` | Actual | High |
| Per extraction ≈ $0.0036 measured (h1: 326,797 in / 55,289 out tokens for 167 turns at $1/$5 per M → $0.603) | Computed from result file | `golden_h1_model_20261118_0907.json` | Computed | High |
| Per extraction typical ≈ $0.0013 (900 in / 80 out) | Design estimate | `COSTS.md` | Estimate | Medium |
| App cap $0.25/day. **Superseded by DEMO-04:** $2.00/day, judge $0.75/day (owner, 27 Nov; Part D) | Owner approval 21 Nov | `COSTS.md` | Deployed (config, until DEMO-04) | High |
| OpenRouter key limit $10 total, no reset (read from the key endpoint, 26 Nov); not a monthly budget; $5.98 used and $4.02 remaining at the 26 Nov check, taken after the two golden re-runs and before the live replay. The owner confirmed the pilot and the evaluation runs use this same key. The 21 Nov approval was $3/month; the setting in force is $10, no reset | Owner-run read of `/api/v1/key`; owner confirmation | `docs/operations/COSTS.md` | Reported | Medium |
| p5 re-runs, 26 Nov: $1.0462 across the three successful paid runs (v4 $0.1320, h1 $0.6125, replay $0.3017). Calibration was free | `cost.spent_usd` in the result files | three 26 Nov result files | Computed | High |
| A first v4 attempt crashed in the runner (unreported token count) after ≈ 38 paid calls: ≈ $0.14 ESTIMATE ($0.0036 per call), in no result file; the key counter is authoritative | Cache entries; runner traceback | `evals/cache/model_calls.jsonl` | Estimate | Low |
| Judge configured $0.50/day but bounded by the shared cap (until DEMO-04; then $0.75 within $2.00) | Stack output plus code | `LlmJudge` output; `online_judge.py` | Deployed | High |
| AWS idle ≈ $6.50–6.90/month; a few hours a week ≈ $8–10; always-on with WAF ≈ $85–100 | Estimates | `docs/release/09_LOW_COST_PILOT_PLAN.md` | Estimate | Medium |
| Unit economics and scale scenarios | Projection | `SetuHaul_FDE_Cost_Analysis.md` | Projection | – |

### B8. Demo and screenshots

| Item | Evidence | Status |
|---|---|---|
| `screenshots/console_observability_phase_g_20261123.jpg`, `console_optimizer_recommendation_20261123.jpg`, `console_reliability_eval_phase_g_20261123.jpg` | Copied from `docs/phase-g/screenshots/` (synthetic data; 23 Nov; predate Telegram and DEMO-01) | Anonymized (synthetic ids only) |
| `screenshots/console_langsmith_links_local_failure_injection.jpg`, `console_model_ledger_local_failure_injection.jpg` | Copied from `docs/phase-g1/screenshots/` (local failure-injection run; org shown as `synthetic-org`) | Anonymized |
| Demo E2E evidence (A1, A3–A6, S1) | `docs/release/evidence/demo/demo_e2e_20261125.json` | Pilot-demonstrated (25 Nov). Contains synthetic +999 numbers; do not show raw on screen |

---

## Part C. Things that must not be claimed

- Production readiness, production deployment or production verification of anything.
- Measured business impact (coordinator time, rebooking speed, cost savings). There is no baseline.
- The judge as "accurate", "validated", "blind-calibrated" or multi-reviewer agreed. Say: human calibration completed, owner-scored, one reviewer, not blind to the AI reference; advisory only.
- The 53/60 figure as judge agreement. It is the owner's scores matching the AI reference.
- That every metric improved on p5. Option pick fell from 100% to 91.7% (BB05); BB12 and AT02 still fail.
- A combined score across golden, held-out and replay sets.
- A $3/month key limit, or any monthly capacity derived from the $10 non-resetting key limit.
- The ≈ $0.14 aborted-attempt figure as a verified cost.
- LangChain usage, Vercel hosting, multi-agent orchestration, or the LLM choosing tools.
- WhatsApp delivery to real phones.
- The latency targets as met.
- An AWS cost figure as billed.
- Any count of "tests" other than the ones recorded with their commit: 2,080 passed / 1 skipped (full suite, CI on `e3c5bbe`, 26 Nov); 2,201 passed / 2 skipped (local full suite, fix plan, 27 Nov); later counts in Part D.
- Absolute guarantees such as "no dock is ever double-booked". State the tested evidence: 20 concurrent (local proof) and 50 (suite) gave one winner and no overlap, plus live smoke check 4.
- "Exactly-once" processing. Delivery is at least once; FIFO ordering per driver, de-duplication and idempotent business effects.
- A latency speed-up multiple ("4×", "45×") or any service level; the after-sample is preliminary and uncommitted.
- "No personal data" in general. Say (DEMO-04, synthetic pilot only): the driver's name and phone and every credential are removed from traces; message text, model input/output, tool results and platform ids are exported for debugging. Other environments keep the stricter `off`/`on` modes.
- That p5 results from 26 Nov apply to any later prompt version without a re-run. **The deployed prompt is p6 (27 Nov) and its paid golden re-run was deferred by the owner: model-backed extraction on p6/schema v2 is unvalidated.**
- That the judge scores live turns, until a sampled `llm_judge` run is seen on the pilot after DEMO-04.
- That the two stopped releases (authorizations 72 and 73) were successful; they were revoked by the runbook's safe-stop rule (Part D).

---

## Part D. 27 Nov 2026 evidence

### D1. What changed and where it is

| Item | Evidence | Status | Confidence |
|---|---|---|---|
| 27 Nov fix plan (nine issues): appointment lifecycle gate → dock → completed (operations/ops_lead), automatic no-show at each facility's grace, dispatcher Rebook / cancellation after a no-show, audited history backfill, free drivers, explicit offer target, a dispatcher's new time for a confirmed booking always to a planner, escalations in "Pending your decision", requested dates (facility zone, ≤ 7 days, never another day), full pilot trace content | PR #45 (merge `06f4f25`), migration 0048; `docs/operations/FIX_PLAN_27NOV.md`, `FIX_PLAN_27NOV_EVIDENCE.md` | Deployed (release `5b661a1`) | High |
| Static-review fixes: no-show job eligibility before the batch limit; migration-aware gate trigger check | commit `a0adbbd`; `tests/test_gate_triggers_0048.py` | Deployed | High |
| Category D for `a95aaaa1` (owner-confirmed test origin) and category X (an expired, never-confirmed request holding no dock time needs no attribution) | PRs #47, #49; `owner_test_origin_20261127.json`; `tests/test_expired_exemption_demo03.py` | Deployed | High |
| Migration 0049: one additional recovery from a recovery, pilot only; third hop refused | PR #51; `tests/test_recovery_release_a.py`, `test_recover_expired_0045.py`, `test_readiness.py` | Deployed | High |
| Smoke credential check before any driver message (refreshable credentials refreshed and proven; fixed ones need 15 min) | PRs #49, #53 | Implemented (operator script) | High |
| **DEMO-04** (this PR): traces masked to name + phone + credentials only; guard never scans ids; one tree per message (0050); $2/day overall, $0.75 judge (checked activation input); refused request after an accepted offer releases the hold and resets the conversation (reason logged); Resolve… for planners; live holds only on the schedule; smoke waits up to 60 s for the kill-switch log | branch `feat/fde-demo-finalization`; focused tests in `test_trace_content_full.py`, `test_dispatch_offers.py`, `test_infra.py`, `test_expired_exemption_demo03.py` | Implemented and tested locally; **deploy pending** (D3) | High (code) |

### D2. Test and release evidence

| Run | Result | Source |
|---|---|---|
| Fix plan, full suite (local, PostgreSQL 18.6) | 2,201 passed, 0 failed, 2 skipped | `FIX_PLAN_27NOV_EVIDENCE.md` |
| Fix plan, CI's Data API subset | 745 passed | same |
| Fix plan, CI on `a0adbbd` and the release `1126385` | test and synth passed | GitHub Actions |
| Release `e7c3a2c` (first attempt): provisional run deployed it and applied 0048 (48 migrations live) | run `45960308726`; smoke 07:54Z 10/11 (check 6 missed the log line by CloudWatch lag; the agent did refuse), 08:00Z 11/11; renewal BOOKING_WORKFLOW FAIL (execution `a95aaaa1` unattributed) → **authorization 72 revoked** (runbook §7.5) | release branch `cb3659d` |
| Release `326085a` (recovery, DEMO-02): smoke 11/11 | renewal UNRESOLVED (request `3cba692b` from a smoke attempt that crashed on expired AWS credentials) → **authorization 73 revoked** | release branch `9e97744` |
| Release `5b661a1` (recovery, DEMO-03, 0049 applied) | smoke 11/11 (`option_d_smoke_20261127T100646Z.json`); renewal BOOKING_WORKFLOW **PASS**; renewal run `45980121138` succeeded; `/health` `booking_enabled: true`, `authorized` (10:23 UTC) | release branch `46b49b5` |
| No-show history backfill | dry run `81cd9dbf` (11 eligible, 0 excluded) reviewed by the owner; apply run `7ad06da7`: 11 transitioned | `no_show_backfill_runs` |
| DEMO-04 affected tests (local) | 450 + 279 + 132 passed (trace content, infra, dispatch offers, dispatch 0048, outbound, reassignment, traceability, demo reliability, console, orchestrator, migrations, gate triggers, lifecycle, move, recovery, readiness, judge, full activation, concurrency, DB requests, API) | this branch |

### D3. Pending at the time of writing (to be completed by the DEMO-04 deploy)

- PR CI on this branch, owner approval, merge, release, provisional and renewal runs under `OWNER-APPROVAL-20261127-DEMO-04`.
- On the pilot after the deploy: one Telegram message traced as one tree (inbound → agent_turn → outbound) with message text and the name/phone masked; a sampled `llm_judge` child; stack outputs `LlmJudge` (0.75) and the agent's `SETUHAUL_LLM_DAILY_BUDGET_USD` (2).
- These results are recorded on the release branch as evidence commits, as for every release.

### D4. Open items (not done)

- **Paid golden extraction re-run for prompt p6 / schema v2: deferred by the owner** (≈ $0.28). Model-backed date and reschedule extraction is unvalidated; the rules-reader baseline is unchanged from `main`.
- Hindi and Hinglish wording of the new date replies: native review outstanding.
- OpenRouter key: $10 lifetime limit, no reset; at $2/day it binds first (Cost Analysis §2.5).
- LangSmith retention and access policy for full-content traces (synthetic data only) is not documented.
