# SetuHaul FDE Demo: Presenter Playbook

**Deck:** `SetuHaul_FDE_Demo.pptx` (16 slides). **Talk:** about 24 minutes including a 6-minute live demo, then Q&A. **Evidence:** every number below is in `SetuHaul_FDE_Evidence_Register.md`. If you are tempted to say a number that is not there, don't.

**The one idea to carry through:** *the AI reads; code and the database decide; people approve.* Every slide is a variation on that sentence, and every hard question has an answer that goes back to it.

---

## 0. Timing plan

| # | Slide | Minutes | Cumulative |
|---|---|---|---|
| 1 | Project introduction | 0:30 | 0:30 |
| 2 | Business problem | 1:15 | 1:45 |
| 3 | Discovery and requirements | 1:00 | 2:45 |
| 4 | Solution workflow | 1:15 | 4:00 |
| 5 | Technical architecture | 1:30 | 5:30 |
| 6 | AI architecture | 1:30 | 7:00 |
| 7 | Tool calling | 1:15 | 8:15 |
| 8 | Guardrails | 1:30 | 9:45 |
| 9 | Golden dataset, evaluation, judge | 1:45 | 11:30 |
| 10 | **Live demo** | 6:00 | 17:30 |
| 11 | Observability | 1:15 | 18:45 |
| 12 | Testing and quality loop | 1:00 | 19:45 |
| 13 | Performance | 1:00 | 20:45 |
| 14 | Cost | 1:15 | 22:00 |
| 15 | Challenges and limitations | 1:00 | 23:00 |
| 16 | Business value and close | 1:00 | 24:00 |

**If you are running long:**
- cut slide 12 to 30 seconds (the loop picture only);
- cut slide 3 to the persona row;
- never cut slides 9, 13 or 15. They are where credibility is won.

**If the demo fails:** go to the fallback in the runbook §9 and keep to 6 minutes.

---

## 1. Opening introduction (say before slide 1 settles)

> "Thanks for having me. In the next twenty-odd minutes I want to show you how I took an ambiguous logistics problem, late trucks and contested dock slots, and turned it into a system that uses AI where AI is genuinely better, and refuses to let AI make the decisions that cost money when they're wrong. Everything you'll see runs on synthetic data on a pilot in AWS Sydney, and I'll be explicit about what's measured, what's projected and what isn't solved yet."

---

## Slide 1: SetuHaul Logistics, AI-Powered Logistics Operations Platform

1. **Purpose:** the audience understands the product in 15 seconds.
2. **Key message:** drivers report delays in their own words; SetuHaul offers only slots that work; a planner approves; no double bookings.
3. **Script:** "SetuHaul runs inbound freight into six warehouses with 32 dock doors. When a truck runs late, the driver sends a message, in English, Hindi or Hinglish. The system reads it, offers dock slots that are actually feasible, and a planner approves. The design rule is simple: the AI reads, the code and the database decide, people approve. This is a synthetic pilot; production is switched off by the system itself."
4. **Technical concepts:** none yet; just name the three roles (AI, code, people).
5. **Decisions and rationale:** positioning the AI as a reader, not an operator, is the decision everything else follows from.
6. **Business relevance:** the costly failure is a broken promise at the dock, not a slow chat reply.
7. **Transition:** "Let me show you why this problem isn't a chatbot problem."
8. **Likely questions:** "Is this live with real drivers?" No: synthetic drivers and data; two synthetic test drivers on real Telegram.
9. **Do not claim:** production use, real customers, measured savings.
10. **Evidence:** release `4b9ac56`; stack outputs (BookingMode, TelegramMode); `docs/1-frame/01_problem_definition_sheet.md`.
11. **Time:** 0:30.

## Slide 2: Delays are routine; broken promises are the real cost

