# SetuHaul FDE Demo: Independent Evaluator Gap Analysis

**Stance:** a skeptical FDE evaluator reviewing release `4b9ac56` (evidence head `e3c5bbe`) on 26 Nov 2026. "Current state" uses the statuses in `SetuHaul_FDE_Evidence_Register.md`.

**Priority:**
- **P0** blocks any production use or misleads the audience if not disclosed;
- **P1** must close before a real-customer pilot;
- **P2** matters for scale;
- **P3** is improvement.

## 1. Gap table

| Area | Current state | Gap | Recommendation | Priority |
|---|---|---|---|---|
| Customer discovery | Strong written discovery from the client brief and data audit (`docs/1-frame/`); decision register; owner decisions recorded (OD-2, OD-3, T-series) | No interviews with real drivers, dispatchers or planners; the manual process on slide 2 is assumed | Run 5–8 structured interviews (2 per persona) and shadow one shift before a real pilot; record pain points against the brief | P1 |
| Customer feedback | The owner acted as customer: hands-on tests found the console Confirm 500, the duplicated read-back and 8 E2E defects (PR #23); native-speaker Hindi review (S4-R2-08) | No end-user feedback loop or satisfaction signal | Add a one-tap "was this helpful" for drivers and a planner feedback field; review weekly | P2 |
| Business problem validation | Problem framed with brief figures | No baseline (time to re-slot, coordinator touches, promises broken) and no measured business impact | Measure a 2-week baseline at one facility before switching on; define 3 KPIs with owners | P0 (for any value claim) |
| User experience | Templates in 3 languages, read-back before acting, Telegram buttons; console with role-hidden tabs | Duplicated read-back row in the console; "talk to a person" deferred; no driver app (cancelled by the owner); location sharing not exercised over real Telegram | Fix the duplicate display (label or hide the stored read-back); usability test with 3 planners | P2 |
| Product adoption | Not measurable yet (synthetic) | No adoption metrics | Define activation (driver linked), weekly active drivers, and the share of exceptions handled without a call | P2 |
| Data governance | Synthetic data class enforced at start-up; `SYN-` identities and +999 numbers; production refused | No real-data governance: DPDP consent, lawful basis, data-processing agreements with OpenRouter, Telegram and Meta | Data protection impact assessment; consent text in the link flow; processor list | P0 (before real data) |
| Multi-tenancy | Facility scoping via RLS and staff context; per-role database logins | Single tenant (one customer); no tenant id or tenant isolation test | If multi-customer: tenant column + RLS policy + isolation tests; else state "single-tenant by design" | P2 |
| Privacy | No message text, phone, chat id or name in traces; ids format-gated; redaction tests; ZDR route for the model | Message text is stored in the database (needed for operations), with no documented retention; console users can read conversations | Retention policy and purge job; access logging for conversation views | P1 |
| Data retention | 1-day Aurora backups; CloudWatch log retention set by the stack | No documented retention schedule for conversations, traces (LangSmith) and the usage ledger | Write a retention schedule per store; set the LangSmith project retention | P1 |
| Auditability | Strong: audit rows for hold, request and decision (smoke 2d); deployment evidence append-only; approvals by named console users; renewal attribution A–E | Audit is in the database only; no tamper-evident export | Periodic export of audit tables to immutable storage | P2 |
| Human override | Planner approval on every request; ops_lead moves; kill switch; escalation to a coordinator for safety and no-fit | No driver-initiated "talk to a person" (deferred); no automatic chase when a driver goes silent | Implement the deferred handover button; add a timed nudge plus escalation | P2 |
| AI risk management | The LLM only reads; schema with extra keys rejected; safety-word net; templates; budget caps; rules fallback | No formal AI risk register; no red-team on prompt injection beyond the golden adversarial set (7/7 in live replay) | AI risk register (misread ETA, missed safety word, language mismatch) with owner and control; quarterly red-team | P1 |
| Golden dataset quality | v1–v4 + held-out h1 (167 turns), hashed and versioned; adversarial, language and policy groups | Synthetic, partly model-generated (h1 by Sonnet); no real driver messages; no inter-annotator check on labels | Seed with anonymised real messages at pilot start; double-label 10% | P1 |
| Evaluation reliability | Deterministic hard gates; replay with 0 hard-gate violations; scripted suites run in tests | Paid evals are manual, not a CI gate. The last model-backed golden run was 18 Nov under prompt version p3. The deployed version is p5, but p4 and p5 changed only the unused phrasing prompt | Re-run golden v4 + h1 under p5 to confirm (expected cache hit, $0); add an approved weekly eval job with a pass threshold | P1 |
| Judge calibration | Accepted under owner rules against **model-generated** references; human calibration OUTSTANDING | No human scores (`human_scores_v1.json` empty); online judge configured but starved by the shared cap after deploy | Owner or native speaker scores the 30 cases; run `judge_calibration.py`; set the judge budget ≤ the shared cap minus the agent reserve | P1 |
| Observability | LangSmith thread per conversation; driver/shipment/facility ids; CloudWatch alarms; Ops views | 4 inbound runs without `thread_id`; no alerting on traces; no per-stage spans inside the agent turn; no dashboards for latency percentiles | Add `thread_id` to link-code and callback paths; span the agent turn; latency SLO alarm | P1 |
| Incident management | Kill switch; rollback table in `FULL_ACTIVATION_RUNBOOK.md` §11; DLQ alarm → SNS; drills done (DLQ, lease loss, approvals) | No on-call rota, severity definitions or post-incident template | Lightweight incident runbook: severities, who is paged, a comms template | P1 |
| Disaster recovery | Aurora backups (1 day); infrastructure as code; forward-only migrations | No restore drill; RPO/RTO not defined; single region | Define RPO/RTO; run one point-in-time restore drill; document region failure stance | P1 |
| Business continuity | Rules reader if the model fails; outbox if the sender sleeps; nothing lost while asleep | If AWS or Telegram is down, drivers have no channel; coordinators need a manual fallback procedure | Write the manual fallback procedure (phone tree) and test it once | P2 |
| Scalability | Designed for 10× the brief's spike; locally 5 users, 0 failures; integrity proof with 50 concurrent | No AWS load test; Aurora max 2 ACU; $0.25/day cap supports ≈ 23–63 bookings/day | AWS load test at the brief's spike (35 messages / 30 min) and at 10×; size the cap and ACU | P1 |
| FinOps | Daily cap, key limit, judge sub-cap, cost ledger, eval cost estimates, idle sleep | No actual AWS bill in evidence; no cost dashboard; judge budget misconfigured relative to the shared cap | Monthly cost review from Billing; budget alarm in AWS Budgets; fix the judge/shared-cap relation | P1 |
| Vendor lock-in | OpenRouter pinned to Bedrock; Bedrock provider kept as an option (`model_for()`); Telegram behind a provider interface; WhatsApp templates defined | Data API and Step Functions are AWS-specific; LangSmith exporter is custom (portable) | Accept AWS lock-in for the pilot; keep the provider interfaces; document an exit path | P3 |
| Model portability | One extraction schema; golden sets make swapping measurable | Only Haiku 4.5 evaluated on the current prompt; Bedrock direct not metered | Evaluate one alternative (e.g. Bedrock direct, or a smaller model) on h1 before contract renewal | P3 |
| Deployment safety | Strong: allow-listed refs; reproducible zip; synth checked without credentials; DB-side evidence guard; provisional authorization with expiry; weekly renewal; booking only under an owner approval reference | Renewal and approval steps are manual and owner-dependent; `/health` reports version 0.2.0 | Bump the version string from the release tag; calendar the weekly renewal | P2 |
| Operational ownership | The owner holds every approval; runbooks exist | Single owner is a single point of failure; no RACI | RACI for approve, deploy, renew, incident; a second approver | P1 |
| Production readiness | Synthetic pilot only, under 3 owner-approved exceptions:<ul><li>AURORA T33-P (PostgreSQL 17.9);</li><li>WORKLOAD_IDENTITY (no live drift detection);</li><li>OUTBOUND_DELIVERY (Meta deferred, test sink + Telegram allowlist)</li></ul> | Production needs PostgreSQL 18, drift detection, WhatsApp/Meta approval, real-data governance, latency within target, calibrated judge | Treat the exceptions as the production checklist; close each with evidence | P0 |

## 2. What the presentation must say because of these gaps

The deck already discloses these:
- slide 13: missed latency targets;
- slide 15: open issues;
- slide 16: production prerequisites;
- slide 9: judge calibration outstanding;
- slide 14: AWS bill not available.

The deck makes no measured business-impact claim. The playbook's "what not to claim" sections enforce this.

## 3. FDE capability assessment (master prompt §15)

| Capability | Evidence in this project | Verdict | Missing |
|---|---|---|---|
| Business understanding | Problem framed as correctness under burst contention, not chat volume; sub-problems assigned to AI vs code (`01_problem_definition_sheet.md` §1) | **Strong** | Validation with real users |
| Customer orientation | Owner-driven tests turned into fixes (Confirm 500, duplicate read-back, 8 E2E defects); native-speaker review; owner decisions recorded | **Adequate** | Real end-user feedback |
| Architecture | Serverless with the database as source of truth; cost-driven choices (no NAT, scale to zero); trade-offs written down | **Strong** | AWS load test |
| AI engineering | Forced-schema extraction, provider pinning with ZDR, metering, budget, fallback, golden sets with gates, prompt versioning | **Strong** | Model comparison on current prompt |
| Agent orchestration | Clear and defensible: deterministic orchestrator, LLM as reader, tools as database functions | **Strong**, if explained as deliberate | Do not oversell as "agentic" |
| AI quality | Golden + held-out + replay + judge with baseline and negative cases | **Strong design; partial evidence** | Human calibration; CI eval gate; re-run on prompt p5 |
| Reliability | Leases, fences, outbox, DLQ, idempotency, drills, integrity proof | **Strong** | DR drill; AWS-scale test |
| Security | Layered controls, per-role logins, RLS, allowlist, secrets handling, evidence guard | **Strong** | Formal threat model refresh for Telegram |
| Observability | Thread correlation, entity ids without PII, judge as child run | **Good** | Alerting, agent-turn spans, 4 uncorrelated runs |
| Economics | Real ledger, caps, unit-cost model with assumptions | **Good** | AWS bill, LangSmith cost, measured success rate |
| Ownership | Discovery → design → build → pilot → renewal, with evidence at each step | **Strong** | RACI and a second approver |

**Overall:** the project demonstrates FDE capability convincingly on engineering, AI design and governance. Its weakest areas are **customer validation and measured business value**. Both are honest consequences of a synthetic pilot and should be presented that way, not hidden.

## 4. Top 5 actions before presenting

1. Owner reads the AWS bill (Billing console) and pilot LLM spend (`model_budget_day`) for the cost slide. Otherwise keep "not available".
2. Save the `scripts/latency_report.py` output from the demo run into `docs/release/evidence/` so the post-release latency figures become committed evidence.
3. Capture fresh, anonymized screenshots: a Telegram chat (crop the chat header), the conversation view and a LangSmith thread.
4. Decide on the judge budget versus the shared cap (lower the judge budget, or raise the shared cap) before the demo, so a judge score can appear live.
5. Rehearse the fallback path once (recorded screens + local demo console).
