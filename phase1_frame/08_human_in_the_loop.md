# Phase 1 · Frame — Human in the Loop

**Why this file exists.** Earlier Phase 1 files named *when* a person must step in: decision 12's triggers, the escalation hand-off record, planner approval for moving confirmed slots, and the rule that safety always goes to a human. They did not define the **workflow**: who acts, what they see, how fast, what happens when nobody answers, how a person takes over a chat and hands it back, and how human decisions improve the system. That was a gap, because the brief makes humans part of the design (*"A person approves or takes over"*, and *"An escalation path to a human coordinator"* is a deliverable). This file closes it.

---

## 1. Principles

1. **Humans override policy, never integrity.** A coordinator can approve an after-hours start, a dock downgrade or a priority bump. Nobody, human or agent, can create a double booking or put a 31 t load on a 20 t dock. Every human action goes through the same booking service and `is_feasible()` hard constraints.
2. **The agent never decides alone** on the brief's list: promising the same capacity twice, vehicle–dock fit when rules conflict, ranking scarce capacity when rules conflict, and whether a booking committed. Code decides the first three. People decide where policy runs out.
3. **Every handover carries a complete record.** The person never has to re-ask the driver or re-query the system (hand-off record, `02_variables.md` §7).
4. **Silence is never approval.** If a person does not respond in time, the system takes the safe default (no change) and escalates upward. It never approves automatically.
5. **The driver always knows who is handling them and what happens next**, with a time.
6. **Five coordinators per shift is the budget.** Automation exists to keep their queue short and actionable.

## 2. Five modes of human involvement

| Mode | When | Who | Blocking? |
|---|---|---|---|
| **A. Approve** | The system has a specific proposed action, but policy requires a person | Warehouse planner or ops coordinator | Yes, for that action only |
| **B. Decide** | No safe automatic outcome exists | Ops coordinator | Yes, for that case |
| **C. Take over** | Safety, distress, abuse, the driver asks for a person, or the agent keeps failing | Ops coordinator (shift manager for safety) | Yes. The agent goes silent on that thread |
| **D. Confirm** | Warehouse confirmation of a driver-chat booking (`PENDING_CONFIRMATION`) | Warehouse planner of that facility | Yes, for that slot (TTL per facility and shift) |
| **E. Oversee** | Always: dashboards, sampled review, post-hoc override, pause switch | Shift manager, ops lead | No |

## 3. Triggers

| # | Trigger | Mode | Source of the trigger (deterministic) | Seed example |
|---|---|---|---|---|
| H1 | Safety concern: accident, injury, fire, threat, medical | C | Keyword list **or** `safety_concern = true` from extraction | — (none in seed; covered by eval) |
| H2 | No feasible slot | B | `find_feasible_options` returns none | SHP1015 reefer (THR005), SHP1012 latest finish |
| H3 | Contradictory data: driver or vehicle double-assigned, e-way-bill vehicle mismatch, signals disagree persistently | B | `assignment_conflict`, `fuse_eta` flags | SHP1006 / SHP1021 (DRV006), DRV007 |
| H4 | Start after `LAST_NEW_START_TIME` or ending after close | A | `needs_approval(start)` | Any D1–D4 slot at 21:00 (RULE005) |
| H5 | Moving another driver's **on-time confirmed** promise (scheduler proposal or priority request) | A | Scheduler diff marks `changed_j` on an on-time confirmed job | CRITICAL SHP1014 vs confirmed trucks at 11–12 |
| H6 | Dock downgrade (STANDARD load on a HEAVY or REEFER dock) | A | `allow_dock_downgrade = false` and the only options are downgrades | — |
| H7 | Warehouse confirmation pending | D | State machine | APT1013A, APT1014A |
| H8 | Pending confirmation expired, or the warehouse rejected it | B | Timer / rejection | OM002 still QUEUED |
| H9 | Clarification limit reached (e.g. 3 questions, still no usable ETA) | B | `clarification_turns > max` | THR003 if the driver never clarifies |
| H10 | The driver asks for a person, or is abusive or distressed | C | Extraction intent plus a keyword list | — |
| H11 | Penalty, compensation, customer commitment or re-routing to another facility | B | Intent or tool (out of agent scope) | SHP1015 re-route to GGN |
| H12 | Regulated load or emergency (hazmat, medical urgency beyond priority) | C | Product category and keyword rules | — |
| H13 | Agent or model failure persists after the degraded mode | C | Error-rate breaker per thread | — |
| H14 | Notification to a human failed (escalation not delivered) | E → page | Outbox FAILED | OM004 |

## 4. What happens in each mode

### A. Approve

1. The booking service creates an **approval request**: the proposed action, the exact rule that requires approval, the evidence, the alternatives, and an `expires_at`.
2. The driver hears: *"D2 at 21:15 is possible but needs approval because unloading after 21:00 needs a manager's OK. I'll update you by 19:40."*
3. The planner sees one card with **Approve / Reject / Choose alternative** buttons. Approve calls the same `confirm_booking` path, which re-runs `is_feasible()`. If the world changed, the approval fails safely and the planner sees why.
4. **Timeout:** at `expires_at` the request lapses, nothing changes, the driver is offered the next non-approval option or the case becomes mode B, and the shift manager is notified.

### B. Decide (escalation)

