# Runbook: rolling out the database booking evidence guard (migration 0030)

Owner-run. **Nothing here has been executed.** This is the procedure for when the owner approves a release containing
PR #6. Every command shown is read-only unless it says otherwise. Account ids are masked.

> **Do not apply migration 0030 on its own to the running v0.3.0 pilot.** The pilot's database already hosts recorded
> deployments, so 0030 turns the guard ON when it is applied. v0.3.0 code does not declare its deployment, so from that
> moment **every booking commitment is refused** until code that declares (PR #6) is running with fresh evidence for its
> own deployment. Apply 0030 only through the release pipeline, together with that code.

## 1. Current state (v0.3.0, 23–24 Nov 2026)

| Item | State |
|---|---|
| Release | v0.3.0 (`1d304f8`) deployed; the release branch is at `e280a40` (G.1, not deployed) |
| Booking | ON for `setuhaul-pilot-api` and `setuhaul-pilot-agent` only; kill switch disengaged |
| Evidence | checked at container start only. A warm container is not re-checked (the gap PR #6 closes) |
| Migrations | 0001–0028 on `main`; 0029 (PR #5) and 0030 (PR #6) not merged |
| Outbound | internal test sink; WhatsApp/Meta off. LangSmith off. Production disabled |

## 2. What 0030 does when applied

* Creates `booking_evidence_guard`. It is **ON immediately** if `deployment_manifests` already has a row (the pilot
  does: every `activate-booking` run recorded one), and OFF on a database with no recorded deployment.
* Adds the guard triggers. From then on, every new booking commitment needs a declared deployment that is the newest
  manifest, the same code, and current evidence or exceptions recorded after that manifest.
* Cannot be turned off by data (one-way). Backing it out needs a new migration: an owner decision.

## 3. Prerequisites (all required before starting)

1. PR #6 reviewed and merged into the release, together with the application code it contains (`db.py`, `data_api.py`,
   `app.py`). Never merged migration-only.
2. **A fresh-evidence path for the new release.** The owner's policy forbids re-recording an older report. As of
   24 Nov 2026 this path doesn't exist for a *new* release: PR #5's renewal mode covers the running release, and how a
   new release gets its first smoke (P/O/S) is an open owner decision. **Until that is decided and built, do not run
   this rollout.**
3. Owner approval reference for the release; typed confirmation `activate-synthetic-pilot-booking`.
4. The owner is present (the smoke test engages the kill switch briefly).

## 4. Deployment order (inside `activate-booking`, unchanged pipeline)

The workflow's existing order already does the right thing:

| # | Workflow step | Booking during this step |
|---|---|---|
| 1 | test (full suite on PostgreSQL 17.9), synth (booking ON for api and agent, test sink, no WhatsApp) | unchanged (old code serving) |
| 2 | CDK deploy: the new code goes to every function | new containers can't start booking (no manifest for their code yet); warm v0.3.0 containers keep serving |
| 3 | **Migrations: 0030 applied, guard ON** | **paused**: warm v0.3.0 containers are undeclared, so every commitment is refused |
| 4 | Logins, manifest built | paused |
| 5 | Evidence step: the new manifest, then its **fresh** evidence and exceptions, in one transaction | paused until this transaction commits |
| 6 | New containers start (start-up check passes) and declare the deployment; `/health` shows booking ON | **resumes** |

Expected pause: from step 3 to step 6, normally minutes. Reductions (cancel, expire, release) and reads keep working
throughout. During the pause:
* **driver chat:** `hold_option` / `submit_booking_request` return `ERROR` with code `BOOKING_EVIDENCE_…`, and the
  agent escalates to a person and tells the driver a coordinator will help. That's the same path as a kill-switch
  refusal (`orchestrator._book` → `_human`).
* **console:** a planner's Confirm or move is refused with HTTP 400 and the code `BOOKING_EVIDENCE_…`. Nothing is
  written.

## 5. Verification after the run (read-only)

**5.1 Guard state** (owner session, READ ONLY transaction):
```sql
SELECT enforced, changed_at, reason FROM setuhaul.booking_evidence_guard;
-- expect: true | <migration time> | migration 0030: this database hosts a recorded deployment
```

**5.2 Both functions declare the right deployment.** Each container logs one line at start:
```
booking evidence guard: this process declares environment=pilot-ap-southeast-2 deployment=m-<40 hex> code=<12 hex>
```
Check that both the **api** and **agent** log groups have it after the deploy, with the same `deployment` and `code`,
and that they equal the newest manifest:
```sql
SELECT deployment_id, artifact_sha256, recorded_at FROM setuhaul.deployment_manifests
 WHERE environment = 'pilot-ap-southeast-2' ORDER BY manifest_id DESC LIMIT 1;
```
A function with no such line hasn't started on the new code (or started with booking off). One with an older
`deployment` is a stale container, and the guard refuses its commitments.

**5.3 The running deployment is authorized** (owner session, READ ONLY; returns NULL when authorized):
```sql
SELECT setuhaul.booking_evidence_verdict('pilot-ap-southeast-2', '<deployment from 5.2>', '<full code hash>',
                                         clock_timestamp());
```

**5.4 Invalid evidence is refused** (same function, no booking is created):
```sql
SELECT setuhaul.booking_evidence_verdict('pilot-ap-southeast-2', '<a previous deployment id>', '<its code>', clock_timestamp());
-- expect: DEPLOYMENT_SUPERSEDED: ...
SELECT setuhaul.booking_evidence_verdict('pilot-ap-southeast-2', '<current deployment>', repeat('0', 64), clock_timestamp());
-- expect: ARTIFACT_MISMATCH: ...
SELECT setuhaul.booking_evidence_verdict('PROD', '<current deployment>', '<code>', clock_timestamp());
-- expect: PRODUCTION_DISABLED: ...
```

**5.5 End to end:** the release's fresh smoke test (`scripts/smoke_option_d.py --release-sha <sha>`) books one
synthetic appointment through the deployed agent and approval workflow. It succeeds only if the guard authorized it.

## 6. Recovery if the deployment fails partway

| Failed at | State | Recovery |
|---|---|---|
| 1 (tests/synth) | nothing changed | fix and rerun |
| 2 (CDK deploy) | CloudFormation rolls back; 0030 not applied; v0.3.0 keeps serving | fix and rerun |
| 3 (migration) | if 0030 committed: guard ON, booking paused (fail closed). If not: nothing changed | rerun the workflow; migrations are idempotent per version |
| 4–5 (logins, evidence) | guard ON, new code deployed, no current evidence for it: **booking paused** | rerun the workflow (it records a new manifest and its fresh evidence). Nothing to undo. Booking stays paused until a run completes |
| 6 (start-up) | evidence recorded but a function won't start | read its start-up error (fingerprint, queue, manifest). Booking is refused for anything not declaring the newest deployment |

In every case the failure mode is **booking refused, never booking without current evidence**. The kill switch stays
available throughout (ops_lead console or the owner CLI).

## 7. Rollback

* **Rolling back the code** (re-deploying an earlier release) records that release's manifest again, as the newest.
  Its earlier evidence predates that record and **does not count**. The rollback needs fresh evidence of its own,
  recorded by the same pipeline run. Until then, booking stays paused.
* **Rolling back to v0.3.0 specifically** is not possible with the guard on: v0.3.0 doesn't declare its deployment, so
  every commitment is refused. A rollback target must contain PR #6's code.
* **Rolling back 0030** isn't supported by data (the guard row is one-way). If the owner decides to remove the guard,
  that's a new migration dropping the two `ab_booking_evidence_guard` triggers, reviewed like any other. Existing
  bookings are unaffected either way: the guard only judges new commitments.

## 8. What this rollout does not change

The kill switch, owner approvals, the three pilot exceptions and their scope, facility isolation, production (refused
in any letter case), WhatsApp/Meta (off), LangSmith (off), and the booking switch itself (still set only by
`activate-booking`).
