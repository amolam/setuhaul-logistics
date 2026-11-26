# SetuHaul FDE Demo: Technical and Leadership Q&A Preparation

**90 questions** in 12 groups, plus 6 rapid-fire hard questions at the end. Each has:
- **S**: a short answer (about 30 s);
- **D**: a detailed answer (1–2 min);
- **E**: implementation evidence;
- **F**: the likely follow-up challenge, with a pointer to the answer.

Release `4b9ac56` (evidence head `e3c5bbe`); all data synthetic. Numbers come from `SetuHaul_FDE_Evidence_Register.md`. When something is not implemented, the answer says so and gives the recommended approach.

**Rules for answering:**
- Say "measured", "projected" or "not available" explicitly.
- Never improve a number under pressure.
- When unsure, say "I'd check the evidence register" and move on.

---

## A. Business and product

**A1. Why this problem?**
- **S:** Late trucks are routine; the expensive failure is a broken dock promise during a burst. That is a correctness problem where AI helps at the edges and must be kept away from the decision.
- **D:** The brief describes 280–360 loads a day into 32 dock doors, with 20–35 exception messages in a 30-minute spike and five coordinators. There is no live GPS, so an ETA is a declaration. The risk is two drivers promised one dock, and coordinators overwhelmed exactly when it matters. It is a good FDE problem because it mixes messy language (AI) with hard constraints (code) and human authority (planners).
- **E:** `docs/1-frame/01_problem_definition_sheet.md` §1–2.
- **F:** "Is it big enough to matter?" → I don't have a value baseline; that's the first thing a real pilot measures (A5).

**A2. How was it identified?**
- **S:** From the client brief and its data package, followed by a 30-check data audit and a framing of five sub-problems.
- **D:** I audited the snapshot (`scripts/frame/run_audit.py`, 30 checks) and separated facts, estimates, assumptions and unknowns. Then I split the work: replies, re-scheduling, warehouse constraints, truck constraints and simultaneous requests, each with a different owner (LLM, code, database, people).
- **E:** `docs/1-frame/audit_results.md`; `01_problem_definition_sheet.md` §4.
- **F:** "Did you talk to users?" → No real users. The owner acted as the customer, and real interviews are a P1 gap.

**A3. Who are the users?**
- **S:** Drivers, dispatchers, warehouse planners, operations coordinators and ops leads.
- **D:** Their decision rights:
  - Drivers report and choose.
  - Dispatchers offer and reassign loads but decide nothing.
  - Planners approve or reject requests.
  - Operations work escalations.
  - Ops leads move confirmed appointments and hold the kill switch.

  These are Cognito groups and database roles, not UI conventions.
- **E:** `setuhaul/api/security.py` `STAFF_ROLES`; migration 0037; PR #15.
- **F:** "Why can't dispatchers approve?" → Separation of duties: the requester never decides (migration 0036).

**A4. What business value is created?**
- **S:** Fewer coordinator calls per delay, faster re-slotting, and correct slot allocation under contention. Only the last has evidence, from synthetic concurrency tests; the first two are expected but not measured.
- **D:**
  - Measured (synthetic data): competing requests for one slot gave one winner and 0 overlapping claims (20 concurrent in the local proof, 50 in the suite, plus live smoke check 4); 0 hard-gate violations in 64 live replay scenarios.
  - Preliminary (reported, uncommitted, 6 exchanges): reply → outbound p95 1.3 s against 58.6 s before.
  - Expected: coordinator time saved and faster rebooking. Those need a baseline from a real facility.
- **E:** `INTEGRITY_PROOF.md`; `replay_v4_live_*.json`; commit `ff4076d`.
- **F:** "Give me an ROI number." → I won't invent one; here is how I'd measure it (A5).

**A5. What is measurable?**
- **S:** Promises kept, double bookings, coordinator touches per exception, time from delay report to a new confirmed slot, and the share of exceptions handled without a call.
- **D:** The system already records every hold, request, decision and escalation with timestamps (`booking_events`, `approval_requests`, `escalations`, `chat_messages`). A two-week baseline at one facility, measured the old way, then gives a before/after.
- **E:** Database tables (Technical Appendix §8).
- **F:** "Who owns these KPIs?" → The ops lead per facility. That's in the gap analysis as a RACI action.

**A6. How would customer feedback influence the roadmap?**
- **S:** Through the same loop as defects: feedback → a case in the golden or regression set → a change → a gated release.
- **D:** The owner's hands-on tests already worked this way: the console Confirm 500, the duplicated read-back and 8 E2E defects became fixes with regression tests (PR #23, v0.2.1). For real users, I'd add a one-tap "was this helpful" for drivers and a planner feedback field, and review them weekly.
- **E:** `docs/release/15_RELEASE_NOTES.md`; PR #23.
- **F:** "What if feedback conflicts with policy?" → Policy wins in code, and the request becomes a policy question for the owner (L6).

## B. Architecture

**B1. Why this architecture?**
- **S:** Bursty, low volume and correctness-critical, so serverless around one PostgreSQL that owns all commitments.
- **D:** The pieces:
  - Lambdas for the API, the agent and the workers;
  - SQS FIFO grouped per driver for ordered, de-duplicated turns;
  - Step Functions to wait for a person;
  - Aurora PostgreSQL via the Data API, so there is no NAT or proxy and the database scales to zero.

  The database enforces every commitment through definer functions, locks and an exclusion constraint.
- **E:** `infra/setuhaul_stack.py`; `docs/release/09_LOW_COST_PILOT_PLAN.md`.
- **F:** "What breaks first at scale?" → The daily LLM cap, then Aurora ACU and the agent-turn latency (J4).

**B2. Why FastAPI?**
- **S:** Typed request models with pydantic, async endpoints, and it runs unchanged locally and in Lambda via Mangum.
- **D:** The same app serves the webhook, the console API and the console's static files. Pydantic models with extra fields forbidden are the first input guardrail.
- **E:** `requirements.txt` (fastapi, pydantic); `setuhaul/api/app.py`; `requirements-aws.txt` (mangum).
- **F:** "Cold starts?" → Orchestrator import is about 0.73 s locally; the pilot turn latency is under investigation (J2).

