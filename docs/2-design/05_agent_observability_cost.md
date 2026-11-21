# Phase 2 · Design — Agent Design, Observability and Cost Controls

> **Revised after external review (17 Nov 2026).** Where this file and `06_review_response.md` differ, `06_review_response.md` wins. Key changes: JWT for people / IAM for services; India-only models (`in.anthropic.claude-sonnet-5`, `in.anthropic.claude-haiku-4-5`); deterministic orchestration (no autonomous tool selection); `submit_booking_request` replaces `confirm_booking`; select → read-back → explicit yes before any hold; Data API = IAM + Secrets Manager, RLS set inside each transaction; outbox is at-least-once; AgentCore Memory deferred; truck-level exclusion constraint; MVP vertical slice.

## 1. Agent design

### 1.1 Shape: a workflow with an agent at the edges

Each driver turn runs a **fixed pipeline**. The model is used in only two places.

```
ingest (code) → [Haiku] extract → validate + fuse_eta (code) → decide next step (code: missing fields? requires human? options?)
            → tools (code) → [Sonnet] phrase reply from tool output → grounding check (code) → send
```

- **Decide next step is code, not the model.** If `missing_fields` is non-empty, ask. If `requires_human`, escalate. If the intent is ASK_OPTIONS or REPORT_DELAY with an at-risk booking, find options. If BOOK with a selected option, hold then confirm. If CHECK_STATUS, get the status and use a template.
- The Sonnet agent loop (Strands in AgentCore Runtime) is used for multi-turn phrasing and the rare open-ended question ("can I park overnight?"). Its tools are limited to the contract list, and Policy rules R1–R6 apply.
- **Status checks and confirmations use templates** (no LLM). Expected share is about 30–40 % of turns.

### 1.2 Models and settings (Bedrock `bedrock-runtime`, India geographic inference profile, via Strands / boto3 Converse)

| Use | Model | Settings |
|---|---|---|
| Extraction / classification | `in.anthropic.claude-haiku-4-5-20251001-v1:0` (India profile; forced tool + strict schema if accepted, else Pydantic + one retry) | Structured output against the `record_driver_report` schema (`output_config.format`). Effort `low`. Prompt ≤ 4k tokens |
| Reply phrasing (no tools) | `in.anthropic.claude-sonnet-5` (India profile) | **No tools**: the orchestrator already ran them; Sonnet only turns verified tool output into a short reply. Thinking off or minimal, low effort, ≤ 300 output tokens |
| Judge (evals) | `in.anthropic.claude-sonnet-5` | Fixed rubric prompt. Structured scores |

