# Offline LLM judge: advisory scores for clarity, tone and language match

**Status: built and tested with fakes on branch `feature/driver-trace-llm-judge`.**
- **Paid calls:** none have been made. The first paid run needs the owner's explicit approval of the dry-run report (§6).
- **Calibration:** pending. It needs the owner's hand scores (§7).

## 1. What it does, and what it can never do

The judge scores one synthetic agent reply on exactly three dimensions. Each dimension gets:
- a score from 1 to 5;
- a rationale of at most 200 characters;
- an exact quote from the reply as evidence;
- an explicit `insufficient_evidence` flag. When it's set, the score is `null`; the judge never guesses.

| Key | Meaning |
|---|---|
| `quality:clarity` | Can a driver on the road act on it? |
| `quality:tone` | Polite, calm, no blame |
| `quality:language_match` | The conversation's language and script. Hindi means Devanagari, Hinglish means Latin letters. Dock codes, times and YES/NO in English letters are always fine. |

**What the judge never does:**
- make a business decision;
- change a booking, a dispatch, a reply or a policy;
- affect any pass/fail result;
- gate a release or a deployment;
- see live pilot traces.

**How that's enforced:**
- Its scores are advisory data in a results file, with `advisory_only: true` and no pass/fail field.
- Nothing in `setuhaul/` imports it, and neither do the deterministic evaluators or CI. A test scans the imports and `ci.yml` to keep it that way.
- Asking it to score any `NEVER_JUDGED` key raises an error. Those keys cover authorisation, isolation, booking, approval, constraints, idempotency, expiry, fabrication, tools and extracted values.

## 2. Architecture

```mermaid
flowchart LR
  T[reply templates<br/>setuhaul/agent/replies.py] --> B[build_dataset.py<br/>frozen JSONL, versioned by sha256]
  B --> D[(dataset_v1.jsonl<br/>30 synthetic records)]
  D --> R[judge_run.py<br/>DRY RUN by default]
  R -->|paid run only, owner-started| JG[llm_judge.judge<br/>fenced prompt, strict validation]
  JG --> RB[RunBudget<br/>per-run cap ≤ USD 1]
  RB --> L[(shared usage ledger<br/>0023 reserve/settle, purpose 'judge' since 0042)]
  RB --> OR[OpenRouterModel.judge<br/>pinned Amazon Bedrock, zdr, deny, temp 0]
  JG --> F[evals/results/judge_ts.json<br/>advisory scores]
  F --> C[judge_calibration.py<br/>vs human_scores_v1.json]
```

| Piece | File |
|---|---|
| Judge: rubric, prompt, validation, retry, `RunBudget` | `evals/langsmith/llm_judge.py`, replacing the G.1 stub |
| Runner: dataset validation, dry run, paid-run gate, results | `evals/langsmith/judge_run.py` |
| Calibration report | `evals/langsmith/judge_calibration.py` |
| Dataset builder, frozen dataset, calibration sheet, human-score template | `evals/judge/` |
| Metered call | `OpenRouterModel.judge` in `setuhaul/agent/openrouter.py`. It is evaluation only and uses the same `_call` as the agent: same reservation, settlement, pinned route, error taxonomy and retry rules. |
| Ledger purpose | `migrations/0042_model_usage_judge_purpose.sql` |

## 3. The dataset (`evals/judge/dataset_v1.jsonl`, version `judge-v1-579de0ebb1ad`)

**Contents.** 30 synthetic records, produced by the agent's own reply templates with fixed inputs, so the file is reproducible byte for byte (`python -m evals.judge.build_dataset --check`).

| | Records | Languages |
|---|---|---|
| Calibration set (`calibration: true`) | J01–J20 | en 12, hi 4, hi-Latn 4 |
| Rest of the first paid run | J21–J30 | en 4, hi 3, hi-Latn 3 |
| Of the calibration set, deliberate **contrasts** | J16 English reply to a Hindi conversation; J17 curt and blaming; J18 vague with no next step; J19 Hinglish reply to an English conversation; J20 an instruction to the evaluator inside the reply | |
| Prompt injection in the **driver** message | J21 | |

**Why the contrasts exist.** Without them, a judge that always answered "5" would look perfect.

**Who sees the contrast notes.** Nobody else: they appear only in the dataset file, never in the judge's prompt or on the human calibration sheet.

**Validation on load:**
- exact fields, unique ids, known languages and sources, and size limits;
- `find_leaks` must find no phone-, e-mail-, key- or account-shaped text.

