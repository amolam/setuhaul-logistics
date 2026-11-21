# Phase 2 · Design — Response to the External Architecture Review

**Reviewed artefact:** `phase2_design_page.html`. The reviewer saw the summary page only, not the Markdown files or `schema.sql`.

**How this response was made:**
1. Each factual claim was checked against AWS documentation (sources at the end).
2. Schema changes were re-validated on PostgreSQL 18 (`validation_results.md`).
3. Three decisions were confirmed with the project owner:
   - **Build scope:** production design + MVP vertical slice.
   - **API auth:** JWT for people, IAM for services.
   - **Models:** India-only inference with Sonnet 5 + Haiku 4.5.

**Disposition key:**

| Disposition | Meaning |
|---|---|
| ✅ **Accepted** | Design changed |
| ➕ **Accepted, extended** | Changed, and went further than the review |
| 📄 **Already covered** | Present in files the reviewer didn't see; now made visible on the page |
| ↩️ **Declined** | With reason |

---

## P0 — must fix before implementation

| # | Review point | Disposition | What changed |
|---|---|---|---|
| 1 | Cognito + identity pool + SigV4 everywhere is over-complicated; use a JWT authoriser for people | ✅ (owner approved) | **People:** Cognito user pool (MFA for staff, OTP for web drivers) issues JWTs, and API Gateway's **JWT authoriser** validates them. Role and facility checks happen in the FastAPI app (RBAC + facility ABAC). **Services:** every Lambda, AgentCore and AWS call uses **IAM roles**. **Agent:** AgentCore Identity for workload identity and Policy for tool authorisation. **Webhook:** Meta `X-Hub-Signature-256` verification. **No identity pool.** These are documented as separate trust mechanisms, not one model |
| 2 | Data API: IAM authorises the call, but DB credentials come from Secrets Manager; session state resets between calls; writer-only | ✅ verified, and the design was wrong | **Verified in AWS docs:**<br>• Data API uses DB credentials from **Secrets Manager** (`secretArn`); IAM authorises `rds-data:*`.<br>• Before reusing a pooled connection, it runs **`DISCARD ALL`**.<br>• It runs **only on the writer**.<br>• Responses are capped at **1 MiB**.<br>**Changes:**<br>• RLS scope is set with `SET LOCAL ROLE` and `set_config(...)` **inside the same Data API transaction** as the queries, and fails closed when missing.<br>• Added `FORCE ROW LEVEL SECURITY` and non-bypass roles `setuhaul_console`, `setuhaul_agent` and `setuhaul_system`. After the re-review, RLS covers every facility-owned table and **no role has BYPASSRLS**: jobs set an explicit scope (`07_acceptance_and_verification.md` §1A).<br>• Validator §5: scoped = 1 row, both = 2, unset = 0, non-superuser owner = 0, system = 2.<br>• **New rule found while testing:** PostgreSQL superusers always bypass RLS, so the app never connects as the master user.<br>• The Aurora reader is now documented as a **failover target only**, not a read scaler for the Data API |
| 3 | Outbox doesn't make notifications exactly-once | ✅ | The "nothing sent twice" claim is withdrawn. New wording: **at-least-once delivery + idempotent consumers**.<br>• Each notification carries `notification_id`; the sender records `(notification_id, channel)` before and after the provider call.<br>• Provider idempotency is used where offered.<br>• Delivery status is tracked.<br>• Residual risk stated: if the sender crashes after the provider accepts but before recording, a duplicate is possible. The message is written to be harmless if repeated (same reference number) |
| 4 | SQS FIFO dedupe is only a 5-minute window, not business idempotency | ➕ | Already persistent in the schema: `UNIQUE (channel, external_message_id)`, `driver_exceptions.dedupe_key UNIQUE`, `idempotency_keys` table. Now documented explicitly. Added `operation_id`, `operation_status`, `result_ref` to the idempotency record. SQS dedupe is described as a cheap first filter only |
| 5 | Contradiction: "fixed workflow" vs Sonnet with `tool_choice: auto` choosing tools | ✅ | **Committed to deterministic orchestration.**<br>• Haiku **extracts** (forced `record_driver_report` tool, strict schema).<br>• **Python** validates, fuses the ETA and **selects the next operation from the state machine**.<br>• Deterministic services run it.<br>• Sonnet **only phrases** the reply from verified tool output.<br>• **No autonomous tool selection in v1.**<br>• Strands runs inside AgentCore Runtime as the harness; the orchestrator calls tools through AgentCore Gateway so Policy and observability still apply. Sequence diagrams redrawn |
| 6 | Is "2" a selection or a booking confirmation? Rename `confirm_booking` | ✅ | The flow is now: select ("2") → read-back (*"You selected D2 at 13:00, unloading until 13:55. Shall I request it?"*) → explicit yes → 3-min hold → **`submit_booking_request`** → warehouse confirms or rejects. "Book D2 at 13:00" in one message counts as select + confirm, but the read-back is still sent as the receipt. `confirm_booking` is renamed to `submit_booking_request`. Only the warehouse (or an auto-confirm rule) **confirms** |
| 7 | Data residency: Global inference can process outside India; masking isn't approval | ➕ (owner chose India-only) | **New finding:** Bedrock now offers **India geographic cross-Region inference** (Mumbai ↔ Hyderabad only) for **Claude Sonnet 5, Opus 5 and Haiku 4.5** on `bedrock-runtime` (Messages API, InvokeModel, Converse). The design now uses `in.anthropic.claude-sonnet-5` and `in.anthropic.claude-haiku-4-5-20251001-v1:0`. Sonnet 5.5 / Haiku 5.5 (Global-only from Mumbai) are not used. A formal residency table was added (§ below). AgentCore Evaluations' cross-region processing is treated separately: online evals use sanitised traces only, or custom evaluators on the India profile |
| 8 | Evaluation and observability missing | 📄 + ➕ | Both existed (`phase1_frame/07_evaluation_plan.md`, `05_agent_observability_cost.md`) but were not on the page. Now added to the page. The review's metrics and acceptance gates were merged in (§ below). Trace attributes and the PII-in-logs ban were made explicit |

