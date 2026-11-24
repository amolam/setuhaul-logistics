# Recovery Release A: design (migration 0033)

**Status:** implemented on `fix/recovery-release-a`. Not merged, not deployed, no authorization issued.

## 1. Why

On 24 Nov 2026 the Option P release `a17e74df1203` (deployment `m-36e59dde…`) went live and passed its smoke test.
Its renewal report then failed two gates because of code defects, and its authorization (ID 1) was **revoked** at
16:06:47 UTC. The database refuses every booking commitment, as designed.

Two problems block the way back:
* **D1–D4** (below) are defects that a new release must fix.
* **No chaining.** `issue_provisional_authorization` requires the previous release to have complete normal evidence.
  The revoked release can never get BOOKING_WORKFLOW evidence, because the consumption trigger refuses it after
  revocation. So Option P refuses every next release, and the pilot can't get back to booking.

## 2. The four defects

| ID | Defect | Fix |
|---|---|---|
| D1 | The AURORA collector expected **exactly two** triggers on `deployment_evidence`; 0031 and 0032 add two more | `setuhaul/ops/evidence_triggers.py`: the complete expected set on **all three** evidence tables and both commitment tables, with each trigger's function, timing, events and level. Missing, disabled, unexpected or misconfigured triggers are each reported. The collector reads the full `pg_trigger` rows |
| D2 | 0029 classified every `staff:%` decider as `drill_staff`. The console records each signed-in person as `staff:<Cognito sub>`, so the owner's real decision (29ffac03) contradicted its owner-confirmed record | 0033 replaces `approval_execution_evidence`: `staff:<UUID>` (a Cognito `sub`) is `console_user`, and any other `staff:<name>` is `drill_staff`. A new append-only `approval_decider_attestations` table records owner-attested corrections with their provenance. 0033 records the one for 29ffac03, only if that approval exists and was decided by a `staff:<UUID>` person. **`approval_requests` is not modified** |
| D3 | `gate_evidence --revoke-provisional` failed at argument parsing without `--artifact-uri`, so the workflow's automatic revoke would have failed | `--artifact-uri` is required only with `--activate` or `--provisional`. The workflow revokes on `failure() \|\| cancelled()`, then a final `always()` guard fails the job loudly if a failed or cancelled provisional run left an authorization open |
| D4 | `/health` reported the start-up snapshot (`booking_enabled`, `booking_provisional_until`) after revocation. A cold container refused to start, which took the **whole API** (console included) down with HTTP 502 | `/health` now reads the **database's** verdict on every call (`booking_authorization_status`, 0033). `booking_enabled` is true only when the switch is on, the container passed its gates, the database authorizes this deployment, code and identity now, and the kill switch is off. If any read fails, the state is `unknown` and booking is reported as not enabled. A cold container whose gates fail now starts **booking-OFF** (an ALERT log line, and `booking_state: refused` with the reason) instead of crashing. Warm and cold containers report the same database truth |

## 3. The recovery exception (0033)

The default stays **no chaining**. 0033 adds one narrow, owner-approved exception.

### 3.1 What qualifies: all of these must hold, checked by the database at issuance under the advisory lock

1. The caller names the revoked authorization explicitly: `p_recovery_of` = its `authorization_id`. The workflow input
   is `recovery_of`, and it never defaults.
2. That authorization belongs to **this environment** and to **the previous release**, the newest other deployment
   manifest.
3. Its status is **REVOKED**.
4. It was itself issued **normally** (`recovery_of IS NULL`). **A recovery can't recover a recovery**, so at most one
   hop is possible.
5. It hasn't already been used for a recovery (a UNIQUE index on `recovery_of`).
6. It was revoked within the last **7 days**.
7. Its audit trail is intact: exactly one `ISSUED` and one `REVOKED` event.
8. The previous release's **only** gap is BOOKING_WORKFLOW: its other five gates are still current. That keeps the
   recovery recent, with the infrastructure verified.

Everything else in `issue_provisional_authorization` still applies unchanged:
* the guard and M2 must be on;
* pilot class only, never production;
* 1–120 minutes, 60 by default;
* the release must be the newest;
* its five other gates must pass under this approval;
* one open authorization per environment;
* at most 2 issued per environment in any 24 h (**the revoked one counts**);
* one authorization per deployment;
* a **fresh, single-use** approval reference.

The new authorization is ordinary in every other way:
* BOOKING_WORKFLOW only;
* atomic consumption by the new release's own smoke-backed evidence;
* no renewal or extension;
* ops-lead and pipeline revoke;
* the append-only audit (the `ISSUED` event records `recovery_of`).

### 3.2 What doesn't qualify

| Previous-release state | Why not |
|---|---|
| No `recovery_of` given | The default rule applies: no chaining |
| Complete normal evidence | Not needed: a normal issuance. Passing `recovery_of` is refused (`RECOVERY_NOT_APPLICABLE`) |
| Authorization `EXPIRED` | The window lapsed without the pipeline or an ops lead acting. That's not a recorded, deliberate stop |
| Authorization `ISSUED` (open) | Already refused: one open per environment |
| Authorization `CONSUMED` | The release has full evidence: the normal path |
| A revoked authorization that was **itself a recovery** | No second hop. A full activation is needed |
| A revoked authorization already used for a recovery | Single use (UNIQUE) |
| Revoked more than 7 days ago | Stale: a full activation is needed |
| The previous release lacks gates other than BOOKING_WORKFLOW | The infrastructure evidence lapsed: a full activation is needed |
| No authorization at all | The previous release was never on Option P: a full activation is needed |
| A different environment's or an older release's authorization | Refused (`RECOVERY_MISMATCH`) |

### 3.3 Why this is safe

* **It can't chain indefinitely:** one hop, once per revoked window, within 7 days, and still under the 2-per-24 h
  limit.
* **It can't fabricate:** the recovered release still needs its **own** five gates, smoke and six-gate renewal. The
  revoked release's records are never changed.
* **The owner stays in the loop:** a new single-use approval reference and an explicit `recovery_of` input on a manual
  workflow dispatch on the release branch.

## 4. Live pilot sequence for Recovery Release A (owner approval required)

See `OPTION_P_M2_RELEASE_RUNBOOK.md` §7. It's the same as §3 of that runbook, with `-f recovery_of=1` on the provisional
run and a new approval reference.