**B3. Why PostgreSQL?**
- **S:** Because correctness lives in transactions, row locks, row-level security and exclusion constraints. The exclusion constraint makes the database refuse an overlapping claim on the same dock, whoever writes it; the concurrency tests exercise exactly that.
- **D:** `dock_occupancy` has an EXCLUDE constraint (`ex_one_truck_per_dock`). Holds take a per-dock lock. Approvals, evidence guard, kill switch and provisional authorization are tables plus definer functions. The usage ledger reserves model budget atomically. 46 forward-only migrations.
- **E:** `migrations/0001_core_schema.sql`, `0023`, `0026`, `0030`, `0032`.
- **F:** "Isn't putting logic in the DB hard to test?" → 2,080 tests run against PostgreSQL 17.9 in CI, plus a Data API emulator path.

**B4. Why LangChain?**
- **S:** It isn't used. That's a correction to the brief.
- **D:** The orchestration is a fixed pipeline in plain Python, and the model call is a single forced tool call through our OpenRouter client. LangChain would add abstraction and dependencies without adding control. Tracing goes to LangSmith through a small exporter we wrote, so we control exactly what leaves the process.
- **E:** `requirements*.txt`; `setuhaul/agent/openrouter.py`; `setuhaul/observability/langsmith.py`.
- **F:** "Would you use it for a bigger agent?" → Possibly, for multi-step tool loops, but the redaction and metering boundaries would stay ours.

**B5. Why an orchestrator?**
- **S:** Someone has to decide the next step, and it must be deterministic and auditable. So it's code.
- **D:** The orchestrator takes a lease on the turn, gets context, calls the model once, validates, applies safety and read-back rules, picks the tool, writes the reply from a template and stores it under a fence. That gives one path to test, one place to trace and no model-driven control flow.
- **E:** `setuhaul/agent/orchestrator.py`.
- **F:** "Isn't that just a workflow?" → Yes, deliberately: an agentic workflow with an LLM at the edge (B6).

**B6. Why agents?**
- **S:** Only as far as natural language needs it. The "agent" reads; it does not act on its own judgement.
- **D:** A pure workflow can't read "tyre burst near Neemrana, 90 min late" or Hinglish variants. A free agent can't be trusted to book. On synthetic held-out data, on the deployed prompt p5, the model gets intent right on 167 of 167 turns against 61% for rules, and that is why the LLM is there.
- **E:** `golden_h1_*`.
- **F:** "When would you give it more autonomy?" → For low-risk, reversible actions, such as drafting a coordinator hand-off note, with a judge and human review.

**B7. What would you redesign?**
- **S:** Three things:
  - put per-stage spans in the agent turn from day one;
  - express the judge's budget explicitly as a share of the shared cap, so the configured judge figure ($0.50) cannot be mistaken for the operative limit;
  - plan for a direct database connection, rather than only the Data API, where latency matters.
- **D:** The Data API was right for cost but is the likely latency culprit. After the deploy, the judge was skipped with `budget:shared_daily_cap`. The configured judge budget ($0.50) is not the binding limit: the judge only gets the shared cap's ($0.25) headroom above the agent reserve. The exact usage balance at the time is not available. Missing early per-stage timing made the 8.5–11 s agent turn hard to explain.
- **E:** Cost Analysis §6; Gap Analysis.
- **F:** "Anything you'd remove?" → The ten secrets could be fewer, but they buy least privilege; I'd keep them.

## C. LLM engineering

**C1. Why use an LLM?**
- **S:** Drivers write free text in three languages. Rules cover the common patterns, but the model is far better: 100% vs 61% intent, and 97.8% vs 69.2% ETA recall on synthetic held-out data (prompt p5).
- **D:** The model's job is narrow, filling a typed form. That makes it measurable: golden sets, schema validity and field accuracy.
- **E:** `golden_h1_model_20261118_0907.json`, `golden_h1_rules_…`.
- **F:** "Why not fine-tune a small model?" → There's no real data yet, and Haiku at about a third of a cent per extraction is cheap enough. Revisit with real volume.

**C2. Why not deterministic code?**
- **S:** We have it, as the rules reader, and it's the fallback. It just understands much less.
- **D:** The rules reader gets 59% of turns fully right against 95% for the model on h1. It is used when the model fails or the budget is spent, so a degraded system still books safely, with more questions asked.
- **E:** `setuhaul/agent/rules_extractor.py`; h1 results.
- **F:** "Where is deterministic preferable?" → Everywhere after extraction: feasibility, holds, approvals, replies.

**C3. How are models selected?**
- **S:** One model for one job: Claude Haiku 4.5, chosen on golden-set results and cost, pinned at start-up.
- **D:** `model_for()` builds the provider once; it never changes mid-conversation. Golden runs on v1–v4 drove prompt changes p1→p3. Sonnet was used only offline, for generating the held-out set and for a phrasing spike.
- **E:** `setuhaul/agent/models.py`; `golden_v*`; `COSTS.md`.
- **F:** "Did you compare other models on the current prompt?" → No. That's a P3 gap (model portability).

**C4. How is fallback handled?**
- **S:** No fallback model. Every failure ends in the rules reader, with the reason recorded.
- **D:** Failures include timeout, 4xx/5xx, malformed output, a provider other than the pinned one, and budget refusal. At most one retry, for specific transient errors only. `TurnResult.fallback_reason` and `model_usage.error_category` say why.
- **E:** `docs/3-build/README.md` "Model provider"; `openrouter.py`.
- **F:** "Why not fall back to another model?" → It would be a new data path, new behaviour and new cost. The rules reader is predictable and free.

**C5. How is context managed?**
- **S:** Per turn: static system prompt first, then a user block with this turn's facts (shown options, pending read-back, the driver's message). There is no long-term memory.
- **D:** Facts the conversation needs are stored as `thread_facts` and `thread_flow` in PostgreSQL and passed back each turn. The model is told to copy option ids only from the context and never to invent a time.
- **E:** `prompts.py`; `orchestrator._user_block`.
- **F:** "Token growth with long conversations?" → h1 averaged about 1,957 input tokens; trimming context is a planned optimisation.

**C6. How are hallucinations controlled?**
- **S:** The model can't say anything to the driver; it only fills fields, and code validates every field before use.
- **D:** The layers:
  - Null is allowed and preferred for anything unstated.
  - Times are format-checked.
  - Option ids must be among those shown.
  - "Explicit confirmation" only counts after a read-back.
  - Replies are templates filled from database facts; "confirmed" appears only when the database says CONFIRMED.
  - On golden sets, ETA precision is 100%: no invented times.
