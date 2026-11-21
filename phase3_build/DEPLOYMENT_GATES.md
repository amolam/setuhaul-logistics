# Deployment gates for the booking switch

Reviews R1.9 (S4-05), R2.0 (S4-R2-04, -05, -06) and R2.1 (S4-R3-01, -02). This is a **deployment acceptance
contract**: it says what must be proven before the switch goes on. Passing tests here do not show that any of it is
deployed yet; step 6 does that.

With `SETUHAUL_BOOKING_ENABLED=true` the service refuses to start unless:

1. the inbound queue is SQS FIFO, its visibility timeout is at least twice the processing lease, and its redrive policy
   points at a readable **FIFO** dead-letter queue, with `maxReceiveCount` 1–10, that keeps messages at least as long as the queue does;
2. `SETUHAUL_ENVIRONMENT` and `SETUHAUL_DEPLOYMENT_MANIFEST` are set. The manifest is complete, and it was built for this environment;
3. both evidence triggers (append-only, freshness) exist and are enabled; and
4. each of the six gates has a **current** row in `deployment_evidence` for this environment and deployment:
   - the newest row for the gate is within its validity window (below);
   - its `config_fingerprint` equals the fingerprint the service computes at start-up.

## The deployment manifest and the deployment id (S4-R3-02)

The pipeline builds a JSON manifest with every deployment and ships it with the artifact. It names an immutable
version for everything the service cannot inspect itself:

| Gate | Manifest section must name |
|---|---|
| RECOVERY_WORKER | worker `artifact_sha256`, `schedule`, `config_version`, `queue_visibility_timeout_s` |
| BOOKING_WORKFLOW | `state_machine_arn`, published `state_machine_version`, `definition_sha256` |
| WORKLOAD_IDENTITY | `iam_policies_sha256` (all attached policies and trust policies), `role_mapping` (workload → database role) |
| OUTBOUND_DELIVERY | sender `artifact_sha256`, `provider_config_sha256`, `delivery_policy_version` |

The **deployment id is derived from the manifest** (`m-` followed by 40 hex characters of its SHA-256). Nobody types
it. A new worker build, state-machine version, IAM policy or sender configuration is a new manifest, so it gets a new
deployment id, and evidence recorded for the old one no longer counts. `SETUHAUL_DEPLOYMENT_ID` is optional. If it is
set, it must equal the derived id.

Fingerprints:

| Gate | Fingerprint |
|---|---|
| AWS_QUEUE | Computed live from the queue and its dead-letter queue (ARN, FIFO, visibility, redrive, retention) |
| AURORA | Computed live from the database (name, server version, every applied migration and its hash) |
| The other four | Computed from that gate's manifest section |

```bash
python -m setuhaul.ops.fingerprints
```

This prints the deployment id and all six fingerprints exactly as the service computes them. The pipeline records
these values.

**What stays outside the service.** The manifest says what *should* be deployed. It cannot show that someone has not
changed the state machine or an IAM policy in the console afterwards. In step 6, drift detection (AWS Config rules, or a
pipeline check that compares live resource versions with the manifest) is part of the WORKLOAD_IDENTITY and
BOOKING_WORKFLOW gates.

## Validity windows (S4-R3-01, S4-R3-02)

| Evidence | Gates | Valid for at most |
|---|---|---|
| Configuration | AWS_QUEUE, AURORA, BOOKING_WORKFLOW, WORKLOAD_IDENTITY | 30 days, and void at once when the fingerprint changes |
| Operational (exercised against the real queue or provider) | RECOVERY_WORKER, OUTBOUND_DELIVERY | 7 days |

An insert trigger checks each row against the **database clock**:

- `verified_at` may be at most 5 minutes ahead (clock skew);
- `expires_at` may be at most one window from now, and at most one window after `verified_at`.

Evidence cannot be dated into the future to stretch its life. The start-up check applies the same rules when it reads
the rows.

## Who may write evidence

Only `setuhaul_deploy_evidence` can write evidence. It can `INSERT` and `SELECT` here and nothing else.

