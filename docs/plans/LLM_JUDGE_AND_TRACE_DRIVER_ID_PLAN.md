# Plan: LLM-as-judge, and a minimal driver identifier in traces

Status: **draft for owner review. Nothing is built yet.** No paid model call happens until you approve a budget (part A, step A5).

## Where things stand today

**LLM judge.** [`evals/langsmith/llm_judge.py`](../../evals/langsmith/llm_judge.py) is a placeholder.
- It defines the only keys a judge may score: `quality:clarity`, `quality:tone` and `quality:language_match`.
- It also lists what a judge may never decide: authorisation, booking, approval, constraints, idempotency, expiry, fabrication, tools and extracted values.
- `judge()` always raises `JudgeDisabled`.
- All correctness scoring is done by the deterministic evaluators (G.1: 111/111; G.2 smoke: 10).

**Traces.** Every span passes through [`redaction.py`](../../setuhaul/observability/redaction.py) and the export guard. The root `agent_turn` run carries:
- `turn_ref`, `correlation_id`;
- `conversation_ref`, which is a sha256 of the thread id (not reversible, and not searchable by a person);
- `provider`, `model`, `facility_id`;
- path, tools, `fallback_reason`.

Nothing in it says **which driver** a turn belonged to. To debug "what happened to DRV-JAI-0042's turn at 14:05" you have to go to the database, find the thread, and work out its hash.

