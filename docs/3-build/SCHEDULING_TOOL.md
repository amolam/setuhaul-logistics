# Facility scheduling tool and `move_appointment`

Brief p.10 (optional): *the agent handles one driver; a scheduling tool looks at one facility and all its trucks
together; the agent calls it as a controlled tool; it reads structured data, never free text; it returns a proposed
sequence or ranked options.* This document records what was built, the objective and score, the results as printed
(including where a simple rule wins), and how a proposal becomes a booking change only through a person and the
database.

| Piece | Where |
|---|---|
| Planner (pure, deterministic, no library) | `setuhaul/domain/scheduling.py` |
| Agent tool `propose_facility_schedule` (read only) | `setuhaul/api/service.py` (`t_propose_facility_schedule`), allow-list in `setuhaul/domain/booking.py` |
| Console routes | `setuhaul/api/app.py`: `GET /console/facilities/{id}/schedule-proposal`, `POST /console/facilities/{id}/schedule-proposals`, `POST /console/appointments/{id}/move` |
| Database | `migrations/0025_schedule_proposals_and_move.sql`: `schedule_proposals`, `schedule_proposal_rows`, `record_schedule_proposal()`, `move_appointment()`, `enqueue_notifications()` (adds `AppointmentMoved`) |
| Comparison | `scripts/scheduling_compare.py` (offline: no database, network or model) |
| Tests | `tests/test_scheduling.py` (pure), `tests/test_move_appointment.py` (PostgreSQL, also in Data API mode) |
| Adversarial cases | `docs/challenge/break_scheduling_cases.md` |

## 1. Objective in plain words

Less waiting for the trucks that matter most, fewer missed appointments, and fewer changes to times drivers were
already told. When these pull against each other, the score below decides, and the proposal shows every term so a
person can see why.

## 2. The model (brief p.10 vocabulary)

| Brief | Here | Source of the value |
|---|---|---|
| truck = job | `Job` | one shipment heading to the facility, not cancelled, completed or in a dock |
| dock = machine | `Dock` | the facility's docks |
| latest ETA = release time | `Job.release` | the feasibility engine's own rule: effective ETA (ETA fusion) + confidence band + gate-to-dock, never before now |
| gate-in = available now | `Job.release`, `Job.arrived_at` | gate-in + gate-to-dock for a truck already through the gate |
| unload duration = processing time | `Job.processing_min` | expected unload + facility changeover (the same occupancy the engine books) |
| appointment = promised window | `Job.promise` | the current CONFIRMED appointment (dock, start, end) |
| truck already unloading = fixed task | `FixedTask` kind `IN_DOCK` | IN_PROGRESS appointments, FIXED occupancy, trucks in dock; also `BLOCK` (dock events), `HOLD` and `REQUEST_PENDING` (a driver mid-booking: the planner does not touch it) |
| priority = weight | `Job.weight` | CRITICAL 8, HIGH 4, NORMAL 2, LOW 1 (Frame weights) |

Compatibility, ETA, committed dock time, dock events and the double-assignment check all come from
`FeasibilityEngine` (`snapshot_from_data`), so the planner and the driver-facing option search cannot disagree about
what fits. A truck whose driver or vehicle is double-assigned (`CONTRADICTORY_DATA`) is never planned: it is listed
for a person, as the engine already escalates it.

Planning window: 12 hours rolling from now, inside each day's opening time, last new start time and close.

## 3. The score (lower is better), per truck

| Term | Formula | Why |
|---|---|---|
| waiting | `w × (start − release)` minutes | yard time, weighted by priority |
| lateness | `2 × w × max(0, start − promised start)` | a missed promise costs twice a minute of waiting |
| change | `30 × changed` | changed = different dock, or a start more than 15 minutes from the promise |
| unscheduled | `2000 × w` | always worse than any feasible placement; the truck goes to a person |

"Missed promise" = start later than promised start + the facility's no-show grace (30 min). These are the Frame spike's
objective and weights (`docs/1-frame/solver_results.md`); the spike showed CP-SAT is not needed at this size, so no
solver library is used.

## 4. The three rules