1. **Purpose:** frame the problem as correctness under contention.
2. **Key message:** the scarce resource is 32 dock doors in a burst, not chat throughput.
3. **Script:** "The brief gave me these numbers: 280 to 360 loads a day, 32 dock doors, 15 to 25 exception messages on a normal day, but 20 to 35 inside half an hour when something goes wrong, and five coordinators on shift. And zero live GPS: an ETA is whatever the driver says. So the hard part isn't volume. It's that in a spike, several drivers want the same slot within seconds. A chatbot makes that worse. It will happily promise a slot another truck holds, because it can't see dock rules and it can't make a booking atomic. On the right is how I assume this works by hand today. I haven't observed it, and I've labelled it that way."
4. **Technical concepts:** burst contention; why throughput is not the design driver.
5. **Decisions:** spend engineering on concurrency control, idempotency and escalation, not on scale (Phase 1 "Consequence").
6. **Business relevance:** outcomes to track are promises kept, double bookings, coordinator touches and time to a new slot. **We have no production baseline yet.**
7. **Transition:** "Before writing code, I worked out who decides what."
8. **Likely questions:** "Where do these numbers come from?" The client brief; they are inputs, not measurements.
9. **Do not claim:** that the manual process was observed; any improvement percentage.
10. **Evidence:** `01_problem_definition_sheet.md` §1–3.
11. **Time:** 1:15.

## Slide 3: Discovery turned a vague ask into testable rules

1. **Purpose:** show the FDE method of decision rights first.
2. **Key message:** each requirement maps to a capability and to a test or live check.
3. **Script:** "Five personas, and for each the question 'what may this person decide?' A dispatcher can offer a load but decides nothing; that's a database role, not a UI convention. A planner approves requests. An ops lead can move a confirmed slot and pull the kill switch. Then every requirement got a capability and a proof. 'Never promise a conflicting slot' became a database constraint and a concurrency proof. 'People keep authority' became a Step Functions approval that waits for a human."
4. **Technical concepts:** decision rights encoded as database roles and functions; requirement-to-test traceability (`docs/review/TRACEABILITY.md`).
5. **Decisions:** a dedicated `dispatcher` role (migration 0037) rather than overloading operations; the requester can never decide their own request.
6. **Business relevance:** auditability: who decided what is a table row, not an email.
7. **Transition:** "Here's the workflow those rules produce."
8. **Likely questions:** "Did you interview users?" No real users. The owner acted as the customer and found real defects in hands-on tests; real interviews are a stated gap.
9. **Do not claim:** user research with real drivers.
10. **Evidence:** `setuhaul/api/security.py`; PR #15; `docs/review/TRACEABILITY.md`; smoke JSON checks 2a–2c, 4–7.
11. **Time:** 1:00.

## Slide 4: AI at the edges, code in the middle, people decide

1. **Purpose:** make the AI/deterministic boundary visible.
2. **Key message:** amber (AI) appears in one lane only.
3. **Script:** "Read the colours. Amber is AI; it appears only in the lane that reads the driver's message into a typed form. Blue is code and PostgreSQL: validate, read the time back, wait for an explicit YES, compute feasible slots, hold one and file a request. Green is a person, the planner. Notice that nothing moves until the driver confirms the read-back, and nothing is confirmed until the planner approves. The exceptions are along the bottom: safety words go straight to a coordinator, no fit means escalation with a hand-off note, and if the model is down or the budget is spent, a rules reader takes over."
4. **Technical concepts:** read-back and explicit confirmation; escalation as a durable record before the driver is promised a person.
5. **Decisions:** owner decision OD-3, templates for every reply, so no model writes to a driver.
6. **Business relevance:** each step is auditable, and a planner sees every request in one queue.
7. **Transition:** "Where does each of these boxes run?"
8. **Likely questions:** "Isn't the read-back annoying?" It costs one message and prevents booking on a misread ETA.
9. **Do not claim:** that the LLM decides the next step.
10. **Evidence:** `orchestrator.py` docstring; `docs/challenge/CLIENT_READOUT.md`.
11. **Time:** 1:15.

## Slide 5: Serverless on AWS Sydney; PostgreSQL is the source of truth

