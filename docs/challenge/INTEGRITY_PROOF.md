# Integrity proof: competing requests cannot double-book a dock

*Hand-in item 5. Brief decision 13: "How will you prove the system did not double-book capacity? A concurrency
test and its result."*

## In one paragraph (for the head of operations)

We set up a warehouse with **one dock** and gave **20 truck drivers** the same offer: dock D1, 07:00–07:55 on
24 Nov. Every driver replied "haan ji, kar do" (yes, book it), and the system recorded each YES as a real driver
confirmation. Then we released all 20 "hold this slot for me" requests **at the same instant**. All 20 were in
flight within 5 milliseconds of each other, and none had come back before the last one was sent.
**Exactly one driver got the slot. The other 19 were told at once that the slot had just been taken, and each
got three other times to choose from. None of those times overlaps the taken slot.** Afterwards we read the
database directly: one claim on that dock window, and zero overlapping claims on any dock. Finally we tried to
force a second booking into the same window as the database administrator, skipping the application entirely.
The database refused it. Result: **PASS, 9 of 9 checks**. The same holds for 50 drivers in the automated test
suite.

## Results at a glance

Every number below is copied from a recorded output file. Each run used a fresh local PostgreSQL 18.6 database and the full production booking path.

| Run | Requests sent | Got the slot (hold, then pending request) | Told "slot taken" (`LOST`) | Other outcomes | Double bookings (overlapping active claims, whole DB) | Superuser bypass | Checks | Output |
|---|---:|---:|---:|---:|---:|---|---|---|
| **N = 10**, 27 Nov 2026, on the submission branch | 10 (all in flight before the first answer) | 1 | 9 | 0 | **0** | refused, `23P01` | PASS 9/9 | [`evidence/integrity_proof_n10.txt`](evidence/integrity_proof_n10.txt) |
| **N = 20**, 23 Nov 2026 | 20 (all in flight before the first answer) | 1 | 19 | 0 | **0** | refused, `23P01` | PASS 9/9 | [`evidence/integrity_proof.txt`](evidence/integrity_proof.txt) |
| **N = 50**, automated test | 50, behind one barrier | 1 | 49 | 0 | **not counted in the database by this test**. It checks the service's answers: exactly 1 OK and 49 LOST, every loser with fresh options | — | pytest | `tests/test_api.py::test_fifty_drivers_race_for_one_slot` |

The double-booking count is measured in the database (Q4 and the superuser bypass) only in the N = 10 and N = 20 runs. The N = 50 test shows that the service gives exactly one driver the slot.

The N = 10 run matches the brief's headline: *"ten drivers want the same dock slot"*.

## Why it cannot double-book

The AI agent never decides whether a slot is free. It can only *ask*. The decision is made in three layers of
code, and each layer is enough on its own:

1. **The service** checks that the driver really said YES to this exact option, and that it is the driver's
   latest word.
2. **The database function `hold_option()`** queues every request for the same dock behind a per-dock lock, so
   requests are judged one at a time.
3. **The database rule `ex_one_truck_per_dock`** (a PostgreSQL EXCLUDE constraint on `dock_occupancy`) makes two
   active claims on overlapping times at the same dock impossible to store, whoever writes them. Holds, bookings,
   breakdown blocks and trucks already in the dock all live in that one table. Source:
   `migrations/0001_core_schema.sql`.

## The run

Run on 23 Nov 2026 at 00:33 IST (22 Nov 19:03 UTC) against a fresh local database on PostgreSQL 18.6. Raw output:
`docs/challenge/evidence/integrity_proof.txt`.

### 1. Initial state

| Item | Value |
|---|---|
| Facility | FAC-JAI-01 (Jaipur), with **1 dock**: DOCK-JAI-D1 |
| The slot everyone wanted | Dock D1, **24 Nov 07:00–07:55 IST** (unload plus changeover) |
| Claims on that window before the race | **0** (the slot is free) |
| Drivers holding a confirmed YES for exactly this slot | **20** (20 different drivers, trucks and shipments) |

All 20 trucks had the same arrival time on purpose, so the planner offered every driver the same first slot. The
script refuses to run the race unless that is true.

### 2. The competing requests

- **How many:** 20 hold requests, one per driver.
- **From whom:** 20 distinct drivers. Each request came through the production path: signed WhatsApp webhook →
  booking service → `hold_option()` database function → constraint.
- **How they were launched:** 20 threads, each with its own HTTP client and its own database connection, all
  waiting on one barrier and released together.
- **Proof they overlapped:** all 20 started within **4.9 ms** of each other. The first answer came back after
  **1,046 ms**. **20 of 20** requests were already sent before any answer arrived.

### 3. The rule we expect to hold

