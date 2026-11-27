# 27 Nov fix plan: build and test evidence (before proposing the release)

Branch `feat/fix-plan-27nov`, from `main` at `079c8e2`. Local machine, PostgreSQL 18.6 for the suite and an empty
PostgreSQL 18.6 cluster for the Aurora-like owner test (CI uses 17.9, the Aurora engine). Synthetic data only.
Nothing was deployed, no migration was applied to the pilot, no paid model call was made.

**This is not the pilot's gate evidence.** The release still needs the established sequence on
`release/fde-sydney-pilot` (fresh gate report, provisional `activate-booking.yml` run, smoke test, renewal report and
run) with a **new owner booking approval reference** (`docs/operations/FIX_PLAN_27NOV.md`, "Release").

## Automated tests

| Run | Result |
|---|---|
| Full suite on commit `465d59d` (`python -m pytest tests -q`) | **2,199 passed, 0 failed, 2 skipped** (the same two skips as on `main`; one is the fresh-cluster test, run separately below) |
| CI's Data API emulator subset (`SETUHAUL_TEST_DATA_API=1`, now including the four new database test files) | **743 passed** |
| New tests, workstream A: `tests/test_lifecycle_0048.py` | lifecycle end to end, step order, idempotency, exact grace boundary (shared function, per facility), no-show eligibility (8 exclusion cases), history left to the backfill, arrival vs job under row locks (both orders), 12 simultaneous arrivals and jobs, D3 rebook and cancellation (approve and reject), backfill dry run / stale refusal / apply / idempotent / append-only / drivers not messaged, RBAC by API and database, facility boundary, arrivals list, ops_lead Controls reads, `/console/me` |
| New tests, workstream B: `tests/test_dispatch_0048.py` | reschedule goes to a planner and moves atomically on approval, rejection keeps the booking, the pending slot cannot be taken by a competing booking, idempotency (same person replays, another person is refused), two dispatchers racing make one request, the requester cannot approve, roles, kill switch refusal, free drivers (assigned, overlapping, at gate, completed), explicit offer target (API and database), safety escalation visibility |
| New tests, workstream C: `tests/test_dates_0048.py`, `tests/test_reschedule_dates_0048.py`, `tests/test_trace_content_full.py` | date resolution in the facility zone, 7-day limit, past / unclear / conflicting days, model and rules reader agree, a missed day still counts, times on the named day, unchanged behaviour without a day; through the orchestrator: options only on the day, no slot said plainly with no other day, the service's own 7-day check, reschedule of a confirmed booking as a request with "yes, <day>" confirming; trace content: full / on / off rules on synthetic PII and secrets, export guard, pilot-only setting, a real traced turn |
| Aurora-like owner (`tests/test_aurora_owner.py`, `SETUHAUL_TEST_FRESH_DSN` = an empty local cluster, final 0048) | 5 passed: every migration through 0048 applies as a non-superuser owner |
| Golden extraction, rules reader (free) | identical to `main` (every metric and every turn); the gates fail on `main` too, as before (the rules reader is the no-model baseline) |
| Golden extraction, model (paid) | **not run**: needs the owner's approval. Estimate 76 calls, ~$0.28 (`anthropic/claude-haiku-4.5` via OpenRouter, nothing cached after the prompt change) |

Pins that changed on purpose (each with a comment naming the decision): the operations and dispatcher permission sets
(`tests/test_console_me.py`, `tests/test_dispatcher_role.py`), the booking-table trigger inventory
(`setuhaul/ops/evidence_triggers.py`: `no_show_blocks_hold`), the extraction schema version in the golden runner
(`tests/test_golden.py`), the pilot's trace content (`tests/test_infra.py`, `activate-booking.yml`).

## Manual checks in a local environment (`scripts/local_console.py`, 127.0.0.1, synthetic)

| Role | Checked |
|---|---|
| dispatcher | only the Dispatch tab; No-show filter (the job marked the seeded 45-minute-old booking); Rebook (back to Needs booking); load slots, "Another driver" with the free-driver list (assigned driver and a driver with a live booking nearby excluded), confirmation naming the receiver, offer sent; confirmed booking: Check slots → request a new time → "sent to a planner, the booking stays confirmed"; safety escalation banner |
| planner | no Controls or Ops tab; on Dispatch no action buttons at all, a note per action naming who can; Schedule "Pending your decision" lists the overdue SAFETY escalation (banner) and the dispatcher's PLANNER_REVIEW request (capacity claimed); Approve → the booking moved in one step (only the new slot remains); Arrivals shown without record buttons |
| operations | Ops health Arrivals; Arrived at gate → At dock → Completed on a booking inside its grace (CONFIRMED/AT_GATE → IN_PROGRESS/IN_DOCK → COMPLETED/COMPLETED) |
| ops_lead | Controls tab: kill switch refused with a wrong typed word, engaged and disengaged with who / when / why shown; a facility hold set and cleared; automation paused and resumed with who / when / why; Schedule "Store proposal and show moves" (this data needed no move, so Apply was not exercised in the browser; it is covered by `tests/test_move_appointment.py`) |

Every request the new screens made returned 200; the one 403 seen is the dispatcher's first page load asking for
Conversations before `/console/me` hides that tab (unchanged behaviour).
