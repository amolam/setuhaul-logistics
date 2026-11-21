# Golden dataset v2

v2 is v1 (`evals/golden/v1/`, reviewed as hash `66a96947a6bf1ad7`) with **one** scenario changed after the step 3 review. The format, the composition and the remaining 59 scenarios are identical. See `../v1/README.md` for the format, findings and the model results recorded against v1.

| Scenario | v1 | v2 | Why |
|---|---|---|---|
| BB10 "The second one works, but I need to leave by 9." | Read-back straight away | Turn 1: `ASK:latest_finish_kind`, with no selection. Turn 2, "Gate-out.": options re-offered under the constraint (`gate_out_by` 21:00) | Review S3-01: a pick that arrives with a new constraint is never selected in the same turn. "Leave by" is ambiguous (decision D1) |

Kept as v1, as confirmed by the reviewer:
- **AT06** stays `earliest_start` 14:00 for "a Gurugram slot at 2 PM": a requested start time, not an arrival time. Haiku p2 still reads it as an arrival time. The prompt is not tuned for this one scenario; the held-out set (B-EV-1b) will show whether it is a pattern.
- **H04** stays a safety escalation, as Phase 1 currently requires. The rule itself awaits an owner decision.

v1 stays frozen. `tests/test_golden.py` pins its hash.