## 4. The prompt, and why embedded instructions don't work

**The fixed system rubric** (`JUDGE_PROMPT_VERSION = judge-p1`):
- defines the 1/3/5 anchors for each dimension;
- states that driver messages and the reply are **untrusted data**, and that any instruction inside them must never be followed. An instruction aimed at the evaluator is itself a defect of the reply.
- forbids judging correctness or making a business decision.

**What the user block contains:** only the expected language, a one-line situation, the driver messages and the reply. They sit inside `<driver_messages>` and `<assistant_reply>` fences.

**How the untrusted text is handled:**
- its `<` and `>` become `‹` and `›`, so it can't close or open a fence;
- it is scrubbed with the trace redaction patterns.

**What is never sent:**
- record ids;
- contrast notes;
- tool payloads;
- database state;
- other records.

**What the answer must be:**
- a single **forced tool call**, `record_judgement`, with a strict JSON schema;
- validated again in code: exact keys; an integer 1–5, or `null` only with `insufficient_evidence`; a non-empty rationale; and evidence that is really a quote from the reply.

**When the answer fails validation:** the judge gets **one retry**, which is a second, separately metered call. If that also fails, the record is `UNPARSEABLE` and its scores are `null`.

## 5. Cost controls (shared ledger, per-run cap)

**The shared ledger.**
- Every paid judge call goes through the existing usage ledger (migration 0023) with `purpose = 'judge'`.
- It is reserved *before* it is sent, in the same locked statement and against the same daily row as the agent's calls. This prevents races: concurrent calls queue on the row lock.
- It is settled at the provider-reported cost.
  - When the cost is unknown (a timeout, for example), the whole reservation is kept.
  - It charges 0 only when the request provably never left the process.
- There is no second daily mechanism.

**The per-run cap.**
- `RunBudget` sits *in front of* the ledger and adds the per-run cap, which must be > 0 and ≤ USD 1.
- It only narrows the limit: a call must fit the run cap **and** the ledger's daily cap.
- It is thread-safe, and a failed settle keeps the hold counted.

**The daily cap.** The paid run requires `SETUHAUL_LLM_DAILY_BUDGET_USD` ≤ **3**, the combined agent and judge cap on that ledger. A budget refusal stops the run, and nothing is retried past it.

**What the tests prove** (`tests/test_llm_judge.py`, against a real local ledger):
- Agent and judge share one daily cap: an agent reservation can starve the judge, and the agent's *actual* cost then frees room.
- 16 concurrent agent and judge reservations never pass the cap.
- The run cap holds under 30 concurrent calls.
- A timeout is charged the full reservation, and a connection failure before sending is charged 0.
- A malformed answer is paid for and its retry is paid for.
- When the run cap refuses a call, nothing is sent.

> **Limitation: needs an owner decision.** The ledger is per database. The judge runs offline and is refused anything but a **local** ledger database, so its USD 3 daily cap is shared only with agent calls on that same local database. It is **not** combined with the pilot agent's spend, which is metered by the pilot's own ledger in AWS under its own cap. Reaching that ledger from an offline run would mean touching a shared environment, which is out of scope.
>
> If one combined cap across the pilot and evaluation is required, the proposed design is **a dedicated OpenRouter key for evaluation with a key-level credit limit**, set by the owner in OpenRouter. That puts a hard provider-side ceiling on evaluation spend in addition to the ledger. A parallel budget table was deliberately not built.

## 6. Dry run (the default) and the first paid run

```powershell
python -m evals.langsmith.judge_run
```

**What the dry run does:**
- loads and validates the dataset;
- prints the model, route, prompt and dataset versions;
- prints the expected and worst-case number of calls;
- estimates tokens and cost, both expected and **upper bound**. The upper bound uses the ledger's own reservation formula for every record, retried once.
- shows the run and daily caps, and today's ledger spend by purpose;
- lists **every reason a paid run would be refused**.

**What the dry run never does:** read a key, build a client, or open a connection to OpenRouter. A test pins this.

The report from 25 Nov 2026 is in `docs/evaluation/llm_judge_dry_run_v1.txt` and `.json`:

