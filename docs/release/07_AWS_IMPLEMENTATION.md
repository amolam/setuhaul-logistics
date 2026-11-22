# AWS build items E0–E6: implementation record (22 Nov 2026)

**Implemented and tested locally. Not deployed.** Nothing was created in AWS, no paid model call was made, no template
was submitted to Meta, and `SETUHAUL_BOOKING_ENABLED` is `false` everywhere. The six step 6 gates are still
**NOT TESTED**: everything below is local evidence that the code is ready to be tested in AWS, not gate evidence.

Two commits, kept apart so the reviewed baseline stays reproducible:

| Commit | Branch | Contents | Tests on that exact code |
|---|---|---|---|
| `250b794` (review baseline, unchanged) | `phase-e/aws-implementation` | E0–E6 as first built | full normal suite **not** run on it: the 924-passed run predates its last change (Data API error classes); the Data API run of the database test files (347 passed) and the four AWS test files in normal mode (63 passed, 1 skipped) do cover it |
| `5a9a16d` | `phase-e/aws-implementation-fixes`, from exactly `250b794` | the consolidated review fixes below | full normal suite and Data API mode, both run once after its last code change: `01_TEST_REPORT.md` |
| second fix commit (this record) | `phase-e/aws-implementation-fixes`, on top of `5a9a16d` | E4-02, E5-07, E5-08 below | full normal suite and Data API mode, both run once after its last code change: `01_TEST_REPORT.md` |

Labels used for evidence: **verified** = run here and read; **emulated** = run against a local stand-in whose fidelity
to AWS is an assumption; **AWS-verified** = run against real AWS (nothing is, yet); **not tested**.

## What was built

