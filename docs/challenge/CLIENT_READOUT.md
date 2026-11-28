# SetuHaul delay desk: one-page readout

**For:** the Head of Operations and the warehouse planners. **State:** 28 Nov 2026. Built and tested on made-up data and on your 4 August 2026 snapshot. Nothing is in production.

## What it does
- A late driver writes in their own words: *"tyre burst, 90 minutes late"*.
- The service reads the new arrival time back and asks one question only if something is missing.
- It offers only dock times that fit the truck, the load, the dock rules and the other bookings.

## What is automated
- **Reading the message.** An AI model or a rules reader turns the text into facts, and does nothing else.
- **Choosing dock times.** Code and the database do this, never the AI. Replies are fixed sentences.
- **Stopping double bookings.** The database refuses a second truck on the same dock at the same time.

## Booking policy
- **A YES is a request, not a booking.** The slot is held for 3 minutes and the driver is told *"NOT confirmed yet"* until the warehouse decides.
- **When drivers compete, the first valid request wins.** Everyone else hears *"That slot was just taken"* and gets new options at once.
- **A person confirms every booking** (MANUAL mode). A planner or ops lead decides anything critical, refrigerated, after hours or offered by a dispatcher. An operations coordinator can also decide a standard booking.
- **AUTO mode exists but is off.** It would confirm only safe standard bookings.
- **Priority belongs to your people.** A scheduling tool only proposes an order; an ops lead applies each move.

## What stays with people
- **No workable slot:** the driver hears *"A coordinator has your details and will reply within 10 minutes"*, and the coordinator receives the full case. No slot is invented.
- **Always a person:** safety incidents, contradictory data, penalties, customer commitments, time changes and cancellations.
- **Stop button:** an ops lead can stop all new bookings at once. Every action is logged.

## Limits
- **No GPS.** Arrival times come from the plan, the driver, your staff and a hand-entered gate arrival.
- **Small sample.** The snapshot has two facilities, nine docks and 21 shipments. Your real volume has not been load-tested.
- **Proof is local.** With 10, 20 and 50 drivers racing for one slot, exactly one got it each time. For 10 and 20 we also counted double bookings in the database: zero.
- **WhatsApp is not connected.** A private pilot uses Telegram with two test drivers.
- **Not measured yet:** driver waiting time at the dock, dock utilisation and priority-rule breaches.

## Risks and decisions for you
1. **Confirmation deadlines per shift.** Gurugram has no night cover.
2. **Priority weights and the scheduling rule.** On the Jaipur snapshot, "earliest arrival first" gave less waiting; our rule kept more promises already given.
3. **A written policy** before AUTO is switched on anywhere.
4. **Real WhatsApp** needs a Meta number and approved Hindi and Hinglish templates.
5. **Source data quality.** The snapshot has drivers and trucks assigned twice, overweight bookings and docks marked active during a breakdown. We send these to a person, but the source systems need fixing.