- **E:** `extraction.py`; `replies.py`; h1 `eta_precision: 1.0`.
- **F:** "And if it misreads a time?" → The read-back catches it; the driver must say YES.

**C7. How are structured outputs validated?**
- **S:** With a pydantic model with literal enums, bounded strings and unknown keys rejected.
- **D:** Schema drift is treated as an error. Half-filled ranges are dropped rather than failing the whole extraction, a fix from live replay case BB09. Schema validity was 95.5% on prompt p1 and 100% from p2.
- **E:** `extraction.py` `class Extraction`; `golden_v1_model_*`.
- **F:** "Does the provider enforce the schema?" → Not reliably (the judge-p1 length issue showed that), so we validate ourselves.

## D. Tool calling

**D1. How are tools defined?**
- **S:** As typed contracts in the design, implemented as service methods that call PostgreSQL definer functions.
- **D:** There are 10 agent tools: three read, six write and one hand-off. Staff-only operations (decide, move, kill switch) are not agent tools.
- **E:** `docs/2-design/04_tool_contracts_and_states.md`; `orchestrator.py`; `service.py`.
- **F:** "Who can add a tool?" → A code change with tests and a release; the model can't discover tools.

**D2. How are tool schemas validated?**
- **S:** Arguments are built by code from validated extraction fields, then re-checked by the database function.
- **D:** The model never passes arguments to a tool. Each function checks its own preconditions: ownership of the conversation, option freshness, expiry, the fence and the role.
- **E:** `service.py`; migrations 0010–0013.
- **F:** "Example of a refusal?" → `STALE_SHIPMENT`, `BOOKING_KILLED`, slot taken.

**D3. How does the LLM select tools?**
- **S:** It doesn't. Code selects the tool from the validated facts.
- **D:** The only tool the model sees is the forced `record_driver_report` form. The orchestrator's `_decide` maps intent and state to the next tool.
- **E:** `orchestrator.py` `_decide`.
- **F:** "Isn't that limiting?" → It's the point; the model's errors can't become actions.

**D4. How is authorization enforced?**
- **S:** By a principal per caller, per-role database logins, row-level security per facility and role checks inside definer functions.
- **D:** The agent's login can't decide approvals; this was demonstrated live in smoke check 5. Staff context is set once per transaction, and a second attempt fails.
- **E:** `renewal_gates.json` WORKLOAD_IDENTITY (`staff_context_probe`); smoke check 5.
- **F:** "What about the console?" → Cognito MFA and groups, and a pre-token Lambda adds the facility scope.

**D5. How are duplicate calls handled?**
- **S:** De-duplicated at every hop.
- **D:**
  - The FIFO dedup id is the message id.
  - `inbound_receipts` stores each message once.
  - There is one reply per message and generation.
  - Idempotency keys exist for writes.
  - A repeated YES or decision changes nothing (smoke check 3).
  - A redelivered Telegram update is stored once.
- **E:** migrations 0011–0013; smoke check 3; FULL_ACTIVATION_RUNBOOK §12 step 6.
- **F:** "Exactly-once?" → I don't claim exactly-once. Delivery is at least once; ordering is FIFO per driver; duplicates are de-duplicated and business effects are idempotent (a repeat changes nothing).

**D6. How are retries handled?**
- **S:** Retries are bounded and fenced.
- **D:**
  - Model calls: at most 1 retry, for specific transient errors.
  - SQS redelivers if a turn fails; a turn that lost its lease commits nothing (`TurnLeaseLost`).
  - The outbound relay retries notifications.
  - Messages stuck too long are recovered after 30 s or 6 min.
  - Poison messages go to the DLQ with an alarm.
- **E:** `drill_lease_loss.json`, `drill_dlq_*`.
- **F:** "Retry storms?" → The model retry is single and budget-checked; SQS uses max receives and then the DLQ.

**D7. Can the LLM execute arbitrary operations?**
- **S:** No. It has no tools and no database access. It returns a form.
- **D:** Even a perfectly crafted injection can only produce field values. Those go through the read-back, the driver's YES, feasibility, the database and a planner.
- **E:** `orchestrator.py`; Technical Appendix §6.
- **F:** "What about SQL injection via the message?" → All queries are parameterised and the text is stored as data.

## E. Guardrails and security

**E1. How is prompt injection handled?**
- **S:** As data. The prompt says so, but nothing relies on it: the model's output is a validated form with no power.
- **D:** The golden replay includes an adversarial group, which passed 7/7 live. Worst case, a field is wrong and is caught by the read-back or by feasibility.
- **E:** `prompts.py`; `replay_v4_live` by_group.
- **F:** "Indirect injection through shipment notes?" → The context includes structured facts only; a red-team is a P1 gap.

**E2. How is PII protected?**
- **S:** Message text and contact details stay in PostgreSQL. Traces and logs carry internal ids and opaque refs only. The model route has zero data retention.
- **D:**
  - Ids pass a format gate.
  - Redaction runs at the export boundary.
  - The chat id becomes a tag.
  - Locations are stored as a geohash.
  - All data is synthetic now; real data needs consent and a retention schedule.
- **E:** `identifiers.py`, `redaction.py`; commit `ff4076d`; G2 smoke `blocked_leaks: 0`.
- **F:** "Data residency?" → AWS Sydney. The model goes via OpenRouter to Bedrock under ZDR. For real data, a DPIA (Data Protection Impact Assessment) comes first.

**E3. How are business policies enforced?**
- **S:** In domain code and in the database, never in the prompt.
- **D:** Feasibility applies 12 hard constraints. Approval rules, offer expiry and hold TTL are enforced, a declined offer can't be booked, and the requester can't decide. Automatic confirmation requires a written owner policy; none is on.
- **E:** `domain/optimizer.py`; migrations 0015, 0036, 0040; `CLIENT_READOUT.md`.
- **F:** "Who changes a policy?" → A code or configuration change, released through the gated workflow.

