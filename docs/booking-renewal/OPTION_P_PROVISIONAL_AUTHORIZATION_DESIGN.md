# Option P: single-use provisional booking authorization (design only)

Status (24 Nov 2026, later): **implemented in migration 0032 on `feature/option-p-m2`; not merged, not deployed, no
authorization issued.** Owner-approved parameters: 60 min default, 2 h hard maximum, 2 per environment per 24 h, one
open per environment, one per deployment and per approval, ops lead may revoke, M2 enforced before first use.
Implementation differences from this design: the lifecycle is ISSUED -> CONSUMED | REVOKED | EXPIRED (booking is
allowed from issuance; there is no separate activation step), and consumption is a trigger on the release's
BOOKING_WORKFLOW evidence insert, so it is atomic with the evidence on every path. Runbook:
OPTION_P_M2_RELEASE_RUNBOOK.md.

## 1. Problem

A new release N+1 may book only with evidence for its own deployment (0030). The BOOKING_WORKFLOW gate's evidence comes
from a release-bound smoke test that drives the *deployed* agent with booking ON (PR #5). Booking ON needs evidence, and
the evidence needs booking ON. P resolves the cycle with one short, single-use, pipeline-issued window in which N+1 may
book **only so that the smoke can run**, after which full evidence must exist or the database refuses every commitment.

## 2. Scope of the provisional authority

| Gate | During the window | Why |
|---|---|---|
| AWS_QUEUE, AURORA, RECOVERY_WORKER, WORKLOAD_IDENTITY, OUTBOUND_DELIVERY | **normal evidence or owner-approved exception for N+1**, recorded before the window opens | collectible with booking OFF; their fingerprints are N+1's |
| BOOKING_WORKFLOW | **provisional authorization** | the only gate that needs booking ON for N+1 |

The provisional authorization stands in for **BOOKING_WORKFLOW only**. It never stands in for another gate, never
widens an owner-approved exception, and never applies outside a `pilot` environment.

## 3. Data model (proposed migration 0032)

`provisional_authorizations` (append-only except the status transitions below, made only by definer functions):

| Column | Meaning |
|---|---|
| `authorization_id` | identity |
| `environment` | `booking_environment_class(environment) = 'pilot'` (CHECK); never production, never invalid |
| `deployment_id`, `code_sha256` | N+1's manifest id and code; must be the **newest** manifest at issuance |
| `release_sha` | the Git commit the smoke must be bound to (40 hex) |
| `predecessor_deployment` | the newest deployment with **full, non-provisional** current evidence at issuance |
| `approval_ref` | `OWNER-APPROVAL-YYYYMMDD-P-…`, **UNIQUE** (an approval is consumed once) |
| `pipeline_run` | GitHub run id + attempt (audit) |
| `issued_at`, `expires_at` | database clock; CHECK `expires_at <= issued_at + interval '2 hours'` |
| `status` | `ISSUED → ACTIVE → CLOSED` or `→ REVOKED`; `EXPIRED` is derived from the clock, never needed for enforcement |
| `activated_at`, `closed_at`, `closed_by_evidence_id`, `revoked_by`, `revoke_reason` | transitions |

Constraints: `UNIQUE (environment, deployment_id)` (single-use per deployment: no second window for the same code), a
partial unique index allowing **at most one ISSUED/ACTIVE row per environment**, and a per-environment rate limit
(at most 2 issued per 24 h) enforced in the issuing function.

`provisional_authorization_events`: append-only audit (who, what, when, run id, correlation id, before and after
status), written by the same functions in the same transaction.

## 4. Lifecycle and atomicity

All functions are `SECURITY DEFINER` owned by `setuhaul_rpc` (NOBYPASSRLS, no login), `search_path` pinned, `EXECUTE`
granted only to `setuhaul_deploy_evidence` (issue, activate, close) and the console's ops-lead/owner path (revoke).
No role gets table DML.

1. **Issue** (`issue_provisional_authorization`): pipeline only, in the evidence step's transaction. It takes 0030's
   exclusive manifest lock. It refuses unless every check passes:
   * guard enforced (M1);
   * `pilot` class;
   * N+1 is the newest manifest;
   * the predecessor has full current evidence and was **not** itself provisional (no chaining);
   * the five other gates have current evidence or exceptions for N+1;
   * no ISSUED/ACTIVE row exists for the environment;
   * the approval ref is unused;
   * the rate limit holds.
   
   It fixes `expires_at = clock_timestamp() + window`, with `window ≤ 2 h`, chosen by the pipeline.
2. **Activate** (atomic consumption): `UPDATE … SET status='ACTIVE', activated_at=clock_timestamp() WHERE
   authorization_id=$1 AND status='ISSUED' AND clock_timestamp() < expires_at RETURNING …`. Exactly one caller can win
   (row lock + status predicate). A second call, a replay or a late call returns nothing and is refused. Activation is
   the moment booking may start. The 2 h limit counts from **issuance**, so a delayed activation shortens the window
   and never extends it.
3. **Smoke** (mandatory, inside the window): `smoke_option_d.py --release-sha <release_sha>` against the deployed N+1
   (PR #5). Its executions are attributed B (smoke).
4. **Close**: the renewal collection (PR #5, renewal mode) records full BOOKING_WORKFLOW evidence for N+1, bound to
   the release SHA and code hash and naming this run's approval. In the **same transaction** the authorization moves
   `ACTIVE → CLOSED` with `closed_by_evidence_id`. Closing requires `clock_timestamp() < expires_at`: evidence produced
   after the window does not retroactively legitimize it, and the pipeline must stop.
5. **Revoke**: the pipeline on any failed step, or an ops lead/owner from the console, moves `ISSUED/ACTIVE → REVOKED`
   immediately. Revocation is one-way.

## 5. Enforcement in the database (extends 0030's verdict)

`booking_evidence_verdict` treats BOOKING_WORKFLOW as satisfied for (env, dep, code) only when one of these holds:
* current normal evidence exists (unchanged), or
* exactly one row has `status = 'ACTIVE'`, the same env/dep/code, a `pilot` class, `activated_at <= clock_timestamp() <
  expires_at`, and the deployment is still the newest manifest.

Everything else in the verdict is unchanged: the kill switch still fires first (`aa_…`), the other five gates still need
their evidence or exceptions, and production and invalid environments are still refused before any lookup. **Expiry
needs no job.** The clock comparison happens on every commitment, so at `expires_at` the next commitment is refused
even if every process is wedged. A sweeper may later write `EXPIRED` for audit readability only.

The start-up check (`check_booking_preconditions`) gets the same rule through `booking_gate_snapshot`, so a container
starting inside the window may switch booking on, and one starting after it may not.

## 6. Failure and recovery

| Failure | Outcome |
|---|---|
| Smoke fails, or the pipeline crashes or hangs | no close → refusals from `expires_at` (≤ 2 h); the pipeline's failure step also revokes at once |
| Renewal evidence lands after `expires_at` | close refused; booking stays refused; N+1 cannot get a second window (UNIQUE env+deployment) |
| A newer manifest appears mid-window | N+1 is no longer newest → refused at once (0030 rule) |
| Guard switched off | impossible (one-way); with the guard off nothing can be issued (M1 refuses the manifest) |
| Database error during verdict | refused (0030's fail-closed exception handling) |

**Recovery path** after a failed window: (a) fix and ship N+2 with its own manifest and a **new owner approval** (the
predecessor rule still requires a fully evidenced earlier deployment, so windows cannot be chained); or (b) fall back
to **S** (staging smoke) or the activation-mode path. A rollback to N's code is a new manifest (0030), so it also needs
fresh evidence; it cannot reuse N's old evidence.

## 7. Audit trail

Every transition writes `provisional_authorization_events` in the same transaction: principal (`session_user`), run id,
release SHA, deployment, correlation id, and the before and after status. Refused issuance and activation attempts
raise. The pipeline log records them, and a `booking.refused` telemetry event (L5) is proposed. The weekly evidence
report lists every window with issued, activated and closed/revoked/expired times and the smoke's execution ids.

## 8. Threats considered

| Threat | Control |
|---|---|
| Replay or reuse of an authorization or approval | UNIQUE approval_ref; UNIQUE env+deployment; status predicate on activation |
| Rolling windows keeping unsmoked code live | no-chaining predecessor rule; one open window per environment; 2 per 24 h |
| Window extension | `expires_at` fixed at issuance, CHECK ≤ 2 h, no UPDATE path for it |
| Production use | CHECK on environment class + verdict class check + no workflow input for production |
| Non-pipeline issuer | EXECUTE only for `setuhaul_deploy_evidence`; no table DML for anyone |
| Stale container books in the window | must declare the newest deployment and its code (0030); see M2 for self-declaration |
| Clock skew | database clock only (`clock_timestamp()`), as 0030 |

## 9. Implementation plan (when authorized)

1. Migration 0032: tables, functions, verdict and snapshot extension, grants, and pins in `test_agent_not_operator`.
2. `gate_evidence`: `--provisional` issue/activate/close, refusing everything §4 refuses (defence in depth).
3. `activate-booking.yml`: `mode=provisional` (pilot only, approval input required, failure step revokes).
4. Start-up check: accept an ACTIVE authorization for BOOKING_WORKFLOW only.
5. Runbook: the window procedure, stop conditions and the recovery path.

**Tests required before review** (PostgreSQL 17.9 CI and the Data API emulator):
* issue, activate and close happy path;
* double activation from two sessions (exactly one wins);
* activation after expiry;
* close after expiry;
* a commitment one microsecond before and after `expires_at`;
* production and invalid environments;
* chained windows;
* a second window for the same deployment;
* a reused approval;
* a newer manifest mid-window;
* revoke, then commit;
* a kill switch engaged inside the window;
* the other five gates missing;
* a rolled-back issuance leaving nothing behind;
* an unauthorized role calling each function;
* a guard-off database (M1).

## 10. Not decided here

The window length within ≤ 2 h (proposed default 60 min), the per-day limit, and whether the console may revoke (proposed: yes, ops lead and owner) are owner decisions.
