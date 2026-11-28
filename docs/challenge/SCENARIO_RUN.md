# Scenario run

Hand-in item 4. There are two reproducible runs. Both execute the real webhook, API, orchestrator, feasibility engine and database, and both use the **deterministic rules reader**, so no AI model is called and nothing is billed. Model-based extraction is evaluated separately (see *Rules reader versus model* below).

| Run | Data | What it proves | Script / test | Evidence |
|---|---|---|---|---|
| **A. Six-item scenario** | Synthetic drivers on a seeded Jaipur facility | All six brief items, plus optional item 7, in one continuous run that ends in an escalation | `tests/test_scenario_run.py::test_scenario_run_shows_all_six_items_and_ends_in_an_escalation` | [`evidence/scenario_run.json`](evidence/scenario_run.json) (recorded 23 Nov 2026) |
| **B. Supplied snapshot** | `data/setuhaul_freight_operations.db` loaded into PostgreSQL through the real constraints; clock fixed to 4 Aug 2026 | Ladder levels 1, 2 and 6, with the drivers' recorded words. **SHP1015 escalates: no feasible slot** | `scripts/snapshot_scenario.py`; `tests/test_snapshot_scenario.py` | [`evidence/snapshot_scenario_run.txt`](evidence/snapshot_scenario_run.txt), [`.json`](evidence/snapshot_scenario_run.json) (recorded 27 Nov 2026) |

The brief's measures are computed from these files: [`evidence/scenario_measures.md`](evidence/scenario_measures.md), using `python scripts/scenario_measures.py --write`.

**No video was recorded.** To capture one, see *Recording* below.

---

## A. Six-item scenario, recorded 23 Nov 2026, 00:52 IST

**What this is:** the challenge brief's *"Your scenario run must show"* list (p.5): items 1–6, plus the optional item 7, in one continuous run that ends in an escalation.
- **Raw transcript:** `docs/challenge/evidence/scenario_run.json`, with millisecond timestamps and database evidence for each item.
- **Test:** `tests/test_scenario_run.py::test_scenario_run_shows_all_six_items_and_ends_in_an_escalation`. It runs in every full suite. The transcript file is rewritten only with `SETUHAUL_RECORD_SCENARIO=1`.

**What is real in this run:**
- the signed WhatsApp webhook and the API;
- the PostgreSQL roles, functions and row-level security;
- the feasibility engine and the orchestrator;
- the **rules reader**, the same deterministic reader the deployed service falls back to: no AI model was called, so there was no cost;
- the approval-workflow port, the console decision route and the facility scheduling tool.

**What is local:** WhatsApp (a local provider), Step Functions (a local workflow) and the database (local PostgreSQL 18.6).

The booking path runs under the local test harness, as in every flow test. **This run is local evidence, not pilot evidence.** When it was recorded (23 Nov 2026) the deployed pilot's booking switch was off. All data is synthetic.

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
- the AWS pilot. On 23 Nov 2026 its booking switch was off, so items 4–5 could not happen there; the AWS evidence of that date is `docs/release/evidence/option_a.json`. Later pilot booking evidence is separate from this hand-in and is indexed in `artifacts/fde-demo/SetuHaul_FDE_Evidence_Register.md`;
- the AI model reader. Its accuracy evidence is the held-out set, `docs/3-build/README.md` (B-EV-1b).

---

## B. Supplied snapshot run, recorded 27 Nov 2026

**What happens:**
- `scripts/snapshot_scenario.py` creates the database `setuhaul_snapshot_scenario`, applies every migration and loads the original SQLite snapshot with `setuhaul.adapters.pg_seed.seed`.
- The database refuses the four known-bad seed bookings: APT1004, APT1005, APT1014A and APT1015A (audit A03, A04 and A08).
- Each driver sends the exact words recorded in the snapshot, through the signed webhook. The service and orchestrator clock is set to the time of that message on 4 Aug 2026.

