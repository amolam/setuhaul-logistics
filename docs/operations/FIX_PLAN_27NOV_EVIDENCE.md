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
| After the static-review fixes: full suite | **2,201 passed, 0 failed, 2 skipped** (the two new tests added) |
| After the static-review fixes: CI's Data API subset | **745 passed** |
| After the migration-aware gate change: the affected files (gate triggers, recovery release A and its hardening, readiness, renewal gate, infra, lifecycle, trace content) | **337 passed**; the whole suite runs in CI on the commit |
| New tests, workstream A: `tests/test_lifecycle_0048.py` | lifecycle end to end, step order, idempotency, exact grace boundary (shared function, per facility), no-show eligibility (8 exclusion cases), history left to the backfill, arrival vs job under row locks (both orders), 12 simultaneous arrivals and jobs, D3 rebook and cancellation (approve and reject), backfill dry run / stale refusal / apply / idempotent / append-only / drivers not messaged, RBAC by API and database, facility boundary, arrivals list, ops_lead Controls reads, `/console/me` |
| New tests, workstream B: `tests/test_dispatch_0048.py` | reschedule goes to a planner and moves atomically on approval, rejection keeps the booking, the pending slot cannot be taken by a competing booking, idempotency (same person replays, another person is refused), two dispatchers racing make one request, the requester cannot approve, roles, kill switch refusal, free drivers (assigned, overlapping, at gate, completed), explicit offer target (API and database), safety escalation visibility |
| New tests, workstream C: `tests/test_dates_0048.py`, `tests/test_reschedule_dates_0048.py`, `tests/test_trace_content_full.py` | date resolution in the facility zone, 7-day limit, past / unclear / conflicting days, model and rules reader agree, a missed day still counts, times on the named day, unchanged behaviour without a day; through the orchestrator: options only on the day, no slot said plainly with no other day, the service's own 7-day check, reschedule of a confirmed booking as a request with "yes, <day>" confirming; trace content: full / on / off rules on synthetic PII and secrets, export guard, pilot-only setting, a real traced turn |
| Aurora-like owner (`tests/test_aurora_owner.py`, `SETUHAUL_TEST_FRESH_DSN` = an empty local cluster, final 0048) | 5 passed: every migration through 0048 applies as a non-superuser owner |
| Golden extraction, rules reader (free) | identical to `main` (every metric and every turn); the gates fail on `main` too, as before (the rules reader is the no-model baseline) |
| Golden extraction, model (paid) | **not run: deferred by the owner on 27 Nov 2026** (not approved at this stage; to be reconsidered once API credits are available and the run is still needed). Estimate 76 calls, ~$0.28 (`anthropic/claude-haiku-4.5` via OpenRouter, nothing cached after the prompt change), over the $0.10 default cap. **Model-backed extraction on schema v2 / prompt p6 is therefore unvalidated**: the passing suite and the rules-reader baseline do not stand in for it |

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

## Static review after CI (27 Nov 2026)

GitHub CI on `9c38958`: `test` and `synth` passed (`deploy` skipped, as for every pull request). Then a read-through
of migration 0048, the trigger changes and the trace redaction. Two findings, both fixed with a test that fails
without the fix:

| Area | Finding | Fix |
|---|---|---|
| 0048 `process_no_shows` | Appointments the job must leave alone stay CONFIRMED (for example the TMS shows the truck at the gate but nobody recorded the arrival). They were only rejected inside the loop, after the batch `LIMIT`, so enough of them would fill every batch and later due appointments would never be marked | the eligibility rule is applied in the job's query, before the `LIMIT`; `mark_no_show` still re-checks under the lock. Test: `test_ineligible_appointments_never_starve_the_job` |
| Trace redaction (full mode) | Only the driver's full registered name was masked; a first name written alone passed unless the Telegram profile carried it | each part of the registered name (3+ characters) is also a masked term. Test: `test_a_first_name_alone_is_masked_too` |

Checked with no change needed:
- The grace boundary is one function for both arrival and no-show.
- Row locks:
  - the job takes SKIP LOCKED and the backfill takes FOR UPDATE;
  - the job and the backfill never select the same rows, because the cutover splits them;
  - lock order is the same everywhere: the appointment, then the shipment.
- The backfill accepts only a matching dry run, and a second apply transitions nothing.
- The no-show block covers every insert into `dock_occupancy` other than a BLOCK: the hold (where a booking request starts), the dispatch offer, and the move and reschedule claims, which require a CONFIRMED booking.
- Every new SECURITY DEFINER function fixes its `search_path`, and every scope it widens is restored before it returns.
- The requester can never approve a time change, because a database trigger refuses it.
- The model input in a trace is the per-turn block only; the system instructions are exported by version and never as text.
- Credentials are dropped before the full-mode pass-through, and the export guard re-checks the finished payload.

Noted, not changed: `free_drivers_for` has no role check of its own in the database. The API gates it (`view_free_drivers`), and it returns only the shipment's carrier's drivers, within the caller's facility scope.

## Release ordering: the gate's trigger check is migration-aware (27 Nov 2026, owner decision)

The deployment order stays as it is: the gate report comes before the provisional activation, which is the only step
that applies 0047 and 0048. Requiring 0048's trigger before that run would make the order impossible to satisfy, so
the check now reads the migration ledger (`setuhaul_meta.schema_migrations`):

| Ledger | `dock_occupancy.no_show_blocks_hold` | Gate |
|---|---|---|
| 0048 not recorded | absent | pass, listed under `evidence_triggers_pending_migration` |
| 0048 not recorded | present (added by hand) | **fail**: "present, but migration 0048 is not recorded as applied" |
| 0048 recorded | present, enabled, as specified | pass |
| 0048 recorded | missing, disabled or altered | **fail** (never treated as pending) |

Every other trigger is required whatever the ledger says, so nothing else is relaxed.

Tests in `tests/test_gate_triggers_0048.py`:
- a real upgrade from 0047 to 0048 on a fresh database, checking each row of the table;
- the gate's AURORA function itself, with AWS faked and the ledger before and after 0048;
- older triggers still required with an empty ledger.
