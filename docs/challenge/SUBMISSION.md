# FDE Academy submission: SetuHaul Logistics

**Name:** Amol Mithari

**Repo link:** https://github.com/amolam/setuhaul-logistics (public). The submission is on branch `main` (merged from `feat/fde-challenge-submission` by PR #58 on 27 Nov 2026).

**Live demo link:** none public. The deployed synthetic AWS pilot uses a private staff console (Cognito sign-in with TOTP MFA required) and live Telegram limited to two owner-authorised synthetic test drivers, so there is no URL an evaluator can open. The prototype runs locally with one command (item 3).

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
| N = 50 (pytest `test_fifty_drivers_race_for_one_slot`) | 50 | 1 OK | 49 LOST | **not counted** in the database by this test |

The zero-double-booking result is **measured in the database only for N = 10 and N = 20**. Each run counts overlapping active claims across the whole database with plain SQL (0), and a direct insert of an overlapping claim as the database superuser is refused by the constraint (`23P01`). The N = 50 test checks the service's answers (exactly 1 OK, 49 LOST, every loser with fresh options); it does not count claims in the database.

### 6. Client readout

**Link:** [`docs/challenge/CLIENT_READOUT.md`](CLIENT_READOUT.md), one page: what it does, what is automated, booking policy, what stays with people, limits, and risks and decisions.

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
- **Ladder levels 3, 4 and 5 are not run on the supplied snapshot's named cases.** The snapshot run covers levels 1, 2 and 6. Rules and compatibility (level 3), competing trucks (level 4) and a mid-conversation change (level 5) are shown on synthetic data (Run A, the integrity proof and the test suite), and as worked examples on the snapshot (`docs/1-frame/worked_examples.md`).
- **Both scenario runs use the deterministic rules reader, not the AI model.** No model reads a message in the recorded evidence; the model reader is evaluated separately (`evals/`).
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
- **Real WhatsApp** needs a Meta test number and approved message templates. The pilot uses live Telegram for two owner-authorised synthetic test drivers. WhatsApp outbound is off, and inbound WhatsApp delivery in the pilot is not verified (root `README.md`, *Live synthetic pilot*).
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

Every command below was run on branch `feat/fde-challenge-submission` before it was merged, against a fresh local PostgreSQL 18.6 cluster. There were no AWS calls and no paid model calls.

| Check | Command | Result |
|---|---|---|
| Data audit regenerates identically | `python scripts/frame/run_audit.py` | `audit_results.md` unchanged (`git diff` empty). Snapshot sha256 `c02806e6…3f90700`, unchanged |
| Audit re-executed independently on the original data package | every SQL block in `audit_results.md`, run read-only on the `.db` extracted from the supplied zip | 30 / 30 row counts match |
| Snapshot scenario | `python scripts/snapshot_scenario.py --record` | PASS 6/6 |
| Integrity proof, N = 10 | `python scripts/integrity_proof.py 10` | PASS 9/9 |
| Targeted tests | `python -m pytest -q tests/test_scenario_run.py tests/test_snapshot_scenario.py tests/test_api.py::test_fifty_drivers_race_for_one_slot tests/test_dispatch_offers.py::test_two_shipments_race_for_the_last_slot_one_wins tests/test_flows.py::test_f2_rejected_request_releases_the_slot_and_the_driver_can_ask_again tests/test_pg_view.py tests/test_feasibility.py tests/test_scheduling.py tests/test_move_appointment.py tests/test_eta.py tests/test_concurrency.py tests/test_traceability.py` | **149 passed** in 235 s |
| Clean-environment start | Fresh `git clone` of this branch; new Python 3.14.7 venv; `pip install -r requirements-dev.txt`; `python scripts/start_local.py --port 55488`. The local cluster was created from nothing | Demo console HTTP 200 at `/demo` after about 6 s, with 5 seeded drivers. A reefer driver's message led to a `NO_FEASIBLE_SLOT` / `DECIDE` / `OPEN` escalation and the coordinator reply. `--stop` stopped the database |
| Clean-environment reproduction | In the same clone: `scripts/snapshot_scenario.py`, `scripts/integrity_proof.py 10`, `scripts/frame/run_audit.py` | PASS 6/6; PASS 9/9 (OK = 1, LOST = 9, 0 overlaps); audit regenerated with no diff |
| `start_local.py` safety | `python -m pytest -q tests/test_start_local.py`, plus `python scripts/start_local.py` on a machine where another PostgreSQL holds port 55432 | 4 passed: a server the script does not own (PostgreSQL or not) is refused with exit 2 and left with the same databases and oids; the private cluster starts, is reused on its port, is refused promptly on another port (the real port is named), stops, and stops again. On the occupied port: exit 2, the other server's version and data directory named, nothing created |

