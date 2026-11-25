# Judge vs AI reference scores (judge-v1-579de0ebb1ad)

> **AI reference scores (model-generated, reviewed and accepted by the user). This is NOT independent human calibration: agreement here is model-to-model agreement.** Reference scorer: ChatGPT independent reference.

> FABRICATED judge output for illustrating the report format only. No model was called; these are not real judge scores.

**Verdict:** PRELIMINARY PASS against the AI reference (not human calibration)  
**Acceptance:** preliminary: >= 80% of comparable dimension-level ratings within +-1

| Scope | Comparable | Within ±1 | Exact | MAE | Mean signed error | Direction |
|---|---|---|---|---|---|---|
| Overall | 57 | 96.5% | 73.7% | 0.298 | 0.228 | no systematic bias |
| clarity | 19 | 94.7% | 78.9% | 0.263 | 0.053 | no systematic bias |
| tone | 19 | 100.0% | 52.6% | 0.474 | 0.474 | no systematic bias |
| language_match | 19 | 94.7% | 89.5% | 0.158 | 0.158 | no systematic bias |

## Disagreements of 2 or more points

- J19 language_match: judge 3, reference 1 (+2)
- J20 clarity: judge 3, reference 1 (+2)

## Flagged for manual inspection (J16–J20, the deliberate contrasts)

| Id | Reference (clarity / tone / language) | Judge | Largest gap |
|---|---|---|---|
| J16 | 5 / 4 / 1 | 5 / 5 / 2 | 1 |
| J17 | 4 / 1 / 5 | 4 / 2 / 5 | 1 |
| J18 | 1 / 3 / 5 | 2 / 4 / 5 | 1 |
| J19 | 5 / 4 / 1 | 5 / 5 / 3 | 2 |
| J20 | 1 / 3 / 5 | 3 / 4 / 5 | 2 |

## Not comparable (3)

- J13 clarity: no scored judgement
- J13 tone: no scored judgement
- J13 language_match: no scored judgement
