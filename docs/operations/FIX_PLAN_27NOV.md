# 27 Nov 2026 fix plan: what this release changes, and how to release it

The owner's "SetuHaul Pilot – 27 Nov Fix Plan" (nine issues, decisions D1–D3 and Q1–Q5), built as three independently
tested workstreams in one release. Migration **0048** (`migrations/0048_appointment_lifecycle.sql`).
Decision register: **T54** (lifecycle), **T55** (dispatch), **T56** (dates), **T57** (trace content).

## What stays exactly as it is

- A planner (or ops_lead) approves every booking and every time change. `submit_booking_request`,
  `decide_booking_request`, `close_approval`, the confirmation rules and the approval roles are unchanged. No new
  approval setting and no new automatic approval: facilities stay MANUAL (0047, unchanged).
- Booking stays ON only through `activate-booking.yml` with fresh evidence and a **new** owner approval reference.
  The kill switch and the evidence guard see a dispatcher's reschedule request exactly like a driver's request.
- RBAC and facility boundaries: every new action is checked by the API (role) and again by the database (role,
  facility scope, state), and writes an audit row in `booking_events`.
- Telegram stays limited to the two authorized test drivers; WhatsApp and production stay off.

## Workstream A · Operations (issues 1, 2, 3)

| Issue | Change |
|---|---|
| 1 | **Controls** tab (ops_lead only): kill switch with who / when / why and a typed confirmation; booking holds (list, set on a facility or dock, clear); automation pause per facility with who paused it. New reads `GET /console/controls`, `GET /console/holds`. Schedule tab: ops_lead stores a proposal and applies one move. Revoking a provisional authorization stays a command-line action. |
| 2 | Every action button is shown only when `/console/me` permits it, with a note naming who can ("Only dispatcher, operations or ops lead can ..."). Every refusal carries a plain sentence (`message`) next to its code. |
| 3 | Appointment lifecycle: **Arrived at gate** (only from CONFIRMED, until start + grace), **At dock**, **Completed** (rest of dock time released, driver told "Unloading complete"). Operations and ops_lead only (D1). **Automatic no-show** every minute in the recovery worker (D2), per-facility `no_show_grace_min` (Q4). **After a no-show** the dispatcher decides (D3): Rebook or Request cancellation; the database refuses any hold or offer until then. Arrivals list in Ops health and Schedule, with the driver's "I have reached" shown as a prompt only. |

Concurrency: arrival and the job lock the appointment row (the job with SKIP LOCKED) and the job's transition is
conditional; one boundary function (`no_show_due`: due only when now > start + grace) decides both.

## Workstream B · Dispatch (issues 4, 5, 6, 7)

| Issue | Change |
|---|---|
| 4 | `GET /console/dispatch/shipments/{id}/free-drivers?start&end`: active, same carrier, not at a gate or in a dock, no live booking within 2 h of the slot (completed and no-show trips no longer count), no open offer for another shipment. |
| 5 | One send button with an explicit choice, **This driver** or **Another driver** (picked from the free list); the confirmation names who receives the offer. `target` is checked by the API and the database (`create_dispatch_offer_to`): "Another driver" can never be the assigned driver. The assignment stays until the new driver accepts (unchanged). |
| 6 | Confirmed booking before its start: **Check slots / request a new time**. The new slot is claimed at once (no competing booking can take it) and becomes a PLANNER_REVIEW request (always a planner, Q2); the booking stays confirmed until approval, then the move is one transaction. The requester can never approve it. |
| 7 | Schedule "Pending your decision" lists open escalations with their due time; a SAFETY escalation shows a banner on the shipment (Dispatch, Schedule); an escalation past its acknowledgement time is highlighted in the Escalations tab. |

## Workstream C · Agent and observability (issues 8, 9)

| Issue | Change |
|---|---|
| 9 | Requested dates: extraction schema **v2** (`requested_date_local`, intent `RESCHEDULE`), prompt **p6**. The model copies the day; code resolves it in the facility's zone (`setuhaul/agent/dates.py`), up to 7 days ahead; past, too far, unclear or contradictory days are said so; options only on that day; "no slot on 28 Nov" is said as it is. The service re-checks the 7-day rule. Read-backs state the date. |
| 8 | `SETUHAUL_TRACE_CONTENT=full` for the synthetic pilot only, with driver PII and credentials removed. Access and retention: `docs/observability/TRACE_CONTENT.md`. |

**Golden extraction evaluation:** the prompt and schema changed, so it must be re-run. It is a paid OpenRouter run
and needs the owner's explicit approval with its cost estimate (`python -m evals.budget`-style estimate first). Not run
in this release yet.

## The historical no-show backlog (Q1)

The minute job only covers deadlines after 0048 was applied. The backlog (for example SYN-DRV-TEST-1's 26 Nov slot)
is cleared by the owner, after the release, in two audited steps:

```
python scripts/no_show_backfill.py dry-run --reason "27 Nov backlog review"
python scripts/no_show_backfill.py apply --run <run id from the dry run> --reason "27 Nov backlog, reviewed"
python scripts/no_show_backfill.py runs
```

The dry run records its report (`no_show_backfill_runs`, `booking_events`) and changes nothing. Apply transitions
exactly the reviewed set (refused with `DRY_RUN_STALE` if it changed) and is idempotent. Excluded: any appointment with
an arrival, dock activity, completion or cancellation. Backfilled drivers are not messaged; the dispatcher sees them
under **No-show**.

## Release (main → release/fde-sydney-pilot)

1. PR to `main` reviewed and approved by the owner; CI green (no branch protection on this plan: check it).
2. Golden extraction evaluation re-run, if the owner approves the paid run.
3. PR `main` → `release/fde-sydney-pilot`; then the usual sequence: fresh gate report, provisional
   `activate-booking.yml` run (applies 0047 and 0048 through the pipeline; never by hand), smoke test, renewal report,
   renewal run. **Needs a new booking approval reference from the owner.** The order is unchanged (owner, 27 Nov):
   the gate stays before the activation. Its trigger check is migration-aware (`setuhaul/ops/evidence_triggers.py`,
   `INTRODUCED_BY`): while the ledger does not record 0048, `dock_occupancy.no_show_blocks_hold` must be **absent**
   (the pre-migration state; the report lists it under `evidence_triggers_pending_migration`); once 0048 is recorded,
   the trigger is required in full, and a missing, disabled or altered one fails the gate. Every other trigger is
   required unconditionally, as before.
4. After deploy, check: both facilities still MANUAL and no AUTO; planner approval unchanged (one request decided by a
   planner); `no_show_grace_min` unchanged (Jaipur 30, Gurugram 20); Telegram limited to the two test drivers,
   WhatsApp off, production off; stack output `TraceContent` = `FULL`; one synthetic shipment through gate, dock and
   completed; the recovery worker's log line for the no-show job; then the backlog dry run.