1. **Purpose:** architecture with the decisions behind it.
2. **Key message:** event-driven Lambdas around one database that owns state.
3. **Script:** "Messages arrive from Telegram at an API Lambda running FastAPI. It stores the message and its receipt in one transaction, then publishes to a FIFO queue grouped per driver, so one driver's messages are processed in order. The agent Lambda runs the orchestrator and talks to Aurora PostgreSQL through the Data API. That's deliberate: no NAT gateway, no connection proxy, and the database can pause to zero when the pilot sleeps. Approvals run in Step Functions, which simply waits for a person. Workers handle outbound, recovery and the approval relay. One honest note: the brief mentioned LangChain and Vercel. Neither is in this release. Tracing goes to LangSmith through a small exporter we wrote, and the console is served by the same API Lambda."
4. **Technical concepts:** outbox pattern; FIFO message groups; Data API; scale to zero; Step Functions task tokens.
5. **Decisions and trade-offs:** the Data API saves fixed cost but adds round-trip latency, which is the bottleneck on slide 13. PostgreSQL 17.9 is an owner-approved pilot exception because the free plan lacks 18.
6. **Business relevance:** the idle pilot costs about $7 a month (an estimate), so it can stay deployed between demos.
7. **Transition:** "The interesting part is inside the agent Lambda."
8. **Likely questions:**
   - "Why not containers?" Bursty, low-volume traffic.
   - "Why not RDS Proxy?" Cost and simplicity; the Data API avoids connection management.
9. **Do not claim:** LangChain, Vercel, multi-region, production-grade availability.
10. **Evidence:** `infra/setuhaul_stack.py`; stack outputs; `docs/release/09_LOW_COST_PILOT_PLAN.md`.
11. **Time:** 1:30.

## Slide 6: The orchestrator is code; the LLM is a reader, not an operator

1. **Purpose:** explain the AI architecture precisely, without overstating autonomy.
2. **Key message:** one forced tool call per turn; code chooses every tool.
3. **Script:** "Follow the arrows. The orchestrator, which is plain code, sends the message to Claude Haiku 4.5 with one instruction: fill this form, `record_driver_report`. It's a forced tool call, so the model can't chat back and can't choose another tool. The JSON comes back and is validated against a strict schema; unknown fields are rejected. Code checks safety words itself, regardless of what the model said. Then code decides the next tool: find options, hold, submit. The reply is a template. So why use an LLM at all? Because 'tyre burst near Neemrana, 90 minutes late' and its Hinglish variants are unbounded language. On our held-out set the model gets intent right 98% of the time against 61% for the rules reader. And why not a free agent? Because I can't let a probabilistic component book a dock."
4. **Technical concepts:** forced tool use as structured output; schema validation; model routing pinned to one provider route with zero data retention; ≤ 1 retry; fallback to rules, not to another model; budget reserved before each call.
5. **Decisions:** pinning OpenRouter to the Bedrock route with fallbacks off was chosen for data retention and predictability over availability. The availability gap is covered by the rules reader.
6. **Business relevance:** the expensive mistakes are impossible by construction, not unlikely.
7. **Transition:** "So what does a tool call look like in practice?"
8. **Likely questions:**
   - "Is this really an agent?" It is an agentic workflow with a deterministic orchestrator. I chose that deliberately; I don't claim autonomy.
   - "Context management?" Per-turn facts (shown options, pending read-back) are passed in a user block. There is no long-term memory beyond the conversation's stored facts.
9. **Do not claim:** that the LLM selects tools, writes replies, or runs a multi-agent loop.
10. **Evidence:** `orchestrator.py`, `extraction.py`, `prompts.py` (p5), `openrouter.py`, `docs/3-build/README.md`; `golden_h1_*`.
11. **Time:** 1:30.

## Slide 7: Tools are database functions behind a typed contract

