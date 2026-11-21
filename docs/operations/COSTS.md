# Model spend ledger (OpenRouter)

Every figure says how it is known:
- **computed**: provider-reported tokens × published list price, from a result file;
- **inferred**: the difference between credit-balance checkpoints, used where a run recorded no tokens;
- **checkpoint**: the key's own usage counter, read from the OpenRouter key endpoint.

An inferred number is never presented as measured.

## Step 3, 18 Nov 2026

The key showed **$1.43 of usage before this work started** (checkpoint). The limit is $5.00.

| Activity | Calls | Cost | Basis |
|---|---|---|---|
| G3 extraction runs (Haiku): p1, p2, p2 on v1-final, p3 on v3, p3 on v4 | 348 | $1.23 | computed (`evals/results/golden_*_model_*.json`) |
| Held-out h1 scoring (Haiku) | 167 | $0.60 | computed |
| Held-out h1 generation (Sonnet) | 65 | $0.16 | computed (`evals/golden/h1/generation.json`) |
| Spikes (Haiku extraction + Sonnet phrasing) | ~44 | $0.14 | computed (`scripts/spike/results_*.json`) |
| Provider probes, smoke tests | ~10 | ~$0.03 | inferred |
| Live end-to-end replays (3 full, plus reruns of 1–4 scenarios) | ~290 | ~$1.00 | **inferred**: the replay did not record tokens |
| **Total for this work** | | **≈ $3.16** | checkpoint: $4.59 used − $1.43 before |

About $1.5 of this was full-set reruns after fixes that touched a few scenarios. Decision T42 now prevents that.

## From here (decision T42)

- Every paid run prints an estimate first, refuses to start over **$0.10** without `--approved --max-usd X`, checks each call (retries included) against the remaining budget, and records `cost` in its result file.
- An unchanged input is served from `evals/cache/model_calls.jsonl` at **$0**. The cache was backfilled from the recorded p3 runs, so a full G3 v4 or h1 rerun with prompt p3 now costs $0.00.
- A full live replay is estimated at about $0.35 (an upper bound) and is refused unless approved.

## Price assumptions (`evals/budget.py`)

| Model | Input $/1M | Output $/1M |
|---|---|---|
| `anthropic/claude-haiku-4.5` | 1.00 | 5.00 |
| `anthropic/claude-haiku-4.5:batch` | 0.50 | 2.50 |
| `anthropic/claude-sonnet-5` | 2.00 | 10.00 |

A model without a price in this table is never called.

## Sydney pilot (decision T52, migration 0023)

**Per call.** One extraction is about 900 input and 80 output tokens: about **$0.0013** at Haiku 4.5 list prices
($1 / $5 per million), up to about $0.0035 with a long conversation context (the G3 runs measured $0.0035). The ledger
reserves a conservative upper bound before each attempt (request size ÷ 3 tokens plus 600 output tokens, +20 %, at
least $0.001) and settles it to OpenRouter's reported `usage.cost`.

**What is charged to the budget.**

| Attempt outcome | Charged |
|---|---|
| Answer received and usable | OpenRouter's reported cost (`cost_source = provider`), status `OK` |
| Answer received but unusable (wrong shape, no tool call, arguments not a JSON object) | OpenRouter's reported cost, status `ERROR`, `error_category = malformed`; tokens kept; not retried |
| Rejected by OpenRouter before inference (400, 401, 402, 403, 404, 429, 503) | $0 |
| Never sent (connection failure, budget refusal) | $0 (`not_sent`) |
| Unknown (read timeout, 500/502/504, unreadable answer) | the whole reservation (`reservation`): conservative |
| A process that crashed between reserve and settle | the reservation stays counted for that day |

**Approved limits (owner, 21 Nov 2026).** Both are configuration, not code.

| Limit | Where | Approved | Resets | Role |
|---|---|---|---|---|
| Application daily budget | `SETUHAUL_LLM_DAILY_BUDGET_USD` (environment of the worker) | **$0.25/day** | 00:00 UTC | Authoritative for the application: reserved atomically in PostgreSQL before every attempt |
| OpenRouter key credit limit | OpenRouter dashboard → Keys → the pilot key → credit limit | **$3.00/month** | monthly (OpenRouter's key limit reset) | Independent external safeguard, also covers anything the ledger cannot see (a crashed process, a mis-set cap) |
| Automatic top-ups | OpenRouter dashboard → Credits | **disabled** wherever available | — | No silent spend beyond the purchased credit |
| OpenRouter privacy (recommended) | OpenRouter dashboard → Settings → Privacy | ZDR-only routing for Anthropic models, safety-retention endpoints off | — | Second layer behind the per-request `zdr: true` |

**What $0.25/day allows.** A typical extraction reserves about $0.006 (a 6 KB request plus 600 output tokens, +20 %),
so up to about 41 attempts can be in flight at once, and actual spend is about $0.0013–0.0035 per extraction: roughly
70–190 model extractions a day. When either limit is reached the agent uses the rules reader: the application budget
refuses before sending (`budget_exhausted`); the OpenRouter limit makes OpenRouter answer 402 or 403, which is never
retried. Booking and approval are unaffected.

The dashboard settings are the owner's to apply; Claude changes no dashboard setting, buys no credit and makes no paid
call without explicit approval.

**How to inspect usage** (as `setuhaul_system`, read-only):

```sql
SELECT day, spent_usd, reserved_usd FROM setuhaul.model_budget_day ORDER BY day DESC LIMIT 7;
```

```sql
SELECT status, error_category, count(*), sum(cost_usd), avg(latency_ms)::int
  FROM setuhaul.model_usage WHERE day = (now() AT TIME ZONE 'UTC')::date GROUP BY 1, 2 ORDER BY 3 DESC;
```

The OpenRouter dashboard (Activity) shows the same calls by generation id (`model_usage.generation_id`).

**Not free.** Neither OpenRouter nor Bedrock inference is free for this account; no free-tier credit is assumed.
