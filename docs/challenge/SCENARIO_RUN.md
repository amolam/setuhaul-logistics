# Scenario run — recorded 23 Nov 2026, 00:52 IST

**What this is:** the challenge brief's *"Your scenario run must show"* list (p.5): items 1–6, plus the optional item 7, in one continuous run that ends in an escalation.
- **Raw transcript:** `docs/challenge/evidence/scenario_run.json`, with millisecond timestamps and database evidence for each item.
- **Test:** `tests/test_scenario_run.py::test_scenario_run_shows_all_six_items_and_ends_in_an_escalation`. It runs in every full suite. The transcript file is rewritten only with `SETUHAUL_RECORD_SCENARIO=1`.

**What is real in this run:**
- the signed WhatsApp webhook and the API;
- the PostgreSQL roles, functions and row-level security;
- the feasibility engine and the orchestrator;
- the production **rules reader**: no AI model was called, so there was no cost;
- the approval-workflow port, the console decision route and the facility scheduling tool.

**What is local:** WhatsApp (a local provider), Step Functions (a local workflow) and the database (local PostgreSQL 18.6).

The booking path runs under the local test harness, as in every flow test. **The deployed pilot's booking switch stays OFF.** All data is synthetic.

| # | Brief item | Time (s) | What happened | Evidence |
|---|---|---:|---|---|
| 1 | A driver reports a delay and answers clarification questions | 1.3–3.1 | Driver A: *"Stuck in traffic near Neemrana, running late."* The agent asks one question: *"What time do you expect to reach the warehouse gate?"* A answers *"around 03:22"* and gets three slots: D1, D2 and D3 at 4:00 AM. | `path: ["ask:declared_eta_local"]`, then `["options"]` |
| 2 | The driver asks for later options and compares them | 4.9 | A: *"anything after 05:52?"* The agent gives a new list, D1, D2 and D3 at 6:00 AM, which A can compare with the 4:00 AM list. | Both lists are in the evidence (`first_list`, `later_list`) |
| 3 | Several requests run against the same facility schedule | 6.7–8.7 | Drivers B (*"Tyre puncture…"*) and C (*"Late due to rain…"*) report the same arrival time at the same facility. Both are offered the same first slot, D1 at 4:00 AM. | Identical first lines in both replies |
| 4 | At least two requests compete for the same capacity | 9.9–13.2 | B picks 1 and says YES: *"Request sent for dock D1 at 4:00 AM… It is NOT confirmed yet."* C picks the same slot. | B's request is PENDING_CONFIRMATION |
| 5 | The driver sees what happens when an option changes or disappears | 15.2 | C says YES and is told plainly: *"That slot was just taken."* C gets the remaining options (D2 and D3 at 4:00, D1 at 5:00), and the contested slot is not among them. | The database has **one** active appointment on D1 at 4:00 AM (B's) |
| — | The warehouse decides | 20.4 | The planner confirms B's request in the staff console. The appointment becomes CONFIRMED, and B's phone gets the outcome. | `status: CONFIRMED`, `b_phone_shows` |
| 7 | *(optional)* The scheduling tool proposes a new sequence after ETA updates | 20.7 | The planner asks for a facility proposal. It covers 3 trucks: it keeps B's promise on D1 and plans the two unbooked trucks on D2 and D3. Nothing is left unscheduled. | `status: PROPOSAL`, `not_a_booking: true`, **0 appointments changed** (checked before and after) |
| 6 | At least one case ends in escalation because no safe automatic outcome exists | 22.6 | Driver H's truck carries a load no Jaipur dock can take. The agent offers nothing it cannot keep: *"A coordinator has your details and will reply within 10 minutes."* | Escalation `NO_FEASIBLE_SLOT`, mode DECIDE, status OPEN. The hand-off record was built by the database: driver, shipment, thread, what the driver was told, and a 10-minute acknowledgement deadline |

**Found during this run:** the rules reader read *"anything after 05:26"* as 17:26. A zero-padded 24-hour time now keeps its morning meaning, while a driver's *"after 5"* still means the evening. Regression test: `test_a_zero_padded_24_hour_time_keeps_its_morning_meaning`.

**Not shown here:**
- the live AWS pilot runs with booking OFF, so items 4–5 cannot happen there. The AWS end-to-end evidence is in `docs/release/evidence/option_a.json`;
- the AI model reader. Its accuracy evidence is the held-out set, `docs/3-build/README.md` (B-EV-1b).
