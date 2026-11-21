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

**Answer.** Verified phone (WhatsApp sender) or Cognito identity → `drivers.phone_e164` → live shipments (not CANCELLED or COMPLETED).
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

**Status:** Decided. Signal vendors are pending (U11).

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

**Answer.** Three layers:
1. **Per-request commits: first valid commit wins.** Integrity comes from the database. Speed of typing gives no business priority, because options are computed per driver from the same state.
2. **Priority is applied by policy, not by race.** When a CRITICAL or HIGH load has no option inside its useful window, the **facility scheduler** (CP-SAT) proposes a re-sequence that may move lower-priority **unconfirmed or at-risk** bookings. Moving someone's **on-time confirmed** promise needs planner approval.
3. **Fairness guard.** A hard `max_yard_wait_min` (default 90) stops the solver starving a waiting NORMAL or LOW truck.

The objective in plain words: less weighted waiting, fewer missed appointments, fewer changes to promises already made.

**Reason.** Fairness and service levels conflict.

**Evidence.** Policy tested on the competing trucks (SPK):
- CP-SAT: −32 % weighted wait against FCFS, with the fewest broken promises.
- "Priority first" did **worse** than FCFS (docks idle while CRITICAL trucks are still on the road).
- Unconstrained CP-SAT left SHP1004 waiting 139 min, hence the fairness guard.

**Status:** Decided. Weights and `max_yard_wait_min` are pending client confirmation.

#### D8. When does the facility schedule recalculate, what is fixed, and what is the objective? (optional extension)

**Answer.**
- **Recalculate** on any of: ETA change > 15 min, gate-in, dock event, cancellation or no-show, hold or pending expiry. Debounced to one solve per facility per 60 s.
- **Fixed:** trucks in a dock, until predicted end (`max(now, unload_start + p)`); bookings starting within the next 30 min; dock blocks.
- **Objective:** `Σ w·wait + α·Σ w·lateness + β·changed promises + γ·unscheduled`, with hard compatibility, release, no-overlap, last-start, latest-finish and max-yard-wait constraints. CP-SAT with a 2–5 s limit, falling back to the best dispatch rule.

**Evidence.** One replay: SPK (09:56 snapshot, 16 trucks, optimal in 1.4 s).

**Status:** Decided.

#### D9. How do we handle stale options, cancellations, duplicate messages and retries?

**Answer.**

| Problem | Handling |
|---|---|
| **Stale option** | `hold_option` re-checks against current state. If the option's `schedule_version` is older and it is no longer feasible, the driver hears *"D1 13:00 was just taken; here are the current options"* with fresh options |
| **Cancellation** | The occupancy row is released, `schedule_version` bumps, and the scheduler is triggered. Drivers holding expired options in that area are not auto-notified, but their next request sees the capacity |
| **Duplicate message** | Unique `(channel, external_message_id)`, plus a content-hash window for retries that arrive with new ids. Unique `dedupe_key` on exceptions |
| **Retries of tool calls** | Idempotency key (thread, tool, args, turn) returns the stored result |

**Evidence.** One replayed case for each:
- **Duplicate:** THR001 and THR009 (MSG015, 2 s apart) — the second insert is rejected by `dedupe_key` (VAL §4).
- **Cancellation:** APT1008 frees D2 10:00.
- **Stale option:** the contention test's 1,990 losers all received LOST, not an error (VAL §2).
- **Retry:** the idempotency table contract (`04_…`).

**Status:** Decided.

### FAIL SAFELY

#### D10. What happens when no feasible slot exists?

**Answer.** Escalate in **Decide** mode with a complete hand-off record. Tell the driver plainly what happens and by when. Never invent a slot.

**Evidence.** The SHP1015 escalation message: *"No reefer dock is free tonight because of planned maintenance from 6 PM. An operations coordinator is reviewing options and will message you by 16:45. Please continue to Jaipur safely."* The hand-off record is in `docs/1-frame/02_variables.md` §7. Delivery goes to the durable queue first; e-mail is a best-effort notification, so the OM004-style failure cannot lose it.

**Status:** Decided.

#### D11. What do we say when the preferred slot is not granted?

**Answer.** Give the reason in plain words and the best alternative in the same message. Never a bare "no".

**Evidence.** Two refusal messages:
1. *"D1 at 11:00 can't take your truck: your load is 21 t and D1 is limited to 20 t. The earliest dock that fits is D2 at 13:00 (unloading until 14:25). Shall I request it?"* (SHP1014)
2. *"Another truck confirmed D2 13:00 a moment ago. I can offer D4 at 13:00 or D2 at 14:00. Reply 1 or 2."* (contention loser)

**Status:** Decided.

#### D12. Which decisions need human approval or takeover?

**Answer.** 14 triggers across five modes (approve, decide, take over, confirm, oversee), each with an SLA and a safe timeout where silence is never approval. See `docs/1-frame/08_human_in_the_loop.md`.

**Status:** Decided. SLAs and safety wording are pending client confirmation.

### PROVE

#### D13. How will we prove the system did not double-book capacity?

