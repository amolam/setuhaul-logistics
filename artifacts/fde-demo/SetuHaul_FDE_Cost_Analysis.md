# SetuHaul FDE Demo: Cost and Performance Analysis

**Scope:** release `4b9ac56` (evidence head `e3c5bbe`), synthetic pilot `pilot-ap-southeast-2` · prepared 26 Nov 2026 ·
**updated 27 Nov 2026** for the owner's new model budgets (DEMO-04: **$2.00/day overall, of which $0.75/day for the
judge**). The 26 Nov figures below are kept where they describe what was measured then; the budget, capacity and
binding-limit statements are updated (§2.5, §4.3, §4.4) and marked.

Every number has one of these labels:
- **ACTUAL**: billed or ledger-recorded money;
- **COMPUTED**: provider-reported tokens × list price, from a committed result file;
- **INFERRED**: credit-balance differences;
- **ESTIMATE**: a project estimate from list prices;
- **PROJECTION**: this document's model for volumes that have not happened;
- **REPORTED**: observed on 26 Nov but not committed;
- **NOT AVAILABLE**.

A projection is never actual spend.

---

## 1. What is known, and what is not

| Cost item | Status | Value | Source |
|---|---|---|---|
| OpenRouter, evals + development to 18 Nov | ACTUAL (key checkpoint) | ≈ $3.16 | `docs/operations/COSTS.md` |
| Offline LLM-judge runs, 25 Nov (2 × 30 judgements) | ACTUAL (usage ledger) | $0.205280 | `evals/results/judge_20261125_*.json` |
| p5 eval re-runs, 26 Nov (golden v4, held-out h1, live replay v4) | COMPUTED (result files) | $1.0462 | three `*_20261126_*.json` result files |
| Aborted first v4 attempt, 26 Nov (≈ 38 calls) | **ESTIMATE** | ≈ $0.14 | cache entries; not in any result file |
| OpenRouter key limit and counter | REPORTED (owner-run key endpoint read, 26 Nov, before the replay) | $10 limit, no reset; $5.98 used, $4.02 remaining | – |
| Pilot model spend since 23 Nov (agent extraction and online judge) | **NOT AVAILABLE** | In the pilot's `model_usage` / `model_budget_day` tables. Not read: reading them would resume the paused Aurora cluster. Bounded above by the daily cap ($0.25/day until the DEMO-04 release; $2.00/day after it) | `COSTS.md` "How to inspect usage" |
| Pilot judge cost per judgement | REPORTED | ≈ $0.0033 (9 turns scored on 26 Nov) | previous session |
| AWS bill | **NOT AVAILABLE** | Owner to read Billing → Bills (free) before quoting | – |
| AWS monthly estimate | ESTIMATE | idle ≈ $6.50–6.90; a few hours a week ≈ $8–10; always on with WAF ≈ $85–100 | `docs/release/09_LOW_COST_PILOT_PLAN.md` |
| LangSmith | **NOT AVAILABLE** | Plan and invoice unknown; not modelled | – |
| Telegram Bot API | No charge known for bot messaging; not verified against a bill | – | – |
| Cognito | NOT AVAILABLE | Staff-only user pool, a handful of users | – |
| Vercel | **Not used.** The console is served by the API Lambda | – | – |

## 2. LLM cost

### 2.1 Provider, model and prices

| Item | Value |
|---|---|
| Provider | OpenRouter, pinned to the `amazon-bedrock` upstream route; `zdr: true`; `allow_fallbacks: false` |
| Model | `anthropic/claude-haiku-4.5`, the only production-path model (extraction and judge) |
| Price basis | $1.00 per M input tokens, $5.00 per M output tokens (OpenRouter list price, 21 Nov 2026; `evals/budget.py`, `setuhaul/agent/openrouter.py`) |
| Fallback model | **None.** A failed call ends in the rules reader at $0. Fallback-model spend is $0 by design |
| Other models used in development only | `anthropic/claude-sonnet-5` for held-out set generation ($0.16) and the B-AI-3 phrasing spike |

### 2.2 Development and evaluation spend to date

