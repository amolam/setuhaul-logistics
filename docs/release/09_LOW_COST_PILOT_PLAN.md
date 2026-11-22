# Low-cost synthetic pilot: inventory, cleanup plan, security impact and cost (22 Nov 2026)

**For approval. Nothing below has been created, changed or deleted in AWS.** Goal: a production-like FDE demonstration
on real AWS at minimal ongoing cost, not production-grade availability. Account: AWS Free plan, about $100 of credits
left. Region `ap-southeast-2`.

Prices marked **verified** come from the AWS Price List API for Asia Pacific (Sydney), read on 22 Nov 2026; others are
marked **estimate**. Usage figures are estimates.

## 1. What exists in AWS now (read-only inventory)

| Resource | State | Evidence | Plan |
|---|---|---|---|
| Stack `SetuHaulPilot` | `ROLLBACK_COMPLETE` after the free-plan refusal of 7-day Aurora backups | CloudFormation | delete (required before a new create) |
| Log group `/aws/rds/cluster/setuhaul-pilot/postgresql` | retained by the rollback, 0 bytes, 0 streams | CloudWatch Logs | delete (its name blocks the redeploy) |
| Cognito pool `setuhaul-pilot-staff` | retained, 0 users, 0 app clients, no domain, deletion protection off | Cognito | delete |
| Customer-managed KMS key `d3e1fc22-…` (from the failed stack) | enabled, no alias, 0 grants, no secret or log group uses it; CloudTrail shows only CloudFormation management reads (DescribeKey, GetKeyPolicy, GetKeyRotationStatus, ListResourceTags), no Encrypt/Decrypt/GenerateDataKey | KMS, CloudTrail | schedule deletion, 7-day wait |
| AWS-managed key `233858d4-…` | AWS-managed | KMS | **not touched** |
| API Gateway account setting and role `SetuHaulPilot-ApiCloudWatchRole…` | retained; the region's API Gateway account setting **points at this role** | API Gateway | **keep** (in use; IAM roles are free); the new stack stops managing this setting |
| `CDKToolkit` (bootstrap), OIDC provider, deploy role | created and verified earlier today | CloudFormation, IAM | keep |
| Default VPC | AWS default | EC2 | not touched |
| NAT gateways, Elastic IPs, VPC endpoints | none | EC2 | none needed |

## 2. Changes to the stack (code; deployed only after approval)

| Change | Why | Security impact |
|---|---|---|
| Aurora Serverless v2 **min 0 ACU, auto-pause after 300 s, max 2 ACU** (Aurora 18.3 supports min 0, verified) | no compute charge while idle | none |
| Aurora **backup retention 1 day** (the Aurora minimum; 0 is not possible) | free-plan limit; synthetic, disposable data | point-in-time restore window 1 day instead of 7 |
| **Remove WAF** (web ACL, 3 rule groups, association) | ~$8–9 a month | weaker edge filtering: API Gateway throttling (20 req/s, burst 40) limits volume but does **not** inspect requests (no managed rule sets, no per-IP rate rule, no bad-input signatures). Kept: HMAC-signed webhooks, Cognito with MFA for staff, input validation (pydantic, extra fields forbidden), the database's own checks, RLS. Acceptable only for a controlled synthetic pilot |
| **Schedules off when idle**: the 3 one-minute rules (recovery, outbound, approval relay) are created DISABLED; `python -m setuhaul.ops.pilot_mode wake|sleep` enables or disables them for a test session | otherwise they query the database every minute and **Aurora never pauses** (~0.5 ACU around the clock ≈ $73 a month) | none. While asleep, a driver message is still received, queued and answered by the agent (SQS triggers it), but the reply is sent to the test sink only after `wake`; nothing is lost (outbox) |
| **Data API resume retry**: `BeginTransaction` retried with backoff on `DatabaseResumingException` (up to ~45 s) | the first request after a pause would otherwise fail while Aurora resumes (~15 s) | none. The webhook Lambda has 29 s (API Gateway limit); a webhook arriving during a cold resume may time out once, and Meta, or the test harness, retries it; idempotent by message id |
| **Log retention 1 week** (Lambda and database logs; was 1 month) | shorter retention for synthetic data | less history for diagnosis |
| API Gateway `cloudWatchRole: false` | the existing account setting already has a role; avoids re-creating and orphaning one | none |
| Everything else unchanged | — | database private in isolated subnets, reachable only through the Data API with IAM and per-role secrets; no public endpoint; least-privilege roles as reviewed |

