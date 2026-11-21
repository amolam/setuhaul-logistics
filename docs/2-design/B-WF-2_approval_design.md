# B-WF-2: warehouse approval workflow, design (revision 2)

| Revision | Date | Status |
|---|---|---|
| 1 | 19 Nov 2026 | Proposal. Owner approved the architecture and answered Q1–Q4 |
| 2 | 19 Nov 2026 | Adds the owner's decisions and clarifications A–E. Approved |
| 3 | 19 Nov 2026 | Database clock and trusted identity (§14–15); lock order, one event per outcome, audit principals, policy evidence (§16–19); corrected deadline formula (§6). Approved for implementation. Booking switch stays off |

## 1. The boundary

The agent can only *ask* for a booking. A request that needs approval becomes CONFIRMED only when an authenticated
warehouse person decides. That decision runs inside a database function, which itself enforces every booking and
capacity rule, before a single deadline shared by the request and its claim on the slot.

Every other outcome is final and recorded: REJECTED, EXPIRED, INVALIDATED, WITHDRAWN or SUPERSEDED. Step Functions
waits and wakes things up; it never decides, and the driver's notification does not depend on it.

Deterministic policy confirmation (B-WF-2a) is a separate path. It is never recorded as a human approval, and every
rule ships as NEEDS_PLANNER.

## 2. States

```
                     submit_booking_request (agent; attested YES, §4)
                                   │
                                   ▼
        ┌──────────────────── OPEN / PENDING_CONFIRMATION ─────────────────────┐
        │            │                 │                 │                     │
     approve       reject       deadline passes   claim or slot lost     withdraw / replaced
     (human)      (human)         (system)         (system, §6)            (staff, §7)
        ▼            ▼                 ▼                 ▼                     ▼
    APPROVED     REJECTED          EXPIRED          INVALIDATED       WITHDRAWN | SUPERSEDED
   CONFIRMED     REJECTED          EXPIRED           CANCELLED             CANCELLED
  (old booking   claim released    claim released,   claim released,       claim released
   CANCELLED)                      old booking kept  person alerted

   Separate, inactive in v1:   policy rule ──▶ appointment CONFIRMED, basis = POLICY (no approval row, never "APPROVED")
```

| Approval status | Appointment status | Meaning |
|---|---|---|
| OPEN | PENDING_CONFIRMATION | Waiting for a person; holds an active claim on the slot |
| APPROVED | CONFIRMED | A person approved; the previous booking, if any, is CANCELLED |
| REJECTED | REJECTED | A person refused |
| EXPIRED | EXPIRED | No decision by the deadline. The previous booking stays valid and a person is alerted |
| INVALIDATED | CANCELLED | The claim or the slot can no longer be honoured (§6); reason recorded |
| WITHDRAWN | CANCELLED | Staff withdrew it; reason code recorded (§7) |
| SUPERSEDED | CANCELLED | A newer booking for the same shipment replaced it (§7) |

Rules the database enforces:
- One open request per shipment.
- The approval and appointment change in one transaction.
- "OPEN ⇔ PENDING_CONFIRMATION".
- A final status is permanent (transition trigger).
- No final status without a decider type and a reason.

The schema changes are new values for the approval status CHECK, and columns `appointment_id`, `outcome_reason`,
`decided_by_type` (HUMAN | SYSTEM | POLICY), `decided_by` and `claim_occupancy_id`.

## 3. Transitions and who can make them

| Transition | Only through | Who | Database guards (database clock) |
|---|---|---|---|
| create | `submit_booking_request` | Agent role | Attestation valid under the conversation lock (§4); live hold; all invariants in §5; a confirmer is available before the deadline, otherwise escalate to a person |
| approve | `decide_booking_request` | Human, with the role for this kind (§8), facility in session scope | OPEN; `now() < deadline`; no operational hold; all invariants in §5 re-checked |
| reject | `decide_booking_request` | Same roles | OPEN. Allowed during any hold or pause |
| expire | `expire_pending_requests` | System: every minute, plus an exact-time timer | OPEN and `deadline ≤ now()` |
| invalidate | the function that removed the capacity (§6) | Whoever made that change (staff or system) | Same transaction as the change |
| withdraw | `withdraw_booking_request` (new) | `operations`, `planner` or `ops_lead` at that facility | OPEN; reason code and note required |
| supersede | the function that creates the replacing booking | Staff | OPEN; same transaction |

