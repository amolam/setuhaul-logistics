# SetuHaul FDE Demo: Canonical Business Workflow

**Release:** `d6b20b4` deployed on the synthetic pilot `pilot-ap-southeast-2` (27 Nov 2026; booking ON under
`OWNER-APPROVAL-20261127-DEMO-05`) · **All data synthetic.**

This is the end-to-end workflow the demo presents. Every step was checked against the deployed code. Each step has
one of three statuses:
- **Implemented**: built and deployed as described;
- **Works differently**: built, but not exactly as the target wording; the actual behaviour is stated;
- **Not implemented**: a gap, listed in §6.

Diagrams: `diagrams/17_business_workflow_by_actor.png` (deck slide 4) and
`diagrams/18_telegram_conversation_manual_vs_auto.png` (deck slide 17).

---

## 1. Who does what

| Actor | Does | Never does |
|---|---|---|
| **Dispatcher** (console) | Sees shipments that need a booking; offers a dock slot to the assigned driver or another free driver; requests a new time for a confirmed booking; requests a cancellation; after a no-show, chooses Rebook or Request cancellation | Approves any booking, time change or cancellation |
| **Driver** (Telegram) | Accepts or declines an offer; reports a delay or asks for a slot or a day in their own words; picks an option and confirms with YES | Books anything by themselves: a driver's YES is a confirmation, not a booking |
| **Agent + code + database** | Reads the driver's message (the only AI step), reads it back, finds feasible slots, holds the slot, creates the request, applies MANUAL / AUTO, marks no-shows automatically, sends every driver message from fixed templates | Approves on a person's behalf; promises a slot before it is confirmed |
| **Planner** (console) | Approves or rejects booking requests and time changes; decides cancellations; sees escalations needing a decision; resolves escalations | Approves a request they made themselves (the database refuses it) |
| **Operations** (console) | Records Arrived at gate, At dock and Completed; resolves escalations; requests cancellations | Approves bookings |
| **Ops lead** (console) | Everything operations does, plus the Controls tab (kill switch, booking holds, automation pause), moving confirmed bookings, and approving | — |

## 2. MANUAL versus AUTO approval

Each facility has an approval mode, set in the console (migration 0047). Jaipur and Gurugram are both **MANUAL**
on the pilot (Jaipur was set back from AUTO at 13:14 UTC on 27 Nov).

| Booking type | MANUAL | AUTO |
|---|---|---|
| Driver books in the chat (new booking or time change) | A planner decides | **Confirmed by rule if eligible**; otherwise a planner decides |
| Driver accepts a dispatcher's offer | A planner decides | A planner decides (**always**) |
| Dispatcher requests a new time for a confirmed booking | A planner decides | A planner decides (**always**) |
| Cancellation | A planner decides | A planner decides |

**Eligible for AUTO** means:
- a planner-authorised confirmation rule marks it auto-eligible;
- it is a standard request;
- there is no risk flag: earlier than planned, after hours or after the last start time, a busier dock, critical or refrigerated cargo, a smaller dock, an open safety escalation, contradictory data;
- it is not a dispatcher's offer.

An AUTO confirmation records the rule, its version and the evidence. It is never recorded as a person's approval.

**Driver confirmation and planner approval are two separate events:**
- **Driver confirmation** is the driver's YES, or a tap on Accept. It holds the slot for about 3 minutes and creates a request. The driver is told plainly that it is not confirmed yet.
- **Planner approval**, or an eligible AUTO rule, is the only thing that turns the request into a booking. The driver is then told it is confirmed.

## 3. The workflow, step by step

### Step 1. Customer booking received — Implemented
The shipment enters the system with an assigned carrier, truck and driver and no dock appointment. On the dispatcher's
board it shows as *Needs booking*.

### Step 2. Dispatcher-led booking — **Works differently**
- **Implemented:**
  - the dispatcher loads feasible slots for the shipment (the same feasibility engine the agent uses);
  - they choose who receives the offer: **This driver** (the assigned one) or **Another driver**, picked from the free-driver list;
  - the driver gets a Telegram message: "Booking offer from your dispatcher for order …: dock D2 at 6:00 PM on 27 Nov. Reply YES to accept, NO to decline, or tell us another time that suits you.", with Accept / Decline buttons;
  - the offer expires after about 5 minutes and is replaced if a newer one is sent;
  - on Accept the slot is held, a request is created, and the driver reads: "Thanks. Your slot is held and the request is with the warehouse planner. You will hear back here."
- **Differs from the target:**
  - an offer carries **one** slot, not a list of options. The driver accepts or declines it, or asks for another time, and then *the dispatcher* is told to send a new offer ("Understood. Your dispatcher will send you another time."). Several options to choose from exist only in the driver's own chat booking (step 3);
  - an accepted dispatcher offer **always goes to a planner**. AUTO never applies to dispatcher offers (migration 0047, `f_dispatch`).

### Step 3. Driver-initiated booking changes — Implemented
1. The driver reports a delay or asks for a time or a day, in English, Hindi or Hinglish. Examples: "Traffic on the highway. Reaching around 1:45 PM, any slot?", "slot on 29 Nov".
2. The agent reads the message (one model call). It reads an arrival time back when it needs to ("Do you mean you will reach the gate around …? Reply YES"). Then it offers feasible slots: on a requested day only, up to 7 days ahead, in the facility's time zone. It never silently offers another day.
3. The driver picks a number. The agent reads the choice back and asks for an explicit YES.
4. A planner approves, or AUTO confirms it if eligible.
5. **The existing booking stays valid until the replacement is confirmed.** It is then replaced in one transaction. If the replacement is rejected or expires, the original stands.

