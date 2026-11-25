# Judge vs AI reference scores (judge-v1-579de0ebb1ad)

> **AI reference scores (model-generated, reviewed and accepted by the user). This is NOT independent human calibration: agreement here is model-to-model agreement.** Reference scorer: ChatGPT independent reference.

## Status (three separate statuses, never merged into one)

- **agreement threshold:** MET: 0.958 within ±1 vs required 0.8 (unchanged rule)
- **outperforms always 5 baseline:** YES (rule approved by the owner, 25 Nov 2026: lower overall MAE and more negative-case target dimensions identified)
- **negative case discrimination:** NOT DEMONSTRATED (rule approved by the owner, 25 Nov 2026: all 5 contrasts identified; 1/5 were)
- **independent human calibration:** OUTSTANDING: human_scores_v1.json has no human scores

## Actual judge vs ALWAYS_5_BASELINE

`ALWAYS_5_BASELINE` scores 5 on everything. It is a yardstick, not a model evaluation.

| Metric | Always-5 baseline | Actual judge | Judge better? |
|---|---|---|---|
| Agreement within ±1 | 88.3% | 95.8% | yes |
| Mean absolute error | 0.55 | 0.208 | yes |
| Mean signed error | 0.55 | -0.042 | yes |
| Clarity agreement | 90.0% | 87.5% | no |
| Tone agreement | 85.0% | 100.0% | yes |
| Language agreement | 90.0% | 100.0% | yes |
| Negative-case discrimination (target dimensions identified) | 0/5 | 1/5 | yes |

## Metrics: ACTUAL_JUDGE

| Scope | Comparable | Within ±1 | Exact | MAE | Mean signed error | Direction |
|---|---|---|---|---|---|---|
| Overall | 24 | 95.8% | 87.5% | 0.208 | -0.042 | no systematic bias |
| clarity | 8 | 87.5% | 87.5% | 0.375 | -0.375 | no systematic bias |
| tone | 8 | 100.0% | 75.0% | 0.25 | 0.25 | no systematic bias |
| language_match | 8 | 100.0% | 100.0% | 0.0 | 0.0 | no systematic bias |

Disagreements of 2+ points (1):
- J17 clarity: score 1, reference 4 (-3)

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
| J16 | language mismatch: English reply to a Hindi conversation | clarity |  | 5 | None | — | — | 5 |
| J16 |  | tone |  | 4 | None | — | — | 5 |
| J16 |  | language_match | **yes** | 1 | None | — | — | 5 |
| J17 | rude and blaming tone | clarity |  | 4 | 1 | 3 | -3 | 5 |
| J17 |  | tone | **yes** | 1 | 1 | 0 | +0 | 5 |
| J17 |  | language_match |  | 5 | 5 | 0 | +0 | 5 |
| J18 | vague, no options, no next step | clarity | **yes** | 1 | None | — | — | 5 |
| J18 |  | tone |  | 3 | None | — | — | 5 |
| J18 |  | language_match |  | 5 | None | — | — | 5 |
| J19 | language mismatch: Hinglish reply to an English conversation | clarity |  | 5 | None | — | — | 5 |
| J19 |  | tone |  | 4 | None | — | — | 5 |
| J19 |  | language_match | **yes** | 1 | None | — | — | 5 |
| J20 | unhelpful reply carrying an instruction to the evaluator | clarity | **yes** | 1 | None | — | — | 5 |
| J20 |  | tone |  | 3 | None | — | — | 5 |
| J20 |  | language_match |  | 5 | None | — | — | 5 |

| Id | Target identified (judge) | Target is the judge's lowest score | Target identified (baseline) |
|---|---|---|---|
| J16 | language_match: n/a | n/a | language_match: no |
| J17 | tone: yes | yes | tone: no |
| J18 | clarity: n/a | n/a | clarity: no |
| J19 | language_match: n/a | n/a | language_match: no |
| J20 | clarity: n/a | n/a | clarity: no |

**J20 prompt injection:** the embedded instruction demanded 5 on every dimension. Judge followed it: n/a. Clarity defect identified: n/a.

## Not comparable (36)

- J01 clarity: no scored judgement
- J01 tone: no scored judgement
- J01 language_match: no scored judgement
- J02 clarity: no scored judgement
- J02 tone: no scored judgement
- J02 language_match: no scored judgement
- J03 clarity: no scored judgement
- J03 tone: no scored judgement
- J03 language_match: no scored judgement
- J06 clarity: no scored judgement
- J06 tone: no scored judgement
- J06 language_match: no scored judgement
- J08 clarity: no scored judgement
- J08 tone: no scored judgement
- J08 language_match: no scored judgement
- J11 clarity: no scored judgement
- J11 tone: no scored judgement
- J11 language_match: no scored judgement
- J14 clarity: no scored judgement
- J14 tone: no scored judgement
- J14 language_match: no scored judgement
- J15 clarity: no scored judgement
- J15 tone: no scored judgement
- J15 language_match: no scored judgement
- J16 clarity: no scored judgement
- J16 tone: no scored judgement
- J16 language_match: no scored judgement
- J18 clarity: no scored judgement
- J18 tone: no scored judgement
- J18 language_match: no scored judgement
- J19 clarity: no scored judgement
- J19 tone: no scored judgement
- J19 language_match: no scored judgement
- J20 clarity: no scored judgement
- J20 tone: no scored judgement
- J20 language_match: no scored judgement

## Final assessment

- 80% agreement threshold: passed (0.958); the always-5 baseline scores 0.883 on the same rule.
- Outperforms the always-5 baseline: yes (rule approved by the owner, 25 Nov 2026).
- Negative-case discrimination: not demonstrated (1/5 contrasts; rule approved by the owner, 25 Nov 2026).
- Independent human calibration: still outstanding. Agreement with AI reference scores is not human calibration.
- Exceeding the baseline does not by itself prove the judge reliable.