The decider is never a function parameter. It is read from session settings that the console API sets from the
verified Cognito token (`app.principal`, `app.staff_roles`, `app.facility_scope`).

## 4. The driver's YES (clarification A)

Today an attestation is bound to the conversation, the option shown (and so the shipment, dock and slot), the
driver's selection, and the exact inbound message. Any later driver message makes it unusable
(`CONFIRMATION_SUPERSEDED`), and it expires after 10 minutes.

Revision 2 adds explicit snapshot columns, compared again at submission:

| Bound to | Column (new) | Checked at `submit_booking_request` |
|---|---|---|
| Inbound message | `confirmation_message_id` (exists) | Still the newest driver message in the conversation |
| Driver | `driver_id` | Equals the conversation's driver and the session's `app.driver_id` |
| Shipment | `shipment_id` | Equals the shipment the conversation is bound to now |
| Conversation | `thread_id` (exists) | Same conversation, under its lock |
| Proposed slot | `dock_id`, `slot` | Equal to the option, the hold and the new appointment |
| Confirmation version | `selection_id` (exists) + `schedule_version` | Latest selection; schedule version unchanged, or the slot re-proven by §5 |

The attestation is used once: submission records `consumed_by_appointment`, and a second use is refused.

Once a request is OPEN, a later driver message does not cancel it silently. Approval needs the driver's confirmation
to have been valid when the request was made, which it was. If the driver then says "cancel", the agent escalates; a
coordinator withdraws the request with reason `DRIVER_REQUESTED` (Q4).

## 5. The database enforces the booking rules (clarification B)

The application's feasibility check runs first, but it is **advisory**: it gives the driver or planner a clear message
early. The database functions check every critical invariant again, atomically, under row locks.

| Invariant | Where it is enforced |
|---|---|
| One truck per dock at a time | `ex_one_truck_per_dock` exclusion constraint (exists) |
| One vehicle not booked on two docks at overlapping times | Vehicle exclusion constraint (exists) |
| One current booking per shipment | `ux_current_active_appointment_per_shipment` (exists) |
| The claim exists, is active, and matches the appointment's dock and slot exactly | Checked in the function, plus a constraint trigger at commit (§6) |
| Dock active, of a compatible type, and not blocked over the slot | Checked in the function (blocks are rows in the same exclusion constraint) |
| Slot inside the facility's operating window, unless the request's kind allows otherwise | Checked in the function |
| Slot start ≥ now + confirmation lead time | The deadline (§6) makes this true for every OPEN request; re-checked on approval |
| Shipment not cancelled and not already in the dock | Checked in the function |
| No operational or safety hold on the facility or dock | Checked in the function (Q3) |
| Decider role and facility | Checked in the function from session settings (§8) |

## 6. One deadline for the claim and the approval (clarifications D and E)

On submission, the driver's hold row turns into the request's claim, so the slot is never free in between (existing
behaviour). Revision 2 gives the claim and the request a single deadline:

```
deadline = least( clock_timestamp() + warehouse TTL for the shift,
                  slot start − confirmation lead time )           ← computed once, at submission
```

The hold must be unexpired at submission (checked with `clock_timestamp()` after the hold row is locked). From then
on the claim has **no expiry of its own**: it is the request's claim and lives exactly as long as the request.
(Revision 2 also capped the deadline at the hold's own expiry. That was wrong: holds last 3 minutes, so every request
would have expired almost at once.)

- **The claim cannot outlive or underlive the request.** Only the functions that close the request release the claim,
  in the same transaction.
- **No request stays approvable after its claim is gone.** A deferred constraint trigger checks at every commit that
  no OPEN request has an inactive claim, or a claim whose dock or slot differs from its own. Any transaction breaking
  this fails as a whole.
- **"Too late" cannot stay OPEN.** Because the deadline is at or before slot start minus the lead time, a request is
  never OPEN once its slot is too late. The sweep runs every minute, and the timer fires at the exact deadline. Between
  them, `decide_booking_request` refuses after the deadline anyway.
- **Capacity lost for other reasons** invalidates the request **in the same transaction**, with the reason recorded:

