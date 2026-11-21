# Phase 2 sign-off checklist and Phase 3 backlog

> Response to the third and fourth external reviews (17 Nov 2026). Fourth review verdict: "I approve the current Phase 2 architecture baseline, subject to the three corrections above." All three are now made and tested (S16, S6, S8).
> This file is the bridge from Design to Build. Every Build item links to the acceptance test that closes it.
> Status key: **Proven** = executed evidence in this repo · **Documented** = design text, no execution needed · **Gate** = must be proven in Build before the MVP ships.

## 1. Phase 2 sign-off checklist

| # | Item | Status | Evidence |
|---|---|---|---|
| S1 | Deterministic orchestration: Haiku extracts, Python chooses the operation, Sonnet phrases, templates as fallback | Documented | `04_tool_contracts_and_states.md`, `05_agent_observability_cost.md` |
| S2 | One truck per dock, one dock per truck, no overlap | **Proven** | `validation_results.md` §2–§4 (0 overlaps in 150; truck exclusion test) |
| S3 | Concurrency: exactly one winner per slot | **Proven** | 10 / 50 / 200 parallel holds, 0 deadlocks |
| S4 | RLS on all 16 facility- or driver-owned tables, reads and writes, no bypass roles, fail closed | **Proven** | §5 + §5b: 54 checks, including driver-or-facility checks for `eta_signals`, `driver_exceptions` and `tracking_consents` |
| S5 | RLS coverage cannot silently regress | **Proven** | §5 coverage check: every table is RLS-forced or on a reasoned allowlist |
| S6 | RLS on the TMS / WMS mirror tables (`shipments`, `appointment_slots`, `dock_events`) | **Proven** (fourth review) | §5b: 15 checks, including dependent access and the fail-closed compatibility trigger |
| S7 | Hold expiry releases capacity | **Proven** | §6 |
| S8 | One notification per event and channel; scheduled retries; UNKNOWN state; recovery resend at most once | **Proven** (schema) | §6 and §8 (14 checks); delivery semantics in `07_…` §1C (no exactly-once claim) |
| S9 | Booking sequence SELECT → READ_BACK → EXPLICIT_YES → HOLD → SUBMIT → WAREHOUSE | Documented | `07_…` §1D, `04_…` |
| S10 | India-only inference | **Gate (B1–B4)** | Designed (T4, T24); register status changed to "Build verification pending" |
| S11 | Target-engine compatibility (Aurora PostgreSQL 18.3+, decision T33) | **Gate (G1 b)** | Same major version as the local evidence; Aurora run in step 6 |
| S12 | Golden dataset (60 scenarios) exists, versioned, labelled | **Gate (G3)** | ✅ Built 18 Nov 2026, before prompt tuning: `evals/golden/v1/` (reviewer sign-off pending) |
| S13 | Cost per conversation / per resolved exception | **Gate (G4)** | Estimates are hypotheses until measured |
| S14 | Driver / vehicle assignment validity | Documented (§3) | DB constraint vs TMS validation separated |
| S16 | Least-privilege database grants (fourth review P0) | **Proven** | §7: 20 checks; decision T31 |
| S15 | Documentation consistency | Done | 23 tables everywhere; one cost baseline ($440 / $2,060); rename header fixed (`confirm_booking` → `submit_booking_request`); residency marked pending |

**Phase 2 is frozen** with S10–S13 carried as tracked gates. Any change to `schema.sql` or a decision after this point needs a register entry and a validator run.

## 2. Engineering gates added by the third review

### G1. Target-engine compatibility (Aurora PostgreSQL 18.3+; target changed from 16 by decision T33)