**E4. How are approval boundaries enforced?**
- **S:** Step Functions waits for a planner's decision through `decide_booking_request`; the database checks the decider's role and that the decider is not the requester.
- **D:** Approval expires on a timeout. The agent's identity is refused (smoke 5). A duplicate click changes nothing.
- **E:** `approval_workflow.asl.json`; smoke 2a–2c, 3, 5; `drill_approval_*`.
- **F:** "Can an ops lead bypass?" → No. Ops leads can move a confirmed slot, but the database re-checks it.

**E5. How are tenant boundaries maintained?**
- **S:** There's one tenant. Facilities are isolated by row-level security and staff scope; multi-customer tenancy isn't built.
- **D:** A facility scope comes from Cognito via the pre-token Lambda, and RLS filters every query. For multi-customer use I'd add a tenant column, RLS policies and isolation tests.
- **E:** `pretoken.py`; RLS migrations; smoke check 5 (other-facility refused).
- **F:** "Why not now?" → No second customer; this is in the gap analysis as P2.

**E6. What happens if the LLM ignores instructions?**
- **S:** Nothing unsafe. It can only fill a form badly, and that hits validation, the read-back and the driver's YES before anything is held.
- **D:** The safety-word net in code catches emergencies even if the model sets `safety_concern=false`.
- **E:** `extraction.py` `SAFETY_WORDS`.
- **F:** "False positives on 'medical' cargo?" → Handled: cargo words don't count (G3 ADV02).

**E7. What are the kill switches?**
- **S:**
  - Booking kill switch (database state; ops lead or owner);
  - Telegram outbound off;
  - Telegram inbound off;
  - judge off;
  - tracing off;
  - pilot sleep;
  - back to the test sink.
- **D:** Each is independent. Booking off was demonstrated live in smoke check 6: `BOOKING_KILLED`, escalated, nothing booked.
- **E:** `ops/kill_switch.py`; FULL_ACTIVATION_RUNBOOK §11; smoke check 6.
- **F:** "How fast?" → The next booking attempt; it is checked in the database transaction.

## F. Golden dataset and evaluation

**F1. Why a golden dataset?**
- **S:** To turn "it seems to work" into a regression-tested number for the model's one job.
- **D:** Each turn has expected fields. Gates on schema validity, ETA precision and recall, and intent accuracy block a prompt change that regresses.
- **E:** `evals/golden.py`, `evals/golden/v1–v4`, `h1`.
- **F:** "How big is enough?" → It's sized to cover categories, not to give statistical power; real data will grow it.

**F2. How was it constructed?**
- **S:** Hand-written from the brief's scenarios and data audit (v1–v4), plus a held-out set generated separately by a different model and frozen.
- **D:** Groups: brief beats, delay ladder, language, policy, adversarial, audit traps, seed threads. Each version is hashed and the hash recorded in results.
- **E:** `evals/golden/*`; `golden_*` result `dataset` fields.
- **F:** "Synthetic bias?" → Real. That's why real anonymised messages come first in a real pilot.

**F3. How are edge cases represented?**
- **S:** As explicit groups: adversarial, ambiguity, half ranges, 12-hour times, cargo vs safety words, Hinglish.
- **D:** Replay groups: adversarial 7/7, audit trap 9/10, brief beat 8/10, delay ladder 10/10, language 8/8, policy 7/7, seed threads 12/12 (live, v4).
- **E:** `replay_v4_live_20261118_0803.json`.
- **F:** "What failed?" → 3 scenarios, with 0 hard-gate violations: misreads caught downstream.

**F4. How do you prevent dataset leakage?**
- **S:** The held-out set was generated separately, frozen, and used only for scoring, never for prompt tuning.
- **D:** Prompt changes were tuned on v1–v4; h1, also synthetic, confirmed generalisation: 98% intent on p3 and 100% on the deployed p5, though option pick fell from 100% to 91.7% (one miss, BB05). It's recorded by hash.
- **E:** h1 dataset hash `2e72cb40af0b106d`.
- **F:** "But you've seen the h1 results now." → Yes. The next held-out set should come from real messages.

**F5. How are evaluation cases versioned?**
- **Note:** all model-backed runs were re-run on the deployed prompt p5 on 26 Nov: h1 158/167 all fields right (unchanged), v4 74/76, live replay 62/64 with 0 hard-gate violations. BB12 and AT02 fail on both p3 and p5; causes not established.
- **S:** Dataset hash, prompt version, schema version and model id in every result file.
- **D:** Decision T28: every model call records the prompt version, and a prompt change needs a golden run. LangSmith datasets are named by version.
- **E:** result files `run` block.
- **F:** "Are evals tied to commits?" → Phase G results record the `code_version`; golden results record the prompt.

**F6. How do you assess model regression?**
- **S:** Re-run golden and held-out sets on the new prompt or model and compare gates and per-field accuracy. Replay checks whole-conversation safety.
- **D:** The scripted versions run in the test suite. Paid runs are approved and capped, and the cache makes unchanged reruns $0.
- **E:** `evals/cache/model_calls.jsonl`; T42.
- **F:** "Why isn't that in CI?" → Cost control. I'd add an approved weekly job with thresholds (P1).

**F7. What are the acceptance criteria?**
- **S:**
  - golden-set gates: schema valid, ETA precision, ETA recall and intent accuracy all PASS;
  - replay: zero hard-gate violations;
  - judge: beat the always-5 baseline and catch 5/5 planted defects.
- **D:** The model passed all golden gates on v4 and h1. Live replay had 0 hard-gate violations. Judge prompt p2 was accepted under both owner rules on 25 Nov.
- **E:** result files; `LLM_JUDGE.md`.
- **F:** "Who approved the criteria?" → The owner, recorded with dates.

**F8. How do you know the reference answers are correct?**
- **S:** The golden labels are hand-written and reviewed. The judge's AI references are model-generated and labelled as such, which is why I scored the 20 calibration cases myself. That calibration has one reviewer and isn't blind, so it's labelled that way too.
- **D:** Native-speaker review covered the Hindi and Hinglish templates (S4-R2-08). The judge references were accepted by the owner but are not human scores.
- **E:** `ai_reference_scores_v1.json` (label); `human_scores_v1.json` (empty).
- **F:** "Inter-annotator agreement?" → Not measured; double-label 10% is a P1 action.

## G. LLM-as-a-Judge