1. **Purpose:** show the AI touching real business systems safely.
2. **Key message:** success and refusal are decided by code, never by the model.
3. **Script:** "There are ten agent tools: read tools, write tools that are PostgreSQL functions, and one hand-off. Here's the success path for 'haan ji, kar do'. The model reports intent BOOK with an explicit confirmation. Code checks that this exact option was read back and is the driver's latest word. `hold_option` takes a per-dock lock, and an exclusion constraint makes an overlapping claim unstorable. The request goes to PENDING_APPROVAL, and the template says plainly that it is not confirmed. On the right are three refusals, two of them run live on the pilot: the kill switch, where booking is refused and escalated, and an agent identity trying to approve, which is refused. The third is a database rule: a declined offer can never be booked."
4. **Technical concepts:** definer functions; per-role logins; idempotency (dedup ids, receipts, idempotency keys); fenced turns.
5. **Decisions:** tools as database functions, so the same rule holds for the agent, the console and an administrator.
6. **Business relevance:** no "the AI did it" incidents: every action has a code path and an audit row.
7. **Transition:** "Tools are one layer. Here are all seven."
8. **Likely questions:**
   - "What if the model passes wrong arguments?" It doesn't pass arguments to tools; code builds them from validated fields.
   - "Retries?" Inbound is de-duplicated; a redelivered turn is fenced.
9. **Do not claim:** that the LLM performs tool selection.
10. **Evidence:** smoke `option_d_smoke_20261126T140741Z.json` checks 4–6; migration 0040; `INTEGRITY_PROOF.md`.
11. **Time:** 1:15.

## Slide 8: Seven layers of guardrails; prompts are not one of the controls

1. **Purpose:** show defence in depth, and why prompts are not security.
2. **Key message:** every rule that matters is in code or PostgreSQL.
3. **Script:** "Seven layers:
   - input: a webhook secret, an allowlist of two test chats, and one-time link codes;
   - prompt and context: the prompt says the driver's text is data, not instructions, but nothing depends on the model obeying that;
   - output validation: a forced schema, a safety-word net in code, and templates;
   - tool execution: code picks the tool, and the driver must say YES;
   - business policy: feasibility and approval rules;
   - the database: roles, row-level security per facility, and the constraint;
   - people and operations: planner approval, the kill switch, and authorization that expires.

   The three questions on the right are the ones I get most. If the model ignores instructions, it can only fill a form badly, and that reaches a read-back and a planner. If it returns garbage, it's rejected and the rules reader takes over. And it cannot 'attempt' an unauthorised operation, because it has no tools."
4. **Technical concepts:** defence in depth; RLS; separation of duties; fail-closed allowlist; environment separation (production refused, synthetic data class enforced).
5. **Decisions:** the kill switch and provisional authorization are database state, so a misconfigured Lambda cannot bypass them.
6. **Business relevance:** safe to pilot without trusting the model.
7. **Transition:** "Controls stop bad actions. But how good is the model at its one job?"
8. **Likely questions:**
   - "Prompt injection?" Treated as data. The worst case is a wrong field, caught downstream.
   - "PII?" Kept in PostgreSQL; ids only in traces.
9. **Do not claim:** that prompt wording provides security; tenant isolation (single tenant).
10. **Evidence:** `security.py`, `extraction.py`, `telegram.py`, migrations 0026/0030/0032/0036/0040.
11. **Time:** 1:30.

## Slide 9: AI quality is measured against versioned references, not demos

1. **Purpose:** the AI quality engineering story.
2. **Key message:** golden sets drive changes; the judge is advisory and not yet human-calibrated.
3. **Script:** "The pipeline runs left to right: a versioned, hashed golden dataset; the agent run; comparison to references; deterministic scoring with hard gates; the judge, which is advisory; acceptance rules; a report; and every defect becomes a regression case. The chart is the held-out set: 167 driver turns generated separately and never used for tuning. The model gets intent right 98% of the time against 61% for the rules reader, and every field right on 95% of turns. Whole-conversation replay passed 61 of 64 scenarios with zero hard-gate violations, meaning no unsafe booking even when the model was wrong. Now the judge. It scores reply clarity, tone and language. It agrees within one point with reference scores 95% of the time, but those references are model-generated, and a dummy judge that always says 5 scores 88%. So we added five planted defects: our judge catches all five, the dummy catches none. The first prompt version produced unparseable output 18 times out of 30, and we rejected it. Human calibration is still outstanding, and I won't call the judge accurate until it's done."
4. **Technical concepts:**
   - precision vs recall for ETA: we prefer missing a time to inventing one;
   - hard gates vs scores;
   - held-out discipline;
   - judge baselines and negative cases;
   - calibration;
   - sampling and budget.