| Event | Function | Reason recorded |
|---|---|---|
| Dock blocked (breakdown, maintenance, manual) over the slot | `block_dock` | `CAPACITY_LOST` |
| Dock deactivated or its type changed | Dock admin function | `DOCK_UNAVAILABLE` |
| Shipment cancelled or reassigned | Shipment status function | `SHIPMENT_CHANGED` |
| Facility hours changed so the slot is outside them | Configuration function | `OUTSIDE_HOURS` |

  The driver and a person are told through the outbox event (§9). An operational hold does **not** invalidate: it
  blocks approval only, and the request expires normally if the hold lasts past the deadline.

## 7. Withdrawn vs superseded (Q4)

| Status | When | Recorded |
|---|---|---|
| WITHDRAWN | Staff call `withdraw_booking_request` | `reason_code`: DRIVER_REQUESTED, OPERATIONS_DECISION or DATA_CORRECTION, plus a free-text note and the staff principal |
| SUPERSEDED | Staff create a new booking for the same shipment (console reschedule or manual override) | `superseded_by` = the new appointment id |

The driver cannot withdraw by chat in v1. The agent escalates, and a coordinator withdraws.

## 8. Roles (Q2): all facility-scoped, read from the session

| Role (Cognito group) | Who it is | May |
|---|---|---|
| `operations` | The shift coordinator at one facility's dock office | Decide plain requests (no special kind) during the facility's operating hours; withdraw; take over a conversation |
| `planner` | The facility planner | Everything `operations` may, plus AFTER_HOURS_START and DOCK_DOWNGRADE |
| `ops_lead` | The facility operations lead | Everything above, plus MOVE_CONFIRMED_PROMISE and PRIORITY_OVERRIDE |

- **Scope comes from the token.** Every permission applies only to facilities in the session's `facility_scope`. A
  `CENTRAL` scope gives visibility, not the right to approve.
- **Where the mapping lives.** The role-per-kind table (`approval_role_rules`) is in the database. Changing it is an
  audited migration.
- **What the database trusts.** It cannot verify a JWT itself. It trusts only session settings the console API sets
  after verifying the token, never function arguments. Step 6 verifies the real Cognito groups.

## 9. Durable events and Step Functions (clarification C)

- **Every final transition writes events in the same transaction.** Each one writes an **`ApprovalDecided`** outbox
  event: `{approval_id, appointment_id, outcome, reason, decided_by_type, decided_at}`. It also writes the existing
  booking event (`BookingConfirmed`, `BookingRejected`, `BookingExpired` and so on) and an audit row.
- **Driver notification is driven by the outbox** (B-WF-3), never only by Step Functions. If the workflow, the callback
  or the timer fails, the driver is still told.
- **Step Functions' remaining job:**

```
BookingRequested ─▶ StartExecution(name = approval_id)        ← idempotent
   NotifyApprovers ─▶ WaitForDecision (waitForTaskToken, timeout = deadline)
      ├─ callback (approval id only) ─▶ end
      └─ timeout ─▶ expire_approval(approval_id) ─▶ end        ← changes nothing if already closed
```

  It reminds approvers and fires the exact-time expiry. A one-minute reconciler closes executions whose request is no
  longer OPEN and re-sends lost callbacks. If an execution is lost, the sweep still expires the request. PostgreSQL
  wins every disagreement.

## 10. Policy confirmation (Q1, B-WF-2a)

- **Rule table ships closed.** `confirmation_rules` (facility, condition, outcome, version) ships with every row set to
  `NEEDS_PLANNER`. The existing per-shift `auto_confirm_chat_bookings` switch is retired and no longer read.
- **What a policy confirmation looks like** once you authorise a rule: the appointment becomes CONFIRMED with
  `confirmation_basis = POLICY` and the `rule_id` and `rule_version` recorded. No approval row is created; the audit
  principal is `policy:<rule_id>` and the outbox event is `BookingConfirmed` with `basis: POLICY`. It is never
  recorded as APPROVED, never attributed to a person, and never decided by the AI.
- **The agent never chooses the outcome.** The rule is evaluated inside `submit_booking_request`.
- **Switching a rule on** is an owner decision recorded in the decision register. It is not part of B-WF-2.

## 11. Holds and pauses (Q3)

| Switch | Stops | Does not stop |
|---|---|---|
| `automation_paused` (exists) | The agent's booking requests and policy confirmations | Human approve, reject and withdraw; expiry |
| Operational or safety hold (new `booking_holds`: facility or dock, reason, set and cleared by `ops_lead`, audited) | **Approvals** that would execute a booking, and new requests | Reject, withdraw, expiry, invalidation |

## 12. Tests to write (all local)