**G1. Why use a judge?**
- **S:** For reply quality (clarity, tone, language match) that rules can't score. It's advisory only.
- **D:** Business-critical checks stay deterministic. The judge never gates a booking. It runs after the reply is committed, on 20% of turns.
- **E:** `online_judge.py`; `judge_core.py`.
- **F:** "If replies are templates, why judge them?" → Templates are filled per turn and language; the judge watches for regressions such as language mismatch.

**G2. How is the judge model selected?**
- **S:** Haiku 4.5 on the same pinned, zero-retention route: no new vendor or data path, and cheap at $0.0033 per judgement.
- **D:** The trade-off is self-preference bias. It's mitigated by the baseline, the planted defects and one non-blind human calibration; a blind second reviewer is the next step.
- **E:** `judge_20261125_172110.json`.
- **F:** "Why not a stronger model?" → Possible after calibration shows a need; it's a cost decision.

**G3. How is the rubric designed?**
- **S:** Three dimensions scored 1–5, each with anchored descriptions, plus a forced tool `record_judgement` with a rationale and evidence quote.
- **D:** Prompt p2 fixed p1's overlong rationales (18/30 unparseable). Validation is strict.
- **E:** `LLM_JUDGE.md` §10–11.
- **F:** "Why not pairwise comparison?" → Single replies, advisory; pairwise is useful for prompt A/B later.

**G4. How is judge agreement measured?**
- **S:** The share of dimension ratings within ±1 of the reference, plus exact match, MAE and signed error per dimension.
- **D:** Against my own scores (20 cases, 60 ratings, 26 Nov): 93.3% within ±1, 83.3% exact, average error 0.30, no systematic bias; clarity 90%, tone 95%, language 95%. Against the earlier AI reference it was 95.0%. My scores match that reference on 53 of 60, and I had seen it the day before, so this isn't blind.
- **E:** `llm_judge_run2_report.md`.
- **F:** "93% sounds high." → An always-5 baseline gets 88.3% against the same scores, so agreement alone is weak; see G5.

**G5. How do you detect judge bias?**
- **S:** With signed error per dimension, the always-5 baseline and planted negative cases.
- **D:** The negative cases (J16–J20) each contain one deliberate defect. The judge must score the defective dimension lowest. p2 found 5/5; the baseline found 0/5. Against my scores, the judge still under-penalised two of them: J17 clarity and J20 tone (I gave 1, it gave 3).
- **E:** `LLM_JUDGE.md` §7b.
- **F:** "Self-preference?" → Only partly: one non-blind human reviewer reduces the risk; a blind second reviewer would measure it.

**G6. How do you handle false positives?**
- **S:** The judge is advisory, so a false positive can't change an outcome. We look at large disagreements by hand.
- **D:** Four disagreements of 2+ points against my scores: J13 language (I gave 5, judge 1) and J14 clarity (5, 2) are judge false positives; J17 and J20 are planted defects the judge partly missed. These are inspected, not acted on automatically.
- **E:** run 2 report.
- **F:** "False-negative rate?" → Not stable with one reviewer and 20 cases. Against my scores the judge under-penalised J20 (tone: I gave 1, it gave 3).

**G7. What happens when judge evaluation fails?**
- **S:** A SKIPPED, ERROR or UNPARSEABLE row is written and logged. It never raises into the worker and is never retried.
- **D:** After the deploy, judgements were skipped with reason `budget:shared_daily_cap` (reported). Two different numbers are involved:
  - the configured judge budget, $0.50/day (stack output `LlmJudge`), which is the judge's own sub-cap;
  - the shared application cap, $0.25/day. The judge may only use its headroom above the agent reserve, which defaults to half the cap; no override was found in the stack.

  The skip reason means the shared-cap check refused the reservation. The exact usage balance at that moment is not available. Changing either budget is an owner decision; I don't recommend raising anything without that approval.
- **E:** `online_judge.py`; stack output `LlmJudge`.
- **F:** "So the judge isn't running?" → It runs when budget allows; 9 turns were scored earlier on 26 Nov (reported).

**G8. Can the judge itself hallucinate?**
- **S:** Yes. That's why it's advisory, validated strictly, and calibrated against baselines and one human reviewer, not yet blind or multi-reviewer.
- **D:** Evidence quotes are bounded. Malformed output is rejected. Scores never reach a decision path.
- **E:** `judge_core.py`.
- **F:** "Then why trust it at all?" → I trust it as a cheap trend signal, not as truth.

**G9. How do you calibrate it?**
- **S:** A person scores the 20 calibration cases without the judge's output, then `judge_calibration.py` compares. I did it on 26 Nov: 93.3% within ±1, accepted for advisory use. One reviewer, and not blind to the AI reference.
- **D:** The sheet and tooling exist (`calibration_sheet_v1.md`, `pilot_calibration.py`). AI-to-AI agreement never moves the calibration status. Still open: a blind second reviewer, and `pilot_calibration.py` on real pilot rows.
- **E:** `evals/judge/*`.
- **F:** "When?" → Before any decision uses judge scores; it's the first item on the AI roadmap.

## H. Observability

**H1. Why LangSmith?**
- **S:** Run trees, thread grouping, feedback on runs, and datasets with experiments in one tool, which fits evaluation and tracing together.
- **D:** We export via our own REST client, restricted to allowed endpoints, so the vendor sees only what we choose. The G2 smoke exported 96 runs with 0 leaks.
- **E:** `langsmith.py`; `g2_smoke_20261125_015757.json`.
- **F:** "Lock-in?" → Low: the tracer is ours; only the exporter is LangSmith-specific.

**H2. What is a trace?**
- **S:** The tree of everything that happened for one request, here one driver turn, with timing and outcome.
- **D:** The root `agent_turn` has child runs: the extraction (LLM), each tool, the judge. A thread groups traces of one conversation.
- **E:** `tracing.py`.
- **F:** "Trace vs log?" → H8.

**H3. What is a run?**
- **S:** One node in the trace: an LLM call, a tool call or a chain step, with inputs and outputs (metadata only here), timing and status.
- **D:** Run types: chain, llm, tool. Ids are derived deterministically (the turn ref is uuid5 of correlation, message and generation) so redeliveries correlate.
- **E:** `orchestrator.py` `TURN_NS`.
- **F:** "Why deterministic ids?" → So a redelivered turn joins the same trace.