| Case | Ladder level | Snapshot thread | Driver says | Agent replies | Path |
|---|---|---|---|---|---|
| L1 | 1: only asks about options | THR011 / SHP1017, 10:02 | *"What slots are possible after 1 PM if I reach around 12:45?"* | *"Available slots: 1. Dock D1, 2:00 PM on 4 Aug 2. Dock D2, 2:00 PM… 3. Dock D3, 2:00 PM…"*. Nothing is held or requested | `options` |
| L2a | 2: message misses key facts | THR003 / SHP1013, 09:19 | *"I am late by one hour."* | *"Do you mean you will now reach the gate around 11:00 AM? Reply YES, or send your arrival time."* | `ask:delay_kind` |
| L2b | 2: driver has two live shipments | THR010 / DRV004, 09:31 | *"I will be late by 45 minutes."* | *"You have more than one shipment today. Which order is this about? 1. ORD-260804-004 2. ORD-260804-020"* | `ask-shipment` |
| **L6** | **6: no feasible slot** | **THR005 / SHP1015, 16:24** | *"Evening traffic. ETA 6:30. Can the reefer unload tonight?"* | *"A coordinator has your details and will reply within 10 minutes."* | `options`, `escalated:NO_FEASIBLE_SLOT` |

**Why SHP1015 escalates:**
- SHP1015 is a refrigerated load for Jaipur.
- Jaipur has exactly one reefer dock, D5, and the snapshot has D5 under MAINTENANCE from 18:00 to 22:00.
- The truck is ready at 17:55 at the earliest and needs 70 minutes of dock time (60 unload plus 10 changeover). It cannot finish before the maintenance starts, and no other dock can take a reefer.
- No safe automatic outcome exists, so the database writes an escalation: `NO_FEASIBLE_SLOT`, mode `DECIDE`, status `OPEN`, queue `FAC-JAI-01`, priority `HIGH`, acknowledgement SLA 10 minutes.
- The hand-off record names the driver, the shipment, the thread and exactly what the driver was told. No slot is invented or held.
- The script also re-derives the outcome independently: the same feasibility engine run directly on the SQLite file, plus the `dock_status_events` row. Both give NO_FEASIBLE_SLOT.

**Not shown here:**
- **The booking path.** The database's own clock is today, so a hold on 4 Aug 2026 would be a booking in the past. Booking is therefore switched off in this run. Run A and the integrity proof cover booking under contention.

Result: **PASS, 6/6 checks** (`evidence/snapshot_scenario_run.txt`).

---

## Rules reader versus model

| | Rules reader (both runs here) | Model reader (Claude Haiku 4.5 via OpenRouter) |
|---|---|---|
| Used in | The scenario runs, the integrity proof, the demo console in "rules" mode, and as the fallback in production | The deployed pilot, when the daily budget allows |
| Cost | None | Paid per call, capped daily |
| Evidence | These runs | Held-out extraction set and golden evaluations (`evals/`, `docs/3-build/README.md`) |
| Booking decisions | Never: code and the database decide | Never: code and the database decide |

Neither reader can book, rank or promise. The reader only turns text into a structured report. That is why the scenario results above do not depend on which reader is used, apart from how varied the phrasing it understands can be.

---

## Reproduce

Each step is one command, run from the repository root. Run A needs a local PostgreSQL 18; `python scripts/start_local.py --db-only` starts one.

```bash
export SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres"
```

```bash
python -m pytest -q tests/test_scenario_run.py tests/test_snapshot_scenario.py
```

```bash
python scripts/snapshot_scenario.py
```

```bash
SETUHAUL_RECORD_SCENARIO=1 python -m pytest -q tests/test_scenario_run.py
```

```bash
python scripts/snapshot_scenario.py --record
```

```bash
python scripts/scenario_measures.py --write
```

What each command does:
- **The `pytest` command (no recording):** both runs, checked; no evidence file is rewritten.
- **`snapshot_scenario.py`:** the snapshot run, printed turn by turn.
- **`SETUHAUL_RECORD_SCENARIO=1 … test_scenario_run.py`:** re-records Run A's evidence file.
- **`snapshot_scenario.py --record`:** re-records Run B's evidence files.
- **`scenario_measures.py --write`:** recomputes the brief's measures from the evidence files.

## Recording

No video has been captured for this submission. To record one:
1. Start a screen recorder.
2. Run `python scripts/snapshot_scenario.py` in a terminal. The whole run prints in about 10 seconds and ends with `RESULT: PASS`.
3. For the two-sided view, run `python scripts/start_local.py` instead. It opens the demo console at `http://127.0.0.1:8765/demo`; then walk the flows. To show the no-slot escalation, add a reefer driver (the demo warehouses have no reefer dock) and report a delay.

Any recording link must be added only after it exists.