## P1 — should improve

| # | Review point | Disposition | What changed |
|---|---|---|---|
| 9 | Add Bedrock Guardrails explicitly | 📄 + ➕ | It was decision T15 but missing from the page. Added a protection matrix (§ below), including the reviewer's point that **tool results are not screened by the prompt-attack filter**: driver-originated text echoed back from the DB is wrapped and treated as data |
| 10 | Truck-level double booking; tenant isolation | ✅ truck · ↩️ tenant | **Truck:** new `ex_one_dock_per_truck EXCLUDE (vehicle_id =, during &&, shipment_id <>)`. The vehicle is set by trigger from the shipment. Validator: same truck on D1 and D2 at overlapping times → **rejected**. **Tenant:** declined for v1. SetuHaul is a single 3PL, and the isolation boundary that matters is the **facility** (RLS). A `tenant_id` would only matter if this became multi-customer SaaS, and is recorded as a future change |
| 11 | Tool permissions per agent | ✅ | One agent identity in v1. Gateway target permissions and Policy rules are listed per tool in `04_…`; the console API has its own role |
| 12 | ML vs deterministic boundaries | 📄 + ➕ | Added the table below. *"ETA prediction is probabilistic; availability is deterministic"* is now a stated principle |
| 13 | SLOs and failure recovery | 📄 | `phase1_frame/06_performance_reliability_security.md` (SLOs, FMEA, RPO/RTO, FIS). Summarised on the page |
| 14 | Defer AgentCore Memory | ✅ | Removed from v1. Conversation and workflow state live in **PostgreSQL** (threads, messages, shown options, settled facts) and **Step Functions** (approvals) |
| 15 | Webhook signature verification | 📄 | Already in the ingest Lambda (HMAC + replay window). Now listed in the auth table |
| — | `btree_gist`, half-open `[)` ranges, hold expiry | 📄 | All in `schema.sql`: `CREATE EXTENSION btree_gist`; `CHECK (lower_inc(during) AND NOT upper_inc(during))`; `place_hold()` releases expired overlapping holds in the same transaction, plus a reconciler. Validated |

## P2 / scope

| Review point | Disposition |
|---|---|
| Build a vertical slice, not the whole platform; target ₹1,000–2,500/month | ✅ owner approved. See "MVP vertical slice" below. The production architecture stays documented as the target |
| Defer AppSync, CloudFront + WAF, multi-channel notifications, live FASTag/SIM, Aurora Multi-AZ, DR deployment | ✅ deferred to the production rollout |
| Argilla for human feedback | ✅ adopted as **optional** for the eval and labelling loop (Phase 4/5). Until then, labels are stored as JSONL from the console review page, in a format Argilla can import |
| Redis | ↩️ not needed: idempotency, holds and timers are in PostgreSQL |
| Local Docker | Adjusted: Docker isn't installed on the build machine, so the MVP uses the **native PostgreSQL 18** already installed. Docker Compose files are still provided for portability |

---