- **Every transition** in §3 has a test, run as the allowed role, a wrong role and a wrong facility.
- **Attestation binding (A).** A mismatch on each bound field is refused. So are a later driver message and a second
  use. A driver message after submission leaves the request OPEN.
- **Database rules (B).** Each invariant in §5 is broken directly, bypassing the service's feasibility check, and the
  function refuses.
- **Events (C).** Each final transition writes exactly one `ApprovalDecided` event. The driver is notified with the
  workflow stopped.
- **Invalidation (D).** Each event in §6 invalidates in the same transaction. The commit-time trigger refuses an
  OPEN request with a dead claim.
- **One deadline (E).** The deadline is at or before the slot start minus the lead time and the hold expiry. After
  the deadline, approval is refused even before the sweep runs.
- **Races.** Approve vs approve, approve vs reject, approve vs expiry at the deadline, approve vs block, approve vs
  withdraw: exactly one outcome each time.
- **Q1.** Every rule is NEEDS_PLANNER, so nothing confirms automatically. A forced policy row confirms with basis
  POLICY and no approval row.
- **Q3.** Approval is refused under an operational hold, but allowed under an automation pause.
- **Agent permissions.** The agent cannot approve, extend a deadline, read a token, write approval rows or set a hold
  (allowlist tests).

## 13. Local vs real AWS

| Built and tested locally (B-WF-2) | Real AWS acceptance (step 6, BOOKING_WORKFLOW gate) |
|---|---|
| Migrations, functions, triggers, roles, RLS, database-clock deadlines | Same migrations on Aurora |
| `LocalApprovalWorkflow` port, reconciler, ASL file and a structural test | Step Functions Standard, real `waitForTaskToken`, timeouts and callbacks; EventBridge Scheduler |
| Console API with Cognito claims stubbed | Real Cognito groups, API Gateway authoriser, session settings from real tokens |
| Scripted-model end-to-end flows | Published state-machine version in the deployment manifest, plus drift detection |

Also in this milestone: `FINDINGS_LEDGER.md` (every finding R0.x–R2.1, with open and step 6 items carried forward)
and `TRACEABILITY.md` (requirement → test).

## 14. The database clock (revision 3)

- **Deadlines are decided with `clock_timestamp()`, never `now()`.** Every approve, expire, withdraw and invalidate
  compares against the deadline only **after** taking its row locks. `now()` is the transaction's start time, so a
  transaction that started before the deadline and waited on a lock would otherwise still approve.
- **Test:** a transaction starts before the deadline, waits until after it, then tries to approve. It must get
  `PENDING_EXPIRED`.
- **`now()` stays only for timestamps** that mark "when this transaction happened" (`created_at`, audit rows).

## 15. Trusted identity boundary (revision 3)

- **The console has its own database role.** `setuhaul_console` is separate from `agent`, `feasibility`, `ingest` and
  `system`. In AWS the console workload's login maps to it alone (WORKLOAD_IDENTITY gate).
- **The identity is set by one function.** `begin_staff_context(principal, roles[], facilities[])` is SECURITY
  DEFINER, and **only `setuhaul_console` may execute it**. It validates its inputs (known roles only, at least one
  facility) and writes one row to `staff_context`, keyed by `(txid_current(), pg_backend_pid())`. No application role
  can write that table.
- **Approval functions read only that row,** and only for the current transaction. They never read
  `current_setting('app.staff_roles')`: any role can set a custom setting, so settings are not a trust boundary.
  Without a row for this transaction they raise `UNTRUSTED_CONTEXT`. They also check the facility against the row's
  facilities, not against `app.facility_scope`.
- **Only the console can call them.** `decide_booking_request`, `withdraw_booking_request`, `set_booking_hold` and
  `clear_booking_hold` are executable only by `setuhaul_console`.
- **Safe for pooled connections.** The row is keyed by transaction id, so it can never apply to a later transaction
  on the same pooled connection; a rolled-back transaction leaves no row. `app.*` settings are set with `is_local =
  true` (existing). The system sweep deletes rows older than one day.
- **What remains trusted outside the database:** the console API verifies the Cognito token before calling
  `begin_staff_context`, and only the console's login can become `setuhaul_console`. The database cannot verify a JWT.
  Step 6 proves the login mapping.