**Answer.** Four layers:
1. **The database makes it impossible.** `EXCLUDE USING gist (dock_id WITH =, during WITH &&) WHERE active` covers holds, bookings, blocks and in-dock trucks.
2. **A per-dock advisory lock** gives clean losers instead of deadlocks.
3. **A concurrency test** in CI.
4. **A post-run checker** that self-joins active occupancy.

**Evidence.** On PostgreSQL 18:
- 10 / 50 / 200 parallel holds on one window × 30 / 20 / 10 rounds → **exactly one winner every round**.
- 150 random overlapping holds → **0 overlaps**.
- **0 deadlocks** after the advisory-lock fix. The first run surfaced 40P01 deadlocks, which led to the fix. (VAL §2–3.)

**Status:** Decided. The full-stack Locust contention test runs in the Break phase.

---

## Part B — Technology and architecture decisions

| # | Decision | Choice | Main reason | Rejected alternatives | Evidence |
|---|---|---|---|---|---|
| T1 | Agent platform | **Amazon Bedrock AgentCore** (Runtime, Gateway, Identity, Observability, Policy, Evaluations) in **ap-south-1**. **Memory deferred**: conversation and workflow state live in PostgreSQL and Step Functions | Managed agent hosting with session isolation, MCP tool gateway, built-in tracing and evals. All needed features are in Mumbai | Self-hosted on ECS (more ops); Bedrock Agents classic (less control of the loop) | AgentCore regions table |
| T2 | Agent framework inside Runtime | **Strands Agents** (Python) as the harness, with **deterministic orchestration**: Python selects every operation from the state machine; the LLM never chooses tools in v1 | AWS-native, first-class AgentCore and Bedrock support, Python like the rest | LangGraph (heavier for a small tool set) | — |
| T3 | Models | **India profile:** `in.anthropic.claude-haiku-4-5-20251001-v1:0` for extraction (forced tool, strict schema); `in.anthropic.claude-sonnet-5` for reply phrasing only; templates when no LLM is needed | Quality where it matters; Haiku costs ~20× less for structured extraction | One model for everything (cost); Opus (unneeded) | Claude in Amazon Bedrock docs |
| T4 | Model endpoint | **India geographic cross-Region inference** (ap-south-1 ↔ ap-south-2 only) on `bedrock-runtime` (Converse). Global profiles are blocked by IAM. Masking remains as defence in depth | Driver data stays in India. Costs the +10 % regional premium and means the previous model generation (Sonnet 5, Haiku 4.5), because 5.5 models are Global-only from Mumbai | Global Sonnet 5.5 / Haiku 5.5 | AWS India CRIS announcement (29 Sep 2026) |
| T5 | System of record | **Aurora PostgreSQL Serverless v2**, Multi-AZ, min 0.5 ACU in production (scale to 0 in dev and test) | Exclusion constraints, transactions, RLS. Scales with spikes | DynamoDB (cannot express range-overlap constraints, so integrity moves into app code); provisioned Aurora (idle cost) | VAL |
| T6 | DB access from Lambdas | **RDS Data API**: IAM authorises `rds-data:*`; DB credentials come from **Secrets Manager** (`secretArn`); RLS scope is set **inside each Data API transaction** (session state is `DISCARD ALL`-reset between calls); writer-only; 1 MiB response cap. MVP uses local PostgreSQL behind the same repository interface | $0.397 per million requests in Mumbai. No RDS Proxy (8-ACU minimum ≈ $134/month) and no VPC-attached Lambdas or interface endpoints for DB access | RDS Proxy + VPC Lambdas | Mumbai price list |
| T7 | Integrity mechanism | `dock_occupancy` + exclusion constraint + compatibility trigger + per-dock advisory lock | Correct by construction under any concurrency | App-level locks only; slot-row uniqueness only (the seed's gap) | VAL |
| T8 | Scheduling solver | **OR-Tools CP-SAT** (Lambda container) with a dispatch-rule fallback | Optimal in seconds at this size; Python | OptaPlanner / Timefold (JVM; Python discontinued) | SPK |
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
| T24 | Data residency | **Status: designed, Build verification pending (spike B1–B4).** India-only inference; no raw phone numbers, registrations or coordinates in prompts; sanitised eval data | DPDP and customer trust; masking is not a substitute for approval | Global inference with masking | `06_review_response.md` |
| T26 | Workflow state ownership | PostgreSQL owns business state; Step Functions owns long waits (ids and task tokens only); PostgreSQL wins on any disagreement | One source of truth | State duplicated in workflow payloads | `07_acceptance_and_verification.md` |
| T27 | Human approval authorisation | Authenticated (JWT + role + facility), expiring, idempotent (`decision_idem_key`), re-validated with `is_feasible`; task token kept server-side; audited | Approvals are the highest-risk human action | Approve links with embedded tokens | `07_…` |
| T28 | Model and prompt versioning | Every inference span records model, inference profile ARN, prompt / schema / policy / cfg versions, tokens, latency. Changes require a golden-set run | Reproducibility and residency evidence | Unversioned prompts | `07_…` |
| T29 | Failure recovery | Per dependency: timeout, jittered retries, circuit breaker, idempotent compensation, DLQ + redrive runbook | Predictable degradation | Ad-hoc retries | `07_…` |
| T31 | Database privileges | Least privilege per role (console / agent / system). Capacity changes only through `SECURITY DEFINER` functions owned by a NOBYPASSRLS, NOLOGIN, member-less role with a pinned `search_path`; column-level grants for other writes; no DELETE; nothing for PUBLIC. Every new function gets its own EXECUTE grant | RLS limits rows, not operations; the architecture promises controlled tool execution | One role with SELECT / INSERT / UPDATE on everything | `schema.sql`, validator §7 |
| T32 | Agent authority (Build invariant, owner's instruction 17 Nov 2026) | The agent understands and **requests**; the application and database **decide**. The agent role's tables, columns and functions are pinned to an allowlist by a CI test; it cannot insert driver messages, so it cannot forge a driver's yes | Keeps the LLM out of every decision with business consequences | Agent with broad DB write access guarded by prompts | `tests/test_agent_not_operator.py` |
| T33 | Database engine version | **Aurora PostgreSQL 18** (minimum 18.3, the first 18.x the RDS Data API supports; Data API on 18 is available in ap-south-1 and ap-south-2). Pin the latest minor and patch on AWS's schedule | Same major version as every local test (PostgreSQL 18); no downgrade; longest support window | Aurora PostgreSQL 16 (an older major, and the local evidence would be a different version) | AWS docs: Aurora PG 18 GA (June 2026); Data API region/version table |
| T34 | Confirmation and hand-over trust (step 2 review) | The driver's yes is an **attestation** written by the trusted inbound path for a DRIVER message after a database-written read-back; hold and request verify it. ETA reports cite a DRIVER message in the caller's thread. TAKE_OVER escalations and console takeovers set the human flag, thread status and flow state atomically. Unassigned escalations go to a CENTRAL queue, never a default facility | The agent requests; it never attests, backdates or chooses identity | Trusting the agent's claim that the driver said yes | `migrations/0004_step2_review_fixes.sql` |
| T35 | Database roles per responsibility (step 2 final review) | `setuhaul_agent` (requests), `setuhaul_ingest` (verified inbound + attestations), `setuhaul_feasibility` (reads + publishes options), `setuhaul_console` (people), `setuhaul_system` (background jobs), `setuhaul_rpc` (owns definer functions). Each has a catalogue-tested allowlist. A driver's YES is valid only until their next message | A read-only transaction is not a security boundary; a later NO must always win | One broad system role for inbound, feasibility and jobs | `migrations/0005_attestation_freshness_and_ingest_role.sql` |
| T36 | Per-conversation serialization (review R0.7) | One transaction lock per conversation, taken by inbound storage, read-back, attestation, attestation verification (hold, request), takeover and escalation; lock order conversation → dock | Freshness must hold under concurrency, not only sequentially | Row locks on `chat_threads` (would give the ingest role UPDATE rights on threads); SERIALIZABLE everywhere (retries on every conflict) | `migrations/0006_serialize_per_thread.sql`, `tests/test_concurrency.py` |
| T37 | Production booking switch (review R0.8) | `SETUHAUL_BOOKING_ENABLED`, default off. The agent can understand, offer and read back, but cannot hold or request a slot until durable inbound delivery is in place and verified | Model integration can start without risking live bookings | Starting model work only after all step 4 work | `setuhaul/api/service.py`, `test_booking_switch_is_off_by_default` |
| T38 | Model provider switch (18 Nov 2026, AWS blocker B-AWS-0; hardened after the step 3 review) | Deployed code has one entry point, `production_model()`: India Bedrock only, explicit `SETUHAUL_ENV` required, any other provider refused. The AWS/OpenRouter switch is in `evals/providers.py`, outside the deployed package. OpenRouter (same Haiku 4.5 / Sonnet 5, prompts, schema and forced tool call) needs `SETUHAUL_ENV` in {dev, eval, ci}, `SETUHAUL_DATA_CLASS=synthetic` and a `SyntheticEvalOnly` capability, all re-checked against the real process environment; requests ask for no data collection (a preference, not a guarantee) | The AWS account cannot call Claude yet; accuracy and tokens transfer, latency and price do not. Residency must not depend on one variable being set correctly (review) | Running dev on `global.` Bedrock profiles; relaxing the India guard; a denylist of production names; Gemini (a different model) | `evals/providers.py`, `setuhaul/agent/models.py`, `tests/test_agent_units.py` |
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
| U12 | Data-residency sign-off | **Build verification pending.** Designed for India-only processing (T4, T24); runtime proof (B1–B4: profiles, IAM deny on `global.`, 100 % `in.` ARNs in traces, no other AI service receiving driver data) and formal sign-off are outstanding. Choosing an India profile is not proof that every model-backed operation stays in India |
| HITL | Escalation SLAs and safety wording | 10 / 5 min; placeholder text |
