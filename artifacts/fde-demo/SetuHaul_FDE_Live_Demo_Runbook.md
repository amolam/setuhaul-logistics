# SetuHaul FDE Demo: Live Demo Runbook

**Length:** 6 minutes on stage, with a 15-minute owner preparation before the session. **Environment:** synthetic pilot `pilot-ap-southeast-2` (AWS Sydney). **Data:** synthetic only.

> **Updated 27 Nov 2026** for the 27 Nov fix plan (PR #45: lifecycle, no-show, dispatch, dates), the recovery releases
> (0049, category X) and the DEMO-04 release (full operational traces, one trace tree per message, $2/day model
> budget, planner Resolve, no stuck HELD). Items introduced by DEMO-04 are marked **(DEMO-04)**; DEMO-04 is deployed as `d6b20b4`
> (booking ON 13:49 UTC, Evidence Register Part D3a).

> **Safety rules for this demo**
> - Only the owner wakes the pilot, sends Telegram messages and changes switches.
> - Nothing in this runbook enables production, WhatsApp/Meta, any non-allowlisted chat, or any model outside the existing capped route.
> - The preparation in this package made **no** paid call and sent **no** message.
> - Never show these on screen: account ids, API ids, chat ids, phone numbers, the owner's email, secrets, or raw evidence JSON containing synthetic phone numbers. Crop the Telegram chat header.

---

## 1. Demo environment

| Item | Value |
|---|---|
| Stack | `SetuHaulPilot`, ap-southeast-2 |
| Booking | ON, under the newest owner booking reference (stack output `BookingMode`): `OWNER-APPROVAL-20261127-DEMO-05` on release `d6b20b4` (renewal PASS, 27 Nov 13:49 UTC). Both facilities on MANUAL approval |
| Telegram | LIVE under `OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-TELEGRAM-LIVE-01`, allowlisted to the two test drivers (unchanged) |
| LLM | OpenRouter → Claude Haiku 4.5 (Bedrock route, ZDR). App cap **$2.00/day (DEMO-04)**, $0.25 before. Key limit $10 total, no reset: **at $2/day this is the first limit to bind** (Cost Analysis §2.5) |
| Judge | ON, 20% sampling. **(DEMO-04)** $0.75/day of the $2.00 cap. Before DEMO-04 it was always skipped (`budget:shared_daily_cap`) |
| LangSmith | Project `setuhaul-synthetic-pilot`, tracing ON, content **full** (synthetic pilot only). **(DEMO-04)** only the driver's name and phone and credentials are removed; message text, model input/output, tool results, ids are exported. One tree per Telegram message: `telegram_inbound` → `agent_turn` → `telegram_outbound` |
| Database | Migrations 0001–0050 applied (0048 lifecycle, 0049 one extra recovery hop, 0050 reply trace link) |
| Pilot state on 27 Nov | **AWAKE** (booking needs the workers; owner decides when to sleep) |

## 2. Prerequisites (owner, T−30 to T−15 min)

| # | Check | How | Pass |
|---|---|---|---|
| 1 | Booking authorization still valid | Open `<ApiUrl>/health` in a private window (do not project it) | `booking_state: authorized`, `booking_kill_switch: disengaged`. If expired: **do not demo booking live**; use the fallback (§9) |
| 2 | Pilot awake | `python -m setuhaul.ops.pilot_mode wake`, then `python -m setuhaul.ops.pilot_mode status` | 3 rules ENABLED; nothing stuck in `SENDING` |
| 3 | Pre-warm | From test driver 2's Telegram, send one throwaway message ("status") | A reply arrives. This absorbs the ~14.6 s Aurora resume and the Lambda cold starts |
| 4 | Console sign-in | Sign in as a **planner** in one browser profile and as a **dispatcher** (or operations) in another; complete MFA | Dispatch, Conversations and Requests tabs visible |
| 5 | Telegram | Both test accounts are linked; private chats with the bot are open; crop or hide the header | Messages from the earlier test are visible |
| 6 | LangSmith | Project `setuhaul-synthetic-pilot` open, Threads view; zoom 125% | Recent threads visible |
| 7 | Budget headroom | Owner check: `model_budget_day` spent today well below the cap ($2.00 with DEMO-04), **and** the OpenRouter key's remaining balance (owner-run key read) | The demo needs ≈ 4–8 extractions ≈ $0.01–0.03 plus judge calls |
| 7a | No open SAFETY escalation on the demo shipments | Console → Escalations (or Schedule "Pending your decision") | None open for `SYN-SHP-TEST-*`. An open SAFETY escalation makes every booking for that shipment go to a person (by design); resolve it first: **(DEMO-04)** planners can press **Resolve…**; before, only operations/ops_lead from Ops health |
| 8 | Fallback assets open in tabs | `screenshots/`, this deck (slides 7, 11, 13), `docs/release/evidence/smoke/option_d_smoke_20261126T140741Z.json` (release branch) | Ready |

Have these commands ready. All are owner-run; none was run while preparing this package.

```
python -m setuhaul.ops.pilot_mode status
python -m setuhaul.ops.pilot_mode wake
python -m setuhaul.ops.pilot_mode sleep
python scripts/latency_report.py --minutes 30
```

## 3. Required accounts and synthetic identities

| Persona | Identity | Notes |
|---|---|---|
| Driver A | `SYN-DRV-TEST-1` (Telegram account 1, allowlisted) | Primary demo driver |
| Driver B | `SYN-DRV-TEST-2` (Telegram account 2, allowlisted) | Pre-warm, plus a second driver if time allows |
| Shipments | `SYN-SHP-TEST-<n>` at `FAC-JAI-01` | Category E renewal rule covers planner-approved bookings of these shipments (`owner_test_drivers_20261126.json`) |
| Dispatcher | Console user in the `dispatcher` group (or `operations`) | Can offer and reassign; decides nothing |
| Planner | Console user in the `planner` group, scope FAC-JAI-01 | Approves requests; must not be the requester |
| Engineer | LangSmith viewer | Read-only |

Do not put user names, emails or chat ids on slides or in the narration.

## 4. Test data

Use the synthetic shipments already assigned to the test drivers.

**Pre-stage (owner, about 10 minutes before going on stage):** the dispatcher sends Driver B an offer for another `SYN-SHP-TEST-<n>` slot and leaves it untouched. Offers expire after 5 minutes, so by step 7 it is expired.

Messages Driver A types in step 5:

| Step | Driver A types | Language |
|---|---|---|
| Delay | `bhai traffic mein phasa hoon, 45 min late ho jaunga` | Hinglish |
| Read-back answer | `haan` | Hinglish |
| Pick | `2` | – |
| Alternative (English) | `Running about 40 minutes late, tyre puncture near the toll` | English |

**Expected texts.** These two were validated over the real Bot API on 25 Nov (`docs/operations/TELEGRAM_TEST_BOT_VALIDATION_REPORT.md`, journeys A and C):
- "Your dock booking is confirmed by the warehouse."
- "This offer has expired. Your dispatcher can send a new one."

Agent replies are templates: first a read-back ("Do you mean you will reach the gate around …? Reply YES"), then a numbered option list, then "Request sent … NOT confirmed yet". **Rehearse once** to see today's times and to confirm the delay flow on an already-confirmed appointment.

## 4a. The workflow the demo shows (canonical; `SetuHaul_FDE_Business_Workflow.md`)

| Canonical step | Shown in the demo | Actor and decision |
|---|---|---|
| 1 Shipment ready, no dock slot | Dispatch board, *Needs booking* | — |
| 2 Dispatcher-led booking | Step 2–4 below: one-slot offer → driver Accept (confirmation) → planner Approve (approval) | Dispatcher offers; planner decides (always, even in AUTO) |
| 3 Driver-initiated change | Step 5: delay in Hinglish → read-back → options → pick → YES → request | Driver confirms; planner decides (AUTO only if eligible) |
| 4 Dispatcher time change | Optional: Dispatch → confirmed booking → Check slots → request a new time | Planner decides (always); no driver step today |
| 5 Cancellation | Optional: Dispatch → Request cancellation; planner decides in Schedule | Staff request; planner decides (drivers cannot request today) |
| 6 Arrival and unloading | Optional: Ops health → Arrivals → gate → dock → completed | Operations or ops lead |
| 7 Automatic no-show | Recorded: Dispatch → No-show filter → Rebook | System marks; dispatcher decides |

Say the two separate events out loud at steps 3 and 4: **the driver confirms; the planner approves.** Check that
both facilities are on **MANUAL** before the session (Facility settings), otherwise a chat booking may be
auto-confirmed and the planner step will not appear.

## 5. Exact workflow (about 6 minutes)

| # | Time | Persona | User action | System response | AI component | Tool call(s) | Deterministic control | Business outcome |
|---|---|---|---|---|---|---|---|---|
| 1 | 0:00–0:30 | Ops lead | Show booking state and kill switch (console Ops health, or the deck) | booking authorized, kill switch disengaged | none | none (read-only) | Booking is database state under an owner approval reference | The audience sees the safety envelope first |
| 2 | 0:30–1:15 | Dispatcher | Console → Dispatch → offer a dock slot for `SYN-SHP-TEST-<n>` to `SYN-DRV-TEST-1` | Driver A receives Telegram buttons (Accept / Decline) | none | dispatch offer → outbox → outbound (event kick) | Offer expiry and superseding; allowlist; dispatcher cannot decide | The driver gets the load in seconds |
| 3 | 1:15–1:45 | Driver A | Taps **Accept** | Offer ACCEPTED; request PENDING_CONFIRMATION; planner review OPEN; buttons disappear | none | accept → attestation → hold + request (database functions) | **Driver acceptance is never confirmation**; per-dock claim | A slot is held, not promised |
| 4 | 1:45–2:30 | Planner | Console → Requests → **Approve**; then open Conversations (refreshes every 3 s) | Driver A: "Your dock booking is confirmed by the warehouse." | none | `decide_booking_request` → Step Functions resumes → notice | Requester ≠ decider; approval workflow; event-driven send | The planner keeps authority; the driver is notified (event-driven send) |
| 5 | 2:30–4:00 | Driver A | Types the Hinglish delay → `haan` → `2` | Read-back, then option list, then "Request sent … NOT confirmed yet" | **LLM extraction** (Haiku 4.5): `intent=REPORT_DELAY`, `delay_kind=DELAY_DURATION`, `language=hi-Latn`; then YES and the pick | `get_driver_context`, `record_driver_report`, `find_feasible_options`, `select_option`, `confirm_selection`, `hold_option`, `submit_booking_request` | Schema; safety-word net; read-back + explicit YES; 12 hard constraints; per-dock lock + EXCLUDE | A late driver is re-slotted without a phone call; a planner still decides |
| 6 | 4:00–5:00 | Engineer | LangSmith → the newest `telegram_inbound` trace | **(DEMO-04)** one tree: `telegram_inbound` (message text) → `agent_turn` (model input and raw output, each tool with its result, the decision path) → `telegram_outbound` (the text sent); `llm_judge` child if sampled | The trace shows model, tokens, cost, latency, and what the model read and returned | – | Driver name and phone masked (`[PII]`, `[PHONE]`); credentials removed; synthetic data only | Any booking is explainable end to end, from the trace alone |
| 7 | 5:00–5:45 | Exception | **Option A (live):** Driver B taps **Accept** on the pre-staged, now-expired offer | "This offer has expired. Your dispatcher can send a new one." No appointment | none | Refused: the offer is past `expires_at` | Expiry enforced by code and data, not by a model | Stale offers cannot become bookings |
| | | | **Option B (recorded):** smoke check 6 (kill switch: `BOOKING_KILLED`, escalated, nothing booked) and check 5 (agent cannot approve); migration 0040 (a declined offer can never be booked) | – | – | – | – | Same message, zero live risk |

Optional, if time remains: approve Driver A's new request from step 5 to show the second confirmation.

**Narration rule for every step:** persona → action → what the AI did (if anything) → which tool ran → which deterministic control applied → business outcome. Steps 2–4 deliberately use **no AI**. Say so: it shows the AI is used only where language must be read.

## 6. Dashboard navigation

| Tab | Who | What to show |
|---|---|---|
| Dispatch | dispatcher / operations | Offer a shipment; offer status; Telegram delivery label |
| Conversations | planner, operations | Live thread (3 s refresh). Bot replies and notices are labelled. **Known issue:** the stored read-back record and the agent reply may show as two identical rows. Say "one is the stored record the driver's YES is checked against; only one was sent" |
| Requests | planner | Pending request → Approve |
| Ops health / Observability | operations, ops_lead (Ops health); planner, ops_lead (Observability) | Turns by reader, tool p50/p95, model ledger (tokens, cost), optimizer runs; Arrivals: gate → dock → completed (operations, ops_lead) |
| Optimizer | planner, ops_lead | Ranked options with reasons and rejected alternatives (read-only; books nothing) |
| Schedule | planner, ops_lead | "Pending your decision": booking requests, a dispatcher's new time for a confirmed booking (PLANNER_REVIEW), and escalations needing a decision (safety banner, overdue highlight). **(DEMO-04)** Resolve… on the escalation card; only live holds are drawn |
| Escalations | operations, planner, ops_lead | **(DEMO-04)** Resolve… on each open escalation (written outcome required) |
| Controls | ops_lead only | Kill switch (who/when/why, typed confirmation), booking holds, automation pause |
| Dispatch: No-show filter | dispatcher | After an automatic no-show (no gate arrival by start + the facility's grace): **Rebook** or **Request cancellation**; until then the database refuses any new hold or offer for that shipment |

**Optional 27 Nov workflows (if time allows, each ≈ 30 s):**
- *Requested date:* Driver A types "any slot on <day after tomorrow, e.g. 29 Nov>" → options only on that day, the read-out names the day; a day more than 7 days ahead is refused plainly.
- *Lifecycle:* operations → Ops health → Arrivals → Arrived at gate → At dock → Completed (completion releases the rest of the dock time and tells the driver).
- *New time for a confirmed booking:* dispatcher → Check slots → request a new time → "sent to a planner, the booking stays confirmed"; planner approves → moved in one step.

## 7. Telegram interaction notes

- Use only the two allowlisted test accounts. Any other chat gets "This chat is not linked…" and no data.
- Buttons are per driver: a forwarded button from another account is refused ("This button is not valid for your account").
- If a reply is slow (> 20 s), keep talking. The 1-minute tick is the fallback sender. On the reported six-exchange sample, end to end was about 13–15 s (preliminary; not a service level).

## 8. Trace inspection and evaluation demonstration

- **Trace (DEMO-04):** open the newest `telegram_inbound` trace (or filter by `SYN-DRV-TEST-1`). It holds the whole message: the inbound text, the `agent_turn` with `intent_extraction` → `model_call` (model input and raw output, tokens, `cost_usd`, latency), each tool with its result, `decide`, then `telegram_outbound` with the text sent. Point out what is masked: the driver's registered name and phone (`[PII]`, `[PHONE]`) and any credential. Before DEMO-04 the test drivers' traces were dropped by the export guard (their names "Telegram test driver 1/2" matched ids) — do not demo traces on a release without DEMO-04.
- **Evaluation (no paid call):** open `evals/results/judge_20261125_172110.json` → `comparison` (5/5 defects; the 95.0% there is against the AI reference), `golden_h1_model_20261126_2242.json` → `metrics` (prompt p5) and `replay_v4_live_20261126_2305.json` → `summary`. The human-calibration figures (93.3% within ±1) come from `python -m evals.langsmith.judge_calibration evals/results/judge_20261125_172110.json`, which is free and local. Do **not** start an eval run live.

## 9. Fallback plan

| Failure | Detect | Fallback | Narrative |
|---|---|---|---|
| LLM provider unavailable / budget spent | Read-back still arrives, but the console shows reader `rules-fallback` | Continue. The rules reader handles the scripted messages (prefer the English one) | "This is the designed degraded mode: booking is unaffected" |
| Telegram unavailable | No delivery in 30 s; webhook errors | Switch to recorded screens; or run the local console (`python demo/server.py`, rules reader, $0, local synthetic data) | "Channel down; the queue and outbox keep the work" |
| Database unavailable (resume stuck) | Console errors / 5xx | Wait 30 s once (resume ≈ 15 s); then recorded screens + smoke JSON | "The paused database is the cost choice for an idle pilot" |
| AWS unavailable | Console unreachable | Local demo console + deck slides 4, 7 and 11 | – |
| Frontend unavailable | Console does not load | Show the Telegram side live and the recorded console screenshots | – |
| LangSmith unavailable | Project does not load | Deck slide 11 (trace tree) + `diagrams/09_langsmith_trace_lifecycle.png` | "Tracing is a no-op when down; nothing depends on it" |
| Demo account failure (MFA, session) | Sign-in fails | Second pre-signed-in browser profile; else recorded screens | – |
| Booking authorization expired | `/health` not authorized | Do not demo booking; show steps 3–4 up to "options" and the recorded approval | "The system refuses to book without current evidence: that is the point" |
| A driver's booking stops after YES ("A coordinator will confirm the next step") | No request in Requests; Escalations shows an open SAFETY escalation for the shipment | Resolve the escalation (Resolve…), then the dispatcher sends the offer again. **Before DEMO-04** the conversation also stayed stuck at HELD: take over and hand back the conversation in the console to reset it | "Safety escalations always go to a person first" |

## 10. Reset instructions (owner, after the session)

1. Let unanswered offers and holds expire (5 min and 180 s). Do not delete rows: all bookings are synthetic and attributed (category E).
2. Save the latency output as evidence:
   ```
   python scripts/latency_report.py --minutes 60 > docs/release/evidence/demo/latency_<UTC timestamp>.txt
   ```
   It is read-only and prints no text or chat ids.
3. `python -m setuhaul.ops.pilot_mode sleep`, then `pilot_mode status`. Expect 3 rules DISABLED and nothing queued.
4. Optional, after 5 or more minutes: `python -m setuhaul.ops.pilot_mode pause-check` (Aurora at 0 ACU).
5. Note in the run sheet: what was shown, any defect seen, and the time of sleep.

## 11. Rehearsal checklist

- [ ] One full dry run the day before (≈ $0.03 of model spend, within the cap)
- [ ] Exact reply texts noted for steps 3, 4 and 7
- [ ] Recorded screen capture of a full successful run (fallback)
- [ ] Fresh anonymized screenshots saved to `artifacts/fde-demo/screenshots/` (Telegram cropped, conversation view, LangSmith thread)
- [ ] Live judge scores: with DEMO-04 the judge has $0.75/day (owner-approved 27 Nov); confirm one sampled turn shows an `llm_judge` child in LangSmith before relying on it, otherwise show the offline judge results
- [ ] Confirm one full trace tree (inbound → agent_turn → outbound) for a test driver in LangSmith after the DEMO-04 deploy
