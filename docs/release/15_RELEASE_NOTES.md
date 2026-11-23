# 15 — Release notes and the frozen pilot baseline

## Tags

| Tag | Commit | What it is |
|---|---|---|
| `v0.1.0-fde-pilot` | (22 Nov 2026) | Option A pilot: synthetic data, booking OFF |
| `v0.2.0-synthetic-booking-pilot` | `80f34bc` | First Option D activation (23 Nov 2026). Kept unchanged. **Known defect:** console Confirm returns HTTP 500. |
| `v0.2.1` | `4f7da62` | Synthetic booking-enabled pilot baseline (23 Nov 2026). Kept unchanged; the rollback target for v0.3.0. |
| **`v0.3.0`** | **`1d304f8`** | **Phase G on the synthetic booking-enabled pilot** (23 Nov 2026). This is what is deployed. |
| `option-d-evidence-20261123` | `6b0a8ec` | The activation evidence as first recorded |

**Wording:** this is a *synthetic booking-enabled pilot*, not a production logistics deployment.

## v0.3.0 (Phase G, 23 Nov 2026)

### Deployment

| | |
|---|---|
| Branch and commit | `release/fde-sydney-pilot` fast-forwarded from `4f7da62` to `1d304f8` (merge of PR amolam/setuhaul-logistics#3) |
| Activation run | [activate-booking 45173727105](https://github.com/amolam/setuhaul-logistics/actions/runs/45173727105): passed (test on PostgreSQL 17.9, synth, activate) |
| Approval reference | `OWNER-APPROVAL-20261123-PHASE-G` |
| Deployment id | `m-a6ae8e5b7e9458595c27c099d109201abf1aaf9c` |
| Artifact | `234c266b…` |
| Migration | `0027_phase_g_observability` applied over the Data API (additive: 3 append-only tables, 4 definer functions) |
| Booking | ON for `setuhaul-pilot-api` and `setuhaul-pilot-agent` only; every other function OFF |
| `/health` | `booking_enabled: true`, `booking_kill_switch: disengaged`, `gate_exceptions: [AURORA, OUTBOUND_DELIVERY, WORKLOAD_IDENTITY]` |

**Gates:** the same PASS evidence (AWS_QUEUE, RECOVERY_WORKER, BOOKING_WORKFLOW) and owner-approved exceptions as
v0.2.1, re-recorded for this deployment. The recorded rows restart their 7-day validity, but the gate report itself is
still the one collected on 23 Nov, so **the renewal date stays 30 Nov 2026**.

### Changes since v0.2.1

- **Optimizer** (`setuhaul/domain/optimizer.py`): deterministic dock/slot optimizer with 12 hard constraints. Driver-path
  ranking is **OFF** (`SETUHAUL_OPTIMIZER_RANKING` not set); turning it on is the owner's booking-policy decision.
- **Console:** new **Optimizer** and **Observability** tabs for planner and ops_lead (operations unchanged).
  `GET /console/shipments/{id}/recommendations` is read-only and books nothing. Both routes read with the console login
  only (checked before release with exactly the api's `ingest` and `console` logins).
- **Telemetry:** structured `optimizer.run` and `agent.turn` log lines with correlation ids and no personal data;
  per-turn and per-run rows in the new tables.
- **Expired slot offers (v0.2.1 open item, fixed):** a pick after the offer expired is now answered with "those slots
  expired" and fresh options instead of "Sorry, I did not understand".
- Evaluation suite (`evals/phase_g/`) and Locust load test (`perf/`): local only; they refuse any non-local target.

### Checks after the deploy

- `/health` as above; new console routes answer 401 without sign-in (present, not 500); the served console script
  contains the new tabs.
- Not yet checked live: the two tabs signed in as a planner, and the new log lines in CloudWatch after a driver turn.

### Open items carried forward

- Native-speaker review of the new Hindi/Hinglish expired-offer wording.
- Rules-reader findings I14 ("finish unloading by 5 pm" not read as a deadline) and I15 ("pahunch gaya hoon gate pe"
  read as a delay).
- `GET /console/facilities/{id}/schedule-proposal` (Phase F) needs a feasibility login the api does not have: expect
  HTTP 500 on the pilot.
- `/health` still reports `version: 0.2.0` (the application version string was not bumped).
- **Reliability measurement:** a sustained sequence of synthetic logistics exceptions; plan to be agreed.
- **Weekly renewal** of the booking evidence (next by 30 Nov 2026).

## v0.2.1 (previous baseline, 23 Nov 2026)

### Deployment

| | |
|---|---|
| Branch and commit | `release/fde-sydney-pilot` at `4f7da62` |
| Activation run | [activate-booking 45140560077](https://github.com/amolam/setuhaul-logistics/actions/runs/45140560077): passed (test, synth, activate) |
| Deployment id | `m-7426b8525e2b51f24906a7654a865157ea45d94a` |
| Artifact | `9c20d852…`; all 8 workload functions verified to run it |
| Booking | ON for `setuhaul-pilot-api` and `setuhaul-pilot-agent` only; every other function OFF. Stack output `BookingMode = ON (OWNER-APPROVAL-20261123-OPTION-D)`. |
| `/health` | `booking_enabled: true`, `booking_kill_switch: disengaged`, `gate_exceptions: [AURORA, OUTBOUND_DELIVERY, WORKLOAD_IDENTITY]` |

**Gates:**
- PASS evidence: AWS_QUEUE, RECOVERY_WORKER, BOOKING_WORKFLOW.
- Owner-approved pilot exceptions:
  - AURORA: T33-P;
  - WORKLOAD_IDENTITY: NO-LIVE-DRIFT-DETECTION;
  - OUTBOUND_DELIVERY: META-DEFERRED-TEST-SINK-ONLY.

The evidence and exceptions are valid for 7 days. **Re-run `activate-booking` by 30 Nov 2026**, or the api and agent refuse to start.

### Changes since v0.2.0

- **Console Confirm returned HTTP 500 (`8fe6935`).**
  - Cause: the advisory feasibility re-check needed a database login the deployed api does not have.
  - Fix: the check is skipped where that login is absent; `decide_booking_request` still re-proves every rule. No permission was widened.
  - Regression test: uses exactly the deployed api logins.
- **The read-back looked duplicated in the console (`4f7da62`).**
  - One row is the stored read-back record the driver's YES is checked against. It is never sent; the driver always received the read-back once.
  - The console now labels it **READ-BACK RECORD (not sent)**. `GET /console/threads/{id}` returns `read_back_record`.
- The console's outdated "booking execution is off" empty-state text was replaced.

Both defects were found by the owner's hands-on test on the pilot.

### Tests at the tag

- 16 of 16 conversation-view tests, locally on PostgreSQL and through the Data API path.
- The activation run's full suite on PostgreSQL 17.9.

### Open items carried forward

- **Expired slot offers:** when a driver's pick arrives after the 5-minute slot offer expired, the agent says "Sorry, I did not understand" instead of saying the slots expired and offering new ones.
- **Reliability measurement:** a sustained sequence of synthetic logistics exceptions; plan to be agreed.
- **Weekly renewal** of the booking evidence (next by 30 Nov 2026).

## v0.2.0-synthetic-booking-pilot

See `14_OPTION_D_ACTIVATION.md`: migration 0026, the kill switch, owner-approved exceptions, the recorded manifest and the activation workflow; smoke test 11 of 11.