> At any moment, a dock time window has **at most one** active claim (hold or booking). Every other driver who
> asked for it gets a clear "taken" answer and alternatives, never a silent failure and never a second booking.

### 4. What happened

| Outcome | Drivers |
|---|---:|
| Got the slot (`OK / HELD`) | **1** (shipment SHP-14-d5de25) |
| Told the slot was taken (`LOST`) | **19** |
| Anything else (error, timeout) | **0** |

**What each losing driver is told.** This is the orchestrator's fixed template from `setuhaul/agent/replies.py`,
filled with the fresh options the service returned to one of the losers:

```
That slot was just taken.
Available slots:
1. Dock D1, 8:00 AM on 24 Nov
2. Dock D1, 9:00 AM on 24 Nov
3. Dock D1, 10:00 AM on 24 Nov
Reply with the number you want.
```

A Hinglish driver gets "Wo slot abhi kisi aur ne le liya." with the same options. In total the 19 losers got 57
fresh options, and **0** of them overlap the taken window.

The winner then sent the booking request to the warehouse: `OK PENDING APT-44356f049ac1484ea8e0b2f13194ed8e`.

### 5. What the database actually holds

Right after the race, before anything else happened:

```
id=1 kind=HOLD shipment=SHP-14-d5de25 07:00-07:55 active=True hold_expires=00:36:54
```

After the winner's booking request. This lists **every** row touching the window, active or not. The 19 losing
attempts left no row at all:

```
id=1 dock=DOCK-JAI-D1 kind=APPOINTMENT shipment=SHP-14-d5de25 07:00-07:55 active=True
```

| Check (SQL against the database) | Result |
|---|---|
| Q1 Shipments with an active claim on that dock window | **1** |
| Q2 Active HOLD rows on that window (after the hold became an appointment) | **0** |
| Q3 Active appointments among the 20 racing shipments | **1**: APT-44356f…, SHP-14-d5de25, PENDING_CONFIRMATION |
| Q4 Pairs of active claims by *different* shipments that overlap on **any** dock, whole database | **0** |
| Q5 Fresh options given to losers that overlap the taken window | **0 of 57** |

### 6. Trying to force it

As the PostgreSQL superuser, which ignores the app, row-level security and permissions, we inserted a second
APPOINTMENT for a losing driver into the same window:

```
REFUSED sqlstate=23P01 conflicting key value violates exclusion constraint "ex_one_truck_per_dock"
```

### 7. Result

```
[PASS] exactly one of 20 hold requests succeeded
[PASS] the other 19 were told LOST (slot taken)
[PASS] every loser received fresh options in the same answer
[PASS] no fresh option offered to a loser overlaps the taken window
[PASS] one shipment holds the dock window in the database
[PASS] at most one active HOLD on the window
[PASS] exactly one active appointment among the racers (the winner's)
[PASS] zero overlapping claims of different shipments on any dock
[PASS] direct superuser insert of an overlapping claim is refused (23P01)

RESULT: PASS  (9/9 checks)  elapsed 38.9 s
```

## Corroborating evidence

| Evidence | What it shows | Result |
|---|---|---|
| `tests/test_api.py::test_fifty_drivers_race_for_one_slot` (run 23 Nov 2026, `evidence/test_api.xml`) | 50 drivers, one slot, barrier through the HTTP API. Asserts `statuses.count("OK") == 1 and statuses.count("LOST") == 49` and that every loser has `fresh_options` | PASS (191.7 s) |
| `tests/test_approvals.py::test_races_resolve_to_exactly_one_outcome[block]` | Approval racing a dock block: if approved first, the block hits the booking (23P01). Never both | PASS |
| `tests/test_concurrency.py` (14 tests) | A driver's YES, a later "cancel", a takeover or a shipment switch racing a hold always land in the safe order | 14 passed |
| `docs/2-design/validation_results.md` §2–3 (18 Nov, `validate_schema.py`, historical, not re-run) | 10×30, 50×20 and 200×10 parallel holds: one winner every round, 0 other errors. 150 random overlapping windows: 0 overlaps | PASS |
| `demo/server.py` `/demo/race` | The same race through the full WhatsApp → orchestrator path, for a live demonstration. It reports the overlap count from the database | Manual (not run for this report) |

---

## Engineering appendix

### Reproduce

```bash
export SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres"   # or: python scripts/start_local.py --db-only
python scripts/integrity_proof.py 10        # the brief's ten drivers (output: evidence/integrity_proof_n10.txt)
python scripts/integrity_proof.py 20        # N is optional (default 20); exit 0 only if every check passes
python -m pytest tests/test_api.py::test_fifty_drivers_race_for_one_slot -rA
```

