# SetuHaul FDE Demo: Live Demo Runbook

**Length:** 6 minutes on stage, with a 15-minute owner preparation before the session. **Environment:** synthetic pilot `pilot-ap-southeast-2` (AWS Sydney), release `4b9ac56`. **Data:** synthetic only.

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
| Booking | ON under `OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-DEMO-01` (stack output `BookingMode`) |
| Telegram | LIVE under `OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-TELEGRAM-LIVE-01`, allowlisted to the two test drivers |
| LLM | OpenRouter → Claude Haiku 4.5 (Bedrock route, ZDR); app cap $0.25/day; key limit $10 total, no reset |
| Judge | ON, 20% sampling. **It may be skipped:** after the deploy the observed reason was `budget:shared_daily_cap`. The judge only gets the shared $0.25/day cap's headroom above the agent reserve; the configured $0.50/day judge budget is not the binding limit. The current usage balance is unknown |
| LangSmith | Project `setuhaul-synthetic-pilot`, tracing ON |
| Pilot state on 26 Nov | **ASLEEP** (all three one-minute rules DISABLED; checked read-only) |

## 2. Prerequisites (owner, T−30 to T−15 min)

| # | Check | How | Pass |
|---|---|---|---|
| 1 | Booking authorization still valid | Open `<ApiUrl>/health` in a private window (do not project it) | `booking_state: authorized`, `booking_kill_switch: disengaged`. If expired: **do not demo booking live**; use the fallback (§9) |
| 2 | Pilot awake | `python -m setuhaul.ops.pilot_mode wake`, then `python -m setuhaul.ops.pilot_mode status` | 3 rules ENABLED; nothing stuck in `SENDING` |
| 3 | Pre-warm | From test driver 2's Telegram, send one throwaway message ("status") | A reply arrives. This absorbs the ~14.6 s Aurora resume and the Lambda cold starts |
| 4 | Console sign-in | Sign in as a **planner** in one browser profile and as a **dispatcher** (or operations) in another; complete MFA | Dispatch, Conversations and Requests tabs visible |
| 5 | Telegram | Both test accounts are linked; private chats with the bot are open; crop or hide the header | Messages from the earlier test are visible |
| 6 | LangSmith | Project `setuhaul-synthetic-pilot` open, Threads view; zoom 125% | Recent threads visible |
| 7 | Budget headroom | Owner check (optional): `model_budget_day` spent today well below $0.25 | The demo needs ≈ 4–8 extractions ≈ $0.01–0.03 |
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

## 5. Exact workflow (about 6 minutes)

| # | Time | Persona | User action | System response | AI component | Tool call(s) | Deterministic control | Business outcome |
|---|---|---|---|---|---|---|---|---|
| 1 | 0:00–0:30 | Ops lead | Show booking state and kill switch (console Ops health, or the deck) | booking authorized, kill switch disengaged | none | none (read-only) | Booking is database state under an owner approval reference | The audience sees the safety envelope first |
| 2 | 0:30–1:15 | Dispatcher | Console → Dispatch → offer a dock slot for `SYN-SHP-TEST-<n>` to `SYN-DRV-TEST-1` | Driver A receives Telegram buttons (Accept / Decline) | none | dispatch offer → outbox → outbound (event kick) | Offer expiry and superseding; allowlist; dispatcher cannot decide | The driver gets the load in seconds |
| 3 | 1:15–1:45 | Driver A | Taps **Accept** | Offer ACCEPTED; request PENDING_CONFIRMATION; planner review OPEN; buttons disappear | none | accept → attestation → hold + request (database functions) | **Driver acceptance is never confirmation**; per-dock claim | A slot is held, not promised |
| 4 | 1:45–2:30 | Planner | Console → Requests → **Approve**; then open Conversations (refreshes every 3 s) | Driver A: "Your dock booking is confirmed by the warehouse." | none | `decide_booking_request` → Step Functions resumes → notice | Requester ≠ decider; approval workflow; event-driven send | The planner keeps authority; the driver is notified (event-driven send) |
| 5 | 2:30–4:00 | Driver A | Types the Hinglish delay → `haan` → `2` | Read-back, then option list, then "Request sent … NOT confirmed yet" | **LLM extraction** (Haiku 4.5): `intent=REPORT_DELAY`, `delay_kind=DELAY_DURATION`, `language=hi-Latn`; then YES and the pick | `get_driver_context`, `record_driver_report`, `find_feasible_options`, `select_option`, `confirm_selection`, `hold_option`, `submit_booking_request` | Schema; safety-word net; read-back + explicit YES; 12 hard constraints; per-dock lock + EXCLUDE | A late driver is re-slotted without a phone call; a planner still decides |
| 6 | 4:00–5:00 | Engineer | LangSmith → Threads → newest thread | `telegram_inbound` → `agent_turn` (extract, tool spans) → `telegram_outbound`; `llm_judge` child if sampled and within budget | The trace shows model, tokens, cost, latency | – | Ids only: no text, phone or chat id in traces | Any booking is explainable end to end |
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
| Ops health / Observability | planner, ops_lead | Turns by reader, tool p50/p95, model ledger (tokens, cost), optimizer runs |
| Optimizer | planner, ops_lead | Ranked options with reasons and rejected alternatives (read-only; books nothing) |

## 7. Telegram interaction notes

- Use only the two allowlisted test accounts. Any other chat gets "This chat is not linked…" and no data.
- Buttons are per driver: a forwarded button from another account is refused ("This button is not valid for your account").
- If a reply is slow (> 20 s), keep talking. The 1-minute tick is the fallback sender. On the reported six-exchange sample, end to end was about 13–15 s (preliminary; not a service level).

## 8. Trace inspection and evaluation demonstration

- **Trace:** filter by the internal driver id `SYN-DRV-TEST-1`. Expand `agent_turn` → `extract` and show model, input/output tokens, `cost_usd` and latency. Point out the absence of message text.
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
- [ ] If a live judge score is wanted, the owner decides on budgets first. No increase without explicit owner approval; otherwise show the offline judge results