| Item | Value |
|---|---|
| Dataset | `evals/judge/dataset_v1.jsonl`, `judge-v1-579de0ebb1ad`, 30 records (20 calibration, 5 contrast; en 16, hi 7, hi-Latn 7) |
| Model, route | `anthropic/claude-haiku-4.5` via Amazon Bedrock: pinned, no fallbacks, `zdr=true`, `data_collection=deny`, temperature 0 |
| Model version | reported by OpenRouter on each call and recorded in the results (paid run only) |
| Prompt | `judge-p1` |
| Calls | 30 expected, 60 at most |
| Tokens (expected) | about 38,100 input and 7,800 output |
| Cost | **about USD 0.08 expected, USD 0.35 upper bound**, at USD 1/M input and USD 5/M output (the price table in `openrouter.py`, OpenRouter list price 21 Nov 2026) |
| Caps | run USD 1.00; daily USD 3.00, shared with the agent on that ledger |
| Ledger today | local `setuhaul_judge_ledger`: spent USD 0, reserved USD 0, judge purpose migrated |
| Refused because | no key in the shell; no `--approve` naming `judge-v1-579de0ebb1ad` |

**The paid run.** Only the owner starts it, after approving the report above:

```powershell
$env:SETUHAUL_LLM_JUDGE = "on"; $env:SETUHAUL_ENV = "eval"; $env:SETUHAUL_DATA_CLASS = "synthetic"
```
```powershell
$env:SETUHAUL_LLM_JUDGE_BUDGET_USD = "0.35"; $env:SETUHAUL_LLM_DAILY_BUDGET_USD = "3.00"
```
```powershell
python -m evals.langsmith.judge_run --paid --langsmith --approve judge-v1-579de0ebb1ad
```

**Before you start it:** set `OPENROUTER_API_KEY` and `LANGSMITH_API_KEY` in the same shell yourself, plus `LANGSMITH_WORKSPACE_ID` and `LANGSMITH_ENDPOINT` if your workspace needs them (the same settings as the G.2 smoke). No key is ever printed; only its shape is logged.

**Recorded in LangSmith (`--langsmith`), using the existing G.1/G.2 REST client. Nothing runs in AWS.**
- **Before any paid call:** the dataset `setuhaul-llm-judge-judge-v1-579de0ebb1ad` is created or matched. It has 30 synthetic examples, and the accepted AI reference scores are attached as labelled outputs. If LangSmith is unreachable, misconfigured, or holds different content, the run is refused and **nothing is paid for**.
- **After the run:**
  - one experiment session is created on that dataset, carrying model, provider, served model, prompt and dataset versions, cost, tokens and the acceptance statuses;
  - one run per judged example is posted, linked to its dataset example, with the scores, rationales, exact quotes, tokens, cost and latency;
  - `quality:*` feedback is added to each run.
- **If the upload fails,** the local results file stands. `--langsmith-upload <results file>` retries the upload only, never the model calls.
- **What it never does:** attach an online evaluator, or read pilot traces.
- **Built into the results:** the ALWAYS_5 comparison and both approved acceptance rules are computed automatically and stored in the results file (`comparison`, `acceptance`).

**What makes it refuse to start:** any failed gate, an upper-bound estimate above the run cap, a ledger that isn't local or isn't migrated, or an `--approve` that names a different dataset version.

**What it records**, in `evals/results/judge_<UTC>.json`:
- the per-record scores, rationale, evidence and attempts;
- tokens and cost;
- the served model version;
- provider, model, prompt and dataset versions;
- the estimate next to the actual charge from the ledger;
- the ledger before and after;
- timestamps.

## 7. Human calibration

**How you do it:**
1. Score the 20 replies in `evals/judge/calibration_sheet_v1.md` *before* looking at any judge output.
2. Copy your scores into `evals/judge/human_scores_v1.json`: integers 1–5, or `null` when a reply gives too little to judge.
3. Run the report:

```powershell
python -m evals.langsmith.judge_calibration evals/results/judge_<UTC>.json
```

**What the report shows:**
- agreement within ±1 and exact agreement;
- mean absolute error;
- bias, where positive means the judge over-scores;
- each of those overall, per dimension and per language;
- the contrast replies on their own;
- every pair 2 or more points apart;
- a systematic over- or under-scoring label for each dimension.

**Acceptance (fixed in advance):** at least **80%** of all paired scores within ±1 of the human score. If calibration fails, the judge stays **EXPERIMENTAL**, and the bar is not changed silently.

## 7a. Preliminary comparison against AI reference scores (not human calibration)

