# SetuHaul delay desk — client readout

**For:** the Head of Operations and the warehouse planners.
**Status:** pilot on synthetic data, 23 Nov 2026. No real driver, customer or shipment data has been used.

## What it does

Drivers report delays by WhatsApp in their own words, in English, Hindi or Hinglish. For example: *"tyre burst near Neemrana, around 90 minutes late"*. The service:

1. **Understands the message.** It reads the delay, the new arrival time and anything the driver cannot do, such as "I must leave by 9". If something is missing or unclear, it asks one short question.
2. **Reads the time back before acting.** For example: *"Do you mean you will reach the gate around 7:30 PM? Reply YES."* Nothing moves until the driver confirms.
3. **Offers real options.** These are later dock slots that fit this truck, this load and your dock rules, worked out by the scheduling rules and not by the AI.
4. **Takes the driver's choice to your planners** as a request. A slot is promised only when a planner approves it, or when a rule you have authorised in writing allows it.
5. **Tells the driver the outcome** in the language of the conversation.

## What stays with your staff

| Decision | Who decides |
|---|---|
| Confirming or rejecting a requested dock appointment | A planner, in the staff console |
| Moving an already confirmed appointment | An operations lead, from a scheduling proposal |
| Anything involving safety (accident, injury, threat, medical) | A coordinator. The service stops and hands the conversation over at once. |
| No slot fits the truck | A coordinator. The service escalates with a full hand-off note and never invents a slot. |
| Penalties, compensation, customer commitments, disputed or contradictory data | A person, always |
| Which truck goes first when capacity is short | Your written priority policy. The facility schedule proposes; a planner decides. |

**The AI only reads messages.** It never books, approves, prioritises or promises. Every booking decision is made by fixed rules and checked by the database, which refuses anything that would double-book a dock.

## How scheduling proposals work

The facility scheduling tool looks at one facility's whole day at once. It considers:
- the trucks already unloading, which it never moves;
- early trucks and late trucks;
- each truck's priority;
- the times already promised to drivers.

It proposes an order with a plain reason for each truck, and lists any truck it cannot fit; those go to a person. A proposal is only a suggestion. Planners can view and save proposals. Only an operations lead can apply a move, one at a time, and the database checks it again at that moment. The staff console has no screen for this yet; it is done through the console's API. Moves also stay off while booking is off. If anything has changed since the proposal was made, the move is refused and a fresh proposal is needed.

**Compared with two simple rules**, first-come-first-served and earliest arrival first. Full tables: `docs/3-build/SCHEDULING_TOOL.md`.

- **On a test day with an early truck, a late truck and one truck already unloading:**
  - **Promises:** our rule changed 1 promised time where the simple rules changed 5 and 4, and kept 4 promises against 0 and 1.
  - **Easy to apply:** its one change can be applied at once. Several of the simple rules' changes swap trucks with each other, so they can't be applied one by one.
  - **The price is waiting.** Under the simple rules the early truck docks straight away. Our rule keeps it in its promised slot, 75 minutes later, rather than move other drivers.
- **On the challenge's real Jaipur snapshot:** earliest arrival first came out best overall and with the least waiting. Our rule kept one more promise, to the critical load, but made two late trucks wait longer. In every rule, the five trucks with conflicting bookings went to a person.

**What this means:** the facility rule is right when changing a time a driver was already given is costly. It is wrong when one late, high-priority truck should take a promised slot. Both trade-offs show in each proposal, so the planner sees them before applying anything. Changes are applied one at a time, and only by an operations lead.

## Proof that it does not double-book

Many drivers can ask for the same slot within seconds, so we tested exactly that. Competing requests for one slot were fired at the same moment:
- exactly one got it;
- every other driver was told plainly that the slot had just been taken, and was offered the next options;
- the database shows one booking for that slot and no overlap on that dock.

The details are in `docs/challenge/INTEGRITY_PROOF.md`. The wider attack and failure testing is in `docs/challenge/BREAK_REPORT.md`.

## What is live today, and what is not

| | Status |
|---|---|
| **Option A pilot** on AWS Sydney | **Live, synthetic data only.** <ul><li>Drivers' messages are read, options are offered, and replies go to an internal test inbox, not to WhatsApp.</li><li>The staff console works, with sign-in and two-step verification.</li></ul> |
| **Option D: booking requests and approvals** | **Built and tested, but switched OFF.** It turns on only after all six release checks pass on AWS, and with your written approval. |
| Real WhatsApp messages | **Off.** This needs a Meta test number and Meta-approved message templates. |
| Production use | **Refused** by the system itself until a separate decision is made. |

## Limits you should know

- **Text only:** the service has no GPS or telematics. ETAs come from the plan or from what the driver says, and it treats a repair time and an arrival time differently.
- **No requests to decide yet:** while booking is off, the console's Requests list stays empty.
- **AI budget:** the AI reader is capped at $0.25 a day. When the cap is reached, a simpler rules-based reader takes over, which understands less varied phrasing.
- **Hinglish templates:** Meta must approve the Hinglish message templates separately. Until then, a Hinglish-speaking driver contacted outside WhatsApp's 24-hour window gets the English template.
- **Database version:** the cloud database is PostgreSQL 17.9, the only version on the current free AWS plan. The design asked for 18. The same software is tested on both, and this needs your sign-off as a pilot exception.

## Open risks and decisions for you

1. **Release checks:** approve the remaining live checks (a dead-letter queue drill, and a test run of the approval workflow) and the database-version exception. Then provide the Meta test number and templates.
2. **Automatic confirmation:** any rule that confirms without a person (for example, low-risk night arrivals at Gurugram) needs your written policy. None is switched on.
3. **"Talk to a person" button:** deferred at your request. Drivers can still reach a coordinator through the safety words and the escalation path.
4. **Peak capacity:** before any automatic rule relies on "how busy is the dock", peak occupancy should be measured against usable capacity (review item R3 Q3).