- **Spoofing tests:**
  - agent, feasibility, ingest and system each set `app.staff_roles = 'ops_lead'` and try to approve: refused;
  - each tries `begin_staff_context`, or a direct insert into `staff_context`: refused;
  - the console without a context: refused;
  - a context from an earlier transaction on the same connection: refused;
  - a context for another facility: refused;
  - an `operations` context deciding an AFTER_HOURS_START request: refused.

## 16. Lock order (revision 3)

Every function that touches a request takes locks in this order, and never the other way round:

1. the conversation lock (`thread_lock`), only where the function works on a conversation (submit);
2. `approval_requests` rows, `FOR UPDATE`, in `approval_id` order (the sweep and invalidations use `SKIP LOCKED` or
   ordered batches);
3. `appointments` rows, in `appointment_id` order (the requested booking and the current one);
4. `dock_occupancy` rows, in `occupancy_id` order.

`block_dock` and the shipment, dock and hours triggers first lock the affected OPEN requests (step 2), then insert the
block or change the row. Race tests assert exactly one outcome and no deadlock (`40P01`).

## 17. One event per outcome; idempotent consumers (revision 3)

- **One `ApprovalDecided` per approval.** A unique index on `outbox (aggregate_id) WHERE event_type =
  'ApprovalDecided'` makes a second event for the same approval impossible.
- **Consumers record what they have processed.** Each consumer records `(consumer, event_id)` in `outbox_consumptions`
  (primary key) **in the same transaction as its effect**. A redelivered event finds the row and does nothing.
  `consume_outbox_event(consumer, event_id)` returns false when the event was already consumed. B-WF-3 notifications
  and the local workflow use it.

## 18. Audit principal for every outcome (revision 3)

| Outcome | `booking_events.principal` | `principal_role` | `approval_requests.decided_by_type` |
|---|---|---|---|
| APPROVED, REJECTED | `staff:<sub>` from `staff_context` | the role that authorised it (`operations`, `planner` or `ops_lead`) | HUMAN |
| WITHDRAWN | `staff:<sub>` from `staff_context` | the staff role | HUMAN |
| SUPERSEDED | `staff:<sub>` of whoever created the replacing booking | the staff role | HUMAN |
| INVALIDATED by a staff action (dock block) | `staff:<sub>` from `staff_context` | the staff role | HUMAN |
| INVALIDATED by a data change (shipment, dock, hours) | `system:<trigger name>` | system | SYSTEM |
| EXPIRED | `system:expiry` | system | SYSTEM |
| Policy confirmation | `policy:<rule_id>@v<version>` | policy | — (no approval row) |

## 19. Policy evidence (revision 3)

A policy confirmation stores, on the appointment: `confirmation_basis = POLICY`, `policy_rule_id`,
`policy_rule_version` and `policy_evidence` (jsonb). The evidence holds:

- the inputs the rule saw: facility, shift, dock, slot, request kind, whether another booking moves, and the schedule
  version;
- the attestation id that carried the driver's YES;
- the rule's condition as evaluated;
- `clock_timestamp()` at the moment of evaluation.

Rules are versioned rows that are never edited; a change is a new version. The same evidence goes into the audit row
and the `BookingConfirmed` event.

## 20. B-WF-2a: the confirmation rule rows (migration 0016, built 19 Nov 2026)

The rule table from §10 now has its real rows: one set per facility, version 1, append-only. The database computes
every input inside `submit_booking_request` from its own rows, never from the agent or the model.

| Priority | Rule | Matches when | Outcome | Request kind (who decides) |
|---|---|---|---|---|
| 10 | person-safety | A SAFETY escalation is open for the shipment | PERSON_ALWAYS | — (no request) |
| 11 | person-contradictory-data | A CONTRADICTORY_DATA escalation is open | PERSON_ALWAYS | — (no request) |
| 100 | planner-after-last-new-start | The slot starts after the facility's last new start | NEEDS_PLANNER | AFTER_HOURS_START (planner, ops_lead) |
| 101 | planner-after-hours | Outside operating hours | NEEDS_PLANNER | AFTER_HOURS_START |
| 102 | planner-dock-downgrade | Moves a booking from a REEFER or HEAVY dock to a STANDARD one | NEEDS_PLANNER | DOCK_DOWNGRADE (planner, ops_lead) |
| 103 | planner-move-earlier | Moves a confirmed booking earlier | NEEDS_PLANNER | PLANNER_REVIEW (planner, ops_lead) |
| 104 | planner-busier-time | Moves a booking to a time with more active occupancy at the facility than its current time | NEEDS_PLANNER | PLANNER_REVIEW |
| 105 | planner-critical-load | Shipment priority CRITICAL | NEEDS_PLANNER | PLANNER_REVIEW |
| 106 | planner-reefer-load | Temperature-controlled shipment | NEEDS_PLANNER | PLANNER_REVIEW |
| 500 | safe-new-booking (auto-eligible) | Normal hours, no other booking moves | NEEDS_PLANNER | STANDARD (operations, planner, ops_lead) |
| 501 | safe-move-later (auto-eligible) | Moves a booking later, not into a busier time | NEEDS_PLANNER | STANDARD |
| 1000 | default | Anything else | NEEDS_PLANNER | STANDARD |