5. **Decisions:** deterministic checks for everything business-critical; the judge only for language quality; never let the judge gate anything.
6. **Business relevance:** quality is a number you can regress against, not a demo impression.
7. **Transition:** "Enough slides. Let me show you."
8. **Likely questions:**
   - "Why Haiku as judge of Haiku?" Same pinned route and no new data path. Self-preference bias is a real risk, which is exactly why human calibration is the open item.
   - "Is the eval in CI?" Scripted suites are; paid runs are manual with approval.
9. **Do not claim:** judge accuracy; human agreement; an eval gate in CI; production quality.
10. **Evidence:** `golden_h1_*`, `replay_v4_live_*`, `g1_langsmith_eval_*`, `judge_20261125_172110.json`, `LLM_JUDGE.md`.
11. **Time:** 1:45.

## Slide 10: Live demo (narration in §2 below)

1. **Purpose:** prove the slides with a real run.
2. **Key message:** two synthetic drivers, one planner, one correlated trace.
3. **Script:** see §2.
4. **Technical concepts:** see the runbook table (persona, action, AI, tool, control, outcome).
5. **Decisions:** steps 2–4 deliberately use no AI.
6. **Business relevance:** a late truck is re-slotted without a phone call, and a planner still decides.
7. **Transition:** "What you just saw, as a trace."
8. **Likely questions:** "What did that cost?" A handful of extractions at fractions of a cent each, within a $0.25 daily cap.
9. **Do not claim:** that the judge will score live (it may be skipped; see the gap analysis); WhatsApp.
10. **Evidence:** `SetuHaul_FDE_Live_Demo_Runbook.md`; `TELEGRAM_TEST_BOT_VALIDATION_REPORT.md` journeys A and C.
11. **Time:** 6:00.

## Slide 11: One LangSmith thread per conversation, no personal data

1. **Purpose:** observability as an engineering tool.
2. **Key message:** traces answer "what happened to this booking?"
3. **Script:** "Every component, from the webhook to the agent turn, the judge and the outbound send, shares one opaque thread id, so LangSmith shows a whole conversation. Inside the agent turn you see the extraction with the model name, tokens, cost and latency, and each tool span with its result code. The judge is a child of the turn it scored. What's not there is just as important: no message text, no phone number, no chat id. Internal ids pass a format gate. To investigate a failed booking you filter by driver id, read the turn's outcome, open the failing tool span, and confirm in the database's audit trail. Gaps: four inbound runs, link codes and button callbacks, don't carry the thread id yet, and we alert from CloudWatch, not from traces."
4. **Technical concepts:** trace, run, parent/child; logs vs metrics vs traces; correlation ids; PII redaction at the export boundary.
5. **Decisions:** a custom exporter so we control exactly what leaves the process.
6. **Business relevance:** support can answer "why was my slot refused?" from one link.
7. **Transition:** "How do we stop changes from breaking any of this?"
8. **Likely questions:**
   - "Why LangSmith?" Thread view, feedback on runs, datasets and experiments in one place.
   - "Cost of tracing?" Measured overhead was 13 ms per turn locally; the LangSmith bill is not known.
9. **Do not claim:** complete observability; trace-based alerting.
10. **Evidence:** `tracing.py`, `identifiers.py`, `redaction.py`, commit `ff4076d`, PR #17, `g1_perf_20261123_1500.json`.
11. **Time:** 1:15.

## Slide 12: Tests prove the rules; evaluations measure the model