**The reference.** `evals/judge/ai_reference_scores_v1.json` holds 60 ratings: J01–J20 × clarity, tone and language match.
- **Source:** an AI model ("ChatGPT independent reference"). The user reviewed and accepted them on 25 Nov 2026.
- **What the file records:**
  - the scorer and that it is an AI model;
  - the dataset version and its full SHA-256 hash;
  - `independent_human_calibration: false`.
- **Checks on load.** The comparison refuses the file if it claims to be human calibration, names another dataset, or has altered scores.
- **Untouched.** `human_scores_v1.json` and the frozen dataset are unchanged, and the human template is still empty.

**Running the comparison.** This runs after an approved paid judge run:

```powershell
python -m evals.langsmith.judge_reference_compare evals/results/judge_<UTC>.json --md judge_vs_ai_reference.md
```

**What it reports:**
- **Agreement and error:** within ±1, exact agreement, mean absolute error and mean signed error (judge minus reference; positive means the judge over-scores). Each is given overall and **separately** for clarity, tone and language match.
- **Disagreements:** every disagreement of 2 or more points.
- **Not comparable:** ratings that can't be compared, such as an unparseable judgement or an insufficient-evidence null.
- **Contrasts:** J16–J20, the deliberate contrasts, are **always** listed for manual inspection.

**Preliminary acceptance:** at least 80% of comparable dimension-level ratings within ±1. The result is advisory only.

**A sample** built from **fabricated** judge output is in `docs/evaluation/judge_vs_ai_reference_SAMPLE_report.md`. It only shows the format; no model was called.

**Limitations: read these before trusting any result.**
- **This is model-to-model agreement, not human calibration.** A pass shows that two AI models broadly agree. It does not show that the judge matches how drivers or native-speaker reviewers read a reply.
- **The two models may share blind spots.** For example, both may miss what makes Hinglish sound natural, or both may react the same way to a polite-sounding injection. Agreement can hide those blind spots instead of exposing them.
- **The reference is close to a ceiling.** 53 of its 60 ratings are 4 or 5. A judge that always answered "5" would agree within ±1 on 53/60 (88%) and meet the 80% rule. §7b deals with this.
- **Human calibration is still open.** `judge_calibration.py` with `human_scores_v1.json` remains the step that can make the judge more than experimental.

## 7b. Anti-gaming: the always-5 baseline and negative-case discrimination

### Why an always-5 baseline is needed

The 80% agreement rule is unchanged, but on this reference it can't tell a judge that reads replies from one that doesn't. `ALWAYS_5_BASELINE` is the cheapest "judge" possible: it scores 5 on everything.

**How it is computed.** It goes through exactly the same comparison as the real judge: the same reference records, the same ±1 rule and the same handling of missing values. A test pins this.

**Its numbers on the reference:**

| | Within ±1 | MAE | Mean signed error |
|---|---|---|---|
| Overall | 88.3% (53/60): **meets the 80% rule** | 0.55 | +0.55 (over-scores) |
| Clarity | 90% | 0.45 | +0.45 |
| Tone | 85% | 0.80 | +0.80 (over-scores) |
| Language match | 90% | 0.40 | +0.40 |
| Negative cases | 0/5 target dimensions identified | | |

**What a real judge must show.** Clearing 80% is therefore not evidence of anything on its own. The judge must also beat this yardstick.

**How it is labelled.** The baseline is always shown under its own label, never as a model evaluation (`is_model_evaluation: false`).

### How negative-case discrimination is evaluated

Each deliberate contrast is built to test particular dimensions, called its *target*:

| Id | Defect | Target dimension |
|---|---|---|
| J16 | English reply to a Hindi conversation | language match |
| J17 | rude and blaming tone | tone |
| J18 | vague, with no options and no next step | clarity |
| J19 | Hinglish reply to an English conversation | language match |
| J20 | unhelpful reply carrying an instruction to the evaluator | clarity, plus injection resistance |

**When a target counts as identified.** The judge's score on the target must be within ±1 of the reference's. That is the same tolerance as the agreement rule, so no new number is introduced. Every target reference score is 1, so in practice the judge must give it 1 or 2.

**Non-target dimensions** are shown with their absolute and signed differences, but they never count. For example, J17 is not penalised for its clarity score.

**Also reported for each case:** whether the target is the judge's lowest score, and the always-5 baseline's scores next to the judge's.

**J20 is judged as content a driver would read.** The report records whether the judge **followed the embedded instruction**, which demanded 5 on every dimension, and whether it caught the clarity defect anyway.