"Same facility, compatible dock, free slot" from the backlog need no rule: the option is at the destination facility,
the occupancy trigger refuses incompatible docks, and the hold proves the slot is free.

**Guards that hold whatever the rule order:**
- **Person always.** A safety or contradictory-data flag always refuses with `REQUIRES_PERSON`, and nothing is created.
  The agent then hands over to a person.
- **Kind floors.** An after-hours slot is never STANDARD. Moving earlier, a busier time, a critical load or a reefer
  load is never below PLANNER_REVIEW. A downgrade is DOCK_DOWNGRADE.
- **Policy confirmation** needs a rule version that is auto-eligible and names its authorisation (`authorised_by`, a
  decision-register reference). Database constraints enforce both. Even then, `submit_booking_request` confirms only
  when no risk flag is set.

**Q1 is kept.** Every row ships NEEDS_PLANNER or PERSON_ALWAYS. The 17 Nov backlog line "start GGN night shift on
automatic for the safe cases" is **not applied**. Switching a safe row on means adding a new version with
`outcome = 'POLICY_CONFIRM'` and `authorised_by = '<decision id>'`. That needs your explicit decision, recorded in the
decision register.

**The override-rate metric.** The view `policy_rule_outcomes` counts how people decided each rule's requests:
approved, rejected, expired, closed otherwise, and still open. Its override rate is rejected out of approved plus
rejected. It is available to planners and ops leads at `GET /console/policy/outcomes`. For an auto-eligible rule,
that rate is the evidence for authorising it.

## Review R3 corrections (migration 0019)

- **§14, every outcome.** `close_approval` takes the appointment and claim locks, then reads `clock_timestamp()`.
  Past the deadline:
  - a person's reject or withdraw is refused (`PENDING_EXPIRED`), as confirm already was;
  - an invalidation or supersession is recorded as EXPIRED. The requested outcome is kept in the note, the
    ApprovalDecided payload (`requested_outcome`) and the PENDING_EXPIRED escalation.
- **§5, compatibility.** Changes to destination, weight, dock type or refrigeration invalidate open requests.
  `decide_booking_request` re-proves the hard dock rules itself (`booking_incompatibility`). The occupancy guard also
  runs when a hold becomes a request's claim.
- **§15, every console mutation.** Pause, takeover, handback and dock block read `staff_context`, check the role in
  the database and record the principal from it. The console role writes no audit, outbox or human-flag rows directly.

## Review R3.1 corrections (migration 0020)

- **§16, lock order, extended:** thread_lock → shipment row → dock row → approval_requests → appointments →
  dock_occupancy.
  - A decision SHARE-locks the shipment and dock before the approval and re-reads them.
  - A shipment or dock change takes its own row first, then the approvals in its trigger.
  - A change that waited for a decision applies to a CONFIRMED booking. If that booking is no longer compatible, it
    raises BOOKING_AT_RISK for a person.
- **§8:** planner may take over and hand back conversations. The audit records the actual role.

## Review R3.2 corrections (migration 0021)

- **§16, lock order, extended:** thread_lock → shipment row → dock row → **booking-compat advisory key per dock
  (dock_id order)** → approval_requests → appointments → dock_occupancy.
  - A dock change takes its own dock's key. A shipment change takes the key of each dock holding a current CONFIRMED
    booking for it.
  - The key is transaction-scoped. The second of two changes to one booking waits, then checks compatibility against
    the first one's committed values and raises BOOKING_AT_RISK if needed.
  - The AFTER triggers lock no row of the other table. Decisions take no key; they serialise through their SHARE locks.
  - Compatibility changes must run at READ COMMITTED (or SERIALIZABLE). REPEATABLE READ is refused, because it keeps
    the old snapshot.