| Activity | Calls | USD | Basis |
|---|---|---|---|
| G3 golden extraction runs (p1, p2, p2 final, p3 on v3, p3 on v4) | 348 | 1.23 | COMPUTED |
| Held-out h1 scoring (Haiku) | 167 | 0.60 | COMPUTED |
| Held-out h1 generation (Sonnet) | 65 | 0.16 | COMPUTED |
| Spikes (extraction + phrasing) | ~44 | 0.14 | COMPUTED |
| Provider probes, smoke tests | ~10 | ~0.03 | INFERRED |
| Live end-to-end replays | ~290 | ~1.00 | INFERRED (the replay recorded no tokens) |
| **Subtotal to 18 Nov** | | **≈ 3.16** | key checkpoint ($4.59 − $1.43) |
| Offline judge run 1 (prompt judge-p1) | 30 | 0.106965 | ACTUAL (ledger) |
| Offline judge run 2 (prompt judge-p2) | 30 | 0.098315 | ACTUAL (ledger) |
| p5 re-run: golden v4 (26 Nov) | 36 paid + 40 cached | 0.1320 | COMPUTED (`golden_v4_model_20261126_2234.json`) |
| p5 re-run: held-out h1 (26 Nov) | 167 | 0.6125 | COMPUTED (`golden_h1_model_20261126_2242.json`) |
| p5 re-run: live replay v4 (26 Nov) | 82 paid + 8 cached | 0.3017 | COMPUTED (`replay_v4_live_20261126_2305.json`) |
| Human judge calibration (26 Nov) | 0 | 0 | local comparison, no model call |
| **Total recorded** | | **≈ 4.42** (3.37 + 1.0462) | |
| Aborted first v4 attempt (26 Nov; runner crashed on an unreported token count) | ≈ 38 | ≈ 0.14 | **ESTIMATE** ($0.0036 per call); in no result file |

The OpenRouter key's own counter is authoritative: at the 26 Nov check (after the two golden re-runs, before the replay) it showed $5.98 used and $4.02 remaining of a **$10 limit with no reset** (REPORTED, owner-run read of the key endpoint).

About $1.50 of the $3.16 went on full-set reruns after fixes that touched only a few scenarios. Decision T42 fixed this: estimates first, a refusal over $0.10 without approval, and a response cache. A full G3 v4 or h1 rerun with prompt p3 now costs $0.00 (`evals/cache/model_calls.jsonl`).

### 2.3 Tokens and cost per request

| Measure | Value | Basis |
|---|---|---|
| Held-out h1: 167 extractions | 326,797 input, 55,289 output tokens → $0.603 → **$0.0036 per extraction**; ≈ 1,957 input + 331 output tokens average | COMPUTED (`golden_h1_model_20261118_0907.json`) |
| Golden v4: 76 extractions | 148,420 input, 25,157 output → $0.274 → $0.0036 per extraction | COMPUTED |
| Typical short pilot extraction | ≈ 900 input + 80 output tokens → ≈ $0.0013 | ESTIMATE (`COSTS.md`; one local ledger row shows 900/80 and $0.0012 provider-reported) |
| Judge, prompt p2 | 64,490 input, 6,765 output for 30 → $0.0033 per judgement | ACTUAL (ledger) |
| Budget reservation per attempt | request bytes ÷ 3 + 600 output tokens, +20%, at least $0.001 (≈ $0.006 typical), then settled to OpenRouter's `usage.cost` | Design (`COSTS.md`) |

### 2.4 Retry expenditure

- At most one retry, and only for: a connection failure before sending, 429 with Retry-After ≤ 2 s, or 500/502. Never for timeouts, 504/408, 503, 4xx, malformed answers, provider mismatch or budget refusal. The judge never retries.
- An unusable answer is charged once (status `ERROR`, `malformed`) and is not retried.
- **Actual retry rate on the pilot: NOT AVAILABLE** (it is in `model_usage.attempt`). The theoretical worst case is 2× for the retryable categories only.

### 2.5 What happens when the budget is exhausted