**H4. How do parent-child spans work?**
- **S:** Each child run carries its parent's id and the trace id; the judge is created as a child of the turn it scores.
- **D:** Since PR #33, `telegram_inbound`'s run id equals the turn's ingest correlation id, and the main conversation path (inbound, agent turn, judge, outbound) shares `thread_id`. Four inbound runs for link codes and button callbacks do not yet.
- **E:** commit `ff4076d`; `tests/test_demo_reliability.py`.
- **F:** "Gaps?" → 4 inbound runs (link codes, button callbacks) lack `thread_id`.

**H5. How do you correlate a failed booking?**
- **S:** Filter by internal driver or shipment id, open the thread, read the turn outcome, open the failing tool span, then confirm in the database audit tables.
- **D:** Technical Appendix §14 maps each layer (API, orchestrator, LLM, tool, authorization, database, Telegram) to the signal that shows the failure.
- **E:** Appendix §14.
- **F:** "And if tracing was down?" → CloudWatch `agent.turn` lines and the database tables carry the same correlation ids.

**H6. How do you track a driver workflow?**
- **S:** `driver_id` on every turn trace, CloudWatch line and Ops metrics row, with a driver filter in the console.
- **D:** Format-gated: a malformed id exports as `[INVALID_ID]`, never as raw text.
- **E:** PR #17; `tests/test_driver_traceability.py`.
- **F:** "Is the driver id personal data?" → It's an internal synthetic id; for real data, it's pseudonymous and access-controlled.

**H7. How do you identify expensive LLM calls?**
- **S:** The LLM span and the usage ledger carry tokens and cost per attempt; the console sums them by model and status.
- **D:** The ledger records the provider-reported cost; an unknown cost keeps the reservation (conservative).
- **E:** `model_usage`; screenshot `console_model_ledger_local_failure_injection.jpg`.
- **F:** "Alerting on spend?" → The cap enforces it. An AWS Budgets alarm is a P1 action.

**H8. How do you avoid logging sensitive information?**
- **S:** Allowlisted fields only, an id format gate, redaction at export, and no message text unless explicitly enabled (and then redacted).
- **D:** `SETUHAUL_TRACE_CONTENT` is off. Chat ids become tags. Tests assert no phone, text or token appears in metadata.
- **E:** `redaction.py`; `orchestrator._TRACE_FIELDS`.
- **F:** "Logs vs metrics vs traces?" → Logs show what a component said, metrics show how often and how fast, traces show what happened to this request.

## I. Testing and reliability

**I1. How do you test nondeterministic workflows?**
- **S:** Separate the nondeterminism. Logic is tested with a scripted model; the model is evaluated with golden sets and replay.
- **D:** The orchestrator, tools and database run against a `FakeModel` in 2,080 tests. Model quality is measured offline with approved, capped runs.
- **E:** `tests/`; `evals/`.
- **F:** "Flaky tests?" → One redaction test was flaky in local runs (reported); the earlier flaky allowlist test was fixed in PR #19.

**I2. What is your testing strategy?**
- **S:** Unit, database-integration, Data API emulation, concurrency, security and authorization, agent scripted, golden and replay, live smoke and drills.
- **D:** CI runs the full suite on PostgreSQL 17.9, matching the pilot, plus the Data API path. Activation reruns everything before switching booking on.
- **E:** CI run 45836948268; activate-booking runs.
- **F:** "Coverage %?" → Not measured; I'd rather show behaviour tests and drills.

**I3. How do you test tool calls?**
- **S:** Each tool path is tested against a real PostgreSQL, with exactly the logins the deployed function has.
- **D:** The v0.2.1 regression test reproduced the Confirm-500 defect using only the deployed API logins.
- **E:** `15_RELEASE_NOTES.md` v0.2.1.
- **F:** "And through the Data API?" → 678 tests on the emulator path in the same CI run.

**I4. How do you prevent duplicate bookings?**
- **S:** With a per-dock lock and an exclusion constraint, so two overlapping claims can't be stored by anyone.
- **D:** 20 concurrent "hold" requests within 5 ms gave exactly 1 winner. The 19 losers were told and re-offered, with 0 overlaps. Even a forced database-administrator insert was refused. The suite also runs 50 concurrent requests. Live smoke check 4 passed on the pilot.
- **E:** `INTEGRITY_PROOF.md`; smoke check 4.
- **F:** "Across facilities?" → Docks belong to one facility; the scheduler runs per facility.

**I5. How do you handle concurrency?**
- **S:** Through database locks and constraints, FIFO ordering per driver, and leases with fences per turn.
- **D:** A worker that lost its lease or whose message got a new generation commits nothing more (`assert_turn_fence`). The lease-loss drill demonstrated this on the pilot.
- **E:** migrations 0012–0013; `drill_lease_loss.json`.
- **F:** "Deadlocks?" → Locks are taken per dock in one order; no deadlock has been observed (not formally proven).

**I6. How do you validate recovery?**
- **S:** With drills on the real pilot: DLQ with alarm and redrive, worker death mid-turn, and the approval workflow including expiry and duplicate clicks.
- **D:** The RECOVERY_WORKER and AWS_QUEUE gates pass on recorded evidence.
- **E:** `docs/release/evidence/drill_*`; `renewal_gates.json`.
- **F:** "DR?" → No restore drill yet (P1).

**I7. How do you prevent regressions?**
- **S:** Every defect gets a regression test or golden case. CI must pass, and activation reruns the suite.
- **D:** Examples:
  - the expired-offer reply;
  - the duplicated read-back label;
  - declined offers unbookable (0040);
  - message ids per chat (0038).
- **E:** PRs #15, #23; release notes.
- **F:** "Model regressions?" → F6.

## J. Performance and cost

**J1. What is the most expensive operation?**
- **S:** In money, the LLM extraction at about $0.0013–0.0036 per turn. In time, the agent turn on the pilot at 8.5–11 s.
- **D:** At scale, LLM spend dominates. At low volume, AWS fixed cost dominates.
- **E:** Cost Analysis §2, §4.
- **F:** "Cost per booking?" → J3.

