# Phase E: AWS Sydney pilot deployment plan

**Update 22 Nov 2026: build items E0–E6 are implemented and tested locally (`07_AWS_IMPLEMENTATION.md`); still not
deployed.** Two corrections from that work: the API is a REST API (AWS WAF cannot attach to an HTTP API), and the
least-privilege login table in §4 is now proven by tests (`07_AWS_IMPLEMENTATION.md`).

**Status: planned, not deployed.** Nothing has been created in AWS. The AWS CLI on this workstation is not signed in,
and deploying needs the owner's approval of this plan. Region `ap-southeast-2` for everything. Synthetic data only.
`SETUHAUL_BOOKING_ENABLED=false` throughout this phase.

## 1. What exists today and what must be built first

| Component | In the repository | Ready for AWS? |
|---|---|---|
| HTTP API (FastAPI): webhook, tool calls, console, `/health` | `setuhaul/api/app.py` | Runs under uvicorn. Needs a container image or a Lambda adapter (**E1**) |
| Inbound queue | `SqsFifoQueue` (send, receive, attribute and redrive checks) | ✅ code ready; queue and DLQ to create |
| Agent worker (orchestrator) and recovery (`InboundRelay.recover`) | `setuhaul/agent/orchestrator.py`, `setuhaul/api/inbound.py` | Logic ready. Needs a worker entry point: SQS-triggered Lambda or a long-running task (**E2**) |
| Model provider | `model_for()`: OpenRouter from Secrets Manager, metered (migration 0023); Bedrock optional | ✅ code ready; secret to create |
| Database | 23 forward-only migrations, five application roles, RLS | ✅ schema ready. `Db` speaks the Data API *pattern* over psycopg; a real RDS Data API backend (or IAM-auth connections per role) is **E3** |
| Approval workflow | `ApprovalWorkflow` port, `LocalApprovalWorkflow`, `approval_workflow.asl.json` | Needs a Step Functions adapter implementing the port, and the state machine deployed (**E4**) |
| Outbound WhatsApp | `OutboundRelay`, `WhatsAppCloudProvider` (Graph API v21.0) | ✅ code ready; Meta test number, token and approved templates to set up (**E6**) |
| Staff identity | `CognitoVerifier` (JWT, groups, facility scope) | ✅ code ready; user pool to create |
| Deployment evidence | migration 0013, `python -m setuhaul.ops.fingerprints`, manifest checks | ✅ code ready; the pipeline that builds the manifest is **E5** |
| Infrastructure as code | none | **E0** |

## 2. Recommended pilot shape (smallest that can pass the six gates)

```
Meta test number ──HTTPS──► API Gateway (REST API, WAF) ─► Lambda "api" (FastAPI via an ASGI adapter)
                                                       │  webhook: verify HMAC, store, enqueue
                                                       ▼
                                         SQS FIFO setuhaul-inbound.fifo ──► DLQ setuhaul-inbound-dlq.fifo
                                                       │ (event source mapping, batch 1)
                                                       ▼
                                         Lambda "agent" (orchestrator; OpenRouter key from Secrets Manager)
                                                       │
EventBridge 1/min ─► Lambda "recovery" (InboundRelay.recover)   Lambda "outbound" (OutboundRelay) ─► Meta Graph API
                                                       │
                    Step Functions "approval" (B-WF-2, published version) ◄─ Lambda "approval-relay"
                                                       │
                     Aurora PostgreSQL Serverless v2 (18.x), RDS Data API, one secret per workload role
Cognito user pool (staff, MFA) · Secrets Manager · KMS · CloudWatch logs/metrics/alarms · AWS Config (drift)
```

Why Lambda and the Data API: no VPC NAT or RDS Proxy (the India design saved about $200/month this way), and the
Data API is what the `Db.tx()` contract was written for. A single container on ECS Fargate is the fallback if the
ASGI adapter or Data API backend proves awkward; it then needs a VPC, NAT or endpoints, and RDS Proxy.

## 3. Build items before the first deployment

