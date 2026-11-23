# Phase F: AWS step 6 acceptance, gate by gate

> **Current status (22 Nov 2026, 18:47 UTC): measured live on the deployed synthetic pilot.**
> Full evidence: `13_STEP6_GATE_EVIDENCE.md` and `evidence/step6_gates.json`.
>
> | Gate | Status |
> |---|---|
> | AWS_QUEUE | **PASS** (23 Nov): redrive drilled live; the DLQ-depth alarm fired on a held message and returned to OK. |
> | RECOVERY_WORKER | **PASS** (23 Nov): DLQ procedure drilled; forced lease loss recovered live by the deployed worker (fenced, one reply). |
> | WORKLOAD_IDENTITY | **PARTIAL.** Checks 1 to 4 live; check 5: second user created and trigger proven, the owner's live sign-in pending; no drift detection on the Free plan. |
> | AURORA | **PASS WITH PILOT EXCEPTION (T33-P)**, 23 Nov: 17.9 verified live and in CI. Production still needs 18.3+ (T33). |
> | BOOKING_WORKFLOW | **PASS** (23 Nov): drill 2 live, 10 of 10 (request, approve, reject, duplicate, refusals, audit, timeout expiry); the failed runs were redriven safely; the alarm was proven. |
> | OUTBOUND_DELIVERY | **BLOCKED.** It needs a Meta test number and approved templates. |
>
> **BOOKING_WORKFLOW, AWS_QUEUE and RECOVERY_WORKER passed on 23 Nov; AURORA passes only with the pilot exception. No `deployment_evidence` row has been written.** The booking switch stays OFF.
>
> The text below is the original plan of 21 Nov, kept as written.

## Original plan (21 Nov 2026)

The six gates are those in `docs/operations/DEPLOYMENT_GATES.md`, unchanged. **No gate has passed**: nothing is
deployed in AWS yet (`04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md`). Local tests prove the code behind each gate; they are not
gate evidence. The booking switch stays OFF even when all six pass: turning it on is a separate owner decision.

B-AWS-0 (an AWS account that can call Bedrock in India) stays open but no longer blocks the pilot: decision T52 makes
OpenRouter the primary provider for synthetic data and keeps all infrastructure in `ap-southeast-2`.

| # | Gate | Requirement (summary) | AWS resources | Verification to perform | Evidence location | Result | Remaining action | Owner decision |
|---|---|---|---|---|---|---|---|---|
| 1 | WORKLOAD_IDENTITY (with B-AWS-0) | Each workload maps to exactly one DB role; pipeline only to `setuhaul_deploy_evidence`; no workload reaches `setuhaul_rpc`, owner or superuser; staff context survives a real Data API transaction; ordinary Cognito ops users cannot create an ops_lead context; drift detection on policies | IAM roles and policies, Lambda execution roles, API Gateway IAM, Cognito, Secrets Manager, AWS Config | the five acceptance checks in DEPLOYMENT_GATES.md, run against the deployed roles; IAM policy hash recorded in the manifest | `deployment_evidence` row (gate WORKLOAD_IDENTITY) + artifact in S3 | **NOT TESTED** (blocked: not deployed) | E0, E3, E5 | approve the IAM table in the deployment plan §4 |
| 2 | AURORA | Aurora PostgreSQL 18.3+; migrations 0001…0024 applied with hashes; RLS and role allowlist tests against it; Data API behind `Db.tx()`; both evidence triggers enabled | Aurora Serverless v2, RDS Data API, Secrets Manager | migration runner; `tests/test_agent_not_operator.py` and `tests/test_db_requests.py` against Aurora; trigger check | evidence row + test log artifact | **NOT TESTED** (blocked) | E0, E3 | none |
| 3 | AWS_QUEUE | FIFO, content-independent dedup id, FIFO DLQ with redrive 1–10 and retention ≥ queue, tested redrive procedure, visibility ≥ 2 × lease, least-privilege send/receive | SQS FIFO + DLQ, IAM | `SqsFifoQueue` attribute and redrive checks (already in code) against the real queue; a redrive drill | evidence row (fingerprint computed live) | **NOT TESTED** (blocked) | E0 | none |
| 4 | RECOVERY_WORKER | `recover()` every minute; lease renewal and generation fencing against the real queue; retries; DLQ procedure | EventBridge schedule, Lambda, SQS | the `test_inbound_delivery.py` scenarios driven against the deployed queue | evidence row (valid 7 days) | **NOT TESTED** (blocked) | E2 | none |
| 5 | BOOKING_WORKFLOW | Step Functions approvals: request, approve, reject, expiry, no auto-approval; idempotency; the published version is the one running | Step Functions, Lambda approval relay | approval tests against the real state machine; version check against the manifest | evidence row | **NOT TESTED** (blocked) | E4 | B-WF-2a rules stay disabled (no change) |
| 6 | OUTBOUND_DELIVERY | Notifications with retries and provider-side duplicate prevention; the stored reply row is sent; approved templates | Lambda outbound, Meta WhatsApp Cloud API (test number), Secrets Manager | send each template (en / hi / hi-Latn) to a test phone; replay a delivery and see it refused as a duplicate | evidence row (valid 7 days) | **NOT TESTED** (blocked: no Meta test number or templates yet) | E6 | submit the templates to Meta |

**Readiness (22 Nov 2026):** the code each gate will exercise in AWS now exists and is tested locally: E0–E6 in
`07_AWS_IMPLEMENTATION.md` (infrastructure template assertions, Data API backend, Lambda entry points, Step Functions
adapter, Data API migrations, one login per role, manifest builder). Results stay NOT TESTED until run in AWS.

**Owner decisions (22 Nov 2026):** notices outside WhatsApp's 24-hour window go as Meta-approved templates, and with
no approved template they fail with an escalation, never silently (built: `07_AWS_IMPLEMENTATION.md`, E6-01). The
Hinglish template definitions are kept, but no language code is assumed for them: their submission and approval are
a separate gate (HINGLISH_TEMPLATES), and until then a Hinglish driver outside the window gets the approved English
template.

Monitoring that each gate must show working (from DEPLOYMENT_GATES.md) is listed in `06_PILOT_OPERATIONS_GUIDE.md` §2.

**What is verified locally (not gate evidence):** the start-up check refuses `SETUHAUL_BOOKING_ENABLED=true` without
a FIFO queue, a DLQ, a manifest and current evidence for all six gates (`tests/test_inbound_delivery.py`); the
fingerprints are computed by `python -m setuhaul.ops.fingerprints`; the evidence table refuses updates, deletes and
back-dated rows (migration 0013; `tests/test_inbound_delivery.py`).