1. **Purpose:** quality engineering beyond unit tests.
2. **Key message:** 2,080 tests gate every release; pilot findings become regression cases.
3. **Script:** "2,080 tests pass in CI on the exact release, on PostgreSQL 17.9 like the pilot, plus 678 through a Data API emulator. The activation workflow reruns the full suite before it may switch booking on. Concurrency: twenty drivers race for one slot and one wins. Live: an eleven-check smoke test on the pilot, plus drills for dead-letter queues, worker crashes and approvals. On the right is the loop: a trace shows a failure, it becomes a golden or regression case, we evaluate, fix, rerun everything, and release through a gated workflow. Tests check that code does what we specified; evals check how well the model reads people."
4. **Technical concepts:** testing nondeterminism (scripted model for logic, golden sets for the model); emulated vs real backend; drills.
5. **Decisions:** run CI on the pilot's database version, not the newest.
6. **Business relevance:** no silent regressions in the rules that protect bookings.
7. **Transition:** "Correct is necessary. Is it fast enough?"
8. **Likely questions:** "Coverage percentage?" Not measured or reported. I'd rather cite the behaviour tests and drills.
9. **Do not claim:** a coverage figure; that CI proves operational readiness.
10. **Evidence:** CI 45836948268; activate-booking 45833537353 / 45836951650; `INTEGRITY_PROOF.md`; `drill_*`.
11. **Time:** 1:00.

## Slide 13: End to end 4× faster; the agent turn is now the bottleneck

1. **Purpose:** honest performance.
2. **Key message:** a big win, two narrow misses, a clear next bottleneck.
3. **Script:** "This morning, a reply waited up to a minute for the sender's one-minute tick: 58.6 seconds at p95 from reply to outbound. We made delivery event-driven and kept the tick as a fallback; that's now 1.3 seconds. End to end went from 53.7 to 13.1 seconds at the median. But the targets were under 15 seconds at p95, and we measured 15.0, and under 2 seconds for send, and we measured about 2.2. Both missed, narrowly, and on six exchanges, so p95 is basically the worst case. The model itself takes about 2 seconds. The agent turn takes 8.5 to 11 on the pilot against 2 locally, so the next job is to instrument that turn: Data API round trips and cold starts are my hypotheses, not findings."
4. **Technical concepts:** model latency vs workflow latency; p50/p95/p99 and sample size; cold start; Aurora resume (14.6 s).
5. **Decisions:** keep the tick as a fallback; measure before optimising the agent turn.
6. **Business relevance:** 13 seconds is acceptable for a driver at a toll plaza; a minute was not.
7. **Transition:** "And what does it cost?"
8. **Likely questions:** "Why not p99?" Six samples; I won't invent it.
9. **Do not claim:** that targets were met; that the hypotheses are findings; that the after-numbers are committed evidence. They were reported from the 26 Nov run; save the report (runbook §10).
10. **Evidence:** commit `ff4076d` (before); `scripts/latency_report.py` output (after, reported); `PHASE_G_PERFORMANCE_REPORT.md`; `10_EXPRESS_AURORA.md`.
11. **Time:** 1:00.

## Slide 14: Cents per booking, capped by design

1. **Purpose:** FinOps fluency.
2. **Key message:** actuals are tiny and traceable; scale numbers are labelled projections; spend is capped by construction.
3. **Script:** "Actuals first. All model spend for development and evaluation up to 18 November was about $3.16, from the key's own counter. The two judge runs cost 21 cents, from our ledger. One extraction costs about a third of a cent at most. I don't have the AWS bill in the repo, so I'm not quoting one; the estimate for the idle pilot is about $7 a month. The chart is a projection with the assumptions underneath. At a hundred bookings a month, fixed AWS cost dominates. At a hundred thousand, the LLM dominates, at about 1.3 cents a booking. Controls: a $0.25 daily cap reserved in PostgreSQL before every call, a $3 monthly key limit, and a judge that can't starve the agent. When the budget runs out, the rules reader takes over and booking keeps working."
4. **Technical concepts:** reservation-then-settle metering; fixed vs variable vs usage-based cost; why growth is not linear.
5. **Decisions:**
   - templates instead of a phrasing model, so there is no second model call per turn;
   - a rules fallback instead of a second model;
   - judge sampling at 20%.