`agent_turn_metrics` (the console's Ops view) also has no driver column. It was built "without any driver data" by design.

---

## Part B (first, $0): a minimal driver identifier in traces

### B1. What to add and what never to add

| Add to the root `agent_turn` metadata | Never in a trace (unchanged) |
|---|---|
| `driver_id`: the internal id, such as `DRV-JAI-0042` | phone, name, e-mail |
| `facility_id` (already set at the end of the turn; set it at the start too) | Telegram chat id, username, link codes |
| `channel`: `telegram`, `whatsapp` or `test` | raw coordinates (not even the geohash) |
| `shipment_id` of the turn's shipment, when known *(your decision, B6)* | message text (still behind `SETUHAUL_TRACE_CONTENT`, synthetic only) |

`driver_id` is a pseudonym: on its own it reveals nothing. To link it to a person you need database access, and staff already see it on the Dispatch tab. Tracing is already refused unless `SETUHAUL_DATA_CLASS=synthetic`, so today it can only ever carry synthetic ids.

### B2. Code changes

**1. `orchestrator.py`.** After `get_driver_context` returns, call `root.set(driver_id=..., facility_id=..., channel=...)`. This is one `tracing.safe(...)` call, so it never changes behaviour. Child spans don't repeat the id, because LangSmith filters on the trace's root metadata.

**2. `redaction.py`.** Add a format gate for identifier keys (`driver_id`, `shipment_id`, `facility_id`):
- the value must match `^[A-Z0-9][A-Z0-9_-]{2,39}$`;
- otherwise it becomes `[INVALID_ID]`.

This way a phone number or a name that ends up in that field by mistake is still never exported. The existing phone and e-mail scrub and the export guard stay as they are.

**3. Migration `0041`.** Add `agent_turn_metrics.driver_id text NULL` with the same format CHECK. It is forward-only and additive. Grants and RLS are unchanged: the console role can already read this table, and it can already see driver ids elsewhere.

**4. `record_turn_metrics`.** Write `driver_id`.

**5. Console, Ops view (recent turns).** Add a "Driver" column and a driver filter. A row's `turn_ref` still links to the LangSmith trace, where it searches as `metadata.driver_id = "DRV-…"`.

**6. Structured CloudWatch turn line.** Add `driver_id`, so one id finds the turn in all three places: the console, LangSmith and CloudWatch.

### B3. Tests ($0)

- **The id appears:** a traced turn's root run has `driver_id`, `facility_id` and `channel`.
- **Nothing else appears:** the exported batch contains no phone, name, chat id or coordinates. The test uses the existing leak fixtures plus a Telegram-linked driver.
- **Format gate:** a phone, `+999…`, an e-mail or free text placed in `driver_id` exports as `[INVALID_ID]`.
- **Tracing off:** no change at all; the no-op tracer stays a no-op.
- **Migration 0041:** applies on a fresh database and on an existing one; the CHECK rejects a phone-shaped value.
- **Pinned tests:** the privilege and health tests are updated only where a column is added.

### B4. Later, if real (non-synthetic) data is ever traced (not built now)

Replace `driver_id` with a keyed pseudonym: `HMAC(secret, driver_id)[:12]`, with the secret kept in Secrets Manager. The console would translate it back for staff. This is noted as the design for that day. It isn't needed while tracing is synthetic-only.

### B5. Effort and order

About half a day, including tests. It ships as its own commit, and nothing in it depends on part A.

### B6. Your decisions for part B

1. **The id:** the raw internal `driver_id` (recommended, since tracing is synthetic-only), or the HMAC pseudonym now.
2. **`shipment_id`:** include it as well (recommended: it makes "which load" one click away), or `driver_id` only.

---

## Part A: LLM-as-judge (subjective quality only)

### A1. Scope (unchanged from G.1)

- **What it scores:** `quality:clarity`, `quality:tone` and `quality:language_match`, each 1–5, plus a rationale of at most 200 characters.
- **What it does with the score:** records it as feedback. A score **never** changes an example's PASS/FAIL, never gates a release, and never runs on the driver's reply path.
- **What it refuses:** asking it about any `NEVER_JUDGED` key raises an error.

### A2. Where it runs: offline first

The judge needs the reply text. Pilot traces don't carry that text, because content export is off, and that stays so. The judge therefore runs **in the eval harness over the synthetic datasets**, where replies exist locally:
- the G.1 set, 111 examples;
- the G.2 smoke set, 10;
- the Phase G scenarios, 67.

**Online judging of LangSmith traces is out of scope for this cycle.** It would need `SETUHAUL_TRACE_CONTENT=on` in an environment and a sampling cap (≤ 10%, from the G.1 design). That is a separate decision, made later.

### A3. Design

1. **`llm_judge.judge(example, reply, client)`.** This replaces the placeholder.
   - **Prompt.** A fixed rubric prompt, versioned as `JUDGE_PROMPT_VERSION`. It contains:
     - the scenario's synthetic driver message;
     - the agent's reply;
     - the expected language (en, hi, or hi-Latn);
     - a one-line scenario description.
   - **Kept out of the prompt:** tool payloads, database state, phone numbers, ids and other drivers. Both inputs go through `redaction.scrub` first.
   - **Output.** Strict JSON with three integer scores and a rationale, checked against a schema. If the output is malformed, the judge retries once, then records `score=None` with the reason "judge_unparseable". It never guesses a score.
   - **Temperature** 0.
2. **Model route.** The judge uses the existing metered OpenRouter client, with the same controls as the agent:
   - the pinned verified-ZDR provider;
   - `data_collection: deny`;
   - the daily usage ledger, and a reservation before every call.

   It adds a new cap, `SETUHAUL_LLM_JUDGE_BUDGET_USD`, per run. When the cap is reached, the run stops and reports what it judged. Nothing is retried past the budget.
3. **Gate.** The existing `judge_enabled` rule stays. It needs all of:
   - `SETUHAUL_LLM_JUDGE=on`;
   - `SETUHAUL_ENV` set to dev or eval;
   - a budget.

   Add one more requirement: `SETUHAUL_DATA_CLASS=synthetic`. The pilot and staging environments are always refused.
4. **Runner, `python -m evals.langsmith.judge_run`.**
   - **`--dry-run`** (default, $0). Builds every prompt and prints the count and a token estimate. Nothing is sent.
   - **`--max-usd X`.** The paid run. You start it yourself, with the key in your own shell.
   - **Output.** Results go to `evals/results/judge_<ts>.json`, which holds only the scores, the rationales and the prompt version, and no keys. When LangSmith is configured, the scores are also posted as `quality:*` feedback on the experiment's runs.
5. **Calibration before anyone trusts it.**
   - You hand-score 20 replies on the same 1–5 rubric, in a sheet like `evals/b_ai_3/review_sheet.md`.
   - The report gives agreement as the share within ±1, per key.
   - **Proposed bar:** ≥ 80% within ±1 on all three keys. If it scores below that, the judge stays informational and the prompt is revised.
6. **Self-grading bias.** The judge would be a model of the same family that wrote the replies. It only rates tone and clarity and decides nothing, which limits the risk, and calibration (step 5) measures it. Using a different judge model needs a new ZDR route verified first: your decision (A6).

### A4. Tests ($0, with a fake client)

- **Off by default:** the judge is off by default and in pilot/staging, and is refused without a budget or synthetic data.
- **`NEVER_JUDGED` keys** are refused.
- **Malformed output:** malformed JSON gives one retry, then `None`, and never a made-up score.
- **Budget:** exhausting the budget stops the run cleanly.
- **Prompt contents:** the prompt never contains a phone number, e-mail, id or tool payload. The test runs `find_leaks` over every prompt built from the datasets.
- **No effect on correctness:** judge scores never touch a deterministic evaluator's result.
- **Dry run:** the dry run makes no network call.

### A5. The paid run (only with your approval)

- **Scope:** the G.2 smoke set (10) plus 20 calibration replies.
- **Calls:** about 30–35, at most twice that if every call needs its one retry.
- **Cost:** the dry run prints the token estimate. **Proposed cap: USD 1 per run**, enforced by the ledger.
- **After it:** the full 111-example set only after calibration passes.

### A6. Your decisions for part A

1. **Budget:** approve the per-run cap (proposed USD 1) and the daily cap the judge may share with the agent's ledger.
2. **Judge model:** the same verified ZDR route as the agent (recommended, available now), or verify a second route first.
3. **Calibration bar:** ≥ 80% within ±1 is the proposal.
4. **Online judging of pilot traces:** confirm it stays out of scope for this cycle (recommended).

### A7. Effort

- The code and $0 tests: about one day.
- The paid calibration run: a few minutes, started by you.

---

## Order and boundaries

1. **Part B:** commit it, then run the focused tests and the tracing, privacy and migration suites.
2. **Part A:** the judge code, its $0 tests, then the dry run.
3. **Stop:** I report the dry-run estimate, and you decide on the paid run.

Boundaries for the whole plan:
- no AWS changes or deployment;
- no paid call without your go-ahead;
- no change to booking, authorisation or kill-switch paths;
- migration 0041 is additive.

It all goes on a new branch from `main`, and a PR is opened only when you ask.
