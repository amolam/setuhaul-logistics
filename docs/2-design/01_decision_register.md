# Phase 2 · Design — Decision Register

> **Revised after external review (17 Nov 2026).** Where this file and `06_review_response.md` differ, `06_review_response.md` wins. Key changes: JWT for people / IAM for services; India-only models (`in.anthropic.claude-sonnet-5`, `in.anthropic.claude-haiku-4-5`); deterministic orchestration (no autonomous tool selection); `submit_booking_request` replaces `confirm_booking`; select → read-back → explicit yes before any hold; Data API = IAM + Secrets Manager, RLS set inside each transaction; outbox is at-least-once; AgentCore Memory deferred; truck-level exclusion constraint; MVP vertical slice.

Brief deliverable #2: *"Your answer, reason and evidence for each of the decisions in this brief."* Part A answers the brief's 13 decisions. Part B records the technology and architecture decisions this design depends on.

Every decision cites evidence that someone can re-run:

| Evidence | What it is | Where |
|---|---|---|
| **AUD-xx** | Data-audit check | `scripts/frame/data_audit.sql` |
| **WEX** | Worked feasibility examples | `docs/1-frame/worked_examples.md` |
| **SPK** | Solver spike | `docs/1-frame/solver_results.md` |
| **VAL** | Schema validation and integrity proof on PostgreSQL 18 | `docs/2-design/validation_results.md` |

Status is **Decided**, **Decided, pending client confirmation** (needs a SetuHaul value), or **Open**.

---

## Part A — The brief's 13 decisions

### UNDERSTAND

#### D1. What must we collect before showing useful options?

**Answer.** Four things, and only from the driver when the backend does not already hold them:
1. **Which shipment.** Resolved from the verified phone. Asked only if the driver has more than one live shipment.
2. **A usable ETA.** A time, a range, or a delay plus a reference point, with `delay_kind` known. "Repair time" and "arrival time" must be told apart.
3. **Hard constraints, if the driver has any.** `earliest_start`, `latest_finish` and what that finish means (gate-out or unload end).
4. **Intent.** Browse or book.

Everything else (appointment, priority, load, dock compatibility, rules, events) is looked up.

**Reason.** Missing inputs produce wrong options. Asking for backend facts wastes the driver's time and invites contradictions.

**Evidence.** Three example chats:
- **THR001.** ETA 11:20 and "after 12" are both given. Only the latest finish is missing, and the agent asks that once (MSG002).
- **THR003.** "Late by one hour" is ambiguous, so the agent asks one question to resolve `delay_kind` (MSG006).
- **THR002.** "Leave before 1:30". The agent asks only what 1:30 means (MSG004).

The fields are listed in the `record_driver_report` schema, `docs/1-frame/02_variables.md` §6c.

**Status:** Decided.

#### D2. How does a conversation link to the right driver, shipment and appointment?

**Answer.** Verified sender identity → driver: the signed WhatsApp webhook in the original design; in the current synthetic pilot, an allowlisted Telegram test account. Or a Cognito identity. Then → `drivers.phone_e164` → live shipments (not CANCELLED or COMPLETED).
- **One live shipment:** link the thread automatically.
- **Several:** ask with the order references ("ORD-…-004 or ORD-…-020?"). Never guess.

The current appointment is the row with `is_current` and an active status. The thread's `shipment_id` stays null until it is resolved.

**Reason.** One driver can hold two shipments (DRV004).

**Evidence.** Message-to-rows trace for THR010:
1. MSG016 from +91-9000010004 maps to DRV004.
2. Live shipments are SHP1004 (WAITING) and SHP1020 (ASSIGNED).
3. Two candidates, so the agent asks (MSG017).
4. The answer "004" sets `chat_threads.shipment_id = SHP1004`, and the current appointment becomes APT1004.

**Status:** Decided.

#### D3. How do we set the revised ETA, and show how sure it is?

**Answer.** A deterministic `fuse_eta()` combines all ETA evidence. The LLM never sets ETA or confidence.
- **Source order:** gate-in > ops override > fresh telematics > location pin > SIM ping > FASTag + route time > warehouse estimate > driver declaration > plan.
- **Checks:** physically impossible speed, optimistic declaration, staleness.
- **Confidence:** HIGH, MEDIUM or LOW, with a window (±0 / 15 / 30 min) and the pessimistic edge used for planning.
- **Repair time is not arrival time:** a `REPAIR_DURATION` statement triggers a clarification.

**Reason.** A driver's word is evidence, not truth. There is no GPS in the snapshot.

**Evidence.** The rule applied to two messages:
- **SHP1006.** Two declarations, 10:50 (MEDIUM) then 11:20 (HIGH). The latest same-source update wins, so the ETA is 11:20, HIGH, window 11:20–11:20, `ready_ts` 11:30.
- **SHP1013.** "Late by one hour" gives 11:00, LOW. The window is 11:00–11:30, `ready_ts` 11:40.

See WEX, and `docs/1-frame/04_eta_signals_and_tracking.md` §3.

**Status:** Decided and implemented in `setuhaul/domain/eta.py`, tested in `tests/test_eta.py`. The fusion code accepts gate-in, ops override, telematics, location pin, SIM, FASTag, warehouse estimate, driver declaration and plan. **No independent signal feed (FASTag, SIM, telematics) is connected**, so in practice ETAs come from the plan, the driver, gate-in and staff. Signal vendors are pending (U11).

### DECIDE

#### D4. What makes a slot feasible for this shipment?

**Answer.** One predicate, `is_feasible()`, used when listing options **and** at commit. A slot is feasible when all of these hold:

| Rule | Detail |
|---|---|
| Same facility | The dock is at the shipment's destination |
| Dock type | REEFER if temperature-controlled; HEAVY if `required_dock_type = HEAVY` or the load is above the facility's heavy-dock threshold; otherwise STANDARD |
| Weight | Load ≤ dock maximum |
| Duration | Occupancy = unload + changeover, covered by `ceil(occupancy ÷ slot length)` contiguous slots |
| Dock free | No overlapping block, booking, hold or in-dock truck |
| Timing | Start ≥ `ready_ts`; start ≥ the driver's earliest start; end ≤ the driver's latest finish; end ≤ facility close |
| Approval | Start ≥ last-new-start time means "needs approval" |
| Assignments | No driver or vehicle conflict |
| Shipment state | Not cancelled or completed |

The database **re-enforces** the physical rules (dock type, weight, overlap, facility, shipment state) with an exclusion constraint and a trigger, so even a bug in the service cannot commit an infeasible booking.

**Reason.** Many rules apply at the same time. An empty row in a slot table is not feasibility (AUD-A03, A04, A08).

**Evidence.** The written check run on three shipments:
- **SHP1014.** Two-slot options on D2 or D4 only. D1 and D3 are rejected on weight.
- **SHP1015.** No slot, so escalate.
- **SHP1006.** Options exist, but there is an assignment conflict, so escalate.

See WEX. The DB layer rejected the seed's infeasible bookings APT1005 and APT1014A on weight, and APT1004 and APT1015A on overlap (VAL §1).

**Status:** Decided.

#### D5. What does "available" mean while another driver is deciding?

**Answer.** *Available* means no active booking, no blocking event, no truck in the dock **and no unexpired hold** overlapping the window. A **shown option does not reserve anything.** It is a snapshot stamped with `schedule_version` and valid for 5 min for display purposes only.

