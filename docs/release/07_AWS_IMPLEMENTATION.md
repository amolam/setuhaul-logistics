# AWS build items E0–E6: implementation record (22 Nov 2026)

**Implemented and tested locally. Not deployed.** Tests: full suite 924 passed, 1 skipped; the database test files
through the Data API backend 347 passed (`01_TEST_REPORT.md`). Nothing was created in AWS, no paid model call was made, no template
was submitted to Meta, and `SETUHAUL_BOOKING_ENABLED` is `false` everywhere. The six step 6 gates are still
**NOT TESTED**: everything below is local evidence that the code is ready to be tested in AWS, not gate evidence.
Branch `phase-e/aws-implementation`.

Labels: **verified** = run here and read; **emulated** = run against a local stand-in whose fidelity to AWS is an
assumption; **not tested**.

## What was built

| Item | Files | Evidence | Label |
|---|---|---|---|
| **E0** Infrastructure as code (CDK v2, Python), `ap-southeast-2` only | `infra/app.py`, `infra/setuhaul_stack.py`, `cdk.json`, `requirements-infra.txt` | `tests/test_infra.py` synthesises the stack and asserts: Sydney; booking switch `false`, data class synthetic and no plain-text secret on all 8 functions; Aurora PostgreSQL 18.3 Serverless v2 with the Data API, encrypted, deletion-protected, pgaudit (`ddl,role`, no parameters), `standard_conforming_strings` on; no NAT or internet gateway; FIFO queue + FIFO DLQ meeting the AWS_QUEUE contract; each function can read only its own database logins; the repository state machine with a published version and `live` alias; WAF and throttling on the API; Cognito MFA required, admin-created users only, the three staff groups; the gate monitoring alarms with an SNS action | verified (synth), not deployed |
| **E1** API entry point | `setuhaul/aws/lambdas.py` (`api_handler`, Mangum) | `tests/test_aws.py`: `/health` and a signed WhatsApp webhook through the handler | verified |
| **E2** Worker entry points | `lambdas.py`: `agent_handler` (SQS batch 1, visibility extended by the orchestrator heartbeat, failures reported for redelivery), `recovery_handler`, `outbound_handler`, `approval_relay_handler`; application secrets from Secrets Manager at cold start (refused from the environment in staging/pilot) | `tests/test_aws.py`: a driver message from webhook to stored agent reply; a failing turn reported, not swallowed; recovery, approval and outbound handlers | verified (SQS and Meta stubbed) |
| **E3** Database over the RDS Data API | `setuhaul/aws/data_api.py`; `SETUHAUL_DSN=rds-data` selects it (`make_db`) | `tests/test_data_api.py` (hostile strings, every result type equal to psycopg's, error codes, rollback, per-role refusal, two-round-trip setup); the database test files also run through it (`SETUHAUL_TEST_DATA_API=1`) | emulated |
| **E4** Step Functions approval workflow | `setuhaul/aws/stepfunctions.py` | `tests/test_aws.py`: idempotent start, token stored and answered once, late token answered at once, lost callback re-sent by the reconciler, expiry in PostgreSQL with retry when not yet due | verified (Step Functions stubbed) |
| **E5** Pipeline | `.github/workflows/ci.yml`; `scripts/build_lambda.py`; `setuhaul/aws/migrate_data_api.py`; `setuhaul/aws/provision_logins.py`; `setuhaul/ops/manifest.py` | Migrations over the Data API build a catalog identical to the psycopg runner's; edited migrations refused; logins end up in exactly one role (drift removed) and cannot switch to another; the manifest from the synthesised template passes the start-up check and changes with the artifact or any IAM change; the Lambda zip is byte-reproducible (two builds, same SHA-256) and holds Linux wheels only | verified (CI itself has not run yet) |
| **E6** Meta preparation | `scripts/whatsapp_templates.py`, `infra/whatsapp_templates.json` (21 definitions, `"submitted": false`) | `tests/test_infra.py`: the file equals what the reviewed texts generate | verified; not submitted |

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
| api | ingest, console, agent, feasibility | webhook (ingest), console routes (console); agent + feasibility **only** for the `/tools` route |
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
| Open items | (1) every function reads the one application secret, which also holds the WhatsApp token: split it so only `outbound` can read the token; (2) the API workload has agent + feasibility logins only because of `/tools`: disable that route in the pilot to drop them; (3) GitHub Actions are pinned by major version: pin by commit SHA before the first deploy; (4) `npx aws-cdk@2` is a major-version pin: pin the exact CLI version; (5) the GitHub OIDC provider and deploy role are not in the stack (the owner creates them, scoped to `ap-southeast-2`); (6) the AWS Managed Rules common rule set could block an unusually large webhook body: watch the WAF metrics in the first week | follow-ups |

## Cost review (changes against `06_PILOT_OPERATIONS_GUIDE.md` §5)

| Line | Change | Per month |
|---|---|---|
| WAF | on a REST API: web ACL + 3 rules + requests | ≈ $8–9 (was $10–12) |
| API Gateway REST | ~$4.25 per million requests in Sydney; pilot volume is tiny | < $1 |
| RDS Data API | ≈ $0.35 per million requests; about 10 requests per transaction after the setup change | < $1 at pilot volume |
| Secrets Manager | 9 secrets (owner, 6 logins, application, OpenRouter) | ≈ $3.60 |
| CloudWatch alarms | 14 alarms | ≈ $1.40 |
| **Total** | still **≈ $75–120 a month** (Aurora's 0.5 ACU minimum is most of it) | estimate |

## Gaps that remain before the gates can be tested

| Gap | Needs |
|---|---|
| WhatsApp messages outside the 24-hour window must be **template** messages; the provider sends free-form text today, so a late approval notice would be refused by Meta | owner decision on the approach; then a small change to `WhatsAppCloudProvider` and its tests |
| Hinglish templates are listed under language `en` with a `_hl` suffix | owner confirms at submission |
| The exact text form the real Data API returns for `timestamptz` (the decoder accepts an offset, else UTC) | checked in the AURORA gate |
| CI has not run on GitHub yet (it runs on the first push of this branch) | owner approval to push the branch |
| Deployment itself, migrations against Aurora, login provisioning, the six gates | owner approval and AWS sign-in |
