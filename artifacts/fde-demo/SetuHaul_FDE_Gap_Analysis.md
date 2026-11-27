# SetuHaul FDE Demo: Independent Evaluator Gap Analysis

**Stance:** a skeptical FDE evaluator reviewing release `4b9ac56` (evidence head `e3c5bbe`) on 26 Nov 2026. "Current state" uses the statuses in `SetuHaul_FDE_Evidence_Register.md`.

**Updated 27 Nov 2026** (fix plan, recovery releases, DEMO-04; Evidence Register Part D). Rows changed: Privacy,
Data retention, Evaluation reliability, Judge calibration, Observability, Scalability, FinOps, Deployment safety; new rows
for the 27 Nov items. §4 updated.

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
| Privacy | **Changed 27 Nov (owner):** on the synthetic pilot, traces carry full operational content (message text, model input/output, tool results, platform ids); only the driver's name and phone and every credential are removed (DEMO-04). Other environments keep the stricter modes. ZDR route for the model | Full-content traces would expose personal data with real drivers; message text stored in the database with no documented retention; console users can read conversations | Keep full content synthetic-only (enforced in code and CDK); before real data, return to `off`/`on` or a reviewed field list; retention policy and purge job; access logging | P1 (P0 before real data) |
| Data retention | 1-day Aurora backups; CloudWatch log retention set by the stack | No documented retention schedule for conversations, traces (LangSmith, now full content) and the usage ledger | Write a retention schedule per store; set the LangSmith project retention | P1 |
| Auditability | Strong: audit rows for hold, request and decision (smoke 2d); deployment evidence append-only; approvals by named console users; renewal attribution A–E | Audit is in the database only; no tamper-evident export | Periodic export of audit tables to immutable storage | P2 |
| Human override | Planner approval on every request; ops_lead moves; kill switch; escalation to a coordinator for safety and no-fit | No driver-initiated "talk to a person" (deferred); no automatic chase when a driver goes silent | Implement the deferred handover button; add a timed nudge plus escalation | P2 |
| AI risk management | The LLM only reads; schema with extra keys rejected; safety-word net; templates; budget caps; rules fallback | No formal AI risk register; no red-team on prompt injection beyond the golden adversarial set (7/7 in live replay) | AI risk register (misread ETA, missed safety word, language mismatch) with owner and control; quarterly red-team | P1 |
| Golden dataset quality | v1–v4 + held-out h1 (167 turns), hashed and versioned; adversarial, language and policy groups | Synthetic, partly model-generated (h1 by Sonnet); no real driver messages; no inter-annotator check on labels | Seed with anonymised real messages at pilot start; double-label 10% | P1 |
| Evaluation reliability | Deterministic hard gates; replay with 0 hard-gate violations; scripted suites run in tests | Paid evals are manual, not a CI gate. All model-backed evals were re-run on the deployed p5 on 26 Nov (done). Open: replay BB12 and AT02 fail on p3 and p5 (persistent, cause not established); AT02 may or may not depend on the replay date, which is unconfirmed; h1 option pick fell to 91.7% with a new p5 miss, BB05 | Investigate and reproduce BB12, AT02 and BB05, turn each into a regression case; add an approved weekly eval job with a pass threshold. **27 Nov:** the deployed prompt is p6 (schema v2: requested dates, RESCHEDULE); its paid golden re-run (≈ $0.28) was deferred by the owner, so model-backed extraction on p6 is unvalidated | P1 |
| Judge calibration | **Done (26 Nov), with limits.** Human calibration completed: owner-scored, one reviewer, not blind to the AI reference. The judge was within ±1 on 93.3% of 60 ratings (20 cases × 3 dimensions), average error 0.30 (always-5 baseline: 88.3%, 0.517). Accepted for advisory use only; judge scores are not an enforcement gate | One reviewer; not blind (owner scores match the AI reference on 53/60); 4 disagreements of 2+ points (J13, J14 judge false positives; J17, J20 planted defects partly missed); online pilot rows not calibrated; until DEMO-04 the online judge was always skipped (`budget:shared_daily_cap`: the shared $0.25/day cap left it no headroom). **27 Nov (owner):** shared cap $2.00, judge $0.75; live scoring verified 27 Nov during the 13:30 UTC smoke: 2 sampled smoke turns `SCORED` (agent log) | A second, blind reviewer (ideally a native Hindi/Hinglish speaker) for the 20 cases; run `pilot_calibration.py` on pilot rows once the judge scores live turns; keep the judge advisory, never a gate | P1 |
| Observability | LangSmith `thread_id` correlation; **DEMO-04:** one trace tree per Telegram message (inbound → agent_turn → outbound), full operational content on the synthetic pilot; CloudWatch alarms; Ops views. **27 Nov finding:** from the fix-plan release until DEMO-04 the export guard dropped every test-driver trace (their names matched ids) — fixed, with a regression test | 4 link-code/callback inbound runs without `thread_id`; offer-continuation and proactive notices are still their own roots; no alerting on traces or on guard drops; no latency dashboards | Alarm on the `blocked_leaks` counter; extend the tree to offer continuations; latency SLO alarm | P1 |
| Incident management | Kill switch; rollback table in `FULL_ACTIVATION_RUNBOOK.md` §11; DLQ alarm → SNS; drills done (DLQ, lease loss, approvals) | No on-call rota, severity definitions or post-incident template | Lightweight incident runbook: severities, who is paged, a comms template | P1 |
| Disaster recovery | Aurora backups (1 day); infrastructure as code; forward-only migrations | No restore drill; RPO/RTO not defined; single region | Define RPO/RTO; run one point-in-time restore drill; document region failure stance | P1 |
| Business continuity | Rules reader if the model fails; outbox if the sender sleeps; nothing lost while asleep | If AWS or Telegram is down, drivers have no channel; coordinators need a manual fallback procedure | Write the manual fallback procedure (phone tree) and test it once | P2 |
| Scalability | Designed for 10× the brief's spike; locally 5 users, 0 failures; integrity proof with 50 concurrent | No AWS load test; Aurora max 2 ACU; the $2.00/day cap (DEMO-04) alone supports ≈ 156–339 bookings/day (ESTIMATE, Cost Analysis §4.3); the $10 key limit binds first | AWS load test at the brief's spike (35 messages / 30 min) and at 10×; size the cap and ACU | P1 |
| FinOps | Daily cap ($2.00, checked activation input), $10 non-resetting key limit, judge sub-cap ($0.75), cost ledger, eval cost estimates, idle sleep | No actual AWS bill in evidence; no current pilot LLM spend or LangSmith bill in evidence; no cost dashboard; **the $10 lifetime key limit can be exhausted within about two days at $2/day** | Owner to raise or replace the key limit before sustained use; monthly cost review from Billing; budget alarm in AWS Budgets | P1 |
| Vendor lock-in | OpenRouter pinned to Bedrock; Bedrock provider kept as an option (`model_for()`); Telegram behind a provider interface; WhatsApp templates defined | Data API and Step Functions are AWS-specific; LangSmith exporter is custom (portable) | Accept AWS lock-in for the pilot; keep the provider interfaces; document an exit path | P3 |
| Model portability | One extraction schema; golden sets make swapping measurable | Only Haiku 4.5 evaluated on the current prompt; Bedrock direct not metered | Evaluate one alternative (e.g. Bedrock direct, or a smaller model) on h1 before contract renewal | P3 |
| Deployment safety | Strong: allow-listed refs; reproducible zip; synth checked without credentials; DB-side evidence guard; provisional authorization with expiry; weekly renewal; booking only under an owner approval reference | Renewal and approval steps are manual and owner-dependent; `/health` reports version 0.2.0. **27 Nov:** two releases stopped at renewal and were revoked by the safe-stop rule (an unattributed smoke booking; a smoke that died on expired AWS credentials); fixed by category D/X, a credential pre-check in the smoke and migration 0049 (one more recovery hop). Recovery depth is now the limit: a third failed recovery needs a full activation | Bump the version string; calendar the weekly renewal; run smokes only with a fresh sign-in; keep the safe-stop rule | P2 |
| Lifecycle and no-show (27 Nov) | Gate → dock → completed (operations/ops_lead); automatic no-show at each facility's grace with row locks against a racing arrival; dispatcher Rebook/cancellation; audited backfill (11 historical no-shows applied 27 Nov) | Arrival is recorded by staff only (no gate scanner or geofence); no-show messaging only in English templates checked | Integrate a gate event source; native review of any new driver-facing text | P2 |
| Workflow: dispatcher offers (W1, W2) | One slot per offer; driver accepts, declines or asks for another time; accepted offers always go to a planner | The target workflow sends the driver **several options**; not implemented. AUTO never applies to dispatcher offers (by design) | Multi-option offers (a short list the driver picks from, each re-checked at acceptance) if the business wants it; keep planner approval for dispatcher-chosen slots | P2 |
| Workflow: dispatcher time change (W3) | Dispatcher picks the new slot; a planner always decides; driver informed | The target has the **driver confirm** an option first; not implemented | Send the proposed time to the driver as an offer before the planner request | P2 |
| Workflow: cancellation (W4) | Staff request, planner decides | **Drivers cannot request a cancellation** of a confirmed booking (their "cancel" only releases a held slot) | A driver "cancel my booking" intent that files a cancellation request for a planner | P2 |
| Requested dates (27 Nov) | Facility time zone, ≤ 7 days, never silently another day; service re-checks | Hindi/Hinglish wording of the new date replies awaits native review; model-backed accuracy unmeasured (p6 eval deferred) | Native review; run the p6 golden eval when approved | P1 |
| Operational ownership | The owner holds every approval; runbooks exist | Single owner is a single point of failure; no RACI | RACI for approve, deploy, renew, incident; a second approver | P1 |
| Production readiness | Synthetic pilot only, under 3 owner-approved exceptions:<ul><li>AURORA T33-P (PostgreSQL 17.9);</li><li>WORKLOAD_IDENTITY (no live drift detection);</li><li>OUTBOUND_DELIVERY (Meta deferred, test sink + Telegram allowlist)</li></ul> | Production needs PostgreSQL 18, drift detection, WhatsApp/Meta approval, real-data governance, latency within target, multi-reviewer blind judge calibration | Treat the exceptions as the production checklist; close each with evidence | P0 |

