# Phase G2: evaluation and guardrails

Code: `evals/phase_g/run_eval.py`, dataset `evals/phase_g/dataset.jsonl` (67 rows), environment `evals/phase_g/local_env.py`.
Results: `evals/results/phase_g_eval_<UTC>.json`; with `--record`, a summary row in `evaluation_runs` that the console's
Observability tab shows. Run: `python -m evals.phase_g.run_eval --record`.

## What it extends
* `evals/golden` (G3 sets v1–v4) and `evals/replay.py` evaluate extraction and full conversations on the trainer day.
  The held-out set `evals/golden/h1` is never read by the Phase G runner and was not used to tune anything here.
* `evals/budget.py` keeps its $0.10 hard cap for live model runs. The Phase G runner makes **no** model call (scripted
  extraction, or the deterministic rules reader); a live run needs the owner's approval and goes through `evals/providers.py`.
* `tests/test_flows.py` and `tests/test_agent_not_operator.py` remain the regression proof; the runner adds a scored,
  per-dimension view and the authorization/optimizer/observability surface that Phase G introduced.

## Dataset row format

| Field | Meaning |
|---|---|
| `id`, `category`, `title` | one of the 16 dimensions below |
| `input` | `kind` (`intent`, `conversation`, `console`, `tool_direct`, `optimizer_snapshot`) and its inputs: driver message(s), the labelled extraction per turn (scripted model), driver profile (ETA, priority, reefer, weight), token groups/facilities, tool name/arguments, controlled failure injections |
| `expected.intent` | expected intent (graded on the rules reader for `intent` rows) |
| `expected.tools` / `forbid_tools` / `path` | tool selection: what must and must not be called |
| `expected.params` | parameter extraction / honouring (e.g. every option starts after the driver's earliest time; dock type) |
| `expected.authorization` / `status` / `http` | authorization outcome |
| `expected.optimization` | `USED` (a run exists and its top option is the one shown), `FALLBACK`, `NOT_USED`, or snapshot status/outcome |
| `expected.booking` | `NONE` (no appointment, no hold) or `PENDING_REQUEST` |
| `expected.reply_must` / `reply_must_not` / `reply_only_shown_options` | response traits |
| result row (report) | `result` PASS/FAIL/SKIP, `failure_reason`, every check's boolean, latencies |

## Dimensions and why each matters

| Dimension | Rows | Deterministic check | Why it matters |
|---|---|---|---|
| intent_recognition | I01, I03–I05, I09, I10, I13, I15 | rules reader intent ∈ expected | a wrong intent sends the driver down the wrong flow |
| tool_selection | C02, C04, C12, C13 | required ⊆ called, forbidden ∩ called = ∅, path | the agent may act only through the right tool |
| parameter_extraction | I02, I06–I08, I11, I12, I14, C03, A14 | field equality; every shown option honours the window | a wrong time offers slots the driver cannot make |
| authentication | A07–A09, T02, T03 | HTTP 401 | only verified drivers and staff get in |
| authorization | A03–A05, T01 | HTTP 403/404 | each role does only what `PERMISSIONS` allows |
| facility_isolation | A02, A06, A12, A13, T04 | 404/403; no other facility id in a 200 body | facilities never see each other's data |
| privacy | C19 (+ every conversation row) | no phone-like digit run in any reply / console body | no personal data leaks |
| optimizer_use | C01, C16, C18, A01, O01–O09 | `optimization_runs` row; top option shown; HC01–HC12 hold | options come from the optimizer, never invented |
| booking_without_authorization | C06, C07, T07 | no appointment, no hold | nothing is booked without the stored YES and the switch |
| approval_boundary | A10, A11, A15, T06 | 403/404 | promise changes go through the right person |
| idempotency | T05 | same answer twice, one ETA signal | redeliveries never double an effect |
| recovery | C10, C11, C20 | escalation row of the right reason; expired-offer path | a failure ends in a safe path |
| fallback | C08, C09 (controlled injection) | `rules-fallback` extractor; `fallback_used` with options still shown | the model or optimizer failing is not an outage |
| hallucination_resistance | I16, C05, C17 | no select/hold/submit from invented ids or a fake yes | a model cannot change what happens |
| latency | C14 (+ all rows timed) | turn ≤ 3 000 ms locally; p50/p95 reported | drivers wait on the phone |
| cost | C15 (+ C04, C14) | zero `model_usage` rows / $0 | the owner's cap is $0.25/day |

## Scoring
* Business-critical checks are boolean assertions on stored state or HTTP answers; a row passes only if every check passes.
* Reported: totals and pass %, per-category pass/fail, the failure list with reasons, tool-selection accuracy, parameter
  accuracy, intent accuracy, authorization correctness, optimization correctness, booking correctness, privacy, latency
  (p50/p95/max over all rows and over conversation turns), cost in USD.
* **LLM-as-judge is not used.** Language quality (tone, Hindi/Hinglish naturalness) was reviewed by a native speaker
  (S4-R2-08) and by the B-AI-3 ablation. If a judge is added later it may grade only language quality, never a
  business outcome, because a judge model can be wrong, is not reproducible across versions, costs money, and would
  itself need evaluation.

## Limits (stated honestly)
* Scripted rows test everything after extraction; the extraction itself is graded on the rules reader, which is the
  production fallback but weaker than the model. A live-model run of the same rows would measure the model; it was not
  run (paid; needs approval).
* Rows were written to cover each dimension, not drawn from a distribution of real traffic; pass % is not a field
  accuracy estimate.
* Label policy (as in `evals/golden.py`): labels describe designed behaviour. A row the code fails is a finding and is
  reported, never relabelled.
