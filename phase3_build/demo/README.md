# Demo console: walk the flows by hand

A local page where you play both sides: the **driver's WhatsApp** (left) and the **warehouse console** (right),
plus a checklist of the nine business flows F1–F9 (bottom) that records what you saw.

**Real code:**
- the signed WhatsApp webhook;
- the API with staff tokens;
- the database (roles, functions, triggers);
- the agent orchestrator;
- the approval workflow;
- outbound delivery.

**Local stand-ins** (the same ones the flow tests use):
- the AI model is the **rules extractor**, so there are no model calls and no cost;
- staff tokens are signed with a key made at start-up;
- LocalQueue stands in for SQS;
- LocalApprovalWorkflow stands in for Step Functions;
- LocalProvider stands in for WhatsApp. The phone panel shows what it delivered.

The booking path runs under the local harness only. The production switch `SETUHAUL_BOOKING_ENABLED` is not read
and stays **off**. Nothing touches AWS or Meta.

## Run

Needs the local PostgreSQL on port 55432. Run from `phase3_build`:

```
python demo/server.py --reset     # fresh demo database (setuhaul_demo), migrated and seeded
python demo/server.py             # keep the data from last time
```

Then open http://127.0.0.1:8765/demo.

- It uses its own database, `setuhaul_demo`, never the test databases. Running the tests at the same time is fine.
- The seed has 4 Jaipur drivers and 1 Gurugram driver. Each has a truck due in 2–3.5 hours.
- The `+ driver` button adds a fresh driver whenever a flow needs a clean conversation.
- The demo approval window is **5 minutes**, so you can watch F3 (expiry). In production it is 15.
- Both facilities are open all day, so every flow works at any hour.

## How to drive it

- **Driver:** pick a driver, then type, or use the quick buttons.
  - The rules extractor understands simple English and Hinglish: delays ("2 ghante late"), times ("reaching by 4 pm"),
    option numbers, yes/no ("haan ji"), and safety words.
  - It can't handle free-form text the way the real model would.
- **Console:** choose a role (planner, operations, ops_lead) and a facility, then **Sign in**.
  - Requests: Confirm, Reject, Withdraw, and **Two at once** (F9).
  - Escalations.
  - Conversations: take over and hand back.
  - Docks: block a dock (F4).
  - Policy rule: the F8 demo rule, switched on and off.
  - Activity log: every agent turn and any errors.
- **Delivery tricks:** resend the last webhook or redeliver it to the agent (F6); make the next send time out, then
  send a delivery receipt (F7).
- **What the database says:** under the phone, the conversation state, the booking and approval rows, and the outgoing
  messages with their status.

Results are saved to `demo/checklist_results.json`. Keep that file as the record of the human check.
