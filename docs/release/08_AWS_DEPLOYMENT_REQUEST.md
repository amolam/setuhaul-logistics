# AWS deployment request: Option A, synthetic pilot with booking off (22 Nov 2026)

**One authorization for everything below.** Nothing in this document has been created, configured or run in AWS yet.
Region `ap-southeast-2` only. `SETUHAUL_DATA_CLASS=synthetic`, `SETUHAUL_BOOKING_ENABLED=false` on all 8 functions,
the production environment refused, WhatsApp off (internal test sink), no Meta submission, model budget unchanged
($0.25 a day in the app, $3 a month on the OpenRouter key, top-ups off).

## 1. What would be deployed

The reviewed Cloud Assembly from the release branch, through the pipeline's deploy job (`cdk deploy --app cdk.out`,
never a fresh synthesis). The commit to deploy is the first one below that you approve:

| Commit | Contents | Status |
|---|---|---|
| `99d98af` | Phase E with both review rounds | CI: see section 8 |
| next commit (this request) | Deployment blockers for Option A: (1) the **notification test sink** (`SETUHAUL_NOTIFICATION_MODE=test_sink`, the default; WhatsApp needs an explicit `whatsapp` mode **and** `SETUHAUL_WHATSAPP_ENABLED=true`, otherwise refused), without it the outbound function would call Meta's API; (2) the **synthetic seed over the Data API** (`setuhaul.aws.synthetic_seed`: two facilities, four docks, hourly slots; `SYN-` ids and `+999` reserved phone numbers; refuses to run if any non-synthetic row exists), without it the database is empty and unreachable by the existing psycopg loaders; (3) a seed step in the deploy job | local tests pass; CI after push |

## 2. Resources created

| Resource | Notes |
|---|---|
| **CDK bootstrap stack `CDKToolkit`** (once, by you from your machine: `cdk bootstrap aws://<ACCOUNT_ID>/ap-southeast-2`) | S3 asset bucket, ECR repository, and the CDK roles (deploy, file-publishing, image-publishing, lookup, CloudFormation execution). The execution role defaults to AdministratorAccess; to narrow it, bootstrap with `--cloudformation-execution-policies <policy ARN>` |
| **GitHub OIDC provider** `token.actions.githubusercontent.com` (if not present) | audience `sts.amazonaws.com` |
| **Deploy role** `setuhaul-pilot-github-deploy` | trust: `infra/github_oidc_deploy_trust_policy.json`, exact `sub` `repo:amolam/setuhaul-logistics:environment:pilot:ref:refs/heads/release/fde-sydney-pilot`, StringEquals only |
| **Stack `SetuHaulPilot`** | VPC with isolated subnets only (no NAT, no internet gateway); Aurora PostgreSQL 18.3 Serverless v2 (0.5–4 ACU, Data API, encrypted, deletion protection, 7-day backups) and its log group; 10 Secrets Manager secrets (owner, 6 database logins, application, WhatsApp, OpenRouter); KMS key; SQS FIFO queue and DLQ; 8 Lambda functions; Step Functions state machine with version and `live` alias; 3 EventBridge rules; REST API with WAF (3 rules) and throttling; Cognito user pool (MFA required, 3 groups); SNS topic and 14 alarms |

GitHub settings (yours, repository level): the `pilot` environment, the repository variable `SETUHAUL_DEPLOY_ROLE_ARN`,
and the OIDC subject customisation `include_claim_keys: ["repo", "context", "ref"]` (without it the token's subject has
no branch and the role above refuses it). On the free plan the environment cannot restrict branches or require
reviewers; branch restriction comes from the workflow and the role's trust.

## 3. IAM permissions of the deploy role

