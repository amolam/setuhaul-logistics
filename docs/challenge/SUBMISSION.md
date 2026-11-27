# FDE Academy submission: SetuHaul Logistics

**Name:** Amol Mithari

**Repo link:** https://github.com/amolam/setuhaul-logistics (public). The submission is on branch `feat/fde-challenge-submission`, which becomes `main` once merged.

**Live demo link:** none public. The deployed synthetic AWS pilot uses a private staff console (Cognito sign-in with two-step verification) and a Telegram test bot limited to two allowlisted test drivers, so there is no URL an evaluator can open. The prototype runs locally with one command (item 3).

**Stack used:**
- Python 3.14: FastAPI, Pydantic, psycopg 3.
- PostgreSQL 18: roles, row-level security, SECURITY DEFINER functions, and an EXCLUDE constraint for dock time.
- A deterministic Python orchestrator, with fixed reply templates.
- An AI reader, Claude Haiku 4.5 via OpenRouter, with a strict schema. A rules reader is the fallback.
- A deterministic scheduling planner, with no solver library.
- AWS pilot (beyond the brief): Aurora PostgreSQL (Data API), Lambda, SQS FIFO, Step Functions, Cognito, CDK, LangSmith tracing over REST, and the Telegram Bot API.
- Exact versions: the root [`README.md`](../../README.md#technology-stack-and-pinned-versions).

---

### 1. Problem Definition Sheet and data audit

**Links:**
- [`docs/1-frame/01_problem_definition_sheet.md`](../1-frame/01_problem_definition_sheet.md)
- [`docs/1-frame/audit_results.md`](../1-frame/audit_results.md): 30 checks, each with an executable SQL query and its actual rows, run read-only on the supplied SQLite snapshot.
- To reproduce: `python scripts/frame/run_audit.py`.

**Top data issue found:**
- The database's two integrity rules protect **slot rows, not dock time** (A30). Four active appointments need longer than their 60-minute slot: APT1014A, APT1002, APT1005 and APT1009 need 70–75 minutes (A04). Each spills into the next slot, which another driver can hold.
- On top of that, the seed already contains:
  - a historical double booking (A12);
  - a confirmed booking on a dock that has a breakdown (A08);
  - two overweight bookings (A03).
- So "the slot row is free" is not the same as "the dock is free". The build therefore moves integrity to dock time: `dock_occupancy`, guarded by the EXCLUDE constraint `ex_one_truck_per_dock`. When the snapshot is loaded through the real constraints, the database refuses exactly the four known-bad bookings: APT1004, APT1005, APT1014A and APT1015A.

### 2. Decision Register

**Link:** [`docs/2-design/01_decision_register.md`](../2-design/01_decision_register.md), D1–D13, each with an answer, a reason, evidence and a status.

**How competing requests are ordered:**
- **First valid commit wins, decided by the database.** Every driver's options come from the same state. A driver's explicit YES to a read-back lets the service call `hold_option()`. That function queues requests for the same dock behind a per-dock lock, and the EXCLUDE constraint is the final guard.
- **Outcome for the drivers.** Exactly one driver gets the slot. Every other driver is told *"That slot was just taken"* and gets fresh options in the same reply. Typing speed carries no business priority.
- **Business priority is applied by people.** A planner approves a request, or an eligible AUTO rule confirms it. The facility scheduling tool only proposes a priority-weighted sequence, and an ops lead applies each move, re-checked by the database.

### 3. Prototype

**One-command startup**, after `pip install -r requirements-dev.txt`, with Python 3.14 and PostgreSQL 18 binaries installed:

```bash
python scripts/start_local.py
```

This creates and starts a private local PostgreSQL cluster, applies all migrations, seeds a synthetic demo and serves the demo console at http://127.0.0.1:8765/demo. It uses the rules reader, so there is no API key and no cost. `python scripts/start_local.py --stop` stops the database. The prerequisites and the tested steps are in the root [`README.md`](../../README.md#run-it-locally).

### 4. Scenario run

**Video:** none recorded. Reproducible scripts:
- [`docs/challenge/SCENARIO_RUN.md`](SCENARIO_RUN.md)
- `python scripts/snapshot_scenario.py`
- `python -m pytest -q tests/test_scenario_run.py tests/test_snapshot_scenario.py`

**Which case escalates and why:**
- **SHP1015 on the supplied snapshot (THR005, 16:24 on 4 Aug 2026).** The driver writes *"Evening traffic. ETA 6:30. Can the reefer unload tonight?"*
  - The load is refrigerated, and Jaipur's only reefer dock, D5, is under maintenance from 18:00 to 22:00.
  - The truck cannot be ready before 17:55 and needs 70 minutes of dock time, so no feasible slot exists.
  - The agent tells the driver *"A coordinator has your details and will reply within 10 minutes"*. The database opens a `NO_FEASIBLE_SLOT` escalation (mode `DECIDE`, queue `FAC-JAI-01`, priority `HIGH`, acknowledgement SLA 10 minutes) with a complete hand-off record. No slot is invented.
- **Synthetic Run A.** All six brief items (plus optional item 7) end in the same kind of escalation, for a truck no Jaipur dock can carry.

### 5. Integrity test

**Link:** [`docs/challenge/INTEGRITY_PROOF.md`](INTEGRITY_PROOF.md). Command: `python scripts/integrity_proof.py 10`. It runs the full path: signed webhook, booking service, `hold_option()`, then the constraint.

| Run | Requests sent | Bookings made (slot claims) | Told "slot taken" | Double bookings |
|---|---:|---:|---:|---:|
| N = 10 (27 Nov 2026) | 10 | 1 | 9 | **0** |
| N = 20 (23 Nov 2026) | 20 | 1 | 19 | **0** |
| N = 50 (pytest `test_fifty_drivers_race_for_one_slot`) | 50 | 1 | 49 | 0 (asserted through the statuses) |

A direct insert of an overlapping claim as the database superuser is refused by the constraint (`23P01`).

### 6. Client readout

**Link:** [`docs/challenge/CLIENT_READOUT.md`](CLIENT_READOUT.md), one page: booking policy, MANUAL/AUTO, escalation, no-show and rebooking, limits, risks and decisions.

### Optional scheduling tool

**Links:**
- [`docs/3-build/SCHEDULING_TOOL.md`](../3-build/SCHEDULING_TOOL.md)
- [`setuhaul/domain/scheduling.py`](../../setuhaul/domain/scheduling.py)
- comparison: `python scripts/scheduling_compare.py`

It is a deterministic three-pass scoring planner (`w·wait + 2w·lateness + 30·changed`). Trucks already unloading, dock blocks, live holds and pending requests are fixed tasks it never moves. The results are reported as found:

| Case | Result |
|---|---|
| Early, late and in-progress trucks | It keeps 4 promises, against 0 and 1 for the simple rules, and changes 1, against 5 and 4. It loses on waiting |
| The Jaipur snapshot | **Earliest-ETA beats it** on total score and waiting |

---

## What does not work yet

**Not shown or not available**
- **No video** of the scenario run was recorded.
- **No public live demo.** The pilot console is private.
- **The booking path cannot run on the snapshot.** The supplied snapshot is dated 4 Aug 2026, and a hold then would be a booking in the past for the database. Booking under contention is shown on synthetic data (Run A and the integrity proof).
- **Three brief measures are not measured:**
  - average driver waiting from gate-in to dock-in;
  - warehouse slot utilisation;
  - a count of priority-policy violations.
  The scenario runs contain no gate-in or dock-in (see [`evidence/scenario_measures.md`](evidence/scenario_measures.md)).

**Limits of what is built**
- **Scheduling:**
  - the planner is greedy, with no look-ahead, and loses to earliest-ETA on the real snapshot;
  - there is no event-driven recalculation (proposals are on demand);
  - there is no atomic multi-move, so swaps go to a person;
  - there is no console page for proposals (only the API);
  - the `max_yard_wait_min` fairness guard is not enforced.
- **ETA:** no GPS, telematics, FASTag or SIM feed is connected. The fusion code supports them, but ETAs come from the plan, the driver, staff and gate-in. Gate arrival is entered by hand.
- **Dispatcher workflow:** one slot per dispatcher offer. Drivers cannot request a cancellation of a confirmed booking. A driver does not confirm a dispatcher's time change.
- **AI reader:** spend is capped daily; at the cap the rules reader takes over, and it understands less varied phrasing. The paid evaluation of the date and reschedule prompt (p6) is deferred pending owner approval. The Hindi and Hinglish date replies await native-speaker review.

**External dependencies**
- **Real WhatsApp** needs a Meta test number and approved message templates. The pilot uses a Telegram test bot for two allowlisted test drivers.
- **No AWS load test** has been run at the brief's spike (35 messages in 30 minutes) or at 10× that. Double-booking safety is proven locally.
- **The pilot database is PostgreSQL 17.9**, a pilot-only exception (T33-P). Production needs 18.

**Outstanding client decisions** (`docs/1-frame/01_problem_definition_sheet.md` §4.4; decision register "Open items")
- warehouse confirmation deadlines per shift, and night coverage at Gurugram (U1, U5);
- priority weights and the default planner rule;
- a written policy before AUTO is enabled at any facility;
- changeover time and the meaning of a driver's latest finish (U3, U4);
- whether the source systems will fix the data-quality issues the audit found (double assignments, overweight bookings, dock status that disagrees with dock events).

---

## Verification for this submission (27 Nov 2026)

Every command below was run on this branch, against a fresh local PostgreSQL 18.6 cluster. There were no AWS calls and no paid model calls.

| Check | Command | Result |
|---|---|---|
| Data audit regenerates identically | `python scripts/frame/run_audit.py` | `audit_results.md` unchanged (`git diff` empty). Snapshot sha256 `c02806e6…3f90700`, unchanged |
| Audit re-executed independently on the original data package | every SQL block in `audit_results.md`, run read-only on the `.db` extracted from the supplied zip | 30 / 30 row counts match |
| Snapshot scenario | `python scripts/snapshot_scenario.py --record` | PASS 6/6 |
| Integrity proof, N = 10 | `python scripts/integrity_proof.py 10` | PASS 9/9 |
| Targeted tests | see the root README; results below | RESULTS_PLACEHOLDER |
| Clean-environment start | CLEAN_PLACEHOLDER | CLEAN_RESULT_PLACEHOLDER |

## Requirement-to-evidence checklist

| Brief requirement | Evidence | State |
|---|---|---|
| Facts, estimates, assumptions, unknowns | Problem Definition Sheet §4 | Implemented and evidenced |
| Every data issue proved by a query on the supplied snapshot | `audit_results.md` (30 checks), re-verified 30/30 | Implemented and evidenced |
| All 13 decisions: answer, reason, evidence | Decision register D1–D13 | Implemented and evidenced. D3 signal feeds and D8 event-driven recalculation are design-only, as stated there |
| Conversation layer, tools, state | `setuhaul/agent/`, `setuhaul/api/service.py`, migrations | Implemented and evidenced |
| Library versions named in the README | Root README table | Implemented |
| Startup | `scripts/start_local.py` | Implemented; see the verification table |
| Scenario items 1–6, ending in escalation | Run A (`tests/test_scenario_run.py`), Run B (snapshot) | Implemented and evidenced |
| Scenario item 7 (optional) | Run A item 7 | Implemented and evidenced |
| Competing requests, no double booking | Integrity proof, N = 10, 20 and 50 | Implemented and evidenced |
| Short client note: policy, limits, risks | `CLIENT_READOUT.md` | Implemented |
| Scheduling tool with comparison (optional) | `SCHEDULING_TOOL.md`, `scheduling_compare.py` | Implemented and evidenced |
| Brief measures | `evidence/scenario_measures.md` | Partially: 6 of 9 measured; 3 explicitly not measured |
| Boundaries: the agent never decides alone | Tool allow-list, booking switch, DB functions, `tests/test_agent_not_operator.py` | Implemented and evidenced |