**Networking (cheapest secure, unchanged):** the Lambdas run outside the VPC, so OpenRouter (HTTPS) is reached
directly through Lambda's managed egress: **no NAT gateway** (which would cost ≈ $0.059 per hour plus data, estimate,
≈ $43 a month idle), **no public IPv4** ($0.005 per hour each, verified), **no VPC endpoints** ($0.013 per hour each per
AZ, verified). Aurora is in isolated subnets with no route to the internet and is reached only through the Data API.

**Auto-pause blockers checked:** no RDS Proxy, no Performance Insights, no enhanced monitoring, no replicas, no public
access in the template (verified from the synthesised template). The Data API itself manages connections; whether
its connections let the cluster pause after 5 minutes is **to be verified after deployment** (the `ServerlessDatabaseCapacity`
metric falling to 0).

## 3. What keeps charging while idle

| Item | Basis | Per month |
|---|---|---|
| Secrets Manager | 10 secrets × $0.40 (verified) | $4.00 |
| KMS customer key | 1 key × $1.00 (estimate, list price) | $1.00 |
| CloudWatch alarms | 14 × $0.10 (verified) | $1.40 |
| Aurora storage | ~0.2–0.5 GB × $0.11 (verified price, estimated size); 1-day backup within the free allocation | $0.02–0.06 |
| SQS long polling by the agent's event source | ~0.3–0.7 M empty receives × $0.50/M (verified FIFO price; mostly inside the 1 M free requests) | $0–0.35 |
| CloudWatch log storage, CDK asset bucket (28 MB) | $0.033/GB-month (verified) | < $0.05 |
| Aurora compute when paused, Lambda, API Gateway, Step Functions, EventBridge (disabled), Cognito (0 users), X-Ray | $0 while idle | $0 |
| **Idle total** | | **≈ $6.50–6.90** |

## 4. Monthly cost estimate

Usage-based prices (verified): Aurora $0.20 per ACU-hour, storage $0.11 per GB-month, I/O $0.22 per million; API
Gateway REST $3.50 per million; Lambda $0.20 per million requests and $0.0000166667 per GB-second; Step Functions
$0.000025 per transition; SQS FIFO $0.50 per million; CloudWatch logs ingestion $0.67 per GB.

| Scenario | Fixed (idle) | Usage-based | AWS total per month | OpenRouter (separate) |
|---|---|---|---|---|
| **A. Idle** (schedules asleep, Aurora paused) | $6.50–6.90 | ≈ $0 | **≈ $7** | $0 |
| **B. A few hours of E2E a week** (≈ 3 h a week, 13 h a month awake; 0.5–1 ACU while active plus 5 min after each burst; < 5,000 requests) | $6.50–6.90 | Aurora $1.50–3.00; Lambda, API Gateway, SQS, Step Functions inside free allowances or < $0.10; logs < $0.20 | **≈ $8–10** | ≤ $3 (key limit) |
| **C. An active demonstration day** (8 h awake, 0.5–2 ACU) | daily share ≈ $0.22 | Aurora $0.80–3.20; other usage < $0.20 | **≈ $1.20–3.60 for that day** (add to A or B) | ≤ $0.25 that day (app cap) |
| For comparison: current stack, always on, with WAF | ≈ $82–98 | | **≈ $85–100** | |