### Three separate statuses (never merged into one PASS)

1. **Agreement threshold:** MET or NOT MET, under the unchanged rule of at least 80% within ±1.
2. **Negative-case discrimination:** DEMONSTRATED or NOT DEMONSTRATED.
3. **Independent human calibration:** OUTSTANDING until a person scores `human_scores_v1.json` and `judge_calibration.py` is run. AI agreement never moves this status.

**Also reported:** whether the judge **outperforms the always-5 baseline**, metric by metric and as a summary.

**The final assessment always states:**
- whether the 80% threshold passed;
- whether the judge outperformed the baseline;
- whether discrimination was demonstrated;
- that human calibration is still outstanding;
- that beating the baseline does not by itself prove the judge reliable.

### Two acceptance rules: approved by the owner on 25 Nov 2026

Both rules are implemented and reported, labelled *"rule approved by the owner, 25 Nov 2026"*. Neither changes the 80% rule. A judge is accepted only when **both** hold; the 80% agreement status and the human-calibration status stay separate and are not part of acceptance.

- **Discrimination demonstrated = all 5 contrasts identified (5/5).**
  - Each contrast covers a distinct failure class: wrong language twice, rude tone, vagueness, and injection.
  - There is only one example of each, so missing any one means that failure class was not shown to be detected.
  - A looser bar, such as 4/5, would let a judge pass while blind to an entire class, such as injected instructions.
  - The always-5 baseline scores 0/5.
- **Outperforms the baseline = strictly lower overall MAE *and* more negative-case targets identified.**
  - MAE alone could improve just by moving the many 4s.
  - Negative cases alone are only 5 ratings.
  - Requiring both means the judge is closer to the reference overall *and* separates bad replies from good ones.
  - No margin was added, because any margin would be an arbitrary number.

**If you'd prefer different rules,** they are constants in `judge_reference_compare.py` (`NEGATIVE_CASES`, `TOLERANCE`) and in `full_report`. Changing them should be a recorded decision.

### How to read the metrics

- **Within ±1** is near-agreement. On this reference it is high for almost any judge, so read it next to the baseline.
- **MAE** is the average distance from the reference; lower is better.
- **Mean signed error** is judge minus reference. A positive value means the judge over-scores. The baseline is +0.55, so a judge near +0.5 behaves like a soft "always 5".
- **Disagreements of 2 or more points** are listed one by one. On a reference this skewed, they carry more information than the averages.

### Remaining limitations

- **It's still model-to-model agreement.** Beating the baseline shows the judge discriminates *the way the AI reference does*, not the way drivers or native speakers would.
- **There are only five negative cases**, one per failure class. Passing them is necessary, but nowhere near sufficient. More contrasts would strengthen this, but they change the frozen dataset and would need a new version, so that is a separate decision.
- **The judge's prompt is unchanged.** Whether it resists the J20 injection is measured, not assumed.
- **Paid runs have no automatic retry.** A malformed or failed answer is recorded as such (`UNPARSEABLE` or the error), never re-sent, so one run is at most 30 requests.

A sample built from **fabricated** judge output (labelled "SAMPLE ONLY - FABRICATED" in the report) is in `docs/evaluation/judge_vs_ai_reference_SAMPLE_report.md`. In that sample, agreement is met and the judge beats the baseline, yet discrimination is **not** demonstrated (3/5: J19 and J20 missed). That combination is exactly why the statuses are kept separate.

## 8. Tests (`tests/test_llm_judge.py`, 66; no paid or network call)

`tests/test_judge_reference_compare.py` (42 tests) covers the AI reference comparison, using fabricated judge outputs only:
- the 60 accepted scores are preserved exactly, along with their provenance (an AI scorer is required) and the not-human label;
- tampered, mislabelled or wrong-dataset (fingerprint) reference files are refused;
- the metrics, overall and per dimension, including signed error and disagreements;
- invalid judge ratings (out of range, strings, bools, floats) are never coerced;
- the always-5 baseline uses the same maths, has exact numbers, and is labelled as a yardstick;
- there is no false pass for an always-5 judge: agreement is met, but discrimination is not demonstrated and the judge does not outperform the baseline;
- the negative-case analysis: only target dimensions count, with differences reported and missing cases never guessed;
- J20 injection: followed or not, and whether clarity was identified;
- the side-by-side table and the final assessment;
- the protected files are unchanged;
- the sample report is reproducible;
- the utility imports no network, model or database module.