| Item | Files | Evidence | Label |
|---|---|---|---|
| **E0** Infrastructure as code (CDK v2, Python), `ap-southeast-2` only | `infra/app.py`, `infra/setuhaul_stack.py`, `cdk.json`, `requirements-infra.txt` | `tests/test_infra.py` synthesises the stack and asserts: Sydney; booking switch `false`, data class synthetic and no plain-text secret on all 8 functions; Aurora PostgreSQL 18.3 Serverless v2 with the Data API, encrypted, deletion-protected, pgaudit (`ddl,role`, no parameters), `standard_conforming_strings` on, its log group created by the stack (no CDK helper Lambda); no NAT or internet gateway; FIFO queue + FIFO DLQ meeting the AWS_QUEUE contract; each function can read only its own database logins; only the outbound function can read the WhatsApp token; the pilot API has `/tools` off; the repository state machine with a published version and `live` alias; WAF and throttling on the API; Cognito MFA required, admin-created users only, the three staff groups; the gate monitoring alarms with an SNS action, the 4xx alarm named for what it counts | verified (synth), not deployed |
| **E1** API entry point | `setuhaul/aws/lambdas.py` (`api_handler`, Mangum) | `tests/test_aws.py`: `/health` and a signed WhatsApp webhook through the handler | verified |
| **E2** Worker entry points | `lambdas.py`: `agent_handler` (SQS batch 1, visibility extended by the orchestrator heartbeat, failures reported for redelivery), `recovery_handler`, `outbound_handler`, `approval_relay_handler`; application secrets from Secrets Manager at cold start (refused from the environment in staging/pilot) | `tests/test_aws.py`: a driver message from webhook to stored agent reply; a failing turn reported, not swallowed; recovery, approval and outbound handlers | verified (SQS and Meta stubbed) |
| **E3** Database over the RDS Data API | `setuhaul/aws/data_api.py`; `SETUHAUL_DSN=rds-data` selects it (`make_db`) | `tests/test_data_api.py` (hostile strings, every result type equal to psycopg's, error codes, rollback, per-role refusal, two-round-trip setup); the database test files also run through it (`SETUHAUL_TEST_DATA_API=1`) | emulated |
| **E4** Step Functions approval workflow | `setuhaul/aws/stepfunctions.py`; migration `0024_task_callback_tracking.sql` | `tests/test_aws.py`: idempotent start, token stored and answered once, late token answered at once, lost callback re-sent by the reconciler, a confirmed callback never re-sent, an unclear outcome retried after a backoff with the token kept, a finished task recorded and left alone, expiry in PostgreSQL with retry when not yet due | verified (Step Functions stubbed) |
| **E5** Pipeline | `.github/workflows/ci.yml`; `scripts/build_lambda.py`; `setuhaul/ops/assembly.py`; `setuhaul/aws/migrate_data_api.py`; `setuhaul/aws/provision_logins.py`; `setuhaul/ops/manifest.py` | The app writes its Cloud Assembly to `cdk.out/`; every Lambda in the template uses the one staged zip, and the credential-free check finds the template, asset manifest, staged zip, `build/lambda.zip` and the deployment manifest agree (and fails when any of them is altered); migrations over the Data API build a catalog identical to the psycopg runner's; edited migrations refused; logins end up in exactly one role and cannot switch; the manifest passes the start-up check and changes with the artifact or any IAM change; the Lambda zip is byte-reproducible on Windows (two builds, same SHA-256); the pinned CDK CLI reads the assembly | verified locally on Windows; **CI (Ubuntu) has not run yet** |
| **E6** Meta preparation and template sending | `scripts/whatsapp_templates.py`, `infra/whatsapp_templates.json` (21 definitions, `"submitted": false`); `setuhaul/notify/outbound.py` | `tests/test_infra.py`: the file equals what the reviewed texts generate, Hinglish has no assumed language code; `tests/test_outbound.py`: inside the 24-hour window text, outside it the approved template in the driver's language or English, no approved template or an agent reply outside the window fails with an escalation, the safety margin, Meta's refusal code kept | verified (Meta stubbed); not submitted |

## Design decisions made in this phase

| Decision | Why | Where |
|---|---|---|
| **Client-side literal binding** for the Data API (values rendered as SQL literals, `standard_conforming_strings = on` forced first in every transaction and in the parameter group, NUL refused) | The Data API types every parameter (a string is `varchar`), while psycopg leaves strings untyped and the server infers uuid, arrays and overloads. Typed parameters would break SQL that works today. psycopg2 used the same technique for years | `data_api.py` docstring; 14 hostile-string tests |
| Data API errors translated to the **same psycopg error class** for their SQLSTATE, with `sqlstate` and `diag.message_primary` | The service maps database refusals (HOLD_CONFLICT, 23P01, 42501, …) from those two fields, and code that catches a specific class (e.g. `InsufficientPrivilege`) must behave the same; the Data API run of the suite caught the class difference | `data_api.translate` |
| REST API + WAF (not an HTTP API) | AWS WAF cannot be attached to API Gateway HTTP APIs; the deployment plan's WAF line needed a REST API | `setuhaul_stack.py` |
| Lambdas outside the VPC; Aurora in isolated subnets; no NAT | Data API over HTTPS with IAM; OpenRouter reachable without a NAT gateway | stack |
| Migrations over the Data API (statement splitter) | The cluster has no network path in; the Data API runs one statement per call | `migrate_data_api.py` |
| State machine task permissions from the machine's fixed name | A reference would be a dependency cycle (found by synth) | stack |
| Setup in two round trips per transaction | Each Data API call is a network request; seven setup calls per transaction would add seconds to a driver turn | `data_api.py`; test |

## Least-privilege database logins per workload (proven, not assumed)

Each handler test runs with logins for only these roles; a handler that touched another role would fail.

| Workload | Database roles | Why |
|---|---|---|
| api | ingest, console | webhook (ingest), console routes (console); `/tools` is off in staging and pilot, so no agent or feasibility login (IAM-02) |
| agent | agent, feasibility, ingest | tools (agent, feasibility), turn leases (ingest); **no system** |
| recovery | system, ingest | sweep (system), re-publishing inbound events (ingest) |
| outbound | system | |
| approval relay and the three state-machine tasks | system | |
| pipeline | deploy_evidence (owner only for migrations and login provisioning) | |

## Security review of the new code

| Area | Finding | Status |
|---|---|---|
| SQL injection through literal binding | Strings quoted with doubled quotes under forced standard strings; arrays escaped for the array parser; unknown types refused; placeholder and parameter counts checked | tested (hostile strings round-trip unchanged) |
| Secrets in logs | Login passwords appear only in `CREATE/ALTER ROLE`, run with `pgaudit.log = 'none'` for that transaction; pgaudit logs DDL and role changes, never statement parameters; application secrets never logged; an unreadable secret's error names no secret | tested where local |
| Plain-text secrets | None in any function environment (asserted on the template); staging/pilot refuse them from the environment | tested |
| Booking switch | `"false"` on every function; nothing in the stack or pipeline can set it | tested |
| WhatsApp token | Own secret `setuhaul/<env>/whatsapp`; only the outbound function has the IAM grant and the secret id; refused in the application secret and in a staging/pilot environment | tested (template and loader) |
| `/tools` | Not registered in staging and pilot whatever the environment says; the API has ingest and console logins only | tested |
| Supply chain | Every GitHub Action pinned to a full commit SHA (release in the comment); CDK CLI pinned to `aws-cdk@2.1148.0` | done; Dependabot alerts on |
| Deploy branch | The deploy job runs only from `refs/heads/release/fde-sydney-pilot` (job condition and a first-step guard), with the typed confirmation still required | tested statically (`tests/test_pipeline.py`) |
| Still open | (1) the GitHub OIDC provider and deploy role are not in the stack: the owner sets the repository's OIDC subject to include `ref`, creates the role from `infra/github_oidc_deploy_trust_policy.json` (repository + environment + branch in one exact `sub`, scoped to `ap-southeast-2`) and verifies that a token from another branch is refused (`04` §4; external gate); (2) the AWS Managed Rules common rule set could block an unusually large webhook body: watch the WAF metrics during initial testing; (3) the deploy gate is a typed confirmation in an environment without required reviewers (free private repository): an owner-controlled gate, not two-person approval; (4) the application secret (session and Meta app secrets) is still read by every function, because every function builds the app | follow-ups before or at deployment |

## Cost review (changes against `06_PILOT_OPERATIONS_GUIDE.md` §5)

| Line | Change | Per month |
|---|---|---|
| WAF | on a REST API: web ACL + 3 rules + requests | ≈ $8–9 (was $10–12) |
| API Gateway REST | ~$4.25 per million requests in Sydney; pilot volume is tiny | < $1 |
| RDS Data API | ≈ $0.35 per million requests; about 10 requests per transaction after the setup change | < $1 at pilot volume |
| Secrets Manager | 10 secrets (owner, 6 logins, application, WhatsApp, OpenRouter) | ≈ $4.00 |
| CloudWatch alarms | 14 alarms | ≈ $1.40 |
| **Total** | still **≈ $75–120 a month** (Aurora's 0.5 ACU minimum is most of it) | estimate |

## Gaps that remain before the gates can be tested

| Gap | Needs |
|---|---|
| Templates submitted to Meta and approved; the owner then lists the approved ones in the `whatsapp_approved_templates` context (until then every notice outside the window fails with an escalation, by design) | owner, at the Meta submission gate |
| Hinglish: which language code Meta accepts and whether it approves the texts (separate gate HINGLISH_TEMPLATES; no code is assumed) | owner, at submission |
| E3-01 to E3-04 below | the AURORA gate |
| CI has not run on GitHub yet: the Ubuntu build reproducibility (E5-06), synthesis and the assembly check on the runner | owner approval to push the branch |
| Deployment itself, migrations against Aurora, login provisioning, the six gates | owner approval and AWS sign-in |

## Review fixes (consolidated review of `250b794`, 22 Nov 2026)

Branch `phase-e/aws-implementation-fixes`, from exactly `250b794`. `main` stays at `996c602`.

| ID | Priority | Finding | Fix | Evidence | Label |
|---|---|---|---|---|---|
| E5-01 | High | `python infra/app.py` run outside the CDK CLI writes the assembly to a temporary directory, so `cdk.out/` did not exist to upload; the docstring said otherwise | The app writes to `$CDK_OUTDIR`, default `cdk.out/`; the synth job sets `CDK_OUTDIR=cdk.out`; docstring corrected | `test_the_app_writes_its_assembly_to_the_named_directory` (runs the app as the pipeline does); local run wrote `cdk.out/` | verified locally |
| E5-02 | High | The deploy job re-synthesised from source (and lacked `build/lambda/`) instead of deploying the reviewed assembly | The synth job uploads the complete `cdk.out/` (template, asset manifest, staged zip) with `build/lambda.zip`; the deploy job re-runs the check on the download, compares it with the synth job's result, and runs `cdk deploy --app cdk.out` | workflow; the pinned CLI lists the stack from `cdk.out/` with no AWS call | verified locally; CI not run |
| E5-03 | High | CDK zipped the `build/lambda/` folder itself while the manifest hashed `build/lambda.zip` | The Lambda asset is the zip file (a folder is refused); CDK stages it byte for byte. Aurora's log retention no longer adds a CDK helper Lambda, so all 8 functions use the one staged zip. `setuhaul/ops/assembly.py` follows template → asset manifest → staged file and hashes the bytes; the manifest is built from that staged file | `test_the_assembly_check_*` (passes; fails on an altered zip, a function pointing elsewhere, a missing staged file, a mismatched manifest); local: staged zip, `build/lambda.zip` and manifest all `d0761360…abcd` | verified locally |
| E6-01 | High | Free-form text outside WhatsApp's 24-hour window would be refused, so a late approval notice could disappear | The relay checks the window in the database (30-minute margin). Outside it a notice goes as the Meta-approved template (driver's language if approved, else English), with variables checked against the text; with no approved template, or for an agent reply, the notification fails and a NOTIFICATION_FAILED escalation tells a person. Meta refusals keep their error code | 12 new tests in `tests/test_outbound.py` | verified (Meta stubbed) |
| IAM-01 | High | Every function read the secret holding the WhatsApp token | Separate `setuhaul/<env>/whatsapp` secret, readable by the outbound function only | `test_only_the_outbound_function_can_read_the_whatsapp_token`; loader tests | verified (synth) |
| IAM-02 | High | The API kept agent and feasibility logins for `/tools` | `/tools` off in staging and pilot; API logins are ingest and console | `test_the_tools_route_is_off_…`; the API handler tests run with only those two logins | verified |
| E4-01 | Medium | The reconciler re-sent every closed callback of the last 24 hours every minute | Migration 0024: `approval_callbacks` records attempts, the next due time and a definite outcome. A claim counts the attempt and backs off before sending; only a definite answer from Step Functions completes it, so a failed or unclear send stays due. A decided approval row is still never modified | 7 new tests in `tests/test_aws.py`; catalog tests (no application role can touch the table) | verified (Step Functions stubbed) |
| E5-04 | Medium | Actions pinned by major version, CDK CLI by major version | Commit SHAs for every action; `aws-cdk@2.1148.0` | workflow | done |
| E5-05 | Medium | The guides described Lambda alias rollback, but the stack publishes no Lambda versions or aliases | Documents corrected: rollback = redeploy the previous reviewed assembly (`cdk deploy --app <its cdk.out>`, from that run's artifact); the state machine alias is the only alias | `04` §6, `06` §4 | corrected |
| E5-06 | Medium | Build reproducibility verified on Windows only | The synth job builds the zip twice on Ubuntu and fails if they differ | workflow step | **outstanding until CI runs** |
| E0-01 | Medium | `Api4xxAuth` counted every 4xx | Renamed `Api4xxClientErrors`, with a description saying it counts all client errors | `test_the_4xx_alarm_is_named_for_what_it_counts` | verified (synth) |

### Second review round (22 Nov 2026): E4-02, E5-07, E5-08

Fixed on top of `5a9a16d` in a new commit; earlier commits unchanged.

| ID | Severity | Root cause | Files | Fix | Regression tests | Validation | Remaining external gate |
|---|---|---|---|---|---|---|---|
| E4-02 | High | `least(30, power(2, c.attempts)::integer)` casts before it caps: from 31 attempts `power(2, attempts)` exceeds `integer` and the claim raises `integer out of range`, so a callback could never be retried again | `migrations/0024_task_callback_tracking.sql` | `least(30, power(2, least(c.attempts, 5))::integer)`: the exponent is capped first; delays 1, 2, 4, 8, 16, then 30 minutes. 0024 was edited in place because it has never been applied outside throwaway test databases (nothing deployed; applied migrations stay immutable once a real database has them) | `tests/test_aws.py::test_e4_02_the_backoff_is_capped_at_30_minutes_without_integer_overflow` (attempts 0–6, 31, 32, 1,000,000) | against the old SQL the 31, 32 and 1,000,000 cases fail with `integer out of range`; with the fix all pass, in normal and Data API mode | none |
| E5-07 | Medium | The assembly check compared only `S3Key`; a function could point at another bucket with the same key and still pass | `setuhaul/ops/assembly.py` | Each function on its own: `Code` must be exactly `S3Bucket` + `S3Key`; the bucket must be a literal or a single `Fn::Sub` string, the key a literal; the pair must equal one asset-manifest destination's `bucketName` and `objectKey`; all functions must resolve to the one staged asset; anything missing, malformed or mismatched is refused and named by function | `tests/test_infra.py::test_e5_07_a_valid_assembly_passes_with_every_function_checked`, `…a_function_in_another_bucket_with_the_same_key_is_refused`, `test_the_assembly_check_fails_when_a_function_points_elsewhere` (key only), `…every_function_is_checked_not_one_representative`, `…a_missing_or_malformed_code_reference_is_refused` (8 cases), `…an_asset_destination_in_another_bucket_is_refused` | all pass; the real account-agnostic `cdk.out/` (bucket `cdk-hnb659fds-assets-${AWS::AccountId}-ap-southeast-2`) still passes | none |
| E5-08 | High | The deploy job required `workflow_dispatch` and the typed `pilot` but not a branch, so a manual dispatch on any branch could deploy | `.github/workflows/ci.yml`; new `infra/github_oidc_deploy_trust_policy.json`; `04` §4 | Allow-list of exactly `refs/heads/release/fde-sydney-pilot` (the one pilot release branch in `03`) in the job condition and in a first-step guard that runs before checkout and sign-in; typed confirmation kept. Required OIDC trust documented: subject customised to include `ref`, `StringEquals` on the exact `repo:…:environment:pilot:ref:refs/heads/release/fde-sydney-pilot`, no wildcard; the environment-only subject is explicitly not accepted as branch identity | `tests/test_pipeline.py` (condition allow-list; 8 refused refs incl. feature, `main`, tags, pull requests; confirmation and event still required; guard is the first step; guard script refuses the same refs under bash; trust policy exact and wildcard-free; environment-only subject refused) | all pass (static, no credentials) | **AWS-side and GitHub-side enforcement**: the repository's OIDC subject customisation and the deploy role do not exist yet; the owner creates and verifies them. Not claimed as implemented |

### Data API conditions for the AURORA gate (not local failures)

| ID | Condition | How it is checked |
|---|---|---|
| E3-01 | Real Aurora error messages carry the `SQLState:` suffix the translation reads | run the error-class tests of `tests/test_data_api.py` against Aurora |
| E3-02 | The real text form of `timestamptz` results and its time zone | the type-parity test against Aurora |
| E3-03 | The 1 MiB Data API response limit is not handled | before the pilot: a tested bound on the largest console listing at pilot scale, or paging |
| E3-04 | Latency, throttling and the 3-minute idle-transaction timeout | measured during the AURORA and RECOVERY_WORKER gates |
