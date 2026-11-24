# Option P + M2 release runbook (synthetic Sydney pilot)

Status: **implemented and tested (migration 0032, branch `feature/option-p-m2`). Not merged, not deployed, no
authorization issued.**

Every live step below needs the owner's approval at the time it's taken:
* merging;
* updating the release branch;
* the workflow runs;
* the smoke test;
* the commits of evidence reports.

This runbook upgrades the **running** pilot, v0.3.0 (`1d304f874e4f4443fc565f010050a055528c930b`), in place:
* same Aurora cluster, API, queues, state machine, Cognito, KMS key and alarms;
* the same 10 secrets;
* no new paid resource.

WhatsApp stays off, and the internal test sink stays the only outbound destination. The kill switch and every
approval boundary are unchanged.

## 1. What changes in the pilot

| Item | Change | Effect |
|---|---|---|
| Lambda code (11 functions) | in-place update to the release artifact | one new artifact |
| `setuhaul-pilot-api`, `setuhaul-pilot-agent` execution roles | **replaced** (logical id carries the release); CloudFormation deletes the old roles and their policies at the end of the update | old containers lose access to the login secrets and the Data API (M2) |
| Secrets `setuhaul/pilot/db/agent`, `…/console` | same secrets; regenerated with the username `setuhaul_login_<role>_<12 hex>` | new database identity per release (M2); still 10 secrets |
| Database | migrations 0028–0032 (additive). Pilot guard ON (0030); identity enforcement ON (0032); evidence recording needs the guard (0031) | booking pauses from the migration until the window opens |
| Logins | `provision_logins` creates the two release logins and makes the previous ones (`setuhaul_login_agent`, `setuhaul_login_console`) **NOLOGIN**, ending their sessions | old identity revoked at cutover |
| Booking | v0.3.0 evidence (until 30 Nov 12:31 UTC) is **not** reused for the new code; booking resumes under a ≤ 60 min provisional authorization, then on full evidence | |

## 2. Prerequisites (owner)

1. Merge order into `main`, each after green CI:
   1. **PR #7** (docs only);
   2. **PR #5** (0029);
   3. **PR #6** (0030, 0031);
   4. **this branch** (0032).
   
   The runner applies missing versions in order: 0028 → 0029 → 0030 → 0031 → 0032.
2. Fast-forward `release/fde-sydney-pilot` to the merged `main` (explicit owner authorization).
3. A new written approval for this release, for example `OWNER-APPROVAL-20261124-OPTION-P`. It is single-use: the
   database refuses a second window under it.
4. An AWS session for the operator (smoke test and gate collection). The owner signs in.

## 3. Sequence (target ≤ 60 minutes from step 3.2 to step 3.5)

**3.1 Five-gate report from the running release** (read-only collection, before the deploy):
```
python scripts/step6_gate_evidence.py --mode renewal --approval-ref OWNER-APPROVAL-20261124-OPTION-P \
  --release-sha 1d304f874e4f4443fc565f010050a055528c930b --out docs/release/evidence/provisional_gates.json
```
* Commit the report to the release branch. It's a docs-only commit, so the Lambda artifact is unchanged.
* It must be ≤ 24 h old at step 3.2.
* BOOKING_WORKFLOW in this report is ignored; the authorization stands in for it.
* AWS_QUEUE and RECOVERY_WORKER must be PASS. AURORA, WORKLOAD_IDENTITY and OUTBOUND_DELIVERY must meet their
  exception conditions.

**3.2 Deploy and open the window** (GitHub Actions):
```
gh workflow run activate-booking.yml --ref release/fde-sydney-pilot \
  -f mode=provisional -f approval_ref=OWNER-APPROVAL-20261124-OPTION-P -f confirm=activate-synthetic-pilot-booking
```
In order:
1. tests and synth;
2. deploy (roles and secrets rotate);
3. migrations 0028–0032;
4. login provisioning, which **stops unless the two secrets name this release** (`--expect-release`);
5. manifest;
6. **one transaction:** the manifest, the five gates and `issue_provisional_authorization` (60 min);
7. `/health` booking ON (`booking_provisional_until` shown).

On any failure after issuance, the workflow **revokes** the authorization.

**3.3 Release-bound smoke** (operator, inside the window):
```
python scripts/smoke_option_d.py --release-sha <the deployed release commit>
```
It declares the newest deployment and books through the release's own logins.

