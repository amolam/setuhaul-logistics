# Phase G: pilot operating guide and monthly cost

For the synthetic-data Sydney pilot, once deployed (`04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md`). Booking switch OFF.

## 1. Daily checks

| What | How | Healthy |
|---|---|---|
| Application | `GET /health` | `200`, `{"status": "ok", "database": "ok", "booking_enabled": false}`; `503` means the database does not answer |
| Booking switch | `/health` and the Lambda configuration | `booking_enabled: false` everywhere |
| Inbound queue | CloudWatch `ApproximateAgeOfOldestMessage`, DLQ `ApproximateNumberOfMessagesVisible` | oldest < 2 min; DLQ = 0 |
| Model spend | `SELECT day, spent_usd, reserved_usd FROM setuhaul.model_budget_day ORDER BY day DESC LIMIT 7;` (as `setuhaul_system`) | below $0.25/day; OpenRouter dashboard under $3/month |
| Model failures | `SELECT status, error_category, count(*) FROM setuhaul.model_usage WHERE day = current_date GROUP BY 1, 2;` | mostly `OK`; any `auth`/`credits` is an alert |
| Escalations | Console → Escalations | every row has an owner |

## 2. Alarms (CloudWatch), matching the gate monitoring

| Alarm | Source | Gate |
|---|---|---|
| Oldest inbound message > 5 min | SQS metric | AWS_QUEUE |
| DLQ depth > 0 | SQS metric | AWS_QUEUE / RECOVERY_WORKER |
| Recovery failures, expired processing leases | log metric filter on the recovery Lambda | RECOVERY_WORKER |
| Webhook 5xx rate sustained (> 5 % for 10 min) | API Gateway metric | RECOVERY_WORKER |
| Database connection errors, failed transactions | Aurora / Data API metrics, log filter | AURORA |
| Failed or expired booking confirmations | Step Functions `ExecutionsFailed`, `ExecutionsTimedOut` | BOOKING_WORKFLOW |
| Authorization failures | API Gateway 401/403, Cognito | WORKLOAD_IDENTITY |
| Undelivered driver notifications | outbound log filter; rows stuck unsent | OUTBOUND_DELIVERY |
| `ALERT model_call auth/credits` | log filter on the agent Lambda | model provider |
| Daily model budget > 80 % | metric from `model_budget_day` | cost |

## 3. Model provider: limits and behaviour

- Application cap `SETUHAUL_LLM_DAILY_BUDGET_USD=0.25` (UTC day), reserved atomically before every attempt; key credit
  limit $3.00/month on OpenRouter; automatic top-ups off.
- When either is reached, or OpenRouter answers 402/403/429/503, times out, or returns an unusable answer, the turn uses
  the rules reader (`fallback_reason` in the trace). Booking and approval do not depend on the model.
- Rate limits: one retry only for a 429 whose `Retry-After` is ≤ 2 s; otherwise the rules reader.
- Details: `docs/operations/COSTS.md`.

## 4. Runbooks

| Situation | Action |
|---|---|
| Messages in the DLQ | Read the message attributes (never the driver text into a ticket), fix the cause, redrive with the SQS redrive tool; the orchestrator's generation fencing makes a redelivered message safe |
| Webhook failing | Check the Meta app secret in Secrets Manager and the API Gateway logs; Meta retries for up to 7 days |
| Notifications not delivered | `OutboundRelay` retries; check the Meta token, template status and the outbound log; never re-send by hand (duplicate prevention is at the provider) |
| Model failing or over budget | Nothing to do for safety (rules reader takes over); check `model_usage.error_category`; `auth`/`credits` → check the OpenRouter key and credit (owner) |
| A conversation needs a person | Console → thread → **Take over**; the agent stops replying; **Hand back** with a summary when done |
| Incident | 1. Confirm booking switch OFF. 2. If the agent misbehaves, take over the affected threads, then point the agent Lambda alias back to the previous version. 3. Record the incident in `docs/review/REVIEW_LOG.md` |
| Rollback | Lambda and state machine aliases back to the previous version; migrations are forward-only (fix forward) |
| Backup and recovery | Aurora automated backups (7 days) and point-in-time restore; the data is synthetic, so a restore is a drill, not a recovery |

## 5. Monthly cost estimate (pilot)

Assumptions, labelled: about 50 synthetic conversations a day, staff use in working hours, Sydney prices as listed by
AWS on 21 Nov 2026 where known, otherwise marked "estimate". Not measured in AWS: nothing is deployed yet.

| Line | Basis | Per month |
|---|---|---|
| **1. AWS infrastructure** | | |
| Aurora Serverless v2 | 0.5 ACU minimum around the clock (or auto-pause to 0 ACU when idle) + 1 GB storage + I/O | $45–65 (estimate; less with auto-pause) |
| Lambda, API Gateway, SQS, Step Functions, EventBridge, Cognito | pilot volume, mostly inside the always-free allowances | $1–5 (estimate) |
| Secrets Manager | 9 secrets at $0.40 (owner, 6 logins, application, OpenRouter) + API calls | ≈ $3.60 |
| KMS | 1–2 customer keys | $1–2 |
| WAF (on the REST API) | web ACL + 3 rules + requests | $8–9 |
| GuardDuty, Security Hub, AWS Config | small account, few resources | $5–15 (estimate) |
| **2. OpenRouter inference** | Haiku 4.5 extraction, $0.0013–0.0035 per extraction measured in evaluation runs; capped by the app at $0.25/day and by the key at $3/month | **at most $3** (the key limit binds; at 50 conversations/day the daily cap is reached and the rules reader takes the rest) |
| **3. Bedrock (optional)** | not used in the pilot (dev/eval only, not metered); same Haiku 4.5 list price as above if enabled later | $0 |
| **4. WhatsApp** | test number during the pilot; replies inside the 24-hour window are free; proactive utility templates are charged per message by Meta's rate card for the recipient's country | $0 on the test number; check Meta's current rate card before any real-number use |
| **5. Monitoring and logging** | CloudWatch logs (~2–5 GB), 10–15 alarms, a dashboard | $8–15 |
| **Total** | | **≈ $75–120 a month** |

For comparison, the original India production design (Bedrock, AgentCore, Mumbai) was estimated at about $440/month
at 100 conversations a day and $2,060 at 1,000 a day (`docs/2-design/02_architecture.md` §7, the readout deck). Neither
OpenRouter nor Bedrock inference is free for this account.