| Rule | Order | Placement |
|---|---|---|
| `FCFS` | trucks already through the gate first, by gate-in time; then trucks on the road by their **original** planned arrival (the TMS plan the booking was made from, not a later ETA); ties by shipment id | earliest free compatible dock time (ties: the promised dock, then dock code) |
| `EARLIEST_ETA` | by release time, then higher weight, then shipment id | same |
| `FACILITY_SCORE` (proposed) | pass 1 keep promises: every truck ready by its promised start + grace keeps its dock at max(promised start, ready), in promise order, if that dock time is free; pass 2 dispatch the rest (no booking, promise already lost, promised dock blocked or taken): at each step the earliest dock time any of them can use, and of the trucks competing for it the highest weight first (then earliest release, then id); each goes where its score is lowest; pass 3 pull early trucks forward: a pass-1 truck moves to an earlier free dock time only if its own score drops (the waiting saved, w × minutes, outweighs the 30-point change) | lowest score |

Every rule: a KEPT promise keeps its current claim in the database (nothing is applied for it), so the planner
reserves the union of its planned interval and that claim; nothing else is planned into it.

### Apply order (found while testing)

A proposal is a whole plan, but moves are applied one at a time and each must find its target free in the database.
A target can overlap the current claim of another truck the same proposal also moves. The first end-to-end run hit
exactly this (`SLOT_TAKEN` from the database, correctly). Each MOVE now carries `depends_on` (moves to apply first)
and `apply_order` (a deterministic topological order). Trucks in a cycle (a swap) get no order: single moves cannot
do a swap, so a person handles it or re-plans after one of them moves (`moves_in_a_cycle` in the metrics).

## 5. Results (actual output of `python scripts/scheduling_compare.py`)

### 5.1 Scenario: an early truck, a late truck and a truck in progress at one facility

Facility FAC-DEMO, now 09:00, three standard docks, open 06:00–22:00, last new start 21:00, grace 30 min.

| Shipment | Priority (w) | Ready | Processing min | Promised | Arrived |
|---|---|---|---|---|---|
| SHP-EARLY | NORMAL (2) | 09:00 | 50 | D2 10:30 | 08:45 |
| SHP-C | LOW (1) | 09:05 | 40 | — | — |
| SHP-A | NORMAL (2) | 09:10 | 60 | D2 09:15 | — |
| SHP-D | NORMAL (2) | 09:20 | 60 | D3 09:30 | — |
| SHP-LATE | HIGH (4) | 10:35 | 60 | D1 09:45 | — |
| SHP-B | CRITICAL (8) | 10:40 | 70 | D1 10:50 | — |

Fixed: D1 08:20–09:40, IN_DOCK, APT-UNLOADING (the truck already unloading).

| Rule | weighted_wait | total_wait | max_wait | missed_promises | changed_promises | kept_promises | unscheduled | moves | moves_in_a_cycle | score |
|---|---|---|---|---|---|---|---|---|---|---|
| FCFS | 95 | 65 | 35 | 1 | 5 | 0 | 0 | 5 | 3 | 725 |
| EARLIEST_ETA | 112 | 56 | 30 | 1 | 4 | 1 | 0 | 4 | 3 | 796 |
| FACILITY_SCORE | 185 | 110 | 75 | 1 | 1 | 4 | 0 | 1 | 0 | 615 |

Orderings (dock, start, action; full tables with every score term and reason are printed by the script):

| Shipment | FCFS | EARLIEST_ETA | FACILITY_SCORE |
|---|---|---|---|
| SHP-EARLY | D2 09:00 MOVE (swap cycle) | D2 09:00 MOVE (swap cycle) | D2 10:15 KEEP (waits 75 min) |
| SHP-C | D1 09:40 PLAN | D3 09:05 PLAN | D1 09:40 PLAN |
| SHP-A | D3 09:10 MOVE (swap cycle) | D1 09:40 MOVE (swap cycle) | D2 09:10 KEEP |
| SHP-D | D2 09:50 MOVE (swap cycle) | D3 09:46 MOVE, apply 1 | D3 09:20 KEEP |
| SHP-LATE | D1 10:35 MOVE, apply 2 after SHP-B | D2 10:35 MOVE (swap cycle) | D3 10:35 MOVE, apply 1 |
| SHP-B | D3 10:40 MOVE, apply 1 | D1 10:40 KEEP | D1 10:40 KEEP |

What happens to each kind of truck:

* **In progress**: fixed under every rule. Nothing is placed on D1 before 09:40.
* **Late** (promised D1 09:45, ready 10:35): its promise is missed under every rule (the only missed promise). The
  score rule gives it the next free dock (D3 10:35) without taking anyone else's slot; FCFS gives it D1 and then has
  to move the CRITICAL truck off D1.