**J2. What causes the most latency?**
- **S:** The agent turn on the pilot, 8.5–11 s, against about 2 s for the model call itself. It isn't broken down yet.
- **D:** Hypotheses (not findings): Data API round trips (1–3 transactions per tool), Lambda cold starts, secrets and identity lookups on cold invocations. Locally the same turn takes about 1.9 s.
- **E:** commit `ff4076d` (8.3 s p50 before); reported after-numbers; `PHASE_G_PERFORMANCE_REPORT.md`.
- **F:** "Then why claim targets?" → I don't; both were missed and are shown as missed.

**J3. How is cost per booking calculated?**
- **S:** 3 model-read turns × cost per extraction + 20% judge sampling × 3 × cost per judgement = $0.006–0.013 per booking (projection).
- **D:** Non-booking traffic and retries are excluded until measured. Cost per *successful* booking is higher by the success-rate factor (1.5–2× assumed).
- **E:** Cost Analysis §4.
- **F:** "Where's the measured number?" → Not available from the pilot without reading its ledger; the formula uses measured unit costs.

**J4. How does cost change with scale?**
- **S:** Not linearly. Fixed AWS dominates at 100 a month (about $113 per 1,000 bookings); the LLM dominates at 100,000 a month (about $16.5 per 1,000 in total, upper estimate).
- **D:** There are steps: once the pilot must stay awake, Aurora's floor (about $73 a month) applies. By estimate, the $0.25/day cap alone supports about 20–42 bookings a day ($0.25 ÷ $0.0059–0.0128 per booking, including the judge); any change is an owner decision. The OpenRouter key's $10 limit never resets, so I don't derive monthly capacity from it. The 10k and 100k figures are projections, not demonstrated AWS capacity.
- **E:** Cost Analysis §4.3–4.4.
- **F:** "LangSmith at scale?" → Unknown cost; trace sampling is the mitigation.

**J5. How do you optimise token consumption?**
- **S:** One call per turn, templates instead of generated replies, a compact forced schema, and static prompt text first for caching.
- **D:** Planned:
  - skip the model when the rules reader is certain (bare numbers, YES);
  - trim conversation context;
  - batch pricing for offline evals.
- **E:** `prompts.py`; `orchestrator._bare_number`.
- **F:** "Is caching working?" → `cache_read` was 0 in the golden runs, so not realised via this route.

**J6. Why use smaller models?**
- **S:** The job is narrow extraction, where a small fast model is accurate enough (100% intent on synthetic held-out data, prompt p5) at a fraction of the cost and latency.
- **D:** Sonnet was used only offline. A larger model adds latency to an already slow turn.
- **E:** golden results; `COSTS.md` price table.
- **F:** "Smaller still?" → Worth testing on h1 (P3).

**J7. What happens when the LLM budget is exhausted?**
- **S:** No request is sent; the rules reader takes over; booking is unaffected. It's reserved atomically in PostgreSQL before each call.
- **D:** The OpenRouter key limit ($10 total, no reset) is a second, independent safeguard, never retried. The judge can't starve the agent.
- **E:** migration 0023; `COSTS.md`; screenshot (BUDGET_EXCEEDED → rules-fallback, local).
- **F:** "User impact?" → Lower understanding (61% vs 100% intent on synthetic held-out data), so more clarifying questions.

**J8. How would you reduce AWS expenditure?**
- **S:** It's already near the floor: no NAT, no proxy, scale to zero, schedules asleep when idle. Next: fewer secrets if least privilege allows, and AWS Budgets alarms.
- **D:** The biggest avoidable cost is leaving the schedules awake (about $73 a month of Aurora). The `pilot_mode sleep` command handles it.
- **E:** `09_LOW_COST_PILOT_PLAN.md`; `pilot_mode.py`.
- **F:** "Actual bill?" → Not in the repository; the owner should read the Billing console.

**J9. What is the impact of judge evaluation on operating cost?**
- **S:** About $0.002 per booking at 20% sampling, roughly 15–35% of LLM cost per booking, and capped.
- **D:** At $0.0033 per judgement × 0.6 judgements per booking. Sampling could drop to 5% after blind, multi-reviewer calibration.
- **E:** Cost Analysis §4.2.
- **F:** "Is it worth it?" → It's calibrated by one non-blind reviewer, so it's still a monitoring experiment, advisory only.

## K. Deployment and operations

**K1. How is the application deployed?**
- **S:** GitHub Actions: CI on every PR; a release PR into `release/fde-sydney-pilot`; then the `activate-booking` workflow with an owner approval reference, which tests, synthesises, deploys the checked assembly, migrates and records evidence.
- **D:** Allow-listed refs, a reproducible Lambda zip, synthesis checked without credentials, and a deployment manifest bound to evidence.
- **E:** `.github/workflows/ci.yml`, `activate-booking.yml`; runs 45833537353, 45836951650.
- **F:** "Who can deploy?" → Through the workflow with an owner reference only.

**K2. How are migrations managed?**
- **S:** 46 forward-only, additive migrations, applied over the Data API in the deploy job and tested in CI on PostgreSQL 17.9.
- **D:** Risky ones have runbooks (`RUNBOOK_0011.md`) and recovery rules (0045 bounded recovery of an expired authorization).
- **E:** `migrations/`; `setuhaul/aws/migrate_data_api.py`.
- **F:** "Rollback of a migration?" → Forward-fix only; additive design makes old code compatible.

**K3. How do you roll back?**
- **S:** Each control has an independent off switch (booking, Telegram in or out, judge, tracing, back to the sink), and code rolls back to a previous tagged release.
- **D:** Tags `v0.2.1` and `v0.3.0` were rollback targets in earlier releases.
- **E:** FULL_ACTIVATION_RUNBOOK §11; `15_RELEASE_NOTES.md`.
- **F:** "Data rollback?" → None needed for additive migrations; booking data is append-only and audited.

**K4. How are environments separated?**
- **S:** dev, eval, ci, staging and pilot are allowed; production is refused in code. The synthetic data class is mandatory, and the CI workflow never turns booking on.
- **D:** The pilot is in its own stack and region with its own secrets; the Telegram test bot mode is refused in the pilot.
- **E:** `models.py`; `ci.yml`; PR #15.
- **F:** "How would production differ?" → Its own account, PostgreSQL 18, Meta, real-data governance (K5).

