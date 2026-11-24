# Booking evidence renewal gate: gap assessment

Branch `feature/booking-evidence-renewal-gate` from `main` `e280a40` (Phase G.1 merged). Assessed 23 Nov 2026 against
the live synthetic pilot (v0.3.0, booking ON). Read-only throughout: nothing deployed, no configuration or booking state changed.

## 1. Why the current gate fails on an active pilot

`scripts/step6_gate_evidence.py` was written as the **pre-activation** acceptance (Step 6, booking OFF). Collected fresh
on 23 Nov 2026 against the active pilot:

| Gate | Fresh result | Why |
|---|---|---|
| AWS_QUEUE, RECOVERY_WORKER | PASS | infrastructure, mode-independent |
| AURORA | PASS WITH PILOT EXCEPTION (T33-P) | approved exception |
| WORKLOAD_IDENTITY | PARTIAL | approved exception (no live drift detection) |
| OUTBOUND_DELIVERY | BLOCKED | approved exception (Meta deferred, test sink only) |
| **BOOKING_WORKFLOW** | **FAIL** | two activation-only checks, below |

Two `BOOKING_WORKFLOW` checks can never hold once booking is on:

1. **"booking switch false on every function"**: by design, booking is ON for the api and agent.
2. **"every execution is an owner-approved Step 6 drill request (SYN-STEP6-*)"**: activation creates real (synthetic)
   approval executions. Three exist from the activation window (07:30–08:36 UTC, 23 Nov).

In addition, `setuhaul/ops/gate_evidence.rows_from_report` refuses any report with `booking_enabled != false`. That's why
v0.3.0 re-recorded the 07:02 UTC report collected before activation. Under the owner's policy this is stale evidence
and is no longer acceptable.

## 2. Activation-only versus renewal checks

| Check | Activation | Renewal |
|---|---|---|
| booking switch false everywhere | required | replaced by: ON on exactly api+agent, OFF everywhere else |
| every execution is a Step 6 drill | required | replaced by: every execution attributed (A drill / B smoke / C owner-confirmed) |
| live alias routes 100% to one version | required | required |
| published definition = repository ASL | required | required |
| no failed execution | required | required |
| drill 2 / redrive / alarm evidence | required | carried (historical facts), not re-run |
| /health status, environment, kill switch | not checked | required |
| owner approval reference | at activation time | required, and must match the activation run |
| fresh smoke bound to release + deployed code | not required | required |
| report bound to the deployed code, fresh (≤ 24 h) | not required | required before any evidence row is written |

## 3. Approval execution records and RLS

`approval_requests` and `appointments` have FORCE RLS with `scope_allows(facility_id)`. The collector read drill ids with
the **database owner** session and a facility scope. My own read-only attempts were blocked:
- the `system` login without a scope saw nothing (RLS);
- an owner-secret read from this session was refused by the session's permission check.

That access is broader than needed. Gap: there is no narrow, auditable read path for evidence. See RLS_EVIDENCE_ACCESS.md.

## 4. The three known executions

Investigated from Step Functions history and the api's CloudWatch logs (no database read was possible). See
APPROVAL_EXECUTION_ATTRIBUTION.md:

* `73cac501`: the documented Option D smoke test (`SYN-SHP-2172ACB8`). Category B.
* `87915a37`, `29ffac03`: the owner's own console Confirm tests (HTTP 500 defect, then the fix). The owner confirmed
  this on 23 Nov 2026. Category C, restricted to exactly these two. The database facts are still **to be verified** by
  the gate through migration 0029.

## 5. Changes required

* Migration **0029** `approval_execution_evidence(uuid[])`: read-only, RLS-respecting, EXECUTE for
  `setuhaul_deploy_evidence` only. No table, policy or role change.
* `setuhaul/ops/renewal.py`: renewal acceptance criteria (pure functions, unit-tested).
* Collector `--mode renewal`. Activation mode is unchanged.
* `gate_evidence`: accepts a booking-ON report only in renewal mode with all bindings. Activation is unchanged.
* `smoke_option_d.py`: release-bound record, new file per run.
* `activate-booking.yml`: `mode` input (default `activation`, unchanged path).

## 6. Open gap (owner decision): the first renewal of a NEW release

A smoke test drives the **deployed** agent with booking ON. A new release's code only starts with booking ON once evidence
for its deployment exists, and that evidence needs a smoke test on that code. Weekly renewal of a running release has no
such loop. For a new release, the renewal path **refuses** (the report's code hash is not the new deployment's): it fails
safe. See RENEWAL_GATE_DESIGN.md §6 for the options. No option is implemented: it's a booking-policy decision.