* **Early** (at the gate since 08:45, promised 10:30): FCFS and EARLIEST_ETA dock it at once (0 wait) and pay with
  other drivers' promises (SHP-A, SHP-D moved). The score rule keeps it at its slot (docks 10:15, 75 min wait): the
  only free dock time before then (D1 09:40) goes to the unbooked truck SHP-C in pass 2, and no remaining gap lowers
  the early truck's score.

Honest reading: **the simple rules win on waiting** (weighted wait 95 and 112 against 185; max wait 35 and 30 against
75). **The score rule wins on promises and on applicability**: 1 changed promise against 5 and 4, 4 kept against 0
and 1, total score 615 against 725 and 796, and its one move can be applied at once, while 3 of the FCFS and
EARLIEST_ETA moves form swap cycles that single moves cannot apply. Missed promises are equal (1).

### 5.2 Challenge snapshot: FAC-JAI-01 at 09:56 on 4 Aug 2026 (the Frame spike's instant)

14 trucks in the window; fixed: SHP1002 unloading in D2 (to 10:06), D3 blocked 09:15–13:00 (DEVT001), D5 blocked
from 18:00 (DEVT002), two drivers' pending requests (APT1013A, APT1014A). Five trucks are double-assigned and go to a
person under every rule; SHP1015 (reefer, D5 blocked) has no capacity in the window.

| Rule | weighted_wait | total_wait | max_wait | missed_promises | changed_promises | kept_promises | unscheduled | moves | moves_in_a_cycle | score |
|---|---|---|---|---|---|---|---|---|---|---|
| FCFS | 870 | 210 | 85 | 8 | 5 | 2 | 6 | 5 | 0 | 35922 |
| EARLIEST_ETA | 780 | 210 | 85 | 8 | 5 | 2 | 6 | 5 | 0 | 35592 |
| FACILITY_SCORE | 924 | 338 | 139 | 7 | 4 | 3 | 6 | 4 | 0 | 35954 |

Honest reading: on the real snapshot **EARLIEST_ETA has the best total score** (35592) and the least waiting; the
score rule keeps one more promise (the CRITICAL SHP1009 on D4 11:00, which both simple rules give to the late HIGH
truck SHP1005) and misses one fewer, but the two late trucks it displaces wait longer (SHP1005 124 min, SHP1004 139
min), so its weighted wait and total score are the worst of the three. The totals are dominated by the six
unscheduled trucks (2000 × w each), which all three rules leave to a person. The numbers differ from the Frame spike
(`solver_results.md`) because the spike planned the double-assigned trucks; here they are never planned.

What this means for the choice: pass 1 protects promises absolutely, which is right when changing a told time is
expensive (driver trust, calls) and wrong when one late high-priority truck would be better served by bumping a
promise. The 30-point change penalty and the "keep promises first" pass are the two levers; both are visible in the
score breakdown so the dock office can see the trade-off before applying anything.

## 6. Advantages and limitations

Advantages
* Transparent: every assignment carries its wait, lateness and change terms and a factual reason; the metrics are
  the same three things the objective names.
* Deterministic: total ordering with shipment-id tie-breaks; `input_fingerprint` = sha256 of the canonical input;
  tests prove equal input gives byte-equal output and that reordering the input changes nothing.
* Same facts as the driver path: ETA, compatibility, committed time and the double-assignment check come from the
  feasibility engine.
* Safe by construction: proposals reserve no capacity; moves are applied one at a time by a person, re-checked by
  PostgreSQL under the booking locks; the apply order and swap cycles are explicit.

Limitations
* Greedy rules, not an optimiser: the snapshot result shows the score rule can lose on its own total score. No
  look-ahead beyond one decision; no bumping of a kept promise even when it would lower the total.
* Rolling 12-hour window from now; trucks ready later are listed as out of window, not planned.
* Continuous time (minutes), not the published slot grid. A move can land between slots; the database checks dock
  time (the integrity mechanism), not slots, and slots are a planning view (0001).
* Swaps cannot be applied with single moves; there is no atomic multi-move apply yet.
* Readiness at apply time is checked by the service (advisory, from the current ETA); the database checks every hard
  rule but cannot know ETAs better than the service.
* Unbooked trucks (PLAN rows) are informational: they need the driver's booking flow, not `move_appointment`.

## 7. How the agent uses it

