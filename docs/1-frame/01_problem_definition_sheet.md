# Phase 1 · Frame — Problem Definition Sheet and Data Audit

SetuHaul Logistics · Driver exception and dock re-scheduling service
Snapshot under study: `data/setuhaul_freight_operations.db` (4 Aug 2026, Asia/Kolkata)

Companion files in this folder:

| File | What it holds |
|---|---|
| `01_problem_definition_sheet.md` | This sheet: problem, scale, facts / estimates / assumptions / unknowns, data audit summary |
| `02_variables.md` | Every variable the system reads, asks for, derives, stores or optimises |
| `03_high_level_design.md` | High-level design, the boundary between the LLM and code, Cognito + IAM auth, verified AWS facts, cost model |
| `04_eta_signals_and_tracking.md` | Independent ETA signals (FASTag, location pins, SIM, telematics), cost-optimal tiers, deterministic fusion, DPDP consent |
| `feasibility_prototype.py` → `worked_examples.md` | Deterministic prototype of `is_feasible()`. Worked checks on 5 cases plus derived values for all 21 shipments |
| `05_solver_and_maps.md`, `solver_spike.py` → `solver_results.md` | Solver choice (dispatch rules vs CP-SAT vs OptaPlanner / Timefold) with a spike on the snapshot. OpenStreetMap feasibility |
| `06_performance_reliability_security.md` | SLOs, quota headroom, Locust performance plan, reliability (FMEA, chaos, DR), security threat model and vulnerability management |
| `07_evaluation_plan.md` | Evaluation layers, scenario set, deterministic assertions, LLM-judge rubric, how each brief measure is computed |
| `08_human_in_the_loop.md` | Human-in-the-loop workflow: five modes, 14 triggers, SLAs and timeouts, takeover and handback, routing and workload, learning loop |
| `data_audit.sql` | 30 audit checks. Each issue below cites its check ID |
| `run_audit.py` | Runs the checks and writes `audit_results.md` (`python scripts/frame/run_audit.py`) |
| `audit_results.md` | Generated evidence: each query and the rows it returns |

---

## 1. Problem statement

> How might SetuHaul give drivers a conversational way to report delays, ask questions and consider revised dock appointments, while many simultaneous requests meet limited warehouse capacity, **without making a promise that is infeasible or conflicts with another promise**?

The brief splits this into five sub-problems. Each needs a different kind of solution:

| Sub-problem | What it asks | Who solves it |
|---|---|---|
| Replies | What does the driver hear at each turn? | LLM agent, using facts from tools only |
| Re-scheduling | What replaces the slot that will be missed? | Deterministic feasibility and ranking code |
| Warehouse constraints | Docks, hours, local rules, other bookings | Rules engine over structured data |
| Truck and driver constraints | Vehicle, load, next pickup, latest acceptable finish | LLM extracts them, code validates them |
| Many requests at once | Many drivers compete for one set of slots | Database transactions, holds and a facility-level scheduler |

