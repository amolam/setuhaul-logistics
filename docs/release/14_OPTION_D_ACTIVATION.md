# 14 — Option D activation on the synthetic Sydney pilot (23 Nov 2026)

**Authority:** the owner's written decision "FINAL DECISION — OPTION D APPROVED", recorded as
`OWNER-APPROVAL-20261123-OPTION-D`. Scope: the **synthetic** Sydney pilot only. No production deployment and no merge
to `main`.

**Accepted pilot exceptions:**

| Gate | Exception | Condition the system enforces |
|---|---|---|
| AURORA | **T33-P** | The live server is PostgreSQL 17.9 (17.x, never 18), on the pilot only. Production stays 18.3+ (T33). |
| WORKLOAD_IDENTITY | **NO-LIVE-DRIFT-DETECTION** | Every acceptance check passes live; the only item not shown is drift detection. Permission fingerprinting continues (manifest `iam_policies_sha256`). |
| OUTBOUND_DELIVERY | **META-DEFERRED-TEST-SINK-ONLY** | The deployed sender is the internal test sink with WhatsApp off. |

## Result

**Booking is ON for the synthetic pilot.**

| | |
|---|---|
| Activation run | [activate-booking 45129676384](https://github.com/amolam/setuhaul-logistics/actions/runs/45129676384) (`release/fde-sydney-pilot` at `80f34bc`) |
| Deployment id | `m-5bb1fd39371dec9621df873b22cdf5a9d7f2e339` (`evidence/option_d_manifest.json`, `evidence/option_d_activation.json`) |
| PASS evidence | AWS_QUEUE, RECOVERY_WORKER, BOOKING_WORKFLOW |
| Owner-approved exceptions | AURORA (T33-P), WORKLOAD_IDENTITY (NO-LIVE-DRIFT-DETECTION), OUTBOUND_DELIVERY (META-DEFERRED-TEST-SINK-ONLY) |
| Live `/health` | `booking_enabled: true`, `booking_kill_switch: disengaged`, `gate_exceptions: [AURORA, OUTBOUND_DELIVERY, WORKLOAD_IDENTITY]` |
| Booking switch scope | Only `setuhaul-pilot-api` and `setuhaul-pilot-agent`. Every other function keeps it off. |

## What had to be built first (all on `phase-f/challenge-completion`; CI green on PostgreSQL 17.9)

The deployed `3a8ffd8` could not turn booking on. Its fail-closed start-up check needs a deployment manifest and evidence for all six gates; nothing delivered the manifest to the functions; three gates are owner exceptions rather than passes; and there was no kill switch.

### Migration 0026 (additive; no row changed or removed)

- **`deployment_manifests`:** the pipeline records the manifest of what it deployed. The functions accept it only if it names their own code hash (`lambda:GetFunctionConfiguration` on themselves), so a stale manifest keeps the switch closed.
- **`deployment_exceptions`:** only the three accepted gate/code pairs; never a production environment; at most 30 days (written for 7); bound to the live configuration fingerprint; append-only; written only by the pipeline's `setuhaul_deploy_evidence` login. An exception is never written as evidence.
- **`booking_kill_switch`:** while engaged, database triggers refuse every new booking commitment, whatever the application or its deployment says:
  - a new hold or appointment claim on dock time;
  - a changed time or dock;
  - a new appointment;
  - a confirmation.

  Releasing, rejecting, expiring, cancelling and operational dock blocks still work. It is operated by an ops_lead in the console (`POST /console/booking-kill-switch`, `set_booking_kill_switch`) or by the owner (`python -m setuhaul.ops.kill_switch engage|disengage --reason …`). Both are audited in `booking_events`, and neither needs a deployment.
- **`booking_gate_snapshot()`:** one read-only definer function (owner `setuhaul_rpc`) for the start-up check. It returns no facility, driver or booking data. The api and agent logins got this function, not table grants; the privilege allow-list tests pin it.

### Workflows and tests

- **`.github/workflows/activate-booking.yml`:**
  - started by hand, on the release branch only, with the typed confirmation and the approval reference;
  - runs the full suite on 17.9;
  - synthesises with booking ON for the api and agent only, and asserts that in the template (sender: test sink, WhatsApp off);
  - deploys, then records the manifest, evidence and exceptions with the deploy-evidence login only;
  - waits for `/health` to report booking ON.

  `ci.yml` still never turns booking on (its next run turns it off again).
- **Tests:** `tests/test_option_d.py` (12), `tests/test_infra.py` (switch scope; activation workflow guards), `tests/test_agent_not_operator.py` (allow-lists, with reasons). CI: 1,152 + 437 (Data API) passed before activation.

## Pre-activation checks (owner's list)

| # | Check | Result |
|---|---|---|
| 1 | Deployed commit is `3a8ffd8` | **PASS.** All 8 workload functions' code hash equals the `3a8ffd8` deploy manifest (run 45114941968). Activation then deployed the successor commits above, which is necessary: `3a8ffd8` cannot activate. |
| 2 | Six gate statuses and documented exceptions | **PASS.** Re-collected after deploying 0026 (`evidence/step6_gates.json`, 07:02 UTC): 3 PASS; AURORA PASS WITH PILOT EXCEPTION; WORKLOAD_IDENTITY PARTIAL (drift detection only); OUTBOUND_DELIVERY BLOCKED (Meta). Every check passes. |
| 3 | Isolation of synthetic booking data | **PASS.** Every function `SETUHAUL_DATA_CLASS=synthetic`; 0 non-synthetic carriers, drivers or vehicles; 0 non-`SYN-` shipments; 0 deliveries outside the test sink; outbound sender test sink with WhatsApp off. |
| 4 | Kill switch works | **PASS, 8 of 8 live** (`evidence/kill_switch_live.json`). Owner path and ops_lead console path each engaged a commitment refused `BOOKING_KILLED` and each disengaged it; a planner was refused; four audit rows were written; nothing committed. |

## Post-activation smoke test (`scripts/smoke_option_d.py`, `evidence/option_d_smoke.json`): 11 of 11

A synthetic driver (`SYN-DRV-2172ACB8`, shipment `SYN-SHP-2172ACB8`, +999 number) talked to the **deployed** agent through signed webhooks:

| Turn | WhatsApp id (synthetic) | API request id | Agent reply |
|---|---|---|---|
| "Traffic … reaching around 1:45 PM, any slot?" | `wamid.SYN.3b6e975e…` | `0b240505-8c0d-449b-a1fa-348dec2f9442` | three options, 3:00 PM |
| "1" | `wamid.SYN.d1c589c3…` | `f2515228-ff24-4fdd-9101-da60e8337c99` | read-back for dock D1, 3:00 PM; reply YES |
| "YES" | `wamid.SYN.4fe33cce…` | `f048aa17-1528-4edc-a911-44e6b94a6e7e` | request sent, **NOT confirmed yet** |

| Booking outcome | |
|---|---|
| Appointment | `APT-01c31ce02a444ad79d6f43d0fa874cc0`: PENDING_CONFIRMATION, then **CONFIRMED** (dock `DOCK-JAI-D1`, 09:30–10:25 UTC) |
| Approval | `73cac501-fe58-457a-80d5-b90234d56139`: OPEN (no automatic approval), then **APPROVED** by `staff:option-d-smoke-planner` (planner, HUMAN) |
| Workflow execution | `…:execution:setuhaul-pilot-approval:73cac501-…`: one execution, **SUCCEEDED** |
| Audit (`booking_events`) | HOLD_OPTION (agent, 07:30:04), SUBMIT_BOOKING_REQUEST (agent, 07:30:05), APPROVAL_APPROVED (planner, 07:30:49) |
| Driver notice | `booking_approved`: SENT to `testsink.2af87d43c60d44c08163690cef00e590` (internal test sink, not WhatsApp) |

**Safety checks:**

| Check | Result |
|---|---|
| Idempotency | The same YES (same WhatsApp id) resent: still 1 request, 1 active appointment. The same decision key returned the same decision. A second decision was refused. |
| Double-booking | Another claim on `DOCK-JAI-D1` 09:30–10:25 was refused: `23P01 ex_one_truck_per_dock` (rolled back). |
| Authorization | Agent role deciding: `42501`. GGN planner: `APPOINTMENT_UNKNOWN` (RLS). No staff identity: `UNTRUSTED_CONTEXT`. |
| Kill switch (owner) | Engaged: `/health` showed ENGAGED. A second driver (`SYN-DRV-B9A53402`) went through to YES; the deployed agent's turn logged `confirmation->book->escalated:BOOKING_KILLED`; 0 appointments, 0 requests; the driver was told a coordinator will reply. Disengaged afterwards; both changes audited. |
| Isolation | 0 non-synthetic identities; 0 deliveries outside the test sink. |

**Corrections, stated openly:**

- **Check 6, first evaluated as a fail.** The script's extra direct hold passed no driver attestation, so the database refused it `ATTESTATION_INVALID` before the kill switch could. The check now uses the deployed agent's own refusal (the log line above), in the script and in the evidence file (`check_correction`). The direct database refusal was proven live in check 4.
- **Two failed attempts first:**
  - Activation run 45128314359 failed closed: the writer function was defined after the `__main__` guard. Nothing was written, and the api and agent refused to start from about 07:10 to 07:23 UTC; no booking was possible. Fixed, with a module-run test and an end-to-end Data API test.
  - The first smoke run stopped before booking (the scripted replies did not follow the agent's questions). It left one synthetic conversation, with no booking.

## Operating notes

- **Turn booking off (two independent ways):**
  - immediately: the kill switch (ops_lead or owner), no deployment;
  - fully: run the ordinary `ci.yml` deployment (booking OFF everywhere).
- **Weekly renewal:** evidence for the operational gates and the exceptions lasts 7 days. After that the api and agent **refuse to start** (fail closed). That stops the console and driver chat, not just booking. Re-collect the gates and re-run `activate-booking` before then (by **30 Nov 2026**).
- **Retained synthetic records (not cleaned up):** the confirmed smoke booking `APT-01c31ce0…`, the two smoke drivers, the first smoke run's driver, and all earlier Step 6 drill records.
- **Model use:** the smoke test's extraction fell back to the rules reader on at least one turn (`extractor=rules-fallback` in the agent log). The booking path does not depend on the model.