At scenario B the $100 credits last roughly 10–12 months of AWS charges (free-plan accounts are also limited in time:
check the plan's end date in the AWS Billing console).

## 5. Cleanup order (after approval)

1. Re-read each leftover just before deleting it (state, size, users, grants, aliases).
2. Delete the stack `SetuHaulPilot` (`ROLLBACK_COMPLETE`; deletes nothing further, the retained resources are outside it).
3. Delete the empty log group, then the empty Cognito pool.
4. Schedule deletion of KMS key `d3e1fc22-…` with the 7-day waiting period (cancellable during those 7 days).
5. Leave the API Gateway role, `CDKToolkit`, the OIDC provider and the deploy role.

## 6. Then

Commit the stack and code changes with tests, run CI, fast-forward `release/fde-sydney-pilot`, and redeploy through
the pipeline. Verify outputs, functions (booking off, synthetic, test sink), migrations, logins, seed, the schedules
disabled, and Aurora pausing; then the Option A scenarios (`scripts/e2e_aws.py`) with `wake` before and `sleep` after.

## 7. Approved and done (22 Nov 2026)

### Cleanup in AWS (each item re-read just before deletion)

| Item | Re-check | Result |
|---|---|---|
| Stack `SetuHaulPilot` | `ROLLBACK_COMPLETE`, termination protection off, nothing left in it but the retained items | **deleted** (`stack-delete-complete`) |
| Log group `/aws/rds/cluster/setuhaul-pilot/postgresql` | 0 bytes, 0 streams, 0 subscription filters | **deleted** |
| Cognito pool `setuhaul-pilot-staff` | 0 users, 0 app clients, 0 identity providers, no domain, no triggers, deletion protection off | **deleted** |
| KMS key `d3e1fc22-…` (customer-managed) | 0 aliases, 0 grants; no secret, log group, queue, topic, database or function refers to it; CloudTrail: management reads only | **PendingDeletion**, deletes on 29 Nov 2026 12:36 IST (cancellable until then) |
| AWS-managed key, API Gateway logging role (still the region's setting), `CDKToolkit`, OIDC provider, deploy role | — | **untouched** |

Revised inventory: `CDKToolkit`; the OIDC provider and deploy role; the API Gateway account setting with its role; the
KMS key pending deletion; the AWS-managed key; the default VPC. No database, no stack, nothing that charges per hour.

### Stack and code (not deployed; needs its own deployment approval)

| Item | Where | Test |
|---|---|---|
| Aurora 0–2 ACU, `SecondsUntilAutoPause` 300, backups 1 day | `infra/setuhaul_stack.py` | `test_aurora_postgresql_18_serverless_with_the_data_api_isolated_and_audited` |
| No WAF; no API Gateway account resource (`cloudWatchRole: false`) | stack | `test_low_cost_pilot_no_waf_but_throttling_and_staff_need_mfa` |
| Schedules named and DISABLED; nothing that blocks pausing; logs 7 days; resume waits 20 s (api) / 40 s | stack | `test_aurora_can_pause_nothing_wakes_it_while_idle_and_logs_are_short` |
| `python -m setuhaul.ops.pilot_mode status / wake / sleep / pause-check` | `setuhaul/ops/pilot_mode.py` | `tests/test_pilot_mode.py` |
| Data API resume retry (`DatabaseResumingException`, backoff 1, 2, 4, 5 s, bounded wait) | `setuhaul/aws/data_api.py`, owner session | `test_begin_waits_while_the_database_resumes`, `test_begin_gives_up_after_the_wait_and_never_retries_other_errors`, `test_application_transactions_ride_out_a_resume` |
| Live protection probes (S1) and wake/sleep around the end-to-end run | `scripts/e2e_aws.py` | run after deployment |

### Acceptance requirements

| # | Requirement | Local evidence | After deployment |
|---|---|---|---|
| 1 | Wake/sleep preserve pending outbox work and handle in-flight executions safely | idempotent wake/sleep; sleep disables before it drains; a notice owed while asleep waits as an outbox event and is delivered after wake; a drain overlapping a scheduled run sends each notice once; Step Functions and the agent are not affected by sleep (`tests/test_pilot_mode.py`) | `pilot_mode sleep` output (drain results, counts) |
| 2 | Aurora actually reaches 0 ACU after inactivity | configuration only: min 0, 300 s, schedules off, no blockers in the template | **to show**: `pilot_mode pause-check` (CloudWatch `ServerlessDatabaseCapacity` at 0) after sleep + more than 5 minutes |
| 3 | Authentication, signed webhooks, throttling and input validation remain | `test_api.py`: `test_webhook_signature_and_duplicates`, `test_console_rejects_bad_tokens` (9 cases), `test_console_role_action_matrix`, `test_session_tokens_are_verified`, `test_ggn_planner_cannot_see_or_decide_jai`, the 422 checks; `test_aws.py`: `/tools` off; `test_infra.py`: throttling 20/40, MFA | **to show**: scenario S1 against the live API |
| 4 | Queued driver responses are distinguished from delivered test-sink responses while asleep | `pilot_mode status` separates queued (not yet sent), delivered to the test sink (`testsink.` id), sent by a real provider (must stay 0), failed, and outbox events waiting | the same counts from the live database |