1. Before each attempt, `reserve_model_call` (migration 0023) atomically reserves the upper-bound cost in PostgreSQL for the UTC day.
2. If the reservation would exceed `SETUHAUL_LLM_DAILY_BUDGET_USD` (**$2.00 from the DEMO-04 release**; $0.25 before), no request is sent (`budget_exhausted`, $0). The value is now an explicit, checked input of the activation run (`llm_daily_budget_usd`, 0.25–2, default 2); the stack refuses a value above $2 or below the judge's budget.
3. The orchestrator uses the rules reader. Booking, approval and outbound are unaffected. The driver is understood less well: on held-out h1, intent accuracy is 61.2% for the rules reader against 98.0% for the model.
4. Independently, the OpenRouter key limit ($10 total, **no reset**, no auto top-up) makes OpenRouter answer 402/403 once spent, which is never retried. It is a lifetime ceiling, not a monthly budget. (The owner approved $3/month on 21 Nov; the key endpoint on 26 Nov shows $10 with no reset, and the owner confirmed that the pilot and the evaluation runs use this same key, so evals and the pilot draw on one $10. `COSTS.md` records both.)
5. The online judge is limited twice (`setuhaul/agent/online_judge.py`):
   - **Configured judge budget** (`SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD`; stack output `LlmJudge`): the judge's own sub-cap (refusal reason `judge_daily_cap`). **$0.75/day from the DEMO-04 release** ($0.50 before).
   - **Shared application cap** (`SETUHAUL_LLM_DAILY_BUDGET_USD`). The judge's reservations are checked against the shared cap minus `SETUHAUL_LLM_JUDGE_AGENT_RESERVE_USD`. That reserve defaults to half the shared cap; no override was found in `infra/setuhaul_stack.py`. Refusal reason: `shared_daily_cap`.

   **Before DEMO-04** the shared cap was $0.25, so the judge could use at most $0.125 of headroom and was in practice always skipped: on 27 Nov every sampled turn logged `budget:shared_daily_cap` (pilot agent log, e.g. 11:09 UTC). **From DEMO-04** the shared cap is $2.00, the agent's reserve is $1.00, and the judge's own $0.75 is the binding judge limit (owner decision, 27 Nov). The judge now scores live turns: verified 27 Nov during the 13:30 UTC smoke: 2 sampled smoke turns `SCORED` (agent log) (Evidence Register D3a).
6. **The $10 OpenRouter key limit (no reset) becomes the first limit to bind.** At $2/day of headroom, and with $4.02 remaining at the 26 Nov check (less the $0.30 replay and pilot use since), the key can be exhausted within about two days of heavy use. When it is spent every model call fails (402/403, not retried) and the agent uses the rules reader. Raising or resetting the key limit is an owner action at OpenRouter.

## 3. Infrastructure cost (AWS, Sydney)

### 3.1 Services in the stack

From `infra/setuhaul_stack.py` and the stack outputs:
- Lambda: 8 workload functions plus setup and Cognito functions;
- API Gateway (REST);
- SQS FIFO plus DLQ;
- Step Functions (Standard);
- EventBridge: 3 one-minute rules, disabled while asleep;
- Aurora PostgreSQL Serverless v2: 0–2 ACU, auto-pause after 300 s, Data API;
- Secrets Manager; KMS; CloudWatch logs and alarms; SNS; Cognito.
- **Not present:** NAT gateway, RDS Proxy, VPC endpoints and public IPv4. WAF was removed for the low-cost pilot.

### 3.2 Idle month (ESTIMATE, `docs/release/09`)

| Item | Basis | USD / month | Class |
|---|---|---|---|
| Secrets Manager | 10 × $0.40 | 4.00 | Fixed |
| CloudWatch alarms | 14 × $0.10 | 1.40 | Fixed |
| KMS customer key | 1 × $1.00 | 1.00 | Fixed |
| SQS long polling | ~0.3–0.7 M empty receives × $0.50/M, mostly free tier | 0–0.35 | Usage-based |
| Aurora storage | 0.2–0.5 GB × $0.11 | 0.02–0.06 | Usage-based |
| Logs + CDK assets | $0.033/GB-month | < 0.05 | Usage-based |
| Aurora compute (paused), Lambda, API Gateway, Step Functions, EventBridge (disabled), Cognito | – | 0 | Variable (0 while idle) |
| **Idle total** | | **≈ 6.50–6.90** | |

### 3.3 Cost classes and avoidable spend

| Class | Items |
|---|---|
| Fixed | Secrets Manager, KMS, alarms (≈ $6.40). Plus Aurora's minimum ACU whenever the schedules run |
| Variable (by awake hours) | Aurora ACU-hours ($0.20 per ACU-hour; ≈ 0.5 ACU around the clock ≈ $73/month if never paused) |
| Usage-based | Lambda, API Gateway, SQS, Step Functions transitions, log ingestion ($0.67/GB), model tokens |
| Potentially avoidable / idle | Leaving the schedules awake (≈ $73/month of Aurora that never pauses); re-adding WAF without traffic (~$8–9/month); 10 separate secrets (one per database role, kept for least privilege) |

Idle cost control is implemented: `python -m setuhaul.ops.pilot_mode sleep` (verified asleep on 26 Nov: all three rules DISABLED). Pausing to 0 ACU was observed once (`docs/release/10_EXPRESS_AURORA.md`).

## 4. Unit economics (PROJECTION)

### 4.1 Assumptions