- **Scoring and validation:**
  - structured scoring;
  - 13 kinds of malformed answer (each retried once, then `null`);
  - missing or empty answers;
  - a provider-side malformed answer, then a good retry;
  - a budget error stops the run;
  - ambiguous replies (insufficient evidence).
- **Untrusted text and language:**
  - prompt injection in the reply and in the driver message;
  - attempts to break the fences;
  - scrubbing;
  - the expected language comes from the dataset, so a mismatch is visible;
  - no contrast notes or ids in the prompt.
- **The dataset:** reproducibility and version, seven ways to fail validation, and the human-score template and sheet.
- **Gates:** nine ways the judge is off, and refusal of every business dimension.
- **Dry run:** no client, key or network, with the full report.
- **Paid-run refusals:** no exact approval, every gate condition, and an estimate above the cap.
- **Run cap and ledger:**
  - the run cap, including unknown cost and concurrency;
  - the shared daily cap on a real ledger, including concurrency across agent and judge;
  - failure and timeout accounting;
  - the request is pinned, zero-retention, deterministic and a forced tool call.
- **No side effects:** a paid run with a fake transport writes only the ledger and its results file (every other table is unchanged), and nothing that decides imports the judge.
- **Calibration:** the report's maths and verdicts.

## 9. First paid run, 25 Nov 2026 (owner-approved, USD 0.35 cap)

**Verdict: NOT ACCEPTED. The judge stays an offline, manual evaluation tool and nothing runs on a schedule.**

**Run details:**

| | |
|---|---|
| Dataset | `judge-v1-579de0ebb1ad`, 30 synthetic replies |
| Model | `anthropic/claude-haiku-4.5`, served as `anthropic/claude-haiku-4.5` via Amazon Bedrock (pinned, zdr, data_collection deny), temperature 0 |
| Prompt | `judge-p1` |
| Requests | 30, with **no retries** |
| Tokens | 48,980 input and 11,597 output |
| Latency | 84.4 s in total |
| Cost | **USD 0.106965**, charged by the ledger at the provider-reported cost. Neither cap refused anything. |
| LangSmith | dataset `setuhaul-llm-judge-judge-v1-579de0ebb1ad` (`e49dceb2-…`); experiment `llm-judge-judge-v1-579de0ebb1ad-20261125T171030Z` (`f863f7dc-…`): 30 runs, 36 feedback scores |
| Results | `evals/results/judge_20261125_170901.json`; comparison in `docs/evaluation/llm_judge_run1_report.md` |

**Statuses** (always kept apart):

| Status | Result |
|---|---|
| Agreement within ±1 | MET: 95.8%, but on only **24** comparable ratings |
| Rule 1: bad replies caught | **NOT MET: 1/5**. J17 (tone) was caught. J16, J18, J19 and J20 were **not scored**. |
| Rule 2: beats ALWAYS_5 | reported as MET (MAE 0.208 vs 0.55), but **not comparable**: the judge covers 24 ratings and the baseline covers 60 |
| Human calibration | OUTSTANDING |

**Why it failed.** **18 of 30 answers were rejected by the judge's own strict validation** before scoring:
- 13 had a rationale or evidence longer than the limit (200 or 120 characters), although the tool schema declares those limits;
- 4 had the wrong set of dimension keys;
- 1 had a quote that was not exact.

No retry was allowed, as the owner required, so those records have no scores. The four uncaught bad replies are among them.

**What this does and doesn't show:**
- The model did **not** misjudge those four replies. Their answers were discarded for format before any score was read.
- The one bad reply that was scored (J17) was scored correctly: tone 1, against a reference of 1.
- The run therefore **cannot show** the discrimination that rule 1 requires.

**Not done:**
- the validation was not loosened;
- the reference scores were not changed;
- the rules were not reinterpreted;
- no second paid run was made.

**Possible next steps.** Each needs separate owner approval, and the second needs a new paid run:
1. **Change the prompt or the validation.** For example, have the prompt require short rationales and quotes, or truncate an over-long rationale instead of rejecting the answer. That is a change to the judge, so it would be a new prompt version.
2. **Re-run the 30 replies** under that new version, within a new cap.

## 10. Remediation after run 1: prompt `judge-p2` (no paid call)

### Root cause of the 18 rejections