**State needed:**
- `dock_occupancy` rows of kind `HOLD`, with `expires_at` (released lazily in the same transaction and by a sweeper).
- `facilities.schedule_version`, bumped by a trigger on every occupancy change.
- `shown_options`, so the system can tell the driver "this option changed since I showed it".

**Reason.** Two readers can see the same slot. Only the commit decides.

**Evidence.** Schema and the contention test (VAL §2).

**Status:** Decided.

### COMMIT

#### D6. When does an option become a hold, a request, a confirmed booking?

**Answer.**

| State | Who moves it | What the driver sees |
|---|---|---|
| OPTION_SHOWN | Agent (read-only tool) | "D2 13:00 is free right now. It is not reserved until you choose it." |
| → SELECTED | Driver picks one ("2") | "You selected D2 at 13:00, unloading until 13:55. Shall I request it?" |
| → HOLD (3 min) | Driver's explicit yes to the read-back. `hold_option` → `place_hold()` | "Holding D2 13:00 for you…" (internal; usually the next step happens within the same turn) |
| → PENDING_CONFIRMATION | Booking service, after re-checking `is_feasible`, when the facility requires warehouse confirmation | "Requested D2 13:00. The warehouse confirms by 10:07. You'll hear from me either way." |
| → CONFIRMED | Warehouse planner, or automatic where the facility and shift allow | "Confirmed: D2 13:00–13:55, ref APT-…" |
| → IN_PROGRESS → COMPLETED | Gate and dock events | — |

Side exits:
- **LOST:** the hold failed. The driver gets fresh options in the same turn.
- **EXPIRED:** the hold or pending TTL ran out.
- **REJECTED:** the warehouse said no. The driver is offered alternatives.
- **AT_RISK:** a new ETA makes the booking infeasible. Re-schedule.
- **NO_SHOW:** the no-show grace period passed.

**Reason.** Showing is not reserving. Reserving is not confirming.

**Evidence.** State machines in `04_tool_contracts_and_states.md`. Pending TTL per facility and shift (APT1014A booked 09:52 would expire at 10:07).

**Status:** Decided. TTL values are pending client confirmation (U1).

#### D7. How do we order simultaneous requests when capacity runs short?

**Answer** (as built; revised 27 Nov 2026 to match the code). There are two layers.
1. **Driver requests: the first valid commit wins.**
   - Every driver's options are computed from the same database state.
   - The database judges competing holds one at a time: `hold_option()` takes a per-dock advisory lock, and `ex_one_truck_per_dock` is the final guard.
   - Exactly one request gets the slot. Every other driver gets `LOST`, a plain "That slot was just taken." and fresh options in the same answer.
   - Typing speed carries no business priority, and the agent cannot reorder anyone.
2. **Priority is applied by people, through a proposal, never by the agent.**
   - The facility scheduling tool (`setuhaul/domain/scheduling.py`, rule `FACILITY_SCORE`) is a **deterministic greedy scoring planner with no solver library**. It proposes a sequence for one facility in three passes:
     - (1) keep every promise the truck can still make;
     - (2) dispatch the rest, highest priority weight first, onto the earliest usable dock time;
     - (3) pull an early truck forward only if its own score drops.
   - Score per truck: `w × wait + 2 × w × lateness + 30 × changed`, plus `2000 × w` if unscheduled. The weights are CRITICAL 8, HIGH 4, NORMAL 2, LOW 1.
   - A proposal books nothing. Only an ops lead applies a move, one at a time, through `move_appointment()`, which re-checks every rule under the booking locks.

The objective in plain words: less weighted waiting, fewer missed appointments, fewer changes to promises already made.

**Not built:**
- **CP-SAT.** It was evaluated in the Frame spike and in `optimizer.py`, and not used: the problem size does not need it, and the OR-Tools wheel would change the Lambda build.
- **The `max_yard_wait_min` fairness guard.** The column exists in `facilities` (0001), but no planner reads it.

**Reason.** Fairness and service levels conflict. A race must never decide priority, and a proposal must never silently break a promise.

**Evidence.** The policy was tested on competing trucks. These are the actual outputs of `python scripts/scheduling_compare.py`, from `docs/3-build/SCHEDULING_TOOL.md` §5. `tests/test_scheduling.py` pins the behaviour.
- **Early, late and in-progress trucks at one facility.**
  - `FACILITY_SCORE` changed 1 promise, against 5 for FCFS and 4 for EARLIEST_ETA, and kept 4, against 0 and 1.
  - Total score 615, against 725 and 796.
  - Its one move is applicable at once.
  - It **loses on waiting**: weighted wait 185, against 95 and 112.
- **The challenge snapshot (FAC-JAI-01, 09:56).**
  - **EARLIEST_ETA has the best total** (35,592, against 35,954 for `FACILITY_SCORE`) and the least waiting.
  - `FACILITY_SCORE` keeps one more promise (CRITICAL SHP1009) but makes SHP1005 and SHP1004 wait longer.
  - Six trucks go to a person under every rule: the five double-assigned trucks, and SHP1015, which has no reefer capacity.
- **Competing driver requests.**
  - `docs/challenge/INTEGRITY_PROOF.md`, N = 10 and N = 20: exactly one winner.
  - `tests/test_api.py::test_fifty_drivers_race_for_one_slot`, N = 50: 1 OK, 49 LOST, every loser with fresh options.

**Status:** Decided and implemented. Two things are pending the client: the priority weights, and which planner rule to use by default. The snapshot result favours EARLIEST_ETA when waiting matters more than keeping told times.

#### D8. When does the facility schedule recalculate, what is fixed, and what is the objective? (optional extension)

**Answer** (as built; revised 27 Nov 2026 to match the code).
- **When it recalculates:** on demand only. Nothing re-plans automatically.
  - A planner or ops lead asks in the console (`GET /console/facilities/{id}/schedule-proposal`, or `POST …/schedule-proposals` to store one).
  - The agent can also call the read-only tool `propose_facility_schedule`.
  - Each proposal is computed from the current database state: latest ETAs, gate-ins, dock events, holds and pending requests. So a proposal asked for after an ETA update or a gate check-in reflects it.
  - Event-driven recalculation and debouncing were designed but **not built**.
- **What is fixed** (`FixedTask`, never moved):
  - trucks unloading (IN_PROGRESS, IN_DOCK, FIXED claims);
  - dock blocks from breakdown or maintenance events;
  - a driver's live hold;
  - a pending request awaiting a planner.
- **Other boundaries:** a truck with contradictory data (a double-assigned driver or vehicle) is never planned, and goes to a person. The planning window is 12 hours rolling, inside opening hours and the last-new-start time.
- **Objective:** `w × wait + 2 × w × lateness + 30 × changed`, with `2000 × w` for an unscheduled truck. It runs as the greedy three-pass rule described in D7.
  - Compatibility, release time and committed dock time come from the same `FeasibilityEngine` the driver path uses.
  - No solver library is used.
- **Applying a proposal:**
  - Moves carry `depends_on` and `apply_order`.
  - Swap cycles get no order and go to a person.
  - Each move is re-proved by `move_appointment()` and refused if anything changed (`APPOINTMENT_CHANGED`, `SLOT_TAKEN`, `PROPOSAL_EXPIRED`, …).

**Reason.** Changes can ripple through the whole day. A proposal that moved trucks already unloading, or silently re-timed promises already given, would cost more trust than it saves. So the work that is fixed is never moved, every change is visible as a score term, and a person applies each move.

