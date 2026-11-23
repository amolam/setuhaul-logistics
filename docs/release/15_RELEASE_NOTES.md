# 15 — Release notes and the frozen pilot baseline

## Tags

| Tag | Commit | What it is |
|---|---|---|
| `v0.1.0-fde-pilot` | (22 Nov 2026) | Option A pilot: synthetic data, booking OFF |
| `v0.2.0-synthetic-booking-pilot` | `80f34bc` | First Option D activation (23 Nov 2026). Kept unchanged. **Known defect:** console Confirm returns HTTP 500. |
| **`v0.2.1`** | **`4f7da62`** | **Frozen synthetic booking-enabled pilot baseline** (23 Nov 2026). This is what is deployed. |
| `option-d-evidence-20261123` | `6b0a8ec` | The activation evidence as first recorded |

**Wording:** this is a *synthetic booking-enabled pilot*, not a production logistics deployment.

## v0.2.1 (frozen baseline, 23 Nov 2026)

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