| Statement | Actions | Resources |
|---|---|---|
| Use the CDK roles | `sts:AssumeRole` | `arn:aws:iam::<ACCOUNT_ID>:role/cdk-hnb659fds-*-<ACCOUNT_ID>-ap-southeast-2` |
| Migrations, logins, seed over the Data API | `rds-data:BeginTransaction`, `ExecuteStatement`, `CommitTransaction`, `RollbackTransaction` | `arn:aws:rds:ap-southeast-2:<ACCOUNT_ID>:cluster:setuhaul-pilot` |
| Read the owner and login secrets | `secretsmanager:GetSecretValue` | `arn:aws:secretsmanager:ap-southeast-2:<ACCOUNT_ID>:secret:setuhaul/pilot/db/*` |
| Decrypt those secrets | `kms:Decrypt` with `kms:ViaService = secretsmanager.ap-southeast-2.amazonaws.com` | the stack's key (`alias/setuhaul-pilot`) |

No other permissions; no `iam:*`, no wildcard resources outside the patterns above. The functions' own roles are in
the stack and were reviewed (each reads only its own database logins; only `outbound` can read the WhatsApp secret).

## 4. Secrets and configuration you set (Claude never enters them)

| Secret | Value |
|---|---|
| `setuhaul/pilot/openrouter` | your OpenRouter key ($3/month limit, top-ups off) |
| `setuhaul/pilot/app` | `SESSION_SECRET` is generated; set `META_APP_SECRET` to any random string (it signs the synthetic test webhooks; no Meta app is used) |
| `setuhaul/pilot/whatsapp` | leave as generated: it is not read in test-sink mode |
| AWS CLI on this machine | your sign-in, so the end-to-end checks can read the stack outputs and query the database as the evidence role |

## 5. Cost

| | Estimate |
|---|---|
| One-off | none beyond the first hours of running resources; CDK bootstrap storage is cents |
| Ongoing, per month | **≈ $75–120** (`06_PILOT_OPERATIONS_GUIDE.md` §5): Aurora at 0.5 ACU around the clock is $45–65 of it; WAF $8–9; Secrets Manager ≈ $4; KMS $1–2; CloudWatch $8–15; Lambda, API Gateway, SQS, Step Functions, Cognito ≈ $1–5; OpenRouter at most $3 |
| Per day while running | ≈ $2.50–4 |
| Cost lever (not applied) | Aurora Serverless v2 can pause at 0 ACU when idle, removing most of the Aurora line at the price of a ~15 s resume; a one-line stack change you may approve separately |

## 6. Rollback and teardown

- Application rollback: redeploy the previous reviewed assembly (`cdk deploy --app cdk.out` with that run's
  `pilot-build` artifact). Migrations are forward-only.
- Full teardown: `cdk destroy SetuHaulPilot` after turning off the cluster's deletion protection; the KMS key, the
  database log group and the Cognito pool are retained by policy and are deleted by hand; Aurora leaves a final
  snapshot (delete it to stop storage charges). Then delete the deploy role, the OIDC provider (if created for this)
  and, optionally, `CDKToolkit`.

## 7. What Option A can and cannot show

- Can show, in AWS: a signed synthetic webhook through API Gateway, the API Lambda, SQS, the agent Lambda with
  OpenRouter extraction (and the rules reader when the model fails or the budget is spent), feasibility against Aurora,
  options returned and stored, the outbound function sending to the test sink, notification failure handling (the
  reserved number `+99900000009999` is refused by the sink), the recovery and approval relay schedules, CloudWatch logs.
- Cannot show with booking off: a booking request, an approval, a Step Functions execution or a callback. The booking
  tools refuse while `SETUHAUL_BOOKING_ENABLED=false`, so no approval is created. These belong to Option D (separate
  approval), which also needs a Cognito planner user with MFA that you create, or an approved confirmation policy.

## 8. Order of work after your authorization

1. You: CDK bootstrap, OIDC provider, deploy role, GitHub variable and subject customisation, secrets (section 4).
2. Me: inspect the role and its trust from the AWS CLI (no change), fast-forward `release/fde-sydney-pilot` to the
   approved commit after its CI passes, start the deploy workflow on that branch with confirmation `pilot`.
3. Me: verify outputs, functions (booking off, synthetic, test sink), migrations 0001–0024 recorded, logins, seed.
4. Me: Option A end-to-end scenarios, each with its execution and request ids and database evidence.
5. Stop again before Option D.