- **Why:** community PostgreSQL 18 evidence does not prove behaviour on Aurora, which has its own storage, I/O and parameter differences.
- **Target changed (17 Nov 2026, T33):** Aurora PostgreSQL 18 is generally available (June 2026) and the RDS Data API supports 18.3 and later in Mumbai and Hyderabad. The target is now Aurora PostgreSQL 18, the same major version as all local evidence, so part (a) below (a local PostgreSQL 16 run) is no longer needed.
- **History:** the earlier static scan for PostgreSQL 17 / 18-only syntax (none used) was evidence for a 16 target. With T33 it is no longer needed, but it also means nothing ties the schema to 18-only features.
- **Pass condition (revised for T33):** `validate_schema.py` **and** the test suite (`tests/`) pass unchanged against the provisioned **Aurora PostgreSQL 18.3+** cluster, through the RDS Data API transaction pattern. Results are committed as `validation_results_aurora.md`. Part (a), a local PostgreSQL 16 run, is withdrawn.
- **Evidence today is local only:** community PostgreSQL 18.6 on a developer machine. It proves the logic and the constraints; it does not prove Aurora's storage, I/O, parameter groups, extension allow-list or Data API behaviour.
- **Also check on Aurora:** `btree_gist` is in the allowed extension list; contention numbers through the Data API (latency, not just correctness).

### G2. Notification delivery semantics

- Stated in `07_…` §1C as a layer table. **No exactly-once claim is made anywhere.**
- **Database part done (fourth review):** the resend-once rule is enforced by the schema and tested (validator §8). What remains is the worker and provider behaviour below.
- **Pass condition (F7 extended):** stub provider scenarios (5xx, timeout-then-accepted, timeout-then-lost webhook) produce at most one duplicate per notification, each with the same reference; every critical-message timeout pages a person; zero notifications stuck in SENDING beyond `sending_timeout`.

### G3. Golden dataset in the repository

- **Location:** `evals/golden/v1/` in the Build repo, under version control. Changes need review and bump the version.
- **Composition (60):** 13 brief beats · 10 delay-ladder cases (on time → > 4 h) · 12 seed threads replayed (THR001–THR012) · 10 audit traps (drawn from A04, A08, A11, A12, A18, SHP1006 / SHP1021 conflicts, APT1004 / 1005 / 1014A / 1015A) · 8 Hindi / Hinglish / noisy · 7 adversarial (injection, priority spoofing, cross-driver requests).
- **Each scenario carries:** input messages, DB fixture id, frozen `NOW`, expected extraction (`record_driver_report` JSON), expected operation sequence, expected DB end state, hard-gate labels, reply rubric labels. No real personal data.
- **Pass condition:** the set exists before any prompt is tuned; every run records the dataset version in its trace.

### G4. Cost gate: four metrics, measured separately

| Metric | Definition | Hypothesis | Source |
|---|---|---|---|
| Cost per inbound conversation | All model + Guardrails tokens for a thread, ÷ threads | ≈ $0.04 | Inference spans (T28) |
| Cost per resolved exception | Thread cost for exceptions closed without a human, ÷ those exceptions | ≤ $0.08 (gate) | Spans + exception status |
| Cost per successful rescheduling | Thread cost where the warehouse approved a new slot, ÷ approvals | Measure | Spans + `appointments` |
| Cost of escalations and retries | Tokens spent on threads that escalated, plus retried / failed calls | Measure; report as % of total | Spans with `retry` / `escalated` flags |

AWS fixed costs ($440 / month baseline) are reported beside these, not mixed into them. The LLM-vs-code ablation (`07_…` §4) uses the same metrics.

## 3. Driver / vehicle assignment: what the database does and does not prove

Two different controls, often confused:

| Control | Question it answers | Enforced by | Failure handling |
|---|---|---|---|
| `ex_one_dock_per_truck` | Can the same truck hold two overlapping dock claims for different shipments? | **Database** (exclusion constraint; vehicle copied from the shipment by trigger) | Insert rejected → LOST / escalate |
| Assignment validation | Is this driver, and this truck, really assigned to this shipment? Is the truck double-assigned in the TMS? | **Upstream data** (TMS), checked by `assignment_conflict` before options are offered | No bookable options; escalate CONTRADICTORY_DATA (F3) |

The constraint only prevents conflicting **occupancy**. It cannot detect a wrong assignment, a truck assigned to two in-transit shipments with non-overlapping slots, or a driver messaging about a shipment that the TMS has re-assigned. Those are data-quality problems surfaced by `assignment_conflict` and the audit checks (A-series), and resolved by a person.