`scripts/integrity_proof.py` drops and recreates the database `setuhaul_integrity_proof`. It applies every
migration with `setuhaul.db.migrate.migrate`, then seeds as `tests/conftest.py` does (carrier, facility,
`facility_config`, one STANDARD dock, 72 hourly `appointment_slots`). It adds N drivers, trucks and shipments with
an identical fixed ETA, 30 h out at :20, so all N are offered the same first option.

### Path exercised (no mocks in the booking path)

```
POST /webhooks/whatsapp (HMAC-signed)           -> setuhaul_ingest: store DRIVER message, attestation
POST /tools/find_feasible_options               -> feasibility engine, shown_options (setuhaul_feasibility)
POST /tools/select_option                       -> record_read_back()
POST /webhooks/whatsapp "haan ji, kar do"       -> confirmation attestation (trusted inbound path)
POST /tools/confirm_selection                   -> verdict YES
--- barrier (N threads, N TestClients, N psycopg connections via Db.tx) ---
POST /tools/hold_option                         -> BookingService.t_hold_option
                                                -> SELECT hold_option(option, thread, attestation, idem_key, ...)
                                                   -> verify_attestation -> hold_option_core
                                                   -> pg_advisory_xact_lock(hashtext(dock_id))
                                                   -> INSERT dock_occupancy  (EXCLUDE ex_one_truck_per_dock)
   loser: psycopg error 23P01 -> map_db_error -> "LOST" -> fresh options in the same response
```

The app runs with `booking_enabled=True, local_test_harness=True`, the same settings as `tests/test_api.py`. That
lets the booking path run on the in-process queue without the six deployment gates. It does not change any
booking or database logic.

### SQL behind Q1–Q5

```sql
-- Q1
SELECT count(DISTINCT shipment_id) FROM setuhaul.dock_occupancy
 WHERE active AND dock_id = 'DOCK-JAI-D1' AND during && tstzrange(:lo, :hi);
-- Q2
SELECT count(*) FROM setuhaul.dock_occupancy
 WHERE active AND kind = 'HOLD' AND dock_id = 'DOCK-JAI-D1' AND during && tstzrange(:lo, :hi);
-- Q3
SELECT appointment_id, shipment_id, status FROM setuhaul.appointments
 WHERE shipment_id = ANY(:racers) AND status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');
-- Q4 (whole database, every dock)
SELECT count(*) FROM setuhaul.dock_occupancy a JOIN setuhaul.dock_occupancy b
  ON a.dock_id = b.dock_id AND a.occupancy_id < b.occupancy_id AND a.during && b.during
 AND a.shipment_id IS DISTINCT FROM b.shipment_id
 WHERE a.active AND b.active;
-- Q5
SELECT count(*) FROM setuhaul.shown_options
 WHERE option_id = ANY(:fresh_ids) AND dock_id = 'DOCK-JAI-D1' AND during && tstzrange(:lo, :hi);
```

Q4 allows rows of the *same* shipment to overlap on purpose. The schema permits an at-risk booking and its
replacement hold to coexist. It counts only overlaps between different shipments, which is what a double booking
is.

### Mechanisms (where the guarantee lives)

| Guarantee | Mechanism | Location |
|---|---|---|
| Two active claims on one dock can never overlap | `EXCLUDE USING gist (dock_id WITH =, during WITH &&) WHERE (active)` | `migrations/0001_core_schema.sql`, `dock_occupancy` |
| A truck can't be on two docks at once for different shipments | `ex_one_dock_per_truck`. `vehicle_id` is set by a trigger from the shipment, so callers cannot spoof it | same |
| One active hold per shipment (no hoarding) | `ux_one_active_hold_per_shipment` | same |
| Racing writers lose cleanly (23P01) instead of deadlocking (40P01) | `pg_advisory_xact_lock(hashtext(dock_id))` in the hold path | `place_hold` / `hold_option_core` |
| Only code holds: the agent cannot call the core functions or write `dock_occupancy` | Grants plus `SECURITY DEFINER` wrappers. `test_agent_not_operator.py` allow-lists | `migrations/0002`–`0004`, `tests/test_agent_not_operator.py` |
| The hold requires the driver's own, latest YES for this option | `verify_attestation()` | `migrations/0004_step2_review_fixes.sql` |

### Limits of this proof

- Run locally on PostgreSQL 18.6, not on Aurora through the Data API. The same SQL and constraint are deployed
  (migrations are checksummed, see `tests/test_migrations.py`). The live pilot runs with the booking switch OFF,
  so no live race has been run (`option_a.json` A2: "no appointment").
- The race goes through `/tools/*`, the orchestrator's tool API. It does not go through the model-driven
  orchestrator. `/demo/race` and `test_replay.py` cover the orchestrator, and the model only *requests* tools.