### Step 4. Dispatcher-initiated time changes — **Works differently**
- **Implemented:**
  - the dispatcher opens a confirmed booking, checks slots and requests a new time with a note;
  - the new slot is claimed at once, so no competing booking can take it;
  - the request always goes to a planner (PLANNER_REVIEW), whatever the facility mode;
  - the person who requested it can never approve it;
  - the booking stays confirmed until the planner approves, and is then moved in one step.
- **Differs from the target:** there is **no driver confirmation step**. The dispatcher chooses the new slot, and the driver is informed of the planner's decision.

### Step 5. Cancellation — **Works differently**
- **Implemented:** a dispatcher, operations or ops lead requests a cancellation with a reason, and a planner or ops lead decides it. Approval cancels the booking and releases its dock time in one transaction, and the driver is told.
- **Differs from the target:** a **driver cannot request a cancellation** of a confirmed booking. A driver's "cancel" only releases a slot still on hold.

### Step 6. Arrival and unloading — Implemented (with one wording difference)
Operations or an ops lead records each step in the console (Ops health → Arrivals):
- **Arrived at gate**: accepted up to slot start plus the facility's grace period;
- **At dock**: unloading started;
- **Completed**: the rest of the dock time is released, and the driver is told "Unloading complete for order …. Thank you. You are free for your next trip."

All three steps can be recorded by **operations or ops lead**. Completion is not limited to the ops lead. Each step is
idempotent and audited, and a step out of order is refused.

### Step 7. Automatic no-show — Implemented
- The minute job marks a NO_SHOW when no gate arrival is recorded by slot start plus the facility's grace period: Jaipur 30 minutes, Gurugram 20.
- Gate arrival and no-show can never both win, because both take row locks and check the same boundary function.
- The dock time is released, and the driver is told: "You did not reach the gate for order … (dock …, …) within the grace period, so the slot was released. Your dispatcher will arrange the next step."
- The dispatcher chooses **Rebook** or **Request cancellation**:
  - Rebook returns the shipment to *Needs booking* (step 2);
  - a cancellation goes to a planner; if it is rejected, the choice goes back to the dispatcher.
- Until the dispatcher decides, the database refuses any new hold or offer for that shipment.
- History from before the automatic job existed was cleared once by an audited backfill: 11 bookings on 27 Nov, after a dry run the owner reviewed.

## 4. Background controls — Implemented
- **Human escalation:** safety words or an open safety escalation, and contradictory data, always go to a person. Such a booking never becomes a request automatically.
- **Kill switch:** the ops lead's kill switch, in the Controls tab, makes the database refuse every new booking at once. It records who, when and why, and needs a typed confirmation.
- **Audit trail:** every hold, request, decision, lifecycle step, no-show, rebook, cancellation and setting change writes a row to `booking_events`, with the person or system responsible.

## 5. A realistic Telegram conversation (deployed templates)

Driver chat booking, facility in MANUAL:

> **Driver:** Traffic on the highway. Reaching around 1:45 PM, any slot?
> **SetuHaul:** Available slots:
> 1. Dock D3, 3:00 PM on 28 Nov
> 2. Dock D1, 4:00 PM on 28 Nov
> Reply with the number you want.
> **Driver:** 1
> **SetuHaul:** You selected dock D3 at 3:00 PM on 28 Nov. Shall I request it? Reply YES to confirm.
> **Driver:** YES  ← *driver confirmation*
> **SetuHaul:** Request sent for dock D3 at 3:00 PM on 28 Nov. It is NOT confirmed yet; the warehouse will reply and I will tell you.
> *(A planner approves in the console.)*  ← *planner approval*
> **SetuHaul:** Your dock booking is confirmed by the warehouse.

With the facility in AUTO and an eligible booking, the reply after YES is the confirmation itself. Seen live on 27 Nov:
"Confirmed: dock D3 at 3:00 PM on 28 Nov."

The same exchange in Hinglish uses the reviewed Hinglish templates (for example "Ye slots khaali hain: …"). The new
date-specific replies in Hindi and Hinglish still await native review.

## 6. Gaps between the target workflow and the deployed release

| # | Target | Deployed | Status |
|---|---|---|---|
| W1 | Dispatcher sends **several options**; the driver selects one | One slot per offer; to change it the driver declines or asks for another time, and the dispatcher sends a new offer | Not implemented (multi-option offers) |
| W2 | A dispatcher booking can be **auto-approved** in AUTO | A dispatcher's accepted offer always goes to a planner (by design, migration 0047) | Works differently |
| W3 | Dispatcher time change: **the driver confirms** an option | The dispatcher picks the slot and a planner decides; the driver is informed, with no confirmation step | Not implemented (driver confirmation of a dispatcher time change) |
| W4 | **Either party** can request cancellation | Staff only (dispatcher, operations, ops lead); a driver's "cancel" only releases a held slot | Not implemented (driver-requested cancellation) |
| W5 | Completed recorded by the **ops lead** | Operations or ops lead | Works differently (broader) |

Other limits that apply to this workflow:
- gate arrival is recorded manually: there is no gate scanner or geofence;
- the paid evaluation of the model on the date and reschedule prompt (p6) was deferred, so it is not model-validated;
- the Hindi/Hinglish date replies await native review.