6. **Business relevance:** AI cost per booking is cents; the real cost driver at scale is how many turns need the model.
7. **Transition:** "Here's what was hard, and what isn't solved."
8. **Likely questions:** "Isn't $0.25/day tiny?" Deliberately so. It supports about 23–63 bookings a day; scale is an explicit owner decision.
9. **Do not claim:** an AWS figure as billed; projections as actuals; LangSmith cost.
10. **Evidence:** `COSTS.md`; `judge_*.json`; `golden_h1_model_*`; `09_LOW_COST_PILOT_PLAN.md`; `SetuHaul_FDE_Cost_Analysis.md`.
11. **Time:** 1:15.

## Slide 15: Four hard problems, and what is still not solved

1. **Purpose:** engineering maturity.
2. **Key message:** fixes came from evidence; open issues are listed.
3. **Script (pick two of the four in depth):**
   - Double booking: "Application-level checks race. I let PostgreSQL decide, with a per-dock lock and an exclusion constraint. Twenty concurrent requests, one winner, zero overlaps."
   - Latency: "Replies waited for a timer. Now they're event-driven, with the timer as a fallback."
   - Judge trust: "95% agreement looked great until an always-5 dummy scored 88%. Planted defects became an acceptance rule."
   - Activation safety: "Evidence goes stale, and humans forget. The database refuses to book without current evidence, authorization expires, and every approval is re-attributed weekly."

   Then read the open issues without apologising.
4. **Technical concepts:** problem → investigation → decision → implementation → outcome.
5. **Decisions:** as above.
6. **Business relevance:** the open issues are the production plan.
7. **Transition:** "So what does all this prove?"
8. **Likely questions:** "Why is the read-back shown twice?" The console shows the stored record the driver's YES is checked against, plus the reply; only one was sent. It's a display defect.
9. **Do not claim:** that the open issues are fixed.
10. **Evidence:** `INTEGRITY_PROOF.md`, PR #33, `LLM_JUDGE.md` §7b, PRs #5/#6/#8/#35, `renewal_gates.json`.
11. **Time:** 1:00.

## Slide 16: What it proves, and what production still needs

1. **Purpose:** close with measured vs expected value.
2. **Key message:** Business problem → AI engineering → reliable execution → measurable business value.
3. **Script:** "What's measured, on synthetic data: no double bookings under concurrency, zero unsafe actions in 64 replayed conversations, the model reading drivers far better than rules, and delivery 45 times faster than this morning. What's expected but not measured: fewer coordinator calls and faster re-slotting. That needs a real baseline, and I'd start a real pilot by measuring one. And here's what stands between this and production: real-data governance and WhatsApp approval, human calibration of the judge, PostgreSQL 18 with drift detection and a recovery drill, and latency inside target. I don't just build AI agents. I engineer business solutions that use AI responsibly, operate reliably, deliver measurable value and can scale economically. Thank you. Happy to take questions."
4. **Technical concepts:** none new.
5. **Decisions:** separate measured from expected on the slide itself.
6. **Business relevance:** a clear, honest go-to-production list.
7. **Transition:** Q&A.
8. **Likely questions:** "What would you do first with a real customer?" Baseline KPIs at one facility, interviews, human judge calibration.
9. **Do not claim:** ROI, savings or adoption.
10. **Evidence:** Evidence Register Parts B4–B7.
11. **Time:** 1:00.

---

## 2. Live demo narration (6 minutes)

**Steps 1–4 use no AI. Say so: it shows the AI is used only where language must be read.**