## Revised decisions

### Data residency (decision T24)

| Data | Decision |
|---|---|
| Model inference | `in.` India geographic profile only (ap-south-1 ↔ ap-south-2). Global profiles blocked by an IAM / SCP condition on the inference-profile ARN |
| Driver phone number | Never sent to an LLM. Replaced by the thread pseudonym |
| Vehicle registration, licence | Pseudonymised before prompts. Masked in logs |
| GPS / pin / SIM coordinates | Never in LLM context. Only the derived ETA and confidence |
| Shipment, customer, order identifiers | Opaque IDs (order reference shown to the driver only, from the template layer) |
| Evaluation datasets | Synthetic or sanitised; no production PII |
| AgentCore Evaluations (online) | Sanitised traces only, or custom evaluators using the India profile. Global-CRIS evaluators are not used on production data |
| Human feedback / labels | Masked fields only |
| Storage | All stateful data in ap-south-1; backups copied to ap-south-2 (production only) |

### Model choice (decision T3/T4, revised)

| Use | Model (India profile) | Settings |
|---|---|---|
| Extraction | `in.anthropic.claude-haiku-4-5-20251001-v1:0` | **Forced tool use** of `record_driver_report`. `strict: true` if the India profile accepts it on Converse (**verify in the Build spike**); otherwise Pydantic validation + one corrective retry. No thinking. Converse or Messages API on `bedrock-runtime` |
| Reply phrasing | `in.anthropic.claude-sonnet-5` | No tools. Adaptive thinking off or minimal. Effort `low`. Output checked by the grounding post-check |
| Judge (evals) | `in.anthropic.claude-sonnet-5` | Fixed rubric |

**Gotcha found (from the Claude API reference).** Current Anthropic SDK releases send API version `2026-10-21` through their Bedrock clients, and Bedrock accepts that only for Claude 4.7+. **Haiku 4.5 therefore needs either a pinned older SDK or boto3 Converse.** The design uses **Strands' Bedrock provider (boto3 Converse)** for both models, which avoids the issue. This is the first Build-phase spike.

### ML vs deterministic boundary (decision T25)

| Capability | v1 | Future ML (only with labelled history) |
|---|---|---|
| Combine GPS, FASTag and driver reports | Deterministic `fuse_eta()` | Keep deterministic |
| Source confidence | Deterministic rules | Calibrate from historical declared-vs-actual |
| Predict actual arrival | Not modelled | Gradient-boosted model (LightGBM / XGBoost) on lane, time and carrier features. Offline experiment first; feeds only `eta_confidence` / `uncertainty_min` |
| Abnormal delay detection | Rules (speed, staleness) | Anomaly detection |
| Dock allocation | OR-Tools CP-SAT | Stays optimisation, never ML |
| Language understanding | Haiku extraction | — |

**Principle:** *ETA prediction is probabilistic; appointment availability is deterministic. They never mix.* ML may change confidence and uncertainty. It never decides capacity.

### Guardrails and protection matrix

| Threat | Protection |
|---|---|
| Prompt injection in driver text | Bedrock Guardrails prompt-attack filter on the **user input** (Converse `guardContent`). Plus: the LLM cannot select tools in v1, so injected instructions have nothing to act on |
| PII | Deterministic pre-mask (phone, registration, licence) + Guardrails sensitive-information filter |
| Malformed model output | Strict tool schema (Haiku) + **Pydantic** validation. Grounding post-check on Sonnet text |
| Unauthorised tool use | Orchestrator state machine + AgentCore Policy + backend ownership checks + DB constraints and RLS |
| Untrusted tool results | Tool output is structured data from our DB. Any driver-originated string inside it is wrapped as quoted data, length-capped and never placed in the system prompt. Not relied on Guardrails for this |
| Unsafe operational requests ("cancel all appointments") | No such operation exists for the driver principal. Business policy plus human escalation |

### Evaluation acceptance gates (merged)

| Metric | Gate | Source |
|---|---|---|
| Structured-output validity (extraction) | ≥ 99 % | Review |
| ETA extraction precision / recall on the golden set | ≥ 95 % / ≥ 95 % | Review + Phase 1 |
| Intent classification accuracy | ≥ 95 % | Review |
| Next-operation (tool) selection correctness | **100 %**: deterministic code, so any miss is a bug | Revised |
| Tool argument correctness | ≥ 98 % | Review |
| Critical booking-rule violations, double bookings, unauthorised operations, duplicate mutations | **0** (hard gates) | Both |
| Grounding (every time and dock in replies traceable) | 100 % (hard gate) | Phase 1 D4 |
| Golden scenario completion | ≥ 95 % | Review |
| Judge rubric mean | ≥ 4.0, never < 3 on overpromising | Phase 1 |
| Cost per resolved exception | ≤ $0.08 (production) | Revised |

