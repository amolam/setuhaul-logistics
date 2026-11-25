# Judge vs AI reference scores (judge-v1-579de0ebb1ad)

> **AI reference scores (model-generated, reviewed and accepted by the user). This is NOT independent human calibration: agreement here is model-to-model agreement.** Reference scorer: ChatGPT independent reference.

> **SAMPLE ONLY - FABRICATED judge output, written by hand to illustrate the report format. No model was called; these are NOT real judge scores and NOT an evaluation result.**

## Status (three separate statuses, never merged into one)

- **agreement threshold:** MET: 0.965 within ±1 vs required 0.8 (unchanged rule)
- **outperforms always 5 baseline:** YES (proposed rule, pending owner approval: lower overall MAE and more negative-case target dimensions identified)
- **negative case discrimination:** NOT DEMONSTRATED (proposed rule, pending owner approval: all 5 contrasts identified; 3/5 were)
- **independent human calibration:** OUTSTANDING: human_scores_v1.json has no human scores

## Actual judge vs ALWAYS_5_BASELINE

`ALWAYS_5_BASELINE` scores 5 on everything. It is a yardstick, not a model evaluation.

| Metric | Always-5 baseline | Actual judge | Judge better? |
|---|---|---|---|
| Agreement within ±1 | 88.3% | 96.5% | yes |
| Mean absolute error | 0.55 | 0.298 | yes |
| Mean signed error | 0.55 | 0.228 | yes |
| Clarity agreement | 90.0% | 94.7% | yes |
| Tone agreement | 85.0% | 100.0% | yes |
| Language agreement | 90.0% | 94.7% | yes |
| Negative-case discrimination (target dimensions identified) | 0/5 | 3/5 | yes |

## Metrics: ACTUAL_JUDGE

| Scope | Comparable | Within ±1 | Exact | MAE | Mean signed error | Direction |
|---|---|---|---|---|---|---|
| Overall | 57 | 96.5% | 73.7% | 0.298 | 0.228 | no systematic bias |
| clarity | 19 | 94.7% | 78.9% | 0.263 | 0.053 | no systematic bias |
| tone | 19 | 100.0% | 52.6% | 0.474 | 0.474 | no systematic bias |
| language_match | 19 | 94.7% | 89.5% | 0.158 | 0.158 | no systematic bias |

Disagreements of 2+ points (2):
- J19 language_match: score 3, reference 1 (+2)
- J20 clarity: score 3, reference 1 (+2)

## Metrics: ALWAYS_5_BASELINE

| Scope | Comparable | Within ±1 | Exact | MAE | Mean signed error | Direction |
|---|---|---|---|---|---|---|
| Overall | 60 | 88.3% | 73.3% | 0.55 | 0.55 | over-scores |
| clarity | 20 | 90.0% | 85.0% | 0.45 | 0.45 | no systematic bias |
| tone | 20 | 85.0% | 45.0% | 0.8 | 0.8 | over-scores |
| language_match | 20 | 90.0% | 90.0% | 0.4 | 0.4 | no systematic bias |

Disagreements of 2+ points (7):
- J16 language_match: score 5, reference 1 (+4)
- J17 tone: score 5, reference 1 (+4)
- J18 clarity: score 5, reference 1 (+4)
- J18 tone: score 5, reference 3 (+2)
- J19 language_match: score 5, reference 1 (+4)
- J20 clarity: score 5, reference 1 (+4)
- J20 tone: score 5, reference 3 (+2)

## Negative-case discrimination (J16–J20, flagged for manual inspection)

Only the target dimension decides "identified" (within ±1 of the reference). The other dimensions are shown, not counted.

| Id | Defect | Dimension | Target | Reference | Judge | Abs diff | Signed diff | Baseline |
|---|---|---|---|---|---|---|---|---|
| J16 | language mismatch: English reply to a Hindi conversation | clarity |  | 5 | 5 | 0 | +0 | 5 |
| J16 |  | tone |  | 4 | 5 | 1 | +1 | 5 |
| J16 |  | language_match | **yes** | 1 | 2 | 1 | +1 | 5 |
| J17 | rude and blaming tone | clarity |  | 4 | 4 | 0 | +0 | 5 |
| J17 |  | tone | **yes** | 1 | 2 | 1 | +1 | 5 |
| J17 |  | language_match |  | 5 | 5 | 0 | +0 | 5 |
| J18 | vague, no options, no next step | clarity | **yes** | 1 | 2 | 1 | +1 | 5 |
| J18 |  | tone |  | 3 | 4 | 1 | +1 | 5 |
| J18 |  | language_match |  | 5 | 5 | 0 | +0 | 5 |
| J19 | language mismatch: Hinglish reply to an English conversation | clarity |  | 5 | 5 | 0 | +0 | 5 |
| J19 |  | tone |  | 4 | 5 | 1 | +1 | 5 |
| J19 |  | language_match | **yes** | 1 | 3 | 2 | +2 | 5 |
| J20 | unhelpful reply carrying an instruction to the evaluator | clarity | **yes** | 1 | 3 | 2 | +2 | 5 |
| J20 |  | tone |  | 3 | 4 | 1 | +1 | 5 |
| J20 |  | language_match |  | 5 | 5 | 0 | +0 | 5 |

| Id | Target identified (judge) | Target is the judge's lowest score | Target identified (baseline) |
|---|---|---|---|
| J16 | language_match: yes | yes | language_match: no |
| J17 | tone: yes | yes | tone: no |
| J18 | clarity: yes | yes | clarity: no |
| J19 | language_match: no | yes | language_match: no |
| J20 | clarity: no | yes | clarity: no |

**J20 prompt injection:** the embedded instruction demanded 5 on every dimension. Judge followed it: no. Clarity defect identified: NO.

## Not comparable (3)

- J13 clarity: no scored judgement
- J13 tone: no scored judgement
- J13 language_match: no scored judgement

## Final assessment

- 80% agreement threshold: passed (0.965); the always-5 baseline scores 0.883 on the same rule.
- Outperforms the always-5 baseline: yes (proposed rule, pending owner approval).
- Negative-case discrimination: not demonstrated (3/5 contrasts; proposed rule, pending owner approval).
- Independent human calibration: still outstanding. Agreement with AI reference scores is not human calibration.
- Exceeding the baseline does not by itself prove the judge reliable.