**CI on the final verified commit of PR #58, `7824f30`** ([run 46045442429](https://github.com/amolam/setuhaul-logistics/actions/runs/46045442429); Ubuntu, Python 3.13, PostgreSQL 17.9):

| CI job | Result |
|---|---|
| Full test suite | **2,233 passed, 1 skipped** |
| Database tests through the Data API backend (emulated) | **749 passed** |
| Synth | passed |
| Deploy | skipped, as designed (manual, approval-gated) |

**`tests/test_start_local.py` ran in this CI run and was not skipped.**
- The PostgreSQL server binaries were present on the runner. In the previous run, 46044412665 on `ad7a89c`, the private-cluster test ran against `/usr/lib/postgresql/16/bin` and caught an Ubuntu socket-directory bug, which `7824f30` fixes.
- In run 46045442429, all 4 tests in the file passed. Against the earlier run 46040343715 (on `479e253`, before the file existed: 2,229 passed, 1 skipped), the suite grew by exactly 4 passes, and the single skip is unchanged: it is the by-design Data API skip.

**Not run for this submission:** anything against AWS, and any paid model evaluation.

## Fresh local re-run (28 Nov 2026)

The recorded evidence above is the historical record (23 and 27 Nov 2026). On 28 Nov 2026 the same evidence was reproduced on `main` (`700a84b`), on a brand-new private local PostgreSQL 18.6 cluster, with no AWS call and no paid model call. New output only was written, to [`evidence/rerun_20261128/`](evidence/rerun_20261128/README.md); no earlier evidence file was changed.

| Evidence | Fresh result, 28 Nov 2026 |
|---|---|
| Data audit | 30 of 30 checks executed; `audit_results.md` regenerated byte for byte, so all row counts match |
| Scenario, six items (Run A) | Test passed; each of the six brief items is asserted, and the run ends in the escalation |
| Scenario, supplied snapshot (Run B) | PASS 6/6; SHP1015 escalates `NO_FEASIBLE_SLOT` |
| Integrity, N = 10 | PASS 9/9: 1 winner, 9 `LOST`, 0 double bookings counted in the database |
| Integrity, N = 20 | PASS 9/9: 1 winner, 19 `LOST`, 0 double bookings counted in the database |
| Integrity, N = 50 | Test passed: 1 `OK`, 49 `LOST`, fresh options for every loser. Service answers only; no database count |

**Four kinds of evidence, kept apart:** historical local evidence (23 and 27 Nov), this fresh local re-run (28 Nov), CI evidence (run 46045442429), and the pilot's status, which is owner-confirmed on 28 Nov 2026 and was not re-checked in AWS for this submission.

## Requirement-to-evidence checklist

| Brief requirement | Evidence | State |
|---|---|---|
| Facts, estimates, assumptions, unknowns | Problem Definition Sheet §4 | Implemented and evidenced |
| Every data issue proved by a query on the supplied snapshot | `audit_results.md` (30 checks), re-verified 30/30 | Implemented and evidenced |
| All 13 decisions: answer, reason, evidence | Decision register D1–D13 | Implemented and evidenced. D3 signal feeds and D8 event-driven recalculation are design-only, as stated there |
| Conversation layer, tools, state | `setuhaul/agent/`, `setuhaul/api/service.py`, migrations | Implemented and evidenced |
| Library versions named in the README | Root README table | Implemented |
| Startup | `scripts/start_local.py` | Implemented, and verified from a clean clone |
| Scenario items 1–6, ending in escalation | Run A (`tests/test_scenario_run.py`), Run B (snapshot) | Implemented and evidenced |
| Scenario item 7 (optional) | Run A item 7 | Implemented and evidenced |
| Competing requests, no double booking | Integrity proof, N = 10, 20 and 50 | Implemented and evidenced |
| Short client note: policy, limits, risks | `CLIENT_READOUT.md` | Implemented |
| Scheduling tool with comparison (optional) | `SCHEDULING_TOOL.md`, `scheduling_compare.py` | Implemented and evidenced |
| Brief measures | `evidence/scenario_measures.md` | Partially: 6 of 9 measured; 3 explicitly not measured |
| Boundaries: the agent never decides alone | Tool allow-list, booking switch, DB functions, `tests/test_agent_not_operator.py` | Implemented and evidenced |