Ordinary `UPDATE`, `DELETE` and `TRUNCATE` are refused for every role, the table owner included. A re-verification adds
a new row (`supersedes` points at the old one). `verifier` is the database session user, not a value the writer
supplies.

**What the triggers do not stop:** a table owner or superuser can disable or drop them. They protect against ordinary
DML, not against a hostile database administrator. Two measures cover that:

- **At start-up:** the service refuses to start if either trigger is missing or disabled.
- **In step 6:** database activity is audited (pgaudit or Database Activity Streams), and the owner role is not used by
  any workload.

In step 6 the pipeline's short-lived IAM identity is mapped to `setuhaul_deploy_evidence`. Mapping it, and showing that
no other identity can, is part of the WORKLOAD_IDENTITY gate.

Each row carries:

- the environment, deployment id and gate;
- the verification and expiry times, and the verifier;
- the artifact URI (`s3://` or `https://`) and its SHA-256;
- the configuration fingerprint.

The service cannot open the artifact. Checking that the artifact really shows a pass is the pipeline's job, done before
it writes the row.

## The six gates

| Gate | Must cover | Monitoring verified as part of the gate |
|---|---|---|
| **AWS_QUEUE** | FIFO, content-independent deduplication id, DLQ (FIFO, redrive count, retention) and a tested redrive procedure, visibility ≥ 2 × lease. Send and receive permissions for exactly the relay and worker roles, and no wider SQS actions (least privilege checked live) | Age of oldest message; DLQ depth |
| **AURORA** | Aurora PostgreSQL 18.3+ (T33). Migrations 0001…head applied, with their hashes. RLS and role allowlist tests run against it. RDS Data API connectivity behind `Db.tx()`. Both evidence triggers enabled | Connection errors; failed transactions |
| **RECOVERY_WORKER** | `InboundRelay.recover()` scheduled every minute. Lease renewal and generation fencing exercised against the real queue. Failure retries. DLQ handling procedure | Recovery failures; expired processing leases; webhook 5xx rate (alert on a sustained rate, not just provider retries) |
| **BOOKING_WORKFLOW** | Step Functions approvals (B-WF-2): request, approve, reject, expiry, no auto-approval. Booking idempotency. State transitions. The published version in the manifest is the one running | Failed or expired booking confirmations |
| **WORKLOAD_IDENTITY** | Lambda / AgentCore roles, API Gateway IAM, Cognito. Each workload is mapped to exactly one database role (agent, ingest, feasibility, console, system). The pipeline identity is mapped to `setuhaul_deploy_evidence` and nothing else. Drift detection compares live policies with the manifest | Authorization failures |
| **OUTBOUND_DELIVERY** | Driver and coordinator notifications (B-WF-3): retries, and duplicate prevention at the provider. The worker sends the **stored** reply row (`chat_messages`, one per inbound message and generation), never a re-generated reply. Built and tested locally in B-WF-3 (migration 0017); this gate proves it against the real provider, with approved templates | Undelivered driver notifications |

## Workload identity acceptance (review R3, Q1)

The WORKLOAD_IDENTITY gate's evidence must show all five:

1. The console workload's login maps only to `setuhaul_console`.
2. The agent, ingest, feasibility and system logins cannot assume the console role.
3. No workload can assume `setuhaul_rpc`, use the database owner, or use a superuser login.
4. A real Data API transaction keeps the staff context (`begin_staff_context`) across the statements that need it.
5. A Cognito user in an ordinary operations group cannot create an ops_lead context.

Locally, `Db.tx()` uses one DSN and switches roles with `SET LOCAL ROLE`. That is a test convenience. Production must
not use one shared credential that can reach every application role: the separation comes from the AWS identity and
credential arrangement.

**Recommended evidence order (reviewer, R3 Q4):**
1. B-AWS-0 and workload identity;
2. AURORA;
3. AWS_QUEUE;
4. RECOVERY_WORKER;
5. BOOKING_WORKFLOW;
6. OUTBOUND_DELIVERY.

All six must be current against one manifest, with live drift detection working.
