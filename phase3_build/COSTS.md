# Model spend ledger (OpenRouter, evaluation only)

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
| Spikes (Haiku extraction + Sonnet phrasing) | ~44 | $0.14 | computed (`spike/results_*.json`) |
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