**Core principle (from the brief and Anthropic's *Building effective agents*):** the agent works where conversations start and end, which is messy multi-turn text. Code decides capacity. The LLM never decides feasibility, priority, or whether a booking was committed.

## 2. Scale to design for

These figures come from the brief. The derived figures are my estimates, marked `est`.

| Measure | Value | Design implication |
|---|---|---|
| Loads per day | 280–360 | ~15 per hour average |
| Destination facilities | 6 | The scheduler runs per facility. No cross-facility optimisation |
| Dock doors (total) | 32 | ~5 per facility. Scarce. |
| Receiving appointments per day | 190–240 | `est` about 40–55 % slot utilisation over a 16-hour day, but peaks cluster |
| Exception messages, normal day | 15–25 | Low steady load |
| Messages in 30 minutes during a spike | 20–35 | `est` up to ~35 concurrent conversations. Contention is the problem, not throughput |
| Ops coordinators per shift | 5 | Escalations must be few, complete and actionable |
| Live GPS feeds | 0 | An ETA is a declaration, not a measurement. Confidence matters. We add independent, low-cost signals (FASTag, location pins, targeted SIM tracking) only where they change a decision (`04_eta_signals_and_tracking.md`) |
| `est` design headroom | 10× spike (≈350 msgs / 30 min) | Covers onboarding more facilities with no redesign |

**Consequence:** this is not a high-throughput system. It is a **high-correctness system under burst contention**. Spend the engineering on concurrency control, idempotency and escalation, not on throughput.

## 3. Stakeholders and what each needs during an exception

| Party | Knows | Needs | System touchpoint |
|---|---|---|---|
| Driver | Location, breakdown, traffic, likely delay | Clear choices, instructions, confirmation | WhatsApp / chat |
| Dispatcher / carrier | Vehicle assignment, driver availability | Revised plan, escalation status | Notifications |
| SetuHaul operations | Shipment plan, customer commitments, open exceptions | One consistent view across many simultaneous requests | Ops console and escalation queue |
| Warehouse planner | Dock capacity, labour, equipment, local rules | Feasible arrivals without overbooking | Confirmation workflow and facility schedule view |
| Consignee / customer | Inventory or production dependency | Reliable revised arrival information | Downstream notification (out of scope to build) |

## 4. Facts, estimates, assumptions, unknowns

### 4.1 Facts (verified in the data or stated in the brief)

| # | Fact | Evidence |
|---|---|---|
| F1 | 2 facilities, 9 docks, 21 shipments, 106 slots, 20 appointments | Table counts |
| F2 | Jaipur: D1–D4 STANDARD (20–25 t), D5 REEFER (22 t), D6 HEAVY (35 t, 90-min slots). Gurugram: D1–D2 STANDARD, D3 REEFER, 60-min slots 09:00–18:00 | `docks`, `appointment_slots` |
| F3 | The DB enforces only two concurrency rules: one active appointment per slot, and one current active appointment per shipment (partial unique indexes) | A30 |
| F4 | ETAs come from `ORIGINAL_PLAN`, `DRIVER_DECLARED`, `OPERATIONS_OVERRIDE` or `WAREHOUSE_ESTIMATE`. `v_latest_eta` takes the latest by `created_at` | Schema, `v_latest_eta` |
| F5 | Jaipur rules: early check-in ≤ 60 min, no-show grace 30 min, reefer → D5, > 25 000 kg → heavy dock, no new start after 21:00 without approval | `facility_rules` |
| F6 | D3 breakdown 09:15–13:00; D5 maintenance 18:00–22:00; D2 capacity reduction 08:00–09:20 | `dock_status_events` |
| F7 | One driver (DRV004) has two live assignments, and one thread has no shipment link | A20 |
| F8 | The data contains no live GPS | Brief, guide |
| F9 | Gate processing (gate-in → yard) is 1 min at JAI in all 5 check-ins. Gurugram has **no** check-in history | `facility_checkins` |
| F10 | AgentCore Runtime, Memory, Gateway, Identity, Observability, Policy and Evaluations are available in ap-south-1. Claude on Bedrock in ap-south-1 is served via the Global endpoint only | AWS AgentCore regions table; Claude in Amazon Bedrock docs (checked 17 Nov 2026) |

### 4.2 Estimates (derived; may be wrong)

| # | Estimate | Basis | How we firm it up |
|---|---|---|---|
| E1 | Average conversation reaches an outcome in 3–5 driver turns | Seed chats need 1–2 clarifications | Measure in scenario runs |
| E2 | Spike concurrency of ~35 conversations, ~10 contending for the same facility in the same 2-hour window | Brief spike figure; Jaipur has 4 standard docks | Load test in Break phase |
| E3 | ~6k input and ~0.4k output tokens per LLM turn, ~4 LLM turns per conversation | Prompt plus tool schemas plus compact context | Measure in Build; cost in Design |
| E4 | A driver decides on a shown option within 2–5 minutes | WhatsApp behaviour | Sets hold TTL; tune with data |
| E5 | Changeover buffer between trucks on a dock is ~10 min | Not in data | Confirm with warehouse planner (U3) |

### 4.3 Assumptions (we act on these until told otherwise)

| # | Assumption | Why it is safe enough | Risk if wrong |
|---|---|---|---|
| S1 (historical: Frame-phase assumption; **superseded by S1a–S1c**, kept as the record of the original assumption) | Every human signs in through **Amazon Cognito**: staff with MFA, drivers on web or app with a phone OTP. WhatsApp drivers are matched by the Meta-verified sender number to the same driver record. **Every API call is authorised with IAM (SigV4)** through Cognito identity-pool roles or service roles. The WhatsApp webhook (Meta HMAC) is the only non-IAM entry point | WhatsApp and OTP both verify the phone. IAM gives one authorisation model and audit trail | A shared phone could mis-attribute. Mitigation: confirm the order reference when the driver has more than one live shipment |
| S1a (as built, 28 Nov 2026) | **Staff** sign in through Amazon Cognito, and TOTP MFA is required for every staff user (`infra/setuhaul_stack.py`: `mfa=REQUIRED`, TOTP only, no SMS). There are four staff groups: `planner`, `operations`, `ops_lead` and `dispatcher`. **Drivers have no Cognito account and no phone OTP** | Staff identity is verified by a second factor; drivers never need a password | A staff account without a facility group sees no facility data (row-level security) |
| S1b (as built, 28 Nov 2026) | **Drivers** are identified by the messaging channel. In the synthetic pilot that channel is Telegram: staff issue a single-use, expiring linking code for a driver, the driver sends it to the bot, and the driver record is bound to Telegram's numeric `from.id` (`setuhaul/notify/telegram.py`, migration 0035). WhatsApp matching by sender number is the original design; it is **not operating** in the pilot (WhatsApp outbound goes to an internal test sink) | The link rests on a number Telegram assigns, not on a username a person can change | A shared phone or account could mis-attribute. Mitigation: confirm the order reference when the driver has more than one live shipment |
| S1c (as built, 28 Nov 2026) | **API authorisation is done by the application, not by the gateway.** API Gateway forwards every route (`ANY /` and `{proxy+}`) to the API Lambda with **no gateway authoriser**. FastAPI verifies the Cognito access token on each console route (signature, issuer, client, expiry, staff group), and the database enforces facility scope with row-level security. Webhooks are verified by an HMAC signature (WhatsApp) or a secret token (Telegram). IAM (SigV4) is used between AWS services only | One check that the tests exercise directly (`tests/test_api.py`, `tests/test_agent_not_operator.py`) | There is a single layer of token checking in front of the database rules. A gateway authoriser was designed (decision T10) and not built |
| S10 | A driver's message is **evidence, not truth**. ETAs are fused deterministically with independent signals when available. Accident and safety claims always go to a human | Drivers can be optimistic or unverifiable | Signal coverage gaps (no toll on a route, no consent). Fall back to a LOW-confidence driver ETA |
| S11 | Timing parameters (gate-to-dock, changeover, pending-confirmation timeout) are **per facility** (and per shift for the timeout), seeded now and learned from check-in history later | Warehouses differ in layout and staffing (GGN has no night coordinator) | Thin data at new facilities. Mitigation: conservative seeds, and options labelled `timing_basis = SEED` |
| S2 | A shown option is **not** a reservation. Only an explicit driver "yes" creates a short **hold**. The booking becomes CONFIRMED only after a deterministic re-check and a successful commit | The brief: "Showing is not reserving. Reserving is not confirming." | Final rule set in Design |
| S3 | An unload longer than one slot occupies the next contiguous slot(s) on the same dock | Otherwise A04 shows a hidden overlap | The warehouse may allow overrun. Confirm (U2) |
| S4 | Times are IST. The service uses an injected clock (`now`) so scenarios can be replayed at their message time | The snapshot has no single as-of time (A13) | None for production. Tests become deterministic |
| S5 | Priority order is CRITICAL > HIGH > NORMAL > LOW, but priority alone never moves a truck that is already in a dock or confirmed and on time | Brief: an early truck does not automatically win; protect work in progress | Final policy in Design (decision 7) |
| S6 | Re-routing to another facility is out of scope for automation. It is an escalation | Brief boundaries | — |
| S7 | Slots after `LAST_NEW_START_TIME` are shown only as "needs approval" and never auto-booked | RULE005 | — |
| S8 | A `LOW` confidence ETA is treated as a window (± 30 min, or the range the driver gave), and the agent asks before booking | A28, MSG005 / MSG006 | The window size is a configuration value |
| S9 | Warehouse confirmation is required for bookings the agent creates (`booking_source = DRIVER_CHAT`) until the client says otherwise | Seed data shows PENDING_CONFIRMATION for chat bookings | Adds latency. Can be auto-confirmed per facility by a flag |

### 4.4 Unknowns (the snapshot cannot answer these; questions for the client)

| # | Unknown | Why it matters | Default until answered |
|---|---|---|---|
| U1 | How long may **each** warehouse take to confirm a pending request, by shift? | Sets pending expiry (A23). GGN has no night coordinator (A22) | JAI day 15 / night 10, GGN day 15 / night 0 (escalate at once), capped at slot start − 30 min |
| U10 | Gate-to-dock time per facility. The snapshot lacks a `called_to_dock_ts` | `ready_ts` and every option depend on it | JAI 10 (one clean observation: 6 min), GGN 15 (no data) |
| U11 | Which tracking signals SetuHaul can contract: ULIP partner for FASTag, SIM-tracking vendor, existing carrier telematics APIs, and their prices | Coverage and cost of independent ETA evidence | Plan on the tiered strategy. Prices need vendor quotes |
| U12 | Is Global-endpoint model inference acceptable, given that Claude in Mumbai is Global-only? | Data residency of prompts | Accept, with PII and coordinates masked before any model call. Client sign-off required |
| U2 | May an unload run past its slot end, or must it fit? | Multi-slot occupancy (A04) | Must fit. Reserve contiguous slots |
| U3 | Changeover time between trucks on a dock | Feasibility margin | 10 min |
| U4 | Is the driver's "latest finish" a hard constraint (hours-of-service, next pickup)? | Option filtering | Hard if stated, else none |
| U5 | Who owns the night shift? CON005 is vacant with no email | Escalation routing (A21, A22) | Route to shift manager and ops queue |
| U6 | Real-world double assignments of drivers and vehicles (A01, A02): data entry error or real? | We must not book a slot for a truck that cannot be there | Flag for ops. Do not auto-book either shipment |
| U7 | Does SetuHaul pay detention penalties? Customer SLAs per priority? | Objective weights | Use priority weights only |
| U8 | Do dock events always block slots, or does the warehouse decide? | Events vs slot status disagree (A08, A09, A10) | Any overlapping BREAKDOWN or MAINTENANCE blocks the slot |
| U9 | Can the planner's original slot start before the planned ETA (A07)? Is it a buffer convention? | Interpreting "late" | Treat the slot start as authoritative for no-show |

## 5. Data audit summary

All 30 checks run with `python scripts/frame/run_audit.py`. Full queries and returned rows are in `audit_results.md`. **"System response" is what we will build so the issue cannot cause a wrong promise.**

### 5.1 HIGH: would lead to a wrong or conflicting promise

| ID | Finding (evidence) | System response |
|---|---|---|
| A01 | **One driver in two places.** DRV007 drives SHP1007 (VEH008, Ajmer, departed 06:10) **and** SHP1016 (VEH007, Mumbai, departed 3 Aug 18:20). DRV006 is in transit on SHP1006 (ETA 11:20 Jaipur) yet assigned SHP1021 departing Noida 10:00. DRV003 waits in the Jaipur yard with SHP1003 while SHP1018 "departs Bhiwadi 08:00". DRV001 left Jaipur at 08:50 but SHP1017 departed Neemrana at 08:15. | Validator `assignment_conflict` runs before any booking. If it hits, no auto-booking and the case is escalated as "contradictory data". |
| A02 | **One vehicle in two places.** VEH015 (SHP1006 and SHP1021) and VEH003 (SHP1003 and SHP1018) overlap. | Same validator |
| A03 | **Weight above dock limit on active bookings.** SHP1014 (21 000 kg) is pending on D1 (max 20 000). SHP1005 (20 500 kg) is confirmed on D3 (max 20 000). | `dock_compatible()` checks weight, dock type and refrigeration on every option and again at commit |
| A04 | **Unload longer than the slot** (SHP1002 70 min, SHP1005, SHP1009 and SHP1014 75 min on 60-min slots). The next slot looks free but is not. | Occupancy = `ceil((unload + changeover) / slot_len)` contiguous slots, all reserved atomically |
| A05 | **Pending request that cannot work.** APT1014A: ETA 11:25 + 75 min = 12:40, but the slot ends at 12:00 and D1 is booked at 12:00 (SHP1017). | The feasibility check uses `ready_ts + occupancy`, not just "slot is empty" |
| A06 | **Stale confirmed promises.** APT1006 (SHP1006 slot 10–11, ETA now 11:20) and APT1015A (SHP1015 slot 17–18, ETA 18:30) are still CONFIRMED. | Each ETA update re-evaluates the current appointment. If infeasible, it becomes `AT_RISK` and the driver is offered a re-schedule |
| A08 | **Confirmed booking on a broken dock.** APT1005 (SHP1005) is on D3 09:00–10:00. The breakdown starts 09:15 but the slot is still OPEN. | Dock events are joined into availability. Overlapping events block slots |
| A12 | **History shows a double booking.** APT1007 and APT1012A were both active on SLOT-JAI-003 from 1 Aug 11:50 until 4 Aug 09:40, which the current index forbids. Either the history was rewritten or the index was added later. | Audit trail is append-only. Integrity test in the Break phase |
| A21 | **Escalation was lost.** OM004 (reefer exception, SHP1015) went to `night.shift@…`, which is not a known contact, and FAILED. | Escalations go to a durable queue and the console first. Email and SMS are best-effort notifications with retry and alarms. A FAILED delivery is never treated as acknowledged |

### 5.2 MEDIUM: wrong state, ambiguity or missing control

| ID | Finding | System response |
|---|---|---|
| A07 | Planner booked slots starting **before** the planned ETA (SHP1006, 1015, 1016, 1017). | The rule uses slot start for no-show. Flag the planner inconsistency to the client (U9) |
| A09 / A10 | OPEN slots overlap dock events. `docks.dock_status` stays ACTIVE during a breakdown. `v_slot_availability` ignores events. | Our availability function joins events. We never trust `dock_status` alone |
| A11 | SHP1002 in D2 is expected to run to 09:18 and overruns into SHP1004's 09:00 slot (with the DEVT003 reduction). | The scheduler treats in-dock trucks as fixed tasks with a predicted end of `max(now, start + unload)` |
| A13 / A14 | No single as-of time: updates span 07:10 to 16:30. Six confirmed slots are past grace with no check-in and no NO_SHOW mark. | Injected clock. A no-show sweeper job marks NO_SHOW deterministically |
| A15 | OPEN slots start at or after 21:00 on D1–D4 despite RULE005. | The rule engine labels them `NEEDS_APPROVAL` |
| A18 / A19 | A messaging retry created a second message, a second thread and a second exception with the same `dedupe_key`. There is no UNIQUE constraint. | Idempotency on `external_message_id` at ingest, plus a content hash within a time window. Unique constraint on `dedupe_key` |
| A20 | THR010: DRV004 has SHP1004 and SHP1020 live, and the message names neither. | The agent asks with the order references. It never guesses |
| A23 | Two pending confirmations (APT1013A, APT1014A) have no expiry. OM002 is still QUEUED. | Add `hold_expires_at` and `pending_expires_at`. A Step Functions or EventBridge timer releases or escalates them |
| A24 | THR005 is ESCALATED, but APT1015A is still CONFIRMED. | One state machine owns appointment status. The conversation reads it and never writes it |
| A25 | SHP1012, SHP1016, SHP1018 and SHP1021 are live with no current appointment. SHP1018 has no exception owner. | A daily "orphan shipment" check goes to ops |

### 5.3 LOW / INFO

| ID | Finding |
|---|---|
| A16 | Gurugram has only a no-show rule. No early-check-in, last-start, reefer or heavy rule. Grace is defined twice (`facilities.checkin_grace_min` and `NO_SHOW_GRACE_MIN`). Slots cover 09–18 although opening hours are 07–21. Default unload is 45 min, but slots are 60 min. |
| A17 | The reefer rule hard-codes "D5". Use the capability `supports_refrigerated = 1`. |
| A22 | CON005 is a vacant night-shift role with no email. |
| A26 | EXC007 (early arrival, SHP1003) stores `reported_delay_min = 0`, but the declared ETA minus the original ETA is −30. The column cannot express "early", so delay must be a signed value we compute, not a value the LLM fills in. |
| A27 | EXC001 captured "after 12" as `earliest_acceptable_ts`, but the latest finish is still unknown. The agent asked (MSG002), the driver has not answered, and the exception stays NEEDS_INFORMATION. The extraction schema needs explicit `earliest_start` and `latest_finish` fields, each with an "unknown / asked" state. |
| A28 | Two LOW-confidence ETAs (SHP1013 "late by one hour", SHP1017 "12:30–13:00"). |
| A29 | SHP1015 (reefer, ETA 18:30): **0** feasible reefer slots. The correct outcome is escalation. |

### 5.4 What the two partial unique indexes do **not** cover

The database stops two active appointments on the **same slot row**. It does not stop:

1. Overlapping slots on the same dock with different times. `UNIQUE (dock_id, start, end)` only blocks exact duplicates.
2. A booking that spills into the next slot (A04).
3. A booking on a BLOCKED or CLOSED slot, or on a dock with an overlapping event (A08).
4. A booking on an incompatible dock: weight, reefer or heavy (A03).
5. A slot whose `facility_id` differs from its dock's facility, or from the shipment's destination.
6. An active appointment for a CANCELLED shipment.
7. The same driver or vehicle committed to two places at once (A01, A02).
8. Holds that never expire, and stale pending confirmations (A23).
9. Two conversations both *reading* "free" and both *telling* the driver yes. The index rejects the second write, but only if the code reads that rejection and tells the driver the truth.
10. Duplicate exceptions (`dedupe_key` is not unique, A18).

The Build phase closes 1–6 and 10 with database constraints (PostgreSQL `EXCLUDE USING gist` on `(dock_id, tstzrange)`, CHECKs and FKs) and closes 7–9 with the transactional booking service.

## 6. Ladder of situations: expected behaviour

| Level | Case | Seed | Expected outcome |
|---|---|---|---|
| 1 | Driver only asks about options | THR011 / SHP1017 | Show ranked feasible options. No hold, no booking |
| 2 | Message misses key facts | THR003 / SHP1013, THR010 / DRV004 | Ask one targeted question (ETA meaning, which order). No tool that writes is called |
| 3 | Rules narrow the choices | SHP1010, SHP1015, SHP1016, RULE005 | Only compatible docks. After-21:00 slots are labelled "needs approval" |
| 4 | Several delayed trucks chase nearby slots | SHP1006, 1012, 1013, 1014, 1009 | Holds serialise. CRITICAL gets precedence only by policy, never by speed |
| 5 | The world changes mid-conversation | SHP1005 / DEVT001, APT1008, THR001 / THR009, SHP1002 | Re-check at confirm. Tell the driver the option changed. Drop duplicates |
| 6 | No feasible slot | SHP1015 / THR005 / OM004 | Escalate with a complete hand-off record. Tell the driver plainly. Never invent a slot |
| + | Early, late and in-progress trucks share a facility | SHP1002, 1003, 1004, 1006 | The scheduler proposes a sequence. In-dock work is fixed |

## 7. Measures and Phase-1 targets

| Measure | Target (proposed) | Evidence source |
|---|---|---|
| Time from first message to usable outcome | p50 ≤ 3 min, p90 ≤ 8 min (excluding warehouse wait) | Scenario run timings |
| Share resolved without manual takeover | ≥ 70 % of non-escalation-class cases | Scenario set count |
| Conflicting or duplicate allocations | **0** (hard) | Integrity test, N parallel requests |
| Options later found infeasible at confirmation | ≤ 10 %, and every one is caught and explained | Re-check log |
| No-slot escalations handled correctly | 100 % have a complete hand-off record | Hand-off schema validation |
| Average driver waiting after rescheduling | Below the "do nothing" baseline in the replay | Gate-in to dock-in |
| Warehouse slot utilisation | Report it, no target | Used / open slots in window |
| Priority-policy violations | **0** | Policy checker on every decision |
| Driver clarification turns | ≤ 1.5 questions per resolved case | Transcript count |

## 8. Decisions carried into Phase 2 (Design)

These are the brief's 13 decisions. Phase 1 records the leaning. Phase 2 decides each with evidence.

| # | Decision | Phase-1 leaning |
|---|---|---|
| 1 | What to collect before showing options | Shipment resolved. ETA with confidence. Earliest start and latest finish if stated. Intent: browse or book |
| 2 | Link a conversation to driver, shipment and appointment | Phone → driver → live shipments. If more than one, ask by order reference |
| 3 | Revised ETA and its certainty | Deterministic `fuse_eta()` over the driver declaration **and** independent signals (FASTag, location pin, SIM, telematics, gate). The LLM only extracts. "Repair time ≠ arrival time" is clarified. Confidence comes from evidence |
| 4 | What makes a slot feasible | `is_feasible()` predicate (see `02_variables.md` §4) |
| 5 | What "available" means while another driver decides | AVAILABLE = no active appointment **and** no unexpired hold |
| 6 | Option → hold → request → confirmed | State machine with TTLs |
| 7 | Ordering of simultaneous requests | First valid commit wins inside a priority-protected window. Scheduler for re-sequencing |
| 8 | When the facility schedule recalculates | On ETA change, gate-in, dock event or cancellation. Fixed = in dock or started |
| 9 | Stale options, cancellations, duplicates, retries | Re-check at commit. Idempotency keys. Optimistic version check |
| 10 | No feasible slot | Escalate with a hand-off record. Tell the driver what happens next |
| 11 | Refusal wording | Reason plus the next-best option, in plain words |
| 12 | Human approval triggers | 14 triggers across five modes (approve, decide, take over, confirm, oversee), each with an SLA and a safe timeout. See `08_human_in_the_loop.md` |
| 13 | Proving no double booking | Parallel-request integrity test with DB constraints. 0 conflicts |

## 9. Out of scope

National network optimisation, carrier selection and rates, autonomous driver-safety decisions, customs and hazmat, commercial penalty approval, live GPS, and re-routing to another facility (escalated instead).