| # | Assumption | Why |
|---|---|---|
| A1 | A booking needs **3 model-read driver turns** (delay report, YES, slot pick) | The Locust workflow and the demo flow use 3 driver messages per booking |
| A2 | Extraction cost **$0.0013 (low) to $0.0036 (high)** per turn | Typical estimate vs measured on long-context golden runs |
| A3 | Judge on **20%** of turns at **$0.0033** | Deployed sample rate; measured judge cost |
| A4 | Non-booking traffic (status questions, escalations, abandoned conversations) is **excluded**. Add it as a multiplier once a real pilot measures it | Unknown |
| A5 | No retries counted | Retry rate not available |
| A6 | AWS from the project's estimates (§3) plus list prices; Data API request charges, LangSmith and Cognito **excluded** (not verified) | Unknown |

### 4.2 Per booking

| | Low | High |
|---|---|---|
| Extraction (3 turns) | $0.0039 | $0.0108 |
| Judge (0.6 judgements) | $0.0020 | $0.0020 |
| **LLM per booking** | **$0.0059** | **$0.0128** |
| **LLM per 1,000 bookings** | **$5.90** | **$12.80** |

Cost per *successful* booking needs the success rate. On the 23 Nov local load test, 40 requests were submitted for 56 holds, with 26 confirmed and 1 pending, so the share of flows that end in a booking is well below 1. Real-pilot data is **not available**. A conservative multiplier of 1.5–2× gives an LLM cost per *successful* booking of roughly **$0.009–0.026** (PROJECTION).

### 4.3 Monthly scenarios (PROJECTION, not demonstrated AWS capacity)

None of these volumes has been run on AWS. Today the pilot is limited by design:
- the app cap: **$2.00/day from DEMO-04** ($0.25/day before);
- the $10 OpenRouter key limit, which never resets (a lifetime ceiling, so no monthly capacity is derived from it). At $2/day it binds first (§2.5, item 6);
- the daily cap alone allows ≈ 156–339 bookings/day from DEMO-04 (ESTIMATE: $2.00 ÷ $0.0059–0.0128 LLM cost per booking including the judge, §4.2); ≈ 20–42 at the earlier $0.25.

| Bookings / month | ≈ per day | LLM / month | AWS / month (assumption) | Total / month | Per 1,000 bookings |
|---|---|---|---|---|---|
| 100 | 3 | $0.59–1.28 | $8–10 (scenario B: awake a few hours a week) | **≈ $9–11** | ≈ $86–113 |
| 10,000 | 330 | $59–128 | $85–100 (awake all day: schedules on, ≈ 0.5 ACU floor, WAF back) | **≈ $144–228** | ≈ $14–23 |
| 100,000 | 3,300 | $590–1,280 | $200–370 (1–2 ACU sustained = $146–292; Step Functions ≈ $25; Lambda + API Gateway + SQS + logs ≈ $20–50; fixed ≈ $15) | **≈ $790–1,650** | ≈ $8–17 |

The slide 14 chart plots the **upper** values per 1,000 bookings: $112.8, $22.8 and $16.5.

**Why the growth is not linear:**
- AWS has a fixed floor that dominates at low volume. Once the pilot must stay awake, it jumps to an always-on floor of about $73/month of Aurora.
- After that, AWS grows sub-linearly (ACU scales in steps).
- LLM cost is linear in turns. At 100,000 bookings/month it is about 75% of the total, so model choice and turn count become the main levers.

### 4.4 Capacity constraints and likely bottlenecks

| Constraint | Today | Breaks at |
|---|---|---|
| App LLM cap **$2.00/day** (DEMO-04, owner 27 Nov; $0.25 before) | ≈ 156–339 bookings/day (ESTIMATE: $2.00 ÷ $0.0059–0.0128 LLM cost per booking including the judge, §4.2). Method: daily cap ÷ LLM cost per booking (3 extraction turns + 0.6 judgements). The judge shares the cap, at most the headroom above the agent reserve, and its own $0.75 | above ≈ 4,700–10,000 bookings/month (ESTIMATE). In practice the $10 key limit binds long before |
| OpenRouter key limit $10, no reset | $4.02 remaining at the 26 Nov check (before the $0.30 replay) | a lifetime ceiling: when spent, model calls stop until the owner raises it; no monthly capacity is derived |
| Aurora max 2 ACU | sized for the pilot | must rise before 100,000/month; not load-tested on AWS |
| Agent turn latency 8.5–11 s on the pilot (REPORTED) | fine for 2 drivers | the 20–35-message spike in the brief has **not** been tested on AWS |
| Telegram Bot API rate limits; OpenRouter rate limits | not measured | unknown |
| LangSmith trace volume (~10 runs per turn) | small | potentially material cost at 100k/month; mitigate with trace sampling |

## 5. Cost optimisation levers

