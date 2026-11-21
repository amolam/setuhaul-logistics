# Golden dataset v3

v3 = v2 (sent for review as `d9fc6777cb68343f`) + the owner decisions OD-1 (finish-based grace, T39) and OD-2 (three-tier breakdown escalation, T40). The 60 core scenarios keep the signed-off composition. A separate **policy** group (7) holds the decision-specific cases. v1 and v2 stay frozen; `tests/test_golden.py` pins their hashes.

## Changes to the core 60

| Scenario | v2 | v3 | Why |
|---|---|---|---|
| L01 | "On schedule, 12:10" → keep the slot | "A bit early, 11:50" → keep | Seed finding below: 12:10 is already at risk under the design |
| L02 | 15 min late → keep | → options after the confirmation | Same finding |
| ADV02 | ETA 12:30 → keep | → options, ranked as LOW | Same finding; the priority-spoofing point is unchanged |
| H04 | Repair in progress → safety takeover | → BREAKDOWN escalation (DECIDE) + one safety question | OD-2 |

**Seed finding.** SHP1017's slot has no slack. Unload 50 min plus changeover 10 fills D1 12:00–13:00. A driver-declared ETA carries a 15-minute band, and gate-to-dock is 10 min. So under OD-1 the slot is at risk for any ETA after 12:05, including the shipment's own planned ETA of 12:15. Most seed slots are 60 minutes with occupancy of 60 minutes or more, so expect "at risk" often. That is the data, not the engine.

## Policy group (7)

| ID | Case | Expected |
|---|---|---|
| P01 | Puncture at a dhaba, repair ongoing | BREAKDOWN escalation, DECIDE, no safety question |
| P02 | Broke down in the middle of the highway | SAFETY takeover |
| P03 | Broke down, safety unknown, then "nahi" | BREAKDOWN + question, then SAFETY takeover (one BREAKDOWN escalation only) |
| P04 | Engine failure, safety unknown, then "haan safe hai" | BREAKDOWN + question, then acknowledgement; no takeover |
| P05 | Tyre repaired, moving, reaching 12:30 | Normal ETA update and options |
| P06 | SHP1017 ETA 12:05: finishes exactly at the grace deadline | Keep the slot |
| P07 | SHP1017 ETA 12:06: one minute past it | Options |

The exact OD-1 boundaries, including no-show, are unit-tested in `tests/test_feasibility.py::test_od1_*`. The OD-2 tiers, takeover and hand-back are tested end to end in `tests/test_orchestrator.py::test_od2_*`.

## Results (18 Nov 2026, dataset `v3+sha256:f28789922065e7d2`, OpenRouter, synthetic data)

| Extractor | Fully right turns | Schema valid | ETA precision / recall | Intent accuracy |
|---|---|---|---|---|
| Rules baseline | 58 / 74 | 1.00 | 1.00 / **0.84 ✗** | **0.76 ✗** |
| Haiku 4.5, prompt p3 | **71 / 74** | 1.00 | 1.00 / 1.00 | 1.00 |

p3 adds two rules to p2, both from OD-2: a breakdown alone is not a safety concern unless the driver is in danger, and a definition of `vehicle_fixed`. The misses:
- **BB10 turn 2:** for a bare "Gate-out." the model leaves the kind empty because the message has no time. Fixed in code: when the agent has just asked that question, the answer is read from the driver's words (`test_answer_to_the_kind_question_is_read_from_the_drivers_words`).
- **AT06:** the known, debatable label.
- **H04:** the language came back as `hi` instead of `hi-Latn`. p2 got it right; one-off variation.

Still not a held-out estimate. p2 and p3 were written with this set in view (B-EV-1b).