**3.4 Renewal report on the new code** (operator, inside the window):
```
python scripts/step6_gate_evidence.py --mode renewal --approval-ref OWNER-APPROVAL-20261124-OPTION-P \
  --release-sha <the deployed release commit> --smoke docs/release/evidence/smoke/<file from 3.3> \
  --out docs/release/evidence/renewal_gates.json
```
Commit it to the release branch (docs only, so the artifact and deployment id are unchanged).

**3.5 Close the window** (GitHub Actions, inside the window):
```
gh workflow run activate-booking.yml --ref release/fde-sydney-pilot \
  -f mode=renewal -f approval_ref=OWNER-APPROVAL-20261124-OPTION-P -f confirm=activate-synthetic-pilot-booking
```
* The deploy is a no-op (same artifact and template).
* It records the manifest and **all six gates**.
* The BOOKING_WORKFLOW row **consumes** the authorization in the same transaction.
* The database refuses it after expiry, and so does `gate_evidence` if the smoke wasn't inside the window.

**3.6 Sleep the workers again:** `python -m setuhaul.ops.pilot_mode sleep`, then `status`.

## 4. Verification (read-only)

```sql
SELECT enforced, identity_enforced FROM setuhaul.booking_evidence_guard;            -- true, true
SELECT version FROM setuhaul_meta.schema_migrations ORDER BY version DESC LIMIT 5;   -- 0032 … 0028
SELECT status, issued_at, expires_at, closed_at FROM setuhaul.provisional_authorizations ORDER BY 1 DESC LIMIT 1;  -- CONSUMED
SELECT action, principal, recorded_at FROM setuhaul.provisional_authorization_events ORDER BY event_id DESC LIMIT 3;
SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname ~ '^setuhaul_login_(agent|console)';  -- only the release logins can log in
SELECT setuhaul.booking_evidence_verdict('pilot-ap-southeast-2', '<deployment>', '<code>', clock_timestamp(),
                                         'setuhaul_login_console_<12 hex>');        -- NULL
```

Also check:
* `/health`: `booking_enabled true`, kill switch `disengaged`, three exceptions, and no `booking_provisional_until`
  after close.
* All 14 alarms OK.
* The DLQ is empty.
* Notifications went to the test sink only.

## 5. Stop conditions and recovery

| Situation | What happens | Recovery |
|---|---|---|
| 3.1 report not PASS/exception for the five gates | nothing deployed | fix and re-collect |
| Deploy fails and CloudFormation rolls back | the rollback regenerates the two login secrets (previous usernames); the workflow's failure step re-provisions the logins from them, so v0.3.0 keeps working (no migration ran, so it keeps booking on its own evidence until 30 Nov 12:31 UTC) | check `/health`; fix and retry |
| Migration fails after the deploy | booking paused (0030 may be applied: old containers undeclared; new ones can't start booking) | fix forward, or §6 |
| `--expect-release` refuses | stop before evidence; booking paused | investigate the secret; §6 |
| Issuance refused (predecessor, gates, rate limit, approval reuse) | nothing recorded (one transaction) | correct the cause; a new approval if one was used |
| Smoke fails, or the window passes without close | revoked by the pipeline, or expired by the clock: **every commitment refused**; new API/agent cold starts fail the start-up check | §6; a new release and **new approval** for another window (max 2 per 24 h) |
| Anything wrong at any time | ops lead: **kill switch** (console) or **revoke** (`POST /console/provisional-authorization/revoke`) | then §6 |

## 6. Rollback

* **Fastest (booking OFF, everything else working):** `ci.yml` workflow_dispatch `deploy=pilot` on the release
  branch. It carries no booking approval, so the functions start with booking off. Roles and logins stay per release.
* **Code rollback to v0.3.0:**
  * possible, but it can't book: its code doesn't declare a deployment, and 0030 refuses undeclared commitments;
  * its template reverts the two secret usernames, and provisioning re-enables `setuhaul_login_agent/console` and
    switches the release logins off;
  * so it's booking OFF in effect, and needs the release branch reset (owner authorization).
* **Database:** migrations are forward-only and additive. The guard and identity enforcement are one-way by design.
  Backing them out is a new migration (owner decision). No existing data is changed by 0028–0032.
