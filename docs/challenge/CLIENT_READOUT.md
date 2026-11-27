# SetuHaul delay desk: one-page readout

**For:** the Head of Operations and the warehouse planners.

**State on 27 Nov 2026:** synthetic data only.
- Drivers use a Telegram test bot (two test drivers). WhatsApp is off.
- Booking is on for the pilot under an owner approval, with both facilities in MANUAL.
- Production use is refused by the system.

**What it does.**
- A driver writes in their own words: *"tyre burst, 90 minutes late"*.
- The service reads the new arrival time back to the driver and asks one question if anything is missing.
- It then offers only dock times that fit the truck, the load, the dock rules and the other bookings.
- Code chooses the times, not the AI. The AI only reads the message, and replies are fixed templates.

## Booking policy
- **A driver's YES is a request, not a booking.** The slot is held for 3 minutes. The driver is told *"NOT confirmed yet"* until the warehouse decides.
- **Competing drivers:** the first valid request wins. Everyone else hears *"That slot was just taken"* and gets new options straight away. The database makes a double booking impossible.
- **MANUAL or AUTO, per facility.** Every facility starts in MANUAL.
  - In MANUAL, a planner decides every booking.
  - In AUTO, only a safe standard booking is confirmed by rule.
  - A planner always decides cargo that is critical or refrigerated, anything after hours or contradictory, dispatcher offers, time changes and cancellations.
  - Changing the mode needs a reason and is logged.
- **Priority** belongs to your planners, not to typing speed.
  - A scheduling tool proposes the order for the facility. It never books.
  - An ops lead applies moves one at a time.

## People stay in charge
- **No feasible slot:** the driver hears *"A coordinator has your details and will reply within 10 minutes"*, and the coordinator gets a complete hand-off. No slot is invented.
- **Always handled by a person:**
  - safety incidents (the coordinator calls within 5 minutes);
  - contradictory data;
  - penalties;
  - customer commitments.
- **Kill switch:** an ops lead can stop all new bookings at once. Every action is logged.

## No-show and rebooking
- A truck not at the gate by slot start plus the grace time (Jaipur 30 minutes, Gurugram 20) is marked NO_SHOW. Its dock time is released and the driver is told.
- The dispatcher then chooses either to **rebook** (a planner approves) or to **request cancellation** (a planner decides).

## Limits
- **No GPS:** ETAs come from the plan, the driver, staff and gate-in. Gate-in is entered by hand.
- **Dispatcher offers:** one option per offer. Drivers cannot cancel a confirmed booking themselves.
- **AI cost:** AI spend is capped daily. When the cap is reached, a rules reader takes over. Booking safety does not depend on the AI.
- **Proof:** double-booking safety is proven locally. With 10 and 20 drivers racing for one slot, the database was checked: 0 double bookings. A 50-driver test also gives the slot to exactly one driver. No AWS load test has been run.

## Decisions and risks for you
1. **Confirmation deadlines per shift.** Gurugram has no night cover.
2. **Priority weights and the default scheduling rule.**
   - On the Jaipur snapshot, "earliest arrival first" gave less waiting.
   - Our rule kept more promises already given to drivers.
3. **A written policy before AUTO is switched on anywhere.**
4. **Real WhatsApp** needs a Meta number and approved Hindi/Hinglish templates.
5. **Source data quality.**
   - The snapshot has double-assigned drivers and trucks, overweight bookings and docks marked ACTIVE during breakdowns.
   - We escalate these cases and never book them, but the source systems need fixing.