Tool `propose_facility_schedule`, arguments (structured, `extra` forbidden): `{"rule": "FACILITY_SCORE" | "FCFS" |
"EARLIEST_ETA"}` (default FACILITY_SCORE). The facility is the conversation's shipment's facility, never an argument.
Allowed in every flow state except HUMAN (a person owns the thread); works with the booking switch off (it books
nothing). It runs the planner under the read-only feasibility role and returns only:

```json
{"status": "OK", "data": {"status": "PROPOSAL", "not_a_booking": true, "note": "Proposal only, not a booking. ...",
  "rule": "FACILITY_SCORE", "facility_id": "FAC-JAI-01", "input_fingerprint": "<sha256>", "shipment_id": "SHP...",
  "proposed": {"dock_code": "D2", "start": "12:10 PM on 4 Aug", "start_ts": "...", "action": "MOVE",
               "keeps_promise": false, "wait_min": 124} | null,
  "needs_person": {"reason_code": "NO_CAPACITY_IN_HORIZON"} | null, "in_window": true,
  "facility_totals": {"trucks": 14, "planned": 8, "missed_promises": 7, "changed_promises": 4, "unscheduled": 6}},
 "say_hint": "This is a planning proposal for the dock office, not a booking. Do not tell the driver this time is confirmed; ..."}
```

No other truck, driver or reason text leaves the service on the driver channel, and the result carries no proposal
id, so nothing the agent holds can be applied. The agent still offers bookable times only through
`find_feasible_options` and the read-back / explicit-yes / hold / request flow.

## 8. How the console uses it

| Route | Who | What |
|---|---|---|
| `GET /console/facilities/{facility_id}/schedule-proposal?rule=…` | planner, ops_lead | the full proposal as of now (all trucks of that facility, score terms, reasons, fixed tasks, `depends_on`, `apply_order`, metrics). Stores nothing. Facility outside the person's scope: 404, as for conversations. |
| `POST /console/facilities/{facility_id}/schedule-proposals` `{"rule": …, "ttl_s": 60…1800}` | planner, ops_lead | computes and stores the proposal (evidence, not a booking) via `record_schedule_proposal`; returns the proposal plus `proposal_id`, `expires_at`. |
| `POST /console/appointments/{appointment_id}/move` `{"proposal_id": uuid, "idempotency_key": 16–128 chars, "note"?}` | ops_lead | applies that appointment's MOVE row. 409 `BOOKING_DISABLED` while `SETUHAUL_BOOKING_ENABLED` is off (default). Returns `{"outcome": "MOVED", "status": "CONFIRMED", "appointment_id": <new>, "replaced": <old>, "dock_id", "dock_code", "start", "end", "from_dock_id", "from_start", "proposal_id", "input_fingerprint", "rule", "moved_by", "moved_by_role", "moved_at"}`. |