**Platform notes (verified for Claude in Amazon Bedrock):**
- Server-side `fallbacks` are **not** available, so use the SDK's client-side refusal-fallback pattern and the deterministic degraded mode.
- Batches, Files API and the MCP connector are not available (we don't need them).
- Calls use the **India** profile (ap-south-1 ↔ ap-south-2). Global profiles are denied in IAM. PII masking stays as defence in depth.
- The current Anthropic SDK's Bedrock client sends API version `2026-10-21`, which Bedrock accepts only for Claude 4.7+. Haiku 4.5 is therefore called through boto3 Converse (Strands), not that client.

**Build-phase spike.** Confirm that the Strands Bedrock model provider supports these models and settings on the path it uses. If it does not, wrap `AnthropicBedrockMantle` (Python SDK) as a custom Strands model provider.

### 1.3 Prompt structure (cache-friendly)

Ordering is `tools → system → messages`. The static parts are cached:
1. **Tools:** fixed JSON schemas, sorted and versioned.
2. **System:** role, policy rules (never promise without CONFIRMED; never ask for backend facts; reason plus alternative on refusals; WhatsApp brevity; language mirroring), the refusal and escalation phrasebook, and examples taken from the seed chats.
3. **Messages:** a compact context block (masked driver first name, shipment ref, facility, current status, ETA and confidence), then the conversation turns.

Volatile values (`now`, schedule version) go after the cache breakpoint. `prompt_version` is recorded on every trace.

### 1.4 Grounding post-check (R5)

A regex and parser pull every `HH:MM`, dock code (`D\d`), appointment ref and date from the draft reply. Each must appear in this turn's tool outputs, with the same dock/time pairing. Pending bookings must not be described as "confirmed". On failure, regenerate once with the violation stated. On a second failure, send the deterministic template.

### 1.5 Degraded mode (no LLM)

Templates in English and Hindi, filled from tool output:
- options list with numbered replies
- status
- request received
- escalation notice
- safety notice

They are triggered by model errors or timeouts, by a grounding failure twice, or by an ops-wide switch.

### 1.6 Language

Reply in the driver's language and script (Hindi, English, Hinglish). Times are 12-hour format with AM/PM for drivers (e.g. "1:00 PM") and 24-hour in the console. Dock codes are never translated.

## 2. Observability

### 2.1 Traces

One OpenTelemetry trace per inbound message: ingest → SQS → dispatcher → AgentCore Runtime (model spans with tokens and cache hits) → Gateway → tool Lambda → Data API. `trace_id` is stored on `chat_messages`, `booking_events`, escalations and the outbox, so the console links any case to its trace.

### 2.2 Business metrics (CloudWatch EMF, one per brief measure)

| Metric | Dimension | Alarm |
|---|---|---|
| `TimeToUsableOutcome` (s) | facility | p90 > 8 min for 15 min |
| `AutoResolved` / `Escalated` | facility, reason_code | escalation share > 40 % over 1 h |
| `DoubleBookingDetected` (post-run checker, every 5 min) | facility | **> 0 → page** |
| `OptionStaleAtHold` | facility | > 20 % over 1 h |
| `HandoffSchemaInvalid` | — | > 0 |
| `EscalationAckLatency` | facility, priority | p90 > 10 min |
| `PriorityPolicyViolation` | facility | > 0 |
| `ClarificationTurns` | — | mean > 1.5 / day |
| `GroundingFailure` | model, prompt_version | > 2 % over 1 h |
| `DegradedModeReplies` | reason | > 5 % over 15 min |
| `TokensIn/Out/CacheRead`, `CostPerConversation` | model | cost per conversation > $0.10 daily |
| `DriverWaitAfterReschedule` (gate-in → dock-in) | facility | Reported |

Platform alarms cover Lambda errors and throttles, SQS age and DLQ depth, Aurora ACU at max, Data API errors, AgentCore invocation errors, and Bedrock throttling.

### 2.3 Dashboards

1. **Ops live:** queues, SLAs, at-risk bookings per facility.
2. **Service health:** latency, errors, queues.
3. **Quality:** eval trend, grounding failures, judge scores.
4. **Cost:** tokens, cost per conversation, AWS spend by service.

## 3. Cost controls

| Control | Effect |
|---|---|
| Templates for status and confirmation turns | ~30–40 % of turns without an LLM call |
| Haiku for extraction (≈ 20× cheaper than Sonnet per token) | Most calls are cheap |
| Prompt caching of tools and system | Static ~4k tokens billed at the cache-read rate (about 0.1× the input price; confirm Sonnet 5 India rates) |
| Per-conversation caps: 12 LLM calls, 60k tokens | Bounded cost per case. Escalate past the cap |
| WAF and per-driver rate limits | No flood spend |
| AWS Budgets alarms at 80 % and 100 % of monthly plan; anomaly detection | Early warning |
| Data API (no proxy, NAT or endpoints), arm64, scale-to-zero in dev | ≈ $200+ / month fixed saving |

Cost model and totals: `02_architecture.md` §7. That is **≈ $440 / month** at 100 conversations a day and **≈ $2,060** at 10× (single baseline: `02_architecture.md` §7). Claude's Bedrock price must be confirmed in the console.