**K5. What is required before production?**
- **S:**
  - real-data governance and consent;
  - WhatsApp/Meta approval;
  - PostgreSQL 18 and drift detection;
  - blind, multi-reviewer judge calibration and an eval gate;
  - latency inside target;
  - an AWS load test;
  - a DR drill and on-call.
- **D:** The three owner exceptions (AURORA, WORKLOAD_IDENTITY, OUTBOUND_DELIVERY) are effectively the production checklist.
- **E:** `renewal_gates.json`; Gap Analysis.
- **F:** "Timeline?" → It depends on the customer's governance; the engineering items are weeks, not months.

**K6. How do you monitor the application?**
- **S:** With CloudWatch alarms (DLQ, errors, 4xx) to SNS, the console Ops and Observability tabs, LangSmith traces, and the weekly booking-evidence renewal.
- **D:** There's no trace-based alerting and no latency SLO alarm yet.
- **E:** `infra/setuhaul_stack.py`; console screenshots.
- **F:** "Who gets paged?" → Not defined yet (incident management is P1).

**K7. What are the operational risks?**
- **S:** A single owner for approvals, the manual weekly renewal, an untested DR, no chase for silent drivers, and real Telegram failure handling not exercised.
- **D:** Each has a recommended action in the gap analysis.
- **E:** Gap Analysis.
- **F:** "Biggest one?" → Owner dependency: add a second approver and a RACI.

## L. FDE mindset and leadership

**L1. How did you handle ambiguity?**
- **S:** I wrote down facts, estimates, assumptions and unknowns, and turned each open question into an explicit owner decision with a date.
- **D:** Examples:
  - OD-2: breakdown vs safety;
  - OD-3: templates instead of a phrasing model;
  - T33-P: the PostgreSQL 17.9 exception;
  - T52: OpenRouter with ZDR.
- **E:** `docs/2-design/01_decision_register.md`; build log.
- **F:** "One you got wrong?" → The phrasing model; OD-3 removed it after evaluation.

**L2. How did you prioritise?**
- **S:** Correctness first (one winner per contested slot), then safety and authority, then the language quality that drives value, then cost and latency.
- **D:** The Phase 1 conclusion: spend engineering on concurrency, idempotency and escalation, not throughput.
- **E:** `01_problem_definition_sheet.md` §2.
- **F:** "What did you cut?" → The driver app, device ETA signals, and "talk to a person" (owner-deferred).

**L3. What was the hardest engineering decision?**
- **S:** Not letting the model write replies or choose tools, even though it looks less impressive.
- **D:** It costs some fluency but keeps every expensive decision out of the model's reach by construction, makes replies testable, and shrinks the model surface to one call per turn.
- **E:** OD-3; `orchestrator.py` docstring.
- **F:** "Would a customer prefer a chattier bot?" → Then that's measurable, and the judge plus wider human calibration would tell us.

**L4. What would you redesign?**
- **S:** Per-stage spans in the agent turn from the start, a judge budget stated as a share of the shared cap, and planning for lower-latency database access.
- **D:** See B7.
- **E:** Cost Analysis §6.
- **F:** –

**L5. What did you deliberately not automate?**
- **S:**
  - confirming bookings;
  - prioritising trucks under scarcity;
  - anything involving safety;
  - penalties and customer commitments;
  - moving confirmed appointments without an ops lead.
- **D:** These stay with people because they carry accountability, and errors are costly or irreversible. Automatic confirmation needs a written policy; none is on.
- **E:** `CLIENT_READOUT.md` "What stays with your staff".
- **F:** "Ever?" → Low-risk rules (e.g. night arrivals at one facility) once policy and evidence support them.

**L6. How would you handle a customer changing requirements?**
- **S:** Classify the change as policy (configuration plus owner sign-off), behaviour (golden cases first, then code) or architecture (decision record plus migration plan), and release it through the gated workflow.
- **D:** The decision register and traceability keep each change linked to a test.
- **E:** `TRACEABILITY.md`; decision register.
- **F:** "And if it's urgent?" → The kill switch and feature switches buy time without a rushed deploy.

**L7. How would you balance speed, quality and cost?**
- **S:** Make quality non-negotiable where errors are expensive (bookings), move fast where it's reversible (replies, console), and cap cost by construction.
- **D:** Examples:
  - a $0.25/day cap with a rules fallback;
  - an eval cache that makes reruns free;
  - scale-to-zero infrastructure;
  - weekly renewal instead of permanent authorization.
- **E:** `COSTS.md`; T42.
- **F:** "Where did you over-engineer?" → Possibly the renewal accounting (categories A–E), but it's what made a safe weekly cadence possible.

**L8. How would you measure adoption?**
- **S:** By linked drivers, weekly active drivers, the share of exceptions handled without a phone call, planner time per request, and repeat use after the first booking.
- **D:** Most of these are queryable from existing tables; a baseline period is needed first.
- **E:** Technical Appendix §8.
- **F:** "Targets?" → Set with the customer after the baseline, not before.

---

## Hard questions you should expect, with one-line answers

| Question | Answer |
|---|---|
| "So the AI barely does anything?" | "It does the one thing code can't, reading messy language (100% intent on 167 synthetic held-out turns), and nothing that can cost money when it's wrong." |
| "Your latency target was missed." | "Yes: 15.0 s against < 15 s, on six reported exchanges that aren't committed evidence yet. Removing the one-minute wait is a preliminary improvement, not a service level, and the next bottleneck is identified." |
| "Your judge agrees with another AI, so what?" | "Exactly. That's why it's advisory, tested against a dummy baseline and planted defects, and why I scored it myself: 93.3% within a point, one non-blind reviewer, so it stays advisory." |
| "Is any of this in production?" | "No. It's a synthetic pilot with booking under an owner approval, and the system refuses production by design." |
| "What does it cost?" | "About $4.42 of recorded model spend for development and evaluation, including $1.05 for the 26 November re-runs on the deployed prompt; a crashed first attempt adds about 14 cents by estimate, and the key's own counter is authoritative. Current pilot LLM spend isn't available: it's capped at $0.25 a day and I didn't read its ledger. Nor are the AWS and LangSmith bills. AWS idle is estimated at about $7 a month, and the bill isn't in my evidence, so I won't quote it." |
| "Why should a customer trust it?" | "Because every commitment is a database rule with an audit row, and a person approves every booking." |