| Id | Work | Size | Proven by |
|---|---|---|---|
| E0 | IaC (AWS CDK, Python, per the original design): stacks for network-less Lambdas, SQS + DLQ, Aurora + Data API, Step Functions, Cognito, Secrets, alarms | L | `cdk synth` + `cdk diff` reviewed |
| E1 | Lambda entry point for the API (Mangum or the AWS Lambda Web Adapter) | S | the API test suite against the adapter |
| E2 | Worker entry points: agent (SQS event, lease renewal through the visibility timeout), recovery, outbound, approval relay | M | `tests/test_inbound_delivery.py` scenarios against a real queue (RECOVERY_WORKER gate) |
| E3 | `Db` backend on the RDS Data API (BeginTransaction / ExecuteStatement / Commit), one Secrets Manager secret per workload role, `begin_staff_context` inside the transaction | M | RLS and role allowlist tests run against Aurora (AURORA, WORKLOAD_IDENTITY) |
| E4 | Step Functions adapter for `ApprovalWorkflow`; deploy `approval_workflow.asl.json` as a published version | M | approval tests against the real state machine (BOOKING_WORKFLOW) |
| E5 | Pipeline: build artifacts, write the manifest, run `setuhaul.ops.fingerprints`, record evidence as `setuhaul_deploy_evidence` | M | the start-up check accepts the evidence |
| E6 | Meta: test WhatsApp number, system-user token in Secrets Manager, templates submitted (English, Hindi, Hinglish separately) | owner + S | OUTBOUND_DELIVERY gate |

These are code changes; each will come with tests and a review before deployment.

## 4. IAM (least privilege, to be proven by the WORKLOAD_IDENTITY gate)

| Workload | Allowed | Database role |
|---|---|---|
| api | `sqs:SendMessage` on the inbound queue; read its own DB secrets | ingest, console; agent + feasibility for `/tools` only |
| agent | `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility` on the inbound queue; read the OpenRouter secret and its own DB secrets | agent, feasibility, ingest |
| recovery | `sqs:SendMessage`; its DB secrets | system, ingest |
| outbound | read the Meta token secret; its DB secret | system |
| approval-relay | `states:StartExecution` on the published state machine version; its DB secret | system |
| pipeline | `rds-data` for `setuhaul_deploy_evidence` only | deploy_evidence |

No workload may assume another's role, use `setuhaul_rpc`, the owner or a superuser (DEPLOYMENT_GATES.md, workload
identity acceptance 1–5).

## 5. Configuration per environment

| | dev / eval / ci | staging | pilot |
|---|---|---|---|
| `SETUHAUL_ENV` | dev / eval / ci | staging | pilot |
| `SETUHAUL_DATA_CLASS` | synthetic | synthetic | synthetic |
| Model key | `OPENROUTER_API_KEY` or key file | Secrets Manager (`SETUHAUL_OPENROUTER_SECRET_ID`) | Secrets Manager |
| Usage ledger and daily cap | optional | required | required, $0.25/day |
| Bedrock | allowed (optional) | refused (not metered) | refused (not metered) |
| `SETUHAUL_BOOKING_ENABLED` | false | false | **false** (this phase) |
| Queue | LocalQueue | SQS FIFO | SQS FIFO |

## 6. Deployment and rollback steps (after approval)

1. The owner signs in to the AWS CLI for the Sydney account (Claude never enters credentials).
2. `cdk bootstrap` and `cdk deploy` the stacks into `ap-southeast-2`.
3. Run migrations 0001–0023 against Aurora through the migration runner as the owner role, once.
4. Store the OpenRouter key and the Meta token in Secrets Manager (owner, from their own machine).
5. Smoke test: `GET /health` → `{"status": "ok", "booking_enabled": false}`; one synthetic driver message through the
   Meta test number; check the usage ledger row and the reply.
6. Run the gate checks in the order in `05_STEP6_GATE_STATUS.md` and record evidence.
7. **Rollback:** Lambda aliases point at the previous version (instant); the state machine alias points at the previous
   published version; migrations are forward-only, so a schema problem is fixed by a new migration, never by editing
   one. Aurora point-in-time restore is the last resort (synthetic data, so no data-loss risk).

## 7. WhatsApp in the pilot

| Path | What it needs | How it is verified |
|---|---|---|
| Inbound webhook | Meta app webhook to the API Gateway URL, app secret in Secrets Manager | a message from a test phone appears in `chat_messages` |
| Replies inside the 24-hour customer-service window | free-form text allowed | the stored reply row is delivered (OUTBOUND_DELIVERY) |
| Proactive notifications (outside the window: approval results, invalidations) | **approved Meta templates**, submitted per language (English, Hindi, Hinglish) | each template delivered once to a test phone; duplicates refused at the provider |

Local simulations and mocks are not evidence of WhatsApp delivery.