Errors on the move: 403 `FORBIDDEN`/`ROLE_NOT_ALLOWED`; 404 `PROPOSAL_UNKNOWN`, `APPOINTMENT_UNKNOWN`,
`NOT_A_PROPOSED_MOVE`; 409 `BOOKING_DISABLED`, `PROPOSAL_EXPIRED`, `ALREADY_APPLIED`, `APPOINTMENT_CHANGED`,
`APPOINTMENT_IN_PROGRESS`, `SHIPMENT_IN_DOCK`, `SLOT_TAKEN`, `SLOT_IN_PAST`, `OUTSIDE_HOURS`, `OPERATIONAL_HOLD`,
`REQUEST_PENDING`, `DOCK_*` incompatibility, `NOT_FEASIBLE` (the truck's current ETA is after the proposed start),
`IDEMPOTENCY_KEY_REUSED`.

## 9. How `move_appointment` is protected

* **Who**: SECURITY DEFINER owned by `setuhaul_rpc` (NOBYPASSRLS), EXECUTE for `setuhaul_console` only (decision T31).
  The caller's identity comes from `begin_staff_context` (verified token); role from `approval_role_rules`
  `MOVE_CONFIRMED_PROMISE` = ops_lead (B-WF-2 §8), checked in the app and again in the database. The agent role has
  no EXECUTE and no privilege on the proposal tables (catalogue test).
* **Which facility**: RLS on `schedule_proposals`, `schedule_proposal_rows`, `appointments`; a proposal or
  appointment outside the staff scope is `PROPOSAL_UNKNOWN` / `APPOINTMENT_UNKNOWN`; the appointment must be at the
  proposal's facility (`CROSS_FACILITY`).
* **Which move**: only a MOVE row of a stored, unexpired proposal (`NOT_A_PROPOSED_MOVE`, `PROPOSAL_EXPIRED`), applied
  once (`ALREADY_APPLIED`), for an appointment that has not changed since (status CONFIRMED and current, same
  `row_version`, dock and start: `APPOINTMENT_CHANGED`).
* **Fixed trucks**: refused if the appointment is IN_PROGRESS, the shipment is IN_DOCK / COMPLETED / CANCELLED, or a
  FIXED claim exists for it.
* **Booking rules, re-proved under the locks**: compatibility (`booking_incompatibility`, the 0019 rules), opening
  hours and last new start (`slot_is_after_hours`), operational holds, not in the past, no open driver request for
  the shipment (`REQUEST_PENDING`), and the target dock time free of every active claim (expired holds released as
  `place_hold` does).
* **Locks** (extends 0020/0021): shipment row FOR SHARE → dock rows FOR SHARE (dock_id order) → booking-compat
  advisory keys (dock_id order; REPEATABLE READ refused) → the target dock's writer key (the one `place_hold` takes) →
  the shipment's OPEN approval rows → the appointment → the proposal row → occupancy. Two moves into the same dock
  time serialise; the second re-reads after the wait and is refused `SLOT_TAKEN`. A concurrent block or hold that
  takes none of these keys meets the `ex_one_truck_per_dock` exclusion constraint: exactly one commits (tests).
* **Record**: old appointment → CANCELLED (not current), its claim released; new appointment CONFIRMED,
  `booking_source = SCHEDULING_TOOL`, `replaced_appointment_id`, `confirmation_basis = HUMAN`; `booking_events`
  `APPOINTMENT_MOVED` with before/after and the note; outbox `AppointmentMoved`; idempotent per key and person.
* **Switch**: the console route is refused with `BOOKING_DISABLED` while the booking switch is off, the same switch
  as the agent's `hold_option` and `submit_booking_request`.

## 10. State transitions

```
proposal:     (computed, GET)  ──store──▶  STORED (expires_at ≤ 30 min)  ──time──▶  EXPIRED
              rows are immutable; a MOVE row: unapplied ──move_appointment──▶ applied (once)

appointment:  CONFIRMED (current) ──move_appointment──▶ CANCELLED (not current, replaced)
                                                    └─▶ new CONFIRMED (current, SCHEDULING_TOOL, replaced_appointment_id)
              IN_PROGRESS / truck IN_DOCK / FIXED claim: no transition (refused)
              PENDING_CONFIRMATION: not movable (decide the driver's request first: REQUEST_PENDING / not a MOVE row)

dock time:    old APPOINTMENT claim active ──▶ inactive;  new APPOINTMENT claim active (same transaction)

driver:       outbox AppointmentMoved ──enqueue_notifications──▶ notification template booking_superseded
              ("Your booking was changed by the warehouse. Reply here if you have a question.") ──▶ existing relay
```

## 11. Open items for the owner

* `arrived` was read as *at a dock* (IN_PROGRESS, IN_DOCK, FIXED claim): a truck waiting at the gate or in the yard
  can still be moved, because pulling an early truck forward is the main use. If "arrived" should also block yard
  trucks, add `AT_GATE`/`WAITING` to the refusal in `move_appointment`.
* Role: moving a confirmed promise is ops_lead only (`MOVE_CONFIRMED_PROMISE`); planners can view and store proposals.
* Driver message: the existing template `booking_superseded` does not state the new time. A template with the new
  dock and time needs Meta approval (`setuhaul_booking_superseded` must also be approved for sends outside the 24 h
  window). The conversation's `thread_flow.appointment_id` is not updated by a move (booking status reads the
  database, so the driver is told the current booking).
* `setuhaul/ops/pilot_mode.py` counts pending outbox events of three types; `AppointmentMoved` is not among them
  (file owned elsewhere).
* No atomic multi-move apply (swaps); no console page yet (`console_static` owned elsewhere).
* Proposal rows are computed by the service and stored through the console role; a staff member calling
  `record_schedule_proposal` directly could store hand-made rows. They are validated (facility, current
  appointments, no overlaps) and every move is still re-proved and audited, so integrity holds; the stored rows are
  evidence of a proposal, not proof that the planner produced them.