## 2. What the presentation must say because of these gaps

The deck already discloses these:
- slide 9: synthetic datasets, p5 re-run results including the option-pick decline and persistent replay failures, and the limits of a one-reviewer, non-blind calibration;
- slide 11: link-code, callback, offer-continuation and proactive runs are still their own roots; full content is synthetic-pilot only;
- slide 15: p6 not model-validated; two releases stopped by their own gates on 27 Nov;
- slide 13: missed latency targets, and a preliminary, uncommitted after-sample;
- slide 14: pilot LLM, AWS and LangSmith bills not available; projections are not demonstrated capacity;
- slide 15: open issues;
- slide 16: production prerequisites.

The deck makes no measured business-impact claim. The playbook's "what not to claim" sections enforce this.

## 3. FDE capability assessment (master prompt §15)

| Capability | Evidence in this project | Verdict | Missing |
|---|---|---|---|
| Business understanding | Problem framed as correctness under burst contention, not chat volume; sub-problems assigned to AI vs code (`01_problem_definition_sheet.md` §1) | **Strong** | Validation with real users |
| Customer orientation | Owner-driven tests turned into fixes (Confirm 500, duplicate read-back, 8 E2E defects); native-speaker review; owner decisions recorded | **Adequate** | Real end-user feedback |
| Architecture | Serverless with the database as source of truth; cost-driven choices (no NAT, scale to zero); trade-offs written down | **Strong** | AWS load test |
| AI engineering | Forced-schema extraction, provider pinning with ZDR, metering, budget, fallback, golden sets with gates, prompt versioning | **Strong** | Model comparison on current prompt |
| Agent orchestration | Clear and defensible: deterministic orchestrator, LLM as reader, tools as database functions | **Strong**, if explained as deliberate | Do not oversell as "agentic" |
| AI quality | Golden + held-out + replay + judge with baseline and negative cases | **Strong design; partial evidence** | Blind, multi-reviewer calibration; CI eval gate; BB12, AT02, BB05 |
| Reliability | Leases, fences, outbox, DLQ, idempotency, drills, integrity proof | **Strong** | DR drill; AWS-scale test |
| Security | Layered controls, per-role logins, RLS, allowlist, secrets handling, evidence guard | **Strong** | Formal threat model refresh for Telegram |
| Observability | One trace tree per message (DEMO-04), full operational content on the synthetic pilot with name/phone/credentials removed, judge as child run | **Good** | Alerting (incl. guard drops), 4 uncorrelated runs |
| Economics | Real ledger, caps, unit-cost model with assumptions | **Good** | AWS bill, LangSmith cost, measured success rate |
| Ownership | Discovery → design → build → pilot → renewal, with evidence at each step | **Strong** | RACI and a second approver |

**Overall:** the project demonstrates FDE capability convincingly on engineering, AI design and governance. Its weakest areas are **customer validation and measured business value**. Both are honest consequences of a synthetic pilot and should be presented that way, not hidden.

## 4. Top 5 actions before presenting

1. Owner reads the AWS bill (Billing console) and pilot LLM spend (`model_budget_day`) for the cost slide. Otherwise keep "not available".
2. Save the `scripts/latency_report.py` output from the demo run into `docs/release/evidence/` so the post-release latency figures become committed evidence.
3. Capture fresh, anonymized screenshots: a Telegram chat (crop the chat header), the conversation view and a LangSmith thread.
4. After the DEMO-04 deploy, confirm one full trace tree and one sampled `llm_judge` child on the pilot before showing them live; otherwise present the offline judge results.
5. Rehearse the fallback path once (recorded screens + local demo console).
6. Check the OpenRouter key's remaining balance on the day (the $10 lifetime limit binds first at $2/day), and that no SAFETY escalation is open on the demo shipments.