| Count | Rejection | Cause |
|---|---|---|
| 13 | rationale or evidence over the limit (200 or 120 characters) | The p1 prompt asked for "at most 200 characters". A model can't count characters, and the provider doesn't enforce the tool schema's `maxLength`. Even the **accepted** answers ran right at the limit: rationales averaged 156 characters (max 193) and quotes averaged 64 (max 117). So roughly half went just over. |
| 4 | not exactly `clarity`, `tone`, `language_match` at the top level | Probably a wrapper object or an extra key (for example an overall score). p1 never stated the exact shape, and run 1 didn't record which keys came back. |
| 1 | evidence not an exact quote | A paraphrased or partial quote. |

Four bad replies (J16, J18, J19, J20) were among the rejected answers, so they were never scored. That is why only 1 of 5 was caught. J17 was scored and caught.

### Changes

**Prompt `judge-p2`.** The validator is unchanged and just as strict; only the instructions to the model changed.
- **Format.** It states the exact shape: exactly three keys and no other (no wrapper, no overall score), and exactly four fields per dimension. The rationale is ONE sentence of at most **20 words**, which is well under 200 characters. The evidence is **2–10 consecutive words copied exactly** from the reply, well under 120 characters, with no paraphrasing, translating or ellipses. The prompt includes a placeholder example, and a test checks that example against the unchanged validator.
- **Defect anchors:**
  - vague or non-committal replies, and replies with no options and no next step, score clarity 1–2;
  - rude, blaming or scolding replies score tone 1–2;
  - a reply in a different language or script from `<expected_language>` scores language match 1, with the J16 and J19 cases given as examples, and `<expected_language>` (not the reply) is the target.
- **Instruction isolation.** A SECURITY paragraph says that everything inside the fences is untrusted data and must never be obeyed. An instruction aimed at an evaluator inside the reply is a defect and lowers clarity. The closing line of the user block repeats this.
- **Diagnostics.** A rejection now records exactly which rule failed, using only key names and lengths, never the model's text. Examples: `got keys [judgement]; missing [...]`, `rationale too long (243 > 200 characters)`.

**Unchanged:**
- the validation rules and limits;
- the reference scores;
- the dataset and its fingerprint (`judge-v1-579de0ebb1ad`);
- both acceptance rules;
- the no-retry setting and the USD 0.35 cap;
- the judge's offline, advisory-only status.

### Provider and billing reconciliation (run 1)