1. An escalation is created with the **hand-off record**: shipment, driver, ETA and confidence with signal evidence, constraints, every option considered with its rejection reason, suggested actions, the driver's last message, what the driver was told, and the trace id.
2. The driver hears what happens and when: *"No reefer dock is free tonight because of planned maintenance. An operations coordinator is reviewing options and will message you by 16:45. Please continue to Jaipur safely."*
3. The coordinator picks a **structured action**: book a specific slot (validated), approve an exception, re-route, ask the driver something, call the driver, cancel or hold the load, or close with a note. Free text goes to the driver only through the console's send box. It is logged and never parsed into capacity changes.
4. **SLA:** acknowledge within 10 min (5 min for CRITICAL loads or a slot within 2 h). Resolve before the latest useful time, computed as `slot_needed_by − buffer`.
5. **Timeout:** an unacknowledged escalation is re-routed to the shift manager and then the ops lead (paging). The driver gets a holding update. The system never acts on the escalation by itself.

### C. Take over

1. The thread is flagged `HUMAN_ACTIVE`. **The agent stops sending messages on that thread**, and inbound driver messages go to the console.
2. Safety (H1): the coordinator is paged at once and **calls the driver by phone**. The chat sends one fixed message: *"If anyone is hurt or in danger, call 112 now. An operations coordinator is calling you."* (The emergency number is to be confirmed by SetuHaul's safety policy.)
3. **Handback:** the coordinator presses *Return to assistant* with a short structured summary (what was agreed, new constraints). This is written to the conversation memory, so the agent continues without re-asking.
4. Takeover can also start manually: any coordinator can claim any thread.

### D. Confirm (warehouse)

Already in the state machine: the planner confirms or rejects pending requests in the console. The TTL is `warehouse_confirm_ttl_min[facility][shift]`. Gurugram night is 0, so requests go straight to mode B because there is no coordinator on that shift.

### E. Oversee

- **Live board** per facility: queue, at-risk appointments, open escalations with SLA timers, coordinator load.
- **Sampled review:** a shift manager reviews ~10 % of auto-resolved conversations a day, plus 100 % of overrides and complaints.
- **Post-hoc override:** any automated booking can be changed through the console (validated, audited, driver notified).
- **Pause switch** per facility or globally: automation is paused, the agent replies with the degraded template ("A coordinator will handle your request"), and every new case becomes mode B. Used during incidents or system doubt.

## 5. Routing and workload

| Rule | Detail |
|---|---|
| Routing | By facility and role: approvals → that facility's planner, decisions → the ops coordinator pool, safety → shift manager plus the on-duty coordinator |
| Coverage gaps | Gurugram night coordinator is vacant (A22), so Gurugram night routes to the central ops queue. A failed delivery to an unknown address (A21) triggers paging, never a silent drop |
| Ordering in the queue | By SLA due time, then priority weight, then wait time |
| Load estimate | 15–25 exceptions a day; with ≥ 70 % auto-resolved, about 5–8 human cases a day. Spike: 20–35 messages in 30 min means about 6–11 human cases spread over 5 coordinators, ≈ 1–2 each. Feasible if handle time ≤ 10 min, which the complete hand-off record is meant to make possible |
| Overload guard | If open human cases per coordinator exceed 4, the shift manager is alerted. Non-urgent approvals (H4, H6) are batched |

## 6. Implementation sketch (AWS)

| Piece | Service |
|---|---|
| Approval and escalation records, with SLA timers | Aurora tables (`approval_requests`, `escalations`) plus EventBridge Scheduler for timeouts. Long waits run in Step Functions with `waitForTaskToken`, so the console's button resumes the workflow |
| Ops console | Web app (Amplify or S3 + CloudFront). Cognito sign-in with MFA. API Gateway with IAM. Facility-scoped RLS |
| Live updates to the console | AppSync subscriptions or API Gateway WebSocket |
| Paging | SNS → SMS / e-mail. Optional Amazon Connect outbound call for safety cases |
| Takeover | A thread flag in the conversation state. The ingest Lambda checks it **before** invoking AgentCore, and routes the message to the console when set |
| Audit | Every human action goes to `booking_events` (append-only) with user, role, reason and before/after state. CloudTrail covers API calls |

## 7. Learning loop

- Every human decision is labelled data: trigger, action, outcome. It feeds the evaluation set (`07_evaluation_plan.md`) and monthly policy review.
- **Promote to automation only through change control.** If a planner approves, say, > 95 % of a trigger type with no bad outcomes over a month, propose an automatic rule, review it, version it (`policy_version`) and test it in the scenario suite before it goes live.
- **Disagreement tracking:** when a human reverses an automated decision, both decisions are kept and reviewed weekly. Patterns become eval scenarios.

## 8. Measures

| Measure | Target |
|---|---|
| Escalation rate (share of cases reaching a human) | ≤ 30 % of non-escalation-class cases (mirrors the ≥ 70 % automation measure) |
| Time to acknowledge | p90 ≤ 10 min (≤ 5 min for CRITICAL and safety) |
| Time to resolve | p90 before the latest useful time, for 100 % of cases |
| Hand-off completeness | 100 % schema-valid (eval assertion D6) |
| Re-asks by humans | Share of cases where the coordinator had to ask the driver for something the record should have held. Target ≤ 10 % |
| Override rate | Tracked per trigger. A rising rate is a signal to fix policy or prompts |
| Agent silence during takeover | 100 % (new eval assertion D13) |
| Coordinator load | ≤ 4 open cases per coordinator |

## 9. Changes to other Phase 1 files

- `07_evaluation_plan.md`: new assertion **D13**, no agent message on a `HUMAN_ACTIVE` thread. New scenarios: approval timeout, escalation unacknowledged, safety takeover and handback, pause switch.
- `06_performance_reliability_security.md`: the escalation SLO becomes "acknowledged within 10 min" in addition to "delivered to the queue within 60 s".
- `01_problem_definition_sheet.md` decision 12: points here.
