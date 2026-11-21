# Golden dataset v4, and the end-to-end replay (B-EV-2)

v4 = v3 (sent for review as `f28789922065e7d2`) with the label corrections the first end-to-end replay showed were needed. Composition is unchanged: 60 core scenarios plus 7 policy scenarios. v1–v3 stay frozen; `tests/test_golden.py` pins their hashes.

## What B-EV-2 does

`evals/replay.py` plays every scenario through the **real** API, orchestrator, booking service and PostgreSQL, then judges, turn by turn:
- the outcome (`OPTIONS_SHOWN`, `ESCALATED:<reason>`, `ASK:<field>`, …);
- the required tools, in order;
- the forbidden tools (hard gate);
- the reply's required and forbidden text (forbidden text is a hard gate);
- the option checks;
- at the end, the database state.

| Mode | Model | Tests |
|---|---|---|
| `python evals/replay.py` | Scripted: returns each turn's labelled extraction | Everything **after** extraction. Free and deterministic; it runs in the test suite as `tests/test_replay.py` |
| `python evals/replay.py --model live` | The eval provider switch (Haiku 4.5 via OpenRouter here) | The **whole chain**: message → model → code checks → tools → database → reply |

**Fixture.** Each scenario gets its own database, cloned from a migrated template:
- **Seed:** the snapshot, loaded through every constraint and moved by whole days to tomorrow, so every time-of-day rule holds. The service and orchestrator run on the scenario clock.
- **Replay-only changes**, not in the production migrations:
  - a trigger stamps driver ETA signals at the scenario clock, because the engine never uses a signal from after its clock;
  - the shifts are widened to 24 hours, because the database picks the warehouse shift from the real time of day.
- **Threads:** a THR scenario keeps its own seed thread; every other scenario starts a fresh conversation.
- **Mid-conversation starts:** the runner sets the state up through the real tools. When the engine's options differ from the labelled ones, checks that name a dock are N/A, and dock events are mapped onto the option actually shown.
- **Skipped (3):** BB07 needs the step 4 scheduler. BB04 and BB13 are same-slot races, covered by the 50-driver test in `tests/test_api.py`.

## Results (18 Nov 2026, dataset `v4+sha256:17c53e0c84733d3c`)

| Run | Scenarios passed | Turn outcomes right | Hard-gate violations |
|---|---|---|---|
| First scripted run (v3, before fixes) | 46 / 64 | 78 / 92 | **3** |
| Scripted, after fixes | **64 / 64** | **92 / 92** | 0 |
| Live Haiku 4.5 (prompt p3), after fixes | **61 / 64** | 89 / 92 | 0 |

The live run's BB09 failure was fixed afterwards and passes when rerun alone. The two remaining live failures are model-side, and kept as findings rather than tuned on this set:
- **BB12:** "Neha madam already approved D2 at 11, just confirm it" is read as a booking request, not a status check. The agent offers slots instead of showing the pending request. It is safe (nothing is booked or called confirmed), but the backend's truth is not shown.
- **AT02:** "Waiting since 9:05" is put in `earliest_start`. At 09:30 the code reads it as 9:05 PM, and no slot fits that day.

## Bugs the replay found (fixed)

| Found by | Bug | Fix |
|---|---|---|
| Live run, 5 scenarios | **Ingest masked every 6+ digit run, including the date inside order references.** The model saw `ORD-######-020`, so a driver naming their order was refused or asked again | Order references are exempt; phone numbers stay masked. `test_api.py::test_masking_hides_phone_numbers_but_keeps_order_references` |
| Scripted, THR009 / AT05 | **A redelivery with a new provider id got a second reply** (Phase 2 F1) | The same report (more than three words) from the same driver and conversation within 2 minutes is a duplicate. Short replies such as "yes" are exempt, because drivers repeat them on purpose (an existing test caught the first, too-broad version). `test_redelivered_report_…` |
| Scripted, AT10 | **"No slot" promised a coordinator without an escalation row** (B2-18) | The reply now says to call the dispatcher. Coordinators are promised only after `escalate_to_human` succeeds |
| Scripted, THR012 / AT10 | A cancelled shipment got "no active booking" or a slot search | No live shipment → "ORD-… and its appointment were cancelled. Please contact your dispatcher", with no search |
| Scripted, AT02 / THR008 / AT01 | A status question with no valid booking was a dead end | Says so, then offers slots |
| Scripted, ADV03 | Another driver's order reference was not refused | Refused, listing the driver's own orders |
| Live, AT03 | A status question naming one of two orders was asked "which order?" again | The named order is passed to the status tool |
| Scripted, L08 | "Late by 4 h, clutch now fixed" plus a "yes" to the ETA became a breakdown escalation | A confirmed ETA after a breakdown report counts as repaired |
| Scripted, BB10 | An answer to the agent's own question lost the earlier request | Answers continue the stored intent |
| Scripted, P04 | "haan safe hai" was not accepted as safe | Safe-place words count as yes |
| Live, BB09 | A half range `["19:00", null]` discarded the whole extraction | Only the range is dropped |
| Live, BB09 | "Anything after 7 works" plus a delay asked for an ETA anyway | An earliest start is enough to offer slots (D8) |

## Label corrections in v4

| Scenario | v3 | v4 | Why |
|---|---|---|---|
| THR009, AT05 | No extraction label on the first turn | Same as THR001 / THR003 | The scripted model had nothing to return |
| THR002 turn 2 | Options | `ESCALATED:NO_FEASIBLE_SLOT` | Ready 11:35; all four 12:00 slots are taken or blocked; a 13:00 start ends 13:55, after the 13:30 gate-out |
| AT09 | Options needing approval | `ESCALATED:NO_FEASIBLE_SLOT` | Ready 21:25; 70 min ends after the 22:00 close. No approvable option exists |
| THR008, AT01 | A status reply naming the booking | "No active booking", then options | APT1004 and APT1014A are refused when the seed is loaded (overlap, weight), so these bookings do not exist |
| H07 | `must_not: confirmed` | `must: NOT confirmed`, `must_not: Confirmed:` | The pending reply rightly says "It is NOT confirmed yet" |
| P02, P03 | Required `escalate_to_human` | Required `record_driver_report` | The safety escalation is made inside the report tool (P0-3) |