| Step | Say |
|---|---|
| 1 (0:30) | "First, the safety envelope. Booking is authorized by a database record tied to an owner approval reference, and the kill switch is disengaged. If either changes, the system refuses to book." |
| 2 (0:45) | "I'm the dispatcher. I offer this dock slot to our synthetic test driver on Telegram. Watch the phone." *Buttons arrive.* "That's event-driven delivery: seconds, not a minute." |
| 3 (0:30) | "The driver taps Accept. Important: that's a request, not a booking. Acceptance is never confirmation." |
| 4 (0:45) | "Now I'm the planner. One pending request. Approve." *Phone:* "'Your dock booking is confirmed by the warehouse.' And the conversation view updates on its own every three seconds." |
| 5 (1:30) | "Now the AI part. The driver is stuck in traffic and writes in Hinglish." *Type.* "The model has read it into a typed form: intent delay, duration 45 minutes, language Hinglish. But we don't act yet; we read the time back." *Reply 'haan'.* "Now code, not the model, computes feasible slots." *Pick 2.* "Held, requested, and the reply says plainly it is not confirmed yet." |
| 6 (1:00) | "As an engineer: here's that conversation in LangSmith. One thread: inbound, the agent turn with the extraction (model, tokens, cost, latency), each tool, and the outbound send. No message text, no phone, no chat id." |
| 7 (0:45) | "Last: a stale action. Ten minutes ago the dispatcher sent driver two an offer. It has expired. Driver two taps Accept." *Phone:* "'This offer has expired. Your dispatcher can send a new one.' No appointment. That refusal isn't the model's opinion; it's a rule in the system." |

**If something fails:** "This is why we have fallbacks." Switch to the recorded run (runbook §9) without apologising, and keep the persona → AI → tool → control → outcome narration.

---

## 3. Closing statement (if Q&A is cut)

> "The pattern I'd take to any customer is the one you saw: find the decision that's expensive to get wrong, keep it in code and with people, and use AI where language is genuinely unbounded. Then measure it, trace it, cap its cost, and be honest about what's still open."

---

## 4. Short explanations for non-technical audience members

| Term | Plain explanation |
|---|---|
| LLM | Software that reads text the way a person would. Here it only reads drivers' messages |
| Orchestrator | The fixed checklist the system follows for every message |
| Tool | A specific action the system can take, like "hold this slot". Each one is guarded by rules |
| Golden dataset | An answer key: real-looking messages with the correct reading, used to grade the AI |
| LLM-as-a-judge | A second AI that grades the tone and clarity of replies. It gives advice only and decides nothing |
| Trace | A step-by-step receipt of what happened to one conversation |
| Kill switch | An emergency stop that makes the database refuse new bookings |
| p95 | 95 out of 100 requests were at least this fast |

---

## 5. How to explain SetuHaul to different audiences

**Business leadership (60 s).**
> "When a truck is late, today a coordinator juggles calls and can promise the same dock twice. SetuHaul lets drivers report delays in their own words, offers only slots that really work, and keeps your planners in charge of every confirmation. It can't double-book, and its AI spend is capped at cents per booking. It's a tested pilot on synthetic data; the next step is a measured pilot at one warehouse."

**CTO / architect (90 s).**
> "Serverless on AWS: API and agent Lambdas, a FIFO queue per driver, Step Functions for human approval, and Aurora PostgreSQL as the single source of truth. Commitments are database functions under locks and an exclusion constraint, so correctness doesn't depend on application code paths. Release safety is in the database too: an evidence guard, expiring authorization and weekly renewal. The trade-off is Data API latency, which is our current bottleneck."

**AI engineer (90 s).**
> "One forced tool call per turn into a strict pydantic schema, pinned to Haiku 4.5 on a zero-data-retention route, with no fallback model; the rules reader is the fallback. The prompt is versioned and every change needs a golden run. Held-out intent accuracy is 98% vs 61% for rules. Replay has zero hard-gate violations. The judge is advisory, accepted against an always-5 baseline and planted defects, and is not human-calibrated yet."

**Software engineering interviewer (90 s).**
> "The hard problems were concurrency and delivery semantics:
> - an outbox written in the same transaction as the inbound message;
> - FIFO dedup;
> - a processing lease with a heartbeat and a fence, so a worker that lost its lease commits nothing;
> - idempotency keys;
> - a per-dock lock plus an EXCLUDE constraint, proven with 20 and 50 concurrent requests.
>
> 2,080 tests run in CI on the pilot's PostgreSQL version, plus a live 11-check smoke test."

**FDE evaluator (90 s).**
> "I started from decision rights, not models. I wrote down what each persona may decide, mapped every requirement to a capability and a proof, and kept a decision register. I shipped to a real cloud pilot under owner approvals and measured cost and latency, including the targets we missed. I can also tell you exactly what's not done: user research, a business baseline, judge calibration and production governance."
