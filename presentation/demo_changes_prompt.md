# Prompt for the code session: presentation mode for the demo console

Paste everything below the line into the other session.

---

We present SetuHaul to faculty in a 15-minute slot (live demo plus questions). Change the local demo console
(`demo/server.py`, `demo/console.html`, `demo/README.md`) so it can carry a 6-minute live demo plus a 1.5-minute
integrity proof. Change only files under `demo/`. Do not change `setuhaul/`, `migrations/` or `tests/` unless a step
below cannot work without it; if so, stop and tell me first. Do not commit. Do not run pytest against the test
database while another full test run is going (module fixtures DROP the test databases). The demo uses its own
database, `setuhaul_demo`.

## 1. AI mode switch: Real model / Rules, switchable at runtime

- Add a toggle in the console header: **AI: Real model (Haiku 4.5 via OpenRouter)** / **AI: Rules (no model call)**.
  Default when the server starts: Rules. The toggle changes the Orchestrator's `model` for the next turn only. It
  never restarts the server and never loses conversations.
- Build the real model only through `setuhaul.agent.models.model_for()` with `SETUHAUL_ENV=dev`,
  `SETUHAUL_DATA_CLASS=synthetic`, `SETUHAUL_MODEL_PROVIDER=openrouter`. Read the key from
  `%USERPROFILE%\.setuhaul\openrouter.key` into the process environment (`OPENROUTER_API_KEY`). Never print, log or
  show the key, and never send it to the browser.
- Meter real calls in the demo database's usage ledger (migration 0023), with daily cap
  `SETUHAUL_LLM_DAILY_BUDGET_USD=0.25`. If `model_for()` needs a different setup for metering in dev, use what it
  supports and tell me.
- If the key is missing or `model_for()` raises, the toggle stays on Rules and the header shows the reason in one
  line (no stack trace).
- Every agent turn shows which reader handled it: `model`, `rules` or `rules-fallback`, as a small tag on the agent's
  bubble in the phone panel (the activity log already has it). The header shows "today's model spend: $x.xxxx of
  $0.25", read from the ledger.
- You must NOT make any paid call while testing. Test the real path with a scripted or fake model client, plus one
  check that a missing key falls back to Rules. I will do the one paid smoke test myself.

## 2. Race button: many drivers, one slot (integrity proof on stage)

- Add a **"Race for one slot"** panel with N = 2, 10 or 50 (default 10).
- One click creates N fresh identical Jaipur drivers with the same fixed ETA, so every driver's first option is the
  same dock and start. Each driver goes through the real path up to an attested YES: delay message, options, pick
  the same option, yes. Then all N call `hold_option` at the same instant (threads plus a barrier, as in
  `tests/test_api.py::test_fifty_drivers_race_for_one_slot`). The race must be genuinely concurrent; the
  single-threaded worker loop is not enough.
- The race always uses the Rules reader, whatever the toggle says, so a race never makes N paid calls. Say so on
  the panel.
- The result panel shows, in large plain text:
  - "1 won, N-1 told 'taken a moment ago' with fresh options"
  - the winning driver, dock and time
  - the database check, run as a query after the race: overlapping active occupancy rows on that dock = 0
  - elapsed time
- The first two racers' phones are selectable in the driver list afterwards. The loser's phone must show the
  plain-language refusal with fresh options (decision D11), not a raw status.

## 3. No-feasible-slot escalation (beat D) in one click

- Add a preset driver whose truck no demo dock can take (for example a reefer load: the demo has no reefer dock),
  or another way to reach `NO_FEASIBLE_SLOT` from a normal delay message.
- After the delay message the driver hears the escalation message. Console > Escalations shows a NO_FEASIBLE_SLOT
  row. Add a "View hand-off record" action that shows the full record (shipment, driver, what was asked, the
  rejections or reasons, the deadline).
- If the existing SAFETY flow (F5) is the only reliable escalation, tell me before building something new.

## 4. Presentation mode: minimal checks

- Add a **Presentation** flow above the guided walkthrough. Collapse the walkthrough and the manual checklist by
  default; they must still work.
- The Presentation flow has four beats, one button each, with one to three automatic checks per beat, no more:
  - **A. Late driver**: delay > options > pick > yes > planner confirms > driver told. Check: CONFIRMED in the
    database, approval message on the phone.
  - **B. Two drivers, one slot**: the race panel with N = 2. Check: 1 OK, 1 LOST with fresh options, 0 overlaps.
  - **C. Option disappears**: a pending request, then the planner blocks the dock. Check: INVALIDATED, driver told.
  - **D. No safe outcome**: the no-slot preset. Check: NO_FEASIBLE_SLOT escalation exists, driver told.
- Each beat must finish in under 60 seconds in Rules mode. Beat A in Real mode makes at most 3 to 4 model calls.
- Add a **"Reset for presentation"** button: fresh demo database, rule switch OFF, AI toggle back to Rules.

## 5. Pre-flight and evidence

- Add a "Pre-flight" line in the header: database reachable, migrations current, workers running, AI mode, key
  present (yes/no only).
- Run only the tests that touch what you changed, plus a smoke run of beats A to D in Rules mode. Report the
  timings for each beat.
- Update `demo/README.md` with how to start the demo for the presentation and what each new control does.
- Report: files changed, what you tested, beat timings, and anything you could not verify (for example the paid
  path).