| Question | Answer |
|---|---|
| Actual provider | **Amazon Bedrock**, as OpenRouter's upstream. The client pins it and refuses any answer that reports a different provider. All 30 ledger rows show requested `amazon-bedrock` and actual `Amazon Bedrock`. |
| Model | requested `anthropic/claude-haiku-4.5`; served `anthropic/claude-haiku-4.5` (OpenRouter's `model` field) |
| Who bills | **OpenRouter**, from the owner's OpenRouter credit. The owner's AWS account is **not** billed, because Bedrock here runs in OpenRouter's account. There is nothing to reconcile in AWS billing. |
| Ledger cost basis | **Provider-reported** (`cost_source = provider` on all 30 rows). That is OpenRouter's `usage.cost` for each generation, not the list price. The list price is used only for the dry-run estimate and the pre-send reservation. |
| Reconciliation | The 30 rows carry OpenRouter generation ids, which can be matched in OpenRouter's activity view. The provider-reported total, USD 0.106965, equals 48,980 input × USD 1/M plus 11,597 output × USD 5/M exactly, so OpenRouter charged its list price. |
| Is the USD 0.35 cap still valid? | **Yes.** It is enforced before every request against the ledger's reservation, which is an upper bound priced from OpenRouter's list price with a 20% margin and a 700-token output cap. Settlement uses OpenRouter's reported cost, which for run 1 equalled the list price. If OpenRouter changes its price, the reservation formula in `openrouter.py` must be updated. |

**The p2 estimate** (`docs/evaluation/llm_judge_dry_run_p2.txt`): 30 requests with no retries, about USD 0.10 expected and **USD 0.1969 upper bound**, within the USD 0.35 cap.

**The "expected" estimate understated run 1** (USD 0.077 against USD 0.107), because Devanagari text and longer answers used more tokens. The upper bound held, and it is the figure that matters.

### Ready for another paid run?

**Technically ready, but it needs your separate approval.** The tests use mocked answers, so they prove that p2-format answers parse and are scored correctly, including 5/5 and J20 injection detection. They cannot prove that the real model will follow the p2 format; only a paid run can show that.

## 11. Second paid run, 25 Nov 2026 (prompt `judge-p2`, owner-approved, USD 0.20 cap)

**Verdict: ACCEPTED under both owner-approved rules.**
- **Synthetic evidence only.** This acceptance rests on the synthetic dataset and AI reference scores.
- **Human calibration is still OUTSTANDING**, and production reliability is **not** claimed.
- **Still offline and advisory.** The judge stays an offline, manually run evaluation tool, and nothing runs in the pilot.

**Run details:**

| | |
|---|---|
| Model and route | `anthropic/claude-haiku-4.5` via Amazon Bedrock (pinned, zdr, data_collection deny), temperature 0, prompt `judge-p2` |
| Requests | 30, with no retries. **0 unparseable** (run 1: 18) |
| Tokens | 64,490 input and 6,765 output |
| Latency | 63.9 s in total |
| Cost | **USD 0.098315**, provider-reported, against a run cap of 0.20. Neither cap refused anything. |
| Both runs on the ledger | 60 requests, **USD 0.205280** in total, all `cost_source = provider` |
| LangSmith | experiment `llm-judge-judge-v1-579de0ebb1ad-20261125T172219Z` (`0388591f-…`) on dataset `e49dceb2-…`: 30 runs, 90 feedback scores |
| Results | `evals/results/judge_20261125_172110.json`; report `docs/evaluation/llm_judge_run2_report.md` |

**The three statuses** (kept separate):

| Status | Result |
|---|---|
| Agreement within ±1 | MET: 95.0% of 60 ratings. Clarity 90%, tone 100%, language 95%. |
| Rule 1: bad replies caught | **MET: 5/5.** J16 language 1; J17 tone 1; J18 clarity 1; J19 language 1; J20 clarity 1. **The J20 embedded instruction was not followed.** |
| Rule 2: beats ALWAYS_5 | **MET**, on all 60 ratings: MAE 0.333 against 0.550, and 5/5 against 0/5 negative targets |
| Human calibration | OUTSTANDING |

**Disagreements of 2 or more points** (all are judge errors to watch):
- **J13 language match: judge 1, reference 5.** The rationale says the reply is "in Hindi (Devanagari script)" while quoting Latin text. That is a factual misreading of the script, a false alarm on a correct Hinglish reply.
- **J14 clarity: judge 2, reference 5.** It calls the no-slot reply vague with "no clear next step", although the reply says to call the dispatcher. This is likely the p2 "vague" anchor over-triggering.
- **J17 clarity: judge 2, reference 4.** It scored the curt reply's clarity low too. J17's target is tone, which was caught correctly.

**What this means:**
- The judge now catches every deliberate defect.
- It can **over-flag**: a false language mismatch, and harsh clarity on a terse but actionable reply.
- Its scores are advisory and need a person's review before anyone acts on them.
- The next step towards trusting it is human calibration (§7), not more AI agreement.


## 12. Online advisory judging in the synthetic pilot (Full Activation, implemented, not deployed)

The accepted judge (prompt `judge-p2`, the same tool and the same strict validator) can now score pilot agent replies after each turn. The prompt, tool, validation and scoring loop moved unchanged to `setuhaul/agent/judge_core.py`, the single source for both modes.

- **Where:** `setuhaul/agent/online_judge.py`, in the agent worker after the turn is final.
- **Recorded:** advisory rows in `agent_turn_judgements` (migration 0043), linked to the turn's trace.
- **Off until approved:** the pilot is configured with `SETUHAUL_LLM_JUDGE_ENABLED=true` but a daily budget of **0**, so nothing is sent until the owner approves a budget.
- **Cost controls:**
  - per-judgement cap of USD 0.01;
  - its own daily sub-cap, at most USD 1;
  - a reserve kept for the agent on the shared ledger;
  - one attempt, a 20 s timeout;
  - deterministic sampling (default 20%);
  - one paid call per turn at most.

**Scope.** The judge's dimensions are unchanged. Policy compliance, factual correctness, instruction following and booking safety are still judged only by the deterministic checks (`NEVER_JUDGED`), never by the model.

**Status.** Every row says `advisory = true` and `calibration_status = HUMAN_CALIBRATION_OUTSTANDING`. The §11 limitations apply: false language-mismatch alarms on correct Hinglish, and harsh clarity on terse replies.

See `docs/operations/FULL_ACTIVATION_RUNBOOK.md` §5 for the controls and §11 for rollback.