## 4. Phase 3 backlog, in build order

Build the workflow first, then add infrastructure in the order that proves each decision.

| Step | Item | Closes | Acceptance test |
|---|---|---|---|
| **1. Database and rules** | ✅ B-DB-1 Migrations: forward-only runner, checksums, immutable once applied | — | `tests/test_migrations.py` |
| | ~~B-DB-2 PostgreSQL 16 run~~ Not needed: target is Aurora PostgreSQL 18 (T33) | G1 (a) | — |
| | ~~B-RLS-2~~ Mirror-table RLS: **done in Design** (fourth review) | S6 | Validator §5b |
| | ✅ B-DB-5 (except `move_appointment`, deferred to the scheduling tool) Remaining mutation functions on the T31 pattern: `submit_booking_request` (agent); `decide_approval`, `block_dock`, `set_automation_paused`, `move_appointment` (console) | S16 | Validator §7 extended: each callable only by its role, RLS applies inside |
| | ✅ B-DB-3 `is_feasible()`, `fuse_eta()` in Python with property tests | D-series | Differential test: identical to the Phase 1 prototype on every shipment × 24 scenarios |
| | ✅ B-DB-4 Booking state machine + idempotency keys | S9, F1 | `test_booking_flow.py`, `test_db_requests.py` (thread replay of THR001 / THR009 moves to step 3, with the agent) |
| **2. Deterministic backend** | ✅ B-API-1 FastAPI, tool contracts, RBAC / ABAC | F8 | `tests/test_api.py`: GGN cannot read or decide JAI; 9 bad-token cases rejected |
| | ✅ B-API-2 Driver identity + shipment linking | F3, §3 | Identity from the verified webhook only; double-assigned driver → CONTRADICTORY_DATA |
| | ✅ Review R0.9 B2-16 / B2-17 (migration 0010, 18 Nov 2026): `bind_thread_shipment()` itself checks the session driver; `resolve_thread_shipment()` reads, chooses and binds the shipment under the conversation lock and returns the authoritative one; publishing options re-checks the binding (STALE_SHIPMENT) | R0.9 | `test_concurrency.py::test_b2_1*` (4), `test_api.py::test_b2_17_*` (2, through `BookingService`), `test_orchestrator.py::test_b2_17_*` |
| | ✅ Step 2 round 3 (migration 0007, review R0.8): shipment switching locked, lock on release and hand-back, 64-bit lock key, production booking switch (off) | R0.8 B2-13, P2 | `tests/test_concurrency.py`, `test_api.py` |
| | ✅ Step 2 concurrency correction (migration 0006, review R0.7): per-conversation lock; atomic enqueue claim | R0.7 P0 | `tests/test_concurrency.py` |
| | ✅ Step 2 final corrections (migration 0005): attestation freshness (a later reply always overrides a YES), `setuhaul_ingest` and `setuhaul_feasibility` roles, webhook-retry recovery and reconciler, central escalation events | Final review P0-1, P0-2, P1-1, P1-2 | `docs/3-build/README.md` §Step 2 final corrections |
| | ✅ Step 2 review corrections (migration 0004): attested confirmation, ETA provenance, real takeover, central queue, idempotent ETA and escalation, aware datetimes, console role matrix, no token in webhook responses | P0-1…3, P1-4…8 | `docs/3-build/README.md` §Step 2 review |
| | ✅ B-API-3 Options, read-back, explicit yes, hold, submit | F2, F4 | 50 drivers through the API: 1 HELD, 49 LOST with fresh options. The hold now also needs the driver's yes in the database (migration 0003) |
| **3. Agent integration** | B-AI-0 India-profile spike | **S10 / B1–B4** | Exit criteria in `07_…` §1B |
| | ◐ B-AI-1 Haiku extraction: on G3 (via OpenRouter, T38) p2 scores 65/66 with every gate at 1.00; to repeat on the India profile (B-AWS-0) and a held-out set | — | ≥ 95 % precision / recall on G3 |
| | ✅ B-AI-2 Python orchestration + Sonnet phrasing + grounding check + templates (scripted model) | F5 | `tests/test_orchestrator.py` |
| | B-AI-2a REQUIRES_HUMAN from any tool (including BOOKING_DISABLED) becomes a durable `escalate_to_human` before the driver is told a coordinator will help (R0.9 B2-18) | R0.9 | Integration test: escalation row exists whenever the reply promises a coordinator |
| | ✅ B-AI-3 **Superseded by OD-3 (21 Nov 2026):** no phrasing model in production; the Sonnet arm and stage 5 scoring were not run; templates reviewed by a native speaker (S4-R2-08). History: Ablations: rules vs Haiku decided (keep Haiku). Templates vs Sonnet, free stages done (`evals/b_ai_3/`): phrasing applies to option lists only. After the Hinglish templates and conversation-level language (T43), the templates pass all five checks on 20/20 situations. Proposed, pending the human review: remove Sonnet from option replies. The Sonnet arm and judge need approval | `07_…` §4 | Decision recorded per message type |
| | ✅ B-FEAS-1 Overnight arrivals: each slot is now judged by its own day's close and last-new-start time, and the search covers the ready day and the next one. A truck ready late at night is offered the first morning slot (shown with its date), without the 21:00 approval flag. Same-day behaviour is unchanged (Phase 1 differential test passes) | Found in step 3 | `test_feasibility.py::test_b_feas_1_*` (3); the time-of-day skip in `test_api.py` is removed |
| | ✅ B-COST-1 Spend control (T42): estimate first, $0.10 hard cap, per-call budget, result cache, cost in every result | Cost review | `tests/test_budget.py`; `COSTS.md` |
| | B-COST-2 Compact extraction output (only the fields that have values, normalised to the full 15-field contract in code) and Haiku batch for offline runs. Target 100–150 output tokens per call, and batch at least 45 % cheaper. Needs a paid, approved measurement on a frozen held-out set (a new h2, since h1 would then guide the change) | Cost review | No meaningful accuracy regression; safety fields checked one by one |
| | ✅ B-EV-1b Held-out set h1 (167 variants by a prompt-blind generator, frozen before evaluation): Haiku p3 158/167 (95 %), all gates pass; rules 99/167. p3 is not overfitted | `07_evaluation_plan.md` §2 | Gates hold on variants |
| | ⛔ B-AWS-0 **(21 Nov 2026: still open; the synthetic-data pilot goes to Sydney with OpenRouter as primary provider and Bedrock optional, so it no longer blocks the pilot.)** An AWS account that can call Bedrock in ap-south-1. The current account is locked to ap-southeast-2 and Claude access is not enabled. Interim plan: an eval-only `au.` run on synthetic G3 data, recorded as an exception | Blocks B-AI-1 accuracy, B-AI-3, step 6 | `in.` Converse call succeeds from ap-south-1 |
| **4. Workflow and humans** | ✅ B-WF-1 (local, migration 0011, decision T45): the inbound receipt is a transactional outbox; a relay publishes it with deduplication id = WhatsApp id and group = driver, then marks it published; the orchestrator takes a processing lease and marks it processed; a recovery sweep re-publishes stored-but-unpublished and stalled messages. `LocalQueue` locally, `SqsFifoQueue` for AWS (checked with a stub; a live SQS run is step 6). B2-14 and B2-15 closed. **All four live-booking gates (B2-14…17) are closed in code; the switch stays off until the review agrees** | F1 | `tests/test_inbound_delivery.py` (8): 4 simultaneous HTTP deliveries → 1 message, 1 event; crash between claim and publish loses nothing; duplicate after 6 min still rejected; crashed turn and dead worker redelivered |
| | ✅ Review R1.9 (migration 0012): webhook answers 503 when a message was not stored (S4-01); processing lease renewed by a heartbeat, claimed per generation, and every turn write fenced under the conversation lock (S4-02/S4-03); 0011 cutover runbook and checker (S4-04); booking switch fails closed without a verified FIFO queue (DLQ, visibility ≥ 2 × lease) and evidence for four deployment gates (S4-05); audit principal from the session (S4-07); one reply per inbound message (S4-08); Hinglish register made respectful (S4-09, native review still open); review sheet asks whether the reply answers the driver (S4-10) | R1.9 | `tests/test_inbound_delivery.py` (16), `test_concurrency.py::test_s4_07_*`, `RUNBOOK_0011.md` |
| | ✅ S4-06 Next operating day (T44 as written): closed days are skipped within a 7-day window; a truck ready on Friday night at a facility closed at the weekend is offered Monday morning. Drivers get one operating day; `operating_days_ahead` is there for a planner view | R1.9 | `test_feasibility.py::test_s4_06_*` (3) |
| | ✅ Review R2.0 (migration 0013): a worker can mark a message processed only with a live lease (S4-R2-01); the cutover checker lists every backfilled receipt, with later replies as evidence, not as an exclusion (S4-R2-02); reopen only touches backfilled receipts (S4-R2-03); deployment evidence is append-only, bound to environment and deployment, expires within 30 days, carries an artifact hash and a configuration fingerprint checked against the live queue and database, and is written only by `setuhaul_deploy_evidence` (S4-R2-04/05); six gates (S4-R2-06, `DEPLOYMENT_GATES.md`) | R2.0 | `tests/test_inbound_delivery.py` (21), `test_agent_not_operator.py::test_deploy_evidence_role_can_only_insert_evidence` |
| | ◐ Still open from R2.0: S4-R2-05 the pipeline identity's mapping to `setuhaul_deploy_evidence` (step 6, WORKLOAD_IDENTITY gate); S4-R2-07 the outbound worker sends the stored reply row (B-WF-3); S4-R2-08 human and native-speaker review | R2.0 | — |
| | ✅ Review R2.1 (migration 0014): evidence cannot be future-dated; every insert is checked against the database clock (S4-R3-01). Per-gate validity: 7 days for operational evidence (recovery worker, outbound delivery), 30 for configuration. All six gates are bound to fingerprints. The four the service cannot inspect come from an immutable deployment manifest, and the deployment id is derived from that manifest (S4-R3-02). The service also checks the DLQ target, its FIFO setting and its retention, and refuses to start if an evidence trigger is disabled | R2.1 | `tests/test_inbound_delivery.py` (26) |
| | ◐ Still open from R2.1: drift detection between the manifest and the live AWS resources, SQS least privilege, and the pipeline identity mapping (all step 6); S4-R2-07 (B-WF-3); S4-R2-08 (owner) | R2.1 | — |
| | B-WF-2 Step Functions approval (`waitForTaskToken`), expiry, T27 rules | F6 | Shortened TTL → EXPIRED, no auto-approval |
| | ✅ B-WF-2 built locally (19 Nov 2026, migration 0015, decision T49), design revision 3 approved by the owner: one OPEN approval per request, bound to its claim and one deadline; decisions only through `decide_booking_request` with a trusted staff identity (`begin_staff_context`, console role only); `clock_timestamp()` after locks; invalidation on capacity loss; withdraw vs supersede; operational holds; policy rules shipped NEEDS_PLANNER; one `ApprovalDecided` per outcome; idempotent consumers; `LocalApprovalWorkflow` + ASL. Real Step Functions is step 6 | B-WF-2 | `tests/test_approvals.py`, `TRACEABILITY.md` |
| | B-WF-2a Risk-based confirmation policy (owner decision 17 Nov 2026): replace the single per-shift `auto_confirm_chat_bookings` switch with a per-facility rule table. Automatic: same facility, normal hours, compatible dock, free slot, no other booking moves, or a move later into an empty slot. Planner: after last new start, moving a confirmed booking earlier or into a busier time, critical or reefer loads, dock downgrade. Person always: contradictory data, safety. Start GGN night shift on automatic for the safe cases. Track planner override rate | Planner overhead | One test per rule row; override-rate metric reported |
| | ✅ B-WF-2a built locally (19 Nov 2026, migration 0016, decision T50): the rule rows above per facility; the database computes the risk inputs; person-always and kind floors hold regardless of rule order; policy confirmation needs an auto-eligible, authorised rule version and no risk flag; override rate per rule (`policy_rule_outcomes`, `GET /console/policy/outcomes`). Q1 kept: every row ships NEEDS_PLANNER or PERSON_ALWAYS; GGN night automatic is **not** applied (owner decision needed) | B-WF-2a | `tests/test_confirmation_policy.py` |
| | ✅ B-WF-3 built locally (19 Nov 2026, migration 0017, decision T51): outbound notifications from stored agent replies and approval outcomes; stable ids; the stored reply is sent and checked against its hash (S4-R2-07); retries with backoff; ambiguous sends resolved by receipt or one same-id resend, never a third; reconciliation of lost answers; provider idempotency where supported (WhatsApp: none, so at most one identical repeat). Real WhatsApp and approved templates in step 6 | B-WF-3 | `tests/test_outbound.py`, `docs/2-design/B-WF-3_outbound_design.md` |
| | ✅ End-to-end flow tests (19 Nov 2026, `tests/test_flows.py`): nine business flows from WhatsApp to the driver's phone through the real webhook, API, roles, database, orchestrator, workflow port and outbound relay; they found three defects, fixed in migration 0018 (safety message suppressed; duplicate read-back; duplicate policy confirmation) | Flows | `TRACEABILITY.md` § flows |
| | B-WF-3 Outbox relay + notification worker + stub provider | F7, G2 | G2 pass condition |
| **5. Evaluation and observability** | ◐ B-EV-1 Golden dataset v1: 60 scenarios, schema-valid, seed-checked, rules baseline run; needs review | **G3** | 60 scenarios, schema-valid, reviewed |
| | ◐ B-EV-2 End-to-end replay of G3 (`evals/replay.py`) through API, orchestrator, service and PostgreSQL: scripted 64/64 (in the suite as `tests/test_replay.py`), live Haiku 61/64, zero hard-gate violations; 12 bugs found and fixed. Still to do: the LLM judge and the same run on the `in.` profile (B-AWS-0) | T30 | Zero hard-gate failures |
| | B-EV-3 OpenTelemetry spans with T28 fields; cost metrics; and (review) Sonnet call success rate, template-fallback rate, grounding-rejection rate and retry rate, because a silent fallback can look healthy | **G4** | Four metrics reported per run; fallback metrics alarmed |
| | ✅ OD-1 Grace is finish-based (T39). Engine changed; boundary and no-show tests added | S3-02 | `test_feasibility.py::test_od1_*` |
| | ✅ OD-2 Three-tier breakdown escalation (T40). Migration 0009; operational escalation separate from takeover | H04 | `test_orchestrator.py::test_od2_*`, G3 v3 policy group |
| | B-EV-4 Locust + FIS fault injection | Phase 1 `06_…` | Targets in Phase 1 `06_…` |
| **6. AWS and demo** | B-AWS-1 Cognito, API Gateway JWT, IAM roles, Data API | — | Security suite |
| | B-AWS-2 Aurora PostgreSQL 18.3+ validator and Build test run through the Data API | G1 (b) | `validation_results_aurora.md` all PASS |
| | B-AWS-3 AgentCore Runtime deployment, IAM deny `global.` | S10 | 100 % `in.` ARNs in traces |
| | B-DEMO End-to-end MVP acceptance run | T30 | §5 below in one trace |

## 5. The demonstration

One scenario, end to end, in a single trace:

1. Driver: *"I'm running 90 minutes late. Can I get a slot after 12?"*
2. **Haiku** extracts intent and ETA: structured output, confidence, missing fields.
3. **Python** decides: fuses the ETA with other signals, validates the shipment and assignment, computes feasible slots.
4. Driver picks an option; the system reads back dock and time and waits for an explicit yes.
5. **PostgreSQL** atomically places the hold (idempotency key, advisory lock, exclusion constraints).
6. **Step Functions** waits for the warehouse; the planner approves in the console.
7. Driver receives the outcome: grounded reply, notification row, trace id.

Responsibilities shown: the LLM understands and communicates; Python decides the next operation; OR-Tools optimises; PostgreSQL enforces the truth; Step Functions manages long human waits.