**Evidence.**
- **The replay:** `python scripts/scheduling_compare.py`. Its output is in `docs/3-build/SCHEDULING_TOOL.md` §5. It covers one early, one late and one in-progress truck at one facility, plus the Jaipur snapshot at 09:56.
- **Item 7 of the scenario run:** the proposal keeps B's confirmed promise and changes 0 appointments (`tests/test_scenario_run.py`).
- **Tests:** `tests/test_scheduling.py` (pure, deterministic) and `tests/test_move_appointment.py` (PostgreSQL), including `test_two_proposals_moving_different_trucks_to_the_same_slot_exactly_one_wins`.

**Status:** Decided and implemented, with on-demand recalculation. Event-driven recalculation and an atomic multi-move (swap) apply are not built. There is no console page for proposals yet; the console API serves them.

#### D9. How do we handle stale options, cancellations, duplicate messages and retries?

**Answer.**

| Problem | Handling |
|---|---|
| **Stale option** | `hold_option` re-checks against current state. If the option's `schedule_version` is older and it is no longer feasible, the driver hears *"D1 13:00 was just taken; here are the current options"* with fresh options |
| **Cancellation** | The occupancy row is released, `schedule_version` bumps, and the scheduler is triggered. Drivers holding expired options in that area are not auto-notified, but their next request sees the capacity |
| **Duplicate message** | Unique `(channel, external_message_id)`, plus a content-hash window for retries that arrive with new ids. Unique `dedupe_key` on exceptions |
| **Retries of tool calls** | Idempotency key (thread, tool, args, turn) returns the stored result |

**Reason.** Chat channels repeat messages and networks retry calls. A stale option or a duplicate must never become a second booking or a silent failure. So freshness is re-checked at commit, and every write is idempotent.

**Evidence.** One replayed case for each:
- **Duplicate:** THR001 and THR009 (MSG015, 2 s apart) — the second insert is rejected by `dedupe_key` (VAL §4).
- **Cancellation:** APT1008 frees D2 10:00.
- **Stale option:** the contention test's 1,990 losers all received LOST, not an error (VAL §2).
- **Retry:** the idempotency table contract (`04_…`).

**Status:** Decided.

### FAIL SAFELY

#### D10. What happens when no feasible slot exists?

**Answer.** Escalate in **Decide** mode with a complete hand-off record. Tell the driver plainly what happens and by when. Never invent a slot.

**Reason.** An invented answer is worse than an escalation. A slot the facility cannot honour turns into a broken promise at the gate, while a coordinator with a complete record can solve the case once.

**Evidence.** The SHP1015 escalation message: *"No reefer dock is free tonight because of planned maintenance from 6 PM. An operations coordinator is reviewing options and will message you by 16:45. Please continue to Jaipur safely."* The hand-off record is in `docs/1-frame/02_variables.md` §7. Delivery goes to the durable queue first; e-mail is a best-effort notification, so the OM004-style failure cannot lose it.

**Status:** Decided.

#### D11. What do we say when the preferred slot is not granted?

**Answer.** Give the reason in plain words and the best alternative in the same message. Never a bare "no".

**Reason.** A refusal without a reason or a next step costs trust and triggers repeat messages. Telling the driver what happened and what they can do next keeps the conversation moving.

**Evidence.** Two refusal messages as built. They are fixed templates, not model text, and each is asserted by a test.
1. **Another driver got the slot first.** The reply is *"That slot was just taken."* followed by *"Available slots: 1. … 2. … 3. … Reply with the number you want."*
   - The template is `lost` in `setuhaul/agent/replies.py`.
   - Asserted by `tests/test_scenario_run.py::test_scenario_run_shows_all_six_items_and_ends_in_an_escalation`, item 5: the reply starts with "That slot was just taken.", the contested slot is absent and new options are present.
   - Also asserted by `tests/test_api.py::test_fifty_drivers_race_for_one_slot`: every loser receives fresh options.
   - The verbatim text is in `docs/challenge/evidence/integrity_proof.txt` §4.
2. **The planner rejected the request.** The reply is *"The warehouse could not take that slot. Reply here and we will find another one."*
   - The template is `booking_rejected` in `setuhaul/notify/outbound.py`.
   - Asserted by `tests/test_flows.py::test_f2_rejected_request_releases_the_slot_and_the_driver_can_ask_again`: the driver's phone gets exactly this text, the slot is released, and the next message returns real options.

**Difference from the design wording:**
- A slot that does not fit the truck, such as SHP1014's 21 t load on the 20 t D1, is **never offered**, so the driver is never refused for it.
- The planner's rejection reason is not passed to the driver; the refusal names what happens next instead.
- When no slot exists at all, the driver gets the escalation message (D10), not a refusal.

**Status:** Decided and implemented.

#### D12. Which decisions need human approval or takeover?

**Answer.** 14 triggers across five modes (approve, decide, take over, confirm, oversee), each with an SLA and a safe timeout where silence is never approval. See `docs/1-frame/08_human_in_the_loop.md`.

**Reason.** Safety, penalties, customer commitments, contradictory data and cases with no feasible slot carry risk that a rule cannot own. The brief's boundaries say a person approves or takes over these. Silence must never count as approval.

**Evidence.**
- Triggers, modes, SLAs and timeouts are defined in `docs/1-frame/08_human_in_the_loop.md` §2–§4.
- `tests/test_approvals.py` covers approval and rejection decided by the session principal, and the claim being released on rejection. Examples: `test_approve_confirms_with_the_session_principal_one_event_and_an_audit_row`, `test_reject_releases_the_claim_and_records_the_decider`.
- `tests/test_confirmation_policy.py` covers the rule rows that require a person. Examples: `test_every_facility_has_every_row_and_none_confirms`, `test_a_plain_new_booking_is_the_safe_case_and_still_needs_a_person`, `test_critical_load_needs_a_planner_not_operations`.

**Status:** Decided and implemented.
- Approvals: `migrations/0015_approval_workflow.sql`, `migrations/0016_confirmation_policy.sql`. Tests: `tests/test_approvals.py`, `tests/test_confirmation_policy.py`.
- Escalations and takeover: `0009`, `0018`, `0022`.
- Facility approval mode, MANUAL (default) or AUTO for auto-eligible safe cases only: T53, `0047`.
- SLAs and safety wording are pending client confirmation.

### PROVE

#### D13. How will we prove the system did not double-book capacity?

**Answer.** Four layers:
1. **The database makes it impossible.** `EXCLUDE USING gist (dock_id WITH =, during WITH &&) WHERE active` covers holds, bookings, blocks and in-dock trucks.
2. **A per-dock advisory lock** gives clean losers instead of deadlocks.
3. **A concurrency test** in CI.
4. **A post-run checker** that self-joins active occupancy.

**Reason.** The business trusts an allocation only with evidence. Application checks alone can race, so the guarantee must hold in the database even if the service has a bug or someone writes to the database directly.

**Evidence.** On PostgreSQL 18:
- 10 / 50 / 200 parallel holds on one window × 30 / 20 / 10 rounds → **exactly one winner every round**.
- 150 random overlapping holds → **0 overlaps**.
- **0 deadlocks** after the advisory-lock fix. The first run surfaced 40P01 deadlocks, which led to the fix. (VAL §2–3.)