**Golden set for the MVP:** 60 scenarios (brief beats, ladder, seed threads, audit traps, Hinglish, adversarial).
- Core-only (stubbed LLM) runs on every commit.
- The real-model suite runs on demand and before demos, to fit the MVP budget.

### Observability: one trace per message

The `message_id` → `correlation_id` (= trace) carries through every span:

1. webhook
2. ingest
3. SQS (queue age, retries)
4. AgentCore Runtime (model id, input / output / cache tokens, latency)
5. Gateway tool span (tool, status)
6. booking service (rule checks, result)
7. PostgreSQL (transaction, idempotency hit, constraint outcome)
8. Step Functions (approval state)
9. notification (delivery status, retries)

**Metrics:** the Phase 2 list, plus agent latency p50/p95, tool failure rate, SQS oldest-message age, booking success rate, hold-expiry cleanup lag, outbox publish lag, notification failure rate, escalation rate, and cost per conversation.

**Never logged:** raw phone numbers, coordinates, tokens or credentials, or unmasked message bodies.

---

## MVP vertical slice (what Build will deliver)

**One complete delay → options → select → confirm → hold → request → warehouse decision workflow**, plus no-slot escalation and two-driver contention. It must be real where it matters.

| Component | MVP | Production target |
|---|---|---|
| Driver channel | Web chat (+ WhatsApp test number if available) | WhatsApp Business |
| Ops console | React + **FastAPI** (local) | Same app on Lambda / ECS behind API Gateway + CloudFront / WAF |
| Auth | **Real Cognito** user pool + JWT; FastAPI RBAC / ABAC | Same + API Gateway JWT authoriser |
| Database | **Local PostgreSQL 18**, same `schema.sql`, psycopg repository | Aurora Serverless v2 + Data API (repository swap) |
| Orchestrator + LLM | Strands harness; **real Bedrock India profile** | Same, in AgentCore Runtime |
| AgentCore Runtime / Gateway / Policy | Deployed for integration and demos only | Always on |
| Queue / workflow | **Real SQS FIFO + Step Functions** (approval) | Same + EventBridge Scheduler, outbox relay |
| Scheduler | OR-Tools locally | Lambda container |
| ETA signals | Mock FASTag / pin / SIM adapters | Real vendors |
| Notifications | One channel | WhatsApp + SMS + email + paging |
| Evals | pytest + golden set; Argilla optional | + AgentCore Evaluations (sanitised) |
| Observability | OpenTelemetry locally + AWS traces for deployed parts | Full CloudWatch / X-Ray dashboards |

**MVP running cost (estimate):**

| Item | ~ per month |
|---|---|
| Bedrock: ~300 development conversations + 4 golden-set runs (240 conversations + judge) at ≈ $0.05 each | ≈ $25 |
| AgentCore during demo windows | < $3 |
| Cognito, SQS, Step Functions, Lambda (free-tier scale) | < $2 |
| **Total** | **≈ $30 ≈ ₹2,500** at ~₹85 per USD (rate not verified) |

Running the golden set fewer times keeps it nearer ₹1,500.

**Production estimate, revised** with India-profile models and deterministic orchestration:
- **LLM:** ≈ **$0.04 per conversation**. The regional premium is +10 %, and Haiku 4.5 costs more per token, but fewer Sonnet calls more than offset both. **Confirm Bedrock India prices in the console.**
- **AWS total:** ≈ **$440 / month** at 100 conversations/day and ≈ **$2,060** at 1,000/day. AgentCore Memory is removed.

## Sources

- [RDS Data API overview (Secrets Manager credentials)](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html) · [Session management (DISCARD ALL)](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.session-management.html) · [Limitations (writer only, 1 MiB)](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.limitations.html)
- [Amazon Bedrock expands Claude availability to India cross-Region inference (29 Sep 2026)](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-expands-claude-model-availability-to-india-cross-region-inference/) · [Global CRIS for India](https://aws.amazon.com/blogs/machine-learning/access-anthropic-claude-models-in-india-on-amazon-bedrock-with-global-cross-region-inference) · [Supported inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html)
- [AgentCore Evaluations cross-region inference](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations-cross-region-inference.html)
- [Anthropic pricing (regional / multi-region +10 % on Bedrock)](https://platform.claude.com/docs/en/about-claude/pricing)