| Lever | Status |
|---|---|
| One model call per turn; templates for every reply (no phrasing model) | Implemented (OD-3) |
| Rules reader instead of a fallback model | Implemented |
| Daily budget reserved atomically in PostgreSQL; $10 non-resetting key limit; no auto top-up | Implemented |
| Judge sampling 20%, own sub-cap, no retry, cannot starve the agent | Implemented |
| Eval cache; estimate first; refusal over $0.10 without approval | Implemented (T42) |
| Static prompt first so it can be cached (prompt caching) | Designed in `prompts.py`. `cache_read` tokens were 0 in the golden runs, so **not realised** through this route |
| Skip the model for turns the rules reader handles with certainty (bare "1", "YES") | Planned. The orchestrator already parses bare numbers (`_bare_number`). Extending this to a confident-rules gate could cut model calls by a measurable share |
| Batch pricing for offline evals (`claude-haiku-4.5:batch` at half price) | Price listed in `evals/budget.py`; not used |
| Shorter context (fewer prior turns in the user block) | Planned; h1 measured ≈ 1,957 input tokens on average vs ≈ 900 typical |
| Idle management (sleep schedules, Aurora 0 ACU) | Implemented and verified asleep |
| Trace sampling for LangSmith at volume | Planned |

## 6. Performance

### 6.1 Pilot Telegram latency

| Stage | Before demo release (7 exchanges, 26 Nov, recorded in commit `ff4076d` message) | After (6 exchanges, 26 Nov, REPORTED, not committed) | Target (owner) |
|---|---|---|---|
| received → queued | p50 1.2 s | – | – |
| queued → agent reply (agent turn incl. LLM) | p50 8.3 s | 8.5–11 s | – |
| reply → outbound | p50 43.9 s, p95 58.6 s | p95 1.3 s | – |
| outbound queue → send (see note) | 2.0 s ("send") | p95 ≈ 2.2 s ("queue-to-send") | < 2 s (**missed**) |
| **end to end** | p50 53.7 s, p95 72.6 s | **p50 13.1 s, p95 15.0 s** | < 15 s (**missed narrowly**) |

- **Preliminary.** The after column is a reported six-exchange sample, not committed evidence. Save the `latency_report.py` output to make it evidence.
- **Comparability.** `scripts/latency_report.py` was added in the same commit (`ff4076d`) whose message records the before numbers. The stage names match: received→queued, queued→reply, reply→outbound, end to end received→sent. So the **end to end** and **reply → outbound** boundaries are the same timestamps before and after. The outbound-to-send row is less certain: the before "send" and after "queue-to-send" figures cannot be mapped with certainty to the script's `outbound->sending` or `sending->sent` stage. The samples also differ (7 vs 6 exchanges, different conversations, warm or cold state unknown).
- **p99 is not available.** With n = 6, p95 is effectively the worst case. These numbers indicate direction; they are not a service level, and no speed-up multiple is claimed.
- **Model vs workflow latency.** The extraction call takes about 2.2–2.4 s at p50 (golden runs). The workflow takes 13 s end to end. The gap is queueing, the agent's database round trips over the Data API (each tool is one to three transactions), Lambda cold starts and delivery.
- **Agent turn: pilot vs local.** It takes 8.5–11 s on the pilot against about 1.9 s locally (Phase G, rules reader). This has **not been broken down** yet. The hypotheses are Data API round trips, cold starts, and secrets and identity lookups on cold invocations.

### 6.2 Local load test (Phase G, 23 Nov, local only, rules reader, $0)

Setup: 5 users for 4 minutes. Result: 544 requests, 0 failures, p50 400 ms, p95 690 ms.
- Agent turn: p50 1,911 ms, p95 2,279 ms.
- Optimizer: p50 24 ms, p95 39 ms.
- `find_feasible_options`: p95 1,036 ms.
- Correctness under load: 26 bookings confirmed and 0 overlapping bookings.

Source: `docs/phase-g/PHASE_G_PERFORMANCE_REPORT.md`. **This is not an AWS result.**

### 6.3 Other measurements

- Aurora resume from 0 ACU took 14.6 s on the first Data API call, measured once. A demo started on a cold pilot pays this cost.
- Tracing overhead was p50 13 ms per turn, 1.4% (local).

### 6.4 Improvement opportunities, in priority order

1. Break the agent turn into per-stage spans (fetch context, extraction, each tool, reply), then remove avoidable Data API round trips. This is the largest share of the remaining 13 s.
2. Pre-warm before a demo: wake the pilot and send one throwaway synthetic message, so the 14.6 s resume and the cold starts are absorbed.
3. Keep the agent Lambda warm during active hours (provisioned concurrency has a fixed cost; decide by volume).
4. Measure p95/p99 over at least 50 exchanges before setting a service level.