**Evidence on the built system (27 Nov 2026):** `scripts/integrity_proof.py`.
- It runs the full path: signed webhook → booking service → `hold_option()` → the constraint.
- **N = 10:** 10 requests, 1 slot taken, 9 `LOST`, 0 double bookings.
- **N = 20:** 20 requests, 1 slot taken, 19 `LOST`, 0 double bookings.
- In both runs, a direct superuser insert of an overlapping claim is refused with `23P01`.
- See `docs/challenge/INTEGRITY_PROOF.md`.

**Status:** Decided and implemented. The VAL runs above used the Phase 2 design schema; the integrity proof runs on the final migrations.

---

## Part B — Technology and architecture decisions

| # | Decision | Choice | Main reason | Rejected alternatives | Evidence |
|---|---|---|---|---|---|
| T1 | Agent platform | **Amazon Bedrock AgentCore** (Runtime, Gateway, Identity, Observability, Policy, Evaluations) in **ap-south-1**. **Memory deferred**: conversation and workflow state live in PostgreSQL and Step Functions | Managed agent hosting with session isolation, MCP tool gateway, built-in tracing and evals. All needed features are in Mumbai | Self-hosted on ECS (more ops); Bedrock Agents classic (less control of the loop) | AgentCore regions table |
| T2 | Agent framework inside Runtime | **Strands Agents** (Python) as the harness, with **deterministic orchestration**: Python selects every operation from the state machine; the LLM never chooses tools in v1 | AWS-native, first-class AgentCore and Bedrock support, Python like the rest | LangGraph (heavier for a small tool set) | — |
| T3 | Models | **India profile:** `in.anthropic.claude-haiku-4-5-20251001-v1:0` for extraction (forced tool, strict schema); `in.anthropic.claude-sonnet-5` for reply phrasing only; templates when no LLM is needed | Quality where it matters; Haiku costs ~20× less for structured extraction | One model for everything (cost); Opus (unneeded) | Claude in Amazon Bedrock docs |
| T4 | Model endpoint | **Superseded for the synthetic Sydney FDE pilot by T52 (21 Nov 2026); kept for an India deployment with real data.** **India geographic cross-Region inference** (ap-south-1 ↔ ap-south-2 only) on `bedrock-runtime` (Converse). Global profiles are blocked by IAM. Masking remains as defence in depth | Driver data stays in India. Costs the +10 % regional premium and means the previous model generation (Sonnet 5, Haiku 4.5), because 5.5 models are Global-only from Mumbai | Global Sonnet 5.5 / Haiku 5.5 | AWS India CRIS announcement (29 Sep 2026) |
| T5 | System of record | **Aurora PostgreSQL Serverless v2**, Multi-AZ, min 0.5 ACU in production (scale to 0 in dev and test) | Exclusion constraints, transactions, RLS. Scales with spikes | DynamoDB (cannot express range-overlap constraints, so integrity moves into app code); provisioned Aurora (idle cost) | VAL |
| T6 | DB access from Lambdas | **RDS Data API**: IAM authorises `rds-data:*`; DB credentials come from **Secrets Manager** (`secretArn`); RLS scope is set **inside each Data API transaction** (session state is `DISCARD ALL`-reset between calls); writer-only; 1 MiB response cap. MVP uses local PostgreSQL behind the same repository interface | $0.397 per million requests in Mumbai. No RDS Proxy (8-ACU minimum ≈ $134/month) and no VPC-attached Lambdas or interface endpoints for DB access | RDS Proxy + VPC Lambdas | Mumbai price list |
| T7 | Integrity mechanism | `dock_occupancy` + exclusion constraint + compatibility trigger + per-dock advisory lock | Correct by construction under any concurrency | App-level locks only; slot-row uniqueness only (the seed's gap) | VAL |
| T8 | Scheduling solver | Planned: **OR-Tools CP-SAT** (Lambda container) with a dispatch-rule fallback. **As built (27 Nov 2026 correction): no solver library.** A deterministic greedy scoring planner (`setuhaul/domain/scheduling.py`, see D7/D8), plus exhaustive per-truck option ranking (`optimizer.py`) | The spike showed the size does not need CP-SAT, and the ortools wheel would change the Lambda build | OptaPlanner / Timefold (JVM; Python discontinued) | SPK |
| T9 | Ingest | API Gateway (HTTP API) → ingest Lambda → **SQS FIFO** (`MessageGroupId = driver_id`, dedupe = message id) | Per-driver ordering plus dedupe for free | Direct synchronous agent call from the webhook (timeouts, no ordering) | AUD-A18/A19 |
| T10 | Auth | **People:** Cognito user pool JWT, validated by the API Gateway **JWT authoriser**, then FastAPI RBAC/ABAC. **Services:** IAM roles. **Agent:** AgentCore Identity + Policy. **Webhook:** Meta HMAC. No identity pool | Separate trust mechanisms per caller type; the browser does not need AWS credentials (owner-approved after review) | Identity pool + SigV4 from the browser; API keys | `06_review_response.md` |
| T11 | Timers and long waits | **EventBridge Scheduler** (hold and pending expiry, approval timeouts) and **Step Functions** (`waitForTaskToken` approvals), plus a 1-minute reconciler | Exact-time triggers, at-least-once with idempotent handlers | Cron polling only | — |
| T12 | Notifications | Transactional **outbox** → EventBridge → WhatsApp (Cloud API via Lambda), **AWS End User Messaging** SMS, **SES** e-mail, with delivery tracking. **At-least-once** with idempotent consumers keyed on `notification_id` | No lost events; the consumer suppresses duplicates, and any residual duplicate is harmless (same reference) | Fire-and-forget e-mail (OM004) | AUD-A21 |
| T13 | ETA signals | Tiered: FASTag (ULIP partner), WhatsApp location pins, targeted SIM tracking, existing telematics; deterministic `fuse_eta()` | Cost-optimal evidence beyond the driver's word | Fleet-wide GPS rollout | Phase 1 `04_…` |
| T14 | Maps and routing | v1: offline drive-time matrix built with self-hosted Valhalla on OSM; no runtime routing service | ≈ $0 runtime; public OSM servers are not allowed | Public OSM / OSRM / Nominatim | Phase 1 `05_…` |
| T15 | Guardrails | Bedrock Guardrails (PII mask, prompt attack, denied topics) plus a deterministic pre-mask plus a **grounding post-check** in code | Defence in depth; grounding is cheaper and stricter in code | Guardrails alone | — |
| T16 | Ops console | React SPA on S3 + CloudFront, Cognito sign-in, API Gateway (IAM) + AppSync for live queue updates | Small, serverless | Amplify Hosting (fine too) | — |
| T17 | Observability | AgentCore Observability (OTel) → CloudWatch + X-Ray; custom business metrics; dashboards per measure | One trace from webhook to DB | Third-party APM (cost) | — |
| T18 | Evals | Scenario replay harness in CI plus AgentCore Evaluations online | Deterministic gates plus a calibrated judge | — | Phase 1 `07_…` |
| T19 | IaC | **AWS CDK (Python)** with cdk-nag | Same language; security rules in CI | Terraform (fine; team preference) | — |
| T20 | Region and DR | ap-south-1, Multi-AZ; backups copied to ap-south-2 (Hyderabad) | Residency, low latency, cost-proportionate DR | Active-active multi-region (overkill) | — |
| T21 | Degraded mode | Template replies with numbered options, generated from tool output when the LLM path fails | Capacity logic never depended on the model | Hard failure | Phase 1 `06_…` §4 |
| T22 | Load testing | Locust on ECS Fargate (core-only and full-stack modes) | Python, shares code and signing | JMeter | Phase 1 `06_…` |
| T23 | Build scope | **Production design + MVP vertical slice** (local PostgreSQL, real Bedrock, Cognito, SQS, Step Functions, AgentCore for demos) | Proves the workflow end to end at ≈ ₹1,500–2,500/month | Deploying the full platform now | `06_review_response.md` |
| T24 | Data residency | **Modified for the synthetic Sydney FDE pilot by T52 (21 Nov 2026): no real driver data is processed, so India-only inference is not required for the pilot; the masking rules stay.** **Status: designed, Build verification pending (spike B1–B4).** India-only inference; no raw phone numbers, registrations or coordinates in prompts; sanitised eval data | DPDP and customer trust; masking is not a substitute for approval | Global inference with masking | `06_review_response.md` |
| T26 | Workflow state ownership | PostgreSQL owns business state; Step Functions owns long waits (ids and task tokens only); PostgreSQL wins on any disagreement | One source of truth | State duplicated in workflow payloads | `07_acceptance_and_verification.md` |
| T27 | Human approval authorisation | Authenticated (JWT + role + facility), expiring, idempotent (`decision_idem_key`), re-validated with `is_feasible`; task token kept server-side; audited | Approvals are the highest-risk human action | Approve links with embedded tokens | `07_…` |
| T28 | Model and prompt versioning | Every inference span records model, inference profile ARN, prompt / schema / policy / cfg versions, tokens, latency. Changes require a golden-set run | Reproducibility and residency evidence | Unversioned prompts | `07_…` |
| T29 | Failure recovery | Per dependency: timeout, jittered retries, circuit breaker, idempotent compensation, DLQ + redrive runbook | Predictable degradation | Ad-hoc retries | `07_…` |
| T31 | Database privileges | Least privilege per role (console / agent / system). Capacity changes only through `SECURITY DEFINER` functions owned by a NOBYPASSRLS, NOLOGIN, member-less role with a pinned `search_path`; column-level grants for other writes; no DELETE; nothing for PUBLIC. Every new function gets its own EXECUTE grant | RLS limits rows, not operations; the architecture promises controlled tool execution | One role with SELECT / INSERT / UPDATE on everything | `schema.sql`, validator §7 |
| T32 | Agent authority (Build invariant, owner's instruction 17 Nov 2026) | The agent understands and **requests**; the application and database **decide**. The agent role's tables, columns and functions are pinned to an allowlist by a CI test; it cannot insert driver messages, so it cannot forge a driver's yes | Keeps the LLM out of every decision with business consequences | Agent with broad DB write access guarded by prompts | `tests/test_agent_not_operator.py` |
| T33 | Database engine version | **Aurora PostgreSQL 18** (minimum 18.3, the first 18.x the RDS Data API supports; Data API on 18 is available in ap-south-1 and ap-south-2). Pin the latest minor and patch on AWS's schedule | Same major version as every local test (PostgreSQL 18); no downgrade; longest support window | Aurora PostgreSQL 16 (an older major, and the local evidence would be a different version) | AWS docs: Aurora PG 18 GA (June 2026); Data API region/version table |
| T33-P | Database engine version, **pilot-only exception** (owner approval, 23 Nov 2026) | **PostgreSQL 17.9 (Aurora express configuration) is permitted exclusively for the synthetic AWS pilot on the AWS Free Plan.** T33 is unchanged: PostgreSQL 18.3+ is required for production and for any non-synthetic data. The exception ends when the account leaves the Free Plan, when real data is processed, or before production, whichever comes first; it does not activate booking (Option D still needs all six gates and the owner's separate written approval) | The AWS Free Plan offers Aurora PostgreSQL only as the express configuration, whose engine is 17.9 (no 18.x option; a plan upgrade is a paid change). Compatibility evidence: the whole suite runs on PostgreSQL 17 in CI and on 18 locally; migrations 0001-0024 run live on Aurora 17.9 with matching hashes; migration 0025 and its functions verified on the live 17.9 cluster in a rolled-back transaction | Upgrade the AWS plan for an 18.x cluster (paid; not authorised); block the pilot on 18.x | `docs/release/13_STEP6_GATE_EVIDENCE.md`; `evidence/step6_gates.json`; `evidence/pg17_migration_0025.json`; CI runs 45062734398 and the PostgreSQL 17.9-pinned run |
| T34 | Confirmation and hand-over trust (step 2 review) | The driver's yes is an **attestation** written by the trusted inbound path for a DRIVER message after a database-written read-back; hold and request verify it. ETA reports cite a DRIVER message in the caller's thread. TAKE_OVER escalations and console takeovers set the human flag, thread status and flow state atomically. Unassigned escalations go to a CENTRAL queue, never a default facility | The agent requests; it never attests, backdates or chooses identity | Trusting the agent's claim that the driver said yes | `migrations/0004_step2_review_fixes.sql` |
| T35 | Database roles per responsibility (step 2 final review) | `setuhaul_agent` (requests), `setuhaul_ingest` (verified inbound + attestations), `setuhaul_feasibility` (reads + publishes options), `setuhaul_console` (people), `setuhaul_system` (background jobs), `setuhaul_rpc` (owns definer functions). Each has a catalogue-tested allowlist. A driver's YES is valid only until their next message | A read-only transaction is not a security boundary; a later NO must always win | One broad system role for inbound, feasibility and jobs | `migrations/0005_attestation_freshness_and_ingest_role.sql` |
| T36 | Per-conversation serialization (review R0.7) | One transaction lock per conversation, taken by inbound storage, read-back, attestation, attestation verification (hold, request), takeover and escalation; lock order conversation → dock | Freshness must hold under concurrency, not only sequentially | Row locks on `chat_threads` (would give the ingest role UPDATE rights on threads); SERIALIZABLE everywhere (retries on every conflict) | `migrations/0006_serialize_per_thread.sql`, `tests/test_concurrency.py` |
| T37 | Production booking switch (review R0.8) | `SETUHAUL_BOOKING_ENABLED`, default off. The agent can understand, offer and read back, but cannot hold or request a slot until durable inbound delivery is in place and verified | Model integration can start without risking live bookings | Starting model work only after all step 4 work | `setuhaul/api/service.py`, `test_booking_switch_is_off_by_default` |
| T38 | Model provider switch (18 Nov 2026, AWS blocker B-AWS-0; hardened after the step 3 review) | **Superseded by T52 (21 Nov 2026): OpenRouter moved into the deployed package behind `model_for()`; the synthetic-data rule and fail-closed selection are kept.** Deployed code has one entry point, `production_model()`: India Bedrock only, explicit `SETUHAUL_ENV` required, any other provider refused. The AWS/OpenRouter switch is in `evals/providers.py`, outside the deployed package. OpenRouter (same Haiku 4.5 / Sonnet 5, prompts, schema and forced tool call) needs `SETUHAUL_ENV` in {dev, eval, ci}, `SETUHAUL_DATA_CLASS=synthetic` and a `SyntheticEvalOnly` capability, all re-checked against the real process environment; requests ask for no data collection (a preference, not a guarantee) | The AWS account cannot call Claude yet; accuracy and tokens transfer, latency and price do not. Residency must not depend on one variable being set correctly (review) | Running dev on `global.` Bedrock profiles; relaxing the India guard; a denylist of production names; Gemini (a different model) | `evals/providers.py`, `setuhaul/agent/models.py`, `tests/test_agent_units.py` |
| T39 | Appointment grace (owner decision OD-1, 18 Nov 2026; closes review S3-02) | The no-show grace is how long after slot start a truck may check in at the gate (strict deadline: check-in exactly at the deadline is on time). For feasibility, grace applies to the expected end of unloading + changeover only: at risk when `ready + occupancy > slot end + grace`. The independent `ready > slot start + grace` check (from the Phase 1 prototype) is removed | Matches Phase 1 `02_variables.md`; a truck that arrives late but still finishes within grace keeps its slot | Grace on dock arrival; grace on dock readiness | `domain/feasibility.py` (`find_options`, `is_no_show`), `tests/test_feasibility.py::test_od1_*` |
| T40 | Breakdown escalation (owner decision OD-2, 18 Nov 2026; supersedes `02_variables.md` "breakdown not fixed → safety") | Immediate danger, accident, injury, fire or threat → SAFETY escalation, TAKE_OVER (the agent stops until a person hands back). Unrepaired breakdown without identified danger → durable BREAKDOWN escalation, DECIDE mode (the agent continues within its boundaries). Safety status unknown → BREAKDOWN escalation plus one safety question; NO or danger words → SAFETY takeover. Repaired → normal ETA update and feasibility. An operational escalation never transfers ownership; a takeover does | Proportionate: a roadside puncture at a dhaba is not an emergency, but it still reaches a coordinator durably | Blanket safety takeover (Phase 1); no escalation for breakdowns | migration `0009_breakdown_escalation.sql`, `agent/extraction.py` (`SAFE_PLACE`, danger words, `Checked.breakdown`), `agent/orchestrator.py` (`_breakdown`, `_safety_answer`), `tests/test_orchestrator.py::test_od2_*` |
| T41 | Inbound duplicates and masking (18 Nov 2026, found by the B-EV-2 replay) | (a) A provider redelivery with a NEW message id is a duplicate when the same driver sends the same text in the same conversation within 2 minutes, for messages of more than three words. Short replies ("yes", "2", "haan ji") are exempt: drivers repeat them on purpose, and confirmations are protected by the attestation rules (T35). (b) Masking hides runs of 6+ digits (phone, licence) except the date inside an order reference (`ORD-260804-020`) | F1 asks for one reply and one exception per redelivery; masking every digit run hid the driver's own order reference from the model | Dedupe on id only (misses new-id redeliveries); dedupe every repeat (drops a genuine second yes) | `api/service.py` (`ingest`), `tests/test_api.py::test_redelivered_report_*`, `::test_masking_*` |
| T42 | Model spend in development and evaluation (18 Nov 2026, cost review) | Principle: ask whether a model call is needed before making it cheaper. Level 1 (default, free): the rules extractor, the scripted end-to-end replay, Pydantic checks, recorded model outputs, and deterministic grounding, safety and scheduling checks. Level 2 (paid, approved): live calls for affected scenarios only, held-out runs at milestones. Every paid runner estimates first, refuses runs over $0.10 without `--approved --max-usd`, checks each call (retries included) against one run budget, caches by model + prompt and schema version + prompt text + input, and records cost with its basis (computed / inferred) | 71 % of step 3 spend was full-set extraction reruns and live replays after fixes that touched few scenarios | Warnings instead of a hard cap; caching on exact input only (new option ids and dates every run would always miss) | `evals/budget.py`, `tests/test_budget.py`, `docs/operations/COSTS.md` |
| T43 | Reply language (18 Nov 2026, B-AI-3) | Replies exist in English, Hindi (Devanagari) and Hinglish (Latin). The language follows the conversation: Devanagari in the message means Hindi; a short reply (2 words or fewer) keeps the language of the driver's last longer message, detected in code from its words; otherwise the extractor's language for the message stands. Dock codes, times and references are written the same way in every language | The B-AI-3 checks found a Hindi conversation answered in English after a one-word "हाँ", and Hinglish drivers always answered in English | Language per message (the old rule); a model for Hinglish replies (closes the gap at a cost the templates now avoid) | `agent/replies.py`, `api/service.py` (`recent_driver_messages`), `tests/test_agent_units.py`, `tests/test_orchestrator.py` |
| T44 | Planning horizon (18 Nov 2026, B-FEAS-1) | Options come from the day the truck is ready and the next operating day. Each slot is checked against its own day's close and last-new-start time (21:00 approval rule). Later days are not offered: plans beyond the next morning change too much, and that is a planner's call | A truck ready at 23:00 got no options and was escalated, although the next morning had free slots | One day only (the old behaviour); an unlimited horizon | `domain/feasibility.py` (`find_options`), `tests/test_feasibility.py` |
| T45 | Inbound delivery semantics (18 Nov 2026, B-WF-1) | At least once to the queue, exactly one turn per message. The receipt row is the outbox: claim, send (deduplication id = WhatsApp id + generation, group = driver), then mark published. The orchestrator takes a database processing lease before a turn and marks the message processed after it; a delivery for a processed message is refused at any age. A recovery sweep re-publishes unpublished messages after 30 s and stalled ones after 6 min (beyond the 5-minute SQS window) | An in-memory append after the claim lost the message on a crash (B2-14). SQS FIFO deduplication alone lasts 5 minutes | Exactly-once queue delivery (not available); deduplication in the queue only | `migrations/0011_inbound_outbox.sql`, `setuhaul/api/inbound.py`, `Orchestrator.handle`, `tests/test_inbound_delivery.py` |
| T46 | Turn fencing and the booking switch (18 Nov 2026, review R1.9) | (a) A worker may write only while it holds a live lease for the message's current generation; every agent and feasibility write checks this under the conversation lock, and a heartbeat renews the lease every third of its length. (b) The webhook acknowledges only what it stored. (c) `SETUHAUL_BOOKING_ENABLED=true` starts only with a durable FIFO queue that has a dead-letter queue and a visibility timeout of at least twice the lease, and recorded evidence for AWS_QUEUE, AURORA, RECOVERY_WORKER and BOOKING_WORKFLOW in `deployment_gates`, which no application role can write | A lease alone let an expired or superseded worker keep committing; a flag could be switched on with an in-memory queue | Visibility timeout as the only guard; plain environment flags for the gates | `migrations/0012_turn_fencing_and_gates.sql`, `setuhaul/api/db.py`, `setuhaul/api/inbound.py`, `Orchestrator.handle` |
| T47 | Deployment evidence (18 Nov 2026, review R2.0) | Supersedes T46(c)'s gate table. Booking starts only with a current row per gate in append-only `deployment_evidence` for this environment and deployment id: unexpired (≤ 30 days), with artifact URI and SHA-256, and a configuration fingerprint that, for the queue and the database, must equal what the service computes at start-up. Six gates: AWS_QUEUE, AURORA, RECOVERY_WORKER, BOOKING_WORKFLOW, WORKLOAD_IDENTITY, OUTBOUND_DELIVERY. Only `setuhaul_deploy_evidence` can insert | A gate name written once stayed valid forever, for any deployment and any configuration | Plain gate flags; evidence without expiry | `migrations/0013_deployment_evidence.sql`, `setuhaul/api/inbound.py`, `DEPLOYMENT_GATES.md` |
| T48 | Evidence freshness and manifest binding (19 Nov 2026, review R2.1) | Extends T47. (a) An insert trigger checks evidence against the database clock: `verified_at` at most 5 minutes ahead, and expiry within the gate's window both from now and from verification. Windows: RECOVERY_WORKER and OUTBOUND_DELIVERY 7 days, other gates 30 days. The start-up check applies the same rules. (b) The deployment id is derived from an immutable deployment manifest shipped with the artifact. Each gate's evidence must carry a fingerprint: computed live for the queue (now including its DLQ) and the database, and from the gate's manifest section for the other four. (c) The service refuses to start if an evidence trigger is disabled | A future `verified_at` stretched evidence's life. Four gates were bound only to a hand-typed deployment id | Service computes all fingerprints live (step 6 drift detection instead); a single 30-day window | `migrations/0014_evidence_freshness.sql`, `setuhaul/api/inbound.py`, `DEPLOYMENT_GATES.md` |
| T49 | Warehouse approval boundary (19 Nov 2026, owner-approved design B-WF-2 revision 3) | The agent only asks. A request needing approval is confirmed only by `decide_booking_request`, run by `setuhaul_console` with a staff identity recorded by `begin_staff_context` for that transaction. The function re-proves every booking invariant and decides the deadline with `clock_timestamp()` after its locks. The request and its claim share one deadline; capacity loss invalidates in the same transaction. Outcomes: APPROVED, REJECTED, EXPIRED, INVALIDATED, WITHDRAWN, SUPERSEDED, each with one `ApprovalDecided` event and an audit principal. Policy confirmation is a separate, versioned rule path that ships NEEDS_PLANNER and is never recorded as an approval. Step Functions only waits | The approval boundary must be fixed before B-WF-2a and B-WF-3 build on it | Approval in the workflow; principal as a function argument; `now()` deadlines | `docs/2-design/B-WF-2_approval_design.md`, `migrations/0015_approval_workflow.sql` |
| T50 | Confirmation rule rows (19 Nov 2026, B-WF-2a) | Per facility, versioned and append-only: person-always (safety, contradictory data), planner (after last new start, after hours, dock downgrade, move earlier, busier time, critical, reefer) and auto-eligible safe cases (new booking, move later). The database computes the inputs. Person-always and request-kind floors hold regardless of rule order. Policy confirmation needs `auto_eligible` and `authorised_by` and no risk flag. Every row ships NEEDS_PLANNER or PERSON_ALWAYS (Q1). The 17 Nov "GGN night automatic" line awaits an explicit decision | Implements the 17 Nov risk policy within Q1 | A single per-shift switch (retired in 0015) | `migrations/0016_confirmation_policy.sql`, `docs/2-design/B-WF-2_approval_design.md` §20 |
| T51 | Outbound delivery semantics (19 Nov 2026, B-WF-3) | At least once, with at most one same-id resend per notification; never claimed exactly once. The notification id is derived from (event, channel, recipient) and is the provider idempotency key where supported; WhatsApp has none, so our id travels as `biz_opaque_callback_data` for receipt matching. Agent replies are sent from the stored row, verified by hash. Terminal failures escalate (NOTIFICATION_FAILED) | Send what was stored and audited; never silently lose or multiply a message | Regenerating the reply at send time; blind retries after timeouts | `migrations/0017_outbound_delivery.sql`, `docs/2-design/B-WF-3_outbound_design.md` |
| T52 | Model provider for the synthetic Sydney FDE pilot (21 Nov 2026, Phase B) | **OpenRouter is the primary provider; Amazon Bedrock is optional.** Extraction model `anthropic/claude-haiku-4.5`, pinned to ONE upstream provider, `amazon-bedrock` (verified on OpenRouter's zero-data-retention list, safety retention off), with `provider.order` and `provider.only` naming it, `allow_fallbacks: false`, `zdr: true`, `data_collection: deny`, `require_parameters: true` and no model fallback list. A response from any other provider is refused; if the pinned route cannot serve the request OpenRouter answers 503 and the turn uses the rules reader, never another provider. One entry point, `model_for()`: environment dev/eval/ci/staging/pilot only (`production` refused), `SETUHAUL_DATA_CLASS=synthetic` mandatory, provider named explicitly. Staging and pilot read the key from AWS Secrets Manager (`SETUHAUL_OPENROUTER_SECRET_ID`; an environment-variable key is refused there) and meter every attempt in the usage ledger with an application-level daily budget (migration 0023, `SETUHAUL_LLM_DAILY_BUDGET_USD`), on top of the OpenRouter account limit the owner sets. Bedrock: region `ap-southeast-2`, `au.` geographic profile; not metered yet, so dev/eval/ci only. AWS Sydney stays the fixed infrastructure region | Synthetic data only, so India-only inference is not a constraint for this pilot; the Sydney account cannot use India regions (B-AWS-0); OpenRouter is the provider already used for every evaluation run | OpenRouter default routing (provider may change per request); Bedrock-only pilot (blocked: Claude not enabled in the Sydney account on 18 Nov); India regions (not available to this account) | Phase B report; `tests/test_providers.py`; OpenRouter `/models/anthropic/claude-haiku-4.5/endpoints` and `/endpoints/zdr` (21 Nov 2026) |
| T53 | Facility approval mode (27 Nov 2026, migration 0047) — **APPROVED by the owner, 27 Nov 2026**, within these boundaries: (1) every facility defaults to MANUAL; (2) only authorised planners and ops leads change it, for facilities in their scope; (3) switching to AUTO needs a reason and is audited with the previous and new values; (4) AUTO confirms only bookings the existing rules classify as safe (auto-eligible); (5) mandatory human-approval conditions always override AUTO; (6) the mode is captured when the booking request is submitted, and later changes never alter existing requests; (7) existing permissions, double-booking prevention, duplicate-request protection and notifications stay intact; (8) this approval does not authorise production activation, a deployment, applying 0047 to a shared or live database or enabling AUTO for any facility, and does not override existing release authorization requirements | Each facility has a mode, MANUAL (default; every facility starts MANUAL) or AUTO, changed only by a planner or ops_lead in scope through `set_facility_approval_mode` (reason required, versioned, audited as SET_APPROVAL_MODE). In AUTO, a driver's confirmed selection is confirmed at once through the 0016 policy-confirmation path (CONFIRMED, BookingConfirmed, AUTO_CONFIRM_BOOKING by `approval-mode:<facility>@v<n>`) only when the matched rule is auto-eligible (safe-new-booking, safe-move-later), the kind is STANDARD, no risk flag is set and it is not a dispatcher offer; PERSON_ALWAYS, every planner rule and every risk flag still win. The mode is read and recorded when the selection is submitted. **Change to the Q1 boundary (19 Nov 2026, T50):** Q1 allowed automatic confirmation only through a new POLICY_CONFIRM rule version naming its authorisation (`authorised_by`, a decision-register reference) and kept every row NEEDS_PLANNER. With T53, a planner or ops_lead can switch the auto-eligible rows to automatic confirmation for a facility from the console, without a new rule version or a decision-register reference; the authorisation becomes the audited mode change. The owner approved this change to Q1 on 27 Nov 2026 within the boundaries above; every facility still starts MANUAL | Planners asked to auto-confirm safe driver selections without a code change | Rule-version authorisation only (Q1 as is); owner-only switch | `migrations/0047_facility_approval_mode.sql`, `tests/test_facility_approval_mode*.py` |
| T54 | Appointment lifecycle and automatic no-show (27 Nov 2026, fix plan issue 3, migration 0048). Owner decisions D1–D3, Q1, Q4 | CONFIRMED → (gate arrival) → AT_GATE → (at dock) → IN_PROGRESS / IN_DOCK → (completed) → COMPLETED, each step one database function (operations or ops_lead only, D1; facility scope; only the next legal state; idempotent; audited in booking_events). Gate arrival is accepted while now ≤ start + the facility's own `no_show_grace_min` (Q4); after that the recovery worker marks the appointment NO_SHOW every minute (D2) and releases its dock time. One boundary function decides both. Arrival and the job lock the appointment row (the job with SKIP LOCKED) and the job's transition is conditional, so the two can never both win. Completion releases the rest of the dock time (the claim ends; a release is never a booking commitment, so it works with booking off) and tells the driver. After a no-show the shipment waits for the dispatcher (D3): Rebook (back to Needs booking; a planner approves the new booking) or Request cancellation (a planner decides); until then the database refuses any hold or offer for it. A gate arrival closes a time change still waiting for a planner (INVALIDATED). Q1: the backlog is cleared in one go, but only by the owner-run backfill, which records a dry-run report first and applies exactly the reviewed set; the minute job covers deadlines after the 0048 cutover only. Eligible = current CONFIRMED with no arrival, dock activity, completion or cancellation (open or approved) | Nothing moved a booking past CONFIRMED: past bookings stayed CONFIRMED forever, cancellation after the start was refused and a driver could never rebook | Manual no-show (D2 chose automatic); rebook automatically (D3: the dispatcher decides); backlog in the minute job (would act before a dry run) | `migrations/0048_appointment_lifecycle.sql`, `setuhaul/api/lifecycle.py`, `scripts/no_show_backfill.py`, `tests/test_lifecycle_0048.py` |
| T55 | Dispatcher time changes and reassignment (27 Nov 2026, fix plan issues 4–7, migration 0048). Owner decision Q2 | A dispatcher can ask for a new time for a CONFIRMED booking before its start: the slot is claimed at once (an APPOINTMENT claim, so no competing booking can take it) and becomes a PLANNER_REVIEW request that replaces the booking only when a planner approves (decide_booking_request, unchanged: the old booking is cancelled and the new one confirmed in one transaction); rejection or expiry releases the new claim and leaves the booking as it was. Always a planner (Q2), whatever the confirmation rules say, and the person who asked can never approve it. Offers name their target explicitly (This driver / Another driver); "Another driver" can never be the assigned driver (refused by the API and the database). Free drivers = active, same carrier, not at a gate or in a dock, no live booking within 2 h of the slot (completed and no-show trips no longer count), no open offer for another shipment. The existing assignment stays until the new driver accepts (0036, unchanged). Open escalations needing a decision appear in "Pending your decision" with their due time; a SAFETY escalation shows a banner on the shipment | Confirmed bookings could not be moved by a dispatcher; one offer went to the assigned driver from the wrong button; the planner did not see a driver's request held by a safety escalation | Dispatcher moves under the driver rules (Q2 chose always a planner); a free-text driver id | `migrations/0048_appointment_lifecycle.sql` (request_reschedule, free_drivers_for, create_dispatch_offer_to), `tests/test_dispatch_0048.py` |
| T56 | Requested dates (27 Nov 2026, fix plan issue 9; extraction schema v2, prompt p6) | The model copies the day the driver wrote (`requested_date_local`); the rules reader finds the same phrase; code alone (`setuhaul/agent/dates.py`) turns it into a date in the facility's configured zone (`facilities.timezone`). Explicit dates up to 7 days ahead, today included. A past, too far, unclear ("next Friday") or contradictory day is answered as such and asked again; a day the model missed but the driver wrote still counts; options are searched on that day only and "no slot on 28 Nov" is said as it is: another day is never substituted. RESCHEDULE is its own intent. The service re-checks the 7-day rule itself (the agent requests; the app decides). Exception to T44 for a driver-named day only | "Reschedule to 28 Nov" was offered 27 Nov: times were read as the next occurrence within 24 h and there was no date field | Let the model compute dates (non-deterministic, unauditable); refuse all dates | `setuhaul/agent/dates.py`, `agent/extraction.py`, `agent/rules_extractor.py`, `domain/feasibility.py`, `tests/test_dates_0048.py`, `tests/test_reschedule_dates_0048.py`. **Golden extraction evaluation re-run pending owner approval (paid)** |
| T57 | Full trace content for the synthetic pilot (27 Nov 2026, fix plan issue 8). Owner decision Q3 | `SETUHAUL_TRACE_CONTENT=full`, honoured only when `SETUHAUL_ENV=pilot` and `SETUHAUL_DATA_CLASS=synthetic` (anywhere else it falls back to off; the CDK stack refuses it outside the pilot) and the pilot default from this release. Exported, after the scrub: driver messages and context, the model's per-turn input and raw output, prompt and schema versions (the fixed instructions by version only, never their text), tool calls and full results, rule outcomes and the decision path, correlation ids. Always removed: credentials, tokens and secrets (by key and by pattern), and driver identifiers: phone numbers, e-mails, names, chat and user ids, usernames, coordinates (by key), plus the turn's own driver name, phone and Telegram ids wherever they appear in free text. Operational data that is not driver PII (facility, dock, times, order and shipment ids, statuses, reasons) stays readable. Values up to 4,000 characters. The export guard drops any turn that still matches a pattern or a registered identifier. Access and retention: `docs/observability/TRACE_CONTENT.md` | Traces showed almost nothing, so a turn could not be debugged from LangSmith | Content on (text only, 300 characters); prompts included (the fixed instructions are versioned code) | `setuhaul/observability/redaction.py`, `observability/langsmith.py`, `infra/setuhaul_stack.py`, `tests/test_trace_content_full.py` |
| T30 | Release gate | Golden set + zero hard-gate failures + security, concurrency and F1–F8 tests + cost ≤ $0.08 per resolved exception | One objective ship / no-ship rule | Manual sign-off only | `07_…` |
| T25 | ML boundary | No ML in v1. ETA prediction (probabilistic) may later adjust confidence only; availability stays deterministic | No labelled history yet | Predictive ETA in the booking path | `06_review_response.md` |

## Open items needing SetuHaul input

| ID | Item | Default until answered |
|---|---|---|
| U1 | Warehouse confirmation TTL per facility and shift | JAI 15 / 10, GGN 15 / 0 |
| U6 | Are the driver and vehicle double-assignments real? | Escalate as contradictory data |
| U7 / D7 | Priority weights, `max_yard_wait_min`, penalty policy | 8 / 4 / 2 / 1, 90 min |
| U10 | Gate-to-dock and changeover per facility | Seeds; add a `called_to_dock_ts` capture |
| U11 | Tracking vendors and prices | Tiered plan; quotes needed |
| U12 | Data-residency sign-off | **For the synthetic Sydney pilot: not required (T52).** For a real-data India deployment: **Build verification pending.** Designed for India-only processing (T4, T24); runtime proof (B1–B4: profiles, IAM deny on `global.`, 100 % `in.` ARNs in traces, no other AI service receiving driver data) and formal sign-off are outstanding. Choosing an India profile is not proof that every model-backed operation stays in India |
| HITL | Escalation SLAs and safety wording | 10 / 5 min; placeholder text |
